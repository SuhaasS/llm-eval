# Pruned-Mirror Handoff Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out [HANDOFF.md](../../../HANDOFF.md) — fix the six shipped prose defects, land the four deferred code fixes (lock, streaming object sweep, utf-8 git output, cache permissions), cover the two uncovered lines, and update CLAUDE.md/HANDOFF.md/TASKS.md so the docs describe the code that exists.

**Architecture:** Every change stays inside `ensure_pruned_mirror` and its neighbours in `bakeoff/src/bakeoff/tasks.py`, plus the tests and docs that describe them. No schema change, no record change, no new module. The prose-heavy docstring style of this repo is load-bearing (comments carry the failure mode, not the mechanics) — every new comment must say what breaks without the line, and every claim about git behavior must state what it was verified against. All git behavior claims below were measured on this host (darwin, APFS, git 2.50.1) during plan review, 2026-08-14.

**Tech Stack:** Python 3.12, pytest, git ≥ 2.50, `fcntl.flock` (POSIX-only, matching the darwin/linux hosts this runs on).

## Global Constraints

- Working directory for all commands: `cd bakeoff`, interpreter `.venv/bin/python`.
- **Do not break a mutation anchor.** `scripts/mutation_check.py` matches exact source substrings *including leading indentation* (`run()` does `if find not in original: STALE ANCHOR`). Anchors inside `ensure_pruned_mirror`'s current body: the fast-path two-line `and not any(...)` literal (16-sp), `        except (ValueError, OSError):`, `    for pattern in (f"{scoped}*", "prune-*.tmp"):`, the 8-sp `--dissociate` clone line, `        if deletions:`, `            if dest.exists() or dest.is_symlink():`. **This is why Task 5 extracts the body into a helper instead of wrapping it in `with` — re-indenting the region would stale all six.** Other anchors touched-adjacent: the `cat-file -e` two-line literal in `_verify_pruned` (Task 1 edits only the raise body below it), `    if outside:` (Task 4 preserves it verbatim), `    loose = sum(1 for _ in (repo / "objects").glob("[0-9a-f][0-9a-f]/*"))` (untouched; the `glob("*.idx")` line above it is NOT an anchor), both `_GC_CONFIG` `-c` literals.
- **Do not break a `match=` string.** `"pruned mirror does not contain base_sha"` (test_tasks.py:931), `"survived the prune"` (test_tasks.py:548), `"carries future commit ids"` (test_tasks.py:952) must keep matching after any message edit.
- Full check after every task: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` then `cd bakeoff && .venv/bin/python scripts/mutation_check.py tasks:`.
- Comment style: prose that records the failure mode and what it was measured against. Never "what the next line does". A claim of measurement must only be copied where that measurement was actually made.
- Commit after each task; message style follows repo history (`fix:`/`docs:`/`test:` prefix, lowercase, a claim not a description, `--` for dashes, subject ≤ ~85 chars).

**Expected end state:** 543 tests (536 − 1 parametrize case + 8 new), `tasks:` mutations 22 → 26, whole-suite mutations 121 → 125. Task 7 writes the *measured* numbers into HANDOFF.md, not these predictions.

---

### Task 1: The six shipped prose defects (HANDOFF "Incorrect now")

**Files:**
- Modify: `bakeoff/tests/test_tasks.py:860-913` (heal test)
- Modify: `bakeoff/scripts/mutation_check.py:1145-1157` (.keep entry comment)
- Modify: `bakeoff/scripts/mutation_check.py:1225-1238` (base_sha entry comment)
- Modify: `bakeoff/src/bakeoff/tasks.py:874` (refspec in `_pack_fingerprint` docstring)
- Modify: `bakeoff/src/bakeoff/tasks.py:902` (idx glob)
- Modify: `bakeoff/src/bakeoff/tasks.py:946-952` (base_sha guard error message)
- Modify: `bakeoff/tests/test_tasks.py:916-925` (missing-base_sha test docstring)

**Interfaces:** none — prose and one glob pattern; no signature moves.

- [ ] **Step 1: Fix the heal test (HANDOFF items 1 and 2 together).**

Delete the `_grow_a_commit_graph` helper (test_tasks.py:865-866) and its parametrize entry, keeping only the `objects_gone` shape — the commit-graph shape is already pinned by `test_a_cache_that_stopped_being_pruned_is_not_served[commit_graph]` (verified: with a correct marker, `usable` short-circuits at the `_FORBIDDEN_PATHS` check before the fingerprint is ever read, so the two parametrize cases were testing the same structural check). With only `objects_gone` left the marker rewrite is unnecessary — emptying `objects/pack` empties the `.idx` set, so `_pack_fingerprint` mismatches on its own — and dropping the rewrite makes the test *stronger*: it now exercises the genuine stale-but-parseable marker path instead of a synthetic `'x' * 16`. Drop the parametrize decorator entirely and inline the one-caller helper. New test body:

```python
def test_a_damaged_prune_cache_heals_itself(tmp_path, upstream):
    """A pruned mirror that cannot be shown to be pruned must be REBUILT, not
    refused. An earlier revision re-verified it and raised -- before the
    rebuild block, so the damaged entry stayed on disk and every cell of every
    task on that (repo, base_sha) died on every re-invocation, with a message
    that reads like a prune bug and no instruction to delete anything.
    Measured: three identical TaskErrors in a row.

    Emptying `objects/pack` empties the `.idx` set, so the marker's recorded
    fingerprint mismatches the recomputed one on its own -- the genuine
    stale-but-parseable shape. An earlier revision rewrote the marker "to
    reach the guard at all", which was wrong twice over: `_verify_pruned` has
    one call site, on the freshly built tmp, so there is no guard on the
    cache path to reach; and the fast path's own structural checks catch the
    fingerprint-invisible shapes whatever the marker says (pinned in
    test_a_cache_that_stopped_being_pruned_is_not_served).

    Unlinking `objects/pack/*` rather than `objects/` on purpose: the first
    leaves a valid bare repository with an empty object store (`cat-file -e`
    exits 128 "Not a valid object name", `rev-parse --is-bare-repository`
    still true), which is the truncated-mirror shape. Removing `objects/`
    outright stops git recognising the directory at all."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    first = materialize(task, tmp_path / "a" / "repo", cache)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    for path in (pruned / "objects" / "pack").glob("*"):
        path.unlink()

    repo = tmp_path / "b" / "repo"

    assert materialize(task, repo, cache) == first
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{upstream['head']}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode != 0
```

- [ ] **Step 2: Fix mutation_check.py:1145-1157 (.keep entry comment).** Replace the comment's *"The unlink is the only thing that does"* — `_verify_pruned` also refuses a surviving `.keep`. The unlink is load-bearing for **availability**, not leak prevention: without it a repo carrying an inherited `pack-<hash>.keep` fails `_verify_pruned` on every rebuild forever, because every rebuild re-inherits the file. New comment (label, literals, selector unchanged):

```python
    (
        # A .keep makes gc refuse the pack wholesale, so the future survives
        # with rc=0 -- and `repack.packKeptObjects=true` does NOT save it
        # (measured). The unlink is load-bearing for AVAILABILITY, not leak
        # prevention: `_verify_pruned` refuses a surviving .keep either way,
        # but without the unlink a repo carrying an inherited pack-<hash>.keep
        # can never build -- every rebuild re-inherits the file and re-fails.
        # Note the fixture has to be a `pack-<hash>.keep`: a name matching no
        # existing pack is ignored by git entirely, which is what made an
        # earlier `stale.keep` fixture vacuous.
        "tasks: stop unlinking an inherited pack .keep",
```

- [ ] **Step 3: Fix the VACUOUS claim in all three places it appears (HANDOFF item 4, generalized — the same false claim ships in the mutation comment, the guard's own error message, and the test docstring).** The claim — that without the `cat-file -e` guard `_commits_outside` passes vacuously on an empty store — is false: `_ancestors` runs `rev-list base_sha` under `check=True` and raises first (verified: `rev-list` on an empty bare repo exits 128 `fatal: bad object`), so the object check never runs. The guard's real value is a **named** failure: `_git`'s generic message names neither the mirror path nor the cache defect, and the mutation is caught because that generic message fails the test's `match=`. Fix:

  1. `mutation_check.py:1225-1231` comment (label, literals, selector unchanged) →

```python
    (
        # The guard exists for a NAMED failure, not to stop a vacuous pass:
        # with it gone, `_ancestors` raises first -- `rev-list base_sha` runs
        # under check=True -- so `_commits_outside` never sees the empty
        # store. What the caller loses is the message: _git's generic
        # "git rev-list ... failed (exit 128)" names neither the mirror nor
        # the fact that this is a cache defect. The mutation is caught
        # because that generic message fails the test's match=.
        # Anchored by a direct call because the one production call site
        # passes a mirror it just cloned from a source `ensure_mirror`
        # already resolved base_sha in.
        "tasks: accept a pruned mirror whose base_sha is gone",
```

  2. `tasks.py:948-952` error message — the first sentence stays byte-compatible with the `match=` string; replace the rationale:

```python
        raise TaskError(
            f"{repo}: pruned mirror does not contain base_sha {base_sha}. "
            "Checked first for the message: with this guard gone, _ancestors "
            "raises out of rev-list's own check=True before the object sweep "
            "runs, naming neither the mirror nor that this is a cache defect."
        )
```

  3. `test_tasks.py:916-925` docstring — same correction:

```python
def test_a_pruned_mirror_missing_its_base_sha_is_refused(tmp_path):
    """Belt and braces, and called directly because nothing else can reach it:
    the one caller passes a mirror it just cloned from a source `ensure_mirror`
    resolved `base_sha` in, so on any passing path the commit is present by
    construction.

    Kept for the MESSAGE, not to stop a vacuous pass -- an earlier revision of
    this docstring claimed `_commits_outside` over an empty store would return
    `[]` and pass, but `_ancestors` raises out of `rev-list base_sha`'s own
    check=True before the sweep runs. What the guard buys is a failure that
    names the mirror and calls it a cache defect, instead of _git's generic
    exit-128 line from the wrong depth."""
```

- [ ] **Step 4: Fix tasks.py:874 refspec and tasks.py:902 glob (HANDOFF items 5 and 6).** In `_pack_fingerprint`'s docstring change `` `git fetch <upstream> +refs/*:refs/future/*` `` to `` `git fetch <upstream> +refs/heads/*:refs/future/*` `` (what the test actually runs). Change the `glob("*.idx")` to `glob("pack-*.idx")` with the reason beside it (the `loose = ...` anchor line below is untouched):

```python
    packs = sorted(
        # `pack-*.idx`, not `*.idx`: Path.glob matches dotfiles (measured:
        # `glob("*.idx")` returns an in-flight `.tmp-1-pack-abc.idx`;
        # `glob("pack-*.idx")` does not). Safe direction -- a spurious full
        # re-prune, never staleness -- but a fingerprint that can flap under
        # a concurrent repack is noise this module can avoid.
        (p.name, p.stat().st_size)
        for p in (repo / "objects" / "pack").glob("pack-*.idx")
    )
```

- [ ] **Step 5: Run the suite and the mutation gate.**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: all pass, count 535 (down **1**: the parametrize had two cases, one param was deleted and the survivor became a plain test).

Run: `cd bakeoff && .venv/bin/python scripts/mutation_check.py tasks:`
Expected: all 22 caught, no `STALE ANCHOR`, no `NO TESTS`.

- [ ] **Step 6: Commit.**

```bash
git add -A && git commit -m "docs: correct the six claims f67ccc2 left misdescribing the prune cache"
```

---

### Task 2: Cover tasks.py:1080 and tasks.py:1171, and fix the lie the second one exposes

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py:1169-1174` (publish error message)
- Test: `bakeoff/tests/test_tasks.py` (two new tests, after `test_an_abandoned_build_is_reclaimed_but_a_live_one_is_not`)

**Interfaces:** none.

**The exposed defect:** the publish `TaskError` says *"The build itself succeeded and is at {tmp}"* — but the `finally: shutil.rmtree(tmp, ignore_errors=True)` on line 1176 runs while the exception propagates, so `tmp` is gone before the caller sees the message. Rewording is the right fix, not preserving `tmp`: `tmp` and `dest` are siblings under `dest.parent` (mkdtemp `dir=dest.parent`), so EXDEV is impossible and ENOSPC is the live failure — where stranding a full mirror of disk makes the problem worse. The new message must not *instruct the operator to act on* `dest` either (the leading `{dest}:` locator stays — the module's path-scoped `TaskError`s open with the path): on the second `os.replace` (the likelier failure), `dest` has already been renamed to `doomed` and reclaimed, so "delete {dest} by hand" would name another missing path — and a leftover `dest` needs no manual deletion anyway, since the next invocation's publish handles an existing `dest` itself.

- [ ] **Step 1: Write the two tests.** The first is red only in coverage terms (it must not raise); the second is genuinely red against current code — its `"prune-" not in` assertion fails while the message still names `tmp`.

```python
def test_a_stale_entry_that_vanishes_mid_sweep_is_skipped(tmp_path, upstream, monkeypatch):
    """The sweep stats entries it did not create, in a cache directory shared
    by every repo -- so an entry can vanish (or become unstatable) between the
    glob and the stat when another invocation reclaims it first. That OSError
    must mean "skip this entry", never "fail the build": the sweep only
    reclaims disk, and a build that dies on someone else's leftover turns a
    janitor into a single point of failure."""
    from bakeoff.tasks import _STALE_TMP_AGE_S, pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)

    ghost = pruned.parent / "prune-vanishing.tmp"
    ghost.mkdir(parents=True)
    old = time.time() - _STALE_TMP_AGE_S - 60
    os.utime(ghost, (old, old))

    real_stat = Path.stat

    def stat_that_loses_the_race(self, **kwargs):
        if self.name == "prune-vanishing.tmp":
            raise OSError("stale file handle")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_loses_the_race)

    materialize(task, tmp_path / "run" / "repo", cache)  # must not raise
    # os.listdir, not ghost.exists(): Path.exists() routes through the
    # patched stat, and an OSError with errno=None is outside pathlib's
    # _ignore_error set, so exists() re-raises it instead of returning False.
    assert "prune-vanishing.tmp" in os.listdir(ghost.parent), \
        "an unstatable entry must be skipped, not deleted"


def test_a_failed_publish_raises_a_named_taskerror(tmp_path, upstream, monkeypatch):
    """The publish rename is the one step allowed to fail after a verified
    build, and its TaskError must not point the operator at a path that is
    already gone: the finally-block rmtree reclaims `tmp` while the exception
    propagates, and on the second os.replace `dest` has itself been renamed
    aside -- so an earlier message saying the build "is at {tmp}" named a
    directory the caller could never find."""
    import bakeoff.tasks as tasks_mod
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)

    real_replace = os.replace

    def replace_that_hits_a_full_disk(src, dst):
        if Path(dst) == pruned:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(tasks_mod.os, "replace", replace_that_hits_a_full_disk)

    with pytest.raises(TaskError, match="could not publish the pruned mirror") as exc:
        materialize(task, tmp_path / "run" / "repo", cache)
    assert "prune-" not in str(exc.value), \
        "the message names a build directory the finally block already reclaimed"
    assert not list(pruned.parent.glob("prune-*")), \
        "a failed publish stranded a build the finally block should reclaim"
```

- [ ] **Step 2: Run to verify the second is red.**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "vanishes_mid_sweep or failed_publish"`
Expected: `vanishes_mid_sweep` PASSES (pure coverage of the `continue`), `failed_publish` FAILS on the `"prune-" not in` assertion — the current message embeds `{tmp}`, whose basename starts `prune-`.

- [ ] **Step 3: Fix the message (tasks.py:1169-1174).**

```python
        except OSError as exc:
            raise TaskError(
                f"{dest}: could not publish the pruned mirror ({exc}). The "
                "build was verified and is discarded on the way out; whatever "
                f"this failure left under {dest.parent} is swept or rebuilt "
                "on the next invocation."
            ) from exc
```

- [ ] **Step 4: Run both tests green, then the full suite + mutation gate.**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "vanishes_mid_sweep or failed_publish"` → both PASS, then the full gates.

- [ ] **Step 5: Commit.**

```bash
git add -A && git commit -m "test: cover the sweep's vanishing entry, and stop the publish naming reclaimed paths"
```

---

### Task 3: `_git` decodes utf-8 with replacement, independent of locale

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py:683-696` (`_git`)
- Test: `bakeoff/tests/test_tasks.py` (two new tests)
- Modify: `bakeoff/scripts/mutation_check.py` (one new `tasks:` entry)

**Interfaces:** `_git` signature unchanged; output type stays `str`.

**Fixture constraint (measured):** APFS refuses filenames that are not valid UTF-8, so `git tag caf\xe9` exits 128 (`Unable to create '...refs/tags/caf\xe9.lock': Illegal byte sequence`) on the dev host — a loose-ref fixture cannot exist here. And a latin-1 *commit message* is useless as a substitute: git re-encodes it to UTF-8 on output, so it decodes cleanly. The working fixture is `packed-refs`, a plain file that takes arbitrary bytes on both filesystems; `git for-each-ref` then emits the raw `\xe9` (verified, rc=0).

**Consequence constraint (measured):** the old claim "a mangled ref name gets deleted, the safe direction" is false. The ref sweep builds `delete refs/tags/caf�` from the *decoded* name, and `git update-ref --stdin` exits 0 on a delete of a nonexistent ref — so the real ref survives the sweep. What actually contains it is `_verify_pruned`'s object-level post-condition: if the surviving ref keeps an object outside `base_sha`'s history alive through the gc, the sweep raises `survived the prune`. Loud refusal, not a leak — and the second test pins exactly that.

- [ ] **Step 1: Write the two failing tests.**

```python
def _pack_a_latin1_ref(repo: Path, sha: str) -> None:
    # packed-refs is a plain file, so it takes ref-name bytes APFS refuses
    # as a loose-ref filename (measured: `git tag caf\xe9` exits 128
    # "Illegal byte sequence" on APFS, while this file round-trips).
    with (repo / ".git" / "packed-refs").open("ab") as refs:
        refs.write(f"{sha} refs/tags/caf".encode() + b"\xe9\n")


def test_git_output_survives_bytes_the_locale_cannot_decode(tmp_path, upstream):
    """`text=True` decodes with the harness process's locale encoding and
    errors='strict', so one non-UTF-8 byte in git output -- a latin-1 ref
    name here -- turned any git call into a raw UnicodeDecodeError naming
    neither the repo nor the task. (A CI runner whose own LC_ALL=C makes the
    same crash out of plain UTF-8 output; pinning the encoding closes both.)
    """
    from bakeoff.tasks import _git

    _pack_a_latin1_ref(upstream["path"], upstream["base"])

    out = _git("for-each-ref", "--format=%(refname)", cwd=upstream["path"])
    assert "caf�" in out.stdout


def test_a_ref_the_decode_mangled_is_refused_not_leaked(tmp_path, upstream):
    """Replacement is survivable only because the object sweep backstops it,
    and this pins the chain. The ref sweep deletes by DECODED name, and
    `git update-ref --stdin` exits 0 deleting a ref that does not exist
    (measured, git 2.50.1) -- so a latin-1 ref pointing outside base_sha's
    history survives the sweep, keeps the future alive through the gc, and
    `_verify_pruned` refuses the mirror. An earlier claim that the mangled
    ref "gets deleted, which is the safe direction" was measured false; the
    safe direction is this loud refusal."""
    _pack_a_latin1_ref(upstream["path"], upstream["head"])

    task = load_task(_write_task(tmp_path / "set", upstream))

    with pytest.raises(TaskError, match="survived the prune"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")
```

(The chain was run end-to-end during plan review, 2026-08-14, darwin/APFS, git 2.50.1, with `_git` patched exactly as Step 3 specifies: `git clone --mirror` writes the refs *packed* — zero loose ref files, so the latin-1 name never touches an APFS filename — the pruned clone inherits the ref, the decoded-name delete exits 0 touching nothing, the gc keeps the head commit, and `_verify_pruned` raises `1 commit(s) outside …'s history survived the prune`. Both tests are committed to as written; fold this measured chain into the second test's docstring when implementing.)

- [ ] **Step 2: Run to verify both fail.**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "locale_cannot_decode or decode_mangled"`
Expected: the first FAILS with `UnicodeDecodeError` out of `subprocess.run` (host locale is UTF-8); the second fails the same way — the decode blows up before the prune gets far enough to refuse anything.

- [ ] **Step 3: Fix `_git`.**

```python
    result = subprocess.run(
        # utf-8 with replacement, never the locale: text=True made one
        # non-UTF-8 byte in git output -- or LC_ALL=C in the harness's own
        # environment -- raise UnicodeDecodeError out of any git call,
        # naming neither the repo nor the task. A replaced ref name is NOT
        # deleted by the ref sweep (update-ref --stdin exits 0 on a
        # nonexistent name, measured); it survives to _verify_pruned's
        # object sweep, which refuses the mirror -- loud, not leaked.
        ["git", *args], cwd=cwd, capture_output=True,
        encoding="utf-8", errors="replace", env=full_env, input=input,
    )
```

- [ ] **Step 4: Add the mutation entry** (in the `tasks:` block of `mutation_check.py`, alongside the others):

```python
    (
        # text=True decodes with the harness locale and errors='strict', so
        # one latin-1 byte in git output -- or LC_ALL=C on the CI runner --
        # crashed every git call with a UnicodeDecodeError naming neither
        # the repo nor the task. The pinned encoding is one keyword pair; a
        # refactor that "simplifies" it back to text=True is byte-for-byte
        # this mutation.
        "tasks: decode git output with the locale instead of pinned utf-8",
        "src/bakeoff/tasks.py",
        '        ["git", *args], cwd=cwd, capture_output=True,\n'
        '        encoding="utf-8", errors="replace", env=full_env, input=input,',
        '        ["git", *args], cwd=cwd, capture_output=True, text=True,\n'
        '        env=full_env, input=input,',
        "tests/test_tasks.py -k locale_cannot_decode",
        "not integration",
    ),
```

- [ ] **Step 5: Run both tests green, the full suite, and the mutation gate (now expecting 23 under `tasks:`).**

- [ ] **Step 6: Commit.**

```bash
git add -A && git commit -m "fix: git output decodes pinned utf-8 with replacement, not whatever the locale says"
```

---

### Task 4: `_commits_outside` streams instead of buffering the store listing

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py:910-931` (`_commits_outside`), `bakeoff/src/bakeoff/tasks.py:965-971` (caller message), one new module constant beside `_STALE_TMP_AGE_S`
- Test: `bakeoff/tests/test_tasks.py` (one new test; existing `prune_that_left_the_future_behind` keeps passing)

**Interfaces:** `_commits_outside(repo: Path, ancestors: set[str]) -> list[str]` — same signature; now returns **at most `_OUTSIDE_SAMPLE + 1` (= 4)** names and stops reading once it has them. The one-past-the-sample read is what lets the message distinguish "exactly N" from "more than N" — 3 outside commits (a ref-sweep miss) and 30,000 (a gc that did nothing) are different diagnoses, and *absence is recorded, never implied* forbids rendering them identically.

- [ ] **Step 1: Add the constant.**

```python
# How many outside commits the object sweep names before it stops reading.
# It reads ONE PAST this: the message can then distinguish "exactly N" from
# "more than N", instead of rendering a ref-sweep miss (a handful) and a gc
# that did nothing (tens of thousands) identically.
_OUTSIDE_SAMPLE = 3
```

- [ ] **Step 2: Write the failing test.**

```python
def test_the_object_sweep_stops_reading_after_the_evidence(tmp_path, upstream):
    """`_commits_outside` buffered the entire `--batch-all-objects` listing --
    659 KB on pruned click, ~229 MB extrapolated to a 5M-object monorepo --
    then split it, for an error message that only ever prints three names.
    Pinned by counting what it returns: one past the sample, on a repo with
    more than that outside, so the message can say "more than 3" without
    claiming a count it never took.

    The MEMORY half is deliberately unanchored: a rewrite that buffers the
    listing and slices it passes this test. What the test pins is the
    contract the message depends on -- the cap and the one-past read."""
    from bakeoff.tasks import _OUTSIDE_SAMPLE, _ancestors, _commits_outside

    repo_path = upstream["path"]
    for i in range(5):
        (repo_path / f"extra{i}.txt").write_text(f"{i}\n")
        _sh("git", "add", "-A", cwd=repo_path)
        _sh("git", "commit", "-q", "-m", f"extra {i}", cwd=repo_path)

    outside = _commits_outside(
        repo_path / ".git", _ancestors(repo_path / ".git", upstream["base"])
    )
    assert len(outside) == _OUTSIDE_SAMPLE + 1
```

- [ ] **Step 3: Run to verify it fails** (the `upstream` fixture has 2 commits, +5 = 7, ancestors of base = 1, so 6 outside; current code returns all 6).

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k stops_reading`
Expected: FAIL, `assert 6 == 4`.

- [ ] **Step 4: Rewrite `_commits_outside` as a streaming read with an early break.**

```python
def _commits_outside(repo: Path, ancestors: set[str]) -> list[str]:
    """Commit objects present in `repo` that `ancestors` does not contain --
    at most `_OUTSIDE_SAMPLE + 1` of them, because the error message this
    feeds prints `_OUTSIDE_SAMPLE` names plus whether there were more, and
    the caller branches on emptiness alone.

    THE post-condition, and it is stated over objects rather than refs because
    the leak is an object-level one: `clone --local` hardlinks the whole store,
    so the merged fix is readable through `cat-file -p`, `--batch-all-objects`
    and `fsck --lost-found` with no ref pointing at it. A ref-level check is
    satisfied by a prune that leaves the oracle sitting there.

    `--batch-all-objects` lists cruft-pack objects too, which is what makes
    this catch the `gc.cruftPacks` and `gc.bigPackThreshold` bypasses rather
    than merely surviving them. `--unordered` because the sort is pure cost.

    STREAMED, not buffered: the listing is one line per object, 659 KB on
    pruned click and ~229 MB extrapolated to a 5M-object monorepo, and
    `.splitlines()` plus the comprehension held three copies of it at once.
    stderr is not drained while stdout streams, so a git that filled the
    stderr pipe (64 KB -- measured to the byte on darwin 25.5.0, and the
    linux default) mid-listing would deadlock -- accepted rather than paid
    for with a drain thread, and recorded here. That git stays under it is
    inference from the command's shape (`--batch-check` has nothing to
    narrate), not a measurement.
    """
    with subprocess.Popen(
        ["git", "cat-file", "--batch-all-objects", "--unordered",
         "--batch-check=%(objecttype) %(objectname)"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        outside: list[str] = []
        try:
            for line in proc.stdout:
                kind, _, name = line.rstrip("\n").partition(" ")
                if kind == "commit" and name not in ancestors:
                    outside.append(name)
                    if len(outside) > _OUTSIDE_SAMPLE:
                        # Enough evidence; stop paying for the rest of the
                        # listing. The kill makes the exit code meaningless,
                        # which is fine -- the hits are already a harder
                        # failure than any exit code.
                        proc.kill()
                        break
            else:
                stderr_text = proc.stderr.read()
                if proc.wait() != 0:
                    raise TaskError(
                        f"git cat-file --batch-all-objects failed "
                        f"(exit {proc.returncode}) in {repo}: "
                        f"{stderr_text.strip()}"
                    )
        finally:
            proc.kill()
    return outside
```

(`Popen.__exit__` closes both pipes and waits, so the fds do not leak — verified that the bare-`Popen` version warns under `-W error::ResourceWarning`. The `finally: proc.kill()` is a no-op after a completed `wait()`.)

- [ ] **Step 5: Adjust the caller's message** — the count is honest on both sides of the cap. The mutation anchor `    if outside:` and the `match=` substring `survived the prune` are both preserved:

```python
    outside = _commits_outside(repo, _ancestors(repo, base_sha))
    if outside:
        count = (str(len(outside)) if len(outside) <= _OUTSIDE_SAMPLE
                 else f"more than {_OUTSIDE_SAMPLE}")
        raise TaskError(
            f"{repo}: {count} commit(s) outside {base_sha}'s history "
            f"survived the prune, e.g. "
            f"{', '.join(sorted(outside)[:_OUTSIDE_SAMPLE])}. The run tree "
            "would contain the merged fix the task was cut from."
        )
```

- [ ] **Step 6: Add the mutation entry** — the cap is what bounds the peak, so its enforcement gets an anchor (the `find` is at 20-space indent: function body 4 → `with` 8 → `try` 12 → `for` 16 → `if kind` 20):

```python
    (
        # A MEMORY anchor -- the test docstring declares the memory half
        # unanchored (a buffer-and-slice passes it), and this is the other
        # half: the test pins the cap, this pins that the cap is enforced.
        # Neutralising the break changes nothing on a correctly pruned
        # mirror (the list is empty either way) and nothing in the message
        # ("more than 3" still renders); what it removes is the bound on
        # `outside`, which on the failure this post-condition exists to
        # catch -- a gc that did nothing over a monorepo -- accumulates
        # every outside commit, the exact unbounded peak the streaming
        # rewrite was for. The test catches it by counting the return.
        "tasks: read the whole listing instead of stopping past the sample",
        "src/bakeoff/tasks.py",
        "                    if len(outside) > _OUTSIDE_SAMPLE:\n",
        "                    if False:\n",
        "tests/test_tasks.py -k stops_reading",
        "not integration",
    ),
```

- [ ] **Step 7: Run the new test, `-k prune_that_left_the_future_behind`, `-k decode_mangled`, the full suite, the mutation gate (24 under `tasks:`).**

- [ ] **Step 8: Commit.**

```bash
git add -A && git commit -m "fix: the object sweep streams one past its sample instead of buffering the listing"
```

---

### Task 5: One flock per repo slug closes the cache-build races

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (new `_repo_lock` helper above `ensure_mirror`; body of `ensure_pruned_mirror` extracted verbatim into `_build_pruned_mirror`; lock acquisitions in `ensure_mirror`, `ensure_pruned_mirror`, and around `materialize`'s clone)
- Test: `bakeoff/tests/test_tasks.py` (two new tests)
- Modify: `bakeoff/scripts/mutation_check.py` (one new `tasks:` entry)

**Interfaces:**
- Produces: `_repo_lock(repo_url: str, cache_root: Path)` — a `contextlib.contextmanager` yielding `None`, exclusive for the duration; lock file at `cache_root/repos/<slug>.lock` (i.e. `mirror_path(...).with_suffix(".lock")` — cannot collide with `<slug>.git` or any `prune-*` sweep glob).
- Produces: `_build_pruned_mirror(source: Path, dest: Path, base_sha: str) -> Path` — the current body of `ensure_pruned_mirror` from the `marker = ...` line down, moved verbatim. The region references only `source`, `dest`, `base_sha` and module globals (verified), so **every line keeps its exact current indentation and all six mutation-anchor literals inside it survive byte-for-byte** — this extraction, not an inline `with`, is the whole reason the function exists. It gets its own docstring saying so, because the wrapper's comment is not where someone editing the body looks:

```python
def _build_pruned_mirror(source: Path, dest: Path, base_sha: str) -> Path:
    """The body of `ensure_pruned_mirror`, split out as a SEAM, not a
    decomposition: the lock had to wrap this whole region, and wrapping it
    in-place would re-indent every line -- six of which are exact-substring
    mutation anchors in `mutation_check.py`, indentation included. Inlining
    this back "for tidiness" stales all six. Runs entirely under the caller's
    `_repo_lock`; see `ensure_pruned_mirror` for why the fast path is inside
    it too."""
```

**Design constraints (from HANDOFF, the code, and review measurements):**
- One lock per repo slug, per HANDOFF ("Both are one `flock` on `cache_root/repos/<name>`"). Slug, not `(repo, base_sha)`, because both keys share one `source` mirror that `ensure_mirror` may `fetch` into and one `dest.parent` sweep namespace — a per-base_sha lock would let a fetch run during another key's prune.
- **`flock` self-conflicts across two fds in one process** (verified: second `LOCK_EX|LOCK_NB` on a fresh fd → `BlockingIOError`), and `ensure_pruned_mirror` calls `ensure_mirror`. So the acquisitions must not nest: `ensure_pruned_mirror` takes the lock only *after* `ensure_mirror` returns (having taken and released it itself). No other call path nests (`images.py:175` calls `ensure_mirror` standalone).
- The whole fast path goes inside the lock, not just the rebuild: the publish has a window between `os.replace(dest, doomed)` and `os.replace(tmp, dest)` where `dest` does not exist, and an unlocked reader in that window sees no marker and starts a redundant rebuild. Serializing it is affordable — `run_matrix` is single-process today, and the fast path is `exists()` calls, one glob sweep, `_pack_fingerprint`'s scan of `objects/pack` and every `objects/xx/`, and one `cat-file` fork.
- `materialize`'s own `git clone --local` from the published mirror also takes the lock: without it, a concurrent invocation's publish renames the mirror aside and `rmtree`s it *while the clone is reading it* — the clone survives on hardlinks only until the rmtree wins the race. This closes the third reader; `images.py`'s `git archive` against the full mirror stays outside any lock and is recorded as residual in Task 7's HANDOFF update.
- The `.lock` file is never cleaned up, deliberately: unlinking a lock file another process holds open breaks the mutual exclusion for every later acquirer.

- [ ] **Step 1: Write the failing tests.**

```python
def _assert_flock_held(lock_path: Path) -> None:
    import fcntl
    with open(lock_path) as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(probe, fcntl.LOCK_UN)
    raise AssertionError(f"{lock_path} was not held")


def test_the_prune_and_the_run_tree_clone_run_under_the_repo_lock(
    tmp_path, upstream, monkeypatch
):
    """Two invocations on the same (repo, base_sha) race twice: both prune
    at once, and one publishes -- rename aside, rmtree -- while the other's
    materialize is cloning from the mirror being reclaimed. The lock is
    asserted from INSIDE each critical section, with a probe fd taking
    LOCK_EX|LOCK_NB and expecting to be refused, because a lock tested only
    by its file existing is a lock nothing proves is taken."""
    import bakeoff.tasks as tasks_mod
    from bakeoff.tasks import mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    lock_path = mirror_path(str(upstream["path"]), cache).with_suffix(".lock")

    real_strip = tasks_mod._strip_derived
    real_git = tasks_mod._git
    probed = []

    def strip_while_probing(repo):
        _assert_flock_held(lock_path)
        probed.append("prune")
        return real_strip(repo)

    def git_while_probing(*args, **kwargs):
        if args and args[0] == "clone" and "--local" in args and "--mirror" not in args:
            _assert_flock_held(lock_path)
            probed.append("run-tree clone")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks_mod, "_strip_derived", strip_while_probing)
    monkeypatch.setattr(tasks_mod, "_git", git_while_probing)
    materialize(task, tmp_path / "run" / "repo", cache)
    assert probed == ["prune", "run-tree clone"], \
        f"a probe never fired: {probed}"


def test_the_mirror_fetch_path_runs_under_the_repo_lock(tmp_path, upstream, monkeypatch):
    """`ensure_mirror`'s `HEAD`-exists check is the same race one level up:
    `git clone` writes HEAD early, so a second process can `fetch --prune`
    into a half-populated clone. The probe rides the mirror clone itself."""
    import bakeoff.tasks as tasks_mod
    from bakeoff.tasks import ensure_mirror, mirror_path

    cache = tmp_path / "cache"
    lock_path = mirror_path(str(upstream["path"]), cache).with_suffix(".lock")

    real_git = tasks_mod._git
    probed = []

    def git_while_probing(*args, **kwargs):
        if args and args[0] == "clone" and "--mirror" in args and "--local" not in args:
            _assert_flock_held(lock_path)
            probed.append("mirror clone")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(tasks_mod, "_git", git_while_probing)
    ensure_mirror(str(upstream["path"]), upstream["base"], cache)
    assert probed == ["mirror clone"], "the probe never fired; the clone path was not exercised"
```

- [ ] **Step 2: Run to verify both fail** (`FileNotFoundError` opening the `.lock` probe, or the sentinel assertion).

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k repo_lock`
Expected: FAIL.

- [ ] **Step 3: Implement.** Add `import contextlib` and `import fcntl` to `tasks.py`'s imports. Add above `ensure_mirror`:

```python
@contextlib.contextmanager
def _repo_lock(repo_url: str, cache_root: Path):
    """One exclusive flock per repo slug, held across every cache build and
    every read that a concurrent build could tear.

    Three critical sections take it: `ensure_mirror` (git clone writes HEAD
    early, so a second process's HEAD-exists check passes on a half-populated
    clone and `fetch --prune`s into it), the prune build-and-publish (two
    processes pruning one (repo, base_sha) -- the second's publish renames
    the first's live mirror aside), and `materialize`'s run-tree clone (the
    publish rmtree's the directory that clone is reading). Neither race was
    ever observed as corruption; both were derived from the code -- at
    f67ccc2, `grep -rE 'flock|fcntl|O_EXCL'` over src/ found zero hits, and
    this function is the first.

    Per SLUG, not per (repo, base_sha): both keys share one source mirror
    that `ensure_mirror` may fetch into, and one dest.parent sweep namespace,
    so a per-key lock would let a fetch run during another key's prune.

    The acquisitions MUST NOT nest: flock self-conflicts across two fds in
    one process (measured: LOCK_EX|LOCK_NB on a fresh fd raises
    BlockingIOError while the first is held), so `ensure_pruned_mirror`
    calls `ensure_mirror` -- which locks and releases -- BEFORE taking the
    lock itself.

    The lock file is separate from the mirror because the publish
    `os.replace`s the mirror away -- a lock on a renamed path guards nothing
    -- and it is never unlinked: removing a lock file another process holds
    open silently breaks the exclusion for every later acquirer.
    """
    lock_path = mirror_path(repo_url, cache_root).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
```

In `ensure_mirror`: after the existing `mirror.parent.mkdir(...)`, wrap the rest of the body in `with _repo_lock(repo_url, cache_root):` (no mutation anchors live in `ensure_mirror`; the re-indent is safe).

`ensure_pruned_mirror` becomes a shell — its docstring stays put; the body from `marker = dest / _PRUNE_MARKER` through `return dest` moves **verbatim, unre-indented**, into `_build_pruned_mirror(source, dest, base_sha)` defined directly above it:

```python
def ensure_pruned_mirror(repo_url: str, base_sha: str, cache_root: Path) -> Path:
    """<existing docstring, unchanged>"""
    # `ensure_mirror` locks and releases inside itself; taking the lock only
    # after it returns is what keeps the two acquisitions from nesting, which
    # flock punishes with a same-process deadlock (see _repo_lock). The whole
    # fast path sits inside the lock because the publish has a window where
    # `dest` does not exist, and an unlocked reader there starts a redundant
    # rebuild. The body lives in _build_pruned_mirror so this wrapper could
    # be added without re-indenting the six mutation anchors inside it.
    source = ensure_mirror(repo_url, base_sha, cache_root)
    dest = pruned_mirror_path(repo_url, base_sha, cache_root)
    with _repo_lock(repo_url, cache_root):
        return _build_pruned_mirror(source, dest, base_sha)
```

In `materialize`, the clone comes under the lock (the checkout, remote-remove and reflog-expire run on the private run tree and stay outside):

```python
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Under the repo lock: a concurrent invocation's publish renames the
    # mirror aside and rmtree's it, and a clone reading it at that moment
    # survives only until the rmtree wins. The clone is the last reader of
    # shared state; everything after it touches only this run's tree.
    with _repo_lock(task.repo_url, cache_root):
        _git("clone", "--local", "--no-checkout", str(mirror), str(dest))
    _git("checkout", "--detach", task.base_sha, cwd=dest)
```

- [ ] **Step 4: Add the mutation entry.**

```python
    (
        # An unheld lock is indistinguishable from a held one at every call
        # site -- the with-block enters, the build runs, nothing raises. The
        # probe tests take LOCK_EX|LOCK_NB from a second fd and expect to be
        # refused, which only a real LOCK_EX can do.
        "tasks: open the lock file without taking the lock",
        "src/bakeoff/tasks.py",
        "        fcntl.flock(fd, fcntl.LOCK_EX)\n",
        "        pass\n",
        "tests/test_tasks.py -k repo_lock",
        "not integration",
    ),
```

(The `finally`'s `LOCK_UN` on a never-locked fd is a no-op, so the mutated code runs to completion and only the probes catch it.)

- [ ] **Step 5: Run the two new tests, full suite, mutation gate (25 under `tasks:`).** The suite's many `materialize` calls now exercise the lock on every path; watch for a hang, which would mean a nested acquisition somewhere.

- [ ] **Step 6: Commit.**

```bash
git add -A && git commit -m "fix: one flock per repo slug so concurrent invocations stop building over each other"
```

---

### Task 6: The published mirror keeps mkdtemp's 0700

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (in `_build_pruned_mirror` after Task 5: delete the `shutil.rmtree(tmp)` and correct the comment above it)
- Test: `bakeoff/tests/test_tasks.py` (one new test)
- Modify: `bakeoff/scripts/mutation_check.py` (one new `tasks:` entry)

**Interfaces:** none.

**The fix is a deletion, not a chmod (measured).** The current code `rmtree`s the `mkdtemp` directory because of a comment claiming clone "tolerates an empty one only if it does not exist" — measured false: `git clone --mirror --local` into an **existing empty** directory exits 0 (git 2.50.1), and it *keeps the directory's mode*: a pre-existing 0700 stays 0700, while letting git create it yields the umask. Deleting the `rmtree` fixes the permission exposure and removes a measurably wrong claim in one move. `os.replace` preserves the inode and its mode, and nothing after the publish re-chmods, so 0700 holds on the published cache and across the fast path.

- [ ] **Step 1: Write the failing test.** The umask is pinned inside the test — otherwise the verdict is a property of the dev host's umask, the exact laptop-dependence `test_the_prune_holds_under_a_hostile_gitconfig` was written to avoid. Under `umask(0)` the pre-fix state is loud: the clone creates the directory 0777.

```python
def test_the_published_prune_is_not_world_readable(tmp_path, upstream):
    """`mkdtemp` gives 0700, but the build rmtree'd that directory so `git
    clone` could create it fresh -- under the umask -- and `os.replace`
    published THAT as the permanent cache. The exposure is the whole cached
    repository, forever, not the build window; it matters the moment the
    task repos are private.

    The umask is pinned to 0 so the verdict is a property of this repository
    rather than of the laptop -- under a 077 umask the defect is invisible.
    os.umask is process-global, so this test is not xdist-safe; the suite
    runs serially today, and this line is the notice if that changes.

    WHAT THIS DOES NOT COVER: `ensure_mirror`'s full mirror sits in the same
    cache directory at the umask's mercy and holds a superset of these
    objects, including the merged fix. That exposure is recorded in
    HANDOFF.md's open list, not silently closed here."""
    from bakeoff.tasks import pruned_mirror_path

    cache = tmp_path / "cache"
    task = load_task(_write_task(tmp_path / "set", upstream))
    old_umask = os.umask(0)
    try:
        materialize(task, tmp_path / "run" / "repo", cache)
    finally:
        os.umask(old_umask)

    pruned = pruned_mirror_path(str(upstream["path"]), upstream["base"], cache)
    assert (pruned.stat().st_mode & 0o777) == 0o700
```

- [ ] **Step 2: Run to verify it fails** (mode 0o777 under the pinned umask).

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k world_readable`
Expected: FAIL, `assert 0o777 == 0o700` (rendered in decimal).

- [ ] **Step 3: Delete the rmtree and fix the comment** (in `_build_pruned_mirror`; current lines tasks.py:1089-1091):

```python
    tmp = Path(tempfile.mkdtemp(
        dir=dest.parent, prefix=f"{scoped}{os.getpid()}-", suffix=".tmp"
    ))
    try:
        # The clone goes INTO mkdtemp's directory, keeping its 0700: an
        # earlier revision rmtree'd it first on the claim that clone
        # "tolerates an empty target only if it does not exist" -- measured
        # false (git 2.50.1 clones into an existing empty directory, rc=0,
        # mode kept) -- so the clone recreated it under the umask and
        # os.replace published a world-readable cache, permanently.
        _git("clone", "--mirror", "--local", "--dissociate", str(source), str(tmp))
```

- [ ] **Step 4: Add the mutation entry.** The `find` is the comment-free tail of the block so it stays byte-stable; the mutation reintroduces the rmtree:

```python
    (
        # Reintroducing the rmtree hands the directory back to the umask:
        # the clone recreates it 0755 (or 0777 under umask 0) and os.replace
        # publishes that as the permanent cache -- the whole cached
        # repository readable by every local user, which matters the moment
        # the task repos are private. The test pins umask(0) so the verdict
        # is the repository's, not the laptop's.
        "tasks: hand the published mirror's mode back to the umask",
        "src/bakeoff/tasks.py",
        '        _git("clone", "--mirror", "--local", "--dissociate", str(source), str(tmp))',
        '        shutil.rmtree(tmp)\n'
        '        _git("clone", "--mirror", "--local", "--dissociate", str(source), str(tmp))',
        "tests/test_tasks.py -k world_readable",
        "not integration",
    ),
```

(Two entries now share that `find` literal — verified safe: `mutation_check.run()` does `original.replace(find, replace, 1)` with no uniqueness assumption and restores the pristine original in a `finally`, so entries cannot interact, and the literal occurs exactly once in `tasks.py`.)

- [ ] **Step 5: Run the test green, full suite, mutation gate (26 under `tasks:`).**

- [ ] **Step 6: Commit.**

```bash
git add -A && git commit -m "fix: clone into mkdtemp's 0700 instead of rmtree-ing it back to the umask"
```

---

### Task 7: Docs converge on the code, then the gates

**Files:**
- Modify: `CLAUDE.md:117` (cached-artifact invariant bullet)
- Modify: `HANDOFF.md` (closed items ledger; remaining open list; counts)
- Modify: `TASKS.md:109-116` (the pruned-mirror open list — CLAUDE.md's docs table says TASKS.md is "Open work only", so closed items must leave it)
- Modify: `tasks/todo.md` (review section)

- [ ] **Step 1: Rewrite the CLAUDE.md cached-artifact bullet** so it names the structural checks and `_FORBIDDEN_PATHS`, which it currently mentions zero times:

```markdown
- **A cached artifact is trusted only on the invariant re-checked against the artifact itself — the fingerprint is a change-detector, not the proof.** `_pack_fingerprint` sees the *local pack set*: `pack-*.idx` names and sizes (what a run tree's `git add -A` cannot move) plus a loose-object count, because `git fetch` into a cached mirror lands objects *loose* under `transfer.unpackLimit` and moves no index. Three shapes leave that digest byte-identical while handing the agent the fix — `objects/info/alternates`, a re-grown commit-graph, a real `pack-<hash>.keep` — so the fast path re-asserts the cheap half of `_verify_pruned` beside it: no `_FORBIDDEN_PATHS` entry exists, no `*.keep` exists, `base_sha` resolves. Three rounds each added a term the fingerprint could not see; a fourth term is the wrong move — check the invariant instead. And any unusable cache state **rebuilds rather than raises**: raising left the damaged entry on disk, unmaterializable until an operator deleted it by hand — three identical `TaskError`s, measured — while the rebuild's own post-condition, on the `tmp` it just built, still raises and still gates the rename: refusing a prune this code just built is a code defect, refusing one it found on disk is a cache defect, and only the second is recoverable. Checked against the preflight cache, the one other place a verdict outlives the artifact it describes — its tree is `rmtree`d and re-materialized on every invocation whether the key hits or not (`run_matrix.py:137-145`), so only the verdict is cached, never an artifact the key describes from inside, which is what this one was.
```

- [ ] **Step 2: Update HANDOFF.md.** Move the six "Incorrect now" items and deferred items 1–5 to a short closed ledger (one line each: what shipped, in which commit). The open list that remains:
  - item 6 (the unanchored `materialize` alternates assertion — still kept, still unanchored);
  - `ensure_mirror`'s full mirror is still umask-mode and holds a superset of the pruned objects (Task 6's test docstring names it);
  - `images.py:175` runs `git archive` against the full mirror outside any lock (harmless today — a fetch never removes `base_sha` — recorded so the closeout does not read as "the mirror race is closed" without qualification);
  - HANDOFF item 3's old wording ("a mangled ref name … gets deleted, which is the safe direction") is corrected wherever it was copied — the measured truth is the Task 3 chain: undeletable by decoded name, survives the ref sweep, refused by the object sweep.
  The lock's ledger entry says it closed *three* readers (the mirror fetch path, the prune build-and-publish, and `materialize`'s run-tree clone) where HANDOFF item 1 listed two — so a later reader does not conclude item 1 was mis-transcribed. Keep the "Traps" section verbatim — it is the part written to outlive the work. Update the "What 'verified' means" section and the counts (tests, mutations, `tasks:` count, coverage) with the **measured** numbers from Step 4's runs.

- [ ] **Step 3: Update TASKS.md:109-116** — the pruned-mirror section shrinks to the open remainder from Step 2, and add the review section to `tasks/todo.md` (repo convention: one section per finished task, what changed, what the gates said).

- [ ] **Step 4: Run every gate.**

Run, in order:
```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python scripts/mutation_check.py
cd bakeoff && .venv/bin/python scripts/verify_logger.py
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight
```
Expected: suite green at 543; mutation check all caught, 125 total, 26 under `tasks:` (the full run, not just `tasks:` — nothing else should move, and the run proves it); `GATE PASSED`; preflight `PASS` with `start_sha` still `33575cc0…`. `--force-preflight` is required — the preflight cache keys on `manifest_digest|image|start_sha`, none of which these changes move. If no Docker daemon is available, record `GATE INCOMPLETE` verbatim in todo.md rather than skipping silently.

- [ ] **Step 5: Commit.**

```bash
git add -A && git commit -m "docs: the invariant names its structural checks, and the handoff records what closed"
```
