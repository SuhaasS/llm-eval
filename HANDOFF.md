# Handoff — the pruned-mirror cache

**Scope.** Everything below concerns `ensure_pruned_mirror` and its neighbours in
[tasks.py](bakeoff/src/bakeoff/tasks.py). It is *not* the project backlog —
that is [TASKS.md](TASKS.md), and its five collection blockers are untouched by
any of this. This file exists because the subsystem has now been wrong three
times in the same way, and the next person should start from that fact rather
than rediscover it.

Written 2026-08-14, against `f67ccc2`.

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

## Incorrect now — six items, all mine, all cheap

These were listed as in-scope for `f67ccc2` and were not shipped. Each is a
comment or a docstring that misdescribes what the code does, which this repo
treats as a defect because the prose is the only place the failure mode is
recorded.

| # | file:line | what it says | why it is wrong |
|---|---|---|---|
| 1 | [test_tasks.py:888](bakeoff/tests/test_tasks.py:888) | the marker rewrite is *"to reach the guard at all"* | there is no guard on the cache path — `_verify_pruned` has one call site, on the freshly built `tmp`. Since `f67ccc2` it is doubly wrong: the fast-path structural checks catch that damage whatever the marker says. |
| 2 | [test_tasks.py:871](bakeoff/tests/test_tasks.py:871) | `_grow_a_commit_graph` param of the heal test | redundant with `test_a_cache_that_stopped_being_pruned_is_not_served[commit_graph]`. `_gut_the_object_store` is **not** redundant — nothing else empties the pack — so delete one param, not the test. |
| 3 | [mutation_check.py:1148](bakeoff/scripts/mutation_check.py:1148) | the `.keep` unlink is *"the only thing that does"* | `_verify_pruned` also refuses. The unlink is load-bearing for **availability** (without it a repo carrying an inherited `.keep` can never build), not for leak prevention. |
| 4 | [mutation_check.py:1228](bakeoff/scripts/mutation_check.py:1228) | the `base_sha` guard stops a *"VACUOUS"* pass | it is caught by the `match=` string. `_ancestors` raises from `rev-list`'s own `check=True` first, so the documented mechanism never runs. |
| 5 | [tasks.py:874](bakeoff/src/bakeoff/tasks.py:874) | refspec `+refs/*:refs/future/*` | the test uses `+refs/heads/*:refs/future/*`. |
| 6 | [tasks.py:902](bakeoff/src/bakeoff/tasks.py:902) | `glob("*.idx")` | `Path.glob` returns dotfiles, so an in-flight `.tmp-<pid>-pack-*.idx` counts. Safe direction — a spurious full re-prune, never staleness — but it should be `pack-*.idx`. |

**Also**: [CLAUDE.md](CLAUDE.md) mentions `alternates` and `_FORBIDDEN_PATHS`
**zero** times. The cached-artifact invariant there describes a fingerprint that
is no longer the only check.

## Pending — deferred on purpose

Ranked by what can silently corrupt a result. None of these can hand the agent
the answer; that is why they were deferred rather than fixed.

1. **No lock anywhere in `src/`** (`grep -rE 'flock|fcntl|O_EXCL'` → zero hits).
   The tmp sweep is now scoped by `dest.name` and gated on an age floor, which
   bounds the blast radius; two invocations pruning the **same** `(repo,
   base_sha)` still race, and `ensure_mirror`'s `if not (mirror / "HEAD")
   .exists()` is the same race one level up — `git clone` writes `HEAD` early, so
   a second process can `fetch --prune` into a half-populated clone. Both are one
   `flock` on `cache_root/repos/<name>`.
2. **`_commits_outside` buffers the entire object listing.** 659 KB on pruned
   click, ~229 MB of stdout extrapolated to 5M objects, plus `.splitlines()` and
   the ancestor set — three copies. Once per `(repo, base_sha)`, so bounded in
   frequency, not in peak memory. The target dataset is Pindrop monorepos. The
   streaming `Popen` fix is ~5 lines and should include an early break, since the
   error message only ever prints three.
3. **`_git` uses `text=True`** with the locale encoding and `errors='strict'`. A
   latin-1 tag name, or `LC_ALL=C` in CI, turns any git output into a raw
   `UnicodeDecodeError` naming neither the repo nor the task. `encoding="utf-8",
   errors="replace"` is safe here — a mangled ref name is not in `ancestors` and
   gets deleted, which is the safe direction.
4. **`mkdtemp`-then-`rmtree` discards `0700`.** The clone recreates the directory
   under the umask and `os.replace` publishes *that* as the permanent cache, so
   the exposure is the whole cache, not the build window. Matters once the task
   repos are private.
5. **Two uncovered lines**, both needing injected IO failures:
   [tasks.py:1080](bakeoff/src/bakeoff/tasks.py:1080) (sweep `OSError` on a
   vanishing entry) and [tasks.py:1171](bakeoff/src/bakeoff/tasks.py:1171) (the
   publish `TaskError`).
6. **The `materialize` alternates assertion
   ([tasks.py:1228](bakeoff/src/bakeoff/tasks.py:1228)) is unanchored** —
   measured, deleting it fails no test, because reaching it needs `--dissociate`
   *and* the fast-path check to fail at once. Kept deliberately; noted so nobody
   deletes it as dead code.

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

Current state at `f67ccc2`: **536 tests**, **121 mutations** across the whole
suite (22 under `tasks:`), `tasks.py` at **92%**. A mutation reporting `STALE
ANCHOR` means a literal moved; `NO TESTS` means a selector stopped matching.
Both are hard failures, not warnings.

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

Last full verification, 2026-08-14 against `f67ccc2`:

- All five failure reproductions recover — alternates and commit-graph/`.keep` in
  a cached mirror rebuild rather than being served; `dest`-as-a-file and
  marker-as-a-directory succeed three times running; a borrowing upstream
  materializes with its history intact.
- `verify_logger.py` GATE PASSED. Preflight PASS on the real task, `start_sha`
  still the pinned `33575cc0…`.
- The real click mirror rebuilt under `_PRUNE_VERSION` 2: no alternates, no
  commit-graph, no `.keep`, the fix absent, zero leftover tmps.
- A live 4-arm run (event log `eventlog-postfix`): 4/4 records, no exclusions,
  `wire_unattributed: 0` and `isolated: true` on every arm, and all four kept run
  trees showing the fix absent, no alternates, 3,131 objects, `fsck` clean.

**What that does not establish.** Nothing here says a model can solve the task —
`outcome` never becomes `RESOLVED` at harness time by design, and the offline
grader (TASKS.md blocker 5) does not exist. And every capability figure in the
repo still carries the caveats in TASKS.md, including that **21 of 25 stored
records were taken while the reference fix was reachable in the agent's own
repository.**
