#!/usr/bin/env python3
"""Grade a stored collection: one line per record, beside the log it never
touches. See `docs/superpowers/specs/2026-08-17-offline-grader-design.md`.

The harness does not grade (spec section 6.1), so every `RunRecord` in the
event log carries `tests_passed: None` forever. This is the batch that answers
the question offline, over the stored final diffs, in each task's own pinned
image -- and it writes to `<event-log>/grades/grades.jsonl`, never into the
event log itself. The log is the immutable input; the grade file is a derived
view that can be thrown away and recomputed, which is exactly why the grader is
allowed to be re-run and the collection is not.

THE EXIT CONTRACT, which is what most of this file is for
---------------------------------------------------------

Exit 0 means every selected record produced a line. Exit 1 means at least one
did not. That contract is one uncaught exception away from being false in the
worst direction: a task whose image will not build, an oracle that cannot be
derived, a container that dies inside preflight, a `schema_at_least` raise over
a hand-edited version string -- each of them, uncaught, ends the batch with half
the collection ungraded and no line saying which half. So:

* the per-task setup region -- `build_task_image`, the ENTRYPOINT rejection,
  `materialize`, `preflight()` and `ensure_oracle`, two of which run containers
  through ~20 execs each -- is wrapped in a bare `except Exception`. A
  named-tuple catch re-arms the kill-the-batch door for every type nobody
  listed, and docker's own exceptions are not in anyone's list. `OracleError`
  is pulled out first, because in the generic bucket a broken oracle is
  indistinguishable from a grader bug;
* every record of a task whose setup failed gets a line naming the failure
  (`TASK_SETUP_FAILED` / `ORACLE_FAILED` / `PREFLIGHT_FAILED` /
  `SCOPE_COLLECTED_NOTHING`), with the exception type and message behind it;
* a per-record failure is counted in `errors` and the batch continues.

WHAT IS RECORDED RATHER THAN REFUSED
------------------------------------

An image digest that differs from the one the run used. `run_matrix` refuses a
mismatch because it is about to spend tokens; the grader spends nothing, the
task-image build is non-hermetic, and off the collecting machine a rebuilt
digest differs almost surely -- so a refusal would become routine
`--allow-mixed-images` noise. `graded_in_image` and `image_matches_run` are on
every line and the false-count is bannered.

WHAT IS READ-ONLY
-----------------

`run_matrix`'s `cache/preflight.json`. Its verdicts are served (the key carries
`PREFLIGHT_VERSION`, so a verdict from an older gate misses) and never written:
that driver's own `--force-preflight` path seeds `{}` and writes the whole file
back, so a grader entry in there would erase every other task's verdict. The
grader's verdicts go to `cache/preflight-grade.json`.

Usage:
    python scripts/grade.py --event-log ~/.cache/bakeoff/eventlog
    python scripts/grade.py --event-log PATH --only <run_id> --re-grade

Exit codes:
    0  every selected record produced a line
    1  at least one record errored, or the resume was refused
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bakeoff.eventlog import EventLog  # noqa: E402
from bakeoff.grade_schema import (  # noqa: E402
    CHECK_ORDER,
    MIN_GRADABLE_SCHEMA,
    CheckResult,
    GradeRecord,
    NotGradedReason,
    append_grade,
    # Defined beside the record they name rather than here, and re-imported so
    # `scripts.grade.grades_path` stays the name every existing caller reads.
    # `scripts/judge.py` needs the first of these and nothing else in this
    # module: importing this file for it dragged `bakeoff.images` -> `docker`
    # into a driver that judges offline, which is a Docker package standing
    # between an operator and `--help`.
    artifacts_root,
    grades_path,
    load_grades,
    schema_at_least,
)
from bakeoff.grader import (  # noqa: E402
    GRADER_VERSION,
    LadderResult,
    build_grade_record,
    grade_run,
    not_graded_gate,
)
from bakeoff.images import (  # noqa: E402
    build_base_images,
    build_task_image,
    image_entrypoint,
)
from bakeoff.oracle import Oracle, OracleError, ensure_oracle  # noqa: E402
from bakeoff.preflight import (  # noqa: E402
    PREFLIGHT_VERSION,
    SCOPE_COLLECTS_NOTHING,
    PreflightResult,
    preflight,
    # Imported, never restated: two copies of a cache key is how a verdict
    # written under one gate gets served to another.
    preflight_cache_key,
)
from bakeoff.runner import harness_commit  # noqa: E402
from bakeoff.tasks import TaskError, load_task_set, materialize  # noqa: E402

# Under $HOME for the reason `run_matrix` documents: the Docker VM on macOS
# mounts $HOME only, and a directory bind-mounted from elsewhere appears inside
# the container as a silently EMPTY one -- which `grader._scan_saw_input` was
# written after measuring on the gitleaks mount.
CACHE = Path.home() / ".cache" / "bakeoff"
DEFAULT_TASK_SET = REPO / "taskset"

#: `run_matrix`'s cache, read for hits and never written. See the module
#: docstring.
DRIVER_PREFLIGHT_CACHE = "preflight.json"
#: The grader's own, which is the only one it writes.
GRADE_PREFLIGHT_CACHE = "preflight-grade.json"


class ResumeRefused(RuntimeError):
    """The grade file could not be read completely, so a resume keyed on
    "which runs are already graded" cannot be trusted.

    A malformed line is an unreadable grade, and a resume that mistook one for
    an ABSENT grade would re-grade a run that already has a line -- leaving two
    verdicts for one run under one grader version, which is the shape the file
    reserves for a deliberate re-grade.
    """


class VerdictInvariantError(RuntimeError):
    """`resolved is None` if and only if `not_graded_reason is not None`.

    A violation is a DRIVER bug, and the file is append-only: a line written
    once is permanent. `resolved is False` beside a reason is an accusation
    stamped on a run the grader refused to look at; `resolved is None` without
    one is a verdict nothing can interpret.
    """


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
#
# `grades_path` and `artifacts_root` live in `bakeoff.grade_schema` now and are
# imported above. Moved rather than copied: two definitions of where the grade
# file lives is how a moved grade file turns into a judging pass that reports
# "nothing is graded" and judges nothing.


# ---------------------------------------------------------------------------
# the per-task setup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSetup:
    """Everything grading a record of this task needs that is not in the
    record -- or the reason there is nothing.

    `refusal` and `oracle` are the two states: a task that preflighted carries
    an oracle, a task that did not carries the `(reason, detail)` every one of
    its records gets. `preflight_version` is kept even on the refusal path,
    because a NO-GO is still a verdict some gate made and the line has to name
    which one.
    """

    image: str = ""
    preflight_version: str = ""
    oracle: Oracle | None = None
    refusal: tuple[NotGradedReason, str] | None = None


def _cache_file(cache: Path, name: str) -> dict:
    path = Path(cache) / name
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        # A miss, never a raise. `ensure_oracle`'s posture: raising leaves the
        # damaged file on disk and the task ungradable until an operator
        # deletes it by hand, and re-earning a verdict is a preflight run.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def preflight_cached(task, image: str, start_sha: str, cache: Path) -> bool:
    """Has THIS gate already passed this task, in this image, at this tree?

    Both caches are consulted -- `run_matrix`'s and the grader's -- because a
    collection that has just been run has already paid for the verdict, and
    re-validating 80 tasks per grading pass is how a gate becomes one nobody
    runs. Only the grader's is written.

    The key is `preflight.preflight_cache_key`, imported: it carries
    `PREFLIGHT_VERSION`, so a verdict written by an older gate misses and the
    assertions added since are not inert on exactly the tasks about to be
    graded.
    """
    key = preflight_cache_key(task, image, start_sha)
    return any(
        _cache_file(cache, name).get(task.task_id, {}).get("key") == key
        for name in (DRIVER_PREFLIGHT_CACHE, GRADE_PREFLIGHT_CACHE)
    )


def record_preflight_pass(task, image: str, start_sha: str,
                          cache: Path) -> None:
    """Store a PASS in the grader's own cache. Never the driver's.

    Written to a temp file and `os.replace`d, which is atomic on POSIX. The
    file holds EVERY task's verdict and is rewritten whole on each pass, so a
    batch killed mid-write does not lose one entry -- it loses the file, and
    `_cache_file` then reads the truncation as a miss and every task in the
    collection re-preflights. That is a real cost (a preflight per task) paid
    for a signal that was never damaged.
    """
    path = Path(cache) / GRADE_PREFLIGHT_CACHE
    stored = _cache_file(cache, GRADE_PREFLIGHT_CACHE)
    stored[task.task_id] = {"key": preflight_cache_key(task, image, start_sha)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def preflight_refusal(result: PreflightResult) -> tuple[NotGradedReason, str]:
    """Which refusal a NO-GO earns, off the TYPED `problem_codes`.

    A mis-scoped task and a task that fails its red-before assertion are
    different author errors with different remedies, so the mis-scoped one is
    named rather than genericized. Branching on the code and not on a prose
    prefix is the whole reason `problem_codes` exists -- `problems` is for a
    human, and a string-prefix match over it is a test nothing can really
    guard.
    """
    reason = (
        NotGradedReason.SCOPE_COLLECTED_NOTHING
        if SCOPE_COLLECTS_NOTHING in result.problem_codes
        else NotGradedReason.PREFLIGHT_FAILED
    )
    return reason, "; ".join(result.problems)


def resolve_task(task, cache: Path, base_image: str,
                 force_preflight: bool = False) -> TaskSetup:
    """Build, materialize, gate and derive -- the Docker-touching half.

    Raises. Every exception out of this function is the caller's to map onto a
    `NotGradedReason`, and the caller catches `Exception` on purpose: the four
    calls below reach `ImageError`, `TaskError`, `ContainerError` and whatever
    the docker library raises when the daemon hiccups mid-exec.

    The preflight tree is removed on the way out, including on the failure
    paths -- `preflight` leaves a tree that has had the reference fix applied
    and restored, and `grade_run` materializes its own anyway.
    """
    image = build_task_image(task, base_image, Path(cache) / "build", cache)
    entrypoint = image_entrypoint(image)
    if entrypoint:
        # Joins the setup bucket rather than being a special case: an inherited
        # ENTRYPOINT makes `RunContainer`'s `sleep infinity` an argument to it
        # and the container exits immediately, which is a task-setup failure
        # whatever `run_matrix` calls it.
        raise TaskError(
            f"the task image declares ENTRYPOINT {entrypoint!r}; "
            "RunContainer's `sleep infinity` would become an argument to it "
            "and the container would exit immediately"
        )

    work = Path(cache) / "grade-preflight-tree" / task.task_id
    shutil.rmtree(work, ignore_errors=True)
    try:
        start_sha = materialize(task, work / "repo", Path(cache))

        if not force_preflight and preflight_cached(task, image, start_sha,
                                                    cache):
            # The module constant, and it is sound because the version is IN
            # the key: a hit can only have been written by this gate.
            version = PREFLIGHT_VERSION
        else:
            result = preflight(task, image=image, repo_path=work / "repo",
                               start_sha=start_sha)
            if not result.ok:
                reason, detail = preflight_refusal(result)
                return TaskSetup(image=image,
                                 preflight_version=result.preflight_version,
                                 refusal=(reason, detail))
            record_preflight_pass(task, image, start_sha, cache)
            version = result.preflight_version

        return TaskSetup(
            image=image,
            preflight_version=version,
            # After preflight passes, never before: the quarantine is derived
            # at the reference state, and a task that cannot be shown to
            # discriminate has no reference state worth deriving at.
            oracle=ensure_oracle(task, image, Path(cache)),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def task_resolver(cache: Path, force_preflight: bool = False) -> Callable:
    """`resolve_env` for the real thing: one base image per Python version.

    A closure rather than a parameter, so `grade_event_log`'s seam is the
    one-argument `(task) -> TaskSetup` every test can fake, and so the base
    image is not built at all by a batch whose records are all gated. The
    cache is keyed by `task.image.python`: one build per distinct version, and
    still lazy, so that property survives the broadening.

    This driver deliberately does NOT call `run_matrix.assert_one_agent`. It
    is offline and per record, it reads the preflight cache `run_matrix`
    already wrote, and the invocation that spent the money is where a
    cross-task claim about the agent has to hold -- refusing here would refuse
    to grade a collection that is already paid for. `resolve_task`'s own
    `base_image` parameter is unchanged: it takes one image, and the caller
    decides which.
    """
    built: dict[str, str] = {}

    def resolve(task) -> TaskSetup:
        version = task.image.python
        if version not in built:
            built.update(build_base_images(REPO, [version]))
        return resolve_task(task, cache, built[version], force_preflight)

    return resolve


# ---------------------------------------------------------------------------
# driver-level refusals
# ---------------------------------------------------------------------------


def _skipped_ladder(reason: NotGradedReason, detail: str) -> LadderResult:
    """A ladder that ran no check, because the driver answered first.

    Every check is `skipped` rather than absent, so a driver-level line has the
    same nine-name shape as one the ladder produced -- a reader of the grade
    file does not have to know which authority refused to be able to read the
    row.
    """
    return LadderResult(
        checks=tuple(
            CheckResult(name=name, status="skipped") for name in CHECK_ORDER
        ),
        resolved=None,
        not_graded_reason=reason.value,
        not_graded_detail=detail,
    )


def _refused(record, task, image: str, reason: NotGradedReason, detail: str,
             preflight_version: str = "") -> GradeRecord:
    """A not-graded line, assembled through the SAME `build_grade_record` a
    verdict goes through.

    `task` may be `None` (`TASK_NOT_FOUND`), which the assembly already handles
    -- it reads the manifest through `getattr` with defaults. The alternative,
    a hand-built `GradeRecord` on this path, is a second place for the
    provenance fields to be forgotten.
    """
    grade = build_grade_record(record, task, image, None,
                               _skipped_ladder(reason, detail))
    # `""` when no preflight was consulted at all: the module constant would
    # name a gate that never ran on this task.
    return replace(grade, graded_under_preflight_version=preflight_version)


def _assert_verdict_invariant(grade: GradeRecord) -> None:
    if (grade.resolved is None) != (grade.not_graded_reason is not None):
        raise VerdictInvariantError(
            f"{grade.run_id}: resolved={grade.resolved!r} beside "
            f"not_graded_reason={grade.not_graded_reason!r}; the two move "
            "together and neither is inferable from the other's absence"
        )


def _schema_refusal(record) -> str | None:
    """Why this record's schema version is not gradable, or `None`.

    `schema_at_least` RAISES on a version that is not dotted integers, and the
    raise is deliberate: an unparsable version is not evidence about age, so
    the comparison declines to answer rather than answering "too old". The
    driver still has to put the record somewhere, and the closed
    `NotGradedReason` set has no member for "unreadable version" -- so the
    reason is `RECORD_SCHEMA_TOO_OLD` and the DETAIL says the comparison could
    not be made, which is the honest half of the claim. Contained per record:
    uncaught, one hand-edited field takes the whole batch down.
    """
    try:
        if schema_at_least(record.schema_version, MIN_GRADABLE_SCHEMA):
            return None
        return (
            f"the record's schema {record.schema_version} is below the "
            f"gradable floor {MIN_GRADABLE_SCHEMA}, so its fields do not mean "
            "what the ladder was written to assume"
        )
    except ValueError as exc:
        return (
            f"the record's schema {record.schema_version!r} could not be "
            f"compared against the floor {MIN_GRADABLE_SCHEMA}: {exc}. An "
            "unparsable version is not evidence about age, so this is a "
            "refusal to compare rather than a claim that the record is old"
        )


# ---------------------------------------------------------------------------
# the batch
# ---------------------------------------------------------------------------


def _grade_one(record, task, setups: dict, resolve_env: Callable, cache: Path,
               artifacts: Path, grade_one: Callable) -> GradeRecord:
    """One record, from the driver's own gates through to the ladder.

    THE ORDER IS THE CONTENT. Input problems first (is there a task, is it the
    same task, can the record be read at all), then `not_graded_gate` -- is
    there a submission -- and only then the per-task setup.

    The gate sits ABOVE the setup for two reasons, and the first is not an
    optimisation. `EXCLUDED` is a RECORD-level refusal and `TASK_SETUP_FAILED`
    is a TASK-level one, and the design's section 4.2.2 pairing constraint
    branches on exactly that distinction: a record-level refusal breaks one
    arm's pair, a task-level one drops the task for every arm. Resolving the
    setup first meant an excluded record in a task whose image would not build
    was written `task_setup_failed` -- the wrong authority, and the row would
    be dropped from every arm instead of one.

    The second is cost: a task whose records are all gated used to pay an image
    build, a preflight and two full suite runs for the oracle, to produce
    refusals that never look at any of it.
    """
    if task is None:
        return _refused(
            record, None, "", NotGradedReason.TASK_NOT_FOUND,
            f"task_id {record.task_id!r} is not in the loaded task set",
        )
    if record.task_version != task.task_version:
        return _refused(
            record, task, "", NotGradedReason.TASK_VERSION_MISMATCH,
            f"the record was written under task_version "
            f"{record.task_version}; the loaded manifest declares "
            f"{task.task_version}, so the two describe different tasks under "
            "one task_id",
        )
    schema_problem = _schema_refusal(record)
    if schema_problem is not None:
        return _refused(record, task, "",
                        NotGradedReason.RECORD_SCHEMA_TOO_OLD, schema_problem)

    # Is there a submission at all? `grade_run` asks this again and is complete
    # on its own -- one function, three call sites, because the ORDER is what
    # is load-bearing and a second copy of it is a second thing that can be
    # wrong. Asked here so the answer is not masked by a setup that fails after
    # it, and so a gated record costs no container.
    #
    # `image=""`: nothing was resolved, so `graded_in_image` names nothing and
    # `image_matches_run` is `None` rather than a mismatch against the empty
    # string. `oracle=None` follows from never reaching the setup, which is
    # what makes the line carry `quarantined: null` -- `()` is the
    # healthy-suite derivation and the two must not collapse.
    gate = not_graded_gate(record)
    if gate is not None:
        reason, detail = gate
        return _refused(record, task, "", reason, detail)

    if task.task_id not in setups:
        setups[task.task_id] = _resolve(task, resolve_env)
    setup = setups[task.task_id]
    if setup.refusal is not None:
        reason, detail = setup.refusal
        return _refused(record, task, setup.image, reason, detail,
                        setup.preflight_version)

    grade = grade_one(record, task, setup.image, setup.oracle, cache, artifacts)
    return replace(grade, graded_under_preflight_version=setup.preflight_version)


def _resolve(task, resolve_env: Callable) -> TaskSetup:
    """The per-task setup, and the `except Exception` the exit contract needs.

    `OracleError` first: it is the loudest failure `oracle.py` can raise -- a
    reference state that is red in both runs, a quarantine that swallows the
    declared p2p list -- and in the generic bucket a broken oracle is
    indistinguishable from a grader bug. `PREFLIGHT_FAILED` already has exactly
    this shape.
    """
    try:
        return resolve_env(task)
    except OracleError as exc:
        return TaskSetup(refusal=(NotGradedReason.ORACLE_FAILED, str(exc)))
    except Exception as exc:  # noqa: BLE001 - one task, not the batch
        return TaskSetup(
            refusal=(NotGradedReason.TASK_SETUP_FAILED,
                     f"{type(exc).__name__}: {exc}"),
        )


def grade_event_log(event_log_root, tasks, cache, grade_one=grade_run,
                    resolve_env: Callable | None = None,
                    only: Iterable[str] | None = None,
                    re_grade: bool = False) -> dict:
    """Grade every selected record in one event log. Returns the batch.

    `grade_one` and `resolve_env` are the two seams that keep this testable
    without a daemon: the first is the ladder over one run, the second is the
    per-task setup. Both defaults are the real thing.

    There is deliberately NO `force_preflight` parameter. Forcing is the
    RESOLVER's business -- `task_resolver(cache, force_preflight)` closes over
    it -- and a flag here would be silently dead whenever `resolve_env` is
    supplied, which is every test and any caller with its own setup. A
    parameter that is ignored on the path most callers take is worse than one
    that does not exist.

    Runs are walked in `sorted(list_runs())`. `list_runs` globs and glob order
    is nondeterministic; sorted-by-run_id is arbitrary but deterministic, which
    is what a resumable batch needs -- chronology is irrelevant to a derived
    view.
    """
    event_log_root = Path(event_log_root)
    cache = Path(cache)
    if resolve_env is None:
        resolve_env = task_resolver(cache)

    path = grades_path(event_log_root)
    artifacts = artifacts_root(event_log_root)
    existing, malformed = load_grades(path)
    if malformed and not re_grade:
        raise ResumeRefused(
            f"{malformed} malformed line(s) in {path}: a resume is keyed on "
            "which runs are already graded, and an unreadable grade is not an "
            "absent one. Read the file, remove the damage, or pass --re-grade "
            "to grade everything again."
        )

    by_id = {task.task_id: task for task in tasks}
    done = {(g.run_id, g.grader_version) for g in existing}
    commit = harness_commit()
    warnings: list[str] = []
    if malformed:
        # Reached only under `--re-grade`, which skips nothing and so cannot
        # mistake an unreadable grade for an absent one. Said out loud anyway:
        # `existing` is also what the grader_commit banner below is computed
        # from, so a damaged file can silently stop that banner from firing.
        warnings.append(
            f"{malformed} unreadable line(s) in {path} were skipped. "
            "--re-grade makes them harmless to the resume, but the "
            "grader_commit check, the summary and the cross-image banner below "
            "all read the same list, so each is reported over an incomplete "
            "view of what has already been graded."
        )
    prior = {
        g.grader_commit for g in existing
        if g.grader_version == GRADER_VERSION and g.grader_commit
    }
    if prior - {commit}:
        warnings.append(
            f"this resume spans more than one grader_commit under "
            f"grader_version {GRADER_VERSION}: "
            f"{', '.join(sorted(prior | {commit}))}. The version gates the "
            "resume; the commit is the evidence the gate was honest, and a "
            "clean version string over two working trees is what it cannot see."
        )

    log = EventLog(event_log_root)
    graded: list[GradeRecord] = []
    not_graded: list[GradeRecord] = []
    errors: list[str] = []
    skipped: list[str] = []
    setups: dict[str, TaskSetup] = {}
    selected = None if only is None else set(only)

    stored = sorted(log.list_runs())
    if selected is not None and selected - set(stored):
        # A `--only` id that names no record in this log. Not an error -- the
        # exit contract is about records that were SELECTED and exist -- but
        # silence here reads as "graded, nothing to report", and the usual
        # cause is an operator pointing at the wrong event log, where every id
        # is missing and the batch reports a clean zero.
        warnings.append(
            "--only named "
            f"{len(selected - set(stored))} run_id(s) that this event log "
            f"does not hold: {', '.join(sorted(selected - set(stored)))}"
        )

    for run_id in stored:
        if selected is not None and run_id not in selected:
            continue
        if not re_grade and (run_id, GRADER_VERSION) in done:
            skipped.append(run_id)
            continue
        try:
            record = log.read_run(run_id)
            grade = _grade_one(record, by_id.get(record.task_id), setups,
                               resolve_env, cache, artifacts, grade_one)
            _assert_verdict_invariant(grade)
            append_grade(path, grade)
        except Exception as exc:  # noqa: BLE001 - one record, not the batch
            # Loud and counted, never swallowed: this is what makes the exit
            # code 1, and a caller that believed the batch was complete would
            # compute a resolve rate over a collection with a hole in it.
            errors.append(f"{run_id}: {type(exc).__name__}: {exc}")
            continue
        (not_graded if grade.not_graded_reason else graded).append(grade)

    # THE AUDIT IS OVER THE CAMPAIGN, NOT OVER THIS INVOCATION. Both of the
    # things below are answers about a COLLECTION -- how it resolved, and
    # whether any of it was graded somewhere else -- and computing them from
    # `graded + not_graded` made them answers about a slice instead. A resume
    # is the normal way this driver is used (a batch is minutes of container
    # work per row and gets interrupted), so the last invocation of a campaign
    # is routinely the one that grades three rows, prints a summary over three
    # rows, and lets every mismatch accumulated across the first eighty go
    # unbannered. `existing` is already loaded for the resume; it is the same
    # list the `grader_commit` banner above reads.
    audited = _last_line_per_grade(existing + graded + not_graded)
    fresh = {(g.run_id, g.grader_version) for g in graded + not_graded}
    from_prior = sum(
        1 for g in audited if (g.run_id, g.grader_version) not in fresh
    )

    mismatches = sum(1 for g in audited if g.image_matches_run is False)
    if mismatches:
        warnings.append(
            f"{mismatches} grade(s) in {path} ran in an image whose digest "
            "differs from the one the run used. Recorded rather than refused "
            "-- the task image build is not hermetic, so a rebuilt digest "
            "differs almost surely off the collecting machine -- but a "
            "cross-image comparison is a different measurement and "
            "`image_matches_run` is how a reader tells. Counted over the whole "
            "grade file, so a resumed campaign does not lose the ones an "
            "earlier invocation wrote."
        )

    return {
        "graded": graded,
        "not_graded": not_graded,
        "errors": errors,
        "skipped": skipped,
        "warnings": warnings,
        "summary": summarize(audited),
        # How much of the summary this invocation did NOT produce. Printed, so
        # a reader of a three-row resume cannot mistake a campaign-wide summary
        # for one this invocation earned.
        "audited": len(audited),
        "from_prior_invocations": from_prior,
    }


def _last_line_per_grade(grades: list[GradeRecord]) -> list[GradeRecord]:
    """One row per `(run_id, grader_version)` -- the LAST line, in file order.

    The grade file is append-only and a re-grade appends rather than replaces,
    so a run can hold several lines under one version; summing over all of them
    counts one run two or three times and inflates the denominator of every
    rate computed from the summary. The last line is the current verdict, on
    the same reasoning `done` uses to skip: it is the one a later reader of the
    file would land on.

    Keyed on the VERSION too, never on `run_id` alone. Two grader versions
    disagreeing about one run is a finding the file exists to keep, and
    collapsing them would silently discard the older ladder's verdict from the
    audit while its line stays on disk.
    """
    last: dict[tuple[str, str], GradeRecord] = {}
    for grade in grades:
        last[(grade.run_id, grade.grader_version)] = grade
    return list(last.values())


# ---------------------------------------------------------------------------
# the summary -- a printout, not a stored score
# ---------------------------------------------------------------------------


def summarize(grades: list[GradeRecord]) -> dict:
    """Per model: what resolved, what failed where, and what never got looked
    at.

    The environment-error bucket is a per-`environment_error_check` HISTOGRAM
    rather than a count, because the typed field makes it a groupby and because
    one arm's bucket being all `p2p` is plausibly model-caused not-grading -- a
    section 6.4 exclusion under another name, which a single number cannot
    show.
    """
    summary: dict[str, dict] = {}
    for model in sorted({g.model for g in grades}):
        rows = [g for g in grades if g.model == model]
        summary[model] = {
            "graded": sum(1 for g in rows if g.resolved is not None),
            "resolved": sum(1 for g in rows if g.resolved is True),
            "failed": dict(Counter(
                g.grade_failure for g in rows if g.grade_failure
            )),
            "not_graded": dict(Counter(
                g.not_graded_reason for g in rows if g.not_graded_reason
            )),
            "environment_error_check": dict(Counter(
                g.environment_error_check for g in rows
                if g.environment_error_check
            )),
            "image_mismatch": sum(
                1 for g in rows if g.image_matches_run is False
            ),
            # Grades carrying at least one check the task declares no command
            # for. `not_configured` is "this task has no linter"; `skipped` is
            # "the linter was cut short", and folding them would report every
            # task without a typechecker as a truncated ladder.
            "not_configured": sum(
                1 for g in rows
                if any(c.status == "not_configured" for c in g.checks)
            ),
        }
    return summary


def print_summary(result: dict) -> None:
    print(
        f"\n{len(result['graded'])} graded, "
        f"{len(result['not_graded'])} not graded, "
        f"{len(result['errors'])} errored, "
        f"{len(result['skipped'])} already graded"
    )
    # Said before the per-model rows, because the rows are the whole campaign
    # and the line above is this invocation. A resume's last slice grades three
    # rows and prints a summary over eighty; a reader who took the second for
    # the first would read a finished collection off a batch that graded three.
    print(
        f"\nsummary over {result['audited']} grade(s) in the grade file "
        f"({result['from_prior_invocations']} from prior invocation(s)), "
        "deduped to the last line per (run_id, grader_version)"
    )
    for model, row in result["summary"].items():
        print(f"\n{model}")
        print(f"  graded          {row['graded']}")
        print(f"  resolved        {row['resolved']}")
        print(f"  not_configured  {row['not_configured']}")
        print(f"  image_mismatch  {row['image_mismatch']}")
        for label in ("failed", "not_graded", "environment_error_check"):
            for key, count in sorted(row[label].items()):
                print(f"  {label:15} {key}: {count}")

    for warning in result["warnings"]:
        print(f"\nWARNING: {warning}")
    if result["errors"]:
        print(f"\n{len(result['errors'])} record(s) produced NO line:")
        for line in result["errors"]:
            print(f"  - {line}")
        print(
            "\nThese are holes in the derived view, not verdicts. Every one of "
            "them is a grader defect or an input the driver could not read; "
            "the runs themselves are untouched and re-running this command "
            "grades them."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade a stored collection offline: one line per record, "
                    "in <event-log>/grades/grades.jsonl. The event log itself "
                    "is never written.",
    )
    parser.add_argument("--event-log", required=True)
    parser.add_argument("--taskset", default=str(DEFAULT_TASK_SET))
    parser.add_argument("--cache", default=str(CACHE))
    parser.add_argument(
        "--only", nargs="+", metavar="RUN_ID",
        help="grade only these run_ids (default: every run in the log)",
    )
    parser.add_argument(
        "--re-grade", action="store_true",
        help="append a new line for every selected run, even one already "
             "graded under this grader_version",
    )
    parser.add_argument(
        "--force-preflight", action="store_true",
        help="re-run preflight even when a cached PASS matches",
    )
    args = parser.parse_args(argv)

    event_log_root = Path(args.event_log)
    try:
        tasks = load_task_set(Path(args.taskset))
    except TaskError as exc:
        print(f"task set: {exc}")
        return 1

    print(f"event log {event_log_root}")
    print(f"grades    {grades_path(event_log_root)}")
    print(f"task set  {args.taskset}  ({len(tasks)} task(s))")
    print(f"grader    version {GRADER_VERSION}, commit {harness_commit()}")

    try:
        result = grade_event_log(
            event_log_root, tasks, Path(args.cache),
            # Built here rather than passed as a flag: forcing is the
            # resolver's business, and `grade_event_log` takes no parameter it
            # would ignore whenever a caller supplies its own setup.
            resolve_env=task_resolver(Path(args.cache), args.force_preflight),
            only=args.only, re_grade=args.re_grade,
        )
    except ResumeRefused as exc:
        print(f"\nRESUME REFUSED: {exc}")
        return 1

    print_summary(result)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
