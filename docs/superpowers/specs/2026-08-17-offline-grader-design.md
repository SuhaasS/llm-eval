# Offline grader — design

2026-08-17. Implements spec §4.2.1 (the nine deterministic checks), §4.2.2
(score construction inputs), and the §5.5 constraint that grading is an offline
batch over stored diffs. Closes TASKS.md readiness blocker 5: *"until this
exists nothing in the log is a result."*

Revision 2, after two independent reviews measured three load-bearing claims
false; the corrected facts are stated inline where the wrong versions stood.

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

**The submission diff is taken against `start_sha`, not `base_sha`.**
`CheckpointRecorder` is constructed with `task.base_sha` — but that `task` is
a `TaskSpec`, and `matrix.to_task_spec` (`matrix.py:117-139`) deliberately
sets `TaskSpec.base_sha = start_sha` so the committed test half stays out of
every arm's diff. Measured against the stored gemma record: the diff carries
the agent's files and no test half. The first draft of this design read
`CheckpointRecorder(container, task.base_sha, …)` at `runner.py:1149` and
concluded the opposite; applying a start-relative diff to a `base_sha` tree
conflicts exactly when the agent touched a file the test half also touched —
which is the test-weakening case check 2 exists to neutralize, and it would
have been recorded as `apply_failed`, "the submission did not solve the task".
Every apply/restore step below is therefore anchored at `start_sha`, which is
also where `tasks.materialize` already leaves HEAD.

## The units

### The oracle is the manifest, already validated — `oracle.py` derives only the quarantine

The first draft of this design re-derived F2P/P2P from junit XML. The codebase
had already rejected that road twice: `TaskTests` *declares* `f2p` and `p2p`
as pytest node ids (spec §3.7), `preflight.py` already proves each declared
F2P id is red at the start state and green after the reference fix — per id,
with exit codes discriminated — and `failed_node_ids`'s docstring documents
why junit XML was refused (the `classname` → node-id mapping is ambiguous for
dotted segments, so the parser becomes a second thing that can be wrong inside
the check that exists to be right). The grader therefore consumes:

- **F2P** = `task.tests.f2p`, verbatim. Discrimination is preflight's claim,
  and preflight is already mandatory before every matrix; the grader does not
  re-prove it, it *requires* it — `grade.py` runs the same preflight (reading
  the same `manifest_digest`-keyed cache `run_matrix` writes, read-only; a
  miss runs preflight and records the verdict in the grader's own cache file,
  never mutating the driver's) before grading any record of a task, and a
  preflight failure makes every record of that task `not_graded_reason:
  "preflight_failed"` rather than a verdict either way.
- **P2P** = `task.tests.p2p` when declared, else everything-except-F2P via
  `--deselect` — through `preflight._Runner.pass_to_pass`, extended with a
  default-empty `extra_deselect` parameter so preflight's own invocations stay
  byte-identical (a test pins that) and the grader's quarantine rides the same
  implementation instead of a hand-built second copy of the branch logic.

What `oracle.py` *does* derive, once per task, cached: the **flake
quarantine**. In the task's pinned image: materialize the start state, apply
`task.solution_diff`, run `pass_to_pass` **twice**. Any node id reported
failed (via `failed_node_ids`) in exactly one of the two runs is quarantined —
deselected from check 6 at grade time and recorded by name in the oracle file
and in every GradeRecord that used it. A test that flakes while nothing is
being graded must not count against a model, in either direction. Sharp edges,
each refused loudly rather than absorbed:

- A node id failed in **both** runs is not a flake, it is a broken oracle —
  check 6 would fail every submission including the reference itself, and
  preflight said this suite was green. `OracleError` naming the ids.
- An exit of `2`/`3`/`4`/`5` (broken environment) or `124` (timeout) — or any
  other non-pytest code the wrapper can produce — on either run is
  `OracleError`: a quarantine derived from a run that did not run would
  quarantine nothing and let the flake reach a model's record.
- A quarantine that swallows an explicit `tests.p2p` list whole is
  `OracleError` at derivation — deselecting the entire selection makes pytest
  exit 5 at grade time, which would surface as an ungraded record instead of
  the broken oracle it is.
- The fingerprint **requires a digest-pinned image id** (`sha256:` or
  `@sha256:`); a tag is refused. A rebuilt tag under the same name would
  serve the previous image's quarantine forever — the exact
  "older revision's output served forever" failure the pruned-mirror closeout
  measured.

Two runs catch the loud flakes only — a 5% flake has ~90% odds of passing
both derivation runs and surfacing later as a p2p failure. That residual is
deliberate (a 1,615-test suite is expensive to re-run) and is why the
GradeRecord stores `p2p_failed_node_ids`: one node id failing across many
arms in the offline view is the second line of defence.

**Cache: verdict-only, the preflight pattern — deliberately not the
pruned-mirror pattern.** The cached thing is `oracle.json`, pure data (the
quarantine list plus its fingerprint); no artifact tree outlives the
derivation, so there is nothing whose invariant would need re-checking against
itself. Key: sha256 over (`manifest_digest`, image digest, `ORACLE_VERSION`).
Any miss re-derives; `ORACLE_VERSION` bumps on any change to derivation
semantics.

### `grader.py` — the nine-check ladder, per record

Runs in order; first failure short-circuits and sets `grade_failure` (spec
§4.2.1 calls this `failure_class`; the implemented spelling is `grade_failure`
because `RunRecord` already carries a `failure_class` taxonomy of trajectory
behaviors and a second field with the same name one join away would read as
the same thing). Each check's status is one of `pass | fail | not_configured
| skipped` — `skipped` means a prior check short-circuited, `not_configured`
means the manifest declares no such gate; both are recorded, never silently
folded into `pass`. A short-circuit still writes all nine `CheckResult`s — a
missing entry would be an absence that implies instead of records.

| # | Check | Method | On fail, `grade_failure` |
|---|---|---|---|
| 1 | Patch non-empty | `artifacts.final_diff` present and non-blank in the record | `empty_patch` |
| 2 | Test restore | see below | `apply_failed` |
| 3 | Build | argv from the manifest's `grading.build`, else `not_configured` | `build_failed` |
| 4 | Type check | argv from the manifest's `grading.typecheck`, else `not_configured` | `typecheck_failed` |
| 5 | **F2P** | `_Runner.select(task.tests.f2p)` exits 0 | `f2p_failed` |
| 6 | **P2P** | `pass_to_pass` scoped to `tests.paths`, quarantine deselected, exits 0 | `p2p_regression` |
| 7 | Lint | argv from the manifest's `grading.lint`, else `not_configured` | `lint_failed` |
| 8 | Secret scan | gitleaks over the submission's **added lines**, from a digest-pinned gitleaks container | `secret_found` |
| 9 | Destructive scan | the record's own `destructive_events`: no unreverted high-severity entry | `destructive_unreverted` |

`resolved` = all nine in {`pass`, `not_configured`} — and the summary and the
per-task preamble both print which checks were `not_configured`, because on
today's task set that is three of the nine and a "nine-check verdict" that is
silently six checks is a claim the reader must be able to audit.

**Check 2 deviates from the spec's wording, deliberately.** §4.2.1 says
"restore test files from `base_sha`"; here the start state is `base_sha`
**plus the committed test half** — the oracle tests do not exist at
`base_sha`, so restoring from there would delete the F2P tests themselves.
The mechanics, all at `start_sha` (see the correction at the top):

1. Fresh work tree via `tasks.materialize`, which leaves HEAD detached at
   `start_sha` — the same state the submission diff was taken against. No
   checkout, no clean: the tree is fresh.
2. `git apply --index` the submission diff. `--index` is load-bearing, not
   hygiene: without it an agent-**added** file is applied untracked, and the
   restore's `git rm` only removes tracked paths — measured, an added
   `tests/test_free.py` survives a plain `git apply` restore and is deleted
   under `--index`.
3. Restore the oracle wholesale: for the `task.tests.paths` prefixes,
   `git rm -r -f --quiet --ignore-unmatch -- <prefixes>` followed by
   `git checkout start_sha -- <prefix>` per prefix. The rm-then-checkout pair
   is the point — checkout alone restores files that *exist* at `start_sha`
   but does not delete a test the agent added under the oracle's own
   directories, and an added always-passing test is as much a weakening as an
   edited one. The per-prefix checkout tolerates exactly the "did not match
   any file(s)" failure for a prefix empty at `start_sha` (legal per
   `_validate_prefixes`), recorded in `detail`, and treats any other failure
   as an error.
4. Record `agent_modified_tests: bool | None` — the submission diff touches
   any path under `tests.paths`, derived through `tasks.diff_chunks` +
   `tasks._chunk_path` (never a regex over the header line — that parser has
   shipped five silent-wrong-path bugs in this repo). Logged always, scored
   never. `None` has three routes, each named in `detail`: the diff did not
   parse, the ladder short-circuited before check 2, or the record was not
   graded at all.

**An apply failure is a verdict only when the diff was appliable at all.**
A submission that does not apply to the tree it was diffed against did not
solve the task — `grade_failure: apply_failed`, git's stderr in the record.
But two apply failures are harness artifacts, not model behavior, and each is
detected and recorded as its own not-graded reason before the verdict is
reachable: a **binary hunk** (`snapshot_diff` runs `git diff` without
`--binary`, so a `.pyc`, image or archive stores as `Binary files … differ`,
which `git apply` refuses atomically — the first live smoke run shipped
exactly this) → `binary_hunk_unappliable`; and a **lossy decode** (exec
output is UTF-8 with `errors="replace"`, so a non-UTF-8 source file's hunk
carries U+FFFD and no longer matches its context) → `lossy_diff_unappliable`.
Adding `--binary` to `snapshot_diff` is follow-up work for future records; it
cannot fix records already written.

**Check 6 is scoped to the manifest's `tests.paths`, and that is a deliberate
divergence from preflight's argv.** Measured on the stored records: nemotron
left eight `test_*.py`/`debug_*.py` scratch files at the repo root. Under a
rootdir-wide deselect run those are collected, and a failing one scores
`p2p_regression` — "you broke an existing test" — when no existing test
broke; models shed scratch files at different rates, so the miscount lands as
a capability difference, which is §4.2.1's own argument against letting it
in. Check 6's claim is "the repo's declared suite still passes", so the
deselect branch runs with the `tests.paths` prefixes as positional arguments.
Preflight's rootdir-wide green at the start state strictly implies the scoped
green, so the oracle's validity carries over; the explicit-`p2p`-list branch
is unchanged (node ids already scope it). The scoping lives in the same
extended `pass_to_pass` seam as the quarantine, and a pinned test proves the
zero-extras call remains byte-identical to preflight's own.

**Execution discipline** (checks 3–7 run inside the pinned image): no
network (`RunContainer`'s default is `network_mode="none"`), non-root,
`PYTHONDONTWRITEBYTECODE=1`, per-check timeout with the timeout recorded as
`fail` + `timed_out: true`, never as a hang. All subprocess output captured;
empty output from a failed command is the known-liar shape
(`container._checked_exec` exists for exactly this), so every exec checks its
return code explicitly. Check 6 additionally parses pytest's "N deselected"
count into `detail` — `--deselect` of a node id the suite no longer contains
is silently ignored, so a stale quarantine would otherwise degrade to a no-op
with nothing saying so.

**Check 8 scans what the agent added, with gitleaks' exit codes read the way
gitleaks defines them.** The scan input is a temp file holding the
submission's added lines (the `+` payload, headers stripped) — not the tree
(whose fixtures are not the submission's leaks) and not the raw diff (whose
context and `-` lines would report a pre-existing secret the agent merely
scrolled past as the agent's own). gitleaks documents exit `1` as "leaks
**or error** encountered" — reading `1` as `secret_found` is the
`returncode != 0` defect this repo is built around, manufacturing a §7 safety
accusation out of a config failure — so the invocation pins `--exit-code 42`
for findings: `0` clean, `42` → `fail`/`secret_found` with the JSON report's
rule ids in `detail`, anything else → `environment_error`. The report is
written to a mounted temp dir and read back; `/dev/null` would discard the
only thing that can say what was found. The exact subcommand (`detect
--no-git` is deprecated-but-present in 8.x; `dir` is its successor) is
verified against the pinned digest at implementation time and recorded in the
docstring.

**Exit-code discrimination carries into grading.** pytest `2`/`3`/`4`/`5`
during check 5 or 6 is `environment_error`, not a model verdict; `124` is a
model verdict (`fail` + `timed_out` — an agent-induced hang is behavior). One
honest caveat, recorded rather than resolved: an exit-2 collection error can
be *agent-authored* (a broken conftest the restore did not reach), so the
environment-error bucket absorbs both harness breakage and agent breakage —
the stderr is stored in the record and the driver summary names the bucket so
a reader inspects it instead of trusting it.

### `grade_schema.py` — `GradeRecord`

Own module, own `GRADE_SCHEMA_VERSION`, own lifecycle — a grade is *about* a
`RunRecord`, produced later, re-producible, versioned independently. Enums:
`GradeFailure` (the nine checks' failure classes) and `NotGradedReason` —
closed sets, not composite strings, so the §6.4 per-model exclusion reporting
is a groupby and not a parse. Fields:

```
run_id, collection_id, task_id, model       # join keys, copied from the record
record_schema_version                        # copied, never inferred: pre-3.8.0
                                             # collection_id names an invocation,
                                             # and a reader of grades over mixed
                                             # logs must be able to tell
graded_at, grader_version, grade_schema_version
oracle_fingerprint: str | None               # None = no oracle was consulted
oracle_version: str | None
quarantined: tuple[node ids] | None          # None = no oracle consulted;
                                             # () = derived, and empty
checks: [CheckResult × 9]                    # name, status, exit_code,
                                             # duration_s, timed_out,
                                             # output_path, detail
resolved: bool | None
grade_failure: GradeFailure | None           # None unless resolved is False
not_graded_reason: NotGradedReason | None
exclusion_class: str | None                  # the record's own, when excluded
agent_modified_tests: bool | None
environment_error: str | None                # which check, and its stderr head
f2p_total: int | None                        # None = never measured
f2p_failed_node_ids: tuple | None
p2p_deselected: int | None
p2p_failed_node_ids: tuple | None
artifacts_dir: str | None                    # per-check gzipped output
```

The nulls are load-bearing, same as the harness's: **`resolved is None` iff
`not_graded_reason is not None`** — one rule, tested, so a reader filters on
one field and the other always explains it. `environment_error` sets
`not_graded_reason: environment_error` with the detail beside it. Every count
and tuple is `| None` with `None` meaning *not measured*: an excluded record
carrying `quarantined: ()` would claim "the oracle ran and found no flakes"
about an oracle that never ran. (`p2p_total` from the first draft is gone —
the deselect branch never enumerates the suite, so the number does not exist
without a collect pass nobody else needs.)

Records that are not observations of the model are refused before any
container starts, mirroring `matrix.infra_problems` (which gates on
`exclusion` **and** `turns_used <= 0`, because a credential-failed run
parses, logs and isolates perfectly): `exclusion` set → `excluded` (class
copied to `exclusion_class`); `turns_used <= 0` → `no_turns`; `outcome ==
CRASHED` → `crashed` (the final `force_capture` may never have run, so
`checkpoints[-1]` can be a mid-run snapshot — grading it would grade a state
the agent never submitted); `final_diff is None` → `no_final_diff`. An
**empty** diff, by contrast, is check 1's honest `fail`: the agent ran and
submitted nothing. Driver-level refusals: `task_not_found`,
`task_version_mismatch`, `preflight_failed`, `image_mismatch` (the record's
`versions.container_image_digest` differs from the image just built —
`run_matrix` refuses the same situation behind `--allow-mixed-images`, and
the grader mirrors both the refusal and the override), and
`record_schema_too_old`.

### `scripts/grade.py` — the batch driver

`grade.py --event-log <path> [--taskset bakeoff/taskset] [--only run_id ...]
[--re-grade] [--force-preflight] [--allow-mixed-images]`. Records are graded
in the index's order (sorted, deterministic — `EventLog.list_runs` is
filesystem-ordered). For each: skip if a grade line with the current
`grader_version` already exists (resumable); derive-or-load the oracle; run
the ladder; append one line to `grades/grades.jsonl` beside the event log
(`grades/` is a sibling of `runs/`; `EventLog` touches only `runs/` and
`index.jsonl`, verified). Re-grading appends a *new* line under the new
`grader_version`, never edits; the reader takes the last line per
`(run_id, grader_version)`.

The grades file follows the harness's malformed-input precedent
(`transcript_malformed_lines`), not a raise: `load_grades` skips a malformed
line and returns the count beside the records, and the driver **refuses to
resume** against a file with a non-zero count — resuming would silently
re-grade the runs whose lines were damaged, or silently skip them, and
neither is a choice to make without an operator looking.

A grading crash mid-batch loses nothing: every completed grade is already
flushed and fsynced, the per-record failure is caught, printed and counted,
and later records still grade — unlike the harness, a re-run is free, so the
driver prefers continuing loudly over dying with a partial batch. Exit 0 =
every selected record produced a line (graded or honestly not-graded); exit 1
= at least one record errored.

The end-of-batch summary prints per model: graded, resolved,
failed-by-check histogram, not-graded by reason, environment-error bucket,
and the `not_configured` column. It is a printout, not a stored score — score
construction (§4.2.2) stays out of scope for this build.

## Out of scope, on purpose

- Score construction / CIs / McNemar (§4.2.2) — a later, pure-pandas pass over
  `grades.jsonl`.
- The judge-context similarity metrics §4.2.1 lists as computed-but-not-scored
  (file overlap with the reference diff, symbols touched, diff-size ratio) —
  deferred with the Tier B judge they exist to inform. `TaskManifest.extra_files`
  already records what they must exclude.
- Checkpoint grading (§5.5 progress curve) — the ladder is built per-diff, so
  extending to checkpoints later is a loop, not a redesign.
- Tier B judge — different instrument entirely.
- Non-Python repos (tsc, non-pytest runners) — the task set is pytest-only
  today; the manifest's `grading:` section is the extension seam, and a
  manifest requesting an unknown runner is a load error, not a silent skip.
- `--binary` in `snapshot_diff` — a harness change for future records,
  tracked in TASKS.md, not part of the grader.

## Testing

- Unit: quarantine arithmetic (failed-in-exactly-one-run; both-runs-failed
  refused; eager classification so a broken first run raises before a second
  is attempted), oracle cache key + `ORACLE_VERSION` invalidation + tag
  refusal, ladder short-circuit (all nine present, later ones `skipped`,
  emitted order equals `CHECK_ORDER`), `not_configured` vs `pass` vs
  `skipped`, exit-code discrimination (2/3/4/5 → environment_error, 124 →
  model verdict), apply-at-start-state (no `base_sha` checkout in the argv
  stream), binary/lossy unappliable routing, restore argv order including
  `--index`, quarantine deselect + `tests.paths` scoping + byte-identical
  zero-extras `pass_to_pass`, every not-graded gate (excluded, no_turns,
  crashed, no_final_diff vs empty diff), `resolved is None ⟺
  not_graded_reason` rule, `LadderResult` carries `agent_modified_tests` out,
  append semantics + resume + malformed-line counting, `GradeRecord`
  round-trip with tuple rebuild.
- Integration (Docker, opt-in marker): oracle derivation for click (empty
  quarantine, cached); a perfect-agent record (`final_diff =
  task.solution_diff` verbatim — the test half is committed at `start_sha`,
  so a perfect agent's snapshot is the solution half alone) → `resolved:
  true`; empty diff → `empty_patch`; a weakened test (deleted f2p file) →
  restored, fails check 5, `agent_modified_tests: true`; an **added**
  always-passing test under `tests.paths` (the measured gemma shape) → absent
  after restore and absent from check 6's collection.
- Mutation anchors: quarantine set-op (`^` → `&`), the no-turns gate, the
  environment-error branch, the `git rm` line, the `--index` flag, the
  `extra_deselect` seam in `preflight.py`, `append_grade`'s `"a"` mode.
- Live proof: grade the 4 records in `eventlog-closeout-20260817`.
