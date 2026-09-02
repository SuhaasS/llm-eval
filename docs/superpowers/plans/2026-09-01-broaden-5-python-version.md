# Broadening 5: a per-task Python version — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a manifest declare `image.python: "3.11"` so a repository whose suite needs an interpreter other than 3.12 can be cut as a task, instead of being refused by an eval base image that hard-codes `FROM python:3.12-slim-bookworm`.

**Architecture:** The base Dockerfile gains `ARG BASE_PYTHON_VERSION=3.12` before its `FROM`, so one file builds every allowed interpreter. `images.py` grows `base_tag(python_version)` and `build_base_images(repo_root, versions)`; the two drivers (`run_matrix.py`, `grade.py`) build the *set* of bases their task set needs — one build per distinct version, not per task — and hand each task the base its manifest names. The manifest key is validated at load time against a **closed allowlist** of versions someone has actually built. Preflight reads `python --version` back out of the container and refuses a mismatch (`PREFLIGHT_VERSION` +1). **No record field is added**: `Versions.container_image_digest` already pins the layer, and a `python_version` copied from the manifest would be configuration reported as observation.

**Tech Stack:** Python 3.12 (the harness venv), pytest, PyYAML, Docker (integration legs only). All code under `bakeoff/`, run with `bakeoff/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3 the self-correction loop, §3.7 the manifest, §5.1 the image, §5.4 what is held identical across arms). Shared broadening context: `.superpowers/broaden/CONTEXT.md` (this is broadening **#5**).

## Global Constraints

- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. Claims about external behaviour are annotated with what they were verified against (`Measured 2026-09-01, Docker 29.5.2`).
- **Do NOT edit `CLAUDE.md` on this branch.** Sentences that belong there are listed in the final section for later application.
- **`PREFLIGHT_VERSION` bumps by ONE from whatever value is on disk when Task 4 starts.** Broadenings 3 and 4 land before this one and each moves it. Read the constant, add one, do not hard-code a literal.
- **Do not depend on evidence keys or helpers that broadenings 3 and 4 add.** This plan adds exactly two evidence keys, `python_declared` and `python_observed`, and reads no others. It deliberately does **not** reuse broadening 3's `_runner_python` — see D5.
- **`GRADER_VERSION` and `ORACLE_VERSION` do NOT move.** Neither the ladder nor the quarantine derivation changes; the image id, which both already key on, moves on its own when a task's base changes.
- **`SCHEMA_VERSION` does NOT move.** No record field is added or changes meaning (D6). Broadenings 1–3 set the precedent: `git log --name-only` over them touches `schema.py` zero times.
- **Backwards compatibility:** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` loads unchanged, its `start_sha` does not move, and — measured, see M4 — the parameterised base at its default builds to the **byte-identical image id**, so the click task's stored `container_image_digest` and its cached preflight verdict both survive this change.
- **The gated argv and the graded argv stay byte-identical.** Nothing here touches a runner argv.
- **Tests pin every new invariant.** Unit tests in `bakeoff/tests/`; integration tests are marked `integration` (+ `task_image` where a task image is built).
- **Commit hygiene:** stage files explicitly (`git add <paths>`), never `git add -A`. Subject in the repo's style (`feat:`/`fix:`/`docs:` + a sentence saying what breaks without it). End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Baseline before starting: `cd bakeoff && .venv/bin/python -m pytest tests/ -q` → 1129 passed, 46 deselected, plus whatever broadenings 1–4 added.

---

## Measurements

Taken 2026-09-01 on this machine. Docker server **29.5.2**, **legacy builder** (`--progress` is rejected: *"unknown flag"*; buildx is not installed). Reproduce from the scratchpad copy at `/private/tmp/claude-501/.../scratchpad/eval-agent.param.Dockerfile`, which is `bakeoff/docker/eval-agent.Dockerfile` with its `FROM` line replaced by an `ARG`+`FROM` pair.

**M1 — the tags exist.** `docker manifest inspect python:<v>-slim-bookworm`: `3.11` EXISTS, `3.12` EXISTS, `3.13` EXISTS, `3.14` EXISTS.

**M2 — `ARG` before `FROM` works on the legacy builder, and the name `PYTHON_VERSION` is poisoned.** A minimal `ARG PYTHON_VERSION=3.12` / `FROM python:${PYTHON_VERSION}-slim-bookworm` builds with `--build-arg PYTHON_VERSION=3.11` and yields `Python 3.11.16`. But the official `python:` images set **their own `ENV PYTHON_VERSION`** — measured `3.11.16`, `3.12.13`, `3.13.15` — and `ENV` beats `ARG`, so *after* `FROM` the expansion resolves to the image's patch-level value, redeclaration or not:

```
after-FROM without redeclare: [3.11.16]
after-FROM WITH redeclare:    [3.11.16]     # ARG PYTHON_VERSION redeclared; ENV still won
```

With a collision-free name the redeclaration behaves as documented:

```
collision-free ARG after FROM: [3.11] vs image ENV [3.11.16]
```

**M3 — an unallowed version fails late and confusingly.** `--build-arg BASE_PYTHON_VERSION=3.99`:

```
Step 2/4 : FROM python:${BASE_PYTHON_VERSION}-slim-bookworm
failed to resolve reference "docker.io/library/python:3.99-slim-bookworm": docker.io/library/python:3.99-slim-bookworm: not found
```

A registry round-trip, network-dependent, after a build has started. This is the argument for a load-time closed allowlist (D2).

**M4 — the real eval Dockerfile builds on 3.11 and on 3.13, pytest 9.1.1 and Claude Code 2.1.220 install on both, and the 3.12 default is byte-identical to today's image.** Four builds of the real file (only the `FROM` line parameterised), then `docker inspect --format '{{.Id}}'` and a probe inside each:

| build | image id | `python --version` | `pytest --version` | `claude --version` | `id -u` | `PYTHONDONTWRITEBYTECODE` |
|---|---|---|---|---|---|---|
| `--build-arg PYTHON_VERSION=3.11` | `sha256:e670945930d073e6942eae8652ed896f64ac05d99e96e193c53ecf118381bddd` | `Python 3.11.16` | `pytest 9.1.1` | `2.1.220 (Claude Code)` | `1000` | `1` |
| `--build-arg PYTHON_VERSION=3.13` | `sha256:4eb69f78dc689ab980976b186ea488c5e4f2c3a112454d1e8703e1b1f0d78309` | `Python 3.13.15` | `pytest 9.1.1` | `2.1.220 (Claude Code)` | `1000` | `1` |
| parameterised, **no** `--build-arg` | `sha256:dfd2cc069bad6346465ec1ecfe0a704faac3cb0e86ddbcfbc424b60f9380915a` | `Python 3.12.13` | `pytest 9.1.1` | `2.1.220 (Claude Code)` | `1000` | `1` |
| **unmodified** `eval-agent.Dockerfile` | `sha256:dfd2cc069bad6346465ec1ecfe0a704faac3cb0e86ddbcfbc424b60f9380915a` | `Python 3.12.13` | `pytest 9.1.1` | `2.1.220 (Claude Code)` | `1000` | `1` |

The last two rows are the same id. Both in-Dockerfile pin assertions fired on every build (`pytest 9.1.1 pinned`, `claude 2.1.220 pinned`), so pytest 9.1.1 and the Claude Code installer both work on 3.11 and 3.13 — no per-version pytest pin is needed (D7).

**M5 — `python --version` writes to stdout, not stderr.** `docker run --rm <base-3.11> python --version 2>/dev/null` → `Python 3.11.16`; `... 2>&1 >/dev/null` → empty. So `container.exec([...]).stdout` is the right field. (Python 2 wrote it to stderr; 3.4+ does not.)

**M6 — the naive prefix comparison is wrong, and would be silently wrong.** `"3.13.15".startswith("3.1")` is `True`, and so is `"Python 3.13.15".startswith("Python 3.1")`. A read-back written as a `startswith` accepts 3.13 for a manifest declaring 3.1. Parse and compare major.minor (D5).

---

## Design decisions, settled

### D1. The key is `image.python`, a string, defaulting to `"3.12"`

It sits in `image:` beside `apt`, `pip`, `build` and `env` because it is the same kind of statement — "what this task's container has in it" — and `TaskImage` is the one place a reader already looks for one.

A **string**, never a float. YAML parses bare `3.11` as the float `3.11` and bare `3.10` as `3.1`, which is the same class of silent-wrong-value the `_ENV_VALUE_REFUSED` list one key over exists for. The loader therefore refuses a non-`str` outright with a message naming the quotes, rather than coercing.

`manifest_digest` needs no new term: it is `sha256(raw_manifest + raw_reference)` over the file's **bytes** (the `manifest_digest=` argument in `load_task`'s `TaskManifest(...)`), so declaring the key moves the digest by construction, and every preflight and oracle cache keyed on it invalidates on its own.

`start_sha` cannot move: the key takes no part in the setup commit that `materialize` builds. Asserted anyway (Task 1, Step 9), because "cannot move" is the claim, not the evidence.

### D2. A closed allowlist — `{"3.11", "3.12", "3.13"}` — enforced at load time in `tasks.py`

**Why closed.** An arbitrary string is a moving base in two different ways. `python:3-slim-bookworm` and `python:3.13-slim-bookworm` both float: the tag is republished, so two collections months apart run different interpreters under one manifest and nothing in the record says so — the same defect `image.pip`'s "pinned, not floored" rule exists to prevent, one key over. And an unbuildable string fails as M3 shows: a registry error, mid-build, on a machine that may simply be offline. An allowlist turns both into a `TaskError` with the manifest path in it, before any daemon is touched.

The three entries are exactly the three someone has built (M4). `3.14` is deliberately **not** in the set even though the tag exists (M1): nobody has built the eval image on it, and an entry that has not been measured is the allowlist claiming something it does not know.

**How it grows.** Three steps, all required, and the plan says so in the constant's own comment so the next author does not have to find this document:

1. Build the real base at that version — `docker build --build-arg BASE_PYTHON_VERSION=<v> -f docker/eval-agent.Dockerfile -t bakeoff-eval-agent:base-<v> .` — and confirm the two in-Dockerfile pin assertions fire (`pytest <pin> pinned`, `claude <pin> pinned`).
2. Add the string to `_PYTHON_VERSIONS`, with the measurement date beside it.
3. Add the row to `HARVESTING.md`'s allowlist table, and update the `docs/BUILDING-A-TASK-SET.md` screening command's note.

**Where it lives.** In `tasks.py`, beside `_IMAGE_ENV_ALLOWED`, because that is where every other manifest rule already is and because the refusal must be reachable with no daemon. `images.py` deliberately does **not** re-validate: a second copy of an allowlist is how a value accepted by one gate reaches a builder governed by another. `images.py` cannot import `tasks.py` at module scope anyway — `build_task_image` already imports `ensure_mirror` locally to avoid the cycle.

### D3. The base image becomes per-version, and `BASE_TAG` is deleted rather than aliased

`images.BASE_TAG = "bakeoff-eval-agent:base"` becomes a function, `base_tag(python_version) -> "bakeoff-eval-agent:base-3.11"`. Grep says the constant has exactly one non-test consumer (`build_base_image`'s default argument), so there is nothing to keep an alias for — and a stale `BASE_TAG` left in place is precisely how a caller goes on building the 3.12 base for a 3.11 task and gets an image that is wrong in a way no test can see (the tag resolves, the build succeeds, the suite runs under the wrong interpreter). `tests/test_runner_integration.py`'s `bakeoff-eval-agent` (no tag, i.e. `:latest`) is a different tag entirely and is untouched.

The Dockerfile ARG is named **`BASE_PYTHON_VERSION`, never `PYTHON_VERSION`** (M2). The current file does not reference the version after `FROM`, so today the collision is latent — but a later edit that wants to (an `ENV`, a version-conditional `pip` line, broadening 7's node install) would silently read `3.11.16` from the base image's own `ENV` instead of the build arg, redeclaration or not, and the value would look plausible. The comment in the Dockerfile records the measurement so nobody renames it back.

### D4. The drivers build the SET of bases, once per distinct version — and refuse a set whose bases disagree on Claude Code

`images.build_base_images(repo_root, versions) -> dict[str, str]` maps each distinct version string to an image id, building each exactly once. `run_matrix.main` and `grade.task_resolver` both switch to it; `resolve_tasks` takes the `bases` mapping instead of one `base_image` and indexes it by `task.image.python`.

`grade.task_resolver`'s cache already has the right shape — a `dict` with one hard-coded `"base"` key inside `task_resolver` — and becomes a dict keyed by version, which preserves the property its docstring claims: a batch whose records are all gated still builds nothing.

**What stops being checked, precisely.** `run_matrix` today probes `claude --version` out of the one base and passes it to preflight as `expected_claude_version`, whose refusal says *"two tasks would run different agents and the comparison across them is not one"*. Passing each task its **own** base's version keeps the useful half of that check — it still catches a task image whose `image.pip`/`image.build` clobbered `claude` — and loses exactly one thing: **cross-base agreement**. Two bases carrying different Claude Code builds would then pass every per-task gate and be invisible in the record, since `Versions.claude_code` is read from the transcript, per run, and nothing compares it across tasks. So `run_matrix` probes **every** base and refuses the whole invocation when they do not all agree.

**The rejected alternative** is to probe one base and pass that single string to every task, which also restores cross-base agreement. It is rejected because it makes one base arbitrarily canonical — the answer depends on which version happens to sort first — and because it converts a build-level defect into a per-task preflight failure late in the run, whereas `assert_one_agent` fails before the first task image is built. Cost: one `docker run` per distinct version, offline, before the proxy starts.

An empty probe is a refusal, not agreement. `base_claude_version` returns `""` when the `docker run` exits non-zero, so "every base failed to answer" collapses to the single value `""` — and preflight's guard is `elif expected_claude_version and ...`, which is **falsy on `""`**, silently disabling the agent-version check for every task in the matrix. `assert_one_agent` therefore raises when *any* probed version is empty, before it looks at whether they agree.

In practice all bases always agree: one `ARG CLAUDE_CODE_VERSION` in one file. That is exactly why an unchecked divergence would be assumed away rather than noticed.

**Interface note for broadening 7 (a node base image).** `base_tag` and `build_base_images` are named for *versions*, not for Python: when node joins, `base_tag` gains a second keyword and the mapping key becomes whatever tuple identifies a base, and those two functions are the only places that change. That is a naming choice, not a hook — nothing is built for it here.

### D5. Preflight reads `python --version` back and refuses a mismatch; `PREFLIGHT_VERSION` +1

Two evidence keys, `python_declared` and `python_observed`, written beside the existing environment checks. `python_observed` carries the **full** string (`"Python 3.11.16"`) rather than the parsed pair, because the patch level is information the manifest cannot express and the evidence file is what a human reads afterward. (It outlives the *run*, not the next preflight — `run_matrix` rewrites `preflight/<task_id>.json` on every invocation — so it answers "what did the gate see this time", which is the question a mismatch raises.)

**Which interpreter.** Plain `python` on `PATH`, and this is a decision rather than a default. `image.python` is a claim about the **base image's** interpreter, and `python` is what the `ARG` selects; a task whose `tests.runner` names `/opt/venv/bin/python` is talking about a different environment the key makes no claim about. So this plan does **not** reuse broadening 3's `_runner_python` helper — it answers a different question, and reusing it would make the gate assert something the manifest never said. (It also keeps this task independent of broadening 3's in-flight churn in the same file.)

**How they are compared.** Parse `Python X.Y.Z` and compare `X.Y` for **equality**. Not `startswith` (M6: `"3.13.15".startswith("3.1")` is `True`), and not equality against the whole string (the manifest carries no patch level and must not have to).

The problem message names the actual remedy, which is a rebuild rather than a manifest edit: the base for this version may be stale or the task image may have been built against the wrong base.

`PREFLIGHT_VERSION` moves because a verdict cached under the old gate was written by one that never looked at the interpreter at all — the entire point of that constant being in the cache key.

**Why the read-back is not redundant with "we passed the right `--build-arg`."** The base tag is mutable and local: an operator with a stale `bakeoff-eval-agent:base-3.11` from an earlier Dockerfile, a task image built against the wrong entry of the `bases` map, or an `image.build` step that puts a different `python` earlier on `PATH` all leave every rendering and unit test green. And the failure is the worst-shaped one this repo knows: the suite runs, the gate is green, and the interpreter is not the one the task was cut for.

### D6. No record field. `Versions.container_image_digest` plus the manifest is the join, and there is no cheap run-time read

Adding `Versions.python_version` copied from `task.image.python` would be configuration reported as observation — the thing `sampling`, `isolated` and `bedrock_model_id` are each shaped to avoid. It would be an *observation* only if read out of the run container.

There is no free read to take it from. `execute_run` makes **no** `claude --version` exec — `Versions.claude_code` comes from the transcript (`claude_code=parsed.claude_code_version` in `runner`'s `Versions(...)`), and the only `claude --version` execs in the repo are preflight's and `run_matrix.base_claude_version`'s base probe, neither on the run path. Adding one would put a new exec inside the stretch guarded by "a run always produces a record", to re-derive something the image id already pins, for every cell of the matrix.

So the join is the one broadening 3 made for `image.env` and the same one that already carries `apt`, `pip` and `build`, and it is worth naming the fields exactly because `RunRecord` does **not** carry `manifest_digest`:

- **On the record:** `Versions.container_image_digest` pins the layer the run executed in, and `task_id` + `Versions.task_set_commit` (both populated in `matrix.to_task_spec`) name the `task.yaml` revision that declared `image.python`. A reader goes record → task set at that commit → manifest.
- **Beside it:** `preflight_cache_key` is `(manifest_digest, image, start_sha, PREFLIGHT_VERSION)`, and the evidence file that key gates carries the measured `python_observed`.

`SCHEMA_VERSION` therefore does not move, and Task 1 asserts that no `schema.py` change is needed rather than leaving it as an assumption.

### D7. One pytest pin for every version — measured, not assumed

The base pins pytest 9.1.1. M4 shows the in-Dockerfile assertion `pytest 9.1.1 pinned` firing on 3.11 and on 3.13, so no per-version pin is needed and none is added. Task pins (`image.pip`) still win, unchanged — `click` needs `pytest==8.3.5` and gets it on whatever base it names.

Should a future allowlist entry ever fail this, the Dockerfile's existing `case` guard already **fails the build** rather than shipping a floating runner, which is the right failure: loud, at build time, before anything is gated.

---

## File structure

| File | Change |
|---|---|
| `bakeoff/docker/eval-agent.Dockerfile` | `ARG BASE_PYTHON_VERSION=3.12` before `FROM`; `FROM python:${BASE_PYTHON_VERSION}-slim-bookworm`; comment recording M2 |
| `bakeoff/src/bakeoff/tasks.py` | `_PYTHON_VERSIONS` allowlist, `_python_version()` validator, `TaskImage.python`, wired in `load_task`'s `TaskImage(...)` |
| `bakeoff/src/bakeoff/images.py` | delete `BASE_TAG`; add `base_tag()`, `build_base_images()`; `build_base_image()` gains `python_version` |
| `bakeoff/scripts/run_matrix.py` | build the base *set*; refuse disagreeing Claude versions; `resolve_tasks` takes `bases` |
| `bakeoff/scripts/grade.py` | `task_resolver`'s cache keyed by version |
| `bakeoff/src/bakeoff/preflight.py` | `_declared_python()`, `_observed_python()`, the read-back check, two evidence keys, `PREFLIGHT_VERSION` +1 |
| `bakeoff/tests/test_tasks.py` | the manifest key: default, allowlist, type refusal, click unchanged |
| `bakeoff/tests/test_images.py` | `base_tag`, the `--build-arg`, one build per distinct version |
| `bakeoff/tests/test_run_matrix.py` | the base-set mapping and the Claude-version refusal |
| `bakeoff/tests/test_preflight.py` | the read-back, the parse, the refusal; the 3.11 integration leg |
| `bakeoff/tests/test_integration_grader.py` | `build_base_image(REPO_ROOT)` — the third caller; unchanged source, new tag |
| `bakeoff/taskset/HARVESTING.md`, `docs/BUILDING-A-TASK-SET.md`, click `task.yaml`, `tasks/todo.md`, `TASKS.md` | docs |

**Line numbers are deliberately absent from the task bodies.** Broadenings 3 and 4 land first and both edit `preflight.py` (broadening 4 also changes `resolve_tasks`'s signature and `main`'s `preflight(...)` call), so every offset in this plan would be stale on arrival. Anchor on symbol names: `PREFLIGHT_VERSION`, `_declared_env`, `_IMAGE_ENV_ALLOWED`, `TaskImage`, `task_resolver`, `resolve_tasks`, `main`.

**Three callers of `build_base_image`, not two.** `run_matrix.main`, `grade.task_resolver`, and `tests/test_integration_grader.py::click_image` (`build_base_image(REPO_ROOT)`). The third needs **no source change** — the `python_version` parameter defaults — but it now tags `bakeoff-eval-agent:base-3.12` instead of `bakeoff-eval-agent:base`. The old `:base` tag is left orphaned on any machine that has one, pointing at the same image id (measured: `dfd2cc069bad`), so it is stale rather than wrong; nothing reads it after Task 2.

**Deliberately unchanged, and each for its own reason:**

| Script | Why it is not touched |
|---|---|
| `scripts/smoke_test.py` | builds `docker/eval-agent.Dockerfile` itself under `AGENT_TAG = "bakeoff-eval-agent:smoke"` with no `--build-arg`, so it gets the Dockerfile's `ARG` default — which is the version it wants. It runs one fixture task, not a task set, and has no manifest to read `image.python` from. |
| `scripts/verify_logger.py` | builds nothing; it invokes the other scripts and the pytest selections. |
| `scripts/dry_run.py` | builds a stand-in agent `FROM alpine/git:latest` with a fake `claude`. No Python base, no manifest. |

Task order is load-time → build-time → driver → gate → docs, so each task's tests pass against the tree the previous one left.

---

## Task 1: the manifest key and its closed allowlist

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` (`_IMAGE_ENV_ALLOWED`; `TaskImage`; the `TaskImage(...)` construction inside `load_task`)
- Test: `bakeoff/tests/test_tasks.py` (new section after the `# --- image.env ---` group)

**Interfaces:**
- Produces: `bakeoff.tasks._PYTHON_VERSIONS: frozenset[str]`; `TaskImage.python: str` (default `"3.12"`); `bakeoff.tasks._python_version(value, where) -> str`.
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_tasks.py`:

```python
# --- image.python -------------------------------------------------------------


def test_image_python_defaults_to_the_base_images_version(tmp_path, upstream):
    """Every manifest written before this key existed must load unchanged, and
    the default has to be the version the base Dockerfile's own ARG default
    builds -- two defaults that can drift is one manifest loading as a task
    whose image nobody built."""
    task_dir = _write_task(tmp_path / "set", upstream)  # no image: block

    assert load_task(task_dir).image.python == "3.12"


def test_an_allowlisted_version_is_carried_verbatim(tmp_path, upstream):
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  python: "3.11"\n'
    )

    assert load_task(task_dir).image.python == "3.11"


def test_a_version_nobody_built_is_refused_at_load_time(tmp_path, upstream):
    """Measured 2026-09-01: `--build-arg BASE_PYTHON_VERSION=3.99` fails with
    `failed to resolve reference "docker.io/library/python:3.99-slim-bookworm"
    ... not found` -- a registry round-trip, mid-build, on a machine that may
    be offline. The allowlist turns that into a message naming the manifest."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  python: "3.99"\n'
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "3.99" in str(excinfo.value)
    assert "3.12" in str(excinfo.value)  # the allowlist is in the message


def test_a_floating_major_version_is_refused(tmp_path, upstream):
    """`python:3-slim-bookworm` resolves and is republished, so two collections
    months apart run different interpreters under one manifest and no record
    says so. Same defect `image.pip`'s "pinned, not floored" rule prevents."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='image:\n  python: "3"\n'
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


def test_an_unquoted_version_is_refused_rather_than_coerced(tmp_path, upstream):
    """YAML parses bare `3.11` as a float and bare `3.10` as `3.1`. Coercing
    with str() would turn the second into a version nobody named; the refusal
    names the quotes."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml="image:\n  python: 3.11\n"
    )

    with pytest.raises(TaskError) as excinfo:
        load_task(task_dir)

    assert "quote" in str(excinfo.value).lower()


def test_the_worked_example_task_still_loads_on_the_default(tmp_path):
    """Backwards compatibility, stated over the real manifest rather than a
    fixture: click declares no `python:` and must keep the base it has."""
    task = load_task(
        Path(__file__).resolve().parent.parent
        / "taskset" / "click-3360-write-usage-empty-args"
    )

    assert task.image.python == "3.12"
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -k image_python -q
```

Expected: FAIL — `AttributeError: 'TaskImage' object has no attribute 'python'`.

- [ ] **Step 3: Add the allowlist and the validator**

In `bakeoff/src/bakeoff/tasks.py`, immediately after `_IMAGE_ENV_ALLOWED`:

```python
#: Python versions the base Dockerfile is KNOWN to build, because someone
#: built it. A closed set rather than a free string, for two reasons that are
#: each silent without it:
#:
#:   A floating tag is a moving base. `python:3-slim-bookworm` and
#:   `python:3.13-slim-bookworm` are both republished, so two collections
#:   months apart run different interpreters under one manifest and nothing in
#:   the record says which -- the defect `image.pip`'s "pinned, not floored"
#:   rule exists to prevent, one key over.
#:
#:   An unbuildable string fails LATE. Measured 2026-09-01, Docker 29.5.2:
#:   `--build-arg BASE_PYTHON_VERSION=3.99` gets `failed to resolve reference
#:   "docker.io/library/python:3.99-slim-bookworm": ... not found` at the FROM,
#:   after a build has started and over the network. Here it is a TaskError
#:   with the manifest path in it, before any daemon is touched.
#:
#: Each entry was measured by building `docker/eval-agent.Dockerfile` at that
#: version and confirming BOTH in-Dockerfile pin assertions fired -- `pytest
#: 9.1.1 pinned` and `claude 2.1.220 pinned`. Verified 2026-09-01 for all
#: three; 3.11 gave Python 3.11.16, 3.12 gave 3.12.13, 3.13 gave 3.13.15.
#:
#: TO ADD A VERSION, all three steps: build the real base at it and confirm
#: both assertions fire; add the string here with the date; add the row to
#: taskset/HARVESTING.md's table and the note in docs/BUILDING-A-TASK-SET.md.
#: `python:3.14-slim-bookworm` exists and is deliberately absent -- nobody has
#: built the eval image on it, and an unmeasured entry is this constant
#: claiming something it does not know.
_PYTHON_VERSIONS = frozenset({"3.11", "3.12", "3.13"})

#: The base Dockerfile's own `ARG BASE_PYTHON_VERSION` default, restated here
#: as `TaskImage.python`'s default. Pinned equal by
#: `tests/test_images.py::test_the_dockerfile_default_matches_the_manifest_default`
#: -- two defaults that can drift means a manifest declaring nothing loads as a
#: task whose base was never built.
_DEFAULT_PYTHON = "3.12"


def _python_version(value: Any, where: str) -> str:
    """`image.python`, validated. The default when absent.

    A `str` is REQUIRED, never coerced. YAML parses an unquoted `3.11` as the
    float 3.11 and an unquoted `3.10` as `3.1` -- so `str(value)` would turn a
    manifest asking for 3.10 into one asking for a version that is not in the
    allowlist at all, or (had 3.1 been listed) into a silently different
    interpreter. Same class as `_ENV_VALUE_REFUSED`'s `$`: a value that is a
    property of the parser rather than of the manifest.
    """
    if value is None:
        return _DEFAULT_PYTHON
    if not isinstance(value, str):
        raise TaskError(
            f"{where}: {value!r} is {type(value).__name__}, not a string -- "
            "quote it (`python: \"3.11\"`). YAML reads an unquoted 3.11 as a "
            "float and an unquoted 3.10 as 3.1, so the version that reaches "
            "the build is not the one the manifest names"
        )
    if value not in _PYTHON_VERSIONS:
        raise TaskError(
            f"{where}: {value!r} is not a Python version this base image is "
            f"known to build. Allowed: {sorted(_PYTHON_VERSIONS)}. A version "
            "outside the set is either a floating tag -- two collections "
            "months apart running different interpreters under one manifest, "
            "with nothing in the record saying so -- or one that fails at the "
            "FROM with a registry error mid-build. To add one, build "
            "docker/eval-agent.Dockerfile with "
            "`--build-arg BASE_PYTHON_VERSION=<v>`, confirm the pytest and "
            "claude pin assertions fire, then add it to _PYTHON_VERSIONS and "
            "to taskset/HARVESTING.md"
        )
    return value
```

- [ ] **Step 4: Add the field and wire it**

In `TaskImage`, after `env`:

```python
    #: The base image's Python. Every ARM of a task runs the same base, so
    #: this is a per-task property and not a section 5.4 divergence -- what
    #: that section holds identical is the environment two ARMS are compared
    #: in, and a task is compared against itself.
    #:
    #: Validated against `_PYTHON_VERSIONS` at load time. It selects which
    #: base image the drivers build and hand to `build_task_image`; preflight
    #: reads `python --version` back out of the finished container and refuses
    #: a mismatch, because the tag is mutable and local and a stale one leaves
    #: every unit test green while the suite runs under the wrong interpreter.
    python: str = "3.12"
```

In `load_task`'s `TaskImage(...)` construction, add:

```python
            python=_python_version(image_raw.get("python"), f"{where}:image.python"),
```

- [ ] **Step 5: Run the tests**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -k image_python -q
```

Expected: PASS (6 tests).

- [ ] **Step 6: Pin the two defaults to each other**

The literal `"3.12"` now appears in `TaskImage.python`'s default and in `_DEFAULT_PYTHON`. Make the dataclass default reference the constant instead — move `_DEFAULT_PYTHON` above `TaskImage` and write `python: str = _DEFAULT_PYTHON` — so there is one literal, not two. Re-run Step 5.

- [ ] **Step 7: Confirm no schema change is needed**

```bash
cd bakeoff && grep -n "python" src/bakeoff/schema.py | grep -iv "pythondontwrite"
```

Expected: no `Versions` field mentions Python. Per D6 nothing is added; `SCHEMA_VERSION` stays where it is. Record the check in the commit body.

- [ ] **Step 8: Full suite, and the digest/start_sha assertions**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Expected: baseline + 6, 0 failed.

- [ ] **Step 9: Prove `start_sha` and `manifest_digest` behave**

Append to `bakeoff/tests/test_tasks.py`:

```python
def test_declaring_a_python_version_moves_the_digest_but_not_the_start_state(
    tmp_path, upstream
):
    """`manifest_digest` hashes the manifest BYTES, so the key participates
    with no term of its own and every preflight and oracle cache keyed on it
    invalidates. `start_sha` must NOT move: the key takes no part in the setup
    commit, and a task whose start state moved is a different task."""
    plain = load_task(_write_task(tmp_path / "a", upstream))
    pinned = load_task(
        _write_task(tmp_path / "b", upstream,
                    extra_yaml='image:\n  python: "3.11"\n')
    )

    assert pinned.manifest_digest != plain.manifest_digest
    assert materialize(pinned, tmp_path / "tb" / "repo", tmp_path / "cb") == \
        materialize(plain, tmp_path / "ta" / "repo", tmp_path / "ca")
```

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -k python -q` → PASS.

- [ ] **Step 10: Commit**

```bash
cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py
git commit -m "$(cat <<'EOF'
feat: a manifest can pin its Python, on a closed set someone has actually built

A repository whose suite needs an interpreter other than 3.12 could not be
cut as a task at all: the base image hard-codes FROM python:3.12-slim-bookworm
and nothing in the manifest could say otherwise.

The set is CLOSED, and refused at load time rather than at build time, for
two reasons that are each silent without it. A floating tag is a moving base
-- python:3-slim-bookworm and python:3.13-slim-bookworm are both republished,
so two collections months apart run different interpreters under one manifest
and no record says which. And an unbuildable string fails late: measured
2026-09-01 on Docker 29.5.2, --build-arg BASE_PYTHON_VERSION=3.99 gets
`failed to resolve reference "docker.io/library/python:3.99-slim-bookworm":
not found` at the FROM, over the network, after a build has started.

The value must be a quoted string and is never coerced. YAML reads an
unquoted 3.11 as a float and an unquoted 3.10 as 3.1, so str() would send a
version the manifest never named to the builder.

3.11, 3.12 and 3.13 are in because the real Dockerfile was built at each and
both in-image pin assertions fired. 3.14 exists upstream and is out, because
nobody has built it.

No record field: Versions.container_image_digest already pins the layer and
manifest_digest pins the manifest that produced it, so SCHEMA_VERSION does
not move -- a python_version copied from the manifest would be configuration
reported as observation.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: the base image builds at any allowed version

**Files:**
- Modify: `bakeoff/docker/eval-agent.Dockerfile` (the single `FROM python:3.12-slim-bookworm` line)
- Modify: `bakeoff/src/bakeoff/images.py` (`BASE_TAG`, `build_base_image`)
- Test: `bakeoff/tests/test_images.py`

**Interfaces:**
- Consumes: `bakeoff.tasks._PYTHON_VERSIONS`, `_DEFAULT_PYTHON` (Task 1).
- Produces:
  - `bakeoff.images.base_tag(python_version: str) -> str` → `"bakeoff-eval-agent:base-3.11"`
  - `bakeoff.images.build_base_image(repo_root: Path, python_version: str = _DEFAULT_PYTHON) -> str` (image ID)
  - `bakeoff.images.build_base_images(repo_root: Path, versions: Iterable[str]) -> dict[str, str]` (version → image ID)
  - `bakeoff.images.BASE_TAG` is **removed**.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_images.py`:

```python
# --- the parameterised base image ---------------------------------------------


def test_each_version_gets_its_own_tag():
    """One tag for two interpreters means the second build silently replaces
    the first, and every task afterwards resolves the same tag to the wrong
    base -- which builds, runs, and is green."""
    assert images.base_tag("3.11") == "bakeoff-eval-agent:base-3.11"
    assert images.base_tag("3.12") != images.base_tag("3.11")


def test_build_base_image_passes_the_version_as_a_build_arg(monkeypatch):
    calls = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args) or "")
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:" + tag)

    images.build_base_image(Path("/repo_root"), "3.11")

    argv = calls[0]
    assert "--build-arg" in argv
    assert argv[argv.index("--build-arg") + 1] == "BASE_PYTHON_VERSION=3.11"
    assert "-t" in argv and argv[argv.index("-t") + 1] == \
        "bakeoff-eval-agent:base-3.11"


def test_the_arg_is_not_named_PYTHON_VERSION(monkeypatch):
    """Measured 2026-09-01: the official python: images set their own
    ENV PYTHON_VERSION (3.11.16, 3.12.13, 3.13.15) and ENV beats ARG, so after
    FROM the expansion reads the base image's patch level -- even after the ARG
    is redeclared. The current file never expands it after FROM, so today the
    collision is latent; a later edit that does would read a plausible wrong
    value. The name is the whole defence."""
    calls = []
    monkeypatch.setattr(images, "_run", lambda args, cwd=None: calls.append(args) or "")
    monkeypatch.setattr(images, "image_id", lambda tag: "sha256:" + tag)

    images.build_base_image(Path("/repo_root"), "3.13")

    assert "PYTHON_VERSION=3.13" not in calls[0]


def test_one_build_per_distinct_version_not_per_request(monkeypatch):
    """The drivers call this with one entry per TASK. Building per task pays
    an image build for every duplicate, and on a 60-task set that is the
    difference between a preflight pass and one nobody waits for."""
    built = []
    monkeypatch.setattr(
        images, "build_base_image",
        lambda root, version: built.append(version) or ("sha256:" + version),
    )

    bases = images.build_base_images(Path("/r"), ["3.12", "3.11", "3.12", "3.12"])

    assert sorted(built) == ["3.11", "3.12"]
    assert bases == {"3.11": "sha256:3.11", "3.12": "sha256:3.12"}


def test_every_copy_of_the_default_version_says_the_same_thing():
    """THREE copies of "3.12" exist by the end of this broadening -- the
    Dockerfile's ARG default, `tasks._DEFAULT_PYTHON` (which is
    `TaskImage.python`'s default), and `images._DEFAULT_PYTHON` (restated
    because this module cannot import `tasks` at module scope). Two that can
    drift means a manifest declaring no `python:` loads as a task whose base
    nobody built -- the build succeeds, the suite runs, and the interpreter is
    not the one the default named.

    `preflight` is deliberately NOT a fourth copy: it imports
    `_DEFAULT_PYTHON` from `tasks` (Task 4)."""
    from bakeoff.tasks import _DEFAULT_PYTHON as manifest_default

    dockerfile = (
        Path(__file__).resolve().parent.parent / "docker" / "eval-agent.Dockerfile"
    ).read_text()

    assert images._DEFAULT_PYTHON == manifest_default
    assert f"ARG BASE_PYTHON_VERSION={manifest_default}\n" in dockerfile
    assert "FROM python:${BASE_PYTHON_VERSION}-slim-bookworm\n" in dockerfile


def test_every_allowlisted_version_is_a_tag_this_module_can_name():
    from bakeoff.tasks import _PYTHON_VERSIONS

    tags = {images.base_tag(v) for v in _PYTHON_VERSIONS}

    assert len(tags) == len(_PYTHON_VERSIONS)
```

Add `from pathlib import Path` to the file's imports if it is not already there.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -k "base_tag or base_image or default_version or allowlisted" -q
```

Expected: FAIL — `AttributeError: module 'bakeoff.images' has no attribute 'base_tag'`.

- [ ] **Step 3: Parameterise the Dockerfile**

In `bakeoff/docker/eval-agent.Dockerfile`, replace the `FROM python:3.12-slim-bookworm` line with:

```dockerfile
# The interpreter, per task. `image.python` in a manifest selects which of
# these bases the drivers build and hand to `build_task_image`; every ARM of
# a task runs the same one, so this is not a section 5.4 divergence -- what
# that section holds identical is the environment two arms are compared in.
#
# NOT named PYTHON_VERSION, and the name is load-bearing. Measured 2026-09-01
# (Docker 29.5.2, legacy builder): the official python: images set their own
# `ENV PYTHON_VERSION` -- 3.11.16, 3.12.13, 3.13.15 -- and ENV beats ARG, so
# after this FROM the expansion resolves to the base image's PATCH level and
# not to the build arg, whether or not the ARG is redeclared:
#
#   after-FROM without redeclare: [3.11.16]
#   after-FROM WITH redeclare:    [3.11.16]
#
# Nothing below expands it, so today that is latent. A later edit that wants
# to -- a version-conditional pip line, an ENV, broadening 7's node install --
# would read a plausible wrong value with no error. Under this name the
# redeclaration behaves: `[3.11] vs image ENV [3.11.16]`.
#
# The default matches `bakeoff.tasks._DEFAULT_PYTHON` and is pinned equal by
# tests/test_images.py. Measured: with no --build-arg this file builds to the
# byte-identical image id it built before the ARG existed
# (sha256:dfd2cc069bad6346465ec1ecfe0a704faac3cb0e86ddbcfbc424b60f9380915a),
# so no stored record's container_image_digest and no cached preflight verdict
# moves.
ARG BASE_PYTHON_VERSION=3.12
FROM python:${BASE_PYTHON_VERSION}-slim-bookworm
```

Leave the pytest and Claude Code pins alone: measured on 3.11 and 3.13, both assertions fire (D7).

- [ ] **Step 4: Replace `BASE_TAG` with `base_tag` and add the two builders**

In `bakeoff/src/bakeoff/images.py`, delete the `BASE_TAG = "bakeoff-eval-agent:base"` constant and put in its place:

```python
def base_tag(python_version: str) -> str:
    """The local tag for one base image.

    Per VERSION, because one tag for two interpreters means the second build
    silently replaces the first and every task afterwards resolves that tag to
    the wrong base. That failure builds, runs and goes green: the suite is
    executed by an interpreter the task was not cut for, and only preflight's
    `python --version` read-back says so.

    Named for the version rather than for Python. When broadening 7 adds a
    node base, this function and `build_base_images` are the only two places
    that learn about a second axis.
    """
    return f"bakeoff-eval-agent:base-{python_version}"
```

Rewrite `build_base_image`:

```python
def build_base_image(repo_root: Path, python_version: str = _DEFAULT_PYTHON) -> str:
    """Build docker/eval-agent.Dockerfile at one Python and return its image ID.

    The version reaches the build as `--build-arg BASE_PYTHON_VERSION`, which
    is what the Dockerfile's pre-FROM ARG consumes. See the comment there for
    why that name and not `PYTHON_VERSION`.

    No allowlist check here. `tasks._python_version` is the one gate, at load
    time with no daemon; a second copy of the set is how a value one gate
    accepted reaches a builder governed by another. This module cannot import
    `tasks` at module scope in any case -- `build_task_image` already imports
    `ensure_mirror` locally to avoid the cycle.
    """
    tag = base_tag(python_version)
    _run(
        [
            "docker", "build", "-q",
            "--build-arg", f"BASE_PYTHON_VERSION={python_version}",
            "-f", str(Path(repo_root) / "docker" / "eval-agent.Dockerfile"),
            "-t", tag, str(repo_root),
        ]
    )
    return image_id(tag)


def build_base_images(repo_root: Path, versions) -> dict[str, str]:
    """Every base a task set needs, built once each, keyed by version.

    Callers pass one entry per TASK; this deduplicates. Building per task pays
    a full image build for every duplicate, and on a 60-task set that turns
    the free offline half of `--preflight-only` into something nobody waits
    for.

    Sorted, so a build log reads the same way twice and a failure names the
    same version first.
    """
    return {
        version: build_base_image(repo_root, version)
        for version in sorted(set(versions))
    }
```

Add near the top of the module, beside the imports:

```python
#: Restated rather than imported: `build_task_image` already imports `tasks`
#: locally rather than at module scope, and a module-scope import here would
#: undo that. Pinned equal to `tasks._DEFAULT_PYTHON` AND to the Dockerfile's
#: `ARG BASE_PYTHON_VERSION` default by
#: tests/test_images.py::test_every_copy_of_the_default_version_says_the_same_thing
#: -- three copies that can drift is a manifest declaring nothing loading as a
#: task whose base nobody built.
_DEFAULT_PYTHON = "3.12"
```

Extend the module docstring's "the BASE image" paragraph with one sentence: the base is now per Python version, selected by `image.python`, and every arm of a task runs the same one.

- [ ] **Step 5: Run the tests**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q
```

Expected: PASS. Then confirm nothing still imports the deleted constant:

```bash
cd bakeoff && grep -rn --include='*.py' 'BASE_TAG' . | grep -v '/.venv/'
```

Expected: only `PROXY_TAG` matches, i.e. no `BASE_TAG` line at all.

- [ ] **Step 6: Full suite**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
```

Expected: failures only in `test_run_matrix.py` / `test_grade_script.py` if they referenced the old signature — Task 3 fixes those. If `run_matrix.py` and `grade.py` still call `build_base_image(REPO)` with one argument, that keeps working (the version defaults), so the suite should be green here.

- [ ] **Step 7: Integration leg — build 3.11 for real**

Append to `bakeoff/tests/test_images.py`:

```python
@pytest.mark.integration
def test_a_non_default_base_really_builds_and_carries_the_pins():
    """The claim this broadening rests on, checked against a daemon rather
    than against a rendered string. Measured 2026-09-01: 3.11 gives Python
    3.11.16, pytest 9.1.1, claude 2.1.220, uid 1000."""
    repo_root = Path(__file__).resolve().parent.parent
    image = images.build_base_image(repo_root, "3.11")

    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
         "python --version && pytest --version && claude --version && id -u"],
        capture_output=True, text=True, check=True,
    )

    assert probe.stdout.startswith("Python 3.11.")
    assert "pytest 9.1.1" in probe.stdout
    assert "2.1.220" in probe.stdout
    assert probe.stdout.rstrip().endswith("1000")
```

Run:

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q -m integration \
  --basetemp="$HOME/.cache/bakeoff-pytest"
```

Expected: PASS. (Not marked `task_image` — it builds no task image and touches no repo mirror, so `verify_logger.py`'s `-m "integration and not task_image"` selection legitimately includes it.)

- [ ] **Step 8: Commit**

```bash
cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden
git add bakeoff/docker/eval-agent.Dockerfile bakeoff/src/bakeoff/images.py \
        bakeoff/tests/test_images.py
git commit -m "$(cat <<'EOF'
feat: one base Dockerfile builds every allowed interpreter, tagged per version

FROM python:3.12-slim-bookworm was the reason a manifest could not name an
interpreter. It becomes ARG BASE_PYTHON_VERSION before the FROM, and
build_base_image passes --build-arg.

The ARG is NOT named PYTHON_VERSION, and that is measured rather than
stylistic: the official python: images set their own ENV PYTHON_VERSION
(3.11.16, 3.12.13, 3.13.15) and ENV beats ARG, so after the FROM the
expansion resolves to the base image's patch level even when the ARG is
redeclared. Nothing below the FROM expands it today, so the collision is
latent -- and a later edit that does would read a plausible wrong value with
no error.

BASE_TAG becomes base_tag(version). Deleted rather than aliased: it had one
non-test consumer, and a stale constant is exactly how a caller goes on
building the 3.12 base for a 3.11 task -- which builds, runs and goes green
under the wrong interpreter.

build_base_images deduplicates, because the drivers pass one entry per task
and a per-task build turns the free offline half of --preflight-only into
something nobody waits for.

Measured 2026-09-01, Docker 29.5.2: 3.11 -> Python 3.11.16, 3.13 -> 3.13.15,
pytest 9.1.1 and claude 2.1.220 install on both, and with no --build-arg this
file builds to the byte-identical image id it built before the ARG existed,
so no stored container_image_digest and no cached preflight verdict moves.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: the drivers build the set of bases, and refuse one that disagrees

**Files:**
- Modify: `bakeoff/scripts/run_matrix.py` (the `bakeoff.images` import block; `base_claude_version`; `resolve_tasks`; `main`)
- Modify: `bakeoff/scripts/grade.py` (the `bakeoff.images` import block; `task_resolver`)
- Test: `bakeoff/tests/test_run_matrix.py`, `bakeoff/tests/test_grade_script.py`

**Interfaces:**
- Consumes: `images.build_base_images` (Task 2), `TaskImage.python` (Task 1).
- Produces:
  - `scripts.run_matrix.assert_one_agent(bases: dict[str, str]) -> str` — probes every base; raises `ImageError` if any probe is empty or the answers disagree; otherwise returns the single non-empty version.
  - `scripts.run_matrix.prepare_bases(tasks) -> tuple[dict[str, str], str]` — the seam `main` calls: derives the version set from the tasks, builds one base each, and returns `(bases, expected_claude_version)`. Raises `ImageError`.
  - `scripts.run_matrix.resolve_tasks(tasks, bases: dict[str, str], expected_version: str, cache, force)` — `base_image` becomes `bases`.

**Why a `prepare_bases` seam and not two calls inline in `main`.** `main` is 200 lines of argument parsing, resume planning and matrix execution, and the claim that has to be pinned is an *ordering* one: the version set comes from the task set, the bases are built, they are checked, and only then is a task image built or a proxy started. A seam makes that one function a test can drive; `main` then holds a single call whose failure path is pinned separately (Step 1's fourth test).

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_run_matrix.py` (add `import pytest` and `from pathlib import Path` to the imports if absent):

```python
class _PyTask:
    """Only what the base-selection path reads.

    `task_set_commit` is here for one reason: `main` prints
    `tasks[0].task_set_commit` in its task-set banner, BEFORE the base block,
    so without it `test_main_stops_before_any_task_image_when_the_bases_disagree`
    dies of an AttributeError several lines short of the thing it pins.
    Nothing else `main` touches before `prepare_bases` is missing from this
    fake.
    """

    def __init__(self, task_id, python):
        self.task_id = task_id
        self.task_set_commit = ""
        self.image = type("I", (), {"python": python})()


def test_the_version_set_comes_from_the_task_set_not_from_the_allowlist(
    monkeypatch
):
    """The driver builds what the TASKS need. Deriving it from
    `tasks._PYTHON_VERSIONS` instead would build every allowed interpreter on
    every invocation -- three image builds for a task set that uses one, in
    the offline half that is supposed to be free."""
    import scripts.run_matrix as rm

    asked = {}
    monkeypatch.setattr(
        rm, "build_base_images",
        lambda root, versions: asked.setdefault("versions", list(versions))
        or {v: "sha256:" + v for v in versions},
    )
    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    bases, expected = rm.prepare_bases(
        [_PyTask("a", "3.12"), _PyTask("b", "3.11"), _PyTask("c", "3.12")]
    )

    assert sorted(asked["versions"]) == ["3.11", "3.12"]
    assert bases == {"3.11": "sha256:3.11", "3.12": "sha256:3.12"}
    assert expected == "2.1.220"


def test_two_bases_that_disagree_on_the_agent_stop_the_invocation(monkeypatch):
    """With one base, preflight's `expected_claude_version` refusal did this.
    Passing each task its OWN base's version keeps the half that catches a
    task image whose pip/build clobbered `claude`, and loses exactly one
    thing: cross-BASE agreement. Nothing downstream restores it --
    `Versions.claude_code` is read from the transcript, per run, and no reader
    compares it across tasks."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "base_claude_version",
        lambda image: "2.1.220" if image.endswith("3.12") else "2.1.999",
    )

    with pytest.raises(rm.ImageError) as excinfo:
        rm.assert_one_agent({"3.12": "sha256:3.12", "3.11": "sha256:3.11"})

    assert "2.1.220" in str(excinfo.value)
    assert "2.1.999" in str(excinfo.value)


def test_a_base_that_answers_nothing_is_a_refusal_not_an_agreement(monkeypatch):
    """`base_claude_version` returns "" when the `docker run` exits non-zero.
    Collapsing on the SET would make "every base failed to answer" a single
    value and therefore an agreement -- and the "" it returned is falsy, so
    preflight's `elif expected_claude_version and ...` guard would never fire
    and the agent-version check would be silently off for the whole matrix.
    Emptiness is checked BEFORE agreement."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "")

    with pytest.raises(rm.ImageError) as excinfo:
        rm.assert_one_agent({"3.12": "sha256:a", "3.11": "sha256:b"})

    assert "3.11" in str(excinfo.value) and "3.12" in str(excinfo.value)


def test_one_base_that_answers_nothing_is_also_a_refusal(monkeypatch):
    """The mixed case, which a set-collapse check would report as a
    disagreement with a misleading message and an all-empty check would
    miss."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "base_claude_version",
        lambda image: "2.1.220" if image.endswith("3.12") else "",
    )

    with pytest.raises(rm.ImageError):
        rm.assert_one_agent({"3.12": "sha256:a", "3.11": "sha256:b"})


def test_bases_that_agree_yield_the_single_version(monkeypatch):
    import scripts.run_matrix as rm

    monkeypatch.setattr(rm, "base_claude_version", lambda image: "2.1.220")

    assert rm.assert_one_agent({"3.12": "sha256:a", "3.11": "sha256:b"}) == \
        "2.1.220"


def test_main_stops_before_any_task_image_when_the_bases_disagree(
    monkeypatch, tmp_path
):
    """The ordering claim, driven through `main` rather than asserted about
    it. A refusal that fires AFTER `resolve_tasks` has already built task
    images -- or after the proxy is up -- is a refusal that costs the thing it
    exists to protect. `resolve_tasks` is replaced by a sentinel that fails
    the test if it is reached at all."""
    import scripts.run_matrix as rm

    monkeypatch.setattr(
        rm, "load_task_set",
        lambda path, only=None: [_PyTask("a", "3.11"), _PyTask("b", "3.12")],
    )
    monkeypatch.setattr(
        rm, "prepare_bases",
        lambda tasks: (_ for _ in ()).throw(rm.ImageError("bases disagree: boom")),
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError(
            "resolve_tasks ran after the base check refused: a task image was "
            "built for an invocation that cannot produce a comparison"
        )

    monkeypatch.setattr(rm, "resolve_tasks", _must_not_run)
    monkeypatch.setattr(
        sys, "argv",
        ["run_matrix.py", "--preflight-only", "--event-log", str(tmp_path / "log")],
    )

    assert rm.main() == 1


def test_each_task_is_built_against_the_base_its_manifest_names(
    monkeypatch, tmp_path
):
    """The mapping, not the first entry of it. A task handed the wrong base
    still builds and still runs -- under an interpreter it was not cut for,
    and only preflight's read-back says so.

    The fake `image_entrypoint` returns a non-empty entrypoint so every task
    is rejected on the line right after its image is built. That is the
    earliest point at which the base has already been chosen, and it keeps
    this test off `materialize` and `preflight`, neither of which it is about.
    If `resolve_tasks` is ever refactored so the entrypoint check no longer
    follows the build, this test needs a new stopping point.
    """
    import scripts.run_matrix as rm

    seen = {}

    def _fake_build(task, base, build_root, cache):
        seen[task.task_id] = base
        return "sha256:img-" + task.task_id

    monkeypatch.setattr(rm, "build_task_image", _fake_build)
    monkeypatch.setattr(rm, "image_entrypoint", lambda image: ["/inherited"])

    rm.resolve_tasks(
        [_PyTask("a", "3.11"), _PyTask("b", "3.12")],
        {"3.11": "sha256:B11", "3.12": "sha256:B12"},
        "2.1.220", tmp_path, force=True,
    )

    assert seen == {"a": "sha256:B11", "b": "sha256:B12"}
```

Add `import sys` to the test module's imports.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_run_matrix.py -q
```

Expected: FAIL — `AttributeError: module 'scripts.run_matrix' has no attribute 'assert_one_agent'` (and `prepare_bases`).

- [ ] **Step 3: Change `run_matrix.py`**

Import `build_base_images` and `ImageError` from `bakeoff.images` in place of `build_base_image`. Add, after `base_claude_version`:

```python
def assert_one_agent(bases: dict[str, str]) -> str:
    """Every base ships the same Claude Code, or nothing runs.

    What this replaces. `preflight`'s `expected_claude_version` refusal says
    "two tasks would run different agents and the comparison across them is
    not one". Handing each task its OWN base's version keeps the useful half
    of that -- it still catches a task image whose `image.pip`/`image.build`
    clobbered `claude` -- and loses exactly one thing: cross-BASE agreement.
    Nothing downstream restores it. `Versions.claude_code` is read from the
    transcript, per run, and no reader compares it across tasks, so two bases
    on two agents would pass every gate and be invisible in the log.

    Probing ONE base and passing that string to everyone would also restore
    it, and is rejected: it makes one base arbitrarily canonical, and it
    surfaces a build-level defect as a per-task preflight failure late in the
    run rather than before the first task image is built.

    EMPTY IS A REFUSAL, and it is checked before agreement.
    `base_claude_version` returns "" when its `docker run` exits non-zero, so
    "every base failed to answer" collapses to the single value "" -- which
    reads as agreement, and is falsy, so preflight's
    `elif expected_claude_version and ...` guard never fires and the
    agent-version check is off for every task in the matrix. A gate that
    disables another gate by returning its own failure is worse than no gate.

    In practice they always agree: one `ARG CLAUDE_CODE_VERSION` in one file.
    That is exactly why an unchecked divergence would be assumed away.
    """
    seen = {version: base_claude_version(image)
            for version, image in sorted(bases.items())}
    rendered = ", ".join(
        f"python {v} -> {c or '<no answer>'}" for v, c in seen.items()
    )
    blank = sorted(v for v, c in seen.items() if not c)
    if blank:
        raise ImageError(
            f"`claude --version` answered nothing in the base image(s) for "
            f"python {', '.join(blank)}: {rendered}. An empty version is not "
            "agreement -- it is falsy, so preflight's expected_claude_version "
            "check would be silently disabled for every task in this matrix. "
            "Rebuild the bases."
        )
    distinct = set(seen.values())
    if len(distinct) > 1:
        raise ImageError(
            f"the base images do not all ship the same Claude Code: {rendered}. "
            "Section 5.4 holds the agent identical across everything being "
            "compared, and no per-task check can see this -- each task is "
            "gated against its own base. Rebuild them all from one "
            "docker/eval-agent.Dockerfile."
        )
    return distinct.pop()


def prepare_bases(tasks) -> tuple[dict[str, str], str]:
    """The base images this task set needs, built and checked.

    Called by `main` BEFORE `resolve_tasks` and before the proxy image is
    built, because both of the refusals below are free and offline while
    everything after them costs an image build or a token.

    The version set comes from the TASKS, never from `tasks._PYTHON_VERSIONS`:
    building every allowed interpreter on every invocation is three image
    builds for a task set that uses one, in the half of the run documented as
    free.
    """
    versions = sorted({task.image.python for task in tasks})
    print(f"\nbuilding {len(versions)} base image(s): "
          f"python {', '.join(versions)} ...", flush=True)
    bases = build_base_images(REPO, versions)
    expected = assert_one_agent(bases)
    for version in versions:
        print(f"base py{version}  {bases[version][:19]}...  claude {expected}")
    return bases, expected
```

Change `resolve_tasks`'s signature to `(tasks, bases, expected_version, cache, force)`, say in its docstring that the base is chosen per task from `image.python`, and change the `build_task_image` call to:

```python
        image = build_task_image(task, bases[task.image.python],
                                 cache / "build", cache)
```

In `main`, replace the three base lines with a call whose failure is a clean exit, placed exactly where the old block was — after the task set is loaded and printed, before `resolve_tasks`:

```python
    try:
        bases, expected_version = prepare_bases(tasks)
    except ImageError as exc:
        # Same shape as the preflight NO-GO below: nothing was run, nothing
        # was spent, and the operator gets the reason rather than a traceback.
        print(f"\nBASE IMAGE NO-GO -- nothing was run and nothing was spent\n  {exc}")
        return 1

    resolved, failures = resolve_tasks(
        tasks, bases, expected_version, CACHE, args.force_preflight
    )
```

- [ ] **Step 4: Change `grade.py`**

Import `build_base_images` alongside the rest. Rewrite `task_resolver`'s closure:

```python
    built: dict[str, str] = {}

    def resolve(task) -> TaskSetup:
        version = task.image.python
        if version not in built:
            built.update(build_base_images(REPO, [version]))
        return resolve_task(task, cache, built[version], force_preflight)
```

Update the docstring on two points: the cache is keyed by Python version, one base per version, still lazy so a batch whose records are all gated builds nothing; and this driver deliberately does **not** call `assert_one_agent` — it is offline and per record, reads the preflight cache `run_matrix` wrote, and the invocation that spent the money is where a cross-task claim has to hold. `resolve_task`'s own `base_image` parameter is unchanged: it takes one image and the caller decides which.

- [ ] **Step 5: Run the tests**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_run_matrix.py tests/test_grade_script.py -q
```

Expected: PASS (7 new). `test_grade_script.py`'s existing `resolve_task(..., base_image="sha256:base")` calls are untouched by design.

- [ ] **Step 6: Full suite plus a real dry preflight**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only 2>&1 | head -30
```

Expected: the suite green; the preflight run printing `building 1 base image(s): python 3.12`, then `base py3.12  sha256:...  claude 2.1.220`, then reaching a PASS on click. A NO-GO here means the base mapping is wrong — do not proceed.

- [ ] **Step 7: Commit**

```bash
cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden
git add bakeoff/scripts/run_matrix.py bakeoff/scripts/grade.py \
        bakeoff/tests/test_run_matrix.py
git commit -m "$(cat <<'EOF'
feat: the drivers build the set of bases a task set needs, and refuse a split agent

Both drivers built exactly one base and handed it to every task. With
image.python they build one per distinct version -- derived from the tasks,
never from the allowlist, since building every allowed interpreter on every
invocation is three image builds for a task set that uses one, in the half of
the run documented as free.

assert_one_agent is what this broadening makes necessary. Handing each task
its own base's version keeps the half of preflight's expected_claude_version
refusal that catches a task image whose pip/build clobbered claude, and loses
exactly one thing: cross-BASE agreement. Nothing downstream restores it --
Versions.claude_code is read from the transcript, per run, and no reader
compares it across tasks. Probing one base and passing that string to
everyone would also restore it and is rejected: it makes one base arbitrarily
canonical and surfaces a build-level defect as a late per-task preflight
failure.

An empty answer is a refusal, checked before agreement. base_claude_version
returns "" on a non-zero docker run, so "every base failed to answer"
collapses to one value and reads as agreement -- and that "" is falsy, so
preflight's `elif expected_claude_version and ...` guard would never fire and
the agent-version check would be off for every task in the matrix.

prepare_bases is the seam main calls, before resolve_tasks and before the
proxy image, because both refusals are free and offline while everything
after them costs an image build or a token. A test drives main with the check
failing and a sentinel resolve_tasks, so the ordering is pinned rather than
asserted in prose.

grade.py's resolver keys its cache by version instead of the literal "base"
and stays lazy. It does not call assert_one_agent: it is offline and per
record, and the invocation that spent the money is where the cross-task claim
has to hold.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

## Task 4: preflight reads the interpreter back and refuses a mismatch

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (`PREFLIGHT_VERSION`; helpers beside `_declared_env`; the evidence pre-write block that sets `image_env_observed = None`; the environment checks immediately after the `claude --version` block inside `preflight`)
- Test: `bakeoff/tests/test_preflight.py` (`_FakeImage`, `_ScriptedContainer.exec`, new tests after the `image.env` group)

**Interfaces:**
- Consumes: `TaskImage.python` (Task 1).
- Produces:
  - `bakeoff.preflight._declared_python(task) -> str`
  - `bakeoff.preflight._parse_python_version(raw: str) -> str` — `"Python 3.11.16"` → `"3.11"`, `""` on anything unparseable
  - evidence keys `python_declared: str` and `python_observed: str | None`
  - `PREFLIGHT_VERSION` incremented by one from its on-disk value.

- [ ] **Step 1: Write the failing tests**

First extend the fakes. In `_FakeImage`:

```python
@dataclass(frozen=True)
class _FakeImage:
    env: dict = field(default_factory=dict)
    python: str = "3.12"
```

In `_ScriptedContainer.__init__`, add `python="Python 3.12.13"` to the keyword arguments and `self.python = python`. In `exec`, add this branch **before** the `cmd[0] == "sh"` branch and before the hypothesis `-c` branch:

```python
        if cmd == ["python", "--version"]:
            # Measured 2026-09-01: CPython writes this to STDOUT, not stderr
            # (`docker run ... python --version 2>&1 >/dev/null` is empty).
            # Python 2 wrote it to stderr; 3.4+ does not.
            if self.python is None:
                return _Exec(exit_code=127, stderr="python: not found\n")
            return _Exec(stdout=self.python + "\n")
```

Then append the tests:

```python
# --- image.python read-back ---------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Python 3.11.16", "3.11"),
    ("Python 3.12.13", "3.12"),
    ("Python 3.13.15", "3.13"),
    ("Python 3.13.0rc1", "3.13"),
    ("", ""),
    ("nonsense", ""),
])
def test_the_version_is_parsed_to_major_minor(raw, expected):
    assert _parse_python_version(raw) == expected


def test_a_declaration_that_prefixes_another_version_is_still_refused(
    monkeypatch, tmp_path
):
    """The shape M6 measured, driven through the GATE rather than the parser,
    because this is the one a `startswith` revert survives everywhere else:
    `"Python 3.13.15".startswith("Python 3.11")` is False, so the 3.11-vs-3.13
    test above stays green under the mutant. `"Python 3.13.15".startswith(
    "Python 3.1")` is True, and this is the test that goes red.

    `_FakeImage(python="3.1")` deliberately carries a value `load_task` would
    refuse: preflight does not re-validate the allowlist -- `tasks`'s loader is
    the single gate for that (D2) -- so the comparison inside preflight has to
    stand on its own arithmetic, and this is what says it does.
    """
    task = _FakeTask(image=_FakeImage(python="3.1"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   python="Python 3.13.15")

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("3.1" in p and "3.13" in p for p in result.problems)


def test_a_prefix_match_would_accept_the_wrong_interpreter():
    """Why the comparison is equality on the parsed pair and not a startswith.
    Measured: "3.13.15".startswith("3.1") is True, and so is
    "Python 3.13.15".startswith("Python 3.1"). A read-back written that way
    accepts 3.13 for a manifest declaring 3.1 -- green gate, wrong
    interpreter, and no later stage re-derives it."""
    assert "Python 3.13.15".startswith("Python 3.1")
    assert _parse_python_version("Python 3.13.15") != "3.1"


def test_the_declared_interpreter_is_read_back_out_of_the_container(
    monkeypatch, tmp_path
):
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   python="Python 3.11.16")

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["python_declared"] == "3.11"
    # The FULL string: the patch level is information the manifest cannot
    # carry, and preflight.json outlives the run.
    assert result.evidence["python_observed"] == "Python 3.11.16"


def test_an_interpreter_that_is_not_the_declared_one_is_refused(
    monkeypatch, tmp_path
):
    """The tag is mutable and local. A stale bakeoff-eval-agent:base-3.11, a
    task image built against the wrong entry of the bases map, or an
    image.build step that puts another python earlier on PATH all leave every
    unit and rendering test green -- and the suite then runs, and goes green,
    under an interpreter the task was not cut for."""
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   python="Python 3.13.15")

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("3.11" in p and "3.13" in p for p in result.problems)
    assert result.evidence["python_observed"] == "Python 3.13.15"


def test_a_patch_level_difference_is_not_a_mismatch(monkeypatch, tmp_path):
    """The manifest carries no patch level and must not have to: the upstream
    tag is republished with security fixes and a task pinned to 3.11.16 would
    NO-GO the day 3.11.17 ships."""
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   python="Python 3.11.99")

    assert _run_preflight(monkeypatch, tmp_path, task, container).ok


def test_an_image_with_no_python_at_all_is_a_named_problem(monkeypatch, tmp_path):
    task = _FakeTask(image=_FakeImage(python="3.12"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   python=None)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("python --version" in p for p in result.problems)
    assert result.evidence["python_observed"] is None


def test_the_evidence_keys_exist_on_the_path_that_never_starts_a_container(
    tmp_path
):
    """`observed: ""` from a gate that never looked is a CLAIM. Two absences
    that render identically are the same defect one layer down -- the reason
    image_env_observed is pre-written as None."""
    task = _FakeTask(tests=_FakeTests(runner=("nose",)),
                     image=_FakeImage(python="3.11"))

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.evidence["python_declared"] == "3.11"
    assert result.evidence["python_observed"] is None
```

Add `_parse_python_version` to the module's `from bakeoff.preflight import (...)` block.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -k python -q
```

Expected: FAIL — `ImportError: cannot import name '_parse_python_version'`.

- [ ] **Step 3: Add the helpers**

In `bakeoff/src/bakeoff/preflight.py`, add `from bakeoff.tasks import _DEFAULT_PYTHON` to the module imports (beside `from bakeoff.container import RunContainer`; `tasks` imports nothing from `preflight`, so there is no cycle — its only `bakeoff` import is a function-local `claude_runner` one). Then, after `_declared_env`:

```python
#: `Python 3.11.16` -> the leading two components. Anchored, and the trailing
#: group is deliberately not required to be numeric-only: measured shapes
#: include `3.13.0rc1`.
_PYTHON_VERSION_LINE = re.compile(r"^Python\s+(\d+)\.(\d+)(?:\.|\s|$)")


def _parse_python_version(raw: str) -> str:
    """`"Python 3.11.16"` -> `"3.11"`. `""` when it cannot be read.

    Major.minor, compared for EQUALITY, and both halves of that are load
    bearing.

    Not a prefix test: measured, `"3.13.15".startswith("3.1")` is True and so
    is `"Python 3.13.15".startswith("Python 3.1")`, so a `startswith`
    read-back accepts 3.13 for a manifest declaring 3.1 -- a green gate over
    the wrong interpreter, which no later stage re-derives.

    Not the whole string either: the manifest carries no patch level and must
    not have to. The upstream tag is republished with security fixes, so a
    task pinned to 3.11.16 would NO-GO the day 3.11.17 ships.

    `""` rather than a raise: this is a gate that COLLECTS problems, and an
    unparseable answer is reported as the mismatch it is alongside whatever
    else is wrong, not as a traceback that hides the other four.
    """
    match = _PYTHON_VERSION_LINE.match(raw.strip())
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def _declared_python(task) -> str:
    """The manifest's `image.python`, or the base Dockerfile's default.

    `getattr` twice, like `_declared_env`'s: this module's entry point takes an
    untyped `task`, and a manifest object predating the key must not crash the
    gate.

    The fallback is IMPORTED, never restated. Three copies of the default
    already exist (the Dockerfile's ARG, `tasks._DEFAULT_PYTHON`,
    `images._DEFAULT_PYTHON`) and are pinned equal to each other by
    tests/test_images.py; a fourth one here would be the only unpinned copy,
    and it sits in the gate that exists to catch exactly this class of
    disagreement.
    """
    return getattr(getattr(task, "image", None), "python", "") or _DEFAULT_PYTHON
```

- [ ] **Step 4: Pre-write the evidence and add the check**

Beside the existing evidence pre-writes (the block ending `evidence["hypothesis_imported_by_suite"] = None`), before the non-pytest early return:

```python
    evidence["python_declared"] = declared_python = _declared_python(task)
    #: `None`, not `""`. A gate that never started a container has not
    #: observed an empty version -- it has not observed anything.
    evidence["python_observed"] = None
```

Immediately after the `claude --version` block inside `preflight`, add:

```python
        # The interpreter, read back out of the container rather than trusted
        # from the manifest. `image.python` selects which base the drivers
        # build, and the tag they build it under is MUTABLE and LOCAL: a stale
        # `bakeoff-eval-agent:base-3.11` left by an earlier Dockerfile, a task
        # image built against the wrong entry of the bases map, or an
        # `image.build` step that puts another interpreter earlier on PATH all
        # leave every rendering and unit test green. The failure is then the
        # worst shape this repository knows -- the suite runs, the gate is
        # green, and the interpreter is not the one the task was cut for.
        #
        # Plain `python`, deliberately, and NOT the runner's interpreter.
        # `image.python` is a claim about the BASE image, and `python` is what
        # the Dockerfile's ARG selects; a task whose `tests.runner` names
        # /opt/venv/bin/python is describing a different environment the key
        # makes no claim about.
        #
        # Measured 2026-09-01: `python --version` writes to STDOUT (Python 2
        # wrote it to stderr; 3.4+ does not), so `.stdout` is the right field.
        python = container.exec(["python", "--version"])
        if python.exit_code != 0:
            problems.append(
                "`python --version` failed inside the image: the base this "
                f"task declares (image.python: {declared_python!r}) either was "
                "not the one it was built on, or its interpreter is no longer "
                "first on PATH. Every check below runs the suite through it"
            )
        else:
            evidence["python_observed"] = observed_python = python.stdout.strip()
            if _parse_python_version(observed_python) != declared_python:
                problems.append(
                    f"the container runs {observed_python!r} but the manifest "
                    f"declares image.python: {declared_python!r}. The base tag "
                    "is local and mutable, so this is a stale or mismatched "
                    "base rather than a manifest error: rebuild the bases "
                    "(`run_matrix.py --preflight-only` builds the set the task "
                    "set needs) and rebuild this task's image against the "
                    "right one"
                )
```

- [ ] **Step 5: Bump `PREFLIGHT_VERSION`**

Read `PREFLIGHT_VERSION` (a `str`; broadenings 3 and 4 each moved it), add one, and append to the comment block above it:

```
#: <N> adds the `image.python` read-back: a verdict cached under <N-1> was
#: written by a gate that never asked which interpreter the container runs, so
#: a task built against a stale or mismatched base keeps serving a PASS while
#: its suite runs under an interpreter the task was not cut for.
```

- [ ] **Step 6: Run the tests**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q
```

Expected: PASS, including the pre-existing tests — the new `python --version` exec must not break `_ScriptedContainer`'s `unscripted exec` assertion anywhere.

- [ ] **Step 7: Full suite and the integration leg**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -m integration \
  --basetemp="$HOME/.cache/bakeoff-pytest"
```

Expected: both green. The integration legs build the real base at the default and the fixture task declares no `python:`, so the read-back sees `Python 3.12.x` against a declared `3.12`.

- [ ] **Step 8: Confirm the cache invalidates**

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only 2>&1 | tail -20
```

Expected: click preflights **fresh** (not `preflight cached PASS`) because `PREFLIGHT_VERSION` moved, and ends `preflight PASS on all 1 task(s)`. Re-run it: the second invocation must say `preflight cached PASS`.

- [ ] **Step 9: Add one mutation anchor, and check the existing ones still bite**

`scripts/mutation_check.py` has **no anchor in `images.py`**, and its
`preflight.py` and `run_matrix.py` anchors sit on lines this broadening does
not touch (the `preflight_cache_key` f-string is the nearest, and it is
unchanged) — so nothing there goes stale. Confirm that first:

```bash
cd bakeoff && .venv/bin/python scripts/mutation_check.py
```

Run it **solo**: it edits sources in place. Expected: no `stale anchor`
failure.

Then add one anchor for the guarantee this task introduces, in the same shape
as the ones already in the file — revert the parsed-equality comparison to the
prefix test that M6 measured as wrong, and require a named test to go red:

```python
    (
        # Major.minor EQUALITY, not a prefix test. Measured:
        # "Python 3.13.15".startswith("Python 3.1") is True, so a prefix
        # comparison accepts 3.13 for a declared 3.1 -- a green gate over an
        # interpreter the task was not cut for, which no later stage
        # re-derives.
        "preflight: accept a prefixing interpreter as the declared one",
        "src/bakeoff/preflight.py",
        "            if _parse_python_version(observed_python) != declared_python:",
        '            if not observed_python.startswith(f"Python {declared_python}"):',
        "tests/test_preflight.py -k prefixes_another_version_is_still_refused",
        "not integration",
    ),
```

Six fields, `-k` and never `::` — the shape every entry in the file uses;
copy the indentation of the `before` line from the source exactly or the
anchor reports stale.

**The selector is `test_a_declaration_that_prefixes_another_version_is_still_refused`,
not the 3.11-vs-3.13 test.** Measured: `"Python 3.13.15".startswith("Python
3.11")` is **False**, so under the mutant the ordinary-case test still appends
its problem and stays green — the anchor would never bite. Only a declared
version that is a genuine string prefix of the observed one kills it, which is
the exact shape M6 measured.

Re-run `scripts/mutation_check.py` solo and confirm the new anchor reports the
test going red.

- [ ] **Step 10: Commit**

```bash
cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py \
        bakeoff/scripts/mutation_check.py
git commit -m "$(cat <<'EOF'
fix: the gate asks the container which interpreter it runs, instead of trusting the tag

image.python selects which base the drivers build, and the tag they build it
under is mutable and local. A stale bakeoff-eval-agent:base-3.11 from an
earlier Dockerfile, a task image built against the wrong entry of the bases
map, or an image.build step that puts another python earlier on PATH all
leave every rendering and unit test green -- and the failure is then the worst
shape this repository knows: the suite runs, the gate is green, and the
interpreter is not the one the task was cut for.

Plain `python`, not the runner's interpreter. image.python is a claim about
the BASE image and `python` is what the Dockerfile's ARG selects; a runner
naming /opt/venv/bin/python describes an environment the key says nothing
about, and asserting against it would make the gate claim something the
manifest never did.

Compared on parsed major.minor, for equality. Not a prefix test --
"3.13.15".startswith("3.1") is True, so a startswith read-back accepts 3.13
for a manifest declaring 3.1. Not the whole string either: the manifest
carries no patch level, and the upstream tag is republished, so a task pinned
to 3.11.16 would NO-GO the day 3.11.17 ships.

python_observed carries the full string, because the patch level is
information the manifest cannot express and preflight.json outlives the run.
It is pre-written as None on the path that never starts a container: "" from
a gate that never looked is a claim.

PREFLIGHT_VERSION moves. A verdict cached under the old gate was written by
one that never asked.

Measured 2026-09-01: python --version writes to stdout, not stderr.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: docs, the worked example, and the review log

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md` (Layer 1's loader table and its preflight table; the `### The image` section)
- Modify: `docs/BUILDING-A-TASK-SET.md` (the `## 2. Choosing a corpus` screen command; the manifest skeleton; "Three keys that bite"; the troubleshooting table)
- Modify: `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` (the `image:` block)
- Modify: `tasks/todo.md` (append a section)

- [ ] **Step 1: `HARVESTING.md` — Layer 1**

Add one row to the loader table, after the `strip_paths` rows:

```markdown
| `image.python`, when declared, is a quoted string in `{"3.11", "3.12", "3.13"}` | an unquoted `3.10` is the float `3.1` and an unquoted `3.11` is `3.11` — the version that reaches the build is the parser's, not the manifest's. An unlisted value is either a floating tag (two collections months apart on different interpreters, with nothing in the record saying which) or one that fails at the `FROM` with a registry error mid-build |
```

Add one row to the `### Preflight` table:

```markdown
| `python --version` inside the image parses to the declared `image.python` | the base tag is local and mutable; a stale or mismatched base runs the suite under an interpreter the task was not cut for, and every other gate stays green |
```

- [ ] **Step 2: `HARVESTING.md` — "The image"**

Insert before the **No git submodules** bullet:

```markdown
- **The interpreter is a choice, and a closed one.** `image.python` selects the
  base — `"3.11"`, `"3.12"` (the default) or `"3.13"`. Every arm of a task runs
  the same base, so this is not a §5.4 divergence: what that section holds
  identical is the environment two *arms* are compared in, and a task is
  compared against itself. Adding a fourth version is three steps and the first
  is a measurement: build `docker/eval-agent.Dockerfile` with
  `--build-arg BASE_PYTHON_VERSION=<v>` and confirm both in-image pin
  assertions fire (`pytest 9.1.1 pinned`, `claude 2.1.220 pinned`), then add the
  string to `tasks._PYTHON_VERSIONS`, then add it here. Verified 2026-09-01:
  3.11 → `Python 3.11.16`, 3.12 → `3.12.13`, 3.13 → `3.13.15`, with pytest
  9.1.1 and Claude Code 2.1.220 installing on all three.

  **No screened repository is excluded on this ground today.** The screen at
  `docs/BUILDING-A-TASK-SET.md` §2 was run entirely in `python:3.12-slim-bookworm`
  (recorded above), so a repository needing 3.11 or 3.13 semantics would have
  shown up as a suite failure with an unrelated-looking cause rather than as a
  version verdict — the key exists for the candidates the screen has not reached
  yet, and a re-screen at a second version is what would populate this
  paragraph.
```

- [ ] **Step 3: `docs/BUILDING-A-TASK-SET.md` — the screening command**

Under the `docker run ... python:3.12-slim-bookworm` block, add:

```markdown
`python:3.12-slim-bookworm` here matches the base image's default. If a
repository's suite fails on it for a version reason — a `SyntaxError` on
newer syntax, a removed stdlib module, a C extension with no wheel — re-run
the screen against `python:3.11-slim-bookworm` or `python:3.13-slim-bookworm`
and, if it passes there, declare `image.python: "3.11"` (quoted) in the
manifest. Only those three are accepted; see `taskset/HARVESTING.md` for how
the set grows.
```

- [ ] **Step 4: `docs/BUILDING-A-TASK-SET.md` — the skeleton and the keys**

In the manifest skeleton, add to the `image:` block:

```yaml
image:
  # python: "3.12"                    # QUOTED. 3.11 | 3.12 (default) | 3.13
  apt: []                             # OS packages the suite needs
  pip: ["pytest==8.3.5"]              # PINNED versions only
  build: ["pip install -e ."]         # EDITABLE — see below
```

Add a fourth bullet to "Three keys that bite" (rename the heading to "Four keys that bite"):

```markdown
- **`image.python` is quoted, and from a closed set.** YAML reads an unquoted
  `3.10` as the float `3.1`, so the version that reaches the build is not the
  one you wrote — the loader refuses a non-string rather than coercing. The set
  is `{"3.11", "3.12", "3.13"}` because those are the three the base image has
  been built at; anything else is a floating tag or a registry error mid-build.
  Every arm of a task runs the same base, so this is a per-task choice and not
  a §5.4 divergence.
```

Add a troubleshooting row:

```markdown
| preflight says the container runs a different Python than the manifest declares | a stale or mismatched base image. Rebuild: `run_matrix.py --preflight-only` builds the set the task set needs and re-tags each one |
```

- [ ] **Step 5: the worked example**

In `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, add to the `image:` block above `apt:`:

```yaml
  # python: "3.12"
  #
  # Omitted, which means the default — and the default is what this repository
  # needs. Declare it only when the suite requires another interpreter, and
  # QUOTE it: YAML reads an unquoted 3.10 as the float 3.1, so the version that
  # reaches the build would not be the one written here. Accepted values are
  # "3.11", "3.12" and "3.13" — the three the base image has been built at
  # (verified 2026-09-01; pytest 9.1.1 and Claude Code 2.1.220 install on all
  # three). Every ARM of a task runs the same base, so this is a property of
  # the task and not a §5.4 divergence; preflight reads `python --version` back
  # out of the container and refuses a mismatch, because the base tag is local
  # and mutable and a stale one leaves every other gate green.
```

Do **not** uncomment it: `task_version` would have to move and `manifest_digest` would change for no behavioural reason.

- [ ] **Step 6: verify the manifest still loads and its start state is unchanged**

```bash
cd bakeoff && .venv/bin/python -c "
from bakeoff.tasks import load_task
t = load_task('taskset/click-3360-write-usage-empty-args')
print(t.image.python, t.manifest_digest, t.declared_start_sha)
"
```

Expected: `3.12`, and a `manifest_digest` **different** from before Step 5 (a comment is manifest bytes) with `declared_start_sha` unchanged. Then:

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only
```

Expected: `preflight PASS on all 1 task(s)`. It re-runs rather than hitting the cache, because the comment moved `manifest_digest` — that is the cache key working, not a fault.

- [ ] **Step 7: the review log**

Append a `## Broadening 5 — a per-task Python version — 2026-09-01` section to `tasks/todo.md`, in the style of the Broadening 1 and 2 sections above it. Cover, one bullet each:

- **The `ARG` name is measured, not stylistic.** The `python:` images set their own `ENV PYTHON_VERSION` (3.11.16 / 3.12.13 / 3.13.15) and `ENV` beats `ARG`, so `${PYTHON_VERSION}` after the `FROM` reads the patch level even when redeclared. Latent today; a plausible wrong value the moment anything expands it.
- **The default is byte-identical.** The parameterised file with no `--build-arg` builds to `sha256:dfd2cc06…`, the same id as the unparameterised one, so no stored `container_image_digest` and no cached verdict moved.
- **`assert_one_agent` is the check the broadening created.** Several bases make preflight's `expected_claude_version` refusal tautological; the driver-level comparison is the only place a split agent is visible.
- **No record field.** `execute_run` makes no `claude --version` exec — `Versions.claude_code` comes from the transcript — so there was no free run-time read to make an observation out of. Digest + manifest is the join, as it is for `image.env`, and `SCHEMA_VERSION` did not move.
- **What was not done:** no repository was re-screened at 3.11 or 3.13, so no currently-excluded repo has been shown to be reopened by this key (see TASKS.md follow-up below).

- [ ] **Step 8: `TASKS.md` follow-up**

Add one P2 entry: re-screen the `HARVESTING.md` corpus at 3.11 and 3.13 to find out which candidates the key actually reopens. The key is built and gated; the corpus evidence for it is not yet collected, and claiming a yield improvement without that measurement would be the "report the gap, never estimate it" rule being broken.

- [ ] **Step 9: Full suite and both gates**

```bash
cd bakeoff && .venv/bin/python -m pytest tests/ -q
cd bakeoff && .venv/bin/python -m pytest -q -m integration \
  --basetemp="$HOME/.cache/bakeoff-pytest"
cd bakeoff && .venv/bin/python scripts/verify_logger.py
```

Expected: unit suite baseline + the new tests, 0 failed; the integration suite green; `verify_logger.py` reporting a pass (it needs the Docker daemon or it reports `GATE INCOMPLETE` and exits 1 — that is not a pass).

- [ ] **Step 10: Commit**

```bash
cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden
git add bakeoff/taskset/HARVESTING.md docs/BUILDING-A-TASK-SET.md \
        bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml \
        tasks/todo.md TASKS.md
git commit -m "$(cat <<'EOF'
docs: what image.python accepts, how the set grows, and what has not been measured

HARVESTING gains the two rules the code now enforces -- the quoted-string
allowlist at load time and preflight's python --version read-back -- plus the
three-step procedure for adding a version, whose first step is a measurement
rather than an edit.

It also records the honest negative: no screened repository is excluded on
this ground today. The whole screen was run in python:3.12-slim-bookworm, so
a repo needing 3.11 or 3.13 semantics would have surfaced as a suite failure
with an unrelated-looking cause, not as a version verdict. The key exists for
candidates the screen has not reached; a re-screen at a second version is the
missing evidence, and it is filed in TASKS.md rather than asserted here.

BUILDING-A-TASK-SET gets the fallback path in the screening step, the
commented key in the skeleton, a fourth entry in "keys that bite" (the
unquoted-3.10-is-3.1 trap), and the troubleshooting row for a read-back
mismatch, which points at a rebuild rather than at the manifest.

The click manifest carries the key commented out, as image.env's example
does. Left commented deliberately: uncommenting it would move task_version
and manifest_digest for no behavioural change.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Sentences that belong in `CLAUDE.md`, deferred

Not applied here — `CLAUDE.md` is not edited on this branch (an uncommitted edit to it exists on `main` and the merge would conflict). Apply these after the branch lands.

1. To **Invariants**, a new bullet:

> **The base image is per Python version, and the tag that names it is not evidence.** `image.python` (`"3.11" | "3.12" | "3.13"`, closed set, refused at load time) selects which base a task is built on; `run_matrix` and `grade.py` build one base per *distinct* version and index it by the manifest's value. Every arm of a task runs the same base, so this is not a §5.4 divergence — what that section holds identical is the environment two *arms* are compared in. The tag is local and mutable, so preflight reads `python --version` back out of the container and refuses a mismatch on parsed **major.minor equality**: `"3.13.15".startswith("3.1")` is `True`, so a prefix test accepts the wrong interpreter, and the whole string is wrong the other way because the upstream tag is republished. Nothing is added to `Versions`: `execute_run` makes no version exec of its own (`claude_code` comes from the transcript), so a `python_version` field could only be copied from the manifest — configuration reported as observation. `container_image_digest` pins the layer and `manifest_digest` pins the manifest that produced it.

2. To **Config gotchas that have already cost a debugging session**:

> **The base Dockerfile's `ARG` is `BASE_PYTHON_VERSION`, never `PYTHON_VERSION`.** The official `python:` images set their own `ENV PYTHON_VERSION` — measured `3.11.16`, `3.12.13`, `3.13.15` — and `ENV` beats `ARG`, so after the `FROM` the expansion resolves to the base image's *patch level* rather than to the build arg, redeclaration or not (measured 2026-09-01, Docker 29.5.2, legacy builder: `after-FROM WITH redeclare: [3.11.16]`). Nothing below the `FROM` expands it today, so the collision is latent — and a later edit that does read a plausible wrong value with no error. Under the safe name the redeclaration behaves: `[3.11] vs image ENV [3.11.16]`.

3. To **Architecture**, extending the images paragraph:

> `images.py` builds a base per Python version (`base_tag`, `build_base_images`); adding a version means building the real Dockerfile at it and confirming both in-image pin assertions fire, then adding it to `tasks._PYTHON_VERSIONS` and to `HARVESTING.md`. `run_matrix.prepare_bases` derives the version set from the tasks (never from the allowlist) and `assert_one_agent` refuses an invocation whose bases do not all ship the same Claude Code — with several bases, cross-*base* agreement is the one thing preflight's own `expected_claude_version` refusal stops covering, since each task is gated against its own base. An **empty** probe is a refusal there, not agreement: `base_claude_version` returns `""` on a non-zero `docker run`, all-empty collapses to one value, and that value is falsy — so preflight's `elif expected_claude_version and ...` guard would never fire and the agent-version check would be off for every task in the matrix.

---

## Self-review

**Spec coverage.** The broadening's five named deliverables map to tasks: the manifest key with a default → Task 1; the parameterised base and per-version tagging → Task 2; `run_matrix` building the set → Task 3; preflight's `python --version` evidence and mismatch refusal → Task 4; "check `schema.py` for where the base image is recorded and whether `SCHEMA_VERSION` must move" → D6 and Task 1 Step 7 (answered: it does not). Docs → Task 5.

**Type consistency.** `base_tag(python_version: str) -> str`, `build_base_image(repo_root, python_version="3.12", tag=None) -> str`, `build_base_images(repo_root, versions) -> dict[str, str]`, `assert_one_agent(bases: dict[str, str]) -> str`, `resolve_tasks(tasks, bases, expected_version, cache, force)`, `_python_version(value, where) -> str`, `_parse_python_version(raw) -> str`, `_declared_python(task) -> str`, `TaskImage.python: str`. Used under those exact names in every later task.

**Type consistency, continued.** `prepare_bases(tasks) -> tuple[dict[str, str], str]` and `assert_one_agent(bases) -> str` (raises `ImageError`; never returns `""`). `resolve_tasks` takes `bases` positionally in the same slot `base_image` held.

**Known rough edge, called out rather than hidden.** Task 3's `test_each_task_is_built_against_the_base_its_manifest_names` drives `resolve_tasks` through a fake `image_entrypoint` that returns a non-empty entrypoint, so every task is rejected on the line right after its image is built. That is the earliest point at which the base has already been chosen, and it keeps the test off `materialize` and `preflight`. If `resolve_tasks` is refactored so the entrypoint check no longer follows the build, this test needs a different stopping point — its docstring says so.

## Review round 1 — what changed

Every measurement in this plan was independently reproduced by the reviewer (the three digests, the `ARG`/`ENV` collision, `python --version` on stdout, YAML's bare `3.10` → `3.1`, and the byte-identical default). Six defects were found and all six are addressed above:

| # | Defect | Resolution |
|---|---|---|
| C1 | Nothing pinned that `main` calls the agent check — both tests called it directly | `prepare_bases` extracted as the seam; `test_main_stops_before_any_task_image_when_the_bases_disagree` drives `main` with a sentinel `resolve_tasks` |
| C2 | `assert_one_agent` returned `""` when every probe failed, silently disabling preflight's falsy-guarded agent check matrix-wide | emptiness raises, and is checked **before** agreement; two tests (all-empty, one-empty) |
| I3 | `images._DEFAULT_PYTHON`'s docstring claimed a pin nothing wrote, and `preflight` held a fourth unpinned literal | the test asserts `images._DEFAULT_PYTHON == tasks._DEFAULT_PYTHON` **and** the Dockerfile ARG; `preflight` imports the constant instead of restating it |
| I4 | `test_one_base_is_built_per_distinct_version_not_per_task` patched a dead name and duplicated Task 2 | deleted; replaced by `test_the_version_set_comes_from_the_task_set_not_from_the_allowlist`, which pins the claim only the driver makes |
| I5 | a `...`-bodied stub written and deleted inside one step | the concrete test is presented once, with its stopping point explained in its own docstring |
| I6 | D4 overstated the loss and skipped the alternative | D4 now says cross-**base** agreement is what stops being checked, and rejects the single-reference-base design in one paragraph |

Round 2 added two fixes: the mutation anchor now selects the only test that kills the prefix mutant (`"Python 3.13.15".startswith("Python 3.11")` is False, so the ordinary-case test survives it), written as the real six-field `-k` tuple; and `_PyTask` carries `task_set_commit = ""` because `main` prints it in the task-set banner before the base block.

Minor items applied: D6 names `task_id` + `Versions.task_set_commit` (not `manifest_digest`, which `RunRecord` does not carry); D5 says the evidence file outlives the *run*, not the next preflight; `build_base_image`'s unused `tag=` parameter dropped; `test_integration_grader.py::click_image` named as the third caller with the orphaned `:base` tag noted; a table records why `smoke_test.py`, `verify_logger.py` and `dry_run.py` are untouched; `mutation_check.py` gets a stale-anchor check plus one new anchor (Task 4, Step 9); every line-number reference replaced by a symbol name, since broadenings 3 and 4 land first and both edit these files.
