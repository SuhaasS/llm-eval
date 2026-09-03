# Round 2, item 17 — the invisible in-submodule edit: record it, then refuse to grade it

**Revision 4** (reviews 1–3 folded; see the three *Review N → changes*
sections at the end).

**Sequenced after item 2** (`submodules_unneeded`) — and now with a hard
dependency on it, not just an ordering preference. Review 1 measured that
`container.snapshot_diff` emits a **199-byte `deleted file mode 160000` chunk
on a clean tree** for an uninitialised gitlink, because it stages into a
scratch `GIT_INDEX_FILE`. **Plan 2 owns the fix in `snapshot_diff`**; this plan
states it as a precondition in §0 and is written to be correct on both sides of
it.

Nothing here blocks on item 9 (the pure-gitlink *rename*), which touches
`_chunk_is_gitlink` — a different authority in the same file; see *What this
does NOT do*.

**One commit:** this plan + code + docs.

---

## 0. Preconditions

1. **Item 2 has landed, including its `snapshot_diff` fix.** Check first:
   `grep -n "submodules_unneeded" bakeoff/src/bakeoff/tasks.py` matches, and
   `snapshot_diff` no longer stages a bare gitlink deletion for an
   uninitialised, declared-unneeded submodule. If either is missing, **STOP and
   report** — D9 and Task 9 are written against the post-fix behaviour, and the
   pre-fix behaviour is a live cross-item defect, not this item's to close.
2. **Read the three version constants off disk before editing them, by grep
   and never by line number** — the tree churns underneath this item, and a
   line number is exactly the thing that goes stale. Measured during review 2:
   `GRADER_VERSION` moved from line 247 to line 258 mid-review, so revision 2's
   own citation already resolved to prose inside the history comment.

   ```bash
   grep -n '^GRADER_VERSION' bakeoff/src/bakeoff/grader.py
   grep -n '^GRADE_SCHEMA_VERSION' bakeoff/src/bakeoff/grade_schema.py
   grep -n '^SCHEMA_VERSION' bakeoff/src/bakeoff/schema.py
   grep -rn 'GRADE_SCHEMA_VERSION ==\|GRADER_VERSION ==' bakeoff/tests/
   ```

   Items 1, 8 and 9 each move at least one of them ahead of this item. **Do not
   transcribe a number from this plan for the first two.** See D6/D7/D10.
3. `.venv/bin/python -m pytest tests/ -v` is green at the starting revision.

---

## 1. The measured defect

`TASKS.md`, bullet **"A pure-gitlink submission is refused; a pure-gitlink
*edit* is invisible"** (measured 2026-09-01, broadening 6, M8):

> The grader catches the agent who COMMITS inside a submodule
> (`SUBMODULE_GITLINK_UNGRADABLE`, read out of the submission's own chunks).
> It cannot catch the agent who edits and does not commit: `git add -A` stages
> nothing for a submodule, so the submission diff is **zero bytes** and the
> ladder stops at `EMPTY_PATCH` — a `GradeFailure` that stamps
> `resolved: False`. That is byte-identical to an honest empty run […] and no
> field distinguishes them, because the harness never observed the edit.

`EMPTY_PATCH` is a `GradeFailure`, so the row sits in the denominator as a
model failure. The claim is an accusation — "this model changed nothing" — over
a limitation of the harness's own capture, and the event log is append-only.

---

## 2. Measurements

2026-09-02, git 2.50.1 (Apple Git-155). Scratch superproject built to match
`materialize`'s sequence (`clone --local --no-checkout` →
`checkout --detach <base>` → `remote remove origin` → `clean -xfd`), then
`submodule update --init vendor/libdep` from a local mirror.
`base = 16a96502442080b63fd627af0fea5fc6812069f2`,
gitlink `= e4d0af7db0f07da573818471855cf4bfe494f288`.

**Every diff column below is `container.snapshot_diff`'s actual command
sequence** — `GIT_INDEX_FILE=<scratch> git add -A` then
`GIT_INDEX_FILE=<scratch> git diff --cached <base>` — not a plain `git add -A`
against the repository's own index. Revision 1 of this plan inherited row G
from item 2's MU3, which used the repository index, and got it wrong by 199
bytes; rows A–F were always taken against the scratch index and are unchanged.

### M1 — the seven states, under the two candidate readers

| # | run-tree state | `git submodule status` | `git status --porcelain` | `--porcelain=v2 --ignore-submodules=none` | `snapshot_diff` (scratch index) |
|---|---|---|---|---|---|
| A | initialised, clean | `␣<sha> vendor/libdep (heads/main)` | *(empty)* | *(empty)* | **0 bytes** |
| B | tracked file edited inside, **not committed** | `␣<sha> …` | ` M vendor/libdep` | `1 .M `**`S.M.`**` … vendor/libdep` | **0 bytes** |
| C | untracked file created inside | `␣<sha> …` | ` M vendor/libdep` | `1 .M `**`S..U`**` …` | **0 bytes** |
| D | committed inside (HEAD moved) | `+0cf80365… (heads/main-1-g0cf8036)` | ` M vendor/libdep` | `1 .M `**`SC..`**` …` | **245 bytes** — `index e4d0af7..0cf8036 160000` + `Subproject commit` |
| E | D **and** B together | `+0cf80365… …` | ` M vendor/libdep` | `1 .M `**`SCM.`**` …` | 245 bytes |
| F | `rm -f vendor/libdep/lib.txt` (tracked content destroyed) | `␣<sha> …` | ` M vendor/libdep` | `1 .M `**`S.M.`**` …` | **0 bytes** |
| G | **uninitialised** gitlink, empty directory, agent did nothing | `-<sha> vendor/libdep` | *(empty)* | *(empty)* | pre-seed **199 bytes** (`deleted file mode 160000`) → post-seed **0** |
| G′ | uninitialised gitlink, agent wrote `vendor/libdep/agent_wrote_this.py` | `-<sha> …` | *(empty)* | *(empty)* | pre-seed **410 bytes** (the G deletion **plus** `new file mode 100644 …`) → post-seed **0** |

**Rows A–F are byte-identical across item 2's seed**, and so is an
ordinary modify+add+delete with no submodule involved (391 bytes either way).
That is what makes §0's precondition cheap: the whole initialised half of this
table, D8's predicate, Task 7's `diff_vs_base == ""` assertion and M6 all
survive it untouched. **Only rows G and G′ move**, and they move to zero.

`pre-seed` is today's `snapshot_diff` — `git add -A` into an EMPTY scratch
index, so a gitlink the working tree cannot supply is staged as a deletion.
`post-seed` is with plan 2 D10's `git read-tree <base_sha>` inserted before the
staging loop, which is what §0 precondition 1 mandates. Measured on both, 2026-09-02:

```
G   uninitialised, clean      today: add exit 0, 199 bytes   seeded: add exit 0, 0 bytes
G′  uninitialised + a file    today: add exit 0, 410 bytes   seeded: add exit 0, 0 bytes
```

Row G reproduced pre-seed under each variable that could have explained it:
with `submodule.<name>.url` registered in `.git/config`; with the directory
removed entirely; against a *persisted* scratch index on a second call.

**Row G′ post-seed is why this plan grew a second reader.** `git add -A` does
not descend through a gitlink boundary once the index carries the gitlink, so
after the seed an agent's file inside an uninitialised submodule directory is
in the diff (0 bytes), in the v2 stream (no record) and in `git status`
(nothing) — **recorded nowhere at all**. Revision 2 was going to file that as a
`TASKS.md` residual. It is closed here instead, by M7's filesystem reader.

**The reader choice.** `git submodule status` reports a **space** marker for B,
C and F — it compares the submodule's HEAD against the index gitlink and says
nothing about the submodule's working tree. It is blind to every state this
item exists for. Its `+` fires only on D/E, which is exactly the state
`git add -A` already stages and `grader._gitlinks_touched` already refuses.
`git status --porcelain` (v1) sees B–F but collapses them all into ` M`.
`--porcelain=v2`'s third field is the one that separates them: `S<c><m><u>`,
where `c` is the moved gitlink, `m` modified tracked content, `u` untracked
content. `N...` on an ordinary file.

### M2 — every token of the argv is load-bearing

**`--no-optional-locks`.** `git status` **rewrites the index** — and the
submodule's index too. `.git/index` and `.git/modules/vendor/libdep/index`
were both stamped to `1577865600` (2020-01-01) and the command re-run:

```
after --no-optional-locks:  super=1577865600  sub=1577865600   (untouched)
after plain status:         super=1788384510  sub=1788384510   (both rewritten)
```

Review 1 additionally measured the **full** `force_capture` sequence — scratch
`git add -A`, two `git diff --cached`, then this status — and both indexes are
still at `1577865600` afterwards. That is what Task 7's assertion rests on.

`container.snapshot_diff`'s own comment pins the rule this protects: *"Nothing
here writes to .git/index at all."* Checkpoints are taken from inside the
agent's stdout loop, concurrently with the agent's own git.

**`--ignore-submodules=none`.** Three config sites silence the entry
completely without it — measured, each in isolation, in state E:

| config | v2 without the flag | v2 with the flag |
|---|---|---|
| `submodule.<name>.ignore=all` in `.git/config` | *(empty)* | `1 .M SCM. … vendor/libdep` |
| `diff.ignoreSubmodules=all` in `.git/config` | *(empty)* | `1 .M SCM. … vendor/libdep` |
| `submodule.<name>.ignore=all` in **`.gitmodules`** | *(empty)* | `1 .M SCM. … vendor/libdep` |

The third is repository-authored: it ships in the upstream tree the task is cut
from. Without the flag the refusal is silently disarmed by a file the task
author never wrote.

**`-z`.** The v1/v2 path field is C-quoted under `core.quotePath` and
space-delimited otherwise. `-z` makes it a NUL-terminated byte string, which is
the same rule `preflight._gitlink_paths` and `tasks._numstat` already follow
(*paths come from git, never from a regex over a display line*).

### M3 — the `-z` record shapes the parser has to consume

```
1 .M S.M. 160000 160000 160000 <h> <h> vendor/libdep|
1 .M N... 100644 100644 100644 <h> <h> calc.py|
2 R. N... 100644 100644 100644 <h> <h> R100 calc2.py|calc.py|
? src/|
? a dir/|
```

(`|` = NUL; the stream ends with a trailing empty item after the final NUL.) A
`2` (rename/copy) record occupies **two** NUL items — the new path, then the
original. Type `u` (unmerged) is one item with three mode and three hash
columns; `?` and `!` are one item each.

The sub-state is field index **2** on both `1` and `2` records, and neither is
affected by spaces in the path, because `split(" ", 8)` (`1`) and
`split(" ", 9)` (`2`) stop before the path.

### M3b — a repository path can forge a record, and the rename slot is where it lands

New in revision 2, and it is what makes the two-item consumption load-bearing
rather than defensive. A tracked file may be **named** so that it reads as a
`1 ` submodule record. Measured: a file named

```
1 .M S.M. 160000 160000 160000 aaaaaaa bbbbbbb sneakysub
```

renamed to `ordinary.py` produces

```
2 R. N... 100644 100644 100644 <h> <h> R100 ordinary.py|1 .M S.M. 160000 160000 160000 aaaaaaa bbbbbbb sneakysub|
```

A parser that advances by one after a `2` record reads that second item at the
top of the loop, matches `"1 "`, and files `{"sneakysub": "S.M."}` — a
submodule state fabricated out of a **filename**, on a repository with no
submodule at that path. Same class as `_GITLINK_MODE`'s rule that *content
never reaches column zero of a header*: here the second NUL item is content,
and consuming it correctly is what keeps it out of column zero.

Also measured in the same shape: with the forged name as the **new** path,
`split(" ", 9)[9]` returns it whole — the field split is space-**count**-based
and is not fooled by spaces in a path.

### M4 — an agent-created embedded repository is not this shape

`git init` in a tracked subdirectory (`src/vendored`), the third shape
`grader._gitlinks_touched`'s docstring records:

```
git status --porcelain=v2 -z  ->  "? src/"
```

Not an `S` record — so the new field does **not** see it, and does not need to:
`git add -A` stages it as `new file mode 160000`, the diff carries it, and
`_gitlinks_touched` already refuses it. The two authorities stay disjoint.

### M5 — cost

100 iterations, same tree, warm:

```
git submodule status                                          9.076 s  (~91 ms each)
git --no-optional-locks status --porcelain=v2 \
    --ignore-submodules=none                                  1.382 s  (~14 ms each)
```

The command this plan adds is ~6.5× cheaper than the one it rejected **and**
strictly more informative. (Review 1 reproduced 71 ms / 13 ms, 5.5×, under
different load — same result.) Marginal cost per checkpoint is ~14 ms beside a
`git add -A` and two `git diff --cached` that already run.

### M6 — the refusal does not fire on the corpus' one real submodule task

Taken by review 1 and transcribed here, because §6 cannot cover it and the
question — *does this refusal false-positive on a task whose suite touches its
submodule?* — is the one that would make it worse than the defect.

`tomlkit-514-inline-table-comment-separator` from
`~/.cache/bakeoff-probe/taskset`, materialized at
`start_sha e1d72b883d2e452ca14835047e2fa7db02cdc4d8`, the manifest's own runner
(`python -m pytest -q -p no:cacheprovider`) run inside
`bakeoff-task-tomlkit-514-inline-table-comment-separator:v1` to the expected
red-before state (1 failed, 1002 passed), then the tree read:

```
v2 --ignore-submodules=none -z              -> (empty)
git -C tests/toml-test status --porcelain   -> (empty)
scratch-index add -A + diff --cached        -> 0 bytes
```

Safe on the one real submodule task in the corpus. The upstream screen is
already load-bearing and stays so: `HARVESTING.md`'s *"A suite that writes
inside the submodule is out"* is enforced today by preflight's clean-tree NO-GO
on ` M <path>`, which is the **same** signal (M1 rows B/C, v1 column) this
refusal reads at a finer resolution.


### M7 — the second reader: content in an uninitialised submodule directory

New in revision 3, and it exists because of row G′ post-seed: after item 2's
seed that content is in neither the diff nor the v2 stream. git will not report
it — the path is a gitlink boundary git does not descend through — so the only
authority left is the filesystem, and the read has to be narrow enough that it
cannot be confused with git's own verdict.

The probe, measured verbatim on 2026-09-02 (git 2.50.1, `/bin/sh`), given the
`160000` paths from `git ls-files -s -z`:

| # | tree state | `.git` | directory | probe output | v2 output | `snapshot_diff` |
|---|---|---|---|---|---|---|
| 1 | uninitialised, agent wrote `vendor/libdep/agent_wrote_this.py` | no | non-empty | `vendor/libdep\0` | *(empty)* | 0 post-seed |
| 2 | uninitialised, empty directory | no | present, empty | *(empty)* | *(empty)* | 0 post-seed |
| 3 | initialised, clean | **yes** | non-empty | *(empty)* | *(empty)* | 0 |
| 4 | initialised, tracked file edited | **yes** | non-empty | *(empty)* | `1 .M S.M. … vendor/libdep` | 0 |
| 5 | **gitlink directory removed entirely** | no | **absent** | *(empty)* | **`1 .D S... … vendor/libdep`** | **199 bytes per gitlink, seed-invariant** |

Exit 0 in all five.

**Disjointness holds, and the mechanism is not the `.git` test.** Revision 3
said *"the probe skips every path with a `.git`, which is exactly the set v2 can
report on, and v2 emits nothing for a path without one"*. The second clause is
false and row 5 is the counter-example: `vendor/libdep` has no `.git` there and
v2 reports it (`1 .D S...`). The measured rule is a **directory** test, not a
`.git` test:

> On a path with no `.git`, v2 fires only when the directory is **absent**
> (row 5) and the probe fires only when it is **non-empty** (row 1). Absent and
> non-empty are mutually exclusive, so no path can be filed twice.

The `.git` test is still worth having — it is what makes the probe **cheap**,
skipping every initialised path without a `find` — but it is not what makes the
readers disjoint. Rows 3 and 4 are the other half: a path with a `.git` is
never probed, and rows 1–2 show v2 silent wherever the probe can speak.

**Row 5 is a third disjoint case and belongs to neither reader's refusal.**
`_gitlinks_touched` takes it: `snapshot_diff` carries a `deleted file mode
160000` chunk for the removed path — 199 bytes for the one gitlink on this
fixture, 402 on review 3's two-gitlink one, i.e. one such chunk per path — and
that is **identical seeded and unseeded**, unlike row G's deletion which the
seed removes. The mechanism is the same directory test: with the gitlink in the
seeded index, `git add -A` leaves an empty-but-present directory alone (row 2 →
0 bytes) and records a deletion for an absent one (row 5 → 199). So `S...`
never needs to reach `_submodule_edits`, and D8's predicate correctly declines
it — see D8 and R3-2.

A gitlink path containing a space (`vendor/lib dep`) is passed through `"$@"`
and comes back whole — verified.

**Cost.** 100 iterations each, on the scratch tree, and 50 on this repository's
own 154-file tree:

```
git ls-files -s -z                    scratch  7 ms      this repo  7 ms
the probe (one `sh -c`, one gitlink)  scratch  6 ms
git --no-optional-locks status …      scratch  8 ms      this repo  9 ms
```

(Review 3 re-measured 6.3 / 6.0 / 7.0 ms on its own fixture, and ran the probe
inside the real eval image under `dash`: `printf '%s\0'` emits a true NUL,
`find -mindepth 1 -maxdepth 1 -print -quit` is supported, a missing directory is
skipped silently through the discarded stderr, and a spaced path comes back
whole.)

So a checkpoint's submodule capture is at most three execs and ~21 ms — still
under a quarter of the ~91 ms `git submodule status` costs for an answer that
sees none of this. **On a repository with no gitlinks the probe does not run at
all**: `ls-files` returns nothing and `_uninitialised_with_content` short
-circuits before the exec, so every task in today's corpus except
`tomlkit-514…` pays one extra `ls-files` and nothing more.

---

## 3. Design

### D1. Two readers, because no single one sees both halves

An **initialised** submodule's dirt is visible to git and invisible to the diff
(M1 rows B/C/F). An **uninitialised** one's content is invisible to git *and*,
after item 2's seed, invisible to the diff too (M1 row G′, M7). One command
cannot answer both, so `submodule_states` runs two and merges them. They are
measured disjoint, and the rule is a **directory** test rather than a `.git`
test: on a path with no `.git`, v2 fires only when the directory is absent and
the probe only when it is non-empty (M7). The `.git` test is what keeps the
probe cheap, not what keeps the two apart.

Three module constants in `container.py`, immediately above `_parse_status_v2`:

```python
#: The submodule-state read, word for word. Every token is load-bearing and
#: each was measured on 2026-09-02 (git 2.50.1); see `submodule_states`.
_SUBMODULE_STATUS_ARGV = [
    "git", "--no-optional-locks", "status",
    "--porcelain=v2", "--ignore-submodules=none", "-z",
]

#: The gitlink path set, same rule and same reason as
#: `preflight._gitlink_paths`: paths come from git, NUL-delimited, never from
#: a display line. It is also the short-circuit -- no gitlinks, no probe.
_GITLINK_ARGV = ["git", "ls-files", "-s", "-z"]

#: Which declared gitlink directories hold content, for the paths passed as
#: "$@". POSIX sh, no bashisms, every branch measured 2026-09-02 (M7).
#:
#: `[ -e "$p/.git" ]` is the INITIALISED test: a modern submodule has a `.git`
#: FILE there and an older one a directory, so `-e` covers both. It also
#: excludes an agent who ran `git init` in the directory -- that shape stages a
#: `160000` chunk and `grader._gitlinks_touched` already owns it.
#:
#: `find -mindepth 1 -maxdepth 1 -print -quit` rather than `ls -A`: it stops at
#: the first entry, and its output is empty-or-not rather than a list that has
#: to be parsed. Command substitution strips trailing newlines, which is why
#: the test is `-n` on ONE printed entry and never a count of lines. A missing
#: directory writes to stderr, which is discarded, and prints nothing.
#:
#: `if`/`then` blocks and a closing `exit 0`, never `[ … ] && continue`: the
#: loop's last command decides the script's exit status, so a trailing false
#: test makes `checked_exec` raise on a healthy tree.
_UNINITIALISED_CONTENT_SH = '''
for p in "$@"; do
  if [ -e "$p/.git" ]; then
    continue
  fi
  if [ -n "$(find "$p" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    printf '%s\\0' "$p"
  fi
done
exit 0
'''
```

And one in `schema.py`, beside the field whose value space it belongs to:

```python
#: What `Checkpoint.submodules_dirty` records for a gitlink directory that git
#: will not describe: uninitialised, and holding content anyway.
#:
#: ONE CHARACTER, and deliberately not a four-character `S…` shape. git's own
#: sub-state is always exactly four characters beginning with `S`, and
#: `container._parse_status_v2` files nothing else -- so a reader can always
#: tell which of the two readers spoke, and `grader._submodule_edits`'
#: `len(state) == 4` guard stays a statement about what GIT said. Writing
#: `"S..U"` here would forge a verdict git never issued, which is the
#: configuration-reported-as-observation family; `?` borrows git's own porcelain
#: vocabulary for untracked, which is exactly what this is.
SUBMODULE_UNINITIALISED_CONTENT = "?"
```

**Rejected: `git submodule status`.** M1 is the reason and it is decisive — it
reports a space marker for B, C and F, the three states this item exists for.
It would have closed nothing while looking like it had, which is the failure
mode `CLAUDE.md` names *"a plausible-looking zero"*.

**Rejected: `git status --porcelain` (v1).** Sees the states but reports ` M`
for all of B–F. The record would then say "something in this submodule is
dirty" and a reader could not tell the state the diff already carries (D) from
the state it cannot (B/C). `_parse_submodule_status`'s own docstring already
litigates this shape of collapse ("*that boolean collapses two states an
operator has to tell apart*").

**Rejected: reading the filesystem** (`ls -A` under each gitlink, the way item
2's `evidence["submodules_empty_after_suite"]` does). It cannot tell a file the
agent wrote from a file the submodule's own commit carries, so on an
initialised submodule it answers a different question. Item 2's use is sound
because there the directory is *asserted empty*.

**Rejected: a second exec for `git ls-files -s -z`** to supply an authoritative
gitlink path set, the way `preflight` does. Unnecessary here: v2's third field
already identifies a submodule (`S…` vs `N…`, M1/M3), so the same stream is
both the path authority and the state authority, and the paths in it are
NUL-delimited rather than display-formatted — which is the property
`preflight`'s two-reader scheme exists to obtain. One exec, not two, inside the
agent's stdout loop.

### D2. `RunContainer.submodule_states() -> dict[str, str]`

Placed directly after `snapshot_diff`. Runs `_SUBMODULE_STATUS_ARGV` through
`self.checked_exec` — **not** `self.exec` — for the reason that method exists:
an empty stdout from a failed `git status` is byte-identical to a tree with no
dirty submodule, and reading that silence as "clean" is the fabricated
measurement `checked_exec` was written to prevent.

```python
    def submodule_states(self) -> dict[str, str]:
        states = _parse_status_v2(
            self.checked_exec(list(_SUBMODULE_STATUS_ARGV)).stdout
        )
        gitlinks = _gitlinks_from_ls_files(
            self.checked_exec(list(_GITLINK_ARGV)).stdout
        )
        for path in self._uninitialised_with_content(gitlinks):
            states[path] = SUBMODULE_UNINITIALISED_CONTENT
        return states

    def _uninitialised_with_content(
        self, gitlinks: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not gitlinks:
            return ()
        result = self.checked_exec(
            ["sh", "-c", _UNINITIALISED_CONTENT_SH, "sh", *gitlinks]
        )
        return tuple(path for path in result.stdout.split("\0") if path)
```

`states[path] = …` cannot overwrite a v2 entry, measured across all five states
in M7: the probe skips every path with a `.git`, and on a `.git`-less path the
two fire on mutually exclusive conditions — v2 when the directory is absent,
the probe when it is non-empty. The assignment is written as a loop rather than a `dict.update` so the
mutation anchor is a line that means "consult the second reader".

`sh -c <script> sh <paths…>` — the paths go through `"$@"`, never through
string interpolation, so a gitlink path containing a space, a quote or a `$` is
passed verbatim (M7 verified the space). `"sh"` in the `$0` slot is what makes
`"$@"` start at the first real path.

Returns a mapping `{path: sub_state}`. From the first reader, every record
whose sub field starts with `S`, with the raw four-character field **verbatim
and unenumerated**. The value space is git's own grammar, `S<c><m><u>`, each
slot either its letter or `.` — **eight values**, not a list of the common
ones. Seven are measured (2026-09-02): `S...` (directory removed), `S.M.`
(tracked edit, or a `rm` of tracked content), `S..U` (untracked file), `SC..`
(commit inside), `SCM.`, `S.MU`, `SCMU`; `SC.U` follows from the grammar and
was not constructed. `_parse_status_v2` files whatever git printed, so naming
four of them would be a closed-world claim the code does not make — and `S...`
and `S.MU` are both trivially reachable. From the second reader, `"?"` per
uninitialised gitlink directory that holds content. Raw, not a derived boolean,
for the reason `_parse_submodule_status` keeps `marker` beside `initialised`:
the record is an observation and the verdict is derived from it somewhere else,
and here the marker also says **which reader** made the observation.

`{}` is a measurement ("read, nothing dirty" — which includes "no submodules",
and an uninitialised submodule whose directory is genuinely empty). The read
either returns a mapping or raises; it never returns `None`.

**One method, one containment, one result.** If either exec fails the whole
mapping becomes `None` in `_capture` — "not read" — rather than a partial
answer presented as a complete one. A half-read mapping would be a `{}` that
means "the first reader found nothing" masquerading as "nothing is dirty",
which is the defect this field exists to avoid one level down.

**No retry.** `snapshot_diff` retries `git add -A` on `unable to stat` because
it loses a documented race against Claude Code's atomic `Write`. `git status`
does not fail that way — a file that vanishes mid-walk is reported, not an exit
128 — and a checkpoint's submodule state is supplementary to a checkpoint,
which is itself supplementary. A failure is contained and named (D4).

### D3. Parsing: `container._parse_status_v2(out: str) -> dict[str, str]`

Module-level, pure, unit-testable without Docker. Written out rather than
described, because `mutation_check.py` does a literal find/replace and an
implementer transcribes rather than invents:

```python
def _parse_status_v2(out: str) -> dict[str, str]:
    """Submodule sub-states out of `git status --porcelain=v2 -z`.

    Records are NUL-terminated, and the count of items per record is NOT
    uniform: a `2` (rename/copy) record occupies TWO -- the new path, then the
    original. Measured 2026-09-02 (git 2.50.1); the five shapes are in this
    item's plan under M3.

    ADVANCING BY TWO AFTER A `2` IS LOAD-BEARING, not hygiene. The second item
    is a PATH, which is repository-authored text, and a path may be named so
    that it reads as a record: a file named

        `1 .M S.M. 160000 160000 160000 aaaaaaa bbbbbbb sneakysub`

    renamed to `ordinary.py` puts exactly that string in the second slot, and a
    parser that reads it as a record files a submodule state for `sneakysub` on
    a repository that has no submodule at all. Same rule as `_GITLINK_MODE`'s:
    content must never reach column zero of a record.

    Fields are split by COUNT (`split(" ", 8)` / `split(" ", 9)`), so a path
    containing spaces survives -- verified against `a dir/f.txt` and against
    the forged name above. A record too short to index contributes nothing
    rather than raising: this is called from the agent's stdout loop, and
    `preflight._gitlink_paths`'s `partition` comment records what an
    unadvertised `IndexError` costs there.
    """
    items = out.split("\0")
    states: dict[str, str] = {}
    index = 0
    while index < len(items):
        item = items[index]
        if item.startswith("2 "):
            index += 2
            fields = item.split(" ", 9)
            path_at = 9
        elif item.startswith("1 "):
            index += 1
            fields = item.split(" ", 8)
            path_at = 8
        else:
            # `u`, `?`, `!`, a `#` header this argv never asks for, and the
            # trailing empty item after the final NUL. One item, no entry.
            index += 1
            continue
        if len(fields) > path_at and fields[2].startswith("S"):
            states[fields[path_at]] = fields[2]
    return states
```

`index += 2` occurs exactly once in the file and is the mutation anchor;
`index += 1` occurs twice and is not usable as one.

Beside it, the gitlink path set — the same rule and the same three failure
modes `preflight._gitlink_paths` records, but taking the stdout string so it is
pure and testable without Docker:

```python
def _gitlinks_from_ls_files(out: str) -> tuple[str, ...]:
    """Every 160000 entry's path, from `git ls-files -s -z`.

    `partition`, never `split("\\t", 1)[1]`: a record beginning `160000 ` with
    no TAB makes the indexed form raise `IndexError`, and this is called from
    the agent's stdout loop. A record that yields no path contributes none.
    """
    paths = []
    for record in out.split("\0"):
        if record.startswith("160000 "):
            _meta, _tab, path = record.partition("\t")
            if path:
                paths.append(path)
    return tuple(paths)
```

### D4. `checkpoints.py`: one extra read, inside the same containment as the diff — and inside `force_capture` too

`SupportsSnapshot` gains a second method:

```python
class SupportsSnapshot(Protocol):
    def snapshot_diff(self, base_sha: str) -> tuple[str, list[str]]: ...
    def submodule_states(self) -> dict[str, str]: ...
```

`CheckpointRecorder._capture` calls it in its **own** `try`, not the caller's:

```python
def _capture(self, turn: int, elapsed_ms: int) -> Checkpoint:
    diff, files = self.container.snapshot_diff(self.base_sha)
    try:
        submodules: dict[str, str] | None = self.container.submodule_states()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        submodules = None
        self.errors.append(
            f"turn {turn}: submodule state: {type(exc).__name__}: {exc}"
        )
    checkpoint = Checkpoint(..., submodules_dirty=submodules, ...)
```

Two things follow, and both are the point.

*The brief's requirement is met:* a failure never raises past `maybe_capture`,
and it names itself in `recorder.errors`, which `runner._collect_checkpoints`
joins into `checkpoint_error`.

*It is stronger than the brief for `force_capture`.* Containing inside
`_capture` rather than relying on `maybe_capture`'s catch keeps
`force_capture`'s contract exactly as written — its **diff** is still allowed
to raise, because that diff is the submission — while making sure a failed
*submodule* read can never be the thing that destroys it. Leaving the read
outside would have inverted the module's own trade: the run's product would
have been lost to a supplementary observation.

`submodules_dirty=None` on that path is "not read", and it is distinct from
`{}` = "read, nothing dirty". `CLAUDE.md`: *a null says which kind of null it
is.*

### D5. Schema: a field on the checkpoint and a field on the record

```python
@dataclass(frozen=True)
class Checkpoint:
    ...
    submodules_dirty: dict[str, str] | None = None
```

```python
@dataclass(frozen=True)
class RunRecord:
    ...
    submodules_dirty_at_exit: dict[str, str] | None = None
```

**On `RunRecord`, not on `Artifacts`.** `Artifacts` holds paths that are
existence- **and** ownership-checked (`wire_log_gz` is `null` rather than a
path to another attempt's file); a mapping of observed states is not that kind
of thing, and putting it there would weaken a block whose whole contract is
about file ownership.

**A top-level field, not "read `checkpoints[-1]` in the grader."** Two
independent reasons.

*First, the division of labour.* The harness records and the grader derives.
An observation the harness made must land in the record whether or not any
grader reads it — offline views need it as much as the ladder does, and the
grader writes nothing back into the log.

*Second, D5's original argument.* Which checkpoint is the final capture is a
question `not_graded_gate` already litigates at length
(`checkpoints[-1].turn == record.turns_streamed`, with a comment recording two
wrong formulations). A second consumer re-deriving it is a second thing that
can be wrong about it.

Both fields round-trip with no serialization change: `to_dict`'s `encode`
recurses into dicts and `from_dict` builds `Checkpoint` through `_build`, which
filters unknown keys and passes a plain `dict` value through untouched
(`schema.py:925-981`). No encoder branch is needed.

`runner.py` gains one module-level helper so the two assignment sites cannot
drift:

```python
def _submodules_at_exit(checkpoints: list[Checkpoint]) -> dict[str, str] | None:
    """The LAST capture's submodule state, or `None` when there is none.

    The last capture, not necessarily the final one: on a run that crashed
    mid-loop this is a mid-run snapshot, exactly as `artifacts.final_diff` --
    the same expression -- is. A reader who needs "at exit" specifically has
    the test `not_graded_gate` performs, `checkpoints[-1].turn ==
    turns_streamed` (`grader.py:585-600`, whose comment records two wrong
    formulations of it).

    `None` rather than `{}` when there is no capture at all: `{}` is the
    measurement "read, nothing dirty", and producing it from the absence of
    any observation is a positive claim manufactured by a failure.
    """
    return checkpoints[-1].submodules_dirty if checkpoints else None
```

Called at **both** sites that already spell `checkpoints[-1].diff_vs_base`:
`assemble_record`'s `RunRecord(...)` construction (`runner.py:837`) and
`_minimal_record`'s (`runner.py:934`). The second matters for the reason its
own docstring gives — *"every field this defaults is a claim it fabricates"* —
and the rescue path carries real checkpoints, so it can carry this honestly.

### D6. `SCHEMA_VERSION` `"3.8.0"` → `"3.9.0"`

The one version constant this plan may write as a literal: checked against all
sixteen other round-2 plans, none moves it, and review 2 re-checked. **Confirm
with `grep -n '^SCHEMA_VERSION' bakeoff/src/bakeoff/schema.py` anyway** (§0).

Additive in fields; the bump is mandatory under the file's own rule
(*"a reader that cannot tell versions apart reads an absent field as a positive
negative claim"*). Concretely: on a 3.8.0 record `submodules_dirty_at_exit` is
absent, and absent must not read as `{}`. It is the difference between "no
submodule was dirty" and "nobody looked", on exactly the records where looking
was impossible.

A paragraph goes in the version-history comment above the constant, in the
established shape.

**And `grade_schema.schema_at_least`'s docstring moves with it.** It reads
*"String comparison puts `"3.10.0"` BELOW `"3.9.0"`, and `SCHEMA_VERSION` is at
3.8.0 — two additive bumps from the wrap."* At 3.9.0 it is **one** bump from
the wrap. That is the docstring of the function whose whole purpose is guarding
that wrap, and repo convention is that prose explaining an invariant moves with
it. There is no `SCHEMA_VERSION` pin test to edit (grepped: only
`GRADE_SCHEMA_VERSION` and `JUDGE_SCHEMA_VERSION` are pinned).

### D7. `NotGradedReason.SUBMODULE_EDIT_UNGRADABLE = "submodule_edit_ungradable"`

`NotGradedReason`, never a `GradeFailure`, on the argument
`_gitlinks_touched`'s docstring already makes: *a GradeFailure puts the row in
the denominator as a model failure, which is precisely the claim this refusal
exists to avoid making.* Today the row lands as `EMPTY_PATCH`, which is a
`GradeFailure`.

Added to the enum after `SUBMODULE_GITLINK_UNGRADABLE`, and named in the class
docstring's **first** group ("the run never produced a gradable submission"),
with its own clause: *a submission exists but the tree it was taken from
carried changes `git add -A` stages nothing for, so the stored diff is not a
description of what the agent produced.*

**`GRADE_SCHEMA_VERSION`: read the literal on disk and increment the minor.**
Not a literal in this plan. It is `"1.3.0"` at round-2 base `8232032`, **and
item 8 takes `"1.3.0"` → `"1.4.0"`** for `GradeRecord.not_run_node_ids` — see
item 8's own *"the version constants"* section and its `GRADE_SCHEMA_VERSION`
paragraph, where it is written unhedged on a check that no item ahead of *it*
moves the constant, a check this item invalidates. (Cited by heading, not by
line: review 2 measured that item 8's line numbers had already moved.) Item 8
is scheduled before item 17, so the expected value here is `"1.4.0"` →
`"1.5.0"`; **the implementer greps `^GRADE_SCHEMA_VERSION` and increments what
is there.**
Whichever of the two lands second and reuses a version that already means
something else commits the precise defect the constant exists to prevent, which
is what the `1.2.0` history entry was written about.

The history comment gains a line in the established shape: `not_graded_reason`
can now carry a value no earlier writer could produce, so its absence on an
older line is a vocabulary gap and not a measurement.

### D8. The grader refusal: `grader._submodule_edits`, before `materialize`, after the gitlink refusal

```python
#: The states a submission diff cannot carry, from either reader. Measured
#: 2026-09-02 (git 2.50.1) through `snapshot_diff`'s own command sequence, with
#: item 2's `read-tree` seed in place: it stages ZERO BYTES for an uncommitted
#: edit (`S.M.`), for an untracked file (`S..U`) and for a `rm` of tracked
#: content (`S.M.`) inside an INITIALISED submodule -- and zero bytes for
#: content inside an UNINITIALISED one (`?`), which git does not report either.
#: So the stored diff describes a tree that is not the one the agent produced.
#:
#: The states NOT here are the ones with neither `M` nor `U` set, and both are
#: already somebody else's: `SC..` (the gitlink moved by a commit inside --
#: staged as a 245-byte chunk) and `S...` (the directory removed -- staged as a
#: `deleted file mode 160000`, 199 bytes per path and seed-invariant). The diff
#: carries both, so `_gitlinks_touched` refuses them by name. Two refusals,
#: disjoint.
#:
#: The sub-state is git's `S<c><m><u>` grammar -- eight values, of which seven
#: are measured -- so this tests the two BITS and never a list of spellings.
#: `S.MU` and `SCMU` are reachable and are refused on `M` exactly as `S.M.` is.
def _submodule_edits(record: RunRecord) -> tuple[str, ...]:
    states = record.submodules_dirty_at_exit
    # `None` (pre-3.9.0, or a contained read failure) and `{}` (read, nothing
    # dirty) collapse HERE and only here. The VERDICT treats them alike --
    # "not measured" is not evidence of an edit, and every stored record
    # predates the field, so fail-closed would refuse the whole corpus. The
    # RECORD keeps them apart permanently; do not "fix" it to match this.
    if not states:
        return ()
    return tuple(sorted(
        path for path, state in states.items()
        if state == SUBMODULE_UNINITIALISED_CONTENT
        or (len(state) == 4 and (state[2] == "M" or state[3] == "U"))
    ))
```

The `?` disjunct is FIRST and is an equality, not a bit test, because the two
readers speak different languages and the predicate must not pretend otherwise:
`len(state) == 4` is a claim about git's own four-character sub-state, and the
filesystem marker is deliberately outside that shape (D1). Refusing it is the
same claim as refusing `M`/`U` — the grading tree is re-materialized, so its
uninitialised submodule directory arrives **empty**, and the content the agent
put there exists in neither the diff nor the tree.

`MIN_GRADABLE_SCHEMA` is `"3.0.0"` and every record in every stored event log
predates 3.9.0, which is what makes the fail-open direction the only honest one.

`len(state) == 4` guards a hand-edited record rather than anything the parser
produces; without it `state[3]` raises `IndexError` out of a function
`grade_run` does not wrap.

Call site in `grade_run`, immediately **after** the `gitlinks` block and
**before** `artifacts_dir` / `fresh_tree` / `materialize`:

```python
    # AFTER the gitlink refusal, and the order is deliberate. State E (a
    # commit inside a submodule with further edits left behind) is `SCM.` AND
    # stages a 245-byte `index …160000` chunk, so both refusals are true of
    # it; the gitlink reason is the more specific description -- it names the
    # commit that exists only in the run tree -- and it is the one already in
    # the stored vocabulary (GRADE_SCHEMA 1.2.0). Keeping it first also keeps
    # it reachable for the uninitialised-gitlink shape it takes on a task with
    # a declared-unneeded submodule.
    edits = _submodule_edits(record)
    if edits:
        return build_grade_record(record, task, image, oracle, _gated_result((
            NotGradedReason.SUBMODULE_EDIT_UNGRADABLE,
            f"the run tree's submodule(s) {', '.join(edits)} carried changes "
            "git stages nothing for when the agent exited "
            f"({record.submodules_dirty_at_exit}); the stored submission "
            "cannot reproduce that tree, so grading it would grade a "
            "different tree than the agent produced",
        )))
```

**Before `materialize`**, for `grade_run`'s stated reason: a refused row costs
no tree, no container and no mirror clone. This refusal reads only the record,
so it is even cheaper than the gitlink one.

### D9. What a declared-unneeded submodule can and cannot reach

Revised twice. Revision 1 got the reasoning wrong; revision 2 got the
consequence wrong. The measured position:

**A declared-unneeded submodule that nobody touched is never refused.**
`git status --porcelain=v2` emits no record for an uninitialised gitlink whose
directory is **present** — empty or not (M1 rows G/G′, M7 rows 1 and 2) — and
the probe stays silent on an empty one. (Not M7 row 5: a gitlink whose
directory has been *removed* does get a `1 .D S...` record, and the diff carries
the deletion, so `_gitlinks_touched` owns that shape.) So `submodules_dirty` is `{}` and
`SUBMODULE_EDIT_UNGRADABLE` cannot fire. That holds on both sides of item 2's
`snapshot_diff` seed.

**A declared-unneeded submodule the agent WROTE INTO is refused, and that is
the point of revision 3.** Post-seed — the world §0 mandates — such a file is
in the diff (0 bytes), in the v2 stream (no record) and in `git status`
(nothing): **recorded nowhere**, and the run would grade as `EMPTY_PATCH` →
`resolved: False`, which is the same accusation this whole item exists to stop
making, one submodule shape over. M7's probe is what closes it. The refusal is
correct rather than merely convenient: `grade_run` re-materializes the tree, so
the declared-unneeded directory arrives empty and the graded tree is not the
tree the agent produced.

Revision 1 wrote the opposite of this and revision 2 preserved the error in
three places. The claim *"an agent writing into an uninitialised submodule
directory is invisible to git, and the diff carries it as a `100644` chunk"* is
true of **today's** `snapshot_diff` (M1 row G′ pre-seed, 410 bytes) and false
of the one §0 mandates (0 bytes). Writing a plan against the pre-seed number
while forbidding the pre-seed code is what review 2 caught.

**A false positive here is an item-2-shaped question, and item 2 already owns
the guard.** If a task's suite, conftest or `image.build` step writes into a
declared-unneeded submodule directory, every run on that task would be refused.
Item 2's `evidence["submodules_empty_after_suite"]` measures exactly that
emptiness at preflight time, per declared path, after the suite has run — so
such a task is a NO-GO before any cell is spent on it. This plan adds no new
screen and needs none; it makes item 2's existing one load-bearing at grade
time as well. §6 says the same thing about the initialised case.

### D10. `GRADER_VERSION`: read the literal on disk and add one

Not a literal in this plan. It read `"9"` at round-2 base `8232032` (item 3
moved it from `"8"`) and is already `"10"` as of 2026-09-02, with
`tests/test_grader.py`'s pin still asserting `"9"` while another item's
implementer is mid-edit — the read-and-increment ruling vindicated in real
time. Items 1, 8 and 9 each bump it again before this one.
**`grep -n '^GRADER_VERSION' bakeoff/src/bakeoff/grader.py` and add one.**

The resume gate in `scripts/grade.py` keys on `(run_id, GRADER_VERSION)`
**alone**. Without the bump, every already-graded row keeps its verdict —
including any row this refusal would now take out of the denominator — and no
line says the two were measured under different rules.

No verdict on today's corpus changes: no stored record carries
`submodules_dirty_at_exit`, so `_submodule_edits` returns `()` for all of them
and the ladder runs exactly as under the previous version. It moves anyway, on
the same argument the `4 -> 5` entry makes in full: *the bump has to land with
the code that makes the divergence possible, not with the run that first
exercises it.*

Operator note, in the established shape: this re-grades every stored run into a
fresh `v<N>` artifacts directory beside the existing one, at the cost of one
full grading pass per event log; schedulable rather than urgent.

`ORACLE_VERSION` does **not** move — the quarantine derivation is untouched.

---

## 4. File structure

| file | change |
|---|---|
| `bakeoff/src/bakeoff/container.py` | `+_SUBMODULE_STATUS_ARGV`, `+_GITLINK_ARGV`, `+_UNINITIALISED_CONTENT_SH`, `+_parse_status_v2`, `+_gitlinks_from_ls_files`, `+RunContainer.submodule_states`, `+RunContainer._uninitialised_with_content` (all after `snapshot_diff`) |
| `bakeoff/src/bakeoff/checkpoints.py` | `SupportsSnapshot` gains `submodule_states`; `_capture` gains the contained read; module and `force_capture` docstrings updated |
| `bakeoff/src/bakeoff/schema.py` | `+SUBMODULE_UNINITIALISED_CONTENT`, `+Checkpoint.submodules_dirty`, `+RunRecord.submodules_dirty_at_exit`, `SCHEMA_VERSION` → `"3.9.0"` + history paragraph |
| `bakeoff/src/bakeoff/runner.py` | `+_submodules_at_exit`; both `RunRecord(...)` constructions pass it |
| `bakeoff/src/bakeoff/grade_schema.py` | `+NotGradedReason.SUBMODULE_EDIT_UNGRADABLE`, docstring group, `GRADE_SCHEMA_VERSION` **read-and-increment** + history line, **`schema_at_least` docstring** ("two additive bumps" → "one") |
| `bakeoff/src/bakeoff/grader.py` | `+_submodule_edits`, `grade_run` branch + order comment, `GRADER_VERSION` **read-and-increment** + history paragraph, `__all__` unchanged (`_submodule_edits` is private) |
| `bakeoff/tests/test_container.py` | `+_ArgvRecorder` and a dispatching double; +7 tests |
| `bakeoff/tests/test_checkpoints.py` | `+_StatesRaise` double; +4 tests; `FakeContainer` and `ExplodingContainer` gain `submodule_states` |
| `bakeoff/tests/test_fault_injection.py` | `FakeContainer.submodule_states` added (l.142's class) |
| `bakeoff/tests/test_smoke_test.py` | fake at l.63 gains `submodule_states` |
| `bakeoff/tests/test_runner.py` | +3 tests |
| `bakeoff/tests/test_grader.py` | +6 tests; version-pin test edited |
| `bakeoff/tests/test_grade_schema.py` | version-pin test edited |
| `bakeoff/tests/test_integration_submodules.py` | `_record_with_diff` parameterised; +1 test |
| `bakeoff/scripts/mutation_check.py` | +8 rows |
| `bakeoff/taskset/HARVESTING.md` | one bullet amended |
| `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` | §5.6, two paragraphs |
| `CLAUDE.md` | two sentences (§8) |
| `TASKS.md` | bullet removed |
| `tasks/todo.md` | review section |

### The four existing test doubles

`grep -rn 'def snapshot_diff' bakeoff/tests bakeoff/src bakeoff/scripts` returns
exactly six hits: the Protocol, `RunContainer`, and **four** doubles —
`tests/test_checkpoints.py:13`, `tests/test_checkpoints.py:67`,
`tests/test_fault_injection.py:142`, `tests/test_smoke_test.py:63`. All four
need `submodule_states`. `SupportsSnapshot` is a `typing.Protocol` and is not
runtime-checked, so a double missing the method does **not** fail loudly: it
raises `AttributeError` inside `_capture`'s new `try`, which is contained, and
the test goes on passing with `submodules_dirty=None` and a spurious
`recorder.errors` entry. Adding the method to all four is therefore not
optional tidying — it is what keeps the existing suite honest.

Default for the three doubles that do not test this behaviour:
`def submodule_states(self): return {}`. `ExplodingContainer` (l.67) keeps its
diff-raising behaviour and returns `{}`, so the existing tests keep asserting
what they assert about the *diff*.

---

## 5. Tasks

### Task 1 — `container.py`: the read and its parser

1. Add `_SUBMODULE_STATUS_ARGV` exactly as written in D1, with the comment.
2. Add `_parse_status_v2` exactly as written in D3, module-level, beside the
   other module helpers.
3. Add `RunContainer.submodule_states` and `_uninitialised_with_content` per
   D2, after `snapshot_diff`, and `_gitlinks_from_ls_files` per D3. The
   `submodule_states` docstring carries:

   * M1's rows in prose — which states the diff already carries and which it
     does not — using **0, 245 and M7 row 5's 199 only**. Those three are
     seed-invariant, measured. The **pre-seed row-G/G′ 199 and 410 must NOT be
     written into source**: §0 forbids implementing against the `snapshot_diff`
     that produces them, and the repo's convention is that a claim about
     external behaviour names what it was verified against. Note the collision
     and do not let it blur — 199 is a *removed* gitlink directory's deletion
     chunk (row 5, true either way) and also a *present-but-empty* one's under
     the pre-seed code only (row G, 0 after the seed).
   * M2's two paragraphs with their measured numbers.
   * M7's disjointness, stated as the measured **directory** rule: on a
     `.git`-less path v2 fires only when the directory is absent and the probe
     only when it is non-empty. The `.git` test is what makes the probe cheap,
     not what makes the readers disjoint — do not write the `.git` version,
     which review 3 measured false against row 5.
   * The value space as git's `S<c><m><u>` grammar — eight values, seven
     measured — with examples rather than a closed list of four. `S...` and
     `S.MU` are reachable and `_parse_status_v2` files whatever git printed, so
     a four-item list would be a closed-world claim the code does not make.
   * The `checked_exec`-not-`exec` argument, and the "`{}` is a measurement,
     this method never returns `None`, one containment one result" sentences.
4. **The doubles.** `tests/test_container.py` has no argv-recording stub —
   `_AddSequence` (`:394-410`) records an integer, and `_container_with`
   (`:413-419`) monkeypatches **both** `exec` and `checked_exec` onto a real
   `RunContainer`, replacing the exit-code check wholesale. Two constructions:

   ```python
   class _ArgvRecorder:
       """Records every argv it is handed and answers with canned stdout."""

       def __init__(self, stdout: str = "", exit_code: int = 0, stderr: str = ""):
           self.argvs: list[list[str]] = []
           self._result = (exit_code, stdout, stderr)

       def __call__(self, cmd, env=None):
           from bakeoff.container import ExecResult

           self.argvs.append(list(cmd))
           return ExecResult(*self._result, 1)
   ```

   Wired for the first two tests with `_container_with(_ArgvRecorder(...))`.
   The **third** test must NOT use `_container_with`: it asserts the error
   `RunContainer.checked_exec` builds, and `_container_with:418` replaces that
   method with `lambda cmd, env=None: exec_stub(cmd, env)`, dropping the
   exit-code check entirely. It stubs `exec` only —

   ```python
   container = RunContainer(image="sha256:" + "0" * 64,
                            repo_path="/tmp/x", base_sha="abc")
   container.exec = _ArgvRecorder(exit_code=1, stderr="fatal: not a git repository")
   ```

   — which is also the only construction that actually exercises D2's
   `checked_exec`-not-`exec` argument.
5. **Tests** (`tests/test_container.py`):
   - `test_the_submodule_state_argv_is_pinned_word_for_word` — asserts
     `recorder.argvs == [["git", "--no-optional-locks", "status", "--porcelain=v2", "--ignore-submodules=none", "-z"]]`.
     Docstring carries M2: the index-mtime table (both indexes rewritten by a
     plain `status`, neither by this one) and the three-config table showing
     `--ignore-submodules=none` is what keeps a repository-authored
     `.gitmodules` `ignore = all` from disarming the refusal.
   - `test_a_submodule_state_read_takes_paths_and_states_from_the_nul_stream` —
     canned stdout, NUL-separated, containing **five** items and a trailing
     empty one:

     ```
     1 .M S.M. 160000 160000 160000 aaaa bbbb vendor/libdep
     1 .M N... 100644 100644 100644 cccc dddd calc.py
     2 R. N... 100644 100644 100644 eeee ffff R100 ordinary.py
     1 .M S.M. 160000 160000 160000 aaaaaaa bbbbbbb sneakysub
     ? a dir/
     ```

     (item 4 is the rename's ORIGINAL path — a real, measured shape, M3b.)
     Asserts the result is **exactly** `{"vendor/libdep": "S.M."}`: the
     ordinary file excluded by `N`, the untracked path with a space excluded,
     and — the assertion the mutation turns red — **`"sneakysub"` absent**,
     because the rename consumed its original path rather than parsing it.
     Docstring carries M3b verbatim, including the filename.
   - `test_a_failed_submodule_state_read_raises_rather_than_reporting_a_clean_tree`
     — the `exec`-only container above; asserts `ContainerError` is raised and
     its message carries `"fatal: not a git repository"`. Docstring: an empty
     stdout on a failed status is byte-identical to a tree with no dirty
     submodule, and `_container_with` cannot be used because it replaces the
     method under test.

   The second reader needs a stub that answers three different argvs, so these
   four use a small dispatching double built on `_ArgvRecorder`'s recording:
   `git … status …` → the canned v2 stream, `git ls-files -s -z` → a canned
   `160000 <sha> 0\tvendor/libdep\0`, `sh -c …` → a canned NUL list.

   - `test_content_in_an_uninitialised_submodule_is_recorded_as_a_distinct_marker`
     — v2 empty, `ls-files` naming `vendor/libdep`, probe answering
     `"vendor/libdep\0"`. Asserts
     `states == {"vendor/libdep": "?"}` and, explicitly,
     `states["vendor/libdep"] != "S..U"`. Docstring carries M1 row G′ post-seed
     (0 bytes in the diff, no v2 record — recorded nowhere without this) and
     D1's argument for one character rather than a forged `S…` shape.
   - `test_an_initialised_submodule_is_left_to_git_and_not_probed` — v2
     reporting `S.M.` for `vendor/libdep`, `ls-files` naming it, probe
     answering `""` (as the real script does, because `.git` is present).
     Asserts `states == {"vendor/libdep": "S.M."}` — the probe cannot overwrite
     a git verdict, and M7's disjointness is what guarantees it never tries.
   - `test_the_uninitialised_probe_argv_is_pinned_word_for_word` — asserts the
     recorded probe argv is
     `["sh", "-c", _UNINITIALISED_CONTENT_SH, "sh", "vendor/libdep"]`.
     Docstring: paths go through `"$@"` and never through interpolation, `"sh"`
     fills the `$0` slot, and M7 verified a path with a space survives.
   - `test_a_tree_with_no_gitlinks_never_runs_the_probe` — `ls-files` answering
     `""`; asserts no `sh` argv was recorded at all and `states == {}`.
     Docstring: this is the short-circuit that keeps the cost off every task in
     today's corpus but one (M7).

### Task 2 — `checkpoints.py`: the contained read

1. Extend `SupportsSnapshot` per D4.
2. Rewrite `_capture` per D4. The `except` comment points at the docstring.
3. Module docstring gains a paragraph: what the diff cannot carry, and that the
   checkpoint records it beside the diff rather than instead of it.
4. `force_capture`'s docstring gains a sentence: its **diff** is still allowed
   to raise; the submodule read never is, because losing the submission to a
   supplementary observation inverts the trade the module rests on.
5. **The doubles.** `FakeContainer` (l.13) gains `submodule_states` returning
   `self.states`, defaulted to `{}` in `__init__` and set per test.
   `ExplodingContainer` (l.67) gains `def submodule_states(self): return {}`
   and keeps its diff-raising body untouched, so
   `test_the_final_snapshot_is_still_allowed_to_fail_loudly` keeps asserting
   what it asserts. A **third** double, for the two tests that need the
   opposite pair (working diff, raising state read) — its exception type and
   message are load-bearing, because the tests assert the error string
   verbatim:

   ```python
   class _StatesRaise:
       """A container whose diff works and whose submodule read does not."""

       def snapshot_diff(self, base_sha):
           return ("diff-body", ["calc.py"])

       def submodule_states(self):
           raise ContainerError("boom")
   ```

6. **Tests** (`tests/test_checkpoints.py`):
   - `test_a_checkpoint_records_the_submodule_state_the_diff_cannot_carry` —
     `FakeContainer` with `states={"vendor/libdep": "S.M."}`; asserts
     `checkpoint.submodules_dirty == {"vendor/libdep": "S.M."}` and that
     `diff_vs_base` is still the fake's diff. Docstring carries M1 row B: the
     diff is zero bytes for exactly this tree, through `snapshot_diff`'s own
     command sequence.
   - `test_no_dirty_submodule_is_an_empty_mapping_not_a_none` — `states={}`;
     asserts `checkpoint.submodules_dirty == {}` and `recorder.errors == []`.
     Docstring: `{}` is a measurement, `None` is "not read", and collapsing
     them is the defect one layer down.
   - `test_a_failed_submodule_read_is_contained_and_named` — `_StatesRaise` via
     `maybe_capture(1, elapsed_ms=0)`; asserts it returns a checkpoint,
     `checkpoint.diff_vs_base == "diff-body"`, `submodules_dirty is None`, and
     `recorder.errors == ["turn 1: submodule state: ContainerError: boom"]`.
   - `test_the_final_capture_survives_a_failed_submodule_read` — `_StatesRaise`
     via `force_capture(turn=1, elapsed_ms=0)`; asserts it returns a Checkpoint
     rather than raising, with `submodules_dirty is None` and the diff intact.
     Docstring: the submission must never be lost to the supplementary read,
     which is why the containment is inside `_capture` and not in
     `maybe_capture`.
   - The existing `test_the_final_snapshot_is_still_allowed_to_fail_loudly` is
     left byte-for-byte unchanged and keeps passing: it fails the **diff**.

### Task 3 — `schema.py`

1. `Checkpoint.submodules_dirty: dict[str, str] | None = None`, with a comment
   carrying the three-value meaning and M1's row B/G asymmetry: an initialised
   submodule's edit is in the v2 record and not in the diff; an uninitialised
   one's stray file is in the diff (as a `100644` chunk) and not in the record.
2. `RunRecord.submodules_dirty_at_exit: dict[str, str] | None = None`. The
   comment says it is the **last** capture's state, points at
   `not_graded_gate`'s `checkpoints[-1].turn == turns_streamed` test for a
   reader who needs "at exit" specifically, and says why it is not on
   `Artifacts` (D5).
3. Add `SUBMODULE_UNINITIALISED_CONTENT = "?"` exactly as written in D1, with
   its comment, beside the `Checkpoint` field whose value space it defines.
   `container.py` and `grader.py` both already import from `schema.py`, so it
   lives with the field rather than in either reader — a value space owned by
   the producer would make the consumer's predicate depend on the producer.
4. `SCHEMA_VERSION = "3.9.0"` and the history paragraph. The history entry
   names **both** field and marker: a 3.8.0 reader has neither.

### Task 4 — `runner.py`

1. Add `_submodules_at_exit` exactly as written in D5, near `_artifacts_block`.
2. Pass `submodules_dirty_at_exit=_submodules_at_exit(checkpoints)` in
   `assemble_record`'s `RunRecord(...)` and in `_minimal_record`'s.
3. **Tests** (`tests/test_runner.py`, using the module's existing
   `assemble_record` fixtures):
   - `test_the_record_carries_the_last_captures_submodule_state` — three
     checkpoints, the last carrying `{"tests/toml-test": "S.M."}`; asserts
     `record.submodules_dirty_at_exit == {"tests/toml-test": "S.M."}`.
   - `test_no_checkpoints_reports_no_submodule_state_rather_than_a_clean_one` —
     `checkpoints=[]`; asserts `submodules_dirty_at_exit is None` beside
     `artifacts.final_diff is None`. Docstring: `{}` here would be a positive
     claim manufactured by there being no observation.
   - `test_the_minimal_record_carries_the_submodule_state_too` — drives the
     `_minimal_record` path (the existing `assembly_error` test's mechanism)
     with a final checkpoint carrying `{"vendor/libdep": "S..U"}`; asserts the
     rescue record carries it. Docstring quotes `_minimal_record`'s own rule:
     every field it defaults is a claim it fabricates.

### Task 5 — `grade_schema.py`

1. `SUBMODULE_EDIT_UNGRADABLE = "submodule_edit_ungradable"` after
   `SUBMODULE_GITLINK_UNGRADABLE`.
2. `NotGradedReason` docstring: extend the first group per D7.
3. `GRADE_SCHEMA_VERSION`: **`grep -n '^GRADE_SCHEMA_VERSION'
   src/bakeoff/grade_schema.py` and increment the minor** (D7). Append the
   history line for the new reason.
4. `schema_at_least`'s docstring: *"`SCHEMA_VERSION` is at 3.8.0 — two additive
   bumps from the wrap"* → 3.9.0, **one** additive bump from the wrap (D6).
5. Edit the `GRADE_SCHEMA_VERSION` pin in `tests/test_grade_schema.py` (find
   it with `grep -n 'GRADE_SCHEMA_VERSION ==' tests/`) to whatever step 3
   produced, extending its docstring with the new reason. **Not** to a literal
   from this plan.

### Task 6 — `grader.py`

1. Add `_submodule_edits` exactly as written in D8, in the *"the gates, which
   run before any container"* section, after `not_graded_gate`, including the
   `if not states` comment saying the collapse is the verdict's and not the
   record's.
2. Add the `grade_run` branch and its order comment, exactly as written in D8.
3. `grade_run`'s docstring gains a paragraph: two submodule refusals now sit
   before `materialize`, what separates them (the diff-derived one reads the
   submission's chunks, the record-derived one reads a state the submission
   cannot carry), and why the gitlink one is first.
4. `GRADER_VERSION`: **`grep -n '^GRADER_VERSION' src/bakeoff/grader.py` and
   add one** (D10), never a line number and never a literal from this plan.
   Append the `N -> N+1` history paragraph.
5. **Tests** (`tests/test_grader.py`, in the *"1b. the gitlink refusal"*
   section, whose banner gains "and the edit refusal"). **`_record` is not
   changed**: it is already `def _record(diff=TEXT_DIFF, **kw)` with
   `fields.update(kw)` (`:234-265`), and its docstring states the design an
   alias would break — *"every gate test overrides exactly the field it is
   about"*. The five tests pass `submodules_dirty_at_exit=` by its real name.
   - `test_a_dirty_submodule_at_exit_is_not_graded_rather_than_failed` —
     `grade_run` with an ordinary non-empty `TEXT_DIFF` and
     `submodules_dirty_at_exit={"vendor/libdep": "S.M."}`. Asserts
     `resolved is None`, `not_graded_reason == "submodule_edit_ungradable"`,
     `grade_failure is None`, `"vendor/libdep" in not_graded_detail`, and that
     no tree was materialized
     (`not (tmp_path / "cache" / "grade-tree").exists()`). Docstring carries
     M1 row B and the `EMPTY_PATCH`-is-an-accusation argument.
   - `test_an_untracked_file_inside_a_submodule_is_refused_too` — same, with
     `"S..U"` (M1 row C).
   - `test_the_states_the_diff_carries_are_left_to_the_gitlink_refusal` —
     predicate level, **both** of them:
     `_submodule_edits(_record(submodules_dirty_at_exit={"v": "SC.."})) == ()`
     and `… {"v": "S..."} …) == ()`. Docstring: these are the two sub-states
     with neither `M` nor `U` set, and `git add -A` stages a chunk for each —
     245 bytes for the moved gitlink (M1 row D) and a `deleted file mode
     160000` for the removed directory (M7 row 5, 199 bytes per path,
     seed-invariant) — so the diff carries them and `_gitlinks_touched` names
     them. Mutation row 6 goes red on the `SC..` half.
   - `test_a_record_that_never_measured_its_submodules_is_still_graded` —
     `_submodule_edits` returns `()` for `None` and for `{}`. Docstring: every
     stored record predates 3.9.0 and `MIN_GRADABLE_SCHEMA` is `"3.0.0"`, so
     fail-closed would refuse the whole corpus; the record still keeps the two
     apart.
   - `test_the_gitlink_refusal_wins_when_a_submission_does_both` — `grade_run`
     with `_GITLINK_SUBMISSION` **and**
     `submodules_dirty_at_exit={"vendor/libdep": "SCM."}`; asserts
     `not_graded_reason == "submodule_gitlink_ungradable"` (M1 row E).
   - `test_content_in_an_uninitialised_submodule_is_refused_too` — `grade_run`
     with an ordinary non-empty `TEXT_DIFF` and
     `submodules_dirty_at_exit={"vendor/libdep": "?"}` (the marker imported
     from `schema`, never typed as a literal in the test). Asserts
     `resolved is None`,
     `not_graded_reason == "submodule_edit_ungradable"`,
     `"vendor/libdep" in not_graded_detail`. Docstring carries M1 row G′
     post-seed and the re-materialization argument: the graded tree's
     uninitialised submodule directory arrives **empty**, so the content the
     agent put there is in neither the diff nor the tree.
   - Edit `test_the_grader_version_moved_with_what_check_5_means` (find it with
     `grep -n 'GRADER_VERSION ==' tests/test_grader.py`) to whatever Task 6.4
     produced, with an `N -> N+1` paragraph. **Not** to a literal from this
     plan. Note it may already be *stale on arrival*: measured 2026-09-02, the
     constant is `"10"` and this pin still asserts `"9"`.

### Task 7 — the integration test

`tests/test_integration_submodules.py`, reusing the module-scoped
`superproject`, `workspace` and `local_urls` fixtures. Module `pytestmark`
already carries `integration` **and** `task_image`.

**`_record_with_diff` (`:420-450`) is parameterised first**, because as written
it mints `run_id=f"sub-int-002-{'empty' if not diff else 'reference'}"` and
`collection_id="integration-submodule-test-prefix"` — so an empty-diff record
built for this test would reuse `sub-int-002-empty`, which is exactly the
collision the helper's own docstring says the namespacing prevents, and would
inherit the *fix-1* fixture's collection id while using the `superproject`
fixture. It gains three keyword parameters whose defaults leave every existing
call byte-identical:

```python
def _record_with_diff(task, diff: str, *, run_id: str | None = None,
                      collection_id: str = "integration-submodule-test-prefix",
                      submodules_dirty_at_exit: dict[str, str] | None = None):
```

`run_id` defaults to the existing expression when `None`. The new test passes
`run_id="sub-int-003-edit"`, `collection_id="integration-submodule-edit"`, and
the observed state.

`test_an_uncommitted_edit_inside_a_submodule_is_recorded_then_refused`:

1. `load_task` → `materialize(task, workspace / "run-edit", cache)`.
   **A separate run tree from the first test's** — module-scoped fixtures run
   in an unspecified order, and `fresh_tree`'s rule about never reissuing a
   mounted path applies to bind mounts here too (the file's own
   `superproject_under_test_prefix` fixture records the same reasoning).
2. Build the task image the same two ways the first test does
   (`build_base_images(REPO_ROOT, [task_runtime(task)])` → `build_task_image`).
   Docker's layer cache makes the second build a no-op. Not the base image:
   `RunContainer.__enter__` asserts non-root and no `ENTRYPOINT`, the first
   test asserts both of the **task** image, and nothing in the suite exercises
   the base image under `RunContainer` — a failure there would read as this
   item's defect.
3. `with RunContainer(image=image, repo_path=str(run_tree), base_sha=start_sha)
   as container:`
   - stamp both indexes via
     `container.exec(["touch", "-d", "@1577865600", ".git/index", ".git/modules/vendor/libdep/index"])`;
   - `container.exec(["sh", "-c", "echo EDITED >> vendor/libdep/libdep/__init__.py"])`;
   - `recorder = CheckpointRecorder(container, start_sha, every_k_turns=1)`;
     `checkpoint = recorder.force_capture(turn=1, elapsed_ms=0)`.
4. **The three assertions this test exists for**:
   - `assert checkpoint.diff_vs_base == ""` — the defect, in a real container
     against a real submodule. This is the assertion that would have caught it.
   - `assert checkpoint.submodules_dirty == {SUB_PATH: "S.M."}` — the closure,
     on `Checkpoint.submodules_dirty`.
   - both index mtimes still `1577865600` after the capture — M2, in the
     environment that matters, read back with
     `container.exec(["stat", "-c", "%Y", …])`.
5. Then the grader half. `_record_with_diff(task, "", run_id="sub-int-003-edit",
   collection_id="integration-submodule-edit",
   submodules_dirty_at_exit={SUB_PATH: "S.M."})` — the helper sets
   **`RunRecord.submodules_dirty_at_exit`** directly, because this test does not
   run `assemble_record` and so the record field does not appear on its own from
   step 4's `Checkpoint.submodules_dirty`. Then
   `grade_run(record, task, image, None, cache, artifacts)` asserts
   `resolved is None`, `not_graded_reason == "submodule_edit_ungradable"`, and
   that the detail names `vendor/libdep`. Docstring: without this refusal the
   same record grades `EMPTY_PATCH` → `resolved: False`, permanently, in an
   append-only file.

### Task 8 — `scripts/mutation_check.py`

Eight rows, in the file's `(label, path, anchor, replacement, selector, marker)`
tuple shape. Revision 1's `2`-record entry is row 4 below and is no longer
inert: M3b's forged original path makes the mis-consumption observable in the
parser test's asserted dict.

| # | label | file | anchor → replacement | selector | marker |
|---|---|---|---|---|---|
| 1 | `checkpoints: record the diff and drop the submodule state` | `src/bakeoff/checkpoints.py` | `submodules_dirty=submodules,` → `submodules_dirty=None,` | `tests/test_checkpoints.py -k records_the_submodule_state` | `not integration` |
| 2 | `container: let the submodule read take the index lock` | `src/bakeoff/container.py` | `"git", "--no-optional-locks", "status",` → `"git", "status",` | `tests/test_container.py -k argv_is_pinned` | `not integration` |
| 3 | `container: let a repository-authored ignore setting hide a dirty submodule` | `src/bakeoff/container.py` | `"--porcelain=v2", "--ignore-submodules=none", "-z",` → `"--porcelain=v2", "-z",` | `tests/test_container.py -k argv_is_pinned` | `not integration` |
| 4 | `container: read a rename's original path as a record of its own` | `src/bakeoff/container.py` | `            index += 2` → `            index += 1` | `tests/test_container.py -k nul_stream` | `not integration` |
| 5 | `grader: grade a submission whose tree carried an uncommitted submodule edit` | `src/bakeoff/grader.py` | `    if edits:` → `    if False:` | `tests/test_grader.py -k dirty_submodule_at_exit` | `not integration` |
| 6 | `grader: refuse on the commit bit too, making the gitlink refusal unreachable` | `src/bakeoff/grader.py` | `        or (len(state) == 4 and (state[2] == "M" or state[3] == "U"))` → `        or state != "S..."` | `tests/test_grader.py -k left_to_the_gitlink_refusal` | `not integration` |
| 7 | `container: skip the second reader, so content in an uninitialised submodule is recorded nowhere` | `src/bakeoff/container.py` | `        for path in self._uninitialised_with_content(gitlinks):` → `        for path in ():` | `tests/test_container.py -k recorded_as_a_distinct_marker` | `not integration` |
| 8 | `grader: read git's verdict only, letting the filesystem marker through` | `src/bakeoff/grader.py` | `        if state == SUBMODULE_UNINITIALISED_CONTENT` → `        if False` | `tests/test_grader.py -k uninitialised_submodule_is_refused` | `not integration` |

Anchor uniqueness, checked against D1/D3/D8's code: `index += 2` occurs once in
`container.py` (`index += 1` occurs twice and is not usable); each argv string
occurs once; `if edits:` occurs once; `_uninitialised_with_content(gitlinks)`
occurs once as a call (its `def` line differs); `SUBMODULE_UNINITIALISED_CONTENT`
occurs once inside `_submodule_edits`. Rows 7 and 8 are the two halves of the
second reader — capture and verdict — and each must go red on its own, because
either one alone silently restores the gap. Each row carries the comment block the
file's style requires: the measured defect the guarantee closes, not a
restatement of the line. **The implementer re-verifies the anchor text against
the tree as it lands** — `mutation_check.py` fails loudly on a stale anchor,
which is the intended behaviour.

### Task 9 — docs

1. **`bakeoff/taskset/HARVESTING.md`**, the bullet *"A task whose fix touches
   submodule content is out"*: keep the rule and the refusal, and append —
   since this change the harness **records** what it cannot capture
   (`RunRecord.submodules_dirty_at_exit`, from the last checkpoint) and the
   grader refuses such a run as `submodule_edit_ungradable` rather than letting
   it land as `EMPTY_PATCH` → `resolved: False`. The rule stands because a
   refused row is still a lost observation, and because what an agent chooses
   to edit is not something a manifest can constrain. Still **six** limits — a
   strengthened one, not a seventh.
   *Not touched:* the *"A suite that writes inside the submodule is out"*
   bullet, which item 2 amends (its review finding 13). Two plans editing one
   bullet is a merge conflict for no benefit.
2. **Spec §5.6**, after *"That diff is the submission…"*, **two paragraphs**:
   - The normalization is not total. `git add -A` stages **nothing** for an
     uncommitted edit or an untracked file inside an *initialised* submodule
     (measured 2026-09-02, git 2.50.1, through this exact command sequence: 0
     bytes for both, against 245 bytes for a commit inside one), so a
     submission diff is a complete description of the tree only outside gitlink
     boundaries. The run records `submodules_dirty_at_exit` so a reader can
     tell that shape from an honest empty submission, and the offline grader
     refuses the former rather than scoring it.
   - The second paragraph is review finding 13's, and it goes here because no
     other plan is editing this file: the *untracked*-file case (`S..U`) is
     what `HARVESTING.md`'s *"a suite that writes inside the submodule is out"*
     rule is about, and its consequence is now grade-visible — such a task
     would refuse **every** collected run as `submodule_edit_ungradable` rather
     than merely dirtying the tree. Preflight's clean-tree NO-GO on
     ` M <path>` is what keeps such a task out before that can happen, and it
     reads the same signal at a coarser resolution.
3. **`TASKS.md`**: delete the *"A pure-gitlink submission is refused; a
   pure-gitlink *edit* is invisible"* bullet. **No residual line replaces it**,
   and that is a change of substance rather than a decision to stay silent:
   revision 2 was going to file "an agent writing into a declared-unneeded
   submodule directory is recorded nowhere" as a `TASKS.md` P3 line, and
   revision 3 **closes it** with M7's second reader instead. Post-seed that
   content is invisible to the diff, to `git status` and to the v2 stream, so
   filing it would have left a known-invisible, known-ungradable shape in the
   backlog while shipping the capture that was one exec away from seeing it.
   The pre-seed 199-byte gitlink deletion remains item 2's, named in D9 and
   routed to plan 2's planner; §0's precondition stops the implementer if it is
   still open, and only then does it go in `TASKS.md` **as an item-2 defect**,
   quoting M1 rows G and G′.
4. **`tasks/todo.md`**: review section for this item, in the file's established
   shape.

---

## 6. Verification

Run from `bakeoff/`, with its venv interpreter.

```bash
.venv/bin/python -m pytest tests/ -v
```
Expect **+21 tests** and 3 edited over whatever HEAD carries when this lands
(baseline at round start was `1539 passed, 62 deselected`; items 1–16 move it).
Zero failures — in particular the four existing doubles must be updated first,
or the checkpoint tests pass while silently exercising the containment path.

```bash
.venv/bin/python -m pytest -v -m "integration and task_image" \
    tests/test_integration_submodules.py --basetemp="$HOME/.cache/bakeoff-pytest"
```
`--basetemp` under `$HOME` is mandatory; the module docstring says why.

```bash
.venv/bin/python -m pytest -v -m integration \
    --basetemp="$HOME/.cache/bakeoff-pytest"
```

```bash
.venv/bin/python scripts/verify_logger.py      # GATE PASSED
```

```bash
.venv/bin/python scripts/mutation_check.py     # SOLO; nothing else in the tree
```
All existing anchors plus the eight new rows green.

```bash
.venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
    --task-set ~/.cache/bakeoff-probe/taskset \
    --tasks tomlkit-514-inline-table-comment-separator
```
Still GO.

**What §6 cannot cover, stated rather than left implicit.** None of the above
produces a `RunRecord`, so none of them can show whether this refusal
false-positives on a real task — a suite, a conftest, an `image.build` step or a
stray tool artefact inside an *initialised* submodule would refuse every run on
that task, permanently, with no evidence a reader could use to tell it from the
defect this item closes. `--preflight-only` cannot see that: it produces no
record and no grade. **M6 is the substitute** and it is a measurement, not a
gate: the corpus' one real submodule task (`tomlkit-514…`) is clean on all
three readers after its own suite runs. The standing protection is upstream and
unchanged — preflight's clean-tree NO-GO refuses a task whose suite dirties the
tree, and ` M <path>` is exactly the signal an in-submodule write produces. Any
task added later with an initialised submodule should have M6's three commands
run against it once, after the suite, before it is collected on.

**The second reader has the same exposure and the same guard, one door over.**
A suite, conftest or `image.build` step that writes into a *declared-unneeded*
submodule directory would make `submodules_dirty` carry `"?"` on every run of
that task, refusing all of them. `--preflight-only` cannot see that either —
but item 2's `evidence["submodules_empty_after_suite"]` can and does: it
measures that directory's emptiness per declared path, after the suite, at
preflight time, which is a NO-GO before any cell is spent. This plan adds no
screen because item 2's already covers it; what changes is that item 2's check
is now load-bearing at grade time as well, and that is worth saying in its
review log. No task in today's corpus declares an unneeded submodule at all.

```bash
graphify update .
```

---

## 7. What this does NOT do

- **It does not fix `snapshot_diff`'s spurious gitlink deletion** (M1 row G).
  That is item 2's, and §0 makes it a precondition. This plan is correct on
  both sides of it: D9's argument runs off the v2 stream's silence, not off
  which refusal takes the row.
- **It does not make the submission diff carry the edit.** No
  `--recurse-submodules`, no per-submodule `git diff` folded into
  `diff_vs_base`. Doing so would produce a patch that `git apply` cannot apply
  against a materialized tree (the submodule there is a fresh clone from a
  pruned mirror), which trades an honest refusal for an `APPLY_FAILED` — the
  accusation shape `grader._refresh_index`'s docstring exists about. The row
  stays out of the denominator; it does not become gradable.
- **It does not touch the pure-gitlink *rename* residual** (round-2 item 9,
  `_chunk_is_gitlink`'s docstring). Different authority, different chunk shape,
  its own plan.
- **It does not move `MIN_GRADABLE_SCHEMA`.** Every stored record stays
  gradable with `submodules_dirty_at_exit` absent, which `_submodule_edits`
  reads as "nobody looked" and not as "clean".
- **It does not read file *contents* anywhere.** The second reader tests one
  gitlink directory for emptiness and stops at the first entry; it never names
  a file, never diffs one, and cannot tell the agent's file from any other. The
  record says "this directory has content git will not report", and that is the
  whole claim.
- **It does not refuse anything at load or at preflight.** No task becomes
  uncuttable and no manifest key is added; `tasks.py`, `images.py` and
  `preflight.py` are untouched.
- **It does not add a per-turn submodule *diff*.** `submodules_dirty` is a
  state, not content — four characters per path, taken from git.
- **It does not change `ORACLE_VERSION`, the ladder, or any check.** The new
  refusal runs before the ladder and never inside it.
- **It does not change what `run_matrix` does with such a run.**
  `matrix.infra_problems` is untouched: a dirty submodule is an observation of
  the model, not an infrastructure failure, and folding it in would let one
  arm's habit trip `StreakTracker` and kill that arm's remaining cells under
  §5.7's round-major interleaving.

---

## 8. Sentences for `CLAUDE.md`

To the **"Checkpoint capture must never cost the run it is observing"**
invariant, at the end:

> The checkpoint also records what the diff cannot carry. Measured 2026-09-02
> (git 2.50.1) through `snapshot_diff`'s own command sequence: `git add -A`
> stages **zero bytes** for an uncommitted edit (`S.M.`), for an untracked file
> (`S..U`) and for a `rm` of tracked content inside an *initialised* submodule,
> while a commit inside one (`SC..`) stages a 245-byte gitlink chunk — so a
> submission diff is a complete description of the tree only outside gitlink
> boundaries, and a run that edited a submodule is byte-identical to one that
> did nothing. `Checkpoint.submodules_dirty` carries
> `git --no-optional-locks status --porcelain=v2 --ignore-submodules=none -z`'s
> four-character sub-state per gitlink path. Every token there is load-bearing:
> a plain `git status` **rewrites** `.git/index` *and* the submodule's, which is
> the one thing `SNAPSHOT_INDEX` exists to avoid; `--ignore-submodules=none` is
> what keeps a repository's own `.gitmodules` `ignore = all` from silencing the
> record; `git submodule status` is not an alternative, because it reports a
> **space** marker for every state the diff cannot carry and fires only on the
> one it can. The `-z` parse advances by **two** after a rename record, because
> the second item is a path and a path can be *named* so that it reads as a
> submodule record — same rule as `_GITLINK_MODE`'s, content never reaches
> column zero. **A second reader sits beside it**, because git sees only half
> of this: after item 2 seeds the snapshot index with `read-tree`, content an
> agent writes into an *uninitialised* submodule directory is invisible to the
> diff (0 bytes), to `git status` and to the v2 stream alike — recorded
> nowhere. So for each `160000` path from `git ls-files -s -z` whose directory
> carries no `.git`, a `find -mindepth 1 -maxdepth 1 -print -quit` that prints
> anything files the marker `"?"` (`schema.SUBMODULE_UNINITIALISED_CONTENT`).
> One character, never a forged four-character `S…` shape: git's sub-state is
> always four characters beginning `S`, so the marker says which reader spoke
> and keeps `len(state) == 4` a claim about git. The two readers are measured
> disjoint by the DIRECTORY test, not by the `.git` test: on a `.git`-less path
> v2 fires only when the directory is absent and the probe only when it is
> non-empty, so no path can be filed twice — and the probe does not run at all
> on a tree with no gitlinks.
> The whole read is contained inside `_capture`, not in `maybe_capture` — so a
> failure names itself in `checkpoint_error` on both paths, `force_capture` can
> never lose the submission to a supplementary observation, and a partial read
> is `None` rather than a `{}` that would read as "nothing is dirty".

Beside the gitlink sentence wherever it lands:

> Two submodule refusals sit before `materialize`, and they are disjoint by
> construction. `_gitlinks_touched` reads the **submission's** chunks and names
> a moved gitlink (`SUBMODULE_GITLINK_UNGRADABLE`); `_submodule_edits` reads
> the **record's** `submodules_dirty_at_exit` and names the `M`/`U` bits plus
> the `?` marker, which are the states `git add -A` stages nothing for. Without the second, that run
> lands as `EMPTY_PATCH` — a `GradeFailure`, so `resolved: False`, an
> accusation that the model changed nothing — and it is byte-identical to an
> honest empty run. `None` there is "nobody looked", not "clean": every stored
> record predates schema 3.9.0, so a fail-closed reading would refuse the whole
> corpus. The collapse is the *verdict's*; the record keeps the two apart.

---

## Review 1 → changes

14 findings, **14 addressed, 0 disputed**. Two carry a note where the fix went
further than, or differs in framing from, what the finding proposed.

| # | finding | change |
|---|---|---|
| 1 | **BLOCKER** — row G measured the repository index, not `snapshot_diff`'s scratch one; the residual it produced is false | *(This row records what revision 2 did; review 2 found it incomplete — see* **Review 2 → changes, R2-1** *for what revision 3 does instead.)* Re-measured: **199 bytes** (`deleted file mode 160000`) on a clean tree, **410** with a stray agent file (my `print('agent')` against the reviewer's shorter file — same shape as their 401). M1 gains rows G and G′ and a header sentence saying every diff column is `snapshot_diff`'s own sequence. §0 makes item 2's `snapshot_diff` fix a **precondition** and tells the implementer to stop if it is missing. D9 rewritten: the conclusion now runs off the v2 stream's **silence** on an uninitialised gitlink, which holds on **both sides** of item 2's fix — *note, this differs in framing from the finding's proposed reasoning ("`_gitlinks_touched` takes the row first"), which is true today and stops being true once item 2 lands*. Task 9.3 no longer adds a `TASKS.md` residual; it routes consequence (c) to item 2 by name, with the condition under which it becomes a `TASKS.md` line. §7 opens with "does not fix `snapshot_diff`". |
| 2 | mutation entry 4 is inert under the plan's own test | Made observable rather than dropped, on a new measurement (**M3b**): a tracked file *named* `1 .M S.M. 160000 160000 160000 aaaaaaa bbbbbbb sneakysub`, renamed away, puts that exact string in the rename's second NUL item — measured verbatim. Under `index += 1` the parser files `{"sneakysub": "S.M."}`; the parser test's canned stream now carries that item and asserts the result is exactly `{"vendor/libdep": "S.M."}`. The two-item consumption is now load-bearing, not defensive, and is documented as the same rule `_GITLINK_MODE` states. |
| 3 | entry 4's anchor is described, not written out | `_parse_status_v2` written out as code in D3. Anchor is `            index += 2`; uniqueness checked and stated (`index += 2` once, `index += 1` twice). |
| 4 | no exec-recording double exists in `tests/test_container.py` | `_ArgvRecorder` written out in Task 1.4 with its `argvs: list[list[str]]` recorder and its `ExecResult` return. |
| 5 | `_container_with` replaces `checked_exec`, so the third container test cannot pass | Task 1.4 now says the third test must **not** use `_container_with`, quotes the line that replaces the method, and gives the `exec`-only construction verbatim — noting it is the only construction that exercises D2's `checked_exec`-not-`exec` argument at all. |
| 6 | **BLOCKER** — three hard-coded version constants; `GRADE_SCHEMA_VERSION "1.4.0"` collides with item 8 | `GRADER_VERSION` and `GRADE_SCHEMA_VERSION` are now **read-and-increment** in D7, D10, the file table and Tasks 5.3/5.5/6.4/6.5, in item 8's and item 9's spelling. D7 names the item-8 collision explicitly, cites `round2-8:452`/`:722`, and notes that item 8's "no item ahead moves this constant" check is what item 17 invalidates. Both pin tests move to whatever results, not to a literal. `SCHEMA_VERSION` stays `"3.8.0"` → `"3.9.0"` (uncontested, per the reviewer's own check of the sixteen other plans) with a §0 confirm-on-disk step. |
| 7 | the 3.9.0 bump makes `schema_at_least`'s docstring wrong, and it is not in the file table | Added to D6, to the file table and to Task 5.4: *"two additive bumps from the wrap"* → **one**. Also recorded that there is no `SCHEMA_VERSION` pin test to edit. |
| 8 | `_submodules_at_exit`'s docstring overclaims what `checkpoints[-1]` is | Docstring rewritten: **last** capture, not final; explicit that a mid-loop crash leaves a mid-run snapshot there exactly as `artifacts.final_diff` does; points a reader needing "at exit" at `not_graded_gate`'s `checkpoints[-1].turn == turns_streamed` (`grader.py:585-600`). Task 3.2 carries the same wording into the `RunRecord` field comment. |
| 9 | `_record` needs no new keyword | Helper change dropped. Task 6.5 states `_record` is already `(diff=TEXT_DIFF, **kw)` with `fields.update(kw)` (`:234-265`), quotes its docstring's rule, and all five tests pass `submodules_dirty_at_exit=` by its real name. |
| 10 | the new integration record collides with `sub-int-002-empty` | Task 7 parameterises `_record_with_diff` with `run_id` / `collection_id` keywords whose defaults leave every existing call byte-identical, and the new test passes `run_id="sub-int-003-edit"`, `collection_id="integration-submodule-edit"`. |
| 11 | say which field the helper sets | Task 7 step 4 names `Checkpoint.submodules_dirty`; step 5 names `RunRecord.submodules_dirty_at_exit` and says the helper sets it **directly**, because the test does not run `assemble_record`. |
| 12 | §6 proves nothing about false positives | The reviewer's `tomlkit-514` measurement transcribed as **M6** with attribution. §6 gains a "**What §6 cannot cover**" paragraph saying `--preflight-only` produces no record and therefore cannot see this, naming M6 as the substitute, naming preflight's clean-tree NO-GO as the standing protection, and prescribing M6's three commands for any future task with an initialised submodule. |
| 13 | the `S..U` bit changes the *other* HARVESTING bullet's consequence | Put in the spec §5.6 edit (Task 9.2), now two paragraphs, where no other plan is editing — as the finding proposed. The `HARVESTING.md` bullet split is unchanged. |
| 14 | the checkpoint doubles are under-specified | Task 2.5 rewritten: `FakeContainer` gains a `self.states` set per test; `ExplodingContainer` gains `return {}` and keeps its diff-raising body; a third double `_StatesRaise` is written out with its exact `ContainerError("boom")`, which the two error-path tests assert verbatim. |

**Rulings adopted.** Q1 — `{}`/`None` as written, with the caveat now stated in
D8's code comment (*the collapse is the verdict's, not the record's*) and
repeated in the `CLAUDE.md` sentences. Q2 — gitlink first, with finding 1's
fact added to the order comment. Q3 — the field, with the reviewer's first
reason (*the harness records, the grader derives*) added to D5 ahead of the
original one, and subject to finding 8. Q4 — task image, with the
`RunContainer.__enter__` precondition argument written into Task 7 step 2.
Q5 — no, with the `StreakTracker` consequence written into §7.

---

## Review 2 → changes

2 open findings, **both addressed**; R2-1's fix is deliberately stronger than
the one proposed, on the coordinator's direction.

### R2-1 — rows G/G′ are pre-seed presented as post-seed

Re-measured on my own fixture and the reviewer's numbers reproduce exactly:

```
G   uninitialised, clean      today: 199 bytes    seeded (read-tree): 0 bytes
G′  uninitialised + a file    today: 410 bytes    seeded (read-tree): 0 bytes
```

All four inverted places fixed:

| place | change |
|---|---|
| **M1** | Rows G/G′ now carry `pre-seed … → post-seed 0`, with the two-line measurement block below the table and the pre/post definitions written out. Added the reviewer's own observation that **rows A–F and an ordinary modify+add+delete are byte-identical across the seed** (391 either way) — the cheapest defence of §0, as they said. |
| **D9** | Rewritten end to end. The *"it is not invisible"* paragraph is gone; the new text says the claim is true of **today's** `snapshot_diff` and false of the one §0 mandates, and names that as the error revision 2 preserved. |
| **§7** | The *"the diff is not, and the two describe different halves"* bullet is deleted — it is backwards post-seed. Replaced with a true scope limit: the second reader tests emptiness and never reads file contents. |
| **Task 1.3** | Now says **0 and 245 only** go into `submodule_states`' docstring, and that the pre-seed 199/410 **must not be written into source**, citing §0 and the repo's verified-against convention. |

**Task 9.3 — the residual is closed, not restored.** R2-1 asked me to restore
the `TASKS.md` line, because post-seed it becomes true: an agent writing into a
declared-unneeded submodule directory is recorded nowhere. Per the
coordinator's direction I extended the capture to **see** it instead, which is
strictly stronger — filing a known-invisible, known-ungradable shape in the
backlog while shipping a capture that was one exec away from catching it is the
trade this whole item exists to refuse. New in revision 3:

* **M7**, the second reader, measured across five tree states plus a
  space-containing path plus cost. Both readers measured **disjoint**.
* **D1** gains `_GITLINK_ARGV`, `_UNINITIALISED_CONTENT_SH` (written out, every
  branch justified) and `schema.SUBMODULE_UNINITIALISED_CONTENT`.
* **D2** merges the two readers, short-circuits when there are no gitlinks, and
  states the one-containment-one-result rule.
* **D3** gains `_gitlinks_from_ls_files`.
* **D8**'s predicate gains the `?` disjunct, first and as an equality.
* **+5 tests** (4 container, 1 grader) and **+2 mutation rows** — capture and
  verdict anchored separately, because either one alone silently restores the
  gap.
* **§6** records that a false positive here is guarded by item 2's
  `evidence["submodules_empty_after_suite"]`, the same shape as M6.

**The marker is `"?"`.** One character, and deliberately not `"S..U"`-shaped:
git's sub-state is always exactly four characters beginning with `S` and
`_parse_status_v2` files nothing else, so a reader can always tell which reader
spoke, and `_submodule_edits`' `len(state) == 4` stays a claim about what *git*
said. A forged `S..U` would pass that guard silently — the
configuration-reported-as-observation family, and the same rule `_GITLINK_MODE`
states one level down. `?` borrows git's own porcelain vocabulary for
untracked, which is what this is.

### R2-2 — stale line citations

Every "read line N" instruction is now a grep, which is stable under exactly
the churn this item is sequenced behind. §0 precondition 2 carries the four
commands; D6, D7, D10 and Tasks 5.3/5.5/6.4/6.5 cite by constant name. D10 and
Task 6.5 additionally record the measured fact that `GRADER_VERSION` is already
`"10"` while `tests/test_grader.py`'s pin still asserts `"9"` — so the pin may
be stale on arrival. Item 8 is re-cited by section heading rather than by line.

---

## Review 3 → changes

2 findings, **both addressed**. Both are documentation accuracy; no design,
code shape, predicate, anchor or test-count change follows from either, which
matches review 3's own reading. Re-measured on my fixture before editing:

```
gitlink directory REMOVED:   v2 -> 1 .D S... 160000 160000 000000 <sha> <sha> vendor/libdep
                             snapshot_diff -> 199 bytes, `deleted file mode 160000`
                                              IDENTICAL today and seeded
edit + untracked inside:     v2 -> 1 .M S.MU ...
commit + edit + untracked:   v2 -> 1 .M SCMU ...
```

Review 3's 402 bytes and my 199 are the same measurement: one
`deleted file mode 160000` chunk **per gitlink**, on their two-gitlink fixture
and my one-gitlink one. The plan now says "per gitlink" and gives both.

### R3-1 — M7 row 5's v2 cell, and the disjointness argument built on it

| place | change |
|---|---|
| **M7 table** | Numbered, and gains a `directory` column and a `snapshot_diff` column. Row 5's v2 cell is corrected from *(empty)* to `1 .D S... … vendor/libdep`, with **199 bytes per gitlink, seed-invariant**. |
| **M7 prose** | The false sentence is gone. Disjointness is restated as the measured **directory** rule — on a `.git`-less path v2 fires only when the directory is **absent** and the probe only when it is **non-empty**, mutually exclusive — with the `.git` test demoted to what makes the probe *cheap* rather than what makes the readers disjoint. |
| **M7 prose** | New paragraph: row 5 is a **third** disjoint case belonging to neither reader's refusal. `_gitlinks_touched` takes it on the deletion chunk, and the mechanism is the same directory test — with the gitlink in the seeded index, `git add -A` leaves a present-but-empty directory alone (row 2, 0 bytes) and records a deletion for an absent one (row 5, 199). That is why row 5's bytes are seed-invariant while row G's are not. |
| **D1** | The two-reader paragraph now states the directory rule instead of the `.git` rule. |
| **D2** | The merge note likewise: "measured across all five states in M7", with the mutually-exclusive conditions spelled out. |
| **D9** | Citation corrected. It now reads "an uninitialised gitlink whose directory is **present** — empty or not (M1 rows G/G′, M7 rows 1 and 2)", with a parenthesis saying row 5 is the removed-directory shape and belongs to `_gitlinks_touched`. |
| **Task 1.3** | Rewritten as a bullet list, and now tells the implementer **not** to write the `.git` version of the disjointness sentence, naming it as the claim review 3 measured false. |

The 199/402 collision is called out explicitly in Task 1.3 rather than left to
be discovered: **199 means two different things.** It is a removed gitlink
directory's deletion chunk (row 5, true seeded and unseeded) *and* a
present-but-empty one's under the pre-seed code only (row G, 0 after the seed).
Row 5's 199 may go into source; row G's may not.

### R3-2 — the sub-state enumeration is not exhaustive and was headed for a docstring

Measured `S.MU` and `SCMU` directly, and `S...` falls out of R3-1's row 5.
Seven of the grammar's eight values are now measured; only `SC.U` is
unconstructed.

| place | change |
|---|---|
| **D2** | *"the raw four-character field verbatim — `S.M.`, `S..U`, `SC..`, `SCM.`"* → *"verbatim and **unenumerated**"*, with the value space given as git's `S<c><m><u>` grammar — eight values, seven measured, listed as examples — and the reason spelled out: `_parse_status_v2` files whatever git printed, so a four-item list is a closed-world claim the code does not make. |
| **D8** | *"The one bit NOT here is `C`"* → *"the states with neither `M` nor `U` set"*, naming both `SC..` and `S...` and what the diff carries for each. A second paragraph says the predicate tests the two **bits** and never a list of spellings, and that `S.MU`/`SCMU` refuse on `M` exactly as `S.M.` does. |
| **Task 1.3** | The docstring instruction carries the grammar-with-examples rule, so the non-exhaustive list cannot be transcribed into source. |
| **`tests/test_grader.py`** | `test_a_commit_only_state_is_left_to_the_gitlink_refusal` is renamed `test_the_states_the_diff_carries_are_left_to_the_gitlink_refusal` and now asserts `()` for **both** `SC..` and `S...`. Mutation row 6's selector follows the rename (`-k left_to_the_gitlink_refusal`); it still goes red, on the `SC..` half. |

No verdict changes: `S...` → `len == 4`, neither bit set → `()`, and
`_gitlinks_touched` refuses the row on the deletion chunk. `S.MU` and `SCMU`
refuse on `M`. The predicate was already right; only what the plan said about
its input was wrong.

### Ruling noted

The `"?"` marker is kept as written, per review 3's ruling and its three
reasons — the third (the leading equality means `"?"` never reaches the index
tests, so the ordering is legibility rather than safety) is exactly what D8
claims. Review 3's aside that a `#`-style prefix would avoid the pun with v2's
own `?` record type is noted and not taken: `_parse_status_v2` files only
values beginning `S` and skips `? ` records, so there is nothing to collide
with, and changing a marker that is already argued for in three places would
cost more than the pun does.
