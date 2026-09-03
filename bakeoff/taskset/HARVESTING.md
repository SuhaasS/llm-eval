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
| `image.env` keys are in the allowlist (`CI`, `HYPOTHESIS_STORAGE_DIRECTORY`, `TZ`) and are not a key the harness itself sets; values carry none of `\n` `\r` `"` `\\` `$`; `HYPOTHESIS_STORAGE_DIRECTORY` is an absolute path outside `/repo`; `TZ` matches an IANA-name/`UTC`/`Etc/GMT+N` shape | an allowlist because a denylist would have to anticipate `CLAUDE_CODE_USE_BEDROCK`, which bypasses the proxy and leaves the wire log empty with the run still looking normal; a harness-owned key applying to preflight and the grader but overridden on the agent's own process — two environments for one task; a value that does not survive a generated `ENV KEY="value"` Dockerfile line; hypothesis writing into the tree the §5.6 submission diff is taken against; a `TZ` value is read by libc, not by this harness, and a leading `:` OR `/` makes glibc treat it as a file path instead of a zone name |
| `image.python`, when declared, is a quoted string in `{"3.11", "3.12", "3.13"}` | an unquoted `3.10` is the float `3.1` and an unquoted `3.11` is `3.11` — the version that reaches the build is the parser's, not the manifest's. An unlisted value is either a floating tag (two collections months apart on different interpreters, with nothing in the record saying which) or one that fails at the `FROM` with a registry error mid-build |
| `tests.framework`, when declared, is one of `pytest` (default), `vitest`, `jest` | an unrecognised value would reach `for_framework` and raise a bare `KeyError` out of the middle of preflight, with no manifest path in the message |
| `tests.runner`'s argv contains the declared framework's marker (`pytest`/`vitest`/`jest`) | the two keys catch each other's typo; a runner read by the wrong adapter is classified by the wrong rules, and on the node side no exit code says so — vitest and jest both exit 1 for a failing test and for a broken config alike |
| a node `f2p`/`p2p` id is `<file>::<full test name>`, both halves non-empty, the file half has no leading `-` and no `..`/absolute component, and lies under a declared `tests.paths` prefix | `-t` matching no test exits **0** on both frameworks with every test reported skipped (measured), so an id outside the scope could never be deselected from the scoped p2p run, and nothing downstream would say so |
| `image.python` is absent on a node task, and `image.node` is absent on a pytest task | the runtime is derived from `tests.framework`, never declared twice — a disagreement is a task gated in one interpreter and run in another |
| `image.node`, when declared, is a quoted string in `_NODE_VERSIONS` (`{"22"}`) | an unquoted `22.10` is the float `22.1`; an unlisted value is a base nobody has built |

### Preflight — `src/bakeoff/preflight.py`

Runs inside the pinned image, before the proxy starts.

- `tests.runner` contains the declared framework's marker (`pytest`,
  `vitest` or `jest`). The red/green distinction is built on what the runner
  reported, and each framework reports it differently — pytest through its
  exit code, vitest and jest through a JSON report, because both of those
  exit 1 for a test failure and for a broken config alike.
- The image is non-root, `claude --version` matches the base pin, `git` and
  `rg` are present.
- **`python --version` inside the image parses to the declared
  `image.python`.** The base tag is local and mutable; a stale or mismatched
  base runs the suite under an interpreter the task was not cut for, and
  every other gate stays green. **On python tasks only.** On a node task the
  probe never runs and both `python_declared` and `python_observed` are
  `null` — the node base ships no `python` at all, and a node manifest is
  forbidden from declaring `image.python`, so a gate that probed anyway got a
  non-zero exit and refused every node task in the set. `null` is "the gate
  did not look", which is a different absence from the `""` a probe that ran
  and answered nothing files.
- The container's HEAD is `start_sha`.
- **No `CLAUDE.md`, `AGENTS.md`, `.claude` or `.cursorrules` in the start
  state.** §5.2 pins the session config precisely because agent files
  substantially change behaviour; a task-local one gives this task a context
  the others do not have.
- **The repo's OWN pytest configuration is not itself a usage error.** Runs
  the manifest's interpreter with none of `tests.runner`'s extra arguments —
  `python -m pytest --co -q`, collection only — so the
  repo's own `pyproject.toml`/`pytest.ini` addopts apply exactly as they
  would for an agent that never read `task.yaml`. Pytest only; vitest and
  jest have no addopts analogue and exit 1 for a broken config and a real
  failure alike, so there is nothing this probe could tell apart there, and
  it does not run — `bare_runner_exit` stays `null` and `bare_runner_skipped`
  names why. Exit 4 is a NO-GO naming the flag pytest reported and, for
  `--numprocesses`/`--cov`/`--timeout`/`--hypothesis-profile`, the plugin
  `image.pip` is missing (`pytest-xdist`/`pytest-cov`/`pytest-timeout`/
  `hypothesis` respectively) — measured on
  `bidict-389-putall-rollback-clean`, whose GATED runner passes by bypassing
  the repo's own addopts with `--override-ini=addopts=` while the bare
  command an agent naturally types exits 4 from turn one, bug fixed and
  unfixed alike. A timeout (124) is also a NO-GO; exit 2 or 5 is not — see
  `bakeoff/src/bakeoff/preflight.py`'s `PREFLIGHT_VERSION` 13 comment.
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
- **A failing `unittest.subTest` counts as the NODE failing, not as no
  failure at all.** pytest 9's core-integrated subtests (folded into core in
  9.0; the base image pins 9.1.1) print `SUBFAILED(label) <node id> - <msg>`
  for each failing subtest instead of a `FAILED <node id>` line for the node
  — measured 2026-09-02 (`sqlglot-6927-dremio-trycast`, whose `validate_all`
  helper wraps every assertion in `subTest`). The parser folds every
  `SUBFAILED` line for a node back onto that one node id, so a task whose f2p
  id fails only through `subTest` still gates GO at the before-check and the
  whole node — every subtest under it — must be green after the reference
  fix for the after-check to pass; one remaining `SUBFAILED` there is still a
  failed f2p id, not a partial credit.
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
- **Every declared f2p id must have RUN, not merely not-failed (node only).**
  pytest answers a selection matching nothing with exit 4; vitest and jest
  answer it with exit **0** and every test reported skipped (measured
  2026-09-01). After the f2p-before run, when the outcome is not a load error,
  any id the report shows no terminal verdict for is refused — otherwise a
  renamed or deleted f2p test reads as a green gate and then as a solved run on
  every arm. Evidence key `f2p_before_not_run`.
- **The scoped p2p run must not have left the declared scope (node only).**
  A positional argument is a substring filter on vitest and a regex on jest,
  not a path: measured, `vitest run tests/` also matched
  `/repo/jtests/fail.test.cjs`. After the scoped run, every file it actually
  touched must be under a declared `tests.paths` prefix (component-wise, never
  `startswith`) — otherwise the scoped run stops doing the one thing it exists
  for, which is keeping the agent's scratch files out of the regression check.
  Evidence keys `scope_files_run` and `scope_files_outside`.
- **`tests.runner` must carry the framework's cache flags (node only).**
  `--no-cache` for vitest. Measured: `vitest run` writes
  `<cwd>/node_modules/.vite` into the bind-mounted tree, and every JavaScript
  repository's `.gitignore` carries `node_modules/` — which makes the
  `git status` check below blind to it, unlike pytest's `.pytest_cache/`.
  jest needs no flag; its default cache directory is already outside the tree
  and it wrote nothing into the tree in any measured run. Evidence key
  `runner_cache_flags`.
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
- **A suite that pins `TZ` is allowed, and it is a §6.4 confound to record.**
  `image.env: {TZ: "America/New_York"}` (or any IANA name / `UTC` /
  `Etc/GMT+N`) is the fix for a suite whose assertions are written against a
  specific zone — date/time libraries do this routinely. Declaring it scores
  every arm on that zone's behaviour rather than on a neutral one, same class
  of confound as `CI`; record the zone in the manifest the way the `CI`
  bullet below asks. The value is checked against an extra shape the other
  keys do not need, because it is consumed by libc's `tzset` rather than by
  this harness: a value starting with `:` OR `/` makes glibc read the rest
  as a file path, not a zone name, so `TZ` needs a positive allowlist
  (`_TZ_VALUE` in `tasks.py`) on top of the newline/quote/`$` blacklist every
  key gets.
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

#### Screening a JavaScript or TypeScript repository

Everything in this section is in addition to Layer 2's language-independent
rules, not instead of them.

- **The suite must be vitest or jest.** mocha, ava, `node --test` and a custom
  runner have no adapter, and adding one is a measured exit-code table plus a
  report shape, not a config entry. Check `package.json`'s `scripts.test`.
- **The reporter must be available.** `vitest run --reporter=json --outputFile`
  and `jest --json --outputFile` are built in and need no plugin, but a repo
  that pins a custom reporter in its config can override the flag. Run both
  once by hand and confirm a file appears.
- **The tests must not reach the network.** Nothing in the eval container can
  (the agent's network is `internal=True`), and a suite that fetches at import
  time fails as a *load error*, which the gate reads as a broken image.
- **`node_modules` is not yours to place, and the verb is `npm install`.** The
  base image installs vitest and jest at `/node_modules` and puts
  `/node_modules/.bin` on `PATH`; a task's own dependencies go through
  `image.build` at the same prefix:

      build: ["npm install --prefix / --omit=dev <deps>"]

  Each element of `image.build` is one **shell command string**, rendered as
  its own `RUN cd /repo && <element>` — not an argv. An `["sh", "-lc", "…"]`
  spelling renders three RUN lines, the second of which is `RUN cd /repo &&
  -lc` and exits 127, so the build fails before your dependencies are
  installed. The `cd /repo && ` prefix is already there; a shell is already
  there.

  **Never `npm ci` at that prefix.** Its documented contract is to delete
  `node_modules` before installing, and that is where the runners live;
  whether it fires depends on which `package.json`/`package-lock.json` pair
  npm resolves for the prefix and cwd, which your `image.build` line decides
  by accident. And the branch that does *not* delete them is the worse one:
  measured, `npm ci --prefix /` run from `/repo` resolved the **base's**
  `/package.json`, kept the runners, and installed **none of your
  dependencies** — a silent no-op that passes every build-time check and
  surfaces as a load error at preflight, which the gate reads as a broken
  image rather than as your manifest. The generated Dockerfile re-asserts the base's pinned runner
  versions after your build steps, so a build that removes them **fails the
  build** rather than reaching a model as exit 127 on every arm — but the
  failure is yours to avoid, not the harness's to repair.

  Anything installed under `/repo` is **erased by the bind mount at run time**
  — measured — and will look like a missing dependency in every arm.
- **Two tests may share a full name across *different* files.** They could not
  until 2026-09-03: `-t` matches `fullName`, and one invocation carrying every
  declared file meant a quarantine of `a::works` also removed `b::works`,
  silently, with `p2p_deselected` agreeing. A node selection is now one
  invocation **per file**, each pattern holding only that file's titles
  (measured: one positional plus one `-t` runs the named tests of that file
  only), so the file half of the id is carried into the argv and the collision
  is unambiguous. Preflight still **records** every cross-file collision under
  `duplicate_full_names`; it no longer refuses one.

  Two rules replace that exclusion.
- **No two test *files* under `tests.paths` may have one repo-relative path
  contained in another's — on vitest only.** jest's per-file positional is a JS
  `RegExp` anchored at the mount and escaped, so it names exactly one file;
  vitest's is a **substring filter no anchoring reaches** — measured
  2026-09-02, `/repo/tests/doc/a.test.js` still matched
  `/repo/pkg/tests/doc/a.test.js`. On such a tree the per-file group carries
  its name pattern into a file it does not name. Preflight refuses it as
  `ambiguous_file_filters`. Rename or move one of the files, narrow
  `tests.paths`, or cut the task on jest.
- **Two tests with the same full name in the *same* file cannot be named as two
  ids at all.** A node id is `<file>::<fullName>` with no positional index, so
  the two collapse to one identical string — invisible to the loader and to
  `duplicate_full_names` alike. Open work, tracked in `TASKS.md`; such a
  repository is still **excluded**. Check both before you cut:

      vitest run --reporter=json --outputFile=/tmp/r.json tests/
      node -e 'const r=require("/tmp/r.json"),f=r.testResults.map(x=>x.name);
        for (const t of r.testResults) { const s=new Set();
          for (const a of (t.assertionResults||[]))
            { if (s.has(a.fullName))
                console.log("SAME-FILE DUP:", t.name, a.fullName);
              s.add(a.fullName); } }
        for (const a of f) for (const b of f)
          if (a!==b && b.includes(a)) console.log("CONTAINED PATH:", a, "<", b);'
- **A jest task's own `testPathIgnorePatterns` is honoured.** Under
  `GRADER_VERSION` 10 the harness emits that flag nowhere, so the repository's
  configuration decides what jest collects. This is a change from 9, where the
  one run that used it **replaced** the repository's list with jest's built-in
  default — measured on `eemeli/yaml`, whose config declares `tests/_utils` and
  `tests/json-test-suite/` and no `/node_modules/` at all, and whose
  `tests/_utils` helpers match its own `testMatch`.
- **`tests.runner` must carry the framework's cache flags.** `--no-cache` for
  vitest. Preflight refuses a manifest without them, because vitest writes
  `node_modules/.vite` into the tree and your repo's `.gitignore` hides that
  from the dirty-tree check.
- **Node ids are `<file>::<full test name>`.** The name is the reporter's
  `fullName`: every enclosing `describe` title and the test title, joined by
  single spaces. Get it from a real run's JSON rather than by reading the
  source — a `describe.each` or a template literal makes the two differ.
- **`tests.paths` prefixes must not have a sibling they are a substring of.**
  A positional argument is a substring filter on vitest and a regex on jest,
  not a path: `tests/` matches `jtests/` too (measured). Preflight refuses a
  scoped run that leaves the declared paths, and the remedy is a narrower
  prefix.
- **The suite must be deterministic, and nothing checks it here.** Preflight's
  hypothesis probe is a Python-ecosystem check and answers nothing for
  `fast-check` or `jest-fuzz`. If the suite is property-based, pin its seed in
  the repo's own config and say so under `provenance`.

The `npm install` vs `npm ci` rule and the duplicate-full-name exclusion above
are both screening decisions a harvester makes before writing a manifest, not
manifest keys — nothing in the loader or preflight can see a repository you
have not yet cut a task from.

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

`image.env` now also admits `TZ`, so a suite that pins a zone (moment/luxon
asserting against `America/New_York` and similar) is a task, not a rejection
— see the loader table above and the hypothesis bullet under *The oracle* for
the value-shape and §6.4 detail.

**`-c /dev/null` is not a safe way to discard a repo's own `addopts`, and
`--override-ini=addopts=` is.** Both zero the `[tool.pytest]`/`pytest.ini`
`addopts` block, but an explicit `-c <path>` argument also retargets pytest's
**rootdir** to that path's dirname — measured 2026-09-02 (`bidict`): every
node id pytest then reports comes out relative to `/dev` instead of `/repo`
(`../dev/test_bidict.py::...`), so preflight's exact-string `FAILED <id>`
match against the declared f2p id never fires, and a genuinely-red test gates
NO-GO as "did not fail at the start state." Collection **count** stays
correct, which is why this is easy to miss by hand — only the reported node-id
strings are corrupted. `--override-ini=addopts=` drops the same `addopts`
block without moving rootdir.

**Narrowing `tests.paths` does not narrow the p2p sweep when `tests.p2p` is
left empty.** An empty `tests.p2p` means "everything the runner collects",
and that sweep is scoped by the repo's **rootdir**, not by the declared
`tests.paths` prefix — measured 2026-09-02 (`yaml-474-single-newline-empty-value`),
where `tests.paths: ["tests/doc/stringify.ts"]` still pulled
`tests/properties.ts` (a `fast-check` property suite elsewhere in the tree)
into the p2p-before/after checks and NO-GO'd on a missing dependency a
host-side simulation scoped to `tests/doc/` never exercised. A task author
narrowing `tests.paths` to dodge a submodule still has to make the **entire**
suite's dependencies installable,
not just the scoped file's — narrowing `tests.p2p` itself (leaf ids only, per
the rule above) is the only lever that actually shrinks what gets swept.

Three ways a task image fails at build time, silently, all found by screening
rather than by reasoning:

- **The interpreter is a choice, and a closed one.** `image.python` selects the
  base — `"3.11"`, `"3.12"` (the default) or `"3.13"`. Every arm of a task runs
  the same base, so this is not a §5.4 divergence: what that section holds
  identical is the environment two *arms* are compared in, and a task is
  compared against itself. Adding a fourth version is four steps and the first
  is a measurement: build `docker/eval-agent.Dockerfile` with
  `--build-arg BASE_PYTHON_VERSION=<v>` and confirm both in-image pin
  assertions fire (`pytest 9.1.1 pinned`, `claude 2.1.220 pinned`), then add the
  string to `tasks._PYTHON_VERSIONS`, then add it here, and update
  `docs/BUILDING-A-TASK-SET.md`'s screening note and keys-that-bite bullet.
  Verified 2026-09-01:
  3.11 → `Python 3.11.16`, 3.12 → `3.12.13`, 3.13 → `3.13.15`, with pytest
  9.1.1 and Claude Code 2.1.220 installing on all three.

  **No screened repository is excluded on this ground today.** The screen at
  `docs/BUILDING-A-TASK-SET.md` §2 was run entirely in `python:3.12-slim-bookworm`
  (recorded under **Screened repositories** below), so a repository needing 3.11 or 3.13 semantics would have
  shown up as a suite failure with an unrelated-looking cause rather than as a
  version verdict — the key exists for the candidates the screen has not reached
  yet, and a re-screen at a second version is what would populate this
  paragraph.
- **The runtime is a second choice, derived from `tests.framework` rather
  than declared.** A pytest task builds the python base above; a vitest or
  jest task builds the node one, selected by `image.node` — closed, one entry
  today:

  | version | measured | node | vitest | jest | claude |
  |---|---|---|---|---|---|
  | `"22"` (default) | 2026-09-01 | v22.23.2 | 3.2.7 | 30.5.0 | 2.1.220 |

  Adding a second entry is the same three steps as a python version, with one
  extra: build `docker/eval-agent-node.Dockerfile` with `--build-arg
  BASE_NODE_VERSION=<v>` and confirm the claude pin **and** both runner pin
  assertions fire (the base re-asserts its own pins after `image.build`, so a
  version that cannot install vitest or jest at the pinned string fails the
  base build itself); add the string to `tasks._NODE_VERSIONS` with the date;
  add the row here.
- **Submodules are supported, with seven limits.**
  - The path, url and pinned commit are derived from `base_sha` — nothing goes
    in the manifest. Two git readers are involved (`git ls-tree` for the
    gitlink, `.gitmodules` for the url) and the two directions of disagreement
    are **not** symmetric: a gitlink with no `.gitmodules` url is refused at
    load, because that directory would simply stay empty and `git status
    --porcelain` reports the tree as clean throughout. The reverse — a stanza
    naming no gitlink — is inert and is recorded rather than refused (last
    bullet).
  - The url must be `https://`. Relative (`../x.git`), `ssh://`, `git@…` and
    `file://` are refused; a repository whose `.gitmodules` uses a relative
    url is currently out, and that is a deferral rather than a judgement
    (`TASKS.md`).
  - Nested submodules are refused.
  - **`strip_paths` may not touch a submodule, from above or below.** Both
    `vendor/libdep` and `vendor` (with the gitlink at `vendor/libdep`) are
    refused at load. The strip runs against a start state where the submodule
    is not yet initialised, so it removes the gitlink and leaves `.gitmodules`
    naming a path that no longer exists — and materialization then chdirs into
    a directory that was never created. A repository whose vendored tree has
    to go is not a candidate; strip a sibling directory instead.
  - **A task whose fix touches submodule content is out.** Measured
    2026-09-01: `git add -A` stages nothing for an uncommitted edit inside a
    submodule, so a submission diff is zero bytes for it; an agent that
    commits inside the submodule produces a gitlink diff that applies green
    and grades the original content. The loader refuses a reference diff
    touching a submodule path, and the grader refuses such a submission as
    not-graded.
  - **A submodule the task's suite never reads can be declared unneeded.**
    `submodules_unneeded: ["<path>"]`, top-level. The gitlink stays in the
    index and the tree exactly as at `base_sha`, the directory is never
    populated, **its url is never checked** — which is what reopens an
    ssh-url repository — and a readable `.gitmodules` is not required for it
    either. No mirror is built and no network read is made for it, and
    `start_sha` does not move. It relaxes four of the six refusals above and
    **neither of the two that protect grading**: a `strip_paths` entry
    covering the path and a reference diff touching it are refused with the
    key exactly as without it. What the gate checks is narrower than the
    key's name: the directory exists, is EMPTY and `git submodule status`
    reads `-`; the declared f2p ids are red-before/green-after; and the p2p
    sweep is green — all with the directory empty. It records no
    collected-test count, so it **cannot** tell a suite that guards on the
    submodule's presence from one that silently collects fewer tests
    (`TASKS.md`). Section 6.4 confound: the humans who wrote the PR had the
    submodule, so upstream's own suite was strictly larger than any arm's.
  - **A suite that writes inside the submodule is out** — and the enforcement
    depends on whether the submodule is populated. For a POPULATED one an
    untracked file there makes the superproject's `git status --porcelain`
    report ` M <path>`, which is preflight's clean-tree NO-GO, and
    `gitignore_extra` cannot fix it (that key writes the superproject's
    `.gitignore`, and the rule would have to live inside the submodule's own
    tree). For an UNINITIALISED one — which is every `submodules_unneeded`
    path — git reports **nothing at all**: measured 2026-09-02, a file inside
    an uninitialised submodule directory is invisible to `git status
    --porcelain` (with or without `-uall`), to `git ls-files -o` and to
    `git add -A`, because git does not descend into a gitlink path in any
    state. The enforcement there is preflight's `ls -A` read, taken before
    and after the suite. What the agent writes in there reaches neither the
    submission nor a checkpoint **because `container.snapshot_diff` seeds its
    scratch index from the start state**; before that seed the submission
    carried the agent's file *and* a phantom `deleted file mode 160000` that
    the offline grader refused outright.
  - **A suite, a conftest or an `image.build` step that runs `git submodule
    update --remote`, or deinits first, is out.** A *plain* `git submodule
    update` is fine and this bullet used to claim otherwise: measured
    2026-09-01, the submodule is registered in `.git/config` and already at
    its gitlink before the container starts, so the command has nothing to
    fetch and exits 0 with no network. What is out is any form that needs the
    remote — `--remote` resolves the branch upstream, and a `deinit` (or a
    wiped `.git/modules`) makes a subsequent `update` a clone. The container
    has no route off the host and the `.gitmodules` url the run tree carries
    is the truthful upstream https one, so those forms fail, and they fail
    inside a suite whose exit code the gate reads as the task's own.
  - **An orphaned `.gitmodules` stanza is fine.** A `path` naming no gitlink
    is inert (never listed, never fetched, no directory created); preflight
    records it as `submodules_orphaned` and nothing refuses it.
- **No VCS-derived version**, unless `build:` supplies a pretend-version. Same
  cause: `git archive` leaves no `.git`, and `setuptools_scm` refuses with
  *"unable to detect version"*. Measured across 12 candidates, only
  `setuptools_scm` is strict about this — `hatch-vcs` builds fine without it.
- **The suite is fast enough — or the manifest says how slow.** Preflight runs
  the suite **five** times (f2p before, p2p before, f2p after, p2p after, and
  the scoped p2p the grader will make — four when `tests.p2p` is declared
  explicitly, which skips the scoped run), plus once per declared `grading.*`
  argv. The oracle then runs it twice more at grade time, and the ladder runs up
  to five bounded commands per graded record — two suite runs plus one per
  declared `grading.*` argv. Each of those carries a coreutils `timeout`
  prefix whose value is `budget.suite_timeout_s`, default **600 s** — the same
  number on the gate's side and the grader's, which is the point of the key:
  a suite that fits one bound and is killed under the other stamps `timed_out`
  on the model.

  **~40 s is still the practical ceiling, and raising the key is not a way
  around it.** `click`'s 1.4 s is what comfortable looks like. Raising
  `suite_timeout_s` multiplies through: the worst case is 8 × the value for
  one task's gate, all of it spent **before the proxy starts** and inside the
  same one-hour SSO session the matrix itself needs (measured twice — the
  window is one hour, not the eight an earlier note claimed). A task set of
  slow suites can therefore burn the credential window on the gate and leave
  nothing for the cells.

  `budget.suite_timeout_s` may not exceed `budget.wall_clock_timeout_s`, and
  `load_task` refuses the manifest with the arithmetic when it does: the agent
  re-runs this suite inside its wall clock with no per-command bound, so a
  longer one describes a task no arm could verify even once — spec §3.3
  measures a loop that ends in "runs tests, sees failures, self-corrects", and
  a run terminated mid-suite is that loop truncated with an unchecked diff.
  Raising `wall_clock_timeout_s` is the other escape and is not a §5.4
  divergence: the budget is per task, identical across arms.

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

A submodule's history is pruned the same way, to its gitlink: the run tree's
`.git/modules/<path>` is hardlink-cloned from a mirror built and verified
exactly like the superproject's, so `git -C <path> log --all` in the run tree
reaches nothing after the pin.

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

Everything from *Clean* through *Excluded* below is Python-only, because that
is what the screen in `docs/BUILDING-A-TASK-SET.md` §2 has run against. The
JavaScript/TypeScript section immediately below is the first pass at a
corpus that screen has no recipe for yet.

### JavaScript / TypeScript

Measured 2026-09-02 against real clones on the host (node v24.18.0, npm
11.16.0) and, for the jest task, additionally against a simulated container
`node_modules` layout standing in for the base image.

| repo | framework | result | notes |
|---|---|---|---|
| unjs/ufo | vitest | cut and gated (`ufo-214-without-trailing-slash-query`): 461 passed / 1 failed at `base_sha` → 462 passed after the fix, 16 s | zero runtime dependencies — no `image.build` needed at all; `/node_modules/.bin` **is** on `PATH` for the `eval` user in the node-22 base image (measured, corrects an earlier assumption written into this task's own dispatch notes) |
| eemeli/yaml | jest | cut and gated (`yaml-474-single-newline-empty-value`): 216 passed / 1 failed at `base_sha` → 217 passed after the fix, 62 s | four `https://` submodules (`tests/yaml-test-suite`, `tests/json-test-suite`, `docs-slate`, `playground`), all populated by the harness's own submodule derivation (broadening 6); `image.build` must install `babel-jest@30` + the `@babel/*` transform chain + a custom resolver **and** `fast-check`, because an unscoped p2p sweep (`tests.p2p` empty) is not narrowed by a narrow `tests.paths` and still collects `tests/properties.ts`; cross-file duplicate `fullName`s under `tests/doc/` (four, all under `describe('circular references', ...)`) **forced** `tests.paths` down to one file until 2026-09-03, when node selection became per-file and that refusal was removed — they are recorded as `duplicate_full_names` evidence now, and a wider `tests.paths` is admissible |
| moment/luxon | jest | rejected at first screen: 31 unrelated failures without `TZ=America/New_York` set, 1 (the real f2p) with it — `TZ` was not in the `image.env` allowlist | **usable now**: "feat: a suite that pins TZ is a task, not a rejection" added `TZ` to `tasks._IMAGE_ENV_ALLOWED`; not yet re-screened against a live gate |

Two things worth carrying into any future node task, neither specific to one
repo:

- **The tagged task image (`bakeoff-task-<id>:v{task_version}`) is
  `base_sha`-only by design** — it is built from `git archive base_sha` and
  never carries the committed test half, which is applied to a separately
  materialized run tree at run time and bind-mounted over `/repo`. Inspecting
  the image directly with `docker run` and no bind mount reads as "suite fully
  green, bug not present" (measured: 456/456 vs. the real 461/462), which
  looks like a stale or wrong image and is not one.
- **`npm ci --prefix /` in `image.build` deletes the base image's
  `/node_modules`** — including whatever that same `image.build` line just
  installed there — before jest or vitest can even boot, because `npm ci`'s
  documented contract is to remove `node_modules` before installing. Measured
  again here against a task with a real custom-resolver dependency
  (`jest-ts-webcompat-resolver`): `npm install`, never `npm ci`, at that
  prefix (already in the loader table and the JavaScript screening
  subsection above).

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

**pallets/werkzeug's row above and its cut task are two states of the same
repo, not two figures in tension.** The 1,003-passed row is the full-suite
screen at HEAD. The cut task (`werkzeug-3037-duplicate-rule-error`,
broadening 2 — the collection-error f2p shape) is measured 2026-09-02 at the
earlier `base_sha`: 958 passed, exit 4 confined to the one PR-added test
module, 964 passed after the fix. It needs nothing extra — broadening 2 is
what qualifies it, not a declared dependency — so it does not belong in the
*Usable once a dependency is declared* table below; this note is the one
place both figures live.

### Usable once a dependency is declared

Measured 2026-09-02: re-screening at `python:3.11-slim-bookworm` and
`python:3.13-slim-bookworm` reopened **zero** repositories: the three
*Excluded* rows below with a version-shaped exclusion (`pyftpdlib`,
`tornado`, `pytest-localserver`) and the five rows in this table that were
not already gated (`pygments`, `rich`, `humanize`, `attrs`, `cattrs`) show
the identical blocker — or, for pygments, the identical green — on 3.11,
3.12 and 3.13 (`~/.cache/bakeoff-probe/reports/r2-19-rescreen.md`). The
already-gated rows (`sqlglot`, `tomlkit`, `bidict`, `pytest`, `chimera`) were
measured at one version each and were not re-screened; `image.python` alone
has not yet been shown to grow the corpus.

| repo | what it needs | result | PRs |
|---|---|---|---|
| tobymao/sqlglot | `pip: [duckdb, pytz, pandas, python-dateutil]` and `SETUPTOOLS_SCM_PRETEND_VERSION` in `build:`; at a `base_sha` after 2026-02-27 also `submodules_unneeded: ["sqlglot-integration-tests"]` and `strip_paths: ["CLAUDE.md", "AGENTS.md"]` | 1,217 passed, 19,201 subtests, 41.5 s. Open at ANY `base_sha` with those levers — the ssh submodule is no longer a floor | **795** |
| pygments/pygments | `pip: ["wcag_contrast_ratio"]` | 1 collection error without it; re-screened 2026-09-02 — green (5330 passed, 16 skipped) identically on 3.11/3.12/3.13 | 64 |
| Textualize/rich | `pip: ["attrs"]` | 1 collection error without it; re-screened 2026-09-02 — 8 failures, identical on 3.11/3.12/3.13, all Pygments-syntax-highlighting snapshot mismatches driven by which Pygments release is on PyPI, not by the interpreter | 40 |
| python-humanize/humanize | `SETUPTOOLS_SCM_PRETEND_VERSION` (the editable install produces no `humanize._version`) plus `image.pip: ["freezegun"]` | diagnosed 2026-09-02 — the "6 import errors" are two ordinary shapes, identical on 3.11/3.12/3.13, neither version-dependent; not yet measured with either lever declared | 21 |
| python-attrs/attrs | `image.env: {CI: "1", HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"}` for the property-based suite, `image.pip: ["hypothesis"]` (the editable install pulls no test-only extra — a bare screen dies at collection without it), and a `SETUPTOOLS_SCM_PRETEND_VERSION` lever for the two `test_packaging.py` failures below | measured 2026-09-02, Pass 2 — hypothesis and the CI env declared, installed alone, **no** site-packages collision (`attr.__file__` resolves under the clone on 3.11/3.12/3.13); 1398–1402 passed, 2 failures, both `test_packaging.py::TestLegacyMetadataHack::test_version_info`, the same `SETUPTOOLS_SCM_PRETEND_VERSION` shape as humanize — that lever itself not yet declared or measured | — |
| python-attrs/cattrs | same `image.env`, plus `image.pip: ["hypothesis"]` and, by the same argument as attrs, a `SETUPTOOLS_SCM_PRETEND_VERSION` lever (unmeasured for cattrs — the benchmark usage error masks collection before either would matter), plus the site-packages collision below | **not measured** — the collision is confirmed real but a bare screen never reaches it (see below) | — |
| python-poetry/tomlkit | nothing declared — the submodule (`tests/toml-test`) is derived, not declared (broadening 6) | measured 2026-09-02, cut and gated (`tomlkit-514-inline-table-comment-separator`): 1,002 passed, ~1.2 s at `base_sha` with the submodule initialised; without it the whole run is **interrupted** (`FileNotFoundError` at collection, not a partial pass) | — |
| jab/bidict | `image.env: {CI: "1", HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"}`; `tests.runner` needs `--override-ini=addopts=`, never `-c /dev/null` (the addopts-bypass trap below); `image.pip: ["pytest-xdist"]` so the bare `pytest tests/` an agent would actually type does not itself exit 4 | measured 2026-09-02, cut and gated (`bidict-389-putall-rollback-clean`): 130 passed, ~1.7 s | — |
| pytest-dev/pytest | its own suite as source: `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST` inlined into the `image.build` command (the build context is `git archive base_sha` and never has `.git`); `tests.runner` regenerates `src/_pytest/_version.py` at the start of every invocation (the image-build copy is discarded by the runtime bind mount over `/repo`); one baked-in `--deselect` for the one self-test that asserts a `.pyc` got written, which the image's own `PYTHONDONTWRITEBYTECODE=1` defeats permanently | measured 2026-09-02, cut and gated (`pytest-10210-approx-nested-container`): 106.34 s single-process at `base_sha` — see the `-p no:cacheprovider` note below | — |
| astroufsc/chimera | `image.python: "3.13"` (`requires-python >= 3.13`); `image.apt: ["libatomic1"]` (a transitive `pynng` import fails without it); `tests.runner` needs `-o addopts=` against the repo's own `--cov=...` block, and an explicit `tests.p2p` narrowed to 30 measured-clean leaf ids — the wider tree is pre-existing bit rot, not this PR (see below) | measured 2026-09-02, cut and gated (`chimera-228-equinox-numeric`): 14 s (pinned `start_sha`) | — |

**tomlkit's submodule exposed a bug in grade-time restore, not an obligation
on the task.** `tests/toml-test` sits under the declared `tests.paths` prefix
(`tests/`), the ordinary layout for a submodule holding test fixtures — and
the grader's own test-restore step used to `git rm -r -f -- tests/` then
`git checkout <start_sha> -- tests/`, which deletes the submodule's working
tree and restores only the gitlink, never its content. That turned the
genuine reference fix into `not_graded_reason: environment_error` at the p2p
check. Fixed this session ("fix: the test restore removed a submodule it had
no reason to touch", `GRADER_VERSION` 6 → 7) — `_check_test_restore` now
excludes a `160000`-mode gitlink under a declared test prefix from both the
`git rm` and the `git checkout`, so the submodule's working tree is never
removed in the first place. tomlkit needs nothing further.

**attrs' *collision* reason is lifted; both repos are still ungated.** They
were excluded for two reasons and this broadening lifts one for both — but
the other, `pip install -e .` resolving `attr` against site-packages instead
of the run tree, measures differently per repository. Measured 2026-09-02
(`~/.cache/bakeoff-probe/reports/r2-19-rescreen.md`): installed alone, attrs
shows **no** collision — `attr.__file__` resolves under the clone on
3.11/3.12/3.13 alike, so this reason no longer applies to it. cattrs'
collision is real — it declares `attrs` as a runtime dependency, so
`pip install -e .` on the cattrs checkout pulls a non-editable `attrs` wheel
into site-packages ahead of any editable attrs checkout — but a bare screen
never reaches it: cattrs' own `pyproject.toml` addopts name `pytest-benchmark`
flags a bare `pytest` install rejects with a usage error (exit 4) before a
single test collects. The bare-runner probe added at `PREFLIGHT_VERSION` 13
would name the pytest-benchmark usage error that currently masks the
collision; the collision itself is silent — imports resolve past `/repo`,
nothing the agent writes takes effect, every arm fails identically, and
preflight's green-after check is what catches it. Neither has been run
against cattrs. attrs' own re-screen leaves 2 failures needing the
`SETUPTOOLS_SCM_PRETEND_VERSION` lever above, undeclared and unmeasured, and
no preflight run has been made against it either. Do not cut a task from
attrs or cattrs without doing so first.

**pytest-dev/pytest's own suite is structurally in tension with the harness's
`-p no:cacheprovider` convention, because pytest's own suite legitimately
exercises the plugin that flag disables.** At HEAD, `-p no:cacheprovider`
leaves `testing/test_legacypath.py::test_cache_makedir` erroring (`fixture
'cache' not found`) alongside 4,504 passed — the exact fixture that flag turns
off. The fix is not to fight the flag but to use the mechanism that already
exists for "the suite writes into the tree": drop `-p no:cacheprovider` from
`tests.runner` and declare `gitignore_extra: [".pytest_cache"]` instead.
Verified 2026-09-02: with the flag dropped, `git status --porcelain` stays
empty after a full run and the fixture error is gone. This is a property of
the repository, not of the one PR cut from it, and should be checked against
any other task cut from this source.

**chimera's own `tests/` tree is bit-rotted independent of Python version or
of this PR.** An unscoped p2p sweep at `base_sha` collection-errors on 7
modules importing names that three-plus PRs' worth of drift removed
(`tests/chimera/instruments/base.py` does not exist anywhere in the repo's
history; `SlewRate` was never added to `chimera.interfaces.telescope`), and a
further eight files raise or hang under a shared `manager` fixture — none of
it caused by the reference PR, which touches only `telescope.py`'s
`get_metadata` and one new test file. `tests.p2p` narrowed to leaf ids (per
the Layer 2 rule below) is the documented remedy for exactly this shape, not
a special case: the 30 ids are the leaf tests of the six files that collect
and run 100% clean, individually verified, with the two involving real thread
joins or heavy imports re-run twice to confirm determinism.

**sqlglot carries a date constraint, and the window has a close as well as an
open now.** `CLAUDE.md` was added 2026-02-02 (`a65c8701a306`, PR #6899);
`AGENTS.md` followed on 2026-03-19 (`7f0dd47f60a3`), and a later commit made
`CLAUDE.md` a symlink to it. Both are present at HEAD and preflight's `test -e`
follows symlinks, so **`base_sha` must predate 2026-02-02**. That leaves 680
clean candidates (301 of them from 2025), against 116 after the cutoff — the
constraint costs almost nothing, and sqlglot remains the richest source by an
order of magnitude. A SQL transpiler is also close to the ideal bug shape:
input SQL, exact expected output, tests that assert rendered strings rather
than internal names.

Measured 2026-09-02: a **second**, harder floor closes the window again.
`.gitmodules` naming an ssh url (`git@github.com:fivetran/sqlglot-integration-tests`)
was added in `3a930dad6` ("Chore: add integration test automations", PR #7167,
merged 2026-02-27) and is present at HEAD — refused by the submodule-url rule
before preflight ever runs, and `strip_paths` cannot lift this floor the way
it lifts the agent-file one (a strip cannot touch a submodule path; see the
submodule bullets under *The image* below). So a plain (non-submodule-aware)
`strip_paths` task from this repo is only usable with `base_sha` strictly
**between** `a65c8701a306` (2026-02-02) and `3a930dad6` (2026-02-27) — at
which point `AGENTS.md` does not exist yet and `CLAUDE.md` is a plain file, so
`strip_paths: ["CLAUDE.md"]` alone suffices.

**Cutting after 2026-02-27 is open, with `submodules_unneeded:
["sqlglot-integration-tests"]`.** That key declines to populate the path: its
url is never checked, so the ssh one costs nothing, and the gitlink stays in
the index and the tree. It is truthful here because this suite guards on the
submodule's presence rather than requiring it — measured at
`05eed63b281f7ac020045e2b792beef8fad8d3ee`, `git grep
sqlglot-integration-tests <base_sha> -- tests/` finds exactly two files and
both are behind an existence test:
`tests/sqlglot/__init__.py` appends the integration package to `__path__`
only `if os.path.isdir(<submodule>/tests/sqlglot)`, and
`tests/test_integration_loader.py`'s `load_tests` discovers additional tests
under the same guard. With the directory empty neither appends anything and
there is no collection error. At a post-2026-02-27 `base_sha` the manifest
also needs `strip_paths: ["CLAUDE.md", "AGENTS.md"]` — **both** names,
because `CLAUDE.md` is a symlink to `AGENTS.md` there (the "at which point
`AGENTS.md` does not exist yet" sentence applies only to the older window)
and preflight does not catch a target-only strip. Section 6.4 confound: the
humans who wrote the PR had the submodule populated by `make install`, so
upstream's own `make test` ran a strictly larger suite than any arm will.

Also measured 2026-09-02: sqlglot's dialect tests wrap every assertion in
`unittest.TestCase.subTest()` via the shared `validate_all` helper — pervasive
across the dialect suite, not incidental to one PR — so any f2p id cut from
this repo needs the `SUBFAILED` fix above ("fix: SUBFAILED never matched, so a
subtest-only failure read as no failure", `PREFLIGHT_VERSION` 11 → 12) to gate
correctly. Cut against an older preflight, a genuinely-red `subTest`-only f2p
test reads as "did not fail at the start state" and the task cannot pass the
gate as authored.

`strip_paths: ["CLAUDE.md", "AGENTS.md"]` lifts the first floor — both are agent
files and neither is touched by a bug-fix PR — which reopens candidates in the
window above, and beyond the submodule commit when the manifest also carries
`submodules_unneeded: ["sqlglot-integration-tests"]`. List **both** names:
`CLAUDE.md` is a
symlink to `AGENTS.md` there, and preflight does NOT catch a target-only strip.
`strip_paths: ["AGENTS.md"]` alone removes the target and leaves `CLAUDE.md` a
dangling link an agent's `ls` still shows — the strip probe only looks at the
paths this task DECLARED, and the context-file probe that checks for a
leftover `CLAUDE.md` is `-e` only, which calls a dangling link absent. The
gate passes clean on the mistake, so list both names rather than relying on
preflight to catch the omission.

### Excluded

| repo | why |
|---|---|
| un33k/python-slugify | no harvestable PRs |
| giampaolo/pyftpdlib | genuinely breaks on Python 3.12 at the historical candidate `base_sha`s screened (asyncore/asynchat removed, PEP 594), but no PR in its history is simultaneously a real bug-fix closing a formal issue and one whose added test actually reproduces the bug on the version boundary — measured 2026-09-02 across every candidate through 2023-08. Re-screened 2026-09-02 at HEAD: `ImportError: Error importing plugin "instafail": No module named 'instafail'`, identical on 3.11/3.12/3.13 — a different, HEAD-only blocker (the repo's own PR #605 already fixed the asyncore/asynchat removal this repo was screened in for) |
| tornadoweb/tornado | its whole suite is built on a custom `unittest.TestCase` subclass architecture (`AsyncTestCase`) that does not collect under pytest 9 regardless of interpreter (`AttributeError: 'CookieTest' object has no attribute 'runTest'`, measured at the candidate `base_sha` in `d5-python-version.md`, a 2023-era commit) — a bad fit for `tests.framework: pytest` independent of any candidate PR. Re-screened 2026-09-02 at HEAD: `ModuleNotFoundError: No module named 'cythonapp'` (then `'redbot'`), identical on 3.11/3.12/3.13, from an unscoped collection into `maint/`'s local helper packages — the `AsyncTestCase` failure quoted above did not reproduce at HEAD at all, and the exclusion now rests on this `maint/` collection blocker, not on `AsyncTestCase` |
| pytest-dev/pytest-localserver | its own Python-3.12 breakage (`smtpd` removal) is real, but every issue closed before the fix commit is either non-code or touches no file under `tests/` — `tests.paths` would produce an empty test half, which the loader refuses outright. Re-screened 2026-09-02 at HEAD: `ModuleNotFoundError: No module named 'pytest_localserver._version'`, identical on 3.11/3.12/3.13 — the same setuptools_scm shape as humanize/attrs, fatal at plugin load before the suite ever reaches the `smtpd` break quoted above |

**`mahmoud/boltons` is excluded for broadening 2 (collection-error f2p)
specifically, not in general** — it stays in *Clean* above for an ordinary
exit-1 task. Its `tests/conftest.py` defines a `pytest_ignore_collect` hook,
which is a **firstresult** hookspec: the first plugin to return non-`None`
wins, and boltons' own implementation (skipping `_pyN`-suffixed files) returns
a definitive `False` for every other file, including an erroring one — so
pytest's own `--ignore` handling is never reached. Measured 2026-09-02: with
the hook intact, `pytest --ignore=tests/test_statsutils.py` still reports
`ERROR collecting tests/test_statsutils.py` and exits 2, even though the item
count proves the file genuinely was excluded from collection; with the hook
renamed, the same command exits 0. Preflight's collection-error p2p baseline
needs `--ignore=<module>` to actually exclude that module, so no
broadening-2 task can pass preflight against this repository while the hook
exists — not fixable at the manifest level, since the hook is load-bearing
test infrastructure (Python-version skipping), not an agent file or a
vendored tree `strip_paths` could remove.

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
