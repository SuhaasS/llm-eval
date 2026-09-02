# Round 2, item 3: a unique host path per bind-mounted tree — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Revision state:** revised after review 1 (`.superpowers/broaden/round2/plan-3-review-1.md`, 12 findings, REVISE). What changed per finding is in "Review 1 → changes" at the end.

**Goal:** Remove the *cause* of the stale bind mount. Every host path that is bind-mounted into a container gets a component no earlier container ever mounted, allocated by one shared helper, so the Docker VM has no cached inode to serve. `RunContainer._assert_repo_mounted` — the post-condition that turned this from a silent wrong measurement into a loud refusal — stays exactly as it is.

**Architecture:** One function, `bakeoff.container.fresh_tree(parent)`, returns a freshly created directory `<parent>/<uuid4().hex>` that has never existed before. The **six** call sites that today build a bind-mount source out of a *stable* key (`run_id`, `task_id`, a cell label, an artifacts root) stop `rmtree`-ing that path and re-creating it, and instead allocate under it: the stable key becomes the *parent directory*, the uuid becomes the leaf. Cleanup keeps working as it does now — `shutil.rmtree(tree, ignore_errors=True)` — but it now removes a path the allocator will never hand out again, which is what makes the cleanup safe rather than re-arming.

**Tech Stack:** Python 3.12 (the harness venv), pytest, Docker (integration legs only). All code under `bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§5.1 the container, §3.3 the self-correction loop). Grader spec: `docs/superpowers/specs/2026-08-17-offline-grader-design.md`. Round-2 context: `.superpowers/broaden/round2/CONTEXT.md` (this is item **3**); global constraints in `.superpowers/broaden/CONTEXT.md` still bind.

---

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. A comment that says what a line does rather than what breaks without it does not fit here. Claims about external behaviour carry what they were verified against.
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section for later application. One existing `CLAUDE.md` sentence is now *wrong* (it cites the preflight cache's rmtree-and-re-materialize as a defence); it is listed there, not edited here.
- **`SCHEMA_VERSION` does NOT move** (D5). No `RunRecord` field is added or changes meaning.
- **`GRADE_SCHEMA_VERSION` does NOT move** (D5). No `GradeRecord` field is added.
- **`GRADER_VERSION` bumps by ONE** from whatever value is on disk when Task 2 starts (it reads `"8"`). Justified in D6. **Its pin test moves with it** (`tests/test_grader.py:1236`).
- **`PREFLIGHT_VERSION` bumps by ONE** from whatever value is on disk when Task 3 starts (it reads `"13"`). Justified in D7. **Its pin test moves with it** (`tests/test_preflight.py:2212`). Read each constant and add one; do not hard-code a literal — other round-2 items may land first and move them.
- **`ORACLE_VERSION` does NOT move** (D6).
- **A run always produces a record.** The one new call inside `execute_run` (Task 4, site 6) sits exactly where a bare `mkdir` already sits and is discussed in D10; nothing else in this plan runs inside `execute_run` at all.
- **Backwards compatibility:** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` loads unchanged and its `start_sha` stays `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`. Nothing in this plan touches `materialize`, the manifest, the image, or any runner argv.
- **The gated argv and the graded argv stay byte-identical.**
- **Tests pin every new invariant** with a unit test in `bakeoff/tests/`; integration tests are marked `integration` (+ `task_image` where a task image is built).
- **Commit hygiene:** one commit (plan + code + docs). Subject in the repo's style (`fix:` + a sentence saying what breaks without it). Stage files explicitly (`git add <paths>`), never `git add -A` / `-a`. End the message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Do not run `scripts/mutation_check.py`** concurrently with anything else; it edits sources in place.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` → `1539 passed, 62 deselected`; `scripts/mutation_check.py` → 160/160; `scripts/verify_logger.py` → GATE PASSED.

---

## The measured defect

**Source:** `TASKS.md` P1, "A reused host run-tree path is served stale by the Docker VM's mount cache, and it reads as a passing measurement rather than as a failure" (found 2026-09-02 while gating a real node task — broadening 7, Task 9). The measurement is reproduced verbatim in `bakeoff/tests/test_integration_node_task.py::node_tree`'s docstring and in `bakeoff/src/bakeoff/container.py::RunContainer._assert_repo_mounted`.

**M1 — the same host path, rmtree'd and re-materialized between containers, is served empty every other time.** Four cycles of `rmtree` → `materialize` → new container on one path, `ls /repo` inside each container:

```
cycle 0  ls /repo -> ['README.md', 'package.json', 'src', 'tests']
cycle 1  ls /repo -> []
cycle 2  ls /repo -> ['README.md', 'package.json', 'src', 'tests']
cycle 3  ls /repo -> []
```

This is **not** the `/var/folders` trap (a path the Docker VM does not share at all, which the harness's `--basetemp` rule covers). It is a path the VM *does* share, whose inode the host replaced underneath the VM's cache.

**M2 — an empty mount resolves rather than failing, in both directions that matter.** Under vitest and jest an empty `/repo` answers `No test files found` at exit **1**, which is the exit a genuinely failing suite gives — so a node gate does not go flaky, it **passes while measuring nothing**. On the offline grader the same emptiness makes `git apply` fail, which is `APPLY_FAILED` → `resolved: False` — an accusation that a model's patch did not work, written permanently into an append-only line, over an environment difference the model never saw. (True of the grades already on disk; see D6 for what the `_assert_repo_mounted` guard changed about this.)

**M3 — the post-condition already landed; the cause did not.** `RunContainer._assert_repo_mounted` (broadening 7, Task 10) execs `find /repo -mindepth 1 -maxdepth 1 -print -quit` at the end of `__enter__`, conditioned on the host side being non-empty, and raises `ContainerError` after force-removing the container. Pinned by `tests/test_container.py::test_a_bind_mount_that_did_not_land_is_refused_and_the_container_removed`, `::test_a_bind_mount_that_landed_is_not_refused`, `::test_an_empty_host_directory_is_not_a_mount_failure`. It converts a silent wrong measurement into a loud refusal **and no further**: every second `--preflight-only --force-preflight` invocation on one task still dies instead of gating. And it covers `REPO_MOUNT` and only `REPO_MOUNT` (`container.py:192-213`), which is why site 6 below is not covered by anything.

**M4 — the sites, read out of the current tree (line numbers at HEAD 8232032):**

| # | site | path today | reused when | caught by `_assert_repo_mounted`? |
|---|---|---|---|---|
| 1 | `grader.grade_run` (`grader.py:1994-1995`) | `<cache>/grade-tree/<run_id>` | a second grade pass, or a second event log — `run_id` is unique *within* a collection, not across one (5 of 7 stored `run_id`s appear in more than one log) | yes |
| 2 | `oracle._derive` (`oracle.py:282-283`) | `<cache>/oracle-tree/<task_id>` | any second derivation for that task (a `--re-grade`, an `ORACLE_VERSION` bump, a second log) | yes |
| 3 | `grade.resolve_task` (`scripts/grade.py:294-295`) | `<cache>/grade-preflight-tree/<task_id>` | every second `grade.py` invocation | yes |
| 4 | `run_matrix.resolve_tasks` (`scripts/run_matrix.py:262-263`) | `<cache>/preflight-tree/<task_id>` | **every second `run_matrix.py` invocation** — the one the probe measured, because the gate loop is `--preflight-only --force-preflight` run repeatedly on one task | yes |
| 5 | `run_matrix.run_cell` (`scripts/run_matrix.py:326`) | `<artifacts>/<stamp>/<cell>/repo` | two invocations sharing a `stamp` (`date -u +%Y%m%dT%H%M%SZ`, one-second granularity); a run-level retry (`TASKS.md` P1) | yes |
| 6 | `runner.execute_run` (`runner.py:1047-1053`, mounted at `1105` / `1130-1135`) | `<artifacts_root>/claude-config` → `CONTAINER_CONFIG_DIR` | **the rmtree is already in the code** (`runner.py:1048`), so any second `execute_run` under one `artifacts_root` — which is what a run-level retry is | **no** |

Site 5's exposure today is a **loud** one — `materialize` refuses an existing destination, so a same-second collision is a `TaskError` and a STRANDED cell, not a stale mount.

**Site 6 is the one with no backstop at all**, and it is worse than site 5 on every axis: the `rmtree` → `mkdir` → mount sequence is already written (site 5 carries a deliberate "No rmtree" comment), `_assert_repo_mounted` checks `REPO_MOUNT` and not `CONTAINER_CONFIG_DIR`, and the failure is not loud. The agent writes its transcript into the cached inode, the host directory stays empty, transcript discovery globs it and finds nothing, and the run parses to **zero turns, zero tokens and zero cost** — with the tokens already spent. `trajectory_parse_error` is non-empty in that case ("Absence is recorded, never implied"), so it is not silent; it is a lost cell, which is the cost site 5 is in scope to avoid.

The **other** `extra_mounts` entry, `settings_host_path` (`REPO/config/eval_settings.json`), is **not** at risk: it is a file in the working tree that nothing removes, so no inode is replaced under the VM's cache. Same for `proxy.py:115` and `proxy.py:143`, which mount `repo_root/fixtures`, `repo_root/src`, `repo_root/config` (stable, never removed) and `CACHE/wire/<stamp>` (created with `mkdir(exist_ok=True)`, never `rmtree`'d).

**M5 — site 4 leaves its tree behind on purpose today, and that is the setup for the next invocation's stale mount.** `resolve_tasks` stores `resolved[task_id]["repo"] = work / "repo"` (lines **274** and **294**) and never removes the tree; the removal happens at the *top* of the next invocation. Checked across `scripts/`, `src/` and `tests/`: **nothing ever reads `resolved[...]["repo"]`** — the only `resolved[...]` readers are `run_matrix.py:338/341/358` (`start_sha`, `image`) and an unrelated dict in `codex_judge.py`. The key is dead, and it is the only thing that ever made the tree look like it had to outlive `resolve_tasks`.

---

## Design decisions, settled

### D1. One helper, `fresh_tree(parent)`, in `bakeoff/src/bakeoff/container.py`

```python
STALE_TREE_AGE_S = 86_400


def fresh_tree(parent: Path) -> Path:
    """A host directory under `parent` that NO container has ever mounted.

    The Docker VM caches the directory it serves for a bind-mount source. A
    host path that is `rmtree`d and re-created underneath that cache is served
    from it and arrives EMPTY inside the container -- measured 2026-09-02,
    four cycles on one path gave `[files], [], [files], []`. An empty mount
    then RESOLVES: an empty `/repo` is `No test files found` at exit 1 under
    vitest and jest, the same exit a red suite gives, and `git apply` fails,
    which the grader reads as APPLY_FAILED -> `resolved: False`; an empty
    CLAUDE_CONFIG_DIR is a run that parses to zero turns, zero tokens and zero
    cost with the tokens already spent. So the failure is not flakiness, it is
    a passing measurement of nothing, an accusation against a model, or a lost
    cell. `RunContainer._assert_repo_mounted` is the post-condition for the
    `/repo` case; this is the cause, for all of them.

    THE RULE THIS FUNCTION EXISTS TO KEEP: a host path that has been
    bind-mounted once is never bind-mounted again. Callers may `rmtree` a tree
    this returned -- that is safe precisely because the allocator will not hand
    the same name out a second time -- but they may never write into a path
    they removed.

    The stable key (`run_id`, `task_id`, the cell label, the artifacts root)
    stays in the path as the PARENT, so an operator can still find a tree by
    grepping the cache layout; only the leaf is unique. The key-level directory
    survives cleanup as an empty husk -- see `_sweep_stale_trees` for what that
    costs and what is and is not collected.

    `uuid4`, not `tempfile.mkdtemp`: mkdtemp hard-codes mode 0o700, and the
    container runs as uid 1000 while the host directory is owned by the
    operator, so on a Linux host (no ownership remapping) the mount would be
    unreadable to the agent. `materialize` creates its trees with the default
    mode and this stays beside it.
    """
```

Body:

```python
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_trees(parent)
    tree = parent / uuid.uuid4().hex
    tree.mkdir()
    return tree
```

`tree.mkdir()` with the default `exist_ok=False`: a collision here is a bug and must be loud, never papered over by reusing the directory — reusing it is the whole defect.

**Where it lives.** `container.py`, beside `_assert_repo_mounted`, so the cause and the post-condition that catches its absence are in one file and one body of prose. Rejected: `tasks.py` beside `materialize` (the invariant is about *bind mounts*, not materialization — site 6 mounts a directory `materialize` never touches); a new `trees.py` module (one function, no state).

**Import cost, and why the two scripts import it at module scope.** `container.py` imports `docker` at module scope and **does not import litellm**. All six call sites already have `bakeoff.container` in their import closure: `runner`, `grader` and `oracle` import `RunContainer` directly; `scripts/grade.py` and `scripts/run_matrix.py` (`run_matrix.py:61`) import `bakeoff.preflight`, and `preflight.py:38` imports `bakeoff.container`. This matters because `run_matrix` deliberately defers `bakeoff.runner`, `bakeoff.claude_runner` and `bakeoff.proxy_callback` to *inside* `run_cell` (`run_matrix.py:320-323`) to keep `--preflight-only` off litellm. **Import `fresh_tree` at module scope in both scripts** — it pulls no litellm — and do not move it inside a function "to be safe": that changes the indentation the mutation anchors match.

### D2. uuid4, not a stamp plus a counter

A stamp+counter is readable and ordered, and it is **wrong for this defect**. The reuse that bites is *across processes*: `run_matrix.py` invoked twice on one task, `grade.py` invoked twice on one cache. A counter restarts at `0` in a new process, so `…-0` is precisely a path the previous process already mounted — the counter re-arms the defect on the first re-invocation, which is the exact case measured in M1. Making the stamp unique per process is possible, but a per-process unique token *is* a uuid, only with a clock dependency and a one-second collision window (`run_matrix`'s stamp already has one, M4 site 5). Readability is preserved by keeping the human key as the parent directory (D1).

Also rejected: `tempfile.mkdtemp(dir=parent)` — see the 0700 argument in D1's docstring, pinned by a test in Task 1. It is what `grader._ContainerEnv.scan_secrets` uses for the gitleaks holder (`grader.py:1696`), and that is correct *there* because gitleaks runs as root in its own container; it is not correct for a directory the agent must read and write as uid 1000.

### D3. Cleanup rules that do not re-arm the defect

The rule, stated once and repeated in `fresh_tree`'s docstring: **only ever `rmtree` a path the allocator will not return again.** Concretely:

- Every existing `shutil.rmtree(tree, ignore_errors=True)` in a `finally` **stays**. It now removes a uuid leaf.
- Every `shutil.rmtree(...)` that runs **before** a write into the same path is **deleted**. There are five (`grader.py:1995`, `oracle.py:283`, `scripts/grade.py:295`, `scripts/run_matrix.py:263`, `runner.py:1048`) and each one is the defect written out.
- Site 4 gains the `finally` it never had (M5), matching `grade.resolve_task`, and drops the dead `"repo"` key.
- Site 6 has no cleanup at all today and gains none: the config directory holds the transcript, which is what `artifacts.*` points at after the run.
- The key-level parent is **not** removed. Rejected: `rmtree(tree)` followed by `parent.rmdir()` — it races a concurrent allocator between its `mkdir(parents=True, exist_ok=True)` and its leaf `mkdir`, making the leaf `mkdir` raise `FileNotFoundError` in an allocator whose whole job is to not fail sideways, and `run_matrix` and `grade.py` are documented as running against one cache concurrently. Buying that back needs a retry loop around an inode.

### D4. Disk growth: what the sweep bounds, and what it does not

- **Live trees.** `grade.py` over N records and T tasks holds at most **three** trees at once (one grade tree, one grade-preflight tree, one oracle tree), each removed in a `finally`. `run_matrix` holds one preflight tree per task inside `resolve_tasks` and one run tree per cell inside `run_cell`. Unchanged from today.
- **Husks.** A leaf survives only when the process dies between `fresh_tree` and its cleanup (SIGKILL, power loss), or — for site 5 — when the cell goes STRANDED (D5). Before this change the *next* invocation deleted it, which is the defect; now it can accumulate.
- **`_sweep_stale_trees(parent)` is called with the KEY directory as `parent`, so it only ever collects a husk under a key that is allocated under again.** That is honest for two of the six parents and not for the rest:
  - `preflight-tree/<task_id>` and `grade-preflight-tree/<task_id>` — revisited on **every** invocation, which is exactly where husks would otherwise accumulate per invocation. The bound holds: one day of crashes.
  - `grade-tree/<run_id>`, `oracle-tree/<task_id>` (revisited only on a re-derivation), `<artifacts>/<stamp>/<cell>/tree` and `<artifacts_root>/claude-config` — a husk under a key that is never allocated under again **persists until the operator's `rm -rf`**. For those, growth is bounded by *work done and crashed*, not by invocations, which is the acceptable shape: one orphan per killed process, never one per run.
  - The empty key directories themselves are **never** collected. `grade-tree/` grows one empty inode per distinct `run_id` per cache root — for an 80 × 4 × 3 matrix that is ~960 empty directories. Small, but it is not "one inode per task", and a reader should not be told it is.
- **Rejected: sweeping the key level too** (`_sweep_stale_trees(parent.parent)`). It would collect both classes above, and it reintroduces exactly the race D3 and the Q4 ruling reject: `rmtree`-ing a key directory that a concurrent process has just passed through and is about to allocate a leaf under makes that leaf `mkdir` raise `FileNotFoundError`. **The age guard does not close that window**, and this is the mechanism, checked rather than assumed: `fresh_tree` opens with `parent.mkdir(parents=True, exist_ok=True)`, which on an *existing* key directory is a no-op that does **not** refresh its mtime — so a key older than `STALE_TREE_AGE_S` stays sweepable in exactly the moment a live process is allocating under it, because the age being tested is the key's and not the leaf's. The sweep stays at the leaf level, where the leaf's own mtime is its allocation time and age is therefore a real guard.
- The sweep is **age**-based, never "delete every sibling": `run_matrix` and `grade.py` run against one cache at the same time on purpose, and deleting a live tree out from under another process is worse than leaking an inode. The margin is ~1000×: a graded record's tree lives minutes, an eval cell's is capped by `wall_clock_timeout_s`.
- The sweep never raises — a sweep failure must not cost the run it is allocating for, the same rule `HostSampler.start()` and `checkpoints.maybe_capture` keep.
- The sweep is never pointed at the pruned-mirror cache (`HANDOFF.md`): its callers pass `grade-tree/…`, `oracle-tree/…`, `grade-preflight-tree/…`, `preflight-tree/…`, a per-cell `tree/` and a per-run `claude-config/`, and no mirror path is a parent of any of them.
- Operator remedy, unchanged and still safe when nothing is running: `rm -rf ~/.cache/bakeoff/{grade-tree,oracle-tree,grade-preflight-tree,preflight-tree}`.

### D5. No record field, no grade field; `SCHEMA_VERSION` and `GRADE_SCHEMA_VERSION` do not move

Considered and rejected: `RunRecord.artifacts.run_tree` (and a `GradeRecord` twin) naming the host tree.

- **Two absences would need two fields.** "The tree was discarded" and "no tree was ever allocated" render identically, and "a null says which kind of null it is" forbids that. Two schema fields, both null in production, to name a directory that no longer exists, is not a trade this record should make.
- **The failure path already names the tree.** `_assert_repo_mounted`'s `ContainerError` interpolates `self.repo_path`, and that message reaches `crash_error` on a run and the grader's per-record `errors` bucket.
- **Not adding a field does not violate "absence is recorded, never implied."** That invariant governs fields the record *has*; a record has never claimed anything about a host tree, so declining to add one implies nothing.
- **The tree does not always die with the run, and the plan should not pretend otherwise.** Removal in `run_cell` is **not** in a `finally` (`run_matrix.py:383-384`): a raise from `effective_config`, `write_json` or `unattributed_count` between the run and the removal sends the cell to STRANDED (`run_matrix.py:635-645`) with the tree still on disk — alongside a record in the log or a `record.unwritten.json` beside it. That, and `--keep`, are the two survival cases, and they are the two where a reader most wants the path.
- **So the kept path is recorded where it is true, and nowhere else:** `run_cell` returns it when `args.keep` is set, `main` writes it into the `matrix-<stamp>.json` row under a `"kept_repo"` key **present only then**, and the cell also prints `  kept <path>`. Present-or-absent is the same discipline `artifacts.wire_log_gz` uses one layer down; a key written only when a tree was kept is not a null. The earlier draft's "print only" remedy was the shape `CLAUDE.md` names as "a log nobody keeps" and is rejected. The STRANDED case is left to the stranded line's own message plus the operator's `rm -rf`; adding a survivor key there would mean writing a path from an exception handler that does not know whether the removal ran.

### D6. `GRADER_VERSION` bumps; `ORACLE_VERSION` does not

`GRADER_VERSION` moves 8 → 9 because **grades already on disk may be wrong in the accusatory direction.** A grade pass that ran over a second event log, or a second pass over one log, mounted `grade-tree/<run_id>` for a `run_id` an earlier pass had already mounted — M1's every-other-time emptiness, M2's `APPLY_FAILED` → `resolved: False`.

**The poisoned window is closed, and the bump is what re-opens the lines inside it.** At HEAD, a grade pass that hits the stale mount raises `ContainerError` from `RunContainer.__enter__` *before* `_refresh_index` or `run_ladder`, and `scripts/grade.py` contains that per record into the `errors` bucket — it does **not** produce `resolved: False` today. The suspect lines are the grades written **before `_assert_repo_mounted` landed** (broadening 7, Task 10). `scripts/grade.py`'s resume key is `(run_id, GRADER_VERSION)` alone, so without the bump those lines are never revisited; with it, a re-grade writes a **new line whose disagreement with the old one is the finding**, which is exactly what the version constant is for. An operator cannot distinguish a stale-mount `APPLY_FAILED` from a real one by reading either line, so "document it and tell operators to pass `--re-grade`" is a manual step for a defect nobody knows they hit.

`ORACLE_VERSION` does **not** move. A derivation over an empty tree cannot cache a wrong verdict: `_derive` writes `.bakeoff-solution.patch` from the host (which is not empty) and `container.exec(["git", "apply", …])` then fails against the empty view (`oracle.py:286-311`), so `OracleError("the reference fix does not apply…")` is raised before any verdict exists and `ensure_oracle` caches nothing.

### D7. `PREFLIGHT_VERSION` bumps

Preflight verdicts are cached in **two** files, both keyed through the shared `preflight.preflight_cache_key` (`preflight.py:199`) as `(manifest digest, image id, start_sha, PREFLIGHT_VERSION)`:

- `<cache>/preflight.json` — `run_matrix`'s (`run_matrix.py:239`; `grade.py:137`, `DRIVER_PREFLIGHT_CACHE`),
- `<cache>/preflight-grade.json` — `grade.py`'s (`grade.py:139`, `GRADE_PREFLIGHT_CACHE`).

The bump invalidates **both**, so a `grade.py` invocation pays the re-gate as well as a `run_matrix` one — one offline re-gate per task per driver, no credentials, no spend.

The tree is not in that key and could not be: the verdict outlives the artifact it describes. A PASS recorded before this fix was observed through a container this code cannot now interrogate, and on a vitest task an empty `/repo` is `No test files found` at exit 1 — the shape M2 says a PASS cannot be told apart from. The only lever that retires a suspect verdict is the version.

The counter-argument is recorded so the next reader does not re-litigate it: `PREFLIGHT_VERSION` is documented as moving "for any change to what preflight asserts", and this change alters nothing preflight asserts. The alternative — leaving it and relying on `--force-preflight` — is an opt-in flag for a defect the operator has no signal for. Review 1 ruled: bump, with the precedent that `docs/superpowers/plans/2026-09-01-broaden-4-suite-timeout.md` bumps all three constants for a manifest key `manifest_digest` could not see, on the same "a warm cache would otherwise serve a verdict computed under a bound the manifest no longer asks for" reasoning.

### D8. `_assert_repo_mounted` stays, unchanged in behaviour

The cause is fixed; the guard remains. Three reasons, and each is a thing the guard still catches that `fresh_tree` does not:

1. The **other** empty-mount shape — a path the Docker VM does not share at all (`/var/folders` on macOS) — is untouched by this plan and is a `--basetemp` convention, not a code guarantee.
2. A **new call site** that builds a path by hand instead of calling `fresh_tree`. The guard is the only thing standing between that and M2.
3. Anything else that makes a mount not land (a daemon fault, a removed host directory). The guard's claim is about the mount, not about the path allocation.

Its docstring changes only where it states the present tense of the defect ("Four production call sites reuse a path that way") — see Task 6. Its three unit tests are untouched. It does **not** grow to cover site 6: the guard's conditioning ("an empty answer is evidence only when there was something to see") is inverted there — a freshly allocated config dir is *supposed* to be empty on the host at mount time, so there is nothing for a mount-time check to compare.

**So after this change site 6 has NO mount post-condition, and that is accepted rather than hidden.** The emptiness read-back at `runner.py:1057-1066` is not one and must not be described as one: it reads the **host** side, which after `fresh_tree` is empty by construction, so it can never observe a stale mount. It is a post-condition on the *allocator* — Task 4's wording — and nothing else. The only downstream signal that a stale config mount happened would be `trajectory_parse_error` on a run with zero turns, after the tokens are spent. What keeps the allocation itself from being reverted is the site-6 mutation anchor in Task 5; that anchor is doing the work a guard would otherwise do, which is why it is not optional.

### D9. Scope: paths that are NOT bind-mounted change nothing

The stale VM mount cache exists only on the bind-mount path; these are read and written on the host and are explicitly out of scope.

- `grader.grade_run`'s `artifacts_root/<run_id>/v<GRADER_VERSION>` — wiped and rewritten by the harness, never mounted. Its per-version-and-emptied-first design is correct for its own defect and is not this one.
- `run_matrix.run_cell`'s `run_root/artifacts` **as a directory** — never mounted; its per-invocation stamped root is the fix for the artifacts-collision defect and stays as it is. **Its `claude-config` child IS mounted and is site 6**, fixed in Task 4 — the pre-review draft claimed this whole subtree was never mounted and was wrong.
- The wire directory `CACHE/wire/<stamp>`, the event log, `cache/preflight/<task_id>.json`, `cache/build`.
- `grader._ContainerEnv.scan_secrets`'s gitleaks holder — *is* mounted, and is already unique per invocation (`tempfile.mkdtemp(prefix="scan-", dir=self.scan_root)`, `grader.py:1696`). Unchanged.
- `proxy.py:115` and `proxy.py:143` — *are* mounts, of `repo_root/{fixtures,src,config}` (stable, never removed) and `CACHE/wire/<stamp>` (`mkdir(exist_ok=True)`, never `rmtree`'d). No inode is replaced under any of them.
- `scripts/smoke_test.py` (`~/.cache/bakeoff-smoke/<stamp>/<arm>-<repeat>/repo`) and `scripts/dry_run.py` (`tempfile.mkdtemp(prefix="bakeoff-dry-run-", dir=$HOME)`) — per-invocation, and a same-second collision in smoke_test fails **loudly** at `shutil.copytree` (destination exists). Left alone (Q5 ruling). Their `claude-config` mounts go through `execute_run` and are covered by site 6.
- `preflight.preflight` takes `repo_path` from its caller and starts one container over it (`preflight.py:959`). It allocates nothing and needs no change; both of its callers (sites 3 and 4) are fixed here.

`grep -rn "RunContainer(" src/ scripts/` gives exactly four constructions (`runner.py:1130`, `grader.py:1998`, `oracle.py:287`, `preflight.py:959`); `grep -rn "volumes\|Mount("` adds only the two `proxy.py` lines. The six sites are the six sites.

### D10. Site 6's allocation sits where a bare `mkdir` already sits, and that is not a new hazard

`runner.py:1047` is inside `execute_run` but **outside every `try`**, and the comment at `1049-1052` records what that cost once: the `rmtree` at 1048 cannot fail loudly (`ignore_errors=True`), so the bare `mkdir` raised `FileExistsError` on exactly that case and "the run died with no record at all" — which is why `exist_ok=True` is there.

Replacing both lines with `fresh_tree(...)` **removes that failure mode outright**: nothing is removed, so no silent removal failure can exist, and the leaf is a name no directory has ever had, so `exist_ok=False` cannot collide. What remains is the same class of exposure the bare `mkdir` has today — an `OSError` from the two `mkdir`s (a full disk, a permission change) escaping `execute_run` before the run body starts. `_sweep_stale_trees` never raises, so it adds nothing. This plan does **not** wrap that region: containing it needs a `finalize_error`-style channel for the pre-container phase, which is its own change, and the pre-existing hazard is neither introduced nor widened here. The `exist_ok` comment is rewritten (Task 4) to say what is now true instead of being carried forward describing code that is gone.

---

## File Structure

```
bakeoff/src/bakeoff/container.py             Task 1  + STALE_TREE_AGE_S, fresh_tree, _sweep_stale_trees; Task 6 docstring
bakeoff/tests/test_container.py              Task 1  6 new tests
bakeoff/src/bakeoff/grader.py                Task 2  grade_run allocates; GRADER_VERSION 8 -> 9
bakeoff/src/bakeoff/oracle.py                Task 2  _derive allocates
bakeoff/tests/test_grader.py                 Task 2  1 new test; the GRADER_VERSION pin at :1236
bakeoff/tests/test_oracle.py                 Task 2  1 new test, 2 assertions updated
bakeoff/scripts/grade.py                     Task 3  resolve_task allocates
bakeoff/scripts/run_matrix.py                Task 3  resolve_tasks allocates + finally; run_cell allocates; --keep row + print
bakeoff/src/bakeoff/preflight.py             Task 3  PREFLIGHT_VERSION 13 -> 14 (constant only)
bakeoff/tests/test_grade_script.py           Task 3  1 new test
bakeoff/tests/test_run_matrix.py             Task 3  3 new tests
bakeoff/tests/test_preflight.py              Task 3  the PREFLIGHT_VERSION pin at :2212
bakeoff/src/bakeoff/runner.py                Task 4  site 6: the claude-config mount
bakeoff/tests/test_fault_injection.py        Task 4  1 new test
bakeoff/tests/test_integration_node_task.py  Task 5  the private uuid workaround -> fresh_tree
bakeoff/scripts/mutation_check.py            Task 5  4 new anchors
tasks/todo.md                                Task 6  review section
TASKS.md                                     Task 6  item closed
```

**Test arithmetic**, so Verification has a number that is not wrong: **13 new named tests** (6 + 1 + 1 + 1 + 3 + 1), of which **two are parametrized ×2** (`test_a_sweep_that_fails_does_not_cost_the_allocation`, `test_the_preflight_tree_does_not_outlive_the_resolution`), so pytest collects **15** new items. Four existing tests change without changing the count: two assertions (`test_oracle.py:440`, `:451`) and two version literals (`test_grader.py:1236`, `test_preflight.py:2212`).

---

## Task 1: `fresh_tree`, and the tests that pin what it guarantees

- [ ] Add `import shutil` and `import uuid` to `bakeoff/src/bakeoff/container.py`'s import block (it has `os`, `threading`, `time`), keeping it alphabetical: `os`, `shutil`, `threading`, `time`, `uuid`. Add `from pathlib import Path` — the module has **no** pathlib import today.
- [ ] Add `STALE_TREE_AGE_S = 86_400` beside `REPO_MOUNT`.
- [ ] Add `fresh_tree(parent: Path) -> Path` with the docstring and body from D1, at module level, above `class RunContainer`.
- [ ] Add `_sweep_stale_trees(parent: Path, max_age_s: float = STALE_TREE_AGE_S) -> None`:

```python
def _sweep_stale_trees(parent: Path, max_age_s: float = STALE_TREE_AGE_S) -> None:
    """Remove leaves under `parent` that no live process can still be using.

    Husks are the price of `fresh_tree`'s rule: a leaf is removed by the
    process that allocated it, so a process killed before that leaves one
    behind, and nothing deletes it later -- deleting it later, BY NAME, is the
    defect this whole mechanism removes.

    AGE-BASED, never "every sibling". `run_matrix` and `grade.py` run against
    one cache at the same time on purpose, and deleting a tree out from under
    a live container is worse than leaking an inode. The leaf's own `st_mtime`
    is effectively its ALLOCATION time -- everything a run writes goes into a
    child of it and never touches the leaf's own mtime -- so a 24 h bound is
    ~1000x the longest legitimate hold (a graded record's tree lives minutes,
    an eval cell's is capped by `wall_clock_timeout_s`) rather than a bet on
    what a long run does to its directory.

    LEAF LEVEL ONLY, so this collects a husk only under a key that is
    allocated under again -- true of `preflight-tree/<task_id>` and
    `grade-preflight-tree/<task_id>` on every invocation, and not true of a
    `grade-tree/<run_id>` or a per-cell tree. Sweeping the key level too would
    collect those, and would `rmtree` a directory a concurrent allocator has
    just created and is about to put a leaf under, whose `mkdir` then raises
    `FileNotFoundError`.

    Never raises. A sweep failure must not cost the run it is allocating for
    -- the rule `HostSampler.start` and `checkpoints.maybe_capture` keep.
    """
    cutoff = time.time() - max_age_s
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue
```

- [ ] Tests in `bakeoff/tests/test_container.py`, under a new section comment `# --- fresh_tree: a host path no container has mounted before ---`. None needs Docker; all must run under the default (non-integration) selection.

  **The import block needs four additions, not one.** It reads, in full, `import threading`, `import time`, `import pytest`, `from bakeoff.container import ContainerError, HostSampler, RunContainer`. The six new tests need:

```python
import os        # os.umask, os.utime
import shutil    # test_a_removed_tree_is_never_handed_out_again
import stat      # stat.S_IMODE
import bakeoff.container   # the module object, for the two sweep-failure monkeypatches

from bakeoff.container import (
    ContainerError, HostSampler, RunContainer, STALE_TREE_AGE_S, fresh_tree,
)
```

  - [ ] `test_two_allocations_under_one_parent_are_different_paths(tmp_path)` — `fresh_tree(tmp_path / "grade-tree" / "run-a")` twice; asserts the two paths differ, both exist, both are empty directories, and both have the same parent (the stable key stays greppable).
  - [ ] `test_an_allocated_tree_is_empty_even_when_a_sibling_holds_content(tmp_path)` — write a file into the first allocation, allocate again, assert the second is empty and the first is untouched. This is the property `materialize` depends on (it refuses an existing `dest` and does `dest.parent.mkdir(parents=True, exist_ok=True)`, `tasks.py:2506-2511`, so allocating the leaf before `materialize(..., leaf / "repo", ...)` is correct).
  - [ ] `test_a_removed_tree_is_never_handed_out_again(tmp_path)` — allocate, `shutil.rmtree` it (the cleanup every call site does), then allocate 50 more times; assert the removed name appears in none of them. Pins D3's rule at the allocator: removing a tree is safe *because* the name does not come back.
  - [ ] `test_a_sibling_older_than_a_day_is_swept_and_a_fresh_one_is_not(tmp_path)` — create two sibling directories by hand under one key, `os.utime` one to `(time.time() - STALE_TREE_AGE_S - 60,) * 2`, leave the other at now; allocate; assert the old one is gone, the recent one survives, and the new allocation exists. The recent-sibling half is the load-bearing one: it is what says a concurrent process's live tree is not deleted. **This is the only test pinning the sweep at all; it must not be dropped.**
  - [ ] `test_a_sweep_that_fails_does_not_cost_the_allocation(tmp_path, monkeypatch)` — parametrized ×2 over which call raises: `monkeypatch.setattr(bakeoff.container.os, "scandir", …)` raising `OSError`, and `monkeypatch.setattr(bakeoff.container.shutil, "rmtree", …)` raising `OSError` (with a stale sibling present so it is reached). Both assert `fresh_tree` still returns a fresh, existing, empty directory.
  - [ ] `test_an_allocated_tree_is_not_mkdtemps_owner_only_mode(tmp_path)` — the assertion is **relative to the process umask**, which is honest at every umask and red under `mkdtemp`'s hard-coded `0o700` at every umask:

```python
    old = os.umask(0o022)
    os.umask(old)
    tree = fresh_tree(tmp_path / "grade-tree" / "run-a")
    assert stat.S_IMODE(tree.stat().st_mode) == 0o777 & ~old
```

    Docstring: `tempfile.mkdtemp` hard-codes `0o700`; the container runs as uid 1000 and the host directory is owned by the operator, so on a Linux host that mode makes the mount unreadable to the agent. This is the test behind D2's `mkdtemp` rejection — the one design decision here a future refactor is most likely to undo, since the neighbouring `scan_secrets` uses `mkdtemp` (`grader.py:1696`). Do **not** delete or weaken it.

- [ ] The three existing `_assert_repo_mounted` tests are **not touched** (D8).

---

## Task 2: the two library call sites (`grader.grade_run`, `oracle._derive`)

- [ ] `bakeoff/src/bakeoff/grader.py:79` — the existing line is `from bakeoff.container import ContainerError, RunContainer`. It becomes, verbatim:

```python
from bakeoff.container import ContainerError, RunContainer, fresh_tree
```

  (`ContainerError` is used in this module; dropping it breaks the import.)

- [ ] Replace `grader.py:1994-1995`:

```python
    tree = Path(cache_root) / "grade-tree" / record.run_id
    shutil.rmtree(tree, ignore_errors=True)
```

with

```python
    # A path no container has mounted, never `run_id` alone: `run_id` is
    # unique within a collection and not across one, so a second pass mounts a
    # path an earlier one already did -- and the Docker VM then serves the
    # stale, EMPTY directory it cached (measured 2026-09-02, `[files], [],
    # [files], []` over four cycles on one path). `git apply` fails against
    # that, which is APPLY_FAILED -> `resolved: False`: an accusation that the
    # model's patch did not work, over an environment difference it never saw.
    tree = fresh_tree(Path(cache_root) / "grade-tree" / record.run_id)
```

- [ ] The `finally: shutil.rmtree(tree, ignore_errors=True)` at `grader.py:2013` **stays** — it now removes a leaf whose name will not be reissued. Extend the `grade_run` docstring's "The tree is removed on the way out" paragraph with that clause.
- [ ] Bump `GRADER_VERSION` (`grader.py:239`) by one from the value on disk (`"8"` → `"9"`), with a comment carrying D6: not a change to what any check asserts; it retires grades that may carry an `APPLY_FAILED` produced by a stale bind mount before `_assert_repo_mounted` landed, and `scripts/grade.py`'s resume key is `(run_id, GRADER_VERSION)` alone.
- [ ] **`bakeoff/tests/test_grader.py:1236`**, inside `test_the_grader_version_moved_with_what_check_5_means`: change `assert GRADER_VERSION == "8"` to `"9"` and append to the docstring's enumeration (that docstring is the repo's changelog for this constant — do not invent wording, use this):

  > `8 -> 9` is not a change to what any check asserts. It retires grades that may carry an `APPLY_FAILED` produced by a stale bind mount rather than by the submission (round 2 item 3, 2026-09-03): `grade-tree/<run_id>` was reused across passes, the Docker VM served the cached empty directory, and `scripts/grade.py`'s resume key is `(run_id, GRADER_VERSION)` alone, so without the bump those lines are never revisited.

- [ ] `bakeoff/src/bakeoff/oracle.py:67` — `from bakeoff.container import RunContainer` becomes `from bakeoff.container import RunContainer, fresh_tree`. Replace `oracle.py:282-283` the same way as the grader's, with a comment naming *its* failure mode (an empty tree makes the reference fix fail to apply, so the task becomes ungradable — loud, but for the wrong reason). Keep the `finally` at `oracle.py:351`.
- [ ] `ORACLE_VERSION` does **not** move (D6).

Tests — **self-contained fakes, written out here**, because `_grade_capturing` (`test_grader.py:2145`) defines `FakeContainer` *inside* itself, records no `dest`, hard-codes `tmp_path / "cache"` and calls `grade_run` once:

- [ ] `bakeoff/tests/test_grader.py::test_two_grades_of_one_run_mount_different_host_paths(monkeypatch, tmp_path)`:

```python
def test_two_grades_of_one_run_mount_different_host_paths(monkeypatch, tmp_path):
    """A re-grade -- or a second event log carrying the same `run_id`, which
    is 5 of the 7 stored ones -- used to mount `grade-tree/<run_id>` a second
    time. The Docker VM serves that from its cache, EMPTY (measured
    2026-09-02), and an empty tree makes `git apply` fail: APPLY_FAILED ->
    `resolved: False`, against a submission the model really produced."""
    import bakeoff.grader as grader

    mounted: list[str] = []

    class FakeContainer:
        def __init__(self, **kw):
            mounted.append(kw["repo_path"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec(self, argv, env=None):
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(grader, "materialize", lambda *a, **kw: START_SHA)
    monkeypatch.setattr(grader, "RunContainer", FakeContainer)
    monkeypatch.setattr(grader, "run_ladder",
                        lambda *a, **kw: _ladder_result())

    record = _record()
    cache = tmp_path / "cache"
    for _ in range(2):
        grade_run(record, _task(), "sha256:image", _oracle(),
                  cache, tmp_path / "art")

    assert len(mounted) == 2
    assert mounted[0] != mounted[1]
    assert all(p.endswith("/repo") for p in mounted)
    # The stable key stays in the path, so a tree is still findable by run_id.
    assert all(record.run_id in p for p in mounted)
    # And the `finally` still fires: no leaf outlives its grade.
    assert not list((cache / "grade-tree" / record.run_id).iterdir())
```

  `START_SHA`, `_record`, `_task`, `_oracle` and `_ladder_result` are the module's existing helpers. `RunContainer` is constructed with keyword arguments at `grader.py:1998`, so `kw["repo_path"]` is safe.

- [ ] `bakeoff/tests/test_oracle.py::test_two_derivations_for_one_task_mount_different_host_paths(tmp_path, monkeypatch)` — `_stub_derive_environment`'s container fake is `lambda image, repo_path, base_sha: _FakeContainer(existing)`. Record the path by re-stubbing after the helper runs:

```python
def test_two_derivations_for_one_task_mount_different_host_paths(
    tmp_path, monkeypatch
):
    """`oracle-tree/<task_id>` is one path per task, and a re-derivation
    mounts it again -- the second container then sees the VM's cached, empty
    copy and the reference fix "does not apply", which makes a sound task
    ungradable."""
    mounted: list[str] = []

    def _recording(image, repo_path, base_sha):
        mounted.append(str(repo_path))
        return _FakeContainer({"tests/"})

    for _ in range(2):
        _stub_derive_environment(
            monkeypatch, tmp_path, existing={"tests/"},
            results=[(0, ""), (0, "")],
        )
        monkeypatch.setattr("bakeoff.oracle.RunContainer", _recording)
        _derive(_derive_task(paths=("tests/",)), "sha256:img", tmp_path)

    assert mounted[0] != mounted[1]
    assert all("click-3360" in p for p in mounted)
```

- [ ] **Update two existing assertions** in `test_oracle.py` (lines **440** and **451**). `assert not (tmp_path / "oracle-tree" / "click-3360").exists()` becomes:

```python
    # The KEY directory survives as an empty husk by design (one inode per
    # task, never per derivation); what must not survive is a tree holding the
    # reference fix, which is the leaf.
    assert not list((tmp_path / "oracle-tree" / "click-3360").iterdir())
```

  in `test_the_derivation_tree_does_not_outlive_the_derivation` and `test_the_tree_is_removed_even_when_the_derivation_refuses`. Their names and intent are unchanged, and both stay red if the `finally` at `oracle.py:351` is deleted.

---

## Task 3: the three script call sites, and the two version constants

- [ ] `bakeoff/scripts/grade.py` — **a new module-scope import line** (this file imports nothing from `bakeoff.container` today), placed with the other `bakeoff.*` imports:

```python
from bakeoff.container import fresh_tree
```

  Replace `grade.py:294-295` with `work = fresh_tree(Path(cache) / "grade-preflight-tree" / task.task_id)`, carrying a comment that names the every-second-invocation reuse. The `finally` at `grade.py:324` stays.

- [ ] `bakeoff/scripts/run_matrix.py` — **a new module-scope import line**, with the other `bakeoff.*` imports. It is safe at module scope: `bakeoff.container` pulls `docker`, not litellm, and `--preflight-only` must stay off litellm, which is why `bakeoff.runner` / `claude_runner` / `proxy_callback` are deferred into `run_cell` (`run_matrix.py:320-323`). Do **not** move this import inside a function — it changes the indentation the mutation anchor matches.

```python
from bakeoff.container import fresh_tree
```

- [ ] `resolve_tasks` (the site the probe measured):
  - [ ] Replace `run_matrix.py:262-263` with `work = fresh_tree(cache / "preflight-tree" / task.task_id)` at **eight spaces** of indentation, and keep the allocation **outside** the new `try` (as `grade.resolve_task` does) — the mutation anchor matches that line.
  - [ ] Wrap the rest of the per-task body in `try: … finally: shutil.rmtree(work, ignore_errors=True)`, so the preflight tree does not outlive the resolution. Both `continue` paths (cached hit, NO-GO) must run the `finally`.
  - [ ] Drop the dead `"repo": work / "repo"` key from both `resolved[task.task_id] = {...}` statements — the literals are on lines **274** and **294** (the statements begin at 273 and 293). M5: nothing reads it.
  - [ ] The `resolve_tasks` docstring gains a paragraph: the previous shape — one stable path per task, `rmtree`'d at the *top* of the next invocation — is M1 exactly, and on a vitest task it made every second `--preflight-only` invocation gate on an empty tree.
- [ ] `run_cell`:
  - [ ] Replace `repo = run_root / "repo"` (`run_matrix.py:326`) with `repo = fresh_tree(run_root / "tree") / "repo"`. Keep the existing "No rmtree" comment and extend it: the artifacts root is per invocation *and* the tree leaf is per materialization, so a run-level retry lands on a path no container has mounted.
  - [ ] `if not args.keep: shutil.rmtree(repo.parent, ignore_errors=True)` — remove the whole leaf, not just `repo`.
  - [ ] Under `--keep`: `print(f"  kept {repo}")`, and return the kept path so it can be recorded. `run_cell` returns `(record, config_dump, unattributed)` today; make it `(record, config_dump, unattributed, kept)` where `kept` is `str(repo)` when `args.keep` else `None`, and update the single call site in `main` (`run_matrix.py:623` — 624 is its first argument line; there is exactly one).
  - [ ] In `main`'s row construction, add the key **only when the tree survives**. `rows.append({...})` is inline today (`run_matrix.py:657-672`, `rows.append(` on 657 and the closing `)` on 672), so there is no `row` variable to attach to — **hoist it**, in this exact three-line shape:

```python
            row = {
                ...  # every existing key, unchanged
            }
            if kept:
                row["kept_repo"] = kept
            rows.append(row)
```

    The key is **`kept_repo`**, not `repo`: this same commit deletes a dead `"repo"` key from `resolved` a few hundred lines up in this file, and a live `"repo"` key appearing here would make the diff read as a move rather than as two unrelated changes. Present-or-absent, never a null (D5).
- [ ] `bakeoff/src/bakeoff/preflight.py:177` — bump `PREFLIGHT_VERSION` by one from the value on disk (`"13"` → `"14"`), with a comment carrying D7's reason.
- [ ] **`bakeoff/tests/test_preflight.py:2212`**, inside `test_the_preflight_version_moved_with_the_new_assertion`: change `assert PREFLIGHT_VERSION == "13"` to `"14"` and append to the docstring's enumeration:

  > `13 -> 14` is not a new assertion. It retires cached PASS verdicts observed through a container this code can no longer interrogate: `preflight-tree/<task_id>` was one path per task, reused across invocations, and the Docker VM served the second container an empty `/repo` — which on a vitest task is `No test files found` at exit 1, the shape a PASS cannot be told apart from (round 2 item 3, 2026-09-03). Both caches key through `preflight_cache_key`, so `preflight.json` and `preflight-grade.json` invalidate together.

Tests:

- [ ] `bakeoff/tests/test_grade_script.py::test_two_resolutions_of_one_task_mount_different_host_paths(tmp_path, monkeypatch)` — the existing seam is `_stub_setup` at **line 954** (not 963) and its `fake_preflight(task, **kw)` records only `task.task_id`. Write a self-contained stub in the new test rather than changing the shared one:

```python
def test_two_resolutions_of_one_task_mount_different_host_paths(
    tmp_path, monkeypatch
):
    """Every second `grade.py` invocation re-mounted
    `grade-preflight-tree/<task_id>`, and the VM served it empty."""
    import scripts.grade as grade

    mounted: list[str] = []

    monkeypatch.setattr(grade, "build_task_image", lambda *a, **k: IMAGE)
    monkeypatch.setattr(grade, "image_entrypoint", lambda image: [])
    monkeypatch.setattr(grade, "materialize", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(grade, "ensure_oracle", lambda *a, **k: _oracle())

    def fake_preflight(task, **kw):
        mounted.append(str(kw["repo_path"]))
        return PreflightResult(
            task_id=task.task_id, task_version=task.task_version,
            start_sha=kw["start_sha"], image=kw["image"],
            manifest_digest=task.manifest_digest,
            preflight_version=PREFLIGHT_VERSION,
        )

    monkeypatch.setattr(grade, "preflight", fake_preflight)

    task = _task()
    for _ in range(2):
        grade.resolve_task(task, tmp_path, "sha256:base", force_preflight=True)

    assert mounted[0] != mounted[1]
    assert all(task.task_id in p for p in mounted)
```

- [ ] `bakeoff/tests/test_run_matrix.py::test_two_preflights_of_one_task_mount_different_host_paths(tmp_path, monkeypatch)` — monkeypatch `rm.build_task_image` (returns `"sha256:img"`), `rm.image_entrypoint` (`[]`), `rm.materialize` (returns a fixed 40-char sha), `rm.preflight` (records `str(kw["repo_path"])`, returns an ok `PreflightResult`), `rm.write_json` (no-op). Call `rm.resolve_tasks([task], bases, "2.1.220", tmp_path, force=True)` twice; assert the two recorded paths differ and both carry the `task_id`.
- [ ] `bakeoff/tests/test_run_matrix.py::test_the_preflight_tree_does_not_outlive_the_resolution(tmp_path, monkeypatch)` — parametrized ×2 over `preflight` returning ok / NO-GO (the failure path is the one that leaks). After `resolve_tasks` returns, assert `list((tmp_path / "preflight-tree" / task.task_id).iterdir()) == []`. Red on a reverted `finally`.
- [ ] `bakeoff/tests/test_run_matrix.py::test_two_runs_of_one_cell_mount_different_host_paths(tmp_path, monkeypatch)` — the first `run_cell` unit test. `run_cell` imports `execute_run`, `unattributed_count` and `ClaudeCodeConfig` *inside* the function body, so patch them on their own modules; `materialize`, `write_json` and `effective_config` are module-level names on `rm`:

```python
    import bakeoff.proxy_callback
    import bakeoff.runner
    import scripts.run_matrix as rm

    dests: list[str] = []

    def _fake_materialize(task, dest, cache):
        dests.append(str(dest))
        return SHA

    monkeypatch.setattr(rm, "materialize", _fake_materialize)
    monkeypatch.setattr(bakeoff.runner, "execute_run",
                        lambda **kw: _stub_record())
    monkeypatch.setattr(bakeoff.proxy_callback, "unattributed_count",
                        lambda d: 0)
    monkeypatch.setattr(rm, "write_json", lambda *a, **k: None)
```

  `_stub_record()` needs `artifacts.container_stdout` falsy (the `effective_config` branch is guarded at `run_matrix.py:378`; 377 is `config_dump = {}`) and the fields the return path touches. `args` is `SimpleNamespace(keep=False)`. Call `run_cell` twice with the same cell and the same `artifacts` root; assert the two `dest`s differ, that neither leaf survives afterwards, and — with `keep=True` — that the leaf survives and the returned `kept` is that path.

---

## Task 4: site 6 — the `CLAUDE_CONFIG_DIR` mount

- [ ] `bakeoff/src/bakeoff/runner.py:57` — the existing line is `from bakeoff.container import HostSampler, RunContainer`. It becomes, verbatim:

```python
from bakeoff.container import HostSampler, RunContainer, fresh_tree
```

- [ ] Replace **`runner.py:1045-1053`** — the two-line comment at 1045-1046, the assignment at 1047, the `rmtree` at 1048, the four-line `exist_ok` comment at 1049-1052 and the `mkdir` at 1053. (The range is 1045, not 1046: line 1045 is `# Fresh and empty per run: transcript discovery globs this directory and` and 1046 is its continuation, so starting at 1046 orphans half a sentence.) The replacement is one allocation and a comment that says what breaks without it:

```python
    # Fresh per run AND a path no container has mounted. This directory is
    # bind-mounted (`extra_mounts` below), and the rmtree + mkdir it replaces
    # was the stale-mount defect at a site nothing catches: the Docker VM
    # serves a replaced host inode from its cache -- measured 2026-09-02,
    # empty every other container -- and `_assert_repo_mounted` checks
    # `REPO_MOUNT` and only `REPO_MOUNT`. The agent then writes its transcript
    # into the cached inode, this directory stays empty, transcript discovery
    # globs it and finds nothing, and the run parses to zero turns, zero
    # tokens and zero cost with the tokens already spent.
    #
    # It also retires the `exist_ok=True` this line used to need: `rmtree` with
    # `ignore_errors` could not fail loudly, so a removal that did not work
    # left the bare `mkdir` raising FileExistsError -- outside every `try` in
    # this function, so the run died with no record at all. Nothing is removed
    # now, and a uuid leaf cannot collide.
    host_config_dir = fresh_tree(artifacts_root / "claude-config")
```

- [ ] Keep the emptiness read-back at `runner.py:1057-1066` and reword its message, which now names the wrong mechanism. `"config dir not empty after rmtree: ..."` becomes:

```python
                f"config dir not empty at allocation: {len(leftover)} entry(ies); "
                "transcript discovery may find another run's file"
```

  It becomes a post-condition on the allocator rather than on a removal — the same relationship `_assert_repo_mounted` has to `fresh_tree` one layer over. No test asserts the old string (checked across `tests/`); `stale_config` feeds `finalize_errors` at `runner.py:1080` (re-checked at HEAD: `finalize_errors: list[str] = [stale_config] if stale_config else []` is on 1080, not 1079 — see the Review 2 table) and that is unchanged.
- [ ] **Delete `import shutil` at `runner.py:38`.** Checked: `shutil` appears exactly twice in the file, at `:38` (the import) and `:1048` (the `rmtree` this task removes), so the import goes with it. This is stated rather than left as a check because a lint failure here is a needless second pass.
- [ ] Nothing else builds `artifacts_root / "claude-config"` (checked across `src/`, `scripts/`, `tests/` — the only other matches are `/eval/claude-config`, the container-side constant, and `test_claude_runner.py`'s own fixtures), so no other path needs updating.

Test:

- [ ] `bakeoff/tests/test_fault_injection.py::test_two_runs_under_one_artifacts_root_get_different_config_dirs(task, tmp_path, monkeypatch)` — this file's `_fake_run` already takes `artifacts_name` and `sample_index` and passes `artifacts_root=tmp_path / artifacts_name`, so "two runs under one artifacts root" is one call each with `sample_index=0` and `1` (distinct `run_id`s, so the event log's mode `"x"` does not refuse the second) sharing one `EventLog`. Assert `len(list((tmp_path / "artifacts" / "claude-config").iterdir())) == 2`. Docstring: this is the run-level-retry shape — the `rmtree` was already in the code here, and unlike `/repo` nothing downstream would have caught the stale mount; the run would have been recorded with zero turns and the tokens spent.

---

## Task 5: the integration test's private workaround, and the mutation anchors

- [ ] `bakeoff/tests/test_integration_node_task.py`, `node_tree` fixture (line 225): replace

```python
    tree = CACHE_ROOT / "tree" / uuid.uuid4().hex
```

with

```python
    tree = fresh_tree(CACHE_ROOT / "tree")
```

  change the import to `from bakeoff.container import RunContainer, fresh_tree`, and drop `import uuid` (line 39 — used nowhere else in the file). Rewrite the fixture docstring's closing paragraph: the uuid was this file's private workaround for M1, the production allocator now carries it, and this fixture is one of its callers rather than a parallel implementation. **Keep the measurement block verbatim** — it is the evidence for the whole change — and keep the module docstring's "THE EMPTY MOUNT HAS A SECOND SHAPE" paragraph, with "Hence a uuid per run tree" rewritten to name `fresh_tree`.
- [ ] **`_mounted` stays.** It asserts `"tests" in listing` — a claim about the expected *content*, strictly stronger than `_assert_repo_mounted`'s "anything at all". Add one sentence to its docstring saying the production guard now covers emptiness and this one covers content, so the next reader does not delete it as redundant.
- [ ] **Four** anchors in `bakeoff/scripts/mutation_check.py`, in the existing six-field tuple form — one per site whose revert is a distinct guarantee, and the fourth is site 6, which D8 now says has no mount post-condition at all: with no guard behind it, the anchor is the only thing that keeps that allocation from being reverted. All four `old` strings must match the code Tasks 1–4 write byte for byte, indentation included; if the implementation deviates, fix the anchor, never the other way round.

```python
    (
        # Round 2 item 3: the Docker VM serves a reused bind-mount source from
        # a stale cache -- measured 2026-09-02, `[files], [], [files], []` over
        # four cycles on one path. A fixed leaf name puts that back, and an
        # empty mount does not fail: it is `No test files found` at exit 1 on
        # both node frameworks and APPLY_FAILED on the grader. NOTE: under this
        # mutation the SECOND fresh_tree call raises FileExistsError at
        # `tree.mkdir()` before the assertion is reached -- `exist_ok=False` is
        # deliberate, so the test is red with a traceback rather than an
        # assertion diff.
        "mount: hand back one shared host path per key, restoring the stale mount",
        "src/bakeoff/container.py",
        "    tree = parent / uuid.uuid4().hex",
        '    tree = parent / "tree"',
        "tests/test_container.py -k different_paths",
        "not integration",
    ),
    (
        # The site the probe measured: `--preflight-only` twice on one task
        # gated the second invocation against an empty tree, and on a vitest
        # task that PASSES while measuring nothing.
        "preflight: reuse one host tree per task across invocations",
        "scripts/run_matrix.py",
        '        work = fresh_tree(cache / "preflight-tree" / task.task_id)',
        '        work = cache / "preflight-tree" / task.task_id\n'
        "        shutil.rmtree(work, ignore_errors=True)",
        "tests/test_run_matrix.py -k different_host_paths",
        "not integration",
    ),
    (
        # The site GRADER_VERSION's bump rests on: `grade-tree/<run_id>` reused
        # across passes, served empty, `git apply` failing, and APPLY_FAILED
        # stamping `resolved: False` on a submission that was fine.
        "grade: reuse one host tree per run_id across grading passes",
        "src/bakeoff/grader.py",
        '    tree = fresh_tree(Path(cache_root) / "grade-tree" / record.run_id)',
        '    tree = Path(cache_root) / "grade-tree" / record.run_id\n'
        "    shutil.rmtree(tree, ignore_errors=True)",
        "tests/test_grader.py -k different_host_paths",
        "not integration",
    ),
    (
        # Site 6, the one with no mount post-condition: `_assert_repo_mounted`
        # reads REPO_MOUNT only, and the host-side read-back in runner.py
        # answers for the allocator, not for the mount. A stale
        # CLAUDE_CONFIG_DIR is a run recorded with zero turns, zero tokens and
        # zero cost after the tokens are spent. NOTE: `new` restores the reused
        # path WITHOUT the `shutil.rmtree` line, because Task 4 deletes
        # `import shutil` from this module -- a NameError would make the test
        # red for a reason that is not the guarantee. Reuse alone is the
        # guarantee: under this mutation both runs share one config directory,
        # so the test sees 1 entry where it asserts 2.
        "config dir: reuse one host path per artifacts root across runs",
        "src/bakeoff/runner.py",
        '    host_config_dir = fresh_tree(artifacts_root / "claude-config")',
        '    host_config_dir = artifacts_root / "claude-config"\n'
        "    host_config_dir.mkdir(parents=True, exist_ok=True)",
        "tests/test_fault_injection.py -k different_config_dirs",
        "not integration",
    ),
```

---

## Task 6: docs, and the item's closure

- [ ] `bakeoff/src/bakeoff/container.py`, `RunContainer._assert_repo_mounted` docstring. The paragraph reading "Four production call sites reuse a path that way -- `grader.py`'s `grade-tree/<run_id>` on a re-grade, `oracle.py`'s `oracle-tree/<task_id>`, `grade.py`'s `grade-preflight-tree/<task_id>`, and `run_matrix.py` -- so the check belongs at the one place all four go through." becomes, in substance (write it in the file's voice):

  > Six production call sites reused a path that way until 2026-09-03; each now allocates through `fresh_tree`, so no container mounts a path an earlier one did. **The check stays**, because the cause it caught is one of several: a path the Docker VM does not share at all (`/var/folders`, the `--basetemp` convention) is untouched by that fix, a new call site that builds a path by hand is not covered by it, and a mount can fail to land for reasons that have nothing to do with the path. It is the post-condition; `fresh_tree` is the cause. It also covers `REPO_MOUNT` and only `REPO_MOUNT`. The `CLAUDE_CONFIG_DIR` mount is fresh by allocation and has **no** mount post-condition of its own: a freshly allocated config directory is *supposed* to be empty on the host, so there is nothing here for a mount-time check to compare, and `runner.py`'s own read-back answers for the allocator rather than for the mount.

- [ ] `docs/BUILDING-A-TASK-SET.md`: run **two** greps, not one — `grep -n "preflight-tree\|grade-tree\|oracle-tree\|\.cache/bakeoff"` for the cache layout, and `grep -n "PREFLIGHT_VERSION\|GRADER_VERSION"` for the constants. At the time of writing the first finds nothing and the second finds four **historical attributions** (`PREFLIGHT_VERSION` at 239, 282, 786; `GRADER_VERSION` at 729 — "the bare-runner probe (`PREFLIGHT_VERSION` 13)", "Since `PREFLIGHT_VERSION` 4", "already graded under the current `GRADER_VERSION`"), all of which stay true after both bumps. **Conclusion: the file does not move** — record that finding in the review log so a later reader does not re-check.
- [ ] `HANDOFF.md` does not move — the pruned-mirror cache is a different root and no sweep reaches it (D4). Nothing to add.
- [ ] `tasks/todo.md`: one review section, "Round 2 item 3: a unique host path per bind-mounted tree", carrying M1's four-cycle measurement, the six sites (with site 6 named as the one review 1 found), the version bumps and why (D6, D7), the sweep's real bound (D4), what was left open, and the tests added.
- [ ] `TASKS.md`: delete the closed item. Add **one** short P3 follow-up: a husk left by a killed process under a key that is never allocated under again (`grade-tree/<run_id>`, a per-cell tree, a per-run `claude-config`) survives until the operator's `rm -rf`, and the empty key directories are never collected — ~960 empty inodes for an 80 × 4 × 3 matrix. Do not re-open anything else.

---

## Verification

Run in this order. Nothing here spends money or needs credentials.

- [ ] **Unit.** `cd bakeoff && .venv/bin/python -m pytest tests/ -q`. Expected: `1554 passed, 62 deselected` (1539 baseline + 15 collected items from 13 new named tests, two of them parametrized ×2). **The gate is zero failures**; if the count differs, explain the difference before proceeding — do not absorb it. The four existing tests that change without changing the count are `test_oracle.py:440`, `test_oracle.py:451` (assertions) and the `GRADER_VERSION` / `PREFLIGHT_VERSION` pins (literals).
- [ ] **The §6.6 gate.** `cd bakeoff && .venv/bin/python scripts/verify_logger.py` → `GATE PASSED` (needs a Docker daemon; reports `GATE INCOMPLETE` and exits 1 without one).
- [ ] **Mutation.** Run **solo**: `cd bakeoff && .venv/bin/python scripts/mutation_check.py` → **164/164** (160 baseline + this plan's four).
- [ ] **Integration, node.** `cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" tests/test_integration_node_task.py --basetemp="$HOME/.cache/bakeoff-pytest"` → green, with `node_tree` now going through `fresh_tree`.
- [ ] **Integration, grader.** `cd bakeoff && .venv/bin/python -m pytest -v -m integration tests/test_integration_grader.py --basetemp="$HOME/.cache/bakeoff-pytest"`. Its volatile-path wipe at `test_integration_grader.py:128` is `("grade-tree", "grade-scan", "oracle-tree", "scratch")` — all key roots, unchanged by this plan, so it needs no edit.
- [ ] **The acceptance test for this item — the loop that measured the defect.** With the vitest task `ufo-214` under `~/.cache/bakeoff-probe/taskset/`, run **four times in a row**:

```
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
    --task-set ~/.cache/bakeoff-probe/taskset --tasks ufo-214
```

  All four must print `preflight PASS`. Before this change the even-numbered invocations refuse with `_assert_repo_mounted`'s `ContainerError` (and, before that guard existed, passed while measuring nothing). Record the four outcomes in the review log — this is the measurement that closes the item.
- [ ] **The grader half.** Grade one event log twice against one cache root:

```
cd bakeoff && .venv/bin/python scripts/grade.py --event-log ~/.cache/bakeoff/<log> --re-grade
cd bakeoff && .venv/bin/python scripts/grade.py --event-log ~/.cache/bakeoff/<log> --re-grade
```

  The two passes must agree on every `resolved` verdict, with no `APPLY_FAILED` appearing only in the second. Note in the review log that the `GRADER_VERSION` bump means the first of these writes new lines for every already-graded run, by design.

---

## What this does NOT do

- **Does not remove or weaken `_assert_repo_mounted`** (D8), and does not extend it to the config mount (D8's closing paragraph).
- **Does not add a record or grade field, and moves no schema version** (D5). `SCHEMA_VERSION` stays `3.8.0`; `GRADE_SCHEMA_VERSION` stays `1.3.0`. The only place a host path is newly written down is the `matrix-<stamp>.json` row, under a `"kept_repo"` key present only when the tree was kept.
- **Does not contain the pre-container region of `execute_run`** (D10). `fresh_tree` raising at `runner.py:1047` loses the record exactly as the bare `mkdir` there does today; one concrete failure mode (a silent `rmtree` failure into `FileExistsError`) is removed and none is added.
- **Does not sweep the key level**, so a husk under a key never allocated under again survives until the operator's `rm -rf`, and the empty key directories are never collected (D4). Stated, bounded, and left as a P3 line.
- **Does not touch `run_id`**. It still names no episode and still collides across collections; `collection_id` remains the thing that distinguishes them (D9).
- **Does not fix the other empty-mount shape.** A path the Docker VM does not share (`/var/folders`) is still handled by the `--basetemp` convention, with `_assert_repo_mounted` as the catch.
- **Does not touch `smoke_test.py` or `dry_run.py`** as allocators (Q5 ruling); their `claude-config` mounts change only because they go through `execute_run`.
- **Does not touch `materialize`, `preflight`, `images.py`, `proxy.py`, the manifest, or any runner argv.** No `start_sha` moves.
- **Does not make two concurrent graders safe against each other in general.** It makes them safe against *this*: they no longer mount the same path. The oracle and preflight caches are still last-writer-wins, which is pre-existing.

---

## Review 1 → changes

| # | finding | what changed |
|---|---|---|
| 1 | site 6 (`runner.py`'s `claude-config`) missed; D9 and "does NOT" asserted it was never mounted | **Accepted in full, option (a).** M4 gains site 6 with a "caught by the guard?" column and the zero-turns failure mode; new **Task 4** allocates it through `fresh_tree`, rewords the read-back message and adds a `test_fault_injection.py` test; new **D10** answers the "the `exist_ok` comment has to move or be justified" half — the comment is rewritten because the failure mode it documents ceases to exist, and the residual `OSError` exposure is the bare `mkdir`'s, unchanged and explicitly not contained here. D9 bullet 2 and the "does NOT" bullet corrected; `settings_host_path` and the two `proxy.py` mounts stated as not-at-risk with the reason. |
| 2 | `test_grader.py:1236` and `test_preflight.py:2212` pin the literals | **Accepted.** Both files added to File Structure and to Tasks 2 and 3, with the docstring sentence written out verbatim for each so the implementer transcribes rather than invents. |
| 3 | 12 vs 13 test count, and parametrization | **Accepted.** File Structure carries the arithmetic — 13 named, 15 collected, two parametrized ×2 — and Verification states `1554 passed, 62 deselected` **and** that the gate is zero failures with any deviation explained rather than absorbed. |
| 4 | D4's bound is false for three of five parents; husk count understated | **Accepted, second option (restate honestly).** D4 now says which parents the sweep bounds (`preflight-tree`, `grade-preflight-tree`) and which it does not, gives the ~960-empty-inode figure, and records the rejection of sweeping the key level *because it reintroduces the very race the Q4 ruling used to reject `parent.rmdir()`* — taking the first option would have contradicted that ruling. `_sweep_stale_trees`'s docstring gains the `st_mtime`-is-allocation-time sentence and the leaf-level-only paragraph. A P3 line records the residue. |
| 5 | the `finally` claim is wrong (STRANDED), and print-only is "a log nobody keeps" | **Accepted in full.** D5 names the STRANDED path from `run_matrix.py:383-384` / `635-645` beside `--keep`; the kept path now goes into the `matrix-<stamp>.json` row under a key written only when kept (present-or-absent, the `wire_log_gz` discipline), with the print retained; `run_cell` returns the kept path and `main` writes it. The schema decision stands, and D5's first bullet drops the reason the review called wrong (the "permanently null field" argument), keeping the two that hold. |
| 6 | the import lines do not match the files | **Accepted.** All four written verbatim: `grader.py:79` keeps `ContainerError`; `oracle.py:67` extends; `grade.py` and `run_matrix.py` each get a **new** module-scope line, with the litellm sentence explaining why module scope is safe and why moving it into a function would break the anchor indentation. |
| 7 | the `test_grader.py` and `test_grade_script.py` seams cannot be transcribed as described | **Accepted.** Both new tests are written out in full as self-contained fakes (the `FakeContainer`-is-local, `cache_root`-hard-coded and single-call problems in `_grade_capturing`; the `_stub_setup` line corrected to **954** and its `fake_preflight` not reused). The `run_cell` test keeps its shape, which the review verified as feasible, with the recording fake written out. |
| 8 | the umask test asserts the environment, and its escape hatch unpins D2 | **Accepted.** Replaced with the relative assertion `stat.S_IMODE(...) == 0o777 & ~old`, renamed `test_an_allocated_tree_is_not_mkdtemps_owner_only_mode`, and the "delete the test" branch is gone with an explicit "do not delete or weaken it". |
| 9 | no anchor on the grader site D6 rests on | **Accepted.** Third anchor added on `grader.py`; Verification moved to 163/163 (**superseded by Review 2 item 1 — the final count is 164/164**); anchor 1's comment records that its mutation goes red via `FileExistsError` at the second allocation rather than an assertion diff. |
| 10 | D6's `APPLY_FAILED` claim is present-tense and no longer true at HEAD | **Accepted.** D6 now says the suspect lines are the grades written before `_assert_repo_mounted` landed and that the guard converts the same defect into a contained environment error today; M2 carries a pointer to that. The bump stands (Q2 ruling). |
| 11 | D7 names one cache; the docs grep would have missed the version constants | **Accepted.** D7 names both `preflight.json` and `preflight-grade.json` with their constants and line numbers and says both drivers pay the re-gate; Task 6 runs a second grep for `PREFLIGHT_VERSION` / `GRADER_VERSION` and records the four historical attributions as the evidence that `BUILDING-A-TASK-SET.md` does not move. |
| 12 | line-number precision | **Accepted.** The `"repo"` literals are cited at **274 and 294**; `run_matrix.py:326` replaces the earlier `325-337` range for `run_cell`'s path; `_stub_setup` at **954**; the `CLAUDE.md` section now says the `run_matrix.py:137-145` citation was already stale before this plan and gives the replacement either the current line (which this change deletes) or none. |

**Rulings adopted as given.** Q1 bump `PREFLIGHT_VERSION` (D7, with the precedent cited); Q2 accept the re-grade cost (D6, with finding 10's correction); Q3 keep site 5 in scope *and therefore* site 6 (M4, Task 4); Q4 keep the husks (D3's rejection of `parent.rmdir()`, D4's honest bound); Q5 fix the umask assertion rather than delete it (Task 1) and leave `smoke_test.py` / `dry_run.py` alone (D9).

**Disputed: none.** Every finding was checked against the tree at HEAD `8232032` before being accepted — including the two the review left open as choices (4: which of its two options; 5: where the kept path goes), which are argued above rather than merely applied.

### Review 2 → changes

Review 2 **APPROVED** the plan (12 of 12 addressed) with seven items to carry into implementation, none blocking. All seven are folded in below, so this document is what the implementer transcribes.

| # | item | what changed |
|---|---|---|
| 1 | site 6 has no mutation anchor, in a plan whose self-review says to implement Task 4 first | **Adopted.** Task 5 now carries **four** anchors; the fourth reverts `runner.py`'s allocation and targets `tests/test_fault_injection.py -k different_config_dirs`. Verification moves to **164/164** and File Structure to "4 new anchors". One deviation from the suggested `new`, with its reason in the anchor comment: it restores the reused path **without** the `shutil.rmtree` line, because Task 4 deletes `import shutil` from that module and a `NameError` would make the test red for a reason that is not the guarantee. Reuse alone is the guarantee — under the mutation both runs share one config directory, so the test sees one entry where it asserts two. |
| 2 | D8's closing paragraph overstates the read-back as a post-condition | **Adopted in full.** D8 now says plainly that after this change site 6 has **no** mount post-condition, that the `runner.py:1057-1066` read-back cannot observe a stale mount because it reads the host side (empty by construction after allocation), that it is a post-condition on the *allocator*, and that `trajectory_parse_error` on a zero-turn run is the only downstream signal. The same overstatement in Task 6's `_assert_repo_mounted` docstring text is corrected the same way, and the self-review note now says never to implement Task 4 without its anchor. |
| 3 | Task 4's replacement range starts one line late | **Adopted.** Verified at HEAD: 1045 is `# Fresh and empty per run: transcript discovery globs this directory and`, 1046 its continuation, 1047 the assignment, 1048 the `rmtree`, 1049-1052 the `exist_ok` comment, 1053 the `mkdir`. The range is **1045-1053**, and the plan now says why starting at 1046 orphans half a sentence. |
| 4 | `shutil` in `runner.py`: state the answer | **Adopted.** Verified: `shutil` appears exactly twice, `:38` (import) and `:1048` (the line Task 4 deletes). The step now reads "delete `import shutil` at `runner.py:38`" rather than asking for a check. |
| 5 | three more line slips, and `runner.py:57` verbatim | **Two of three adopted, one disputed with evidence.** The `run_cell` call site is **623** (624 is its first argument line) — corrected. The `effective_config` guard is **378** (377 is `config_dump = {}`) — corrected. `runner.py:57` is written out verbatim as `from bakeoff.container import HostSampler, RunContainer` → `…, fresh_tree`. **Disputed: `finalize_errors` is at 1080, not 1079.** `grep -n "finalize_errors: list"` at HEAD `8232032` returns `1080:    finalize_errors: list[str] = [stale_config] if stale_config else []`; 1079 is the comment above it (`# Accumulated by the finalize phase below, one entry per step that failed.`). The plan keeps 1080 and now says so at the call site, so the next reader does not "fix" it back. |
| 6 | `test_container.py` needs more than `import stat` | **Adopted.** Verified the file's whole import block is `threading`, `time`, `pytest`, `from bakeoff.container import ContainerError, HostSampler, RunContainer`. Task 1 now writes out the additions: `os`, `shutil`, `stat`, `import bakeoff.container` (the module object, for the two sweep-failure monkeypatches), and `STALE_TREE_AGE_S` + `fresh_tree` added to the from-import. |
| 7 | the row edit needs a hoist; consider `kept_repo` | **Both adopted.** Verified `rows.append({` is inline at `run_matrix.py:657` with the closing `)` at 672, so there is no `row` variable; Task 3 now writes the three-line `row = {...}` / `if kept:` / `rows.append(row)` shape. The key is **`kept_repo`**, for the reason given: this commit deletes a dead `"repo"` key from `resolved` in the same file, and a live `"repo"` key here would make the diff read as a move. D5 and "What this does NOT do" carry the new name. |

**On finding 4's argued choice**, review 2 checked the window and agreed the key-level sweep would have reintroduced the `FileNotFoundError` race — `parent.mkdir(parents=True, exist_ok=True)` on an existing directory does **not** refresh its mtime, so a key older than `STALE_TREE_AGE_S` stays sweepable in exactly the moment a live process is allocating under it. That mechanism is now the reason recorded in D4, and it is stronger than the one the revision first wrote.

---

## Self-review notes

- **Site 6 changes this item's shape.** It is the only site whose failure is a *lost cell with the tokens spent*, and — as D8 now says plainly — the only one **no post-condition covers at all** after this change. If anything here is implemented partially, implement Task 4, and never Task 4 without its anchor: the anchor is standing in for the guard site 6 does not have.
- **D7 remains the judgment call**, now with the review's ruling and precedent behind it.
- **The sweep is honest but small.** It bounds the two parents that are revisited per invocation and nothing else. That is worth having and is not worth growing into a garbage collector; the P3 line is where the rest goes.

---

## Sentences that belong in `CLAUDE.md`, to be applied after this branch merges

1. **New invariant bullet.** "A host path that has been bind-mounted once is never bind-mounted again. The Docker VM caches the directory it serves for a mount source, and a path `rmtree`'d and re-created underneath that cache is served EMPTY — measured 2026-09-02, `[files], [], [files], []` over four cycles on one path. An empty mount resolves rather than failing: an empty `/repo` is `No test files found` at exit **1** under vitest and jest, the same exit a red suite gives, so a gate passes while measuring nothing, and `git apply` fails, which the grader records as `APPLY_FAILED` → `resolved: False` against a submission the model really produced; an empty `CLAUDE_CONFIG_DIR` — a sixth site, which `_assert_repo_mounted` does **not** cover, since it checks `REPO_MOUNT` and only `REPO_MOUNT` — is a run that parses to zero turns, zero tokens and zero cost with the tokens already spent. `container.fresh_tree` allocates a uuid leaf under the stable key at all six mount sites; `RunContainer._assert_repo_mounted` remains as the post-condition, because a path the VM does not share at all (`/var/folders`) and a call site that builds a path by hand are not covered by the allocator."
2. **A correction to an existing sentence.** The cached-artifact invariant currently defends the preflight cache with: "its tree is `rmtree`d and re-materialized on every invocation whether the key hits or not (`run_matrix.py:137-145`), so only the verdict is cached, never an artifact the key describes from inside." That rmtree-and-re-materialize *was the cause* of the stale mount; the tree is now allocated fresh per invocation and removed when the resolution ends. The claim being made — that only a verdict is cached — survives; the mechanism cited for it must change, and the sentence should add that the verdict itself was suspect until 2026-09-03, which is why `PREFLIGHT_VERSION` moved. **Note for whoever applies this:** the `run_matrix.py:137-145` line reference is already stale at HEAD `8232032` (that range is `assert_one_agent`'s docstring), independently of this change. Replace it with the current location (`run_matrix.py:262-263`, which this change deletes) or carry no line reference at all.
