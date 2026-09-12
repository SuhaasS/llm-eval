# Round 2 item 12 — a property-based determinism check for the node frameworks

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the node half of the gate broadening 3 closed for pytest — a
task whose declared test paths run a property-based suite whose seed nobody
pinned must not be certified on whichever draw preflight happened to get.

**The honest shape is a REFUSAL, not a lever.** Broadening 3 could add
`image.env: {CI: "1"}` because hypothesis reads that variable. Measured
2026-09-02 (below), **fast-check reads no environment variable at all**, and
the one external lever that does work — `NODE_OPTIONS` preloading a module
that calls `fc.configureGlobal({seed})` — works on vitest and is **defeated by
jest's module registry**. A lever that silently does nothing on one of two
peer frameworks is exactly the failure mode this repo's `image.env` read-back
exists to catch, so this plan does not invent one. It refuses, and it says so
plainly.

**Architecture:** One new adapter method (`RunnerAdapter.property_scan`),
one small frozen dataclass (`PropertyScan`), two `rg` probes in `preflight`
scoped to the p2p-before sweep's own `Outcome.files_run` and sharing the
hypothesis probe's exit-code trichotomy, three new evidence keys, one
`PREFLIGHT_VERSION` bump (read-and-add-one). Nothing enters `RunRecord`,
`GradeRecord`, `_IMAGE_ENV_ALLOWED`, the image, or the grader.

**Tech Stack:** Python 3.12; pytest (worktree venv); node base image
`bakeoff-eval-agent:base-node-22` (node 22, vitest 3.2.7, jest 30.5.0,
ripgrep 13.0.0); fast-check 3.23.2 and 2.25.0; Docker 29.5.2 (integration
leg only).

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md`
(§3.3 the loop being measured, §3.7 the task record, §5.4 arm parity, §6.4
confounds) and `docs/superpowers/specs/2026-08-17-offline-grader-design.md`
(checks 5 and 6, which consume the p2p verdict this gate certifies).
Predecessor: `docs/superpowers/plans/2026-09-01-broaden-3-hypothesis.md`.
Candidate rules: `bakeoff/taskset/HARVESTING.md`.

---

## 1. The measured defect

### 1.1 The gap as it stands

`bakeoff/src/bakeoff/runners/node_adapter.py`'s `hypothesis_interpreter`
returns `None` for both node flavours, which makes `preflight` skip the whole
determinism block and leave `hypothesis_importable` and
`hypothesis_imported_by_suite` as `None`. That null is correct and deliberate
— a recorded absence, never a claim — and both that method's docstring and
`bakeoff/tests/test_runners.py::test_the_node_adapters_ask_for_no_hypothesis_probe`
name `TASKS.md` as where the follow-up is tracked. This is that follow-up.

The exposure is not hypothetical. Measured 2026-09-02 against the gated task
`yaml-474-single-newline-empty-value`
(`~/.cache/bakeoff-probe/reports/w7b-yaml-474.md`; tree at
`~/.cache/bakeoff/preflight-tree/yaml-474-single-newline-empty-value/repo`):

- `tests/properties.ts` line 1 is `import * as fc from 'fast-check'`.
- `rg -n configureGlobal` over the entire repository matches **nothing** — the
  suite pins no seed anywhere.
- `image.build` installs `fast-check@2.25.0` precisely because the
  p2p-before/after sweep collects that file.

So a real, already-gated node task today runs an unseeded property suite
inside the sweep whose verdict the gate publishes and the grader's p2p check
consumes.

### 1.2 fast-check draws a fresh seed per run — measured

Scratch project `~/.cache/bakeoff-fcprobe`, probe images `bakeoff-fcprobe:latest`
(fast-check 3.23.2) and `bakeoff-fcprobe:v2` (2.25.0), both `FROM
bakeoff-eval-agent:base-node-22`. The property is deliberately flaky —
`fc.assert(fc.property(fc.nat({ max: 99 }), (n) => n !== 7), { numRuns: 70 })`
— chosen so one draw in roughly two finds the counterexample, which is what
makes a per-run seed visible in ten runs instead of ten thousand.

| what | ten fresh runs of unchanged code (exit codes) |
|---|---|
| vitest 3.2.7 + fast-check 3.23.2, no lever | `0 0 0 1 1 0 0 0 0 0` |
| vitest 3.2.7 + fast-check 2.25.0, no lever | `1 1 0 0 1 0 0 0 0 1` |
| jest 30.5.0 + fast-check 3.23.2, no lever | `0 1 1 0 1 0 0 0 1 0` |

The source says why, in both majors:

```
fast-check 3.23.2  lib/check/runner/configuration/QualifiedParameters.js:73
  QualifiedParameters.readSeed = (p) => {
      if (p.seed == null)
          return safeDateNow() ^ (safeMathRandom() * 0x100000000);

fast-check 2.25.0  same file:58
      if (p.seed == null)
          return Date.now() ^ (Math.random() * 0x100000000);
```

Clock XOR `Math.random()`. This is the same shape broadening 3 measured for
hypothesis (`0 0 0 0 1 1 1 1 0 0` unlevered, `1 1 1 1 1 1` under `CI=1`), and
it has the same consequence: the gate's red-before / green-after verdict, and
the grader's p2p verdict after it, are a draw.

`numRuns` does not change the kind of the problem, only its rate: a larger
`numRuns` makes a rare counterexample more likely to be found on **every**
draw and a smaller one less likely on **any**, and neither makes two runs
agree. It is not a lever and is not treated as one.

### 1.3 A failure is reproducible after the fact, and only after the fact

fast-check prints the seed it drew, but only on a failing run — measured,
verbatim:

```
     → Property failed after 64 tests
{ seed: -451355127, path: "63", endOnFailure: true }
Counterexample: [7]
```

Useful for a human debugging a red run. Useless as a gate: a run that passes
prints no seed, so preflight cannot read back which draw it got, and "the
suite was green" is exactly the outcome that has to be distrusted.

### 1.4 There is NO external seed lever that covers both frameworks

Four things were measured, in the node base image, from **outside** the test
files (the harness cannot edit a task repo — a task is a pinned repo plus the
reference diff):

**(a) An environment variable — there is none.** `grep -rl 'process\.env'`
over the whole installed `fast-check` package matches nothing, in 3.23.2 and
in 2.25.0 alike (`grep` exits 1). The package never reads the environment.
There is no `FC_SEED`, and `readSeed` above is the whole story: the seed
comes from `Parameters.seed`, from `fc.configureGlobal({seed})`, or from the
clock.

**(b) A framework seed flag — neither framework has one that reaches
fast-check.** vitest's `--sequence.seed` seeds *test ordering*; nothing in
either CLI is plumbed into fast-check's `QualifiedParameters`.

**(c) `NODE_OPTIONS` preloading `fc.configureGlobal({seed})` — works on
vitest, defeated by jest.** Preload file
`/repo/fcseed.mjs` = `import fc from 'fast-check'; fc.configureGlobal({ seed: 42 });`

| what | ten runs |
|---|---|
| vitest, `NODE_OPTIONS=--import=file:///repo/fcseed.mjs`, seed 42 | `1 1 1 1 1 1 1 1 1 1` |
| vitest, same preload, seed 1 | `0 0 0 0 0` (five runs) |
| jest, `NODE_OPTIONS=--require /repo/fcseed.cjs` | `0 0 1 0 0 0 0 1 1 0` |
| jest, `NODE_OPTIONS=--import=file:///repo/fcseed.mjs` | `0 0 1 1 1 1 1 1 1 0` |
| jest, CJS preload **and** `--runInBand` | `0 1 0 0 1 1 0 0 0 0` |

The vitest rows are a real lever and both polarities were checked, which is
what rules out "the preload merely broke the run": under seed 42 the failure
output reads `{ seed: 42, path: "45", endOnFailure: true }`, and under seed 1
the suite passes ten times out of ten.

The jest rows have a measured cause rather than a guessed one. A diagnostic
test printing `fc.readConfigureGlobal()` from *inside* jest, with the CJS
preload active, answers:

```
IN-TEST readConfigureGlobal={}
```

Jest's module registry hands the test its own instance of `fast-check`; the
preload configured a different one. `--runInBand` does not help, so this is
the registry and not the worker pool.

**(d) An in-registry lever — works on jest, and is an argv.**
`jest --setupFilesAfterEnv /repo/setup.js`, where `setup.js` requires
fast-check and calls `configureGlobal({seed: 42})`, gives
`1 1 1 1 1 1 1 1 1 1`. It is rejected in §3 for the reason broadening 3
already settled: an argv only the harness ever types is not the environment
the agent's own `npm test` runs in.

**Conclusion, and it is the finding of this item:** no environment lever
exists for fast-check, and no single lever covers both node frameworks. The
design is therefore a refusal.

### 1.5 Regexes, measured against the container's own `rg`

ripgrep 13.0.0 in `bakeoff-eval-agent:base-node-22`, twelve fixture files
under `~/.cache/bakeoff-fcprobe/rgfix`, `-q` exit codes:

| fixture | content | import scan | pin scan |
|---|---|---|---|
| `a_esm_ns.ts` | `import * as fc from 'fast-check'` (yaml-474's real line) | 0 | 1 |
| `b_esm_default.ts` | `import fc from "fast-check";` | 0 | 1 |
| `c_named_integration.ts` | `import { test, fc } from '@fast-check/vitest';` | 0 | 1 |
| `d_cjs.js` | `const fc = require('fast-check');` | 0 | 1 |
| `e_dynamic.mjs` | `const fc = await import('fast-check');` | 0 | 1 |
| `f_jestfuzz.js` | `const Fuzz = require('jest-fuzz');` | 0 | 1 |
| `g_jsverify.js` | `import jsc from 'jsverify';` | 0 | 1 |
| `h_none.ts` | imports `yaml` only | **1** | 1 |
| `i_pin_oneline.ts` | `fc.configureGlobal({ seed: 42, numRuns: 100 });` | 0 | **0** |
| `j_pin_multiline.ts` | `configureGlobal({\n numRuns: 100,\n seed: 1234,\n});` | 0 | **0** |
| `k_perassert.ts` | `fc.assert(..., { seed: 7 });` | 0 | 1 |
| `l_decoy_configureGlobal_noseed.ts` | `configureGlobal({ numRuns: 500 })` + a later `const seed = 3;` | 0 | 1 |
| `m_decoy_noseed_plus_perassert.ts` | `configureGlobal({ numRuns: 500 })` **and** `fc.assert(..., { seed: 7 })` in one file | 0 | 1 |
| `n_pin_with_paren_in_call.ts` | `configureGlobal({ randomType: prand.xorshift128plus(), seed: 42 })` — a real pin | 0 | **0** |
| `o_comment_mention.ts` | `// we used to use 'fast-check' here but removed it`, imports only `yaml` | **0** | 1 |
| `p_setupfile_pin_elsewhere.ts` | imports fast-check, uses `fc.assert`, pin lives in a setup file out of scope | 0 | 1 |
| `q_pin_nested_object.ts` | `configureGlobal({ examples: [{ a: 1 }], seed: 42 })` — a real pin | 0 | **1 (miss)** |
| `r_pin_paren_and_nested.ts` | a call **and** a nested object before `seed:` — a real pin | 0 | **1 (miss)** |
| `s_decoy_close_brace_split.ts` | `configureGlobal({\n numRuns: 500\n}\n);` then a per-assert `{ seed: 7 }` | 0 | 1 |

**Seventeen of nineteen, and the two misses are the point.** The pin column is
measured against the final pattern `configureGlobal\s*\(\s*\{[^{}]*?\bseed\s*:`.
It misses `q` and `r` — real suite-wide pins whose options object contains a
nested **object literal**, which `[^{}]` cannot cross. Both misses are **false
refusals**, and the exclusion remedy clears them.

**Three bounds were measured, and the count is the wrong metric** (review 2,
C1). The first draft used `[^)]*?`; review 2 added `q` and `r` and showed both
bounds score 16/18 on that set, each missing a different real pin. A third
bound was then measured here — `(?:[^}]|\}[^)])*?`, "no `})` between", which
needs no PCRE2 and works on rg's default engine — and it scores **18/18** on
review 2's set:

| fixture | correct | `[^)]` | `[^{}]` (chosen) | `(?:[^}]|\}[^)])` |
|---|---|---|---|---|
| `i_pin_oneline`, `j_pin_multiline` | match | 0 | 0 | 0 |
| `n_pin_with_paren_in_call` | match | **1 miss** | 0 | 0 |
| `q_pin_nested_object` | match | 0 | **1 miss** | 0 |
| `r_pin_paren_and_nested` | match | **1 miss** | **1 miss** | 0 |
| `k`, `l`, `m`, `o`, `p` + every import fixture | no match | 1 | 1 | 1 |
| **`s_decoy_close_brace_split`** | **no match** | 1 | 1 | **0 — FALSE ACCEPT** |

Fixture `s` is why the 18/18 bound is rejected. Its `configureGlobal({…}\n)`
closes with a newline between the brace and the paren, so "no `})` between"
does not fire, the match runs on into the per-assert `{ seed: 7 }`, and the
scan reports a suite-wide pin that does not exist — measured, `rg -o` returns
`configureGlobal({\n  numRuns: 500\n}`. That is a **silent false accept**: the
gate would certify a sweep that is a coin flip, which is the whole defect this
item exists to close.

So the choice is by failure **kind**, not by score. `[^{}]` cannot cross a
brace, so it can never join two different calls — **every one of its misses is
a false refusal, and it produced zero false accepts across all nineteen
fixtures**. "Prefer a loud failure over a plausible-looking zero" decides it.
Rows `k`, `l`, `m` and `s` are the ones that matter on the accept side; rows
`o` and `p` are measured false refusals, both loud, both named in §7.

### 1.6 The remedy, measured on the real task — and it is not one line

Review 1's ruling turns on the refusal being clearable. It is, and the exact
form matters, because the obvious spelling **destroys the run**. Measured
2026-09-02 in `bakeoff-task-yaml-474-single-newline-empty-value:v1` against the
task's own start-state tree, `runner = /node_modules/.bin/jest --config
config/jest.config.js`:

| variant | result |
|---|---|
| **A** baseline sweep | 25 suites, **3,497 tests**, 1 failed (the f2p test at the start state); `tests/properties.ts` **present** in `testResults` |
| **B** `--testPathIgnorePatterns=tests/properties\.ts` alone | **NO REPORT.** The run dies in `tests/json-test-suite/parsers/test_json.js:10` at `process.exit(1)` |
| **C** the full list re-emitted — `/node_modules/`, `tests/_utils`, `tests/json-test-suite/`, `tests/properties\.ts` | 24 suites, **3,496 tests**, 1 failed; `properties.ts` **absent**; no `node_modules` suite |

**B is the trap.** jest's `--testPathIgnorePatterns` on the CLI **replaces**
the config's list *and* jest's built-in `/node_modules/` rule — the same
behaviour `node_adapter._ignore_prefix` already exists to compensate for on
preflight's own ignore argv. yaml's `config/jest.config.js` declares
`testPathIgnorePatterns: ['tests/_utils', 'tests/json-test-suite/']`, so B
un-ignores the vendored JSON-test-suite fixtures, jest collects a plain script
that calls `process.exit(1)` — line 10, inside its `catch`: under jest
`process.argv[2]` is undefined, so its `readFileSync` throws and the catch
fires (the `process.exit(0)` on line 13 is never reached) — and the runner
exits having written no report at all. That is `KIND_ENVIRONMENT` in the adapter and a NO-GO whose message names
the missing report, not the flag — a harvester following a "one-line" remedy
would lose an afternoon to it.

So every place this remedy is written — the refusal message, `HARVESTING.md`,
the negative integration step — writes **C**: the framework's existing ignore
entries **and** its built-in default, re-emitted alongside the new one. The
cost of the remedy on this task is exactly **one test of 3,497**.

The vitest analogue is `--exclude`, and it is **not measured here** — no vitest
task exists to measure it against. `HARVESTING.md` says so in those words
rather than asserting a symmetry; vitest's `--exclude` is documented to replace
its defaults too, and a harvester cutting the first vitest property task must
measure it before relying on the parallel.

## 2. Design

### 2.1 What is added

One method on the adapter protocol, answered by the framework rather than
branched on in `preflight` — the same shape `hypothesis_interpreter` uses,
and for the same reason: the question "how is a property suite spelled in
this ecosystem" is not a fact `preflight` should hold.

```python
# bakeoff/src/bakeoff/runners/__init__.py, beside the protocol
@dataclass(frozen=True)
class PropertyScan:
    """How to recognise a property-based suite, and a pinned seed in it."""
    import_pattern: str
    pin_pattern: str
    frameworks: tuple[str, ...]
```

`RunnerAdapter.property_scan(self) -> PropertyScan | None`.
`PytestAdapter.property_scan` returns `None` (the hypothesis path above it
already covers Python, and folding two working gates into one is a rewrite
this item does not need). `_NodeFlavour.property_scan` returns the shared
module-level `_NODE_PROPERTY_SCAN` — shared because vitest and jest disagree
about nothing here; both resolve the same npm packages by the same specifier
strings.

Three evidence keys — `property_framework_imported_by_suite`,
`property_framework_seed_pinned` and `property_scan_files`. The third exists
because the scope is now **derived from a run** rather than read from the
manifest: a reader of a cached verdict cannot otherwise tell which files the
answer was computed over, and "the sweep ran 25 files" versus "the sweep ran
one" is the difference between a meaningful `false` and a vacuous one.

### 2.2 The two probes, and why two

Exactly the pytest probe's shape: the first says *is this a property suite*,
the second says *is it pinned*, and only the second answering "no" after the
first answering "yes" is a refusal. One probe would refuse on either half
alone and be wrong both times — a repo that pins its seed would be refused,
and a repo that merely has `fast-check` in `package.json` without any swept
file importing it would be refused over a dependency it does not exercise.

The second probe is **not run** when the first says no. `pinned` stays `None`
and that null is unambiguous when read beside its partner: `imported: false`
means the pin was never asked about, `imported: true` with `pinned: null`
means it was asked and could not be answered, and `imported: null` alongside
a non-null `property_scan_files` means the scan ran and rg did not answer,
while `imported: null` with `property_scan_files: null` means there was
nothing to scan or nothing to scan it from. The trio is jointly readable,
which is what this repo's null rule asks for — no one of them is legible
alone.

### 2.3 Scope: the p2p-before run's `files_run` — what the sweep actually ran

**Revised in review 1.** The first draft scanned `tests.paths`. That scans
something other than what the gate certifies, and review 1's ruling is adopted.

What the gate's verdict is made of, measured: with `tests.p2p: []`,
`_Runner.pass_to_pass` takes the deselect branch and `p2p_args` gets
`head = list(scope) == []`, so the p2p-before sweep is **rootdir-wide**. On
`yaml-474-single-newline-empty-value` that sweep loaded **25 suites and 3,497
tests**, `tests/properties.ts` among them (measured 2026-09-02 in
`bakeoff-task-yaml-474-single-newline-empty-value:v1`), while `tests.paths` is
the single file `tests/doc/stringify.ts`. A scan over `tests.paths` therefore
answers a question about a file the certified verdict barely depends on, and
says nothing about the 24 others it does.

So the scan runs over **`Outcome.files_run` from the p2p-before run**, filtered
through `_present`:

- `files_run` is the rootdir-relative set of every `testResults` entry — every
  file the run loaded or executed, an unloadable one included
  (`runners/__init__.py:77-86`, filled at `node_adapter.py:326-354`).
- It is `None` for pytest, so the check stays node-only **by construction**,
  with no framework branch in `preflight`.
- `preflight` already reads the same field for the scoped run
  (`preflight.py:1937-1942`, `evidence["scope_files_run"]`), so this is an
  established shape rather than a new one.
- It costs no extra container work: the p2p-before run happens anyway and is
  already classified one line later.
- Still filtered through `_present`, because `_relpath` "never raises" and a
  report `name` that is absolute or mangled would make `rg` exit 2 — which
  must read as "could not answer", not "no match". One `test -e` per swept
  file is nothing beside the sweep that produced the list.

**The property this scope has and `tests.paths` did not: the refusal has a
remedy, and the remedy IS the fix.** Excluding the property file from the
sweep takes it out of `files_run`, which clears the refusal — and the
certified verdict genuinely stops depending on a draw. Measured on yaml-474
(§1.6): the sweep goes from 25 suites / 3,497 tests to 24 / 3,496, one test
lost, `properties.ts` gone from `files_run`, and the run still reports its one
genuine start-state failure.

A rootdir `rg` — the candidate the first draft rejected — has the opposite
property and is rejected for a stronger reason than the draft gave: excluding
the file from the sweep leaves it **on disk**, so the grep still matches and
**no author action clears the refusal**. See §2.5 F.

### 2.4 The refusal has one escape hatch, and its weakness is stated

If the declared scope itself pins the seed (`configureGlobal(... seed: ...)`),
the task is accepted. This is deliberately weaker than the pytest side's
`image.env: {CI: "1"}`, and the asymmetry is named in code rather than
papered over: `image.env` is *verified against the container*
(`image_env_observed` / `image_env_mismatch`), whereas a `configureGlobal`
match proves only that the call **appears in a scanned file** — not that it
executes, not that it runs before every property, and not that a
`describe`-local `fc.assert` overrides it. It is a signal that the repository
has thought about the problem, and the comment beside it says exactly that
much and no more.

**A pin that lives outside what ran is not seen.** The idiomatic home for
`fc.configureGlobal({seed})` is a framework setup file (vitest `setupFiles`,
jest `setupFilesAfterEnv`), which appears in neither `files_run` nor
`tests.paths` — measured as fixture `p_setupfile_pin_elsewhere.ts`, which the
scan refuses. That is a **false refusal on the best-behaved repository this
check can meet**, it is the loud direction, and the exclusion remedy still
clears it. Review 1 proposed a fourth remedy — "add the setup file to
`tests.paths`" — and it is **disputed on a measurement** (§8, finding 8): a
non-test file in `tests.paths` becomes a positional the framework collects as
a suite, and jest fails it ("Your test suite must contain at least one test":
measured, `2 failed, 2 total` where only one failure is real). The case is
named in §7 and filed as a `TASKS.md` follow-up instead of being papered over
with a remedy that breaks the scoped run.

### 2.5 Alternatives rejected

| # | alternative | why not |
|---|---|---|
| A | `image.env: {NODE_OPTIONS: "--import=file:///opt/fcseed.mjs"}` with an image-built preload calling `configureGlobal({seed})` | Measured to work on vitest (10/10 stable, both polarities) and to be **defeated by jest's registry** (`readConfigureGlobal()` is `{}` inside the test; `--runInBand` too). A lever that silently does nothing on one of two peer frameworks is the precise failure `image.env`'s read-back exists to catch — and preflight *cannot* read this one back, because fast-check prints its seed only on a failing run. `NODE_OPTIONS` in the allowlist is also `--require anything`, inherited by the agent's own `claude` node process, which is the `CLAUDE_CODE_USE_BEDROCK`-shaped hole the allowlist exists to keep closed. |
| B | `tests.runner: [..., "--setupFilesAfterEnv", "/opt/seed.js"]` | Measured to work on jest (10/10 stable). Rejected for broadening 3's own stated reason: it is an argv **only the harness ever types**. The agent is handed `prompt` and nothing else, so its own `npm test` / `npx jest` runs against a different oracle than the gate — §3.3's self-correction loop then corrects away from the answer the gate will grade. Also vitest has no equivalent flag, so this is a jest-only lever. |
| C | Patch or shim `fast-check` at `image.build` so its default seed is fixed | Manufactures a modified dependency. The suite the model is scored on is then not the repository's suite (§6.4), and nothing in the record would say so. |
| D | Behavioural check — run the property scope twice and refuse on disagreement | Two draws of a suite that passes 90% of the time agree 81% of the time, so a weak test would read as a strong one. It also doubles the cost of the slowest part of the gate to buy that. |
| E | `rg -o` to name the matched framework in the refusal | Adds output parsing (and its own failure modes) for a string the author already has in front of them. `-q`'s exit-code trichotomy — 0 match / 1 no match / anything else could-not-answer — is the whole reason the pytest probe has the shape it has. |
| F | Scan the whole rootdir with `rg` | Rejected, and for a stronger reason than "it refuses a real task": **no author action clears it.** Excluding the property file from the sweep (`--testPathIgnorePatterns` / `--exclude`) or declaring `tests.p2p` leaves the file **on disk**, so a rootdir grep still matches and the refusal is permanent. A refusal nothing can clear is not a gate, it is a ban. `files_run` scoping (§2.3) is the form of this candidate that keeps a remedy. |
| F2 | Scan `tests.paths` (the first draft's rule) | Rejected in review 1. It does not scan what the gate certifies — measured on yaml-474, the p2p-before sweep loads 25 suites while `tests.paths` is one file — and its only remedy ("narrow `tests.paths`") removes the file from the **scan** while leaving it in the **sweep**, which is a remedy that silences the check without changing the verdict. Parity with the pytest probe is also weaker than it looks: the hypothesis remedy is a Dockerfile `ENV` that reaches **every** process, so a task declaring `CI=1` for an in-scope import also determinises a hypothesis file outside `tests.paths`. The node side has no lever and therefore no spillover, so the same scope leaves a strictly larger hole. |
| G | A manifest acknowledgement key (`tests.property_seed_pinned: true`) | A rubber stamp. A key whose only content is "I checked" is configuration reported as observation — the thing this repo's fourth invariant forbids. |
| H | Fold the hypothesis probe and this one into one generic `property_scan` covering pytest too | A rewrite of a shipped, gated, mutation-anchored path to save one `if`. The pytest side's trigger is an import **plus** a declared remedy that is verified in the container; the node side has no remedy to verify. They are not the same check. |

---

## 3. Files and exact changes

### 3.1 `bakeoff/src/bakeoff/runners/__init__.py`

- [ ] Add the frozen dataclass `PropertyScan` with fields `import_pattern: str`,
      `pin_pattern: str`, `frameworks: tuple[str, ...]`, placed **above** the
      `RunnerAdapter` protocol (the protocol references the type).
      Docstring carries the why: what a match proves and what it does not.
- [ ] Add to the `RunnerAdapter` protocol, directly after
      `hypothesis_interpreter`:

```python
    def property_scan(self) -> PropertyScan | None:
        """How to recognise a property-based suite in this ecosystem.

        Consumed by preflight over the p2p-BEFORE run's `Outcome.files_run`
        -- what the certified sweep actually loaded -- rather than over
        `tests.paths`, which with an empty `tests.p2p` is a small subset of it.

        `None` from an adapter whose determinism question is answered
        elsewhere -- pytest's is `hypothesis_interpreter` above, whose remedy
        (`image.env: {CI: "1"}`) is a lever this one has no analogue for. It
        would also have no scope: `files_run` is `None` for pytest.
        Measured 2026-09-02 against fast-check 3.23.2 and 2.25.0: the package
        reads no environment variable at all and `readSeed` falls back to
        `Date.now() ^ (Math.random() * 0x100000000)`, so there is nothing to
        declare and the node check is a refusal rather than a lever.
        """
```

### 3.2 `bakeoff/src/bakeoff/runners/pytest_adapter.py`

- [ ] Add, after `hypothesis_interpreter`:

```python
    def property_scan(self):
        # `None`, and NOT because pytest has no property-based suites -- it is
        # the ecosystem this whole check was built for. `hypothesis_interpreter`
        # above already answers it, with a stronger instrument: its remedy is
        # `image.env: {CI: "1"}`, which preflight reads back OUT of the
        # container (`image_env_observed`). The node scan has no lever to read
        # back, so the two are different checks and folding them would weaken
        # the one that works.
        return None
```

### 3.3 `bakeoff/src/bakeoff/runners/node_adapter.py`

- [ ] Add module-level constants **above** `_NodeFlavour`, with the measured
      annotation. Written out verbatim — the implementer transcribes:

```python
#: The npm specifiers that mean "this file drives a property-based suite".
#: Matched as a QUOTED MODULE SPECIFIER rather than anchored to an import
#: statement the way pytest's `^\s*(from|import)\s+hypothesis\b` is: JavaScript
#: has four spellings that all reach the same package -- `import fc from`,
#: `import * as fc from`, `import { fc } from`, `const fc = require(...)` and
#: `await import(...)` -- and `require` is an expression that can appear
#: anywhere on a line, so an anchored pattern would miss the two commonest
#: forms. The quoted specifier is the one shape all of them share.
#:
#: Measured 2026-09-02 against ripgrep 13.0.0 in bakeoff-eval-agent:base-node-22
#: over twelve fixtures: all seven import spellings match, a file importing
#: only `yaml` does not. `@fast-check/<pkg>` covers the official vitest and
#: jest integrations, which re-export fast-check and share its seed.
_PROPERTY_IMPORT_PATTERN = (
    r"""['"](@fast-check/[A-Za-z0-9._-]+|fast-check|jest-fuzz|jsverify)['"]"""
)

#: A SUITE-WIDE seed pin. `[^{}]*?` rather than `.*?`: a negated class matches
#: newlines, so this spans a multi-line `configureGlobal({\n seed: 1234,\n})`
#: under `rg -U` while the brace bound keeps it inside the one options object --
#: a `.*?` would happily pair a `configureGlobal(` here with a `seed:` two
#: hundred lines away.
#:
#: BRACES, NOT PARENTHESES, and it was measured both ways. `[^)]*?` misses a
#: genuine pin whose options carry a nested CALL --
#: `configureGlobal({ randomType: prand.xorshift128plus(), seed: 42 })`, a
#: supported fast-check shape -- and refusing a task whose suite is already
#: deterministic is the cost. `[^{}]` matches that one and still refuses the
#: case the bound exists for: an unseeded `configureGlobal({ numRuns: 500 })`
#: in the same file as a per-assert `fc.assert(..., { seed: 7 })`, because the
#: text between them contains a `{`. Measured 2026-09-02 over sixteen fixtures:
#: 16/16 for `[^{}]`, 15/16 for `[^)]`.
#:
#: A per-call seed is deliberately not a pin: seeding one assertion is not a
#: claim about the file. The residual miss is a pin whose options object
#: contains a nested OBJECT literal, which the brace bound cannot cross -- a
#: false refusal, which is the loud direction.
_PROPERTY_PIN_PATTERN = r"configureGlobal\s*\(\s*\{[^{}]*?\bseed\s*:"

#: Shared by both flavours because they disagree about nothing here: vitest and
#: jest resolve the same npm packages by the same specifier strings, and a
#: per-flavour copy would be two places for one fact to drift.
_NODE_PROPERTY_SCAN = PropertyScan(
    import_pattern=_PROPERTY_IMPORT_PATTERN,
    pin_pattern=_PROPERTY_PIN_PATTERN,
    frameworks=("fast-check", "@fast-check/*", "jest-fuzz", "jsverify"),
)
```

- [ ] Import `PropertyScan` from `bakeoff.runners` at the top of the module
      (it already imports `Outcome` and the `KIND_*` names from there — extend
      that import; do **not** add a second import line).
- [ ] Add `property_scan` to `_NodeFlavour`, directly after
      `hypothesis_interpreter`:

```python
    def property_scan(self):
        return _NODE_PROPERTY_SCAN
```

- [ ] **Rewrite the `hypothesis_interpreter` comment.** Its last sentence is
      now false. Replace the sentence beginning "The JavaScript property-based
      libraries (fast-check, jest-fuzz) have their own seed mechanisms..."
      with:

```python
        # `None`, so preflight skips the whole block and leaves both evidence
        # keys `None` -- a recorded ABSENCE, never a claim that the suite is
        # deterministic. The JavaScript determinism question is answered by
        # `property_scan` below instead, and it is a REFUSAL rather than a
        # probe-plus-remedy: measured 2026-09-02, fast-check reads no
        # environment variable, so there is no node analogue of CI=1 to
        # declare.
```

### 3.4 `bakeoff/src/bakeoff/preflight.py`

- [ ] **`PREFLIGHT_VERSION`: read the literal in `preflight.py` and write its
      successor.** Do **not** transcribe a number from this plan. It was `"14"`
      at HEAD 8232032, and item 12 lands after items 1, 2, 5, 6, 7, 11 and
      probably 13/14 in the round-2 queue, several of which move it — writing
      `"15"` would move the constant **backwards**, and `preflight_key` would
      then serve verdicts cached under a higher version, which is the exact
      silent defect the bump exists to prevent. Record the observed value and
      the value written in the review log.
- [ ] Beside the existing evidence defaults (currently lines 838-839), add
      three keys:

```python
    #: `None` on the pre-container path, on a pytest task, and on a node run
    #: whose p2p-before sweep wrote no report -- and the trio is what makes the
    #: nulls readable. `imported: false` means the pin was never asked about;
    #: `imported: true` with `pinned: null` means it was asked and rg could not
    #: answer; `imported: null` with `scan_files: null` means there was nothing
    #: to scan or nothing to scan it from. A single key could not tell those
    #: apart, and `scan_files` is what says WHICH files a verdict was computed
    #: over now that the scope is derived from a run rather than declared.
    evidence["property_framework_imported_by_suite"] = None
    evidence["property_framework_seed_pinned"] = None
    evidence["property_scan_files"] = None
```

- [ ] **Bind the p2p-before Outcome.** At `preflight.py:1659` the line reads
      `p2p_green = runner.classify(green).kind == KIND_PASSED`. Split it,
      preserving the comment above it verbatim:

```python
        p2p_before_outcome = runner.classify(green)
        p2p_green = p2p_before_outcome.kind == KIND_PASSED
```

  Behaviour-preserving: `classify` is called exactly once, in the same place,
  and the existing comment ("Classified IMMEDIATELY after its own invocation:
  `classify` reads `_Runner.last_report`, which the next `run` overwrites")
  is now doing double duty and stays where it is.

- [ ] Add the scan block **immediately after** those two lines — after the
      p2p-before run that produces its scope, and before anything else runs.
      Written out; the implementer transcribes:

```python
        # The node half of the same question, and it is a REFUSAL rather than a
        # probe-plus-remedy because there is nothing to declare.
        #
        # Measured 2026-09-02 in bakeoff-eval-agent:base-node-22 against
        # fast-check 3.23.2 and 2.25.0: `QualifiedParameters.readSeed` falls
        # back to `Date.now() ^ (Math.random() * 0x100000000)`, the package
        # reads NO environment variable anywhere (`grep -rl process.env` over
        # the installed tree exits 1), and ten fresh vitest runs of one
        # property over unchanged code gave `0 0 0 1 1 0 0 0 0 0`. So a node
        # property suite gates on whichever draw preflight happened to run.
        #
        # NODE_OPTIONS preloading `fc.configureGlobal({seed})` was measured and
        # rejected: it works on vitest (10/10 stable, both polarities) and is
        # DEFEATED by jest's module registry (the test's own
        # `fc.readConfigureGlobal()` reads `{}`, `--runInBand` included). A
        # lever that silently does nothing on one of two peer frameworks is the
        # failure the image.env read-back above exists to catch -- and this one
        # could not be read back at all, since fast-check prints its seed only
        # on a FAILING run.
        #
        # SCOPED TO THE P2P-BEFORE RUN'S `files_run`, which is the whole design
        # decision. `tests.paths` would scan something other than what this
        # gate certifies: with `tests.p2p: []` the sweep above is rootdir-wide,
        # and measured on yaml-474 it loads 25 suites and 3,497 tests while
        # `tests.paths` is one file. A rootdir `rg` would scan the right
        # question and produce a refusal NO AUTHOR ACTION CLEARS -- excluding
        # the file from the sweep leaves it on disk. `files_run` is the scope
        # where the remedy and the fix are the same action: exclude the file
        # from the sweep, it leaves `files_run`, the refusal clears, and the
        # certified verdict stops depending on a draw.
        #
        # `None` for pytest (`Outcome.files_run`'s contract), so this stays
        # node-only with no framework branch here. `None` also on a node run
        # that wrote no report -- KIND_ENVIRONMENT -- and that case adds no
        # problem of its own, because the missing report is already a NO-GO
        # said better one branch below.
        #
        # `_present` even though the runner just loaded these files: `_relpath`
        # never raises, so a mangled or absolute report `name` would make rg
        # exit 2, which must read as "could not answer" and not as "no match".
        #
        # TWO probes, for the pytest block's reason: the first asks whether the
        # sweep ran a property suite, the second whether it is pinned, and only
        # the second answering no after the first answering yes is refused.
        scan = adapter.property_scan()
        swept = p2p_before_outcome.files_run
        if scan is not None and swept:
            scan_files = _present(container, swept)
            evidence["property_scan_files"] = scan_files
            if scan_files:
                imported = _rg_probe(
                    container, scan.import_pattern, scan_files, problems,
                    "property-framework-import scan (rg over the p2p-before "
                    "run's files_run)",
                    "silently reading it as 'no property framework' would "
                    "disarm the one check that catches an unpinned "
                    "property-based suite.",
                )
                evidence["property_framework_imported_by_suite"] = imported

                pinned: bool | None = None
                if imported:
                    # Only asked when the first probe said yes, so `None` here
                    # is "not asked" and is read beside `imported` above. `-U`,
                    # because a real `configureGlobal({\n seed: 1234,\n})`
                    # spans lines and rg is line-based without it.
                    pinned = _rg_probe(
                        container, scan.pin_pattern, scan_files, problems,
                        "property-seed-pin scan (rg over the p2p-before run's "
                        "files_run)",
                        "silently reading it as 'no seed pin' would refuse a "
                        "task whose suite is already deterministic.",
                        multiline=True,
                    )
                evidence["property_framework_seed_pinned"] = pinned

                if imported and not pinned:
                    problems.append(
                        "the p2p-before sweep ran a file importing a "
                        "JavaScript property-based framework ("
                        + ", ".join(scan.frameworks)
                        + ") and nothing it ran pins a seed. Measured "
                        "2026-09-02 against fast-check 3.23.2 and 2.25.0, the "
                        "seed defaults to `Date.now() ^ (Math.random() * "
                        "0x100000000)` and ten fresh runs of one property over "
                        "unchanged code gave `0 0 0 1 1 0 0 0 0 0` -- so the "
                        "p2p verdict this gate publishes, and the grader's "
                        "checks 5 and 6 after it, are a draw. THERE IS NO "
                        "image.env LEVER TO ADD: fast-check reads no "
                        "environment variable at all, and the one external "
                        "lever that works for vitest (NODE_OPTIONS preloading "
                        "a module that calls fc.configureGlobal) is defeated "
                        "by jest's module registry. TWO remedies, and the "
                        "first is the one that actually changes the verdict: "
                        "(1) take the file out of the SWEEP by adding the "
                        "framework's ignore flag to tests.runner -- and "
                        "re-emit the entries it replaces, because a CLI "
                        "--testPathIgnorePatterns REPLACES both the config's "
                        "list and jest's built-in /node_modules/ rule "
                        "(measured: the naive one-flag form killed yaml-474's "
                        "sweep outright, no report written); (2) pick a repo "
                        "whose own suite calls `fc.configureGlobal({seed: "
                        "...})` in a file the sweep runs. Read "
                        "taskset/HARVESTING.md's 'Screening a JavaScript or "
                        "TypeScript repository' first -- a pinned seed makes "
                        "this oracle reproducible, not correct."
                    )
```

- [ ] Add the helper `_rg_probe` beside `_present`, and **refactor the
      hypothesis block to call it**. Three call sites wanting one trichotomy is
      the right time to extract it; the argv it builds with `multiline=False`
      is byte-identical to the existing hypothesis argv.

```python
def _rg_probe(container, pattern: str, scanned: list[str], problems: list[str],
              what: str, consequence: str, *,
              multiline: bool = False) -> bool | None:
    """Three-valued `rg -q` over `scanned`. `None` is "could not answer".

    0 is a match, 1 is no match, and anything else -- an unreadable path, a bad
    pattern, no rg -- is UNKNOWN. A quiet boolean there silently disarms the
    caller, which is the whole reason this is not a `bool`.

    UNKNOWN is not silent either: `scanned` is non-empty at every call site, so
    the probe RAN and did not answer, and that is a controller-ruling ambiguity
    rather than the "never ran" shape an empty `scanned` gives. The two would
    render identically as `None` in `evidence`, so this appends a problem
    naming the argv and the exit code.

    `consequence` is the caller's own closing sentence rather than one sentence
    shared by all three. The hypothesis probe's misreading disarms a check; the
    seed-pin probe's misreading REFUSES a task that was already fine. One
    sentence covering both would be wrong for one of them, and the hypothesis
    caller's text is passed verbatim so that message stays byte-identical --
    `test_the_hypothesis_rg_message_is_byte_identical_after_the_extraction`
    pins it.

    `--` before the paths: an entry starting with `-` would otherwise parse as
    an rg flag, turning a scan target into a silent argv change.

    INHERITED DEFAULTS, stated once here rather than rediscovered per caller:
    `rg` without `--no-ignore` honours `.gitignore`/`.ignore` and skips hidden
    files, so a gitignored test file is not scanned -- the SILENT direction for
    an import probe. Left as-is because it is the behaviour the hypothesis
    probe has always had, and changing it here would change that probe too;
    a change must move both, deliberately.
    """
    argv = ["rg", "-q"] + (["-U"] if multiline else []) + [pattern, "--",
                                                           *scanned]
    probe = container.exec(argv)
    if probe.exit_code == 0:
        return True
    if probe.exit_code == 1:
        return False
    problems.append(
        f"the {what} could not answer: `" + " ".join(argv)
        + f"` exited {probe.exit_code}, "
        "not 0 (match) or 1 (no match). rg exits 2 on an "
        "unreadable path or a bad pattern and it is asserted "
        "present above, so this names an environment problem "
        "preflight cannot see through -- "
        + consequence
    )
    return None
```

  **The hypothesis call site, written out so the message does not move.**
  Replace the existing inline probe with:

```python
                used = _rg_probe(
                    container, r"^\s*(from|import)\s+hypothesis\b", scanned,
                    problems,
                    "hypothesis-import scan (rg over tests.paths)",
                    "silently reading it as 'not imported' would disarm the "
                    "one check that catches an undeclared property-based "
                    "suite.",
                )
```

  This reproduces today's message exactly:

```
the hypothesis-import scan (rg over tests.paths) could not answer: `rg -q ^\s*(from|import)\s+hypothesis\b -- tests/` exited 2, not 0 (match) or 1 (no match). rg exits 2 on an unreadable path or a bad pattern and it is asserted present above, so this names an environment problem preflight cannot see through -- silently reading it as 'not imported' would disarm the one check that catches an undeclared property-based suite.
```

- [ ] **Do not hoist `scanned`.** The first draft hoisted it so two blocks
      could share one `_present` sweep. Under `files_run` scoping the two
      blocks have **different scopes** — the hypothesis probe scans
      `tests.paths`, the property scan scans the sweep's `files_run` — so
      sharing one variable would be wrong. The hypothesis block keeps its own
      `scanned = _present(container, tests.paths)` exactly where it is.

### 3.5 Version constants

| constant | how to set it | why |
|---|---|---|
| `preflight.PREFLIGHT_VERSION` | **Read the literal in `preflight.py` and write its successor.** Never transcribe a number from this plan; record the observed and written values in the review log. | It is a component of `preflight_key`, so a cached GO produced by a gate that never ran this scan must not be served. It was `"14"` at HEAD 8232032, but item 12 lands after several other preflight-touching round-2 items that also move it — a literal here would move the constant **backwards** and silently serve verdicts cached under a higher version, which is the defect the bump exists to prevent. Items 11, 13, 14 and 16 all state the rule this way. |

Deliberately **not** moved, and each for a stated reason:

- `schema.SCHEMA_VERSION` — nothing enters `RunRecord`. Preflight evidence is
  not a record field.
- `grader.GRADER_VERSION` — the grader is untouched; no verdict changes shape.
- `tasks` manifest/digest versioning — no manifest key is added, so
  `manifest_digest` is unchanged and every existing manifest still loads.

---

## 4. Tests, by name, with what each asserts

### 4.1 `bakeoff/tests/test_preflight.py`

First, the fixture changes the new tests need. **Decided here rather than left
to the implementer** (review 1, finding 7): the existing helper is
`_node_container(*, f2p_ran=True, scope_names=None, scope_files=None, p2p=(),
runner=…)` at `tests/test_preflight.py:3123`, it builds the
`_ScriptedContainer` itself at `:3172-3173` with `present=tests.paths`, and it
accepts none of what these tests need. Extend it; do not add a second helper.

- [ ] `_ScriptedContainer.__init__` gains four keyword-only parameters, stored
      as attributes with comments in the file's existing style:
      `property_imported=False`, `property_pinned=False`,
      `property_import_rg_exit=None`, `property_pin_rg_exit=None`.
- [ ] The `rg` branch of `exec` becomes **pattern-dispatched**, because three
      probes now run `rg` and answering them from one boolean would make every
      new test a test of the hypothesis probe:

```python
        if cmd[:1] == ["rg"]:
            # Three probes run rg now, told apart by their PATTERN rather than
            # by position -- the hypothesis one, the property-import one and
            # the seed-pin one. `-U` is on the pin probe's argv only, which is
            # what a test asserting "the pin scan was never asked" reads.
            pattern = " ".join(cmd)
            if "configureGlobal" in pattern:
                if self.property_pin_rg_exit is not None:
                    return _Exec(exit_code=self.property_pin_rg_exit)
                return _Exec(exit_code=0 if self.property_pinned else 1)
            if "fast-check" in pattern:
                if self.property_import_rg_exit is not None:
                    return _Exec(exit_code=self.property_import_rg_exit)
                return _Exec(exit_code=0 if self.property_imported else 1)
            if self.rg_exit is not None:
                return _Exec(exit_code=self.rg_exit)
            return _Exec(exit_code=0 if self.hypothesis_in_suite else 1)
```

- [ ] `_node_container` gains exactly two things — one helper, every existing
      call site unchanged:

```python
def _node_container(*, f2p_ran=True, scope_names=None, scope_files=None,
                    p2p=(), runner=("/node_modules/.bin/vitest", "run",
                                    "--no-cache"),
                    p2p_before_files=(), **container_kwargs):
```

  `p2p_before_files` appends extra file entries (each with one passing test) to
  the **p2p_before report only**, which is what puts a file such as
  `tests/properties.ts` into that run's `files_run` without touching
  `tests.paths`, the f2p report, or the scoped report. `**container_kwargs` is
  forwarded verbatim into the `_ScriptedContainer(...)` call at `:3172-3173`,
  which is how the four new parameters and a `present=()` override reach it —
  note `present=tests.paths` there must become `container_kwargs.pop("present",
  tests.paths)` so an override wins instead of colliding.

**Assertion form, written out so it is transcribed and not invented (review 1,
finding 11):** `container.commands` is `list[list[str]]`, so `"fast-check" in
cmd` is **element** membership and is always false — the pattern is one argv
element containing that substring. Every fast-check assertion below is written
`any("fast-check" in part for part in cmd)`. The `-U` assertions are correct as
`"-U" in cmd`, because `-U` **is** its own argv element.

The tests:

- [ ] `test_a_swept_file_that_imports_fast_check_with_no_seed_pin_is_refused`
      — `_node_container(p2p_before_files=("tests/properties.ts",),
      property_imported=True, property_pinned=False)`. Asserts
      `not result.ok`; `evidence["property_framework_imported_by_suite"] is
      True`; `evidence["property_framework_seed_pinned"] is False`;
      `"tests/properties.ts" in evidence["property_scan_files"]`; and some
      problem contains both `"fast-check"` and `"pins a seed"`. Docstring: the
      gate would otherwise publish a p2p verdict that is a draw, the grader's
      checks 5 and 6 consume it, and there is no lever to add — the measured
      numbers go here.
- [ ] `test_the_scan_reads_the_p2p_before_runs_files_run_not_tests_paths`
      — the load-bearing scope test. `_node_container(p2p_before_files=
      ("tests/properties.ts",), property_imported=True, property_pinned=False)`
      with `tests.paths` left at its default `("tests/",)`. Asserts the recorded
      import-scan argv's trailing paths are the p2p-before run's files
      (`"tests/properties.ts"` among them) and **not** the literal
      `"tests/"` prefix. Docstring: measured on yaml-474, the sweep loads 25
      suites while `tests.paths` is one file — scanning the declared paths
      answers a question about something the certified verdict barely depends
      on.
- [ ] `test_the_scan_runs_after_the_p2p_before_sweep_that_gives_it_its_scope`
      — replaces the first draft's "runs before the suite" test, which no
      longer states anything true. Asserts the index of the first argv with
      `any("fast-check" in part for part in cmd)` is **greater** than the index
      of the p2p-before suite invocation and **less** than the index of the
      **next suite invocation of any kind** — the predicate the file's existing
      ordering tests already use, `cmd[:1] == ["timeout"] and "--co" not in
      cmd` (verified at `tests/test_preflight.py:1825`; `"--co"` excludes the
      bare-runner probe). Bounding on the p2p-**after** run instead would
      permit a placement the rationale forbids, because the f2p-after run sits
      between the two (review 2, C3). Docstring: ordering here is not the
      environment-defect rule (preflight collects problems rather than raising,
      so order carries no verdict) — it is that `classify` reads
      `_Runner.last_report`, which **any** subsequent `_Runner.run` overwrites,
      so the scope must be taken from the Outcome bound at the p2p-before run
      and used before the next suite invocation, whichever one that is.
- [ ] `test_a_swept_file_whose_scope_pins_the_seed_is_accepted`
      — `property_imported=True, property_pinned=True`. Asserts both evidence
      keys are `True` and that **no** problem mentions `"pins a seed"`.
      Docstring: what a `configureGlobal` match proves (the repository has
      thought about it) and what it does not (that it executes, that it covers
      every property) — deliberately weaker than `image.env`, which is read
      back out of the container.
- [ ] `test_a_sweep_with_no_property_import_is_not_asked_about_a_pin`
      — `property_imported=False`. Asserts
      `evidence["property_framework_imported_by_suite"] is False`;
      `evidence["property_framework_seed_pinned"] is None`;
      `evidence["property_scan_files"]` is a non-empty list; and
      `not any("-U" in cmd for cmd in container.commands)`. Docstring: `None`
      here is "not asked", legible only beside its partner keys — which is why
      the three are written as a set.
- [ ] `test_the_property_import_scan_that_could_not_answer_is_None_and_a_NO_GO`
      — `property_import_rg_exit=2`. Asserts `not result.ok`;
      `evidence["property_framework_imported_by_suite"] is None`;
      `evidence["property_framework_seed_pinned"] is None`; a problem
      containing `"property-framework-import scan"` and `"exited 2"`; and that
      no `-U` probe ran. Docstring: a quiet `False` reads as "no property
      framework" and disarms the check on an environment defect preflight can
      see.
- [ ] `test_the_seed_pin_scan_that_could_not_answer_is_None_and_a_NO_GO`
      — `property_imported=True, property_pin_rg_exit=2`. Asserts
      `not result.ok`; `evidence["property_framework_seed_pinned"] is None`;
      a problem containing `"property-seed-pin scan"` and `"exited 2"`. This is
      the case a single shared `rg_exit` cannot express, which is why the fake
      dispatches on the pattern. Docstring must also note the asymmetry the
      `consequence` parameter exists for: misreading this probe REFUSES a task
      that was already fine, which is not what misreading the other two does.
- [ ] `test_the_pin_scan_is_the_only_probe_that_asks_rg_for_multiline`
      — `property_imported=True, property_pinned=True`. Asserts exactly one
      recorded `rg` argv has `"-U" in cmd`, and that it is the one whose parts
      contain `"configureGlobal"`. Docstring: a real
      `configureGlobal({\n seed: 1234\n})` spans lines and rg is line-based
      without `-U`, so dropping it turns a pinned suite into a refused one;
      putting `-U` on the *import* probe would change what that pattern can
      match across file boundaries.
- [ ] `test_a_pytest_task_records_all_three_property_keys_as_absent`
      — the existing pytest `_FakeTask` / `_pytest_container`. Asserts all three
      new keys are `None` and that no recorded argv has
      `any("fast-check" in part for part in cmd)`. Docstring: `files_run` is
      `None` for pytest by the adapter's own contract, so the check is node-only
      by construction rather than by a framework branch — and the null is a
      recorded absence, never a claim that a Python suite has no property
      framework, which the hypothesis probe answers with a stronger instrument.
- [ ] `test_a_sweep_that_wrote_no_report_leaves_the_property_keys_absent`
      — a node container whose `p2p_before` report is `None` (the
      `KIND_ENVIRONMENT` shape: measured, a broken config exits 1 and writes no
      file on both frameworks). Asserts all three new keys are `None`; that no
      `rg` argv mentions fast-check; and that the run is already NO-GO **for
      the missing-report reason**, not for a property one. Docstring: the scan
      adds no problem of its own here, because the missing report is a NO-GO
      said better one branch below — a second message for one cause is how a
      reader learns to skim both.
- [ ] `test_the_property_scan_keys_say_which_absence_on_the_early_return`
      — a task whose `tests.runner` names no framework, so `preflight` returns
      before `RunContainer`. Asserts all three new keys are `None`. Docstring:
      the keys are written on every path, because a key present on one branch
      and absent on another is the same defect one layer down. **A new test,
      not an edit to the existing one** — round-2 item 5 is about that
      function's early-return evidence and the two must not collide.
- [ ] `test_the_hypothesis_rg_message_is_byte_identical_after_the_extraction`
      — the existing hypothesis rg-exit-2 scripting (`_pytest_container`,
      `present=("tests/",)`, `rg_exit=2`). Asserts the problem **equals** the
      full string written out in §3.4, character for character. Docstring: the
      plan is on record wanting this message stable through the `_rg_probe`
      extraction, and nothing pinned it before — the two existing tests assert
      only `"rg" in problem and "exited 2" in problem`, which survives any
      rewording. Review 1 caught the first draft silently rewording two clauses
      of it.

### 4.2 `bakeoff/tests/test_runners.py`

**The conformance gate is free and already exists — do not duplicate it.**
`tests/test_runners.py:50-59` already asserts `isinstance(adapter,
RunnerAdapter)` for every registry entry, and `RunnerAdapter` is
`@runtime_checkable`. Adding `property_scan` to the protocol therefore makes
that test fail loudly for any adapter missing it, at no cost, so **no "every
adapter has the method" test is to be added**. Checked and clean: the ad-hoc
`_Adapter` at `tests/test_preflight.py:2875` is passed to `_Runner`, never to
`preflight`, so it needs no `property_scan`.

- [ ] `test_pytest_asks_for_no_property_scan` — asserts
      `for_framework("pytest").property_scan() is None`. Docstring: not because
      pytest has no property suites, but because `hypothesis_interpreter`
      answers it with a lever preflight can read back out of the container —
      and because `Outcome.files_run` is `None` for pytest, so there would be
      no scope to scan even if it returned one.
- [ ] `test_the_node_adapters_ask_for_a_property_scan` — for both `"vitest"`
      and `"jest"`, asserts `property_scan()` is not `None`, that the two
      adapters return the **same object** (one fact, one place), and that
      `frameworks` contains `"fast-check"`, `"jest-fuzz"` and `"jsverify"`.
- [ ] `test_the_property_patterns_match_the_measured_spellings`
      — parametrized over the **nineteen** §1.5 fixture strings, using Python's
      `re`. Asserts the import-matching spellings match `import_pattern`
      (`o_comment_mention` among them — the over-match is deliberate and
      pinned, not tolerated silently) and that `h_none` does not; that
      `i_pin_oneline`, `j_pin_multiline` and `n_pin_with_paren_in_call` match
      `pin_pattern`; and that `k_perassert`, `l_decoy_configureGlobal_noseed`,
      `m_decoy_noseed_plus_perassert`, `p_setupfile_pin_elsewhere` and
      `s_decoy_close_brace_split` do not. `q_pin_nested_object` and
      `r_pin_paren_and_nested` are parametrized as expected **non**-matches
      with an `# accepted false refusal (§1.5)` comment on each: they are real
      pins the chosen bound misses, and pinning them as they are is what stops
      a later "improvement" from silently trading them for fixture `s`'s false
      accept.
      Docstring must state the caveat plainly: Python's `re` is not ripgrep's
      Rust engine, so this pins the pattern's *intent* offline; the authority
      is the container measurement of 2026-09-02 recorded in §1.5, and a change
      to either pattern must be re-measured there rather than only here.
- [ ] **Edit** `test_the_node_adapters_ask_for_no_hypothesis_probe`'s
      docstring (`tests/test_runners.py:980-983`). Its final clause ("...and no
      such check exists yet (TASKS.md)") is now false. Replacement text:

```python
    """A recorded absence, never a claim that the suite is deterministic. The
    hypothesis block is a Python-ecosystem check; the node determinism question
    is answered by `property_scan` instead, over the p2p-before sweep's
    `files_run` rather than over `tests.paths`, and as a refusal rather than a
    probe-plus-remedy -- fast-check reads no environment variable, so there is
    no node analogue of CI=1 to declare."""
```

### 4.3 `bakeoff/scripts/mutation_check.py`

- [ ] Add one anchor, placed beside the existing
      `"preflight: record the image.env mismatch and stop refusing it"` entry:

```python
    (
        # The node half of the same defect. Reverting the refusal leaves every
        # piece of EVIDENCE in place -- imported: true, pinned: false -- and
        # flips the verdict from NO-GO to PASS with nothing else visible, which
        # is exactly how a property suite gets certified on a lucky draw.
        "preflight: record the unpinned node property suite and stop refusing it",
        "src/bakeoff/preflight.py",
        "                if imported and not pinned:\n                    problems.append(",
        "                if False:\n                    problems.append(",
        "tests/test_preflight.py -k fast_check_with_no_seed_pin",
        "not integration",
    ),
```

  **The indentation is 16 spaces for the `if` and 20 for `problems.append(`**,
  which is what §3.4's nesting gives (`if scan is not None and swept:` at 8,
  `if scan_files:` at 12, `if imported and not pinned:` at 16). It is two
  levels deeper than the first draft's, because review 1's `files_run` scoping
  added the `if scan_files:` guard. Transcribe it from the file as written, and
  if §3.4's nesting is implemented differently, fix the ANCHOR to match the
  code — never the code to match the anchor. `if imported and not pinned:` is
  unique in `preflight.py`, so the `find` cannot land twice.

  One anchor rather than two: the second candidate — dropping `-U` from the
  pin probe — produces a **false refusal**, which is loud by construction and
  is already pinned by
  `test_the_pin_scan_is_the_only_probe_that_asks_rg_for_multiline`. The
  mutation check exists for the silent direction.

---

## 5. Docs

- [ ] `bakeoff/taskset/HARVESTING.md`, **Layer 1 → Preflight**, immediately
      after the existing hypothesis bullet (`:104`): a new bullet for the node
      scan. Content: it fires when the **p2p-before sweep** runs a file
      importing `fast-check`, `@fast-check/*`, `jest-fuzz` or `jsverify` and
      nothing that sweep runs pins a seed with `configureGlobal(... seed: ...)`;
      the scope is the sweep's `files_run`, **not** `tests.paths`, because with
      `tests.p2p: []` the sweep is rootdir-wide (measured on yaml-474: 25
      suites, 3,497 tests, against a one-file `tests.paths`); there is **no**
      `image.env` remedy because fast-check reads no environment variable in
      either major; the remedies are excluding the file from the sweep or
      picking a different repo.
- [ ] `bakeoff/taskset/HARVESTING.md`, the rg-exit-code bullet directly below
      it: extend it to say the trichotomy now covers **three** probes, name
      them, and add the asymmetry — misreading the seed-pin probe **refuses a
      task that was already fine**, which is why each probe supplies its own
      closing sentence rather than sharing one.
- [ ] `bakeoff/taskset/HARVESTING.md`, **"Screening a JavaScript or TypeScript
      repository"** (`:393`): **replace** the bullet that currently reads
      "**The suite must be deterministic, and nothing checks it here.**
      Preflight's hypothesis probe is a Python-ecosystem check and answers
      nothing for `fast-check` or `jest-fuzz`..." — that sentence is now false.
      The replacement carries: the ten-run tables from §1.2 and §1.4; that no
      environment lever exists and that the NODE_OPTIONS one is jest-defeated;
      the `files_run` scope; **the remedy written out in full, in the measured
      C form of §1.6**, with the explicit warning that a lone
      `--testPathIgnorePatterns` REPLACES the config's list *and* jest's
      built-in `/node_modules/` rule and killed yaml-474's sweep outright (no
      report written) — one line is the wrong number of lines; that the vitest
      `--exclude` analogue is **unmeasured** and must be measured before the
      first vitest property task relies on it; and the two known false
      refusals (a pin in a framework setup file, a quoted specifier in a
      comment), both loud, both clearable by the exclusion remedy.
- [ ] `bakeoff/taskset/HARVESTING.md`, **Layer 2 → The oracle**, the bullet "A
      property-based suite is allowed, and only with `image.env: {CI: "1"}`"
      (`:206`): add a closing paragraph saying that rule is a *hypothesis*
      rule, that node has no such lever, and that the node equivalent is the
      Layer 1 refusal above — cross-referenced both ways.
- [ ] `bakeoff/taskset/HARVESTING.md`, the existing "**Narrowing `tests.paths`
      does not narrow the p2p sweep when `tests.p2p` is left empty**" paragraph
      (~`:536`): add one sentence noting that the node property scan is scoped
      to the sweep **for this exact reason**, so it is the one gate that
      narrowing `tests.paths` does not silence.
- [ ] `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, inside the
      `image.env` comment block, immediately after the `CI=1`/hypothesis
      paragraph (~`:240`): a paragraph stating that `CI` is a hypothesis lever
      with **no node analogue** — fast-check reads no environment variable in
      either major (measured 2026-09-02), so a node property suite is refused
      by preflight rather than levered by this key, and adding `NODE_OPTIONS`
      to the allowlist would not fix it (measured: defeated by jest's module
      registry, and it would reach the agent's own `claude` process). This
      manifest is the documentation for the key, so a reader arriving here must
      not conclude the key covers node.
- [ ] `TASKS.md`: **remove** the "No property-based determinism check exists
      for the node frameworks" bullet (`:1217`).
- [ ] `TASKS.md`: **add** a P2 bullet for the setup-file blind spot — a
      suite-wide `fc.configureGlobal({seed})` living in a framework setup file
      (vitest `setupFiles`, jest `setupFilesAfterEnv`) appears in neither
      `files_run` nor `tests.paths`, so the best-behaved repository this check
      can meet is falsely refused. State that the obvious remedy was measured
      and **does not work** — a non-test file added to `tests.paths` becomes a
      positional the framework collects as a suite, and jest fails it ("Your
      test suite must contain at least one test", measured `2 failed, 2 total`)
      — and that the candidate fix is a manifest key nominating extra files for
      the pin scan, deliberately not added here.
- [ ] `TASKS.md`: **add** a P3 bullet recording that `rg` inherits
      `.gitignore`/`.ignore` and skips hidden files in all three probes, so a
      gitignored test file is not scanned — the silent direction, pre-existing
      on the hypothesis probe, and a change would have to move both probes
      together.
- [ ] `docs/superpowers/plans/2026-09-01-broaden-3-hypothesis.md`: leave
      untouched. It is a shipped plan and a record of what was done, not a
      living document.

---

## 6. Verification

### 6.1 Unit

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py tests/test_runners.py -v
```

Then the whole suite; the baseline is `1539 passed, 62 deselected`, so the
expected new total is 1539 + (number of tests added above) with the same
deselect count.

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v
```

### 6.2 Mutation

Run **solo** — it edits sources in place.

```bash
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Baseline is 160/160; expect 161/161. A stale-anchor failure means the `find`
string in §4.3 does not match the code as written — fix the anchor to the
code, never the code to the anchor.

### 6.3 Gate

```bash
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

Expect `GATE PASSED`.

### 6.4 Integration — the refusal fires on the real task, unmodified

**This is the step the scope decision changed, and it is now a NO-GO rather
than a GO.** Under `files_run` scoping,
`yaml-474-single-newline-empty-value` **as it stands today** is refused: its
rootdir-wide p2p-before sweep loads `tests/properties.ts`, which imports
fast-check, and nothing the sweep runs pins a seed (measured 2026-09-02 —
`grep -rn configureGlobal` over the whole tree matches nothing).

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only \
  --force-preflight --task-set ~/.cache/bakeoff-probe/taskset \
  --tasks yaml-474-single-newline-empty-value
```

Expect **NO-GO**, a problem naming `fast-check` and "pins a seed", and evidence
`property_framework_imported_by_suite: true`,
`property_framework_seed_pinned: false`, with `tests/properties.ts` in
`property_scan_files`. Record the verbatim problem text in the review log.

This is a real finding about a gated task, not a regression: the task's
certified p2p verdict genuinely depends on a draw today, and the gate now says
so. **The probe task set must be updated by §6.5's remedy**, and the review log
must say the task was changed and why — a task that was GO and is now NO-GO is
exactly the sort of thing that gets quietly reverted six weeks later.

### 6.5 Integration — the remedy clears it, and the remedy is the fix

Copy the manifest to a scratch task set as `yaml-474-propscan-fixed`, bump
`task_version`, and change `tests.runner` from

```yaml
  runner: ["/node_modules/.bin/jest", "--config", "config/jest.config.js"]
```

to the measured **C** form of §1.6 — every entry the CLI flag replaces,
re-emitted:

```yaml
  runner:
    - "/node_modules/.bin/jest"
    - "--config"
    - "config/jest.config.js"
    - "--testPathIgnorePatterns=/node_modules/"
    - "--testPathIgnorePatterns=tests/_utils"
    - "--testPathIgnorePatterns=tests/json-test-suite/"
    - "--testPathIgnorePatterns=tests/properties\\.ts"
```

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only \
  --force-preflight --task-set <scratch-task-set> --tasks yaml-474-propscan-fixed
```

Expect **GO**, `property_framework_imported_by_suite: false`,
`property_framework_seed_pinned: null`, and `tests/properties.ts` **absent**
from `property_scan_files`. This is the better integration test the first draft
did not have: it exercises the remedy end to end and proves the refusal is
clearable by an action that changes what the gate certifies.

**Also run the trap**, once, and record it — a harvester will meet it:
substitute the single flag `--testPathIgnorePatterns=tests/properties\.ts` for
the four above and confirm the run writes **no report** and NO-GOs on that,
which is §1.6's variant B. It is the reason `HARVESTING.md` spells the remedy
out in full rather than in one line.

### 6.6 Reproducing the §1 measurements

Left in place: `~/.cache/bakeoff-fcprobe` — **nineteen** fixtures under
`rgfix/` (twelve from review 1, `q`/`r` added by review 2, `s` added while
answering C1) and the yaml start-state tree under `yamltree/`, which is a clean
copy again (review 2's temporary `tests/fcsetup.ts` was removed). Probe images
`bakeoff-fcprobe:latest` (fast-check 3.23.2) and `bakeoff-fcprobe:v2` (2.25.0);
`bakeoff-fcprobe:yamlrun` also exists from review 1 and is **not needed** —
`bakeoff-task-yaml-474-single-newline-empty-value:v1` is the authority for
§1.6. Any change to either regex, to the "no environment lever" claim, or to
the remedy's exact flag list must be re-measured there, in the container,
before the prose moves — and a proposed pin bound must be run against fixture
`s`, which is the one that distinguishes a false refusal from a false accept.

---

## 7. What this does NOT do

- **It does not make any node property suite deterministic.** There is no
  environment lever to add, and the two levers that do exist each cover one
  framework and miss the other. This item closes a gate and gives it a remedy
  that removes the file from what is certified; it does not fix the underlying
  nondeterminism, and no code or doc it touches may claim it does.
- **It does not add `NODE_OPTIONS` — or any key — to `_IMAGE_ENV_ALLOWED`.**
  That set stays `{"CI", "HYPOTHESIS_STORAGE_DIRECTORY", "TZ"}`.
- **It does not see a pin that lives outside what the sweep ran.** A
  suite-wide `fc.configureGlobal({seed})` in a framework setup file appears in
  neither `files_run` nor `tests.paths` — measured (fixture
  `p_setupfile_pin_elsewhere.ts`) — so the best-behaved repository this check
  can meet is **falsely refused**. Loud direction; the exclusion remedy still
  clears it; filed in `TASKS.md`, with the measurement that shows why "add the
  setup file to `tests.paths`" is not the fix.
- **It over-matches a quoted specifier.** `'fast-check'` inside a comment or a
  string literal triggers the import probe — measured (fixture
  `o_comment_mention.ts`). A false refusal, the loud direction, and the pin
  hatch does not clear it; the exclusion remedy does. Anchoring the pattern
  would cost the two commonest real spellings (§3.3).
- **It does not treat a per-`fc.assert` seed as a pin.** Seeding one assertion
  is not a claim about the file, and the pattern was measured to exclude it
  even when an unseeded `configureGlobal` sits in the same file (fixture
  `m_decoy_noseed_plus_perassert.ts`).
- **It does not match a pin whose options object contains a nested object
  literal.** `[^{}]` cannot cross a `{`, so `configureGlobal({ examples: [{ a:
  1 }], seed: 42 })` is a false refusal — measured, fixtures
  `q_pin_nested_object.ts` and `r_pin_paren_and_nested.ts`. A nested **call** is
  fine (`n_pin_with_paren_in_call.ts`, measured). This is a deliberate trade,
  not an oversight: the one measured bound that catches `q` and `r`
  (`(?:[^}]|\}[^)])*?`) produces a **false accept** on
  `s_decoy_close_brace_split.ts`, and a silent accept certifies a coin-flip
  sweep. Every miss this bound has is in the loud direction, and the exclusion
  remedy clears all of them.
- **It does not scan a gitignored or hidden file.** `rg` inherits those
  defaults in all three probes. The silent direction, pre-existing on the
  hypothesis probe, stated once in `_rg_probe`'s docstring and filed rather
  than changed under one of them.
- **It does not verify that a found pin executes.** Unlike `image.env`, which
  preflight reads back out of the container, a `configureGlobal` match is a
  grep over source. The asymmetry is stated in the code comment and in
  `HARVESTING.md`; it is not hidden behind a confident-looking boolean.
- **It does not add a node analogue of `hypothesis_importable`.** That key
  exists because availability alone must **not** trigger the pytest refusal;
  here the import *is* the trigger, so an availability probe would be an
  evidence key nothing reads.
- **It does not touch the pytest/hypothesis path's behaviour or its message.**
  The only edit there is routing the existing probe through `_rg_probe` with
  its problem text byte-identical, newly pinned by a test.
- **It does not change the p2p sweep, `tests.p2p`, or any suite argv the
  harness builds.** The remedy is something a task *author* writes into
  `tests.runner`; `preflight` emits no new flag.
- **It does not touch** `SCHEMA_VERSION`, `GRADER_VERSION`, `RunRecord`,
  `GradeRecord`, `grader.py`, `oracle.py`, `images.py`, the generated
  Dockerfile, or any manifest key. No manifest that loads today stops loading —
  though `yaml-474-single-newline-empty-value` in the probe task set now
  requires the §6.5 runner change to stay GO, which §6.4 says out loud.

---

## 8. Review 1 → changes

Review 1 (`.superpowers/broaden/round2/plan-12-review-1.md`, 2026-09-02):
REVISE, 13 findings, 4 blocking. **13 addressed; 2 adopted with a measured
correction, both flagged below.** Everything the review listed under "What is
right and should not be re-litigated" is unchanged.

**Finding 1 (BLOCKING) — the first remedy silenced the check. ADOPTED.**
"Narrow `tests.paths`" is gone from the refusal message, from `HARVESTING.md`,
and from everywhere else in this plan. Under finding 2's scope it is not even
expressible: `tests.paths` is no longer the scan's scope, so narrowing it
changes nothing about this gate. The remedy list is now two entries, and the
first one is the one that changes what the gate certifies.

**Finding 2 (BLOCKING, Q1) — scan the p2p-before run's `files_run`. ADOPTED**,
and §2.3 is rewritten around it. `Outcome.files_run` verified present and
populated (`runners/__init__.py:77-86`, `node_adapter.py:326-354`), `None` for
pytest, already read by preflight for the scoped run
(`preflight.py:1937-1942`). The p2p-before run at `:1655` was verified
unconditional inside `with RunContainer` with no early return between it and
the f2p block. The scan now sits immediately after
`runner.classify(green)`, whose result is bound rather than discarded — one
line, behaviour-preserving. `test_the_property_scan_runs_before_the_suite` is
deleted and replaced by
`test_the_scan_runs_after_the_p2p_before_sweep_that_gives_it_its_scope`, whose
rationale is the `last_report` overwrite rather than the environment-defect
ordering rule the review correctly says does not apply here. §2.5 F is
rewritten to the review's stronger reason (a rootdir grep is a refusal **no
action clears**), and the first draft's own `tests.paths` rule is added as a
named rejected alternative F2 rather than quietly vanishing. §6.4/§6.5 are
re-framed as the review suggests: the real task is now a **NO-GO**, and the
remedy clearing it is the integration test.

**Finding 3 (BLOCKING) — `PREFLIGHT_VERSION` read-and-add-one. ADOPTED.** Both
§3.4's bullet and §3.5's table row now say "read the literal in `preflight.py`
and write its successor; record the observed value in the review log", with the
regression the literal would cause spelled out. No number is transcribable from
this plan.

**Finding 4 (BLOCKING) — the byte-identical claim was false. ADOPTED, option
(b).** `_rg_probe` gains a `consequence` parameter carrying the caller's own
closing sentence, and the hypothesis call site passes its current tail verbatim
— so the message really is byte-identical, and §3.4 writes the full expected
string out. The parameter is not merely a fix for the promise: the review's own
framing shows why one shared sentence is wrong, since misreading the seed-pin
probe **refuses a task that was already fine** while misreading the other two
disarms a check.
`test_the_hypothesis_rg_message_is_byte_identical_after_the_extraction` is
added to pin it, since nothing did.

**Finding 5 / Q2 — refusal versus lever. Confirmed by the reviewer; unchanged.**
§2.5 A keeps the `NODE_OPTIONS`-in-the-allowlist reason prominent, as asked.

**Finding 5 / Q3 — keep the escape hatch. ADOPTED**, with §2.4 corrected: the
review's new fixture `m` is folded in, and §2.4 now states the **refuse**
direction as well as the accept one.

**Finding 6 (MEDIUM) — the `)` bound was never tested. ADOPTED, and the bound
changed. See the Review 2 block below: the change is a TRADE, not a fix.** The
review is right that no fixture exercised the bound and that `m` is the missing
case; `m` is now in §1.5, §4.2 and §6.6. On the twelve-plus-`m` set `[^{}]`
scored 16/16 against `[^)]`'s 15/16, and the first draft of this section called
that "fixed". Review 2 added `q` and `r` and showed both bounds score 16/18
there, each missing a different real pin — so the headline was an artifact of
which fixtures existed. `[^{}]` is kept, and the reason is now the failure
**kind** rather than the count: it traded a measured miss for a different
measured miss, both in the loud direction, and it is the bound that cannot
cross a brace and therefore cannot join two calls. §1.5 carries the
three-bound table and fixture `s`, which is what settles it.

**Finding 7 (MEDIUM) — the node fixture was deferred. ADOPTED**, and decided in
the plan. `_node_container` gains `p2p_before_files=()` and
`**container_kwargs` forwarded into `_ScriptedContainer`, with
`present=tests.paths` at `:3172-3173` becoming
`container_kwargs.pop("present", tests.paths)` so an override wins rather than
collides. Signature written out in §4.1. One helper; every existing call site
unchanged. The first draft's `_node_task(**overrides)` is dropped.

**Finding 8 (MEDIUM) — a pin in a setup file is refused. FINDING ADOPTED, FIX
DISPUTED ON A MEASUREMENT.** The case is real and is now stated in §2.4 and
§7 with the review's fixture `p`. The proposed fourth remedy — "add the file
that calls `fc.configureGlobal` to `tests.paths`" — is **not viable**, measured
2026-09-02 in `bakeoff-task-yaml-474-single-newline-empty-value:v1`: a non-test
file passed as a positional is collected as a suite and jest fails it, giving
`Test Suites: 2 failed, 2 total` where only one failure is real. It would trade
a loud false refusal for a broken scoped run. Under `files_run` scoping the
setup file is out of scope anyway, so the remedy would not even reach the
probe. Filed as a `TASKS.md` follow-up with that measurement, and the candidate
fix named (a manifest key nominating extra files for the pin scan).

**Finding 9 (LOW) — a pin containing a call is missed. TRADED, not fixed.**
`[^{}]` matches `n_pin_with_paren_in_call.ts`, which `[^)]` missed. It then
misses `q_pin_nested_object.ts` and `r_pin_paren_and_nested.ts`, which `[^)]`
caught. Both residues are false refusals; §7's bullet now cites the fixtures
rather than asserting the shape, which review 2 correctly flagged as the only
claim in that section with nothing behind it.

**Finding 10 (LOW) — the import pattern matches a comment. ADOPTED.** §7 now
lists what the check **over**-detects, not only what it misses, with fixture
`o` cited; §4.2's parametrized test pins the over-match deliberately rather
than leaving it to be discovered.

**Finding 11 (LOW) — list-membership where substring was meant. ADOPTED.** All
three fast-check assertions are written `any("fast-check" in part for part in
cmd)` in §4.1, with a paragraph saying why and confirming the `-U` assertions
are correct as element membership.

**Finding 12 (LOW) — `rg` inherits `.gitignore`. ADOPTED.** One paragraph in
`_rg_probe`'s docstring (§3.4) naming the inherited defaults, that this is the
silent direction, that it is parity with the hypothesis probe rather than a
regression, and that a change would have to move both. Also a §7 bullet and a
`TASKS.md` P3 bullet.

**Finding 13 (INFO) — the free conformance gate. ADOPTED.** §4.2 opens by
naming `tests/test_runners.py:50-59` and instructing that **no** redundant
"every adapter has the method" test be added, with the `_Adapter` at
`tests/test_preflight.py:2875` confirmed clean.

### One correction to the review itself

§5 Q1.4 says the remedy is "adding `--testPathIgnorePatterns=tests/properties\.ts`
to `tests.runner`" and puts the cost at "1 of 217 tests". Measured on the real
image (§1.6), **that exact flag destroys the run**: a CLI
`--testPathIgnorePatterns` replaces the config's list *and* jest's built-in
`/node_modules/` rule, so yaml's vendored `tests/json-test-suite/parsers/
test_json.js` gets collected, throws in `readFileSync` (jest leaves
`process.argv[2]` undefined) and calls `process.exit(1)` from its catch on
line 10, and jest writes **no
report at all** — `KIND_ENVIRONMENT`, a NO-GO whose message names the missing
report and not the flag. The working remedy re-emits all four entries, and the
cost is **1 test of 3,497** (the sweep is 3,497 tests across 25 suites; 217 is
the scoped file's count). The ruling stands — the refusal is clearable and the
remedy is the fix — but the remedy is not one line, and §1.6, §3.4's message,
§5's `HARVESTING.md` bullet and §6.5 all now spell it out in full, with the
trap recorded as a step to run once.

---

### Review 2

Review 2 (same file, 2026-09-02): **APPROVE**, 4 findings, none blocking.
It re-measured the §1.6 trap table (identical, all three variants), the
sixteen-fixture pin comparison (identical), the rejected fourth remedy
(`2 failed, 2 total`, rejection upheld), the `_rg_probe` reconstruction
(**byte-identical: True**), and the mutation anchor's nesting (matches). All
four carry-ins are folded in place above.

**C1 (LOW) — the `[^{}]` claim over-reached. ADOPTED, and answered with a new
measurement rather than a hedge.** Review 2's `q_pin_nested_object.ts` and
`r_pin_paren_and_nested.ts` show `[^)]` and `[^{}]` both scoring 16/18, each
missing a different real pin — so "fixed" and "16/16 against 15/16" were
artifacts of the fixture set, and §8's finding 6/9 entries are reworded above
to say so. Taking the offered alternative, a bound scoring **18/18** was found
and measured: `(?:[^}]|\}[^)])*?` — "no `})` between" — which needs no PCRE2
and runs on rg's default engine, matching `i`, `j`, `n`, `q` and `r` and
rejecting all thirteen non-pins in review 2's set.

**It is rejected, and the new fixture `s_decoy_close_brace_split.ts` is why.**
A `configureGlobal({\n numRuns: 500\n}\n);` closes with a newline between the
brace and the paren, so "no `})` between" never fires and the match runs on
into a later per-assert `{ seed: 7 }`: measured, that bound exits **0** on `s`
where `[^{}]` exits 1, and `rg -o` shows it matching across the closed call.
That is a **silent false accept** — the gate certifying a sweep that is a coin
flip, which is the defect this whole item exists to close — while every miss
`[^{}]` has is a **false refusal** the exclusion remedy clears. On nineteen
fixtures `[^{}]` scores 17/19 with **zero false accepts**; the 18/18 bound
scores 18/19 **with one**. The count was the wrong metric, and "prefer a loud
failure over a plausible-looking zero" decides it. §1.5 now carries the
three-bound table, §7's bullet cites `q` and `r`, and §4.2 parametrizes `q` and
`r` as accepted false refusals so a later "improvement" cannot silently trade
them for `s`.

**Final pattern, unchanged from the review-1 revision:**
`configureGlobal\s*\(\s*\{[^{}]*?\bseed\s*:`.

**C2 (LOW) — the trap's fatal line. ADOPTED.** §1.6 now says
`process.exit(1)` at `tests/json-test-suite/parsers/test_json.js:10`, inside
the `catch` — under jest `process.argv[2]` is undefined, so `readFileSync`
throws — and notes that the `process.exit(0)` on line 13 is never reached. The
mechanism and the conclusion are unaffected; the citation is now what a
re-measurer will see.

**C3 (LOW) — the ordering test permitted what its rationale forbids. ADOPTED.**
`test_the_scan_runs_after_the_p2p_before_sweep_that_gives_it_its_scope` now
bounds the scan below by the **next suite invocation of any kind**, using the
predicate the file's existing ordering tests already use — `cmd[:1] ==
["timeout"] and "--co" not in cmd`, verified at
`tests/test_preflight.py:1825`. The p2p-after bound was wrong because the
f2p-after run sits between the two, and `last_report` is overwritten by **any**
subsequent `_Runner.run`, not only the p2p-after one.

**C4 (INFO) — measurement-cache state. ADOPTED.** §6.6 now records nineteen
fixtures under `rgfix/`, the `yamltree/` copy as clean again after review 2's
temporary `tests/fcsetup.ts` was removed, and `bakeoff-fcprobe:yamlrun` as
existing but not needed — `bakeoff-task-yaml-474-single-newline-empty-value:v1`
is the authority for §1.6. It also now instructs that any proposed pin bound be
run against fixture `s`, which is the fixture that separates a false refusal
from a false accept.
