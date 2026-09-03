# Round 2, item 6: every `budget:` number goes through one validator — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `budget.max_turns` and `budget.wall_clock_timeout_s` stop being parsed by a bare `int(...)`. All three budget keys go through `tasks._positive_int`, so a quoted, floated, boolean, null, zero or negative value is a `TaskError` that names the manifest path and the key, instead of either a bare `ValueError`/`TypeError` with no manifest in it or — worse — a silently accepted number the author did not write. The `suite_timeout_s > wall_clock_timeout_s` comparison keeps running strictly *after* all three are validated, so a bad `wall_clock_timeout_s` is reported as itself and never as a comparison failure against a `suite_timeout_s` the author never declared. One hygiene sibling rides along, because leaving it out would ship the same inconsistency one block up: `budget:` and `image:` stop reading a written-but-falsy section (`[]`, `0`, `""`) as an unwritten one, matching the `grading:` block that already does (D4).

**Architecture:** No new function and no new concept. `_positive_int` already exists, already refuses `bool` before it refuses non-`int`, and is already applied to `suite_timeout_s`; this item extends it to the two keys its own docstring says were deliberately left out. One statement changes in `load_task`, two adjacent statements (`data.get("budget") or {}` and `data.get("image") or {}`) stop reading a written-but-falsy section as an unwritten one, and the comparison block below is left exactly where it is — its correctness now rests on the validation above it, which is what the new ordering test pins.

**Tech Stack:** Python 3.12.13 (the harness venv), PyYAML 6.0.3, pytest. No Docker, no network, no credentials for the unit work; one Docker-only preflight re-run is needed because a doc edit to the click manifest invalidates its cached verdict (see V4).

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` §3.7 (the manifest), §5.4 (the caps are placeholders until the §3.5 calibration pilot sets them). Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **6**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind. Repo invariants: `CLAUDE.md`, "Silence is the enemy" and "Configuration is never reported as observation" — a manifest that says `max_turns: true` and runs at 1 turn is configuration that stopped being a faithful record of what was asked for.

**Review state:** **APPROVED.** Review 1 (`.superpowers/broaden/round2/plan-6-review-1.md`) returned REVISE with 11 findings, 2 blocking; all 11 addressed and all four open questions resolved per the reviewer's rulings. Review 2 (same file, `# Review 2 — the revised plan`) **APPROVED** with 6 open findings, all LOW, all folded in. See **Review 1 → changes** and **Review 2** at the end. Nothing is disputed. Ready to implement.

---

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. A comment that says what a line does rather than what breaks without it does not fit here. Claims about external behaviour are annotated with what they were verified against.
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section.
- **THIS PLAN IS WRITTEN AGAINST HEAD 8232032 AND MAY NOT BE IMPLEMENTED ON IT.** `CONTEXT.md:34` makes implementation sequential, but **do not assume items 1–5 have landed**: measured 2026-09-02, HEAD was still 8232032 and the working tree carried **item 3 only**. Item 4 (`load_task_set` validates every sibling before `--tasks` filters) is the one other round-2 item that edits `tasks.py`, and it is **measured disjoint** from this one: its edits are confined to `load_task_set` and new helpers (`RefusedManifest`, `_manifest_committed`, `_fatal_reason`, `load_task_set_with_refusals`), it does not touch `load_task`'s budget block, and its five mutation anchors share no text with this plan's three. **Item 6 applies cleanly in either order.** The one interaction worth naming is downstream and changes nothing here: after item 4, `load_task_set` downgrades a refusal on an *uncommitted, unselected* sibling to a warning, so where a new budget refusal *surfaces* depends on whether item 4 has landed. `load_task` itself refuses identically either way, and V2 calls it directly. Therefore **every `tasks.py:NNN` and `test_tasks.py:NNN` below is "at HEAD 8232032; re-locate by symbol or by the exact quoted source text"**; every edit is addressed by quoted text, not by line number. Confirmed in review 1: every quoted "before" block in T1.1, T1.2, T1.4, T1.5, T1.7, T4.1 and T5 matches the current file byte for byte.
- **`SCHEMA_VERSION` does NOT move.** No `RunRecord` field is added, removed or changes meaning. A record's `Versions` block does not carry `budget` at all.
- **`GRADE_SCHEMA_VERSION` and `GRADER_VERSION` do NOT move.** The ladder, the oracle and the grader's env are untouched. `GradeRecord.suite_timeout_s` still copies `task.budget.suite_timeout_s`, and the values it can copy are unchanged for every manifest that loads today (D1/M5).
- **`PREFLIGHT_VERSION` does NOT move.** `preflight()` reads `task.budget.*`; it does not parse them. No check it makes changes, no verdict changes, and no evidence key is added or changes meaning. A cached verdict written before this commit is still a correct statement about the same task. (`manifest_digest` moves for the one manifest whose comments this plan edits — that is the cache key doing its job, not a version bump; see V4.)
- **`ORACLE_VERSION` does NOT move.**
- **`task_version` does NOT move on any task**, and neither does any `start_sha`. The only manifest edit in this plan is to comments (T5), and the start state is `base_sha` plus the test half — the manifest is not an input to it.
- **No verdict moves and no refusal that exists today is removed.** This commit only *adds* refusals, all of them at load time, all of them before an image is built or a token is spent.
- **Commit hygiene:** one commit (plan + code + tests + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Indentation in this file's code fences.** **Every fence in this plan carries two extra leading spaces from the markdown list — subtract two — EXCEPT Task 3's, which are at true source indentation because `mutation_check.py` matches them byte for byte.** The rule covers Tasks 4, 5 and 6 as well as Task 1, and getting it wrong is not always loud: T5.2's "before" block occurs **1** time in `click-3360-write-usage-empty-args/task.yaml` after subtracting two and **0** times verbatim, so a +2 transcription is a replacement that silently matches nothing. Measured examples: `budget = TaskBudget(` shown at 6, real 4 (`tasks.py`); `# A positive integer…` shown at 4, real 2 (`task.yaml`); `budget:` shown at 2, real 0 (`BUILDING-A-TASK-SET.md`); and, the exception, `max_turns=_positive_int(` shown at 8, real 8 (`tasks.py`). Verify each `find` against the file before adding it: `grep -c` must return exactly 1 — measured in review 1, all four are already unique on their own text (`budget_raw` vs `grading_raw` vs `image_raw`, and one `TaskBudget(...)` call site), with or without Task 1's new comments.
- **Do not run `scripts/mutation_check.py` concurrently with anything else**; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — record the number it prints. **Do not predict it** (V1): the bullet at the top of this section retracts the assumption that items 1–5 have landed, and this one used to contradict it. Measured 2026-09-02 the tree stood at **1554**, against the round's opening 1539.

---

## The measured defect

**Source:** `TASKS.md`, "**`max_turns` and `wall_clock_timeout_s` still parse with a bare `int(...)`.**" (`TASKS.md:1499-1504` at HEAD 8232032).

Every measurement below was taken on **2026-09-02** on this worktree, Python **3.12.13**, PyYAML **6.0.3**, via `bakeoff/.venv/bin/python`, against a scratch copy of the real click manifest at `$HOME/.cache/bakeoff-round2-6/set/click` (`bakeoff/taskset/click-3360-write-usage-empty-args` copied verbatim, its `budget:` block replaced per row, `load_task()` called on the directory). `load_task` touches no network and no container, so the copy loads exactly as the original does — the baseline row confirms it.

### M1 — the input table

`OK` means `load_task` returned a manifest. The **intended** column is what this plan makes it do.

| YAML written under `budget:` | PyYAML gives | today's `load_task` | intended |
|---|---|---|---|
| `max_turns: 40` | `int` `40` | OK, `max_turns=40` | unchanged — OK, `max_turns=40` |
| `max_turns: "40"` | `str` `'40'` | **OK, `max_turns=40`** | `TaskError: <path>/task.yaml:budget.max_turns: must be a positive integer, got '40'` |
| `max_turns: 40.0` | `float` `40.0` | **OK, `max_turns=40`** | `TaskError: …:budget.max_turns: must be a positive integer, got 40.0` |
| `max_turns: 3.7` | `float` `3.7` | **OK, `max_turns=3`** | `TaskError: …:budget.max_turns: must be a positive integer, got 3.7` |
| `max_turns: true` | `bool` `True` | **OK, `max_turns=1`** | `TaskError: …:budget.max_turns: must be a positive integer, got True` |
| `max_turns: false` | `bool` `False` | **OK, `max_turns=0`** | `TaskError: …:budget.max_turns: must be a positive integer, got False` |
| `max_turns: 0x28` | `int` `40` | **OK, `max_turns=40`** | unchanged — OK, `max_turns=40` (D6) |
| `max_turns: 0` | `int` `0` | **OK, `max_turns=0`** | `TaskError: …:budget.max_turns: must be a positive integer, got 0` |
| `max_turns: -1` | `int` `-1` | **OK, `max_turns=-1`** | `TaskError: …:budget.max_turns: must be a positive integer, got -1` |
| `max_turns: null` | `None` | `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'` | `TaskError: …:budget.max_turns: must be a positive integer, got None` |
| `max_turns: forty` | `str` `'forty'` | `ValueError: invalid literal for int() with base 10: 'forty'` | `TaskError: …:budget.max_turns: must be a positive integer, got 'forty'` |
| `max_turns: 1e3` | `str` `'1e3'` | `ValueError: invalid literal for int() with base 10: '1e3'` | `TaskError: …:budget.max_turns: must be a positive integer, got '1e3'` |
| `max_turns: [40]` | `list` | `TypeError: int() argument must be … not 'list'` | `TaskError: …:budget.max_turns: must be a positive integer, got [40]` |
| `wall_clock_timeout_s: "900"` | `str` `'900'` | **OK, `wall_clock_timeout_s=900`** | `TaskError: …:budget.wall_clock_timeout_s: must be a positive integer, got '900'` |
| `wall_clock_timeout_s: forty` | `str` | `ValueError: invalid literal for int() with base 10: 'forty'` | `TaskError: …:budget.wall_clock_timeout_s: must be a positive integer, got 'forty'` |
| `wall_clock_timeout_s: null` | `None` | `TypeError: int() argument must be … not 'NoneType'` | `TaskError: …:budget.wall_clock_timeout_s: must be a positive integer, got None` |
| `wall_clock_timeout_s: true` | `bool` `True` | **`TaskError: …: budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (1) by 599s. …Raise wall_clock_timeout_s, or lower suite_timeout_s…`** | `TaskError: …:budget.wall_clock_timeout_s: must be a positive integer, got True` |
| `wall_clock_timeout_s: 0` | `int` `0` | **`TaskError: …: budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (0) by 600s. …`** | `TaskError: …:budget.wall_clock_timeout_s: must be a positive integer, got 0` |
| `wall_clock_timeout_s: 0.5` | `float` `0.5` | **`TaskError: …: budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (0) by 600s. …`** | `TaskError: …:budget.wall_clock_timeout_s: must be a positive integer, got 0.5` |

Bare-`int` behaviour, measured separately and identical to the table: `int("forty")` → `ValueError`, `int(True)` → `1`, `int(False)` → `0`, `int(3.7)` → `3`, `int("40")` → `40`, `int(None)` → `TypeError`.

### M2 — the three surprising rows, and why each matters

1. **`wall_clock_timeout_s: true` / `: 0` / `: 0.5` are reported as a `suite_timeout_s` problem.** This is the worst row in the table and it is the one the item's design brief singles out. The click manifest does not declare `suite_timeout_s` at all — the 600 comes from `TaskBudget.suite_timeout_s`'s default — so the author is handed a refusal naming a key they never wrote, ending in *"or lower suite_timeout_s to what the suite actually needs"*, which is advice about the wrong number. The one key they *did* get wrong is mentioned only as the thing to raise. `int(True)` is `1`, so the arithmetic in that message ("by 599s") is arithmetic over a boolean. Verbatim, measured:

   ```
   /Users/…/task.yaml: budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (1) by 599s. The agent re-runs this suite inside its wall clock, so it could not verify its own work even once -- the run would be terminated mid-suite with an unchecked diff, and that is indistinguishable from a model that simply ran out of time. Raise wall_clock_timeout_s, or lower suite_timeout_s to what the suite actually needs.
   ```

2. **`1e3` is a `str` in YAML, not a `float`.** PyYAML 6.0.3 implements YAML 1.1's float resolver, which requires a decimal point in the mantissa and a signed exponent (`1.0e+3`); a bare `1e3` fails the pattern and resolves as a string. So the "raise the timeout by an order of magnitude" shorthand an author is most likely to reach for is the one input that produces a bare `ValueError` today and a message naming the key after this change. It is in the parametrization for exactly that reason.

3. **`max_turns: false` loads as `0`, and `--max-turns 0` is not refused by the CLI either.** Measured 2026-09-02 against **claude 2.1.258** (the host binary; the eval image pins 2.1.220 — see D5's caveat) with `ANTHROPIC_BASE_URL=http://127.0.0.1:1` so nothing could be spent: `claude -p --output-format stream-json --verbose --max-turns 0 "say hi"` emits the `system/init` event and then `api_retry` events — it starts a session and calls the API. `--max-turns -1` and `--max-turns 0.5` behave the same. Only a non-numeric value is refused, by commander, before any network:

   ```
   error: option '--max-turns <turns>' argument 'abc' is invalid. must be a number
   ```

   `--max-turns` is **not listed in `claude --help`** at 2.1.258. So there is no downstream backstop: a `max_turns` of `0`, `false` or `-1` reaches a container, spends tokens on at least one call, and produces a record for a run that was never allowed to work. Refusing it at load is the only place it gets caught.

### M3 — the second silent-default: `budget: []`

Measured on the same scratch manifest. `budget_raw = data.get("budget") or {}` reads *any falsy value* as an unwritten section:

| YAML | today | intended |
|---|---|---|
| `budget: []` | **OK, all three defaults (40/900/600)** | `TaskError: <path>/task.yaml: budget must be a mapping` |
| `budget: 0` | **OK, all three defaults** | `TaskError: …: budget must be a mapping` |
| `budget: ""` | **OK, all three defaults** | `TaskError: …: budget must be a mapping` |
| `budget: true` | `TaskError: …: budget must be a mapping` | unchanged |
| `budget: null` | OK, all three defaults | unchanged — OK, all three defaults |
| absent | OK, all three defaults | unchanged |

`budget: []` is what an author who started a list and never wrote the keys leaves behind — the exact shape, and the exact wording, of the `grading:` comment **nine lines below** it in the same function: *"`is None`, NOT `or {}`: `grading: []` is what an author who started a list and never wrote the keys leaves behind"*. The precedent is not only a comment: `test_a_grading_section_that_is_not_a_mapping_is_refused_at_load` is already parametrized over exactly `['grading: ruff', 'grading: []', "grading: ''", 'grading: 0']`. The fix and its test both already exist in this file; the budget block simply predates them. See D4.

### M4 — what does *not* change for any manifest that exists

All nine manifests on disk declare exactly `max_turns: 40` and `wall_clock_timeout_s: 900`, both plain YAML ints. One — `pytest-10210-approx-nested-container` — also declares `suite_timeout_s: 240`, itself a plain positive int; no manifest declares any other shape. Re-verified in review 1 against a patched package: all nine load, and all nine `TaskBudget(...)` lines are identical before and after.

`bakeoff/taskset/click-3360-write-usage-empty-args`, and under `~/.cache/bakeoff-probe/taskset/`: `bidict-389-putall-rollback-clean`, `chimera-228-equinox-numeric`, `pytest-10210-approx-nested-container`, `sqlglot-6927-dremio-trycast`, `tomlkit-514-inline-table-comment-separator`, `ufo-214-without-trailing-slash-query`, `werkzeug-3037-duplicate-rule-error`, `yaml-474-single-newline-empty-value`.

So `TASKS.md`'s stated reason for deferring this ("extending it to the other two could refuse a manifest that loads today") is measured to be **vacuous for the corpus that exists** — the risk is real for a manifest someone writes later, which is the manifest this refusal is for. V2 re-runs the check at implementation time, because the probe task set is not version-controlled here and may have grown. It already has: as of 2026-09-02 `~/.cache/bakeoff-probe/taskset/` holds a **tenth** directory, `boltons-90-statsutils-mode`, carrying a `reference.diff` and **no `task.yaml`**. V2's glob is on `*/task.yaml` and skips it silently, which is why V2 also prints the directory count it walked — a probe directory that has since grown a manifest, or lost one, has to be visible rather than skipped.

### M5 — nothing downstream re-validates, and one thing copies

`task.budget.max_turns` is read by `scripts/run_matrix.py` (into `ClaudeCodeConfig.max_turns`, which `claude_runner.build_command` renders as `--max-turns <str>`), and `task.budget.wall_clock_timeout_s` by `run_matrix.py` (into the same config) and by **`run_matrix`'s per-cell margin** (`wall_clock_timeout_s + CELL_OVERHEAD_S`, `run_matrix.py:593,638`), which is handed to `proxy.credential_stop` (`proxy.py:379`). There is no `credentials` module — only `tests/test_credentials.py`. Neither re-checks the value. `GradeRecord` copies `suite_timeout_s` only. So every consumer inherits whatever `load_task` returned, and a `max_turns` of `1` derived from `true` reads downstream as a task the author capped at one turn.

---

## Design decisions, settled

### D1. Both keys go through the existing `_positive_int`, unchanged

`_positive_int` already does everything this item needs, and — checked at HEAD 8232032 — **it already refuses `bool` correctly**, testing `isinstance(value, bool)` *before* `isinstance(value, int)`:

```python
    if value is _ABSENT:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskError(f"{where}: must be a positive integer, got {value!r}")
    return value
```

So the design brief's contingency ("or a sibling that also refuses `bool` — check whether `_positive_int` already does; if not, fix it there so `suite_timeout_s` gets the same protection") resolves to: **it does; no sibling, no fix, no behaviour change to `suite_timeout_s`.** The function body is not edited at all — only its docstring, whose last paragraph currently states the deferral this item closes.

**Rejected: a per-key message.** A message like "max_turns bounds how many API calls an arm may make" would read better in isolation, but it forks one refusal into three that can drift, and `where` already carries `<manifest path>:budget.<key>` — the two things the brief requires the refusal to name. One function, one message, one mutation anchor per call site.

**Rejected: coercing instead of refusing** (accept `"40"` and `40.0` when integral). That is the defect stated as a feature: the manifest is the record of what every arm was asked to do, and a value the loader silently rewrote is configuration reported as something the author wrote. `_positive_int`'s own docstring already argues this for `suite_timeout_s`.

### D2. `_ABSENT`, not the dataclass default, as the `.get` fallback

The call must be `budget_raw.get("max_turns", _ABSENT)`, matching `suite_timeout_s`. Passing `TaskBudget.max_turns` as the `.get` default would make an explicit `max_turns: null` indistinguishable from an absent key, and `_positive_int` would return the default for a key the author *wrote*. The refusal for `null` is the point of the sentinel — this is `CLAUDE.md`'s "A null says which kind of null it is" at manifest level, and `test_a_suite_timeout_that_is_not_a_positive_int_is_a_load_error`'s docstring already states it for the third key.

### D3. The comparison stays where it is, and the ordering is now load-bearing

The `suite_timeout_s > wall_clock_timeout_s` refusal already sits *after* the `TaskBudget(...)` construction, so it already runs after all three keys are read. Nothing moves. What changes is that "read" now means "validated": before this commit the comparison could run on a `wall_clock_timeout_s` of `1` derived from `True`, and it did (M2.1).

Python evaluates keyword arguments left to right, so within `TaskBudget(...)` the refusal order is `max_turns`, then `wall_clock_timeout_s`, then `suite_timeout_s`. A manifest with two bad keys reports the first of those, deterministically. That is stated in a comment at the call site and pinned by T3.4, because "the first bad key wins" is only obvious once you know the evaluation rule.

**Rejected: hoisting the comparison into `_positive_int` or a new `_validate_budget`.** The comparison is not a property of one key; it is arithmetic over two, and it is already commented at length where it lives. Moving it buys nothing and would put a two-key rule inside a one-key validator.

**Rejected: reordering so `wall_clock_timeout_s` is validated first.** Left-to-right matches the dataclass field order, which matches the manifest's own key order in every task on disk and in `docs/BUILDING-A-TASK-SET.md`'s skeleton. An order chosen for any other reason is one more thing to explain.

### D4. A written-but-falsy `budget:` or `image:` section stops reading as an unwritten one (M3)

`budget_raw = data.get("budget") or {}` becomes an explicit `is None` test, so only an absent key or an explicit `budget: null` takes the defaults, and `[]`, `0`, `""` fall through to the existing "budget must be a mapping" refusal — the message and the `isinstance` check below it are untouched. **`image_raw = data.get("image") or {}` gets the same one-statement treatment** (T1.7), against the same untouched `"image must be a mapping"` refusal.

**In scope because it is the same defect one layer up**: a value the author wrote, read as a value they did not write, with nothing downstream able to tell. It is one statement per section, it refuses no manifest that exists (M4 covers both — all nine declare a mapping for each), the precedent and its wording are nine lines below in the same function (the `grading:` block, and its existing parametrized test), and each has its own test and its own mutation anchor, so either can be dropped whole if code review disagrees without touching anything else in this plan.

**Why `image:` comes with it rather than after it.** Leaving it out ships a `tasks.py` in which `grading:` and `budget:` use `is None` and `image:`, two screens above, uses `or {}` — with nothing in the tree saying why. That is the inconsistency this decision exists to remove, moved one block up. And the `image:` instance is *strictly worse* than the budget one. Measured 2026-09-02 on the scratch click manifest:

```
image: []      -> OK  python='3.12' pip=() build=() apt=()
image: 0       -> OK  python='3.12' pip=() build=() apt=()
image: ""      -> OK  python='3.12' pip=() build=() apt=()
image: null    -> OK  python='3.12' pip=() build=() apt=()   (unchanged by this plan)
image: true    -> TaskError: image must be a mapping         (unchanged by this plan)
```

One `image: []` silently discards click's `build: ["pip install -e ."]` **and** its `apt: ["less"]` — the first is the non-editable install `CLAUDE.md` lists among the defects that read as model capability ("imports resolve to site-packages so the agent's edits do nothing"), the second is the missing pager that turns 189 unrelated tests into errors. Neither is a live hole today: `preflight` catches both, which is why this is a hygiene fix and not a P0. But it is a larger instance of exactly the defect the paragraph above argues is worth one statement.

**`provenance:` is deliberately left on `or {}`.** `provenance=dict(data.get("provenance") or {})` is a free dict with **no defaults to silently apply** — an empty one and an absent one produce the same field, and nothing downstream branches on its contents. There is no value a falsy section could discard, so there is nothing for the refusal to protect. Measured 2026-09-02, the case against changing it is stronger than that: `provenance: []`, `: 0` and `: null` all produce `{}` today, identical to absent — but `dict(0)` raises a bare **`TypeError`**, so switching this one to `is None` would *introduce* an unhandled non-`TaskError` path for `provenance: 0`, which is the opposite of what D4 is for. Named here so its absence is a decision rather than an oversight.

`budget: null` deliberately keeps taking the defaults, unlike `max_turns: null` which is refused. The asymmetry is real and is commented: a `null` *section* is what a commented-out block leaves behind, and every key inside it has a default that is exactly today's behaviour; a `null` *key* is an author reaching for one specific number and writing nothing. This also matches the `grading:` block's `is None` precedent exactly.

### D5. `max_turns` gets **no** upper bound

Considered and rejected, for the reason `test_a_suite_timeout_equal_to_the_wall_clock_loads` gives for keeping the comparison strictly `>`: a cap is *a judgement about how much is too much*, not something arithmetic settles, and §5.4 says these numbers come from the §3.5 calibration pilot's slowest-converging model rather than from anyone's intuition. Three further reasons:

- **The real bound is already there.** `wall_clock_timeout_s` terminates the run whatever `max_turns` says; `max_turns` is the *cheaper* of the two stops, not the outer one. A cap would be a second bound on a quantity already bounded.
- **There is no number to pick.** `dry_run.py` uses 60, `test_fault_injection.py` uses 60/10/2, every manifest uses 40. Any ceiling would be a round number with no measurement behind it, on a branch whose whole discipline is that constants cite what they were measured against.
- **The CLI has no opinion** (M2.3): `--max-turns` is undocumented in `claude --help` at 2.1.258 and accepts any number, so a cap here would not be mirroring a downstream limit.

What *is* refused is the bottom: `0`, `-1` and `false`, all of which reach the container and spend tokens on a run that cannot work (M2.3). `_positive_int`'s `value <= 0` gives that with no new code. T2.6 pins the absence of a ceiling so a later cap is a deliberate change rather than an unnoticed one.

**Caveat for the implementer:** the `--max-turns 0` measurement was taken against the **host** binary, claude 2.1.258; the eval image pins **2.1.220** (`CLAUDE.md`, "Verified against claude 2.1.220"). The claim this plan actually rests on is *"nothing downstream refuses a non-positive `max_turns`"*, and 2.1.220 having a stricter check would only mean an arm dies at CLI startup instead of after one API call — the load-time refusal is right either way. Do **not** re-measure inside the image for this commit (it costs an image build for a claim the design does not turn on); write the version into the comment as `verified against claude 2.1.258 on the host` and say so.

### D6. `0x28` stays accepted, and is documented rather than refused

YAML 1.1 resolves `0x28` to the **int** `40` (measured). It arrives at `_positive_int` as a positive `int` and passes, before and after this change. Refusing it would mean re-reading the raw manifest bytes to find out how a valid int was spelled, which is a parser this repo has already learned not to hand-roll. It is a legitimate, if odd, way to write an integer; the manifest is still a faithful record of the number. Not tested for, not refused, and mentioned here only so a reviewer does not read its absence from the parametrization as an oversight.

### D7. Unknown keys under `budget:` are **not** guarded here

`budget:\n  max_turn: 5` and `budget:\n  suite_timeout_ms: 5` both load today with every default applied (measured, M3's script). That is the same class of invisible typo `_IMAGE_KEYS` exists to catch for `image:` — and it is a *different subject* from "these two values are parsed by a bare `int`". It would also be the one change in this area that could refuse a manifest someone has written locally, which is precisely the risk `TASKS.md` cited for deferring the present item.

Deferred, with a new `TASKS.md` entry written in T6 so it is not lost. The fix, when it happens, is `_BUDGET_KEYS = tuple(f.name for f in dataclass_fields(TaskBudget))` beside `_IMAGE_KEYS` plus a refusal naming the unknown key and the accepted set — and T2.5's dataclass-derived test in this plan is the thing that will keep it honest, because it already iterates the same field list.

---

## Tasks

### Task 1 — `load_task`: all three budget keys through `_positive_int`, plus two falsy-section reads

- [ ] **1.1** In `bakeoff/src/bakeoff/tasks.py`, in `load_task`, replace this statement (grep for `budget_raw = data.get`):

  ```python
      budget_raw = data.get("budget") or {}
      if not isinstance(budget_raw, dict):
          raise TaskError(f"{where}: budget must be a mapping")
  ```

  with:

  ```python
      # `is None`, NOT `or {}` -- for the reason the `grading:` block below
      # gives in the same words: `budget: []` is what an author who started a
      # list and never wrote the keys leaves behind, and `or {}` reads it as a
      # section they never wrote, applying all three defaults to a manifest
      # that visibly asked for something else. Measured 2026-09-02: `[]`, `0`
      # and `""` all loaded as 40/900/600. An explicit `budget: null` still
      # takes the defaults, which is a commented-out block and is what the
      # defaults are for -- unlike a null KEY, which is an author reaching for
      # one number and writing none, and is refused by `_positive_int`.
      budget_raw = data.get("budget")
      if budget_raw is None:
          budget_raw = {}
      if not isinstance(budget_raw, dict):
          raise TaskError(f"{where}: budget must be a mapping")
  ```

  The refusal string `f"{where}: budget must be a mapping"` is unchanged, byte for byte.

- [ ] **1.2** Replace the `TaskBudget(...)` construction (grep for `budget = TaskBudget(`):

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
  ```

  with:

  ```python
      # All three through one validator, and the two below the comparison
      # matter most: a bare `int(...)` accepted `wall_clock_timeout_s: true`
      # as 1 and `: 0.5` as 0, and the `>` refusal further down then reported
      # the manifest's problem as a `suite_timeout_s` the author never
      # declared -- advising them to lower the one number that was correct.
      # `_ABSENT` rather than the dataclass default, so `key: null` is refused
      # instead of read as a key nobody wrote.
      #
      # Keyword arguments evaluate left to right, so a manifest with two bad
      # keys is refused by the FIRST in this order. Deterministic, and it is
      # the order the keys appear in the dataclass and in every manifest.
      budget = TaskBudget(
          max_turns=_positive_int(
              budget_raw.get("max_turns", _ABSENT),
              f"{where}:budget.max_turns",
              TaskBudget.max_turns,
          ),
          wall_clock_timeout_s=_positive_int(
              budget_raw.get("wall_clock_timeout_s", _ABSENT),
              f"{where}:budget.wall_clock_timeout_s",
              TaskBudget.wall_clock_timeout_s,
          ),
          suite_timeout_s=_positive_int(
              budget_raw.get("suite_timeout_s", _ABSENT),
              f"{where}:budget.suite_timeout_s",
              TaskBudget.suite_timeout_s,
          ),
      )
  ```

- [ ] **1.3** The `if budget.suite_timeout_s > budget.wall_clock_timeout_s:` block and its comment and message are **unchanged**. Add one sentence to the end of that block's existing comment, before the `if`:

  ```
      # Runs after all three keys are validated above, which is what lets this
      # message be trusted: on a bare `int(...)` it fired for a boolean
      # `wall_clock_timeout_s` and blamed `suite_timeout_s`.
  ```

- [ ] **1.4** Rewrite `_positive_int`'s last docstring paragraph. Replace:

  ```
      `_ABSENT` rather than `None` for "not declared", so an explicit `key: null`
      falls through to the refusal. Applied to `suite_timeout_s` only:
      `max_turns` and `wall_clock_timeout_s` keep their bare `int(...)` because
      tightening them could refuse a manifest that loads today, which belongs in
      its own change (`TASKS.md`).
  ```

  with:

  ```
      `_ABSENT` rather than `None` for "not declared", so an explicit `key: null`
      falls through to the refusal.

      Applied to all three `budget:` keys. `max_turns` and `wall_clock_timeout_s`
      kept a bare `int(...)` until 2026-09-02, and the cost was not the shapes
      that raised -- it was the shapes that did NOT. `wall_clock_timeout_s: true`
      loaded as 1 second, and the `>` refusal below then reported the manifest's
      defect as a `suite_timeout_s` the author had never declared, ending in
      "lower suite_timeout_s", which is advice about the wrong key. `: 0.5`
      loaded as 0 the same way. Measured across the nine manifests that existed
      that day, extending this validator refused none of them.

      No UPPER bound on `max_turns`, deliberately: `wall_clock_timeout_s` is the
      outer stop whatever this says, section 5.4 leaves the number to the
      section 3.5 calibration pilot, and there is no downstream limit to mirror
      AT THE VERSION MEASURED -- verified against claude 2.1.258 on the host,
      where `--max-turns` is absent from `--help` entirely and `--max-turns 0`
      starts a session and calls the API rather than being refused. The eval
      image pins 2.1.220 and was not measured; a stricter check there would
      only move the failure earlier, never make this refusal wrong. The bottom
      is what the container cannot survive, and `value <= 0` is what refuses
      it.
  ```

  The function **body** is not edited.

- [ ] **1.5** Also update `_positive_int`'s first bullet, which is now written in the present tense about behaviour this commit removes. Replace:

  ```
      * `int("forty")` raises a bare `ValueError` out of `load_task`, with no
        manifest path in the traceback -- which is how `max_turns` and
        `wall_clock_timeout_s` behave today.
  ```

  with:

  ```
      * `int("forty")` raises a bare `ValueError` out of `load_task`, with no
        manifest path in the traceback -- and `int(None)` a bare `TypeError`,
        which is how `max_turns` and `wall_clock_timeout_s` behaved before
        2026-09-02. An author is handed a stack trace naming this module
        instead of a message naming their file and their key.
  ```

- [ ] **1.6** Update `TaskBudget`'s field comments in the same file. Above `max_turns: int = 40`, the existing §5.4 comment stays; append:

  ```
      # All three are positive integers, refused at load by name if not --
      # see `_positive_int`. No upper bound on max_turns: wall_clock_timeout_s
      # is the outer stop and section 3.5's pilot owns the number.
  ```

- [ ] **1.7** (D4, `image:`) In the same function, replace this statement (grep for `image_raw = data.get`):

  ```python
      image_raw = data.get("image") or {}
      if not isinstance(image_raw, dict):
          raise TaskError(f"{where}: image must be a mapping")
  ```

  with:

  ```python
      # `is None`, NOT `or {}`, for the reason given at `budget_raw` above and
      # at `grading_raw` below -- and this section is the one where a silently
      # discarded body costs the most. Measured 2026-09-02: `image: []`, `: 0`
      # and `: ""` all loaded as the default python with EMPTY apt, pip and
      # build, so one stray bracket throws away `build: ["pip install -e ."]`
      # (imports then resolve to site-packages and nothing the agent writes
      # takes effect) and `apt: ["less"]` (189 tests error on a closed stdout
      # in the pager test). Preflight catches both, which is the only reason
      # this was ever a hygiene defect rather than a P0.
      image_raw = data.get("image")
      if image_raw is None:
          image_raw = {}
      if not isinstance(image_raw, dict):
          raise TaskError(f"{where}: image must be a mapping")
  ```

  The refusal string `f"{where}: image must be a mapping"` and the `_IMAGE_KEYS` unknown-key block below it are unchanged, byte for byte. `provenance:` keeps its `or {}` (D4's last paragraph).

### Task 2 — tests

All in `bakeoff/tests/test_tasks.py`, in the budget section, using the existing `_budget_task(tmp_path, upstream, body)` helper. Every parametrization is over **YAML source text**, for the reason the existing suite_timeout_s test's docstring gives: written as a Python `"40"` the quotes would not reach the file and the case would silently stop testing anything.

- [ ] **2.1** `test_a_max_turns_that_is_not_a_positive_int_is_a_load_error` — parametrized `yaml_value` over `['"40"', "40.0", "3.7", "null", "0", "-1", "forty", "1e3", "[40]"]`; body `f"  max_turns: {yaml_value}\n"`; asserts `pytest.raises(TaskError, match="budget.max_turns")`. Docstring: names the three shapes that were **accepted** today (`"40"` → 40, `40.0` → 40, `3.7` → 3 — a manifest that stops being a faithful record) and the two that raised without naming the file (`forty`, `null`); notes that `1e3` is a `str` to PyYAML 6.0.3, not a float, because YAML 1.1's float resolver needs `1.0e+3`, so the shorthand an author reaches for to raise a bound by an order of magnitude is one of the raising cases.

- [ ] **2.2** `test_a_boolean_max_turns_is_refused_rather_than_read_as_one_turn` — parametrized over `("true", "false")`; asserts `TaskError` matching `budget.max_turns`. Docstring: `bool` IS an `int`, so `int(True)` is 1 and `int(False)` is 0; the failure mode differs from every value in 2.1 because these were **accepted**. `max_turns: true` gives every arm of that task one API call and a record that reads as a model that stopped after one turn; `max_turns: false` gives it zero. Records the 2026-09-02 measurement that the CLI does not save this: verified against claude 2.1.258 with an unreachable `ANTHROPIC_BASE_URL`, `--max-turns 0` emits `system/init` and then `api_retry` — it starts a session and calls the API — and `--max-turns -1` and `0.5` do the same; only a non-numeric argument is refused (`error: option '--max-turns <turns>' argument 'abc' is invalid. must be a number`), and `--max-turns` is not in `claude --help` at all.

- [ ] **2.3** `test_a_wall_clock_timeout_that_is_not_a_positive_int_is_a_load_error` — parametrized `yaml_value` over `['"900"', "900.0", "null", "0", "-1", "forty", "[900]"]`; body `f"  wall_clock_timeout_s: {yaml_value}\n"`; asserts `TaskError` matching `budget.wall_clock_timeout_s`. Docstring: `"900"` was accepted silently; this key is also read by `run_matrix`'s per-cell credential margin (`wall_clock_timeout_s + CELL_OVERHEAD_S`, handed to `proxy.credential_stop`), so a value nobody wrote propagates into a refusal-to-start decision about the SSO window.

- [ ] **2.4** `test_a_bad_wall_clock_names_its_own_key_not_the_suite_comparison` — **the ordering pin, and the item's measured defect.** Parametrized over two bodies:

  - `"  wall_clock_timeout_s: true\n"` (no `suite_timeout_s` declared — the click shape)
  - `"  wall_clock_timeout_s: 0\n  suite_timeout_s: 1800\n"` (both declared)

  Asserts `TaskError` whose message contains `"budget.wall_clock_timeout_s"` **and** `"must be a positive integer"`, and does **not** contain `"suite_timeout_s"` or `"exceeds"`. Docstring quotes the message this used to produce for the first body verbatim — *"budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (1) by 599s … Raise wall_clock_timeout_s, or lower suite_timeout_s to what the suite actually needs"* — and states that 600 is the dataclass default for a key the manifest never mentions, so the refusal named a key the author never wrote, did arithmetic over a boolean, and advised lowering the one number that was correct. The `not in` assertions are the load-bearing half: with the validation removed the comparison still fires and still raises a `TaskError`, so a bare `pytest.raises(TaskError)` would pass on the bug.

- [ ] **2.5** `test_every_budget_key_refuses_a_boolean` — parametrized over `[f.name for f in dataclasses.fields(TaskBudget)]`, body `f"  {name}: true\n"`. Capture with `with pytest.raises(TaskError) as exc:` and assert on `str(exc.value)`: it **contains** `f"budget.{name}"` **and** `"must be a positive integer"`, and does **not** contain `"exceeds"`.

  **`match=` on the key name alone is not enough, and this is measured.** Against a package with only `wall_clock_timeout_s` reverted to the bare `int(...)` (mutation 3.2), `pytest.raises(TaskError, match="budget.wall_clock_timeout_s")` **passes on the bug** — the comparison's own message contains that key name: `"…: budget.suite_timeout_s (600) exceeds budget.wall_clock_timeout_s (1) by 599s…"`. So the test advertised as the drift pin would have gone green for the one key this whole item exists to fix. Adding `"must be a positive integer"` flips that case to a failure; it is the same trap T2.4 names, carried into the derived test.

  Docstring: derived from the dataclass rather than listing three names, so a fourth budget key added later and wired up with a bare `int(...)` fails here instead of shipping — and it must say *why* the extra term is asserted, because a reader who trims it back to `match=` re-opens the hole silently. This is the drift pin; 2.1–2.3 are the per-key evidence. Import `dataclasses` (or `from dataclasses import fields as dataclass_fields`, matching `tasks.py`'s own import style); `test_tasks.py` does not import it today.

- [ ] **2.6** `test_max_turns_has_no_upper_bound` — `"  max_turns: 100000\n"` loads, `task.budget.max_turns == 100000`. Docstring: mirrors `test_a_suite_timeout_equal_to_the_wall_clock_loads`. A ceiling is a judgement about how much is too much, §5.4 leaves the number to §3.5's pilot, `wall_clock_timeout_s` is the outer stop whatever this says, and the CLI enforces nothing (2.2's measurement). Pinned so a later cap is a deliberate change rather than an unnoticed one.

- [ ] **2.7** `test_a_budget_section_that_is_not_a_mapping_is_refused_rather_than_defaulted` (D4) — parametrized over the raw section text `["budget: []", "budget: 0", 'budget: ""']`, written via `_write_task(..., extra_yaml=<text>)` rather than `_budget_task` (which prepends `budget:\n`). Asserts `TaskError` matching `"budget must be a mapping"`. **Keep the substring `budget_section_that_is_not_a_mapping` in the name**: mutation anchor 3.3 selects on it, and the shorter `not_a_mapping_is_refused` collides with five existing tests (F3). Docstring: `or {}` read every falsy value as an unwritten section and applied all three defaults, measured 2026-09-02; the precedent is `test_a_grading_section_that_is_not_a_mapping_is_refused_at_load`, nine lines below in the same function and already parametrized over exactly `['grading: ruff', 'grading: []', "grading: ''", 'grading: 0']` — this test is that one, for the section that predates it.

- [ ] **2.8** `test_a_null_budget_section_reads_as_absent` — `extra_yaml="budget: null"` loads with `TaskBudget()`'s three defaults. Docstring: the deliberate asymmetry against `max_turns: null`, which is refused — a null *section* is a commented-out block whose keys all have defaults that are exactly the prior behaviour; a null *key* is an author reaching for one number and writing none. Pins that 2.7 did not tighten this by accident.

- [ ] **2.9** `test_an_absent_budget_block_takes_every_default` — `load_task(_write_task(tmp_path / "set", upstream))`, asserting all three fields against `TaskBudget()`'s. Docstring: the click manifest and eight probe manifests all rely on the defaults for at least one key, so the no-`budget:` path is the one every task actually takes.

- [ ] **2.10** Update two existing docstrings that this commit falsifies:
  - `test_a_suite_timeout_that_is_not_a_positive_int_is_a_load_error` — the sentence *"the bare `int(...)` the other two budget keys use accepts a quoted or floated value silently"* becomes *"a bare `int(...)` accepts a quoted or floated value silently, which is how the other two budget keys behaved until 2026-09-02"*.
  - `test_a_boolean_suite_timeout_is_refused_rather_than_read_as_one_second` — unchanged in substance; verify it still reads correctly now that its sibling tests exist, and do not duplicate its `bool`-is-an-`int` explanation in 2.2 (2.2 explains the *turn* consequence and 2.2 owns the CLI measurement).

- [ ] **2.11** `test_an_image_section_that_is_not_a_mapping_is_refused_rather_than_defaulted` (D4, T1.7) — parametrized over `["image: []", "image: 0", 'image: ""']` via `_write_task(..., extra_yaml=<text>)`; asserts `TaskError` matching `"image must be a mapping"`. Docstring: measured 2026-09-02, each of these loaded as the default python with **empty** `apt`, `pip` and `build`, so one stray bracket throws away `build: ["pip install -e ."]` — imports resolve to site-packages, nothing the agent writes takes effect, and every arm fails identically — and `apt: ["less"]`, whose absence errors 189 unrelated tests in click's pager test. Preflight catches both, so this is hygiene rather than a live hole; the point is that `image:` must not be the one section left reading a written value as an unwritten one. **Keep the substring `image_section_that_is_not_a_mapping` in the name**: mutation anchor 3.4 selects on it, and the neighbourhood already holds `test_an_image_env_that_is_not_a_mapping_is_refused`, so a plausible shortening would both over-collect (`-k not_a_mapping_is_refused` takes 5/185) and be selected for the wrong reason — the long form takes 0/185 today. Also assert that `image: null` still loads with the defaults, in the same test or a one-line sibling, so T1.7 is pinned as a *narrowing* and not a tightening of the null path.

### Task 3 — mutation anchors

Four entries in `bakeoff/scripts/mutation_check.py`'s `MUTATIONS`, placed beside the other `src/bakeoff/tasks.py` entries. Each is `(label, file, find, replace, selector, marker)`; the `find` string must be unique in the file — verify with `grep -c`, which returns 1 for each of these on its own text (`budget_raw` vs `image_raw` vs `grading_raw`, and one `TaskBudget(...)` call site), with or without Task 1's new comments. **The fences in this task are at true source indentation — transcribe them verbatim, do not subtract two.** `mutation_check.run` splits a selector on `" -k "` and passes the remainder as one `-k` expression, so 3.1's `a or b` form is valid (`scripts/mutation_check.py:2031-2032`).

- [ ] **3.1** *"budget: parse max_turns with a bare int again, accepting `true` as one turn"*
  find:
  ```
        max_turns=_positive_int(
            budget_raw.get("max_turns", _ABSENT),
            f"{where}:budget.max_turns",
            TaskBudget.max_turns,
        ),
  ```
  replace:
  ```
        max_turns=int(budget_raw.get("max_turns", TaskBudget.max_turns)),
  ```
  selector `tests/test_tasks.py -k budget_key_refuses_a_boolean or max_turns`, marker `not integration`.
  Comment above it: reverting this makes `max_turns: true` load as 1 and `max_turns: false` as 0, and the CLI refuses neither — measured 2026-09-02, `--max-turns 0` starts a session and calls the API.

- [ ] **3.2** *"budget: parse wall_clock_timeout_s with a bare int, blaming suite_timeout_s for a boolean"*
  find:
  ```
        wall_clock_timeout_s=_positive_int(
            budget_raw.get("wall_clock_timeout_s", _ABSENT),
            f"{where}:budget.wall_clock_timeout_s",
            TaskBudget.wall_clock_timeout_s,
        ),
  ```
  replace:
  ```
        wall_clock_timeout_s=int(
            budget_raw.get("wall_clock_timeout_s", TaskBudget.wall_clock_timeout_s)
        ),
  ```
  selector `tests/test_tasks.py -k names_its_own_key`, marker `not integration`.
  Comment: this is the 2026-09-02 defect verbatim — `wall_clock_timeout_s: true` becomes 1 and the `>` refusal below reports it as a `suite_timeout_s` the manifest never declared.

- [ ] **3.3** *"budget: read a written-but-empty budget section as an unwritten one"*
  find:
  ```
    budget_raw = data.get("budget")
    if budget_raw is None:
        budget_raw = {}
  ```
  replace:
  ```
    budget_raw = data.get("budget") or {}
  ```
  selector `tests/test_tasks.py -k budget_section_that_is_not_a_mapping`, marker `not integration`.
  Comment: `budget: []` then applies all three defaults to a manifest that visibly asked for something else. The selector is deliberately the long form: `-k not_a_mapping_is_refused` is a substring match that also collects the four `grading:` cases and `test_an_image_env_that_is_not_a_mapping_is_refused` (measured, 5/185), which still catches the mutation but re-runs four irrelevant tests and fails 3.5's stated pass condition.

- [ ] **3.4** *"image: read a written-but-empty image section as an unwritten one"*
  find:
  ```
    image_raw = data.get("image")
    if image_raw is None:
        image_raw = {}
  ```
  replace:
  ```
    image_raw = data.get("image") or {}
  ```
  selector `tests/test_tasks.py -k image_section_that_is_not_a_mapping`, marker `not integration`.
  Comment: `image: []` then builds the default base with no `apt`, no `pip` and no `build` — a non-editable install every arm fails identically on, and click's missing `less`.

- [ ] **3.5** Confirm each selector actually matches the intended test(s) and nothing else: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -k '<selector>' --collect-only -q -m 'not integration'`. If a selector over-collects, lengthen it until it does not; do not proceed with a selector that pulls in unrelated tests.

### Task 4 — `docs/BUILDING-A-TASK-SET.md`

- [ ] **4.1** In the manifest skeleton (grep for `wall_clock_timeout_s: 900` in that file), replace:

  ```
  budget:
    max_turns: 40
    wall_clock_timeout_s: 900
    # suite_timeout_s: 600   # the timeout on every command preflight, the
    #                        # oracle and the grader run in the container.
    #                        # Must not exceed wall_clock_timeout_s.
  ```

  with:

  ```
  budget:
    # All three are POSITIVE INTEGERS, refused at load by name otherwise:
    # a quoted "40", a floated 40.0, true/false, null, 0 and negatives are
    # each a TaskError naming this file and the key. Unquoted and unfloated,
    # or the manifest stops being a record of what the arms were asked to do.
    max_turns: 40
    wall_clock_timeout_s: 900
    # suite_timeout_s: 600   # the timeout on every command preflight, the
    #                        # oracle and the grader run in the container.
    #                        # Must not exceed wall_clock_timeout_s.
  ```

- [ ] **4.2** No other line in that file changes. The `suite green but slow` table row already states the `≤ wall_clock_timeout_s` rule correctly.

### Task 5 — the click manifest's `budget:` comments

- [ ] **5.1** In `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, under `budget:`, after the existing two-line §5.4 comment and before `max_turns: 40`, insert:

  ```
    # All three keys are positive integers and are refused at load by name
    # otherwise -- a quoted "40", a floated 40.0, `true`, `false`, `null`, 0
    # or a negative each raise a TaskError naming this file and the key.
    # QUOTE nothing here: unlike image.python, which must be quoted because
    # YAML reads an unquoted 3.10 as the float 3.1, a quoted number in this
    # block is refused rather than coerced.
  ```

- [ ] **5.2** In the same block, replace these six comment lines verbatim (the `suite_timeout_s` paragraph, the last one before the blank line and `provenance:`):

  ```
    # A positive integer, and it may not exceed wall_clock_timeout_s -- the
    # agent re-runs this suite inside that budget, so a longer one describes a
    # task no arm could verify even once. Raising it multiplies the gate's wall
    # cost: preflight runs the suite five times (four when tests.p2p is
    # declared) plus once per declared grading.* argv, all before the proxy
    # starts and inside the same one-hour SSO session the matrix needs.
  ```

  with these nine:

  ```
    # A positive integer, and it may not exceed wall_clock_timeout_s -- the
    # agent re-runs this suite inside that budget, so a longer one describes a
    # task no arm could verify even once. That comparison runs only after all
    # three keys above have been validated, so a bad wall_clock_timeout_s is
    # refused as itself rather than surfacing here as this key exceeding it.
    # Raising it multiplies the gate's wall cost: preflight runs the suite five
    # times (four when tests.p2p is declared) plus once per declared grading.*
    # argv, all before the proxy starts and inside the same one-hour SSO
    # session the matrix needs.
  ```

  Whole lines, with their `  # ` prefixes, because the sentence being extended does not end where a shorter quotation would suggest and the new text's "That comparison" has to sit adjacent to the clause it refers to.

- [ ] **5.3** **Comments only.** `max_turns: 40` and `wall_clock_timeout_s: 900` keep their values. This does **not** move `start_sha` (the manifest is not an input to the start state) and does **not** need a `task_version` bump (nothing about what any arm is asked to do changes). It **does** move `manifest_digest`, which is a term in `preflight._cache_key` — so the task's cached verdict is invalidated and the gate re-runs once (V4), and `GradeRecord.graded_against_manifest_digest` on future grades differs from the value on records already written. That is the cache key and the provenance field each doing their job; the log is append-only and the disagreement is the finding, not a defect.

### Task 6 — `bakeoff/taskset/HARVESTING.md`, `TASKS.md`, `tasks/todo.md`

- [ ] **6.1** `HARVESTING.md`: in the paragraph beginning "`budget.suite_timeout_s` may not exceed `budget.wall_clock_timeout_s`", add one sentence at its end:

  ```
  All three `budget:` keys are positive integers, refused at load with the
  file and the key named — a quoted, floated, boolean, null, zero or negative
  value is a load error, not a coercion — and that check runs before this
  comparison, so a bad `wall_clock_timeout_s` is reported as itself.
  ```

- [ ] **6.2** `TASKS.md`: **delete** the entry outright ("**`max_turns` and `wall_clock_timeout_s` still parse with a bare `int(...)`.**", six lines at HEAD 8232032). Not struck through, not annotated — deleted. That is the convention item 3 used in the tree at the time of review 1 (`git diff HEAD -- TASKS.md`: the closed entry removed, the residual deferral added as a new `- [ ]` entry). Confirm against whatever items 1–5 actually committed before doing it; if they diverge, follow them and say so in the commit message.

- [ ] **6.3** `TASKS.md`: add the D7 deferral as a new entry in the same section:

  ```
  - [ ] **An unknown key under `budget:` loads silently with every default.**
    `budget:\n  max_turn: 5` and `budget:\n  suite_timeout_ms: 5` both load as
    40/900/600 (measured 2026-09-02) — the same invisible typo `_IMAGE_KEYS`
    exists to catch for `image:`, where a misspelled `pyhton:` builds the
    default base and every read-back agrees with the field it was compared to.
    The fix is `_BUDGET_KEYS = tuple(f.name for f in dataclass_fields(
    TaskBudget))` beside `_IMAGE_KEYS` plus a refusal naming the unknown key
    and the accepted set. Its own change: unlike the value validation (round 2
    item 6), this one CAN refuse a manifest an author has written locally.
    (Round 2 item 6, 2026-09-02.)
  ```

  The trailing `(Round 2 item <N>, <date>.)` is the attribution item 3's residual deferral carries. The date is **2026-09-02** — every measurement in this plan was taken that day, whatever the 2026-09-03 in the plan's filename says.

- [ ] **6.4** `tasks/todo.md`: add a review-log section for this item, matching the format items 1–5 used — what was measured, what changed, what was deliberately not changed (D5's absent ceiling, D6's `0x28`, D7's unknown keys, D4's `provenance:`). Include one line recording that **claude 2.1.220's `--max-turns` handling is unmeasured**: the CLI evidence in this commit is from 2.1.258 on the host, and 2.1.220 is what the eval image pins. It can be checked for free the next time that image is built, and a stricter check there would only move the failure earlier — it cannot make the load-time refusal wrong.

---

## Verification

Run from `bakeoff/` with `.venv/bin/python`. Record the actual output of each; a step is not done until its output has been read.

- [ ] **V1 — baseline, before Task 1.** `.venv/bin/python -m pytest tests/ -q`. Record whatever counts it prints; V3 compares against that number and against nothing else. Do not predict it — which round-2 items have landed when this runs is not knowable from here (the round opened at `1539 passed, 62 deselected`, and `test_tasks.py` alone was `185 passed` at review 1).

- [ ] **V2 — the compatibility claim, re-measured at implementation time (M4).** Before Task 1, load every manifest on disk and print its budget; after Task 1, do it again and confirm the same values and no new refusal:

  ```
  .venv/bin/python -c "
  from pathlib import Path
  from bakeoff.tasks import load_task
  import os, glob
  roots = ['taskset', os.path.expanduser('~/.cache/bakeoff-probe/taskset')]
  for root in roots:
      subdirs = sorted(p for p in Path(root).iterdir() if p.is_dir())
      manifests = [d for d in subdirs if (d / 'task.yaml').exists()]
      print(f'{root}: {len(subdirs)} directories, {len(manifests)} with a manifest')
      for d in manifests:
          task = load_task(d)
          print(' ', d.name, task.budget, 'image.build=', task.image.build)
  "
  ```

  Every line must be identical before and after Task 1 — including `image.build`, which T1.7 is the reason to print. The two counts per root must also be read. Measured 2026-09-02 by running this exact script: `taskset: 1 directories, 1 with a manifest` and `bakeoff-probe/taskset: 9 directories, 8 with a manifest` — **nine manifests in total**, and the one probe directory without a manifest is `boltons-90-statsutils-mode`, which carries a `reference.diff` only (a glob on `*/task.yaml` skips it in silence, which is why this walks directories instead). A directory that has since grown a manifest is a manifest M4 never saw.

  If any manifest is refused after Task 1, **stop**: it declares a shape M4 did not see, and it needs a decision (fix the manifest, or narrow the refusal) before the commit — not a quiet edit to the manifest.

  This is the corpus-wide check, and it is the one the reviewer's Q2 ruling asks to be strengthened rather than V4 widened: it is offline, costs nothing, and covers every manifest, while a probe-task gate would build an image to measure that task's own health rather than anything about this change.

- [ ] **V3 — unit suite.** `.venv/bin/python -m pytest tests/ -q`, then `.venv/bin/python -m pytest tests/test_tasks.py -v -k budget`. Expect V1's count plus the new cases (2.1's 9 + 2.2's 2 + 2.3's 7 + 2.4's 2 + 2.5's 3 + 2.6's 1 + 2.7's 3 + 2.8's 1 + 2.9's 1 + 2.11's 3 = **32** new test cases; 2.10 adds none). No failures, no errors. Every existing test must still pass unmodified apart from 2.10's two docstrings — measured in review 1 against a patched package, `tests/test_tasks.py` was **185 passed** with Task 1.1 and 1.2 applied.

- [ ] **V4 — the gate, on the one task whose manifest T5 edits.** T5 moves `manifest_digest`, so the cached preflight verdict for click is stale by construction. Needs a Docker daemon; no credentials, no spend:

  ```
  .venv/bin/python scripts/run_matrix.py --preflight-only
  ```

  Expect a GO for `click-3360-write-usage-empty-args`, with the gate re-running rather than reporting a cache hit. **Click only — do not gate a probe task.** T5 edits exactly one manifest, so click is the only task whose `manifest_digest` moves and the only one whose cached verdict this commit invalidates. A probe task's manifest is untouched and its digest unmoved, so the run would either hit cache or need `--force-preflight`, and what it would then measure is that task's own health at the price of an image build plus a repo-mirror fetch. The corpus-wide claim is V2's, and V2 is offline.

- [ ] **V5 — the logger gate.** `.venv/bin/python scripts/verify_logger.py`. Offline, no credentials, no spend. Expect `GATE PASSED`; a `GATE INCOMPLETE` means no Docker daemon, not a regression.

- [ ] **V6 — mutation check, run SOLO** (nothing else may touch the tree): `.venv/bin/python scripts/mutation_check.py`. Expect the previous total plus **4** — Task 3 specifies 3.1, 3.2, 3.3 and 3.4 — all passing. A stale-anchor failure means Task 1's text was transcribed differently from Task 3's `find` strings; note that 3.4's `find` is transcribed from **T1.7**, not from T1.1/T1.2. Fix the anchor, never the source.

- [ ] **V7 — the defect itself, end to end.** Reproduce M2.1 on a scratch copy and confirm the message changed. Under `$HOME` (not `/var/folders`, per `CLAUDE.md`'s Docker-mount note — irrelevant here but keeps the habit):

  ```
  .venv/bin/python -c "
  import pathlib, shutil, os
  from bakeoff.tasks import load_task, TaskError
  src = pathlib.Path('taskset/click-3360-write-usage-empty-args')
  dst = pathlib.Path(os.path.expanduser('~/.cache/bakeoff-round2-6/v7/click'))
  shutil.rmtree(dst.parent, ignore_errors=True); dst.parent.mkdir(parents=True)
  shutil.copytree(src, dst)
  t = dst / 'task.yaml'; y = t.read_text()
  i, j = y.index('\nbudget:'), y.index('\nprovenance:')
  for body in ['  wall_clock_timeout_s: true', '  max_turns: false',
               '  max_turns: \"40\"', '  wall_clock_timeout_s: 0.5']:
      t.write_text(y[:i] + '\nbudget:\n' + body + '\n' + y[j:])
      try:
          print(repr(body), '-> OK', load_task(dst).budget)
      except TaskError as e:
          print(repr(body), '-> TaskError:', e)
  "
  ```

  Every line must be a `TaskError` naming `budget.<the key written>`. The first line must **not** mention `suite_timeout_s`.

---

## What this does NOT do

- **Does not add an upper bound to `max_turns`** (D5). `max_turns: 100000` loads, and T2.6 pins that.
- **Does not refuse `0x28`** or any other integer spelling YAML resolves to a positive `int` (D6).
- **Does not guard unknown keys under `budget:`** (D7). `budget:\n  max_turn: 5` and `budget:\n  suite_timeout_ms: 5` still load with every default; T6.3 files it. The `image:` unknown-key guard (`_IMAGE_KEYS`) already exists and is untouched.
- **Does not change `provenance:`** (D4, last paragraph). `provenance=dict(data.get("provenance") or {})` keeps its `or {}`: it is a free dict with no defaults to silently apply, so a falsy section discards nothing and there is nothing for a refusal to protect.
- **Does not change `image: null`, an absent `image:` block, or the `_IMAGE_KEYS` guard** (T1.7). Only `image: []`, `: 0` and `: ""` change, from "all defaults" to the existing `image must be a mapping` refusal.
- **Does not touch `_positive_int`'s body.** It already refuses `bool` before `int` and `<= 0`; only its docstring changes.
- **Does not move the `suite_timeout_s > wall_clock_timeout_s` comparison, its message, or its strictness.** Equality still loads (`test_a_suite_timeout_equal_to_the_wall_clock_loads` is untouched).
- **Does not change `suite_timeout_s`'s behaviour in any way.** Every existing budget test passes unmodified except for two docstring edits (T2.10).
- **Does not change `budget: null` or an absent `budget:` block** — both still take all three defaults (T2.8, T2.9).
- **Does not re-validate anywhere downstream.** `run_matrix`, `claude_runner`, `proxy.credential_stop` and the grader keep reading `task.budget.*` as given; `load_task` is the single gate, which is the existing shape and not something this item is renegotiating.
- **Does not touch `_require`**, and so does not fix its sibling hole: `_require(data, "task_version", int, where)` accepts `task_version: true`, because `isinstance(True, int)`. Noticed while measuring, out of subject; it belongs with D7's `_BUDGET_KEYS` work or its own entry.
- **Does not move any version constant.** Nothing a verdict asserts changes: `preflight` makes the same checks and reaches the same GO/NO-GO (`PREFLIGHT_VERSION`), the ladder runs the same commands against the same oracle (`GRADER_VERSION`, `ORACLE_VERSION`), and no field is added to or changes meaning in either record (`SCHEMA_VERSION`, `GRADE_SCHEMA_VERSION`). The only thing that changes is which manifests **fail to load at all** — before any of those versions is ever stamped on anything.
- **Does not re-measure `--max-turns` inside the eval image** (D5's caveat, and the reviewer's Q3 ruling). The host measurement at claude 2.1.258 is what the comments cite, scoped to that version in the shipped docstring (T1.4) and recorded as unmeasured-at-2.1.220 in the review log (T6.4).
- **Does not change where a refusal surfaces.** After round-2 item 4, `load_task_set` downgrades a refusal on an uncommitted, unselected sibling to a warning; this plan neither relies on that nor alters it. `load_task` refuses identically whether or not item 4 has landed.

---

## Sentences that belong in `CLAUDE.md` (do NOT add them on this branch)

For whoever merges this branch and updates the root `CLAUDE.md`, under "Config gotchas that have already cost a debugging session" or beside the manifest invariants:

> **A budget number is validated before it is compared, and the order is the whole fix.** All three `budget:` keys go through `tasks._positive_int`, which refuses `bool` *before* it refuses non-`int` (`int(True)` is 1). Until 2026-09-02 `max_turns` and `wall_clock_timeout_s` used a bare `int(...)`, so `wall_clock_timeout_s: true` loaded as **1 second** and the `suite_timeout_s > wall_clock_timeout_s` refusal below then blamed a `suite_timeout_s` the manifest never declared — 600 is the dataclass default — telling the author to lower the one number that was correct. `"900"`, `900.0` and `3.7` were accepted silently and the manifest stopped being a record of what the arms were asked to do. There is deliberately **no upper bound** on `max_turns`: `wall_clock_timeout_s` is the outer stop, §5.4 leaves the number to §3.5's pilot, and the CLI enforces nothing — verified against claude 2.1.258, `--max-turns` is absent from `--help` and `--max-turns 0` starts a session and calls the API rather than refusing.

---

## Review 1 → changes

`.superpowers/broaden/round2/plan-6-review-1.md`, 2026-09-02: **REVISE**, 11 findings (2 blocking, 9 low) plus rulings on the plan's four open questions. All 11 addressed, all four rulings adopted, none disputed. The review re-measured every M1/M3 row, D6, D7 and M2.3 independently and reproduced them, and additionally measured the "intended" column against a patched scratch package — evidence a v1 reading could not produce. That evidence is folded in where it strengthens a claim.

| # | finding | change |
|---|---|---|
| **F1** | **BLOCKING.** T2.5's `raises(match="budget.wall_clock_timeout_s")` **passes on the bug**: the comparison's own message contains that key name, so the test advertised as the drift pin would have gone green for the one key this item exists to fix. Measured against the mutation-3.2 package. | T2.5 rewritten: capture with `pytest.raises(TaskError) as exc` and assert `str(exc.value)` contains `f"budget.{name}"` **and** `"must be a positive integer"` and does **not** contain `"exceeds"`. The docstring must say why the extra term is there, so a later reader who trims back to `match=` sees what they are re-opening. Same trap T2.4 already names, now carried into the derived test. |
| **F2** | **BLOCKING (scope hygiene).** `image_raw = data.get("image") or {}` is the identical defect and was neither fixed nor mentioned; `image: []` silently discards `build: ["pip install -e ."]` and `apt: ["less"]`. After v1 the file would have had `grading:` and `budget:` on `is None` and `image:` on `or {}` with nothing saying why. | Took the reviewer's option (a): D4 renamed and extended with the measured `image:` table and the argument for including it; new **T1.7** applies the same one-statement fix with its own comment; new **T2.11** is T2.7's sibling (three falsy shapes refused, `image: null` still defaulting); new mutation anchor **3.4** (old 3.4 → **3.5**); `image.build` added to V2's printout; two new "does NOT" bullets. `provenance:` is explicitly ruled out in D4's last paragraph and in "What this does NOT do" — a free dict with no defaults to discard. |
| **F3** | Mutation selector `-k not_a_mapping_is_refused` over-collects five tests (four `grading:` cases plus `test_an_image_env_that_is_not_a_mapping_is_refused`), so T3.4's stated pass condition ("and nothing else") could not be met. | 3.3's selector narrowed to `-k budget_section_that_is_not_a_mapping`, with the measurement recorded beside it; 3.4 (image) uses the matching long form; **T2.7 now requires that substring in the test name**; renumbered 3.5 says what to do when a selector over-collects instead of leaving it open. |
| **F4** | M3 and T2.7 said the `grading:` precedent is "eleven lines **above**" the budget block. It is below — and T1.1's own comment already said "below", so the plan contradicted itself and the wrong half was the one destined for a docstring. | Both corrected to "**nine lines below** in the same function". While there, both now cite the precedent as an existing *test* (`test_a_grading_section_that_is_not_a_mapping_is_refused_at_load`, parametrized over exactly `['grading: ruff', 'grading: []', "grading: ''", 'grading: 0']`), not merely a comment — which also strengthens D4/Q1's first reason. |
| **F5** | M4's "none declares `suite_timeout_s`" is false — `pytest-10210-approx-nested-container` declares `suite_timeout_s: 240`. And the probe corpus has grown a tenth directory, `boltons-90-statsutils-mode`, with a `reference.diff` and no `task.yaml`, which V2's glob skipped in silence. | M4 corrected (one manifest declares 240, itself a plain positive int; conclusion unmoved) and now cites the review's independent before/after check. V2 rewritten to walk directories rather than glob manifests, printing `<n> directories, <m> with a manifest` per root, with review 1's counts (1/1 and 10/9) written out as the expected reading. |
| **F6** | There is no `credentials` module. M5 and T2.3's specified docstring both named one; `credential_stop` is `proxy.py:379` and the margin arithmetic is `run_matrix.py:593,638`. | Corrected in M5, in T2.3's docstring, and in the "does not re-validate downstream" bullet. |
| **F7** | The indentation constraint claimed Task 3's fences are "not nested" — they are; what differs is that their *contents* carry true source indentation while Task 1's carry +2. An implementer applying one rule uniformly gets Task 3 wrong by two spaces. The adjoining claim that Task 1's new comments are what make the anchors unique is also false (measured: all unique without them). | Constraint restated per-task ("Task 1: subtract two. Task 3: transcribe verbatim"), with a true-indentation example for each. The uniqueness clause is replaced by what actually makes each anchor unique (`budget_raw` vs `image_raw` vs `grading_raw`, one `TaskBudget(...)` call site), and the `mutation_check.run` selector-splitting fact moved into Task 3's preamble. |
| **F8** | T5.2 told the implementer to "extend" a sentence that does not end where the quotation ends, leaving the splice point — and the referent of "that bound" — to their judgement, which the plan's own contract forbids. | T5.2 rewritten as a whole-lines before/after block (six comment lines → nine), the way T1.1 and T4.1 do it, with the new clause placed adjacent to the sentence it qualifies. |
| **F9** | T6.2 deferred the striking convention that item 3's in-flight tree already answers, and T6.3's new entry lacked the attribution that convention adds. | T6.2 now says **delete the entry outright** (item 3's shape), with an instruction to confirm against items 1–5 and to say so in the commit message if they diverge. T6.3's entry ends `(Round 2 item 6, 2026-09-02.)`, and the plan now states that 2026-09-02 is the measurement date whatever the filename says. |
| **F10** | "Items 1–5 land first" is already false (HEAD 8232032, item 3 only in the tree), and the item-4 interaction was asserted without being characterised. | The constraint now records what was measured, states item 4's edits and mutation anchors as **disjoint** (verified), says item 6 applies cleanly in either order, and names the one real interaction — item 4's `load_task_set` warning path changes where a new refusal *surfaces*, never whether `load_task` raises. V1 drops its prediction about the baseline count. |
| **F11** | T1.4's docstring stated "there is no downstream limit to mirror" flatly, with the version qualifier only in the next clause, while the eval image's 2.1.220 was never measured. | The shipped docstring now reads "no downstream limit to mirror AT THE VERSION MEASURED", names 2.1.220 as unmeasured, and says a stricter check there would only move the failure earlier. |

**Rulings adopted.**

- **Q1 — D4 IN, conditional on F2.** Both taken: `budget:` and `image:` get the fix, `provenance:` is excluded in writing with its reason, and each fix keeps its own test and anchor so either stays droppable whole.
- **Q2 — V4 is click only.** The conditional probe-task leg is deleted; V2 is strengthened instead (directory counts, `image.build`), since it is the offline check that actually covers the corpus.
- **Q3 — do not re-measure in the image.** Kept the host measurement, tightened the claim (F11), and added the free-when-next-built note to T6.4.
- **Q4 — keep the quoted-text discipline, drop the ordering premise.** Done in F10; the review's byte-for-byte confirmation of every quoted "before" block is recorded in the same constraint.

**Not disputed:** nothing. Two of the review's own observations are folded in as strengthened claims rather than findings — the patched-package result that `tests/test_tasks.py` is `185 passed` with Task 1 applied (now V3's stated expectation), and the confirmation that all three original anchors are unique and each is caught by its selected test.

### Review 2

`.superpowers/broaden/round2/plan-6-review-1.md`, `# Review 2 — the revised plan`, 2026-09-02: **APPROVE**. All 11 review-1 findings addressed (9 fully, F7 and F10 with a residual that became N1 and N3), both blocking findings closed and **verified by measurement in both directions** against a fully patched scratch package carrying Tasks 1.1, 1.2 and 1.7. Six open findings, all LOW; all six folded in below. Four of the six were introduced by the v2 edits themselves.

Two results worth keeping, because they are the evidence the blocking fixes actually work:

**T2.5's revised assertion flips exactly where it must.** Re-measured per `dataclasses.fields(TaskBudget)`; the old `match=`-only form was GREEN in every cell of this table, which is why F1 was blocking:

```
                              max_turns      wall_clock_timeout_s   suite_timeout_s
  on the fix (1.1+1.2+1.7)    GREEN          GREEN                  GREEN
  on mutation 3.2             GREEN          RED                    GREEN
  on unpatched HEAD           RED            RED                    GREEN
```

**Task 1 in full, T1.7 included, is clean against the suite.** `PYTHONPATH=<fully patched> pytest tests/ -q` → **1552 passed, 2 failed, 62 deselected**; both failures are artifacts of importing the package from a non-git scratch directory (`test_version_block_is_populated_for_every_component` and `test_every_line_names_the_inputs_it_was_derived_from` assert a non-empty `grader_commit`/`harness_commit`) and both pass against the unpatched tree in the same venv. Nothing in the suite reacts to T1.7. V2 runs verbatim and is identical before and after on all nine manifests, `budget` and `image.build` alike.

| # | finding | change |
|---|---|---|
| **N1** | Residual of F7. The indentation rule covered Tasks 1 and 3 only; Tasks 4, 5 and 6 also carry +2 fences, and **T5.2's is matched against a real file** — measured, its "before" block occurs 1 time in `task.yaml` after subtracting two and **0 times verbatim**, so a +2 transcription silently matches nothing. | The constraint is now stated once for the whole plan — every fence is +2 **except Task 3's** — with four measured examples, including the two that made the old rule insufficient (`task.yaml` shown at 4 / real 2; `BUILDING-A-TASK-SET.md` shown at 2 / real 0). |
| **N2** | Introduced by F5's fix. V2 quoted `10 directories, 9 with a manifest` for the probe root; the 9 was the **total** manifest count including click. A correct run prints `9 / 8` and, under V2's own instruction, reads as a lost directory. | Corrected to the measured output of V2's exact script: `taskset: 1 directories, 1 with a manifest` and `bakeoff-probe/taskset: 9 directories, 8 with a manifest` — nine manifests in total, with `boltons-90-statsutils-mode` named as the manifest-less directory. |
| **N3** | Residual of F10. The section opened by retracting "items 1–5 land first" and closed, eight bullets later, by asserting it. V1 was de-predicted; the baseline bullet was not. | The prediction is gone: "record the number it prints. **Do not predict it** (V1)", with the measured 2026-09-02 figure (**1554**, not the round's opening 1539) given as data rather than as an expectation. |
| **N4** | Introduced by F2's fix. V6 still said "plus **3**" after Task 3 grew to four entries. | V6 says **plus 4**, names the four, and adds that 3.4's `find` is transcribed from **T1.7** rather than from T1.1/T1.2 — the stale-anchor sentence previously pointed only at "Task 1's text". |
| **N5** | Pre-existing, missed in review 1: D5 cited "T3.6" for the no-ceiling pin (it is **T2.6**) and D7 cited "T3.5" for the dataclass-derived test (it is **T2.5**). F2's renumbering made T3.5 a real step, so the wrong reference had started resolving to something that exists and is not a test. | Both corrected. |
| **N6** | T2.7 carries an explicit "keep this substring in the name, anchor 3.3 selects on it"; T2.11 is in the identical position with respect to anchor 3.4 and carried no such instruction — and `test_an_image_env_that_is_not_a_mapping_is_refused` sits in the same neighbourhood, so a shortened name would both over-collect and be selected for the wrong reason. | The same sentence added to T2.11, naming anchor 3.4, with the measured collection counts (long form 0/185, `-k not_a_mapping_is_refused` 5/185). |

**One strengthening folded in beyond the six.** Review 2 measured that `provenance: []`, `: 0` and `: null` all produce `{}` today, but that `dict(0)` raises a bare **`TypeError`** — so switching `provenance:` to `is None` would *introduce* an unhandled non-`TaskError` path for `provenance: 0`. D4's exclusion paragraph now says so; the exclusion is better-founded than v2 claimed.

**Carry into `tasks/todo.md` (T6.4):** record that N1–N6 were folded in at plan level before implementation, and that four of the six were artifacts of the v2 revision rather than defects in the design.
