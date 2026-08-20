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
import os
import random
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff.eventlog import EventLog
from bakeoff.grade_schema import CheckResult, GradeRecord, append_grade
from bakeoff.judge import (
    ALLOW_NON_NEUTRAL_JUDGE_ENV,
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    RUBRIC_DIMENSIONS,
    RUBRIC_FLAGS,
    RUBRIC_VERSION,
    VOTE_POSITIONS,
    NonNeutralJudge,
    _CANONICAL_VERDICTS,
    is_auth_failure,
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

import scripts.judge as judge_script
from scripts.grade import grades_path
from scripts.judge import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    ELO_ANCHOR,
    ELO_SCALE,
    KAPPA_CAVEAT,
    MAX_CONSECUTIVE_ERRORS,
    CollectionNotFound,
    GateInvariantError,
    _KAPPA_CAVEAT_ASCII,
    _MAX_NAMED,
    ResumeRefused,
    StorageFailure,
    _assert_rubric_gate,
    _comparison_key,
    _generation_of,
    _judge_generation,
    _print_kappa_caveat,
    _recentred,
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
# 0. the judge's own family
#
# Numbered 0 because it happens before everything below it: §4.3's neutral
# family is checked on the ARGUMENT, before the collection is opened, before a
# credential is minted and before the resume can skip a single unit. The unit
# behaviour of `assert_neutral_judge` is `test_judge.py`'s; what these two pin
# is the wiring -- that the driver calls it first, that `main` answers a
# refusal with a sentence instead of a traceback, and that the override's
# warning reaches BOTH the result and the terminal.
# --------------------------------------------------------------------------


CLAUDE_JUDGE = "anthropic.claude-sonnet-5"


def test_a_non_neutral_judge_is_refused_before_the_collection_is_even_read(
    tmp_path, monkeypatch, capsys
):
    """The refusal is about the COMMAND, so nothing on disk can change it.

    A judge from a compared family is the one failure this driver cannot
    detect after the fact: the payloads are clean, the verdicts parse, the
    matrix fills in and every number in it is shifted the same way. So the
    guard runs on the argument, ahead of the `CollectionNotFound` check that
    used to be the first statement -- and the ordering is asserted with a path
    holding no `runs/` directory, where a guard placed second would answer with
    the wrong refusal and let the operator fix the path and re-run into the
    same biased pass.

    Nothing is created, nothing is asked, and `main` turns it into a sentence
    and exit 1 rather than a traceback, on the `ResumeRefused` precedent.

    And there is NO FLAG, which is the κ caveat's philosophy applied to the
    other unconditional rule in this file: a flag is how a mandatory rule
    becomes a default, so the override is an environment variable an operator
    has to type on purpose and cannot leave in a shell script by habit.
    """
    monkeypatch.delenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, raising=False)
    root = _two_arms(tmp_path)
    fake = FakeComplete()

    with pytest.raises(NonNeutralJudge):
        _run(root, complete=fake, judge_model_id=CLAUDE_JUDGE)

    assert fake.calls == 0
    assert not judgments_path(root).exists()

    # No `runs/` here, so `CollectionNotFound` is the refusal a guard placed
    # after it would raise. The family is checked first.
    with pytest.raises(NonNeutralJudge):
        _run(tmp_path / "not-a-collection", judge_model_id=CLAUDE_JUDGE)

    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )
    assert main([
        "--event-log", str(root), "--judge-model", CLAUDE_JUDGE,
    ]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "4.3" in out

    with pytest.raises(SystemExit):
        main(["--help"])
    help_text = capsys.readouterr().out.lower()
    # The sweep's own tripwire, for the reason the caveat's sweep has one: four
    # `not in` assertions pass perfectly on an EMPTY string, so anything that
    # stopped the help text reaching this capture -- argparse writing to
    # stderr, a `SystemExit` raised before it formats anything -- would leave
    # them asserting nothing while staying green. `--judge-model` is the flag
    # this test is about, so its presence is what proves the sweep was read
    # against a help text that arrived.
    assert "--judge-model" in help_text
    for banned in ("--allow-non-neutral", "--non-neutral", "--any-judge",
                   "--no-judge-check"):
        assert banned not in help_text


def test_the_override_warning_reaches_the_result_and_the_terminal_both(
    tmp_path, monkeypatch, capsys
):
    """`BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE=1` admits the judge and says so twice.

    Two destinations because they answer two different readers. `warnings` is
    what a caller prints beside the matrix and what a later reader of this
    result finds attached to the numbers; the terminal line is what the
    operator sees while the pass is still running, next to the census, before
    thousands of paid calls have been made against a judge that will inflate
    one of the arms being compared.

    The batch does RUN -- that is what "admits" means, and a probe of judge
    self-preference is a real experiment -- so the lines are asserted present.
    A warning attached to an empty batch would be a refusal wearing a
    warning's clothes.
    """
    monkeypatch.setenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, "1")
    root = _two_arms(tmp_path)

    result = _run(root, rubric=False, judge_model_id=CLAUDE_JUDGE)

    (loud,) = [w for w in result["warnings"] if w.startswith("NON-NEUTRAL")]
    assert CLAUDE_JUDGE in loud
    assert loud in capsys.readouterr().out
    assert len(_lines(root)) == 2
    assert {j.judge_model_id for j in _lines(root)} == {CLAUDE_JUDGE}
    assert result["errors"] == []


def test_the_override_banner_survives_a_terminal_that_cannot_encode_it(
    tmp_path, monkeypatch
):
    """`print_summary`'s WARNING loop re-emits the banner, `§` and all.

    The loop sits BELOW the `try/finally` that protects the caveat and the
    spend line, so an unguarded `print` there raises `UnicodeEncodeError` on a
    stdout the environment pinned to ASCII (`LC_ALL=C`, or a pipe into a tool
    that did) and the WARNING section -- and the error section under it --
    never appear at all. The section that goes missing is the one saying the
    numbers above it came from a judge inside the compared families: the
    operator is left reading a complete, clean, caveated report of a biased
    pass, with the one line that would have told them otherwise removed by the
    crash that was supposed to be about encoding.

    The loop carries collection-derived text besides the banner -- the excluded
    and ungraded warnings name run ids, the abort names task ids and model
    names -- so the guard covers more than the warning that exposed it.

    `_print_reading` IS STUBBED so that this test is about THIS loop. It prints
    two `§` legends of its own through bare `print`s and raises before the loop
    is reached on a strict stream, which would mask it entirely and leave this
    test pinning another defect under this one's name.

    That stub is also the honest scope of the assertion: `print_summary` as a
    LIBRARY entry point, where the caller owns the stream and today's semantics
    are unchanged. The CLI no longer has the problem at all -- `main`
    reconfigures stdout to `backslashreplace` before the summary, and
    `test_an_ascii_terminal_gets_the_whole_report_of_an_override_pass` is the
    end-to-end proof. This loop keeps its own per-line guard regardless,
    because a library caller reaches it on a stream nobody here reconfigured.

    Escaped rather than dropped, and asserted as such: `\\xa7` is greppable
    back to the sentence, and a guard that swallowed the line would leave the
    pass alive and the warning invisible -- this failure wearing a different
    hat.
    """
    monkeypatch.setenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, "1")
    root = _two_arms(tmp_path)
    monkeypatch.setattr("scripts.judge._print_reading", lambda result: None)
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    result = _run(root, rubric=False, judge_model_id=CLAUDE_JUDGE)
    print_summary(result)
    stream.flush()

    text = stream.buffer.getvalue().decode("ascii")
    assert len(_lines(root)) == 2, "the batch died on a printout"
    assert "kappa unmeasured" in text
    # The section that used to be missing, and the sentence inside it.
    assert "WARNING: NON-NEUTRAL JUDGE ADMITTED" in text
    assert CLAUDE_JUDGE in text
    assert "\\xa74.3" in text


def test_an_ascii_terminal_gets_the_whole_report_of_an_override_pass(
    tmp_path, monkeypatch
):
    """END TO END, through `main`, on a stdout pinned to ASCII. Nothing raises.

    This is the criterion the per-line guards could not meet on their own.
    `_print_reading` prints two `§` legends through bare `print`s -- the
    interval legend and the 40% pairwise threshold -- and on a strict ASCII
    stream it died PARTWAY THROUGH the report, after the pass had spent its
    money and fsynced every line. Guarding those sites one at a time was
    rejected as too wide, so `main` sets the STREAM's error handler instead:
    every report line, the ones written today and the ones added later,
    degrades into readable escapes rather than raising.

    The pass under test is the one where losing the report costs most. An
    override run's WARNING section is the only thing saying the numbers above
    it came from a judge inside the compared families, it prints LAST, and a
    crash anywhere above it takes it out -- leaving a complete, clean, caveated
    reading of a biased pass and nothing to say so.

    Every section is asserted, in the order a reader meets them, because the
    old failure was partial output and a test that checked only the last line
    would pass against a report that lost its middle. One unit is deliberately
    failed so the ERROR section is real rather than skipped, which also makes
    the exit code 1 -- from the errors, and not from a traceback.

    THE κ CAVEAT IS ASSERTED IN ITS ASCII TWIN, and that is the subtle half.
    Under `backslashreplace` the caveat's own `print` no longer raises, so an
    exception-keyed fallback would never fire and the one sentence in this file
    that must be READ rather than grepped would arrive as
    `\\u03ba unmeasured (OPEN-5) \\u2014 ...`. `_encodable` is what keeps the
    twin reachable: escaping is right for an id and wrong for a claim.

    THE COLLECTION SITS UNDER A NON-ASCII DIRECTORY, which is what reaches the
    HEADER -- the five lines `main` prints before the collection is read and
    before a credential is minted. Those are ABOVE the reconfigure, so the
    widened error handler does not cover them, and four of them carry a path:
    `--event-log`, the two paths derived from it, and `--taskset` as the
    operator typed it. Unguarded, a collection under an accented directory --
    ordinary, not exotic -- takes `main` down on a traceback at hour zero,
    above the census, having named nothing. Asserted FIRST and in printing
    order for the same reason the rest of this test is: a guard covering only
    the report still loses everything above it.
    """
    monkeypatch.setenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, "1")
    root = _two_arms(tmp_path / "café")
    # One vote answered, the rest unparsable: a real ERROR section, and a
    # second unit that leaves its hole in the derived view.
    _cli(monkeypatch, complete=FakeComplete(replies=[_pairwise_reply("A")]))
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    code = main([
        "--event-log", str(root), "--no-rubric",
        "--taskset", "tâches.yaml",
        "--judge-model", CLAUDE_JUDGE,
    ])
    stream.flush()

    text = stream.buffer.getvalue().decode("ascii")
    assert code == 1, "exit 1 from the errored unit, not from a traceback"
    assert len(_lines(root)) == 1, "the pass died before writing its line"

    # The header, printed above the reconfigure and before anything is spent.
    assert "event log  " in text
    assert "caf\\xe9" in text          # escaped, so the path is still greppable
    assert "grades     " in text       # derived from the same root, same guard
    assert "judgments  " in text       # and so is this one
    assert "t\\xe2ches.yaml" in text   # the operator's own --taskset, echoed back

    # Then the whole report, in the order it prints.
    assert "judged" in text and "errored" in text     # the invocation counts
    assert "pairwise win rates" in text               # the matrix section
    assert "\\xa74.4" in text                         # a legend that used to kill it
    assert "\\xa710.1" in text                        # and the second one
    assert "kappa unmeasured" in text                 # the twin, not the escapes
    assert "\\u03ba" not in text
    assert "WARNING: NON-NEUTRAL JUDGE ADMITTED" in text
    assert "produced NO line" in text                 # the error section, last


def test_main_reconfigures_only_a_stream_that_offers_it(tmp_path, monkeypatch):
    """A stdout with no `reconfigure` is skipped, not crashed on.

    `sys.stdout` is not always a `TextIOWrapper`: a `StringIO`, a capture
    object, an embedding host's stream. Reaching for `reconfigure`
    unconditionally would turn the fix for a printing failure into an
    `AttributeError` on the line above the report -- the same failure it was
    placed against, one line earlier and on every stream rather than only the
    ASCII ones.

    Skipping is safe because nothing depends on it: the reconfigure widens what
    the report can print, and a stream that cannot be widened is one whose own
    error handler already applies. The driver's own prints keep
    `_print_ascii_safe` either way.
    """
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)
    _cli(monkeypatch)
    stream = io.StringIO()
    assert not hasattr(stream, "reconfigure"), "fixture no longer proves anything"
    monkeypatch.setattr(sys, "stdout", stream)

    code = main(["--event-log", str(root), "--no-rubric"])

    assert code == 0
    assert "pairwise win rates" in stream.getvalue()


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


# --- what the operator is told about what did NOT enter -----------------------
#
# Four warnings, and each one is the only signal for a mistake that otherwise
# reads as a smaller collection. A batch that silently judges 40 of 60 tasks
# reports a clean pass over the 40, and every rate in it is computed over a
# denominator nobody chose.


def test_orphan_grade_lines_are_warned_with_the_copied_file_diagnosis(tmp_path):
    """A grade line naming a run this log does not hold, with the CAUSE named.

    Run ids are `sha256(task|model|sample|attempt)[:16]` and unique within a
    collection, not across one, so an id in `grades.jsonl` that no run answers
    to almost always means the grade file came from somewhere else -- a copy, a
    restored backup, a `--event-log` pointed one directory over. The warning
    says that, because "3 grade line(s) name a run this event log does not
    hold" on its own reads as data loss and sends an operator looking for
    missing runs that were never here.

    Silence is the failure this replaces: the orphans simply do not appear in
    any cell, so the batch judges whatever DOES match -- possibly nothing --
    and reports a clean pass over it.
    """
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
        ],
        [
            _grade("run-a", model="model-one"),
            _grade("run-b", model="model-two"),
            _grade("run-ghost", model="model-three"),
        ],
    )

    result = _run(root, rubric=False)

    (warning,) = [w for w in result["warnings"] if "run-ghost" in w]
    assert "1 grade line(s)" in warning
    assert "copied from another collection" in warning
    assert result["errors"] == []


def test_the_excluded_warning_counts_and_names_the_dropped_runs(tmp_path):
    """`resolved is None` rows are dropped before pairing, and SAID SO.

    Five such rows exist in `trucking-pilot-v2`, and an excluded row is not an
    observation of the model -- ranking it ranks the infrastructure. But the
    drop is invisible from the output: the arm simply has fewer comparisons,
    which reads as a smaller sample rather than as rows that were removed. The
    count and the ids are what let an operator decide whether the exclusions
    are spread across arms or sitting entirely on one, which is the difference
    between a narrower interval and a biased one.
    """
    root = _collection(
        tmp_path,
        [
            _record("run-a", model="model-one", final_diff=DIFF_A),
            _record("run-b", model="model-two", final_diff=DIFF_B),
            _record("run-c", model="model-three", final_diff=DIFF_C),
            _record("run-d", model="model-four", final_diff=DIFF_D),
        ],
        [
            _grade("run-a", model="model-one"),
            _grade("run-b", model="model-two"),
            _grade("run-c", model="model-three", resolved=None),
            _grade("run-d", model="model-four", resolved=None),
        ],
    )

    result = _run(root, rubric=False)

    (warning,) = [w for w in result["warnings"] if "excluded" in w]
    assert "2 graded run(s)" in warning
    assert "run-c, run-d" in warning
    assert "resolved is null" in warning


def test_only_task_ids_absent_from_the_log_are_warned_not_errored(tmp_path):
    """A named task with no judgeable run is a WARNING, and the exit stays 0.

    The exit contract is about units that were selected and exist, so an id
    that selected nothing errors nothing -- but the usual cause is a pointer at
    the wrong collection, where EVERY id is missing and the batch reports a
    clean zero. The warning is the only thing between that and a satisfied
    operator, and it names the ids so a typo is visible as a typo.

    Only the absent id is named: an id that DID select work is not a problem,
    and a warning listing every requested task would be a warning nobody reads.
    """
    root = _collection(
        tmp_path,
        [
            _record("run-a", task_id="calc-1", model="model-one"),
            _record("run-b", task_id="calc-1", model="model-two",
                    final_diff=DIFF_B),
        ],
        [
            _grade("run-a", task_id="calc-1", model="model-one"),
            _grade("run-b", task_id="calc-1", model="model-two"),
        ],
    )

    result = _run(root, [_task("calc-1"), _task("calc-9")], rubric=False,
                  only_tasks=["calc-1", "calc-9"])

    (warning,) = [w for w in result["warnings"] if "--only-task" in w]
    assert "1 task(s)" in warning
    assert "calc-9" in warning
    assert "calc-1" not in warning
    assert result["errors"] == []
    assert len(_lines(root)) == 2


def test_a_task_missing_from_the_task_set_is_warned_and_its_cells_skipped(
    tmp_path,
):
    """A graded task absent from the loaded task set is skipped, and NAMED.

    The payload is anchored on the manifest's prompt and solution diff, so
    there is nothing to judge a submission against -- the cell cannot be
    judged, and the driver walks past it. What it must not do is walk past it
    quietly: a task set at the wrong commit, or one task folder missing, drops
    those cells out of every rate in the summary while the pass exits 0 and
    prints a matrix that looks complete.

    Both halves are asserted, because they fail apart: a driver that warned and
    then judged anyway would build a payload against another task's manifest,
    and one that skipped without warning is the silence above.
    """
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
    fake = FakeComplete()

    result = _run(root, [_task("calc-1")], complete=fake, rubric=False)

    (warning,) = [w for w in result["warnings"] if "task set" in w]
    assert "1 task(s)" in warning
    assert "calc-2" in warning
    assert {j.task_id for j in _lines(root)} == {"calc-1"}
    # Skipped rather than asked about: two votes for calc-1 and nothing else.
    assert fake.calls == 2
    assert result["errors"] == []


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


# --- the evidence fields: what a line PROVES about the call it names ----------
#
# `judge_prompt_sha` and `judge_sampling` are the two fields that attest to a
# request. Everything else on a line is a claim; these two are the receipt, and
# a receipt for a request nobody made is worse than none -- it is what makes a
# fabricated line indistinguishable from a real one when the file is read in
# bulk months later.


def test_a_gate_decided_line_carries_an_empty_sha_and_an_empty_sampling_block(
    tmp_path,
):
    """No call was made, so both as-sent fields are EMPTY rather than copied.

    `judge_prompt_sha` and `judge_sampling` are populated from constants on
    every other kind of line, which is exactly why they are the two easiest to
    fill in here by habit -- `dict(JUDGE_SAMPLING)` is already on the line
    above in both siblings. A gate-decided pair rendered no prompt and sent no
    request, so a sha would attest to text that never existed and a sampling
    block would describe a call nobody made. Read in bulk, that line is
    indistinguishable from a vote.

    The identity fields ARE populated beside them, and the contrast is the
    point: `judge_model_id`, `judge_prompt_version` and `rubric_version` are
    the resume key, so a later pass can see this unit is done. They say who
    WOULD have been asked; the two above would say what was sent.
    """
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)

    _run(root, rubric=False)

    (line,) = _lines(root)
    assert line.verdict == "gate_decided"
    assert line.judge_prompt_sha == ""
    assert line.judge_sampling == {}
    # The resume identity, populated, on the same line.
    assert line.judge_model_id == JUDGE_MODEL_ID_DEFAULT
    assert line.judge_prompt_version == JUDGE_PROMPT_VERSION
    assert line.rubric_version == RUBRIC_VERSION


def test_a_rubric_line_stores_the_sha_of_the_prompt_it_actually_sent_and_the_sampling_as_sent(  # noqa: E501
    tmp_path,
):
    """The sha is of the text the model saw, carried out of `judge_rubric`.

    Rebuilding the prompt at record-assembly time produces the same bytes
    today and is the shape that lets them differ tomorrow -- a renderer that
    grew a timestamp, a payload re-serialized in another key order, a retry
    that re-rendered. The record would still hold a 64-hex digest, still look
    like evidence, and attest to a prompt nobody sent. `judge_prompt_sha` is
    the only thing that makes `judge_prompt_version` an honest declaration
    rather than a label, so it is asserted against the prompt the SEAM saw and
    not against anything the driver could recompute.

    The sampling block is asserted as a COPY, and asserted on the IN-MEMORY
    record rather than on the line read back: a `JudgeRecord` reconstructed
    from JSON holds a fresh dict whatever the driver did, so the same
    assertion over `_lines(root)` is one that is always true. Storing the
    module constant itself would hand every record in the campaign one shared
    dict, where a caller normalizing a value on one silently rewrites what
    every other record claims to have sent.
    """
    root = _collection(tmp_path, [_record("run-a")], [_grade("run-a")])
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=True)

    (line,) = _lines(root)
    (sent,) = fake.prompts
    # The prompt the model saw, identified by content rather than by index
    # alone: a sha over an empty string or over the payload is also 64 hex.
    assert "Fix the adder in calc-1." in sent
    assert line.judge_prompt_sha == prompt_sha(sent)
    assert line.judge_prompt_sha != prompt_sha("")

    assert line.judge_sampling == dict(JUDGE_SAMPLING)
    (built,) = result["judged"]
    assert built.judge_sampling == dict(JUDGE_SAMPLING)
    assert built.judge_sampling is not JUDGE_SAMPLING


def test_a_vote_line_records_the_sampling_block_as_sent(tmp_path):
    """Both votes carry the sampling the call went out under, each its own copy.

    Temperature 0 is what makes two forced positions the whole of the signal --
    a comparison judged at temperature 1 is two draws rather than two positions,
    and the position-consistency figure taken off it measures sampling noise
    instead of position bias. The stored block is the only place a reader can
    check that after the fact, and a reader who finds it empty cannot tell a
    temperature-0 pass from a temperature-1 one.

    The cap is spelled `max_completion_tokens` because that is the name it left
    under: the candidate arms reach that spelling through a litellm patch this
    module deliberately never imports, so a line recording `max_tokens` would
    describe a call that went out uncapped.

    The copy check runs over the records the DRIVER built, not the ones read
    back: a `JudgeRecord` reconstructed from a JSON line holds a fresh dict
    whatever the driver did, so `is not` over `_lines(root)` asserts nothing.
    """
    root = _two_arms(tmp_path)

    result = _run(root, rubric=False)

    lines = _lines(root)
    assert len(lines) == 2
    for line in lines:
        assert line.judge_sampling == dict(JUDGE_SAMPLING)
        assert line.judge_sampling["temperature"] == 0.0
        assert "max_completion_tokens" in line.judge_sampling
        assert "max_tokens" not in line.judge_sampling

    first, second = result["judged"]
    assert first.judge_sampling is not JUDGE_SAMPLING
    # Two records, two dicts: one shared object would make a later
    # normalization of either rewrite both -- and the campaign's whole file.
    assert first.judge_sampling is not second.judge_sampling


def test_a_rubric_line_names_exactly_the_one_grade_generation_it_was_gated_on(
    tmp_path,
):
    """One entry, for its own run -- not the pair's two, and not none.

    A verdict is only interpretable against the grade generation it saw: a
    re-grade under a new `GRADER_VERSION` can flip `resolved`, which changes
    which runs are eligible for a rubric call at all. `grade_version_seen` is
    what lets a later reader tell a profile scored under grader 2 from one
    scored under grader 3, and an empty dict there is not "unknown" -- it reads
    as a line nobody can date, sitting in an append-only file beside lines that
    can be.

    Exactly one key, because a rubric is about one run. The pairwise sibling
    carries two and that difference is the schema's, not an accident of which
    dict was in scope.
    """
    root = _collection(
        tmp_path,
        [_record("run-a")],
        [_grade("run-a", grader_version="7")],
    )

    _run(root, rubric=True)

    (line,) = _lines(root)
    assert line.kind == "rubric"
    assert line.grade_version_seen == {"run-a": "7"}


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


def _cli(monkeypatch, tasks=None, complete=None):
    """Wire `main` to a task set and a seam without touching the network.

    The three tests below are about ARGPARSE-TO-DRIVER wiring and nothing else,
    so both of `main`'s outside edges are replaced: `load_task_set` (which would
    otherwise read `taskset/` off disk) and `live_completion` (which would mint
    a credential). What is left is exactly the mapping from a flag to a keyword
    argument -- which is untested code with three real ways to be silently
    wrong, since a flag wired to nothing produces a batch that runs, exits 0 and
    quietly judges the wrong set of units.
    """
    monkeypatch.setattr(
        "scripts.judge.load_task_set", lambda *a, **k: tasks or [_task()]
    )
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: complete if complete is not None else FakeComplete(),
    )


def test_re_judge_from_the_command_line_appends_rather_than_skips(
    tmp_path, monkeypatch
):
    """`--re-judge` has to reach `re_judge`, or the resume silently wins.

    The flag's whole job is to defeat the resume, and a flag wired to nothing
    fails in the direction that looks like success: every unit is already
    judged, so every unit is skipped, the pass exits 0 in a second and prints a
    summary computed off the OLD verdicts. An operator re-judging a collection
    after a prompt fix would read that as confirmation rather than as a batch
    that did nothing.
    """
    root = _two_arms(tmp_path)
    _cli(monkeypatch)

    assert main(["--event-log", str(root), "--no-rubric"]) == 0
    first = _lines(root)
    assert len(first) == 2

    assert main([
        "--event-log", str(root), "--no-rubric", "--re-judge",
    ]) == 0

    second = _lines(root)
    assert len(second) == 4
    # Appended, not replaced: the first two lines are still the first two.
    assert [j.judgment_id for j in second[:2]] == [
        j.judgment_id for j in first
    ]


def test_samples_from_the_command_line_select_exactly_the_named_indices(
    tmp_path, monkeypatch
):
    """`--samples 1` judges sample 1 and nothing else.

    Subsampling SAMPLES is §4.2.3's answer to a binding cost budget -- dropping
    pairs breaks the Elo graph's connectivity, while subsampling only widens
    the intervals -- so this flag is the one an operator reaches for when the
    money is real. Wired to nothing it judges every sample, which is the
    opposite of the request and is discovered on the invoice.

    `type=int` matters here too: the driver compares against
    `record.sample_index`, an int, so a string "1" from argparse would select
    nothing at all and report a clean pass over zero units.
    """
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
    _cli(monkeypatch)

    assert main([
        "--event-log", str(root), "--no-rubric", "--samples", "1",
    ]) == 0

    assert {
        (j.sample_index, j.run_id_a, j.run_id_b) for j in _lines(root)
    } == {(1, "run-a1", "run-b1")}


def test_only_task_from_the_command_line_reaches_the_selection(
    tmp_path, monkeypatch
):
    """`--only-task calc-2` judges calc-2 and leaves calc-1 unbought.

    This is the flag the breaker's own abort message tells an operator to
    reach for -- a collection holding units that fail deterministically aborts
    every resume in the same place, and narrowing the selection is one of the
    two documented ways out. A flag that did not reach the selection would make
    that advice wrong, at the moment somebody is following it to get a stuck
    campaign moving.
    """
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
    _cli(monkeypatch, tasks=[_task("calc-1"), _task("calc-2")])

    assert main([
        "--event-log", str(root), "--no-rubric", "--only-task", "calc-2",
    ]) == 0

    lines = _lines(root)
    assert {j.task_id for j in lines} == {"calc-2"}
    assert len(lines) == 2


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

    Under the two-phase walk the separation is structural rather than
    incidental: skips are resolved in phase 1 and never enter the worklist at
    all, so the breaker cannot see one even in principle. `skipped` is
    therefore the whole selection -- every unit this pass found already bought
    -- rather than the prefix the walk got through before it aborted. That is
    the honest number and the one the census prints: how much of the
    collection is done does not depend on where an unrelated failure stopped
    the pass.
    """
    root = _four_arms(tmp_path)
    first = _DiesAfter(succeed_at=set(range(2, 17, 2)))
    _run(root, complete=first, rubric=True, max_consecutive_errors=100)
    assert len(_lines(root)) == 8, "the first pass did not judge half the cell"

    second = _DiesAfter()
    result = _run(root, complete=second, rubric=True)

    # Sixteen units in the cell; the first pass bought eight, so eight are
    # skipped and eight are selected. Five of those eight are attempted and
    # fail, which is where the breaker stops the pass.
    assert second.calls == MAX_CONSECUTIVE_ERRORS
    assert len(result["skipped"]) == 8, "the failures were not separated"
    assert result["selected"]["units"] == 8
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

    The paragraph names TWO shapes, and both halves are pinned here, because
    "not auth" is not the same as "deterministic": `is_auth_failure` is
    401/403 only, so a rate-limit or quota window lands in this paragraph too,
    and on a pass of thousands of calls it is the likelier arrival. An
    unconditional "these units fail deterministically" sends that operator to
    `--only-task` to exclude a task that is fine, when the fix is to wait and
    run the same command again. So the transient fix is offered under a
    rate-limit condition, and the futile-resume warning under a data-shaped
    one -- what must never come back is the flat determinism claim.
    """
    root = _three_arms(tmp_path)
    fake = _DiesAfter(error=_UnreadableDiff)

    abort = _abort_of(_run(root, complete=fake, rubric=True))

    assert "will fail again on resume" in abort
    assert "--only-task" in abort
    assert "--max-consecutive-errors" in abort
    assert "credential" not in abort
    # The transient half, conditioned on the error being rate-limit shaped.
    assert "Rate-limit" in abort
    assert "wait for the window to clear and resume with the same command" in (
        abort
    )
    # The overclaim itself, in either phrasing.
    assert "fail deterministically" not in abort
    assert "these units fail" not in abort


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
# 6c. what a forty-hour pass looks like from the terminal
# --------------------------------------------------------------------------
#
# The failure these are placed against is not a wrong number: it is a pass that
# is RIGHT and unusable. A full batch is ~9,600 paid calls over many more hours
# than a credential lives, and before this section the driver printed five
# header lines and then nothing at all until the summary -- so an operator
# could not tell a working pass from a hung one, could not tell what the pass
# had committed to spending before it started spending, and lost even the
# summary to a Ctrl-C. Three of the five things below are about a batch that
# must STOP: a typo that would otherwise be created empty and reported as a
# clean pass over zero units, a disk that has stopped accepting writes while
# the driver goes on buying verdicts, and an interrupt.

#: One progress line, as an operator reads it. The durations are matched by
#: SHAPE rather than by value: they are wall-clock over a real batch, so an
#: exact assertion would either pin the machine this ran on or need a frozen
#: clock threaded through the driver to say less than this does.
_PROGRESS = re.compile(
    r"^\[(?P<index>\d+)/(?P<total>\d+)\] (?P<label>.+?) "
    r"(?P<status>ok|ERROR .+?) "
    r"\(unit \d+\.\d+s, elapsed (?:\d+h)?(?:\d+m)?\d+s\)$"
)

#: `bakeoff/`, which is what `pythonpath = ["."]` puts on the path for this
#: suite and therefore what the import probe below has to hand a subprocess.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _progress_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[")]


def test_a_typoed_event_log_is_refused_before_anything_is_created_on_disk(
    tmp_path, monkeypatch, capsys
):
    """A mistyped `--event-log` is a typo, not an empty collection.

    `EventLog.__init__` mkdirs `runs/`, so the old driver CREATED the typo,
    found no runs in it, judged nothing and printed a complete, triumphant
    report with an exit status of 0. Every number in it was zero and none of
    them was wrong, which is what made it unreadable as a failure -- and the
    directory it left behind is a second collection root that looks real to
    the next command that points at it.

    Refused BEFORE any mkdir, so the check is on the absence of the directory
    rather than on the message: a driver that reported the typo after creating
    it would satisfy every assertion about the text.
    """
    _two_arms(tmp_path)  # the collection that was meant
    typo = tmp_path / "evenlog"
    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )

    assert main(["--event-log", str(typo)]) == 1

    assert not typo.exists(), "the typo was created rather than refused"
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert str(typo) in out
    # What a collection root looks like, so the operator can compare.
    assert "runs/" in out

    with pytest.raises(CollectionNotFound):
        judge_event_log(typo, [_task()], complete=FakeComplete())
    assert not typo.exists()


def test_the_census_is_printed_before_the_first_paid_call(tmp_path, capsys):
    """What the pass has committed to spending, said before it spends any of it.

    Ordering is the whole content of this test, so it is asserted from INSIDE
    the seam: the completion records what was on the terminal at the moment it
    was first asked for a verdict, and the census has to already be in it. A
    census printed beside the summary would pass every assertion about its
    text and answer none of the question it exists for, which is "may I let
    this run".
    """
    root = _three_arms(tmp_path)
    before: list[str] = []

    def _note_the_terminal(prompt: str) -> None:
        if not before:
            before.append(capsys.readouterr().out)

    result = _run(
        root, complete=FakeComplete(on_call=_note_the_terminal), rubric=True
    )

    assert before, "no call was made, so the ordering was never exercised"
    assert (
        "selected: 3 rubric, 6 votes over 3 pairs, 0 gate-decided; "
        "0 already judged (skipped)"
    ) in before[0]
    assert result["selected"] == {
        "rubric": 3, "votes": 6, "pairs": 3, "gate_decided": 0,
        "skipped": 0, "units": 9,
    }


def test_every_attempted_unit_prints_a_progress_line_naming_task_model_and_sample(
    tmp_path, capsys
):
    """One line per attempted unit, in the fields the flags take.

    `_unit_label`'s reasoning, at the one place it is read on a HEALTHY pass:
    task and sample first because `--only-task` and `--samples` want them, the
    arms next, the run ids last. A progress line naming only a counter would
    say the pass is alive and nothing about where it is.
    """
    root = _two_arms(tmp_path)

    _run(root, rubric=True)

    lines = _progress_lines(capsys.readouterr().out)
    assert len(lines) == 4, "two rubric units and one comparison's two votes"
    parsed = [_PROGRESS.match(line) for line in lines]
    assert all(parsed), lines
    assert [m["index"] for m in parsed] == ["1", "2", "3", "4"]
    assert {m["total"] for m in parsed} == {"4"}
    assert [m["status"] for m in parsed] == ["ok"] * 4
    for match in parsed:
        assert "task=calc-1" in match["label"]
        assert "sample=0" in match["label"]
    assert parsed[0]["label"].startswith("rubric ")
    assert "model-one" in parsed[0]["label"]
    assert "model-two" in parsed[1]["label"]
    assert parsed[2]["label"].startswith("vote 0 (a_first) ")
    assert "model-one vs model-two" in parsed[2]["label"]


def test_skipped_units_print_one_census_line_not_five_thousand(
    tmp_path, capsys
):
    """A resume is skip-heavy by construction, and a skip is not progress.

    The pass that follows an abort or an interrupt re-selects everything and
    skips almost all of it. One line per skip is thousands of lines saying
    nothing happened, which buries the handful that say something did -- so a
    skipped unit is a number in the census and never a line of its own.
    """
    root = _two_arms(tmp_path)
    _run(root, rubric=True)
    capsys.readouterr()

    fake = FakeComplete()
    result = _run(root, complete=fake, rubric=True)

    out = capsys.readouterr().out
    assert fake.calls == 0
    assert _progress_lines(out) == []
    assert (
        "selected: 0 rubric, 0 votes over 0 pairs, 0 gate-decided; "
        "4 already judged (skipped)"
    ) in out
    assert result["selected"]["skipped"] == 4


def test_a_failed_append_aborts_the_batch_instead_of_buying_more_verdicts(
    tmp_path, monkeypatch
):
    """A full disk is not a per-unit failure, and treating it as one is how a
    pass spends thousands of dollars writing nothing.

    ENOSPC fails EVERY write from the moment it starts, so the per-unit
    `except` that makes one unreadable diff cheap makes this one catastrophic:
    the driver goes on calling a paid model for every remaining unit and
    records not one of the answers. Batch-fatal on the first one, and the
    message is about the disk rather than about the unit.
    """
    root = _three_arms(tmp_path)

    def _no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("scripts.judge.append_judgment", _no_space)
    fake = FakeComplete()

    result = _run(root, complete=fake, rubric=True)

    # A `RuntimeError` and deliberately NOT an `OSError`: the wrap is what
    # tells a disk failure from a socket one, and a StorageFailure that was
    # itself an OSError would be re-wrapped by the next `except OSError` it
    # passed and lose the distinction on the way out.
    assert issubclass(StorageFailure, RuntimeError)
    assert not issubclass(StorageFailure, OSError)

    assert fake.calls == 1, "the driver bought verdicts it could not record"
    assert len(result["errors"]) == 1
    assert "StorageFailure" in result["errors"][0]
    assert "No space left on device" in result["errors"][0]
    (abort,) = [w for w in result["warnings"] if "writes are failing" in w]
    assert "cannot be recorded" in abort
    assert "resume with the same command" in abort
    assert _lines(root) == []


def test_a_connection_shaped_oserror_from_the_seam_is_a_unit_error_not_a_batch_abort(
    tmp_path,
):
    """`OSError` is the socket's base class as well as the disk's, so the WRAP
    SCOPE is what makes the storage abort safe.

    A `ConnectionResetError` is an `OSError`, and it arrives from the seam --
    one flaky call in a nine-unit batch. Wrapping the unit rather than the two
    store calls would turn every reset into a batch abort, which is the
    opposite of the per-unit isolation the whole driver is arranged around.
    """
    root = _three_arms(tmp_path)
    fake = _DiesAfter(succeed_at=set(range(2, 10)), error=ConnectionResetError)

    result = _run(root, complete=fake, rubric=True)

    assert fake.calls == 9, "a per-unit failure stopped the batch"
    assert len(result["errors"]) == 1
    assert "ConnectionResetError" in result["errors"][0]
    assert not any("writes are failing" in w for w in result["warnings"])
    assert len(_lines(root)) == 8


def test_a_keyboard_interrupt_still_prints_the_summary_and_the_resume_hint_and_exits_130(
    tmp_path, monkeypatch, capsys
):
    """Ctrl-C is how a long pass actually ends, so it is a path with a report.

    An uncaught `KeyboardInterrupt` unwinds through `judge_event_log` and takes
    the summary with it -- the operator loses the reading over everything the
    pass DID buy, on the one exit that happens most often. The lines are on
    disk either way; what the traceback destroys is the only place anybody
    meets them.

    130 rather than 1, because `128 + SIGINT` is what a shell reads as "the
    operator stopped this" and 1 is what it reads as "the batch found
    problems".
    """
    root = _three_arms(tmp_path)

    class _CtrlC(FakeComplete):
        """A judge the operator interrupts after two units."""

        def __call__(self, prompt: str) -> str:
            if len(self.prompts) >= 2:
                raise KeyboardInterrupt
            return super().__call__(prompt)

    monkeypatch.setattr("scripts.judge.load_task_set", lambda *a, **k: [_task()])
    monkeypatch.setattr("scripts.judge.live_completion", lambda *a, **k: _CtrlC())

    assert main(["--event-log", str(root)]) == 130

    out = capsys.readouterr().out
    assert KAPPA_CAVEAT in out, "the interrupt took the summary with it"
    assert "resume with the same command" in out
    # The two units bought before the interrupt are on disk and READABLE: a
    # resume is keyed on them, so a half-written tail would cost them twice.
    assert len(_lines(root)) == 2


def test_the_summary_reports_tokens_not_dollars_and_says_why(capsys):
    """Spend, in the only unit this harness can honestly report it in.

    The judge models are deliberately absent from `costs.PRICE_BOOK` --
    mantle pricing is unpublished -- so a dollar figure here would be invented.
    Printing nothing was the other failure: a ~40-hour paid pass left the call
    count and the token totals unreconstructable from anything on disk, so
    "what did that cost" had no answer at all.
    """
    result = _empty_result()
    result["judge_usage"] = {
        "calls": 3, "prompt_tokens": 41_000, "completion_tokens": 2_500,
        "total_tokens": 43_500, "calls_without_usage": 1, "auth_refreshes": 2,
    }

    print_summary(result)

    out = capsys.readouterr().out
    assert "41,000" in out
    assert "2,500" in out
    assert "43,500" in out
    assert "3 completion request(s)" in out
    assert "2 credential refresh" in out
    assert "1 of them reported no usage" in out
    # The sentence that says why this is not a dollar figure, and names the
    # place a reader would go looking for one.
    assert "PRICE_BOOK" in out
    assert "$" not in out


def test_a_progress_line_a_terminal_cannot_encode_never_takes_the_batch_down(
    tmp_path, monkeypatch
):
    """The progress line is the only collection-derived text printed from
    INSIDE the walk, which makes its encoding a batch-safety question rather
    than a formatting one.

    A label carries a task id, model names and run ids. On a stdout the
    environment pinned to ASCII -- `LC_ALL=C`, or a pipe into a tool that did
    -- one non-ASCII character in any of them raises `UnicodeEncodeError` from
    `print`, and that is NOT an `Exception` the per-unit handler catches: it is
    raised past it, past `except _BatchAborted`, past `except
    KeyboardInterrupt` and out of `main`. A pass that had been running for
    hours would end on a traceback with no summary, no usage totals and every
    remaining unit unbought -- the failure class this section exists to remove,
    arriving from the code added to remove it.

    Unreachable from today's data (task ids are directory names, model ids come
    from config, run ids are hex) and catastrophic if it is ever reached, which
    is the same shape as the κ caveat's own ASCII fallback -- and it is reached
    by an environment variable rather than by a flag, so no review of the
    collection can rule it out.

    The label is asserted to SURVIVE, escaped, rather than merely not to crash:
    `backslashreplace` keeps it greppable, and a guard that dropped the line
    would leave the batch alive but the unit invisible.
    """
    root = _collection(
        tmp_path,
        [_record("run-π", model="modèle-un", final_diff=DIFF_A)],
        [_grade("run-π", model="modèle-un")],
    )
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    result = _run(root, rubric=True)
    stream.flush()

    text = stream.buffer.getvalue().decode("ascii")
    assert result["errors"] == [], "the batch died on a printout"
    assert len(_lines(root)) == 1, "the unit was not judged"
    # Escaped rather than dropped, and still greppable back to the run an
    # operator would go looking for.
    assert "run-\\u03c0" in text
    assert "mod\\xe8le-un" in text
    assert "[1/1]" in text and " ok " in text


def test_an_injected_seam_reports_no_usage_and_the_live_one_reports_zeros(
    tmp_path, monkeypatch
):
    """`None` and a dict of zeros are different facts, and both are real.

    A batch driven through an injected `complete` cannot be counted at all --
    the seam's contract is one string in and one string out, so there is no
    response to read tokens off -- and reporting zeros for it would be a
    measurement of a batch nobody measured. A batch on the LIVE seam that
    happens to make no call (every comparison settled by the ladder, or
    everything already judged) genuinely spent nothing, and zero is the right
    answer.

    The second half also pins that the accumulator is created BESIDE the lazy
    seam rather than inside it: the live judge is never built here, so a dict
    made on first use would still be `None` at the end of the pass.
    """
    root = _two_arms(tmp_path, resolved_a=True, resolved_b=False)

    assert _run(root, complete=FakeComplete(), rubric=False)["judge_usage"] is None

    fresh = _two_arms(tmp_path / "second", resolved_a=True, resolved_b=False)
    monkeypatch.setattr(
        "scripts.judge.live_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )
    usage = judge_event_log(fresh, [_task()], rubric=False)["judge_usage"]

    assert usage == {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "calls_without_usage": 0, "auth_refreshes": 0,
    }


def test_importing_the_judge_driver_needs_neither_docker_nor_litellm():
    """`python scripts/judge.py --help` on a box with no Docker daemon.

    Two import edges made that fail before argparse ran. `scripts.grade` was
    imported for `grades_path` alone and drags `bakeoff.images` -> `docker`
    behind it, so an analysis box without the package could not read the usage
    line; and litellm calls `load_dotenv()` on import, so merely importing the
    driver poured `bakeoff/.env` into the process environment -- credentials
    and all -- before the operator had asked for anything.

    A SUBPROCESS, because `sys.modules` in this one is already full of both:
    the suite imports litellm for the live-completion tests and docker for the
    container ones, so an in-process assertion would be about pytest rather
    than about the driver.
    """
    probe = (
        "import sys; import scripts.judge; "
        "print('docker' in sys.modules, 'litellm' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "False"], done.stdout


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
        "interrupted": False, "judge_usage": None,
        "selected": {"rubric": 0, "votes": 0, "pairs": 0, "gate_decided": 0,
                     "skipped": 0, "units": 0},
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


# --------------------------------------------------------------------------
# 7a. what the numbers are WORTH: intervals, the voted-only decomposition,
#     and the position-swap probe
# --------------------------------------------------------------------------
#
# §10.3: "any metric reported without a confidence interval is not reported",
# and §4.4 says which interval -- a cluster bootstrap over TASKS, because
# comparisons inside one task are correlated and a naive binomial band is
# several times too narrow. The other two sections here are about what a rate
# is made of: how much of it the deterministic ladder decided rather than the
# judge (§10.1's 40% reference is a TIER B number, and gate-decided
# comparisons carry Tier A's pass rate into it), and whether the judge said the
# same thing in both forced positions (§4.2.3's position-swap probe, free now
# that every v2 comparison is judged in both orders).


def _voted_pair(run_id_a, run_id_b, verdict, **kw):
    """One complete v2 comparison: both forced positions, agreeing."""
    return [
        _vote(run_id_a, run_id_b, verdict, vote_index=0, **kw),
        _vote(run_id_a, run_id_b, verdict, vote_index=1, **kw),
    ]


def test_voted_only_win_rates_are_reported_beside_combined_and_never_divide_by_zero():
    """A gate-decided comparison is a TIER A result standing in a Tier B
    number, so a combined win rate is part pass rate and part judgment -- and
    nothing on the printout said which part. §10.1 reports Tier B pairwise win
    rates against a 40% reference; read off the combined rate alone, two arms
    the judge cannot separate at all land either side of it purely on their
    gate results.

    The fixture is exactly that collection: the judge preferred `model-one` in
    both comparisons it was asked about, the ladder settled the other two the
    other way, and the combined rate is a dead heat that neither channel
    reported. The voted-only rate and the voted-only rating are what make the
    decomposition visible.

    `None` and never `0.0` when a pair holds no judged comparison: 0/0 is a
    `ZeroDivisionError` out of the middle of a summary a 40-hour batch already
    paid for, and a `0.0` printed in its place is a win rate of zero -- the
    judge's worst possible verdict -- reported for a judge that was never
    asked.
    """
    judgments = [
        *_voted_pair("run-a", "run-b", "a", sample_index=0),
        *_voted_pair("run-a", "run-b", "a", sample_index=1),
        _gate("run-a", "run-b", "b", sample_index=2),
        _gate("run-a", "run-b", "b", sample_index=3),
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][_judge_gen()][("model-one", "model-two")]
    assert row["win_rate_x"] == pytest.approx(0.5)
    assert row["win_rate_x_voted"] == pytest.approx(1.0)
    # The same split in the ratings: pooled, the two channels cancel to the
    # anchor and the table prints a dead heat the judge never reported.
    combined = summary["elo"][_judge_gen()]
    voted = summary["elo_voted"][_judge_gen()]
    assert combined["model-one"] == pytest.approx(combined["model-two"])
    assert voted["model-one"] > voted["model-two"]

    settled = summarize([_gate("run-a", "run-b", "a")], MODEL_OF)
    only_gate = settled["comparisons"][_judge_gen()][("model-one", "model-two")]
    assert only_gate["win_rate_x"] == pytest.approx(1.0)
    assert only_gate["win_rate_x_voted"] is None
    # And no rating either: an empty table says "nothing was voted on here",
    # where a table of anchors would say "the judge called it a dead heat".
    assert settled["elo_voted"][_judge_gen()] == {}


def test_gate_decided_share_is_reported_per_arm_per_generation():
    """How much of an arm's block the judge never saw, said out loud.

    The voted-only rate says what the judge thought; this says how much weight
    it carries. An arm whose comparisons are three-quarters gate-decided has a
    Tier B row that is mostly Tier A's pass rate re-derived, and that is a fact
    about THAT arm -- a shared block-level count would average a heavily gated
    arm together with one the ladder never touched.
    """
    other = "openai.gpt-5.6-luna"
    judgments = [
        *_voted_pair("run-a", "run-b", "a", sample_index=0),
        _gate("run-a", "run-b", "a", sample_index=1),
        _gate("run-a", "run-c", "a", sample_index=2),
        _gate("run-a", "run-b", "a", sample_index=3, judge_model_id=other),
    ]

    share = summarize(judgments, MODEL_OF)["gate_decided_share"]

    assert share[_judge_gen()] == {
        "model-one": {"comparisons": 3, "gate_decided": 2},
        "model-three": {"comparisons": 1, "gate_decided": 1},
        "model-two": {"comparisons": 2, "gate_decided": 1},
    }
    # Per generation, for the matrix's reason: the second oracle read one
    # comparison of this collection, not a fourth of the first oracle's three.
    assert share[_judge_gen(other)] == {
        "model-one": {"comparisons": 1, "gate_decided": 1},
        "model-two": {"comparisons": 1, "gate_decided": 1},
    }


def test_position_consistency_counts_agreement_across_the_two_forced_positions():
    """§4.2.3 asks for a position-swap probe on a subset. Under two forced
    positions every complete comparison IS the probe, so it is computed over
    the whole collection and costs nothing extra.

    MEASURABLE means the comparison's votes cover both positions; CONSISTENT
    means every canonical verdict in it is the same word. A comparison whose
    two positions disagree is the judge preferring whichever submission it saw
    first -- `majority` already scores it a tie, and this is the count that
    says how often that happened rather than leaving a reader to infer it from
    the tie column, which also holds honest ties.

    A gate-decided comparison has no votes and therefore no positions: it is
    neither measurable nor inconsistent, and counting it either way would put
    the ladder's arithmetic inside a rate about the judge's.
    """
    judgments = [
        *_voted_pair("run-a", "run-b", "a", sample_index=0),
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=1),
        _vote("run-a", "run-b", "b", vote_index=1, sample_index=1),
        _gate("run-a", "run-b", "a", sample_index=2),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["position_consistency"][_judge_gen()] == {
        "measurable": 2, "consistent": 1, "rate": 0.5, "rate_ci95": (0.5, 0.5),
        "single_position": 0, "position_unrecorded": 0,
    }


def test_a_v2_comparison_decided_on_one_position_is_counted_and_not_measurable():
    """One position's vote errored and the other's is on disk, so the verdict
    rests on a single order -- exactly the position bias forced positions exist
    to cancel, and the aggregation cannot see it: the comparison resolves
    through `majority` like any other and enters the matrix as one comparison.

    It is NOT measurable -- there is nothing to compare it against -- and it is
    counted, because "the judge agreed with itself on 100% of 4 measurable
    comparisons" over a block where 30 more verdicts each rest on one position
    is a consistency rate reported over a denominator nobody can see.
    """
    judgments = [
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=0),
        *_voted_pair("run-a", "run-b", "a", sample_index=1),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["position_consistency"][_judge_gen()] == {
        "measurable": 1, "consistent": 1, "rate": 1.0, "rate_ci95": (1.0, 1.0),
        "single_position": 1, "position_unrecorded": 0,
    }
    # Still one comparison each in the matrix: visible, not dropped.
    assert summary["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]["comparisons"] == 2


def test_an_old_three_vote_comparison_is_measurable_only_when_both_positions_appear():
    """ONE rule for both protocols, which is why it is written about positions
    rather than about vote counts.

    v1 drew its three positions at random, so a v1 comparison is a position
    probe only when the draw happened to split -- and the draw that landed on
    one order three times is the case the rule has to refuse, because all three
    of its verdicts share whatever bias that order carries. Written as "a v1
    comparison is measurable" it would report agreement between three views of
    the same position as agreement across positions.
    """
    judgments = [
        # The draw landed a_first three times: no swap in it to probe.
        *[_vote("run-a", "run-b", verdict, vote_index=index, sample_index=0,
                judge_prompt_version=1, position_assignment="a_first")
          for index, verdict in enumerate(("a", "a", "b"))],
        # A draw that split, and agreed everywhere it landed.
        *[_vote("run-a", "run-b", "a", vote_index=index, sample_index=1,
                judge_prompt_version=1, position_assignment=position)
          for index, position in enumerate(("a_first", "a_first", "b_first"))],
        # A draw that split and disagreed across the two orders.
        *[_vote("run-a", "run-b", verdict, vote_index=index, sample_index=2,
                judge_prompt_version=1, position_assignment=position)
          for index, (verdict, position) in enumerate(
              (("a", "a_first"), ("a", "b_first"), ("b", "b_first")))],
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["position_consistency"][_judge_gen(prompt_version=1)] == {
        "measurable": 2, "consistent": 1, "rate": 0.5, "rate_ci95": (0.5, 0.5),
        "single_position": 1, "position_unrecorded": 0,
    }


def test_a_comparison_whose_votes_record_no_position_gets_its_own_column():
    """Two ways a `position_assignment` this reader cannot use reaches the
    aggregation: a hand-edited line, and a line from a schema that never wrote
    the field. Neither is measurable and neither rests on a KNOWN single
    position, so a comparison made only of them belongs in a column of its
    own -- absent from all three, it is a denominator moving invisibly, which
    is the failure `dropped` is placed against one level down.

    And the verdict set is taken over the same lines the position check
    accepted. The second comparison here has both forced positions recorded and
    agreeing, plus a third line with no position and the opposite verdict:
    reading that verdict would mark the comparison inconsistent on the strength
    of a line the measurable test had already thrown out, which is a position
    effect reported off a line that records no position.
    """
    judgments = [
        # Votes, no readable position anywhere in the comparison.
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=0,
              position_assignment=None),
        _vote("run-a", "run-b", "a", vote_index=1, sample_index=0,
              position_assignment="somewhere-else"),
        # Both positions recorded and agreeing, plus an unplaced dissenter.
        _vote("run-a", "run-b", "a", vote_index=0, sample_index=1),
        _vote("run-a", "run-b", "a", vote_index=1, sample_index=1),
        _vote("run-a", "run-b", "b", vote_index=2, sample_index=1,
              position_assignment=None),
    ]

    summary = summarize(judgments, MODEL_OF)

    assert summary["position_consistency"][_judge_gen()] == {
        "measurable": 1, "consistent": 1, "rate": 1.0, "rate_ci95": (1.0, 1.0),
        "single_position": 0, "position_unrecorded": 1,
    }


def test_a_swept_pair_does_not_print_a_zero_width_interval_at_sixty_tasks():
    """The boundary the bootstrap cannot express, and the one place a resampled
    band lies rather than merely being wide.

    Every resample of a pair one arm swept returns 1.0, and the percentiles of
    a constant are that constant -- so the band printed was `[100.0%, 100.0%]`
    at ANY task count, a 95% interval claiming the collection ruled out every
    other value. A swept pair is not exotic: an arm the ladder gate-decided
    against in every comparison is exactly this shape, and the pilot collection
    has one at 9 of 9.

    So a rate with no variation across resamples falls back to a Wilson score
    interval on the TASK count: 60 swept tasks read as about [94%, 100%], which
    is the figure an analyst computing it by hand would get, and two swept
    tasks read as about [34%, 100%]. The consistency rate takes the same
    fallback for the same reason -- a judge that agreed with itself on every
    measurable comparison is the HEALTHY shape, so the degenerate resample is
    the expected case there rather than the exception.

    ONE TASK is deliberately left degenerate: there is nothing to resample, and
    a Wilson band on n=1 would dress a single task up as a measurement of the
    population. `test_win_rate_intervals_are_task_clustered_...` pins that end.
    """
    judgments = [
        vote
        for task in range(60)
        for vote in _voted_pair("run-a", "run-b", "a", task_id=f"task-{task}")
    ]

    summary = summarize(judgments, MODEL_OF)

    row = summary["comparisons"][_judge_gen()][("model-one", "model-two")]
    low, high = row["win_rate_x_ci95"]
    assert row["win_rate_x"] == 1.0
    assert low < 1.0
    assert high == pytest.approx(1.0)
    assert low == pytest.approx(0.94, abs=0.01)
    # The judge-voted rate is the same rate over the same comparisons here, and
    # it gets the same treatment -- a second code path would be a second way to
    # print a zero-width band.
    assert row["win_rate_x_voted_ci95"] == (low, high)
    consistency = summary["position_consistency"][_judge_gen()]
    assert consistency["rate"] == 1.0
    assert consistency["rate_ci95"] == (low, high)

    # Two tasks, same sweep: the band is far wider, because two tasks say far
    # less. Same rule, no threshold anywhere in the code.
    thin = [
        vote
        for task in range(2)
        for vote in _voted_pair("run-a", "run-b", "a", task_id=f"task-{task}")
    ]

    scarce = summarize(thin, MODEL_OF)["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]

    assert scarce["win_rate_x_ci95"][0] == pytest.approx(0.34, abs=0.01)


def test_a_sparse_pairs_band_rests_on_its_own_tasks_and_never_on_the_blocks():
    """The boundary band's n is the number of tasks THAT RATE rests on. Handed
    the block's count instead, a rate borrows precision from tasks it was never
    measured on -- §4.4's error again, arriving through the correction that
    was placed against it.

    The sequence below is the version that gives it away. A pair playing
    exactly ONE task slips past the one-task exception the moment a second,
    unrelated task exists, and its band then NARROWS as more unrelated tasks
    arrive: measured on the block count, [100.0%, 100.0%] alone, [34.2%, ...]
    at 2 tasks, [61.0%, ...] at 6, [83.9%, ...] at 20 and [94.0%, ...] at 60,
    with that pair's own evidence untouched throughout. A collection growing
    around a pair cannot sharpen what is known about the pair.

    The 2-of-40 case underneath is the same defect at a size a real collection
    reaches: two tasks' worth of evidence must read like two tasks.
    """
    pair = ("model-one", "model-two")
    for foreign in (0, 1, 5, 19, 59):
        judgments = _voted_pair("run-a", "run-b", "a", task_id="own-task")
        judgments += [
            vote
            for task in range(foreign)
            for vote in _voted_pair("run-a", "run-c",
                                    "a" if task % 2 else "b",
                                    task_id=f"foreign-{task}")
        ]

        row = summarize(judgments, MODEL_OF)["comparisons"][_judge_gen()][pair]

        assert row["win_rate_x_ci95"] == (1.0, 1.0), foreign

    sparse = [
        vote
        for task in range(2)
        for vote in _voted_pair("run-a", "run-b", "a", task_id=f"task-{task}")
    ]
    sparse += [
        vote
        for task in range(38)
        for vote in _voted_pair("run-a", "run-c", "a" if task % 2 else "b",
                                task_id=f"task-{task + 2}")
    ]

    row = summarize(sparse, MODEL_OF)["comparisons"][_judge_gen()][pair]

    # Two tasks, read as two tasks -- 91.2% is what the block's 40 would say.
    assert row["win_rate_x_ci95"][0] == pytest.approx(0.342, abs=0.001)


def test_the_voted_only_band_rests_on_the_tasks_the_judge_actually_voted_on():
    """The most reachable form of the same defect, and the one the pilot
    collection already has: an arm the ladder settled nearly everywhere has a
    judge-voted rate resting on a couple of tasks inside a collection of forty.
    Its band has to say a couple of tasks.

    The two rates on this row are deliberately EQUAL -- both 100% -- so the
    only thing separating their bands is the evidence each rests on: 40 tasks
    for the combined rate, 2 for the judge-voted one.
    """
    judgments = [
        vote
        for task in range(2)
        for vote in _voted_pair("run-a", "run-b", "a", task_id=f"task-{task}")
    ]
    judgments += [
        _gate("run-a", "run-b", "a", task_id=f"task-{task + 2}")
        for task in range(38)
    ]

    row = summarize(judgments, MODEL_OF)["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]

    assert row["win_rate_x"] == row["win_rate_x_voted"] == 1.0
    assert row["win_rate_x_ci95"][0] == pytest.approx(0.912, abs=0.001)
    assert row["win_rate_x_voted_ci95"][0] == pytest.approx(0.342, abs=0.001)


def test_the_consistency_band_rests_on_the_tasks_a_probe_could_read():
    """Third call site, same rule. A block where the position probe can read
    two tasks out of forty knows what two tasks know; the other thirty-eight
    hold verdicts that rest on one position and say nothing about position at
    all, so they cannot be in the denominator of the band."""
    judgments = [
        vote
        for task in range(2)
        for vote in _voted_pair("run-a", "run-b", "a", task_id=f"task-{task}")
    ]
    judgments += [
        _vote("run-a", "run-b", "a", vote_index=0, task_id=f"task-{task + 2}")
        for task in range(38)
    ]

    consistency = summarize(judgments, MODEL_OF)["position_consistency"][
        _judge_gen()
    ]

    assert (consistency["measurable"], consistency["single_position"]) == (
        2, 38,
    )
    assert consistency["rate"] == 1.0
    assert consistency["rate_ci95"][0] == pytest.approx(0.342, abs=0.001)


def test_a_one_task_rate_is_marked_where_it_is_printed(capsys):
    """After the boundary rule the only rate that can still print a zero-width
    band is one resting on a single task -- and a reader who meets
    `[100.0%, 100.0%]` a few rows below a widened band has no way to tell which
    rule produced it. The header explains both; the row says which one it is.
    """
    result = _empty_result()
    result["summary"] = summarize(
        [
            *_voted_pair("run-a", "run-b", "a", task_id="lonely"),
            *[
                vote
                for task in range(4)
                for vote in _voted_pair("run-a", "run-c",
                                        "a" if task % 2 else "b",
                                        task_id=f"task-{task}")
            ],
        ],
        MODEL_OF,
    )

    print_summary(result)

    lines = capsys.readouterr().out.splitlines()
    (lonely,) = [
        line for line in lines
        if line.strip().startswith("combined") and "model-two" in line
    ]
    (spread,) = [
        line for line in lines
        if line.strip().startswith("combined") and "model-three" in line
    ]
    assert "one task" in lonely
    assert "one task" not in spread


def test_a_rating_band_of_zero_width_prints_as_a_refusal_not_a_number(capsys):
    """A swept pair's rating is identical in every task resample, and unlike a
    rate it cannot be widened -- past two arms a rating is a function of the
    whole comparison graph, so a rate's Wilson bound does not map onto one.

    What is left is a choice between printing `[1381.7, 1381.7]`, which says
    the collection resolved this arm exactly, and printing a dash, which says
    it did not. The rating is not even stable in the way that band claims: the
    same sweep fits a different number at a different task count, because what
    bounds it is the half-game prior rather than the evidence. The table
    already prints `--` for an arm the judge never voted on; the same dash
    carries the same meaning here.
    """
    result = _empty_result()
    result["summary"] = summarize(
        [
            vote
            for task in range(2)
            for vote in _voted_pair("run-a", "run-b", "a",
                                    task_id=f"task-{task}")
        ],
        MODEL_OF,
    )

    low, high = result["summary"]["elo_ci95"][_judge_gen()]["model-one"]
    assert low == high

    print_summary(result)

    # The ratings row specifically -- `model-one` also opens a matrix row.
    (rating_line,) = [
        line for line in capsys.readouterr().out.splitlines()
        if re.match(r"\s+model-one\s+\d+\.\d", line)
    ]
    assert "--" in rating_line
    assert f"{low:.1f}, " not in rating_line
    # The win rate above it IS widened, and the printout says to read it there.
    assert result["summary"]["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]["win_rate_x_ci95"][0] == pytest.approx(0.342, abs=0.001)


def test_every_rating_resample_is_recentred_on_the_arms_it_shares_with_the_full_fit():
    """Bradley-Terry identifies DIFFERENCES between strengths and nothing else,
    so `elo_from_outcomes` anchors the mean of whatever arms it was handed at
    1000. A resample that misses an arm therefore re-centres on a different arm
    set, and every surviving arm's rating moves by the mean of the missing
    ones -- a measured 69-point offset between one arm's samples with and
    without another arm present. Those offset samples land in the percentiles
    of arms that were never absent, so the anchor's movement is reported as
    uncertainty about arms it says nothing about.

    Re-centring is a pure translation onto the full fit's level over the shared
    arms: every difference the resample fit survives it, and the level it lands
    on is the one the printed point estimates are on.

    The fixture is the shape that produces the offset -- `model-three` plays in
    ONE of eight tasks, so a quarter of the resamples drop it entirely.
    Measured on it: `model-two`'s band is 280 points wide re-centred and 428
    points wide without, over a point estimate that does not move either way.
    """
    fit = {"a": 1100.0, "b": 900.0}
    anchor = {"a": 1200.0, "b": 1000.0, "gone": 800.0}

    shifted = _recentred(fit, anchor)

    # The level matches over the shared arms, and the differences are intact.
    assert shifted == {"a": 1200.0, "b": 1000.0}
    assert shifted["a"] - shifted["b"] == fit["a"] - fit["b"]
    # Nothing shared: no common level to move onto, so nothing is invented.
    assert _recentred(fit, {"gone": 800.0}) == fit

    judgments = [
        vote
        for task in range(8)
        for vote in _voted_pair("run-a", "run-b", "a" if task % 2 else "b",
                                task_id=f"task-{task}")
    ]
    judgments += [
        vote
        for sample in range(6)
        for vote in (
            *_voted_pair("run-a", "run-c", "a", sample_index=sample,
                         task_id="task-0"),
            *_voted_pair("run-b", "run-c", "a", sample_index=sample + 10,
                         task_id="task-0"),
        )
    ]

    summary = summarize(judgments, MODEL_OF)

    low, high = summary["elo_ci95"][_judge_gen()]["model-two"]
    assert low < summary["elo"][_judge_gen()]["model-two"] < high
    # 280 re-centred, 428 without. Asserted with room either side: this pins
    # that the anchor's movement is out of the band, not the exact width.
    assert high - low < 350.0


def test_the_voted_only_rates_and_the_consistency_rate_carry_intervals_too():
    """§10.3 has no exemption in it: "any metric reported without a confidence
    interval is not reported". The judge-voted rate is a win rate, the
    judge-voted rating is a rating and the consistency rate is a rate, so all
    three get the same treatment as the combined figures -- and out of the SAME
    task resamples, so that a reader comparing the combined band with the
    judge-voted band beside it is comparing draws of the collection that
    actually happened together.

    A rate that does not exist gets no band: `win_rate_x_voted_ci95` is `None`
    exactly when `win_rate_x_voted` is, and an arm absent from the voted-only
    fit is absent from its intervals too.
    """
    judgments = [
        vote
        for task in range(6)
        for vote in (
            *_voted_pair("run-a", "run-b", "a" if task % 3 else "b",
                         sample_index=0, task_id=f"task-{task}"),
            # The second comparison on each task splits its positions on two of
            # the six, so the consistency rate has something to resample.
            _vote("run-a", "run-b", "a", vote_index=0, sample_index=1,
                  task_id=f"task-{task}"),
            _vote("run-a", "run-b", "a" if task % 3 else "b", vote_index=1,
                  sample_index=1, task_id=f"task-{task}"),
        )
    ]
    judgments += [
        _gate("run-a", "run-c", "a", sample_index=2, task_id="task-0"),
    ]

    summary = summarize(judgments, MODEL_OF)

    block = summary["comparisons"][_judge_gen()]
    row = block[("model-one", "model-two")]
    voted_low, voted_high = row["win_rate_x_voted_ci95"]
    assert voted_low < row["win_rate_x_voted"] < voted_high
    # A pair with no judged comparison at all: no rate, and so no band either.
    swept = block[("model-one", "model-three")]
    assert swept["win_rate_x_voted"] is None
    assert swept["win_rate_x_voted_ci95"] is None

    consistency = summary["position_consistency"][_judge_gen()]
    rate_low, rate_high = consistency["rate_ci95"]
    assert rate_low < consistency["rate"] < rate_high

    voted_elo = summary["elo_voted"][_judge_gen()]
    voted_bands = summary["elo_voted_ci95"][_judge_gen()]
    # `model-three` played one comparison and the ladder settled it, so it is
    # in the combined fit and in neither voted-only table.
    assert set(voted_elo) == set(voted_bands) == {"model-one", "model-two"}
    assert "model-three" in summary["elo_ci95"][_judge_gen()]
    for model, rating in voted_elo.items():
        low, high = voted_bands[model]
        assert low <= rating <= high


def test_the_per_generation_seed_survives_a_different_process_hash_salt():
    """`_bootstrap_seed` derives a seed from the generation, and the obvious
    way to write it -- `hash((BOOTSTRAP_SEED,) + generation)` -- is wrong in a
    way no in-process test can see. Python salts `hash()` for strings and
    tuples per process (PYTHONHASHSEED), so the seed, and every band under it,
    would differ between two runs of the SAME command on the SAME file. Inside
    one interpreter the salt is fixed, so the equality test above stays green
    and the damage only appears on someone else's machine, or on a re-run.

    A SUBPROCESS PAIR, with the salt pinned to two different values, is the
    only way to assert this at all. `hashlib` has no salt, which is why the
    implementation uses it.
    """
    probe = "\n".join([
        "import sys, uuid",
        "sys.path.insert(0, 'bakeoff/src')",
        "sys.path.insert(0, 'bakeoff')",
        "from bakeoff.judge import (JUDGE_MODEL_ID_DEFAULT, "
        "JUDGE_PROMPT_VERSION, JUDGE_SAMPLING, RUBRIC_VERSION)",
        "from bakeoff.judge_schema import JudgeRecord",
        "from scripts.judge import summarize",
        "lines = [JudgeRecord(judgment_id=uuid.uuid4().hex,",
        "    judged_at='2026-08-19T00:00:00Z',",
        "    judge_model_id=JUDGE_MODEL_ID_DEFAULT,",
        "    judge_prompt_version=JUDGE_PROMPT_VERSION,",
        "    judge_prompt_sha='0' * 64,",
        "    judge_sampling=dict(JUDGE_SAMPLING),",
        "    rubric_version=RUBRIC_VERSION, kind='pairwise',",
        "    task_id='task-' + str(task), sample_index=0,",
        "    run_id_a='run-a', run_id_b='run-b', vote_index=index,",
        "    position_assignment=('a_first', 'b_first')[index],",
        "    verdict=('b' if task % 3 == 0 else 'a'),",
        "    input_payload_path='/p/x.json.gz', input_payload_sha='s' * 64)",
        "    for task in range(8) for index in (0, 1)]",
        "summary = summarize(lines, "
        "{'run-a': 'model-one', 'run-b': 'model-two'})",
        "block = summary['comparisons'][list(summary['comparisons'])[0]]",
        "print(block[('model-one', 'model-two')]['win_rate_x_ci95'])",
    ])
    printed = set()
    for salt in ("0", "12345"):
        done = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT),
                 "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": salt},
        )
        assert done.returncode == 0, done.stderr
        printed.add(done.stdout.strip())

    # One band, from two processes that hash their strings differently. The
    # band is asserted non-degenerate first, so this cannot pass by printing
    # the point estimate twice.
    (band,) = printed
    low, high = eval(band)
    assert low < high


def test_a_foreign_generations_lines_cannot_move_another_generations_bands():
    """One generator for the whole summary makes each block's draws depend on
    how many blocks were resampled before it. Appending a single line under a
    DIFFERENT oracle cannot change a v2 comparison, a v2 win rate or a v2
    rating -- but with a shared generator it shifted the v2 bands anyway, by a
    measured 2.8 rating points, with every point estimate byte-identical. A
    band that moves when the collection did not is the failure the seed exists
    to prevent; a band that moves because of a DIFFERENT oracle's lines reads
    as a finding about that oracle.

    `_bootstrap_seed` derives each block's seed from its own generation, so the
    two blocks are independent no matter what else is in the file.
    """
    base = [
        vote
        for task in range(8)
        for vote in _voted_pair("run-a", "run-b", "a" if task % 3 else "b",
                                task_id=f"task-{task}")
    ]
    foreign = _voted_pair("run-a", "run-b", "b", task_id="task-0",
                          judge_prompt_version=1)

    alone = summarize(base, MODEL_OF)
    beside = summarize(base + foreign, MODEL_OF)

    assert set(beside["comparisons"]) == {
        _judge_gen(), _judge_gen(prompt_version=1),
    }
    for key in ("comparisons", "elo", "elo_ci95", "elo_voted_ci95",
                "position_consistency"):
        assert alone[key][_judge_gen()] == beside[key][_judge_gen()], key


def test_win_rate_intervals_are_task_clustered_so_one_task_collections_collapse_to_the_point():
    """§4.4, and the reason it is stated in the spec at all: attempts inside
    one task are correlated, so effective sample size tracks the TASK count and
    not the comparison count. Ten comparisons on one task are ten reads of one
    task's difficulty; resampling them independently would print a +-15-point
    band off a collection whose real uncertainty about the population of tasks
    is unbounded.

    The degenerate interval is the honest one here and it is what pins the
    clustering: a comparison-level bootstrap cannot produce it, and the same
    ten comparisons spread over ten tasks give the wide band underneath.
    """
    pair = ("model-one", "model-two")
    one_task = [
        vote
        for sample in range(10)
        for vote in _voted_pair(
            "run-a", "run-b", "a" if sample < 7 else "b",
            sample_index=sample, task_id="one-task",
        )
    ]

    row = summarize(one_task, MODEL_OF)["comparisons"][_judge_gen()][pair]

    assert row["win_rate_x"] == pytest.approx(0.7)
    assert row["win_rate_x_ci95"] == (row["win_rate_x"], row["win_rate_x"])

    spread = [
        vote
        for sample in range(10)
        for vote in _voted_pair(
            "run-a", "run-b", "a" if sample < 7 else "b",
            sample_index=sample, task_id=f"task-{sample}",
        )
    ]

    wide = summarize(spread, MODEL_OF)["comparisons"][_judge_gen()][pair]

    low, high = wide["win_rate_x_ci95"]
    assert wide["win_rate_x"] == pytest.approx(0.7)
    assert low < wide["win_rate_x"] < high
    # Ten tasks is a small collection and the band says so, rather than
    # reporting the 0.7 as though it were resolved.
    assert high - low > 0.3


def test_elo_intervals_follow_task_resamples():
    """The rating table is descriptive of the win rates, so its interval is
    taken the same way: refit Bradley-Terry on every task resample and read the
    percentiles off the refits. A rating printed without one invites exactly
    the reading §4.3 forbids -- a 40-point gap taken for a result when the
    collection cannot resolve 200.

    Two tasks that disagree completely are the sharp case: a quarter of the
    resamples draw only the task `model-one` swept, a quarter only the task it
    lost, and the interval has to span both rather than concentrate around the
    dead heat the pooled fit reports.
    """
    one_task = [
        vote
        for sample in range(4)
        for vote in _voted_pair("run-a", "run-b", "a", sample_index=sample,
                                task_id="one-task")
    ]

    settled = summarize(one_task, MODEL_OF)

    rating = settled["elo"][_judge_gen()]["model-one"]
    assert settled["elo_ci95"][_judge_gen()]["model-one"] == (rating, rating)

    split = [
        vote
        for task, verdict in (("task-1", "a"), ("task-2", "b"))
        for sample in range(4)
        for vote in _voted_pair("run-a", "run-b", verdict,
                                sample_index=sample, task_id=task)
    ]

    summary = summarize(split, MODEL_OF)

    point = summary["elo"][_judge_gen()]["model-one"]
    low, high = summary["elo_ci95"][_judge_gen()]["model-one"]
    assert point == pytest.approx(ELO_ANCHOR)
    assert low < point < high
    assert high - low > 100.0


def test_the_bootstrap_is_seeded_and_two_summaries_of_one_file_agree_exactly():
    """A resampled interval that moves between two readings of one unchanged
    file is an interval a reader cannot diff, cite or reproduce -- and it moves
    in the last digit, which is where it looks like a real change in the
    collection rather than like noise the summary invented.

    A generator built per BLOCK from `BOOTSTRAP_SEED` and the generation
    (`_bootstrap_seed`), never module state: module state would make the
    numbers depend on how many summaries this process had already taken.

    THE FIXTURE HAS TO BE RICH TO PIN THIS, and the first version of this test
    was not. Eight tasks at one rate per task put every resampled statistic on
    a coarse lattice -- the pair rate can only be k/8 -- so the 2.5th and
    97.5th percentiles land on the same two atoms in most runs and two
    UNSEEDED summaries agree by accident about a fifth of the time. Twelve
    tasks at three samples, three arms, per-task rates on a finer grid, some
    comparisons the ladder settled and some positions that split gives ten-odd
    bands that all have to coincide at once: measured, two unseeded summaries
    disagreed in 20 of 20 trials.

    The constants are pinned to their literals for `test_ratings_are_mean_
    anchored`'s reason -- an interval whose resample count nobody recorded is
    a number nobody can reproduce -- and the bands are asserted non-degenerate
    so the equality above has something to be equal about.
    """
    assert (BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED) == (1000, 0)

    judgments = []
    for task in range(12):
        for sample in range(3):
            judgments += _voted_pair(
                "run-a", "run-b", "a" if sample < task % 4 else "b",
                sample_index=sample, task_id=f"task-{task}",
            )
            # The two positions disagree on a third of these, so the
            # consistency rate has a distribution of its own to resample.
            split = (task + sample) % 3 == 0
            judgments += [
                _vote("run-a", "run-c", "a", vote_index=0,
                      sample_index=sample + 10, task_id=f"task-{task}"),
                _vote("run-a", "run-c", "b" if split else "a", vote_index=1,
                      sample_index=sample + 10, task_id=f"task-{task}"),
            ]
        judgments.append(
            _gate("run-b", "run-c", "b" if task % 2 else "a",
                  sample_index=99, task_id=f"task-{task}")
        )

    first = summarize(judgments, MODEL_OF)
    second = summarize(judgments, MODEL_OF)

    assert first == second
    block = first["comparisons"][_judge_gen()]
    for pair, row in block.items():
        low, high = row["win_rate_x_ci95"]
        assert low < high, pair
    assert all(
        low < high for low, high in first["elo_ci95"][_judge_gen()].values()
    )
    consistency_low, consistency_high = first["position_consistency"][
        _judge_gen()
    ]["rate_ci95"]
    assert consistency_low < consistency_high


def test_a_three_vote_generation_and_a_two_vote_generation_report_side_by_side_never_pooled():
    """Every number added here partitions the way the matrix does. A judgment
    file outlives a protocol change and holds both, so a consistency rate, a
    gate-decided share, a voted-only rate or an interval computed across the
    two would describe a collection nobody ran.

    One task throughout, so every interval collapses to its point estimate and
    the expected numbers can be read straight off the fixture rather than off
    a resample.
    """
    judgments = [
        # v1: three votes, the draw split, and the two orders disagreed.
        *[_vote("run-a", "run-b", verdict, vote_index=index, sample_index=0,
                judge_prompt_version=1, position_assignment=position)
          for index, (verdict, position) in enumerate(
              (("a", "a_first"), ("b", "b_first"), ("a", "b_first")))],
        # v2: one judged comparison, both positions agreeing on b ...
        *_voted_pair("run-a", "run-b", "b", sample_index=0),
        # ... and one the ladder settled the other way.
        _gate("run-a", "run-b", "a", sample_index=1),
    ]

    summary = summarize(judgments, MODEL_OF)

    old, new = _judge_gen(prompt_version=1), _judge_gen()
    pair = ("model-one", "model-two")
    for key in ("position_consistency", "gate_decided_share", "elo_voted",
                "elo_ci95", "elo_voted_ci95"):
        assert set(summary[key]) == {old, new}, key

    assert summary["position_consistency"][old] == {
        "measurable": 1, "consistent": 0, "rate": 0.0, "rate_ci95": (0.0, 0.0),
        "single_position": 0, "position_unrecorded": 0,
    }
    assert summary["position_consistency"][new] == {
        "measurable": 1, "consistent": 1, "rate": 1.0, "rate_ci95": (1.0, 1.0),
        "single_position": 0, "position_unrecorded": 0,
    }
    # The v1 generation holds no gate-decided comparison and the v2 one is half
    # gate-decided. Pooled, both arms would read 1 of 3.
    assert summary["gate_decided_share"][old]["model-one"] == {
        "comparisons": 1, "gate_decided": 0,
    }
    assert summary["gate_decided_share"][new]["model-one"] == {
        "comparisons": 2, "gate_decided": 1,
    }
    # The decomposition is the same arithmetic on a three-vote comparison as on
    # a two-vote one: majority first, then voted-only over what the judge saw.
    assert summary["comparisons"][old][pair]["win_rate_x_voted"] == (
        pytest.approx(1.0)
    )
    assert summary["comparisons"][new][pair]["win_rate_x"] == (
        pytest.approx(0.5)
    )
    assert summary["comparisons"][new][pair]["win_rate_x_voted"] == (
        pytest.approx(0.0)
    )
    # One task per block, so each interval is that block's point estimate --
    # and the two points differ, which a pooled bootstrap could not report.
    assert summary["comparisons"][old][pair]["win_rate_x_ci95"] == (1.0, 1.0)
    assert summary["comparisons"][new][pair]["win_rate_x_ci95"] == (0.5, 0.5)


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
    # A real, EMPTY collection root rather than a path that does not exist: an
    # absent `runs/` is now a refusal (`CollectionNotFound`) that prints no
    # numbers, so it would be asserting the caveat over a batch that never ran.
    # An empty collection is the case this was reaching for -- zero units, one
    # caveat.
    fresh = tmp_path / "fresh"
    (fresh / "runs").mkdir(parents=True)
    for flags in ([], ["--no-rubric"], ["--re-judge"],
                  ["--judge-model", "openai.gpt-5.6-terra"],
                  ["--no-rubric", "--re-judge", "--samples", "0"]):
        main(["--event-log", str(fresh), *flags])
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


def test_the_spend_line_survives_the_same_summary_the_caveat_does(capsys):
    """Spend rides in the caveat's `finally`, for a related reason.

    `_print_reading` prints collection-derived text -- model names in the
    matrix, the profile and the Elo table -- so it raises PARTWAY through on a
    summary shaped by another reader, and (measured) on an ASCII-pinned stdout
    against a non-ASCII arm name. Everything sequenced after the block would be
    skipped, and the token totals are the one thing on this printout the
    operator cannot recover from anywhere else: the judgment file records no
    tokens and `costs.PRICE_BOOK` has no entry for the judge models. The batch
    has already been paid for by the time any of this runs, so losing the only
    record of what it cost to a formatting failure is the same trade the caveat
    already refuses.

    The ORDER is asserted too, because the fix must not reshuffle the happy
    path: the caveat still comes first, and spend still reads as a fact about
    the batch rather than as one of the numbers κ qualifies.
    """
    result = _empty_result()
    result["judge_usage"] = {
        "calls": 7, "prompt_tokens": 100, "completion_tokens": 20,
        "total_tokens": 120, "calls_without_usage": 0, "auth_refreshes": 0,
    }
    del result["summary"]["elo"]

    with pytest.raises(KeyError):
        print_summary(result)

    out = capsys.readouterr().out
    assert KAPPA_CAVEAT in out
    assert "7 completion request(s)" in out
    assert out.index(KAPPA_CAVEAT) < out.index("7 completion request(s)")


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
    such flag, and this fails the moment one appears.

    THE POSITIVE ASSERTIONS ARE THE TEST'S OWN TRIPWIRE. A `not in` sweep over
    captured stdout passes perfectly on an EMPTY string, so anything that stops
    the help text reaching this capture -- a parser that writes it to stderr, a
    `SystemExit` raised before argparse formats anything, a `main` that grew an
    early return -- would leave the sweep asserting nothing while staying
    green. Two flags that must be there are asserted alongside, so the sweep is
    only ever read against a help text that actually arrived.

    Those two rather than any two: `--event-log` is required, so its absence is
    a driver nobody can invoke, and `--max-consecutive-errors` is the operator's
    only escape from a collection whose deterministic failures abort every
    resume in the same place.
    """
    with pytest.raises(SystemExit):
        main(["--help"])

    text = capsys.readouterr().out.lower()
    assert "--event-log" in text
    assert "--max-consecutive-errors" in text
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


def test_every_number_the_summary_gained_prints_above_the_kappa_caveat(capsys):
    """The caveat qualifies "every number above", so a number printed after it
    is a number reported without it -- and these four are the ones a reader is
    most likely to carry into a decision: an interval, the rate with the
    ladder's comparisons taken out, how much of the block the ladder decided,
    and whether the judge said the same thing in both positions.

    Asserted by POSITION, not by presence. `_print_reading` is one function and
    the caveat is in `print_summary`'s `finally`, so a section appended in the
    wrong place still prints -- just underneath the line that qualifies it.
    """
    result = _empty_result()
    result["summary"] = summarize(
        [
            *_voted_pair("run-a", "run-b", "a", sample_index=0,
                         task_id="task-1"),
            *_voted_pair("run-a", "run-b", "b", sample_index=1,
                         task_id="task-2"),
            _gate("run-a", "run-b", "a", sample_index=2, task_id="task-2"),
        ],
        MODEL_OF,
    )

    print_summary(result)

    out = capsys.readouterr().out
    for marker in ("95%", "judge-voted only", "position consistency",
                   "gate-decided share"):
        assert marker in out, marker
        assert out.index(marker) < out.index(KAPPA_CAVEAT), marker
    # The interval itself, beside the rate and beside the rating. Asserted on
    # the COMBINED line by name rather than anywhere in the output: a blanket
    # search is satisfied by the judge-voted line underneath, which is how the
    # combined rate would lose its band and still pass.
    (combined,) = [
        line for line in out.splitlines()
        if line.strip().startswith("combined")
    ]
    assert re.search(r"model-one \d+\.\d% \[\d+\.\d%, \d+\.\d%\]", combined)
    assert re.search(r"model-one\s+\d+\.\d\s+\[\s*\d+\.\d,\s+\d+\.\d\]", out)
    # And beside the three numbers §10.3 would otherwise have left bare: the
    # judge-voted rate, the judge-voted rating and the consistency rate.
    (voted_line,) = [
        line for line in out.splitlines() if "judge-voted only  model" in line
    ]
    assert re.search(r"\[\d+\.\d%, \d+\.\d%\]", voted_line)
    (consistency_line,) = [
        line for line in out.splitlines() if "position consistency" in line
    ]
    assert re.search(r"\[\d+\.\d%, \d+\.\d%\]", consistency_line)
    assert re.search(
        r"judge-voted only\s+\d+\.\d\s+\[\s*\d+\.\d,\s+\d+\.\d\]", out
    )


def test_the_printed_row_carries_the_second_arms_band_as_the_first_arms_mirror(
    capsys,
):
    """The two rates on a row sum to 1 in every resample, so y's band is x's
    mirrored -- `[1 - high, 1 - low]`, ends SWAPPED. Written `[1 - low, 1 -
    high]` it comes out inverted, low above high, and every other assertion in
    this file stays green: the numbers are individually right and the interval
    is nonsense. This is the one test that reads the printed band rather than
    the dict, because the mirror exists only on the terminal.
    """
    result = _empty_result()
    result["summary"] = summarize(
        [
            vote
            for task in range(6)
            for vote in _voted_pair("run-a", "run-b",
                                    "a" if task % 3 else "b",
                                    task_id=f"task-{task}")
        ],
        MODEL_OF,
    )

    print_summary(result)

    row = result["summary"]["comparisons"][_judge_gen()][
        ("model-one", "model-two")
    ]
    low, high = row["win_rate_x_ci95"]
    assert low < high
    (combined,) = [
        line for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("combined")
    ]
    printed = re.findall(r"\[(\d+\.\d)%, (\d+\.\d)%\]", combined)
    assert len(printed) == 2
    for band in printed:
        assert float(band[0]) < float(band[1])
    assert printed[0] == (f"{low:.1%}"[:-1], f"{high:.1%}"[:-1])
    assert printed[1] == (f"{1 - high:.1%}"[:-1], f"{1 - low:.1%}"[:-1])


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


# ---------------------------------------------------------------------------
# the codex backend: selection, record honesty, and the abort it needs
# ---------------------------------------------------------------------------

CODEX_JUDGE = "codex:gpt-5.2-codex"


def _codex_home(tmp_path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    return home


def _fake_harness(monkeypatch, tmp_path):
    """`codex_harness` without a binary to read a version off.

    Patched at the name `scripts.judge` imported rather than in the backend
    module: the driver holds its own reference, so patching the source would
    leave the driver calling the real one.
    """
    home = _codex_home(tmp_path)
    monkeypatch.setenv("BAKEOFF_CODEX_HOME", str(home))
    monkeypatch.setattr(
        judge_script,
        "codex_harness",
        lambda judge_model_id, reasoning_effort=None: {
            "backend": "codex-cli",
            "codex_cli_version": "codex-cli 0.145.0-alpha.18",
            "codex_model": judge_model_id.split(":", 1)[1],
            "auth_mode": "chatgpt",
            "auth_seat": "pindrop-chatgpt-business",
            "sandbox": "read-only",
            "reasoning_effort": reasoning_effort,
        },
    )
    return home


def test_the_backend_is_read_off_the_id_because_a_flag_could_disagree_with_it():
    assert judge_script._judge_backend(CODEX_JUDGE) == "codex"
    assert judge_script._judge_backend(JUDGE_MODEL_ID_DEFAULT) == "mantle"


def test_a_codex_judge_id_passes_the_neutrality_guard_and_a_codex_claude_does_not(
    tmp_path, monkeypatch
):
    """Neutrality is about the FAMILY, and the codex namespace changes neither
    half of the guard: a GPT-class judge still sits outside all four compared
    families, and a compared family re-hosted under `codex:` is still refused.
    """
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path)

    result = _run(root, judge_model_id=CODEX_JUDGE)
    assert result["errors"] == []

    with pytest.raises(NonNeutralJudge):
        _run(root, judge_model_id="codex:claude-sonnet-5")


def test_codex_lines_never_claim_temperature_zero_and_carry_the_harness(
    tmp_path, monkeypatch
):
    """The record says what was SENT, and codex cannot send a temperature.

    A line that copied `JUDGE_SAMPLING` in would claim exact reproducibility
    for a call that has none, in the one field whose whole job is to be true
    about the request -- and nothing downstream could tell, because the value
    would parse and the number it implies is the number a mantle line carries.
    """
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path)

    _run(root, judge_model_id=CODEX_JUDGE)
    paid = [line for line in _lines(root) if line.judge_prompt_sha]
    assert paid

    for line in paid:
        assert "temperature" not in line.judge_sampling
        assert line.judge_sampling == {}
        assert line.judge_harness["backend"] == "codex-cli"
        assert line.judge_harness["codex_model"] == "gpt-5.2-codex"
        assert line.judge_harness["auth_seat"] == "pindrop-chatgpt-business"
        # The only identity the ~14.6k tokens of un-hashed Codex scaffolding
        # have. Without it `judge_prompt_sha` silently attests half the input.
        assert line.judge_harness["codex_cli_version"]


def test_the_reasoning_effort_reaches_the_record_as_sent(tmp_path, monkeypatch):
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path)

    _run(root, judge_model_id=CODEX_JUDGE, reasoning_effort="high")
    paid = [line for line in _lines(root) if line.judge_prompt_sha]

    assert all(
        line.judge_sampling == {"model_reasoning_effort": "high"} for line in paid
    )
    assert all(line.judge_harness["reasoning_effort"] == "high" for line in paid)


def test_mantle_lines_still_carry_the_pinned_sampling_block_unchanged(tmp_path):
    """The swap must not move the mantle path by one byte: a collection judged
    before and after this change has to produce identical lines, or every
    stored mantle verdict becomes a different generation for no reason."""
    root = _two_arms(tmp_path)

    _run(root)
    paid = [line for line in _lines(root) if line.judge_prompt_sha]

    assert paid
    for line in paid:
        assert line.judge_sampling == dict(JUDGE_SAMPLING)
        assert line.judge_sampling["temperature"] == 0.0
        assert line.judge_harness == {"backend": "litellm-mantle"}


def test_a_gate_decided_line_records_no_harness_because_nothing_carried_it(
    tmp_path, monkeypatch
):
    """Nothing was sent, so all three as-sent fields are empty TOGETHER.

    A harness block here would additionally resolve a credential for a line
    that needs none -- which is the property that lets an analysis-only pass
    run with no seat at all.
    """
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path, resolved_b=False)

    _run(root, judge_model_id=CODEX_JUDGE, rubric=False)
    (line,) = [ln for ln in _lines(root) if ln.verdict == "gate_decided"]

    assert line.judge_harness is None
    assert line.judge_sampling == {}
    assert line.judge_prompt_sha == ""


def test_a_fully_gate_decided_codex_batch_never_needs_a_seat(tmp_path, monkeypatch):
    """The laziness that matters: no binary, no judge home, no auth.json.

    `codex_completion` refuses at its first call when any of the three is
    missing, so a batch the deterministic ladder settles entirely must reach
    neither the closure nor the harness. Every environment variable the
    backend reads is unset here, and the pass must still produce its line.
    """
    monkeypatch.delenv("BAKEOFF_CODEX_HOME", raising=False)
    monkeypatch.delenv("BAKEOFF_CODEX_BIN", raising=False)
    root = _two_arms(tmp_path, resolved_b=False)

    # No injected seam: the driver builds the real lazy codex one.
    result = judge_event_log(
        root, [_task()], judge_model_id=CODEX_JUDGE, rubric=False
    )

    assert result["errors"] == []
    (line,) = [ln for ln in _lines(root) if ln.verdict == "gate_decided"]
    assert line.judge_model_id == CODEX_JUDGE


def test_codex_and_mantle_verdicts_in_one_file_are_never_pooled(tmp_path, monkeypatch):
    """Judging one collection under both backends leaves two GENERATIONS.

    This is the whole reason the backend is inferred from the id rather than
    set by a flag: `judge_model_id` is the first element of every resume key
    and of `_judge_generation`, so a codex id cannot collide with a mantle one
    and no aggregation can average the two into a number that describes
    neither.
    """
    _fake_harness(monkeypatch, tmp_path)
    root = _two_arms(tmp_path)

    _run(root, rubric=False)
    before = len(_lines(root))
    # No `--re-judge`: every unit is fresh under the other generation.
    result = _run(root, judge_model_id=CODEX_JUDGE, rubric=False)

    # Nothing was skipped: the codex generation's resume keys differ in
    # `judge_model_id`, so every unit is fresh without `--re-judge`.
    assert not result["skipped"]
    assert len(_lines(root)) > before
    generations = {
        judge_script._judge_generation(line) for line in _lines(root)
    }
    assert len(generations) == 2


def test_the_abort_paragraph_names_codex_login_and_never_an_aws_session(tmp_path):
    """A codex 401 is answered by a browser flow, and by nothing this process
    can do. Sending that operator to `aws sso login` names a credential the
    pass never used, which is the failure `_abort_message`'s classification
    was built to stop -- one backend further on."""
    from bakeoff.codex_judge import CodexAuthFailure

    run = [("vote 0", True)] * 5
    codex = judge_script._abort_message(run, backend="codex")
    mantle = judge_script._abort_message(run, backend="mantle")

    assert "codex" in codex.lower() and "login" in codex
    assert "aws sso" not in codex
    assert "aws sso" in mantle
    # The one classifier both backends answer to.
    assert is_auth_failure(CodexAuthFailure("nope"))


def test_the_codex_rate_limit_paragraph_says_the_backoff_already_lost(tmp_path):
    """A 429 that reaches the breaker on this backend is not a blip: the
    backend's own backoff spent five waits first, so "wait for the window"
    has to mean a longer wait than it does on mantle."""
    run = [("vote 0", False)] * 5

    codex = judge_script._abort_message(run, backend="codex")

    assert "backoff was exhausted" in codex
