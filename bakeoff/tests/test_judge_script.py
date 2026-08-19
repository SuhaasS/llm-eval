"""The judging batch driver: which units get a line, and which never do.

Nothing here opens a socket. `judge_event_log` composes two seams -- `complete`
(the `CompleteFn`, one rendered prompt in, raw text out) and `rng` (the position
draw) -- and every test injects both. The EVENT LOG and the GRADE FILE are real,
built in `tmp_path` with real `RunRecord`s and real `append_grade` lines, because
the thing being pinned is which runs get paired and in what order, and a fake log
would let the driver read the wrong ones and still pass.

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
import random
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
    _CANONICAL_VERDICTS,
    prompt_sha,
)
from bakeoff.judge_schema import (
    JudgeRecord,
    append_judgment,
    load_judgments,
    read_payload,
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
    ELO_BASE,
    ELO_K,
    KAPPA_CAVEAT,
    GateInvariantError,
    _KAPPA_CAVEAT_ASCII,
    ResumeRefused,
    _assert_rubric_gate,
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


def _run(root, tasks=None, *, complete=None, rng=None, **kw):
    complete = FakeComplete() if complete is None else complete
    rng = random.Random(1234) if rng is None else rng
    return judge_event_log(
        root, tasks if tasks is not None else [_task()],
        complete=complete, rng=rng, **kw
    )


def _lines(root):
    records, malformed = load_judgments(judgments_path(root))
    assert malformed == 0
    return records


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
        root, [_task()], complete=fake, rng=random.Random(7), rubric=True
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

    _run(root, rubric=True, votes=1)

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

    result = _run(root, rubric=True, votes=1)

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

    _run(root, rubric=False, votes=1)

    (line,) = _lines(root)
    assert (line.run_id_a, line.run_id_b) == ("run-a2", "run-b")


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

    _run(root, rubric=False, votes=1, sample_indices=[1])

    (line,) = _lines(root)
    assert line.sample_index == 1
    assert (line.run_id_a, line.run_id_b) == ("run-a1", "run-b1")


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

    _run(root, [_task("calc-1"), _task("calc-2")], rubric=False, votes=1,
         only_tasks=["calc-2"])

    (line,) = _lines(root)
    assert line.task_id == "calc-2"


# --------------------------------------------------------------------------
# 3. the vote protocol as the driver drives it
# --------------------------------------------------------------------------


def test_three_votes_are_three_independent_calls_with_three_prompts_recorded(
    tmp_path,
):
    """A single call asked to produce three opinions is one vote wearing three
    hats."""
    root = _two_arms(tmp_path)
    fake = FakeComplete()

    _run(root, complete=fake, rubric=False, votes=3)

    assert fake.calls == 3
    votes = sorted(j.vote_index for j in _lines(root))
    assert votes == [0, 1, 2]
    lines = _lines(root)
    assert len({j.judgment_id for j in lines}) == 3
    assert len({j.input_payload_path for j in lines}) == 3
    by_index = {j.vote_index: j for j in lines}
    for index, prompt in enumerate(fake.prompts):
        assert by_index[index].judge_prompt_sha == prompt_sha(prompt)


def test_position_assignment_stored_matches_the_payload_shown_order(tmp_path):
    """Stored, not derived. A stored assignment that disagrees with what was
    sent makes the position-swap probe report a bias figure it did not
    measure."""
    root = _two_arms(tmp_path)
    diffs = {"run-a": DIFF_A, "run-b": DIFF_B}

    _run(root, rng=random.Random(20260818), rubric=False, votes=8)

    lines = _lines(root)
    assert {j.position_assignment for j in lines} == {"a_first", "b_first"}
    for line in lines:
        payload = read_payload(line.input_payload_path)
        shown_first = payload["submission_first"]["diff"]
        expected = (
            diffs[line.run_id_a]
            if line.position_assignment == "a_first"
            else diffs[line.run_id_b]
        )
        assert shown_first == expected


def test_the_stored_verdict_is_canonical_not_presentation_order(tmp_path):
    """The model always answers "A" here, so a driver that skipped the
    inversion would record every verdict as "a"."""
    root = _two_arms(tmp_path)

    _run(root, rng=random.Random(20260818), rubric=False, votes=8,
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

    result = _run(root, complete=fake, rubric=False, votes=1)

    assert _lines(root) == []
    assert len(result["errors"]) == 1
    assert "MalformedVerdict" in result["errors"][0]

    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: FakeComplete(replies=[]),
    )
    assert main(["--event-log", str(root), "--no-rubric", "--votes", "1"]) == 1
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

    _run(root, rubric=True, votes=1)

    lines = _lines(root)
    (pairwise,) = [j for j in lines if j.kind == "pairwise"]
    assert pairwise.grade_version_seen == {"run-a": "2", "run-b": "3"}
    for line in lines:
        assert line.graded_against_task_set_commit == "taskset-def"
        assert line.judge_model_id == JUDGE_MODEL_ID_DEFAULT
        assert line.judge_prompt_version == JUDGE_PROMPT_VERSION
        assert line.rubric_version == RUBRIC_VERSION


def test_a_rubric_line_is_one_call_at_vote_index_zero(tmp_path):
    """Three votes are pairwise-only. The rubric is diagnostic and one call, as
    the spec's cost math assumes."""
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


def test_a_rubric_payload_that_cannot_be_written_leaves_no_line_behind(
    tmp_path,
):
    """`write_payload` BEFORE `append_judgment`. A line pointing at a payload
    that failed to write is a verdict naming an input nobody can read, which
    breaks "a re-judge is a re-score, not a re-run" (§4.3) exactly as a missing
    payload does -- while looking like a present one. Driven with a real secret
    so the refusal comes from `PayloadSecretsFound` rather than a stub."""
    root = _collection(
        tmp_path,
        [_record("run-a", final_diff=SECRET_DIFF)],
        [_grade("run-a")],
    )

    result = _run(root, rubric=True)

    assert _lines(root) == []
    assert len(result["errors"]) == 1
    assert "PayloadSecretsFound" in result["errors"][0]


def test_a_vote_payload_that_cannot_be_written_leaves_no_line_behind(tmp_path):
    """The same ordering on the path that carries most of the units.

    The rubric test above builds a single-run collection, so no pair forms and
    only `_rubric_line` runs -- which leaves the vote path, the one `--no-rubric`
    makes the ONLY path, unpinned. `_vote_line` has its own `write_payload` call
    and its own append, and a reorder there is invisible to every other test in
    this file: the verdict is well formed, the line looks complete, and the
    payload it names is not on disk.
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

    result = _run(root, rubric=False, votes=1)

    assert _lines(root) == []
    assert len(result["errors"]) == 1
    assert "PayloadSecretsFound" in result["errors"][0]


def test_payload_sits_beside_the_jsonl_and_its_sha_matches_the_record(tmp_path):
    """A verdict whose input cannot be reconstructed forces a re-collection to
    re-score (§4.3), and a sha that does not match its own input is
    indistinguishable from a tampered one."""
    root = _two_arms(tmp_path)

    _run(root, rubric=True, votes=1)

    lines = _lines(root)
    assert lines
    for line in lines:
        path = Path(line.input_payload_path)
        assert path.parent == payloads_root(root)
        assert path.exists()
        canonical = json.dumps(read_payload(path), sort_keys=True).encode()
        assert hashlib.sha256(canonical).hexdigest() == line.input_payload_sha


# --------------------------------------------------------------------------
# 4. resume
# --------------------------------------------------------------------------


def test_resume_skips_comparisons_already_judged_under_the_same_versions(
    tmp_path,
):
    root = _two_arms(tmp_path)
    first = _run(root, rubric=True, votes=3)
    written = len(_lines(root))
    fake = FakeComplete()

    second = _run(root, complete=fake, rubric=True, votes=3)

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
    _run(root, rubric=False, votes=1)

    second = _run(root, rubric=False, votes=1,
                  judge_model_id="openai.gpt-5.6-luna")

    lines = _lines(root)
    assert len(lines) == 2
    assert {j.judge_model_id for j in lines} == {
        JUDGE_MODEL_ID_DEFAULT, "openai.gpt-5.6-luna"
    }
    assert second["skipped"] == []


def test_re_judge_appends_new_lines_rather_than_replacing(tmp_path):
    """Judgments are append-only. The disagreement between two lines is the
    finding; an update API would delete the only evidence that the judge is not
    deterministic."""
    root = _two_arms(tmp_path)
    _run(root, rubric=True, votes=1)
    first = _lines(root)

    _run(root, rubric=True, votes=1, re_judge=True)

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
        _run(root, rubric=False, votes=1)
    assert str(path) in str(exc.value)

    result = _run(root, rubric=False, votes=1, re_judge=True)
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
            _run(root, rubric=False, votes=1, re_judge=re_judge)
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
    _run(root, rubric=False, votes=2)

    second = _run(root, rubric=True, votes=2)

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

    _run(root, rubric=True, votes=3)

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
        root, [_task()], rng=random.Random(3), rubric=False
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

    judge_event_log(root, [_task()], rng=random.Random(3), rubric=True, votes=3)

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

    result = _run(root, rubric=False, votes=1)

    assert [
        (j.task_id, j.sample_index, j.run_id_a, j.run_id_b)
        for j in result["judged"]
    ] == [
        ("calc-1", 0, "run-a", "run-b"),
        ("calc-1", 1, "run-c", "run-d"),
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


def test_main_passes_the_judge_model_and_vote_count_through(
    tmp_path, monkeypatch
):
    root = _two_arms(tmp_path)
    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion", lambda *a, **k: FakeComplete()
    )

    assert main([
        "--event-log", str(root), "--no-rubric",
        "--judge-model", "openai.gpt-5.6-terra", "--votes", "2",
    ]) == 0

    lines = _lines(root)
    assert len(lines) == 2
    assert {j.judge_model_id for j in lines} == {"openai.gpt-5.6-terra"}


# --------------------------------------------------------------------------
# 7. the summary -- a printout, not a stored score
# --------------------------------------------------------------------------
#
# These drive `summarize`/`elo_from_outcomes` over hand-built `JudgeRecord`s
# rather than through a batch. The aggregation rules being pinned are about
# lines that a REAL batch cannot produce in one invocation -- a stale
# gate-decided line sitting beside three votes for the same pair is what a
# re-grade leaves behind two passes apart -- and reaching them through the
# driver would mean staging two collections to assert one arithmetic rule.


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
    return _judgment(
        kind="pairwise", run_id_a=run_id_a, run_id_b=run_id_b,
        sample_index=sample_index, verdict=verdict, vote_index=vote_index,
        position_assignment="a_first",
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


def _generation(model, judge_model_id=JUDGE_MODEL_ID_DEFAULT,
                prompt_version=JUDGE_PROMPT_VERSION,
                rubric_version=RUBRIC_VERSION):
    """The key a rubric profile block sits under: the arm, and the judge
    generation that profiled it."""
    return (model, judge_model_id, prompt_version, rubric_version)


def _empty_result() -> dict:
    """A batch that judged nothing. The sharpest case for the caveat: an
    implementation that emitted it alongside a number would skip it here."""
    return {
        "judged": [], "gate_decided": [], "skipped": [], "errors": [],
        "warnings": [], "summary": summarize([], {}),
        "audited": 0, "from_prior_invocations": 0,
    }


def test_majority_over_vote_lines_with_gate_decided_wins_and_half_point_ties():
    """The whole outcome rule in one collection: three votes collapse to one
    comparison through `majority`, a gate-decided pair is a win for the side the
    ladder already picked, and a tie is half a point to each.

    Deliberately ASYMMETRIC -- 2.5 points against 1.5 -- because a matrix that
    reported x's rate under y's name is invisible against a balanced fixture.
    """
    judgments = [
        # settled by votes, a wins 2-1
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=0),
        _vote("run-a", "run-b", "b", vote_index=1, sample_index=0),
        _vote("run-a", "run-b", "a", vote_index=2, sample_index=0),
        # settled by votes, no strict majority -> tie
        _vote("run-a1", "run-b1", "a", vote_index=0, sample_index=1),
        _vote("run-a1", "run-b1", "b", vote_index=1, sample_index=1),
        _vote("run-a1", "run-b1", "tie", vote_index=2, sample_index=1),
        # settled by the ladder, one each way
        _gate("run-a2", "run-b2", "a", sample_index=2),
        _gate("run-a3", "run-b3", "b", sample_index=3),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][("model-one", "model-two")]
    assert row["voted"] == 2
    assert row["gate_decided"] == 2
    assert (row["wins_x"], row["wins_y"], row["ties"]) == (2, 1, 1)
    assert row["comparisons"] == 4
    assert row["win_rate_x"] == pytest.approx(2.5 / 4)
    assert row["win_rate_y"] == pytest.approx(1.5 / 4)


def test_a_comparison_short_of_a_strict_majority_is_a_tie_not_a_plurality_win():
    """`majority` is strict, and the summary must not launder a split decision
    into a win by counting votes itself."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "b", vote_index=1),
    ]

    row = summarize(judgments, MODEL_OF)["comparisons"][
        ("model-one", "model-two")
    ]

    assert (row["wins_x"], row["wins_y"], row["ties"]) == (0, 0, 1)


def test_vote_lines_supersede_a_stale_gate_decided_line_for_the_same_pair():
    """A re-grade that flips the losing side turns a gate-decided pair into a
    judgeable one, and the judgment file is APPEND-ONLY -- so the old
    gate-decided line stays on disk beside the three votes that came later.

    Counting both would enter one comparison twice, once for each side. The
    votes win, and the disagreement is TALLIED rather than hidden: it is a
    finding about the collection, not noise.
    """
    judgments = [
        _gate("run-a", "run-b", "b"),
        _vote("run-a", "run-b", "a", vote_index=0),
        _vote("run-a", "run-b", "a", vote_index=1),
        _vote("run-a", "run-b", "b", vote_index=2),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][("model-one", "model-two")]
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
        _vote("run-a", "run-b", "tie", vote_index=2),
        _gate("run-a2", "run-b2", "a", sample_index=2),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["vote_verdicts"] == {"a": 2, "tie": 1}
    assert "gate_decided" not in summary["vote_verdicts"]
    assert summary["lines"]["pairwise_votes"] == 3
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


def test_elo_is_deterministic_over_a_fixed_outcome_set():
    """Elo is order-dependent, and the outcome list is assembled from a dict
    walk. Sorted inside the function is what makes two passes over one
    unchanged collection print the same table -- otherwise the ratings move
    while nothing about the collection did."""
    outcomes = [
        ("model-one", "model-two", 1.0),
        ("model-two", "model-three", 0.5),
        ("model-one", "model-three", 0.0),
        ("model-two", "model-one", 1.0),
    ]

    first = elo_from_outcomes(outcomes)
    second = elo_from_outcomes(outcomes)
    shuffled = elo_from_outcomes(list(reversed(outcomes)))

    assert first == second
    assert first == shuffled
    assert list(first) == sorted(first)


def test_elo_starts_at_the_base_and_moves_by_k_on_an_even_first_match():
    """K=32 over a base of 1000, GDPval's parameters. Two unrated models are
    even, so the expectation is 0.5 and the first win moves exactly K/2.

    The LITERAL numbers, not `ELO_BASE ± ELO_K / 2`. Written against the
    constants, this test follows them wherever they go and says nothing about
    which parameters the table was computed at -- and a rating printed under an
    unrecorded K is a number nobody can reproduce.
    """
    assert (ELO_K, ELO_BASE) == (32.0, 1000.0)

    ratings = elo_from_outcomes([("model-one", "model-two", 1.0)])

    assert ratings["model-one"] == pytest.approx(1016.0)
    assert ratings["model-two"] == pytest.approx(984.0)
    # Zero-sum: Elo redistributes, it does not create rating.
    assert sum(ratings.values()) == pytest.approx(2000.0)


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

    row = summary["comparisons"][("alpha-model", "zeta-model")]
    assert (row["wins_x"], row["wins_y"], row["ties"]) == (0, 1, 0)
    assert row["win_rate_x"] == 0.0
    assert row["win_rate_y"] == 1.0
    assert summary["elo"]["zeta-model"] > summary["elo"]["alpha-model"]


def test_elo_refuses_a_score_that_is_not_a_win_a_loss_or_a_tie():
    """The only three outcomes a comparison has. A 3.0 slipped in from a vote
    COUNT rather than a result would inflate the table silently and by an amount
    nobody could reconstruct from the printout."""
    with pytest.raises(ValueError) as exc:
        elo_from_outcomes([("model-one", "model-two", 3.0)])
    assert "model-one" in str(exc.value)


def test_elo_follows_the_win_rates_rather_than_leading_them():
    """Descriptive, not a second opinion. The arm that won more comparisons
    outranks the one that won fewer."""
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=0),
        _vote("run-a1", "run-b1", "a", vote_index=0, sample_index=1),
        _vote("run-a2", "run-b2", "b", vote_index=0, sample_index=2),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][("model-one", "model-two")]
    assert row["win_rate_x"] > row["win_rate_y"]
    assert summary["elo"]["model-one"] > summary["elo"]["model-two"]


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

    assert set(summary["comparisons"]) == {("model-one", "model-two")}
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

    result = _run(root, rubric=False, votes=1)

    assert result["errors"] == []
    assert result["summary"]["dropped"]["unknown_model_comparison"] == 1
    assert result["summary"]["comparisons"][("model-one", "model-two")][
        "comparisons"
    ] == 1


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
    assert summary["comparisons"][("model-one", "model-two")][
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
    for flags in ([], ["--no-rubric"], ["--re-judge"], ["--votes", "1"],
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

    print_summary(_run(root, rubric=True, votes=1))

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
    _run(root, rubric=False, votes=1)

    second = _run(root, rubric=True, votes=1)

    # One vote line from the first invocation, two rubric lines from this one.
    assert second["audited"] == 3
    assert second["from_prior_invocations"] == 1
    summary = second["summary"]
    assert summary["lines"] == {
        "rubric": 2, "pairwise_votes": 1, "gate_decided": 0,
    }
    # The comparison the FIRST invocation judged is still in the matrix.
    assert summary["comparisons"][("model-one", "model-two")][
        "comparisons"
    ] == 1
    assert set(summary["rubric_profile"]) == {
        _generation("model-one"), _generation("model-two"),
    }

    print_summary(second)
    out = capsys.readouterr().out
    assert "3 judgment(s)" in out
    assert "1 from prior invocation(s)" in out


def test_the_summary_names_arms_for_runs_this_invocation_never_read(tmp_path):
    """The campaign rule has a name problem the grade summary does not: a
    judgment from an earlier pass can name a run that a later re-grade has since
    excluded, and `judge_event_log` drops excluded runs BEFORE reading them. The
    comparison is still in the file and still belongs in the matrix, so the name
    has to be recovered from the event log rather than from this pass's records.
    """
    root = _two_arms(tmp_path)
    _run(root, rubric=False, votes=1)

    # The re-grade that excludes one side, appended after the judgment.
    append_grade(
        grades_path(root),
        _grade("run-b", model="model-two", resolved=None, grader_version="3"),
    )
    second = _run(root, rubric=False, votes=1)

    row = second["summary"]["comparisons"][("model-one", "model-two")]
    assert row["comparisons"] == 1
    assert second["summary"]["dropped"]["unknown_model_comparison"] == 0


def test_the_summary_is_a_printout_and_stores_nothing(tmp_path):
    """`grade.py summarize`'s precedent, and the reason it matters more here: a
    stored Elo is a published number, and §4.3 forbids publishing one without
    the κ that was in force. Nothing on disk moves when the summary is taken."""
    root = _two_arms(tmp_path)
    result = _run(root, rubric=True, votes=1)
    before = _bytes_under(root)

    summarize(_lines(root), {"run-a": "model-one", "run-b": "model-two"})
    print_summary(result)

    assert _bytes_under(root) == before
