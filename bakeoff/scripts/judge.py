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

WHAT THE OPERATOR SEES, AND WHY ANY OF IT IS HERE
-------------------------------------------------

A full pass is ~9,600 paid calls over many more hours than a credential lives,
and it used to print five header lines and then NOTHING until the summary. That
is not a cosmetic gap: it made a working pass indistinguishable from a hung one,
gave the operator no way to know what the pass had committed to spending before
it started spending, and left the whole reading hostage to the one exit a long
batch actually takes. Four things are placed against that, and each of them is
about a decision somebody has to make while the batch is still running:

1. **The census is printed before the first paid call.** Phase 1 of the walk is
   pure selection -- no calls, no writes -- so the count of rubric calls, votes,
   pairs, gate-decided pairs and skips is known before a credential is minted.
   `--only-task` narrowed to nothing, a resume that has everything already, a
   grade file from another collection: all of them read as one line at the top
   rather than as a suspiciously short summary forty hours later.
2. **One line per ATTEMPTED unit**, naming task, sample and arms -- `_unit_label`,
   the same string the errors use. Skipped units are a number in the census and
   never a line: a resume is skip-heavy by construction, and one line per skip
   is thousands of lines saying nothing happened.
3. **A batch that cannot record what it buys stops.** `StorageFailure` wraps
   `write_payload` and `append_judgment` and NOTHING else, so a full disk is
   batch-fatal while a `ConnectionResetError` from the seam -- also an `OSError`
   -- stays the per-unit error it has always been. The wrap scope is the whole
   of that distinction.
4. **Ctrl-C is an exit with a report.** `KeyboardInterrupt` is caught around the
   walk rather than allowed to unwind through it, so the reading over everything
   the pass DID buy survives the way every long pass actually ends. Exit 130.

And one refusal before any of it: `CollectionNotFound`. `EventLog.__init__`
mkdirs `runs/`, so a mistyped `--event-log` used to be CREATED, found empty, and
reported as a clean pass over zero units -- every number in it zero and none of
them wrong. The check is the first statement of `judge_event_log`, before that
constructor can run.

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
    0    every selected unit produced its lines
    1    at least one unit errored, or the batch was refused
    130  the operator interrupted the pass (128 + SIGINT). Distinct from 1 on
         purpose: a shell reads 130 as "somebody stopped this" and 1 as "the
         batch found problems", and a resume is the answer to only one of them
"""

from __future__ import annotations

import argparse
import itertools
import math
import random
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bakeoff.eventlog import EventLog  # noqa: E402
from bakeoff.grade_schema import (  # noqa: E402
    GradeRecord,
    # `grade.py` owns where the grade file lives and this driver's whole gate
    # comes out of it, so a second copy of the path is how a moved grade file
    # turns into a judging pass that reports "nothing is graded" and judges
    # nothing -- silent, and in the direction that looks like success. Taken
    # from `bakeoff.grade_schema` rather than from `scripts.grade`, which is
    # where it used to live: that import cost this driver `scripts.grade`'s
    # whole module graph, `bakeoff.images` -> `docker` included, so an analysis
    # box with no Docker package could not read `--help`. Same one derivation,
    # one import edge lighter.
    grades_path,
    load_grades,
)
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
    new_usage_totals,
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

#: The cluster bootstrap behind every interval on the printout. §10.3: "any
#: metric reported without a confidence interval is not reported", and §4.4
#: says which interval -- resampled over TASKS, because comparisons inside one
#: task are correlated and effective sample size tracks the task count rather
#: than the comparison count. Resampling comparisons independently would print
#: a band several times too narrow, which is worse than printing none: a naive
#: +-4 points reads as a resolved result where the honest figure is nearer +-12
#: and the spec says in as many words that the optimistic figures "must not be
#: quoted".
#:
#: 1,000 resamples: the 2.5/97.5 percentiles are stable to well under the
#: tenth of a point the printout shows, and the cost is bounded by the refit --
#: measured at ~0.1s per thousand fits on a four-arm, 60-comparison block, and
#: a few seconds on a full 60-task collection, at the end of a pass that took
#: hours. A SEED rather than fresh entropy, and a fresh generator per
#: `summarize` call rather than module state: two readings of one unchanged
#: file must print one interval, or the band appears to move while nothing
#: about the collection did -- and it moves in the last digit, where it looks
#: like a change in the numbers rather than like noise the summary invented.
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 0

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


class CollectionNotFound(RuntimeError):
    """`--event-log` names a directory that is not a collection root.

    A TYPO, and the reason this needs its own refusal is that the old failure
    mode was not a failure at all. `EventLog.__init__` mkdirs `runs/`, so a
    mistyped path was CREATED, read as an empty collection, and reported
    through a complete summary with an exit status of 0 -- every number in it
    zero, none of them wrong, and nothing anywhere saying the pass had judged
    a directory that did not exist a second earlier. The directory it left
    behind is the second half of the damage: it looks like a real collection
    root to the next command that points at it.

    Raised as the FIRST statement of `judge_event_log`, which is the only place
    it can be raised from and still be true to its own name -- one line later
    the constructor has already made the thing this says is missing.

    Its own class rather than `ResumeRefused`, on that class's own reasoning:
    the two refuse over different things -- a file that cannot be read
    completely against a collection that is not there -- and one `except` that
    caught both would report a damaged judgment file as a typo.
    """


class StorageFailure(RuntimeError):
    """`write_payload` or `append_judgment` raised `OSError`: the disk has
    stopped accepting what this pass is buying.

    BATCH-FATAL, and the only failure in this driver that is. Everything else
    here is per-unit on purpose -- one unreadable diff, one model that cannot
    produce JSON, one payload that tripped the scan costs its own line and not
    the rest of the batch. A full disk is the opposite shape: it fails EVERY
    write from the moment it starts, so the isolation that makes one bad unit
    cheap makes this one catastrophic. Measured shape of the old behaviour: the
    driver went on calling a paid model for every remaining unit and recorded
    not one of the answers, at a cost per unit and nothing on disk to show for
    any of it.

    A DISTINCT CLASS BECAUSE THE WRAP SCOPE IS THE WHOLE CONTRACT. `OSError` is
    the base class of `ConnectionResetError` as much as of `ENOSPC`, and a
    reset arriving from the completion seam is an ordinary per-unit failure a
    long batch must survive. So the wrap sits around the two STORE calls and
    nothing else -- not around the unit, not around the model call -- and this
    class is what carries "the disk said no" out past a per-unit `except
    Exception` that would otherwise flatten it back into one more unit error.

    Deliberately NOT an `OSError` itself: it passes through the builders and
    the executor, and a `StorageFailure` that were an `OSError` would be caught
    and re-wrapped by the next `except OSError` it met, losing on the way out
    exactly the distinction it exists to carry.
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

    ONE OTHER THING RAISES IT, and it is not a run of anything: a
    `StorageFailure` aborts on the FIRST unit rather than the fifth, under
    `_storage_abort_message`. Both end the walk the same way -- the warning,
    the partial return, the summary over everything already bought -- because
    the operator's next step is the same in both cases and it is not reading a
    traceback. What differs is what the message tells them to fix, which is
    why the two messages are built by two functions rather than parameterised
    out of one.
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


def lazy_live_completion(judge_model_id: str,
                         usage_totals: dict[str, int] | None = None
                         ) -> CompleteFn:
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

    `usage_totals` is passed THROUGH rather than counted here, and the laziness
    is why that matters: this wrapper sees a prompt and a reply, not a response,
    so counting at this layer would report a call count and no tokens at all.
    It is also the caller's dict rather than one made here, so a batch that
    never builds the live judge -- fully gate-decided, or fully skipped --
    still hands the driver a set of zeros to print rather than a `None` that
    would read as "not counted".
    """
    built: dict[str, CompleteFn] = {}

    def complete(prompt: str) -> str:
        if "fn" not in built:
            built["fn"] = live_completion(
                judge_model_id, usage_totals=usage_totals
            )
        return built["fn"](prompt)

    return complete


def _write_payload_or_fail(payloads: Path, judgment_id: str,
                           payload: dict) -> tuple[str, str]:
    """`write_payload`, with an `OSError` promoted to `StorageFailure`.

    THE WRAP IS EXACTLY THIS CALL, which is the whole of what makes the batch
    abort safe -- see `StorageFailure`. A wrap one frame wider would take the
    completion seam with it, and a `ConnectionResetError` from a flaky endpoint
    is an `OSError` too: every reset in a nine-hour batch would end the pass.

    `PayloadSecretsFound` is deliberately left to propagate. It is not an
    `OSError`, it is not a disk problem, and it is per-unit by design -- that
    payload is refused, that unit costs its line, and nothing else changes.
    """
    try:
        return write_payload(payloads, judgment_id, payload)
    except OSError as exc:
        raise StorageFailure(
            f"the payload for {judgment_id} could not be written under "
            f"{payloads}: {type(exc).__name__}: {exc}"
        ) from exc


def _append_or_fail(path: Path, judgment: JudgeRecord) -> None:
    """`append_judgment`, with an `OSError` promoted to `StorageFailure`.

    The second half of the same wrap, and the more expensive half: the payload
    is already on disk by the time this runs, so a failure here leaves a
    payload no line names. That is the documented safe direction (module
    docstring, step 7) -- the unit is simply retried on the next pass -- but it
    is debris, and it is another reason the batch stops here rather than
    accumulating one orphan per remaining unit.
    """
    try:
        append_judgment(path, judgment)
    except OSError as exc:
        raise StorageFailure(
            f"the judgment line for {judgment.judgment_id} could not be "
            f"appended to {path}: {type(exc).__name__}: {exc}"
        ) from exc


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

    Both store calls go through `_write_payload_or_fail`/`_append_or_fail`,
    which promote an `OSError` to `StorageFailure` and are wrapped around those
    two calls and nothing else. `judge_rubric` above is deliberately OUTSIDE
    the wrap: a socket error from the seam is an `OSError` as well, and it is a
    per-unit failure rather than a reason to end the batch.
    """
    payload, rendered, result = judge_rubric(inputs, complete)
    judgment_id = uuid.uuid4().hex
    written, payload_sha = _write_payload_or_fail(
        payloads, judgment_id, payload
    )
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
    _append_or_fail(path, judgment)
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
    written, payload_sha = _write_payload_or_fail(
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

    _append_or_fail(path, judgment)
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
    # Through the same wrap as the other two, even though this unit is free.
    # A disk that has stopped accepting writes stops the batch whichever kind
    # of line discovered it: the next unit is a paid one, and it would be
    # bought against the same disk.
    _append_or_fail(path, judgment)
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


def _storage_abort_message(label: str, exc: BaseException) -> str:
    """The abort a disk causes, phrased as a disk problem.

    ITS OWN MESSAGE rather than a case inside `_abort_message`, for that
    function's own reason one step further along: the abort's whole job is to
    send the operator to the thing that will fix it, and a full disk shares no
    fix with either paragraph there. It is not a credential (a fresh token
    writes no better), and it is not the data (raising the limit or narrowing
    `--only-task` reaches more units against the same disk). The fix is space,
    or a permission, and then the same command.

    ONE unit, not a run of them, and the message says so by naming the unit
    that discovered the problem rather than listing five. That is the point of
    the whole decision: a batch aborting on the fifth ENOSPC would have bought
    four verdicts it could not record, at a paid call each.
    """
    return (
        f"{label} could not be recorded: writes are failing ({exc}). "
        "Aborting the batch with units left unattempted -- continuing would "
        "buy verdicts that cannot be recorded, which is a paid model call per "
        "unit and nothing on disk to show for any of it. Free space (or fix "
        "the permission on the judgments directory) and resume with the same "
        "command: judgments are append-only and the resume is keyed on units "
        "already bought, so everything written before this point is kept."
    )


def _interrupt_message(attempted: int, total: int) -> str:
    """Ctrl-C, reported as an exit rather than as a crash.

    An uncaught `KeyboardInterrupt` unwinds through `judge_event_log` and takes
    the summary with it, which is the worst possible place to lose a report:
    this is how a long pass USUALLY ends -- an operator stops a batch that has
    been running for hours -- and the lines it bought are on disk either way.
    What the traceback destroys is the only place anybody meets them.

    The counts are attempted-of-selected rather than a percentage, because the
    number an operator acts on is how much is left.
    """
    return (
        f"interrupted by the operator (Ctrl-C) after {attempted} of {total} "
        "selected unit(s). Nothing is lost -- judgments are append-only and "
        "the resume is keyed on units already bought -- so resume with the "
        "same command to pick up where this stopped. The reading below covers "
        "everything the pass did buy."
    )


# ---------------------------------------------------------------------------
# the worklist: what phase 1 selects and phase 2 executes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """One unit of work, chosen by phase 1 and executed by phase 2.

    THE SPLIT EXISTS SO THE CENSUS CAN BE HONEST. Selection is a pure function
    of what is on disk -- which runs are graded, which units the resume already
    holds, what `--only-task` and `--samples` name -- and none of it needs a
    credential or a call. Deciding all of it first is what lets the driver say
    what the pass has committed to spending BEFORE it spends any of it, which
    is the one moment an operator can still act on the number.

    Selection also stays exactly where it was in the old single-pass walk:
    cell, then rubric per model, then pair, then position, `sorted()` at every
    level. That order is pinned by the resume-determinism tests and it is not
    arbitrary decoration -- a killed pass that resumed in a different order
    would redo different work, and the two positions of a comparison are
    adjacent so a batch killed mid-comparison leaves the missing position as
    the very next unit a resume buys.

    FROZEN and carrying ids rather than records. `records` and `gating` are
    both keyed on `run_id` and both live for the whole call, so holding the id
    is one dict lookup at execution time against a second reference to a
    mutable record here -- and a frozen descriptor cannot be edited between the
    census that counted it and the executor that runs it.

    `run_ids` is canonical order: one id for a rubric unit, and the two ids
    SORTED for a pairwise one. Sorted-by-run-id is not the model order, which
    is why `label` is built at selection time by the same code that knows both
    -- a label naming each arm as the other is worse than no label at all
    (`_unit_label`).
    """

    #: `"rubric"`, `"gate-decided"` or `"vote"`. What the census counts and
    #: what the executor dispatches on.
    kind: str
    #: `_unit_label`'s string, built once at selection and used for the
    #: progress line, the error line and the abort listing -- so the three
    #: cannot describe the same unit three ways.
    label: str
    task: Any
    sample_index: int
    run_ids: tuple[str, ...]
    #: Votes only. `None` on the other two kinds, which is the schema's own
    #: contract for a gate-decided line.
    vote_index: int | None = None
    position: str | None = None


def _census(worklist: list[_Unit], skipped: list) -> dict:
    """What phase 1 selected, in the units an operator budgets in.

    PAIRS BESIDE VOTES because they are the two different questions. Votes are
    the paid calls; pairs are the comparisons those calls buy, at two forced
    positions each. A census reporting only one of them makes the operator do
    the arithmetic to reach the other, and getting it wrong by a factor of two
    is how a pass gets started that nobody meant to pay for.

    `skipped` is a count and never a list of lines -- see the progress printer.
    """
    votes = [unit for unit in worklist if unit.kind == "vote"]
    return {
        "rubric": sum(1 for unit in worklist if unit.kind == "rubric"),
        "votes": len(votes),
        # Run ids are unique within a collection, so the two of a pair identify
        # the comparison without the task and sample riding along.
        "pairs": len({unit.run_ids for unit in votes}),
        "gate_decided": sum(
            1 for unit in worklist if unit.kind == "gate-decided"
        ),
        "skipped": len(skipped),
        "units": len(worklist),
    }


def _census_line(selected: dict) -> str:
    return (
        f"selected: {selected['rubric']} rubric, {selected['votes']} votes "
        f"over {selected['pairs']} pairs, {selected['gate_decided']} "
        f"gate-decided; {selected['skipped']} already judged (skipped)"
    )


def _seconds(value: float) -> str:
    """One unit's duration. One decimal, because a judge call is seconds."""
    return f"{value:.1f}s"


def _elapsed(value: float) -> str:
    """Wall clock since the walk began, in the largest units that fit.

    `8m12s` and `3h07m40s` rather than `492.3` -- the number is read to answer
    "how much longer", against a pass measured in tens of hours, and seconds
    past the first minute is a number nobody converts in their head.

    Truncated to whole seconds and zero-padded below the leading unit, so the
    column does not jitter between lines.
    """
    hours, rest = divmod(int(value), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


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

    TWO PHASES, and the split is what makes the census honest. Phase 1 selects
    -- a pure function of what is on disk, making no call and writing nothing
    -- and produces the worklist in exactly the order the single-pass walk used
    to execute it in. The census is printed off that worklist, so it is on the
    terminal before the first credential is minted, and phase 2 then walks the
    list printing one line per attempted unit. A census computed anywhere else
    would be a report rather than a decision an operator can still act on, and
    a walk that decided as it went could not produce one at all.

    `_judgeable_inputs` deliberately stays in PHASE 2, even though it is
    selection-shaped. Building payload inputs walks two diffs per run
    (`similarity_context`), it is exactly the work a skip-heavy resume must not
    pay for, and its failures are per-unit error lines rather than selection
    facts -- a run with no `final_diff` was still selected, and reporting it as
    though it were never chosen would hide it.

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

    Raises `CollectionNotFound` before touching anything, `ResumeRefused` if
    either input file cannot be read completely, and nothing else: a batch that
    got as far as the walk returns its partial reading whatever happens in it.
    """
    # FIRST, and before `EventLog(...)` below, which mkdirs `runs/` -- see
    # `CollectionNotFound`. `Path()` creates nothing, so this is still the
    # first statement that could.
    event_log_root = Path(event_log_root)
    if not (event_log_root / "runs").is_dir():
        raise CollectionNotFound(
            f"{event_log_root} is not a collection: it holds no runs/ "
            "directory. A collection root is the directory a harness run wrote "
            "its event log into -- runs/ (one JSON per run) and index.jsonl, "
            "with grades/ beside them once scripts/grade.py has run. Judging "
            "only reads those, so a path that does not already hold them has "
            "nothing to judge; the usual cause is a typo in --event-log, which "
            "would otherwise be created empty and reported as a clean pass "
            "over zero units."
        )
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

    # The usage accumulator belongs to the LIVE seam and to nothing else. A
    # caller that injected its own `complete` gets `judge_usage is None` --
    # zeros would be a measurement of a batch that made no live call, and the
    # driver has no way to count tokens through a function whose contract is
    # one string in and one string out.
    judge_usage: dict[str, int] | None = None
    if complete is None:
        judge_usage = new_usage_totals()
        complete = lazy_live_completion(judge_model_id, judge_usage)

    judged: list[JudgeRecord] = []
    gate_decided: list[JudgeRecord] = []
    skipped: list[tuple] = []
    missing_tasks: set[str] = set()

    # ONE cache for the whole walk, keyed on `run_id`, which is unique within a
    # collection -- so the flat worklist can share it where the old per-cell
    # dict could not. Still lazy and still filled by `_judgeable_inputs`, so a
    # skip-heavy resume builds nothing: what changes is only that a run
    # appearing in two cells is walked once instead of twice. It holds no diff
    # text of its own beyond what `records` already holds.
    inputs: dict[str, PayloadInputs] = {}

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
    # Closures rather than inline bookkeeping, so the per-unit handlers cannot
    # each grow their own version of "does this one count" -- which is exactly
    # where a gate-decided pair would quietly become a success.
    failure_run: list[tuple[str, bool]] = []

    # The unit currently being attempted, and how many have been. Written by
    # `_begin` and read by `_finish`, which is the ONE place a progress line is
    # printed. Every outcome a unit can have ends in a `_finish` call: four of
    # the five go through an outcome closure below (`_unit_succeeded`,
    # `_unit_failed`, `_unit_failed_fatally`, `_gate_unit_failed`,
    # `_unit_not_judged`), and the fifth -- a gate-decided pair that WROTE its
    # line -- calls `_finish("ok")` from `_attempt` directly, because it must
    # not reset the breaker and so cannot use `_unit_succeeded`. That one
    # deliberate exception is why the rule is "exactly one `_finish` per
    # attempted unit" rather than "every path goes through a closure".
    progress: dict[str, Any] = {
        "index": 0, "label": "", "start": 0.0, "attempted": 0,
        "total": 0, "batch_started": 0.0,
    }

    def _begin(index: int, unit: _Unit) -> None:
        progress.update(
            index=index, label=unit.label, start=time.monotonic()
        )

    def _finish(status: str) -> None:
        """One progress line for the unit just attempted.

        `ok` or `ERROR <what>`, then the two clocks. The unit duration is what
        says whether the pass is moving; the elapsed total is what an operator
        divides by the index to estimate the rest, which is the number they
        actually want at hour nine of a forty-hour batch.

        Printed AFTER the outcome is known and BEFORE any abort is raised, so
        the unit that ended the batch is on the terminal like every other one.
        A unit whose error line appears in the summary with no progress line
        above it reads as a unit that never ran.

        THE ENCODING GUARD IS NOT DECORATION, and it is the one thing here that
        is about the batch rather than about the report. This line is the only
        collection-derived text printed from INSIDE the walk: the label carries
        a task id, model names and run ids, and on a stdout the environment
        pinned to ASCII (`LC_ALL=C`, or a pipe into a tool that did) a single
        non-ASCII character in any of them raises `UnicodeEncodeError` from
        `print`. That exception is not an `Exception` the per-unit handler
        catches -- it is raised past it, past `except _BatchAborted`, past
        `except KeyboardInterrupt` and out of `main` -- so a batch that had
        been running for hours would die on a traceback with no summary, no
        usage totals and every remaining unit unbought. Exactly the failure
        class this whole section exists to remove, arriving from the code added
        to remove it. `_print_kappa_caveat` carries the same guard for the same
        reason, reached by an environment variable rather than by a flag.

        `backslashreplace` rather than a dropped label: an id an operator has
        to grep for is worth more mangled than absent, and the escape is
        reversible. The fallback is pure ASCII by construction, so it cannot
        raise the same error a second time.
        """
        now = time.monotonic()
        progress["attempted"] += 1
        line = (
            f"[{progress['index']}/{progress['total']}] {progress['label']} "
            f"{status} (unit {_seconds(now - progress['start'])}, "
            f"elapsed {_elapsed(now - progress['batch_started'])})"
        )
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "backslashreplace").decode("ascii"))

    def _unit_succeeded() -> None:
        """A unit produced its line, so whatever was failing is not systemic."""
        failure_run.clear()
        _finish("ok")

    def _unit_failed(exc: BaseException) -> None:
        """Record one unit's error, and abort the batch on a run of them.

        See `MAX_CONSECUTIVE_ERRORS`. The EXCEPTION is taken rather than a
        finished message, because the abort needs two things from it -- the
        text for the error line and `is_auth_failure`'s answer for the
        diagnosis -- and a caller that formatted the text itself would be the
        one place the classification could be skipped.
        """
        label = progress["label"]
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        failure_run.append((label, is_auth_failure(exc)))
        _finish(f"ERROR {type(exc).__name__}")
        if len(failure_run) >= max_consecutive_errors:
            raise _BatchAborted(_abort_message(failure_run))

    def _unit_failed_fatally(exc: BaseException) -> None:
        """A `StorageFailure`: the disk stopped, so the batch stops with it.

        NOT a breaker event and deliberately not counted as one -- see
        `StorageFailure`. The breaker is a heuristic about a run of failures
        that might be systemic; this is a fact about the one that just
        happened, and waiting for four more would mean four more paid calls
        whose answers go nowhere.

        The error line is recorded first so the unit that discovered the
        problem is in the list beside the abort, then the progress line, then
        the abort itself.
        """
        errors.append(f"{progress['label']}: {type(exc).__name__}: {exc}")
        _finish(f"ERROR {type(exc).__name__}")
        raise _BatchAborted(_storage_abort_message(progress["label"], exc))

    def _gate_unit_failed(exc: BaseException) -> None:
        """A gate-decided pair that could not be written, but not by the disk.

        Straight into `errors` and past the breaker in both directions: that
        unit makes no model call, so it is neither evidence that the credential
        is dead nor evidence that it recovered. See `_BatchAborted`.
        """
        errors.append(f"{progress['label']}: {type(exc).__name__}: {exc}")
        _finish(f"ERROR {type(exc).__name__}")

    def _unit_not_judged(run_ids: Sequence[str]) -> None:
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
        errors.append(f"{progress['label']}: not judged: {why}")
        # It IS an attempted unit as far as the terminal is concerned -- it was
        # selected, it was walked, and it produced no line -- so it prints
        # like one. What it is not is evidence about the judge, which is the
        # breaker's question and not this one.
        _finish("ERROR no judgeable payload")

    def _judgeable_inputs(run_id: str, grade: GradeRecord,
                          task) -> PayloadInputs | None:
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
            return _inputs_for(inputs, records[run_id], grade, task)
        except Exception as exc:  # noqa: BLE001 - one run, not the batch
            unjudgeable[run_id] = f"{type(exc).__name__}: {exc}"
            return None

    # ----- phase 1: selection. No call, no write, no credential -----------
    #
    # The order below IS the old single-pass walk's order, statement for
    # statement: cell, then rubric per model, then pair, then position,
    # `sorted()` at every level. Resume determinism is pinned on it -- a killed
    # pass that resumed in a different order would redo different work -- and
    # the two positions of a comparison stay adjacent so a batch killed
    # mid-comparison leaves the missing position as the very next unit a resume
    # buys. What moved out of this loop is the paying and the writing, and
    # nothing else.
    worklist: list[_Unit] = []

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

        if rubric:
            for model in sorted(cell):
                record = cell[model]
                grade = gating[record.run_id]
                # Selection-time gate. The assert before the append is the one
                # that is load-bearing; this is what keeps the batch from
                # paying for a call it must then refuse to record.
                if grade.resolved is not True:
                    continue
                key = _rubric_key(record.run_id, judge_model_id)
                if not re_judge and key in done:
                    skipped.append(key)
                    continue
                worklist.append(_Unit(
                    kind="rubric",
                    label=_unit_label(
                        "rubric", task_id, sample_index,
                        (record.model,), (record.run_id,),
                    ),
                    task=task,
                    sample_index=sample_index,
                    run_ids=(record.run_id,),
                ))

        for model_x, model_y in itertools.combinations(sorted(cell), 2):
            # Canonical a/b is the two run ids sorted, NOT the two models: the
            # identity has to be stable under a model rename, and `(x, y)` and
            # `(y, x)` must be one comparison or the position-swap probe can no
            # longer find its own pairs.
            run_id_a, run_id_b = sorted(
                (cell[model_x].run_id, cell[model_y].run_id)
            )
            grade_a, grade_b = gating[run_id_a], gating[run_id_b]
            passed_a = grade_a.resolved is True
            passed_b = grade_b.resolved is True
            # Named off the RUN ids, not off `(model_x, model_y)`: a/b is the
            # two run ids sorted, so the model order can be the other one, and
            # a label naming each arm as the other is worse than no label at
            # all.
            arms = (records[run_id_a].model, records[run_id_b].model)

            if not passed_a and not passed_b:
                # Nothing. Neither side is a submission worth ranking, and a
                # "tie" here would be a verdict about two failures.
                continue

            if passed_a != passed_b:
                key = _pairwise_key(task_id, sample_index, run_id_a,
                                    run_id_b, None, judge_model_id)
                if not re_judge and key in done:
                    skipped.append(key)
                    continue
                worklist.append(_Unit(
                    kind="gate-decided",
                    label=_unit_label(
                        "gate-decided", task_id, sample_index, arms,
                        (run_id_a, run_id_b),
                    ),
                    task=task,
                    sample_index=sample_index,
                    run_ids=(run_id_a, run_id_b),
                ))
                continue

            # One vote per forced position, and `enumerate` is what ties
            # `vote_index` to `VOTE_POSITIONS` -- 0 is `a_first`, 1 is
            # `b_first`.
            for vote_index, position in enumerate(VOTE_POSITIONS):
                key = _pairwise_key(task_id, sample_index, run_id_a,
                                    run_id_b, vote_index, judge_model_id)
                if not re_judge and key in done:
                    skipped.append(key)
                    continue
                worklist.append(_Unit(
                    kind="vote",
                    # The POSITION rides along with the index, as the string
                    # this replaced carried it. It is derivable -- `enumerate`
                    # over `VOTE_POSITIONS` ties the two -- but an error line
                    # is read by somebody who does not have that mapping in
                    # front of them, and `a_first`/`b_first` is what the stored
                    # `position_assignment` calls the same thing.
                    label=_unit_label(
                        f"vote {vote_index} ({position})", task_id,
                        sample_index, arms, (run_id_a, run_id_b),
                    ),
                    task=task,
                    sample_index=sample_index,
                    run_ids=(run_id_a, run_id_b),
                    vote_index=vote_index,
                    position=position,
                ))

    # ----- the census, before the first paid call -------------------------
    selected = _census(worklist, skipped)
    print(f"\n{_census_line(selected)}")
    print(
        "  the rubric and vote units above are the paid calls this pass will "
        "make; gate-decided pairs and skips cost nothing. Nothing has been "
        "asked yet."
    )

    # ----- phase 2: execution ---------------------------------------------
    progress["total"] = len(worklist)
    progress["batch_started"] = time.monotonic()
    interrupted = False

    def _attempt(unit: _Unit) -> None:
        """Execute one selected unit, and account for however it ends.

        Every path out of this ends in exactly one `_finish` call, which is
        what makes the one-line-per-attempted-unit rule structural. Four of the
        five reach it through an outcome closure; the fifth -- a gate-decided
        pair whose line was written -- calls `_finish("ok")` here directly,
        because `_unit_succeeded` would reset the breaker and that unit made no
        call, so it is no evidence the credential recovered. The exception is
        deliberate and commented at its call site.

        `StorageFailure` is caught BEFORE the bare `except Exception`, which is
        the whole of what makes a full disk batch-fatal and a `ConnectionReset`
        from the seam per-unit. Ordering the two the other way round would put
        every disk failure back into the per-unit bucket, silently.
        """
        if unit.kind == "rubric":
            (run_id,) = unit.run_ids
            grade = gating[run_id]
            unit_inputs = _judgeable_inputs(run_id, grade, unit.task)
            if unit_inputs is None:
                _unit_not_judged(unit.run_ids)
                return
            try:
                judged.append(_rubric_line(
                    records[run_id], grade, unit.task, unit_inputs,
                    payloads, path, complete, judge_model_id,
                ))
            except StorageFailure as exc:
                _unit_failed_fatally(exc)
            except Exception as exc:  # noqa: BLE001 - one unit
                _unit_failed(exc)
            else:
                _unit_succeeded()
            return

        run_id_a, run_id_b = unit.run_ids
        grade_a, grade_b = gating[run_id_a], gating[run_id_b]

        if unit.kind == "gate-decided":
            try:
                gate_decided.append(_gate_decided_line(
                    unit.task, unit.sample_index, run_id_a, run_id_b,
                    grade_a, grade_b, path, judge_model_id,
                ))
            except StorageFailure as exc:
                _unit_failed_fatally(exc)
            except Exception as exc:  # noqa: BLE001 - one unit
                _gate_unit_failed(exc)
            else:
                # `_finish` and NOT `_unit_succeeded`: this unit made no call,
                # so it is no evidence the credential recovered and must not
                # reset the breaker. See `_BatchAborted`.
                _finish("ok")
            return

        # Bound before the call rather than inline: two positional
        # `PayloadInputs` four lines apart is where an a/b transposition hides,
        # and a transposed pair produces a complete, confident, inverted
        # verdict. Both are bound BEFORE the `try` as well, so a run that
        # cannot produce inputs is pre-filtered rather than counted -- see
        # `_judgeable_inputs`.
        inputs_a = _judgeable_inputs(run_id_a, grade_a, unit.task)
        inputs_b = _judgeable_inputs(run_id_b, grade_b, unit.task)
        if inputs_a is None or inputs_b is None:
            _unit_not_judged(unit.run_ids)
            return
        try:
            judged.append(_vote_line(
                unit.task, unit.sample_index, run_id_a, run_id_b,
                inputs_a, inputs_b,
                unit.vote_index, unit.position, grade_a, grade_b,
                payloads, path, complete, judge_model_id,
            ))
        except StorageFailure as exc:
            _unit_failed_fatally(exc)
        except Exception as exc:  # noqa: BLE001 - one unit
            _unit_failed(exc)
        else:
            _unit_succeeded()

    try:
        for index, unit in enumerate(worklist, 1):
            _begin(index, unit)
            _attempt(unit)
    except _BatchAborted as exc:
        # The abort is a WARNING and not an error, because it is not a unit:
        # the units that failed are already in `errors`, and the exit code
        # follows them. The batch still returns its partial reading rather
        # than a traceback -- everything judged before the run of failures is
        # on disk and belongs in the summary.
        warnings.append(str(exc))
    except KeyboardInterrupt:
        # A `BaseException`, so the per-unit `except Exception` above never
        # sees it and the unit in flight is simply unfinished -- no line, no
        # progress line, and the resume buys it next time. Caught HERE rather
        # than allowed to unwind, because unwinding takes the summary with it:
        # see `_interrupt_message`.
        interrupted = True
        warnings.append(
            _interrupt_message(progress["attempted"], len(worklist))
        )

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
        # What phase 1 chose, so a caller can assert against the same numbers
        # the census printed rather than re-deriving them from `judged` --
        # which would be a count of what SUCCEEDED and is a different fact.
        "selected": selected,
        # Ctrl-C. A flag rather than an exception, because everything below it
        # in this dict is a real partial reading the caller should print.
        "interrupted": interrupted,
        # `None` when a caller injected its own seam -- see where it is built.
        "judge_usage": judge_usage,
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


def _task_of(key: tuple) -> str:
    """The task a `_comparison_key` belongs to -- its first element.

    `_generation_of`'s counterpart at the other end of the key, and here for
    the same reason: the bootstrap clusters on this field, and a hand-counted
    index beside a key whose layout moved would resample the wrong column --
    `sample_index` would cluster on the repeat rather than the task, which is
    precisely the correlation §4.4 says the interval must not ignore, and it
    would fail by printing a NARROWER band rather than by raising.
    """
    return key[0]


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


def _task_blocks(
    task_ids: list[str],
    outcomes: list[tuple[str, str, float]],
    cells: list[tuple[tuple[str, str], float]],
) -> list[tuple[tuple, tuple]]:
    """The three PARALLEL lists, regrouped into one block per task.

    `task_ids[i]`, `outcomes[i]` and `cells[i]` are three views of comparison
    `i`: which task it came from, its `elo_from_outcomes` triple, and its
    (sorted pair, x's score) cell for the matrix. They are parallel rather than
    one list of wide records because `elo_from_outcomes` takes triples and
    nothing else: widening its input to carry a `task_id` would put a field it
    must never read into the contract every caller and every test writes
    against, and the fit is defined over win counts -- a rating that varied
    with the task labels attached to identical counts would be a bug that only
    this function could cause.

    A BLOCK IS THE RESAMPLING UNIT (§4.4). Comparisons inside one task share
    that task's difficulty, so they travel together or the interval measures a
    sample size the collection does not have.

    Blocks come back in `_deterministic` order, and the comparisons inside each
    keep the order they were walked in, so a one-task collection resamples to
    the identical triple list every time and its interval collapses onto the
    point estimate exactly rather than to within a float wobble of it.
    """
    blocks: dict[str, tuple[list, list]] = {}
    for task_id, outcome, cell in zip(task_ids, outcomes, cells):
        block = blocks.get(task_id)
        if block is None:
            block = blocks[task_id] = ([], [])
        block[0].append(outcome)
        block[1].append(cell)
    return [
        (tuple(blocks[task_id][0]), tuple(blocks[task_id][1]))
        for task_id in _deterministic(blocks)
    ]


def _cluster_bootstrap(
    blocks: list[tuple[tuple, tuple]], rng: random.Random,
) -> tuple[dict[tuple[str, str], list[float]], dict[str, list[float]]]:
    """`BOOTSTRAP_RESAMPLES` draws of the task set, with replacement.

    Returns the resampled win rates per pair and the resampled ratings per
    model -- the samples themselves rather than percentiles, because a caller
    that finds a pair or an arm missing from every resample has to fall back to
    the point estimate, and that decision belongs where the point estimate is.

    THE WHOLE COLLECTION IS RESAMPLED ONCE PER DRAW, not once per pair: the
    pairs in a block are the same tasks seen from different sides, and drawing
    independently per pair would produce a set of intervals no single
    collection could have produced.

    The rating is REFIT on every draw rather than perturbed, which is what
    `elo_from_outcomes` being a maximum-likelihood fit over win counts buys:
    the fit is a function of the resampled counts alone, with no walk order to
    carry over from the original stream.

    Allocation-light on purpose -- this runs 1,000 times inside every summary,
    including the ones in the test suite. The triples inside a block are stored
    once and REFERENCED by each draw (they are never mutated), the pair tally
    is a two-slot list updated in place, and the only per-draw allocations are
    the draw itself and the triple list the fit needs.
    """
    rate_samples: dict[tuple[str, str], list[float]] = {}
    rating_samples: dict[str, list[float]] = {}
    if not blocks:
        return rate_samples, rating_samples

    for _ in range(BOOTSTRAP_RESAMPLES):
        outcomes: list[tuple[str, str, float]] = []
        tally: dict[tuple[str, str], list] = {}
        for block_outcomes, block_cells in rng.choices(blocks, k=len(blocks)):
            outcomes.extend(block_outcomes)
            for pair, score_x in block_cells:
                counted = tally.get(pair)
                if counted is None:
                    tally[pair] = [score_x, 1]
                else:
                    counted[0] += score_x
                    counted[1] += 1
        for pair, (points_x, comparisons) in tally.items():
            rate_samples.setdefault(pair, []).append(points_x / comparisons)
        for model, rating in elo_from_outcomes(outcomes).items():
            rating_samples.setdefault(model, []).append(rating)
    return rate_samples, rating_samples


def _interval(
    samples: list[float] | None, point: float,
) -> tuple[float, float]:
    """The 95% interval over one quantity's resamples: 2.5th to 97.5th.

    A pair or an arm can be ABSENT from a resample -- the tasks that hold it
    were not drawn -- and the percentiles are then taken over the resamples
    where it appears. That is a conditional interval and it is the honest one
    available: the alternative readings are to score the missing draws as some
    number the collection never produced, or to widen the interval by the
    probability of the arm being drawn at all, which measures the sampler
    rather than the arms. An arm that is missing from EVERY resample cannot
    have been in the collection either, so the fallback there is the point
    estimate rather than a band around nothing.
    """
    if not samples:
        return (point, point)
    ordered = sorted(samples)
    return (_percentile(ordered, 0.025), _percentile(ordered, 0.975))


def _percentile(ordered: list[float], fraction: float) -> float:
    """Linear-interpolated percentile over an ALREADY SORTED list.

    Interpolated rather than nearest-rank so the two ends of the interval move
    smoothly with the collection: at 1,000 resamples the difference is far
    inside the tenth of a point the printout shows, but nearest-rank makes the
    printed band jump by a whole resample's width when one comparison is added,
    which reads as a change in the collection.
    """
    position = (len(ordered) - 1) * fraction
    below = math.floor(position)
    above = math.ceil(position)
    if below == above:
        return ordered[below]
    return ordered[below] + (ordered[above] - ordered[below]) * (
        position - below
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

    Six aggregation rules, each placed against a specific way an append-only
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
    5. **Every rate is decomposed into what the judge said and what the ladder
       decided.** A gate-decided comparison is a TIER A result, and counting it
       in a Tier B win rate -- which is correct, see below -- means a row with
       a large gate-decided share is partly Tier A's pass rate re-derived. Two
       arms a judge cannot separate at all can sit either side of §10.1's 40%
       pairwise reference on their gate results alone. So `win_rate_x_voted`
       and `elo_voted` are reported beside the combined figures, and
       `gate_decided_share` says per ARM how much of its block the judge never
       saw. Nothing is subtracted from the primary numbers; the reader is given
       both halves instead of one blend.
    6. **Position consistency is counted, not assumed.** §4.2.3 asks for a
       position-swap probe; under two forced positions every complete
       comparison is one, so `position_consistency` reports it over the whole
       collection. A comparison is MEASURABLE when its votes cover both
       positions and CONSISTENT when every canonical verdict in it is the same
       word -- one rule for both protocols, which is why it is written about
       positions rather than vote counts: a v1 comparison whose random draw
       landed on one order three times is three views of one position, and
       `single_position` counts it (with the v2 comparisons whose other vote
       errored) rather than letting a rate be reported over a denominator
       nobody can see.

    A gate-decided comparison IS a win for `gate_decided_by`'s side in the win
    rates and in Elo -- the objective result settled it, which is a result about
    the arm. Rule 3 is about the vote census, which is a different question:
    what did the judge say when asked. Rule 5 is a third: how much of this
    number is the judge's opinion at all.

    EVERY RATE AND EVERY RATING CARRIES A 95% INTERVAL (§10.3: "any metric
    reported without a confidence interval is not reported"), taken as a
    cluster bootstrap over TASKS -- see `BOOTSTRAP_RESAMPLES` and
    `_cluster_bootstrap`. The clustering is the whole content: comparisons
    inside one task are correlated, so an interval that resamples comparisons
    independently is several times too narrow and fails by printing precision
    the collection does not have.

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
        #
        # The POSITION travels with the verdict, and is read only by the
        # consistency count below. It is deliberately not part of the sort key:
        # `position_assignment` is nullable, and `sorted` over a tuple mixing
        # `None` with a string raises `TypeError`, which would take down the
        # summary of a paid batch over a field the ordering does not use.
        votes_by.setdefault(key, []).append(
            (judgment.vote_index if judgment.vote_index is not None else -1,
             judgment.verdict, judgment.position_assignment)
        )

    superseded = 0
    # Rule 4: keyed on the generation FIRST. Both walks below stay inside one
    # oracle's verdicts, so neither a comparison count nor a rating fit can
    # cross from one generation into another's numbers. Every structure filled
    # in this walk is keyed that way, the four added ones included: a
    # consistency rate, a gate-decided share, a voted-only rate and an interval
    # each describe ONE oracle's reading of this collection.
    outcomes: dict[tuple, list[tuple[str, str, float]]] = {}
    # PARALLEL to `outcomes`, index for index: which task each triple came from
    # (the bootstrap's cluster) and which matrix cell it lands in. Kept beside
    # the triples rather than inside them -- see `_task_blocks`.
    clusters: dict[tuple, list[str]] = {}
    cells: dict[tuple, list[tuple[tuple[str, str], float]]] = {}
    voted_outcomes: dict[tuple, list[tuple[str, str, float]]] = {}
    voted_points: dict[tuple, dict[tuple[str, str], float]] = {}
    pairs: dict[tuple, dict[tuple[str, str], dict[str, int]]] = {}
    gate_share: dict[tuple, dict[str, dict[str, int]]] = {}
    consistency: dict[tuple, dict[str, int]] = {}

    for key in _deterministic(set(votes_by) | set(gate_by)):
        vote_lines = votes_by.get(key)
        gate = gate_by.get(key)
        if vote_lines:
            if gate is not None:
                superseded += 1
            # `majority` cannot see an empty sequence here: the bucket exists
            # only because a vote line landed in it.
            winner = majority([
                verdict for _, verdict, _ in
                sorted(vote_lines, key=lambda line: line[:2])
            ])
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
        outcome = (model_a, model_b, score_a)
        outcomes.setdefault(generation, []).append(outcome)
        clusters.setdefault(generation, []).append(_task_of(key))

        # The matrix is keyed on the two model names SORTED, so `(x, y)` and
        # `(y, x)` are one row -- the same reason `run_id_a`/`run_id_b` are
        # canonical on the record. Inside ONE generation's block: see rule 4.
        model_x, model_y = sorted((model_a, model_b))
        score_x = score_a if model_x == model_a else 1.0 - score_a
        pair = (model_x, model_y)
        cells.setdefault(generation, []).append((pair, score_x))

        # Rule 5, and the reason the decomposition is taken here rather than
        # subtracted from the totals later: a gate-decided comparison is a
        # TIER A result standing in a Tier B number. Kept apart, the two
        # channels can be read against each other; blended, a §10.1 pairwise
        # figure is part pass rate and nothing on the printout says how much.
        if settled == "voted":
            voted_outcomes.setdefault(generation, []).append(outcome)
            points = voted_points.setdefault(generation, {})
            points[pair] = points.get(pair, 0.0) + score_x
        share = gate_share.setdefault(generation, {})
        for model in (model_a, model_b):
            # PER ARM, not per block: an arm the ladder settled three
            # comparisons in four is carrying a Tier A pass rate into its Tier
            # B row, and a block-level count would average it together with an
            # arm the ladder never touched.
            arm = share.setdefault(
                model, {"comparisons": 0, "gate_decided": 0}
            )
            arm["comparisons"] += 1
            if settled == "gate_decided":
                arm["gate_decided"] += 1

        # Rule 6: §4.2.3's position-swap probe, one rule for both protocols.
        # MEASURABLE is about the POSITIONS a comparison's votes covered and
        # never about how many votes it has -- v1 drew its three positions at
        # random, so a v1 comparison probes the swap only when the draw
        # happened to split, and three views of one position agreeing says
        # nothing about position at all. A gate-decided comparison has no
        # votes and so no positions, and is counted in neither column.
        counts = consistency.setdefault(
            generation,
            {"measurable": 0, "consistent": 0, "single_position": 0},
        )
        if vote_lines:
            positions = {
                position for _, _, position in vote_lines
                if position in VOTE_POSITIONS
            }
            if len(positions) == len(VOTE_POSITIONS):
                counts["measurable"] += 1
                if len({verdict for _, verdict, _ in vote_lines}) == 1:
                    counts["consistent"] += 1
            elif positions:
                # One position only: a v2 comparison whose other vote errored,
                # or a v1 draw that never split. The verdict carries whatever
                # bias that order has and there is nothing to cancel it
                # against, which is invisible in every other number here -- it
                # resolves through `majority` and enters the matrix like any
                # other comparison. Counted so a consistency rate is read
                # beside how many verdicts it could not cover.
                counts["single_position"] += 1

        row = pairs.setdefault(generation, {}).setdefault(
            pair,
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

    # One generator per call, seeded -- see `BOOTSTRAP_SEED`. Generations are
    # walked in sorted order so the draws a block gets are a function of the
    # file and not of dict iteration.
    rng = random.Random(BOOTSTRAP_SEED)
    comparisons: dict[tuple, dict[tuple[str, str], dict]] = {}
    elo: dict[tuple, dict[str, float]] = {}
    elo_voted: dict[tuple, dict[str, float]] = {}
    elo_ci95: dict[tuple, dict[str, tuple[float, float]]] = {}
    for generation in sorted(pairs):
        # One table per generation, for rule 4's reason. Pooling two oracles
        # fits one set of strengths to two different opinions about the same
        # pair, printing a consensus neither of them gave.
        ratings = elo_from_outcomes(outcomes[generation])
        rate_samples, rating_samples = _cluster_bootstrap(
            _task_blocks(
                clusters[generation], outcomes[generation], cells[generation],
            ),
            rng,
        )
        elo[generation] = ratings
        elo_voted[generation] = elo_from_outcomes(
            voted_outcomes.get(generation, [])
        )
        elo_ci95[generation] = {
            model: _interval(rating_samples.get(model), rating)
            for model, rating in ratings.items()
        }

        voted_x = voted_points.get(generation, {})
        block: dict[tuple[str, str], dict] = {}
        for pair in sorted(pairs[generation]):
            row = pairs[generation][pair]
            total = row["voted"] + row["gate_decided"]
            # A tie is half a point to each, exactly as it is to Elo. The two
            # have to agree or the table and the matrix rank differently.
            win_rate_x = (row["wins_x"] + 0.5 * row["ties"]) / total
            # Written out field by field rather than spread from `row`: what a
            # printout row holds is an interface two printers and a dozen tests
            # read, and spreading an accumulator publishes whatever the walk
            # above happened to keep in it.
            block[pair] = {
                "voted": row["voted"],
                "gate_decided": row["gate_decided"],
                "wins_x": row["wins_x"],
                "wins_y": row["wins_y"],
                "ties": row["ties"],
                "comparisons": total,
                "win_rate_x": win_rate_x,
                "win_rate_y": (row["wins_y"] + 0.5 * row["ties"]) / total,
                # `None`, never `0.0`, on a row the judge was never asked
                # about: 0/0 raises out of the middle of a paid batch's
                # summary, and a `0.0` in its place is the judge's worst
                # possible verdict printed for a judge that never voted.
                "win_rate_x_voted": (
                    voted_x.get(pair, 0.0) / row["voted"]
                    if row["voted"] else None
                ),
                "win_rate_x_ci95": _interval(
                    rate_samples.get(pair), win_rate_x
                ),
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
        "elo": elo,
        "elo_voted": elo_voted,
        "elo_ci95": elo_ci95,
        "gate_decided_share": {
            generation: {model: arms[model] for model in sorted(arms)}
            for generation, arms in sorted(gate_share.items())
        },
        "position_consistency": {
            generation: {
                **counts,
                # `None` on a block with nothing to probe, for
                # `win_rate_x_voted`'s reason: a 0.0 here would read as a judge
                # that contradicted itself every time.
                "rate": (
                    counts["consistent"] / counts["measurable"]
                    if counts["measurable"] else None
                ),
            }
            for generation, counts in sorted(consistency.items())
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
        # In the `finally` WITH the caveat, and below it -- the position on
        # stdout is unchanged on the happy path, and on the unhappy one it is
        # the difference between an operator learning what the pass cost and
        # not. `_print_reading` prints collection-derived text (model names in
        # the matrix, the profile and the Elo table), so an ASCII-pinned stdout
        # or a summary shaped by another reader raises PARTWAY through it and
        # everything after the block would be skipped. The caveat is in a
        # `finally` for exactly that reason; spend has the same claim on it,
        # because the batch has already been paid for by the time anything here
        # runs. Its own output is ASCII and integers by construction, so it
        # cannot replace the propagating exception with one of its own.
        #
        # BELOW the caveat because it is a fact about the BATCH rather than one
        # of the numbers the caveat qualifies: above it would read as though κ
        # had something to say about a token count.
        _print_usage(result["judge_usage"])

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


def _print_usage(usage: dict[str, int] | None) -> None:
    """What this invocation spent, in tokens, and why that is not dollars.

    THERE IS NO DOLLAR FIGURE AND THERE DELIBERATELY IS NOT GOING TO BE. The
    judge models are absent from `costs.PRICE_BOOK` because mantle pricing is
    unpublished, so any number here would be invented -- and an invented cost
    on a printout beside real token counts is indistinguishable from a measured
    one. The sentence names `PRICE_BOOK` so a reader who wants the conversion
    knows exactly where the harness stopped and their own arithmetic starts.

    Printing NOTHING was the failure this replaces. A ~40-hour pass left its
    call count and token totals in no file and no variable that outlived the
    process, so "what did that cost" had no answer at all -- not an
    approximate one, none.

    `calls` is completion REQUESTS, which is more than the number of verdicts
    when a credential died mid-pass: the refresh count beside it is what
    explains the difference, and both are printed rather than reconciled here.

    `calls_without_usage` is printed only when it is nonzero, and it is the one
    line that changes what the totals MEAN: above zero, the token counts are an
    under-count rather than a measurement, and a reader has to be told that in
    the same breath. It counts a call whose usage block was absent OR only
    partly readable, because both leave the same hole in the total -- see
    `bakeoff.judge._add_usage`.
    """
    if usage is None:
        # A batch driven through an injected seam. Said out loud rather than
        # skipped: a silent absence reads as a batch that spent nothing.
        print(
            "\njudge usage was not counted: this batch ran on an injected "
            "completion seam rather than the live judge."
        )
        return

    refreshes = usage["auth_refreshes"]
    minted = (
        f" ({refreshes} credential refresh(es) mid-pass)" if refreshes else ""
    )
    print(f"\njudge spend: {usage['calls']} completion request(s){minted}")
    print(f"  prompt tokens      {usage['prompt_tokens']:>12,}")
    print(f"  completion tokens  {usage['completion_tokens']:>12,}")
    print(f"  total tokens       {usage['total_tokens']:>12,}")
    if usage["calls_without_usage"]:
        print(
            f"  {usage['calls_without_usage']} of them reported no usage "
            "block, or only part of one, so the token totals above are an "
            "under-count rather than a measurement."
        )
    print(
        "  Tokens and not dollars: the judge models are deliberately absent "
        "from costs.PRICE_BOOK because mantle pricing is unpublished, so a "
        "cost here would be a number nobody could reconstruct. Multiply by "
        "the rate you were quoted."
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


def _print_block_provenance(summary: dict, generation: tuple) -> None:
    """Where one block's verdicts came from: which positions, which channel.

    Both numbers qualify the rates printed directly above them, so they are
    printed INSIDE the block rather than gathered into a section of their own.
    A consistency rate and a gate-decided share collected at the bottom of the
    report would be read against whichever block the reader last looked at,
    and the whole point of the partition is that two generations disagree.

    §4.2.3's position-swap probe, said as a rate over the comparisons that can
    carry it. The `single_position` count beside it is the denominator's
    other half -- verdicts that rest on ONE order, so nothing in them cancels
    the judge's preference for whatever it was shown first -- and it is printed
    only when there are some, because a zero is the shape a complete v2 block
    has and a line saying so every time is a line nobody reads.
    """
    counts = summary["position_consistency"][generation]
    if counts["rate"] is None:
        print(
            "    position consistency: no comparison in this block carries "
            "both forced positions, so §4.2.3's swap probe has nothing to "
            "read here"
        )
    else:
        print(
            f"    position consistency: {counts['consistent']} of "
            f"{counts['measurable']} measurable comparison(s) "
            f"({counts['rate']:.1%}) gave the same verdict in both forced "
            "positions"
        )
    if counts["single_position"]:
        print(
            f"      {counts['single_position']} further comparison(s) rest on "
            "ONE position -- the other position's vote is not in the file, so "
            "the verdict carries that order's bias with nothing to cancel it"
        )
    print(
        "    gate-decided share -- how much of each arm's block the judge "
        "never saw:"
    )
    for model, arm in sorted(
        summary["gate_decided_share"][generation].items()
    ):
        print(
            f"      {model:24} {arm['gate_decided']:>4} of "
            f"{arm['comparisons']:>4} comparison(s)  "
            f"{arm['gate_decided'] / arm['comparisons']:.1%}"
        )


def _print_reading(result: dict) -> None:
    """Every number, in the report's order of authority.

    Every header prints whether or not it has rows under it. A `--no-rubric`
    pass showing an empty rubric section is honest; a section that vanishes
    when it is empty makes two passes over one collection print two different
    shapes, and a reader diffing them cannot tell an absent section from an
    absent feature.

    EVERY NUMBER HERE IS QUALIFIED WHERE IT IS PRINTED, never in a section of
    its own further down: the interval sits in the same line as the rate it
    belongs to, the judge-voted rate under the combined one it decomposes, and
    the consistency rate and gate-decided share inside the block they describe
    -- see `_print_block_provenance`. A qualifier a reader has to scroll to
    find is one they read against the wrong block, and this whole function is
    a printout whose blocks deliberately disagree.

    All of it runs inside `print_summary`'s `try`, which is what puts it above
    the κ caveat. Nothing that reports a number may be appended after that
    block: §4.3's caveat qualifies "every number above", so a number printed
    below it is one published without it.
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
    print(
        f"  [in brackets] 95% interval, cluster bootstrap over TASKS "
        f"({BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}, so two "
        "readings of one file print one interval). §4.4: comparisons inside "
        "one task are correlated, so resampling them independently would "
        "print a band several times too narrow -- precision this collection "
        "does not have. A collection on ONE task has no spread to report and "
        "its interval is the point estimate"
    )
    print(
        "  JUDGE-VOTED ONLY is the same rate with the gate-decided "
        "comparisons taken out. A gate-decided comparison is a Tier A result "
        "standing in a Tier B number, so a row with a large gate-decided "
        "share is partly Tier A's pass rate re-derived -- two arms this judge "
        "cannot separate can still sit either side of §10.1's 40% pairwise "
        "reference on their gate results alone. Read the two together"
    )
    for generation, block in sorted(summary["comparisons"].items()):
        # The generation is named above every block for `_rubric_profile`'s
        # reason. A block that does not name its oracle is one the reader
        # pools in their head, which is the same wrong number the partition
        # just kept out of the arithmetic.
        print(f"  {_generation_label(generation)}")
        for (model_x, model_y), row in sorted(block.items()):
            print(f"    {model_x} vs {model_y}")
            low, high = row["win_rate_x_ci95"]
            # y's interval is x's mirrored, and exactly so rather than
            # approximately: the two rates sum to 1 in every resample, so the
            # 2.5th percentile of one IS one minus the 97.5th of the other. It
            # is printed rather than left to the reader because a rate with no
            # band beside it reads as the one number on the row that was
            # measured precisely.
            print(
                f"      {row['wins_x']}-{row['wins_y']}-{row['ties']} "
                f"(W-L-T for {model_x}) over {row['comparisons']} "
                f"comparison(s): {model_x} {row['win_rate_x']:.1%} "
                f"[{low:.1%}, {high:.1%}] / "
                f"{model_y} {row['win_rate_y']:.1%} "
                f"[{1.0 - high:.1%}, {1.0 - low:.1%}]"
            )
            voted_rate = row["win_rate_x_voted"]
            if voted_rate is None:
                print(
                    f"      {row['voted']} judged, "
                    f"{row['gate_decided']} gate-decided -- judge-voted only: "
                    "NOTHING, every comparison in this row was settled by the "
                    "ladder, so the rate above is a Tier A result"
                )
            else:
                print(
                    f"      {row['voted']} judged, "
                    f"{row['gate_decided']} gate-decided; judge-voted only: "
                    f"{model_x} {voted_rate:.1%} / "
                    f"{model_y} {1.0 - voted_rate:.1%} over {row['voted']} "
                    "comparison(s)"
                )
        _print_block_provenance(summary, generation)

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
    print(
        "  The interval is the matrix's -- the same task resamples, refit -- "
        "and it is the number to read first: a gap narrower than the band is "
        "a gap this collection did not measure. JUDGE-VOTED ONLY refits over "
        "the judged comparisons alone; where it disagrees with the combined "
        "rating, the difference is the ladder's"
    )
    for generation, ratings in sorted(summary["elo"].items()):
        print(f"  {_generation_label(generation)}")
        intervals = summary["elo_ci95"][generation]
        voted = summary["elo_voted"][generation]
        for model, rating in sorted(
            ratings.items(), key=lambda item: (-item[1], item[0])
        ):
            low, high = intervals[model]
            voted_rating = voted.get(model)
            # An arm can be missing from the voted-only fit entirely -- every
            # comparison it played was gate-decided. Said with a dash rather
            # than a number, because any number here would be a rating for an
            # arm no judge ever voted on.
            said = (
                f"{voted_rating:8.1f}" if voted_rating is not None
                else "      --"
            )
            # Padded inside the brackets so the two ends of every band line up
            # under each other: an unpadded column makes a wide interval and a
            # narrow one the same length on the terminal, which is the one
            # comparison this table exists to make easy.
            print(
                f"    {model:24} {rating:8.1f}  [{low:7.1f}, {high:7.1f}]  "
                f"judge-voted only {said}"
            )

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
    # `CollectionNotFound` beside `ResumeRefused`: two refusals, one exit path.
    # Both mean the batch never ran, both print no numbers, and neither is
    # something a flag can talk past -- so the operator gets the sentence and
    # exit 1 rather than a traceback out of a driver they pointed at a typo.
    except (ResumeRefused, CollectionNotFound) as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    print_summary(result)
    if result["interrupted"]:
        # 130 = 128 + SIGINT, AFTER the summary. The shell distinction is the
        # point: 1 says the batch found problems and 130 says somebody stopped
        # it, and only one of the two is answered by running the same command
        # again. Ahead of the error check because an interrupt is the reason
        # the pass ended even when it had also recorded errors along the way.
        print(
            "\nINTERRUPTED: the pass stopped on Ctrl-C with units left "
            "unattempted. Nothing already judged is lost -- judgments are "
            "append-only and the resume is keyed on units already bought -- "
            "so resume with the same command."
        )
        return 130
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
