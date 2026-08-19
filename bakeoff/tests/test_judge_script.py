"""The judging batch driver: which units get a line, and which never do.

Nothing here opens a socket. `judge_event_log` has ONE seam -- `complete`, the
`CompleteFn`, one rendered prompt in and raw text out -- and every test injects
it. There is no second seam for position: at temperature 0 the two positions are
walked in a fixed order rather than drawn, so what used to need a seeded
generator is now a constant the tests read. The EVENT LOG and the GRADE FILE are
real, built in `tmp_path` with real `RunRecord`s and real `append_grade` lines,
because the thing being pinned is which runs get paired and in what order, and a
fake log would let the driver read the wrong ones and still pass.

Four properties most of these tests are really about, in the order they are
easiest to break silently:

1. **The judge never rescues the gate.** No rubric line for a run whose
   `resolved` is not `True`; no pairwise vote where one side failed; nothing at
   all where both did. A driver that got this wrong would publish a ranking over
   rows the deterministic ladder already refused.
2. **An excluded run (`resolved is None`) is not an observation of the model**
   and is dropped BEFORE grouping, so it cannot reach a pair.
3. **Resume is keyed on `(judge_model_id, judge_prompt_version,
   rubric_version)`** and a gate-decided pair is its own unit. Judgments are
   append-only, so a resume that mistook an unreadable line for an absent one
   would pay twice for verdicts already bought.
4. **The event log and `grades.jsonl` are inputs.** The driver never opens
   either for writing, and one test pins their bytes across a whole batch.
5. **The summary is a printout, not a stored score**, and the κ caveat is
   unconditional. Section 7 pins the three aggregation rules an append-only
   file makes load-bearing -- dedupe to the last line per resume identity, vote
   lines supersede a stale `gate_decided` line for the same pair, and
   `gate_decided` never enters the vote verdict distribution -- plus the one
   line no flag, branch or terminal encoding may drop.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff.eventlog import EventLog
from bakeoff.grade_schema import CheckResult, GradeRecord, append_grade
from bakeoff.judge import (
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    RUBRIC_DIMENSIONS,
    RUBRIC_FLAGS,
    RUBRIC_VERSION,
    VOTE_POSITIONS,
    _CANONICAL_VERDICTS,
    prompt_sha,
)
from bakeoff.judge_schema import (
    JudgeRecord,
    append_judgment,
    load_judgments,
    payload_relative_path,
    read_payload,
    resolve_payload_path,
)
from bakeoff.schema import (
    SCHEMA_VERSION,
    Artifacts,
    Outcome,
    RunRecord,
    TerminationReason,
)

from scripts.grade import grades_path
from scripts.judge import (
    ELO_ANCHOR,
    ELO_SCALE,
    KAPPA_CAVEAT,
    MAX_CONSECUTIVE_ERRORS,
    GateInvariantError,
    _KAPPA_CAVEAT_ASCII,
    _MAX_NAMED,
    ResumeRefused,
    _assert_rubric_gate,
    _comparison_key,
    _generation_of,
    _judge_generation,
    _print_kappa_caveat,
    _VOTE_VERDICTS,
    elo_from_outcomes,
    gating_view,
    judge_event_log,
    judgments_path,
    main,
    payloads_root,
    print_summary,
    summarize,
)

GRADER = "2"

REFERENCE_DIFF = """diff --git a/src/calc.py b/src/calc.py
index 1111111..2222222 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

DIFF_A = """diff --git a/src/calc.py b/src/calc.py
index 1111111..3333333 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return b + a
"""

DIFF_B = """diff --git a/src/calc.py b/src/calc.py
index 1111111..4444444 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a - b
+    total = a + b
+    return total
"""

DIFF_C = """diff --git a/src/calc.py b/src/calc.py
index 1111111..5555555 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return sum((a, b))
"""

DIFF_D = """diff --git a/src/calc.py b/src/calc.py
index 1111111..7777777 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return operator.add(a, b)
"""


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _task(task_id="calc-1"):
    """A manifest stand-in carrying exactly what the driver and the whitelist
    read: `prompt`, `solution_diff`, `extra_files`, `task_id`,
    `task_set_commit`."""
    return SimpleNamespace(
        task_id=task_id,
        task_version=3,
        prompt=f"Fix the adder in {task_id}.",
        solution_diff=REFERENCE_DIFF,
        extra_files=(),
        task_set_commit="taskset-def",
    )


def _record(run_id, *, task_id="calc-1", model="model-one", sample_index=0,
            attempt_number=1, final_diff=DIFF_A, **kw):
    fields = dict(
        run_id=run_id,
        task_id=task_id,
        task_version=3,
        model=model,
        harness="claude-code",
        sample_index=sample_index,
        attempt_number=attempt_number,
        started_at="2026-08-17T00:00:00Z",
        finished_at="2026-08-17T00:05:00Z",
        outcome=Outcome.RESOLVED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=3,
        collection_id="coll-1",
        artifacts=Artifacts(final_diff=final_diff),
    )
    fields.update(kw)
    return RunRecord(**fields)


def _grade(run_id, *, task_id="calc-1", model="model-one", resolved=True,
           grader_version=GRADER, **kw):
    """A grade line of the shape the ladder writes.

    `resolved is None` carries a `not_graded_reason`, because the grader's own
    invariant moves the two together and a stand-in that broke it would exercise
    the driver against a row the grade file cannot hold.
    """
    return GradeRecord(
        run_id=run_id,
        collection_id="coll-1",
        task_id=task_id,
        model=model,
        record_schema_version=SCHEMA_VERSION,
        graded_at="2026-08-18T00:00:00Z",
        grader_version=grader_version,
        checks=(
            CheckResult(name="patch_applies", status="pass"),
            CheckResult(
                name="f2p", status="pass" if resolved is True else "fail"
            ),
        ),
        resolved=resolved,
        grade_failure=None if resolved is not False else "f2p_failed",
        not_graded_reason=None if resolved is not None else "excluded",
        not_graded_detail=None if resolved is not None else "credential failure",
        **kw,
    )


def _collection(tmp_path, runs, grades) -> Path:
    """A real event log with a real grade file beside it."""
    root = tmp_path / "eventlog"
    log = EventLog(root)
    for record in runs:
        log.write_run(record)
    for grade in grades:
        append_grade(grades_path(root), grade)
    return root


def _two_arms(tmp_path, resolved_a=True, resolved_b=True) -> Path:
    """The smallest cell that produces a pair: one sample, two models."""
    return _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
        ],
        [
            _grade("run-a", model="model-one", resolved=resolved_a),
            _grade("run-b", model="model-two", resolved=resolved_b),
        ],
    )


RUBRIC_REPLY = json.dumps(
    {
        "dimension_scores": {name: 2 for name in RUBRIC_DIMENSIONS},
        "flags": {name: False for name in RUBRIC_FLAGS},
        "reasoning": "reaches the same result by a different route",
    }
)


def _pairwise_reply(verdict="A"):
    return json.dumps(
        {"verdict": verdict, "reasoning": "one handles the empty case"}
    )


class FakeComplete:
    """A scripted `CompleteFn`: counts calls and keeps every prompt it saw.

    Answers by prompt SHAPE rather than by call index -- the pairwise prompt is
    the one carrying "## Submission A" -- so a test that changes how many rubric
    calls a batch makes does not have to re-script the queue.

    `replies` overrides that with a fixed queue, which is how the malformed
    verdict tests drive the retry path. `on_call` fires before the reply is
    produced and is what the gate-invariant test uses to move the world under
    the driver mid-unit.
    """

    def __init__(self, replies=None, pairwise_verdict="A", on_call=None):
        self.prompts: list[str] = []
        self.replies = None if replies is None else list(replies)
        self.pairwise_verdict = pairwise_verdict
        self.on_call = on_call

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.on_call is not None:
            self.on_call(prompt)
        if self.replies is not None:
            return self.replies.pop(0) if self.replies else "no verdict here"
        if "## Submission A" in prompt:
            return _pairwise_reply(self.pairwise_verdict)
        return RUBRIC_REPLY


def _run(root, tasks=None, *, complete=None, **kw):
    complete = FakeComplete() if complete is None else complete
    return judge_event_log(
        root, tasks if tasks is not None else [_task()],
        complete=complete, **kw
    )


def _lines(root):
    records, malformed = load_judgments(judgments_path(root))
    assert malformed == 0
    return records


def _payload_of(root, line):
    """The payload one line names, resolved the way every reader must.

    A stored path is RELATIVE to the judgments directory, so reaching for
    `Path(line.input_payload_path)` on its own is the bug relativization was
    for, arriving in the test file: it works from the collection's parent
    directory and nowhere else.
    """
    return resolve_payload_path(
        judgments_path(root).parent, line.input_payload_path
    )


def _bytes_under(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --------------------------------------------------------------------------
# 1. the gate is never rescued
# --------------------------------------------------------------------------


def test_a_pair_with_one_gate_failure_is_recorded_gate_decided_with_no_model_call(
    tmp_path,
):
    """The objective result already settles the pair, so asking a judge would
    let it disagree with a fact -- and a line claiming a call that never
    happened is the other half of the same error."""
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=False)

    assert fake.calls == 0
    (line,) = _lines(root)
    assert line.kind == "pairwise"
    assert line.verdict == "gate_decided"
    assert line.gate_decided_by == "a"
    assert line.vote_index is None
    assert line.position_assignment is None
    assert line.input_payload_path is None
    assert line.input_payload_sha is None
    assert line.full_reasoning_text == ""
    assert result["gate_decided"] == [line]
    assert result["judged"] == []
    assert result["errors"] == []


def test_the_gate_decided_side_names_the_run_that_passed_not_the_first_one(
    tmp_path,
):
    """`gate_decided_by` is canonical a/b, so the losing side sorting first is
    the case that catches a driver hard-coding "a"."""
    root = _two_arms(tmp_path, resolved_a=False, resolved_b=True)

    _run(root, rubric=False)

    (line,) = _lines(root)
    assert (line.run_id_a, line.run_id_b) == ("run-a", "run-b")
    assert line.gate_decided_by == "b"


def test_a_pair_where_neither_side_passed_writes_nothing(tmp_path):
    root = _two_arms(tmp_path, resolved_a=False, resolved_b=False)
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=False)

    assert fake.calls == 0
    assert _lines(root) == []
    assert result["judged"] == result["gate_decided"] == []
    assert result["errors"] == []


def test_a_rubric_line_is_never_written_for_a_failed_or_excluded_run(tmp_path):
    """§4.2.3's central rule. A rubric score beside a failed gate is exactly the
    number somebody averages into a resolve rate."""
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
        ],
        [
            _grade("run-a", model="model-one", resolved=True),
            _grade("run-b", model="model-two", resolved=False),
            _grade("run-c", model="model-three", resolved=None),
        ],
    )
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=True)

    rubric_lines = [j for j in _lines(root) if j.kind == "rubric"]
    assert [j.run_id for j in rubric_lines] == ["run-a"]
    # Refused at SELECTION, not just at the append. A driver that asked and then
    # refused to record would spend a paid call per failed run and turn a
    # healthy batch into exit 1 -- the assert is the backstop, not the gate.
    assert fake.calls == 1
    assert result["errors"] == []


def test_the_gate_assert_refuses_a_rubric_line_for_a_run_that_did_not_resolve():
    """The assert itself, driven directly: it is the last thing standing between
    a driver bug and a permanent line in an append-only file."""
    _assert_rubric_gate(_grade("run-a", resolved=True), "run-a")

    for resolved in (False, None):
        with pytest.raises(GateInvariantError) as exc:
            _assert_rubric_gate(_grade("run-a", resolved=resolved), "run-a")
        assert "run-a" in str(exc.value)


class _FlipsAfterTheCall:
    """A `GradeRecord` stand-in whose `resolved` turns `False` once the model
    has been asked. Everything else delegates."""

    def __init__(self, grade):
        self._grade = grade
        self._flipped = False

    def flip(self) -> None:
        self._flipped = True

    def __getattr__(self, name):
        return getattr(self._grade, name)

    @property
    def resolved(self):
        return False if self._flipped else True


def test_a_gate_that_flips_mid_unit_is_an_error_and_no_rubric_line(
    tmp_path, monkeypatch
):
    """The driver bug `GateInvariantError` exists to catch, injected: a grade
    that reads `resolved is True` when the run is selected and `False` by the
    time the line is assembled. A correct driver cannot produce it, so the only
    way to prove the assert sits between the model call and the append is to
    move the world in between.

    `gating_view` is the driver's own read of the grade file, so replacing it is
    how the flipping stand-in reaches the rubric path.
    """
    root = _collection(
        tmp_path, [_record("run-a")], [_grade("run-a", resolved=True)]
    )
    flipping = _FlipsAfterTheCall(_grade("run-a", resolved=True))
    fake = FakeComplete(on_call=lambda prompt: flipping.flip())
    monkeypatch.setattr(
        "scripts.judge.gating_view", lambda grades: {"run-a": flipping}
    )

    result = judge_event_log(
        root, [_task()], complete=fake, rubric=True
    )

    assert _lines(root) == []
    assert len(result["errors"]) == 1
    assert GateInvariantError.__name__ in result["errors"][0]


# --------------------------------------------------------------------------
# 2. what never enters the batch
# --------------------------------------------------------------------------


def test_an_excluded_run_is_dropped_before_pairing(tmp_path):
    """`resolved is None` means the row is not an observation of the model.
    Ranking it ranks the infrastructure -- five such rows exist in
    `trucking-pilot-v2`."""
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
        ],
        [
            _grade("run-a", model="model-one", resolved=True),
            _grade("run-b", model="model-two", resolved=True),
            _grade("run-c", model="model-three", resolved=None),
        ],
    )

    _run(root, rubric=True)

    named = set()
    for line in _lines(root):
        named.update(
            v for v in (line.run_id, line.run_id_a, line.run_id_b) if v
        )
    assert "run-c" not in named
    assert named == {"run-a", "run-b"}


def test_a_run_without_a_grade_line_is_warned_and_never_judged(tmp_path):
    """Rescuing an ungraded run is out of scope: with no gate to defer to, the
    judge has nothing to be blind about."""
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
        ],
        [
            _grade("run-a", model="model-one", resolved=True),
            _grade("run-b", model="model-two", resolved=True),
        ],
    )

    result = _run(root, rubric=True)

    assert any("run-c" in w for w in result["warnings"])
    for line in _lines(root):
        assert "run-c" not in (line.run_id, line.run_id_a, line.run_id_b)


def test_the_ungraded_warning_names_runs_in_sorted_order(tmp_path, monkeypatch):
    """`list_runs` globs and glob order is nondeterministic, so the driver
    sorts. Unsorted, the warning text moves between passes over an unchanged
    collection -- a diff nobody can read and a resume nobody can compare.

    The disorder is FORCED rather than hoped for: this filesystem happens to
    return these three names already sorted, so a test that merely wrote them
    in reverse would pass against a driver that never sorted anything.
    """
    root = _collection(
        tmp_path,
        [
            _record("run-z", model="model-three", final_diff=DIFF_C),
            _record("run-y", model="model-two", final_diff=DIFF_B),
            _record("run-x", model="model-one", final_diff=DIFF_A),
        ],
        [],
    )
    monkeypatch.setattr(
        EventLog, "list_runs", lambda self: ["run-z", "run-x", "run-y"]
    )

    result = _run(root, rubric=True)

    (warning,) = [w for w in result["warnings"] if "no grade line" in w]
    assert "run-x, run-y, run-z" in warning


def test_a_cell_keeps_the_max_attempt_number_run_per_model(tmp_path):
    """A retried cell holds two runs for one model. Judging both would enter the
    same model twice into one comparison."""
    root = _collection(
        tmp_path,
        [
            _record("run-a1", model="model-one", attempt_number=1,
                    final_diff=DIFF_A),
            _record("run-a2", model="model-one", attempt_number=2,
                    final_diff=DIFF_C),
            _record("run-b", model="model-two", final_diff=DIFF_B),
        ],
        [
            _grade("run-a1", model="model-one"),
            _grade("run-a2", model="model-one"),
            _grade("run-b", model="model-two"),
        ],
    )

    _run(root, rubric=False)

    # One comparison, which is two vote lines over the same two runs.
    assert {(j.run_id_a, j.run_id_b) for j in _lines(root)} == {
        ("run-a2", "run-b")
    }


def test_samples_are_subsampled_and_pairs_never_are(tmp_path):
    """If cost binds, drop sample indices. Dropping pairs breaks the Elo
    graph's connectivity; subsampling only widens intervals (§4.2.3)."""
    root = _collection(
        tmp_path,
        [
            _record("run-a0", model="model-one", sample_index=0),
            _record("run-b0", model="model-two", sample_index=0,
                    final_diff=DIFF_B),
            _record("run-a1", model="model-one", sample_index=1,
                    final_diff=DIFF_C),
            _record("run-b1", model="model-two", sample_index=1,
                    final_diff=DIFF_B),
        ],
        [
            _grade("run-a0", model="model-one"),
            _grade("run-b0", model="model-two"),
            _grade("run-a1", model="model-one"),
            _grade("run-b1", model="model-two"),
        ],
    )

    _run(root, rubric=False, sample_indices=[1])

    assert {
        (j.sample_index, j.run_id_a, j.run_id_b) for j in _lines(root)
    } == {(1, "run-a1", "run-b1")}


def test_only_tasks_selects_the_named_tasks(tmp_path):
    root = _collection(
        tmp_path,
        [
            _record("run-a", task_id="calc-1", model="model-one"),
            _record("run-b", task_id="calc-1", model="model-two",
                    final_diff=DIFF_B),
            _record("run-c", task_id="calc-2", model="model-one"),
            _record("run-d", task_id="calc-2", model="model-two",
                    final_diff=DIFF_B),
        ],
        [
            _grade("run-a", task_id="calc-1", model="model-one"),
            _grade("run-b", task_id="calc-1", model="model-two"),
            _grade("run-c", task_id="calc-2", model="model-one"),
            _grade("run-d", task_id="calc-2", model="model-two"),
        ],
    )

    _run(root, [_task("calc-1"), _task("calc-2")], rubric=False,
         only_tasks=["calc-2"])

    assert {j.task_id for j in _lines(root)} == {"calc-2"}


# --------------------------------------------------------------------------
# 3. the vote protocol as the driver drives it
# --------------------------------------------------------------------------


def test_a_comparison_is_exactly_two_votes_one_per_forced_position(tmp_path):
    """Two calls, two prompts, two lines -- and no third.

    At temperature 0 the judge is fully described by its answer under each of
    the two orders, so a third call re-asks a question already answered: the
    old majority-of-three over RANDOM positions was measured distributionally
    identical to a single vote, at three times the price. Two forced positions
    extract the whole of the signal, and they do it reproducibly -- the same
    collection judged twice buys the same two prompts.

    A single call asked to produce both opinions is still one vote wearing two
    hats, so the two are separate calls with no shared context.
    """
    root = _two_arms(tmp_path)
    fake = FakeComplete()

    _run(root, complete=fake, rubric=False)

    assert fake.calls == 2
    lines = _lines(root)
    assert sorted(j.vote_index for j in lines) == [0, 1]
    assert len({j.judgment_id for j in lines}) == 2
    assert len({j.input_payload_path for j in lines}) == 2
    # The two prompts differ, because position changes the text. One prompt
    # sent twice would be the shape of a driver that forced no position at all.
    assert len(set(fake.prompts)) == 2
    by_index = {j.vote_index: j for j in lines}
    for index, prompt in enumerate(fake.prompts):
        assert by_index[index].judge_prompt_sha == prompt_sha(prompt)


def test_vote_index_zero_is_shown_a_first_and_vote_index_one_is_shown_b_first(
    tmp_path,
):
    """The index and the position move together, and the position is STORED.

    Derivable from the index today, and stored anyway: the moment a line is
    read back to compute position consistency, a derived position is a claim
    about what the driver does now rather than a record of what this call was
    shown. A stored assignment that disagreed with the payload would make that
    figure describe pairs of votes nobody sent in those orders.
    """
    root = _two_arms(tmp_path)
    diffs = {"run-a": DIFF_A, "run-b": DIFF_B}

    _run(root, rubric=False)

    lines = _lines(root)
    by_index = {j.vote_index: j for j in lines}
    assert by_index[0].position_assignment == "a_first"
    assert by_index[1].position_assignment == "b_first"

    for line in lines:
        payload = read_payload(_payload_of(root, line))
        shown_first = payload["submission_first"]["diff"]
        expected = (
            diffs[line.run_id_a]
            if line.position_assignment == "a_first"
            else diffs[line.run_id_b]
        )
        assert shown_first == expected


def test_the_stored_verdict_is_canonical_not_presentation_order(tmp_path):
    """The model always answers "A" here, so a driver that skipped the
    inversion would record every verdict as "a".

    Under forced positions that bug is no longer half-invisible: both orders
    occur on every comparison, so a missing inversion flips exactly one of the
    two votes and every comparison in the file splits 1-1.
    """
    root = _two_arms(tmp_path)

    _run(root, rubric=False,
         complete=FakeComplete(pairwise_verdict="A"))

    verdicts = {
        j.position_assignment: j.verdict for j in _lines(root)
    }
    assert verdicts == {"a_first": "a", "b_first": "b"}


def test_a_malformed_verdict_after_retries_is_an_error_not_a_line_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    """Exhausted retries leave a hole in the derived view, and the exit code is
    the only thing that says so."""
    root = _two_arms(tmp_path)
    fake = FakeComplete(replies=[])

    result = _run(root, complete=fake, rubric=False)

    # One error per unit, and the comparison is two units now.
    assert _lines(root) == []
    assert len(result["errors"]) == 2
    assert all("MalformedVerdict" in error for error in result["errors"])

    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: FakeComplete(replies=[]),
    )
    assert main(["--event-log", str(root), "--no-rubric"]) == 1
    assert "produced NO line" in capsys.readouterr().out


def test_every_vote_line_names_both_grade_generations_it_was_gated_on(tmp_path):
    """A re-grade under a new `GRADER_VERSION` can flip `resolved`, which
    changes which pairs are gate-decided -- so a verdict is only interpretable
    against the grade generation it saw. Two entries on a pairwise, because the
    spec's scalar cannot describe a mixed-version pair."""
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
        ],
        [
            _grade("run-a", model="model-one", grader_version="2"),
            _grade("run-b", model="model-two", grader_version="3"),
        ],
    )

    _run(root, rubric=True)

    lines = _lines(root)
    pairwise = [j for j in lines if j.kind == "pairwise"]
    assert len(pairwise) == 2
    for vote in pairwise:
        assert vote.grade_version_seen == {"run-a": "2", "run-b": "3"}
    for line in lines:
        assert line.graded_against_task_set_commit == "taskset-def"
        assert line.judge_model_id == JUDGE_MODEL_ID_DEFAULT
        assert line.judge_prompt_version == JUDGE_PROMPT_VERSION
        assert line.rubric_version == RUBRIC_VERSION


def test_a_rubric_line_is_one_call_at_vote_index_zero(tmp_path):
    """The forced positions are pairwise-only. The rubric is diagnostic and one
    call, as the spec's cost math assumes."""
    root = _collection(
        tmp_path, [_record("run-a")], [_grade("run-a")]
    )
    fake = FakeComplete()

    _run(root, complete=fake, rubric=True)

    (line,) = _lines(root)
    assert fake.calls == 1
    assert line.kind == "rubric"
    assert line.vote_index == 0
    assert line.run_id == "run-a"
    assert line.run_id_a is None and line.run_id_b is None
    assert set(line.dimension_scores) == set(RUBRIC_DIMENSIONS)
    assert set(line.flags) == set(RUBRIC_FLAGS)
    assert line.judge_sampling == dict(JUDGE_SAMPLING)
    assert line.full_reasoning_text


SECRET_DIFF = """diff --git a/src/calc.py b/src/calc.py
index 1111111..6666666 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    AWS_KEY = "AKIA1234567890ABCDEF"
-    return a - b
+    return a + b
"""


def test_a_rubric_payload_carrying_a_secret_costs_nothing_but_its_own_unit(
    tmp_path,
):
    """No call, no file, no line -- and the batch goes on.

    The scan is in front of the wire now, so a secret-bearing submission is
    refused before the prompt is rendered and the unit costs nothing at all.
    What survives from the write-time-only version of this guarantee is the
    other half, and it is the half §4.3 rests on: no judgment line is appended
    for a payload that never landed, because a line naming an input nobody can
    read breaks "a re-judge is a re-score, not a re-run" exactly as a missing
    payload does while looking like a present one.

    Driven with a real secret so the refusal comes from `PayloadSecretsFound`
    over the real scanner rather than from a stub.
    """
    root = _collection(
        tmp_path,
        [_record("run-a", final_diff=SECRET_DIFF)],
        [_grade("run-a")],
    )
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=True)

    assert fake.calls == 0
    assert _lines(root) == []
    assert list(payloads_root(root).glob("*")) == []
    assert len(result["errors"]) == 1
    assert "PayloadSecretsFound" in result["errors"][0]


def test_a_vote_payload_carrying_a_secret_costs_nothing_but_its_own_units(
    tmp_path,
):
    """The same refusal on the path that carries most of the units.

    The rubric test above builds a single-run collection, so no pair forms and
    only `_rubric_line` runs -- which leaves the vote path, the one
    `--no-rubric` makes the ONLY path, unpinned. `judge_pair_vote` builds its
    payload in the position's own order and scans that, so a guard reached
    through one slot only would refuse one of the two forced positions and look
    like a working one on the other.
    """
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=SECRET_DIFF),
        ],
        [
            _grade("run-a", model="model-one"),
            _grade("run-b", model="model-two"),
        ],
    )
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=False)

    assert fake.calls == 0
    assert _lines(root) == []
    assert list(payloads_root(root).glob("*")) == []
    # Both forced positions carry the same secret, so both units fail.
    assert len(result["errors"]) == 2
    assert all("PayloadSecretsFound" in error for error in result["errors"])


def test_a_payload_the_disk_refuses_still_leaves_no_line_behind(
    tmp_path, monkeypatch
):
    """`write_payload` BEFORE `append_judgment`, pinned by a failing disk.

    The two secret tests above no longer reach `write_payload` at all -- the
    build-time scan refuses the payload before the store is asked -- so the
    ordering they used to pin needs its own driver. A write that fails for a
    reason no scan can anticipate is that driver, and it is also the case the
    ordering was written for: the reverse order leaves a line naming a payload
    that is not on disk, which reads as a complete verdict forever.
    """
    root = _collection(tmp_path, [_record("run-a")], [_grade("run-a")])

    def _no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("scripts.judge.write_payload", _no_space)
    result = _run(root, rubric=True)

    assert _lines(root) == []
    assert len(result["errors"]) == 1
    assert "OSError" in result["errors"][0]


def test_payload_sits_beside_the_jsonl_and_its_sha_matches_the_record(tmp_path):
    """A verdict whose input cannot be reconstructed forces a re-collection to
    re-score (§4.3), and a sha that does not match its own input is
    indistinguishable from a tampered one."""
    root = _two_arms(tmp_path)

    _run(root, rubric=True)

    lines = _lines(root)
    assert lines
    for line in lines:
        path = _payload_of(root, line)
        assert path.parent == payloads_root(root)
        assert path.exists()
        canonical = json.dumps(read_payload(path), sort_keys=True).encode()
        assert hashlib.sha256(canonical).hexdigest() == line.input_payload_sha


def test_stored_payload_paths_are_relative_to_the_judgments_directory(tmp_path):
    """What a line stores is `payloads/<judgment_id>.json.gz`, and nothing more.

    An absolute path records where the collection sat on the machine that
    judged it, which is a fact about that machine rather than about the
    verdict. §4.3 says a re-judge is a re-score BECAUSE the inputs were logged,
    so a path that resolves on one host and nowhere else is the same loss as no
    payload at all -- arriving silently, and months later.

    The relative value is asserted to be exactly what `payload_relative_path`
    derives from the judgment id, not merely "not absolute": a driver that
    stored some other relative string would satisfy the weaker assertion and
    still send every reader to the wrong file.
    """
    root = _two_arms(tmp_path)

    _run(root, rubric=True)

    lines = _lines(root)
    assert lines
    for line in lines:
        stored = line.input_payload_path
        assert stored == payload_relative_path(line.judgment_id)
        assert not Path(stored).is_absolute()
        assert str(root) not in stored
        assert _payload_of(root, line).exists()


def test_a_payloads_directory_outside_the_judgments_directory_is_refused(
    tmp_path, monkeypatch
):
    """The per-line check is against the RESOLVED path, not against the tail.

    A check comparing only the last two components passes for a `payloads/`
    relocated anywhere at all -- out of `judgments/`, or into another
    collection -- while the stored `payloads/<id>.json.gz` goes on being joined
    against the judgments directory, where nothing is. That is precisely the
    file of dead paths `payloads_root`'s docstring claims this prevents, so the
    check asks the question a reader will ask: does
    `resolve_payload_path(judgments_dir, stored)` name the file that was
    actually written?

    Driven by moving the directory out from under the driver, because a correct
    driver cannot produce the mismatch.
    """
    root = _collection(tmp_path, [_record("run-a")], [_grade("run-a")])
    monkeypatch.setattr(
        "scripts.judge.payloads_root", lambda log: Path(log) / "payloads"
    )

    result = _run(root, rubric=True)

    assert _lines(root) == []
    assert len(result["errors"]) == 1
    assert "AssertionError" in result["errors"][0]


def test_a_moved_collection_still_resolves_every_payload(tmp_path):
    """`mv` on a collection, and every verdict still names a readable input.

    This is the whole of what relativization buys, and it is not a hypothetical
    -- collections are copied off the machine that judged them, mounted at
    another root inside a container, and moved out of `~/.cache` when they stop
    being the current one. Under absolute paths every line in the file breaks
    at once, and nothing about the file looks any different afterwards.

    The move is asserted to be a real move first, so the resolution below is
    over a collection whose old location genuinely no longer exists.
    """
    root = _two_arms(tmp_path)
    _run(root, rubric=True)
    before = {line.judgment_id: read_payload(_payload_of(root, line))
              for line in _lines(root)}
    assert before

    moved = tmp_path / "elsewhere" / "eventlog"
    moved.parent.mkdir()
    shutil.move(str(root), str(moved))
    assert not root.exists()

    after = {line.judgment_id: read_payload(_payload_of(moved, line))
             for line in _lines(moved)}
    assert after == before


# --------------------------------------------------------------------------
# 4. resume
# --------------------------------------------------------------------------


def test_resume_skips_comparisons_already_judged_under_the_same_versions(
    tmp_path,
):
    root = _two_arms(tmp_path)
    first = _run(root, rubric=True)
    written = len(_lines(root))
    fake = FakeComplete()

    second = _run(root, complete=fake, rubric=True)

    assert fake.calls == 0
    assert second["judged"] == [] and second["gate_decided"] == []
    assert len(second["skipped"]) == len(first["judged"])
    assert len(_lines(root)) == written


def test_a_gate_decided_pair_resumes_as_its_own_unit(tmp_path):
    """`vote_index=None` is the key for a line no model produced. A resume that
    keyed it on 0 would re-write it beside itself on every pass."""
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)
    _run(root, rubric=False)

    second = _run(root, rubric=False)

    assert len(_lines(root)) == 1
    assert len(second["skipped"]) == 1
    assert second["gate_decided"] == []


def test_a_changed_judge_model_id_re_judges_everything(tmp_path):
    """The verdict is only reproducible against the model that gave it, so a
    different pin is a different generation of verdict rather than a resume."""
    root = _two_arms(tmp_path)
    _run(root, rubric=False)

    second = _run(root, rubric=False,
                  judge_model_id="openai.gpt-5.6-luna")

    lines = _lines(root)
    assert len(lines) == 4
    assert {j.judge_model_id for j in lines} == {
        JUDGE_MODEL_ID_DEFAULT, "openai.gpt-5.6-luna"
    }
    assert second["skipped"] == []


def test_a_v1_line_with_the_same_vote_index_does_not_satisfy_the_v2_resume_key(
    tmp_path,
):
    """Why `JUDGE_PROMPT_VERSION` HAD to move from 1 to 2.

    The old protocol wrote `vote_index` 0, 1 and 2 under positions drawn at
    random; the new one writes 0 and 1 under forced positions. The resume key
    holds the index and the generation and NOT the position, so under an
    unbumped version a v1 line at index 0 -- a random-position vote that may
    well have been `b_first` -- would answer the v2 unit for index 0 and the
    driver would skip it.

    The batch would then report a clean resume over a comparison holding one
    random-position vote where the protocol says two forced ones, and every
    position-consistency figure taken off that file would be computed over
    pairs that were never both bought. The bump is what makes the two
    protocols two generations, which is the one thing every aggregation in
    this file already knows how to keep apart.
    """
    root = _two_arms(tmp_path)
    append_judgment(judgments_path(root), JudgeRecord(
        judgment_id=uuid.uuid4().hex,
        judged_at="2026-08-18T00:00:00Z",
        judge_model_id=JUDGE_MODEL_ID_DEFAULT,
        # The old generation, and the only field that differs from a unit this
        # batch is about to buy.
        judge_prompt_version=1,
        judge_prompt_sha="0" * 64,
        judge_sampling=dict(JUDGE_SAMPLING),
        rubric_version=RUBRIC_VERSION,
        kind="pairwise",
        task_id="calc-1",
        run_id_a="run-a",
        run_id_b="run-b",
        sample_index=0,
        position_assignment="b_first",
        verdict="a",
        vote_index=0,
        full_reasoning_text="judged under the three-vote protocol",
        input_payload_path=str(root / "judgments" / "payloads" / "old.json.gz"),
        input_payload_sha="1" * 64,
        grade_version_seen={"run-a": GRADER, "run-b": GRADER},
        graded_against_task_set_commit="taskset-def",
    ))

    result = _run(root, rubric=False)

    assert result["skipped"] == []
    fresh = [j for j in _lines(root) if j.judge_prompt_version == 2]
    assert sorted(j.vote_index for j in fresh) == [0, 1]
    assert [j.position_assignment for j in sorted(
        fresh, key=lambda j: j.vote_index
    )] == ["a_first", "b_first"]
    # The old line is still on disk -- judgments are append-only, and the two
    # protocols are two readings of this collection rather than one.
    assert len(_lines(root)) == 3


def test_re_judge_appends_new_lines_rather_than_replacing(tmp_path):
    """Judgments are append-only. The disagreement between two lines is the
    finding; an update API would delete the only evidence that the judge is not
    deterministic."""
    root = _two_arms(tmp_path)
    _run(root, rubric=True)
    first = _lines(root)

    _run(root, rubric=True, re_judge=True)

    second = _lines(root)
    assert len(second) == 2 * len(first)
    assert [j.judgment_id for j in second[: len(first)]] == [
        j.judgment_id for j in first
    ]


def test_malformed_judgments_refuse_resume_without_re_judge(tmp_path):
    """An unreadable judgment is not an absent judgment, and a resume that
    confused the two pays a second time for a verdict already bought."""
    root = _two_arms(tmp_path)
    path = judgments_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json\n")

    with pytest.raises(ResumeRefused) as exc:
        _run(root, rubric=False)
    assert str(path) in str(exc.value)

    result = _run(root, rubric=False, re_judge=True)
    assert result["errors"] == []
    assert any("unreadable" in w for w in result["warnings"])


def test_malformed_grades_refuse_judging_outright_even_with_re_judge(tmp_path):
    """No flag cures it -- an unreadable gate might BE the gate, and a run read
    as ungraded is a run the judge would either skip or, worse, pair."""
    root = _two_arms(tmp_path)
    with open(grades_path(root), "a", encoding="utf-8") as handle:
        handle.write("{ truncated\n")

    for re_judge in (False, True):
        with pytest.raises(ResumeRefused) as exc:
            _run(root, rubric=False, re_judge=re_judge)
        assert "grades" in str(exc.value)


def test_the_gating_view_is_the_last_line_per_run_in_file_order(tmp_path):
    """A re-grade appends rather than replaces, so a run can hold several lines.
    The last one is the current verdict."""
    grades = [
        _grade("run-a", resolved=True, grader_version="1"),
        _grade("run-a", resolved=False, grader_version="2"),
    ]

    view = gating_view(grades)

    assert set(view) == {"run-a"}
    assert view["run-a"].resolved is False
    assert view["run-a"].grader_version == "2"


def test_the_audit_covers_the_campaign_not_the_invocation(tmp_path):
    """A resume's last slice judges two units and would otherwise report a
    campaign as two units wide."""
    root = _two_arms(tmp_path)
    _run(root, rubric=False)

    second = _run(root, rubric=True)

    assert second["audited"] == 4
    assert second["from_prior_invocations"] == 2


# --------------------------------------------------------------------------
# 5. the inputs are inputs
# --------------------------------------------------------------------------


def test_the_event_log_and_grades_file_bytes_are_unchanged_after_a_batch(
    tmp_path,
):
    """Two derived views, two files, no cross-writes. The event log is the
    immutable input and `grades.jsonl` belongs to the grader."""
    root = _two_arms(tmp_path)
    before = {
        **_bytes_under(root / "runs"),
        "index.jsonl": (root / "index.jsonl").read_bytes(),
        "grades.jsonl": grades_path(root).read_bytes(),
    }

    _run(root, rubric=True)

    after = {
        **_bytes_under(root / "runs"),
        "index.jsonl": (root / "index.jsonl").read_bytes(),
        "grades.jsonl": grades_path(root).read_bytes(),
    }
    assert before == after
    assert judgments_path(root).exists()


def test_a_fully_gate_decided_batch_never_builds_the_live_judge(
    tmp_path, monkeypatch
):
    """A batch whose comparisons the ladder already settled must not mint a
    credential for a judge it never asks -- and minting at construction starts a
    ~1h clock before the first call."""
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)

    def explode(*args, **kwargs):
        raise AssertionError("the live judge was built for a batch with no call")

    monkeypatch.setattr("scripts.judge.live_completion", explode)

    result = judge_event_log(
        root, [_task()], rubric=False
    )

    assert len(result["gate_decided"]) == 1
    assert result["errors"] == []


def test_the_live_judge_is_built_once_on_the_first_call_that_needs_it(
    tmp_path, monkeypatch
):
    """The `task_resolver` one-slot-cache pattern: lazy, and lazy exactly
    once."""
    root = _two_arms(tmp_path)
    built = []

    def factory(*args, **kwargs):
        built.append(args)
        return FakeComplete()

    monkeypatch.setattr("scripts.judge.live_completion", factory)

    judge_event_log(root, [_task()], rubric=True)

    assert len(built) == 1


def test_the_batch_walks_cells_and_pairs_in_sorted_order(tmp_path):
    """`list_runs` globs and glob order is nondeterministic. Sorted is arbitrary
    but deterministic, which is what a resumable batch needs."""
    root = _collection(
        tmp_path,
        [
            _record("run-c", model="model-three", sample_index=1,
                    final_diff=DIFF_C),
            _record("run-a", model="model-one", sample_index=0,
                    final_diff=DIFF_A),
            _record("run-b", model="model-two", sample_index=0,
                    final_diff=DIFF_B),
            _record("run-d", model="model-one", sample_index=1,
                    final_diff=DIFF_A),
        ],
        [
            _grade("run-c", model="model-three"),
            _grade("run-a", model="model-one"),
            _grade("run-b", model="model-two"),
            _grade("run-d", model="model-one"),
        ],
    )

    result = _run(root, rubric=False)

    # Two vote lines per pair, and the two positions stay adjacent: the walk is
    # cell, then pair, then position, so a resume that stops mid-comparison
    # leaves the missing position as the next unit rather than a hole a cell
    # away.
    assert [
        (j.task_id, j.sample_index, j.run_id_a, j.run_id_b, j.vote_index)
        for j in result["judged"]
    ] == [
        ("calc-1", 0, "run-a", "run-b", 0),
        ("calc-1", 0, "run-a", "run-b", 1),
        ("calc-1", 1, "run-c", "run-d", 0),
        ("calc-1", 1, "run-c", "run-d", 1),
    ]


# --------------------------------------------------------------------------
# 6. the CLI
# --------------------------------------------------------------------------


def test_main_exits_zero_when_every_selected_unit_produced_its_lines(
    tmp_path, monkeypatch, capsys
):
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)
    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )

    assert main(["--event-log", str(root), "--no-rubric"]) == 0
    assert "gate-decided" in capsys.readouterr().out


def test_main_exits_one_when_the_resume_is_refused(
    tmp_path, monkeypatch, capsys
):
    root = _two_arms(tmp_path)
    with open(grades_path(root), "a", encoding="utf-8") as handle:
        handle.write("{ truncated\n")
    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])

    assert main(["--event-log", str(root)]) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_main_passes_the_judge_model_through(tmp_path, monkeypatch):
    root = _two_arms(tmp_path)
    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion", lambda *a, **k: FakeComplete()
    )

    assert main([
        "--event-log", str(root), "--no-rubric",
        "--judge-model", "openai.gpt-5.6-terra",
    ]) == 0

    lines = _lines(root)
    assert len(lines) == 2
    assert {j.judge_model_id for j in lines} == {"openai.gpt-5.6-terra"}


def test_there_is_no_vote_count_flag_to_set(tmp_path, monkeypatch, capsys):
    """The vote count is not a knob any more, so the flag that offered it is
    gone rather than clamped.

    Under forced positions there is exactly one legal value -- one vote per
    position -- and a `--votes 3` that silently meant 2, or worse honoured the
    3, would write a third line whose position no protocol assigns. A flag
    argparse refuses is the only version of this that cannot be misread.
    """
    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])

    with pytest.raises(SystemExit):
        main(["--event-log", str(_two_arms(tmp_path)), "--votes", "3"])
    assert "--votes" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "--votes" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# 6b. the consecutive-failure breaker
# --------------------------------------------------------------------------
#
# The failure mode these are placed against: the mantle token's real window is
# about an hour (Identity Center caps the session well below the token's own
# TTL) and a 60-task pass is ~9,600 calls -- 7,200 pairwise at two forced
# positions per comparison, plus 2,400 rubric -- over many more hours. When
# the credential dies mid-batch and `live_completion`'s own refresh cannot
# recover it, the per-unit `except` records an error and continues -- so the
# driver grinds through every remaining unit on a dead credential and prints
# thousands of error lines. Resume recovers the WORK; what it cannot recover is
# an operator's afternoon.


class _ExpiredToken(RuntimeError):
    """What escapes `live_completion` when the credential is gone for good --
    a freshly minted token that still 401s, or no token to mint at all.

    `status_code = 403` is not decoration: it is what makes this AUTH-SHAPED to
    `is_auth_failure`, which is the one classifier the abort message asks. The
    endpoint answers a dead principal with 401/403, so a stand-in that carried
    no status would model a failure the driver has no way to recognise -- and
    every test below that asserts the credential paragraph would be asserting
    it against the wrong evidence.
    """

    status_code = 403


class _UnreadableDiff(RuntimeError):
    """A per-unit DATA failure: nothing about the credential is wrong.

    No `status_code` and no auth class name in its MRO, so `is_auth_failure`
    says False -- which is the whole difference the abort message is built on.
    A run of these is deterministic: it fails again, identically, on every
    resume, and telling the operator to re-mint a token would send them past
    the only thing that can actually fix it.
    """


class _DiesAfter(FakeComplete):
    """A `CompleteFn` that raises on every call except the ones named.

    `succeed_at` holds 1-based CALL indices. One call is one unit here: a
    raised exception is not a `MalformedVerdict`, so `_ask_and_parse` does not
    re-ask and the driver's per-unit `except` sees it directly.

    `error` is the exception CLASS it raises, defaulting to the auth-shaped
    one, because the credential death is the failure the breaker was built for.
    """

    def __init__(self, succeed_at=(), error=_ExpiredToken):
        super().__init__()
        self.succeed_at = set(succeed_at)
        self.error = error

    def __call__(self, prompt: str) -> str:
        if len(self.prompts) + 1 in self.succeed_at:
            return super().__call__(prompt)
        self.prompts.append(prompt)
        raise self.error(
            "ExpiredTokenException: the security token included in the "
            "request is expired"
        )


def _three_arms(tmp_path, resolved_three=True) -> Path:
    """One cell, three models: 3 rubric units and 3 pairs at two votes each,
    so a batch here has nine units -- more than the breaker's limit, which is
    what lets these tests prove where it stopped."""
    return _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
        ],
        [
            _grade("run-a", model="model-one", resolved=True),
            _grade("run-b", model="model-two", resolved=True),
            _grade("run-c", model="model-three", resolved=resolved_three),
        ],
    )


def _four_arms(tmp_path, resolved_four=True) -> Path:
    """One cell, four models -- the round-robin the spec actually runs.

    Sixteen units when every arm passes (4 rubric, 6 pairs at two votes each),
    which is what the reset test needs: a run of failures, a success, and then
    a FULL fresh run of `MAX_CONSECUTIVE_ERRORS` after it, none of which fits
    inside a three-arm cell now that a comparison costs two calls instead of
    three.

    With the fourth arm gate-failed it is 3 rubric units, then three
    gate-decided pairs making no call at all, then three judgeable pairs --
    which puts the gate-decided pairs in the middle of a run of failures,
    where a breaker that counted them as successes would reset.
    """
    return _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
            _record("run-d", model="model-four", final_diff=DIFF_D),
        ],
        [
            _grade("run-a", model="model-one", resolved=True),
            _grade("run-b", model="model-two", resolved=True),
            _grade("run-c", model="model-three", resolved=True),
            _grade("run-d", model="model-four", resolved=resolved_four),
        ],
    )


def test_a_run_of_consecutive_unit_failures_aborts_the_batch(tmp_path):
    """The breaker, at exactly the limit. Nine units are available and five
    are attempted: a dead credential fails every unit it touches, so the sixth
    error carries no information the first five did not, and the other four
    would each cost a call, a line of noise, and a place in a list the operator
    has to read past to find the one message that matters."""
    root = _three_arms(tmp_path)
    fake = _DiesAfter()

    result = _run(root, complete=fake, rubric=True)

    assert fake.calls == MAX_CONSECUTIVE_ERRORS
    assert len(result["errors"]) == MAX_CONSECUTIVE_ERRORS
    # Nothing was judged, so nothing was written: the abort costs no line.
    assert _lines(root) == []
    (abort,) = [w for w in result["warnings"] if "consecutive" in w]
    assert f"{MAX_CONSECUTIVE_ERRORS} consecutive" in abort
    assert "aborting" in abort
    # The recovery instruction is the point of the message. The resume is keyed
    # on units already bought, so the same command is the whole procedure.
    assert "resume with the same command" in abort


def test_one_successful_unit_resets_the_consecutive_failure_count(tmp_path):
    """CONSECUTIVE, not cumulative. A judging pass over a flaky endpoint
    produces scattered errors all day, and a cumulative counter would abort a
    healthy batch that had simply been running for long enough. The credential
    failure this breaker is for looks nothing like that: it fails every unit
    from the moment it starts.

    Four arms, because the run after the reset has to be a FULL
    `MAX_CONSECUTIVE_ERRORS` and 4 + 1 + 5 units do not fit in a three-arm
    cell at two votes per comparison.
    """
    root = _four_arms(tmp_path)
    # Four failures, one success, then the run that trips it.
    fake = _DiesAfter(succeed_at={5})

    result = _run(root, complete=fake, rubric=True)

    assert fake.calls == 5 + MAX_CONSECUTIVE_ERRORS
    assert len(result["errors"]) == 4 + MAX_CONSECUTIVE_ERRORS
    assert len(_lines(root)) == 1, "the one unit that succeeded wrote its line"
    assert any("consecutive" in w for w in result["warnings"])


def test_a_gate_decided_pair_neither_trips_the_breaker_nor_resets_it(tmp_path):
    """A gate-decided pair makes no call, so it is not evidence that the
    credential recovered. Counting it as a success would reset the counter on
    the strength of nothing having been asked.

    The cell here interleaves them deliberately: three rubric units, then the
    three gate-decided pairs the failed arm produces, then the votes that carry
    the count to the limit. WHAT A RESET ACTUALLY COSTS HERE IS A DELAY, not an
    abort that never comes: the three judgeable pairs supply six vote units, so
    a counter restarted by the gate-decided block still reaches the limit --
    but five vote units later, aborting on call 8 instead of call 5. Three more
    paid calls on a credential already known to be dead, which is why the
    assertion is on the exact count rather than on the warning alone.

    The unbounded version of that is a collection, not this fixture: gate-
    decided pairs interleaved BETWEEN the judgeable ones rather than blocked
    ahead of them reset the counter between every failure, and a breaker whose
    run is broken that way never fires at all. Which shape a real collection
    has depends on how the ladder happened to fall, so the rule cannot depend
    on it: a unit that made no call is neither evidence.
    """
    root = _four_arms(tmp_path, resolved_four=False)
    fake = _DiesAfter()

    result = _run(root, complete=fake, rubric=True)

    assert fake.calls == MAX_CONSECUTIVE_ERRORS
    assert any("consecutive" in w for w in result["warnings"])
    # The gate-decided lines were still written, and are not errors.
    assert len(result["gate_decided"]) == 3
    assert [j.verdict for j in _lines(root)] == ["gate_decided"] * 3


def test_the_abort_is_reported_on_the_terminal_and_exits_one(
    tmp_path, monkeypatch, capsys,
):
    """Exit 1 for the reason every other error path exits 1 -- units that
    produced no line -- and the abort says so distinctly, because "5 unit(s)
    produced NO line" alone reads as a batch that finished with five holes in
    it rather than one that stopped early with seven units never attempted."""
    root = _three_arms(tmp_path)
    monkeypatch.setattr("scripts.judge.load_task_set",
                        lambda *a, **k: [_task()])
    monkeypatch.setattr("scripts.judge.live_completion",
                        lambda *a, **k: _DiesAfter())

    assert main(["--event-log", str(root)]) == 1

    out = capsys.readouterr().out
    assert f"{MAX_CONSECUTIVE_ERRORS} consecutive" in out
    assert "resume with the same command" in out
    # The κ caveat is not skipped by an abort: it is the one line no branch
    # may drop.
    assert KAPPA_CAVEAT in out


def test_a_batch_whose_failures_stay_below_the_limit_runs_to_the_end(tmp_path):
    """The breaker is for a systemic failure, not for a bad unit. One
    unreadable diff or one model that could not produce JSON must still cost
    its own line and nothing else -- that is the exit contract, and a breaker
    that fired early would turn a 99%-complete batch into a resume."""
    root = _three_arms(tmp_path)
    # Every other unit fails: the count never reaches two in a row.
    fake = _DiesAfter(succeed_at=set(range(2, 25, 2)))

    result = _run(root, complete=fake, rubric=True)

    assert fake.calls == 9, "the batch stopped short of its nine units"
    assert len(result["errors"]) == 5
    assert not any("consecutive" in w for w in result["warnings"])


def _abort_of(result) -> str:
    (abort,) = [w for w in result["warnings"] if "consecutive" in w]
    return abort


def test_the_breaker_trips_only_on_attempted_units_so_a_skip_heavy_resume_reports_the_real_run_length(
    tmp_path,
):
    """A resume attempts only what is not already bought, and the run the
    breaker counts is a run of ATTEMPTS -- skips neither lengthen it nor break
    it.

    Both halves matter and they pull opposite ways. Counting a skip as a
    failure would abort a resume that is merely walking past work it already
    has. RESETTING on one, which is the tempting fix for the deadlock this
    task is about, is worse: the resume that follows an aborted pass is
    skip-heavy by construction, so a counter a skip could reset would never
    reach the limit again and the breaker would be off for exactly the pass it
    was written for.

    The first pass here judges every other unit with the limit raised out of
    the way, so the second sees failures separated by skips -- and still stops
    at five, saying five.
    """
    root = _four_arms(tmp_path)
    first = _DiesAfter(succeed_at=set(range(2, 17, 2)))
    _run(root, complete=first, rubric=True, max_consecutive_errors=100)
    assert len(_lines(root)) == 8, "the first pass did not judge half the cell"

    second = _DiesAfter()
    result = _run(root, complete=second, rubric=True)

    # Units 1, 3, 5, 7, 9 are attempted and fail; 2, 4, 6, 8 are skipped.
    assert second.calls == MAX_CONSECUTIVE_ERRORS
    assert len(result["skipped"]) == 4, "the failures were not separated"
    assert len(result["errors"]) == MAX_CONSECUTIVE_ERRORS
    assert f"{MAX_CONSECUTIVE_ERRORS} consecutive" in _abort_of(result)


def test_max_consecutive_errors_is_operator_settable_from_the_command_line(
    tmp_path, monkeypatch, capsys,
):
    """The escape hatch, and the only one there is.

    Without it a collection holding `MAX_CONSECUTIVE_ERRORS` permanently
    failing units is a deadlock: the pass aborts, the resume attempts the same
    units in the same order and aborts identically, and no sequence of resumes
    ever reaches the units behind them. Raising the limit is what walks past
    them, and the abort message names this flag for that reason.

    Refused below 1 through `parser.error`, so an operator who meant `-1` gets
    a usage line rather than a batch that aborts on its first unit -- and a
    traceback out of argparse would be the same defect wearing a stack.
    """
    root = _four_arms(tmp_path)
    monkeypatch.setattr("scripts.judge.load_task_set",
                        lambda *a, **k: [_task()])
    fake = _DiesAfter()
    monkeypatch.setattr("scripts.judge.live_completion", lambda *a, **k: fake)

    assert main(["--event-log", str(root),
                 "--max-consecutive-errors", "20"]) == 1

    assert fake.calls == 16, "the raised limit never reached the walk"
    out = capsys.readouterr().out
    # The phrase, not the word: `tmp_path` is named after this test, so the
    # paths printed in the header carry "consecutive" all by themselves.
    assert "consecutive unit failures" not in out, "it aborted anyway"

    with pytest.raises(SystemExit):
        main(["--event-log", str(root), "--max-consecutive-errors", "0"])
    assert "--max-consecutive-errors" in capsys.readouterr().err


def test_an_all_auth_failure_run_aborts_with_a_credential_shaped_message(
    tmp_path,
):
    """Every unit in the run died on a 401/403, so the credential IS the
    diagnosis and the recovery is the same command once it is fixed.

    Classified by `is_auth_failure` and by nothing else -- never by matching
    words in the message text, which is how a data failure whose text happens
    to say "token" ends up sending an operator to `aws sso login`.
    """
    root = _three_arms(tmp_path)
    fake = _DiesAfter()

    abort = _abort_of(_run(root, complete=fake, rubric=True))

    assert "credential" in abort
    assert "aws sso" in abort
    assert "resume with the same command" in abort
    # The data diagnosis must be ABSENT, not merely present alongside: an
    # operator handed both reads the one that matches their last outage.
    assert "--only-task" not in abort


def test_a_data_shaped_failure_run_is_not_blamed_on_credentials(tmp_path):
    """The failure this whole task exists for. Five units failed for reasons
    that have nothing to do with the token, and the old message told the
    operator to re-mint one -- so the fix that was actually needed (exclude the
    task, or raise the limit) was the one thing the abort did not mention.

    "Resume with the same command" is worse than useless here: these units fail
    deterministically, so the resume re-attempts them in the same order and
    aborts in the same place, forever.
    """
    root = _three_arms(tmp_path)
    fake = _DiesAfter(error=_UnreadableDiff)

    abort = _abort_of(_run(root, complete=fake, rubric=True))

    assert "will fail again on resume" in abort
    assert "--only-task" in abort
    assert "--max-consecutive-errors" in abort
    assert "credential" not in abort
    assert "resume with the same command" not in abort


def test_the_abort_names_the_task_model_and_sample_of_every_unit_in_the_failing_run(
    tmp_path,
):
    """A run id is a hash. `--only-task` takes a task id and `--samples` takes
    an index, so an abort naming only run ids leaves the operator reversing
    hashes by hand to use either -- which is the state that made the deadlock
    undiagnosable rather than merely annoying.

    Every unit in the failing run is named, not a count of them and not the
    first: the shared field across five labels is the finding (one task, one
    sample, one model on every line), and it is only visible if all five are
    there.

    Capped at `_MAX_NAMED`, which is the one thing that stops "every" being
    literal, and only because the limit is now an operator flag: a run of 13
    under `--max-consecutive-errors 13` would otherwise print the same 13 lines
    twice, once as a warning and once as the error list.
    """
    root = _three_arms(tmp_path)
    fake = _DiesAfter(error=_UnreadableDiff)

    abort = _abort_of(_run(root, complete=fake, rubric=True))

    assert abort.count("task=calc-1") == MAX_CONSECUTIVE_ERRORS
    assert abort.count("sample=0") == MAX_CONSECUTIVE_ERRORS
    # Three rubric units, then both votes of the first pair.
    for model in ("model-one", "model-two", "model-three"):
        assert model in abort
    assert "rubric" in abort
    assert "vote 0" in abort and "vote 1" in abort

    long_run = _abort_of(_run(
        _four_arms(tmp_path / "wider"),
        complete=_DiesAfter(error=_UnreadableDiff),
        rubric=True, max_consecutive_errors=13,
    ))

    assert long_run.count("task=calc-1") == _MAX_NAMED
    assert "and 3 more" in long_run
    assert "13 consecutive" in long_run, "the count is over the whole run"


def test_one_unreadable_run_fails_its_pairs_with_one_line_each_and_never_trips_the_breaker(
    tmp_path,
):
    """One run whose stored record cannot be turned into a submission takes
    every unit it appears in with it -- a rubric call and both votes of three
    pairs, seven units, adjacent in the walk. Under the old driver that was a
    run of five inside a collection with nothing else wrong, and the batch
    aborted blaming a credential that was fine.

    So it is PRE-FILTERED: the driver finds out the run is unusable before the
    unit's `try`, writes one line per unit saying which run and why, and the
    breaker never sees any of it. Not a failure of the judge, not evidence
    about the credential, and not a reason to stop -- the other three arms are
    judgeable and the pass judges them.

    `final_diff=None` is the shape: the ladder's own `NO_FINAL_DIFF` normally
    catches it (`resolved is None`, dropped before pairing), so reaching the
    walk means a grade line from a pass that saw a different record -- which is
    exactly the collection an operator arrives with.

    The WORDING is asserted, not just the count. This run was read -- it is in
    `records`, and the `ValueError` printed beside it came out of
    `payload_inputs_from` -- so the line must not claim a read failure. "Could
    not be read from the event log" is the genuine read failure's phrasing
    verbatim; one phrase over both faults makes a grep for either return both,
    one of them under a claim that is false, and it would contradict the cause
    printed in the same sentence.
    """
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
            _record("run-d", model="model-four", final_diff=None),
        ],
        [
            _grade("run-a", model="model-one"),
            _grade("run-b", model="model-two"),
            _grade("run-c", model="model-three"),
            _grade("run-d", model="model-four"),
        ],
    )
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=True)

    # One rubric unit and three pairs at two votes each.
    assert len(result["errors"]) == 7
    assert all("yielded no judgeable payload" in line
               for line in result["errors"])
    assert not any("could not be read" in line for line in result["errors"])
    assert all("run-d" in line for line in result["errors"])
    assert all("no artifacts.final_diff" in line for line in result["errors"])
    assert not any("consecutive" in w for w in result["warnings"])
    # The nine units that never touched run-d were judged, and not one of the
    # seven paid for a call.
    assert fake.calls == 9
    assert len(_lines(root)) == 9


def test_a_run_that_cannot_be_read_is_reported_by_task_and_arm_not_by_digest(
    tmp_path,
):
    """The other fault, under its own phrasing and named the same way.

    A run id is `sha256(task|model|sample|attempt)[:16]`
    (`runner.make_run_id`), so those fields ARE its preimage and a line
    carrying only the digest tells an operator nothing they can act on -- the
    complaint this whole task was raised over, on the one path a genuine read
    failure takes. `GradeRecord` has no `sample_index`, so this cannot be a
    full `_unit_label`; the task and the arm are what the grade line holds.

    The run never reaches a cell -- grouping is over the records that READ --
    so it produces no units, costs no call, and has nothing to do with the
    breaker. That is why it needs its own line: it is the only report this run
    gets.
    """
    root = _two_arms(tmp_path)
    corrupt = root / "runs" / "run-b.json"
    corrupt.write_text("{not json at all", encoding="utf-8")
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=True)

    (line,) = result["errors"]
    assert "run run-b" in line
    assert "task=calc-1" in line, "the id's preimage, not just the digest"
    assert "model-two" in line
    assert "could not be read from the event log" in line
    # Its own phrasing, distinct from the pre-filter's: these are two faults
    # with two fixes, and a grep for either must not return both.
    assert "yielded no judgeable payload" not in line
    # One arm left in the cell, so one rubric unit and no pair at all.
    assert fake.calls == 1
    assert len(_lines(root)) == 1


# --------------------------------------------------------------------------
# 7. the summary -- a printout, not a stored score
# --------------------------------------------------------------------------
#
# These drive `summarize`/`elo_from_outcomes` over hand-built `JudgeRecord`s
# rather than through a batch. The aggregation rules being pinned are about
# lines that a REAL batch cannot produce in one invocation -- a stale
# gate-decided line sitting beside the votes for the same pair is what a
# re-grade leaves behind two passes apart, and a three-vote v1 line beside a
# two-vote v2 one is what the protocol change leaves behind -- and reaching
# them through the driver would mean staging two collections to assert one
# arithmetic rule.


MODEL_OF = {
    "run-a": "model-one", "run-b": "model-two", "run-c": "model-three",
    "run-a1": "model-one", "run-b1": "model-two",
    "run-a2": "model-one", "run-b2": "model-two",
    "run-a3": "model-one", "run-b3": "model-two",
}


def _judgment(**kw) -> JudgeRecord:
    fields = dict(
        judgment_id=uuid.uuid4().hex,
        judged_at="2026-08-18T00:00:00Z",
        judge_model_id=JUDGE_MODEL_ID_DEFAULT,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_prompt_sha="0" * 64,
        judge_sampling=dict(JUDGE_SAMPLING),
        rubric_version=RUBRIC_VERSION,
        kind="pairwise",
        task_id="calc-1",
    )
    fields.update(kw)
    return JudgeRecord(**fields)


def _vote(run_id_a, run_id_b, verdict, *, vote_index=0, sample_index=0, **kw):
    """One pairwise vote line.

    `position_assignment` FOLLOWS `vote_index` through `VOTE_POSITIONS` unless
    a test overrides it. A v2 line whose index and position disagree is a
    record the driver cannot write, and a fixture modelling an impossible
    record teaches the next reader the wrong shape. The v1 lines below --
    three votes at random positions -- are the case that overrides it, and
    they say so where they do.
    """
    kw.setdefault(
        "position_assignment", VOTE_POSITIONS[vote_index % len(VOTE_POSITIONS)]
    )
    return _judgment(
        kind="pairwise", run_id_a=run_id_a, run_id_b=run_id_b,
        sample_index=sample_index, verdict=verdict, vote_index=vote_index,
        input_payload_path="/payloads/x.json.gz", input_payload_sha="s" * 64,
        **kw,
    )


def _gate(run_id_a, run_id_b, by, *, sample_index=0, **kw):
    return _judgment(
        kind="pairwise", run_id_a=run_id_a, run_id_b=run_id_b,
        sample_index=sample_index, verdict="gate_decided", gate_decided_by=by,
        **kw,
    )


def _rubric(run_id, *, scores=2, flags=False, **kw):
    """One rubric line. `scores`/`flags` take a scalar for "same on every
    dimension", a dict for a specific profile, or `None` verbatim -- which is
    how the damaged-line cases reach the reader with a half missing."""
    if scores is not None and not isinstance(scores, dict):
        scores = {name: scores for name in RUBRIC_DIMENSIONS}
    if flags is not None and not isinstance(flags, dict):
        flags = {name: flags for name in RUBRIC_FLAGS}
    return _judgment(
        kind="rubric", run_id=run_id, vote_index=0,
        dimension_scores=None if scores is None else dict(scores),
        flags=None if flags is None else dict(flags),
        input_payload_path="/payloads/y.json.gz", input_payload_sha="t" * 64,
        **kw,
    )


def _judge_gen(judge_model_id=JUDGE_MODEL_ID_DEFAULT,
               prompt_version=JUDGE_PROMPT_VERSION,
               rubric_version=RUBRIC_VERSION):
    """One judge generation: the oracle a verdict came from. The key a win-rate
    matrix block and an Elo table each sit under, and the tail of the key a
    rubric profile block sits under."""
    return (judge_model_id, prompt_version, rubric_version)


def _generation(model, judge_model_id=JUDGE_MODEL_ID_DEFAULT,
                prompt_version=JUDGE_PROMPT_VERSION,
                rubric_version=RUBRIC_VERSION):
    """The key a rubric profile block sits under: the arm, and the judge
    generation that profiled it."""
    return (model,) + _judge_gen(judge_model_id, prompt_version,
                                 rubric_version)


def _empty_result() -> dict:
    """A batch that judged nothing. The sharpest case for the caveat: an
    implementation that emitted it alongside a number would skip it here."""
    return {
        "judged": [], "gate_decided": [], "skipped": [], "errors": [],
        "warnings": [], "summary": summarize([], {}),
        "audited": 0, "from_prior_invocations": 0,
    }


def test_majority_over_vote_lines_with_gate_decided_wins_and_half_point_ties():
    """The whole outcome rule in one collection: the votes for a pair collapse
    to one comparison through `majority`, a gate-decided pair is a win for the
    side the ladder already picked, and a tie is half a point to each.

    Deliberately ASYMMETRIC -- 2.5 points against 1.5 -- because a matrix that
    reported x's rate under y's name is invisible against a balanced fixture.
    """
    judgments = [
        # settled by votes, both positions agree
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=0),
        _vote("run-a", "run-b", "a", vote_index=1, sample_index=0),
        # settled by votes, the two positions disagree -> tie
        _vote("run-a1", "run-b1", "a", vote_index=0, sample_index=1),
        _vote("run-a1", "run-b1", "b", vote_index=1, sample_index=1),
        # settled by the ladder, one each way
        _gate("run-a2", "run-b2", "a", sample_index=2),
        _gate("run-a3", "run-b3", "b", sample_index=3),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][_judge_gen()][("model-one", "model-two")]
    assert row["voted"] == 2
    assert row["gate_decided"] == 2
    assert (row["wins_x"], row["wins_y"], row["ties"]) == (2, 1, 1)
    assert row["comparisons"] == 4
    assert row["win_rate_x"] == pytest.approx(2.5 / 4)
    assert row["win_rate_y"] == pytest.approx(1.5 / 4)


def test_a_comparison_short_of_a_strict_majority_is_a_tie_not_a_plurality_win():
    """`majority` is strict, and the summary must not launder a split decision
    into a win by counting votes itself.

    Under forced positions a 1-1 split is the judge preferring whichever
    submission it was shown first, which is a POSITION effect and not a
    preference between the two submissions. Breaking that tie by taking the
    `a_first` vote would publish the position effect as a win."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "b", vote_index=1),
    ]

    row = summarize(judgments, MODEL_OF)["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]

    assert (row["wins_x"], row["wins_y"], row["ties"]) == (0, 0, 1)


def test_vote_lines_supersede_a_stale_gate_decided_line_for_the_same_pair():
    """A re-grade that flips the losing side turns a gate-decided pair into a
    judgeable one, and the judgment file is APPEND-ONLY -- so the old
    gate-decided line stays on disk beside the votes that came later.

    Counting both would enter one comparison twice, once for each side. The
    votes win, and the disagreement is TALLIED rather than hidden: it is a
    finding about the collection, not noise.
    """
    judgments = [
        _gate("run-a", "run-b", "b"),
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "a", vote_index=1),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][_judge_gen()][("model-one", "model-two")]
    assert row["comparisons"] == 1
    assert row["voted"] == 1
    assert row["gate_decided"] == 0
    assert (row["wins_x"], row["wins_y"], row["ties"]) == (1, 0, 0)
    assert summary["superseded_gate_decided"] == 1
    # The line is still a line in the file, and the census still counts it.
    assert summary["lines"]["gate_decided"] == 1


def test_a_superseded_gate_decided_line_is_reported_even_when_it_agrees():
    """The tally is about the SHAPE of the file, not about who won. Counting
    only the disagreements would make a file full of stale lines look clean
    whenever the re-grade happened to keep the same winner."""
    judgments = [
        _gate("run-a", "run-b", "a"),
        _vote("run-a", "run-b", "a", vote_index=0),
    ]

    assert summarize(judgments, MODEL_OF)["superseded_gate_decided"] == 1


def test_gate_decided_is_counted_apart_from_the_vote_verdict_distribution():
    """`gate_decided` is the one verdict no model produced. Inside the
    distribution it is a fourth thing the judge said, and every rate computed
    off that denominator is wrong by however many pairs the ladder settled."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "a", vote_index=1),
        _vote("run-a1", "run-b1", "tie", vote_index=0, sample_index=1),
        _vote("run-a1", "run-b1", "tie", vote_index=1, sample_index=1),
        _gate("run-a2", "run-b2", "a", sample_index=2),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["vote_verdicts"] == {"a": 2, "tie": 2}
    assert "gate_decided" not in summary["vote_verdicts"]
    assert summary["lines"]["pairwise_votes"] == 4
    assert summary["lines"]["gate_decided"] == 1


def test_the_vote_verdict_vocabulary_matches_the_one_majority_accepts():
    """The driver's vocabulary and `majority`'s are the same three words. Two
    copies that drift make the summary drop a verdict `majority` would have
    happily counted -- or hand it one it raises on."""
    assert _VOTE_VERDICTS == _CANONICAL_VERDICTS


def test_the_summary_dedupes_to_the_last_line_per_resume_identity():
    """A re-judge appends. Aggregating both lines counts one vote twice, and
    the later line is the one a reader of the file lands on."""
    first = _vote("run-a", "run-b", "a", vote_index=0)
    second = JudgeRecord(**{
        **first.to_dict(), "judgment_id": uuid.uuid4().hex, "verdict": "b",
    })

    summary = summarize([first, second], MODEL_OF)

    assert summary["lines"]["pairwise_votes"] == 1
    assert summary["vote_verdicts"] == {"b": 1}


def test_two_judge_generations_over_one_pair_are_two_blocks_never_pooled():
    """`--judge-model openai.gpt-5.6-luna` re-judges a collection under a
    SECOND oracle, and the judgment file is append-only, so both generations'
    verdicts live in it. A matrix keyed on the two model names alone enters
    that pair twice: the comparison count doubles and the win rate is the mean
    of two oracles nobody asked for a joint opinion. That is the average
    `_comparison_key` keeps out of a bucket and `_rubric_profile` keeps out of
    a block -- here on the PRIMARY channel, which is the one a reader ranks on.

    Deliberately OPPOSITE verdicts. Two generations that agree pool into a
    number that happens to be right, so a pooled matrix only shows itself when
    they disagree -- and 1-1 over 2 comparisons is exactly the 50% a reader
    would take for a real dead heat.
    """
    other = "openai.gpt-5.6-luna"
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "b", vote_index=0, judge_model_id=other),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert set(summary["comparisons"]) == {_judge_gen(), _judge_gen(other)}
    pair = ("model-one", "model-two")
    sol = summary["comparisons"][_judge_gen()][pair]
    luna = summary["comparisons"][_judge_gen(other)][pair]
    # One comparison each, not two in one row: the counts are not blended.
    assert sol["comparisons"] == luna["comparisons"] == 1
    assert (sol["wins_x"], sol["wins_y"], sol["ties"]) == (1, 0, 0)
    assert (luna["wins_x"], luna["wins_y"], luna["ties"]) == (0, 1, 0)
    assert (sol["win_rate_x"], sol["win_rate_y"]) == (1.0, 0.0)
    assert (luna["win_rate_x"], luna["win_rate_y"]) == (0.0, 1.0)

    # Two Elo tables, each over its own oracle's comparisons. Pooled, the two
    # opposite results cancel to the base and the table prints a dead heat
    # neither judge reported.
    assert set(summary["elo"]) == {_judge_gen(), _judge_gen(other)}
    sol_elo = summary["elo"][_judge_gen()]
    luna_elo = summary["elo"][_judge_gen(other)]
    assert sol_elo["model-one"] > sol_elo["model-two"]
    assert luna_elo["model-two"] > luna_elo["model-one"]


def test_a_bumped_prompt_or_rubric_version_also_opens_its_own_matrix_block():
    """The whole generation key partitions, not just the model id. A verdict is
    reproducible against the model that gave it, the prompt text it saw AND the
    rubric it was scored under -- `_resume_key` treats a change to any of the
    three as new work, so the matrix has to treat it as a new reading."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "a", vote_index=0, judge_prompt_version=99),
        _vote("run-a", "run-b", "a", vote_index=0, rubric_version="99"),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert set(summary["comparisons"]) == {
        _judge_gen(),
        _judge_gen(prompt_version=99),
        _judge_gen(rubric_version="99"),
    }
    assert set(summary["elo"]) == set(summary["comparisons"])
    for block in summary["comparisons"].values():
        assert block[("model-one", "model-two")]["comparisons"] == 1


def test_old_three_vote_lines_aggregate_and_never_pool_with_two_vote_ones():
    """A judgment file outlives a protocol change, and this one holds both.

    v1 lines are three votes drawn at RANDOM positions; v2 lines are two votes
    at forced ones. `majority` reads either -- it counts canonical verdicts and
    has no opinion about how many there are -- so the old comparison still
    resolves, and `_comparison_key` carries the prompt version, so the two
    protocols land in two blocks.

    Pooled, they would be one pair entered twice: the comparison count doubles
    and the printed rate is the mean of two protocols nobody ran together. The
    verdicts here are OPPOSITE for that reason -- two generations that agreed
    would pool into a number that happens to be right.
    """
    judgments = [
        # v1: three votes, random positions, a takes it 2-1.
        _vote("run-a", "run-b", "a", vote_index=0, judge_prompt_version=1,
              position_assignment="b_first"),
        _vote("run-a", "run-b", "b", vote_index=1, judge_prompt_version=1,
              position_assignment="b_first"),
        _vote("run-a", "run-b", "a", vote_index=2, judge_prompt_version=1,
              position_assignment="a_first"),
        # v2: two votes, one per forced position, and b takes it.
        _vote("run-a", "run-b", "b", vote_index=0),
        _vote("run-a", "run-b", "b", vote_index=1),
    ]

    summary = summarize(judgments, MODEL_OF)

    old, new = _judge_gen(prompt_version=1), _judge_gen()
    assert set(summary["comparisons"]) == {old, new}
    pair = ("model-one", "model-two")
    assert summary["comparisons"][old][pair]["comparisons"] == 1
    assert summary["comparisons"][new][pair]["comparisons"] == 1
    # The old protocol's majority-of-three still resolves, in its own block.
    assert summary["comparisons"][old][pair]["wins_x"] == 1
    assert summary["comparisons"][new][pair]["wins_y"] == 1
    # Every line is still counted in the census, whichever protocol wrote it.
    assert summary["lines"]["pairwise_votes"] == 5
    assert summary["vote_verdicts"] == {"a": 2, "b": 3}
    assert set(summary["elo"]) == {old, new}


def test_the_comparison_key_carries_the_generation_the_matrix_partitions_on():
    """The matrix reads the generation back off the tail of a
    `_comparison_key`. Two independent derivations of "which oracle is this"
    would drift, and the drift is silent: the buckets stay apart while the
    blocks they are printed in pool."""
    vote = _vote("run-a", "run-b", "a", vote_index=0,
                 judge_model_id="openai.gpt-5.6-luna")

    assert _generation_of(_comparison_key(vote)) == _judge_generation(vote)
    assert _judge_generation(vote) == _judge_gen("openai.gpt-5.6-luna")


def test_rubric_profile_reports_per_dimension_means_and_never_a_sum():
    """The rubric is DIAGNOSTIC. Summing five dimensions into one number is the
    absolute 1-10 score §4.2.3 rules out, wearing a rubric's clothes -- and
    joining it to `resolved` is the other half of the same mistake, which is why
    a `JudgeRecord` carries no `resolved` to join to."""
    judgments = [
        _rubric("run-a", scores=2, flags={
            "introduced_stub": True, "left_debug_artifacts": False,
            "wrote_tests": True,
        }),
        _rubric("run-a1", scores={
            "functional_equivalence": 0,
            "completeness": 1,
            "cross_file_consistency": 2,
            "scope_discipline": 2,
            "convention_adherence": 0,
        }, flags={
            "introduced_stub": False, "left_debug_artifacts": False,
            "wrote_tests": True,
        }),
    ]

    row = summarize(judgments, MODEL_OF)["rubric_profile"][
        _generation("model-one")
    ]

    assert set(row) == {"runs", "dimensions", "flags"}
    assert row["runs"] == 2
    assert {name: cell["mean"] for name, cell in row["dimensions"].items()} == {
        "functional_equivalence": pytest.approx(1.0),
        "completeness": pytest.approx(1.5),
        "cross_file_consistency": pytest.approx(2.0),
        "scope_discipline": pytest.approx(2.0),
        "convention_adherence": pytest.approx(1.0),
    }
    assert {name: cell["mean"] for name, cell in row["flags"].items()} == {
        "introduced_stub": pytest.approx(0.5),
        "left_debug_artifacts": pytest.approx(0.0),
        "wrote_tests": pytest.approx(1.0),
    }
    # No aggregate ANYWHERE in the profile -- not at the top, not per block, not
    # among the dimension names. The per-dimension cell holds the mean of ONE
    # dimension and the count behind it, which is the opposite of an aggregate.
    banned = {"total", "sum", "score", "overall", "mean", "rank", "resolved"}
    assert not banned & set(row)
    assert not banned & set(row["dimensions"])
    assert not banned & set(summarize(judgments, MODEL_OF))
    for cell in row["dimensions"].values():
        assert set(cell) == {"mean", "n"}


def test_each_dimension_carries_the_count_it_was_averaged_over():
    """The names are read from the records, not from `RUBRIC_DIMENSIONS`, so a
    dimension carried by some lines and not others is a shape this walk
    produces -- a newer rubric's sixth against an older one's five. Its mean
    printed beside the block's `runs`, with nothing saying how many lines it
    actually came from, is a denominator moving invisibly."""
    judgments = [
        _rubric("run-a", scores={"functional_equivalence": 2}),
        _rubric("run-a1", scores={
            "functional_equivalence": 0, "novel_dimension": 2,
        }),
    ]

    row = summarize(judgments, MODEL_OF)["rubric_profile"][
        _generation("model-one")
    ]

    assert row["runs"] == 2
    assert row["dimensions"]["functional_equivalence"] == {
        "mean": pytest.approx(1.0), "n": 2,
    }
    # Carried by ONE of the two lines, and the profile says so rather than
    # letting a reader assume the block's `runs` of 2.
    assert row["dimensions"]["novel_dimension"] == {
        "mean": pytest.approx(2.0), "n": 1,
    }


def test_two_rubric_generations_for_one_run_are_two_blocks_not_one_average():
    """`_resume_key` keys a rubric line on the three version fields, so a run
    re-judged under a bumped rubric survives the dedupe as TWO lines -- that is
    the point of the key. Grouping the profile on the model alone would blend
    two rubrics into one row under a `runs` count that is really a line count.
    """
    judgments = [
        _rubric("run-a", scores=2, rubric_version="1.0.0"),
        _rubric("run-a", scores=0, rubric_version="2.0.0"),
    ]

    profile = summarize(judgments, MODEL_OF)["rubric_profile"]

    assert set(profile) == {
        _generation("model-one", rubric_version="1.0.0"),
        _generation("model-one", rubric_version="2.0.0"),
    }
    for version, expected in (("1.0.0", 2.0), ("2.0.0", 0.0)):
        block = profile[_generation("model-one", rubric_version=version)]
        # One run, not two: `runs` counts runs under ONE generation, and never
        # lines across generations.
        assert block["runs"] == 1
        assert block["dimensions"]["completeness"] == {
            "mean": expected, "n": 1,
        }


def test_a_changed_judge_model_or_prompt_version_also_splits_the_profile():
    """The same rule for the other two thirds of a generation. A verdict is only
    reproducible against the model that gave it and the prompt text it saw, so
    neither may be averaged across."""
    judgments = [
        _rubric("run-a", scores=2),
        _rubric("run-a", scores=0, judge_model_id="openai.gpt-5.6-luna"),
        _rubric("run-a", scores=1,
                judge_prompt_version=JUDGE_PROMPT_VERSION + 1),
    ]

    profile = summarize(judgments, MODEL_OF)["rubric_profile"]

    assert len(profile) == 3
    assert all(block["runs"] == 1 for block in profile.values())


def test_the_rubric_profile_is_per_model_not_pooled():
    """Pooling is how a profile stops being a profile: two arms averaged into
    one row says nothing about either."""
    judgments = [
        _rubric("run-a", scores=2),
        _rubric("run-b", scores=0),
    ]

    profile = summarize(judgments, MODEL_OF)["rubric_profile"]

    assert set(profile) == {_generation("model-one"), _generation("model-two")}
    for model, expected in (("model-one", 2.0), ("model-two", 0.0)):
        assert profile[_generation(model)]["dimensions"]["completeness"][
            "mean"
        ] == expected


def test_a_rubric_line_with_an_unscorable_value_is_counted_not_raised():
    """`sum(values) / len(values)` raises `TypeError` on a hand-edited score
    holding a string or a `None` -- and a `None` passes a `name in block` guard.
    `summarize` runs inside `judge_event_log`'s return, so that exception would
    destroy the result of a batch that already paid for thousands of calls."""
    good = _rubric("run-a", scores=2)
    for bad_value in (None, "excellent", True, 1.5):
        damaged = {**dict.fromkeys(RUBRIC_DIMENSIONS, 2),
                   "completeness": bad_value}

        summary = summarize([good, _rubric("run-a1", scores=damaged)],
                            MODEL_OF)

        assert summary["dropped"]["unreadable_rubric"] == 1
        block = summary["rubric_profile"][_generation("model-one")]
        assert block["runs"] == 1
        assert block["dimensions"]["completeness"] == {"mean": 2.0, "n": 1}


def test_a_rubric_line_with_a_score_in_a_flag_slot_is_unreadable():
    """`bool` is a subclass of `int`. Without the second half of the type test a
    `2` in a flag slot would report as a 200% flag rate, and a `True` in a
    dimension slot would average in as a 1."""
    judgments = [
        _rubric("run-a", flags={**dict.fromkeys(RUBRIC_FLAGS, False),
                                "wrote_tests": 2}),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["rubric_profile"] == {}
    assert summary["dropped"]["unreadable_rubric"] == 1


def test_a_rubric_line_with_no_scores_is_unreadable_not_a_silent_denominator():
    """The explicit choice: an empty or absent half makes the line UNREADABLE
    rather than "readable and contributing nothing". Counting it in `runs` while
    it moves no mean is a denominator that grows with no number under it
    moving -- the same invisible-denominator failure the per-dimension counts
    are placed against."""
    for empty in ({}, None):
        for kwargs in ({"scores": empty}, {"flags": empty}):
            summary = summarize(
                [_rubric("run-a", scores=2), _rubric("run-a1", **kwargs)],
                MODEL_OF,
            )

            assert summary["dropped"]["unreadable_rubric"] == 1
            assert summary["rubric_profile"][
                _generation("model-one")
            ]["runs"] == 1


def test_the_ratings_are_returned_in_name_order():
    """The printout sorts by rating, but the dict is the interface and a
    stable key order is what makes two summaries diffable at all."""
    ratings = elo_from_outcomes(_block_structured_outcomes())

    assert list(ratings) == sorted(ratings)


def test_the_matrix_reports_each_arms_rate_under_its_own_name():
    """`run_id_a`/`run_id_b` are canonical by RUN ID; a matrix row is keyed by
    MODEL name, sorted. The two orders are independent, so a summary that
    carried the a-side score straight into the x column would report one arm's
    win rate under the other's name -- and stay invisible in every fixture where
    the two orders happen to agree, which is most of them.
    """
    model_of = {"run-a": "zeta-model", "run-b": "alpha-model"}
    judgments = [_vote("run-a", "run-b", "a", vote_index=0)]

    summary = summarize(judgments, model_of)

    row = summary["comparisons"][_judge_gen()][("alpha-model", "zeta-model")]
    assert (row["wins_x"], row["wins_y"], row["ties"]) == (0, 1, 0)
    assert row["win_rate_x"] == 0.0
    assert row["win_rate_y"] == 1.0
    elo = summary["elo"][_judge_gen()]
    assert elo["zeta-model"] > elo["alpha-model"]


def test_elo_refuses_a_score_that_is_not_a_win_a_loss_or_a_tie():
    """The only three outcomes a comparison has. A 3.0 slipped in from a vote
    COUNT rather than a result would inflate the table silently and by an amount
    nobody could reconstruct from the printout."""
    with pytest.raises(ValueError) as exc:
        elo_from_outcomes([("model-one", "model-two", 3.0)])
    assert "model-one" in str(exc.value)


def test_elo_follows_the_win_rates_rather_than_leading_them():
    """Descriptive, not a second opinion. The arm that won more comparisons
    outranks the one that won fewer, and it does so through `summarize`'s own
    triples -- the call site is what has to keep working, not just the
    function."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=0),
        _vote("run-a1", "run-b1", "a", vote_index=0, sample_index=1),
        _vote("run-a2", "run-b2", "b", vote_index=0, sample_index=2),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][_judge_gen()][("model-one", "model-two")]
    assert row["win_rate_x"] > row["win_rate_y"]
    elo = summary["elo"][_judge_gen()]
    assert elo["model-one"] > elo["model-two"]


#: The C1 fixture. Four arms whose TRUE strength runs against their names:
#: `arm-a` is the weakest and sorts first, `arm-d` is the strongest and sorts
#: last. The win counts are a 100-point-per-rung ladder (1000/1100/1200/1300)
#: rounded over 150 comparisons per pair -- roughly this bakeoff's real size,
#: ~40-60 tasks at N>=3 samples -- so the win-rate order is strictly
#: `arm-d > arm-c > arm-b > arm-a` with no tie to argue about.
_LADDER_WINS = {
    ("arm-a", "arm-b"): (54, 96),
    ("arm-a", "arm-c"): (36, 114),
    ("arm-a", "arm-d"): (23, 127),
    ("arm-b", "arm-c"): (54, 96),
    ("arm-b", "arm-d"): (36, 114),
    ("arm-c", "arm-d"): (54, 96),
}


def _block_structured_outcomes() -> list[tuple[str, str, float]]:
    """`_LADDER_WINS` emitted the way a sequential rating walk is worst hurt.

    One contiguous block per pair in name order, and inside each block every
    loss for the first-named arm before every win. That is not an adversarial
    stream so much as the ONLY stream the sequential implementation ever saw:
    it began with `sorted(outcomes)`, which turns any input order into exactly
    this shape.
    """
    stream: list[tuple[str, str, float]] = []
    for (model_x, model_y), (wins_x, wins_y) in sorted(_LADDER_WINS.items()):
        stream.extend([(model_x, model_y, 0.0)] * wins_y)
        stream.extend([(model_x, model_y, 1.0)] * wins_x)
    return stream


def _win_totals(outcomes) -> dict[str, float]:
    """Points per arm straight off the triples, ties counted as half."""
    totals: dict[str, float] = {}
    for model_a, model_b, score_a in outcomes:
        totals[model_a] = totals.get(model_a, 0.0) + score_a
        totals[model_b] = totals.get(model_b, 0.0) + (1.0 - score_a)
    return totals


def test_elo_recovers_the_true_order_on_a_block_structured_outcome_stream():
    """THE regression. A rating that is a function of the model NAMES is not a
    rating, and the sequential K-update was one: it walked `sorted(outcomes)`,
    so each pair arrived as one monotone block and the arm whose block landed
    last kept whatever the last few dozen results handed it.

    On this fixture the old code printed the ranking exactly BACKWARDS --
    `arm-a`, with 113 of 900 points, on top at 1262, and `arm-d`, with 337, at
    the bottom on 520. Rename the arms so the names sort with strength instead
    of against it and the same collection printed the right answer, which is
    the whole complaint.
    """
    outcomes = _block_structured_outcomes()
    totals = _win_totals(outcomes)
    assert sorted(totals, key=lambda arm: -totals[arm]) == [
        "arm-d", "arm-c", "arm-b", "arm-a"
    ]

    ratings = elo_from_outcomes(outcomes)

    assert sorted(ratings, key=lambda arm: -ratings[arm]) == [
        "arm-d", "arm-c", "arm-b", "arm-a"
    ]


def test_elo_ratings_are_invariant_under_model_renaming():
    """A relabelling carries no information about strength, so it may not move
    a single rating. This is the property the sequential walk broke: the same
    900 comparisons under `zulu`/`yankee`/`xray`/`whiskey` sorted into a
    different order and therefore rated differently.

    Bit-for-bit, not approximately, because an approximate assertion here would
    pass on a function that had merely made the name dependence small. If this
    ever starts flaking in the last decimal, the fix is `math.fsum` over the
    per-arm denominators, not a tolerance -- see the function's docstring.
    """
    rename = {
        "arm-a": "zulu", "arm-b": "yankee", "arm-c": "xray", "arm-d": "whiskey",
    }
    outcomes = _block_structured_outcomes()

    before = elo_from_outcomes(outcomes)
    after = elo_from_outcomes(
        [(rename[a], rename[b], score) for a, b, score in outcomes]
    )

    assert after == {rename[arm]: rating for arm, rating in before.items()}


def test_elo_is_a_function_of_the_outcome_multiset_not_its_order():
    """The triples are assembled from a dict walk, so the order they arrive in
    is an accident of iteration. Two passes over one unchanged collection must
    print one table -- otherwise the ratings appear to move while nothing about
    the collection did.

    The old code bought this with `sorted(outcomes)`, which is precisely what
    made it name-dependent. Bradley-Terry gets it for free: the likelihood is
    defined over win COUNTS, and a shuffle does not move a count.
    """
    outcomes = _block_structured_outcomes()
    shuffled = list(outcomes)
    random.Random(11).shuffle(shuffled)
    assert shuffled != outcomes

    assert elo_from_outcomes(shuffled) == elo_from_outcomes(outcomes)


def test_a_sixty_forty_split_between_two_arms_reads_as_about_seventy_elo():
    """The scale has to MEAN something. Unregularised, a 60/40 split is a
    strength ratio of 1.5 and 400*log10(1.5) = 70.44 rating points -- the
    number an Elo reader would quote for that split, which is the entire reason
    the ratings are reported on this scale rather than as raw strengths.

    The function returns 69.72, and the gap is the virtual tie, not an error in
    the scale: the fit sees 60.5/40.5, and 400*log10(60.5/40.5) = 69.72. Both
    formulas are written out because a reader who re-derives 70.44, finds 69.72
    and has only the first one to compare against concludes the implementation
    is 0.7 points off. The prior costs 0.72 points here, which is its whole
    visible effect on a two-arm collection.

    Tolerance is +-10: this test pins the SCALE -- that a 60/40 split reads as
    tens of points and not as 494, which is what the sequential K-update
    returned -- and it should not fail over a change to the prior.
    """
    outcomes = [("winner", "loser", 1.0)] * 60 + [("winner", "loser", 0.0)] * 40

    ratings = elo_from_outcomes(outcomes)

    assert ratings["winner"] - ratings["loser"] == pytest.approx(69.7, abs=10.0)


def test_an_undefeated_arm_gets_a_finite_rating():
    """Why the virtual tie exists. The unregularised Bradley-Terry MLE for an
    arm that never lost is +infinity, and the iteration chasing it either
    diverges for 10,000 rounds or prints an `inf` that formats as a rating.
    One virtual tie per played pair bounds every strength without touching the
    ranking -- and the winless arm at the other end is bounded by the same
    stroke.
    """
    outcomes = (
        [("undefeated", "middle", 1.0)] * 20
        + [("middle", "winless", 1.0)] * 10
    )

    ratings = elo_from_outcomes(outcomes)

    assert all(math.isfinite(rating) for rating in ratings.values())
    assert ratings["undefeated"] > ratings["middle"] > ratings["winless"]


def test_ties_enter_the_likelihood_as_half_wins():
    """The matrix already scores a tie as half a point to each side. If the
    rating table scored it any other way the two would rank differently off one
    collection, and the reader would have to guess which of the two printed
    orders the comparisons actually support.
    """
    four_ties = elo_from_outcomes([("model-one", "model-two", 0.5)] * 4)
    split = elo_from_outcomes(
        [("model-one", "model-two", 1.0)] * 2
        + [("model-one", "model-two", 0.0)] * 2
    )

    assert four_ties == split
    # Four drawn comparisons are four comparisons, not a reason to separate.
    assert four_ties["model-one"] == pytest.approx(four_ties["model-two"])


def test_ratings_are_mean_anchored():
    """Bradley-Terry fixes only the DIFFERENCES between strengths; the overall
    level is free, and left free it wanders with the arm set. Anchoring the
    mean at 1000 is what lets a reader carry an intuition about the number
    across two blocks -- and it is a presentation choice, not a measurement, so
    it is pinned here rather than left to whatever the normalisation happened
    to land on.

    LITERAL 1000, not the constant: written against the constant this test
    would follow it anywhere and say nothing about which scale the table was
    printed on, and a rating whose anchor nobody recorded is a number nobody
    can reproduce. The constants are pinned to their literals once, here, for
    the same reason.
    """
    assert (ELO_SCALE, ELO_ANCHOR) == (400.0, 1000.0)

    for outcomes in (
        _block_structured_outcomes(),
        [("model-one", "model-two", 1.0)],
        [("solo-a", "solo-b", 0.5), ("solo-b", "solo-c", 1.0)],
    ):
        ratings = elo_from_outcomes(outcomes)

        assert math.fsum(ratings.values()) / len(ratings) == pytest.approx(
            1000.0
        )


def test_empty_outcomes_yield_an_empty_table():
    """A collection with nothing comparable in it has no ranking, and an empty
    table says so. The alternative -- every arm at the anchor -- is a printed
    dead heat that no comparison supports.
    """
    assert elo_from_outcomes([]) == {}


def test_a_comparison_whose_arms_cannot_be_named_is_dropped_and_counted():
    """`model_of` comes from the event log, and a judgment from an earlier
    invocation can name a run this collection no longer holds. Entering it under
    a `None` arm would put a row called `None` in the matrix; dropping it
    silently would shrink a denominator nobody could see move."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-gone", "a", vote_index=0, sample_index=1),
        _rubric("run-vanished"),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert set(summary["comparisons"][_judge_gen()]) == {
        ("model-one", "model-two")
    }
    assert summary["dropped"]["unknown_model_comparison"] == 1
    assert summary["dropped"]["unknown_model_rubric"] == 1
    assert summary["rubric_profile"] == {}


def test_a_comparison_with_one_model_on_both_sides_is_dropped_and_counted():
    """A cell keys on model, so the driver cannot produce this -- a judgment
    file read beside the wrong collection can. "model-one beat model-one" is a
    row that makes the matrix unreadable rather than visibly wrong."""
    judgments = [_vote("run-a", "run-a1", "a", vote_index=0)]

    summary = summarize(judgments, MODEL_OF)

    assert summary["comparisons"] == {}
    assert summary["dropped"]["same_model_comparison"] == 1


def test_a_gate_decided_line_naming_no_winner_settles_nothing():
    """`gate_decided_by` is the entire content of a gate-decided line. Without
    it there is no verdict, and defaulting to "a" would hand the win to
    whichever run id happened to sort first."""
    judgments = [_gate("run-a", "run-b", None)]

    summary = summarize(judgments, MODEL_OF)

    assert summary["comparisons"] == {}
    assert summary["dropped"]["unreadable_verdict"] == 1
    # Not counted as a gate-decided comparison either: it settled nothing. The
    # line is accounted for under `dropped`, which is where a reader looks for
    # what the numbers above do not include.
    assert summary["lines"]["gate_decided"] == 0


def test_a_judgment_naming_a_run_this_log_never_held_does_not_stop_the_batch(
    tmp_path,
):
    """Run ids are unique WITHIN a collection and not across one, so a judgment
    file beside the wrong event log names runs that are simply not there. The
    comparison cannot be entered under a name nobody has -- but the end of a
    paid batch is not the place to discover it, and a `read_run` raising out of
    the summary would lose every verdict the pass just bought."""
    root = _two_arms(tmp_path)
    append_judgment(
        judgments_path(root),
        _vote("run-ghost-a", "run-ghost-b", "a", vote_index=0, sample_index=9),
    )

    result = _run(root, rubric=False)

    assert result["errors"] == []
    assert result["summary"]["dropped"]["unknown_model_comparison"] == 1
    assert result["summary"]["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]["comparisons"] == 1


def test_a_pairwise_line_with_an_unreadable_verdict_is_counted_not_raised():
    """`majority` raises on a verdict outside its vocabulary. A hand-edited line
    reaching it would take the whole summary down at the end of a batch that
    already paid for thousands of calls."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "first", vote_index=1),
        _judgment(kind="something-else", run_id="run-a"),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["dropped"]["unreadable_verdict"] == 1
    assert summary["dropped"]["unreadable_kind"] == 1
    assert summary["comparisons"][_judge_gen()][("model-one", "model-two")][
        "comparisons"
    ] == 1


def test_the_kappa_caveat_line_is_always_printed(tmp_path, monkeypatch, capsys):
    """§4.3: below κ 0.6 the number is directional only, and an UNMEASURED κ is
    below 0.6 by construction (OPEN-5). The caveat is hard-coded and
    unconditional because a flag is how it gets dropped -- so it is printed for
    an empty batch, for a fully gate-decided one, and under every flag the
    parser has."""
    print_summary(_empty_result())
    assert KAPPA_CAVEAT in capsys.readouterr().out

    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)
    print_summary(_run(root, rubric=False))
    assert KAPPA_CAVEAT in capsys.readouterr().out

    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion", lambda *a, **k: FakeComplete()
    )
    for flags in ([], ["--no-rubric"], ["--re-judge"],
                  ["--judge-model", "openai.gpt-5.6-terra"],
                  ["--no-rubric", "--re-judge", "--samples", "0"]):
        main(["--event-log", str(tmp_path / "fresh"), *flags])
        assert KAPPA_CAVEAT in capsys.readouterr().out


def test_the_caveat_survives_a_summary_this_printer_cannot_read(capsys):
    """The caveat sits in a `finally`, not at the end of a happy path.

    A summary dict shaped by an older or a newer reader raises PARTWAY through
    the sections -- after the matrix is already on the terminal. A reader who
    got the numbers and not the caveat is worse off than one who got neither,
    which is the whole reason the line is unconditional.
    """
    result = _empty_result()
    del result["summary"]["elo"]

    with pytest.raises(KeyError):
        print_summary(result)

    assert KAPPA_CAVEAT in capsys.readouterr().out


def test_the_caveat_falls_back_to_ascii_on_a_terminal_that_cannot_encode_it(
    monkeypatch,
):
    """`KAPPA_CAVEAT` carries a κ and an em dash. On a stdout the environment
    pinned to ASCII -- `LC_ALL=C`, or a pipe into a tool that did -- `print`
    raises `UnicodeEncodeError`, which drops the caveat AND takes the exit path
    down with it. That is this line dying by environment variable instead of by
    flag, and it is the same death."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    _print_kappa_caveat()
    stream.flush()

    text = stream.buffer.getvalue().decode("ascii")
    assert "must not carry a decision" in text


def test_the_kappa_caveat_says_exactly_what_it_has_to_say():
    """The sentence itself, pinned literally. Every other test asserts
    `KAPPA_CAVEAT in out`, which follows the constant wherever it goes -- so all
    of them would pass against a caveat softened into agreement. That is how
    this line actually dies: not deleted, reworded.

    The ASCII fallback carries the same claim, because a terminal that cannot
    encode a κ is not a terminal that may be told less.
    """
    assert KAPPA_CAVEAT == (
        "κ unmeasured (OPEN-5) — every number above is directional only and "
        "must not carry a decision."
    )
    for text in (KAPPA_CAVEAT, _KAPPA_CAVEAT_ASCII):
        assert "unmeasured (OPEN-5)" in text
        assert "directional only" in text
        assert "must not carry a decision" in text
    assert _KAPPA_CAVEAT_ASCII.isascii()


def test_no_command_line_flag_offers_to_suppress_the_caveat(capsys):
    """The tripwire for the way the caveat actually dies: somebody adds
    `--quiet` or `--no-caveat` and the printout becomes conditional. There is no
    such flag, and this fails the moment one appears."""
    with pytest.raises(SystemExit):
        main(["--help"])

    text = capsys.readouterr().out.lower()
    for banned in ("--quiet", "--no-caveat", "--no-kappa", "--brief",
                   "--no-summary"):
        assert banned not in text


def test_the_summary_prints_the_matrix_the_profile_and_the_elo_table(
    tmp_path, capsys,
):
    """The summary MAY name arms -- `model_of` comes from the event log. The
    payload never does, which is `test_judge.py`'s business, not this file's."""
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
        ],
        [
            _grade("run-a", model="model-one"),
            _grade("run-b", model="model-two"),
        ],
    )

    print_summary(_run(root, rubric=True))

    out = capsys.readouterr().out
    assert "model-one vs model-two" in out
    assert "win rate" in out.lower()
    assert "rubric profile" in out.lower()
    assert "elo" in out.lower()
    assert "functional_equivalence" in out
    assert "wrote_tests" in out
    # The generation is named beside the profile block, and every mean carries
    # the count it was taken over. Neither is inferable from the block header.
    assert f"rubric {RUBRIC_VERSION}" in out
    assert f"prompt v{JUDGE_PROMPT_VERSION}" in out
    assert JUDGE_MODEL_ID_DEFAULT in out
    assert "n=1" in out
    assert KAPPA_CAVEAT in out


def test_the_printed_profile_carries_each_dimension_count_beside_its_mean(
    capsys,
):
    """A mean on the terminal without its own denominator is what this pins.
    The block's `runs` cannot stand in: two lines here carry
    `functional_equivalence` and only one carries `novel_dimension`, so the two
    means are over different denominators under one `2 run(s)` header.

    Asserted on the DIMENSION lines specifically. A blanket `"n=" in out` is
    satisfied by the flag block below it, which is how a dimension line that
    lost its count would slip through.
    """
    result = _empty_result()
    result["summary"] = summarize(
        [
            _rubric("run-a", scores={"functional_equivalence": 2}),
            _rubric("run-a1", scores={
                "functional_equivalence": 0, "novel_dimension": 2,
            }),
        ],
        MODEL_OF,
    )

    print_summary(result)

    lines = capsys.readouterr().out.splitlines()
    (shared,) = [line for line in lines if "functional_equivalence" in line]
    (partial,) = [line for line in lines if "novel_dimension" in line]
    assert "n=2" in shared
    assert "n=1" in partial


def test_the_printed_matrix_and_elo_table_name_the_generation_over_each_block(
    capsys,
):
    """A block that does not name its oracle is one the reader pools in their
    head, which is the same wrong number the arithmetic was just stopped from
    producing. Two generations, two matrix blocks, two Elo tables, each headed
    by the judge that produced it.
    """
    other = "openai.gpt-5.6-luna"
    result = _empty_result()
    result["summary"] = summarize(
        [
            _vote("run-a", "run-b", "a", vote_index=0),
            _vote("run-a", "run-b", "b", vote_index=0, judge_model_id=other),
        ],
        MODEL_OF,
    )

    print_summary(result)

    out = capsys.readouterr().out
    # Once over the matrix block and once over the Elo table, per generation.
    assert out.count(f"judge {JUDGE_MODEL_ID_DEFAULT},") == 2
    assert out.count(f"judge {other},") == 2
    assert out.count("model-one vs model-two") == 2
    # The pooled shape this partition exists to prevent: one row saying 1-1
    # over two comparisons, which reads as a dead heat neither judge reported.
    assert "over 2 comparison(s)" not in out


def test_the_summary_mentions_a_superseded_gate_decided_line_when_there_is_one(
    capsys,
):
    """Append-only means the disagreement IS the finding, so it is surfaced
    rather than quietly resolved in the aggregation."""
    result = _empty_result()
    result["summary"] = summarize(
        [
            _gate("run-a", "run-b", "b"),
            _vote("run-a", "run-b", "a", vote_index=0),
        ],
        MODEL_OF,
    )

    print_summary(result)

    out = capsys.readouterr().out
    assert "superseded" in out.lower()
    assert "1 gate-decided line" in out


def test_the_summary_says_nothing_about_superseding_when_nothing_was(capsys):
    print_summary(_empty_result())

    assert "superseded" not in capsys.readouterr().out.lower()


def test_summary_covers_the_campaign_not_the_invocation(tmp_path, capsys):
    """A judging pass is thousands of paid calls and is killed far more often
    than it finishes, so the last invocation routinely writes two lines. A
    summary computed from those two describes a campaign of two."""
    root = _two_arms(tmp_path)
    _run(root, rubric=False)

    second = _run(root, rubric=True)

    # Two vote lines from the first invocation, two rubric lines from this one.
    assert second["audited"] == 4
    assert second["from_prior_invocations"] == 2
    summary = second["summary"]
    assert summary["lines"] == {
        "rubric": 2, "pairwise_votes": 2, "gate_decided": 0,
    }
    # The comparison the FIRST invocation judged is still in the matrix -- and
    # it is ONE comparison, because the two forced positions are one.
    assert summary["comparisons"][_judge_gen()][("model-one", "model-two")][
        "comparisons"
    ] == 1
    assert set(summary["rubric_profile"]) == {
        _generation("model-one"), _generation("model-two"),
    }

    print_summary(second)
    out = capsys.readouterr().out
    assert "4 judgment(s)" in out
    assert "2 from prior invocation(s)" in out


def test_the_summary_names_arms_for_runs_this_invocation_never_read(tmp_path):
    """The campaign rule has a name problem the grade summary does not: a
    judgment from an earlier pass can name a run that a later re-grade has since
    excluded, and `judge_event_log` drops excluded runs BEFORE reading them. The
    comparison is still in the file and still belongs in the matrix, so the name
    has to be recovered from the event log rather than from this pass's records.
    """
    root = _two_arms(tmp_path)
    _run(root, rubric=False)

    # The re-grade that excludes one side, appended after the judgment.
    append_grade(
        grades_path(root),
        _grade("run-b", model="model-two", resolved=None, grader_version="3"),
    )
    second = _run(root, rubric=False)

    row = second["summary"]["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]
    assert row["comparisons"] == 1
    assert second["summary"]["dropped"]["unknown_model_comparison"] == 0


def test_the_summary_is_a_printout_and_stores_nothing(tmp_path):
    """`grade.py summarize`'s precedent, and the reason it matters more here: a
    stored Elo is a published number, and §4.3 forbids publishing one without
    the κ that was in force. Nothing on disk moves when the summary is taken."""
    root = _two_arms(tmp_path)
    result = _run(root, rubric=True)
    before = _bytes_under(root)

    summarize(_lines(root), {"run-a": "model-one", "run-b": "model-two"})
    print_summary(result)

    assert _bytes_under(root) == before
