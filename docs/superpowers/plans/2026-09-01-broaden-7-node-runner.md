# Broadening 7: a second test runner (vitest / jest) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a manifest declare `tests.framework: vitest` or `jest` so a JavaScript/TypeScript repository can be cut as a task, instead of being refused by a gate whose every red/green judgement is built on pytest's exit codes.

**Architecture:** Every pytest-specific decision — what an exit code means, how a failed node id is parsed out, how a selection and a deselection are spelled, what "nothing was collected" looks like — moves behind a **runner adapter** in a new package `bakeoff/src/bakeoff/runners/`, selected by a new manifest key `tests.framework` (default `pytest`). `preflight.py`, `oracle.py` and `grader.py` keep their structure and their messages and call the adapter instead of branching on pytest's numbers. The pytest adapter reproduces today's behaviour **argv-byte-identically**; the node adapter cannot use exit codes at all (measured: vitest and jest both exit 1 for a test failure, an import error, a syntax error, a missing file and a config error alike) and reads a **JSON report written to a file outside `/repo`** instead. Node needs its own base image, so broadening 5's `base_tag`/`build_base_images` grow from a version key to a `(runtime, version)` key.

**Tech Stack:** Python 3.12 (the harness venv), pytest, PyYAML, Docker. Node 22 (`node:22-bookworm-slim`), vitest 3.2.7, jest 30.5.0. All code under `bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3 the self-correction loop, §3.7 the manifest, §4.2.1 the grader checks, §5.1 the image, §5.4 what is held identical across arms, **OPEN-2** the deferred language scope) and `docs/superpowers/specs/2026-08-17-offline-grader-design.md` (the nine-check ladder). Shared broadening context: `.superpowers/broaden/CONTEXT.md` (this is broadening **#7**, the last and largest).

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. Claims about external behaviour are annotated with what they were verified against (`Measured 2026-09-01, vitest 3.2.7 / jest 30.5.0 on node:22-bookworm-slim`).
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section for later application.
- **The pytest path does not change.** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` loads unchanged, its `start_sha` does not move, its preflight runs the **byte-identical argv** it runs today, and every existing test in `bakeoff/tests/` passes unmodified. The existing argv-identity test `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` (`tests/test_preflight.py`) is **not edited** — it is the gate on Tasks 1–3.
- **Version constants move by +1 from whatever is on disk when the task that touches them starts.** Broadenings 1–6 land first and several of them move these. Read the constant, add one, do not hard-code a literal. `preflight.py` in particular is in flux — its `PREFLIGHT_VERSION` was `"5"` and then `"6"` within one hour on 2026-09-01.
  - `PREFLIGHT_VERSION` +1 (Task 7). `ORACLE_VERSION` +1 (Task 7). `GRADER_VERSION` +1 (Task 7). `GRADE_SCHEMA_VERSION` minor +1 (Task 8, adds `GradeRecord.framework`).
  - `SCHEMA_VERSION` does **not** move: no `RunRecord` field is added or changes meaning (D11).
- **The gated argv and the graded argv stay byte-identical** for every framework, not only for pytest. The JSON report path is therefore a **fixed** string, never a per-invocation temp name (D8).
- **Absence is recorded, never implied; configuration is never reported as observation.** New manifest keys are visible in `manifest_digest` by construction (it is `sha256(manifest bytes + reference bytes)`). None of them touches the setup commit, so `start_sha` cannot move — asserted anyway.
- **A run always produces a record.** Nothing in this broadening runs inside `execute_run`; the agent container is unchanged apart from which base image it is built from.
- **Tests pin every new invariant.** Unit tests in `bakeoff/tests/`; integration tests marked `integration` (+ `task_image` where a task image is built).
- **Commit hygiene:** stage files explicitly (`git add <paths>`), never `git add -A`. Subject in the repo's style (`feat:`/`fix:`/`docs:` + a sentence saying what breaks without it). End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **`scripts/mutation_check.py` is run SOLO** and is a gate on Tasks 2, 3 and 7. It edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` → 1129 passed, 46 deselected, plus whatever broadenings 1–6 added.

---

## Measurements

Taken 2026-09-01 on this machine. Docker server 29.5.2. Image `node:22-bookworm-slim` (node v22.23.2, npm 10.9.8), `vitest 3.2.7`, `jest 30.5.0`, installed with `npm install` from a scratch `package.json`. Reproduce from `$HOME/.cache/bakeoff-nodeprobe/` (it must live under `$HOME` — the Docker VM does not mount `/private/tmp`, and a bind mount from there appears inside the container as a **silently empty directory**, which is the first thing this probe hit).

### M1 — the exit code carries no information

Every framework, every failure shape, one number.

| what was run | vitest exit | jest exit |
|---|---|---|
| all tests pass | **0** | **0** |
| one test fails | **1** | **1** |
| a test file with an unresolvable `import`/`require` | **1** | **1** |
| a test file with a syntax error | **1** | **1** |
| a nonexistent test file given as an argument | **1** | **1** |
| a broken config file | **1** | **1** |
| `-t '<pattern matching nothing>'` | **0** | **0** |

The last row is the one that matters most and has no pytest analogue. pytest answers a selection that matches nothing with exit 4 and `ERROR: not found:`. **vitest and jest exit 0**, report the file as passed/skipped, and print a summary that reads like success:

```
 Test Files  1 skipped (1)
      Tests  3 skipped (3)
```
```
Test Suites: 1 skipped, 0 of 1 total
Tests:       3 skipped, 3 total
```

So a manifest naming an f2p test that has since been renamed — or an oracle quarantine that swallows the whole p2p list — is **silently green**. That is the defect this plan spends a preflight assertion and a classification rule on (D6, D7).

### M2 — the JSON report, per framework

`vitest run --reporter=json --outputFile=<f>` and `jest --json --outputFile=<f>`.

Top-level keys are a near-superset. Both carry `numTotalTests`, `numPassedTests`, `numFailedTests`, `numPendingTests`, `numTotalTestSuites`, `numFailedTestSuites`, `success`, `testResults`. **jest additionally carries `numRuntimeErrorTestSuites`; vitest does not.** Neither wrote a `testExecError` key in any measured shape — the jest documentation's `testExecError` did not appear even for a suite that failed to run, so nothing in this plan may depend on it.

`testResults[]` is one entry **per test file**, with `name` (an **absolute** path, `/repo/tests/pass.test.js`), `status`, `message`, and `assertionResults[]` carrying `status`, `title`, `fullName`, `ancestorTitles`. `fullName` is `ancestorTitles` joined with single spaces plus `title` (`["outer"] + "adds"` → `"outer adds"`).

| shape | vitest | jest |
|---|---|---|
| all pass | `numTotalTests:3 numFailedTests:0 success:true`; suite `status:"passed"` | same |
| one fails | `numFailedTests:1 success:false`; the assertion `status:"failed"` | same |
| import error | `numTotalTests:0`, suite `status:"failed"`, **`assertionResults:[]`**, `message:"Cannot find module '../src/missing.js' …"` | `numTotalTests:0 numRuntimeErrorTestSuites:1`, suite `status:"failed"`, **`assertionResults:[]`**, `message:"  ● Test suite failed to run…"` |
| syntax error | identical shape; `message:"Parse failure: Expected ',', got '=>'"` | identical shape |
| no test files matched | `testResults` **empty**, `numTotalTests:0`, `success:false` | `testResults` **empty**, `numTotalTests:0`, **`success:true`** |
| `-t` matching nothing | `numTotalTests:3 numPendingTests:3 success:true`; every assertion `status:"skipped"` | `numTotalTests:3 numPendingTests:3 success:true`; every assertion `status:"pending"`; suite `status:"skipped"` |
| `-t` matching one | matched `passed`, rest `skipped`; suite `status:"passed"` | matched `passed`, rest `pending`; suite `status:"focused"` |
| one good file + one unloadable file | `numPassedTests:3`, two `testResults`: the broken one `status:"failed"` with `assertionResults:[]`, the good one `status:"passed"` | same, plus `numRuntimeErrorTestSuites:1` |
| **a config error** | **no file written at all** | **no file written at all** |

Three consequences, each load-bearing:

1. **`success` is not the classifier.** jest reports `success: true` while exiting 1 on "no test files matched". Reading `success` would call a run that executed nothing a pass.
2. **The load-error discriminator is portable and does not need `numRuntimeErrorTestSuites`**: a `testResults` entry with `status == "failed"` and an **empty `assertionResults`** is a file that could not be loaded, in both frameworks. That keeps one classifier for both.
3. **An absent report file is the config-error signal.** It is also what a crashed runner leaves, which is the same claim: the command did not run.

### M3 — stdout, stderr, and where the report goes

- vitest writes its human summary to **stdout** (237 bytes) and nothing to stderr on a pass. **jest writes its human summary to stderr** (254 bytes) and **nothing to stdout**. So there is no cross-framework "final summary line" to parse, which is the argument for the file rather than for a second regex.
- With `--outputFile`, vitest prints `JSON report written to <path>` on stdout and jest prints nothing. Without it, both dump the whole JSON document to stdout, mixed with nothing else — but that is a single 1.5 KB line here and megabytes on a real suite, inside the same `stdout` the grader captures and stores. **Use the file.**
- `timeout 5 vitest …` on a hanging test exits **124**, exactly as the pytest path already relies on.

### M4 — selection and deselection by name

Both frameworks take `-t <regex>` matched against `fullName`.

| pattern | vitest | jest |
|---|---|---|
| `-t '^(outer adds\|top level)$'` | `outer adds` passed, `outer subs` **skipped**, `top level` passed | same (`pending` instead of `skipped`) |
| `-t '^(?!(outer adds\|top level)$)'` | `outer adds` **skipped**, `outer subs` passed, `top level` **skipped** | same |
| `-t '^handles a\+b \(x\) \[y\]$'` on a test literally named `handles a+b (x) [y]` (alongside `handles a+b (x) [y] EXTRA`) | the escaped one passed, `EXTRA` skipped | — |

So **a negative lookahead deselects by name on both frameworks**, an alternation selects, and regex metacharacters in a test title must be escaped.

**A second `-t` is not "last one wins" — the two frameworks disagree, and both answers are bad.** Measured 2026-09-02:

```
$ vitest run -t 'a' -t 'b' tests/pass.test.js
Error: Expected a single value for option "-t, --testNamePattern <pattern>", received ["a", "b"]
    at transform (file:///node_modules/vitest/dist/chunks/cac.BfaZ95xE.js:1269:10)
vitest 2x-t EXIT=1
```

vitest **rejects** it outright — exit 1, a node stack trace, and **no report file**, so the adapter would classify it `KIND_ENVIRONMENT`. jest does not reject it and does not take the last one either: it **joins them with a comma**.

```
$ jest --rootDir /repo -t 'adds' -t 'subs'
Ran all test suites with tests matching "adds,subs".
jest 2x-t EXIT=0
```

`adds,subs` is a regex that matches neither test, so jest runs nothing and exits **0** — M1's silent hole, reached by an argv nobody meant to write. Either way the conclusion is the same and stronger than the original one: the adapter must emit **exactly one** `-t`, which is why it gets a single `p2p_args(...)` entry point rather than composable `select`/`deselect` (D5). vitest's refusal is the good half — it is loud — and jest's silent comma-join is the reason a test pins the count.

**`-t` matches `fullName` and knows nothing about which FILE a test is in.** That is C2 and it is the sharpest edge in this design: two tests in different files with the same `fullName` are indistinguishable to a selection or a deselection. See D2's duplicate-name rule.

### M11 — file-level ignore flags, and jest's replaces its default

`ignored` (preflight's p2p-BEFORE run only) maps to a per-framework flag. Both measured working: vitest `--exclude 'tests/fail.test.js'` dropped that file from the run; jest `--testPathIgnorePatterns 'fail.test.cjs'` did too.

**jest's flag REPLACES the built-in `/node_modules/` ignore rather than adding to it.** So an `--testPathIgnorePatterns=<path>` emitted alone makes jest collect test files out of `node_modules` — which, with the runners at `/node_modules`, means jest's own vendored fixtures. The adapter emits `--testPathIgnorePatterns=/node_modules/` alongside, always.

### M12 — `/node_modules/.bin` is not on `PATH`

```
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  vitest NOT on PATH
  jest NOT on PATH
```

`npm install --prefix /` puts the shims at `/node_modules/.bin/` and changes no environment. A `tests.runner` of `["vitest", "run"]` would therefore be exit 127 — which the grader reads as `_INFRA_EXITS`, correctly, but only after a container has started. Worse, §3.3's loop is measured on commands **the agent invents**, and the agent will type `npx vitest` or `npm test`; both work (npx and npm resolve `.bin` themselves) while a bare `vitest` does not, which is an inconsistency the agent has to discover by failing. The node Dockerfile therefore sets `ENV PATH=/node_modules/.bin:$PATH`.

### M13 — `npm ci` at the prefix, and why the recommendation is `npm install`

The reviewer measured `npm ci --prefix /` **removing** `/node_modules/.bin/vitest`. Reproducing it here with the lockfile at `/repo` rather than at `/`, it did **not**:

```
  before: vitest present
  npm ci exit=0
/node_modules/.bin/vitest          <- still there
```

**And the variant that "worked" installed nothing of the task's.** From `cd /repo` with the lockfile at `/repo`, npm resolved the **base's** `/package.json` for the given prefix: the 325 packages it reported were the base's own tree, the runners survived, and **none of the repo's dependencies were installed**. That is the worse branch, because it is silent — it passes D15's post-condition (the runners are exactly where they should be) and surfaces only at preflight, as a load error on a module the image was supposed to carry, which the gate reads as a broken environment rather than as a manifest problem.

The two observations are not in conflict and the disagreement is itself the finding: `npm ci`'s documented contract is to **delete `node_modules` before installing**, and whether it fires depends on which `package.json`/`package-lock.json` pair npm resolves for the given prefix and cwd — which a task author's `image.build` line decides by accident. A convention that is right only under an unstated cwd is not a convention.

`npm install --prefix / --omit=dev <deps>` is measured **additive** and is what `HARVESTING.md` recommends:

```
  npm install exit=0
  AFTER npm install: vitest still present
  lodash also present
```

Because the recommendation cannot be enforced, the **post-condition** is what actually holds the line: the generated task Dockerfile re-asserts the base's pinned runner versions after every `image.build` step (D15).

### M15 — the runners cannot be installed with `--prefix /` into a tree that does not exist

`npm install --prefix / <pkgs>` fails on a fresh `node:22-bookworm-slim`, from any cwd, when `/package.json` and `/node_modules` are not there yet:

```
npm error Tracker "idealTree" already exists
The command '/bin/sh -c npm install --no-audit --no-fund --prefix / vitest@3.2.7 jest@30.5.0' returned a non-zero code: 1
```

Writing `/package.json` first and installing at `/` **without** `--prefix` works:

```
RUN printf '{"name":"bakeoff-runners","private":true}' > /package.json \
 && cd / && npm install --no-audit --no-fund vitest@3.2.7 jest@30.5.0
-> deps: {"jest":"^30.5.0","vitest":"^3.2.7"}   dev: undefined
-> vitest/3.2.7 linux-arm64 node-v22.23.2
```

Two things fall out of that output and both are load-bearing. The runners land under **`dependencies`**, not `devDependencies` — which is exactly **why a task's later `npm install --prefix / --omit=dev <deps>` does not prune them**, and why the base's install must never be spelled `--save-dev`. And `--prefix /` is fine *once the tree exists*, which is why `image.build` keeps it (measured: a task install at that prefix works and leaves vitest in place).

### M14 — `fullName` is not unique across files

Nothing in either framework namespaces a test title by its file. Two files each containing `it('works', ...)` produce two assertions whose `fullName` is `works`, and `-t '^(?:works)$'` selects both while `-t '^(?!(?:works)$)'` deselects both. There is no flag that scopes a name pattern to a file: the file positionals and the `-t` pattern are ANDed across the whole run, not paired.

### M5 — positional arguments are filters, not paths

`vitest run tests/` matched `/repo/jtests/fail.test.cjs` — the positional is a **substring filter over the absolute file path**, so a declared `tests.paths` prefix of `tests/` silently sweeps a sibling directory named `jtests/`. jest's positional is a **regex** over the absolute path (`Pattern: jtests/nonexistent.test.cjs - 0 matches`), unanchored, with the same over-match hazard.

pytest's positionals are paths and cannot over-match. This is why the node path gains a preflight assertion that the scoped run only touched files under the declared prefixes (D7c).

`vitest run nosuchdir/` exits 1 with `No test files found, exiting with code 1`; the report file **is** written, with an empty `testResults`.

### M6 — the tree, and the `-p no:cacheprovider` analogue

Running `vitest` in a repo creates **`<cwd>/node_modules/.vite`** — inside the bind-mounted tree. `git status --porcelain` reports `?? node_modules/`. `vitest run --no-cache` creates nothing (measured: `/repo/node_modules: NO` after the run). A `cacheDir` outside the tree in a config file also works, but needs a config file.

jest's **default** `cacheDirectory` is `/tmp/jest_0` — already outside the tree — and jest wrote nothing into the tree in any measured run.

This matters more than the equivalent pytest case: every JavaScript repository's `.gitignore` carries `node_modules/`, so preflight's existing "running the suite leaves the tree dirty" check is **blind** to vitest's artifact. Hence D7d.

### M7 — there is no `.pyc` analogue

The failure the base image's `PYTHONDONTWRITEBYTECODE=1` exists to prevent — an edit that preserves byte count inside the same second being served from a stale cache, so §3.3's self-correction loop reads the *old* behaviour after a correct fix — **does not reproduce on either framework**. Measured directly: run with the bug (`a - b`), overwrite with the fix (`a + b`, same 44/38 bytes, same `stat` mtime second), re-run.

```
  >> vitest run 1 (bug: a-b), expect FAIL
 Test Files  1 failed (1)|      Tests  1 failed (1)| EXIT=1
  >> vitest run 2 (fixed, same size, same second) -- STALE? expect PASS
 Test Files  1 passed (1)|      Tests  1 passed (1)| EXIT=0
  >> jest run 1 (bug), expect FAIL
Tests:       1 failed, 1 total   EXIT=1
  >> jest run 2 (fixed, same size, same second) -- STALE? expect PASS
Tests:       1 passed, 1 total   EXIT=0
```

`node_modules/.vite` is vite's **dependency** optimiser cache, not a source-transform cache, and jest's cache is content-hash keyed rather than `(mtime, size)` keyed. So the node base image needs **no** `PYTHONDONTWRITEBYTECODE` analogue. `--no-cache` is in the runner argv for the tree-cleanliness reason of M6, not for a staleness reason, and the Dockerfile comment must say so — otherwise the next author will delete it as redundant with a cache that was never the problem.

### M8 — `node_modules` cannot be baked at `/repo`, and `/node_modules` works

The whole reason `pip install -e .` is correct in this codebase is that site-packages survives the bind mount. Node has no site-packages. Measured, with a Dockerfile that `npm install`s into `/repo/node_modules`:

```
--- run WITHOUT bind mount ---   node_modules PRESENT
--- run WITH /tree bind-mounted at /repo (the real harness shape) ---   node_modules GONE
```

Installing at the **container root** instead — `printf '{…}' > /package.json && cd / && npm install <pkgs>`, giving `/node_modules` and `/node_modules/.bin` — works, because Node's resolver walks up from the importing file: `/repo/tests/x.test.js` → `/repo/tests/node_modules` → `/repo/node_modules` → `/node_modules`. Measured with **`NODE_PATH` unset** and as **uid 1000**:

```
NODE_PATH=[<unset>]  uid=1000
--- vitest from /node_modules/.bin, --no-cache, NO NODE_PATH ---
 Test Files  1 passed (1)
      Tests  3 passed (3)
  exit=0  /repo/node_modules: NO
--- jest from /node_modules/.bin, NO NODE_PATH ---
Test Suites: 1 passed, 1 total
Tests:       3 passed, 3 total
--- can a test file import a DEPENDENCY (not just the runner)? ---
 Test Files  1 passed (1)      Tests  1 passed (1)
--- can the non-root agent write to /node_modules? (should be NO) ---
touch: cannot touch '/node_modules/CANARY': Permission denied
```

The repo's own config is still discovered: a `/repo/vitest.config.js` written during the probe was honoured (`include` narrowed the run to one file) even though the binary lives at `/node_modules/.bin/vitest`.

`NODE_PATH` was **not** needed and is deliberately not set (D4).

### M9 — `node:22-bookworm-slim` already occupies uid 1000

```
uid=1000(node) gid=1000(node) groups=1000(node)
node:x:1000:1000::/home/node:/bin/bash
```

`useradd --create-home --uid 1000 eval` therefore fails with exit 4 on this base, where it succeeds on `python:3.12-slim-bookworm`. Measured: the build died with `returned a non-zero code: 4`. `userdel -r node` first makes it succeed. This one line is a large part of the argument for a second Dockerfile rather than one file with an `ARG` switching the `FROM` (D3).

### M10 — the image builds and the deps are 73 MB

`npm install` of `vitest ^3.2.4` + `jest ^30.2.0` produced a 73 MB `node_modules`. Resolved: `vitest/3.2.7 linux-arm64 node-v22.23.2`, `jest 30.5.0`.

---

## Design decisions, settled

### D1. `tests.framework`, a closed set of three, defaulting to `"pytest"`

```yaml
tests:
  framework: vitest        # pytest (default) | vitest | jest
  runner: ["/node_modules/.bin/vitest", "run", "--no-cache"]
```

It sits in `tests:` rather than in `image:` because it describes **how the gate reads the suite**, not what the container has in it — and `tests.runner`, whose meaning it changes, is the key immediately above it.

A **closed allowlist** validated in `tasks.py`, for the reason broadening 5's `_PYTHON_VERSIONS` is closed: an unknown string would reach `for_framework` and raise a `KeyError` out of the middle of preflight, with no manifest path in the message. The three entries are the three that have an adapter.

`tests.runner` stays exactly what it is: the argv, run inside the pinned image. Nothing is inferred from it. The framework and the runner are cross-checked (D7a) rather than derived from each other, because each catches the other's typo.

### D2. Node ids are `<file>::<full test name>`, validated per framework at load

pytest's shape is unchanged and **no new validation is added to it** — a rule added now could refuse a manifest that loads today, and there is exactly one manifest.

For `vitest` and `jest` the shape is `tests/formatting.test.ts::HelpFormatter writes usage for a program with no args`:

- **`::`, not `>` or a custom separator**, because `f2p_modules`' one-line "split from the left, maxsplit 1" already means "the file half of a node id" and stays correct verbatim; because `collection_error_modules`' whole parser is the presence or absence of `::`; and because a reader who knows one id shape knows both. A JS test title *may* contain `::`; `split("::", 1)` from the left is unambiguous anyway, which is the same argument the existing docstring makes for parametrized pytest ids.
- The **right half is the `fullName`** the report emits — `ancestorTitles` joined by single spaces plus `title` (M2) — not a `describe > it` rendering. It is what the reporter produces and what `-t` matches, so no translation layer can be wrong about it.
- **Validated at load** for the node frameworks: exactly one split, both halves non-empty, the file half not starting with `-` (it becomes a positional argument), and the file half under one of the declared `tests.paths` prefixes (it must be, or the scoped p2p run cannot see it — and an id outside the scope is the silent-no-op shape M1's last row already punishes).

`tests.p2p` ids take the same shape and the same validation.

**And no two declared ids may share a `fullName`, across files, within the scope.** This is the sharpest edge in the design and it has no pytest counterpart. `-t` matches `fullName` and knows nothing about which file a test came from (M14); the file positionals and the name pattern are ANDed across the whole run, never paired. So on a repo where `tests/a.test.js` and `tests/b.test.js` each carry `it('works', …)`:

- a quarantine of `tests/a.test.js::works` also deselects `tests/b.test.js::works`, **silently** — and `p2p_deselected` agrees, because two tests really were skipped;
- `select_args(("tests/a.test.js::works", "tests/b.test.js::other"))` passes both files and the pattern `^(?:works|other)$`, which also runs `tests/b.test.js::works`.

**Rejected: one invocation per file.** It would pair name and file exactly. It is refused because it multiplies every gated and graded suite run by the number of distinct f2p files, in a gate that already makes five suite invocations per task; because a per-file run changes what the framework collects (the same argument preflight's scoped-p2p assertion exists for); and because it would make the gated argv a *list* of argvs, which the byte-identity property is not stated over.

**Taken instead, two layers:**

1. **The loader refuses** a manifest whose declared `f2p` + `p2p` ids contain two entries with the same right half and different left halves. Costs no daemon, and catches the case the author created.
2. **Preflight asserts it against the real report** of the scoped p2p run: no two *executed* tests under `tests.paths` may share a `fullName`. This is the one that matters, because the collision that breaks a quarantine is between a declared id and a test the manifest never mentions — which layer 1 cannot see. Evidence key `duplicate_full_names`.

A repository whose suite genuinely carries duplicate titles within one scope is a **screening exclusion**, recorded in `HARVESTING.md` alongside the remedy (narrow `tests.paths`, or rename the titles in the task repo — which is a start-state edit and therefore a `strip_paths`/provenance question, not something the harness does).

The over-selection mechanism is written into `node_adapter`'s module docstring, not only here: a reader looking at `_names()` sees a `partition("::")[2]` that throws the file away, and has to be told immediately that two guards elsewhere are what make that safe.

### D3. Two Dockerfiles, not one with a switched `FROM`

`bakeoff/docker/eval-agent.Dockerfile` is **not touched by this broadening**. `bakeoff/docker/eval-agent-node.Dockerfile` is new.

The rejected alternative — one file with `ARG BASE_IMAGE` and `RUN` steps guarded by `if [ "$RUNTIME" = node ]` — was rejected on three measured differences that are not parameters:

1. **uid 1000 is taken** on the node base (M9). The python file's `useradd --create-home --uid 1000 eval` must become `userdel -r node && useradd …`, and a shell conditional around a `useradd` is exactly the shape that half-succeeds and leaves an image running as root — which Claude Code refuses, emitting zero events, on every arm.
2. The node image installs the runners at `/node_modules` with `npm install --prefix /` (M8). There is no python analogue and nothing to parameterise.
3. `PYTHONDONTWRITEBYTECODE=1` is meaningless there and the node equivalent is **nothing** (M7), so the shared `ENV` block is not actually shared.

What the two files **must** hold identically, and what a test asserts by reading both:

- `ARG CLAUDE_CODE_VERSION=2.1.220` and the `curl … install.sh` + `case`-guard that fails the build on drift.
- `git`, `ripgrep`, `coreutils`, `ca-certificates`, `curl`.
- a non-root user at uid 1000 named `eval`, owning `/repo` and `/eval/claude-config`.
- the `CLAUDE_CONFIG_DIR` / `DISABLE_AUTOUPDATER` / `DISABLE_UPDATES` / `DISABLE_TELEMETRY` / `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` block, set **after** the install.
- `WORKDIR /repo` and `ENTRYPOINT []`.

The node file additionally pins `ARG NODE_TEST_RUNNERS="vitest@3.2.7 jest@30.5.0"` and asserts both versions after install, for the reason `PYTEST_VERSION` is pinned: it is part of the environment every arm is compared in.

The `ARG` before `FROM` is `BASE_NODE_VERSION`, never `NODE_VERSION` — the official `node:` images set their own `ENV NODE_VERSION` (`22.23.2`), and broadening 5 measured that `ENV` beats a redeclared `ARG` after `FROM`, so the collision would silently resolve to the patch-level string.

### D4. Runner dependencies live at `/node_modules`, not in the run tree and not on `NODE_PATH`

Measured (M8): baking at `/repo/node_modules` is **replaced by the bind mount** — the exact defect `pip install -e .` avoids, with no equivalent escape. `/node_modules` resolves for the runner binary, for the test files' imports of the runner, and for a transitive dependency, with `NODE_PATH` unset and as the non-root eval user, and it is not writable by that user.

The three rejected alternatives:

- **`NODE_PATH=/node_modules`.** Not needed (measured with it unset), legacy, ignored by ESM resolution, and bypassed by jest's and vite's own resolvers. Setting it would be an unmeasured belt whose failure mode is that it hides which mechanism actually worked.
- **A `/repo/node_modules` symlink to `/node_modules`, created in `materialize`.** It is a file inside the tree, so it is in `git status`, in `gitignore_extra`, and in every submission diff unless ignored — and if it *is* ignored, `git clean -xfd` (which preflight runs) deletes it and the second suite invocation of the same preflight resolves nothing.
- **`npm ci` into the run tree at materialize time.** Needs network at materialize (materialize is offline today and must stay so — it is called by the oracle and the grader), costs 73 MB per run tree across a matrix that materializes one per cell, and makes the tree a function of the registry rather than of the manifest.

`/node_modules/.bin` is put on `PATH` by the Dockerfile. Measured (M12): the install changes no environment, so a bare `vitest` is exit 127 while `npx vitest` and `npm test` work — an inconsistency §3.3's loop would make the agent discover by failing, since the agent invents its own commands.

**Task-specific dependencies go through `image.build`, and the recommended verb is `npm install`, not `npm ci`.** `npm ci`'s documented contract is to **delete `node_modules` before installing**, which at the `/` prefix is exactly where the pinned runners live. Whether it actually fires depends on which `package.json`/`package-lock.json` pair npm resolves for the given prefix and cwd — the reviewer measured `/node_modules/.bin/vitest` gone after `npm ci --prefix /`; reproducing it here with the lockfile at `/repo` rather than at `/`, it survived (M13). The disagreement *is* the finding: a convention that is right only under an unstated cwd is not a convention, and a task author writing an `image.build` line has no reason to know which one they are in.

So `HARVESTING.md` recommends `["sh", "-lc", "npm install --prefix / --omit=dev <deps>"]` — measured additive, leaving both runners in place and installing the repo's dependency beside them — **and the recommendation is backed by a post-condition rather than trusted** (D15). A convention this plan cannot enforce is one a task will eventually violate, and its failure mode is a task image whose runner is simply missing, which reaches the model as exit 127 on every arm.

### D15. The task image re-asserts the base's pinned runners after `image.build`

`render_dockerfile` emits, after the last `image.build` `RUN` and only for a node base, a step that fails the **build** if `/node_modules/.bin/vitest --version` or `jest --version` no longer matches the base's pins. The versions come from the base image's own `ENV` (`BAKEOFF_VITEST_VERSION`, `BAKEOFF_JEST_VERSION`, written by the node Dockerfile), never from a second constant in `images.py` — two copies of a pin is how one moves.

It is a **build-time** check rather than a preflight one on purpose: preflight would catch it too (the f2p run would exit 127), but it would report it as `KIND_ENVIRONMENT` on a task whose image was already built and pushed through a task-image cache, and the operator's remedy is a rebuild either way. Failing the build names the cause at the step that caused it.

This is the only place in the plan where `render_dockerfile` changes, and the change is guarded on the runtime so a python task image is byte-identical to today's — which `test_images.py`'s existing rendering tests already pin.

### D5. The adapter interface — one `p2p_args` entry point, because `-t` does not compose

`bakeoff/src/bakeoff/runners/__init__.py` defines:

```python
@dataclass(frozen=True)
class Outcome:
    """What one suite invocation did, in terms no framework owns."""
    kind: str                                   # see KIND_* below
    exit_code: int
    failed_ids: frozenset[str] = frozenset()
    #: Files the runner could not LOAD. `None` means the question was not
    #: asked (this exit code is not a collection failure); an EMPTY frozenset
    #: means it was asked and nothing parsed -- which is a refusal, not an
    #: absence. The two must not collapse: preflight's message says
    #: "empty" for one and "not parsed" for the other.
    errored_files: frozenset[str] | None = None
    #: Rootdir-relative paths the runner actually executed, or `None` when the
    #: framework does not report them (pytest). Node only: M5's positional
    #: filters over-match, so the scope has to be verified after the fact.
    files_run: tuple[str, ...] | None = None
    #: Requested ids that produced no terminal status. Node only, `None` for
    #: pytest, which answers this with exit 4 instead. See M1's last row.
    not_run: frozenset[str] = frozenset()
    explain: str = ""

KIND_PASSED = "passed"
KIND_FAILED = "failed"
KIND_LOAD_ERROR = "load_error"
KIND_NOTHING_RAN = "nothing_ran"
KIND_ENVIRONMENT = "environment"
```

and the protocol:

```python
class RunnerAdapter(Protocol):
    name: str
    #: A substring that must appear in `tests.runner` for this framework.
    runner_marker: str
    #: Flags this framework needs so the suite writes nothing into the tree.
    no_cache_args: tuple[str, ...]

    def select_args(self, node_ids: tuple[str, ...]) -> list[str]: ...
    def p2p_args(self, *, selected: tuple[str, ...], scope: tuple[str, ...],
                 deselected: tuple[str, ...], ignored: tuple[str, ...]) -> list[str]: ...
    def report_args(self, report_path: str) -> list[str]: ...
    def report_path(self) -> str | None: ...
    def classify(self, *, exit_code: int, stdout: str, stderr: str,
                 report: dict | None) -> Outcome: ...
    def parse_deselected(self, *, stdout: str, report: dict | None) -> int | None: ...
    def module_of(self, node_id: str) -> str: ...
    def validate_node_id(self, node_id: str, paths: tuple[str, ...], where: str) -> None: ...
    def hypothesis_interpreter(self, runner: tuple[str, ...]) -> str | None: ...
    def explain(self, code: int) -> str: ...
```

**Why `p2p_args` and not composable `select`/`deselect`.** The p2p run must express, in **one** pattern, "select these ids" *and* "not these" (the f2p set plus the oracle quarantine), and M4 measured what happens otherwise — the two frameworks disagree and neither answer is usable. vitest **rejects** a second `-t` (`Expected a single value for option "-t, --testNamePattern <pattern>", received ["a", "b"]`, exit 1, **no report file**), which lands as `KIND_ENVIRONMENT` and is at least loud. jest **comma-joins** them into `adds,subs`, a regex matching neither test, and exits **0** having run nothing — M1's silent hole reached by an argv nobody meant to write. Two methods whose outputs are concatenated produce two `-t` flags and therefore one of those two failures, which is the exact class of silent-wrong-argv this repo already lost five drafts of a diff parser to. One method that owns the whole argv cannot be composed wrongly.

**The pytest implementation reproduces today's ordering exactly**, which is what makes the untouched argv-identity test the gate:

```python
def p2p_args(self, *, selected, scope, deselected, ignored):
    if selected:
        head = list(selected)
    else:
        head = list(scope)
    out = list(head)
    for node_id in deselected:
        out += ["--deselect", node_id]
    out += [f"--ignore={path}" for path in ignored]
    return out
```

`_Runner.pass_to_pass` keeps the branch decision (it is where the mutation anchors live) and calls this once:

```python
def pass_to_pass(self, tests, extra_deselect=(), scope=(), ignore=()):
    if tests.p2p:
        return self.run(self.adapter.p2p_args(
            selected=tuple(tests.p2p), scope=(),
            deselected=tuple(extra_deselect), ignored=tuple(ignore)))
    return self.run(self.adapter.p2p_args(
        selected=(), scope=tuple(scope),
        deselected=tuple(tests.f2p) + tuple(extra_deselect), ignored=tuple(ignore)))
```

Check the two literals the existing test pins. Deselect branch: `scope` empty, `deselected = ("tests/a.py::test_one",)`, `ignored` empty → `["--deselect", "tests/a.py::test_one"]`. Explicit branch: `selected = ("tests/b.py::test_two",)` → `["tests/b.py::test_two"]`. Both byte-identical to today.

**Module naming.** `runners/pytest_adapter.py` and `runners/node_adapter.py`, **not** `runners/pytest.py`. A submodule named `pytest` does not actually shadow site-packages' pytest for absolute imports, but `from bakeoff.runners import pytest` in a test file that also needs the real `pytest` is a trap nobody should have to reason about, and the cost of avoiding it is six characters.

One module for both node frameworks, parameterised by a small `_NodeFlavour` record (`name`, `runner_marker`, `report_args`, `no_cache_args`), because M2 shows one classifier covers both: the load-error discriminator is `status == "failed"` with empty `assertionResults`, which does not need jest's `numRuntimeErrorTestSuites` and therefore does not need a jest-specific branch.

### D6. Node classification, in the order the branches must be taken

`classify` consults the report and never the exit code, except to record it. The order is not incidental:

1. **`report is None`** (the file was absent, or unreadable, or not JSON) → `KIND_ENVIRONMENT`, `errored_files=None`. Measured: a config error exits 1 and writes **no file** on both frameworks, and so does a runner that could not start at all. This is the one honest reading of "the command produced no evidence", and it is what keeps a broken config off the model's record.
2. **`errored_files` non-empty** → `KIND_LOAD_ERROR`. `errored_files` is every `testResults[]` entry with `status == "failed"` and empty `assertionResults`, made rootdir-relative. **Before the failure branch**, because M2's mixed row (one good file, one unloadable file) reports three *passing* tests and zero failing ones — read in the other order it grades as `passed`, and a task whose f2p file stopped importing would be scored as solved.
3. **`failed_ids` non-empty** → `KIND_FAILED`. `failed_ids` is every assertion with `status == "failed"`, rendered `<relpath>::<fullName>`.
4. **`ran == 0`** → `KIND_NOTHING_RAN`, where `ran` counts assertions whose status is `passed` or `failed`. This is the node analogue of pytest's exit 5 **and** the fix for M1's last row: a `-t` pattern matching nothing, and a quarantine that swallowed the whole p2p list, both land here instead of exiting 0 as a pass. It also correctly catches jest's `success: true` on "no test files matched".
5. otherwise → `KIND_PASSED`.

**The confined-collection-error rule (broadening 2), one runtime over.** `KIND_LOAD_ERROR` is exactly the shape of "the fix adds an export and the test file cannot import it yet" — measured as `message: "Cannot find module '../src/missing.js' imported from '/proj/tests/broken.test.js'"` with an empty `assertionResults`. The **comparison against the declared f2p set is deliberately not made inside `classify`**, for the reason `collection_error_modules`' docstring already gives: preflight needs **equality** (every declared f2p file errored and no stranger did) and the grader needs **containment** (never accuse for anything outside the task), and folding both behind a flag makes each call site unreadable about which claim it is making. `Outcome.errored_files` is the channel; both callers keep their own comparison and their own messages.

`Outcome.not_run` is filled by a separate `verify_selected(report, requested_ids)` helper rather than by `classify`, because only the caller knows what it asked for.

### D7. What preflight gains, and the three node-only assertions

**a. The runner gate becomes framework-driven and keeps its teeth.** Today:

```python
if not any("pytest" in part for part in tests.runner):
```

becomes `if not any(adapter.runner_marker in part for part in tests.runner):`, with `runner_marker` `"pytest"`, `"vitest"`, `"jest"`. For a pytest task the check is character-for-character the same test it is today. Its message changes from "preflight can only distinguish … for pytest" to one naming the declared framework and the marker it did not find — the failure it now catches is a manifest that declares `framework: vitest` and a runner that invokes jest, which would otherwise be classified by the wrong adapter.

**b. Every declared f2p id must have RUN, not merely not-failed.** Node only (`Outcome.not_run` is empty for pytest, which answers this with exit 4). After the f2p-before run, when the outcome is not a load error, any id in `not_run` is a problem naming M1: `-t` matching nothing exits **0** with `Tests 3 skipped (3)`, so a renamed test makes an unsatisfiable task read as a green gate and then as a solved run for every arm. Evidence key `f2p_before_not_run`.

**c. The scoped p2p run must not have left the declared scope.** Node only. M5: `vitest run tests/` matched `/repo/jtests/fail.test.cjs`, because the positional is a substring filter over the absolute path, not a path. The scoped p2p run exists to keep the agent's scratch files at the rootdir out of the regression check; a filter that over-matches restores exactly what it was added to remove. After the scoped run, every entry of `Outcome.files_run` must be under a declared `tests.paths` prefix (reusing `tasks._under`, the same component-wise matcher the diff split uses — **not** `startswith`, which is what a substring filter already got wrong). Evidence keys `scope_files_run` and `scope_files_outside`.

**d. The framework's cache flags must be in `tests.runner`.** Node only, and the asymmetry is the point. For pytest, an absent `-p no:cacheprovider` writes `.pytest_cache/` into the tree and preflight's existing "running the suite leaves the tree dirty" check fires loudly — nothing more is needed and nothing is added, so no manifest that loads today can be refused. For vitest, the artifact is `node_modules/.vite` (M6), and **every JavaScript repository's `.gitignore` carries `node_modules/`**, so the dirty-tree check is blind and the flag's absence is invisible. `no_cache_args` is `("--no-cache",)` for vitest, `()` for jest (measured: jest's default `cacheDirectory` is `/tmp/jest_0`, outside the tree, and jest wrote nothing into the tree in any measured run), `("-p", "no:cacheprovider")` for pytest — **advisory only** there. Evidence key `runner_cache_flags`.

**e. `_runner_python` becomes `adapter.hypothesis_interpreter(runner)`.** The hypothesis block is a Python-ecosystem determinism check (broadening 3): it asks whether the declared test paths `import hypothesis` and whether `image.env` declares `CI`. The pytest adapter returns today's `_runner_python(runner)` verbatim; the node adapter returns `None`, preflight skips the whole block, and both evidence keys stay `None` — a recorded absence, not a claim that the suite is deterministic. The JS equivalents (`fast-check`, `jest-fuzz`) have their own seed mechanisms and no such check exists; that goes to `TASKS.md` as named follow-up work rather than being silently assumed away.

**f. Evidence gains `framework`.** A cached verdict outlives the code that wrote it, and every other evidence key in it now means something framework-dependent.

`PREFLIGHT_VERSION` +1: a verdict cached under the old gate was written by one that could not read a node suite at all, and — for pytest tasks — by one that made none of the assertions above.

### D8. The report path is FIXED, and stale reports are impossible

`report_path()` returns `"/tmp/bakeoff-run-report.json"` for the node adapters and `None` for pytest.

**Fixed, not per-invocation.** The path is an argv element (`--outputFile=…`), and the gated argv must be byte-identical to the graded argv. A uuid or a counter in it breaks that property for every node task, silently, in the one place this codebase has a test for.

**Deleted before every invocation, as a separate exec.** Measured: a config error writes no file, so a stale report from the *previous* invocation would be read as *this* run's evidence — a passing report standing in for a run that never happened. `_Runner.run` therefore issues `["rm", "-f", <path>]` before the measured command and reads the file back with `["cat", <path>]` after. Neither is part of the measured argv, so argv identity is untouched. `/tmp` is inside the container and never bind-mounted, so the file cannot reach a submission diff.

A `cat` that exits non-zero, or output that is not JSON, becomes `report=None`, which D6 step 1 turns into `KIND_ENVIRONMENT`. That is the correct reading and it is the same reading a config error gets.

### D9. The oracle supports the node quarantine, and gains the hole M1 opened

`--deselect` has no node equivalent, but the negative lookahead does (M4), and it is composed into the single `-t` pattern by `p2p_args`. So the quarantine works on vitest and jest and nothing is unsupported.

What *does* change is `derive_quarantine`'s existing "the quarantine covers the entire declared p2p list" guard. Its stated mechanism is *"pytest would exit 5, which the grader records as an infrastructure problem"*. On node, deselecting everything exits **0** with every test skipped (M1). D6's rule 4 is what restores it: `ran == 0` is `KIND_NOTHING_RAN` whatever the exit code, so the grader still records `SCOPE_COLLECTED_NOTHING`. The guard's docstring is updated to say the exit code is not what carries this any more.

`_classify` reads `Outcome.kind` instead of the two exit constants; the three-way (`passed` → empty set, `failed` → `failed_ids`, anything else → `OracleError`) is unchanged in shape, and `load_error`/`nothing_ran`/`environment` all reach the raise — which is right, because a reference-state run that could not load a file or ran nothing is a broken oracle, not a flake.

`ORACLE_VERSION` +1: a cached quarantine derived before this change was derived by rules that could not see a node run at all.

### D10. The grader

- `_check_f2p` and `_check_p2p` branch on `Outcome.kind` instead of on exit codes. The `_TIMEOUT_EXIT == 124` branch and `_INFRA_EXITS` stay **outside** the adapter and unchanged: 124/125/126/127/137 are coreutils and container facts, not framework facts, and folding them in would put a docker OOM behind a runner's opinion.
- `_check_f2p` gains one branch for node: `Outcome.not_run` non-empty → `state.environment(...)`, never `state.fail(...)`. The test half is restored by check 2, so the f2p names are the manifest's, not the model's; an id that stopped matching is the grader's problem and stamping `f2p_failed` for it is an accusation the model did not earn.
- `_SUMMARY_LINE`, `_DESELECTED` and `parse_deselected` **move into `runners/pytest_adapter.py`** and are reached as `adapter.parse_deselected(stdout=…, report=…)`. `parse_deselected` stays importable from `bakeoff.grader` as a re-export so its existing tests and any external caller are untouched.
- Node's `parse_deselected` returns `report["numPendingTests"]`, or `None` when the report is absent. The units are **not** the same as pytest's: pytest counts deselections it was asked to make, node counts tests that did not run for any reason, `it.skip` included. That is precisely the caveat `p2p_deselected`'s docstring already carries for pytest (`pallets/click`'s `addopts = "-m 'not stress'"` reports 30,007 against 7 requested), one framework over — the floor claim stays honest and its power to fire is weaker still. Which is why the record has to say which framework produced the number (D11).
- `GRADER_VERSION` +1.

### D11. `GradeRecord.framework`, and no `RunRecord` change

`GradeRecord` gains `framework: str = ""` (`""` = the grade predates the field), carried `_State` → `LadderResult` → `build_grade_record`. `GRADE_SCHEMA_VERSION` minor +1.

It is an **observation**, not configuration echoed back: it names the adapter that produced this record's `f2p_failed_node_ids`, `p2p_failed_node_ids` and `p2p_deselected`, whose id shapes and units differ per framework. Without it a reader summing `p2p_deselected` across a mixed task set adds pytest deselections to node skips and gets a number that is not a count of anything — the same defect `Versions.pricing_basis` exists to prevent one subsystem over.

`SCHEMA_VERSION` does **not** move. `RunRecord` describes the agent's episode; nothing about it changes, and a `framework` field copied from the manifest onto it would be configuration reported as observation. The join is the one broadening 5 already documented: `task_id` + `Versions.task_set_commit` name the `task.yaml` revision, and `Versions.container_image_digest` pins the base the run executed in.

### D12. `base_tag` and `build_base_images` grow a runtime key

Broadening 5 ships `base_tag(python_version) -> "bakeoff-eval-agent:base-3.12"` and `build_base_images(repo_root, versions) -> dict[str, str]`, and its own D4 says in as many words that when node joins, `base_tag` gains a second keyword and the mapping key becomes whatever tuple identifies a base, and that those two functions are the only places that change. Taking it up:

```python
def base_tag(runtime: str, version: str) -> str:          # "bakeoff-eval-agent:base-python-3.12"
def build_base_images(repo_root, runtimes: set[tuple[str, str]]) -> dict[tuple[str, str], str]
```

`build_base_image(repo_root, runtime, version, tag=...)` picks the Dockerfile (`eval-agent.Dockerfile` / `eval-agent-node.Dockerfile`) and the build arg (`BASE_PYTHON_VERSION` / `BASE_NODE_VERSION`) from `runtime`.

**The tag string moves and nothing else does.** `bakeoff-eval-agent:base-3.12` becomes `bakeoff-eval-agent:base-python-3.12`. The python Dockerfile is unchanged, so the **image id is unchanged**, and every cache in this repo keys on the id (`preflight_cache_key`, `oracle_fingerprint`, `Versions.container_image_digest`) — so no warm verdict is invalidated by the rename and the click task's stored records still join. The old tag is left orphaned on any machine that has one, pointing at the same id; nothing reads it.

**The runtime comes from the framework, not from a second key.** `image.node: "22"` selects the node base's *version*; which base a task gets is `"node" if task.tests.framework in ("vitest", "jest") else "python"`. Declaring `image.node` on a pytest task, or `image.python` on a node task, is a **load error** — two keys that can each imply a runtime is two sources for one fact, and the failure of a disagreement between them is a task gated in one interpreter and run in another.

`_NODE_VERSIONS = frozenset({"22"})`, default `"22"`, with broadening 5's three-step growth rule restated in the constant's own comment: build the real base at that version, confirm the Claude Code and runner pin assertions fire, add the string here with the date, add the row to `HARVESTING.md`. `20` and `24` exist as tags and are deliberately absent — nobody has built the eval image on them, and an unmeasured entry is the constant claiming something it does not know.

`run_matrix`'s `assert_one_agent` (broadening 5's cross-base Claude Code check) now spans **both runtimes**, which is where it earns the most: two Dockerfiles carrying two `ARG CLAUDE_CODE_VERSION` lines is a real way for two arms of one comparison to run different agents, and it is invisible in the record.

### D13. Spec OPEN-2 gets a decision, in the table, not silently

`CONTEXT.md` requires this plan to say what it decides about OPEN-2's language scope and to add a note to the spec's open-questions table rather than resolving the row away. What is decided here is exactly the **runner** half:

> **Decided 2026-09-01 (broadening 7).** The harness's *executable* language scope is Python (pytest) and JavaScript/TypeScript (vitest, jest); `tests.framework` names which, and the runner adapter in `src/bakeoff/runners/` is the whole of the per-language surface. Type-check and lint (checks 4 and 7) remain manifest-declared `grading.*` argvs and are language-agnostic, as this row already recorded. **What stays open is the repository question** — which Pindrop repos, mono or multi — which is what actually blocks Phase 2, and adding a third language means adding a third adapter and its base image, not changing the ladder.

The row is **not** closed, its Notes cell gains that paragraph, and `Blocks` stays `Phase 2`.

### D14. What is deliberately NOT built

- **No `.pyc` analogue in the node image.** M7 measured that the failure does not reproduce. Adding an unnecessary cache-disabling `ENV` would be a mitigation for a defect nobody has seen, and its cost is that the next author cannot tell it from the python file's, which mitigates a defect that was measured twice.
- **No third framework, no mocha, no `node --test`.** YAGNI, and each is an adapter plus a measured exit-code and report table.
- **No property-based determinism check for node.** D7e; `TASKS.md` follow-up.
- **No `NODE_PATH`.** D4.
- **No change to `render_dockerfile`'s task-image template.** It already takes `base_image` as a string and emits `apt`/`pip`/`build`/`env`; a node task's dependency install goes through `image.build` exactly as a python task's non-pip step does. `image.pip` on a node task is not refused — a JS repo may legitimately need a python tool — and that is a deliberate non-decision, recorded in `HARVESTING.md`.

---

## File structure

| File | Change |
|---|---|
| `bakeoff/src/bakeoff/runners/__init__.py` | **new** — `Outcome`, `KIND_*`, `RunnerAdapter` protocol, `FRAMEWORKS`, `for_framework()` |
| `bakeoff/src/bakeoff/runners/pytest_adapter.py` | **new** — every pytest constant and parser, moved: `EXIT_*`, `_EXIT_MEANING`, `_FAILED_LINE`, `failed_node_ids`, `collection_error_modules`, `f2p_modules`, `_SUMMARY_LINE`, `_DESELECTED`, `parse_deselected`, `_PYTHON_BASENAME`, `_runner_python` |
| `bakeoff/src/bakeoff/runners/node_adapter.py` | **new** — one classifier for vitest and jest, `_NodeFlavour`, `verify_selected` |
| `bakeoff/src/bakeoff/preflight.py` | re-export the moved names under their exact bare names; `_Runner` takes an adapter; the runner gate, the hypothesis probe and every exit-code branch go through it; three node-only assertions; evidence keys; `PREFLIGHT_VERSION` +1 |
| `bakeoff/src/bakeoff/oracle.py` | `_classify` reads `Outcome.kind`; `_ORACLE_EXIT_MEANING` composes with `adapter.explain`; `ORACLE_VERSION` +1 |
| `bakeoff/src/bakeoff/grader.py` | `_check_f2p`/`_check_p2p` read `Outcome`; `parse_deselected` re-exported from the pytest adapter; the `not_run` environment branch; `state.framework`; `GRADER_VERSION` +1 |
| `bakeoff/src/bakeoff/grade_schema.py` | `GradeRecord.framework`; `GRADE_SCHEMA_VERSION` minor +1 |
| `bakeoff/src/bakeoff/tasks.py` | `_FRAMEWORKS`, `_framework()`, `TaskTests.framework`; `_NODE_VERSIONS`, `TaskImage.node`; the runtime cross-check; per-framework node-id validation |
| `bakeoff/src/bakeoff/images.py` | `base_tag(runtime, version)`, `build_base_image(repo_root, runtime, version, tag=…)`, `build_base_images(repo_root, runtimes)`; `render_dockerfile` re-asserts the base's runner pins after `image.build` on a node base (D15) |
| `bakeoff/docker/eval-agent-node.Dockerfile` | **new** |
| `bakeoff/scripts/run_matrix.py` | build the `(runtime, version)` base set; `assert_one_agent` spans both runtimes; `resolve_tasks` indexes by the pair |
| `bakeoff/scripts/grade.py` | `task_resolver`'s cache keyed by the pair |
| `bakeoff/scripts/mutation_check.py` | repoint P3, P4; rewrite G4; add six new anchors (Task 7) |
| `bakeoff/fixtures/node_task/` | **new** — a tiny real vitest repo, the `smoke_task` shape |
| `bakeoff/tests/test_runners.py` | **new** — the adapter package, both adapters, on captured JSON fixtures |
| `bakeoff/tests/fixtures/node_reports/*.json` | **new** — the eight reports captured in M2 |
| `bakeoff/tests/test_preflight.py`, `test_oracle.py`, `test_grader.py`, `test_tasks.py`, `test_images.py`, `test_run_matrix.py` | new sections; **no existing test edited** in Tasks 1–3 |
| `bakeoff/tests/test_integration_node_task.py` | **new** — `integration` + `task_image` |
| `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`, click `task.yaml`, the design spec's OPEN-2 row, `tasks/todo.md`, `TASKS.md` | docs |

**Line numbers are deliberately absent from the task bodies.** Broadenings 1–6 all land first and four of them edit `preflight.py`; broadening 4 deletes `preflight(timeout_s=)` and `ensure_oracle(timeout_s=)` in favour of `task.budget.suite_timeout_s`, renames `GRADE_TIMEOUT_S` to `SCAN_TIMEOUT_S` for the gitleaks call only, and adds `_Runner.last_timeout_s`; broadening 5 changes `resolve_tasks`' signature and `build_base_image`'s. Anchor on symbol names: `PREFLIGHT_VERSION`, `_Runner`, `pass_to_pass`, `failed_node_ids`, `collection_error_modules`, `f2p_modules`, `_check_f2p`, `_check_p2p`, `parse_deselected`, `_classify`, `derive_quarantine`, `TaskTests`, `TaskImage`, `base_tag`, `build_base_images`, `task_resolver`, `resolve_tasks`, `main`.

**Task order** is: behaviour-preserving refactor first and fully gated (1–3), then the manifest key (4), then the image (5), then the node adapter against captured fixtures with no Docker (6), then wiring it into the three consumers (7), then the record field (8), then one real end-to-end node task (9), then docs (10). Tasks 1–3 are the regression risk and they land before anything node-shaped exists, so a failure there is unambiguous.

---

## Task 1: the adapter package and the pytest adapter (no consumer changes yet)

The pytest logic moves; every module that uses it keeps importing it under the **exact same bare name** via a re-export. Nothing behaves differently. This task exists on its own so that "the move was lossless" is provable before anything calls the new seam.

**Files:**
- Create: `bakeoff/src/bakeoff/runners/__init__.py`, `bakeoff/src/bakeoff/runners/pytest_adapter.py`
- Modify: `bakeoff/src/bakeoff/preflight.py` (delete the moved definitions, add re-export imports), `bakeoff/src/bakeoff/grader.py` (same for `_SUMMARY_LINE`, `_DESELECTED`, `parse_deselected`)
- Test: `bakeoff/tests/test_runners.py` (new)

**Interfaces:**
- Produces: `bakeoff.runners.Outcome`, `KIND_PASSED`/`KIND_FAILED`/`KIND_LOAD_ERROR`/`KIND_NOTHING_RAN`/`KIND_ENVIRONMENT`, `RunnerAdapter` (Protocol), `FRAMEWORKS: tuple[str, ...]`, `for_framework(name: str) -> RunnerAdapter`; `bakeoff.runners.pytest_adapter.PytestAdapter` and the module-level singleton `ADAPTER`.
- Preserves: `bakeoff.preflight.EXIT_ALL_PASSED`, `EXIT_TESTS_FAILED`, `EXIT_COLLECTION_INTERRUPTED`, `EXIT_USAGE_ERROR`, `EXIT_NOTHING_COLLECTED`, `EXIT_COLLECTION_FAILURES`, `_EXIT_MEANING`, `_FAILED_LINE`, `failed_node_ids`, `collection_error_modules`, `f2p_modules`, `_PYTHON_BASENAME`, `_runner_python`; `bakeoff.grader.parse_deselected`, `_SUMMARY_LINE`, `_DESELECTED` — all importable from their current modules under their current names.
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

Create `bakeoff/tests/test_runners.py`:

```python
"""The runner adapter seam.

Every red/green judgement this harness makes used to be a branch on pytest's
exit codes. That is not portable -- measured 2026-09-01, vitest 3.2.7 and jest
30.5.0 exit 1 for a failing test, an unresolvable import, a syntax error, a
nonexistent file argument AND a broken config -- so the judgement moves behind
an adapter and the exit code becomes one input among several.

This file pins the adapter contract. `test_preflight.py`, `test_oracle.py` and
`test_grader.py` keep pinning what the CONSUMERS do with it.
"""

import pytest

from bakeoff.runners import (
    KIND_ENVIRONMENT,
    KIND_FAILED,
    KIND_LOAD_ERROR,
    KIND_NOTHING_RAN,
    KIND_PASSED,
    FRAMEWORKS,
    for_framework,
)


def test_the_three_frameworks_resolve_and_nothing_else_does():
    """`for_framework` is reached from `load_task`'s validated value, so an
    unknown name here means the allowlist and the registry disagreed -- and the
    failure of that disagreement is a KeyError out of the middle of preflight
    with no manifest path in it."""
    assert set(FRAMEWORKS) == {"pytest", "vitest", "jest"}
    for name in FRAMEWORKS:
        assert for_framework(name).name == name
    with pytest.raises(KeyError):
        for_framework("mocha")


# --- the pytest adapter reproduces today's behaviour -------------------------


def test_pytest_p2p_args_deselect_branch_is_byte_identical_to_todays_argv():
    """The whole reason the refactor is safe. `_Runner.pass_to_pass` used to
    build this list inline; if the adapter emits anything else -- a reordered
    segment, an inserted flag -- the gated command stops being the graded one
    and the oracle stops describing the thing being graded."""
    adapter = for_framework("pytest")

    assert adapter.p2p_args(
        selected=(), scope=(), deselected=("tests/a.py::test_one",), ignored=()
    ) == ["--deselect", "tests/a.py::test_one"]


def test_pytest_p2p_args_explicit_branch_is_byte_identical_to_todays_argv():
    adapter = for_framework("pytest")

    assert adapter.p2p_args(
        selected=("tests/b.py::test_two",), scope=(), deselected=(), ignored=()
    ) == ["tests/b.py::test_two"]


def test_pytest_p2p_args_keeps_scope_then_f2p_then_quarantine_then_ignore():
    """The ORDER is the argv. Today `pass_to_pass` emits scope, then one
    --deselect per f2p id, then one per quarantined id, then the --ignores; the
    combined `deselected` tuple is (f2p + quarantine) precisely so that this
    stays true."""
    adapter = for_framework("pytest")

    assert adapter.p2p_args(
        selected=(),
        scope=("tests/",),
        deselected=("tests/a.py::test_one", "tests/c.py::test_flaky"),
        ignored=("tests/broken.py",),
    ) == [
        "tests/",
        "--deselect", "tests/a.py::test_one",
        "--deselect", "tests/c.py::test_flaky",
        "--ignore=tests/broken.py",
    ]


def test_pytest_select_args_are_the_bare_node_ids():
    assert for_framework("pytest").select_args(("a::b", "c::d")) == ["a::b", "c::d"]


def test_pytest_writes_no_report_and_asks_for_no_reporter_flags():
    """pytest's evidence is its exit code and its `-q` summary lines. Asking it
    for a JSON report would add a second thing that can be wrong about what
    happened, inside the check that exists to be right about it -- the reason
    `failed_node_ids` parses the summary rather than a JUnit file."""
    adapter = for_framework("pytest")

    assert adapter.report_path() is None
    assert adapter.report_args("/tmp/x.json") == []


@pytest.mark.parametrize(
    "exit_code, output, kind",
    [
        (0, "3 passed in 0.01s", KIND_PASSED),
        (1, "FAILED tests/a.py::test_one\n1 failed in 0.01s", KIND_FAILED),
        (4, "ERROR tests/new.py", KIND_LOAD_ERROR),
        (2, "ERROR tests/new.py", KIND_LOAD_ERROR),
        (5, "no tests ran in 0.00s", KIND_NOTHING_RAN),
        (4, "ERROR: not found: tests/a.py::gone", KIND_ENVIRONMENT),
        (3, "INTERNALERROR", KIND_ENVIRONMENT),
    ],
)
def test_pytest_classify_maps_each_exit_code_to_the_kind_it_means(
    exit_code, output, kind
):
    """The Phase 0c failure in one table: 1, 2, 3, 4 and 5 are all non-zero and
    only ONE of them means a test failed. `ERROR: not found:` carries a colon
    `_FAILED_LINE` does not match, so it parses to an empty set and stays an
    environment problem -- a manifest naming a renamed test must keep stopping
    the matrix."""
    outcome = for_framework("pytest").classify(
        exit_code=exit_code, stdout=output, stderr="", report=None
    )

    assert outcome.kind == kind
    assert outcome.exit_code == exit_code


def test_pytest_classify_distinguishes_parsed_empty_from_never_parsed():
    """Two different absences that render identically are the same defect one
    layer down. preflight's message says "empty" for one and "not parsed (exit
    N is not a collection failure)" for the other, and it can only do that
    while these stay apart."""
    adapter = for_framework("pytest")

    parsed_empty = adapter.classify(
        exit_code=4, stdout="ERROR: not found: x", stderr="", report=None
    )
    never_parsed = adapter.classify(
        exit_code=3, stdout="INTERNALERROR", stderr="", report=None
    )

    assert parsed_empty.errored_files == frozenset()
    assert never_parsed.errored_files is None


def test_pytest_classify_reports_the_failed_node_ids():
    outcome = for_framework("pytest").classify(
        exit_code=1,
        stdout="FAILED tests/a.py::test_one\nERROR tests/b.py::test_two\n",
        stderr="",
        report=None,
    )

    assert outcome.failed_ids == {"tests/a.py::test_one", "tests/b.py::test_two"}


def test_pytest_module_of_splits_once_from_the_left():
    """A parametrized id can carry `::` inside its brackets and a class-scoped
    id carries two, so rsplit or an unbounded split names something that is not
    a module."""
    adapter = for_framework("pytest")

    assert adapter.module_of("tests/a.py::test_one[x::y]") == "tests/a.py"
    assert adapter.module_of("tests/a.py::Klass::test_one") == "tests/a.py"


def test_pytest_parse_deselected_is_the_summary_line_parser():
    adapter = for_framework("pytest")

    assert adapter.parse_deselected(
        stdout="1 passed, 3 deselected in 0.00s", report=None
    ) == 3
    assert adapter.parse_deselected(
        stdout="4 passed in 0.00s", report=None
    ) == 0
    assert adapter.parse_deselected(stdout="", report=None) is None


def test_pytest_hypothesis_interpreter_comes_off_the_runner():
    """A runner of ["/opt/venv/bin/python", "-m", "pytest"] resolves imports
    against that venv, so probing whichever python is first on PATH would
    answer a question about a different environment."""
    adapter = for_framework("pytest")

    assert adapter.hypothesis_interpreter(
        ("/opt/venv/bin/python", "-m", "pytest")) == "/opt/venv/bin/python"
    assert adapter.hypothesis_interpreter(("pytest", "-q")) == "python"


def test_pytest_no_cache_args_names_the_flag_the_manifest_should_carry():
    """Advisory for pytest and asserted for node -- see the plan's D7d. The
    value is here so one place says what each framework needs."""
    assert for_framework("pytest").no_cache_args == ("-p", "no:cacheprovider")


def test_the_moved_names_are_still_importable_from_preflight_and_grader():
    """The refactor must be invisible to every existing caller and to every
    mutation anchor. A name that moved out from under `from bakeoff.preflight
    import ...` is a silent break in a module nothing in this task touches."""
    from bakeoff import grader, preflight

    assert preflight.EXIT_ALL_PASSED == 0
    assert preflight.EXIT_TESTS_FAILED == 1
    assert preflight.EXIT_NOTHING_COLLECTED == 5
    assert preflight.EXIT_COLLECTION_FAILURES == (4, 2)
    assert preflight.failed_node_ids("FAILED a::b") == {"a::b"}
    assert preflight.collection_error_modules("ERROR a.py") == frozenset({"a.py"})
    assert preflight.f2p_modules(("a.py::b",)) == frozenset({"a.py"})
    assert preflight._runner_python(("python", "-m", "pytest")) == "python"
    assert grader.parse_deselected("1 passed, 2 deselected in 0.0s") == 2
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_runners.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'bakeoff.runners'`.

- [ ] **Step 3: Create `bakeoff/src/bakeoff/runners/__init__.py`**

```python
"""What "the tests failed" means, per test runner. Spec sections 3.3 and 4.2.1.

Every red/green judgement this harness makes was, until this package existed, a
branch on pytest's exit codes -- and that is the right design for pytest and
only for pytest. The whole reason `preflight.py` can tell "the bug is present"
from "the environment is broken" is that pytest answers them with 1 and with
2/4/5, which is the distinction Phase 0c's `returncode != 0` did not make and
that invalidated every capability figure taken under it.

Measured 2026-09-01 in `node:22-bookworm-slim` against vitest 3.2.7 and jest
30.5.0: BOTH frameworks exit 1 for a failing test, for a test file with an
unresolvable import, for a test file with a syntax error, for a nonexistent
file passed as an argument, and for a broken config file. There is no exit code
to branch on. Worse, a `-t` name pattern that matches NOTHING exits **0** with
every test reported skipped -- so a manifest naming a renamed test reads as a
green gate rather than as the usage error pytest would raise.

So the judgement moves here. An adapter takes what a suite invocation produced
-- exit code, stdout, stderr, and (for the frameworks that write one) a parsed
JSON report -- and returns an `Outcome` in terms no framework owns. The
consumers keep their structure, their messages and their own comparisons
against the manifest; what they stop doing is knowing which numbers mean what.

The `Outcome` deliberately does NOT compare anything against the task. Preflight
needs EQUALITY between the errored files and the declared f2p modules; the
grader needs CONTAINMENT (see the offline-grader spec, check 5). Folding both
behind a flag would make each call site unreadable about which claim it is
making, which is the reason `collection_error_modules` never made that
comparison either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: The suite ran and everything selected passed.
KIND_PASSED = "passed"
#: The suite ran and something failed an assertion. The ONLY kind that is a
#: statement about the code under test.
KIND_FAILED = "failed"
#: A test FILE could not be loaded -- an import error, a syntax error, a
#: collection error. `errored_files` names them. Whether that is an accepted
#: task shape (broadening 2) or a broken image is the CALLER's comparison.
KIND_LOAD_ERROR = "load_error"
#: The invocation completed and executed no test. pytest's exit 5; on node,
#: zero assertions with a terminal status -- which is also what a `-t` pattern
#: matching nothing and a quarantine that swallowed the whole p2p list produce,
#: both of which exit 0 (measured).
KIND_NOTHING_RAN = "nothing_ran"
#: The suite did not run, or did not say what it did. Never a statement about
#: the model: this is the Phase 0c failure, and a bare `!= 0` reports it as one.
KIND_ENVIRONMENT = "environment"


@dataclass(frozen=True)
class Outcome:
    """One suite invocation, in terms no framework owns.

    Fields are three-valued where a missing answer and a negative answer are
    different facts, because two absences that render identically are the same
    defect one layer down.
    """

    kind: str
    exit_code: int
    #: Node ids that failed an assertion, in the framework's own id shape.
    failed_ids: frozenset[str] = frozenset()
    #: Files that could not be LOADED. `None` means the question was not asked
    #: -- this exit code is not a collection failure, or no report existed. An
    #: EMPTY frozenset means it WAS asked and nothing parsed, which is a
    #: refusal rather than an absence: preflight's message says "not parsed"
    #: for the first and "empty" for the second, and it can only do that while
    #: these stay apart.
    errored_files: frozenset[str] | None = None
    #: Rootdir-relative paths the runner LOADED or executed -- every
    #: `testResults` entry, including a file that failed to load and therefore
    #: ran nothing. "Executed" alone would be the wrong word and the wrong set: what D7c's scope
    #: check asks is whether the run touched a file outside `tests.paths`, and
    #: a file the filter pulled in and then failed to parse is exactly such a
    #: file. `None` when the framework does not report them (pytest). Node
    #: fills it because its positional arguments are SUBSTRING FILTERS over the
    #: absolute path rather than paths -- measured, `vitest run tests/` matched
    #: `/repo/jtests/...`.
    files_run: tuple[str, ...] | None = None
    #: Requested ids that produced no terminal status. Node only; pytest
    #: answers this with exit 4 and an `ERROR: not found:` line. Empty for
    #: pytest, and empty is honest there -- the exit code already carried it.
    not_run: frozenset[str] = frozenset()
    #: A human phrase for `kind`/`exit_code`, for a problem message. Never
    #: parsed.
    explain: str = ""


@runtime_checkable
class RunnerAdapter(Protocol):
    """The seam. One implementation per `tests.framework`.

    Everything here is a pure function of its arguments except that nothing
    here touches a container: the adapter builds argv and reads output, and the
    caller runs the command. That is what keeps `classify` testable against a
    captured report with no Docker daemon -- which matters, because the eight
    node report shapes this package branches on were captured once and can be
    replayed forever, while re-measuring them needs a network and 73 MB of
    npm.
    """

    #: The manifest's `tests.framework` value.
    name: str
    #: A substring that must appear somewhere in `tests.runner`. Preflight
    #: refuses a manifest whose declared framework and declared argv disagree
    #: -- each catches the other's typo, which is why neither is derived from
    #: the other.
    runner_marker: str
    #: Flags the runner needs so the suite writes nothing into the tree.
    #: Section 5.6 stages everything, so anything a test run drops lands in
    #: every submission diff and diff size measures the runner rather than the
    #: agent.
    no_cache_args: tuple[str, ...]

    def select_args(self, node_ids: tuple[str, ...]) -> list[str]:
        """Argv that runs exactly these ids and nothing else."""

    def p2p_args(self, *, selected: tuple[str, ...], scope: tuple[str, ...],
                 deselected: tuple[str, ...],
                 ignored: tuple[str, ...]) -> list[str]:
        """The WHOLE p2p argv, in one call, because it cannot be composed.

        vitest and jest express both selection and deselection through a
        single `-t <regex>`, and emitting two is NOT "last one wins" -- the two
        frameworks disagree and neither answer is usable. Measured 2026-09-02:
        vitest REJECTS it (`Expected a single value for option "-t,
        --testNamePattern <pattern>", received ["a", "b"]`, exit 1, and no
        report file, so it classifies as KIND_ENVIRONMENT), while jest
        COMMA-JOINS them into `adds,subs` -- a regex matching neither test --
        and exits **0** having run nothing. One method that owns the whole
        argv cannot be composed wrongly.

        It also matches by NAME only: `-t` knows nothing about which file a
        test came from, so two tests sharing a `fullName` across files are
        indistinguishable to a selection or a deselection. The loader's
        duplicate-name refusal and preflight's report-level assertion are what
        make that safe; see the plan's D2.

        `selected` is the manifest's explicit `tests.p2p`; when it is empty the
        run is the deselect branch and `scope` is the declared `tests.paths`.
        `deselected` is (f2p + quarantine) on the deselect branch and the
        quarantine alone on the explicit branch. `ignored` has exactly one
        caller -- preflight's p2p run at the START state on a task whose f2p
        module does not import there.
        """

    def report_args(self, report_path: str) -> list[str]:
        """Argv that makes the runner write a machine-readable report there."""

    def report_path(self) -> str | None:
        """Where that report goes, or `None` for a framework that writes none.

        FIXED, never per-invocation: it is an argv element, and the gated argv
        must be byte-identical to the graded one. Staleness is handled by
        deleting the file before each run rather than by varying the name --
        measured, a config error writes NO file, so a leftover report from the
        previous invocation would stand in as this run's evidence.
        """

    def classify(self, *, exit_code: int, stdout: str, stderr: str,
                 report: dict | None) -> Outcome:
        """What that invocation did. Never a comparison against the manifest."""

    def parse_deselected(self, *, stdout: str,
                         report: dict | None) -> int | None:
        """How many items the runner did not run. `None` = nobody counted."""

    def module_of(self, node_id: str) -> str:
        """The FILE half of a node id."""

    def validate_node_id(self, node_id: str, paths: tuple[str, ...],
                         where: str) -> None:
        """Raise `TaskError` if this id cannot be selected by this framework."""

    def hypothesis_interpreter(self, runner: tuple[str, ...]) -> str | None:
        """The interpreter to probe for hypothesis, or `None` for no probe.

        A Python-ecosystem determinism check (broadening 3). `None` from a
        non-Python adapter makes preflight skip it and leave both evidence keys
        `None` -- a recorded absence, never a claim that the suite is
        deterministic.
        """

    def explain(self, code: int) -> str:
        """A phrase naming what this exit code means to this framework."""


def for_framework(name: str) -> RunnerAdapter:
    """The adapter for a validated `tests.framework`.

    Raises `KeyError` on an unknown name rather than defaulting, and the
    manifest loader's closed allowlist is what keeps that unreachable: a
    default here would silently classify a jest run with pytest's exit codes.
    """
    return _REGISTRY[name]


def _build_registry() -> dict[str, RunnerAdapter]:
    # Imported inside the function so `bakeoff.runners` can be imported by the
    # adapters themselves for `Outcome` and the KIND_* names without a cycle.
    from bakeoff.runners.node_adapter import JEST, VITEST
    from bakeoff.runners.pytest_adapter import ADAPTER as PYTEST

    return {PYTEST.name: PYTEST, VITEST.name: VITEST, JEST.name: JEST}


#: In manifest-documentation order: the default first.
FRAMEWORKS: tuple[str, ...] = ("pytest", "vitest", "jest")

#: Built last, so a NameError here is a missing adapter rather than a module
#: that half-imported. `tasks._FRAMEWORKS` must equal `FRAMEWORKS`; a test
#: pins it, because `for_framework` raises KeyError rather than defaulting and
#: that allowlist is the only thing keeping the raise unreachable.
_REGISTRY = _build_registry()
```

**Note for the implementer:** `_build_registry` imports `node_adapter`, which Task 6 creates. For this task, create a **stub** `bakeoff/src/bakeoff/runners/node_adapter.py` containing only `VITEST` and `JEST` built from a `_NodeFlavour` whose methods `raise NotImplementedError("broadening 7 Task 6")`, and a `test_runners.py` test asserting exactly that. Task 6 replaces the stub. A stub is right here and a deferred registry entry is not: `FRAMEWORKS` and `tasks._FRAMEWORKS` must agree from the first commit, and a registry that grows later is a registry the allowlist can disagree with.

- [ ] **Step 4: Create `bakeoff/src/bakeoff/runners/pytest_adapter.py`**

Move — do not retype — these definitions out of `preflight.py`: `EXIT_ALL_PASSED`, `EXIT_TESTS_FAILED`, `EXIT_COLLECTION_INTERRUPTED`, `EXIT_USAGE_ERROR`, `EXIT_NOTHING_COLLECTED`, `EXIT_COLLECTION_FAILURES`, `_EXIT_MEANING`, `_FAILED_LINE`, `failed_node_ids`, `collection_error_modules`, `f2p_modules`, `_PYTHON_BASENAME`, `_runner_python`; and out of `grader.py`: `_SUMMARY_LINE`, `_DESELECTED`, `parse_deselected`. **Carry every docstring and every `#:` comment across verbatim** — they hold the measurements (`Measured 2026-09-01 against pytest 9.1.1 and 8.3.5`, the six-row collection table, the `-q` summary-line samples) and re-deriving them is not possible.

Then add the class:

```python
class PytestAdapter:
    """pytest, whose exit codes are the reason this seam is shaped as it is.

    Every method here is today's inline logic with a signature around it. The
    argv methods reproduce `_Runner.pass_to_pass`'s emission ORDER exactly,
    which is what makes `test_grading_p2p_with_no_extras_is_the_argv_preflight
    _validated` -- untouched -- the gate on this refactor.
    """

    name = "pytest"
    runner_marker = "pytest"
    #: Advisory, not asserted. Without it pytest writes `.pytest_cache/` into
    #: the tree, and preflight's existing dirty-tree check fires loudly on it
    #: -- so nothing more is needed here, and adding an assertion could refuse
    #: a manifest that loads today. The node adapters ARE asserted, because
    #: vitest's artifact is `node_modules/`, which every JavaScript repo's
    #: .gitignore already hides from that check.
    no_cache_args = ("-p", "no:cacheprovider")

    def select_args(self, node_ids):
        return list(node_ids)

    def p2p_args(self, *, selected, scope, deselected, ignored):
        out = list(selected) if selected else list(scope)
        for node_id in deselected:
            out += ["--deselect", node_id]
        out += [f"--ignore={path}" for path in ignored]
        return out

    def report_args(self, report_path):
        # pytest's evidence is its exit code and its `-q` summary lines. A
        # JUnit report would need `classname` mapped back to a node id, and
        # that mapping is ambiguous -- a dotted segment is a package or a class
        # and the XML does not say which -- so the parser becomes a second
        # thing that can be wrong about what happened, inside the check that
        # exists to be right about it.
        return []

    def report_path(self):
        return None

    def classify(self, *, exit_code, stdout, stderr, report):
        output = stdout + stderr
        if exit_code == EXIT_ALL_PASSED:
            return Outcome(kind=KIND_PASSED, exit_code=exit_code,
                           explain="all selected tests passed")
        if exit_code == EXIT_TESTS_FAILED:
            return Outcome(kind=KIND_FAILED, exit_code=exit_code,
                           failed_ids=frozenset(failed_node_ids(output)),
                           explain=self.explain(exit_code))
        if exit_code == EXIT_NOTHING_COLLECTED:
            return Outcome(kind=KIND_NOTHING_RAN, exit_code=exit_code,
                           explain=self.explain(exit_code))
        if exit_code in EXIT_COLLECTION_FAILURES:
            modules = collection_error_modules(output)
            # `None` from the parser means "no bare-module ERROR line" -- a
            # renamed node id (`ERROR: not found:`, a colon `_FAILED_LINE`
            # does not match) or a conftest that could not import (no summary
            # section at all). Both must keep stopping the matrix, so they
            # become an ENVIRONMENT outcome carrying an EMPTY errored_files:
            # the parse ran and reported nothing, which is not the same fact
            # as never having run.
            if modules:
                return Outcome(kind=KIND_LOAD_ERROR, exit_code=exit_code,
                               errored_files=modules,
                               explain=self.explain(exit_code))
            return Outcome(kind=KIND_ENVIRONMENT, exit_code=exit_code,
                           errored_files=frozenset(),
                           explain=self.explain(exit_code))
        return Outcome(kind=KIND_ENVIRONMENT, exit_code=exit_code,
                       errored_files=None, explain=self.explain(exit_code))

    def parse_deselected(self, *, stdout, report):
        return parse_deselected(stdout)

    def module_of(self, node_id):
        return node_id.split("::", 1)[0]

    def validate_node_id(self, node_id, paths, where):
        # Deliberately empty. pytest ids are validated today only for
        # non-emptiness, duplication and f2p/p2p overlap, and adding a shape
        # rule now could refuse a manifest that loads -- there is exactly one,
        # and backwards compatibility is a constraint of this broadening.
        return None

    def hypothesis_interpreter(self, runner):
        return _runner_python(tuple(runner))

    def explain(self, code):
        return _EXIT_MEANING.get(code, f"exit code {code}")


ADAPTER = PytestAdapter()
```

**One splitter, not two.** Rewrite `f2p_modules` as `frozenset(ADAPTER.module_of(node_id) for node_id in f2p)` so the split from the left, maxsplit 1 — which is what makes a parametrized id carrying `::` inside its brackets, and a class-scoped id carrying two, both name a real module — exists in exactly one place. `test_pytest_module_of_splits_once_from_the_left` pins the behaviour.

- [ ] **Step 5: Re-export the moved names**

At the top of `preflight.py`, after the existing imports:

```python
# Re-exported, not re-defined. These moved to `bakeoff.runners.pytest_adapter`
# when the exit-code judgement became per-framework (broadening 7), and they
# are imported back under their EXACT bare names because four modules, twelve
# tests and six `scripts/mutation_check.py` anchors reference them that way. A
# namespaced re-spelling (`adapter.f2p_modules(...)`) would rot every one of
# those anchors silently -- `mutation_check` fails a missing anchor with STALE
# ANCHOR, but only on a run somebody makes.
from bakeoff.runners.pytest_adapter import (  # noqa: F401
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_FAILURES,
    EXIT_COLLECTION_INTERRUPTED,
    EXIT_NOTHING_COLLECTED,
    EXIT_TESTS_FAILED,
    EXIT_USAGE_ERROR,
    _EXIT_MEANING,
    _FAILED_LINE,
    _PYTHON_BASENAME,
    _runner_python,
    collection_error_modules,
    f2p_modules,
    failed_node_ids,
)
```

and in `grader.py`, replacing the deleted definitions:

```python
from bakeoff.runners.pytest_adapter import (  # noqa: F401
    _DESELECTED,
    _SUMMARY_LINE,
    parse_deselected,
)
```

- [ ] **Step 6: Run the new tests, then the whole suite**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_runners.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Expected: the new file passes; the whole suite is at the baseline count plus the new tests, with **zero** pre-existing tests edited and zero failures.

- [ ] **Step 7: Run the mutation check, solo**

```bash
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Expected: PASS with no `STALE ANCHOR`. Nothing has moved out of `preflight()`, `_Runner` or `_check_f2p` yet — the anchors in this task's blast radius (P1 `elif red.exit_code != EXIT_TESTS_FAILED:`, P2 `confined = collected is not None and collected == f2p_modules(tests.f2p)`) depend only on the bare names being importable, which Step 5 guarantees. **If either reports STALE ANCHOR, the re-export is wrong — fix the re-export, do not repoint the anchor.**

- [ ] **Step 8: Commit**

```bash
git add bakeoff/src/bakeoff/runners/ bakeoff/src/bakeoff/preflight.py \
        bakeoff/src/bakeoff/grader.py bakeoff/tests/test_runners.py
git commit -m "refactor: put pytest's exit-code judgement behind a runner adapter

Every red/green decision the gate and the grader make is a branch on pytest's
exit codes, which is correct for pytest and portable to nothing. Measured
2026-09-01 in node:22-bookworm-slim, vitest 3.2.7 and jest 30.5.0 both exit 1
for a failing test, an unresolvable import, a syntax error, a nonexistent file
argument and a broken config alike -- so a second runner cannot be added while
the judgement lives in the consumers.

This moves the pytest constants and parsers into bakeoff.runners.pytest_adapter
and re-exports them under their exact bare names, so no caller, no test and no
mutation_check anchor changes. Behaviour is byte-identical, including the
p2p argv, which the untouched argv-identity test gates.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 2: preflight calls the adapter

`preflight` still only ever sees the pytest adapter (nothing can declare another framework until Task 4), so every assertion, every message and every argv must be unchanged. This is the highest-regression-risk task in the plan and it is deliberately alone.

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (`_Runner.__init__`, `_Runner.select`, `_Runner.pass_to_pass`, `preflight`'s runner gate, the hypothesis probe, the four red-before branches, the green-after branches, the scoped branch)
- Modify: `bakeoff/scripts/mutation_check.py` (repoint two anchors)
- Test: `bakeoff/tests/test_preflight.py` (new section; **no existing test edited**)

**Interfaces:**
- Consumes: `bakeoff.runners.for_framework`, `Outcome`, `KIND_*`, `PytestAdapter` from Task 1.
- Produces: `preflight._Runner(container, runner, timeout_s, adapter)` — the adapter is a **required positional-or-keyword** fourth parameter with **no default**. `oracle._derive` and `grader._check_f2p`/`_check_p2p` construct `_Runner` and Task 3 updates them; a default here would let one of those three keep the pytest adapter for a node task, silently.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_preflight.py`:

```python
# --- the runner adapter seam --------------------------------------------------


def test_the_runner_holds_the_adapter_and_will_not_guess_one():
    """No default. `oracle._derive` and the grader's two checks each build a
    `_Runner`, and a default here means one of those three keeps classifying a
    jest run with pytest's exit codes -- which is exit 1 for a config error,
    graded as a model failure, permanently, in an append-only store."""
    import inspect

    from bakeoff.preflight import _Runner

    adapter_param = inspect.signature(_Runner.__init__).parameters["adapter"]
    assert adapter_param.default is inspect.Parameter.empty


def test_the_runner_gate_names_the_declared_framework_and_the_marker():
    """The gate used to be `any("pytest" in part)`, which could only ever mean
    one framework. It now asserts the DECLARED framework and the argv agree --
    each catching the other's typo, which is why neither is derived from the
    other. A manifest declaring vitest whose runner invokes jest would
    otherwise be classified by the wrong adapter."""
    from bakeoff.runners import for_framework

    assert for_framework("pytest").runner_marker == "pytest"
    assert for_framework("vitest").runner_marker == "vitest"
    assert for_framework("jest").runner_marker == "jest"


def test_preflight_refuses_a_runner_that_does_not_match_the_framework():
    """The early return, before any container is started -- the same shape the
    old `pytest`-substring gate had, so a bad manifest costs no daemon."""
    from bakeoff.preflight import preflight

    task = _FakeTask(runner=("python", "-m", "unittest"))

    result = preflight(task, image="img", repo_path=Path("/nonexistent"),
                       start_sha="0" * 40)

    assert not result.ok
    assert "pytest" in result.problems[0]
    assert "unittest" in result.problems[0]
    # No container was started, so the container-only evidence keys stay None.
    assert result.evidence["image_env_observed"] is None


def test_the_hypothesis_probe_is_asked_of_the_adapter():
    """It is a Python-ecosystem check. A node adapter answers `None`, preflight
    skips the block, and both evidence keys stay `None` -- a recorded absence,
    never a claim that a JS suite is deterministic."""
    from bakeoff.runners import for_framework

    assert for_framework("pytest").hypothesis_interpreter(
        ("python", "-m", "pytest")) == "python"
    assert for_framework("vitest").hypothesis_interpreter(
        ("/node_modules/.bin/vitest", "run")) is None


def test_preflight_evidence_names_the_framework_it_judged_under():
    """A cached verdict outlives the code that wrote it, and every other
    evidence key now means something framework-dependent -- `f2p_before_exit`
    most of all, since 1 means "a test failed" under pytest and means nothing
    at all under vitest."""
    from bakeoff.preflight import preflight

    task = _FakeTask(runner=("python", "-m", "unittest"))
    result = preflight(task, image="img", repo_path=Path("/nonexistent"),
                       start_sha="0" * 40)

    assert result.evidence["framework"] == "pytest"
```

`_FakeTask` is whatever this file's existing fake-manifest helper is called; reuse it rather than adding a second one, and give it a `tests.framework` attribute defaulting to `"pytest"`.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "adapter or runner_gate or framework or hypothesis_probe"
```

Expected: FAIL — `KeyError: 'adapter'` on the signature test, `KeyError: 'framework'` on the evidence test.

- [ ] **Step 3: Give `_Runner` the adapter**

```python
    def __init__(self, container: RunContainer, runner: tuple[str, ...],
                 timeout_s: int, adapter):
        self.container = container
        self.runner = list(runner)
        self.timeout_s = timeout_s
        self.adapter = adapter
        self.last_argv: list[str] = []
        #: What the last invocation asked for BY ID, so `classify` can report
        #: which of them never ran. Only the caller knows this; the report
        #: cannot tell a test that was skipped from one that was never
        #: selected. `()` when the last run selected nothing by id.
        self._selected: tuple[str, ...] = ()
        #: The report the last invocation wrote, parsed, or `None`. Read back
        #: through `cat` because the file lives in the container's /tmp, which
        #: is never bind-mounted -- deliberately, so it cannot reach a
        #: submission diff.
        self.last_report: dict | None = None

    def run(self, extra: list[str]):
        report_path = self.adapter.report_path()
        if report_path:
            # Deleted BEFORE the measured command, as a separate exec, because
            # a config error writes no report at all (measured 2026-09-01,
            # vitest 3.2.7 and jest 30.5.0 both) -- so a leftover file from the
            # previous invocation would stand in as this run's evidence. The
            # path is FIXED rather than per-invocation because it is an argv
            # element and the gated argv must equal the graded argv; the rm is
            # what makes a fixed name safe.
            self.container.exec(["rm", "-f", report_path])
            extra = [*extra, *self.adapter.report_args(report_path)]
        self.last_argv = ["timeout", str(self.timeout_s), *self.runner, *extra]
        result = self.container.exec(self.last_argv)
        self.last_report = self._read_report(report_path)
        return result

    def _read_report(self, report_path: str | None) -> dict | None:
        """The parsed report, or `None` -- which is a measurement, not a gap.

        `None` means the runner produced no machine-readable evidence, and
        `classify` turns that into an ENVIRONMENT outcome. That is the correct
        reading of a config error, of a runner that could not start, and of a
        report this code could not parse: in all three the command did not say
        what it did, and reading silence as success is the Phase 0c failure.
        """
        if not report_path:
            return None
        read = self.container.exec(["cat", report_path])
        if read.exit_code != 0:
            return None
        try:
            return json.loads(read.stdout)
        except (ValueError, TypeError):
            return None

    def classify(self, result) -> "Outcome":
        return self.adapter.classify(
            exit_code=result.exit_code, stdout=result.stdout,
            stderr=result.stderr, report=self.last_report,
        )
```

(Add `import json` at the top of `preflight.py`.)

- [ ] **Step 4: Route `select` and `pass_to_pass` through the adapter**

```python
    def select(self, node_ids: tuple[str, ...]):
        return self.run(self.adapter.select_args(node_ids))

    def pass_to_pass(self, tests, extra_deselect: tuple[str, ...] = (),
                     scope: tuple[str, ...] = (),
                     ignore: tuple[str, ...] = ()):
        """<keep the entire existing docstring verbatim, then append:>

        The argv itself is now the adapter's, in ONE call rather than
        composed from pieces: vitest and jest express selection and
        deselection through a single `-t <regex>`, and emitting two is not
        "last one wins" -- measured 2026-09-02, vitest REJECTS the second
        (exit 1, no report) and jest COMMA-JOINS them into a pattern matching
        neither, running nothing at exit 0. The BRANCH -- explicit `tests.p2p`
        or the deselect default -- stays here, because it is a statement about
        the manifest rather than about the framework.
        """
        if tests.p2p:
            return self.run(self.adapter.p2p_args(
                selected=tuple(tests.p2p), scope=(),
                deselected=tuple(extra_deselect), ignored=tuple(ignore)))
        return self.run(self.adapter.p2p_args(
            selected=(), scope=tuple(scope),
            deselected=tuple(tests.f2p) + tuple(extra_deselect),
            ignored=tuple(ignore)))
```

- [ ] **Step 5: Route `preflight` itself**

Five edits, each a one-for-one substitution:

1. Near the top, after `tests = task.tests`:
   ```python
   adapter = for_framework(getattr(tests, "framework", "pytest"))
   evidence["framework"] = adapter.name
   ```
   `getattr` with a default, like `_declared_grading`'s and the strip's: this function takes an untyped `task` and a manifest object predating the key must not crash the gate. Task 4 adds the field.
2. The runner gate:
   ```python
   if not any(adapter.runner_marker in part for part in tests.runner):
       problems.append(
           f"tests.runner is {list(tests.runner)!r} but tests.framework is "
           f"{adapter.name!r}, and no argument contains "
           f"{adapter.runner_marker!r}. The red/green distinction is built on "
           "what THIS runner reports, so a runner the declared adapter cannot "
           "read would be classified by the wrong rules -- which for the node "
           "frameworks means exit 1 for a config error graded as a test "
           "failure."
       )
   ```
   For a pytest task with a pytest runner this is the same test it is today.
3. The hypothesis probe: replace `_runner_python(tests.runner)` with
   ```python
   interpreter = adapter.hypothesis_interpreter(tests.runner)
   ```
   and wrap the whole hypothesis block (both probes and the `CI` problem) in `if interpreter is not None:`. The three evidence keys `hypothesis_importable` and `hypothesis_imported_by_suite` are already written as `None` before the guard, so an adapter that declines the probe leaves them as recorded absences with no further code.
4. The red-before block: keep every branch, every message and the four-value `f2p_red_kind`, and derive them from the outcome instead of from the number:
   ```python
   red = runner.select(tests.f2p)
   evidence["f2p_before_exit"] = red.exit_code
   red_outcome = runner.classify(red)

   collected = red_outcome.errored_files
   confined = collected is not None and collected == f2p_modules(tests.f2p)
   ```
   `confined` is **character-identical to today's line**, which is why mutation anchor P2 does not move.

   `collected` is now `frozenset()` rather than `None` on the parsed-but-empty path. That changes nothing here, and the reason is worth writing down beside the unchanged anchor: `tests.f2p` is **required non-empty** by the loader, so `f2p_modules(tests.f2p)` is never empty, so `frozenset() == f2p_modules(...)` is false exactly as `None == f2p_modules(...)` was. The existing `sorted(collected or ())` and `if collected` sites treat the two the same. The one place the difference IS visible — the `reported_desc` three-way — is repaired in the next bullet.

6. `f2p_red_kind` keeps its four values and its argument (*"the task is already done" and "the gate could not classify this run" are different facts*), stated over the outcome:
   ```python
   evidence["f2p_red_kind"] = (
       "collection_error" if confined
       else "failed" if red_outcome.kind == KIND_FAILED
       else "passed" if red_outcome.kind == KIND_PASSED
       else "unknown"
   )
   ```
7. The `reported_desc` three-way keeps its three arms with the source of the middle one changed:
   ```python
   reported_desc = (
       sorted(collected) if collected
       else "empty" if collected is not None
       else f"not parsed (exit {red.exit_code} is not a collection failure)"
   )
   ```
   The predicate moves from `red.exit_code in EXIT_COLLECTION_FAILURES` to `collected is not None`, which is the same claim stated over the parse rather than over the code — and it is the claim that survives a framework with no exit codes to consult.
8. Every remaining `X.exit_code != EXIT_ALL_PASSED` (the p2p-before, f2p-after, p2p-after and scoped runs) becomes `runner.classify(X).kind != KIND_PASSED`, keeping `_explain(X.exit_code)` in the message. `_explain` becomes `adapter.explain`.

7. `elif red.exit_code != EXIT_TESTS_FAILED:` becomes `elif red_outcome.kind != KIND_FAILED:`, and its mutation anchor is repointed in Step 6. It would still be *correct* today as written — `EXIT_TESTS_FAILED` is a re-exported bare name and the branch is only reached when the outcome is neither passed nor confined — but a comparison against a pytest constant inside a branch that will shortly run for three frameworks is exactly the line that is right today and silently wrong on arrival.

- [ ] **Step 6: Repoint the mutation anchors**

Three anchors change. `MUTATIONS` in `scripts/mutation_check.py` is a list of 6-element tuples `(label, file, find, replace, selector, marker)`; `find` is matched as an **exact substring including leading indentation** and applied with `.replace(find, replace, 1)`, and a missing anchor prints `STALE ANCHOR` and fails the run.

| anchor | today | after |
|---|---|---|
| `preflight: accept any non-zero exit as evidence the bug is present` | file `src/bakeoff/preflight.py`, find `        elif red.exit_code != EXIT_TESTS_FAILED:`, replace `        elif False:` | find `        elif red_outcome.kind != KIND_FAILED:`, replace `        elif False:`. Same file, same test selector (`tests/test_preflight.py -k cannot_even_run`, marker `integration`). |
| `preflight: grade with the flake in the suite` | file `src/bakeoff/preflight.py`, find `        extra = [arg for node_id in extra_deselect\n                 for arg in ("--deselect", node_id)]`, replace `        extra = []` | file **`src/bakeoff/runners/pytest_adapter.py`**, find `        for node_id in deselected:\n            out += ["--deselect", node_id]`, replace `        for node_id in ():\n            out += ["--deselect", node_id]`. Same selector (`tests/test_preflight.py -k quarantine_rides_as_deselect`). |
| `preflight: collect the agent's scratch files into p2p` | file `src/bakeoff/preflight.py`, find `        args: list[str] = [*scope]`, replace `        args: list[str] = []` | file **`src/bakeoff/runners/pytest_adapter.py`**, find `        out = list(selected) if selected else list(scope)`, replace `        out = list(selected) if selected else []`. Same selector (`tests/test_preflight.py -k scope_prefixes_lead`). |

Anchor `preflight: accept any collection error, not one confined to the f2p modules` (find `        confined = collected is not None and collected == f2p_modules(tests.f2p)`) is **unchanged** — Step 5 keeps that line character-identical on purpose.

- [ ] **Step 7: Run the whole suite and the mutation check**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Expected: all three green, no `STALE ANCHOR`, and **`test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` passing unmodified** — it is the whole gate on this task.

- [ ] **Step 8: Prove the click task still gates identically**

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only
```

Expected: PASS, and the emitted `~/.cache/bakeoff/preflight/click-3360-write-usage-empty-args.json` carries the same `f2p_before_exit`, `p2p_before_exit`, `f2p_after_exit`, `p2p_after_exit` and `p2p_scoped_after_exit` values as a run made before this task. Capture both files and `diff` them; only `framework`, `preflight_version` and `stripped_paths`-adjacent keys added by earlier broadenings may differ.

- [ ] **Step 9: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/scripts/mutation_check.py \
        bakeoff/tests/test_preflight.py
git commit -m "refactor: preflight asks the adapter what a run did, not the exit code

Every branch in the gate read a pytest exit code directly, so the gate could
only ever gate pytest. The branches, the messages and the four-value
f2p_red_kind are unchanged; what changed is where the number is interpreted.

_Runner takes the adapter with NO default, because oracle._derive and the
grader's two checks each build one and a default would let one of the three go
on classifying a jest run with pytest's codes -- exit 1 for a config error,
stamped on the model, permanently.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 3: the oracle and the grader call the adapter

**Files:**
- Modify: `bakeoff/src/bakeoff/oracle.py` (`_classify`, `_ORACLE_EXIT_MEANING`, `_derive`, `derive_quarantine`'s guard docstring)
- Modify: `bakeoff/src/bakeoff/grader.py` (`_check_f2p`, `_check_p2p`, both `_Runner(...)` constructions)
- Modify: `bakeoff/scripts/mutation_check.py` (rewrite anchor G4)
- Test: `bakeoff/tests/test_oracle.py`, `bakeoff/tests/test_grader.py` (new sections; **no existing test edited**)

**Interfaces:**
- Consumes: `_Runner(container, runner, timeout_s, adapter)` from Task 2; `for_framework`, `Outcome`, `KIND_*` from Task 1.
- Produces: nothing new; behaviour is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_grader.py`:

```python
# --- the runner adapter seam --------------------------------------------------


def test_the_graders_runner_is_built_with_the_tasks_adapter():
    """Both `_Runner` constructions in the grader take the adapter for the
    task's declared framework. A pytest adapter on a jest task reads exit 1 --
    which jest returns for a broken config -- as F2P_FAILED, and that is an
    accusation against the model for the task author's error, permanently, in
    an append-only store."""
    import inspect

    from bakeoff import grader

    source = inspect.getsource(grader._check_f2p) + inspect.getsource(
        grader._check_p2p)
    assert source.count("_Runner(") == 2
    assert "for_framework(" in source or "adapter=" in source


def test_a_run_that_did_not_say_what_it_did_is_never_stamped_on_the_model():
    """KIND_ENVIRONMENT reaches `state.environment`, never `state.fail`. This
    is the Phase 0c failure stated over the new seam: a broken environment is
    also a non-zero exit, and on vitest and jest it is the SAME non-zero exit
    as a test failure."""
    state = _run_f2p_with(exit_code=3, stdout="INTERNALERROR", report=None)

    assert state.grade_failure is None
    assert state.not_graded_reason == "environment_error"
    assert state.environment_error_check == "f2p"


def test_a_confined_load_error_is_still_an_f2p_failure():
    """Broadening 2's rule, restated over the adapter. An arm that changed
    nothing leaves the f2p module not importing; reading that as an environment
    error made the do-nothing arm NOT GRADED while the arm that half-fixed it
    graded False -- so in any view counting False the arm that did nothing
    looked better than the one that tried."""
    state = _run_f2p_with(exit_code=4, stdout="ERROR tests/new.py", report=None)

    assert state.grade_failure == "f2p_failed"
    assert state.f2p_failed_node_ids == ("tests/new.py",)
```

`_run_f2p_with` is a small helper over this file's existing fake `env`; write it beside the existing fakes rather than inlining it three times.

Append to `bakeoff/tests/test_oracle.py`:

```python
def test_the_oracle_refuses_every_kind_that_is_not_passed_or_failed():
    """A quarantine is derived from which tests failed, and a run that did not
    happen reports none -- indistinguishable from a clean run, and it would
    quarantine nothing. `load_error`, `nothing_ran` and `environment` all reach
    the raise, and `nothing_ran` is the one that matters most on node: a
    quarantine that swallows the whole p2p list exits 0 there (measured), so
    the exit code cannot carry this any more."""
    from bakeoff.oracle import OracleError, _classify
    from bakeoff.runners import (
        KIND_ENVIRONMENT, KIND_LOAD_ERROR, KIND_NOTHING_RAN, Outcome,
    )

    result = SimpleNamespace(stdout="the tail an operator needs", stderr="")
    for kind, code in (
        (KIND_LOAD_ERROR, 4), (KIND_NOTHING_RAN, 5), (KIND_ENVIRONMENT, 127),
    ):
        with pytest.raises(OracleError) as excinfo:
            _classify(Outcome(kind=kind, exit_code=code, explain="x"), result)
        # The tail is not decoration: every path through here is a broken
        # oracle, and a refusal with no output is one an operator cannot act on.
        assert "the tail an operator needs" in str(excinfo.value)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py tests/test_oracle.py -q -k "adapter or did_not_say or confined_load_error or refuses_every_kind"
```

Expected: FAIL — `_classify` takes an `ExecResult`, not an `Outcome`; `_Runner(...)` is called with three arguments.

- [ ] **Step 3: Rewrite `oracle._classify` over the outcome**

```python
def _classify(outcome, result) -> set[str]:
    """What failed in one p2p run, or a refusal that the run did not happen.

    <keep the existing docstring's second paragraph verbatim -- the polarity
    argument is unchanged and is the reason this function exists -- then
    append:>

    Stated over `Outcome.kind` rather than over an exit code, because on vitest
    and jest there is no exit code to state it over: measured 2026-09-01, both
    return 1 for a failing test and for a broken config, and BOTH return 0 for
    a `-t` pattern that matched nothing -- which is exactly what a quarantine
    covering the whole p2p list produces. `KIND_NOTHING_RAN` is what carries
    that now; the exit code used to.
    """
    if outcome.kind == KIND_PASSED:
        return set()
    if outcome.kind == KIND_FAILED:
        return set(outcome.failed_ids)
    meaning = _ORACLE_EXIT_MEANING.get(outcome.exit_code) or outcome.explain
    detail = f" -- {meaning}" if meaning else ""
    # `result` is carried alongside the outcome solely for this tail, which the
    # exit-code version already had. Every path through here is a BROKEN
    # ORACLE, which is the failure an operator has to debug from the message
    # alone -- so dropping the output would be the one regression this refactor
    # could make that no test would see.
    raise OracleError(
        f"the p2p run at the reference state exited {outcome.exit_code} "
        f"({outcome.kind}){detail}. The quarantine is derived from which tests "
        "failed, and a run that did not happen reports none -- so this would "
        "be indistinguishable from a clean run and would quarantine nothing.\n"
        + (result.stdout or result.stderr)[-2000:]
    )
```

`derive_quarantine` becomes, at both call sites, `result = runner.pass_to_pass(tests, scope=scope)` followed by `_classify(runner.classify(result), result)` — the raw result threaded through for the output tail, keeping the eager ordering (classify each result before starting the next run) that its docstring already argues for. `_derive` builds `_Runner(container, task.tests.runner, timeout_s, for_framework(task.tests.framework))`.

Add to `derive_quarantine`'s "swallows the entire declared p2p list" guard docstring:

```
        On pytest this surfaces because deselecting every selected id makes
        pytest exit 5. On vitest and jest it exits **0** with every test
        reported skipped (measured 2026-09-01) -- so what carries it there is
        `classify`'s rule that zero assertions with a terminal status is
        `KIND_NOTHING_RAN` whatever the exit code was. The guard's claim is
        unchanged; the mechanism behind it is no longer the exit code.
```

- [ ] **Step 4: Rewrite `_check_f2p` and `_check_p2p` over the outcome**

Both keep their structure, their `CHECK_ORDER` position, their messages and the order of their terminal branches. The substitutions:

- `runner = _Runner(env, task.tests.runner, task.budget.suite_timeout_s, adapter)` where `adapter = for_framework(task.tests.framework)`.
- `code = result.exit_code` stays (the timeout and infra branches read it), and `outcome = runner.classify(result)` is added beside it.
- `if code == EXIT_ALL_PASSED:` → `if outcome.kind == KIND_PASSED:`
- `if code == EXIT_TESTS_FAILED:` → `if outcome.kind == KIND_FAILED:`, with `state.f2p_failed_node_ids = tuple(sorted(outcome.failed_ids))`.
- `if code == _TIMEOUT_EXIT:` **unchanged and still read off the exit code** — 124 is coreutils, not a framework fact.
- the confined-collection branch becomes:
  ```python
  if outcome.kind == KIND_LOAD_ERROR:
      modules = outcome.errored_files
      if modules is not None and modules <= f2p_modules(tuple(task.tests.f2p)):
          state.f2p_failed_node_ids = tuple(sorted(modules))
          state.fail("f2p", GradeFailure.F2P_FAILED, result,
                     detail="did not import: "
                            + ", ".join(state.f2p_failed_node_ids))
  ```
  The two statements inside are today's, unchanged; only the guard moved from
  `code in EXIT_COLLECTION_FAILURES` plus a `collection_error_modules(...)` call
  to the outcome. Keep the whole existing comment block above it verbatim — it
  is the argument for containment here and equality in preflight, and for why
  preflight's green-after conjunct is what licenses this branch at all.
- `if code == EXIT_NOTHING_COLLECTED:` in `_check_p2p` → `if outcome.kind == KIND_NOTHING_RAN:`, keeping its "NOT the environment path" comment and adding one sentence: *"and on the node frameworks this is also where a quarantine that deselected everything lands, which exits 0 there rather than 5."*
- `state.p2p_deselected = parse_deselected(result.stdout)` → `state.p2p_deselected = adapter.parse_deselected(stdout=result.stdout, report=runner.last_report)`.

- [ ] **Step 5: Rewrite mutation anchor G4**

Today: file `src/bakeoff/grader.py`, find `'    state.environment(\n        "f2p",'`, replace with a `state.fail("f2p", GradeFailure.F2P_FAILED, result, detail=_head(result))` prepended before it, selector `tests/test_grader.py -k f2p_environment_exit`, marker `not integration`.

The fall-through still exists and still lives in `_check_f2p`, and the `find` string is still distinguished from the earlier `state.environment(\n            name,` by the 8-space indent of `"f2p",` — so the anchor **survives unedited** provided Step 4 does not change the indentation of that call. Verify it explicitly by running `mutation_check.py` and reading the line; if the reindent was unavoidable, repoint `find` and `replace` together, keeping the mutation's meaning (*make the terminal fall-through accuse the model instead of naming the environment*) and its selector unchanged.

- [ ] **Step 6: Run everything**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Expected: all green, no `STALE ANCHOR`.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/oracle.py bakeoff/src/bakeoff/grader.py \
        bakeoff/scripts/mutation_check.py bakeoff/tests/test_oracle.py \
        bakeoff/tests/test_grader.py
git commit -m "refactor: the oracle and the ladder read an Outcome, not an exit code

_classify and checks 5 and 6 were the last places a pytest number decided what
a run had done. The timeout (124) and infra (125/126/127/137) branches stay on
the exit code deliberately -- those are coreutils and container facts, and
folding them into a runner adapter would put a docker OOM behind a framework's
opinion.

derive_quarantine's 'the quarantine swallowed the whole p2p list' guard is
documented anew: on pytest that surfaces as exit 5, on vitest and jest it exits
0 with everything skipped, and KIND_NOTHING_RAN is what carries it now.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 4: the manifest keys — `tests.framework`, `image.node`, and per-framework id validation

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (`TaskTests`, `TaskImage`, the `_parse_tests` block, the `TaskImage(...)` construction, new `_FRAMEWORKS`/`_framework`/`_NODE_VERSIONS`/`_node_version`/`_validate_node_ids`/`task_runtime`)
- Test: `bakeoff/tests/test_tasks.py` (new section)

**Interfaces:**
- Produces: `tasks._FRAMEWORKS: frozenset[str]`; `TaskTests.framework: str = "pytest"`; `tasks._NODE_VERSIONS: frozenset[str]`; `TaskImage.node: str = "22"`; `tasks.task_runtime(task) -> tuple[str, str]` returning `("python", "3.12")` or `("node", "22")`.
- Consumes: `bakeoff.runners.for_framework` (only to assert the allowlist and the registry agree).

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_tasks.py`:

```python
# --- tests.framework ----------------------------------------------------------


def test_framework_defaults_to_pytest_so_every_existing_manifest_loads(
    tmp_path, upstream
):
    task_dir = _write_task(tmp_path / "set", upstream)  # no framework: key

    assert load_task(task_dir).tests.framework == "pytest"


def test_the_allowlist_and_the_adapter_registry_cannot_disagree():
    """`for_framework` raises KeyError on an unknown name rather than
    defaulting, and this allowlist is the only thing that keeps that
    unreachable. A default in either place would classify a jest run with
    pytest's exit codes -- exit 1 for a config error, graded as the model's
    failure."""
    from bakeoff.runners import FRAMEWORKS
    from bakeoff.tasks import _FRAMEWORKS

    assert set(_FRAMEWORKS) == set(FRAMEWORKS)


def test_an_unknown_framework_is_refused_with_the_allowlist_in_the_message(
    tmp_path, upstream
):
    task_dir = _write_task(
        tmp_path / "set", upstream, tests_extra="  framework: mocha\n"
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "mocha" in str(excinfo.value)
    assert "pytest" in str(excinfo.value)


def test_a_node_framework_needs_a_node_shaped_runner(tmp_path, upstream):
    """Not preflight's check -- this one costs no daemon. The two are not
    redundant: this refuses a manifest, preflight refuses an IMAGE whose
    runner is on PATH but wrong."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        tests_extra='  framework: vitest\n  runner: ["python", "-m", "pytest"]\n',
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "vitest" in str(excinfo.value)


# --- node id shapes -----------------------------------------------------------


def test_a_node_f2p_id_must_carry_a_file_and_a_full_test_name(
    tmp_path, upstream
):
    """`<file>::<fullName>`. The right half is what the reporter emits --
    ancestorTitles joined by single spaces plus the title -- and what `-t`
    matches, so no translation layer can be wrong about it."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream, f2p=["tests/a.test.js"]
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "::" in str(excinfo.value)


def test_a_node_f2p_id_with_an_empty_half_is_refused(tmp_path, upstream):
    for bad in ("::a name", "tests/a.test.js::"):
        task_dir = _write_node_task(tmp_path / bad.replace("/", "_"), upstream,
                                    f2p=[bad])
        with pytest.raises(TaskError):
            load_task(task_dir)


def test_a_node_f2p_id_outside_tests_paths_is_refused(tmp_path, upstream):
    """The scoped p2p run selects by path prefix, so an id whose file is
    outside the declared scope can never be deselected from it -- and a
    deselection that matches nothing is SILENT on node: measured, `-t` matching
    nothing exits 0 with every test reported skipped."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream, paths=["tests/"],
        f2p=["other/a.test.js::does a thing"],
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "tests/" in str(excinfo.value)


def test_two_declared_ids_sharing_a_full_name_across_files_are_refused(
    tmp_path, upstream
):
    """`-t` matches `fullName` and knows nothing about which file a test came
    from, and there is no flag that pairs them -- the file positionals and the
    name pattern are ANDed across the whole run. So a quarantine of
    `a.test.js::works` also deselects `b.test.js::works`, silently, with
    `p2p_deselected` agreeing because two tests really were skipped.

    This is the half the author created and it costs no daemon. The half that
    matters more -- a collision between a declared id and a test the manifest
    never mentions -- is preflight's, against the real report."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream,
        f2p=["tests/a.test.js::works"], p2p=["tests/b.test.js::works"],
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "works" in str(excinfo.value)
    assert "tests/a.test.js" in str(excinfo.value)


def test_the_same_full_name_in_the_same_file_is_not_refused(tmp_path, upstream):
    """Two entries with the same left AND right half are a duplicate, which
    `tests.f2p contains duplicates` already refuses. The new rule is about
    DIFFERENT files, and a rule that also fired on the same file would be a
    second, worse message for a case that already has one."""
    task_dir = _write_node_task(
        tmp_path / "set", upstream,
        f2p=["tests/a.test.js::works"], p2p=["tests/a.test.js::other"],
    )

    assert load_task(task_dir).tests.framework == "vitest"


def test_a_pytest_manifest_may_share_node_names_across_modules(
    tmp_path, upstream
):
    """pytest selects by the WHOLE node id, path included, so `a.py::test_x`
    and `b.py::test_x` are unambiguous there. Applying the node rule to pytest
    would refuse a manifest that loads today, over a hazard pytest does not
    have."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        tests_extra='  f2p: ["tests/a.py::test_x"]\n  p2p: ["tests/b.py::test_x"]\n',
    )

    assert load_task(task_dir).tests.framework == "pytest"


def test_a_wellformed_node_manifest_loads(tmp_path, upstream):
    task = load_task(_write_node_task(tmp_path / "set", upstream))

    assert task.tests.framework == "vitest"
    assert task.image.node == "22"


def test_a_pytest_id_shape_is_not_newly_constrained(tmp_path, upstream):
    """Backwards compatibility as a rule, not as a hope: adding a shape rule to
    the pytest branch could refuse a manifest that loads today, and there is
    exactly one."""
    task = load_task(_write_task(tmp_path / "set", upstream))

    assert task.tests.framework == "pytest"


# --- the runtime is derived, never declared twice -----------------------------


def test_image_node_on_a_pytest_task_is_a_load_error(tmp_path, upstream):
    """Two keys that can each imply a runtime is two sources for one fact, and
    the failure of a disagreement between them is a task gated in one
    interpreter and run in another."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  node: "22"\n'
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "framework" in str(excinfo.value)


def test_image_python_on_a_node_task_is_a_load_error(tmp_path, upstream):
    task_dir = _write_node_task(
        tmp_path / "set", upstream, extra_yaml='  python: "3.12"\n'
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


def test_task_runtime_names_the_base_a_task_needs(tmp_path, upstream):
    from bakeoff.tasks import task_runtime

    assert task_runtime(load_task(_write_task(tmp_path / "p", upstream))) == (
        "python", "3.12")
    assert task_runtime(load_task(_write_node_task(tmp_path / "n", upstream))) == (
        "node", "22")


def test_a_node_version_nobody_built_is_refused(tmp_path, upstream):
    task_dir = _write_node_task(tmp_path / "set", upstream,
                                extra_yaml='  node: "18"\n')

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "22" in str(excinfo.value)


def test_an_unquoted_node_version_is_refused_rather_than_coerced(
    tmp_path, upstream
):
    """YAML parses a bare 22 as an int. `str(22)` happens to be right; the
    refusal is here because the NEXT version is `22.1` and a bare 22.10 parses
    as the float 22.1, which is the silent-wrong-value shape one key over."""
    task_dir = _write_node_task(tmp_path / "set", upstream,
                                extra_yaml="  node: 22\n")

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "quote" in str(excinfo.value).lower()


def test_the_click_task_still_loads_and_its_start_sha_has_not_moved(tmp_path):
    """None of these keys takes part in the setup commit, so `start_sha`
    cannot move. Asserted anyway, because "cannot move" is the claim rather
    than the evidence."""
    task = load_task(
        Path(__file__).resolve().parent.parent
        / "taskset" / "click-3360-write-usage-empty-args"
    )

    assert task.tests.framework == "pytest"
    assert task.declared_start_sha == (
        "33575cc0b75608fa5cbcb1d3ae3347b81eac437f")
```

Add a `_write_node_task(root, upstream, *, paths=("tests/",), f2p=None, extra_yaml="")` helper beside the existing `_write_task`, emitting `framework: vitest`, a `/node_modules/.bin/vitest run --no-cache` runner and a default f2p of `["tests/a.test.js::does a thing"]`.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "framework or node_id or node_version or task_runtime or image_node"
```

Expected: FAIL — `AttributeError: 'TaskTests' object has no attribute 'framework'`.

- [ ] **Step 3: Add the constants and validators to `tasks.py`**

Beside `_IMAGE_ENV_ALLOWED`:

```python
#: The test frameworks this harness can classify. Closed, and it must equal
#: `bakeoff.runners.FRAMEWORKS` -- a test pins that. `for_framework` raises
#: KeyError rather than defaulting, and this allowlist is the only thing that
#: keeps that unreachable; a default in either place classifies a jest run with
#: pytest's exit codes, which means exit 1 for a BROKEN CONFIG read as a test
#: failure and stamped on the model, permanently, in an append-only store.
#:
#: Measured 2026-09-01 in node:22-bookworm-slim, vitest 3.2.7 and jest 30.5.0:
#: both exit 1 for a failing test, an unresolvable import, a syntax error, a
#: nonexistent file argument AND a broken config, and BOTH exit 0 when a `-t`
#: pattern matches nothing. That is why each entry needs an adapter that reads
#: a JSON report rather than a row in a table of exit codes.
_FRAMEWORKS = frozenset({"pytest", "vitest", "jest"})

#: The frameworks that need the node base image. Derived from the framework
#: rather than declared, so there is one source for the runtime.
_NODE_FRAMEWORKS = frozenset({"vitest", "jest"})

#: Node majors the node base Dockerfile is KNOWN to build, because someone
#: built it. Same closed-set argument as `_PYTHON_VERSIONS`: `node:22-...` is
#: republished, so an unpinned entry means two collections months apart run
#: different runtimes under one manifest with nothing in the record saying so;
#: and an unbuildable string fails at the FROM with a registry error, over the
#: network, mid-build.
#:
#: Verified 2026-09-01: node v22.23.2, npm 10.9.8, vitest 3.2.7, jest 30.5.0,
#: Claude Code 2.1.220, uid 1000 (after `userdel -r node` -- the base image
#: ships a `node` user at that uid and `useradd --uid 1000` exits 4 without it).
#:
#: TO ADD A VERSION, all three steps: build docker/eval-agent-node.Dockerfile
#: with `--build-arg BASE_NODE_VERSION=<v>` and confirm the claude and runner
#: pin assertions fire; add the string here with the date; add the row to
#: taskset/HARVESTING.md. `20` and `24` exist as tags and are deliberately
#: absent -- an unmeasured entry is this constant claiming what it does not
#: know.
_NODE_VERSIONS = frozenset({"22"})
_DEFAULT_NODE = "22"


def _framework(value: Any, where: str) -> str:
    if value is None:
        return "pytest"
    if not isinstance(value, str) or value not in _FRAMEWORKS:
        raise TaskError(
            f"{where}: {value!r} is not a test framework this harness can "
            f"classify. Allowed: {sorted(_FRAMEWORKS)}. The gate tells 'the "
            "bug is present' from 'the environment is broken' by reading what "
            "the runner reported, and each framework reports it differently -- "
            "pytest through its exit code, vitest and jest through a JSON "
            "report, because both of those exit 1 for a test failure and for a "
            "broken config alike"
        )
    return value


def _node_version(value: Any, where: str) -> str:
    if value is None:
        return _DEFAULT_NODE
    if not isinstance(value, str):
        raise TaskError(
            f"{where}: {value!r} is {type(value).__name__}, not a string -- "
            'quote it (`node: "22"`). YAML reads an unquoted 22.10 as the '
            "float 22.1, so the version that reaches the build is not the one "
            "the manifest names"
        )
    if value not in _NODE_VERSIONS:
        raise TaskError(
            f"{where}: {value!r} is not a Node version this base image is "
            f"known to build. Allowed: {sorted(_NODE_VERSIONS)}. To add one, "
            "build docker/eval-agent-node.Dockerfile with `--build-arg "
            "BASE_NODE_VERSION=<v>`, confirm the claude and runner pin "
            "assertions fire, then add it to _NODE_VERSIONS and to "
            "taskset/HARVESTING.md"
        )
    return value


def task_runtime(task) -> tuple[str, str]:
    """Which base image this task needs: `("python", "3.12")` or `("node", "22")`.

    Derived from `tests.framework`, never declared. Two manifest keys that can
    each imply a runtime is two sources for one fact, and a disagreement
    between them is a task gated in one interpreter and run in another -- the
    silent shape, since both halves would build and both would run.
    """
    if task.tests.framework in _NODE_FRAMEWORKS:
        return ("node", task.image.node)
    return ("python", task.image.python)
```

- [ ] **Step 4: Add the fields and wire the parse**

`TaskTests` gains, after `paths`:

```python
    #: Which runner adapter reads this suite. `pytest` (the default) keeps
    #: every existing manifest loading unchanged. See `_FRAMEWORKS` for why the
    #: set is closed and `bakeoff/src/bakeoff/runners/` for what an adapter is.
    framework: str = "pytest"
```

(placed after `paths` and given a default so it does not disturb the positional
construction in `load_task`; the field ordering in a frozen dataclass with
defaults requires every following field to have one, which `p2p` and
`allow_extra_paths` already do and `runner`/`f2p` do not — so put `framework`
**after `allow_extra_paths`**, at the end, and pass it by keyword.)

`TaskImage` gains, after `python`:

```python
    #: The node base image's major, for a `vitest`/`jest` task. Ignored -- and
    #: REFUSED -- on a pytest task: see `task_runtime`.
    node: str = _DEFAULT_NODE
```

In `_parse_tests` (or wherever `tests_raw` is read), after `runner` is parsed:

```python
    framework = _framework(tests_raw.get("framework"), f"{where}:tests.framework")
    adapter = for_framework(framework)
    if not any(adapter.runner_marker in part for part in runner):
        raise TaskError(
            f"{where}: tests.framework is {framework!r} but no element of "
            f"tests.runner contains {adapter.runner_marker!r} "
            f"({list(runner)!r}). Each key catches the other's typo, which is "
            "why neither is derived from the other -- and a runner read by the "
            "wrong adapter is classified by the wrong rules."
        )
    for node_id in (*f2p, *p2p):
        adapter.validate_node_id(node_id, test_paths, where)
    adapter.validate_id_set((*f2p, *p2p), where)
```

And after `image_raw` is read:

```python
    declared_node = image_raw.get("node")
    declared_python = image_raw.get("python")
    is_node = framework in _NODE_FRAMEWORKS
    if is_node and declared_python is not None:
        raise TaskError(
            f"{where}: image.python is declared but tests.framework is "
            f"{framework!r}, which runs on the node base image. The runtime "
            "comes from the framework; declaring both is two sources for one "
            "fact, and a disagreement gates the task in one interpreter and "
            "runs it in another."
        )
    if not is_node and declared_node is not None:
        raise TaskError(
            f"{where}: image.node is declared but tests.framework is "
            f"{framework!r}, which runs on the python base image. The runtime "
            "comes from the framework; declaring both is two sources for one "
            "fact."
        )
```

- [ ] **Step 5: Implement `validate_node_id` and `validate_id_set` in the node adapter stub**

The stub from Task 1 raises `NotImplementedError` for everything. These two are what Task 4 needs, so implement them now (the rest stays stubbed until Task 6). Add `validate_id_set(self, node_ids: tuple[str, ...], where: str) -> None` to the `RunnerAdapter` protocol in Task 1's `__init__.py`; the pytest adapter's is empty, with the comment *"pytest selects by the whole node id, path included, so two modules may carry the same test name unambiguously — the node rule would refuse a manifest that loads today over a hazard pytest does not have."*

```python
    def validate_node_id(self, node_id, paths, where):
        """`<file>::<full test name>`, and the file must be inside the scope.

        `::` rather than a custom separator so `f2p_modules`' "split once from
        the left" stays correct verbatim across both runtimes, and so the
        `::`-presence discriminator that tells a test id from a file id keeps
        working. A JS title may itself contain `::`; splitting from the LEFT
        with maxsplit 1 is unambiguous anyway, which is the same argument the
        pytest parser makes for parametrized ids.

        The scope rule exists because a deselection that matches nothing is
        SILENT here: measured 2026-09-01, `-t` with a pattern matching no test
        exits 0 on both vitest and jest with every test reported skipped. An id
        whose file is outside `tests.paths` can never be deselected from the
        scoped p2p run, and nothing downstream would say so.
        """
        from bakeoff.tasks import TaskError, _under

        if "::" not in node_id:
            raise TaskError(
                f"{where}: {node_id!r} is not a {self.name} node id. The shape "
                "is `<file>::<full test name>`, where the name is the "
                "reporter's `fullName` -- the describe titles and the test "
                "title joined by single spaces"
            )
        path, _, title = node_id.partition("::")
        if not path or not title:
            raise TaskError(
                f"{where}: {node_id!r} has an empty half; both the file and "
                "the full test name are required"
            )
        if path.startswith("-"):
            raise TaskError(
                f"{where}: {node_id!r}'s file half starts with '-', which the "
                "runner would parse as a flag rather than as a file filter"
            )
        if not _under(path, tuple(paths)):
            raise TaskError(
                f"{where}: {node_id!r}'s file is outside tests.paths "
                f"({list(paths)}). The scoped p2p run selects by those "
                "prefixes, so this id could never be deselected from it -- and "
                "a deselection that matches nothing exits 0 with every test "
                "skipped (measured), so nothing downstream would say so"
            )

    def validate_id_set(self, node_ids, where):
        """No two declared ids may share a full name across different files.

        `-t` matches `fullName` and knows nothing about which file a test came
        from: the file positionals and the name pattern are ANDed across the
        whole run, never paired, and no flag scopes a name to a file. So a
        quarantine of `a.test.js::works` ALSO deselects `b.test.js::works` --
        silently, with `p2p_deselected` agreeing, because two tests really were
        skipped -- and a selection of `a.test.js::works` plus
        `b.test.js::other` also runs `b.test.js::works`.

        This is the half an author can create. The half that matters more is a
        collision between a declared id and a test the manifest never mentions,
        which no loader can see; preflight asserts that against the real report
        of the scoped run.

        Per-file invocations would pair them exactly and are rejected: they
        multiply every gated and graded suite run by the number of distinct
        files, change what the framework collects, and turn the gated argv into
        a LIST of argvs, which the byte-identity property is not stated over.
        """
        from bakeoff.tasks import TaskError

        by_name: dict[str, str] = {}
        for node_id in node_ids:
            path, _, title = node_id.partition("::")
            first = by_name.setdefault(title, path)
            if first != path:
                raise TaskError(
                    f"{where}: {title!r} is the full name of a test in both "
                    f"{first!r} and {path!r}. {self.name} selects and "
                    "deselects by name alone -- there is no flag that scopes a "
                    "name pattern to a file -- so a quarantine of one would "
                    "silently remove the other from the regression check, and "
                    "the deselection count would agree. Rename one of them in "
                    "the task repo, or narrow tests.paths so only one is in "
                    "scope; see taskset/HARVESTING.md"
                )
```

- [ ] **Step 6: Run the tests**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS, at the baseline plus the new tests.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/src/bakeoff/runners/node_adapter.py \
        bakeoff/tests/test_tasks.py
git commit -m "feat: tests.framework selects the runner adapter, and the runtime follows it

A JS repo cannot be cut as a task while the loader has no way to say which
runner reads its suite. tests.framework does, from a closed set that must equal
the adapter registry, and image.node names the node base's major.

The runtime is DERIVED from the framework rather than declared a second time.
Declaring image.python on a vitest task, or image.node on a pytest task, is a
load error: two keys that each imply a runtime is two sources for one fact, and
a disagreement gates the task in one interpreter and runs it in another.

Node ids are <file>::<full test name>, validated at load, including that the
file is inside tests.paths -- a deselection that matches nothing exits 0 with
every test reported skipped on both vitest and jest (measured), so an id
outside the scope would be a silent no-op.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 5: the node base image, and `(runtime, version)` base keys

**Files:**
- Create: `bakeoff/docker/eval-agent-node.Dockerfile`
- Modify: `bakeoff/src/bakeoff/images.py` (`base_tag`, `build_base_image`, `build_base_images`)
- Modify: `bakeoff/scripts/run_matrix.py` (the base set, `assert_one_agent`, `resolve_tasks`), `bakeoff/scripts/grade.py` (`task_resolver`)
- Test: `bakeoff/tests/test_images.py`, `bakeoff/tests/test_run_matrix.py`

**Interfaces:**
- Consumes: `tasks.task_runtime` from Task 4.
- Produces: `images.base_tag(runtime: str, version: str) -> str`; `images.build_base_image(repo_root, runtime: str, version: str, tag: str | None = None) -> str`; `images.build_base_images(repo_root, runtimes: set[tuple[str, str]]) -> dict[tuple[str, str], str]`. `resolve_tasks` and `grade.task_resolver` index `bases` by `task_runtime(task)`.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_images.py`:

```python
# --- per-runtime base images --------------------------------------------------


def test_base_tag_names_the_runtime_and_the_version():
    """The tag string moves (`base-3.12` -> `base-python-3.12`) and nothing
    else does: the python Dockerfile is unchanged, so the image ID is
    unchanged, and every cache in this repo keys on the ID -- preflight's, the
    oracle's fingerprint, Versions.container_image_digest. No warm verdict is
    invalidated by the rename."""
    from bakeoff.images import base_tag

    assert base_tag("python", "3.12") == "bakeoff-eval-agent:base-python-3.12"
    assert base_tag("node", "22") == "bakeoff-eval-agent:base-node-22"


def test_build_base_images_builds_each_pair_exactly_once(monkeypatch):
    calls = []
    from bakeoff import images

    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args))
    monkeypatch.setattr(images, "image_id", lambda tag: f"sha256:{tag}")

    built = images.build_base_images(
        Path("/repo"),
        {("python", "3.12"), ("node", "22"), ("python", "3.12")},
    )

    assert set(built) == {("python", "3.12"), ("node", "22")}
    assert len(calls) == 2


def test_each_runtime_gets_its_own_dockerfile_and_build_arg(monkeypatch):
    """Two files, not one with a switched FROM. node:22-bookworm-slim already
    occupies uid 1000 with a `node` user, so `useradd --uid 1000 eval` exits 4
    there and succeeds on python:3.12-slim-bookworm (measured 2026-09-01) --
    and a shell conditional around a useradd is the shape that half-succeeds
    and leaves the image running as root, which Claude Code refuses."""
    from bakeoff import images

    seen = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: seen.append(args))
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:x")

    images.build_base_image(Path("/repo"), "node", "22")

    argv = " ".join(seen[0])
    assert "eval-agent-node.Dockerfile" in argv
    assert "BASE_NODE_VERSION=22" in argv
    assert "BASE_PYTHON_VERSION" not in argv


def test_both_base_dockerfiles_pin_the_same_claude_code_version():
    """Versions.claude_code is read from the transcript, per run, and nothing
    compares it ACROSS tasks. Two Dockerfiles carrying two ARG lines is a real
    way for two arms of one comparison to run different agents, invisibly."""
    root = Path(__file__).resolve().parent.parent / "docker"
    pattern = re.compile(r"^ARG CLAUDE_CODE_VERSION=(\S+)", re.M)

    pins = {
        pattern.search(p.read_text()).group(1)
        for p in (root / "eval-agent.Dockerfile",
                  root / "eval-agent-node.Dockerfile")
    }

    assert len(pins) == 1


def test_both_base_dockerfiles_run_as_a_non_root_eval_user_and_clear_entrypoint():
    root = Path(__file__).resolve().parent.parent / "docker"
    for name in ("eval-agent.Dockerfile", "eval-agent-node.Dockerfile"):
        text = (root / name).read_text()
        assert "USER eval" in text
        assert "ENTRYPOINT []" in text
        assert "--uid 1000 eval" in text


def test_the_node_dockerfile_deletes_the_images_own_uid_1000_user_first():
    """Measured 2026-09-01: node:22-bookworm-slim ships `node:x:1000:1000`, and
    `useradd --create-home --uid 1000 eval` fails there with exit 4. Without
    the userdel the build dies; with it removed later, it dies again."""
    text = (Path(__file__).resolve().parent.parent / "docker"
            / "eval-agent-node.Dockerfile").read_text()

    assert text.index("userdel") < text.index("--uid 1000 eval")


def test_the_node_dockerfile_installs_the_runners_outside_repo():
    """Baking node_modules at /repo is REPLACED by the bind mount -- measured,
    `node_modules GONE` -- which is the failure `pip install -e .` avoids and
    node has no site-packages to avoid it with. /node_modules resolves because
    node's resolver walks up from the importing file."""
    text = (Path(__file__).resolve().parent.parent / "docker"
            / "eval-agent-node.Dockerfile").read_text()

    # M15: `npm install --prefix /` fails on a bare tree with `Tracker
    # "idealTree" already exists`, so /package.json is written first and the
    # install runs with `cd /` and no --prefix.
    assert "> /package.json" in text
    assert "--save-dev" not in text   # the runners must stay under dependencies
    assert "/repo/node_modules" not in text
    assert "NODE_PATH" not in text  # measured unnecessary; see the plan's D4
```

Append to `bakeoff/tests/test_run_matrix.py`:

```python
def test_the_base_set_is_the_runtimes_the_task_set_actually_needs(monkeypatch):
    from bakeoff.tasks import task_runtime

    tasks = [_fake_task(framework="pytest"), _fake_task(framework="vitest")]

    assert {task_runtime(t) for t in tasks} == {("python", "3.12"), ("node", "22")}


def test_disagreeing_claude_versions_across_bases_refuse_the_whole_invocation(
    monkeypatch
):
    """Broadening 5's cross-base check, unchanged in CONTRACT and widened only
    in key type -- it takes `{key: image_id}`, probes each with
    `base_claude_version`, and raises `ImageError`. Two Dockerfiles carrying two
    `ARG CLAUDE_CODE_VERSION` lines is where it earns the most: nothing in a
    record compares `Versions.claude_code` across tasks.

    An EMPTY probe is a refusal, not agreement: preflight's guard is `elif
    expected_claude_version and ...`, falsy on "", so "every base failed to
    answer" would silently disable the agent-version check for every task."""
    from bakeoff.images import ImageError
    from bakeoff.scripts import run_matrix as rm

    monkeypatch.setattr(
        rm, "base_claude_version",
        lambda image: "2.1.219" if "node" in image else "2.1.220")
    with pytest.raises(ImageError):
        rm.assert_one_agent({("python", "3.12"): "sha256:py",
                             ("node", "22"): "sha256:node"})

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "")
    with pytest.raises(ImageError):
        rm.assert_one_agent({("python", "3.12"): "sha256:py",
                             ("node", "22"): "sha256:node"})
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_images.py tests/test_run_matrix.py -q
```

Expected: FAIL — `base_tag() takes 1 positional argument but 2 were given`; `FileNotFoundError: .../eval-agent-node.Dockerfile`.

- [ ] **Step 3: Write `bakeoff/docker/eval-agent-node.Dockerfile`**

```dockerfile
# The node eval base image (spec section 5.1), the sibling of
# eval-agent.Dockerfile. Broadening 7.
#
# TWO FILES RATHER THAN ONE WITH A SWITCHED `FROM`, on three measured
# differences that are not parameters:
#
#   node:22-bookworm-slim already ships a `node` user at uid 1000, so
#   `useradd --create-home --uid 1000 eval` fails there with exit 4 (measured
#   2026-09-01) where it succeeds on python:3.12-slim-bookworm. A shell
#   conditional around a useradd is exactly the shape that half-succeeds and
#   leaves an image running as root -- which Claude Code refuses, emitting zero
#   stream-json events, on every arm of every task built from it.
#
#   The test runners are installed OUTSIDE /repo, which has no python analogue.
#
#   PYTHONDONTWRITEBYTECODE has no analogue at all (see below).
#
# What must stay identical to the python file -- the pinned Claude Code and its
# build-time assertion, git, ripgrep, coreutils, the non-root eval user at uid
# 1000, the CLAUDE_CONFIG_DIR/DISABLE_* block, WORKDIR /repo, ENTRYPOINT [] --
# is asserted by tests/test_images.py, which reads both files.
#
# Build:
#   docker build -f docker/eval-agent-node.Dockerfile \
#     --build-arg BASE_NODE_VERSION=22 -t bakeoff-eval-agent:base-node-22 .

# BASE_NODE_VERSION, never NODE_VERSION. The official node: images set their
# own `ENV NODE_VERSION` (measured 22.23.2), and ENV beats a redeclared ARG
# after FROM -- so a later edit that wanted the version after the FROM would
# silently read the patch-level string instead of the build arg, and the value
# would look plausible.
ARG BASE_NODE_VERSION=22
FROM node:${BASE_NODE_VERSION}-bookworm-slim

# ripgrep is a Claude Code runtime dependency; git is what snapshot_diff and
# the base_sha checkout need; `timeout` (coreutils) enforces the wall-clock
# budget from inside the container, because Docker offers no way to kill a
# running exec from outside.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        curl \
        git \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# THE TEST RUNNERS, INSTALLED AT THE CONTAINER ROOT AND NOT UNDER /repo.
#
# `/repo` in a built task image is a scaffold that the bind mount REPLACES at
# run time. For python that is survivable because `pip install -e .` puts the
# package in site-packages; node has no site-packages, and a node_modules baked
# at /repo/node_modules is simply gone once the tree is mounted -- measured
# 2026-09-01: "node_modules PRESENT" without the mount, "node_modules GONE"
# with it.
#
# Installing at `/` works because node's resolver walks up from the importing
# file: /repo/tests/x.test.js -> /repo/tests/node_modules -> /repo/node_modules
# -> /node_modules. Measured with NODE_PATH UNSET and as uid 1000: vitest and
# jest both run, a test file's import of a transitive dependency resolves, and
# a /repo/vitest.config.js is still discovered. NODE_PATH is deliberately not
# set -- it was not needed, it is ignored by ESM resolution and by both
# frameworks' own resolvers, and setting it would hide which mechanism worked.
#
# Not writable by the eval user (measured: `Permission denied`), so the agent
# cannot corrupt the runner it is being measured with.
#
# Pinned for the reason CLAUDE_CODE_VERSION is: these are part of the
# environment every arm is compared in, so a floating version would be an
# unrecorded difference between two runs the log swears were identical.
# `/package.json` is written FIRST and the install runs with `cd /` and NO
# `--prefix`. Measured 2026-09-02: `npm install --prefix / <pkgs>` into a tree
# that does not exist yet fails outright --
#
#     npm error Tracker "idealTree" already exists          (exit 1)
#
# -- from any cwd. `--prefix` is fine once `/package.json` and `/node_modules`
# are there, which is why a task's `image.build` keeps it.
#
# The runners land under `dependencies`, NOT `devDependencies` (measured:
# `deps: {"jest":"^30.5.0","vitest":"^3.2.7"}  dev: undefined`), and that is
# the coupling the whole scheme rests on: a task's later
# `npm install --prefix / --omit=dev <its deps>` does not prune them. Do not
# "tidy" this into `--save-dev` -- `--omit=dev` would then delete the runners
# on the first task that installs anything, and the failure reaches the model
# as exit 127 on every arm.
ARG VITEST_VERSION=3.2.7
ARG JEST_VERSION=30.5.0
RUN printf '{"name":"bakeoff-runners","private":true}\n' > /package.json \
    && cd / \
    && npm install --no-audit --no-fund \
        "vitest@${VITEST_VERSION}" "jest@${JEST_VERSION}" \
    && v="$(/node_modules/.bin/vitest --version)" \
    && case "$v" in \
         *"${VITEST_VERSION}"*) echo "vitest ${VITEST_VERSION} pinned" ;; \
         *) echo "expected vitest ${VITEST_VERSION}, got ${v}" >&2; exit 1 ;; \
       esac \
    && j="$(/node_modules/.bin/jest --version)" \
    && case "$j" in \
         "${JEST_VERSION}"*) echo "jest ${JEST_VERSION} pinned" ;; \
         *) echo "expected jest ${JEST_VERSION}, got ${j}" >&2; exit 1 ;; \
       esac

# Exported so the GENERATED task Dockerfile can re-assert them after its own
# image.build steps (the plan's D15) without a second copy of the numbers in
# images.py -- two copies of a pin is how one moves. The check is needed
# because a task's dependency install can remove these: `npm ci`'s documented
# contract is to delete node_modules before installing, and at the `/` prefix
# that is exactly where they live. Whether it fires depends on which
# package.json/package-lock.json pair npm resolves for the given prefix and
# cwd, which an image.build line decides by accident -- so HARVESTING
# recommends `npm install --prefix / --omit=dev` (measured additive) and this
# pair of variables is what makes the recommendation enforceable.
ENV BAKEOFF_VITEST_VERSION=${VITEST_VERSION} \
    BAKEOFF_JEST_VERSION=${JEST_VERSION}

# Pinned deliberately, and IDENTICAL to eval-agent.Dockerfile's. This value
# becomes versions.claude_code for every run built from this image, and the
# harness compares arms on the assumption that all of them ran the same agent.
# Nothing in a record compares it ACROSS tasks, so two bases carrying two
# values would be invisible -- run_matrix.assert_one_agent is what refuses it.
ARG CLAUDE_CODE_VERSION=2.1.220

# The `node` user occupies uid 1000 on this base (measured:
# `node:x:1000:1000::/home/node:/bin/bash`), so it is removed first. uid 1000
# is not cosmetic: it must match eval-agent.Dockerfile's, because the
# bind-mounted repo's ownership is honoured on a Linux host.
#
# NOT root, and this is load-bearing rather than hygiene: Claude Code refuses
# bypassPermissions under root and exits before emitting a single stream-json
# event.
RUN userdel -r node \
    && useradd --create-home --uid 1000 eval \
    && mkdir -p /repo /eval/claude-config \
    && chown -R eval:eval /repo /eval

USER eval
# /node_modules/.bin on PATH, and this is a measurement rather than a
# convenience. The install above puts the shims there and changes no
# environment: measured 2026-09-02, `command -v vitest` finds nothing on the
# default PATH, so a bare `vitest` is exit 127. `npx vitest` and `npm test`
# both work, because npx and npm resolve `.bin` themselves -- and section 3.3
# measures a loop run with commands THE AGENT INVENTS, so leaving the three
# spellings inconsistent means the agent discovers the rule by failing, inside
# the turn budget it is being scored on.
ENV HOME=/home/eval \
    PATH=/node_modules/.bin:/home/eval/.local/bin:$PATH

RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

# Set AFTER the install: the installer goes through the same update machinery
# these disable, so declaring them earlier makes the build print "Updates are
# disabled by your administrator", leave no binary behind, and still exit 0.
#
# THERE IS NO PYTHONDONTWRITEBYTECODE ANALOGUE HERE, and that is a measurement
# rather than an omission. CPython invalidates a .pyc on (source mtime in whole
# seconds, source size), and an agent's edit routinely preserves both -- so a
# correct fix is served the OLD behaviour and section 3.3's self-correction
# loop corrects away from the right answer. Measured 2026-09-01 on both node
# frameworks: overwrite a source with the fix at the SAME byte count inside the
# SAME second, re-run, and both go green. node_modules/.vite is vite's
# DEPENDENCY optimiser cache, not a source-transform cache, and jest's cache is
# content-hash keyed. The `--no-cache` that vitest tasks carry in tests.runner
# is there so the suite writes nothing into the TREE (section 5.6 stages
# everything), not for staleness -- do not delete it as redundant with a
# staleness problem that was never the problem.
ENV CLAUDE_CONFIG_DIR=/eval/claude-config \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN installed="$(claude --version)" \
    && case "$installed" in \
         "${CLAUDE_CODE_VERSION} "*) echo "claude ${CLAUDE_CODE_VERSION} pinned" ;; \
         *) echo "expected ${CLAUDE_CODE_VERSION}, got ${installed}" >&2; exit 1 ;; \
       esac

WORKDIR /repo

# RunContainer overrides this with `sleep infinity` and drives the container
# through exec; an image-declared ENTRYPOINT would prefix that command.
ENTRYPOINT []
```

- [ ] **Step 4: Grow `images.py`**

```python
#: One Dockerfile and one build arg per runtime. A dict rather than an
#: if/else so adding a runtime is one entry and the two facts about it cannot
#: drift apart.
_BASES = {
    "python": ("eval-agent.Dockerfile", "BASE_PYTHON_VERSION"),
    "node": ("eval-agent-node.Dockerfile", "BASE_NODE_VERSION"),
}


def base_tag(runtime: str, version: str) -> str:
    """The local tag for one base image.

    `bakeoff-eval-agent:base-3.12` became `base-python-3.12` when node joined.
    The tag string is the ONLY thing that moved: the python Dockerfile is
    unchanged, so its image ID is unchanged, and every cache in this repo keys
    on the ID rather than the tag -- `preflight_cache_key`, `oracle_fingerprint`
    and `Versions.container_image_digest` alike. So no warm verdict is
    invalidated by this rename, and the old tag is left orphaned on any machine
    that has one, pointing at the same image.
    """
    if runtime not in _BASES:
        raise ImageError(f"no base image is defined for runtime {runtime!r}")
    return f"bakeoff-eval-agent:base-{runtime}-{version}"


def build_base_image(repo_root: Path, runtime: str, version: str,
                     tag: str | None = None) -> str:
    """Build one base image and return its ID."""
    dockerfile, build_arg = _BASES[runtime]
    tag = tag or base_tag(runtime, version)
    _run([
        "docker", "build", "-q",
        "--build-arg", f"{build_arg}={version}",
        "-f", str(Path(repo_root) / "docker" / dockerfile),
        "-t", tag, str(repo_root),
    ])
    return image_id(tag)


def build_base_images(repo_root: Path,
                      runtimes: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Every base the task set needs, built once per distinct pair.

    Keyed by `(runtime, version)` because that is what identifies a base now.
    Broadening 5 keyed it by version alone and said in as many words that when
    node arrived the key would become whatever tuple identifies a base and that
    this function and `base_tag` would be the only places that changed.
    """
    return {
        pair: build_base_image(repo_root, *pair)
        for pair in sorted(runtimes)
    }
```

- [ ] **Step 5: `render_dockerfile` re-asserts the runner pins on a node base**

Emit, after the last `image.build` `RUN` and **only** when the base is a node
base, one step:

```dockerfile
RUN v="$(/node_modules/.bin/vitest --version 2>/dev/null)"; \
    j="$(/node_modules/.bin/jest --version 2>/dev/null)"; \
    case "$v" in *"${BAKEOFF_VITEST_VERSION}"*) ;; \
      *) echo "image.build changed the pinned test runner: expected vitest ${BAKEOFF_VITEST_VERSION}, got '${v}'. \
Use 'npm install --prefix / --omit=dev', never 'npm ci' -- npm ci deletes node_modules at the prefix before installing, and the cwd where it does not delete them is the one where it installs none of yours instead. The base saves the runners under 'dependencies', so --omit=dev never prunes them." >&2; exit 1 ;; \
    esac; \
    case "$j" in "${BAKEOFF_JEST_VERSION}"*) ;; \
      *) echo "image.build changed the pinned test runner: expected jest ${BAKEOFF_JEST_VERSION}, got '${j}'." >&2; exit 1 ;; \
    esac
```

`render_dockerfile` gains a `runtime: str = "python"` keyword and emits this
only for `"node"`, so a python task image renders **byte-identically to
today's** — which `test_images.py`'s existing rendering tests already pin, and
which is the reason the parameter defaults rather than being required.

Add one test:

```python
def test_a_node_task_image_re_asserts_the_bases_runner_pins_after_build():
    """A task's own dependency install can remove the runners: `npm ci`'s
    documented contract is to delete node_modules before installing, and at the
    `/` prefix that is where they live. HARVESTING recommends `npm install`
    instead -- but a convention this codebase cannot enforce is one a task will
    eventually violate, and the failure reaches the model as exit 127 on every
    arm of that task."""
    text = render_dockerfile("base", apt=[], pip=[],
                             build=["npm install --prefix / lodash"],
                             runtime="node")

    assert text.index("npm install --prefix / lodash") < text.index(
        "BAKEOFF_VITEST_VERSION")
    assert "npm ci" in text  # the remedy is named in the failure message


def test_a_python_task_image_renders_byte_identically_to_today():
    assert render_dockerfile("base", apt=["less"], pip=["pytest==8.3.5"],
                             build=[]) == render_dockerfile(
        "base", apt=["less"], pip=["pytest==8.3.5"], build=[],
        runtime="python")
```

- [ ] **Step 6: Wire the two drivers**

In `run_matrix.main`: build `{task_runtime(t) for t in tasks}`, pass the mapping to `resolve_tasks(..., bases=…)`, which indexes it by `task_runtime(task)`. `assert_one_agent` keeps **broadening 5's contract exactly** — `assert_one_agent(bases: dict[K, str]) -> str`, where the values are **image ids**, which it probes itself with `base_claude_version(image)`, raising `ImageError` if any probe is empty or the answers disagree and otherwise returning the single version. The only change is the key type, `str` → `tuple[str, str]`, and since the function never inspects the key, that is a type-annotation edit. `base_claude_version` does not move and its call site stays inside `assert_one_agent`. An empty probe stays a refusal rather than agreement, because preflight's guard is `elif expected_claude_version and ...` and is falsy on `""` — which would silently disable the agent-version check for every task in the matrix.

In `grade.task_resolver`: the base cache becomes a dict keyed by the pair, built lazily, preserving the property its docstring claims (a batch whose records are all gated builds nothing).

`tests/test_integration_grader.py::click_image` calls `build_base_image(REPO_ROOT)`; it becomes `build_base_image(REPO_ROOT, "python", "3.12")`. That is a **signature** change, so update the call — the image it produces is byte-identical.

- [ ] **Step 7: Build the node base for real and record what it produced**

```bash
cd bakeoff && docker build -f docker/eval-agent-node.Dockerfile \
  --build-arg BASE_NODE_VERSION=22 -t bakeoff-eval-agent:base-node-22 .
docker run --rm bakeoff-eval-agent:base-node-22 sh -c \
  'node -v; vitest --version; jest --version; claude --version; id -u; command -v git rg timeout vitest jest; echo "$BAKEOFF_VITEST_VERSION $BAKEOFF_JEST_VERSION"'
```

Expected: the three in-Dockerfile pin assertions all print (`vitest 3.2.7 pinned`, `jest 30.5.0 pinned`, `claude 2.1.220 pinned`), `id -u` is `1000`, `git`/`rg`/`timeout` all resolve, and — the point of the bare `vitest --version` rather than the absolute path — **`vitest` and `jest` resolve on `PATH`**. They do so *only* because of the `ENV PATH=/node_modules/.bin:$PATH` added in Step 3: on the stock base `command -v vitest` finds nothing (M12), so this line of the probe is the assertion for that ENV and fails loudly if it is ever dropped. Paste the output into `tasks/todo.md` in Task 10.

- [ ] **Step 8: Run the suite and commit**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

```bash
git add bakeoff/docker/eval-agent-node.Dockerfile bakeoff/src/bakeoff/images.py \
        bakeoff/scripts/run_matrix.py bakeoff/scripts/grade.py \
        bakeoff/tests/test_images.py \
        bakeoff/tests/test_images.py bakeoff/tests/test_run_matrix.py \
        bakeoff/tests/test_integration_grader.py
git commit -m "feat: a node base image, and base images keyed by (runtime, version)

A vitest task needs node in the image, and node cannot be an ARG on the python
base: node:22-bookworm-slim ships a \`node\` user at uid 1000, so \`useradd --uid
1000 eval\` exits 4 there (measured), and a shell conditional around a useradd
is the shape that half-succeeds and leaves the image running as root -- which
Claude Code refuses, emitting zero events on every arm.

The test runners install at /node_modules, not /repo/node_modules: the bind
mount replaces /repo wholesale, so a baked node_modules is simply gone
(measured), and node has no site-packages to survive it the way pip -e does.

base_tag and build_base_images take the (runtime, version) key broadening 5
said they would. The python image ID does not move, so no cached verdict is
invalidated by the tag rename.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 6: the node adapter, against the eight captured reports

No Docker. The eight report shapes measured in M2 are committed as fixtures and replayed forever; re-measuring them needs a network and 73 MB of npm.

**Files:**
- Create: `bakeoff/tests/fixtures/node_reports/{pass,fail,import_error,syntax_error,no_files,t_nomatch,t_match,mixed}.{vitest,jest}.json` (16 files; capture both frameworks for each shape)
- Modify: `bakeoff/src/bakeoff/runners/node_adapter.py` (replace the Task 1 stub)
- Test: `bakeoff/tests/test_runners.py` (new section)

**Interfaces:**
- Produces: `node_adapter.VITEST`, `node_adapter.JEST` (both `_NodeFlavour` instances satisfying `RunnerAdapter`); `node_adapter.verify_selected(report, requested, adapter) -> frozenset[str]`.
- Consumes: `bakeoff.runners.Outcome`, `KIND_*`.

- [ ] **Step 1: Capture the fixtures**

```bash
mkdir -p "$HOME/.cache/bakeoff-nodefixtures" && cd "$HOME/.cache/bakeoff-nodefixtures"
# package.json with vitest@3.2.7 and jest@30.5.0, tests/{pass,fail,broken,syntax}.test.js
# and jtests/*.test.cjs as in the plan's Measurements section, then in a
# node:22-bookworm-slim container:
#   vitest run --reporter=json --outputFile=<f> <args>
#   jest -c jest.config.cjs --json --outputFile=<f> <args>
```

Copy each into `bakeoff/tests/fixtures/node_reports/` and **rewrite the absolute `name` fields to `/repo/...`** so the fixtures exercise the rootdir-relative conversion the real runs need. Add a one-line `README.md` beside them recording the framework versions and the argv each came from — a fixture whose provenance is not written down is a fixture nobody dares regenerate.

- [ ] **Step 2: Write the failing tests**

Append to `bakeoff/tests/test_runners.py`:

```python
# --- the node adapters --------------------------------------------------------

import json
from pathlib import Path

_REPORTS = Path(__file__).resolve().parent / "fixtures" / "node_reports"


def _report(shape: str, framework: str) -> dict:
    return json.loads((_REPORTS / f"{shape}.{framework}.json").read_text())


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_report_that_was_never_written_is_an_environment_problem(framework):
    """Measured 2026-09-01: a broken config exits 1 and writes NO report file,
    on both frameworks. So does a runner that could not start. Reading silence
    as anything but "the command did not say what it did" is the Phase 0c
    failure, and here it would stamp a config error on the model."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="Validation Error", report=None
    )

    assert outcome.kind == KIND_ENVIRONMENT
    assert outcome.errored_files is None


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_all_passing_is_passed(framework):
    outcome = for_framework(framework).classify(
        exit_code=0, stdout="", stderr="", report=_report("pass", framework)
    )

    assert outcome.kind == KIND_PASSED
    assert outcome.failed_ids == frozenset()
    assert outcome.files_run == ("tests/pass.test.js",)


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_failing_assertion_is_failed_with_the_id_the_manifest_uses(framework):
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("fail", framework)
    )

    assert outcome.kind == KIND_FAILED
    assert outcome.failed_ids == {"tests/fail.test.js::will fail"}


@pytest.mark.parametrize("framework", ["vitest", "jest"])
@pytest.mark.parametrize("shape", ["import_error", "syntax_error"])
def test_a_file_that_could_not_load_is_a_load_error(framework, shape):
    """The portable discriminator is a testResults entry with status 'failed'
    and an EMPTY assertionResults -- true on both frameworks and on both
    shapes. jest's numRuntimeErrorTestSuites says the same thing and vitest has
    no such key, so it is deliberately not consulted: one classifier, not two."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report(shape, framework)
    )

    assert outcome.kind == KIND_LOAD_ERROR
    assert outcome.errored_files == {
        "import_error": frozenset({"tests/broken.test.js"}),
        "syntax_error": frozenset({"tests/syntax.test.js"}),
    }[shape]


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_load_error_beats_a_passing_file_in_the_same_run(framework):
    """Measured: one good file plus one unloadable file reports THREE PASSING
    tests and zero failing ones. Classified in the other order that grades as
    `passed`, and a task whose f2p file stopped importing is scored as solved."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("mixed", framework)
    )

    assert outcome.kind == KIND_LOAD_ERROR
    assert "tests/broken.test.js" in outcome.errored_files


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_no_files_matched_is_nothing_ran_whatever_success_says(framework):
    """jest reports `success: true` while exiting 1 on 'no test files matched'
    (measured). Reading `success` would call a run that executed nothing a
    pass, which on the p2p check is a regression suite that ran zero tests."""
    outcome = for_framework(framework).classify(
        exit_code=1, stdout="", stderr="", report=_report("no_files", framework)
    )

    assert outcome.kind == KIND_NOTHING_RAN


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_a_name_pattern_that_matched_nothing_is_nothing_ran_despite_exit_zero(
    framework
):
    """The single most dangerous measured behaviour in this broadening. `-t`
    with a pattern matching no test exits **0** on both frameworks, with every
    test reported skipped and a summary that reads like success. pytest answers
    the same input with exit 4 and `ERROR: not found:`.

    Two things land here: a manifest naming a renamed f2p test, and an oracle
    quarantine that swallowed the entire p2p list -- which `derive_quarantine`'s
    guard used to catch through pytest's exit 5."""
    outcome = for_framework(framework).classify(
        exit_code=0, stdout="", stderr="", report=_report("t_nomatch", framework)
    )

    assert outcome.kind == KIND_NOTHING_RAN
    assert outcome.exit_code == 0


@pytest.mark.parametrize("framework", ["vitest", "jest"])
def test_verify_selected_names_the_requested_ids_that_did_not_run(framework):
    """Separate from `classify`, because only the caller knows what it asked
    for. A skipped test and a test that was never selected are the same shape
    in the report."""
    from bakeoff.runners.node_adapter import verify_selected

    report = _report("t_nomatch", framework)
    adapter = for_framework(framework)

    assert verify_selected(
        report, ("tests/pass.test.js::outer adds",), adapter
    ) == {"tests/pass.test.js::outer adds"}
    assert verify_selected(
        _report("t_match", framework),
        ("tests/pass.test.js::outer adds",), adapter
    ) == frozenset()


# --- node argv ----------------------------------------------------------------


def test_node_select_args_are_the_files_plus_one_anchored_name_alternation():
    adapter = for_framework("vitest")

    assert adapter.select_args(
        ("tests/a.test.js::outer adds", "tests/a.test.js::top level")
    ) == ["tests/a.test.js", "-t", "^(?:outer adds|top level)$"]


def test_node_p2p_args_compose_selection_and_deselection_into_ONE_pattern():
    """Emitting two `-t` flags is not "last one wins" -- the frameworks
    disagree and neither answer is usable. Measured 2026-09-02:

        $ vitest run -t 'a' -t 'b' tests/pass.test.js
        Error: Expected a single value for option
        "-t, --testNamePattern <pattern>", received ["a", "b"]
        -> exit 1, and NO report file

        $ jest -t 'adds' -t 'subs'
        Ran all test suites with tests matching "adds,subs".
        -> exit 0, zero tests run

    vitest's refusal is loud and classifies as KIND_ENVIRONMENT; jest's
    comma-join is M1's silent hole reached by an argv nobody meant to write.
    Hence one method owning the whole argv, and hence this count."""
    adapter = for_framework("vitest")

    argv = adapter.p2p_args(
        selected=(), scope=("tests/",),
        deselected=("tests/a.test.js::outer adds",), ignored=())

    assert argv.count("-t") == 1
    assert argv == ["tests/", "-t", "^(?!(?:outer adds)$)"]


def test_node_p2p_args_on_the_explicit_branch_anchor_both_halves():
    adapter = for_framework("jest")

    assert adapter.p2p_args(
        selected=("tests/b.test.js::keeps working",), scope=(),
        deselected=("tests/b.test.js::flaky",), ignored=()
    ) == ["tests/b.test.js", "-t", "^(?!(?:flaky)$)(?:keeps working)$"]


def test_a_test_name_with_regex_metacharacters_is_escaped_the_JS_way():
    """Measured: `-t '^handles a\+b \(x\) \[y\]$'` selects exactly the test
    named `handles a+b (x) [y]` and skips `... [y] EXTRA`. Unescaped, the `+`
    and the groups change what the pattern means.

    `re.escape` is NOT usable here and the difference is not cosmetic: Python
    escapes characters JavaScript treats as IDENTITY ESCAPES, and an identity
    escape of a non-syntax character is a SyntaxError in a Unicode-mode
    RegExp. The pattern is compiled by node, not by Python, so the escape set
    has to be JavaScript's -- exactly the twelve characters `. * + ? ^ $ { } (
    ) | [ ] \` -- and nothing else. A space in particular must NOT be escaped;
    Python's `re.escape` escaped it through 3.6 and a `\ ` reaching node is a
    pattern that means something different.
    """
    argv = for_framework("vitest").select_args(("tests/m.test.js::a+b (x) y",))

    assert argv[-1] == r"^(?:a\+b \(x\) y)$"


def test_the_escape_set_is_javascripts_and_not_pythons():
    from bakeoff.runners.node_adapter import _js_escape

    assert _js_escape("a+b") == r"a\+b"
    assert _js_escape("a b") == "a b"          # a space is NOT escaped
    assert _js_escape("a-b") == "a-b"          # nor a hyphen, outside a class
    assert _js_escape("[x]") == r"\[x\]"


def test_node_report_args_write_to_a_fixed_path_outside_repo():
    """FIXED because it is an argv element and the gated argv must equal the
    graded argv; outside /repo because section 5.6 stages everything and a
    report inside the tree lands in every submission diff. Staleness is handled
    by deleting the file before each run, which `_Runner.run` does as a
    separate exec."""
    for framework, flag in (("vitest", "--reporter=json"), ("jest", "--json")):
        adapter = for_framework(framework)
        path = adapter.report_path()

        assert path.startswith("/tmp/")
        assert adapter.report_args(path) == [flag, f"--outputFile={path}"]


def test_node_ignore_flags_are_per_framework_and_jest_keeps_its_default():
    """`ignored` has exactly ONE caller -- preflight's p2p run at the START
    state, which the grader never makes -- so the two spellings cannot diverge
    between a gated and a graded argv.

    Measured 2026-09-02: vitest `--exclude <path>` and jest
    `--testPathIgnorePatterns <path>` both drop the named file. jest's flag
    REPLACES its built-in `/node_modules/` ignore rather than adding to it, so
    emitted alone it makes jest collect test files out of node_modules --
    which, with the runners installed at /node_modules, means jest's own
    vendored fixtures. The default is therefore re-emitted alongside."""
    assert for_framework("vitest").p2p_args(
        selected=(), scope=("tests/",), deselected=(), ignored=("tests/x.js",)
    ) == ["tests/", "--exclude=tests/x.js"]

    assert for_framework("jest").p2p_args(
        selected=(), scope=("tests/",), deselected=(), ignored=("tests/x.js",)
    ) == ["tests/",
          "--testPathIgnorePatterns=/node_modules/",
          "--testPathIgnorePatterns=tests/x.js"]


def test_jest_does_not_emit_its_default_ignore_when_there_is_nothing_to_ignore():
    """Emitting it on every run would put a flag in both the gated and the
    graded argv that changes what jest collects for EVERY task, for the benefit
    of the one preflight run that uses `ignored`."""
    assert for_framework("jest").p2p_args(
        selected=(), scope=("tests/",), deselected=(), ignored=()
    ) == ["tests/"]


def test_node_no_cache_args_are_measured_per_framework():
    """vitest creates <cwd>/node_modules/.vite -- inside the bind-mounted tree
    -- and `--no-cache` prevents it entirely (measured). jest's DEFAULT
    cacheDirectory is /tmp/jest_0, already outside the tree, and jest wrote
    nothing into the tree in any measured run."""
    assert for_framework("vitest").no_cache_args == ("--no-cache",)
    assert for_framework("jest").no_cache_args == ()


def test_node_parse_deselected_counts_pending_tests():
    report = _report("t_nomatch", "vitest")

    assert for_framework("vitest").parse_deselected(stdout="", report=report) == 3
    assert for_framework("vitest").parse_deselected(stdout="", report=None) is None
```

- [ ] **Step 3: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_runners.py -q -k node
```

Expected: FAIL — `NotImplementedError: broadening 7 Task 6`.

- [ ] **Step 4: Implement `node_adapter.py`**

Replace the stub. Structure: one `_NodeFlavour` dataclass carrying `name`, `runner_marker`, `no_cache_args` and `_report_flag`, with every method on it, and two module-level instances.

```python
"""vitest and jest, whose exit codes say nothing. Spec sections 3.3 and 4.2.1.

Measured 2026-09-01 in node:22-bookworm-slim, vitest 3.2.7 and jest 30.5.0.

    all tests pass ............................. 0
    one test fails ............................. 1
    a file with an unresolvable import ......... 1
    a file with a syntax error ................. 1
    a nonexistent file given as an argument .... 1
    a broken config file ....................... 1
    `-t` matching no test ...................... 0

The gate's whole job -- telling "the bug is present" from "the environment is
broken" -- has no exit code to read here. So this adapter reads a JSON report
instead, written to a file outside /repo, and the exit code is recorded rather
than consulted.

The last row is the one with no pytest analogue and the most dangerous shape.
pytest answers a selection that matches nothing with exit 4 and `ERROR: not
found:`; both node frameworks answer it with **0** and a summary that reads
like success (`Tests 3 skipped (3)` / `Tests: 3 skipped, 3 total`). A manifest
naming a renamed f2p test, and an oracle quarantine that swallowed the entire
p2p list, both arrive that way. `classify` calls it KIND_NOTHING_RAN whenever
no assertion reached a terminal status, and `verify_selected` names the
requested ids that did not run.

ONE MODULE FOR BOTH FRAMEWORKS, because one classifier covers both. The
load-error discriminator is a `testResults` entry with `status == "failed"` and
an EMPTY `assertionResults` -- true on both, on an import error and on a syntax
error alike. jest's `numRuntimeErrorTestSuites` says the same thing and vitest
has no such key, so it is deliberately not consulted; a second, jest-only path
would be a second thing that can be wrong about what happened. `testExecError`
is documented by jest and did NOT appear in any measured report, so nothing
here depends on it either.
"""
```

The classification, in the order D6 fixes:

```python
    def classify(self, *, exit_code, stdout, stderr, report):
        if report is None:
            return Outcome(
                kind=KIND_ENVIRONMENT, exit_code=exit_code, errored_files=None,
                explain=("the runner wrote no JSON report, so it did not say "
                         "what it did -- measured, a broken config exits 1 and "
                         "writes no file on both frameworks, and so does a "
                         "runner that could not start"),
            )
        suites = report.get("testResults") or []
        errored, failed, ran, files = set(), set(), 0, []
        for suite in suites:
            path = self._relpath(suite.get("name") or "")
            files.append(path)
            assertions = suite.get("assertionResults") or []
            # A suite that FAILED and reported no assertion is a file that
            # could not be LOADED. The only portable discriminator, and it is
            # the same on an import error and on a syntax error.
            if suite.get("status") == "failed" and not assertions:
                errored.add(path)
                continue
            for item in assertions:
                status = item.get("status")
                if status == "failed":
                    failed.add(f"{path}::{item.get('fullName', '')}")
                    ran += 1
                elif status == "passed":
                    ran += 1
        files_run = tuple(sorted(files))
        if errored:
            # BEFORE the failure branch. Measured: one good file plus one
            # unloadable file reports three PASSING tests and zero failing
            # ones, so the other order grades a task whose f2p file stopped
            # importing as solved.
            return Outcome(kind=KIND_LOAD_ERROR, exit_code=exit_code,
                           failed_ids=frozenset(failed),
                           errored_files=frozenset(errored),
                           files_run=files_run, explain="a test file did not load")
        if failed:
            return Outcome(kind=KIND_FAILED, exit_code=exit_code,
                           failed_ids=frozenset(failed),
                           errored_files=frozenset(), files_run=files_run,
                           explain="tests ran and failed")
        if ran == 0:
            # `success` is NOT consulted: jest reports `success: true` while
            # exiting 1 on "no test files matched" (measured). And this is
            # where a `-t` matching nothing lands, at exit 0.
            return Outcome(kind=KIND_NOTHING_RAN, exit_code=exit_code,
                           errored_files=frozenset(), files_run=files_run,
                           explain=("no test reached a terminal status -- no "
                                    "file matched, or every selected test was "
                                    "skipped"))
        return Outcome(kind=KIND_PASSED, exit_code=exit_code,
                       errored_files=frozenset(), files_run=files_run,
                       explain="all selected tests passed")
```

`_relpath` strips a leading `/repo/` (the container's `REPO_MOUNT`) and otherwise returns the path unchanged; it must never raise, because a report entry with no `name` is still evidence about the other entries.

The argv builders:

```python
    _NAME_ANCHOR_OPEN = "^(?:"
    _NAME_ANCHOR_CLOSE = ")$"

    def _files(self, node_ids):
        seen, out = set(), []
        for node_id in node_ids:
            path = node_id.partition("::")[0]
            if path not in seen:
                seen.add(path)
                out.append(path)
        return out

    def _names(self, node_ids):
        # The FILE half is thrown away here, and two guards elsewhere are what
        # make that safe: `-t` matches `fullName` and no flag scopes a name
        # pattern to a file, so two tests sharing a name across files are
        # indistinguishable to a selection or a deselection. The loader refuses
        # a manifest whose declared ids collide (`validate_id_set`), and
        # preflight asserts against the real report that no two EXECUTED tests
        # under `tests.paths` share a name -- which is the half a loader cannot
        # see. See the plan's D2.
        return [_js_escape(node_id.partition("::")[2]) for node_id in node_ids]

    def select_args(self, node_ids):
        if not node_ids:
            return []
        return [*self._files(node_ids), "-t",
                "^(?:" + "|".join(self._names(node_ids)) + ")$"]

    def p2p_args(self, *, selected, scope, deselected, ignored):
        # ONE `-t`, always, and never two. Measured 2026-09-02: vitest
        # REJECTS a second occurrence (`Expected a single value for option
        # "-t, --testNamePattern <pattern>", received ["a", "b"]`, exit 1, and
        # no report file), while jest COMMA-JOINS them into `adds,subs` -- a
        # regex matching neither test -- and exits 0 having run nothing. So
        # emitting a selection pattern and a deselection pattern separately is
        # a hard failure on one framework and a silent empty run on the other.
        head = self._files(selected) if selected else list(scope)
        if ignored:
            head += list(self._ignore_prefix)
            head += [f"{self._ignore_flag}={path}" for path in ignored]
        pattern = ""
        if deselected:
            pattern += "(?!(?:" + "|".join(self._names(deselected)) + ")$)"
        if selected:
            pattern += "(?:" + "|".join(self._names(selected)) + ")$"
        if not pattern:
            return head
        return [*head, "-t", "^" + pattern]
```

**`ignored`** maps to `--exclude=<path>` on vitest and `--testPathIgnorePatterns=<path>` on jest — `_ignore_flag` on `_NodeFlavour`, both measured working 2026-09-02. jest additionally carries `_ignore_prefix = ("--testPathIgnorePatterns=/node_modules/",)` and vitest `()`: **jest's flag REPLACES its built-in `/node_modules/` ignore rather than adding to it**, so emitted alone it makes jest collect test files out of `node_modules` — which, with the runners installed at `/node_modules`, means jest's own vendored fixtures. The prefix is emitted only when `ignored` is non-empty, so it never enters an argv that had no reason to carry it. This is preflight's p2p-BEFORE run only, which the grader never makes (the existing `ignore` docstring says so), so the two spellings cannot diverge between a gated and a graded argv.

**`_js_escape`, never `re.escape`.** The pattern is compiled by node, not by Python, and Python escapes characters JavaScript treats as **identity escapes** — an identity escape of a non-syntax character is a `SyntaxError` in a Unicode-mode `RegExp`. Escape exactly the twelve JavaScript syntax characters and nothing else:

```python
_JS_SYNTAX = frozenset(".*+?^${}()|[]\\")


def _js_escape(text: str) -> str:
    """Escape for a JavaScript RegExp, which is not Python's escape set.

    `re.escape` escapes characters JS reads as identity escapes, and an
    identity escape of a non-syntax character is a SyntaxError under the `u`
    flag. A space is the one that would bite first: Python escaped it through
    3.6, and a `\\ ` reaching node is a pattern that means something else.
    """
    return "".join("\\" + ch if ch in _JS_SYNTAX else ch for ch in text)
```

`verify_selected(report, requested, adapter)` returns the requested ids for which the report holds no assertion with status `passed` or `failed`.

- [ ] **Step 5: Run the tests**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_runners.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/runners/node_adapter.py \
        bakeoff/tests/fixtures/node_reports/ bakeoff/tests/test_runners.py
git commit -m "feat: a vitest/jest adapter that reads a JSON report, not an exit code

Measured 2026-09-01: both frameworks exit 1 for a failing test, an unresolvable
import, a syntax error, a nonexistent file argument and a broken config alike,
and both exit 0 when a -t pattern matches nothing. There is nothing in the exit
code to gate on.

Three measured rules the classifier is built around, each of which reads as a
green gate if taken in the wrong order: a report file that was never written is
an environment problem (a config error writes none); a file that failed to LOAD
is a testResults entry with status failed and EMPTY assertionResults, and it
must be checked BEFORE the failing-assertion branch, since a run with one good
file and one unloadable file reports three passing tests and zero failing ones;
and zero assertions with a terminal status is NOTHING RAN whatever the exit code
or jest's `success: true` claims.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 7: the three node-only preflight assertions, and the version bumps

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (three assertions, four evidence keys, `PREFLIGHT_VERSION` +1)
- Modify: `bakeoff/src/bakeoff/grader.py` (the `not_run` environment branch, `GRADER_VERSION` +1), `bakeoff/src/bakeoff/oracle.py` (`ORACLE_VERSION` +1)
- Modify: `bakeoff/scripts/mutation_check.py` (six new anchors)
- Test: `bakeoff/tests/test_preflight.py`, `bakeoff/tests/test_grader.py`

**Interfaces:**
- Consumes: `node_adapter.verify_selected`, `Outcome.files_run`, `Outcome.not_run` from Task 6; `tasks._under`.
- Produces: preflight evidence keys `f2p_before_not_run`, `duplicate_full_names`, `scope_files_run`, `scope_files_outside`, `runner_cache_flags`, `runner_cache_flags_missing`; adapter method `executed_names(report) -> Iterable[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_preflight.py`:

```python
def test_a_declared_f2p_id_that_never_RAN_is_a_problem(monkeypatch):
    """`-t` with a pattern matching no test exits **0** on both vitest and jest
    (measured 2026-09-01), with `Tests 3 skipped (3)`. So a manifest naming a
    renamed test makes an unsatisfiable task read as a green gate, and then as
    a solved run for every arm of it. pytest answers the same input with exit 4
    and needs no such check."""
    result = _preflight_over(_node_container(f2p_ran=False))

    assert not result.ok
    assert any("did not run" in p for p in result.problems)
    assert result.evidence["f2p_before_not_run"] == [
        "tests/a.test.js::does a thing"]


def test_f2p_before_not_run_is_recorded_even_when_everything_ran():
    """Absence is recorded, never implied: "nothing failed to run" and "the
    gate did not look" render identically as a missing key, and a cached
    verdict outlives the code that wrote it."""
    result = _preflight_over(_node_container(f2p_ran=True))

    assert result.evidence["f2p_before_not_run"] == []


def test_two_executed_tests_that_share_a_full_name_are_a_problem():
    """The half the loader cannot see. Its rule covers a collision between two
    DECLARED ids; this one is between a declared id and a test the manifest
    never mentions, which only the real report shows. A quarantine of one then
    silently removes the other from the regression check -- and
    `p2p_deselected` agrees, because two tests really were skipped."""
    result = _preflight_over(_node_container(
        scope_names=[("tests/a.test.js", "works"), ("tests/b.test.js", "works")]
    ))

    assert not result.ok
    assert result.evidence["duplicate_full_names"] == [
        "'works' in tests/a.test.js and tests/b.test.js"]


def test_duplicate_full_names_is_recorded_when_there_are_none():
    result = _preflight_over(_node_container())

    assert result.evidence["duplicate_full_names"] == []


def test_every_new_evidence_key_is_present_on_an_explicit_p2p_task():
    """Three of the four are measured inside `if not tests.p2p:`, which an
    explicit p2p list skips entirely -- so written only where they are
    measured, they would be ABSENT from every explicit-p2p verdict. A key
    present on one task shape and missing on another cannot be read across a
    set of cached verdicts, which outlive the code that wrote them.

    `None` here is "no scoped run was made", not "nothing was found"; the two
    must not render identically."""
    result = _preflight_over(_node_container(p2p=("tests/a.test.js::other",)))

    assert result.evidence["runner_cache_flags"] == ["--no-cache"]
    assert result.evidence["f2p_before_not_run"] == []
    assert result.evidence["duplicate_full_names"] is None
    assert result.evidence["scope_files_run"] is None
    assert result.evidence["scope_files_outside"] is None


def test_the_new_evidence_keys_survive_the_early_return():
    """The runner-gate refusal returns before any container starts. The two
    keys knowable without one are still written; the rest stay `None`."""
    result = _preflight_over(_node_container(runner=("python", "-m", "pytest")))

    assert not result.ok
    assert result.evidence["scope_files_run"] is None


def test_a_scoped_run_that_left_the_declared_paths_is_a_problem():
    """Measured 2026-09-01: `vitest run tests/` matched `/repo/jtests/
    fail.test.cjs` -- the positional is a SUBSTRING FILTER over the absolute
    path, not a path. The scoped p2p run exists to keep the agent's scratch
    files out of the regression check (eight of them in one stored record); a
    filter that over-matches restores exactly what it was added to remove."""
    result = _preflight_over(
        _node_container(scope_files=("tests/a.test.js", "jtests/b.test.cjs"))
    )

    assert not result.ok
    assert result.evidence["scope_files_outside"] == ["jtests/b.test.cjs"]


def test_the_scope_check_matches_by_path_component_not_by_prefix_string():
    """`startswith` is the bug the runner already has. `tests_helpers/x` starts
    with `tests` and is not under `tests/`."""
    from bakeoff.tasks import _under

    assert _under("tests/a.test.js", ("tests/",))
    assert not _under("tests_helpers/a.test.js", ("tests/",))


def test_a_vitest_runner_without_no_cache_is_a_problem():
    """Asymmetric on purpose. For pytest an absent `-p no:cacheprovider`
    writes .pytest_cache/ and preflight's existing dirty-tree check fires
    loudly. For vitest the artifact is node_modules/.vite, and every JavaScript
    repository's .gitignore carries node_modules/ -- so that check is BLIND and
    the flag's absence is invisible. Measured: with `--no-cache` the tree stays
    clean; without it, `?? node_modules/`."""
    result = _preflight_over(
        _node_container(runner=("/node_modules/.bin/vitest", "run"))
    )

    assert not result.ok
    assert any("--no-cache" in p for p in result.problems)
    assert result.evidence["runner_cache_flags"] == ["--no-cache"]


def test_a_pytest_runner_without_no_cacheprovider_is_NOT_newly_refused():
    """Backwards compatibility as a rule: a new assertion on the pytest branch
    could refuse a manifest that loads today, and the failure it would catch
    is already loud."""
    result = _preflight_over(_pytest_container(runner=("python", "-m", "pytest")))

    assert not any("no:cacheprovider" in p for p in result.problems)
```

Append to `bakeoff/tests/test_grader.py`:

```python
def test_an_f2p_id_that_never_ran_is_the_graders_problem_not_the_models():
    """Check 2 restores the test half, so the f2p names in the graded tree are
    the MANIFEST's, not whatever the model wrote. An id that stopped matching
    is therefore an environment fact, and stamping F2P_FAILED for it is an
    accusation the model did not earn -- permanently, in an append-only store."""
    state = _run_node_f2p_with(report_shape="t_nomatch")

    assert state.grade_failure is None
    assert state.not_graded_reason == "environment_error"
    assert "did not run" in state.environment_error
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py tests/test_grader.py -q -k "never_RAN or not_run or scope_files or no_cache or by_path_component"
```

Expected: FAIL — `KeyError: 'f2p_before_not_run'`.

- [ ] **Step 3: The four preflight assertions**

Every key is written **unconditionally**, on every path — including the pytest
one, where the values are `[]`/`None`, and including the paths that never reach
the assertion that measures it. Two absences that render identically as a
missing key are the same defect one layer down, and a preflight verdict is
cached and read by hand long after the code that wrote it.

**That is not free, because three of the four are measured inside `if not
tests.p2p:`.** `duplicate_full_names`, `scope_files_run` and
`scope_files_outside` all come off the scoped p2p run, which does not happen on
a task with an explicit `tests.p2p` — so writing them only where they are
measured makes them **absent** from every explicit-p2p verdict, which is the
exact shape the preamble above forbids and which `image_env_observed` already
solved the same way. So all four are initialised **before the container block**,
beside `evidence["framework"]`, and overwritten only where something was
actually measured:

```python
    # Written here, before any container starts, for the reason
    # `image_env_observed` is: three of these are measured inside the scoped
    # p2p branch, which an explicit `tests.p2p` skips entirely, and a key that
    # is present on one task shape and missing on another cannot be read across
    # a set of cached verdicts. `None` is "not measured on this path"; `[]` is
    # "measured, nothing found".
    evidence["runner_cache_flags"] = []
    evidence["runner_cache_flags_missing"] = []
    evidence["f2p_before_not_run"] = []
    evidence["duplicate_full_names"] = None
    evidence["scope_files_run"] = None
    evidence["scope_files_outside"] = None
```

`runner_cache_flags` and `f2p_before_not_run` start as `[]` rather than `None`
because both are measured on **every** path that reaches them — the cache
flags before the container, the not-run set on every f2p run — so a `None`
there would be a third state nothing can produce.

**a. The cache flags**, beside the existing runner gate (no container needed):

```python
        missing_cache_flags = [
            flag for flag in adapter.no_cache_args
            if flag not in tests.runner
        ]
        evidence["runner_cache_flags"] = list(adapter.no_cache_args)
        evidence["runner_cache_flags_missing"] = missing_cache_flags
        # Both overwrite the initialisation above. Written on the early-return
        # path too -- this block sits beside the runner gate and needs no
        # container -- so a manifest refused for a bad runner still records
        # what the framework wanted.
        if missing_cache_flags and adapter.name != "pytest":
            problems.append(
                "tests.runner is missing "
                + ", ".join(missing_cache_flags)
                + f", which {adapter.name} needs so the suite writes nothing "
                "into the tree. Measured 2026-09-01: vitest creates "
                "<cwd>/node_modules/.vite and `--no-cache` prevents it "
                "entirely. Section 5.6 stages everything, so the artifact "
                "would land in every submission diff -- and unlike pytest's "
                ".pytest_cache this one is INVISIBLE to the dirty-tree check "
                "below, because every JavaScript repository's .gitignore "
                "carries node_modules/."
            )
```

The `adapter.name != "pytest"` guard is the one string comparison in this file and it is deliberate: making it a boolean on the adapter (`cache_flags_required`) would be a second thing to keep in sync with a rule whose whole content is "pytest's failure is already loud, node's is not". Write that sentence in the comment.

**b. Every declared f2p id ran**, immediately after the red-before classification and only when the outcome is not a load error (a file that did not load holds no assertions, so nothing "ran" and the load-error branch already names it):

```python
        not_run = sorted(red_outcome.not_run)
        evidence["f2p_before_not_run"] = not_run
        if not_run and red_outcome.kind != KIND_LOAD_ERROR:
            problems.append(
                "these declared f2p tests did not RUN at the start state: "
                + ", ".join(not_run)
                + ". Measured 2026-09-01 against vitest 3.2.7 and jest 30.5.0: "
                "a `-t` pattern matching no test exits **0** and reports every "
                "test skipped, so a renamed test makes an unsatisfiable task "
                "read as a green gate and then as a solved run for every arm "
                "of it. pytest answers the same input with exit 4 and an "
                "`ERROR: not found:` line, which is why this check has no "
                "pytest equivalent."
            )
```

`Outcome.not_run` is filled by `_Runner.select`, not by `classify` — only the
caller knows what it asked for. `Outcome` is frozen, so `select` rebuilds it:

```python
    def select(self, node_ids: tuple[str, ...]):
        result = self.run(self.adapter.select_args(node_ids))
        self._selected = tuple(node_ids)
        return result

    def classify(self, result) -> "Outcome":
        outcome = self.adapter.classify(
            exit_code=result.exit_code, stdout=result.stdout,
            stderr=result.stderr, report=self.last_report,
        )
        if self._selected and self.last_report is not None:
            missing = verify_selected(
                self.last_report, self._selected, self.adapter)
            if missing:
                outcome = dataclasses.replace(outcome, not_run=missing)
        return outcome
```

`verify_selected` returns an empty frozenset for the pytest adapter (it has no
report to consult), so `not_run` stays empty there and nothing on the pytest
path changes. `pass_to_pass` sets `_selected` to `tuple(tests.p2p)` on the
explicit branch — so a renamed entry in an explicit p2p list is caught by the
same rule, and it is M1's failure exactly as a renamed f2p id is — and to `()`
on the deselect branch, where a leftover value would make a correct scoped run
report the f2p ids as "did not run", which is exactly what a correct p2p run
does. Add `import dataclasses` to `preflight.py`.

**b2. No two EXECUTED tests under the scope share a `fullName`.** Node only, in the same `if not tests.p2p:` block, off the scoped run's report. Its key is initialised to `None` before the container block (Step 3's preamble) precisely because this branch does not always run:

```python
                    seen: dict[str, str] = {}
                    duplicates: list[str] = []
                    for path, name in adapter.executed_names(runner.last_report):
                        first = seen.setdefault(name, path)
                        if first != path:
                            duplicates.append(f"{name!r} in {first} and {path}")
                    # Overwrites the `None` set before the container block.
                    # On an explicit-`tests.p2p` task this branch never runs
                    # and the key stays `None` -- "no scoped run was made",
                    # which is a different fact from "no duplicates were
                    # found", and the two must not render identically.
                    evidence["duplicate_full_names"] = duplicates
                    if duplicates:
                        problems.append(
                            "two or more tests under tests.paths share a full "
                            "name: " + "; ".join(duplicates) + ". "
                            f"{adapter.name} selects and deselects by name "
                            "alone -- `-t` matches fullName and no flag scopes "
                            "a name pattern to a file -- so a quarantine of "
                            "one silently removes the other from the "
                            "regression check, and p2p_deselected AGREES, "
                            "because two tests really were skipped. The loader "
                            "already refuses a collision between two DECLARED "
                            "ids; this is the one it cannot see, between a "
                            "declared id and a test the manifest never "
                            "mentions. Narrow tests.paths, or rename one of "
                            "the titles in the task repo; see "
                            "taskset/HARVESTING.md."
                        )
```

`executed_names(report)` is a new adapter method yielding `(relpath, fullName)` pairs and returning nothing for pytest, whose ids carry the path and have no such hazard — so on a pytest task this branch runs and writes `[]`, while on an explicit-p2p task of any framework the key keeps the `None` it was initialised with. Those are different facts and the plan wants them to look different.

**What an explicit `tests.p2p` therefore does not get, said out loud:** no duplicate-name check and no scope check, because both are properties of the scoped run and that run is not made. That is not a gap this broadening closes — an explicit p2p list selects node ids rather than path prefixes, so the scope hazard does not arise, and the duplicate-name hazard is caught for the *declared* ids by the loader. The residual (a declared id colliding with an undeclared test) is `TASKS.md` follow-up #4.

**c. The scoped run stayed inside `tests.paths`**, in the existing `if not tests.p2p:` block after the scoped run. Its two keys are initialised to `None` before the container block for the same reason b2's is:

```python
                    files_run = scoped_outcome.files_run
                    # Overwrites the `None`s above; stays `None` on the
                    # explicit-p2p branch, which makes no scoped run, and on
                    # pytest, whose adapter reports no file list at all.
                    evidence["scope_files_run"] = (
                        list(files_run) if files_run is not None else None)
                    outside = (
                        [] if files_run is None
                        else [p for p in files_run if not _under(p, scope)])
                    evidence["scope_files_outside"] = outside
                    if outside:
                        problems.append(
                            "the scoped p2p run executed files outside "
                            f"tests.paths ({', '.join(scope)}): "
                            + ", ".join(outside)
                            + ". Measured 2026-09-01: a positional argument is "
                            "a SUBSTRING FILTER over the absolute file path on "
                            "vitest and a REGEX on jest, not a path -- `vitest "
                            "run tests/` matched `/repo/jtests/fail.test.cjs`. "
                            "The scoped run exists to keep the agent's scratch "
                            "files out of the regression check, and a filter "
                            "that over-matches restores exactly what it was "
                            "added to remove. Narrow tests.paths, or rename "
                            "the sibling directory in the task repo."
                        )
```

`_under` is imported from `bakeoff.tasks` — the same component-wise matcher the diff split uses, **never** `startswith`, which is the mistake the runner's own filter already makes.

- [ ] **Step 4: The grader's `not_run` branch**

In `_check_f2p`, **before** the `KIND_PASSED` branch (an id that never ran must not be absorbed by a green report):

```python
    if outcome.not_run:
        # The test half is restored by check 2, so the f2p names in this tree
        # are the MANIFEST's and not whatever the model wrote. An id that
        # stopped matching is therefore an environment fact -- and on node it
        # arrives at exit 0 with a report that reads like a pass, so nothing
        # else would catch it. Stamping F2P_FAILED here would be an accusation
        # the model did not earn, permanently, in an append-only store.
        state.environment(
            "f2p",
            "these declared f2p ids did not run: "
            + ", ".join(sorted(outcome.not_run)),
            result,
        )
```

- [ ] **Step 5: Bump the three versions**

Read each constant, add one, and append a paragraph to its comment saying what a verdict cached under the previous value was written by.

**The bumps land HERE and not in Tasks 1–3, and that is safe on purpose.** Tasks 1–3 are a pure refactor: the argv is byte-identical on both branches, every exit-code branch maps one-for-one onto a kind, and the click task's preflight evidence is diffed against a pre-refactor capture in Task 2 Step 8. So a warm verdict cached under the old constants and served across Tasks 1–3 describes a gate that would return the same verdict — which is the property the version exists to protect, stated over what changed rather than over whether a file was touched. Task 7 is the first commit that changes what a gate *asserts*, so it is the first that must invalidate a cache.

- `PREFLIGHT_VERSION`: *"N adds the runner adapter (broadening 7). A verdict cached under N-1 was written by a gate whose every red/green branch read a pytest exit code, which classifies a vitest or jest run by rules those frameworks do not follow — and which made none of the three node assertions: that every declared f2p id actually ran, that the scoped run stayed inside tests.paths, and that the framework's cache flags are in the runner."*
- `ORACLE_VERSION`: *"N reads an Outcome rather than an exit code. A quarantine cached under N-1 was derived by rules that could not classify a node run at all, and whose 'the quarantine swallowed the whole p2p list' guard depended on pytest's exit 5 — which vitest and jest answer with 0."*
- `GRADER_VERSION`: *"N routes checks 5 and 6 through the runner adapter and adds the `did not run` environment branch. That is a change to what a check MEANS, so the version moves whether or not anything was graded under N-1."*

- [ ] **Step 6: Add eight mutation anchors**

The extracted package has no mutation coverage today, and the regions that moved into it are the ones a mutation is most worth. Each entry is a 6-tuple `(label, file, find, replace, selector, marker)`; `find` must be an **exact substring including indentation**.

| label | file | find → replace | selector |
|---|---|---|---|
| `runners: read a report that was never written as a clean run` | `src/bakeoff/runners/node_adapter.py` | `        if report is None:` → `        if False:` | `tests/test_runners.py -k never_written` |
| `runners: let a passing file hide a file that did not load` | `src/bakeoff/runners/node_adapter.py` | `        if errored:` → `        if False:` | `tests/test_runners.py -k beats_a_passing_file` |
| `runners: read a run that executed nothing as a pass` | `src/bakeoff/runners/node_adapter.py` | `        if ran == 0:` → `        if False:` | `tests/test_runners.py -k matched_nothing_is_nothing_ran` |
| `runners: emit selection and deselection as two -t flags` | `src/bakeoff/runners/node_adapter.py` | `        if not pattern:\n            return head` → `        if True:\n            return head` | `tests/test_runners.py -k ONE_pattern` |
| `preflight: accept an f2p id that never ran` | `src/bakeoff/preflight.py` | `        if not_run and red_outcome.kind != KIND_LOAD_ERROR:` → `        if False:` | `tests/test_preflight.py -k never_RAN` |
| `preflight: grade files the declared scope never named` | `src/bakeoff/preflight.py` | `                    if outside:` → `                    if False:` | `tests/test_preflight.py -k left_the_declared_paths` |
| `preflight: let one quarantine silently deselect two tests` | `src/bakeoff/preflight.py` | `                    if duplicates:` → `                    if False:` | `tests/test_preflight.py -k share_a_full_name` |
| `tasks: accept two declared ids that share a full name` | `src/bakeoff/runners/node_adapter.py` | `            if first != path:` → `            if False:` | `tests/test_tasks.py -k sharing_a_full_name` |

All eight take marker `not integration`.

- [ ] **Step 7: Run everything**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/mutation_check.py
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only
```

Expected: all green; the click task still PASSes and its evidence differs from Task 2's capture only in `preflight_version` and the four new keys, whose values are `["-p", "no:cacheprovider"]`, `[]`, `[]`, `None`/`[]`.

- [ ] **Step 8: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/src/bakeoff/grader.py \
        bakeoff/src/bakeoff/oracle.py bakeoff/scripts/mutation_check.py \
        bakeoff/tests/test_preflight.py bakeoff/tests/test_grader.py
git commit -m "feat: refuse the three node task shapes that read as a green gate

Each of these exits 0, or hides inside one, and none has a pytest analogue.

A -t pattern matching no test exits 0 on vitest and on jest with every test
reported skipped, so a manifest naming a renamed f2p test gates green and then
scores every arm as having solved it. preflight refuses it and the grader calls
it an environment error rather than F2P_FAILED -- check 2 restores the test
half, so the names are the manifest's and the model did not earn the accusation.

A positional argument is a substring filter over the absolute path on vitest and
a regex on jest, so `tests/` matched /repo/jtests/. The scoped p2p run exists to
keep the agent's scratch files out of the regression check; an over-matching
filter restores what it was added to remove.

vitest writes node_modules/.vite into the tree, and every JS repo's .gitignore
hides that from the dirty-tree check -- so --no-cache is asserted for node and
stays advisory for pytest, whose .pytest_cache is already loud.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 8: `GradeRecord.framework`

**Files:**
- Modify: `bakeoff/src/bakeoff/grade_schema.py` (`GradeRecord.framework`, `GRADE_SCHEMA_VERSION`), `bakeoff/src/bakeoff/grader.py` (`_State`, `LadderResult`, `build_grade_record`)
- Test: `bakeoff/tests/test_grader.py`

**Interfaces:**
- Produces: `GradeRecord.framework: str = ""`; `LadderResult.framework: str | None`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_grade_says_which_runner_produced_its_numbers():
    """`p2p_deselected`, `f2p_failed_node_ids` and `p2p_failed_node_ids` all
    have framework-dependent shapes and units. pytest's `p2p_deselected` counts
    deselections it was asked to make; node's counts every test that did not
    run, `it.skip` included. A reader summing across a mixed task set adds
    those together and gets a number that is not a count of anything -- the
    defect `Versions.pricing_basis` exists to prevent one subsystem over."""
    record = _grade_record_for(framework="vitest")

    assert record.framework == "vitest"
    assert record.grade_schema_version.startswith("1.")


def test_a_grade_written_before_the_field_existed_reads_as_unknown():
    """`""`, not `"pytest"`. A default naming a real framework would be this
    field claiming a fact about a record nobody measured."""
    from bakeoff.grade_schema import GradeRecord

    assert GradeRecord.__dataclass_fields__["framework"].default == ""
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py -q -k "which_runner or before_the_field"
```

Expected: FAIL — `KeyError: 'framework'`.

- [ ] **Step 3: Add the field**

In `grade_schema.py`, on `GradeRecord`:

```python
    #: Which runner adapter produced this record's numbers. `""` means the
    #: grade predates the field, never "pytest" -- a default naming a real
    #: framework would be this field claiming a fact nobody measured.
    #:
    #: It is an OBSERVATION rather than configuration echoed back: it names the
    #: adapter that produced `f2p_failed_node_ids`, `p2p_failed_node_ids` and
    #: `p2p_deselected`, whose id SHAPES and whose UNITS differ per framework.
    #: pytest's `p2p_deselected` counts deselections pytest was asked to make;
    #: the node adapters' counts every test that did not run, `it.skip`
    #: included. Summed across a mixed task set without this field, the total
    #: is not a count of anything.
    framework: str = ""
```

Bump `GRADE_SCHEMA_VERSION`'s **minor** by one from its on-disk value (broadening 4 already moves it once), with a comment saying an additive bump moves the version for the same reason `SCHEMA_VERSION` does: a reader that cannot tell versions apart reads an absent field as a positive negative claim.

Thread it: `_State.framework: str | None = None`, set in `run_ladder` from `for_framework(task.tests.framework).name` **before** the first check (so a record refused at check 1 still names it); carried on `LadderResult`; read in `build_grade_record` as `framework=ladder.framework or ""`.

- [ ] **Step 4: Run and commit**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

```bash
git add bakeoff/src/bakeoff/grade_schema.py bakeoff/src/bakeoff/grader.py \
        bakeoff/tests/test_grader.py
git commit -m "feat: a grade names the runner that produced its numbers

p2p_deselected means different things per framework -- pytest counts the
deselections it was asked to make, the node adapters count every test that did
not run, it.skip included -- and f2p_failed_node_ids carries a different id
shape. Summed across a mixed task set with no framework field, the total is not
a count of anything.

"" rather than "pytest" for a grade written before the field existed: a default
naming a real framework is this field claiming a fact nobody measured.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 9: one real vitest task, end to end

Everything before this is unit-tested against captured JSON. This is the first time a node task is built, materialized, gated and graded, and it is the only place the `/node_modules` resolution, the report path and the `--no-cache` flag meet a real container.

**Files:**
- Create: `bakeoff/fixtures/node_task/` — `package.json`, `src/calc.js`, `tests/calc.test.js`, `.gitignore`, `README.md`. **This is the only fixture location**; there is no separate manifest directory, because `task.yaml` needs a `repo.url` and a `base_sha` that only exist once the fixture has been git-initialised.
- Create: `bakeoff/tests/test_integration_node_task.py`
- Test: the file above, marked `integration` **and** `task_image`

**How the task is built, and why not as a static fixture.** `bakeoff/fixtures/smoke_task/` is *not* a task directory — it is a source tree, and `tests/test_preflight.py::_smoke_task` turns it into one at run time: copy the tree into `tmp_path/upstream`, overwrite the buggy file, write the pre-oracle test file, `git init` + commit, read `HEAD`, then emit a `task.yaml` naming that path as `repo.url` and that sha as `base_sha`. A checked-in `task.yaml` cannot do this, because the sha does not exist until the commit does. This task follows the same pattern with a module-scoped `tmp_path_factory` so the git init and the two image builds are paid once.

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: a fixture other integration tests may reuse; nothing importable.

- [ ] **Step 1: Build the fixture repo**

`bakeoff/fixtures/node_task/README.md` says what it is and why the bug is shaped the way it is: an operator swap in `src/calc.js` that preserves byte count, so the fixture also exercises M7's "there is no `.pyc` analogue" claim in a real container rather than only in the probe.

```
bakeoff/fixtures/node_task/
  README.md          -- what this is, and the exact node id its test carries
  package.json       -- {"name":"bakeoff-node-fixture","type":"module","private":true}
  .gitignore         -- node_modules/
  src/calc.js        -- export function add(a, b) { return a - b; }   <- the bug
  tests/calc.test.js -- import { it, expect } from 'vitest';
                        import { add } from '../src/calc.js';
                        it('adds two numbers', () => { expect(add(2, 3)).toBe(5); });
                        it('keeps subtracting elsewhere', () => { expect(3 - 1).toBe(2); });
```

The node id is `tests/calc.test.js::adds two numbers`. The second test is the p2p baseline — without one, a green p2p run is vacuous and the fixture would not exercise check 6.

- [ ] **Step 2: Write the failing integration test**

Create `bakeoff/tests/test_integration_node_task.py`. It follows
`tests/test_integration_grader.py`'s module shape exactly — both markers
module-wide, `REPO_ROOT` off `__file__`, and an explicit `$HOME` cache root
rather than `tmp_path`, because on macOS `tmp_path` resolves under
`/var/folders`, which the Docker VM does not mount, so a repo bind-mounted from
there appears inside the container as a **silently empty directory** and every
assertion compares nothing against nothing and passes. This probe's very first
run hit exactly that.

```python
"""The node path, end to end, in a real container.

Every unit test above this replays a captured JSON report. This file is the
only place four things measured separately on a scratch probe meet each other:
`/node_modules` resolution under a bind mount that replaces `/repo`, the fixed
report path at `/tmp`, `--no-cache`, and the eval user at uid 1000 on a base
image that shipped its own. `smoke_task` exists for the same reason on the
pytest side, and for the same reason it is a fixture rather than a real repo:
what is under test is the harness, not the task.

MARKERS: both, module-wide, like `test_integration_grader.py`'s. `task_image`
is what keeps the section 6.6 logger gate offline -- `verify_logger.py` selects
`-m "integration and not task_image"`.

    cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" \
        tests/test_integration_node_task.py --basetemp="$HOME/.cache/bakeoff-pytest"
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bakeoff.container import RunContainer
from bakeoff.grade_schema import GradeFailure
from bakeoff.images import build_base_image, build_task_image
from bakeoff.preflight import preflight
from bakeoff.tasks import load_task, materialize

pytestmark = [pytest.mark.integration, pytest.mark.task_image]

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "fixtures" / "node_task"
#: An explicit $HOME path, not tmp_path. See the module docstring.
CACHE_ROOT = Path.home() / ".cache" / "bakeoff-node-integration"

#: The one node id this fixture declares, spelled the way the reporter emits
#: it: the file, `::`, then `fullName` -- the enclosing describe titles and the
#: test title joined by single spaces. This fixture has no describe block, so
#: fullName is the title.
F2P_ID = "tests/calc.test.js::adds two numbers"


@pytest.fixture(scope="module")
def task_dir(tmp_path_factory):
    """The fixture tree, git-initialised, plus a task.yaml naming its HEAD.

    The same shape as `test_preflight._smoke_task`, and for the same reason a
    checked-in `task.yaml` cannot replace it: `repo.base_sha` does not exist
    until the commit does. Module-scoped, so the git init and the two image
    builds below are paid once.

    `calc.js` is written here with the BUG (`a - b`), and `tests/calc.test.js`
    is written with only the p2p test -- the f2p test arrives with the test
    half, exactly as it does for a real task, because the start state is
    base_sha plus that half.
    """
    root = tmp_path_factory.mktemp("node-task")
    upstream = root / "upstream"
    shutil.copytree(FIXTURE, upstream)
    (upstream / "src" / "calc.js").write_text(
        "export function add(a, b) { return a - b; }\n")
    (upstream / "tests" / "calc.test.js").write_text(
        "import { it, expect } from 'vitest';\n"
        "it('keeps subtracting elsewhere', () => { expect(3 - 1).toBe(2); });\n")
    for args in (["init", "-q"], ["config", "user.email", "t@t.test"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=upstream, check=True,
                       capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream,
                          check=True, capture_output=True,
                          text=True).stdout.strip()

    task_dir = root / "task"
    task_dir.mkdir()
    (task_dir / "reference.diff").write_text(_REFERENCE)
    (task_dir / "task.yaml").write_text(_manifest(upstream, base))
    return task_dir


@pytest.fixture(scope="module")
def node_image(task_dir):
    base = build_base_image(REPO_ROOT, "node", "22")
    return build_task_image(load_task(task_dir), base,
                            CACHE_ROOT / "build", CACHE_ROOT)


@pytest.fixture
def node_tree(task_dir):
    """A fresh run tree at the start state, removed afterwards.

    `materialize` refuses an existing destination on purpose -- a shared tree
    lets one test start from another's dirty state -- so the rmtree is on the
    way IN as well as out.
    """
    tree = CACHE_ROOT / "tree"
    shutil.rmtree(tree, ignore_errors=True)
    task = load_task(task_dir)
    start_sha = materialize(task, tree / "repo", CACHE_ROOT)
    yield task, tree / "repo", start_sha
    shutil.rmtree(tree, ignore_errors=True)


def test_the_start_state_declares_the_f2p_test_and_the_bug(node_tree):
    """`start_sha` is base_sha PLUS the committed test half, so the f2p test
    exists in the tree the agent gets and the fix does not. Asserted before
    anything is run, because every claim below is about that state."""
    task, repo, _ = node_tree

    assert F2P_ID in task.tests.f2p
    assert "adds two numbers" in (repo / "tests" / "calc.test.js").read_text()
    assert "a - b" in (repo / "src" / "calc.js").read_text()


def test_the_node_fixture_gates_green(node_tree, node_image):
    """The whole claim preflight makes, on the one task shape that has never
    been run end to end: red before the reference fix, green after it, p2p
    green on both sides, a clean tree, and none of the three node refusals
    firing on a task that satisfies them."""
    task, repo, start_sha = node_tree

    result = preflight(task, image=node_image, repo_path=repo,
                       start_sha=start_sha)

    assert result.ok, result.problems
    assert result.evidence["framework"] == "vitest"
    assert result.evidence["f2p_red_kind"] == "failed"
    assert result.evidence["f2p_before_not_run"] == []
    assert result.evidence["scope_files_outside"] == []
    assert result.evidence["runner_cache_flags_missing"] == []
    assert result.evidence["dirty_after_tests"] == ""


def test_the_suite_leaves_no_node_modules_in_the_tree(node_tree, node_image):
    """Measured on the probe: WITHOUT `--no-cache`, vitest creates
    `<cwd>/node_modules/.vite` and `git status` reports `?? node_modules/`.

    The assertion is over the FILESYSTEM rather than over `git status`, and
    that is the point: this fixture's `.gitignore` carries `node_modules/`, as
    every JavaScript repository's does, so the dirty-tree check preflight
    already makes is blind to exactly this artifact.
    """
    task, repo, start_sha = node_tree

    with RunContainer(image=node_image, repo_path=str(repo),
                      base_sha=start_sha) as container:
        container.exec(["timeout", "600", *task.tests.runner, "tests/"])

        assert container.exec(["test", "-e", "/repo/node_modules"]).exit_code != 0
        assert container.exec(
            ["git", "status", "--porcelain"]).stdout.strip() == ""

        # The NEGATIVE half, which is what makes the assertion above mean
        # something: without `--no-cache` the directory appears. A test that
        # only ever saw the flag's presence would stay green if the flag
        # stopped doing anything.
        stripped = [a for a in task.tests.runner if a != "--no-cache"]
        container.exec(["timeout", "600", *stripped, "tests/"])

        assert container.exec(["test", "-e", "/repo/node_modules"]).exit_code == 0
        container.exec(["rm", "-rf", "/repo/node_modules"])


def test_a_fix_at_the_same_byte_count_in_the_same_second_is_seen(
    node_tree, node_image
):
    """There is no `.pyc` analogue here, and this is where that stops being a
    probe result.

    `src/calc.js`'s bug is an operator swap, so the fix preserves byte count,
    and the second run starts inside the same second -- the exact input that
    made pytest serve stale bytecode in this repo's own eval image on
    2026-08-13, feeding section 3.3's self-correction loop the OLD behaviour
    after a correct fix.
    """
    task, repo, start_sha = node_tree
    runner = ["timeout", "600", *task.tests.runner, "-t", "^(?:adds two numbers)$"]

    with RunContainer(image=node_image, repo_path=str(repo),
                      base_sha=start_sha) as container:
        assert container.exec(runner).exit_code == 1

        fixed = "export function add(a, b) { return a + b; }\n"
        (repo / "src" / "calc.js").write_text(fixed)

        assert container.exec(runner).exit_code == 0


def test_the_report_lands_outside_the_tree_and_a_stale_one_cannot_be_read(
    node_tree, node_image
):
    """The report path is FIXED because it is an argv element and the gated
    argv must equal the graded argv. What makes a fixed name safe is the `rm
    -f` `_Runner.run` issues before the measured command -- measured, a config
    error writes no file at all, so a leftover report would stand in as the
    next run's evidence, and a passing one would stand in for a run that never
    happened.
    """
    from bakeoff.preflight import _Runner
    from bakeoff.runners import KIND_ENVIRONMENT, for_framework

    task, repo, start_sha = node_tree
    adapter = for_framework("vitest")
    path = adapter.report_path()

    with RunContainer(image=node_image, repo_path=str(repo),
                      base_sha=start_sha) as container:
        runner = _Runner(container, task.tests.runner, 600, adapter)
        runner.select((F2P_ID,))

        # The report is in the container's /tmp, which is never bind-mounted,
        # so it cannot reach a submission diff.
        assert container.exec(["test", "-e", path]).exit_code == 0
        assert (repo / Path(path).name).exists() is False
        assert container.exec(
            ["git", "status", "--porcelain"]).stdout.strip() == ""

        # A run whose command cannot start must not inherit that report.
        result = runner.run(["--config", "/tmp/does-not-exist.mjs"])
        assert runner.last_report is None
        assert runner.classify(result).kind == KIND_ENVIRONMENT


def test_the_grader_resolves_the_reference_fix(node_tree, node_image):
    """Checks 5 and 6 over a real node submission. The reference diff IS the
    oracle, so if it does not grade `resolved: True` no submission can."""
    from bakeoff.grader import grade_run

    task, repo, start_sha = node_tree
    record = _record_with_diff(task, task.solution_diff)

    grade = grade_run(record, task, image=node_image, cache_root=CACHE_ROOT)

    assert grade.resolved is True, grade.not_graded_detail or grade.grade_failure
    assert grade.framework == "vitest"
    assert grade.f2p_failed_node_ids == ()


def test_an_empty_submission_is_EMPTY_PATCH_and_never_reaches_the_suite(
    node_tree, node_image
):
    """`EMPTY_PATCH` is a GradeFailure rather than a NotGradedReason: the run
    produced turns, cost tokens and changed no file, which is a fact about the
    model and belongs in the denominator."""
    from bakeoff.grader import grade_run

    task, repo, start_sha = node_tree
    record = _record_with_diff(task, "")

    grade = grade_run(record, task, image=node_image, cache_root=CACHE_ROOT)

    assert grade.resolved is False
    assert grade.grade_failure == GradeFailure.EMPTY_PATCH.value
```

`_manifest(upstream, base)` returns the `task.yaml` text: `framework: vitest`,
`paths: ["tests/"]`, `runner: ["/node_modules/.bin/vitest", "run",
"--no-cache"]`, `f2p: [F2P_ID]`, `repo.url` the local `upstream` path and
`repo.base_sha` the commit — no `start_sha`, so the loader computes it (a real
task pins it; a fixture whose upstream is regenerated per module cannot).
`_REFERENCE` is the reference diff: its test half adds `it('adds two numbers',
…)` to `tests/calc.test.js` and its solution half flips `-` to `+` in
`src/calc.js`.

`_record_with_diff(task, diff)` builds a minimal gradable `RunRecord` —
non-zero `turns_used`, `outcome` not `CRASHED`, `artifacts.final_diff = diff`,
a final checkpoint whose `turn == turns_streamed`. Copy
`test_integration_grader.py`'s existing record builder rather than writing a
second one; if it is not already a module-level helper there, lift it into
`tests/conftest.py` in this task and have both files import it.

- [ ] **Step 3: Add the PATH assertion**

The one claim M12 makes that nothing else in the plan pins in a real container:

```python
def test_the_runners_resolve_on_PATH_for_the_commands_the_agent_invents(
    node_tree, node_image
):
    """Section 3.3 measures a loop run with commands THE AGENT INVENTS, and
    `npm install --prefix /` changes no environment -- measured, `command -v
    vitest` finds nothing on the stock base, so a bare `vitest` is exit 127
    while `npx vitest` works. Leaving the spellings inconsistent makes the
    agent discover the rule by failing, inside the turn budget it is scored
    on."""
    task, repo, start_sha = node_tree

    with RunContainer(image=node_image, repo_path=str(repo),
                      base_sha=start_sha) as container:
        assert container.exec(["sh", "-c", "command -v vitest"]).exit_code == 0
        assert container.exec(["sh", "-c", "command -v jest"]).exit_code == 0
```

- [ ] **Step 4: Run it**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_integration_node_task.py -v \
  -m "integration and task_image" --basetemp="$HOME/.cache/bakeoff-pytest"
```

Expected: PASS. **The `--basetemp` under `$HOME` is not optional** — the Docker VM mounts `$HOME` and not `/var/folders`, and a repo bind-mounted from there appears inside the container as a *silently empty directory*, so every assertion compares nothing against nothing and passes. This probe hit exactly that on its first run.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/fixtures/node_task/ bakeoff/tests/test_integration_node_task.py
git commit -m "test: gate and grade a real vitest task in a real container

Every node test above this replays a captured JSON report. /node_modules
resolution, the fixed /tmp report path, --no-cache and the bind mount that
replaces /repo were each measured once on a probe and never together, and the
probe's own first run hit the macOS \$HOME mount trap -- a bind mount from
/private/tmp appears inside the container as a silently empty directory, so
every assertion compared nothing against nothing and passed.

The fixture's bug is an operator swap, so the fix preserves byte count and the
test also pins that node has no stale-bytecode failure to defend against.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 10: docs, and the spec's OPEN-2 row

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`, `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (OPEN-2), `tasks/todo.md`, `TASKS.md`

- [ ] **Step 1: `HARVESTING.md`**

Four edits.

**Layer 1 (`### The loader`)** gains the new refusals: `tests.framework` outside the closed set; a runner argv that does not name the declared framework; a node f2p/p2p id that is not `<file>::<full test name>` or whose file is outside `tests.paths`; `image.node` on a pytest task or `image.python` on a node task; a node version outside `_NODE_VERSIONS`.

**Layer 1 (`### Preflight`)** gains the three node assertions, each with its measurement in one sentence.

**Layer 2 (`### The oracle`)** gains a JavaScript subsection, which is the screening checklist a harvester actually needs:

```markdown
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

      build: ["sh", "-lc", "npm install --prefix / --omit=dev <deps>"]

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
- **No two tests under `tests.paths` may share a full name.** `-t` matches
  `fullName` and knows nothing about which file a test came from, and there is
  no flag that scopes a name pattern to a file. Two `it('works', …)` in two
  files means a quarantine of one silently removes the other from the
  regression check — and `p2p_deselected` agrees, because two tests really were
  skipped. The loader refuses a collision between two *declared* ids; preflight
  refuses a collision between any two *executed* ones. A repository whose suite
  genuinely carries duplicate titles within one scope is **excluded**, unless a
  narrower `tests.paths` separates them. Check before you cut:

      vitest run --reporter=json --outputFile=/tmp/r.json tests/
      node -e 'const r=require("/tmp/r.json"),s=new Map();
        for (const f of r.testResults) for (const a of (f.assertionResults||[]))
          { if (s.has(a.fullName) && s.get(a.fullName)!==f.name)
              console.log("DUP:", a.fullName, s.get(a.fullName), f.name);
            s.set(a.fullName, f.name); }'
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
```

**Layer 2 (`### The oracle`)** also gains, in the JS subsection, the `npm install` vs `npm ci` rule above and the duplicate-full-name exclusion — both are screening decisions a harvester makes before writing a manifest, not manifest keys.

**Layer 2 (`### The image`)** gains the `_NODE_VERSIONS` allowlist table (one row, `22`, measured 2026-09-01, node v22.23.2 / vitest 3.2.7 / jest 30.5.0 / Claude Code 2.1.220) and the three-step rule for adding one.

**`## Screened repositories`** gains a note that no JavaScript repository has been screened yet, so the exclusion list says nothing about them either way — an empty section is not a finding.

- [ ] **Step 2: `docs/BUILDING-A-TASK-SET.md`**

- §1.1 Prerequisites: nothing new (the node base is built by `run_matrix`).
- §3.4 "Measure the f2p node ids — never guess them": add the node recipe beside the pytest one:
  ```bash
  # inside the task image, at the start state
  /node_modules/.bin/vitest run --no-cache --reporter=json \
      --outputFile=/tmp/r.json tests/
  node -e 'const r=require("/tmp/r.json");
    for (const s of r.testResults) for (const a of (s.assertionResults||[]))
      if (a.status==="failed") console.log(s.name.replace("/repo/","")+"::"+a.fullName)'
  ```
  with the sentence that makes it non-optional: *a `-t` pattern matching nothing exits 0 with every test skipped, so an id you typed from the source rather than read from a run is a silent no-op.*
- §3.5 "Write `task.yaml`": add the commented node example, `framework:` and `image.node:` included.
- §10 "Failure modes that do not announce themselves": add four rows — the `-t` silent no-match; `node_modules` erased by the bind mount; `npm ci` at the `/` prefix deleting the pinned runners; and two tests sharing a `fullName`, where a quarantine of one removes both and the deselection count agrees.

- [ ] **Step 3: the click `task.yaml`**

Add, under `tests:`, immediately above `runner:`:

```yaml
  # Which runner adapter reads this suite: pytest (the default), vitest or
  # jest. It decides how "the tests failed" is told from "the environment is
  # broken" -- pytest answers with its exit code, and the node frameworks
  # cannot: measured 2026-09-01, vitest 3.2.7 and jest 30.5.0 both exit 1 for a
  # failing test, an unresolvable import, a syntax error and a broken config
  # alike, so their adapter reads a JSON report instead.
  #
  # framework: pytest
  #
  # A vitest task instead declares, and its f2p ids carry the reporter's
  # fullName after `::` rather than a pytest node path:
  #
  #   framework: vitest
  #   runner: ["/node_modules/.bin/vitest", "run", "--no-cache"]
  #   f2p: ["tests/formatting.test.ts::HelpFormatter writes usage with no args"]
  #
  # and, under `image:`, the node base's major instead of image.python:
  #
  #   node: "22"
```

- [ ] **Step 4: the spec's OPEN-2 row**

Append to OPEN-2's **Notes** cell. The table's columns are `ID | Decision | Blocks | Owner | Notes` — check them before editing rather than trusting this line — and the other four are left untouched, the row **open**:

> **The runner half is decided (2026-09-01, broadening 7).** The harness's executable language scope is Python (pytest) and JavaScript/TypeScript (vitest, jest), selected per task by `tests.framework`; `src/bakeoff/runners/` is the whole of the per-language surface, and a third language means a third adapter and a base image rather than a change to §4.2.1's ladder. Type-check and lint stay manifest-declared `grading.*` argvs, as this row already recorded. **What remains open, and what actually blocks Phase 2, is the repository question** — which Pindrop repos, mono or multi.

- [ ] **Step 5: `tasks/todo.md` and `TASKS.md`**

`tasks/todo.md` gets one section, `## Broadening 7 — a second test runner (vitest/jest)`, following the file's existing per-task shape: what was built, the measurement table from this plan's Measurements section (M1, M2 and M8 at minimum — they are the ones a future reader will otherwise re-derive), the Task 5 Step 6 build output verbatim, and a review paragraph naming what the plan got wrong before any code was written.

`TASKS.md` gets three follow-ups, only these three:

1. **No property-based determinism check for the node frameworks.** Broadening 3 refuses a pytest task whose suite imports `hypothesis` with no `image.env CI`; nothing equivalent exists for `fast-check` or `jest-fuzz`, and the adapter answers `None` to the probe. A node task with a seeded-by-clock property suite gates on a lucky draw, exactly as a pytest one did before broadening 3. P2.
2. **`p2p_deselected`'s units differ per framework and nothing normalizes them.** `GradeRecord.framework` makes the difference *readable*; it does not make the numbers comparable, and the offline view that sums them has to branch. P3.
3. **Only node 22 is in `_NODE_VERSIONS`, and only vitest 3.2.7 / jest 30.5.0 are pinned.** A candidate repo needing another major, or a jest 29 config, is refused at load with an accurate message and no path forward except the three-step growth rule. P3.
4. **A duplicate `fullName` outside `tests.paths` is unguarded.** Preflight's assertion is over the scoped run, so a test outside the scope sharing a name with a declared id cannot collide with a *scoped* quarantine — but the **f2p** run is not scoped: it passes the declared files plus a name pattern, and a same-named test in one of those files is selected too. That is narrower than the scoped hazard (same file set, not the whole repo) and it is caught by the loader only when both are declared. Widening preflight's check to the f2p run's own report is one line and is deliberately not taken here, because it needs a fixture to pin it and this broadening is already ten tasks. P2.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/taskset/HARVESTING.md docs/BUILDING-A-TASK-SET.md \
        bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml \
        docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md \
        tasks/todo.md TASKS.md
git commit -m "docs: what a JavaScript task has to satisfy, and OPEN-2's runner half

HARVESTING gains a JS screening checklist whose every bullet is a measured
failure: a suite that reaches the network fails as a LOAD error and reads as a
broken image; node_modules installed under /repo is erased by the bind mount;
tests/ as a scope prefix also matches jtests/, because a positional argument is
a substring filter rather than a path; and an f2p id typed from the source
rather than read from a run is a silent no-op, since -t matching nothing exits
0 with every test reported skipped.

OPEN-2 stays open. What is decided is the runner half -- Python and JS/TS, one
adapter each -- and the note says so rather than the row being quietly closed
over a repository question nobody answered.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Sentences that belong in `CLAUDE.md`, to be applied on `main` later

`CONTEXT.md` forbids editing `CLAUDE.md` on this branch. These go in when the branch merges.

1. Under **Invariants**, a new bullet:

   > **An exit code is a pytest fact, not a test-runner fact.** The whole reason preflight can tell "the bug is present" from "the environment is broken" is pytest's 1-versus-2/4/5, and that generalizes to nothing: measured 2026-09-01, vitest 3.2.7 and jest 30.5.0 both exit **1** for a failing test, an unresolvable import, a syntax error, a nonexistent file argument and a **broken config file** alike, and both exit **0** when a `-t` pattern matches no test — reporting every test skipped, under a summary that reads like success. So the judgement lives in `src/bakeoff/runners/`, selected by `tests.framework`, and the node adapters read a JSON report written outside `/repo` rather than a number. Three rules in that classifier are order-dependent and each reads as a green gate if taken wrongly: a report file that was never written is an ENVIRONMENT problem (a config error writes none); a file that failed to LOAD — `status: "failed"` with an empty `assertionResults`, the one discriminator both frameworks share — must be checked **before** the failing-assertion branch, because a run with one good file and one unloadable file reports three *passing* tests and zero failing ones; and zero assertions with a terminal status is NOTHING RAN whatever the exit code says and whatever jest's `success: true` claims on "no test files matched".

2. Under **Config gotchas**, four bullets:

   > **`node_modules` cannot be baked at `/repo`, and there is no site-packages to save it.** `pip install -e .` is correct because the install lands outside the tree the bind mount replaces; Node has no such place. Measured: an image that `npm install`s into `/repo/node_modules` reports `node_modules PRESENT` unmounted and `node_modules GONE` with a run tree bind-mounted over it. The runners install at the **container root** instead — `npm install --prefix /` — because Node's resolver walks up from the importing file (`/repo/tests/x` → `/repo/node_modules` → `/node_modules`), which was measured working with `NODE_PATH` **unset**, as uid 1000, with the repo's own `vitest.config.js` still discovered, and with `/node_modules` not writable by the eval user. `NODE_PATH` is deliberately not set: it was not needed and is ignored by ESM resolution and by both frameworks' own resolvers, so setting it would hide which mechanism actually worked.

   > **`-t` matches a test's NAME and knows nothing about its file, and a second `-t` is not "last one wins".** There is no flag on either framework that scopes a name pattern to a file — the file positionals and the pattern are ANDed across the whole run — so two tests sharing a `fullName` across files are indistinguishable to a selection *or* a deselection: a quarantine of one silently removes both, and `p2p_deselected` agrees because two tests really were skipped. The loader refuses a collision between two declared ids and preflight refuses one between any two *executed* tests under `tests.paths`; a repo with duplicate titles in one scope is a screening exclusion. And emitting two `-t` flags is not a workaround: measured 2026-09-02, vitest **rejects** it (`Expected a single value for option "-t, --testNamePattern <pattern>", received ["a", "b"]`, exit 1, no report) while jest **comma-joins** them into a pattern matching neither test and exits **0** having run nothing. Hence one adapter method owning the whole p2p argv.

   > **`npm ci` at the `/` prefix deletes the pinned test runners.** `npm ci`'s documented contract is to remove `node_modules` before installing, and `/node_modules` is where the base image puts vitest and jest. Whether it fires depends on which `package.json`/`package-lock.json` pair npm resolves for the prefix and cwd — measured both ways on 2026-09-02, which is the point: a convention that is right only under an unstated cwd is not a convention. `image.build` uses `npm install --prefix / --omit=dev` (measured additive), and the generated task Dockerfile re-asserts the base's `BAKEOFF_VITEST_VERSION`/`BAKEOFF_JEST_VERSION` after the build steps so a violation fails the **build** rather than reaching the model as exit 127 on every arm. `/node_modules/.bin` is also put on `PATH` by the base image: `npm install --prefix /` changes no environment, so without it a bare `vitest` is 127 while `npx vitest` works — and §3.3 measures a loop run with commands the agent invents.

   > **`node:22-bookworm-slim` already owns uid 1000.** It ships `node:x:1000:1000::/home/node:/bin/bash`, so `useradd --create-home --uid 1000 eval` exits **4** there where it succeeds on `python:3.12-slim-bookworm`; `userdel -r node` must come first. That is the largest reason the node base is a second Dockerfile rather than an `ARG` on the first — a shell conditional around a `useradd` is the shape that half-succeeds and leaves an image running as root, and Claude Code refuses `bypassPermissions` under root and exits before emitting a single event.

3. Under the **stale `.pyc`** bullet, one appended sentence:

   > There is no node analogue, and that is measured rather than assumed: overwriting a source with its fix at the **same byte count inside the same second** and re-running went green on both vitest 3.2.7 and jest 30.5.0 (`node_modules/.vite` is vite's *dependency* optimiser cache, not a source-transform cache, and jest's cache is content-hash keyed). The `--no-cache` a vitest task carries in `tests.runner` is there so the suite writes nothing into the **tree** — §5.6 stages everything, and every JS repo's `.gitignore` hides `node_modules/` from preflight's dirty-tree check — never for staleness.

4. Under **Docs**, add `bakeoff/src/bakeoff/runners/` to the table with the role *"the runner adapters: what 'the tests failed' means per framework, and the measured exit-code and JSON-report tables behind each answer."*
