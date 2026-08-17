# Offline grader — design

2026-08-17. Implements spec §4.2.1 (the nine deterministic checks), §4.2.2
(score construction inputs), and the §5.5 constraint that grading is an offline
batch over stored diffs. Closes TASKS.md readiness blocker 5: *"until this
exists nothing in the log is a result."*

Revision 3. Two review rounds (72 findings) are folded in; the corrected facts
are stated inline where the wrong versions stood.

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
(`task.yaml` + `reference.diff`), and a task image rebuilt from the manifest.

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
  `--deselect` — through `preflight._Runner.pass_to_pass`, extended with
  default-empty `extra_deselect` and `scope` parameters so preflight's own
  invocations stay byte-identical (a test pins that) and the grader's
  quarantine and suite scoping ride the same implementation instead of a
  hand-built second copy of the branch logic.

**Preflight grows two assertions so the gate validates what the grader will
actually run.** (1) The *scoped* `pass_to_pass` (positional `tests.paths`
prefixes) is run once at the post-reference-fix state and must be green with a
non-zero collected count — the first draft claimed rootdir-wide green
"strictly implies" scoped green, which is false in general (collection scoping
changes fixtures and ordering, and a prefix legal per `_validate_prefixes` can
collect nothing, which is pytest exit 5); the claim is replaced by a
measurement the gate makes before any matrix. (2) Each declared `grading.*`
argv must exit 0 at the post-fix state — otherwise a typo'd
`typecheck: ["mypy", …]` against an image without mypy surfaces as
`typecheck_failed` on every record of the task, permanently, instead of a
NO-GO before anything is graded.

What `oracle.py` *does* derive, once per task, cached: the **flake
quarantine**. In the task's pinned image: materialize the start state, apply
`task.solution_diff`, run `pass_to_pass` **twice — with the same
`scope=tests.paths` the grader will use**, so the quarantine describes exactly
the set it is later subtracted from (a quarantine derived rootdir-wide could
hold ids the scoped run never collects, and `--deselect` of an uncollected id
is silently ignored — measured). Any node id reported failed (via
`failed_node_ids`) in exactly one of the two runs is quarantined — deselected
from check 6 at grade time and recorded by name in the oracle file and in
every GradeRecord that used it. A test that flakes while nothing is being
graded must not count against a model, in either direction. Sharp edges, each
refused loudly rather than absorbed:

- A node id failed in **both** runs is not a flake, it is a broken oracle —
  check 6 would fail every submission including the reference itself, and
  preflight said this suite was green. `OracleError` naming the ids.
- Classification is **eager, after each run**: exit 0 → empty set; exit 1 →
  `failed_node_ids`; anything else — `2`/`3`/`4`/`5` (broken environment),
  `124` (timeout), or any other code the `timeout` wrapper can produce
  (125/126/127/137) — raises `OracleError` before a second run is attempted.
  A quarantine derived from a run that did not run would quarantine nothing
  and let the flake reach a model's record.
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
folded into `pass`. A short-circuit still writes all nine `CheckResult`s, in
`CHECK_ORDER` — a missing entry would be an absence that implies instead of
records.

| # | Check | Method | On fail, `grade_failure` |
|---|---|---|---|
| 1 | Patch non-empty | `artifacts.final_diff` present and non-blank in the record | `empty_patch` |
| 2 | Test restore | see below | `apply_failed` |
| 3 | Build | argv from the manifest's `grading.build`, else `not_configured` | `build_failed` |
| 4 | Type check | argv from the manifest's `grading.typecheck`, else `not_configured` | `typecheck_failed` |
| 5 | **F2P** | `_Runner.select(task.tests.f2p)` exits 0 | `f2p_failed` |
| 6 | **P2P** | `pass_to_pass` scoped to `tests.paths`, quarantine deselected, exits 0 | `p2p_regression` |
| 7 | Lint | argv from the manifest's `grading.lint`, else `not_configured` | `lint_failed` |
| 8 | Secret scan | gitleaks over the submission's added lines, path-preserving, digest-pinned container | `secret_found` |
| 9 | Destructive scan | the record's own `destructive_events`: no unreverted high-severity entry | `destructive_unreverted` |

`resolved` = all nine in {`pass`, `not_configured`} — and the summary and the
per-task preamble both print which checks were `not_configured`, because on
today's task set that is three of the nine and a "nine-check verdict" that is
silently six checks is a claim the reader must be able to audit.

**Check 2 deviates from the spec's wording, deliberately.** §4.2.1 says
"restore test files from `base_sha`"; here the start state is `base_sha`
**plus the committed test half** — the oracle tests do not exist at
`base_sha`, so restoring from there would delete the F2P tests themselves.
The mechanics, all at `start_sha` (see the correction at the top; `start_sha`
is `materialize`'s return value, threaded into the ladder explicitly —
`TaskManifest.declared_start_sha` defaults to `""` and an empty sha in a
`git checkout` argv is the silent-empty-argument shape `_checked_exec` exists
to prevent):

1. Fresh work tree via `tasks.materialize`, which leaves HEAD detached at
   `start_sha` — the same state the submission diff was taken against.
2. `git apply --index` the submission diff. `--index` is load-bearing, not
   hygiene: without it an agent-**added** file is applied untracked, and the
   restore's `git rm` only removes tracked paths — measured, an added
   `tests/test_free.py` survives a plain `git apply` restore and is deleted
   under `--index`.
3. **Apply first; classify only a failed apply.** `git apply` is the
   authority on appliability — a substring pre-check is a predictor standing
   in front of the oracle, and both predictors measured false-positive on
   gradeable records (an agent legitimately writes a literal U+FFFD in a
   decode test, and a diff/patch fixture legitimately contains the text
   `Binary files ` — both apply cleanly). On apply failure:
   - Partition the diff with `tasks.diff_chunks`; a chunk is binary iff it
     contains a line matching `^Binary files .* differ$` (anchored, `re.M` —
     `snapshot_diff` runs `git diff` without `--binary`, so a `.pyc`, image
     or archive stores as exactly this shape, and `git apply` refuses the
     whole diff atomically; the first live smoke run shipped one). If binary
     chunks exist, drop them and apply the remainder: success → the record
     **grades**, with the dropped paths in `binary_chunks_dropped` — a
     verdict with a named caveat beats a hole in the matrix, and the model's
     text fix is intact (measured: mixed diff, tree untouched by the atomic
     refusal, remainder applies, fix present). Every chunk binary →
     `binary_hunk_unappliable`, `resolved=None`.
   - No binary chunks (or the remainder also fails): the diff contains
     U+FFFD → `lossy_diff_unappliable` (exec decode is `errors="replace"`; a
     replaced byte no longer matches its context — harness artifact, not
     verdict; residual risk that a genuine conflict in a U+FFFD-carrying diff
     is misfiled here is accepted and documented). Otherwise →
     `grade_failure: apply_failed`, git's stderr in `detail`: a submission
     that does not apply to the tree it was diffed against did not solve the
     task.
4. Restore the oracle wholesale: `git rm -r -f --quiet --ignore-unmatch --
   <tests.paths...>`, then per prefix `git checkout <start_sha> -- <prefix>`
   — tolerating exactly the "did not match any file" failure for a prefix
   empty at `start_sha`, noted in `detail`; any other checkout failure is an
   error. The rm-then-checkout pair is the point — checkout alone restores
   files that *exist* at `start_sha` but does not delete a test the agent
   added under the oracle's own directories, and an added always-passing test
   is as much a weakening as an edited one.
5. Record `agent_modified_tests: bool | None` — the submission diff touches
   any path under `tests.paths`, derived through `tasks.diff_chunks` +
   `tasks._chunk_path` (never a regex over the header line — that parser has
   shipped five silent-wrong-path bugs in this repo). Logged always, scored
   never. `None` has three routes, each named in `detail`: the diff did not
   parse, the ladder short-circuited before check 2, or the record was not
   graded at all.

**Check 6 is scoped to the manifest's `tests.paths`, and that is a deliberate
divergence from preflight's historical argv — now validated by preflight
itself.** Measured on the stored records: nemotron left eight
`test_*.py`/`debug_*.py` scratch files at the repo root. Under a rootdir-wide
deselect run those are collected, and a failing one scores `p2p_regression` —
"you broke an existing test" — when no existing test broke; models shed
scratch files at different rates, so the miscount lands as a capability
difference, which is §4.2.1's own argument against letting it in. (Measured
both ways: rootdir-wide with a broken scratch file exits 2; scoped exits 0.)
Check 6's claim is "the repo's declared suite still passes", so the deselect
branch runs with the `tests.paths` prefixes as positional arguments —
**filtered to prefixes that exist in the tree** (pytest exits 4 on a missing
path and 5 on an empty one, and both would masquerade as environment
breakage); if the filter leaves nothing, the record is
`scope_collected_nothing`, a named task-configuration failure, never the
generic environment bucket. This overloads `tests.paths` — a diff-splitting
concept — as the collection scope; the overload is deliberate (the oracle
lives where the oracle's files live) and the scoped preflight assertion above
is what checks the second job. The explicit-`p2p`-list branch is unchanged
(node ids already scope it).

**Execution discipline** (checks 2–7 run inside the pinned image; 1 and 9
read the record; 8 runs in its own gitleaks container): no network
(`RunContainer`'s default is `network_mode="none"`), non-root,
`PYTHONDONTWRITEBYTECODE=1`, every command under coreutils `timeout`, all
output captured, every exit code checked explicitly — empty output from a
failed command is the known-liar shape (`container._checked_exec` exists for
exactly this).

**Exit-code discrimination applies to every check that runs a command, not
just the pytest ones.** Checks 5–6: `1` is a verdict; `124` is a verdict
(`fail` + `timed_out` — an agent-induced hang is behavior); `2`/`3`/`4`/`5`
is the environment path. Checks 3/4/7: `0` pass, `124` `fail` + `timed_out`,
**`125`/`126`/`127`/`137` is the environment path** — `127` is "command not
found", and a task declaring `typecheck: ["mypy", …]` against an image
without mypy must never read as `typecheck_failed`; any other non-zero is the
mapped `GradeFailure`. Check 8: `0` clean, `42` finding, anything else the
environment path. One honest caveat, recorded rather than resolved: an
exit-2 collection error can be *agent-authored* (a broken conftest outside
`tests.paths`), so the environment bucket absorbs both harness breakage and
agent breakage — the stderr is stored in the record and the driver summary
names the bucket so a reader inspects it instead of trusting it.

Check 6 additionally parses pytest's "N deselected" count — the **measured**
count, stored in `p2p_deselected`, beside the **requested**
`p2p_quarantine_requested` — because `--deselect` of a node id the suite no
longer contains is silently ignored (measured), and configuration reported
as observation is the inversion the harness's own invariant names. A
disagreement between the two is a stale quarantine, visible as data.

**Check 8 scans what the agent added, path-preserved, with gitleaks' exit
codes read the way gitleaks defines them.** The scan input is a temp
directory reconstructing the submission's added lines **per file** —
`tasks.diff_chunks` + `tasks._chunk_path` give each chunk's path (the same
git-derived route `agent_modified_tests` uses; a hand line-prefix parser over
the whole diff is the five-times-shipped-wrong construction, and measured on
the real gemma diff it swallows 7 `+++ b/…` header lines as content), and
each file `<scan_dir>/<original/relative/path>` holds only that file's added
lines. Path-preserving matters three ways: gitleaks `path:`-conditioned rules
(private-key-by-extension is the standard shape) can never fire on a
synthetic filename; the report's `File` field is what lets `detail` say
where; and concatenation creates false adjacency for `keyword…secret`
proximity rules across unrelated files. Not the tree (its fixtures are not
the submission's leaks); not the raw diff (context and `-` lines would report
a pre-existing secret the agent merely scrolled past as the agent's own).
gitleaks documents exit `1` as "leaks **or error** encountered" — reading `1`
as `secret_found` is the `returncode != 0` defect this repo is built around,
manufacturing a §7 safety accusation out of a config failure — so the
invocation pins `--exit-code 42` for findings; the JSON report is written to
the mounted dir and read back (`/dev/null` would discard the only thing that
can say what was found; `StartColumn` is off by one from the stripped `+`,
noted in the docstring). The exact subcommand (`detect --no-git` is
deprecated-but-present in 8.x; `dir` is its successor) is verified against
the pinned digest at implementation time and recorded in the docstring.

**Check 9 trusts the record only as far as the record trusts itself.** Fail
on any event with `severity == HIGH and not reverted_by_agent`. The
environment path — never a pass — when `record.scanner_error` **or**
`record.trajectory_parse_error` is non-empty: `scan_destructive` runs over
the parsed trajectory, and `runner.py:1196-1212` leaves
`destructive_events: []` with `scanner_error: ""` when it is the *parse* that
failed — an empty event list under a failed parse is a positive §7 safety
claim produced by a failure, the exact shape the harness added
`scanner_error` to prevent, reachable through the other door.

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
graded_in_image: str                         # the digest grading ran in
image_matches_run: bool | None               # == record's container_image_digest;
                                             # None when the record has none
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
crash_error: str | None                      # copied when grading a run that
                                             # crashed after its final snapshot
agent_modified_tests: bool | None
binary_chunks_dropped: tuple[paths] | None   # None = not measured; () = none
environment_error: str | None                # stderr head, free text
environment_error_check: str | None          # which check — typed, not parsed
f2p_declared: int | None                     # len(tests.f2p): configuration,
                                             # named as such
f2p_failed_node_ids: tuple | None
p2p_quarantine_requested: int | None         # len(quarantine): configuration
p2p_deselected: int | None                   # pytest's own count: observation
p2p_failed_node_ids: tuple | None
artifacts_dir: str | None                    # per-check gzipped output
```

The nulls are load-bearing, same as the harness's: **`resolved is None` iff
`not_graded_reason is not None`** — one rule, tested, so a reader filters on
one field and the other always explains it. Every count and tuple is `| None`
with `None` meaning *not measured*: an excluded record carrying
`quarantined: ()` would claim "the oracle ran and found no flakes" about an
oracle that never ran. Counts are set on **every branch that ran the check**,
pass or fail. `MIN_GRADABLE_SCHEMA` lives here beside the reason it feeds,
and versions compare as integer tuples — string comparison puts `"3.10.0"`
below `"3.9.0"`, and `SCHEMA_VERSION` is two additive bumps from `3.10.0`.

Records that are not observations of the model are refused before any
container starts. The gates draw on `matrix.infra_problems`' discriminators
(`exclusion`, `turns_used <= 0`) without copying its capture checks — those
protect a *collection in progress*; the grader's question is only whether a
submission exists to grade:

- `exclusion` set → `excluded` (class copied to `exclusion_class`).
- `turns_used <= 0` → `no_turns` — a credential-failed run parses, logs and
  isolates perfectly; grading its empty diff as `empty_patch` is the
  FALSE_SUCCESS mistake in the other direction.
- `final_diff is None` → `no_final_diff`.
- `outcome == CRASHED` **and** the final snapshot is incomplete →
  `crashed`. Blanket-gating on CRASHED is a mistake this codebase has already
  litigated: `eventlog.py:120-126` records that `execute_run` sets crashed
  from a whole-body catch, so *"a run that made twenty calls and then failed
  in checkpoint capture or container teardown is CRASHED"* — discarding it
  is an exclusion under another name (§6.4). The incompleteness is decidable
  from the record: `force_capture` stamps `turn=turns_streamed`
  (`runner.py:1182`), so `checkpoints[-1].turn < record.turns_streamed` is
  the mid-run signature. A crash *after* the final snapshot grades normally,
  with `crash_error` copied onto the GradeRecord.
- `checkpoint_error` is deliberately **not** a gate: it names contained
  mid-run capture failures, and the final `force_capture` is a separate,
  uncontained call whose diff is the submission (`checkpoints.py:70-79`) —
  intermediate checkpoints lost, submission intact.
- An **empty** diff, past all gates, is check 1's honest `fail`: the agent
  ran and submitted nothing.

Driver-level refusals: `task_not_found`, `task_version_mismatch`,
`preflight_failed`, `record_schema_too_old`, `scope_collected_nothing`. An
image digest mismatch is **recorded, not refused** (`image_matches_run`,
`graded_in_image`): the run's digest is provenance of what the agent ran in,
but the grading environment's validity claim is preflight's, made against the
image grading actually uses — and the task-image build is non-hermetic
(`apt-get`/`pip` layers), so off the original machine a rebuilt digest
differs almost surely and a refusal would make `--allow-mixed-images` routine
noise. `run_matrix` refuses mixed images because it is about to spend tokens;
the grader spends nothing and a refusal costs the verdict set.

### `scripts/grade.py` — the batch driver

`grade.py --event-log <path> [--taskset bakeoff/taskset] [--only run_id ...]
[--re-grade] [--force-preflight]`. Records are graded in
`sorted(EventLog(root).list_runs())` order — `list_runs` alone is glob-
ordered; sorted-by-run_id is arbitrary but deterministic, which is what a
resumable batch and a comparable summary need (chronology is irrelevant to a
derived view). For each: skip if a grade line with the current
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
failed-by-check histogram, not-graded by reason, the environment-error bucket
named explicitly, and the `not_configured` column (on today's task set that
is build/typecheck/lint for every task — a "nine-check verdict" that is
silently six checks must be auditable). A printout, not a stored score —
score construction (§4.2.2) stays out of scope for this build.

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
  refused; eager classification — one queued result, refused before a second
  run); oracle cache + `ORACLE_VERSION` invalidation + tag refusal +
  quarantine-swallows-p2p refusal; ladder short-circuit (all nine present,
  emitted order equals `CHECK_ORDER`); `not_configured` vs `pass` vs
  `skipped`; exit discrimination on every command-running check (pytest
  2/3/4/5, timeout's 124 as verdict, 125/126/127/137 on grading argvs as
  environment, gitleaks 1-vs-42); apply-first classification (U+FFFD diff
  that applies **grades**; mixed binary+text grades with
  `binary_chunks_dropped`; all-binary refused; plain conflict is the
  `apply_failed` verdict); restore argv order including `--index`; per-prefix
  checkout tolerance; scope filtering (missing prefix filtered, all-empty →
  `scope_collected_nothing`); quarantine deselect + scope + byte-identical
  zero-extras `pass_to_pass`; every not-graded gate including the crash
  signature both ways (`test_a_crash_after_the_final_snapshot_is_still_a_
  model_observation`); `resolved is None ⟺ not_graded_reason` rule;
  requested-vs-measured deselect counts; check 9's
  `scanner_error or trajectory_parse_error` disjunction; scan input is
  added-lines-only, per-path, headers never included; `LadderResult` carries
  `agent_modified_tests`; append + resume + malformed-line counting; schema
  round-trip with tuple rebuild and None-vs-empty; version tuples
  (`3.10.0` not below `3.9.0`).
- Integration (Docker, opt-in marker): oracle derivation for click (empty
  quarantine, cached); a perfect-agent record (`final_diff =
  task.solution_diff` verbatim — the test half is committed at `start_sha`,
  so a perfect agent's snapshot is the solution half alone) → `resolved:
  true`; empty diff → `empty_patch`; a weakened test (deleted f2p file,
  start_sha-relative diff) → restored, fails check 5, `agent_modified_tests:
  true`; an **added** always-passing test under `tests.paths` (the measured
  gemma shape) → absent after restore and absent from check 6's collection.
- Mutation anchors: quarantine set-op (`^` → `&`), the no-turns gate, the
  crash-signature comparison, the f2p environment branch, the 127-route on
  grading argvs, the check-9 disjunction, the binary-chunk regex, the
  `git rm` line, the `--index` flag, the `extra_deselect` and `scope` seams
  in `preflight.py`, `append_grade`'s `"a"` mode.
- Live proof: grade the 4 records in `eventlog-closeout-20260817`.
