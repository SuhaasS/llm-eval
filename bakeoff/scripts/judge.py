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

And two refusals before any of it, in this order.

`NonNeutralJudge` is first, because it is a refusal about the COMMAND rather
than about anything on disk. §4.3 makes a neutral judge family mandatory -- the
compared families are Anthropic, Google, NVIDIA and Moonshot, a Claude judge
inflates Sonnet 5, and self-preference is measured rather than suspected -- and
a judge from a compared family is the one failure nothing below can detect: the
payloads are clean, the verdicts parse, the matrix fills in, the intervals are
honest about sampling error and silent about the bias, and every number moves
together. Placed second it could be masked by a typo in `--event-log`, which the
operator would fix before re-running into the same biased pass.
`BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE=1` admits one anyway, loudly and in the
result's `warnings` -- an environment variable and not a flag, for the reason
the κ caveat has no flag either.

`CollectionNotFound` is second. `EventLog.__init__` mkdirs `runs/`, so a
mistyped `--event-log` used to be CREATED, found empty, and reported as a clean
pass over zero units -- every number in it zero and none of them wrong. The
check still runs before that constructor can; the guard above it reads nothing
and creates nothing.

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
import hashlib
import itertools
import math
import random
import re
import sys
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

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
from bakeoff.codex_judge import (  # noqa: E402
    # Stdlib-only and it imports `bakeoff.judge`, which is imported below
    # anyway -- so a mantle pass pays nothing for this edge. What is deferred
    # is the RESOLUTION (binary, judge home, auth.json), which happens on the
    # first prompt inside `lazy_codex_completion`.
    CODEX_MODEL_PREFIX,
    codex_harness,
    codex_sampling,
    is_codex_judge,
)
from bakeoff.judge import (  # noqa: E402
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    RUBRIC_VERSION,
    VOTE_POSITIONS,
    CompleteFn,
    NonNeutralJudge,
    PayloadInputs,
    assert_neutral_judge,
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
#: measured at ~0.6s for a full 60-task, four-arm collection (3,600
#: comparisons), at the end of a pass that took hours. A SEED rather than fresh
#: entropy, and a fresh generator per BLOCK rather than module state or one
#: generator for the whole summary: two readings of one unchanged file must
#: print one interval, or the band appears to move while nothing about the
#: collection did -- and it moves in the last digit, where it looks like a
#: change in the numbers rather than like noise the summary invented. One
#: generator shared across generations is the same failure one level up:
#: appending a single v1 line consumes draws before the v2 block is resampled,
#: which moved a measured v2 band by 2.8 points with the point estimate
#: unchanged. `_bootstrap_seed` derives each block's seed from its generation.
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 0

#: The two-sided 95% normal quantile, for the boundary case in
#: `_proportion_interval` -- see `_wilson_interval`. Written out rather than
#: computed because the printed band is part of what the number means and a
#: constant a reader can grep for is one they can check.
_Z_95 = 1.959963984540054

#: Below two resamples there is no distribution to take a percentile of. One
#: sample would return itself at both ends, which prints a zero-width band off
#: a single draw -- the sharpest version of the failure `_proportion_interval`
#: is placed against.
_MIN_RESAMPLES = 2

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

    The four version fields are the whole gate. A verdict is only reproducible
    against the model that gave it, the prompt text it saw, the rubric it was
    scored under and -- on the codex backend -- the reasoning effort it was
    sampled at, so a change to any of the four makes a NEW generation of
    verdict rather than a resume, and the disagreement between the two lines
    is the finding the file exists to keep. `None` is itself one of the effort's
    values, derived identically on both sides of the key, which is what keeps
    every mantle line and every codex line judged before the flag existed
    resuming exactly as it did.

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
        # The one operator-variable sampling knob. `None` on every mantle
        # line (JUDGE_SAMPLING carries no such key), every gate-decided line
        # (judge_sampling is {}), and every codex line judged without the
        # flag -- so every line that exists today keys exactly as it did.
        # Absent from the key, a resume that changed the flag would skip
        # vote 0 bought at one effort and buy vote 1 at another, and
        # `majority` would combine two different judges as one.
        #
        # `or {}` because a hand-edited `judge_sampling: null` reaches here
        # intact -- `JudgeRecord` is a bare frozen dataclass, so the field is
        # present and `load_judgments` never sees the `TypeError` that would
        # make the line malformed. A bare `.get` then raises `AttributeError`
        # out of `summarize`, which runs inside `judge_event_log`'s RETURN,
        # losing the reading of a batch whose every call is already bought.
        (judgment.judge_sampling or {}).get("model_reasoning_effort"),
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


def _rubric_key(run_id: str, judge_model_id: str,
                effort: str | None) -> tuple:
    return ("rubric", run_id, judge_model_id, JUDGE_PROMPT_VERSION,
            RUBRIC_VERSION, effort)


def _pairwise_key(task_id: str, sample_index: int, run_id_a: str,
                  run_id_b: str, vote_index: int | None,
                  judge_model_id: str, effort: str | None) -> tuple:
    return ("pairwise", task_id, sample_index, run_id_a, run_id_b, vote_index,
            judge_model_id, JUDGE_PROMPT_VERSION, RUBRIC_VERSION, effort)


# ---------------------------------------------------------------------------
# the seams
# ---------------------------------------------------------------------------


def lazy_live_completion(judge_model_id: str,
                         usage_totals: dict[str, int] | None = None,
                         stop: threading.Event | None = None,
                         ) -> CompleteFn:
    """`live_completion`, built on the first prompt that needs it.

    The `task_resolver` pattern (`scripts/grade.py:317`) one layer further out.
    `live_completion` already defers its ROUTER; this defers `live_completion`
    itself, which is what a fully gate-decided batch needs: that function
    resolves `scripts.smoke_bedrock` at construction, and a batch that makes no
    call should not require the credential module to be importable at all.

    A one-slot dict rather than `nonlocal`, so there is no rebind to get
    wrong, and a lock around the fill because `--concurrency` submits the
    first prompts of a batch together.

    THIS LOCK GUARDS ONLY THIS CACHE. `live_completion`'s router is built on
    the first INVOCATION, not at construction, so it is reached outside this
    lock entirely and carries its own -- an earlier version of this sentence
    claimed guarding here guarded both, which was false in the direction that
    matters: two threads through one closure.

    `usage_totals` is passed THROUGH rather than counted here, and the laziness
    is why that matters: this wrapper sees a prompt and a reply, not a response,
    so counting at this layer would report a call count and no tokens at all.
    It is also the caller's dict rather than one made here, so a batch that
    never builds the live judge -- fully gate-decided, or fully skipped --
    still hands the driver a set of zeros to print rather than a `None` that
    would read as "not counted".

    `stop` is passed THROUGH for the same reason `usage_totals` is: it belongs
    to the batch, not to this wrapper. The batch's event has to reach the
    closure that makes the calls, or an aborted concurrent pass keeps buying --
    and on this backend a stopped worker that hits a 401 also mints a fresh
    credential, which is spend the summary has already been printed without.
    """
    built: dict[str, CompleteFn] = {}
    # A LOCK, not a bare check-then-set. Under `--concurrency` the first
    # prompts of a batch are submitted together, so two workers race here --
    # and on the mantle side losing that race means minting a second
    # credential for a router that is thrown away, which is a real cost
    # against a token whose window is about an hour.
    lock = threading.Lock()

    def complete(prompt: str) -> str:
        with lock:
            if "fn" not in built:
                built["fn"] = live_completion(
                    judge_model_id, usage_totals=usage_totals, stop=stop
                )
        return built["fn"](prompt)

    return complete


def lazy_codex_completion(judge_model_id: str,
                          usage_totals: dict[str, int] | None = None,
                          reasoning_effort: str | None = None,
                          stop: threading.Event | None = None,
                          ) -> CompleteFn:
    """`codex_completion`, built on the first prompt that needs it.

    The twin of `lazy_live_completion`, and lazy for the same reason one layer
    further out than that function's own laziness: `codex_completion` refuses
    at its first call when there is no binary, no `BAKEOFF_CODEX_HOME` or no
    `auth.json` in it, and a batch every comparison of which the deterministic
    ladder already decided must not require any of the three to exist. The
    module itself is stdlib-only and is imported at the top of this file --
    what is deferred here is the RESOLUTION and the closure it builds, not the
    import.

    Locked for `lazy_live_completion`'s reason: `--concurrency` submits the
    first prompts of a batch together. As there, this guards only this cache;
    `codex_completion` resolves the binary, the home and the auth file on its
    first invocation and holds its own lock for that.
    """
    built: dict[str, CompleteFn] = {}
    # A LOCK, not a bare check-then-set. Under `--concurrency` the first
    # prompts of a batch are submitted together, so two workers race here --
    # and on the mantle side losing that race means minting a second
    # credential for a router that is thrown away, which is a real cost
    # against a token whose window is about an hour.
    lock = threading.Lock()

    def complete(prompt: str) -> str:
        with lock:
            if "fn" not in built:
                from bakeoff.codex_judge import codex_completion

                built["fn"] = codex_completion(
                    judge_model_id,
                    usage_totals=usage_totals,
                    # PASSED, not defaulted. `judge_facts` puts this same
                    # value into every line's `judge_sampling`, so a closure
                    # built without it would run at the model's default
                    # effort while every record claimed the effort was sent
                    # -- a false statement in the one field whose whole job
                    # is to be true about the request, and one nothing
                    # downstream could detect because the value parses.
                    reasoning_effort=reasoning_effort,
                    # The batch's own event, so a second batch in the same
                    # process builds a second closure holding a second event
                    # -- nothing to re-arm, and nothing of this batch's left
                    # to revive. See `codex_judge`'s note above its constants.
                    stop=stop,
                )
        return built["fn"](prompt)

    return complete


def _judge_backend(judge_model_id: str) -> str:
    """Which backend this id selects: `"codex"` or `"mantle"`.

    INFERRED FROM THE ID, and there is deliberately no `--judge-backend` flag
    to disagree with it. `judge_model_id` is already the identity every resume
    key and every generation partition reads first (`_rubric_key`,
    `_pairwise_key`, `_judge_generation`), so a `codex:` id cannot collide with
    a mantle id and the two backends can never be pooled into one number by
    accident. A flag beside the id could be set to one backend while the id
    named the other, and the resulting file would carry lines whose recorded
    identity is not the thing that answered them -- a mislabelling nothing
    downstream could detect, since every field would parse.
    """
    return "codex" if is_codex_judge(judge_model_id) else "mantle"


def judge_facts(judge_model_id: str, reasoning_effort: str | None = None
                ) -> tuple[dict, Callable[[], dict | None]]:
    """`(judge_sampling, harness_of)` for this backend. See D7.

    Returns the sampling block by VALUE and the harness block behind a
    MEMOISED CALLABLE, and the asymmetry is the point. Sampling is knowable
    from the arguments alone; the harness is not -- `codex_harness` reads the
    CLI's version off the binary and the auth mode out of the judge home, so
    building it eagerly would make a fully gate-decided batch refuse for want
    of a credential it is never going to use. Deferred, it is built by the
    first PAID line and by nothing else, which is the same rule
    `lazy_codex_completion` follows one layer down.

    The mantle side is a value in a callable's clothing on purpose: one shape
    for both backends means the two line builders have one code path, and a
    builder with an `if backend ==` in it is where the two records drift.

    The sampling block is what the record will claim was sent. On the codex
    side that is the reasoning effort or nothing at all -- never
    `temperature`, which `codex exec` cannot send and which would therefore be
    a false statement in the one field whose entire job is to be true about
    the request. See `codex_judge.codex_sampling`.
    """
    if not is_codex_judge(judge_model_id):
        mantle = {"backend": "litellm-mantle"}
        return dict(JUDGE_SAMPLING), lambda: dict(mantle)

    built: dict[str, dict] = {}

    def harness_of() -> dict | None:
        if "harness" not in built:
            built["harness"] = codex_harness(
                judge_model_id, reasoning_effort=reasoning_effort
            )
        return dict(built["harness"])

    return codex_sampling(reasoning_effort), harness_of


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


def _print_ascii_safe(line: str) -> None:
    """`print`, surviving a stdout the environment pinned to ASCII.

    ONE implementation for every site in this file whose text `str.encode` can
    refuse, which is these and no others:

    * `main`'s pre-summary header and its task-set load failure -- the two
      paths the operator passed (`--event-log`, `--taskset`), the two derived
      from the first, and the `TaskError` message, which opens with the
      task-set root it could not read.
    * The non-neutral judge warning, which carries a `§`.
    * The per-unit progress line, collection-derived: a task id, model names,
      run ids.
    * `print_summary`'s WARNING loop -- run ids in the excluded and ungraded
      warnings, task ids and model names in the abort, and the neutral-judge
      warning re-emitted.
    * The refusal path in `main`, whose `NonNeutralJudge` message carries a `§`.

    On an ASCII stdout -- `LC_ALL=C`, or a pipe into a tool that pinned it --
    `print` raises `UnicodeEncodeError`, which is not an `Exception` any
    per-unit handler catches: it goes past the breaker, past
    `except KeyboardInterrupt` and out of `main`, taking a batch that had been
    running for hours down on a traceback with no summary and no usage totals.
    The header is the same failure one step earlier and one step cheaper --
    before the collection is read, before a credential is minted -- which is
    worth surviving for the path it prints rather than for the spend it saves.

    THE CENSUS IS NOT ON THIS LIST, and the docstring used to say it was.
    `_census_line` is a format string over five integers, so it is ASCII by
    construction and its bare `print` cannot raise. Listing it here read as
    coverage this helper does not provide and hid the header, which needed it.
    A census line that ever grows collection-derived text becomes a site.

    `_print_reading` is NOT among them and no longer needs to be: `main`
    reconfigures the stdout STREAM to `errors="backslashreplace"` before the
    report, which covers its two `§` legends and every other report print,
    current and future, without a guard at each site. That is what closed T5's
    recorded OPEN, and the comment at the reconfigure carries the reasoning.

    What the reconfigure does NOT touch is what this helper is still the cover
    for: everything printed BEFORE that line on the CLI path (the header, the
    per-unit progress lines, the non-neutral banner), and a library caller of
    `print_summary`, whose stream is theirs and stays as they set it.

    `backslashreplace` rather than a dropped line: an id an operator has to
    grep for is worth more mangled than absent, and the escape is reversible.
    The fallback is pure ASCII by construction, so it cannot raise the same
    error a second time.

    `_print_kappa_caveat` deliberately does NOT use this, and the difference is
    what each line is FOR. An id is grepped, so mangling it into escapes keeps
    it usable; the caveat is a claim a reader has to understand, and a sentence
    delivered in escape sequences is one they skip. So that line keeps its
    hand-written ASCII twin, which says the same thing in letters every
    terminal has.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"))


def _encodable(text: str) -> bool:
    """Whether stdout can render `text` as itself, asked rather than attempted.

    `main` reconfigures stdout to `errors="backslashreplace"` before the
    summary, which means a `print` of un-renderable text no longer raises -- it
    succeeds, silently, in escapes. Every fallback in this file keyed on
    `except UnicodeEncodeError` therefore goes unreached under that policy, and
    for `_print_kappa_caveat` that is a real loss: its hand-written ASCII twin
    is a SENTENCE, and a sentence delivered as `\\u03ba ... \\u2014` is one a
    reader skips. Asking first is what keeps the choice deliberate.

    A stream with no `encoding` (a `StringIO`, a test double) is treated as
    able to render anything, which is true of every such stream here.
    `LookupError` is caught beside the encode error because a stream can carry
    an encoding name Python has no codec for, and a summary must not die
    deciding how to print a caveat.
    """
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# ---------------------------------------------------------------------------
# the three kinds of line
# ---------------------------------------------------------------------------


def _rubric_line(record, grade: GradeRecord, task, inputs: PayloadInputs,
                 payloads: Path, path: Path, complete: CompleteFn,
                 judge_model_id: str, judge_sampling: dict,
                 harness_of: Callable[[], dict | None],
                 precomputed: Callable[[], Any] | None = None) -> JudgeRecord:
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
    # THE COMPUTE/COMMIT SEAM. `precomputed` is a zero-argument callable --
    # in practice `Future.result` -- so a call already made on a worker thread
    # arrives here as a value, and a worker that RAISED re-raises from the
    # same line the inline call would have raised from. Passing the future's
    # method rather than its value is what buys that: a caller that unwrapped
    # the result itself would have to re-raise by hand, and an exception
    # re-raised outside this frame is one the per-unit handler does not catch.
    payload, rendered, result = (
        precomputed() if precomputed is not None
        else judge_rubric(inputs, complete)
    )
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
        # THREADED, not read off the constant. `JUDGE_SAMPLING` describes the
        # mantle request and nothing else; a codex line that copied it in
        # would claim a `temperature: 0.0` no codex call can send.
        judge_sampling=dict(judge_sampling),
        judge_harness=harness_of(),
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
               complete: CompleteFn, judge_model_id: str,
               judge_sampling: dict,
               harness_of: Callable[[], dict | None],
               precomputed: Callable[[], Any] | None = None) -> JudgeRecord:
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
    # See `_rubric_line`: the same compute/commit seam, and the position is
    # applied in the WORKER so the payload that was built is the payload that
    # was sent, exactly as it is when the call happens inline.
    outcome = (
        precomputed() if precomputed is not None
        else judge_pair_vote(inputs_a, inputs_b, position_assignment, complete)
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
        # See `_rubric_line`: as sent on THIS backend, never the constant.
        judge_sampling=dict(judge_sampling),
        judge_harness=harness_of(),
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
    * `judge_prompt_sha` is `""` because no prompt was rendered,
      `judge_sampling` is `{}` because nothing was sent, and `judge_harness`
      is `None` because nothing carried it. All three are as-sent evidence
      fields; a sha, a sampling block or a harness block copied in from the
      batch would attest to a request that was never made -- and on the codex
      backend building the harness would additionally resolve a credential
      for a line that needs none.
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


def _abort_message(failure_run: list[tuple[str, bool]],
                   backend: str = "mantle") -> str:
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

    THAT NARROWNESS IS ALSO WHY THE SECOND PARAGRAPH NAMES TWO SHAPES rather
    than promising determinism. Non-auth is not the same as data-shaped: a
    rate limit or an exhausted quota answers with a 429, so it lands there
    too, and on a pass of thousands of calls it is the likelier of the two --
    `Router(num_retries=2)` has already spent the transport retries by the
    time a unit reaches the breaker at all. Telling that operator their units
    fail deterministically sends them to exclude a task that is fine, when the
    fix is to wait for the window and run the same command again.

    A MIXED run gets BOTH paragraphs rather than a majority verdict. Mixed is
    real -- a credential dying in the middle of a task whose diffs are also
    unreadable -- and the honest report is that both were seen, in the counts
    they were seen in.

    `backend` CHOOSES THE FIX, not the diagnosis. `is_auth_failure` still
    decides which paragraph prints -- that stays one classifier answering for
    both backends, which is why `CodexAuthFailure` carries a `status_code` --
    but the two backends are answered by opposite actions and a paragraph that
    named the wrong one would be worse than a vague one. A mantle 401 is
    answered by a mint the harness already attempted; a codex 401 cannot be
    answered by any process at all, because `codex login` is a browser flow a
    person has to complete. Telling a codex operator to check their `aws sso`
    session sends them to a credential this pass never used.

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
    if auth and backend == "codex":
        parts.append(
            f"{len(auth)} of them failed authentication (HTTP 401/403). "
            "NOTHING HERE CAN FIX THAT and nothing tried to: unlike the "
            "mantle bearer there is no credential to mint, so the call was "
            "not retried. Log the judge home in again -- "
            "`CODEX_HOME=$BAKEOFF_CODEX_HOME <codex> login`, then confirm "
            "with `... login status` -- and check that BAKEOFF_CODEX_HOME "
            "still points at the attested seat's home rather than a personal "
            "one. Nothing is lost: judgments are append-only and the resume "
            "is keyed on units already bought, so resume with the same "
            "command once the login works."
        )
    elif auth:
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
            "token would change nothing, and the errors below name each one. "
            "Two shapes land here and they take different fixes. Rate-limit "
            "and quota errors are transient -- throttling answers with a 429, "
            "not a 401, and the transport retries behind each call have "
            "already lost -- so wait for the window to clear and resume with "
            + ("the same command. On the codex backend a 429 reaching this "
               "point means the backend's OWN backoff was exhausted first, so "
               "the window is a long one: wait longer than you would for a "
               "transport blip. " if backend == "codex" else "the same "
               "command. ")
            + "Data-shaped errors (malformed verdicts, "
            "secret-bearing payloads, unjudgeable runs) will fail again on "
            "resume, in the same order and in the same place, until something "
            "about the collection or the command changes. Judge past them by "
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
                    reasoning_effort: str | None = None,
                    concurrency: int = 1,
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

    ONE refusal is taken between the two phases and files an error for units
    it never selects: a task whose gating grades name a manifest other than the
    one loaded here (`_digest_check`). Its units are not "selected and
    unjudged" -- they are units this pass refuses to select at all -- and the
    error line is what keeps that refusal from reading as a smaller collection.

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

    Raises `NonNeutralJudge` on the argument alone, `ValueError` on the three
    argument shapes that would otherwise cost a pass before anything downstream
    could report them (a mixed-case `codex:` prefix, an effort with a mantle
    backend, an effort that is not lowercase letters), `CollectionNotFound`
    before touching anything, `ResumeRefused` if either input file cannot be
    read completely, and nothing else: a batch that got as far as the walk
    returns its partial reading whatever happens in it.
    """
    # FIRST, ahead of even the collection check, and the order is the content.
    # This is a refusal about the COMMAND rather than about what is on disk:
    # §4.3 makes a neutral judge family mandatory, and a judge from a compared
    # family is the one failure nothing downstream can see -- the payloads are
    # clean, the verdicts parse, the matrix fills in and every number in it
    # shifts together. Placed after the collection check, a typo in
    # `--event-log` would answer with the wrong refusal, and the operator would
    # fix the path and re-run straight into the biased pass. It reads nothing,
    # creates nothing and mints nothing, so `CollectionNotFound`'s own
    # "before `EventLog(...)` mkdirs runs/" constraint is untouched.
    #
    # It also sits ahead of the resume: `done` cannot skip a unit under a judge
    # this function would refuse, which is what keeps a re-judge from
    # inheriting the first pass's model by accident.
    non_neutral = assert_neutral_judge(judge_model_id)

    # Immediately after the guard and before the collection is read: the
    # backend is a property of the id the guard just admitted, and both facts
    # below are needed by the first line this pass writes.
    backend = _judge_backend(judge_model_id)
    # THE LIBRARY HALF of the two refusals `main` makes at the usage line, and
    # it is not redundant with them: everything below runs for any caller that
    # imports this function, and both shapes below are silently dropped rather
    # than reported. A refusal here costs a traceback; the alternative costs a
    # collection of lines that parse and say nothing about what was ignored.
    if judge_model_id.lower().startswith(CODEX_MODEL_PREFIX) \
            and backend != "codex":
        raise ValueError(
            f"{judge_model_id!r} spells the codex namespace with the wrong "
            "case: the prefix is spelled 'codex:' exactly, and a normalised "
            "id would put two spellings of one judge in one file"
        )
    if reasoning_effort is not None and backend != "codex":
        raise ValueError(
            "reasoning_effort is a codex-backend knob; the mantle path "
            "cannot send it and must not record it"
        )
    # THE SAME SHAPE `codex_judge.build_codex_argv` refuses, asserted HERE --
    # a batch away from the argv it corrupts, and before anything is read or
    # spent. That guard fires per CALL, and by then this batch has already
    # walked the collection, mounted the gate and committed a line for every
    # comparison the deterministic ladder decided; each paid unit after that
    # dies identically inside `build_codex_argv` -- the value is interpolated
    # into a `model_reasoning_effort="..."` TOML override, so a quote, a
    # backslash or a newline invalidates the config of EVERY call rather than
    # of one -- until `max_consecutive_errors` aborts a pass whose only
    # product is error lines. The CLI's `choices=` refuses it at the usage
    # line; this is the batch-level guard for a direct library caller, where
    # argparse enforces nothing.
    #
    # Truthiness, exactly as `build_codex_argv` gates: `""` is the absent
    # effort here (see `batch_effort` below), and it never reaches an argv.
    if reasoning_effort and not re.fullmatch(r"[a-z]+", reasoning_effort):
        raise ValueError(
            f"unusable reasoning effort {reasoning_effort!r}: the value is "
            "interpolated into a TOML -c override, so anything beyond "
            "lowercase letters would corrupt every call of this batch rather "
            "than fail one"
        )
    # The batch side of the sampling identity in every resume key. `None` on
    # the mantle backend whatever the argument says, because the mantle path
    # sends no effort -- the key must describe what goes out, not what was
    # typed.
    #
    # `or None` for the same reason one clause further in: `""` passes the
    # `is not None` guard above, and every consumer of it -- `codex_sampling`,
    # `build_codex_argv`, `codex_harness` -- gates on TRUTHINESS and drops it.
    # The lines this batch writes therefore carry `judge_sampling: {}`, off
    # which `_judge_generation` reads `None`; a key holding `""` would mismatch
    # every one of them and re-buy the whole collection on the next resume,
    # reporting a clean pass over work already paid for.
    batch_effort = (reasoning_effort or None) if backend == "codex" else None
    # `batch_effort`, never the raw argument, from here down. The `or None`
    # above is the ONE place this batch decides what "no effort" spells, and
    # every consumer reading the argument instead would be a second copy of
    # that decision -- three of them today, each gating on truthiness, so the
    # copies agree until one stops.
    judge_sampling, harness_of = judge_facts(judge_model_id, batch_effort)

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

    if non_neutral is not None:
        # BOTH destinations, because they answer two readers. The terminal line
        # reaches the operator while the pass is still cheap to stop -- above
        # the census, before a credential is minted -- and the `warnings` entry
        # reaches whoever reads the numbers afterwards, attached to the result
        # that carries them. Either one alone leaves a reading that looks
        # neutral to somebody.
        warnings.append(non_neutral)
        _print_ascii_safe(f"\n{non_neutral}")

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

    # The two selection filters, read HERE rather than at the worklist walk,
    # because the read loop below is the first thing that can be narrowed by
    # them. `wanted_samples` cannot narrow THAT -- see the boundary comment --
    # but it narrows the digest check at step 5b, which is the other place a
    # unit nobody selected could otherwise decide this pass's exit code.
    wanted_tasks = None if only_tasks is None else set(only_tasks)
    wanted_samples = None if sample_indices is None else set(sample_indices)

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
        # THE SELECTION BOUNDARY, and the exit contract is what draws it.
        # "Exit 0 means every selected unit produced its lines" -- so a read
        # failure counts only for a run this pass would have judged.
        # Unfiltered, ONE damaged record made every future pass over this
        # collection exit 1, the passes that select another task included:
        # they never wanted the record, no flag routes around it, and the
        # error line they carry is about a unit nobody selected. Unselected
        # and unreadable is not this pass's problem; SELECTED and unreadable
        # is, and stays an error below.
        #
        # `task_id` is on the grade line, which is what makes this filter
        # possible before the read. `sample_index` is NOT (`GradeRecord` has
        # no such field -- see the error line below, which cannot name one),
        # so `--samples` cannot narrow the read and keeps filtering at the
        # worklist walk. A run selected by task and dropped by sample is
        # therefore still read: that is a cost, not a wrong exit code.
        if wanted_tasks is not None and grade.task_id not in wanted_tasks:
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

    # Step 5b: is every gating grade anchored on the manifest this pass loaded?
    # ONE VERDICT PER TASK, taken here rather than inside the worklist walk,
    # because the refusal covers every unit of the task and a task spreads over
    # as many cells as it has samples -- a check made per cell would refuse the
    # cells it had reached and judge the rest. See `_digest_check`.
    #
    # THE SAME SELECTION BOUNDARY the read loop draws, and for the same reason:
    # over every sample of the task, this check re-opened the hole the filter
    # above had just closed. A collection whose sample-1 grades straddle a
    # `task.yaml` edit and whose sample-0 grades do not would refuse a coherent
    # `--samples 0` pass and exit 1 -- grade lines gating units no filter
    # selected deciding this pass's exit code. So the grades compared here are
    # the ones gating SELECTED cells only. `wanted_tasks` needs no term: a task
    # outside it has no record read and therefore no cell. No coverage is lost
    # -- an unselected unit gates nothing this pass, and the pass that selects
    # it takes the refusal then.
    refused: set[str] = set()
    for task_id in sorted({task_id for task_id, _ in cells}):
        task = by_task.get(task_id)
        if task is None:
            # Absent from the task set: warned once by the walk below, and
            # there is no manifest here to compare a digest against.
            continue
        graded_runs = sorted(
            record.run_id
            for (cell_task, sample_index), cell in cells.items()
            if cell_task == task_id
            and (wanted_samples is None or sample_index in wanted_samples)
            for record in cell.values()
        )
        refusal, unknown = _digest_check(
            task, [(run_id, gating[run_id]) for run_id in graded_runs]
        )
        if unknown is not None:
            warnings.append(unknown)
        if refusal is not None:
            errors.append(refusal)
            refused.add(task_id)

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
    # ONE PER BATCH, owned here and handed to BOTH backends at construction.
    # `_walk_concurrently` sets it once it has stopped committing, so a worker
    # still inside the rate-limit ladder does not wake up and buy a call whose
    # verdict nobody will record -- money spent after the pass has already
    # reported what it spent. Never cleared, because it never outlives the
    # closure it was built into.
    #
    # BOTH, because for a while only the codex arm received it while this
    # comment already claimed otherwise: a stopped mantle worker went on making
    # its call, and on a 401 minted a fresh credential to retry it with, which
    # is spend of a second kind after the same summary.
    stop_spending = threading.Event()

    judge_usage: dict[str, int] | None = None
    if complete is None:
        judge_usage = new_usage_totals()
        # SELECTED BY THE ID, never by a flag -- see `_judge_backend`. Both
        # sides are lazy in the same way and for the same reason: a batch the
        # ladder decides entirely must not require either backend's credential
        # to exist.
        complete = (
            lazy_codex_completion(
                judge_model_id, judge_usage, batch_effort, stop_spending
            )
            if backend == "codex"
            else lazy_live_completion(judge_model_id, judge_usage,
                                      stop_spending)
        )

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
    # printed. Every outcome a unit can have ends in a `_finish` call: five of
    # the six go through an outcome closure below (`_unit_succeeded`,
    # `_unit_failed`, `_unit_failed_fatally`, `_gate_unit_failed`,
    # `_unit_not_judged`), and the sixth -- a gate-decided pair that WROTE its
    # line -- calls `_finish("ok")` from `_attempt` directly, because it must
    # not reset the breaker and so cannot use `_unit_succeeded`. That one
    # deliberate exception is why the rule is "exactly one `_finish` per
    # attempted unit" rather than "every path goes through a closure".
    progress: dict[str, Any] = {
        "index": 0, "label": "", "start": 0.0, "attempted": 0,
        "total": 0, "batch_started": 0.0, "unit_seconds": None,
    }

    def _begin(index: int, unit: _Unit,
               unit_seconds: float | None = None) -> None:
        """Open one unit's progress line. `unit_seconds` is the WORKER's clock.

        Under `--concurrency` the paid call happens on a worker thread and
        this frame only commits its result, so a duration measured here would
        be the commit's -- near zero for every unit, and near the whole wait
        for one unlucky unit that happened to sit at the head while the window
        filled. Neither number is what the operator is dividing by the index
        to estimate the rest of a forty-hour batch. `None` means the call was
        made inline and this frame's own clock is the right one.
        """
        progress.update(
            index=index, label=unit.label, start=time.monotonic(),
            unit_seconds=unit_seconds,
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
        to remove it. `_print_ascii_safe` is that guard, and it is shared with
        every other line in this file that can carry text an ASCII terminal
        refuses -- that helper's docstring enumerates them.
        """
        now = time.monotonic()
        progress["attempted"] += 1
        unit_seconds = progress["unit_seconds"]
        if unit_seconds is None:
            unit_seconds = now - progress["start"]
        _print_ascii_safe(
            f"[{progress['index']}/{progress['total']}] {progress['label']} "
            f"{status} (unit {_seconds(unit_seconds)}, "
            f"elapsed {_elapsed(now - progress['batch_started'])})"
        )

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
            raise _BatchAborted(_abort_message(failure_run, backend))

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
        if task_id in refused:
            # The manifest this pass loaded is not the one the gate gated. The
            # refusal is already in `errors` -- one line for the task rather
            # than one per unit, since every unit of it fails for one reason
            # and the fix is the same for all of them.
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
                key = _rubric_key(record.run_id, judge_model_id, batch_effort)
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
                # effort None: nothing is sent for a gate-decided pair, so
                # effort is not part of its identity and a flag change must not
                # re-append it.
                key = _pairwise_key(task_id, sample_index, run_id_a,
                                    run_id_b, None, judge_model_id, None)
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
                key = _pairwise_key(task_id, sample_index, run_id_a, run_id_b,
                                    vote_index, judge_model_id, batch_effort)
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

    def _attempt(unit: _Unit,
                 precomputed: Callable[[], Any] | None = None) -> None:
        """Execute one selected unit, and account for however it ends.

        Every path out of this ends in exactly one `_finish` call, which is
        what makes the one-line-per-attempted-unit rule structural. Five of the
        six reach it through an outcome closure; the sixth -- a gate-decided
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
                    judge_sampling, harness_of, precomputed,
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
                judge_sampling, harness_of, precomputed,
            ))
        except StorageFailure as exc:
            _unit_failed_fatally(exc)
        except Exception as exc:  # noqa: BLE001 - one unit
            _unit_failed(exc)
        else:
            _unit_succeeded()

    def _paid_call(unit: _Unit) -> Callable[[], Any] | None:
        """The worker half of one unit: the paid call and NOTHING else.

        Returns a zero-argument callable to run on a worker, or `None` for a
        unit that makes no call -- a gate-decided pair, or one whose runs
        cannot be turned into submissions. Those are committed inline at the
        head of the queue, where they cost nothing and cannot reorder
        anything.

        `_judgeable_inputs` is called HERE, on the main thread, and its two
        caches (`inputs`, `unjudgeable`) are therefore never touched
        concurrently. `_attempt` calls it again at commit time and gets the
        same memoised answer, so a run that could not be judged is diagnosed
        once and reported once, exactly as in the sequential walk.
        """
        if unit.kind == "gate-decided":
            return None
        if unit.kind == "rubric":
            (run_id,) = unit.run_ids
            ready = _judgeable_inputs(run_id, gating[run_id], unit.task)
            if ready is None:
                return None
            return lambda: judge_rubric(ready, complete)

        run_id_a, run_id_b = unit.run_ids
        ready_a = _judgeable_inputs(run_id_a, gating[run_id_a], unit.task)
        ready_b = _judgeable_inputs(run_id_b, gating[run_id_b], unit.task)
        if ready_a is None or ready_b is None:
            return None
        return lambda: judge_pair_vote(
            ready_a, ready_b, unit.position, complete
        )

    def _timed(clock: dict, call: Callable[[], Any]) -> Any:
        """Run one paid call and leave its duration where `_begin` can read it.

        `finally`, so a call that RAISED still reports how long it took: the
        progress line for a failed unit is where an operator sees a timeout
        as a timeout rather than as an unexplained error.
        """
        started = time.monotonic()
        try:
            return call()
        finally:
            clock["seconds"] = time.monotonic() - started

    def _walk_sequentially() -> None:
        """The original walk, untouched, and it is what `--concurrency 1` runs.

        Kept as its own path rather than expressed as a window of size one,
        because the two are NOT the same thing: a window submits the next
        unit's call before the current one commits, and the gate-invariant
        tests move the collection under the driver mid-unit precisely to catch
        a driver that acts on a stale read. A one-wide window would make that
        prefetch happen at concurrency 1 too, which is a semantic change
        smuggled in under a default.
        """
        for index, unit in enumerate(worklist, 1):
            _begin(index, unit)
            _attempt(unit)

    def _walk_concurrently(width: int) -> None:
        """Concurrent COMPUTE, strictly ordered COMMIT.

        A sliding window of paid calls runs ahead of a cursor that commits in
        WORKLIST ORDER -- the order phase 1 built and that resume determinism
        is pinned on. Everything that writes, prints or counts stays on this
        thread, and that single fact is what preserves the invariants one by
        one:

        * `judgments.jsonl` and the payload store are written by one thread,
          so the append-plus-fsync needs no lock and the file's line order is
          the worklist's order rather than a race's.
        * Exactly one `_finish` per attempted unit survives verbatim, because
          `_attempt` is called here and nowhere else.
        * `StorageFailure` can only be raised on this thread, so it still ends
          the batch BEFORE any further commit.
        * THE BREAKER KEEPS ITS MEANING. "N consecutive failures" is evaluated
          in commit order, which is deterministic, so a batch aborts at the
          same unit on every run and a resume behaves the way the abort
          message says it will. Counting in COMPLETION order would make the
          abort point depend on thread scheduling: the same collection,
          judged twice, would stop in two different places and neither would
          be reproducible.

        The price is bounded and worth naming: when the batch aborts or the
        operator interrupts, up to `2 * width - 1` paid calls are in flight or
        buffered -- already answered, not yet committed -- and every one of
        their results is discarded unwritten. The buffer above `width` is what
        the ordered commit costs: the worker budget counts only RUNNING calls,
        so answers pile up behind a slow head unit instead of the pool idling
        until it commits, which on a backend whose latencies span 30 s to 20
        minutes is the difference between a concurrent pass and a serial one.
        It is capped at twice the width, and not left to grow with the
        worklist, so the discard stays O(width) -- the same reason the window
        is not larger than it needs to be.
        """
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
        from concurrent.futures import wait as futures_wait

        # A future per unit, in worklist order. `None` for units that make no
        # call. The queue is the window: it is refilled to `width` RUNNING
        # paid calls -- and at most `2 * width` uncommitted ones -- before
        # each commit AND every time a worker frees during one, so a stretch
        # of gate-decided pairs cannot starve the pool and a stretch of votes
        # cannot overfill it.
        queue: deque[tuple[_Unit, Any, dict]] = deque()
        cursor = 0

        # NOT `with ThreadPoolExecutor(...)`. That context manager's `__exit__`
        # calls `shutdown(wait=True)` unconditionally, which JOINS every
        # worker -- so an abort or a Ctrl-C would sit waiting for calls it has
        # already decided to discard. On this backend that wait is not
        # theoretical: a worker can be inside a `codex exec` with a
        # twenty-minute timeout, its own transport retry and up to five
        # rate-limit backoffs, and `start_new_session=True` means the
        # terminal's Ctrl-C never reached that child either. The operator
        # would watch an interrupted pass hang for tens of minutes, and a
        # second Ctrl-C during the join escapes as a traceback with no
        # summary -- exactly the failure `_finish`'s docstring says this
        # section exists to remove.
        pool = ThreadPoolExecutor(max_workers=width)
        try:
            def _top_up() -> None:
                nonlocal cursor
                # RUNNING futures gate the worker budget; ALL uncommitted
                # paid entries gate the buffer. Counting done futures
                # against the width -- the first version -- idled every
                # worker behind one slow head unit, which is serial
                # execution wearing a --concurrency flag. The buffer cap is
                # what keeps the abort-time discard O(width): at most
                # 2*width - 1 paid results can be uncommitted when the walk
                # stops.
                running = sum(
                    1 for _, future, _ in queue
                    if future is not None and not future.done()
                )
                buffered = sum(
                    1 for _, future, _ in queue if future is not None
                )
                while (cursor < len(worklist) and running < width
                       and buffered < 2 * width):
                    unit = worklist[cursor]
                    cursor += 1
                    call = _paid_call(unit)
                    clock: dict = {}
                    if call is None:
                        queue.append((unit, None, clock))
                        continue
                    queue.append(
                        (unit, pool.submit(_timed, clock, call), clock)
                    )
                    running += 1
                    buffered += 1

            for index in range(1, len(worklist) + 1):
                _top_up()
                # PEEKED, not popped, and popped only once its future is
                # done: the head has to stay in the queue for the top-ups
                # below to count it. Popped first, it would hold a worker
                # that nothing counted, the window would run `width + 1`
                # calls against `width` workers, and the discard bound below
                # would be off by the one unit that is never discarded.
                unit, future, clock = queue[0]
                if future is None:
                    queue.popleft()
                    _begin(index, unit)
                    _attempt(unit)
                    continue

                # Block on THIS unit even if later ones finished first:
                # commit order is the whole point. `futures_wait` and not
                # `future.result()`, because the duration has to be read out
                # of the clock BEFORE `_begin` opens the line -- and a
                # `result()` here would raise the worker's exception one frame
                # OUTSIDE `_attempt`'s try, where the per-unit handler cannot
                # catch it and a single bad diff would end the batch. The
                # bound method is handed on unevaluated instead, so the raise
                # happens inside.
                #
                # Topping up only at the head of the for-loop is what made
                # the counting fix above a no-op for the very case it was
                # written for: the main thread spends the whole of a slow
                # head unit parked in this wait, so a worker that freed
                # during it stayed free until the head committed. Measured on
                # the fix without this loop -- four units, width 2, head
                # blocked -- exactly two calls ever started. So the wait is
                # FIRST_COMPLETED over the head AND its siblings, and every
                # wake tops the window up again.
                #
                # It is not a poll. Every wake is a future completing, and a
                # completed future is either replaced by a submission or
                # dropped from `waiting_on`, so the loop runs at most once
                # per future and then blocks on the head alone.
                while not future.done():
                    # Top up BEFORE the wait, never after it. The for-loop's
                    # call above ran while this unit's siblings were still
                    # running, so by the time the head blocks its counts are
                    # already stale -- and waiting on a stale set is not a
                    # smaller version of this bug, it is the whole bug back:
                    # a sibling that finished in between is filtered out of
                    # `waiting_on` as done, the wait then has nothing left to
                    # wake it, and the pool idles until the head returns.
                    _top_up()
                    # The head is ALWAYS waited on, even when `_top_up` just
                    # finished it, so a head that completes mid-top-up is
                    # committed now rather than held hostage to whichever
                    # sibling finishes next -- which on this backend is a
                    # twenty-minute hostage.
                    waiting_on = [future] + [
                        f for _, f, _ in queue
                        if f is not None and f is not future and not f.done()
                    ]
                    futures_wait(waiting_on, return_when=FIRST_COMPLETED)
                queue.popleft()
                _begin(index, unit, clock.get("seconds"))
                _attempt(unit, future.result)
        finally:
            # Nothing in flight is worth waiting for once the walk has
            # stopped: an abort, an interrupt and a clean finish all leave
            # results nobody will commit. `cancel_futures` keeps the
            # not-yet-started ones from being bought at all, and
            # `wait=False` means the SUMMARY PRINTS NOW rather than after the
            # slowest in-flight call.
            #
            # The stop is set FIRST, because `cancel_futures` can only
            # cancel calls that have not STARTED. A worker already inside the
            # backend's rate-limit ladder would otherwise wake from a backoff
            # after the report is on the terminal and buy a fresh call -- real
            # money spent on a verdict nobody will commit, and spent after the
            # pass reported what it had spent. Cooperative rather than a kill,
            # because the seam is one string in and one string out.
            #
            # This event belongs to THIS batch, so there is nothing to re-arm
            # afterwards: a second batch in the same process builds its own.
            #
            # Honest about what this does not fix: `concurrent.futures`
            # registers its own atexit join, so a worker still inside a
            # `codex exec` can keep the interpreter alive after the report is
            # on the terminal. That is a lingering process rather than a
            # hidden one -- the operator has their numbers, their warnings and
            # their exit-shaped report, and nothing further will be written.
            # Killing those children would mean this driver tracking the
            # backend's subprocesses, which is a seam the `CompleteFn`
            # contract deliberately does not have.
            stop_spending.set()
            pool.shutdown(wait=False, cancel_futures=True)

    try:
        if concurrency <= 1:
            _walk_sequentially()
        else:
            _walk_concurrently(concurrency)
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


def _digest_check(task, graded: Sequence[tuple[str, GradeRecord]],
                  ) -> tuple[str | None, str | None]:
    """Was every gating grade for this task taken against the manifest THIS
    pass loaded? Returns `(refusal, unknown)`, either or both `None`.

    The gate and the payload have to be anchored on one manifest. The gate
    decided which pairs are judgeable at all, against whatever `task.yaml` and
    `reference.diff` held at grading time; the payload anchors every verdict on
    the prompt and the solution diff this pass just loaded. Edit the task
    between the two and the judge scores submissions against a reference the
    gate never gated -- silently, with the right shape, and with no
    `task_version` bump needed, which is exactly why `GradeRecord` stores the
    digest beside the version rather than trusting the version to follow it.

    `graded` is the gating grades of the cells THIS pass selected, never the
    task's whole census -- the caller applies `--samples` before it asks. A
    check over unselected units files errors about units nobody chose, which
    is the exit contract's own complaint one step further in.

    A MISMATCH REFUSES THE WHOLE TASK, into the errors bucket. It is a hole in
    the reading rather than a verdict, and holes are what that bucket counts;
    the alternative is a block of comparisons that resolve, print and rank
    while naming two manifests. Per task and not per collection: the mismatch
    is a property of one manifest, and every other task in the collection is
    still coherent. The message names BOTH digests, because nothing in the
    numbers says which side moved, and BOTH remedies, because either end can
    be brought back to the other.

    AN ABSENT DIGEST IS UNKNOWN, NOT A MISMATCH. The grade's
    `graded_against_manifest_digest` defaults to `""`, so a grade written
    before the field existed carries none, and a task can carry none either --
    refusing on that would refuse a collection over a fact nobody recorded,
    while judging on it silently would claim a check that never ran. It warns
    and proceeds on either absence, and the two
    absences are worded apart: which SIDE could not answer is the whole content
    of the warning, and one phrase over both would send an operator to the
    wrong file.
    """
    expected = getattr(task, "manifest_digest", "") or ""
    mismatched: dict[str, list[str]] = {}
    unchecked: list[str] = []
    for run_id, grade in graded:
        seen = getattr(grade, "graded_against_manifest_digest", "") or ""
        # EITHER side absent is unchecked. A missing `expected` with a digest
        # on every grade line is the same "do not know" as the reverse, and
        # counting it in neither list is how a check reports a clean pass
        # over a comparison it never made.
        if not seen or not expected:
            unchecked.append(run_id)
        elif seen != expected:
            mismatched.setdefault(seen, []).append(run_id)

    refusal = None
    if mismatched:
        named = "; ".join(
            f"{digest} ({_named(sorted(runs))})"
            for digest, runs in sorted(mismatched.items())
        )
        count = sum(len(runs) for runs in mismatched.values())
        refusal = (
            f"task {task.task_id}: {count} grade line(s) were taken against "
            f"manifest digest {named}, but the task set loaded here carries "
            f"{expected}. Every unit of this task is refused rather than "
            "judged: the deterministic gate decided which pairs are judgeable "
            "against one manifest and the payload would anchor every verdict "
            "on another, so the verdicts would be taken against a reference "
            "the gate never gated. An edit to task.yaml or reference.diff "
            "moves the digest with no task_version bump, which is the usual "
            "cause. Two remedies: re-grade this task under the current task "
            "set (scripts/grade.py), or check out the task set state the "
            "grades were taken against -- graded_against_task_set_commit on "
            "the same grade lines -- and judge there."
        )

    unknown = None
    if unchecked and not expected:
        # THE OTHER SIDE could not answer: nothing to compare against.
        unknown = (
            f"task {task.task_id}: the loaded task carries no "
            f"manifest_digest, so the {len(unchecked)} grade line(s) gating "
            "it could not be checked against it. Judged anyway -- an absent "
            "digest is unknown, not a mismatch -- but nothing here proves the "
            "payloads are anchored on the manifest the gate gated."
        )
    elif unchecked:
        unknown = (
            f"task {task.task_id}: {len(unchecked)} grade line(s) name no "
            f"manifest digest ({_named(sorted(unchecked))}) and could not be "
            f"checked against the loaded task's {expected}. Judged anyway -- "
            "an absent digest is unknown, not a mismatch, and a grade written "
            "before the field existed carries none -- but nothing here proves "
            "the payloads are anchored on the manifest the gate gated. "
            "Re-grading under the current grader records it."
        )
    return refusal, unknown


def _judge_generation(judgment: JudgeRecord) -> tuple:
    """WHICH ORACLE said it: judge model, prompt version, rubric version, and
    the reasoning effort it was sampled at.

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

    The reasoning effort is the fourth for the same reason, one backend down:
    it is the codex backend's only sampling knob, so two passes at two efforts
    are two oracles, and pooling them would average a disagreement under one
    number that describes neither. It is derived from the STORED
    `judge_sampling` -- `None` on every mantle line, every gate-decided line
    and every codex line judged before the flag existed -- so no line already
    on disk changes generation.

    `or {}` for `_resume_key`'s reason, which is sharper here: a hand-edited
    `judge_sampling: null` is a line `load_judgments` hands over intact, and a
    bare `.get` on it raises `AttributeError` out of `summarize` -- after every
    call in the batch is bought. A null sampling block is the absent effort it
    looks like, and both readers of the field say so identically.
    """
    return (
        judgment.judge_model_id,
        judgment.judge_prompt_version,
        judgment.rubric_version,
        (judgment.judge_sampling or {}).get("model_reasoning_effort"),
    )


#: How many trailing elements of a `_comparison_key` name the generation.
#: `_comparison_key` APPENDS `_judge_generation`'s tuple and the matrix walk
#: slices it back off, so one constant holds both halves of that layout
#: together -- a hand-counted `[-3:]`, which is what this was until the
#: reasoning effort became the fourth field, would partition on a slice of one,
#: which is a pooling nothing would report.
_GENERATION_FIELDS = 4


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

    The four version fields stay, for `_resume_key`'s reason. A re-judge under
    a new prompt -- or, on the codex backend, at a new reasoning effort -- is a
    new generation of verdict, and pooling two generations into one comparison
    would average a disagreement the file exists to keep.
    They go LAST and as one appended tuple, because the matrix walk slices them
    back off with `_generation_of` to partition its blocks.
    """
    return (
        judgment.task_id,
        judgment.sample_index,
        judgment.run_id_a,
        judgment.run_id_b,
    ) + _judge_generation(judgment)


def _grade_generation(judgment: JudgeRecord) -> dict[str, int] | None:
    """WHICH GRADE GENERATION a line was gated on, as `run_id -> int`, or
    `None` when this line cannot answer.

    `grade_version_seen` is the field a verdict names its gate by, and
    `GRADER_VERSION` is a monotonic counter kept as a string ("1" -> "2" when
    `_refresh_index` changed what check 5 MEANS). Read as an integer, two
    lines' generations are ordered; read as strings they are not -- "10" sorts
    before "9" -- which is why this parses rather than compares.

    `None` for three real states, all of them "do not know": no map at all (a
    hand-edited `null`, or any line written before the field existed), an
    empty map, and a version this reader cannot order (a future
    `grader_version` of "2.1", or a commit). Every one of them must leave the
    preference where it was rather than pick a side, because a parse failure
    is not evidence about which line is newer -- see `_supersedes`.
    """
    seen = judgment.grade_version_seen or {}
    if not seen:
        return None
    versions: dict[str, int] = {}
    for run_id, version in seen.items():
        try:
            versions[run_id] = int(str(version).strip())
        except (TypeError, ValueError):
            return None
    return versions


def _supersedes(gate: dict[str, int] | None,
                votes: dict[str, int] | None) -> bool:
    """Is the gate-decided line's grade generation STRICTLY NEWER than the
    generation these votes were bought under?

    The whole content of rule 2's direction. Both maps must name the same two
    runs -- they describe one comparison, so a different key set is a line
    from somewhere else and is not comparable -- neither run may have gone
    BACKWARDS, and at least one must have moved forward. Equal generations are
    not newer, and the tie goes to the votes: they are the richer verdict and
    the only one a judge produced.

    `False` on every unknown, which is the safe direction and not an arbitrary
    one: the votes are what the collection paid for, and discarding them needs
    positive evidence that the grade under them no longer stands.
    """
    if not gate or not votes or set(gate) != set(votes):
        return False
    if any(gate[run_id] < votes[run_id] for run_id in gate):
        return False
    return any(gate[run_id] > votes[run_id] for run_id in gate)


def _gate_bucket(key: tuple) -> tuple:
    """The bucket a GATE-DECIDED line is filed in: its comparison key with the
    reasoning effort dropped.

    A gate-decided line records `judge_sampling={}` -- nothing was sent, and
    filling it would attest to a request that was never made -- so
    `_judge_generation` reads its effort as `None`. That is honest about the
    line and wrong about the FACT: which pair the deterministic ladder settled
    is a property of the grades, not of a sampling knob no call used. Filed
    under the full key, one codex pass at `--reasoning-effort high` split
    itself in two, its votes in the effort block and its own ladder results in
    a phantom `None` one. See the bucket walk in `summarize`, which is where
    the buckets are joined back up.

    `key[:-1]` and not a rebuild, because the effort is the LAST element of
    `_judge_generation` and therefore of `_comparison_key` -- pinned by
    `test_the_comparison_key_carries_the_generation_the_matrix_partitions_on`.
    """
    return key[:-1]


def _oracle_of(bucket: tuple) -> tuple:
    """The judge, prompt and rubric a gate bucket names -- everything about the
    oracle except the effort it was sampled at.

    The triple whose blocks a gate-decided line joins. A change to any of the
    three IS a different oracle and the ladder result is not shared across it:
    the versions gate resume, so two lines under two prompt versions are two
    units of work whose disagreement the file exists to keep. The effort is the
    one element a gate-decided line cannot carry, which is why it is the one
    element dropped.
    """
    return bucket[-3:]


def _deterministic(keys) -> list:
    """`sorted`, over keys that may hold a `None` beside an `int` or a `str`.

    A hand-edited or foreign-schema line can carry `sample_index=None`, and
    `sorted` on a tuple mixing `None` with `int` raises `TypeError` -- which
    would take the whole summary down at the end of a batch that already paid
    for thousands of calls. `repr` is a total order over anything, arbitrary but
    stable, and stable is the only property the walk needs.

    A GENERATION key needs it too, and needs it on a file no hand ever touched:
    its reasoning-effort element is `None` on every mantle and gate-decided
    line and a string on a codex line judged under `--reasoning-effort`, so one
    collection judged under both is the ordinary case that raises. That is why
    every walk over a generation-keyed dict here goes through this and not
    through `sorted`.
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


def _bootstrap_seed(generation: tuple) -> int:
    """This generation's bootstrap seed, derived from the generation itself.

    ONE GENERATOR PER BLOCK, not one per summary. A shared generator makes
    every block's draws depend on how many blocks were resampled before it, so
    appending a single line under a DIFFERENT oracle -- which cannot change a
    v2 comparison, a v2 win rate or a v2 rating -- shifts the v2 bands anyway:
    measured at 2.8 rating points, with the point estimate byte-identical.
    That is the same "the band moved while the collection did not" failure
    `BOOTSTRAP_SEED` exists to prevent, one level up, and it is worse than the
    unseeded version because it looks like a finding about the second oracle.

    `hashlib`, NOT `hash()`. Python salts `hash()` for strings and tuples per
    process (PYTHONHASHSEED), so a seed derived from it would differ between
    two runs of the same command on the same file -- reintroducing exactly the
    nondeterminism the seed is for, and only visibly on someone else's
    machine. sha256 over the generation's `repr` is stable across processes,
    machines and Python versions; 64 bits of it is far more entropy than
    `Random` needs to be well separated between blocks.
    """
    digest = hashlib.sha256(
        repr((BOOTSTRAP_SEED,) + generation).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big")


def _task_blocks(
    task_ids: list[str],
    outcomes: list[tuple[str, str, float]],
    cells: list[tuple],
) -> list[tuple[tuple, tuple]]:
    """The three PARALLEL lists, regrouped into one block per task.

    `task_ids[i]`, `outcomes[i]` and `cells[i]` are three views of comparison
    `i`: which task it came from, its `elo_from_outcomes` triple, and
    everything the resample tallies about it (see `_cell`). They are parallel
    rather than one list of wide records because `elo_from_outcomes` takes
    triples and nothing else: widening its input to carry a `task_id` would put
    a field it must never read into the contract every caller and every test
    writes against, and the fit is defined over win counts -- a rating that
    varied with the task labels attached to identical counts would be a bug
    that only this function could cause.

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


@dataclass(frozen=True)
class _RateTasks:
    """How many TASKS each rate on the printout actually rests on.

    THE BOUNDARY BAND IS ONLY AS WIDE AS ITS OWN EVIDENCE. When every resample
    of a rate agrees, `_proportion_interval` widens with a Wilson interval on a
    task count -- and handing it the BLOCK's task count says a swept pair that
    played 2 tasks was measured over all 40 the collection holds. Measured:
    [91.2%, 100.0%] printed where its own evidence supports [34.2%, 100.0%].

    Its sharpest form is the one that gives the game away: a pair playing
    exactly ONE task would slip past the one-task exception, and its band would
    NARROW as unrelated tasks were appended -- [100.0%, 100.0%] alone, then
    [34.2%, ...] at 2 tasks, [83.9%, ...] at 20, [94.0%, ...] at 60, with that
    pair's own evidence untouched throughout. That is §4.4's error (precision
    borrowed from correlated units) and `_bootstrap_seed`'s (one block's
    numbers moving because of another's) arriving together by a third route.

    So three counts, each matching one printed rate, all read off ONE walk of
    the cells rather than a second pass over the collection:

    * `per_pair` -- distinct tasks a pair played in, for its combined rate
    * `voted_per_pair` -- distinct tasks in which the JUDGE voted on that pair,
      for its judge-voted rate. The reachable case: an arm the ladder settled
      nearly everywhere has a judge-voted rate resting on two tasks inside a
      collection of forty.
    * `measurable` -- distinct tasks holding a comparison the position probe
      could read, for the consistency rate

    DISTINCT TASKS, not comparisons: ten comparisons of one pair on one task
    are one task's worth of evidence, which is the whole of §4.4.
    """

    per_pair: dict[tuple[str, str], int]
    voted_per_pair: dict[tuple[str, str], int]
    measurable: int


def _rate_task_counts(task_ids: list[str], cells: list[tuple]) -> _RateTasks:
    """The three counts, off one walk of the parallel task/cell lists.

    Sets while counting and lengths at the end, because a task contributes its
    evidence once however many comparisons it holds.
    """
    per_pair: dict[tuple[str, str], set[str]] = {}
    voted_per_pair: dict[tuple[str, str], set[str]] = {}
    measurable: set[str] = set()
    for task_id, cell in zip(task_ids, cells):
        _, pair, _, voted, probed, _ = cell
        per_pair.setdefault(pair, set()).add(task_id)
        if voted:
            voted_per_pair.setdefault(pair, set()).add(task_id)
        if probed:
            measurable.add(task_id)
    return _RateTasks(
        {pair: len(tasks) for pair, tasks in per_pair.items()},
        {pair: len(tasks) for pair, tasks in voted_per_pair.items()},
        len(measurable),
    )


@dataclass(frozen=True)
class _Resamples:
    """Every quantity the bootstrap resampled, as its raw samples.

    Samples rather than percentiles, because the caller that holds the point
    estimate is the one that has to decide what a missing or degenerate sample
    set means -- see `_interval` and `_proportion_interval`.

    ONE SET OF DRAWS FEEDS ALL FIVE. The voted-only rate and the consistency
    rate are tallied inside the same resample loop as the combined rate, not by
    a second pass: two independent resamplings of one collection would report
    a combined band and a judge-voted band that no single draw of the tasks
    ever produced together, and a reader compares exactly those two.

    NO TASK COUNT TRAVELS HERE. The boundary rule needs the number of tasks the
    RATE rests on, which is a different number for almost every rate in a
    block -- see `_RateTasks`, which is where it comes from.
    """

    win_rates: dict[tuple[str, str], list[float]]
    voted_win_rates: dict[tuple[str, str], list[float]]
    ratings: dict[str, list[float]]
    voted_ratings: dict[str, list[float]]
    consistency: list[float]


def _cluster_bootstrap(
    blocks: list[tuple[tuple, tuple]],
    rng: random.Random,
    anchor: dict[str, float],
    voted_anchor: dict[str, float],
) -> _Resamples:
    """`BOOTSTRAP_RESAMPLES` draws of the task set, with replacement.

    THE WHOLE COLLECTION IS RESAMPLED ONCE PER DRAW, not once per pair: the
    pairs in a block are the same tasks seen from different sides, and drawing
    independently per pair would produce a set of intervals no single
    collection could have produced.

    The ratings are REFIT on every draw rather than perturbed, which is what
    `elo_from_outcomes` being a maximum-likelihood fit over win counts buys:
    the fit is a function of the resampled counts alone, with no walk order to
    carry over from the original stream.

    EVERY REFIT IS RE-CENTRED ON THE ARMS IT SHARES WITH THE FULL FIT
    (`_recentred`), and this is a correction rather than a nicety. The anchor
    is a presentation choice -- Bradley-Terry identifies differences and
    nothing else, so `elo_from_outcomes` puts the MEAN of whatever arms it was
    given at 1000. A resample that misses an arm therefore re-centres on a
    different arm set, and every surviving arm's rating shifts by the mean of
    the missing ones: measured at a 69-point offset between arm-A's samples
    with and without arm-C present. Those shifted samples land in the
    percentiles of arms that were never absent, widening their bands with an
    artefact of the anchor. Re-centring on the shared arms removes the shift
    while keeping every difference the resample actually fit.

    Allocation-light on purpose -- this runs 1,000 times inside every summary,
    including the ones in the test suite. The triples inside a block are stored
    once and REFERENCED by each draw (they are never mutated), the pair tallies
    are two-slot lists updated in place, and the per-draw allocations are the
    draw, the two triple lists the fits need, and the re-centred dicts.
    """
    win_rates: dict[tuple[str, str], list[float]] = {}
    voted_win_rates: dict[tuple[str, str], list[float]] = {}
    ratings: dict[str, list[float]] = {}
    voted_ratings: dict[str, list[float]] = {}
    consistency: list[float] = []
    if not blocks:
        return _Resamples(win_rates, voted_win_rates, ratings, voted_ratings,
                          consistency)

    for _ in range(BOOTSTRAP_RESAMPLES):
        outcomes: list[tuple[str, str, float]] = []
        voted_outcomes: list[tuple[str, str, float]] = []
        tally: dict[tuple[str, str], list] = {}
        measurable = 0
        consistent = 0
        for block_outcomes, block_cells in rng.choices(blocks, k=len(blocks)):
            outcomes.extend(block_outcomes)
            for cell in block_cells:
                outcome, pair, score_x, voted, probed, agreed = cell
                counted = tally.get(pair)
                if counted is None:
                    # [combined points, combined count, voted points, voted
                    # count] -- one four-slot list per pair per draw rather
                    # than two dicts, updated in place.
                    counted = tally[pair] = [0.0, 0, 0.0, 0]
                counted[0] += score_x
                counted[1] += 1
                if voted:
                    voted_outcomes.append(outcome)
                    counted[2] += score_x
                    counted[3] += 1
                measurable += probed
                consistent += agreed
        for pair, counted in tally.items():
            points, played, voted_points, voted_played = counted
            win_rates.setdefault(pair, []).append(points / played)
            if voted_played:
                voted_win_rates.setdefault(pair, []).append(
                    voted_points / voted_played
                )
        if measurable:
            consistency.append(consistent / measurable)
        for model, rating in _recentred(
            elo_from_outcomes(outcomes), anchor
        ).items():
            ratings.setdefault(model, []).append(rating)
        for model, rating in _recentred(
            elo_from_outcomes(voted_outcomes), voted_anchor
        ).items():
            voted_ratings.setdefault(model, []).append(rating)
    return _Resamples(win_rates, voted_win_rates, ratings, voted_ratings,
                      consistency)


def _recentred(
    fit: dict[str, float], anchor: dict[str, float],
) -> dict[str, float]:
    """One resample's ratings, shifted onto the full fit's level.

    The shift is whatever makes the mean over the arms this fit SHARES with the
    anchor equal the anchor's mean over those same arms -- see
    `_cluster_bootstrap` for why. A pure translation, so every rating
    DIFFERENCE the resample fit is untouched; only the free overall level
    moves, and it moves onto the level the printed point estimates are on.

    No shared arms means there is no common level to move onto, and the fit is
    returned as it stands rather than translated by an arbitrary amount.
    """
    shared = [model for model in fit if model in anchor]
    if not shared:
        return fit
    shift = (
        math.fsum(anchor[model] for model in shared)
        - math.fsum(fit[model] for model in shared)
    ) / len(shared)
    return {model: rating + shift for model, rating in fit.items()}


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
    rather than the arms. An arm missing from every resample but one has no
    distribution to read at all, so below `_MIN_RESAMPLES` the point estimate
    is returned rather than a zero-width band off a single draw.

    A DEGENERATE BAND HERE IS NOT WIDENED, unlike `_proportion_interval`'s, and
    it is not printed as a band either: `_print_reading` prints a dash in its
    place. Zero width means every task resample produced the same fit, which at
    a swept boundary is the bootstrap's floor rather than certainty -- and the
    rating it would be a band around is itself an artefact of the half-game
    prior against the task count (a swept pair fits 1139.8 at two tasks and
    1520.6 at two hundred, on evidence that says the same thing), so a
    zero-width band there is false exactness twice over.

    WHY NOT A WILSON-DERIVED BAND, since the fit stays finite: the map from a
    win rate to a rating is unambiguous only for TWO arms. With three or more,
    a rating is a function of the whole comparison graph -- every arm's
    strength enters through its opponents' -- so pushing one pair's rate bound
    through the scale would be a two-arm special case dressed up as a rule, and
    it would disagree with the fit on every collection that has a third arm.
    The rating table is DESCRIPTIVE of the win rates, whose bands do carry the
    boundary correction, and the printout says so where the table is printed.
    """
    if not samples or len(samples) < _MIN_RESAMPLES:
        return (point, point)
    ordered = sorted(samples)
    return (_percentile(ordered, 0.025), _percentile(ordered, 0.975))


def _proportion_interval(
    samples: list[float] | None, point: float, tasks: int,
) -> tuple[float, float]:
    """`_interval` for a RATE, with the boundary case answered rather than hit.

    A pair one arm swept -- every comparison a win, which is what a gate-swept
    arm looks like -- gives 1.0 in every resample, and the percentiles of a
    constant are that constant. The band printed is then `[100.0%, 100.0%]` at
    ANY task count: a 95% interval claiming the collection ruled out every
    other value, which is the one thing a confidence interval must never say.
    It is not rare either: `gemma-4-31b` is 9-of-9 gate-decided in the pilot
    collection. The same arithmetic hits any rate with no variation across
    resamples, a pair that drew every comparison included.

    So when the resamples do not vary, the band falls back to a WILSON score
    interval at the point estimate over `tasks`. Wilson rather than
    normal-approximation because the normal interval is exactly [p, p] at p=0
    and p=1 -- the same failure again -- while Wilson stays inside [0, 1] and
    keeps a sensible width at the boundary: 60 swept tasks read as
    [94.0%, 100.0%], two swept tasks as [34.2%, 100.0%].

    `tasks` IS THIS RATE'S OWN TASK COUNT, never the block's -- see
    `_RateTasks`, which is where each call site gets it. A task count and never
    a comparison count, for §4.4's reason: the comparisons inside a task are
    correlated, and the boundary band must not claim the precision the
    clustering exists to refuse.

    ONE TASK IS THE EXCEPTION and is left degenerate -- per RATE, so a pair
    that played one task keeps its degenerate band however many tasks the
    collection holds. There, "no variation across resamples" is not a boundary
    artefact: there is only one thing to draw, so the bootstrap is reporting
    exactly what it knows about a rate with no spread over tasks behind it, and
    a Wilson band on n=1 ([20.7%, 100.0%] for a swept pair) would dress a
    single task up as a measurement of the population. The printout marks those
    rows rather than leaving a reader to infer why one band has no width.
    """
    if samples and len(samples) >= _MIN_RESAMPLES:
        ordered = sorted(samples)
        if ordered[0] != ordered[-1]:
            return (_percentile(ordered, 0.025), _percentile(ordered, 0.975))
    if tasks > 1:
        return _wilson_interval(point, tasks)
    return (point, point)


def _wilson_interval(point: float, count: int) -> tuple[float, float]:
    """Wilson score interval for `point` observed over `count` units.

    Used only at `_proportion_interval`'s boundary -- see there for why this
    and not the normal approximation. Clipped into [0, 1] because a rate
    outside it is not a rate, and `count` is positive by the one call site's
    guard (`tasks > 1`).
    """
    z_squared = _Z_95 * _Z_95
    denominator = 1.0 + z_squared / count
    centre = (point + z_squared / (2 * count)) / denominator
    half = (_Z_95 / denominator) * math.sqrt(
        point * (1.0 - point) / count + z_squared / (4 * count * count)
    )
    return (max(0.0, centre - half), min(1.0, centre + half))


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
    2. **Within one comparison, the line gated on the NEWER grade wins, and a
       tie goes to the votes.** A comparison can hold both kinds of line at
       once, because the file is append-only and a re-grade changes which pairs
       are judgeable. Counting both enters one comparison twice, once for each
       side, so one of them has to give way -- and which one is a question
       about the GRADES, not about the kinds of line.

       Both directions happen. A re-grade that flips a failed side to passing
       turns a gate-decided pair into a judgeable one, and the votes bought
       afterwards are the later, richer verdict: they win. A re-grade that
       flips a PASSING side to failed does the reverse -- the pair the judge
       voted on becomes one the ladder settles, and the votes on disk were
       bought against a grade that no longer stands. Preferring votes
       unconditionally ranked an arm on a verdict about a submission the ladder
       now rejects, under a docstring that asserted the direction rather than
       checking it.

       So the direction is MEASURED, off `grade_version_seen` -- the field
       every line carries for exactly this purpose, mapping each run to the
       `grader_version` that gated it (see `_grade_generation`, `_supersedes`).
       The gate-decided line wins only when its grade generation is strictly
       newer than the one the votes were bought under; equal, unreadable or
       absent generations leave the votes in front. `judged_at` is deliberately
       not consulted: it orders wall clocks rather than grade generations, and
       a gate line appended later against an older grade file would read as
       newer.

       Whichever side gives way is TALLIED rather than quietly resolved --
       `superseded_gate_decided` one way, `superseded_votes` the other. The
       file is append-only, so the disagreement is the finding, and votes left
       out of a rate are paid calls missing from a denominator.

       Both tallies are in LINES, which is the word the printout uses, and the
       gate side is deduplicated across the effort blocks rule 4 hands one
       gate-decided line to: counted per block, a collection judged at two
       efforts reported two supersessions of the one line it holds.
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

       **A gate-decided line belongs to no generation, and so joins every
       block that shares its `(judge_model_id, judge_prompt_version,
       rubric_version)` triple -- whatever that block's effort.** It is a
       LADDER fact: no call was made for it, `judge_sampling` is `{}` because
       nothing was sent, and the effort `_judge_generation` reads off that
       empty block is the absence of a call rather than a reading. Partitioned
       on it, one codex pass at `--reasoning-effort high` reported itself as
       two blocks -- the effort block with every ladder-settled pair missing
       from its win rates and its Elo under a `gate_decided: 0`, and a phantom
       effort-`None` block holding only the ladder results -- and rule 2 could
       never fire, because the votes and the gate line for one pair sat in
       different buckets. Blocks are opened by VOTES; a triple with no vote
       line anywhere reports its ladder results under the `None` they carry.
       When a collection holds two effort generations the same gate line is
       counted in both blocks, which is correct precisely because rule 4 never
       sums them: they are two readings reported side by side, and a pair
       present in one and absent from the other would make them readings of
       two different collections. This is an AGGREGATION rule only -- no line
       on disk changes shape, and a gate-decided line still resumes at effort
       `None`, since nothing was sent for it and a flag change must not
       re-append it.
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
    reported without a confidence interval is not reported"), the voted-only
    rates and the consistency rate included -- they are rates, and the sentence
    has no exemption in it. All of them come out of ONE set of task resamples
    per generation, so the combined band and the judge-voted band beside it
    describe draws of the collection that actually happened together. See
    `BOOTSTRAP_RESAMPLES`, `_cluster_bootstrap` and `_Resamples`.

    The clustering is the whole content: comparisons inside one task are
    correlated, so an interval that resamples comparisons independently is
    several times too narrow and fails by printing precision the collection
    does not have. Two rules sit on top of it, each placed against a way a
    resampled band lies: `_proportion_interval` refuses to print a zero-width
    band for a swept rate at more than one task, and `_bootstrap_seed` gives
    each generation its own generator so a foreign oracle's lines cannot move
    another generation's band.

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
    votes_by: dict[
        tuple, list[tuple[int, str, str | None, dict[str, int] | None]]
    ] = {}
    # THE LINE, not its winner: rule 2 reads the grade generation off it as
    # well as the side the ladder picked, and two structures holding one line's
    # two fields drift apart on the day one of them is filled somewhere else.
    gate_by: dict[tuple, JudgeRecord] = {}
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
                # Filed WITHOUT the effort -- see `_gate_bucket`. The line
                # itself is unchanged on disk and resumes exactly as it did.
                gate_by[_gate_bucket(key)] = judgment
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
        #
        # The GRADE GENERATION travels with it too, per line rather than per
        # bucket: rule 2's direction is a question about the grade each vote
        # was bought under, and a re-judge can leave one comparison holding
        # votes from either side of a re-grade. Parsed here, once per line,
        # where an unreadable one is already `None` by the time the walk asks.
        votes_by.setdefault(key, []).append(
            (judgment.vote_index if judgment.vote_index is not None else -1,
             judgment.verdict, judgment.position_assignment,
             _grade_generation(judgment))
        )

    # BOTH COUNTERS ARE IN LINES, because "line(s)" is the word the printout
    # puts beside each of them and a census figure a reader cannot reconcile
    # against the file is worse than no figure.
    #
    # They are accumulated differently, and the asymmetry is rule 4's: a vote
    # line carries its own effort and lands in exactly ONE block, so summing
    # per block already counts it once; a gate-decided line joins every block
    # sharing its oracle triple, so the same sum reported 2 for a collection
    # judged at two efforts that holds exactly one such line on disk. The gate
    # side is therefore a SET of buckets -- one entry per line -- and a line
    # superseded in any block it joins is a line that was set aside.
    #
    # The blocks themselves still double-count that line, which is correct and
    # is rule 4's whole point: they are readings reported side by side and are
    # never summed. These counters are not block figures.
    superseded_gate_lines: set[tuple] = set()
    superseded_votes = 0
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
    # Per comparison: its outcome triple (the same object `outcomes` holds),
    # the matrix cell it lands in, x's score, whether the judge voted on it,
    # and the two position-probe bits. One shape, read by `_task_blocks`, by
    # `_rate_task_counts` and inside the resample loop.
    cells: dict[
        tuple,
        list[tuple[tuple[str, str, float], tuple[str, str], float, bool, int,
                   int]],
    ] = {}
    voted_outcomes: dict[tuple, list[tuple[str, str, float]]] = {}
    voted_points: dict[tuple, dict[tuple[str, str], float]] = {}
    pairs: dict[tuple, dict[tuple[str, str], dict[str, int]]] = {}
    gate_share: dict[tuple, dict[str, dict[str, int]]] = {}
    consistency: dict[tuple, dict[str, int]] = {}

    # WHICH BLOCKS EACH GATE-DECIDED LINE JOINS, and it is every block that
    # shares its oracle triple -- whatever effort that block was sampled at.
    # The blocks a collection has are the ones its VOTES opened: a gate-decided
    # line opens none of its own, because the effort it stores is the absence
    # of a call rather than a reading. A triple with no vote line anywhere is
    # the exception and the only one: the ladder results are then the whole
    # reading, and they report under the `None` they honestly carry.
    #
    # A collection judged at two efforts therefore counts the SAME gate line in
    # both blocks. That is correct and it is the only place double-counting is:
    # the blocks are two readings reported side by side and are never summed,
    # and a ladder-settled pair present in one and missing from the other would
    # make the two blocks readings of different collections.
    vote_efforts: dict[tuple, set] = {}
    for key in votes_by:
        vote_efforts.setdefault(_oracle_of(_gate_bucket(key)), set()).add(
            key[-1]
        )
    walk = set(votes_by)
    for bucket in gate_by:
        for effort in vote_efforts.get(_oracle_of(bucket)) or {None}:
            walk.add(bucket + (effort,))

    for key in _deterministic(walk):
        vote_lines = votes_by.get(key)
        gate = gate_by.get(_gate_bucket(key))
        # Rule 2's direction, measured rather than assumed. The gate-decided
        # line takes the comparison only when its grade generation is strictly
        # newer than EVERY vote's: newer than the newest of them is newer than
        # all, and `all` over the list says so without a max over values that
        # need not be comparable. `False` whenever the gate line is absent,
        # equal or unreadable, which is the direction the votes keep.
        gate_versions = None if gate is None else _grade_generation(gate)
        outdated = bool(vote_lines) and gate is not None and all(
            _supersedes(gate_versions, versions)
            for *_, versions in vote_lines
        )
        if vote_lines and not outdated:
            if gate is not None:
                # The BUCKET, not a counter: one entry per gate-decided line,
                # however many effort blocks that line joined.
                superseded_gate_lines.add(_gate_bucket(key))
            # `majority` cannot see an empty sequence here: the bucket exists
            # only because a vote line landed in it.
            winner = majority([
                verdict for _, verdict, _, _ in
                sorted(vote_lines, key=lambda line: line[:2])
            ])
            settled = "voted"
        else:
            if outdated:
                # PER LINE, matching the phrase the printout uses: these are
                # paid calls left out of a rate, and a comparison count would
                # understate what the reader is missing. Summing per block is
                # safe HERE and only here -- a vote line carries its own
                # effort, so it sits in one block and is met once.
                superseded_votes += len(vote_lines)
            winner = gate.gate_decided_by
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
        # votes and so no positions, and is counted in no column.
        #
        # A comparison whose votes were superseded by a newer gate line still
        # counts here, and deliberately: the probe asks whether the judge said
        # the same thing in both orders, and it did or it did not. Which line
        # settled the comparison is a different question, answered above.
        counts = consistency.setdefault(
            generation,
            {"measurable": 0, "consistent": 0, "single_position": 0,
             "position_unrecorded": 0},
        )
        probed = agreed = 0
        if vote_lines:
            # The verdict set is taken over the SAME lines the position check
            # accepted. A line whose position is unreadable says nothing about
            # position, so letting its verdict into the set would let it break
            # an agreement between the two positions that were actually
            # recorded -- a comparison marked inconsistent on the strength of a
            # line the measurable test had already excluded.
            placed = [
                (verdict, position) for _, verdict, position, _ in vote_lines
                if position in VOTE_POSITIONS
            ]
            positions = {position for _, position in placed}
            if len(positions) == len(VOTE_POSITIONS):
                probed = 1
                agreed = int(len({verdict for verdict, _ in placed}) == 1)
            elif positions:
                # One position only: a v2 comparison whose other vote errored,
                # or a v1 draw that never split. The verdict carries whatever
                # bias that order has and there is nothing to cancel it
                # against, which is invisible in every other number here -- it
                # resolves through `majority` and enters the matrix like any
                # other comparison. Counted so a consistency rate is read
                # beside how many verdicts it could not cover.
                counts["single_position"] += 1
            else:
                # Votes, but not one of them records a position this reader
                # knows -- a hand-edited line, or a foreign schema. Its own
                # column rather than silence: it is neither probed nor known to
                # rest on one order, and a comparison that appears in no column
                # at all is a denominator moving invisibly, which is the
                # failure `dropped` exists to prevent one level down.
                counts["position_unrecorded"] += 1
        counts["measurable"] += probed
        counts["consistent"] += agreed
        # PARALLEL to `outcomes`, and carrying the triple itself so a resample
        # can rebuild the voted-only fit without a second walk -- see
        # `_Resamples`: all five quantities come out of one set of draws.
        cells.setdefault(generation, []).append(
            (outcome, pair, score_x, settled == "voted", probed, agreed)
        )

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

    comparisons: dict[tuple, dict[tuple[str, str], dict]] = {}
    elo: dict[tuple, dict[str, float]] = {}
    elo_voted: dict[tuple, dict[str, float]] = {}
    elo_ci95: dict[tuple, dict[str, tuple[float, float]]] = {}
    elo_voted_ci95: dict[tuple, dict[str, tuple[float, float]]] = {}
    resampled: dict[tuple, _Resamples] = {}
    rate_tasks: dict[tuple, _RateTasks] = {}
    for generation in _deterministic(pairs):
        # One table per generation, for rule 4's reason. Pooling two oracles
        # fits one set of strengths to two different opinions about the same
        # pair, printing a consensus neither of them gave.
        ratings = elo_from_outcomes(outcomes[generation])
        voted_ratings = elo_from_outcomes(voted_outcomes.get(generation, []))
        # ONE GENERATOR PER GENERATION, seeded from the generation itself --
        # see `_bootstrap_seed`. A generator shared across blocks makes each
        # block's draws depend on which other oracles happen to be in the file.
        samples = _cluster_bootstrap(
            _task_blocks(
                clusters[generation], outcomes[generation], cells[generation],
            ),
            random.Random(_bootstrap_seed(generation)),
            ratings,
            voted_ratings,
        )
        resampled[generation] = samples
        # How many TASKS each rate in this block rests on -- its own count, not
        # the block's. See `_RateTasks`.
        tasks = _rate_task_counts(clusters[generation], cells[generation])
        rate_tasks[generation] = tasks
        elo[generation] = ratings
        elo_voted[generation] = voted_ratings
        elo_ci95[generation] = {
            model: _interval(samples.ratings.get(model), rating)
            for model, rating in ratings.items()
        }
        elo_voted_ci95[generation] = {
            model: _interval(samples.voted_ratings.get(model), rating)
            for model, rating in voted_ratings.items()
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
            # `None`, never `0.0`, on a row the judge was never asked about:
            # 0/0 raises out of the middle of a paid batch's summary, and a
            # `0.0` in its place is the judge's worst possible verdict printed
            # for a judge that never voted. The interval follows it: a band
            # around a rate that does not exist is worse than no band.
            voted_rate = (
                voted_x.get(pair, 0.0) / row["voted"] if row["voted"] else None
            )
            block[pair] = {
                "voted": row["voted"],
                "gate_decided": row["gate_decided"],
                "wins_x": row["wins_x"],
                "wins_y": row["wins_y"],
                "ties": row["ties"],
                "comparisons": total,
                "win_rate_x": win_rate_x,
                "win_rate_y": (row["wins_y"] + 0.5 * row["ties"]) / total,
                "win_rate_x_voted": voted_rate,
                "win_rate_x_ci95": _proportion_interval(
                    samples.win_rates.get(pair), win_rate_x,
                    tasks.per_pair.get(pair, 0),
                ),
                "win_rate_x_voted_ci95": (
                    None if voted_rate is None else _proportion_interval(
                        samples.voted_win_rates.get(pair), voted_rate,
                        tasks.voted_per_pair.get(pair, 0),
                    )
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
        "elo_voted_ci95": elo_voted_ci95,
        "gate_decided_share": {
            generation: {
                model: gate_share[generation][model]
                for model in sorted(gate_share[generation])
            }
            for generation in _deterministic(gate_share)
        },
        "position_consistency": {
            generation: _consistency_block(
                consistency[generation], resampled.get(generation),
                rate_tasks.get(generation),
            )
            for generation in _deterministic(consistency)
        },
        # Gate-decided LINES set aside by votes, counted once each however
        # many effort blocks the line joined -- see `superseded_gate_lines`.
        "superseded_gate_decided": len(superseded_gate_lines),
        # Rule 2's other direction, in vote LINES: paid calls the newer ladder
        # result left out of every rate above.
        "superseded_votes": superseded_votes,
        "dropped": dropped,
    }


def _consistency_block(
    counts: dict[str, int], samples: _Resamples | None,
    tasks: _RateTasks | None,
) -> dict:
    """One generation's position-swap probe: the counts, the rate, the band.

    The rate is `None` on a block with nothing to probe, for
    `win_rate_x_voted`'s reason -- a 0.0 there would read as a judge that
    contradicted itself every time rather than as a judge nobody could check --
    and the interval follows the rate: no rate, no band.

    It is a RATE over comparisons, so it gets the same treatment as the win
    rates (§10.3 does not exempt it): the same task resamples, and
    `_proportion_interval`'s boundary rule, which matters here more than
    anywhere else on the printout -- a judge that agreed with itself on every
    measurable comparison is the expected shape of a healthy block, and that is
    exactly the degenerate resample that would otherwise print
    `100.0% [100.0%, 100.0%]`.

    Its boundary n is the number of tasks holding a MEASURABLE comparison, not
    the block's task count: a block where the probe can read two tasks out of
    forty knows what two tasks know, and the other thirty-eight say nothing
    about position at all.
    """
    rate = (
        counts["consistent"] / counts["measurable"]
        if counts["measurable"] else None
    )
    return {
        **counts,
        "rate": rate,
        "rate_ci95": (
            None if rate is None or samples is None or tasks is None
            else _proportion_interval(
                samples.consistency, rate, tasks.measurable
            )
        ),
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

    THE FOUR VERSION FIELDS ARE IN THE KEY, for `_resume_key`'s reason and
    `_comparison_key`'s. `_resume_key` keys a rubric line on `("rubric",
    run_id, judge_model_id, judge_prompt_version, rubric_version, effort)`,
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
    for key in _deterministic(by_key):
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

    # Guarded, because this loop prints text from two sources that can carry
    # characters an ASCII stdout refuses. The neutral-judge banner carries a
    # `§`, and a raise here would take the WARNING section down along with the
    # error section below it -- leaving an operator a complete, caveated
    # reading of a biased pass with the one line naming the bias removed. The
    # rest of the loop is collection-derived: the excluded and ungraded
    # warnings name run ids, the abort names task ids and model names.
    for warning in result["warnings"]:
        _print_ascii_safe(f"\nWARNING: {warning}")
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

    `(judge_model_id, judge_prompt_version, rubric_version, effort)` -- the
    four fields `_comparison_key` and `_rubric_profile` partition on, and
    therefore the four a reader needs in order to know which two blocks are
    comparable. One phrasing across the matrix, the profile and the Elo tables,
    because two spellings of one generation read as two generations.

    The effort is printed only when there is one, so every mantle block and
    every block from a pass that named no effort prints the bytes it always
    did -- a trailing `, effort None` on the three-quarters of blocks that can
    never carry one is a field a reader learns to skip, and then skips on the
    codex block where it is the whole difference between two of them.
    """
    judge_model_id, prompt_version, rubric_version, effort = generation
    label = (
        f"judge {judge_model_id}, prompt v{prompt_version}, "
        f"rubric {rubric_version}"
    )
    return label if effort is None else f"{label}, effort {effort}"


def _rate_pair(
    model_x: str, model_y: str, rate_x: float,
    interval: tuple[float, float],
) -> str:
    """One row's two rates with their bands: `x 62.5% [45.0%, 78.3%] / y ...`.

    Y'S BAND IS X'S MIRRORED, and exactly so rather than approximately: the two
    rates sum to 1 in every resample and at both ends of the Wilson fallback,
    so the 2.5th percentile of one IS one minus the 97.5th of the other. It is
    printed rather than left to the reader to subtract, because a rate with no
    band beside it reads as the one number on the row that was measured
    precisely -- and the mirror is written here once, for the combined rate and
    the judge-voted rate both, so the two cannot drift into disagreeing about
    which end is which.

    A ZERO-WIDTH BAND IS MARKED where it is printed. After
    `_proportion_interval`'s boundary rule the only rate that can produce one
    is a rate resting on a single task, and a reader who meets `[100.0%,
    100.0%]` three rows below a widened band has no way to tell which rule
    produced it. Said on the row rather than inferred from the header.

    "One task" is exact rather than usually-right: the widened branch never
    returns zero width, and the percentile branch cannot either. Two task
    blocks that differ at all are drawn in different proportions in more than
    a twentieth of the resamples -- the chance of never drawing a given block
    is at most (1 - 1/T)^T, which is 0.25 at T=2 and rises only to ~0.37 -- so
    a rate with any spread across tasks has more than 5% of its mass off its
    modal value, and its 2.5th and 97.5th percentiles cannot coincide.
    """
    low, high = interval
    mirrored = (
        f"{model_x} {rate_x:.1%} [{low:.1%}, {high:.1%}] / "
        f"{model_y} {1.0 - rate_x:.1%} [{1.0 - high:.1%}, {1.0 - low:.1%}]"
    )
    if low == high:
        return f"{mirrored}  (one task: no spread to resample)"
    return mirrored


def _rating_band(interval: tuple[float, float]) -> str:
    """One rating's band, or a dash where it would claim certainty.

    A zero-width rating band means every task resample produced the same fit,
    and unlike a rate that cannot be corrected -- see `_interval` for why the
    win rates' Wilson fallback does not transfer to a rating. What is left is a
    choice between printing `[1416.6, 1416.6]`, which says the collection
    resolved this arm exactly, and printing nothing, which says it did not. The
    table already prints `--` for an arm the judge never voted on, so the same
    dash carries the same meaning here: a refusal, not a value.

    Padded to the width of a printed band so the columns stay aligned either
    way -- an unpadded dash would shift everything to its right on that row.
    """
    low, high = interval
    if low == high:
        return f"{'--':^18}"
    return f"[{low:7.1f}, {high:7.1f}]"


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
        low, high = counts["rate_ci95"]
        print(
            f"    position consistency: {counts['consistent']} of "
            f"{counts['measurable']} measurable comparison(s) "
            f"{counts['rate']:.1%} [{low:.1%}, {high:.1%}] gave the same "
            "verdict in both forced positions"
        )
    if counts["single_position"]:
        print(
            f"      {counts['single_position']} further comparison(s) rest on "
            "ONE position -- the other position's vote is not in the file, so "
            "the verdict carries that order's bias with nothing to cancel it"
        )
    if counts["position_unrecorded"]:
        print(
            f"      {counts['position_unrecorded']} further comparison(s) "
            "record no position this reader knows, so nothing above can say "
            "whether their verdicts rest on one order or two"
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
        f"({BOOTSTRAP_RESAMPLES} resamples, seeded per block, so two readings "
        "of one file print one interval). §4.4: comparisons inside one task "
        "are correlated, so resampling them independently would print a band "
        "several times too narrow -- precision this collection does not have"
    )
    print(
        "  A rate every resample agreed on -- a pair one arm SWEPT is the "
        "usual way -- has no bootstrap spread to report, and a band of zero "
        "width would claim the collection ruled every other value out. Those "
        "fall back to a Wilson score interval over THE TASKS THAT RATE RESTS "
        "ON, which is its own count and not the block's: a pair playing 2 of "
        "40 tasks reads [34.2%, 100.0%], the same as if those two tasks were "
        "the whole collection. A rate resting on ONE task is the exception, "
        "prints its point estimate at both ends and is marked as such -- "
        "there is nothing to resample, and a band there would dress one task "
        "up as a measurement of the population"
    )
    print(
        "  JUDGE-VOTED ONLY is the same rate with the gate-decided "
        "comparisons taken out. A gate-decided comparison is a Tier A result "
        "standing in a Tier B number, so a row with a large gate-decided "
        "share is partly Tier A's pass rate re-derived -- two arms this judge "
        "cannot separate can still sit either side of §10.1's 40% pairwise "
        "reference on their gate results alone. Read the two together"
    )
    for generation in _deterministic(summary["comparisons"]):
        block = summary["comparisons"][generation]
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
                f"comparison(s), {row['voted']} judged and "
                f"{row['gate_decided']} gate-decided"
            )
            print(
                "      combined          "
                + _rate_pair(model_x, model_y, row["win_rate_x"],
                             row["win_rate_x_ci95"])
            )
            voted_rate = row["win_rate_x_voted"]
            if voted_rate is None:
                print(
                    "      judge-voted only  NOTHING: every comparison in "
                    "this row was settled by the ladder, so the rate above "
                    "is a Tier A result"
                )
            else:
                print(
                    "      judge-voted only  "
                    + _rate_pair(model_x, model_y, voted_rate,
                                 row["win_rate_x_voted_ci95"])
                    + f"  over {row['voted']} judged comparison(s)"
                )
        _print_block_provenance(summary, generation)

    print(
        "\nrubric profile per model and judge generation -- diagnostic, never "
        "summed across dimensions and never joined to resolved. Each mean is "
        "on its own rubric's anchored scale, over the n line(s) that carried "
        "that name"
    )
    for key in _deterministic(summary["rubric_profile"]):
        row = summary["rubric_profile"][key]
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
        "  The interval is the matrix's -- the same task resamples, refit, "
        "each refit re-centred on the arms it shares with the full fit -- and "
        "it is the number to read first: a gap narrower than the band is a "
        "gap this collection did not measure. JUDGE-VOTED ONLY refits over "
        "the judged comparisons alone; where it disagrees with the combined "
        "rating, the difference is the ladder's"
    )
    print(
        "  A rating whose every task resample gave the same fit prints -- for "
        "its band rather than a zero-width one. At a swept boundary the fit "
        "is still finite (the half-game prior sees to that) but the NUMBER "
        "is an artefact of that prior against the task count: the same sweep "
        "fits "
        "1139.8 over two tasks and 1520.6 over two hundred. A band of zero "
        "width around it would be false exactness twice over, and a rate's "
        "Wilson correction cannot be borrowed here -- rate maps to rating "
        "unambiguously for two arms only, and past two a rating is a function "
        "of the whole comparison graph. Read the widened win rate above it"
    )
    for generation in _deterministic(summary["elo"]):
        ratings = summary["elo"][generation]
        print(f"  {_generation_label(generation)}")
        intervals = summary["elo_ci95"][generation]
        voted = summary["elo_voted"][generation]
        voted_intervals = summary["elo_voted_ci95"][generation]
        for model, rating in sorted(
            ratings.items(), key=lambda item: (-item[1], item[0])
        ):
            voted_rating = voted.get(model)
            # An arm can be missing from the voted-only fit entirely -- every
            # comparison it played was gate-decided. A dash rather than a
            # number, because any number here would be a rating for an arm no
            # judge ever voted on -- and no band, for the same reason.
            if voted_rating is None:
                said = f"{'--':>8}"
            else:
                said = (
                    f"{voted_rating:8.1f}  "
                    f"{_rating_band(voted_intervals[model])}"
                )
            print(
                f"    {model:24} {rating:8.1f}  "
                f"{_rating_band(intervals[model])}  "
                f"judge-voted only {said}".rstrip()
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
    if summary["superseded_votes"]:
        # THE OTHER DIRECTION, and the louder one: these are paid calls
        # missing from a voted denominator. Reported in lines rather than in
        # comparisons for exactly that reason.
        print(
            f"\n{summary['superseded_votes']} vote line(s) were SUPERSEDED by "
            "a gate-decided line gated on a NEWER grade generation and left "
            "out of the numbers above. The cause is a re-grade that flipped a "
            "passing side to failed: a pair the judge had voted on is one the "
            "ladder now settles, and those votes were bought against a grade "
            "that no longer stands."
        )
    for reason, count in sorted(summary["dropped"].items()):
        if count:
            print(
                f"\n{count} line(s) or comparison(s) could not be aggregated "
                f"({reason}) and are absent from every number above."
            )


def _print_kappa_caveat() -> None:
    """The one line in this file that no argument, flag or branch can suppress.

    `KAPPA_CAVEAT` carries a κ and an em dash, and a stdout the environment
    pinned to ASCII cannot render either -- which used to drop the caveat and
    take the exit path down with it. That is the failure this line exists to
    prevent, reached through an environment variable rather than a flag, so the
    fallback says the same sentence in letters every terminal has.

    ASKED, then caught, and the order matters since `main` reconfigures stdout
    to `backslashreplace` before the summary. Under that policy the `print`
    below no longer RAISES -- it succeeds and emits
    `\\u03ba unmeasured (OPEN-5) \\u2014 ...`, so an exception-only fallback
    would leave the twin unreachable and hand the ASCII terminal the one
    sentence in this file that has to be read rather than grepped, in escapes.
    Escaping is the right answer for an id and the wrong one for a claim (see
    `_print_ascii_safe`), so the encodability of the stream is asked FIRST and
    the twin is chosen deliberately.

    The `except` stays as the backstop for every stream that is not
    reconfigured -- a direct library caller, and the pre-summary prints.
    """
    if not _encodable(KAPPA_CAVEAT):
        print(f"\n{_KAPPA_CAVEAT_ASCII}")
        return
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
             f"oracle (default: {JUDGE_MODEL_ID_DEFAULT}). A `codex:<model>` "
             "id selects the Codex-CLI backend instead of the mantle "
             "endpoint, and needs BAKEOFF_CODEX_HOME pointing at a home "
             "holding only the attested seat's auth.json. There is no "
             "separate backend flag: the id IS the identity every resume key "
             "and every generation partition carries, so the two backends can "
             "never be pooled into one number",
    )
    parser.add_argument(
        "--concurrency", type=int, default=1, metavar="N",
        help="how many paid calls may be in flight at once (default: 1). "
             "Calls run concurrently; lines are still COMMITTED in worklist "
             "order, so the judgments file, the resume and the "
             "consecutive-error breaker all behave exactly as they do at 1. "
             "Committing in order costs a buffer: N calls RUN at once, but "
             "up to 2N may be paid for and not yet committed, and an abort "
             "or a Ctrl-C discards every one of those unwritten. "
             "The default is 1 because a seat's rate window is unmeasured "
             "until a pass has run against it -- raise it once a short pass "
             "has shown the per-call latency and whether 429s appear",
    )
    parser.add_argument(
        "--reasoning-effort", default=None, metavar="LEVEL",
        choices=("minimal", "low", "medium", "high"),
        help="codex backend only: the ONE sampling knob it has, recorded into "
             "judge_sampling as sent. There is no temperature -- `codex exec` "
             "cannot send one -- so a codex pass is not exactly reproducible "
             "the way a mantle pass at temperature 0 is. The choices are the "
             "values the backend sends",
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
    if args.concurrency < 1:
        # `parser.error` and not a raise, for `--max-consecutive-errors`'
        # reason: a usage mistake deserves the usage line and exit 2, not a
        # traceback out of the middle of a batch that already read the
        # collection.
        parser.error(
            "--concurrency must be at least 1: below one there is no walk at "
            "all, and 1 is the sequential pass"
        )
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
    # Both of these are DROPPED-CONFIGURATION shapes, which is the class of
    # failure this repo refuses on sight: the pass runs, every line parses, and
    # the file says nothing about the thing that was ignored. `judge_event_log`
    # raises on both as well -- these two are the operator-facing half, and
    # they fire before the task set is read and long before a credential is
    # minted, so the cost of the mistake is the usage line rather than a
    # collection.
    lowered = args.judge_model.lower()
    if lowered.startswith(CODEX_MODEL_PREFIX) and not is_codex_judge(
        args.judge_model
    ):
        parser.error(
            f"{args.judge_model!r} spells the codex namespace with the wrong "
            "case: the prefix is 'codex:' exactly. Refused rather than "
            "normalised, because the id goes onto every line verbatim and "
            "two spellings would be two generations of one judge"
        )
    if args.reasoning_effort is not None and not is_codex_judge(
        args.judge_model
    ):
        parser.error(
            "--reasoning-effort is the codex backend's knob and the mantle "
            "path cannot send it; with a mantle judge id the flag would be "
            "silently dropped, which is worse than this refusal"
        )

    event_log_root = Path(args.event_log)
    try:
        tasks = load_task_set(Path(args.taskset))
    except TaskError as exc:
        # Through the guard for the same reason the header below is: the
        # message names the path the operator passed, and a bad `--taskset` is
        # exactly how this branch is reached.
        _print_ascii_safe(f"task set: {exc}")
        return 1

    # THROUGH THE GUARD, and every line of it. Four of these five carry a
    # path -- the two the operator supplied on the command line
    # (`--event-log`, `--taskset`) and the two derived from the first -- and a
    # path is the one thing here that can hold a character `str.encode`
    # refuses: a collection under an accented directory name is ordinary, not
    # exotic. The fifth is guarded with them rather than left bare, since a
    # header that survives in four lines and dies in the fifth is a worse
    # thing to reason about than one rule. They also print BEFORE the
    # stream reconfigure further down, which is what covers the report, so on
    # an ASCII stdout (`LC_ALL=C`, or a pipe into a tool that pinned it) a bare
    # `print` here raises `UnicodeEncodeError` out of `main` before the
    # collection is read and before anything is spent. Cheap to survive, and a
    # traceback where the header belongs tells the operator nothing about the
    # path that caused it -- `backslashreplace` at least prints it.
    _print_ascii_safe(f"event log  {event_log_root}")
    _print_ascii_safe(f"grades     {grades_path(event_log_root)}")
    _print_ascii_safe(f"judgments  {judgments_path(event_log_root)}")
    _print_ascii_safe(f"task set   {args.taskset}  ({len(tasks)} task(s))")
    _print_ascii_safe(
        f"judge      {args.judge_model}, prompt v{JUDGE_PROMPT_VERSION}, "
        f"rubric {RUBRIC_VERSION}"
    )
    # The backend is printed even though the id already implies it, because
    # the implication runs the wrong way for a reader: `codex:` is easy to
    # mistype as part of a model name, and an operator who meant the seat and
    # got the mantle endpoint would find out from the bill rather than from
    # the header. It prints before anything is read or spent.
    _print_ascii_safe(f"backend    {_judge_backend(args.judge_model)}")

    try:
        result = judge_event_log(
            event_log_root, tasks,
            judge_model_id=args.judge_model,
            only_tasks=args.only_task,
            sample_indices=args.samples,
            rubric=not args.no_rubric,
            re_judge=args.re_judge,
            max_consecutive_errors=args.max_consecutive_errors,
            reasoning_effort=args.reasoning_effort,
            concurrency=args.concurrency,
        )
    # Three refusals, one exit path. All three mean the batch never ran, all
    # three print no numbers, and none is something a flag can talk past -- so
    # the operator gets the sentence and exit 1 rather than a traceback out of
    # a driver they pointed at a typo or aimed at a judge from a compared
    # family. `NonNeutralJudge` is the one whose message carries a `§`, which
    # is why the print goes through the encoding guard: a refusal an ASCII
    # terminal turns into a traceback is a refusal nobody reads.
    except (ResumeRefused, CollectionNotFound, NonNeutralJudge) as exc:
        _print_ascii_safe(f"\nREFUSED: {exc}")
        return 1

    # STREAM-LEVEL, AND AT THE CLI ENTRYPOINT ONLY. This supersedes the OPEN
    # left at Task 5: `_print_reading` prints two `§` legends through bare
    # `print`s (the interval legend and the 40% pairwise threshold), so on a
    # stdout the environment pinned to ASCII -- `LC_ALL=C`, or a pipe into a
    # tool that did -- the report died partway through, AFTER a pass that had
    # already spent its money and fsynced every line. Guarding those sites one
    # by one was rejected as too wide (~40 prints, and every future one would
    # have to remember); setting the STREAM's error handler makes every report
    # line, current and future, degrade into readable escapes instead of
    # raising.
    #
    # `main` and not `print_summary`, because the two have different contracts:
    # the CLI must never die on encoding, while a library caller of
    # `print_summary` keeps today's semantics and its own choice about the
    # stream it owns.
    #
    # Guarded with `getattr` -- pytest's capture object and other exotic
    # streams need not expose `reconfigure`, and a missing one is skipped
    # silently: the driver's own prints still carry `_print_ascii_safe`, which
    # is also what covers everything printed BEFORE this line (the header
    # above, the per-unit progress lines, the non-neutral banner) since the
    # walk has already run by the time we get here.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")

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
