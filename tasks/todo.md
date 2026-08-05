# Bakeoff Harness — Task Progress

Source plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](../docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

- [x] **Task 1** — Schema and event log
- [ ] Task 2 — Cost calculation (`costs.py`)
- [ ] Task 3 — Trajectory parser (`trajectory.py`)
- [ ] Task 4 — Destructive-command and secret scanners (`scanners.py`)
- [ ] Task 5 — Container lifecycle and git pinning (`container.py`)
- [ ] Task 6 — Checkpoint capture (`checkpoints.py`)
- [ ] Task 7 — Wire-level logging (`wire.py`)
- [ ] Task 8 — Failure and exclusion classification (`classify.py`)
- [ ] Task 9 — Claude Code runner (`claude_runner.py`)
- [ ] Task 10 — Run orchestrator (`runner.py`)
- [ ] Task 11 — Fault-injection gate
- [ ] Task 12 — End-to-end smoke test

---

## Review — Task 1 (2026-08-05)

**Delivered:** `bakeoff/` package with `schema.py` (SCHEMA_VERSION 1.0.0, 6 enums, 14 frozen dataclasses) and `eventlog.py` (`EventLog`, `ImmutabilityError`). 9 tests, all passing. Implemented verbatim from the plan doc; TDD order held, each module's tests observed failing with `ModuleNotFoundError` before implementation.

**Spec constraints verified:**
- Immutability — `write_run` opens the temp file mode `"x"`; a second write of the same `run_id` raises `ImmutabilityError`. No update or delete method exists on `EventLog`.
- Crash safety — record is fsynced and atomically renamed *before* the index line is appended, so the index never advertises a run that isn't on disk. Covered by `test_partial_write_does_not_corrupt_index`.
- No write-time metrics — schema stores raw observations only.
- `ExclusionClass` has no `MODEL_FAILURE` member, by design.

**Spot check beyond the suite:** wrote a record with a nested `TurnRecord` and `DestructiveEvent`, read it back — round-trip equal, nested enums restored as `Severity`/`DestructiveCategory`, not bare strings.

**Deviation from plan:** venv installed with `pip install -e . --no-deps` plus pytest, rather than `-e '.[dev]'`. Task 1 is stdlib-only; `docker` and `litellm` are first needed in Tasks 5 and 7 and can be installed then. Nothing in the pyproject changed.

**Verify:**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v
```
