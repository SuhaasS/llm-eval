# Broadening 2 — a task whose f2p module does not COLLECT at the start state

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept a task whose declared f2p tests cannot even be *collected* at the start state — the shape a PR that ADDS a symbol always has — without reopening the Phase 0c failure that `assert returncode != 0` was.

**Architecture:** One predicate, three call sites. `preflight.py` grows `collection_error_modules(output)` and `f2p_modules(f2p)` beside the existing `failed_node_ids`; preflight's red-before block accepts a non-{0,1} f2p exit **only** when the reported errors are exactly the declared f2p modules **and** the p2p baseline is green, which requires deferring the f2p verdict until after the p2p-before run and passing `--ignore=<module>` to that one run so it can happen at all; `grader.py`'s check 5 uses the same predicate with a *containment* comparison so an unfixed submission on such a task is a named `f2p_failed` instead of an ungradable `ENVIRONMENT_ERROR`. Green-after is untouched and stays strict. The graded argv is untouched.

**Tech Stack:** Python 3.12, pytest (venv 9.1.1; eval image pins 9.1.1; the click task pins 8.3.5 — every claim below was measured on **both**), Docker (integration tests only).

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3 the loop being measured, §3.7 the task record, §5.6 the submission diff, §6.4 confounds) and `docs/superpowers/specs/2026-08-17-offline-grader-design.md` (checks 5 and 6, what `resolved` means). Shared context: `.superpowers/broaden/CONTEXT.md`. Candidate rules: `bakeoff/taskset/HARVESTING.md`.

## Global Constraints

- Repo: `/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden`, branch `broaden-taskset`. Do not touch any other checkout.
- Run everything from `bakeoff/` with its venv: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`. Baseline before this work: **1129 passed, 46 deselected** (broadening 1 is already at HEAD; re-measure and record the real number in the first commit's verification step).
- **Integration tests are deselected by default** (`addopts = "-m 'not integration'"`). A guarantee pinned *only* by an integration test is not pinned by the suite anyone runs, so every guarantee below has a default-suite test too. Run the integration leg when a change touches container/preflight/image behaviour: `cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`. `--basetemp` under `$HOME` is mandatory on macOS — the Docker VM mounts `$HOME` but not `/var/folders`, and a repo bind-mounted from there is a silently empty directory inside the container.
- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. Claims about external behaviour are annotated with what they were verified against (`Measured 2026-09-01 against pytest 9.1.1 and 8.3.5`). A comment saying what a line does rather than what breaks without it does not fit here.
- **Do NOT edit `CLAUDE.md` in this branch.** An uncommitted edit to it exists on `main` and the merge would conflict. Invariant prose goes in module docstrings and `HARVESTING.md`; the sentences that belong in `CLAUDE.md` are listed in this plan's final section.
- **`PREFLIGHT_VERSION` "3" → "4".** It is in the preflight cache key; without the bump every warm cache serves a verdict written by a gate that refused this task shape.
- **`GRADER_VERSION` "2" → "3".** It gates resume. Check 5's verdict on a non-{0,1} exit changes from `ENVIRONMENT_ERROR` to `f2p_failed` for one input class, and that is a change to what a check MEANS.
- **`SCHEMA_VERSION` does not move.** Nothing new is written into a `RunRecord`. `GradeRecord` gains no field either — see decision 7.
- **No new manifest key.** This broadening adds nothing to `task.yaml`, so `manifest_digest` and `start_sha` cannot move and the click task's `start_sha: 33575cc0b75608fa5cbcb1d3ae3347b81eac437f` must be byte-identical at the end. `git diff bakeoff/taskset/` must be empty.
- **The gated argv is the graded argv.** `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` pins this against a *literal*. The new `ignore` keyword defaults inert and that test grows a third assertion proving the default is the same literal.
- Stage files explicitly (`git add <paths>`), never `git add -A`/`-a`. End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Do not run `bakeoff/scripts/mutation_check.py` concurrently with anything else; it edits sources in place. This plan repoints one `MUTATIONS` entry and adds one (Task 3, Step 5); every other guarantee below is pinned by a default-suite test that exercises the *caller* (`preflight`, `run_ladder`), not just the helper. It adds no entry for the `and p2p_green` conjunct — `if not p2p_green:` is unconditional, so reverting that conjunct changes the refusal message and not the verdict, and an entry guarding a message would report a guarantee this repo does not have.
- YAGNI: build only what this broadening needs. No hooks for broadenings 3–7.

---

## Design decisions, and why

Read this whole section before Task 1. Every task implements one of these; a reviewer rejecting a task rejects one of these decisions.

### 1. What pytest actually prints — measured, and it refutes the brief

Measured 2026-09-01 in the worktree venv. **Every row below was run twice — once under pytest 9.1.1 (the venv, and the base image's `PYTEST_VERSION` pin) and once under pytest 8.3.5 (what `click-3360`'s `image.pip` pins) — and the two agreed on every exit code and on every summary line.** All runs carry the pinned `-q -p no:cacheprovider`.

**Which tree each row was taken on matters and is named per row**, because rows G and H are only exit 2 → 0 on a tree with nothing else red — with a failing test present, H measures 1:

- **T1** — `mypkg/__init__.py` (no `redact_db_url`), `tests/test_missing_symbol.py` (imports `redact_db_url`, so it will not import), `tests/test_ok.py` (collects, passes). **Nothing else red.**
- **T2** — T1 plus `tests/test_fails.py` (one failing assertion).
- **T3** — `tests/conftest.py` importing a module that does not exist, `tests/test_calc.py` passing. This is the replacement Phase 0c fixture (Task 3, step 6).

| # | tree | argv after the runner | exit |
|---|---|---|---|
| A | T1 | `tests/test_missing_symbol.py::test_redacts tests/test_missing_symbol.py::test_passes_through` | **4** |
| B | T1 | `tests/test_missing_symbol.py` | **2** |
| C | T1 | `tests/` | **2** |
| D | T1 | A + `--continue-on-collection-errors` | **4** |
| E | T1 | C + `--continue-on-collection-errors` | **1** |
| F | T1 | B + `--continue-on-collection-errors` | **1** |
| G | T1 | `--deselect tests/test_missing_symbol.py::test_redacts --deselect tests/test_missing_symbol.py::test_passes_through` (preflight's p2p-before argv verbatim) | **2** |
| H | T1 | G + `--ignore=tests/test_missing_symbol.py` | **0** |
| I | T1 | `--ignore=tests/no_such_module.py` (a path that does not exist) | **2** — the flag is silently accepted, it is the *unignored* module that still errors |
| J | T2 | `tests/test_missing_symbol.py::test_redacts tests/test_fails.py::test_will_fail` | **4**, and **only** the collection error is reported |
| K | T3 | `tests/test_calc.py::test_add` (a broken **conftest**) | **4**, with **no `short test summary info` section at all** — no `ERROR <path>` line, so the reported set is *empty* and the run is UNCONFINED |

**Row A is the finding that reshapes this broadening.** `_Runner.select(tests.f2p)` passes **node ids positionally**, and pytest answers a node id whose module will not import with a **usage error, exit 4** — not exit 2. Exit 2 is what a *module-path* or *directory* run gives (rows B, C), which is how the `trucking-doc-extraction` #3 measurement in HARVESTING.md was taken and why the brief says "exit 2". Preflight's f2p run is row A. So the acceptance must cover **exit 4 first and exit 2 second** (2 is only reachable if an author declares a bare module as an f2p entry, which HARVESTING already discourages; handling both costs one tuple).

Verbatim output of row A (9.1.1; 8.3.5 differs only in the traceback frames):

```
ERROR: found no collectors for /…/tests/test_missing_symbol.py::test_redacts

ERROR: found no collectors for /…/tests/test_missing_symbol.py::test_passes_through


==================================== ERRORS ====================================
________________ ERROR collecting tests/test_missing_symbol.py _________________
ImportError while importing test module '/…/tests/test_missing_symbol.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/…/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_missing_symbol.py:1: in <module>
    from mypkg import redact_db_url
E   ImportError: cannot import name 'redact_db_url' from 'mypkg' (/…/mypkg/__init__.py)
=========================== short test summary info ============================
ERROR tests/test_missing_symbol.py
1 error in 0.01s
```

Four properties of that output the parser depends on, each measured:

- The summary line is **`ERROR tests/test_missing_symbol.py`** — the **module path, relative to rootdir, with no `::`**. There is no `ERROR <path> - ImportError: …` reason-suffix form under `-q` for a collection error; the reason lives in the `ERRORS` block above. (`FAILED a.py::t - AssertionError: …` and `ERROR a.py::t - ValueError: …` — the fixture-error form the existing `test_failed_node_ids_reads_both_failures_and_errors` pins — both carry `::`. **The absence of `::` is what separates "this module did not import" from "this test failed or errored", and it is the whole discriminator.**)
- The `ERROR: found no collectors for …` lines carry a **colon** right after `ERROR`, so `_FAILED_LINE = ^(?:FAILED|ERROR)\s+(\S+)` does **not** match them (`:` is not `\s`). `failed_node_ids` therefore already returns exactly `{"tests/test_missing_symbol.py"}` on this output, with no change. Confirmed against the two other exit-4 shapes: a node id that does not exist in an importable module prints `ERROR: not found: …` + `(no match in any of [<Module test_ok.py>])` and a missing file prints `ERROR: file or directory not found: …` — **neither produces any `ERROR <path>` summary line at all**, so `failed_node_ids` returns the empty set for both. A typo'd manifest id and a collection error are already distinguishable through the existing parser. This is the reason no new regex is written.
- **`-q` does NOT suppress `!!! Interrupted: 1 error during collection !!!`** — measured, it is printed in rows B, C and G. (The line `-q` *does* suppress is `collected N items`, which is what the `EXIT_NOTHING_COLLECTED` comment in `preflight.py` is about; do not conflate them.) Nothing is parsed from the Interrupted line and nothing should be — the exit code and the `ERROR <path>` lines carry it.
- **Row J: a MIX cannot occur.** With one f2p module erroring at collection and another f2p id in a module that imports and fails, pytest exits 4 and reports *only* the collection error — the failing test never runs, nothing is reported for it. Without `--continue-on-collection-errors` an exit of 2 or 4 means **nothing ran at all**. So the existing rule "every declared f2p id appears in FAILED/ERROR lines" is **unsatisfiable** on this shape and must become a module-level rule (decision 3).

### 2. `--continue-on-collection-errors` is REJECTED, and rows D/E/G/H say why

The brief proposes appending it to the f2p select run to "turn exit 2 into exit 1 with the erroring modules listed and any importable f2p ids actually run". **Measured, it does none of that on the f2p run**: row D is exit 4, byte-for-byte the same summary as row A. The flag only helps a run that collects a *directory* or a *module path* (rows E, F), and the f2p run collects neither.

It does help the **p2p** run — row G (preflight's p2p-before argv verbatim) is exit **2**, because the erroring module is inside the rootdir sweep and collection aborts before `--deselect` is ever applied; with the flag it becomes exit 1 / `1 passed, 1 error` (row E's shape). But exit 1 is not green, so the p2p-before assertion would still have to be relaxed to a predicate, and the flag would then have to be added identically to preflight, `oracle._derive` and `grader._check_p2p` to keep the gated argv the graded argv. That changes what check 6 *means* on every task in the set: a submission that breaks an unrelated import today exits 2 → `ENVIRONMENT_ERROR`, and with the flag would exit 1 → `p2p_regression` — an **accusation** manufactured out of an environment difference, which is the one direction this codebase refuses. Recovering today's behaviour would then need a second predicate inside check 6 to route bare-module errors back to `environment()`. That is a lot of blast radius for a flag that does not fix the run the broadening is actually about.

**Instead: `--ignore=<module>`, on preflight's p2p-BEFORE run only.** Row H: exit **0**, clean, on both pytest versions. Row I: an `--ignore` naming a path that does not exist is silently accepted and cannot itself produce a usage error, so a stale entry degrades to "no effect" rather than to a NO-GO.

This is legitimate precisely because **preflight's p2p-before is not an argv the grader ever runs.** The grader's check 6 runs p2p at the *post-submission* state; the preflight run that has to match it byte-for-byte is p2p-**after** (and the scoped one), and neither gets `--ignore`. The one thing `--ignore` hides — a non-f2p test living inside a stripped-out f2p module — is measured anyway by the unchanged p2p-**after** run, which collects that module (it imports once the reference lands) and must exit 0. So nothing goes unmeasured; a run that *could not happen* is replaced by one that can, and the thing it could not see is seen one step later.

### 3. The acceptance predicate, exactly

Two tiny helpers in `preflight.py`, beside `failed_node_ids`, because three modules already import from there and a second copy of this parse is where the semantics drift:

```python
def collection_error_modules(output: str) -> frozenset[str] | None:
    """The modules pytest could not COLLECT, or None if it reported anything else."""

def f2p_modules(f2p: tuple[str, ...]) -> frozenset[str]:
    """The module half of each declared f2p node id."""
```

`collection_error_modules` returns `None` when the reported set is empty **or** when any reported entry contains `::` — i.e. it answers "this output is *purely* collection errors and nothing else", which is (iii) of the brief. The comparison against `f2p_modules` is left to each caller, because **the two callers want different comparisons and hiding that behind one flag would make both unreadable**:

- **preflight (red-before) compares for EQUALITY.** The rule it replaces is "every declared f2p id appears in FAILED/ERROR lines", whose job is that no declared id is silently unchecked. Row J proves a partial collection error hides the rest, so equality at module granularity is the same claim one level up: every declared f2p module errored, and nothing else did. A task whose f2p set spans a new module and an existing one is therefore **refused** on this path — correctly, because the existing module's ids are unobservable at exit 4 and accepting them would be exactly the silent-unchecked-id defect. The refusal message says to split the task or cut a different PR.
- **the grader (check 5) compares for CONTAINMENT.** Its job is the opposite: never accuse for something outside the task. An unfixed submission errors on all the f2p modules (equality); a *partially* fixed one errors on a subset and is still a model failure; an error naming a module outside the set is indistinguishable from a lost image dependency and must stay `ENVIRONMENT_ERROR`. Subset is the predicate that says exactly that.

Full acceptance of a collection-error task is a **THREE-way conjunction**, and the first draft of this plan got it wrong by claiming two of the three were enough:

> a task whose f2p run exits 2 or 4 is a GO **iff**
> (i) `collection_error_modules(f2p output) == f2p_modules(tests.f2p)` — the errors are confined;
> (ii) the p2p-before run exits 0; **and**
> (iii) the f2p-after run exits 0 — the collection error is GONE once the reference lands.

Each conjunct rules out something the other two cannot see:

- **(i) confinement** rules out a module erroring that no f2p id names, and rules out a declared f2p module that did not error (an id nobody checked — see decision 3's equality paragraph).
- **(ii) p2p-green-before** rules out a *globally* broken image and establishes the regression baseline: a p2p check against an already-red suite means nothing. Row G is why it needed `--ignore` before it could be observed at all.
- **(iii) f2p-green-after** is the one that rules out **a dependency imported only by the f2p module** — and neither (i) nor (ii) can. The f2p select run imports only the f2p modules, so such a dependency produces a *perfectly confined* error set; the p2p run never imports that module, so p2p is *green*. **That is exactly the existing Phase 0c integration fixture** (`test_a_task_whose_tests_cannot_even_run_is_refused`, whose test half is `import a_module_this_image_does_not_have` inside the declared f2p module), and it is refused by green-after, not by the pair. Saying "p2p-before is the only thing that observes the rest of the suite" is true and irrelevant here: the defect is not in the rest of the suite.

**(iii) needs no new code.** The green-after block already refuses `after_f2p.exit_code != EXIT_ALL_PASSED`, unconditionally, and this plan does not touch it (decision 5, Task 4). What has to change is the *argument*: every place that states the guarantee — decision 3, the preflight comment block, the two acceptance test docstrings, and the `CLAUDE.md` sentence in the final section — names all three conjuncts, because a reader who believes two are sufficient will eventually "simplify" the third away.

**A consequence of (ii) worth writing down:** `--ignore` of the erroring module can leave the rootdir sweep with nothing to collect, which is pytest exit 5 — measured on the smoke fixture, whose `tests/` holds only the f2p module. Such a task is refused ("the rest of the suite is not green — no tests were collected"), correctly: it has no regression baseline at all. This is not new behaviour — an exit-1 task whose only tests are its f2p tests already exits 5 on p2p-before today — but the refusal message is easy to misread on a collection-error task, which is why the branch in decision 4 prints the argv it ran.

### 4. Ordering: the runs stay in the order they are in; the f2p **verdict** is deferred

The `--ignore` list for p2p-before is derived from the f2p run's output, and the f2p verdict depends on p2p-before's exit. That is a cycle only if both are decided where they are run. It is not: `preflight` **collects** problems rather than raising them, so the fix is to run f2p, keep its result, run p2p-before with the derived `--ignore`, and then evaluate both.

Do **not** swap the two runs. Their order determines what each sees of the tree (the same reason the grading-argv assertions were deliberately placed before the scoped p2p run), and swapping would change a measurement to save one local variable.

**The confined-but-p2p-red refusal prints `runner.last_argv`**, the way the scoped-p2p refusal already does, and that is not cosmetic. `ERROR <path>` lines are **rootdir-relative**; `--ignore` resolves against the **invocation directory**. They coincide for every task in the set today (`RunContainer` execs with cwd `/repo` and pytest's rootdir is `/repo`), but a repo whose rootdir sits below the working directory — a `pyproject.toml` in a subdirectory, a `tests.runner` with `-c`/`--rootdir` — makes the derived path miss. Measured (row I): a `--ignore` naming a path that does not exist is **silently accepted**, so the failure mode is that the flag no-ops, p2p-before still exits 2, and the author reads "the rest of the suite is not green at the start state" with nothing anywhere saying an ignore was attempted. Printing the argv turns that into one glance.

**One conjunct is a diagnostic, not a gate, and the plan says so rather than pretending otherwise.** `if not p2p_green: problems.append(…)` is unconditional, so `elif confined and p2p_green:` versus `elif confined:` does not change GO/NO-GO — the task is refused either way. What the conjunct buys is a refusal that *names the coupling* (and prints the argv), instead of an author on a collection-error task reading a bare "p2p is not green" and having no way to know the two are related. This is why the `MUTATIONS` entry added in Task 3 anchors on the **equality**, which does flip a NO-GO into a GO, and not on this conjunct, which does not — see Task 3, step 7.

### 5. Green-after is NOT relaxed, and that is pinned

After the reference fix, `f2p` must exit **0**. The collection error is the bug; a reference that leaves the module unimportable has not fixed it, and a solved run and an idle run would leave identical evidence — the exact failure `preflight`'s module docstring opens with. `p2p`-after and the scoped p2p-after keep their `!= EXIT_ALL_PASSED` refusals unchanged and get no `--ignore`. Task 4 exists solely to pin this against a future "symmetrical" relaxation: a scripted task whose f2p-after exits 4 with perfectly confined errors is still a problem.

### 6. Grade time: check 5 changes, check 6 does not, the oracle does not

`_check_f2p` today maps every non-{0, 1, 124} exit to `state.environment(...)` → `not_graded_reason: ENVIRONMENT_ERROR`, `resolved: None`. On a broadening-2 task that is a systematic mis-bucketing, and an **asymmetric** one: an arm that half-fixed the module gets exit 1 → `resolved: False`, while an arm that did **nothing** leaves the import broken, gets exit 4, and grades as *not graded* — so the do-nothing arm is invisible in any view that counts `False`. A null standing in for a negative is the defect class this repo names most often.

The change is one branch, before the environment fallback: on a non-{0,1,124} exit, if `collection_error_modules(output)` is not `None` and is a **subset** of `f2p_modules(task.tests.f2p)`, record `F2P_FAILED`. Otherwise `environment()` exactly as today.

`f2p_failed_node_ids` is set to the sorted module paths. That is honest observation — it is what pytest reported, and a pytest module *is* a node id (the collector node), distinguishable from a test id by the absent `::`. No new `GradeRecord` field, so no `grade_schema.py` change and no `SCHEMA_VERSION` move; the grader spec's field list gets one sentence saying a `::`-less entry means the module did not import.

**Check 6 is untouched**, and it cannot be reached in the confined state anyway: check 5 runs first and raises `_Stop` on failure, so check 6 only ever runs on a tree where every f2p module imported. Its exit-2 → `environment()` route stays. **`oracle.py` is untouched**: `_classify` accepts only 0 and 1, both its runs are at the post-fix state, and preflight has already proved f2p-after exits 0 there, so no collection error can reach it. `derive_quarantine`'s use of `failed_node_ids` is unaffected for the same reason. Task 5 pins both non-changes with tests, because "unchanged" is a claim a reviewer should be able to see fail.

### 7. Evidence, and no new problem code

`PreflightResult.evidence` gains three keys, **all written unconditionally** — "this task has no collection errors" and "the gate did not look" render identically as a missing key:

| key | value |
|---|---|
| `f2p_red_kind` | `"failed"` (exit 1, ids named) \| `"collection_error"` (exit 2/4, confined) \| `"passed"` (exit 0 — the task is already done) \| `"unknown"` (anything else: an unconfined error, a typo'd id, a timeout). `"passed"` and `"unknown"` are separate values because collapsing them makes an already-solved task and a broken environment render identically in a cached verdict, which is the defect one layer down that "absence is recorded" exists to prevent |
| `f2p_collection_errors` | sorted module paths from the f2p run, `[]` when there were none |
| `p2p_before_ignored` | the `--ignore` paths actually passed to p2p-before, `[]` normally |

`f2p_before_exit` and `p2p_before_exit` already exist and now legitimately carry 4 and 0 respectively for this shape.

**No new `problem_codes` entry.** That channel exists for outcomes a caller must *branch* on (`SCOPE_COLLECTS_NOTHING` is read by `run_matrix`); nothing branches on the red kind, and adding a code nothing reads is the manifest-key-that-looks-like-a-measurement defect. `f2p_red_kind` is the readable record and lives where a cached verdict already carries the rest of its evidence.

### 8. What is deliberately NOT built

- No manifest key to opt a task into this shape. The predicate is evidence-based and measured per run; a key would let an author assert something the gate can already see, and a wrong assertion would be silent.
- No relaxation of `tests.runner` must contain `pytest`. Every exit code above is pytest's.
- No change to `run_matrix.py` or `grade.py`. `preflight_cache_key` picks up `PREFLIGHT_VERSION` on its own and `grade.py`'s resume gate reads `GRADER_VERSION` on its own.

---

## File Structure

| file | responsibility in this broadening |
|---|---|
| `bakeoff/src/bakeoff/preflight.py` | the two helpers, the exit constants, `_Runner.pass_to_pass(ignore=…)`, the deferred red-before verdict, the three evidence keys, `PREFLIGHT_VERSION` "4" |
| `bakeoff/src/bakeoff/grader.py` | check 5's confined-collection-error branch, `GRADER_VERSION` "3" |
| `bakeoff/tests/test_preflight.py` | the measured-against-real-pytest pins, the helper's unit pins, the `_ScriptedContainer` knobs, the argv-identity extension, the acceptance/refusal/green-after pins, and the **rewritten Phase 0c end-to-end pin** |
| `bakeoff/scripts/mutation_check.py` | the existing Phase 0c anchor's verifying test is repointed at the new unconfined fixture; one new anchor for the confinement equality |
| `bakeoff/tests/test_grader.py` | check 5's new branch, check 6 unchanged |
| `bakeoff/tests/test_oracle.py` | `_classify` still refuses 4 |
| `bakeoff/taskset/HARVESTING.md` | Layer 1 preflight bullets, Layer 2 "must IMPORT cleanly" paragraph |
| `docs/BUILDING-A-TASK-SET.md` | §3.4 (measuring f2p ids when the module does not collect), §3.7 refusal table |
| `docs/superpowers/specs/2026-08-17-offline-grader-design.md` | check 5 row + `f2p_failed_node_ids` note |
| `tasks/todo.md` | the review section |

---

### Task 1: The predicate — two helpers, two constants, measured against real pytest

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (the `EXIT_*` block, `_EXIT_MEANING`, and beside `failed_node_ids`)
- Test: `bakeoff/tests/test_preflight.py`

**Interfaces:**
- Produces: `EXIT_COLLECTION_INTERRUPTED: int = 2`, `EXIT_USAGE_ERROR: int = 4`, `EXIT_COLLECTION_FAILURES: tuple[int, int]`, `collection_error_modules(output: str) -> frozenset[str] | None`, `f2p_modules(f2p: tuple[str, ...]) -> frozenset[str]`. Tasks 3 and 5 import all of these.

- [ ] **Step 1: Write the failing tests**

Add to `bakeoff/tests/test_preflight.py`, in the "the distinction the gate is built on" section, right after `test_a_node_id_that_does_not_exist_is_a_usage_error`:

```python
def test_selecting_a_node_id_whose_module_will_not_import_is_exit_4_not_2(tmp_path):
    """The measurement this whole broadening turns on, taken against the real
    runner rather than asserted.

    `_Runner.select` passes node ids POSITIONALLY, and pytest answers a node id
    whose module raises on import with a USAGE ERROR (4), not with the
    collection-interrupted code (2). 2 is what a module-path or directory run
    gives -- which is how the `trucking-doc-extraction` #3 measurement was
    taken, and why an acceptance written for 2 alone would be dead code on
    every task preflight actually runs.

    Measured 2026-09-01 against pytest 9.1.1 (the venv and the base image pin)
    and pytest 8.3.5 (what `click-3360`'s image.pip pins); both agree.
    """
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "mypkg.py").write_text("def other():\n    return 1\n")
    (repo / "tests" / "test_new.py").write_text(
        "from mypkg import added_symbol\n\n\n"
        "def test_added():\n    assert added_symbol() == 1\n"
    )
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

    selected = subprocess.run(
        [*argv, "tests/test_new.py::test_added"],
        cwd=repo, capture_output=True, text=True,
    )
    whole_module = subprocess.run(
        [*argv, "tests/test_new.py"], cwd=repo, capture_output=True, text=True
    )

    assert selected.returncode == EXIT_USAGE_ERROR
    assert whole_module.returncode == EXIT_COLLECTION_INTERRUPTED
    # And both name the MODULE, with no `::`, in the summary.
    assert collection_error_modules(selected.stdout + selected.stderr) == {
        "tests/test_new.py"
    }


def test_nothing_runs_at_all_when_collection_fails(tmp_path):
    """So "every declared f2p id appears in FAILED/ERROR" is UNSATISFIABLE on
    this shape and has to become a claim about modules.

    A selection spanning a module that will not import and a module that
    imports and fails reports ONLY the collection error -- the failing test
    never runs. A gate that kept the id-level rule would refuse every task of
    this shape while believing it was checking something."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_broken.py").write_text("import nonexistent_module\n")
    (repo / "tests" / "test_red.py").write_text(
        "def test_red():\n    assert 1 == 2\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_broken.py::test_x", "tests/test_red.py::test_red"],
        cwd=repo, capture_output=True, text=True,
    )

    assert result.returncode == EXIT_USAGE_ERROR
    # Measured (row J): the output names only the erroring module, so a
    # direct absence check is exact. A `.split(header)[-1]` form would return
    # the WHOLE output when the header is absent (row K), and assert nothing.
    assert "test_red.py" not in result.stdout + result.stderr


def test_every_erroring_module_is_reported_not_only_the_first(tmp_path):
    """The equality in preflight's acceptance rests on this and nothing else.

    If pytest stopped at the first import failure a two-module f2p set would
    report one module, the equality could never hold, and every such task would
    be refused for a reason that is a property of the reporter rather than of
    the task -- a gate that looks strict and is arbitrary."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    for name in ("test_one.py", "test_two.py"):
        (repo / "tests" / name).write_text("import nonexistent_module\n")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_one.py::test_a", "tests/test_two.py::test_b"],
        cwd=repo, capture_output=True, text=True,
    )

    assert result.returncode == EXIT_USAGE_ERROR
    assert collection_error_modules(result.stdout + result.stderr) == {
        "tests/test_one.py", "tests/test_two.py"
    }


def test_collection_error_modules_separates_a_dead_module_from_a_dead_test():
    """The discriminator is the absent `::`, and it is the whole parser.

    pytest writes `ERROR <module>` for a module that would not import and
    `FAILED <mod>::<test>` / `ERROR <mod>::<test>` for a test that failed or
    whose fixture blew up. Reading a fixture error as "the module did not
    import" would let a task through whose declared tests never ran for a
    reason the gate is supposed to refuse."""
    collection = (
        "ERROR: found no collectors for /repo/tests/new.py::test_added\n"
        "ERROR tests/new.py\n"
        "1 error in 0.01s\n"
    )
    fixture_error = (
        "ERROR tests/new.py::test_added - ValueError: closed file\n"
        "1 error in 0.01s\n"
    )
    mixed = "ERROR tests/new.py\nFAILED tests/other.py::test_x\n"

    assert collection_error_modules(collection) == {"tests/new.py"}
    assert collection_error_modules(fixture_error) is None
    assert collection_error_modules(mixed) is None


def test_a_typoed_node_id_is_not_a_collection_error():
    """The two exit-4 shapes have to stay apart: a manifest naming a test that
    was renamed upstream must keep stopping the matrix, not be accepted as
    "the module could not be collected".

    Measured: both `ERROR: not found:` and `ERROR: file or directory not
    found:` carry a COLON after ERROR, so `_FAILED_LINE` never matches them and
    the reported set is empty."""
    not_found = (
        "ERROR: not found: /repo/tests/a.py::test_gone\n"
        "(no match in any of [<Module a.py>])\n\n\nno tests ran in 0.00s\n"
    )
    no_file = (
        "ERROR: file or directory not found: tests/nope.py::test_x\n\n"
        "no tests ran in 0.00s\n"
    )

    assert collection_error_modules(not_found) is None
    assert collection_error_modules(no_file) is None


def test_f2p_modules_is_the_part_before_the_first_colons():
    """Parametrized ids carry `::` inside brackets on the RIGHT of the split,
    so splitting once from the left is the only correct reading."""
    assert f2p_modules(
        ("tests/a.py::test_one[x::y]", "tests/a.py::Klass::test_two",
         "tests/b.py::test_three")
    ) == {"tests/a.py", "tests/b.py"}
```

Extend the import block at the top of the file:

```python
from bakeoff.preflight import (
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_INTERRUPTED,
    EXIT_TESTS_FAILED,
    EXIT_USAGE_ERROR,
    collection_error_modules,
    f2p_modules,
    failed_node_ids,
    preflight,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "collection_error_modules or f2p_modules or exit_4 or nothing_runs or typoed"`
Expected: FAIL — `ImportError: cannot import name 'EXIT_COLLECTION_INTERRUPTED'`.

- [ ] **Step 3: Write the implementation**

In `bakeoff/src/bakeoff/preflight.py`, replace the `EXIT_*` block and `_EXIT_MEANING` with:

```python
EXIT_ALL_PASSED = 0
EXIT_TESTS_FAILED = 1
#: Collection was interrupted. Reached by a run that collects a DIRECTORY or a
#: MODULE PATH -- not by the f2p selection, which is positional node ids. See
#: EXIT_USAGE_ERROR.
EXIT_COLLECTION_INTERRUPTED = 2
#: Two causes, and telling them apart is the whole of broadening 2. Measured
#: 2026-09-01 against pytest 9.1.1 and 8.3.5: selecting `mod.py::test` when
#: `mod.py` raises on import exits 4 with an `ERROR mod.py` summary line, and
#: selecting a node id that does not exist in a module that imports fine ALSO
#: exits 4 -- with `ERROR: not found:` (colon), which `_FAILED_LINE` does not
#: match, so the reported set is empty. The first is a task shape to accept;
#: the second is a manifest typo that must keep stopping the matrix.
EXIT_USAGE_ERROR = 4
#: The scoped run's failure mode, and the whole of its detection. No "collected
#: 0 items" summary matching: the pinned runners carry `-q`, which suppresses
#: that line (measured), so a guard on the string could not fire under the
#: configuration actually used -- the dead-guard shape a mutation cannot catch.
#: `-q` does NOT suppress "Interrupted: N error during collection" (also
#: measured); nothing parses that line and nothing should.
EXIT_NOTHING_COLLECTED = 5
#: The two codes a collection error can arrive as. 4 first, because that is the
#: one preflight's own f2p run produces.
EXIT_COLLECTION_FAILURES = (EXIT_USAGE_ERROR, EXIT_COLLECTION_INTERRUPTED)
_EXIT_MEANING = {
    2: "collection was interrupted (an import error in a test module, most "
       "often a dependency the image does not ship)",
    3: "pytest hit an internal error",
    4: "usage error -- a selected node id does not exist, OR the module it "
       "names could not be imported",
    5: "no tests were collected",
    124: "the command hit the preflight timeout",
}
```

Add below `failed_node_ids`:

```python
def collection_error_modules(output: str) -> frozenset[str] | None:
    """The test modules pytest could not COLLECT -- or `None` for anything else.

    Section 3.3's loop ends in "runs tests, sees failures, self-corrects", and
    a PR that ADDS a symbol hands the agent an `ImportError` instead of an
    assertion. That is a real task shape, and it is one preflight refused
    outright until broadening 2, because both of its exit codes (4 for a
    selection, 2 for a directory sweep) are also what a broken image gives.

    The discriminator is `::`, and it is the whole parser. Measured 2026-09-01
    against pytest 9.1.1 and 8.3.5, under the pinned `-q -p no:cacheprovider`:

    * a module that raised on import          -> `ERROR tests/new.py`
    * a test that failed                      -> `FAILED tests/new.py::test_x`
    * a test whose fixture raised             -> `ERROR tests/new.py::test_x`
    * a node id that does not exist           -> `ERROR: not found: ...`
    * a path that does not exist              -> `ERROR: file or directory ...`

    The last two carry a COLON after `ERROR`, so `_FAILED_LINE` never matches
    them and they arrive here as an empty set -- which is refused, because a
    manifest naming a renamed test must keep stopping the matrix rather than
    being read as "the module could not be collected".

    `None` rather than an empty frozenset for the refusal: "no collection
    errors" and "collection errors mixed with test results" are different
    facts, and a caller comparing an empty set against the declared modules
    would silently accept the second on a task that declares no f2p ids.

    The comparison against `f2p_modules` is deliberately NOT made here. The two
    callers need different ones -- preflight equality, the grader containment
    (see the offline-grader spec, check 5) -- and folding both behind a flag
    would make each call site unreadable about which claim it is making.
    """
    reported = failed_node_ids(output)
    if not reported or any("::" in item for item in reported):
        return None
    return frozenset(reported)


def f2p_modules(f2p: tuple[str, ...]) -> frozenset[str]:
    """The module half of each declared f2p node id.

    Split once, from the LEFT: a parametrized id can carry `::` inside its
    brackets (`tests/a.py::test_one[x::y]`) and a class-scoped id carries two,
    so `rsplit` or an unbounded `split` would name something that is not a
    module and the equality in `preflight` would never hold.
    """
    return frozenset(node_id.split("::", 1)[0] for node_id in f2p)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py
git commit -m "feat: a module that will not import exits 4, not 2, and the gate could not see the difference

preflight's f2p run selects node ids POSITIONALLY, and pytest answers a node
id whose module raises on import with a usage error -- exit 4 -- not with the
collection-interrupted code. Measured 2026-09-01 against pytest 9.1.1 and
8.3.5. An acceptance written for exit 2 alone would be dead code on every task
the gate actually runs.

collection_error_modules reads the one thing that separates a dead module from
a dead test: pytest writes ERROR <module> for the first and FAILED/ERROR
<module>::<test> for the second, and the two exit-4 typo shapes carry a colon
after ERROR that _FAILED_LINE has never matched. Without that separation a
manifest naming a renamed test reads as a task shape to accept.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `_Runner.pass_to_pass(ignore=…)`, inert by default

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (`_Runner.pass_to_pass`)
- Test: `bakeoff/tests/test_preflight.py` (`test_grading_p2p_with_no_extras_is_the_argv_preflight_validated`)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `_Runner.pass_to_pass(tests, extra_deselect=(), scope=(), ignore=())`. Task 3 passes `ignore`; `oracle._derive` and `grader._check_p2p` never do.

- [ ] **Step 1: Write the failing test**

Replace the body of `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` with the version below (the first two literals are unchanged; the third assertion is new and the docstring gains a paragraph):

```python
def test_grading_p2p_with_no_extras_is_the_argv_preflight_validated():
    """The whole reason the grader goes through this method rather than
    hand-building the branch: with every keyword argument left at its default
    the argv is byte-identical to the one the gate validated. The moment the
    graded command and the gated command drift apart, the oracle stops
    describing the thing being graded.

    Written against the LITERAL argv rather than against a second call of the
    same method: `pass_to_pass(t) == pass_to_pass(t, extra_deselect=(),
    scope=(), ignore=())` is symmetric and holds no matter what the body emits,
    so it would stay green through an inserted flag or a reordered segment --
    the two changes the property exists to catch. Both branches are spelled out
    because the explicit-p2p branch has its own `*extra` splice.

    `ignore` is the third keyword and the one broadening 2 added. It is used by
    ONE caller (preflight's p2p run at the START state, which the grader never
    makes) and defaults inert everywhere else, which is what keeps this literal
    true for the run the grader does make. It is spliced into `extra`, so it
    reaches BOTH branches -- and on the explicit-`tests.p2p` branch it is a
    NO-OP, because that branch selects node ids and pytest imports only the
    modules those ids name. Spliced there anyway rather than guarded: one
    splice is one thing to keep right, and a guard would be a second place the
    two branches could diverge, for a saving of nothing.
    """
    from bakeoff.preflight import _Runner

    deselect_branch = _Recorder()
    _Runner(deselect_branch, _Tests().runner, 60).pass_to_pass(_Tests())
    assert deselect_branch.commands == [
        ["timeout", "60", "python", "-m", "pytest", "-q",
         "--deselect", "tests/a.py::test_one"]
    ]

    explicit_branch = _Recorder()
    tests = _Tests(p2p=("tests/b.py::test_two",))
    _Runner(explicit_branch, tests.runner, 60).pass_to_pass(tests)
    assert explicit_branch.commands == [
        ["timeout", "60", "python", "-m", "pytest", "-q",
         "tests/b.py::test_two"]
    ]

    # Passing the new keywords empty is the same thing as omitting them --
    # checked against the same literal, never against the other call, so the
    # comparison cannot pass by symmetry.
    supplied = _Recorder()
    _Runner(supplied, _Tests().runner, 60).pass_to_pass(
        _Tests(), extra_deselect=(), scope=(), ignore=()
    )
    assert supplied.commands == deselect_branch.commands


def test_ignore_lands_as_one_flag_per_path_after_the_deselects():
    """Position is pinned, not just presence.

    `--ignore=<path>` is one argument, not a flag and a value: measured
    2026-09-01, pytest 9.1.1 and 8.3.5 both accept `--ignore=tests/x.py` and
    both silently accept a path that does not exist (so a stale entry degrades
    to no effect rather than to the exit-4 usage error the gate would refuse).
    A test that only asserted "the string appears somewhere" would stay green
    through a splice that put it before the runner, where it is not a pytest
    argument at all."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    _Runner(recorder, _Tests().runner, 60).pass_to_pass(
        _Tests(), ignore=("tests/new.py", "tests/other.py")
    )

    assert recorder.commands == [
        ["timeout", "60", "python", "-m", "pytest", "-q",
         "--deselect", "tests/a.py::test_one",
         "--ignore=tests/new.py", "--ignore=tests/other.py"]
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "argv_preflight_validated or ignore_lands"`
Expected: FAIL — `pass_to_pass() got an unexpected keyword argument 'ignore'`.

- [ ] **Step 3: Write the implementation**

In `bakeoff/src/bakeoff/preflight.py`, change `_Runner.pass_to_pass`'s signature and `extra` construction (the rest of the method and the whole docstring above the new paragraph stay as they are):

```python
    def pass_to_pass(self, tests, extra_deselect: tuple[str, ...] = (),
                     scope: tuple[str, ...] = (),
                     ignore: tuple[str, ...] = ()):
        """The p2p set: whatever the manifest declared, or everything else.

        ... (existing docstring unchanged) ...

        `ignore` appends `--ignore=<path>` and has exactly ONE caller:
        preflight's p2p run at the START state, on a task whose f2p module does
        not import there. That run sweeps the rootdir, so the erroring module
        aborts collection before `--deselect` is ever applied -- measured
        2026-09-01, pytest 9.1.1 and 8.3.5, exit 2 -- and the p2p baseline the
        acceptance depends on cannot be observed at all without it. The
        flag is spliced into both branches below, so on a task with an
        explicit `tests.p2p` it is emitted and inert -- positional ids never
        collect the f2p module -- which is why one splice point, not two, is
        the honest shape.

        It is safe here and only here because preflight's p2p-BEFORE run is not
        an argv the grader ever makes: the graded p2p runs at the
        post-submission state, and the preflight run that must match it
        byte-for-byte is p2p-AFTER, which gets no `ignore`. What the ignore
        hides -- a non-f2p test inside the ignored module -- is measured by
        that same p2p-after run, which collects the module once the reference
        lands and must exit 0.
        """
        extra = [arg for node_id in extra_deselect
                 for arg in ("--deselect", node_id)]
        extra += [f"--ignore={path}" for path in ignore]
        if tests.p2p:
            return self.run([*tests.p2p, *extra])
        args: list[str] = [*scope]
        for node_id in tests.f2p:
            args += ["--deselect", node_id]
        return self.run([*args, *extra])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py tests/test_grader.py tests/test_oracle.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py
git commit -m "feat: the p2p baseline cannot be observed at all when an f2p module does not import

preflight's p2p run at the start state sweeps the rootdir, so a test module
that raises on import aborts collection before --deselect is applied -- exit 2,
measured against pytest 9.1.1 and 8.3.5 on the p2p-before argv verbatim. The
acceptance in the next commit rests on that baseline being green, and without
--ignore there is no baseline to read.

The flag goes on that one run and no other. preflight's p2p-BEFORE is not an
argv the grader makes; p2p-AFTER is, and it keeps the literal the argv-identity
test pins. --continue-on-collection-errors was measured and rejected: it leaves
the f2p selection at exit 4 unchanged, and applying it to the graded p2p would
turn a broken import anywhere in the tree from an environment error into a
p2p_regression -- an accusation manufactured out of the environment.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Preflight accepts the confined shape

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (the red-before and p2p-green-before blocks, `PREFLIGHT_VERSION`)
- Test: `bakeoff/tests/test_preflight.py` (`_ScriptedContainer` knobs + four new tests)

**Interfaces:**
- Consumes: `EXIT_COLLECTION_FAILURES`, `collection_error_modules`, `f2p_modules` (Task 1); `pass_to_pass(ignore=…)` (Task 2).
- Produces: evidence keys `f2p_red_kind`, `f2p_collection_errors`, `p2p_before_ignored`; `PREFLIGHT_VERSION == "4"`.

- [ ] **Step 1: Write the failing tests**

First extend `_ScriptedContainer` so a test can script a collection-error run (add the four constructor parameters, the two counters and the p2p branch; everything else is unchanged):

```python
    def __init__(self, *, start_sha, tests, present=(), dangling=(),
                 scoped_exit=0, grading_exits=None,
                 f2p_before=None, f2p_after=None, p2p_before=None):
        ...                       # existing assignments unchanged
        #: Override the default red-before / green-after / p2p-before answers.
        #: Defaults stay the healthy exit-1 task so a test states only the one
        #: thing it is about.
        self.f2p_before = f2p_before
        self.f2p_after = f2p_after
        self.p2p_before = p2p_before
        self.p2p_runs = 0
        self.p2p_argvs = []

    def _timeout(self, argv):
        runner = list(self.tests.runner)
        if argv[: len(runner)] != runner:
            return _Exec(exit_code=self.grading_exits.get(tuple(argv), 0))
        rest = argv[len(runner):]
        if rest == list(self.tests.f2p):
            self.f2p_runs += 1
            if self.f2p_runs == 1:  # red before the reference fix
                return self.f2p_before or _Exec(
                    exit_code=EXIT_TESTS_FAILED,
                    stdout="".join(f"FAILED {n}\n" for n in self.tests.f2p),
                )
            return self.f2p_after or _Exec()
        if any(arg in self.tests.paths for arg in rest):
            self.scoped_runs += 1
            return _Exec(exit_code=self.scoped_exit)
        self.p2p_runs += 1
        self.p2p_argvs.append(list(rest))
        if self.p2p_runs == 1 and self.p2p_before is not None:
            return self.p2p_before
        return _Exec()
```

Then add these tests (a module-level constant first, so the three tests that need the scripted output share one copy):

```python
#: What pytest prints, under the pinned `-q -p no:cacheprovider`, when the
#: module holding a selected node id raises on import. Measured 2026-09-01,
#: pytest 9.1.1 and 8.3.5 identically -- the `ERROR:` lines carry a colon and
#: are not node ids; the summary line names the MODULE with no `::`.
_COLLECTION_ERROR_OUT = (
    "ERROR: found no collectors for /repo/tests/a.py::test_one\n"
    "\n"
    "==================================== ERRORS ===================================\n"
    "________________________ ERROR collecting tests/a.py __________________________\n"
    "ImportError while importing test module '/repo/tests/a.py'.\n"
    "tests/a.py:1: in <module>\n"
    "    from app import added_symbol\n"
    "E   ImportError: cannot import name 'added_symbol' from 'app'\n"
    "=========================== short test summary info ===========================\n"
    "ERROR tests/a.py\n"
    "1 error in 0.01s\n"
)


def test_an_f2p_module_that_will_not_import_is_accepted_when_p2p_is_green(
    monkeypatch, tmp_path
):
    """The broadening. A PR that ADDS a symbol puts it in the solution half, so
    the test half cannot import at the start state and pytest exits 4 -- the
    code that also means "a broken image".

    Acceptance is a THREE-way conjunction and never a parse: the errors are
    confined to the declared f2p modules, p2p is green before, AND f2p is green
    after. The f2p selection imports ONLY the f2p modules, so a dependency that
    only that module needs is confined AND leaves p2p green -- the first two
    conjuncts cannot see it and green-after is what refuses it (see
    `test_a_collection_error_after_the_reference_fix_is_still_a_refusal`).
    This container scripts all three healthy, which is what makes it a GO.
    """
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["f2p_before_exit"] == 4
    assert result.evidence["f2p_red_kind"] == "collection_error"
    assert result.evidence["f2p_collection_errors"] == ["tests/a.py"]
    assert result.evidence["p2p_before_ignored"] == ["tests/a.py"]
    # The ignore reached the run that needed it, and only that run.
    assert "--ignore=tests/a.py" in container.p2p_argvs[0]
    assert all("--ignore=tests/a.py" not in argv
               for argv in container.p2p_argvs[1:])


def test_an_ordinary_red_task_records_the_other_red_kind(monkeypatch, tmp_path):
    """Absence is recorded, never implied. "This task has no collection errors"
    and "the gate did not look" render identically as a missing key, so both
    keys are written on every path -- which is also what lets a reader of a
    cached verdict tell the two task shapes apart."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["f2p_red_kind"] == "failed"
    assert result.evidence["f2p_collection_errors"] == []
    assert result.evidence["p2p_before_ignored"] == []
    assert all("--ignore" not in " ".join(argv) for argv in container.p2p_argvs)


def test_a_collection_error_with_a_red_p2p_is_still_refused(monkeypatch, tmp_path):
    """The second conjunct, and what it does and does not rule out.

    p2p-green-before rules out a GLOBALLY broken image and establishes the
    regression baseline -- a p2p check against an already-red suite means
    nothing. It does NOT rule out a dependency imported only by the f2p module:
    that one is confined AND leaves p2p green, and green-after is what catches
    it. Naming the wrong conjunct here is how the first draft of this plan
    talked itself into a two-way guarantee.

    Note what this test does NOT assert: that the task is a NO-GO. It would be
    one either way, because the `if not p2p_green:` block below is
    unconditional. What the branch buys is a refusal that NAMES the coupling
    and prints the argv it ran, instead of an author on a collection-error task
    reading a bare "p2p is not green" with no way to know the two are related
    -- which is why the assertion is on the message and why the MUTATIONS entry
    for this broadening anchors on the equality instead."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
        p2p_before=_Exec(exit_code=EXIT_TESTS_FAILED,
                         stdout="FAILED tests/z.py::test_other\n"),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("could not be collected" in p and "p2p" in p
               for p in result.problems), result.problems
    assert result.evidence["f2p_red_kind"] == "collection_error"


def test_a_collection_error_outside_the_declared_f2p_modules_is_refused(
    monkeypatch, tmp_path
):
    """Equality, not containment, and in both directions.

    A module erroring that no f2p id names is a broken environment. A declared
    f2p module that did NOT error is a declared id nobody checked -- and
    measured, a partial collection error hides the rest of the selection
    entirely, so an id-level rule is unsatisfiable and a subset rule would
    silently accept the gap the id-level rule existed to close."""
    tests = _FakeTests(f2p=("tests/a.py::test_one", "tests/b.py::test_two"))
    task = _FakeTask(tests=tests)

    stranger = _ScriptedContainer(
        start_sha="s" * 40, tests=tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout="ERROR tests/zzz.py\n"),
    )
    result = _run_preflight(monkeypatch, tmp_path, task, stranger)
    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems)

    partial = _ScriptedContainer(
        start_sha="s" * 40, tests=tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout="ERROR tests/a.py\n"),
    )
    result = _run_preflight(monkeypatch, tmp_path, task, partial)
    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems)


def test_a_typoed_f2p_id_still_stops_the_matrix(monkeypatch, tmp_path):
    """Same exit code, opposite verdict. `ERROR: not found:` carries a colon,
    so nothing is reported and the confinement predicate refuses -- which is
    what keeps a manifest naming a renamed test from being read as a task
    shape to accept."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(
            exit_code=4,
            stdout="ERROR: not found: /repo/tests/a.py::test_one\n"
                   "(no match in any of [<Module a.py>])\n\nno tests ran\n",
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems)


def test_the_preflight_version_moved_with_what_the_gate_asserts():
    """It is in `preflight_cache_key`, and none of the other three components
    moves when this file changes: a manifest digest describes the task, an
    image id the environment, a start sha the tree. Without the bump every warm
    cache serves a verdict written by a gate that refused this task shape."""
    from bakeoff.preflight import PREFLIGHT_VERSION

    assert PREFLIGHT_VERSION == "4"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "will_not_import or red_kind or red_p2p or outside_the_declared or typoed_f2p or preflight_version_moved"`
Expected: FAIL — the acceptance tests fail on `result.ok` (the gate still refuses exit 4), and `PREFLIGHT_VERSION` is `"3"`.

- [ ] **Step 3: Write the implementation**

In `bakeoff/src/bakeoff/preflight.py`, bump the version and extend its comment:

```python
#: ... (existing comment) ...
#: 3 adds the `strip_paths` assertion: a verdict cached under 2 was written
#: by a gate that never looked at that key at all.
#: 4 accepts an f2p run that could not COLLECT (broadening 2). A verdict
#: cached under 3 was written by a gate that refused that task shape outright,
#: and one cached under 4 was written by a gate whose p2p-before run carries
#: `--ignore` on exactly those tasks.
PREFLIGHT_VERSION: str = "4"
```

Then replace the red-before and p2p-green-before blocks with the deferred-verdict form:

```python
        # --- red before, and the p2p baseline it is judged against
        #
        # Two runs, ONE verdict, and the verdict comes last. A task whose fix
        # ADDS a symbol puts that symbol in the solution half, so the test half
        # raises ImportError at the start state and pytest exits 4 (measured
        # 2026-09-01, pytest 9.1.1 and 8.3.5: a positional NODE ID whose module
        # will not import is a usage error, not the collection-interrupted 2 a
        # directory sweep gives). That is a real task shape -- section 3.3's
        # loop still runs, the agent just reads an ImportError instead of an
        # assertion, which is exactly what the human who filed the issue read.
        #
        # It is accepted only under a THREE-way conjunction, because the parse
        # alone cannot carry it -- the f2p selection imports ONLY the f2p
        # modules, so an environment defect there produces exactly the confined
        # error set the task shape produces. The three, and what each one and
        # only it can see:
        #
        #   (i)   the errors are confined to the declared f2p modules -- no
        #         stranger module errored, and no declared module stayed quiet;
        #   (ii)  p2p is green BEFORE -- a globally broken image is refused
        #         here, and the regression baseline exists at all;
        #   (iii) f2p is green AFTER -- and this is the ONLY one that refuses a
        #         dependency imported solely by the f2p module, which is
        #         confined under (i) and leaves (ii) green. That is exactly the
        #         Phase 0c integration fixture.
        #
        # (iii) is asserted unconditionally by the green-after block further
        # down and needs nothing here. It is named here because a reader who
        # believes (i) and (ii) suffice will eventually simplify it away.
        #
        # The runs stay in this order -- their order is what each sees of the
        # tree -- and the JUDGEMENT is deferred instead. Problems are collected
        # rather than raised, so nothing else about this function has to move.
        red = runner.select(tests.f2p)
        evidence["f2p_before_exit"] = red.exit_code

        collected = (
            collection_error_modules(red.stdout + red.stderr)
            if red.exit_code in EXIT_COLLECTION_FAILURES else None
        )
        # EQUALITY, in both directions. A module erroring that no f2p id names
        # is a broken environment; a declared f2p module that did NOT error is
        # a declared id nobody checked -- and measured, a partial collection
        # error hides the rest of the selection entirely, so the id-level rule
        # ("every declared f2p id appears in FAILED/ERROR") is unsatisfiable
        # here and this is that same claim at module granularity.
        confined = collected is not None and collected == f2p_modules(tests.f2p)
        # Written on every path, all three keys: "this task has no collection
        # errors" and "the gate did not look" render identically as a missing
        # key, and a cached verdict outlives the code that wrote it.
        evidence["f2p_collection_errors"] = sorted(collected or ())
        # Four values, not three: "the task is already done" and "the gate
        # could not classify this run" are different facts about a cached
        # verdict, and one name for both is the defect one layer down that
        # "absence is recorded, never implied" exists to prevent.
        evidence["f2p_red_kind"] = (
            "collection_error" if confined
            else "failed" if red.exit_code == EXIT_TESTS_FAILED
            else "passed" if red.exit_code == EXIT_ALL_PASSED
            else "unknown"
        )

        # `--ignore` on THIS run only. Without it the erroring module aborts
        # collection of the whole rootdir sweep before `--deselect` is applied
        # (measured: exit 2 on the p2p-before argv verbatim), so the baseline
        # this acceptance depends on cannot be observed at all. Safe here
        # because preflight's p2p-BEFORE is not an argv the grader makes: the
        # graded run is at the post-submission state, matched by p2p-AFTER,
        # which gets no ignore -- and the one thing the ignore hides, a non-f2p
        # test inside the ignored module, is measured by that same p2p-after
        # run once the reference lands.
        ignore = tuple(sorted(collected)) if confined else ()
        evidence["p2p_before_ignored"] = list(ignore)
        green = runner.pass_to_pass(tests, ignore=ignore)
        evidence["p2p_before_exit"] = green.exit_code
        p2p_green = green.exit_code == EXIT_ALL_PASSED

        if red.exit_code == EXIT_ALL_PASSED:
            problems.append(
                "the f2p tests PASS at the start state: the task is already "
                "done, and every arm would be scored on work it did not do"
            )
        elif confined and p2p_green:
            pass  # accepted: the bug is a collection error, and it is confined
        elif confined:
            problems.append(
                "the f2p tests could not be collected at the start state "
                f"({', '.join(sorted(collected))}), which is an accepted task "
                "shape ONLY while the rest of the suite is green there -- and "
                "the p2p run exited "
                f"{green.exit_code} ({_explain(green.exit_code)}). The f2p "
                "selection imports only the f2p modules, so a broken image "
                "produces exactly this error set; p2p is what separates them. "
                "If the run below collected nothing, this task's test tree "
                "holds no regression baseline outside the erroring module. If "
                "it still reports that module, the --ignore missed: those "
                "paths come from pytest's ROOTDIR-relative ERROR lines and "
                "--ignore resolves against the working directory, and an "
                "--ignore naming a path that does not exist is accepted "
                "silently (measured).\n"
                f"  {' '.join(runner.last_argv)}\n"
                + (green.stdout or green.stderr)[-2000:]
            )
        elif red.exit_code != EXIT_TESTS_FAILED:
            problems.append(
                f"the f2p tests did not run at the start state -- "
                f"{_explain(red.exit_code)}. This is the Phase 0c failure: a "
                "broken environment is also a non-zero exit, and an agent "
                "reading the output cannot tell it from the bug. A collection "
                "error IS accepted, but only when every reported ERROR names a "
                "declared f2p module and no other, and here the reported set "
                f"is {sorted(collected) if collected else 'empty'} against "
                f"declared {sorted(f2p_modules(tests.f2p))}.\n"
                + (red.stdout or red.stderr)[-2000:]
            )
        else:
            reported = failed_node_ids(red.stdout + red.stderr)
            missing = set(tests.f2p) - reported
            if missing:
                problems.append(
                    "declared f2p tests did not fail at the start state: "
                    + ", ".join(sorted(missing))
                )

        if not p2p_green:
            problems.append(
                f"the rest of the suite is not green at the start state -- "
                f"{_explain(green.exit_code)}. A p2p regression check against "
                "an already-red suite cannot mean anything.\n"
                + (green.stdout or green.stderr)[-2000:]
            )
```

- [ ] **Step 4: Rewrite the Phase 0c end-to-end pin — it is now testing something else**

`bakeoff/tests/test_preflight.py:325-372`, `test_a_task_whose_tests_cannot_even_run_is_refused`, is the integration pin for the whole reason preflight exists, and **this broadening silently changes what it proves**. Its fixture's broken import — `import a_module_this_image_does_not_have` — lives inside `tests/test_calc.py`, which *is* the declared f2p module (`test_preflight.py:170`). Measured on both pytest versions: the f2p select exits 4 reporting `ERROR tests/test_calc.py`, so `confined` is **True**; the smoke fixture's `tests/` holds only that module, so p2p-before with `--ignore=tests/test_calc.py` collects nothing and exits **5**. The task is still refused, but by the new `elif confined:` branch, whose message contains nothing the assertion at `:371` looks for. **The test fails, and the Phase 0c exit-code branch loses its end-to-end cover.**

Split it into two integration tests. The first keeps the existing fixture and pins the new outcome; the second restores Phase 0c cover with a fixture whose failure is **unconfined**:

```python
@pytest.mark.integration
def test_a_confined_collection_error_with_no_p2p_baseline_is_refused(
    tmp_path, agent_image
):
    """The Phase 0c fixture, and what broadening 2 made of it.

    Its broken import lives INSIDE the declared f2p module, so the error set is
    confined and the exit-code branch no longer owns this input. What refuses
    it now is the second conjunct: this fixture's `tests/` holds only that one
    module, so ignoring it leaves the rootdir sweep with nothing to collect
    (pytest exit 5) and there is no regression baseline at all.

    Kept as an integration test rather than folded into the scripted ones
    because the exit codes it turns on -- 4 from the selection, 5 from the
    ignored sweep -- are the real runner's, in the real image, and that is the
    whole reason this file has an integration half."""
    test_half = (
        "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
        "--- a/tests/test_calc.py\n"
        "+++ b/tests/test_calc.py\n"
        "@@ -1,5 +1,10 @@\n"
        " from calc import add\n"
        " \n"
        " \n"
        " def test_zero():\n"
        "     assert add(0, 0) == 0\n"
        "+\n"
        "+import a_module_this_image_does_not_have\n"
        "+\n"
        "+def test_add():\n"
        "+    assert add(2, 3) == 5\n"
    )
    solution = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    task = load_task(
        _smoke_task(tmp_path, "def add(a, b):\n    return a - b\n",
                    test_half + solution)
    )
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert result.evidence["f2p_red_kind"] == "collection_error"
    assert result.evidence["p2p_before_ignored"] == ["tests/test_calc.py"]
    assert result.evidence["p2p_before_exit"] == EXIT_NOTHING_COLLECTED
    assert any("could not be collected" in p and "p2p" in p
               for p in result.problems), result.problems
    # And green-after refuses it independently: the dependency is still absent.
    assert result.evidence["f2p_after_exit"] != EXIT_ALL_PASSED


@pytest.mark.integration
def test_a_task_whose_tests_cannot_even_run_is_refused(tmp_path, agent_image):
    """The Phase 0c failure, reproduced end to end -- with a fixture broadening
    2 cannot absorb.

    Same defect as before: a module the image does not have. It lives in
    `tests/conftest.py` rather than in the f2p module, which is what keeps the
    exit-code branch owning this input. Measured 2026-09-01, pytest 9.1.1 and
    8.3.5 alike: a broken conftest under a node-id selection exits 4 and prints
    NO `short test summary info` section at all -- no `ERROR <path>` line -- so
    the reported set is EMPTY, `collection_error_modules` returns `None`, and
    the run is unconfined.

    That is what `assert returncode != 0` reads as "the bug is present", which
    is exactly what happened for the whole of Phase 0c: `python3
    tests/test_calc.py` raised ModuleNotFoundError with the bug fixed and
    unfixed alike, Gemma burned 30 of 30 turns on it, and the 9/9 was recorded
    as capability.

    Refusing here costs a message. Accepting it costs a matrix."""
    task = load_task(_smoke_task(
        tmp_path, "def add(a, b):\n    return a - b\n",
        _reference(fix_source=True),
        extra_files={"tests/conftest.py":
                     "import a_module_this_image_does_not_have\n"},
    ))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert result.evidence["f2p_red_kind"] == "unknown"
    assert result.evidence["f2p_collection_errors"] == []
    assert result.evidence["p2p_before_ignored"] == []
    assert any("did not run at the start state" in p for p in result.problems), (
        result.problems
    )
```

`EXIT_NOTHING_COLLECTED` joins the imports at the top of the file.

- [ ] **Step 5: Repoint one mutation anchor and add one**

`bakeoff/scripts/mutation_check.py:726-736` reverts `elif red.exit_code != EXIT_TESTS_FAILED:` to `elif False:` and verifies with `tests/test_preflight.py -k cannot_even_run`. The anchor line survives this broadening verbatim, but under the **old** fixture that input is intercepted by the earlier `elif confined:` branch, so the mutation would change nothing and the check would go green on a reverted guarantee. Step 4's replacement fixture is unconfined, so the anchor is live again — change only the verifying selector and extend the comment:

```python
    (
        # The Phase 0c failure in one operator: ModuleNotFoundError is also a
        # non-zero exit, so `!= 0` accepts a broken environment as "the bug is
        # present" and every arm is scored on a task that was never runnable.
        # The verifying fixture's broken import is in `tests/conftest.py`, NOT
        # in the f2p module: since PREFLIGHT_VERSION 4 a confined collection
        # error is an accepted task shape and is handled by an EARLIER branch,
        # so a fixture that is confined would leave this mutation inert.
        "preflight: accept any non-zero exit as evidence the bug is present",
        "src/bakeoff/preflight.py",
        "        elif red.exit_code != EXIT_TESTS_FAILED:",
        "        elif False:",
        "tests/test_preflight.py -k cannot_even_run",
        "integration",
    ),
```

Then add one entry for this broadening. It anchors on the **confinement equality**, not on the `and p2p_green` conjunct the acceptance prose names, and that choice is deliberate: `if not p2p_green:` is unconditional, so reverting the conjunct changes the refusal *message* and not the verdict, and a mutation entry that guards a message would report a guarantee this repo does not have. Dropping the equality DOES flip a NO-GO into a GO — a stranger module's error, or a declared f2p module that never errored, both become "confined", the task passes with p2p green, and an id nobody checked is in the matrix:

```python
    (
        # The confinement equality is the whole of the acceptance. Without it
        # `collected is not None` is true for any collection error at all, so a
        # module no f2p id names -- a dependency the image lost -- reads as the
        # task shape, and a DECLARED f2p module that never errored reads as
        # checked when a collection error hid it. Both are GO under the
        # mutation, with the rest of the gate green.
        "preflight: accept any collection error, not one confined to the f2p modules",
        "src/bakeoff/preflight.py",
        "        confined = collected is not None and collected == f2p_modules(tests.f2p)",
        "        confined = collected is not None",
        "tests/test_preflight.py -k outside_the_declared_f2p_modules",
        "not integration",
    ),
```

Both other preflight anchors — line 1632 (`extra = [arg for node_id in extra_deselect…`) and the grader's line 1539 (`state.environment(\n        "f2p",`) — survive verbatim; Task 2 appends to `extra` on the line *after* the anchored one and Task 5 inserts *above* the anchored call.

- [ ] **Step 6: Run everything**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS. Then the integration leg, because this changes what runs inside a real container:
`cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`
Expected: PASS, including both tests from Step 4. Confirm `git diff bakeoff/taskset/` is empty. Then, **solo**, `cd bakeoff && .venv/bin/python scripts/mutation_check.py` — expected: PASS, with the two entries from Step 5 both going red under mutation.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py \
        bakeoff/scripts/mutation_check.py
git commit -m "feat: a task whose f2p module cannot be COLLECTED is a task, when the errors are confined

A PR that ADDS a symbol puts it in the solution half, so the test half raises
ImportError at the start state and preflight refused it as a broken
environment. That refusal cost the corpus a whole PR shape -- measured on
trucking-doc-extraction #3 -- and section 3.3's loop still runs on it; the
agent reads an ImportError instead of an assertion, which is what the human who
filed the issue read.

Accepted under a THREE-way conjunction, never a parse: the errors are confined
to the declared f2p modules, p2p is green before, and f2p is green after. The
f2p selection imports only the f2p modules, so a dependency only that module
needs is confined AND leaves p2p green -- green-after is the only conjunct that
refuses it, which is exactly what the Phase 0c fixture is. The two before-runs
keep their order (their order is what each sees of the tree) and the verdict is
deferred to after the second.

That fixture's broken import is IN the f2p module, so it is confined now and the
exit-code branch no longer owns it -- the end-to-end Phase 0c pin is split in
two, and its replacement puts the missing module in tests/conftest.py, which
exits 4 with no summary section at all and is therefore unconfined. The
mutation anchor on that branch is repointed at it, because a confined fixture
would have left the mutation inert and the check green on a reverted guarantee.

The comparison is EQUALITY in both directions: a module erroring that no f2p id
names is a broken environment, and a declared f2p module that did not error is
a declared id nobody checked. Measured, a partial collection error hides the
rest of the selection, so the id-level rule cannot hold and this is that rule
at module granularity.

f2p_red_kind, f2p_collection_errors and p2p_before_ignored are written on every
path: a cached verdict outlives the gate that wrote it, and a missing key
cannot say which of the two task shapes it describes. PREFLIGHT_VERSION 3 -> 4.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Green-after is the environment discriminator, and it stays strict

**Files:**
- Test only: `bakeoff/tests/test_preflight.py`

**Interfaces:**
- Consumes: `_ScriptedContainer(f2p_after=…)` (Task 3), `_COLLECTION_ERROR_OUT` (Task 3).
- Produces: nothing. This task adds no source change; it exists because the relaxation it forbids is the one a future reader will find tempting for symmetry.

- [ ] **Step 1: Write the failing test**

```python
def test_a_collection_error_after_the_reference_fix_is_still_a_refusal(
    monkeypatch, tmp_path
):
    """The third conjunct, and the only one that can see this defect.

    Green-after is NOT relaxed, and the symmetry that suggests relaxing it is
    the trap. Red-before accepts a confined collection error because the
    missing symbol IS the bug; after the reference fix that symbol exists, so
    the module imports and the tests exit 0. A reference that leaves it
    unimportable has fixed nothing, and a solved run and an idle run would
    leave identical evidence -- the failure `preflight`'s module docstring
    opens with.

    It is also the ENVIRONMENT DISCRIMINATOR, which is the part that is easy to
    miss. A dependency imported only by the f2p module is confined under
    conjunct (i) -- the f2p selection imports only those modules -- and leaves
    p2p green under conjunct (ii), because p2p never imports it. Neither of the
    first two conjuncts can refuse it. This scripted shape is that defect
    exactly: the error set is perfectly confined and p2p is green, so nothing
    but this block keeps the refusal."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
        f2p_after=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("do NOT pass after the reference" in p for p in result.problems)
    assert result.evidence["f2p_after_exit"] == 4
```

- [ ] **Step 2: Run the test**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "collection_error_after_the_reference"`
Expected: PASS immediately — Task 3 deliberately left the green-after block untouched. **If it fails, Task 3 relaxed something it must not have; fix Task 3, do not weaken this test.**

Also assert the two things that make it the environment discriminator rather than a duplicate of Task 3's acceptance test, so a later reader cannot mistake it for one: `result.evidence["f2p_red_kind"] == "collection_error"` (conjunct (i) held) and `result.evidence["p2p_before_exit"] == EXIT_ALL_PASSED` (conjunct (ii) held). Both are already true of the scripted container above.

- [ ] **Step 3: (no implementation)**

There is nothing to write. The test is the deliverable.

- [ ] **Step 4: Run the whole suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/tests/test_preflight.py
git commit -m "test: green-after is the only conjunct that can see a dependency the f2p module alone needs

Red-before now accepts a confined collection error, under confined AND
p2p-green-before AND f2p-green-after. The third looks redundant and is not: the
f2p selection imports only the f2p modules and p2p imports none of them, so a
dependency needed solely by the f2p module is confined and leaves p2p green.
Neither of the first two conjuncts can refuse it. That is the Phase 0c fixture
exactly.

The symmetric relaxation of green-after is the tempting one and it is wrong:
after the fix the symbol exists, the module imports, the tests pass. The
scripted error set here is perfectly confined and p2p is green, so the only
thing keeping the refusal is the absence of that branch from this block --
which is exactly what this pins.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The grader — check 5 names the failure instead of refusing to grade

**Files:**
- Modify: `bakeoff/src/bakeoff/grader.py` (`GRADER_VERSION`, `_check_f2p`, the import block)
- Test: `bakeoff/tests/test_grader.py`, `bakeoff/tests/test_oracle.py`

**Interfaces:**
- Consumes: `collection_error_modules`, `f2p_modules` (Task 1).
- Produces: `GRADER_VERSION == "3"`. Nothing else in the codebase reads the new branch.

- [ ] **Step 1: Write the failing tests**

In `bakeoff/tests/test_grader.py`. Every test below goes through the file's own `_ladder(...)` helper (`test_grader.py:263`) rather than calling `run_ladder` directly — the helper supplies `START_SHA` (`test_grader.py:103`), which is the start sha the ladder checks out; `BASE_SHA` on the next line is a *different* constant and passing it would be checking out the wrong tree:

```python
def test_an_unfixed_collection_error_is_a_named_failure_not_an_ungradable_run():
    """Otherwise a do-nothing arm outranks a half-working one.

    On a task whose f2p module does not import at the start state, an arm that
    changed nothing leaves it not importing: pytest exits 4 and check 5 used to
    call that ENVIRONMENT_ERROR -- `resolved: None`, not graded. An arm that
    half-fixed the import gets exit 1 and `resolved: False`. So in any view
    that counts False, the arm that did NOTHING looks better than the one that
    tried. A null standing in for a negative is the defect this ladder is built
    around, and here it was pointing the wrong way.

    Containment, not equality: a submission that fixed one of two f2p modules
    errors on a subset and is still a model failure. Anything OUTSIDE the
    declared modules stays an environment error -- see the next test."""
    env = FakeEnv(rules=[
        (is_f2p, (4,
                  "ERROR: found no collectors for /repo/tests/test_calc.py::test_add\n"
                  "ERROR tests/test_calc.py\n1 error in 0.01s\n",
                  "")),
    ])

    result = _ladder(env=env)

    assert result.resolved is False
    assert result.grade_failure is GradeFailure.F2P_FAILED
    assert result.not_graded_reason is None
    assert result.environment_error is None
    # Observation, verbatim: pytest reported a MODULE, and a module is a node
    # id -- the collector node. The absent `::` is what says so.
    assert result.f2p_failed_node_ids == ("tests/test_calc.py",)


def test_a_collection_error_outside_the_f2p_modules_stays_an_environment_error():
    """A `False` is an accusation. A module erroring that no f2p id names is
    indistinguishable from an image that lost a dependency, and stamping
    f2p_failed on it would blame the model for the grader's environment. This
    is also exactly what today's exit-2 route already does, so the branch above
    is not allowed to widen it."""
    env = FakeEnv(rules=[
        (is_f2p, (4, "ERROR tests/test_unrelated.py\n1 error in 0.01s\n", "")),
    ])

    result = _ladder(env=env)

    assert result.resolved is None
    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value
    assert result.environment_error_check == "f2p"
    assert result.grade_failure is None


def test_a_typoed_f2p_id_at_grade_time_is_still_an_environment_error():
    """Same exit code, and the manifest is the thing that is wrong. `ERROR: not
    found:` carries a colon, so nothing is reported, the predicate refuses, and
    the grade does not accuse the model for the task author's typo."""
    env = FakeEnv(rules=[
        (is_f2p, (4, "ERROR: not found: /repo/tests/test_calc.py::test_add\n"
                     "(no match in any of [<Module test_calc.py>])\n", "")),
    ])

    result = _ladder(env=env)

    assert result.resolved is None
    assert result.environment_error_check == "f2p"


def test_check_6_still_reads_a_collection_error_as_an_environment_error():
    """Check 6 is deliberately UNCHANGED. It is only reached when check 5
    passed, which means every f2p module imported -- so a bare-module ERROR
    there names something outside the task, and that is the environment. The
    p2p argv is untouched too: no `--ignore`, no
    `--continue-on-collection-errors`, so the graded command is still the one
    preflight validated."""
    env = FakeEnv(rules=[
        (is_p2p, (2, "ERROR tests/test_other.py\n"
                     "!!! Interrupted: 1 error during collection !!!\n", "")),
    ])

    result = _ladder(env=env)

    assert result.resolved is None
    assert result.environment_error_check == "p2p"
    p2p_argv = next(argv for argv in env.argvs if is_p2p(argv))
    assert not any(arg.startswith("--ignore") for arg in p2p_argv)
    assert "--continue-on-collection-errors" not in p2p_argv


def test_the_grader_version_moved_with_what_check_5_means():
    """It gates resume -- a run already graded under the current grader is
    skipped -- so a stored grade the gate skipped for agreeing with "the
    current grader" would otherwise be one this grader disagrees with."""
    from bakeoff.grader import GRADER_VERSION

    assert GRADER_VERSION == "3"
```

In `bakeoff/tests/test_oracle.py`:

```python
def test_the_quarantine_still_refuses_a_run_that_could_not_collect():
    """The oracle is deliberately UNCHANGED by broadening 2.

    Both of its runs are at the POST-FIX state, where preflight has already
    proved f2p exits 0 -- so no collection error can reach it. If one does, the
    polarity is the worst in the codebase: a broken run reports no failed node
    ids, so a classifier that softened here would read a suite that never
    collected as a clean run and derive an empty quarantine from two of them."""
    from types import SimpleNamespace

    from bakeoff.oracle import OracleError, _classify

    with pytest.raises(OracleError) as excinfo:
        _classify(SimpleNamespace(
            exit_code=4, stdout="ERROR tests/a.py\n1 error in 0.01s\n",
            stderr="",
        ))

    assert "exited 4" in str(excinfo.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py tests/test_oracle.py -q -k "collection_error or typoed_f2p_id_at_grade or grader_version_moved or could_not_collect"`
Expected: the first test FAILS (`resolved is None`, `ENVIRONMENT_ERROR`), `test_the_grader_version_moved_with_what_check_5_means` FAILS (`"2" != "3"`), and the other four PASS already — they pin behaviour the new branch must not widen.

- [ ] **Step 3: Write the implementation**

In `bakeoff/src/bakeoff/grader.py`, extend the preflight import block:

```python
from bakeoff.preflight import (
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_FAILURES,
    EXIT_NOTHING_COLLECTED,
    EXIT_TESTS_FAILED,
    PREFLIGHT_VERSION,
    _existing_prefixes,
    _Runner,
    collection_error_modules,
    f2p_modules,
    failed_node_ids,
)
```

Bump the version and extend its comment:

```python
#: ... (existing comment) ...
#: 2 -> 3: check 5 reads a CONFINED collection error as `f2p_failed` rather
#: than as an environment error (broadening 2). On a task whose f2p module does
#: not import at the start state, an arm that changed nothing left it not
#: importing and graded as NOT GRADED, while an arm that half-fixed it graded
#: `False` -- so the do-nothing arm was invisible in every view that counts
#: `False`. That is a change to what a check MEANS, so the version moves
#: whether or not anything was graded under 2.
GRADER_VERSION: str = "3"
```

Then, in `_check_f2p`, insert the new branch between the `_TIMEOUT_EXIT` branch and the environment fallback, and rewrite the trailing comment:

```python
    if code == _TIMEOUT_EXIT:
        state.fail("f2p", GradeFailure.F2P_FAILED, result, timed_out=True,
                   detail=f"hit the {GRADE_TIMEOUT_S}s grading timeout")

    # A collection error CONFINED to this task's own f2p modules. Preflight
    # (version 4 or later) accepts a task whose f2p module does not import at
    # the start state, and an arm that changed nothing leaves it not importing:
    # exit 4, which the fallback below calls an environment error. That made
    # the do-nothing arm NOT GRADED while an arm that half-fixed the import
    # graded `False` -- so in any view counting `False` the arm that did
    # nothing looked better than the one that tried.
    #
    # CONTAINMENT here, where preflight uses equality. Preflight must account
    # for every declared id; this must never accuse for anything outside the
    # task, and a submission that fixed one of two f2p modules errors on a
    # subset and is still a model failure. A module outside the set is
    # indistinguishable from an image that lost a dependency, and an empty
    # reported set is a manifest typo (`ERROR: not found:` carries a colon) --
    # both fall through to the environment path, unchanged.
    if code in EXIT_COLLECTION_FAILURES:
        modules = collection_error_modules(result.stdout + result.stderr)
        if modules is not None and modules <= f2p_modules(tuple(task.tests.f2p)):
            # Observation, verbatim: pytest reported MODULES, and a module is a
            # node id -- the collector node. A reader tells them from test ids
            # by the absent `::`, which is the same discriminator the parser
            # uses.
            state.f2p_failed_node_ids = tuple(sorted(modules))
            state.fail("f2p", GradeFailure.F2P_FAILED, result,
                       detail="did not import: "
                              + ", ".join(state.f2p_failed_node_ids))

    # Everything else: the suite did not run. That is the Phase 0c failure --
    # a broken environment is also a non-zero exit -- and reading it as a model
    # failure is what a bare `!= 0` does.
    state.environment(
        "f2p",
        f"the f2p run exited {code}, so the tests did not run: {_head(result)}",
        result,
    )
```

(`state.fail` raises `_Stop`, which is why the branches above it carry no `return` — keep that shape.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/grader.py bakeoff/tests/test_grader.py bakeoff/tests/test_oracle.py
git commit -m "fix: on a collection-error task the do-nothing arm graded better than the one that tried

Check 5 mapped every non-{0,1,124} exit to an environment error. On a task
whose f2p module does not import at the start state -- the shape preflight 4
now accepts -- an arm that changed nothing leaves it not importing, exits 4 and
grades NOT GRADED, while an arm that half-fixed the import exits 1 and grades
False. In any view that counts False the arm that did nothing looks better.

The new branch is containment where preflight uses equality: preflight must
account for every declared id, this must never accuse for anything outside the
task. A module outside the set is indistinguishable from an image that lost a
dependency and stays an environment error; so does an empty reported set, which
is a manifest typo. f2p_failed_node_ids carries the module paths pytest
actually reported -- a module IS a node id, told apart by the absent `::`.

Check 6 and oracle._classify are unchanged and pinned as unchanged: check 6 is
only reached once every f2p module imported, and both oracle runs are at the
post-fix state. GRADER_VERSION 2 -> 3.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Docs — what a candidate must satisfy changed, so the rules move with it

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md` (Layer 1 preflight bullets; Layer 2 "The test half must IMPORT cleanly")
- Modify: `docs/BUILDING-A-TASK-SET.md` (§3.4; §3.7 refusal table)
- Modify: `docs/superpowers/specs/2026-08-17-offline-grader-design.md` (check 5 row; the `f2p_failed_node_ids` field line)
- Modify: `tasks/todo.md` (append a review section)

- [ ] **Step 1: Rewrite HARVESTING.md Layer 1's two f2p/p2p preflight bullets**

Replace the existing `f2p exits **1** …` and `p2p exits 0 …` bullets with:

```markdown
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
- f2p exits 0 after the reference fix, and p2p still exits 0. If the reference
  cannot pass, no submission can — and on a collection-error task this is
  where the import must have started working. It is **not** relaxed to match
  red-before.
```

- [ ] **Step 2: Rewrite HARVESTING.md Layer 2's "must IMPORT cleanly" paragraph**

Replace the whole bullet (from "**The test half must IMPORT cleanly at the start state.**" through "…if any does not, it cannot.") with:

```markdown
- **Both import shapes are accepted, and they are different tasks.** A PR that
  adds a new function and tests for it puts that symbol in the solution half,
  so the test module raises `ImportError` during collection and pytest exits
  **4** (a selected node id whose module will not import) rather than **1**.
  Preflight refused that outright until broadening 2 and now accepts it, under
  a narrow, measured condition: every reported `ERROR` line names a declared
  f2p module, nothing else is reported, and p2p is green at the start state.
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
```

- [ ] **Step 3: Rewrite `docs/BUILDING-A-TASK-SET.md` §3.4**

Append to §3.4, after the `grep -E '^(FAILED|ERROR)'` block and before the "Copy those ids exactly" paragraph:

```markdown
**If that command prints `ERROR <module>` with no `::` and nothing else, the
test half does not import at the start state** — the PR adds a symbol its tests
call. That is an accepted shape, and the ids still come from the **post-fix**
run, never from the red one: the red run cannot name them, because a collection
error stops pytest before anything is collected. Get them from the state the
grader will grade:

```bash
git checkout --detach <merge_commit>
python -m pytest -q -p no:cacheprovider tests/ | tail -3   # must be all green
python -m pytest -q -p no:cacheprovider --collect-only -q <the new test module>
```

Every id `--collect-only` prints for the module(s) the PR adds is an f2p
candidate. Then check the constraint the gate enforces: **every module holding
a declared f2p id must be one of the modules that errored** in the red run. If
the PR also changes a test in a module that already imports, that test's id
cannot be in `f2p` for this task — a collection error hides it, so preflight
would be accepting an id nobody checked, and it refuses instead.
```

- [ ] **Step 4: Update `docs/BUILDING-A-TASK-SET.md` §3.7's refusal table**

Replace the `f2p exits 2 / 4 / 5 at the start state` row with these three:

```markdown
| f2p exits 5 at the start state, or 2/4 with `ERROR: not found:` | a declared node id does not exist — a typo, or the id changed shape (parametrization) |
| f2p exits 2/4 and the reported `ERROR` modules are not exactly the declared f2p modules | a module errored that no f2p id names (broken environment: fix `image.pip`/`image.apt`), or a declared f2p module did not error (its ids are unobservable behind another module's collection error — narrow `f2p` to the modules that actually error, or pick a different PR) |
| f2p could not be collected AND p2p is not green | the confinement parse cannot tell a missing symbol from a missing interpreter; p2p is the evidence that separates them, so fix the environment first |
```

- [ ] **Step 5: Fix the two places `BUILDING-A-TASK-SET.md` still calls this shape unrepairable**

`docs/BUILDING-A-TASK-SET.md` §3.1 tells a harvester to reject the candidate outright, and §10's symptom table repeats it. Both also name exit **2** where the gate sees **4**. In §3.1, replace the paragraph beginning "A PR that *adds* a function and tests for it…" and the heuristic sentence after it with:

```markdown
A PR that *adds* a function and tests for it puts that symbol in the solution
half, so the test module raises `ImportError` during collection and pytest
exits **4** (a selected node id whose module will not import; a directory run
gives 2). Since `PREFLIGHT_VERSION` 4 that is an **accepted** task shape, under
a narrow condition you can check by eye before cutting anything: the reported
`ERROR` lines must name **exactly** the modules holding your declared f2p ids —
so an f2p set that spans a module the PR adds *and* a module that already
imports cannot work, because a collection error stops the run before the second
module's tests are ever attempted. Measured on `trucking-doc-extraction` #3.

What you are choosing between is what the agent reads: an assertion message on
an exit-1 task, `ImportError: cannot import name …` on this one. Both are what
the human who filed the issue read, so neither is the "wrong" kind of task —
but an exit-1 task gives per-test signal from the first run and this one gives
none until the import works, so **prefer a PR that changes the behaviour of an
existing symbol when you have the choice**, and reach for this shape when you
do not.
```

In §10's symptom table, replace the `a candidate PR's f2p exits 2 at the start state with `error during collection`` row with:

```markdown
| a candidate PR's f2p exits **4** at the start state with `ERROR <module>` and no `::` | the test half imports a symbol the fix introduces. **Repairable — this is an accepted shape** if every reported `ERROR` names a declared f2p module, p2p is green there, and f2p goes green after the fix (§3.1, §3.4) |
| a collection-error candidate is refused with "the rest of the suite is not green — no tests were collected" | ignoring the erroring module left the sweep empty: this repo's test tree holds no regression baseline outside that module. Not repairable by configuration; pick a PR in a repo with a wider suite |
| a collection-error candidate is refused and the printed argv shows an `--ignore` that did not take | the ignore paths come from pytest's **rootdir**-relative `ERROR` lines and `--ignore` resolves against the **working directory**. They coincide when rootdir is `/repo`; a `pyproject.toml` in a subdirectory or a `--rootdir` in `tests.runner` breaks it |
```

- [ ] **Step 6: Update the offline-grader spec**

In the ladder table, replace check 5's Method cell:

```markdown
| 5 | **F2P** | `_Runner.select(task.tests.f2p)` exits 0. A non-{0,1} exit whose reported `ERROR` lines are all bare module paths **contained in** the declared f2p modules is `f2p_failed` (the submission left an import broken — see broadening 2); anything else is an environment error | `f2p_failed` |
```

And in the `GradeRecord` field list:

```markdown
f2p_failed_node_ids: tuple | None            # on a collection error these are
                                             # MODULE paths (no `::`) -- what
                                             # pytest reported; a module is a
                                             # node id, the collector node
```

- [ ] **Step 7: Append the review section to `tasks/todo.md`**

Add a section headed `## Broadening 2 — f2p that cannot be COLLECTED at the start state` covering, one bullet each:

- **The brief said exit 2; the gate sees exit 4.** `_Runner.select` passes node ids positionally and pytest answers a node id whose module raises on import with a usage error. Exit 2 is a directory or module-path run — how the `trucking-doc-extraction` #3 measurement was taken. An acceptance written for 2 alone would have been dead code on every task preflight runs. Measured against pytest 9.1.1 and 8.3.5; they agree on every row.
- **`--continue-on-collection-errors` was measured and rejected.** Exit 4 on the f2p selection with and without it, byte-identical. It does rescue the p2p run, but only to exit 1, and applying it to the *graded* p2p would turn a broken import anywhere in the tree from an environment error into a `p2p_regression` — an accusation manufactured out of the environment. `--ignore=<module>` on preflight's p2p-before, the one p2p argv the grader never makes, gives exit 0 instead.
- **The confinement parse alone is `returncode != 0` wearing a regex.** The f2p selection imports only the f2p modules, so a missing interpreter dependency produces exactly the confined error set the task shape produces. The p2p baseline is the second conjunct, and it is why the f2p verdict is deferred until after the p2p run rather than the runs being reordered.
- **Equality in preflight, containment in the grader.** Preflight must account for every declared id and a partial collection error hides the rest of the selection (measured), so equality is the id-level rule at module granularity. The grader must never accuse for anything outside the task, so a partially fixed submission errors on a subset and is still `f2p_failed`, while a stranger module stays an environment error.
- **The mis-bucketing the grader change fixes was directional.** A do-nothing arm graded NOT GRADED and a half-fixing arm graded `False`, so the arm that did nothing was invisible in every view counting `False`.
- **The acceptance is a THREE-way conjunction, and the first draft of this plan claimed two.** A dependency imported *only* by the f2p module is confined (the f2p selection imports nothing else) and leaves p2p green (p2p never imports it), so neither of the first two conjuncts can see it. **Green-after is the environment discriminator** — and the existing Phase 0c integration fixture is that shape exactly.
- **The Phase 0c end-to-end pin had to be rewritten, and its mutation anchor repointed.** That fixture's broken import lives in the declared f2p module, so it became *confined* and was intercepted by the new branch — the assertion no longer matched and, worse, `mutation_check`'s revert of `elif red.exit_code != EXIT_TESTS_FAILED:` became **inert**, i.e. green on a reverted guarantee. The replacement puts the missing module in `tests/conftest.py`: measured, that exits 4 with no `short test summary info` section at all, so the reported set is empty and the run is unconfined.
- **The new `MUTATIONS` entry anchors on the equality, not on the `and p2p_green` conjunct.** `if not p2p_green:` is unconditional, so reverting that conjunct changes the refusal *message* and not the verdict; dropping the equality flips a NO-GO into a GO.
- **What was NOT relaxed:** green-after (pinned by its own test), check 6, `oracle._classify`, the graded p2p argv, and `tests.runner` must contain `pytest`.
- **Operator note carried into `HANDOFF`-style prose rather than left implicit:** `GRADER_VERSION` 2 → 3 makes `scripts/grade.py`'s resume gate re-grade **every stored run**, into a fresh `v3` artifacts directory beside the existing one. That is intended (a verdict derived under a different ladder is a new line whose disagreement with the old one is the finding), and it costs a full grading pass per event log — budget for it rather than discovering it mid-run.
- `PREFLIGHT_VERSION` 3 → 4, `GRADER_VERSION` 2 → 3, `SCHEMA_VERSION` unmoved, no manifest key, `click-3360`'s `start_sha` unmoved.

- [ ] **Step 8: Verify and commit**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` (docs-only change; confirm still green) and re-read each edited section once for the convention that a comment says what breaks without it.

```bash
git add bakeoff/taskset/HARVESTING.md docs/BUILDING-A-TASK-SET.md \
        docs/superpowers/specs/2026-08-17-offline-grader-design.md tasks/todo.md
git commit -m "docs: both import shapes are tasks, and the one the gate refuses is now a different one

HARVESTING Layer 2 told harvesters to prefer a PR that CHANGES a symbol over
one that ADDS one, because the second could only fail at collection. That
advice is obsolete: both shapes are accepted, what differs is that the agent
reads an ImportError instead of an assertion -- which is what the human who
filed the issue read.

What replaces it is the constraint that IS still refused, and it is easy to
trip: the acceptance is an equality, so an f2p set spanning a new module and an
existing one leaves the existing module's ids unobservable behind the
collection error, and the gate refuses rather than accept ids nobody checked.

BUILDING-A-TASK-SET 3.4 gains the case where the red run cannot name the ids at
all -- they come from the post-fix run -- and 3.7's single 'f2p exits 2/4/5'
row becomes the three different causes it was hiding.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Final verification (run before declaring the broadening done)

- [ ] `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — no regressions against the recorded baseline, plus the new tests.
- [ ] `cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"` — the real-image preflight tests, because the change alters what runs inside a container.
- [ ] `git diff main -- bakeoff/taskset/` is **empty** — no manifest key, so no `start_sha` may move.
- [ ] `cd bakeoff && .venv/bin/python scripts/verify_logger.py` — the §6.6 gate, unaffected but cheap and offline.
- [ ] Run `bakeoff/scripts/mutation_check.py` **solo**. It must pass with **one entry repointed and one entry added** (Task 3, step 5). Confirm specifically that the repointed Phase 0c entry goes red — under the *old* fixture it would have been inert, which is a check reporting a guarantee that is not there. No anchor is added for the grader change: check 5's new branch is pinned through `run_ladder()` by `test_an_unfixed_collection_error_is_a_named_failure_not_an_ungradable_run` and its two negative neighbours.
- [ ] Operator note before running `scripts/grade.py` against an existing event log: `GRADER_VERSION` 3 re-grades every stored run into a new `v3` artifacts directory. Expected, and it costs a full pass per log.

---

## Sentences for `CLAUDE.md`, to be applied on `main` after the merge

Not written here — `CLAUDE.md` has an uncommitted edit on `main` and the merge would conflict (CONTEXT.md's global constraint). Add these to the invariants list, after the existing preflight bullet:

1. **"A task must be shown to discriminate" has two shapes, and the second exits 4, not 2.** `_Runner.select` passes node ids **positionally**, and pytest answers a selected id whose module raises on import with a **usage error, exit 4** — measured 2026-09-01 against pytest 9.1.1 and 8.3.5. Exit 2 is what a *directory* or *module-path* run gives, which is how the `trucking-doc-extraction` #3 measurement was taken and why the first draft of this acceptance would have been dead code on every run preflight actually makes. Since `PREFLIGHT_VERSION` 4 a non-{0,1} f2p exit is accepted under a **three-way** conjunction: the reported `ERROR` lines are **exactly** the declared f2p modules, p2p is green **before**, and f2p is green **after**. Never on the parse alone, and never on the first two either — **the f2p selection imports only the f2p modules and p2p imports none of them**, so a dependency needed *only* by the f2p module is confined AND leaves p2p green, and **green-after is the only conjunct that refuses it**. That is the existing Phase 0c fixture exactly, which is why its integration pin had to move to a broken `tests/conftest.py` (measured: exit 4 with no summary section, so the reported set is empty and the run is unconfined) and why `mutation_check`'s anchor on the exit-code branch had to be repointed with it — a confined fixture leaves that mutation inert, i.e. green on a reverted guarantee. The discriminator is the absent `::`: pytest writes `ERROR <module>` for a module that would not import, `FAILED/ERROR <module>::<test>` for a test, and `ERROR: not found:` (with a colon, which `_FAILED_LINE` has never matched) for an id that does not exist — so a manifest typo and a task shape share exit 4 and are still separable. Green-after is **not** relaxed to match: after the fix the symbol exists, the module imports, and the tests must exit 0.
2. **The p2p baseline on such a task cannot be observed without `--ignore`, and the flag is legal on exactly one run.** The p2p-before argv sweeps the rootdir, so the erroring module aborts collection before `--deselect` is applied (measured: exit 2). `--ignore=<module>` on **preflight's p2p-before only** gives exit 0. That run is not an argv the grader ever makes — the graded p2p is at the post-submission state and is matched by preflight's p2p-**after**, which gets no ignore — and the one thing the ignore hides, a non-f2p test inside the ignored module, is measured by that same p2p-after run. `--continue-on-collection-errors` was the obvious alternative and is worse in both directions: measured, it leaves the f2p selection at exit 4 unchanged, and on the graded p2p it would convert a broken import anywhere in the tree from `ENVIRONMENT_ERROR` into `p2p_regression` — an accusation manufactured out of an environment difference.
3. **Preflight compares for equality; the grader compares for containment.** Preflight's job is that no declared f2p id goes silently unchecked, and a collection error stops the run dead (measured: a selection spanning a broken module and a failing test reports only the collection error), so equality at module granularity is the id-level rule one level up. Check 5's job is the opposite — a `False` is an accusation — so a submission erroring on a *subset* of the declared modules is `f2p_failed` and anything outside them stays `ENVIRONMENT_ERROR`. Before `GRADER_VERSION` 3 that whole branch was environment, which made the mis-bucketing **directional**: an arm that changed nothing graded NOT GRADED while an arm that half-fixed the import graded `False`, so the do-nothing arm was invisible in every view that counts `False`.
