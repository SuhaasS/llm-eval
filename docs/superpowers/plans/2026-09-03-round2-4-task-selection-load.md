# Round 2, item 4: a `--tasks` selection may skip an uncommitted broken sibling, and nothing else may — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One broken manifest in a shared drafting directory must stop exactly the work that needs it, and no more. `load_task_set` keeps validating every manifest under the root — it must, because that is what makes `task_set_commit` mean something — but a refusal is now *collected* rather than raised on the spot, and the decision to refuse or to warn is taken once, in one function, against three facts: whether a `--tasks` selection was given, whether the refused directory is named by it, and whether the refused manifest is **part of the revision a record would name**. A manifest tracked in that revision and failing to load is a defect in the task set itself and refuses whatever the selection says. One that is untracked, ignored, or in no repository at all is work in progress, and a selection that does not name it may proceed past it with a WARNING that names both the manifest and the selection.

**Architecture:** All of the decision lives in `bakeoff/src/bakeoff/tasks.py`. `load_task_set_with_refusals(root, only=None)` is the one implementation; `load_task_set(root, only=None)` becomes a two-line wrapper over it with its existing signature and return type, so `scripts/grade.py` and `scripts/judge.py` are **not edited at all** and inherit the new refusal text unchanged. `scripts/run_matrix.py` is the only caller that has a task selection to give, so it is the only one that switches to the reporting form and prints `tasks.refusal_warnings(...)`.

**Tech Stack:** Python 3.12 (the harness venv), pytest, git. No Docker, no credentials, no spend — every test in this plan is a unit test.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.7 the task manifest, §5.7 the matrix). Procedural companion: `docs/BUILDING-A-TASK-SET.md` (§1.5 make the task set a git repository, §3.6 run the gate, §9 scaling to a set). Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **4**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind.

---

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. A comment that says what a line does rather than what breaks without it does not fit here. Claims about external behaviour carry what they were verified against (`git 2.50.1`).
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section.
- **No version constant moves.** `SCHEMA_VERSION`, `GRADE_SCHEMA_VERSION`, `GRADER_VERSION`, `PREFLIGHT_VERSION`, `ORACLE_VERSION` all stay. Justified in D7 — nothing this plan touches changes the shape of a `RunRecord`, a `GradeRecord`, a preflight verdict or an oracle derivation. This is a **loader** change on the host, before any image is built.
- **`scripts/grade.py` and `scripts/judge.py` are not edited.** That is a design decision, not an omission — see D5.
- **Backwards compatibility:** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` loads unchanged; `load_task_set(root)` and `load_task_set(root, only=[...])` keep their signatures and their `list[TaskManifest]` return, so `scripts/grade.py:814`, `scripts/judge.py:5044`, `tests/test_tasks.py:487,498` and the twelve `tests/test_judge_script.py` monkeypatches are untouched. **Exactly one existing test is edited:** `tests/test_run_matrix.py:212-215`, which monkeypatches the symbol `run_matrix.main` will stop calling (Task 2.4). That edit adds and removes no test.
- **Tests pin every new invariant** with a unit test in `bakeoff/tests/`. Nothing here is marked `integration`.
- **Commit hygiene:** one commit (plan + code + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End the message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Do not run `scripts/mutation_check.py`** concurrently with anything else; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`, `scripts/mutation_check.py`, `scripts/verify_logger.py` → GATE PASSED. **Read the counts off the tree you start from and record them** (round-start figures were `1539 passed, 62 deselected` and mutation `160/160`, but items 1–3 land before this one and move both).

---

## The measured defect

**Source:** `TASKS.md`, "**`load_task_set` validates every manifest in the task-set root before `--tasks` filters, so one broken sibling manifest blocks every other task's gate and grade**". Measured 2026-09-02, twice independently; written up in `~/.cache/bakeoff-probe/reports/w6-tomlkit-514.md`, "Blocking issue found before step 1".

**M1 — the measurement.** Six probe workers were cutting tasks concurrently into one shared task-set directory, `~/.cache/bakeoff-probe/taskset/`. Worker 6, gating an unrelated task, ran the documented gate command and got:

```
task set: /Users/suhaassurapaneni/.cache/bakeoff-probe/taskset/sqlglot-8225-mysql-key-constraint/task.yaml:image.env: 'SETUPTOOLS_SCM_PRETEND_VERSION' is not an allowed image.env key. The allowed set is ['CI', 'HYPOTHESIS_STORAGE_DIRECTORY']; it is an allowlist because a denylist would have to anticipate CLAUDE_CODE_USE_BEDROCK, which bypasses the proxy and leaves the wire log empty with the run still looking normal
```

The command was `run_matrix.py --preflight-only --force-preflight --task-set <shared> --tasks tomlkit-514-inline-table-comment-separator`. The named task's manifest is valid and loads. The error names a **different worker's in-progress task**, and nothing in the message says so. `scripts/grade.py --taskset <shared>` was blocked identically. The worker's workaround was to copy one task into an isolated directory and keep two copies of the manifest in sync by hand — which is itself a way to gate one manifest and collect against another.

**M2 — the code.** `bakeoff/src/bakeoff/tasks.py::load_task_set` (currently at line 1431) walks every directory under the root, calls `load_task` on each, and only then applies `only`:

```python
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (task_dir / MANIFEST_NAME).exists():
            continue
        tasks.append(load_task(task_dir, set_commit=commit))
    if only:
        ...
```

The first `load_task` that raises ends the walk. Callers: `scripts/run_matrix.py:426` (with `only=` from `--tasks`), `scripts/grade.py:814` (`only=None`), `scripts/judge.py:5044` (`only=None`). `run_matrix.py:429` and `grade.py:815` both `print(f"task set: {exc}")` and `return 1`, which is why the two measurements read identically; `judge.py:5049` routes the same string through `_print_ascii_safe`. Every string this plan specifies is pure ASCII — `--` rather than an em dash, no smart quotes — so the multi-line report passes that guard unchanged. Neither `grade.py` nor `judge.py` has a task selection to give: their `--only` (`grade.py:798`) and `--only-task` (`judge.py:167`) filter *records*, not the manifest walk.

**M3 — a second, unreported failure on the same line.** `load_task` calls `yaml.safe_load`, which raises `yaml.YAMLError` — **not** a `TaskError`. A sibling whose manifest is not valid YAML therefore escapes both scripts' `except TaskError` and reaches the operator as a traceback rather than as a refusal. Not in the probe report; found reading the path for this plan, and fixed here because the collector has to decide what to do with *every* way a manifest can fail to load, not just the well-behaved one.

**M4 — a warn-and-proceed that is not conditioned on committed-ness would create a worse defect than the one it fixes.** `docs/BUILDING-A-TASK-SET.md` §1.5 says, in bold, *commit the task set before every collection run*, because `task_set_commit` is what makes a stored result re-derivable. If a **committed** manifest is invalid, every record written against that clean sha names a revision of a directory that does not load — and the reader who later runs `grade.py` over those records is refused, because `grade.py` has no `--tasks` and loads the set whole. A blanket "unselected refusals are warnings" therefore lets a broken committed task set stay broken across an entire collection and surface only at grading time, after the tokens are spent. That is the hazard the `TASKS.md` entry names ("a committed task set can carry a broken manifest that nobody notices until the full run"), and it is the reason this plan does not implement the third remedy unconditionally.

**M5 — why `grade.py` cannot take the warn-and-proceed path even if it had one.** `scripts/grade.py::_grade_one` (line 463) is:

```python
    if task is None:
        return _refused(
            record, None, "", NotGradedReason.TASK_NOT_FOUND,
            f"task_id {record.task_id!r} is not in the loaded task set",
        )
```

If `grade.py` were made to skip a refused manifest and grade anyway, every record naming that task would be written a permanent `task_not_found` line saying *"is not in the loaded task set"* — which is **false**: the task is in the set, its manifest is invalid. `TASK_NOT_FOUND` is a `not_graded` reason, so it does not even make the exit code 1. That is precisely the "plausible-looking zero" shape the codebase refuses, written into a file that is only ever appended to.

---

## Design decisions, settled

**D1 — the chosen remedy is the item's third option, gated on committed-ness; the item's (b) comes along for free.**
Load every manifest, collect every refusal, then decide once:

| a refusal is… | and the manifest is… | outcome |
|---|---|---|
| named by `--tasks` (by directory name) | either | **refuse**, tagged `SELECTED by --tasks as '<name>'` |
| not named by `--tasks` | tracked in the enclosing revision | **refuse**, tagged `committed, and NOT the task you selected (<ids>)` — the selection is in the text, which is the item's remedy (b) on the one branch this change creates |
| not named by `--tasks` | untracked, ignored, or in no repository | **WARNING** naming the manifest and the selection; load proceeds |
| — (no `--tasks` given at all) | either | **refuse**, tagged `no --tasks selection was given, so the whole set is required` |

Every refusal message names the offending manifest by absolute path (it already does — `load_task`'s `where` is `str(manifest_path)`) **and** tags why *that* refusal is fatal, which is remedy (b). So the item's two named remedies are both delivered: (a)-with-a-guard for the drafting path, (b) for every path that still refuses.

**D2 — committed-ness is per directory, not per set, and it takes TWO git facts, not one.** `task_set_commit` already tells us whether the *set* is dirty, but a set can be dirty for an unrelated reason (an edited `README`) while the broken manifest is committed, and vice versa. The predicate has to answer *"is this task directory part of the revision a record would name?"*, and `git status --porcelain -- <dir>` on its own answers something narrower: *"does the enclosing repository report anything for this path?"*. Those diverge in a layout that is entirely ordinary — **an ignored scratch task set inside a git repository**. Measured (review round 1, git 2.50.1, `~/.cache/plan4-review/gitcheck2.py`): with `scratch/` in `.gitignore`, `task_set_commit` returns the *enclosing* repo's HEAD (non-empty, so D4's guard does not fire), `git status --porcelain -- <scratch>/t-002` prints nothing and exits 0 (ignored paths are absent without `--ignored`), and status-alone therefore reports the directory as **committed** and hard-refuses it under a message asserting the opposite of the truth — `git ls-files --error-unmatch -- <dir>` exits non-zero, nothing there is tracked. A gate whose false answer is "refuse, because this is committed" on an untracked directory is strictly worse than no gate: it re-breaks the drafting workflow this item exists to unblock, while claiming a fact.

So the predicate is two terms in order: **tracked** (`git ls-files --error-unmatch`) decides whether the path is in the index at all, and only if it is does **clean** (`git status --porcelain`) decide whether it is modified. Untracked, ignored, or outside the index all mean *not part of any revision* → skippable. Cost is unchanged in the shape that matters: both subprocesses run **per refusal only**, and the happy path never reaches this function.

Rejected: deriving committed-ness from `task_set_commit`'s `-dirty` suffix (one boolean behind two different questions); `git status --ignored` (it would report the directory, but as `!!`, which still has to be distinguished from a real modification — `ls-files` answers the question directly); and skipping the check entirely (M4).

**D3 — the pathspec is resolved, and that is not a formality.** `task_set_commit`'s own docstring records the measurement: `git status -- <pathspec>` resolves the pathspec against the CWD, so passing a relative path while `cwd` is that same directory asks about `<root>/<root>` — which matches nothing, git prints nothing and exits 0, and **every directory reads as committed**. Under this design that failure is not cosmetic: it would flip every warning back into a refusal. Both paths are resolved inside `_manifest_committed` before the call.

**D4 — the "tracked but unreadable" case fails closed; the empty-commit guard is an economy, not a correctness argument.** Once a path is known to be tracked, a `git status` that cannot run leaves us unable to prove the manifest is work in progress, so that half returns `True` (committed → fatal). `_manifest_committed` is then called **only when `task_set_commit(root)` returned a non-empty sha** — an empty commit means "not a git repository", "git unusable", or "a repo with no commits yet" (`git rev-parse HEAD` fails on the third), and in all three there is no revision for a record to name, so the caller can answer without asking git.

**That guard is not what makes the non-repo case correct, and saying it is would be dangerous here.** Measured against the two-term predicate, git 2.50.1: on a root outside any repository `_manifest_committed` returns `False` **on its own**, because `ls-files --error-unmatch` exits non-zero and the path reads as untracked before the status half runs. It is the `ls-files` term that covers it. Under the superseded one-term predicate the guard *was* load-bearing (every git call exits 128 in a non-repo, and the fail-closed rule read that as committed), and a maintainer who found that justification still written down could delete the `ls-files` term on the strength of it — which is finding 2 from review round 1, reintroduced. So the guard is kept for what it actually buys: two subprocesses not spent asking git about a directory in no repository, at the layer that already knows the set has no revision.

**One boundary this change is the first to make load-bearing:** `task_set_commit` names the **enclosing** repository. For the in-repo task set (`bakeoff/taskset/`) that is the *harness* repo, not a task-set repo, so "committed" here means "committed to whatever repository encloses this path". Pre-existing and documented behaviour, but this plan turns it into a refuse/warn decision, so it goes in `_manifest_committed`'s docstring.

**D5 — one implementation, three callers, and only one of them is edited.**
- `load_task_set_with_refusals(root, only=None) -> tuple[list[TaskManifest], list[RefusedManifest]]` is the implementation.
- `load_task_set(root, only=None) -> list[TaskManifest]` keeps its exact current signature and return type and delegates. `scripts/grade.py:814` and `scripts/judge.py:5044` pass no selection, take the "whole set is required" branch, and get the improved message **with no source change**.
- `scripts/run_matrix.py` is the only caller with a selection, so it is the only one that calls the reporting form and prints the warnings.

Rejected: adding the same (always-empty, because `only is None`) warning-printing block to `grade.py` so the two call sites look identical. It would be dead by construction, and dead code with a comment explaining why it is dead is worse than the comment alone. The guarantee that the two drivers cannot drift comes from the shared implementation, not from duplicated call sites.

Rejected: changing `load_task_set`'s return type to the tuple. Three call sites and ~15 test monkeypatches (`tests/test_run_matrix.py:213`, `tests/test_judge_script.py` × 12) bind the current shape; churning them buys nothing.

Rejected: an out-parameter (`refusals: list | None = None`) or a callback (`on_warning=print`). Both make it possible to filter with `only` and silently drop the warnings — the exact silence this item is about.

**D6 — `grade.py` does NOT gain a `--tasks` flag in this plan.** It was designed and rejected here rather than left unconsidered. It would work — `--tasks` alongside the existing `--only RUN_ID`, intersected, plus a `task_ids=` parameter on `grade_event_log` skipping unselected records into the existing `skipped` bucket — and it would make the two drivers literally symmetric. It is out of scope because: (i) the round-2 constraint for this item is that *the full-set path (no `--tasks`) still refuses on any invalid manifest*, and `grade.py` having no selection **is** the full-set path; (ii) grading writes verdicts, and every alternative that lets it proceed past an unreadable manifest runs into M5; (iii) the operator's escape hatch already exists and is the one the probe worker used — point `--taskset` at a directory holding only the tasks being graded. Carried to open questions, not implemented.

**D7 — no version constant moves.** `SCHEMA_VERSION` guards `RunRecord`; nothing here adds or changes a field. `GRADER_VERSION` / `GRADE_SCHEMA_VERSION` guard the verdict; no `NotGradedReason` is added and no ladder check changes (that is exactly why D6's `TASK_MANIFEST_INVALID` reason is not being added — it *would* move `GRADER_VERSION`). `PREFLIGHT_VERSION` keys a cached verdict computed from `(manifest digest, image id, start sha)`; a manifest that loads today loads identically after this change, byte for byte, so no cached verdict becomes wrong. `ORACLE_VERSION` is untouched.

**D8 — the refused directory is matched against the selection by DIRECTORY NAME, and the hole that leaves is closed loudly rather than papered over.** A manifest that did not load has no readable `task_id`, so the only name available is the directory's. The taskset convention is `<root>/<task_id>/task.yaml`, but `load_task` does not enforce it (`tests/test_tasks.py::test_a_duplicate_task_id_is_refused_at_load` writes directories `a` and `b` both declaring `t-001`). So a refused directory named `foo` could declare `task_id: bar`, and `--tasks bar` would proceed thinking `bar` was not refused. That case cannot pass silently: `bar` is then absent from the loaded ids, the existing `missing = wanted - known` check fires, and this plan extends its message to name every surviving refusal as a directory that could not be checked for that id. Rejected: requiring `task_id == directory name` (a new refusal on manifests that load fine today — a different item), and best-effort re-reading the id out of an unparseable file.

**D9 — refusals are collected from *any* exception, not only `TaskError`, and the type is reported QUALIFIED.** M3: `yaml.YAMLError` is not a `TaskError`, and neither is an `OSError` on an unreadable file. The collector catches broadly (`except Exception`, with the same `# noqa: BLE001` rationale `grade.py::grade_event_log` already carries — one manifest, not the set) and formats a non-`TaskError` as

```python
f"{manifest_path}: {type(exc).__module__}.{type(exc).__name__}: {exc}"
```

The path prefix is load-bearing, not cosmetic: PyYAML's own traceback names no file (it reports `in "<unicode string>"`, measured in review round 1), so without the prefix the operator is told a manifest is broken and not which one. The **module** qualifier is there because `yaml.YAMLError` is a base class that is never the concrete type — an unterminated flow sequence raises `ParserError`, and other malformed shapes raise `ScannerError`, `ComposerError`, `ConstructorError`. Bare `ParserError` names a family the reader has to guess at; `yaml.parser.ParserError` does not. Builtins render as `builtins.OSError`, which is noisier than `OSError` and is accepted for the one rule rather than a special case.

Nothing becomes quieter: under `only=None` every collected refusal is re-raised as a `TaskError`, so a malformed-YAML sibling that used to escape both drivers' `except TaskError` as a bare traceback now produces a refusal they already catch and print.

**D10 — a warning does not become a refusal just because the run is live, and the reason is the drafting loop, NOT a `-dirty` marker.** A `--tasks` live cell against a directory with an uncommitted broken sibling proceeds with the warning: `docs/BUILDING-A-TASK-SET.md` §7 ("One live cell") is part of the drafting loop this item exists to unblock, and refusing live-but-not-preflight would put a second, invisible rule behind one flag. An earlier draft of this decision rested on the record carrying `task_set_commit` with `-dirty`; **that is false in the headline case.** A shared scratch directory (`~/.cache/bakeoff-probe/taskset/`, which 3.2 and V5 both model) is not a git repository, so `task_set_commit` is `""`, not `"<sha>-dirty"` — measured in review round 1, and `run_matrix.py:440` prints it as `not a git repo`.

What the record actually carries is that `""`, which is already the honest blank meaning *"this result is not re-derivable against a revision"* — and that covers a warned run exactly as it covers any other run over a non-repo set. It is not a marker for "manifests were skipped", and this plan does not add one. A *record-level* field would move `SCHEMA_VERSION` (D7) and would store one collection-wide fact once per cell; the cheap alternative — a `"refused_manifests"` key in the matrix summary `run_matrix.py:709-711` already writes — moves no version constant and is where a later item should start (open question 5). **The consequence is stated rather than papered over** — the WARNING on the driver's stdout is the *only* place the skip is recorded, it goes in §3.6's new paragraph, and it appears in "what this does NOT do". That `""` account is not the whole story: when the drafting directory sits *ignored* inside an enclosing repository (the layout review round 1's finding 2 added, and `test_an_ignored_task_set_inside_a_repo_is_not_committed` pins), `task_set_commit` is not blank at all — it returns the enclosing repository's clean HEAD, a sha that names a revision the task set is not even part of. A warned run over that layout is therefore indistinguishable from an ordinary clean run by anything the record carries, which is a stronger silence than the `""` case and is called out separately in §3.6 and below.

---

## File Structure

```
bakeoff/src/bakeoff/tasks.py          # RefusedManifest, _manifest_committed, _fatal_reason,
                                      # _refusal_report, refusal_warnings,
                                      # load_task_set_with_refusals, load_task_set (wrapper)
bakeoff/scripts/run_matrix.py         # import + call the reporting form, print the warnings
bakeoff/scripts/mutation_check.py     # three anchors
bakeoff/tests/test_tasks.py           # 8 tests
bakeoff/tests/test_run_matrix.py      # 2 tests
docs/BUILDING-A-TASK-SET.md           # §1.5 cross-reference, §3.6 paragraph, §9 bullet
TASKS.md                              # strike the item
docs/superpowers/plans/2026-09-03-round2-4-task-selection-load.md   # this file
```

---

## Task 1: the loader — collect, decide, report

- [ ] **1.1** In `bakeoff/src/bakeoff/tasks.py`, immediately **above** the existing `load_task_set` (currently line 1431), add the dataclass. `dataclass` and `subprocess` and `Path` are already imported by this module.

```python
@dataclass(frozen=True)
class RefusedManifest:
    """One task directory under a task-set root whose manifest did not load.

    `error` is the message the load raised. A `TaskError` already opens with
    the absolute path of the offending `task.yaml` (`load_task`'s `where`), so
    a caller printing these never has to reconstruct which file is meant; the
    collector prefixes the path itself for the exception types that do not
    carry one.

    `committed` is the whole reason a refusal can ever be downgraded to a
    warning, and it is stated per DIRECTORY rather than per set: it means
    "this directory is part of the revision a record would name". A directory
    that is untracked, ignored, or in no repository at all is work in progress
    -- `docs/BUILDING-A-TASK-SET.md` section 1.5 says to commit the task set
    before every collection, so an uncommitted one is by definition not the
    thing a collection runs against. A TRACKED manifest that does not load is
    a different animal: the set's own revision is broken, every record written
    against it names a sha that promises a set which does not load whole, and
    `grade.py` -- which has no `--tasks` and loads the set whole -- grades
    every record naming that task as TASK_NOT_FOUND, at exit code 0. So such a
    refusal is fatal whatever the selection says, and the drafting affordance
    cannot be used to carry a broken task set through a collection.
    """

    directory: Path
    error: str
    committed: bool
```

- [ ] **1.2** Add `_manifest_committed`, directly below the dataclass.

```python
def _manifest_committed(root: Path, task_dir: Path) -> bool:
    """True when this task directory is part of the revision a record names.

    TWO facts, in this order, because one of them alone answers a different
    question. `git status --porcelain -- <dir>` reports what the enclosing
    repository has to SAY about a path, and it says nothing about an IGNORED
    path -- measured, git 2.50.1: a scratch task set inside a repo whose
    `.gitignore` holds `scratch/` gets exit 0 and empty stdout, while `git
    ls-files --error-unmatch` on the same path exits non-zero because nothing
    there is tracked. Status alone therefore calls an untracked drafting
    directory "committed" and hard-refuses it under a message asserting the
    opposite of the truth -- which re-breaks the exact workflow the warning
    path exists to allow. So: tracked decides whether the path is in the
    index at all, and only then does clean decide whether it is modified.

    Pathspec discipline, learned the way `task_set_commit` learned it: `git
    status -- <pathspec>` resolves the pathspec against the CWD, so a relative
    path plus `cwd=root` asks about `<root>/<root>/...`, matches nothing, and
    git prints nothing and exits 0 -- every directory would read as committed.
    Here that is not cosmetic: it would flip every warning back into the
    refusal this function exists to lift. Both paths are resolved first, for
    both calls.

    The status half fails CLOSED. Once a path is known to be tracked, a `git
    status` that cannot run leaves us unable to prove the manifest is work in
    progress, and guessing permissively is how a committed task set that does
    not load whole reaches the end of a collection.

    "Committed" means committed to whatever repository ENCLOSES this path.
    `task_set_commit` names the enclosing repo, which for the in-repo task set
    (`bakeoff/taskset/`) is the harness repo rather than a task-set repo. That
    is pre-existing, and this is the first code to turn it into a refuse/warn
    decision.

    The caller does not call this when the set has no commit at all -- an
    empty `task_set_commit` means not a git repository, or git unusable, or no
    commits yet -- and that guard is NOT what makes the non-repo case correct.
    Measured, git 2.50.1: called on a root outside any repository this returns
    `False` on its own, because `ls-files` exits non-zero and the path reads as
    untracked before the status half is reached. The guard is kept because
    asking git twice about a directory in no repository spends two subprocesses
    to learn nothing, and because the caller is the layer that already knows
    the set has no revision. Do not read it as the thing that covers a non-repo
    root: under the SUPERSEDED one-term predicate it was, and removing the
    `ls-files` term on that reading reintroduces the ignored-directory refusal
    above.

    TOTAL: returns a bool for every input and raises for none. It is called
    from inside the collector's `except` block, where a raise would chain onto
    the manifest's own error and escape `load_task_set_with_refusals` as an
    exception no driver catches -- the traceback this change exists to remove,
    reintroduced one layer over.
    """
    root = Path(root).resolve()
    task_dir = Path(task_dir).resolve()
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(task_dir)],
            cwd=root, capture_output=True, text=True,
        ).returncode == 0
    except OSError:
        return True
    if not tracked:
        return False
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", str(task_dir)],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return True
    return not status
```

- [ ] **1.3** Add the three text builders, below `_manifest_committed`. The strings are transcribed, not paraphrased.

```python
def _fatal_reason(refusal: RefusedManifest, wanted: set[str] | None) -> str:
    """Why THIS refusal cannot be skipped. Order is precedence, not taste:
    a directory that is both selected and committed is refused because it was
    asked for, which is the fact the operator can act on.

    The third branch names the SELECTION, not just the sibling. `TASKS.md`
    states the remedy as "the error should name the offending sibling AND say
    explicitly that it is not the selected task", and the committed-unselected
    branch is the one a curated task set hits every time -- the branch this
    change creates. Without the selection in the text it reproduces exactly
    the operator experience the probe reported: a message about a sibling,
    with no mention of what was actually asked for.
    """
    if wanted is not None and refusal.directory.name in wanted:
        return f"SELECTED by --tasks as {refusal.directory.name!r}"
    if wanted is None:
        return "no --tasks selection was given, so the whole set is required"
    return (
        f"committed, and NOT the task you selected "
        f"({', '.join(sorted(wanted))}): this manifest is not work in "
        "progress, so the task set's own revision does not load and no "
        "selection can skip it"
    )


def _refusal_report(root: Path, fatal: list[RefusedManifest],
                    wanted: set[str] | None, skippable: int) -> str:
    """The refusal text. `skippable` is the count of refusals that were NOT
    fatal, and it is stated when non-zero because the header claims every
    listed manifest is required -- a reader who knows there were four
    refusals and sees one listed would otherwise be reading a report that
    silently dropped three."""
    lines = [
        f"{root}: {len(fatal)} manifest(s) in this task set did not load, "
        "and every one of them is required here:"
    ]
    for refusal in fatal:
        lines.append(f"  - [{_fatal_reason(refusal, wanted)}]")
        lines.append(f"    {refusal.error}")
    if skippable:
        lines.append(
            f"({skippable} further manifest(s) here also did not load and "
            "would have been skippable under this selection; they are listed "
            "only when the load is allowed to proceed.)"
        )
    lines.append(
        "Every record stamps task_set_commit -- the revision of this whole "
        "directory -- so a set that does not load whole is not a state to "
        "record against, and grade.py, which has no --tasks and loads the set "
        "whole, cannot tell that a manifest it cannot read is not the one a "
        "record names. Fix the manifest, or move it out of this directory. "
        "Only work in progress can be skipped -- a manifest not tracked in "
        "this directory's revision, or in a directory that has none -- and "
        "only by a --tasks selection that does not name it "
        "(docs/BUILDING-A-TASK-SET.md section 1.5: commit the task set before "
        "every collection)."
    )
    return "\n".join(lines)


def refusal_warnings(refusals: list[RefusedManifest], *, root: Path,
                     selected: set[str]) -> list[str]:
    """The lines a driver prints for refusals it was allowed to skip.

    A list of lines rather than a print, because `tasks.py` is a library and a
    module that prints cannot be asserted against. Returned as text rather
    than as the refusals themselves so that two drivers cannot word the same
    finding differently -- the reason this is a function at all.

    The middle clause claims only what was measured. "Uncommitted work in
    progress" would be an overclaim about a directory in a tree that is not a
    git repository at all, where nothing is known about work in progress; what
    IS known is that no such refusal is committed in this task set's revision,
    because there is none or because the directory is not tracked in it.

    Empty whenever `refusals` is: `load_task_set_with_refusals` returns a
    non-empty list only on the branch that was allowed to proceed.
    """
    if not refusals:
        return []
    lines = [
        f"WARNING: {len(refusals)} manifest(s) under {root} did not load and "
        f"were skipped. None of them is a selected task "
        f"({', '.join(sorted(selected))}), and none is committed in this task "
        "set's revision (it has none, or the directory is not tracked there):"
    ]
    lines += [f"  - {refusal.error}" for refusal in refusals]
    lines.append(
        "WARNING: these are refusals, not skips. A run without --tasks, and "
        "any grade.py run over this directory, refuses until each one is "
        "fixed or moved out. This warning is the only place the skip is "
        "recorded -- no record field carries it."
    )
    return lines
```

- [ ] **1.4** Replace the body of `load_task_set` with the reporting implementation plus a wrapper. Keep the existing docstring's duplicate-`task_id` paragraph — it is still true and still the reason that check exists.

```python
def load_task_set_with_refusals(
    root: Path, only: list[str] | None = None
) -> tuple[list[TaskManifest], list[RefusedManifest]]:
    """Every task under `root`, validated, in a stable order -- and the
    directories that would not load, when the caller is allowed to skip them.

    Duplicate task_ids raise. `run_id` is a hash of (task_id, model, sample,
    attempt), so two tasks sharing an id would collide in the event log and
    the second one's records would be refused as immutability violations --
    at the far end of a matrix, after the tokens were spent.

    A refusal is NOT raised where it is found. Measured 2026-09-02 in a shared
    drafting directory: one worker's in-progress `image.env` typo blocked a
    different worker's `--tasks <unrelated>` gate, and the printed error named
    only the sibling. Refusals are collected and judged once, against three
    facts -- was a selection given, does it name this directory, is this
    manifest tracked in the enclosing revision -- because each answers a
    different question. The selection says what the operator asked for;
    tracked-ness says whether the task set's own revision is broken or whether
    this is work in progress in a tree that has no revision for a record to
    name (`task_set_commit` is then `""`, never `"<sha>-dirty"` -- a scratch
    task set is typically not a git repository at all).

    A refused directory is matched against `only` by DIRECTORY NAME, because a
    manifest that did not load has no readable task_id. `load_task` does not
    require the two to agree, so a refused `foo/` could declare `task_id: bar`
    and a `--tasks bar` selection would proceed past it. That cannot pass
    quietly: `bar` is then missing from the loaded ids and the "no such task"
    refusal below names every surviving refusal as a directory it could not
    check.

    `only` is read for truthiness, not for `is not None`, exactly as before:
    an empty selection has always meant "no selection", and `run_matrix.py`
    passes `None` for an absent `--tasks`.
    """
    root = Path(root)
    if not root.is_dir():
        raise TaskError(f"{root}: no such task set")
    commit = task_set_commit(root)
    tasks: list[TaskManifest] = []
    refusals: list[RefusedManifest] = []
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (task_dir / MANIFEST_NAME).exists():
            continue
        try:
            tasks.append(load_task(task_dir, set_commit=commit))
        except Exception as exc:  # noqa: BLE001 - one manifest, not the set
            # Broad on purpose. `load_task` raises TaskError for everything it
            # checks, but `yaml.safe_load` raises `yaml.YAMLError` and an
            # unreadable file raises OSError -- neither is a TaskError, so a
            # sibling with malformed YAML used to reach the operator as a
            # traceback straight through both drivers' `except TaskError`.
            # Nothing gets quieter: every collected refusal is re-raised as a
            # TaskError below unless it is provably skippable.
            refusals.append(RefusedManifest(
                directory=task_dir,
                error=(str(exc) if isinstance(exc, TaskError) else
                       f"{task_dir / MANIFEST_NAME}: "
                       f"{type(exc).__module__}.{type(exc).__name__}: {exc}"),
                # Only asked when the set has a revision at all -- see
                # `_manifest_committed`'s docstring for why the empty-commit
                # case must not reach it.
                committed=(bool(commit)
                           and _manifest_committed(root, task_dir)),
            ))

    wanted = set(only) if only else None
    fatal = [r for r in refusals
             if wanted is None or r.committed or r.directory.name in wanted]
    if fatal:
        raise TaskError(
            _refusal_report(root, fatal, wanted, len(refusals) - len(fatal))
        )

    if wanted is not None:
        known = {task.task_id for task in tasks}
        missing = wanted - known
        if missing:
            unchecked = ""
            if refusals:
                unchecked = (
                    f"; {len(refusals)} manifest(s) here did not load and were "
                    "matched against the selection by DIRECTORY NAME only, so "
                    "one of them may be the task you asked for: "
                    + ", ".join(str(r.directory) for r in refusals)
                )
            raise TaskError(
                f"no such task(s) in {root}: "
                f"{', '.join(sorted(missing))}{unchecked}"
            )
        tasks = [task for task in tasks if task.task_id in wanted]

    seen: set[str] = set()
    for task in tasks:
        if task.task_id in seen:
            raise TaskError(f"duplicate task_id {task.task_id!r} in {root}")
        seen.add(task.task_id)
    if not tasks:
        raise TaskError(f"{root}: no tasks")
    return tasks, refusals


def load_task_set(root: Path, only: list[str] | None = None
                  ) -> list[TaskManifest]:
    """`load_task_set_with_refusals` for a caller with nothing to skip.

    Signature and return type are unchanged, so `scripts/grade.py` and
    `scripts/judge.py` keep their call sites and inherit the refusal text
    without an edit. Both pass no selection, which is the branch on which
    `refusals` is empty by construction: a caller that named no subset is
    asking for the whole set, and every manifest in it is required.
    """
    tasks, _ = load_task_set_with_refusals(root, only)
    return tasks
```

**Ordering inside the function is load-bearing and must be transcribed as written.** The `fatal` raise comes *before* the `missing` check, so a selected broken manifest reports its own load error rather than "no such task". The duplicate check stays *after* the filter, exactly as today — a duplicate id among unselected tasks is not detected under a selection, which is unchanged behaviour and is listed in "what this does NOT do".

---

## Task 2: `run_matrix.py` — the one caller with a selection

- [ ] **2.1** In `bakeoff/scripts/run_matrix.py`, in the `from bakeoff.tasks import ...` block at line ~74: add `load_task_set_with_refusals` and `refusal_warnings`, and **remove `load_task_set`**. This is a decision, not a judgment call to re-derive: `load_task_set` is referenced in that file at exactly two places, the import at `:74` and the call at `:426`, and 2.2 replaces the call. Leaving the import in place would be the quieter defect — `tests/test_run_matrix.py:213` monkeypatches that symbol, and with the import kept the patch would target a name `main` no longer calls, so the real loader would run against `DEFAULT_TASK_SET = REPO / "taskset"` (touching the filesystem and git) while the test still passed, because `prepare_bases` is separately patched to raise. A test about ordering that has silently stopped replacing its outside edge. Removing the import makes 2.4 mandatory instead of optional, which is the point.

- [ ] **2.2** Replace the load in `main` (currently line 426):

```python
    selected = args.tasks.split(",") if args.tasks else None
    try:
        tasks, refusals = load_task_set_with_refusals(
            Path(args.task_set), only=selected
        )
    except TaskError as exc:
        print(f"task set: {exc}")
        return 1
```

- [ ] **2.3** Print the warnings immediately after the existing task-set banner line (`print(f"task set  {args.task_set}  ({len(tasks)} task(s), commit ...")`), before `prepare_bases`. Nothing has been built and nothing has been spent at that point, and the banner has already named the root the warning refers to.

```python
    for line in refusal_warnings(refusals, root=Path(args.task_set),
                                 selected=set(selected or ())):
        print(line)
```

`refusals` is non-empty only when `selected` is not None, so `selected or ()` is total.

- [ ] **2.4** Update the one existing test that binds the removed symbol. `bakeoff/tests/test_run_matrix.py:212-215`, inside `test_main_stops_before_any_task_image_when_the_bases_disagree`, currently reads:

```python
    monkeypatch.setattr(
        rm, "load_task_set",
        lambda path, only=None: [_PyTask("a", "3.11"), _PyTask("b", "3.12")],
    )
```

Replace with:

```python
    monkeypatch.setattr(
        rm, "load_task_set_with_refusals",
        lambda path, only=None: ([_PyTask("a", "3.11"), _PyTask("b", "3.12")], []),
    )
```

`monkeypatch.setattr` on an attribute a module does not have raises `AttributeError` unless `raising=False` is passed, so skipping this step turns that test into an error rather than a silent pass. No other test in the suite binds `run_matrix.load_task_set` (the twelve `test_judge_script.py` patches bind `scripts.judge.load_task_set`, which is unchanged).

---

## Task 3: tests

All in `bakeoff/tests/test_tasks.py` unless stated. Fixtures already in that module: `upstream`, `_write_task(root, upstream, name=..., **overrides)`, `_manifest`'s `task_id` and `extra_yaml` override keys, and `_sh`.

- [ ] **3.1** Add two module-level helpers next to `_write_task`:

```python
_BROKEN_IMAGE_ENV = (
    "image:\n"
    "  env:\n"
    "    SETUPTOOLS_SCM_PRETEND_VERSION: '1.0'\n"
)


def _write_broken_task(root: Path, upstream, name="t-002") -> Path:
    """A sibling that refuses exactly the way the 2026-09-02 measurement did.

    The `image.env` allowlist is the real defect the probe hit; any refusal
    would exercise the collector, but this one keeps the test and the report
    describing one thing. `task_id` is set to the directory name because
    `_manifest`'s default is a fixed `t-001` and these tests turn on the
    selection matching directories by name.
    """
    return _write_task(root, upstream, name=name, task_id=name,
                       extra_yaml=_BROKEN_IMAGE_ENV)


def _git_task_set(root: Path, *, commit: str = "tasks") -> None:
    """Make the task-set directory a git repository with everything in it
    committed. Separate from the `upstream` fixture's repo: this one is the
    SET, and `_manifest_committed` reads its status."""
    _sh("git", "init", "-q", cwd=root)
    _sh("git", "config", "user.email", "t@t.test", cwd=root)
    _sh("git", "config", "user.name", "t", cwd=root)
    _sh("git", "add", "-A", cwd=root)
    _sh("git", "commit", "-q", "-m", commit, cwd=root)
```

**Every task directory in these tests is written with an explicit `task_id=` matching its directory name.** `_manifest`'s default `task_id` is the fixed string `"t-001"` regardless of the `name=` that sets the directory (`tests/test_tasks.py:190`, `:237-244`), which is exactly why `test_a_duplicate_task_id_is_refused_at_load` exists. A second valid sibling written as `_write_task(root, upstream, name="t-003")` would declare `task_id: t-001`, so `only=["t-001", "t-003"]` would raise `no such task(s) in …: t-003` before any assertion in the test ran. `_write_broken_task` already sets it; the valid ones must too.

- [ ] **3.2** `test_a_tasks_selection_loads_past_an_uncommitted_broken_sibling` — *the item's headline test.* Root is `tmp_path / "set"` (**not** a git repository, mirroring `~/.cache/bakeoff-probe/taskset/`), holding `_write_task(root, upstream, name="t-001", task_id="t-001")` and `_write_broken_task(root, upstream)`. Asserts `load_task_set_with_refusals(root, only=["t-001"])` returns exactly one task whose `task_id == "t-001"`, and exactly one refusal whose `.directory.name == "t-002"`, whose `.committed is False`, and whose `.error` contains `"is not an allowed image.env key"`. Docstring cites the measurement.

- [ ] **3.3** `test_a_broken_manifest_that_is_selected_still_refuses` — same root, `only=["t-002"]`. `pytest.raises(TaskError)`; the message contains `"SELECTED by --tasks as 't-002'"` **and** `"is not an allowed image.env key"`. The text assertion is load-bearing, not decoration: with the selection term dropped from `fatal`, `fatal` is empty, the load falls through to the `missing` check and raises `"no such task(s)"` — still a `TaskError`, so a bare `pytest.raises(TaskError)` would pass over the mutation this test anchors (anchor 1).

- [ ] **3.4** `test_no_tasks_selection_refuses_on_any_invalid_manifest` — same root, `only=None`. Raises; message contains `"no --tasks selection was given"`, `"task_set_commit"`, and the sibling's own error. A second assertion drives the same root through the plain `load_task_set(root)` to pin that `grade.py`'s and `judge.py`'s unedited call sites still refuse.

- [ ] **3.5** `test_the_refusal_text_names_the_sibling_and_the_selected_tasks` — *"the error text names both."* Root (not a git repository) is built with exactly these three calls:

```python
    _write_task(root, upstream, name="t-001", task_id="t-001")
    _write_task(root, upstream, name="t-003", task_id="t-003")
    _write_broken_task(root, upstream)
```

  Two assertions:
  1. the *warning* path (`only=["t-001", "t-003"]`): `refusal_warnings(refusals, root=root, selected={"t-001", "t-003"})` produces lines containing the absolute path of `t-002/task.yaml`, the literal `"None of them is a selected task (t-001, t-003)"`, and the sentence saying a run without `--tasks` still refuses;
  2. the *refusal* path (`only=["t-002"]`): the message names both the manifest path and that it is the selected task.

- [ ] **3.6** `test_a_committed_broken_sibling_refuses_even_under_a_selection` — root holds `_write_task(root, upstream, name="t-001", task_id="t-001")` and `_write_broken_task(root, upstream)`, then `_git_task_set(root)` so **both** directories are committed, then `only=["t-001"]`. Raises; the message contains `"committed, and NOT the task you selected (t-001)"` **and** the sibling's own `image.env` error. The selection half of that assertion is the item's remedy (b) on the one branch this change creates — a curated committed task set hits it every time, and without the selection in the text it reproduces the operator experience the probe reported: a message about a sibling, with no mention of what was asked for. Docstring carries M4: a committed set that does not load whole would otherwise reach the end of a collection and surface only at `grade.py`, at exit 0.

- [ ] **3.7** `test_an_untracked_broken_sibling_warns_inside_a_git_task_set` — the pair to 3.6, and the one that catches D3's pathspec trap: write and commit only `t-001` (`_git_task_set` before the broken sibling exists), then write the broken `t-002`, then `only=["t-001"]`. Returns one task and one refusal with `.committed is False`. Without the resolved pathspec this test fails, because `git status` would match nothing and report the untracked directory as committed.

- [ ] **3.8** `test_an_ignored_task_set_inside_a_repo_is_not_committed` — finding 2's measurement, and the anchor for the `ls-files` term. `git init` a repo at `tmp_path / "outer"`, write `.gitignore` holding `scratch/`, commit it, then build the task set at `tmp_path / "outer" / "scratch" / "taskset"` with `_write_task(..., name="t-001", task_id="t-001")` and `_write_broken_task(...)`. Asserts `load_task_set_with_refusals(root, only=["t-001"])` returns one task and one refusal with `.committed is False`. Docstring: `task_set_commit` returns the **enclosing** repo's HEAD here, so the empty-commit guard does not fire; `git status --porcelain` says nothing about an ignored path and reads as committed; only `git ls-files --error-unmatch` reports the truth. With the `ls-files` term removed this test fails with a `TaskError` tagged `committed`, which is the whole point of it.

- [ ] **3.9** `test_a_modified_broken_manifest_in_a_committed_set_is_not_committed` — the branch the `status --porcelain` half now decides **on its own**, and the ordinary drafting loop: editing an existing task rather than adding a new one. Build the root with `_write_task(root, upstream, name="t-001", task_id="t-001")` and a **valid** `_write_task(root, upstream, name="t-002", task_id="t-002")`, `_git_task_set(root)` so both are committed, then rewrite `t-002/task.yaml` with `_BROKEN_IMAGE_ENV` appended to what `_manifest` produces. `only=["t-001"]` returns one task and one refusal with `.committed is False` — tracked, but modified, so it is not part of the revision as committed. 3.6 pins tracked-and-clean, 3.7 untracked-inside-a-git-set, 3.8 ignored-inside-a-repo; without this one a refactor that dropped the `status --porcelain` half entirely would keep every other test in this plan green.

- [ ] **3.10** `test_a_manifest_that_is_not_valid_yaml_is_a_refusal_not_a_traceback` — M3. Root holds `_write_task(root, upstream, name="t-001", task_id="t-001")` and a `t-002/task.yaml` written directly as `"task_id: [\n"` (unterminated flow sequence; no `reference.diff` needed, the load never gets that far). With `only=["t-001"]` it is collected as a refusal whose `.error` contains the manifest path and the literal `"yaml.parser.ParserError"`; with `only=None` `load_task_set_with_refusals` raises `TaskError` — **not** `yaml.YAMLError` — which is what both drivers' `except TaskError` catches. The asserted string is the *qualified* name on purpose: `type(exc).__name__` for this input is `ParserError`, and `YAMLError` (the base class) is never the concrete type, so asserting `"YAMLError"` would fail against the plan's own formatter.

- [ ] **3.11** `test_a_selected_id_no_directory_supplies_names_the_refused_manifests` — D8. Root holds valid `t-001` and broken `t-002`; `only=["t-999"]`. Raises; message contains `"no such task(s)"`, `"t-999"`, `"DIRECTORY NAME only"`, and the path of the refused directory.

- [ ] **3.12** In `bakeoff/tests/test_run_matrix.py`, `test_run_matrix_prints_the_unselected_refusals_as_warnings` — monkeypatch `rm.load_task_set_with_refusals` to return `([_PyTask("a", "3.11")], [tasks.RefusedManifest(directory=tmp_path / "set" / "b", error="…: is not an allowed image.env key", committed=False)])`, monkeypatch `rm.prepare_bases` to raise `rm.ImageError("stop")` so `main` returns 1 immediately after the banner, run with `--preflight-only --tasks a`, and assert `capsys` output contains `"WARNING"`, `"b"` and the sibling's error. Modelled on `test_main_stops_before_any_task_image_when_the_bases_disagree` (line 203).

- [ ] **3.13** `test_run_matrix_stops_before_any_image_when_a_selected_manifest_is_broken` — monkeypatch `rm.load_task_set_with_refusals` to raise `TaskError("… SELECTED by --tasks as 'a' …")`, monkeypatch `rm.prepare_bases` and `rm.resolve_tasks` to sentinels that fail the test if reached, assert `main() == 1` and that the printed line starts `task set: `. Pins that the refusal is still before any image build.

---

## Task 4: mutation anchors

- [ ] **4.1** Add five entries to `MUTATIONS` in `bakeoff/scripts/mutation_check.py`, in the `src/bakeoff/tasks.py` group. Tuple shape is `(label, file, find, replace, test selector, marker)`; the marker is `"not integration"` for all five. The `find` strings are transcribed from the code written in Task 1 — if the implementer reformats a line, the anchor must be updated to match, and `mutation_check.py` fails loudly on a stale anchor rather than silently passing.

| # | label | find | replace | selector |
|---|---|---|---|---|
| 1 | `task set: let a selected broken manifest through as a warning` | `    fatal = [r for r in refusals\n             if wanted is None or r.committed or r.directory.name in wanted]` | `    fatal = [r for r in refusals\n             if wanted is None or r.committed]` | `tests/test_tasks.py -k broken_manifest_that_is_selected` |
| 2 | `task set: treat a committed broken sibling as work in progress` | `                committed=(bool(commit)\n                           and _manifest_committed(root, task_dir)),` | `                committed=False,` | `tests/test_tasks.py -k committed_broken_sibling` |
| 3 | `task set: let the full-set path proceed past a manifest that did not load` | `    fatal = [r for r in refusals\n             if wanted is None or r.committed or r.directory.name in wanted]` | `    fatal = [r for r in refusals\n             if wanted is not None and (r.committed or r.directory.name in wanted)]` | `tests/test_tasks.py -k no_tasks_selection_refuses` |
| 4 | `task set: judge committed-ness from git status alone, refusing an ignored drafting directory` | `    if not tracked:\n        return False` | `    if not tracked:\n        pass` | `tests/test_tasks.py -k ignored_task_set_inside_a_repo` |
| 5 | `task set: call a tracked-but-modified manifest committed, refusing the ordinary drafting edit` | `    return not status` | `    return True` | `tests/test_tasks.py -k modified_broken_manifest` |

Anchor 3's replacement is deliberately *not* `if r.committed or r.directory.name in wanted`: that raises `TypeError` on `in None` and the test goes red for the wrong reason, which proves nothing about the guard. The form in the table keeps the expression total and removes only the "no selection means the whole set" rule.

Anchors 1 and 3 mutate the same line differently, which `mutation_check.py` applies one at a time (`original.replace(find, replace, 1)`, restored in a `finally`) — confirm both are listed and both are counted, since a copy-paste that leaves two identical `find`/`replace` pairs would still print green.

Anchor 4's `replace` is `pass` rather than deleting the branch, so the mutated code stays syntactically valid and falls through to the `git status` half — which is precisely the "status alone" predicate finding 2 measured as wrong. Under it, 3.8's ignored directory reads as committed and the test raises instead of returning.

Anchor 5 covers the other half of the predicate: `return not status` → `return True` makes every *tracked* directory read as committed regardless of modification, which is the branch `status --porcelain` decides on its own. `    return not status` occurs once in `tasks.py` and is the last line of `_manifest_committed`; confirm that before adding the anchor, since a bare `return not status` elsewhere in the file would make `replace(..., 1)` mutate the wrong site.

- [ ] **4.2** Run `scripts/mutation_check.py` **solo** and confirm the new anchors are counted and green (`N+5` of `N+5`).

---

## Task 5: docs

- [ ] **5.1** `docs/BUILDING-A-TASK-SET.md` §1.5, after "**Commit the task set before every collection run.**", add one sentence tying the new rule to the one already there:

> That rule now has teeth in the loader: a manifest that does not load is fatal to every command over that directory once it is **tracked in the enclosing repository**, because that revision is what a record's `task_set_commit` names. Only work in progress can be skipped — a directory that is untracked, ignored, or in no repository at all — and only by a `--tasks` selection that does not name it.

- [ ] **5.2** `docs/BUILDING-A-TASK-SET.md` §3.6, after "Drop `--tasks` to gate the whole set at once.", add:

> **Drafting beside other tasks.** The loader validates every manifest under `--task-set`, not just the ones `--tasks` names — that is what makes the whole set loadable before a collection. With a `--tasks` selection, a sibling that does not load is downgraded to a `WARNING` naming the offending `task.yaml`, provided it is not itself selected and is not *tracked* in this directory's revision (untracked, ignored, or in a directory that is not a git repository at all). A manifest that **is** tracked and does not load refuses whatever you select, because the set's own revision is then broken and `grade.py` — which has no `--tasks` — will refuse to grade anything collected against it. Measured 2026-09-02: before this rule, one worker's in-progress `image.env` typo blocked every other worker's gate in a shared directory, and the error named only the sibling.
>
> **The warning is the only record of the skip.** Nothing in the run record says manifests were skipped: a scratch task set that is not a git repository records `task_set_commit` as `""` — the honest blank meaning this result is not re-derivable against a revision — and a warned run looks like any other run over such a directory. Keep the driver's output if you need to reconstruct what a drafting run did, and gate the whole set before you commit it.

- [ ] **5.3** `docs/BUILDING-A-TASK-SET.md` §9, add a bullet:

> - **The set must load whole before a collection, not just the tasks you are running.** `--tasks` is a drafting affordance: it will skip an untracked sibling that does not load, with a warning that exists only in the driver's output. It will not skip a tracked one, and `grade.py` has no `--tasks` at all — so a set with one broken committed manifest collects fine under `--tasks` and then grades nothing, at exit code 0. Gate the whole set (`--preflight-only`, no `--tasks`) before you commit it.

- [ ] **5.4** `TASKS.md`: strike the item (`load_task_set` validates every manifest…), matching how items 1–3 were struck in this round. Record in one line what was chosen: the third remedy, gated on committed-ness, with the improved error text on every path that still refuses; note that `grade.py` gaining `--tasks` was considered and deferred, with M5 as the reason.

---

## Verification

- [ ] **V1** `cd bakeoff && .venv/bin/python -m pytest tests/ -q` → the recorded baseline count **+12**, same deselected count. Task 2.4 edits one existing test and adds none.
- [ ] **V2** `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py tests/test_run_matrix.py -v` → all green, and the **twelve** new names appear (3.2–3.11 in `test_tasks.py`, 3.12–3.13 in `test_run_matrix.py`), with `test_main_stops_before_any_task_image_when_the_bases_disagree` still green after its 2.4 edit.
- [ ] **V3** `cd bakeoff && .venv/bin/python scripts/mutation_check.py` (**solo**) → baseline **+5**, all green.
- [ ] **V4** `cd bakeoff && .venv/bin/python scripts/verify_logger.py` → `GATE PASSED`. Requires a Docker daemon; if none is available it reports `GATE INCOMPLETE` and exits 1, which is not a pass — say so rather than recording a weaker gate under the same name.
- [ ] **V5 — the measurement, reproduced end to end, offline.** Build a scratch task set that is **not** a git repository, holding a copy of `bakeoff/taskset/click-3360-write-usage-empty-args/` and a sibling directory whose `task.yaml` is that same manifest plus `image:\n  env:\n    SETUPTOOLS_SCM_PRETEND_VERSION: '1.0'`. Then:
  1. `.venv/bin/python scripts/run_matrix.py --preflight-only --task-set <scratch> --tasks click-3360-write-usage-empty-args` → the `WARNING` block names the sibling, and the gate proceeds to preflight (Docker needed for the preflight itself; the warning prints before any build, so the warning half is observable even without a daemon).
  2. the same command with `--tasks <sibling>` → refuses, message tagged `SELECTED by --tasks`.
  3. the same command with no `--tasks` → refuses, message tagged `no --tasks selection was given`.
  4. `git init` the scratch directory, commit everything, repeat (1) → now refuses, tagged `committed:`.
  5. `.venv/bin/python scripts/grade.py --event-log <any existing log under ~/.cache/bakeoff> --taskset <scratch>` on the *uncommitted* scratch set → still refuses (grade.py has no selection), and the message now names the sibling and says why the whole set is required. This is the half of the item that is deliberately not "fixed" — record the exact output in the review log.
  6. **Finding 2's reproduction, and the one an implementer can check without reading the review.** `git init` a wrapper repository around the scratch directory's *parent*, put the scratch directory's name in that repo's `.gitignore`, commit, and re-run (1). It must still **WARN**, not refuse. With `git status` alone as the predicate it refuses, tagged `committed`, on a directory `git ls-files --error-unmatch` says is not tracked at all.
- [ ] **V6** `graphify update .` after the code lands.

---

## What this does NOT do

- **It does not give `grade.py` a task selection.** `grade.py --taskset <dir>` is still blocked by any manifest in that directory that does not load. What changes is the message: it now names every offending manifest at once (not just the first), tags why each is required, and says what to do. D6 records the design that was rejected and why; V5.5 records the behaviour as measured, so the next round can decide with evidence rather than from the report.
- **It does not add a `NotGradedReason` for "manifest invalid".** That would move `GRADER_VERSION`, and it is only needed if `grade.py` is later allowed to proceed past a refusal (D6, M5).
- **It does not detect a duplicate `task_id` among unselected tasks.** The duplicate check still runs after the `only` filter, exactly as today. Two unselected directories declaring one id are found the next time the set is loaded whole.
- **It does not require a manifest's `task_id` to match its directory name.** D8 explains why the selection matches refused directories by name anyway, and how the residual case is caught loudly by the "no such task(s)" refusal.
- **It does not change what a record contains.** No schema field, no preflight verdict, no oracle derivation, no grade line. `PREFLIGHT_VERSION` does not move because a manifest that loads today loads byte-identically after this change.
- **It does not refuse a live `--tasks` run just because a sibling warned** (D10). §7's one-live-cell step is part of the drafting loop, and a second invisible rule behind `--mode live` would be worse than the warning.
- **It does not record the skip anywhere a reader keeps.** The WARNING lives in the driver's stdout and nothing else carries it: a record over a non-repo task set stamps `task_set_commit` as `""` — the honest blank for "not re-derivable against a revision" — and a warned run is indistinguishable from any other run over such a directory. Over the ignored-inside-a-repo layout it is worse than a blank: `task_set_commit` returns the enclosing repository's own clean HEAD, a sha that names a revision the task set is not even part of, so a warned run there reads as an ordinary clean one rather than as a collection with a skipped manifest. It is not fixed here, and the reason is scope rather than cost: the skip is collection-wide, so the right home is a `"refused_manifests"` key in the matrix summary at `run_matrix.py:709-711` (no version constant moves), not a `RunRecord` field (which would move `SCHEMA_VERSION` and repeat one fact per cell). Open question 5 carries it. It is why §3.6's new text tells the operator to keep the driver's output.
- **It does not touch `scripts/judge.py`.** It passes no selection and inherits the improved text through the `load_task_set` wrapper.
- **It does not make the harness watch for manifests appearing mid-run.** The set is read once, at the top of `main`, as it always was.

---

## Open questions

Each stated as a question, with the current answer and its status. Review round 1 ruled on all four; the rulings are folded in and the answers are marked accordingly.

1. **Is committed-ness the right pivot, or is a plain "selected-only + name the sibling" remedy enough?** — **SETTLED: keep the pivot, but only with a predicate that measures it.** The plain remedy is not free: `grade.py` and `judge.py` have no selection at all, so the full-set path is the only path they have, and a committed task set with one broken manifest therefore collects fine under `--tasks` and then grades **nothing at exit code 0** (M5 — `TASK_NOT_FOUND` is a `not_graded` reason and `grade.py:838` fails only on `errors`). `BUILDING-A-TASK-SET.md:126` already draws the committed/uncommitted line; this gate is its first enforcement. Marginal cost: two subprocesses **per refusal** (zero on a healthy load), one dataclass field, one branch in `_fatal_reason`, three of eleven tests, one of four anchors. The condition on keeping it is finding 2's `ls-files` term — a gate whose false answer is "refuse, because this is committed" on an untracked directory is *worse* than no gate, because it re-breaks the workflow the item exists to unblock while asserting a fact git contradicts.

2. **Should `grade.py` gain `--tasks`?** — **DEFERRED, and this is settled for this item.** D6's reason (ii) decides it: every design that lets `grade.py` proceed past an unreadable manifest hits M5, a permanent false accusation in an append-only file at exit 0. Reason (i) — "no selection" *is* the full-set path, which the item's own constraint says must still refuse — carries it independently. Do **not** lean on reason (iii), the `--taskset`-at-your-own-directory escape hatch: it is a hand-maintained second copy of a manifest, which the probe worker himself flagged as a way to gate one file and collect against another. **What would have to be true to revisit:** a `NotGradedReason` distinguishing "manifest invalid" from "task not in the set" — which moves `GRADER_VERSION`, which is why it is a separate item and not a paragraph in this one.

3. **Matching refused directories by directory name (D8) — right, or should `task_id == directory name` be required?** — **SETTLED: by directory name.** A manifest that did not parse has no readable `task_id` and there is nothing else to match on; re-reading the id out of an unparseable file is the best-effort parse the codebase refuses elsewhere. Requiring the two to agree would refuse manifests that load fine today and would break `test_a_duplicate_task_id_is_refused_at_load`, which deliberately writes directories `a` and `b` both declaring `t-001`. The residual hole is closed loudly by the `unchecked` clause on the "no such task(s)" refusal, verified in review to fire and to name the refused directory path.

4. **Is `except Exception` too broad, and does the malformed-YAML fix belong in this item?** — **SETTLED: yes it belongs, and the breadth is right.** The catch is bounded to one directory, every exception is reported with its qualified type, and the `only=None` path re-raises all of them as `TaskError`. Leaving `yaml.YAMLError` outside the collector would mean a sibling with a typo'd `image.env` is skippable while a sibling with a stray bracket still takes the whole gate down — two rules where the operator sees one. Corrected in revision: the reported type is `type(exc).__module__ + "." + type(exc).__name__`, because `YAMLError` is a base class that is never the concrete type (finding 3).

5. **Should a `--tasks` run that warned record *anywhere durable* that manifests were skipped?** — **Answer for this item: nothing.** Not because the only alternative is expensive: the skip is a property of the **collection**, not of a run — every cell in the matrix saw the same skipped siblings, so a `RunRecord` field would store one fact N times *and* move `SCHEMA_VERSION`. The cheap home this item did not take is the matrix summary `run_matrix.py` already writes beside the wire dir (`run_matrix.py:709-711`, `matrix-<stamp>.json`, carrying `seed`, `mode`, `order`, `skipped`, `rows`, `stranded`, `infra`, `proxy_requests`, `credential_window`): a `"refused_manifests"` key there is durable, is per collection, and moves **no version constant at all**. That is where a later item should start. **Revisit trigger:** the first time a drafting run's skipped siblings need reconstructing after the fact — *not* "if a warned collection is found in the stored logs", which is unfalsifiable by construction, since nothing records the warning and that is the whole question.

---

## Self-review notes

- The one place this design can be argued the other way is D1's committed-ness gate. Without it the change is smaller and the drafting workflow is unblocked identically; with it, a committed broken manifest is caught on the very next `--tasks` invocation instead of at the end of a collection. M4 is the argument for keeping it, and 3.6/3.7/3.8/3.9 are the four tests that make it real rather than decorative: they pin tracked-and-clean, untracked-inside-a-git-set, ignored-inside-a-repo and tracked-and-modified, which is every state the predicate can be in. 3.8 fails if the predicate is the obvious one-term version; 3.9 is the only one that fails if the `status` half is dropped.
- `_manifest_committed` is the only new subprocess (two calls, in the worst case). Both run once per **refusal**, which is zero on every healthy load, so the happy path spawns nothing it did not spawn before.
- The `except Exception` in the collector is the widest catch in this change. It is bounded to one directory, every caught exception is reported verbatim with its qualified type name, and the `only=None` path re-raises all of them — so nothing becomes quieter than it is today, and the malformed-YAML case becomes strictly louder (a refusal naming the file, instead of a traceback that names none).
- `_manifest_committed` is called from **inside** the collector's `except` block. That is why it is contractually total: a raise there would chain onto the manifest's own error and escape as an exception no driver catches, which is the defect this change removes, reintroduced one layer up.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

Under **Invariants**:

> - **A refusal is fatal to the work that needs it, and a `--tasks` selection may skip exactly one kind.** `load_task_set` validates every manifest under the root, because that is what makes `task_set_commit` — the revision of the whole directory — a claim about a set that loads. But a refusal is collected, not raised where it is found: measured 2026-09-02 in a shared drafting directory, one worker's in-progress `image.env` typo blocked a different worker's `--tasks <unrelated>` gate and the error named only the sibling. The decision is taken once against three facts. A selection that names the directory refuses; **no** selection refuses; and a manifest **tracked in the enclosing revision** refuses whatever the selection says, because the set's own revision is then broken and `grade.py` — which has no `--tasks` and loads the set whole — grades every record naming that task as `TASK_NOT_FOUND`, *"is not in the loaded task set"*, which is false, permanently, in an append-only file, under a reason that does not even set the exit code. Only work in progress is skippable, and then only with a WARNING that names the manifest, names the selection, and says a run without `--tasks` still refuses. That predicate takes **two** git facts, not one: `git status --porcelain` says nothing about an *ignored* path, so status alone calls an ignored scratch task set inside a repo "committed" and refuses it while `git ls-files --error-unmatch` reports it is not tracked at all — measured, git 2.50.1. Tracked decides first; clean decides second, failing closed. Neither is asked when the set has no commit — not a repo, git unusable, and no commits yet all collapse to one blank, and in all three there is no revision for a record to name. And both carry `task_set_commit`'s pathspec lesson: a relative pathspec plus `cwd=root` matches nothing, and every directory reads as committed.

---

## Review 1 → changes

Review: `.superpowers/broaden/round2/plan-4-review-1.md` (opus, round 1, REVISE, 8 findings). All 8 adopted; none disputed. Every finding in that review was executed rather than read, and three of them (2, 3, 4) would have stopped an implementer inside the first hour.

**1 — the "every existing test compiles untouched" claim is false; `tests/test_run_matrix.py:213` breaks.** Adopted. The Backwards-compatibility bullet now names the one test that IS edited and lists the call sites that are not. Task 2.1 is no longer a grep-and-decide: it says remove `load_task_set` from `run_matrix.py`'s import, and records why keeping it is the *quieter* defect (the monkeypatch would target a symbol `main` no longer calls, the real loader would run against `DEFAULT_TASK_SET`, and the test would still pass because `prepare_bases` is separately patched to raise). New **Task 2.4** writes out the replacement monkeypatch verbatim and notes that `monkeypatch.setattr` on a missing attribute raises `AttributeError` unless `raising=False`. Test count unaffected.

**2 — `_manifest_committed` does not measure committed-ness for an ignored drafting directory, and fails toward the refusal.** Adopted in full; this was the finding that mattered. `_manifest_committed` is now two terms in order: `git ls-files --error-unmatch` decides *tracked*, and only a tracked path reaches the `git status` *clean* check (which keeps its fail-closed behaviour). D2 carries the measurement — with `scratch/` in `.gitignore`, `task_set_commit` returns the enclosing repo's HEAD so the empty-commit guard does not fire, `git status --porcelain` prints nothing, and status-alone hard-refuses an untracked directory under a message asserting the opposite of the truth. New test **3.8** `test_an_ignored_task_set_inside_a_repo_is_not_committed`, new **mutation anchor 4** on `if not tracked: return False`, and new **V5 step 6** reproducing it from the command line. `refusal_warnings`'s middle clause was softened from "each is uncommitted work in progress" to a claim that is provable — "none is committed in this task set's revision (it has none, or the directory is not tracked there)". D4, the Goal, D1's table, the dataclass docstring, the doc edits and the `CLAUDE.md` sentence all moved from *committed* to *tracked in the enclosing revision*. Q1's boundary ruling — `task_set_commit` names the **enclosing** repository, which for `bakeoff/taskset/` is the harness repo — is now in D4 and in `_manifest_committed`'s docstring.
  *Found while transcribing the fix:* `_manifest_committed` is called from inside the collector's `except` block, so a raise there would chain and escape as an exception no driver catches. The `ls-files` call is wrapped in `except OSError: return True`, and totality is now a stated contract in the docstring and a self-review note.

**3 — the asserted `"YAMLError"` does not appear in the message the plan's own code produces.** Adopted, preferred option. The formatter carries the qualified name (`f"{type(exc).__module__}.{type(exc).__name__}"`), rendering `yaml.parser.ParserError`; D9 is rewritten with the reasoning (the base class is never the concrete type; `ScannerError`, `ComposerError`, `ConstructorError` are the other shapes) and records that builtins render as `builtins.OSError`, accepted for the one rule. Test 3.9 (renumbered from 3.8) asserts `"yaml.parser.ParserError"` and says why the qualified form is asserted. D9 also now records the review's finding that PyYAML's own traceback names **no** file, which is what makes the path prefix load-bearing rather than cosmetic. No anchor covers this line.

**4 — 3.5's second valid task collides on `task_id`.** Adopted. A paragraph at the head of Task 3 states the rule for every test in the section (`_manifest`'s default `task_id` is a fixed `"t-001"` regardless of `name=`, which is why `test_a_duplicate_task_id_is_refused_at_load` exists), and 3.2, 3.5, 3.6, 3.8, 3.9 now write their `_write_task(..., name=X, task_id=X)` calls out. 3.5's three calls are given as a code block.

**5 — the committed-and-unselected refusal never names the selected task, which is literally remedy (b).** Adopted. `_fatal_reason`'s third branch is now `f"committed, and NOT the task you selected ({', '.join(sorted(wanted))}): …"`, with the docstring saying why that branch specifically needs it (a curated committed set hits it every time, and it is the branch this change creates). 3.6 asserts `"committed, and NOT the task you selected (t-001)"`. The related awkwardness the review flagged is fixed too: `_refusal_report` takes a `skippable` count and states it when non-zero, so a header claiming "every one of them is required" cannot silently omit refusals that were skippable.

**6 — D10's justification is false in exactly the case the change is designed for.** Adopted; the ruling (warn-and-proceed is right, the reason was wrong) is taken as given. D10 now rests on §7's one-live-cell step being part of the drafting loop, explicitly retracts the `-dirty` claim with the measurement (`task_set_commit` on a non-repo is `""`, printed as `not a git repo` at `run_matrix.py:440`), and says what the record *does* carry: `""`, the honest blank for "not re-derivable against a revision". No schema field — D7 stays intact. The consequence is stated in a new "what this does NOT do" bullet ("It does not record the skip anywhere a reader keeps"), in §3.6's doc paragraph, and in `refusal_warnings`'s closing line ("This warning is the only place the skip is recorded — no record field carries it"). Carried as open question 5.

**7 — D6 points at an "open questions" section that does not exist.** Adopted. A numbered `## Open questions` section now sits before `## Self-review notes`, with the four reconstructed questions plus the one this revision raised, each with its answer marked SETTLED / DEFERRED / provisional, and the round-1 rulings folded in — including Q2's "what would have to be true to revisit" (a `NotGradedReason` that moves `GRADER_VERSION`) and Q2's instruction not to lean on the escape-hatch argument.

**8 — `judge.py` does not `print`.** Adopted. M2 now says `judge.py:5049` routes the string through `_print_ascii_safe`, records that every string this plan specifies is pure ASCII so the multi-line report passes that guard unchanged, and adds the review's verified note that neither `grade.py`'s `--only` nor `judge.py`'s `--only-task` filters the manifest walk.

**Verification steps updated per the review's closing list:** V1 baseline **+11**, V2 names eleven tests and checks the 2.4-edited test is still green, V3 baseline **+4**, V5 gains step 6. V5.5 unchanged.

**Not disputed, and not changed:** the review's "What checks out" list (M2 line numbers, M3 and M5 reproductions, D3's pathspec measurement, D7's `preflight_cache_key` derivation, anchor transcription-exactness, doc anchor line numbers, the `run_matrix.main` ordering, and seven of eight scenarios producing the asserted text) is taken as confirmation and nothing in it was edited.

---

## Review 2 → changes

Review: `.superpowers/broaden/round2/plan-4-review-1.md`, `# Review 2` (opus, REVISE, 3 open findings; all eight round-1 findings confirmed ADDRESSED, none disputed). The two-term predicate was re-measured against the plan's own extracted code in all four states plus the finding-2 case and answers correctly in every one. All 3 adopted, plus the ruling on open question 5.

**R1 — the retracted `-dirty` claim survives inside `load_task_set_with_refusals`'s docstring, which ships into `tasks.py`.** Adopted. D10 retracts the claim two sections above, but the Task 1.4 docstring an implementer transcribes verbatim still said the refused manifest may be "work in progress in a tree that already stamps `-dirty` on every record it produces". Replaced with the D10 wording: work in progress in a tree that has **no revision for a record to name** (`task_set_commit` is then `""`, never `"<sha>-dirty"` — a scratch task set is typically not a git repository at all). The same sentence's three facts now read "is this manifest **tracked in the enclosing revision**" rather than "committed", matching the predicate the plan actually specifies.

**R2 — the docstring and D4 both justify the caller's empty-commit guard with behaviour the two-term predicate no longer has.** Adopted; this was the sharper of the two, because in a file where docstrings are the specification a maintainer could read the stale justification and delete the `ls-files` term on the strength of it — finding 2, reintroduced. Both places said passing an empty-commit root through would "exit 128 on every call and read as **committed**"; measured against the plan's own code (STATE 4, git 2.50.1) it returns **`False`**, because `ls-files` exits non-zero and the path reads as untracked before the status half runs. Both now say what is true: the `ls-files` term is what covers a non-repo root, the guard is **not** load-bearing for correctness, it is kept as an economy (two subprocesses not spent asking git about a directory in no repository, at the layer that already knows the set has no revision), and the superseded one-term predicate — under which it *was* load-bearing — is named explicitly so the reasoning cannot be mistaken for a live justification.

**R3 — nothing pins "tracked and modified", the most common real drafting shape.** Adopted with the test and anchor as spelled out. New test **3.9** `test_a_modified_broken_manifest_in_a_committed_set_is_not_committed`: both tasks valid and committed via `_git_task_set`, then `t-002/task.yaml` rewritten with `_BROKEN_IMAGE_ENV`, `only=["t-001"]` → one task and one refusal with `.committed is False`. It is the only branch `status --porcelain` decides on its own, and the ordinary drafting loop is editing an existing task rather than adding a new one. New **mutation anchor 5** on `    return not status` → `    return True`, selector `-k modified_broken_manifest`, with a note to confirm that string occurs once in `tasks.py` before adding it (`replace(..., 1)` would otherwise mutate the wrong site). Tests 3.9–3.12 renumbered to 3.10–3.13; the self-review note now names 3.6/3.7/3.8/3.9 as the four states the predicate can be in, and says which test fails for which half.

**Open question 5 — ruling adopted: keep the answer, replace the reasoning.** The review is right that "a `RunRecord` field or nothing" is a false pair. The skip is a property of the **collection**, not of a run — every cell saw the same skipped siblings, so a per-record field stores one fact N times *and* moves `SCHEMA_VERSION`. `run_matrix.py:709-711` already writes `matrix-<stamp>.json` beside the wire dir (`seed`, `mode`, `order`, `skipped`, `rows`, `stranded`, `infra`, `proxy_requests`, `credential_window`); a `"refused_manifests"` key there is durable, per collection, and moves **no version constant at all**. Q5 now says that is where a later item starts, and the revisit trigger changed from "if a warned collection is found in the stored logs" — unfalsifiable by construction, since nothing records the warning — to "the first time a drafting run's skipped siblings need reconstructing after the fact". D10 and the matching "what this does NOT do" bullet were rewritten to agree.

**Counts:** V1 baseline **+12**, V2 twelve new names (3.2–3.11 in `test_tasks.py`, 3.12–3.13 in `test_run_matrix.py`), V3 baseline **+5**. Still no version constant moves.

**Confirmed and unchanged:** the review's two unprompted checks — a tracked directory containing one untracked extra file answers `False` (permissive, consistent with "only a proven revision refuses"), and anchor 4 goes red for the right reason — need no plan change and are recorded here so a later round does not re-derive them.
