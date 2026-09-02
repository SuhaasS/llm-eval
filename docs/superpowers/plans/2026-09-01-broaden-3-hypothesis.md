# Broadening 3 — hypothesis (property-based) test suites

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept a task whose suite is property-based, by making Hypothesis deterministic for **every process in the container** — the gate's, the grader's, and the ones the agent invents — through the environment the task image bakes in, rather than through an argv only the harness ever types.

**Architecture:** One new manifest key, one new Dockerfile line, one new preflight assertion. `TaskImage` gains `env: dict[str, str]`, restricted to a closed allowlist of keys; `images.render_dockerfile` emits it as `ENV` lines **after** the build steps, so the built image carries it; Docker merges an image's environment into every `exec`, so preflight, `oracle`, `grader` and the agent's own `claude` process all see it without any of them being changed. Preflight reads the declared keys back out of the container with `printenv` and refuses a mismatch. Nothing enters `RunRecord`: `Versions.container_image_digest` already pins the ENV layer, and it pins it as an observation of the built image rather than as a copy of the manifest.

**Tech Stack:** Python 3.12, pytest (worktree venv 9.1.1; eval base image pins 9.1.1; `click-3360` pins 8.3.5), **Hypothesis 6.167.1**, Docker 29.5.2 (integration tests only).

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.3 the loop being measured, §3.7 the task record, §5.1 the image, §5.2 the pinned session config, §5.6 the submission diff, §6.4 confounds) and `docs/superpowers/specs/2026-08-17-offline-grader-design.md` (checks 5 and 6). Shared context: `.superpowers/broaden/CONTEXT.md`. Candidate rules: `bakeoff/taskset/HARVESTING.md`.

## Global Constraints

- Repo: `/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden`, branch `broaden-taskset`. Do not touch any other checkout. Broadenings 1 (`strip_paths`) and 2 (collection-error f2p) are already at HEAD; read code, not their plans.
- Run everything from `bakeoff/` with its venv: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`. **Baseline: `1185 passed, 49 deselected in 45.73s`, measured 2026-09-01 at `e3ceb13`.** Three `docs:` commits landed on the branch during planning (`ce35473`, `b69b958`, `e3ceb13`); if HEAD has moved again, re-measure before Task 1 rather than trusting this line.
- **Integration tests are deselected by default** (`addopts = "-m 'not integration'"`). A guarantee pinned *only* by an integration test is not pinned by the suite anyone runs, so every guarantee below has a default-suite test too. Run the integration leg when a change touches container/preflight/image behaviour: `cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`. `--basetemp` under `$HOME` is mandatory on macOS — the Docker VM mounts `$HOME` but not `/var/folders`, and a repo bind-mounted from there is a silently empty directory inside the container.
- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. Claims about external behaviour are annotated with what they were verified against (`Measured 2026-09-01 against hypothesis 6.167.1`). A comment saying what a line does rather than what breaks without it does not fit here.
- **Do NOT edit `CLAUDE.md` in this branch.** An uncommitted edit to it exists on `main` and the merge would conflict. Invariant prose goes in module docstrings and `HARVESTING.md`; the sentences that belong in `CLAUDE.md` are listed in this plan's final section.
- **`PREFLIGHT_VERSION` "4" → "5".** It is in the preflight cache key, and this broadening adds an assertion (the env read-back). Without the bump every warm cache serves a verdict written by a gate that never looked at `image.env`.
- **`SCHEMA_VERSION` does not move, and `GRADER_VERSION` does not move.** Nothing new is written into a `RunRecord` or a `GradeRecord`, and no check changes what it means. See decision 8.
- **`click-3360`'s `start_sha` must not move.** The new key is under `image:`, which is not an input to `materialize`, so `start_sha` is a pure function of the same four things it was. `git diff main -- bakeoff/taskset/` must show **only** comment lines at the end of this broadening (the commented-out `image.env` example), and `.venv/bin/python -c "from bakeoff.tasks import load_task; ..."` must still recompute `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`.
- **The gated argv is the graded argv.** `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` pins this against a *literal*. This broadening adds nothing to any argv, so that test must pass **unchanged and untouched**. If you find yourself editing it, you have chosen the wrong mechanism — go back and read decision 3.
- Stage files explicitly (`git add <paths>`), never `git add -A`/`-a`. End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Do not run `bakeoff/scripts/mutation_check.py` concurrently with anything else; it edits sources in place. This plan adds **one** anchor (Task 3, Step 7).
- YAGNI: build only what this broadening needs. No hooks for broadenings 4–7. In particular: do **not** add a `python:` key, do **not** parameterise the base image, and do **not** generalise `image.env` past the two keys measured below.

---

## Design decisions, and why

Read this whole section before Task 1. Every task implements one of these; a reviewer rejecting a task rejects one of these decisions.

### 1. What Hypothesis actually does — measured, and it refutes three premises

Measured 2026-09-01 against **hypothesis 6.167.1** and **pytest 9.1.1** (CPython 3.12.13), in two places: a host venv, and inside a container built `FROM python:3.12-slim-bookworm` with `pip install "pytest==9.1.1" hypothesis` — i.e. the eval base image's pins. The container and host agreed on every row.

#### 1a. A property-based suite really is a coin flip, and here is the number

Probe repo, one file, run with the pinned `-q -p no:cacheprovider`, a **fresh tree with no `.hypothesis/` before every run**:

```python
from hypothesis import given, strategies as st

@given(st.integers(min_value=0, max_value=200))
def test_rare(n):
    assert n != 137
```

```
no seed, default profile, 10 fresh runs : 0 0 0 0 1 1 1 1 0 0
CI=1 (derandomize), 6 fresh runs        : 1 1 1 1 1 1
--hypothesis-seed=0, 6 fresh runs       : 1 1 1 1 1 1
--hypothesis-seed=1, 6 fresh runs       : 1 1 1 1 1 1
--hypothesis-seed=2, 6 fresh runs       : 1 1 1 1 1 1
--hypothesis-seed=3, 6 fresh runs       : 1 1 1 1 1 1
```

Six green and four red for the same code on the same tree. That is `HARVESTING.md`'s "lucky draw" bullet, measured rather than asserted, and it is why this broadening is a *determinism* change and not an *acceptance* change. Under either lever, six of six agree.

(The seeds all happened to find this bug, so these rows show run-to-run stability and **not** seed-to-seed disagreement. A separate probe, `n % 97 != 42` over `0..10_000`, disagrees across seeds — 17 of 30 seeds fail — and `--hypothesis-seed=0` passes it 3/3 while `--hypothesis-seed=1` fails it 3/3. Both facts matter: the draw is stable per seed and arbitrary across seeds.)

#### 1b. `HYPOTHESIS_PROFILE` is not an environment variable

The brief assumes one. There isn't one in 6.167.1:

```
$ grep -rn "HYPOTHESIS_PROFILE" <venv>/lib/python3.12/site-packages
exit=1  (no matches)
```

The `HYPOTHESIS_PROFILE` that appears in `pytest --help` is argparse's auto-generated **metavar** for `--hypothesis-profile`, not a variable anything reads. And `--hypothesis-profile=<unregistered>` is not a soft failure: it raises inside `pytest_configure`, printing an `INTERNALERROR>` traceback ending

```
INTERNALERROR> hypothesis.errors.InvalidArgument: Profile 'doesnotexist' is not registered
----- exit code: 3
```

with **zero tests run**. `_explain(3)` already reads "pytest hit an internal error", so a preflight refusal there would be accurate but unhelpful. This kills any design that names a profile the manifest asks the repo to register — which is what the start state forbids anyway (§5.2, and `materialize` builds base_sha + the test half and nothing else).

#### 1c. There *is* a built-in, pre-registered profile, loaded from the environment

`hypothesis/_settings.py:1227-1239`, at import time:

```python
CI = settings(
    derandomize=True,
    deadline=None,
    database=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.register_profile("ci", CI)


if is_in_ci():
    settings.load_profile("ci")
```

and `_settings.py:392-413`:

```python
_CI_VARS = {
    "CI": None,  # various, including GitHub Actions, Travis CI, and AppVeyor
    ...
}


def is_in_ci() -> bool:
    return any(
        key in os.environ and (value is None or os.environ[key] == value)
        for key, value in _CI_VARS.items()
    )
```

`"CI": None` means **presence alone** — any value, including the empty string. Confirmed at runtime in the container:

```
$ CI=1 python -c "import os; from hypothesis._settings import is_in_ci, settings; print(is_in_ci(), settings._current_profile); s=settings.get_profile('ci'); print(s.derandomize, s.database, s.deadline)"
is_in_ci: True
profile: ci
derandomize: True database: None deadline: None
```

`derandomize=True` seeds each test from its own name, so the draw is a pure function of (test name, tree) with no RNG. `database=None` removes the example database. `deadline=None` removes Hypothesis's 200 ms per-example deadline, which is its own machine-speed flake source and one this eval would otherwise hit on a loaded host — `HostSampler` already records that hosts here are contended.

`derandomize=True` **implies** `database=None` (`_settings.py:684-690` raises `InvalidArgument` if you pass a non-`None` database with it), so the two are one lever, not two.

#### 1d. Hypothesis writes into the tree, and `git status --porcelain` does not see it

Measured in the container, a failing property, `-q -p no:cacheprovider`:

```
--- .hypothesis tree ---
.hypothesis
.hypothesis/constants
.hypothesis/constants/94fdebed9607acca
.hypothesis/.gitignore
.hypothesis/examples
.hypothesis/examples/04e6b3400353b141
.hypothesis/examples/04e6b3400353b141/5923ec2bf8c85c50
...
--- git status --porcelain ---
[end status]
--- git status --porcelain --ignored ---
!! .hypothesis/
```

Because Hypothesis writes its own `.gitignore` containing `*` (`configuration.py:29-40`), and only when it creates the directory (`configuration.py:47-54`):

```python
    def create_if_missing(self) -> None:
        existed_before = self.home_directory.exists()
        self.path.mkdir(parents=True, exist_ok=True)
        if not existed_before:
            p = self.home_directory / ".gitignore"
            p.write_text(_GITIGNORE_STRING, encoding="utf-8")
```

**So preflight's tree-clean check does not fire on a hypothesis suite today** — and `git add -A` does not sweep it either, so it never reaches a §5.6 submission diff. Measured directly: pre-create `.hypothesis/examples` by hand, run until a failure records, and `git status --porcelain` prints `?? .hypothesis/`. The guard is `existed_before`, so the only shapes that dirty the tree are a repo that ships a `.hypothesis/` in git, or tooling that pre-creates one.

Two more properties of that directory:

- It is `Path.cwd() / ".hypothesis"` evaluated at **import time** of `hypothesis.configuration` and cached in a module global (`configuration.py:20`). The harness runs pytest at `workdir=/repo`, so it lands at `/repo/.hypothesis` — but **an agent that runs pytest from a subdirectory gets a second, differently-placed copy**, and that one is under a directory whose `.gitignore` guard has never fired.
- `.hypothesis/constants/` survives `CI=1` — `database=None` removes `examples/` and not the constant-mining cache. Whether it is written **at all** is conditional, and the condition is not obvious: measured, `CI=1` on a suite importing a local module wrote `.hypothesis/constants/91af60eb2a35462a`, while `CI=1` on a passing suite importing no local module wrote **nothing**. Relocating the storage root is the only thing that makes that answer unconditional. Measured: with `HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp` the repo tree has no `.hypothesis` at all and `/tmp/hyp` holds `constants/`, `unicode_data/` and `.gitignore`. A harness that relies on "it is usually not written" is relying on a property of the task's imports.

#### 1e. Two adjacent knobs that look like fixes and are traps

- **`HYPOTHESIS_DATABASE_FILE` is removed and setting it is a hard error** (`database.py:89-93` raises `HypothesisException`), which surfaces as a nonsense failure on the *test*: `E assert None is not None`, `FAILED tests/test_prop.py::test_rare - assert None is not None`. Every property test in the suite fails for a reason that names nothing.
- **`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` silently degrades `@given` to a single invocation.** Measured: a test that should sample 100 examples prints `1 passed in 0.07s`. That is a false green, not a determinism fix, and it would also break any repo relying on other autoloaded plugins.

Neither goes anywhere near this design; both are named here because both are one search away.

#### 1f. A hypothesis failure prints an ordinary summary line

```
=========================== short test summary info ============================
FAILED tests/test_fail.py::test_no_dupes - assert 1 == 2
1 failed in 0.31s
```

`_FAILED_LINE` matches it, `failed_node_ids` returns the node id, and the grader's `-q` summary parse is unaffected. **No parser in this repo changes for this broadening.** That is worth stating because it is the thing a reviewer will assume needs changing.

#### 1g. Determinism is per-tree, and the coupling runs through the code under test

This is the finding that decides what `HARVESTING.md` may promise, and the first draft of this plan got its mechanism wrong. The claim there — "adding one unrelated file to the repo flips the verdict" — **does not reproduce**. Measured, adding a file at the repo root holding the falsifying literal changed nothing:

```
MAGIC=1370 + unrelated.py holding 137 (NOT imported) :  0 0 0
```

What *does* flip the verdict is Hypothesis's constant mining over the modules the suite **imports**. Same probe, same `CI=1`, same test body (`@given(st.integers(0, 1_000_000_000))`, `assert n != 137`), varying only a literal in an imported `magic.py`:

```
MAGIC=137   (imported)  CI=1 exits: 1 1 1 1 1 1     <- bug found, 6/6
MAGIC=1370  (imported)  CI=1 exits: 0 0 0 0 0 0     <- bug missed, 6/6
MAGIC=137   (imported)  CI=1 exits: 1 1 1 1 1 1     <- found again, 6/6
MAGIC=999   (imported)  CI=1 exits: 0 0 0 0 0 0     <- missed, 6/6
```

One in a billion, found deterministically, because the literal `137` was sitting in a module the test imports and Hypothesis mined it into the example pool. Change that literal and the same test stops finding the same bug — six times out of six, in both directions, reversibly.

That is a worse hazard than a random draw and a sharper one than "the agent edits the tree". **The example pool is a function of the literals in the imported source, which is exactly what the agent is being asked to change.** So on a property-based task the oracle's strictness is coupled to the shape of the fix: a submission that happens to write the right constant is judged by a strictly stronger example set than one that does not, and the two are graded as if the same test ran. §5.7's N=3 repeats see none of it — all three repeats draw the same examples — and preflight's red-before/green-after verdict, taken on the *reference* fix's tree, does not transfer to a submission's tree.

None of that argues against this broadening: an undetermined coin flip is worse than a determined coupling, and only the second can be reasoned about. It argues for a Layer 2 rule about which property-based suites are admissible, which Task 5 writes.

### 2. The lever is `CI`, not a seed, and the difference is what happens on every *other* task

Both levers give determinism (§1a). They differ on what they do where they do not apply:

| | hypothesis installed | hypothesis **not** installed |
|---|---|---|
| `CI=1` in the environment | `ci` profile: `derandomize=True`, `database=None`, `deadline=None` | **inert** — measured, `1 passed`, exit 0 |
| `--hypothesis-seed=0`, CLI **or** `PYTEST_ADDOPTS` | seed pinned, database bypassed | **exit 4**, before collection |

The exit-4 text, measured identically for both delivery routes in a container with pytest 9.1.1 and no hypothesis:

```
ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]
pytest: error: unrecognized arguments: --hypothesis-seed=0
  inifile: None
  rootdir: /w
```

That asymmetry is what makes `CI` the honest key. It is declared per task either way, so a seed flag would not *actually* leak onto other tasks — but "safe only because nobody made a mistake" is not the standard here, and exit 4 is now, since broadening 2, a code preflight partially *accepts*: an `image.env` typo that produced it would land in the `collection_error_modules` branch with an empty reported set and be refused with a message about f2p modules. Under `CI` there is no such shape.

`CI=1` is also not a lie. This *is* an automated, non-interactive, batch test run; §5.2 pins the session config precisely so that it is one. The rejected alternative was to set a **vendor** variable instead — `TEAMCITY_VERSION` or `CODEBUILD_BUILD_ID`, both of which `_CI_VARS` accepts on presence alone — to get Hypothesis's `ci` profile without touching the one variable the wider Python ecosystem branches on. Refused: that is asserting something false about the environment in order to obtain a side effect, which is the same shape as reporting configuration as observation, and the next reader would have no way to know why the image claims to be TeamCity.

Two residuals, and neither is waved away.

**The repository may branch on it.** `CI` is a general-purpose signal, and a repo's own `conftest.py`, `tox.ini` or source may read it. What that costs is bounded, because **preflight re-measures the whole red-before / green-after ladder in the image that carries the variable** — so whatever the repo does under `CI=1` is what the gate proved, what the grader runs, and what the agent sees. It is a §6.4 confound to record (the arms are scored on the repo's CI-mode suite, not its dev-mode suite), not a correctness hole. It goes in `HARVESTING.md` (Task 5).

**Claude Code itself branches on it, and this was measured rather than left open.** Grepping the bundled `claude` 2.1.220 inside `bakeoff-eval-agent:base` (271,825,824 bytes, text-mode `grep -a`):

| pattern | occurrences | what it is |
|---|---|---|
| `"CI" in env` | 1 | bundled `supports-color`'s block, which selects terminal **colour depth** |
| `CI_ENVS` | 3 (`CI_ENVS = {`, `in CI_ENVS)`, `CI_ENVS[name]`) | the vendor table that same block consults |
| `process.env.CI` | 1 — and it is `process.env.CIRCLECI` | an environment-**name** function returning `"circleci"` / `"buildkite"` / `"ci"` / `"kubernetes"` |
| `isCI` | 8 | **7 are false positives** — `isCIDR`, from bundled zod. The other is `isCI:Yt(!1)`, a field in an environment-description object |
| `"CI"` in an env-name list | 1 | beside `CLICOLOR`, `CLICOLOR_FORCE`, `DEBIAN_FRONTEND`, `GIT_TERMINAL_PROMPT` |

So the CLI **does** read `CI`, and every branch found is colour selection or an environment label. Both should be inert for a non-TTY `claude -p --output-format stream-json` run — `supports-color` already returns 0 with no TTY — but "inert by reading a minified 272 MB bundle" is not this repo's standard. The plan settles it by running the offline smoke gate against an image that carries the variable (Final verification), which exercises the real CLI on the real transport. `TASKS.md` keeps only what that cannot answer: whether a *live* run differs.

### 3. Mechanism (B): a manifest `image.env`, baked as Dockerfile `ENV`

Three candidates were on the table. (B) wins, and the deciding measurement is that **the agent is never told `tests.runner`.**

`execute_run` hands the agent `task.prompt` and nothing else (`runner.py:1175`). `tests.runner` has exactly four consumers, all inside the harness:

```
src/bakeoff/preflight.py:  _Runner(container, tests.runner, timeout_s)
src/bakeoff/oracle.py:271  runner = _Runner(container, task.tests.runner, timeout_s)
src/bakeoff/grader.py:859  runner = _Runner(env, task.tests.runner, GRADE_TIMEOUT_S)
src/bakeoff/grader.py:966  runner = _Runner(env, task.tests.runner, GRADE_TIMEOUT_S)
```

**(A) — `--hypothesis-seed=N` appended to `tests.runner`.** Refused. The gate, the oracle and the grader would run a deterministic suite while the agent runs a random one, because the agent types its own `pytest` command and has never seen the manifest. §3.3 measures a loop that ends in "runs tests, sees failures, self-corrects", so under (A) the loop being measured operates on a *different suite* from the one the gate proved discriminates and the grader scores: the agent can watch a test go green that the grader will call red, and correct away from a fix that was already right. That is the stale-`.pyc` defect with a different cause — an agent corrected away from the right answer and scored as capability — which is exactly why `PYTHONDONTWRITEBYTECODE=1` is an image `ENV` and not a `-B` on one runner. (A) also touches the argv, which is the one thing this repo has a literal-valued test guarding.

**(C) — a manifest `tests.env` applied per exec.** Refused, and it is (A) with more code. Four call sites (`_Runner.run`, `oracle._derive`'s container execs, `grader._ContainerEnv.exec`, `execute_run`'s `container_env`) would each have to remember to pass it, a fifth would be added by the next broadening, and the agent's *invented* commands — the ones §3.3 is about — still get nothing. It is a denylist-shaped fix for an allowlist-shaped problem.

**(B) — `image.env`, rendered as `ENV` in the generated task Dockerfile.** Accepted. It is the shape `PYTHONDONTWRITEBYTECODE=1` already takes in `docker/eval-agent.Dockerfile`, for the reason written there in full:

> Never `-B` on one runner: the agent runs its own commands and this has to hold for every process in the container, including the ones it invents.

It changes no argv, so `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` stays literally true. It reaches every consumer without any consumer being modified, because of decision 4. And it is already covered by the preflight cache key twice over: `manifest_digest` hashes the raw manifest bytes, and `image` is the image **id**, which moves when a layer does.

### 4. Docker merges the image environment into every exec — measured, not assumed

The whole of (B) rests on this, and `container_env`'s docstring already asserts it ("Docker merges this with the image's environment"). Measured directly, 2026-09-01, Docker 29.5.2, against an image declaring `ENV PYTEST_ADDOPTS=--hypothesis-seed=0 IMAGE_ONLY_KEY=from_image PYTHONDONTWRITEBYTECODE=1`, driven through the same `docker` Python client `RunContainer` uses:

```
--- exec env=None (exit 0) ---
PYTEST_ADDOPTS=--hypothesis-seed=0
IMAGE_ONLY_KEY=from_image
PYTHONDONTWRITEBYTECODE=1
ANTHROPIC_MODEL=

--- exec env={'ANTHROPIC_MODEL':'x'} (exit 0) ---
PYTEST_ADDOPTS=--hypothesis-seed=0
IMAGE_ONLY_KEY=from_image
PYTHONDONTWRITEBYTECODE=1
ANTHROPIC_MODEL=x

--- exec env overriding PYTEST_ADDOPTS (exit 0) ---
PYTEST_ADDOPTS=--override
IMAGE_ONLY_KEY=from_image
PYTHONDONTWRITEBYTECODE=1
ANTHROPIC_MODEL=
```

The rule is **merge, with the exec's keys winning**. So:

- `preflight._Runner.run` → `container.exec(argv)` with no `env` → sees `image.env`.
- `oracle._derive`'s execs → same → sees `image.env`.
- `grader._ContainerEnv.exec(argv)` → `self.container.exec(list(argv))`, no `env` → sees `image.env`.
- The agent → `exec_stream(..., env=container_env(config))`, and `container_env` returns `_eval_env(config)` only, which names none of the allowed keys → sees `image.env`.
- Every command the agent invents inside that container → sees `image.env`.

And `RunContainer.__enter__` passes no `environment=` to `containers.run`, so the image's environment reaches the container intact in the first place.

The load-bearing negative is the third row: an exec env key **shadows** the image's. That is why decision 6 exists.

### 5. `image.env` is a key ALLOWLIST, not a free map

`claude_runner`'s module docstring names the contaminant this repo fears most:

> an env allowlist rather than a denylist. A denylist has to anticipate every contaminant; the one that matters most is `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`, either of which makes the CLI ignore `ANTHROPIC_BASE_URL` and call the provider directly — bypassing the proxy and leaving the mandatory wire log (section 6.2) empty, with the run still looking normal.

An unrestricted `image.env` re-opens that hole from the other side: those two variables are kept out of the *host* environment by `PASSTHROUGH_ENV`, and `container_env` does not set them — so an image `ENV CLAUDE_CODE_USE_BEDROCK=1` would reach the agent, bypass the proxy, and produce a run that looks entirely normal with an empty wire log. A denylist here would have to anticipate that plus `PATH`, `HOME`, `CLAUDE_CONFIG_DIR`, `PYTHONDONTWRITEBYTECODE` and whatever Claude Code adds next.

So the loader accepts only keys in a closed set, which for this broadening is exactly two:

```python
_IMAGE_ENV_ALLOWED = {
    "CI",
    "HYPOTHESIS_STORAGE_DIRECTORY",
}
```

Each entry is admitted once, with the argument recorded beside it. A key not in the set is a load error naming the set. Adding a key later is a one-line change plus a comment — which is the point: the argument gets made rather than assumed.

`HYPOTHESIS_STORAGE_DIRECTORY` additionally carries a **value** rule: it must be absolute and must not be `/repo` or under it. Without it the key can be pointed back into the tree and undo the only thing it is there for, and "what is written into the graded tree" would become a property of a string an author typed. Same class as `strip_paths`' pathspec refusal. `/repo` is spelled as a local constant in `tasks.py` rather than imported from `container.REPO_MOUNT`, because `container.py` imports `docker` at module level and `tasks.py` deliberately does not depend on the daemon; a test pins the two equal, which is where that drift belongs.

Key and value syntax are constrained too: a key must match `[A-Za-z_][A-Za-z0-9_]*`, and a value may not contain a newline, a carriage return, a `"`, a `\` or a `$`. The `$` refusal is not cosmetic — Docker expands `$VAR` in an `ENV` value against the build environment, so `ENV X="$PATH"` would make the recorded value a property of the builder rather than of the manifest, which is the `strip_paths` glob argument one subsystem over.

### 6. `image.env` cannot shadow a pinned key, and the check is derived rather than copied

Decision 4's third row: an exec env key beats the image's. So even with the allowlist, the two sets must be proven disjoint — otherwise a future allowlist entry could name a key `_eval_env` also sets, and the image value would be silently ignored on the agent's process while applying to preflight's and the grader's. That is two environments for one task, and nothing would say so.

Derived, never hand-copied — the `_GRADING_KEYS` precedent. `claude_runner` grows:

```python
def pinned_env_keys() -> frozenset[str]:
    """Every environment key the harness itself decides, for `tasks.py` to refuse.

    Derived from `_eval_env` and `PASSTHROUGH_ENV` rather than listed, for
    `_GRADING_KEYS`' reason: a hand-written copy goes stale the first time a
    key is added, and its failure is the silent one -- a task image would set
    a key the agent's exec then overrides, so preflight and the grader see one
    environment and the agent sees another, with nothing in the record saying
    which.

    `custom_headers` is a non-empty sentinel because `_eval_env` only emits
    ANTHROPIC_CUSTOM_HEADERS when it is set; with the default "" that key
    would be missing from the set it exists to define.

    PYTHONDONTWRITEBYTECODE is added by hand and is the one member that
    cannot be derived: it is the BASE IMAGE's ENV, not this process's. A task
    that overrode it would re-arm the stale-pyc defect that already made
    verify_logger.py fail on 2 of 3 consecutive runs.
    """
```

`tasks.py` imports it **inside `_env_map`**, not at module level — the shape `build_task_image` already uses for `from bakeoff.tasks import ensure_mirror`. There is no cycle to break (`claude_runner` imports `hashlib`, `json`, `os`, `subprocess`, `time`, `dataclasses`, `pathlib`, `typing` and nothing from `bakeoff`), so this is about the import *graph* rather than about correctness: `tasks.py` is the input layer and it is read by people cutting tasks, and a module-level line making the manifest loader depend on the agent driver would say something about the architecture that is not true. The dependency is real and it is one function wide.

The allowlist and the refusal are both applied: a key must be in `_IMAGE_ENV_ALLOWED` **and** must not be in `pinned_env_keys()`. Belt and braces on purpose — the allowlist is what an author reads, and the disjointness assertion is what catches an allowlist entry added carelessly later. A test asserts the two sets are disjoint *today*, so adding `CLAUDE_CONFIG_DIR` to the allowlist in a year fails the suite rather than the eval.

### 7. `ENV` goes AFTER the build steps

`render_dockerfile` emits, in order: `FROM`, `USER root`, the apt `RUN`, the pip `RUN`, `COPY repo /repo`, the `image.build` `RUN`s, `RUN chown -R eval:eval /repo`, `USER eval`, `WORKDIR /repo`, `ENTRYPOINT []`.

The `ENV` lines go **after `RUN chown -R eval:eval /repo` and before `USER eval`** — i.e. after every `RUN` in the file, the chown included. Placement does not change the final image config — an `ENV` anywhere in the file lands in it — but it decides whether the **build** sees the value. It must not:

- `image.build` is `pip install -e .` on the click task and arbitrary shell in general. `CI=1` changes pip's own output handling and is read by a great many build scripts, so a build that succeeded when the author measured it could start failing, or succeeding differently, for a variable declared to make a *test runner* deterministic.
- The image build is not the graded suite. Nothing in `image.build` is measured by preflight's ladder, so a value that leaked into it would be an unmeasured difference in an artifact every arm shares.

The chown is the one that is easy to get wrong: it is emitted *after* the `image.build` loop, so "before the `lines.extend` block" and "after every RUN" are not the same position, and only the second is what decision 7 is about. Emitted one `ENV` line per key, sorted, values double-quoted. One line per key rather than a single continuation-joined block because a `#` comment cannot be interleaved into a backslash-continued `ENV` and the generated file is documentation as much as instructions — the base image's own `PYTHONDONTWRITEBYTECODE` block is the example. Sorted so the Dockerfile text, and therefore the image id, is a pure function of the manifest and not of dict insertion order.

### 8. Nothing new reaches the record, and `SCHEMA_VERSION` does not move

The question is whether a `RunRecord` should carry the task's declared env keys. It should not, and the reason is the invariant itself: **configuration is never reported as observation.** `image.env` is what the manifest *asked for*. Copying it into the record would publish a manifest field under the provenance of a measurement, exactly as `sampling` read from the config file would.

What the record already carries is the observation: `Versions.container_image_digest` is the image **id**, taken with `docker inspect --format '{{.Id}}'` on the built image (`images.image_id`, and `RunContainer` refuses a tag). An id is a content pin over every layer, and an `ENV` line is a layer. So a record already answers "what environment did this run's container have" in the only way that cannot be wrong — by naming the artifact rather than the request. Two records with the same `container_image_digest` had the same `image.env`; two with different digests may or may not, and the manifest plus `task_set_commit` resolves it. Adding a field would give a reader a *second* answer with worse provenance.

`SCHEMA_VERSION` therefore does not move. It moves for additive fields precisely because a reader that cannot tell versions apart reads an absent field as a positive negative claim — and no field is being added.

`GRADER_VERSION` does not move either. No check changes what it means, no ladder branch is added, and the grader's argv is byte-identical. The grader *behaves* differently on a hypothesis task, but only because its container's environment differs — which `GradeRecord`'s existing `image` field names.

### 9. What preflight asserts, and why the read-back is not redundant

`image.env` is configuration. The observation is the container's actual environment, and preflight already has the container. It reads each declared key back with `printenv` and refuses a mismatch.

Measured 2026-09-01 in the probe image, which is why `printenv` and not `sh -c 'echo $KEY'`:

```
printenv SET_VAL   -> exit 0 stdout=[xy]
printenv SET_EMPTY -> exit 0 stdout=[]
printenv NOT_SET   -> exit 1 stdout=[]
```

Exit code separates "set to the empty string" from "not set at all", which `echo` cannot. So the evidence records `""` and `None` as different values — the "a null says which kind of null it is" invariant, in the one place this broadening can honour it cheaply.

This is not redundant with "the image was built from a Dockerfile we generated". Three ways it can be wrong without it:

- A stale image. `build_task_image` tags `bakeoff-task-<id>:v<version>` and `image_id` reads that tag back; a task edited without bumping `task_version`, or a build that failed after tagging, leaves the tag pointing at an older layer stack. The manifest digest moves, the image id does not have to.
- A base image whose own `ENV` later collides. `PYTHONDONTWRITEBYTECODE` is already there; the next one added upstream could be an allowlist member.
- Emitting the lines in the wrong place, or not at all, in a future edit to `render_dockerfile`. Task 2's unit tests pin the string; only the read-back pins the *image*.

An un-applied `image.env` is otherwise invisible in the worst way: the suite becomes nondeterministic again, the gate passes on a lucky draw, and every arm is scored against an oracle that answers differently per run. Same class as the `strip_paths` read-back, and it gets the same shape — declared and observed both written unconditionally, so "this task declares no env" and "the gate did not look" do not render identically.

Five evidence keys:

```python
evidence["image_env_declared"]           = declared    # dict[str, str], possibly {}
evidence["image_env_observed"]           = observed    # dict[str, str | None], or None
evidence["image_env_mismatch"]           = mismatch    # list[str] sorted, or None
evidence["hypothesis_importable"]        = importable  # bool, or None
evidence["hypothesis_imported_by_suite"] = used        # bool or None, or None
```

**`declared` is written on every path; the other three are `None` on the path that never reaches a container.** `preflight` returns early at `preflight.py:405-417` when `tests.runner` names no pytest, *before* `RunContainer` — the same hole `stripped_paths` has today, and this plan does not fix that one but must not add a second. Writing `observed={}` / `mismatch=[]` there would be worse than omitting them: an empty mismatch list is a *claim* that the gate looked and found agreement. `None` says which kind of absence it is, which is the same rule `cache_state.warm` and `wire_unattributed` already follow.

**A second check runs in the other direction, and it is the one that catches the manifest nobody wrote.** The read-back compares declared→observed, so a property-based task whose author simply never declared `CI` passes every assertion above: `declared == {} == observed`, mismatch empty, and the ladder then runs on a lucky draw with the gate reporting GO.

It takes **two** probes, not one, and the second is what keeps the refusal honest. *Installed* is not *used*: hypothesis arrives transitively in plenty of environments — a dev-extra, a `pip install -e .[test]` in `image.build`, a dependency of a dependency — and a NO-GO on installation alone would refuse a task whose suite never imports it, with a remedy ("drop it from `image.pip`") that does not exist when nothing put it there directly. So:

```python
# Availability. `-c "import hypothesis"` and not `pip show`: what matters is
# whether the interpreter the RUNNER uses can import it, which a
# site-packages/venv split answers differently -- so the interpreter comes off
# `tests.runner` rather than being hardcoded.
importable = container.exec(
    [_runner_python(tests.runner), "-c", "import hypothesis"]).exit_code == 0

# Reachability. Availability alone is not the claim: hypothesis is a common
# transitive dependency, and refusing a task that merely HAS it would refuse
# suites that never touch it, with a remedy the author cannot apply.
scanned = _present(container, tests.paths)
probe = container.exec(
    ["rg", "-q", r"^\s*(from|import)\s+hypothesis\b", *scanned]
) if scanned else None
```

`rg` is already asserted present a few checks above, so this adds no dependency. Its exit codes are three-valued and are read that way — `0` is a match, `1` is no match, anything else (no readable path, a bad pattern) is **`None`**, "the probe could not answer", never a quiet `False`. `None` also covers "no declared `tests.paths` exists in the tree", which preflight separately records as `SCOPE_PREFIX_MISSING`.

**The NO-GO fires only when the suite imports hypothesis and `image.env` names no `CI`.** Both other combinations are recorded and allowed: importable-but-unimported is the transitive case, and imported-with-`CI` is the configuration this broadening exists to permit. `hypothesis_importable` stays in evidence beside it because the pair is what a reader needs — "the suite imports it and the image cannot" is a different broken task, one the ladder will fail anyway, and one this evidence names.

The check runs with the other environment checks — after the `strip_paths` probe and before `_Runner` is constructed — so a bad environment is reported as itself rather than as five downstream suite failures.

`PREFLIGHT_VERSION` "4" → "5", because that list is what preflight asserts and it grew twice.

### 10. `gitignore_extra` for `.hypothesis/` is REFUSED

The brief offers a choice between `gitignore_extra` (visible in `start_sha`) and an env/flag that disables the database. Measured, the choice is not close:

1. **`gitignore_extra` is a no-op.** §1d: `git status --porcelain` is already clean, because Hypothesis writes `.hypothesis/.gitignore` containing `*`. A `gitignore_extra: [".hypothesis/"]` line would move `start_sha`, require re-pinning and a `task_version` bump, and change nothing observable. A manifest key that *looks* like the fix and does nothing is worse than no key: the next reader believes the hazard is handled.
2. **It cannot reach the actual hazard.** The example database is not a dirty-tree problem, it is a **coupling** problem, and it couples exactly the runs this harness depends on being independent:
   - `preflight` runs the suite five times in **one tree**: f2p-before, p2p-before, f2p-after, p2p-after, scoped-p2p-after. With a database, the red-before run records the falsifying example and the green-after run **replays it first**. Green-after then becomes a strictly stronger check than the grader's check 5, which runs on a fresh tree with no prior database — and the difference is invisible, in neither argv nor record. That is a gated/graded divergence that no argv-identity test can catch.
   - `oracle.derive_quarantine` runs `pass_to_pass` **twice, back to back, in one tree**, and its entire purpose is to tell a flake from a real failure. With a database, run 2 is not a repeat of run 1: it replays run 1's recorded failures. Measured, the database is a one-way ratchet — it can pin a *failing* verdict permanently (3/3 replays of the same example) but cannot stabilise a *passing* one (a passing run followed by `0 1 1`). So the quarantine probe systematically over-reports "reliably red in both runs", which `derive_quarantine` raises on with the message "That is not a flake — the task's own p2p declaration is wrong". A correct task would be refused, and the printed diagnosis would name the wrong cause.
3. **The real hazard has two halves and one key fixes each.** `CI` removes the database (`database=None`) and the randomness (`derandomize=True`). `HYPOTHESIS_STORAGE_DIRECTORY` removes the directory from `/repo` entirely — including the `constants/` cache, which `CI=1` does **not** stop and whose presence is otherwise conditional on what the suite imports, and including the second copy an agent running pytest from a subdirectory would create in a directory whose `.gitignore` guard has never fired. Measured together:

```
$ CI=1 HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypstore2 pytest -q -p no:cacheprovider tests/test_prop.py   # x6
 0 0 0 0 0 0
repo .hypothesis: ABSENT
git status --porcelain: [empty]
```

So: no `gitignore_extra`, no new `.gitignore` line, `start_sha` untouched, and the tree carries nothing the harness put there.

### 11. `PYTEST_ADDOPTS` is not in the allowlist, and here is the measurement that keeps it out

It was the obvious carrier for a seed flag, and it has two disqualifying properties beyond decision 2's exit 4.

**It is prepended to the command line, so the CLI wins on store-type options but count-type options accumulate.** Measured 2026-09-01, pytest 9.1.1:

```
--- CLI has no -k, env -k beta ---
tests/test_ok.py::test_beta PASSED       1 passed, 1 deselected
--- CLI -k beta, env -k alpha ---
tests/test_ok.py::test_beta PASSED       1 passed, 1 deselected
```

The CLI wins for `-k`. But verbosity is a counter: `PYTEST_ADDOPTS="-v"` against the pinned `-q` yields verbosity **0**, not `-q`. Every runner in `bakeoff/taskset/` carries `-q`, and the grader parses pytest's `-q` summary line. An `image.env` able to set `PYTEST_ADDOPTS` could therefore change the output shape the grader reads, from a key declared for an unrelated reason.

**It is unconditional and global.** Every pytest process from that environment gets it — `python -m pytest` and the console script, from any cwd, including commands the agent invents. That is the property that makes it attractive and the property that makes a mistake in it unbounded.

Neither of `CI` nor `HYPOTHESIS_STORAGE_DIRECTORY` can do any of this, which is the argument for a key allowlist rather than a value-checked general one.

### 12. What is deliberately NOT built

- **No `tests.env`, no per-exec env plumbing.** Decision 3.
- **No new `RunRecord` or `GradeRecord` field, no `SCHEMA_VERSION`/`GRADER_VERSION` bump.** Decision 8.
- **No hypothesis in the base image.** A task that needs it declares `image.pip: ["hypothesis==<pin>"]` like any other dependency, or gets it from the repo's own lockfile. Putting it in the base would make every task's image carry a library only some tasks use, and §5.1's "dependencies from lockfile" is per task.
- **No unknown-key rejection for the `image:` block generally.** `grading:` has one and `image:` does not; adding it is a separate, unrelated tightening and would refuse manifests that are currently valid.
- **No new task in `bakeoff/taskset/`.** This broadening makes `attrs`/`cattrs` reachable; cutting a task from them is a §3 exercise in `BUILDING-A-TASK-SET.md`, and their *other* blocker (an editable install colliding with a site-packages `attr`) is unmeasured. `HARVESTING.md` says so rather than implying they are now usable.
- **No `--hypothesis-seed`, `--hypothesis-profile`, `HYPOTHESIS_DATABASE_FILE` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD` anywhere.** Decisions 1e, 2, 11.

---

## File Structure

| file | responsibility in this broadening |
|---|---|
| `bakeoff/src/bakeoff/claude_runner.py` | `pinned_env_keys()` — the derived set of keys the harness itself decides |
| `bakeoff/src/bakeoff/tasks.py` | `TaskImage.env`, `_IMAGE_ENV_ALLOWED`, `_REPO_MOUNT`, `_env_map()` validation, the `load_task` wiring |
| `bakeoff/src/bakeoff/images.py` | `render_dockerfile(..., env=None)` emitting sorted `ENV` lines after the build steps; `build_task_image` passing `task.image.env` |
| `bakeoff/src/bakeoff/preflight.py` | `_observed_env()`, `_declared_env()`, `_runner_python()`, the five evidence keys (declared on every path, four `None` on the pre-container early return), the mismatch refusal, the suite-imports-hypothesis-without-`CI` refusal, `PREFLIGHT_VERSION` "5" |
| `bakeoff/tests/test_claude_runner.py` | `pinned_env_keys()` covers the sentinel-only key and the base image's |
| `bakeoff/tests/test_tasks.py` | the allowlist, the pinned-key refusal, the syntax rules, the `/repo` value rule, the `REPO_MOUNT` equality, `start_sha` unmoved |
| `bakeoff/tests/test_images.py` | placement after the build steps, sorted order, quoting, the degenerate empty case |
| `bakeoff/tests/test_preflight.py` | the read-back, the mismatch refusal, unset vs empty, the early-return nulls, the imports-hypothesis-without-`CI` refusal with its negative and its installed-but-unused case, the unanswerable-`rg` null, `_runner_python`'s six shapes |
| `bakeoff/tests/test_integration_grader.py` | the real-image pin: a task image carrying `image.env` delivers it to `container.exec` **and** to an exec carrying the agent's own env |
| `bakeoff/scripts/mutation_check.py` | one anchor: revert the read-back refusal |
| `bakeoff/taskset/HARVESTING.md` | Layer 1 preflight bullet, Layer 2 "No property-based oracle" rewritten, `attrs`/`cattrs` moved out of Excluded |
| `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` | commented `image.env` example |
| `docs/BUILDING-A-TASK-SET.md` | §3.5 key list, §10 failure-mode table |
| `tasks/todo.md` | the review section |
| `TASKS.md` | the one deliberately-open follow-up (Claude Code's own behaviour under `CI`) |

---

### Task 1: `pinned_env_keys()` and the `image.env` manifest key

**Files:**
- Modify: `bakeoff/src/bakeoff/claude_runner.py` (after `container_env`, ~line 195)
- Modify: `bakeoff/src/bakeoff/tasks.py` (`TaskImage`, a new validator beside `_validate_strip_paths`, and the `TaskImage(...)` construction in `load_task` at ~line 795)
- Test: `bakeoff/tests/test_claude_runner.py`, `bakeoff/tests/test_tasks.py`

**Interfaces:**
- Produces: `claude_runner.pinned_env_keys() -> frozenset[str]`; `tasks.TaskImage.env: dict[str, str]` (default `{}`); `tasks._IMAGE_ENV_ALLOWED: frozenset[str]`; `tasks._REPO_MOUNT: str`. Task 2 reads `task.image.env`; Task 3 reads it through `getattr`.

- [ ] **Step 1: Write the failing tests**

Add to `bakeoff/tests/test_claude_runner.py`, after `test_container_env_does_not_forward_host_paths`:

```python
def test_pinned_env_keys_covers_the_key_that_is_only_set_when_non_empty():
    """The sentinel in `pinned_env_keys` is the whole reason it is a function.

    `_eval_env` emits ANTHROPIC_CUSTOM_HEADERS only when `custom_headers` is
    truthy, and the dataclass default is "". Built from a default config, the
    set would be missing exactly the key that carries the run id -- so a task
    image could set it, the agent's exec would override it, and preflight and
    the grader would read one value while the agent's calls carried another.
    """
    from bakeoff.claude_runner import pinned_env_keys

    keys = pinned_env_keys()

    assert "ANTHROPIC_CUSTOM_HEADERS" in keys
    assert "ANTHROPIC_BASE_URL" in keys
    assert "CLAUDE_CONFIG_DIR" in keys


def test_pinned_env_keys_includes_the_host_allowlist_and_the_base_image_pin():
    """PATH and HOME come from PASSTHROUGH_ENV; PYTHONDONTWRITEBYTECODE comes
    from the base image and is the one member nothing in this process can
    derive.

    A task image overriding PATH would stop `claude` resolving; overriding
    PYTHONDONTWRITEBYTECODE re-arms the stale-pyc defect that already made
    verify_logger.py fail on 2 of 3 consecutive runs and shipped a `.pyc` as
    the first hunk of a live submission diff.
    """
    from bakeoff.claude_runner import pinned_env_keys

    keys = pinned_env_keys()

    assert {"PATH", "HOME", "PYTHONDONTWRITEBYTECODE"} <= keys
```

Add to `bakeoff/tests/test_tasks.py`. **Use the module's existing idiom exactly** — `_write_task(root, upstream, extra_yaml=<raw YAML block>)` returns a *task directory*, and `load_task(task_dir)` is what raises. Do **not** add a keyword to `_write_task`: the `extra_yaml` escape hatch exists precisely so a test can write malformed shapes a keyword-per-key helper could not express, and `image.env` needs exactly that (see the `not-a-mapping` case). Add one import at the top of the file, beside the existing `from bakeoff.tasks import (...)`:

```python
from bakeoff import tasks
```

```python
# --- image.env ---------------------------------------------------------------

_ENV_BLOCK = 'image:\n  env:\n    CI: "1"\n'


def test_image_env_defaults_to_empty_and_an_old_manifest_still_loads(
    tmp_path, upstream
):
    """Every manifest written before this key existed must load unchanged, and
    the absent case has to be ONE value rather than a None every caller
    re-decides -- the reason `grading` is defaulted the same way."""
    task_dir = _write_task(tmp_path / "set", upstream)  # no image: block

    assert load_task(task_dir).image.env == {}


def test_image_env_is_carried_verbatim(tmp_path, upstream):
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            "image:\n"
            '  env:\n'
            '    CI: "1"\n'
            '    HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"\n'
        ),
    )

    assert load_task(task_dir).image.env == {
        "CI": "1",
        "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/bakeoff-hypothesis",
    }


def test_a_key_outside_the_allowlist_is_refused(tmp_path, upstream):
    """An unrestricted image.env re-opens the hole the env ALLOWLIST exists to
    close from the other side: CLAUDE_CODE_USE_BEDROCK is kept out of the host
    environment by PASSTHROUGH_ENV and is not set by container_env, so an image
    ENV carrying it would reach the agent, make the CLI ignore
    ANTHROPIC_BASE_URL, bypass the proxy, and leave the mandatory wire log
    empty with the run still looking normal."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='image:\n  env:\n    CLAUDE_CODE_USE_BEDROCK: "1"\n',
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "CLAUDE_CODE_USE_BEDROCK" in str(exc.value)
    assert "HYPOTHESIS_STORAGE_DIRECTORY" in str(exc.value)  # names the set


def test_an_image_env_that_is_not_a_mapping_is_refused(tmp_path, upstream):
    """`image: {env: []}` is what an author who started a list and never wrote
    the keys leaves behind. `or {}` cannot tell it from absent, so it would
    load as a task declaring no environment -- and a hypothesis suite whose
    determinism lever silently never applied is a suite that sometimes
    passes."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml="image:\n  env: []\n"
    )

    with pytest.raises(TaskError, match="expected a mapping"):
        load_task(task_dir)


def test_the_allowlist_and_the_harness_pinned_keys_are_disjoint():
    """Checked as a property of the two sets, not of one manifest.

    An allowlist entry naming a key `_eval_env` also sets would be silently
    overridden on the AGENT's exec while still applying to preflight's and the
    grader's -- two environments for one task, with nothing in the record
    saying which. This is what makes adding a careless allowlist entry a red
    suite rather than a bad eval."""
    from bakeoff.claude_runner import pinned_env_keys

    assert not (tasks._IMAGE_ENV_ALLOWED & pinned_env_keys())


def test_a_pinned_key_is_refused_even_if_someone_allowlists_it(
    tmp_path, upstream, monkeypatch
):
    """Belt and braces, and the braces are the half that survives a future
    edit: the allowlist is what an author reads, and this refusal is what
    catches an entry added to it without reading decision 6."""
    monkeypatch.setattr(
        tasks, "_IMAGE_ENV_ALLOWED",
        tasks._IMAGE_ENV_ALLOWED | {"CLAUDE_CONFIG_DIR"},
    )
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='image:\n  env:\n    CLAUDE_CONFIG_DIR: "/elsewhere"\n',
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "the harness sets" in str(exc.value)


@pytest.mark.parametrize("literal", [
    '"/tmp/a\\nb"',        # a newline would end the ENV line early
    "'/tmp/a\"b'",         # the value is emitted double-quoted
    "'/tmp/a\\\\b'",       # a backslash continues a Dockerfile line
    '"$HOME/hyp"',         # Docker EXPANDS this against the build environment
])
def test_a_value_that_would_not_survive_a_dockerfile_line_is_refused(
    tmp_path, upstream, literal
):
    """The `$` case is the one that is not about syntax. Docker expands $VAR
    in an ENV value against the BUILD environment, so the recorded value would
    be a property of the builder rather than of the manifest -- the same
    argument that refuses pathspec magic in strip_paths.

    Written as YAML literals rather than Python strings because the loader
    reads YAML: `"a\\nb"` in double quotes is a real newline to the parser,
    which is the shape that has to be refused."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            f"image:\n  env:\n    HYPOTHESIS_STORAGE_DIRECTORY: {literal}\n"
        ),
    )

    with pytest.raises(TaskError):
        load_task(task_dir)


@pytest.mark.parametrize("value", ["relative/path", "/repo", "/repo/.hyp"])
def test_a_storage_directory_inside_the_repo_is_refused(
    tmp_path, upstream, value
):
    """The key exists to keep hypothesis's writes out of the tree the §5.6
    submission diff is taken against. Pointed back into /repo it undoes
    exactly that, and what lands in the tree becomes a property of a string an
    author typed."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            f'image:\n  env:\n    HYPOTHESIS_STORAGE_DIRECTORY: "{value}"\n'
        ),
    )

    with pytest.raises(TaskError) as exc:
        load_task(task_dir)

    assert "/repo" in str(exc.value)


def test_the_repo_mount_constant_matches_the_container_it_describes():
    """tasks.py spells /repo itself rather than importing REPO_MOUNT, because
    container.py imports `docker` at module level and the loader deliberately
    does not depend on a daemon. The drift belongs here."""
    from bakeoff.container import REPO_MOUNT

    assert tasks._REPO_MOUNT == REPO_MOUNT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py tests/test_claude_runner.py -q -k "image_env or pinned_env or repo_mount or storage_directory or allowlist or pinned_key or dockerfile_line"`
Expected: FAIL — `ImportError: cannot import name 'pinned_env_keys'`, `AttributeError: module 'bakeoff.tasks' has no attribute '_IMAGE_ENV_ALLOWED'`, and `AttributeError: 'TaskImage' object has no attribute 'env'`. Confirm the `-k` filter actually selects **all 16 collected items** — 2 in `test_claude_runner.py` and 14 in `test_tasks.py` (9 functions, two of them parametrized 4 and 3 ways). Check with `--collect-only -q | tail -1`; a filter that quietly matches nine of them is a step that passes without checking what it claims to.

- [ ] **Step 3: Add `pinned_env_keys()` to `claude_runner.py`**

Insert after `container_env`:

```python
#: The base image's own ENV, and the one member of `pinned_env_keys` that
#: nothing in this process can derive -- it is set in
#: docker/eval-agent.Dockerfile, not by `_eval_env`. Listed here so the
#: refusal in tasks.py has one source for the whole set.
#:
#: CPython invalidates a .pyc on (source mtime in whole seconds, source size)
#: and both halves are ordinary here, so a task image that turned this off
#: would feed section 3.3's self-correction loop the code the agent already
#: replaced. Measured 2026-08-13 in the eval image, and it made
#: verify_logger.py fail on 2 of 3 consecutive runs.
_BASE_IMAGE_ENV = frozenset({"PYTHONDONTWRITEBYTECODE"})


def pinned_env_keys() -> frozenset[str]:
    """Every environment key the harness itself decides. `tasks.py` refuses these.

    Docker MERGES an exec's environment into the image's, with the exec's keys
    winning (measured 2026-09-01, Docker 29.5.2). So a task image declaring a
    key this function names would be overridden on the AGENT's process --
    which alone gets `container_env` -- while still applying to preflight's
    and the grader's execs, which pass no env. That is two environments for
    one task, and nothing in the record would say which one produced a result.

    DERIVED from `_eval_env` and `PASSTHROUGH_ENV` rather than hand-listed,
    for `_GRADING_KEYS`' reason: a copy goes stale the first time a key is
    added, and the failure of a stale list is the silent one.

    The sentinel matters. `_eval_env` emits ANTHROPIC_CUSTOM_HEADERS only when
    `custom_headers` is truthy and the dataclass default is "", so a default
    config would leave the set missing exactly the key that carries the run id
    -- the one whose loss makes every call unattributed.
    """
    sentinel = ClaudeCodeConfig(
        model="", base_url="", auth_token="", settings_path="",
        config_dir="", max_turns=0, wall_clock_timeout_s=0,
        custom_headers="X-Bakeoff-Run-Id: sentinel",
    )
    return (
        frozenset(_eval_env(sentinel))
        | frozenset(PASSTHROUGH_ENV)
        | _BASE_IMAGE_ENV
    )
```

- [ ] **Step 4: Add the key to `tasks.py`**

`TaskImage` grows a field. `dict`, not `tuple`, and `field(default_factory=dict)` because the dataclass is `frozen=True` but its contents need not be:

```python
@dataclass(frozen=True)
class TaskImage:
    apt: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    #: Environment baked into the task image as Dockerfile ENV lines, so it
    #: reaches EVERY process in the container -- preflight's runner, the
    #: oracle's, the grader's, the agent's `claude`, and the commands the
    #: agent invents. That last one is the whole reason it is here and not in
    #: `tests.runner`: the agent is never told the runner argv (it gets
    #: `task.prompt` and nothing else), so a flag in the runner would leave
    #: the gate measuring one suite and section 3.3's self-correction loop
    #: running another. Same shape, and the same argument, as the base image's
    #: PYTHONDONTWRITEBYTECODE.
    #:
    #: Keys are restricted to `_IMAGE_ENV_ALLOWED`. See its comment: an
    #: unrestricted map re-opens the CLAUDE_CODE_USE_BEDROCK hole that
    #: `claude_runner`'s env allowlist exists to close.
    env: dict[str, str] = field(default_factory=dict)
```

`dict` inside a `frozen=True` dataclass makes `TaskImage` **unhashable** — `hash()` on one raises `TypeError`. Nothing hashes a `TaskImage` today (`manifest_digest` hashes the manifest *bytes*, and no code path puts one in a set or a dict key), and the precedent is already in the file: `TaskManifest` is frozen and carries `provenance: dict`. A `tuple[tuple[str, str], ...]` would keep hashability at the cost of every reader having to `dict(...)` it, so this is the right trade — but it is a real property change, so it is stated rather than discovered.

Beside `_PATHSPEC_MAGIC`, add:

```python
#: Where `RunContainer` binds the run tree. Spelled here rather than imported
#: from `container.REPO_MOUNT`, because container.py imports `docker` at
#: module level and this loader deliberately runs without a daemon.
#: `test_the_repo_mount_constant_matches_the_container_it_describes` is where
#: the drift is caught.
_REPO_MOUNT = "/repo"

#: The only keys `image.env` may set. An ALLOWLIST, not a denylist, for
#: exactly the reason `claude_runner`'s module docstring gives: a denylist has
#: to anticipate every contaminant, and the one that matters most --
#: CLAUDE_CODE_USE_BEDROCK / _USE_VERTEX -- makes the CLI ignore
#: ANTHROPIC_BASE_URL, bypass the proxy, and leave the mandatory wire log
#: empty with the run still looking normal. Those two are kept out of the HOST
#: environment by PASSTHROUGH_ENV and are not set by `container_env`, so an
#: image ENV is precisely the route that is otherwise open.
#:
#: Every entry is argued once, here:
#:
#:   CI  -- Hypothesis registers a built-in `ci` profile at import time
#:          (derandomize=True, database=None, deadline=None) and auto-loads it
#:          whenever any of twelve CI variables is present; `"CI": None` in
#:          its `_CI_VARS` means presence alone, any value. Measured
#:          2026-09-01 against hypothesis 6.167.1: a property-based suite that
#:          gives `0 0 0 0 1 1 1 1 0 0` over ten fresh runs gives `1 1 1 1 1 1`
#:          under CI=1. It is also INERT where it does not apply -- in an
#:          image without hypothesis, `CI=1 pytest` is exit 0 and writes
#:          nothing, where any `--hypothesis-*` flag is exit 4 before
#:          collection.
#:
#:   HYPOTHESIS_STORAGE_DIRECTORY -- Hypothesis's storage root is
#:          `Path.cwd() / ".hypothesis"` fixed at import time
#:          (configuration.py:20), so with workdir=/repo it lands in the tree
#:          the section 5.6 submission diff is taken against. CI=1 stops the
#:          `examples/` database but not the `constants/` cache, and an agent
#:          running pytest from a subdirectory gets a second copy in a
#:          directory whose self-written .gitignore guard never fired. This is
#:          the only lever that keeps all of it out.
_IMAGE_ENV_ALLOWED = frozenset({"CI", "HYPOTHESIS_STORAGE_DIRECTORY"})

#: Characters that do not survive a generated `ENV KEY="value"` line. `$` is
#: the one that is not about syntax: Docker EXPANDS it against the build
#: environment, which would make the value a property of the builder rather
#: than of the manifest -- the argument that refuses pathspec magic in
#: strip_paths, one key over.
_ENV_VALUE_REFUSED = ('\n', '\r', '"', '\\', '$')

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_map(value: Any, where: str) -> dict[str, str]:
    """`image.env`, validated. Empty when absent.

    Four refusals, each naming a failure that is silent without it:

    * a key outside `_IMAGE_ENV_ALLOWED` -- see that constant;
    * a key the harness itself sets -- Docker's exec env wins over the image's
      (measured), so the value would apply to preflight and the grader and NOT
      to the agent, which is two environments for one task;
    * a value carrying `_ENV_VALUE_REFUSED`;
    * HYPOTHESIS_STORAGE_DIRECTORY inside /repo, which undoes the only thing
      that key is for.
    """
    from bakeoff.claude_runner import pinned_env_keys

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TaskError(f"{where}: expected a mapping, got {value!r}")

    pinned = pinned_env_keys()
    env: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not _ENV_KEY.match(key):
            raise TaskError(
                f"{where}: {key!r} is not a usable environment variable name"
            )
        if key in pinned:
            raise TaskError(
                f"{where}: {key!r} is a key the harness sets itself. Docker "
                "merges an exec's environment into the image's with the "
                "exec's keys winning, so this would apply to preflight and "
                "the grader and be overridden on the agent's own process -- "
                "two environments for one task, with nothing recording which"
            )
        if key not in _IMAGE_ENV_ALLOWED:
            raise TaskError(
                f"{where}: {key!r} is not an allowed image.env key. The "
                "allowed set is "
                f"{sorted(_IMAGE_ENV_ALLOWED)}; it is an allowlist because a "
                "denylist would have to anticipate CLAUDE_CODE_USE_BEDROCK, "
                "which bypasses the proxy and leaves the wire log empty with "
                "the run still looking normal"
            )
        if not isinstance(raw_value, str) or not raw_value:
            raise TaskError(
                f"{where}: {key!r} must be a non-empty string, got "
                f"{raw_value!r}"
            )
        bad = [ch for ch in _ENV_VALUE_REFUSED if ch in raw_value]
        if bad:
            raise TaskError(
                f"{where}: {key!r} carries {bad!r}, which would not survive "
                'a generated `ENV KEY="value"` line ("$" is expanded by the '
                "builder, so the value would be a property of the build "
                "rather than of the manifest)"
            )
        if key == "HYPOTHESIS_STORAGE_DIRECTORY":
            path = PurePosixPath(raw_value)
            # `is_relative_to` covers the equal case too -- measured,
            # `PurePosixPath("/repo").is_relative_to("/repo")` is True -- so
            # `/repo` itself and anything under it are one check, and the
            # `is_absolute` clause is what catches a relative entry before
            # `is_relative_to` is asked a question about a path with no root.
            if not path.is_absolute() or path.is_relative_to(_REPO_MOUNT):
                raise TaskError(
                    f"{where}: {raw_value!r} must be an absolute path outside "
                    f"{_REPO_MOUNT}. This key exists to keep hypothesis's "
                    "writes out of the tree the section 5.6 submission diff "
                    "is taken against; pointed back inside it, it undoes "
                    "exactly that"
                )
        env[key] = raw_value
    return env
```

In `load_task`, the `TaskImage(...)` construction gains one line:

```python
        image=TaskImage(
            apt=_strs(image_raw.get("apt"), f"{where}:image.apt"),
            pip=_strs(image_raw.get("pip"), f"{where}:image.pip"),
            build=_strs(image_raw.get("build"), f"{where}:image.build"),
            env=_env_map(image_raw.get("env"), f"{where}:image.env"),
        ),
```

`manifest_digest` needs no term: it hashes the raw manifest bytes, so declaring or editing `image.env` already invalidates the task's preflight cache entry.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py tests/test_claude_runner.py -q`
Expected: PASS, all of them.

- [ ] **Step 6: Prove no existing manifest moved**

Run:

```bash
cd bakeoff && .venv/bin/python -c "
from pathlib import Path
from bakeoff.tasks import load_task
t = load_task(Path('taskset/click-3360-write-usage-empty-args'))
print('image.env      =', t.image.env)
print('declared start =', t.declared_start_sha)
print('digest         =', t.manifest_digest)
"
```

Expected: `image.env = {}`, `declared start = 33575cc0b75608fa5cbcb1d3ae3347b81eac437f`, and a digest that matches whatever `git stash`-ing your changes and re-running prints — the manifest bytes have not changed, so it must be identical.

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: the recorded baseline plus the new tests, no regressions.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/claude_runner.py bakeoff/src/bakeoff/tasks.py \
        bakeoff/tests/test_claude_runner.py bakeoff/tests/test_tasks.py
git commit -m "feat: a manifest can pin container environment, on an allowlist that cannot reach the proxy

A property-based suite is a coin flip: measured 2026-09-01 against
hypothesis 6.167.1, one probe gives 0 0 0 0 1 1 1 1 0 0 over ten fresh
runs of the same code on the same tree, and 1 1 1 1 1 1 under CI=1,
whose built-in `ci` profile is derandomize=True, database=None,
deadline=None. That variable has to reach EVERY process in the
container, because the agent is never told tests.runner -- it gets
task.prompt and nothing else -- so a flag in the runner leaves the gate
measuring one suite while section 3.3's self-correction loop runs
another, and an agent corrects away from a fix that was already right.
That is the stale-pyc defect with a different cause, which is why
PYTHONDONTWRITEBYTECODE is an image ENV and not a -B on one runner.

image.env is a key ALLOWLIST, not a free map. Unrestricted it re-opens
the hole claude_runner's env allowlist exists to close, from the other
side: CLAUDE_CODE_USE_BEDROCK is kept out of the host environment by
PASSTHROUGH_ENV and is not set by container_env, so an image ENV is
precisely the route left open -- and it would make the CLI ignore
ANTHROPIC_BASE_URL, bypass the proxy, and leave the mandatory wire log
empty with the run still looking normal.

pinned_env_keys() is derived from _eval_env and PASSTHROUGH_ENV rather
than copied, for _GRADING_KEYS' reason, with a non-empty custom_headers
sentinel because _eval_env emits ANTHROPIC_CUSTOM_HEADERS only when it
is set -- a default config would leave the set missing exactly the key
that carries the run id.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `render_dockerfile` emits the environment, after the build steps

**Files:**
- Modify: `bakeoff/src/bakeoff/images.py` (`render_dockerfile` signature and body; `build_task_image`'s call at ~line 274)
- Test: `bakeoff/tests/test_images.py`

**Interfaces:**
- Consumes: `tasks.TaskImage.env` from Task 1.
- Produces: `render_dockerfile(base_image: str, apt: list[str], pip: list[str], build: list[str], env: dict[str, str] | None = None) -> str`. The default keeps every existing caller and every existing test valid.

- [ ] **Step 1: Write the failing tests**

Add to `bakeoff/tests/test_images.py`:

```python
def test_env_lines_come_after_every_build_step():
    """Placement does not change the final image config -- an ENV anywhere in
    the file lands in it -- but it decides whether the BUILD sees the value,
    and it must not.

    `image.build` is arbitrary shell (`pip install -e .` on the click task).
    CI=1 changes pip's own behaviour and is read by a great many build
    scripts, so a build that succeeded when the author measured it could start
    failing, or succeeding differently, for a variable declared to make a test
    runner deterministic. Nothing in image.build is measured by preflight's
    ladder, so a value that leaked into it would be an unmeasured difference
    in an artifact every arm shares.
    """
    lines = _lines(build=["pip install -e ."], env={"CI": "1"})
    last_run = max(i for i, line in enumerate(lines) if line.startswith("RUN "))
    env_index = next(i for i, line in enumerate(lines)
                     if line.startswith("ENV CI="))

    assert env_index > last_run
    # ...and still before the USER switch, so the file reads top-to-bottom as
    # root-setup then eval-runtime.
    assert env_index < lines.index("USER eval")


def test_env_lines_are_sorted_so_the_image_id_is_a_function_of_the_manifest():
    """One ENV line per key, in sorted order. Dict insertion order would make
    the Dockerfile text -- and therefore the built image id, which is what
    Versions.container_image_digest records -- depend on YAML key order rather
    than on the manifest's content."""
    forward = _lines(env={"CI": "1", "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/h"})
    reverse = _lines(env={"HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/h", "CI": "1"})

    assert forward == reverse
    assert [line for line in forward if line.startswith("ENV ")] == [
        'ENV CI="1"',
        'ENV HYPOTHESIS_STORAGE_DIRECTORY="/tmp/h"',
    ]


def test_a_task_with_no_env_emits_no_env_line():
    """The degenerate manifest is the common one -- click declares no env --
    and an empty `ENV` line is a build error, not a no-op."""
    assert not [line for line in _lines() if line.startswith("ENV ")]


def test_build_task_image_passes_the_manifests_env_through(tmp_path,
                                                           monkeypatch):
    """The wiring, not the rendering. Dropping `env=dict(task.image.env)` from
    the call in `build_task_image` leaves every render_dockerfile test above
    green while no task image carries any environment at all -- and the
    failure is silent, because a hypothesis suite whose determinism lever
    never applied is simply a suite that sometimes passes.

    Faked exactly the way `test_build_task_image_strips_the_context_it_unpacks`
    fakes it, and for its stated reasons: `git archive` runs through
    `subprocess.run` DIRECTLY (images.py:252), not through `_run`, so patching
    `_run` alone leaves it shelling out to a mirror that does not exist and
    raising `ImageError: git archive ... failed`. `real_run`, captured before
    the patch, keeps every other caller honest -- the patch lands on the
    shared `subprocess` module and is process-wide for its duration.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text("x = 1\n")
    archive = _archive_bytes(source)
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if list(args)[:2] == ["git", "archive"]:
            return subprocess.CompletedProcess(args, 0, stdout=archive,
                                               stderr=b"")
        return real_run(args, **kwargs)

    rendered = {}
    real_render = images.render_dockerfile

    def capture(*args, **kwargs):
        rendered.update(kwargs)
        return real_render(*args, **kwargs)

    monkeypatch.setattr("bakeoff.tasks.ensure_mirror",
                        lambda url, sha, cache: tmp_path / "mirror")
    monkeypatch.setattr("bakeoff.images.subprocess.run", fake_run)
    # `_run` covers `docker build` AND `image_id`, which calls it -- there is
    # no separate `image_id` to patch.
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")
    monkeypatch.setattr("bakeoff.images.render_dockerfile", capture)

    class _Image:
        apt = ()
        pip = ()
        build = ()
        env = {"CI": "1"}

    class _Task:
        task_id = "envwiring"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        strip_paths = ()

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    assert rendered["env"] == {"CI": "1"}
```

Three notes on fitting this into the module. `_lines` already `setdefault`s its keyword arguments; add `kwargs.setdefault("env", {})` to it so every existing `render_dockerfile` test keeps producing the same string. `_archive_bytes` and `import subprocess` are already in the file (used by `test_build_task_image_strips_the_context_it_unpacks`); `import bakeoff.images as images` is not — add it, or write the two `monkeypatch.setattr` targets that need the module object as dotted strings as above and drop the alias, whichever the file already prefers. `_Task`/`_Image` are function-local stubs in the existing test, not module-level fixtures, so define fresh ones here rather than importing them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q -k env`
Expected: FAIL — `TypeError: render_dockerfile() got an unexpected keyword argument 'env'`.

- [ ] **Step 3: Implement**

`render_dockerfile` gains a parameter and four lines. Signature:

```python
def render_dockerfile(base_image: str, apt: list[str], pip: list[str],
                      build: list[str],
                      env: dict[str, str] | None = None) -> str:
```

Extend the docstring with a paragraph:

```
    `env` is emitted AFTER every RUN, which is what keeps it out of the build.
    Placement does not change the final image config, but it decides what the
    build steps see -- and `image.build` is arbitrary shell, so a variable
    declared to make a test runner deterministic must not silently change how
    the image was assembled. Sorted and one line per key, so the file text
    (and therefore the image id `Versions.container_image_digest` records) is
    a function of the manifest rather than of YAML key order.
```

Body, **inside** the `lines.extend([...])` block, on the line after `"RUN chown -R eval:eval /repo",` and before `"USER eval",`. The block is a list literal, so split it: keep the chown entry and its comment in the first `extend`, insert the loop, then `extend` the remaining four entries. After every `RUN` in the file — the chown included, which is emitted *after* the `image.build` loop and is the one this is easy to get wrong about:

```python
    # AFTER every RUN, the chown included. See the docstring: an ENV here reaches every
    # process in the finished container -- preflight's runner, the oracle's,
    # the grader's, the agent's `claude` and the commands the agent invents --
    # and reaches no build step. Docker merges this into every exec's
    # environment, with the exec's own keys winning (measured 2026-09-01,
    # Docker 29.5.2), and `claude_runner.container_env` sets none of the keys
    # `tasks._IMAGE_ENV_ALLOWED` permits.
    for key in sorted(env or {}):
        lines.append(f'ENV {key}="{env[key]}"')
```

`build_task_image`'s call:

```python
        render_dockerfile(
            base_image,
            list(task.image.apt),
            list(task.image.pip),
            list(task.image.build),
            env=dict(task.image.env),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q`
Expected: PASS, including every pre-existing test — `_lines`' new `setdefault` keeps them producing the same string.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/images.py bakeoff/tests/test_images.py
git commit -m "feat: a task's environment is baked into its image, after the build and never into it

Docker merges an image's environment into every exec, with the exec's
own keys winning (measured 2026-09-01, Docker 29.5.2, through the same
client RunContainer uses). So an ENV in the generated task Dockerfile
reaches preflight's runner, the oracle's, the grader's, the agent's
`claude` and every command the agent invents -- none of which change --
because container_env sets only _eval_env's keys and image.env cannot
name one of those.

The lines go AFTER every RUN. Placement does not change the final image
config, but it decides what the build sees, and image.build is
arbitrary shell: CI=1 changes pip's behaviour and is read by many build
scripts, so a variable declared to make a test runner deterministic
must not quietly change how the image was assembled. Nothing in
image.build is measured by preflight's ladder, so that difference would
be unmeasured and shared by every arm.

Sorted, one line per key, so the rendered text and therefore the image
id that Versions.container_image_digest records is a function of the
manifest rather than of YAML key order.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Preflight reads the environment back out of the container

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` (`PREFLIGHT_VERSION`, a helper beside `_present`, and the environment block after the `strip_paths` check at ~line 500)
- Modify: `bakeoff/scripts/mutation_check.py` (one new anchor)
- Test: `bakeoff/tests/test_preflight.py`

**Interfaces:**
- Consumes: `TaskImage.env` from Task 1, reached through `getattr` so a manifest object predating the key cannot crash the gate.
- Produces: `preflight._observed_env(container, keys) -> dict[str, str | None]`, `preflight._declared_env(task) -> dict[str, str]`, `preflight._runner_python(runner) -> str`; evidence keys `image_env_declared`, `image_env_observed`, `image_env_mismatch`, `hypothesis_importable`, `hypothesis_imported_by_suite`; `PREFLIGHT_VERSION == "5"`.

- [ ] **Step 1: Write the failing tests**

Add to `bakeoff/tests/test_preflight.py`, near the `strip_paths` tests:

```python
def test_a_declared_env_that_did_not_reach_the_image_is_a_problem(
    monkeypatch, tmp_path
):
    """image.env is CONFIGURATION; the container's environment is the
    OBSERVATION, and this is the only place the two are compared.

    An un-applied image.env is invisible in the worst way: the suite goes back
    to being nondeterministic, the gate passes on a lucky draw, and every arm
    is scored against an oracle that answers differently per run. Three ways
    it happens without anyone editing render_dockerfile -- a stale tag (a task
    edited without a task_version bump moves manifest_digest and need not move
    the image id), a base image whose own ENV later collides, and a future
    edit that emits the lines in the wrong place.
    """
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={})  # nothing set in the container

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("CI" in problem and "image.env" in problem
               for problem in result.problems)
    assert result.evidence["image_env_mismatch"] == ["CI"]


def test_a_declared_env_that_matches_is_not_a_problem(monkeypatch, tmp_path):
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={"CI": "1"})

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["image_env_observed"] == {"CI": "1"}
    assert result.evidence["image_env_mismatch"] == []


def test_both_env_keys_are_written_even_when_nothing_is_declared(
    monkeypatch, tmp_path
):
    """"This task declares no environment" and "the gate did not look" render
    identically as a missing key, and a cached verdict outlives the code that
    wrote it. Same rule as stripped_paths / stripped_paths_present."""
    task = _FakeTask()  # no image.env
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["image_env_declared"] == {}
    assert result.evidence["image_env_observed"] == {}
    assert result.evidence["image_env_mismatch"] == []


def test_an_unset_variable_is_recorded_as_null_and_an_empty_one_as_empty(
    monkeypatch, tmp_path
):
    """Two different absences that render identically are the same defect one
    layer down. `printenv` is what separates them and `echo $KEY` is not:
    measured 2026-09-01, `printenv NOT_SET` exits 1 with empty stdout while
    `printenv SET_EMPTY` exits 0 with empty stdout."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={"CI": ""})

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert result.evidence["image_env_observed"] == {"CI": ""}

    absent = _ScriptedContainer(start_sha="s" * 40, tests=task.tests, env={})
    result = _run_preflight(monkeypatch, tmp_path, task, absent)

    assert result.evidence["image_env_observed"] == {"CI": None}


def test_the_env_is_read_before_the_suite_runs(monkeypatch, tmp_path):
    """Order, not presence. A broken environment produces five downstream
    suite failures, and reporting them instead of the cause sends the task
    author round the loop for the wrong reason."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={"CI": "1"})

    _run_preflight(monkeypatch, tmp_path, task, container)

    printenv = next(i for i, cmd in enumerate(container.commands)
                    if cmd[:1] == ["printenv"])
    first_suite = next(i for i, cmd in enumerate(container.commands)
                       if cmd[:1] == ["timeout"])

    assert printenv < first_suite


def test_a_suite_that_imports_hypothesis_with_no_CI_is_refused(monkeypatch,
                                                               tmp_path):
    """The check that runs in the OTHER direction, and the only one that can
    catch the manifest nobody wrote.

    The read-back compares declared against observed, so a property-based task
    whose author never declared CI passes every other assertion in this file:
    declared == {} == observed, mismatch empty, GO. The ladder then runs on a
    lucky draw -- measured, `0 0 0 0 1 1 1 1 0 0` over ten fresh runs -- and
    nothing in the record distinguishes that task from one that never needed a
    lever."""
    task = _FakeTask()  # no image.env
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",),
                                   hypothesis_importable=True,
                                   hypothesis_in_suite=True)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert result.evidence["hypothesis_importable"] is True
    assert result.evidence["hypothesis_imported_by_suite"] is True
    assert any("hypothesis" in problem and "CI" in problem
               for problem in result.problems)


def test_a_suite_that_imports_hypothesis_WITH_CI_is_fine(monkeypatch, tmp_path):
    """The negative, so the check above cannot pass by refusing everything."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), env={"CI": "1"},
                                   hypothesis_importable=True,
                                   hypothesis_in_suite=True)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems


def test_hypothesis_merely_INSTALLED_is_recorded_and_not_refused(monkeypatch,
                                                                 tmp_path):
    """INSTALLED is not USED, and the distinction is the whole of the trigger.

    hypothesis arrives transitively all the time -- a dev-extra, a
    `pip install -e .[test]` in image.build, a dependency of a dependency --
    and a NO-GO on availability alone would refuse a task whose suite never
    imports it, naming a remedy ("drop it from image.pip") the author cannot
    apply because nothing they wrote put it there. Recorded, so a reader can
    still see it; not a problem."""
    task = _FakeTask()  # no image.env
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",),
                                   hypothesis_importable=True,
                                   hypothesis_in_suite=False)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["hypothesis_importable"] is True
    assert result.evidence["hypothesis_imported_by_suite"] is False


def test_an_rg_probe_that_could_not_answer_is_None_and_not_False(monkeypatch,
                                                                 tmp_path):
    """A quiet False here silently disarms the only check that catches an
    undeclared property-based suite.

    rg exits 0 for a match and 1 for none; anything else -- an unreadable
    path, a bad pattern, no rg -- is a probe that did not answer. Two absences
    that render identically as `false` are the same defect one layer down."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), rg_exit=2)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["hypothesis_imported_by_suite"] is None
    assert not any("hypothesis" in problem for problem in result.problems)


def test_no_declared_test_path_exists_so_the_probe_never_ran(monkeypatch,
                                                             tmp_path):
    """`_present` filters the prefixes, for `_existing_prefixes`' reason: a
    declared path absent at the start state is an input this gate tolerates,
    and handing rg a path that does not exist makes it exit 2. With nothing to
    scan the answer is None -- not False."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=(),  # tests/ is not there
                                   hypothesis_importable=True)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["hypothesis_imported_by_suite"] is None
    assert not any(cmd[:1] == ["rg"] for cmd in container.commands)


@pytest.mark.parametrize("runner,expected", [
    (("python", "-m", "pytest", "-q"), "python"),
    (("python3", "-m", "pytest"), "python3"),
    (("/opt/venv/bin/python3.12", "-m", "pytest"), "/opt/venv/bin/python3.12"),
    # Not an interpreter: a console script gives no interpreter path to reuse,
    # so the probe falls back rather than running `pytest -c "import ..."`.
    (("pytest", "-q"), "python"),
    (("pythonish-wrapper", "-m", "pytest"), "python"),
    ((), "python"),
])
def test_the_import_probe_uses_the_runners_own_interpreter(runner, expected):
    """A runner of ["/opt/venv/bin/python", "-m", "pytest"] resolves imports
    against that venv's site-packages, so probing whichever `python` is first
    on PATH answers a question about a different environment -- and answers it
    confidently."""
    from bakeoff.preflight import _runner_python

    assert _runner_python(runner) == expected


def test_the_env_evidence_says_which_absence_it_is_on_the_early_return(
    monkeypatch, tmp_path
):
    """`preflight` returns before RunContainer when tests.runner names no
    pytest, so there is no container to read anything back from.

    `observed: {}` and `mismatch: []` there would be a CLAIM -- that the gate
    looked and found agreement -- for a gate that never started a container.
    None says which kind of absence it is, the same way cache_state.warm and
    wire_unattributed do. `declared` IS written, because it is a property of
    the manifest and is knowable without a daemon."""
    task = _FakeTask(tests=_FakeTests(runner=("make", "test")),
                     image=_FakeImage(env={"CI": "1"}))

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert not result.ok  # the non-pytest runner, which is the existing check
    assert result.evidence["image_env_declared"] == {"CI": "1"}
    assert result.evidence["image_env_observed"] is None
    assert result.evidence["image_env_mismatch"] is None
    assert result.evidence["hypothesis_importable"] is None


def test_the_preflight_version_moved_with_the_new_assertion():
    """It is in the cache key, and it is the only component that moves when
    THIS file changes -- a manifest digest describes the task, an image id the
    environment, a start sha the tree. Without the bump every warm cache
    serves a verdict written by a gate that never looked at image.env."""
    from bakeoff.preflight import PREFLIGHT_VERSION

    assert PREFLIGHT_VERSION == "5"
```

`_ScriptedContainer` grows four keywords — `env: dict[str, str] | None = None` (stored as `self.env = dict(env or {})`), `hypothesis_importable: bool = False`, `hypothesis_in_suite: bool = False`, and `rg_exit: int | None = None` (an explicit override, so a test can script the "could not answer" exit that `hypothesis_in_suite` cannot express) — and three branches, placed immediately after the `test -L` branch and before the `git rev-parse` one. None of them can be swallowed by the existing `cmd[0] == "sh"` catch-all (the probes are `printenv`, an interpreter, and `rg`), so they sit together for readability:

```python
        if cmd[:1] == ["printenv"]:
            # Measured 2026-09-01: `printenv KEY` exits 0 with the value (even
            # when that value is empty) and exits 1 with empty stdout when the
            # key is unset. The exit code is the whole discriminator, which is
            # why the gate does not use `echo $KEY`.
            if cmd[1] in self.env:
                return _Exec(stdout=self.env[cmd[1]] + "\n")
            return _Exec(exit_code=1)
        if len(cmd) >= 3 and cmd[1] == "-c" and "import hypothesis" in cmd[2]:
            # Matched on the SHAPE, not on `cmd[0] == "python"`: the probe
            # takes its interpreter from `tests.runner`, so a test that
            # scripts a venv runner must still be answered here.
            return _Exec(exit_code=0 if self.hypothesis_importable else 1)
        if cmd[:1] == ["rg"]:
            # rg's three exit codes are the point (0 match, 1 no match,
            # anything else could-not-answer), so `rg_exit` overrides the
            # boolean when a test is about the third one.
            if self.rg_exit is not None:
                return _Exec(exit_code=self.rg_exit)
            return _Exec(exit_code=0 if self.hypothesis_in_suite else 1)
```

Note the existing `_ScriptedContainer` answers `test -e` from `present`, so these tests pass `present=("tests/",)` to make `_FakeTests.paths` survive the `_present` filter — without it the probe never runs and `hypothesis_imported_by_suite` is `None`, which is what `test_no_declared_test_path_exists_so_the_probe_never_ran` asserts deliberately.

`_FakeTests` also needs its `runner` to be settable for the early-return test; it is a frozen dataclass with a default, so `_FakeTests(runner=("make", "test"))` already works and no change is required.

and `_FakeTask` grows `image: _FakeImage = field(default_factory=_FakeImage)` with

```python
@dataclass(frozen=True)
class _FakeImage:
    env: dict = field(default_factory=dict)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k "env or preflight_version or hypothesis"`
Expected: FAIL — `KeyError: 'image_env_declared'`, `TypeError: __init__() got an unexpected keyword argument 'hypothesis_importable'`, `ImportError: cannot import name '_runner_python'`, and `assert '4' == '5'`. Twelve new test functions, 17 collected items (`test_the_import_probe_uses_the_runners_own_interpreter` is parametrized six ways); confirm the filter selects all 17 with `--collect-only -q | tail -1` before trusting the run.

- [ ] **Step 3: Implement the probe**

Beside `_present` in `preflight.py`:

```python
def _observed_env(container, keys: tuple[str, ...]) -> dict[str, str | None]:
    """What the container's environment actually holds, per declared key.

    `printenv`, never `sh -c 'echo $KEY'`, because the exit code is the whole
    discriminator: measured 2026-09-01, `printenv KEY` exits 0 with the value
    even when that value is the empty string, and exits 1 with empty stdout
    when the key is unset. `echo` cannot tell those apart, and two different
    absences that render identically are the same defect one layer down.
    `None` here means "not set"; `""` means "set to nothing".

    Only the DECLARED keys are read. A dump of the whole environment would put
    values nobody asked about into `preflight.json`, which outlives the run
    and is read by hand.
    """
    observed: dict[str, str | None] = {}
    for key in keys:
        result = container.exec(["printenv", key])
        observed[key] = (
            result.stdout.rstrip("\n") if result.exit_code == 0 else None
        )
    return observed


def _declared_env(task) -> dict[str, str]:
    """The manifest's `image.env`, or {}.

    `getattr` twice, like `_declared_grading`'s and the strip's: this module's
    entry point takes an untyped `task`, and a manifest object predating the
    key must not crash the gate.
    """
    return dict(getattr(getattr(task, "image", None), "env", {}) or {})


#: Basenames that mean "this argv element is a Python interpreter".
#: `python`, `python3`, and `pythonX.Y` -- the three shapes a `tests.runner`
#: actually carries. Matched on the BASENAME so an absolute
#: `/usr/local/bin/python3.12` counts, and anchored so `pythonish-wrapper`
#: does not.
_PYTHON_BASENAME = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")


def _runner_python(runner: tuple[str, ...]) -> str:
    """The interpreter this task's suite runs under, or "python".

    The availability probe has to ask the interpreter the RUNNER uses, not
    whichever `python` is first on PATH: a task whose runner is
    `["/opt/venv/bin/python", "-m", "pytest", ...]` resolves imports against
    that venv's site-packages, and probing the system interpreter would answer
    a question about a different environment. `click-3360`'s runner is
    `["python", "-m", "pytest", ...]`, so the common case is unchanged.

    Falls back to "python" when `runner[0]` is not an interpreter at all --
    `["pytest", "-q", ...]` is a legal runner, and the console script gives no
    interpreter path to reuse. The fallback is a guess and is allowed to be:
    this probe decides whether to ASK for `CI`, and preflight's runner check
    has already established that `pytest` is in the argv.
    """
    if runner and _PYTHON_BASENAME.match(PurePosixPath(runner[0]).name):
        return runner[0]
    return "python"
```

`re` is already imported in `preflight.py`; `PurePosixPath` is not — add `from pathlib import Path, PurePosixPath` to the existing `pathlib` import.

- [ ] **Step 4: Implement the assertion**

In `preflight`, immediately after the `strip_paths` block and **before** `runner = _Runner(...)`:

```python
        # The environment, checked against the CONTAINER rather than against
        # the manifest. `image.env` is configuration; what the container holds
        # is the observation, and this is the only place the two meet.
        #
        # An un-applied image.env is invisible in the worst way. Its whole job
        # is determinism -- measured 2026-09-01 against hypothesis 6.167.1, a
        # property-based suite gives `0 0 0 0 1 1 1 1 0 0` over ten fresh runs
        # of unchanged code and `1 1 1 1 1 1` under CI=1 -- so a value that
        # did not take means the gate passed on a lucky draw and every arm is
        # scored against an oracle that answers differently per run.
        #
        # Not redundant with "we generated the Dockerfile ourselves": a stale
        # tag (a task edited without a task_version bump moves manifest_digest
        # and need not move the image id), a base image whose own ENV later
        # collides, and a future edit that emits the lines in the wrong place
        # all leave the rendering tests green.
        #
        # `getattr`, like `_declared_grading`'s and the strip's: this function
        # takes an untyped `task`, and a manifest object predating the key
        # must not crash the gate.
        #
        # BEFORE `_Runner` is built, so a bad environment is reported as
        # itself rather than as five downstream suite failures.
        #
        # `image_env_declared` was written before the guard above, on every
        # path including the one that never reaches a container. What this
        # block writes is the pair only a container can answer -- `observed`
        # and `mismatch` -- overwriting the `None`s set there. Both are
        # written whether or not anything was declared: "this task declares no
        # environment" and "the gate did not look" render identically as a
        # missing key, and a cached verdict outlives the code that wrote it.
        observed = _observed_env(container, tuple(sorted(declared)))
        mismatch = sorted(
            key for key, value in declared.items() if observed.get(key) != value
        )
        evidence["image_env_observed"] = observed
        evidence["image_env_mismatch"] = mismatch
        if mismatch:
            problems.append(
                "the container's environment does not match the manifest's "
                "image.env for "
                + ", ".join(
                    f"{key} (declared {declared[key]!r}, container "
                    f"{observed.get(key)!r})" for key in mismatch
                )
                + ". That key is baked as a Dockerfile ENV so it reaches "
                "every process in the container, including the ones the agent "
                "invents; a value that did not take is silent -- the suite "
                "goes back to being nondeterministic and the gate passes on a "
                "lucky draw. Rebuild the task image (a task edited without a "
                "task_version bump moves manifest_digest and need not move "
                "the image id)."
            )

        # The other direction, and the only check that catches the manifest
        # nobody wrote. Everything above compares DECLARED against OBSERVED,
        # so a property-based task whose author never declared CI passes all
        # of it -- declared is {}, observed is {}, mismatch is empty, GO --
        # and the ladder then runs on a lucky draw. Measured 2026-09-01
        # against hypothesis 6.167.1, that draw is `0 0 0 0 1 1 1 1 0 0` over
        # ten fresh runs of unchanged code on an unchanged tree.
        #
        # TWO probes, and the second is what keeps the refusal honest.
        # INSTALLED is not USED: hypothesis is a common transitive dependency
        # (a dev-extra, a `pip install -e .[test]` in image.build, a
        # dependency of a dependency), and a NO-GO on availability alone would
        # refuse a task whose suite never imports it -- with a remedy the
        # author cannot apply, since nothing they wrote put it there.
        #
        # The interpreter comes off `tests.runner` rather than being
        # hardcoded: a runner of ["/opt/venv/bin/python", "-m", "pytest"]
        # resolves imports against that venv, so probing whichever `python` is
        # first on PATH would answer a question about a different environment.
        importable = container.exec(
            [_runner_python(tests.runner), "-c", "import hypothesis"]
        ).exit_code == 0
        evidence["hypothesis_importable"] = importable

        # `rg` is asserted present above, so this adds no dependency. Filtered
        # through `_present` for the reason `_existing_prefixes` gives: a
        # declared prefix absent at the start state is an input this gate
        # tolerates, and handing rg a path that does not exist makes it exit 2
        # -- which must read as "could not answer", not as "no match".
        scanned = _present(container, tests.paths)
        used: bool | None = None
        if scanned:
            probe = container.exec(
                ["rg", "-q", r"^\s*(from|import)\s+hypothesis\b", *scanned]
            )
            # Three-valued on purpose. 0 is a match, 1 is no match, and
            # anything else (an unreadable path, a bad pattern, no rg) is
            # UNKNOWN -- a quiet False there would silently disarm the only
            # check that catches an undeclared property-based suite.
            used = (True if probe.exit_code == 0
                    else False if probe.exit_code == 1 else None)
        evidence["hypothesis_imported_by_suite"] = used

        if used and "CI" not in declared:
            problems.append(
                "the declared test paths import hypothesis and the manifest "
                "declares no image.env CI. A property-based suite without it "
                "is a coin flip -- measured, ten fresh runs of one property "
                "test over unchanged code gave `0 0 0 0 1 1 1 1 0 0`, and six "
                "under CI=1 gave `1 1 1 1 1 1` -- so the gate would be "
                "certifying a task whose red-before/green-after verdict is a "
                "draw. Add `image.env: {CI: \"1\", "
                "HYPOTHESIS_STORAGE_DIRECTORY: \"/tmp/bakeoff-hypothesis\"}` "
                "and read HARVESTING.md's Layer 2 bullet before doing so -- "
                "determinism makes this oracle reproducible, not correct. "
                "(This fires on an IMPORT in tests.paths, not on the package "
                "being installed: hypothesis arriving transitively through "
                "image.build in a suite that never uses it is fine and is "
                "recorded as hypothesis_importable without a problem.)"
            )
```

And, **before** the early return at the top of `preflight` — the one taken when `tests.runner` names no pytest, which never reaches a container — write the declared half and name the other three as unmeasured:

```python
    # Written BEFORE the guard below, because `image.env` is a property of the
    # manifest and is knowable with no daemon. The other three keys stay None
    # on this path: `observed: {}` and `mismatch: []` would be a CLAIM that
    # the gate looked and agreed, from a gate that never started a container.
    # Two absences that render identically are the same defect one layer down.
    evidence["image_env_declared"] = declared = _declared_env(task)
    evidence["image_env_observed"] = None
    evidence["image_env_mismatch"] = None
    evidence["hypothesis_importable"] = None
    evidence["hypothesis_imported_by_suite"] = None

    if not any("pytest" in part for part in tests.runner):
        ...
```

(`declared` is then already bound when the container block runs, which is why Step 4's block above no longer computes it.)

And bump the constant, extending its comment:

```python
#: 5 adds two environment assertions: the `image.env` read-back, and the
#: refusal of a task whose declared test paths IMPORT hypothesis while the
#: manifest declares no CI (availability alone is not the trigger -- the
#: package is a common transitive dependency). A verdict cached under 4 was written by a gate that looked
#: at neither, so a task whose determinism lever silently failed to apply --
#: or was never declared -- would keep serving a PASS.
PREFLIGHT_VERSION: str = "5"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q`
Expected: PASS.

- [ ] **Step 6: Run the whole suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: the recorded baseline plus every test added in Tasks 1–3. `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` must pass **untouched** — if it does not, an argv changed and decision 3 was violated.

- [ ] **Step 7: Add the mutation anchor**

Append to `MUTATIONS` in `bakeoff/scripts/mutation_check.py`:

```python
    (
        # An image.env that did not reach the image is silent: the suite goes
        # back to being nondeterministic (measured, 0 0 0 0 1 1 1 1 0 0 over
        # ten fresh runs of unchanged code), the gate passes on a lucky draw,
        # and every arm is scored against an oracle that answers differently
        # per run. Reverting the refusal restores exactly that -- the evidence
        # is still recorded, so the verdict flips from NO-GO to PASS with no
        # other visible change.
        "preflight: record the image.env mismatch and stop refusing it",
        "src/bakeoff/preflight.py",
        "        if mismatch:\n            problems.append(",
        "        if False:\n            problems.append(",
        "tests/test_preflight.py -k a_declared_env_that_did_not_reach",
        "not integration",
    ),
```

- [ ] **Step 8: Run the mutation check, solo**

Run: `cd bakeoff && .venv/bin/python scripts/mutation_check.py`
Expected: every anchor including the new one reports the test going red under the mutation and green after restoration. Nothing else may be running against this tree while it does.

- [ ] **Step 9: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/tests/test_preflight.py \
        bakeoff/scripts/mutation_check.py
git commit -m "feat: the gate reads the declared environment out of the container it will run in

image.env is configuration. The container's environment is the
observation, and preflight is the only place the two meet. An
un-applied image.env is invisible in the worst way: its whole job is
determinism -- measured 2026-09-01 against hypothesis 6.167.1, a
property-based suite gives 0 0 0 0 1 1 1 1 0 0 over ten fresh runs of
unchanged code and 1 1 1 1 1 1 under CI=1 -- so a value that did not
take means the gate passed on a lucky draw and every arm is scored
against an oracle that answers differently per run.

Not redundant with generating the Dockerfile ourselves. A stale tag (a
task edited without a task_version bump moves manifest_digest and need
not move the image id), a base image whose own ENV later collides, and
a future edit emitting the lines in the wrong place all leave the
rendering tests green.

printenv, never `echo $KEY`: measured, `printenv KEY` exits 0 with the
value even when that value is empty and exits 1 when the key is unset,
so the evidence can record "" and null as the different facts they are.
Read before the runner is built, so a bad environment is reported as
itself rather than as five downstream suite failures.

A second check runs in the other direction, and it is the one that
catches the manifest nobody wrote: declared-vs-observed passes a
property-based task whose author never declared CI, because {} == {}
and the mismatch list is empty. Two probes, not one -- INSTALLED is not
USED. hypothesis arrives transitively all the time, so availability
alone would refuse suites that never touch it, naming a remedy the
author cannot apply. The NO-GO is `rg` finding a hypothesis import
under the declared tests.paths while image.env names no CI;
availability is recorded beside it. The interpreter comes off
tests.runner, because a venv runner resolves imports somewhere other
than the first `python` on PATH.

On the early return -- tests.runner naming no pytest, which never
reaches a container -- only image_env_declared is written and the other
three keys are None. `observed: {}` and `mismatch: []` there would be a
CLAIM that the gate looked and agreed, from a gate that never started a
container; two absences that render identically are the same defect one
layer down.

PREFLIGHT_VERSION 4 -> 5: it is in the cache key and it is the only
component that moves when this file does, so without the bump every
warm cache serves a verdict written by a gate that never looked.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The two seams — the agent's env cannot shadow it, and a real image really carries it

**Files:**
- Test: `bakeoff/tests/test_claude_runner.py` (the offline half)
- Test: `bakeoff/tests/test_integration_grader.py` (the real-image half)

**Interfaces:**
- Consumes: `pinned_env_keys()` (Task 1), `render_dockerfile(env=…)` (Task 2), `TaskImage.env` (Task 1).
- Produces: nothing. This task is entirely tests, and it exists because the guarantee "the agent sees the same environment the gate does" spans a boundary neither of the earlier tasks crosses.

- [ ] **Step 1: Write the offline test**

Add to `bakeoff/tests/test_claude_runner.py`:

```python
def test_the_agents_env_names_no_key_a_task_image_may_set():
    """The offline half of "the agent sees the same suite the gate does".

    Docker merges an exec's environment into the image's with the EXEC's keys
    winning (measured 2026-09-01, Docker 29.5.2), so the only way an image.env
    value fails to reach the agent's `claude` process is `container_env`
    naming the same key. This is that claim, over the whole allowed set rather
    than over one example, and it is the assertion that turns a careless
    future allowlist entry into a red suite instead of a task whose
    determinism lever applies to the gate and not to the agent.
    """
    from bakeoff.tasks import _IMAGE_ENV_ALLOWED

    env = container_env(make_config(custom_headers="X-Bakeoff-Run-Id: r-1"))

    assert not (_IMAGE_ENV_ALLOWED & set(env))
```

**The two import idioms are deliberate, not an inconsistency.** `test_tasks.py` reaches the constant as `tasks._IMAGE_ENV_ALLOWED` because one of its tests `monkeypatch.setattr`s it, and a `from ... import` there would bind a copy the patch never reaches. This file only reads it, so the direct import is fine and says so. If you change either, change it for the reason, not for symmetry.

- [ ] **Step 2: Run it**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_claude_runner.py -q -k names_no_key`
Expected: PASS immediately — it is a regression guard, not a driver of new code. Confirm it is not vacuous by temporarily adding `"CI"` to `_IMAGE_ENV_ALLOWED` and re-running: it must go red, alongside `test_the_allowlist_and_the_harness_pinned_keys_are_disjoint` staying green (they are different claims — one is about `_eval_env`, this one is about `container_env`'s output). Revert the temporary edit.

- [ ] **Step 3: Write the integration test**

Add to `bakeoff/tests/test_integration_grader.py`. The module already carries `pytestmark = [pytest.mark.integration, pytest.mark.task_image]`, which is right: this builds a task image.

```python
def test_a_task_images_env_reaches_both_the_gates_exec_and_the_agents(
    click_task, click_image, grader_cache, tmp_path
):
    """The real-image half, and the one that cannot be faked.

    Everything offline proves is that we render the right Dockerfile line and
    that container_env does not name the key. What no unit test can reach is
    the merge itself -- that an image ENV survives into an exec that carries
    the agent's OWN environment. If it did not, every determinism lever would
    apply to preflight and the grader and not to the loop section 3.3
    measures, and the only symptom would be a suite that sometimes passes.

    Both execs, deliberately: `container.exec(argv)` is the shape preflight,
    the oracle and the grader all make, and `exec(argv, env=container_env(...))`
    is the shape `claude_runner.ContainerBackend` makes for the agent.
    """
    from bakeoff.claude_runner import ClaudeCodeConfig, container_env
    from bakeoff.container import RunContainer
    from bakeoff.images import build_task_image

    declared = {"CI": "1", "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/bakeoff-hyp"}
    task = dataclasses.replace(
        click_task,
        task_id=click_task.task_id + "-envprobe",
        image=dataclasses.replace(click_task.image, env=declared),
    )
    image = build_task_image(task, _base_image(), grader_cache / "build",
                             grader_cache)

    agent_env = container_env(ClaudeCodeConfig(
        model="m", base_url="http://litellm:4000", auth_token="t",
        settings_path="/eval/settings.json", config_dir="/eval/claude-config",
        max_turns=1, wall_clock_timeout_s=1,
        custom_headers="X-Bakeoff-Run-Id: r-envprobe",
    ))

    with RunContainer(image=image, repo_path=str(tmp_path),
                      base_sha="") as container:
        for key, value in declared.items():
            gate = container.exec(["printenv", key])
            agent = container.exec(["printenv", key], env=agent_env)

            assert gate.exit_code == 0, key
            assert gate.stdout.strip() == value
            # The merge, which is the whole claim: a key the exec env does not
            # name survives from the image.
            assert agent.exit_code == 0, key
            assert agent.stdout.strip() == value

        # And the eval's own keys still win where they ARE named, so this is
        # not passing because the exec env was ignored wholesale.
        model = container.exec(["printenv", "ANTHROPIC_MODEL"], env=agent_env)
        assert model.stdout.strip() == "m"
```

`_base_image()` is whatever the module already uses to get the base — `build_base_image(REPO_ROOT)` at line 147; extract it to a module-level fixture if it is inline, or call `build_base_image(REPO_ROOT)` directly here. `dataclasses` needs importing if it is not already.

- [ ] **Step 4: Run the integration leg**

Run: `cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" --basetemp="$HOME/.cache/bakeoff-pytest" tests/test_integration_grader.py`
Expected: PASS, including every pre-existing test in the module. This builds the click image twice (once under the original task id, once under `-envprobe`) and takes several minutes; that is the cost of the only test that can see the merge.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/tests/test_claude_runner.py bakeoff/tests/test_integration_grader.py
git commit -m "test: an image ENV has to survive the exec that carries the agent's own environment

Docker merges an exec's environment into the image's with the exec's
keys winning, so the one way image.env fails to reach the agent's
`claude` process is container_env naming the same key. Two tests, one
per side of a seam neither implementation task crosses.

Offline: container_env's output is disjoint from _IMAGE_ENV_ALLOWED,
asserted over the whole set rather than over one example, so a careless
future allowlist entry is a red suite instead of a task whose
determinism lever applies to the gate and not to the loop section 3.3
measures.

Integration: a real task image built with image.env, read back through
BOTH exec shapes -- `container.exec(argv)`, which preflight, the oracle
and the grader all make, and `exec(argv, env=container_env(...))`,
which ContainerBackend makes for the agent -- plus a control proving
the eval's own keys still win where they are named, so the assertion
cannot pass by the exec environment being ignored wholesale.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Docs — what a candidate must satisfy changed, so the rules move with it

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md` (Layer 1 preflight list, Layer 2 "No property-based oracle", the "Usable once a dependency is declared" and "Excluded" tables)
- Modify: `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` (a commented `image.env` example)
- Modify: `docs/BUILDING-A-TASK-SET.md` (§3.5 key list, §10 failure-mode table)
- Modify: `tasks/todo.md` (the review section)
- Modify: `TASKS.md` (one open follow-up)

- [ ] **Step 1: `HARVESTING.md` — Layer 1**

In the "Preflight — `src/bakeoff/preflight.py`" bullet list, after the `strip_paths` bullet, add:

```markdown
- **Every `image.env` key holds its declared value inside the container.** Read
  back with `printenv`, whose exit code separates "set to the empty string"
  from "not set at all" (measured; `echo $KEY` cannot). `image.env` is
  configuration and the container's environment is the observation, and this
  is the only place the two meet — a value that did not take is silent, and
  what it silently loses is determinism.
```

- [ ] **Step 2: `HARVESTING.md` — Layer 2, replacing the "No property-based oracle" bullet**

Replace:

```markdown
- **No property-based oracle.** A hypothesis-driven suite can pass a wrong fix
  on a lucky draw and fail a right one on an unlucky seed. `attrs` and `cattrs`
  are out for this reason.
```

with:

```markdown
- **A property-based suite is allowed, and only with `image.env: {CI: "1"}`.**
  Measured 2026-09-01 against hypothesis 6.167.1, a property test over a rare
  input gives `0 0 0 0 1 1 1 1 0 0` across ten fresh runs of unchanged code on
  an unchanged tree, and `1 1 1 1 1 1` under `CI=1`. Hypothesis registers a
  built-in `ci` profile at import time — `derandomize=True`, `database=None`,
  `deadline=None` — and auto-loads it when any of twelve CI variables is
  present; `"CI"` counts on presence alone, any value. There is **no
  `HYPOTHESIS_PROFILE` environment variable** (the string in `pytest --help`
  is argparse's metavar for `--hypothesis-profile`), and a profile the repo
  would have to register is unreachable anyway: the start state is `base_sha`
  plus the test half, so the harness cannot rely on a repo-side `conftest.py`.

  Declare `HYPOTHESIS_STORAGE_DIRECTORY` too, pointing outside `/repo`.
  Hypothesis's storage root is `Path.cwd() / ".hypothesis"` fixed at import
  time, so with `workdir=/repo` it lands in the tree the §5.6 submission diff
  is taken against. `CI=1` stops the `examples/` database but not the
  `constants/` cache, and an agent that runs pytest from a subdirectory gets a
  second copy there. `gitignore_extra` is **not** the fix and is not needed:
  hypothesis writes `.hypothesis/.gitignore` containing `*`, so the tree is
  already clean to `git status --porcelain` and to `git add -A` — a
  `gitignore_extra` entry would move `start_sha` and change nothing.

  **What determinism does and does not buy.** It does not make the oracle
  safe; it makes it *reproducible*. A wrong fix can still pass on a pinned
  draw, and it will then pass on every repeat — so §5.7's N=3 repeats catch
  nothing here, because all three runs draw the same examples.

  Worse, and this is the rule that decides which suites are admissible:
  **Hypothesis mines integer and string literals out of the modules the suite
  imports and feeds them into the example pool, so the examples a submission
  is judged by are a function of the source code under test.** Measured
  2026-09-01, one property (`@given(st.integers(0, 1_000_000_000))`,
  `assert n != 137`) against an imported `magic.py`, `CI=1` throughout, six
  fresh runs per row:

  | imported `magic.py` | verdict |
  |---|---|
  | `MAGIC = 137` | `1 1 1 1 1 1` — the one-in-a-billion bug is found, every time |
  | `MAGIC = 1370` | `0 0 0 0 0 0` — missed, every time |
  | `MAGIC = 137` again | `1 1 1 1 1 1` — found again; the flip is reversible |
  | `MAGIC = 999` | `0 0 0 0 0 0` |

  The same literal in a file the suite does **not** import changes nothing
  (`0 0 0`), so it is import reachability and not the tree's bytes. The agent
  is being asked to edit exactly those modules, so the oracle's strictness is
  coupled to the shape of the fix: a submission that happens to write the
  right constant is judged by a strictly stronger example set than one that
  does not, and the grade records both as "the suite passed". Preflight's
  red-before/green-after verdict is taken on the **reference** fix's tree and
  does not transfer.

  So: prefer a suite whose examples are **exhaustive and explicit** —
  `@example` decorators, or `st.sampled_from` over a small closed set — where
  the mined pool cannot change what is checked. A suite whose only oracle is a
  draw over a large space is admissible only if you have read the property and
  are satisfied that *any* correct fix passes it and *no* wrong one does,
  which is the same judgment call as the "tests assert behaviour, not internal
  names" bullet above and is harder here, not easier.
```

- [ ] **Step 3: `HARVESTING.md` — the two repository tables**

Remove the `python-attrs/attrs, python-attrs/cattrs` row from **Excluded** and add to **Usable once a dependency is declared**:

```markdown
| python-attrs/attrs, python-attrs/cattrs | `image.env: {CI: "1", HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"}` for the property-based suite | **not measured** — the editable install collides with a site-packages `attr`, and that is now the only known blocker | — |
```

and, immediately under that table, add a paragraph:

```markdown
**attrs/cattrs are half-reopened, not reopened.** They were excluded for two
reasons and this broadening lifts one. The other — `pip install -e .`
resolving `attr` against site-packages instead of the run tree — is the
non-editable-install failure mode in a new dress: imports resolve past `/repo`
so nothing the agent writes takes effect, every arm fails identically, and
preflight's green-after check is what catches it. Nobody has run that gate on
these repositories. Do not cut a task from either without doing so first.
```

- [ ] **Step 4: `task.yaml` — the commented example**

In `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, inside the `image:` block, after the `build:` key, add:

```yaml
  # env: NOT USED by this task -- click's suite is example-based, so nothing
  # here needs pinning. Shown because this manifest is the documentation for
  # the key.
  #
  # Baked into the task image as Dockerfile ENV lines, AFTER every build step
  # (so a value declared for the test runner cannot change how the image was
  # assembled) and therefore in the finished image's config. Docker merges an
  # image's environment into every exec with the exec's own keys winning, so
  # these reach preflight's runner, the oracle's, the grader's, the agent's
  # `claude` and every command the agent invents -- which is the point.
  # `tests.runner` could not do that: the agent is never told the runner argv,
  # it gets `prompt` and nothing else.
  #
  #   env:
  #     CI: "1"
  #     HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"
  #
  # An ALLOWLIST of keys, not a free map: CI and HYPOTHESIS_STORAGE_DIRECTORY
  # and nothing else. An unrestricted map would reopen the hole the harness's
  # env allowlist exists to close -- CLAUDE_CODE_USE_BEDROCK here would make
  # the CLI ignore ANTHROPIC_BASE_URL, bypass the proxy, and leave the
  # mandatory wire log empty with the run still looking normal.
  #
  # CI=1 is what makes a hypothesis suite deterministic: hypothesis registers
  # a built-in `ci` profile (derandomize=True, database=None, deadline=None)
  # and auto-loads it on the presence of that variable. Measured 2026-09-01
  # against hypothesis 6.167.1: ten fresh runs of one property test gave
  # `0 0 0 0 1 1 1 1 0 0`, and six under CI=1 gave `1 1 1 1 1 1`. There is no
  # HYPOTHESIS_PROFILE environment variable to use instead.
  #
  # HYPOTHESIS_STORAGE_DIRECTORY must be absolute and outside /repo -- refused
  # at load otherwise. Hypothesis's storage root is fixed to the cwd at import
  # time, so without it the `constants/` cache lands in the tree the section
  # 5.6 submission diff is taken against.
  #
  # Values may not carry a newline, a quote, a backslash or a `$`: the last
  # one is EXPANDED by the builder, which would make the value a property of
  # the build rather than of the manifest.
  #
  # Declaring env does NOT move start_sha -- it is under `image:`, which is
  # not an input to the start state. It does move the image id, and therefore
  # `Versions.container_image_digest` on every record, which is where a reader
  # finds it: the record deliberately carries no copy of this map, because a
  # manifest field published under a record's provenance would be
  # configuration reported as observation.
  #
  # Section 6.4 confound, to be recorded here by any task that uses it: `CI`
  # is a general-purpose signal and the repository's own code may branch on
  # it, so the arms are scored on that repo's CI-mode suite.
```

- [ ] **Step 5: `BUILDING-A-TASK-SET.md`**

In §3.5, after the `strip_paths` paragraph, add:

```markdown
`image.env` is a **map under `image:`**, and its keys are an allowlist: `CI`
and `HYPOTHESIS_STORAGE_DIRECTORY`, nothing else. It is baked into the task
image as `ENV` lines, so it reaches every process in the container — the
gate's runner, the grader's, the agent's `claude`, and the commands the agent
invents. Use it when the suite is property-based:

```yaml
image:
  pip: ["hypothesis==6.167.1"]        # or whatever the repo's lockfile pins
  build: ["pip install -e ."]
  env:
    CI: "1"
    HYPOTHESIS_STORAGE_DIRECTORY: "/tmp/bakeoff-hypothesis"
```

`CI=1` loads Hypothesis's built-in `ci` profile — `derandomize=True`,
`database=None`, `deadline=None` — which is the difference between a suite
that answers `0 0 0 0 1 1 1 1 0 0` over ten runs and one that answers
`1 1 1 1 1 1`. It does **not** move `start_sha` (it is not an input to the
start state) but it does move the image id, so preflight re-runs. Read
`HARVESTING.md`'s Layer 2 bullet before cutting a property-based task at all:
determinism makes the oracle reproducible, not correct, and the draw is a
function of the tree the agent is editing.
```

In §10's failure-mode table, add two rows:

```markdown
| a property-based task's preflight passes, then a later run of the same task NO-GOes with no manifest change | `image.env` never applied. Preflight reads it back with `printenv` since `PREFLIGHT_VERSION` 5, so the refusal names the key — rebuild the task image. A task edited without a `task_version` bump moves `manifest_digest` and need not move the image id |
| a hypothesis task grades `resolved` for one arm and not another on submissions that look equivalent | expected, and not a bug in the harness. Hypothesis mines literals out of the modules the suite **imports** and feeds them into the example pool, so two correct-looking fixes are judged by different examples — measured, changing one integer literal in an imported module flipped six consecutive verdicts in each direction. The same literal in a non-imported file changes nothing. Prefer suites with exhaustive `@example`s (§4) |
```

- [ ] **Step 6: `tasks/todo.md` — the review section**

Append a `## Broadening 3 — hypothesis suites — 2026-09-01` section in the file's existing style: what shipped (the four commits), the measurements that decided it (§1a's ten-run row, §1g's constant-mining table, the Docker merge probe, `HYPOTHESIS_PROFILE` not existing, the `ci` profile's three settings, the `.hypothesis/.gitignore` self-ignore, `--hypothesis-seed` being exit 4 without the plugin, and the `claude` 2.1.220 grep plus the `CI=1` smoke probe), the four things the plan got wrong before code was written (the brief assumed a `HYPOTHESIS_PROFILE` env var; it assumed `gitignore_extra` was needed and the tree is in fact already clean; it assumed a seed and a database were separate levers when `derandomize=True` implies `database=None`; and **the first draft blamed tree-dependence on "the agent edits the tree", which did not reproduce — the mechanism is constant mining over IMPORTED modules, which is narrower, worse, and coupled to the fix itself**), and what was deliberately left out (decision 12).

- [ ] **Step 7: `TASKS.md` — one open item**

Add, at P2:

```markdown
- **Confirm on a LIVE cell that `CI=1` in a task image does not change Claude
  Code's behaviour.** Broadening 3 bakes that variable into a property-based
  task's image and it reaches the agent's own process, which is the point.
  Measured offline: `claude` 2.1.220 reads `CI` in exactly two places — the
  bundled `supports-color` colour-depth block (`"CI" in env` ×1, `CI_ENVS` ×3)
  and an environment-name function (`process.env.CIRCLECI` …); the eight
  `isCI` hits are seven zod `isCIDR` plus one `isCI:Yt(!1)` field. Both should
  be inert without a TTY, and `scripts/smoke_test.py --mode offline` against an
  image carrying `ENV CI=1` passes (broadening 3's Final verification). What
  that leaves open is a live cell: turn counts, tool-call counts and
  `terminal_finish_reason` against the same cell without the variable. Do it
  before the first property-based task is collected, not after — a behaviour
  difference on one task's image is a §6.4 confound only that task's arms
  carry, and `RunRecord.config_digest` will not move, because it covers
  `_eval_env` and `PASSTHROUGH_ENV` and not the image's ENV. That is correct
  (the image is pinned by `container_image_digest`) and it does mean the
  difference is invisible in a digest comparison.

- **Two evidence families disagree about what an unreachable check writes.**
  Broadening 3's `image_env_*` / `hypothesis_*` keys are written on *every*
  path — `declared` with a value, the rest as explicit `null` — on the
  pre-container early return at `preflight.py:405-417`. `stripped_paths` and
  `stripped_paths_present`, added by broadening 1, are simply **absent** there.
  Both encode "the gate did not look"; only one of them says so, and a reader
  of a cached `preflight.json` cannot tell an absent `stripped_paths` from a
  verdict written by a gate too old to have the key. Make them consistent by
  moving the strip's two keys before the guard as `null`s — the new family is
  the shape to copy, not the other way round. Cheap, and it needs a
  `PREFLIGHT_VERSION` bump because it changes what a cached verdict contains.
```

- [ ] **Step 8: Commit**

```bash
git add bakeoff/taskset/HARVESTING.md \
        bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml \
        docs/BUILDING-A-TASK-SET.md tasks/todo.md TASKS.md
git commit -m "docs: a property-based suite is allowed with a pinned profile, and reproducible is not the same as correct

HARVESTING's Layer 2 said hypothesis suites are out because a wrong fix
can pass on a lucky draw. Measured, that draw is real -- one property
test gives 0 0 0 0 1 1 1 1 0 0 over ten fresh runs of unchanged code --
and CI=1 removes it, because hypothesis registers a built-in `ci`
profile (derandomize=True, database=None, deadline=None) and auto-loads
it on that variable's presence.

The rewritten bullet says what determinism actually buys, which is less
than it sounds: a wrong fix can still pass on a pinned draw and will
then pass on every repeat, so section 5.7's N=3 repeats catch nothing
there. And the example pool is a function of the SOURCE UNDER TEST --
hypothesis mines literals out of the modules the suite imports, so an
imported MAGIC = 137 finds a one-in-a-billion bug 6/6 while MAGIC =
1370 misses it 6/6, reversibly, and the same literal in a file the
suite does not import changes nothing. The agent edits exactly those
modules, so the oracle's strictness is coupled to the shape of the fix
and preflight's verdict, taken on the reference tree, does not
transfer. Prefer exhaustive, explicit examples.

attrs and cattrs move out of Excluded but only halfway: they were out
for two reasons and this lifts one. The editable install colliding with
a site-packages `attr` is unmeasured and is the non-editable-install
failure mode in a new dress, so the table says `not measured` rather
than implying the repos are usable.

gitignore_extra is documented as NOT the fix. Hypothesis writes
.hypothesis/.gitignore containing `*`, so the tree is already clean to
git status and git add -A; an entry would move start_sha and change
nothing, while leaving the real hazard -- a database that couples
preflight's five runs and the oracle's two -- untouched.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Final verification (run before declaring the broadening done)

- [ ] `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — no regressions against the recorded baseline (`1185 passed, 49 deselected` at `e3ceb13`), plus the new tests.
- [ ] `cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"` — the full integration leg, because this change alters what the container's environment holds.
- [ ] `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` passes **and its source is unmodified**. Run `git diff main -- bakeoff/tests/test_preflight.py | grep -E "^[+-].*(argv_preflight_validated|pass_to_pass|timeout\", \"60\")"` — it must print nothing. (A context line, prefixed with a space, is fine; an added or removed one is not.) This broadening changes no argv; if it did, decision 3 was violated and the mechanism is wrong, not the test.
- [ ] `git diff main -- bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` shows **only added comment lines**. `start_sha` must still recompute to `33575cc0b75608fa5cbcb1d3ae3347b81eac437f`.
- [ ] `cd bakeoff && .venv/bin/python scripts/verify_logger.py` — the §6.6 gate. Unaffected, offline, and cheap.
- [ ] `cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --tasks click-3360-write-usage-empty-args` — the click task must still be GO, with `image_env_declared: {}`, `image_env_observed: {}`, `image_env_mismatch: []` in `~/.cache/bakeoff/preflight/click-3360-write-usage-empty-args.json`, and the file's `preflight_version` reading `"5"`.
- [ ] **The `CI=1` smoke gate**, which is what settles finding 6 rather than a grep. Build a throwaway image `FROM bakeoff-eval-agent:base` adding `ENV CI=1`, and run the offline smoke against it:

  **`smoke_test.py` has no image override** — checked: `main` calls `build_images()`, which builds `docker/eval-agent.Dockerfile` unconditionally and returns its id, and no flag or environment variable redirects it. So the probe is a temporary edit to that Dockerfile, run, and revert:

  ```bash
  cd bakeoff
  # The file must be CLEAN before you start: the revert below is
  # `git checkout --`, which discards any pre-existing edit along with ours.
  git diff --exit-code -- docker/eval-agent.Dockerfile || \
    { echo "eval-agent.Dockerfile is already modified -- stash first"; exit 1; }

  .venv/bin/python scripts/smoke_test.py --mode offline    # the control, first
  printf '\nENV CI=1\n' >> docker/eval-agent.Dockerfile
  .venv/bin/python scripts/smoke_test.py --mode offline    # the probe
  git checkout -- docker/eval-agent.Dockerfile             # MANDATORY
  git diff --exit-code -- docker/eval-agent.Dockerfile     # must exit 0
  ```

  `--exit-code` on both ends rather than `--stat`: a `--stat` that prints nothing and a `--stat` you did not read look the same in a terminal, and this is a step whose failure mode is leaving `ENV CI=1` in the base image every task then inherits. **That contamination would not be caught by the read-back** — `image_env_mismatch` compares only the keys a manifest *declared*, so a stray `CI` no manifest asked for is invisible to it. The final `git diff --exit-code` is the whole guard.

  The `ENV` goes at the **end** of the file, after the version-pin `RUN` — an `ENV` before the `curl | bash` installer is the shape the file's own comment warns about (declaring the update variables too early makes the install leave no binary behind while still exiting 0).

  The contaminated *image* is harmless and needs no cleanup: `smoke_test.py` tags `bakeoff-eval-agent:smoke`, nothing else consumes that tag, and `build_images()` rebuilds it unconditionally on the next run. It is the Dockerfile, not the image, that has to be put back.

  **What to compare, and it is not just PASS.** Colour codes are the first thing a `CI` branch would change, and they would leak into stdout as unparseable stream-json rather than as a failure — so read `stdout_malformed_lines` and `turns_streamed` off both records, not only the exit status and the turn count. Expected: the probe matches the control on all four. Offline, no credentials, no spend. Measured offline, `claude` 2.1.220 reads `CI` only in bundled `supports-color`'s colour-depth block and an environment-name function, both of which should be inert with no TTY — this is the step that proves it on the real CLI over the real transport instead of by reading a 272 MB bundle. If any of the four moves, **stop**: `CI` is then not a safe key and decision 2 has to be reopened, not worked around.
- [ ] Run `bakeoff/scripts/mutation_check.py` **solo**. One new anchor; it must go red under the mutation.
- [ ] Operator note: `PREFLIGHT_VERSION` 5 invalidates every cached preflight verdict on purpose. The next `run_matrix.py` or `grade.py` re-validates the whole task set — offline, no credentials, no spend, but it costs one full gate pass per task.

---

## Sentences for `CLAUDE.md`, to be applied on `main` after the merge

Not written here — `CLAUDE.md` has an uncommitted edit on `main` and the merge would conflict (CONTEXT.md's global constraint). Add these to the invariants list, after the existing preflight bullets, and the last one to the config-gotchas list:

1. **A knob that has to hold for the agent's own commands lives in the image's `ENV`, never in `tests.runner`.** The agent is never told the runner argv — `execute_run` hands it `task.prompt` and nothing else, and `tests.runner` has exactly four consumers, all inside the harness (preflight, the oracle, and the grader twice). So a flag added there makes the gate prove one suite discriminates while §3.3's self-correction loop runs a different one: the agent watches a test go green that the grader will call red, and corrects away from a fix that was already right, scored as capability. That is the stale-`.pyc` defect with a different cause, which is why `PYTHONDONTWRITEBYTECODE=1` is an image `ENV` and not a `-B` on one runner, and why `image.env` (schema-free, `PREFLIGHT_VERSION` 5) takes the same shape. **Docker merges an exec's environment into the image's with the exec's keys winning** — measured 2026-09-01, Docker 29.5.2, through the same client `RunContainer` uses — so an image `ENV` reaches `container.exec(argv)` (preflight, oracle, grader), reaches `exec_stream(..., env=container_env(cfg))` (the agent), and reaches every command the agent invents, with no consumer changed. The corollary is the hazard: a key `container_env` *does* name would be overridden on the agent's process alone, applying to the gate and the grader and not to the thing being measured, which is why `image.env` is a **key allowlist** (`CI`, `HYPOTHESIS_STORAGE_DIRECTORY`) proven disjoint from a `pinned_env_keys()` derived from `_eval_env` and `PASSTHROUGH_ENV`. Unrestricted it would reopen the hole the env allowlist exists to close from the other side: `CLAUDE_CODE_USE_BEDROCK` is kept out of the *host* environment by `PASSTHROUGH_ENV` and is not set by `container_env`, so an image `ENV` is precisely the route left open — and it makes the CLI ignore `ANTHROPIC_BASE_URL`, bypass the proxy, and leave the mandatory wire log empty with the run still looking normal.
2. **A property-based suite is deterministic only via the environment, and `HYPOTHESIS_PROFILE` does not exist.** Measured 2026-09-01, hypothesis 6.167.1: one property test over a rare input exits `0 0 0 0 1 1 1 1 0 0` across ten fresh runs of unchanged code, and `1 1 1 1 1 1` under `CI=1`. Hypothesis registers a built-in `ci` profile at import (`derandomize=True`, `database=None`, `deadline=None`) and auto-loads it when any of twelve CI variables is present; `"CI": None` in its `_CI_VARS` means presence alone, any value including `""`. There is **no `HYPOTHESIS_PROFILE` environment variable** — the string in `pytest --help` is argparse's metavar for `--hypothesis-profile`, and that flag with an unregistered name is an `INTERNALERROR` at `pytest_configure`, exit 3, zero tests run. A repo-side `conftest.py` registering a profile is unreachable anyway: the start state is `base_sha` plus the test half. `--hypothesis-seed=N` works and is refused for two reasons — it is argv, so the agent never gets it, and it is **exit 4 in any image without the plugin**, on the CLI and through `PYTEST_ADDOPTS` alike, where `CI=1` is inert (exit 0, nothing written). Two adjacent knobs are traps: `HYPOTHESIS_DATABASE_FILE` now raises, surfacing as `assert None is not None` on every property test, and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` silently degrades `@given` to a single call — a false green.
3. **`.hypothesis/` does not dirty the tree, and that is why `gitignore_extra` is the wrong fix.** Hypothesis writes `.hypothesis/.gitignore` containing `*`, so `git status --porcelain` is clean and `git add -A` does not sweep it — the guard is `existed_before` on the home directory, so it fails only where the directory pre-exists without that file. A `gitignore_extra` entry would therefore move `start_sha` for no observable change while leaving the real hazard untouched: the example **database** couples runs that must be independent. Preflight runs the suite five times in one tree, so a database makes green-after replay red-before's falsifying example and become strictly stricter than the grader's check 5, which runs on a fresh tree — a gated/graded divergence in neither argv nor record. `oracle.derive_quarantine` runs `pass_to_pass` twice in one tree to tell a flake from a real failure, and the database is a measured one-way ratchet: it pins a *failing* verdict permanently and cannot stabilise a passing one, so it systematically manufactures the "red in BOTH reference runs" refusal. `CI=1` removes the database and the randomness; `HYPOTHESIS_STORAGE_DIRECTORY` outside `/repo` removes the directory — including the `constants/` cache, which `database=None` does not stop and which is written or not depending on what the suite imports (measured: written under `CI=1` for a suite importing a local module, absent for one that imports none), and including the second copy an agent running pytest from a subdirectory would create.
4. **Determinism makes a property-based oracle reproducible, not correct, and the example pool is a function of the source under test.** Hypothesis mines integer and string literals out of the modules the suite **imports**. Measured 2026-09-01 under an unchanged `ci` profile, one property (`@given(st.integers(0, 1_000_000_000))`, `assert n != 137`), six fresh runs per row: an imported `MAGIC = 137` finds the one-in-a-billion bug `1 1 1 1 1 1`, `MAGIC = 1370` misses it `0 0 0 0 0 0`, `MAGIC = 137` finds it again, `MAGIC = 999` misses it — reversible, in both directions. The same literal in a file the suite does **not** import changes nothing (`0 0 0`), so it is import reachability rather than the tree's bytes. The agent is asked to edit exactly those modules, so the oracle's strictness is coupled to the shape of the fix: a submission that writes the right constant is judged by a stronger example set than one that does not, and the grade records both as "the suite passed". Preflight's red-before/green-after verdict is taken on the *reference* fix's tree and does not transfer, and §5.7's N=3 repeats see none of it — all three draw the same examples. Prefer suites whose examples are exhaustive and explicit (`@example`, `st.sampled_from` over a closed set). Since `PREFLIGHT_VERSION` 5 the gate refuses a task image in which `import hypothesis` succeeds while the manifest declares no `CI`, because declared-vs-observed alone passes that task: `{} == {}`, mismatch empty, GO, and the ladder then runs on the coin flip.
5. *(config gotchas)* **`PYTEST_ADDOPTS` is prepended to the command line, so the CLI wins on store options and count options accumulate.** Measured, pytest 9.1.1: with `-k beta` on the command line, `PYTEST_ADDOPTS="-k alpha"` loses; but `PYTEST_ADDOPTS="-v"` against the `-q` every `tests.runner` in the task set carries yields verbosity **0**, not `-q`. The grader parses pytest's `-q` summary line, so a variable set for one purpose can change the output shape another stage reads. That, plus its unconditional reach (every pytest process from that environment, any cwd, either entry point, including commands the agent invents), is why `image.env`'s allowlist does not include it.
