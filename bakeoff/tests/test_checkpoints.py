import pytest

from bakeoff.checkpoints import CheckpointRecorder
from bakeoff.container import ContainerError


class FakeContainer:
    """Records calls and returns a synthetic diff per turn."""

    def __init__(self, states=None):
        self.calls = 0
        # `{}` and not `None`: `{}` is the measurement "read, nothing dirty",
        # which is what a real container answers on a tree with no submodule.
        self.states = {} if states is None else states

    def snapshot_diff(self, base_sha):
        self.calls += 1
        return (f"diff-{self.calls}", [f"file{self.calls}.py"])

    def submodule_states(self):
        return self.states


def test_captures_every_turn_when_k_is_one():
    recorder = CheckpointRecorder(FakeContainer(), base_sha="abc", every_k_turns=1)
    for turn in (1, 2, 3):
        assert recorder.maybe_capture(turn, elapsed_ms=turn * 100) is not None
    assert len(recorder.captured) == 3


def test_skips_turns_when_k_is_greater_than_one():
    container = FakeContainer()
    recorder = CheckpointRecorder(container, base_sha="abc", every_k_turns=3)
    results = [recorder.maybe_capture(t, elapsed_ms=0) for t in range(1, 8)]
    captured_turns = [c.turn for c in results if c is not None]
    assert captured_turns == [3, 6]
    assert container.calls == 2


def test_checkpoint_carries_diff_files_and_elapsed():
    recorder = CheckpointRecorder(FakeContainer(), base_sha="abc", every_k_turns=1)
    checkpoint = recorder.maybe_capture(1, elapsed_ms=250)
    assert checkpoint.diff_vs_base == "diff-1"
    assert checkpoint.files_touched == ["file1.py"]
    assert checkpoint.elapsed_ms == 250


def test_tests_pass_is_none_because_grading_is_offline():
    """Spec section 5.5: the agent run only stores diffs. Grading runs
    later as a separate batch job."""
    recorder = CheckpointRecorder(FakeContainer(), base_sha="abc", every_k_turns=1)
    checkpoint = recorder.maybe_capture(1, elapsed_ms=0)
    assert checkpoint.tests_pass is None
    assert checkpoint.per_test == []


def test_final_capture_forces_a_snapshot_regardless_of_k():
    container = FakeContainer()
    recorder = CheckpointRecorder(container, base_sha="abc", every_k_turns=10)
    recorder.maybe_capture(1, elapsed_ms=0)
    final = recorder.force_capture(turn=1, elapsed_ms=999)
    assert final is not None
    assert container.calls == 1


class ExplodingContainer:
    """Fails the way the real race does, then recovers."""

    def __init__(self, fail_turns):
        self.fail_turns = set(fail_turns)
        self.calls = 0

    def snapshot_diff(self, base_sha):
        self.calls += 1
        if self.calls in self.fail_turns:
            raise ContainerError(
                "git add -A failed (exit 128): fatal: unable to stat "
                "'calc.py.tmpQ3xR1a': No such file or directory"
            )
        return (f"diff-{self.calls}", [f"file{self.calls}.py"])

    def submodule_states(self):
        # The DIFF is what this double fails. Its submodule read works, so
        # every test below keeps asserting exactly what it asserted.
        return {}


def test_a_failed_mid_run_snapshot_does_not_destroy_the_run():
    """The defect this containment exists for, measured on 2026-08-12.

    `maybe_capture` runs inside the agent's stdout loop. An exception there
    does not lose a checkpoint -- it unwinds out of ClaudeCodeRunner.run,
    past the container context manager, into execute_run's catch-all, and
    the record says CRASHED with zero turns, zero tokens, no diff and no
    cost for a run that was working. One offline arm in seven was destroyed
    that way by `git add -A` racing Claude Code's atomic Write.

    Checkpoints are supplementary evidence (section 5.5's cost/quality
    curve); the trajectory and the final diff are the run's product. The
    cheap thing must never cost the expensive one -- the tokens are already
    spent by the time this is called.
    """
    container = ExplodingContainer(fail_turns=[2])
    recorder = CheckpointRecorder(container, base_sha="abc", every_k_turns=1)

    results = [recorder.maybe_capture(t, elapsed_ms=0) for t in (1, 2, 3)]

    assert results[1] is None
    assert [c.turn for c in results if c is not None] == [1, 3]
    assert len(recorder.captured) == 2


def test_a_missing_checkpoint_says_so_rather_than_looking_like_an_idle_turn():
    """Containment alone would have swapped a loud wrong record for a quiet
    one. A short `checkpoints` list is byte-identical to an agent that
    changed nothing for a few turns, and the section 5.5 curve is computed
    post-hoc over exactly this list -- so the pass rate at budget K would be
    understated with nothing anywhere saying why."""
    recorder = CheckpointRecorder(
        ExplodingContainer(fail_turns=[1]), base_sha="abc", every_k_turns=1
    )

    recorder.maybe_capture(1, elapsed_ms=0)

    assert recorder.errors
    assert "unable to stat" in recorder.errors[0]
    assert "turn 1" in recorder.errors[0]


def test_the_final_snapshot_is_still_allowed_to_fail_loudly():
    """Deliberately not contained. force_capture runs after the agent has
    exited -- no race left to lose -- and its diff is the submission
    (section 5.6). Swallowing it would publish a record claiming the agent
    submitted nothing."""
    recorder = CheckpointRecorder(
        ExplodingContainer(fail_turns=[1]), base_sha="abc", every_k_turns=1
    )

    with pytest.raises(ContainerError):
        recorder.force_capture(turn=9, elapsed_ms=0)


# --- round 2 item 17: the submodule state the diff cannot carry ---


class _StatesRaise:
    """A container whose diff works and whose submodule read does not."""

    def snapshot_diff(self, base_sha):
        return ("diff-body", ["calc.py"])

    def submodule_states(self):
        raise ContainerError("boom")


def test_a_checkpoint_records_the_submodule_state_the_diff_cannot_carry():
    """Measured 2026-09-02 (git 2.50.1) through `snapshot_diff`'s own command
    sequence -- scratch-index `git add -A`, then `git diff --cached <base>`:
    a tracked file edited inside an initialised submodule and NOT committed
    stages ZERO BYTES, while `git status --porcelain=v2
    --ignore-submodules=none` reports `1 .M S.M. ... vendor/libdep`. So the
    diff is not the authority on that tree and the checkpoint carries the
    state beside it -- beside, never instead of."""
    container = FakeContainer(states={"vendor/libdep": "S.M."})
    recorder = CheckpointRecorder(container, base_sha="abc", every_k_turns=1)

    checkpoint = recorder.maybe_capture(1, elapsed_ms=0)

    assert checkpoint.submodules_dirty == {"vendor/libdep": "S.M."}
    assert checkpoint.diff_vs_base == "diff-1"


def test_no_dirty_submodule_is_an_empty_mapping_not_a_none():
    """`{}` is a measurement -- read, nothing dirty, which includes a tree
    with no submodule at all -- and `None` is "not read". Collapsing them is
    the defect this field exists to avoid one level down: a reader could not
    tell a clean tree from a read that never happened."""
    recorder = CheckpointRecorder(
        FakeContainer(states={}), base_sha="abc", every_k_turns=1
    )

    checkpoint = recorder.maybe_capture(1, elapsed_ms=0)

    assert checkpoint.submodules_dirty == {}
    assert recorder.errors == []


def test_a_failed_submodule_read_is_contained_and_named():
    """A supplementary read must not cost the checkpoint that carries it. The
    diff survives, the mapping says `None` -- not read -- and the failure
    names itself in `errors`, which `runner._collect_checkpoints` joins into
    `checkpoint_error`."""
    recorder = CheckpointRecorder(_StatesRaise(), base_sha="abc",
                                  every_k_turns=1)

    checkpoint = recorder.maybe_capture(1, elapsed_ms=0)

    assert checkpoint is not None
    assert checkpoint.diff_vs_base == "diff-body"
    assert checkpoint.submodules_dirty is None
    assert recorder.errors == ["turn 1: submodule state: ContainerError: boom"]


def test_the_final_capture_survives_a_failed_submodule_read():
    """The containment is inside `_capture` and not in `maybe_capture`, and
    this is why. `force_capture`'s DIFF is the submission and is still
    allowed to raise -- see the test above this section -- but losing that
    submission to a supplementary observation would invert the trade the
    whole module rests on."""
    recorder = CheckpointRecorder(_StatesRaise(), base_sha="abc",
                                  every_k_turns=1)

    checkpoint = recorder.force_capture(turn=1, elapsed_ms=0)

    assert checkpoint.diff_vs_base == "diff-body"
    assert checkpoint.submodules_dirty is None
    assert recorder.errors == ["turn 1: submodule state: ContainerError: boom"]
