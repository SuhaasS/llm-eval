"""Per-turn diff capture. See spec section 5.5.

The agent run stores diffs only. Running the oracle inline would cost
roughly 96,000 test-suite executions across the full eval, so grading is a
separate offline batch job over these stored diffs. That also makes
checkpoint grading re-runnable if the oracle changes.

A checkpoint is only meaningful if it is taken while the agent is working:
snapshotting after the run has finished yields the same end state for every
turn number, which reads as a per-turn progression that never happened.
The recorder cannot detect that -- it snapshots whenever it is called, so
the caller owns interleaving capture with execution.
"""

from __future__ import annotations

from typing import Protocol

from bakeoff.schema import Checkpoint


class SupportsSnapshot(Protocol):
    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]: ...


class CheckpointRecorder:
    def __init__(
        self, container: SupportsSnapshot, base_sha: str, every_k_turns: int = 1
    ) -> None:
        if every_k_turns < 1:
            raise ValueError("every_k_turns must be >= 1")
        self.container = container
        self.base_sha = base_sha
        self.every_k_turns = every_k_turns
        self.captured: list[Checkpoint] = []

    def maybe_capture(self, turn: int, elapsed_ms: int) -> Checkpoint | None:
        if turn % self.every_k_turns != 0:
            return None
        return self._capture(turn, elapsed_ms)

    def force_capture(self, turn: int, elapsed_ms: int) -> Checkpoint:
        return self._capture(turn, elapsed_ms)

    def _capture(self, turn: int, elapsed_ms: int) -> Checkpoint:
        diff, files = self.container.snapshot_diff(self.base_sha)
        checkpoint = Checkpoint(
            turn=turn,
            diff_vs_base=diff,
            files_touched=files,
            elapsed_ms=elapsed_ms,
            tests_pass=None,  # filled by the offline grader
            per_test=[],
        )
        self.captured.append(checkpoint)
        return checkpoint
