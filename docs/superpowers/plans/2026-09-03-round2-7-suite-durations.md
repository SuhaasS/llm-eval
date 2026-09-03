# Round 2, item 7: the gate records how long its own bounded runs took — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every bounded command `preflight()` runs records its own wall-clock seconds in the verdict, per invocation and as a maximum. A task author sizes `budget.suite_timeout_s` from the gate's own measurement instead of from a hand-run `time docker run`; a reader of a `timed_out` grade can put the bound next to a number the suite was actually observed to need; and the gate's cost inside the one-hour SSO window becomes a sum over stored verdicts rather than a thing `HARVESTING.md` can only warn about.

**Architecture:** two new evidence keys. `bounded_run_durations_s` is a dict with one entry per bounded invocation the gate can make — `bare_runner`, `f2p_before`, `p2p_before`, `f2p_after`, `p2p_after`, `grading_build`, `grading_typecheck`, `grading_lint`, `p2p_scoped_after` — `null` where that run did not happen. `bounded_run_duration_max_s` is the largest measured entry, or `null` when nothing ran. Both are seeded by item 5's `_evidence_seed()`, so the shape is on every verdict including the pre-container early return. The seconds come off `ExecResult.duration_ms`, which `RunContainer.exec` already measures with `time.monotonic()` on the host; no second clock is introduced. Every duration is written on the same statement pair as that run's exit code, so "an exit code with no duration beside it" is unrepresentable. `run_matrix` prints the slowest run beside the bound on every task's line — PASS, NO-GO or cache hit — and one total for the gate.

**Tech Stack:** Python 3.12 (the harness venv), pytest. Every test in this plan is a unit test against `_ScriptedContainer` and a monkeypatched `resolve_tasks`; no Docker, no network, no credentials. The optional final verification step uses a real task image and a Docker daemon.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md`. Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **7**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind. Repo invariants: `CLAUDE.md`, "Configuration is never reported as observation", "Absence is recorded, never implied", and "A cached artifact is trusted only on the invariant re-checked against the artifact itself".

---

## Global Constraints

- **This item lands AFTER round-2 item 5** (`docs/superpowers/plans/2026-09-03-round2-5-evidence-schema.md`). The implementation queue in `LEDGER.md` is `3 → 1 → 2 → 4 → 5 → 7 → 6 → 8-15`: items 3, 1, 2, 4 and 5 land **before** this one and item 6 lands **after** it. This plan depends on `preflight.EVIDENCE_KEYS`, `preflight._evidence_seed()`, `PreflightResult.__post_init__` and `tests/test_preflight.py::_SCHEMA_ROUTES` existing. **Precondition check, first thing:**

  ```bash
  cd bakeoff && grep -n "EVIDENCE_KEYS\|_evidence_seed" src/bakeoff/preflight.py | head
  grep -n "_SCHEMA_ROUTES" tests/test_preflight.py | head
  ```

  If either returns nothing, **STOP and report** — do not re-implement item 5's seed here and do not add these keys to a `{}` evidence dict. Two keys added to the old two-family shape would be the exact defect item 5 exists to close, landed one item after it was closed.
- **All `preflight.py:NNN` / `run_matrix.py:NNN` line numbers in this plan are HEAD 8232032 readings, for orientation only.** Items 3, 1, 2, 4 and 5 all edit `preflight.py` before this lands. **Locate every edit by the quoted code, never by line.** The non-`preflight.py` citations (`TASKS.md:1506-1519`, `grade_schema.py`'s `suite_timeout_s` paragraph, `HARVESTING.md:643-671`, `docs/BUILDING-A-TASK-SET.md:233` and `:458-462`) were each re-checked at HEAD and are accurate, but locate those by their quoted text too.
- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode; a comment that says what a line does rather than what breaks without it does not fit here. Claims about external behaviour carry what they were verified against.
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section.
- **`SCHEMA_VERSION` does NOT move.** No `RunRecord` field is added or changes meaning.
- **`GRADE_SCHEMA_VERSION` does NOT move**, and **no `GradeRecord` field is added**. D3 is the argument; the only edit to `grade_schema.py` is a comment paragraph that becomes false when this lands.
- **`GRADER_VERSION` does NOT move.** `grader.py` is not edited at all.
- **`ORACLE_VERSION` does NOT move.** `oracle.py` is not edited at all (D4).
- **`PREFLIGHT_VERSION` bumps by ONE from whatever value is on disk when Task 5 starts.** It reads `"13"` at HEAD 8232032; items 1, 2, 3 and 5 each bump it and items 4 and 6 do not, so **expect `"17" -> "18"`**. **Read the constant and add one; do not hard-code a literal, and do not transcribe a `13 -> 14` header** — the queue may still move. Every place this plan says `PREFLIGHT_VERSION <N>` in prose to be transcribed is a placeholder for the value Task 5 just wrote. D8 is the justification.
- **No verdict moves.** Every `problems.append` and every `problem_codes.append` in `preflight()` is untouched. This commit changes what a stored verdict *says*, never what it *decides*. T2.6 pins it.
- **The gated argv and the graded argv stay byte-identical.** `_Runner` is not touched — not its `run`, not `select`, not `pass_to_pass`, not `last_timeout_s`. D2 explains why the duration is read at the call site instead.
- **Commit hygiene:** one commit (plan + code + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Do not run `scripts/mutation_check.py`** concurrently with anything else; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — record the number it prints. It will not be the round's opening `1539 passed, 62 deselected`.

---

## The measured defect

**Source:** `TASKS.md`, "**Preflight's observed suite duration is not recorded, and it is the figure two separate readings need.**" (`TASKS.md:1506-1519` at HEAD 8232032). The entry names two readings; both are checked below, and one of them turns out to be already satisfied on the grader's side (M5).

**M1 — 21 stored verdicts, zero durations.** Every blob under `~/.cache/bakeoff/preflight/*.json` (read 2026-09-02): 21 files, `preflight_version` 2, 11, 12 and 13, between 10 and 34 evidence keys each, and **not one key whose name contains `dur`**. Every one of them carries `suite_timeout_s` — the bound the argv actually held — beside nothing at all saying what the run under that bound cost.

**M2 — so every author sized the key by hand, and the probe reports are the evidence.** `~/.cache/bakeoff-probe/reports/d4-suite-timeout.md:7,57` (2026-09-02) sizes `pytest-10210-approx-nested-container` from a hand-run measurement outside the harness: `docker run ... time python -m pytest -q` → *"4470 passed, 53 skipped, 13 xfailed, 7 xpassed in 106.34s"*, Docker wall 106.889 s. The manifest then declares `suite_timeout_s: 240` with the comment *"~2x measured"* (`task.yaml:272-274`). Six other manifests in that task set carry the same shape of comment (`sqlglot-6927`: "44.36s"; `werkzeug-3037`: "5.73s"; `ufo-214`: "1.09s"; `yaml-474`: "0.24-0.57s"; `tomlkit-514`: "~1.0-1.4s"; `bidict-389`) — each a number measured by a person, in a shell, outside the gate, and then not comparable to anything the gate observed in the image it actually gates.

**M3 — reading (a): the gate's own cost is unrecorded, and the worst case is not a usable proxy for it.** `budget.suite_timeout_s` bounds **nine** commands on a pytest task (M4). At `pytest-10210`'s declared 240 s that is a worst case of **2,160 s — 36 minutes of a 60-minute SSO session, for one task**, all of it before the proxy starts. The real cost at 106 s a suite is roughly 9 minutes. The gap between 36 and 9 is the entire question `HARVESTING.md` currently answers with a warning, and a gate built on the worst case would refuse task sets that run fine. Nothing in the harness records the 9.

**M4 — the count in the docs is 8 and it has been 9 since fix 2 (2026-09-02).** Every bounded command in `preflight()`, taken off the three `"timeout"` occurrences in the file (`grep -n '"timeout"' src/bakeoff/preflight.py` → `_Runner.run`, the bare-runner probe, the grading loop):

| # | invocation | site (HEAD, locate by code) | skipped when |
|---|---|---|---|
| 1 | the bare pytest collection | `:1123-1129`, `bare = container.exec(bare_argv)` | `tests.framework` is not pytest |
| 2 | f2p before | `:1567`, `red = runner.select(tests.f2p)` | — |
| 3 | p2p before | `:1642`, `green = runner.pass_to_pass(tests, ignore=ignore)` | — |
| 4 | f2p after | `:1776`, `after_f2p = runner.select(tests.f2p)` | the reference fix does not apply |
| 5 | p2p after | `:1789`, `after_p2p = runner.pass_to_pass(tests)` | ditto |
| 6-8 | `grading.build` / `.typecheck` / `.lint` | `:1813`, `checked = container.exec([...])` — a **loop body**, once per declared key | the fix does not apply, or the key is not declared |
| 9 | the scoped p2p | `:1857`, `scoped = runner.pass_to_pass(tests, scope=scope)` | the above, or `tests.p2p` is explicit, or no declared prefix exists |

`HARVESTING.md:643-658` says "**five** times … plus once per declared `grading.*` argv" and "the worst case is 8 ×"; `docs/BUILDING-A-TASK-SET.md:233` says "up to 8×"; the `click` manifest's own comment says "five times (four when tests.p2p is declared)". All three predate the bare-runner probe, which is bounded by the same number and which `PREFLIGHT_VERSION` 13 added. The true worst case is **9 ×** on a pytest task and **8 ×** on a node one. Correcting that arithmetic is part of this commit's docs work.

**M5 — reading (b) is already half-satisfied, and the half that is missing is preflight's.** `grade_schema.CheckResult.duration_s` exists (`grade_schema.py:249`) and `grader._timing` (`grader.py:497-505`) fills it from `result.duration_ms`. It is called from exactly three places — `_State.passed` (`grader.py:419`), `_State.fail` (`:429`) and `_State.environment` (`:439`) — and **every rung that ran a command** reaches one of them with its `result`: `_check_command` (`:1131-1144`), `_check_f2p` (`:1180-1246`), `_check_p2p` (`:1340-1370`). The two rungs that do not are `not_configured` (`:421-423`, no command exists) and `refuse` at the pre-container gates (`result=None`), where `duration_s` correctly stays `None`. So on the grading side the observed seconds and the bound are already on the same line. What is missing is the *other* side of the comparison — and `grade_schema.py`'s `suite_timeout_s` docstring says so in its own words: *"The figure that would settle it — preflight's OBSERVED suite duration, against which this is the margin — is not recorded anywhere yet (`TASKS.md`)."* That sentence is what this commit makes false, which is why the file is edited (comment only, D3).

**M6 — the clock this needs already exists and is already the right one.** `ExecResult.duration_ms` (`container.py:62`) is filled by `RunContainer.exec` from `time.monotonic()` taken around `exec_run` on the host (`container.py:232-244`). It is the number `CheckResult.duration_s` is already built from, one subsystem over. Introducing a second `time.monotonic()` inside `preflight()` would be a second thing that can be wrong about the same interval.

**M7 — nothing else reads `evidence`, so a nested value breaks no consumer.** `git grep '\.evidence' bakeoff/src bakeoff/scripts` returns one hit: `preflight.py`'s own `to_dict`. `scripts/grade.py::preflight_cached` consults the caches on the key alone. The cost of the shape is paid entirely by the human reading `<cache>/preflight/<task_id>.json`, which is the reader this item is for.

---

## Design decisions, settled

### D1. One dict keyed by invocation, plus a max — not nine flat keys, and not a total

```python
#: Every bounded invocation `preflight` can make, in the order the gate makes
#: them. The grading entries come off `tasks._GRADING_KEYS` for the reason
#: `_declared_grading` and `EVIDENCE_KEYS` both give: a hand-listed copy goes
#: stale the first time a check is added to `TaskGrading`, and that failure is
#: the silent one.
#:
#: The order is the order the gate MAKES them, which is not the order the
#: manifest declares: the bare-runner probe runs before the f2p selection, and
#: the scoped p2p runs last, after the grading commands, because the grader's
#: ladder runs build and typecheck before p2p and a grading command can write
#: into the tree.
#:
#: `bare_runner` is a COLLECTION (`--co -q`), not a suite run, and it is in
#: here anyway -- which is why these are "bounded runs" and not "suite runs".
#: It carries the same `timeout <suite_timeout_s>` prefix, it has its own exit
#: 124 branch, and a count of the gate's bounded commands that leaves one out
#: is wrong about the number the one-hour SSO window is spent on. That is the
#: same off-by-one this commit corrects in three documents.
BOUNDED_RUN_KEYS: tuple[str, ...] = (
    "bare_runner",
    "f2p_before", "p2p_before",
    "f2p_after", "p2p_after",
    *(f"grading_{key}" for key in _GRADING_KEYS),
    "p2p_scoped_after",
)
```

`bounded_run_durations_s` is `dict.fromkeys(BOUNDED_RUN_KEYS)` filled where each run happens; `bounded_run_duration_max_s` is the largest non-`None` entry, or `None`.

**Why the keys are named for bounded runs and not for suites:** nothing is being renamed — both keys are new, no stored blob and no test carries either spelling — and the tuple genuinely enumerates *bounded invocations*, one of which is a collection. A key whose name needs a docstring to explain that it means something wider than it says is a key a reader will join on wrongly. The printed label stays `suite time`, which is what an author calls it.

**Why a nested dict, when item 5 rejected one for the grading exits:** that rejection was about *renaming* `grading_typecheck_exit`, a key six tests and every stored blob already carry — the flat name is what makes an old verdict and a new one comparable. Nothing is renamed here. Nine flat `*_duration_s` keys would put nine entries in the outer schema that only ever move together and are only ever read together, and would leave the max maxing over a set no key names. The dict *is* that set.

**Why the nesting does not reintroduce item 5's defect:** because the inner key set is a schema too — `BOUNDED_RUN_KEYS`, seeded whole by `_evidence_seed()`, asserted in `__post_init__` (D6). The rule "two absences that render identically are the same defect one layer down" applies one layer down.

**Why the max is stored and not left to the reader:** it is the number `run_matrix` prints and the number reading (b) is about, and a null-aware maximum is exactly the computation that is easy to get wrong — `max(())` raises `ValueError`, `max([1.0, None])` raises `TypeError`. One implementation, in the gate, recorded once.

**Rejected: storing a total as well.** `run_matrix` computes it for its own line (D7); the verdict records the measurement and the offline view decides, which is the posture `load_p95` / `vm_cpus` already take in `RunRecord.host`.

**Rejected: seconds as `duration_ms` integers.** `CheckResult.duration_s` is float seconds from the identical source; two units for one measurement across two files is a conversion a reader has to remember.

### D2. The seconds come off each `ExecResult`, at the call site — `_Runner` is not touched

```python
def _elapsed_s(result) -> float:
    """The wall-clock seconds one bounded invocation took, off its own result.

    Measured by `RunContainer.exec` with `time.monotonic()` around
    `exec_run` ON THE HOST, so it INCLUDES the docker exec round trip and
    the demux of both streams. That is the right number and not an
    approximation of a better one: `budget.suite_timeout_s` bounds a
    `timeout` INSIDE the container, but what the one-hour SSO window is
    spent on -- and what an operator waits for -- is the host-side interval,
    and sizing a bound against the smaller number is how a suite that fits
    the gate is killed under the grader.

    Not a `time.monotonic()` taken here. `CheckResult.duration_s` is already
    built from this field one subsystem over (`grader._timing`), and a second
    clock around the same call is a second thing that can be wrong about one
    interval.

    Attribute access, NOT `getattr(result, "duration_ms", None)` --
    which is what `grader._timing` does, for a reason that does not apply
    here. That helper is called with `result=None` on the rungs that ran no
    command; every call site below holds a real `ExecResult`, and a default
    there would let a stub that forgot the field record `None` as though it
    were a measurement.
    """
    return result.duration_ms / 1000.0
```

**Why not a `_Runner.last_duration_s` property beside `last_timeout_s`:** `last_timeout_s` exists because the bound cannot be read off the result at all — it has to come from the argv, and a second read of the configuration is the failure that property prevents. The duration *is* on the result, in hand at every call site. Adding it to `_Runner` would also put it in the grader's path, where `_check_f2p` and `_check_p2p` construct `_Runner` against fake `env` objects in `tests/test_grader.py` that carry no `duration_ms` — turning a two-file change into a fake-object migration for no gain.

**Why `run`'s return value is the right result:** `_Runner.run` performs up to three execs — `rm -f <report>`, the measured command, `cat <report>` — and returns only the middle one. The recorded duration is therefore the suite command alone, and excludes the report bracket. Say so in the key's docstring.

**No rounding.** `_timing` does `ms / 1000.0`; this does the same, so `12345 ms` is `12.345` in both files. Rounding is a presentation choice and lives in `run_matrix`'s format string.

### D3. The grader gets NO new field and `GRADE_SCHEMA_VERSION` does NOT move

The `TASKS.md` bullet names two readings; only one of them is missing a measurement.

- **Reading (a)** — the gate's cost inside the SSO window — is preflight's alone. The grader runs offline, needs no credentials, and is not inside that window.
- **Reading (b)** — the headroom behind a `timed_out` grade — needs two numbers: what the suite *needed* and what it was *given*. On the grade line, what it was given is `GradeRecord.suite_timeout_s` and what it took is `CheckResult.duration_s`, **already recorded on every rung that ran a command** (M5). What is missing is the *reference* measurement — the same suite, in the same image, under a gate that was not competing with a grading batch — and that is preflight's number.

So: no `GradeRecord` field, no `GRADE_SCHEMA_VERSION` bump, no `grader.py` edit. The one edit to `grade_schema.py` is the paragraph asserting the figure "is not recorded anywhere yet" — a comment, no field, no meaning change, so the version does not move for it. The sentence about there being no host or contention block on a grade stays true and stays.

**Rejected: copying preflight's max onto `GradeRecord`.** It is a property of the *task*, not of the graded run, and it would be a second copy that goes stale the moment a task is re-preflighted — configuration reported as observation, in the record that exists to say what happened. A reader who wants both joins on `task_id` against the preflight blob, which `graded_under_preflight_version` already dates.

### D4. The oracle records nothing here, and that is a decision rather than an omission

`oracle._derive` makes two bounded suite runs through `_Runner` (`oracle.py:330-331`), and `Oracle.to_dict` records none of their timing.

**It stays that way, for this item.** Neither reading needs it: the oracle runs offline with the grader (not in the SSO window), and it derives the flake quarantine rather than the pass/fail a `timed_out` grade accuses the model of. And the cost is not zero — a stored oracle is keyed on `oracle_fingerprint` = `manifest_digest|image|ORACLE_VERSION`, so recording a new field means bumping `ORACLE_VERSION` for the same "a reader must be able to date the shape" reason `PREFLIGHT_VERSION` moves, which invalidates every cached oracle and pays **two full suite runs per task** to buy a number no reading in the item asks for.

If someone later wants the offline batch's own cost profile, that is a separate item covering the oracle and the grader's per-record totals together. It is **not** filed as a `TASKS.md` entry by this commit: an unrequested measurement with a known price and no reader is a backlog line that ages, and the argument is preserved here where the next person to ask will look.

### D5. The duration is written on the same statement as that run's exit code

Every write is a *pair*:

```python
evidence["f2p_before_exit"] = red.exit_code
evidence["bounded_run_durations_s"]["f2p_before"] = _elapsed_s(red)
```

…at all nine invocations, through seven statement pairs (the grading pair is a loop body). That pairing is the invariant, not a habit: a recorded exit code with no duration beside it means the gate ran a command and lost how long it took, and T2.5 asserts the two families agree entry-for-entry on every route item 5 enumerates. It also means no branch has to be re-derived — every "did this run happen?" question the durations dict answers is already answered by the exit-code key sitting next to it.

### D6. `_evidence_seed()` carries the inner shape, and `__post_init__` refuses anything else

The seed knows the nested schema:

```python
def _evidence_seed() -> dict:
    """...  Every key is seeded to the absence ITS OWN schema defines: `None`
    for a scalar, and for `bounded_run_durations_s` -- whose value is a key
    set -- the all-null dict, because "this run did not happen" has to be a
    null inside that dict and never a missing entry.

    The inner dict is built HERE, per call, and never a module-level
    constant: a shared mutable would alias one dict across every
    `PreflightResult` in the process, and one gate's measurements would
    appear in the next gate's verdict.
    """
    seed = dict.fromkeys(EVIDENCE_KEYS)
    seed["bounded_run_durations_s"] = dict.fromkeys(BOUNDED_RUN_KEYS)
    return seed
```

and `__post_init__` gains two clauses, **after** item 5's outer key-set check (which is what guarantees the key exists before this reads it):

```python
durations = self.evidence["bounded_run_durations_s"]
if not isinstance(durations, dict):
    raise ValueError(
        "preflight evidence['bounded_run_durations_s'] is "
        f"{type(durations).__name__}, not a dict of "
        f"{len(BOUNDED_RUN_KEYS)} bounded runs. `_evidence_seed` fills the "
        "shape on every path, including the pre-container early return, so "
        "a non-dict here is a caller that built `evidence` some other way."
    )
if set(durations) != set(BOUNDED_RUN_KEYS):
    raise ValueError(
        "preflight evidence['bounded_run_durations_s'] is not the schema: "
        f"missing {sorted(set(BOUNDED_RUN_KEYS) - set(durations))}, unlisted "
        f"{sorted(set(durations) - set(BOUNDED_RUN_KEYS))}. Every bounded "
        "invocation the gate can make is seeded from BOUNDED_RUN_KEYS so "
        "that 'this run did not happen' is a null and never an absent key -- "
        "the same rule the outer schema follows, one layer down, where a "
        "nested dict is exactly where it stops being enforced."
    )
```

**Why the seed and not a write in `preflight()`:** the alternative (seed `None`, write the shape in the pre-container block) leaves a shape that is unreachable in any stored artifact — both returns run after that block — and therefore encodes nothing a reader can act on; it is a second spelling of the same absence, not a null that says which kind of null it is. It also adds a second writer to keep in step across two returns.

**Why a `not isinstance` refusal and not the `isinstance` *skip* a first draft had:** a skip accepts `[]`, `""`, `0` and `None` in silence. `PREFLIGHT_VERSION` 11 exists because `[]` claimed a measurement never made; leaving a guard that admits `[]` into the one key whose value is itself a schema re-opens that door one layer down.

**The closure argument is item 5's D5, unchanged:** the only input-dependent key here is `grading_<key>`, taken from `dataclass_fields(task.grading)`, and `load_task` refuses an unknown `grading:` key (`tasks.py:1312-1316`), so no manifest can produce one. `tests/test_grade_script.py`'s three direct `PreflightResult(` constructions pass no `evidence=` at all, so `default_factory=_evidence_seed` supplies the full nested shape and they need no edit.

### D7. Where each write goes

- **The shape** comes from `_evidence_seed()` (D6). `preflight()` writes no top-level assignment for it.
- **The nine entries** go beside their exit codes (D5).
- **The max** is written once, immediately after the `with RunContainer(...)` block closes and before the final `return`:
  ```python
  measured = [v for v in evidence["bounded_run_durations_s"].values()
              if v is not None]
  evidence["bounded_run_duration_max_s"] = max(measured) if measured else None
  ```
  On the early-return path it is never reached and stays `None` from the seed, which is the correct claim: nothing ran.

**Note for item 5's T3.2 AST walk:** it collects `ast.Subscript` nodes whose `.value` is the Name `evidence` and whose `.slice` is a Constant. `evidence["bounded_run_durations_s"]["bare_runner"] = …` parses as a Subscript **of** a Subscript: the outer node's `.value` is a Subscript (excluded), the inner node's `.value` is the Name `evidence` with slice `"bounded_run_durations_s"` (included). So the key still appears in the derived `written` set even with no top-level assignment, and the inner names do not leak into it. `bounded_run_duration_max_s` is a literal subscript and is collected. **T3.2 needs no change** — verify that by running it, not by reasoning about it.

### D8. `PREFLIGHT_VERSION` bumps by one

It is the only component of `preflight_cache_key` that moves when this file changes. A pre-bump verdict carries no `bounded_run_durations_s` at all, and — the reason the constant exists — "this gate did not measure durations" and "this gate measured and found none" must not render identically to a reader holding a stored blob. Since the bump, every verdict carries nine entries and a max.

**No verdict moves across the boundary** (T2.6), so a cached PASS from the previous version was a PASS for the same reasons. The cost is one re-preflight per task on the first invocation after this lands — which, on a task set of the probe's shape, is the very run that populates the numbers this item exists to record.

### D9. `run_matrix` prints from the verdict, never from the manifest — on all three branches

The printed bound is `evidence["suite_timeout_s"]` — read off the argv by `_Runner.last_timeout_s` — and **not** `task.budget.suite_timeout_s`. Both are in scope at the print site and they can disagree; printing the manifest's number beside a measured duration is configuration reported as observation, in the one line whose whole purpose is to let an author compare the two.

**The NO-GO branch prints and totals too, and it is the branch that matters most.** `run_matrix.py:281` writes the blob *before* the `if not result.ok:` test at `:282`, so a refused task's durations are already on disk. A task that NO-GOes because a bounded run hit `timeout` (exit 124) is the one that burned the **full** `suite_timeout_s`, up to nine times — the single largest contributor to the number reading (a) exists to check against the SSO window, and the exact task whose bound an author is about to resize. A gate total that silently meant "the cost of the tasks that passed" would be the thing this plan's own caveat line warns against.

On a **cache hit** no `PreflightResult` exists, so the line comes from the stored blob at `<cache>/preflight/<task_id>.json`, re-checked first (D10).

### D10. `verdict_matches_key`, and one shared key formatter

The two caches disagree by construction, and that is the concrete reason for the re-check rather than an analogy: `preflight.json` is written **only on PASS** (`run_matrix.py:292`), while `<cache>/preflight/<task_id>.json` is written **before** the `ok` test (`:281-282`). So a `--force-preflight` run that NO-GOes leaves the stale PASS key in the first file and its own NO-GO blob in the second, and a later warm invocation hits the cached-PASS branch with a NO-GO blob sitting under a matching filename. `ok is not True` plus the key compare is what catches it.

The format itself is extracted so the two derivations cannot drift, rather than being copied and pinned by a test:

```python
def _key_parts(manifest_digest: str, image: str, start_sha: str,
               preflight_version: str) -> str:
    """The ONE encoding of a preflight cache key.

    Two callers derive it from different places -- `preflight_cache_key` from
    a live task and the module constant, `verdict_matches_key` from a stored
    blob's own fields -- and a second copy of the join is how a verdict
    written under one gate gets served to another. The same rule
    `_declared_grading` and `EVIDENCE_KEYS` already follow: derive, never
    restate.
    """
    return f"{manifest_digest}|{image}|{start_sha}|{preflight_version}"


def verdict_matches_key(verdict: dict, key: str) -> bool:
    """Whether a STORED verdict blob describes what `key` names.

    `<cache>/preflight/<task_id>.json` is keyed on the task id and nothing
    else: it is overwritten by every preflight run of that task, PASS or
    NO-GO, under any manifest, image, tree or gate version -- and it is
    written BEFORE the `ok` test, while `preflight.json` is written only on
    PASS. So the two can disagree, and a reader that took seconds out of the
    blob because the FILENAME matched would publish an earlier gate's
    measurement under this run's line, which resolves and is therefore worse
    than the null it replaced.

    Reads the blob's four fields under the names `to_dict` writes them;
    the join itself is `_key_parts`, shared with `preflight_cache_key`.
    """
    return _key_parts(
        str(verdict.get("manifest_digest", "")),
        str(verdict.get("image", "")),
        str(verdict.get("start_sha", "")),
        str(verdict.get("preflight_version", "")),
    ) == key
```

`preflight_cache_key`'s body becomes `return _key_parts(task.manifest_digest, image, start_sha, PREFLIGHT_VERSION)` and its docstring gains one sentence saying the join moved and why. Its contract, its callers and its output are unchanged.

**Rejected: storing the seconds in `preflight.json` beside the key.** It needs no re-check (the key is right there) and it is simpler — and it puts a second, derived copy of a measurement in a second file, where `grade.py::record_preflight_pass` writes entries that would never carry it. One home for the measurement, re-checked at the door.

**Rejected: dropping the cached-path line.** Measured what the gate prints today on a cache hit (`run_matrix.py:270-275`): the image line, the start_sha line, an optional pin line, and `preflight cached PASS`. Nothing opens the blob. Without this piece, reading (a) is answerable only on a cold cache or under `--force-preflight` — and a resumed or repeated invocation, which is what `resolve_tasks`' own docstring is written about, would say nothing about suite time.

---

## File Structure

```
bakeoff/src/bakeoff/preflight.py        BOUNDED_RUN_KEYS, _elapsed_s, _key_parts,
                                        verdict_matches_key, two EVIDENCE_KEYS entries,
                                        _evidence_seed's inner shape, two __post_init__
                                        clauses, seven paired writes, the max,
                                        PREFLIGHT_VERSION
bakeoff/src/bakeoff/grade_schema.py     comment only: the paragraph that says the figure
                                        is not recorded anywhere yet
bakeoff/scripts/run_matrix.py           suite_time_line, _measured_total, cached_verdict,
                                        the three per-task lines and the gate total
bakeoff/tests/test_preflight.py         _Exec.duration_ms, _ScriptedContainer.durations_ms,
                                        _timed, 10 new tests (one parametrized)
bakeoff/tests/test_run_matrix.py        4 new tests
bakeoff/scripts/mutation_check.py       4 new anchors
bakeoff/taskset/HARVESTING.md           the "suite is fast enough" bullet: 8x -> 9x, and
                                        where to read the measurement
bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml   the suite_timeout_s comment
docs/BUILDING-A-TASK-SET.md             the "suite green but slow" row and the budget stanza
TASKS.md                                strike the item
tasks/todo.md                           review section (CLAUDE.md convention)
docs/superpowers/plans/2026-09-03-round2-7-suite-durations.md   this file
```

---

## Task 1: the schema, the helpers, and the gate assertions

- [ ] Run the precondition check from Global Constraints. STOP if item 5 has not landed.
- [ ] `_GRADING_KEYS` is already on the `from bakeoff.tasks import ...` line (item 5 added it). If it is not, add it.
- [ ] Add `BOUNDED_RUN_KEYS` exactly as written in D1, above `EVIDENCE_KEYS`, with its full `#:` docstring.
- [ ] Add `"bounded_run_durations_s"` and `"bounded_run_duration_max_s"` to `EVIDENCE_KEYS`, **immediately after `"suite_timeout_s"`**. Comment: they are listed beside the bound rather than at the sites they are filled from, because the only reading either supports is against that bound.
- [ ] Extend `_evidence_seed()` exactly as in D6, keeping item 5's existing docstring content and adding the two paragraphs shown.
- [ ] Add `_elapsed_s` exactly as written in D2, near the module-level helpers (`_present`, `_declared_grading`), with its full docstring.
- [ ] Extract `_key_parts` and rewire `preflight_cache_key` as written in D10; add `verdict_matches_key` below it.
- [ ] Extend `PreflightResult.__post_init__` with D6's two clauses, **after** item 5's outer key-set check. Comment that the ordering is load-bearing: the outer check is what guarantees the key exists before these read it.

**Tests (in `tests/test_preflight.py`):**

- [ ] **T1.1 `test_the_bounded_run_keys_include_every_declared_grading_command`** — assert `{f"grading_{k}" for k in _GRADING_KEYS} <= set(BOUNDED_RUN_KEYS)`, that `len(BOUNDED_RUN_KEYS) == 6 + len(_GRADING_KEYS)`, and that the tuple has no duplicates. Docstring: adding a check to `TaskGrading` must move this tuple, because `preflight` writes `grading_<key>` off the same dataclass and a key it writes but does not list is refused by its own schema.
- [ ] **T1.2 `test_a_bounded_run_dict_that_is_not_the_schema_is_refused`** — three constructions inside `pytest.raises(ValueError)`: `_evidence_seed() | {"bounded_run_durations_s": {"f2p_before": 1.0}}` (message names a missing key and the word `unlisted`); `_evidence_seed() | {"bounded_run_durations_s": dict.fromkeys(BOUNDED_RUN_KEYS) | {"invented": 1.0}}` (message names `invented`); `_evidence_seed() | {"bounded_run_durations_s": []}` (message names `list`). Docstring: the third is the one that matters — a guard that merely *skipped* a non-dict would accept `[]`, and `PREFLIGHT_VERSION` 11 exists because `[]` claimed a measurement never made.
- [ ] **T1.3 `test_the_seed_carries_the_full_bounded_run_schema`** — `_evidence_seed()["bounded_run_durations_s"] == dict.fromkeys(BOUNDED_RUN_KEYS)`, and `_evidence_seed()["bounded_run_durations_s"] is not _evidence_seed()["bounded_run_durations_s"]` (a fresh dict per call). Docstring: a module-level constant here would alias one mutable dict across every `PreflightResult` in the process, and one gate's measurements would surface in the next gate's verdict — the aliasing bug that never fails in a single-task test run.
- [ ] **T1.4 `test_a_fresh_verdict_matches_the_key_it_was_written_under`** — build a `PreflightResult` from a `_FakeTask`, and assert `verdict_matches_key(result.to_dict(), preflight_cache_key(task, image, start_sha))` is `True`; then `False` for blobs differing in `image`, in `start_sha`, and in `preflight_version`. Docstring: with `_key_parts` shared, the join cannot drift — what this pins is the half a shared formatter cannot cover, that `verdict_matches_key` reads the blob's four fields under the names `to_dict` writes them.

## Task 2: `preflight()` measures

- [ ] Add the seven paired writes (D5) — nine invocations, because the grading row is a loop body — each immediately after the exit-code write it belongs to:

  | after | add |
  |---|---|
  | `evidence["bare_runner_exit"] = bare.exit_code` | `evidence["bounded_run_durations_s"]["bare_runner"] = _elapsed_s(bare)` |
  | `evidence["f2p_before_exit"] = red.exit_code` | `... ["f2p_before"] = _elapsed_s(red)` |
  | `evidence["p2p_before_exit"] = green.exit_code` | `... ["p2p_before"] = _elapsed_s(green)` |
  | `evidence["f2p_after_exit"] = after_f2p.exit_code` | `... ["f2p_after"] = _elapsed_s(after_f2p)` |
  | `evidence["p2p_after_exit"] = after_p2p.exit_code` | `... ["p2p_after"] = _elapsed_s(after_p2p)` |
  | `evidence[f"grading_{key}_exit"] = checked.exit_code` (loop body) | `... [f"grading_{key}"] = _elapsed_s(checked)` |
  | `evidence["p2p_scoped_after_exit"] = scoped.exit_code` | `... ["p2p_scoped_after"] = _elapsed_s(scoped)` |

  One comment, at the first of them only:

  > Written on the same statement pair as the exit code, at every one of these sites and nowhere else. A recorded exit with no duration beside it means the gate ran a command and lost what it cost, and the pairing is what makes that unrepresentable rather than merely tested for -- there is no second "did this run happen?" branch to keep in step. Seven pairs cover nine invocations: the grading pair is a loop body and runs once per declared `grading.*` key.

- [ ] After the `with RunContainer(...)` block closes and before the final `return`, add the max exactly as written in D7, with:

  ```python
  # ONE null-aware maximum, here, rather than at each reader. `max(())`
  # raises ValueError and `max([1.0, None])` raises TypeError, so a reader
  # computing this for itself gets it wrong on exactly the two verdicts
  # that matter: the task where a run was skipped and the gate that never
  # started a container. Unreached on the early return, where the seed's
  # `None` is the honest answer -- nothing ran.
  ```

- [ ] No other statement in `preflight()` changes. No `problems.append`, no `problem_codes.append`, no branch condition, and no top-level `evidence["bounded_run_durations_s"] = …` (the seed owns the shape).

**Tests (in `tests/test_preflight.py`), after the fixture work in Task 3:**

- [ ] **T2.1 `test_every_bounded_run_records_its_own_wall_clock`** — the full pytest route, on a `_FakeTask` declaring `grading=TaskGrading(build=("make",), typecheck=("mypy", "src"), lint=("ruff", "check"))` and `_ScriptedContainer(start_sha="s" * 40, tests=task.tests, present=("tests/",), grading_exits={("make",): 0, ("mypy", "src"): 0, ("ruff", "check"): 0})` — the two leading keyword arguments are required and are what every other construction in the module passes. Assert every one of the nine entries in `result.evidence["bounded_run_durations_s"]` is a `float`, and that the key set is exactly `set(BOUNDED_RUN_KEYS)`.
- [ ] **T2.2 `test_a_bounded_run_that_never_happened_records_no_duration`** — two routes. (i) The runner-gate early return (`_pytest_container(runner=("go", "test", "./..."))`): the dict is present, every value is `None`, and `result.evidence["bounded_run_duration_max_s"] is None`. (ii) `_apply_fails()` (item 5's builder, `apply_exit=1`): `bare_runner`, `f2p_before` and `p2p_before` are floats and the other six are `None`. Docstring: the outer key set is item 5's schema test's claim and is not re-asserted here — what this asserts is that a run that did not happen is a null *inside* the dict rather than a missing entry, which is the same rule one layer down and the layer where a nested value stops being covered.
- [ ] **T2.3 `test_the_recorded_duration_is_the_execs_own_clock`** — the controlled clock. A `_FakeTask` with `grading=TaskGrading(typecheck=("mypy", "src"))`, and `_ScriptedContainer(start_sha="s" * 40, tests=task.tests, present=("tests/",), grading_exits={("mypy", "src"): 0}, durations_ms={"bare_runner": 500, "f2p_before": 12_300, "p2p_before": 41_000, "f2p_after": 1_500, "p2p_after": 2_250, "scoped": 999, ("mypy", "src"): 7_000})`. Assert **exactly**:

  ```python
  assert result.evidence["bounded_run_durations_s"] == {
      "bare_runner": 0.5,
      "f2p_before": 12.3,
      "p2p_before": 41.0,
      "f2p_after": 1.5,
      "p2p_after": 2.25,
      "grading_build": None,
      "grading_typecheck": 7.0,
      "grading_lint": None,
      "p2p_scoped_after": 0.999,
  }
  ```

  Docstring: distinct values per run, so a duration copied from the wrong result — or a constant — fails rather than passing on nine equal numbers. State the one vocabulary difference in the test body as a comment: the scripted container calls the scoped run `scoped`, and the evidence calls it `p2p_scoped_after`.
- [ ] **T2.4 `test_the_slowest_bounded_run_is_recorded`** — two halves. (i) On T2.3's container, `result.evidence["bounded_run_duration_max_s"] == 41.0`. (ii) A route where the container's **largest** scripted number belongs to a run that never happens: `_FakeTests(p2p=("tests/b.py::test_two",))` (explicit p2p, so no scoped run) with `durations_ms={"bare_runner": 500, "f2p_before": 3_000, "p2p_before": 9_000, "f2p_after": 2_000, "p2p_after": 4_000, "scoped": 60_000}`. Assert `result.evidence["bounded_run_durations_s"]["p2p_scoped_after"] is None` and `result.evidence["bounded_run_duration_max_s"] == 9.0` — **not** 60.0 and not a raise. Docstring: the max is what `run_matrix` prints beside the bound and what a `timed_out` grade is read against; it is computed once, in the gate, because a null-aware maximum over a dict with skipped runs in it is the computation a reader gets wrong, and a scripted 60 s for a run that never happened is what proves it is skipping rather than defaulting.
- [ ] **T2.5 `test_a_recorded_exit_code_always_has_a_duration_beside_it`** — the pairing invariant. `@pytest.mark.parametrize` over **item 5's `_SCHEMA_ROUTES`, reused by name** — import or reference the list, do not transcribe the ids; if item 5 lands a different set of routes this test follows it, which is the right coupling, since the pairing should be asserted over exactly the routes the schema test enumerates. For each of the nine `(exit_key, duration_key)` pairs — `bare_runner_exit`/`bare_runner`, `f2p_before_exit`/`f2p_before`, `p2p_before_exit`/`p2p_before`, `f2p_after_exit`/`f2p_after`, `p2p_after_exit`/`p2p_after`, `grading_{k}_exit`/`grading_{k}` for each `_GRADING_KEYS` entry, `p2p_scoped_after_exit`/`p2p_scoped_after` — assert `(evidence[exit_key] is None) == (evidence["bounded_run_durations_s"][duration_key] is None)`. Docstring: seven statement pairs covering nine invocations, one rule — say it that way, the way D5 and Task 2 do, so the test's own prose does not read as a third count beside the nine pairs it asserts. The failure this catches is a tenth bounded command added later with an exit code and no clock, which is how the gate got to nine commands and eight documented ones.
- [ ] **T2.6 `test_the_durations_move_no_verdict`** — for the `pytest_happy` and `runner_gate` routes, assert `result.ok`, `result.problem_codes` and `len(result.problems)` are what they are without this change (`True`/`()`/`0` and `False`/`()`/`1`). Docstring: this commit changes what a stored verdict says, never what it decides; that is what makes the `PREFLIGHT_VERSION` bump a re-read rather than a re-judgement.

## Task 3: the fixture learns a clock

- [ ] `_Exec.__init__` gains `duration_ms=0` and sets `self.duration_ms = duration_ms`. Comment:

  > The field `RunContainer.exec` fills from `time.monotonic()` on the host. Defaulted to 0 rather than omitted so every scripted answer carries it: `preflight._elapsed_s` reads the attribute directly (no `getattr` default), which is what makes a forgotten field a loud `AttributeError` in a test rather than a `None` recorded as if it were a measurement.
  >
  > The other result stubs in this module do NOT need it and must not be given it: `_Recorder`'s `_R` and `_TablessIndex` are driven through `_Runner` or a single git command directly and never reach `preflight()`, so `_elapsed_s` never sees one. If a red test says otherwise, the fix is to give THAT stub the field -- never to relax `_elapsed_s` to a `getattr` default, which is the line D2 exists to hold.

- [ ] `_ScriptedContainer.__init__` gains `durations_ms=None` → `self.durations_ms = dict(durations_ms or {})`. Docstring:

  > What each bounded invocation "took", in milliseconds, keyed by the run this container already tells apart. String keys for the five suite runs and the bare-runner probe; a **tuple** key for a grading argv, exactly as `grading_exits` is keyed, because that branch is discriminated by argv and not by name. Absent means 0, so every test that does not care records `0.0` and stays unedited.
  >
  > The vocabulary is this container's, not the evidence's: the scoped run is `scoped` here and `p2p_scoped_after` there. The one test that asserts exact seconds maps them in its own body, which is what keeps the mapping readable instead of implied.

- [ ] Add the stamping helper and route every bounded answer through it:

  ```python
  def _timed(self, key, result):
      result.duration_ms = self.durations_ms.get(key, 0)
      return result
  ```

  Call sites: the bare-runner branch in `exec` (`"bare_runner"`); in `_timeout`, the grading fall-through (`tuple(argv)`), the f2p branch (`"f2p_before"` / `"f2p_after"`), the scoped branch (`"scoped"`) and the p2p branch (`"p2p_before"` / `"p2p_after"`); in `_node_timeout`, the existing `key` local already carries exactly those five names, so its single `return` becomes `return self._timed(key, _Exec(exit_code=_node_exit(report)))`.
- [ ] **Do not change any argv, exit code or report the container answers with.** The stamp is the only new behaviour; `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` must pass untouched.

## Task 4: `run_matrix` prints it

- [ ] Import `verdict_matches_key` on the existing `from bakeoff.preflight import ...` line.
- [ ] Add `suite_time_line(verdict: dict) -> str` above `resolve_tasks`:

  ```python
  def suite_time_line(verdict: dict) -> str:
      """One line: what the gate's bounded runs cost, against the bound they carried.

      The bound comes from `evidence["suite_timeout_s"]`, which
      `_Runner.last_timeout_s` read off the ARGV, and never from
      `task.budget.suite_timeout_s`, which is in scope at both call sites.
      They can disagree, and printing the configured number beside a measured
      duration is configuration reported as observation -- in the one line
      whose entire purpose is to let an author compare the two.

      The denominator is the SCHEMA's size, not this task's worst case, and
      the wording says so: a node task can make at most eight of these nine
      (the bare-runner probe is pytest-only) and a task with an explicit
      `tests.p2p` never makes the scoped one. "2 of the schema's 9" is a
      statement about the key set; the ceiling for a given task is not
      derivable from a verdict and is not claimed here.

      Three answers, because the absences differ. A verdict written before
      the bump in this commit has no durations at all and says so with the
      version that wrote it; a verdict whose runs are all null is a gate that
      started nothing; anything else is a measurement.
      """
      evidence = verdict.get("evidence") or {}
      durations = evidence.get("bounded_run_durations_s")
      if not isinstance(durations, dict):
          return ("suite time  not recorded: written by preflight_version "
                  f"{verdict.get('preflight_version') or '?'}")
      slowest = evidence.get("bounded_run_duration_max_s")
      if slowest is None:
          return "suite time  no bounded command ran"
      measured = [v for v in durations.values() if v is not None]
      bound = evidence.get("suite_timeout_s")
      return (
          f"suite time  slowest {slowest:.1f}s of the "
          + (f"{bound}s bound" if bound is not None else "unrecorded bound")
          + f"; {sum(measured):.1f}s over {len(measured)} of the schema's "
          + f"{len(durations)} bounded runs"
      )


  def _measured_total(verdict: dict) -> float:
      """The bounded seconds a verdict recorded, 0.0 when it recorded none.

      Reads the same two keys `suite_time_line` reads, the same tolerant way
      -- `.get` at both levels -- so neither helper can raise out of
      `resolve_tasks` on a truncated, hand-edited or future-shaped blob. A
      reporting affordance may not be the thing that stops a matrix.
      """
      evidence = verdict.get("evidence") or {}
      durations = evidence.get("bounded_run_durations_s") or {}
      return sum(v for v in durations.values() if v is not None)
  ```

- [ ] Add `cached_verdict(cache: Path, task_id: str, key: str) -> dict | None` beside them:

  ```python
  def cached_verdict(cache: Path, task_id: str, key: str) -> dict | None:
      """The stored verdict blob for THIS key, or `None` -- never a raise.

      `<cache>/preflight/<task_id>.json` is filed under the task id alone and
      is written BEFORE the `ok` test, while `preflight.json` -- the cache
      that decides a PASS -- is written only on PASS. So a --force-preflight
      run that NO-GOes leaves a stale PASS key in the one file and its own
      NO-GO blob in the other, and a later warm invocation would read seconds
      from a verdict that refused the task. `preflight.verdict_matches_key`
      plus `ok is True` is what catches it.

      A miss on anything -- absent, unreadable, not JSON, wrong key, not a
      PASS -- is `None`, and the caller prints nothing.
      """
      path = Path(cache) / "preflight" / f"{task_id}.json"
      try:
          blob = json.loads(path.read_text())
      except (OSError, json.JSONDecodeError, ValueError):
          return None
      if not isinstance(blob, dict) or blob.get("ok") is not True:
          return None
      return blob if verdict_matches_key(blob, key) else None
  ```

- [ ] In `resolve_tasks`, initialise three accumulators before the loop: `gate_seconds = 0.0`, `gate_tasks = 0`, `gate_cached = 0`.
- [ ] Rewrite the **cached** branch whole. It is `print(...)` → `resolved[...] = {...}` → `continue` today; the new lines go between the print and the assignment:

  ```python
        key = preflight_cache_key(task, image, start_sha)
        if cached.get(task.task_id, {}).get("key") == key:
            print("preflight cached PASS (--force-preflight to re-run)")
            blob = cached_verdict(cache, task.task_id, key)
            if blob is not None:
                print(f"          {suite_time_line(blob)}")
                gate_seconds += _measured_total(blob)
                gate_cached += 1
                gate_tasks += 1
            resolved[task.task_id] = {"image": image, "start_sha": start_sha,
                                      "repo": work / "repo"}
            continue
  ```

- [ ] Rewrite the **fresh** branch whole, so the blob is built once and both verdict branches print and total:

  ```python
        result = preflight(
            task, image=image, repo_path=work / "repo", start_sha=start_sha,
            expected_claude_version=expected_version,
        )
        blob = result.to_dict()
        write_json(cache / "preflight" / f"{task.task_id}.json", blob)
        # Totalled BEFORE the verdict branch, and the NO-GO branch prints its
        # line too. A task refused because a bounded run hit `timeout` burned
        # the FULL suite_timeout_s, up to nine times -- the largest single
        # contributor to the number this total exists to check the one-hour
        # SSO window against, and the exact task whose bound an author is
        # about to resize. A total that quietly meant "the tasks that passed"
        # is the defect the caveat line below warns about, one line up.
        gate_seconds += _measured_total(blob)
        gate_tasks += 1
        if not result.ok:
            print("preflight NO-GO")
            for problem in result.problems:
                print(f"  - {problem}")
            print(f"          {suite_time_line(blob)}")
            failures.append(f"{task.task_id}: {len(result.problems)} problem(s)")
            continue
        print(
            "preflight PASS  f2p red at start, green after the reference; "
            "p2p green both ways; tree clean"
        )
        print(f"          {suite_time_line(blob)}")
  ```

- [ ] After the loop, before `write_json(cache_path, cached)`:

  ```python
    if gate_tasks:
        print(
            f"\ngate suite time  {gate_seconds:.1f}s across {gate_tasks} task(s)"
            + (f", {gate_cached} from cached verdicts" if gate_cached else "")
        )
        print(
            "                 bounded runs only -- image build, materialization "
            "and container start are NOT in this number"
        )
  ```

  The second line is not decoration: this figure is what an author checks the one-hour SSO window against, and a total labelled "the gate" that silently excludes the image build is the kind of number that gets believed.

**Tests (in `tests/test_run_matrix.py`):**

- [ ] **T4.1 `test_the_gate_prints_the_slowest_run_beside_the_bound`** — `suite_time_line` against a hand-built verdict: `evidence` with `suite_timeout_s: 240`, `bounded_run_durations_s` = `dict.fromkeys(BOUNDED_RUN_KEYS) | {"f2p_before": 106.3, "p2p_before": 98.0}`, `bounded_run_duration_max_s: 106.3`. Assert the exact string:

  ```
  suite time  slowest 106.3s of the 240s bound; 204.3s over 2 of the schema's 9 bounded runs
  ```

  Docstring: the format is asserted whole because it is the artifact — an author reads this line and edits a manifest from it. The denominator is the schema's nine and not this task's ceiling, which is eight on a node task and lower again with an explicit `tests.p2p`; the phrase "the schema's" is what keeps that from being read as a per-task worst case, which is the exact 9-vs-8 conflation this commit corrects in three documents.
- [ ] **T4.2 `test_the_line_says_which_kind_of_absence_it_is`** — three asserts: an all-null durations dict gives `"suite time  no bounded command ran"`; a verdict with no `bounded_run_durations_s` key and `preflight_version: "13"` gives `"suite time  not recorded: written by preflight_version 13"`; a verdict with durations and `suite_timeout_s: None` renders `unrecorded bound`. Docstring: three absences that render identically are the defect this whole round is about, and a print is where they are easiest to collapse.
- [ ] **T4.3 `test_a_cached_verdict_from_another_gate_is_not_printed`** — write a blob to `<tmp>/preflight/t.json` and assert `cached_verdict` returns it for its own key and `None` for keys differing in `image`, in `start_sha` and in `preflight_version`; `None` for a blob with `"ok": False`; and `None` for a task id with no file at all. Docstring: name the concrete disagreement — the blob is written before the `ok` test and `preflight.json` only on PASS, so the two files can disagree and a filename is not evidence about which gate wrote it.
- [ ] **T4.4 `test_the_gate_totals_the_bounded_time_it_spent`** — drive `resolve_tasks` with `force=True` and `capsys`, and four monkeypatches whose return values are written out here because one of them is a trap:

  ```python
  monkeypatch.setattr(rm, "build_task_image",
                      lambda task, base, build_root, cache: "sha256:img-" + task.task_id)
  monkeypatch.setattr(rm, "image_entrypoint", lambda image: [])   # FALSY -- see below
  monkeypatch.setattr(rm, "materialize", lambda task, repo, cache: "s" * 40)
  monkeypatch.setattr(rm, "preflight", lambda task, **kw: _CANNED[task.task_id])
  ```

  `image_entrypoint` **must** return something falsy. The two existing tests in this module that monkeypatch it (`test_run_matrix.py:259`, `:321`) return `["/inherited"]` on purpose — they use the entrypoint refusal as an early stopping point — so a copy-paste from either makes all three tasks `continue` at `run_matrix.py:255-260` and this test fails with empty stdout for the wrong reason.

  Three canned `PreflightResult`s, **each built with `evidence=_evidence_seed() | {...}`** — item 5's `__post_init__` raises `ValueError` on any other evidence dict, and a test that fails for that reason proves nothing:

  | task | ok | `suite_timeout_s` | durations | max |
  |---|---|---|---|---|
  | `a` | PASS | 600 | `f2p_before: 5.0`, `p2p_before: 7.0` | 7.0 |
  | `b` | PASS | 600 | `f2p_before: 1.0`, `p2p_before: 2.0` | 2.0 |
  | `c` | NO-GO (`problems=("the f2p run timed out",)`) | 240 | `f2p_before: 240.0` | 240.0 |

  Assert stdout contains, exactly:

  ```
  gate suite time  255.0s across 3 task(s)
                   bounded runs only -- image build, materialization and container start are NOT in this number
  ```

  and that the refused task's own line is present:

  ```
  suite time  slowest 240.0s of the 240s bound; 240.0s over 1 of the schema's 9 bounded runs
  ```

  Docstring: the per-task max answers "is this task's bound sized right"; only the total answers the question the item asks first, which is whether the gate fits inside the credential window — and the NO-GO row is in the total because a task killed by `timeout` is the largest contributor to it, not an excluded one.

## Task 5: `PREFLIGHT_VERSION`, and the mutation anchors

- [ ] Read `PREFLIGHT_VERSION` off disk and add one (expect `"17" -> "18"`; **do not hard-code**). Add the comment paragraph above the constant, in the file's existing style, with the header computed from the two values and `<N>` filled from the value just written:

  > `<N-1> -> <N>`: the gate records how long its own bounded runs took. A verdict written before this carries `suite_timeout_s` -- the bound the argv held -- beside nothing at all saying what the run under it cost, so every `budget.suite_timeout_s` in the task set was sized from a number measured by hand in a shell, outside the image the gate runs in (`~/.cache/bakeoff-probe/reports/d4-suite-timeout.md`, 2026-09-02: `pytest-10210` at 106.34 s by `time docker run`, declared at 240 s). Since `<N>` every verdict carries `bounded_run_durations_s` -- one entry per bounded invocation, `null` for a run that did not happen -- and `bounded_run_duration_max_s`. **No verdict moves across this boundary**: a cached PASS was a PASS for the same reasons and a NO-GO is still a NO-GO. What is re-run is the MEASUREMENT, not the judgement, and re-running it is the point.

- [ ] Add four anchors to `scripts/mutation_check.py`, in the file's existing tuple shape. **Transcribe every `find` string from the file after Tasks 1, 2 and 4 land**, character for character including indentation and quote style; `mutation_check.py` fails loudly on a stale anchor, which is the intended behaviour and not a reason to relax the string.

  ```
  ( # The clock, not a constant. `duration_ms` is on every ExecResult and the
    # bug that matters is reading the wrong one -- or none -- while the key
    # set still looks complete. A verdict full of zeroes reads as a suite
    # that costs nothing, which is the number an author sizes a bound from.
    "preflight: record a constant instead of the exec's own clock",
    "src/bakeoff/preflight.py",
    "    return result.duration_ms / 1000.0",
    "    return 0.0",
    "tests/test_preflight.py -k the_execs_own_clock",
    "not integration",
  ),
  ( # The one null-aware maximum. `max(())` raises and `max([1.0, None])`
    # raises, so the alternative to computing it here is every reader getting
    # it wrong on exactly the two verdicts that matter.
    "preflight: report the slowest bounded run as if nothing ran",
    "src/bakeoff/preflight.py",
    '    evidence["bounded_run_duration_max_s"] = max(measured) if measured else None',
    '    evidence["bounded_run_duration_max_s"] = None',
    "tests/test_preflight.py -k slowest_bounded_run",
    "not integration",
  ),
  ( # The inner schema. Seeded `None`, the shape exists only on the paths
    # that measure something -- which is the two-families defect item 5
    # closed, one layer down inside the one key whose value is a key set.
    "preflight: seed the bounded-run durations as a bare None",
    "src/bakeoff/preflight.py",
    '    seed["bounded_run_durations_s"] = dict.fromkeys(BOUNDED_RUN_KEYS)',
    '    seed["bounded_run_durations_s"] = None',
    "tests/test_preflight.py -k never_happened_records_no_duration",
    "not integration",
  ),
  ( # <cache>/preflight/<task_id>.json is keyed on the task id and nothing
    # else, and is written before the `ok` test while preflight.json is
    # written only on PASS. Trusting it because the filename matched
    # publishes a refusing gate's seconds under a passing run's line.
    "run_matrix: trust a stored verdict because the filename matched",
    "scripts/run_matrix.py",
    "    return blob if verdict_matches_key(blob, key) else None",
    "    return blob",
    "tests/test_run_matrix.py -k from_another_gate_is_not_printed",
    "not integration",
  ),
  ```

  Note for the implementer: under anchor 3 the seed writes `None`, so `__post_init__`'s first clause raises `ValueError` out of `preflight()` and the selected test fails as an ERROR rather than an assertion. That is still red, which is what `mutation_check` requires; do not weaken the anchor to produce a prettier failure.

## Task 6: docs, and the item's closure

- [ ] **`bakeoff/taskset/HARVESTING.md`**, the "**The suite is fast enough — or the manifest says how slow**" bullet (`:643-671`). Two edits:
  - Correct the count. The suite runs **five** times, plus once per declared `grading.*` argv, **plus the bare pytest collection the gate makes on every pytest task** (`PREFLIGHT_VERSION` 13) — so the worst case is **9 ×** the value on a pytest task and 8 × on a node one, not 8 ×. Correct "the worst case is 8 × the value for one task's gate" accordingly.
  - Add, after the "A task set of slow suites can therefore burn the credential window" sentence (fill `<N>` from the constant Task 5 wrote):

    > **Size the key from the gate's own measurement, not from a shell.** Every verdict written under `PREFLIGHT_VERSION` `<N>` or later carries `bounded_run_durations_s` — host wall-clock seconds for each of those bounded runs, `null` for the ones that did not happen — and `bounded_run_duration_max_s`, in `<cache>/preflight/<task_id>.json`. `run_matrix` prints the slowest beside the bound on every task's line, PASS or NO-GO, and totals the gate at the end. That total, not the worst case, is the number to check the one-hour SSO window against; a bound sized on a hand-run `time docker run` outside the task image is measuring a different environment than the one that will kill the suite.
- [ ] **`bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`**, the `suite_timeout_s` comment block (`:292-307`). Correct "preflight runs the suite five times (four when tests.p2p is declared) plus once per declared grading.* argv" to include the bare pytest collection and give the 9 × worst case, and add:

  ```yaml
  # Size it from bounded_run_durations_s in the cached verdict
  # (<cache>/preflight/<task_id>.json), which is what the gate itself
  # observed in this image -- not from a `time docker run` on the host,
  # which measures a different environment than the one the bound kills in.
  ```

- [ ] **`docs/BUILDING-A-TASK-SET.md`**: the "suite green but slow" row (`:233`) — "Costs up to 8× the value per task at the gate" → 9× (8× on node), and add "…and the gate records what it actually cost in `bounded_run_durations_s`". The budget stanza comment (`:458-462`) gets one line pointing at the same key.
- [ ] **`bakeoff/src/bakeoff/grade_schema.py`**, the `suite_timeout_s` docstring. Replace the sentence *"The figure that would settle it — preflight's OBSERVED suite duration, against which this is the margin — is not recorded anywhere yet (`TASKS.md`)"* with (fill `<N>`):

  > The figure it is the margin against — preflight's OBSERVED suite duration in the same image — is recorded since `PREFLIGHT_VERSION` `<N>`, in `<cache>/preflight/<task_id>.json` under `bounded_run_durations_s`; join on `task_id` and date the gate with `graded_under_preflight_version`. It is not copied onto this record, and deliberately: it is a property of the task, it would go stale the moment the task is re-preflighted, and a stale copy of a measurement is configuration reported as observation.

  Keep the rest of the paragraph verbatim — including "there is no host or contention block here", which stays true — and keep the closing instruction to read a `timed_out` grade as "this bound was hit". `GRADE_SCHEMA_VERSION` does **not** move: no field, no meaning change.
- [ ] Strike the item from `TASKS.md` (`:1506-1519`). If a superseding note is left, it must carry the two corrections this plan found: the gate's bounded-command count is **9**, not 8, and reading (b)'s grader-side half was **already recorded** in `CheckResult.duration_s` — what was missing on that side was the reference measurement, which is preflight's.
- [ ] Add a review section to `tasks/todo.md` per `CLAUDE.md`'s convention: what was found (21 stored verdicts with no duration key; nine bounded commands against eight documented; the grader already timing every rung that ran a command), what was built, the numbers the real-gate step measured, and the four rejected alternatives (a `GradeRecord` field, oracle timing, durations in `preflight.json`, a `suite_`-prefixed key name).

---

## Verification

```bash
cd bakeoff
.venv/bin/python -m pytest tests/test_preflight.py -q     # after Tasks 1-3
.venv/bin/python -m pytest tests/test_run_matrix.py -q    # after Task 4
.venv/bin/python -m pytest tests/ -q                      # full unit suite
```

- [ ] Full unit suite green. The delta over the recorded baseline is **`4 + 5 + len(_SCHEMA_ROUTES) + 4`** collected items — T1's four, T2's five unparametrized (T2.1, T2.2, T2.3, T2.4, T2.6), T2.5 once per route in item 5's list (nine at the time of writing), and T4's four. Compute it from the list rather than transcribing a number; **the gate is zero failures**, and if the count differs, explain the difference before proceeding rather than absorbing it.
- [ ] `tests/test_grade_script.py` and `tests/test_grader.py` pass with **no edit**. The first proves `default_factory=_evidence_seed` absorbed the new `__post_init__` clauses for hand-constructed results; the second proves `_Runner` was not touched.
- [ ] `.venv/bin/python -m pytest tests/test_preflight.py -k "argv_preflight_validated or evidence_keys_lists_exactly" -q` — the argv-identity gate and item 5's AST walk, both unedited and green. The second is the check D7's note is about; run it rather than reasoning about it.
- [ ] `.venv/bin/python scripts/mutation_check.py` — run **solo**. Expect the prior count **+4**, all caught.
- [ ] `.venv/bin/python scripts/verify_logger.py` — GATE PASSED (needs a Docker daemon). Unchanged by this item; run it to prove that.
- [ ] **A real gate, and the artifact this item is about.** With a daemon:

  ```bash
  .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
      --task-set ~/.cache/bakeoff-probe/taskset \
      --tasks pytest-10210-approx-nested-container
  ```

  `pytest-10210` is the right vehicle: it is the probe's slow-suite cut, its manifest declares `suite_timeout_s: 240` sized "~2x measured" off a 106.34 s hand measurement, and it is the one task in the set where the gate's own number can be compared against the number a person took outside it. **It is documented as not gate-verified** (`task.yaml:4`: *"NOT gate-verified (--preflight-only was not run"*), so **PASS or NO-GO, confirm the line and record the numbers** — with this commit the suite-time line prints on both branches and the blob is written before the `ok` test, so a NO-GO still produces the measurement. If the task cannot be gated at all (an image build failure, a missing dependency), fall back to `sqlglot-6927-dremio-trycast` (44.36 s measured, default 600 s bound) and say in the review log which vehicle produced the numbers.

  Confirm on stdout: a `suite time  slowest <n>s of the 240s bound; …` line under the verdict, and a `gate suite time  <n>s across 1 task(s)` total with its caveat line. Then read `~/.cache/bakeoff/preflight/pytest-10210-approx-nested-container.json` by hand and confirm `bounded_run_durations_s` has nine entries with `grading_*` null, `bare_runner` well under the suite runs, and `bounded_run_duration_max_s` equal to the largest non-null entry. **Record the observed numbers in `tasks/todo.md`** — they are the first measurement of the gate's real cost this repository has, and the item exists because nobody had one.
  - Then re-run **without** `--force-preflight` and confirm the cached line prints the same seconds out of the stored blob (only on a PASS — a NO-GO writes no key into `preflight.json`, so the warm run re-preflights, which is correct).
- [ ] `git diff --stat` touches only the files in **File Structure**.

---

## What this does NOT do

- **It does not change any verdict.** No `problems.append`, no `problem_codes.append`, no branch condition is touched. A task that gated GO gates GO. T2.6 pins it.
- **It does not add a refusal on a slow suite.** No problem is appended when the max approaches `suite_timeout_s`. The item's own text says why: a gate on the worst case would refuse task sets that run fine, and there is no measured distribution to set a threshold from — this commit is what produces the first one.
- **It does not touch `_Runner`**, so the gated argv and the graded argv stay byte-identical, and the grader's fake envs need no `duration_ms`.
- **It does not add a `GradeRecord` field and does not move `GRADE_SCHEMA_VERSION`, `GRADER_VERSION` or `ORACLE_VERSION`.** D3 and D4.
- **It does not record the oracle's two suite runs**, nor the grader's per-record total. D4 states the price (a forced `ORACLE_VERSION` bump, two suite runs per task to re-derive) and that no reading in the item needs it.
- **It does not time anything unbounded.** The image build, `materialize`, container start, `git status`, the strip probes, the hypothesis probes and the submodule reads are not in `bounded_run_durations_s` and not in the gate total — which is why the total prints its own caveat line. A number labelled "the gate" that quietly means "part of the gate" is worse than no number.
- **It does not claim a per-task ceiling.** The printed denominator is the schema's nine on every task, including node tasks whose ceiling is eight; the wording says "the schema's" for exactly that reason, and no code derives a per-task maximum from a verdict.
- **It does not make `grade.py` store an evidence blob.** That driver runs `preflight()` too and discards `result.evidence` entirely (`grade.py:305-313`), so a gate run under the grader records durations that nothing keeps. That is a real gap, it is one line plus a decision about which cache owns the file, and it is not this item's subject — leave it, and leave this sentence where the next person will find it.
- **It does not put the durations in `matrix-<stamp>.json`.** That file describes cells; `resolve_tasks` returns `(resolved, failures)` and threading a fourth value through it to reach the summary would be a signature change for a number already on stdout and on disk.
- **It does not add a consumer.** Nothing branches on a duration. The reader is a human sizing a manifest, which is the reader the preflight blob exists for.

---

## Self-review notes

- The item's framing — "one measurement answers both" — is right, but only after checking that the grader's half of reading (b) was already there. It was: `_timing` has three call sites and every rung that runs a command reaches one of them with its `result`. Had that not been checked, this plan would have added a `GradeRecord.suite_duration_s` duplicating `CheckResult.duration_s` and moved `GRADE_SCHEMA_VERSION` for a field the file already had. M5 is the check; re-run it (`grep -n "_timing" bakeoff/src/bakeoff/grader.py`) rather than taking it from here.
- The nine-versus-eight count is the second finding, and it changes a documented number in three files. It follows from `grep -n '"timeout"' bakeoff/src/bakeoff/preflight.py` returning three sites, one of which is `_Runner.run` (five call sites) and one of which is a loop over `_declared_grading` (up to three) — worth re-deriving rather than trusting.
- The largest remaining judgement is `_evidence_seed()` owning the nested shape rather than `preflight()` writing it. The consequence to watch is item 5's T3.2 AST walk, which now finds `bounded_run_durations_s` only through the *inner* subscript of the paired writes. That is checked by argument in D7 and must be checked by running the test; if a future item ever removes all nine paired writes at once, the key would silently drop out of the derived set.
- `cached_verdict` and `verdict_matches_key` are the largest piece of this plan the item's one-sentence brief does not ask for. Review 1 ruled them in and required the shared `_key_parts`, which is what makes them cheap: no format is copied, and the one test left is about field names rather than about a join.
- The key rename (`suite_*` → `bounded_run_*`) is the one place this plan departs from the wording of the task brief. Nothing is renamed in the artifact — both keys are new — and the tuple genuinely enumerates bounded invocations, one of which is a collection.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

- Under **Invariants**, after the preflight entry: *"The gate records what its own bounded runs cost. `bounded_run_durations_s` carries host wall-clock seconds for each of the nine commands `preflight` runs under `budget.suite_timeout_s` — the bare collection, four suite runs, up to three grading argvs and the scoped p2p — `null` for the ones that did not happen, and `bounded_run_duration_max_s` beside them. The seconds come off `ExecResult.duration_ms`, the same field `CheckResult.duration_s` is built from, and are written on the same statement pair as that run's exit code so that an exit with no duration beside it is unrepresentable. Without it every `budget.suite_timeout_s` in the task set was sized from a `time docker run` taken by hand, outside the image the bound kills in, and the gate's cost inside the one-hour SSO window — up to 9 × the bound per task, all of it before the proxy starts — could be documented and never checked."*
- Under **Config gotchas**, beside the credential-window entry: *"The gate is 9 bounded commands per pytest task, not 8: the bare-runner probe carries the same `timeout` prefix as the five suite runs and the three grading argvs. `run_matrix` prints the slowest beside the bound on every task's line — PASS, NO-GO or cache hit, and the NO-GO is the one that burned the whole bound — and totals the gate at the end, bounded runs only; the image build, materialization and container start are not in that number."*

---

## Review 1 → changes

All 16 findings adopted; none disputed. The five open questions were ruled on in the review and the rulings are folded into the design sections named below.

| # | finding | change |
|---|---|---|
| 1 | **(BLOCKING)** `PREFLIGHT_VERSION` will be 18, not 14; five prose sites hard-code 14 | Re-derived from `LEDGER.md` and the five plans: items 1, 2, 3, 5 bump; 4 and 6 do not; queue is `3 → 1 → 2 → 4 → 5 → 7 → 6`. Global Constraints now says **expect `"17" -> "18"`, read and add one, do not hard-code**. Every literal `14` is gone: D8, Task 5's version paragraph, Task 4's `suite_time_line` docstring ("written before the bump in this commit"), `_measured_total` (no version mentioned at all), Task 6's HARVESTING sentence and `grade_schema.py` replacement now carry `<N>`, filled from the constant Task 5 just wrote. |
| 2 | **(BLOCKING)** a NO-GO task records durations, prints none, and is missing from the gate total | Adopted. The blob is bound before `write_json` and **`gate_seconds` / `gate_tasks` accumulate before the verdict branch**; the NO-GO branch prints its line after the problem list, before the `continue`. D9 carries the argument (a `timeout`-killed task burned the full bound up to nine times and is the largest contributor to the total). T4.4 gains a third, refused task and asserts both the total and the refused task's own line. The Verification step's real-gate instruction is rewritten accordingly (finding 15). |
| 3 | T2.5's route ids are not item 5's and one does not exist | Adopted. T2.5 now says **parametrize over item 5's `_SCHEMA_ROUTES`, reused by name — do not transcribe the ids**, with the reason (the pairing invariant should be asserted over exactly the routes the schema test enumerates, and it then follows item 5 automatically). `no_grading_declared` is gone; T2.2's second route now uses item 5's own `_apply_fails()` builder. |
| 4 | `_measured_total` can raise `KeyError` out of the gate | Adopted. It is now `(verdict.get("evidence") or {}).get("bounded_run_durations_s") or {}`, given its own docstring saying it reads the same two keys the same tolerant way `suite_time_line` does, and that a reporting affordance may not stop a matrix. |
| 5 | **(ruling on Q4)** `_evidence_seed` should know the nested shape; drop the `isinstance` guard | Adopted in full. D6 is rewritten: the seed builds the inner dict **per call** (T1.3 pins the fresh-dict property and names the aliasing bug), `preflight()`'s pre-container write is deleted, and `__post_init__` gains **two** clauses — a `not isinstance(...)` **refusal** (not a skip, so `[]`/`""`/`0`/`None` cannot pass in silence) and the key-set equality. D6 records that the old `None` shape was unreachable in any artifact and therefore a second spelling rather than a null. D7 carries the reviewer's AST note about item 5's T3.2, and Verification runs that test explicitly rather than reasoning about it. Task 1's third test is restated as "the seed carries the full inner schema". |
| 6 | the key name is the only thing self-review note 3 trades away, and it is free to fix | Adopted. `suite_durations_s` → **`bounded_run_durations_s`**, `suite_duration_max_s` → **`bounded_run_duration_max_s`**, `SUITE_RUN_KEYS` → **`BOUNDED_RUN_KEYS`**, throughout — architecture, D1, every task, every test, every anchor, every doc edit and the `CLAUDE.md` sentences. The printed label stays `suite time`. D1 carries the argument; self-review note 3 is replaced by a note recording the departure from the brief's wording. |
| 7 | **(ruling on Q1)** keep `cached_verdict`/`verdict_matches_key`, but do not copy the key format | Adopted. `_key_parts` is extracted and **both** `preflight_cache_key` and `verdict_matches_key` call it, so drift is impossible rather than tested against. D10 now argues from the measured disagreement the reviewer supplied — the blob is written before the `ok` test, `preflight.json` only on PASS — instead of by analogy to the pruned mirror, and that sentence is also in `cached_verdict`'s docstring, T4.3's docstring and mutation anchor 4's comment. T1.4's claim is narrowed to "the blob's four fields are read under the right names". |
| 8 | the Verification arithmetic is wrong and the baseline sentence contradicts the queue | Adopted. The delta is now the formula **`4 + 5 + len(_SCHEMA_ROUTES) + 4`**, to be computed rather than transcribed, with "the gate is zero failures; explain any difference, do not absorb it". The ordering sentence now reads "items 3, 1, 2, 4 and 5 land first; item 6 lands after this one", matching `LEDGER.md`. |
| 9 | M5 over-claims "every rung", and that sentence carries D3 | Adopted. M5 now says **"every rung that ran a command"**, cites `_timing`'s three call sites (`grader.py:419`, `:429`, `:439`) as the closed argument, and names the two rungs that legitimately carry no timing (`not_configured`, and `refuse` at the pre-container gates). D3 restates the narrowed claim. |
| 10 | the bare attribute read in `_elapsed_s` is safe and the plan does not record why | Adopted. Task 3's `_Exec` comment now carries the sentence: `_Recorder`'s `_R` and `_TablessIndex` never reach `preflight()`, only `_ScriptedContainer` does, and the fix for a red test is to give *that* stub the field — never to relax `_elapsed_s` to a `getattr` default. |
| 11 | "nine paired writes" is followed by a seven-row table | Adopted. Task 2 and D5 now say **seven statement pairs covering nine invocations**, with the grading row named as a loop body, in the table caption and in the in-code comment. |
| 12 | the cached-branch insertion point is under-specified | Adopted. All three branches — cached, NO-GO and PASS — are now written out whole, including the surrounding `resolved[...]` assignment and `continue`. |
| 13 | T2.4's second half and T4.4 leave values to the implementer | Adopted. T2.4 (ii) is now a concrete route with its `durations_ms` written out, chosen so the container's **largest** number (60 s) belongs to the run that never happens and the expected max is 9.0 — which also fixes the sentence that did not describe T2.3's numbers. T4.4 carries a three-row table of canned results and the exact expected strings, **and the instruction that each canned `evidence` must be built from `_evidence_seed()`** or item 5's `__post_init__` fails the test for the wrong reason. |
| 14 | "of 9 bounded runs" is always 9, including on a node task where 8 is the ceiling | Adopted. The rendered line now reads `over 2 of the schema's 9 bounded runs`; `suite_time_line`'s docstring says the denominator is the schema's size, names the node-task eight and the explicit-`tests.p2p` case, and states that a per-task ceiling is not derivable from a verdict and is not claimed. T4.1's docstring says the same, and "What this does NOT do" gains the matching bullet. |
| 15 | the real-gate vehicle is documented as not gate-verified | Adopted. The step now reads **"PASS or NO-GO, confirm the line and record the numbers"** — which finding 2's change makes true — quotes `task.yaml:4`'s own "NOT gate-verified" note, and names `sqlglot-6927-dremio-trycast` (44.36 s measured, 600 s default) as the fallback vehicle, with the review log required to say which one produced the numbers. The warm re-run step now notes that a NO-GO writes no `preflight.json` key, so the second invocation correctly re-preflights. |
| 16 | line citations into `preflight.py` will have drifted by four items | Adopted. New Global Constraints bullet: every `preflight.py` / `run_matrix.py` line number is a HEAD 8232032 reading for orientation, **locate by the quoted code, never by line**; the non-`preflight.py` citations are named as re-checked at HEAD and are to be located by their quoted text too. M4's table now carries the code string for each of the nine sites beside its line number. |

### Review 2

**APPROVE — 16/16 review-1 findings confirmed addressed, 0 open.** All five requested checks came back correct: the per-call nested seed, the `__post_init__` refusal (with the noted and deliberate asymmetry that `suite_time_line`'s `not isinstance` stays a *skip*, because there a non-dict means "a verdict written before the bump" and the honest answer is a printed sentence rather than a raise out of the driver), the shared `_key_parts` (verified at HEAD that all four `to_dict` fields are already `str`, so the coercions change no output and no cache key moves for the refactor), the NO-GO branch counting toward the gate total, and the key rename. Also confirmed independently: T4.4's arithmetic (5.0 + 7.0 + 1.0 + 2.0 + 240.0 = 255.0, `gate_cached` 0 under `force=True`, so the `", N from cached verdicts"` suffix is correctly absent); T2.3's float compares are exact in CPython (`12300/1000.0 == 12.3`, `999/1000.0 == 0.999`, `2250/1000.0 == 2.25`); T2.4 (ii)'s explicit-`tests.p2p` argv lands on `_ScriptedContainer._timeout`'s p2p branch, which is the route the test needs; and each of the four mutation selectors names exactly one test. The one exclusion left in the gate total is honest and stays: a task refused at the ENTRYPOINT check `continue`s before any preflight and contributes nothing, because no bounded command ran.

The three non-blocking implementer notes are folded in:

| # | note | change |
|---|---|---|
| 1 | T4.4's monkeypatch return values were not written out, and `image_entrypoint` is a trap | T4.4 now writes all four `monkeypatch.setattr` lines out verbatim. `image_entrypoint` returns `[]`, flagged `# FALSY -- see below`, with the reason spelled out: the two existing tests in the module that patch it (`test_run_matrix.py:259`, `:321`) return `["/inherited"]` **on purpose** — they use the entrypoint refusal as an early stopping point — so a copy-paste from either makes all three tasks `continue` at `run_matrix.py:255-260` and the test fails with empty stdout for the wrong reason. |
| 2 | T2.5's docstring said "seven sites, one rule" while the test asserts nine pairs | The docstring line is now "seven statement pairs covering nine invocations, one rule", said the way D5 and Task 2 say it, so the test's own prose cannot read as a third count beside the nine pairs it asserts. |
| 3 | T2.1 and T2.3 wrote `_ScriptedContainer(..., present=…)` with a literal ellipsis for two required keyword arguments | Both now spell `start_sha="s" * 40, tests=task.tests` in full, which is what every other construction in the module passes — the plan is transcription-grade throughout. |
