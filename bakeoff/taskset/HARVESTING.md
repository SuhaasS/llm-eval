# Harvesting a task

What a candidate has to satisfy before it belongs in this directory, and why
each rule is here. The bar is deliberately high in one direction: a task that
is refused costs a message, and a task that is accepted wrongly costs a matrix
— every arm scored on an unverifiable guess, in an append-only log.

Three layers. **Only the first is enforced by code.** The other two fail
silently, which is what makes them the expensive ones.

---

## Layer 1 — refused by code

Nothing here needs judgment. Run the gate and read the output.

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --tasks <task_id>
```

Offline, no credentials, no spend. Needs a Docker daemon, and network the first
time a repo is mirrored.

### The loader — `src/bakeoff/tasks.py`

Runs before anything builds.

| rule | the failure it prevents |
|---|---|
| `base_sha` is 40 hex, and the repo actually contains it | `git checkout --detach` onto a SHA that does not resolve leaves the tree wherever it was; every later diff is against a state nobody chose, and the record reads as an ordinary quiet run |
| `start_sha`, when declared, equals the recomputed one | a re-cut patch or an edited manifest silently moving what every arm was asked to do |
| `prompt` is non-empty | — |
| `tests.paths` is non-empty | it is what splits `reference.diff` into the oracle half and the fix half |
| `tests.f2p` is non-empty, has no duplicates, and is disjoint from `p2p` | a task with no discriminating set; a test required to start red and never go red, which no submission can satisfy |
| `reference.diff` begins with `diff --git`, with nothing before it | a reference that is not exactly a diff is a reference nobody can reproduce |
| both halves are non-empty after the split | no oracle for the agent to run, or no reference fix to validate against |
| no rename crosses the test/solution boundary | the file would be simultaneously the agent's oracle and part of its submission, and guessing puts one inside the other |
| `strip_paths` entries are relative, `..`-free, glob-free, and are neither `.` nor `.git` | the key removes what it names: `.` matches every path (`PurePosixPath(".").parts` is `()`), `.git` would destroy the repository the submission diff is taken against, and a glob would make what is removed a property of the tree rather than of the manifest |
| no test-half or solution-half file lies under a `strip_paths` prefix | the patch would be applied onto a path that no longer exists — and dropping the chunk instead would make `solution_diff` something other than the merged PR, or shrink the oracle, with every gate still green |
| every `strip_paths` entry matches a file TRACKED at `base_sha` (checked in `materialize`) | a typo strips nothing and leaves the file the task was cut to remove; the preflight context-file check knows four names, so a mistyped vendored tree passes every gate. It also means a strip cannot name a file the PR *creates* |
| `task_id` is unique in the set | `run_id` is `sha256(task|model|sample|attempt)`, so two tasks sharing an id collide in the event log — discovered at the far end of a matrix, after the tokens are spent |
| `image.env` keys are in the allowlist (`CI`, `HYPOTHESIS_STORAGE_DIRECTORY`) and are not a key the harness itself sets; values carry none of `\n` `\r` `"` `\\` `$`; `HYPOTHESIS_STORAGE_DIRECTORY` is an absolute path outside `/repo` | an allowlist because a denylist would have to anticipate `CLAUDE_CODE_USE_BEDROCK`, which bypasses the proxy and leaves the wire log empty with the run still looking normal; a harness-owned key applying to preflight and the grader but overridden on the agent's own process — two environments for one task; a value that does not survive a generated `ENV KEY="value"` Dockerfile line; hypothesis writing into the tree the §5.6 submission diff is taken against |

### Preflight — `src/bakeoff/preflight.py`

Runs inside the pinned image, before the proxy starts.

- `tests.runner` contains `pytest`. The red/green distinction is built on
  pytest's exit codes and nothing else can currently supply it.
- The image is non-root, `claude --version` matches the base pin, `git` and
  `rg` are present.
- The container's HEAD is `start_sha`.
- **No `CLAUDE.md`, `AGENTS.md`, `.claude` or `.cursorrules` in the start
  state.** §5.2 pins the session config precisely because agent files
  substantially change behaviour; a task-local one gives this task a context
  the others do not have.
- **Every `strip_paths` entry is absent from the start state.** Checked in the
  container against the tree, not against the manifest: a strip that silently
  did not happen puts the file in every arm's context and in every submission
  diff, and no later stage re-derives it. The probe is `-e` **or** `-L`, so a
  symlink whose target was stripped still counts as present.
- **Every `image.env` key holds its declared value inside the container.** Read
  back with `printenv`, whose exit code separates "set to the empty string"
  from "not set at all" (measured; `echo $KEY` cannot). `image.env` is
  configuration and the container's environment is the observation, and this
  is the only place the two meet — a value that did not take is silent, and
  what it silently loses is determinism.
- **A declared test path that imports `hypothesis` is a NO-GO unless the
  manifest declares `image.env: {CI: ...}`.** Availability alone does not
  trigger it — hypothesis is a common transitive dependency — so this is two
  probes: `python -c "import hypothesis"` records `hypothesis_importable`, and
  `rg` over `tests.paths` records whether the declared tests actually import
  it. Only the second firing with no declared `CI` is refused; the remedy
  (`image.env: {CI: "1", HYPOTHESIS_STORAGE_DIRECTORY:
  "/tmp/bakeoff-hypothesis"}`) makes the suite reproducible, not correct — see
  the property-based-suite bullet under Layer 2.
- **An `rg` exit code that is neither 0 (match) nor 1 (no match) is a NO-GO
  naming the argv and the exit code, never a silent "not imported".** rg is
  asserted present earlier in this list, so an unreadable path or a bad
  pattern is an environment problem preflight can see and must not read as a
  quiet `False` — that would disarm the one check that catches an undeclared
  property-based suite.
- f2p is **red** at the start state, in one of two ways, and never `0` (already
  solved). Either it exits **1** with every declared f2p id in pytest's
  FAILED/ERROR lines, or it exits **4** (or **2**) because the modules holding
  those ids could not be *collected* — accepted only when the reported `ERROR`
  lines name **exactly** the declared f2p modules, no more and no fewer, and
  p2p is green. Both halves matter: `returncode != 0` accepts a broken
  environment as evidence the bug is present, which is the Phase 0c failure,
  and the f2p run imports only the f2p modules, so the confinement parse alone
  cannot tell a missing symbol from a missing interpreter. Measured 2026-09-01
  against pytest 9.1.1 and 8.3.5: a *selected node id* whose module raises on
  import gives **4**, a directory or module-path run gives **2**, and a node id
  that simply does not exist also gives **4** but reports no `ERROR <module>`
  line at all — which is how the gate keeps a manifest typo separate from a
  task shape.
- p2p exits 0 at the start state. A regression check against an already-red
  suite cannot mean anything, and on a collection-error task it is the *only*
  observation of the rest of the suite before the fix. Preflight passes
  `--ignore=<module>` for the erroring f2p modules on that one run — without it
  the module aborts collection of the whole sweep and the baseline cannot be
  read at all. That flag is on **no other run**: the graded p2p command is
  unchanged. Two things to know when this refusal fires: the ignore paths come
  from pytest's **rootdir**-relative `ERROR` lines while `--ignore` resolves
  against the **working directory**, and a non-existent `--ignore` path is
  accepted in silence (measured) — so a repo whose rootdir is not the working
  directory gets a flag that no-ops. And if ignoring the erroring module leaves
  nothing to collect, this repo's test tree has no regression baseline outside
  that module and the task is refused. The refusal prints the argv it ran, so
  both are one glance apart.
- `git status` is clean after the suite runs. §5.6 stages everything, so
  anything the suite drops lands in every submission diff and diff size then
  measures the interpreter rather than the agent. Remedy is `gitignore_extra`
  in the manifest, which is applied in the setup commit and therefore visible
  in `start_sha`.
- The solution half applies cleanly to the start state.
- f2p exits 0 after the reference fix, and p2p still exits 0. If the reference
  cannot pass, no submission can — and on a collection-error task this is
  where the import must have started working. It is **not** relaxed to match
  red-before.

---

## Layer 2 — required, and nothing checks it

This is where a task set goes wrong quietly. Every item below produces a task
that passes Layer 1 and measures the wrong thing.

### The oracle

- **Tests assert behaviour, not internal names.** This is the single most
  important judgment call per candidate, and no gate catches it. `click` #3678
  was rejected for exactly this: its tests pin an arbitrary storage name, so a
  different-but-correct fix would be scored as a failure. Prefer tests that
  compare rendered output, return values, or raised messages.
- **A property-based suite is allowed, and only with `image.env: {CI: "1"}`.**
  Measured 2026-09-01 against hypothesis 6.167.1, a property test over a rare
  input gives `0 0 0 0 1 1 1 1 0 0` across ten fresh runs of unchanged code on
  an unchanged tree, and `1 1 1 1 1 1` under `CI=1`. Hypothesis registers a
  built-in `ci` profile at import time — `derandomize=True`, `database=None`,
  `deadline=None` — and auto-loads it when any of twelve CI variables is
  present; `"CI"` counts on presence alone, any value. There is **no
  `HYPOTHESIS_PROFILE` environment variable** (the string in `pytest --help`
  is argparse's metavar for `--hypothesis-profile`), and a profile the repo
  would have to register is unreachable anyway: the start state is `base_sha`
  plus the test half, so the harness cannot rely on a repo-side `conftest.py`.

  Declare `HYPOTHESIS_STORAGE_DIRECTORY` too, pointing outside `/repo`.
  Hypothesis's storage root is `Path.cwd() / ".hypothesis"` fixed at import
  time, so with `workdir=/repo` it lands in the tree the §5.6 submission diff
  is taken against. `CI=1` stops the `examples/` database but not the
  `constants/` cache, and an agent that runs pytest from a subdirectory gets a
  second copy there. `gitignore_extra` is **not** the fix and is not needed:
  hypothesis writes `.hypothesis/.gitignore` containing `*`, so the tree is
  already clean to `git status --porcelain` and to `git add -A` — a
  `gitignore_extra` entry would move `start_sha` and change nothing.

  **What determinism does and does not buy.** It does not make the oracle
  safe; it makes it *reproducible*. A wrong fix can still pass on a pinned
  draw, and it will then pass on every repeat — so §5.7's N=3 repeats catch
  nothing here, because all three runs draw the same examples.

  Worse, and this is the rule that decides which suites are admissible:
  **Hypothesis mines integer and string literals out of the modules the suite
  imports and feeds them into the example pool, so the examples a submission
  is judged by are a function of the source code under test.** Measured
  2026-09-01, one property (`@given(st.integers(0, 1_000_000_000))`,
  `assert n != 137`) against an imported `magic.py`, `CI=1` throughout, six
  fresh runs per row:

  | imported `magic.py` | verdict |
  |---|---|
  | `MAGIC = 137` | `1 1 1 1 1 1` — the one-in-a-billion bug is found, every time |
  | `MAGIC = 1370` | `0 0 0 0 0 0` — missed, every time |
  | `MAGIC = 137` again | `1 1 1 1 1 1` — found again; the flip is reversible |
  | `MAGIC = 999` | `0 0 0 0 0 0` |

  The same literal in a file the suite does **not** import changes nothing
  (`0 0 0`), so it is import reachability and not the tree's bytes. The agent
  is being asked to edit exactly those modules, so the oracle's strictness is
  coupled to the shape of the fix: a submission that happens to write the
  right constant is judged by a strictly stronger example set than one that
  does not, and the grade records both as "the suite passed". Preflight's
  red-before/green-after verdict is taken on the **reference** fix's tree and
  does not transfer.

  So: prefer a suite whose examples are **exhaustive and explicit** —
  `@example` decorators, or `st.sampled_from` over a small closed set — where
  the mined pool cannot change what is checked. A suite whose only oracle is a
  draw over a large space is admissible only if you have read the property and
  are satisfied that *any* correct fix passes it and *no* wrong one does,
  which is the same judgment call as the "tests assert behaviour, not internal
  names" bullet above and is harder here, not easier.
- **The suite is deterministic.** Preflight runs p2p twice; a flake makes the
  gate a coin flip and the eval unreproducible.
- **An explicit `tests.p2p` lists LEAF node ids only** — `path::test_name`,
  never a bare module or a class. The oracle's swallow-refusal
  (`oracle.derive_quarantine`) compares declared ids against quarantined ids,
  which is exact only while every declared entry names one item. A declared
  non-leaf selects many items that leaf ids can never be a superset of, so the
  refusal **fails open** there: a selection deselected down to nothing gets
  past it and the graded run exits 5. That surfaces as a named
  `scope_collected_nothing` record rather than as silence, which is why the
  shape is a requirement here instead of a check in code — the set is authored
  here, and the predicate cannot be made exact from ids alone.
- **Both import shapes are accepted, and they are different tasks.** A PR that
  adds a new function and tests for it puts that symbol in the solution half,
  so the test module raises `ImportError` during collection and pytest exits
  **4** (a selected node id whose module will not import) rather than **1**.
  Preflight refused that outright until broadening 2 and now accepts it, under
  a narrow, measured condition: every reported `ERROR` line names a declared
  f2p module, nothing else is reported, p2p is green at the start state, and
  f2p goes green after the reference fix.
  Measured on `trucking-doc-extraction` #3, whose `test_redact_db_url.py`
  imports a redaction helper the fix introduces — `1 error during collection`,
  nothing else red.

  **What differs is what the agent reads, and it is not a defect.** On an
  exit-1 task the loop in §3.3 ends in an assertion message naming an expected
  value; on a collection-error task it ends in `ImportError: cannot import name
  'redact_db_url'`. That is exactly what the human who filed the issue saw, so
  it is the workflow this eval exists to measure rather than a tidied one. Two
  consequences worth knowing at screening time: a collection-error task gives
  the agent *no* per-test signal until the import works, so partial credit is
  coarser; and any cross-task view that reads `f2p_failed_node_ids` will find
  **module paths** (no `::`) there rather than test ids on such a task.

  **One thing the gate refuses, and it is easy to trip.** The acceptance is an
  **equality**: every declared f2p module must appear in the ERROR set. A
  collection error stops the run dead — measured, a selection spanning a broken
  module and a failing test in a good module reports *only* the collection
  error, and the failing test never runs — so an f2p set that spans a new
  module and an existing one leaves the existing module's ids unobservable, and
  the gate refuses rather than accept ids nobody checked. Split such a
  candidate, or cut the PR that touches one module.

### Grading

- **Every task declares a `grading:` block, or records why each key is
  waived.** `build`, `typecheck` and `lint` are checks 3, 4 and 7 of the
  grader's ladder. (Check 8 is the secret scan and is **not** waivable — it
  has no manifest key and runs on every submission.) An absent key grades as `not_configured`, which is a named
  absence and not a pass — but an *unrecorded* absence is indistinguishable
  from an oversight, and a §10.3 reader has no way to tell a task that
  deliberately has no lint gate from one whose author forgot. A comment in
  `task.yaml` naming each waived key and its reason is the whole requirement;
  see `click-3360-write-usage-empty-args/task.yaml`.
- **A declared command is pinned in the image.** A checker installed by
  version range is a moving oracle: the same submission grades differently on
  two passes and the grade cannot say which tool answered. Pin it in
  `image.pip`/`image.apt` in the same edit that adds the key, or waive it.

### Provenance

- **`base_sha` is `merge_commit^1`** — the real parent of the merge, not the
  PR's recorded `base.sha`. The base branch advances between a PR being opened
  and merged, and diffing against the stale one puts unrelated commits into the
  reference. Measured on `click` #3434: `7c99ebe` → `63274a7`.
- **The reference is the real merged PR, verbatim** (§3.2), without exception.
  It is what converts the judge's question from open-ended "is this diff good?"
  (κ ≈ 0.32, below the 0.6 acceptability bar) into reference-anchored "does this
  accomplish what the reference accomplished?"
- **The licence permits redistributing the diff and the prompt.**

### The start state

Preflight validates what the suite does to the tree. It does not validate what
is *already in* the tree, and the difference is where this section lives.

- **No committed build output, virtualenv, or vendored dependency tree at
  `base_sha`.** §5.6 stages everything, so anything tracked is eligible to land
  in a submission diff — and unlike suite droppings, this needs no test run to
  get there. Measured on `trucking-doc-extraction` #2: **2,902 of 3,010 tracked
  files at that `base_sha` were a committed `lib/python3.12/site-packages/`**,
  and a stub agent that edited nothing produced a **19 MB submission diff**.
  Diff size then measures the venv rather than the agent, and every
  judge-scored or diff-similarity view over that task is answering a question
  about `site-packages`.

  **Preflight passes this**, which is the reason it is written down here rather
  than added as a check. Its tree-clean assertion is about what the *suite*
  writes, and the suite writes nothing; the venv only surfaces once a container
  runs a full agent lifecycle and `git add -A` sweeps it. **The dry run
  (`--mode offline`) is what catches it** — one more argument for never
  skipping that step, since it costs nothing and this defect is invisible until
  a record exists.

  Screen it directly, before cutting anything:

  ```bash
  git ls-tree -r --name-only <base_sha> | wc -l
  git ls-tree -r --name-only <base_sha> | grep -cE 'site-packages/|node_modules/|(^|/)vendor/'
  ```

- **A repository that later untracked such a tree carries a date floor**,
  exactly like the agent-file cutoff above it. `trucking-doc-extraction`
  untracked its venv in #4 (2026-07-02), so every candidate whose `base_sha`
  predates that is out — which removed two of its five candidate PRs. Establish
  both floors *once per repository* (agent files, vendored trees) and screen
  candidates against the later of the two before reading a single diff.

- **Removing an offending path is legitimate, and `strip_paths` is how it is
  recorded.** List the paths in the manifest's top-level `strip_paths` and they
  are removed — from the index and the worktree — inside the same
  fixed-identity setup commit that applies the test half and `gitignore_extra`.
  So the modification is in `manifest_digest` and in `start_sha`, and a reader
  who cannot find it upstream can find it in the manifest. Nothing has to be
  hand-rewritten and `base_sha` stays the true `merge_commit^1`, which is what
  keeps the reference applying.

  A hand-rewritten `base_sha` — a one-commit strip pushed to a fork — is still
  legitimate for anything `strip_paths` cannot express, and still has to be
  recorded under `provenance` (`base_modified`). For a `strip_paths` strip the
  manifest key *is* the record; a duplicate `provenance` note is not required.

  **The path has to be TRACKED at `base_sha`.** A strip that matches no
  tracked file is refused when the start state is built, on purpose — a typo
  that strips nothing is the failure this key exists to prevent. One
  consequence to plan around: `allow_extra_paths` legitimately names a file the
  PR *creates*, and such a file cannot also be stripped, because it is not
  there to remove.

  **Where the reference diff touches a stripped path, the load is refused**
  until that path is also in `tests.allow_extra_paths`, which excludes it from
  both halves. Strip does not imply exclusion: dropping a chunk because its
  path is stripped would make `solution_diff` something other than the merged
  PR, and the case that survives every gate is a reference that is no longer
  one.

  Judge the strip by size. Six agent-file paths is bookkeeping; 2,902 venv
  files is a different repository from the one the PR was merged into, and the
  cheaper answer is a later `base_sha`.

  **The confound, and it has to be recorded per task (§6.4):** the humans who
  wrote the PR had that file. A repository whose `CLAUDE.md` shaped how its
  contributors worked is not quite the repository the models are handed once it
  is stripped, and a stripped vendored tree may be what an import in the fix
  actually resolved against. Name the stripped paths and this caveat in the
  manifest's comments, next to the key.

### The prompt

- **Verbatim** (§3.3): the issue title, then the issue body exactly as filed,
  plus the ticket description where one exists. Nothing added. A
  harness-authored sentence — "the tests are in `tests/`", "do not edit the
  tests" — makes it a different task from the one a human actually opened, and
  this eval exists to measure the workflow that happens rather than a tidied
  one.
- **Human corrections are not replayed** (§3.3). A follow-up like "fix the
  import on line 40" was conditioned on the original model's output; against a
  candidate that wrote different code it is incoherent. Those turns become
  `rubric_items` instead — a free, human-authored list of what "done" required.

### The image

Three ways a task image fails at build time, silently, all found by screening
rather than by reasoning:

- **No git submodules.** The build context is `git archive base_sha`, which
  drops them. `tomlkit` cannot collect its suite for this reason: it needs
  `tests/toml-test`, and the directory arrives empty.
- **No VCS-derived version**, unless `build:` supplies a pretend-version. Same
  cause: `git archive` leaves no `.git`, and `setuptools_scm` refuses with
  *"unable to detect version"*. Measured across 12 candidates, only
  `setuptools_scm` is strict about this — `hatch-vcs` builds fine without it.
- **The suite is fast enough.** Preflight runs it four times, each under a 600 s
  timeout, and the agent re-runs it inside `wall_clock_timeout_s`. ~40 s is the
  practical ceiling; `click`'s 1.4 s is what comfortable looks like.

One consequence of the run tree being pruned to `base_sha`'s history, since it
shows up in exactly the repos the second bullet is about: **tags that are
ancestors of `base_sha` are kept**, so `git describe` still resolves and a
`setuptools_scm`/`hatch-vcs` repo can still derive a version. But the
abbreviated SHA shortens — `8.3.3-69-g63274a79` becomes `8.3.3-69-g63274a7`,
measured — because `core.abbrev` auto-sizes to the smaller object count. A
package installed at image-build time and one the agent rebuilds in the run tree
therefore disagree on version string. Nothing asserts this: preflight already
runs the suite inside the image, so a mismatch that breaks anything surfaces
there.

---

## Layer 3 — properties of the set, not of any task

- **~80 harvested to land ~60** (§3.5), stratified to the *measured* production
  distribution rather than a guessed split — files touched per session, turn
  count, output tokens, tool-call mix, duration. A 50/50 split on a workload
  that is 70% small edits measures the wrong thing and will not transfer to the
  bill. Include a **long-session stratum**: all three candidates claim 256K
  context and degrade unevenly well before it, and small tasks never surface
  that.
- **Calibration pilot before freeze: all four models, N=3, across every
  candidate.** Selecting tasks by one model's difficulty curve tilts the set
  toward what that model happens to discriminate on, which is precisely the bias
  this eval exists to avoid.
- **The drop rule is model-neutral.** Drop only if **all four** score 0% (floor
  — usually underspecified rather than hard) or **all four** score 100%
  (ceiling — no signal). **Keep every disagreement, however lopsided.** A task
  only Sonnet solves is among the most informative in the set.
- **`task_version` bumps on any edit.** It is not part of `run_id`, so an edited
  task leaves every finished cell looking complete while mixing two different
  tasks under one `task_id`. Resume hard-refuses on the mismatch, per cell.

**Read the drop rule against the measured variance.** On 2026-08-13 this task
set's one task went 4/4 resolved and then 2/4 on the same four arms, hours
apart, at temperature 1.0 and N=1 — between-run variance larger than the
between-arm spread. At N=3 a discriminating task can present as a floor. That is
an argument for more repeats in the pilot, not for a looser drop rule.

---

## Screened repositories

Measured 2026-08-13 in `python:3.12-slim-bookworm`: editable install, `git clean
-xfd`, full suite, then `git status`. Suite times are at HEAD, **not** at any
candidate's `base_sha` — green here does not guarantee green there, and
preflight remains the gate. The PR column counts merged pull requests linked to
an issue and created after 2024-01-01; it is an availability proxy only, since
each candidate still needs the Layer 2 read.

### Clean — pass every mechanical gate

| repo | suite | secs | PRs |
|---|---|---|---|
| pypa/packaging | 62,430 passed | 20 | 46 |
| python-jsonschema/jsonschema | 8,280 passed, 232 skipped | 6 | 4 |
| arrow-py/arrow | 1,904 passed | 13 | 8 |
| marshmallow-code/marshmallow | 1,188 passed | 2 | 28 |
| pallets/werkzeug | 1,003 passed | 9 | 36 |
| pallets/jinja | 911 passed | 1 | 9 |
| more-itertools/more-itertools | 736 passed | 9 | 69 |
| PyCQA/isort | 631 passed, 2 skipped | 35 | 43 |
| psf/requests | 619 passed, 15 skipped | 78 | 15 |
| pallets/flask | 494 passed | 1 | 23 |
| mahmoud/boltons | 468 passed | 3 | 9 |
| pallets/itsdangerous | 297 passed | 0 | 3 |

`pallets/click` is already in the set and is the worked example.

### Usable once a dependency is declared

| repo | what it needs | result | PRs |
|---|---|---|---|
| tobymao/sqlglot | `pip: [duckdb, pytz, pandas, python-dateutil]` and `SETUPTOOLS_SCM_PRETEND_VERSION` in `build:` | 1,217 passed, 19,201 subtests, 41.5 s | **795** |
| pygments/pygments | `pip: ["wcag_contrast_ratio"]` | 1 collection error without it | 64 |
| Textualize/rich | `pip: ["attrs"]` | 1 collection error without it | 40 |
| python-humanize/humanize | 6 import errors, undiagnosed | — | 21 |
| python-attrs/attrs, python-attrs/cattrs | `image.env: {CI: "1", HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"}` for the property-based suite | **not measured** — the editable install collides with a site-packages `attr`, and that is now the only known blocker | — |

**attrs/cattrs are half-reopened, not reopened.** They were excluded for two
reasons and this broadening lifts one. The other — `pip install -e .`
resolving `attr` against site-packages instead of the run tree — is the
non-editable-install failure mode in a new dress: imports resolve past `/repo`
so nothing the agent writes takes effect, every arm fails identically, and
preflight's green-after check is what catches it. Nobody has run that gate on
these repositories. Do not cut a task from either without doing so first.

**sqlglot carries a date constraint.** `CLAUDE.md` was added 2026-02-02
(`a65c8701a306`, PR #6899); `AGENTS.md` followed on 2026-03-19
(`7f0dd47f60a3`), and a later commit made `CLAUDE.md` a symlink to it. Both are
present at HEAD and preflight's `test -e` follows symlinks, so **`base_sha` must
predate 2026-02-02**. That leaves 680 clean candidates (301 of them from 2025),
against 116 after the cutoff — the constraint costs almost nothing, and sqlglot
remains the richest source by an order of magnitude. A SQL transpiler is also
close to the ideal bug shape: input SQL, exact expected output, tests that
assert rendered strings rather than internal names.

`strip_paths: ["CLAUDE.md", "AGENTS.md"]` lifts that floor — both are agent
files and neither is touched by a bug-fix PR — which reopens the 116
post-cutoff candidates. List **both** names: `CLAUDE.md` is a symlink to
`AGENTS.md` there, and preflight does NOT catch a target-only strip.
`strip_paths: ["AGENTS.md"]` alone removes the target and leaves `CLAUDE.md` a
dangling link an agent's `ls` still shows — the strip probe only looks at the
paths this task DECLARED, and the context-file probe that checks for a
leftover `CLAUDE.md` is `-e` only, which calls a dangling link absent. The
gate passes clean on the mistake, so list both names rather than relying on
preflight to catch the omission.

### Excluded

| repo | why |
|---|---|
| python-poetry/tomlkit | suite needs the `tests/toml-test` submodule; `git archive` drops it |
| un33k/python-slugify | no harvestable PRs |

### Internal repositories

Not part of the public corpus and not interchangeable with it — an internal
repository is a *stratum*, since five candidate PRs cannot carry a set that
needs ~60 tasks. Recorded because both start-state rules above were measured
here, and because the per-candidate yield is the number to plan against.

`SuhaasS/trucking-doc-extraction` — 122 commits, 7 merged PRs, 1 closed issue.
34-file pytest suite whose `conftest.py` fixtures are in-memory SQLite and
`MagicMock`, so it is hermetic despite a dependency list naming Postgres and
three Google Cloud services. No `pyproject.toml`/`setup.py`: flat modules under
`src/` with `sys.path` manipulation in `conftest.py`, so `image.build` is empty
and the agent's edits are still what the next import reads — preflight's
green-after check is what proves that, per task.

**Two date floors, both measured, and `base_sha` must clear the later one:**

| floor | cause | effect |
|---|---|---|
| 2026-03-31 | `.claude/` added (6 files) | liftable: `strip_paths: [".claude"]` — six paths is bookkeeping, and the §6.4 confound goes in the manifest |
| **2026-07-02** | `#4` untracked a committed venv | `strip_paths` can express it, but 2,902 `site-packages` files is a different repository from the one the PR was merged into. Prefer a later `base_sha` |

**Five candidate PRs, two tasks.** The rejections are each a different rule and
are worth reading as a worked example of Layer 2:

| PR | outcome |
|---|---|
| #6 | **cut** — `trucking-6-zero-amounts-absent`, 6 f2p |
| #8 | **cut** — `trucking-8-sort-order-org-scope`, 2 f2p; prompt names two functions, a §6.4 caveat recorded in the manifest |
| #2 | rejected — `base_sha` predates the venv untracking; stub agent produced a 19 MB diff |
| #3 | rejected — test imports a symbol the fix introduces; exits 2, not 1 |
| #5 | rejected — 14 f2p across 6 files and ~6 unrelated concerns; a model fixing 5 of 6 scores zero |

A 40 % yield is the planning number, and it is *after* the repository already
passed the mechanical screen. Neither task can ship in a published set: the
repository carries no licence, so `provenance.license` is `null` on both.

---

## Cutting one

1. Find a merged PR that closes an issue and carries both a test and a fix.
   Read the tests first — Layer 2's behaviour-not-internals rule is what
   disqualifies most candidates, and it is cheapest to check before anything
   else. Read the test half's **import block** in the same pass: every name it
   imports has to exist at `merge_commit^1`, or collection fails and the task
   exits 2 instead of 1.
2. `base_sha` is `git rev-parse <merge_commit>^1`. Check what is tracked there
   before going further — a committed venv or vendored tree makes every
   submission diff a diff of that tree, and preflight will not tell you.

   An agent file or a small vendored tree there is not disqualifying: list it
   in `strip_paths` and it is removed in the setup commit. A large one is —
   see "The start state" above for where that line falls.
3. Cut the reference with the flags pinned, and store it verbatim:

   ```bash
   git -c diff.noprefix=false -c diff.renames=true \
       diff --binary <base_sha> <merge_commit> > reference.diff
   ```

   **The flags are not cosmetic — without them the diff's SHAPE is a property of
   the harvester's `~/.gitconfig` rather than of the task**, and 80 references cut
   on different machines would not be the same kind of object.

   - `diff.noprefix=false` — a `--no-prefix` diff is refused at load, because
     `git apply`'s `-p1` would strip a *real* path component and silently swap
     which files are the oracle.
   - `diff.renames=true` — `copies` turns plain modifications into copy chunks.
   - `--binary` — a plain `git diff` renders a binary change as `Binary files …
     differ` with no payload. That loads clean and then fails in *preflight*
     ("cannot apply binary patch without full index line").

   Leave `core.quotepath` at its default. Quoted headers are never parsed for
   paths, and turning it off writes raw non-UTF-8 bytes into `reference.diff`,
   which fails to decode at load.
4. Write `task.yaml` per §3.7. Copy the structure from
   `click-3360-write-usage-empty-args/task.yaml`, whose comments explain what
   each key has to satisfy.
5. Leave `repo.start_sha` out on the first pass. Run the gate, then pin the
   value it reports.
6. Run the f2p tests by hand at the start state to get the node ids right —
   they are measured, never guessed.
7. `--preflight-only` until it prints PASS. Every problem it reports maps to a
   defect that would otherwise read as model capability.
