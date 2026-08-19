#!/usr/bin/env python3
"""Judge a stored collection: one line per verdict, beside two files it only
reads. See `docs/superpowers/specs/2026-08-18-judge-design.md`, section
"`scripts/judge.py` -- the batch driver", and spec sections 4.2.3 and 4.3.

The judge RANKS; the deterministic ladder GATES, and the two are never averaged.
So this driver is a sibling of `scripts/grade.py` rather than a stage of it: it
reads the event log and `grades/grades.jsonl`, writes
`<event-log>/judgments/judgments.jsonl` and the gzipped payloads beside it, and
opens neither input for writing. Two derived views, two files, no cross-writes.

THE ORDER IS THE CONTENT
------------------------

Every step below is placed against a specific way a judging pass produces
confident, well-formed, wrong output:

1. **Malformed judgments refuse the resume** unless `--re-judge`. An unreadable
   judgment is not an absent one, and a resume that confused the two would pay a
   second time for a verdict already bought -- then leave two lines for one
   comparison under one `(judge_model_id, judge_prompt_version, rubric_version)`,
   which is the shape the file reserves for a deliberate re-judge.
2. **Any malformed grade line refuses the batch outright**, and no flag cures it
   (plan ambiguity 8). An unreadable gate might BE the gate: a run read as
   ungraded is one the judge either drops from a pair -- silently changing which
   comparisons exist -- or, if the damage sat on the losing side, one it ranks.
3. **The gating view is the LAST line per `run_id` in file order.** A re-grade
   appends rather than replaces, so a run can hold several lines, and the last
   one is the verdict a later reader lands on.
4. **Excluded runs (`resolved is None`) are dropped BEFORE grouping.** Such a
   row is not an observation of the model; ranking it ranks the infrastructure.
   Dropping after grouping would leave it in a cell, where it would either be
   paired or silently change which model won the max-attempt tiebreak.
5. **A run with no grade line is warned about and never judged.** Rescuing an
   ungraded run is out of scope -- with no gate to defer to there is nothing for
   the judge to be blind about.
6. **The rubric gate is asserted immediately before the append**, not at
   selection. The file is append-only, so a rubric line about a run the ladder
   failed is a permanent number somebody averages into a resolve rate. This is
   §4.2.3's central rule and it gets `grade.py`'s `_assert_verdict_invariant`
   treatment: a violation is a DRIVER bug and raises.
7. **`write_payload` runs BEFORE `append_judgment`.** A line pointing at a
   payload that failed to write is a verdict naming an input nobody can read,
   which breaks "a re-judge is a re-score, not a re-run" (§4.3) exactly as a
   missing payload does while looking like a present one. The reverse order
   fails safe: no line, and the unit is retried on the next pass.

THE EXIT CONTRACT
-----------------

Exit 0 means every selected unit produced its lines. Exit 1 means at least one
did not, or the resume was refused. A unit is one rubric call, one pairwise
vote, or one gate-decided pair -- the same granularity the resume is keyed on --
and each is wrapped in a bare `except Exception` so that one unreadable diff,
one model that cannot produce JSON, or one payload that tripped the secret scan
costs its own line and not the rest of the batch. `MalformedVerdict` after
exhausted retries lands here: an error, no line, exit 1. A payload that tripped
the scan costs its line and NOTHING ELSE -- the scan runs on the built payload
before the prompt is rendered (`bakeoff.judge`), so that unit makes no call, is
never billed, and leaves no file.

That per-unit isolation has one failure mode of its own, and
`MAX_CONSECUTIVE_ERRORS` is placed against it: when the cause is systemic
rather than per-unit -- a credential that expired and could not be re-minted,
against a pass that runs for hours past a ~1h window -- every remaining unit
fails identically, and the driver would grind through thousands of them
printing an error line for each. A run of consecutive unit errors aborts the
batch instead, with the abort recorded in `warnings`. Exit 1, and the resume
picks up the same command where it stopped.

The breaker has one failure mode of ITS own, and three things are placed
against it. A collection holding a run of units that fail DETERMINISTICALLY --
one record with no `final_diff` takes every pair its arm is in -- aborts, and
the resume attempts the same units in the same order and aborts identically,
forever. So the limit is `--max-consecutive-errors` rather than a constant; the
abort names every unit in the failing run by task, sample and arm rather than
by run-id hash, so the operator can act on it with `--only-task`; and it asks
`is_auth_failure` what the run was actually about instead of asserting a
credential problem it cannot see. A unit whose run yields no judgeable payload
is pre-filtered out of the breaker entirely -- it never reached the judge, so
it is no evidence about the judge.

THE SUMMARY IS A PRINTOUT, NOT A STORED SCORE
---------------------------------------------

`summarize` reads the judgment file back and says what it holds: a pairwise
win-rate matrix per model pair (primary), a rubric PROFILE per model
(diagnostic -- per-dimension means with the count each was taken over, plus
flag rates; never summed, never joined to `resolved`), and an Elo table that
FOLLOWS the win rates rather than adjudicating them. All three are partitioned
by JUDGE GENERATION -- `(judge_model_id, judge_prompt_version,
rubric_version)` -- and never pooled across one: a re-judge under a second
oracle is a second reading of the same collection, so pooling it would double
every comparison count and report each rate as the mean of two verdicts nobody
gave together. Nothing it computes is written anywhere, on `grade.py
summarize`'s precedent and for a sharper reason: a stored Elo is a published
number, and §4.3 forbids publishing one without the κ that was in force.

Which is why `print_summary` ends every run with `KAPPA_CAVEAT`,
unconditionally. OPEN-5 is blocked on people rather than on code, an unmeasured
κ is below 0.6 by construction, and below 0.6 the number is directional only. A
flag is how that line gets dropped, so there is no flag.

WHAT IS LAZY, AND WHY IT MATTERS
--------------------------------

The live judge is built on the FIRST call that needs one. A batch whose
comparisons the ladder already settled must not mint a credential for a judge it
never asks -- gate-decided pairs cost nothing, which is the point of them -- and
minting at construction starts a credential clock (~1h in practice) before the
first vote, so a long pass would reach it holding a dead token. `live_completion`
is itself lazy about its router; this closure is lazy about `live_completion`.

Usage:
    python scripts/judge.py --event-log ~/.cache/bakeoff/eventlog
    python scripts/judge.py --event-log PATH --only-task calc-1 --samples 0 1
    python scripts/judge.py --event-log PATH --no-rubric

Exit codes:
    0  every selected unit produced its lines
    1  at least one unit errored, or the batch was refused
"""

from __future__ import annotations

import argparse
import itertools
import math
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bakeoff.eventlog import EventLog  # noqa: E402
from bakeoff.grade_schema import GradeRecord, load_grades  # noqa: E402
from bakeoff.judge import (  # noqa: E402
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    RUBRIC_VERSION,
    VOTE_POSITIONS,
    CompleteFn,
    PayloadInputs,
    is_auth_failure,
    judge_pair_vote,
    judge_rubric,
    live_completion,
    majority,
    payload_inputs_from,
    prompt_sha,
)
from bakeoff.judge_schema import (  # noqa: E402
    JudgeRecord,
    append_judgment,
    load_judgments,
    payload_relative_path,
    resolve_payload_path,
    write_payload,
)
from bakeoff.tasks import TaskError, load_task_set  # noqa: E402

# Imported, never restated. `grade.py` owns where the grade file lives and this
# driver's whole gate comes out of it, so a second copy of the path is how a
# moved grade file turns into a judging pass that reports "nothing is graded"
# and judges nothing -- silent, and in the direction that looks like success.
# The import costs `scripts.grade`'s module graph (which reaches `docker`, an
# import with no side effects and a declared dependency) and buys one
# derivation. Same reasoning as `grade.py` importing `preflight_cache_key`.
from scripts.grade import grades_path  # noqa: E402

DEFAULT_TASK_SET = REPO / "taskset"

#: How many ids a warning names before it stops listing them. A collection is
#: ~2,400 runs at full N -- 60 tasks x 10 samples x 4 arms, which is where the
#: 2,400 rubric calls below come from -- and a warning that prints all of them
#: is one nobody reads.
_MAX_NAMED = 10

#: The three verdicts a model may return, as `majority` accepts them.
#: "gate_decided" is deliberately ABSENT: it is the one verdict no model
#: produced, and inside this vocabulary it becomes a fourth thing the judge
#: said -- which puts every pair the ladder settled into the denominator of a
#: rate about what the judge preferred. `_CANONICAL_VERDICTS` in
#: `bakeoff.judge` is the same three words; the test file pins them equal
#: rather than this module importing another module's private name.
_VOTE_VERDICTS: tuple[str, ...] = ("a", "b", "tie")

#: How many unit errors IN A ROW abort the batch, unless
#: `--max-consecutive-errors` says otherwise.
#:
#: The failure this is placed against: the mantle bearer token's real window is
#: about an hour -- the Identity Center session policy caps it well below the
#: token's own TTL -- and a real 60-task pass is ~9,600 calls over many more
#: hours than that: 60 tasks x 10 samples x 6 pairs x 2 forced positions is
#: 7,200 pairwise, plus 2,400 rubric at full N. `live_completion` re-mints ONCE
#: PER EXPIRY -- one auth failure buys one fresh credential and the retry runs
#: on it, so a pass that outlives three tokens mints three -- which covers an
#: expiry; it cannot cover a revoked role, a dead `aws sso` session or an
#: endpoint that has stopped accepting this principal at all. In those cases
#: the auth error propagates past `_ask_and_parse`, the per-unit `except`
#: records it and continues, and the driver grinds through every remaining unit
#: on a credential that will never work -- thousands of paid-looking attempts
#: and thousands of error lines, with the one message that matters buried at
#: the top.
#:
#: Five, and CONSECUTIVE: a systemic failure fails every unit it touches from
#: the moment it starts, while a flaky endpoint or one unreadable diff produces
#: scattered errors a long batch must survive. Any successful unit resets the
#: count. Gate-decided pairs make no call and so are neither -- see
#: `_BatchAborted`.
#:
#: A DEFAULT rather than a rule, which is the other half of the design. A
#: credential is not the only way to fail five units in a row: a collection
#: holding five permanently-failing units fails them on every pass, so the
#: resume attempts the same units in the same order and aborts in the same
#: place, and no sequence of resumes ever reaches the work behind them. That is
#: a deadlock and it needs a way out that is not a code change, so the limit is
#: an operator flag and the abort message says which units failed, whether they
#: were credential failures, and -- when they were not -- that raising this
#: number or narrowing `--only-task` is the fix.
MAX_CONSECUTIVE_ERRORS = 5

#: The reporting scale for `elo_from_outcomes`, whose fit is Bradley-Terry
#: rather than Elo. CONSTANTS rather than flags: neither is a tuning knob, both
#: are part of what the printed number means, and a rating reported under an
#: unrecorded scale is a number nobody can reproduce.
#:
#: 400/log10 is Elo's spacing, kept because it is the one rating scale a reader
#: already has an intuition for -- 400 points is a 10:1 strength ratio, ~70
#: points is 60/40. The ANCHOR is presentation only: Bradley-Terry identifies
#: the DIFFERENCES between strengths and nothing else, so the overall level is
#: free and would otherwise wander with the arm set.
ELO_SCALE = 400.0
ELO_ANCHOR = 1000.0

#: Convergence for the MM/Zermelo iteration below. The tolerance is on the
#: max-abs strength change between iterations, well inside the precision any
#: printed rating shows. The cap is a HALT, not a target: the iteration is
#: monotone in the likelihood and converges in tens of rounds at this scale, so
#: reaching 10,000 means a fixture nobody anticipated (a disconnected
#: comparison graph is the candidate) and an infinite loop inside a summary is
#: worse than an imprecise rating.
_BT_TOLERANCE = 1e-10
_BT_MAX_ITERATIONS = 10_000

#: §4.3: at κ below 0.6 a judge-derived number is directional only, and an
#: UNMEASURED κ is below 0.6 by construction -- OPEN-5 is blocked on people, not
#: on code, so nothing in this repository can raise it. Hard-coded and printed
#: unconditionally: a flag is how this line gets dropped, and the printout it
#: qualifies is the only place a reader meets these numbers.
KAPPA_CAVEAT = (
    "κ unmeasured (OPEN-5) — every number above is directional only and "
    "must not carry a decision."
)

#: The same sentence for a stdout that cannot encode it. An ASCII terminal
#: turns `print(KAPPA_CAVEAT)` into a `UnicodeEncodeError`, which drops the
#: caveat and takes the exit path down with it -- the failure this line exists
#: to prevent, arrived at by an environment variable instead of a flag.
_KAPPA_CAVEAT_ASCII = (
    "kappa unmeasured (OPEN-5) -- every number above is directional only and "
    "must not carry a decision."
)


class ResumeRefused(RuntimeError):
    """The judgment file or the grade file could not be read completely, so the
    batch cannot be trusted to know what it already has -- or what the gate said.

    Its own class rather than `scripts.grade`'s, on that module's precedent: the
    two drivers refuse for related reasons over different files, and a shared
    class would make a `except ResumeRefused` around one of them catch the
    other's refusal about a file it never touched.
    """


class _BatchAborted(RuntimeError):
    """`max_consecutive_errors` units failed in a row, so the loop stops.

    PRIVATE and control flow only: it never leaves `judge_event_log`, which
    catches it, records the abort in `warnings` and returns the partial batch
    exactly as if the loop had run out of cells. The operator gets the summary
    over everything already judged, the errors that were recorded, and one
    message saying the pass stopped early -- rather than a traceback and no
    reading at all.

    Raised from the per-unit handlers, which are siblings rather than nested,
    so nothing catches this on the way out. The gate-decided handler does not
    raise it and does not reset the count either: that unit makes no call, so
    it is no evidence the credential recovered, and treating it as a success
    would mean a collection full of failed-gate arms never trips the breaker.

    ATTEMPTED-AND-FAILED is the whole of what counts, and the two units that
    look like exceptions to that are the two that make the rule readable. A
    unit the resume SKIPS was bought by an earlier pass and never attempted
    here: it cannot trip the breaker, and it cannot reset it either. Resetting
    on a skip is the tempting fix for the deadlock the flag exists for, and it
    is the wrong one -- the pass that follows an abort is skip-heavy by
    construction, so a counter a skip could reset would never reach the limit
    again and the breaker would be off for exactly the pass it was written for.
    A unit whose run yields no judgeable payload is PRE-FILTERED before the
    `try` for the same reason from the other side: it is not evidence about the
    judge, and counting it let one unjudgeable run abort a batch with nothing
    else wrong.

    The message is built from the failing run itself -- every unit named, and
    `is_auth_failure` asked about each -- so it diagnoses rather than guesses.
    See `_abort_message`.
    """


class GateInvariantError(RuntimeError):
    """A rubric `JudgeRecord` was about to be written for a run whose
    `GradeRecord.resolved` is not `True`.

    A violation is a DRIVER bug, and the file is append-only: a line written
    once is permanent. §4.2.3's central rule is that the judge never rescues a
    failed gate, and a rubric profile sitting beside `resolved is False` is
    precisely the number a downstream view folds into a resolve rate. `None` is
    the same refusal for a different reason -- an excluded run is not an
    observation of the model at all.
    """


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def judgments_path(event_log_root: Path | str) -> Path:
    return Path(event_log_root) / "judgments" / "judgments.jsonl"


def payloads_root(event_log_root: Path | str) -> Path:
    """The gzipped payloads, BESIDE the jsonl rather than inside it.

    Payloads carry two full diffs and would dominate the line file. Kept under
    the same `judgments/` directory so the verdicts and the inputs they were
    derived from move as one thing -- copying the jsonl without this directory
    produces a file of verdicts that can no longer be re-scored (§4.3).

    This directory MUST sit under the judgments directory and be named what
    `judge_schema.payload_relative_path` says, because that is the value a line
    stores and every reader joins. The two are not shared through a constant
    and do not need to be: `_stored_payload_path` resolves the stored value the
    way a reader would and compares it against the file that was written, on
    every line -- so moving or renaming this directory is a loud failure on the
    first unit rather than a file of paths that resolve to nothing.
    """
    return Path(event_log_root) / "judgments" / "payloads"


def _stored_payload_path(
    judgments_dir: Path, judgment_id: str, written: str
) -> str:
    """The relative value the line stores, checked against what was written.

    `write_payload` returns an absolute path and the record keeps
    `payloads/<judgment_id>.json.gz`, so the two are no longer the same string
    and "the line stores what was written" has stopped being true by
    inspection. This is what keeps it true by construction, and it asks the
    question a READER will ask rather than a cheaper one: resolved the way
    every reader resolves it, does the stored value name the file that was
    actually written?

    Resolved, and not compared component-wise. A check on the last two
    components alone passes for a `payloads/` relocated anywhere at all -- out
    of `judgments/`, or into another collection -- while the stored value goes
    on being joined against the judgments directory, where nothing is. That is
    exactly the file of dead paths this check exists to prevent, so the weaker
    version would have been a check that agreed with the bug.

    `judgments_dir` is the directory holding the jsonl, which is the directory
    a reader resolves against: they open the judgment file and join its
    siblings. Deriving it from `payloads` instead would compare the driver's
    own value against itself.

    A check and not a derivation, either. Slicing the relative value OFF the
    returned path is what a caller writes once it has stopped checking, and it
    would happily record `payloads/x.json.gz` for a payload written into a
    directory called something else entirely.
    """
    relative = payload_relative_path(judgment_id)
    assert resolve_payload_path(judgments_dir, relative) == Path(written), (
        f"payload for {judgment_id} was written to {written!r}, which is not "
        f"where {relative!r} resolves under {str(judgments_dir)!r}: the stored "
        "path would name a file no reader can find"
    )
    return relative


# ---------------------------------------------------------------------------
# the gate the judge defers to
# ---------------------------------------------------------------------------


def gating_view(grades: list[GradeRecord]) -> dict[str, GradeRecord]:
    """One `GradeRecord` per `run_id` -- the LAST line, in file order.

    The grade file is append-only and a re-grade appends rather than replaces,
    so a run can hold several lines under several grader versions. The last one
    is the current verdict, on the same reasoning `grade.py`'s resume uses to
    skip: it is the one a later reader of the file would land on.

    Deliberately NOT keyed on `grader_version` as `grade.py`'s audit is. That
    function is answering "how did this collection grade", where two versions
    disagreeing about one run is the finding worth keeping. This one is
    answering "may this run be judged, and against what", which admits exactly
    one answer per run -- and `grade_version_seen` on the judgment records which
    generation gave it.
    """
    view: dict[str, GradeRecord] = {}
    for grade in grades:
        view[grade.run_id] = grade
    return view


def _assert_rubric_gate(grade: GradeRecord, run_id: str) -> None:
    """The last thing between a driver bug and a permanent line. See
    `GateInvariantError`."""
    if grade.resolved is not True:
        raise GateInvariantError(
            f"{run_id}: a rubric judgment was about to be written for a run "
            f"whose grade says resolved={grade.resolved!r}. The judge never "
            "rescues a failed gate and never ranks an excluded run, and this "
            "file is append-only, so the line would be permanent"
        )


# ---------------------------------------------------------------------------
# resume identity
# ---------------------------------------------------------------------------


def _resume_key(judgment: JudgeRecord) -> tuple:
    """What makes two lines the same unit of work.

    TUPLES OF SCALARS, never records: `JudgeRecord` carries dict fields on a
    frozen dataclass and is therefore unhashable, so a set of records would
    raise on the first insert rather than skip anything.

    The three version fields are the whole gate. A verdict is only reproducible
    against the model that gave it, the prompt text it saw and the rubric it was
    scored under, so a change to any of the three makes a NEW generation of
    verdict rather than a resume -- and the disagreement between the two lines
    is the finding the file exists to keep.

    A gate-decided pair carries `vote_index=None` and so resumes as its own
    unit. Keying it on 0 would make it collide with the first vote of a pair
    that later becomes judgeable under a re-grade, and a second pass would write
    it again beside itself.

    An unrecognised `kind` -- a hand-edited line -- gets a key that can match
    nothing, so it can neither be mistaken for a completed unit nor collide with
    one whose fields happen to be `None`.
    """
    versions = (
        judgment.judge_model_id,
        judgment.judge_prompt_version,
        judgment.rubric_version,
    )
    if judgment.kind == "rubric":
        return ("rubric", judgment.run_id) + versions
    if judgment.kind == "pairwise":
        return (
            "pairwise",
            judgment.task_id,
            judgment.sample_index,
            judgment.run_id_a,
            judgment.run_id_b,
            judgment.vote_index,
        ) + versions
    return ("unreadable-kind", judgment.judgment_id, judgment.kind)


def _rubric_key(run_id: str, judge_model_id: str) -> tuple:
    return ("rubric", run_id, judge_model_id, JUDGE_PROMPT_VERSION,
            RUBRIC_VERSION)


def _pairwise_key(task_id: str, sample_index: int, run_id_a: str,
                  run_id_b: str, vote_index: int | None,
                  judge_model_id: str) -> tuple:
    return ("pairwise", task_id, sample_index, run_id_a, run_id_b, vote_index,
            judge_model_id, JUDGE_PROMPT_VERSION, RUBRIC_VERSION)


# ---------------------------------------------------------------------------
# the seams
# ---------------------------------------------------------------------------


def lazy_live_completion(judge_model_id: str) -> CompleteFn:
    """`live_completion`, built on the first prompt that needs it.

    The `task_resolver` pattern (`scripts/grade.py:317`) one layer further out.
    `live_completion` already defers its ROUTER; this defers `live_completion`
    itself, which is what a fully gate-decided batch needs: that function
    resolves `scripts.smoke_bedrock` at construction, and a batch that makes no
    call should not require the credential module to be importable at all.

    A one-slot dict rather than `nonlocal`, so there is no rebind to get wrong.
    Not thread-safe (check-then-set), which is fine while the two votes of a
    comparison are sequential and is the same shape `live_completion`'s own
    cache has.
    """
    built: dict[str, CompleteFn] = {}

    def complete(prompt: str) -> str:
        if "fn" not in built:
            built["fn"] = live_completion(judge_model_id)
        return built["fn"](prompt)

    return complete


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# the three kinds of line
# ---------------------------------------------------------------------------


def _rubric_line(record, grade: GradeRecord, task, inputs: PayloadInputs,
                 payloads: Path, path: Path, complete: CompleteFn,
                 judge_model_id: str) -> JudgeRecord:
    """One absolute-rubric profile: one call, `vote_index=0`.

    The forced positions are pairwise-only: there is no second submission to
    order, so `VOTE_POSITIONS` has nothing to say here. The rubric is
    diagnostic rather than decisive, it is reported per model as a profile and
    never summed into a rank, and one call per run is what the spec's cost math
    assumes (4 rubric calls per task-sample).

    `write_payload` first, then the gate assert, then the append. The payload is
    required, not optional (§4.3), and the assert sits in the last position
    before the permanent write for the reason `GateInvariantError` gives.

    What lands on the line is the RELATIVE path, checked against the absolute
    one `write_payload` returned -- see `_stored_payload_path`.
    """
    payload, rendered, result = judge_rubric(inputs, complete)
    judgment_id = uuid.uuid4().hex
    written, payload_sha = write_payload(payloads, judgment_id, payload)
    # `path.parent` is the judgments directory: the jsonl's own directory is
    # what a reader joins a stored payload path against.
    payload_path = _stored_payload_path(path.parent, judgment_id, written)

    judgment = JudgeRecord(
        judgment_id=judgment_id,
        judged_at=_now(),
        judge_model_id=judge_model_id,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        # Of the EXACT text this call sent, carried out of `judge_rubric`
        # rather than re-rendered: a sha over a rebuilt prompt attests to
        # something other than what the model saw.
        judge_prompt_sha=prompt_sha(rendered),
        judge_sampling=dict(JUDGE_SAMPLING),
        rubric_version=RUBRIC_VERSION,
        kind="rubric",
        task_id=task.task_id,
        run_id=record.run_id,
        dimension_scores=dict(result.dimension_scores),
        flags=dict(result.flags),
        # `sample_index` stays `None` here. The schema splits its fields into a
        # rubric half and a pairwise half with `kind` saying which is populated,
        # and sample-vs-sample pairing is a pairwise claim; the run's own index
        # is one `EventLog.read_run` away for any reader that wants it.
        vote_index=0,
        full_reasoning_text=result.reasoning,
        input_payload_path=payload_path,
        input_payload_sha=payload_sha,
        grade_version_seen={record.run_id: grade.grader_version},
        graded_against_task_set_commit=task.task_set_commit,
    )

    _assert_rubric_gate(grade, record.run_id)
    append_judgment(path, judgment)
    return judgment


def _vote_line(task, sample_index: int, run_id_a: str, run_id_b: str,
               inputs_a: PayloadInputs, inputs_b: PayloadInputs,
               vote_index: int, position_assignment: str,
               grade_a: GradeRecord, grade_b: GradeRecord,
               payloads: Path, path: Path,
               complete: CompleteFn, judge_model_id: str) -> JudgeRecord:
    """One blind pairwise vote, in the position the caller forces.

    `inputs_a`/`inputs_b` are CANONICAL a and b -- the two run ids
    lexicographically sorted -- and `judge_pair_vote` applies the position and
    the inversion back out of it, so nothing here reorders anything. A driver
    that swapped the sides on the way in would produce a `position_assignment`
    that no longer describes the stored payload, and every position-consistency
    figure taken off the file would be computed over votes nobody sent in those
    orders.

    `position_assignment` is passed down AND stored, rather than stored and
    re-derived from `vote_index` by a reader: the record is evidence about what
    this call was shown, and a derived value is a claim about what the driver
    does now.

    `write_payload` BEFORE `append_judgment`: see the module docstring, step 7.
    The stored path is relative and is checked against the absolute one that
    was written -- see `_stored_payload_path`.
    """
    outcome = judge_pair_vote(
        inputs_a, inputs_b, position_assignment, complete
    )
    judgment_id = uuid.uuid4().hex
    written, payload_sha = write_payload(
        payloads, judgment_id, outcome.payload
    )
    payload_path = _stored_payload_path(path.parent, judgment_id, written)

    judgment = JudgeRecord(
        judgment_id=judgment_id,
        judged_at=_now(),
        judge_model_id=judge_model_id,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        # Per CALL, not per comparison: position changes the text, so the two
        # votes over one pair carry two distinct shas.
        judge_prompt_sha=prompt_sha(outcome.rendered_prompt),
        judge_sampling=dict(JUDGE_SAMPLING),
        rubric_version=RUBRIC_VERSION,
        kind="pairwise",
        task_id=task.task_id,
        run_id_a=run_id_a,
        run_id_b=run_id_b,
        sample_index=sample_index,
        position_assignment=outcome.position_assignment,
        verdict=outcome.verdict,
        vote_index=vote_index,
        full_reasoning_text=outcome.reasoning,
        input_payload_path=payload_path,
        input_payload_sha=payload_sha,
        # Two entries, because the spec's scalar cannot describe a pair whose
        # two sides were gated by different grader generations.
        grade_version_seen={
            run_id_a: grade_a.grader_version,
            run_id_b: grade_b.grader_version,
        },
        graded_against_task_set_commit=task.task_set_commit,
    )

    append_judgment(path, judgment)
    return judgment


def _gate_decided_line(task, sample_index: int, run_id_a: str, run_id_b: str,
                       grade_a: GradeRecord, grade_b: GradeRecord,
                       path: Path, judge_model_id: str) -> JudgeRecord:
    """The one verdict no model produced.

    One side failed the deterministic gate and the other did not, so the
    objective result settles it and asking a judge would let it disagree with a
    fact. There is no call to record, and every field that would evidence one is
    `None` or empty TOGETHER -- filling any of them would assert a model call
    that never happened:

    * `position_assignment`, `vote_index`, `input_payload_path` and
      `input_payload_sha` are `None`, per the schema's own contract.
    * `judge_prompt_sha` is `""` because no prompt was rendered, and
      `judge_sampling` is `{}` because nothing was sent. Both are the
      as-sent evidence fields; a sha or a sampling block copied in from the
      constants would attest to a request that was never made.
    * `full_reasoning_text` is `""`. Driver-authored prose in the field
      reserved for a model's own words is indistinguishable from a verdict when
      the file is read in bulk, and `verdict == "gate_decided"` beside
      `gate_decided_by` already says everything there is to say.

    `judge_model_id`, `judge_prompt_version` and `rubric_version` ARE populated,
    even though no judge was asked, because they are the resume identity: this
    line is a unit of work and a later pass has to be able to see it is done.
    """
    judgment = JudgeRecord(
        judgment_id=uuid.uuid4().hex,
        judged_at=_now(),
        judge_model_id=judge_model_id,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_prompt_sha="",
        judge_sampling={},
        rubric_version=RUBRIC_VERSION,
        kind="pairwise",
        task_id=task.task_id,
        run_id_a=run_id_a,
        run_id_b=run_id_b,
        sample_index=sample_index,
        verdict="gate_decided",
        gate_decided_by="a" if grade_a.resolved is True else "b",
        grade_version_seen={
            run_id_a: grade_a.grader_version,
            run_id_b: grade_b.grader_version,
        },
        graded_against_task_set_commit=task.task_set_commit,
    )
    append_judgment(path, judgment)
    return judgment


# ---------------------------------------------------------------------------
# what a unit is called, and what a run of failed ones means
# ---------------------------------------------------------------------------


def _unit_label(kind: str, task_id: str, sample_index: int,
                models: Sequence[str], run_ids: Sequence[str]) -> str:
    """One unit, named in the fields an operator can actually act on.

    `vote 1 task=trucking-8 sample=0 gemma-4-31b vs kimi-k2-5
    (runs 3fbe890adfa040cb, 960aa0e97cc82488)`.

    THE TASK AND THE SAMPLE COME FIRST because they are the two the flags take:
    `--only-task` wants a task id and `--samples` wants an index. The error
    lines this replaces named the run ids and nothing else, and a run id is
    `sha256(task|model|sample|attempt)[:16]` (`runner.make_run_id`) -- so those
    four fields ARE the id's preimage, and printing only the digest left the
    operator unable to recover any of them. The one message whose job was to
    say what to do next was the one message that could not be used to do it.

    The ids stay, in FULL, and go last in parentheses. Full because 16 hex
    characters is short enough to print and truncating it would break the two
    things an id is for -- grepping the event log and `EventLog.read_run` --
    to save eight characters. Last because an id identifies a unit exactly and
    explains nothing, so it should not be what a reader scans past to reach the
    fields that do.

    `models` and `run_ids` are parallel, in the same order, and the caller is
    responsible for keeping them so -- canonical a/b is the two RUN IDS sorted,
    which is not the model order, so a caller that passed `sorted(cell)` beside
    `(run_id_a, run_id_b)` would print a label naming each arm as the other.

    `sample_index` is an `int` and not `int | None`. Every unit belongs to a
    cell and a cell is keyed `(task_id, sample_index)`, so there is no caller
    without one -- and the widened type would only ever print `sample=None`,
    which is the field failing at the one job it is here for.
    """
    who = " vs ".join(models)
    noun = "run" if len(run_ids) == 1 else "runs"
    return (
        f"{kind} task={task_id} sample={sample_index} {who} "
        f"({noun} {', '.join(run_ids)})"
    )


def _abort_message(failure_run: list[tuple[str, bool]]) -> str:
    """The abort, built out of the run of failures that caused it.

    `failure_run` is `(label, is_auth_failure(exc))` per unit, oldest first.
    Both halves are load-bearing and neither can be recovered later: the labels
    are what make the abort diagnosable at all, and the classification is what
    decides whether the operator is sent to their credential or to their data.

    THE OLD MESSAGE ASSERTED THE CREDENTIAL UNCONDITIONALLY. That is the right
    guess -- the breaker was placed against an expired token -- and it is a
    guess this function does not have to make, because the exceptions that
    caused the abort were in hand when it fired. A batch whose five failures
    were unreadable diffs told the operator to check their SSO session, so the
    two fixes that would have worked (exclude the task, raise the limit) went
    unmentioned, and the resume they were told to run re-attempted the same
    units in the same order and aborted in the same place.

    `is_auth_failure` and NOTHING ELSE decides which paragraph is printed. It
    is deliberately narrow -- 401/403 by status or by class name, never a match
    against the message text -- and text matching is the version of this that
    fails in both directions at once: a `ValueError` whose message happens to
    say "token" reads as a credential failure, and a real auth error phrased by
    a route that says nothing about auth reads as data. `MalformedVerdict` and
    `PayloadSecretsFound` are data-shaped by construction and land in the
    second paragraph, which is where they belong: neither is fixed by a mint.

    A MIXED run gets BOTH paragraphs rather than a majority verdict. Mixed is
    real -- a credential dying in the middle of a task whose diffs are also
    unreadable -- and the honest report is that both were seen, in the counts
    they were seen in.

    The list is capped at `_MAX_NAMED`, for that constant's reason and because
    the limit is now an operator flag: `--max-consecutive-errors 500` would
    otherwise print a 500-line warning above a 500-line error list saying the
    same thing twice. The counts in the paragraphs below are over the WHOLE
    run, not over what was listed, and every unit is in `errors` regardless.
    """
    shown = failure_run[:_MAX_NAMED]
    named = "\n".join(f"  - {label}" for label, _ in shown)
    if len(failure_run) > _MAX_NAMED:
        named += (
            f"\n  ... and {len(failure_run) - _MAX_NAMED} more, all of them "
            "in the errors below"
        )
    auth = [label for label, was_auth in failure_run if was_auth]
    data = [label for label, was_auth in failure_run if not was_auth]

    parts = [
        f"{len(failure_run)} consecutive unit failures -- aborting the batch "
        f"with units left unattempted. The units that failed, oldest first:\n"
        f"{named}"
    ]
    if auth:
        parts.append(
            f"{len(auth)} of them failed authentication (HTTP 401/403). One "
            "auth failure per call already buys a freshly minted credential "
            "and retries on it, so reaching this point means the mint did not "
            "help: the usual causes are an expired `aws sso` session, a "
            "revoked role, or an endpoint that has stopped accepting this "
            "principal at all. Nothing is lost -- judgments are append-only "
            "and the resume is keyed on units already bought, so resume with "
            "the same command once the credential works again."
        )
    if data:
        parts.append(
            f"{len(data)} of them did not fail authentication, so a fresh "
            "token would change nothing: these units fail deterministically "
            "and will fail again on resume, in the same order and in the same "
            "place, until something about the collection or the command "
            "changes. The errors below name each one. Judge past them by "
            "raising --max-consecutive-errors, or leave their task out of "
            "--only-task, which names the tasks to judge -- a task it does "
            "not name is one this pass never reaches."
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# the batch
# ---------------------------------------------------------------------------


def judge_event_log(event_log_root, tasks, *,
                    complete: CompleteFn | None = None,
                    judge_model_id: str = JUDGE_MODEL_ID_DEFAULT,
                    only_tasks: Iterable[str] | None = None,
                    sample_indices: Iterable[int] | None = None,
                    rubric: bool = True,
                    re_judge: bool = False,
                    max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS,
                    ) -> dict:
    """Judge every selected unit in one event log. Returns the batch.

    `complete` is the ONE seam that keeps this testable without a socket: one
    rendered prompt in, raw model text out. Its default is the real thing and
    it is LAZY -- see `lazy_live_completion`. There is no second seam for
    position, because there is no draw: every comparison is shown in both
    orders, `VOTE_POSITIONS` in that order, one vote each.

    There is no vote-count parameter for the same reason. Under forced
    positions exactly one count is legal, and a knob offering others would let
    a typo drop half of every comparison (or add a third vote whose position no
    protocol assigns) while the batch reported a clean pass.

    A UNIT is one rubric call, one pairwise vote, or one gate-decided pair. That
    is the granularity of the resume key, of the `try/except`, of the
    consecutive-failure breaker and of the exit contract, and keeping the four
    aligned is what makes "exit 0 means every selected unit produced its lines"
    a statement about something.

    `max_consecutive_errors` units failing in a row stops the walk early -- see
    `MAX_CONSECUTIVE_ERRORS`, which is its default, and `_BatchAborted`. The
    batch still returns: everything already judged is on disk and in the
    summary, and the abort is a warning beside the errors rather than an
    exception out of this function. The run it counts is a run of units
    ATTEMPTED here: a unit the resume skipped and a unit whose run yields no
    payload are neither, for the two reasons `_BatchAborted` gives. The
    CLI validates the limit at `>= 1`; a caller passing less gets a breaker
    that fires on the first error it sees, which is a hair trigger rather than
    an exception worth raising from here.

    `sorted()` at every level -- runs, cells, models, pairs. `list_runs` globs
    and glob order is nondeterministic; sorted is arbitrary but DETERMINISTIC,
    which is what a resumable batch needs, since a killed pass that resumes in a
    different order redoes different work.

    `input_payload_path` is stored RELATIVE to the judgments directory --
    `payloads/<judgment_id>.json.gz` -- and never as the absolute path
    `write_payload` returns. An absolute path records where this collection sat
    on the machine that judged it, so the first `mv`, copy or container mount
    at another root turns every line in the file into a verdict naming an input
    nobody can read, which is exactly the loss §4.3 logs payloads to prevent
    and it arrives silently. The old argument for storing the return value
    verbatim -- that the line names what was actually written -- is kept as a
    CHECK instead of an identity: `_stored_payload_path` compares the two on
    every line. Lines written before this change stay absolute and go on
    resolving, through `judge_schema.resolve_payload_path`.
    """
    event_log_root = Path(event_log_root)
    path = judgments_path(event_log_root)
    payloads = payloads_root(event_log_root)
    warnings: list[str] = []

    existing, malformed = load_judgments(path)
    if malformed and not re_judge:
        raise ResumeRefused(
            f"{malformed} malformed line(s) in {path}: a resume is keyed on "
            "which comparisons are already judged, and an unreadable judgment "
            "is not an absent one. Read the file, remove the damage, or pass "
            "--re-judge to judge everything again."
        )
    if malformed:
        warnings.append(
            f"{malformed} unreadable line(s) in {path} were skipped. "
            "--re-judge makes them harmless to the resume, which skips "
            "nothing, but the campaign audit below reads the same list -- so "
            "`audited` and `from_prior_invocations` are reported over an "
            "incomplete view of what has already been judged."
        )

    grade_file = grades_path(event_log_root)
    grades, grade_malformed = load_grades(grade_file)
    if grade_malformed:
        # No flag cures this one. An unreadable gate might BE the gate: the run
        # it belongs to reads as ungraded, so it silently leaves a pair -- and
        # if the damage sat on a `resolved is False` line, the pair the judge
        # would have recorded gate-decided becomes one it asks a model about.
        raise ResumeRefused(
            f"{grade_malformed} malformed line(s) in {grade_file}. The judge "
            "defers to the deterministic gate and cannot read it completely, "
            "so there is nothing to defer to. No flag cures this -- --re-judge "
            "governs the judgment file only. Repair or re-run scripts/grade.py."
        )

    gating = gating_view(grades)
    done = {_resume_key(judgment) for judgment in existing}
    by_task = {task.task_id: task for task in tasks}

    log = EventLog(event_log_root)
    stored = sorted(log.list_runs())
    errors: list[str] = []

    ungraded = [run_id for run_id in stored if run_id not in gating]
    if ungraded:
        warnings.append(
            f"{len(ungraded)} run(s) in the event log carry no grade line and "
            f"were not judged: {_named(ungraded)}. Rescuing an ungraded run is "
            "out of scope -- with no gate to defer to the judge has nothing to "
            "be blind about. Run scripts/grade.py first if these belong in the "
            "comparison."
        )
    orphans = sorted(set(gating) - set(stored))
    if orphans:
        warnings.append(
            f"{len(orphans)} grade line(s) name a run this event log does not "
            f"hold: {_named(orphans)}. The usual cause is a grade file copied "
            "from another collection; run ids are unique within a collection "
            "and not across one."
        )

    # Step 4: read the runs, and drop the excluded ones BEFORE grouping.
    records: dict[str, Any] = {}
    excluded: list[str] = []
    for run_id in stored:
        grade = gating.get(run_id)
        if grade is None:
            continue
        if grade.resolved is None:
            excluded.append(run_id)
            continue
        try:
            records[run_id] = log.read_run(run_id)
        except Exception as exc:  # noqa: BLE001 - one run, not the batch
            # Named by task and arm as well as by id. A run id is
            # `sha256(task|model|sample|attempt)[:16]`, so the id alone tells
            # an operator nothing they can act on -- this is the one path a
            # GENUINE read failure takes, and it was reporting the digest and
            # nothing else. `sample_index` is not on a `GradeRecord`, so this
            # cannot be a full `_unit_label`; task and model are what the
            # grade line has, and they close most of the gap.
            errors.append(
                f"run {run_id} (task={grade.task_id} {grade.model}) could not "
                f"be read from the event log: {type(exc).__name__}: {exc}"
            )
    if excluded:
        warnings.append(
            f"{len(excluded)} graded run(s) are excluded (resolved is null) "
            f"and were dropped before pairing: {_named(excluded)}. An excluded "
            "row is not an observation of the model, and ranking it ranks the "
            "infrastructure."
        )

    # Step 5: group into cells, keeping the max-attempt run per model.
    cells: dict[tuple[str, int], dict[str, Any]] = {}
    for run_id in sorted(records):
        record = records[run_id]
        cell = cells.setdefault((record.task_id, record.sample_index), {})
        prior = cell.get(record.model)
        # `run_id` breaks the tie, so two runs of one model at one attempt
        # number resolve the same way on every pass rather than on dict order.
        if prior is None or (record.attempt_number, record.run_id) > (
            prior.attempt_number, prior.run_id
        ):
            cell[record.model] = record

    wanted_tasks = None if only_tasks is None else set(only_tasks)
    wanted_samples = None if sample_indices is None else set(sample_indices)
    if wanted_tasks is not None:
        absent = sorted(wanted_tasks - {task_id for task_id, _ in cells})
        if absent:
            warnings.append(
                f"--only-task named {len(absent)} task(s) with no judgeable "
                f"run in this event log: {_named(absent)}. Not an error -- the "
                "exit contract is about units that were selected and exist -- "
                "but the usual cause is a pointer at the wrong collection, "
                "where every id is missing and the batch reports a clean zero."
            )

    complete = complete if complete is not None else lazy_live_completion(
        judge_model_id
    )

    judged: list[JudgeRecord] = []
    gate_decided: list[JudgeRecord] = []
    skipped: list[tuple] = []
    missing_tasks: set[str] = set()

    # Runs this pass could not turn into a submission, and why. Filled by
    # `_judgeable_inputs` the first time one is asked for, so a run that fails
    # to build is diagnosed once and then costs nothing for every further unit
    # it appears in.
    #
    # NOT "unreadable": these runs were read, and `records` holds them. What
    # they could not produce is a payload. The distinction is the whole content
    # of the two error phrasings -- a run that failed `EventLog.read_run` and a
    # run whose record has no `final_diff` are different faults with different
    # fixes, and one phrase over both is a claim that is false for one of them.
    unjudgeable: dict[str, str] = {}

    # The breaker's state: the units attempted-and-failed since the last
    # success, each beside what `is_auth_failure` said about the exception that
    # failed it. A LIST rather than a counter, because the abort message is
    # built from exactly this run (`_abort_message`) -- a counter beside a
    # separate record of the failures is two structures answering "how long is
    # the run", and the day they disagree the message describes a run other
    # than the one that fired.
    #
    # Three closures rather than inline bookkeeping, so the per-unit handlers
    # cannot each grow their own version of "does this one count" -- which is
    # exactly where a gate-decided pair would quietly become a success.
    failure_run: list[tuple[str, bool]] = []

    def _unit_succeeded() -> None:
        """A unit produced its line, so whatever was failing is not systemic."""
        failure_run.clear()

    def _unit_failed(label: str, exc: BaseException) -> None:
        """Record one unit's error, and abort the batch on a run of them.

        See `MAX_CONSECUTIVE_ERRORS`. The EXCEPTION is taken rather than a
        finished message, because the abort needs two things from it -- the
        text for the error line and `is_auth_failure`'s answer for the
        diagnosis -- and a caller that formatted the text itself would be the
        one place the classification could be skipped.
        """
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        failure_run.append((label, is_auth_failure(exc)))
        if len(failure_run) >= max_consecutive_errors:
            raise _BatchAborted(_abort_message(failure_run))

    def _unit_not_judged(label: str, run_ids: Sequence[str]) -> None:
        """One line for a unit whose run yielded no payload. NOT a breaker
        event.

        The unit never reached the judge, so it is no evidence about the judge.
        Counting it is what deadlocked a collection with nothing else wrong:
        one unjudgeable run takes seven units in a four-arm cell (its rubric
        call, and both votes of each of its three pairs), and on the RESUME
        those seven are the only ones attempted -- every judgeable unit is
        already bought and skipped -- so the fifth of them aborts the pass,
        under a message blaming a credential that was working the whole time.
        Measured: the first pass judged 9 units and the next three resumes each
        judged 0 and aborted in the same place.

        "YIELDED NO JUDGEABLE PAYLOAD", never "could not be read". The run WAS
        read -- it is in `records`, and its record is what the parenthesised
        cause was raised over. "Could not be read from the event log" is the
        genuine read failure's phrasing, verbatim, and reusing it here would
        put two different faults under one sentence: an operator grepping that
        phrase would collect both and act on the wrong one, and the leading
        clause would contradict the `ValueError` printed beside it.
        """
        why = "; ".join(
            f"run {run_id} yielded no judgeable payload "
            f"({unjudgeable[run_id]})"
            for run_id in run_ids if run_id in unjudgeable
        )
        errors.append(f"{label}: not judged: {why}")

    def _judgeable_inputs(cache: dict[str, PayloadInputs], run_id: str,
                          grade: GradeRecord, task) -> PayloadInputs | None:
        """This run's payload inputs, or `None` if it has none to give.

        THE PRE-FILTER, and it is one function because the failure is a
        property of the RUN rather than of the unit: `payload_inputs_from`
        raises on a record with no `artifacts.final_diff`, and it raises for
        every unit that record appears in -- a rubric call and both votes of
        every pair the arm is in. Discovered here, before the unit's `try`,
        that is one diagnosis and one line per affected unit; discovered inside
        it, it was a run of identical failures the breaker could not tell from
        a dead credential.

        The early return is the working half of that: the SECOND unit to ask
        about a run already known bad gets `None` without re-raising, so the
        seven units of a four-arm cell cost one diagnosis rather than seven.

        `records[run_id]` is expected to be present -- cells are grouped out of
        `records`, so a run that failed to READ never reaches a unit and its
        one error line is already recorded above, under its own phrasing. A
        `KeyError` from it would land in the same place with the same shape
        rather than taking the walk down; that arm is unreachable today and is
        kept as a structural guard.
        """
        if run_id in unjudgeable:
            return None
        try:
            return _inputs_for(cache, records[run_id], grade, task)
        except Exception as exc:  # noqa: BLE001 - one run, not the batch
            unjudgeable[run_id] = f"{type(exc).__name__}: {exc}"
            return None

    try:
        for task_id, sample_index in sorted(cells):
            if wanted_tasks is not None and task_id not in wanted_tasks:
                continue
            if (wanted_samples is not None
                    and sample_index not in wanted_samples):
                continue
            task = by_task.get(task_id)
            if task is None:
                missing_tasks.add(task_id)
                continue

            cell = cells[(task_id, sample_index)]
            # Per CELL, and shared across the rubric call and both votes of
            # every pair the cell produces -- `similarity_context` walks two
            # diffs and the payload builders take `PayloadInputs` by value, so
            # rebuilding per unit would pay for the same walk four times over.
            inputs: dict[str, PayloadInputs] = {}

            if rubric:
                for model in sorted(cell):
                    record = cell[model]
                    grade = gating[record.run_id]
                    # Selection-time gate. The assert before the append is
                    # the one that is load-bearing; this is what keeps the
                    # batch from paying for a call it must then refuse to
                    # record.
                    if grade.resolved is not True:
                        continue
                    key = _rubric_key(record.run_id, judge_model_id)
                    if not re_judge and key in done:
                        skipped.append(key)
                        continue
                    label = _unit_label(
                        "rubric", task_id, sample_index,
                        (record.model,), (record.run_id,),
                    )
                    unit_inputs = _judgeable_inputs(
                        inputs, record.run_id, grade, task
                    )
                    if unit_inputs is None:
                        _unit_not_judged(label, (record.run_id,))
                        continue
                    try:
                        judged.append(_rubric_line(
                            record, grade, task, unit_inputs,
                            payloads, path, complete, judge_model_id,
                        ))
                        _unit_succeeded()
                    except Exception as exc:  # noqa: BLE001 - one unit
                        _unit_failed(label, exc)

            for model_x, model_y in itertools.combinations(sorted(cell), 2):
                # Canonical a/b is the two run ids sorted, NOT the two
                # models: the identity has to be stable under a model rename,
                # and `(x, y)` and `(y, x)` must be one comparison or the
                # position-swap probe can no longer find its own pairs.
                run_id_a, run_id_b = sorted(
                    (cell[model_x].run_id, cell[model_y].run_id)
                )
                grade_a, grade_b = gating[run_id_a], gating[run_id_b]
                passed_a = grade_a.resolved is True
                passed_b = grade_b.resolved is True
                # Named off the RUN ids, not off `(model_x, model_y)`: a/b is
                # the two run ids sorted, so the model order can be the other
                # one, and a label naming each arm as the other is worse than
                # no label at all.
                arms = (records[run_id_a].model, records[run_id_b].model)

                if not passed_a and not passed_b:
                    # Nothing. Neither side is a submission worth ranking,
                    # and a "tie" here would be a verdict about two failures.
                    continue

                if passed_a != passed_b:
                    key = _pairwise_key(task_id, sample_index, run_id_a,
                                        run_id_b, None, judge_model_id)
                    if not re_judge and key in done:
                        skipped.append(key)
                        continue
                    label = _unit_label(
                        "gate-decided", task_id, sample_index, arms,
                        (run_id_a, run_id_b),
                    )
                    try:
                        gate_decided.append(_gate_decided_line(
                            task, sample_index, run_id_a, run_id_b,
                            grade_a, grade_b, path, judge_model_id,
                        ))
                    except Exception as exc:  # noqa: BLE001 - one unit
                        # Straight into `errors`, deliberately: a gate-decided
                        # pair makes no call, so it is neither evidence that
                        # the credential is dead nor evidence that it
                        # recovered. See `_BatchAborted`.
                        errors.append(
                            f"{label}: {type(exc).__name__}: {exc}"
                        )
                    continue

                # One vote per forced position, and `enumerate` is what ties
                # `vote_index` to `VOTE_POSITIONS` -- 0 is `a_first`, 1 is
                # `b_first`. Adjacent, so a batch killed mid-comparison leaves
                # the missing position as the very next unit a resume buys.
                for vote_index, position in enumerate(VOTE_POSITIONS):
                    key = _pairwise_key(task_id, sample_index, run_id_a,
                                        run_id_b, vote_index, judge_model_id)
                    if not re_judge and key in done:
                        skipped.append(key)
                        continue
                    # The POSITION rides along with the index, as the string
                    # this replaced carried it. It is derivable -- `enumerate`
                    # over `VOTE_POSITIONS` ties the two -- but an error line
                    # is read by somebody who does not have that mapping in
                    # front of them, and `a_first`/`b_first` is what the stored
                    # `position_assignment` calls the same thing.
                    label = _unit_label(
                        f"vote {vote_index} ({position})", task_id,
                        sample_index, arms, (run_id_a, run_id_b),
                    )
                    # Bound before the call rather than inline: two positional
                    # `PayloadInputs` four lines apart is where an a/b
                    # transposition hides, and a transposed pair produces a
                    # complete, confident, inverted verdict. Both are bound
                    # BEFORE the `try` as well, so a run that cannot produce
                    # inputs is pre-filtered rather than counted -- see
                    # `_judgeable_inputs`.
                    inputs_a = _judgeable_inputs(
                        inputs, run_id_a, grade_a, task
                    )
                    inputs_b = _judgeable_inputs(
                        inputs, run_id_b, grade_b, task
                    )
                    if inputs_a is None or inputs_b is None:
                        _unit_not_judged(label, (run_id_a, run_id_b))
                        continue
                    try:
                        judged.append(_vote_line(
                            task, sample_index, run_id_a, run_id_b,
                            inputs_a, inputs_b,
                            vote_index, position, grade_a, grade_b,
                            payloads, path, complete, judge_model_id,
                        ))
                        _unit_succeeded()
                    except Exception as exc:  # noqa: BLE001 - one unit
                        _unit_failed(label, exc)

    except _BatchAborted as exc:
        # The abort is a WARNING and not an error, because it is not a unit:
        # the units that failed are already in `errors`, and the exit code
        # follows them. The batch still returns its partial reading rather
        # than a traceback -- everything judged before the run of failures is
        # on disk and belongs in the summary.
        warnings.append(str(exc))

    if missing_tasks:
        warnings.append(
            f"{len(missing_tasks)} task(s) with graded runs are absent from "
            f"the loaded task set and were not judged: "
            f"{_named(sorted(missing_tasks))}. The payload is anchored on the "
            "manifest's prompt and solution diff, so there is nothing to judge "
            "against."
        )

    # THE AUDIT IS OVER THE CAMPAIGN, NOT OVER THIS INVOCATION, on `grade.py`'s
    # precedent. A judging pass is thousands of paid calls and is killed by an
    # operator or a rate limit far more often than it finishes, so the last
    # invocation routinely writes three lines -- and a census computed from
    # those three would describe a campaign of three.
    fresh = judged + gate_decided
    audited = _last_line_per_judgment(existing + fresh)
    fresh_keys = {_resume_key(judgment) for judgment in fresh}

    return {
        "judged": judged,
        "gate_decided": gate_decided,
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        # A printout, not a stored score. Nothing here is written anywhere.
        "summary": summarize(audited, _model_of(log, records, audited)),
        "audited": len(audited),
        "from_prior_invocations": sum(
            1 for judgment in audited
            if _resume_key(judgment) not in fresh_keys
        ),
    }


def _model_of(log: EventLog, records: dict[str, Any],
              audited: list[JudgeRecord]) -> dict[str, str]:
    """`run_id -> model`, for every run the audited judgments name.

    Mostly free: `records` already holds every run this pass judged. The gap it
    fills is the one the campaign rule opens -- a judgment from an EARLIER
    invocation can name a run this pass never read, because a later re-grade
    excluded it (`resolved is None` drops the run before it is read), because
    `--only-task` narrowed the selection, or because the run was already judged
    and skipped. That comparison is still in the file and still belongs in the
    matrix, so the name is recovered from the event log rather than from this
    pass's records.

    A run the log no longer holds stays unnamed rather than raising. Missing is
    a real state -- a judgment file copied beside another collection -- and
    `summarize` counts what it could not name instead of ranking a `None`.
    """
    model_of = {run_id: record.model for run_id, record in records.items()}
    named: set[str] = set()
    for judgment in audited:
        named.update(
            run_id
            for run_id in (judgment.run_id, judgment.run_id_a,
                           judgment.run_id_b)
            if run_id
        )
    for run_id in sorted(named - set(model_of)):
        try:
            model_of[run_id] = log.read_run(run_id).model
        except Exception:  # noqa: BLE001 - an unnameable arm, not a unit
            continue
    return model_of


def _inputs_for(cache: dict[str, PayloadInputs], record, grade: GradeRecord,
                task) -> PayloadInputs:
    """The whitelist chokepoint's output for one run, built at most once.

    Called through `_judgeable_inputs` and never directly, because
    `payload_inputs_from` raises `ValueError` on a run with no
    `artifacts.final_diff` and that failure belongs to the RUN rather than to
    any one unit. Eagerly building these for a whole cell would take the batch
    down over one record; building them inside each unit's `try` charged the
    same record to the consecutive-failure breaker once per unit it appeared
    in. The wrapper is the third option: caught once, per run, before the
    `try`.

    Still LAZY, and that is what the wrapper preserves. A resume whose cell is
    already judged skips every unit in it, and a cell walked eagerly would pay
    for `similarity_context` over every arm to build inputs nothing asks for.
    """
    if record.run_id not in cache:
        cache[record.run_id] = payload_inputs_from(record, grade, task)
    return cache[record.run_id]


def _named(values: list[str]) -> str:
    if len(values) <= _MAX_NAMED:
        return ", ".join(values)
    rest = len(values) - _MAX_NAMED
    return ", ".join(values[:_MAX_NAMED]) + f", and {rest} more"


def _judge_generation(judgment: JudgeRecord) -> tuple:
    """WHICH ORACLE said it: judge model, prompt version, rubric version.

    ONE derivation, read by `_comparison_key`, by the win-rate matrix, by the
    Elo tables and by `_rubric_profile`. Two copies of "which oracle is this"
    drift silently and in the worst direction: the comparison buckets stay
    apart while the blocks they are reported in pool, so the file is right and
    the printout -- the only place a reader meets these numbers -- is wrong.

    A verdict is reproducible against the model that gave it, the prompt text
    it saw and the rubric it was scored under. `_resume_key` treats a change to
    any of the three as new work rather than a resume, which is what puts two
    generations in one append-only file in the first place; everything that
    aggregates that file has to treat them as two readings of one collection,
    never as more comparisons of it.
    """
    return (
        judgment.judge_model_id,
        judgment.judge_prompt_version,
        judgment.rubric_version,
    )


#: How many trailing elements of a `_comparison_key` name the generation.
#: `_comparison_key` APPENDS `_judge_generation`'s tuple and the matrix walk
#: slices it back off, so one constant holds both halves of that layout
#: together -- a hand-counted `[-3:]` beside a four-field generation would
#: partition on a slice of one, which is a pooling nothing would report.
_GENERATION_FIELDS = 3


def _generation_of(key: tuple) -> tuple:
    """The generation back out of a `_comparison_key` -- its last elements.

    The inverse of the append in `_comparison_key`, and pinned as such by
    `test_the_comparison_key_carries_the_generation_the_matrix_partitions_on`.
    """
    return key[-_GENERATION_FIELDS:]


def _comparison_key(judgment: JudgeRecord) -> tuple:
    """One COMPARISON: this pair, at this sample, under one judge generation.

    `vote_index` is deliberately absent, which is the whole point of having a
    second key at all. It is what makes the votes for a pair -- both forced
    positions on a v2 line, all three on a v1 one, and any gate-decided line
    for the same pair -- land in one bucket, and a bucket is where "if votes
    exist, they win" can be applied. `_resume_key` keeps `vote_index` because
    it answers a different question: which unit of WORK is already bought.

    The three version fields stay, for `_resume_key`'s reason. A re-judge under
    a new prompt is a new generation of verdict, and pooling two generations
    into one comparison would average a disagreement the file exists to keep.
    They go LAST and as one appended tuple, because the matrix walk slices them
    back off with `_generation_of` to partition its blocks.
    """
    return (
        judgment.task_id,
        judgment.sample_index,
        judgment.run_id_a,
        judgment.run_id_b,
    ) + _judge_generation(judgment)


def _deterministic(keys) -> list:
    """`sorted`, over keys that may hold a `None` beside an `int`.

    A hand-edited or foreign-schema line can carry `sample_index=None`, and
    `sorted` on a tuple mixing `None` with `int` raises `TypeError` -- which
    would take the whole summary down at the end of a batch that already paid
    for thousands of calls. `repr` is a total order over anything, arbitrary but
    stable, and stable is the only property the walk needs.
    """
    return sorted(keys, key=repr)


def elo_from_outcomes(
    outcomes: list[tuple[str, str, float]],
) -> dict[str, float]:
    """Ratings from `(model_a, model_b, score_a)` triples. Bradley-Terry MLE.

    DESCRIPTIVE. The table follows the win rates, it does not adjudicate them:
    it is a reading of the same comparisons in a scale that composes across
    pairs, and the matrix above it is the primary report. §D7.

    A MAXIMUM-LIKELIHOOD FIT, not a sequential rating walk, and that is the
    whole point of this function's shape. The previous implementation was Elo
    at K=32 applied one comparison at a time over `sorted(outcomes)` -- and
    sorting by model name is what made the ranking a function of the NAMES.
    Sorted triples arrive as one contiguous block per pair, losses before wins
    inside each block, so every arm's final rating was mostly whatever the last
    few dozen comparisons in its last block handed it. Measured on a four-arm
    ladder at 150 comparisons per pair, names running against strength: 40 of
    40 seeds printed a wrong ranking, and the fixture in the tests prints the
    order exactly BACKWARDS -- the weakest arm on top by 740 points. Renaming
    the arms, which says nothing about any of them, moved ratings by over 200
    points. Bradley-Terry has no walk to be ordered: the likelihood is a
    function of the win COUNTS, so a shuffle, a relabelling and a second pass
    over one collection all give one table.

    ONE VIRTUAL TIE per unordered pair that has at least one real comparison
    (§D8). Without it the MLE for an arm that never lost is +infinity -- the
    iteration either burns its whole cap chasing it or prints an `inf` that
    formats as a rating -- and a winless arm is -infinity by the same argument.
    Half a point each way per played pair is the lightest prior that bounds
    both, and it is deliberately weak: at this bakeoff's size (~120-180
    comparisons per pair) it moves arms within ~50 rating points of each other
    by under a point, and about 1.5 points at a 100-point spread.

    It can, however, REORDER, and the reason is worth stating exactly because
    the obvious intuition -- that a pull toward the anchor compresses the table
    without crossing anything -- is wrong. The prior is one tie per PAIR, not
    per arm, so the shrinkage is UNEVEN across arms: an arm whose comparisons
    sit in sparse pairs is pulled harder than one whose comparisons sit in
    dense pairs, and uneven shrinkage can cross two arms rather than merely
    compress them. Equal per-pair counts are not a property this function may
    assume -- a resumed batch, an arm added partway through a collection, and
    dropped comparisons all skew them.

    Two independent sweeps against the unregularised fit agree on the shape: at
    comparable per-pair counts, no reorder in thousands of trials; at a large
    per-pair imbalance (~18x), single-digit-per-thousand flips. The exact rate
    depends on the comparison schedule and the true spread, so it is not a
    constant worth quoting. What held across both sweeps is the bound that
    matters: every flip observed was between arms the unregularised fit
    separates by a fraction of a point (~0.2), which is far inside the
    resolution the kappa caveat above already says these numbers do not have.

    Ties are half a win each way, matching the matrix's `win_rate_x`. If the
    two scored a draw differently they would rank differently off one
    collection and the reader would have to guess which order the comparisons
    actually support.

    DISCONNECTED COMPARISON GRAPHS are the one case this does not really
    answer. If no chain of comparisons links two groups of arms, their relative
    strength is not in the data, and the fit will report some number anyway --
    the normalisation and the anchor are what relate them, not evidence. The
    driver pairs round-robin within a task, so a real collection is connected;
    a hand-assembled outcome list is the way to get here, and the ratings
    across such a split may only be read within each group.

    `math.fsum`, not `sum`, for every aggregate. Relabelling changes the order
    an arm's opponents are visited in, and float addition is not associative,
    so plain `sum` leaves the rename invariance resting on the last bits
    happening to agree. They do agree on today's fixtures -- swapping `fsum`
    for `sum` keeps the suite green -- which is exactly why this is written
    down: the tests would not catch its loss, and the next fixture, with more
    arms or a wider spread, is where a bit-exact assertion would start flaking
    for a reason nobody could find. Exactly-rounded summation makes the
    invariance structural instead of lucky.

    Refuses a score outside `{0.0, 0.5, 1.0}` BEFORE any arithmetic. Those are
    the only three results a comparison has, and a vote COUNT arriving here in
    place of a result would inflate every rating by an amount no reader could
    reconstruct from the printout. Validated in a first pass so the message
    names the offending pair rather than surfacing mid-fit as an arithmetic
    error naming nothing.
    """
    for outcome in outcomes:
        model_a, model_b, score_a = outcome
        if score_a not in (0.0, 0.5, 1.0):
            raise ValueError(
                f"elo_from_outcomes takes a score in {{0.0, 0.5, 1.0}} -- a "
                f"win, a tie or a loss for the first named model -- but got "
                f"{score_a!r} for {model_a} vs {model_b}"
            )

    # Points per arm and comparisons per unordered pair -- the only two things
    # the likelihood reads. `wins` accumulates 0.0/0.5/1.0, all exact in binary
    # and exactly summable at these magnitudes, so a reordered stream builds an
    # identical tally rather than a nearly identical one.
    wins: dict[str, float] = {}
    played: dict[tuple[str, str], float] = {}
    for model_a, model_b, score_a in outcomes:
        if model_a == model_b:
            # One model on both sides is not a comparison, and it has no place
            # in a denominator that sums over OPPONENTS. `summarize` already
            # drops these into `dropped["same_model_comparison"]`; this only
            # guards a direct caller, which is why it is silent here.
            continue
        wins[model_a] = wins.get(model_a, 0.0) + score_a
        wins[model_b] = wins.get(model_b, 0.0) + (1.0 - score_a)
        pair = (model_a, model_b) if model_a < model_b else (model_b, model_a)
        played[pair] = played.get(pair, 0.0) + 1.0

    for model_x, model_y in played:
        wins[model_x] += 0.5
        wins[model_y] += 0.5
        played[(model_x, model_y)] += 1.0

    models = sorted(wins)
    if not models:
        # Nothing comparable in the collection is not a dead heat. Every arm at
        # the anchor would be a printed tie no comparison supports.
        return {}

    opponents: dict[str, list[tuple[str, float]]] = {
        model: [] for model in models
    }
    for (model_x, model_y), games in played.items():
        opponents[model_x].append((model_y, games))
        opponents[model_y].append((model_x, games))

    # MM/Zermelo. Every arm's update reads the PREVIOUS iterate for both itself
    # and its opponents (Jacobi, not in-place): an in-place sweep converges to
    # the same fit but takes a path through the name order to get there, which
    # is the class of bug this function was rewritten to remove.
    strength = {model: 1.0 for model in models}
    for _ in range(_BT_MAX_ITERATIONS):
        updated = {
            model: wins[model] / math.fsum(
                games / (strength[model] + strength[other])
                for other, games in opponents[model]
            )
            for model in models
        }
        # Renormalise to geometric mean 1 each iteration. The likelihood is
        # invariant under a common scale factor, so without this the iterates
        # drift and the convergence test measures the drift instead of the fit.
        scale = math.exp(
            math.fsum(math.log(value) for value in updated.values())
            / len(models)
        )
        updated = {model: value / scale for model, value in updated.items()}
        delta = max(
            abs(updated[model] - strength[model]) for model in models
        )
        strength = updated
        if delta < _BT_TOLERANCE:
            break

    ratings = {
        model: ELO_ANCHOR + ELO_SCALE * math.log10(strength[model])
        for model in models
    }
    # The geometric-mean normalisation already puts the mean at the anchor in
    # exact arithmetic; this removes the float drift, so the printed table sums
    # to what the header claims rather than to a tenth of a point off it.
    drift = math.fsum(ratings.values()) / len(models) - ELO_ANCHOR
    return dict(
        sorted((model, rating - drift) for model, rating in ratings.items())
    )


def summarize(judgments: list[JudgeRecord], model_of: dict[str, str]) -> dict:
    """Win rates, a rubric profile and an Elo table over the judgment file.

    A PRINTOUT, NOT A STORED SCORE, on `grade.py summarize`'s precedent -- and
    it matters more here. A stored Elo is a published number, and §4.3 forbids
    publishing one without the κ that was in force at judging time. Nothing this
    returns is written anywhere; `print_summary` says it out loud, under the
    caveat, and it is gone.

    `model_of` maps `run_id -> model`, out of the event log. The SUMMARY may
    name arms -- that is what makes a win-rate matrix readable. The PAYLOAD
    never does, which is the whitelist chokepoint's business, not this
    function's.

    Three aggregation rules, each placed against a specific way an append-only
    file produces a confident, wrong number:

    1. **Dedupe to the last line per resume identity, before anything else.** A
       re-judge appends rather than replaces, so one vote can hold two lines
       under one identity. Aggregating both counts that vote twice, in whichever
       direction the earlier generation happened to point.
    2. **Within one comparison, vote lines supersede a gate-decided line.** A
       re-grade that flips the losing side turns a gate-decided pair into a
       judgeable one, and the old gate-decided line stays on disk beside the
       votes that came later. Counting both enters one comparison twice,
       once for each side. The votes win -- they are the later, richer verdict
       -- and the disagreement is TALLIED in `superseded_gate_decided` rather
       than quietly resolved: the file is append-only, so the disagreement is
       the finding.
    3. **`gate_decided` never enters the vote verdict distribution.** It is the
       one verdict no model produced. Inside the distribution it is a fourth
       thing the judge said, and every rate off that denominator is wrong by
       however many pairs the ladder settled. It gets its own count in `lines`,
       and its own column in each matrix row.
    4. **Two judge generations are never pooled.** `comparisons` and `elo` are
       keyed on `_judge_generation` first and the model pair second, so
       `--judge-model openai.gpt-5.6-luna` over an already-judged collection
       produces a SECOND matrix block and a SECOND Elo table rather than twice
       as many comparisons in the first. Pooled, every pair enters the primary
       report twice, the comparison count doubles, and each rate is the mean of
       two oracles nobody asked for a joint opinion -- silently, and with the
       right shape. This is `_comparison_key`'s partition and
       `_rubric_profile`'s, carried into the report they feed.

    A gate-decided comparison IS a win for `gate_decided_by`'s side in the win
    rates and in Elo -- the objective result settled it, which is a result about
    the arm. Rule 3 is about the vote census, which is a different question:
    what did the judge say when asked.

    `dropped` counts what could not be aggregated, per reason, rather than
    letting any of it disappear: a line whose arm cannot be named, a comparison
    with one model on both sides, a verdict outside the vocabulary `majority`
    accepts, a `kind` this reader does not know. Every one of them shrinks a
    denominator, and a denominator that moves invisibly is the failure this
    whole file is arranged against.
    """
    deduped = _last_line_per_judgment(judgments)

    dropped = {
        "unreadable_kind": 0,
        "unreadable_verdict": 0,
        "unreadable_rubric": 0,
        "unknown_model_rubric": 0,
        "unknown_model_comparison": 0,
        "same_model_comparison": 0,
    }

    rubric_lines: list[JudgeRecord] = []
    votes_by: dict[tuple, list[tuple[int, str]]] = {}
    gate_by: dict[tuple, str] = {}
    vote_verdicts: Counter = Counter()

    for judgment in deduped:
        if judgment.kind == "rubric":
            rubric_lines.append(judgment)
            continue
        if judgment.kind != "pairwise":
            dropped["unreadable_kind"] += 1
            continue

        key = _comparison_key(judgment)
        if judgment.verdict == "gate_decided":
            if judgment.gate_decided_by in ("a", "b"):
                gate_by[key] = judgment.gate_decided_by
            else:
                # A gate-decided line naming no winner settles nothing.
                dropped["unreadable_verdict"] += 1
            continue
        if judgment.verdict not in _VOTE_VERDICTS:
            dropped["unreadable_verdict"] += 1
            continue

        vote_verdicts[judgment.verdict] += 1
        # `-1` for a vote line carrying no index -- only a hand-edited line can.
        # It sorts first and is stable, which is all the ordering has to be.
        votes_by.setdefault(key, []).append(
            (judgment.vote_index if judgment.vote_index is not None else -1,
             judgment.verdict)
        )

    superseded = 0
    # Rule 4: keyed on the generation FIRST. Both walks below stay inside one
    # oracle's verdicts, so neither a comparison count nor a rating fit can
    # cross from one generation into another's numbers.
    outcomes: dict[tuple, list[tuple[str, str, float]]] = {}
    pairs: dict[tuple, dict[tuple[str, str], dict[str, int]]] = {}

    for key in _deterministic(set(votes_by) | set(gate_by)):
        vote_lines = votes_by.get(key)
        gate = gate_by.get(key)
        if vote_lines:
            if gate is not None:
                superseded += 1
            # `majority` cannot see an empty sequence here: the bucket exists
            # only because a vote line landed in it.
            winner = majority([verdict for _, verdict in sorted(vote_lines)])
            settled = "voted"
        else:
            winner = gate
            settled = "gate_decided"

        run_id_a, run_id_b = key[2], key[3]
        model_a = model_of.get(run_id_a)
        model_b = model_of.get(run_id_b)
        if model_a is None or model_b is None:
            dropped["unknown_model_comparison"] += 1
            continue
        if model_a == model_b:
            # One model on both sides is not a comparison. A cell keys on model
            # so the driver cannot produce it; a cross-collection line can.
            dropped["same_model_comparison"] += 1
            continue

        generation = _generation_of(key)
        score_a = {"a": 1.0, "b": 0.0}.get(winner, 0.5)
        outcomes.setdefault(generation, []).append(
            (model_a, model_b, score_a)
        )

        # The matrix is keyed on the two model names SORTED, so `(x, y)` and
        # `(y, x)` are one row -- the same reason `run_id_a`/`run_id_b` are
        # canonical on the record. Inside ONE generation's block: see rule 4.
        model_x, model_y = sorted((model_a, model_b))
        score_x = score_a if model_x == model_a else 1.0 - score_a
        row = pairs.setdefault(generation, {}).setdefault(
            (model_x, model_y),
            {"voted": 0, "gate_decided": 0, "wins_x": 0, "wins_y": 0,
             "ties": 0},
        )
        row[settled] += 1
        if score_x == 1.0:
            row["wins_x"] += 1
        elif score_x == 0.0:
            row["wins_y"] += 1
        else:
            row["ties"] += 1

    comparisons: dict[tuple, dict[tuple[str, str], dict]] = {}
    for generation in sorted(pairs):
        block: dict[tuple[str, str], dict] = {}
        for pair in sorted(pairs[generation]):
            row = pairs[generation][pair]
            total = row["voted"] + row["gate_decided"]
            block[pair] = {
                **row,
                "comparisons": total,
                # A tie is half a point to each, exactly as it is to Elo. The
                # two have to agree or the table and the matrix rank
                # differently.
                "win_rate_x": (row["wins_x"] + 0.5 * row["ties"]) / total,
                "win_rate_y": (row["wins_y"] + 0.5 * row["ties"]) / total,
            }
        comparisons[generation] = block

    return {
        "lines": {
            "rubric": len(rubric_lines),
            "pairwise_votes": sum(vote_verdicts.values()),
            "gate_decided": len(gate_by),
        },
        "vote_verdicts": dict(sorted(vote_verdicts.items())),
        "comparisons": comparisons,
        "rubric_profile": _rubric_profile(rubric_lines, model_of, dropped),
        # One table per generation, for rule 4's reason. Pooling two oracles
        # fits one set of strengths to two different opinions about the same
        # pair, printing a consensus neither of them gave.
        "elo": {
            generation: elo_from_outcomes(rows)
            for generation, rows in sorted(outcomes.items())
        },
        "superseded_gate_decided": superseded,
        "dropped": dropped,
    }


def _last_line_per_judgment(judgments: list[JudgeRecord]) -> list[JudgeRecord]:
    """One record per resume identity -- the LAST, in file order.

    Rule 1 of `summarize`, and its own function because `judge_event_log` needs
    the same collapse to count `audited`. Idempotent, so the driver handing over
    an already-deduped list costs nothing and a direct caller handing over a raw
    file is still correct.
    """
    last: dict[tuple, JudgeRecord] = {}
    for judgment in judgments:
        last[_resume_key(judgment)] = judgment
    return list(last.values())


def _rubric_profile(rubric_lines: list[JudgeRecord],
                    model_of: dict[str, str], dropped: dict) -> dict:
    """Per model AND per judge generation: the mean of each dimension, and the
    rate of each flag. Keyed
    `(model, judge_model_id, judge_prompt_version, rubric_version)`.

    A PROFILE, never a score. Summing five dimensions into one number is the
    absolute 1-10 rating §4.2.3 rules out, wearing a rubric's clothes -- it is
    the open-ended question whose κ ≈ 0.32 motivated reference-anchoring in the
    first place. So there is no total, no aggregate, and no rank here, and none
    of it is joined to `resolved`: a `JudgeRecord` deliberately carries no
    `resolved` to join to, and a rubric mean sitting in a resolve rate is
    exactly the number §4.2.3 forbids.

    Per model rather than pooled. Two arms averaged into one row says nothing
    about either, which is the one thing a diagnostic output must not do.

    THE THREE VERSION FIELDS ARE IN THE KEY, for `_resume_key`'s reason and
    `_comparison_key`'s. `_resume_key` keys a rubric line on
    `("rubric", run_id, judge_model_id, judge_prompt_version, rubric_version)`,
    so one run re-judged under a bumped rubric survives the dedupe as TWO lines
    -- that is the point of the key. Grouping on the model alone would then
    average two rubrics into one row under a `runs` count that is really a line
    count: bump `RUBRIC_VERSION`, re-judge one arm, and every dimension mean for
    that arm silently blends two scales beside a doubled denominator. Two
    generations disagreeing about one run is the finding an append-only file
    exists to keep, so they get two blocks and the reader compares them.

    The dimension and flag NAMES come from the records rather than from
    `RUBRIC_DIMENSIONS`, and are sorted. A judgment written under a later
    `rubric_version` can carry a sixth dimension, both generations coexist in
    an append-only file, and iterating the constant would silently drop the one
    the newer rubric added -- the schema reader's `_build` reasoning, one layer
    up.
    """
    by_key: dict[tuple, list[JudgeRecord]] = {}
    for judgment in rubric_lines:
        if not _readable_rubric(judgment):
            dropped["unreadable_rubric"] += 1
            continue
        model = model_of.get(judgment.run_id)
        if model is None:
            dropped["unknown_model_rubric"] += 1
            continue
        by_key.setdefault(
            (model,) + _judge_generation(judgment), []
        ).append(judgment)

    profile: dict[tuple, dict] = {}
    for key in sorted(by_key):
        rows = by_key[key]
        profile[key] = {
            "runs": len(rows),
            "dimensions": _means(rows, "dimension_scores"),
            "flags": _means(rows, "flags"),
        }
    return profile


def _readable_rubric(judgment: JudgeRecord) -> bool:
    """Whether a rubric line can be averaged at all.

    Checked BEFORE the line reaches `_means`, because `sum(values) / len(values)`
    raises `TypeError` on a hand-edited `dimension_scores` holding a string or a
    `None` -- and `summarize` runs inside `judge_event_log`'s return, so that
    exception would destroy the result of a batch that already paid for
    thousands of calls. Precisely the failure `dropped` exists to prevent, and a
    damaged rubric line gets the same treatment a pairwise line with an
    unreadable verdict gets: counted, named, never raised.

    A line missing either half, or carrying an EMPTY half, is unreadable rather
    than "readable but contributing nothing". Counting it in `runs` while it
    moves no mean is a denominator that grows with no number under it moving --
    the same invisible-denominator failure the per-dimension counts in `_means`
    are placed against. `kind == "rubric"` asserts both halves are populated, so
    a line missing one is damaged, not partial.

    Dimensions must be `int` and NOT `bool`; flags must be `bool`. `bool` is a
    subclass of `int`, so without the second half of that test a `True` in a
    dimension slot would average in as a 1, and a `2` in a flag slot would
    report as a 200% flag rate.
    """
    scores, flags = judgment.dimension_scores, judgment.flags
    if not isinstance(scores, dict) or not scores:
        return False
    if not isinstance(flags, dict) or not flags:
        return False
    if any(not isinstance(value, int) or isinstance(value, bool)
           for value in scores.values()):
        return False
    return all(isinstance(value, bool) for value in flags.values())


def _means(rows: list[JudgeRecord], attribute: str) -> dict[str, dict]:
    """Per key: `{"mean": ..., "n": ...}` over the rows that carry that key.

    THE COUNT TRAVELS WITH THE MEAN, because the block's `runs` cannot stand in
    for it. The names are read from the records, so a dimension present on 3 of
    40 lines is a shape this walk produces -- a newer rubric's sixth dimension
    against an older one's five, both live in an append-only file. Reporting its
    mean beside a `runs` of 40 with nothing saying 3 is a denominator moving
    invisibly, which is the failure `dropped` exists to prevent, one level down.

    `bool` is a subclass of `int`, so the flag rates and the dimension means are
    the same arithmetic and share this walk rather than drifting apart in two
    copies. `_readable_rubric` has already refused everything else, so there is
    nothing left here to raise on.
    """
    blocks = [getattr(row, attribute) for row in rows]
    means: dict[str, dict] = {}
    for name in sorted({name for block in blocks for name in block}):
        values = [block[name] for block in blocks if name in block]
        means[name] = {"mean": sum(values) / len(values), "n": len(values)}
    return means


def print_summary(result: dict) -> None:
    """Say the whole reading out loud, and then say what it is worth.

    Section order is the report's order of authority: the win-rate matrix is
    primary, the rubric profile is diagnostic beside it, and the Elo table is
    descriptive of the matrix. The κ caveat comes last of the number sections
    and is UNCONDITIONAL -- see `KAPPA_CAVEAT`. Warnings and errors follow it
    because they are about the batch, not about the numbers.

    `finally`, not "at the end". Every number is already on the terminal by the
    time the caveat is due, so a reader who gets the matrix and not the caveat
    is worse off than one who gets neither -- and a `KeyError` from a summary
    shaped by an older or newer reader would produce exactly that. The
    exception still propagates; it just does not take the caveat with it.
    """
    try:
        _print_reading(result)
    finally:
        _print_kappa_caveat()

    for warning in result["warnings"]:
        print(f"\nWARNING: {warning}")
    if result["errors"]:
        print(f"\n{len(result['errors'])} unit(s) produced NO line:")
        for line in result["errors"]:
            print(f"  - {line}")
        print(
            "\nThese are holes in the derived view, not verdicts. The runs and "
            "the grades are untouched and re-running this command judges them."
        )


def _generation_label(generation: tuple) -> str:
    """One judge generation, said the same way over every block that has one.

    `(judge_model_id, judge_prompt_version, rubric_version)` -- the three
    fields `_comparison_key` and `_rubric_profile` partition on, and therefore
    the three a reader needs in order to know which two blocks are comparable.
    One phrasing across the matrix, the profile and the Elo tables, because
    three spellings of one generation read as three generations.
    """
    judge_model_id, prompt_version, rubric_version = generation
    return (
        f"judge {judge_model_id}, prompt v{prompt_version}, "
        f"rubric {rubric_version}"
    )


def _print_reading(result: dict) -> None:
    """Every number, in the report's order of authority.

    Every header prints whether or not it has rows under it. A `--no-rubric`
    pass showing an empty rubric section is honest; a section that vanishes
    when it is empty makes two passes over one collection print two different
    shapes, and a reader diffing them cannot tell an absent section from an
    absent feature.
    """
    print(
        f"\n{len(result['judged'])} judged, "
        f"{len(result['gate_decided'])} gate-decided, "
        f"{len(result['errors'])} errored, "
        f"{len(result['skipped'])} already judged"
    )
    summary = result["summary"]
    lines = summary["lines"]
    # Said after the invocation counts and labelled, because everything below is
    # the whole campaign: a reader who took it for this pass would read a
    # finished collection off a resume that judged three units.
    print(
        f"\nsummary over {result['audited']} judgment(s) in the judgment file "
        f"({result['from_prior_invocations']} from prior invocation(s)), "
        "deduped to the last line per resume identity"
    )
    print(f"  rubric          {lines['rubric']}")
    print(f"  pairwise votes  {lines['pairwise_votes']}")
    print(f"  gate-decided    {lines['gate_decided']}")
    for verdict, count in sorted(summary["vote_verdicts"].items()):
        print(f"  vote verdict    {verdict}: {count}")

    print(
        "\npairwise win rates over COMPARISONS, not votes -- the two forced "
        "positions are one comparison, a split between them is a tie, a tie "
        "is half a point, and a gate-decided pair is a win for the side the "
        "ladder picked. ONE BLOCK PER JUDGE GENERATION: a re-judge under a "
        "second oracle is a second reading of this collection, never more "
        "comparisons of it, and prompt v1 (three votes at random positions) "
        "is its own block for the same reason"
    )
    for generation, block in sorted(summary["comparisons"].items()):
        # The generation is named above every block for `_rubric_profile`'s
        # reason. A block that does not name its oracle is one the reader
        # pools in their head, which is the same wrong number the partition
        # just kept out of the arithmetic.
        print(f"  {_generation_label(generation)}")
        for (model_x, model_y), row in sorted(block.items()):
            print(f"    {model_x} vs {model_y}")
            print(
                f"      {row['wins_x']}-{row['wins_y']}-{row['ties']} "
                f"(W-L-T for {model_x}) over {row['comparisons']} "
                f"comparison(s): {model_x} {row['win_rate_x']:.1%} / "
                f"{model_y} {row['win_rate_y']:.1%}"
            )
            print(
                f"      {row['voted']} judged, "
                f"{row['gate_decided']} gate-decided"
            )

    print(
        "\nrubric profile per model and judge generation -- diagnostic, never "
        "summed across dimensions and never joined to resolved. Each mean is "
        "on its own rubric's anchored scale, over the n line(s) that carried "
        "that name"
    )
    for key, row in sorted(summary["rubric_profile"].items()):
        # The generation is named beside every block, never folded into one.
        # Two rubric versions disagreeing about one arm is the finding.
        print(
            f"  {key[0]}  ({row['runs']} run(s); "
            f"{_generation_label(_generation_of(key))})"
        )
        # One column width across both blocks, wide enough for the longest
        # label either can produce, so the means and the rates line up under
        # each other rather than under two different left edges.
        for name, cell in sorted(row["dimensions"].items()):
            print(f"    {name:26} {cell['mean']:.2f}  (n={cell['n']})")
        for name, cell in sorted(row["flags"].items()):
            print(f"    {'flag ' + name:26} {cell['mean']:.1%}  (n={cell['n']})")

    print(
        "\nRatings -- DESCRIPTIVE, they follow the win rates above rather "
        "than adjudicating them. Bradley-Terry maximum likelihood, reported "
        "on the Elo scale (400/log10 spacing, mean anchored at 1000). ONE "
        "TABLE PER JUDGE GENERATION, for the matrix's reason: two oracles' "
        "verdicts about one pair are two opinions, and pooling them prints a "
        "fit to a joint opinion neither of them gave"
    )
    for generation, ratings in sorted(summary["elo"].items()):
        print(f"  {_generation_label(generation)}")
        for model, rating in sorted(
            ratings.items(), key=lambda item: (-item[1], item[0])
        ):
            print(f"    {model:24} {rating:8.1f}")

    if summary["superseded_gate_decided"]:
        print(
            f"\n{summary['superseded_gate_decided']} gate-decided line(s) were "
            "SUPERSEDED by real votes for the same comparison and left out of "
            "the numbers above. The usual cause is a re-grade that flipped the "
            "losing side, making a pair the ladder had settled judgeable; the "
            "judgment file is append-only, so the older line stays on disk and "
            "the disagreement is the finding."
        )
    for reason, count in sorted(summary["dropped"].items()):
        if count:
            print(
                f"\n{count} line(s) or comparison(s) could not be aggregated "
                f"({reason}) and are absent from every number above."
            )


def _print_kappa_caveat() -> None:
    """The one line in this file that no argument, flag or branch can suppress.

    The `except` is not decoration: `KAPPA_CAVEAT` carries a κ and an em dash,
    and a stdout the environment pinned to ASCII raises `UnicodeEncodeError` on
    it -- which drops the caveat and takes the exit path down with it. That is
    the failure this line exists to prevent, reached through an environment
    variable rather than a flag, so the fallback says the same sentence in
    letters every terminal has.
    """
    try:
        print(f"\n{KAPPA_CAVEAT}")
    except UnicodeEncodeError:
        print(f"\n{_KAPPA_CAVEAT_ASCII}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Judge a stored collection offline: rubric profiles and "
                    "blind pairwise votes, in "
                    "<event-log>/judgments/judgments.jsonl. The event log and "
                    "the grade file are never written.",
    )
    parser.add_argument("--event-log", required=True)
    parser.add_argument("--taskset", default=str(DEFAULT_TASK_SET))
    parser.add_argument(
        "--only-task", nargs="+", metavar="TASK_ID",
        help="judge only these task_ids (default: every task in the log)",
    )
    parser.add_argument(
        "--samples", nargs="+", type=int, metavar="INDEX",
        help="judge only these sample indices. Subsample SAMPLES if cost "
             "binds, never pairs: dropping pairs breaks the Elo graph's "
             "connectivity, subsampling only widens the intervals",
    )
    parser.add_argument(
        "--re-judge", action="store_true",
        help="append a new line for every selected unit, even one already "
             "judged under this judge model, prompt version and rubric",
    )
    parser.add_argument(
        "--judge-model", default=JUDGE_MODEL_ID_DEFAULT,
        help="PINNED model id, never an alias -- a floating alias is a moving "
             f"oracle (default: {JUDGE_MODEL_ID_DEFAULT})",
    )
    # No `--votes`. Under forced positions a comparison is exactly one vote per
    # entry in `VOTE_POSITIONS`, so the only thing a count could express is a
    # mistake -- and argparse refusing the flag outright is the one version of
    # that refusal an operator cannot misread.
    parser.add_argument(
        "--no-rubric", action="store_true",
        help="pairwise only. The rubric is diagnostic, so it is the first "
             "thing to run at a lower N when cost binds",
    )
    parser.add_argument(
        "--max-consecutive-errors", type=int, metavar="N",
        default=MAX_CONSECUTIVE_ERRORS,
        help="abort after N units fail in a row (default: "
             f"{MAX_CONSECUTIVE_ERRORS}). The breaker is placed against a "
             "credential that died mid-pass; raise it to judge PAST units "
             "that fail deterministically, which otherwise abort every "
             "resume in the same place",
    )
    args = parser.parse_args(argv)
    if args.max_consecutive_errors < 1:
        # `parser.error` rather than a raise: this is a usage mistake, and an
        # operator who meant `-1` should get the usage line and exit 2 rather
        # than a traceback out of the middle of a batch that already read the
        # collection.
        parser.error(
            "--max-consecutive-errors must be at least 1: below one, the "
            "first unit that errors ends the batch, which is a hair trigger "
            "rather than a breaker"
        )

    event_log_root = Path(args.event_log)
    try:
        tasks = load_task_set(Path(args.taskset))
    except TaskError as exc:
        print(f"task set: {exc}")
        return 1

    print(f"event log  {event_log_root}")
    print(f"grades     {grades_path(event_log_root)}")
    print(f"judgments  {judgments_path(event_log_root)}")
    print(f"task set   {args.taskset}  ({len(tasks)} task(s))")
    print(
        f"judge      {args.judge_model}, prompt v{JUDGE_PROMPT_VERSION}, "
        f"rubric {RUBRIC_VERSION}"
    )

    try:
        result = judge_event_log(
            event_log_root, tasks,
            judge_model_id=args.judge_model,
            only_tasks=args.only_task,
            sample_indices=args.samples,
            rubric=not args.no_rubric,
            re_judge=args.re_judge,
            max_consecutive_errors=args.max_consecutive_errors,
        )
    except ResumeRefused as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    print_summary(result)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
