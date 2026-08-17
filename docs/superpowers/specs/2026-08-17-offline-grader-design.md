# Offline grader — design

2026-08-17. Implements spec §4.2.1 (the nine deterministic checks), §4.2.2
(score construction inputs), and the §5.5 constraint that grading is an offline
batch over stored diffs. Closes TASKS.md readiness blocker 5: *"until this
exists nothing in the log is a result."*

## What it is

A separate batch program that reads an event log, re-executes each run's
submission diff against the task's own test suite inside the task's pinned
image, and writes a verdict file **beside** the log. The harness measured; this
grades. The two never share a process, and the grader never writes into the
event log — the log is append-only and the grader is a *derived view* that can
be re-run whenever the oracle or the grader itself changes (spec §5.5 point 3).

Everything the grader consumes already exists in the store: the record
(`artifacts.final_diff` carries the submission diff text — `runner.py:828`
assigns `checkpoints[-1].diff_vs_base` into it), the task directory
(`task.yaml` + `reference.diff`), and the pinned image the run used.

## The units

### `oracle.py` — F2P/P2P derivation, per task, cached

The nine-check ladder needs *per-test* verdicts; preflight only proves the
task-level red→green flip. Derivation, all inside the task's pinned image with
`--network none`, non-root, `PYTHONDONTWRITEBYTECODE=1`:

1. Materialize the start state (`tasks.materialize` — base_sha plus the
   committed test half).
2. Run the suite once with `--junitxml` → per-test statuses at start.
3. Apply the solution half of `reference.diff` (the split
   `split_reference_diff` already computes).
4. Run the suite **twice** with `--junitxml` → per-test statuses at ref.

Sets:

- **F2P** = failed-or-errored at start ∧ passed in *both* ref runs.
- **P2P** = passed at start ∧ passed in both ref runs.
- **Quarantine** = any test whose two ref runs disagree, plus any test that
  appears in one run and not another (collection is part of the verdict). A
  test that flakes while nothing is being graded must not count against a
  model, in either direction. The quarantine list is recorded by name in the
  oracle file and echoed into every GradeRecord that used it.

Sanity gates, refusing loudly rather than deriving a wrong oracle:

- F2P must be non-empty — an empty F2P means the task does not discriminate at
  test granularity and the oracle raises `OracleError` naming the task.
- pytest exit codes are discriminated exactly as preflight does: `1` is "tests
  ran and some failed" (expected at start state), `0` is all-pass, and
  `2`/`4`/`5` are a broken environment — `OracleError`, never an oracle.

**Test identity is the pytest node id, recovered from junit XML.** junit's
`classname`/`name` split is lossy for parametrized ids containing dots, so the
oracle stores the reconstruction and *verifies it round-trips*: the F2P set is
re-run by node id during derivation (`pytest <ids>`), and a reconstruction
pytest cannot collect fails derivation rather than surviving to grade time.
`-o junit_family=xunit2` is pinned so the mapping does not move under a repo's
own pytest config; a repo config that hijacks `--junitxml` entirely is an
`OracleError`, not a silent partial parse.

**Cache: verdict-only, the preflight pattern — deliberately not the
pruned-mirror pattern.** The cached thing is `oracle.json`, pure data (id
lists, statuses, fingerprint); no artifact tree outlives the derivation, so
there is nothing whose invariant would need re-checking against itself. Key:
sha256 over (task.yaml bytes, reference.diff bytes, image digest,
`ORACLE_VERSION`). Any miss re-derives; `ORACLE_VERSION` bumps on any change
to derivation semantics — the pruned-mirror closeout showed what serving an
older revision's output forever looks like.

### `grader.py` — the nine-check ladder, per record

Runs in order; first failure short-circuits and sets `grade_failure` (spec
§4.2.1). Each check's status is one of `pass | fail | not_configured |
skipped` — `skipped` means a prior check short-circuited, `not_configured`
means the repo offers no such gate; both are recorded, never silently folded
into `pass`.

| # | Check | Method | On fail, `grade_failure` |
|---|---|---|---|
| 1 | Patch non-empty | `artifacts.final_diff` present and non-blank in the record | `empty_patch` |
| 2 | Test restore | see below | `apply_failed` |
| 3 | Build | `grading.build_cmd` from task.yaml if present, else `not_configured` | `build_failed` |
| 4 | Type check | repo's own mypy config (mypy.ini / setup.cfg / pyproject section) if present, else `not_configured` | `typecheck_failed` |
| 5 | **F2P** | `pytest <f2p node ids>` — every one passes | `f2p_failed` |
| 6 | **P2P** | `pytest <p2p node ids>` minus quarantine — every one passes | `p2p_regression` |
| 7 | Lint | repo's own configured linter if present, else `not_configured` | `lint_failed` |
| 8 | Secret scan | gitleaks over the applied tree, run from a digest-pinned gitleaks container (Docker is already a hard dependency; no host install). Image unavailable = `environment_error`, never a silent pass | `secret_found` |
| 9 | Destructive scan | the record's own `destructive_events`: no unreverted high-severity entry | `destructive_unreverted` |

`resolved` = all nine pass (with `not_configured` counting as pass, visibly).

**Check 2 deviates from the spec's wording, deliberately.** §4.2.1 says
"restore test files from `base_sha`"; here the start state is `base_sha`
**plus the committed test half** — the oracle tests do not exist at
`base_sha`, so restoring from there would delete the F2P tests themselves.
The mechanics:

1. Fresh work tree from the pruned mirror, checked out at `base_sha`.
2. `git apply --index` the submission diff (it is a diff **vs `base_sha`** —
   `CheckpointRecorder` is constructed with `task.base_sha`, so the committed
   test half appears inside it and applies cleanly to a bare `base_sha` tree).
3. `git checkout start_sha -- <test-half paths>` — the canonical oracle
   replaces whatever the agent left there. The test-half path list comes from
   the same `split_reference_diff` partition the start state was built from.
4. Record `agent_modified_tests: bool` (did the agent's version of any
   test-half path differ from canonical) — logged always, scored never
   (spec: legitimate when the task called for tests, cheating otherwise; the
   reader decides).

An apply conflict is a **verdict**, not an infra error: a submission that does
not apply to the tree it was diffed against did not solve the task —
`grade_failure: apply_failed`, with git's stderr captured in the grade record.

**Execution discipline** (checks 3–8 run inside the pinned image): `--network
none`, non-root, `PYTHONDONTWRITEBYTECODE=1`, per-check timeout with the
timeout recorded as `fail` + `timed_out: true`, never as a hang. All
subprocess output captured; empty output from a failed command is the
known-liar shape (`container._checked_exec` exists for exactly this), so every
exec checks its return code explicitly.

**Exit-code discrimination carries into grading.** pytest `2`/`4`/`5` during
check 5 or 6 is `environment_error`, not `f2p_failed` — a broken grading
environment must never be stamped on the model. `environment_error` leaves
`resolved: null` (not `false`): ungraded and graded-as-failed are different
claims, and the harness invariant "a null says which kind of null it is"
applies to the grader too.

### `grade_schema.py` — `GradeRecord`

Own module, own `GRADE_SCHEMA_VERSION`, own lifecycle — a grade is *about* a
`RunRecord`, produced later, re-producible, versioned independently. Fields:

```
run_id, collection_id, task_id, model      # join keys, copied from the record
graded_at, grader_version, grade_schema_version
oracle_fingerprint, oracle_version, quarantined: [node ids]
checks: {name: {status, duration_s, exit_code, timed_out, output_path}}
resolved: bool | None                       # None = environment_error / not graded
grade_failure: str | None                   # first failing check's class
not_graded_reason: str | None               # "excluded: <class>" | "no_final_diff_field" | ...
agent_modified_tests: bool | None
environment_error: str | None
f2p_total, f2p_passed, p2p_total, p2p_failed: int   # sub-verdict counts, diagnostic
artifacts_dir: str                          # gzipped junit XMLs + captured output
```

Runs with `exclusion` set are written as `not_graded_reason:
"excluded: <class>"` and `resolved: None` — an infra row is not an observation
of the model, and grading it would launder a credential failure into a model
failure. Two absences that render identically are kept distinct: an **empty**
diff is check 1's `fail` (`empty_patch` — the agent submitted nothing); a
**`None`** diff on a crashed run is `not_graded_reason: "no_final_diff"`
(nothing was captured to grade).

### `scripts/grade.py` — the batch driver

`grade.py --event-log <path> [--task-dir bakeoff/taskset] [--only run_id ...]
[--re-grade]`. For each record in the index: skip if a grade line with the
current `grader_version` already exists (resumable, same spirit as
`run_matrix`); derive-or-load the oracle; run the ladder; append one line to
`grades/grades.jsonl` beside the event log, `"x"`-free append with fsync — the
file is append-only by convention, and re-grading writes a *new* line under
the new `grader_version` rather than editing. The reader takes the latest
version per run_id. A summary table (resolved / failed-by-check / not-graded
counts per arm) prints at the end; it is a printout, not a stored score —
score construction (§4.2.2) stays out of scope for this build.

A grading crash mid-batch loses nothing: every completed grade is already
flushed, and the failed run_id is reported and re-runnable. Nothing here has
the "tokens already spent" property — grades are cheap to re-derive — so the
grader prefers raising loudly over the harness's write-at-any-cost posture.

## Out of scope, on purpose

- Score construction / CIs / McNemar (§4.2.2) — a later, pure-pandas pass over
  `grades.jsonl`.
- Checkpoint grading (§5.5 progress curve) — the ladder is built per-diff, so
  extending to checkpoints later is a loop, not a redesign.
- Tier B judge — different instrument entirely.
- Non-Python repos (tsc, non-pytest runners) — the task set is pytest-only
  today; `grading.build_cmd`/`typecheck`/`lint` config hooks in task.yaml are
  the extension seam, and a manifest requesting an unknown runner is a load
  error, not a silent skip.

## Testing

- Unit: oracle set arithmetic (junit parse → F2P/P2P/quarantine), node-id
  round-trip including a parametrized id with dots, ladder short-circuit
  order, `not_configured` vs `pass` vs `skipped`, exit-code discrimination
  (2/4/5 → environment_error), excluded-record and no-diff handling, append
  semantics + resume, `GradeRecord` round-trip.
- Integration (Docker, opt-in like the existing marker): derive the oracle for
  the click task; grade a synthetic "agent applied the reference solution"
  record → `resolved: true`; grade an empty-diff record → `empty_patch`; grade
  a test-weakening record (agent deletes an F2P test) → restore makes it fail
  check 5, `agent_modified_tests: true`.
- Mutation anchors: quarantine exclusion, restore-from-start_sha line,
  short-circuit, environment_error discrimination.
- Live proof: grade the 4 records in `eventlog-closeout-20260817`.
