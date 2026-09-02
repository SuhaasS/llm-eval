# Broadening 4: a per-task suite timeout — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 600 s bound that preflight, the oracle and the offline grader wrap every in-container command in a per-task manifest key (`budget.suite_timeout_s`), so a task whose suite is legitimately slow can be gated and graded under the *same* bound instead of being refused by the gate or stamped `timed_out` by the grader.

**Architecture:** One number, one source. `TaskBudget` gains `suite_timeout_s` (default 600, the current constant). Every consumer that today wraps a command in coreutils `timeout` already has the `task` in scope, so each reads `task.budget.suite_timeout_s` directly and the `timeout_s` *parameters* on `preflight()` and `ensure_oracle()` are **deleted** rather than defaulted — a defaulted parameter beside a manifest key is two sources for one number, and a driver left on the default is exactly the gated-vs-graded divergence this broadening exists to close. Neither driver (`run_matrix.py`, `grade.py`) needs a line changed as a result, which is the point. `PREFLIGHT_VERSION`, `ORACLE_VERSION`, `GRADER_VERSION` and `GRADE_SCHEMA_VERSION` all move, because each of the three caches they key would otherwise serve a verdict computed under a bound the manifest no longer asks for.

**Tech Stack:** Python 3.12, pytest, PyYAML, Docker (integration legs only). All code under `bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3 the self-correction loop, §3.7 the manifest, §5.4 budgets) and `docs/superpowers/specs/2026-08-17-offline-grader-design.md` (the nine-check ladder). Shared broadening context: `.superpowers/broaden/CONTEXT.md` (this is broadening **#4**).

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module/function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. Claims about external behaviour are annotated with what they were verified against.
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section of this plan for later application.
- **`PREFLIGHT_VERSION` bumps by ONE from whatever value is on disk when this task starts.** Broadening 3 lands before this one and may already have moved it from `"4"` to `"5"`. Read the constant, add one, and do not hard-code `"5"`.
- **Do not depend on preflight evidence keys broadening 3 may add.** This plan adds exactly one evidence key, `suite_timeout_s`, and reads no other.
- **The gated argv and the graded argv stay byte-identical.** `tests/test_preflight.py::test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` already pins the flag *shape*; Task 5 adds the pin on the timeout *number*.
- **Backwards compatibility:** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` must load unchanged and its `start_sha` must not move. `budget.suite_timeout_s` does not participate in the setup commit, so `start_sha` cannot move; assert it anyway (Task 1, Step 8).
- **Tests pin every new invariant.** Unit tests in `bakeoff/tests/`; integration tests are marked `integration` (+ `task_image` where an image is built).
- **Commit hygiene:** stage files explicitly (`git add <paths>`), never `git add -A`. Subject in the repo's style (`feat:`/`fix:`/`docs:` + a sentence saying what breaks without it). End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` → 1129 passed, 46 deselected (plus whatever broadenings 1–3 added).

---

## Design decisions, settled

### D1. The key is `budget.suite_timeout_s`, and it bounds *every* in-container command the gate and the grader run

It sits in `budget:` beside `max_turns` and `wall_clock_timeout_s` because it is a per-task cap of the same kind, and `TaskBudget` is already the one place a reader looks for one.

It bounds the suite invocations **and** the declared `grading.*` argvs (build / typecheck / lint). Preflight runs those under the same `timeout` prefix today (`preflight.py:728`) and the grader's `_check_command` does too (`grader.py:831`) — both off the same constant. A second key for the grading commands would be a second number that can diverge between the gate and the grader, which is the defect being closed, for the benefit of a distinction nobody has asked for. So: one key, and the docstring says explicitly that "suite" here means *every command preflight and the grader run inside the container*.

Not renamed to `command_timeout_s`: the name is fixed by the broadening's shared context and by the review that will read this, and the docstring carries the breadth.

### D2. Validation — a positive int, and a load-time refusal when it exceeds `wall_clock_timeout_s`

**Positive int, strictly.** `max_turns` and `wall_clock_timeout_s` parse with a bare `int(...)`, which turns `max_turns: "forty"` into a `ValueError` escaping `load_task` with no manifest path in the message. For a *timeout* the failure is worse: `bool` is an `int` in Python, so `suite_timeout_s: true` silently becomes `1`, every suite the gate runs is killed after one second, and the task reads as one whose tests hang. So the new key gets a `_positive_int` helper raising `TaskError` with `where` in the message. The two existing keys are deliberately **left alone** — tightening them could refuse a manifest that loads today, which is out of scope here; it goes to `TASKS.md` as a named follow-up.

**`suite_timeout_s > wall_clock_timeout_s` is a load error, not a preflight problem and not author advice.** The arithmetic is in the message. Reasoning:

- The agent re-runs this suite *inside* its wall clock (`claude_runner`'s container backend wraps the whole `claude -p` in `["timeout", "-s", "TERM", str(timeout_s), *command]`, fed `timeout_s=self.config.wall_clock_timeout_s` from `ClaudeRunner.run`; there is no per-command bound inside the agent's container). Spec §3.3 measures a loop that ends in "runs tests, sees failures, self-corrects". An author who declares the suite may need up to 1800 s while giving the agent 900 s total has declared a task no arm can verify even **once**: the run is SIGTERMed mid-suite and produces a diff nobody checked. That is the same defect class as the image that shipped without `pytest` — it truncates the measured loop and scores every arm on an unverified guess.
- It is a *load* error because it is settled by arithmetic over two manifest numbers: no container, no daemon, no measurement. Pushing it to preflight would pay an image build to discover a contradiction that is visible in the YAML.
- It is not merely documented, because the failure it prevents is silent — a mid-suite SIGTERM looks exactly like a model that ran out of wall clock, which is a legitimate outcome the harness records as `BUDGET_EXHAUSTED`.
- The refusal names both escapes: raise `wall_clock_timeout_s` (per-task, identical across arms, so no §5.4 divergence), or lower `suite_timeout_s` to what the suite actually needs.
- **Strictly `>`, not `>=`.** Equality leaves the agent exactly one suite run and no editing time, which is degenerate — but "degenerate" is a judgement about how much slack an agent needs, and this rule only asserts what arithmetic settles. The defaults (600 vs 900) and `click`'s declared 900 both pass.

The check lives in `load_task`, not in `TaskBudget.__post_init__`, matching every other manifest rule in the file and keeping `TaskBudget` constructible by tests (`tests/test_judge.py:580` builds a bare `TaskBudget()`).

### D3. Every consumer, and why the parameters are deleted rather than defaulted

Every place a command is wrapped in `timeout` today:

| Site | File | Today | After |
|---|---|---|---|
| f2p before | `preflight.py:543` via `_Runner` | `timeout_s` param, default 600 | `task.budget.suite_timeout_s` |
| p2p before | `preflight.py:583` via `_Runner` | same | same |
| f2p after | `preflight.py:693` via `_Runner` | same | same |
| p2p after | `preflight.py:705` via `_Runner` | same | same |
| scoped p2p after | `preflight.py:772` via `_Runner` | same | same |
| declared `grading.*` argvs | `preflight.py:728` inline | same | same |
| oracle's two reference p2p runs | `oracle.py:271` via `_Runner` | `timeout_s` param, default 600 | `task.budget.suite_timeout_s` |
| grader check 3/4/7 (build, typecheck, lint) | `grader.py:831` inline | `GRADE_TIMEOUT_S` | `task.budget.suite_timeout_s` |
| grader check 5 (f2p) | `grader.py:859` via `_Runner` | `GRADE_TIMEOUT_S` | `task.budget.suite_timeout_s` |
| grader check 6 (p2p) | `grader.py:970` via `_Runner` | `GRADE_TIMEOUT_S` | `task.budget.suite_timeout_s` |
| gitleaks secret scan | `grader.py:1382` host-side `subprocess.run(timeout=…)` | `GRADE_TIMEOUT_S` | **stays a constant**, renamed `SCAN_TIMEOUT_S` |

Call sites that need **no change**: `scripts/run_matrix.py:152` (`preflight(task, image=…, repo_path=…, start_sha=…, expected_claude_version=…)`), `scripts/grade.py:300` (`preflight(task, image=…, repo_path=…, start_sha=…)`) and `scripts/grade.py:316` (`ensure_oracle(task, image, Path(cache))`) — none passes a bound today. `scripts/smoke_test.py` and `scripts/dry_run.py` do not call preflight or the oracle at all (their `wall_clock_timeout_s` uses are the agent's bound; `scripts/smoke_bedrock.py:186` defines an unrelated local function also named `preflight`).

**The parameters are deleted.** `preflight(..., timeout_s: int = 600)` and `ensure_oracle(..., timeout_s: int = 600)` both take `task` already, so a defaulted parameter would be a second source for one number whose divergence is invisible: a suite that fits preflight's bound and is killed under the grader's stamps `timed_out` — a `GradeFailure`, i.e. `resolved: False`, an accusation against the model — over a number the model never saw. Deleting the parameter makes "a consumer left on the constant" unrepresentable rather than merely tested for.

**The gitleaks scan keeps a constant, and is renamed.** It is a fixed-size scan of one diff on the *host*, not the task's suite, and `_ContainerEnv.scan_secrets` has no `task` in scope. Leaving it named `GRADE_TIMEOUT_S` is the trap: a constant called "the grade timeout" invites the next consumer to reach for it instead of the manifest. `SCAN_TIMEOUT_S` says what it bounds.

### D4. Recording

- **Preflight evidence** gains `suite_timeout_s`, read back off `_Runner.last_argv` via a new `_Runner.last_timeout_s` property — **not** off the manifest. That is the reason `last_argv` exists at all (`preflight.py:319`): a second read of the configuration is a second thing that can be right about what was *asked for* while the argv carried something else. `last_timeout_s` returns `None` when no invocation has been made or the argv carries no `timeout` prefix. On the early return (a non-pytest `tests.runner`) the key is **absent**, which is honest: no suite ran under any bound.
- **`GradeRecord` today carries no timeout value at all** — only `CheckResult.timed_out: bool`. With the bound now per task and re-gradable under a changed manifest, `timed_out: True` alone is unreadable: a reader cannot tell a suite that blew 600 s from one that blew 1800 s. And the bound is only half of what a `timed_out` grade has to be read against: **a grader timeout is stamped on the model, and nothing on the record says whether the grader's host was busier than the gate's.** `GradeRecord` carries no host, load or contention block (`RunRecord.host` has one; the grade has none), and `CheckResult.duration_s` at a timeout is just the bound again, so the headroom between what the suite actually needs and what it was given is unrecorded on both sides. That is named in the field's docstring and widened into `TASKS.md` follow-up #2 rather than fixed here — the missing measurement is preflight's observed suite duration, which is the same gap. So `GradeRecord.suite_timeout_s: int | None` is added (`None` = no bounded command ran, which is what a record refused at check 1 or 2 looks like), carried through `_State` → `LadderResult` → `build_grade_record`. `GRADE_SCHEMA_VERSION` `"1.0.0"` → `"1.1.0"` — the same additive rule `SCHEMA_VERSION` follows, because a reader that cannot tell versions apart reads an absent field as a positive negative claim.
- **`RunRecord`/`SCHEMA_VERSION` do not move.** This bound never touches the agent's container; nothing about a run record changes.

### D5. Which versions move, and why each one has to

All three caches key on `(manifest_digest, image, …VERSION)`, and `manifest_digest` is `sha256(manifest bytes + reference bytes)` — so it *does* move when an author declares or edits the key. That is not sufficient, and the same argument covers all three: a manifest that already carries `budget.suite_timeout_s: 1800` **loads fine under today's code** (unknown `budget` sub-keys are ignored), is gated/derived/graded at 600, and its verdict is cached under a digest that will not move when this code lands. Every warm cache would then serve a verdict computed under a bound the manifest does not ask for.

- **`PREFLIGHT_VERSION`: bump by one from the value on disk.** The gate's verdict is now a function of a manifest value it previously ignored.
- **`ORACLE_VERSION`: `"1"` → `"2"`.** Identical argument; a quarantine derived under a bound that killed a slow reference run is not the quarantine this manifest asks for.
- **`GRADER_VERSION`: `"3"` → `"4"`.** The ladder's behaviour on the same input can change: a longer bound turns a `timed_out` fail into a pass. **On today's corpus it changes no verdict** — no stored manifest declares the key, so every ladder still runs at 600 — and it still cannot wait, because `scripts/grade.py`'s resume key is `(run_id, grader_version)` **alone**; it never consults `manifest_digest`. So the first manifest edit that raises the bound would find every affected run already marked graded and keep serving the 600 s verdict, with nothing on either line saying they were measured against different bounds. The bump has to land with the code that makes the divergence possible, not with the manifest that first exercises it. Operator cost: a full re-grade of every stored run into a fresh `v4` artifacts directory — intended (a verdict under a different ladder is a new line whose disagreement is the finding), identical in shape to the `2 → 3` bump, and **schedulable rather than urgent**, since no verdict changes until a manifest raises the key.
- **`GRADE_SCHEMA_VERSION`: `"1.0.0"` → `"1.1.0"`** (D4).

### D6. Preflight's multiplied wall budget

Preflight makes **five** suite invocations on the ordinary (deselect) branch — f2p-before, p2p-before, f2p-after, p2p-after, scoped-p2p-after — and **four** when `tests.p2p` is declared explicitly (the scoped run is guarded by `if not tests.p2p`). `HARVESTING.md` currently says "four times"; that is wrong for the branch every task actually takes and is corrected in Task 6. On top of that it runs **one command per declared `grading.*` argv** (up to three). So the worst case is **8 × `suite_timeout_s` per task**, plus two more suite runs at grade time for the oracle and up to five per graded record.

`run_matrix.py` applies **no outer per-task preflight timeout** — the only bound is the `timeout` prefix inside the container — so there is nothing to scale. What does interact, and must be documented rather than gated:

- Preflight runs for **every** task before the proxy starts. Raising `suite_timeout_s` raises the worst-case wall time between `aws sso login` and the first cell, and the SSO session is **one hour, measured twice** (2026-08-12 and 2026-08-13). The credential margin at `run_matrix.py:434/479` is computed per *cell*, after preflight has already spent part of that hour, so it cannot see this.
- No new gate is added: 8 × the bound is a worst case, not a duration, and a gate on a worst case would refuse task sets that run fine. The measurement that would let this be a real check — preflight's actual elapsed time per task — does not exist yet and goes to `TASKS.md` as a named follow-up.

---

## File Structure

**Modified:**
- `bakeoff/src/bakeoff/tasks.py` — `TaskBudget.suite_timeout_s`, `_positive_int`, the `load_task` parse and the `> wall_clock_timeout_s` refusal.
- `bakeoff/src/bakeoff/preflight.py` — `_Runner.last_timeout_s`; `preflight()` loses `timeout_s` and reads the manifest; `evidence["suite_timeout_s"]`; `PREFLIGHT_VERSION`.
- `bakeoff/src/bakeoff/oracle.py` — `_derive`/`ensure_oracle` lose `timeout_s` and read the manifest; `ORACLE_VERSION`.
- `bakeoff/src/bakeoff/grader.py` — three call sites read the manifest; `GRADE_TIMEOUT_S` → `SCAN_TIMEOUT_S` (gitleaks only); `_State`/`LadderResult`/`build_grade_record` carry `suite_timeout_s`; `GRADER_VERSION`; `__all__`.
- `bakeoff/src/bakeoff/grade_schema.py` — `GradeRecord.suite_timeout_s`, `GRADE_SCHEMA_VERSION`.
- `bakeoff/tests/test_tasks.py`, `test_preflight.py`, `test_oracle.py`, `test_grader.py` — the pins.
- `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` — commented example under `budget:`.
- `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`, `tasks/todo.md`, `TASKS.md`.

**Created:** nothing.

---

### Task 1: `budget.suite_timeout_s` in the manifest loader

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (`TaskBudget`, `load_task`'s `budget_raw` parse and its `TaskManifest` construction)
- Modify: `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` (the `budget:` block)
- Test: `bakeoff/tests/test_tasks.py`

**Interfaces:**
- Produces: `TaskBudget.suite_timeout_s: int = 600`; `tasks._positive_int(value, where, default) -> int` raising `TaskError`. Every later task reads `task.budget.suite_timeout_s`.

- [ ] **Step 1: Write the failing tests**

Add to `bakeoff/tests/test_tasks.py`, after the existing `_write_task` helper. **Do not invent a manifest writer.** The file has `_manifest(**overrides)` and `_write_task(root, upstream, name="t-001", **overrides)`, and `_write_task` needs the module's `upstream` fixture (a real git repo plus a real reference diff). `_manifest` has no `budget` parameter and interpolates raw strings, so a budget block goes in through `extra_yaml` as **literal YAML**. Add this local helper beside the tests:

```python
def _budget_task(tmp_path, upstream, body: str) -> Path:
    """A manifest whose `budget:` block is the literal YAML in `body`.

    `extra_yaml` is appended verbatim, which is what lets these tests state
    the malformed shapes a keyword-per-key helper could not express -- the
    same reason the `grading:` tests use it.
    """
    return _write_task(tmp_path / "set", upstream,
                       extra_yaml="budget:\n" + body)
```

Every bad-value case is **parametrized over YAML source strings, never over Python values.** What is under test is what the loader receives from the parser, and the two are not the same: interpolating a Python `"600"` renders `suite_timeout_s: 600`, which YAML parses as the **int** 600 — the refusal never fires, the test reports DID NOT RAISE, and the natural repair is to loosen `_positive_int`. That is a false green that deletes the check it was written to add. `"600"` must reach the file WITH its quotes, `None` as `null`, `True` as `true`, `600.0` as `600.0`.

```python
def test_the_suite_timeout_defaults_to_the_constant_it_replaces(
    tmp_path, upstream
):
    """600 was `preflight(timeout_s=600)`, `ensure_oracle(timeout_s=600)` and
    `grader.GRADE_TIMEOUT_S`, three copies of one number. A manifest that does
    not mention the key must gate and grade exactly as it did before, so the
    default is that number and not a rounder one."""
    task = load_task(_write_task(tmp_path / "set", upstream))
    assert task.budget.suite_timeout_s == 600


def test_a_declared_suite_timeout_is_read(tmp_path, upstream):
    task = load_task(_budget_task(tmp_path, upstream, (
        "  max_turns: 40\n"
        "  wall_clock_timeout_s: 3600\n"
        "  suite_timeout_s: 1800\n"
    )))
    assert task.budget.suite_timeout_s == 1800


@pytest.mark.parametrize("yaml_value", ['"600"', "600.0", "null", "0", "-1"])
def test_a_suite_timeout_that_is_not_a_positive_int_is_a_load_error(
    tmp_path, upstream, yaml_value
):
    """`int("600")` and `int(600.0)` both SUCCEED, so the bare `int(...)` the
    other two budget keys use accepts a quoted or floated value silently and
    the manifest stops being a faithful record. An explicit `null` is refused
    rather than defaulted: the author WROTE the key, so reading it as "never
    written" is the wrong repair -- which is why `_positive_int` takes an
    `_ABSENT` sentinel and not a `None` default.

    Parametrized over YAML SOURCE. `'"600"'` reaches the file with its quotes;
    written as a Python `"600"` it would render unquoted, parse as the int
    600, and this case would silently stop testing anything."""
    with pytest.raises(TaskError, match="budget.suite_timeout_s"):
        load_task(_budget_task(
            tmp_path, upstream, f"  suite_timeout_s: {yaml_value}\n"))


def test_a_boolean_suite_timeout_is_refused_rather_than_read_as_one_second(
    tmp_path, upstream
):
    """Its own test, because the failure mode differs from every value above:
    those are visibly wrong, and this one is ACCEPTED. `bool` IS an `int` in
    Python, so `int(True)` is 1 and `suite_timeout_s: true` kills every gated
    and graded command after one second -- a NO-GO at the gate and, past it,
    `timed_out` stamped on every arm -- for a YAML typo. `isinstance(value,
    bool)` must be tested BEFORE `isinstance(value, int)`; folded into it the
    bool branch is dead code a mutation cannot catch."""
    with pytest.raises(TaskError, match="budget.suite_timeout_s"):
        load_task(_budget_task(tmp_path, upstream, "  suite_timeout_s: true\n"))


def test_a_suite_timeout_over_the_agents_wall_clock_is_refused_with_the_sum(
    tmp_path, upstream
):
    """The agent re-runs this suite INSIDE `wall_clock_timeout_s` and there is
    no per-command bound in its container, so a suite the author says may need
    longer than the agent's whole run is a task no arm can verify even once --
    the run is SIGTERMed mid-suite and the diff is unchecked. Spec section 3.3
    measures a loop that ends in "runs tests, sees failures, self-corrects";
    this is that loop truncated, and it is settled by arithmetic over two
    manifest numbers, so it is a LOAD error rather than a preflight one.

    The message must carry both numbers: an author told only "too large" has
    to guess which of the two to move."""
    with pytest.raises(TaskError) as exc:
        load_task(_budget_task(tmp_path, upstream, (
            "  wall_clock_timeout_s: 900\n"
            "  suite_timeout_s: 1800\n"
        )))
    message = str(exc.value)
    assert "1800" in message and "900" in message
    assert "wall_clock_timeout_s" in message


def test_a_suite_timeout_equal_to_the_wall_clock_loads(tmp_path, upstream):
    """Strictly `>`, not `>=`. Equality leaves the agent exactly one suite run
    and no editing time, which is degenerate -- but "degenerate" is a judgement
    about how much slack an agent needs, and this rule asserts only what
    arithmetic settles. Pinned so a later tightening is a deliberate change
    rather than an unnoticed one."""
    task = load_task(_budget_task(tmp_path, upstream, (
        "  wall_clock_timeout_s: 900\n"
        "  suite_timeout_s: 900\n"
    )))
    assert task.budget.suite_timeout_s == 900


def test_the_default_budget_pair_is_self_consistent():
    """The defaults must not be a pair the loader would refuse: 600 <= 900."""
    assert TaskBudget().suite_timeout_s <= TaskBudget().wall_clock_timeout_s
```

Ensure `TaskBudget` is imported in the test module (`from bakeoff.tasks import ..., TaskBudget, TaskError, load_task`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k suite_timeout`
Expected: FAIL — `AttributeError: 'TaskBudget' object has no attribute 'suite_timeout_s'` on the first two, and `DID NOT RAISE` on the refusal tests.

- [ ] **Step 3: Add the field to `TaskBudget`**

In `bakeoff/src/bakeoff/tasks.py`, extend `TaskBudget`:

```python
@dataclass(frozen=True)
class TaskBudget:
    # Section 5.4: generous caps, measure actuals. These are placeholders
    # until the section 3.5 calibration pilot sets them from the
    # slowest-converging model's p95 -- tuning them to the incumbent is
    # exactly what that section forbids.
    max_turns: int = 40
    wall_clock_timeout_s: int = 900
    #: The coreutils `timeout` bound on every command preflight, the oracle
    #: and the offline grader run INSIDE the container -- each suite
    #: invocation and each declared `grading.*` argv alike. "Suite" is the
    #: short name; the breadth is the point, because the gate and the grader
    #: run the SAME commands and a second key for the grading argvs would be
    #: a second number that can diverge between them.
    #:
    #: It does NOT bound the agent: the agent's own suite runs happen inside
    #: `wall_clock_timeout_s` with no per-command bound (`claude_runner`
    #: wraps the whole `claude -p`). That independence is why the two keys
    #: are separate, and the `>` refusal in `load_task` is the one place
    #: they are compared.
    #:
    #: 600 is the constant this replaced, in three copies: `preflight(
    #: timeout_s=600)`, `ensure_oracle(timeout_s=600)` and
    #: `grader.GRADE_TIMEOUT_S`. A manifest that does not declare the key
    #: therefore gates and grades byte-identically to before.
    suite_timeout_s: int = 600
```

- [ ] **Step 4: Add `_positive_int` beside the other manifest validators**

In `bakeoff/src/bakeoff/tasks.py`, add a module-level sentinel beside the other private constants:

```python
#: "the key is not in the manifest", which is not the same as `null`. A `None`
#: default would collapse the two, and `key: null` is a key the author WROTE
#: -- reading it as "never written" is the wrong repair.
_ABSENT = object()
```

and the validator next to `_require` and `_strs`:

```python
def _positive_int(value: Any, where: str, default: int) -> int:
    """A budget number, or a `TaskError` that names the key.

    Three things a bare `int(...)` gets wrong, and the third is the reason
    this exists at all:

    * `int("forty")` raises a bare `ValueError` out of `load_task`, with no
      manifest path in the traceback -- which is how `max_turns` and
      `wall_clock_timeout_s` behave today.
    * `int("600")` and `int(600.0)` SUCCEED, so a quoted or floated value is
      accepted silently and the manifest stops being a faithful record.
    * `bool` IS an `int` in Python: `int(True)` is 1. `suite_timeout_s: true`
      would kill every gated and graded command after one second, which reads
      as a task whose tests hang -- a NO-GO at the gate and `timed_out`
      stamped on every arm past it -- for a YAML typo. So the bool test comes
      FIRST; folded into the int test it is dead code a mutation cannot catch.

    `_ABSENT` rather than `None` for "not declared", so an explicit `key: null`
    falls through to the refusal. Applied to `suite_timeout_s` only:
    `max_turns` and `wall_clock_timeout_s` keep their bare `int(...)` because
    tightening them could refuse a manifest that loads today, which belongs in
    its own change (`TASKS.md`).
    """
    if value is _ABSENT:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskError(f"{where}: must be a positive integer, got {value!r}")
    return value
```

- [ ] **Step 5: Parse the key and refuse the contradiction**

In `bakeoff/src/bakeoff/tasks.py`, replace the inline `budget=TaskBudget(...)` inside the `return TaskManifest(...)` call with a `budget` local built just above that return, and add the relation check:

```python
    budget = TaskBudget(
        max_turns=int(budget_raw.get("max_turns", TaskBudget.max_turns)),
        wall_clock_timeout_s=int(
            budget_raw.get("wall_clock_timeout_s", TaskBudget.wall_clock_timeout_s)
        ),
        suite_timeout_s=_positive_int(
            budget_raw.get("suite_timeout_s", _ABSENT),
            f"{where}:budget.suite_timeout_s",
            TaskBudget.suite_timeout_s,
        ),
    )
    # The agent re-runs this suite INSIDE its wall clock and nothing bounds it
    # per command there, so a suite the author says may need longer than the
    # agent's whole run is a task no arm can verify even once: section 3.3
    # measures a loop that ends in "runs tests, sees failures, self-corrects",
    # and this is that loop truncated by a SIGTERM mid-suite, with a diff
    # nobody checked. Settled by arithmetic over two manifest numbers, so it
    # is refused HERE rather than in preflight -- an image build is a high
    # price for a contradiction visible in the YAML. Strictly `>`: equality is
    # degenerate but is a judgement about slack, not something arithmetic
    # settles.
    if budget.suite_timeout_s > budget.wall_clock_timeout_s:
        raise TaskError(
            f"{where}: budget.suite_timeout_s ({budget.suite_timeout_s}) "
            "exceeds budget.wall_clock_timeout_s "
            f"({budget.wall_clock_timeout_s}) by "
            f"{budget.suite_timeout_s - budget.wall_clock_timeout_s}s. The "
            "agent re-runs this suite inside its wall clock, so it could not "
            "verify its own work even once -- the run would be terminated "
            "mid-suite with an unchecked diff, and that is indistinguishable "
            "from a model that simply ran out of time. Raise "
            "wall_clock_timeout_s, or lower suite_timeout_s to what the suite "
            "actually needs."
        )
```

and pass `budget=budget` in the `TaskManifest(...)` construction.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q`
Expected: PASS, no regressions in the rest of the file.

- [ ] **Step 7: Document the key in the worked-example manifest**

In `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, extend the `budget:` block. Match the commented-example style the top-level `strip_paths` block uses:

```yaml
budget:
  # Section 5.4 placeholders. Real caps come from the section 3.5 calibration
  # pilot's slowest-converging model, not from the incumbent.
  max_turns: 40
  wall_clock_timeout_s: 900
  # suite_timeout_s: NOT USED by this task -- click's suite runs in 1.4 s, so
  # the 600 s default is 400x the headroom it needs. Shown because this
  # manifest is the documentation for the key.
  #
  # It is the coreutils `timeout` bound on every command preflight, the
  # oracle and the offline grader run inside the container: each suite
  # invocation AND each declared grading.* argv. It does NOT bound the agent,
  # whose own suite runs happen inside wall_clock_timeout_s.
  #
  #   suite_timeout_s: 1800
  #
  # A positive integer, and it may not exceed wall_clock_timeout_s -- the
  # agent re-runs this suite inside that budget, so a longer one describes a
  # task no arm could verify even once. Raising it multiplies the gate's wall
  # cost: preflight runs the suite five times (four when tests.p2p is
  # declared) plus once per declared grading.* argv, all before the proxy
  # starts and inside the same one-hour SSO session the matrix needs.
```

- [ ] **Step 8: Verify the real manifest still loads and `start_sha` has not moved**

Run:
```bash
cd bakeoff && .venv/bin/python -c "
from bakeoff.tasks import load_task
t = load_task('taskset/click-3360-write-usage-empty-args')
print(t.budget)
print('declared_start_sha', t.declared_start_sha)
print('manifest_digest', t.manifest_digest)
"
```
Expected: `suite_timeout_s=600`, and the run completes without a `TaskError`. `manifest_digest` **will** change (the YAML comment bytes are hashed) — that is correct and intended: it invalidates the preflight and oracle caches for this task, which is what a manifest edit should do. `declared_start_sha` is unchanged, and it must be: `suite_timeout_s` takes no part in the setup commit.

Then run the full suite: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py \
        bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml
git commit -m "$(cat <<'EOF'
feat: a suite too slow for one fixed 600 s bound is a task, when the manifest says how slow

`budget.suite_timeout_s` (default 600, the constant it replaces) is the
coreutils `timeout` bound on every command preflight, the oracle and the
grader run inside the container. Without it a task whose suite legitimately
needs longer is refused by the gate, and there is no way to raise the gate's
bound without also raising the grader's by hand -- a suite that fits one and
not the other stamps `timed_out`, which is `resolved: False`, on every arm.

Validated as a positive int rather than with the bare `int(...)` the other two
budget keys use: `bool` is an `int`, so `suite_timeout_s: true` becomes 1 and
every gated and graded command dies after one second, reading as a task whose
tests hang. And refused at load when it exceeds `wall_clock_timeout_s`: the
agent re-runs this suite inside that budget with no per-command bound, so a
longer one describes a task no arm can verify even once -- the run is
terminated mid-suite with an unchecked diff, which is indistinguishable from a
model that ran out of time. The message carries both numbers and their
difference, because an author told only "too large" has to guess which to move.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: preflight reads the bound from the manifest and records what it used

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (`PREFLIGHT_VERSION`, `_Runner`, the `preflight()` signature, the `_Runner` construction inside `preflight`, the evidence write after the f2p-before run, and the `_declared_grading` exec loop)
- Test: `bakeoff/tests/test_preflight.py`

**Interfaces:**
- Consumes: `task.budget.suite_timeout_s` (Task 1).
- Produces: `preflight._Runner.last_timeout_s -> int | None` (the grader imports `_Runner` and uses this in Task 4); `preflight(task, image, repo_path, start_sha, expected_claude_version="")` — **no `timeout_s` parameter**; `PreflightResult.evidence["suite_timeout_s"]`.

- [ ] **Step 1: Write the failing tests**

In `bakeoff/tests/test_preflight.py`, first give the fake task a budget. Add beside `_FakeTests`/`_FakeTask`:

```python
@dataclass(frozen=True)
class _FakeBudget:
    suite_timeout_s: int = 600
    wall_clock_timeout_s: int = 900
    max_turns: int = 40
```

and add `budget: _FakeBudget = field(default_factory=_FakeBudget)` to `_FakeTask`.

Then add the tests:

```python
def test_the_gate_bounds_every_command_by_the_manifests_number(
    monkeypatch, tmp_path
):
    """Not `timeout_s=`. A defaulted parameter beside a manifest key is two
    sources for one number, and a driver left on the default is the silent
    divergence this whole broadening exists to close -- a suite that fits the
    gate's bound and is killed under the grader's stamps `timed_out`, which is
    `resolved: False`, an accusation against the model, over a number it never
    saw.

    EVERY command, not just the suite: the declared grading.* argvs go through
    the same prefix, because the grader runs those too and a second key for
    them would be a second thing that can diverge."""
    task = _FakeTask(
        budget=_FakeBudget(suite_timeout_s=1234, wall_clock_timeout_s=3600),
        grading=TaskGrading(lint=("ruff", "check", ".")),
    )
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    bounded = [cmd for cmd in container.commands if cmd[0] == "timeout"]
    # five suite runs on the deselect branch + one declared grading argv
    assert len(bounded) == 6, bounded
    assert {cmd[1] for cmd in bounded} == {"1234"}


def test_the_evidence_records_the_bound_off_the_argv_not_off_the_manifest(
    monkeypatch, tmp_path
):
    """`last_argv` exists so a problem can name what was actually run rather
    than a reconstruction of it, and the same reasoning covers this: reading
    the manifest a second time is a second thing that can be right about what
    was ASKED for while the argv carried something else. Configuration is
    never reported as observation."""
    task = _FakeTask(budget=_FakeBudget(suite_timeout_s=1234,
                                        wall_clock_timeout_s=3600))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["suite_timeout_s"] == 1234


def test_no_bound_is_recorded_when_no_command_ever_ran(monkeypatch, tmp_path):
    """The non-pytest `tests.runner` refusal returns before the container is
    even entered. An absent key is honest there -- no command ran under any
    bound -- and a manifest value written anyway would be a claim about a run
    that did not happen."""
    task = _FakeTask(tests=_FakeTests(runner=("go", "test", "./...")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert "suite_timeout_s" not in result.evidence


def test_last_timeout_s_is_none_before_any_invocation():
    """`None` is "nobody bounded this", which is a different claim from any
    number -- the same distinction `wire_unattributed` and `cache_state.warm`
    make. A `0` or a fallback to the constructor argument would make "the
    argv carried no timeout prefix" unrepresentable."""
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest", "-q"), 1234)
    assert runner.last_timeout_s is None
    runner.run([])
    assert runner.last_timeout_s == 1234


def test_preflight_takes_no_timeout_parameter():
    """The parameter is DELETED, not defaulted. With it gone, "a consumer left
    on the constant" is unrepresentable rather than merely tested for -- which
    is why neither driver needed a line changed."""
    import inspect

    from bakeoff.preflight import preflight

    assert "timeout_s" not in inspect.signature(preflight).parameters
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "timeout or bound"`
Expected: FAIL — `AttributeError: '_Runner' object has no attribute 'last_timeout_s'`, `KeyError: 'suite_timeout_s'`, and the signature assertion failing because `timeout_s` is still a parameter.

- [ ] **Step 3: Add `_Runner.last_timeout_s`**

In `bakeoff/src/bakeoff/preflight.py`, after `_Runner.run`:

```python
    @property
    def last_timeout_s(self) -> int | None:
        """The bound the last invocation actually CARRIED, off its own argv.

        Preflight's evidence and the grader's `GradeRecord.suite_timeout_s`
        both come through here rather than off the manifest, for the reason
        `last_argv` exists at all: a second read of the configuration is a
        second thing that can be right about what was ASKED for while the argv
        carried something else, sitting inside the record that exists to say
        what happened.

        `None` when no invocation has been made, or when the argv carries no
        `timeout` prefix -- "nobody bounded this", which is a different claim
        from any number, and one a `0` or a fallback to `self.timeout_s` would
        make unrepresentable.
        """
        if len(self.last_argv) >= 2 and self.last_argv[0] == "timeout":
            return int(self.last_argv[1])
        return None
```

- [ ] **Step 4: Delete the parameter and read the manifest**

In `preflight()`, remove `timeout_s: int = 600` from the signature. Inside the body, immediately after `tests = task.tests`:

```python
    # NOT a parameter with a default. Both drivers call this with `task` and
    # neither passes a bound, so reading it here makes "a consumer left on the
    # constant" unrepresentable rather than merely tested for -- and that
    # divergence is the silent one: a suite that fits the gate's bound and is
    # killed under the grader's stamps `timed_out`, which is `resolved:
    # False`, on every arm, permanently, over a number the model never saw.
    timeout_s = task.budget.suite_timeout_s
```

Everything downstream (the `_Runner(container, tests.runner, timeout_s)` construction and the `_declared_grading` exec) keeps using the local unchanged.

- [ ] **Step 5: Make the timeout's own explanation name the key**

`_EXIT_MEANING[124]` reads `"the command hit the preflight timeout"`, and the problem prose built on it names no number — so a NO-GO on a slow suite told the author nothing about which bound was hit or where to change it, and now the bound is a manifest value it can point at. In `bakeoff/src/bakeoff/preflight.py`:

```python
    124: "the command hit the suite timeout (budget.suite_timeout_s)",
```

Preflight-local: the grader does not import `_explain`, and its own timeout details are the `f"hit the {...}s grading timeout"` strings, which already carry the number.

- [ ] **Step 6: Record the bound in the evidence**

In `preflight()`, immediately after `red = runner.select(tests.f2p)` and before `evidence["f2p_before_exit"] = red.exit_code`:

```python
        # Off the ARGV, not off the manifest -- see `_Runner.last_timeout_s`.
        # Written after the first invocation rather than at construction, so
        # the key is absent on the path where no command ever ran and a reader
        # of a cached verdict can tell that from a run that was bounded.
        evidence["suite_timeout_s"] = runner.last_timeout_s
```

- [ ] **Step 7: Bump `PREFLIGHT_VERSION` by one**

Read the current value of `PREFLIGHT_VERSION` in `bakeoff/src/bakeoff/preflight.py` (it is `"4"` at `e3ceb13`, and broadening 3 may have moved it to `"5"`). Set it to that value **plus one**, and append to the comment block above it:

```python
#: <N> makes the gate's bound a manifest value (`budget.suite_timeout_s`)
#: instead of a function default. `manifest_digest` alone does not cover this:
#: a manifest that ALREADY carries the key loads fine under the older loader,
#: which ignores unknown `budget` sub-keys, so its digest does not move when
#: this code lands and every warm cache would serve a verdict gated at 600
#: against a manifest that asks for something else.
```

- [ ] **Step 8: Run the tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q`
Expected: PASS. Then `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — `tests/test_run_matrix.py`'s cache-key tests compare `preflight_cache_key` outputs against each other, not against a literal, so the bump does not break them; if any test hard-codes the old version string, update it and say so in the commit body.

- [ ] **Step 9: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py
git commit -m "$(cat <<'EOF'
fix: the gate's bound came from a function default the manifest could not reach

`preflight(timeout_s=600)` is deleted, not defaulted. A defaulted parameter
beside `budget.suite_timeout_s` is two sources for one number, and a caller
left on the default is invisible: a suite that fits the gate's bound and is
killed under the grader's stamps `timed_out` -- `resolved: False`, an
accusation against the model -- over a number the model never saw. Both
drivers already pass `task` and neither passed a bound, so with the parameter
gone that divergence is unrepresentable instead of merely tested for.

`_Runner.last_timeout_s` reads the bound back off the argv, and the evidence
records THAT, not the manifest: configuration is never reported as
observation, and `last_argv` exists for exactly this. The key is absent when
no command ever ran, so a reader of a cached verdict can tell "unbounded"
from any number.

`_EXIT_MEANING[124]` said "the preflight timeout" and the problem prose named
no number, so a NO-GO on a slow suite told the author neither which bound was
hit nor where to change it. It names the manifest key now. Nothing asserts
that string today, so no test moves with it.

PREFLIGHT_VERSION bumps because `manifest_digest` does not cover this: a
manifest already carrying the key loads under the older loader, which ignores
unknown budget sub-keys, so its digest does not move and every warm cache
would serve a verdict gated at 600.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: the oracle derives under the same bound

**Files:**
- Modify: `bakeoff/src/bakeoff/oracle.py` (`ORACLE_VERSION`, `_derive`, the `_Runner` construction inside `_derive`, `ensure_oracle` and its `_derive` call)
- Test: `bakeoff/tests/test_oracle.py`

**Interfaces:**
- Consumes: `task.budget.suite_timeout_s` (Task 1).
- Produces: `ensure_oracle(task, image, cache_root) -> Oracle` and `_derive(task, image, cache_root) -> tuple[str, ...]` — **no `timeout_s` parameter** on either.

- [ ] **Step 1: Write the failing tests**

In `bakeoff/tests/test_oracle.py`, add `budget=SimpleNamespace(suite_timeout_s=600, wall_clock_timeout_s=900)` to `_derive_task` and to `_task`, then add:

```python
def test_the_derivation_bounds_its_two_reference_runs_by_the_manifest(
    tmp_path, monkeypatch
):
    """The quarantine is derived from two full suite runs at the reference
    state. Under a bound the manifest does not ask for, a slow-but-healthy
    suite raises OracleError ("the p2p run exited 124") and the task becomes
    ungradable -- for a number the task author already declared."""
    from bakeoff.oracle import _derive

    runner = _stub_derive_environment(
        monkeypatch, tmp_path, existing={"tests/"}, results=[(0, ""), (0, "")]
    )
    task = _derive_task(paths=("tests/",))
    task.budget.suite_timeout_s = 4321  # SimpleNamespace, assigns directly

    _derive(task, "sha256:" + "a" * 64, tmp_path)

    assert runner.timeout_s == 4321


def test_neither_oracle_entry_point_takes_a_timeout_parameter():
    """Same reasoning as `preflight`: the parameter is deleted rather than
    defaulted, so `grade.py`'s `ensure_oracle(task, image, Path(cache))` call
    cannot be left on a bound the manifest does not ask for."""
    import inspect

    from bakeoff.oracle import _derive, ensure_oracle

    assert "timeout_s" not in inspect.signature(ensure_oracle).parameters
    assert "timeout_s" not in inspect.signature(_derive).parameters
```

`_stub_derive_environment` currently patches `bakeoff.oracle._Runner` with `lambda container, argv, timeout_s: runner`; change it to record the bound so the first test can read it:

```python
    def _make_runner(container, argv, timeout_s):
        runner.timeout_s = timeout_s
        return runner

    monkeypatch.setattr("bakeoff.oracle._Runner", _make_runner)
```

Also update the `_derive` call-recording stub in this file, whose `__call__(self, task, image, cache_root, timeout_s)` will no longer match the call: drop the `timeout_s` parameter.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_oracle.py -q -k "timeout or manifest"`
Expected: FAIL — `_derive() missing 1 required positional argument: 'timeout_s'` and the signature assertions failing.

- [ ] **Step 3: Delete the parameters and read the manifest**

In `bakeoff/src/bakeoff/oracle.py`:

```python
def _derive(task, image: str, cache_root: Path) -> tuple[str, ...]:
```

and at the `_Runner` construction inside `_derive`:

```python
            # Off the manifest, and the parameter is gone rather than
            # defaulted: `grade.py` calls `ensure_oracle(task, image,
            # Path(cache))` with no bound, so a default here would derive the
            # quarantine under a number the task does not ask for while
            # preflight gated it under one that it does. A slow-but-healthy
            # reference run then exits 124, `_classify` raises, and the task
            # becomes ungradable -- silently, since the cache stores only the
            # verdict.
            runner = _Runner(container, task.tests.runner,
                             task.budget.suite_timeout_s)
```

In `ensure_oracle`, drop `timeout_s: int = 600` from the signature and change its `_derive(...)` call to `_derive(task, image, cache_root)`.

- [ ] **Step 4: Bump `ORACLE_VERSION`**

`"1"` → `"2"`, extending its comment:

```python
#: 1 -> 2: the derivation's two reference runs are bounded by
#: `budget.suite_timeout_s` rather than by a function default. The manifest
#: digest does not cover it -- a manifest that already carries the key loads
#: under the older loader, which ignores unknown `budget` sub-keys -- so
#: without this every cached quarantine would be one derived at 600 against a
#: manifest asking for something else.
ORACLE_VERSION: str = "2"
```

- [ ] **Step 5: Run the tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_oracle.py tests/test_grade_script.py -q`
Expected: PASS. `test_oracle.py` has fingerprint tests that compare keys against each other rather than against a literal; if any hard-codes `"1"`, update it.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/oracle.py bakeoff/tests/test_oracle.py
git commit -m "$(cat <<'EOF'
fix: the quarantine was derived under a bound the task could not ask for

`ensure_oracle(timeout_s=600)` and `_derive(..., timeout_s)` are deleted, not
defaulted. `grade.py` passes neither, so a slow-but-healthy reference suite hit
600 s, `_classify` raised "the p2p run exited 124", and the task became
ungradable -- while preflight, whose bound the manifest now sets, had already
passed it. Both entry points take `task`, so reading `budget.suite_timeout_s`
there is the only source.

ORACLE_VERSION 1 -> 2 for the same reason PREFLIGHT_VERSION moves: a manifest
already carrying the key loads under the older loader, so its digest does not
move and the fingerprint would keep matching a quarantine derived at 600.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: the grader's ladder reads the same bound, and the grade records it

**Files:**
- Modify: `bakeoff/src/bakeoff/grader.py` (`GRADER_VERSION`, `GRADE_TIMEOUT_S`, `_State` and `_State.result`, `LadderResult`, `_check_command`, `_check_f2p`, `_check_p2p`, `_ContainerEnv.scan_secrets`, `build_grade_record`, `__all__`)
- Modify: `bakeoff/src/bakeoff/grade_schema.py` (`GRADE_SCHEMA_VERSION`, `GradeRecord`)
- Test: `bakeoff/tests/test_grader.py`

**Interfaces:**
- Consumes: `task.budget.suite_timeout_s` (Task 1); `preflight._Runner.last_timeout_s` (Task 2).
- Produces: `grader.SCAN_TIMEOUT_S` (replaces `GRADE_TIMEOUT_S`); `LadderResult.suite_timeout_s: int | None`; `GradeRecord.suite_timeout_s: int | None`.

- [ ] **Step 1: Write the failing tests**

In `bakeoff/tests/test_grader.py`, add a budget to the `_task` helper:

```python
def _task(
    paths=("tests/",),
    f2p=("tests/test_calc.py::test_add",),
    p2p=(),
    build=(),
    typecheck=(),
    lint=(),
    suite_timeout_s=600,
):
    return SimpleNamespace(
        ...,
        grading=SimpleNamespace(build=build, typecheck=typecheck, lint=lint),
        budget=SimpleNamespace(
            suite_timeout_s=suite_timeout_s,
            wall_clock_timeout_s=900,
            max_turns=40,
        ),
    )
```

Then add the tests. The recording env is `FakeEnv` (already in the file), it records into `.argvs`, and unmatched argvs answer exit 0 with empty output — so no rules are needed for these. `run_ladder` is called through the file's `_ladder(record=None, task=None, env=None, oracle=None)` helper.

```python
def test_every_graded_command_is_bounded_by_the_manifests_number():
    """Checks 3, 4, 5, 6 and 7 all carry the `timeout` prefix, and all five
    must carry the SAME number the gate used. A build that fits preflight's
    bound and is killed under the grader's stamps `build_failed` -- a
    GradeFailure, so `resolved: False` -- on every arm of the task,
    permanently, in an append-only store, over an environment difference the
    model never saw. That is the same shape as the `--index` stat-cache defect
    one layer up."""
    task = _task(
        suite_timeout_s=1234,
        build=("make", "build"),
        typecheck=("mypy", "."),
        lint=("ruff", "check", "."),
    )
    env = FakeEnv()
    _ladder(task=task, env=env)

    bounded = [argv for argv in env.argvs if argv[0] == "timeout"]
    assert len(bounded) == 5, bounded
    assert {argv[1] for argv in bounded} == {"1234"}


def test_the_grade_records_the_bound_the_ladder_actually_used():
    """`timed_out: True` alone stopped being readable the moment the bound
    became per-task: a reader cannot tell a suite that blew 600 s from one
    that blew 1800 s, and a re-grade under an edited manifest is a NEW line
    whose disagreement with the old one is the finding. The bound has to be
    on the line."""
    task = _task(suite_timeout_s=1234)
    ladder = _ladder(task=task)

    assert ladder.suite_timeout_s == 1234
    grade = build_grade_record(_record(), task, "sha256:image", None, ladder)
    assert grade.suite_timeout_s == 1234


def test_a_grade_where_no_bounded_command_ran_records_no_bound():
    """`None`, not the manifest value. A record refused at the gate or at
    check 1 ran nothing under any bound, and writing the configured number
    there would be configuration reported as observation -- a claim about a
    command that never happened."""
    task = _task(suite_timeout_s=1234)
    # `_record(diff="   \n")` is this file's empty-patch shape: the ladder
    # stops at check 1 with EMPTY_PATCH, before anything is bounded. There is
    # no `final_diff=` keyword -- `_record` takes `diff=`.
    ladder = _ladder(record=_record(diff="   \n"), task=task)

    assert ladder.suite_timeout_s is None


def test_the_gitleaks_bound_is_not_the_tasks_bound():
    """The secret scan is a fixed-size scan of one diff, on the HOST, and
    `_ContainerEnv.scan_secrets` has no task in scope. It keeps a constant --
    renamed, because a constant called GRADE_TIMEOUT_S is precisely what
    invites the next consumer to reach for it instead of the manifest."""
    import bakeoff.grader as grader_module

    assert not hasattr(grader_module, "GRADE_TIMEOUT_S")
    assert grader_module.SCAN_TIMEOUT_S == 600
```

Update the existing gitleaks-timeout test to import and assert `SCAN_TIMEOUT_S` instead of `GRADE_TIMEOUT_S`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py -q -k "bound or timeout"`
Expected: FAIL — `{cmd[1] for cmd in bounded} == {"600"}` not `{"1234"}`, `AttributeError: 'LadderResult' object has no attribute 'suite_timeout_s'`, and `SCAN_TIMEOUT_S` not defined.

- [ ] **Step 3: Rename the constant and narrow it to the scanner**

In `bakeoff/src/bakeoff/grader.py`, replace the `GRADE_TIMEOUT_S` constant:

```python
#: Wall clock for the HOST-side gitleaks scan, and for nothing else.
#: `_ContainerEnv.scan_secrets` shells out to `docker run` rather than through
#: `env.exec`, so it is the one graded command the container's own `timeout`
#: prefix does not cover, and without a bound a wedged daemon hangs the batch
#: forever rather than costing one named row.
#:
#: Every command that runs INSIDE the container -- the suite invocations and
#: the declared `grading.*` argvs -- is bounded by
#: `task.budget.suite_timeout_s` instead, because preflight bounds the same
#: commands by the same manifest value and a suite that fits one bound and is
#: killed under the other stamps `timed_out` on the model. Renamed from
#: `GRADE_TIMEOUT_S` for that reason: a constant called "the grade timeout" is
#: what invites the next consumer to reach for it instead of the manifest.
#: The scan is a fixed-size read of one diff and has no task in scope.
SCAN_TIMEOUT_S: int = 600
```

Update the two uses in `_ContainerEnv.scan_secrets` and the `__all__` entry — `"GRADE_TIMEOUT_S"` → `"SCAN_TIMEOUT_S"`.

- [ ] **Step 4: Read the manifest at the three ladder sites**

`_check_command`, replacing the `env.exec` line and the timeout detail string:

```python
    # The gate ran this same argv under this same number
    # (`preflight._declared_grading`). A build that fits preflight's bound and
    # is killed under the grader's stamps `build_failed` -- a GradeFailure, so
    # `resolved: False` -- on every arm of the task, permanently, over an
    # environment difference the model never saw.
    #
    # `state.suite_timeout_s` is read back off `cmd`, not off the manifest --
    # the same rule `_check_f2p` and `_check_p2p` follow through
    # `_Runner.last_timeout_s`. `build` is check 3 and terminal on failure, so
    # on a build-timeout record this is the ONLY place the bound is ever
    # written; taking it from the manifest there would make the one field that
    # explains a `timed_out` grade configuration reported as observation.
    cmd = ["timeout", str(task.budget.suite_timeout_s), *argv]
    result = env.exec(cmd)
    state.suite_timeout_s = int(cmd[1])
```

and `detail=f"hit the {cmd[1]}s grading timeout"` in the `_TIMEOUT_EXIT` branch.

`_check_f2p`:

```python
    runner = _Runner(env, task.tests.runner, task.budget.suite_timeout_s)
    result = runner.select(tuple(task.tests.f2p))
    # Off the argv, like preflight's evidence -- see `_Runner.last_timeout_s`.
    state.suite_timeout_s = runner.last_timeout_s
```

and `detail=f"hit the {runner.timeout_s}s grading timeout"`.

`_check_p2p`: the same two edits.

- [ ] **Step 5: Carry the value into the record**

`_State` gains, beside the other observation fields:

```python
    #: The bound the LAST bounded command carried, read off its own argv.
    #: Last-writer-wins across checks 3-7 rather than "every command": all of
    #: them read one manifest value, so the three writes agree -- but the
    #: field says what one argv carried, and claiming more of it than that
    #: would be a claim the code does not check.
    #:
    #: `None` until a bounded command runs, which is what a record refused at
    #: the gate or at check 1 looks like -- writing the configured number
    #: there would be a claim about a command that never happened.
    suite_timeout_s: int | None = None
```

`_State.result()` passes `suite_timeout_s=self.suite_timeout_s`; `LadderResult` gains `suite_timeout_s: int | None = None`.

`GradeRecord` in `bakeoff/src/bakeoff/grade_schema.py` gains, beside the other ladder-derived counts:

```python
    #: The `timeout` bound every command in this ladder carried, from
    #: `task.budget.suite_timeout_s`. `None` when no bounded command ran.
    #:
    #: `CheckResult.timed_out` alone stopped being readable the moment the
    #: bound became per task: a reader cannot tell a suite that blew 600 s
    #: from one that blew 1800 s, and a re-grade under an edited manifest is a
    #: NEW line whose disagreement with the old one is the finding -- which it
    #: cannot be if neither line says what it was measured against.
    #:
    #: It is the bound and NOT the headroom. A `timed_out` check is a
    #: `GradeFailure` stamped on the model, and nothing on this record says
    #: whether the grader's host was busier than the gate's: there is no host
    #: or contention block here (`RunRecord.host` has one; a grade does not),
    #: and `duration_s` at a timeout is just this number again. The figure
    #: that would settle it -- preflight's OBSERVED suite duration, against
    #: which this is the margin -- is not recorded anywhere yet (`TASKS.md`).
    #: So read a `timed_out` grade as "this bound was hit", never as "this
    #: suite needs more than this bound".
    suite_timeout_s: int | None = None
```

`build_grade_record` copies `suite_timeout_s=ladder.suite_timeout_s`.

Bump `GRADE_SCHEMA_VERSION` `"1.0.0"` → `"1.1.0"`, extending its comment:

```python
# 1.1.0 adds `suite_timeout_s`: with the bound per task, `timed_out` alone
# does not say what was blown.
```

- [ ] **Step 6: Bump `GRADER_VERSION`**

`"3"` → `"4"`, extending the comment block:

```python
#: 3 -> 4: every bounded check reads `task.budget.suite_timeout_s` instead of
#: a module constant, so the ladder's behaviour on the SAME input can change
#: -- a longer bound turns a `timed_out` fail into a pass.
#:
#: No verdict on today's corpus changes: no stored manifest declares the key,
#: so every ladder still runs at 600. It moves anyway because the resume gate
#: in `scripts/grade.py` keys on (run_id, GRADER_VERSION) ALONE and never on
#: `manifest_digest` -- so the first manifest edit that raises the bound would
#: find every affected run already marked graded and keep serving the 600 s
#: verdict, with neither line saying they were measured against different
#: bounds. The bump has to land with the code that makes the divergence
#: possible, not with the manifest that first exercises it.
#:
#: Operator note: as with 2 -> 3, this re-grades EVERY stored run into a fresh
#: `v4` artifacts directory beside the existing one. That is intended -- a
#: verdict under a different ladder is a new line whose disagreement with the
#: old one is the finding -- and it costs a full grading pass per event log.
#: Schedulable rather than urgent, precisely because no verdict changes until
#: a manifest raises the key.
```

- [ ] **Step 7: Run the tests**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py tests/test_grade_script.py tests/test_grade_schema.py -q`
Expected: PASS. Then the whole suite: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`.

- [ ] **Step 8: Commit**

```bash
git add bakeoff/src/bakeoff/grader.py bakeoff/src/bakeoff/grade_schema.py \
        bakeoff/tests/test_grader.py
git commit -m "$(cat <<'EOF'
fix: the ladder bounded the gate's own commands by a different number than the gate did

Checks 3-7 wrapped every command in `timeout GRADE_TIMEOUT_S` while preflight
wrapped the same commands in `timeout <its own default>`. Both were 600, so
nothing diverged yet -- and the moment `budget.suite_timeout_s` let a task ask
for more, a suite or a build that fits the gate's bound and is killed under the
grader's would stamp `timed_out` on the model. That is a GradeFailure, so
`resolved: False`, on every arm of the task, permanently, in an append-only
store, over an environment difference the model never saw.

All five bounded checks now read `task.budget.suite_timeout_s`, which every one
of them already had in scope. `GRADE_TIMEOUT_S` is renamed `SCAN_TIMEOUT_S` and
narrowed to the host-side gitleaks `docker run`, which has no task in scope and
is not the task's suite: a constant called "the grade timeout" is exactly what
invites the next consumer to reach for it instead of the manifest.

`GradeRecord.suite_timeout_s` records what the ladder actually carried, `None`
when nothing bounded ran. `timed_out: True` alone stopped being readable when
the bound became per task -- a re-grade under an edited manifest is a new line
whose disagreement with the old one is the finding, which it cannot be if
neither line says what it was measured against.

GRADER_VERSION 3 -> 4 (a longer bound turns a timed_out into a pass, and
grade.py skips runs already graded under the current version);
GRADE_SCHEMA_VERSION 1.0.0 -> 1.1.0 for the additive field.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: pin that the gated bound and the graded bound are one number

**Files:**
- Modify: `bakeoff/tests/test_preflight.py` (the cross-module pin goes here, beside `_ScriptedContainer` and the existing "the seams the offline grader rides" section)
- Test: same file

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing; this task is the guarantee.

The three prior tasks each read the manifest independently. Nothing yet fails if one of them is reverted to a constant while the others are not, and that is the exact defect being closed — so it gets its own test, its own review gate, and its own commit.

- [ ] **Step 1: Write the failing test**

Append to `bakeoff/tests/test_preflight.py`, in the "the seams the offline grader rides" section:

```python
def test_the_gate_and_the_grader_bound_one_manifest_by_one_number(
    monkeypatch, tmp_path
):
    """The invariant this broadening is FOR, asserted across the seam rather
    than on either side of it.

    Preflight and the ladder run the same commands -- the f2p selection, the
    scoped p2p run, each declared `grading.*` argv -- and each wraps them in
    its own `timeout` prefix. Written as two independent reads of the manifest
    (which is what Tasks 2 and 4 are), reverting either one to a constant
    leaves both files' own tests green: preflight would gate at 600 and pass a
    slow task, the ladder would kill the same suite at the manifest's 1234, or
    the reverse. What comes out is `timed_out` -- a GradeFailure, so `resolved:
    False` -- on every arm of that task, permanently, in an append-only store,
    over a number the model never saw.

    Asserted on the ARGVs both sides actually emitted, never on "both read the
    same attribute": a mock that watches the attribute is green through a
    consumer that reads it and then discards it.

    The grader's checks are called directly rather than through `run_ladder`,
    because the ladder's early rungs need a patch, a tree and a `git apply`
    that have nothing to do with this property.
    """
    from bakeoff import grader

    task = _FakeTask(
        budget=_FakeBudget(suite_timeout_s=1234, wall_clock_timeout_s=3600),
        grading=TaskGrading(lint=("ruff", "check", ".")),
    )

    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))
    result = _run_preflight(monkeypatch, tmp_path, task, container)
    assert result.ok, result.problems
    gated = {cmd[1] for cmd in container.commands if cmd[0] == "timeout"}
    assert gated == {"1234"}, container.commands

    graded_commands: list[list[str]] = []

    class _Env:
        def exec(self, argv):
            graded_commands.append(list(argv))
            return _Exec(exit_code=0)

    state = grader._State()
    env = _Env()
    grader._check_command(state, "lint", task, env)
    grader._check_f2p(state, task, env)
    grader._check_p2p(state, task, env, None)

    graded = {cmd[1] for cmd in graded_commands if cmd[0] == "timeout"}
    assert graded == gated
    assert state.suite_timeout_s == 1234
```

- [ ] **Step 2: Run it to verify it passes on the finished code, and fails on a revert**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py::test_the_gate_and_the_grader_bound_one_manifest_by_one_number -v`
Expected: PASS.

Then prove it is not vacuous. Temporarily change `_check_f2p`'s `_Runner(env, task.tests.runner, task.budget.suite_timeout_s)` back to a literal `600` and re-run:
Expected: FAIL with `{'600', '1234'} == {'1234'}`.
Revert the temporary edit and re-run: PASS.

Repeat the same revert-and-check for `_check_command`'s `bound` and for preflight's `timeout_s = task.budget.suite_timeout_s`. Each must turn the test red. If any does not, the test is not covering that site — widen it before continuing.

- [ ] **Step 3: Verify no driver was left behind**

Run:
```bash
cd bakeoff && grep -rn "timeout_s\s*=\s*6\|600" src/bakeoff/preflight.py \
    src/bakeoff/oracle.py src/bakeoff/grader.py scripts/run_matrix.py scripts/grade.py
```
Expected: the only surviving literal `600` is `grader.SCAN_TIMEOUT_S` and the default on `TaskBudget.suite_timeout_s` in `tasks.py` (not in this grep's paths). No `timeout_s=` keyword argument appears at any `preflight(` or `ensure_oracle(` call site.

Run the full suite: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 4: Run the integration legs**

This change alters the argv every containerised suite invocation carries, so the container-touching tests must run:

```bash
cd bakeoff && .venv/bin/python -m pytest -v -m integration \
    --basetemp="$HOME/.cache/bakeoff-pytest"
```
Expected: PASS (needs a Docker daemon; the report must say whether this leg was run).

Then the task gate against the real manifest:
```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only
```
Expected: `preflight PASS` for `click-3360-write-usage-empty-args`. The `PREFLIGHT_VERSION` bump invalidates the cached verdict, so this re-runs the gate rather than serving a hit — which is the point of the bump, and confirms it.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/tests/test_preflight.py
git commit -m "$(cat <<'EOF'
test: the gate and the ladder each read the manifest, and nothing said they agreed

Tasks 2 and 4 made preflight and the grader read `budget.suite_timeout_s`
independently. Reverting either one to a constant leaves both files' own tests
green -- and produces `timed_out`, a GradeFailure, on every arm of a task whose
suite fits one bound and not the other. Asserted on the argvs both sides
actually emitted, never on "both read the same attribute": a mock that watches
the attribute stays green through a consumer that reads it and discards it.

Verified non-vacuous by reverting each of the three sites in turn and
confirming the test goes red.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: docs

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md` (the "suite is fast enough" bullet)
- Modify: `docs/BUILDING-A-TASK-SET.md` (the screening table row for a green suite, and the `budget:` example)
- Modify: `tasks/todo.md` (append a review section)
- Modify: `TASKS.md` (two named follow-ups)

- [ ] **Step 1: Rewrite HARVESTING.md's "fast enough" bullet**

Replace the bullet. Two corrections beyond the new key: preflight makes **five** suite invocations on the branch every task actually takes (the scoped p2p run is guarded by `if not tests.p2p`), not four; and it also runs one command per declared `grading.*` argv.

```markdown
- **The suite is fast enough — or the manifest says how slow.** Preflight runs
  the suite **five** times (f2p before, p2p before, f2p after, p2p after, and
  the scoped p2p the grader will make — four when `tests.p2p` is declared
  explicitly, which skips the scoped run), plus once per declared `grading.*`
  argv. The oracle then runs it twice more at grade time and the ladder up to
  five times per graded record. Each of those carries a coreutils `timeout`
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
```

- [ ] **Step 2: Update BUILDING-A-TASK-SET.md**

The screening table's green-suite row becomes:

```markdown
| suite green, under ~40 s | usable. Preflight runs it five times per task (four with an explicit `tests.p2p`) plus once per declared `grading.*` argv, the oracle twice more, and the agent re-runs it inside its own timeout |
| suite green but slow | usable only with `budget.suite_timeout_s` raised — and it must stay ≤ `budget.wall_clock_timeout_s`, or `load_task` refuses the manifest. Costs up to 8× the value per task at the gate, before the proxy starts and inside the one-hour SSO window |
```

And the `budget:` example gains the commented line, matching the manifest:

```yaml
budget:
  max_turns: 40
  wall_clock_timeout_s: 900
  # suite_timeout_s: 600   # the timeout on every command preflight, the
  #                        # oracle and the grader run in the container.
  #                        # Must not exceed wall_clock_timeout_s.
```

- [ ] **Step 3: Append the review section to `tasks/todo.md`**

Match the existing sections' shape (what shipped, what moved, what was learned). Include, at minimum:

- The key, its default, and that it bounds the declared `grading.*` argvs as well as the suite — one key, because a second one is a second thing that can diverge between the gate and the grader.
- The parameter **deletion** (not defaulting) on `preflight()` and `ensure_oracle()`, and that neither driver needed a line changed as a result — the evidence that the seam was the right one.
- The `> wall_clock_timeout_s` load refusal, and why it is a load error rather than a preflight problem or author advice.
- `PREFLIGHT_VERSION` +1, `ORACLE_VERSION` 1 → 2, `GRADER_VERSION` 3 → 4, `GRADE_SCHEMA_VERSION` 1.0.0 → 1.1.0, `SCHEMA_VERSION` **unmoved** (the bound never touches the agent), `click-3360`'s `start_sha` **unmoved**, `manifest_digest` moved (the YAML comment).
- The operator cost: `GRADER_VERSION` 3 → 4 re-grades every stored run into a fresh `v4` artifacts directory; the `PREFLIGHT_VERSION` and `ORACLE_VERSION` bumps invalidate every cached verdict and quarantine on purpose.
- The doc correction found on the way: `HARVESTING.md` said preflight runs the suite "four times"; it is five on the deselect branch every task takes, plus one per declared `grading.*` argv.
- `GRADE_TIMEOUT_S` → `SCAN_TIMEOUT_S`, and why the gitleaks scan keeps a constant.

- [ ] **Step 4: Add the two follow-ups to `TASKS.md`**

Only work deliberately left open:

- **P3 — `max_turns` and `wall_clock_timeout_s` still parse with a bare `int(...)`.** `int("forty")` raises a `ValueError` out of `load_task` with no manifest path in it, and `wall_clock_timeout_s: true` becomes 1. `tasks._positive_int` exists and is applied to `suite_timeout_s` only; extending it to the other two could refuse a manifest that loads today, so it is its own change.
- **P3 — preflight's observed suite duration is not recorded, and it is the figure two separate readings need.** (a) `budget.suite_timeout_s` multiplies through up to 8× per task at the gate, all of it before the proxy starts and inside the one-hour SSO window, and nothing records how long the gate actually took — so the interaction with `credential_window` can only be documented (HARVESTING.md), never checked; a gate on the worst case would refuse task sets that run fine. (b) A `timed_out` grade is a `GradeFailure` stamped on the model, and `GradeRecord.suite_timeout_s` says only which bound was hit — the **headroom**, the gap between what the suite needs and what it was given, is the thing a reader actually wants, and it is unrecorded on both sides. `GradeRecord` also carries no host or contention block (`RunRecord.host` does), so nothing on the line can say the grader's host was busier than the gate's. One measurement — preflight's per-run elapsed suite time in the evidence — answers both.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/taskset/HARVESTING.md docs/BUILDING-A-TASK-SET.md \
        tasks/todo.md TASKS.md
git commit -m "$(cat <<'EOF'
docs: the suite ceiling is per task now, and the run count in HARVESTING was wrong

`budget.suite_timeout_s` makes the 600 s bound a manifest value, so
HARVESTING's "fast enough" rule becomes a per-task ceiling with the ~40 s
figure kept as advice rather than as the rule. Two corrections found while
rewriting it: preflight runs the suite FIVE times on the branch every task
actually takes -- the scoped p2p run is guarded by `if not tests.p2p` -- not
four, and it also runs each declared `grading.*` argv under the same bound.

The multiplied cost is documented rather than gated: the worst case is 8x the
value for one task's gate, spent before the proxy starts and inside the same
one-hour SSO session the matrix needs. Nothing measures the gate's actual
elapsed time yet, so a check on the worst case would refuse task sets that run
fine -- that measurement is a named follow-up in TASKS.md.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes

- **Coverage.** Every consumer in D3's table has a task: preflight (2), oracle (3), grader (4), the cross-seam pin (5). The drivers appear only as "verify unchanged" (Task 5, Step 3), which is the correct outcome of deleting the parameters. `smoke_test.py`/`dry_run.py` call neither preflight nor the oracle and are correctly absent.
- **Names used consistently:** `budget.suite_timeout_s` (manifest and `TaskBudget` field), `tasks._positive_int`, `tasks._ABSENT`, `preflight._Runner.last_timeout_s`, `grader.SCAN_TIMEOUT_S`, `_State.suite_timeout_s`, `LadderResult.suite_timeout_s`, `GradeRecord.suite_timeout_s`, `evidence["suite_timeout_s"]`.
- **The one place the plan defers to the tree rather than a literal:** `PREFLIGHT_VERSION`, which is "read it, add one" because broadening 3 lands first.
- **No new evidence keys are read.** The plan writes `suite_timeout_s` and reads none of broadening 3's.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

Not edited here — `CLAUDE.md` has an uncommitted change on `main` and the merge would conflict.

1. Under **Invariants**, a new bullet:

   > **One bound, read from the manifest, or the gate and the grader accuse the model of the difference.** `budget.suite_timeout_s` (default 600) is the coreutils `timeout` prefix on every command preflight, the oracle and the ladder run inside the container — each suite invocation *and* each declared `grading.*` argv, because a second key for the grading commands is a second number that can diverge. `preflight()` and `ensure_oracle()` take **no** `timeout_s` parameter: a defaulted parameter beside a manifest key is two sources for one number, and the divergence is silent — a suite that fits the gate's bound and is killed under the grader's stamps `timed_out`, a `GradeFailure`, so `resolved: False`, on every arm of that task, permanently, in an append-only store, over a number the model never saw. Deleting the parameter makes "a consumer left on the constant" unrepresentable rather than merely tested for; `grader.SCAN_TIMEOUT_S` survives as a constant only because the host-side gitleaks `docker run` has no task in scope and is not the task's suite. `suite_timeout_s` may not exceed `wall_clock_timeout_s` and `load_task` refuses the manifest with the arithmetic when it does — the agent re-runs the suite inside its wall clock with no per-command bound, so a longer one describes a task no arm can verify even once and §3.3's self-correction loop is truncated by a mid-suite SIGTERM whose record is indistinguishable from an honest `BUDGET_EXHAUSTED`.

2. Under **Config gotchas**, appended to the existing budget/version prose:

   > A manifest key that a *newer* loader reads is invisible to `manifest_digest` when an *older* loader ignored it: `budget.suite_timeout_s: 1800` loads fine under a loader that reads only `max_turns` and `wall_clock_timeout_s`, so the digest does not move when the reading code lands. That is why `PREFLIGHT_VERSION`, `ORACLE_VERSION` and `GRADER_VERSION` all moved for this key even though all three caches already hash the digest — a warm cache would otherwise serve a verdict computed under a bound the manifest no longer asks for.

3. Under **Invariants**, appended to the preflight bullet:

   > Preflight runs the suite **five** times on the deselect branch (f2p before, p2p before, f2p after, p2p after, scoped p2p after) and four when `tests.p2p` is declared, plus one command per declared `grading.*` argv — so `budget.suite_timeout_s` multiplies by up to 8 per task, all of it before the proxy starts and inside the same one-hour SSO session the matrix needs.
