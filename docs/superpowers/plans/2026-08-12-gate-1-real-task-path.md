# Gate 1 — the real-task input path

**Goal.** One real issue, end to end, on all four arms, with a precondition
strong enough that a failure is attributable to the model.

**Status.** Implemented 2026-08-12 at schema 3.5.0. §6 is the review log —
three passes, the third one found during implementation. The outcome and what
it turned up are in [tasks/todo.md](../../../tasks/todo.md).

Spec: [../specs/2026-08-03-llm-bakeoff-eval-design.md](../specs/2026-08-03-llm-bakeoff-eval-design.md)
Backlog: [../../../TASKS.md](../../../TASKS.md) P0

---

## 1. What is actually missing, and why

`TASKS.md` frames Gate 1 as five missing pieces (manifest, loader, matrix
driver, per-task images, `.gitignore`, `task_set_commit`). That list is
correct but it is a list of symptoms. Two root causes produce all of them.

**Root cause A — the harness has never had a precondition on its INPUT.**
Every check that exists is about the harness (capture, isolation,
attribution) or about a run's output (turns, tool calls, a diff). Nothing
asserts that the thing being handed to the agent is a task at all. That is
the defect that invalidated every Phase 0c capability figure: the image
shipped no `pytest`, `tests/test_calc.py` was not importable, and the only
verification command available raised `ModuleNotFoundError` with the bug
fixed and unfixed alike. `assert_agent_can_verify_its_work` was the fix, and
it is too weak — it proves a binary is on `PATH`, not that *this task, in
this image, discriminates between a solved run and an idle one*.

Six of six "model failures" so far have been harness defects. The base rate
is the argument: the cheapest possible way to keep it from becoming seven is
a gate that runs offline, before any spend, and refuses to start a matrix on
a task that cannot be shown to be red before the fix and green after it.

**Root cause B — the eval's operational knowledge lives in a script.**
`scripts/smoke_test.py` is the only place that knows the proxy topology, the
two credential paths, the image build, the §5.2 config dump and the ordering
rules. A matrix driver written beside it either duplicates that knowledge —
in which case the Phase 0c gate and the real run drift into two different
environments, and the gate stops gating anything — or reaches into a script
from library code. The extraction is not tidiness; it is what keeps the gate
and the run the same environment.

Everything below follows from those two.

---

## 2. Design

### 2.1 A task on disk

One directory per task under a task-set root (default `bakeoff/taskset/`),
`--task-set` to point elsewhere. `task.yaml` follows spec §3.7:

```yaml
task_id: click-3360-write-usage-empty-args
task_version: 1
tier: B                      # §3.2: mined — tested PR, no transcript
stratum: small-edit

repo:
  url: https://github.com/pallets/click.git
  base_sha: 63274a79d08fdc5c19220696144489f7144a8547   # 40 hex, validated
  start_sha: null            # computed; pin it once and it is verified

prompt: |                    # §3.3 verbatim. No harness-injected text.
  ...

tests:
  paths: ["tests/"]          # the prefix that splits reference.diff
  runner: ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
  f2p: [ ...node ids... ]    # must fail at start, pass after the reference
  p2p: []                    # empty = "the rest of the suite"

image:
  apt: []
  pip: ["pytest==8.3.5"]
  build: ["pip install -e ."]

budget:
  max_turns: 40
  wall_clock_timeout_s: 900

provenance:
  repo: pallets/click
  issue: 3360
  pr: 3434
  merge_commit: 3bb230dcd5d751f8605b46e9df5a541639d5fd4e
```

Beside it, `reference.diff` — the merged PR diff, verbatim from
`git diff <base_sha> <merge_commit>`.

**One reference diff, split at load, never two hand-maintained files.**
`tests.paths` splits it into a *test half* and a *solution half* on
`diff --git` boundaries. Two properties fall out and both are checked:
the halves recompose to the original byte for byte, so nothing can be
silently dropped from the reference; and a file that is neither is a load
error rather than a quiet omission.

### 2.2 The start state, and why it is not `base_sha`

A real bug-fix PR adds the test that proves the fix. At `base_sha` that test
does not exist, so there is nothing for §3.3's "runs tests, sees failures,
self-corrects" loop to run against — the same truncation that invalidated
Phase 0c, arriving through the dataset instead of the image.

So the *test half* is applied to `base_sha` and committed. That commit is the
state the container is detached to.

- The submission diff (§5.6, `git diff --cached <start>`) therefore does not
  contain the test patch, and `restore_paths` restores the patched tests
  rather than deleting them.
- The commit is made with a fixed author, committer, date and message, so its
  SHA is a pure function of `(base_sha, test half, gitignore_extra)`. It is
  re-derivable from the manifest, which is what keeps §5.1's "byte-identical
  world" checkable. `start_sha` in the manifest is optional: absent, it is
  computed and printed; present, a mismatch is a load error — which is what
  catches a re-cut patch, a different `git apply` whitespace decision, or an
  edited manifest that nobody meant to change the start state.

Materialization itself: a bare mirror per repo under the cache root, fetched
once and validated with `git cat-file -e <base_sha>^{commit}` so an
unresolvable SHA is loud rather than a checkout onto whatever was there; then
`git clone --local` per run, which hardlinks objects and so costs neither
time nor disk at 2,400 runs; then `git remote remove origin`, because a
remote pointing at a host path that does not exist inside the container is
both a confusing error surface for the agent and a host path leaked into the
run.

**This is a methodology decision, not a mechanical one, and it is recorded
as one.** Handing the agent the oracle makes the task "make this test pass"
rather than "fix this bug", which is what SWE-bench measures and is not
identical to the harvested workflow. The alternative — a hidden oracle
applied only at grading — makes every task's difficulty depend on the
agent's test-writing habits, confounding the thing being measured. Default:
apply. Recorded in TASKS.md P3 for the scorecard.

### 2.3 The image

One base image (today's `docker/eval-agent.Dockerfile`, unchanged) carrying
the parts that must be identical for every task and every arm: pinned Claude
Code, git, ripgrep, the non-root user, the mount points, the cleared
entrypoint. Per task, a **generated** Dockerfile:

```
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get ... <image.apt>
RUN pip install --no-cache-dir <image.pip>
COPY repo /repo                 # base_sha, for dependency resolution only
RUN cd /repo && <image.build>
RUN chown -R eval:eval /repo
USER eval
WORKDIR /repo
ENTRYPOINT []
```

Generated rather than hand-written, because the two most expensive ways a
task image can be wrong are structural and a generator makes them
impossible: an image left as `USER root` makes Claude Code refuse
`bypassPermissions` and exit before emitting one event, and an image with an
inherited `ENTRYPOINT` turns `RunContainer`'s `sleep infinity` into an
immediate exit. Both read as total model failure. A hand-written escape
hatch is deliberately deferred until a task needs one.

`/repo` in the built image is a scaffold: at run time the bind mount
replaces it. That is what makes `pip install -e .` correct and
`pip install .` wrong — a non-editable install resolves imports to
site-packages, so the agent's edits to `/repo` do nothing and every arm
fails identically. §2.4's green-after check catches exactly that.

### 2.4 The precondition — `preflight`

Offline, no credentials, no spend, run per task before the matrix starts.
One container, the pinned image, the materialized start state bind-mounted.

Environment, because each of these reads as a model failure:

| check | what it prevents |
|---|---|
| `id -u` ≠ 0 | Claude Code refuses `bypassPermissions`, emits nothing |
| `claude --version` == base image's pin | task images silently diverge |
| `git`, `rg` present | `snapshot_diff` returns empty = "clean tree" |
| test runner resolves | §3.3's loop truncates after "edits" |
| image `Entrypoint` empty | container exits instead of sleeping |
| no `CLAUDE.md` / `AGENTS.md` / `.claude/` in the start state | §5.2 contamination, per task |

Task, which is the half that has never existed:

1. `git rev-parse HEAD` == `start_sha` — measured, not assumed.
2. **Red before.** Run the F2P node ids explicitly. Exit code must be **1**
   — tests ran and failed. It must never be 2, 3, 4 or 5: collection error,
   internal error, usage error (which is what a node id that does not exist
   produces) and "no tests collected" are all non-zero and all mean the
   environment is broken rather than the bug is present. That distinction is
   the whole check; an `assert result.returncode != 0` would pass on the
   exact Phase 0c failure it exists to catch. The set of `FAILED` lines must
   equal the declared F2P set exactly.
3. **P2P green before.** `--deselect` each F2P id and run the rest of the
   suite; exit code must be 0. Explicit selection on both sides rather than
   one whole-suite run parsed into per-test results: exit codes and `FAILED`
   lines are stable across pytest majors, and a report parser is a second
   thing that can be wrong about what happened.
4. **Tree stays clean.** `git status --porcelain` is empty after the suite
   runs. This is the `.gitignore` P0 item, stated as the property that
   actually matters rather than as a file that must exist: §5.6 stages
   everything, so a suite that drops `__pycache__` makes diff size and file
   counts measure the interpreter. Remedy is a manifest-declared
   `gitignore_extra`, applied in the setup commit and therefore visible in
   `start_sha`.
5. **Green after.** Apply the solution half: F2P exit 0, P2P exit 0. Then
   revert, so preflight leaves the start state exactly as it found it.

Steps 2 and 5 are `tests/test_smoke_fixture.py`'s two halves, generalized
from the fixture to any task and moved from the host into the pinned image —
which is where the Phase 0c defect actually lived.

Preflight needs a Docker daemon and, the first time a task is seen, network
to populate the repo mirror. It needs no credentials and spends nothing.

### 2.5 The matrix driver

`bakeoff/src/bakeoff/matrix.py`, CLI at `scripts/run_matrix.py`.

**Ordering (§5.7, §5.8).** Cells are `(task, arm, sample_index)`. Order is
round-major: every `(task, arm)` appears exactly once per round, shuffled
within the round under a recorded seed, rounds concatenated. Repeats of a
cell are therefore separated by a whole round — the strongest separation
available — and order is randomized as §5.7 requires while staying
reproducible. A round boundary that would place the same `(task, arm)`
back-to-back is fixed deterministically. The seed and the resulting order
are reported, not assumed (§5.8).

**Resume, which is mandatory rather than nice.** `run_id` is a hash of
`(task, model, sample, attempt)` and `write_run` opens mode `"x"`, so a
second invocation against the same event log raises `ImmutabilityError`.
`smoke_test` dodges this with a fresh timestamped root per invocation; a
matrix that runs for days across many invocations cannot. The driver
computes each cell's `run_id`, skips cells already written, and reports the
count. A stale `<run_id>.json.partial` poisons its cell permanently, so it
is detected and named rather than deleted — deleting another process's
in-flight write is not the driver's call.

**Resume is where the worst silent failure in this design lives, and it
needs two guards.** `task_version` is *not* part of `run_id`. So editing a
task — a changed prompt, a re-cut patch, a new dependency pin — and
re-running leaves every existing cell looking complete, and the matrix
silently mixes records of the old task with records of the new one under one
`task_id`. The driver therefore reads each skipped cell's record and refuses,
loudly and by cell, when its `task_version` does not match the manifest's.
The same read compares `versions.container_image_digest` against the image
about to be used; a mismatch is a §5.1 violation across the matrix, but
images legitimately rebuild, so that one warns, lists the affected cells and
requires `--allow-mixed-images` to continue rather than making resume
useless.

**Containment.** One cell's exception must not cost the other 2,399, and it
must not be swallowed either. Per-cell `try`, the failure recorded and
printed, the matrix continues, the process exits non-zero with the list. A
run of consecutive infrastructure failures (the shape of an expired
credential, which is P1 and unfixed) aborts the matrix loudly rather than
grinding through the remainder.

**What the driver gates on, and what it does not.** `smoke_test.run_problems`
is the Phase 0c *capability* criterion — no diff is a NO-GO there, because
the question is whether an arm can drive the loop at all. The matrix asks a
different question, and a model failing a task is the data it is collecting.
So the driver checks only what makes a record uninterpretable: the §5.2
config dump (which is how an unmounted settings file surfaces), an empty wire
log, lost attribution, `isolated=False`. Those are reported per run and
counted; they never stop a run from being written, because the log keeps
excluded runs (§6.4).

The settings mount is passed explicitly on every run. Claude Code does not
fail on a `--settings` path that is not there — it starts with none of the
pinned settings, the agent cannot edit anything, every arm lands no diff, and
a one-line harness omission reads as four capability findings.

**Sequential.** `last_run_id_for` reads the index before the container starts
and the index is appended unlocked, so two concurrent runs of one
`(task, model)` read the same prior id and neither sees the other. That is a
P1 precondition on §5.7's parallelism, not something this driver resolves.

### 2.6 `task_set_commit`

Computed by the loader from the task-set root's git state, `-dirty` exactly
as `harness_commit` does, and carried on `TaskSpec`. A hand-built `TaskSpec`
— tests, `dry_run`, `smoke_test` — keeps `""`, which stays the honest blank
it is today.

`SCHEMA_VERSION` moves to 3.4.0 even though no field is added. In 3.3.0 an
empty `task_set_commit` means "no dataset exists"; in 3.4.0 it means "this
task declared none". Same bytes, different claim — which is the case this
codebase's versioning rule exists for.

### 2.7 Extraction (root cause B)

Moved out of `scripts/smoke_test.py` into `src/bakeoff/`, verbatim:

- `proxy.py` — `Proxy`, `proxy_environment`, `freeze_sigv4_credentials`
- `session.py` — `effective_config`, `config_problems`, `config_differences`

`smoke_test` re-exports them, so the Phase 0c gate and its tests are
unchanged and their passing is the regression evidence.

---

## 3. The first real task

`pallets/click` #3360 / PR #3434 — `HelpFormatter.write_usage` emits a blank
line instead of the usage line when a command has no arguments.

Validated on the host before this plan was written:

| | result |
|---|---|
| suite at `base_sha` (pytest 8.3.5) | 1614 passed, 25 skipped, 1.4 s |
| + test half | 7 failed, 1615 passed |
| + solution half | 1622 passed, 0 failed |

Why this one:

- **Real.** A reported issue with a reproduction, a merged fix, a regression
  test written by the maintainer.
- **The tests assert behavior, not implementation.** Rendered output, not an
  internal name. A different-but-correct fix passes. The other candidate
  considered (#3678) pins an arbitrary storage name `_click_default_help` in
  `test_info_dict.py`, which would score a correct alternative fix as a
  failure — the exact false negative this eval cannot afford.
- **Hermetic and fast.** Zero runtime dependencies on Linux, `pytest` only,
  1.4 s for 1,614 tests — which makes §5.5's per-turn checkpoint grading
  cheap rather than a feasibility question.
- **Not the fixture.** 20k lines, `src/` layout, an editable install, 500
  lines of existing formatting tests to not regress. The agent has to
  navigate and verify, not guess one operator.

**It also surfaced a real design input:** the suite does not collect under
the base image's pinned `pytest 9.1.1` (click sets `filterwarnings = error`
and 9.x raises `PytestRemovedIn10Warning` at collection, fixed upstream two
months after this base_sha). Per-task dependency pinning is therefore not a
nicety — the base image's pin cannot be right for every task, and §5.1's
"dependencies from lockfile, pre-installed into the image" is per task.

---

## 4. Work items

1. `src/bakeoff/tasks.py` — manifest dataclasses, loader + validation, diff
   splitter, mirror cache, materialization, `task_set_commit`.
2. `src/bakeoff/taskimage.py` — Dockerfile generation, build, image ID.
3. `src/bakeoff/preflight.py` — §2.4, returning problems rather than raising.
4. `src/bakeoff/proxy.py`, `src/bakeoff/session.py` — extraction, verbatim.
5. `src/bakeoff/matrix.py` + `scripts/run_matrix.py` — ordering, resume,
   containment, reporting, `--preflight-only`.
6. `TaskSpec.task_set_commit` + `assemble_record` + `SCHEMA_VERSION` 3.4.0.
7. `bakeoff/taskset/click-3360-write-usage-empty-args/` — the task.
8. Tests for every one of the above.
9. Docs: `CLAUDE.md`, `TASKS.md`, `tasks/todo.md`.

## 5. Verification

Offline, in order: unit suite → `mutation_check.py` → `verify_logger.py`
(which runs the integration suite, the dry run and the offline smoke) →
`run_matrix.py --preflight-only` on the click task → an offline matrix run.

Live, last: four arms × N=1 on the click task. That is Gate 1's exit
criterion, and nothing above it is allowed to be assumed.

`verify_logger.py` is deliberately **not** extended with any of this. It is
the §6.6 *logging* gate and its defining property is that it is offline and
free; preflight needs a daemon and, once per task, network. Task validity is
a separate gate with a separate entry point, and the matrix driver runs it
itself before spending anything.

---

## 6. Review log

### Pass 1 — what draft 1 got wrong

1. **Per-test results by report parsing.** Draft 1 left "run the suite and
   work out which tests passed" unspecified, and the obvious implementation
   is a JUnit XML parse. Rejected: pytest's `classname` cannot be mapped back
   to a node id unambiguously (a dotted segment is a package or a class and
   the XML does not say which), so the parser would be a second thing that
   can be wrong about what happened. Replaced with explicit selection and
   `--deselect`, which reduces the whole check to exit codes and `FAILED`
   lines.
2. **`returncode != 0` as the red-before check.** Would have passed on the
   exact Phase 0c failure — `ModuleNotFoundError` is also non-zero. The check
   is `== 1`, with 2/3/4/5 named as environment breakage.
3. **Resume against an edited task.** `task_version` is not in `run_id`, so
   resume would silently mix two different tasks under one `task_id`. This
   was the most damaging hole in draft 1 and it is exactly the class Gate 1
   is supposed to close — "silently running the wrong thing, which looks like
   a quiet run". Now a hard refusal, with the image-digest variant as a
   warning behind a flag.
4. **`.gitignore` as a file to check for.** Draft 1 inherited TASKS.md's
   phrasing. The property that matters is that running the suite does not
   dirty the tree; checking for a filename would pass on a `.gitignore` that
   does not cover what this suite actually drops.
5. **Materialization left vague.** Now explicit: mirror, `cat-file -e`
   validation, `--local` hardlink clone, `remote remove origin`.
6. **The driver's gating policy was unstated**, and the available code to
   copy — `run_problems` — is the wrong policy for a matrix: it fails a run
   that landed no diff, which at collection time is data rather than a fault.

### Pass 2 — five more

7. **`RunContainer.exec` has no timeout.** A task whose suite hangs would
   hang preflight forever, and preflight is the thing that runs before every
   matrix. Every test command is wrapped in coreutils `timeout` inside the
   container, the same mechanism `exec_stream` documents for the agent.
8. **Preflight cost at 80 tasks.** Re-validating every task on every resume
   turns a 30-second restart into half an hour, and a gate that is expensive
   to run is a gate that gets skipped — the argument `verify_logger` already
   makes about staying free. Results are cached beside the event log, keyed
   on `(image id, start_sha, manifest digest)`, with `--force-preflight`.
9. **`EVAL_ARMS` would have been duplicated.** The four arms and which
   transport each rides are one fact; the gate and the matrix disagreeing
   about it is root cause B in miniature. It moves to `proxy.py`, next to
   `proxy_environment`, which is where the transport split is already
   explained.
10. **The offline matrix run needs no new machinery and no stub changes.**
    The stub answers with a canned edit to the smoke fixture's `calc.py`, so
    against the click task it lands a real diff that means nothing. That is
    exactly the right claim for an offline path test — manifest, mirror,
    materialization, image, preflight, ordering, resume, capture and record
    are all real; the answer is a fixed script. Parameterizing the stub to
    "solve" click would buy a prettier number and put the Phase 0c gate's
    fixture at risk for it.
11. **§5.2's config dump must be written per run by the matrix too**, not
    only by the smoke script. It is the only artifact that says what the
    session loaded, and the P2 item to promote it into the record depends on
    it existing for real runs.

### Pass 3 — found while implementing

12. **`tests.p2p` was loaded and never read.** The manifest declared it, the
    dataclass carried it, and preflight computed "everything except f2p"
    regardless — a key that reads like a measurement and is inert, which is
    the same defect class as a schema field nothing populates. It is now used
    when non-empty, which is what a task needs if part of its suite is
    legitimately red at `base_sha`. Declaring a test as both f2p and p2p is a
    load error: no submission could satisfy both.

13. **A defect in this plan's own dirty-marker.** `task_set_commit` ran
    `git status --porcelain -- <path>` with the caller's *relative* path while
    running inside that same directory, so the pathspec matched nothing, git
    printed nothing and exited 0 — a task set with every file untracked
    reported clean. Caught by running it, not by reading it.

### Still open, deliberately

- **Linux host uid.** The per-run repo is created by the harness user and the
  container writes as uid 1000. On macOS virtiofs ignores ownership, which is
  why this has never bitten; on a Linux host it would. Pre-existing, not
  introduced here, and out of scope — recorded in TASKS.md.
- **`setuptools_scm`-style repos** need `.git` at image build time, and the
  build context is a `git archive` export without one. No such task exists
  yet; the failure is a loud build error, not a silent one.
- **A hand-written task Dockerfile** escape hatch. Deferred until a task
  needs it, because every escape hatch is a way back to the two structural
  failures the generator exists to make impossible.
