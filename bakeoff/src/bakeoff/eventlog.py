"""Append-only, immutable run log. See spec section 6.

There is deliberately no update or delete API. Excluded runs keep their
records (spec section 6.4) — nothing is ever removed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bakeoff.schema import RunRecord


class ImmutabilityError(RuntimeError):
    """Raised on any attempt to overwrite an existing run record."""


class EventLog:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def write_run(self, record: RunRecord) -> Path:
        path = self._run_path(record.run_id)
        tmp = path.with_suffix(".json.partial")

        # Mode "x" makes overwriting an existing record impossible.
        try:
            handle = open(tmp, "x", encoding="utf-8")
        except FileExistsError as exc:
            raise ImmutabilityError(f"in-flight write for {record.run_id}") from exc

        try:
            with handle:
                json.dump(record.to_dict(), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        if path.exists():
            tmp.unlink(missing_ok=True)
            raise ImmutabilityError(f"run {record.run_id} already written")

        # Rename is atomic on POSIX; the index is only touched after the
        # record is durably on disk, so the index never advertises a run
        # that does not exist.
        tmp.rename(path)

        with open(self.index_path, "a", encoding="utf-8") as index:
            index.write(
                json.dumps(
                    {
                        "run_id": record.run_id,
                        "task_id": record.task_id,
                        "model": record.model,
                        "sample_index": record.sample_index,
                        "outcome": record.outcome.value,
                        "started_at": record.started_at,
                        "schema_version": record.schema_version,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            index.flush()
            os.fsync(index.fileno())

        return path

    def read_run(self, run_id: str) -> RunRecord:
        with open(self._run_path(run_id), encoding="utf-8") as handle:
            return RunRecord.from_dict(json.load(handle))

    def list_runs(self) -> list[str]:
        return [p.stem for p in self.runs_dir.glob("*.json")]
