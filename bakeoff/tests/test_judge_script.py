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
"""

from __future__ import annotations

import hashlib
import json
import random
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
    prompt_sha,
)
from bakeoff.judge_schema import load_judgments, read_payload
from bakeoff.schema import (
    SCHEMA_VERSION,
    Artifacts,
    Outcome,
    RunRecord,
    TerminationReason,
)

from scripts.grade import grades_path
from scripts.judge import (
    GateInvariantError,
    ResumeRefused,
    _assert_rubric_gate,
    gating_view,
    judge_event_log,
    judgments_path,
    main,
    payloads_root,
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


def test_a_payload_that_cannot_be_written_leaves_no_line_behind(tmp_path):
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
