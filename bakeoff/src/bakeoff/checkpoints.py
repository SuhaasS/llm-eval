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
        #: Turns whose snapshot could not be taken, and why. Non-empty means
        #: `captured` is incomplete -- see maybe_capture.
        self.errors: list[str] = []

    def maybe_capture(self, turn: int, elapsed_ms: int) -> Checkpoint | None:
        """Snapshot this turn if it is due. NEVER raises into the caller.

        This runs from inside the agent's stdout loop, so an exception here
        does not just lose a checkpoint: it unwinds out of
        `ClaudeCodeRunner.run`, past the container context manager, into
        `execute_run`'s catch-all, and the run is recorded as CRASHED with
        zero turns, zero tokens, no diff and no cost -- for a run that was
        working. Measured on 2026-08-12: `git add -A` lost a race against the
        agent's atomic Write and destroyed the record of an otherwise healthy
        offline arm.

        The trade the whole module rests on is that checkpoints are
        SUPPLEMENTARY evidence -- section 5.5's cost/quality curve -- while
        the trajectory and the final diff are the run's product. Losing the
        cheap thing must never cost the expensive one, and the tokens are
        already spent by the time this is called.

        The failure is kept on `errors` rather than swallowed, because a
        short `checkpoints` list is otherwise indistinguishable from an agent
        that made no changes for several turns.
        """
        if turn % self.every_k_turns != 0:
            return None
        try:
            return self._capture(turn, elapsed_ms)
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.errors.append(f"turn {turn}: {type(exc).__name__}: {exc}")
            return None

    def force_capture(self, turn: int, elapsed_ms: int) -> Checkpoint:
        """The FINAL snapshot, and it is allowed to raise.

        Deliberately different from maybe_capture. This one is taken after
        the agent has exited, so there is no race to lose and nothing left to
        interrupt -- and its diff is the submission (section 5.6). A caller
        that treated a missing final diff as ordinary would publish a record
        claiming the agent submitted nothing.
        """
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
