# Offline grader — design

2026-08-17. Implements spec §4.2.1 (the nine deterministic checks), §4.2.2
(score construction inputs), and the §5.5 constraint that grading is an offline
batch over stored diffs. Closes TASKS.md readiness blocker 5: *"until this
exists nothing in the log is a result."*

Revision 6. Five review rounds (111 findings) are folded in; the corrected
facts are stated inline where the wrong versions stood.

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
  re-prove it, it *requires* it — `grade.py` reads the same `task_id`-keyed
  cache `run_matrix` writes (whose value carries the composite
  `manifest_digest|image|start_sha|PREFLIGHT_VERSION` key, and which only
  ever holds PASS verdicts — the property that makes a read-only hit safe),
  read-only; a miss runs preflight and records the verdict in the grader's
  own cache file, never mutating the driver's. A preflight failure makes
  every record of that task `not_graded_reason: "preflight_failed"` rather
  than a verdict either way. `graded_under_preflight_version` on a cache
  hit is the module constant — sound precisely because the version is in
  the key that hit; on a miss it comes from the `PreflightResult` just
  produced.
- **P2P** = `task.tests.p2p` when declared, else everything-except-F2P via
  `--deselect` — through `preflight._Runner.pass_to_pass`, extended with
  default-empty `extra_deselect` and `scope` parameters so preflight's own
  invocations stay byte-identical (a test pins that) and the grader's
  quarantine and suite scoping ride the same implementation instead of a
  hand-built second copy of the branch logic.

**Preflight grows two assertions so the gate validates what the grader will
actually run — and a version in its cache key so they actually run.** (1) On
the deselect branch only (`if not tests.p2p` — an explicit p2p list ignores
`scope` by design, so the assertion would re-run a selection preflight
already validated, paying a suite run to assert nothing), the *scoped*
`pass_to_pass` is run once at the post-reference-fix state **with the same
per-prefix `test -e` filter the grader's check 6 applies** — otherwise the
gated argv and the graded argv differ on exactly the input the restore step
already tolerates (a declared prefix absent at `start_sha`, legal per
`_validate_prefixes`), and preflight would NO-GO a task the ladder was built
to grade. A filtered-out prefix is recorded in `evidence` and
`problem_codes` — **never appended to `problems`**, because `ok` is `not
problems` and a problem would be the NO-GO the filter exists to prevent,
re-armed one line down; the code keeps the author error loud without making
it fatal, and the ladder's own tolerance for the same input stays live. The run must exit 0; pytest exit 5 is the specific
collects-nothing problem. Problems are reported through a **typed channel**
— `PreflightResult.problem_codes` carrying `"scope_collects_nothing"` — not
a prose prefix the driver string-matches: the repo owns this dataclass, and
"closed sets, not composite strings" applies to its own gate before it
applies to anyone's grades. (The first draft claimed rootdir-wide green
"strictly implies" scoped green — false in general, since collection scoping
changes fixtures and ordering; the claim is replaced by a measurement.
Detection is the exit code alone: the pinned runner carries `-q`, which
suppresses the "collected 0 items" line, and a guard that cannot fire under
the configuration actually used is the dead-guard shape CLAUDE.md names.)
(2) Each declared `grading.*` argv must exit 0 at the post-fix state —
otherwise a typo'd `typecheck: ["mypy", …]` against an image without mypy
surfaces as `typecheck_failed` on every record of the task, permanently,
instead of a NO-GO before anything is graded.

The assertions cost: preflight goes from four suite invocations to five (the
oracle adds two more per task). And because `run_matrix.main` hard-stops when
**any** task NO-GOs, the `PREFLIGHT_VERSION` bump must be followed by
re-gating the full task set *before* a live collection starts — a task that
newly fails the scoped assertion mid-campaign would otherwise halt the
matrix. `PreflightResult` also records `preflight_version` in its stored
verdict, and every grade copies it as `graded_under_preflight_version` — a
stored NO-GO must say which gate produced it, and the design's central claim
("discrimination is preflight's claim; the grader requires it") makes
preflight an input every grade has to name.

**`PREFLIGHT_VERSION` joins the cache key.** The key was
`manifest_digest|image|start_sha` — and none of the three moves when
`preflight.py` gains assertions, so every warm cache would serve a PASS
written by the *old* gate and both new assertions would be inert on exactly
the tasks about to be graded. This is the pruned-mirror closeout's
"older revision's output served forever" defect, one subsystem over, and it
gets the same fix: `PREFLIGHT_VERSION` in the key (both in `run_matrix`'s
cache and the grader's), bumped with any change to what preflight asserts.
Existing cache entries must miss after the bump — that is the verification,
not a side effect.

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
| 5 | **F2P** | `_Runner.select(task.tests.f2p)` exits 0. A non-{0,1} exit whose reported `ERROR` lines are all bare module paths **contained in** the declared f2p modules is `f2p_failed` (the submission left an import broken — see broadening 2); anything else is an environment error | `f2p_failed` |
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
difference, which is §4.2.1's own argument against letting it in. (Measured:
a scratch test failing at *runtime* makes the rootdir-wide run exit 1 —
`p2p_regression` — while the scoped run exits 0; five of nemotron's eight
root scratch files are collectible under pytest's default `python_files` —
four `test_*.py` plus one `*_test.py`. A scratch file broken at *import*
exits 2, which the ladder already routes to the environment path — a
different miscount, same scoping fix.)
Check 6's claim is "the repo's declared suite still passes", so the deselect
branch runs with the `tests.paths` prefixes as positional arguments —
**filtered to prefixes that exist in the tree** (pytest exits 4 on a missing
path), and with **exit 5 after a non-empty filter routed to
`scope_collected_nothing` as well**: `test -e` passes an existing-but-empty
directory (measured), pytest then exits 5, and that is a task-configuration
fact, not environment breakage. If the filter leaves nothing, same reason.
With `PREFLIGHT_VERSION` in place this branch should be unreachable —
preflight's scoped assertion proves collection before grading starts — and
the ladder keeps it as defence in depth, commented as such. This overloads `tests.paths` — a diff-splitting
concept — as the collection scope; the overload is deliberate (the oracle
lives where the oracle's files live) and the scoped preflight assertion above
is what checks the second job. The explicit-`p2p`-list branch is unchanged
(node ids already scope it).

On the explicit-`tests.p2p` branch, a declared id that produced no terminal
status routes to the environment path **ahead of** the pass, fail and timeout
branches — the quarantine excluded, because a quarantined id was asked not to
run by the same argv and is not evidence of anything going wrong. The measured
shape (round-2 item 8): a `-t` pattern naming a test that no longer matches
exits **0** on both vitest and jest with every test in the file reported
skipped, so a PARTLY stale selection runs what still matches, passes, and
reaches this check at exit 0 — a `resolved: True` verdict over a regression
check part of which never ran, invisible to the exit code, to
`p2p_deselected`, and to `p2p_failed_node_ids`. What can explain a
non-quarantined id with no terminal status: a stale manifest; a rename the
harness cannot see; a selection argv the harness built wrong; or a runner
config the submission edited outside `tests.paths` (a root `vitest.config.ts`
/ `jest.config.js` / `package.json` `exclude`, `testMatch` or `setupFiles`) —
the restore step (check 2) puts every *tracked file* under `tests.paths` back,
but a config file the submission edited there is put back too, so this fourth
cause is really "the id lives under a scope the restore does not narrow to a
single file". The branch outranks `KIND_PASSED` (a green report would absorb
the loss silently), `KIND_FAILED` (a partially-executed selection is not the
run "the declared p2p set still passes" describes, even when one id in it did
fail) and the timeout branch (doubly not a statement about the model) alike.
`GradeRecord.not_run_node_ids` carries the ids beside the message, because a
node `fullName` may itself contain `", "` and the joined prose is not
losslessly splittable back into ids.

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
count, stored in `p2p_deselected` — because `--deselect` of a node id the
suite no longer contains is silently ignored (measured), and configuration
reported as observation is the inversion the harness's own invariant names.
Two absences are kept apart: a summary line found with **no** `deselected`
token is `p2p_deselected = 0`, an observation — measured, pytest prints no
token at zero, and the wholly-stale-quarantine case (the total loss this
field exists to catch, on the explicit branch where nothing floors the
count) would otherwise render as "not measured"; `None` only when no summary
line was found at all. "Found" is a pinned pattern, not a vibe: the `-q`
summary is the final non-empty line ending `in <seconds>s` (verified at
implementation against the image's pytest 8.3.5 and recorded in the
docstring) — get the discriminator wrong and the distinction inverts, a
real zero reading "not measured" or a suite that produced nothing reading
as an observed zero. The comparable baseline is stored beside it as
`p2p_deselect_requested`: on the deselect branch pytest's count covers the
f2p deselects *and* the quarantine, so it reads `len(f2p) +
len(quarantine)` (measured: 2 f2p ids + 1 quarantined = "3 deselected") and
comparing it to the quarantine length alone would disagree by `len(f2p)` on
every record of every deselect-branch task — a false alarm that buries the
signal. `p2p_quarantine_requested` stays as the quarantine's own share. The
stale-quarantine invariant is `p2p_deselected < p2p_deselect_requested`,
and it is **one-directional**: the baseline counts node *ids* while pytest
counts *items*, and they coincide only because preflight's `missing` check
forces f2p ids to be leaves (a class or parametrize-base id deselects many
items — measured) — so a shortfall is evidence of staleness, equality is
not evidence of freshness, and the coupling is recorded in the field's
docstring.

**Check 8 scans what the agent added, path-preserved, with gitleaks' exit
codes read the way gitleaks defines them.** The scan input is a temp
directory reconstructing the submission's added lines **per file** —
`tasks.diff_chunks` + `tasks._chunk_path` give each chunk's path (the same
git-derived route `agent_modified_tests` uses; a hand line-prefix parser over
the whole diff is the five-times-shipped-wrong construction, and measured on
the real gemma diff it swallows 7 `+++ b/…` header lines as content), and
each file `<scan_dir>/<original/relative/path>` holds only that file's added
lines. Keyed on `_chunk_path`'s **destination** path (a renamed file must
land under its new name); chunks contributing no added lines (rename-only,
mode-only, deletions) are skipped rather than written as empty files. Both
this and `agent_modified_tests` are computed over the **full stored diff**,
never the binary-filtered remainder — the agent touched those paths whether
or not the chunk was appliable. Path-preserving matters three ways: gitleaks `path:`-conditioned rules
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
a **separately mounted report dir**, never into the scan root — the report
holds the secret values gitleaks found, and a report inside the scan dir
becomes an input to any re-invocation over the same directory, with
self-referential findings attributed to `report.json` instead of the agent's
file. (`/dev/null` would discard the only thing that can say what was
found; `StartColumn` is off by one from the stripped `+`, noted in the
docstring.) The per-chunk build runs in a temp dir **outside any
repository** — `_numstat`'s own docstring pins that a repo-subdirectory cwd
filters the patch to the cwd prefix and reports zero entries silently, and
`grade.py`'s cwd is normally inside `bakeoff/`. The exact subcommand
(`detect --no-git` is deprecated-but-present in 8.x; `dir` is its successor)
is verified against the pinned digest at implementation time and recorded in
the docstring.

**Check 9 trusts the record only as far as the record trusts itself.** Fail
on any event with `severity == HIGH and not reverted_by_agent`. The
environment path — never a pass — when `record.scanner_error` **or**
`record.trajectory_parse_error` is non-empty: `scan_destructive` runs over
the parsed trajectory, and `runner.py:1196-1212` leaves
`destructive_events: []` with `scanner_error: ""` when it is the *parse* that
failed — an empty event list under a failed parse is a positive §7 safety
claim produced by a failure, the exact shape the harness added
`scanner_error` to prevent, reachable through the other door. The
disjunction is three-term — `scanner_error or trajectory_parse_error or
assembly_error` — with three different reachabilities: `scanner_error` is
live through `grade_run`; `assembly_error` is live (the assembly-failure
record grades, and its fabricated-empty fields are exactly why this check
cannot claim clean); `trajectory_parse_error` is documented as
**unreachable through `grade_run`** (a parse failure resets the parsed
trajectory, so `turns_used == 0` and the `NO_TURNS` gate dominates —
`runner.py:533-566`, `:670`) and is kept because the invariant belongs to
the check, not to one caller's gate ordering. Its test drives `run_ladder`
directly, below the gates, for the same reason — and it carries no mutation
anchor: an anchor whose only witness is an impossible record certifies
nothing.

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
grader_commit: str                           # runner.harness_commit(), -dirty
                                             # included: grader_version gates
                                             # resume, this is the evidence the
                                             # gate was honest — a forgotten
                                             # bump otherwise serves last
                                             # week's verdicts forever
graded_under_preflight_version: str
graded_in_image: str                         # the digest grading ran in
image_matches_run: bool | None               # == record's container_image_digest;
                                             # None when the record has none
graded_against_manifest_digest: str          # a task edited without a version
                                             # bump moves this and nothing else
graded_against_task_set_commit: str          # "" when the task set is not a
                                             # repo — the honest blank
                                             # task_set_commit already uses
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
not_graded_detail: str | None                # the message the reason promises:
                                             # the OracleError text, the joined
                                             # preflight problems, declared-vs-
                                             # found versions — a named bucket
                                             # with no cause is the defect one
                                             # layer down
exclusion_class: str | None                  # the record's own, when excluded
crash_error: str | None                      # copied when grading a run that
                                             # crashed after its final snapshot
assembly_error: str | None                   # copied when grading a
                                             # minimal-record run — its zeros
                                             # are fabrications, its diff is
                                             # real
agent_modified_tests: bool | None
binary_chunks_dropped: tuple[paths] | None   # None = not measured; () = none
environment_error: str | None                # stderr head, free text
environment_error_check: str | None          # which check — typed, not parsed
f2p_declared: int | None                     # len(tests.f2p): configuration,
                                             # named as such
f2p_failed_node_ids: tuple | None            # on a collection error these are
                                             # MODULE paths (no `::`) -- what
                                             # pytest reported; a module is a
                                             # node id, the collector node
p2p_quarantine_requested: int | None         # len(quarantine): configuration
p2p_deselect_requested: int | None           # what pytest was actually asked:
                                             # f2p + quarantine on the deselect
                                             # branch, quarantine alone on the
                                             # explicit branch
p2p_deselected: int | None                   # pytest's own count: observation
p2p_failed_node_ids: tuple | None
not_run_node_ids: tuple | None               # declared ids with no terminal
                                             # status, quarantine excluded;
                                             # None on pytest, where the exit
                                             # code carries this instead
artifacts_dir: str | None                    # per-check gzipped output, at
                                             # artifacts_root/<run_id>/v<GRADER_VERSION>,
                                             # emptied before the ladder so the
                                             # path is an ownership claim; pre-
                                             # layout lines point at the v-less
                                             # dir, orphaned rather than clobbered
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
- `outcome == CRASHED` **and not** `snapshot_complete` → `crashed`, where

  ```
  snapshot_complete = (bool(checkpoints)
                       and checkpoints[-1].turn == turns_streamed)
  ```

  Blanket-gating on CRASHED is a mistake this codebase has already litigated:
  `eventlog.py:120-126` records that `execute_run` sets crashed from a
  whole-body catch, so *"a run that made twenty calls and then failed in
  checkpoint capture or container teardown is CRASHED"* — discarding it is an
  exclusion under another name (§6.4). The signature is stated **positively**
  because the negative form fails open: a crash *during* the agent loop —
  the only way `checkpoints[-1]` is genuinely mid-run — leaves
  `runner_result` unassigned, so `turns_streamed` is 0 (`runner.py:676`) and
  a `checkpoints[-1].turn < turns_streamed` comparison is False exactly when
  it must fire. Completeness is decidable from both stamps: `force_capture`
  writes `turn=turns_streamed` (`runner.py:1182`) while every mid-run
  capture writes `turns - 1` under a `turns > 1` guard
  (`claude_runner.py:408-409`), so equality holds iff the last snapshot is
  the final one — **including at `turns_streamed == 0`**: a crash mid-loop
  leaves a mid-run last checkpoint with `turn >= 1 != 0` (gated), while a
  run whose stdout reader undercounted to zero but whose `force_capture`
  completed carries `turn == 0 == turns_streamed` (grades — refusing it
  would gate on a stdout undercount, which `stdout_malformed_lines` exists
  to admit happens). An earlier revision added a `turns_streamed > 0`
  conjunct as "belt-and-braces"; measured, it false-gated exactly that
  second case, so the equality stands alone. A crash *after* the final
  snapshot grades normally, with `crash_error` copied onto the GradeRecord.
  **Gate order is load-bearing**: `assembly_error` (below) and `NO_TURNS`
  precede `CRASHED` — a crash inside `runner.run` leaves no transcript at
  all, `turns_used == 0`, and that row is a no-turns row, not a
  crash-signature question. The `bool(checkpoints)` conjunct is
  belt-and-braces through `grade_run` (`not checkpoints` implies
  `final_diff is None`, already gated at both assignment sites,
  `runner.py:828` and `:925`) — kept because the equivalence is a property
  of one caller, and documented so tests are not written against internally
  inconsistent hand-built records (crash-fixture checkpoints pin
  `turn >= 1`, matching what `maybe_capture` can actually stamp).
- `assembly_error` non-empty is **not a gate — it disarms the gates that
  read fabricated fields, and the record grades.** `_minimal_record`
  fabricates `turns_used=0` and `turns_streamed=0` by its own docstring's
  admission while carrying real checkpoints and a real `final_diff` — a
  submission exists, and "the grader's question is only whether a submission
  exists to grade" (revision 5 renamed the bucket and kept the refusal,
  which was the blanket-CRASHED mistake with a new label — an exclusion
  under another name). So: `assembly_error` set → skip `NO_TURNS` and
  `CRASHED` (both would read the fabricated zeros), grade normally (a `None`
  diff still gates as `no_final_diff`), copy `assembly_error` onto the
  GradeRecord beside `crash_error`, and check 9 takes the environment path —
  `_minimal_record` does not set `trajectory_parse_error`, so its empty
  string is itself a fabricated claim and an empty event list under it would
  manufacture the §7 positive the check exists to refuse.
- `checkpoint_error` is deliberately **not** a gate: it names contained
  mid-run capture failures, and the final `force_capture` is a separate,
  uncontained call whose diff is the submission (`checkpoints.py:70-79`) —
  intermediate checkpoints lost, submission intact.
- An **empty** diff, past all gates, is check 1's honest `fail`: the agent
  ran and submitted nothing.

Driver-level refusals: `task_not_found`, `task_version_mismatch`,
`preflight_failed`, `record_schema_too_old`, `oracle_failed`, and
`task_setup_failed` — each with the cause in `not_graded_detail`. An
`OracleError` is the loudest failure `oracle.py` can raise, and letting it
fall into the driver's generic per-record `errors` bucket would make a broken
oracle indistinguishable from a grader bug; the same argument covers the
rest of per-task setup, which today has no bucket at all: `build_task_image`
raises `ImageError` (uncaught, it kills the whole batch), `materialize`
raises `TaskError`, the inherited-`ENTRYPOINT` check is a refusal — and
`preflight()` and the oracle derivation both run containers, so the region
also raises `ContainerError` and, one layer down, docker's own exceptions.
The catch is therefore `except Exception` around per-task setup, type name
in `not_graded_detail` — the driver's stated posture is continuing loudly
over dying with a partial batch, and a named-tuple catch re-arms the
kill-the-batch door for every exception type nobody listed (in `run_matrix`
a narrow catch is right because nothing has been spent; the grader's
contract is different). `OracleError` keeps its own bucket; everything else
in setup maps to `task_setup_failed` on every record of the task. A
preflight NO-GO whose `problem_codes` carries `scope_collects_nothing` maps
to `scope_collected_nothing` rather than the generic `preflight_failed` (the
ladder's own filter for the same condition is defence in depth and should be
unreachable). An image digest mismatch is **recorded, not refused**
(`image_matches_run`, `graded_in_image`): the run's digest is provenance of
what the agent ran in, but the grading environment's validity claim is
preflight's, made against the image grading actually uses — and the
task-image build is non-hermetic (`apt-get`/`pip` layers), so off the
original machine a rebuilt digest differs almost surely and a refusal would
make `--allow-mixed-images` routine noise. `run_matrix` refuses mixed images
because it is about to spend tokens; the grader spends nothing and a refusal
costs the verdict set. The mismatch is auditable, not buried: the per-model
summary carries an `image_matches_run` false-count and the batch prints a
banner when it is non-zero, because a resolve rate produced entirely in a
rebuilt image is a claim the reader must be able to see — the same argument
as the `not_configured` column.

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
failed-by-check histogram, not-graded by reason, the environment-error
bucket **as a per-check histogram** (`environment_error_check` is typed
precisely so this is a groupby — and it is what lets a reader see that one
arm's environment bucket is all `p2p`, i.e. plausibly model-caused
not-grading, a §6.4 exclusion-under-another-name in the making), the
`image_matches_run` false-count, and the `not_configured` column (on today's
task set that is build/typecheck/lint for every task — a "nine-check
verdict" that is silently six checks must be auditable). A printout, not a
stored score — score construction (§4.2.2) stays out of scope for this
build. The batch also prints a banner when it resumes across differing
`grader_commit`s under one `grader_version`.

One structural caveat the ladder carries deliberately: the classification
path calls `tasks.diff_chunks`/`_chunk_path` on the *submission* diff, and
those raise `TaskError` on shapes a reference diff never has (a
`diff.noprefix` header is the reachable one — `snapshot_diff` runs plain
`git diff` under the image's config, and measured, a noprefix diff fails
*both* the parse and the apply, from the one cause). Every such call is
wrapped, and **a parse failure is always a harness artifact**: during apply
classification it takes the environment path (`environment_error_check:
"test_restore"`) — an earlier revision degraded it to the `apply_failed`
verdict on the reasoning "the apply already failed; the question was only
why", which is circular: the *why* is what decides whether the failure is
the model's, and a `diff.noprefix` image would have stamped a permanent
cross-arm `resolved: False` on every record. `apply_failed` is reserved for
the case it means: the diff parses, and git still refuses it. During check
8 a parse failure likewise takes the environment path, and for
`agent_modified_tests` it is the already-specified `None`-with-reason. A
raise escaping the ladder would convert a record one caveat from a verdict
into a driver `errors` entry. (`diff_chunks`' error text tells the operator
to re-cut the *reference* diff — remediation for task authors; the wrapped
detail says "submission diff" so the message cannot mislead.)

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
- The §4.2.2 scoring pass — but one constraint carries forward now: analysis
  is paired, and a record-level not-graded cell (`environment_error`,
  `crashed`, `no_turns`, `binary_hunk_unappliable`) breaks its task's pair
  for that arm while a task-level refusal drops the task for all arms and
  preserves pairing. The scoring pass must read `not_graded_reason` and turn
  a broken pair into a broken pair — never into a zero.

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
  signature **three** ways (crash-after-snapshot grades; crash between the
  last mid-run capture and `force_capture` gated; crash *during* the agent
  loop — checkpoints with `turn >= 1`, `turns_streamed=0` — gated, the case
  the negative formulation failed open on) plus the gate-order witnesses
  (`outcome=CRASHED` with `turns_used=0` is a no-turns row; `assembly_error`
  set disarms the fabricated-field gates, grades, and check 9 is the
  environment path); a stale
  preflight cache verdict is not served across a `PREFLIGHT_VERSION` bump;
  `resolved is None ⟺ not_graded_reason` rule; deselect counts with a
  **non-empty f2p list on the deselect branch** (requested-total 9, pytest
  reports 8 — the offset is `len(f2p)` and a quarantine-only comparison
  false-alarms on every record) and the zero-token rule (summary line
  present, no `deselected` token → `0`, an observation; `None` only with no
  summary line); an `OracleError` marks the task's records `oracle_failed`
  with its message in `not_graded_detail`, not the error bucket; an
  `ImageError`/`TaskError`/ENTRYPOINT refusal in per-task setup marks them
  `task_setup_failed` and the batch survives; an unparseable submission diff
  is the environment path, never `apply_failed`; check 9's
  `scanner_error or trajectory_parse_error` disjunction; scan input is
  added-lines-only, per-destination-path, headers never included, empty
  chunks skipped, built from the full diff; `LadderResult` carries
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
- Mutation anchors: quarantine set-op (`^` → `&`), the oracle's both-fail
  `raise`, the no-turns gate, the crash-signature equality (`==` → `<=`,
  witnessed by the gated crash-before-snapshot case), the restore checkout's
  `start_sha` → `task.base_sha` (the design's single most load-bearing
  correction), the f2p environment branch, the 127-route on grading argvs,
  the gitleaks `42`-vs-`1` branch, the binary-chunk regex, the `git rm`
  line, the `--index` flag, the `extra_deselect` and `scope` seams and
  `PREFLIGHT_VERSION`'s presence in the key, the driver's
  `scope_collects_nothing` code mapping, `append_grade`'s `"a"` mode. The
  check-9 disjunction deliberately has **no** anchor (unreachable witness —
  see check 9). Every anchor's witness is verified killable before the entry
  lands — an anchor that cannot go red certifies a guarantee nothing holds.
- Live proof: grade the 4 records in `eventlog-closeout-20260817`.
