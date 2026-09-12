# Round 2, item 16 — relative `.gitmodules` urls

**Status.** Plan v1, 2026-09-02. Written against worktree
`/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden`, branch
`broaden-taskset`, round base `8232032`.

**Sequencing.** Implements **after item 2** (`submodules_unneeded` /
`needed_gitlinks` / `Submodule.declared_unneeded`) **and after item 10** (the
`remote remove` / `reflog expire` asymmetry — TASKS.md's ruling there is
*"leave the code as it is"*, so item 10 is expected to land as a measurement
and a docs/comment change inside `_init_submodules`; this plan edits two lines
of the same function and must be rebased onto whatever it wrote). Item 2 is a
hard prerequisite, not a courtesy: **an unneeded submodule's url is never
resolved**, so every rule below applies to *needed* gitlinks only, and the
`declared_unneeded` field this plan branches on is item 2's.

**One-line summary.** `derive_submodules` resolves a relative `.gitmodules`
url against `task.repo_url` by git's own rules, refuses every shape it cannot
resolve to a fetchable url, records **both** the raw and the resolved value on
the `Submodule` record and in preflight's evidence, and keys the pruned mirror
and the run tree's persisted url on the **resolved** one.

---

## The measured defect

`bakeoff/src/bakeoff/tasks.py:1713` refuses any submodule url that does not
start with `_SUBMODULE_URL_PREFIX` (`"https://"`), and the constant's comment
(`tasks.py:303-308`) gives relative urls as the first reason:

```
#: The only submodule url shape the eval can fetch. A relative url ("../x.git")
#: resolves against the superproject's remote, which `materialize` deletes and
#: the container cannot reach; ssh needs keys the eval does not carry; file://
#: points at the task author's laptop.
```

The premise is correct as far as it goes and is re-measured below (M2:
git warns *"could not look up configuration 'remote.origin.url'. Assuming this
repository is its own authoritative upstream"* and resolves against the run
tree's own **host path**, then fails to clone). What the refusal misses is that
the harness does not need a remote at all: `task.repo_url` is the
superproject's url, it is in the manifest, it is offline, and it is the exact
value git would have read out of `remote.origin.url` in a normal clone.

TASKS.md records this as a deferral rather than a defect (broadening 6), with
the deferral's own condition attached:

> **Relative `.gitmodules` urls.** git resolves `../toml-test.git` against the
> superproject's own remote. The derivation could resolve it against
> `task.repo_url` instead, which is well-defined and offline — but the
> resolution rules (`./`, `../` chains, a trailing `.git`, a `repo_url` with
> or without a trailing slash) need measuring before they are written, and the
> refusal is correct in the meantime. The likeliest thing to block a real
> repository.

This plan closes the deferral by supplying the measurement. **The blast radius
is repository eligibility, not correctness of any recorded run**: no task in
the corpus or in `~/.cache/bakeoff-probe/taskset/` declares a relative
submodule url (tomlkit's `tests/toml-test` is
`https://github.com/BurntSushi/toml-test.git`; eemeli/yaml's four are all
`https://`; sqlglot's is `git@github.com:…`, which stays refused). So nothing
already gated changes verdict, and the whole of the change is measured against
fixtures plus one new integration task.

---

## Measurements

All taken 2026-09-02 on **git 2.50.1 (Apple Git-155)**, in scratch repositories
under `$HOME/.cache/bakeoff-scratch/relsub/` (scripts `measure.sh`,
`measure2.sh`, `measure3.sh`, `measure4.sh` in that directory), with
`GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null` so no operator config
contributes. Method: build a superproject with a real gitlink, rewrite the
committed `.gitmodules` url to the shape under test, commit, remove the
submodule's `.git/config` registration, set `remote.origin.url`, run
`git submodule init`, and read back `git config --get submodule.sub.url` —
which is **git's own resolution**, written by the same code path
`git submodule update --init` uses.

### R — what git resolves a relative url to

| row | `remote.origin.url` | `.gitmodules` url | git's resolution |
|---|---|---|---|
| A | `https://github.com/org/super.git` | `../sub.git` | `https://github.com/org/sub.git` |
| B | `https://github.com/org/super.git` | `../sub` | `https://github.com/org/sub` |
| C | `https://github.com/org/super.git` | `./sub.git` | `https://github.com/org/super.git/sub.git` |
| D | `https://github.com/org/super` | `../sub.git` | `https://github.com/org/sub.git` |
| E | `https://github.com/org/super/` | `../sub.git` | `https://github.com/org/sub.git` |
| F | `https://github.com/org/super.git` | `../../other/sub.git` | `https://github.com/other/sub.git` |
| G | `https://github.com/org/super.git` | `../../../sub.git` | `https://sub.git` |
| H | `https://github.com/org/super.git` | `../../../../sub.git` | `https:/sub.git` |
| I | `https://github.com/org/super.git` | `../a/../b.git` | `https://github.com/org/a/../b.git` |
| J | *(no `origin` at all)* | `../sub.git` | `<superproject's own path>/../sub.git`, i.e. `…/run/sub.git` |
| K | `https://github.com/org/super.git?x=1` | `../sub.git` | `https://github.com/org/sub.git` |
| L | `https://github.com/org/super.git#frag` | `../sub.git` | `https://github.com/org/sub.git` |
| M | `git@github.com:org/super.git` | `../sub.git` | `git@github.com:org/sub.git` |
| N | `https://user@github.com/org/super.git` | `../sub.git` | `https://user@github.com/org/sub.git` |
| O | `https://github.com/org/super.git` | `../sub.git/` | `https://github.com/org/sub.git` |
| O2 | `https://github.com/org/super.git` | `../sub.git//` | `https://github.com/org/sub.git/` |
| P | `https://github.com/org/super.git` | `../../org2/sub.git` | `https://github.com/org2/sub.git` |
| Q | `https://github.com/org/super.git` | `../sub.git` *(gitlink at `vendor/deep/sub`)* | `https://github.com/org/sub.git` |
| R | `https://github.com/org/super.git` | `..//sub.git` | `https://github.com/org//sub.git` |
| S | `https://github.com/org/super.git` | `../` | `https://github.com/org/` |
| T | `https://github.com/org/super.git` | `..` | `..` *(unchanged — not treated as relative)* |
| U | `https://github.com/org/super.git` | `../SUB.git` | `https://github.com/org/SUB.git` |
| V | `https://github.com/org/sub.git` | `./` | `https://github.com/org/sub.git/` |
| W | `http://github.com/org/super.git` | `../sub.git` | `http://github.com/org/sub.git` |
| X | `https://github.com/org/super.git/` | `../sub.git` | `https://github.com/org/sub.git` |
| Y | `https://github.com/org/super.git` | `./sub.git/` | `https://github.com/org/super.git/sub.git` |
| Z | `https://github.com/org/super.git` | `../a/b.git` | `https://github.com/org/a/b.git` |
| N1 | `https://github.com` *(no path)* | `../sub.git` | `https://sub.git` |
| SSH | `ssh://git@github.com/org/super.git` | `../sub.git` | `ssh://git@github.com/org/sub.git` |
| SSH2 | `ssh://git@github.com:22/org/super.git` | `../sub.git` | `ssh://git@github.com:22/org/sub.git` |
| PORT | `https://github.com:8443/org/super.git` | `../sub.git` | `https://github.com:8443/org/sub.git` |
| DOTA | `https://github.com/org/super.git` | `../a/./b.git` | `https://github.com/org/a/./b.git` |
| DOTB | `https://github.com/org/super.git` | `.././sub.git` | `https://github.com/org/sub.git` |

**The nine facts this table settles**, in the order the design uses them:

1. **git does not "strip a trailing `.git`".** It pops a whole `/`-separated
   segment per `../`, and `.git` is simply part of the segment that goes (A vs
   D: both give `…/sub.git`, because in A the popped segment was `super.git`
   and in D it was `super`). Nothing is appended either — B keeps `sub` with no
   `.git`. **The TASKS.md question "strip trailing `.git`?" is answered no**:
   there is no `.git` rule at all, only segment arithmetic.
2. **`./` does not pop.** It appends to the *whole* base, `super.git` included
   (C, Y). A `./` url is legal and rare; git's answer is well-defined and this
   plan reproduces it rather than refusing it.
3. **A trailing slash on `repo_url` is absorbed** (E, X give the same answer as
   A).
4. **The `../` chain will eat the host and then the scheme** (G, H, N1). Those
   are the shapes the design refuses: `https://sub.git` names a host called
   `sub.git`, and `https:/sub.git` is not a url at all. git produces both
   silently, at exit 0.
5. **Interior `..` is not normalised** (I): the result carries a literal
   `a/../b.git` path component.
6. **A non-`https` base resolves to a non-`https` result** (M scp-style, W
   `http://`), so the eval's existing prefix refusal is still the thing that
   judges the *result*.
7. **Query and fragment on the base are silently swallowed when the `../`
   chain pops the segment carrying them** (K, L) — and would be embedded
   mid-url by a `./`.
8. **Empty and trailing-slash remainders are inconsistent even in git**: O
   consumes one trailing slash, O2 keeps the second, S and V keep theirs. Those
   shapes name a directory, not a repository.
9. **A bare `..` is not a relative url** (T): git's trigger is the two-string
   prefix set `("./", "../")`, and `..` matches neither, so the value is stored
   verbatim.
10. **A port and a userinfo live in the authority and are carried through**
   (PORT, SSH2, N), which is why the resolver's colon refusal is on the
   *remainder* only — a `repo_url` with a port resolves normally.
11. **`ssh://` resolves cleanly** (SSH) and is refused one check later for its
   scheme, which is the shape a real corpus repository is most likely to carry.
   The scp-style `git@host:path` (M) is a different thing: it has no `://`, so
   the resolver refuses it itself.
12. **A mixed chain works** (DOTB, `.././sub.git` → one pop then an append),
   and an interior `./` does not (DOTA) — git copies that one through verbatim.

### M — the harness path

| # | what was measured | result |
|---|---|---|
| M1 | `submodule.<n>.url` preset in `.git/config` to a local mirror, `.gitmodules` relative, **no origin** → `git submodule update --init` | exit 0, cloned from the config value; git never consults the relative url. `git config --get` after = the mirror path. |
| M1b | `git submodule status` after rewriting the persisted url to (a) the mirror path, (b) the raw `../sub.git`, (c) a resolved `https://…` | leading **space** in all three. The persisted url does not affect preflight's marker. |
| M2 | today's shape: no config url, no origin, relative `.gitmodules` → `update --init` | exit 1. `warning: could not look up configuration 'remote.origin.url'. Assuming this repository is its own authoritative upstream.` then `fatal: repository '<host path>/sub.git' does not exist`, twice, then `Failed to clone 'sub' a second time, aborting`. `.git/config` is left holding the **host path**. |
| M3 | `git config --blob <sha>:.gitmodules --list -z` (the reader in `derive_submodules`) on a relative url | returns the **raw** value `../sub.git`. The derivation sees the manifest-adjacent fact, unresolved. |
| M4 | preset config url + origin present → `update --init` | config after = the preset value, unchanged. Presetting always wins. |
| M5 | origin present and reachable (a local bare repo), no preset → `update --init` | exit 0, cloned from **git's own resolution**, config after = the resolved url. This is the behaviour being reproduced offline. |
| N2 | run tree with a relative `.gitmodules`, no origin, persisted url rewritten to a resolved `https://…`, then **`git submodule sync`** | exit 0, `Synchronizing submodule url for 'sub'`, and the persisted url is rewritten to the **host path** `<…>/run4/sub.git`. |
| N3 | `git config --local --get-regexp -z '^submodule\..*\.url$'` in a run tree | exit 0, `<key>\n<value>\0` records — the same shape preflight's orphan read already parses. |
| N4 | the same command in a repo with no submodule config | exit **1** (git config's ordinary "no key matched"). |
| N5 | `git show HEAD:.gitmodules` after every rewrite above | still `url = ../sub.git`. The tracked blob is never touched; only `.git/config` moves. |

M1 is the reason this item is a **capability** change and not a bug fix: today
a relative-url task is refused at derivation, and if the refusal were simply deleted
the clone would still succeed, because `_init_submodules` presets the url. What
would break is `ensure_pruned_mirror(sub.url, …)` — `git clone --mirror
../sub.git` — and the persisted value the run tree ends up carrying. Those are
exactly the two places this plan switches to the resolved url.

---

## Design decisions, settled

### D1. `Submodule.url` is **renamed**, not supplemented

```python
@dataclass(frozen=True)
class Submodule:
    name: str
    path: str
    url_declared: str
    url_resolved: str | None
    sha: str
```

`url` disappears. **Counted at round base `8232032`**: exactly one
construction site (`derive_submodules`), four reads of `sub.url` in shipping
code (`tasks.py:1712`, `1713`, `2415`, `2424`; plus `images.py:436`), and one
in the tests (`test_tasks.py:2537`) — measured with
`grep -rn 'Submodule(' bakeoff --include='*.py'` (1 hit) and
`grep -rn '\.url' bakeoff/src/bakeoff` (5 hits, one of them a docstring).

**Re-count after item 2 has landed; do not transcribe those numbers.** Item 2
adds at least three more test reads — its
`test_a_declared_unneeded_submodule_is_not_refused_for_its_url`
(`url == "git@example.invalid:x/y.git"`),
`test_a_declared_unneeded_gitlink_survives_an_unreadable_gitmodules`
(`url == ""`) and
`test_a_declared_unneeded_gitlink_with_no_stanza_in_a_readable_gitmodules`
(`url == ""`) — so expect **four** test reads, not one. Task 2 Step 7 states
the grep to run.

Adding `url_resolved` beside a kept `url` was rejected. A missed call site would
then keep compiling and would key a pruned mirror on `../sub.git` — `mirror_path`
slugs that to `--sub.git` and `git clone --mirror ../sub.git` fails with a bare
`CalledProcessError`, which is the "loader refusing a manifest wearing the
costume of a harness crash" shape `_init_submodules`' own comments already
argue against. After the rename a missed call site is an `AttributeError` on a
frozen dataclass at derivation time — immediate, and it names the attribute.

**`url_resolved` is `str | None`, and the `None` is item 2's.** Three values,
three meanings, and the repo's "a null says which kind of null it is" rule is
why they are not two:

- a string — this url was resolved (it equals `url_declared` whenever the
  declared url was already absolute, which is every task in today's corpus);
- `""` — the stanza declared **no url**; `"".startswith(("./", "../"))` is
  `False`, so the resolver returns it unchanged and the existing *"no url"*
  refusal fires with its message untouched;
- `None` — **this submodule is declared unneeded (item 2) and was never
  resolved.** Nothing fetches it, no mirror is keyed on it, and no url needs to
  exist for it (item 2's D4 rows 1 and 2 exempt it from both `.gitmodules`
  refusals, so `url_declared` may itself be `""` with no stanza behind it).

`url_declared` is the verbatim `.gitmodules` value and its docstring keeps
saying so — N5 measures that the blob is never rewritten, which is what makes
"the raw is the manifest-adjacent fact" true rather than aspirational.

### D2. The resolver takes strings, not the manifest

```python
def _resolve_submodule_url(repo_url: str, declared: str, *,
                           task_id: str, path: str) -> str:
```

`task_id` and `path` are message-only. A `TaskManifest` parameter would make
every one of the ~22 rule/refusal tests build a manifest, a fixture repository
and a reference diff to exercise a pure function of two strings; taking the
strings makes each test three lines and no fixture, which is the same shape
`_chunk_path` and `_under` already have in this module. `derive_submodules`
passes `task.repo_url`, `task.task_id` and the gitlink path.

### D3. The algorithm, and why it is not a general reimplementation of git

```python
#: gitmodules(5)'s two relative forms, and git's own trigger: a bare `..`
#: matches neither and is stored verbatim (measured 2026-09-02, git 2.50.1,
#: row T), which is why the tuple carries the slashes.
_RELATIVE_URL_PREFIXES = ("./", "../")
```

1. **Not relative → returned unchanged.** `declared.startswith(
   _RELATIVE_URL_PREFIXES)` is git's exact trigger. `""`, `"https://…"`,
   `"git@…"` and `".."` all fall through here, and the existing
   `_SUBMODULE_URL_PREFIX` refusal judges them as it does today.
2. **The base is split into an immovable prefix and poppable segments.**
   `repo_url` is partitioned on `"://"`. With a separator, the prefix is
   `<scheme>://<authority>` and the rest is the path; without one, the base
   must start with `/` and the prefix is `""` (the leading slash is restored by
   the join). Anything else — an scp-style `git@host:path` (row M), a bare
   `org/super.git` — is refused, naming `repo:url`.
3. **The base's path is `rstrip("/")`ed** (rows E, X) and split on `/`. An
   empty path refuses ("no path to resolve against", row N1's shape); an empty
   interior segment refuses.
4. **The chain is walked left to right.** `./` consumes two characters and pops
   nothing (rows C, Y). `../` consumes three and pops one segment — **and the
   emptiness guard is checked before the pop, not after**, which is precisely
   what makes row F (two pops from two segments → `https://github.com/other/
   sub.git`, identical to git) legal and row G (three pops from two) a refusal.
5. **The remainder is validated**: non-empty (rows S, V), no trailing `/` (rows
   O, O2), no empty/`.`/`..` component (rows R, I), and none of
   `? # \ :` or whitespace.
6. **The join is `"/".join([prefix, *segments, remainder])`** — uniform across
   both prefix shapes, because the local-path prefix is `""` and contributes
   the leading slash.

Reproduced exactly: A, B, C, D, E, F, N, P, Q, T, U, W, X, Z, SSH, SSH2, PORT,
DOTB (the classification table below partitions all 33 rows).
Deliberately divergent: G, H, N1 (climb past the host — refused instead of a
nonsense url); I, R, DOTA (malformed remainder — refused instead of passed
through); K, L (query or fragment on the base — refused instead of silently
swallowed); O, O2, Y, S, V (directory-shaped result — refused, and git's own
answers disagree between one trailing slash and two, which is itself the
argument). **M** too (an scp-style `repo_url` has no `://` and is not absolute, so the
resolver refuses it in step 2 with *"is neither a `scheme://` url nor an
absolute path"* — the first draft of this paragraph filed it under
"refused one check later", which is wrong: test 18 asserts the resolver's own
message). Not reachable: J (no remote is ever consulted). **Resolved, then
refused one check later by `_SUBMODULE_URL_PREFIX`, unchanged: W (`http://`)
and `ssh://`** — measured, `ssh://git@github.com/org/super.git` + `../sub.git`
resolves cleanly to `ssh://git@github.com/org/sub.git`, and with a port
(`ssh://git@github.com:22/…`) to `ssh://git@github.com:22/org/sub.git`. That
is the shape a real corpus repository is most likely to carry, and it is the
one that demonstrates D4: the resolver does the arithmetic and the prefix
constant makes the judgement.

**The algorithm above was executed against every row of table R before this
plan was written**, and the useful statement is the classification rather than
a raw pass count, because "agreement" is not defined for the rows the design
deliberately diverges on. **The classification partitions all 33 rows, each row
in exactly one class:**

| class | n | rows |
|---|---|---|
| reproduced identically | **18** | A, B, C, D, E, F, N, P, Q, T, U, W, X, Z, SSH, SSH2, PORT, DOTB |
| deliberately divergent (refused where git answers) | **13** | G, H, N1, I, R, DOTA, K, L, O, O2, Y, S, V |
| refused inside the resolver | **1** | M |
| unreachable (no remote is ever consulted) | **1** | J |

18 + 13 + 1 + 1 = **33**. Three of the *reproduced* rows — **W, SSH, SSH2** —
are then refused one check later by `_SUBMODULE_URL_PREFIX` for their scheme;
that is a fact about those rows, not a fourth class, which is why they sit on
exactly one side of the partition. Two further shapes were checked and are
**not table rows**: an absolute local-path base (`/tmp/a/super` + `../lib` →
`/tmp/a/lib`, the fixture branch) and an already-absolute submodule url
(returned unchanged). Row **T** is in the reproduced class on purpose: git
stores `..` verbatim because it matches neither relative prefix, and so does
the resolver.

Zero accept-direction divergences: there is no input the resolver accepts that
git resolves to something else. An implementer transcribing Task 1 Step 2 is
transcribing code that has been run against the measurements.

**A general `relative_url()` port was rejected.** git's function is defined to
produce *something* for every input — that is why G, H and N1 exit 0 — and a
port would import that property into a loader whose entire job is to refuse a
manifest that cannot be run. The narrow resolver above is a *superset of
refusals* over git: everything it accepts, git accepts identically; everything
it refuses, git accepts and hands back a url the eval would then fail to clone
in the middle of a matrix.

### D4. The scheme is judged **once**, by the refusal that already exists

The resolver checks no scheme. `_refuse_submodule_conflicts`'s
`_SUBMODULE_URL_PREFIX` check reads `sub.url_resolved` and judges the *result*
— so row W (`http://` base) resolves cleanly and is then refused for its
scheme, with the message naming both values (D5). Three reasons:

- one place decides what is fetchable, so a future change to the constant
  cannot leave two checks disagreeing;
- the tests' relaxation is untouched. `_SUBMODULE_URL_PREFIX = ""` is
  monkeypatched in four test modules (`test_tasks.py:185`,
  `test_images.py:366/384/416`, `test_integration_submodules.py:208`) so
  fixtures can use local paths as urls; the resolver's step-2 branch accepts an
  absolute local path as a base, so a **relative-url fixture works under the
  same single relaxation** and no new test-only flag is introduced — the rule
  `derive_submodules`' docstring already states;
- a second scheme constant inside the resolver would be configuration
  duplicated, which is the class of thing this codebase spends its comments
  refusing.

### D5. The url refusal's message and the constant's comment both change, because both are now false

`tasks.py:1712-1718` today says *"A relative url resolves against a remote the
run tree does not have"*. That sentence becomes untrue the moment this lands.
Replacement, verbatim:

```python
        if sub.url_resolved is None:
            stated = "no url (declared unneeded)"
        elif not sub.url_declared:
            stated = "no url"
        elif sub.url_resolved == sub.url_declared:
            stated = f"url {sub.url_declared!r}"
        else:
            stated = (f"url {sub.url_declared!r}, which resolves against "
                      f"{task.repo_url!r} to {sub.url_resolved!r}")
```

and **the `if` line, which D5's first draft left out entirely** -- the
shipping check is two statements and only one of them was being replaced, so a
faithful transcription left `sub.url.startswith(...)` in place against a field
that no longer exists:

```python
        if not (sub.url_resolved or "").startswith(_SUBMODULE_URL_PREFIX):
            raise TaskError(
                f"{task.task_id}: submodule {sub.path} declares {stated}; only "
                f"{_SUBMODULE_URL_PREFIX} urls can be fetched by this eval. A "
                "relative url is resolved against repo.url first, and it is "
                "the RESULT that is judged; ssh, http and file urls cannot be "
                "fetched at all."
            )
```

`(sub.url_resolved or "")` is what makes the three-way `stated` reachable:
both `""` (the stanza declared no url) and `None` (declared unneeded, never
resolved) fall through to the raise, and `stated` is what tells them apart in
the message. Writing `sub.url_resolved.startswith(...)` instead would raise
`AttributeError: 'NoneType' object has no attribute 'startswith'` on the very
line after `stated` was computed -- the sentence built and then thrown away,
which is what the first draft of D5 actually specified. The
`sub.url_resolved is None` branch is unreachable while item 2's guard skips
this whole check for a declared-unneeded submodule, and both it and the `or ""`
are written anyway so that a future editor who moves that guard gets the
sentence rather than the traceback.

`_SUBMODULE_URL_PREFIX`'s comment block (`tasks.py:303-308`) is replaced with:

```python
#: The only submodule url shape the eval can fetch, judged against the
#: RESOLVED url. A relative url ("../x.git") is resolved against
#: `task.repo_url` by `_resolve_submodule_url` -- git resolves it against
#: `remote.origin.url`, which `materialize` deletes, and git then falls back to
#: the superproject's own host path (measured 2026-09-02, git 2.50.1: a warning
#: and a clone of `<run tree>/../x.git` that does not exist). ssh needs keys the
#: eval does not carry; file:// points at the task author's laptop. Refused at
#: derivation so the failure names the manifest, not `git submodule update`'s
#: clone error.
```

### D6. Resolution happens at tuple construction, after both `.gitmodules` refusals

In `derive_submodules`, at the `subs = tuple(...)` comprehension, which sits
after item 2's typo refusal, after the unreadable-`.gitmodules` raise and after
the `unfetchable` raise. That order is the ruling, and a test pins it: an author
whose gitlink has **no url at all** must be told that, not told that `""` does
not resolve. (`""` would in fact fall straight through the resolver unchanged —
D1 — so the ordering is belt and braces, and the test says which belt.)

Because the comprehension is a single expression today, it becomes a loop so the
`declared_unneeded` branch is readable:

```python
    unneeded = set(task.submodules_unneeded)      # item 2's key
    subs_list: list[Submodule] = []
    for path, sha in sorted(gitlinks.items()):
        # Item 2's `by_path.get(path, (path, {}))`, bound ONCE instead of
        # evaluated twice: a declared-unneeded gitlink may have no stanza at
        # all (item 2's D4 rows 1 and 2).
        name, entry = by_path.get(path, (path, {}))
        declared = entry.get("url", "")
        subs_list.append(Submodule(
            name=name, path=path, sha=sha, url_declared=declared,
            # ITEM 2'S FIELD, and it must be written explicitly. Its default is
            # `False`, so omitting this keyword compiles, constructs, and
            # silently reverts the whole of item 2 -- the url-refusal guard,
            # `_init_submodules`' filter, `_extract_submodules`' skip and
            # preflight's GO. `unneeded` is read once above and used TWICE:
            # here, and for `url_resolved` on the next line.
            declared_unneeded=path in unneeded,
            # NEVER resolved for a declared-unneeded path (item 2). Nothing
            # fetches it, so no url has to exist -- and item 2's D4 exempts it
            # from both `.gitmodules` refusals, so `declared` here may be ""
            # with no stanza behind it at all. `None` is "not resolved", which
            # is a different absence from `""` ("declared no url").
            url_resolved=None if path in unneeded else _resolve_submodule_url(
                task.repo_url, declared, task_id=task.task_id, path=path),
        ))
    subs = tuple(subs_list)
```

*Implementer note.* `by_path.get(path, (path, {}))` is **item 2's**
expression, verbatim (item 2 Task 1 Step 5(c) evaluates it twice inside a
comprehension; this loop binds it once, which is the only difference). If item
2 landed it in a different form, keep that form and change nothing but the
keyword arguments. **`declared_unneeded=path in unneeded` is not optional and
is not new behaviour** -- it is item 2's own line, carried across the
comprehension-to-loop rewrite. Item 2's field defaults to `False`, so dropping
it produces a program that compiles and constructs while every submodule reads
as needed; that is the same class of silent-miss D1 rejects the additive
`url_resolved`-beside-`url` design for, one field over. Item 2's tests would
catch it, and a transcription-grade plan must not lean on a prerequisite item's
tests to catch a regression it introduced itself.

### D7. The resolved url is what the mirror is keyed on and what the run tree persists

Two lines in `tasks._init_submodules` and one in `images._extract_submodules`
change `sub.url` → `sub.url_resolved`:

```python
    mirrors = {
        sub.path: ensure_pruned_mirror(sub.url_resolved, sub.sha, cache_root)
        for sub in needed                        # `needed` is item 2's filter
    }
    ...
        _git("config", key, sub.url_resolved, cwd=dest)
```

```python
        mirror = ensure_pruned_mirror(sub.url_resolved, sub.sha, cache_root)
```

**The mirror key.** `pruned_mirror_path` slugs the url, so keying on the
resolved value is what makes two tasks that name the same submodule — one
absolutely, one relatively — share one cache entry instead of building the same
prune twice under a slug (`--sub.git`) that describes nothing. Keying on the raw
value is not merely wasteful: `ensure_mirror` would run `git clone --mirror
../sub.git` with `cwd` unset, i.e. relative to the harness process's working
directory, which is a different repository per invocation.

**The persisted url.** M5 measures that git itself writes the *resolved* value
into `.git/config`, so persisting it is fidelity to what a developer's clone
carries, not a harness invention. Persisting the raw value would leave a run
tree in which any command that re-derives from `.gitmodules` resolves against a
superproject whose `origin` `materialize` removed — M2's warning plus a host
path. N2 measures the one such command an agent can still reach: `git submodule
sync` overwrites the persisted url with git's path fallback even when the
persisted value was correct — in the container that is `/sub.git`, a path that
does not exist rather than a leak (see *What this does NOT do*). That is out of
scope; what persisting the resolved url buys is removing the **passive** way
the raw value reaches a command, and M1b measures that `git submodule status`
— the character preflight reads — is a leading space for all three persisted
forms, so nothing downstream is disturbed.

### D8. Preflight records both urls as **observations of the container**, and gains one assertion

Preflight does not hold the derived `Submodule` tuple and must not be given it:
it takes an untyped `task`, has no mirror and no cache root, and its whole
premise is reading the tree back out of the container the suite will run in.
So the two values it records are read **in the container**, from the two files
that carry them, and they are named for what they are:

| evidence key | source | meaning |
|---|---|---|
| `url_declared` | the tree's `.gitmodules` (`git config -f .gitmodules --get-regexp -z '^submodule\..*\.(path\|url)$'`) | the raw, tracked value — N5 measures it survives every rewrite |
| `url_persisted` | the run tree's own config (`git config --local --get-regexp -z '^submodule\..*\.url$'`) | what `_init_submodules` actually wrote, i.e. preflight's observation of the resolved url |

`url_persisted`, not `url_resolved`: the host-side field and the container-side
reading are two different claims about the same thing, and giving them one name
would be the "configuration reported as observation" failure in the file that
documents it. Both keys are present on **every** entry, `None` included — never
present-only-when-known, for the reason preflight's existing comments give about
`[]` versus `None`.

**One exec is added, not two.** The `.gitmodules` read already exists at
`preflight.py` for `submodules_orphaned` (find it by its `r"^submodule\..*\.path$"` pattern); its regex widens from
`^submodule\..*\.path$` to `^submodule\..*\.(path|url)$` and the existing
`-z`-record loop fills two dicts (`path_by_name`, `url_by_name`) instead of one
set. `submodules_orphaned` keeps being computed from the `path` entries alone,
so its value is byte-identical. The second exec is the `--local` read; N3
measures its record shape is the same, and N4 measures **exit 1 is the ordinary
"nothing matched"** — a repository with no initialised submodule — so the
branch is `in (0, 1)` exactly as the existing one is, with `>1` recording
`None` on every entry plus one problem.

The join key is the submodule **name** (both files key on it), and the entry key
is the **path**; `path_by_name` from `.gitmodules` is what bridges them. An
entry whose path no name maps to keeps `None` on both keys, which is honest.

**The new assertion**, and it is the one that would have caught this item's
defect:

```python
                if (entry["url_declared"] or "").startswith(("./", "../")) \
                        and entry["url_persisted"] is not None \
                        and not entry["url_persisted"].startswith(
                            ("http://", "https://", "/")):
                    problems.append(
                        f"submodule {entry['path']} declares the relative url "
                        f"{entry['url_declared']!r} and the run tree persists "
                        f"{entry['url_persisted']!r}. The relative url was "
                        "never resolved, so any command that re-reads it "
                        "resolves against a remote this tree does not have "
                        "and falls back to the superproject's own path, "
                        "which inside the container is a path that does not "
                        "exist."
                    )
```

It cannot fire on a fixture (a local-path url starts with `/`) and it cannot
fire on any task in today's corpus (no relative urls). It fires exactly when the
resolution did not reach the run tree.

**`is not None` on `url_persisted`, and never the `or ""` idiom that is right
on `url_declared` one line up.** The two are not symmetric. On `url_declared`,
`None` and `""` both mean "this tree declares nothing relative here" and
collapsing them changes no claim. On `url_persisted`, `None` means **not
measured**, in two distinct ways: the `--local` read failed (Step 2 sets
`persisted_by_name = None`), or the name carried no entry — which is precisely
what a **declared-unneeded** submodule looks like, since item 2 never registers
one, so the read exits 1 and the lookup is `None`. Under `or ""` both satisfy
the predicate and file a problem asserting *"The relative url was never
resolved"* — a positive claim about something the gate did not look at. That
is "absence is recorded, never implied" broken in the file whose comments state
it, on the key added to state it, and its concrete cost is that **every task
using item 2's `submodules_unneeded` on a relative-url gitlink becomes a
spurious NO-GO** — the exact repository class items 2 and 16 exist together to
reopen. A failed read is already reported by Step 2's own problem; this one
stays silent about what it did not see.

**`PREFLIGHT_VERSION` moves by one, and the number is deliberately not written
down here.** It is `"13"` at round base `8232032` and `"14"` at HEAD `2ddd68a`
(item 3 has landed since the base), and it will be higher again when this
lands: **every preceding round-2 item that changes what the gate asserts, or
what an absence in its evidence means, moves it by the same rule** — that is
several of items 1-15, not a fixed list, and an enumeration here would invite
an implementer to check for two bumps and stop. Transcribing a literal would be
wrong on arrival, and wrong in an expensive direction: too low serves a stale
cached verdict, too high silently retires a valid one.

**Instruction:** read the literal in `preflight.py` and write its successor.

**Post-condition, run after the edit:**

```bash
git show 8232032:bakeoff/src/bakeoff/preflight.py | grep '^PREFLIGHT_VERSION'   # "13"
git show HEAD:bakeoff/src/bakeoff/preflight.py    | grep '^PREFLIGHT_VERSION'   # the value before this commit
grep '^PREFLIGHT_VERSION' bakeoff/src/bakeoff/preflight.py                      # exactly one greater
```

The first line is the floor: if it does not print `"13"`, the tree in front of
you is not the one this plan was written against and the rest of its line
numbers and counts should be re-checked too.

The comment paragraph to append, with `<N>` and `<N-1>` filled in from the
file:

```python
#: <N-1> -> <N> adds the two url keys to every `submodules` entry
#: (`url_declared` from the tree's .gitmodules, `url_persisted` from the run
#: tree's own config) and one problem for a relative url the run tree never
#: resolved. A verdict cached under <N-1> was written by a gate that recorded
#: neither, so a reader of a stored blob cannot tell "this task's submodule url
#: is absolute" from "this gate did not look" -- an absent field read as a
#: positive negative claim. GO/NO-GO is unchanged for every task in the corpus
#: (none declares a relative url); what moved is what a stored verdict's
#: evidence can be read to say.
```

### D9. What does **not** move

- **`start_sha`.** `_init_submodules` runs after `start_sha` is computed and
  after the pin comparison (`tasks.py:2582-2599`), and this change touches only
  what happens inside it plus a host-side derivation that writes nothing into
  the tree. Pinned by a test rather than asserted.
- **The pruned-mirror version constant** (`tasks.py:1795`, *"Bumping this
  invalidates every cached pruned mirror"*). The mirror **key** changes only for
  relative-url tasks, and those were refused at derivation before this change, so no
  cached entry can exist under an old key. Nothing already cached is served
  differently.
- **`SCHEMA_VERSION`.** `Submodule` is a loader type; no `RunRecord` field
  carries a submodule url.
- **`GRADER_VERSION`, `GRADE_SCHEMA_VERSION`, `ORACLE_VERSION`.** The grader
  takes no submodule set (`task_submodules`' docstring records that it stopped
  being a caller); its gitlink refusal reads the submission's own chunks.
- **`manifest_digest`.** No manifest key is added — `repo.url` already exists.

---

## File structure

| file | change |
|---|---|
| `bakeoff/src/bakeoff/tasks.py` | `Submodule.url` → `url_declared` + `url_resolved: str \| None`, docstring; `_RELATIVE_URL_PREFIXES`; `_resolve_submodule_url`; `_SUBMODULE_URL_PREFIX`'s comment; the construction loop in `derive_submodules` (including item 2's `declared_unneeded=`) and its docstring; `stated`, **the `if` line** and the raise in `_refuse_submodule_conflicts`; the two `sub.url` reads in `_init_submodules` and that function's `THE URL IS PERSISTED` paragraph |
| `bakeoff/src/bakeoff/images.py` | the one `sub.url` read in `_extract_submodules` |
| `bakeoff/src/bakeoff/preflight.py` | the widened `.gitmodules` regex and its record loop; the new `--local` url read; two per-entry evidence keys; one problem; `PREFLIGHT_VERSION` +1 and its comment |
| `bakeoff/tests/test_tasks.py` | `sub.url` → `sub.url_declared` at line 2537; the resolver's rule and refusal tests; the derivation/ordering/item-2-interaction tests |
| `bakeoff/tests/test_preflight.py` | the evidence-key and problem tests |
| `bakeoff/tests/test_integration_submodules.py` | one relative-url integration test |
| `bakeoff/scripts/mutation_check.py` | eight new anchors, plus the `find`/`replace` of the existing `"tasks: populate a submodule from the unpruned mirror"` entry, which the rename invalidates |
| `bakeoff/taskset/HARVESTING.md` | the *"The url must be `https://`"* submodule bullet, found by text (item 2 rewrites this list and moves the lines) |
| `docs/BUILDING-A-TASK-SET.md` | the §2 rows whose first cells are *"needs a git submodule"* and *"`.gitmodules` names a non-`https://` url"*, found by text |
| `TASKS.md` | strike the *Relative `.gitmodules` urls* bullet, found by text |

---

## Task 1 — the resolver

- [ ] **Step 1.** Add `_RELATIVE_URL_PREFIXES` immediately above
  `_SUBMODULE_URL_PREFIX` in `tasks.py`, with the comment from D3.

- [ ] **Step 2.** Add `_resolve_submodule_url` beside `_has_gitmodules` (after
  `derive_submodules`, before `_refuse_submodule_conflicts`). Body, to be
  transcribed:

```python
def _resolve_submodule_url(repo_url: str, declared: str, *,
                           task_id: str, path: str) -> str:
    """git's resolution of a relative `.gitmodules` url, offline.

    gitmodules(5) resolves `./` and `../` against the superproject's default
    remote, `remote.origin.url`, or -- with no remote -- against the
    superproject's own path. `materialize` removes `origin` on purpose, so
    measured 2026-09-02 (git 2.50.1) git takes the third branch, warns
    *"Assuming this repository is its own authoritative upstream"*, and tries
    to clone `<run tree>/../x.git`, which does not exist. `task.repo_url` is
    the same fact the deleted remote carried, it is in the manifest, and it is
    offline -- so the resolution is done here instead.

    THIS IS A SUPERSET OF REFUSALS OVER GIT, not a port of `relative_url()`.
    git is defined to produce something for every input, at exit 0, which is
    why `../../../sub.git` under `https://github.com/org/super.git` measures as
    `https://sub.git` (a host named `sub.git`) and one more `../` measures as
    `https:/sub.git` (not a url at all). Everything accepted here git accepts
    identically; everything refused here git accepts and hands back a url the
    eval would fail to clone in the middle of a matrix. The 33 measured rows
    are in `docs/superpowers/plans/2026-09-03-round2-16-relative-submodule-urls.md`.

    No scheme check: the resolver returns a url and
    `_refuse_submodule_conflicts` judges it against `_SUBMODULE_URL_PREFIX`,
    which is the one place that decides what is fetchable -- and is the one
    name the test fixtures relax so a local path can stand in for a url.
    """
    if not declared.startswith(_RELATIVE_URL_PREFIXES):
        # git's own trigger, and it is a two-string prefix set rather than a
        # `..` check: measured, a bare `..` matches neither and git stores it
        # verbatim. Falling through leaves it for the url refusal to name.
        return declared

    where = f"{task_id}: submodule {path} declares the relative url " \
            f"{declared!r}"
    for char, what in (("?", "a query"), ("#", "a fragment")):
        if char in repo_url:
            raise TaskError(
                f"{where}, and repo.url {repo_url!r} carries {what}. Measured "
                "2026-09-02: git silently discards it when the `../` chain "
                "pops the segment holding it and embeds it mid-path for a "
                "`./`, so the resolved url is not a url anyone wrote down."
            )
    scheme, sep, rest = repo_url.partition("://")
    if sep:
        authority, slash, tail = rest.partition("/")
        if not slash:
            raise TaskError(
                f"{where}, and repo.url {repo_url!r} has no path to resolve "
                "against. Measured 2026-09-02: git pops the HOST in that "
                "case, giving `https://sub.git` -- a url naming a host that "
                "does not exist, at exit 0."
            )
        prefix = f"{scheme}://{authority}"
    elif repo_url.startswith("/"):
        # Reachable only from the test fixtures, where a local path stands in
        # for a url and `_SUBMODULE_URL_PREFIX` is relaxed to "". The prefix is
        # empty and the join below restores the leading slash, so the segment
        # arithmetic is the same one git does.
        prefix, tail = "", repo_url[1:]
    else:
        raise TaskError(
            f"{where}, and repo.url {repo_url!r} is neither a `scheme://` url "
            "nor an absolute path, so there is nothing to resolve against. An "
            "scp-style url (`git@host:org/repo.git`) resolves in git to "
            "another scp-style url, which this eval cannot fetch either."
        )
    tail = tail.rstrip("/")
    if not tail:
        raise TaskError(
            f"{where}, and repo.url {repo_url!r} has no path to resolve "
            "against."
        )
    segments = tail.split("/")
    if any(not segment for segment in segments):
        raise TaskError(
            f"{where}, and repo.url {repo_url!r} carries an empty path "
            "segment, so which segment a `../` pops is not well defined."
        )

    remainder = declared
    while True:
        if remainder.startswith("./"):
            # Measured: `./` appends to the WHOLE base, `super.git` included.
            remainder = remainder[2:]
        elif remainder.startswith("../"):
            remainder = remainder[3:]
            if not segments:
                # BEFORE the pop, not after, and that is the whole boundary:
                # two pops from two segments is `https://github.com/other/
                # sub.git`, exactly what git gives; a third pop is where git
                # starts eating the host and this stops.
                raise TaskError(
                    f"{where}, which climbs above the path of repo.url "
                    f"{repo_url!r}. Measured 2026-09-02: git pops the host "
                    "and then the scheme's second slash rather than failing, "
                    "so the clone target is a url naming a host that does not "
                    "exist."
                )
            segments.pop()
        else:
            break

    if not remainder:
        raise TaskError(
            f"{where}, which resolves to a directory rather than to a "
            "repository."
        )
    if remainder.endswith("/"):
        raise TaskError(
            f"{where}, which ends in a slash. Measured 2026-09-02: git "
            "consumes exactly one trailing slash, so `../x.git/` and "
            "`../x.git//` resolve to two different urls."
        )
    parts = remainder.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TaskError(
            f"{where}, which carries an empty or dot path component after its "
            "leading `./`/`../` chain. Measured 2026-09-02: git copies those "
            "through verbatim, so the clone target holds a literal `..` or an "
            "empty segment."
        )
    if any(char in remainder for char in "?#\\:") or \
            any(char.isspace() for char in remainder):
        raise TaskError(
            f"{where}, which carries a query, fragment, backslash, colon or "
            "whitespace. A relative submodule url is a path, and git appends "
            "it to the resolved base without escaping it."
        )
    return "/".join([prefix, *segments, remainder])
```

- [ ] **Step 3.** Tests, in `test_tasks.py`, in a new
  `# --- relative submodule urls ---` section directly above the existing
  `# --- submodules ---` one. Every one calls `tasks._resolve_submodule_url`
  with plain strings — no fixture, no repository. Refusals use
  `pytest.raises(tasks.TaskError, match=...)` with the substring named.

| # | test name | asserts |
|---|---|---|
| 1 | `test_a_parent_relative_url_resolves_against_repo_url` | row A: `../sub.git` under `https://github.com/org/super.git` → `https://github.com/org/sub.git` |
| 2 | `test_the_superprojects_last_segment_is_popped_whole_and_no_git_suffix_is_stripped` | rows A + B: `../sub` → `https://github.com/org/sub` (no `.git` appended), and A's answer comes from popping `super.git` entire |
| 3 | `test_a_repo_url_with_no_git_suffix_resolves_identically` | row D |
| 4 | `test_a_trailing_slash_on_repo_url_is_absorbed` | rows E and X both → `https://github.com/org/sub.git` |
| 5 | `test_a_dot_slash_url_appends_to_the_whole_base` | row C → `https://github.com/org/super.git/sub.git` (row Y is the same shape with a trailing slash and is refused by test 24, **not** accepted here) |
| 6 | `test_a_two_level_climb_empties_the_path_without_refusing` | row F → `https://github.com/other/sub.git`; the boundary case for the pre-pop guard |
| 7 | `test_each_climb_pops_exactly_one_segment` | row P → `https://github.com/org2/sub.git` |
| 8 | `test_userinfo_in_repo_url_survives` | row N |
| 9 | `test_the_resolution_is_case_preserving` | row U |
| 10 | `test_an_absolute_url_is_returned_unchanged` | `https://github.com/x/y.git` in, same out; and `""` in, `""` out |
| 11 | `test_a_bare_dot_dot_is_not_a_relative_url` | row T: `".."` returned verbatim, no raise |
| 12 | `test_an_absolute_local_path_base_resolves_by_the_same_arithmetic` | `/tmp/a/super` + `../lib` → `/tmp/a/lib`; the fixture branch |
| 13 | `test_a_climb_past_the_host_is_refused` | row G → `TaskError`, `match="climbs above the path of repo.url"` |
| 14 | `test_a_climb_past_the_scheme_is_refused` | row H → same message |
| 15 | `test_a_repo_url_with_no_path_is_refused` | `https://github.com` (row N1's shape) and `https://github.com/` → `match="no path to resolve against"` |
| 16 | `test_a_repo_url_with_a_query_is_refused` | row K → `match="carries a query"` |
| 17 | `test_a_repo_url_with_a_fragment_is_refused` | row L → `match="carries a fragment"` |
| 18 | `test_an_scp_style_repo_url_is_refused` | row M → `match="neither a `scheme://` url nor an absolute path"` |
| 19 | `test_a_relative_repo_url_is_refused` | `org/super.git` → same message |
| 20 | `test_a_repo_url_with_an_empty_path_segment_is_refused` | `https://github.com/org//super.git` → `match="empty path segment"` |
| 21 | `test_an_interior_dot_or_dot_dot_component_is_refused` | row I (`../a/../b.git`) **and row DOTA** (`../a/./b.git`, measured to pass through git verbatim as `…/org/a/./b.git`) → `match="empty or dot path component"` |
| 22 | `test_an_empty_component_in_the_relative_url_is_refused` | row R → same message |
| 23 | `test_a_relative_url_that_resolves_to_a_directory_is_refused` | rows S and V → `match="resolves to a directory"` |
| 24 | `test_a_trailing_slash_on_the_relative_url_is_refused` | rows O, O2 and **Y** (`./sub.git/`) → `match="ends in a slash"` |
| 25 | `test_a_relative_url_carrying_a_query_or_whitespace_is_refused` | `../sub.git?x=1` and `../sub .git` → `match="query, fragment, backslash, colon or whitespace"` |
| 26 | `test_the_refusal_names_the_task_and_the_submodule_path` | one refusal, asserting both `task_id` and `path` appear in `str(excinfo.value)` |

---

## Task 2 — the record, the derivation and the two consumers

- [ ] **Step 1.** Rename in `Submodule` (D1) and rewrite the last paragraph of
  its docstring:

```
    `name` is not `path`: the `[submodule "NAME"]` header is what
    `submodule.<name>.url` keys on, and git does not require the two to match.

    `url_declared` is the `.gitmodules` value VERBATIM -- measured 2026-09-02,
    nothing the harness does rewrites that blob, so it stays the
    manifest-adjacent fact. `url_resolved` is what the mirror was built from
    and what the run tree persists: equal to `url_declared` whenever the
    declared url was already absolute, resolved by `_resolve_submodule_url`
    when it was relative, and `None` -- a third value, not `""` -- when this
    submodule is declared unneeded and was therefore never resolved at all.
    `""` remains "the stanza declared no url", which is a different absence.
```

- [ ] **Step 2.** `derive_submodules`: replace the `subs = tuple(...)`
  comprehension with D6's loop, and add to its docstring:

```
    A RELATIVE url is resolved here, against `task.repo_url`, and the
    resolution runs LAST -- after the unreadable-.gitmodules raise and after
    the `unfetchable` raise -- so a gitlink with no url is told that rather
    than told that "" does not resolve.
```

- [ ] **Step 3.** `_refuse_submodule_conflicts`: `stated` and the raise from
  D5. Nothing else in that function moves; item 2's `if not
  sub.declared_unneeded:` guard stays exactly where item 2 put it.

- [ ] **Step 4.** `_SUBMODULE_URL_PREFIX`'s comment block, from D5.

- [ ] **Step 5.** `_init_submodules`: the two reads from D7, and append to the
  `THE URL IS PERSISTED, THEN REWRITTEN` paragraph:

```
    The value persisted is `url_resolved`, which for a relative `.gitmodules`
    url is NOT what the blob says. That is fidelity, not invention: measured
    2026-09-02, a clone with a reachable remote has git itself write the
    resolved url into `.git/config`. Persisting the raw value instead would
    leave a tree in which any command that re-derives from `.gitmodules`
    resolves against the `origin` `materialize` removed and falls back to the
    superproject's own path fallback -- on the host that is a host path, and
    inside the container, where the tree is mounted at `/repo`, it is
    `/sub.git` (measured: a warning, then a clone of a path that does not
    exist, in both cases). `git submodule sync` re-derives that way and is
    deliberately not covered; what this removes is the passive path.
```

- [ ] **Step 6.** `images._extract_submodules`: `sub.url` → `sub.url_resolved`.

- [ ] **Step 7.** Re-locate every `Submodule`-shaped `.url` read in the tests
  **after item 2 has landed** — `git grep -n '\bsub\.url\b' bakeoff/tests` —
  rather than transcribing a line number. At round base there is one
  (`test_tasks.py:2537`); after item 2 expect **four**, the other three being
  its declared-unneeded tests. Rename each to `url_declared`, and:

  - on `test_tasks.py:2537`'s absolute-url fixture, add
    `assert sub.url_resolved == sub.url_declared` (the corpus shape);
  - on each of item 2's three declared-unneeded assertions, add
    `assert sub.url_resolved is None`. That is not decoration: after this item
    a declared-unneeded submodule has `url_declared == ""` **and**
    `url_resolved is None`, and asserting only the first would leave the two
    absences D1 exists to separate unpinned in the very tests that create the
    shape.

- [ ] **Step 8.** Derivation and wiring tests. The relative-url fixture is a new
  sibling of `upstream_submodule` in `test_tasks.py` — call it
  `upstream_relative_submodule`.

  **The directory layout, checked against `test_tasks.py:117-172`, because the
  arithmetic depends on it and the dict key is not the directory name:**
  `upstream_submodule` creates the submodule source at `tmp_path / "libdep"`
  (returned under the dict key `"lib"`) and the superproject at
  `tmp_path / "super"`, with the gitlink at `vendor/libdep`. So the relative
  url is **`../libdep`**, not `../lib`: from base `<tmp_path>/super` one `../`
  pops `super` and the remainder appends, giving `<tmp_path>/libdep` —
  `str(up["lib"])`. `../lib` would resolve to `<tmp_path>/lib`, which does not
  exist, and `ensure_pruned_mirror` would then fail on a `git clone --mirror`
  of a missing path: tests 32-35 would die with a `CalledProcessError` instead
  of the assertion they were written for, while test 27's string comparison
  passed — the worst ordering.

  The fixture builds the same superproject and rewrites the committed
  `.gitmodules` url to `../libdep` (`git config -f .gitmodules
  submodule.vendor/libdep.url ../libdep`, then commit, so `base_sha` carries
  it), and writes its manifest with `url=str(up["path"])`, i.e.
  `<tmp_path>/super`. It reuses the existing `local_urls` relaxation unchanged
  — the resolver's absolute-local-path branch (D3 step 2) is what makes that
  work with no new test-only flag.

| # | test name | asserts |
|---|---|---|
| 27 | `test_both_urls_are_recorded_on_the_submodule_record` | `url_declared == "../libdep"`, `url_resolved == str(up["lib"])` (which is `<tmp_path>/libdep`) |
| 28 | `test_an_absolute_url_records_the_same_value_twice` | on `upstream_submodule`: `url_declared == url_resolved` |
| 29 | `test_a_gitlink_with_no_url_is_refused_before_the_resolver_runs` | a stanza with `path` and no `url` under a `repo_url` that would itself refuse (`https://github.com`) raises the *"no url"* message, **not** a resolver message |
| 30 | `test_an_unresolvable_relative_url_is_refused_at_derivation` | `../../../x.git` → `TaskError` out of `derive_submodules`, `match="climbs above"`. Named "at derivation", not "at load": `load_task` does not call `derive_submodules`, and `_SUBMODULE_URL_PREFIX`'s own comment already uses that word |
| 31 | `test_a_declared_unneeded_submodule_is_never_resolved` | item 2's key on the relative fixture with `../../../x.git`: loads clean, `url_resolved is None`, `url_declared == "../../../x.git"` |
| 32 | `test_no_mirror_is_keyed_on_the_raw_relative_url` | after `materialize`, `pruned_mirror_path(sub.url_resolved, …)` exists and no directory matching the raw url's slug exists under the cache root |
| 33 | `test_the_run_tree_persists_the_resolved_url` | `git config --get submodule.<name>.url` in `dest` equals `url_resolved`, and `git show HEAD:.gitmodules` still carries `../lib` (N5's property) |
| 34 | `test_the_submodule_is_at_its_gitlink_after_a_relative_url_materialization` | `git rev-parse HEAD` in `dest/vendor/libdep` equals the pinned sha; `git submodule status` reads a leading space (M1b) |
| 35 | `test_start_sha_does_not_move_when_a_relative_url_is_resolved` | `materialize` on the relative fixture and on an otherwise identical absolute one return the same `start_sha` |
| 36 | `test_the_image_context_uses_the_resolved_url` | `images._extract_submodules` on the relative fixture populates the path (the `images.py` read) |

---

## Task 3 — preflight

**Where every step in this task goes.** All of Steps 1-4 are inside
`elif status.exit_code == 0:` — the branch that already builds `submodules`
and `submodules_orphaned`. That placement is load-bearing: on the two failure
branches above it `submodules` is `[]`, so the entry loop is correctly inert
there and neither new key is fabricated for a tree the gate could not read.
Order within the branch: the `.gitmodules` read (Step 1), then the run-tree
read (Step 2), then the entry loop (Steps 3-4), then the existing `stale`
check.

- [ ] **Step 1.** Widen the `.gitmodules` regex (find it by its current
  pattern `r"^submodule\..*\.path$"`) to `r"^submodule\..*\.(path|url)$"` and
  split the record loop into `path_by_name: dict[str, str]` and
  `url_by_name: dict[str, str]`. `declared_paths` becomes
  `set(path_by_name.values())` so `submodules_orphaned` is computed from
  exactly the same values as before. Keep the existing `partition("\n")` and
  the `if sep:` guard verbatim — the valueless-key case the comment describes
  is now reachable for two key suffixes instead of one.

  **Both dicts are initialised ABOVE `if declared_subs.exit_code in (0, 1):`,
  not inside it.** `declared_paths` and its loop live inside that branch today,
  and its `else:` sets `submodules_orphaned = None` and appends a problem. If
  the dicts were declared inside the branch, Step 3's unconditional
  `name_by_path = {...}` would hit an unbound name on the `else` path and raise
  `UnboundLocalError` **out of `preflight`**, whose caller does not wrap it —
  a traceback instead of a NO-GO, which is exactly the failure the comment
  eight lines above it documents for `split("\n", 1)[1]`. Hoisted, the `else`
  path leaves both dicts empty, every entry gets `None` on both url keys, and
  the only problem filed is the existing orphan-read one. Step 2 already
  initialises `persisted_by_name` above its own branch for the same reason;
  this makes the two symmetric.

```python
            path_by_name: dict[str, str] = {}
            url_by_name: dict[str, str] = {}
            if declared_subs.exit_code in (0, 1):
                ...                      # the existing loop, filling both
                declared_paths = set(path_by_name.values())
                evidence["submodules_orphaned"] = sorted(
                    declared_paths - set(gitlinks))
            else:
                ...                      # unchanged
```

- [ ] **Step 2.** Add the run tree's own read, immediately after:

```python
            # The RESOLVED url, observed rather than restated. `--local` is
            # deliberate: this asks what `_init_submodules` wrote into THIS
            # tree, not what an operator's global config says. Measured
            # 2026-09-02 (git 2.50.1): `-z` gives `<key>\n<value>\0` records,
            # the same shape the .gitmodules read above parses, and exit 1 is
            # the ordinary "no key matched" for a tree with no initialised
            # submodule -- collapsing it with >1 would report an unreadable
            # config as the measured claim "no submodule has a url".
            persisted = container.exec(
                ["git", "config", "--local", "--get-regexp", "-z",
                 r"^submodule\..*\.url$"]
            )
            persisted_by_name: dict[str, str] | None = {}
            if persisted.exit_code in (0, 1):
                for record in persisted.stdout.split("\0"):
                    if not record:
                        continue
                    key, sep, value = record.partition("\n")
                    if sep:
                        persisted_by_name[
                            key[len("submodule."):-len(".url")]] = value
            else:
                persisted_by_name = None
                problems.append(
                    "reading the run tree's submodule urls failed (exit "
                    f"{persisted.exit_code}): "
                    f"{(persisted.stdout or persisted.stderr)[:500]}"
                )
```

- [ ] **Step 3.** Fill both keys on **every** entry, in the caller, in the
  enclosing block named at the head of this task (`elif status.exit_code == 0:`,
  after the run-tree read, before `stale`) —
  `_parse_submodule_status` is not touched, for the reason its docstring gives
  (*"This is an OBSERVATION, not a restatement of the manifest"*, and both of
  these come from files it never reads):

```python
            name_by_path = {path: name for name, path in path_by_name.items()}
            for entry in submodules:
                name = name_by_path.get(entry["path"])
                entry["url_declared"] = (
                    None if name is None else url_by_name.get(name))
                entry["url_persisted"] = (
                    None if name is None or persisted_by_name is None
                    else persisted_by_name.get(name))
```

- [ ] **Step 4.** The problem from D8, in the same loop.

- [ ] **Step 5.** `PREFLIGHT_VERSION` +1 and the comment paragraph from D8.

| # | test name | asserts |
|---|---|---|
| 37 | `test_every_submodule_entry_carries_both_url_keys` | an absolute-url tree: both keys present on the entry, `url_declared` the `.gitmodules` value, `url_persisted` the config value |
| 38 | `test_the_url_keys_are_present_even_when_the_name_cannot_be_joined` | a status entry whose path no `.gitmodules` `path` names: both keys `None`, no crash |
| 39 | `test_an_unreadable_run_tree_config_nulls_the_persisted_url_and_files_a_problem` | exit 2 from the `--local` read → `url_persisted is None` on every entry and one problem naming the exit code |
| 40 | `test_an_absent_run_tree_config_is_not_a_problem` | exit 1 → `url_persisted is None`, **no** problem (N4's ordinary case) |
| 41 | `test_an_unresolved_relative_url_in_the_run_tree_is_a_problem` | `.gitmodules` `../lib`, config `../lib` → the D8 problem, message substring `"was never resolved"` |
| 42 | `test_a_resolved_relative_url_in_the_run_tree_is_not_a_problem` | `.gitmodules` `../lib`, config `https://github.com/org/lib.git` → no such problem |
| 43 | `test_a_relative_url_resolved_to_a_local_path_is_not_a_problem` | config `/cache/lib.git` → no problem (the fixture branch of the predicate) |
| 44 | `test_the_orphan_set_is_unchanged_by_the_widened_regex` | a `.gitmodules` carrying both `path` and `url` for two stanzas, one orphaned: `submodules_orphaned` is the same single path it was before |
| 45 | `test_the_pre_container_early_return_still_nulls_the_three_submodule_keys_together` | the early return leaves `submodules`, `submodules_orphaned` and (item 2's) `submodules_empty_after_suite` all `None` — the new keys are per-entry and add no fourth top-level key |
| 45a | `test_an_unreadable_gitmodules_nulls_both_url_keys_without_crashing` | the `.gitmodules` read exits 2: **no traceback**, every entry carries `url_declared is None` and `url_persisted is None`, `submodules_orphaned is None`, and the only problem filed is the existing `"reading .gitmodules failed"` one. This is the `UnboundLocalError` finding 2 caught, pinned |
| 45b | `test_an_unreadable_run_tree_config_does_not_also_claim_the_url_was_unresolved` | `.gitmodules` `../lib` **and** the `--local` read exits 2: exactly **one** problem (the read failure), and **not** the "never resolved" one — the `is not None` half of the predicate |
| 45c | `test_a_declared_unneeded_submodule_with_a_relative_url_is_a_go` | item 2's key on a relative-url gitlink: `url_persisted is None` because nothing registered it, and **no** "never resolved" problem — the repository class items 2 and 16 exist together to reopen |

---

## Task 4 — integration

- [ ] **A new module-scoped fixture, `superproject_relative_url(workspace)`,
  with its own fresh directories `libdep3` and `super3`** — never the
  `superproject` fixture's, and never `superproject_under_test_prefix`'s
  (`libdep2` / `super2`). That is the file's own established rule, stated in
  `superproject_under_test_prefix`'s docstring and quoted here because it is
  the reason:

  > A fresh submodule (`libdep2`), never the `superproject` fixture's —
  > module-scoped fixtures in this file run in an unspecified order and a
  > shared submodule mirror would make one test's clone the reason the other's
  > prune assertion holds.

  `workspace` is module-scoped, and `superproject` already occupies
  `workspace / "libdep"` and `workspace / "super"`, so reusing either name is
  both a directory collision and a shared-mirror violation. Concretely, the
  fixture builds `workspace / "libdep3"` and `workspace / "super3"`, adds the
  submodule at `SUB_PATH`, rewrites the committed `.gitmodules` url to
  **`../libdep3`** and commits it, and writes its manifest with
  `url=str(workspace / "super3")` — so the resolution is
  `<workspace>/super3` + `../libdep3` → `<workspace>/libdep3`, the directory
  it just built. It reuses the file's existing hand-swap of
  `_SUBMODULE_URL_PREFIX` (the module-scoped `local_urls` fixture) rather than
  adding a second mechanism, and it is offline: every repository is a local
  `git init` and every submodule-touching command carries
  `-c protocol.file.allow=always`, exactly as `superproject` does.

| # | test name | asserts |
|---|---|---|
| 46 | `test_a_relative_url_superproject_materializes_and_the_image_matches` | on `superproject_relative_url`: `materialize` populates `SUB_PATH` at its gitlink; `git submodule status` reads a leading space; `git config --get submodule.<name>.url` in the run tree is `str(workspace / "libdep3")`, **not** `../libdep3`; and the image's blob at that path equals the run tree's — the same assertion `test_the_image_and_the_run_tree_carry_the_same_submodule_blob` makes for an absolute url |

This is the one test that proves the whole path end to end: the resolver, the
mirror key, `_init_submodules`' preset-and-rewrite, and `_extract_submodules`.
It carries the same Docker and base-image requirement as its sibling test,
which is what the `integration` mark is for.

---

## Task 5 — mutation anchors

**Step 0, and it lands FIRST.** The rename invalidates an anchor that already
exists. `mutation_check.py` carries, under the entry
`"tasks: populate a submodule from the unpruned mirror"`:

```
        "        sub.path: ensure_pruned_mirror(sub.url, sub.sha, cache_root)",
        "        sub.path: ensure_mirror(sub.url, sub.sha, cache_root)",
```

Item 16 rewrites that source line to `sub.url_resolved`, so both the `find` and
the `replace` go stale. `mutation_check.py` fails loudly on a stale anchor, so
this is not silent — but it is a required edit, and it must be made **before**
the new entries below, because new anchors 4 and 5 quote lines this same edit
changes. Update both strings to `sub.url_resolved`; the entry's label, test
selector and comment are unchanged, because what it pins (pruned versus
unpruned) has not moved.

Then **eight new entries**. Each edit is a single valid line replacement, so a
stale anchor fails loudly rather than silently passing.

**Every `find` below is a quoted line, not a description, and every one must be
verified unique before the entry is added.** `mutation_check.py` applies
`original.replace(find, replace, 1)` — the **first** occurrence — so a `find`
that is not unique silently mutates something else and reports the entry
MISSED, which reads as "this guarantee is unanchored" when in fact the anchor
never reached the guarantee. Measured: `grep -c 'and not'` is **8** in
`preflight.py` and **22** in `tasks.py`, with the first hit in `preflight.py` at
line 204 inside `preflight_cache_key`'s docstring — so a fragment like *"the
`and not` in the guard"* is not an anchor at all. Per entry:
`grep -c -- '<find>' <file>` must print exactly `1` **before** the entry is
added; if it prints more, **extend** the `find` with the following line rather
than shortening it (item 2's plan carries the same rule and the same reason).
Verified at round base: `if not segments:`, `remainder = remainder[2:]` and
`_RELATIVE_URL_PREFIXES` have **0** occurrences in `tasks.py` today, so each is
unique the moment the resolver lands.

| # | label | mutation | test that must go red |
|---|---|---|---|
| 1 | `tasks: a relative submodule url is resolved` | in `_resolve_submodule_url`: `    if not declared.startswith(_RELATIVE_URL_PREFIXES):` → `    if True:` | `test_tasks.py -k both_urls_are_recorded` |
| 2 | `tasks: the climb guard runs before the pop` | `if not segments:` → `if False:` | `test_tasks.py -k climb_past_the_host` |
| 3 | `tasks: a dot-slash url does not pop a segment` | `remainder = remainder[2:]` → `remainder = remainder[2:]; segments and segments.pop()` | `test_tasks.py -k dot_slash_url_appends` |
| 4 | `tasks: the mirror is keyed on the resolved url` | `ensure_pruned_mirror(sub.url_resolved, sub.sha, cache_root)` → `ensure_pruned_mirror(sub.url_declared, sub.sha, cache_root)` (in `tasks.py`) | `test_tasks.py -k no_mirror_is_keyed_on_the_raw` |
| 5 | `tasks: the run tree persists the resolved url` | `_git("config", key, sub.url_resolved, cwd=dest)` → `_git("config", key, sub.url_declared, cwd=dest)` | `test_tasks.py -k run_tree_persists_the_resolved_url` |
| 6 | `images: the image context uses the resolved url` | `ensure_pruned_mirror(sub.url_resolved, sub.sha, cache_root)` → `ensure_pruned_mirror(sub.url_declared, sub.sha, cache_root)` (in `images.py`) | `test_tasks.py -k image_context_uses_the_resolved_url` |
| 7 | `tasks: a declared-unneeded submodule is never resolved` | `url_resolved=None if path in unneeded else _resolve_submodule_url(` → `url_resolved=_resolve_submodule_url(` (drop the guard, keep the call's arguments) | `test_tasks.py -k declared_unneeded_submodule_is_never_resolved` |
| 8 | `preflight: an unresolved relative url is a problem` | `                        and not entry["url_persisted"].startswith(` → `                        and entry["url_persisted"].startswith(` | `test_preflight.py -k unresolved_relative_url` |

Anchors 4 and 6 are the same edit in two files on purpose: they are two
independent consumers, and a single anchor would let one of them regress
silently.

**Two notes on the anchors above, both of which cost a draft.**

*Anchor 1 mutates the resolver's own trigger, not the call site.* The first
draft anchored the call site with `url_resolved=None if path in unneeded else
(`, which is a **SyntaxError**: the continuation line it would then head is
`task.repo_url, declared, task_id=task.task_id, path=path),`, and keyword
arguments are not legal in a parenthesized display (`ast.parse` →
*"invalid syntax. Maybe you meant '==' or ':=' instead of '='?"*). A mutation
that does not parse is not a mutation. `if True:` at the resolver's trigger
makes it return `declared` unchanged, which is precisely "no resolution
happens", and it is one unique line.

*Anchors 2 and 8 differ in how their test goes red, and both count.* Anchor 8
inverts the resolved/unresolved test, so test 41 stops filing its problem — a
clean assertion failure. Anchor 2 (`if not segments:` → `if False:`) lets
`segments.pop()` run on an empty list, so test 13's `pytest.raises(TaskError)`
sees an `IndexError` and errors rather than failing. `mutation_check.py` asks
only that the named test go red, and an error is red; it is written down here
so a reader of the report is not surprised by the exception type.

---

## Task 6 — docs and TASKS.md

**Address every doc edit by quoted text, never by line number or ordinal.**
Item 2 rewrites this same bullet list — its Task 5 Step 3 turns *"Submodules
are supported, with **six** limits"* into **seven** and inserts a new bullet —
and it also rewrites both `BUILDING-A-TASK-SET.md` rows quoted below. Line
1317 of `TASKS.md` and lines 601-604 / 236-237 of the docs are where these sat
at the time of writing and will have moved.

- [ ] **`bakeoff/taskset/HARVESTING.md`**, the bullet beginning *"The url must
  be `https://`. Relative (`../x.git`), `ssh://`, `git@…` and `file://` are
  refused"* — find it by that text — replaced with:

```
  - **The url must resolve to `https://`.** A relative url (`../x.git`,
    `./x.git`) is resolved against `repo.url` by the same segment arithmetic
    git uses against `remote.origin.url` — `../` pops one path segment,
    `./` pops none, a trailing `.git` on `repo.url` is simply part of the
    segment that gets popped, and a trailing slash on `repo.url` is absorbed.
    What is refused is a chain that climbs above `repo.url`'s path (git would
    eat the host and hand back `https://sub.git` at exit 0), a `repo.url` with
    no path, a query or fragment on either url, an scp-style or relative
    `repo.url`, and a remainder that names a directory rather than a
    repository. `ssh://`, `git@…`, `http://` and `file://` are refused as
    before, and they are refused on the RESOLVED url, so a relative url under
    an ssh `repo.url` is out for the same reason its parent is. A submodule
    declared unneeded (the bullet below) is never resolved and no url of any
    kind is judged for it.
```

- [ ] The bullet block's lead-in keeps **whatever count item 2 left it at**
  (item 2 takes it from six to seven). This item changes what one existing
  limit says and removes none, so the number does not move again. The
  cross-reference in the last sentence above points at item 2's new bullet so
  the two cannot be read as contradicting each other.

- [ ] **`docs/BUILDING-A-TASK-SET.md` §2**, two rows, found by their leading
  cell text (item 2 edits both, so take whatever it left and apply these two
  changes to it):

  - the row whose first cell is *"needs a git submodule"*: change *"the
    `.gitmodules` url is `https://`"* to *"the `.gitmodules` url is `https://`,
    or is relative and resolves to one against `repo.url`"*.
  - the row whose first cell begins *"`.gitmodules` names a non-`https://`
    url"*: strike *"a relative path"* from that first cell, and add a
    sentence to the second — *"A relative url is not in this row: it is
    resolved against `repo.url` and only the result is judged."*

- [ ] **`TASKS.md`**: delete the bullet beginning *"**Relative `.gitmodules`
  urls.** git resolves `../toml-test.git` against"* — find it by that text
  (`grep -n 'Relative .gitmodules' TASKS.md`; it was line 1317 at the time of
  writing and item 2's own TASKS.md edit moves it). The *Nested submodules*
  bullet below it stays.

---

## Verification

Run from `bakeoff/` with `.venv/bin/python`.

1. `.venv/bin/python -m pytest tests/ -v` — expect **+48 tests** over whatever
   the tree carries when this lands: 26 resolver (table rows 1-26) + 10
   derivation/wiring (27-36) + 12 preflight (37-45, 45a, 45b, 45c) = 48.
   Renaming an existing assertion is not a new test, and test 46 is
   `integration`-marked and deselected here.
2. `.venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`
   — expect **+1**, test 46. (`--basetemp` under `$HOME` is mandatory on macOS;
   see `CLAUDE.md`.)
3. `.venv/bin/python scripts/verify_logger.py` — must print `GATE PASSED`.
4. `.venv/bin/python scripts/mutation_check.py`, **solo** — **eight new
   entries and one edited** (`"tasks: populate a submodule from the unpruned
   mirror"`, Task 5 Step 0), so the total moves by **+8**, not +9. A stale-anchor
   failure naming that entry means Step 0 was skipped.
5. `.venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight
   --task-set ~/.cache/bakeoff-probe/taskset --tasks tomlkit-514-inline-table-comment-separator`
   — the corpus's one real submodule task, whose url is absolute. It must stay
   GO, and its evidence must now carry `url_declared` =
   `https://github.com/BurntSushi/toml-test.git` and `url_persisted` = the same
   string. This is the regression check that the rename and the two new
   evidence keys did not disturb a task that declares nothing relative.
6. `git grep -n '\.url\b' bakeoff/src bakeoff/tests | grep -v repo_url` must
   return no `Submodule`-shaped hit — the rename's own post-condition.

---

## What this does NOT do

- **It does not resolve against a remote.** `remote.origin.url` is never read;
  `task.repo_url` is the only base. A repository whose real submodule url
  resolves differently from `repo.url` (a fork, a mirror) resolves to the
  `repo.url`-relative answer, which is the same answer a clone of `repo.url`
  would get and is therefore the right one for this eval.
- **It does not make `git submodule sync` safe — and the harm is not what the
  first draft of this plan said it was.** N2 measures that `sync` re-derives
  from `.gitmodules` against the `origin` `materialize` removed and overwrites
  the persisted url with git's own path fallback. The scratch tree sat on the
  host, so the value became a host path — but **the only place an agent can run
  `sync` is inside the container, where the run tree is bind-mounted at
  `/repo`**, and the same arithmetic there yields `/sub.git`. That is the
  container's own view of a path that does not exist: no operator cache path,
  no host filesystem layout, **no disclosure**. The actual consequence is
  narrower — a subsequent `git submodule update` would have nothing to fetch
  from, in a tree where the submodule is already populated at its gitlink and
  `git submodule status` reads a leading space for all three persisted forms
  (M1b). Nothing preflight reads changes, nothing the grader reads changes, and
  item 10's `_refuse_host_mirror_path` runs on the host before the container
  exists, so it cannot see this either. **No HARVESTING rule is added**: it
  would be a rule against a non-consequence, and it would be unenforceable
  anyway, since the *agent* runs `sync`, not the suite. Recorded and not fixed;
  the alternative — leaving a synthetic `origin` in the run tree — reintroduces
  the leak `materialize` deletes it to prevent.
- **It does not check the persisted url for host-path leakage in preflight.**
  The new problem fires on *"declared relative, persisted relative"* only. A
  persisted `/Users/…/.cache/bakeoff/…` would pass it — and must, because that
  is exactly what a fixture's url looks like under the `_SUBMODULE_URL_PREFIX`
  relaxation. A leak check needs a claim about the host that preflight cannot
  make from inside the container.
- **It does not touch nested submodules** (round-2 item 18). A relative url one
  level down is still an inner `.gitmodules` this code never reads, and the
  nested refusal fires first.
- **It does not add a manifest key.** No `submodule_url_override`, no
  `submodules:` block. The url is derived from `base_sha` and resolved against a
  key that already exists; anything else would be configuration reported as
  observation, which is broadening 6's founding decision.
- **It does not change `repo.url`'s own validation.** `load_task` still accepts
  any string there. The new refusals fire only when a *relative submodule url
  needs resolving*, so a task with no submodules and an odd `repo.url` loads
  exactly as it does today.
- **It does not move `start_sha`, the pruned-mirror version, `SCHEMA_VERSION`,
  `GRADER_VERSION`, `GRADE_SCHEMA_VERSION` or `ORACLE_VERSION`** (D9). Only
  `PREFLIGHT_VERSION`.
- **It re-gates nothing already collected.** No stored run record carries a
  submodule url, and no cached pruned mirror can exist under a relative-url key
  because such a task was refused at derivation.

---

## Open questions and rulings

1. **Should `./x` be accepted at all, given that it resolves to
   `<repo_url>/x`?** *Ruled: accepted.* It is legal in gitmodules(5), git's
   answer is well-defined and measured twice (rows C, Y), and refusing a shape
   git handles unambiguously would put a second, narrower rule in a resolver
   whose whole claim is fidelity. It is vanishingly rare in practice, and if a
   corpus repository ever uses it the eval will fetch what git would fetch.
2. **Should the resolver normalise an interior `.`/`..` (rows I, R, DOTA)
   instead of refusing?** *Ruled: refuse.* Normalising would make the eval
   clone something git does not, which is the opposite of the design's claim;
   refusing is a superset of git's behaviour in the safe direction.
   **Assumed, not measured:** no repository in the screened corpus declares a
   `.gitmodules` url with an interior `.` or `..` component or an empty
   segment. No GitHub-wide search was run, and none is claimed. If a candidate
   repository is ever refused for this, the message names the exact component,
   and the ruling is revisitable — but the fix would be to widen the corpus
   rule in `HARVESTING.md`, not to widen the resolver, because the resolver's
   whole claim is that it never fetches something git would not.
3. **Should `url_persisted` in preflight instead be the host-derived
   `url_resolved`, threaded in through `task`?** *Ruled: no.* preflight takes an
   untyped `task`, holds no mirror and no cache root, and its stated job is
   reading the tree back out of the container. A threaded value would be
   configuration reported as observation and would make the new problem
   tautological — it would compare a value against itself.
4. **Does `derive_submodules` need `task.repo_url` to be validated as `https://`
   at load?** *Ruled: no, and deliberately.* The resolver refuses the shapes it
   cannot use, and only when a relative url makes `repo.url` load-bearing. A
   load-time `https://` requirement on `repo.url` would break every fixture in
   the suite (they all use local paths) and would be a policy change with a much
   larger blast radius than this item.
5. **Where does the refusal for a relative url under a declared-unneeded
   submodule live?** *Ruled: nowhere — it is not refused.* Item 2's whole point
   is that an unneeded submodule's url is never contacted, and its D4 already
   exempts that path from both `.gitmodules` refusals; making the resolver an
   exception would mean a task could opt out of an ssh submodule but not a
   relative one. Test 31 pins it.
6. **`str | None` on `url_resolved` versus a separate `resolved: bool`.**
   *Ruled: the optional string.* A bool plus a string is two fields that can
   disagree; the optional is one field with three measured meanings (D1).

---

## Sentences for `CLAUDE.md`, to be applied off this branch

To the *Invariants* list, after the run-tree/objects bullet:

> - **A relative `.gitmodules` url is resolved against `task.repo_url`, by
>   git's arithmetic and with git's degenerate cases refused.** git resolves
>   `../x.git` against `remote.origin.url`, which `materialize` deletes —
>   measured 2026-09-02 (git 2.50.1), git then falls back to the superproject's
>   own **host path**, warns *"Assuming this repository is its own authoritative
>   upstream"*, and fails the clone. `repo.url` carries the same fact offline,
>   so `_resolve_submodule_url` pops one path segment per `../` and none per
>   `./` — there is no "strip the trailing `.git`" rule; `.git` is just part of
>   the segment that goes. It is a **superset of refusals over git**, not a
>   port: git produces something at exit 0 for every input, so
>   `../../../x.git` measures as `https://x.git` (a host named `x.git`) and one
>   more `../` as `https:/x.git`. `Submodule` carries `url_declared` (verbatim,
>   and the tracked blob is never rewritten) beside `url_resolved` (`None` when
>   the submodule is declared unneeded and was never resolved — a different
>   absence from `""`, which means the stanza declared no url), and the pruned
>   mirror and the run tree's persisted url are both keyed on the resolved one,
>   which is what git itself writes into `.git/config` on a normal clone.

---

## Review 1 → changes

All 15 findings addressed; **14 adopted as written, 1 adopted with a corrected
fact** (finding 11). Both rulings (findings 10 and 11) adopted.

| # | finding | change |
|---|---|---|
| 1 | **BLOCKER** — D6's loop drops item 2's `declared_unneeded=`, and the field's `False` default makes the regression silent | D6 and Task 2 Step 2 now write `declared_unneeded=path in unneeded,` with a comment naming what its omission silently reverts (item 2's url guard, `_init_submodules`' filter, `_extract_submodules`' skip, preflight's GO) and saying `unneeded` is read once and used twice. The implementer note now states the field is not optional and that the plan must not lean on item 2's tests to catch its own regression. |
| 2 | **BLOCKER** — `path_by_name` defined only inside the `exit_code in (0, 1)` branch → `UnboundLocalError` out of the gate | Task 3 gains a *"where every step goes"* header naming `elif status.exit_code == 0:` and the order within it, and Step 1 now hoists `path_by_name`/`url_by_name` **above** the branch (with the code block), symmetric with Step 2's `persisted_by_name`, and states that the `else` path leaves both url keys `None` on every entry and files only the existing orphan-read problem. Step 3 names its enclosing block. New test **45a** pins it. |
| 3 | **BLOCKER** — D5 never gives the `if` line, and its documented `None` branch dies with `AttributeError` before the sentence is used | D5 now writes `if not (sub.url_resolved or "").startswith(_SUBMODULE_URL_PREFIX):` verbatim, and replaces the rationale: the `or ""` is what makes the three-way `stated` reachable, and `sub.url_resolved.startswith(...)` would throw away the sentence one line after building it. Added to the File Structure row. |
| 4 | **Major** — the fixture sibling is `libdep`, not `lib`; `../lib` resolves to a missing directory | Task 2 Step 8 now states the measured layout (`tmp_path/libdep`, key `"lib"`; `tmp_path/super`; gitlink `vendor/libdep`), uses **`../libdep`**, spells out the arithmetic, and names the failure the wrong value produces (`CalledProcessError` in tests 32-35 while test 27's string compare passes). Test 27 updated. |
| 5 | **Major** — Task 4's fixture collides with `superproject`'s directories and breaks that file's fresh-submodule rule | Task 4 now specifies a new module-scoped `superproject_relative_url(workspace)` with `libdep3`/`super3`, quotes `superproject_under_test_prefix`'s docstring as the reason, and gives the `.gitmodules` url (`../libdep3`), the manifest `repo.url` (`<workspace>/super3`) and the offline/`protocol.file.allow` properties. Test 46 restated against it. |
| 6 | **Major** — the existing `"tasks: populate a submodule from the unpruned mirror"` anchor goes stale; "+8" is wrong | Task 5 gains **Step 0** (edit that entry's `find` and `replace` to `sub.url_resolved`, **first**, because new anchors 4 and 5 quote lines it changes), the File Structure row says so, and Verification 4 now reads "eight new entries and one edited; the total moves by +8". |
| 7 | **Moderate** — the `sub.url` census predates item 2 | D1's counts are now labelled *"counted at round base `8232032`"* with an explicit re-count instruction, and name item 2's three added test reads. Task 2 Step 7 replaces the line number with `git grep -n '\bsub\.url\b' bakeoff/tests` and requires `assert sub.url_resolved is None` on each of item 2's declared-unneeded assertions. |
| 8 | **Moderate** — D3 misclassifies row M | M moved from "not reachable" to refused **inside** the resolver, with the message test 18 asserts. `ssh://` added beside W as the measured resolves-then-refused case, with two new measured rows (SSH, SSH2). |
| 9 | **Moderate** — Task 6 quotes "six limits" and addresses docs by line/ordinal, both of which item 2 moves | Task 6 now opens with an instruction to address every doc edit **by quoted text**, keeps whatever count item 2 left, and the replacement bullet gains a sentence cross-linking item 2's declared-unneeded bullet so the two cannot contradict. File Structure rows re-addressed by text. |
| 10 | **Moderate / RULING** — sync is correctly out of scope, wrong reason | **Adopted.** The bullet now says the agent can only run `sync` in the container, where the fallback base is `/repo` and the value becomes `/sub.git` — not a host path and **not a disclosure**; the consequence is a url nothing can be fetched from in a tree already populated at its gitlink, with `git submodule status`, preflight and the grader all unaffected, and item 10's host-side guard unable to see it. The "which is a HARVESTING rule, not code" option is dropped and the plan states no rule is added. D7's closing paragraph corrected to match. |
| 11 | **Moderate / RULING** — keep "read the literal", but the surrounding facts are missing and one is wrong | **Ruling adopted; one stated fact corrected.** The reviewer's post-condition asserts `git show 8232032:…preflight.py` is `"14"`. Re-measured: it is **`"13"` at `8232032`** and `"14"` at HEAD `2ddd68a` (item 3 landed after the round base). D8 now records **both** floors, replaces the wrong two-item enumeration with the rule ("every preceding round-2 item that changes what the gate asserts or what an absence means"), and carries the three-command post-condition with the corrected expected values. |
| 12 | **Moderate** — the new problem fires on an unmeasured absence and on every declared-unneeded relative submodule | Predicate changed to `entry["url_persisted"] is not None and not entry["url_persisted"].startswith(...)`. D8 gains a paragraph on why `or ""` is right on `url_declared` and wrong here (two kinds of "not measured": the failed read, and the never-registered declared-unneeded path), and names the spurious-NO-GO cost. New tests **45b** and **45c**. |
| 13 | **Minor** — the interior-`..` refusal is ruled on but never stated as an assumption | OQ2 now carries the *"assumed, not measured; no GitHub-wide search was run"* statement in the repo's usual form, and says the fix would be to widen the corpus rule rather than the resolver. `../a/./b.git` (measured as row DOTA) folded into test 21, which is renamed to cover `.` as well as `..`. |
| 14 | **Minor** — "33/33" is not reproducible from the plan | Replaced with the classification: reproduced identically on 18 rows, deliberately divergent on 13, refused inside the resolver on 1 (M), unreachable on 1 (J), resolved-then-refused-for-scheme on 2 (W, `ssh://`) — and the load-bearing claim stated plainly: zero accept-direction divergences. |
| 15 | **Minor** — verification arithmetic off by one | Was +46 for 26+10+9=45; findings 2 and 12 then added three preflight tests, so it is now **+48** (26 + 10 + 12), with the arithmetic written out. |

Smaller notes, all applied: test 30 renamed to `…_is_refused_at_derivation`
and three other *"refused at load"* phrasings corrected (`derive_submodules` is
not called by `load_task`); D6's fallback expression changed to item 2's
`by_path.get(path, (path, {}))`, bound once, with the note saying to keep item
2's form verbatim; `TASKS.md`'s bullet is now addressed by quoted text rather
than a line number (it is at 1317 in the current tree, not 1312 or 1313 — item
2's own edit will move it again).

**Five new measured rows** were taken while addressing findings 8 and 13 and
are in table R: SSH, SSH2, PORT, DOTA, DOTB. The resolver was re-executed over
all of them plus a `./../sub.git` chain — all six agree with the design's
stated classification, and PORT confirms that the remainder-only colon refusal
does not trip on a `repo_url` carrying a port.

### Review 2 → changes

All **3** open findings addressed; none disputed. One additional defect found
while verifying the blocker and fixed in the same pass.

| # | finding | change |
|---|---|---|
| 16 | **BLOCKER** — anchor 8 was a *description* (*"the guard's `and not` → `and`"*) rather than a quoted pair, and `and not` is not unique: `grep -c` gives **8** in `preflight.py`, `mutation_check.py` applies `original.replace(find, replace, 1)`, and the first hit is line 204 **inside `preflight_cache_key`'s docstring** — so the mutation would edit prose, the named test would stay green, and the entry would report MISSED as if the guard were unanchored | **Confirmed by re-measurement** (8 hits in `preflight.py`, 22 in `tasks.py`, first hit at 204 in a docstring, `replace(..., 1)` at `mutation_check.py:2015`). Anchor 8 is now the quoted pair `                        and not entry["url_persisted"].startswith(` → `                        and entry["url_persisted"].startswith(`, which contains `entry["url_persisted"]` and is unique by construction, and which inverts the resolved/unresolved test so test 41 fails cleanly rather than crashing. Task 5 also gains a preamble making the rule general: every `find` is a quoted line, `grep -c -- '<find>' <file>` must print exactly `1` before the entry is added, and a non-unique anchor is **extended with the following line, never shortened** (item 2's rule, same reason). The three new `find` strings were verified to have **0** occurrences in `tasks.py` at round base, so each is unique the moment the resolver lands. |
| 17 | **Minor** — finding 10's correction reached D7's prose but not the two strings that actually ship | Both corrected. Task 2 Step 5's docstring now reads *"the superproject's own path fallback — on the host that is a host path, and inside the container, where the tree is mounted at `/repo`, it is `/sub.git`"*, and drops *"still re-derives that way and is out of scope"* for *"re-derives that way and is deliberately not covered"*. D8's operator-visible problem message now ends *"…falls back to the superproject's own path, which inside the container is a path that does not exist."* — the message is produced by a gate reading a tree from inside the container, so the old wording was false in the only place it is emitted. |
| 18 | **Minor** — the classification did not account for all 33 rows: 16 rows counted as 18, T omitted, SSH/SSH2 double-counted | Replaced with a four-row table that **partitions all 33**: reproduced identically 18 (A, B, C, D, E, F, N, P, Q, T, U, W, X, Z, SSH, SSH2, PORT, DOTB), deliberately divergent 13, refused inside the resolver 1 (M), unreachable 1 (J) — 18 + 13 + 1 + 1 = 33, with the arithmetic written out. T is restored to *reproduced* with its reason. **One deviation from the reviewer's suggested wording, and it is deliberate:** the reviewer put W outside the reproduced set (17 + 13 + 1 + 1 = 32, plus W = 33); W *is* reproduced identically (git and the resolver both give `http://github.com/org/sub.git`), and being refused afterwards for its scheme is a fact about the row, not a class. The plan states W, SSH and SSH2 as *"three of the reproduced rows are then refused one check later"*, which keeps every row on exactly one side — the requirement the finding actually makes. The prose list one paragraph up was brought into line, and the two non-row shapes (local-path base, already-absolute url) are now stated separately as not being rows. |

**Found while verifying finding 16, not raised by either review: mutation
anchor 1 did not parse.** Its replacement was
`url_resolved=None if path in unneeded else (`, which would have headed the
continuation line `task.repo_url, declared, task_id=task.task_id, path=path),`
— keyword arguments in a parenthesized display, i.e. a `SyntaxError`
(`ast.parse` → *"invalid syntax. Maybe you meant '==' or ':=' instead of
'='?"*). A mutation that does not parse is not a mutation. Anchor 1 now targets
the resolver's own trigger, `    if not declared.startswith(
_RELATIVE_URL_PREFIXES):` → `    if True:`, which makes it return `declared`
unchanged — a truer statement of "no resolution happens" — and is a single
unique line. A note under the anchor table records both this and the fact that
anchor 2 goes red via an `IndexError` rather than an assertion, so a reader of
the mutation report is not surprised by the exception type.

## Reviews → changes

*(reviews 1 and 2 folded above)*
