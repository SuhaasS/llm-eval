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

A checkpoint carries what the diff CANNOT carry, beside it rather than
instead of it. `git add -A` stages nothing for an uncommitted edit, an
untracked file or an `rm` of tracked content inside an initialised submodule,
and nothing for content inside an uninitialised one -- measured 2026-09-02
(git 2.50.1) through `container.snapshot_diff`'s own command sequence, 0
bytes in every case -- so a diff describes the tree completely only outside
gitlink boundaries. `Checkpoint.submodules_dirty` records the per-gitlink
state `container.submodule_states` reads; the diff is still the submission.
"""

from __future__ import annotations

from typing import Protocol

from bakeoff.schema import Checkpoint


class SupportsSnapshot(Protocol):
    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]: ...
    def submodule_states(self) -> dict[str, str]: ...


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

        Its DIFF is what is allowed to raise. The submodule read never is: it
        is contained inside `_capture`, so losing the submission to a
        supplementary observation -- the exact inversion of the trade this
        module rests on -- cannot happen on this path either.
        """
        return self._capture(turn, elapsed_ms)

    def _capture(self, turn: int, elapsed_ms: int) -> Checkpoint:
        diff, files = self.container.snapshot_diff(self.base_sha)
        try:
            # Contained HERE and not in `maybe_capture`, which is what makes
            # `force_capture` safe too: see that method's docstring. `None` is
            # "not read" and is distinct from the `{}` a clean tree gives --
            # a partial mapping presented as a whole one would be the very
            # defect this field exists to remove, one level down.
            submodules: dict[str, str] | None = self.container.submodule_states()
        except Exception as exc:  # noqa: BLE001 - see the docstrings above
            submodules = None
            self.errors.append(
                f"turn {turn}: submodule state: {type(exc).__name__}: {exc}"
            )
        checkpoint = Checkpoint(
            turn=turn,
            diff_vs_base=diff,
            files_touched=files,
            elapsed_ms=elapsed_ms,
            # Stays None. "Filled by the offline grader" was wrong about the
            # direction: `grader.py` writes a GradeRecord to `grades.jsonl`
            # (`grade_schema.py`) beside the event log and never back into it.
            # A record is immutable once written, so nothing can fill this in
            # afterwards, and a reader who expects it to be filled reads the
            # permanent None as a grader that did not run.
            tests_pass=None,
            per_test=[],
            submodules_dirty=submodules,
        )
        self.captured.append(checkpoint)
        return checkpoint
