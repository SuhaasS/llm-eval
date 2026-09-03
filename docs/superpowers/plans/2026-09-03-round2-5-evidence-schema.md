# Round 2, item 5: one evidence schema for `preflight` — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `PreflightResult.evidence` carries the SAME key set on every path through `preflight()`. A key the gate did not look at is `null`; a key that is absent means only one thing — the verdict was written by a gate that did not have the key. Enforced by one module-level constant that both the early return and the full path fill from, asserted in the gate itself, and pinned against the source by a static extraction so the constant cannot go stale the next time an item adds a key.

**Architecture:** One tuple, `preflight.EVIDENCE_KEYS`, listing every key `preflight()` can write, in the order the gate writes them. One helper, `_evidence_seed()`, returning `dict.fromkeys(EVIDENCE_KEYS)`. `preflight()` starts from it instead of `{}`; `PreflightResult.evidence`'s `default_factory` is it; `PreflightResult.__post_init__` refuses an evidence dict whose key set is not exactly `EVIDENCE_KEYS`, naming both directions of the difference. One new key, `early_return`, carries the reason the pre-container return fired and is `null` on every path that reaches a container. One test derives the key set from the source by AST and asserts the tuple equals it.

**Tech Stack:** Python 3.12 (the harness venv), pytest. No Docker, no network, no credentials — every test in this plan is a unit test against `_ScriptedContainer`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md`. Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **5**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind. Repo invariants: `CLAUDE.md`, "Absence is recorded, never implied" and "A null says which kind of null it is" — this item is those two rules applied to the one dict in the repo that does not follow them.

**Review state: APPROVED.** Review 1 (`.superpowers/broaden/round2/plan-5-review-1.md`) returned REVISE with 9 findings, 2 blocking; review 2 (same file, from `## Finding-by-finding`) APPROVED with 3 new LOW items. All 12 are addressed in place — see **Review 1 → changes** and its **Review 2** subsection at the end. Nothing is disputed. Ready to implement **after items 1–4 land and after Task 0 is actually run** rather than assumed.

---

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode. A comment that says what a line does rather than what breaks without it does not fit here. Claims about external behaviour carry what they were verified against.
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section.
- **THIS PLAN IS WRITTEN AGAINST HEAD 8232032 AND WILL NOT BE IMPLEMENTED ON IT.** `CONTEXT.md:34`: implementation is strictly sequential, one tree, and this is item 5. Items 1–4 land first and three of them edit `preflight.py`. Therefore:
  - **`EVIDENCE_KEYS` is DERIVED from the tree at implementation time, never transcribed from D2.** D2's tuple is the reading at HEAD 8232032 and is *known* to be short by two: item 1 adds `ambiguous_file_filters` (`…round2-1-node-file-selection.md:382`, written at its `:399`) and item 2 adds `submodules_populated_after_suite` (`…round2-2-unneeded-submodule.md:462-463`, written at its `:951`), and both are seeded on every path. A transcribed tuple leaves both unlisted, `__post_init__` raises on **every path of every task**, and the gate tracebacks instead of returning a NO-GO. Run the derivation in **Verification step V1** before Task 1 and use its output.
  - **Every `preflight.py:NNN` and `test_preflight.py:NNN` in this plan is "at HEAD 8232032; re-locate".** Address every instruction by symbol name or by the exact source text to grep for. Task 2's delete list names statements by text for this reason.
  - **`PREFLIGHT_VERSION` bumps by ONE from whatever value is on disk when Task 4 starts.** It reads `"13"` at HEAD 8232032; items 1, 2 and 3 each bump it, so expect `"16" -> "17"`. Read the constant and add one; **do not hard-code a literal**, and do not transcribe D7's `13 -> 14` header.
  - **The key count is computed, never asserted as a literal.** 40 at HEAD 8232032 (39 written today + `early_return`), plus whatever items 1–4 added — expected 42, but re-derive.
- **`SCHEMA_VERSION` does NOT move.** No `RunRecord` field is added or changes meaning.
- **`GRADE_SCHEMA_VERSION` does NOT move.** No `GradeRecord` field is added or changes meaning. `GradeRecord.graded_under_preflight_version` copies `PreflightResult.preflight_version` and its *meaning* is unchanged — it still names the gate.
- **`GRADER_VERSION` does NOT move.** The grader's ladder, oracle and env are untouched; it reads no evidence key (M5).
- **`ORACLE_VERSION` does NOT move.**
- **No verdict moves.** Every `problems.append` and every `problem_codes.append` in `preflight()` is untouched. This commit changes what a stored verdict *says*, never what it *decides*. T3.7 pins it.
- **The gated argv and the graded argv stay byte-identical.** Nothing in this plan touches `_Runner`, `tests.runner`, or any adapter.
- **Commit hygiene:** one commit (plan + code + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Do not run `scripts/mutation_check.py`** concurrently with anything else; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — record the number it prints; items 1–4 land first and it will not be the round's opening `1539 passed, 62 deselected`.

---

## The measured defect

**Source:** `TASKS.md`, "**Two evidence families disagree about what an unreachable check writes.**" (`TASKS.md:1221-1234` at HEAD 8232032). The entry's own line citation, `preflight.py:507-523`, is **stale** — it points inside `_gitlink_paths`' docstring today; the pre-container seed block is at `:817-896`. Correct it when the entry is struck.

**M1 — the two families.** `preflight()` writes `evidence` from 60 statements. Nineteen keys are seeded before the guard (the "written on every path" family, introduced by broadenings 3, 5, 6 and 7's review). Twenty keys are written only where they are measured (the "written where measured" family: everything older than that seed, plus everything added since without joining it). Both families encode "the gate did not look"; only the first says so.

**M2 — the item under-counts the defect by a factor of six, and the inventory is the finding.** `TASKS.md` names three keys (`stripped_paths`, `stripped_paths_present`, `suite_timeout_s`). The complete inventory, taken by AST off every `evidence[...] =` statement in `preflight()`, is **20** inconsistent keys, not 3. The table is D1; review 1 re-derived it independently and confirmed no missing, extra or duplicate key.

**M3 — the reader is a human, and there is no version-stamped fallback for them.** `run_matrix.resolve_tasks` writes the whole verdict — evidence included — to `<cache>/preflight/<task_id>.json` (`result.to_dict()`). That file is what an author diffs when a task changes shape. The blob carries `preflight_version`, so a *careful* reader can in principle date the gate; but dating a gate is a lookup in this file's version comments, and the absence they would be resolving renders identically to "the gate looked and found nothing" for the other family's keys sitting beside it in the same file. Two absences that render identically are the same defect one layer down — the repo's own rule, and the reason the 19-key family exists at all.

**M4 — the shape has already been got wrong twice inside this file, both times caught late.** `PREFLIGHT_VERSION` 11's comment records `duplicate_full_names`, `scope_files_outside` and `f2p_before_not_run` moving from `[]` to `None` because `[]` claimed a measurement never made; the `bare_runner_skipped` comment records that key being "left out of this seed once". Each was a separate round, each found by review rather than by a test, because nothing asserts the two families agree. That is what this item closes: not the three keys — the *absence of an enforcement*.

**M5 — nothing machine-reads `evidence`, and that is why the drift survived.** `grep -rn "evidence\[" bakeoff/src bakeoff/scripts` outside `preflight.py` returns nothing. `scripts/grade.py::preflight_cached` consults both caches on the *key* alone (`_cache_file(...).get(task_id, {}).get("key") == key`) and `record_preflight_pass` stores `{"key": ...}` and nothing else. No code path breaks on a missing key; the cost is paid entirely by the human reading `<cache>/preflight/<task_id>.json`, which is the reader an append-only artifact exists for. Verified independently in review 1.

---

## Design decisions, settled

### D1. The inventory — which family each key belongs to today

Read off `bakeoff/src/bakeoff/preflight.py` **at HEAD 8232032** (line numbers are that reading; re-locate by symbol). "Seeded" = written before the early return, so present on every path. "Where measured" = absent from any path that does not reach the write. **This table is the finding, not the transcription target** — V1 supersedes it for the actual tuple.

**Family A — seeded before the guard (19 keys, already correct; no change beyond moving into `EVIDENCE_KEYS`):**

| key | seeded at | seed value today |
|---|---|---|
| `framework` | 799 | the adapter name (a real value, knowable with no container) |
| `image_env_declared` | 822 | the manifest's dict (a real value) |
| `image_env_observed` | 823 | `None` |
| `image_env_mismatch` | 824 | `None` |
| `hypothesis_importable` | 825 | `None` |
| `hypothesis_imported_by_suite` | 826 | `None` |
| `python_declared` | 833 | a real value, or `None` on a node task |
| `python_observed` | 843 | `None` |
| `submodules` | 849 | `None` |
| `submodules_orphaned` | 850 | `None` |
| `runner_cache_flags` | 873, **rewritten 905** | `[]` — dead; see D3 |
| `runner_cache_flags_missing` | 874, **rewritten 906** | `[]` — dead; see D3 |
| `f2p_before_not_run` | 875 | `None` |
| `duplicate_full_names` | 876 | `None` |
| `scope_files_run` | 877 | `None` |
| `scope_files_outside` | 878 | `None` |
| `bare_runner_exit` | 894 | `None` |
| `bare_runner_argv` | 895 | `None` |
| `bare_runner_skipped` | 896 | `None` (the early return sets a reason string) |

**Family B — written where measured (20 keys; THE FINDING). Every one is absent from the early-return verdict:**

| # | key | written at | also absent when |
|---|---|---|---|
| 1 | `uid` | 964 | — (container only) |
| 2 | `claude_version` | 973 | — |
| 3 | `head` | 1062 | — |
| 4 | `stripped_paths` | 1222 | — *(named by `TASKS.md`)* |
| 5 | `stripped_paths_present` | 1223 | — *(named by `TASKS.md`)* |
| 6 | `suite_timeout_s` | 1572 | — *(named by `TASKS.md`)* |
| 7 | `f2p_before_exit` | 1573 | — |
| 8 | `f2p_collection_errors` | 1619 | — |
| 9 | `f2p_red_kind` | 1624 | — |
| 10 | `p2p_before_ignored` | 1641 | — |
| 11 | `p2p_before_exit` | 1643 | — |
| 12 | `dirty_after_tests` | 1741 | — |
| 13 | `f2p_after_exit` | 1777 | the reference fix does not apply |
| 14 | `p2p_after_exit` | 1790 | the reference fix does not apply |
| 15 | `grading_build_exit` | 1814 (dynamic) | the fix does not apply, **or** `grading.build` is empty (the ordinary task) |
| 16 | `grading_typecheck_exit` | 1814 (dynamic) | ditto |
| 17 | `grading_lint_exit` | 1814 (dynamic) | ditto |
| 18 | `scope_prefixes` | 1839 | the fix does not apply, **or** `tests.p2p` is explicit |
| 19 | `scope_prefixes_absent` | 1845 | the above, **or** every declared prefix exists (the ordinary task) |
| 20 | `p2p_scoped_after_exit` | 1858 | the above, **or** no declared prefix survives the existence filter |

Rows 13–20 are what the item's "cheap" framing misses: they are absent on paths that *do* start a container, so a reader cannot even use "many nulls" as a proxy for "no container". Rows 15–17 and 19 are absent on the **ordinary, healthy, GO** verdict — the most-read blob in `<cache>/preflight/` — which is why moving only rows 4–6 would leave the schema unreadable across the two blobs a reader is most likely to diff.

**Total after this commit:** 19 (A) + 20 (B) + 1 (`early_return`) = **40 at HEAD 8232032**, plus whatever items 1–4 added (expected `ambiguous_file_filters` and `submodules_populated_after_suite`, i.e. 42). **Compute it with V1; do not carry a literal.**

### D2. `EVIDENCE_KEYS` is a tuple in write order; the grading keys come off `_GRADING_KEYS`

The shape, with the HEAD-8232032 reading as its content. **The implementer replaces the membership with V1's output** and keeps the ordering rule and the star-unpack:

```python
EVIDENCE_KEYS: tuple[str, ...] = (
    "early_return", "framework",
    "image_env_declared", "image_env_observed", "image_env_mismatch",
    "hypothesis_importable", "hypothesis_imported_by_suite",
    "python_declared", "python_observed",
    "submodules", "submodules_orphaned",            # + submodules_populated_after_suite (item 2)
    "runner_cache_flags", "runner_cache_flags_missing",
    "bare_runner_argv", "bare_runner_exit", "bare_runner_skipped",
    "uid", "claude_version", "head",
    "stripped_paths", "stripped_paths_present",
    "suite_timeout_s",
    "f2p_before_exit", "f2p_before_not_run",
    "f2p_collection_errors", "f2p_red_kind",
    "p2p_before_ignored", "p2p_before_exit",
    "dirty_after_tests",
    "f2p_after_exit", "p2p_after_exit",
    *(f"grading_{key}_exit" for key in _GRADING_KEYS),
    "scope_prefixes", "scope_prefixes_absent", "p2p_scoped_after_exit",
    "duplicate_full_names", "scope_files_run", "scope_files_outside",
                                                    # + ambiguous_file_filters (item 1)
)
```

**Expected additions and their positions in write order:** `submodules_populated_after_suite` after `submodules_orphaned`; `ambiguous_file_filters` after `scope_files_outside`. If V1 does not report one of them, that item has not landed and the tuple is simply what V1 says — the derivation is what tells you, not this paragraph.

A **tuple in the order the gate looks**, not a `frozenset` and not alphabetical: `dict.fromkeys` preserves it, so `to_dict()` and therefore `<cache>/preflight/<task_id>.json` read top-to-bottom in the order a reader would walk the gate. The comparison is `set(...) == set(EVIDENCE_KEYS)`, so order is presentation and nothing depends on it.

The grading keys are **derived from `tasks._GRADING_KEYS`**, imported, never restated — the rule `_declared_grading` already follows and states: a hand-listed copy goes stale the first time a check is added to `TaskGrading`, and that failure is the silent one. `_GRADING_KEYS` is `tuple(f.name for f in dataclass_fields(TaskGrading))` and `_declared_grading` reads `dataclass_fields(task.grading)`, so for every task `load_task` can produce, the written key and the listed key are the same string by construction (review 1 confirmed both citations exact). `preflight` already imports from `bakeoff.tasks` (`_DEFAULT_PYTHON`, `_under`, `task_runtime`); this adds one name to that import and no new dependency edge.

**Rejected:** a nested `evidence["grading"] = {key: exit}` sub-dict. It closes the key set without an import, and it renames a key that six tests and every stored blob already carry (`grading_typecheck_exit`), for no gain — the flat name is what makes an old verdict and a new one comparable, which is the property under repair.

### D3. The seed is uniformly `None`, and the two `[]` seeds it replaces are already dead

`_evidence_seed()` returns `dict.fromkeys(EVIDENCE_KEYS)` — every value `None`. One rule, stated once: **`None` is "the gate did not look"**, and every key that means anything else overwrites it where it is measured.

`runner_cache_flags` and `runner_cache_flags_missing` are seeded `[]` today, with a comment explaining that they are "measured on every path that reaches it". That comment is true and the seed is **dead**: the pair is rewritten unconditionally thirty lines later, and no `return`, `raise` or branch sits between. Deleting the `[]` seeds changes no observable value on any path.

**Rejected:** a sentinel string (`"not_measured"`). `None` is JSON `null`, the shape the existing 19 keys already use and the shape `CLAUDE.md`'s null rule is written in. A string would be indistinguishable from a measured value on the four keys whose type *is* a string (`dirty_after_tests`, `claude_version`, `f2p_red_kind`, `bare_runner_skipped`).

**Rejected:** `collections.defaultdict(lambda: None)`. It hides the drift instead of failing it, and a key never touched still does not appear in `to_dict()` — the reader is no better off.

### D4. YES to `early_return`, as a closed code and not prose

A new key, first in the tuple:

```python
#: Why the gate returned before starting a container, or `None` because it
#: did not. The set is {EARLY_RETURN_RUNNER_MISMATCH, None} and there is
#: exactly one pre-container `return PreflightResult` to name; a second one
#: adds a second constant HERE and extends T3.3. A closed code, like
#: `problem_codes`' members and for the same reason: `problems` is prose for
#: a human and nothing can machine-read it.
EARLY_RETURN_RUNNER_MISMATCH = "runner_does_not_match_framework"
```

It earns its place because with a uniform schema **"many nulls" stops being a proxy for "no container ran"**. On the early return 20 keys are null; on a healthy *node* task `python_declared`, `python_observed`, `bare_runner_exit`, `bare_runner_argv` and both `hypothesis_*` are null by design; on a healthy *explicit-p2p* task the four scope keys are null; on an ordinary GO the three `grading_*_exit` are. Four distinct null-sets, none of which is "no container started". Reconstructing the route by intersecting them is exactly the inference `problem_codes` was added to stop.

It is **evidence, not a `problem_code`**: `problem_codes` is documented as "a typed channel for the outcomes a caller has to BRANCH on" and no caller branches on this — `grade.preflight_refusal` already maps this route to `PREFLIGHT_FAILED` through `problems`, correctly. Adding a code would put an entry in a closed branch-set that nothing branches on.

`bare_runner_skipped` keeps its existing early-return string unchanged. It answers a narrower question — why *that probe* did not run — and `HARVESTING.md:83` documents it by name for the *node* case, where `early_return` is null. Two keys carrying a reason for two different scopes is not duplication.

### D5. YES to a schema assertion in the gate, in `PreflightResult.__post_init__`

```python
def __post_init__(self) -> None:
    if set(self.evidence) != set(EVIDENCE_KEYS):
        raise ValueError(...)   # names BOTH directions; text in Task 1
```

**Why in the dataclass rather than at the two `return` sites:** it covers both returns and every future one with no call-site discipline, and call-site discipline is precisely what failed twice already (M4). Stronger than it looks in a file where a third return is cheap to add.

**Why a raise is safe here, when `_gitlink_paths`' docstring argues the opposite for its own failure:** that argument turns on *what causes the failure*. `_gitlink_paths` guards against a defect in the DATA — an unreadable index in some task's tree — which an operator can hit and which must therefore become a NO-GO the driver knows how to handle, never a traceback. A key-set mismatch is a defect in THIS FUNCTION'S OWN CODE: with the seed in place, every key in `evidence` is either seeded or an overwrite of a seeded key, so the set can only diverge when someone edits `preflight()` to write a key they did not list. No task input reaches it. A traceback is the correct loud failure for that, and it can only fire in a developer's own test run — **provided the tuple was derived and not transcribed**, which is what V1 and T3.2 exist for.

**The one input-dependent surface, and why it is closed — narrower than first stated.** `evidence[f"grading_{key}_exit"]` takes `key` from `dataclass_fields(task.grading)`. A `task.grading` that is not a dataclass **at all** never reaches `__post_init__`: `dataclass_fields` raises `TypeError` inside `_declared_grading` first. So the only shape that can reach the assertion with an unlisted key is a *dataclass* stub carrying a field `_GRADING_KEYS` does not have. `load_task` refuses an unknown `grading:` key (`tasks.py:1311-1316`, `if unknown: raise TaskError(...)` — confirmed exact in review 1), so no manifest can produce one; `tests/test_preflight.py::_FakeTask` declares `grading: TaskGrading = field(default_factory=TaskGrading)`, so no existing test can either. A future fake that does gets a `ValueError` naming the unlisted key, which is the right answer. The argument holds a fortiori.

**Why `ValueError` and not a named exception:** `TaskError`, `ContainerError` and `UnknownModelError` all exist because something *catches* them. Nothing may catch this one — a caught schema error is the drift being read past again — so it takes the stdlib exception with an exhaustive message and no class of its own.

**The `evidence` field's default becomes the seed** (`field(default_factory=_evidence_seed)`), which is what keeps the assertion from becoming test churn: the three direct constructions in `tests/test_grade_script.py` omit `evidence` entirely and are about `problem_codes`; they get a full seed and pass unchanged. Review 1 confirmed those three are the only hand constructions in the tree, that none passes `evidence=`, and that no `dataclasses.replace`/`asdict`/rehydration path builds one.

### D6. Scope: `preflight.py` and `tests/test_preflight.py`, plus two version/anchor edits

No change to `grader.py`, `oracle.py`, `scripts/grade.py`, `scripts/run_matrix.py`, `schema.py`, `grade_schema.py`, any adapter, any manifest, or any image. `grade.py` reads the cache key only (M5) and `run_matrix.py` writes `result.to_dict()` verbatim and prints only `result.problems`; both are correct as they stand and get a fuller blob for free.

### D7. `PREFLIGHT_VERSION` bumps

It is in `preflight_cache_key`, and it is the only component that moves when this file changes. **What the previous version's verdict is not:** it is missing the keys measured inside the container, and it carries no `early_return`, so a pre-bump blob and a post-bump blob for the same task differ in *shape* and a reader diffing them across the boundary would read the added keys as a changed task. The bump lets a reader date the shape instead of inferring it.

No verdict moves across the boundary (T3.7 pins it), so a **cached PASS from the previous version is still true**; the bump costs one re-preflight per task on the first invocation after this lands, and buys the guarantee that every verdict on disk under the new version has the same key set. Comment text in Task 4 — **header computed from the constant on disk, not transcribed**.

---

## File Structure

```
bakeoff/src/bakeoff/preflight.py     EVIDENCE_KEYS, _evidence_seed, __post_init__,
                                     early_return, the seed rewire, PREFLIGHT_VERSION
bakeoff/tests/test_preflight.py      the parametrized invariant, the static-extraction
                                     test, 3 new unit tests, 2 inverted tests,
                                     one _ScriptedContainer knob
bakeoff/scripts/mutation_check.py    2 new anchors
TASKS.md                             strike the item
tasks/todo.md                        review section (CLAUDE.md convention)
docs/superpowers/plans/2026-09-03-round2-5-evidence-schema.md   this file
```

---

## Task 0: derive the key set (do this FIRST — it is an input to Tasks 1, 2 and 4)

- [ ] Run **V1** from Verification. Record its output: the sorted key list and `len(written)`.
- [ ] Diff it against D2's tuple. Expect exactly two additions (`ambiguous_file_filters`, `submodules_populated_after_suite`). Any *other* difference means an item landed something this plan did not anticipate — stop and re-read that item's plan before continuing.
- [ ] Record `PREFLIGHT_VERSION` as it reads on disk; the bump in Task 4 is that value plus one.

## Task 1: `EVIDENCE_KEYS`, `_evidence_seed`, and the dataclass assertion

- [ ] Add `_GRADING_KEYS` to the existing `from bakeoff.tasks import ...` line in `preflight.py`.
- [ ] Add `EARLY_RETURN_RUNNER_MISMATCH = "runner_does_not_match_framework"` beside the existing `SCOPE_PREFIX_MISSING` / `SCOPE_COLLECTS_NOTHING` constants, with the `#:` comment from D4 (including the "the set is `{EARLY_RETURN_RUNNER_MISMATCH, None}`" sentence).
- [ ] Add `EVIDENCE_KEYS` in D2's shape, **with Task 0's membership**, with a `#:` docstring carrying the why:

  > Every key `preflight` can write, in the order it writes them. ONE list, filled from by the early return and by the full path alike, because the alternative has already failed twice inside this file: `PREFLIGHT_VERSION` 11 moved three keys from `[]` to `None`, and the `bare_runner_skipped` note records a fourth "left out of this seed once" — each found by review rather than by a test. A key written where it is measured is ABSENT from every path that does not measure it, and absent renders identically to "written by a gate too old to have this key" — two absences that render identically are the same defect one layer down. `test_evidence_keys_lists_exactly_what_preflight_writes` derives this set from the source and is what keeps it from going stale the next time a key is added. The grading keys come off `tasks._GRADING_KEYS` rather than being restated, for the reason `_declared_grading` gives: a hand-listed copy goes stale the first time a check is added to `TaskGrading`, and that failure is the silent one.

- [ ] Add `_evidence_seed() -> dict` returning `dict.fromkeys(EVIDENCE_KEYS)`. Docstring: `None` is "the gate did not look", uniformly; every key that means anything else overwrites it where it is measured. **Add the sentence:** *"Every key starts `None`, including the five that are given a real value in the next few statements (`framework`, `image_env_declared`, `python_declared`, and the two cache-flag keys). Do not hand-seed those here — a seed that carries real values for some keys and `None` for the rest is family A rebuilt, which is the thing this constant replaced."* Also: not a sentinel string — four of these keys are string-typed and a sentinel would be indistinguishable from a measurement; not a `defaultdict` — that hides the drift and still emits no key from `to_dict()`.
- [ ] Change `PreflightResult.evidence` to `field(default_factory=_evidence_seed)`.
- [ ] Add `PreflightResult.__post_init__` with the D5 raise. Exact message:

  ```python
  raise ValueError(
      "preflight evidence is not the schema: missing "
      f"{sorted(set(EVIDENCE_KEYS) - set(self.evidence))}, unlisted "
      f"{sorted(set(self.evidence) - set(EVIDENCE_KEYS))}. Every key the "
      "gate can write is seeded from EVIDENCE_KEYS so that 'the gate did "
      "not look' is a null and never an absent key; a key written on one "
      "path and not another cannot be read across a set of cached "
      "verdicts, which outlive the code that wrote them."
  )
  ```

  Both directions are named because the remedies differ: a *missing* key means a path that constructs the result by hand, an *unlisted* key means a write that was never added to the tuple.

**Tests (all in `tests/test_preflight.py`):**

- [ ] **The imports first.** `tests/test_preflight.py` imports `preflight`, the six `EXIT_*` constants and four helpers from `bakeoff.preflight`, and `TaskGrading, load_task, materialize` from `bakeoff.tasks` — and **none** of the names the tests below need. Add `EVIDENCE_KEYS`, `EARLY_RETURN_RUNNER_MISMATCH`, `PreflightResult` and `_evidence_seed` to the existing `from bakeoff.preflight import (` block, and `_GRADING_KEYS` to the existing `from bakeoff.tasks import TaskGrading, load_task, materialize` line. T1.1, T1.2, T1.3, T3.1 and T3.3 all need them at module scope; T3.2 imports its own inline and needs nothing here.

- [ ] **T1.1 `test_a_preflight_result_whose_evidence_is_not_the_schema_is_refused`** — construct `PreflightResult(task_id="t", task_version=1, start_sha="s"*40, image="i", manifest_digest="d", evidence={"framework": "pytest"})` inside `pytest.raises(ValueError)`; assert the message names a missing key and the word `unlisted`. Docstring: the enforcement is in the gate and not only in the tests because a test can only assert the routes it enumerates, and the route somebody adds next is the one that has failed twice (M4).
- [ ] **T1.2 `test_an_unlisted_evidence_key_is_refused_in_both_directions`** — construct with `_evidence_seed() | {"invented": 1}`; assert `ValueError` and that `"invented"` appears in the message.
- [ ] **T1.3 `test_the_grading_evidence_keys_read_the_grading_dataclass_and_not_a_copy`** — assert `{f"grading_{k}_exit" for k in _GRADING_KEYS} <= set(EVIDENCE_KEYS)`. Docstring must claim only what the assertion pins, which is **less** than "the tuple reads `_GRADING_KEYS`": a subset check is satisfied identically by the star-unpack and by three literal strings, so it cannot tell them apart. *"All three grading names are present in the schema and correctly spelled. It does NOT detect a transcribed copy — `<=` holds for either spelling — and neither does T3.2, both of whose sides read `_GRADING_KEYS`. What a transcribed copy costs is one round, not silence: add a fourth check to `TaskGrading` and the gate writes `grading_<new>_exit`, `__post_init__` raises, and T3.2 fails on the missing member. T3.2's `ast.Starred` assertion is what closes the gap at transcription time."*

## Task 2: `preflight()` fills from the schema

Instructions are by **text to grep for**, not by line number — items 1–3 move all of these.

- [ ] Replace the statement `evidence: dict = {}` (the only one in the file) with `evidence: dict = _evidence_seed()`.
- [ ] Delete the two now-dead statements `evidence["runner_cache_flags"] = []` and `evidence["runner_cache_flags_missing"] = []`. Keep the later `evidence["runner_cache_flags"] = list(adapter.no_cache_args)` and `evidence["runner_cache_flags_missing"] = missing_cache_flags` — those write real values. Before deleting, re-confirm no `return`/`raise`/branch sits between the two pairs.
- [ ] Delete the individual `= None` seed statements, i.e. every `evidence["<key>"] = None` that appears **before** the runner-marker guard: `image_env_observed`, `image_env_mismatch`, `hypothesis_importable`, `hypothesis_imported_by_suite`, `python_observed`, `submodules`, `submodules_orphaned`, `f2p_before_not_run`, `duplicate_full_names`, `scope_files_run`, `scope_files_outside`, `bare_runner_exit`, `bare_runner_argv`, `bare_runner_skipped` — **plus whatever items 1 and 2 added to that block** (`ambiguous_file_filters`, `submodules_populated_after_suite`). Leaving them costs nothing at runtime but restores exactly the "a hand-seeded subset is the schema" reading the tuple exists to remove.
- [ ] Keep `evidence["framework"]`, `evidence["image_env_declared"]`, `evidence["python_declared"]` — real values, not seeds.
- [ ] **Rewrite three comment sites**, all of which state the superseded two-families rule and would otherwise rebuild it in prose:
  1. The comment paragraph whose first line begins **"Written BEFORE the guard below, because"** (grep for `Written BEFORE the guard below`) — re-point at `_evidence_seed()`; keep the sentence about which keys carry a *real* value before any container starts.
  2. The paragraph opening `# Written here, before any container starts, for the reason` through the `bare_runner_*` block — re-point at the schema. **Do not lose the measurements inside it:** the `f2p_before_not_run` two-paths paragraph, the `bare_runner_skipped` "left out of this seed once" note, and the `python_observed` `None`-vs-`""`-vs-node three-absence paragraph are each a recorded finding and no other comment carries them. Attach them to the keys they explain, not to a seed statement that no longer exists.
  3. The strip's comment `# Both keys are written unconditionally: "this task strips nothing" / # and "the gate did not look" render identically as a missing key, and / # absence is recorded rather than implied.` — the sentence stays *true* but its stated reason is now supplied by the seed for every key, so leaving it states the superseded rule at a second site. Re-point it: *"written with a real value rather than left at the schema's `None`, because the strip is knowable once a container exists and `[]` here is the measurement 'this task strips nothing'."*
- [ ] In the early-return block (the one whose `problems.append` names `tests.runner is ... but tests.framework is ...`), add `evidence["early_return"] = EARLY_RETURN_RUNNER_MISMATCH` beside the existing `evidence["bare_runner_skipped"] = (...)` write. Leave `bare_runner_skipped`'s string exactly as it is.
- [ ] No other statement in `preflight()` changes.

## Task 3: the invariant tests, and the two tests that assert the opposite today

- [ ] Add an `apply_exit: int = 0` parameter to `_ScriptedContainer.__init__` and a branch **before** the generic `if cmd[0] == "git": return _Exec()` catch-all:

  ```python
  if cmd[:2] == ["git", "apply"]:
      # The stderr is conditional on the exit code, like the report `cat`
      # branch above: a fixture that says "does not apply" while exiting 0
      # is one a later test can read the wrong way round. `preflight` reads
      # this stderr only under `applied.exit_code != 0`.
      return _Exec(exit_code=self.apply_exit,
                   stderr="error: patch does not apply\n" if self.apply_exit
                   else "")
  ```

  Comment for the parameter: the reference fix failing to apply is a real route through the gate — it skips `f2p_after_exit`, `p2p_after_exit`, every `grading_*_exit` and all three scope keys — and the container answered `git apply` with a bare exit 0 through the catch-all, so no test could reach it.

- [ ] **T3.1 `test_every_evidence_key_is_present_on_every_route`** — `@pytest.mark.parametrize` over the nine routes below, each a `(id, build)` pair whose `build()` returns the `(container, task)` tuple `_preflight_over` takes. Assert `set(result.evidence) == set(EVIDENCE_KEYS)`.

  **The driver is `_preflight_over(scripted)`**, which takes the helper's `(container, task)` tuple and needs no fixtures. `_run_preflight(monkeypatch, tmp_path, task, container)` is the fixture-taking variant and no route here needs it. Note the tuple order: `_node_container()` and `_pytest_container()` return **`(container, task)`**.

  ```python
  _SCHEMA_ROUTES = [
      # id, build() -> (container, task)
      ("runner_gate", lambda: _pytest_container(runner=("go", "test", "./..."))),
      ("pytest_happy", lambda: _pytest_container()),
      ("node_happy", lambda: _node_container()),
      ("explicit_p2p", lambda: _node_container(p2p=("tests/a.test.js::other",))),
      ("patch_does_not_apply", lambda: _apply_fails()),
      ("grading_declared", lambda: _grading_declared()),
      ("scope_collects_nothing", lambda: _no_prefix_exists()),
      ("prefix_absent", lambda: _one_prefix_missing()),
      ("submodule_status_failed", lambda: _submodule_status_failed()),
  ]
  ```

  with the five bespoke builders written out (each returns `(container, task)`):

  ```python
  def _apply_fails():
      tests = _FakeTests()
      task = _FakeTask(tests=tests)
      return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                present=("tests/",), apply_exit=1), task

  def _grading_declared():
      task = _FakeTask(grading=TaskGrading(typecheck=("mypy", "src")))
      return _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                present=("tests/",),
                                grading_exits={("mypy", "src"): 0}), task

  def _no_prefix_exists():
      tests = _FakeTests()
      task = _FakeTask(tests=tests)
      return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                present=()), task

  def _one_prefix_missing():
      tests = _FakeTests(paths=("tests/", "docs/tests/"))
      task = _FakeTask(tests=tests)
      return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                present=("tests/",)), task

  def _submodule_status_failed():
      tests = _FakeTests()
      task = _FakeTask(tests=tests)
      return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                present=("tests/",),
                                submodule_status_exit=1), task
  ```

  | route id | what it does NOT write |
  |---|---|
  | `runner_gate` | every container-measured key — the pre-container early return |
  | `pytest_happy` | the three `grading_*_exit` (`_FakeTask`'s `TaskGrading()` is empty, so `_declared_grading` yields nothing) and `scope_prefixes_absent` (every declared prefix exists). **Not a full key set** — no route writes all of them |
  | `node_happy` | `python_observed` (the node branch is a bare `pass`; `python_declared` IS written, as the `None` that says a node manifest may not declare `image.python`), `bare_runner_exit`/`bare_runner_argv`, both `hypothesis_*` (their two writes are inside `if interpreter is not None:` and the node adapter answers `None`) |
  | `explicit_p2p` | the whole scoped block: `scope_prefixes`, `scope_prefixes_absent`, `p2p_scoped_after_exit`, `duplicate_full_names`, `scope_files_run`, `scope_files_outside` |
  | `patch_does_not_apply` | `f2p_after_exit`, `p2p_after_exit`, the three `grading_*_exit`, all of the scoped block — D1 rows 13–20 |
  | `grading_declared` | two of the three `grading_*_exit` (only `typecheck` is declared), `scope_prefixes_absent` |
  | `scope_collects_nothing` | `p2p_scoped_after_exit`, `duplicate_full_names`, `scope_files_run`, `scope_files_outside`. It DOES write `scope_prefixes_absent` — `absent` is every declared path, which is truthy — before it refuses |
  | `prefix_absent` | nothing in the scoped block; it is the only route that writes `scope_prefixes_absent` **and still reaches the scoped run** |
  | `submodule_status_failed` | the orphan read (`submodules_orphaned` stays at the branch's explicit `None`) |

  Docstring — claim what it pins and nothing more: *"`__post_init__` already guarantees this equality for every `PreflightResult` that exists, so what this parametrization actually pins is that **each enumerated route reaches a return at all**. Remove the seed and every one of them raises instead — which is how mutation anchor 1 goes red. It does not and cannot cover a route it does not enumerate; that coverage is `__post_init__`'s, and the coverage of the tuple itself is `test_evidence_keys_lists_exactly_what_preflight_writes`. The assertion is kept in this readable form because it is the statement of the invariant, not because it is the thing that can fail."*

- [ ] **T3.2 `test_evidence_keys_lists_exactly_what_preflight_writes`** — the test that cannot be made vacuous by `__post_init__`, and the standing enforcement of Task 0. Parse `preflight.py` with `ast`, walk the `preflight` function, collect every `ast.Subscript` whose `.value` is the Name `evidence` and whose `.slice` is an `ast.Constant`; union the dynamic grading names; assert `set(EVIDENCE_KEYS) == written`.

  ```python
  def test_evidence_keys_lists_exactly_what_preflight_writes():
      import ast
      import pathlib
      import bakeoff.preflight as pf
      from bakeoff.tasks import _GRADING_KEYS

      tree = ast.parse(pathlib.Path(pf.__file__).read_text())
      fn = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "preflight")
      written = {n.slice.value for n in ast.walk(fn)
                 if isinstance(n, ast.Subscript)
                 and isinstance(n.value, ast.Name) and n.value.id == "evidence"
                 and isinstance(n.slice, ast.Constant)}
      written |= {f"grading_{key}_exit" for key in _GRADING_KEYS}

      assert set(pf.EVIDENCE_KEYS) == written
      assert len(pf.EVIDENCE_KEYS) == len(set(pf.EVIDENCE_KEYS))

      # The grading names are DERIVED, not transcribed. The subset check in
      # T1.3 cannot see the difference -- `<=` holds for the star-unpack and
      # for three literal strings alike -- and neither can the equality
      # above, since both of its sides read `_GRADING_KEYS`. This reads the
      # definition itself.
      assign = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AnnAssign)
                    and getattr(n.target, "id", None) == "EVIDENCE_KEYS")
      assert any(isinstance(e, ast.Starred) for e in assign.value.elts)
  ```

  Docstring: *"The one assertion here that fails on an `EVIDENCE_KEYS` edit rather than following it. `__post_init__` catches a key written on a path that RUNS; this catches a key written on a path nothing exercises, at edit time — and it is what keeps the tuple from going stale the next time an item adds a key, which is how this plan's own first draft was two keys short against the round's landing order. `early_return` needs no special case: it is a literal subscript in the early-return block, so the walk finds it. Only the `f`-string grading subscript is dynamic, and its names come off `_GRADING_KEYS`, which is the same source `EVIDENCE_KEYS` reads — so this asserts the two agree on a set that is derived twice, not transcribed twice — and the `ast.Starred` check reads the definition to prove the derivation is still a derivation, which is the one thing neither this equality nor T1.3's subset can see."*

- [ ] **T3.3 `test_the_early_return_names_itself`** — `runner_gate` route: `result.evidence["early_return"] == EARLY_RETURN_RUNNER_MISMATCH`. `pytest_happy` route: `result.evidence["early_return"] is None`. Docstring: with a uniform schema, "many nulls" stops being a proxy for "no container started" — a node task, an explicit-p2p task and an ordinary GO each leave their own null-set — so the route says which route it was rather than leaving a reader to intersect them. **A second pre-container return must extend this test.**

- [ ] **T3.4 (CHANGED)** `test_no_bound_is_recorded_when_no_command_ever_ran` (`def` at `test_preflight.py:1620` at HEAD 8232032; re-locate by name) — **rename** to `test_the_bound_is_null_when_no_command_ever_ran` and change `assert "suite_timeout_s" not in result.evidence` to `assert result.evidence["suite_timeout_s"] is None`. Its docstring currently reads "An absent key is honest there" — that sentence is the OLD family's rule and must be replaced: absence is not honest here because it renders identically to a verdict written by a gate that predates `budget.suite_timeout_s` (`PREFLIGHT_VERSION` 6). The rest of the claim — that a manifest value written anyway would be a claim about a run that did not happen — is unchanged and stays.

- [ ] **T3.5 (CHANGED)** `test_the_scoped_assertion_is_skipped_on_an_explicit_p2p_list` (`assert "p2p_scoped_after_exit" not in result.evidence`, at `:1505` at HEAD 8232032) — change to `assert result.evidence["p2p_scoped_after_exit"] is None`. Keep the name and the `container.scoped_runs == 0` assertion; the claim about the argv is untouched. **These two are the only absence assertions in the tree** — review 1 grepped `not in .*\.evidence` over everything and found exactly them (the third hit, `test_container.py:245`, is the unrelated isolation-evidence string) — so the suite will not go red on a third.

- [ ] **T3.6 (KEEP, do not delete)** `test_the_evidence_keys_exist_on_the_path_that_never_starts_a_container`, `test_every_new_evidence_key_is_present_on_an_explicit_p2p_task`, `test_the_new_evidence_keys_survive_the_early_return`, `test_preflight_refuses_a_runner_that_does_not_match_the_framework` (`def` at `:2973` at HEAD 8232032). T3.1 asserts key *presence*; these assert key *values* (`f2p_before_not_run is None` vs `[]`, `python_observed is None` vs `""`), which is a semantic claim set-equality cannot make. They are the ancestors of T3.1, not its duplicates.

- [ ] **T3.7 `test_the_schema_moves_no_verdict`** — for the `pytest_happy` and `runner_gate` routes assert `result.ok` and `result.problem_codes` are what they are today (`True`/`()` and `False`/`()` respectively) and that `len(result.problems)` is unchanged. Docstring: this commit changes what a stored verdict SAYS and never what it DECIDES; that is what makes a cached PASS from the previous version still true and the version bump a re-read rather than a re-judgement.

## Task 4: `PREFLIGHT_VERSION`, and the mutation anchors

- [ ] Read `PREFLIGHT_VERSION` off disk and add one (Task 0 recorded it; expect `"16" -> "17"`, **not** `13 -> 14`). Add the comment paragraph in the file's existing style, with the header computed from the two values:

  > `<N> -> <N+1>`: one evidence schema. A `<N>` verdict is missing every key measured inside the container — from `uid` and `head` through `suite_timeout_s`, the strip's two keys and the three `grading_*_exit` — because they were written where they were measured and are simply absent from any path that did not measure them. Absent renders identically to "written by a gate too old to have this key", which is the one thing the version string exists to let a reader rule out. Since `<N+1>` every verdict carries every key, `None` where the gate did not look, and `early_return` names the pre-container refusal rather than leaving a reader to infer it from which nulls are present. **No verdict moves across this boundary**: a cached `<N>` PASS was a PASS for the same reasons and a `<N>` NO-GO is still a NO-GO. What is re-run is the READING, not the judgement.

- [ ] Add two anchors to `scripts/mutation_check.py`, in the file's existing tuple shape. Re-check both `find` strings against the tree before adding — the mutator is `original.replace(find, replace, 1)` and reports STALE ANCHOR on a miss.

  ```
  ( # A key written where it is measured is absent from every path that does
    # not measure it, and absent renders identically to a verdict written by
    # a gate too old to have the key. Twenty keys were in that family.
    "preflight: let an evidence key be absent on one path and present on another",
    "src/bakeoff/preflight.py",
    "    evidence: dict = _evidence_seed()",
    "    evidence: dict = {}",
    "tests/test_preflight.py -k every_evidence_key_is_present_on_every_route",
    "not integration",
  ),
  ( # The enforcement, not the schema. A test covers the routes it enumerates;
    # this covers the route somebody adds next -- which is the one that has
    # already gone wrong twice inside this file (PREFLIGHT_VERSION 11's three
    # keys, and bare_runner_skipped's "left out of this seed once").
    "preflight: accept an evidence dict that is not the schema",
    "src/bakeoff/preflight.py",
    "        if set(self.evidence) != set(EVIDENCE_KEYS):",
    "        if False:",
    "tests/test_preflight.py -k not_the_schema_is_refused",
    "not integration",
  ),
  ```

  Two notes for the implementer. (1) Under the first mutation the seed is `{}`, so `__post_init__` raises `ValueError` out of `preflight()` and the selected test fails as an ERROR rather than an assertion. That is still red, which is what `mutation_check` requires; do not weaken the anchor to produce a prettier failure. (2) `-k not_the_schema_is_refused` does not trip pytest's `not` operator — Python tokenizes it as one NAME; review 1 ran `--collect-only` and got a clean deselect, not a parse error.

## Task 5: docs, and the item's closure

- [ ] **Docs check, performed and recorded — no doc edit is required.** `bakeoff/taskset/HARVESTING.md:83` names exactly two evidence keys: "`bare_runner_exit` stays `null` and `bare_runner_skipped` names why" on a node task. Both stay true verbatim — those keys were already in family A and their node-task values do not move. The other three `suite_timeout_s` hits in that file (`:650`, `:657`, `:664`) are **`budget.suite_timeout_s`, the manifest key, not the evidence key** — checked, so the next reader need not re-resolve them. `docs/BUILDING-A-TASK-SET.md` names no evidence key: `:233` and `:462` are the same manifest key and `:608` uses "evidence" as prose. Nothing else in `docs/` or `bakeoff/taskset/` mentions one.
- [ ] Strike the item from `TASKS.md` (at `:1221-1234` at HEAD 8232032; re-locate by its bold title). If a superseding note is left, it must correct the two things the entry got wrong: the line citation (`preflight.py:507-523`, which points inside `_gitlink_paths`' docstring, → the seed block) and the count (three keys → twenty).
- [ ] Add a review section to `tasks/todo.md` per `CLAUDE.md`'s convention: what was found (20 keys, not 3), what was built, the two rejected alternatives (nested grading sub-dict, tests-only enforcement), the fact that the plan's own first draft shipped a tuple two keys short against the round's landing order — which is why T3.2 derives it — and review 2's three LOW items (N1 the `node_happy` cell, N2 the subset check that cannot see a transcribed copy, N3 the unnamed test imports), which the reviewer asked be recorded here rather than in another revision cycle.

---

## Verification

### V1 — derive the key set (an input to Tasks 0, 1, 2 and 4, and re-run as a check afterwards)

```bash
cd bakeoff && .venv/bin/python - <<'PY'
import ast, pathlib
from bakeoff.tasks import _GRADING_KEYS
src = pathlib.Path("src/bakeoff/preflight.py").read_text()
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "preflight")
written = {n.slice.value for n in ast.walk(fn)
           if isinstance(n, ast.Subscript)
           and isinstance(n.value, ast.Name) and n.value.id == "evidence"
           and isinstance(n.slice, ast.Constant)}
written |= {f"grading_{k}_exit" for k in _GRADING_KEYS}
written |= {"early_return"}          # drop this line AFTER Task 2 lands
print(len(written)); print(sorted(written))
PY
```

Before Task 2 this is the target membership (the `early_return` union stands in for the write Task 2 adds). After Task 2 the union is redundant and the same command, without it, must give the same set — that is T3.2 as a one-liner.

### The rest

```bash
cd bakeoff
.venv/bin/python -m pytest tests/test_preflight.py -q          # after each task
.venv/bin/python -m pytest tests/ -q                           # full unit suite
.venv/bin/python -m pytest tests/test_grade_script.py -q       # the three direct constructions
```

- [ ] Full unit suite green; the count is the recorded baseline **plus** the new tests (T1.1–T1.3, T3.1's nine parametrized cases, T3.2, T3.3, T3.7).
- [ ] `tests/test_grade_script.py` passes with **no edit** — the proof that `default_factory=_evidence_seed` absorbed the assertion. If it does not, the fix is the default factory, never a `try/except` around the raise.
- [ ] `.venv/bin/python scripts/mutation_check.py` — run solo. Expect the prior count **+2**, all caught.
- [ ] `.venv/bin/python scripts/verify_logger.py` — GATE PASSED (needs a Docker daemon). Unchanged by this item; run it to prove that.
- [ ] **A real gate, and the artifact this item is about.** With a daemon:

  ```bash
  .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
      --task-set ~/.cache/bakeoff-probe/taskset --tasks bidict-389-putall-rollback-clean
  ```

  Then read `~/.cache/bakeoff/preflight/bidict-389-putall-rollback-clean.json` by hand and confirm: `len(evidence)` equals V1's count; `early_return` is `null`; `grading_build_exit`, `grading_typecheck_exit`, `grading_lint_exit` are all `null` (the task declares no grading commands) where the previous version's blob had no such keys at all; `preflight_version` is the bumped value. `bidict-389` is the right vehicle because it is the task fix 2 was measured against and its blob is the one an author is most likely to read.
- [ ] `git diff --stat` touches only the six files in **File Structure**.

---

## What this does NOT do

- **It does not change any verdict.** No `problems.append`, no `problem_codes.append`, no branch condition is touched. A task that gated GO gates GO. T3.7 pins it.
- **It does not add an assertion about a key's VALUE.** The schema is about presence. That `scope_files_outside` is `None` and not `[]` on a pytest task remains pinned by the existing per-key tests (T3.6), which this plan explicitly keeps.
- **It does not make `evidence` a dataclass.** A typed `Evidence` would carry the same guarantee and rename the JSON shape of every stored blob; the flat dict is what makes an old verdict and a new one comparable key by key, which is the property under repair.
- **It does not touch the caches' storage format.** `preflight.json` (the key-only cache) and `preflight-grade.json` are unchanged; only `<cache>/preflight/<task_id>.json`, which is `result.to_dict()`, gets fuller.
- **It does not make anything machine-read `evidence`.** No consumer is added. The reader is still a human with a diff, which is the reader an append-only artifact exists for.
- **It does not close the OTHER shape of this defect** — a key whose *value* means two things. That was `PREFLIGHT_VERSION` 11's round and is done for the four keys it found; this commit does not re-audit the rest for it.
- **It does not add an `early_return` code for anything but the runner-marker mismatch.** There is exactly one pre-container `return PreflightResult` today (review 1 grepped both sites and checked items 1 and 2 add none here). A second one must add a second constant and extend T3.3.

---

## Open questions for the reviewer

Review 1 ruled on all four; the rulings are folded into the design above. Recorded here in the house shape so a later reader can see what was asked and what was decided.

- **Q1 — add `early_return` as a closed code (D4)?** Planner's lean: yes. **Ruled: YES, approved as designed**, with one addition now made — the constant's docstring states the set explicitly as `{EARLY_RETURN_RUNNER_MISMATCH, None}`, and "What this does NOT do" carries the bullet naming T3.3 as the test a second early return must extend.
- **Q2 — raise from `__post_init__` (D5)?** Planner's lean: yes, with the closure argument resting on `load_task`. **Ruled: YES, approved; the closure argument holds a fortiori** — a `task.grading` that is not a dataclass at all never reaches `__post_init__` (`dataclass_fields` raises `TypeError` inside `_declared_grading` first), so the only reachable shape is a dataclass stub with an unlisted field. D5 now says so. The reviewer also noted the raise is the mechanism that catches a stale tuple loudly on the first run — *provided the plan does not ship a tuple it knows is short*, which is finding 1 and is now closed by Task 0 / V1 / T3.2.
- **Q3 — twenty keys instead of the item's three (D1)?** Planner's lean: yes, one commit. **Ruled: YES, and the item is wrong rather than merely under-specified** — rows 15–17 and 19 are absent from the healthy GO verdict, so the three-key fix would not fix the reader's problem on the two blobs a reader is most likely to diff.
- **Q4 — re-check the `load_task` citation (self-review 2).** **Ruled: CONFIRMED EXACT.** `tasks.py:1311-1316` is the `if unknown: raise TaskError(...)` block; `_GRADING_KEYS` evaluates to `('build', 'typecheck', 'lint')`; `_declared_grading` reads `dataclass_fields(getattr(task, "grading", None))`. D2's "derived, never restated" claim is exact.

---

## Self-review notes

- The item as written in `TASKS.md` asks for three keys to move. Following it literally would leave `grading_*_exit` and `scope_prefixes_absent` absent from the ordinary healthy GO verdict, so the reader it is written for would still be unable to diff two tasks. The inventory is D1 rather than a footnote for that reason.
- **The first draft of this plan shipped a literal 40-key tuple against a tree it will not be implemented on**, and would have made `preflight()` traceback on every path of every task once items 1 and 2 landed. That is now three separate defences — Task 0 derives, V1 is the command, T3.2 is the standing test — and it is worth stating plainly in `tasks/todo.md` because the failure was *the plan being right about the wrong tree*, which no amount of care about the current tree would have caught.
- `dict.fromkeys` returns `None` for every key including the five immediately overwritten with real values. That is deliberate — one rule, stated once — and `_evidence_seed`'s docstring now says so explicitly, because the obvious "fix" (hand-seeding those five) rebuilds family A.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

- Under **Invariants**, extending "Absence is recorded, never implied": *"`preflight`'s evidence is one schema, not a union of what each path measured. `EVIDENCE_KEYS` lists every key the gate can write and both returns fill from it, because a key written where it is measured is absent everywhere else — and absent renders identically to a verdict written by a gate too old to have the key, which is the one thing `PREFLIGHT_VERSION` exists to let a reader rule out. `PreflightResult.__post_init__` raises on a key set that is not the schema: unlike every other refusal in that file, this one can only be caused by an edit to `preflight()` itself, so a traceback is the right answer and cannot turn a task NO-GO into a crash. The tuple is pinned against the source by an AST walk, not by review — the first plan to add it was itself two keys short against the round's own landing order."*

---

## Review 1 → changes

| # | finding | change |
|---|---|---|
| 1 | **(BLOCKING)** the tuple is correct at HEAD 8232032 and wrong by two keys by implementation time | New **Global Constraints** bullet making the whole plan sequence-proof: `EVIDENCE_KEYS` is derived, never transcribed; every `preflight.py:NNN` is marked "re-locate"; `PREFLIGHT_VERSION` is read-and-add-one with `16 -> 17` expected; the key count is computed. New **Task 0** running the derivation first. New **V1** carrying the reviewer's AST snippet verbatim as a command. New **T3.2** making the derivation a standing test. D1's total, D2's tuple, D7's header and the Verification `len(evidence)` check are all now computed, and D2 names the two expected additions and their positions in write order. |
| 2 | **(BLOCKING)** no open-questions section | Added **`## Open questions for the reviewer`** with the four questions, the planner's lean on each, and the ruling folded in. Q1's ruling added the explicit `{CONSTANT, None}` set sentence to D4's docstring; Q2's added the `TypeError`-first narrowing to D5. |
| 3 | (MEDIUM) T3.1 and T3.2 cannot fail for the reason their docstrings claim | T3.1's docstring rewritten to claim only what it pins — each enumerated route *reaches a return*, which is how anchor 1 goes red — and to say explicitly that route coverage is `__post_init__`'s and tuple coverage is T3.2's. T3.2 **replaced** by the AST-static test, the one assertion here that fails on an `EVIDENCE_KEYS` edit rather than following it. |
| 4 | (MEDIUM) route table: one duplicate row, two wrong coverage cells | `no_grading_declared` **dropped** (identical to `pytest_happy`; `_FakeTask` already defaults to an empty `TaskGrading`) — nine routes, not ten. `pytest_happy`'s cell corrected to name the four keys it skips and to say no route writes a full set. `prefix_absent`'s cell corrected to "the only route that writes `scope_prefixes_absent` **and still reaches the scoped run**", with `scope_collects_nothing`'s cell now recording that it writes that key too. |
| 5 | (MEDIUM) route table not transcription-grade; three helper-signature mismatches | Every route written as an exact call. The five bespoke builders are written out in full, each returning **`(container, task)`** — the real order `_node_container`/`_pytest_container` return. The driver is named as **`_preflight_over(scripted)`**, with `_run_preflight(monkeypatch, tmp_path, task, container)` identified as the fixture-taking variant no route needs. `_ScriptedContainer` calls carry the required `start_sha=` and `tests=`. |
| 6 | (LOW) Task 2's comment audit misses the strip's site | Task 2 now lists **three** comment sites, the third being the strip's "Both keys are written unconditionally… absence is recorded rather than implied", re-pointed at `_evidence_seed()` with replacement wording given. |
| 7 | (LOW) Task 5's docs claim broader than what was checked | Task 5's bullet now records `HARVESTING.md:650`, `:657`, `:664` as checked and resolved to `budget.suite_timeout_s`, the manifest key. |
| 8 | (LOW) two off-by-one citations; all `preflight.py` line numbers short-lived | `test_no_bound_is_recorded_when_no_command_ever_ran` corrected to `def` at `:1620`, `test_preflight_refuses_a_runner_that_does_not_match_the_framework` to `:2973`. Every instruction in Task 2 is now addressed **by the exact statement text to grep for**; every remaining line number is marked "at HEAD 8232032; re-locate". |
| 9 | (LOW) scripted `git apply` reports a failure message on a success | The branch now reads `stderr="error: patch does not apply\n" if self.apply_exit else ""`, with a comment pointing at the report-`cat` branch as the consistency precedent. |

**Disputed: none.** All nine adopted, all four rulings adopted.

### Review 2 — APPROVED, three LOW open items, folded in here

| # | finding | change |
|---|---|---|
| N1 | (LOW) T3.1's `node_happy` cell counts `python_declared` as skipped, and it is not | The cell now names **`python_observed`** alone as the skipped python key, with the reason (its node branch is a bare `pass`) and the correction (`python_declared` IS written — it is a conditional *expression* at function level, executes on every path, and writes the `None` that says a node manifest may not declare `image.python`; Task 2 already keeps it). The `hypothesis_*` half was verified correct and now carries its own reason (`if interpreter is not None:`, and the node adapter answers `None`). Same class as finding 4's corrected cells, one route over. |
| N2 | (LOW) T1.3's docstring claims coverage the assertion does not have | **Both** remedies applied rather than either. T1.3's docstring is downgraded to what a subset check can pin — the three names are present and correctly spelled — and states plainly that it cannot detect a transcribed copy, that T3.2 shares the blind spot (both its sides read `_GRADING_KEYS`), and that the cost of a transcription is one round rather than silence. **And** T3.2 gains three lines that close it exactly: find the `EVIDENCE_KEYS` `AnnAssign` in the same AST and assert its value contains an `ast.Starred`, which reads the definition and is the only assertion in the plan that can see the difference at transcription time. |
| N3 | (LOW) the new tests' imports are never named | New **first bullet** in Task 1's test block naming the exact edit: `EVIDENCE_KEYS`, `EARLY_RETURN_RUNNER_MISMATCH`, `PreflightResult`, `_evidence_seed` into the existing `from bakeoff.preflight import (` block; `_GRADING_KEYS` onto the existing `from bakeoff.tasks import TaskGrading, load_task, materialize` line. Records which tests need them at module scope and that T3.2 imports its own inline. This was the one place left where an implementer would have had to invent rather than transcribe. |

**Disputed: none.** All three adopted. Per the reviewer's instruction they are also to be noted in the `tasks/todo.md` review section (Task 5) — that bullet now names them.
