from bakeoff.checkpoints import CheckpointRecorder


class FakeContainer:
    """Records calls and returns a synthetic diff per turn."""

    def __init__(self):
        self.calls = 0

    def snapshot_diff(self, base_sha):
        self.calls += 1
        return (f"diff-{self.calls}", [f"file{self.calls}.py"])


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
