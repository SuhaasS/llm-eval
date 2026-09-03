# Round 2, item 18: nested submodules

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision:** review 1 (`.superpowers/broaden/round2/plan-18-review-1.md` —
REVISE, 16 findings, 6 blocking) is folded in. See *Review 1 → changes* at the
end for the finding-by-finding map, and *Open questions and rulings* for the
five questions, three of which the review ruled on.

**Goal:** Let a task be cut from a repository whose `base_sha` carries a
submodule that has a submodule of its own. Two levels, and the third is refused
by the same refusal that refuses the second today, for the same measured reason.

**Architecture:** `derive_submodules` becomes a pre-order recursion over
`(mirror, sha)` pairs — the two readers it already uses are generic over that
pair (M2), so the level-1 body is reused verbatim one level down. Each level's
submodules get their own `ensure_pruned_mirror` entry, keyed by `(url, sha)` as
today. `Submodule` gains `depth` and `parent`; `path` becomes the full
superproject-relative path so every path-shaped refusal keeps comparing like
with like. `_init_submodules` iterates the flat pre-order tuple and runs
`git submodule update --init` **with `cwd` at the parent's working tree**.
`images._extract_submodules` takes one `git archive` per level. `preflight`
reads `git submodule status --recursive` and builds its authoritative path set
by recursing `git ls-files -s -z` per level, guarded by a
`rev-parse --show-prefix` probe that is load-bearing (M11/M12).
`grader.py`, `checkpoints.py` and `schema.py` are untouched.

**Depth cap: 2.** `_MAX_SUBMODULE_DEPTH = 2`. Argued in D3.

**THE TRANSCRIPTION RULE FOR THIS PLAN.** This item lands **fifth**, after
round-2 items 2, 10, 16 and 17, each of which edits the same three functions.
Every code block below is therefore written as **the shape after those four**,
and **no block in this plan may be pasted without re-locating it in the tree
first**. Adopt item 10's convention: for every edit, `grep -cF '<find>'` must
print `1` before the edit is made; if it prints `0`, the neighbouring item
landed a different form — keep that form and apply only the change this plan
names. Findings 1, 6 and 7 of review 1 all had one cause: blocks written
against today's source that would silently revert a neighbour.

**Tech Stack:** Python 3.12 (the harness venv), pytest, git 2.50.1 (Apple
Git-155) on the host, git 2.54.0 in the container, Docker for the integration
and gate legs. All code under `bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3,
§3.7, §5.1, §5.6, §6.4). The subsystem this extends:
`docs/superpowers/plans/2026-09-01-broaden-6-submodules.md` — read **D2**
(refusal 3, the one this item lifts), **D3**, **D4**, **D5**, **D7** and **D10**
before Task 1. `HANDOFF.md` — read the paragraph headed *"If you are about to
add a fourth term to the fingerprint, stop"* before Task 1; this plan adds
`ensure_pruned_mirror` **callers** and changes nothing inside it, which is the
whole of its interaction with that file. Round-2 process:
`.superpowers/broaden/round2/CONTEXT.md`.

---

## The measured defect

`TASKS.md` (search `Nested submodules`):

> **Nested submodules.** `submodule update --init --recursive` plus one pruned
> mirror per `(inner url, inner gitlink)`, and preflight's `git submodule
> status --recursive`. Refused today because the untested path leaves the inner
> directory empty, which reads as clean. A deferral, not a defect (broadening
> 6).

The refusal is `tasks._refuse_submodule_conflicts`, second branch:

```
{task_id}: submodule {path} at {sha} declares submodules of its own. `git
submodule update --init` does not recurse, so the inner directory would arrive
empty -- and an empty submodule directory leaves `git status --porcelain` clean,
so nothing downstream would say so.
```

**The claim in that message is measured and it is exactly right.** M16 below
reproduces it at two levels: with `vendor/lib` populated and
`vendor/lib/vendor/deep` empty, the superproject's `git status --porcelain` is
empty, the inner's own `git status --porcelain` is empty, `git diff HEAD` is
empty, and `git submodule status` **without** `--recursive` reports a clean
leading space. Only `--recursive` says anything. That is the Phase 0c shape —
an environment defect scored as capability on every arm — arriving through the
dataset, one level further down than broadening 6 closed it.

### Corpus evidence: four of the corpus's five gitlinks are measured flat; the fifth is unreachable

Rewritten after review-1 finding 14, which was right that the first draft
asserted a measured negative the table under it did not support. Measured
2026-09-02: `git ls-tree -r HEAD` over `~/.cache/bakeoff-probe/clones` for the
gitlinks, then a `git fetch --depth=1 --filter=blob:none <url> <pinned sha>`
into a scratch bare repo per submodule, then `cat-file -e <sha>:.gitmodules`
plus a `160000` count at that sha:

| superproject | gitlink | pinned sha | `.gitmodules` at that sha | `160000` entries |
|---|---|---|---|---|
| `python-poetry/tomlkit` | `tests/toml-test` | `08ed8697` | **absent** (exit 128, *"path '.gitmodules' does not exist"*) | 0 |
| `eemeli/yaml` | `tests/yaml-test-suite` | `50861920` | **absent** | 0 |
| `eemeli/yaml` | `docs-slate` | `413d60f7` | **absent** | 0 |
| `eemeli/yaml` | `tests/json-test-suite` | `984defc2` | **absent** | 0 |
| `tobymao/sqlglot` | `sqlglot-integration-tests` | `7e55dda8` | **unmeasurable** | — |

The fifth is `git@github.com:fivetran/sqlglot-integration-tests.git`; over https
it answers *"could not read Username for 'https://github.com'"*, i.e. it is
private. It is also **exactly** the submodule round-2 item 2 declares unneeded,
so the harness never fetches it and never reads its tree at all.

**So every gitlink the harness can reach in the screened corpus is flat**, and
the one it cannot reach is one it never touches. This item unblocks a shape no
screened repository has shown. That is the central input to the depth cap (D3):
a broadening built on a prediction, whose cheapest honest version moves the
floor by exactly one level.

---

## Measurements

Taken **2026-09-02**, host **git 2.50.1 (Apple Git-155)**, darwin 25.5.0, in
scratch repositories under `~/.cache/bakeoff-probe/scratch-nested` (under
`$HOME`, per the `--basetemp` rule in `CLAUDE.md`). Fixture: `outer`
(`top.txt`, `tests/test_x.py`) → gitlink `vendor/lib` → `inner` (`mid.txt`) →
gitlink `vendor/deep` → `innermost` (`deep.txt`), each with a bare mirror
beside it; plus `L0→L1→L2→L3` for M18, `colA/colB/colC` for M14, and `orph` for
M21. Review 1 rebuilt the fixture from scratch and reproduced M1, M2, M4, M5,
M10, M11, M12, M13, M14, M15, M16, M17, M18, M19 and M20 exactly, and added the
level-2 half of M11; the shas below are from the first build and differ from the
review's, which is a timestamp difference and nothing else.

**M1 — `git ls-tree -r` in the outer shows only the FIRST-level gitlink.**

```
100644 blob 06949a7…    .gitmodules
100644 blob 83b3766…    tests/test_x.py
100644 blob af740ef…    top.txt
160000 commit 8a71042d15121e6d7ca30231cbd08d054071eb1c    vendor/lib
```

`-r` does not descend through a gitlink. Same at the index: `git -C outer
ls-files -s` lists exactly one `160000` entry, and `git -C runA/vendor/lib
ls-files -s` lists `160000 … vendor/deep`. **So the derivation must ask a
second repository, and preflight must run a second `ls-files`.**

**M2 — `derive_submodules`' two readers are already generic over
`(mirror, sha)`.** Against the *inner* bare mirror at the inner gitlink sha:

```
$ git -C inner.git config --blob 8a71042d…:.gitmodules --list -z | tr '\0' '\n'
submodule.vendor/deep.path
vendor/deep
submodule.vendor/deep.url
<innermost url>
$ git -C inner.git ls-tree -r -z 8a71042d… | tr '\0' '\n'
100644 blob de34cb6…    .gitmodules
100644 blob 5b16fcc…    mid.txt
160000 commit 57c7df7b41f72225c4e8baeb74b9296231b29880    vendor/deep
```

**This is what makes the recursion a parameter change rather than a new parser.**

**M3 — `git submodule status --recursive` does not name a level it cannot
reach.** In a run tree where nothing is initialised, `git submodule status` and
`--recursive` produce the *identical* single line `-8a71042… vendor/lib`. The
deeper entry appears only once `vendor/lib` is populated. Both readers degrade
the same way, which is why a missing level-2 entry under an uninitialised level
1 must **not** be reported as the two readers disagreeing (D6).

**M4 — one `submodule update --init --recursive` with `-c` url overrides
populates every level.** The `-c` propagates into the recursion through
`GIT_CONFIG_PARAMETERS`; `ls -A runA/vendor/lib/vendor/deep` → `.git`,
`deep.txt`. And yet:

**M5 — after M4, `git submodule status --recursive` reads `-` for the level it
just populated.**

```
 8a71042d15121e6d7ca30231cbd08d054071eb1c vendor/lib (heads/main)
-57c7df7b41f72225c4e8baeb74b9296231b29880 vendor/lib/vendor/deep
$ git --git-dir=runA/.git/modules/vendor/lib config --get-regexp '^submodule\.'
(no output)
```

`_init_submodules`' documented *"THE URL IS PERSISTED, THEN REWRITTEN"* failure,
one level down: a transient `-c` populates the tree but `submodule init` skips
registration because the value is already visible, so the parent's own config
never gets it and the marker stays `-`. Preflight reads exactly that character.
**The one-shot `--recursive` form produces a populated tree the gate NO-GOes.**

**M6 — `.git` is a file at every level, and the gitdir pointers chain.**

```
$ cat runA/vendor/lib/.git              → gitdir: ../../.git/modules/vendor/lib
$ cat runA/vendor/lib/vendor/deep/.git  → gitdir: ../../../../.git/modules/vendor/lib/modules/vendor/deep
```

**M7 — the nested module directory is `<outer module>/modules/<inner>`**, i.e.
`runA/.git/modules/vendor/lib/modules/vendor/deep`, inside `/repo/.git`.
Broadening 6's D6 holds unchanged one level down.

**M8 — BOTH levels leak the host cache path, in four files each**, before any
guard runs: `config` (`remote.origin.url`), `logs/HEAD` and
`logs/refs/heads/main` (each *"clone: from <host cache path>"*), and
`logs/refs/remotes/origin/HEAD`.

**M9 — the existing guard pair is sufficient PER LEVEL, and reaches no other
level.** `git -C runA/vendor/lib remote remove origin` then `reflog expire
--expire=now --all` (exit 0) leaves `logs/HEAD` and `logs/refs/heads/main`
present but **empty**, removes `logs/refs/remotes/origin/HEAD` with the remote,
and `grep -rl <cache path>` under that module directory (excluding `modules/`)
returns nothing. The same grep under `runA/.git/modules/vendor/lib/modules`
still returns all four files.

**M10 — `git ls-files --recurse-submodules` is unusable here, and its failure
mode is the worst one.** At the outer, with `vendor/lib` initialised, the
listing contains `vendor/lib/.gitmodules`, `vendor/lib/mid.txt` and
`160000 … vendor/lib/vendor/deep` — and the outer's own gitlink `vendor/lib` is
**absent**. With `vendor/lib` uninitialised the same command lists
`160000 … vendor/lib` and nothing deeper. **The authoritative set this flag
produces changes meaning with the state being checked**: on a healthy tree
`vendor/lib` falls out, its status line lands in `unmatched`, and the gate
NO-GOes a correct tree.

**M11 — `git -C <empty submodule dir> ls-files` walks UP and returns `./`, at
every level.** Level 1 (`runF`, nothing initialised):

```
$ cd runF && git -C vendor/lib ls-files -s -z | tr '\0' '\n'
160000 8a71042d… 0	./                     (exit 0)
$ git -C vendor/lib ls-files --full-name -s
160000 8a71042d… 0	vendor/lib
```

Level 2 (`runE`, level 1 initialised, level 2 empty) — **review 1's addition,
re-measured here**:

```
$ git -C vendor/lib/vendor/deep ls-files -s -z | tr '\0' '\n'
160000 7d941c7a… 0	./                     (exit 0)
```

A recursion that descended unguarded would prefix that to `vendor/lib/./` and
file a gitlink that does not exist — a **fabricated observation**, worse than
the empty directory it was looking for. `--full-name` does not save it. **The
guard is needed at every level.**

**M12 — the guard is `rev-parse --show-prefix`, not `--show-toplevel`.**
Measured on `runG` (both levels healthy), `runF` (level 1 empty) and `runE`
(level 2 empty):

| directory | state | `--show-toplevel` | `--show-prefix` |
|---|---|---|---|
| `vendor/lib` | initialised | `<tree>/vendor/lib` | *(empty)* |
| `vendor/lib/vendor/deep` | initialised | `<tree>/vendor/lib/vendor/deep` | *(empty)* |
| `tests` | ordinary subdirectory | `<tree>` | `tests/` |
| `vendor/lib` | **uninitialised** | `<tree>` | `vendor/lib/` |
| `vendor/lib/vendor/deep` | **uninitialised** | `<tree>/vendor/lib` | `vendor/deep/` |

**`--show-prefix` is empty if and only if the directory is its own repository**,
at every level, and it needs no path composed by the caller — which is why D6
adopts it over the equality form the first draft used (review-1 finding 9).

**M13 — level-by-level, persisted url, guards per level: everything reads
clean.** The sequence this plan implements, run by hand:

```
git -C runC config submodule.vendor/lib.url <inner mirror>
git -C runC -c protocol.file.allow=always submodule update --init -- vendor/lib
git -C runC config submodule.vendor/lib.url <declared url>
git -C runC/vendor/lib remote remove origin
git -C runC/vendor/lib reflog expire --expire=now --all
git -C runC/vendor/lib config submodule.vendor/deep.url <innermost mirror>
git -C runC/vendor/lib -c protocol.file.allow=always submodule update --init -- vendor/deep
git -C runC/vendor/lib config submodule.vendor/deep.url <declared url>
git -C runC/vendor/lib/vendor/deep remote remove origin
git -C runC/vendor/lib/vendor/deep reflog expire --expire=now --all
```

```
$ git -C runC submodule status --recursive
 8a71042d15121e6d7ca30231cbd08d054071eb1c vendor/lib (heads/main)
 57c7df7b41f72225c4e8baeb74b9296231b29880 vendor/lib/vendor/deep (heads/main)
```

Both markers a leading space, both with the `(heads/main)` describe suffix (the
pruned mirror publishes `refs/heads/main` at every level), and `grep -rl <cache
path>` over the whole run tree hits only the **checked-in `.gitmodules`** files,
which in this fixture literally contain local paths because it was built with
`submodule add <local path>`. **This is the route the plan takes.** Review 1
re-ran the same sequence over the name-collision fixture (M14) and confirms it
defeats the collision: both markers a leading space, both HEADs equal their
gitlinks.

**M14 — a single `-c submodule.<name>.url=` collides across levels**, because
names are per-repository and need not be unique. `colA` declares
`[submodule "dep"]` at path `lib`; `colB` declares `[submodule "dep"]` at path
`dep`. One `-c submodule.dep.url=<colB mirror>` for a `--recursive` update:

```
fatal: git upload-pack: not our ref 46cc0bdc…
fatal: Failed to recurse into submodule path 'lib'        exit=128
$ ls -A runD/lib/dep                  → .git
$ git -C runD/lib/dep rev-parse HEAD  → d4b2d00…   (colB's sha, not colC's)
```

Loud here because the wrong mirror lacks the sha; silent and wrong where the two
same-named submodules are a repository and a fork of it. Note the residue:
`lib/dep` contains **only `.git`**, so `any(dir.iterdir())` is satisfied — the
emptiness half of the post-condition does not catch it, the `HEAD == sub.sha`
half does. **Both halves are required at every level.**

**M15 — `git archive` at every level emits the next level's gitlink as an EMPTY
directory.** `git archive <base_sha>` → `.gitmodules`, `tests/test_x.py`,
`top.txt`, `vendor/lib` (0 entries). `git archive <inner gitlink sha>` from the
inner mirror → `.gitmodules`, `mid.txt`, `vendor/deep` (0 entries). **N+1
archives for N levels.**

**M16 — an empty innermost is invisible to every reader the gate uses today.**
`runE`: level 1 initialised, level 2 never touched.

```
outer  git status --porcelain                          → (empty)
outer  git status --porcelain --ignore-submodules=none → (empty)
inner  git status --porcelain                          → (empty)
outer  git diff HEAD --stat                            → (empty)
outer  git submodule status                            →  8a71042… vendor/lib (heads/main)
outer  git submodule status --recursive                →  8a71042… vendor/lib (heads/main)
                                                         -57c7df7… vendor/lib/vendor/deep
ls -A vendor/lib/vendor/deep                           → 0 entries
```

**`--recursive` is the only reader that says anything.**

**M17 — what a superproject read sees when the agent works inside a nested
submodule.** Re-measured against **item 17's own argv**
(`git --no-optional-locks status --porcelain=v2 --ignore-submodules=none -z`),
on `runG`, one state at a time, each reverted before the next:

| agent action | superproject v2 | `git -C vendor/lib` v2 | `submodule status --recursive` | `add -A` + `diff --cached` |
|---|---|---|---|---|
| uncommitted edit in `vendor/lib/mid.txt` (**level 1**) | `1 .M S.M. 160000 160000 160000 9f4425bc… 9f4425bc… vendor/lib` | `1 .M N... 100644 … mid.txt` | both lines leading space | 0 bytes |
| uncommitted edit in `vendor/lib/vendor/deep/deep.txt` (**level 2**) | `1 .M S.M. … vendor/lib` **(identical)** | `1 .M S.M. 160000 … vendor/deep` | both lines leading space | 0 bytes |
| **committed** in `vendor/lib/vendor/deep` | `1 .M S.M. … vendor/lib` **(identical)** | `1 .M SC.. 160000 … vendor/deep` | `+b1a5df6… vendor/lib/vendor/deep (heads/main-1-gb1a5df6)` | 0 bytes |
| **committed** in `vendor/lib` itself | `1 .M SC.. … vendor/lib` | — | `+2c29b80… vendor/lib` | 245 bytes, `index 8a71042..2c29b80 160000` |

Four consequences, which D7 turns into the grader and item-17 statements:

1. **The superproject's sub-state byte is `S.M.` for all three level-2 states
   and for a level-1 uncommitted edit alike.** It says neither the depth nor the
   kind.
2. **A level-2 *committed* edit reads `S.M.` at the superproject, not `SC..`**,
   because the outer gitlink did not move — what changed is the inner's working
   tree. Item 17's D8 comment (*"the third bit, `C` (`SC..`), is the moved
   gitlink — which git DOES stage"*) is true at depth 1 only.
3. Only `git -C <level-1 path>` with the same argv separates them: `N...` on
   `mid.txt` (level-1 edit), `S.M.` on `vendor/deep` (level-2 uncommitted),
   `SC..` on `vendor/deep` (level-2 committed).
4. `git submodule status --recursive` sees **nothing** for the uncommitted cases
   — the half item 17's title names — which is why review-1 finding 8 is right
   that the first draft's `--recursive` obligation was the wrong ask.

**M18 — git imposes no depth limit.** `L0→L1→L2→L3`, one
`submodule update --init --recursive`, exit 0, three status lines,
`cat run3/s/s/s/f.txt` → `L3`. **The cap is policy, not a constraint.**

**M19 — the module directory is named by the submodule NAME, not its path.**
`colA` declares `[submodule "dep"]` at path `lib` → `runD/.git/modules/dep`, and
its child → `runD/.git/modules/dep/modules/dep`. Any code that walks
`.git/modules` by *path* is wrong.

**M20 — `git -C` scopes `-f`.** `git -C vendor/lib config -f .gitmodules
--get-regexp -z '^submodule\..*\.path$'` and
`git config -f vendor/lib/.gitmodules --get-regexp -z …` return the same two
records at exit 0.

**M21 — `_has_gitmodules` is TRUE on a tree with ZERO gitlinks** (review-1
finding 4). A repository where a submodule was `git rm --cached`'d with its
stanza left behind — the inert shape `derive_submodules`' own comment names and
preflight records as `submodules_orphaned`:

```
$ git -C orph.git ls-tree -r <sha> | grep -c 160000     → 0
$ git -C orph.git cat-file -e <sha>:.gitmodules         → exit 0
```

**So `_has_gitmodules` cannot be the descent predicate and cannot be the cap
refusal's condition.** D3 uses a gitlink scan instead.

---

## Dependencies

Implementation is strictly sequential in one tree, so this item lands **after**
items 2, 10, 16 and 17. All four plans now exist; items 16 and 17 were in
revision when this was written, which is why every obligation below is stated as
something to **re-locate and check**, never to transcribe.

### Item 2 — `submodules_unneeded` (`…round2-2-unneeded-submodule.md`)

Read its **D2**, **D3**, **D4** and **D5** before Task 1.

1. **The declared entry stays in the tuple (its D2), and this plan does not
   descend into it.** A declared-unneeded submodule is never populated, so its
   mirror is never built and its own `.gitmodules` is never read. The recursion
   therefore carries an explicit `if sub.declared_unneeded: continue` in the
   descent loop (D2), with a mutation anchor on that line — not the
   `mirrors.get(...) is None` coincidence the first draft claimed to replace and
   then reproduced (review-1 finding 5).
2. **`submodules_unneeded` entries are full superproject-relative paths at any
   depth**, so a level-2 submodule can be declared unneeded while its parent is
   populated. That makes item 2's typo refusal a two-position check; D4 writes
   both positions out and shows both of item 2's ordering tests still pass
   (review-1 finding 2).
3. **Item 2's `needed_gitlinks` subtraction and its `declared_unneeded` stamp
   are computed against level-local `ls-tree` keys today.** At depth 2 the local
   key is `vendor/deep` and the declared path is `vendor/lib/vendor/deep`, so
   both would silently never match. Task 1 Step 3 names the join that fixes it
   (review-1 finding 6).
4. **Item 2's `submodules_empty_after_suite` mapping needs no change, and the
   reason is not the one the first revision gave** (review-2 note N1). That
   mapping is built from `sorted(unneeded)` — item 2's D6 point 6, the
   **manifest** key — not from `evidence["submodules"]`, so "this plan makes the
   evidence recursive" is the wrong mechanism. The conclusion holds for a better
   reason: the declared paths are already full superproject-relative paths at
   any depth (point 2 above), and item 2's `ls -A -- <path>` runs with
   `container.exec`'s hard-wired `workdir=REPO_MOUNT`, so it reaches
   `vendor/lib/vendor/deep` in one exec with no recursion at all. **This item
   owes item 2 nothing there.**

### Item 10 — the submodule leak guards (`…round2-10-submodule-leak-guards.md`, **final**)

The first draft of this plan predicted item 10 would land no production
post-condition and offered a test-level substitute. **That prediction was wrong
and the substitute is dropped** (review-1 finding 1). Item 10 lands:

- **`tasks._refuse_host_mirror_path(dest, cache_root, task_id)`** (its D5),
  called from **`materialize`, after `_init_submodules`** (its D4), walking the
  run tree's whole `.git` subtree with `os.walk(..., onerror=)` and matching
  **bytes** in 1 MiB chunks (its D3).
- **`_init_submodules`' `remote remove origin` becomes a listing plus one
  removal per name, both at `check=True`** (its D1, Task 2.1), with
  `cwd=checked`.
- **`materialize`'s own two guards change to match** (its D2, Task 3.1).

**`os.walk` over `dest/.git` is recursive by construction, so item 10's
post-condition already covers `.git/modules/<outer NAME>/modules/<inner NAME>` —
M8's four files — with no per-level obligation from this item at all.** That is
stronger than the first draft hoped for, and it is why this plan adds no
leak-scan of its own. What this item owes item 10 is one thing only: the two
guards must **run** at every level, which D4 arranges by binding `checked` to
`dest / sub.path` where `sub.path` is the full path (D1). Item 10's own mutation
anchor on the post-condition then covers a level whose guards were skipped.

**No line of item 10's guard code changes here.** `_init_submodules`' guard block
already uses `cwd=checked` and `cwd=dest / sub.path`; both are correct at depth 2
the moment `sub.path` is full. The only lines this item touches in that loop are
the two `config` calls and the `submodule update` call, which move to
`cwd=parent_tree`.

### Item 16 — relative `.gitmodules` urls (`…round2-16-relative-submodule-urls.md`, in revision)

Item 16 **renames** `Submodule.url` to `url_declared: str` plus
`url_resolved: str | None` (its D1) and deliberately makes a missed call site an
`AttributeError` on a frozen dataclass. Three consequences, all folded into
D1/D2/D4:

- the dataclass in D1 is written post-16 and post-2;
- every `sub.url` read in this plan's blocks is `sub.url_resolved` (its D7 names
  the two in `tasks._init_submodules` and the one in
  `images._extract_submodules`);
- **the resolution base at depth ≥ 2 is the parent's `url_resolved`, not
  `task.repo_url`.** Item 16's resolver signature already takes the base
  (`_resolve_submodule_url(repo_url, declared, *, task_id, path)`); what is
  hard-coded is its **call site**, which passes `task.repo_url` (its D6). This
  plan changes that one argument, because the recursion is what knows the parent
  — see D2 and OQ4, which this plan **rules** rather than defers (review-1
  finding 7).

### Item 17 — the invisible in-submodule edit (`…round2-17-submodule-dirty-capture.md`, in revision)

Item 17's reader is
`git --no-optional-locks status --porcelain=v2 --ignore-submodules=none -z` at
the superproject (its D1), and it **explicitly rejects `git submodule status`**
(its M1, and a measured ~91 ms per call). The first draft of this plan asked it
to add `--recursive` to a command it does not run, and asked for a flag that M17
shows is blind to the uncommitted half of item 17's own subject. **Both are
withdrawn** (review-1 finding 8). D7 states the corrected obligation.

---

## Design decisions, settled

### D1. `Submodule.path` is the FULL superproject-relative path; `depth` and `parent` are new fields

**The dataclass after items 2 and 16, plus this item's two fields.** Re-locate
before editing; if items 2/16 landed different names, keep theirs and add only
the last two:

```python
@dataclass(frozen=True)
class Submodule:
    name: str
    path: str                  # full, superproject-relative
    url_declared: str          # item 16
    url_resolved: str | None   # item 16
    sha: str
    declared_unneeded: bool = False   # item 2
    depth: int = 1             # THIS ITEM. 1 = a submodule of the superproject
    parent: str = ""           # THIS ITEM. the parent's full path; "" at depth 1
```

**Full path, not the level-local one**, because every consumer compares it
against superproject-relative paths and would otherwise compare unlike things:

- `_refuse_submodule_conflicts`' `strip_paths` check calls `_under(p,
  (sub.path,))` against `task.strip_paths`, which are superproject-relative;
- its reference-diff check compares against `task.test_files`,
  `task.solution_files` and `task.extra_files`, all from `git apply --numstat`
  over the superproject's diff;
- `images._extract_submodules` uses `repo_dir / sub.path`;
- `_init_submodules`' `checked`, its two leak guards and its HEAD check all use
  `dest / sub.path` — which is what makes item 10's guards correct at depth 2
  with no edit;
- `preflight`'s evidence paths must match `git submodule status --recursive`'s,
  which are superproject-relative (M13, M16);
- item 2's `submodules_unneeded` entries are superproject-relative.

A level-local `path` would make all six silently wrong at depth ≥ 2 — a
`strip_paths: ["vendor/deep"]` would match a submodule actually at
`vendor/lib/vendor/deep`, and a reference diff touching
`vendor/lib/vendor/deep/x.py` would slip past the refusal built to catch it.

**`local_path` is a property, not a field**, computed with `PurePosixPath`,
never a byte slice:

```python
    @property
    def local_path(self) -> str:
        """The path git uses INSIDE the parent -- what `submodule update --init
        -- <path>` takes, and what the parent's `.gitmodules` declared. `path`
        is this joined onto `parent`, so `relative_to` inverts a join this class
        performed rather than slicing a string whose prefix is an assumption: on
        a violated invariant it RAISES, where `path[len(parent) + 1:]` would
        return a plausible wrong path. Measured 2026-09-02 (M13): the level-2
        update runs with `cwd` at the inner working tree and takes
        `vendor/deep`, not `vendor/lib/vendor/deep`."""
        if not self.parent:
            return self.path
        return str(PurePosixPath(self.path).relative_to(self.parent))
```

`PurePosixPath` is already imported in `tasks.py` for `_under`.

**Both new fields default**, so every existing `Submodule(...)` construction
stays valid and keeps meaning what it meant: a depth-1 submodule of the
superproject. **`depth` and `parent` are nonetheless written explicitly at the
one construction site**, for the reason item 16's D6 gives about
`declared_unneeded`: a defaulted field omitted at the site that should set it
compiles, constructs, and silently reverts the feature.

**`parent` is a field and not derived from `path`.** Deriving it means finding
the longest tuple entry that is a prefix of `path` — prefix arithmetic over
paths, which is the defect `_parse_submodule_status`'s boundary-anchoring
correction exists to prevent, and which is ambiguous exactly where it matters
(`vendor/lib` beside `vendor/lib dep`).

### D2. `derive_submodules` recurses; the two readers move into `_read_level`

The body between `entries = _git("ls-tree", …)` and the `unfetchable` refusal is
already a pure function of `(mirror, sha)` (M2). It is extracted as
`_read_level`, and the driver is `_derive_from`. **These are the only two new
function names in `tasks.py`; the first draft also used `_derive_level`, which
existed nowhere** (review-1 finding 7).

```python
def derive_submodules(task, mirror: Path, cache_root: Path) -> tuple[Submodule, ...]:
    subs = _derive_from(task, mirror, task.base_sha, parent=None, depth=1,
                        cache_root=cache_root)
    # SECOND HALF of item 2's typo refusal; the first half is in `_read_level`
    # at depth 1. See D4.
    unexplained = sorted(set(task.submodules_unneeded)
                         - {sub.path for sub in subs})
    if unexplained:
        raise TaskError(
            f"{task.task_id}: submodules_unneeded names "
            f"{', '.join(unexplained)}, which is under a gitlink but is not a "
            "gitlink at any level the derivation reached. A typo declares "
            "nothing: the submodule it was meant to name is still populated "
            "(or still refused for its url), and nothing downstream would say "
            "the key did not apply."
        )
    return subs


def _derive_from(task, mirror: Path, sha: str, parent: Submodule | None,
                 depth: int, cache_root: Path) -> tuple[Submodule, ...]:
    subs = _read_level(task, mirror, sha, parent, depth)
    # CHEAP REFUSALS FIRST, and the ordering is not stylistic: the url refusal
    # has to run BEFORE the url it refuses is handed to a clone. A
    # `git@github.com:...` url reaching `ensure_pruned_mirror` is a fetch
    # against a host the eval carries no key for -- git's own error, or a
    # credential prompt, instead of the loader's message naming the task.
    _refuse_submodule_conflicts(task, subs, mirrors={})
    mirrors = {
        sub.path: ensure_pruned_mirror(sub.url_resolved, sub.sha, cache_root)
        for sub in subs if not sub.declared_unneeded
    }
    # SECOND CALL, a strict superset: the depth refusal is the only one that
    # needs an artifact rather than a comparison.
    _refuse_submodule_conflicts(task, subs, mirrors=mirrors)
    out: list[Submodule] = []
    for sub in subs:
        out.append(sub)          # PRE-ORDER: the parent precedes its children.
        # ITEM 2, EXPLICITLY. Nothing is populated for a declared path, so its
        # own `.gitmodules` is never read and its children never exist. Not
        # left to `mirrors.get(...)` returning None: that is a coincidence of
        # the comprehension above, and a coincidence cannot be mutation-tested.
        if sub.declared_unneeded:
            continue
        if _has_gitlinks(mirrors[sub.path], sub.sha):
            out.extend(_derive_from(task, mirrors[sub.path], sub.sha,
                                    parent=sub, depth=depth + 1,
                                    cache_root=cache_root))
    return tuple(out)
```

Two `_refuse_submodule_conflicts` calls per level, the second a strict superset —
which is the shape that function's docstring already describes and defends (*"the
cheap refusals run wherever the derivation runs, and re-running them costs three
comparisons over tuples already in hand"*). What changes is only *why* there are
two: it was "one call site has no mirrors", it is now "the url must be refused
before it is cloned".

**`parent` is threaded as the parent `Submodule`, not as a string** (review-1
finding 7), because two things below need more than its path: `_read_level` needs
`parent.url_resolved` as item 16's resolution base at depth ≥ 2, and the
`Submodule` it constructs needs `parent.path`. `None` at depth 1 reads as "the
superproject", which is what `task.repo_url` is the base for.

**`_has_gitlinks(mirror, sha)`, not `_has_gitmodules`** (review-1 finding 4).
M21: a tree can carry a `.gitmodules` and **zero** gitlinks — the inert
`git rm --cached` shape this module's own comments say must not be refused. A new
module-private helper does the one read that answers the real question:

```python
def _has_gitlinks(mirror: Path, sha: str) -> bool:
    """Whether the tree at `sha` has at least one 160000 entry.

    NOT `_has_gitmodules`. Measured 2026-09-02 (M21): a repository whose
    submodule was `git rm --cached`'d with its stanza left behind has a readable
    `.gitmodules` and no gitlink at all -- the inert shape `derive_submodules`'
    own comment says must be recorded rather than refused, and which preflight
    files as `submodules_orphaned`. Using the blob's existence as the predicate
    would both descend into a level that yields nothing and, at the cap, REFUSE
    a task the eval can run.

    The descent guard and the cap refusal read THIS ONE predicate, so the two
    cannot disagree about what a nested submodule is.
    """
    entries = _git("ls-tree", "-r", "-z", sha, cwd=mirror).stdout
    return any(record.startswith("160000 ") for record in entries.split("\0"))
```

**`cache_root` becomes a required third parameter of `derive_submodules`.** Both
production call sites hold one. The test call sites are found with
`grep -rn "derive_submodules(" bakeoff/tests/` — **ten** at round base, more
after items 2 and 16, so **run the grep; do not transcribe a count** (review-1
finding 16a). Not a keyword with a `None` default: a `None` silently meaning "do
not recurse" would make the derivation answer a different question depending on
its caller.

**A refused url never builds a mirror**, which keeps the existing refusal tests
offline and fast: they raise out of the first `_refuse_submodule_conflicts`
before `ensure_pruned_mirror` is reached. Pinned by a test (Task 1).

**`_has_gitmodules` keeps its other caller** — `_read_level`'s
`if gitlinks or _has_gitmodules(mirror, sha)` guard, which decides whether to
*attempt* the `.gitmodules` read. That use is correct and unchanged.

### D3. The depth cap is **2**, as a named constant

```python
#: How deep the submodule recursion goes. 1 is a submodule of the superproject,
#: 2 is a submodule of one of those. A submodule at `_MAX_SUBMODULE_DEPTH` whose
#: own tree carries a gitlink is refused -- the same refusal that refused level 2
#: before this change, for the same measured reason: the directory one level
#: further down would arrive empty and `git status --porcelain` reports a tree in
#: that state as CLEAN (measured 2026-09-02, git 2.50.1, at two levels).
_MAX_SUBMODULE_DEPTH = 2
```

**Why a cap at all, when git has none (M18).**

1. **Termination is a property of the code, not an argument about git history.**
   That a `(url, sha)` cycle cannot occur is a *proof about upstream
   repositories* (a gitlink names a commit that already exists, so `A@x → B@y →
   A@z` forces `z ≠ x`), not about the harness. A cap terminates without needing
   it, and without a visited set whose correctness rests on it.
2. **Every level multiplies the surfaces where an empty directory reads as
   clean.** One level costs a pruned mirror (a clone, a `_verify_pruned`, a repo
   lock), two leak guards, three post-conditions, a `git archive` into the build
   context, an `ls-files` recursion in the gate, a `--show-prefix` probe, and an
   evidence entry — all discovered at materialization time, after the preflight
   cache key was computed.
3. **Every gitlink the harness can reach in the screened corpus is flat** (the
   table in *The measured defect*: four measured flat, the fifth private and
   never fetched). This broadening is built on a prediction, and the honest
   version of a speculative broadening moves the floor by one level.

**Why 2 and not 3+.** 2 is the smallest cap that closes the item — 1 is the
status quo. Raising it later is a one-line edit plus the tests named in Task 4,
because the recursion is already generic over depth; the constant exists so that
raising it is a decision rather than a redesign.

**Why not "unbounded with a visited set on `(url, sha)`".** It makes the
*derivation* terminate and says nothing about the other seven surfaces per level,
and it silently deduplicates a genuine diamond (two submodules pinning the same
third repository at the same sha) into one `Submodule` whose `path` and `parent`
can only be one of the two — a wrong observation rather than a refusal.

**The refusal's condition and message** (review-1 finding 3 — the first draft
supplied only the message, and the unchanged condition would have refused every
legal two-level task, because at depth 1 the second
`_refuse_submodule_conflicts` call now has a real `mirrors` dict). Inside
`_refuse_submodule_conflicts`, inside item 2's `if not sub.declared_unneeded:`
guard, at the same position (first after the url check):

```python
        mirror_for = mirrors.get(sub.path)
        if (sub.depth >= _MAX_SUBMODULE_DEPTH
                and mirror_for is not None
                and _has_gitlinks(mirror_for, sub.sha)):
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} at {sub.sha} declares "
                f"submodules of its own, which would be {sub.depth + 1} levels "
                f"below the superproject; this eval populates at most "
                f"{_MAX_SUBMODULE_DEPTH}. `git submodule update --init` is run "
                "per level, so a level below the cap would arrive empty -- and "
                "an empty submodule directory leaves `git status --porcelain` "
                "clean, so nothing downstream would say so."
            )
```

`sub.depth` is what makes both the second per-level call and the
`_init_submodules` call **inert for a legal tree**: at depth 1 the first conjunct
is false, at depth 2 the third is false because the recursion only descended into
levels where `_has_gitlinks` was true and would have refused a level-3 gitlink
there. That inertness is what open question 2's *"it cannot fire"* rests on. The
message keeps the second sentence of the refusal it replaces verbatim, because
that sentence is the measured claim (M16).

**This narrows an existing refusal.** Today `_has_gitmodules` refuses a depth-1
submodule that carries an orphan stanza and no gitlink (M21). After this change
that tree is accepted, recursed into (yielding nothing) and recorded by preflight
as `submodules_orphaned` — which is what the module's own comment says the inert
shape deserves. Stated again in *What this does NOT do*.

### D4. `_init_submodules` walks the flat pre-order tuple with `cwd` at the parent

The tuple is **pre-order** (D2's `out.append(sub)` precedes the recursive
`extend`), pinned by a test rather than left as a property of the recursion's
shape.

**The loop after items 2, 10 and 16, with this item's changes marked.** Re-locate
every line; `grep -cF` each `find` string must print `1`:

```python
    needed = tuple(sub for sub in subs if not sub.declared_unneeded)   # item 2
    if not needed:                                                     # item 2
        return
    mirrors = {
        sub.path: ensure_pruned_mirror(sub.url_resolved, sub.sha, cache_root)
        for sub in needed                                              # item 16
    }
    _refuse_submodule_conflicts(task, subs, mirrors=mirrors)
    for sub in needed:
        key = f"submodule.{sub.name}.url"
        # THIS ITEM. `dest` at depth 1, the parent's working tree below it.
        # The config key lives in the PARENT's config -- at depth 2 that is
        # `.git/modules/<outer NAME>/config`, reached through the `.git` FILE
        # (M6) and never by a path this code assembles, because the module
        # directory is named by the submodule NAME (M19). M5 is what happens
        # when the key does not land there: the tree is populated and
        # `git submodule status` still reads `-`.
        parent_tree = Path(dest) / sub.parent if sub.parent else Path(dest)
        _git("config", key, str(mirrors[sub.path]), cwd=parent_tree)
        _git("-c", "protocol.file.allow=always",
             "submodule", "update", "--init", "--", sub.local_path,
             cwd=parent_tree)
        _git("config", key, sub.url_resolved, cwd=parent_tree)

        checked = dest / sub.path            # UNCHANGED: `sub.path` is full
        <existence-and-emptiness post-condition -- UNCHANGED>
        <item 10's remote listing + per-name removal at check=True -- UNCHANGED>
        <the reflog expire -- UNCHANGED>
        <the HEAD == sub.sha post-condition -- UNCHANGED>
```

**Three lines change and everything below `checked` does not**, which is the
point: item 10's guards and both post-conditions are already correct at depth 2
the moment `sub.path` is the full path (D1). This plan touches **no line of item
10's guard code** and changes no `check=` value anywhere.

Four things this pins, each measured:

- **`cwd=parent_tree`, and `sub.local_path`, not `sub.path`** (M13).
- **No `--recursive`, anywhere.** M4/M5: the one-shot form populates the tree and
  leaves the deeper marker at `-`. M14: a single process-wide
  `-c submodule.<name>.url=` collides across levels, since names are
  per-repository. The per-level form has neither problem and reuses the existing
  code path exactly.
- **Both post-conditions at every level.** M14's residue is a directory holding
  only `.git`, which satisfies `any(checked.iterdir())`; the `HEAD == sub.sha`
  check is what catches it. The existence/emptiness check stays *immediately
  after* the update and *before* the guards, for the reason the current code
  documents — everything below runs with `cwd=dest/sub.path`, and
  `subprocess.run` against a missing cwd raises `FileNotFoundError` with no
  `task_id` in it.
- **`_init_submodules` keeps its `_refuse_submodule_conflicts` call.** It is now
  a re-run of refusals the derivation already made and cannot fire (D3); it
  asserts that materialization and derivation agree about the same tuple.

**Item 2's typo refusal, both positions** (review-1 finding 2 — the first draft
moved it wholesale past the recursion, which breaks item 2's two ordering tests,
and then said so in its own justification).

*Position 1, in `_read_level`, at item 2's pinned line — immediately after the
`gitlinks` dict is built and before the `if gitlinks or _has_gitmodules(...)`
block — guarded to depth 1:*

```python
    if depth == 1:
        # ITEM 2's check, at ITEM 2's position, with ONE term added. A declared
        # path that sits UNDER a gitlink at this level may still be explained
        # one level down (`vendor/lib/vendor/deep` under `vendor/lib`), so it is
        # deferred to `derive_submodules`' second check rather than called a
        # typo here. `_under` is component-wise (`PurePosixPath.is_relative_to`),
        # so a near-miss is NOT deferred: measured 2026-09-02, `vendor/libdeps`
        # is not under `vendor/libdep` and `vendor/typo` is not under it either,
        # which is what keeps item 2's two ordering tests --
        # `test_the_typo_refusal_beats_the_url_refusal` and
        # `test_the_typo_refusal_beats_the_unreadable_gitmodules_refusal` --
        # passing unchanged, both of which use exactly those near-misses.
        # Guarded to depth 1 because at depth 2 the same subtraction would see a
        # level-1 declared path it cannot explain and call THAT a typo.
        deferred = {p for p in task.submodules_unneeded
                    for g in gitlinks if p != g and _under(p, (g,))}
        unknown = sorted(set(task.submodules_unneeded) - set(gitlinks) - deferred)
        if unknown:
            raise TaskError(<item 2's message, verbatim>)
```

*Position 2, in `derive_submodules` after the recursion* — the `unexplained`
block in D2, whose message says the path was deferred and no level explained it.

Under this order, item 2's two tests assert exactly what they asserted:
`vendor/libdeps` and `vendor/typo` are neither gitlinks nor under one, so
position 1 fires before the url refusal and before the unreadable-`.gitmodules`
refusal. A genuine level-2 declaration (`vendor/lib/vendor/deep`) is deferred at
position 1 and satisfied by the recursion. A level-2 **typo**
(`vendor/lib/vendor/deeep`) is deferred at position 1 and refused at position 2.

**One consequence a task author will meet and the messages do not name**
(review-2 note N2). Declaring **both** a parent and its child —
`submodules_unneeded: ["vendor/lib", "vendor/lib/vendor/deep"]` — is now
refused: the child is deferred at position 1 because it is under a gitlink, the
recursion never descends into the declared-unneeded parent (D2's `continue`), so
the child is in no `sub.path`, and position 2 raises `unexplained`. The message
is accurate — the path really is under a gitlink and really is not a gitlink at
any level the derivation reached — but it does not say *why*, and the why is
that **declaring a parent implicitly declines its children**. That sentence goes
in the key's documentation block rather than into the message, because it is a
property of the key rather than of this tree, and Task 6 adds it. Pinned by
`test_declaring_a_parent_and_its_child_is_refused`, whose assertion is the
`unexplained` message — so if a later change makes the pair legal, this test is
what says the documentation block moved with it.

### D5. `images._extract_submodules` takes one archive per level, parents first

M15: `git archive` at level N emits level N+1's gitlink as an empty directory, so
N levels need N+1 archives. The existing loop does the right thing once fed the
flattened tuple — `target.mkdir(parents=True, exist_ok=True)` and
`git archive sub.sha` into `repo_dir / sub.path`. Item 16 changes its
`ensure_pruned_mirror(sub.url, …)` to `sub.url_resolved`; item 2 adds the
`if sub.declared_unneeded: continue`. **This item adds only the parents-first
iteration**, and it is defence rather than a measured break:
`mkdir(parents=True)` would create the deep directory anyway and `tar -x` over an
existing directory only resets its mode. It is pinned because "it happens to work
in the other order" is the kind of statement that stops being true when someone
sorts the tuple.

`ensure_pruned_mirror` is called per entry, as today; the derivation already
built every mirror (D2), so this is the fast path. `HANDOFF.md`'s rule holds:
**no term is added to `_pack_fingerprint`**.

### D6. Preflight recurses both readers, and `--show-prefix` is the load-bearing guard

Eight changes, all inside `preflight`'s existing submodule block.

**1. `git submodule status --recursive`.** M16: without it, an empty level-2
directory is invisible to every reader in the file. The problem message on a
non-zero exit gains `--recursive` so it quotes the command that ran.

**2. `_parse_submodule_status` is NOT modified**, and that is worth stating
because it looks like it should need work. With `paths = {"vendor/lib",
"vendor/lib/vendor/deep"}` and the line
`" 57c7df7… vendor/lib/vendor/deep (heads/main)"`, `tail == "vendor/lib"` is
false and `tail.startswith("vendor/lib" + " ")` is false (the next byte is `/`),
so only the long path matches; `max(match, key=len)` is untouched. Review 1
walked the same match by hand, with and without the describe suffix, and agrees.
`test_a_nested_submodule_status_line_matches_only_its_own_path` pins it as a pure
unit test.

**3. The authoritative path set becomes a recursion, with a probe.**

```python
def _gitlink_paths(container, prefix: str = "") -> tuple[str, ...] | None:
    """... `git -C <prefix>` when prefix is non-empty; paths joined onto it."""

def _is_own_repository(container, path: str) -> bool | None:
    """`git -C <path> rev-parse --show-prefix` is EMPTY.

    `None` if the probe could not be run. THE GUARD, and it is not defensive
    tidiness: measured 2026-09-02 (M11), `git -C <empty submodule dir> ls-files
    -s -z` exits 0 and returns `160000 <parent's sha> 0\\t./` -- git walked UP to
    the enclosing repository and filtered its index by the cwd prefix. It does
    this at EVERY level (measured at level 1 and level 2). A recursion that
    descended unguarded would prefix that to `vendor/lib/./` and file a gitlink
    that does not exist: a FABRICATED observation, worse than the empty directory
    it was looking for. `--full-name` does not help -- it returns the parent's
    own gitlink path.

    `--show-prefix`, NOT `--show-toplevel` compared against a composed path.
    Measured 2026-09-02 at both levels: the prefix is EMPTY for a directory that
    is its own repository and non-empty otherwise (`vendor/lib/` for an
    uninitialised level-1 directory, `vendor/deep/` for an uninitialised level-2
    one, `tests/` for an ordinary subdirectory). The toplevel form would have
    this code compose `/repo/<path>` on the HOST and compare it against a string
    produced INSIDE the container, over a bind mount -- the class of
    host/container path disagreement `CLAUDE.md` already records under "an index
    written on the host is a lie to the container". A false compare there NO-GOes
    every healthy nested tree on an environment difference, which is the worst
    shape a gate defect takes. `--show-prefix` needs no path from the caller at
    all.

    `--show-superproject-working-tree` discriminates too (empty vs the parent's
    path) and is not used: it answers a question about the parent rather than
    about this directory, and it is empty in BOTH the "uninitialised" and the
    "not a submodule at all" cases.
    """
```

The caller descends into a gitlink **iff** the probe returns `True`, records
`depth` per path as it goes, and walks at most `_MAX_SUBMODULE_DEPTH` levels. A
`None` from the probe sets `evidence["submodules"] = None`,
`evidence["submodules_orphaned"] = None` and appends a problem — the shape the
two existing not-read branches use. A `False` on a path whose status marker is a
leading space is itself a problem: the two readers disagree about whether that
path is a repository.

**4. `git ls-files --recurse-submodules` is rejected**, per M10: a reader whose
output changes meaning with the state being checked cannot be the authority on
that state.

**5. Each `evidence["submodules"]` entry gains `"depth": int`.** For every entry,
depth 1 included — never a key present only when it exceeds 1, for the reason
item 2's D6 gives about `declared_unneeded`: a reader that cannot see the field
on the other entries cannot tell *"this tree is flat"* from *"this gate did not
know about nesting"*. The value comes from **the recursion's own bookkeeping**
(which `ls-files` produced the path), never from counting slashes and never from
the manifest. `"parent"` is deliberately not added — see OQ1.

**6. A tree deeper than the cap is NAMED, and the mechanism is written down**
(review-1 finding 10 — the first draft ruled "report, not refuse" and supplied no
mechanism, so what actually happened was the `unmatched` problem naming the wrong
cause). Measured: `git submodule status --recursive` **does** list the depth-3
line (M18's chain gives three lines), so with the walk stopping at 2 that line
lands in `unmatched` and is reported as *"the two readers disagree about this
tree"* — true, and about the wrong thing. So, immediately after the status parse
and **before** the `unmatched` problem is appended:

```python
        too_deep = sorted(line_path for line_path in <status paths>
                          if <slash depth of line_path> > _MAX_SUBMODULE_DEPTH)
        if too_deep:
            problems.append(
                "`git submodule status --recursive` names submodules deeper "
                f"than this eval populates (at most {_MAX_SUBMODULE_DEPTH} "
                "levels): " + ", ".join(too_deep[:5])
                + ". The loader refuses a manifest whose tree is that deep, so "
                "this container's tree is not the one that was derived. "
                "Reported rather than refused: the gate reads a tree, the "
                "loader refuses a manifest."
            )
```

This is the **one** place a path's depth is taken from its own text rather than
from the recursion, and that is deliberate and stated in the comment: the
recursion by construction produces no path deeper than the cap, so the only
available evidence of a too-deep tree is the status reader's own line — which is
why this problem is worded as an observation of the *status output*, and why the
paths in it are quoted rather than used for anything else.

**7. `submodules_orphaned` is read at every initialised level**, with the same
`git config -f .gitmodules --get-regexp -z '^submodule\..*\.path$'` and
`-C <path>` for the deeper ones (M20), values joined onto the level's prefix
before the `declared_paths - set(gitlinks)` subtraction. **The partial-failure
rule** (review-1 finding 11): **any level whose read exits > 1 sets the whole key
to `None` and appends a problem naming the level and the exit code.** Returning
the levels that did answer would render a partial answer as a complete positive
list — the "absence is recorded, never implied" rule, broken in the same block
whose comments argue for it. After this change `[]` means *"every initialised
level was read and none has an orphan stanza"*, and `None` means *"at least one
level could not be read, or no submodule read was made at all"*. Exit 1 stays git
config's ordinary "no key matched" at every level.

**8. `PREFLIGHT_VERSION` moves by one.** **Read the current value in
`preflight.py` at implementation time and add one** — do not transcribe a
literal. Items 1–17 move it; items 2 and 16 each move it again. The ledger note
is in Task 3 Step 8.

`preflight_cache_key` is unchanged. `ORACLE_VERSION` does not move.

### D7. The grader is unchanged, and what it can and cannot see is measured

`grader.py`, `grade_schema.py`, `GRADER_VERSION` and `GRADE_SCHEMA_VERSION` are
untouched. **Do not transcribe a `GRADER_VERSION` literal** — item 17's D10 moves
it to `"10"` and it may move again (review-1 finding 16b); the claim is "this
item moves it by zero", whatever it is.

**What IS visible in a submission diff.** M17: the harness's `git add -A` at the
superproject stages a `160000` chunk **only** when the level-1 gitlink moves,
i.e. when the agent commits inside `vendor/lib` itself:

```
diff --git a/vendor/lib b/vendor/lib
index 8a71042..2c29b80 160000
```

`grader._chunk_is_gitlink` reads that header, `_gitlinks_touched` collects it,
and the submission is refused as `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE` —
a not-graded reason, never a `GradeFailure`. Nesting does not change one byte of
it: the check takes no `task` and no submodule set.

**What is NOT visible.** An agent that commits inside `vendor/lib/vendor/deep`
moves the **inner's** gitlink for `vendor/deep`, which lives in the inner's index
and tree. The superproject's `git add -A` stages nothing (M17: **0 bytes**).
Without item 17 that reaches `_check_patch_non_empty` and becomes
`GradeFailure.EMPTY_PATCH`.

**What item 17 covers, measured rather than assumed** (review-1 finding 8):

- **Its refusal is already depth-complete.** M17: the superproject's v2 record is
  `1 .M S.M. … vendor/lib` for a level-2 uncommitted edit **and** for a level-2
  committed one. `S.M.` is one of the two bits item 17's `_submodule_edits`
  refuses on, so `SUBMODULE_EDIT_UNGRADABLE` fires for a nested edit of either
  kind with no change to item 17 at all. **This plan owes item 17 nothing for
  soundness.**
- **Its record and its message do not localise.** The same `S.M.` at
  `vendor/lib` is produced by a level-1 uncommitted edit, a level-2 uncommitted
  edit and a level-2 commit, so `checkpoint.submodule_states` reads
  `{"vendor/lib": "S.M."}` in all three and the refusal names `vendor/lib` for
  work done two levels down.
- **The fix is item 17's own argv, run per initialised level**, merged with paths
  joined onto the level's prefix: `git -C vendor/lib` with the same command gives
  `N... mid.txt` (level-1 edit), `S.M. vendor/deep` (level-2 uncommitted) and
  `SC.. vendor/deep` (level-2 committed) — the localiser.
  `{"vendor/lib": "S.M.", "vendor/lib/vendor/deep": "SC.."}` is the record that
  says what happened.
- **`git submodule status --recursive` is NOT the fix**, and the first draft of
  this plan was wrong to ask for it: M17 shows it reads a leading space on both
  lines for both uncommitted cases — blind to the half item 17's title names.
- **One correction item 17's own prose needs:** its D8 comment says *"the third
  bit, `C` (`SC..`), is the moved gitlink -- which git DOES stage, which the diff
  DOES carry"*. True at depth 1 only; at depth 2 a moved gitlink reads `S.M.` at
  the superproject and stages **0 bytes**. The two refusals stay disjoint —
  `_gitlinks_touched` does not fire, `_submodule_edits` does — but for a
  different reason than the comment gives.

**The grader's other submodule behaviour**, the fix-1 exclusion in
`_check_test_restore` (`git ls-tree -r -z <start_sha> -- <tests.paths>`,
excluding `160000` entries from the `git rm -r` and the `git checkout`), needs no
change and needs an argument: `ls-tree -r` does not descend through a gitlink
(M1), so at `start_sha` the only `160000` entries under `tests.paths` are level-1
ones — exactly the set whose working trees the restore would otherwise delete. A
level-2 submodule's directory lives *inside* a level-1 submodule's working tree,
which the restore already declines to touch, so it is covered transitively rather
than by a second rule.

### D8. `start_sha` does not move

Three reasons, in decreasing strength, and the first is sufficient:

1. **`_init_submodules` runs after `start_sha` is computed and compared.** It is
   `materialize`'s last statement, below the `declared_start_sha` comparison and
   below `git clean -xfd`. Broadening 6's D4 made that ordering the guarantee.
   Nothing this item changes moves that line, and the derivation is still
   evaluated after the comparison, so a refused task pays no clone.
2. **Nothing in the recursion is an input to the setup commit.** `start_sha` is
   `base_sha` + `strip_paths` + the committed test half + `gitignore_extra`; a
   submodule's depth is in `base_sha`'s trees and is not staged, removed or
   rewritten.
3. **Even out of order it could not move it**: broadening 6's M8 measured `git
   add -A` staging zero bytes for a gitlink path, and M17 re-measures it at two
   levels (0 bytes with the innermost's gitlink moved).

**Pinned**: `test_the_click_task_still_loads_and_its_start_sha_has_not_moved`
keeps pinning `33575cc0b75608fa5cbcb1d3ae3347b81eac437f` and is not modified; a
new `test_a_nested_submodule_does_not_move_start_sha` materializes the two-level
fixture into two destinations and asserts the two `start_sha` values are
byte-identical and that both levels are populated afterwards with
`git status --porcelain` empty.

`SCHEMA_VERSION` does not move: no `RunRecord` field is added or changes meaning.

---

## File Structure

| File | Change |
|---|---|
| `bakeoff/src/bakeoff/tasks.py` | `Submodule.depth`, `.parent`, `.local_path`; `_MAX_SUBMODULE_DEPTH`; `_has_gitlinks`; `_read_level` extracted; `derive_submodules` recursive with a required `cache_root` and the second typo check; `_derive_from`; the depth refusal replacing the nested one; the depth-1 guard and `deferred` term on item 2's typo check; `_init_submodules` per-level `cwd`; three docstring rewrites |
| `bakeoff/src/bakeoff/images.py` | `_extract_submodules` iterates parents-first; one docstring paragraph |
| `bakeoff/src/bakeoff/preflight.py` | `--recursive`; `_gitlink_paths(prefix)`; new `_is_own_repository`; the recursion and its `depth` bookkeeping; per-entry `"depth"`; the too-deep problem; per-level `submodules_orphaned` with the all-or-`None` rule; `PREFLIGHT_VERSION` +1 and its ledger note |
| `bakeoff/scripts/mutation_check.py` | six new anchors (Task 4) |
| `bakeoff/tests/test_tasks.py` | the two-level fixture; the `derive_submodules` call sites (found by grep) gain `cache_root`; the new tests below |
| `bakeoff/tests/test_images.py` | two new tests |
| `bakeoff/tests/test_preflight.py` | nine new tests |
| `bakeoff/tests/test_integration_submodules.py` | the two-level fixture and one new integration test |
| `bakeoff/taskset/HARVESTING.md` | the *"Nested submodules are refused"* **sub-bullet** becomes a depth statement; the count stays **six** |
| `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` | two clauses in item 2's `submodules_unneeded` documentation block: paths are at any depth, and declaring a parent declines its children (review-2 note N2) |
| `docs/BUILDING-A-TASK-SET.md` | §2's *"needs a git submodule"* row |
| `TASKS.md` | the item is removed; the item-17 and item-10 entries gain the measured two-level notes |

---

## Task 1: the recursion in `tasks.py`

**Before any edit in this task**, confirm items 2, 10, 16 and 17 have landed and
re-locate every `find` string; `grep -cF '<find>' bakeoff/src/bakeoff/tasks.py`
must print `1`.

- [ ] **Step 1: `Submodule` gains `depth`, `parent` and `local_path`.** D1's
  shape and docstring. Add to the class docstring: *"`path` is the FULL
  superproject-relative path at every depth, because every consumer — the
  `strip_paths` refusal, the reference-diff refusal, item 2's
  `submodules_unneeded`, the build context, `_init_submodules`' `checked` and
  item 10's leak guards, and the gate's evidence — compares it against
  superproject-relative paths."*

- [ ] **Step 2: add `_MAX_SUBMODULE_DEPTH = 2`** beside `_SUBMODULE_URL_PREFIX`,
  with D3's comment, and **`_has_gitlinks`** beside `_has_gitmodules`, with D2's
  docstring.

- [ ] **Step 3: extract `_read_level(task, mirror, sha, parent, depth)`** from
  `derive_submodules`, with these changes and no others:

  (a) the two `_git` calls take `sha` rather than `task.base_sha`;

  (b) **the join, computed once per gitlink and used four times** (review-1
  finding 6 — the first draft said "extracted verbatim" and this join is what
  verbatim omits):

  ```python
      full = str(PurePosixPath(parent.path) / local) if parent else local
  ```

  `full` is what goes into item 2's `needed_gitlinks` subtraction, into item 2's
  `declared_unneeded=full in unneeded` stamp, into `path=full`, and into both
  refusal messages. Item 2 computes both against level-**local** `ls-tree` keys
  today; at depth 2 the local key is `vendor/deep` while the declared path is
  `vendor/lib/vendor/deep`, so without this the subtraction never matches, the
  flag is always `False`, and a level-2 submodule declared unneeded is still
  refused for its url and still populated — silently.

  (c) `depth=depth`, `parent=parent.path if parent else ""` on the constructed
  `Submodule`, both written explicitly;

  (d) item 16's resolver call site takes the **parent's** resolved url as its
  base at depth ≥ 2:

  ```python
      base_url = parent.url_resolved if parent else task.repo_url
      ...
      url_resolved=None if full in unneeded else _resolve_submodule_url(
          base_url, declared, task_id=task.task_id, path=full),
  ```

  (e) both refusal messages gain a level clause when `parent` is not `None`:

  ```python
      f" That tree is the submodule {parent.path} at depth {depth - 1}."
  ```

  and report the **full** paths, so the message names something the task author
  can find.

  (f) item 2's typo refusal gains the `depth == 1` guard and the `deferred`
  term — D4's *Position 1* block verbatim, at item 2's pinned line.

- [ ] **Step 4: `derive_submodules` and `_derive_from`**, D2's blocks verbatim,
  including the `unexplained` second typo check and the explicit
  `if sub.declared_unneeded: continue`.

- [ ] **Step 5: replace the nested refusal with the depth refusal** in
  `_refuse_submodule_conflicts` — D3's condition and message. Keep it inside item
  2's `if not sub.declared_unneeded:` guard, at the same position (first after
  the url check), and keep the two refusals item 2 preserved outside that guard.

- [ ] **Step 6: `_init_submodules` per level** — D4's three changed lines and
  nothing below `checked`.

- [ ] **Step 7: the three docstrings that become false** (review-1 finding 15).

  (a) **`_refuse_submodule_conflicts`' first paragraph.** *"`mirrors` … is `{}`
  wherever no mirror exists yet — `derive_submodules` runs with no cache root …
  So the one refusal that needs a clone is skipped there and runs again from
  `_init_submodules`"* — all three clauses are false once `derive_submodules`
  takes `cache_root`. Rewrite: there are two calls **per level**, the first with
  `mirrors={}` because a url must be refused before it is cloned, the second with
  the level's real mapping; `_init_submodules` calls it a third time over the
  flat tuple, where it cannot fire and asserts agreement.

  (b) **`task_submodules`' docstring**: *"Deriving per caller costs two git reads
  against a warm cache"* — it now costs two reads plus one
  `ensure_pruned_mirror` fast-path lookup per submodule per level. State the real
  cost and that the lookup is a cache hit because the derivation built it.

  (c) **`_init_submodules`' docstring** gains a fifth load-bearing paragraph,
  **`THE UPDATE RUNS AT THE PARENT, NEVER WITH --recursive`**, carrying M4/M5 and
  M14, and one sentence in the post-condition paragraph saying M14's residue
  satisfies `any(iterdir())`, which is why the HEAD comparison is not optional.

- [ ] **Step 8: update the `derive_submodules` call sites in the tests.** Find
  them with `grep -rn "derive_submodules(" bakeoff/tests/` — **do not transcribe
  a count**; it was ten at round base and items 2 and 16 add more.

**Tests (`tests/test_tasks.py`).** A fixture `nested_submodule(tmp_path)` builds
`innermost` → `inner` (gitlink `vendor/deep`) → `super` (gitlink `vendor/lib`),
each inner level carrying a commit **past** its gitlink — the same reason
`upstream_submodule`'s docstring gives, so a pruned mirror can be told from an
unpruned one at every level. Build order in Task 5.

| test | asserts |
|---|---|
| `test_a_two_level_submodule_is_derived_with_full_paths_and_depths` | two entries; `path` `"vendor/lib"` / `"vendor/lib/vendor/deep"`; `depth` 1 / 2; `parent` `""` / `"vendor/lib"`; `local_path` `"vendor/lib"` / `"vendor/deep"` |
| `test_the_derived_order_is_parents_before_children` | `[s.path for s in subs] == ["vendor/lib", "vendor/lib/vendor/deep"]` |
| `test_a_submodule_three_levels_deep_is_refused_by_the_depth_cap` | a four-repository chain; `pytest.raises(TaskError, match="at most 2")` |
| `test_the_depth_refusal_names_the_full_path_and_the_cap` | the message contains `"vendor/lib/vendor/deep"`, `"most 2"` and *"an empty submodule directory leaves `git status --porcelain` clean"* |
| `test_an_orphan_gitmodules_stanza_at_the_cap_is_not_refused` | **review-1 finding 4.** A depth-2 submodule whose tree has a `.gitmodules` and **zero** gitlinks derives cleanly and materializes; `_has_gitmodules` is true for it and `_has_gitlinks` is false |
| `test_a_nested_submodule_url_that_is_not_https_is_refused` | production `_SUBMODULE_URL_PREFIX`, the inner's `.gitmodules` carrying `ssh://`; `TaskError` naming the **full** path |
| `test_the_inner_url_is_refused_before_its_mirror_is_built` | D2's ordering — see the wrapper below |
| `test_strip_paths_covering_a_nested_submodule_is_refused` | `strip_paths: ["vendor/lib/vendor"]` → `TaskError` naming the full path |
| `test_a_reference_diff_touching_a_nested_submodule_is_refused` | a reference diff touching `vendor/lib/vendor/deep/deep.py` → `TaskError` |
| `test_a_gitlink_with_no_url_at_the_inner_level_is_refused` | the message names the inner tree **and** the parent submodule at its depth |
| `test_a_level_two_submodule_can_be_declared_unneeded` | item 2's key with `["vendor/lib/vendor/deep"]`: derives, `declared_unneeded is True` on that entry only, no mirror is built for it, the directory is present and empty after `materialize` |
| `test_a_level_two_typo_in_submodules_unneeded_is_refused_after_the_recursion` | `["vendor/lib/vendor/deeep"]` → the `unexplained` message |
| `test_declaring_a_parent_and_its_child_is_refused` | **review-2 note N2.** `["vendor/lib", "vendor/lib/vendor/deep"]` → the `unexplained` message, because declaring the parent stops the recursion before the child exists. Pins the sentence Task 6 adds to the key's documentation block |
| `test_a_level_one_typo_still_beats_the_url_refusal` | item 2's near-miss (`vendor/libdeps`) against the ssh fixture: position 1 fires. Duplicates item 2's own test **on the nested fixture**, because that is where the `deferred` term could wrongly swallow it |
| `test_a_nested_submodule_is_populated_at_its_gitlink` | both directories exist and are non-empty, both `rev-parse HEAD` equal their gitlinks, both `status --recursive` lines have a leading space. Pins M13 against M5 |
| `test_a_nested_submodule_cannot_reach_the_future` | `cat-file -e <inner future sha>` in `vendor/lib` **and** `cat-file -e <innermost future sha>` in `vendor/lib/vendor/deep` both non-zero |
| `test_a_nested_submodule_does_not_move_start_sha` | D8 |
| `test_local_path_is_relative_to_the_parent` | pure; including that a `path` not under its `parent` raises `ValueError` out of `relative_to` rather than returning a plausible string |

**The selective wrapper** for
`test_the_inner_url_is_refused_before_its_mirror_is_built` (review-1 finding 12 —
a blanket patch raises at level 1, before any level-2 url exists to refuse,
because the **level-1** mirror is the repository whose `.gitmodules` declares the
inner url, M2):

```python
    real = tasks.ensure_pruned_mirror

    def only_the_innermost_is_forbidden(repo_url, sha, cache_root):
        if repo_url == str(fixture["innermost"]):
            raise AssertionError(
                "the innermost mirror was built before its url was refused"
            )
        return real(repo_url, sha, cache_root)

    monkeypatch.setattr(tasks, "ensure_pruned_mirror",
                        only_the_innermost_is_forbidden)
    with pytest.raises(TaskError, match="only https:// urls"):
        tasks.derive_submodules(task, mirror, tmp_path / "cache")
```

**Verify:**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "submodule or nested or start_sha or local_path or unneeded"
```

---

## Task 2: the build context

- [ ] **Step 1:** `images._extract_submodules` — iterate
  `sorted(subs, key=lambda s: s.depth)`, with a comment saying the sort is
  redundant with `derive_submodules`' pre-order and is kept because
  `mkdir(parents=True)` would hide the mistake: the deep directory would be
  created by the wrong archive and the failure would be a mode difference nobody
  looks at.

- [ ] **Step 2:** add a docstring paragraph **`ONE ARCHIVE PER LEVEL, PARENTS
  FIRST`** carrying M15.

**Tests (`tests/test_images.py`).**

- `test_the_build_context_carries_every_submodule_level` —
  `ctx/vendor/lib/mid.txt` **and** `ctx/vendor/lib/vendor/deep/deep.txt` both
  exist with the expected bytes.
- `test_the_second_archive_runs_parents_before_children` — monkeypatch
  `subprocess.run` to record `git archive <sha>` invocations; the level-1 sha
  precedes the level-2 sha.

**Verify:** `cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q -k "submodule or level"`

---

## Task 3: the gate

- [ ] **Step 1:** `git submodule status` → `git submodule status --recursive`;
  the non-zero-exit problem message quotes the command that ran.
- [ ] **Step 2:** `_gitlink_paths(container, prefix="")` — `git -C <prefix>` when
  non-empty, paths joined onto it with `PurePosixPath`. Keep the `partition`,
  never `split("\t", 1)[1]` rule and its docstring paragraph.
- [ ] **Step 3:** add `_is_own_repository(container, path)` using
  `rev-parse --show-prefix`, with D6's docstring. `container.exec` takes **no
  workdir argument** — it is hard-wired to `REPO_MOUNT` (`container.py:342`) — so
  every deeper read is `git -C <path> …`.
- [ ] **Step 4:** the caller recurses: collect level-1 gitlinks, probe each,
  descend into the `True`s, stamp `depth`, walk at most `_MAX_SUBMODULE_DEPTH`
  levels. `None` from a probe → both keys `None` plus a problem; `False` on a
  leading-space path → a problem naming the disagreement.
- [ ] **Step 5:** each parsed entry gains `"depth"`. `_parse_submodule_status` is
  **not** modified.
- [ ] **Step 6:** the too-deep problem, D6 point 6, **before** the `unmatched`
  problem.
- [ ] **Step 7:** `submodules_orphaned` per initialised level, with the
  all-or-`None` rule (D6 point 7).
- [ ] **Step 8:** `PREFLIGHT_VERSION` — **read the current value and add one**.
  Append to the ledger comment:

  ```
  #: <N-1> -> <N>: the submodule readers recurse (round-2 item 18). A verdict
  #: cached under <N-1> was written by a gate that ran `git submodule status`
  #: WITHOUT `--recursive` and built its authoritative path set from one
  #: `git ls-files -s -z` at the repository root, so on a task carrying a
  #: nested submodule it could not name the deeper entry at all. Measured
  #: 2026-09-02 (git 2.50.1) with level 1 populated and level 2 empty: the
  #: superproject's `git status --porcelain`, the inner's own `git status
  #: --porcelain`, `git diff HEAD` and non-recursive `git submodule status`
  #: are ALL clean, and `git submodule status --recursive` is the only reader
  #: that says anything. So a <N-1> PASS on any task with a submodule is a
  #: verdict about a tree this gate could not interrogate. The evidence shape
  #: also changes for every task, nested or not -- each `submodules` entry
  #: gains `depth`, and `submodules_orphaned` now means "every initialised
  #: level was read" where it used to mean "the root was read" -- so a warm
  #: blob and a fresh blob would otherwise sit in one cache describing two
  #: shapes, and a reader who cannot tell them apart reads an absent field as
  #: a positive negative claim.
  ```

**Tests (`tests/test_preflight.py`).**

| test | asserts |
|---|---|
| `test_a_nested_submodule_status_line_matches_only_its_own_path` | pure, over `_parse_submodule_status`, both lines with describe suffixes; two entries, correct paths, `unmatched == []` |
| `test_an_uninitialised_parent_is_not_descended_into` | **the M11 pin, and the most important test in the task.** A fake container whose `git -C vendor/lib ls-files -s -z` returns `"160000 8a71042… 0\t./\0"` at exit 0 and whose `rev-parse --show-prefix` returns `"vendor/lib/"`; no path containing `"./"` reaches the evidence and no level-2 entry is filed |
| `test_an_uninitialised_inner_is_not_descended_into` | the same at level 2 (`--show-prefix` → `"vendor/deep/"`), which review 1 measured and the first draft only implied |
| `test_a_failed_prefix_probe_makes_the_submodule_evidence_none` | probe exits non-zero → both keys `None` and a problem naming the probe |
| `test_every_submodule_entry_carries_its_depth` | a flat tree's entry has `"depth": 1`; the nested tree's two have 1 and 2 |
| `test_an_empty_inner_submodule_is_a_no_go` | level 2's marker `-` → `not result.ok`, a problem naming `vendor/lib/vendor/deep` |
| `test_a_tree_deeper_than_the_cap_is_named_before_the_unmatched_problem` | a status stream with a depth-3 line → the too-deep problem is present and precedes the `unmatched` one |
| `test_submodules_orphaned_is_read_at_every_level` | an inner stanza with no gitlink → reported as `"vendor/lib/<path>"` |
| `test_one_unreadable_level_makes_submodules_orphaned_none` | level 2's `git config` exits 128 → the key is `None`, not the level-1 list, plus a problem naming the level and the exit code |

**Verify:** `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "submodule or nested or depth or prefix or orphan"`

---

## Task 4: mutation anchors

Six entries in `scripts/mutation_check.py`, tuple format
`(name, file, exact_old, new, pytest selector, marker expr)`. **For each, verify
`grep -cF '<find>' <file>` prints `1` before adding the entry** —
`mutation_check.py` replaces the **first** occurrence only
(`path.write_text(original.replace(find, replace, 1))`), so a short anchor lands
on the wrong line and the entry reports MISSED (review-1 finding 13). Expected
delta: **+6**.

1. **`tasks: recurse one level past the depth cap`** — `src/bakeoff/tasks.py`,
   `_MAX_SUBMODULE_DEPTH = 2` → `_MAX_SUBMODULE_DEPTH = 3`,
   `tests/test_tasks.py -k three_levels_deep`, `not integration`.
2. **`tasks: initialise a nested submodule from the superproject root`** —
   `src/bakeoff/tasks.py`. The find is the **two-line** update call, because
   `cwd=parent_tree` occurs three times in that loop:

   ```
        _git("-c", "protocol.file.allow=always",
             "submodule", "update", "--init", "--", sub.local_path,
             cwd=parent_tree)
   ```

   → the same call with `cwd=Path(dest)`.
   `tests/test_tasks.py -k populated_at_its_gitlink`, `not integration`.
3. **`tasks: join a nested submodule path onto nothing`** —
   `src/bakeoff/tasks.py`,
   `    full = str(PurePosixPath(parent.path) / local) if parent else local` →
   `    full = local`, `tests/test_tasks.py -k full_paths_and_depths`,
   `not integration`. D1's hardest-argued decision, with two tests and, in the
   first draft, no anchor (review-1 finding 13).
4. **`tasks: descend into a submodule item 2 declared unneeded`** —
   `src/bakeoff/tasks.py`, `        if sub.declared_unneeded:` →
   `        if False:`,
   `tests/test_tasks.py -k level_two_submodule_can_be_declared_unneeded`,
   `not integration`. Anchors the `continue` Dependencies §1 promises (review-1
   finding 5).
5. **`preflight: read submodule status without --recursive`** —
   `src/bakeoff/preflight.py`, the argv list with `"--recursive"` → without it,
   `tests/test_preflight.py -k empty_inner_submodule`, `not integration`.
6. **`preflight: descend into a directory that is not its own repository`** —
   `src/bakeoff/preflight.py`, `_is_own_repository`'s
   `    return result.stdout.strip() == ""` → `    return True`,
   `tests/test_preflight.py -k uninitialised_parent_is_not_descended_into`,
   `not integration`.

- [ ] Re-run `mutation_check.py` **solo** and confirm no anchor from items 2, 10
  or 16 went stale. Those items anchor lines this plan edits around; the check
  fails loudly on a stale anchor by design, and the fix is to re-transcribe the
  anchor text, never to delete the anchor.

**Verify:** `cd bakeoff && .venv/bin/python scripts/mutation_check.py`

---

## Task 5: the integration leg

**This leg is not offline-and-free.** It carries `pytest.mark.task_image` because
it builds images: it needs a Docker daemon and, on a cold base, network (review-1
finding 16e). The *fixture* is offline — `submodule add` from local paths under
`-c protocol.file.allow=always` needs no network at either level. The module
docstring's `--basetemp` rule applies and is load-bearing here rather than
incidental: a repo bound from `/var/folders` appears inside the container as a
silently empty directory, which on this file's subject matter is the exact state
under test.

- [ ] **Step 1: `nested_superproject`**, mirroring `superproject`. Build order,
  which the nesting forces and which the first draft left to be re-derived:

  1. `innermost`: commit `deepdep/__init__.py` with `DEEP = 1`; record `pinned`;
     commit `DEEP = 999`; record `future`.
  2. `inner`: commit `libdep/__init__.py` with `VALUE = 1`; then
     `submodule add <innermost>` at `vendor/deep`,
     `git -C vendor/deep checkout <innermost pinned>`, `git add -A`, commit →
     this is `inner`'s **pinned** commit; then commit `VALUE = 999` → `inner`'s
     `future`.
  3. `super`: commit `calc.py` + `tests/test_calc.py`; then
     `submodule add <inner>` at `vendor/lib`,
     `git -C vendor/lib checkout <inner pinned>`, `git add -A`, commit → `base`;
     then the fix commit (`calc.py` fixed, `tests/test_calc.py` at `NEW_TEST`) →
     `head`; the reference diff is `git diff base head`.

  Every submodule-touching command carries `-c protocol.file.allow=always`.

  The suite **imports from both levels**, for the reason the existing file states
  — an assertion that cannot fail for the reason it is written for proves
  nothing:

  ```python
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "lib"))
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "lib" / "vendor" / "deep"))

  from calc import add
  from libdep import VALUE
  from deepdep import DEEP

  def test_both_submodule_levels_are_populated():
      assert VALUE == 1
      assert DEEP == 1

  def test_add():
      assert add(1, 1) == 0
  ```

- [ ] **Step 2:
  `test_the_image_and_the_run_tree_carry_the_same_nested_submodule_blob`**,
  asserting **six** things:

  1. `result.ok` — the suite imports from both levels, so an empty
     `vendor/lib/vendor/deep` is a collection error in both the red-before and
     the green-after run;
  2. `result.evidence["submodules"]` is exactly
     ```python
     [{"path": "vendor/lib", "sha": <inner pinned>, "initialised": True,
       "marker": " ", "depth": 1},
      {"path": "vendor/lib/vendor/deep", "sha": <innermost pinned>,
       "initialised": True, "marker": " ", "depth": 2}]
     ```
     plus whatever keys items 2, 16 and 17 added — **transcribe those from the
     code at implementation time**; and `evidence["submodules_orphaned"] == []`;
  3. the level-2 blob is byte-identical in the run tree and the build context —
     `<run>/vendor/lib/vendor/deep/deepdep/__init__.py` against
     `<build>/image-<task_id>/repo/vendor/lib/vendor/deep/deepdep/__init__.py`,
     each asserted to **exist first**, because two absent files would raise but
     an emptiness making both sides equal would have passed;
  4. both equal `DEEP = 1`, never `999`;
  5. `git -C <run>/vendor/lib/vendor/deep cat-file -e <innermost future>` exits
     non-zero — the innermost mirror was pruned;
  6. `git -C <run>/vendor/lib cat-file -e <inner future>` exits non-zero — the
     **inner** mirror was pruned (review-1 finding 16c: the first draft asserted
     only 5, and the run tree and the image are the two artifacts only this leg
     compares).

**Verify:**

```bash
cd bakeoff && .venv/bin/python -m pytest -v -m integration \
  tests/test_integration_submodules.py --basetemp="$HOME/.cache/bakeoff-pytest"
```

---

## Task 6: docs and `TASKS.md`

- [ ] **`bakeoff/taskset/HARVESTING.md`** — replace the **sub-bullet**
  `  - Nested submodules are refused.` (currently line 605) with a sub-bullet at
  the **same indentation**. The count in the parent bullet stays **six**: a
  sub-bullet is reworded, not added or removed (review-1 finding 16d).

  >   - **Submodules may be nested two levels deep; a third is refused.** A
  >     submodule of the superproject is depth 1 and a submodule of one of those
  >     is depth 2; a submodule at depth 2 whose own tree carries a gitlink is
  >     refused at load. Every level gets its own pruned mirror, its own
  >     `git submodule update --init` run from its parent's working tree, its own
  >     leak guards and its own `git archive` into the build context. The cap is
  >     a policy: measured 2026-09-02, git recurses to any depth, and every
  >     gitlink in the screened corpus that the harness can reach is flat (four
  >     measured at their pinned shas; the fifth is a private repository the
  >     harness never fetches). Raising it is a one-line change to
  >     `tasks._MAX_SUBMODULE_DEPTH` plus the tests that name it. A
  >     `.gitmodules` stanza with no gitlink is **not** nesting and is not
  >     refused at any depth.

- [ ] **The `submodules_unneeded` documentation block** (item 2 Task 5 put it in
  `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`; re-locate it —
  item 2 or 16 may have moved it). Two clauses, both about depth:

  > Each entry is a submodule path at `base_sha` **at any depth** — a level-2
  > submodule (`vendor/lib/vendor/deep`) can be declared unneeded while its
  > parent is populated. **Declaring a parent implicitly declines its children**,
  > because the harness does not descend into a submodule it was told not to
  > populate; so declaring both a parent and one of its children is refused, and
  > the refusal names the child.

  **SUPERSEDED by the round-2 final review (finding 1), and the shipped text
  says the opposite of the first clause.** `submodules_unneeded` is **depth 1
  only**: `_read_level` refuses an entry naming a gitlink at depth >= 2, with
  `test_a_level_two_submodule_cannot_be_declared_unneeded` and the anchor
  *"tasks: accept an unneeded declaration naming a depth-2 gitlink"* pinning it.
  The reason is a run-time reader this plan's D7 did not reach, because item 2's
  lever was reviewed before nesting existed. D7 measured the **initialised**
  depth-2 chain — a level-2 tracked edit gives `1 .M S.M. … vendor/mid` and an
  untracked file `1 .M S..U … vendor/mid`, both refused — and that finding
  stands. The **uninitialised** depth-2 case is the gap: item 17's second reader
  enumerates gitlinks with `git ls-files -s -z` at the **superproject root**, and
  `ls-files` does not descend through a gitlink, so a level-2 gitlink inside a
  *populated* level-1 submodule is never enumerated and never probed, while the
  level-1 path that *is* enumerated carries a `.git` and is skipped by
  `_uninitialised_with_content` by design; `--porcelain=v2` is silent too
  (measured 2026-09-03, git 2.50.1, on the `top → vendor/mid →
  vendor/mid/vendor/deep` fixture). `submodule_states()` therefore returns `{}`
  — which `container.py` documents as the positive measurement *"read, nothing
  dirty"*, not as an absence — `grader._submodule_edits` collapses it to `()`,
  and the ladder stamps `EMPTY_PATCH`: `resolved: False`, an accusation that the
  model changed nothing, permanent in an append-only file. The state is made
  unrepresentable at load rather than closed by a deeper reader, because that
  costs nothing today (no corpus task nests) while a per-level `_GITLINK_ARGV`
  walk is a new measured surface at run time. `PREFLIGHT_VERSION` does not move:
  a load-time refusal is not a gate verdict. The second clause — declaring a
  parent declines its children — is unchanged and still shipped.

- [ ] **`docs/BUILDING-A-TASK-SET.md`** §2, the *"needs a git submodule"* row:
  *"the submodule has no submodules of its own"* becomes *"the submodule chain is
  at most two levels deep — a submodule of a submodule is fine, a third level is
  refused at load"*. The row's other two checks are unchanged and now apply at
  every level.

- [ ] **`TASKS.md`** — remove the *Nested submodules* entry. Then:

  Add to the **item 17** entry:

  > Measured 2026-09-02 at two levels, with item 17's own argv: the
  > superproject's `git status --porcelain=v2 --ignore-submodules=none` reads
  > `1 .M S.M. … vendor/lib` for a level-1 uncommitted edit, a level-2
  > uncommitted edit **and** a level-2 commit alike — so the refusal fires at any
  > depth (soundness is complete) but the record and the message name
  > `vendor/lib` for work done two levels down. Running the same argv per
  > initialised level (`git -C vendor/lib …`) separates them: `N... mid.txt`,
  > `S.M. vendor/deep`, `SC.. vendor/deep`. `git submodule status --recursive` is
  > **not** the fix — it reads a leading space on both lines for both
  > uncommitted cases. One correction to item 17's D8 prose: `SC..` is the
  > staged, diff-carried gitlink move at depth 1 only; at depth 2 a commit reads
  > `S.M.` at the superproject and stages zero bytes.

  Add to the **item 10** entry:

  > Measured 2026-09-02 at two levels: both module directories leak the host
  > cache path in four files each (`config`, `logs/HEAD`,
  > `logs/refs/heads/main`, `logs/refs/remotes/origin/HEAD`); the guard pair
  > removes all four at the level it runs at and reaches no other level; and the
  > nested module directory is `.git/modules/<outer NAME>/modules/<inner NAME>`,
  > named by the submodule name and never its path. `_refuse_host_mirror_path`'s
  > `os.walk` over `dest/.git` covers this by construction.

- [ ] **`tasks/todo.md`** — the completed-work review section.

---

## Verification

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v          # unit; offline, no spend
cd bakeoff && .venv/bin/python scripts/mutation_check.py    # SOLO; offline, no spend
cd bakeoff && .venv/bin/python scripts/verify_logger.py     # GATE PASSED; needs Docker
cd bakeoff && .venv/bin/python -m pytest -v -m integration \
  --basetemp="$HOME/.cache/bakeoff-pytest"                  # needs Docker; network on a cold base
```

Then the real-task leg, which proves nothing regressed for the **flat** case —
there is no nested task to gate, which is the honest statement of this item's
verification ceiling. Needs Docker and, the first time, the repo mirror; no
credentials, no spend:

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
  --task-set ~/.cache/bakeoff-probe/taskset \
  --tasks tomlkit-514-inline-table-comment-separator
```

Expected: GO, `start_sha e1d72b883d2e452ca14835047e2fa7db02cdc4d8`, and
`evidence["submodules"] == [{"path": "tests/toml-test", …, "depth": 1}]`. The
`start_sha` pins that this item did not move the flat path (D8); the `depth: 1`
pins that the new field is filled for a non-nested task.

---

## What this does NOT do

- **It does not gate a real nested task**, because none exists: every gitlink in
  the screened corpus the harness can reach is flat, and the fifth is private
  (the table in *The measured defect*). The two-level evidence is the integration
  leg's synthetic superproject and the M-series. That is weaker verification than
  every other round-2 item's, it is stated rather than papered over, and it is
  the reason the cap is 2 (D3).
- **It does not close the pure-gitlink-edit blind spot**; that is item 17, whose
  refusal D7 measures to be depth-complete already. This plan's contribution is
  the measured localisation obligation and one correction to item 17's prose.
- **It does not touch `grader.py`, `checkpoints.py`, `schema.py`,
  `grade_schema.py`, or any of `GRADER_VERSION`, `GRADE_SCHEMA_VERSION`,
  `SCHEMA_VERSION`, `ORACLE_VERSION`.**
- **It does not add a term to `_pack_fingerprint`** or change
  `ensure_pruned_mirror`. It adds callers.
- **It does not add a leak post-condition.** Item 10's `_refuse_host_mirror_path`
  walks `dest/.git` with `os.walk` and covers every nested module directory by
  construction; this item only makes the guards run at every level.
- **It does not change any `check=` value**, at any level. Item 10 owns those.
- **It NARROWS one existing refusal.** A submodule whose tree carries a
  `.gitmodules` and **zero** gitlinks — the `git rm --cached` shape, M21 — is
  refused today at depth 1 and is accepted after this change, at any depth,
  because the predicate becomes a gitlink scan (D3). Preflight records it as
  `submodules_orphaned`, which is what this module's own comments say the inert
  shape deserves.
- **It does not relax any other refusal.** A non-`https://` resolved url, a
  `strip_paths` entry covering a submodule from above or below, and a reference
  diff touching submodule content are all still refused at **every** level, and
  D1's full-path decision is what keeps the last two from silently comparing
  unlike paths at depth 2.
- **It does not record a collected-test count**, so item 2's D9 gap is unchanged
  and applies at every level.
- **It does not make the depth cap configurable.** A per-task depth would be
  configuration reported as observation, and no measured task needs a different
  value.

---

## Open questions and rulings

1. **Should `evidence["submodules"]` entries carry `"parent"` as well as
   `"depth"`?** *Ruled: no.* `depth` is what the brief asks for and what a reader
   needs to see the shape of the tree; `parent` would be a second copy of a
   relationship the full path already encodes, with no reader. Recorded rather
   than settled silently because the argument against deriving `parent` *from*
   the path (D1: prefix arithmetic over paths is the defect
   `_parse_submodule_status` was corrected for) is also an argument for carrying
   it explicitly. If a reader ever needs the tree structure rather than the
   depths, add the field — do not slice the path. Review 1 did not dispute this.
2. **Should `_init_submodules` keep its `_refuse_submodule_conflicts` call now
   that the derivation runs every refusal with real mirrors?** *Ruled: keep it.*
   It cannot fire — D3's `sub.depth` guard is what makes that true, which is the
   part the first draft left unwritten — and it asserts that materialization and
   derivation agree about the same tuple. Removing it would also stale item 2's
   placement anchors for no benefit.
3. **Should the gate refuse a tree deeper than the cap, or report it?** *Ruled:
   report, and the mechanism is now written* (D6 point 6). The gate reads a tree;
   the loader refuses a manifest. Without the explicit problem the depth-3 status
   line falls into `unmatched` and is reported as the two readers disagreeing —
   true, and about the wrong cause.
4. **Does item 16's relative-url resolver need the parent's url or the parent's
   *resolved* url?** *Ruled here, not deferred* (review-1 finding 7 — deferring it
   to item 16, whose plan never raises it, left it owned by nobody). **The base is
   `task.repo_url` at depth 1 and `parent.url_resolved` at depth ≥ 2.**
   "Resolved-of-resolved" is not a case: a depth-1 relative url is already
   absolute by the time depth 2 is read, so the rule reduces to "resolve against
   the parent's resolved url". Item 16's `url_resolved is None` for a
   declared-unneeded parent is unreachable as a base, because this plan does not
   descend into a declared-unneeded submodule (D2's `continue`). What this item
   changes in item 16 is one argument at one call site; its resolver signature
   already takes the base.
5. ~~What happens if item 17 lands before this item with a non-recursive read?~~
   *Closed by measurement.* M17 shows item 17's superproject reader is
   depth-complete for its refusal and imprecise only in its record and its
   message, so there is no ordering hazard: whichever lands first, the soundness
   property holds. The localisation is an improvement item 17 may take or leave,
   and it is filed in `TASKS.md` either way (Task 6).

---

## Review 1 → changes

`.superpowers/broaden/round2/plan-18-review-1.md`, 16 findings, 6 blocking.
**16 addressed, 0 disputed.** Four findings were re-measured before being adopted
(4, 8, 9, 14); the rest were verified by reading the neighbouring plans.

| # | finding | change |
|---|---|---|
| 1 | **BLOCKING** — code blocks written against pre-item-2/10/16 source would revert three neighbours | Added **THE TRANSCRIPTION RULE FOR THIS PLAN** at the top, adopting item 10's `grep -cF == 1` convention. D1's dataclass rewritten post-2/16 (`url_declared`/`url_resolved`, `declared_unneeded`). Every `sub.url` in D2/D4/D5 is `sub.url_resolved`. D4's guard block rewritten as item 10 leaves it, with the explicit statement that **no line of item 10's guard code changes here** — only `checked`'s binding, which follows from D1's full path. Dependencies §10 rewritten around item 10's actual D1/D2/D4/D5; the test-level substitute is dropped and replaced by the (stronger) fact that `os.walk` over `dest/.git` covers nested module dirs by construction. |
| 2 | **BLOCKING** — moving item 2's typo refusal past the recursion breaks its two ordering tests | Settled in D4 as a **two-position** check: position 1 stays at item 2's pinned line, guarded to `depth == 1` and with a `deferred` term for paths under a level-1 gitlink; position 2 is the `unexplained` check in `derive_submodules` after the recursion. Verified 2026-09-02 that `_under` is component-wise, so item 2's two near-misses (`vendor/libdeps`, `vendor/typo`) are **not** deferred and both its tests pass unchanged. Two new tests cover the level-2 declaration and the level-2 typo, and a third re-runs item 2's level-1 ordering on the nested fixture. |
| 3 | **BLOCKING** — the depth refusal's condition was never written; unchanged it refuses every legal two-level task | Written out in D3: `sub.depth >= _MAX_SUBMODULE_DEPTH and mirror_for is not None and _has_gitlinks(...)`, with the paragraph explaining why `sub.depth` is what makes the second per-level call and the `_init_submodules` call inert — which is what OQ2 rests on. |
| 4 | the artifact test is `.gitmodules` presence, so the cap refuses a legal tree carrying a stale stanza | **Re-measured (M21):** a `git rm --cached`'d submodule leaves `cat-file -e <sha>:.gitmodules` at exit 0 with **zero** `160000` entries. Added `_has_gitlinks(mirror, sha)`; **both** the descent guard and the cap refusal read it, so they cannot disagree. Recorded in D3 and in *What this does NOT do* that this narrows an existing refusal, and added `test_an_orphan_gitmodules_stanza_at_the_cap_is_not_refused`. |
| 5 | the declared-unneeded skip was described as a `continue` and implemented as the coincidence it claimed to replace | `if sub.declared_unneeded: continue` is now in D2's transcribed loop, before the mirror lookup, with mutation anchor 4 on that line. |
| 6 | **BLOCKING** — "extracted verbatim" drops the prefix join item 2's exemption and flag need at depth ≥ 2 | Task 1 Step 3(b) names `full` explicitly and states its four uses (the `needed_gitlinks` subtraction, `declared_unneeded`, `path=`, both messages), with the consequence spelled out: without it a level-2 declaration never matches and the flag is always `False`. Mutation anchor 3 pins the join. |
| 7 | **BLOCKING** — `parent` is a `str`, so `parent.url` does not exist; `_derive_level` names nothing; the nested-relative-url base is owned by nobody | `parent` is threaded as `Submodule \| None` through `_read_level`/`_derive_from`; the two function names are used consistently and `_derive_level` is gone. Task 1 Step 3(d) changes item 16's **call-site argument** (`base_url = parent.url_resolved if parent else task.repo_url`), which is the actual gap — its signature already took the base. **OQ4 is now ruled here**, not deferred. |
| 8 | **BLOCKING** — the item-17 obligation names a command it rejected, and the flag is blind to the half it is about | **Re-measured with item 17's own argv (M17, four states).** The superproject reads `S.M.` for a level-1 uncommitted edit, a level-2 uncommitted edit and a level-2 commit alike; `--recursive` is blind to both uncommitted cases. D7 rewritten: item 17's **refusal is depth-complete already** and this plan owes it nothing for soundness; the obligation is the per-level v2 read for **localisation**, plus one correction to item 17's D8 prose about `SC..`. The `--recursive` instruction is gone from D7, Task 3 and Task 6, and the `TASKS.md` sentence is rewritten around the measured v2 strings. |
| 9 | the probe stakes correctness on host-composed absolute-path equality inside a bind mount | **Re-measured (M12, both levels, three states):** `--show-prefix` is empty iff the directory is its own repository. D6 point 3 adopts it, and the docstring says why the toplevel equality was rejected (the host/container path class `CLAUDE.md` already names) and why `--show-superproject-working-tree` was too. |
| 10 | "the gate reports a too-deep tree" had no mechanism, and what happened named the wrong cause | **Re-measured:** `status --recursive` lists the depth-3 line, so it lands in `unmatched`. D6 point 6 adds the explicit `too_deep` problem **before** the `unmatched` one, with the message written out and the one place a depth is read from a path's own text called out as deliberate. New test `test_a_tree_deeper_than_the_cap_is_named_before_the_unmatched_problem`. |
| 11 | per-level `submodules_orphaned` had no partial-failure rule | D6 point 7 rules it: **any** level failing sets the whole key to `None` plus a problem naming the level and the exit code; `[]` afterwards means "every initialised level was read and none has an orphan". New test `test_one_unreadable_level_makes_submodules_orphaned_none`. |
| 12 | `test_the_inner_url_is_refused_before_its_mirror_is_built` cannot pass as specified | The selective wrapper is written out in Task 1: only the **innermost** url raises, because reaching a level-2 url requires the level-1 mirror (M2). |
| 13 | anchor 2's find string is not unique and `mutation_check` replaces the first occurrence | The find is now the **two-line** update call, quoted verbatim; a `grep -cF == 1` pre-check is stated for **every** anchor; and a fifth anchor was added on D1's join (plus a sixth, splitting the preflight pair), for an expected delta of **+6**. |
| 14 | the corpus heading asserts a measured negative the table does not support | **Re-measured**: `git fetch --depth=1 --filter=blob:none <url> <pinned sha>` for yaml's three submodules — all three have **no** `.gitmodules` and **zero** `160000` entries at their pinned shas. With tomlkit's, that is **four of five measured flat**; the fifth (`fivetran/sqlglot-integration-tests`) is private over https and is the one item 2 declares unneeded, so the harness never fetches it. The heading and D3 reason 3 now state exactly that. |
| 15 | three docstrings become false, one scheduled | Task 1 Step 7 schedules all three: `_refuse_submodule_conflicts`' first paragraph, `task_submodules`' cost sentence, and `_init_submodules`' new fifth paragraph. |
| 16a | "eleven" call sites | Ten at round base, and items 2/16 add more. The plan gives the `grep` command and forbids transcribing a count. |
| 16b | `GRADER_VERSION` literal `"9"` will be stale | D7 drops the literal and uses the read-at-implementation-time instruction. |
| 16c | integration assertion 5 misses the level-1 future | Task 5 Step 2 now has **six** assertions; assertion 6 is `cat-file -e <inner future>` in `vendor/lib`. |
| 16d | HARVESTING indentation and count | Task 6 replaces a **sub-bullet** with a sub-bullet at the same indentation and says outright that the count stays **six**. |
| 16e | "every leg is offline and spends nothing" is false for the integration leg | Verification qualifies each leg (Docker; network on a cold base) the way `CLAUDE.md` qualifies the task gate. Task 5 Step 1 spells out the fixture's forced build order. |

### Review 2

`.superpowers/broaden/round2/plan-18-review-1.md`, `# Review 2`, 2026-09-02.
**APPROVE — 16/16 review-1 findings addressed, 0 not addressed, 0 disputed, 0
open findings.** The reviewer re-ran three of this plan's conclusion changes
from scratch rather than reading them, and all three reproduced on the same host
(git 2.50.1, Apple Git-155):

1. **`_has_gitlinks` is the right predicate.** A `git rm --cached`'d submodule
   leaves a readable `.gitmodules` (exit 0, stanza intact) with **zero**
   `160000` entries, so `_has_gitmodules` can be neither the descent guard nor
   the cap condition (M21).
2. **Item 17's refusal is depth-complete**, and the reviewer cross-checked the
   mechanism this plan only inferred: item 17's `_submodule_edits` refuses on
   `state[2] == "M"`, and the superproject's v2 sub-state is `S.M.` for a
   level-2 uncommitted edit **and** a level-2 commit alike, so it fires at any
   depth. `--recursive` is blind to both uncommitted cases (M17).
3. **`--show-prefix` is empty iff the directory is its own repository** — five
   rows plus the repository root, at both levels and against an ordinary
   subdirectory, with no caller-composed path (M12).

The reviewer also independently checked two things this plan asserts and does
not itself measure: that mutation anchor 4's find string
(`        if sub.declared_unneeded:`) is **unique** against item 2's other two
forms (`if not sub.declared_unneeded:` and
`for sub in subs if not sub.declared_unneeded`), and that item 16's resolver
signature already takes the base while its D6 comprehension hard-codes
`task.repo_url` — confirming that the gap OQ4 rules on is the call site and not
the signature.

**Two non-blocking notes, both folded in:**

| # | note | change |
|---|---|---|
| N1 | Dependencies §2 point 4 cited the wrong mechanism: item 2 builds `submodules_empty_after_suite` from `sorted(unneeded)` — the **manifest** key — not from `evidence["submodules"]`, so "this plan makes the evidence recursive" does not explain why the mapping needs no change. | Point 4 rewritten. The conclusion is unchanged and now rests on the real reason: the declared paths are already full superproject-relative paths at any depth, and item 2's `ls -A -- <path>` runs under `container.exec`'s hard-wired `workdir=REPO_MOUNT`, so it reaches a level-2 path in one exec with no recursion. **This item owes item 2 nothing there.** |
| N2 | Declaring both a parent and its child unneeded is now refused — the child defers at position 1, the recursion never descends into the declared parent, and position 2 raises `unexplained` — and nothing said so. The message is accurate but does not name the cause. | Three edits, none to a refusal message: a paragraph in D4 stating that **declaring a parent implicitly declines its children** and why the sentence belongs in the key's documentation rather than in the message; a Task 6 step adding two clauses to item 2's `submodules_unneeded` documentation block (paths are at any depth; a parent declines its children); and `test_declaring_a_parent_and_its_child_is_refused`, whose assertion is the `unexplained` message, so a later change making the pair legal is forced to move the documentation with it. |

Neither note changes a design decision, a version constant, a refusal condition
or the depth cap.
