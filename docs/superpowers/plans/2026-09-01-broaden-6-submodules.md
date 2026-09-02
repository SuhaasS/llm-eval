# Broadening 6: git submodules — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a repository whose suite needs a git submodule be cut as a task, instead of being refused because `git archive base_sha` drops the submodule and the directory arrives empty in both the image and the run tree.

**Architecture:** Nothing is declared in the manifest. A submodule's path, url and pinned commit are all *inside the tree at `base_sha`*, which the manifest already pins — so they are **derived** from the mirror with git's own parsers (`git ls-tree -r -z` for the gitlinks, `git config --blob <sha>:.gitmodules` for the urls), cross-checked against each other, and refused when they do not agree. Content comes from a per-`(submodule url, gitlink sha)` **pruned bare mirror** built by the existing `ensure_pruned_mirror` — the leak argument that function exists for applies one level down verbatim, and is measured below (M3). `materialize` initialises each submodule from that local mirror with no network; `build_task_image` extracts a second `git archive` into the submodule's path so the build context stays `.git`-free and oracle-free. Preflight reads `git submodule status` back out of the container. The grader gains one refusal, because a submission that moves a gitlink applies **green** and grades the wrong content (M11).

**Tech Stack:** Python 3.12 (the harness venv), pytest, git 2.50.1 (host) / 2.54.0 (container probe), Docker (integration legs only). All code under `bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3 the self-correction loop, §3.7 the manifest, §5.1 the image, §5.6 the submission diff, §6.4 confounds). Grader spec: `docs/superpowers/specs/2026-08-17-offline-grader-design.md`. Shared broadening context: `.superpowers/broaden/CONTEXT.md` (this is broadening **#6**). **`HANDOFF.md` at the repo root is mandatory reading before Task 2** — it is the ledger of the three ways the pruned-mirror cache has already been wrong.

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. Claims about external behaviour are annotated with what they were verified against (`Measured 2026-09-01, git 2.50.1`).
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section for later application.
- **`PREFLIGHT_VERSION` bumps by ONE from whatever value is on disk when Task 4 starts.** Broadenings 4 and 5 land before this one and each moves it (it reads `"5"` at the time of writing). Read the constant, add one; do not hard-code a literal. Same rule for `GRADER_VERSION` (reads `"3"`) and `GRADE_SCHEMA_VERSION` (reads `"1.0.0"`; this is a **minor** bump — an additive enum member) in Task 5.
- **`ORACLE_VERSION` does NOT move.** The quarantine derivation is unchanged; the oracle already reaches submodules through `materialize`.
- **`SCHEMA_VERSION` does NOT move.** No `RunRecord` field is added or changes meaning (D9). Broadenings 1–5 set the precedent: none of them touched `schema.py`.
- **Do not add a manifest key** (D1). Nothing new enters `manifest_digest` by declaration; the derivation is a pure function of `base_sha`, which `manifest_digest` and `start_sha` already pin.
- **Backwards compatibility:** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` loads unchanged, `pallets/click` at `63274a79…` has **no** gitlink, and its `start_sha` must stay `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`. A task with no submodules must take a zero-submodule path that runs no extra git command against the run tree.
- **The gated argv and the graded argv stay byte-identical.** Nothing here touches a runner argv.
- **A run always produces a record.** Nothing in this plan runs inside `execute_run`; every new failure is a `TaskError` raised before a container starts, or a preflight NO-GO, or a `NotGradedReason` in the offline grader.
- **Tests pin every new invariant.** Unit tests in `bakeoff/tests/`; integration tests are marked `integration` (+ `task_image` where a task image is built).
- **Commit hygiene:** stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. Subject in the repo's style (`feat:`/`fix:`/`docs:` + a sentence saying what breaks without it). End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` → 1129 passed, 46 deselected, plus whatever broadenings 1–5 added (broadening 3's review log records 1225 passed, 50 deselected).

---

## Measurements

Taken 2026-09-01 on this machine, **git 2.50.1 (Apple Git-155)** on the host and **git 2.54.0** (`alpine/git:latest`) inside the container probe. The fixture is a scratch superproject (`main.py`, `tests/test_x.py`, `.gitmodules`) with one submodule at `vendor/libdep`, pinned at `SUB_V1 = 942c381d…`, whose upstream has a **later** commit `SUB_V2 = 67540c61…` ("data-v2-FUTURE"). The superproject `base_sha` is `d285a319…`. Reproduce under the scratchpad.

**M1 — the gitlink is one tree entry, and `git archive` emits it as an empty directory.** `git ls-tree <base_sha> -r`:

```
160000 commit 942c381d88cecca36be86b2e902f554ad145ec44	vendor/libdep
```

`git archive --format=tar <base_sha> | tar -tvf -` on the mirror:

```
-rw-rw-r--  0 root root  189 .gitmodules
-rw-rw-r--  0 root root   12 main.py
drwxrwxr-x  0 root root    0 tests/
-rw-rw-r--  0 root root   26 tests/test_x.py
drwxrwxr-x  0 root root    0 vendor/
drwxrwxr-x  0 root root    0 vendor/libdep/
```

So `.gitmodules` **is** in the archive and the submodule directory is present **and empty**. That is the HARVESTING bullet's "the directory arrives empty", confirmed.

**M2 — a `--local` clone of a bare mirror leaves the submodule empty and the tree CLEAN.** After `git clone --local --no-checkout <mirror> run && git -C run checkout --detach <base_sha>`:

```
ls -A vendor/libdep          -> 0 entries
git status --porcelain       -> (empty, 0 bytes)
git submodule status         -> -942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep
git ls-files -s -- vendor    -> 160000 942c381d88cecca36be86b2e902f554ad145ec44 0	vendor/libdep
git rev-list --all | wc -l   -> 2      (the two superproject commits)
git cat-file -e 942c381d…    -> non-zero: does NOT resolve
```

Three consequences. The empty directory is **silent** — nothing in `git status` says the suite is about to fail to collect. `git submodule status`'s leading `-` is the one signal, and it is what preflight will read (D7). And the submodule's objects are **not** in the superproject store, so `_verify_pruned`'s `rev-list base_sha` sweep is untouched by any of this.

**M3 — `git submodule update --init` against the real url fetches the FULL submodule history, including the future.** In the run tree of M2:

```
superproject rev-list --all: 2 before, 2 after     (unchanged)
superproject cat-file -e 942c381d…: still does NOT resolve
objects land in: .git/modules/vendor/libdep/objects
vendor/libdep/.git is a FILE: "gitdir: ../../.git/modules/vendor/libdep"
submodule rev-list --all: 2
submodule cat-file -e 67540c61…  -> RESOLVES        <-- the FUTURE commit
submodule branches: (HEAD detached at 942c381), main, remotes/origin/HEAD, remotes/origin/main
```

The superproject invariant holds and **a new one is broken one level down**: `git -C vendor/libdep log main` in the run tree hands the agent submodule content newer than the pin. This is the whole argument for D3.

**M4 — the submodule can be served from a LOCAL mirror, with no network, and the pruned mirror carries no future.** A bare mirror of the submodule, refs deleted except one at `SUB_V1`, then `gc --prune=now` under `_GC_CONFIG`'s flags → `rev-list --all` = 1, `cat-file -e 67540c61…` non-zero, `cat-file -e 942c381d…` zero. Initialising the run tree from it:

| mechanism | result |
|---|---|
| `git -c submodule.<name>.url=<mirror> submodule update --init` | works, no network; submodule `rev-list --all` = **1**; **but** `git submodule status` afterwards reads `-942c381d…` — *uninitialised* — because the transient `-c` never wrote `submodule.<name>.url` into `.git/config` and `submodule init` therefore skipped its registration step (its `registered for path` line is absent) |
| `git config submodule.<name>.url <mirror>` **first**, then `git submodule update --init` | works, no network; `git submodule status` reads ` 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep (remotes/origin/pinned)` — the leading space is *initialised* |
| `git submodule update --init --reference <mirror>` | **wrong mechanism.** It still registers and clones from the `.gitmodules` url (network), and it writes `.git/modules/vendor/libdep/objects/info/alternates` pointing at the host cache path — the exact file `tasks._FORBIDDEN_PATHS` exists to refuse one level up |

The persistent-config form is the one D4 uses. The local clone **hardlinks**: the submodule's pack and the mirror's pack share inode `25603051` with link count 3, and no `alternates` file is written.

**M5 — `protocol.file.allow=always` is REQUIRED, and both failure modes exit 1.** git 2.50.1 refuses a submodule clone over the file transport by default (CVE-2022-39253 hardening), and the submodule mirror is a local path, so this is a **production** requirement, not a test-fixture one:

```
fatal: transport 'file' not allowed
fatal: clone of '<mirror>' into submodule path '<run>/vendor/libdep' failed
Failed to clone 'vendor/libdep' a second time, aborting
exit code: 1
```

An unreachable url is equally loud: `fatal: repository '<path>' does not exist` … `Failed to clone 'vendor/libdep' a second time, aborting`, **exit code 1**. Neither is a silent zero.

**M6 — the host cache path DOES survive `remote remove origin` + the url rewrite, and only `reflog expire` clears it.** *(Corrected in review round 1. The first version of this measurement claimed the opposite, for two compounding reasons worth recording: it was taken against a hand-rolled submodule mirror whose only ref was `refs/heads/pinned` rather than the `refs/heads/main` a real `ensure_pruned_mirror` publishes — so no local branch was created and no `logs/refs/heads/main` existed — and its "still has content" probe was `cat FILE && echo …`, which reports success on a file the expire had already emptied. Re-measured against the real `ensure_pruned_mirror` output.)*

After `git config submodule.<n>.url <pruned mirror>`, `submodule update --init`, `git -C <path> remote remove origin` and the url rewrite, **before** any expire:

```
grep -rl "cache/repos" .git/modules/            -> .git/modules/vendor/libdep/logs/HEAD
                                                   .git/modules/vendor/libdep/logs/refs/heads/main
logs/HEAD line 1:  0000… 271a4d8c… <name> <email> <epoch>	clone: from /…/cache/repos/…-libdep-271a4d8c….git
logs/HEAD line 2:  271a4d8c… 271a4d8c… <name> <email> <epoch>	checkout: moving from main to 271a4d8c…
logs/refs/heads/main: the same `clone: from /…/cache/repos/…` line
```

Then `git -C <path> reflog expire --expire=now --all` (**exit 0**):

```
grep -rl "cache/repos" .git/modules/            -> no match (grep exit 1)
find .git/modules/.../logs -type f -size +0     -> nothing
git -C vendor/libdep rev-parse HEAD             -> 271a4d8c…      (unchanged)
git submodule status                            -> " 271a4d8c… vendor/libdep (heads/main)"
git status --porcelain                          -> (empty)
```

So the expire is a **leak guard**, not symmetry with the superproject, and the guard is what the operator's cache layout depends on. `.git/modules/<name>/config` is clean either way (`[core]` plus `worktree = ../../../../vendor/libdep`); the leak is entirely in the reflogs, which is why a test that greps only the config files passes with the leak present.

**M7 — `git clean -xfd` does not wipe an initialised submodule.** `materialize` ends with it. Measured: `Removing junk.txt`, and `vendor/libdep` still has 3 entries afterwards, tree clean. (`-ffd` would; the existing command is `-xfd`.)

**M8 — `git add -A` does not recurse into a submodule, and that is what makes a submission diff blind.** With an *uncommitted* edit inside `vendor/libdep`:

```
git add -A ; git diff --cached <base_sha>            -> 0 BYTES
git add -A ; git diff --cached --name-only <base_sha> -> (empty)
git status --porcelain                                -> " M vendor/libdep"
git diff HEAD                                         -> "-Subproject commit 942c381d…" / "+Subproject commit 942c381d…-dirty"
```

`container.snapshot_diff` is exactly `git add -A` then `git diff --cached <base_sha>`, so an agent's edit inside a submodule is **absent from every checkpoint and from the final diff** — not even the `-dirty` gitlink line survives staging.

**M9 — an untracked file inside the submodule dirties the SUPERPROJECT.** `touch vendor/libdep/.pytest_cache_marker` → `git status --porcelain` reports ` M vendor/libdep` (17 bytes). `-uno` and `--ignore-submodules=untracked` both suppress it. Preflight's clean-tree check runs plain `git status --porcelain`, so a suite that writes inside the submodule is a preflight NO-GO — and `gitignore_extra` cannot fix it, because that key writes the *superproject's* `.gitignore` and the ignore rule would have to live inside the submodule's own tree.

**M10 — `git apply` of a diff touching a path INSIDE the submodule succeeds; `--index` refuses it.** On a clean initialised run tree:

```
git apply --check  <in-submodule patch>  -> exit 0
git apply          <in-submodule patch>  -> exit 0; vendor/libdep/libdep/__init__.py now "VALUE = 2"
                                            superproject: " M vendor/libdep"; submodule: " M libdep/__init__.py"
git apply --index --check <same patch>   -> exit 1: "vendor/libdep/libdep/__init__.py: does not exist in index"
```

`git apply --numstat -z` in a temp directory outside any repository reports `1  1  vendor/libdep/libdep/__init__.py`, so `tasks._numstat` / `_chunk_path` parse such a chunk normally and the loader can see the path. **`materialize` applies the test half with `--index`, so a reference diff touching submodule content fails loudly there** — but it fails with git's message, not one naming the manifest, which is why D2 refuses it earlier.

**M11 — a gitlink submission diff applies GREEN and grades the wrong content. This is the sharpest finding in this plan.** An agent that *commits* inside the submodule moves the gitlink, and `git add -A` **does** stage that:

```
diff --git a/vendor/libdep b/vendor/libdep
index 942c381..c464218 160000
--- a/vendor/libdep
+++ b/vendor/libdep
@@ -1 +1 @@
-Subproject commit 942c381d88cecca36be86b2e902f554ad145ec44
+Subproject commit c46421867cd1d0ac5a2038e156236a73a4cdeaac
```

`c464218…` exists **only** in that run tree's `.git/modules`. Applying that submission to a fresh materialized tree with the grader's own `git apply --index`:

```
warning: unable to rmdir 'vendor/libdep': Directory not empty
exit=0
git status --porcelain   -> "MM vendor/libdep"
git submodule status     -> +942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep (remotes/origin/pinned)
git -C vendor/libdep rev-parse HEAD -> 942c381d…      (UNCHANGED)
cat vendor/libdep/data.txt          -> "data-v1"      (the agent's edit is NOT here)
```

The apply is green, the index moves, the working tree does not, and the ladder then runs the suite against the **original** submodule content. The verdict that comes out is `resolved: False` — an accusation — for work that was done and that the pipeline could not see. Hence D8.

**M12 — the two-archive build context is complete, `.git`-free and oracle-free.** `git archive <base_sha>` from the superproject mirror, then `git archive <gitlink sha>` from the submodule's pruned mirror extracted into `repo/vendor/libdep`:

```
repo/.gitmodules
repo/main.py
repo/tests/test_x.py
repo/vendor/libdep/data.txt
repo/vendor/libdep/libdep/__init__.py
find repo -name '.git*'  ->  repo/.gitmodules only
```

No `.git` anywhere, and the tree is `base_sha`'s — the test half (the oracle) is still absent, which is the property `build_task_image`'s docstring exists to protect.

**M13 — the container can use the submodule with the `safe.directory` line `RunContainer` already sets.** `docker run -u 1000:1000 -v <run tree>:/repo` (under `$HOME`, because `/private/tmp` is not mounted into the Docker VM — the trap `CLAUDE.md` records, and it bit this measurement first: the bind mount came back as a silently empty directory), `git config --global --add safe.directory /repo` and nothing else:

```
id=1000 repo_owner=0
git status --porcelain              -> exit 0, empty
git submodule status                -> " 942c381d… vendor/libdep (942c381)", exit 0
git -C vendor/libdep rev-parse HEAD -> 942c381d…, exit 0
git -C vendor/libdep status --porcelain -> exit 0, empty
cat vendor/libdep/data.txt          -> data-v1
```

No second `safe.directory` entry is needed. `vendor/libdep/.git`'s `gitdir:` is **relative**, which is why it survives the host→container path change.

**M14 — the two derivation sources and their exact output shapes.**

```
git -C <mirror> config --blob <base_sha>:.gitmodules --list -z
  -> NUL-separated records, each "key\nvalue":
     submodule.vendor/libdep.path\nvendor/libdep\0submodule.vendor/libdep.url\n<url>\0
  -> exit 128 when the blob does not exist (a commit with no .gitmodules, or a typo'd path)

git -C <mirror> ls-tree -r -z <base_sha>
  -> NUL-separated records, each "<mode> SP <type> SP <sha>\t<path>"
     "160000 commit 942c381d…\tvendor/libdep"
```

Both are git's own parsers over NUL-delimited output, which is the same rule `_chunk_path`'s docstring states: paths come from git, never from a regex over a header.

**M16 — a `.gitmodules` entry with no gitlink is completely INERT.** *(Added in review round 1; the first draft refused this shape on a claim that was never measured.)* A superproject whose `.gitmodules` names `vendor/gone` (url `https://example.invalid/gone.git`) while the tree at that commit carries only the `vendor/libdep` gitlink:

```
git ls-tree -r <sha> | awk '$1=="160000"'   -> only vendor/libdep
git config --blob <sha>:.gitmodules --list  -> both vendor/libdep and vendor/gone
git submodule status                        -> "-271a4d8c… vendor/libdep"      (vendor/gone absent)
git submodule update --init                 -> exit 0
git status --porcelain                      -> (empty)
ls -A vendor                                -> libdep
```

git drives everything off the **index**, so an orphaned `.gitmodules` entry is never fetched, never checked out and never reported. The shape is real — a submodule deleted with `git rm --cached` and the `.gitmodules` stanza left behind, or a `base_sha` predating a submodule the manifest author saw at `HEAD` — and refusing it would refuse a task that works. The **reverse** direction is not symmetric: a gitlink with no url has nothing to fetch from and leaves a directory that M2 shows `git status` reporting as clean.

**M15 — `tomlkit`'s submodule is a plain https one.** `git clone --filter=blob:none --no-checkout --depth 1 https://github.com/python-poetry/tomlkit`, then `git show HEAD:.gitmodules`:

```
[submodule "tests/toml-test"]
	path = tests/toml-test
	url = https://github.com/BurntSushi/toml-test.git
```

`git ls-tree -r HEAD` → `160000 commit 08ed8697864548b3cdb4b8decbf496bef47e1c82  tests/toml-test`. One submodule, https, non-nested. `tomlkit` therefore moves out of HARVESTING's Excluded table (Task 7). This was taken at `HEAD`, not at a chosen `base_sha`; the task author re-checks at their own `base_sha`, which the derivation does for them anyway.

---

## Design decisions, settled

### D1. No manifest key. The submodule set is DERIVED from `base_sha`

An explicit `repo.submodules: [{path, url, sha}]` was considered and rejected. Every field of it is already inside the tree the manifest pins: the gitlink sha is a `160000` entry in `base_sha`'s tree (M1), and the path and url are in the `.gitmodules` blob at `base_sha` (M14). A declared copy would be configuration restating an observation — the shape `sampling`, `isolated` and `bedrock_model_id` are each built to avoid — and it can *drift*: a manifest naming a sha the tree does not carry would either be ignored (silent) or checked (in which case the declaration buys nothing over the check).

`manifest_digest` is `sha256(manifest bytes + reference bytes)` and does not move, because no byte of the manifest changes. It does not need to: the derivation's only input is `base_sha`, which the manifest already carries and which `manifest_digest` therefore already covers. `start_sha` does not move either — see D4 for where the initialisation is sequenced and why that is provable rather than asserted.

**The loader does not derive.** `load_task` runs on the host, offline, with no repository and no cache root; `test_tasks.py` constructs manifests whose `base_sha` is `"0" * 40` against a repo that does not exist. Making it derive would put `ensure_mirror` — the one network dependency in the task path — inside a function every unit test calls. So there is no `TaskManifest.submodules` field. Derivation happens at the two places that already hold a mirror (`materialize`, `build_task_image`) and in the grader, through one public entry point that all three call:

```python
tasks.task_submodules(task: TaskManifest, cache_root: Path) -> tuple[Submodule, ...]
```

which is `ensure_mirror` followed by the pure `tasks.derive_submodules(task, mirror)`. Derivation is milliseconds — two `git` reads against a mirror that already exists by then — so calling it three times is cheaper than threading a value through three signatures, and each caller gets D2's refusals for free rather than depending on a neighbour having run them.

### D2. Every refusal lives in `derive_submodules`, and there are five

The function returns a validated set or raises `TaskError` naming the task. All five refusals are for shapes that would otherwise be **quiet** — and refusal 1 is deliberately *narrower* than the first draft's, which also refused a shape measured to be inert (M16):

1. **A gitlink with no `.gitmodules` url.** One-directional, and the direction is the whole of the decision. A gitlink with no url has nothing to fetch from, so its directory stays empty and `git status --porcelain` reports the tree as clean (M2) — quiet, and fatal. The **reverse** is not: measured (M16), a `.gitmodules` entry naming a path with no gitlink is completely inert — `git submodule status` does not list it, `update --init` exits 0, the tree is clean and no directory is created, because git drives everything off the index. The first draft of this plan refused it, on a claim never measured; that would have refused a real and working shape (a submodule `git rm --cached`'d with its stanza left behind, or a `base_sha` predating a submodule the author saw at `HEAD`). Orphaned `.gitmodules` names are therefore **recorded, not refused** — in preflight's evidence as `submodules_orphaned`, where they are an observation of the container's own tree (D7).
2. **A url that is not `https://`.** Relative (`../toml-test.git`), `ssh://`, `git@host:path`, `git://` and `file://` are all refused. A relative url resolves against the superproject's remote, which `materialize` deletes and the container cannot reach; ssh needs keys the eval does not carry; `file://` points at the task author's laptop. Resolving relative urls against `task.repo_url` is a deliberate deferral, recorded in `TASKS.md` (Task 7) — it is the one refusal here likely to bite a real repository.
3. **Nested submodules.** If the submodule's tree at its gitlink carries its own `.gitmodules`, refuse. `submodule update --init` without `--recursive` would leave the inner one empty, which is M2's silence one level further down. Deferred, not designed around; `TASKS.md` records it.
4. **A `strip_paths` entry at or under a submodule path.** `_strip_paths_from_tree` runs `git rm -r` against the start state, and the submodule is not initialised at that point, so the strip would either no-op or remove the gitlink and leave `.gitmodules` naming a path that no longer exists.
5. **A reference-diff path (either half) at or under a submodule path.** `materialize` applies the test half with `--index`, which refuses such a path with git's own message (M10); the solution half is applied by preflight *without* `--index`, which **succeeds** (M10) and edits submodule content that no submission diff can ever contain (M8). A task whose fix lives inside a submodule is therefore ungradable by construction, and the loader saying so beats preflight discovering it four checks in. Path comparison is `tasks._under(p, (sub.path,))` alone — `_under` is `PurePosixPath.is_relative_to`, so it already matches the submodule path itself and an `or p == sub.path` conjunct would be dead code a mutation could not catch.

### D3. Content comes from a per-`(submodule url, gitlink sha)` PRUNED mirror, built by `ensure_pruned_mirror` unchanged

M3 is the argument: `submodule update --init` against the real url clones the submodule's whole history, including commits after the pin and a `remotes/origin/main` pointing at them. That is `ensure_pruned_mirror`'s founding measurement (`refs/heads/main` 181 commits ahead of the start state on `pallets/click`) reproduced one level down, and it is *differential* in the same way — only an arm that looks inside `vendor/…/.git` collects it.

`ensure_pruned_mirror(repo_url, base_sha, cache_root)` is already generic over its two keys. Calling it with `(sub.url, sub.sha)` gives the submodule its own cache entry, its own `_repo_lock` slug (the slug is derived from the url, and a submodule url is a different url), its own `_PRUNE_VERSION` marker, its own `_pack_fingerprint` fast path and — the part that matters — its own `_verify_pruned` post-condition, which asserts `cat-file -e <gitlink sha>`, no `_FORBIDDEN_PATHS` entry, no `*.keep`, and no commit outside `rev-list <gitlink sha>`. **Nothing in `tasks.py`'s mirror machinery is modified.** Per `HANDOFF.md`'s standing instruction, no term is added to `_pack_fingerprint` and no new cache-validity rule is invented; this is a new *caller*, not a new mechanism.

**Lock ordering.** `materialize` calls `ensure_pruned_mirror(task.repo_url, …)` *before* it takes `_repo_lock(task.repo_url, …)` for the clone, precisely because flock self-conflicts across fds in one process. The submodule mirrors are ensured at the **end** of `materialize`, long after that `with` block has exited — `_init_submodules` needs `dest` to exist, so it cannot run earlier — so no lock is ever nested inside another. (An earlier draft of this decision said "before that `with` block", which contradicted Task 2 Step 5; the end-of-function placement is the workable one and D4 gives its own, stronger reason for it.)

**Which mirror the derivation reads.** Inside `materialize` the derivation runs against the **pruned** superproject mirror, which already holds `base_sha`'s history and answers both reads (`ls-tree`, `config --blob`) — so `materialize` calls `derive_submodules(task, pruned)` directly rather than `task_submodules`, and takes no second trip through `ensure_mirror`. `build_task_image` and `grade_run` call `task_submodules`, because neither holds a mirror at the point they need the answer.

**Cost.** One prune per `(submodule url, gitlink sha)` for the whole task set, cached across invocations exactly like the superproject's. The per-run cost stays a hardlinked local clone (M4: link count 3, shared inode).

### D4. `materialize` initialises from the mirror, with the url persisted then rewritten, AFTER `start_sha` is settled

Sequenced at the very end of `materialize`, after `git clean -xfd` and after the `declared_start_sha` comparison. Two reasons, and the first is the load-bearing one:

- **`start_sha` provably cannot move.** It is computed from a tree that has never seen a submodule checkout. A test asserts the click task's `start_sha` is unchanged, but the *ordering* is what makes that a property rather than a coincidence, and a submodule task's own `start_sha` pin stays meaningful for the same reason. (Even sequenced earlier it would not move — M8 shows `git add -A` stages nothing for a submodule — but "would not move" and "cannot move" are different claims and only one of them survives an edit.)
- Fail fast on a moved start state before spending a clone per submodule.

`git clean -xfd` running *before* the init is therefore vacuous here, and M7 records that it would be harmless either way.

Per submodule, in order:

```
git config submodule.<name>.url <pruned mirror path>              # persisted: M4
git -c protocol.file.allow=always submodule update --init -- <path>   # M5
git -C <path> remote remove origin                                # host path out of the submodule
git config submodule.<name>.url <the .gitmodules url>             # host path out of .git/config
git -C <path> reflog expire --expire=now --all                    # LEAK GUARD -- M6, and check=True
```

The url is **persisted, not transient**, because M4's first row measured the cost of `-c`: the content lands but `git submodule status` still reads `-<sha>`, i.e. *uninitialised*, which is exactly the string D7's gate refuses and exactly the string a later `git submodule update` by the agent would act on by re-registering the `.gitmodules` url. `protocol.file.allow` stays transient (`-c`) because it is a property of this one invocation, not state the run tree should carry.

The rewrite back to the `.gitmodules` url is the same argument as `materialize`'s existing `git remote remove origin`: a host cache path inside the container is both a confusing error surface and a leak of the operator's cache layout. After the rewrite, the value is *truthful* (it is what upstream says) and unreachable from the container, which is the correct end state for a network the run is not allowed to use.

**The `reflog expire` is a leak guard and runs `check=True`**, which is a correction from this plan's first draft. Measured (M6): after the two rewrites above and *before* the expire, `.git/modules/<name>/logs/HEAD` **and** `logs/refs/heads/main` each carry `clone: from /…/cache/repos/…` — the pruned mirror publishes `refs/heads/main`, so the submodule clone creates a local branch and logs the source path twice. The expire clears both (exit 0, every log file emptied or removed) and leaves HEAD, `git submodule status` and `git status --porcelain` unchanged. `.git/modules/<name>/config` is clean *either way*, which is exactly why the first draft's config-only grep saw nothing and called the expire cosmetic. It is not `check=False`: a failed expire leaves the operator's cache layout inside the container with nothing saying so.

**Post-condition, per submodule, raising `TaskError`:** the path is non-empty and `git -C <path> rev-parse HEAD` equals the gitlink sha. The failure this catches is M2's: an empty directory is byte-identical to a suite that simply cannot import, and it would be scored as capability on every arm.

**A submodule task cannot be materialized offline the first time**, the same as any task — `ensure_mirror` is the one network dependency and it now has one more url to satisfy. Nothing changes about *when*: `run_matrix` builds images and materializes before the proxy starts.

### D5. The build context is two `git archive`s, never a copied run tree

`build_task_image` extracts `git archive <base_sha>` from the superproject mirror as today, then, for each submodule, `git archive <gitlink sha>` from that submodule's **pruned** mirror into `repo_dir/<path>` — a directory the superproject archive has already created, empty (M1). M12 measures the result: complete content, no `.git` anywhere, and `base_sha`'s tree, so the test half is still absent.

The rejected alternative is materializing a run tree and copying it. It carries `.git` (defeating the "no VCS-derived version" and "no history" properties in one move) and, worse, the run tree **is** `base_sha` plus the committed test half — copying it bakes the oracle into an image layer where no later step can tell it from a dependency, which is precisely what `build_task_image`'s existing docstring forbids.

Submodule extraction happens after the superproject extraction and **before** `_strip_build_context`, so the strip's symlink-escape guard sees the final tree. D2's refusal 4 means a strip path can never name a submodule path, so the order is stated for determinism rather than to resolve a conflict.

Reusing the pruned mirror rather than the full one is not merely convenient: it means the image layer and the run tree are built from the same object set, so `pip install -e .` in the image resolves against the same submodule content the agent will read.

### D6. `.git/modules/<name>` is inside `/repo/.git`, is readable by the agent, and is pruned

Stated rather than defended, because a reader will ask. The submodule's object store lives at `/repo/.git/modules/<path>/objects` and the agent can read it — `git -C vendor/libdep log --all`, `git cat-file`, `fsck`, all of it. It carries only the gitlink's history, because it was hardlink-cloned from a mirror `_verify_pruned` refused to publish otherwise: M4 measures `rev-list --all` = 1 in the run tree's submodule against 2 when the same init runs against the real url (M3). So the leak channel `ensure_pruned_mirror` closes for the superproject is closed here by the same mechanism and the same post-condition, one level down.

`git add -A` never recurses into it (M8), so no checkpoint diff can pick it up, and `container.py`'s `CLAUDE_CONFIG_DIR`-must-not-live-under-`/repo` rule is unaffected.

### D7. Preflight asserts initialisation by reading `git submodule status` back out of the container, and CHECKS ITS EXIT CODE

Two commands, inside the pinned image, on the materialized tree:

```
git ls-files -s -z            # the authoritative path set: every 160000 entry
git submodule status          # one line per submodule, first character = verdict
```

`git submodule status`'s first character is the verdict, and all three values were measured: `-` uninitialised or empty (M2, and M4's transient-`-c` row), `+` initialised but at a different commit than the gitlink, and a leading **space** for the one acceptable state.

**The exit code is checked, and this is a correction from the first draft.** Reading `.stdout` alone is the silent zero `container._checked_exec` exists to refuse: a non-zero `git submodule status` returns empty stdout, which parses to `[]`, which would be written as `evidence["submodules"] = []` — a positive observation of *none* manufactured out of a failure — with no problem raised. So a non-zero exit is a problem **and** sets `evidence["submodules"] = None`, which is the "a null says which kind of null it is" rule: `[]` is "measured, there are none", `None` is "the measurement did not happen".

Both reads go through `container.exec` with an explicit `exit_code` branch rather than a checked variant: `preflight` **collects** problems and returns a `PreflightResult`, and `run_matrix`'s `preflight(...)` call is unwrapped, so a raise from inside the gate becomes a traceback instead of the NO-GO the driver knows how to handle.

**Paths come from `git ls-files -s -z`, not from splitting the status line.** The status line is human-readable — `<char><sha> <path>[ (describe)]` — with no `-z` form and no escaping, so a path containing `" ("` mis-parses. `ls-files -s -z` gives the 160000 entries NUL-delimited, and the status line is then used only for its first character, matched to the authoritative path it starts with. Same rule as everywhere else in this codebase: paths come from git, never from a regex over a human-readable line. A status line the index has **no** gitlink for is a problem, never a fallback to the line's own text — a display-derived path re-entering the evidence is exactly what this sourcing exists to prevent.

`evidence["submodules"]` is a list of `{"path", "sha", "initialised"}`; a task with no submodules gets `[]` — an observation of *none*, not a missing key. `evidence["submodules_orphaned"]` carries the `.gitmodules` paths with no gitlink (D2's refusal 1, M16): computed in the container from `git config -f .gitmodules --get-regexp` minus the `ls-files` path set, recorded because it is inert rather than wrong, and recorded *here* rather than in the loader because in the container it is an observation of the tree the suite will actually run against. That probe's exit codes are **not** collapsed: `git config --get-regexp` exits **1** when nothing matches, which is the ordinary case for a repository with no `.gitmodules`, and `>1` on a real error. Writing `[]` for both would report an unreadable `.gitmodules` as the measured claim "there are no orphans", so `1` gives `[]` and anything above it gives `None` plus a problem.

This is an **observation**, not a restatement of configuration: the expected sha is not passed in, it is the index gitlink that `git submodule status` compares against internally. That is why the check does not take `task_submodules`'s output as an argument, and it is why it catches the case a configuration echo would not — a tree where `materialize`'s post-condition passed and something later emptied the directory.

`PREFLIGHT_VERSION` moves because a verdict cached under the old gate was written by one that never looked at a submodule at all — which is the entire reason that constant is in the cache key.

M13 measures that the `safe.directory /repo` line `RunContainer.__enter__` already sets is sufficient, at uid 1000 against a repo owned by uid 0, so `container.py` is **not** modified. The reviewer reproduced this at **git 2.39.5** (the production `python:3.12-slim-bookworm` git) as well as 2.54.0.

### D8. The grader refuses a submission that touches a gitlink — `NotGradedReason`, never a `GradeFailure`

M11 is the measurement and it is unambiguous: such a submission applies with exit 0, moves only the index, leaves the submodule's working tree at the original commit, and the ladder then grades content the agent never wrote. The verdict that falls out is `resolved: False` — an accusation — over a limitation of the harness's own diff capture (M8).

**This closes the committed case only.** An *uncommitted* submodule edit produces a zero-byte submission and lands on `EMPTY_PATCH`, which this refusal never sees. D10 records that residual; it is not fixable at grade time.

So `grade_run` refuses it *before* the container starts, with a new member:

```python
NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE = "submodule_gitlink_ungradable"
```

`resolved` is `None`, `not_graded_reason` names it, and the detail names the paths. It belongs in the first of the enum's three groups — "the run never produced a gradable submission" — because that is exactly what happened: the agent may well have fixed the bug inside the submodule, and the harness cannot see it. A `GradeFailure` would put the row in the denominator as a model failure, which is the accusation this refusal exists to prevent.

Detection reuses what is already imported: `_parse_submission(diff)` gives `(chunk, source, dest)` per file from git's own header parser, and a chunk whose `source` or `dest` equals a declared submodule path is the trigger. Equality, not `_under`: a path *inside* the submodule cannot appear in a submission at all (M8 — it is invisible), so anything under one is either impossible or a hand-edited diff, and the exact-match rule keeps the refusal narrow. A parse failure is left alone; `_apply_submission` already routes that to `LOSSY_DIFF_UNAPPLIABLE`/`APPLY_FAILED` with its own detail, and a second authority for the same shape is the mistake `not_graded_gate`'s docstring already names.

`GRADER_VERSION` +1 (a run graded under the old version was graded by a ladder that would have said `False` here) and `GRADE_SCHEMA_VERSION` minor +1 (an additive enum member; a reader that cannot tell the versions apart reads the absent value as a positive claim).

### D9. No record field, and `SCHEMA_VERSION` does not move

`Versions.container_image_digest` pins the image layer the run executed in — and under D5 that layer *contains* the submodule content, so it pins the content too. `task_id` plus `Versions.task_set_commit` name the manifest revision, whose `base_sha` is the derivation's only input. A reader goes record → task set at that commit → `base_sha` → `git ls-tree`, and gets the same tuple the harness got, offline, forever.

A `Versions.submodules` copied from the derivation would be configuration reported as observation. It would be an observation only if read out of the run container, and there is no free read to take it from: `execute_run` makes no git exec of its own, and adding one would put a new command inside the stretch guarded by "a run always produces a record" to re-derive something already pinned twice.

The measurement that *is* worth keeping lives where it is an observation: preflight's evidence (D7), beside `preflight_cache_key`'s `(manifest_digest, image, start_sha, PREFLIGHT_VERSION)`.

### D10. Limitations, recorded rather than engineered around

- **A submission diff cannot capture an edit inside a submodule** (M8: zero bytes). A task whose *fix* touches submodule content is out of the corpus — HARVESTING says so (Task 7) and D2's refusal 5 enforces the reference-diff half of it at load time. What no rule can prevent is an *agent* choosing to edit there anyway, and **D8 contains only half of that**: it sees a moved gitlink, which is what a *committed* submodule edit produces. An **uncommitted** one produces a zero-byte submission (M8), which reaches `_check_patch_non_empty`, becomes `GradeFailure.EMPTY_PATCH` and therefore `resolved: False` — indistinguishable from an agent that ran and changed nothing, and in the denominator. That residual is **not** closed by this plan and cannot be closed at grade time: by then the only evidence is a diff that is byte-identical to the honest empty case. The mitigations are upstream and are the corpus rule (a task whose fix lives in a submodule is out) and the prompt, which never asks for one. Recorded here rather than papered over, because the first draft of D8 claimed containment it does not have.
- **A suite that writes inside the submodule fails preflight's clean-tree check** (M9) and `gitignore_extra` cannot fix it. A screening criterion, not a bug.
- **Relative and non-https `.gitmodules` urls, and nested submodules, are refused** (D2). Both are deferrals with `TASKS.md` entries, not judgements that they are unsupportable.
- **The submodule's reflogs leak the host cache path until they are expired** (M6) — `logs/HEAD` and `logs/refs/heads/main` both record `clone: from /…/cache/repos/…`. Not a residual: `_init_submodules` expires them with `check=True` and `test_the_run_tree_carries_no_host_cache_path_for_the_submodule` asserts over the whole `.git/modules` subtree. Listed here because the first draft called the expire cosmetic and shipped a test that could not see the leak.

---

## File Structure

| File | Change |
|---|---|
| `bakeoff/src/bakeoff/tasks.py` | `Submodule` dataclass; `_SUBMODULE_URL_PREFIX`; `derive_submodules()`; `task_submodules()`; `_init_submodules()`; the call in `materialize` |
| `bakeoff/src/bakeoff/images.py` | `build_task_image` extracts one `git archive` per submodule into its path |
| `bakeoff/src/bakeoff/preflight.py` | `_submodule_status()`, the initialisation gate, `evidence["submodules"]`, `PREFLIGHT_VERSION` +1 |
| `bakeoff/src/bakeoff/grade_schema.py` | `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE`, `GRADE_SCHEMA_VERSION` minor +1 |
| `bakeoff/src/bakeoff/grader.py` | `_submodule_gitlinks_touched()`, the refusal in `grade_run`, `GRADER_VERSION` +1, `__all__` |
| `bakeoff/tests/test_tasks.py` | `upstream_submodule` fixture; derivation, refusals, init, `start_sha`-unmoved pins |
| `bakeoff/tests/test_images.py` | the second archive, and that a zero-submodule task is byte-identical to today |
| `bakeoff/tests/test_preflight.py` | the status parse and the refusal |
| `bakeoff/tests/test_grader.py` | the gitlink refusal and its `resolved is None` |
| `bakeoff/scripts/mutation_check.py` | two entries: the pruned-mirror choice in `_init_submodules`, and `grade_run`'s gitlink refusal |
| `bakeoff/tests/test_integration_submodules.py` | **created.** One `integration` + `task_image` leg over both halves at once |
| `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`, `tasks/todo.md`, `TASKS.md` | docs |

**Created:** `bakeoff/tests/test_integration_submodules.py` only. **`bakeoff/src/bakeoff/container.py` is deliberately unchanged** — M13 measured that the `safe.directory /repo` line it already sets is sufficient. **`bakeoff/scripts/run_matrix.py` and `bakeoff/scripts/grade.py` are deliberately unchanged** — both already call `build_task_image` and `materialize`, and both get submodules through them; no signature moves. **`bakeoff/src/bakeoff/oracle.py` is deliberately unchanged** for the same reason (`_derive` calls `materialize`). **`bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` is deliberately unchanged**: this broadening adds no manifest key (D1), and `pallets/click` at `63274a79…` has no gitlink, so there is not even a commented-out example to add. Task 7 says so in HARVESTING instead.

**Line numbers are deliberately absent from the task bodies.** Broadenings 4 and 5 land first; broadening 4 edits `preflight.py`, `oracle.py`, `grader.py` and `grade_schema.py`, and broadening 5 edits `images.py`, `preflight.py` and `tasks.py`. Anchor on symbol names: `TaskImage`, `TaskBudget`, `materialize`, `_strip_paths_from_tree`, `ensure_mirror`, `ensure_pruned_mirror`, `_under`, `build_task_image`, `_strip_build_context`, `render_dockerfile`, `PREFLIGHT_VERSION`, `NotGradedReason`, `not_graded_gate`, `grade_run`, `_parse_submission`.

Task order is derive → materialize → build → gate → grade → docs, so each task's tests pass against the tree the previous one left.

---

## Task 1: derive the submodule set from `base_sha`, and refuse the five bad shapes

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (new `Submodule` dataclass after `TaskBudget`; `_SUBMODULE_URL_PREFIX`; `derive_submodules`; `task_submodules` — all placed after `ensure_mirror`, before `_PRUNE_VERSION`)
- Test: `bakeoff/tests/test_tasks.py` (a new `# --- submodules ---` section at the end, plus one fixture beside `upstream`)

**Interfaces:**
- Produces: `tasks.Submodule(name: str, path: str, url: str, sha: str)` (frozen dataclass); `tasks.derive_submodules(task: TaskManifest, mirror: Path) -> tuple[Submodule, ...]`; `tasks.task_submodules(task: TaskManifest, cache_root: Path) -> tuple[Submodule, ...]`; `tasks._SUBMODULE_URL_PREFIX = "https://"` (a module constant, so a fixture whose upstream is a local path relaxes it with one `monkeypatch.setattr` and no production signature carries a test-only flag). Tasks 3 and 5 call `task_submodules`; Task 2's `materialize` calls `derive_submodules(task, mirror)` directly, against the pruned mirror it already holds (D3).
- Consumes: existing `tasks._git`, `tasks.ensure_mirror`, `tasks._under`, `tasks.TaskError`.

- [ ] **Step 1: Write the fixture**

Add to `bakeoff/tests/test_tasks.py`, immediately after the existing `upstream` fixture. **Do not modify `upstream`** — every other test in the module depends on its exact halves.

```python
SUB_LIB = "VALUE = 1\n"
SUB_LIB_FUTURE = "VALUE = 999\n"


@pytest.fixture
def upstream_submodule(tmp_path):
    """A superproject with one submodule, whose upstream has moved PAST the pin.

    The future commit is the point. `git submodule update --init` against the
    real url clones the submodule's whole history (measured 2026-09-01, git
    2.50.1), so a fixture whose submodule has nothing after the gitlink cannot
    tell a pruned mirror from an unpruned one -- which is the guarantee Task 2
    exists to pin.

    `-c protocol.file.allow=always` on every submodule-touching command: git
    refuses the file transport for submodules by default since the CVE-2022-39253
    hardening, and a local path is a file transport. Production needs the same
    flag for the same reason (the pruned mirror is a local path), so this is not
    a fixture-only concession.
    """
    lib = tmp_path / "libdep"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB)
    _sh("git", "init", "-q", cwd=lib)
    _sh("git", "config", "user.email", "t@t.test", cwd=lib)
    _sh("git", "config", "user.name", "t", cwd=lib)
    _sh("git", "add", "-A", cwd=lib)
    _sh("git", "commit", "-q", "-m", "libdep v1", cwd=lib)
    pinned = _sh("git", "rev-parse", "HEAD", cwd=lib)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB_FUTURE)
    _sh("git", "commit", "-q", "-am", "libdep FUTURE", cwd=lib)
    future = _sh("git", "rev-parse", "HEAD", cwd=lib)

    repo = tmp_path / "super"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(lib), "vendor/libdep", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "-C", "vendor/libdep",
        "checkout", "-q", pinned, cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "pin the submodule", cwd=repo)
    base = _sh("git", "rev-parse", "HEAD", cwd=repo)

    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "fix", cwd=repo)
    head = _sh("git", "rev-parse", "HEAD", cwd=repo)
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    return {"path": repo, "base": base, "head": head, "reference": reference,
            "lib": lib, "pinned": pinned, "future": future,
            "sub_path": "vendor/libdep"}
```

- [ ] **Step 2: Write the failing tests**

Add a `# --- submodules ---` section at the end of `bakeoff/tests/test_tasks.py`. `_write_task` needs an upstream dict shaped like the fixture's, so these pass `upstream_submodule` in its place.

```python
def _sub_task(tmp_path, up, **overrides):
    return load_task(_write_task(tmp_path / "set", up, **overrides))


def test_a_repository_with_no_submodules_derives_an_empty_tuple(tmp_path, upstream):
    task = _sub_task(tmp_path, upstream)
    mirror = tasks.ensure_mirror(str(upstream["path"]), upstream["base"],
                                 tmp_path / "cache")

    assert tasks.derive_submodules(task, mirror) == ()


def test_the_gitlink_and_the_gitmodules_blob_are_both_read(tmp_path,
                                                           upstream_submodule,
                                                           local_urls):
    """Path and sha come from `ls-tree`, url from `.gitmodules`, name from the
    section header -- all four from git's own parsers over NUL-delimited output.
    """
    up = upstream_submodule
    task = _sub_task(tmp_path, up)
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    (sub,) = tasks.derive_submodules(task, mirror)

    assert sub.path == "vendor/libdep"
    assert sub.name == "vendor/libdep"
    assert sub.sha == up["pinned"]
    assert sub.url == str(up["lib"])


def test_a_gitlink_with_no_gitmodules_url_is_refused(tmp_path,
                                                     upstream_submodule,
                                                     local_urls):
    """The quiet shape: no url, so the directory would simply stay empty and
    the suite would fail to collect on every arm -- with `git status
    --porcelain` reporting the tree as clean throughout.
    """
    up = upstream_submodule
    _sh("git", "rm", "-q", "--cached", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "drop .gitmodules", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base})
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    with pytest.raises(TaskError, match="vendor/libdep"):
        tasks.derive_submodules(task, mirror)


def test_a_gitmodules_entry_with_no_gitlink_is_NOT_refused(tmp_path,
                                                           upstream_submodule,
                                                           local_urls):
    """The other direction is inert, and refusing it would refuse a working
    task. Measured 2026-09-01, git 2.50.1: git drives everything off the
    index, so an orphaned stanza is not listed by `git submodule status`, is
    not fetched by `update --init` (exit 0), creates no directory and leaves
    the tree clean. The shape is real -- a submodule `git rm --cached`'d with
    its .gitmodules stanza left behind. Preflight records the name as
    `submodules_orphaned` instead.
    """
    up = upstream_submodule
    gitmodules = up["path"] / ".gitmodules"
    gitmodules.write_text(
        gitmodules.read_text()
        + '\n[submodule "vendor/gone"]\n\tpath = vendor/gone\n'
        '\turl = https://example.invalid/gone.git\n'
    )
    _sh("git", "add", ".gitmodules", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "orphan stanza", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])
    task = _sub_task(tmp_path, {**up, "base": base})
    mirror = tasks.ensure_mirror(str(up["path"]), base, tmp_path / "cache")

    assert [s.path for s in tasks.derive_submodules(task, mirror)] \
        == ["vendor/libdep"]


def test_a_non_https_submodule_url_is_refused(tmp_path, upstream_submodule):
    """The fixture's own url is a local path, which is exactly the shape that
    must be refused in production -- so this test needs no extra setup, while
    every OTHER test in this section relaxes the constant (see `local_urls`).
    """
    up = upstream_submodule
    task = _sub_task(tmp_path, up)
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="https://"):
        tasks.derive_submodules(task, mirror)


def test_a_strip_path_covering_a_submodule_is_refused(tmp_path,
                                                      upstream_submodule,
                                                      local_urls):
    up = upstream_submodule
    task = _sub_task(tmp_path, up, extra_yaml='strip_paths: ["vendor/libdep"]')
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="strip_paths"):
        tasks.derive_submodules(task, mirror)


def test_a_reference_diff_touching_a_submodule_path_is_refused(
        tmp_path, upstream_submodule, local_urls):
    """Measured 2026-09-01: `git apply` WITHOUT `--index` applies such a patch
    exit 0 and edits submodule content, which is how preflight's green-after
    check would pass on a fix no submission diff can ever contain.
    """
    up = upstream_submodule
    reference = up["reference"] + (
        "diff --git a/vendor/libdep/libdep/__init__.py"
        " b/vendor/libdep/libdep/__init__.py\n"
        "--- a/vendor/libdep/libdep/__init__.py\n"
        "+++ b/vendor/libdep/libdep/__init__.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    task = _sub_task(tmp_path, {**up, "reference": reference})
    mirror = tasks.ensure_mirror(str(up["path"]), up["base"], tmp_path / "cache")

    with pytest.raises(TaskError, match="reference diff"):
        tasks.derive_submodules(task, mirror)
```

**On relaxing the url rule in tests.** The fixture's url is a local path, and every test except `test_a_non_https_submodule_url_is_refused` needs it accepted. The relaxation is a **monkeypatched module constant**, not a keyword argument: a `allow_non_https=True` parameter would have to be threaded through `task_submodules`, `_init_submodules`, `build_task_image` and `grade_run` to reach the fixtures in Tasks 2, 3 and 5, and none of those has a production caller that would ever pass it — a parameter no shipping code sets is a parameter that can be set by mistake. Add this autouse-free fixture beside `upstream_submodule` and request it in every test above except the url one:

```python
@pytest.fixture
def local_urls(monkeypatch):
    """Accept the fixtures' local-path submodule urls.

    Empties `_SUBMODULE_URL_PREFIX` so `startswith` is vacuously true. The
    fixtures are local repositories on purpose -- the same reason `upstream`
    is -- so that the whole materialization path runs offline; a test that
    needed the network would be a test that stops running.
    """
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")
```

- [ ] **Step 3: Run the tests and watch them fail**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k submodule
```

Expected: `AttributeError: module 'bakeoff.tasks' has no attribute 'derive_submodules'`.

- [ ] **Step 4: Implement `Submodule` and the derivation**

In `bakeoff/src/bakeoff/tasks.py`, after `TaskBudget`:

```python
@dataclass(frozen=True)
class Submodule:
    """One gitlink at `base_sha`, DERIVED rather than declared (see the
    module docstring's third load-bearing item, and spec section 3.7).

    Every field is inside the tree `base_sha` already pins: `path` and `sha`
    are a `160000` entry in `git ls-tree`, `name` and `url` are the section
    header and value in the `.gitmodules` blob. A manifest key restating them
    would be configuration reported as observation, and it could drift from
    the tree while every other check passed.

    `name` is not `path`: the `[submodule "NAME"]` header is what
    `submodule.<name>.url` keys on, and git does not require the two to match.
    """

    name: str
    path: str
    url: str
    sha: str
```

Beside it, the url rule:

```python
#: The only submodule url shape the eval can fetch. A relative url ("../x.git")
#: resolves against the superproject's remote, which `materialize` deletes and
#: the container cannot reach; ssh needs keys the eval does not carry; file://
#: points at the task author's laptop. Refused at derivation so the failure
#: names the manifest, not `git submodule update`'s clone error.
_SUBMODULE_URL_PREFIX = "https://"
```

Then, after `ensure_mirror`:

```python
def derive_submodules(task: TaskManifest, mirror: Path) -> tuple[Submodule, ...]:
    """The submodules of `task.base_sha`, from git's own parsers, cross-checked.

    TWO independent readers, and their agreement is the guarantee. `ls-tree`
    gives the paths that have a gitlink; `.gitmodules` gives the paths that
    have a url. A path in one set and not the other is a QUIET failure in both
    directions -- a url with no gitlink makes the init fail with git's message
    instead of one naming the task, and a gitlink with no url leaves the
    directory empty, which is byte-identical to a suite that cannot import and
    is scored as capability on every arm (measured 2026-09-01, git 2.50.1:
    `git status --porcelain` is EMPTY in a run tree whose submodule directory
    has zero entries).

    Both reads are NUL-delimited, for the reason `_chunk_path`'s docstring
    gives: paths come from git, never from a regex over a header line.

    The url rule is enforced through the module constant
    `_SUBMODULE_URL_PREFIX` rather than a parameter, so the test fixtures --
    local repositories, so the whole materialization path runs offline -- relax
    it by monkeypatching one name, and no shipping signature carries a flag
    that only tests ever set.
    """
    entries = _git("ls-tree", "-r", "-z", task.base_sha, cwd=mirror).stdout
    gitlinks: dict[str, str] = {}
    for record in entries.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, _, rest = meta.partition(" ")
        if mode != "160000":
            continue
        gitlinks[path] = rest.split(" ")[-1]

    declared: dict[str, dict[str, str]] = {}
    if gitlinks or _has_gitmodules(mirror, task.base_sha):
        listing = _git("config", "--blob", f"{task.base_sha}:.gitmodules",
                       "--list", "-z", cwd=mirror, check=False)
        if listing.returncode != 0 and gitlinks:
            raise TaskError(
                f"{task.task_id}: {task.base_sha} carries gitlinks "
                f"({', '.join(sorted(gitlinks))}) but no readable .gitmodules, "
                "so no url exists to fetch them from and the directories would "
                "arrive empty."
            )
        for record in listing.stdout.split("\0"):
            key, _, value = record.partition("\n")
            if not key.startswith("submodule."):
                continue
            name, _, field = key[len("submodule."):].rpartition(".")
            if field in ("path", "url"):
                declared.setdefault(name, {})[field] = value

    by_path = {v["path"]: (name, v) for name, v in declared.items()
               if "path" in v}
    # ONE-DIRECTIONAL, and the direction is the decision. A gitlink with no
    # url has nothing to fetch from and leaves a directory `git status
    # --porcelain` reports as CLEAN -- quiet, and fatal. The reverse is inert:
    # measured 2026-09-01, a `.gitmodules` entry naming a path with no gitlink
    # is not listed by `git submodule status`, is not fetched by `update
    # --init` (exit 0) and creates no directory, because git drives everything
    # off the index. Refusing it would refuse a real, working shape -- a
    # submodule `git rm --cached`'d with its stanza left behind. Preflight
    # records those names as `submodules_orphaned` instead.
    unfetchable = sorted(set(gitlinks) - set(by_path))
    if unfetchable:
        raise TaskError(
            f"{task.task_id}: the tree at {task.base_sha} carries gitlinks "
            f"with no .gitmodules url: {', '.join(unfetchable)}. There is "
            "nothing to fetch them from, so each directory would arrive empty "
            "-- and an empty submodule directory leaves `git status "
            "--porcelain` clean, so nothing downstream would say so."
        )

    subs = tuple(
        Submodule(name=by_path[path][0], path=path,
                  url=by_path[path][1].get("url", ""), sha=sha)
        for path, sha in sorted(gitlinks.items())
    )
    _refuse_submodule_conflicts(task, subs, mirrors={})
    return subs
```

with the two helpers beside it:

```python
def _has_gitmodules(mirror: Path, sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}:.gitmodules", cwd=mirror,
                check=False).returncode == 0


def _refuse_submodule_conflicts(task: TaskManifest, subs: tuple[Submodule, ...],
                                mirrors: dict[str, Path]) -> None:
    """The four refusals that are not about the two readers agreeing.

    `mirrors` maps a submodule path to its pruned mirror, and is `{}` wherever
    no mirror exists yet -- `derive_submodules` runs with no cache root, so the
    one refusal that needs a clone (nested submodules, added in Task 2) is
    skipped there and runs again from `_init_submodules`, where a clone was
    going to happen anyway.

    Four refusals here, not five: the gitlink-with-no-url check is
    `derive_submodules`' own, because it is a property of the pair of readers
    rather than of one entry.

    Each is a shape that would otherwise fail LATE and describe the wrong
    thing: a bad url as `git submodule update`'s clone error, a nested
    submodule as an empty directory one level further down, a strip as a
    `git rm` against a path the manifest never meant, and a reference diff
    touching submodule content as a preflight green-after that passes on a fix
    no submission diff can contain (measured 2026-09-01: plain `git apply`
    edits inside a submodule at exit 0, and `git add -A` then stages NOTHING
    for it -- the checkpoint diff is zero bytes).
    """
    for sub in subs:
        if not sub.url.startswith(_SUBMODULE_URL_PREFIX):
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} declares url "
                f"{sub.url!r}, which is not {_SUBMODULE_URL_PREFIX}. A relative "
                "url resolves against a remote the run tree does not have, and "
                "ssh/file urls cannot be fetched by this eval."
            )
        # `_under` is `PurePosixPath.is_relative_to`, so it already matches
        # the path itself; an `or p == sub.path` conjunct would be dead.
        stripped = [p for p in task.strip_paths if _under(p, (sub.path,))]
        if stripped:
            raise TaskError(
                f"{task.task_id}: strip_paths names {', '.join(stripped)}, "
                f"which is at or under the submodule {sub.path}. The strip runs "
                "against a start state where the submodule is not initialised, "
                "so it would remove the gitlink and leave .gitmodules naming a "
                "path that no longer exists."
            )
        touched = [p for p in (*task.test_files, *task.solution_files,
                               *task.extra_files)
                   if _under(p, (sub.path,))]
        if touched:
            raise TaskError(
                f"{task.task_id}: the reference diff touches "
                f"{', '.join(sorted(touched))}, which is at or under the "
                f"submodule {sub.path}. A submission diff cannot carry an edit "
                "inside a submodule -- `git add -A` stages nothing for it -- so "
                "a task whose fix lives there is ungradable by construction."
            )
```

The **nested-submodule** refusal (D2's third) is deliberately not here: it needs the *submodule's own* mirror to ask whether its tree at `sub.sha` carries a `.gitmodules`, and that mirror is built in Task 2. Task 2 adds it to this same function.

Finally the public entry point, after `derive_submodules`:

```python
def task_submodules(task: TaskManifest, cache_root: Path) -> tuple[Submodule, ...]:
    """`ensure_mirror` then `derive_submodules`. THE entry point.

    Three callers -- `materialize`, `images.build_task_image` and
    `grader.grade_run` -- and each of them already needs the mirror. Deriving
    three times costs two git reads against a warm cache; threading one value
    through three signatures would make each caller depend on a neighbour
    having run the refusals in `derive_submodules`, which is the failure this
    module's every other check is shaped to avoid.
    """
    mirror = ensure_mirror(task.repo_url, task.base_sha, cache_root)
    return derive_submodules(task, mirror)
```

- [ ] **Step 5: Run the tests and watch them pass**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k submodule
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Expected: the new section green, and the full suite at its baseline count plus the new items. `test_a_repository_with_no_submodules_derives_an_empty_tuple` is the backwards-compatibility pin for every existing task.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py
git commit -m "feat: a task's submodules are derived from base_sha, not declared beside it

The path, url and pinned commit of every submodule are already inside the
tree base_sha pins, so a manifest key restating them could drift from the
tree while every other check passed. Two git readers -- ls-tree for the
gitlinks, config --blob for .gitmodules -- must agree, because either half
alone fails quietly: a url with no gitlink is git's clone error, and a
gitlink with no url is an empty directory that git status reports as clean.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 2: a pruned mirror per submodule, and `materialize` initialising from it

**Read `HANDOFF.md` at the repo root before starting.** Nothing in this task modifies `ensure_pruned_mirror`, `_verify_pruned` or `_pack_fingerprint`; it adds a *caller*. If you find yourself editing any of those three, stop — that file explains why the last three attempts to make the cache smarter were wrong.

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (`_init_submodules`; the nested-submodule refusal inside `_refuse_submodule_conflicts`; the call at the end of `materialize`)
- Test: `bakeoff/tests/test_tasks.py`

**Interfaces:**
- Produces: `tasks._init_submodules(task, dest: Path, subs: tuple[Submodule, ...], cache_root: Path) -> None`. `materialize`'s signature and return value are unchanged.
- Consumes: Task 1's `Submodule`, `task_submodules`, `_refuse_submodule_conflicts`; existing `ensure_pruned_mirror`, `_repo_lock`, `_git`.

- [ ] **Step 1: Write the failing tests**

Append to the `# --- submodules ---` section of `bakeoff/tests/test_tasks.py`.

```python
def _materialize_sub(tmp_path, up, **overrides):
    """Materialize a submodule task. Requires the `local_urls` fixture."""
    task = _sub_task(tmp_path, up, **overrides)
    start = materialize(task, tmp_path / "run", tmp_path / "cache")
    return task, tmp_path / "run", start


def test_materialize_populates_the_submodule_at_the_gitlink(
        tmp_path, upstream_submodule, local_urls):
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)

    assert (run / "vendor" / "libdep" / "libdep" / "__init__.py").read_text() \
        == SUB_LIB
    assert _sh("git", "-C", "vendor/libdep", "rev-parse", "HEAD", cwd=run) \
        == up["pinned"]


def test_the_run_trees_submodule_cannot_reach_the_future(
        tmp_path, upstream_submodule, local_urls):
    """The leak argument one level down. Measured 2026-09-01: `git submodule
    update --init` against the REAL url clones the whole submodule history,
    so without the pruned mirror `git -C vendor/libdep log main` hands the
    agent content newer than the pin -- differentially, since only an arm
    that looks collects it.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)
    sub = run / "vendor" / "libdep"

    found = subprocess.run(["git", "cat-file", "-e", up["future"]],
                           cwd=sub, capture_output=True)

    assert found.returncode != 0


def test_initialising_a_submodule_does_not_move_start_sha(
        tmp_path, upstream_submodule, local_urls):
    """`materialize` initialises AFTER `start_sha` is settled, so the pin is a
    property of the ordering rather than of `git add -A` happening not to
    stage a gitlink.
    """
    up = upstream_submodule
    task, run, start = _materialize_sub(tmp_path, up)
    head_tree = _sh("git", "rev-parse", "HEAD^{tree}", cwd=run)

    assert start == _sh("git", "rev-parse", "HEAD", cwd=run)
    assert head_tree == _sh("git", "rev-parse", f"{start}^{{tree}}", cwd=run)


def test_the_materialized_tree_is_clean_after_initialisation(
        tmp_path, upstream_submodule, local_urls):
    """The leading SPACE is the whole assertion. Measured: `-` is
    uninitialised (and is what a transient `-c submodule.<n>.url=` leaves
    behind), `+` is initialised at the wrong commit, and a space is the one
    acceptable state. Preflight gates on the same character in Task 4.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)

    assert _sh("git", "status", "--porcelain", cwd=run) == ""
    assert _sh("git", "submodule", "status", cwd=run).startswith(
        f" {up['pinned']}")


def test_the_run_tree_carries_no_host_cache_path_for_the_submodule(
        tmp_path, upstream_submodule, local_urls):
    """The WHOLE `.git/modules` subtree, not the config files.

    Measured 2026-09-01, git 2.50.1: after `remote remove origin` and the url
    rewrite the config files are already clean, and the cache path survives in
    `logs/HEAD` and `logs/refs/heads/main` as `clone: from /…/cache/repos/…`.
    A config-only assertion therefore passes with the leak present -- which is
    exactly what the first draft of this test did. The `reflog expire` in
    `_init_submodules` is what clears both, so this test is what makes that
    call load-bearing rather than deletable.
    """
    up = upstream_submodule
    _, run, _ = _materialize_sub(tmp_path, up)
    cache = str(tmp_path / "cache")

    leaking = [
        path for path in (run / ".git" / "modules").rglob("*")
        if path.is_file() and cache in path.read_text(errors="replace")
    ]

    assert leaking == []
    assert cache not in (run / ".git" / "config").read_text()
    assert str(up["lib"]) in (run / ".git" / "config").read_text()


def test_an_unreachable_submodule_mirror_is_a_task_error(
        tmp_path, upstream_submodule, local_urls):
    """Loud, not empty. Measured: `git submodule update --init` exits 1 both
    when the url does not resolve and when the file transport is refused, and
    an empty submodule directory leaves `git status --porcelain` clean.
    """
    up = upstream_submodule
    shutil.rmtree(up["lib"])

    with pytest.raises(TaskError):
        _materialize_sub(tmp_path, up)


def test_a_nested_submodule_is_refused(tmp_path, upstream_submodule,
                                       local_urls):
    """`submodule update --init` without `--recursive` leaves the inner one
    empty, which is the same silence one level further down.
    """
    up = upstream_submodule
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "x.py").write_text("X = 1\n")
    _sh("git", "init", "-q", cwd=inner)
    _sh("git", "config", "user.email", "t@t.test", cwd=inner)
    _sh("git", "config", "user.name", "t", cwd=inner)
    _sh("git", "add", "-A", cwd=inner)
    _sh("git", "commit", "-q", "-m", "inner", cwd=inner)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(inner), "inner", cwd=up["lib"])
    _sh("git", "commit", "-q", "-m", "nest", cwd=up["lib"])
    nested = _sh("git", "rev-parse", "HEAD", cwd=up["lib"])
    _sh("git", "-c", "protocol.file.allow=always", "-C", "vendor/libdep",
        "fetch", "-q", "origin", cwd=up["path"])
    _sh("git", "-C", "vendor/libdep", "checkout", "-q", nested, cwd=up["path"])
    _sh("git", "add", "-A", cwd=up["path"])
    _sh("git", "-c", "user.email=t@t.test", "-c", "user.name=t",
        "commit", "-q", "-m", "repin at the nested commit", cwd=up["path"])
    base = _sh("git", "rev-parse", "HEAD", cwd=up["path"])

    with pytest.raises(TaskError, match="nested"):
        _materialize_sub(tmp_path, {**up, "base": base})
```

- [ ] **Step 2: Run the tests and watch them fail**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "submodule and (materialize or future or start_sha or clean or host or unreachable or nested)"
```

Expected: the tree materializes with an empty `vendor/libdep`, so `test_materialize_populates_the_submodule_at_the_gitlink` fails on `FileNotFoundError` and `test_the_materialized_tree_is_clean_after_initialisation` fails on the `-`-prefixed `git submodule status` line.

- [ ] **Step 3: Add the nested-submodule refusal**

`_refuse_submodule_conflicts` gains a `mirrors: dict[str, Path]` argument — the per-submodule pruned mirrors, keyed by path — and this clause, placed first inside the per-`sub` loop after the url check:

```python
        mirror_for = mirrors.get(sub.path)
        if mirror_for is not None and _has_gitmodules(mirror_for, sub.sha):
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} at {sub.sha} declares "
                "submodules of its own. `git submodule update --init` does not "
                "recurse, so the inner directory would arrive empty -- and an "
                "empty submodule directory leaves `git status --porcelain` "
                "clean, so nothing downstream would say so."
            )
```

`derive_submodules` passes `mirrors={}` (it has no submodule mirrors and must stay usable without a cache root — Task 3's build path and Task 5's grader both call it that way); `_init_submodules` calls `_refuse_submodule_conflicts` a second time with the real mapping, once its mirrors exist. Two calls, and the second is a strict superset: the cheap refusals run wherever the derivation runs, and the one that needs a clone runs where a clone was going to happen anyway.

- [ ] **Step 4: Implement `_init_submodules`**

In `bakeoff/src/bakeoff/tasks.py`, immediately before `materialize`:

```python
def _init_submodules(task: TaskManifest, dest: Path,
                     subs: tuple[Submodule, ...], cache_root: Path) -> None:
    """Populate every submodule from a PRUNED mirror, with no network.

    Three things are load-bearing and each was measured on 2026-09-01 against
    git 2.50.1.

    THE MIRROR IS PRUNED. `git submodule update --init` against the declared
    url clones the submodule's whole history, `remotes/origin/main` included,
    so the run tree would carry submodule content NEWER than the gitlink --
    `ensure_pruned_mirror`'s founding leak, one level down, and differential in
    the same way, since only an arm that looks inside `vendor/.../.git`
    collects it. `ensure_pruned_mirror` is reused UNCHANGED: it is already
    generic over (url, sha), so the submodule gets its own cache entry, its own
    repo lock, and its own `_verify_pruned` post-condition. Do not add a term
    to `_pack_fingerprint` for this; see HANDOFF.md.

    THE REFLOG EXPIRE IS A LEAK GUARD. `logs/HEAD` and `logs/refs/heads/main`
    both record `clone: from <host cache path>`, and nothing else removes them
    -- the config files are clean without it, so a check that greps only those
    passes with the leak in place. It runs `check=True`.

    THE URL IS PERSISTED, THEN REWRITTEN. A transient `-c submodule.<n>.url=`
    populates the tree but leaves `git submodule status` reading `-<sha>` --
    uninitialised -- because `submodule init` skips its registration step when
    the value is already visible, so `.git/config` never gets it. Preflight
    reads exactly that character. The value is then rewritten to the
    `.gitmodules` url, and `origin` removed from the submodule, for the same
    reason `materialize` removes the superproject's: a host cache path inside
    the container is both an unresolvable error surface for the agent and a
    leak of the operator's cache layout.

    `protocol.file.allow=always` stays TRANSIENT and is REQUIRED in production,
    not only in tests: git has refused the file transport for submodules since
    the CVE-2022-39253 hardening, and a local pruned mirror is a file
    transport. Without it the update exits 1 with `transport 'file' not
    allowed`.

    The post-condition is the point of the function. An empty submodule
    directory leaves `git status --porcelain` EMPTY -- byte-identical to a
    healthy tree -- so a silent failure here reads as a suite that cannot
    import, on every arm, and is scored as capability.
    """
    if not subs:
        return
    # `materialize`'s own `_repo_lock` critical section has long exited by the
    # time this runs (it wraps the clone only), so `ensure_pruned_mirror`'s
    # acquisitions here are never nested inside it -- which flock would punish
    # with a same-process deadlock on the same slug.
    mirrors = {
        sub.path: ensure_pruned_mirror(sub.url, sub.sha, cache_root)
        for sub in subs
    }
    _refuse_submodule_conflicts(task, subs, mirrors=mirrors)
    for sub in subs:
        key = f"submodule.{sub.name}.url"
        _git("config", key, str(mirrors[sub.path]), cwd=dest)
        _git("-c", "protocol.file.allow=always",
             "submodule", "update", "--init", "--", sub.path, cwd=dest)
        _git("config", key, sub.url, cwd=dest)
        _git("remote", "remove", "origin", cwd=dest / sub.path, check=False)
        # check=True (the default). This is a LEAK GUARD, not tidiness:
        # measured 2026-09-01, the submodule's `logs/HEAD` AND
        # `logs/refs/heads/main` each carry `clone: from <host cache path>`
        # after the two rewrites above -- the pruned mirror publishes
        # `refs/heads/main`, so the clone creates a local branch and logs the
        # source twice -- and this expire is the only thing that removes them.
        # `.git/modules/<name>/config` is clean either way, which is why a
        # config-only check sees nothing.
        _git("reflog", "expire", "--expire=now", "--all", cwd=dest / sub.path)

        checked = dest / sub.path
        head = _git("rev-parse", "HEAD", cwd=checked, check=False)
        if head.returncode != 0 or head.stdout.strip() != sub.sha:
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} is not at its gitlink "
                f"{sub.sha} after initialisation (got "
                f"{head.stdout.strip() or 'nothing'}). An empty or wrong "
                "submodule leaves `git status --porcelain` clean, so the suite "
                "would simply fail to collect and every arm would be scored on "
                "an environment defect."
            )
        if not any(checked.iterdir()):
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} is empty after "
                "initialisation."
            )
```

The second call re-runs the cheap refusals, which is deliberate rather than wasteful: they are pure comparisons over tuples already in hand, and having one function own every refusal beats splitting them across two so that a reader has to know which half ran where.

- [ ] **Step 5: Call it from `materialize`**

At the very END of `materialize`, after the `declared_start_sha` comparison and before `return start_sha`:

```python
    # LAST. `start_sha` is computed from a tree that has never seen a submodule
    # checkout, which is what makes "initialising cannot move the pin" a
    # property of the ordering rather than a coincidence about what `git add -A`
    # happens to stage. Failing fast on a moved start state also avoids paying a
    # clone per submodule for a task that is already refused. Measured: `git
    # clean -xfd` above does NOT wipe an initialised submodule (that needs
    # `-ffd`), so the order is safe in the other direction too.
    _init_submodules(task, dest, derive_submodules(task, mirror), cache_root)
    return start_sha
```

`derive_submodules(task, mirror)`, not `task_submodules(task, cache_root)`: `mirror` here is the **pruned** superproject mirror `materialize` already holds, and it carries `base_sha`'s history, so both reads (`ls-tree`, `config --blob`) answer from it. Routing through `task_submodules` would take a second trip through `ensure_mirror` — a lock acquisition and a `cat-file` fork — per run tree, for an answer already available.

Extend `materialize`'s docstring with a paragraph saying the same thing, in the module's voice, cross-referenced to spec §5.1.

- [ ] **Step 6: Run the tests and watch them pass**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Then the one that matters for backwards compatibility — the real task's start state must not move:

```
cd bakeoff && .venv/bin/python -m pytest tests/ -q -k "start_sha or click"
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight
```

Expected: preflight PASS with `start_sha` still `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`. `--force-preflight` is required — the cache keys on `manifest_digest|image|start_sha|PREFLIGHT_VERSION` and none of the first three moves for a change to `materialize`.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py
git commit -m "feat: a submodule is populated from its own pruned mirror, never from its url

git submodule update --init against the declared url clones the submodule's
whole history, remotes/origin/main included, so the run tree would carry
content newer than the gitlink -- the leak ensure_pruned_mirror exists for,
one level down and just as differential. That function is reused unchanged;
it is already generic over (url, sha).

The url is persisted before the update rather than passed with -c, because a
transient value leaves git submodule status reading '-<sha>' -- uninitialised
-- which is the character preflight gates on. It is rewritten to the
.gitmodules url afterwards so no host cache path reaches the container.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 3: the build context gains one `git archive` per submodule

**Files:**
- Modify: `bakeoff/src/bakeoff/images.py` (`build_task_image`, plus a helper beside `_strip_build_context`)
- Test: `bakeoff/tests/test_images.py`

**Interfaces:**
- Produces: `images._extract_submodules(task, repo_dir: Path, cache_root: Path) -> None`. `build_task_image`'s signature is unchanged.
- Consumes: Task 1's `tasks.task_submodules`; Task 2's `tasks.ensure_pruned_mirror` usage pattern.

- [ ] **Step 1: Write the failing tests**

`bakeoff/tests/test_images.py` already fakes `ensure_mirror` and `images._run` for `test_build_task_image_strips_the_context_it_unpacks`, and lets the **real** `tar -x` run so the unpack under test is production's. Reuse that shape exactly; do not add a second faking mechanism. It needs one new helper, because the submodule archive must come from a real second repository:

```python
def _submodule_fixture(tmp_path):
    """A superproject mirror plus a submodule mirror, both real bare repos.

    Real rather than faked because the thing under test is that TWO archives
    are taken and the second lands inside the first's empty directory --
    measured 2026-09-01 (git 2.50.1): `git archive` emits `.gitmodules` and a
    zero-entry directory for the gitlink, so a stubbed single archive could
    not express the bug.
    """
    def sh(*args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    lib = tmp_path / "lib"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text("VALUE = 1\n")
    sh("git", "init", "-q", cwd=lib)
    sh("git", "config", "user.email", "t@t.test", cwd=lib)
    sh("git", "config", "user.name", "t", cwd=lib)
    sh("git", "add", "-A", cwd=lib)
    sh("git", "commit", "-q", "-m", "lib", cwd=lib)
    pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lib,
                            check=True, capture_output=True,
                            text=True).stdout.strip()

    sup = tmp_path / "sup"
    sup.mkdir()
    (sup / "calc.py").write_text("x = 1\n")
    sh("git", "init", "-q", cwd=sup)
    sh("git", "config", "user.email", "t@t.test", cwd=sup)
    sh("git", "config", "user.name", "t", cwd=sup)
    sh("git", "add", "-A", cwd=sup)
    sh("git", "commit", "-q", "-m", "base", cwd=sup)
    sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
       str(lib), "vendor/libdep", cwd=sup)
    sh("git", "add", "-A", cwd=sup)
    sh("git", "commit", "-q", "-m", "sub", cwd=sup)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=sup, check=True,
                          capture_output=True, text=True).stdout.strip()
    return {"sup": sup, "lib": lib, "base": base, "pinned": pinned}


def _sub_task_stub(fixture, strip_paths=()):
    class _Image:
        apt = ()
        pip = ()
        build = ()
        env = {}
        python = "3.12"   # broadening 5 lands FIRST; see the note below

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = str(fixture["sup"])
        base_sha = fixture["base"]
        image = _Image()
        strip_paths = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    _Task.strip_paths = tuple(strip_paths)
    return _Task()
```

**`_Image.python` is required, and only because broadening 5 lands first.** It adds `TaskImage.python` and `run_matrix`/`grade` index a `bases` mapping by it; the module's existing `_Image` stubs will already carry the attribute by the time this task starts. **Copy whatever value and shape the stubs in `test_images.py` use at that moment** rather than the `"3.12"` written above — if broadening 5's final form differs, the file itself is the authority. The same applies to any other attribute broadenings 4 and 5 added to the stand-ins in `test_images.py`, `test_preflight.py` and `test_grader.py`: start each of this plan's tasks by reading the existing stub, not by copying the one in this document.

Then the three tests. They fake only `images._run` (the `docker build`), so `git archive`, `tar -x`, `ensure_mirror` and `ensure_pruned_mirror` all run for real against the fixture — which is what makes the assertions about the unpacked tree meaningful:

```python
def test_the_build_context_carries_submodule_content(tmp_path, monkeypatch):
    """The context is two archives, not one. Measured 2026-09-01: `git archive`
    emits a submodule as an EMPTY DIRECTORY entry (`.gitmodules` is in the
    archive, the content is not), so a one-archive context ships an image whose
    `pip install -e .` resolves against a directory the run tree will have
    content in -- the same silent image/run disagreement a non-editable
    install produces, and nothing downstream compares the two.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")

    build_task_image(_sub_task_stub(fixture), "sha256:base",
                     tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    assert (repo / "vendor" / "libdep" / "libdep" / "__init__.py").read_text() \
        == "VALUE = 1\n"
    assert (repo / ".gitmodules").exists()


def test_the_build_context_still_carries_no_git_directory(tmp_path, monkeypatch):
    """`git archive` twice, never a copied run tree: a run tree is base_sha
    PLUS the committed test half, so copying one bakes the oracle into a layer
    no later step can tell from a dependency.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")

    build_task_image(_sub_task_stub(fixture), "sha256:base",
                     tmp_path / "build", tmp_path / "cache")

    repo = tmp_path / "build" / "image-t" / "repo"
    assert [p for p in repo.rglob(".git")] == []


def test_a_task_with_no_submodules_takes_no_extra_archive(tmp_path, monkeypatch):
    """Backwards compatibility. `pallets/click` at its base_sha has no gitlink,
    and a zero-submodule task must not gain a git call against the mirror.
    """
    fixture = _submodule_fixture(tmp_path)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    real_run = subprocess.run
    archives = []

    def counting_run(args, **kwargs):
        if list(args)[:2] == ["git", "archive"]:
            archives.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr("bakeoff.images.subprocess.run", counting_run)

    class _Image:
        apt = ()
        pip = ()
        build = ()
        env = {}

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = str(fixture["sup"])
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=fixture["sup"], check=True,
            capture_output=True, text=True).stdout.strip()
        image = _Image()
        strip_paths = ()
        test_files = ()
        solution_files = ()
        extra_files = ()

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    assert len(archives) == 1
```

**On `monkeypatch.setattr(tasks, "_SUBMODULE_URL_PREFIX", "")`.** The same relaxation Task 1's `local_urls` fixture makes, spelled out here because `test_images.py` does not import that fixture. `import bakeoff.tasks as tasks` at the top of the module if it is not already there. Emptying the prefix makes `startswith` vacuously true for the duration; monkeypatch restores it.

Mark none of these `integration`. The guarantee is a property of the unpacked context, and a test that has to build an image is a test nobody runs before committing — this module's stated position, in its own docstring.

- [ ] **Step 2: Run the tests and watch them fail**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q -k submodule
```

Expected: `vendor/libdep` exists and is empty, so the `read_text()` assertion raises `FileNotFoundError`.

- [ ] **Step 3: Implement the extraction**

In `bakeoff/src/bakeoff/images.py`, beside `_strip_build_context`:

```python
def _extract_submodules(task, repo_dir: Path, cache_root: Path) -> None:
    """A second `git archive` per submodule, into the empty directory the first
    one left.

    Measured 2026-09-01 (git 2.50.1): `git archive <base_sha>` emits
    `.gitmodules` and an EMPTY directory entry for each submodule path. So the
    image built from a one-archive context has a directory the run tree will
    have content in -- `pip install -e .` resolves against the wrong tree, and
    nothing downstream compares the two. Same class as a non-editable install:
    the image and the run disagree silently.

    From the PRUNED mirror, not the full one, so the image layer and the run
    tree are built from the same object set. Nothing here writes a `.git`, and
    the tree is `base_sha`'s, so the test half -- the oracle -- is still absent;
    that is why this is two archives rather than a copy of a materialized run
    tree, which would carry both.
    """
    from bakeoff.tasks import ensure_pruned_mirror, task_submodules

    for sub in task_submodules(task, cache_root):
        mirror = ensure_pruned_mirror(sub.url, sub.sha, cache_root)
        target = Path(repo_dir) / sub.path
        target.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "archive", "--format=tar", sub.sha],
            cwd=mirror, capture_output=True,
        )
        if archive.returncode != 0:
            raise ImageError(
                f"git archive {sub.sha} for submodule {sub.path} failed: "
                f"{archive.stderr.decode('utf-8', 'replace')}"
            )
        extract = subprocess.run(
            ["tar", "-x", "-C", str(target)], input=archive.stdout,
            capture_output=True,
        )
        if extract.returncode != 0:
            raise ImageError(
                f"unpacking submodule {sub.path} at {sub.sha} failed: "
                f"{extract.stderr.decode('utf-8', 'replace')}"
            )
```

The import is **local**, matching `build_task_image`'s existing local `from bakeoff.tasks import ensure_mirror` — `images.py` cannot import `tasks.py` at module scope without a cycle, and the existing call site says so.

- [ ] **Step 4: Call it from `build_task_image`**

Between the superproject `tar -x` and the `_strip_build_context(...)` call:

```python
    _extract_submodules(task, repo_dir, cache_root)
```

and extend `build_task_image`'s docstring: the context is now `git archive base_sha` **plus one `git archive` per gitlink**, still not the materialized run tree, and for the same reason.

- [ ] **Step 5: Run the tests and watch them pass**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/images.py bakeoff/tests/test_images.py
git commit -m "feat: the build context carries submodule content, from a second git archive

git archive emits a submodule as an empty directory entry, so a one-archive
context ships an image whose pip install -e . resolves against a tree the run
tree will have content in -- the same silent image/run disagreement a
non-editable install produces. A second archive from the submodule's pruned
mirror fills it, and the context still has no .git and still no test half,
which a copied run tree could not claim.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 4: preflight asserts every submodule is initialised at its gitlink

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (`_submodule_status`; the check, placed with the other environment checks that run before the suite; `evidence["submodules"]`; `PREFLIGHT_VERSION`)
- Test: `bakeoff/tests/test_preflight.py`

**Interfaces:**
- Produces: `preflight._submodule_status(container) -> list[dict]` returning `[{"path": str, "sha": str, "initialised": bool}]`; `evidence["submodules"]`.
- Consumes: the existing `container.exec` and the `problems` list.

- [ ] **Step 1: Write the failing tests**

`bakeoff/tests/test_preflight.py` already exercises the check bodies against a fake container; follow whichever fake it uses rather than adding a second.

```python
def test_the_submodule_status_parse_reads_the_leading_character():
    """Measured 2026-09-01, git 2.50.1: `-` uninitialised (which is also what a
    transient `-c submodule.<n>.url=` leaves behind), `+` initialised at a
    different commit than the gitlink, and a leading SPACE the one acceptable
    state. The prefix is the whole verdict -- an empty submodule directory
    leaves `git status --porcelain` clean, so nothing else reports it.
    """
    out = (
        " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep (942c381)\n"
        "-0000000000000000000000000000000000000000 vendor/other\n"
        "+1111111111111111111111111111111111111111 vendor/third (heads/x)\n"
    )

    parsed, unmatched = preflight._parse_submodule_status(
        out, ("vendor/libdep", "vendor/other", "vendor/third"))

    assert unmatched == []
    assert parsed == [
        {"path": "vendor/libdep",
         "sha": "942c381d88cecca36be86b2e902f554ad145ec44",
         "initialised": True},
        {"path": "vendor/other",
         "sha": "0" * 40, "initialised": False},
        {"path": "vendor/third", "sha": "1" * 40, "initialised": False},
    ]


def test_an_uninitialised_submodule_is_a_preflight_problem(monkeypatch,
                                                           tmp_path):
    """NO-GO, not evidence. An empty submodule directory leaves
    `git status --porcelain` clean, so the suite would simply fail to collect
    and every arm would be scored on an environment defect -- the Phase 0c
    shape, arriving through the dataset instead of the image.
    """
    container = _FakeContainer(submodule_status=(
        "-0000000000000000000000000000000000000000 vendor/libdep\n"
    ))

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("vendor/libdep" in p for p in result.problems)
    assert result.evidence["submodules"] == [
        {"path": "vendor/libdep", "sha": "0" * 40, "initialised": False}
    ]


def test_a_task_with_no_submodules_records_an_empty_list(monkeypatch, tmp_path):
    """Absence is recorded, never implied. `[]` is an observation of NONE; a
    missing key reads as "not measured", and the two must not render alike.
    """
    container = _FakeContainer(submodule_status="")

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.evidence["submodules"] == []
    assert result.evidence["submodules_orphaned"] == []
    assert not any("submodule" in p for p in result.problems)


def test_a_failed_submodule_status_records_None_and_a_problem(monkeypatch,
                                                              tmp_path):
    """A null says which kind of null it is.

    A non-zero `git submodule status` returns EMPTY stdout, which parses to
    `[]` -- a positive observation of "there are none" manufactured out of a
    failure. `None` is "the measurement did not happen", and the two must not
    render identically. This is the same silent zero `container._checked_exec`
    exists for: a failed `git diff` returns output byte-identical to a clean
    tree.
    """
    container = _FakeContainer(submodule_status_exit=128,
                               submodule_status="fatal: not a git repository\n")

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules"] is None
    assert result.evidence["submodules_orphaned"] is None
    assert any("git submodule status" in p for p in result.problems)


def test_a_status_line_whose_path_contains_a_paren_is_parsed_from_the_index():
    """The status line is human-readable and has no `-z` form, so splitting it
    on `" ("` mis-parses. The path set comes from `git ls-files -s -z`; the
    line contributes its first character and nothing else.
    """
    out = " 942c381d88cecca36be86b2e902f554ad145ec44 ven (dor)/lib (a)\n"

    parsed, unmatched = preflight._parse_submodule_status(out,
                                                          ("ven (dor)/lib",))

    assert unmatched == []
    assert parsed == [{"path": "ven (dor)/lib",
                       "sha": "942c381d88cecca36be86b2e902f554ad145ec44",
                       "initialised": True}]


def test_a_status_line_the_index_has_no_gitlink_for_is_a_problem():
    """The two readers disagreeing is not resolvable here, and it must not be
    papered over with the display line's own text -- a display-derived path in
    the evidence is what taking paths from `ls-files` exists to prevent.
    """
    out = " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/ghost\n"

    parsed, unmatched = preflight._parse_submodule_status(out,
                                                          ("vendor/libdep",))

    assert parsed == []
    assert unmatched == [out.rstrip("\n")]
```

`_FakeContainer` gains three keywords — `submodule_status: str = ""`, `submodule_status_exit: int = 0`, and `gitlinks: tuple = ()` (returned NUL-joined for `cmd[:3] == ["git", "ls-files", "-s"]`, in the `160000 <sha> 0\t<path>` shape `git` emits) — stored on `self` and returned from its `exec` for `cmd[:2] == ["git", "submodule"]` and the `ls-files` and `git config -f .gitmodules` probes. Add the branches **before** the existing `cmd[0] == "git"` branches so they are not swallowed by one of them; the class's other keywords are the model for the docstring line beside each.

The defaults are what keep every existing test in the module green: a task with no submodules is the state all of them are already in, and it must add no problem.

- [ ] **Step 2: Run the tests and watch them fail**

`-k submodule` does **not** collect `test_a_status_line_whose_path_contains_a_paren_is_parsed_from_the_index`, so select on the two names the new tests share instead:

```
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q \
  -k "submodule or status_line"
```

Expected: `AttributeError: module 'bakeoff.preflight' has no attribute '_parse_submodule_status'`. Check the collected count against the number of tests added — a `-k` that silently selects fewer is how a plan's "watch them fail" step passes on nothing.

- [ ] **Step 3: Implement**

```python
def _parse_submodule_status(out: str,
                            paths: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """`git submodule status`, one dict per line, paths taken from `paths`.

    The FIRST CHARACTER is the verdict and the other two values are context.
    Measured 2026-09-01 (git 2.50.1, and confirmed at 2.54.0 inside the
    container): a leading space means the submodule is initialised AND its HEAD
    equals the index gitlink; `-` means uninitialised, which includes the empty
    directory `git clone --local` leaves behind and the state a transient
    `-c submodule.<name>.url=` produces; `+` means initialised at a different
    commit.

    `paths` is the AUTHORITATIVE path set, from `git ls-files -s -z`'s 160000
    entries. The status line is human-readable -- `<char><sha> <path>[
    (describe)]` -- with no `-z` form and no escaping, so splitting it on `" ("`
    mis-parses any path containing those bytes. Same rule as `_chunk_path`:
    paths come from git, never from a regex over a display line. The line is
    used for its FIRST CHARACTER and nothing else.

    This is an OBSERVATION, not a restatement of the manifest: the expected sha
    is never passed in, it is the index gitlink git compares against on its own.
    That is what makes the check catch a tree emptied after `materialize`'s own
    post-condition passed.

    Returns `(parsed, unmatched)`. An `unmatched` line is one `git ls-files`
    has no gitlink for, which means the two readers disagree about this tree --
    a state nothing here can interpret. It is reported as a problem rather than
    resolved by falling back to `tail.strip()`: that fallback would put a
    DISPLAY-derived path back into the evidence, which is the exact thing
    taking paths from `ls-files` exists to prevent.
    """
    parsed, unmatched = [], []
    for line in out.splitlines():
        if not line.strip():
            continue
        marker, rest = line[0], line[1:]
        sha, _, tail = rest.partition(" ")
        match = [path for path in paths if tail.startswith(path)]
        if not match:
            unmatched.append(line)
            continue
        parsed.append({
            "path": max(match, key=len),
            "sha": sha,
            "initialised": marker == " ",
        })
    return parsed, unmatched


def _gitlink_paths(container) -> tuple[str, ...] | None:
    """Every 160000 entry in the container's index. `None` if it could not be read.

    `container.exec` and an explicit `exit_code` branch, NOT
    `_checked_exec` -- and the reason is the gate's contract, not a style
    preference. `_checked_exec` raises `ContainerError`, and `preflight`
    COLLECTS problems and returns a `PreflightResult`; `run_matrix`'s
    `preflight(...)` call is not wrapped, so a raise from in here would
    surface as a traceback instead of the NO-GO the driver knows how to
    handle. (It also returns an `ExecResult`, not a `str`, so `.split` on it
    would be an `AttributeError` rather than the read this needs.)

    The exit code still has to be looked at, for the reason `_checked_exec`
    exists at all: an empty stdout from a failed `ls-files` is byte-identical
    to a repository with no submodules. `None` here means "not read"; the
    caller turns that into a problem plus `None` evidence, the same shape the
    `git submodule status` branch uses.
    """
    result = container.exec(["git", "ls-files", "-s", "-z"])
    if result.exit_code != 0:
        return None
    paths = []
    for record in result.stdout.split("\0"):
        if record.startswith("160000 "):
            paths.append(record.split("\t", 1)[1])
    return tuple(paths)
```

and, beside the other pre-suite environment checks in `preflight()`:

```python
        status = container.exec(["git", "submodule", "status"])
        if status.exit_code != 0:
            # `None`, not `[]`. A non-zero exit returns empty stdout, which
            # parses to `[]` -- a positive observation of "there are none"
            # manufactured out of a failure, with no problem raised. The two
            # absences must not render identically.
            evidence["submodules"] = None
            evidence["submodules_orphaned"] = None
            problems.append(
                "`git submodule status` failed (exit "
                f"{status.exit_code}): {(status.stdout or status.stderr)[:500]}. "
                "Whether this tree's submodules are initialised is unknown, and "
                "an uninitialised one is invisible in `git status`."
            )
            submodules = []
        gitlinks = None if status.exit_code != 0 else _gitlink_paths(container)
        if status.exit_code == 0 and gitlinks is None:
            # Same shape as the branch above, different cause: the index could
            # not be read, so there is no authoritative path set and nothing
            # below can be trusted to name a submodule.
            evidence["submodules"] = None
            evidence["submodules_orphaned"] = None
            problems.append(
                "`git ls-files -s -z` failed, so the tree's gitlinks could not "
                "be read and no submodule claim can be made about it."
            )
            submodules = []
        elif status.exit_code == 0:
            submodules, unmatched = _parse_submodule_status(status.stdout,
                                                            gitlinks)
            evidence["submodules"] = submodules
            if unmatched:
                # NOT resolved by falling back to the display line's own text:
                # that would put a display-derived path into the evidence,
                # which is what taking paths from `ls-files` exists to prevent.
                problems.append(
                    "`git submodule status` named submodules the index has no "
                    "gitlink for: " + "; ".join(unmatched[:5])
                    + ". The two readers disagree about this tree."
                )
            # Inert, therefore recorded rather than refused (measured: git
            # drives submodules off the index, so a stanza with no gitlink is
            # never listed, never fetched and creates no directory). Recorded
            # HERE because in the container it is an observation of the tree
            # the suite will run against.
            declared = container.exec(
                ["git", "config", "-f", ".gitmodules", "--get-regexp",
                 r"^submodule\..*\.path$"]
            )
            if declared.exit_code in (0, 1):
                # 1 is git config's ORDINARY "no key matched" -- a repository
                # with no .gitmodules at all, or one whose stanzas all have
                # gitlinks. Collapsing it with >1 would report a genuine
                # failure (an unreadable or malformed .gitmodules) as the
                # measured claim "there are no orphans".
                evidence["submodules_orphaned"] = sorted(
                    {line.split(" ", 1)[1]
                     for line in declared.stdout.splitlines() if " " in line}
                    - set(gitlinks)
                )
            else:
                evidence["submodules_orphaned"] = None
                problems.append(
                    "reading .gitmodules failed (exit "
                    f"{declared.exit_code}): "
                    f"{(declared.stdout or declared.stderr)[:500]}"
                )
        stale = [s["path"] for s in submodules if not s["initialised"]]
        if stale:
            problems.append(
                "submodules are not initialised at their gitlink: "
                + ", ".join(stale)
                + ". The directory is empty or at the wrong commit, and "
                "`git status --porcelain` reports a tree in that state as "
                "CLEAN -- so the suite would simply fail to collect and every "
                "arm would be scored on an environment defect. Materialization "
                "should have populated it from the submodule's pruned mirror."
            )
```

No `safe.directory` line is added: measured 2026-09-01 at uid 1000 against a repo owned by uid 0, `git submodule status` and `git -C <path> status` both exit 0 with only the `/repo` entry `RunContainer.__enter__` already sets, because `<path>/.git`'s `gitdir:` is relative and survives the host→container path change.

- [ ] **Step 4: Set the two keys on the non-pytest early return**

`preflight()` has an early return for the case it cannot proceed past (the runner is not pytest), which writes an evidence dict before the checks above ever run. Broadening 3 set its own keys there for this reason and this task follows it:

```python
        evidence["submodules"] = None
        evidence["submodules_orphaned"] = None
```

`None`, not `[]`: nothing was measured. A key absent from that branch and present on the normal one is the same defect one layer down — a reader diffing two evidence files cannot tell "no submodules" from "this gate stopped before it looked".

- [ ] **Step 5: Bump `PREFLIGHT_VERSION`**

Read the constant, add one, and extend its comment block in the module's existing style with a line saying what the new number asserts. **Do not hard-code a literal** — broadenings 4 and 5 land first and each moves it.

- [ ] **Step 6: Run the tests and watch them pass**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight
```

The integration leg needs `--basetemp` under `$HOME`: the Docker VM mounts `$HOME` and not `/var/folders`, and a repo bind-mounted from there appears inside the container as a **silently empty directory** — which this measurement hit directly (M13's first attempt read `/repo` as empty and every git command as "not a git repository"). Report in the commit whether it was run.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py
git commit -m "feat: the gate reads every submodule's state out of the container it will run in

An empty submodule directory leaves git status --porcelain CLEAN, so nothing
downstream says the suite is about to fail to collect -- and it would be
scored as capability on every arm. git submodule status's leading character
is the one signal: a space means initialised at the index gitlink, '-' means
empty or unregistered, '+' means the wrong commit. The expected sha is never
passed in, so this is an observation rather than the manifest echoed back.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 5: the grader refuses a submission that moves a gitlink

**Read the measurement M11 before starting.** This task exists because such a submission applies **green** and grades the wrong content, which is the worst-shaped defect this pipeline can produce: `resolved: False` is an accusation, and it would land on an agent whose fix the harness simply could not see.

**Files:**
- Modify: `bakeoff/src/bakeoff/grade_schema.py` (`NotGradedReason`, its docstring's first group, `GRADE_SCHEMA_VERSION`)
- Modify: `bakeoff/src/bakeoff/grader.py` (`_submodule_gitlinks_touched`, the refusal in `grade_run`, `GRADER_VERSION`, `__all__`)
- Test: `bakeoff/tests/test_grader.py`

**Interfaces:**
- Produces: `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE = "submodule_gitlink_ungradable"`; `grader._submodule_gitlinks_touched(task, diff: str, cache_root: Path) -> tuple[str, ...]`.
- Consumes: Task 1's `tasks.task_submodules`; the existing `grader._parse_submission`, `_gated_result`, `build_grade_record`.

- [ ] **Step 1: Write the failing tests**

`test_grader.py` has `_task(...)` (a `SimpleNamespace`) and `_record(diff=..., **kw)`. Reuse both. The derivation itself is Task 1's subject, so these tests monkeypatch `grader.task_submodules` — the seam is deliberate: the grader's job here is the *refusal*, and a test that also built a real superproject would be testing Task 1 twice and would make this file need a git fixture it has never needed.

```python
_GITLINK_SUBMISSION = (
    "diff --git a/vendor/libdep b/vendor/libdep\n"
    "index 942c381..c464218 160000\n"
    "--- a/vendor/libdep\n"
    "+++ b/vendor/libdep\n"
    "@@ -1 +1 @@\n"
    "-Subproject commit 942c381d88cecca36be86b2e902f554ad145ec44\n"
    "+Subproject commit c46421867cd1d0ac5a2038e156236a73a4cdeaac\n"
)


def _with_submodule(monkeypatch, *paths):
    monkeypatch.setattr(
        grader, "task_submodules",
        lambda task, cache_root: tuple(
            SimpleNamespace(name=p, path=p, url="https://x/y.git", sha="0" * 40)
            for p in paths
        ),
    )


def test_a_gitlink_submission_is_not_graded_rather_than_failed(monkeypatch,
                                                               tmp_path):
    """Measured 2026-09-01, git 2.50.1: `git apply --index` of this diff on a
    freshly materialized tree exits 0 with only `warning: unable to rmdir`,
    moves the INDEX gitlink to a commit that exists nowhere but the original
    run tree's .git/modules, and leaves the submodule's working tree at the
    original commit. The ladder would then run the suite against content the
    agent never wrote and return `resolved: False` -- an accusation over a
    limitation of the harness's own diff capture, since `git add -A` stages
    NOTHING for an uncommitted edit inside a submodule.

    No container is started and no tree is materialized: the refusal sits
    beside `not_graded_gate`, before either, which is why this test needs
    neither Docker nor a mirror.
    """
    _with_submodule(monkeypatch, "vendor/libdep")

    graded = grade_run(_record(diff=_GITLINK_SUBMISSION), _task(),
                       "sha256:x", None, tmp_path / "cache",
                       tmp_path / "artifacts")

    assert graded.resolved is None
    assert graded.not_graded_reason == \
        NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE.value
    assert graded.grade_failure is None
    assert "vendor/libdep" in graded.not_graded_detail


def test_a_task_with_no_submodules_reaches_the_ladder(monkeypatch, tmp_path):
    """Backwards compatibility. `pallets/click` has no gitlink, and the check
    must return `()` for it without changing where the record goes next.
    """
    _with_submodule(monkeypatch)  # no paths

    touched = grader._submodule_gitlinks_touched(
        _task(), _GITLINK_SUBMISSION, tmp_path / "cache")

    assert touched == ()


def test_an_ordinary_submission_touches_no_gitlink(monkeypatch, tmp_path):
    """EQUALITY against the submodule path, not `_under`. A path INSIDE a
    submodule cannot appear in a submission at all (measured: `git add -A`
    stages nothing for it), so only the gitlink entry itself can ever match.
    """
    _with_submodule(monkeypatch, "vendor/libdep")

    touched = grader._submodule_gitlinks_touched(
        _task(), TEXT_DIFF, tmp_path / "cache")

    assert touched == ()


def test_an_unparseable_submission_is_left_to_the_existing_refusal(monkeypatch,
                                                                   tmp_path):
    """A parse failure is `_apply_submission`'s to name, not this check's.
    Two authorities for one shape is the mistake `not_graded_gate`'s docstring
    already records, and this one runs FIRST -- so a raise here would take
    `LOSSY_DIFF_UNAPPLIABLE` off every lossy row.
    """
    _with_submodule(monkeypatch, "vendor/libdep")

    assert grader._submodule_gitlinks_touched(
        _task(), "not a diff at all\n", tmp_path / "cache") == ()
```

`SimpleNamespace` and `grader` are already imported by the module; add `NotGradedReason` to its `from bakeoff.grade_schema import (...)` block if it is not there.

- [ ] **Step 2: Run the tests and watch them fail**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py -q -k gitlink
```

Expected: `AttributeError: module 'bakeoff.grader' has no attribute '_submodule_gitlinks_touched'`.

- [ ] **Step 3: Add the `NotGradedReason` member**

In `bakeoff/src/bakeoff/grade_schema.py`, at the end of the first group in the enum body (after `LOSSY_DIFF_UNAPPLIABLE`):

```python
    SUBMODULE_GITLINK_UNGRADABLE = "submodule_gitlink_ungradable"
```

and extend the class docstring's first bullet to name it, in the existing voice — the group is "the run never produced a gradable submission", and that is exactly what happened: the agent may have fixed the bug inside the submodule and the harness could not see it.

Bump `GRADE_SCHEMA_VERSION` by one **minor** (an additive enum member; read the value on disk, do not hard-code).

- [ ] **Step 4: Implement the detection and the refusal**

In `bakeoff/src/bakeoff/grader.py`:

```python
def _submodule_gitlinks_touched(task, diff: str, cache_root: Path) -> tuple[str, ...]:
    """The submodule paths this submission changes, if any.

    Measured 2026-09-01 (git 2.50.1). Two facts, and together they make such a
    submission ungradable rather than wrong:

      `git add -A` stages NOTHING for an uncommitted edit inside a submodule,
      so `container.snapshot_diff` -- `git add -A` then `git diff --cached
      base_sha` -- returns ZERO BYTES for it. Not even the `-dirty` gitlink
      line survives staging.

      An agent that COMMITS inside the submodule does move the gitlink, and
      `git apply --index` of the resulting diff on a freshly materialized tree
      exits 0 (`warning: unable to rmdir` only), moves the index entry to a
      commit that exists nowhere outside the original run tree's
      `.git/modules`, and leaves the submodule's working tree UNCHANGED.

    So the ladder would apply cleanly and then grade the original content, and
    the verdict would be `resolved: False` -- an accusation -- for work the
    harness could not capture. `NotGradedReason`, never `GradeFailure`: a
    GradeFailure puts the row in the denominator as a model failure, which is
    precisely the claim this refusal exists to avoid making.

    EQUALITY against the submodule path, not `_under`. A path INSIDE a
    submodule cannot appear in a submission at all (first fact above), so
    anything under one is either impossible or a hand-edited diff, and the
    exact-match rule keeps the refusal narrow.

    A submission that cannot be parsed returns `()` and is left alone.
    `_apply_submission` already names that shape with its own detail, and a
    second authority for one refusal is the mistake `not_graded_gate`'s
    docstring records.
    """
    paths = {sub.path for sub in task_submodules(task, Path(cache_root))}
    if not paths:
        return ()
    try:
        parsed = _parse_submission(diff)
    except TaskError:
        return ()
    touched = {p for _chunk, source, dest in parsed for p in (source, dest)}
    return tuple(sorted(touched & paths))
```

Import `task_submodules` in the existing `from bakeoff.tasks import (...)` block, alphabetically. Then in `grade_run`, immediately after the `not_graded_gate` block and **before** the artifacts wipe and `materialize` — the refusal must cost neither a tree nor a container:

```python
    gitlinks = _submodule_gitlinks_touched(
        task, record.artifacts.final_diff or "", Path(cache_root)
    )
    if gitlinks:
        return build_grade_record(record, task, image, oracle, _gated_result((
            NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE,
            f"the submission changes the gitlink(s) {', '.join(gitlinks)}; the "
            "referenced commit exists only in the run tree that produced it, "
            "and applying the diff moves the index without moving the "
            "submodule's content",
        )))
```

Bump `GRADER_VERSION` by one (read the value on disk) and extend its comment block: a run graded under the old version was graded by a ladder that would have returned `False` here. Add `"_submodule_gitlinks_touched"` to `__all__` only if the module's convention is to export private helpers — check; it is not, so leave `__all__` alone except for any name this task makes public. (It makes none.)

- [ ] **Step 5: Run the tests and watch them pass**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_grader.py tests/test_grade_schema.py -q
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
```

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/grade_schema.py bakeoff/src/bakeoff/grader.py bakeoff/tests/test_grader.py
git commit -m "fix: a submission that moves a gitlink is not graded, because it applies green

Measured: git apply --index of such a diff exits 0, moves the index to a
commit that exists only in the run tree that produced it, and leaves the
submodule's working tree unchanged -- so the ladder grades the ORIGINAL
content and returns resolved: False. That is an accusation for work the
harness could not capture, since git add -A stages nothing at all for an
uncommitted edit inside a submodule.

NotGradedReason rather than GradeFailure: the row leaves the denominator
instead of counting as a model failure.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 6: the mutation anchors, and one integration leg over both halves

**Files:**
- Modify: `bakeoff/scripts/mutation_check.py` (two entries)
- Create: `bakeoff/tests/test_integration_submodules.py`

**Interfaces:** consumes everything Tasks 1–5 produced; produces nothing new.

- [ ] **Step 1: Add the two mutation entries**

Broadenings 2 and 3 each added one; this one adds two, because two guarantees here are single lines whose reversal leaves every other test green. Follow the file's exact 6-tuple shape (`name`, `source path`, `exact line`, `replacement`, `pytest selector`, `marker expression`) and match the indentation of the anchor line **byte for byte** — a stale anchor is a hard failure, not a warning.

```python
    (
        # The whole point of D3. Reverting to the FULL mirror leaves every
        # unit test green -- the tree materializes, the suite collects, the
        # gate passes -- while `git -C <path> log --all` in the run tree hands
        # the agent submodule content newer than the gitlink, differentially,
        # since only an arm that looks collects it.
        "tasks: populate a submodule from the unpruned mirror",
        "src/bakeoff/tasks.py",
        "        sub.path: ensure_pruned_mirror(sub.url, sub.sha, cache_root)",
        "        sub.path: ensure_mirror(sub.url, sub.sha, cache_root)",
        "tests/test_tasks.py -k cannot_reach_the_future",
        "not integration",
    ),
    (
        # Without this the ladder applies a gitlink diff GREEN (measured:
        # exit 0), grades the ORIGINAL submodule content and stamps
        # `resolved: False` -- an accusation -- on work the harness could not
        # capture.
        "grader: grade a submission that only moves a gitlink",
        "src/bakeoff/grader.py",
        "    if gitlinks:",
        "    if False:",
        "tests/test_grader.py -k gitlink_submission_is_not_graded",
        "not integration",
    ),
```

Two more were considered and rejected as anchors, with reasons, so nobody re-derives them: the persisted-url line (`_git("config", key, str(mirrors[sub.path]), …)`) has no single-line reversal — deleting it changes *what* is configured, not whether a branch is taken, and the mutation harness swaps lines rather than removing them; and preflight's `if stale:` is already covered by `test_an_uninitialised_submodule_is_a_preflight_problem`, which fails on the returned `PreflightResult` rather than on a string, so its mutation would be caught by a test that already exists rather than pinning something new.

- [ ] **Step 2: Run the mutation check, SOLO**

```
cd bakeoff && .venv/bin/python scripts/mutation_check.py tasks:
cd bakeoff && .venv/bin/python scripts/mutation_check.py grader:
```

Expected: both new entries CAUGHT. `STALE ANCHOR` means the literal moved — re-read the source and copy the line again, indentation included. `NO TESTS` means the `-k` selector stopped matching. Both are hard failures. **Do not run this concurrently with anything**: it edits sources in place.

- [ ] **Step 3: Write the integration test**

Create `bakeoff/tests/test_integration_submodules.py`. This is the only place the two halves are exercised together — Tasks 2 and 3 each test their own half against a scratch repository, and neither can see a disagreement between the image and the run tree, which is the failure mode `build_task_image`'s docstring is about.

```python
"""Materialize and build from ONE superproject, and compare the two.

Every other test in this plan checks one half. The failure this one exists
for is a DISAGREEMENT: the image's `pip install -e .` resolves against the
build context's copy of the submodule, the agent's suite runs against the run
tree's, and nothing downstream compares them. Same class as a non-editable
install -- the image and the run disagree silently, and preflight's
green-after check only catches it when the disagreement happens to break the
suite.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.task_image]


def test_the_image_and_the_run_tree_carry_the_same_submodule_blob(
        tmp_path, monkeypatch):
    # 1. build a synthetic superproject + submodule (reuse test_tasks.py's
    #    `upstream_submodule` shape; import the helper or copy it -- the two
    #    files do not share a conftest today)
    # 2. materialize(task, tmp_path / "run", cache)
    # 3. build_base_image(REPO_ROOT, <the version image.python names>)
    #    then build_task_image(task, base, tmp_path / "build", cache)
    # 4. preflight(task, image=..., repo_path=tmp_path / "run",
    #              start_sha=...)
    #
    # Assertions:
    #   result.ok, with result.problems in the failure message
    #   result.evidence["submodules"] == [{"path": "vendor/libdep",
    #                                      "sha": <gitlink>,
    #                                      "initialised": True}]
    #   the submodule file's bytes in `tmp_path / "run"` equal its bytes in
    #     `tmp_path / "build" / f"image-{task.task_id}" / "repo"`
    #   `git -C <run>/vendor/libdep cat-file -e <the future sub commit>`
    #     exits non-zero -- the prune holds in the artifact the agent gets,
    #     not only in the cache
```

Fill the four numbered steps from `tests/test_integration_grader.py`'s existing setup, which already builds a base image and a task image and is the closest working model — including the `--basetemp` requirement below. `image.python` must be read from the manifest rather than hard-coded: broadening 5 made the base per version.

The synthetic superproject's suite must be trivially green and must import from the submodule, so that step 4's preflight would fail if the submodule were empty — an assertion on `result.ok` that could pass with an unpopulated submodule would prove nothing.

- [ ] **Step 4: Run it**

```
cd bakeoff && .venv/bin/python -m pytest tests/test_integration_submodules.py -v \
  -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
```

`--basetemp` under `$HOME` is **mandatory**, not hygiene: the Docker VM mounts `$HOME` and not `/var/folders`, and a repo bind-mounted from there appears inside the container as a **silently empty directory** — so the comparison would be between two nothings and would pass. This measurement hit exactly that (M13's first attempt), which is why the assertion in step 3 includes a positive check that the file is there at all.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/scripts/mutation_check.py bakeoff/tests/test_integration_submodules.py
git commit -m "test: pin the pruned-mirror choice and the gitlink refusal against reversal

Both are single lines whose reversal leaves every other test green: the run
tree still materializes and the gate still passes with the unpruned mirror,
and a gitlink submission still applies cleanly without the refusal -- it just
grades the wrong content. The integration leg is the only place the image and
the run tree are compared, which is where a silent disagreement between them
would otherwise live.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 7: the docs, and the corpus rule this broadening replaces

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md` (the "No git submodules" bullet in "The image"; the Excluded table)
- Modify: `docs/BUILDING-A-TASK-SET.md` (the screening table's submodule row)
- Modify: `tasks/todo.md` (a review section)
- Modify: `TASKS.md` (two follow-ups)

- [ ] **Step 1: Rewrite HARVESTING's submodule bullet**

Replace the `**No git submodules.**` bullet under "### The image" with a **Submodules are supported, with three limits** bullet saying:

- The path, url and pinned commit are derived from `base_sha` — nothing goes in the manifest. Two git readers must agree (`git ls-tree` for the gitlink, `.gitmodules` for the url), and a task where they do not is refused at load.
- The url must be `https://`. Relative (`../x.git`), `ssh://`, `git@…` and `file://` are refused; a repository whose `.gitmodules` uses a relative url is currently out, and that is a deferral rather than a judgement (`TASKS.md`).
- Nested submodules are refused.
- **A task whose fix touches submodule content is out.** Measured 2026-09-01: `git add -A` stages nothing for an uncommitted edit inside a submodule, so a submission diff is zero bytes for it; an agent that commits inside the submodule produces a gitlink diff that applies green and grades the original content. The loader refuses a reference diff touching a submodule path, and the grader refuses such a submission as not-graded.
- **A suite that writes inside the submodule is out.** An untracked file there makes the superproject's `git status --porcelain` report ` M <path>`, which is preflight's clean-tree NO-GO, and `gitignore_extra` cannot fix it — that key writes the superproject's `.gitignore`, and the rule would have to live inside the submodule's own tree.
- **A suite, a conftest or an `image.build` step that runs `git submodule update` itself is out.** The submodule is already populated before the container starts, the container has no route off the host, and the `.gitmodules` url the run tree carries is the truthful upstream https one — so the command fails, and it fails inside a suite whose exit code the gate reads as the task's own.
- **An orphaned `.gitmodules` stanza is fine.** A `path` naming no gitlink is inert (never listed, never fetched, no directory created); preflight records it as `submodules_orphaned` and nothing refuses it.

Also add, to the paragraph about the run tree being pruned to `base_sha`: the submodule's history is pruned the same way, to its gitlink, so `git -C <path> log --all` in the run tree reaches nothing after the pin.

- [ ] **Step 2: Move `tomlkit` out of Excluded**

Delete the `python-poetry/tomlkit` row from the Excluded table and add it wherever the usable repositories are listed, with the measurement:

> `python-poetry/tomlkit` — one submodule, `tests/toml-test` at `https://github.com/BurntSushi/toml-test.git`, non-nested. Measured 2026-09-01 at `HEAD` via a blobless shallow clone; re-check at the chosen `base_sha`, which the derivation does anyway. Its other screening criteria are unmeasured — being unblocked on submodules is not the same as being usable.

That last sentence matters: broadening 3's review log records the same care for `attrs`/`cattrs`, and overstating it is how a repository gets cut and then abandoned.

- [ ] **Step 3: Update `docs/BUILDING-A-TASK-SET.md`**

Replace the screening table's `needs a git submodule` row's verdict — currently **excluded** — with:

> usable. The submodule is derived from `base_sha` and populated from its own pruned mirror; check three things before cutting: the `.gitmodules` url is `https://`, the submodule has no submodules of its own, and the suite does not write inside it (an untracked file there shows as ` M <path>` in the superproject and is a preflight NO-GO that `gitignore_extra` cannot fix)

- [ ] **Step 4: Add the review section to `tasks/todo.md`**

One section, in the file's existing voice, covering: what was built, the measurements that drove each decision (M3 the future-commit leak, M4 the transient-`-c` uninitialised state, M5 the file-transport refusal, M8 the zero-byte submission diff, M11 the green apply, M13 the container probe), what was deliberately **not** built (no manifest key, no `RunRecord` field, no `SCHEMA_VERSION`/`ORACLE_VERSION` bump, no `container.py` change, no relative-url resolution, no `--recursive`), and the version moves (`PREFLIGHT_VERSION` +1, `GRADER_VERSION` +1, `GRADE_SCHEMA_VERSION` minor +1, `click-3360`'s `start_sha` unmoved at `33575cc0…`). End with the suite counts.

- [ ] **Step 5: Add the follow-ups to `TASKS.md`**

Two entries, both P3, both explicitly deferrals rather than defects:

1. **Relative `.gitmodules` urls.** git resolves `../toml-test.git` against the superproject's own remote. The derivation could resolve it against `task.repo_url` instead, which is well-defined and offline — but the resolution rules (`./`, `../` chains, a trailing `.git`, a `repo_url` with or without a trailing slash) need measuring before they are written, and the refusal is correct in the meantime. The likeliest thing to block a real repository.
2. **Nested submodules.** `submodule update --init --recursive` plus one pruned mirror per `(inner url, inner gitlink)`, and preflight's `git submodule status --recursive`. Refused today because the untested path leaves the inner directory empty, which reads as clean.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/taskset/HARVESTING.md docs/BUILDING-A-TASK-SET.md tasks/todo.md TASKS.md
git commit -m "docs: submodules are in, with the four limits that come with them

The corpus rule flips from 'no git submodules' to a rule with edges: derived
from base_sha, https urls only, no nesting, and -- the two that are about
what the harness can SEE rather than what it can fetch -- no fix that touches
submodule content, because a submission diff is zero bytes for it, and no
suite that writes inside one, because that dirties the superproject and
gitignore_extra cannot reach it. tomlkit leaves the Excluded table on a
measured .gitmodules rather than on the assumption that it now works.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

Run against `.superpowers/broaden/CONTEXT.md`'s description of broadening #6 and against the global constraints.

**Review round 1 (`.superpowers/broaden/b6-plan-review-1.md`), item by item.** Three Critical accepted and fixed: M6 was **wrong** — the host cache path survives in `logs/HEAD` and `logs/refs/heads/main`, the expire clears it, and the first draft's config-only test could not see the leak (M6 rewritten, the expire is now `check=True` and documented as a leak guard, the test rglobs the whole `.git/modules` subtree); preflight's `git submodule status` now branches on `exit_code` and records `None` rather than a manufactured `[]` (D7); the url-without-gitlink refusal was based on a claim never measured and is **dropped** — M16 records it inert, the cross-check is one-directional, and orphans go to `evidence["submodules_orphaned"]`. Four Important accepted: the early return sets both keys as `None` (Task 4 Step 4); two mutation entries and the integration leg are Task 6; D8/D10 now say plainly that the uncommitted-edit → `EMPTY_PATCH` case is **uncontained**. Six Minor accepted: D3/Task 2 ordering reconciled to the end-of-`materialize` placement; the status line is parsed for its first character only, with paths from `git ls-files -s -z`; the redundant `p == sub.path` conjunct is gone; `materialize` derives from the pruned mirror it already holds; broadening 5's `_Image.python` is called out (with an instruction to read the real stub rather than copy this document's); HARVESTING gains the `git submodule update`-in-the-suite rule. Nothing was rejected.

**Review round 2 (`.superpowers/broaden/b6-plan-review-2.md`).** All five accepted, none rejected. `_gitlink_paths` now uses `container.exec` and branches on `exit_code`, returning `None` for "not read" — `_checked_exec` was wrong on both type (`ExecResult`, not `str`) and contract (it raises, and `run_matrix`'s `preflight(...)` is unwrapped, so the gate must collect rather than raise); the orphan probe distinguishes `git config --get-regexp`'s exit 1 ("nothing matched", ordinary) from `>1` (error → `None` + a problem); `_parse_submodule_status` returns `(parsed, unmatched)` and an unmatched line becomes a problem instead of a `tail.strip()` fallback that would put a display-derived path back into the evidence; the M15 and File Structure "Task 6" references and Task 1's Interfaces line are corrected; Task 4 Step 2's selector is `-k "submodule or status_line"` with an instruction to check the collected count.

**Coverage.** CONTEXT asks for four things and each has a task. "Build context becomes a materialized tree at `base_sha` … with submodules initialised at build time" — Task 3 does the equivalent with **two `git archive`s** instead, which is strictly better on CONTEXT's own stated criterion: it keeps the oracle out without needing an argument about which files a copied tree carries (D5, M12). "The run tree must also carry the submodule contents" — Task 2, from the mirror, no network at materialize time. "The pruned-mirror invariant must hold, and submodule objects live in a different repository so think about where they come from" — D3 and M3: the superproject invariant is untouched (M2: the objects are not in its store), and the same invariant is asserted one level down by reusing `ensure_pruned_mirror` unchanged, which is what `HANDOFF.md` asks for. "Reopens `tomlkit`" — Task 7, on a measured `.gitmodules` (M15), with the "unblocked is not usable" caveat.

**Two things CONTEXT did not ask for, and why they are here.** The grader refusal (Task 5) is not in CONTEXT's sketch, but M11 measured a *false verdict* — a green apply that grades the wrong content and stamps `resolved: False`. Leaving it would put an accusation-producing defect into the pipeline the same commit that enables the feature causing it. The reference-diff refusal (D2's fifth) is the load-time half of the same problem. Both are scoped to submodule tasks only.

**Placeholder scan.** Every `...` from the first draft of Tasks 3, 4 and 5 has been replaced with executable test bodies. Task 6's integration test is the one exception and is deliberate: its four setup steps are a numbered comment block pointing at `tests/test_integration_grader.py`, because that file's base-image and task-image setup is ~40 lines that broadenings 4 and 5 both touch, and transcribing it here would ship a stale copy. Two things are still described rather than written, deliberately, and both are edits to prose files: HARVESTING's rewritten bullet (Task 7 Step 1, which lists every clause it must contain) and the `tasks/todo.md` review section (Task 7 Step 4, which lists every item it must cover). Writing those verbatim here would put the same paragraphs in two files.

**Type consistency.** `Submodule(name, path, url, sha)` is used with those four field names in Tasks 1, 2, 3 and 5. `derive_submodules(task, mirror)`, `task_submodules(task, cache_root)`, `_refuse_submodule_conflicts(task, subs, mirrors)` and `_init_submodules(task, dest, subs, cache_root)` each appear with one signature throughout — the `allow_non_https` keyword the first draft carried is gone in favour of the `_SUBMODULE_URL_PREFIX` constant, so there is one relaxation mechanism and no production signature carries a test-only flag. `_parse_submodule_status` returns `list[dict]` with keys `path`/`sha`/`initialised` in both its implementation and its test.

**Three risks worth naming for the implementer.**

1. **Task 2's nested-submodule test is the fiddliest thing in this plan.** It re-pins the outer submodule at a commit that has its own submodule, which means fetching into the fixture's already-added submodule. If it fights back, build the nested shape from scratch in a separate fixture rather than mutating `upstream_submodule` — the assertion is one `pytest.raises`, and how the fixture reaches that state is not load-bearing.
2. **`_git` is `check=True` by default** and `_init_submodules` relies on that for the update. Do not add `check=False` "to give a better message": M5 measured exit 1 on both failure modes with git's own text naming the url and the path, and `_git`'s wrapper already prefixes the command.
3. **Do not run `scripts/mutation_check.py` concurrently with anything.** It edits `src/bakeoff/tasks.py` in place, which is the file Tasks 1 and 2 both change, and Task 6 adds two entries to it.
4. **Read the real stand-ins before copying this document's.** `_Image`, `_Task`, `_FakeTask` and `_FakeContainer` in `test_images.py`, `test_preflight.py` and `test_grader.py` all gain fields in broadenings 4 and 5, which land first. This plan's copies are illustrative; the files are authoritative.

**Gates before calling this done**, in this order and none of them concurrent:

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/verify_logger.py
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

`--force-preflight` is not optional: the cache keys on `manifest_digest|image|start_sha|PREFLIGHT_VERSION`, and although Task 4 moves the last of those, the earlier tasks do not — a gate run between them would print `preflight cached PASS` and check nothing. The preflight run must report `start_sha` still `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

`CLAUDE.md` is not edited on this branch (a separate uncommitted edit to it exists on `main`). These belong in it, in the **Invariants** section, appended to the "The run tree holds no object outside `base_sha`'s history" bullet or as a bullet of their own:

> **A submodule's history is pruned to its gitlink, and the superproject's prune says nothing about it.** The gitlink is one `160000` entry in `base_sha`'s tree, so the submodule's objects are in a *different repository* and `_verify_pruned`'s `rev-list base_sha` sweep never sees them — measured, `git cat-file -e <gitlink sha>` fails in the superproject store. But `git submodule update --init` against the declared url clones the submodule's **whole** history, `remotes/origin/main` included, so the run tree would carry submodule content newer than the pin and `git -C <path> log --all` would hand it to the agent, differentially. `ensure_pruned_mirror` is therefore called a second time per `(submodule url, gitlink sha)` — unchanged, because it is already generic over its two keys — and `materialize` initialises from *that*, with `protocol.file.allow=always` (git has refused the file transport for submodules since CVE-2022-39253, and a local mirror is a file transport). The url is written to `.git/config` **before** the update and rewritten to the `.gitmodules` value after: a transient `-c submodule.<name>.url=` populates the tree but leaves `git submodule status` reading `-<sha>`, i.e. *uninitialised*, which is the character preflight gates on. The rewrite is not enough on its own — measured, the cache path also survives in the submodule's `logs/HEAD` **and** `logs/refs/heads/main` as `clone: from …`, and only `reflog expire --expire=now --all` removes it, so that call is a leak guard rather than tidiness.

> **A submission diff is blind inside a submodule, and the blindness is green rather than loud.** Measured: `git add -A` stages **nothing** for an uncommitted edit inside a submodule, so `snapshot_diff` returns zero bytes for it — not even the `-dirty` gitlink line. An agent that *commits* inside the submodule does move the gitlink, and `git apply --index` of that diff on a fresh tree exits **0** (`warning: unable to rmdir` only), moves the index to a commit that exists nowhere outside the run tree that produced it, and leaves the submodule's working tree unchanged — so the ladder grades the original content and returns `resolved: False`, an accusation for work the harness could not capture. `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE` takes that row out of the denominator, and `HARVESTING.md` refuses a task whose fix touches submodule content in the first place.

> **An empty submodule directory reads as a clean tree.** `git status --porcelain` is byte-identical between a healthy run tree and one whose submodule has zero entries, so a failed initialisation would surface only as a suite that cannot collect — on every arm, scored as capability. `git submodule status`'s leading character is the one signal (` ` initialised at the gitlink, `-` empty or unregistered, `+` the wrong commit), and preflight gates on it — reading its **exit code**, because a failed status returns empty stdout that parses to "there are none". A `.gitmodules` stanza naming no gitlink is the one shape that is genuinely inert (never listed, never fetched, no directory), so it is recorded rather than refused. The converse also bites: an **untracked** file inside the submodule makes the superproject report ` M <path>`, which is preflight's clean-tree NO-GO, and `gitignore_extra` cannot fix it because that key writes the superproject's `.gitignore`.

And one line for the **Config gotchas** section:

> **`git archive` emits a submodule as an empty directory, `.gitmodules` and all.** So the image build context needs a second `git archive <gitlink sha>` extracted into that directory, or `pip install -e .` resolves against a tree the run tree will have content in — the same silent image/run disagreement a non-editable install produces. Two archives, never a copy of a materialized run tree: that tree is `base_sha` **plus the committed test half**, and copying it bakes the oracle into a layer nothing downstream can tell from a dependency.
