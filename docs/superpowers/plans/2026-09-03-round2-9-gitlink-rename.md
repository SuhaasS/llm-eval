# Round 2, item 9: a pure gitlink rename is refused instead of graded against the old content — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a submission whose only change to a submodule is `mv sub newsub` is `NOT GRADED` with `SUBMODULE_GITLINK_UNGRADABLE`, exactly like every other gitlink move, instead of applying green and being graded against a tree whose submodule content never moved. A submission that renames an ordinary file keeps being graded.

**Architecture:** one new authority, and it is the *start state's tree*, not the diff. `_chunk_is_gitlink` stays exactly as it is and stays the pre-materialize gate for every shape that carries a `160000` mode line. A **pure rename** carries no mode line at all — measured below, a 100 %-similar gitlink rename and a 100 %-similar regular-file rename are byte-identical in shape and differ only in their paths — so no diff-only discriminator exists, and the grader asks `git ls-tree <start_sha>` for the mode of the rename **source**. That question needs a tree, so this second branch runs *after* `materialize` and *before* the container. Paths still come from `_chunk_path` (git's own `--numstat -z` parse, forward and `-R`), never from a regex over the `diff --git` line.

**Tech Stack:** Python 3.12 (the harness venv), pytest, real `git` on the host. Every test in this plan is a unit test: no Docker, no network, no credentials. The gitlink fixture repository is built with `git update-index --add --cacheinfo 160000,<sha>,<path>`, which makes a real gitlink entry with no submodule, no clone and no `.gitmodules`.

> **Line numbers in this plan are working-tree-relative as of round-2 item 3 landing (commit `8ab0b08`) and are ADVISORY.** Items 1-8 shift them. Every edit this plan asks for is anchored by quoted text, never by a line number; if a citation and the text disagree, the text wins.

**Spec:** `docs/superpowers/specs/2026-08-17-offline-grader-design.md` (the ladder and the `NotGradedReason` split); `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` §5.6. Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **9**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind. Repo invariants: `CLAUDE.md`, "The harness does not grade, and the grader does not write into the log", "Absence is recorded, never implied", "Silence is the enemy", and `tasks.py`'s "paths come from git, never from a regex over the `diff --git` line".

---

## Global Constraints

- **This item lands AFTER round-2 item 3, which HAS landed** (`8ab0b08`, "a host path mounted once is never mounted again"). It introduced `container.fresh_tree`, rewrote the two lines this plan inserts between (`tree = fresh_tree(...)`, `start_sha = materialize(...)`), and already moved `GRADER_VERSION` from `8` to `9`. **Precondition check, first thing anyway:** `grep -n "fresh_tree" bakeoff/src/bakeoff/grader.py` — it matches at the import and at the two `grade_run` sites. If it returns nothing you are on the wrong revision: **STOP and report**.
- **`fresh_tree` allocates a UUID leaf, and the key-level directory survives as an empty husk BY DESIGN.** `tree` is `grade-tree/<run_id>/<uuid4().hex>` and the `finally` removes the **leaf**; `_sweep_stale_trees` is explicitly documented as *not* collecting a `grade-tree/<run_id>` husk. Nothing in this plan may delete a husk, and no test may assert one is gone (T3.9).
- **`GRADER_VERSION` bumps by ONE from whatever value is on disk when Task 4 starts.** It reads `"9"` at `8ab0b08` (item 3 moved it from `"8"`), and items 4–8 may move it again. **Read the constant and add one; do not hard-code a number.** D6 is the justification.
- **`SCHEMA_VERSION` does NOT move.** No `RunRecord` field is added and none changes meaning. `container.py` is not edited at all — D2 is the argument, and it is the central decision in this plan.
- **`GRADE_SCHEMA_VERSION` does NOT move** and **no `GradeRecord` field is added**. `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE` already exists (added at grade schema 1.2.0) and is reused; only `not_graded_detail`'s text is new. D5.
- **`PREFLIGHT_VERSION` and `ORACLE_VERSION` do NOT move.** `preflight.py` and `oracle.py` are not edited.
- **`_chunk_is_gitlink` and `_GITLINK_MODE` are not edited**, except for the one docstring paragraph this commit makes false (T1.4). Their existing tests must stay green byte-for-byte.
- **The pre-materialize refusal stays pre-materialize.** `test_a_gitlink_submission_is_not_graded_rather_than_failed` and `test_an_agent_created_nested_repo_is_not_graded_either` reach `grade_run` with no Docker and no mirror, and they must keep doing so. The new branch is a *second* site, not a move of the first.
- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode; a comment that says what a line does rather than what breaks without it does not fit here. Claims about external behaviour carry what they were verified against (`git 2.50.1`, and the date).
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section.
- **Commit hygiene:** one commit (plan + code + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Do not run `scripts/mutation_check.py`** concurrently with anything else; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — record the number it prints. Items 1–8 land first, so it will not be the round's opening `1539 passed, 62 deselected`.

---

## The measured defect

**Source:** `TASKS.md`, "**A pure gitlink rename is invisible to the grader's gitlink refusal.**" — find it with `grep -n 'pure gitlink rename' TASKS.md`. Recorded there as *not* measured on a real submission. It is measured now.

All measurements below were taken 2026-09-02 with **git 2.50.1 (Apple Git-155)**, in a scratch superproject under `$HOME/.cache/bakeoff-scratch/gitlink-rename/`: a repository with `file.py`, `big.py` (20 lines) and a submodule at `sub` whose commit is `8d8cfe7b9864c69193d190b7e3c3df9bbddd911a`.

**M1 — the harness's own final-diff command emits the invisible shape.** `container.RunContainer.snapshot_diff` — find it by name, not by line — runs, verbatim:

```
git diff --cached <base_sha>
```

with **no `-M`, no `--no-renames`**, and the run tree sets **no `diff.renames`** (`git config --get diff.renames` in a materialized tree returns nothing; the global config does not set it either). Since git 2.9 the porcelain default for `diff.renames` is **true**, so rename detection is ON. Measured, `git mv sub newsub` then `git diff --cached <base>` produced:

```
diff --git a/.gitmodules b/.gitmodules
index b996bc0..1469971 100644
--- a/.gitmodules
+++ b/.gitmodules
@@ -1,3 +1,3 @@
 [submodule "sub"]
-	path = sub
+	path = newsub
 	url = ../subrepo
diff --git a/sub b/newsub
similarity index 100%
rename from sub
rename to newsub
```

The output of `git diff --cached <base>` was **byte-identical to `git diff --cached -M <base>`**. **The gap is reachable with the command the harness actually runs.** The item is not closed by a test that pins `--no-renames`; there is nothing to pin.

**M2 — the rename chunk carries no mode line, so `_chunk_is_gitlink` returns `False`.** The gitlink chunk is four lines: `diff --git`, `similarity index 100%`, `rename from`, `rename to`. No `new file mode`, no `deleted file mode`, no `old mode`/`new mode`, and **no `index <a>..<b> <mode>` line at all** — `grader._GITLINK_MODE` matches none of them. There is also no hunk body, so the `Subproject commit` authority the docstring rejects would not have seen it either.

**M3 — the docstring's mitigation is wrong in both halves.** `_chunk_is_gitlink`'s docstring (`grader.py:673-681`) says the shape "is out of reach of an agent working in a run tree — renaming a submodule means editing `.gitmodules` too, which IS a text chunk the submission carries". Measured:

* The `.gitmodules` chunk is mode **100644**. `_chunk_is_gitlink` is evaluated **per chunk**, and `_gitlinks_touched` collects paths only from chunks it returns `True` for — so a text chunk beside the rename triggers nothing. Carrying the chunk is not the same as refusing the submission.
* And the `.gitmodules` chunk need not exist. A **plain `mv`** rather than `git mv`, followed by `git add -A`, produced exactly **one** chunk and nothing else:

```
diff --git a/sub b/newsub
similarity index 100%
rename from sub
rename to newsub
```

  (`git add -A` printed only the "adding embedded git repository" advice.) An agent that moves a directory with the Bash tool, which is the ordinary way an agent moves a directory, produces the bare shape.

**M4 — applying it is exactly the harm the refusal exists to prevent.** On a fresh clone checked out at the same base with the submodule initialised, `git apply --index` of that four-line chunk:

```
warning: unable to rmdir 'sub': Directory not empty
exit=0
git ls-files -s   ->  160000 8d8cfe7b... 0  newsub
ls                ->  newsub  sub
ls newsub         ->  (empty)
```

Exit **0**. The index entry moved to `newsub`; the working tree keeps the fully populated **old** `sub/` and gains an **empty directory** at `newsub`. The ladder then runs the suite against a tree where the agent's move did not happen, and `resolved: False` — an accusation — is the likely verdict. This is the same shape `_gitlinks_touched`'s docstring records for the commit-inside-a-submodule and nested-repo cases, reached by a path that check cannot see.

**M5 — there is no diff-only discriminator.** A 100 %-similar rename of an **ordinary file** produces the identical shape:

```
diff --git a/big.py b/moved_big.py
similarity index 100%
rename from big.py
rename to moved_big.py
```

Same four lines, no mode, no index line, no body. The two cases differ **only in their paths**, and a path's mode is not in the diff. Any rule that refuses on the shape alone also refuses every honest file rename.

**M6 — the neighbouring shapes are already covered, and this is why only the pure case needs work.**

| what the agent did | what git emitted | seen by `_chunk_is_gitlink` today |
|---|---|---|
| `git mv sub newsub` (commit unchanged) | `similarity index 100%` + rename, no mode | **no** — this item |
| plain `mv sub newsub` + `git add -A` | the same four lines, no `.gitmodules` chunk | **no** — this item |
| rename **and** a new submodule commit | NOT detected as a rename: `deleted file mode 160000` at `sub` + `new file mode 160000` at `newsub` | yes |
| 89 %-similar regular-file rename | `similarity index 89%` + `index f696b4b..5e66f82 100644` | no (correctly — mode 100644) |
| commit inside the submodule, no move | `index 942c381..c464218 160000` | yes |
| `git init` in a tracked subdirectory | `new file mode 160000` | yes |

A gitlink's content is a single `Subproject commit <sha>` line, so a gitlink rename is either 100 % similar (the commit did not change) or not similar enough to be paired at all. **The pure rename is the whole of the gap**, and a rename chunk that does carry a mode line (rename + chmod) is already refused before this plan's branch is reached.

**M7 — the exposure on stored rows is zero today, and that is a measurement, not an assumption.** All 115 records under `~/.cache/bakeoff/eventlog*/` and `~/.cache/bakeoff/trucking*/` were scanned (2026-09-02) for `"\nrename from "` in `artifacts.final_diff`: **0 records carry a rename chunk of any kind**; 1 record carries the string `160000` (`trucking-dry3/runs/71212309ce6538ea.json`, the nested-repo dry-run shape the existing refusal already catches). So no stored verdict changes when this lands — and equally, no stored row is protected by anything the harness could be changed to do differently in future (D2).

**M8 — reachability is real on the corpus this branch is building.** A pure gitlink rename requires a gitlink **at `start_sha`** (an agent-created nested repository is a `new file mode`, already refused). `tomlkit-514-inline-table-comment-separator` in `~/.cache/bakeoff-probe/taskset/` carries a submodule at `tests/toml-test` — under a declared test prefix, which is the layout item 7 of the previous wave already fixed `_check_test_restore` for. An agent that moves or renames that directory (`tests/toml-test` → `tests/toml_test` is the obvious "make it importable" move) hits this exactly.

---

## Design decisions, settled

### D1. The authority is the start state's tree, consulted only for rename sources

The mode of a path at `start_sha` is the one fact that separates M2 from M5, and it is not in the diff (M5). `git ls-tree -r -z <start_sha> -- <source paths>` answers it directly, and `grader._ls_tree_gitlinks` (`grader.py:880`) already parses exactly that reply — same command, same `-z` framing, same `160000` test — for `_check_test_restore`. Reusing it means one parser for one question.

The **source** side is queried, never the destination: a rename's destination does not exist at `start_sha` by construction, and its source always does, because `final_diff` is `git diff --cached <base_sha>` in the run tree and the grader materializes at that same `start_sha`.

A path absent from the reply is **not** a gitlink and is not refused. `git ls-tree` with a pathspec matching nothing exits **0** with empty output (measured), so absence is indistinguishable from "an ordinary file that ls-tree simply does not report as `160000`" — and both answers are the same answer. A *failed* listing is different and raises (D4).

### D2. `container.snapshot_diff` is not the fix for THIS, and the two changes are not a fork

The alternative measured: pass `--no-renames` to `snapshot_diff`, so every gitlink change carries a mode line and `_chunk_is_gitlink` is complete on its own. It works — `git diff --cached --no-renames <base>` turns M1's rename into `deleted file mode 160000` at `sub` plus `new file mode 160000` at `newsub`, which the existing regex catches.

**It is not rejected as the worse recording. It is insufficient as the fix, for one argument in two halves:**

1. **It cannot reach a row that is already written, and the log is append-only.** The grader is an offline batch whose soundness has to hold over *any* stored record. All 115 stored rows were captured renames-on (M7) and would stay permanently blind to a mode-line-only authority under every future `GRADER_VERSION`. No capture-side flag is retroactive.
2. **It would make the grader's soundness depend, invisibly, on a flag in another process.** Delete `--no-renames` from `container.py` a year from now and every gitlink rename silently starts grading `False` again. Nothing offline can notice: the grader has no way to tell a renames-off diff from a renames-on diff that happens to contain no rename.

Those two are sufficient on their own, and the grader-side `ls-tree` is required **whatever the harness later records** — which is why this is not a fork. A capture-side change would make *future* diffs self-describing; it would not retire this check.

**A third consideration, ranked last because it is the weakest and cuts partly the other way:** changing `snapshot_diff` would be a change to what the harness *records* made for a grader's convenience, which is the wrong justification even for a change that may be right on its own merits. It is only the wrong *justification* — on the merits, `--no-renames` is plausibly the **more honest** record, and the plan does not claim otherwise. The fact in the index is: entry at `sub` deleted, entry at `newsub` added, mode `160000`. `--no-renames` records that; renames-on records a heuristic *interpretation* of it, one that depends on `diff.renames`, `diff.renameLimit` and git's inexact-rename cutoff, none of which the run tree pins — so the recorded bytes can move without anything the agent did moving. And it is measurably lossy: with renames on, `git diff --cached --name-only <base>` reports **only the destination**, so `big.py` and `sub` are simply absent from `files_touched` (measured: 3 names where the index change touches 5). That is "absence recorded, never implied" violated *in the record*.

**So the record question is real, and it is settled on its own evidence in T5.2, not here.** Folding it in would make one commit change both what is measured and what is graded, and would drag `SCHEMA_VERSION` and every diff-size view along with it. Measured cost of that separate change, for whoever picks it up: on one mixed submission (an 85–89 % file rename, a gitlink rename, one edit) the diff went from 3 chunks / 3 names / 400 bytes to 5 / 5 / 863 — a renamed file's content appears twice, as `-` lines in the delete chunk **and** `+` lines in the add chunk.

### D3. The rename branch runs after `materialize` and before `RunContainer`, and that costs one clone per refused row

The existing mode-line refusal runs before everything and costs nothing; it keeps that position. The rename branch cannot: it needs `start_sha`, which only exists once `materialize` has built the setup commit, and it needs a repository to ask.

The cost, stated exactly:

* **A submission with no rename chunk pays no `git ls-tree` and starts no container.** `_renamed_gitlinks` returns `()` from `_rename_pairs` before anything is spawned. It does pay one additional `_parse_submission` — a `TemporaryDirectory` plus two `git apply --numstat` invocations per chunk — which D7 prices and does not wave away. Measured on the stored corpus (M7), that is 115 of 115 rows, ~3 chunks each, so roughly 690 extra short-lived git processes per event-log batch: real, small, and not "nothing".
* **A submission with a rename chunk pays one `git ls-tree`** on the host, in a tree that was going to be built anyway.
* **A row that is refused pays one wasted `materialize`** — a `--local`, hardlinked clone from the already-cached pruned mirror, which `materialize`'s own docstring records as costing "neither time nor disk" at 2,400 repetitions. No container is started, no image is pulled, no oracle is derived. The **leaf** is removed by the existing `finally`; the `grade-tree/<run_id>` key-level directory stays behind as an empty husk, which is `fresh_tree`'s documented and deliberate cost and not a leak this plan introduces.

**And no rearrangement removes that clone.** `start_sha`'s commit is created *by* `materialize`, in the run tree; the pruned mirror is per `(repo, base_sha)` and does not contain it, and the manifest's `declared_start_sha` names a commit no repository holds until a tree is built. So the tree is the earliest place the question can be asked, whatever is computed before it — see D7.

The mirror was considered as a cheaper authority and rejected: the pruned mirror is at **`base_sha`**, and the question is about **`start_sha`** (`base_sha` plus the committed test half plus `strip_paths`). Asking the wrong tree would be right today only because no task strips a submodule path, which is the shape of argument this repository keeps having to unwind.

`grade_run`'s docstring paragraph that says the gitlink refusal "runs before the artifacts wipe and before `materialize`, so a refused row costs no tree and no container" becomes half false and is rewritten in T3.

### D4. A failed listing raises; it does not fall through

`_start_state_gitlinks` raises `TaskError` on a non-zero exit or an `OSError`. `scripts/grade.py:629` wraps every record in `except Exception` and puts it in the `errors` bucket with its type and message, and the batch continues and exits 1 — one named, counted, re-runnable row.

Falling through to "no gitlinks" is the choice that costs something permanent: the fallback for a silent failure here is grading a tree the submission's content never reached, which is `resolved: False` in an append-only file. This is the same argument `_refresh_index` (`grader.py:1917`) and `_check_test_restore`'s `ls-tree` guard (`grader.py:943`) already make, and `TaskError` is the type `tasks._numstat` already raises when a git invocation cannot be run.

### D5. `SUBMODULE_GITLINK_UNGRADABLE` is reused; no new `NotGradedReason`

"A null says which kind of null it is" is about two *different* absences rendering identically. This is the same absence: a gitlink moved in the index and the content did not follow, so no honest verdict exists. The reader who needs the difference gets it from `not_graded_detail`, which names the rename with an arrow (`sub -> newsub`) where the existing message names a plain list of paths. Adding a fifth reason would split one bucket in every offline view for a distinction that changes nothing about what to do with the row.

### D6. `GRADER_VERSION` moves by one

A submission the ladder used to accept and grade — measured, `git apply --index` exits 0 on it (M4) — is now taken out of the denominator. That is a change to what the ladder means on an input it already accepted, which is the same reason 4 → 5 moved for the original gitlink refusal.

It moves **whether or not any stored row hits it**, because `scripts/grade.py`'s resume gate keys on `(run_id, GRADER_VERSION)` alone and never on the code that produced the verdict. It costs a full re-grade into a fresh `v<N>` artifacts directory per event log; **no verdict on today's corpus changes**, because no stored record carries a rename chunk at all (M7). Schedulable, not urgent.

### D7. `_rename_pairs` reads `source != dest` off `_chunk_path`, never off the header

`_chunk_path` runs `git apply --numstat -z` forward and with `-R` per chunk. Measured on the M3 chunk, in a directory outside any repository:

```
forward:  0	0	newsub
reverse:  0	0	sub
```

so the pair is `("sub", "newsub")` and a rename is exactly `source != dest`. `similarity index` / `rename from` / `rename to` are never matched. This is `tasks.py`'s standing rule, and it is not decoration here: `rename from`/`rename to` lines are **not** `-z`-framed, are C-quoted for non-ASCII, and a path containing " b/" makes the `diff --git` line ambiguous — the five silent-wrong-path bugs that module's docstring records.

`_parse_submission` is therefore called a second time per record (once inside `_gitlinks_touched`, once inside `_rename_pairs`). Priced: two `git apply --numstat` invocations per chunk, ~5 ms each, on submissions that are 1–5 chunks — tens of milliseconds per record, ~690 processes across a 115-row batch.

**The alternative actually worth naming, and why it is not taken.** `grade_run` could call `_parse_submission` once, pre-materialize, and hand the list to a `_rename_pairs(parsed)` that takes the parsed list rather than the diff. That changes **no** existing signature and breaks **no** existing test — `_gitlinks_touched` stays exactly as it is. It is declined on its actual cost, which is architectural rather than mechanical: it puts a second consumer of one `_parse_submission` result inside `grade_run` and makes the two refusals share state that today they deliberately do not, so a future change to either has to reason about both. The saving is tens of milliseconds per record.

**What that refactor would NOT buy is the wasted clone.** Having the rename pairs before `materialize` does not let the refusal move before it: the pairs say *that* something was renamed, and M5 measures that a pure rename of an ordinary file is byte-identical in shape, so the pairs alone cannot decide anything. The mode has to come from `start_sha`'s tree, and `start_sha` exists nowhere until `materialize` builds it (the pruned mirror is at `base_sha`; `declared_start_sha` is a name, not a repository). The `ls-tree`, not the parse, is what forces the post-materialize position.

The earlier draft of this section rejected a *different* refactor — threading `parsed` through `_gitlinks_touched` itself — which does break four tests, but which is not the shape a reviewer reaches for. Recorded so the reason on the page matches the alternative it rejects.

### D8. Pathspecs are `:(literal)`-prefixed

A git pathspec is a **pattern** by default, and the sources come from the submission — data, not a constant. Measured: both `-- 'sub[1]'` and `-- ':(literal)sub[1]'` matched a gitlink literally named `sub[1]` at this git version, and `:(literal)` behaves identically on ordinary paths. The decisive case is sharper and was measured in review: a gitlink literally named `:weird` is returned **only** under `:(literal)`; the bare spelling exits **0 with empty output**, which this code would read as "not a gitlink" and let through — a silent false negative, exactly the class the whole item is about. It differs from `_check_test_restore`'s bare `*paths` deliberately: those come from the **manifest**, which a task author writes and preflight checks.

---

## File Structure

```
bakeoff/src/bakeoff/grader.py          # 3 new module functions, 1 branch in grade_run,
                                       #   GRADER_VERSION note, 2 docstring corrections
bakeoff/tests/test_grader.py           # 2 fixtures, 1 helper repo builder,
                                       #   8 new tests + 1 edited (T3.11)
bakeoff/scripts/mutation_check.py      # 1 new anchor (count before, expect +1)
TASKS.md                               # tick the item, record what was measured
tasks/todo.md                          # review-log section
docs/superpowers/plans/2026-09-03-round2-9-gitlink-rename.md   # this file
```

---

## Task 1: the three functions in `grader.py`

- [ ] **T1.1 — `_rename_pairs`.** Place it between `_gitlinks_touched` and `_added_lines` — the two `def` lines are the anchors, not a line range.

```python
def _rename_pairs(diff: str) -> tuple[tuple[str, str], ...]:
    """The `(source, destination)` pairs this submission renames.

    A rename is `source != dest` OFF `_chunk_path`, which runs
    `git apply --numstat -z` forward and with `-R` per chunk. The
    `similarity index` / `rename from` / `rename to` lines are never matched:
    they are not `-z`-framed, they are C-quoted for a non-ASCII path, and a
    path containing " b/" makes the `diff --git` line ambiguous -- the five
    silent-wrong-path bugs `tasks.py`'s module docstring records.

    Measured 2026-09-02, git 2.50.1, on the four-line chunk a submodule
    rename produces: forward `0\t0\tnewsub`, reverse `0\t0\tsub`.

    A submission that cannot be parsed returns `()`, for the same reason
    `_gitlinks_touched` does: `_apply_submission` owns that shape, and a
    second authority for one refusal is the mistake `not_graded_gate`'s
    docstring records.
    """
    try:
        parsed = _parse_submission(diff)
    except TaskError:
        return ()
    return tuple(
        (source, dest) for _chunk, source, dest in parsed if source != dest
    )
```

- [ ] **T1.2 — `_start_state_gitlinks`.** Place it directly after `_rename_pairs`.

```python
def _start_state_gitlinks(repo: Path, start_sha: str,
                          paths: tuple[str, ...]) -> tuple[str, ...]:
    """Which of `paths` are `160000` gitlinks in `start_sha`'s tree.

    On the HOST, in the materialized tree, because the answer comes out of
    the object store and not out of the index -- so none of `_refresh_index`'s
    stat-cache problem applies and no container is needed to ask.

    Pathspecs are `:(literal)`-prefixed: a git pathspec is a PATTERN by
    default and these paths come from the submission, which is data. Measured
    2026-09-02 (git 2.50.1): a gitlink literally named `:weird` is returned
    ONLY under `:(literal)` -- the bare spelling exits 0 with EMPTY OUTPUT,
    which this function would read as "not a gitlink" and let through. A
    silent false negative, which is the whole class of defect this check
    exists to close. `_check_test_restore` passes its paths bare and is right
    to: those come from the manifest, which a task author writes and preflight
    checks.

    RAISES rather than reporting nothing. A pathspec matching no entry is
    exit 0 with empty output (measured), which is a real answer -- "not a
    gitlink" -- and is left alone. A non-zero exit is not: the fallback for a
    silent failure here is grading a tree the submission's content never
    reached, which is `resolved: False` in an append-only file.
    `scripts/grade.py` contains this per record into its `errors` bucket, so
    the cost of being wrong is one named, re-runnable row.
    """
    argv = ["git", "ls-tree", "-r", "-z", start_sha, "--",
            *(f":(literal){p}" for p in paths)]
    try:
        proc = subprocess.run(argv, cwd=repo, capture_output=True)
    except OSError as exc:
        raise TaskError(f"could not run {' '.join(argv)}: {exc}") from exc
    if proc.returncode != 0:
        raise TaskError(
            "could not read the start state's file modes while checking a "
            f"renamed path for a gitlink (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return _ls_tree_gitlinks(proc.stdout.decode("utf-8", "surrogateescape"))
```

  `_ls_tree_gitlinks` is defined **below** this point in the file (`grader.py:880`); that is fine, the call is at run time. Do not move it.

- [ ] **T1.3 — `_renamed_gitlinks`.** Place it directly after `_start_state_gitlinks`.

```python
def _renamed_gitlinks(diff: str, repo: Path,
                      start_sha: str) -> tuple[tuple[str, str], ...]:
    """The gitlinks this submission MOVES without changing, source -> dest.

    The blind spot `_chunk_is_gitlink` cannot close. Measured 2026-09-02,
    git 2.50.1: a 100%-similarity rename carries NO mode line -- not
    `new file mode`, not `deleted file mode`, not `old mode`/`new mode`, and
    not even an `index <a>..<b> <mode>` line -- and no hunk body, so neither
    the mode authority nor the `Subproject commit` one it rejects sees it.
    And a 100%-similarity rename of an ORDINARY file is byte-identical in
    shape:

        diff --git a/sub b/newsub          diff --git a/big.py b/moved_big.py
        similarity index 100%              similarity index 100%
        rename from sub                    rename from big.py
        rename to newsub                   rename to moved_big.py

    The two differ only in their paths, so no diff-only rule can separate
    them: the mode has to come from the tree. Hence `start_sha`, and hence
    this runs after `materialize` rather than beside the other refusal.

    The SOURCE side is what is asked about. A rename's destination does not
    exist at `start_sha`; its source always does, because `final_diff` is
    `git diff --cached <base_sha>` taken in the run tree and the grader
    materializes that same start state.

    Why it matters: `git apply --index` of that four-line chunk exits 0 on a
    freshly materialized tree (`warning: unable to rmdir` only), moves the
    index entry to the new path, and leaves the submodule's files at the OLD
    one with an EMPTY DIRECTORY at the new one -- measured. The ladder then
    grades a tree the agent's move never reached and returns
    `resolved: False`, an accusation over a limitation of the harness's own
    diff capture.

    No rename means no `git ls-tree` at all: measured across the 115 stored
    records under `~/.cache/bakeoff` (2026-09-02), that is every one of them.
    """
    pairs = _rename_pairs(diff)
    if not pairs:
        return ()
    gitlinks = set(_start_state_gitlinks(
        repo, start_sha, tuple(source for source, _dest in pairs)
    ))
    return tuple((s, d) for s, d in pairs if s in gitlinks)
```

- [ ] **T1.4 — correct `_chunk_is_gitlink`'s docstring.** Its final paragraph (`grader.py:673-681`) claims the shape is out of reach and that a `.gitmodules` edit carries it. Both halves are measured false (M3). Replace that paragraph with:

```
    A PURE RENAME of a gitlink is deliberately outside this function. Such a
    chunk carries `similarity index 100%` / `rename from` / `rename to` and
    neither a mode line nor a hunk body, so neither this authority nor the
    `Subproject commit` one can see it -- and a pure rename of an ordinary
    FILE is byte-identical in shape, so nothing in the diff can separate
    them. It is reachable: measured 2026-09-02 (git 2.50.1), a plain
    `mv sub newsub` followed by `git add -A` emits exactly that one chunk and
    NO `.gitmodules` chunk at all, and `git mv`'s `.gitmodules` chunk is mode
    100644 and would not match here anyway. `_renamed_gitlinks` closes it by
    asking `git ls-tree <start_sha>` for the source's mode, which needs a
    tree and therefore runs after `materialize`.
```

---

## Task 2: the branch in `grade_run`

- [ ] **T2.1** Insert the refusal **inside** the existing `try:` block, as its first statement, before `with RunContainer(...)`. Inside the `try` so the existing `finally: shutil.rmtree(tree, ignore_errors=True)` removes the tree on this path too — a refused row must not leave a materialized tree behind.

```python
    start_sha = materialize(task, tree / "repo", Path(cache_root))
    try:
        renamed = _renamed_gitlinks(
            record.artifacts.final_diff or "", tree / "repo", start_sha
        )
        if renamed:
            return build_grade_record(
                record, task, image, oracle, _gated_result((
                    NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE,
                    "the submission renames the gitlink(s) "
                    + ", ".join(f"{s} -> {d}" for s, d in renamed)
                    + "; a 100%-similarity rename carries no mode line and no "
                    "content, so applying it moves the index entry and leaves "
                    "the submodule's files at the old path",
                )),
            )
        with RunContainer(image=image, repo_path=str(tree / "repo"),
                          base_sha=start_sha) as container:
            ...
    finally:
        shutil.rmtree(tree, ignore_errors=True)
```

  `artifacts_dir=` is omitted on this path exactly as it is on the other two gated paths: the wipe above it ran with `ignore_errors=True`, so the directory does not exist and `build_grade_record` records `None` rather than pointing at a pass that wrote nothing.

- [ ] **T2.2** Replace the WHOLE `grade_run` docstring paragraph below, verbatim as it stands on disk (note `--`, not an em dash), with the block that follows it. The replacement subsumes all three sentences, including the first two.

  Text to replace:

```
    The gitlink refusal sits beside it and for the same reason, but it is a
    SEPARATE function rather than a branch of `not_graded_gate`: that gate is
    also called by `run_ladder` and asks only whether a submission EXISTS,
    while this one reads the submission's contents. It runs before the
    artifacts wipe and before `materialize`, so a refused row costs no tree
    and no container -- and, since the authority is the diff rather than the
    task, no mirror clone either.
```

  Replacement:

```
    The gitlink refusal sits beside it and for the same reason, but it is a
    SEPARATE function rather than a branch of `not_graded_gate`: that gate is
    also called by `run_ladder` and asks only whether a submission EXISTS,
    while this one reads the submission's contents.

    It is in TWO PLACES, and the split is a measurement rather than a
    preference. Every shape that carries a `160000` mode line is refused
    before the artifacts wipe and before `materialize`, so those rows cost no
    tree, no container and no mirror clone. A PURE RENAME carries no mode
    line, and a pure rename of an ordinary file is byte-identical to one of a
    gitlink (measured 2026-09-02, git 2.50.1) -- so the mode has to come from
    `start_sha`'s tree, and that branch runs after `materialize` and before
    the container. A refused rename therefore costs one hardlinked `--local`
    clone from the already-cached pruned mirror and nothing else. A
    submission with no rename chunk asks git nothing.
```

---

## Task 3: tests in `tests/test_grader.py`

All of these go in the existing section `# 1b. the gitlink refusal, which runs beside the gates and before the ladder` — rename that banner to `# 1b. the gitlink refusals: the mode-line one before materialize, the rename one after`.

- [ ] **T3.1 — the two fixtures, verbatim from the measurement.** Add beside `_GITLINK_SUBMISSION` and `_NESTED_REPO_SUBMISSION`:

```python
#: Shape 3: a PURE RENAME of a gitlink, which carries no mode line at all.
#: Produced verbatim 2026-09-02 with git 2.50.1, by `mv sub newsub` followed
#: by `git add -A` in a superproject with a submodule at `sub` -- a plain
#: `mv`, so there is no `.gitmodules` chunk beside it. `git diff --cached
#: <base>` and `git diff --cached -M <base>` returned identical bytes, and
#: `container.snapshot_diff` runs the former.
_GITLINK_RENAME_SUBMISSION = (
    "diff --git a/sub b/newsub\n"
    "similarity index 100%\n"
    "rename from sub\n"
    "rename to newsub\n"
)

#: The control, and the reason the tree has to be asked: a 100%-similarity
#: rename of an ORDINARY file is the same four lines with different paths.
#: Produced the same way, by `git mv big.py moved_big.py`.
_FILE_RENAME_SUBMISSION = (
    "diff --git a/big.py b/moved_big.py\n"
    "similarity index 100%\n"
    "rename from big.py\n"
    "rename to moved_big.py\n"
)
```

- [ ] **T3.2 — the hermetic start-state helper.** A real git repository with a real `160000` entry, built without a submodule, a clone or a network:

```python
def _start_state_repo(root: Path) -> str:
    """A real repository whose tree carries a `160000` gitlink at `sub` and a
    blob at `big.py`, and the SHA of the commit holding both.

    `git update-index --add --cacheinfo 160000,<sha>,<path>` writes a gitlink
    entry directly -- no submodule, no clone, no `.gitmodules`, no network --
    so `_start_state_gitlinks` runs against REAL git output rather than a
    string somebody typed. Measured 2026-09-02 (git 2.50.1): the resulting
    `git ls-tree -r -z <sha> -- sub big.py` reply is
    `160000 commit <sha>\\tsub\\0100644 blob <sha>\\tbig.py\\0`.
    """
```

  It runs `git init -q -b main`, writes `big.py`, `git add big.py`, `git update-index --add --cacheinfo 160000,8d8cfe7b9864c69193d190b7e3c3df9bbddd911a,sub`, commits with `-c user.email=` / `-c user.name=` (never the operator's config), and returns `git rev-parse HEAD`.

- [ ] **T3.3 — `test_a_pure_gitlink_rename_is_refused_though_it_carries_no_mode_line`.** Builds `_start_state_repo`, asserts `grader._chunk_is_gitlink(_GITLINK_RENAME_SUBMISSION) is False` — the blind spot, stated as an assertion so the two authorities stay honestly separate — and that `grader._renamed_gitlinks(_GITLINK_RENAME_SUBMISSION, repo, sha) == (("sub", "newsub"),)`.

- [ ] **T3.4 — `test_a_pure_regular_file_rename_is_not_refused`.** Same repository, `_FILE_RENAME_SUBMISSION`, asserts `_renamed_gitlinks(...) == ()`. The docstring says the two fixtures are the same four lines and that a shape-only rule would take every honest rename out of the denominator.

- [ ] **T3.5 — `test_rename_pairs_come_from_git_rather_than_the_rename_header`.** `grader._rename_pairs(_GITLINK_RENAME_SUBMISSION) == (("sub", "newsub"),)`; `grader._rename_pairs(TEXT_DIFF) == ()` (a modification has `source == dest`); `grader._rename_pairs("not a diff at all\n") == ()`.

- [ ] **T3.6 — `test_a_submission_with_no_rename_asks_git_nothing`.** `monkeypatch.setattr(grader, "_start_state_gitlinks", _raiser)` where `_raiser` raises `AssertionError`; `_renamed_gitlinks(TEXT_DIFF, Path("/nonexistent"), START_SHA) == ()`. Pins the zero-cost common path — measured, 115 of 115 stored records.

- [ ] **T3.7 — `test_the_renamed_source_mode_is_read_from_the_start_state`.** Captures the argv by monkeypatching `grader.subprocess.run` with a recorder returning `returncode=0, stdout=b""`; asserts the argv is exactly
  `["git", "ls-tree", "-r", "-z", START_SHA, "--", ":(literal)sub"]`
  and that `cwd` is the repo path. Pins three things at once: the tree is `start_sha`'s and not `base_sha`'s, only the **source** is asked about, and the pathspec is literal.

- [ ] **T3.8 — `test_a_gitlink_rename_is_not_graded_and_starts_no_container`.** Through `grade_run`, in the style of `test_the_stat_cache_is_refreshed_before_the_ladder_applies`. The call is, verbatim:

```python
    graded = grade_run(_record(diff=_GITLINK_RENAME_SUBMISSION), _task(),
                       "sha256:image", _oracle(),
                       tmp_path / "cache", tmp_path / "art")
```

  `_record`'s `diff` defaults to `TEXT_DIFF`, so naming the fixture is what makes the detail assertion hold. Wiring: `monkeypatch.setattr(grader, "materialize", lambda *a, **kw: START_SHA)`, `monkeypatch.setattr(grader, "_start_state_gitlinks", lambda *a, **kw: ("sub",))`, and a `RunContainer` replacement whose `__init__` raises `AssertionError("a refused row must not start a container")`. Asserts `graded.resolved is None`, `graded.not_graded_reason == NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE.value`, `graded.grade_failure is None`, `"sub -> newsub" in graded.not_graded_detail`, and `graded.artifacts_dir is None`.

- [ ] **T3.9 — `test_a_refused_rename_leaves_no_materialized_tree_behind`.** Same wiring and the same `_record(diff=_GITLINK_RENAME_SUBMISSION)` call as T3.8, with the fake `materialize` **capturing the leaf and creating the tree** before it returns:

```python
    leaves: list[Path] = []

    def _fake_materialize(task, dest, cache_root):
        # `dest` is `<leaf>/repo`, so the leaf `fresh_tree` allocated is its
        # parent. There is no other way to learn the name: it is a uuid4.
        Path(dest).mkdir(parents=True)
        leaves.append(Path(dest).parent)
        return START_SHA

    monkeypatch.setattr(grader, "materialize", _fake_materialize)
```

  After `grade_run` returns: `assert leaves and not leaves[0].exists()`.

  **Do NOT assert `not (cache_root / "grade-tree" / record.run_id).exists()`.** Since item 3, `tree = fresh_tree(cache_root / "grade-tree" / <run_id>)` allocates a `uuid4().hex` leaf under that key and the `finally` removes only the leaf; `fresh_tree`'s own docstring says "The key-level directory survives cleanup as an empty husk", and `_sweep_stale_trees` is documented as not collecting a `grade-tree/<run_id>` husk. The key-level directory remaining is correct behaviour, not a leak, and an assertion against it would fail and invite an implementer to "fix" working production code. The leaf is what this test is about, and what it pins is that the refusal sits inside the `try` rather than above it.

- [ ] **T3.10 — `test_a_start_state_listing_that_fails_stops_the_grade`.** `grader.subprocess.run` returns `returncode=128, stderr=b"fatal: not a tree object\n"`; `pytest.raises(TaskError)` out of `_renamed_gitlinks`, and the message contains `"not a tree object"`. The docstring carries D4: `grade.py` counts it in `errors`, against a permanent false `resolved: False`. Exit 128 and that stderr are the measured reply to `git ls-tree` on a nonexistent object. **`TaskError` is not imported in `tests/test_grader.py`, and there is no `from bakeoff.tasks import` line to append to** — the file imports from `bakeoff.grader`, `bakeoff.grade_schema`, `bakeoff.oracle` and `bakeoff.schema` only. Add `from bakeoff.tasks import TaskError` to the import block at the top of the file, beside `from bakeoff.oracle import Oracle`.

- [ ] **T3.11 — extend `test_the_grader_version_moved_with_what_check_5_means`.** Add the `N-1 -> N` clause to its docstring and update the literal to the new value. Do not rewrite the earlier clauses.

---

## Task 4: `GRADER_VERSION` and the mutation anchor

- [ ] **T4.1** Read `GRADER_VERSION` in `bakeoff/src/bakeoff/grader.py` and set it to that integer plus one. Add the note paragraph above it, in the file's existing `#:` style, saying: a pure gitlink rename is now NOT GRADED rather than applied and graded against a tree the move never reached; it moves whether or not a stored row hits it because `scripts/grade.py`'s resume gate keys on `(run_id, GRADER_VERSION)` alone; it costs a full re-grade into a fresh `v<N>` artifacts directory per event log; and **no verdict on today's corpus changes** — measured 2026-09-02, 0 of 115 stored records carry a rename chunk of any kind.

- [ ] **T4.2** Add one anchor to `bakeoff/scripts/mutation_check.py`, in the grader block beside the other gitlink anchors, in the existing 6-tuple shape:

```python
    (
        # A 100%-similarity rename carries NO mode line, and a pure rename of
        # an ordinary file is byte-identical to one of a gitlink -- so the
        # mode can only come from `start_sha`'s tree. Skipping the lookup puts
        # the submission back on the path measured 2026-09-02: `git apply
        # --index` exits 0, the index entry moves, the submodule's files stay
        # at the OLD path, and the ladder grades `resolved: False` -- an
        # accusation over content the harness could not capture.
        "grader: grade a submission that moved a gitlink and nothing else",
        "src/bakeoff/grader.py",
        "        renamed = _renamed_gitlinks(",
        "        renamed = (lambda *a, **kw: ())(",
        "tests/test_grader.py -k gitlink_rename_is_not_graded",
        "not integration",
    ),
```

  **Count `MUTATIONS` before the edit and expect that number plus one; do not hard-code a total.** It read 164 on 2026-09-02, and items 1–8 each may add more — this is the same trap the `GRADER_VERSION` rule above avoids, and `mutation_check.py` itself hard-codes no expected count (it prints `{caught}/{len(real)}` and exits on `all(real)`), so only prose can be wrong here. If the `find` string is not unique or does not match byte-for-byte after Task 2, fix the anchor — a rotted anchor fails only on a run somebody makes.

---

## Task 5: docs, and the item's closure

- [ ] **T5.1 — `TASKS.md`.** Tick "**A pure gitlink rename is invisible to the grader's gitlink refusal.**" and replace "Not measured on a real submission" with what was measured: the harness's own `git diff --cached <base>` emits the shape (renames default to on since git 2.9 and the run tree sets no `diff.renames`); a plain `mv` produces it with **no** `.gitmodules` chunk, which is what the entry's own mitigation assumed; `git apply --index` of it exits 0 and strands the content; and the fix is `git ls-tree <start_sha>` on the rename source, after `materialize`.

- [ ] **T5.2 — `TASKS.md`, a new P2 entry** for the finding D2 turned up and deliberately did not fix:

```
- [ ] **`files_touched` reports only a rename's destination.** `container.
  snapshot_diff` runs `git diff --cached --name-only <base_sha>` with rename
  detection on (git's default since 2.9; the run tree sets no
  `diff.renames`), so a submission that moves `big.py` to `moved_big.py`
  records `moved_big.py` and NOT `big.py` -- measured 2026-09-02, 3 names
  where `--no-renames` reports 5. A reader asking which files the model
  touched is told about the destinations only, and the source's disappearance
  is invisible. This is a change to what the harness RECORDS -- `final_diff`
  bytes and `files_touched` both move, and diff-size views with them -- so it
  needs its own plan and a `SCHEMA_VERSION` bump. Found while closing the
  gitlink-rename item (round 2, item 9); deliberately not folded into it,
  because one commit must not change both what is measured and what is
  graded.
```

- [ ] **T5.3 — `tasks/todo.md`.** A review-log section for this item, carrying: the measurement table from M6; the two-site split and why (D1, D3); the version bump with its zero-verdict-change measurement (0 of 115 stored records carry a rename chunk); and D2 **in its current framing** — `--no-renames` is *insufficient*, not wrong, because a capture-side flag cannot reach a row already written to an append-only log and would leave the grader's soundness resting invisibly on a flag in another process, while the question of whether the *record* should carry renames at all is separate and lands with T5.2. **Do not write "rejected with its four reasons"**: that was the pre-review framing, and D2 no longer claims `--no-renames` is the worse recording.

- [ ] **T5.4** Run `graphify update .` (AST-only, no API cost).

---

## Verification

- [ ] `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — every test passes; the count is the recorded baseline **plus 8**. T3.3 through T3.10 is eight new tests; T3.11 edits an existing one and adds none.
- [ ] `cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py -q -k "gitlink or rename"` — the new tests and the four existing gitlink tests together.
- [ ] The two pre-materialize tests still run with no Docker and no mirror: `cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py -q -k "not_graded_rather_than_failed or nested_repo_is_not_graded_either"`.
- [ ] `cd bakeoff && .venv/bin/python scripts/mutation_check.py` — **solo**. Every anchor CAUGHT, none STALE, and the total is the pre-edit `MUTATIONS` count plus one (164 + 1 = 165 if nothing else lands first; verify, do not assume).
- [ ] `cd bakeoff && .venv/bin/python scripts/verify_logger.py` — `GATE PASSED`. It does not exercise this branch (no stored fixture carries a rename), so this is a no-regression check.
- [ ] Manual re-measurement of the load-bearing external fact, since the whole plan rests on it. In a scratch superproject under `$HOME`, with a submodule at `sub`:

```
mv sub newsub && git add -A && git diff --cached <base>
```

  must print `similarity index 100%` / `rename from sub` / `rename to newsub` with **no** `mode` line and **no** `.gitmodules` chunk. If a future git changes the default for `diff.renames`, this branch becomes unreachable and the plan's premise is gone — say so rather than proceeding.
- [ ] Optional, and only if a submodule task is already gated in `~/.cache/bakeoff-probe/taskset/`: grade one synthetic record whose `final_diff` is a real `mv tests/toml-test tests/toml_test` snapshot against `tomlkit-514-inline-table-comment-separator`, and confirm the line reads `not_graded_reason: submodule_gitlink_ungradable`. This needs a Docker daemon and the repo mirror; it spends nothing.

---

## What this does NOT do

- **It does not change `container.snapshot_diff`, `SCHEMA_VERSION`, or anything the harness records.** D2. `--no-renames` was measured to work and is *insufficient*, not wrong: it cannot reach a row already written to an append-only log, and it would make the grader depend invisibly on a flag in another process. The grader-side `ls-tree` is needed whatever the harness later records, so the two are not alternatives.
- **It does not fix `files_touched`'s missing rename sources.** Filed as its own item (T5.2).
- **It does not detect a submodule change the harness never captured at all.** `git add -A` stages nothing for an *uncommitted* edit inside a submodule, so `snapshot_diff` returns zero bytes and no grader-side check can see it. That is the separate open item "a pure-gitlink *edit* is invisible" (round 2, item 17), which is a change to what a run RECORDS.
- **It does not add a `NotGradedReason`.** D5.
- **It does not make the refusal free.** A refused rename costs one hardlinked `materialize`; a submission with any rename chunk costs one host `git ls-tree`. D3 states both.
- **It does not touch `_check_test_restore`, the ladder, the oracle or preflight.** No check's own verdict changes; the only new outcome is a row leaving the denominator.
- **It does not claim the corpus was affected.** Measured: 0 of 115 stored records carry a rename chunk, so no stored verdict moves. The version bump is for the resume gate, not for a flip.

---

## Self-review notes

- **The `_parse_submission` double call is deliberate** (D7) and is the one thing a reviewer will reach for. The cheap collapse — parse once in `grade_run`, pass the list to `_rename_pairs(parsed)` — breaks no signature and no test; it is declined because it makes the two refusals share state, and because it buys nothing on the clone (`start_sha` exists nowhere before `materialize`). The expensive collapse, threading `parsed` through `_gitlinks_touched`, does break four tests. D7 states both.
- **`_start_state_gitlinks` is a host `subprocess.run`, not an `env.exec`.** The `env` seam exists so `run_ladder` needs no container; this call is not in the ladder, has no container yet, and asks the object store rather than the index — which is why `_refresh_index`'s stat-cache problem does not reach it.
- **The order in T2.1 matters and T3.9 pins it.** Above the `try`, a refused row leaves a materialized tree on disk holding somebody's submission.
- **T3.3's `_chunk_is_gitlink(...) is False` assertion looks like it pins a bug.** It does, on purpose: the two authorities answer different questions and a future reader who "fixes" `_chunk_is_gitlink` to match `rename from` would re-introduce a body-text-style authority that refuses every honest 100 % file rename.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

Under *Invariants*, appended to the paragraph that already covers the gitlink refusal:

> **A 100 %-similarity rename carries no mode line, and a pure rename of an ordinary file is byte-identical to one of a gitlink.** So the gitlink refusal is in two places, not one. Every shape carrying a `160000` mode line is refused from the diff alone, before `materialize` — that row costs no tree, no container and no mirror. A pure rename carries no mode at all (measured 2026-09-02, git 2.50.1, on the harness's own `git diff --cached <base>`, whose rename detection is git's default since 2.9 and which the run tree does not disable), and a plain `mv sub newsub` emits it with no `.gitmodules` chunk beside it — so the mode comes from `git ls-tree <start_sha>` on the rename **source**, after `materialize` and before the container: `start_sha`'s commit is built by `materialize` and exists in no mirror, so no rearrangement asks the question earlier. `git apply --index` of that four-line chunk exits 0, moves the index entry and leaves the submodule's files at the old path with an empty directory at the new one, so the ladder would grade a tree the move never reached. **Capturing with `--no-renames` would not have replaced this check**: a capture-side flag cannot reach a row already written to an append-only log, and a grader whose soundness rests on a flag in another process goes silently blind the day someone removes it. Whether the *record* should carry renames at all is a separate question with its own evidence — with renames on, `files_touched` reports only a rename's destination — and it is tracked separately.

---

## Review 1 → changes

Review at `.superpowers/broaden/round2/plan-9-review-1.md` (opus, 2026-09-02): REVISE, ten findings. Every load-bearing measurement in the plan was reproduced independently by the reviewer under `GIT_CONFIG_GLOBAL=/dev/null` / `GIT_CONFIG_SYSTEM=/dev/null`, so M1–M7, D1, D7's `_chunk_path` behaviour and D8's `:(literal)` all stand. All ten are addressed; one carries a correction back.

**1. T3.9 could not be transcribed, and the obvious spelling fails against item 3's husk — fixed.** Confirmed against `container.fresh_tree` and `_sweep_stale_trees` on disk: `tree` is `grade-tree/<run_id>/<uuid4().hex>`, the `finally` removes the leaf, and the key-level husk is documented as surviving and as *not* collected by the sweep. T3.9 now writes the fake `materialize` out in full so the test learns the uuid leaf from `Path(dest).parent`, asserts `not leaves[0].exists()`, and carries an explicit **do not** for the key-level spelling. A Global Constraint and D3's cost bullet both say the husk is `fresh_tree`'s deliberate cost.

**2. The mutation count was hard-coded and stale — fixed.** `MUTATIONS` holds **164**, not 160, and items 1–8 may add more. Both literals are replaced with "count before the edit, expect that plus one", matching the rule the plan already applies to `GRADER_VERSION`. The anchor tuple itself is unchanged; the reviewer verified the find string is unique and the replacement parses.

**3. "baseline plus 9" was wrong — fixed to plus 8.** T3.3–T3.10 is eight tests; T3.11 edits an existing one.

**4. The named import line does not exist — fixed.** `tests/test_grader.py` has no `from bakeoff.tasks import` line (verified: it imports `bakeoff.grader`, `bakeoff.grade_schema`, `bakeoff.oracle`, `bakeoff.schema`). T3.10 now says to add `from bakeoff.tasks import TaskError` beside `from bakeoff.oracle import Oracle`.

**5. D3 and D7 contradicted each other on the no-rename cost — fixed.** D3's bullet no longer says "pays nothing"; it says no `git ls-tree` and no container, concedes the extra `_parse_submission`, prices it at ~690 short-lived git processes per 115-row batch, and cross-references D7.

**6. D7 rejected a strawman — fixed, and the finding's stated benefit is disputed.** The reason on the page now names the alternative a reviewer actually reaches for (parse once in `grade_run`, pass the list to a new `_rename_pairs(parsed)`), records that it breaks **no** signature and **no** test, and declines it on its real cost: two refusals sharing state that today they deliberately do not, for tens of milliseconds. **The finding's claim that this "would let the plan skip the wasted clone on a refused row" does not hold, and the plan now says why**: the rename *pairs* cannot decide anything on their own — M5 measures that a pure rename of an ordinary file is byte-identical in shape — so the mode must come from `start_sha`'s tree, and `start_sha`'s commit is created by `materialize` in the run tree. The pruned mirror is per `(repo, base_sha)` and does not contain it; `declared_start_sha` is a name, not a repository. No repository holds `start_sha` before `materialize` runs, so the clone is not removable at any parse position. The `ls-tree`, not the parse, is what forces the placement. Everything else in the finding is adopted.

**7. D2 was ranked wrong and framed as a fork — rewritten.** The section is retitled "not the fix for THIS, and the two changes are not a fork". The append-only-log argument leads and is named sufficient, with the invisible cross-process dependency as its second half. Old reason 1 is demoted to last and reworded to claim only what it can: that "for a grader's convenience" is the wrong *justification*, while conceding on the merits that `--no-renames` is plausibly the **more honest** record — renames-on records a heuristic interpretation that depends on `diff.renames`, `diff.renameLimit` and the inexact-rename cutoff, none of which the run tree pins, and it drops the source path from `files_touched` entirely. The `+`-lines slip is corrected (`-` lines in the delete chunk *and* `+` lines in the add chunk; 3→5 chunks, 3→5 names, 400→863 bytes), and that measurement is moved to where it belongs: a cost of the *separate* record change, quoted for whoever picks up T5.2. The closing `CLAUDE.md` sentence and the "what this does NOT do" bullet are reworded to match.

**8. Line numbers were inconsistent and one was wrong at both revisions — fixed.** A note at the top says numbers are working-tree-relative as of `8ab0b08` and advisory, and that every edit is anchored by quoted text. The `container.py:360` citation is replaced with the method name.

**9. T2.2's quoted text did not match the file — fixed.** The whole paragraph is now quoted verbatim from disk (`--`, not an em dash) as the text to replace, and the step says the replacement subsumes all three sentences.

**10. T3.8 and T3.9 never named the record's diff — fixed.** Both now spell out `grade_run(_record(diff=_GITLINK_RENAME_SUBMISSION), _task(), "sha256:image", _oracle(), tmp_path / "cache", tmp_path / "art")`, with a note that `_record`'s `diff` defaults to `TEXT_DIFF`.

**Adopted without change from the review's own measurements:** `:(literal)` is stronger than this plan claimed — a gitlink literally named `:weird` is returned **only** under the literal spelling, and the bare one exits 0 with empty output, i.e. silently reads as "not a gitlink". D8 already chose it; the sharper reason is recorded here.

---

### Review 2

Review 2 (opus, 2026-09-02): all ten of review 1's findings confirmed addressed and the finding-6 dispute upheld — the wasted clone is not removable at any parse position, because `start_sha`'s commit is created by `materialize` and exists in no mirror. Three mechanical edits remained and are folded in above:

**R2.1 — File Structure still hard-coded the mutation total.** `# 1 new anchor (160 -> 161)` contradicted T4.2's own "count before the edit, expect that number plus one" (`MUTATIONS` read 164, and items 4–8 may move it again). Replaced with `(count before, expect +1)`. The adjacent `9 tests` was corrected to `8 new tests + 1 edited (T3.11)` in the same block, for consistency with the Verification section's "plus 8".

**R2.2 — T5.3 told the implementer to write the superseded D2 framing into `tasks/todo.md`.** It said "`--no-renames` rejected with its four reasons", which is the pre-review shape: D2 now says *insufficient, not wrong*, leads on the append-only-log and cross-process-flag arguments, and hands the record question to T5.2 rather than settling it. T5.3 now states the current framing in full and carries an explicit **do not** for the old wording, so a transcribing implementer cannot reintroduce a claim the plan retracted.

**R2.3 — three citations contradicted the advisory-line-numbers note.** The note at the top says numbers are working-tree-relative as of `8ab0b08` and advisory, and that every edit is anchored by quoted text; `TASKS.md:1375-1384 at HEAD 8232032` (M-source), `grader.py:661` (M2) and `grader.py:689-757 at HEAD` (T1.1) each asserted otherwise, and the last one is the label on an actual edit instruction. All three now name a `grep` target or a symbol instead of a range. The remaining `grader.py:<n>` citations in D1, D4, T1.2 and T1.4 are prose cross-references to code this plan does not edit, and the advisory note covers them.
