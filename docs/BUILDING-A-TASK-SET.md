# Building a task set on a new machine

How to stand the harness up on a laptop that has never run it, point it at a
corpus of your choosing, and cut that corpus into a task set the eval can
actually collect against.

This is the procedural companion to
[bakeoff/taskset/HARVESTING.md](../bakeoff/taskset/HARVESTING.md). That file is
the *specification* — what a candidate task must satisfy and why each rule
exists. This file is the *order of operations*, and it assumes you have read
neither the code nor the spec.

Two things worth knowing before you start:

- **The task set does not have to live inside this repo.** `--task-set` takes
  any directory. The corpus you harvest from does not have to be `pallets/*`
  either — the only thing tying a task to a repository is `repo.url` in its
  `task.yaml`.
- **Everything up to the live run is free.** Harvesting, image builds, the
  start-state materialization and the preflight gate need a Docker daemon and
  network, but no credentials and no spend. Do not skip them to save time; six
  of six early "model failures" in this project turned out to be harness or
  environment defects, and by the time a record exists the tokens are paid for.

---

## 0. What you are producing

A directory. One subdirectory per task, two files each:

```
my-taskset/                     # its own git repo — see step 1.5
  sqlglot-6421-parse-qualify/
    task.yaml                   # the manifest, spec §3.7
    reference.diff              # the merged PR, verbatim
  sqlglot-6588-window-frame/
    task.yaml
    reference.diff
  ...
```

`reference.diff` is split by path at load time into a **test half** (the
oracle, committed into the start state so the agent has a failing test to run)
and a **solution half** (the fix, which the agent never sees and which preflight
uses to prove the task is solvable). `task.yaml` says where that split falls,
how to run the tests, and which tests must change color.

---

## 1. Machine setup

### 1.1 Prerequisites

| thing | why |
|---|---|
| Docker daemon, running | every task image, every preflight, every run |
| Python ≥ 3.11 | `requires-python` in `bakeoff/pyproject.toml` |
| git | mirrors, diffs, the start-state commit |
| Network, first time per upstream repo | the mirror clone. Cached under `~/.cache/bakeoff` after that |

### 1.2 Clone and install

```bash
git clone <this-repo-url> && cd Pindrop-llm-eval/bakeoff
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Everything below is run **from `bakeoff/`** with `.venv/bin/python`. The
project's own instructions assume this and the scripts resolve paths relative
to it.

### 1.3 Prove the install before trusting it

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -v
```

Then the offline gate, which is the real one — unit suite, integration suite,
dry run and offline smoke test, no credentials, no spend:

```bash
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

It reports `GATE INCOMPLETE` and exits 1 without a Docker daemon rather than
passing a weaker gate under the same name. If you see that, fix Docker before
going further — a green-looking gate that skipped its integration leg tells you
nothing about the path you are about to use.

### 1.4 macOS: the `$HOME` constraint

On macOS the Docker VM mounts `$HOME` and **not** `/var/folders`. A repo
bind-mounted from outside `$HOME` appears inside the container as a *silently
empty directory* — the agent then edits nothing, the suite compares nothing
against nothing, and it is recorded as the model's failure.

Consequences:

- Keep your task set and any scratch trees under `$HOME`.
- Integration tests need `--basetemp` under `$HOME`:

  ```bash
  cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"
  ```

- The harness cache is `~/.cache/bakeoff` and is **not** configurable by flag
  on `run_matrix.py`. Mirrors, pruned mirrors, preflight verdicts, build
  context, wire logs and artifacts all land there. Budget disk accordingly: a
  bare mirror of a large repo plus its per-`base_sha` pruned copy is not small.

### 1.5 Make the task set a git repository

```bash
mkdir -p ~/my-taskset && cd ~/my-taskset && git init
```

Not optional in practice. Every record stores `task_set_commit`, which is what
makes a stored result re-derivable against the exact task set that produced it.
A non-git directory records `""`, and `run_matrix.py` prints `not a git repo` —
at which point your event log cannot say which version of the task it ran.
Uncommitted changes record as `-dirty`, which is honest but not reproducible.

**Commit the task set before every collection run.**

### 1.6 Credentials — only for the live step

Nothing until step 7 needs these. When you get there:

```bash
cp .env.example .env
```

Then read `.env.example` end to end. Two traps are called out in the file
itself and both have already cost debugging sessions:

- The bearer token variable **must** be `BAKEOFF_MANTLE_TOKEN`, never
  `AWS_BEARER_TOKEN_BEDROCK`. One proxy serves both transports and the variable
  name is the only thing keeping them apart; the AWS-named variable makes the
  proxy bearer-authenticate the SigV4 arms and fail them with
  `bedrock:CallWithBearerToken`, which reads as a bedrock-runtime problem and is
  not one.
- Pick a named profile **or** static keys, not both. botocore prefers the
  profile, so an `AWS_PROFILE` naming a profile that does not exist fails with
  `ProfileNotFound` even when the keys beside it are valid.

For the SSO path in this project's own config:

```bash
AWS_CONFIG_FILE=bakeoff/.aws/config aws sso login --profile pindrop-bakeoff
```

**That buys one hour, not eight.** Measured twice. The bearer token is presigned
with the SigV4 session, so it cannot outlive that session whatever its own TTL
says, and both credentials die at the same instant. The proxy starts once per
matrix invocation and Docker cannot change env on a running container, so
re-logging in mid-run changes nothing until the driver is re-invoked. Plan your
cells to fit inside the hour; the driver refuses a cell that will not and exits
2 so a re-invocation resumes.

---

## 2. Choosing a corpus

The screen is mechanical and cheap. Run it per candidate repository before
reading a single pull request.

```bash
docker run --rm -it -v "$PWD":/w -w /w python:3.12-slim-bookworm bash -c '
  apt-get update -qq && apt-get install -y -qq git >/dev/null &&
  git clone --depth 50 <repo-url> r && cd r &&
  pip install -q -e . && pip install -q pytest &&
  git clean -xfd && time python -m pytest -q -p no:cacheprovider ; git status --porcelain'
```

What you are looking for, and what each answer disqualifies:

| observation | verdict |
|---|---|
| suite green, under ~40 s | usable. Preflight runs it four times per task and the agent re-runs it inside its own timeout |
| suite green but slow (minutes) | expensive; every task from this repo pays it repeatedly |
| collection errors | usually one missing test dependency. Fixable by declaring it in `image.pip` — note which |
| `git status` dirty after the suite | the suite writes into the tree. Every submission diff then carries the droppings and diff size measures the interpreter rather than the agent. Fixable with `gitignore_extra` |
| needs a git submodule | **excluded.** The build context is `git archive base_sha`, which drops submodules; the directory arrives empty |
| `setuptools_scm` refuses to detect a version | needs a pretend-version in `build:`. `hatch-vcs` does not care |
| hypothesis / property-based suite | **excluded.** A property suite can pass a wrong fix on a lucky draw and fail a right one on an unlucky seed |
| `CLAUDE.md`, `AGENTS.md`, `.claude/` or `.cursorrules` present | either pick a `base_sha` predating them, or exclude the repo. A task-local agent file gives that task a context no other task has |

Then filter for supply: merged PRs that close an issue and carry both a test and
a fix. Repos with hundreds of those are worth the setup cost; repos with three
are not.

`bakeoff/taskset/HARVESTING.md` carries the already-screened list for this
project's corpus, with measured suite times and the reason each excluded repo is
out. Add your own rows to it as you screen — that table is the deliverable of
this step.

---

## 3. Cutting one task

Do this once, all the way to a green preflight, before harvesting in bulk. The
first task teaches you the failure modes; tasks two through eighty are
mechanical.

### 3.1 Pick the PR — read the tests first

Find a merged PR that closes an issue and carries both a test and a fix. Then
**open its test diff before anything else**, because the rule that disqualifies
most candidates is the one no gate can check:

> **The tests must assert behaviour, not internal names.**

Prefer tests comparing rendered output, return values, or raised messages. A
test that pins an arbitrary internal storage name scores a
different-but-correct fix as a failure — which is not a measurement of the
model. This project rejected `click` #3678 for exactly that.

While that diff is open, check its **import block** too — it costs one glance
and rejects a whole class of candidate:

> **Every name the test half imports must already exist at `merge_commit^1`.**

A PR that *adds* a function and tests for it puts that symbol in the solution
half, so the test module raises `ImportError` during collection and pytest
exits **2** — which preflight reads as a broken environment, not as a present
bug. There is no way to rescue such a task; the exit-1 requirement is exactly
the check that stops a broken environment counting as evidence the bug is
there. Measured on `trucking-doc-extraction` #3.

The heuristic that follows: **prefer a PR that changes the behaviour of an
existing symbol over one that adds a symbol.**

Also reject: tests that are flaky, tests that depend on wall-clock time or
network, and PRs whose "fix" is a version bump or a pure refactor.

### 3.2 Get the two SHAs

```bash
git clone <repo-url> src && cd src
git rev-parse <merge_commit>^1
```

That is your `base_sha`. **`merge_commit^1`, not the PR's recorded `base.sha`** —
the base branch advances between a PR being opened and merged, and diffing
against the stale one drags unrelated commits into your reference. Measured on
`click` #3434: `7c99ebe` → `63274a7`.

It must be the full 40 hex characters. Abbreviations and branch names both
resolve, and both resolve to something that can move.

### 3.3 Cut the reference diff — flags are load-bearing

```bash
git -c diff.noprefix=false -c diff.renames=true \
    diff --binary <base_sha> <merge_commit> > reference.diff
```

Without these three flags the diff's *shape* becomes a property of the
harvester's `~/.gitconfig` rather than of the task, and eighty references cut on
different laptops are not the same kind of object. Specifically:

- `diff.noprefix=false` — a `--no-prefix` diff is refused at load, because
  `git apply -p1` would strip a real path component and silently swap which
  files are the oracle.
- `diff.renames=true` — with `copies` enabled instead, plain modifications
  render as copy chunks.
- `--binary` — a plain diff renders a binary change as `Binary files … differ`
  with no payload. That loads clean and then fails in preflight with *"cannot
  apply binary patch without full index line"*.

Leave `core.quotepath` at its default. Quoted headers are never parsed for
paths, and turning it off writes raw non-UTF-8 bytes into the file, which fails
to decode at load.

The file must begin with `diff --git` and nothing before it. Strip any cover
letter.

### 3.4 Measure the f2p node ids — never guess them

Check out the base, apply only the test half, and run the suite. The tests that
fail are your `f2p` list, verbatim node ids:

```bash
git checkout --detach <base_sha>
git apply <(git diff <base_sha> <merge_commit> -- tests/)
python -m pytest -q -p no:cacheprovider tests/ 2>&1 | grep -E '^(FAILED|ERROR)'
```

Copy those ids exactly. Preflight asserts every declared id appears in pytest's
FAILED/ERROR lines at the start state, so a typo is caught — but a *missing* id
is not, and an f2p set narrower than the real one under-specifies the task.

`p2p` is normally left empty, meaning "everything else the runner collects".
If you do declare it, **list leaf node ids only** (`path::test_name`, never a
bare module or class). The grader's quarantine refusal compares declared ids
against quarantined ids and is exact only while every entry names one item; a
non-leaf entry makes that refusal fail open and the graded run exits 5.

### 3.5 Write `task.yaml`

Copy the annotated one at
[bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml](../bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml)
— its comments explain what every key has to satisfy. The skeleton:

```yaml
task_id: <repo>-<issue>-<slug>        # unique in the set; part of run_id
task_version: 1                       # bump on ANY edit — see §5
tier: B
stratum: small-edit

repo:
  url: https://github.com/<owner>/<repo>.git
  base_sha: <40 hex, = merge_commit^1>
  # start_sha: leave OUT on the first pass. Preflight computes and prints it.

prompt: |
  <issue title>

  <issue body, exactly as filed>

tests:
  paths: ["tests/"]                   # what splits reference.diff in two
  runner: ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
  f2p: ["tests/test_x.py::test_y"]    # measured in §3.4
  p2p: []                             # empty = everything else
  allow_extra_paths: ["CHANGES.rst"]  # files in NEITHER half

image:
  apt: []                             # OS packages the suite needs
  pip: ["pytest==8.3.5"]              # PINNED versions only
  build: ["pip install -e ."]         # EDITABLE — see below

# grading: build / typecheck / lint. Omit a key only with a comment
# saying why — see §4.

budget:
  max_turns: 40
  wall_clock_timeout_s: 900

provenance:
  repo: <owner>/<repo>
  issue: <n>
  pr: <n>
  merge_commit: <40 hex>
  merged_at: "YYYY-MM-DD"
  license: <SPDX id>
  session_id: null                    # Tier B: state the absence
  original_model: null
```

Three keys that bite:

- **`image.build` must install editable.** A plain `pip install .` resolves
  imports to site-packages, so nothing the agent writes to `/repo` takes
  effect — every arm fails identically and it reads as four weak models.
  Preflight's green-after check is what catches this.
- **`image.pip` versions are pinned, not floored.** An unpinned test runner or
  checker is a moving oracle: the same submission grades differently on two
  passes and the record cannot say which tool answered. If the repo's suite
  needs an older pytest than the base image ships, pin it here — `click` needs
  `pytest==8.3.5` because pytest 9 raises during collection under that repo's
  `filterwarnings = ["error"]`.
- **`prompt` is verbatim.** Issue title, then the body exactly as filed,
  including the reporter's template comments. Adding a harness-authored
  sentence — "the tests are in `tests/`", "do not edit the tests" — makes it a
  different task from the one a human actually opened. This eval measures the
  workflow that happens, not a tidied one.

`gitignore_extra` is a **top-level** key, not under `tests:`. Use it when the
suite drops files into the tree; it is applied in the setup commit and is
therefore visible in `start_sha`.

### 3.6 Run the gate

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only \
  --task-set ~/my-taskset --tasks <task_id>
```

The first run builds the base image, mirrors the upstream repo, builds the task
image and materializes the start state. It prints, among other things:

```
start_sha <40 hex>  (base <12 hex> + test half)
          pin it: repo.start_sha: <40 hex>
```

Paste that value into `repo.start_sha` and re-run. From then on, any re-cut
patch or edited manifest that moves the start state is a **load error** instead
of a silent change to what every arm was asked to do.

Iterate until it prints:

```
preflight PASS  f2p red at start, green after the reference; p2p green both ways; tree clean
```

Verdicts are cached per (manifest digest, image id, start sha, preflight
version), so re-running is cheap; `--force-preflight` re-runs anyway.

Drop `--tasks` to gate the whole set at once.

### 3.7 Reading a preflight refusal

Every check maps to a defect that would otherwise be recorded as model
capability.

| refusal | cause and remedy |
|---|---|
| `base_sha` does not resolve | wrong SHA, or shallow clone. `git fetch --unshallow` |
| `start_sha` pinned to X but materialization produced Y | the reference or manifest changed. Re-pin deliberately and bump `task_version` |
| nothing under `tests.paths` | `tests.paths` does not match where this repo keeps its tests |
| nothing left for the fix | every file landed in the test half or `allow_extra_paths` |
| a rename crosses the test/solution boundary | the file would be both the agent's oracle and part of its submission. Pick a different PR |
| f2p exits 2 / 4 / 5 at the start state | broken environment, not a present bug — missing dependency, collection error. Fix `image.pip` / `image.apt` |
| f2p exits 0 at the start state | the test half did not actually land, or the bug is already fixed at `base_sha` |
| a declared f2p id is not in FAILED/ERROR | typo, or the id changed shape (parametrization) |
| p2p not green at the start state | a regression check against an already-red suite means nothing. Usually a missing dependency |
| tree dirty after the suite | add `gitignore_extra` |
| `CLAUDE.md` / `AGENTS.md` / `.claude` / `.cursorrules` in the start state | pick a `base_sha` predating them, or drop the repo |
| image declares an `ENTRYPOINT` | the container's `sleep infinity` becomes an argument to it and the container exits immediately. Use a base without one |
| the solution half does not apply | the reference was cut against the wrong base |
| `tests.runner` does not contain `pytest` | the red/green distinction is built on pytest's exit codes and nothing else currently supplies it |

---

## 4. The judgment layer — what nothing checks

A task can pass every gate above and still measure the wrong thing. Walk this
list per task; it is where a task set goes wrong quietly.

- [ ] **Tests assert behaviour, not internal names.** The single most important
      call per candidate. Re-read §3.1 if you skipped it.
- [ ] **No property-based / hypothesis oracle.**
- [ ] **The suite is deterministic.** Preflight runs p2p twice; a flake makes the
      gate a coin flip and the eval unreproducible.
- [ ] **`p2p`, if declared, is leaf node ids only.**
- [ ] **A `grading:` block, or a comment per waived key.** `build`, `typecheck`
      and `lint` are checks 3, 4 and 7 of the grader's ladder. An absent key
      grades as `not_configured` — a named absence, not a pass — but an
      *unrecorded* absence is indistinguishable from an oversight, and a reader
      cannot tell a task that deliberately has no lint gate from one whose
      author forgot. The secret scan is check 8, has no manifest key, and is not
      waivable.
- [ ] **Any declared grading command is pinned in `image.pip` / `image.apt`** in
      the same edit that declares it. Otherwise waive it.
- [ ] **The licence permits redistributing the diff and the prompt.**
- [ ] **No human corrections replayed into the prompt.** A follow-up like "fix
      the import on line 40" was conditioned on the original model's output;
      against a different candidate it is incoherent.

---

## 5. Editing a task later

**Bump `task_version` on any edit.** It is not part of `run_id`, so an edited
task leaves every finished cell looking complete while mixing two different
tasks under one `task_id`. Resume hard-refuses on the mismatch, per cell — which
is the behaviour you want, and it only works if you bump.

Commit the task set afterwards, so `task_set_commit` moves with it.

---

## 6. Dry run — the whole orchestrator, no model calls

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --mode offline \
  --task-set ~/my-taskset --tasks <task_id> \
  --event-log ~/.cache/bakeoff/dryrun-log
```

This exercises container start, the agent subprocess, checkpoint capture,
trajectory parsing and the record write against a stand-in agent. Free. Do it
before spending anything.

**Read the `diff=` column, not just the exit code.** The stand-in agent edits
nothing, so every task should report `diff=0b`. Anything else means the tree
changed without an agent touching it, and preflight cannot catch that — its
tree-clean assertion covers what the *suite* writes, and this happens during
the container lifecycle instead. Measured on `trucking-doc-extraction` #2: a
committed virtualenv at `base_sha` (2,902 of 3,010 tracked files) turned a
no-op run into a **19 MB submission diff**, so diff size would have measured
`site-packages` rather than the model. That task passed preflight; this step is
what rejected it.

---

## 7. One live cell

Credentials first (§1.6), then:

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --mode live \
  --task-set ~/my-taskset --tasks <task_id> \
  --models claude-sonnet-5-runtime --repeats 1 \
  --event-log ~/.cache/bakeoff/<name>-log
```

Arms: `claude-sonnet-5-runtime`, `gemma-4-31b`, `nemotron-3-super-120b`,
`kimi-k2-5`. Omit `--models` for all four.

Exit codes: `0` every cell has a record; `1` something is wrong — preflight
refused, cells stranded, records that cannot be interpreted; `2` stopped early
and cleanly, work remains, **re-invoking resumes it**.

Two things to hold onto:

- **Use a distinct `--event-log` per experiment.** `run_id` is
  `sha256(task|model|sample|attempt)` — unique *within* a collection, not
  across one. The event log is append-only; the directory of event logs is not.
- The harness **does not grade**. `outcome` never becomes `RESOLVED` at run
  time and there is no `tests_passed` field in a record. Verdicts come from
  step 8.

---

## 8. Grade

Offline, over the stored diffs. Runs the nine-check ladder per submission and
appends one `GradeRecord` line per run to `<event-log>/grades/grades.jsonl` —
*beside* the log, never inside it, because a record is immutable and a verdict
is a derived view.

```bash
cd bakeoff && .venv/bin/python scripts/grade.py \
  --event-log ~/.cache/bakeoff/<name>-log --taskset ~/my-taskset
```

Note the flag is `--taskset` here and `--task-set` on `run_matrix.py`. Needs a
Docker daemon and the repo mirror; no credentials, no spend. Resumable — a run
already graded under the current `GRADER_VERSION` is skipped unless
`--re-grade`.

A re-grade under a different oracle, image or grader version writes a **new
line**, and its disagreement with the old one is a finding rather than a
correction.

---

## 9. Scaling to a set

Per-task correctness is necessary and not sufficient. The set has properties of
its own.

- **Harvest ~80 to land ~60.** Stratify to your *measured* production
  distribution — files touched per session, turn count, output tokens, tool-call
  mix, duration — not a guessed split. A 50/50 split on a workload that is 70%
  small edits measures the wrong thing and will not transfer to the bill.
- **Include a long-session stratum.** All three candidates claim 256K context
  and degrade unevenly well before it; small tasks never surface that.
- **Run a calibration pilot before freezing: all four models, N=3, every
  candidate.** Selecting tasks by one model's difficulty curve tilts the set
  toward what that model happens to discriminate on, which is the exact bias
  the eval exists to avoid.
- **The drop rule is model-neutral.** Drop only if **all four** score 0% (floor
  — usually underspecified rather than genuinely hard) or **all four** score
  100% (ceiling — no signal). **Keep every disagreement, however lopsided.** A
  task only one model solves is among the most informative in the set.
- **Read that rule against the measured variance.** On 2026-08-13 this
  project's single task went 4/4 resolved and then 2/4 on the same four arms
  hours apart, at temperature 1.0 and N=1 — between-run variance larger than the
  between-arm spread. At N=3 a discriminating task can present as a floor. That
  argues for more repeats in the pilot, not for a looser drop rule.

---

## 10. Failure modes that do not announce themselves

Collected because each one already cost a session somewhere, and none of them
raise.

| symptom | actual cause |
|---|---|
| agent edits nothing, suite compares nothing, run looks quiet | macOS: repo bind-mounted from outside `$HOME` — the container sees an empty directory (§1.4) |
| every arm fails identically on one task | `image.build` is not an editable install; the agent's edits never load |
| the agent cannot verify its own work | no test runner in the image, or the fixture is not importable. The eval measures a loop ending in "runs tests, sees failures, self-corrects"; without a runner it scores one unverified guess |
| a fix is applied, the source is correct on disk, pytest is still red | stale `.pyc`. CPython invalidates on (mtime in whole seconds, size) and both halves are ordinary — an operator swap preserves byte count, and an agent edits and re-runs inside one second. The image sets `PYTHONDONTWRITEBYTECODE=1`; do not remove it |
| a task passes preflight, then the dry run reports a huge `diff=` for an agent that edited nothing | a committed venv, build output or vendored tree tracked at `base_sha`. §5.6 stages everything, so it lands in every submission and diff size measures that tree. Preflight's tree-clean check only covers what the *suite* writes — screen with `git ls-tree -r --name-only <base_sha> \| wc -l` before cutting |
| a candidate PR's f2p exits 2 at the start state with `error during collection` | the test half imports a symbol the fix introduces. Not repairable — pick a PR that changes an existing symbol instead (§3.1) |
| turns, tokens and cost all zero in an otherwise fine record | a model name the price book does not know. `model_name` in `litellm_config.yaml` doubles as the `PRICE_BOOK` key |
| a run reads *"No deployments available"* at status `None` | expired credentials. litellm does not classify an expired AWS token as an auth error; it surfaces as 500, cools the deployment down, and the cooldown then hides the cause |
| every arm's submission fails to apply during grading | index staleness across the host/container boundary. Fixed in `grader._refresh_index`; if you see it again, that is a regression, not a model result |
| one arm's cells are all `turns=0` and permanent | the per-arm abort streak should have caught it. Records are written under mode `"x"` and `run_id` is deterministic, so those rows cannot be rewritten — start a new event log |

---

## Reference

| file | what it holds |
|---|---|
| [bakeoff/taskset/HARVESTING.md](../bakeoff/taskset/HARVESTING.md) | the three-layer spec for a candidate task, plus the screened repository list |
| [bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml](../bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml) | the worked example, annotated key by key |
| [bakeoff/src/bakeoff/tasks.py](../bakeoff/src/bakeoff/tasks.py) | the loader — every validation and the failure it prevents |
| [bakeoff/src/bakeoff/preflight.py](../bakeoff/src/bakeoff/preflight.py) | the per-task gate |
| [specs/2026-08-03-llm-bakeoff-eval-design.md](superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md) | the spec every `§N.N` above points at |
| [specs/2026-08-17-offline-grader-design.md](superpowers/specs/2026-08-17-offline-grader-design.md) | the nine-check ladder and the `GradeRecord` fields |
| [../CLAUDE.md](../CLAUDE.md) | the invariants and the config gotchas, in full |
