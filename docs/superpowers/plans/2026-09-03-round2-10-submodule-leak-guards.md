# Round 2, item 10: the submodule leak guards are measured, made total, and given a post-condition — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A materialized run tree may not carry the operator's mirror-cache path anywhere under `.git`, and that is enforced by the code rather than by a unit test that happens to be green on the machine it was written on. The two leak guards in `tasks._init_submodules` stop being asymmetric — `git remote remove` becomes total (it removes **every** remote, by the name git actually gave it, `check=True`) instead of tolerant of a failure that was measured to be exactly the leak case — and `materialize` gains one post-condition, `_refuse_host_mirror_path`, which walks the whole `.git` subtree of the finished run tree and raises `TaskError` naming the first file that still carries `<cache_root>/repos`.

**Architecture:** No new concept and no new manifest key. Two new module-private functions in `tasks.py` (`_refuse_host_mirror_path` and its chunked matcher `_file_contains`), called once from `materialize` after `_init_submodules` returns. Four existing `_git` call sites change: the two run-tree `remote remove origin` calls (submodule and superproject) become a listing plus a removal per name at `check=True`, and the superproject `reflog expire` loses its `check=False` so both halves match the submodule half that already had it. The three *cache-side* `"origin"` sites — `_build_pruned_mirror`'s removal and `ensure_mirror`'s two fetches — are **not** touched (`HANDOFF.md` gates them; see "What this does NOT do", Task 7.2 and OQ1).

**Tech Stack:** Python 3.12.13 (the harness venv), git 2.50.1 (Apple Git-155), pytest. No Docker, no network, no credentials for the unit work. The integration leg needs the `integration and task_image` selection the module already carries.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` §5.1 (fresh tree per run; the run tree is the artifact the agent receives). Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **10**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind. Repo invariants: `CLAUDE.md`, **"Silence is the enemy"**, **"The run tree holds no object outside `base_sha`'s history"** (this is its host-path sibling), and **"A cached artifact is trusted only on the invariant re-checked against the artifact itself"** — the post-condition here is the same move, applied to the run tree instead of the cache. `HANDOFF.md`, "Traps that already cost time" and "Still open".

**Review state:** **APPROVED.** Review 1 (opus, 2026-09-02) — REVISE, 13 findings, 3 blocking; all 13 addressed, none disputed. Review 2 (opus, 2026-09-02) — **APPROVED, 0 open findings**, 2 nits and a ruling closing OQ4; all three folded in. The per-review record is in "Review 1 → changes" and "Review 2" at the end. **Ready to implement.**

---

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Docstrings carry the *why* and the failure mode. Claims about external behaviour are annotated with what they were verified against (`git 2.50.1`). A comment that says what a line does rather than what breaks without it does not belong.
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section.
- **THIS PLAN IS WRITTEN AGAINST HEAD 8232032 AND WILL NOT BE IMPLEMENTED ON IT.** Implementation is strictly sequential and this is item **10**; items 1–9 land first. Item **2** (`submodules_unneeded`) edits `_init_submodules` directly, item **3** moves every run-tree root, and item **4** edits `load_task_set` in the same file. Therefore **every `tasks.py:NNN` below is "at HEAD 8232032; re-locate by the exact quoted source text"**. Every edit is addressed by quoted text and each quoted `find` string is verified with `grep -cF` (must print exactly `1`) before it is applied.
- **Composition with item 2 (`submodules_unneeded`) — stated explicitly.** Item 2's D5 makes `_init_submodules` filter to `needed = tuple(sub for sub in subs if not sub.declared_unneeded)` and return early when `needed` is empty. **A submodule declared unneeded is never initialised, so neither leak guard runs for it, and neither does anything in this plan's loop.** That is correct and requires no coordination: nothing is cloned for a declared path, so `.git/modules/<name>` is never created and there is nothing for a guard to clean. Review 1 confirmed that item 2's final plan leaves both of this plan's quoted `find` strings textually untouched (it changes `for sub in subs` to `for sub in needed` on a different line). It is also the second reason the post-condition of this plan lives in `materialize` and **not** inside `_init_submodules` — after item 2, `_init_submodules` returns early for a task whose only submodule is declared unneeded, and a post-condition placed inside it would then not run at all for that task, while the *superproject's* own guards (which this plan also tightens) still need covering. See D4.
- **Composition with item 3 (unique run-tree paths).** Item 3 rewrites all five `materialize` destinations. This plan's needle depends on where they are, and the constraint item 3 must preserve is written out in M6: **a run tree may live anywhere under the cache root except `repos/`.**
- **No version constant moves.** `SCHEMA_VERSION`, `GRADE_SCHEMA_VERSION`, `GRADER_VERSION`, `ORACLE_VERSION`, `PREFLIGHT_VERSION`, every `task_version` and every pinned `start_sha` are unchanged. Justified in D7.
- **No verdict moves and no refusal that exists today is removed.** This commit only adds refusals, all of them on the host, before any image is built and before any token is spent.
- **Commit hygiene:** one commit (plan + code + tests + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Indentation in this file's code fences.** Fences nested under a `- [ ]` item carry **two extra leading spaces** from the markdown list; the real source indentation is two less. The `mutation_check.py` `find`/`replace` strings in Task 6 are **not** nested and are given at true indentation, because that script matches them byte for byte.
- **Do not run `scripts/mutation_check.py` concurrently with anything else**; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — record **both** numbers it prints (passed **and** deselected). Items 1–9 land first, so neither will be the round's opening `1539 passed, 62 deselected`.

---

## The measured defect

**Source:** `TASKS.md`, "**The submodule's `remote remove` is `check=False` and the `reflog expire` beside it is `check=True`; nothing measured the asymmetry.**" (`TASKS.md:1386-1397` at HEAD 8232032). That bullet says *"Leave the code as it is… What is missing is the measurement, not the fix."* **The measurement was taken, and it inverts that conclusion.** See M4. Review 1 re-took every decisive row independently and they reproduced exactly.

All measurements below were taken **2026-09-02** on this worktree, **git 2.50.1 (Apple Git-155)**, macOS 25.5.0, via `bakeoff/.venv/bin/python` 3.12.13. Vehicles:

- **a scratch superproject** under `$HOME/.cache/bakeoff-r2-10/` (a `libdep` repo with one commit past the pin, a `super` pinning it at `vendor/libdep`), driven through the **real** `tasks.ensure_pruned_mirror` and then through `_init_submodules`' exact git sequence replayed by hand so a snapshot could be taken *between* the two guards;
- **the real task `tomlkit-514-inline-table-comment-separator`** from `~/.cache/bakeoff-probe/taskset/`, one non-nested submodule `tests/toml-test` → `BurntSushi/toml-test`, driven through the **real** `tasks.materialize` offline from the mirrors already in `~/.cache/bakeoff/repos`;
- Review 1 added `sqlglot-6927` and `pytest-10210` from the same probe taskset for the size figures in M6.

The needle in every scan is the **byte string** of the cache path; every file under `<run tree>/.git` was read as bytes and tested for containment. "Clean" means no file matched.

### M1 — where the host cache path is, guard by guard (scratch superproject)

Snapshots taken in `_init_submodules`' own order. `<n>` = `vendor/libdep`.

| file under the run tree | S1 after `submodule update --init` | S2 after the url rewrite (`git config submodule.<n>.url <declared>`) | S3 after `git remote remove origin` | S4 after `git reflog expire --expire=now --all` |
|---|---|---|---|---|
| `.git/config` (`submodule.<n>.url`) | **LEAK** | clean | clean | clean |
| `.git/modules/<n>/config` (`[remote "origin"] url`) | **LEAK** | **LEAK** | clean | clean |
| `.git/modules/<n>/logs/HEAD` | **LEAK** | **LEAK** | **LEAK** | clean |
| `.git/modules/<n>/logs/refs/heads/main` | **LEAK** | **LEAK** | **LEAK** | clean |
| `.git/modules/<n>/logs/refs/remotes/origin/HEAD` | **LEAK** | **LEAK** | clean | clean |
| `.gitmodules` | not rewritten — carries the *declared* url, never the mirror (it is a tracked file; rewriting it would dirty the tree) | | | |
| `.git/modules/<n>/FETCH_HEAD` | **does not exist** (`submodule update --init` clones, it does not fetch) | | | |
| `<n>/.git` (the gitfile) | `gitdir: ../../.git/modules/vendor/libdep` — **relative**, no host path, at every snapshot | | | |
| `.git/modules/<n>/objects/info/alternates` | **does not exist** (a local clone hardlinks; it does not borrow) | | | |

Three corrections to what the code says today:

1. **`remote remove` clears TWO files, not one.** `_init_submodules`' comment names only `.git/modules/<name>/config` as the config-side leak. `.git/modules/<n>/logs/refs/remotes/origin/HEAD` is a *third* reflog and is cleared by the **remote removal**, not by `reflog expire`.
2. **The docstring's "`.git/modules/<name>/config` is clean either way" is true only because `remote remove` ran first.** Read as written it invites the reader to think that file is never a leak. Column S2 shows it is the single file the removal exists for.
3. **The `.git/config` leak at S1 is closed by the url rewrite**, not by either guard. That rewrite is `check=True` already.

### M2 — the same table on the real task (`tomlkit-514`, submodule `tests/toml-test`)

Driven through the real `materialize`, with `tasks._git` patched to no-op (exit 0) the named call:

| guard skipped | files carrying `<cache>/repos` after materialization |
|---|---|
| neither (control) | *(clean)* |
| `remote remove` | `.git/config`, `.git/modules/tests/toml-test/config` |
| `reflog expire` | `.git/logs/HEAD`, `.git/logs/refs/heads/master`, `.git/modules/tests/toml-test/logs/HEAD`, `.git/modules/tests/toml-test/logs/refs/heads/main` |
| both | all eight of the above plus `.git/logs/refs/remotes/origin/HEAD` and `.git/modules/tests/toml-test/logs/refs/remotes/origin/HEAD` |

The predicates match on argv, so they skip the **superproject's** call and the **submodule's** call together — which is how the `.git/config` and `.git/logs/*` rows got into this table. That is itself the finding of M5: the superproject half leaks the identical way from the identical cause, and *both* of its guards are `check=False` today. (It is also the shape review 1's finding 3 is about: an argv-only predicate is not scoped to one call site, and the implementer must use the **positive, exact** `cwd` predicates written out in Task 4.)

`git status --porcelain` is `''` and `git submodule status` reads a leading space in **every** row of this table, including "both". Nothing downstream notices.

### M3 — `git remote remove origin` when there is no `origin`

```
$ git remote remove origin        # run twice in the same submodule
rc=0  stderr=''
rc=2  stderr="error: No such remote: 'origin'"
```

Exit **2**, message `error: No such remote: 'origin'`. This is the exit code the current `check=False` swallows.

### M4 — the failure `check=False` was tolerating is not benign; it is the leak

`TASKS.md` justified the asymmetry as *"'origin does not exist' on a submodule git chose not to give a remote"*. Measured: **git never chooses not to give a clone a remote.** `git clone` always creates one. The only way it is not named `origin` is `clone.defaultRemoteName`, and in exactly that case the leak survives both guards.

End-to-end through the **real** `materialize` on the **real** `tomlkit-514`, with `clone.defaultRemoteName=upstream` supplied the way an operator's global `~/.gitconfig` supplies it (`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=clone.defaultRemoteName GIT_CONFIG_VALUE_0=upstream`):

```
materialize SUCCEEDED, start_sha e1d72b883d2e452ca14835047e2fa7db02cdc4d8 -- no error, no warning
cache-path leaks after BOTH guards:
  .git/config
  .git/modules/tests/toml-test/config
submodule remotes:    'upstream\n'
superproject remotes: 'upstream\n'
git status --porcelain: ''
```

and the surviving content:

```
[remote "upstream"]
	url = /Users/…/.cache/bakeoff/repos/https---github.com-BurntSushi-toml-test.git-08ed869….git
	fetch = +refs/heads/*:refs/remotes/upstream/*
[branch "main"]
	remote = upstream
	merge = refs/heads/main
```

So: a `check=False` whose only reachable failure shape **is** the shape the guard exists to prevent. The bullet's own standard — *"tightening it without first measuring which exit codes git actually produces there would trade a quiet leak for a loud false refusal"* — is met and then some: there is no false refusal to trade for, because the measured failure is a true leak. It is also why the fix is **not** simply `check=True` (D1): `check=True` on `remote remove origin` would refuse every submodule task on that operator's machine for a condition two lines can *prevent*.

Review 1 re-measured the **fix** as well: removing every listed remote in both the run tree and the submodule clears both files *and* the dangling `[branch "main"]` section, the whole-`.git` scan then comes back clean, and `git submodule status` still reads a leading space.

### M5 — the superproject half has the same defect, from the same cause

`materialize`'s own two calls are **both** `check=False`:

```
    _git("remote", "remove", "origin", cwd=dest, check=False)
    _git("reflog", "expire", "--expire=now", "--all", cwd=dest, check=False)
```

Measured on the scratch superproject, incrementally:

| snapshot | files carrying `<cache>/repos` |
|---|---|
| after `clone --local` + `checkout --detach`, no guards | `.git/config`, `.git/logs/HEAD`, `.git/logs/refs/heads/main`, `.git/logs/refs/remotes/origin/HEAD` |
| after `remote remove` only | `.git/logs/HEAD`, `.git/logs/refs/heads/main` |
| after both | *(clean)* |

M4's `clone.defaultRemoteName` run shows the superproject's `.git/config` leaking for exactly the reason the submodule's does.

### M5b — the same root cause has five sites; this plan fixes two

`grep -n '"origin"' bakeoff/src/bakeoff/tasks.py` at HEAD 8232032:

| line | call | `check=` | what `clone.defaultRemoteName` does to it | in this commit? |
|---|---|---|---|---|
| `:1569` | `fetch --prune origin` (`ensure_mirror`) | `False` | **dead** — measured exit **128**, `fatal: 'origin' does not appear to be a git repository` | no — OQ1 / Task 7.2 |
| `:1577` | `fetch origin <base_sha>` (`ensure_mirror`) | `False` | **dead**, same 128 | no — OQ1 / Task 7.2 |
| `:2191` | `remote remove origin` (`_build_pruned_mirror`, `cwd=tmp`) | `False` | swallowed exit 2; the published mirror keeps `remote.upstream.fetch=+refs/*:refs/*` and `mirror=true`, and `_verify_pruned` asserts nothing about remotes | no — OQ1 / Task 7.2 |
| `:2448` | `remote remove origin` (`_init_submodules`) | `False` | swallowed exit 2 → **leak** into the run tree | **yes — Task 2** |
| `:2519` | `remote remove origin` (`materialize`) | `False` | swallowed exit 2 → **leak** into the run tree | **yes — Task 3** |

The three cache-side sites have consequences that are **not** run-tree leaks: `:1569`/`:1577` are a *dead refresh path* (a cached mirror that does not yet hold `base_sha` can never be refreshed; `ensure_mirror`'s `cat-file -e` then raises, so it is loud), and `:2191` is a *future-restoration hazard* that needs one `git fetch` to fire and (measured) does not reach the run tree, because `clone --local` writes its own remote and copies neither config nor reflogs. All three are gated by `HANDOFF.md` — its scope line covers `ensure_mirror`, and its "Still open" item 2 names that function directly — so they are filed in one bullet, not fixed here. The bullet names all three, because a bullet that names one of three invites a partial fix.

### M6 — the post-condition's cost, its memory bound, and the needle it must use

**Time.** On the real `tomlkit-514` run tree: **61 files, 2,220,835 bytes, 0.0046 s**, against a `materialize` of **0.350 s**. Review 1 added two larger corpus members:

| task | files under `.git` | total | largest single file | scan | peak RSS (whole-file read) |
|---|---|---|---|---|---|
| `tomlkit-514` | 61 | 2.2 MB | — | 0.0046 s | — |
| `sqlglot-6927` | 34 | 35.8 MB | 33.6 MB | 0.009 s | 65 MB |
| `pytest-10210` | 35 | 44.8 MB | 40.3 MB | 0.013 s | 109 MB |

Time is ≤ 4% of a 0.29–0.35 s `materialize` in every case. **Memory, under a naïve `read_bytes()`, is the largest single file** — the run tree's hardlinked pack. That is why Task 1.1 reads in 1 MiB chunks with a `len(needle) - 1` byte overlap (the needle can straddle a boundary): peak is then bounded by the chunk, not by the pack, and the bound is a property of the code rather than of the corpus that happened to be measured. The overlap arithmetic was checked at every offset 0–39 against an 8-byte chunk before being written down.

**The needle must be `<cache_root>/repos`, not `<cache_root>`.** Every one of the **five** `materialize` call sites puts its run tree *underneath* the cache root:

| call site (verified with `git show HEAD:<file>` at HEAD 8232032) | destination |
|---|---|
| `run_matrix.py:278` (`preflight_tasks`) | `<cache>/preflight-tree/<task_id>/repo` |
| `run_matrix.py:361` (`run_cell`) | `<cache>/artifacts/<stamp>/<cell>/repo` (via `artifacts_root`) |
| `oracle.py:289` (`ensure_oracle`) | `<cache>/oracle-tree/<task_id>/repo` |
| `grader.py:2014` (`_materialize_for_grade`) | `<cache>/grade-tree/<run_id>/repo` |
| `grade.py:304` (`_preflight_task`) | `<cache>/grade-preflight-tree/<task_id>/repo` |

**Re-locate these by the quoted call, not by the number.** The line numbers are
correct at HEAD 8232032 and are already stale in a worktree carrying items 1-9:
an earlier draft of this table cited `run_matrix.py:264`/`:337`,
`grader.py:2013` and `grade.py:297`, and `grader.py` moved by one line between
two reads inside a single session. The `grep -n 'materialize(task,'` that
produced this table is the reproducible form.

**None** is under `repos/`, where `mirror_path` and `pruned_mirror_path` both land — so the narrow needle covers the superproject mirror and every submodule mirror, pruned and unpruned, and cannot match a tree's own path. **This is a constraint round-2 item 3 must preserve**, since it is rewriting all five: *a run tree may live anywhere under the cache root except `repos/`.* Measured: both needles come back clean today (the real task materialized into `<cache>/r2-10-fp/repo`), so this is not a live false-positive fix; the narrow needle is chosen because it stays correct if a future git or call site writes the tree's own absolute path into `.git`. git 2.50.1 writes `core.worktree = ../../../../tests/toml-test` — **relative** — and the gitfile relative too, but neither is contractual.

### M7 — what the green case may assert, and what it may not

`git reflog expire --expire=now --all` **truncates** the reflog files; it does not unlink them. **After the expire runs**, `.git/logs/HEAD`, `.git/logs/refs/heads/<branch>`, `.git/modules/<n>/logs/HEAD` and `.git/modules/<n>/logs/refs/heads/main` all still exist at 0 bytes.

**But that is a statement about the moment the guard runs, not about the tree the tests read.** `materialize`'s superproject `reflog expire` (`tasks.py:2519`) runs long **before** the setup commit (`:2580`), and that commit appends a reflog entry. Measured after `materialize` returns:

```
.git/logs/HEAD                                    160     <- NOT 0
.git/logs/refs/heads/master                         0     (branch name is fixture-dependent)
.git/modules/tests/toml-test/logs/HEAD              0
.git/modules/tests/toml-test/logs/refs/heads/main   0
```

and the 160 bytes are `82bb211e… e1d72b88… bakeoff <eval@pindrop.test> 0 +0000\tcommit: bakeoff: task setup (test oracle)`.

So a non-vacuity assertion may use `st_size == 0` **only for the submodule's `logs/HEAD`**, whose expire is the last write to it. For the superproject the tests assert the strictly stronger property instead: the file **exists**, is **non-empty**, and does **not** contain the needle. `.git/logs/refs/heads/<branch>` is not a substitute — the branch is `main` in `test_tasks.py`'s fixture and **`master`** on `tomlkit-514`.

---

## Design decisions, settled

### D1. `remote remove` becomes a listing plus one removal per name, at `check=True` — not `check=True` on `origin`

The item's brief proposed "both guards `check=True`". Taken literally that is worse than the alternative and M4 is why: `check=True` on `git remote remove origin` converts the one reachable failure into a `TaskError` reading `git remote remove origin failed (exit 2): error: No such remote: 'origin'` — which names nothing about the operator's config, looks like a harness bug, and refuses **every** task with a submodule on that machine, mid-matrix, for a condition that is trivially fixable.

Removing every remote git listed fixes it instead of refusing it, and it still satisfies "a failed leak guard is a failed materialization": `check=True` sits on **both** calls, and the name being removed came out of git's own listing one statement earlier, so a name git prints and then refuses to remove is a defect, not a condition to tolerate.

**Why `check=True` on the *listing* is load-bearing rather than decorative** (review 1's ruling on the plan's former OQ3, folded in here). The listing is a config read in a repository `_git` created one or two statements earlier; the shapes that make it non-zero are "not a git repository" (128) and an unreadable or malformed `.git/config` (128), neither benign and both fatal to the removal that follows. The narrow reason is better than the general one: **at `check=False` a failed listing returns `stdout=""`, which is byte-identical to a repository with no remotes**, so the loop iterates zero times and the guard reports success having done nothing — the same silence this item removes, one refactor later.

**Alternatives rejected.**

- *`check=True` on the literal `origin` (the brief's wording).* Rejected per the paragraph above. It converts a preventable leak into an unexplained refusal.
- *Keep `check=False` and rely on the new post-condition alone.* The post-condition would catch M4 — loudly, naming `.git/modules/<n>/config` — but the run would still be **refused** rather than materialized. Same objection: a fixable condition turned into a stop.
- *`git config --remove-section remote.<name>` or unset `remote.*.url` directly.* Reaches inside git's config layout by hand and leaves `branch.<b>.remote` dangling — which the measured fix removes, because `git remote remove` does both and is the documented operation.
- *`git remote | xargs git remote remove` in one shell.* `_git` does not use a shell, deliberately.
- *`.split()` on the listing.* `.splitlines()` instead: it is obviously correct for names, and it does not depend on the (true, but incidental) fact that git refuses a remote name with a space — measured, `git remote add "a b" …` exits **128**, `fatal: 'a b' is not a valid remote name`.

### D2. The superproject's two calls in `materialize` change with the submodule's

`materialize`'s `remote remove` and `reflog expire` are **both** `check=False` today, and M5 shows they protect the identical files one level up, from the identical cause. Fixing only the submodule half would ship a plan whose own measurement contradicts it, and would leave the new post-condition **refusing** the superproject case (M4 leaks `.git/config` too) instead of preventing it.

So: the superproject's `remote remove` becomes the same listing-plus-removal at `check=True`, and its `reflog expire` loses `check=False`, matching the submodule's, which has been `check=True` since broadening 6.

**On `reflog expire` at `check=True` with no measured failure shape.** No failure mode was found for it — it runs in a repository the harness created one statement earlier. The argument for tightening it is the one `materialize`'s alternates check already makes in its own comment: *raising is affordable because `materialize` runs before the container — it costs setup time and zero tokens, and `run_matrix` catches per cell.* An unmeasured failure here becomes a loud, cheap, pre-spend stop rather than a silent gap; the post-condition below then names the surviving file whichever guard failed.

**Alternative rejected:** *leave `materialize` alone, scope strictly to `_init_submodules`.* Rejected, and review 1 ruled the same way for a second reason: the superproject half is not a *second* defect — the M5 snapshots show `.git/logs/HEAD` and `.git/logs/refs/heads/main` leaking from the same clone until `materialize`'s own expire — so splitting would put one measurement in two commits and leave the first commit's own table contradicting its diff.

### D3. The post-condition walks with `os.walk(..., onerror=)` and matches **bytes**, in chunks — not `grep -rF`, not `rglob`, not `read_bytes()`

`grep -rlF` would work, but: it adds a subprocess and a dependence on GNU-vs-BSD binary-file handling for exactly the files that matter least; it cannot name *which* byte string it found; and a Python walk can raise a `TaskError` naming the task and the relative path, which is the shape every other refusal in this module has. The scan is bytes, never text, so no decode can fail and no `errors="replace"` can hide a match.

**`os.walk` with `onerror=`, not `Path.rglob`.** `pathlib`'s recursive glob **suppresses `PermissionError` while walking**, so a directory the process cannot list — and every leaking file under it — is skipped with no exception and no trace. Measured: a `.git/` containing `sub/` at mode `000` with a leaking file inside yields `rglob('*') -> ['sub']` and **no error**, while `os.walk(root, onerror=…)` reports `[Errno 13] Permission denied`. That is not hypothetical in this subsystem: `HANDOFF.md`'s "Still open" item 2 records that `ensure_mirror`'s mirror "is still at the umask's mercy". **The walk raising is as load-bearing as the read raising** — a check whose "clean" verdict can be produced by a directory it never entered is the silence this item exists to remove. Each directory's entries are sorted so the *first* leak reported is deterministic.

**Chunked reads, 1 MiB with a `len(needle) - 1` overlap.** M6: a whole-file `read_bytes()` peaks at the largest file under `.git`, measured 40.3 MB across the probe corpus and 109 MB process RSS. The chunk bounds it at the chunk. The overlap is what keeps a needle that straddles a boundary from being missed, and the arithmetic was verified at every offset before being written down.

**`objects/` is scanned, not skipped.** Packs and loose objects are zlib-compressed, so they contribute no true and (measured) no false positives — but `objects/info/alternates` is plain text and is a **known** leak vector. `materialize`'s dedicated alternates check covers `dest/.git/objects/info/alternates` only, and it runs *before* `_init_submodules`; **`.git/modules/<n>/objects/info/alternates` is checked by nothing today.** Scanning `objects/` covers it for the cost measured in M6. This is the one design decision that a later "optimisation" (`if "objects" in parts: continue`) could silently undo while every other named test stayed green, so it gets its **own** test (Task 4.9), not only the delete-the-whole-check mutation. The dedicated superproject check stays where it is and keeps its own message — it names `--dissociate`, which the generic scan cannot.

**Symlinks are skipped, not followed** (`os.path.islink` before the read): following one would read a file outside the tree and report it as a leak inside it.

**An unreadable file is a `TaskError`, not a `continue`**, for the same reason the walk's `onerror` is.

### D4. The post-condition lives in `materialize`, after `_init_submodules`, not inside it

Three reasons, in order of weight:

1. **After item 2, `_init_submodules` returns early** for a task whose only submodule is declared unneeded (its D5: `if not needed: return`), and it already returns early today for a task with **no** submodules (`if not subs: return`). A post-condition placed inside it therefore would not run for the majority of tasks — including every task in `bakeoff/taskset/` today.
2. **The superproject leaks the same way** (M5), and its guards are in `materialize`. A check placed in `_init_submodules` cannot be the post-condition for guards that ran before it was called.
3. `_init_submodules` runs **last** in `materialize`, so a single call after it sees the finished tree — the same tree the container bind-mounts.

**Alternative rejected:** *call it from both places.* Two calls, one of them redundant, and a reader has to work out which one is load-bearing.

### D5. It is a named function, `_refuse_host_mirror_path`, not an inline block

It joins the `_refuse_*` family already in this module (`_refuse_submodule_conflicts`), it is the unit the mutation anchors point at, and it takes `(dest, cache_root, task_id)` — everything it needs, nothing it does not. Its chunked matcher is a second module-private helper, `_file_contains`, so the walk reads as a walk.

### D6. The existing unit test is widened rather than duplicated

`test_tasks.py::test_the_run_tree_carries_no_host_cache_path_for_the_submodule` scans `.git/modules` and separately asserts `.git/config` is clean. M5 shows `.git/logs/*` is a leak surface it cannot see. It is renamed and widened to the whole `.git` subtree and given the M7 non-vacuity assertions — in the corrected, per-file form. Its docstring already explains why a config-only check is not enough; the widening is the next term in the same sentence.

### D7. No version constant moves

- **`SCHEMA_VERSION`**: no `RunRecord` field is added, removed or changes meaning.
- **`PREFLIGHT_VERSION`**: **unchanged by this item, at whatever value items 1–9 leave it.** Preflight gains **no** assertion here. Considered and rejected: preflight could re-assert the same property from inside the container, where `/repo/.git` is the bind-mounted host tree. It would be a second reading of one artifact by one process from one filesystem, adding no independent observation, and it would invalidate every cached verdict for a check that already ran on the host before the container started. The property is a **materialization** post-condition, and it is enforced where it can still be prevented rather than only detected.
- **`GRADER_VERSION` / `GRADE_SCHEMA_VERSION` / `ORACLE_VERSION`**: the ladder, the oracle and the grader's env are untouched. `grader.py` and `oracle.py` call `materialize` and so inherit the new refusal; no verdict they produce changes for any tree that materializes today.
- **`task_version` / `start_sha`**: unchanged on every task. **The start state does not move.** `start_sha` is computed *before* `_init_submodules` is called, from a tree that has never seen a submodule checkout; and nothing in this plan touches an object, a ref, the index or a tracked file — only `.git` metadata that is already scrubbed. `manifest_digest` gains no term. Confirmed end to end: `tomlkit-514` materializes to `e1d72b883d2e452ca14835047e2fa7db02cdc4d8` before and after.

---

## Tasks

### Task 1: the leak-scan post-condition

- [ ] **1.1** Add `_file_contains` and `_refuse_host_mirror_path` to `bakeoff/src/bakeoff/tasks.py`, immediately **above** `def materialize(`. Verify the insertion point first: `grep -c '^def materialize(task: TaskManifest, dest: Path, cache_root: Path) -> str:' bakeoff/src/bakeoff/tasks.py` must print `1`. `os` is already imported at module scope (`_git` uses `os.environ`); add nothing.

  ```python
  #: Chunk size for the run-tree leak scan. Measured 2026-09-02: the largest
  #: single file under a run tree's `.git` across the probe corpus is a 40.3 MB
  #: hardlinked pack (`pytest-10210`), and a whole-file `read_bytes()` peaks the
  #: process at 109 MB for it. Chunking bounds the peak at this constant instead
  #: of at the corpus.
  _LEAK_SCAN_CHUNK = 1 << 20


  def _file_contains(path: Path, needle: bytes) -> bool:
      """`needle in path`'s bytes, without reading the whole file into memory.

      The `len(needle) - 1` byte carry-over is the whole reason this is not a
      loop over `read()`: a needle that straddles a chunk boundary is invisible
      to a per-chunk test, and the run tree's largest files are exactly the ones
      that need more than one chunk. Verified at every offset 0..39 against an
      8-byte chunk before it was written down.

      The empty-needle guard is not defensive decoration. `overlap` would be
      -1, which is TRUTHY, so the carry-over would evaluate `[-(-1):]` == `[1:]`
      and keep all but one byte of everything read so far -- the whole file in
      memory, which is the one property this function exists to avoid. The
      caller's needle is `<cache_root>/repos` and can never be empty, so the
      guard is unreachable today; it is here because the failure it prevents is
      silent growth rather than an exception, and the next caller does not
      inherit the caller's guarantee.
      """
      if not needle:
          raise ValueError("_file_contains needs a non-empty needle")
      overlap = len(needle) - 1
      tail = b""
      with path.open("rb") as handle:
          while chunk := handle.read(_LEAK_SCAN_CHUNK):
              if needle in tail + chunk:
                  return True
              tail = (tail + chunk)[-overlap:] if overlap else b""
      return False


  def _refuse_host_mirror_path(dest: Path, cache_root: Path, task_id: str) -> None:
      """No file under the run tree's `.git` may name the operator's mirror cache.

      THE POST-CONDITION FOR EVERY LEAK GUARD IN THIS MODULE'S RUN-TREE PATH.
      `materialize` and `_init_submodules` between them run four scrubs -- two
      `git remote remove` loops and two `git reflog expire`s -- and every one of
      them could fail, or miss, in a way `git status --porcelain` renders as a
      clean tree. Measured 2026-09-02, git 2.50.1: an operator with
      `clone.defaultRemoteName = upstream` in their global config gets a run
      tree whose `.git/config` AND `.git/modules/<name>/config` both carry
      `[remote "upstream"] url = <host cache path>`, with materialization
      reporting success and the working tree clean. The path is two defects at
      once: a host path the container cannot resolve (an error surface the agent
      is scored on) and a publication of the operator's cache layout.

      The needle is `<cache_root>/repos`, NOT `cache_root`, and that is
      load-bearing: all five `materialize` call sites put run trees UNDERNEATH
      the cache root (`preflight-tree`, `artifacts`, `oracle-tree`,
      `grade-tree`, `grade-preflight-tree`), so a whole-cache-root needle is a
      prefix of the tree's own path. `mirror_path` and `pruned_mirror_path` both
      land under `repos/` and nothing else does, so one needle covers the
      superproject's mirror and every submodule's, pruned and unpruned.

      `os.walk` with `onerror`, NOT `Path.rglob`: pathlib's recursive glob
      SUPPRESSES the PermissionError a directory it cannot list raises, so the
      leaking files under it are skipped with no exception and no trace
      (measured -- `rglob('*')` returns the mode-000 directory and nothing
      inside it, while `os.walk(..., onerror=)` reports errno 13). A "clean"
      verdict produced by a directory this function never entered is the exact
      silence it exists to remove, so the WALK raising is as load-bearing as the
      READ raising. Entries are sorted so the first leak reported is
      deterministic.

      Bytes, never text: a decode would need `errors="replace"`, and a replaced
      byte is a match this check would miss. `objects/` is scanned rather than
      skipped -- packs are compressed and contribute nothing either way, but
      `objects/info/alternates` is plain text, and while `materialize` checks
      the superproject's own copy by name, NOTHING checks
      `.git/modules/<name>/objects/info/alternates`. Measured cost: 0.005-0.013 s
      across the probe corpus against a `materialize` of 0.29-0.35 s.
      """
      needle = str(Path(cache_root) / "repos").encode()
      base = Path(dest) / ".git"

      def _refuse_walk_error(exc: OSError) -> None:
          raise TaskError(
              f"{task_id}: {exc.filename} under the run tree's .git could not "
              f"be listed ({exc}), so the host-path leak check could not "
              "complete. A clean verdict here would be a claim this process is "
              "not in a position to make."
          )

      for root, dirnames, filenames in os.walk(base, onerror=_refuse_walk_error):
          dirnames.sort()
          for name in sorted(filenames):
              path = Path(root) / name
              if path.is_symlink() or not path.is_file():
                  continue
              try:
                  hit = _file_contains(path, needle)
              except OSError as exc:
                  raise TaskError(
                      f"{task_id}: {path} under the run tree's .git could not "
                      f"be read ({exc}), so the host-path leak check could not "
                      "complete. A clean verdict here would be a claim this "
                      "process is not in a position to make."
                  ) from exc
              if hit:
                  raise TaskError(
                      f"{task_id}: {path.relative_to(dest)} in the run tree "
                      f"carries the host mirror path {needle.decode()}. That is "
                      "a path the container cannot resolve and a publication of "
                      "the operator's cache layout, and `git status "
                      "--porcelain` is clean either way -- so nothing "
                      "downstream would notice. One of materialization's leak "
                      "guards (`git remote remove`, `git reflog expire`) did "
                      "not remove it."
                  )
  ```

- [ ] **1.2** Call it. `find` (verify `grep -cF` prints `1`):

  ```
      _init_submodules(task, dest, derive_submodules(task, mirror), cache_root)
      return start_sha
  ```

  `replace`:

  ```
      _init_submodules(task, dest, derive_submodules(task, mirror), cache_root)
      # AFTER `_init_submodules`, which is the last thing that writes into
      # `.git`, and in `materialize` rather than inside it: `_init_submodules`
      # returns early for a task with no submodules (and, since item 2, for one
      # whose submodules are all declared unneeded), and the superproject's own
      # two guards ran long before it was called.
      _refuse_host_mirror_path(dest, cache_root, task.task_id)
      return start_sha
  ```

### Task 2: `_init_submodules` — remove every remote, at `check=True`

- [ ] **2.1** In `bakeoff/src/bakeoff/tasks.py`, `find` (verify `grep -cF` prints `1`):

  ```
          _git("remote", "remove", "origin", cwd=dest / sub.path, check=False)
  ```

  `replace`:

  ```
          # EVERY remote, by the name GIT gave it -- not the literal "origin",
          # and not `check=False`. Measured 2026-09-02, git 2.50.1: a clone
          # ALWAYS gets a remote, so "a submodule git chose not to give a
          # remote" does not exist; the only way it is not called `origin` is
          # an operator with `clone.defaultRemoteName` set, and in exactly that
          # case `git remote remove origin` exits 2 ("error: No such remote:
          # 'origin'"), the old `check=False` swallowed it, and
          # `.git/modules/<name>/config` rode into the run tree carrying the
          # host cache path with `git status --porcelain` clean. So the one
          # reachable failure of this guard WAS the leak it exists to remove.
          # Listing and removing instead of refusing fixes that case rather
          # than stopping the matrix on it. `check=True` on the LISTING too,
          # and for a narrower reason than "a listing that fails": at
          # check=False a failed listing returns stdout="", which is
          # byte-identical to a repository with no remotes, so the loop would
          # iterate zero times and the guard would report success having done
          # nothing.
          for remote in _git("remote", cwd=checked).stdout.splitlines():
              if remote.strip():
                  _git("remote", "remove", remote.strip(), cwd=checked)
  ```

  The working directory becomes `checked`, which is `dest / sub.path` bound a few lines above; it is the same path, named once.

- [ ] **2.2** Correct the `reflog expire` comment beside it, which is now wrong in two places (M1). `find` (verify `grep -cF` prints `1` for the block's first line):

  ```
          # check=True (the default). This is a LEAK GUARD, not tidiness:
          # measured 2026-09-01, the submodule's `logs/HEAD` AND
          # `logs/refs/heads/main` each carry `clone: from <host cache path>`
          # after the two rewrites above -- the pruned mirror publishes
          # `refs/heads/main`, so the clone creates a local branch and logs the
          # source twice -- and this expire is the only thing that removes them.
          # `.git/modules/<name>/config` is clean either way, which is why a
          # config-only check sees nothing.
  ```

  `replace`:

  ```
          # check=True (the default). This is a LEAK GUARD, not tidiness:
          # measured 2026-09-01, the submodule's `logs/HEAD` AND
          # `logs/refs/heads/main` each carry `clone: from <host cache path>`
          # after the two rewrites above -- the pruned mirror publishes
          # `refs/heads/main`, so the clone creates a local branch and logs the
          # source twice -- and this expire is the only thing that removes them.
          # There is a THIRD reflog, `logs/refs/remotes/origin/HEAD`, and the
          # removal above is what clears it (measured 2026-09-02, git 2.50.1).
          # `.git/modules/<name>/config` is clean here ONLY BECAUSE that removal
          # ran: it is the one file the removal exists for, which is why a
          # config-only check placed after both sees nothing, and why the
          # removal is no longer allowed to fail quietly.
  ```

- [ ] **2.3** Append to the `_init_submodules` docstring's `THE REFLOG EXPIRE IS A LEAK GUARD.` paragraph, keeping its existing sentences:

  > Both guards are `check=True` and neither is the last word: `materialize`
  > calls `_refuse_host_mirror_path` over the finished tree, which is what makes
  > "no host path survives" a property re-checked against the artifact rather
  > than an inference from four commands having exited zero.

### Task 3: `materialize` — the superproject's two guards match

- [ ] **3.1** `find` (verify `grep -cF` prints `1` for each line):

  ```
      _git("remote", "remove", "origin", cwd=dest, check=False)
      _git("reflog", "expire", "--expire=now", "--all", cwd=dest, check=False)
  ```

  `replace`:

  ```
      # Same shape as `_init_submodules`, one level up, and for the same
      # measured reason (2026-09-02, git 2.50.1): under an operator's
      # `clone.defaultRemoteName` the remote is not called `origin`,
      # `git remote remove origin` exits 2, and `check=False` used to let
      # `.git/config` reach the agent carrying the host cache path.
      for remote in _git("remote", cwd=dest).stdout.splitlines():
          if remote.strip():
              _git("remote", "remove", remote.strip(), cwd=dest)
      # `check=True` (the default) here too. No failure shape was found for
      # this call -- it runs in a repository created one statement ago -- but
      # a leak guard that is allowed to fail quietly is the defect this whole
      # item is about, and raising is affordable for the same reason the
      # alternates check below gives: `materialize` runs before the container,
      # so it costs setup time and zero tokens, and `run_matrix` catches per
      # cell. NOTE the ordering: the setup commit further down appends to
      # `.git/logs/HEAD` AFTER this expire, so that file is non-empty in the
      # finished tree -- non-empty and needle-free, which is what the tests
      # assert rather than a size of zero.
      _git("reflog", "expire", "--expire=now", "--all", cwd=dest)
  ```

- [ ] **3.2** Extend `materialize`'s docstring paragraph beginning ``  `origin` is then removed`` so it says *every* remote is removed and why, and add one sentence naming `_refuse_host_mirror_path` as the post-condition over the finished tree.

### Task 4: unit tests (`bakeoff/tests/test_tasks.py`)

All go in the `# --- submodules ---` section and use the existing `upstream_submodule` / `local_urls` / `_materialize_sub` helpers unless noted. `_materialize_sub` materializes into `tmp_path / "run"` from `tmp_path / "cache"`, so **the submodule directory is `tmp_path / "run" / "vendor" / "libdep"`** — that literal path is what the predicates below compare against.

**The fake, written once and reused by 4.2–4.7.** It patches **`subprocess.run` as `tasks` sees it**, not `tasks._git`. That is the correction review 1's finding 2 requires: `_git`'s `check` branch, which produces the message the `match=` strings pin, lives *inside* `_git` (`tasks.py:1485-1490`), so a fake that replaces `_git` and returns a failing `CompletedProcess` never runs it and nothing raises. Patching one layer down lets the real `_git` run its own check, so the message format is **observed** rather than restated. Verified end to end before this plan was written: the fake below produced `TaskError: git reflog expire --expire=now --all failed (exit 1): boom`, and the exact-`cwd` predicate fired on exactly one call site.

  ```python
  def _fake_run(monkeypatch, predicate, returncode=1, stderr="boom"):
      """Make `tasks`' own `subprocess.run` fail (or no-op) for ONE git call.

      `predicate(argv, cwd)` selects it. Everything else, including this file's
      `_sh` helper, delegates to the real `subprocess.run`.

      Patching `subprocess.run` rather than `tasks._git` is what makes the
      asserted message OBSERVED: `_git`'s `check=True` branch is what formats
      `git <argv> failed (exit N): <stderr>`, and a fake that replaced `_git`
      would never reach it.
      """
      real_run = subprocess.run

      def run(*popenargs, **kwargs):
          argv = popenargs[0]
          if predicate(list(argv), str(kwargs.get("cwd"))):
              return subprocess.CompletedProcess(list(argv), returncode,
                                                 "", stderr)
          return real_run(*popenargs, **kwargs)

      monkeypatch.setattr(tasks.subprocess, "run", run)
  ```

- [ ] **4.1** `test_a_submodule_remote_git_did_not_name_origin_is_still_removed`
  Sets `clone.defaultRemoteName` the way an operator's global config does — `monkeypatch.setenv("GIT_CONFIG_COUNT", "1")`, `monkeypatch.setenv("GIT_CONFIG_KEY_0", "clone.defaultRemoteName")`, `monkeypatch.setenv("GIT_CONFIG_VALUE_0", "upstream")` (`_git` merges `os.environ`, so this reaches every git call including the mirror builds; measured — the whole path still succeeds). Asserts: `_materialize_sub` returns; `_sh("git", "-C", "vendor/libdep", "remote", cwd=run) == ""`; `_sh("git", "remote", cwd=run) == ""`; and the whole-`.git` scan of 4.8's shape is clean. **Red today**: measured leak in `.git/modules/vendor/libdep/config` and `.git/config`, with materialization reporting success.

- [ ] **4.2** `test_a_failed_submodule_remote_listing_is_a_task_error`
  `_fake_run(monkeypatch, lambda argv, cwd: argv == ["git", "remote"] and cwd == str(tmp_path / "run" / "vendor" / "libdep"))`. `pytest.raises(TaskError, match=r"git remote failed \(exit 1\): boom")`. Pins `check=True` on the listing — and note this is the test whose *absence* would let the D1 silence back in, since a `check=False` listing returns `""` and materialization would simply succeed.

- [ ] **4.3** `test_a_failed_submodule_remote_removal_is_a_task_error`
  `_fake_run(monkeypatch, lambda argv, cwd: argv[:3] == ["git", "remote", "remove"] and cwd == str(tmp_path / "run" / "vendor" / "libdep"))`. `pytest.raises(TaskError, match=r"git remote remove \S+ failed \(exit 1\): boom")`.

- [ ] **4.4** `test_a_failed_submodule_reflog_expire_is_a_task_error`
  `_fake_run(monkeypatch, lambda argv, cwd: argv[:2] == ["git", "reflog"] and cwd == str(tmp_path / "run" / "vendor" / "libdep"))`. `pytest.raises(TaskError, match=r"git reflog expire --expire=now --all failed \(exit 1\): boom")`.
  **The predicate is positive and exact, never `cwd != dest`.** Measured: `reflog expire` runs **three** times under one `materialize` — `_build_pruned_mirror` runs it for the superproject's mirror and again for the submodule's, both `cwd=<cache>/repos/prune-…tmp`, both `check=False` — so `cwd != dest` intercepts all three and the test would be green for a reason it does not state.

- [ ] **4.5** `test_a_failed_superproject_reflog_expire_is_a_task_error`
  A task with **no** submodules. `test_tasks.py` has no helper for this case; the two lines the other plain tests use are:

  ```python
      task = load_task(_write_task(tmp_path / "set", upstream))
      repo = tmp_path / "run" / "repo"
  ```

  so the predicate is `lambda argv, cwd: argv[:2] == ["git", "reflog"] and cwd == str(tmp_path / "run" / "repo")`, and the body is `with pytest.raises(TaskError, match=r"git reflog expire --expire=now --all failed \(exit 1\): boom"): materialize(task, repo, tmp_path / "cache")`. Uses the plain `upstream` fixture, not `upstream_submodule`, and needs no `local_urls`. Pins Task 3's tightening.

- [ ] **4.6** `test_a_host_mirror_path_surviving_in_the_submodules_reflog_is_refused`
  `_fake_run(..., returncode=0, stderr="")` with 4.4's predicate — exit **0**, so `_git`'s own `check=True` cannot fire and the **post-condition** is the thing under test. `pytest.raises(TaskError, match=r"\.git/modules/vendor/libdep/logs/HEAD")`. The `match=` is what separates this failure from 4.4's; both are `TaskError`.

- [ ] **4.7** `test_a_host_mirror_path_surviving_in_the_superprojects_reflog_is_refused`
  Same `returncode=0` no-op with 4.5's predicate and its two setup lines, on a task with **no submodules**. `pytest.raises(TaskError, match=r"\.git/logs/HEAD")`. This is the test that pins **D4**: the post-condition is in `materialize`, not in `_init_submodules`, and it runs for a submodule-free task.

- [ ] **4.8** Rename and widen the existing test.
  `test_the_run_tree_carries_no_host_cache_path_for_the_submodule` → `test_the_run_tree_carries_no_host_cache_path_anywhere_under_dot_git`. Body walks the whole `.git` (not `.git/modules`), reading **bytes**, asserting `leaking == []`. Keeps the existing `.git/config` assertions. Adds the corrected non-vacuity assertions:

  ```python
      # NOT vacuous, and the two files need DIFFERENT assertions. Measured
      # 2026-09-02, git 2.50.1: `reflog expire` TRUNCATES rather than unlinks,
      # so the submodule's `logs/HEAD` -- whose expire is the last write to it
      # -- is present at 0 bytes. The superproject's is NOT: `materialize`'s
      # expire runs before the setup commit, and that commit appends 160 bytes
      # ("commit: bakeoff: task setup (test oracle)"). So the superproject file
      # is asserted present, NON-EMPTY and needle-free, which is strictly
      # stronger non-vacuity than a size of zero.
      sub_head = run / ".git" / "modules" / "vendor" / "libdep" / "logs" / "HEAD"
      assert sub_head.is_file() and sub_head.stat().st_size == 0

      super_head = run / ".git" / "logs" / "HEAD"
      assert super_head.is_file() and super_head.stat().st_size > 0
      assert cache not in super_head.read_text()
  ```

  Do **not** substitute `.git/logs/refs/heads/<branch>`: it is 0 bytes here but the branch name is fixture-dependent (`main` in this file, **`master`** on `tomlkit-514`). Extend the docstring with the M1 correction (the removal clears `logs/refs/remotes/origin/HEAD` and `.git/modules/<n>/config`; the expire clears the other two; `.git/logs/*` is the superproject's own pair, which the old `.git/modules`-only scan could not see).

- [ ] **4.9** `test_an_alternates_file_under_a_submodule_is_refused`
  The anchor D3's `objects/` decision otherwise lacks. After a successful `_materialize_sub`, write the needle into the one plain-text file inside `objects/`:

  ```python
      alternates = (run / ".git" / "modules" / "vendor" / "libdep"
                    / "objects" / "info" / "alternates")
      alternates.parent.mkdir(parents=True, exist_ok=True)
      alternates.write_text(f"{tmp_path / 'cache' / 'repos' / 'x.git'}/objects\n")

      with pytest.raises(TaskError, match=r"objects/info/alternates"):
          tasks._refuse_host_mirror_path(run, tmp_path / "cache", "t-001")
  ```

  Docstring states why this is not covered by 4.6/4.7: `materialize`'s dedicated alternates check is `dest/.git/objects/info/alternates` only and runs *before* `_init_submodules`, so the submodule's copy is checked by nothing else, and a later narrowing of the walk to skip `objects/` would leave every other test in this set green.

- [ ] **4.10** `test_a_directory_under_dot_git_that_cannot_be_listed_is_refused`
  The anchor for D3's `onerror`. Skip when running as root (`if os.geteuid() == 0: pytest.skip("root ignores directory permissions")`). After a successful `_materialize_sub`, `os.chmod` a directory under `.git` to `0o000`, assert `pytest.raises(TaskError, match=r"could not be listed")` from `tasks._refuse_host_mirror_path(...)`, and restore the mode in a `finally` so the tmp tree can be cleaned. Measured: `rglob('*')` returns the directory and **nothing inside it**, with no error; `os.walk(..., onerror=)` reports errno 13.

### Task 5: integration tests (`bakeoff/tests/test_integration_submodules.py`)

The module carries `pytestmark` `integration` **and** `task_image`, module-wide; both new tests inherit them. Neither needs the Docker daemon itself, but they run under the same selection as their neighbours.

**Both signatures must name `local_urls`.** It is module-scoped and **not** autouse; it empties `tasks._SUBMODULE_URL_PREFIX` so the fixture's local submodule url is accepted, and without it `derive_submodules` refuses the url and the test dies for the wrong reason. A test that only works because a *neighbour* requested it is order- and `-k`-dependent, and V2/V6 both use selections.

- [ ] **5.1** `def test_the_materialized_run_tree_carries_no_host_mirror_path_under_dot_git(workspace, superproject, local_urls):`
  `materialize(load_task(superproject["task_dir"]), workspace / "run-leak", workspace / "cache")`, then walk the whole `.git` for `str(workspace / "cache" / "repos").encode()`, asserting `[] == hits`. Non-vacuity, in the corrected per-file form of M7: `.git/modules/<SUB_PATH>/logs/HEAD` present at **0 bytes**; `.git/logs/HEAD` present, **non-empty**, and needle-free. **The real init the item asks for**, on the module's real superproject rather than a function-scoped scratch one.

- [ ] **5.2** `def test_a_skipped_reflog_expire_is_refused_on_a_real_run_tree(workspace, superproject, local_urls):`
  The red half. A fresh destination, and `tasks.subprocess.run` swapped **by hand** in a `try/finally` (the module's fixtures are module-scoped and `monkeypatch` is function-scoped; the neighbouring `local_urls` fixture already restores an attribute this way) so the submodule's `reflog expire` returns exit 0 and does nothing. The predicate is the same positive, exact `cwd` comparison as 4.4, against `<dest>/<SUB_PATH>`. `pytest.raises(TaskError, match=r"logs/HEAD")`, and assert the message also names the task id.

  **Considered and rejected:** asserting from *inside* a `RunContainer` with `grep -rlF <cache>/repos /repo/.git`. `/repo` is a bind mount of exactly the tree the host-side scan reads, so the container makes no independent observation; and `grep`'s presence in the eval-agent image is not something this plan measured. The bind-mount identity is already covered by this module's blob-comparison test.

### Task 6: mutation anchors (`bakeoff/scripts/mutation_check.py`)

Three tuples in the `tasks:` group, beside the existing `"tasks: populate a submodule from the unpruned mirror"`. All `find` strings are given at **true** source indentation and must each `grep -cF` to exactly `1` before the anchor is added; if one is not unique after items 1–9 land, extend it with the following line rather than shortening it.

- [ ] **6.1**

  ```
    (
        # The post-condition, deleted. Every unit test that drives a HEALTHY
        # materialization stays green -- the tree is clean, so the check never
        # fires -- while a run tree whose guards silently missed goes to the
        # agent carrying the operator's cache layout and a remote the container
        # cannot resolve, with `git status --porcelain` clean.
        "tasks: stop refusing a run tree that carries the host mirror path",
        "src/bakeoff/tasks.py",
        "            if hit:",
        "            if False:",
        "tests/test_tasks.py -k host_mirror_path_surviving",
        "not integration",
    ),
  ```

- [ ] **6.2**

  ```
    (
        # Back to the literal "origin", KEEPING check=True -- so the mutant is
        # louder than the pre-fix code, not identical to it, and the arm goes
        # red on the raise rather than on a remote assertion. Measured
        # 2026-09-02, git 2.50.1: an operator with `clone.defaultRemoteName`
        # set gets a submodule whose only remote has another name, so `git
        # remote remove origin` exits 2 -- which the pre-fix `check=False`
        # swallowed while `.git/modules/<name>/config` kept the host cache
        # path. Only the arm that sets that config sees either version.
        "tasks: remove only a remote literally named origin",
        "src/bakeoff/tasks.py",
        "        for remote in _git(\"remote\", cwd=checked).stdout.splitlines():",
        "        for remote in (\"origin\",):",
        "tests/test_tasks.py -k did_not_name_origin",
        "not integration",
    ),
  ```

- [ ] **6.3**

  ```
    (
        # The walk's error handler, dropped. Measured 2026-09-02: `Path.rglob`
        # suppresses the PermissionError a mode-000 directory raises and
        # returns the directory with nothing inside it, so the scan reports
        # CLEAN over files it never opened -- the same silence the whole item
        # removes, arriving through the walk instead of through a guard.
        "tasks: let the leak scan skip a directory it cannot list",
        "src/bakeoff/tasks.py",
        "    for root, dirnames, filenames in os.walk(base, onerror=_refuse_walk_error):",
        "    for root, dirnames, filenames in os.walk(base):",
        "tests/test_tasks.py -k cannot_be_listed",
        "not integration",
    ),
  ```

### Task 7: docs

- [ ] **7.1** `TASKS.md`: **delete** the item-10 bullet ("The submodule's `remote remove` is `check=False`…") — it is closed, and its stated conclusion ("Leave the code as it is") is superseded by M4, which the commit message must say in one sentence.
- [ ] **7.2** `TASKS.md`: add **one new P2 bullet** covering **all three remaining hard-coded `"origin"` sites** (M5b), with their measured behaviour and their *different* consequences: `ensure_mirror:1569`/`:1577` are a **dead refresh path** (`fetch --prune origin` exits 128, `fatal: 'origin' does not appear to be a git repository`, so a cached mirror that does not yet hold `base_sha` can never be refreshed — loud, via `ensure_mirror`'s own `cat-file -e`, but an unexplained mid-matrix refusal for a preventable condition); `_build_pruned_mirror:2191` is a **future-restoration hazard** (the published mirror keeps `remote.upstream.fetch=+refs/*:refs/*` and `mirror=true`, `_verify_pruned` asserts nothing about remotes, and it does **not** reach the run tree). State that all three are gated by `HANDOFF.md` (its scope line covers `ensure_mirror`; "Still open" item 2 names it), and that the fix for `:2191` is not "remove every remote there" but "decide what `_verify_pruned` should assert about remotes" — a fourth term in the argument that file spends three rounds warning against adding casually. One bullet, all three sites, because a bullet naming one of three invites a partial fix.
- [ ] **7.3** `tasks/todo.md`: add a review section for this item — the M1/M5/M5b tables, M4's inversion of the backlog's own conclusion, D1's rejection of the literal `check=True`, M7's two-different-assertions correction, and the OQ1 ruling.
- [ ] **7.4** No edit to `HANDOFF.md`: nothing in `ensure_mirror` / `ensure_pruned_mirror` / `_build_pruned_mirror` / `_verify_pruned` changes. (OQ1 is *about* those functions; it is filed, not acted on.)

---

## Verification

- [ ] **V1 — unit suite.** `cd bakeoff && .venv/bin/python -m pytest tests/ -q`. Expect the recorded baseline **+9 passed** (4.1–4.7, 4.9, 4.10 are nine new unit tests; 4.8 is a rename, not an addition) and **deselected +2** (5.1 and 5.2, which `addopts = "-m 'not integration'"` deselects). Naming the deselected delta is the half that proves the two integration tests were collected and carry both markers. 4.10 reports **skipped** rather than passed if the suite is somehow run as root; reconcile any other delta before continuing.
- [ ] **V2 — the new tests, named.** `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -v -k "did_not_name_origin or remote_listing or remote_removal or reflog_expire or host_mirror_path or anywhere_under_dot_git or alternates_file_under_a_submodule or cannot_be_listed"`. Every one must pass, and each of 4.2–4.7 must be checked to fail for the **right** reason by reverting its own edit in isolation: the `match=` strings are what distinguish a `check=True` failure (`git … failed (exit 1): boom`) from a post-condition failure (a path), and a test that passes on the wrong `TaskError` is the failure mode this whole item is about.
- [ ] **V3 — red-before-green on 4.1.** Before applying Task 2/3, run 4.1 alone; it must **fail**, and the failure must be the remote assertion or the scan, not an error. This is the only test in the set whose red state proves the *measured* defect rather than a mutation.
- [ ] **V4 — the fake actually raises.** Before writing 4.2–4.7's assertions, run one of them with the fake in place and **no** `pytest.raises`, and confirm a `TaskError` escapes `materialize` with the expected message. Review 1's finding 2 was exactly a fake that intercepted three calls and then let materialization complete; the interception count is not evidence.
- [ ] **V5 — mutation check, solo.** `cd bakeoff && .venv/bin/python scripts/mutation_check.py`. All three new anchors must report their named test going red; the total moves by **+3**. Nothing else may run in the tree while this runs.
- [ ] **V6 — the §6.6 logger gate.** `cd bakeoff && .venv/bin/python scripts/verify_logger.py` → `GATE PASSED`. Needs a Docker daemon; it is not weakened by this change (the new integration tests carry `task_image`, which that gate deselects).
- [ ] **V7 — integration.** `cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" tests/test_integration_submodules.py --basetemp="$HOME/.cache/bakeoff-pytest"`. `--basetemp` under `$HOME` is mandatory; see that module's docstring.
- [ ] **V8 — a real task materializes unchanged.** `cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight --task-set ~/.cache/bakeoff-probe/taskset --tasks tomlkit-514-inline-table-comment-separator`. Must print `start_sha e1d72b883d2e452ca14835047e2fa7db02cdc4d8` — **the same value measured before this change** — and reach a green preflight.
- [ ] **V9 — the taskset preflight is unaffected.** `cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only`. No cached verdict is invalidated by this commit (no manifest and no `PREFLIGHT_VERSION` changes), so this should be entirely cache hits.
- [ ] **V10 — the operator-config case, by hand, on a real task.** With `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=clone.defaultRemoteName GIT_CONFIG_VALUE_0=upstream`, materialize `tomlkit-514` and confirm `git remote` is empty in both the run tree and `tests/toml-test`, and that a whole-`.git` scan for `<cache>/repos` is clean. Before this commit, measured: materialization succeeds and two files leak.
- [ ] **V11 — the scan's cost and memory on the largest corpus member.** Materialize `pytest-10210` and time `_refuse_host_mirror_path`; confirm the chunked read keeps peak RSS flat against the 40.3 MB pack (the whole-file version measured 109 MB).

---

## What this does NOT do

- **It does not touch `ensure_mirror`, `_build_pruned_mirror`, `ensure_pruned_mirror` or `_verify_pruned`.** Those are gated by `HANDOFF.md`. The three cache-side hard-coded `"origin"` uses have the identical `clone.defaultRemoteName` exposure and **different, non-run-tree** consequences — see M5b and **OQ1**. They are filed in one `TASKS.md` bullet, not fixed here.
- **It does not scan for `cache_root` itself, only `<cache_root>/repos`.** A future call site that wrote the run tree's own path into `.git` would not be caught. Measured: nothing does today, and the narrow needle is what keeps the check from becoming a false positive on the five run-tree roots (M6). The constraint that keeps it correct — *a run tree may live anywhere under the cache root except `repos/`* — is stated for round-2 item 3, which is moving all five.
- **It does not scan the run tree outside `.git`.** A host path in a tracked file is a property of `base_sha` and would move `start_sha`; a host path in an untracked file is removed by the `git clean -xfd` that already runs.
- **It does not add a preflight assertion, and `PREFLIGHT_VERSION` does not move.** D7.
- **It does not record anything in the `RunRecord`.** A refusal here happens before a container, before a token, and `run_matrix` already catches it per cell; there is no run to describe.
- **It does not change what a declared-unneeded submodule (item 2) does.** Such a submodule is never initialised, no mirror is built for it, `.git/modules/<name>` never exists, and neither guard runs for it. The post-condition still runs, over a tree that has nothing of it to find.
- **It does not claim `git reflog expire` has a measured failure mode.** None was found; the tightening in Task 3 is argued from cost and from symmetry (D2), not from a measurement.
- **It does not remove the dedicated `objects/info/alternates` check in `materialize`.** The generic scan would catch it, but only with a generic message; the existing one names `--dissociate`, and `HANDOFF.md` is explicit that the alternates path must be asserted absent and never unlinked.
- **It does not bound the scan against a hostile tree.** The chunked read bounds memory and the walk is one pass, but a `.git` with pathological file counts is not defended against; nothing writes into `.git` between the clone and this call except git itself.

---

## Open questions

Review 1 ruled on all three of the previous round's open questions. OQ2 and OQ3 are settled and folded into D2 and D1 respectively; the record of the rulings is in "Review 1 → changes". **OQ1 remains open as a filed backlog item, widened.**

- **OQ1 — the cache-side `"origin"` sites (filed, not fixed; ruling adopted).** Three of the five hard-coded uses are cache-side (M5b) and none reaches the run tree, so none belongs in a commit about run-tree leaks. The bullet Task 7.2 writes covers all three. What is genuinely open is the *shape* of the eventual fix for `_build_pruned_mirror:2191`: not "remove every remote there" but "decide what `_verify_pruned` should assert about remotes", which adds a fourth term to a post-condition `HANDOFF.md` spends three rounds warning against adding terms to casually. Whoever picks it up needs that measurement first.
- **OQ4 — ~~should `_refuse_host_mirror_path` also run in `grader`/`oracle`'s trees after *their* post-materialization steps?~~ CLOSED, ruled NO by review 2.** Both call `materialize` and inherit the check as of this commit. What they do afterwards is `git apply` a stored diff and `_refresh_index` inside the container — neither writes a remote, a reflog or a config, so neither can re-introduce a `<cache_root>/repos` path; and a second scan would be a post-condition on a *grading* step placed in a *materialization* helper, which is the layering mistake D4 rejects in the other direction. The check stays where the property is created. Recorded rather than deleted so the next reader sees it was asked and answered.

---

## Review 1 → changes

Every finding is addressed; **none is disputed**. Blocking findings 1–3 were reproduced independently before the fix was written.

| # | finding | change |
|---|---|---|
| 1 | **BLOCKING** — `.git/logs/HEAD` is 160 bytes after the setup commit, not 0; 4.8 and 5.1 fail as written | Re-measured and confirmed (`82bb211e… e1d72b88… bakeoff <eval@pindrop.test> 0 +0000\tcommit: bakeoff: task setup (test oracle)`, 160 bytes; the submodule's is 0). **M7 rewritten** to say "after the expire runs" and to separate the two files. 4.8 and 5.1 now assert `st_size == 0` for the **submodule's** `logs/HEAD` only, and *present, non-empty, needle-free* for the superproject's. The `refs/heads/<branch>` substitute is explicitly refused (branch is `main` here, `master` on `tomlkit-514`). Task 3.1's comment now states the ordering. |
| 2 | **BLOCKING** — the fake returns a failing `CompletedProcess` instead of raising, so nothing raises | Confirmed: `_git`'s check branch is what formats the asserted message. **The fake now patches `tasks.subprocess.run`**, one layer down, so the real `_git` runs its own check and the message is observed. Written out once as `_fake_run` in the Task 4 preamble and reused by 4.2–4.7. Verified end to end before revising: it produced `TaskError: git reflog expire --expire=now --all failed (exit 1): boom`. New **V4** makes "confirm it actually raises" a verification step rather than an assumption. |
| 3 | **BLOCKING** — `cwd != dest` also intercepts both pruned-mirror `reflog expire` calls | Confirmed (three `reflog expire` calls per `materialize`; two are `cwd=<cache>/repos/prune-…tmp`). **Every predicate is now positive and exact**, comparing against a literal path written out in the plan (`tmp_path / "run" / "vendor" / "libdep"`, `tmp_path / "run" / "repo"`, `<dest>/<SUB_PATH>`). 4.4 carries the measurement as its reason. M2 gained a sentence pointing at it. |
| 4 | the root cause has five sites; the plan named three | Confirmed by `grep -n '"origin"'` and by measuring `fetch --prune origin` → exit 128 under `clone.defaultRemoteName`. New **M5b** tables all five with their `check=`, their behaviour under that config, and whether this commit fixes them. **Task 7.2 now writes one bullet over all three** remaining sites, distinguishing the dead refresh path from the future-restoration hazard. |
| 5 | `Path.rglob` silently skips an unreadable directory | Confirmed (`rglob('*') -> ['sub']`, no error; `os.walk(onerror=)` reports errno 13). **Task 1.1 now uses `os.walk(base, onerror=_refuse_walk_error)`**, sorting each directory's entries for determinism. D3 says the walk raising is as load-bearing as the read raising. New test **4.10** and new mutation anchor **6.3** pin it. |
| 6 | V1's `+9` contradicted its own parenthetical | Corrected — and the count changed again because findings 5 and 9 add two unit tests. V1 now reads **+9 passed and deselected +2**, and says why naming the deselected delta is the half that proves the markers. |
| 7 | `PREFLIGHT_VERSION` is `"13"` at HEAD 8232032, not `"14"` | Confirmed (`git show HEAD:…` prints `"13"`; the worktree reads `"14"` because items 1–9 are in flight). **The parenthetical is deleted**; the surrounding "at whatever value items 1–9 leave it" is the only thing an implementer needs. |
| 8 | 5.1/5.2 do not name `local_urls` | **Both signatures are now written out** — `def test_…(workspace, superproject, local_urls):` — with the reason (module-scoped, not autouse; a test that works only because a neighbour requested it is `-k`-dependent, and V2/V7 both use selections). |
| 9 | D3's `objects/` decision has no anchor | New unit test **4.9** plants `<cache>/repos/x.git` in `.git/modules/vendor/libdep/objects/info/alternates` and asserts the refusal names it. D3 now says explicitly that this is the decision a later narrowing could undo with every other test green. |
| 10 | `read_bytes()` on a whole packfile is unbounded | Confirmed (`pytest-10210`: 40.3 MB largest file, 109 MB peak RSS). **Task 1.1 now reads in 1 MiB chunks with a `len(needle) - 1` overlap**, via a named `_file_contains`; the overlap arithmetic was verified at every offset 0–39 against an 8-byte chunk. M6 carries the corpus table; new **V11** checks the bound. |
| 11 | 4.5 names a helper that does not exist | Confirmed — `test_tasks.py` has `_materialize_sub` only for the submodule case. **The two lines are written out** (`task = load_task(_write_task(tmp_path / "set", upstream))`, `repo = tmp_path / "run" / "repo"`) in 4.5 and reused by 4.7. |
| 12 | 6.2's comment misdescribes its mutant | **Rewritten**: the mutant keeps `check=True`, so it is *louder* than the pre-fix code rather than identical to it, and the arm goes red on the raise rather than on a remote assertion. |
| 13 | M6 names two of five run-tree roots, and item 3 is moving them | **All five are tabled** (`preflight-tree`, `artifacts`, `oracle-tree`, `grade-tree`, `grade-preflight-tree`), with the constraint stated as a rule item 3 must preserve: *a run tree may live anywhere under the cache root except `repos/`.* Repeated in the Global Constraints and in "What this does NOT do". |
| OQ1 | file, do not fix; widen to `ensure_mirror` | **Adopted.** M5b + Task 7.2 (one bullet, three sites); OQ1 restated as "the shape of the eventual `_verify_pruned` fix is what is open". |
| OQ2 | superproject pair stays in this commit | **Adopted.** D2 gained the reviewer's second reason (splitting puts one measurement in two commits and leaves the first commit's table contradicting its diff). OQ2 removed from Open questions. |
| OQ3 | no benign shape; say what `check=True` on the listing buys | **Adopted.** Folded into **D1** as its own paragraph: at `check=False` a failed listing returns `""`, byte-identical to "no remotes", so the loop iterates zero times and the guard reports success having done nothing. OQ3 removed; 4.2's entry names it as the test that keeps that silence out. |

### Review 2

**APPROVED, 0 open findings.** Two nits and one ruling, all folded in below. No
design decision, measurement, task, test or version claim changed; the diff
against the review-1 revision is three edits.

| # | nit / ruling | change |
|---|---|---|
| N1 | `_file_contains` has no empty-needle guard | Added, with the reason written out rather than left as hygiene: for an empty needle `overlap` is `-1`, which is **truthy**, so the carry-over evaluates `[-(-1):]` == `[1:]` and retains everything read so far — **the whole file in memory**, which is the single property this function exists to avoid. It fails by silent growth, not by an exception, which is why it is worth a line. It raises `ValueError`, not `TaskError`: this is a programming error in a caller, not a condition of a task. **Deliberately unanchored** — the only caller's needle is `<cache_root>/repos` and can never be empty, so no mutation can reach it; the repo already keeps one such check (`materialize`'s alternates guard) on the same "expected unreachable is what every defect in this subsystem has been" argument, and this note is here so the next reader does not delete it for lack of a test. |
| N2 | three of M6's five call-site line citations are wrong | Four were, in fact: `run_matrix.py:264`→**278**, `:337`→**361**, `grade.py:297`→**304**, `grader.py:2013`→**2014**; `oracle.py:289` was right. Re-verified with `git show HEAD:<file> \| grep -n 'materialize(task,'` at HEAD 8232032. The table now carries the **function name** beside each number and a standing instruction to re-locate by the quoted call — `grader.py` moved by one line between two reads inside a single session, which is the same reason the Global Constraints already forbid addressing edits by line number. |
| OQ4 | should the post-condition also run in `grader`/`oracle`'s trees after their own post-materialization steps? | **Ruled NO; closed.** What those two do after `materialize` is `git apply` a stored diff and `_refresh_index` inside the container — none of which writes a remote, a reflog or a config, so none can re-introduce a `<cache_root>/repos` path. A second scan would also put a post-condition on a *grading* step inside a *materialization* helper, the layering mistake D4 rejects in the other direction. Kept in Open questions as struck-through-and-answered rather than deleted, so the next reader sees it was asked. |

**Open questions remaining after review 2:** OQ1 only (the three cache-side
`"origin"` sites, filed to `TASKS.md` by Task 7.2, gated by `HANDOFF.md`).

---

## Sentences that belong in `CLAUDE.md` (do NOT add them on this branch)

- Under **"The run tree holds no object outside `base_sha`'s history"**, as its host-path sibling: *"And no file under the run tree's `.git` names the operator's mirror cache. Four scrubs run — two `git remote remove` loops and two `git reflog expire`s — and every one of them could miss in a way `git status --porcelain` renders as a clean tree. Measured 2026-09-02, git 2.50.1: an operator with `clone.defaultRemoteName` set gets `git remote remove origin` exiting 2, and under the old `check=False` both `.git/config` and `.git/modules/<name>/config` reached the agent carrying the host cache path with materialization reporting success. `_refuse_host_mirror_path` re-checks the invariant against the finished artifact, on `<cache_root>/repos` — not on `cache_root`, which is a prefix of every one of the five run-tree roots — and it walks with `os.walk(onerror=)` rather than `Path.rglob`, which suppresses the error a directory it cannot list raises and reports clean over files it never opened."*
- Under **"Silence is the enemy"**: *"A `check=False` is a claim that the failure it tolerates is ordinary. Measure the failure before making the claim: item 10's tolerated exit turned out to be the only reachable one, and it was the leak. The same applies to a `check=False` on a **listing** — an empty stdout is byte-identical to an empty result, so the loop below it does nothing and reports success."*
