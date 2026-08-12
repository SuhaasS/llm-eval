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
                        # For last_run_for: whether this run made a model call
                        # at all, which is what decides if it warmed the cache.
                        # `outcome` cannot answer that -- see that docstring.
                        "turns_used": record.turns_used,
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

    def last_run_id_for(self, task_id: str, model: str) -> str | None:
        """The most recently written run of this task on this model, or None.

        Answers `CacheState.prior_same_task_run_id` -- the run that could
        plausibly have warmed the cache this one starts against.

        KEYED ON MODEL TOO, despite the field's name. Bedrock's prompt cache is
        per-model: a Sonnet run cannot warm Nemotron's. Keyed on task alone the
        stored id would routinely fail to explain the `warm` sitting beside it,
        and an analyst correlating the two would be misled by a record that
        looked complete.

        LAST LINE, not max `started_at`. write_run appends to the index only
        after the record is durably on disk, so line order is COMPLETION order
        -- and the run that most recently finished is the one that most
        recently touched the cache. Today's runner is sequential, so the two
        orders coincide; under section 5.7's interleaving they diverge and
        completion order stays the causally correct one.

        RUNS THAT MADE NO MODEL CALL ARE SKIPPED, on `turns_used`, not on
        `outcome`. A run that died before its first call touched no cache, and
        naming it as the warmer puts a plausible-looking id beside a `warm` it
        cannot explain -- the failure mode this docstring already warns about,
        arriving through the other door.

        Filtering on `outcome == CRASHED` was the obvious way to say that and
        it is wrong: `execute_run` sets `container_crashed` from an `except
        Exception` around its WHOLE body, so a run that made twenty calls and
        then failed in checkpoint capture or container teardown is CRASHED. It
        warmed the cache; skipping it loses the only run that explains the next
        one's `warm`. `turns_used` says what actually happened.

        A missing `turns_used` means an index line written before 3.0.0, and
        those are included -- absent evidence is not evidence of a quiet run.

        TWO LIMITS THE FIELD CANNOT COVER, both recorded rather than fixed:

        - Task scoping is probably too narrow. A warm turn 1 reads 30,506 and
          still writes 11,187 -- a partial prefix match, and the cached part is
          plausibly the task-independent system-prompt and tool-schema prefix.
          If so, a run of a DIFFERENT task on this model can be the real
          warmer, and no task-keyed lookup can name it. Settling that needs a
          measurement (run task B after task A and read turn 1), not a wider
          key: widening it now would replace a narrow answer with a wrong one.
        - This is unsafe under parallelism. The index is appended unlocked, and
          the lookup runs before the container starts, so two concurrent runs
          of the same (task, model) both read the same prior id and neither
          sees the other. Sequential today; a precondition on section 5.7.

        TOTAL BY CONSTRUCTION, and it has to be actually total rather than
        nearly so. A missing index, an unreadable one, or a torn trailing line
        yields None rather than raising -- the same tolerance
        proxy_callback.read_run_entries already applies to the wire log. This
        is what lets execute_run call it outside any try without endangering
        "a run always produces a record"; wrapping it instead would report a
        lookup failure as a container crash.

        Catching only OSError and JSONDecodeError was not enough, and the gap
        cost a record rather than a field: a line reading `null` is valid JSON
        and raises AttributeError on `.get`, and a truncation that splits a
        multi-byte character raises UnicodeDecodeError from the ITERATOR, which
        is a ValueError. Either one propagates out of execute_run, and because
        the bad line stays in the index it does so for every later run of every
        task. Hence `errors="replace"` and a per-line catch-all.

        The cost is real and bounded: an unreadable index is indistinguishable
        from no prior run. Acceptable only because this field is corroborating
        evidence, while `warm` -- the actual measurement -- is read from the
        transcript and does not depend on it.
        """
        found: tuple[str, str] | None = self.last_run_for(task_id, model)
        return found[0] if found else None

    def last_run_for(self, task_id: str, model: str) -> tuple[str, str] | None:
        """`(run_id, started_at)` of the run that plausibly warmed the cache.

        `started_at` is what `CacheState.seconds_since_prior_run` is computed
        from. It rides along on the same scan because the alternative -- opening
        the prior run's record -- turns a total lookup into one that can fail on
        a missing file, in a call site that must not fail at all.

        It is the prior run's START, not its finish: `finished_at` is the
        harness's finish, after checkpoint and artifact collection, and neither
        is the moment of the last model call. The gap is therefore an upper
        bound on cache idle time, which is the safe direction -- it can only
        make a warm run look less explicable, never more.
        """
        found: tuple[str, str] | None = None
        try:
            with open(self.index_path, encoding="utf-8", errors="replace") as index:
                for line in index:
                    try:
                        entry = json.loads(line)
                        if entry.get("turns_used", 1) < 1:
                            continue
                        if (
                            entry.get("task_id") == task_id
                            and entry.get("model") == model
                            and entry.get("run_id")
                        ):
                            found = (entry["run_id"], entry.get("started_at") or "")
                    except Exception:  # noqa: BLE001 - see TOTAL BY CONSTRUCTION
                        continue
        except OSError:
            return None
        return found
