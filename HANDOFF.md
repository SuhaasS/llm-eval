# Handoff — the pruned-mirror cache

**Scope.** Everything below concerns `ensure_pruned_mirror` and its neighbours in
[tasks.py](bakeoff/src/bakeoff/tasks.py). It is *not* the project backlog —
that is [TASKS.md](TASKS.md), and its five collection blockers are untouched by
any of this. This file exists because the subsystem was wrong three
times in the same way, and the next person should start from that fact rather
than rediscover it.

Written 2026-08-14 against `f67ccc2`; closeout pass landed 2026-08-14, seven
commits ending at the docs commit that carries this revision.

## What the subsystem is for, in one paragraph

The eval scores whether a model can fix a bug. `git clone --local` hardlinks the
whole object store, so run trees used to carry every object the upstream mirror
had — including the merge commit of the very PR the task was cut from. Measured
on `pallets/click`: `refs/heads/main` 181 commits ahead of the start state, and
`git log main --grep=3360` naming the fix. It is **differential** — only an arm
that greps history collects it — so it is a §6.4 confound recorded as capability.
The fix is a per-`(repo, base_sha)` pruned bare mirror that run trees clone from.
Building it is expensive, so it is cached, and **the cache is where every
subsequent defect has been.**

## The pattern, stated once

`_pack_fingerprint` detects changes to the **local pack set**. Three revisions in
a row treated it as proof that a cached mirror still satisfied `_verify_pruned`,
which asserts four things. Each round found a channel the fingerprint could not
see, added a term, and shipped:

| round | channel | what was added |
|---|---|---|
| `1d6bafd` | the mirror itself was never pruned | the whole subsystem |
| `ad193f2` | `git fetch` lands objects **loose**, moving no `.idx` | a loose-object count |
| `f67ccc2` | `objects/info/alternates`; a re-grown commit-graph; a real `.keep` | the cheap half of `_verify_pruned`, on the fast path |

**If you are about to add a fourth term to the fingerprint, stop.** The
fingerprint is a change-detector and should stay one. What makes a cached mirror
trustworthy is checking the invariant.

## Closed 2026-08-14 — the ledger

The six prose defects and deferred items 1–5 from the previous revision of this
file, one line each:

- **Six comments/docstrings misdescribing the code** — all corrected
  (`docs: correct the six claims f67ccc2 left misdescribing the prune cache`).
  The "vacuous pass" story around the `base_sha` guard was false in *three*
  places at once (`_ancestors` raises out of `rev-list`'s `check=True` first;
  the guard buys a named failure, and its mutation is caught by the `match=`
  mismatch). The heal test's marker rewrite and its redundant commit-graph
  param are gone; the `.keep` unlink is documented as availability, not leak
  prevention; the fingerprint docstring's refspec matches the test; the idx
  glob is `pack-*.idx` because `Path.glob` matches dotfiles.
- **The two uncovered IO paths** — covered
  (`test: cover the sweep's vanishing entry, ...`). Writing the publish-failure
  test exposed that the error message named `tmp`, a path the `finally`-block
  rmtree reclaims before the caller sees it, and told the operator to delete a
  `dest` that on the likelier branch is also gone. The message now names only
  `dest.parent` and instructs nothing.
- **Locale-strict git decoding** — `_git` now decodes pinned
  `encoding="utf-8", errors="replace"`
  (`fix: git output decodes pinned utf-8 ...`). The fixture is a `packed-refs`
  append, because APFS refuses a latin-1 loose-ref filename. And the old "a
  mangled ref gets deleted, the safe direction" claim was measured false:
  `update-ref --stdin` exits 0 deleting a nonexistent name, the ref survives
  the ref sweep, and `_verify_pruned`'s object sweep is what refuses the
  mirror — pinned end-to-end by
  `test_a_ref_the_decode_mangled_is_refused_not_leaked`.
- **Unbounded memory in the object sweep** — `_commits_outside` streams and
  stops one past `_OUTSIDE_SAMPLE`
  (`fix: the object sweep streams one past its sample ...`). The message
  distinguishes an exact count from "more than 3". The memory half is
  deliberately unanchored in the test; the mutation entry pins that the cap is
  enforced.
- **No lock in `src/`** — one `flock` per repo slug
  (`fix: one flock per repo slug ...`), covering *three* readers where the
  previous revision of this file listed two: `ensure_mirror`'s
  HEAD-exists/fetch race, the prune build-and-publish, and — surfaced by the
  fix — `materialize`'s run-tree clone, which a concurrent publish could
  rmtree mid-read. The prune body moved verbatim into `_build_pruned_mirror`
  so the lock could wrap it without re-indenting the six mutation anchors
  inside it. The acquisitions are sequential, never nested — flock
  self-conflicts across fds in one process.
- **Cache directory mode** — the pruned mirror publishes at `mkdtemp`'s 0700
  (`fix: clone into mkdtemp's 0700 ...`). The fix is a *deletion*: the
  `rmtree` before the clone rested on a comment claiming clone refuses an
  existing empty target — measured false on git 2.50.1 — and it was the
  rmtree that handed the directory to the umask.
- **Bonus, found by the closeout itself:** the mutation harness read its own
  stale bytecode (`fix: the mutation harness reads its own stale bytecode ...`).
  CPython validates a pyc on (mtime whole seconds, size); consecutive entries
  mutate `tasks.py` within one second, and the utf-8 and streaming entries
  both shrink it by exactly 25 bytes, so the batch intermittently loaded the
  previous entry's pyc and reported a MISS on a test that fails against its
  own source. Each entry now runs under a fresh `PYTHONPYCACHEPREFIX` —
  `PYTHONDONTWRITEBYTECODE` would not have helped, since it stops writing
  pycs, not reading a stale one.

## Still open — the remainder

1. **The `materialize` alternates assertion
   ([tasks.py](bakeoff/src/bakeoff/tasks.py), end of the clone block) is
   deliberately unanchored** — measured, deleting it fails no test, because
   reaching it needs `--dissociate` *and* the fast-path check to fail at once.
   Kept; noted so nobody deletes it as dead code.
2. **`ensure_mirror`'s full mirror is still at the umask's mercy** and holds a
   superset of the pruned objects, including the merged fix. The pruned
   mirror's 0700 does not cover it;
   `test_the_published_prune_is_not_world_readable` names this gap in its
   docstring. Matters once the task repos are private.
3. **`images.py` runs `git archive` against the full mirror outside any
   lock.** Harmless today — a fetch never removes `base_sha` — recorded so
   "the mirror race is closed" stays a qualified claim.

## Traps that already cost time — do not re-derive these

Each was tried, measured, and rejected. The measurements are in `f67ccc2`'s
commit message.

- **Adding `objects/info/alternates` to `_DERIVED_PATHS` so it gets unlinked.**
  On a true borrower (`git clone --shared --bare`, **zero** local objects), the
  unlink plus the gc exits **128** — `fatal: bad object refs/heads/main / failed
  to run repack` — and leaves `base_sha` unresolvable. That is a harder wedge
  than the leak. `--dissociate` on the clone is the answer; the path lives in
  `_FORBIDDEN_PATHS` (assert absence) and **must never** be in `_DERIVED_PATHS`
  (never unlink).
- **Publishing the mirror read-only.** `chmod -R a-w` really does block `git
  fetch` while `clone --local` still hardlinks — but `shutil.rmtree(dest,
  ignore_errors=True)` removes **nothing** from a read-only tree, so it wedges
  the very publish it depends on. It also rewrites four existing tests, has no
  portable assertion (root ignores it, and the agent runs as root on packs
  hardlinked from the cache), and closes a channel nothing in this repo uses.
  (The 0700 *directory* mode shipped above is not this: it leaves the tree
  writable and rmtree-able by its owner.)
- **`--dissociate` on `ensure_mirror`'s clone.** No-op for a remote URL, which is
  production. Measured: dropping it fails **no** test, while dropping it from the
  pruned clone fails one. It was removed rather than shipped unanchored.
- **Reordering the tmp sweep after `mkdtemp`.** Inert — it protects a process only
  from its own sweep, which was never at risk.
- **Believing a leftover `prune-*.tmp` wedges the task.** It does not: `tmp` comes
  from `mkdtemp`, 200 calls give 200 distinct names, and the clone can never fail
  into one. The sweep reclaims **disk**. The old comment claiming otherwise is
  corrected in both the code and the test.

## How to check you have not broken it

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

```bash
cd bakeoff && .venv/bin/python scripts/mutation_check.py tasks:
```

Current state after the closeout: **543 tests**, **125 mutations** across the
whole suite (**26** under `tasks:`), `tasks.py` at **93%**. A mutation
reporting `STALE ANCHOR` means a literal moved; `NO TESTS` means a selector
stopped matching. Both are hard failures, not warnings. Do **not** run
`mutation_check.py` concurrently with any other gate — it mutates
`src/bakeoff/tasks.py` in place, and a suite running beside it fails against
sources it never shipped (measured during the closeout: `verify_logger`'s
unit half red for exactly this reason, green solo).

Then the two gates, neither of which spends anything:

```bash
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight
```

`--force-preflight` is required — the preflight cache keys on
`manifest_digest|image|start_sha`, none of which a change to the prune moves, so
without it the gate prints `preflight cached PASS` and runs nothing.

## What "verified" currently means

Closeout verification, 2026-08-14, all four gates in sequence on a clean tree:

- Unit suite 543/543 green; mutation check 125/125 caught (batch run repeated
  five times after the pyc fix — stable).
- `verify_logger.py` **GATE PASSED** — dry run, offline smoke, all three arms
  `go`.
- Preflight **PASS** on the real task, `start_sha` still the pinned
  `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`.
- The lock is probed from inside each critical section (LOCK_EX|LOCK_NB from a
  second fd, expected refused); the published pruned mirror is 0700 under a
  pinned umask; a latin-1 ref pointing outside `base_sha`'s history is refused
  loudly by the object sweep, end-to-end.

**What that does not establish.** Nothing here says a model can solve the task —
`outcome` never becomes `RESOLVED` at harness time by design, and the offline
grader (TASKS.md blocker 5) does not exist. And every capability figure in the
repo still carries the caveats in TASKS.md, including that **21 of 25 stored
records were taken while the reference fix was reachable in the agent's own
repository.**
