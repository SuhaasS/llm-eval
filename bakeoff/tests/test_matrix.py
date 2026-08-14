"""Scheduling, resume, and what the driver is allowed to gate on.

The ordering cases are spec sections 5.7 and 5.8. The resume cases are the
part of this design that can lose data quietly: `run_id` hashes (task, model,
sample, attempt) and NOTHING else, so an edited task reuses every id it had
before and a resumed matrix silently mixes two different tasks under one
task_id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakeoff.eventlog import EventLog
from bakeoff.matrix import (
    INFRA_ABORT_STREAK,
    INFRA_ABORT_STREAK_PER_ARM,
    Cell,
    StreakTracker,
    adjacent_repeats,
    infra_problems,
    matrix_order,
    plan_resume,
    to_task_spec,
)
from bakeoff.runner import make_run_id
from bakeoff.schema import Exclusion, ExclusionClass

TASKS = ["task-a", "task-b"]
ARMS = ["sonnet", "gemma", "kimi"]


# --- ordering (spec sections 5.7, 5.8) ---------------------------------------


def test_every_cell_appears_exactly_once():
    order = matrix_order(TASKS, ARMS, repeats=3, seed=1)

    assert len(order) == len(TASKS) * len(ARMS) * 3
    assert len(set((c.task_id, c.model, c.sample_index) for c in order)) == len(order)


def test_repeats_of_one_cell_never_run_back_to_back():
    """Section 5.8's confound, made impossible by construction rather than by
    luck. Back-to-back repeats warm that arm's prompt cache: on the
    2026-08-11 set the three Sonnet runs started 15 and 14 seconds apart and
    the first cost 2.9x the others, which is a fact about the ordering and
    not about the model.

    A flat shuffle of all cells randomises the order and can still place
    three repeats together. Rounds cannot."""
    for seed in range(25):
        order = matrix_order(TASKS, ARMS, repeats=4, seed=seed)
        assert adjacent_repeats(order) == [], f"seed {seed}"


def test_a_single_cell_matrix_still_orders_its_repeats():
    """One task, one arm: rounds cannot separate a cell from itself, and the
    rotation guard must not spin trying. Reported by the caller rather than
    silently accepted -- the same thing print_run_order says for one arm."""
    order = matrix_order(["only"], ["arm"], repeats=3, seed=7)

    assert len(order) == 3
    assert len(adjacent_repeats(order)) == 2


def test_the_order_is_reproducible_from_the_seed():
    """Section 5.7 makes execution timestamp a covariate in the analysis. An
    ordering nobody can reproduce is an ordering nobody can control for."""
    assert matrix_order(TASKS, ARMS, 3, seed=42) == matrix_order(TASKS, ARMS, 3, 42)
    assert matrix_order(TASKS, ARMS, 3, seed=42) != matrix_order(TASKS, ARMS, 3, 43)


def test_zero_repeats_is_refused():
    with pytest.raises(ValueError):
        matrix_order(TASKS, ARMS, repeats=0, seed=1)


# --- resume ------------------------------------------------------------------


class _Rec:
    """The two fields plan_resume reads off a stored record."""

    def __init__(self, run_id, task_id, task_version, image):
        self.payload = {
            "run_id": run_id,
            "task_id": task_id,
            "task_version": task_version,
            "versions": {"container_image_digest": image},
        }


def _store(log: EventLog, cell: Cell, task_version: int, image: str) -> str:
    run_id = make_run_id(cell.task_id, cell.model, cell.sample_index)
    (log.runs_dir / f"{run_id}.json").write_text(
        json.dumps(_Rec(run_id, cell.task_id, task_version, image).payload)
    )
    return run_id


def test_a_written_cell_is_skipped_and_the_rest_still_run(tmp_path):
    """Resume is mandatory rather than convenient: `write_run` opens mode "x"
    and `run_id` is deterministic, so a second invocation against the same
    event log raises ImmutabilityError. The smoke gate dodges that with a
    fresh root per invocation; a matrix that runs for days cannot."""
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a", "b"], repeats=1, seed=1)
    _store(log, order[0], 1, "img")

    report = plan_resume(order, log, versions={"t": 1}, images={"t": "img"})

    assert report.done == [order[0]]
    assert report.todo == [order[1]]
    assert not report.version_conflicts


def test_an_edited_task_refuses_to_resume(tmp_path):
    """The trap this design would otherwise walk into.

    `task_version` is NOT part of `run_id`. Change a prompt, re-cut a patch,
    bump a pin -- every existing cell still looks complete, so the matrix
    fills the remaining cells with the NEW task while the stored ones
    describe the OLD one, under a single task_id. Nothing downstream can
    separate them, and the log is append-only."""
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a"], repeats=1, seed=1)
    _store(log, order[0], task_version=1, image="img")

    report = plan_resume(order, log, versions={"t": 2}, images={"t": "img"})

    assert report.version_conflicts
    assert "task_version 1" in report.version_conflicts[0]


def test_a_different_image_is_reported_rather_than_ignored(tmp_path):
    """Section 5.1 pins the image per run and the records already say which
    one they used, so a resumed matrix spanning two is a stated fact rather
    than a hidden one. Reported and not refused outright: images rebuild for
    reasons that have nothing to do with the task, and a resume that can
    never be used is not a resume."""
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a"], repeats=1, seed=1)
    _store(log, order[0], task_version=1, image="old")

    report = plan_resume(order, log, versions={"t": 1}, images={"t": "new"})

    assert report.image_conflicts
    assert not report.version_conflicts


def test_a_stale_partial_write_is_named_not_deleted(tmp_path):
    """A process killed mid-write leaves `<run_id>.json.partial`, and
    `write_run` opens "x" -- so that cell can never be written again, and
    `run_id` is deterministic so the retry raises too. The matrix has to
    stop. It must not clean up: the file may belong to a process that is
    still running."""
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a"], repeats=1, seed=1)
    run_id = make_run_id("t", "a", 0)
    partial = log.runs_dir / f"{run_id}.json.partial"
    partial.write_text("{}")

    report = plan_resume(order, log, versions={"t": 1}, images={"t": "img"})

    assert report.stale_partials and not report.todo
    assert partial.exists()


def test_an_unreadable_record_blocks_resume_rather_than_re_running_it(tmp_path):
    """A torn record is not evidence about the task, and it is not a reason
    to pay for the cell twice either. Either way the honest statement is the
    same: this cell cannot be shown to describe the work about to resume."""
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a"], repeats=1, seed=1)
    run_id = make_run_id("t", "a", 0)
    (log.runs_dir / f"{run_id}.json").write_text("{not json")

    report = plan_resume(order, log, versions={"t": 1}, images={"t": "img"})

    assert report.version_conflicts
    assert "could not be read" in report.version_conflicts[0]


def test_a_cell_that_recorded_an_infra_failure_is_named_not_re_run(tmp_path):
    """The account of what a credential incident cost. The abort streak and the
    deadline check bound the damage and cannot undo it: the record is valid,
    run_id is deterministic, write_run opens mode "x", and nothing increments
    attempt_number -- so the cell is a hole no re-invocation fills.

    Warned rather than blocked. The record is legitimate and the log is
    append-only; refusing to resume over it would make one expired session
    permanently fatal to a matrix."""
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a"], repeats=1, seed=1)
    run_id = make_run_id("t", "a", 0)
    (log.runs_dir / f"{run_id}.json").write_text(json.dumps({
        "task_version": 1,
        "exclusion": {"cls": "infra_failure", "reason_code": "api_auth",
                      "pre_registered": True},
        "versions": {"container_image_digest": "img"},
    }))

    report = plan_resume(order, log, versions={"t": 1}, images={"t": "img"})

    assert report.excluded == ["t/a/0: infra_failure/api_auth"]
    assert not report.version_conflicts
    assert report.done


def test_a_clean_stored_record_is_not_reported_as_damage(tmp_path):
    log = EventLog(tmp_path / "log")
    order = matrix_order(["t"], ["a"], repeats=1, seed=1)
    run_id = make_run_id("t", "a", 0)
    (log.runs_dir / f"{run_id}.json").write_text(json.dumps({
        "task_version": 1, "exclusion": None,
        "versions": {"container_image_digest": "img"},
    }))

    assert plan_resume(order, log, versions={"t": 1}, images={"t": "img"}).excluded == []


# --- what the driver gates on ------------------------------------------------


class _Record:
    def __init__(self, **kwargs):
        self.trajectory_parse_error = kwargs.get("parse_error", "")
        self.isolated = kwargs.get("isolated", True)
        self.turns_used = kwargs.get("turns", 10)
        self.exclusion = kwargs.get("exclusion", None)
        self.wire_log_error = kwargs.get("wire_log_error", "")
        self.finalize_error = kwargs.get("finalize_error", "")
        self.assembly_error = kwargs.get("assembly_error", "")

        class _Artifacts:
            final_diff = kwargs.get("diff", "")

        self.artifacts = _Artifacts()


GOOD_CONFIG = {"permissionMode": "bypassPermissions", "tools": ["Edit"]}


def test_a_run_that_solved_nothing_is_data_not_a_gate_failure():
    """The line between this and `smoke_test.run_problems`, which fails a run
    that landed no diff. That is right for a go/no-go gate asking whether an
    arm can drive the loop at all, and wrong for collection, where a model
    failing a task IS the measurement being collected."""
    record = _Record(diff="")

    assert infra_problems(record, wire_entries=5, unattributed=0, config=GOOD_CONFIG) == []


@pytest.mark.parametrize(
    ("kwargs", "wire", "unattributed", "config", "match"),
    [
        ({"parse_error": "boom"}, 5, 0, GOOD_CONFIG, "did not parse"),
        ({}, 0, 0, GOOD_CONFIG, "wire log is empty"),
        ({}, 5, 2, GOOD_CONFIG, "attribution lost"),
        ({"isolated": False}, 5, 0, GOOD_CONFIG, "isolated=False"),
        ({}, 5, 0, {"permissionMode": "default", "tools": ["Edit"]}, "did not load"),
        ({}, 5, 0, {}, "no init event"),
    ],
)
def test_an_uninterpretable_record_is_reported(kwargs, wire, unattributed, config, match):
    """Each of these makes a row unreadable rather than negative. The
    settings-file one matters most and looks least important: Claude Code
    does not fail on a `--settings` path that is not there, so the agent
    simply cannot edit anything and all four arms read as capability
    failures."""
    problems = infra_problems(
        _Record(**kwargs), wire_entries=wire, unattributed=unattributed, config=config
    )

    assert any(match in problem for problem in problems), problems


def test_an_excluded_run_is_not_collection_data():
    """ExclusionClass carries infra, adapter and task-defect and has no
    MODEL_FAILURE by design, so an exclusion is by construction a row that says
    nothing about the model. `smoke_test.run_problems` checked this and
    infra_problems dropped it when the two were split."""
    exclusion = Exclusion(
        cls=ExclusionClass.INFRA_FAILURE, reason_code="api_auth", pre_registered=True
    )
    problems = infra_problems(
        _Record(exclusion=exclusion), wire_entries=5, unattributed=0, config=GOOD_CONFIG
    )
    assert any("api_auth" in problem for problem in problems), problems


def test_a_run_with_no_turns_is_reported_however_it_failed():
    """The status-agnostic backstop, and the only check here that does not
    depend on litellm classifying anything correctly. Zero turns means no
    assistant message was ever parsed, i.e. not one API call completed -- true
    at 401, at 403, at the 500 an expired bedrock token is disguised as, and at
    the no-status-at-all a cooled-down router refusal carries."""
    problems = infra_problems(
        _Record(turns=0), wire_entries=5, unattributed=0, config=GOOD_CONFIG
    )
    assert any("no turns" in problem for problem in problems), problems


def test_a_model_that_ran_and_solved_nothing_is_still_data():
    """The line this must not cross. A model that drove the loop for 12 turns
    and submitted nothing IS the measurement; only a row with no completed call
    is unreadable."""
    assert infra_problems(
        _Record(turns=12, diff=""), wire_entries=5, unattributed=0, config=GOOD_CONFIG
    ) == []


def test_unmeasured_isolation_is_not_reported_as_a_violation():
    """`isolated` is None when the container never started, so nobody measured.
    `not record.isolated` reported that as "section 5.1 did not hold" -- a
    violation claim invented from an absence, and it fed the abort streak. Two
    different absences that render identically are the same defect one layer
    down."""
    problems = infra_problems(
        _Record(isolated=None), wire_entries=5, unattributed=0, config=GOOD_CONFIG
    )
    assert problems
    assert not any("did not hold" in problem for problem in problems), problems
    assert any("could not be measured" in problem for problem in problems), problems


def test_a_violated_isolation_is_still_reported():
    problems = infra_problems(
        _Record(isolated=False), wire_entries=5, unattributed=0, config=GOOD_CONFIG
    )
    assert any("did not hold" in problem for problem in problems), problems


# --- the abort streak (spec section 5.7) -------------------------------------


def test_a_dead_arm_aborts_even_though_its_failures_are_never_adjacent():
    """Section 5.7 requires round-major interleaving, and that is exactly what
    defeats a global consecutive-failure counter. The two credentials have
    independent origins -- BAKEOFF_MANTLE_TOKEN from .env, SigV4 from the live
    SSO session -- so one dead arm is a normal state.

    Simulated on 20 tasks x 4 arms x 3 repeats with claude-sonnet-5-runtime
    dead: the global streak NEVER fires and all 60 of its cells are lost. Every
    one is permanently consumed, because run_id is deterministic and write_run
    opens mode "x"."""
    order = matrix_order(
        [f"t{i}" for i in range(20)],
        ["claude-sonnet-5-runtime", "gemma-4-31b", "kimi-k2-5", "nemotron-3-super-120b"],
        repeats=3, seed=20260812,
    )
    tracker = StreakTracker()
    lost = 0
    for cell in order:
        bad = cell.model == "claude-sonnet-5-runtime"
        lost += bad
        if tracker.record(cell.model, bad):
            break
    else:
        pytest.fail("the tracker never aborted on a fully dead arm")

    assert lost <= INFRA_ABORT_STREAK_PER_ARM


def test_the_abort_reason_names_the_arm():
    tracker = StreakTracker()
    reason = ""
    for _ in range(INFRA_ABORT_STREAK_PER_ARM):
        reason = tracker.record("gemma-4-31b", bad=True)
    assert "gemma-4-31b" in reason


def test_a_good_cell_clears_that_arms_streak():
    """Only a run of unreadable rows means something. An isolated bad cell
    among good ones is noise, and aborting on it would end a multi-day matrix
    over one throttle."""
    tracker = StreakTracker()
    for _ in range(INFRA_ABORT_STREAK_PER_ARM - 1):
        assert tracker.record("gemma-4-31b", bad=True) == ""
    assert tracker.record("gemma-4-31b", bad=False) == ""
    for _ in range(INFRA_ABORT_STREAK_PER_ARM - 1):
        assert tracker.record("gemma-4-31b", bad=True) == ""


def test_the_global_streak_still_catches_a_failure_that_spans_arms():
    """A dead proxy or a fully expired session kills every arm at once, and
    that must abort sooner than the per-arm rule alone would."""
    tracker = StreakTracker()
    arms = ["a", "b", "c", "d", "e"]
    reasons = [tracker.record(arm, bad=True) for arm in arms[:INFRA_ABORT_STREAK]]
    assert reasons[-1]
    assert "in a row" in reasons[-1]


def test_one_arm_failing_does_not_arm_another_arms_counter():
    tracker = StreakTracker()
    for _ in range(INFRA_ABORT_STREAK_PER_ARM - 1):
        tracker.record("gemma-4-31b", bad=True)
    assert tracker.record("kimi-k2-5", bad=True) == ""


# --- the manifest, narrowed --------------------------------------------------


class _Task:
    task_id = "t"
    task_version = 3
    repo_url = "https://example.invalid/r.git"
    base_sha = "b" * 40
    prompt = "do it"
    test_files = ("tests/test_x.py",)
    task_set_commit = "c" * 40


def test_the_spec_carries_the_start_state_not_the_upstream_base():
    """The container detaches to base PLUS the committed test half, so the
    submission diff (section 5.6) is taken against a tree that already holds
    the oracle -- which is what keeps the test patch out of every arm's diff
    and lets `restore_paths` restore the patched tests instead of deleting
    them."""
    spec = to_task_spec(_Task(), image="sha256:img", start_sha="s" * 40)

    assert spec.base_sha == "s" * 40
    assert spec.base_sha != _Task.base_sha
    assert spec.test_paths == ["tests/test_x.py"]
    assert spec.task_set_commit == "c" * 40


def test_two_invocations_cannot_share_an_artifacts_path():
    """Records are immutable; what they point at was not. `artifacts = CACHE /
    "artifacts"` was a pure function of the cell and run_cell rmtree'd that path
    before every attempt, so a re-run under a different event log deleted the
    earlier run's artifacts and left its record pointing at the replacement.

    Confirmed across all 10 stored event logs: 9 run_ids, 6 of them in more
    than one log (one in seven), and 12 records whose wire_entries_seen
    disagrees with the file they point at.

    WireLogger's "x" cannot catch it -- the delete precedes the mkdir."""
    from scripts.run_matrix import artifacts_root

    assert artifacts_root(Path("/c"), "A") != artifacts_root(Path("/c"), "B")
    assert "A" in str(artifacts_root(Path("/c"), "A"))


# --- schema 3.8.0: containment has to reach the thing that can stop the spend


def test_a_finalize_failure_makes_the_row_uninterpretable():
    """Otherwise RC1's containment is a NET LOSS.

    Before 3.8.0 a finalize failure raised into run_matrix, became a
    `stranded` entry and counted toward the abort streak. Contained and not
    reported, a host that has hit ENOSPC runs every remaining cell to the end
    writing records with no stdout, no checkpoints and half a wire log --
    every one of them reported green.
    """
    record = _Record(finalize_error="stdout: OSError: No space left on device")

    problems = infra_problems(
        record, wire_entries=5, unattributed=0, config=GOOD_CONFIG
    )

    assert any("finalize incomplete" in p for p in problems)


def test_a_minimal_record_makes_the_row_uninterpretable():
    record = _Record(assembly_error="ValueError: boom")

    problems = infra_problems(
        record, wire_entries=5, unattributed=0, config=GOOD_CONFIG
    )

    assert any("assembled minimally" in p for p in problems)


def test_a_dead_wire_log_still_aborts_after_the_h1_hoist():
    """A REGRESSION GUARD, not new loudness.

    Until 3.8.0 a `wire_log_error` run tripped the `wire_entries <= 0` check
    above -- `wire is None` meant the replay was skipped, so the count was 0.
    The H1 hoist reads the proxy's own file instead, so such a run now has
    entries and passes that check. Without this clause the abort silently
    stops firing on exactly the failure it was written for.
    """
    record = _Record(wire_log_error="File exists")

    problems = infra_problems(
        record, wire_entries=7, unattributed=0, config=GOOD_CONFIG
    )

    assert any("wire log not written" in p for p in problems)


def test_five_finalize_failures_on_one_arm_stop_the_matrix():
    """The streak is what turns a per-row problem into a stop."""
    tracker = StreakTracker()
    record = _Record(finalize_error="stdout: OSError: No space left on device")

    aborts = [
        tracker.record(
            "gemma-4-31b",
            bad=bool(
                infra_problems(
                    record, wire_entries=5, unattributed=0, config=GOOD_CONFIG
                )
            ),
        )
        for _ in range(5)
    ]

    assert aborts[-1], "five uninterpretable rows on one arm must stop the driver"
