"""Task x model x sample, scheduled. See spec sections 5.7 and 5.8.

`smoke_test` loops arms over one hand-built fixture and is a GATE, not a
scheduler: it opens a fresh timestamped event log every invocation, runs
arm-major by default, and treats a run that landed no diff as a failure.
Every one of those is right for a go/no-go gate and wrong for collection.

Four things this module has to get right that the gate does not:

  ORDER. Section 5.7 requires randomised and interleaved execution, and 5.8
  requires the ordering actually used be reported rather than assumed.
  Round-major with a recorded seed gives both: every (task, arm) appears
  once per round, so repeats are separated by a whole round -- the strongest
  separation available -- and order within a round is shuffled reproducibly.

  RESUME. `run_id` is a hash of (task, model, sample, attempt) and
  `write_run` opens mode "x", so a second invocation against the same event
  log raises ImmutabilityError. A matrix that runs for days across many
  invocations cannot dodge that with a fresh root the way the gate does.

  RESUME'S TRAP, which is worse than the thing it solves. `task_version` is
  NOT part of `run_id`. Edit a task -- a changed prompt, a re-cut patch, a
  new pin -- and every existing cell still looks complete, so the matrix
  silently mixes records of the old task with records of the new one under
  one task_id. That is checked, per cell, and refused.

  CONTAINMENT. One cell's exception must not cost the other 2,399, and must
  not be swallowed either.

What this module deliberately does NOT do is grade. A model failing a task
is the data being collected, so `no diff` is reported and never gated on --
only the things that make a record uninterpretable are.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bakeoff.eventlog import EventLog
from bakeoff.runner import TaskSpec, make_run_id
from bakeoff.session import config_problems


@dataclass(frozen=True)
class Cell:
    """One record's worth of work."""

    task_id: str
    model: str
    sample_index: int

    @property
    def label(self) -> str:
        return f"{self.task_id}/{self.model}/{self.sample_index}"


def matrix_order(
    task_ids: list[str], models: list[str], repeats: int, seed: int
) -> list[Cell]:
    """The (task, model, sample) sequence to execute, in order.

    ROUND-MAJOR, shuffled within each round. Section 5.7 asks for randomised
    and interleaved; those pull in different directions and this is what
    satisfies both. A flat shuffle of all cells would randomise the order and
    could still place three repeats of one (task, model) back to back, which
    is the section 5.8 confound -- Sonnet's first run cost 2.9x its third
    purely because the repeats were adjacent inside a 300 s cache TTL. Rounds
    make that impossible by construction rather than by luck.

    Deterministic in `seed`, which the caller records. An ordering nobody can
    reproduce is an ordering nobody can control for, and section 5.7 makes
    execution timestamp a covariate in the analysis.
    """
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    rng = random.Random(seed)
    order: list[Cell] = []
    for sample_index in range(repeats):
        round_cells = [
            Cell(task_id, model, sample_index)
            for task_id in task_ids
            for model in models
        ]
        rng.shuffle(round_cells)
        if order and len(round_cells) > 1:
            # The one place rounds do not separate repeats on their own: the
            # last cell of one round and the first of the next can be the
            # same (task, model). Rotate rather than reshuffle, so the fix
            # stays deterministic and cannot loop.
            previous = order[-1]
            if (round_cells[0].task_id, round_cells[0].model) == (
                previous.task_id,
                previous.model,
            ):
                round_cells.append(round_cells.pop(0))
        order.extend(round_cells)
    return order


def adjacent_repeats(order: list[Cell]) -> list[str]:
    """(task, model) pairs that occupy two consecutive positions.

    Section 5.8: the interleaving "must be verified, not assumed". This is
    the verification, and it is reported whether or not it is empty.
    """
    return [
        f"{a.task_id}/{a.model}"
        for a, b in zip(order, order[1:])
        if (a.task_id, a.model) == (b.task_id, b.model)
    ]


def to_task_spec(task, image: str, start_sha: str) -> TaskSpec:
    """The manifest, narrowed to what one run needs.

    `base_sha` is the START state -- base plus the committed test half -- not
    the manifest's upstream base_sha. The container detaches to this, so the
    submission diff (section 5.6) is taken against a tree that already
    contains the oracle, which is what keeps the test patch out of every
    arm's diff.

    `test_paths` comes from the reference split rather than from a hand
    list: it is what `scan_destructive` treats as a HIGH-severity deletion,
    and a restated copy would drift from the diff that defines the oracle.
    """
    return TaskSpec(
        task_id=task.task_id,
        task_version=task.task_version,
        repo=task.repo_url,
        base_sha=start_sha,
        container_image_digest=image,
        prompt=task.prompt,
        test_paths=list(task.test_files),
        task_set_commit=task.task_set_commit,
    )


@dataclass
class ResumeReport:
    done: list[Cell] = field(default_factory=list)
    todo: list[Cell] = field(default_factory=list)
    stale_partials: list[str] = field(default_factory=list)
    version_conflicts: list[str] = field(default_factory=list)
    image_conflicts: list[str] = field(default_factory=list)
    #: Cells whose stored record is an exclusion -- a valid record that is not
    #: an observation of any model. Reported, never acted on: run_id is
    #: deterministic and nothing increments attempt_number, so these are holes
    #: no re-invocation can fill (TASKS.md P1).
    excluded: list[str] = field(default_factory=list)


def plan_resume(
    order: list[Cell],
    event_log: EventLog,
    versions: dict[str, int],
    images: dict[str, str],
) -> ResumeReport:
    """Split the order into already-written and still-to-run, and object.

    `versions` and `images` map task_id to the manifest's `task_version` and
    to the image about to be used. Both are compared against what the stored
    record actually says, because resume is a claim that an existing record
    describes the same work this invocation would do -- and `run_id` cannot
    carry that claim: it hashes (task, model, sample, attempt) and nothing
    else, so an edited task reuses every id it had before.

    A stale `<run_id>.json.partial` is NAMED, never removed. It poisons its
    cell permanently (write_run opens "x"), so the matrix has to stop -- but
    deleting another process's in-flight write is not this function's call.
    """
    report = ResumeReport()
    for cell in order:
        run_id = make_run_id(cell.task_id, cell.model, cell.sample_index)
        record_path = event_log.runs_dir / f"{run_id}.json"
        partial = event_log.runs_dir / f"{run_id}.json.partial"
        if partial.exists():
            report.stale_partials.append(f"{cell.label} -> {partial}")
            continue
        if not record_path.exists():
            report.todo.append(cell)
            continue
        report.done.append(cell)
        try:
            stored = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            # A torn record is not a reason to re-run a paid cell, and it is
            # not evidence about the task either. It is reported as a version
            # conflict because the honest statement is the same: this cell
            # cannot be shown to describe the work about to be resumed.
            report.version_conflicts.append(
                f"{cell.label}: record exists but could not be read"
            )
            continue
        stored_version = stored.get("task_version")
        if stored_version != versions.get(cell.task_id):
            report.version_conflicts.append(
                f"{cell.label}: stored task_version {stored_version!r} != "
                f"manifest {versions.get(cell.task_id)!r}"
            )
        stored_image = (stored.get("versions") or {}).get("container_image_digest")
        if stored_image and stored_image != images.get(cell.task_id):
            report.image_conflicts.append(
                f"{cell.label}: stored image {str(stored_image)[:19]}... != "
                f"{str(images.get(cell.task_id))[:19]}..."
            )
        # Named, never acted on. The abort streak and the credential deadline
        # bound the damage of an infra incident; neither undoes it. The record
        # is valid and the log is append-only, `run_id` is deterministic, and
        # nothing increments attempt_number -- so this cell is a hole no
        # re-invocation fills, and the only alternative to naming it is
        # grepping the event log by hand.
        stored_exclusion = stored.get("exclusion")
        if stored_exclusion:
            report.excluded.append(
                f"{cell.label}: {stored_exclusion.get('cls')}/"
                f"{stored_exclusion.get('reason_code')}"
            )
    return report


def infra_problems(
    record: Any, wire_entries: int, unattributed: int, config: dict
) -> list[str]:
    """Reasons this record cannot be interpreted -- never reasons it is bad.

    Deliberately NOT `smoke_test.run_problems`. That function is the Phase 0c
    capability criterion and fails a run that landed no diff, which is
    correct for a gate asking whether an arm can drive the loop at all and
    wrong for collection, where a model failing a task is the measurement.

    What is left is the set of failures that make a row unreadable rather
    than negative: a trajectory that did not parse (every derived field is
    zero and must not be read as a quiet run), a run that was excluded, a run
    that completed no API call at all, a dead wire log, lost attribution, a
    run that was not isolated, and the section 5.2 dump -- which is how an
    unmounted settings file surfaces, and that one turns every arm into a
    capability finding.

    The exclusion and turn checks were in `smoke_test.run_problems` all along
    and were dropped when the two functions were split. Dropping "no diff" was
    right; dropping these was not. A credential-failed run parses, logs,
    attributes and isolates perfectly, so without them this returned [] and
    reset the abort streak on every cell it killed.
    """
    problems: list[str] = []
    if record.trajectory_parse_error:
        problems.append(f"trajectory did not parse: {record.trajectory_parse_error}")
    # An exclusion is never a model observation: ExclusionClass carries infra,
    # adapter and task-defect and has no MODEL_FAILURE, by design. A cell
    # excluded for auth or throttling that reset the abort streak is how a dead
    # credential burns a whole arm while the driver reports success.
    if record.exclusion is not None:
        problems.append(
            f"excluded as {record.exclusion.cls.value}/{record.exclusion.reason_code}: "
            "this row is not an observation of the model"
        )
    # The backstop, and the only check here that does not trust litellm's
    # classification. Zero turns means no assistant message parsed, i.e. not one
    # API call completed -- true at 401, at 403, at the 500 an expired bedrock
    # token is disguised as, and at the no-status-at-all that a cooled-down
    # router refusal carries. A model that ran and solved nothing has turns;
    # this row has none.
    if record.turns_used <= 0:
        problems.append(
            "no turns: not one API call completed, so this row measures nothing"
        )
    if wire_entries <= 0:
        problems.append("wire log is empty: section 6.2 capture is dead")
    # THESE THREE ARE WHY CONTAINMENT IS NOT A NET LOSS, and one of them is a
    # regression guard rather than new loudness.
    #
    # Schema 3.8.0 contains the failures in `execute_run`'s finalize phase and
    # in assembly instead of letting them destroy the record. Contained and not
    # reported, that is strictly worse than the crash it replaced: a host that
    # has hit ENOSPC would run every remaining cell to the end, writing records
    # with no stdout, no checkpoints and half a wire log, and the driver would
    # report all of them green. Before 3.8.0 the raise reached run_matrix,
    # became a `stranded` entry, and five in a row stopped the matrix.
    #
    # `wire_log_error` is the regression guard. Until 3.8.0 a dead wire log
    # tripped the check above -- `wire is None` meant the replay was skipped and
    # `wire_entries_seen` was 0. The H1 hoist reads the proxy's own file
    # instead, so that run now has entries and passes; without this clause the
    # abort silently stops firing.
    if getattr(record, "wire_log_error", ""):
        problems.append(
            f"wire log not written: {record.wire_log_error} -- the section 6.2 "
            "artifact is missing or truncated for this run"
        )
    if getattr(record, "finalize_error", ""):
        problems.append(
            f"finalize incomplete: {record.finalize_error} -- an artifact this "
            "row is read against was not captured"
        )
    if getattr(record, "assembly_error", ""):
        problems.append(
            f"record assembled minimally: {record.assembly_error} -- every "
            "derived field is absent rather than zero"
        )
    if unattributed:
        problems.append(
            f"{unattributed} call(s) landed in unattributed.jsonl: attribution lost"
        )
    # Tri-state, and the middle state is not the bad one. `not record.isolated`
    # said "section 5.1 did not hold" for a run whose container never started
    # and where nobody measured -- a violation claim invented from an absence,
    # counted toward the abort streak.
    if record.isolated is False:
        problems.append("isolated=False: section 5.1 did not hold")
    elif record.isolated is None:
        problems.append(
            "isolation could not be measured: the container never started, so "
            "section 5.1 is unverified rather than violated"
        )
    problems.extend(config_problems(config))
    return problems


# Consecutive cells whose record is uninterpretable before the matrix stops.
# The shape this exists for is an expired credential: every remaining cell
# fails the same way, for hours, and the log fills with rows that say nothing
# about any model.
INFRA_ABORT_STREAK = 5
# And the same count PER ARM, which is the one that actually fires.
#
# Section 5.7 requires round-major interleaving, so consecutive cells are
# almost never the same arm -- and the two credentials have independent origins
# (BAKEOFF_MANTLE_TOKEN from .env, SigV4 from the live SSO session), so one
# dead arm is a normal state rather than an exotic one. Simulated on 20 tasks x
# 4 arms x 3 repeats with the reference arm dead: the global counter never
# reaches 5 and all 60 of that arm's cells are lost, every one of them
# permanently, because run_id is deterministic and write_run opens mode "x".
# Lower than the global threshold because it takes ~4x as many cells to reach.
INFRA_ABORT_STREAK_PER_ARM = 3


class StreakTracker:
    """Consecutive uninterpretable cells, globally and per arm.

    Two counters and not one. The global counter catches what kills every arm
    at once -- a dead proxy, a fully expired session -- and reaches its
    threshold in the fewest cells when it applies. The per-arm counter catches
    what the interleaving hides, which is the case the global counter cannot
    see at all.

    Only rows that are UNREADABLE are counted. A model that reliably fails a
    hard task produces records with turns, a parsed trajectory and no
    exclusion, so it can never trip this -- that is the measurement.
    """

    def __init__(self, overall: int = INFRA_ABORT_STREAK,
                 per_arm: int = INFRA_ABORT_STREAK_PER_ARM):
        self.overall = overall
        self.per_arm = per_arm
        self.streak = 0
        self.by_model: dict[str, int] = {}

    def record(self, model: str, bad: bool) -> str:
        """Note one cell's outcome. "" to continue, or why to stop."""
        if not bad:
            self.streak = 0
            self.by_model[model] = 0
            return ""
        self.streak += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        if self.by_model[model] >= self.per_arm:
            return (
                f"{self.by_model[model]} consecutive cells on {model} produced no "
                "usable record. Section 5.7 interleaves the arms, so these were "
                "not adjacent and the overall counter cannot see them -- this is "
                "the shape of one arm's credential dying while the others live."
            )
        if self.streak >= self.overall:
            return (
                f"{self.streak} cells in a row produced no usable record, across "
                "arms. That is the shape of an expired session or a dead proxy, "
                "and the remaining cells would all fail the same way."
            )
        return ""


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
