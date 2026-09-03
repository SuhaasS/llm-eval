# Round 2, item 2: a manifest lever that declares a submodule UNNEEDED

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision:** reviews 1 and 2
(`.superpowers/broaden/round2/plan-2-review-1.md` — REVISE, 17 findings, then
REVISE, 3 mechanical findings) and a **cross-item blocker** raised by round-2
item 17's reviewer (`plan-17-review-1.md`, finding 1) are folded in. The
cross-item finding invalidated half of MU3 and required a new measurement
(MU9), a new design section (D10) and a new task (Task 5): without them, every
run of every task declaring this key would have been refused by the offline
grader. Review 3 (5 findings) then established that `SCHEMA_VERSION` must move
with it. See *Reviews → changes* at the end for the finding-by-finding map of
all four rounds.

**Goal:** Let a task be cut from a repository whose `base_sha` carries a
submodule the task's suite never reads — an ssh-url one included — by
declaring that submodule unneeded in the manifest. The gitlink stays in the
index and the tree exactly as at `base_sha`, the directory is never
populated, its url scheme is never checked, and no mirror is built for it.

**Architecture:** One top-level manifest key, `submodules_unneeded`, a list of
paths. `tasks.Submodule` gains `declared_unneeded: bool`. Four refusals — two
in `derive_submodules`, two in `_refuse_submodule_conflicts` — become
conditional on it; two others deliberately do not. `_init_submodules` and
`images._extract_submodules` skip a declared path entirely, so
`ensure_pruned_mirror` is never called for it and no network url is ever
contacted. `preflight` stops treating an uninitialised gitlink at a declared
path as a NO-GO and instead asserts the state the declaration promises: the
directory exists, it is EMPTY, and `git submodule status`'s marker is `-`. The
suite then runs five times against that tree, and its red-before /
green-after checks establish that **the declared f2p ids and the p2p sweep
pass with the directory empty** — which is what the gate can show, and is
narrower than "the submodule is unneeded" (D9). `grader.py` is unchanged —
but `container.snapshot_diff` is **not**: it seeds its scratch index from
`base_sha` before staging, without which an uninitialised gitlink is a phantom
`deleted file mode 160000` in every submission and every checkpoint, and the
grader refuses the whole task (D10, MU9). `start_sha` does not move.

**Tech Stack:** Python 3.12 (the harness venv), pytest, git 2.50.1 (Apple
Git-155) on the host, Docker for the integration and gate legs. All code under
`bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3
the self-correction loop, §3.7 the manifest, §5.1 the image, §5.6 the
submission diff, §6.4 confounds). The submodule subsystem this extends:
`docs/superpowers/plans/2026-09-01-broaden-6-submodules.md` — read D1, D2, D3,
D4 and D7 of it before Task 1; every decision below is stated as a *narrowing*
of one of them. Round-2 process: `.superpowers/broaden/round2/CONTEXT.md`.
`HANDOFF.md` is **not** required reading for this item, and that is a
finding rather than an omission: no new `ensure_pruned_mirror` caller is
added, and one is removed for the declared path.

---

## The measured defect

`~/.cache/bakeoff-probe/reports/w1-sqlglot-8225.md` (2026-09-02, worker probe
of shape #1). `sqlglot-8225-mysql-key-constraint` at `base_sha`
`05eed63b281f7ac020045e2b792beef8fad8d3ee` never reached preflight. Verbatim,
from that report's "Decisive output":

```
bakeoff.tasks.TaskError: sqlglot-8225-mysql-key-constraint: submodule sqlglot-integration-tests declares url 'git@github.com:fivetran/sqlglot-integration-tests.git'; only https:// urls can be fetched by this eval. A relative url resolves against a remote the run tree does not have, and ssh/file urls cannot be fetched at all.
```

The raise is at `tasks._refuse_submodule_conflicts`, reached from
`images.build_task_image` → `_extract_submodules` → `task_submodules` →
`derive_submodules`, before any container starts. Exit 1, 111 s, no task
image, no preflight cache entry.

`tobymao/sqlglot` added that `.gitmodules` in `3a930dad6` ("Chore: add
integration test automations", PR #7167, merged 2026-02-27) and it is still
present at HEAD, so **every** `base_sha` at or after 2026-02-27 is refused,
whatever the fix touches. `HARVESTING.md` (the "Measured 2026-09-02" paragraph
in the sqlglot section) records the consequence: the richest single source in
the screened corpus is closed to any task cut after that date, and
`strip_paths` cannot lift the floor because a strip covering a submodule path
is itself refused.

The `TASKS.md` entry is at line 484, titled *"The sqlglot ssh-submodule
refusal blocks a suite that never reads the submodule, and there is no
manifest lever to say so."*

---

## Measurements

Taken 2026-09-02 on this machine, **git 2.50.1 (Apple Git-155)**. MU1–MU3 use
a scratch superproject under `~/.cache/bakeoff-probe/scratch-unneeded`
(`calc.py`, `tests/test_x.py`, `.gitmodules`, gitlink at `vendor/libdep`);
MU4–MU7 read `~/.cache/bakeoff-probe/clones/sqlglot` directly. MU1–MU3 were
independently re-taken by review 1 against its own scratch repo
(`~/.cache/bakeoff-probe/scratch-review-2`) and all three hold; MU8 is the
review's own addition. Reproduce under the scratchpad.

**MU1 — an uninitialised gitlink directory survives `materialize`'s `git clean
-xfd`, and every git reader still names it.** After `git clone --local
--no-checkout <mirror> run && git -C run checkout --detach <base_sha>`, then
`git clean -xfd`:

```
directory exists:        YES
ls -A vendor/libdep:     0 entries
git status --porcelain:  (empty)
git submodule status:    -2da195c879b6a550943b179a90c1b92ae859d4e7 vendor/libdep
git ls-files -s:         160000 2da195c879b6a550943b179a90c1b92ae859d4e7 0	vendor/libdep
```

So the shape this plan asks for is the shape the run tree *already* has when
`_init_submodules` does nothing: the directory is present and empty, the index
gitlink is untouched, and preflight's two existing readers (`git ls-files -s
-z` and `git submodule status`) both report it. Nothing new has to be built to
produce the state; what has to change is the verdict rendered over it. The
`git clean -xfd` this depends on is `materialize`'s, between the `start_sha`
read and the `declared_start_sha` comparison.

**MU2 — `git archive base_sha` puts the empty directory in the build context,
so `images.py` needs no work beyond skipping.** `git archive --format=tar
<base_sha> | tar -x -C ctx` from the bare mirror:

```
drwxrwxr-x 0 root root 0 vendor/
drwxrwxr-x 0 root root 0 vendor/libdep/
...
ctx/vendor/libdep present: YES; entries=0
```

`tar` creates the directory entry, empty. This confirms
`2026-09-01-broaden-6-submodules.md`'s M1 through the extraction step, which
M1 stopped short of.

**MU3 — content written inside an UNINITIALISED submodule directory is
invisible to every git reader the harness uses. This is the sharpest finding
here.** With `vendor/libdep/junk.txt` present in the tree of MU1 (review 1
re-measured with a nested `vendor/libdep/deep/y.txt` as well, same answers):

```
git status --porcelain              -> (empty)
git status --porcelain -uall        -> (empty)
git ls-files -o --exclude-standard  -> (empty)
git submodule status                -> -<sha> vendor/libdep   (unchanged)

# against the REPOSITORY'S OWN index -- see MU9, this is NOT what the
# harness runs:
git add -A ; git diff --cached <base_sha>            -> 0 BYTES
git add -A ; git diff --cached --name-only <base_sha> -> (empty)

find vendor/libdep -mindepth 1      -> vendor/libdep/junk.txt
ls -A vendor/libdep                 -> junk.txt
```

git never descends into a gitlink path, initialised or not, so preflight's
clean-tree check — `git status --porcelain` — is *blind* here. HARVESTING's
existing rule "a suite that writes inside the submodule is out" therefore has
**no enforcement at all** once the submodule is left uninitialised: that
rule's current enforcement (M9 of broadening 6: an untracked file inside an
*initialised* submodule shows as ` M <path>`) does not carry over. A
filesystem read is the only way to see it, which is why D6 adds one — and why
Task 6 corrects the HARVESTING bullet that claims the `git status`
enforcement covers this case.

**The `git add -A` lines above are measured against the repository's own
index, and that is NOT the command the harness runs.** The first draft of this
plan drew a second conclusion from them — that content inside the directory
"can never reach a checkpoint diff or the §5.6 submission" — and that
conclusion was **false on the harness's actual path**. MU9 is the correct
measurement, it is a blocker for this whole item, and D10 is the fix.

**MU9 — `container.snapshot_diff` stages into a SCRATCH index, and against
that index an uninitialised gitlink is a PHANTOM DELETION on a clean tree.**
*(Surfaced by round-2 item 17's reviewer against this plan's MU3; re-measured
here with the harness's own command sequence.)* `snapshot_diff` runs
`GIT_INDEX_FILE=/tmp/bakeoff-snapshot-index git add -A` and then `git diff
--cached <base_sha>` in the same env — a scratch index, never `.git/index`,
because it runs concurrently with the agent. A scratch index starts **empty**,
so `git add -A` populates it from the worktree alone, and `git add -A` does
not descend into a gitlink path: no entry is created for an uninitialised
submodule, and the diff against `base_sha` — which *does* carry the `160000`
entry — reports it as deleted.

Measured on the `vendor/libdep` fixture, clean tree, agent did nothing:

| tree state | index | `git diff --cached <base_sha>` |
|---|---|---|
| clean, gitlink UNINITIALISED | scratch (**the harness**) | **199 bytes** — `deleted file mode 160000` + `-Subproject commit a77f2e0…` |
| clean, gitlink UNINITIALISED | repository's own | 0 bytes (what MU3 reports) |
| agent wrote `vendor/libdep/agent_wrote_this.py` | scratch | **326 bytes** — the deletion **plus** `new file mode 100644` for the agent's file |
| clean, gitlink **POPULATED** (the tomlkit shape) | scratch | **0 bytes** — `git add -A` re-stages it as `160000` (with git's "adding embedded git repository" warning) |
| real agent edit (modify + add + delete), gitlink uninitialised | scratch | 592 bytes, names `calc.py new.py tests/test_x.py` **and `vendor/libdep`** |
| **G′** agent wrote into the empty dir, **after** D10's seed | scratch, seeded | **0 bytes**, names `[]` — while `ls -A` sees the file. See D10's last property. |

Stable across repeated calls against the persisted scratch index (199 bytes on
calls 1, 2 and 3), so it is not a first-call artifact.

**On the real verification vehicle.** `tobymao/sqlglot` at
`05eed63b281f7ac020045e2b792beef8fad8d3ee`, gitlink
`sqlglot-integration-tests` uninitialised, clean tree, three calls: **239
bytes each**, a `deleted file mode 160000` chunk. `grader._GITLINK_MODE`
matches `^deleted file mode 160000$`, so `_chunk_is_gitlink` returns True and
`_gitlinks_touched` refuses the submission. **Without D10, every run of every
task declaring this key — including a clean one where the agent did nothing —
grades `SUBMODULE_GITLINK_UNGRADABLE` and leaves the denominator silently.**
The last row of the table above is why nothing is wrong today: the only
submodule task in the corpus (`tomlkit-514`) is *populated*, and a populated
gitlink is re-staged.

**A second, PRE-EXISTING phantom the same empty index produces.** A file that
is tracked at `base_sha` and also matches a `.gitignore` pattern is skipped by
`git add -A` (from an empty index every path is untracked, and `add -A`
honours the ignore rules), so it is absent from the scratch index and the diff
reports it **deleted**. Measured: with `.gitignore` naming `calc.py`, today's
path gives 460 bytes and names `.gitignore calc.py vendor/libdep`; the D10
path gives 135 bytes and names `.gitignore` alone. That is the `gitignore_extra`
shape and it is not this item's doing — it is fixed by the same one line and
is pinned so nobody reverts it.

**MU4 — the sqlglot submodule at the target `base_sha`, verbatim.**

```
git show 05eed63b281f7ac020045e2b792beef8fad8d3ee:.gitmodules
[submodule "sqlglot-integration-tests"]
	path = sqlglot-integration-tests
	url = git@github.com:fivetran/sqlglot-integration-tests.git

git ls-tree 05eed63b281f7ac020045e2b792beef8fad8d3ee | grep 160000
160000 commit 4d539e4369b07cba70d8249924136d0826cf7ea5	sqlglot-integration-tests
```

Name, path and url are all `sqlglot-integration-tests` / the ssh url; the
gitlink is at the repository ROOT, not under `tests/`.

**MU5 — sqlglot's unit suite reads the submodule through two `os.path.isdir`
guards, and degrades cleanly when it is empty.** `git grep
sqlglot-integration-tests <base_sha> -- tests/` finds exactly two files, both
verbatim below:

```python
# tests/sqlglot/__init__.py
_integration_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "sqlglot-integration-tests", "tests", "sqlglot"
)
if os.path.isdir(_integration_dir):
    __path__.append(os.path.normpath(_integration_dir))

# tests/test_integration_loader.py
def load_tests(loader, suite, pattern):
    if os.path.isdir(INTEGRATION_TEST_DIR):
        suite.addTests(loader.discover(INTEGRATION_TEST_DIR, pattern="test*.py"))
    return suite
```

Both guard on the INNER path `sqlglot-integration-tests/tests/sqlglot`, which
does not exist when the submodule directory is empty. With it empty neither
appends anything and there is no collection error — the suite is simply the
non-integration set. The other references (`Makefile`, lines 28, 93, 104, 121,
135, 138) are `make` targets and are not the task's `tests.runner`.

This is the shape the key is for, and it is worth naming: a repository that
carries an *optional* submodule guards on its presence, precisely because
contributors clone without `--recursive`. **It is also the shape that bounds
what the gate can prove**: the suite silently *shrinks* rather than failing,
and nothing in the evidence records a collected-test count, so the gate cannot
tell "guards and degrades cleanly" from "collects fewer tests". D9 states the
claim at that width and Task 6 writes it into the docs at that width.

**MU6 — at that `base_sha`, `CLAUDE.md` is a symlink.**

```
git ls-tree 05eed63b281f7ac020045e2b792beef8fad8d3ee -- CLAUDE.md AGENTS.md
100644 blob 1e21f01f7d58fa7e4059a0111a676b872c4251bc	AGENTS.md
120000 blob 47dc3e3d863cfb5727b87d785d09abf9743c0a72	CLAUDE.md
git cat-file -p 05eed63b…:CLAUDE.md  ->  AGENTS.md
```

So the verification manifest's `strip_paths` must carry **both** names, per
`HARVESTING.md`'s symlink rule. Not part of this plan's code; part of Task 7's
manifest.

**MU7 — the PR the verification task is cut from.** `91119bcaac977ede6f4a641bdda593b0015ef998`,
"fix(mysql): support KEY in CREATE TABLE column definition (#8230)",
2026-08-21; `git rev-parse <merge>^1` is `05eed63b281f7ac020045e2b792beef8fad8d3ee`.
Two files, +20/-0:

```
 sqlglot/parsers/mysql.py     |  6 ++++++
 tests/dialects/test_mysql.py | 14 ++++++++++++++
```

The test half adds one method, `TestMySQL.test_column_key_constraint`.
Neither file is at or under `sqlglot-integration-tests`, so the reference-diff
refusal passes with the key declared — which is exactly the point D4 makes.

**MU8 — a tree with gitlinks and no `.gitmodules` blob exits 128, not 1, and
that is a SIXTH refusal.** *(Review 1's measurement; it is the blocker finding
and the reason D4's table has six rows rather than five.)* After `git rm
--cached .gitmodules` and a commit, with the gitlink still in the tree:

```
git config --blob <sha>:.gitmodules --list -z
  -> exit 128, "error: unable to resolve config blob"
```

`derive_submodules` raises on that combination (`listing.returncode != 0 and
gitlinks`) **before** `by_path` is built, so it is reached ahead of the
`unfetchable` refusal and is a distinct one. The first draft of this plan
neither counted it nor exempted it, and specified a test that could not pass
under it.

---

## Design decisions, settled

### D1. The key is a top-level `submodules_unneeded`, a list of paths

```yaml
submodules_unneeded: ["sqlglot-integration-tests"]
```

**Top-level, beside `strip_paths` and `gitignore_extra`**, and the argument is
the one already written into `click-3360-write-usage-empty-args/task.yaml`'s
`strip_paths` comment: *"Top-level, beside gitignore_extra, because like
gitignore_extra it is a modification the harness makes rather than a statement
about upstream — `repo:` holds only what can be checked against GitHub."*
`submodules_unneeded` is that same kind of thing twice over. It changes what
the harness *does* (it declines to populate a path), and its truth is a
property of **this task's suite**, not of the repository: the same `base_sha`
of `tobymao/sqlglot` would be a different answer for a task whose f2p id lived
under `sqlglot-integration-tests/`. Nothing in it can be checked against
GitHub, which is `repo:`'s admission criterion.

**A flat list of paths, not a `submodules:` block.** Rejected because a block
invites sub-keys, and every sub-key anyone would reach for — a url override, a
sha pin, a per-submodule mirror source — is configuration restating what
`base_sha` already pins, which is the whole of broadening 6's D1 ("No manifest
key. The submodule set is DERIVED from `base_sha`") and of this codebase's
"Configuration is never reported as observation" invariant. A flat list can
only say the one thing it is allowed to say. This key does not *declare a
submodule*; it declares a **fact about the task** over a submodule the tree
already declares.

**Paths, not `[submodule "NAME"]` names.** `Submodule.name` need not equal
`Submodule.path` — git does not require it — and only `path` comes out of `git
ls-tree`, which is the authoritative reader. A name-keyed list would have to
be joined through `.gitmodules`, which is exactly the file a declared-unneeded
submodule is allowed not to have (MU8, D4).

**`submodules_unneeded`, not `skip_submodules`.** The key names the CLAIM the
gate checks ("this task does not need it"), not the mechanism ("skip it").
The mechanism is an implementation detail that would read as a lie if the
harness later chose to populate the path read-only from a pruned mirror while
the claim stayed true; the claim is also what the §6.4 note (D9) and the
preflight assertions (D6) are both about. `submodules_ignored` was rejected as
ambiguous against `.gitignore`/`gitignore_extra`.

**Not a boolean.** `submodules_unneeded: true` was considered and rejected on
two counts: a repository can carry two submodules of which one is needed
(`eemeli/yaml` carries four), and a boolean cannot be checked against the tree,
so a `base_sha` bumped to one that gains a *second* submodule would silently
stop populating it — the class of silent-wrong-state failure every check in
`tasks.py` is shaped to refuse.

**Not `strip_paths`.** Already refused, and the refusal stays (D4). A strip
DELETES the gitlink, moves `start_sha`, and leaves `.gitmodules` naming a path
that no longer exists. This key does the opposite of a strip: it changes
nothing in the index or the tree.

Loader plumbing: `TaskManifest.submodules_unneeded: tuple[str, ...] = ()`,
parsed with the existing `_strs(data.get("submodules_unneeded"), …)` beside
`strip_paths` and `gitignore_extra`, and shape-validated by a new
`_validate_unneeded_submodules` (D3). `manifest_digest` needs no term — it
hashes the raw manifest bytes, so declaring or editing the key already
invalidates the task's preflight and oracle cache entries.

### D2. `Submodule` carries `declared_unneeded`, and the derivation still returns the entry

`tasks.Submodule` gains `declared_unneeded: bool = False`. The default keeps
every existing construction site — including the tests' — valid unchanged.

The declared entry stays **in** the tuple `derive_submodules` returns rather
than being filtered out, and that is load-bearing three times:

- the two refusals that must survive (D4) read the tuple;
- `preflight` needs the path in `evidence["submodules"]` to record
  `declared_unneeded: true` beside `initialised: false`, which is the "absence
  is recorded, never implied" rule — a filtered-out submodule would render as
  a tree with no gitlink at all, which is a different tree;
- a reader of the evidence has to be able to tell "there is no submodule here"
  from "there is one and this task declined it".

### D3. The typo refusal is split: shape at load, existence at derivation

A declared path that is not a gitlink at `base_sha` must be refused — a typo
that silently declares nothing would leave a real submodule populated (harmless
but not what was asked) or, on a repository with an ssh url, leave the task
refused with a message about the url while the manifest plainly tried to opt
out. But `load_task` **cannot** run the check: it runs on the host, offline,
with no repository and no cache root (broadening 6's D1 — `test_tasks.py`
constructs manifests whose `base_sha` is `"0" * 40` against a repo that does
not exist). So the split is exactly the one `strip_paths` already uses:

- **`_validate_unneeded_submodules(paths, where)` at load**, a sibling of
  `_validate_strip_paths` and calling `_validate_prefixes` first, so it
  inherits the empty/padded/absolute/`..` refusals. Then the same three
  specific ones, for the same reasons: `.`/`./` names the whole tree, a
  `.git`-rooted path is inside the repository's own metadata, and pathspec
  magic (`_PATHSPEC_MAGIC`) would make the key's meaning a property of the
  tree rather than of the manifest. Plus one more that `strip_paths` does not
  need: a **duplicate** entry, because the same path listed twice is a
  manifest the author did not mean and the set arithmetic below would never
  say so.
- **The existence check in `derive_submodules`**, which holds the mirror and
  runs before any container, exactly where `_strip_paths_from_tree` performs
  the analogous "names nothing tracked" check.

**Its position is one line, not a range** — a correction from the first draft,
which said "after the gitlink scan and before the `unfetchable` refusal" and
so spanned MU8's sixth refusal too. It goes **immediately after the `gitlinks`
dict is built, before the `if gitlinks or _has_gitmodules(...)` block**, so
the typo message beats *both* `.gitmodules`-derived refusals. D3's whole point
is that an author who misspells the path of the submodule they are trying to
exempt is told about the typo, not about the url and not about an unreadable
`.gitmodules`; that only holds at this one position, and two tests pin it.

```python
    unknown = sorted(set(task.submodules_unneeded) - set(gitlinks))
    if unknown:
        raise TaskError(
            f"{task.task_id}: submodules_unneeded names "
            f"{', '.join(unknown)}, which the tree at {task.base_sha} has no "
            "gitlink at. A typo declares nothing: the submodule it was meant "
            "to name is still populated (or still refused for its url), and "
            "nothing downstream would say the key did not apply."
        )
```

### D4. Four refusals become conditional; TWO deliberately do not

There are **six**, not five: `derive_submodules` carries two (MU8's
unreadable-`.gitmodules` raise and the per-path `unfetchable` raise) and
`_refuse_submodule_conflicts` carries four. After this change:

| # | refusal | where | declared unneeded | why |
|---|---|---|---|---|
| 1 | gitlinks exist and `.gitmodules` cannot be read at all (exit 128, MU8) | `derive_submodules` | **skipped** | Nothing is fetched for a declared path, so no url is needed for it. A tree whose *only* gitlinks are declared is fully workable with no `.gitmodules` at all. |
| 2 | a gitlink with no `.gitmodules` stanza (`unfetchable`) | `derive_submodules` | **skipped** | Same reason. The refusal's own message says the failure is "the directories would arrive empty" — which is now the *declared and asserted* state, not a silent one. |
| 3 | url is not `https://` | `_refuse_submodule_conflicts` | **skipped** | The entire point of the item. |
| 4 | nested submodules | `_refuse_submodule_conflicts` | **skipped** | It is the one refusal that needs an artifact: `mirrors.get(sub.path)` returns `None` when no pruned mirror exists, and none is built (D5), so it already no-ops. Made explicit rather than left to that coincidence. |
| 5 | `strip_paths` covers the path, above or below | `_refuse_submodule_conflicts` | **KEPT** | Unrelated to population. `_strip_paths_from_tree` still runs `git rm -r` against the start state, still removes the gitlink, and still leaves `.gitmodules` naming a path that no longer exists — and it moves `start_sha` while doing it. Preflight's index cross-check would then report a `.gitmodules` stanza with no gitlink as an orphan, which is inert-by-measurement and would make the mistake read as a normal tree. |
| 6 | the reference diff touches the path (either half) | `_refuse_submodule_conflicts` | **KEPT** | The refusal the item's brief singles out, and it is unweakened. `git add -A` stages **nothing** for a submodule path (broadening 6's M8), and MU3 shows that is true of an uninitialised one too — including for content the agent writes into the empty directory. So a task whose fix lives there is ungradable by construction whether or not the submodule is populated, and declaring it unneeded makes that *more* true, not less. A submission that moves the gitlink is separately refused by `grader._gitlinks_touched` (D7). |

Rows 1 and 2 are exempted through **one** derived set, computed once above
both raises, so a single line carries the exemption and a single mutation can
revert it:

```python
    needed_gitlinks = set(gitlinks) - set(task.submodules_unneeded)
```

Rows 3–6: the loop body in `_refuse_submodule_conflicts` gains one guard, and
the two kept refusals stay OUTSIDE it:

```python
    for sub in subs:
        if not sub.declared_unneeded:
            <url check>
            <nested check>
        <strip_paths check>
        <reference diff check>
```

Not a `continue` at the top of the loop. That is the shape a later editor
would reach for and it would silently drop the two refusals the whole item
promises to keep — which is why Task 6 anchors the *placement* of both kept
refusals with mutations of their own, not only the guard.

### D5. `_init_submodules` and `_extract_submodules` skip the path, so no mirror is built and no url is contacted

`tasks._init_submodules` filters first and returns early if nothing is left:

```python
    needed = tuple(sub for sub in subs if not sub.declared_unneeded)
    if not needed:
        return
    mirrors = {
        sub.path: ensure_pruned_mirror(sub.url, sub.sha, cache_root)
        for sub in needed
    }
    _refuse_submodule_conflicts(task, subs, mirrors=mirrors)
    for sub in needed:
        ...
```

`_refuse_submodule_conflicts` is still called with the **full** `subs` tuple,
not with `needed`, so the two kept refusals still run at their second call
site. `mirrors` has no entry for a declared path, so the nested check skips it
there for the same reason D4 says it does.

The early `if not needed: return` matters for a task whose only submodule is
declared unneeded: it is the line that guarantees `ensure_pruned_mirror` is
never reached, which is what makes "no mirror is built for it" a property of
the code rather than of the dict comprehension being empty. Both are true;
only one survives an edit.

`images._extract_submodules` gains the mirror of that:

```python
    for sub in task_submodules(task, cache_root):
        if sub.declared_unneeded:
            continue
        ...
```

`continue`, before `ensure_pruned_mirror` and before `target.mkdir`. The empty
directory the image needs is already in the context: MU2 measures `git
archive base_sha | tar -x` creating it. Creating it a second time with
`mkdir(exist_ok=True)` would be harmless and is still not done, because a
`mkdir` in the skip path would make the image's directory an artifact of this
function rather than of `base_sha`'s tree, and the two are not the same claim
when a future `_strip_build_context` change reorders around it.

`task_submodules` itself is unchanged — the derivation runs, the refusals run,
and the caller filters. Making the derivation return a shorter tuple would put
the filter where D2 says it must not be.

### D6. Preflight asserts the declared state, and adds filesystem reads because git is blind

Today's gate collapses to: any `git submodule status` marker other than a
leading space is a NO-GO (`stale`). That is exactly right for a submodule the
harness promised to populate, and exactly wrong for one it promised not to.

**The manifest is read with `getattr`, once, above the submodule block:**

```python
        unneeded = frozenset(getattr(task, "submodules_unneeded", ()))
```

Never bare attribute access. `preflight` takes an untyped `task` and the file
states the rule at the strip check: *"`getattr`, like `_declared_grading`'s:
this function takes an untyped `task` and a manifest object predating the key
must not crash the gate."* A bare read makes the gate raise `AttributeError` —
a traceback, not a NO-GO — for any caller passing an older manifest object or
a partial stub.

Changed, all inside `preflight`'s existing submodule block (the `git submodule
status` / `git ls-files -s -z` section, after the strip check):

1. **`_parse_submodule_status` is NOT modified.** Its docstring states the
   principle in so many words — *"This is an OBSERVATION, not a restatement of
   the manifest: the expected sha is never passed in"* — and
   `declared_unneeded` is manifest data, not something git said. Both new
   fields are filled in by the **caller**, in one pass over the parsed
   entries, which is where `"empty"` has to be anyway because it needs the
   container. It also keeps that function's own pure unit tests untouched.
2. **Each `evidence["submodules"]` entry gains `"declared_unneeded": bool`**,
   for every entry, `False` included. Never a key present only when true: a
   reader that cannot see the field on the other entries cannot tell "this
   task declared none" from "this gate did not know about the key".
3. **Each entry gains `"empty": bool | None`**, measured by one
   `container.exec(["ls", "-A", "--", <path>])` per submodule. Non-zero exit →
   `None` **and** a problem, because an unlistable directory is the "not
   measured" absence and must not render as the measured claim `False`.
   Measured for **every** submodule, needed ones included, deliberately: if it
   were measured only for declared paths, `None` would carry two meanings —
   "the read failed" and "the read was not attempted" — which is the "a null
   says which kind of null it is" invariant broken in the same file that
   documents it. The cost is one exec per submodule (four at most in the
   screened corpus) against five suite runs.
4. **`stale` excludes declared paths**: `if not entry["initialised"] and not
   entry["declared_unneeded"]`. This is the one line that turns the NO-GO into
   a GO.
5. **Three new problems, replacing what `stale` used to say about these
   paths:**
   - a declared path whose marker is **not** `-` → something populated a path
     the harness was told to leave alone. The message names the run tree, not
     the image: preflight bind-mounts the materialized tree at `/repo`, so the
     image's own `/repo` is masked and cannot contribute content to what `git
     submodule status` reads. Pointing a debugger at the image layer for a
     state only a host-side write can produce is a wrong lead, and the first
     draft's parenthetical did exactly that.
   - a declared path that is **not empty** → content in a directory no git
     reader can see (MU3), which is HARVESTING's existing "a suite that writes
     inside the submodule is out" rule enforced at the only place it still
     can be;
   - a declared path the container's index has **no gitlink for** → the
     manifest and the tree disagree. `derive_submodules` refuses this on the
     host, so reaching it means the container's tree is not the one that was
     derived; it is recorded as an observation rather than trusted to the
     host-side refusal, for the same reason `_parse_submodule_status`'s
     docstring gives about not restating configuration.
6. **A second emptiness read after the suite runs**, at the existing *"the
   suite does not dirty the tree"* check, over the declared paths only,
   writing `evidence["submodules_empty_after_suite"]: dict[str, bool | None] |
   None`. A **mapping**, not a list, and that is a correction from the first
   draft: a list built from `empty is None or not empty` would have made one
   entry mean either "measured, has content" or "the read failed", collapsing
   two absences at the top level in the same document that argues against it
   two points up. Per path the value is `True` (empty), `False` (has content)
   or `None` (unreadable). At the top level `{}` is "measured, this task
   declares no unneeded submodules" and `None` is "not measured" (the
   pre-container early return). This read exists because MU3 measured that the
   neighbouring `git status --porcelain` — the whole point of that check —
   **cannot see** into a gitlink path. Without it, "the suite ran with the
   directory empty" is an assumption; with it, it is an observation. Any
   non-`True` value raises a problem, and the message says which of the two it
   is.

`evidence["submodules_empty_after_suite"]` is initialised to `None` beside
`evidence["submodules"]` and `evidence["submodules_orphaned"]` at the
pre-container early return, so the three submodule keys keep answering
together — the shape round-2 item 5 exists to keep true.

**`ls -A`, not `find`.** `ls` and `find` are both present in the Debian bases,
but `ls -A -- <path>` answers *both* halves of the assertion in one exec: a
non-zero exit is "the directory does not exist or could not be read", and
empty stdout on a zero exit is "it exists and is empty". `--` guards a path
beginning with a dash. The exit code is not compared against a literal `2`;
any non-zero is the same "could not be read" answer, because a permission
problem and an absent directory are both facts this gate cannot interpret and
must not silently render as `False`. `container.exec` runs with
`workdir=REPO_MOUNT`, so a repo-relative path resolves.

`container.exec` with an explicit exit-code branch, never `_checked_exec`, for
the reason `_gitlink_paths`' docstring already gives: `preflight` collects
problems and returns a `PreflightResult`, and `run_matrix`'s `preflight(...)`
call is unwrapped, so a raise here is a traceback instead of the NO-GO the
driver knows how to handle.

**`PREFLIGHT_VERSION` 13 → 14**, and the reasons in the order they actually
bind — a correction from the first draft, which led with the weaker one:

1. **The evidence shape changes for every task, declaring or not.** Two new
   fields per `submodules` entry and one new top-level key. Those manifests'
   digests do not move, so a warm 13-era blob and a fresh 14 blob would sit in
   the same cache describing different shapes, and a reader who cannot tell
   them apart reads an absent field as a positive negative claim. This is the
   reason the constant must move.
2. **A residual: a 13-era verdict on a key-declaring manifest.**
   `preflight_cache_key` is
   `f"{task.manifest_digest}|{image}|{start_sha}|{PREFLIGHT_VERSION}"` and
   `manifest_digest` is `sha256(raw_manifest + raw_reference)`, so adding the
   key to a manifest already changes the digest and no 13-era verdict can be
   served in the ordinary case. It is reachable only because `load_task` has
   **no top-level unknown-key refusal** (see the bullet in *What this does NOT
   do*): a manifest could have carried `submodules_unneeded` before this code
   shipped, been gated at 13 (which ignored it and NO-GOed on `stale`), and
   that stale NO-GO would otherwise be served forever.

### D7. The grader is unchanged, and both of its submodule behaviours still hold

No file under `grader.py` or `grade_schema.py` is touched, and neither
`GRADER_VERSION` (`"8"`) nor `GRADE_SCHEMA_VERSION` moves. Two things have to
be argued rather than assumed:

- **`_gitlinks_touched` → `NotGradedReason.SUBMODULE_GITLINK_UNGRADABLE`
  still fires.** It reads the submission's own chunks (`_chunk_is_gitlink` on
  a `160000` mode line in the chunk header), takes no `task` and no submodule
  set, and is therefore indifferent to whether the manifest declared anything.
  It is if anything *more* likely to fire here: an agent handed an empty
  tracked directory may well `git init` in it, and that produces the identical
  chunk. Measured after D10 lands (MU9's fixture, agent runs `git init` and
  commits inside the empty directory): the submission carries `index
  a77f2e0..88c9924 160000`, `_GITLINK_MODE`'s third alternative matches, and
  the refusal fires. Refusing that submission remains correct — the content is
  in the run tree's `.git/modules` and nowhere else, so the ladder would grade
  the original tree and stamp `resolved: False` on work it could not see.

  **This paragraph is only true once D10 lands, and the first draft did not
  know it.** Without D10 the refusal fires on *every* run of a
  declared-unneeded task, clean ones included, because the submission carries
  a phantom `deleted file mode 160000` (MU9). "Indifferent to whether the
  manifest declared anything" would then be exactly the defect: the whole task
  leaves the denominator with a `not_graded_reason` that reads as the agent's
  doing. D10 is what makes `_gitlinks_touched` fire on a real gitlink change
  and nothing else, which is what this bullet claims.
- **The fix-1 exclusion in `_check_test_restore` still applies.** It calls
  `git ls-tree -r -z <start_sha> -- <tests.paths>` and excludes every
  `160000` entry it finds from the `git rm -r` and the `git checkout`. A
  declared-unneeded submodule is **still a gitlink at `start_sha`** — that is
  the whole premise of this item — so `_ls_tree_gitlinks` still returns it and
  the exclusion still covers it. What the restore protects is then an empty
  directory rather than a populated one, which is a no-op, and that is the
  correct outcome rather than a reason to change anything: the grader has no
  business knowing which submodules a manifest declined.

For sqlglot specifically the gitlink is at the repository root and
`tests.paths` is `["tests/"]`, so `git ls-tree … -- tests/` finds no gitlink
at all and the exclusion is empty — also correct.

### D8. `start_sha` does not move, and the ordering makes that a property

**It does not move.** Three independent reasons, in decreasing strength:

1. **Nothing this key changes is an input to the setup commit.** `start_sha`
   is `base_sha` + `strip_paths` + the committed test half + `gitignore_extra`
   (the docstring on `materialize` says so, and `_SETUP_ENV` /
   `_SETUP_MESSAGE` fix the identity). `submodules_unneeded` touches none of
   the four. The gitlink is in `base_sha`'s tree and is not staged, not
   removed and not rewritten.
2. **`_init_submodules` runs after `start_sha` is computed and compared** —
   `materialize`'s last statement, below the `declared_start_sha` comparison.
   Broadening 6's D4 made that ordering the guarantee rather than an
   observation, and skipping work inside a function that runs after the pin
   cannot move the pin.
3. **Even if it ran earlier it could not move it**: MU3 measures `git add -A`
   staging zero bytes for a gitlink path in either state.

Pinned by test, because "cannot move" is the claim and not the evidence:
`test_the_click_task_still_loads_and_its_start_sha_has_not_moved` already
pins `33575cc0b75608fa5cbcb1d3ae3347b81eac437f` and is extended to assert
`task.submodules_unneeded == ()`; a new
`test_declaring_an_unneeded_submodule_does_not_move_start_sha` materializes
the fixture superproject twice, once with the key and once without, and
asserts the two `start_sha`s are byte-identical. The **tomlkit** pin
(`e1d72b883d2e452ca14835047e2fa7db02cdc4d8`) cannot be a unit test — that
manifest lives at `~/.cache/bakeoff-probe/taskset/tomlkit-514-inline-table-comment-separator`,
outside this repository — so it is a named verification step (Task 7, step 3)
that re-gates the task and requires the driver to report that exact
`start_sha`. Recorded as an asymmetry rather than papered over; see the open
questions for why the manifest is not copied into `bakeoff/taskset/`.

`ORACLE_VERSION` does not move either: its cache key is
`f"{task.manifest_digest}|{image}|{ORACLE_VERSION}"` and a manifest declaring
a new key has a new digest, so no stored verdict can be served across the
change. **`SCHEMA_VERSION` DOES move** — 3.8.0 to 3.9.0 — but not for anything
in this section: no `RunRecord` field is added or removed by the manifest key,
and nothing here touches the setup commit. It moves because D10 changes what
`artifacts.final_diff` and `Checkpoint.diff` assert; the argument and its
measurement are there, not here.

### D9. What the gate proves, and the §6.4 confound

**The gate does not prove the submodule is unneeded.** It proves something
narrower, and every claim site in this plan, in the click manifest block and in
the docs is written at that width (and none of them says the directory's
contents are invisible to the submission — that is D10's job, not a property
of git, per MU9):

> the declared `tests.f2p` ids are red before the reference fix and green
> after it, and the p2p sweep is green, **with the submodule directory empty**.

It cannot distinguish "the suite guards on the submodule's presence and
degrades cleanly" (MU5's sqlglot shape) from "the suite silently collects
fewer tests", because nothing in the evidence records a collected-test count —
the f2p/p2p verdicts and the dirty-tree check are all consistent with a suite
that shrank. For sqlglot the shrinkage is exactly what happens
(`tests/test_integration_loader.py` discovers nothing). Recording a collected
count for the p2p sweep would close the gap and is **out of scope here**: it
is a runner-adapter change touching all three frameworks, and it belongs with
the node adapter work rather than bolted onto this key. It gets a `TASKS.md`
entry in Task 6 so nobody re-derives the gap as a bug.

**The §6.4 confound: the humans who wrote the PR had the submodule.** For
sqlglot they had it populated by `make install`, and
`tests/sqlglot/__init__.py` then appends the integration package to `__path__`
while `tests/test_integration_loader.py` discovers additional tests (MU5) — so
upstream's own `make test` at that `base_sha` ran a strictly larger suite than
any arm will. Same class as `strip_paths` (the humans had the stripped
`CLAUDE.md`) and `image.env: CI`. It is not a reason to refuse the task — every
arm sees the identical tree, so it is a statement about external validity, not
about fairness between arms.

Recorded in **four** places, and the first draft managed two of them:

- a comment in the `task.yaml` of any task that uses the key — including this
  plan's own verification manifest (Task 7 Step 1), which the first draft
  wrote without one;
- the commented documentation block in `click-3360-write-usage-empty-args/task.yaml`;
- a bullet in `bakeoff/taskset/HARVESTING.md`;
- the rewritten rows in `docs/BUILDING-A-TASK-SET.md`, which is the
  *procedural* doc a task author follows while cutting.

### D10. `snapshot_diff` seeds its scratch index from `base_sha`, or every run of a declared-unneeded task is refused

MU9 is the measurement and it is a **blocker**: `container.snapshot_diff`
stages into `GIT_INDEX_FILE=/tmp/bakeoff-snapshot-index`, that index starts
empty, `git add -A` will not descend into a gitlink path, and the diff against
`base_sha` therefore reports an uninitialised submodule as **deleted** — 199
bytes on the fixture, 239 on sqlglot at the verification vehicle's own
`base_sha`, on a clean tree where the agent did nothing. `_GITLINK_MODE`
matches `^deleted file mode 160000$`, so `grade_run` refuses every such
submission as `SUBMODULE_GITLINK_UNGRADABLE`. Every checkpoint diff carries the
phantom too, so §5.5's curve is wrong from turn one.

**The fix is one line, before the staging loop:**

```python
        env = {"GIT_INDEX_FILE": SNAPSHOT_INDEX}
        self.checked_exec(["git", "read-tree", base_sha], env=env)
        for attempt in range(_ADD_ATTEMPTS):
            ...
```

`git read-tree <base_sha>` writes `base_sha`'s tree into the scratch index, so
`git add -A` then *updates* an index that already holds the gitlink instead of
building one from a worktree scan that cannot see it. Measured (MU9's fixture
and sqlglot): clean tree with an uninitialised gitlink → **0 bytes**.

**Four properties, each measured, and the last three are why this is the right
one of the two candidates:**

- **It leaves a task with no submodules byte-identical.** A plain repository
  with a modification, an addition, a deletion and an ignored untracked file
  gives **384 bytes either way, `cmp`-identical**. Pinned by a test, because
  "byte-identical" is the claim and not the evidence.
- **It is total.** One `checked_exec` that raises `ContainerError` exactly as
  the `git diff` calls below it do, contained by the machinery that already
  contains `snapshot_diff`'s failures — `maybe_capture` catches and records
  `checkpoint_error`, `force_capture` is still allowed to raise. It raises
  rather than falling through, for the same reason `grader._refresh_index`
  does: the fallback for a silently failed seed is the phantom deletion, which
  is the accusation this section exists to prevent.
- **It needs no `task`.** `container.py` has no manifest and must not gain
  one, so the seed is unconditional. That is affordable precisely because of
  the byte-identity property above.
- **It fixes a second, pre-existing phantom**: a tracked file that also
  matches `.gitignore` (the `gitignore_extra` shape) is reported deleted under
  today's empty index and is not under the seed (MU9's last paragraph, 460 vs
  135 bytes). Not this item's bug; fixed by the same line, and pinned so it is
  not reverted.

**The rejected alternative, refused by measurement rather than by taste.**
Re-staging the gitlink after `git add -A` with `git update-index --add
--cacheinfo 160000,<sha>,<path>` was the other candidate. It **fails** in
exactly the case that matters — an agent that wrote a file into the empty
directory — because `add -A` has by then staged `vendor/libdep/agent_wrote_this.py`
as a blob under that prefix:

```
error: 'vendor/libdep' appears as both a file and as a directory
error: vendor/libdep: cannot add to the index - missing --add option?
fatal: git update-index: --cacheinfo cannot add vendor/libdep
```

Non-zero, in a function that must be total; contained, it leaves the phantom
in place (measured: 326 bytes, unchanged by the failed re-stage). It also needs
the submodule set and their shas threaded into `container.snapshot_diff`,
which has no `task` and should not acquire one.

**Cost.** Measured on `tobymao/sqlglot` at the verification `base_sha` (360
tracked files, 70 MB worktree), three consecutive calls against a persisted
scratch index: today **0.04 s**, seeded **0.05 s**. The seed discards the
index's stat cache so `add -A` re-hashes, and at this size that is 10 ms per
checkpoint against a per-turn budget measured in seconds. Named rather than
waved away, because it scales with the worktree; if a future task set carries
a repository where it does not, the remedy is a narrower base for the read-tree
and not a conditional seed.

**Both capture paths, one line.** `snapshot_diff` has exactly one production
caller — `checkpoints.CheckpointRecorder`, which serves both `maybe_capture`
(the per-turn loop) and `force_capture` (the submission) — so this is sound
for both by construction rather than by two arguments.

**`SCHEMA_VERSION` MOVES, and the first draft of this section got that wrong
on a measurement that did not cover the shape it was used to dismiss.** It said
"no record field is added or changes meaning… for every task in today's corpus
they do not [change], which is the byte-identity property" — but the
byte-identity measurement was taken over an *ignored untracked* file, and the
phantom needs a file **tracked at `base_sha` that also matches `.gitignore`**.
Measured with `git ls-files --cached --ignored --exclude-standard` on the probe
clones:

```
sqlglot 0   tomlkit 0   pytest 0   werkzeug 0   pallets/click 0
eemeli/yaml 15   (.editorconfig, .github/workflows/*, .gitignore, .gitmodules, …)
bidict       1   (.coveragerc)
```

`yaml-474-single-newline-empty-value` and `bidict-389-putall-rollback-clean`
are both gated probe tasks and are round 2's own verification vehicles for
items 1 and 3. So a pre-D10 run of yaml-474 writes an `artifacts.final_diff`
and fifteen `Checkpoint.diff`s asserting fifteen deletions that never happened
— `.gitmodules` and `.gitignore` among them — and a post-D10 run of the same
task does not. That is an **existing field changing what it asserts**, which is
strictly stronger than the additive-field case `CLAUDE.md` already says must
move the constant, and nothing in the record discriminates the two except
`harness_commit`, which a reader would have to map to a commit range by hand.

Second-order and worth stating where a reader meets it: the grader **applies**
`final_diff` at `start_sha`, so on a pre-fix yaml run the ladder really does
delete those fifteen files in the grading tree before running the suite.

**No stored record carries the phantom.** All ten event logs under
`~/.cache/bakeoff` hold `click-3360-write-usage-empty-args` records only, and
click has 0 tracked-but-ignored files at its `base_sha`. So the version moves
to keep future records readable, not to annotate past ones.

**And the last property, measured after the seed (MU9 row G′):** an agent that
writes a file *into* the declared-unneeded directory produces **0 bytes** —
the submission diff cannot carry it, and neither can a checkpoint, because git
does not descend into a gitlink path and the seeded index does not either.
`ls -A` sees it, which is why D6's two reads exist and are the gate's only
enforcement. **Capturing that state at run time is round-2 item 17's job**
(`docs/superpowers/plans/2026-09-03-round2-17-submodule-dirty-capture.md`) —
an `ls -A` over uninitialised gitlink directories, recorded in
`submodules_dirty` and refused by the grader. This plan does not duplicate it:
it records the measurement, points at the owner, and Task 6 Step 5 files a
`TASKS.md` line **only if item 17 does not land**.

---

## File Structure

| File | Change |
|---|---|
| `bakeoff/src/bakeoff/tasks.py` | `Submodule.declared_unneeded`; `TaskManifest.submodules_unneeded`; `_validate_unneeded_submodules`; the parse in `load_task`; the typo refusal, `needed_gitlinks` and the two exemptions in `derive_submodules`; the guard in `_refuse_submodule_conflicts`; the filter and early return in `_init_submodules` |
| `bakeoff/src/bakeoff/images.py` | the `continue` in `_extract_submodules` |
| `bakeoff/src/bakeoff/container.py` | **one line** in `snapshot_diff`: `git read-tree <base_sha>` into `SNAPSHOT_INDEX` before `git add -A`, plus the docstring paragraph (D10) |
| `bakeoff/src/bakeoff/schema.py` | `SCHEMA_VERSION` minor +1 (`3.8.0` → `3.9.0`) and its `#:` paragraph — an existing field changed what it asserts (D10) |
| `bakeoff/src/bakeoff/preflight.py` | the `getattr` read, two new per-entry evidence fields filled in the CALLER, `_directory_is_empty`, the `stale` exclusion, three new problems, the post-suite mapping `submodules_empty_after_suite`, `PREFLIGHT_VERSION` 13 → 14 |
| `bakeoff/tests/test_tasks.py` | fourteen new tests, one extended, one existing docstring corrected; the new `upstream_two_submodules` fixture, built in Task 1 |
| `bakeoff/tests/test_images.py` | two tests, **plus `submodules_unneeded = ()` on `_sub_task_stub._Task`** — without it three existing tests die with `AttributeError` (Task 3) |
| `bakeoff/tests/test_container.py` | seven tests: three unit (argv order, the scratch-index env, the raise) and four `@integration`; a new `_SnapshotCalls` recording stub; `_container_with` loses its `checked_exec` override; one existing docstring corrected (Task 5) |
| `bakeoff/tests/conftest.py` | a `git_container_factory` fixture — `git_container` builds one fixed tree and three of the new tests need three different ones (Task 5) |
| `bakeoff/tests/test_preflight.py` | eight tests + two extended; `_ScriptedContainer` and `_FakeTask` gain fields (Task 4) |
| `bakeoff/scripts/mutation_check.py` | **eleven** entries |
| `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` | the commented documentation block for the key |
| `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`, `TASKS.md`, `tasks/todo.md` | docs (Task 6) |

**Not modified, deliberately:** `grader.py`, `grade_schema.py` (D7),
`oracle.py`, `checkpoints.py` (D10 — its one call site is unchanged, and it
already contains `snapshot_diff`'s failures),
`preflight._parse_submodule_status` (D6 point 1), `scripts/run_matrix.py`,
`scripts/grade.py`, `HANDOFF.md`. **No file is created.** `container.py`
moved OFF this list in review 3 — the first two drafts had it here on the
strength of MU3, which measured the wrong index.

Anchor on symbol names, not line numbers: `Submodule`, `TaskManifest`,
`_validate_prefixes`, `_validate_strip_paths`, `_PATHSPEC_MAGIC`, `load_task`,
`derive_submodules`, `_has_gitmodules`, `_refuse_submodule_conflicts`,
`task_submodules`, `_init_submodules`, `materialize`, `_extract_submodules`,
`build_task_image`, `_parse_submodule_status`, `_gitlink_paths`,
`PREFLIGHT_VERSION`, `preflight`, `snapshot_diff`, `SNAPSHOT_INDEX`,
`_ADD_ATTEMPTS`, `checked_exec`, `SCHEMA_VERSION`, `git_container`,
`_container_with`, `_AddSequence`, `_FakeContainer`.

Task order is loader → materialize → image → gate → snapshot index → docs →
verification, so each task's tests pass against the tree the previous one
left. Task 5 is independent of Tasks 1-4 and may be done first; it must be
green before Task 7 runs anything.

---

## Task 1: the manifest key, its shape validation, and the derivation

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py`
- Test: `bakeoff/tests/test_tasks.py`

**Interfaces:**
- Produces: `TaskManifest.submodules_unneeded: tuple[str, ...] = ()`;
  `Submodule.declared_unneeded: bool = False`;
  `tasks._validate_unneeded_submodules(paths: tuple[str, ...], where: str) -> None`.
- Consumes: `tasks._strs`, `tasks._validate_prefixes`, `tasks._PATHSPEC_MAGIC`,
  `tasks._has_gitmodules`, `tasks.TaskError`.

- [ ] **Step 1: `Submodule.declared_unneeded`**

Add the field to the frozen `Submodule` dataclass, last, with default `False`,
and extend the class docstring. The docstring sentence to add, in this file's
register:

> `declared_unneeded` is the one field that is NOT derived. It is the
> manifest's `submodules_unneeded` list, joined onto the derived entry by
> path, and it is carried here rather than consulted separately so that every
> reader of a `Submodule` — the initialiser, the image builder, the gate's
> evidence — sees one answer instead of three lookups that can disagree. The
> entry is kept in the tuple rather than filtered out: a filtered submodule
> renders as a tree with no gitlink there, which is a different tree.

- [ ] **Step 2: `TaskManifest.submodules_unneeded`**

Add the field after `strip_paths`, `tuple[str, ...] = ()`, with a `#:` comment:

> Submodule paths this task declares it does not need. The gitlink stays in
> the index and the tree exactly as at `base_sha`; the directory is never
> populated, its `.gitmodules` url is never checked — nor is a readable
> `.gitmodules` required to exist for it at all — and no pruned mirror is
> built for it. What lifts a repository's submodule floor without rewriting
> history: `tobymao/sqlglot` carries an ssh-url submodule from 2026-02-27 that
> closed every later `base_sha`, for suites that guard on its presence and
> never read it. Section 6.4 confound: the humans who wrote the PR had it.

- [ ] **Step 3: `_validate_unneeded_submodules`**

Place immediately after `_validate_strip_paths`. Reuse `_validate_prefixes`
for empty/padded/absolute/`..`, then the four refusals. Messages verbatim:

```python
def _validate_unneeded_submodules(paths: tuple[str, ...], where: str) -> None:
    _validate_prefixes(paths, where)
    seen: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts:
            raise TaskError(
                f"{where}: {path!r} names the whole tree; this key names one "
                "submodule path per entry, and a tree is not a gitlink"
            )
        if parts[0] == ".git":
            raise TaskError(
                f"{where}: {path!r} is inside the repository's own .git; a "
                "gitlink is a tree entry, and nothing under .git is one"
            )
        if path.startswith(":") or any(ch in path for ch in _PATHSPEC_MAGIC):
            raise TaskError(
                f"{where}: {path!r} carries pathspec magic; this key names "
                "paths, and a pattern would make which submodules are left "
                "unpopulated a property of the tree rather than of the "
                "manifest"
            )
        if path in seen:
            raise TaskError(
                f"{where}: {path!r} is listed twice; the second entry cannot "
                "change anything, so a manifest carrying it means something "
                "the key cannot express"
            )
        seen.add(path)
```

Docstring, in the house register, must say: the shape checks live here because
`load_task` runs offline with no repository, so the *existence* half — is this
path a gitlink at `base_sha`? — cannot be asked until a mirror exists, and is
therefore in `derive_submodules`, exactly as `strip_paths`' "names nothing
tracked" check is in `_strip_paths_from_tree`. The duplicate refusal is the
one `strip_paths` does not have and is here because this key is consumed as a
SET, so a repeat is invisible to every downstream comparison.

- [ ] **Step 4: parse it in `load_task`**

Beside the existing `strip_paths` parse, before the reference split:

```python
    submodules_unneeded = _strs(
        data.get("submodules_unneeded"), f"{where}:submodules_unneeded"
    )
    _validate_unneeded_submodules(
        submodules_unneeded, f"{where}:submodules_unneeded"
    )
```

and pass `submodules_unneeded=submodules_unneeded` in the `TaskManifest(...)`
construction, beside `strip_paths=strip_paths`.

- [ ] **Step 5: `derive_submodules` — the typo refusal, `needed_gitlinks`, and the flag**

Three edits, in this order down the function.

**(a) The typo refusal, immediately after the `gitlinks` dict is built and
before the `if gitlinks or _has_gitmodules(...)` block** — D3's code block
verbatim. Comment above it:

> FIRST, before anything reads `.gitmodules`. Both of the refusals below are
> derived from that file, and an author who misspells the path of the very
> submodule they are exempting must be told about the typo rather than about
> a url or an unreadable blob. The position is the message.

**(b) One derived set, above both `.gitmodules` refusals:**

```python
    needed_gitlinks = set(gitlinks) - set(task.submodules_unneeded)
```

with a comment saying why one set and not two subtractions: both exemptions
are the same claim (nothing is fetched for a declared path, so no url and no
`.gitmodules` are needed for it), so they share one line and one mutation can
revert both.

Then rewrite the two raises to use it:

```python
        if listing.returncode != 0 and needed_gitlinks:
            raise TaskError(
                f"{task.task_id}: {task.base_sha} carries gitlinks "
                f"({', '.join(sorted(needed_gitlinks))}) but no readable "
                ".gitmodules, so no url exists to fetch them from and the "
                "directories would arrive empty."
            )
```

```python
    unfetchable = sorted(needed_gitlinks - set(by_path))
```

Note for the implementer: `if gitlinks or _has_gitmodules(...)` on the
enclosing block is **unchanged** — it decides whether to *attempt* the read,
and a declared-only tree still attempts it; what changes is that a failure is
now tolerated. Measured (MU8), the failing exit is **128**, not 1.

**(c) Stamp the flag when the tuple is built:**

```python
    unneeded = set(task.submodules_unneeded)
    subs = tuple(
        Submodule(name=by_path.get(path, (path, {}))[0], path=path,
                  url=by_path.get(path, (path, {}))[1].get("url", ""), sha=sha,
                  declared_unneeded=path in unneeded)
        for path, sha in sorted(gitlinks.items())
    )
```

`by_path.get(path, (path, {}))` is new and is what lets a declared-unneeded
gitlink construct an entry with **no** stanza and even with no readable
`.gitmodules` at all: `name` falls back to the path and `url` to `""`. That
combination is unreachable for a needed submodule, because rows 1 and 2 of
D4's table refuse it above.

- [ ] **Step 6: the guard in `_refuse_submodule_conflicts`**

Exactly D4's shape, with the two kept refusals outside the guard. Update the
function docstring: it says "four refusals" today; it becomes two that are
skipped for a declared path and two that are not, with the one-sentence reason
for each kept one (the strip still deletes the gitlink and moves `start_sha`;
`git add -A` stages nothing for a gitlink path in either state, so a fix
living there is ungradable however the manifest declares it). Add one line
saying the two kept refusals are deliberately OUTSIDE the guard and that
`mutation_check.py` anchors their placement, so an editor who tidies them
inside is caught.

- [ ] **Step 7: the tests**

In `bakeoff/tests/test_tasks.py`, extending the existing `# --- submodules ---`
section.

**How the key is written into a fixture manifest.** `_write_task` / `_manifest`
have no `submodules_unneeded` keyword and must not gain one; the established
route for a top-level key is `extra_yaml=`, as
`test_strip_paths_loads_and_is_exposed` uses for `strip_paths`. So:

```python
_sub_task(tmp_path, up, extra_yaml='submodules_unneeded: ["vendor/libdep"]')
```

A test needing both top-level keys concatenates them with a newline:

```python
extra_yaml=('strip_paths: ["vendor/libdep"]\n'
            'submodules_unneeded: ["vendor/libdep"]')
```

**The `upstream_two_submodules` fixture is built HERE, in Task 1**, because
Task 1's `test_a_declared_unneeded_gitlink_with_no_stanza_in_a_readable_gitmodules`
is its first consumer and the plan's task order exists so that each task's
tests pass against the tree the previous one left. Task 2 reuses it and
defines nothing.

Add it beside `upstream_submodule` rather than parameterising that one --
every existing test in the section depends on `upstream_submodule`'s exact
halves. Same construction as `upstream_submodule`, with a **second** upstream
library committed at `vendor/other`, so the superproject carries two gitlinks
and a `.gitmodules` with two stanzas. It takes one keyword,
`stanzas: int = 2`: at `stanzas=1` the fixture rewrites `.gitmodules` to carry
the `vendor/libdep` stanza only and commits that, which is the
readable-but-no-stanza tree D4 row 2 is about (`vendor/other` keeps its
gitlink and loses its url). Every submodule-touching git command carries
`-c protocol.file.allow=always`, for the reason `upstream_submodule`'s
docstring already gives. Return the same dict shape plus `other_lib`,
`other_pinned` and `sub_path_other`.

The tests, each with what it asserts:

| test | asserts |
|---|---|
| `test_a_declared_unneeded_submodule_is_not_refused_for_its_url` | rewrite the fixture's `.gitmodules` url to `git@example.invalid:x/y.git` and commit; **without** `local_urls`, `derive_submodules` returns one entry with `declared_unneeded is True`, `url == "git@example.invalid:x/y.git"`, and does not raise. This is the item, in one test. |
| `test_an_unneeded_declaration_naming_no_gitlink_is_refused` | `submodules_unneeded: ["vendor/typo"]` → `TaskError` matching `"vendor/typo"` and `"no gitlink"`. |
| `test_the_typo_refusal_beats_the_url_refusal` | the ssh fixture with `submodules_unneeded: ["vendor/libdeps"]` (a near-miss) → the raised message names `submodules_unneeded`, not the url. |
| `test_the_typo_refusal_beats_the_unreadable_gitmodules_refusal` | `git rm --cached .gitmodules` + commit, then `submodules_unneeded: ["vendor/typo"]` → the message names `submodules_unneeded`, not `"no readable .gitmodules"`. Together with the row above this pins D3's one-line position; either refusal winning means the check moved. |
| `test_a_declared_unneeded_gitlink_survives_an_unreadable_gitmodules` | `git rm --cached .gitmodules` + commit, declare the path → one entry, `url == ""`, `name == "vendor/libdep"`, `declared_unneeded is True`, no raise. Exemption of D4 row 1 (MU8). |
| `test_a_declared_unneeded_gitlink_with_no_stanza_in_a_readable_gitmodules` | uses `upstream_two_submodules`: `.gitmodules` carries a stanza for the first gitlink only, the second is declared → two entries, the declared one `url == ""`, no raise. Exemption of D4 row 2, the narrower shape the `by_path.get` fallback was justified for. |
| `test_a_gitlink_with_no_gitmodules_url_is_refused` | **existing, unchanged** — the needed case still raises. Its docstring is corrected: it performs `git rm --cached .gitmodules`, so it exercises D4 row 1 (the unreadable-blob raise), not row 2. The first draft of this plan mislabelled it. |
| `test_a_strip_path_covering_a_declared_unneeded_submodule_is_still_refused` | `strip_paths` and `submodules_unneeded` both naming `vendor/libdep` → `TaskError` matching `"strip_paths"`. D4 row 5. |
| `test_a_reference_diff_touching_a_declared_unneeded_submodule_is_still_refused` | a reference whose test half writes `vendor/libdep/tests/test_sub.py`, with the path declared → `TaskError` matching `"ungradable"`. D4 row 6. |
| `test_an_unneeded_declaration_is_shape_validated_at_load` | `pytest.mark.parametrize` over `"/abs"`, `"../up"`, `"."`, `"./"`, `".git"`, `".git/modules"`, `"vendor/*"`, `":(exclude)v"`, `""`, `" v"` → each raises `TaskError` from `load_task` with **no** repository present (the `"0"*40` base_sha manifests this module already builds), which is what pins that the shape half is offline. |
| `test_a_duplicated_unneeded_declaration_is_refused` | `["vendor/libdep", "vendor/libdep"]` → `TaskError` matching `"listed twice"`. |
| `test_the_click_task_still_loads_and_its_start_sha_has_not_moved` | **extend**: add `assert task.submodules_unneeded == ()` beside the existing `declared_start_sha` assertion. |

---

## Task 2: materialize leaves it empty, and builds no mirror for it

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (`_init_submodules`)
- Test: `bakeoff/tests/test_tasks.py`

- [ ] **Step 1: the filter and the early return**

Exactly D5's shape. Add to `_init_submodules`' docstring, which already
carries three load-bearing paragraphs, a fourth:

> A submodule the manifest declared UNNEEDED is skipped here entirely, and the
> `if not needed: return` above the mirror comprehension is what makes "no
> pruned mirror is built for it" a property of the code rather than of an
> empty comprehension. Its url is never read, so an ssh or relative url costs
> nothing; its directory stays as `git checkout` left it, which is present and
> empty (measured 2026-09-02, git 2.50.1: `git clean -xfd` does not remove it,
> `git status --porcelain` reports the tree clean, and `git submodule status`
> reads `-<sha>`). The refusals still run over the FULL tuple, because two of
> them — a strip covering the path, and a reference diff touching it — are
> unrelated to whether the directory is populated.

- [ ] **Step 2: the tests**

No new fixture. `upstream_two_submodules` is built in Task 1 Step 7 (its
first consumer is there), and this task's mixed-submodule test reuses it at
its default `stanzas=2`. Manifest keys go in via `extra_yaml=`, as Task 1
Step 7 states.

| test | asserts |
|---|---|
| `test_materialize_leaves_a_declared_unneeded_submodule_empty` | after `materialize`: the directory exists, `list(dir.iterdir()) == []`, `git submodule status` in the run tree starts with `-`, `git status --porcelain` is empty, `git ls-files -s` still carries the `160000` entry at its `base_sha` sha, and `(dest/".git"/"modules").exists()` is False. |
| `test_no_pruned_mirror_is_built_for_a_declared_unneeded_submodule` | `monkeypatch.setattr(tasks, "ensure_pruned_mirror", recording)` where `recording` records `(url, sha)` and delegates to the real function; assert the submodule's url never appears in the recorded calls (the superproject's does). Uses the ssh-url variant, so a mirror attempt would also fail loudly. |
| `test_declaring_an_unneeded_submodule_does_not_move_start_sha` | materialize the same fixture task into two destinations, one manifest with the key and one without (the without-case needs `local_urls`, since its url is checked); assert the two returned `start_sha`s are equal. D8's pin. |
| `test_a_mixed_task_populates_the_needed_submodule_and_not_the_other` | `upstream_two_submodules` with one declared → the declared one is empty with marker `-`, the other is at its gitlink with a leading-space marker. Pins that the filter is per entry, not per task. |

---

## Task 3: the image takes no second archive for it

**Files:**
- Modify: `bakeoff/src/bakeoff/images.py` (`_extract_submodules`)
- Test: `bakeoff/tests/test_images.py`

- [ ] **Step 1: the `continue`**

Exactly D5's shape, placed as the first statement of the loop body, before
`ensure_pruned_mirror`. Add to the function docstring:

> A submodule declared unneeded in the manifest is skipped: no mirror, no
> archive, no `mkdir`. The empty directory the image needs is already in the
> context — measured 2026-09-02, `git archive <base_sha> | tar -x` creates the
> gitlink's path as an empty directory — so re-creating it here would make the
> image's tree an artifact of this function rather than of `base_sha`'s, which
> is not the same claim the moment anything reorders around the strip.

- [ ] **Step 2: fix the stub FIRST, then the tests**

`derive_submodules` now reads `task.submodules_unneeded`. `_sub_task_stub`'s
inner `_Task` is a bare class carrying `task_id`, `task_version`, `repo_url`,
`base_sha`, `image`, `tests`, `strip_paths`, `test_files`, `solution_files`
and `extra_files` — and nothing else, so
`test_the_build_context_carries_submodule_content`,
`test_the_build_context_still_carries_no_git_directory` and
`test_an_ancestor_strip_path_coexists_with_the_submodule_extract` all die with
`AttributeError` before asserting anything.

**Add `submodules_unneeded = ()` to `_sub_task_stub._Task`**, and give
`_sub_task_stub` a `submodules_unneeded=()` keyword set the same way
`strip_paths` is (`_Task.submodules_unneeded = tuple(submodules_unneeded)`), so
the new tests can declare one. Do **not** make `derive_submodules` defend with
`getattr`: `tasks.py` owns the dataclass and must not defend against its own
field — the `getattr` rule is `preflight`'s, and it is `preflight`'s because
that function takes an untyped `task` from callers it does not own.

| test | asserts |
|---|---|
| `test_no_second_archive_is_taken_for_a_declared_unneeded_submodule` | the built context has `repo/<sub path>` as a directory with zero entries, and a recording `ensure_pruned_mirror` was never called with the submodule's url. Mirrors `test_the_build_context_carries_submodule_content`, inverted. |
| `test_a_declared_unneeded_submodule_leaves_no_dotgit_in_the_context` | `assert [p for p in repo.rglob(".git")] == []` and `assert (repo / ".gitmodules").exists()` — the house form, matching `test_the_build_context_still_carries_no_git_directory`. Not `rglob(".git*")` against a one-element list: that also matches `.gitignore` and `.gitattributes`, and `rglob` order is unspecified. |

---

## Task 4: the gate asserts the declared state

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py`
- Test: `bakeoff/tests/test_preflight.py`

- [ ] **Step 1: the manifest read and the per-entry fields**

`_parse_submodule_status` is **not modified** (D6 point 1). Above the submodule
block:

```python
        unneeded = frozenset(getattr(task, "submodules_unneeded", ()))
```

computed once and reused by this step and by Step 4. `getattr`, never bare
attribute access — the rule the strip check states two screens up in the same
function.

After `_parse_submodule_status` returns, one pass over `submodules` fills both
new fields:

```python
            for entry in submodules:
                entry["declared_unneeded"] = entry["path"] in unneeded
                entry["empty"] = _directory_is_empty(container, entry["path"])
```

The entry shape becomes, in this key order:

```python
{"path": ..., "sha": ..., "initialised": ..., "marker": ...,
 "declared_unneeded": ..., "empty": ...}
```

The four `preflight()` tests that assert an entry by equality against a
four-key dict move to the six-key shape. The pure `_parse_submodule_status`
unit tests do **not** move — that function still returns four keys, which is
the second reason D6 keeps the manifest out of it.

- [ ] **Step 2: the emptiness helper**

A module-level helper beside `_gitlink_paths`:

```python
def _directory_is_empty(container, path: str) -> bool | None:
    """Whether `path` in the container is an existing, EMPTY directory.

    `None` is "could not be read", never `False`. `container.exec` with an
    explicit exit-code branch rather than `_checked_exec`, for the reason
    `_gitlink_paths` documents: this gate COLLECTS problems and its caller
    does not wrap it, so a raise here is a traceback instead of a NO-GO.

    This exists because git is BLIND here. Measured 2026-09-02 (git 2.50.1):
    a file inside an UNINITIALISED submodule directory is reported by neither
    `git status --porcelain`, nor `-uall`, nor `git ls-files -o`, nor
    `git add -A` followed by `git diff --cached <base_sha>` (zero bytes) --
    git does not descend into a gitlink path in any state. So the clean-tree
    check next door cannot see a suite that writes in there, and the only
    reader that can is the filesystem.

    `ls -A` answers both halves in one exec: a non-zero exit is "absent or
    unreadable" and empty stdout at exit 0 is "present and empty". The exit
    code is not compared against a literal -- a missing directory and a
    permission error are both answers this gate cannot interpret.
    """
    result = container.exec(["ls", "-A", "--", path])
    if result.exit_code != 0:
        return None
    return not result.stdout.strip()
```

- [ ] **Step 3: the gate rules**

In the `elif status.exit_code == 0:` branch, after `submodules` is parsed and
`unmatched`/`unlisted` are handled, and before `stale`:

- an entry whose `empty` is `None` adds a problem naming the path;
- for each entry with `declared_unneeded` true and `marker != "-"`: a problem;
- for each entry with `declared_unneeded` true and `empty is False`: a problem;
- `declared_missing = sorted(unneeded - set(gitlinks))` → a problem naming the
  paths.

`stale` becomes:

```python
        stale = [entry["path"] for entry in submodules
                 if not entry["initialised"] and not entry["declared_unneeded"]]
```

Problem messages, verbatim:

```
"the manifest declares {path} unneeded, but `git submodule status` reports "
"marker {marker!r} -- something populated a path the harness was told to "
"leave alone, so the suite is reading content this task was not cut against. "
"The run tree is bind-mounted over the image's own /repo, so this can only "
"have been written into the run tree after materialization."

"the manifest declares {path} unneeded, but the directory is not empty. git "
"cannot see in there -- measured 2026-09-02, a file inside an uninitialised "
"submodule directory is invisible to `git status --porcelain`, to `git "
"ls-files -o` and to `git add -A`, so no other check in this gate and no "
"submission diff would report it. A suite that writes inside a submodule is "
"out of the corpus."

"the manifest declares {path} unneeded, but the container's index carries no "
"gitlink there. The manifest and the tree the suite will run against "
"disagree about this path."

"could not list {path} to check whether it is empty (exit {code}): {head}. "
"Whether the directory is empty is unknown, and for a declared-unneeded "
"submodule that is the only claim this gate can check."
```

- [ ] **Step 4: the post-suite read**

At the existing *"the suite does not dirty the tree"* check, after the
`git status --porcelain` comparison:

```python
        after = {path: _directory_is_empty(container, path)
                 for path in sorted(unneeded)}
        evidence["submodules_empty_after_suite"] = after
        not_empty = sorted(p for p, ok in after.items() if ok is False)
        unreadable = sorted(p for p, ok in after.items() if ok is None)
```

with one problem per non-empty group and one per unreadable group, each saying
which of the two it is and that `git status --porcelain` — the check
immediately above — cannot see into a gitlink path. A **mapping**, so the
stored evidence itself distinguishes `False` from `None` per path rather than
relying on the problem text to do it; a list would collapse two absences into
one entry, which is the invariant D6 point 3 invokes two points earlier.

Initialise `evidence["submodules_empty_after_suite"] = None` beside
`evidence["submodules"]` and `evidence["submodules_orphaned"]` at the
pre-container early return. Top level: `None` is "not measured", `{}` is
"measured, this task declares none".

- [ ] **Step 5: `PREFLIGHT_VERSION` 13 → 14**

Read the constant, add one; do not hard-code a literal if another round-2 item
lands first. Add the `#:` paragraph in the file's existing style, in D6's
order: **lead** with the evidence-shape change (two new per-entry fields and
one new top-level key, on every task, whose manifests' digests do not move),
then the narrow residual (a 13-era verdict on a key-declaring manifest is
reachable only because `load_task` has no top-level unknown-key refusal, so a
manifest could have carried the key before this code shipped and been NO-GOed
on `stale`).

- [ ] **Step 6: the tests**

`_ScriptedContainer` gains `ls_entries: dict[str, tuple[str, ...]] | None`
(path → the entries `ls -A` reports; an absent key is an empty directory at
exit 0) and `ls_exits: dict[str, int]` for the unreadable case. To script the
two reads of one path differently, an `ls_entries` value may be a **tuple of
tuples**, consumed by call index on that path — say so in the parameter's `#:`
comment. `_FakeTask` gains `submodules_unneeded: tuple = ()`.

| test | asserts |
|---|---|
| `test_a_declared_unneeded_submodule_is_a_GO_while_uninitialised` | marker `-`, empty directory, `submodules_unneeded=("vendor/libdep",)` → `result.ok`, the evidence entry is exactly `{"path": "vendor/libdep", "sha": "0"*40, "initialised": False, "marker": "-", "declared_unneeded": True, "empty": True}`, and `evidence["submodules_empty_after_suite"] == {"vendor/libdep": True}`. The item, at the gate. |
| `test_a_needed_submodule_that_is_uninitialised_is_still_a_problem` | the same tree with `submodules_unneeded=()` → NO-GO, `stale` fires. The regression this change could silently take out. |
| `test_a_declared_unneeded_submodule_that_is_populated_is_a_problem` | marker `" "` with the path declared → NO-GO, message contains `"left alone"`. |
| `test_a_declared_unneeded_submodule_with_content_is_a_problem` | marker `-` but `ls -A` reports one entry → NO-GO, message contains `"not empty"`, `evidence["submodules"][0]["empty"] is False`. |
| `test_an_unlistable_submodule_directory_records_None_and_a_problem` | `ls` exits non-zero → `entry["empty"] is None` and a problem. `None`, never `False`. |
| `test_an_unneeded_declaration_the_index_has_no_gitlink_for_is_a_problem` | `submodules_unneeded=("vendor/gone",)`, `gitlinks=("vendor/libdep",)` → NO-GO, message contains `"vendor/gone"` and `"disagree"`. |
| `test_a_suite_that_writes_into_a_declared_unneeded_submodule_is_a_problem` | `ls -A` empty at the first read and non-empty at the second → NO-GO, `evidence["submodules_empty_after_suite"] == {"vendor/libdep": False}`. |
| `test_an_unreadable_directory_after_the_suite_is_not_reported_as_content` | first read empty, second read exits non-zero → `evidence["submodules_empty_after_suite"] == {"vendor/libdep": None}` and a problem whose text says the read failed, not that the directory has content. |
| `test_the_submodule_keys_are_written_on_the_early_return` | **extend**: `evidence["submodules_empty_after_suite"] is None` beside the two existing keys. |
| `test_an_initialised_submodule_at_its_gitlink_is_a_GO` | **extend**: the six-key entry, `declared_unneeded: False`, `empty: False`. |

---

## Task 5: seed the snapshot index, so a declared-unneeded gitlink is not a phantom deletion

**Files:**
- Modify: `bakeoff/src/bakeoff/container.py` (`snapshot_diff`)
- Test: `bakeoff/tests/test_container.py`

**Why this task exists and why it is here.** D10. Without it, MU9 measures a
`deleted file mode 160000` chunk in the submission of **every** run of a task
declaring `submodules_unneeded` — 239 bytes on the very sqlglot tree Task 7
gates — which `grader._gitlinks_touched` refuses as
`SUBMODULE_GITLINK_UNGRADABLE`. The whole task leaves the denominator with a
verdict that reads as the agent's doing. It is sequenced after the gate and
before the docs because it is independent of Tasks 1-4 (it touches no
submodule code and reads no manifest) and must be green before Task 7 runs
anything; an implementer may equally do it first.

- [ ] **Step 1: the seed**

In `snapshot_diff`, immediately after `env` is built and **before** the
`_ADD_ATTEMPTS` loop:

```python
        env = {"GIT_INDEX_FILE": SNAPSHOT_INDEX}
        self.checked_exec(["git", "read-tree", base_sha], env=env)
```

`env=env` is not optional and is the one thing an implementer can get wrong in
a way that is worse than the bug: without it the read-tree writes the
**repository's own** `.git/index`, which is the agent's staging area, running
concurrently with the agent — the exact thing this method's docstring says it
must never touch. A test pins the env, not only the argv.

`checked_exec`, not `exec`: it raises `ContainerError` exactly as the two
`git diff` calls below it do. Raising rather than falling through is the same
argument `grader._refresh_index` makes — the fallback for a silently failed
seed is the phantom deletion, i.e. the accusation. Containment already exists
one layer up (`checkpoints.maybe_capture` catches and names
`checkpoint_error`; `force_capture` is still allowed to raise), so this does
not weaken "checkpoint capture must never cost the run it is observing".

One consequence to fix in the same commit: `test_snapshot_diff_raises_when_git_fails`
now raises from the `read-tree` rather than from `git add -A`. Its `match="git"`
still passes, but its docstring describes the add/diff path -- update it (Step
5's closing note).

- [ ] **Step 2: the docstring**

Add a paragraph to `snapshot_diff`, in this file's register:

> THE SCRATCH INDEX IS SEEDED FROM `base_sha` FIRST, and it is not an
> optimisation. `GIT_INDEX_FILE` starts EMPTY, and `git add -A` does not
> descend into a gitlink path -- so for a submodule left uninitialised on
> purpose (`submodules_unneeded`) the staged index has no entry and the diff
> reports the gitlink as DELETED. Measured 2026-09-02, git 2.50.1: 199 bytes
> on a clean fixture tree and 239 on `tobymao/sqlglot` at
> `05eed63b…`, `deleted file mode 160000`, with the agent having done
> nothing. `grader._GITLINK_MODE` matches that line, so every such submission
> would be refused as SUBMODULE_GITLINK_UNGRADABLE and every checkpoint would
> carry a phantom the section 5.5 curve cannot tell from a real deletion.
> `git read-tree <base_sha>` makes the index start as the tree the diff is
> about to be taken against, so `add -A` UPDATES it rather than rebuilding it
> from a scan that cannot see the gitlink. The seed and the diff read the SAME
> parameter, so they can never disagree -- and the parameter named `base_sha`
> holds `start_sha` at run time (`matrix` -> `runner` -> `checkpoints`), which
> is why the committed test half does not appear as additions: `add -A`
> reconciles every worktree-visible path anyway, so the seed survives only
> where git is blind, which is the gitlink and nothing else. Measured 0 bytes
> seeding from either sha. Unconditional, because this class has no manifest and must not
> gain one: measured, a repository with no submodule diffs BYTE-IDENTICALLY
> either way (384 bytes, `cmp`-identical, over a modification, an addition, a
> deletion and an ignored untracked file). It also removes a second phantom
> that predates any of this -- a file tracked at `base_sha` that also matches
> a `.gitignore` pattern is skipped by `add -A` from an empty index and was
> reported DELETED (460 bytes vs 135, measured).

- [ ] **Step 3: the unit harness, and it is NOT `_FakeContainer`**

`_FakeContainer.exec_run(cmd, **_kw)` records nothing and swallows
`environment=`, so it cannot answer either of the two questions these tests
ask (what argv, in what order, with what env). `_AddSequence` is the shape
that works -- a callable stub passed to `_container_with` -- and this is its
sibling. Add beside it:

```python
class _SnapshotCalls:
    """Records (argv, env) per exec and can fail one command by prefix.

    `_FakeContainer` cannot serve the seed tests: its `exec_run(cmd, **_kw)`
    discards `environment=` and records nothing, so neither "read-tree before
    add" nor "the read-tree carried GIT_INDEX_FILE" is observable through it.
    """

    def __init__(self, fail_on: list[str] | None = None,
                 exit_code: int = 1, message: str = "boom"):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.fail_on = fail_on
        self.exit_code = exit_code
        self.message = message

    def __call__(self, cmd, env=None):
        from bakeoff.container import ExecResult

        self.calls.append((list(cmd), dict(env) if env else None))
        if self.fail_on and cmd[:len(self.fail_on)] == self.fail_on:
            return ExecResult(self.exit_code, "", self.message, 1)
        return ExecResult(0, "diff-body", "", 1)

    @property
    def argv(self) -> list[list[str]]:
        return [cmd for cmd, _env in self.calls]
```

**`_container_with` must stop faking `checked_exec`.** It currently sets
`container.checked_exec = lambda cmd, env=None: exec_stub(cmd, env)`, which
does **not** look at the exit code -- so a stub returning 1 for the read-tree
would be swallowed and `test_a_failed_seed_raises_rather_than_diffing` would
assert nothing. **Delete that line.** The real `RunContainer.checked_exec`
calls `self.exec(...)` and raises `ContainerError` on non-zero, and `exec` is
already stubbed, so removing the override exercises the production check
through the same stub. The two existing users of the helper
(`test_a_lost_race_against_the_agents_atomic_write_is_retried` and
`test_a_stat_failure_that_is_not_a_race_still_raises`) return exit 0 for every
non-`add` command, so the real check is a no-op for them -- verify that when
the line goes.

- [ ] **Step 4: the integration fixture, because `git_container` cannot serve three of these**

`git_container` (`tests/conftest.py`) builds **one fixed** repository --
`tests/test_a.py`, one commit, no submodule, no `.gitignore` -- and yields a
started `RunContainer`. Only the byte-identity test can use it unchanged. Add
beside it a factory that takes a build callback:

```python
@pytest.fixture
def git_container_factory(image_digest, tmp_path):
    """`git_container`, but the caller builds the repository.

    `git_container` yields one fixed tree and three of the snapshot-seed tests
    need three different ones: a gitlink over an empty directory, a tracked
    file matching `.gitignore`, and a tree an agent `git init`s inside. A
    fixture per shape would be three near-copies of the same twenty lines.

    Yields `make(build) -> (RunContainer, start_sha)`; `build(repo: Path)`
    writes files and may run git commands in `repo`, after which the factory
    commits everything with a fixed identity and starts the container. The
    container is closed at teardown through an `ExitStack`.
    """
```

**The gitlink is FABRICATED, with no second repository and no file
transport.** Measured 2026-09-02, git 2.50.1: `mkdir -p vendor/libdep` then

```
git update-index --add --cacheinfo 160000,1111111111111111111111111111111111111111,vendor/libdep
```

and a commit gives a tree whose `git ls-files -s` carries
`160000 1111111... 0<TAB>vendor/libdep`, an empty directory at that path, and
`git status --porcelain` empty -- exactly the declared-unneeded state, with no
`git submodule add`, no `protocol.file.allow=always` and no second repository
to clone. The first draft of this step said these tests build a submodule
"with `-c protocol.file.allow=always`", which contradicted "the existing
`git_container` fixture" in the same paragraph; neither is needed.

One caveat to write into the fixture's docstring: the fabricated sha resolves
to nothing, so `git submodule status` in such a tree exits non-zero with *"no
submodule mapping found in .gitmodules"*. That is irrelevant here -- these
tests call `snapshot_diff` and nothing else -- but it means the fixture must
**not** be reused for a preflight test, which reads exactly that command.

- [ ] **Step 5: the tests**

Three unit and four `@integration` -- seven. (The first draft said "two unit"
over a three-row table and called the total seven; the table was right.)

| test | mark | asserts |
|---|---|---|
| `test_the_snapshot_index_is_seeded_from_base_before_staging` | unit | with `_SnapshotCalls`, `stub.argv[0] == ["git", "read-tree", "abc"]` and `stub.argv[1] == ["git", "add", "-A"]`. Order, not presence: a read-tree issued after the staging is a seed the `add -A` never saw. **Mutation anchor 11's selector.** |
| `test_the_seed_writes_the_scratch_index_not_the_agents_own` | unit | the read-tree call's recorded env is `{"GIT_INDEX_FILE": SNAPSHOT_INDEX}`. Without it the seed rewrites the agent's own `.git/index` mid-run, which is worse than the bug being fixed. |
| `test_a_failed_seed_raises_rather_than_diffing` | unit | `_SnapshotCalls(fail_on=["git", "read-tree"])` -> `pytest.raises(ContainerError)`, and no `["git", "diff", ...]` appears in `stub.argv`. Requires Step 3's `checked_exec` deletion; the fallback for a silent failure here is the accusation. |
| `test_an_uninitialised_gitlink_is_not_reported_as_a_deletion` | integration | factory, fabricated gitlink + empty directory -> `snapshot_diff(start_sha) == ("", [])` on a clean tree. Measured unseeded: **199 bytes**, names `["vendor/libdep"]`. |
| `test_a_repo_with_no_submodules_snapshots_byte_identically` | integration | `git_container` **unchanged**: modification + addition + deletion + an ignored untracked file. The unseeded reference is produced *in the test* by raw `container.exec(["git", "add", "-A"], env={"GIT_INDEX_FILE": "/tmp/bakeoff-unseeded-index"})` then `git diff --cached <sha>` in the same env -- `snapshot_diff` always seeds after this change, so it cannot produce its own control. Compare the two as bytes. The pin for D10's unconditionality. |
| `test_a_tracked_file_matching_gitignore_is_not_reported_as_deleted` | integration | factory: `.gitignore` naming `.coveragerc`, plus `git add -f .coveragerc` so it is tracked -> `.coveragerc` absent from the diff's name list. Measured unseeded: names `[".coveragerc"]`. The pre-existing phantom, and the shape `eemeli/yaml` carries fifteen of. |
| `test_a_moved_gitlink_still_reaches_the_submission` | integration | factory with the fabricated gitlink, then in the container `git init` inside the empty directory and commit -> the diff carries `index 1111111..<sha> 160000` for that path, so `grader._chunk_is_gitlink` still matches. Measured post-seed. D7's guarantee, pinned on the fixed path rather than assumed. |

- [ ] **Step 6: `SCHEMA_VERSION`**

Read `schema.SCHEMA_VERSION` and bump the **minor** component (it reads
`"3.8.0"`; do not hard-code a literal if another round-2 item moves it first).
Add the `#:` paragraph in that file's existing prose style, saying what
changed and naming the measurement:

> 3.9.0 is the version at which `artifacts.final_diff` and every
> `Checkpoint.diff` stopped asserting deletions that never happened.
> `container.snapshot_diff` stages into a scratch index, and before this
> version that index was built by `git add -A` alone -- which skips a file
> that is tracked at the start state but also matches `.gitignore`, and never
> descends into a gitlink path. Both absences read as DELETIONS against
> `base_sha`. Measured 2026-09-02: `eemeli/yaml` carries **15** such tracked-
> but-ignored files (`.editorconfig`, `.github/workflows/*`, `.gitignore` and
> `.gitmodules` among them) and `bidict` **1** (`.coveragerc`), so a pre-3.9.0
> record of either task asserts deletions the agent never made -- and the
> offline grader APPLIES that diff, so the ladder really did delete them in
> the grading tree before running the suite. An uninitialised submodule
> directory (`submodules_unneeded`) produced the same shape as a `deleted file
> mode 160000` chunk, which `grader._gitlinks_touched` refuses outright. No
> field is added and none is removed; an existing field changed what it
> asserts, which is why the constant moves. No stored record carries either
> phantom: all ten event logs under `~/.cache/bakeoff` hold
> `click-3360-write-usage-empty-args` only, and click has 0 tracked-but-
> ignored files at its `base_sha`.

**Not a test, but do not leave it stale:** `test_snapshot_diff_raises_when_git_fails`
runs `alpine_container.snapshot_diff("abc123")` and matches `ContainerError,
match="git"`. With the seed first the raise now comes from `read-tree` rather
than from `git add -A`. The assertion still passes; its **docstring** describes
the add/diff path and must be updated to say the first command that touches
git is now the seed. Fix it in Step 1 alongside the code.


## Task 6: mutation anchors and docs

**Files:**
- Modify: `bakeoff/scripts/mutation_check.py`,
  `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`,
  `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`,
  `TASKS.md`, `tasks/todo.md`

- [ ] **Step 1: eleven mutation entries**

Every entry is `(comment, name, file, old, new, selector, marker)` with the
comment inside the tuple, and **every one ends with `"not integration"`** —
the first draft wrote the marker on one entry only, against a contract that
says an implementer transcribes and never invents. Ten came from review 1's
finding 9; entry 11 is D10's, added with the cross-item blocker.

| # | name | old → new | selector |
|---|---|---|---|
| 1 | `tasks: populate a submodule the manifest declared unneeded` | `    needed = tuple(sub for sub in subs if not sub.declared_unneeded)` → `    needed = tuple(subs)` | `tests/test_tasks.py -k leaves_a_declared_unneeded_submodule_empty` |
| 2 | `tasks: refuse a declared-unneeded submodule for its url` | `        if not sub.declared_unneeded:` → `        if True:` | `tests/test_tasks.py -k not_refused_for_its_url` |
| 3 | `tasks: move the strip refusal inside the unneeded guard` | `        stripped = [p for p in task.strip_paths` → `        stripped = [] if sub.declared_unneeded else [p for p in task.strip_paths` | `tests/test_tasks.py -k strip_path_covering_a_declared_unneeded` |
| 4 | `tasks: move the reference-diff refusal inside the unneeded guard` | `        touched = [p for p in (*task.test_files, *task.solution_files,` → `        touched = [] if sub.declared_unneeded else [p for p in (*task.test_files, *task.solution_files,` | `tests/test_tasks.py -k reference_diff_touching_a_declared_unneeded` |
| 5 | `tasks: accept an unneeded declaration naming no gitlink` | `    unknown = sorted(set(task.submodules_unneeded) - set(gitlinks))\n    if unknown:` → the same first line with `    if False:` | `tests/test_tasks.py -k naming_no_gitlink_is_refused` |
| 6 | `tasks: refuse a declared-unneeded gitlink that has no url` | `    needed_gitlinks = set(gitlinks) - set(task.submodules_unneeded)` → `    needed_gitlinks = set(gitlinks)` | `tests/test_tasks.py -k survives_an_unreadable_gitmodules` |
| 7 | `images: clone a mirror for a submodule declared unneeded` | `        if sub.declared_unneeded:\n            continue` → `        if False:\n            continue` | `tests/test_images.py -k no_second_archive_is_taken` |
| 8 | `preflight: keep a declared-unneeded submodule in the stale list` | `        stale = [entry["path"] for entry in submodules\n                 if not entry["initialised"] and not entry["declared_unneeded"]]` → `        stale = [entry["path"] for entry in submodules\n                 if not entry["initialised"]]` | `tests/test_preflight.py -k unneeded_submodule_is_a_GO` |
| 9 | `preflight: skip the start-state emptiness read` | `                entry["empty"] = _directory_is_empty(container, entry["path"])` → `                entry["empty"] = True` | `tests/test_preflight.py -k declared_unneeded_submodule_with_content` |
| 10 | `preflight: skip the post-suite emptiness read` | `        after = {path: _directory_is_empty(container, path)` → `        after = {path: True` | `tests/test_preflight.py -k writes_into_a_declared_unneeded_submodule` |
| 11 | `container: stage the snapshot into an unseeded scratch index` | `        self.checked_exec(["git", "read-tree", base_sha], env=env)` → `        self.checked_exec(["git", "--version"], env=env)` | `tests/test_container.py -k seeded_from_base_before_staging` |

Entries 3 and 4 are the ones D4 argues for in prose and the first draft did
not anchor: they are the exact edit "a later editor tidies the two kept
refusals inside the guard", and both mutated forms are valid Python (`[] if X
else [ ... ]` keeps the bracket balance across the existing continuation
lines). Entry 6 covers **both** `.gitmodules` exemptions with one line, which
is why D4 routes them through one derived set. Entry 7 is the original defect:
without the `continue`, the ssh url is cloned at image-build time. Entry 11
is D10's, and its `new` is a *different* exec rather than a deletion so the
mutated file still parses and the failure is the missing seed rather than a
syntax error; its selector is the unit argv-order test, not one of Task 5's
integration legs, because this gate runs under `"not integration"`.

**Entry 5's anchor is TWO lines, and that is not stylistic.** `mutation_check`
applies `original.replace(find, replace, 1)` with no uniqueness check, and
`    if unknown:` already appears **twice** in `tasks.py` (measured: the
`image.*` unknown-key refusal and the `grading.*` one, both inside `load_task`
and both far above `derive_submodules`). A one-line anchor therefore mutates
the image-key refusal, leaves the typo refusal intact, lets
`-k naming_no_gitlink_is_refused` pass, and the entry is reported MISSED --
loud rather than silent, but the run does not reach 171/171. Including the
`unknown = sorted(...)` line above it makes the anchor unique. Every other
entry's `old` string was checked to occur once; entry 8's does too, and it is
written out verbatim rather than described, because its `old` does not exist
until Task 4 has landed and an implementer would otherwise have to
reconstruct it from prose.

A comment on each entry saying what ships broken without the guarded line, in
the file's existing style.

Expected mutation count: baseline **+11**.

- [ ] **Step 2: the click manifest's documentation block**

`click-3360-write-usage-empty-args/task.yaml` is the documentation for every
manifest key, and this one is no exception even though `pallets/click` has no
gitlink at its `base_sha`. Add a commented block beside the `strip_paths` one:

```yaml
# submodules_unneeded: NOT USED by this task -- pallets/click carries no
# submodule at this base_sha. Shown because this manifest is the
# documentation for the key.
#
# Top-level, beside strip_paths and gitignore_extra, for the same reason:
# it is a modification the harness makes rather than a statement about
# upstream, and `repo:` holds only what can be checked against GitHub. Its
# truth is a property of THIS TASK's suite, not of the repository.
#
#   submodules_unneeded: ["sqlglot-integration-tests"]
#
# Each entry is a submodule path at base_sha -- a path, not a
# [submodule "NAME"] name, because git does not require the two to match and
# only the path comes from `git ls-tree`. The gitlink stays in the index and
# the tree exactly as it is; the directory is created empty by the checkout
# and by `git archive`, is never populated, its .gitmodules url is never
# checked -- nor does a readable .gitmodules have to exist for it at all --
# and no mirror is built for it. Declaring it does NOT move start_sha:
# nothing here takes part in the setup commit, and submodules are initialised
# after start_sha is settled.
#
# What it does NOT relax: a strip_paths entry covering the submodule is
# still refused (the strip removes the gitlink and moves start_sha), and a
# reference diff touching the submodule path -- either half -- is still
# refused (`git add -A` stages nothing for a gitlink path in any state, so a
# fix living there is ungradable by construction). A submission that moves
# the gitlink is still refused by the offline grader.
#
# Refused at load: an absolute path, "..", ".", ".git", a glob, a repeat.
# Refused when the mirror exists: a path the tree at base_sha has no gitlink
# at -- a typo must not silently declare nothing.
#
# What the gate CHECKS, and it is narrower than the key's name: the
# directory exists, is EMPTY and `git submodule status` reads `-`; the
# declared f2p ids are red before the reference fix and green after it; and
# the p2p sweep is green -- all with the directory empty. It re-reads
# emptiness after the suite, because git does not descend into a gitlink
# path and `git status --porcelain` cannot see a file written in there. What
# it CANNOT check is that no test anywhere needed the submodule: a suite
# that guards on its presence silently collects fewer tests, and no
# collected-test count is recorded.
#
# The submission diff carries NOTHING for the path -- but only because
# `container.snapshot_diff` seeds its scratch index from the start state
# first. Without that seed an uninitialised gitlink reads as a DELETION
# against an index `git add -A` built from a worktree scan, and the offline
# grader refuses every such submission as submodule_gitlink_ungradable
# (measured 2026-09-02: 239 bytes on this repository, clean tree).
#
# Section 6.4 confound, to be recorded here by any task that uses it: the
# humans who wrote the PR had the submodule.
```

- [ ] **Step 3: `HARVESTING.md`**

Four edits, and the second is a correction of an existing claim MU3 disproves.

- The "**Submodules are supported, with six limits**" bullet list becomes
  seven, with a new one: **a submodule the task's suite does not read can be
  declared unneeded.** State the key, that the url is then never checked
  (which is what reopens an ssh-url repository) and neither is a readable
  `.gitmodules` required for it, that the `strip_paths` and reference-diff
  refusals are unchanged, what the gate checks at D9's width, and the §6.4
  note.
- **Correct the existing "a suite that writes inside the submodule is out"
  bullet.** It currently says an untracked file there "makes the
  superproject's `git status --porcelain` report ` M <path>`, which is
  preflight's clean-tree NO-GO". MU3 shows that is true only of a **populated**
  submodule; for an uninitialised one — which is every declared-unneeded path
  — git reports nothing at all. Amend it to say so, and to say that for a
  declared-unneeded path the enforcement is preflight's `ls -A` read, taken
  before and after the suite. Left as written, the docs would assert an
  enforcement that does not exist for exactly the new case. Add the other half
  MU9 measured: what the agent writes in there reaches neither the submission
  nor a checkpoint **because the snapshot index is seeded from the start
  state** (D10), not because git is blind — before that seed the submission
  carried the agent's file *and* a phantom gitlink deletion.
- The sqlglot paragraph ("Measured 2026-09-02: a **second**, harder floor
  closes the window again…") ends with *"Cutting after 2026-02-27 needs the
  manifest lever in `TASKS.md` (a submodule declared unneeded) that does not
  exist yet."* Replace that sentence with the key, the two `tests/` guards
  MU5 quotes, and the `strip_paths: ["CLAUDE.md", "AGENTS.md"]` requirement at
  a post-2026-02-27 `base_sha` (MU6 — `CLAUDE.md` is a symlink there, and the
  existing paragraph's "at which point AGENTS.md does not exist yet" applies
  only to the older window).
- Move `tobymao/sqlglot` out of any "closed after 2026-02-27" framing in the
  screened-repository table, replacing it with "open at any `base_sha` with
  `submodules_unneeded: ["sqlglot-integration-tests"]`".

- [ ] **Step 4: `docs/BUILDING-A-TASK-SET.md`**

Row *"`.gitmodules` names a non-`https://` url"* currently says the repository
is closed at that `base_sha` and every descendant. Rewrite: closed **unless
the suite guards on the submodule's presence**, in which case
`submodules_unneeded` names it and the url is never read; check the guard by
grepping the suite for the path and confirming the references are behind an
existence test. Row *"needs a git submodule"* gains a sentence pointing at the
new key for the case where the suite does not need it. **Both rewrites carry
the §6.4 confound sentence** — this is the procedural doc a task author reads
while cutting, and the confound has to reach them there rather than only in
`HARVESTING.md`.

- [ ] **Step 5: `TASKS.md` and `tasks/todo.md`**

Tick the round-2 item 2 entry at line 484 and replace its body with what
shipped. **Conditional entry, and check before writing it:** if round-2 item 17
(`plans/2026-09-03-round2-17-submodule-dirty-capture.md`) has landed, its
`submodules_dirty` capture already owns the run-time visibility of an agent's
writes inside an uninitialised gitlink directory and **no** `TASKS.md` line is
written here. If it has not, add one naming the measurement (MU9 row G′: 0
bytes post-seed, `ls -A` sees the file) and pointing at that plan as the
owner. Do not file it unconditionally — a duplicate backlog entry for work
that shipped is the kind of stale line this repo's `TASKS.md` is curated to
avoid.

Add a **new** `TASKS.md` entry for the deferral D9 names: the gate
records no collected-test count, so it cannot tell a suite that guards on the
submodule from one that silently collects fewer tests; closing it is a
runner-adapter change across all three frameworks and belongs with that work.
Add a review section to `tasks/todo.md` in the existing style: the measured
defect, the key, what it does not relax, the three measurements that shaped
the design (MU3, MU5, MU8), and the verification results from Task 7.

---

## Task 7: verification against a real task

The vehicle is a **re-created** `sqlglot-8225-mysql-key-constraint`. The
original manifest was deleted (`d1-strip-paths.md`, "Old task dir … deleted per
instruction"), and every field it needs is recoverable: MU4, MU6 and MU7 above
plus the deps from the sibling `sqlglot-6927-dremio-trycast` manifest that was
cut from the same repository. A re-cut is **not** needed — this task is a
verification vehicle, not a corpus entry, and it is written under
`~/.cache/bakeoff-probe/taskset/`, never into `bakeoff/taskset/`.

- [ ] **Step 1: write the manifest**

`~/.cache/bakeoff-probe/taskset/sqlglot-8225-mysql-key-constraint/task.yaml`:

```yaml
task_id: sqlglot-8225-mysql-key-constraint
task_version: 1
tier: B
stratum: small-edit

repo:
  url: https://github.com/tobymao/sqlglot.git
  base_sha: 05eed63b281f7ac020045e2b792beef8fad8d3ee

prompt: |
  <issue 8225, verbatim>

tests:
  paths: ["tests/"]
  runner: ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
  f2p:
    - "tests/dialects/test_mysql.py::TestMySQL::test_column_key_constraint"
  p2p: []
  allow_extra_paths: []

strip_paths: ["CLAUDE.md", "AGENTS.md"]

# The submodule `sqlglot-integration-tests` is declared unneeded: its
# .gitmodules url is ssh (`git@github.com:fivetran/...`), which the harness
# cannot fetch, and this suite never reads it -- `tests/sqlglot/__init__.py`
# and `tests/test_integration_loader.py` both guard on
# `os.path.isdir(<submodule>/tests/sqlglot)` and append nothing when the
# directory is empty. The gitlink stays in the index and the tree; the
# directory arrives empty and preflight asserts it, before and after the
# suite.
#
# Section 6.4 confound: the humans who wrote this PR had the submodule.
# `make install` populates it, and upstream's own `make test` therefore ran
# integration tests this task's arms never see. Every arm sees the identical
# tree, so this is a statement about external validity, not about fairness
# between arms.
submodules_unneeded: ["sqlglot-integration-tests"]

image:
  pip: ["duckdb", "pytz", "pandas", "python-dateutil"]
  build:
    - "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install -e ."

budget:
  max_turns: 40
  wall_clock_timeout_s: 900

provenance:
  repo: tobymao/sqlglot
  issue: 8225
  pr: 8230
  merge_commit: 91119bcaac977ede6f4a641bdda593b0015ef998
  merged_at: "2026-08-21"
  license: MIT
  session_id: null
  original_model: null
```

`start_sha` is left out for the first gate run to pin, exactly as the probe's
own manifests do. `strip_paths` carries **both** names because `CLAUDE.md` is
a symlink to `AGENTS.md` at this `base_sha` (MU6) and preflight does not catch
a target-only strip.

`prompt` is the one field that needs network: `gh issue view 8225 --repo
tobymao/sqlglot --json title,body`. If it cannot be fetched, use the PR title
plus a one-line restatement and **say so in a comment** — nothing in
`preflight` reads `prompt` (checked: `preflight.py` has no reference to the
field), so the gate result is unaffected and the manifest is not a corpus
entry. Do not fabricate an issue body and present it as verbatim.

`reference.diff`:

```
git -c diff.noprefix=false -c diff.renames=true diff --binary \
  05eed63b281f7ac020045e2b792beef8fad8d3ee \
  91119bcaac977ede6f4a641bdda593b0015ef998 > reference.diff
```

from `~/.cache/bakeoff-probe/clones/sqlglot`. Expect exactly two files
(MU7), which is why `allow_extra_paths` is `[]`.

- [ ] **Step 2: gate it, and require a PASS**

```
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only \
  --force-preflight --task-set ~/.cache/bakeoff-probe/taskset \
  --tasks sqlglot-8225-mysql-key-constraint
```

Required: **PASS**. The suite is ~45 s, so the default
`budget.suite_timeout_s` of 600 has ample headroom over five runs; the
`SUBFAILED` fix (`PREFLIGHT_VERSION` 12) is already in, which sqlglot's
`subTest`-wrapped dialect assertions need. `load_task_set` skips a directory
with no `task.yaml`, so the probe task set's incomplete entries do not block
this even with round-2 item 4 still open.

Record from the written preflight cache entry
(`~/.cache/bakeoff/preflight/sqlglot-8225-mysql-key-constraint.json`):
`evidence["submodules"]` — one entry, `declared_unneeded: true`,
`initialised: false`, `marker: "-"`, `empty: true` —
`evidence["submodules_empty_after_suite"] == {"sqlglot-integration-tests": true}`,
and the `start_sha` the driver pins. Paste all of it into `tasks/todo.md`.

If the gate NO-GOes, the failure is the finding and the plan is wrong; do not
patch the manifest around a harness refusal without saying which refusal and
why.

- [ ] **Step 3: the submission on the real vehicle carries no phantom**

The gate does not produce a submission, so D10 needs its own check against the
same tree. Materialize the task and run the harness's exact sequence:

```
cd bakeoff && .venv/bin/python -c '
from pathlib import Path
from bakeoff.tasks import load_task, materialize
t = load_task(Path.home()/".cache/bakeoff-probe/taskset/sqlglot-8225-mysql-key-constraint")
dest = Path.home()/".cache/bakeoff-probe/scratch-d10/run"
print("start_sha", materialize(t, dest, Path.home()/".cache/bakeoff"))'
```

then, in that tree, with `IDX` a scratch path and `START` the printed sha:

```
GIT_INDEX_FILE=$IDX git read-tree $START
GIT_INDEX_FILE=$IDX git add -A
GIT_INDEX_FILE=$IDX git diff --cached $START | wc -c
```

Required: **0**. Measured on the same repository at `base_sha` before the fix:
**239 bytes**, `deleted file mode 160000  sqlglot-integration-tests`. Run the
unseeded pair (drop the `read-tree`) once beside it and paste both numbers into
`tasks/todo.md` — the contrast is the evidence that this item would have
graded every one of its own runs `SUBMODULE_GITLINK_UNGRADABLE`.

- [ ] **Step 4: the two regression gates**

```
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only \
  --force-preflight --task-set ~/.cache/bakeoff-probe/taskset \
  --tasks tomlkit-514-inline-table-comment-separator
```

Required: PASS, and the driver must report `start_sha
e1d72b883d2e452ca14835047e2fa7db02cdc4d8`. This is D8's tomlkit pin and the
regression for a *needed* submodule under `tests/` — the shape fix 1 was
written for.

```
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only \
  --force-preflight --tasks click-3360-write-usage-empty-args
```

Required: PASS. `start_sha 33575cc0b75608fa5cbcb1d3ae3347b81eac437f`, the
zero-submodule path.

- [ ] **Step 5: the offline suites**

```
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/verify_logger.py
cd bakeoff && .venv/bin/python scripts/mutation_check.py     # SOLO
```

Baseline at round start: unit `1539 passed, 62 deselected`; mutation 160/160;
`verify_logger.py` GATE PASSED.

Expected after this item: unit **1566 passed** (+27 new tests: 10 in Task 1,
4 in Task 2, 2 in Task 3, 8 in Task 4, and Task 5's **3 unit** tests), with
`62 deselected` becoming **66** (Task 5's four `@integration` tests are
deselected by the default `-m 'not integration'`), and mutation **171/171**
(+11). Two version constants move and no others: `PREFLIGHT_VERSION` 13 → 14
and `SCHEMA_VERSION` 3.8.0 → 3.9.0. Three existing tests are *extended* rather than
added (`test_the_click_task_still_loads_and_its_start_sha_has_not_moved`,
`test_the_submodule_keys_are_written_on_the_early_return`,
`test_an_initialised_submodule_at_its_gitlink_is_a_GO`), the four `preflight()`
tests that compare a `submodules` entry by equality move to the six-key shape,
and `test_a_gitlink_with_no_gitmodules_url_is_refused` gets a corrected
docstring and no assertion change -- none of those four groups moves the
count. A count other than 1566/66/171 means something in Tasks 1-6 was not
transcribed; find it before reading a green run as a pass.
`mutation_check.py` runs with nothing else touching the tree.

The **integration** leg above is no longer optional for this item: it is the
only place Task 5's four `@integration` tests run, and they are the ones that
exercise the phantom-deletion fix against a real git.

---

## What this does NOT do

- **It does not relax the reference-diff refusal.** A task whose reference
  diff touches the submodule's content — or its gitlink — is refused with the
  key declared exactly as without it (D4 row 6, pinned by a test and by
  mutation entry 4). The reason is unchanged and is stronger here: `git add
  -A` stages nothing for a gitlink path in any state (MU3), so a fix living
  there produces a zero-byte submission (measured on the seeded index, MU9;
  on the unseeded one it produced the agent's file plus a phantom deletion,
  which is worse and is what D10 removes).
- **It does not relax the `strip_paths` refusal** (D4 row 5, mutation entry 3).
  A strip covering the path still removes the gitlink and moves `start_sha`.
- **It does not relax the grader's gitlink refusal.** A submission that moves
  a gitlink is still `SUBMODULE_GITLINK_UNGRADABLE`, on any task, declared or
  not (D7). `GRADER_VERSION` does not move.
- **It does not populate anything from anywhere.** No mirror, no clone, no
  network read for the declared path. An ssh, relative or `file://` url stays
  refused for a submodule that is **not** declared.
- **It does not prove the submodule is unneeded.** The gate shows the declared
  f2p ids red-before/green-after and the p2p sweep green *with the directory
  empty*; it records no collected-test count, so it cannot tell a suite that
  guards on the submodule's presence from one that silently collects fewer
  tests (D9, MU5). A `TASKS.md` entry records the gap.
- **It does not catch a misspelled KEY NAME.** `load_task` has no top-level
  unknown-key allowlist — the only unknown-key refusals are for `image.*` and
  `grading.*` — so `submodules_uneeded:` parses to nothing, declares nothing,
  and the task stays refused for its url. That failure is loud and names the
  url rather than the key, which is acceptable but is the one silent-nothing
  this change leaves behind; it is also the only route by which a stale
  `PREFLIGHT_VERSION` 13 verdict can exist for a key-declaring manifest (D6).
- **It does not move `start_sha`, `ORACLE_VERSION`, `GRADER_VERSION` or
  `GRADE_SCHEMA_VERSION`.** Two constants move: `PREFLIGHT_VERSION` 13 → 14
  (D6) and `SCHEMA_VERSION` 3.8.0 → 3.9.0 (D10). The second was "does not
  move" in the first two drafts of D10, on a byte-identity measurement that
  did not cover the tracked-but-ignored shape; `eemeli/yaml` carries fifteen
  of them.
- **It does not add a `RunRecord` field.** `Versions.container_image_digest`
  pins the image layer, and `task_id` + `task_set_commit` name the manifest
  revision the declaration lives in; a copy in the record would be
  configuration reported as observation.
- **It does not close the nested-submodule or relative-url deferrals.** Both
  keep their `TASKS.md` entries.
- **It does not touch `HANDOFF.md` or the pruned-mirror cache.** The change
  removes a caller of `ensure_pruned_mirror`; it adds none.
- **It does not make an agent's writes inside the empty directory visible at
  run time.** Measured post-seed (MU9 row G′): **0 bytes**, names `[]`, while
  `ls -A` sees the file. The submission diff cannot carry it and neither can a
  checkpoint — git does not descend into a gitlink path and the seeded index
  does not either. Capturing that state is round-2 **item 17**'s job
  (`docs/superpowers/plans/2026-09-03-round2-17-submodule-dirty-capture.md`):
  an `ls -A` over uninitialised gitlink directories, recorded in
  `submodules_dirty` and refused by the grader. This plan deliberately does
  not duplicate it. Within this item the only enforcement is preflight's two
  `ls -A` reads (D6), which refuse the task rather than scoring it. **If item
  17 does not land**, Task 6 Step 5 files a `TASKS.md` line naming the gap;
  if it does, the line is redundant and is not written.
- **It does not need to re-grade anything.** Whether a stored record carries
  either phantom is now **answered, not filed**: all ten event logs under
  `~/.cache/bakeoff` hold `click-3360-write-usage-empty-args` records only,
  and click has 0 tracked-but-ignored files at its `base_sha` and no gitlink.
  `SCHEMA_VERSION` moves to keep *future* records readable across the change,
  not to annotate past ones.

---

## Open questions and rulings

The first draft carried these as a message to the coordinator and not in the
document, which is review 1's finding 1. Written down here with the ruling
adopted; findings 2, 5, 7, 8 and 14 are the reviewer's answers to four of them
plus one the review raised itself.

**Q1. The tomlkit `start_sha` pin cannot be a unit test — is a verification
step enough, or should the manifest be copied into `bakeoff/taskset/`?**
*Ruling: verification step; do not copy.* No reviewer objection was raised, and
copying a probe manifest into the shipped task set would make `task_set_commit`
name a corpus that includes a task nobody has reviewed for §3.7 compliance —
a bigger change than this item, made as a side effect of wanting a pin. The
click pin covers the zero-submodule path as a unit test, D8's
two-materialization test covers the property directly, and Task 7 Step 3 pins
`e1d72b883d2e452ca14835047e2fa7db02cdc4d8` by re-gating. **Still open** for a
later item: whether the probe corpus should be promoted wholesale.

**Q2. Is the post-suite emptiness read in scope, given the brief asked only
for the start-state assertion?** *Ruling: yes, and it must not collapse two
absences* (finding 7). It is the only enforcement left for HARVESTING's
existing "a suite that writes inside the submodule is out" rule once the
submodule is uninitialised (MU3), and Task 6 Step 3 corrects the bullet that
claims otherwise. The evidence key is a **mapping**, `{path: bool | None}`,
not a list.

**Q3. Is `empty` measured for every submodule or only declared ones?**
*Ruling: every submodule.* Finding 7 endorses the principle by invoking it
against the first draft's own post-suite list: a `None` that means both "the
read failed" and "the read was not attempted" is the "a null says which kind
of null it is" invariant broken. Cost is one exec per submodule, four at most
in the screened corpus.

**Q4. The verification manifest's `prompt` needs network — acceptable to use a
labelled stand-in?** *Ruling: yes, labelled, never fabricated as verbatim.*
`preflight` never reads `prompt` (checked), the manifest is a verification
vehicle written outside `bakeoff/taskset/`, and Task 7 Step 1 says so. What
review 1 *did* rule on for that manifest is finding 12: it must carry the §6.4
confound comment, which the first draft omitted.

**Q5. Should `_parse_submodule_status` take the manifest?** *Ruling: no*
(finding 5). Its docstring states the opposite principle — *"This is an
OBSERVATION, not a restatement of the manifest"* — and filling the field there
would also move that function's own pure unit tests, which are not about the
manifest. Both new fields are set in the caller, in one pass.

**Q6 (raised by finding 14). Should the p2p sweep's collected-test count be
recorded so a reader can see a shrinking suite?** *Ruling: not here.* It is a
runner-adapter change across pytest, vitest and jest, and folding it into a
manifest-key item would make this commit two things. `TASKS.md` gets the entry
(Task 6 Step 5) so the gap is filed rather than re-derived as a bug later.

---

## Reviews → changes

### Review 1

| # | finding | change |
|---|---|---|
| 1 | no open-questions section | Added *Open questions and rulings* with all five, plus Q6 from finding 14, each with its ruling. |
| 2 | **BLOCKER** — sixth refusal (unreadable `.gitmodules`, exit 128) uncounted; a named test cannot pass | Adopted the reviewer's first option. D4's table now has **six rows** and names where each lives; rows 1 and 2 are exempted through one derived `needed_gitlinks` set (Task 1 Step 5b). MU8 records the measurement. The unpassable test is replaced by `test_a_declared_unneeded_gitlink_survives_an_unreadable_gitmodules` (the exit-128 shape) plus `test_a_declared_unneeded_gitlink_with_no_stanza_in_a_readable_gitmodules` (the narrower shape the `by_path.get` fallback was justified for). The mislabelling of `test_a_gitlink_with_no_gitmodules_url_is_refused` is corrected in the test table. |
| 3 | typo refusal placement is a range, not a line | D3 now says **immediately after the `gitlinks` dict, before the `if gitlinks or _has_gitmodules(...)` block**, with the reason. Two ordering tests pin it: it beats the url refusal, and it beats the unreadable-`.gitmodules` refusal. |
| 4 | `preflight` must use `getattr` | D6 and Task 4 Step 1 read `unneeded = frozenset(getattr(task, "submodules_unneeded", ()))` once, above the block, quoting the rule the strip check states. |
| 5 | do not give `_parse_submodule_status` the manifest | Adopted. That function is untouched and listed under "Not modified, deliberately"; both fields are filled in the caller in one pass (Task 4 Step 1), and the plan notes its pure unit tests do not move. |
| 6 | `test_images.py::_sub_task_stub` breaks three tests | Task 3 Step 2 now fixes the stub **first** (`submodules_unneeded = ()` plus a keyword), and explains why `derive_submodules` must not defend with `getattr` against its own dataclass field. File Structure says so. |
| 7 | `submodules_populated_after_suite` collapses two absences | Renamed `submodules_empty_after_suite` and made a **mapping** `{path: bool \| None}`; `{}` vs `None` at the top level keep their meanings. The first draft's "the failure mode is identical" note is deleted; a new test, `test_an_unreadable_directory_after_the_suite_is_not_reported_as_content`, pins the distinction. |
| 8 | `PREFLIGHT_VERSION` reasoning inverted | D6 and Task 4 Step 5 lead with the evidence-shape change and demote the cached-verdict case to a residual, reachable only via the missing top-level key allowlist (cross-linked to finding 17). |
| 9 | the item's load-bearing lines unanchored | Mutation entries go from 4 to **10**: added the `declared_unneeded` guard, the *placement* of both kept refusals (entries 3 and 4, expressed as valid one-line edits), the `images.py` `continue`, the `needed_gitlinks` exemption, and the post-suite read. Expected delta stated as +10 in what is now Task 7 Step 5; review 3 took it to +11. |
| 10 | missing `"not integration"` marker | Stated once for all ten, with the tuple shape spelled out. |
| 11 | a problem message names an unreachable cause | The image parenthetical is gone; the message now says the run tree is bind-mounted over the image's `/repo`, so only a post-materialization host write can produce the state. |
| 12 | §6.4 note missing from the two places an author reads | The Task 7 manifest carries the confound comment verbatim, and Task 6 Step 4 requires it in both rewritten `BUILDING-A-TASK-SET.md` rows. D9 now lists all four recording sites. |
| 13 | HARVESTING asserts an enforcement MU3 disproves | Task 6 Step 3 adds an edit: correct the existing "a suite that writes inside the submodule is out" bullet to say the ` M <path>` signal exists only for a **populated** submodule, and that the declared-unneeded case is enforced by preflight's `ls -A` reads. |
| 14 | "proves the submodule is unneeded" over-claims | Narrowed at all three sites (Architecture, D6 intro, the click block) to D9's wording, and D9 is rewritten to say what the gate can and cannot show, with MU5 carrying the reason. The optional collected-count evidence is declined with a reason and filed in `TASKS.md` (Q6). |
| 15 | Task 3's second test is over-broad | Now `[p for p in repo.rglob(".git")] == []` plus `(repo / ".gitmodules").exists()`, matching the house form. |
| 16 | tests never say how the key reaches a fixture manifest | Task 1 Step 7 states the `extra_yaml=` form once, with the two-key concatenation, and notes `_manifest` must not gain a keyword. |
| 17 | a misspelled key name is silently ignored | New bullet in *What this does NOT do*, cross-linked from D6 as the route that makes the stale-verdict residual reachable at all. |

### Review 2

| # | finding | change |
|---|---|---|
| 18 | mutation entry 5's anchor `    if unknown:` is not unique in `tasks.py` (two hits, both in `load_task`, and `mutation_check` replaces the FIRST) | Entry 5 now anchors on two lines, `unknown = sorted(set(task.submodules_unneeded) - set(gitlinks))` plus `    if unknown:`. A paragraph under the table records the measurement (two occurrences, `original.replace(find, replace, 1)`) and the consequence it avoids: the mutation would have landed on the `image.*` unknown-key refusal, the typo refusal would have survived, and the entry would be reported MISSED rather than caught. |
| 19 | mutation entry 8 was the only one of ten described in prose | Written out verbatim, both `old` and `new`, with the continuation line. The same paragraph notes why it is the one that most needed it: its `old` string does not exist until Task 4 lands, so an implementer would have had to reconstruct it. |
| 20 | `upstream_two_submodules` used in Task 1, built in Task 2 | The construction moved into Task 1 Step 7, where its first consumer is, with both variants specified through one `stanzas: int = 2` keyword (`stanzas=1` is the readable-but-no-stanza tree). Task 2 Step 2 is retitled "the tests" and refers back. The File Structure row drops the "(Tasks 1, 2)" hedge. |

### Cross-item blocker, from `plan-17-review-1.md` finding 1

| # | finding | change |
|---|---|---|
| C1 | **BLOCKER** — `snapshot_diff` stages into a scratch index (`GIT_INDEX_FILE=SNAPSHOT_INDEX`), and against it an uninitialised gitlink is a `deleted file mode 160000` chunk on a clean tree; MU3 measured the repository's own index instead | Re-measured with the harness's own sequence and recorded as **MU9**: 199 bytes on the fixture, **239 bytes on `tobymao/sqlglot` at this plan's own verification `base_sha`**, stable across repeated calls, and 326 bytes when the agent also wrote a file into the directory. MU3 is corrected in place — its `git add -A` lines are relabelled "against the repository's own index", and the false conclusion it licensed ("such content can never reach a checkpoint diff or the §5.6 submission") is struck and replaced by a pointer to MU9. |
| C2 | consequence: once item 2 lands, every run of a declared-unneeded task is refused `SUBMODULE_GITLINK_UNGRADABLE` and leaves the denominator silently | Fixed by **D10** and **Task 5**: one line, `self.checked_exec(["git", "read-tree", base_sha], env=env)` before the staging loop. Measured 0 bytes on both fixtures after it. Seven tests (three unit, four `@integration`) and mutation entry **11**. |
| C3 | D7 argued `_gitlinks_touched` "still fires" and treated that as correct, not knowing it fires unconditionally | D7 now states that the bullet is true **only once D10 lands**, names what the unfixed behaviour would be, and cites the post-fix measurement that a real rogue `git init` + commit still emits `index <a>..<b> 160000` and is still refused. |
| C4 | the alternative fix (`update-index --add --cacheinfo`) | Refused **by measurement**, recorded in D10: it fails with `'vendor/libdep' appears as both a file and as a directory` exactly when the agent wrote into the directory, in a function that must be total, and it would need the submodule set threaded into a class that has no manifest. |
| C5 | (found while measuring) a tracked file matching `.gitignore` was reported DELETED in every submission and checkpoint | A pre-existing phantom from the same empty index, removed by the same line, measured (460 → 135 bytes) and pinned by its own integration test. Named in D10 and in *What this does NOT do*, with the "has any stored record got one?" question filed rather than answered. |

Counts moved with it: mutation **170 → 171**, unit **1563 → 1566 passed**,
deselected **62 → 66**, and the plan gained Task 5 (old Tasks 5 and 6 became
6 and 7).

### Review 3 → changes

| # | finding | change |
|---|---|---|
| 21 | the `SCHEMA_VERSION` decision rested on a byte-identity measurement taken over an *ignored untracked* file, which does not cover the phantom's actual shape — a file **tracked** at `base_sha` that matches `.gitignore` | **`SCHEMA_VERSION` now moves**, 3.8.0 → 3.9.0, with its own Task 5 step and a `#:` paragraph naming the measurement. Re-measured independently: `eemeli/yaml` **15** (`.editorconfig`, `.github/workflows/*`, and `.gitignore` and `.gitmodules` themselves), `bidict` **1** (`.coveragerc`), sqlglot/tomlkit/pytest/werkzeug/click **0**. Both of those are round 2's own verification vehicles, so a pre-fix and a post-fix record of yaml-474 assert different deletions with nothing but `harness_commit` to tell them apart — an existing field changing what it asserts, which is stronger than the additive case `CLAUDE.md` already says must move the constant. The reviewer's second-order point is written in beside it: the grader *applies* `final_diff` at `start_sha`, so a pre-fix yaml run really did delete those fifteen files in the grading tree. |
| 22 | the four integration tests cannot all use `git_container` — it builds one fixed tree | Task 5 gains **Step 4**, a `git_container_factory` fixture taking a build callback. Only `test_a_repo_with_no_submodules_snapshots_byte_identically` uses `git_container` unchanged, and its unseeded control is now specified: raw `container.exec` with `GIT_INDEX_FILE` set, because `snapshot_diff` always seeds after this change and so cannot produce its own reference. The contradictory "`-c protocol.file.allow=always`" sentence is gone: measured, the gitlink is **fabricated** with `git update-index --add --cacheinfo 160000,<40 hex>,vendor/libdep` plus an empty directory — no second repository, no file transport — and the fixture's caveat (a fabricated sha makes `git submodule status` exit non-zero, so it must not be reused for a preflight test) is written into its docstring. |
| 23 | the unit harness is misnamed, the count is inconsistent, and one test cannot work through it | Three fixes. The count is stated as **three unit + four integration = seven** (the table was right, the prose said "two"). `_FakeContainer` is replaced by a new `_SnapshotCalls` recording stub — `exec_run(cmd, **_kw)` discards `environment=` and records nothing, so neither the order nor the env question is observable through it. And `_container_with`'s `checked_exec` lambda is **deleted** rather than patched: the real `RunContainer.checked_exec` calls the already-stubbed `self.exec` and raises on non-zero, so the raise test exercises the production check; the two existing users return exit 0 for every non-`add` command, so it is a no-op for them. |
| 24 | "seeded from `base_sha`'s tree" invites the wrong reading — the parameter holds `start_sha` at run time | The docstring now says the seed and the diff read the **same parameter** so they cannot disagree, names the `matrix` → `runner` → `checkpoints` path that puts `start_sha` in it, and explains why the committed test half does not appear as additions (`add -A` reconciles every worktree-visible path; the seed survives only where git is blind). Measured 0 bytes either way. |
| 25 | `test_snapshot_diff_raises_when_git_fails` now fails at a different command | Named in Task 5 Step 1 and again in Step 5's closing note: the assertion still passes on `match="git"`, but its docstring describes the add/diff path and is updated to say the seed is now the first command that touches git. |
| — | **G′, from the coordinator's cross-item note.** After the seed, an agent writing a file *into* the declared-unneeded directory diffs to **0 bytes** | Added as row **G′** of MU9's table and as D10's last property, stated plainly: the submission diff cannot carry it and neither can a checkpoint. **Item 17 owns its capture** (`plans/2026-09-03-round2-17-submodule-dirty-capture.md`, an `ls -A` over uninitialised gitlink dirs into `submodules_dirty`, refused by the grader), cross-referenced by plan path in D10 and in *What this does NOT do*. Task 6 Step 5's `TASKS.md` line is **conditional**: written only if item 17 does not land. |

**Disputed: none.** Every finding across all four review rounds is adopted. Two of
review 1's are adopted with a narrowing that is recorded rather than silent:
finding 2 takes the exemption option
rather than the "stays refused" option, argued in D4's row 1 (a tree whose
only gitlinks are declared is fully workable with no `.gitmodules` at all);
and finding 14's *optional* second half — recording a collected-test count —
is declined as a runner-adapter change and filed in `TASKS.md`, while its
required half (stop calling it a proof) is adopted in full.

---

## Sentences for `CLAUDE.md`, to be applied off this branch

Per the branch rule, `CLAUDE.md` is not edited here. The two sentences that
belong in it:

- Under the submodule material: *"A submodule the task's suite never reads can
  be declared `submodules_unneeded` in the manifest — the gitlink stays in the
  index and the tree, the directory is never populated, its url scheme is
  never checked, a readable `.gitmodules` is not required for it, and no
  mirror is built. It relaxes four of the six submodule refusals and neither
  of the two that protect grading: a strip covering the path, and a reference
  diff touching it, are refused with the key exactly as without it."*
- Beside the "Silence is the enemy" bullet: *"git does not descend into a
  gitlink path in any state. Measured 2026-09-02: a file inside an
  uninitialised submodule directory is invisible to `git status --porcelain`
  (with or without `-uall`) and to `git ls-files -o`. So the clean-tree check
  cannot see a suite that writes in there, and preflight's `ls -A` is the only
  reader that can."*
- A third, and it is the one that cost a whole review round: *"`snapshot_diff`
  stages into a SCRATCH index, and a scratch index starts empty — so `git add
  -A` builds it from a worktree scan that cannot see a gitlink, and an
  UNINITIALISED submodule reads as `deleted file mode 160000` against
  `base_sha`. Measured 2026-09-02: 239 bytes on `tobymao/sqlglot`, clean tree,
  agent did nothing; `grader._GITLINK_MODE` matches it, so every run of a
  `submodules_unneeded` task would grade SUBMODULE_GITLINK_UNGRADABLE. The
  same empty index reported a tracked-but-gitignored file as deleted.
  `git read-tree <base_sha>` into that index before staging is the fix, and a
  measurement of `git add -A` against the repository's own index is not a
  measurement of this path."*
