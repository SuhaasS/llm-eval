# Round 2, item 14 — `image.build` writes into `/repo` are discarded by the bind mount

**The decision: (A). The scaffold/tree split stays exactly as it is, the gate learns to *see*
it, and the one shape that is measurably inadmissible gets refused.** `image.build` keeps
running against the build-time scaffold, `materialize` keeps producing a run tree that is
`start_sha` and nothing else, `preflight` records the paths the image's `/repo` has and the run
tree does not, and the bare-runner probe stops accepting the exit code that this class produces.

**Why this is not a docs-only commit.** The class was believed to be loud (a
`ModuleNotFoundError` at gate time). Measured 2026-09-02, it is not. On `sqlglot-6927` the gate
passes and the run silently loses `sqlglot.__version__`. On `pytest-10210` the command §3.3's
loop is built on dies at **exit 1**, and `1` sits in the bare-runner probe's accepted set — where
the branch's own comment says it was put defensively because *"1 cannot happen with `--co`"*.
This item is the measurement of the one way it can. §1.3, §1.4 and §1.7.

**Version constants that move:** `PREFLIGHT_VERSION` **+1** from whatever is on disk when this
item starts (D6 — now for a changed assertion, not only a changed evidence shape). Nothing else
— not `SCHEMA_VERSION`, `GRADER_VERSION`, `ORACLE_VERSION`, `GRADE_SCHEMA_VERSION`, and no image
or Dockerfile text (so no `container_image_digest` moves).

**Global constraints.** Items 1–13 land first and each may move a constant or a line number.
Every `file.py:NNN` below is marked *re-locate by name*. `PREFLIGHT_VERSION` is
**read-off-disk-and-add-one**, never a transcribed literal (it reads `"14"` at HEAD 8232032, so
expect `"14" -> "15"` if nothing else lands first). Item 5 introduces `preflight.EVIDENCE_KEYS`
and a `PreflightResult.__post_init__` that raises on a key set that is not the schema; all three
new keys go into that tuple in write order (D5). If item 5 has not landed, skip that one step —
nothing else in this plan depends on it.

---

## 1. What was measured

All of it on 2026-09-02, on this machine, against the images and build contexts the 2026-09-02
corpus probe left behind (`~/.cache/bakeoff/build/image-<task_id>/repo` and
`bakeoff-task-<task_id>:v1`). Docker 29.5.2. Every figure below was produced twice — once by
this plan and once by review 1 — and the two agree.

### 1.1 The mechanism, restated from the code

`images.build_task_image` builds its context from `git archive base_sha` (plus one archive per
submodule, minus `strip_paths`), writes `COPY repo /repo`, and then runs each `image.build` entry
as `RUN cd /repo && <command>`. `tasks.materialize` builds the run tree *separately*, from a
`--local` clone of the pruned mirror, and has never seen anything the build wrote.
`container.RunContainer.__enter__` bind-mounts that tree over `/repo` in full
(`volumes = {self.repo_path: {"bind": REPO_MOUNT, "mode": "rw"}}`). So the image's `/repo` is
visible only during the build; every process in every container — the agent's `claude`, the
commands the agent invents, preflight's five suite runs, the oracle's two and the grader's ladder
— reads the run tree.

`images.py`'s module docstring already says this ("`/repo` inside a built task image is a
SCAFFOLD ... At run time the bind mount replaces it"). What was missing is any measurement of
**what a build actually writes there**, and what it costs when the writes matter.

### 1.2 The population: 2 of 9 gated probe tasks generate files, and click is not one of them

Method: list the host build context (`find . \( -type f -o -type l \)`) and list the image's
`/repo` (`docker create --entrypoint true <image>` → `docker cp <cid>:/repo/. -` → `tarfile`),
and diff.

| task | context files | image files | added by the build | wall |
|---|---|---|---|---|
| `click-3360-write-usage-empty-args` | 149 | 149 | **0** | 0.26 s |
| `pytest-10210-approx-nested-container` | 684 | 691 | **7** | 0.24 s |
| `sqlglot-6927-dremio-trycast` | 266 | 272 | **6** | 0.32 s |
| `yaml-474-single-newline-empty-value` | 1842 | 1842 | 0 | 1.75 s |
| `tomlkit-514-inline-table-comment-separator` | 1122 | 1122 | 0 | 0.41 s |
| `werkzeug-3037-duplicate-rule-error` | 290 | 290 | 0 | 0.42 s |
| `bidict-389-putall-rollback-clean` | 85 | 85 | 0 | 0.25 s |
| `chimera-228-equinox-numeric` | 189 | 189 | 0 | 0.24 s |
| `ufo-214-without-trailing-slash-query` | 40 | 40 | 0 | 0.21 s |

Nothing was **removed** by any build (`comm -23` empty on all nine). Review 1 re-measured every
count and got the same nine numbers, at 0.08–0.97 s; the timings above are the conservative pair.

The added paths:

```
pytest-10210:  src/_pytest/_version.py
               src/pytest.egg-info/{PKG-INFO,SOURCES.txt,dependency_links.txt,
                                    entry_points.txt,requires.txt,top_level.txt}
sqlglot-6927:  sqlglot/_version.py
               sqlglot.egg-info/{PKG-INFO,SOURCES.txt,dependency_links.txt,
                                 requires.txt,top_level.txt}
```

Both are `setuptools`/`setuptools_scm`. **click generates nothing**, because its backend is
`flit_core`, which writes into site-packages and never into the tree — which is exactly why the
worked example in `HARVESTING.md` has never surfaced this, and why the whole class went
unmeasured until a `setuptools_scm` repo was cut.

### 1.3 The silent case, on a task that gated GREEN

`sqlglot/__init__.py:62`:

```python
try:
    from sqlglot._version import __version__, __version_tuple__
except ImportError:
    logger.error("Unable to set __version__, run `pip install -e .` or `python setup.py develop` first.")
```

Measured, inside the task image:

```
$ docker run --rm --entrypoint sh bakeoff-task-sqlglot-6927-dremio-trycast:v1 \
    -c 'cd /repo && python -c "import sqlglot; print(sqlglot.__version__)"'
0.0.0                                            # the IMAGE, with the generated file

$ docker run --rm --entrypoint sh bakeoff-task-sqlglot-6927-dremio-trycast:v1 \
    -c 'cd /repo && rm -f sqlglot/_version.py && python -c "import sqlglot; print(getattr(sqlglot, \"__version__\", \"<MISSING>\"))"'
Unable to set __version__, run `pip install -e .` or `python setup.py develop` first.
version attr: <MISSING>                          # the RUN TREE
```

`sqlglot-6927` passed its gate. So today, on every arm of that task, every `import sqlglot` emits
that error line and `sqlglot.__version__` does not exist — and nothing in the record, the verdict
or the log says so. That is a genuine "plausible-looking zero": the environment the record pins by
`container_image_digest` is not the environment the run got.

### 1.4 The loud-looking case, and what is measurement versus inference

`pytest-10210`'s suite imports the generated module unconditionally
(`src/_pytest/assertion/rewrite.py:42`, `from _pytest._version import version`). **Measured**, in
the run tree's condition:

```
$ docker run --rm --entrypoint sh bakeoff-task-pytest-10210-approx-nested-container:v1 \
    -c 'cd /repo && rm -f src/_pytest/_version.py && python -m pytest --co -q >/dev/null 2>&1; echo $?'
1
$ ... 2>&1 >/dev/null | tail -1
ModuleNotFoundError: No module named '_pytest._version'
```

The traceback is *runpy's*, not pytest's: pytest never starts.

**Inference, not observation** — and this plan previously stated it as though it were observed:
`EXIT_TESTS_FAILED` (`1`) is in the bare-runner branch's accepted tuple (`preflight.py`, the
`elif bare.exit_code not in (...)` guard), so the probe **would** read that 1 as an ordinary test
failure and pass the task. **No gate run has ever produced that result.** The stored verdict
`~/.cache/bakeoff/preflight/pytest-10210-approx-nested-container.json` is
`preflight_version: "11"` with `bare_runner_exit`, `bare_runner_argv` and `bare_runner_skipped`
all `null`; the bare-runner probe landed at 13, and disk is 14. The w4 report's five gate runs all
predate it (sqlglot's stored verdict is 12, click's 11 — same story). §7.7 is the run that turns
this inference into an observation, and under this plan its expected outcome is a **NO-GO**.

The `tests.runner` heredoc that manifest carries fixes preflight's five invocations, the oracle's
two and the grader's ladder, and fixes **nothing for the agent**: the agent never runs
`tests.runner`. So the invariant *"The agent must be able to check its own work"* is violated from
turn one, on every arm, and no check in the harness looks.

### 1.5 What an offline rebuild at run time would cost (the option-B measurement)

```
$ docker run --rm --network none --entrypoint sh bakeoff-task-click-...:v1 \
    -c 'cd /repo && pip install -e . --no-build-isolation --no-deps'
pip._vendor.pyproject_hooks._impl.BackendUnavailable: Cannot import 'flit_core.buildapi'

$ docker run --rm --network none --entrypoint sh bakeoff-task-click-...:v1 \
    -c 'cd /repo && pip install -e .'
exit code: 1                       # build isolation needs the network

$ docker run --rm --network none --entrypoint sh bakeoff-task-pytest-...:v1 \
    -c 'cd /repo && pip install -e . --no-build-isolation --no-deps'
Successfully installed pytest-0.0.0
```

An offline editable install is a **per-repo property**, not a harness one: it works for pytest
(its backend, `setuptools` + `setuptools_scm`, happens to be installed) and fails for click (its
backend, `flit_core`, went into an ephemeral isolated build env at image-build time and is not in
the image). The very first task in the task set cannot do it.

### 1.6 The listing mechanism, measured

`docker create --entrypoint true <image>` + `docker cp <cid>:/repo/. -` + `tarfile`:

- 0.08–1.75 s per task across the nine (two independent measurement passes), on a 2-CPU Docker VM.
- The container is **created and never started**, so nothing is assumed about binaries inside the
  image (`docker run --entrypoint find` would report an empty `/repo` on an image without `find`,
  which is byte-identical to a build that wrote nothing — the exact false negative this
  measurement exists to rule out). Bare `docker create <image>` fails `no command specified` on a
  task image (`ENTRYPOINT []`, no `CMD`); `--entrypoint true` supplies one that is never executed,
  and accepts the bare `sha256:` id `build_task_image` returns.
- Member names arrive `./`-prefixed with a bare `.` directory member; directories are their own
  members; **symlinks are preserved as symlink members** (bidict's image carries 3, chimera's 1),
  so `issym()` is load-bearing rather than defensive. Measured on sqlglot: 300 members, 299
  `./`-prefixed, `./sqlglot/_version.py` among them.
- **A `.git` directory inside an image IS exported** by `docker cp` (verified on a purpose-built
  image whose build ran `git init`). That is why D2 does not filter `.git` on the image side.
- The tar carries the whole tree's *content*, not just names — 15 MB for sqlglot — so it is
  streamed (`tarfile.open(mode="r|")`), never buffered.

### 1.7 What else can be red under `--co`, and what actually answers 1

The premise of the refusal in D9. Bare argv, i.e. `python -m pytest --co -q`, run in each python
task image with any build-generated file removed (the run tree's condition):

| image | bare `--co` exit |
|---|---|
| `click-3360` | 0 |
| `sqlglot-6927` | 0 |
| `bidict-389` | 0 |
| `werkzeug-3037` | 0 |
| `tomlkit-514` | 0 |
| `chimera-228` | **4** (a usage error; pre-existing, see §7.9) |
| `pytest-10210` | **1** |

And the deliberately-broken shapes, injected into click's image:

| shape | exit |
|---|---|
| module-level `import nosuchmod` in a test file | 2 |
| syntax error in a test file | 2 |
| `conftest.py` import error | 4 |

So `1` is not reachable by any red start state — collection failures answer 2 and conftest
failures answer 4 — and across seven python images exactly one answers 1: the one whose suite
imports a module the image build generated. `preflight.py`'s own comment on that branch already
says *"1 cannot happen with `--co` (kept in the accepted set anyway, since `--co` never selecting
tests is a property of the argv this probe controls, not one worth re-deriving here)"* — a
defensive acceptance, and this item is the measurement of the one thing it swallows.

---

## 2. The decision

**(A) — document the contract, make the gate record the class, and refuse the one shape that is
measurably inadmissible.** Concretely:

1. `image.build` keeps running at image build time, against the `git archive base_sha` scaffold.
   Nothing in `images.py`'s build order changes.
2. The run tree keeps being `start_sha` and only `start_sha`. Nothing is copied into it.
3. `preflight` measures the difference and records it: `build_generated_paths`,
   `build_generated_count` and `build_generated_state`. **That measurement refuses nothing.**
4. The bare-runner probe stops accepting exit **1**, which under `--co` cannot mean a failing test
   and does mean "`python -m pytest` could not start in this image" (§1.7, D9). **That** is the
   refusal, and it is made against the agent's own command in the run tree rather than against a
   path list.
5. `HARVESTING.md` states the contract, both measured consequences, and the rule that follows: *a
   repo whose suite cannot import without a build-generated file is out*.

The separation the harness cannot make is **from the path list**: "the build generated a file" and
"the run needs that file" are different claims, and no property of a path name distinguishes them
— `sqlglot-6927` is a perfectly good task that generates six files. What the harness *can* do is
measure the consequence, which is what (4) is. The earlier draft of this plan generalised "cannot
decide from a path list" into "refuses nothing", and that was wrong.

---

## 3. Alternatives rejected

### 3.1 (B) Run `image.build` against the materialized tree at container start

Rejected on four grounds, the first two measured:

- **It cannot be a harness rule, because it is a per-repo property.** §1.5: click's editable
  install is impossible offline in either form; pytest's works. B has to work for *every* task, and
  it fails on the reference task.
- **Its claimed benefit does not exist.** The stated upside was that the editable install's
  `.pth`/`.egg-info`/finder would then point at the run tree. They already do: an editable install
  records an **absolute path** (`/repo`), not an inode, and `/repo` at run time *is* the run tree.
  That is precisely why `pip install -e .` works here at all and why `pip install .` is silently
  wrong (`images.py` module docstring). Nothing about the import path is fixed by inverting the
  order.
- **It unpins the environment.** `Versions.container_image_digest` is the record's claim about
  what the run ran inside. Move the dependency install to container start and the digest stops
  describing it: two arms of one task would each build their own environment, and §5.4's "identical
  across arms" would rest on `pip` resolving identically, offline, twice.
- **It costs per run what it now costs per task.** pytest's `image.build` step is a full editable
  build; the matrix runs ~720 cells plus an oracle and a ladder per graded record.

### 3.2 (C) Copy build-generated, gitignored files into the run tree at container start

Both measured cases are gitignored upstream (`pytest/.gitignore:22` `src/_pytest/_version.py`;
`sqlglot/.gitignore:143` `sqlglot/_version.py`; both also carry `*.egg-info`), so the copy would be
invisible to `git status --porcelain` and to every submission diff, and would fix `pytest-10210`
outright. Rejected:

- **The path list has to survive from image build to container start, and there is no cheap
  channel.** `execute_run` receives an image digest, not a build context. The options are to bake a
  manifest into the image (changes the generated Dockerfile → changes every task's image id →
  invalidates every warm verdict and every stored digest, and adds two `find` passes to every
  build) or to carry it out-of-band through the driver, in a cache whose verdict outlives the
  artifact it describes — the shape `HANDOFF.md` and the preflight-cache invariant both warn about.
- **It has to fire identically in four places or it is worse than the bug.** The agent's container,
  preflight's, the oracle's and the grader's. If the grader's copy fires and the agent's does not,
  the model is graded in an environment it never saw — the same class as the `--index` stat-cache
  defect, which stamped `resolved: False` on every submission.
- **An agent destroys it, differentially.** `git clean -xfd` is a thing a coding agent reasonably
  does, and it removes exactly the ignored files C would have planted. One arm cleans, another does
  not, and the two are then running different suites — a §5.4 confound manufactured by the harness.
  Under the status quo the file is absent for everybody and an agent's own `pip install -e .`
  (visible in its trajectory) is the only thing that changes that.
- **It only half-closes the class.** A build-generated path that is *not* ignored may not be copied
  (it would dirty the tree and land in every submission diff), so those tasks stay broken — now
  with a harness step that fires for some paths and not others.
- **It buys one task, which is inadmissible for an independent reason** — the one D9 refuses.

### 3.3 (A) without any harness change — docs only

This was the expected shape of the item and §1.3/§1.4 killed it. A documented contract closes the
class only for an author who reads the document *and* correctly guesses which of their repo's
imports are build-generated. One measured instance loses an attribute silently on a green gate;
the other breaks the agent's own verification behind an exit code the probe was built to inspect
and then accepted defensively. Nothing in the harness looks at the image's `/repo` after the build,
and nothing looks at what exit 1 means under `--co`.

**Cost of the two harness changes, honestly:** one `docker create`/`cp`/`rm` per task per
*uncached* gate (0.08–1.75 s measured), one exit-code branch, and a `PREFLIGHT_VERSION` bump. The
bump's marginal cost in this round is zero — items 1, 2, 3 and 5 each already bump it.

### 3.4 Considered and not built: refusing on `build_generated_paths` ∩ `tests.runner`

`pytest-10210`'s runner regenerates `src/_pytest/_version.py` in a heredoc, so a substring test of
the generated paths against the declared `tests.runner` argv would name that dodge exactly. Not
built, and not filed: it is strictly weaker than D9 (a runner may name a path innocently, and a
repo whose `conftest.py` regenerates the file is admissible and would not fire it), and it would be
a second, partially-overlapping refusal for one class. D9 measures the run; this would measure the
author's prose.

---

## 4. Design

### D1. The measurement is *image minus run tree*, not *image minus build context*

Both would find the generated files. The run-tree comparison is chosen because:

- It measures the claim that matters — "these files exist in the image and will not exist at run
  time" — rather than a proxy for it.
- It needs no new plumbing. `preflight` already holds `image` and `repo_path`; the build context
  would have to be threaded from `run_matrix`/`grade.py` through a new parameter, and the path
  `build_root/image-<task_id>/repo` would become a second convention that can drift from
  `build_task_image`'s own.
- Its one asymmetry is harmless and in the safe direction: a path that `.gitattributes
  export-ignore` keeps out of `git archive` is in the run tree and not in the image, so it lands in
  the *other* difference, which this measurement does not report.

### D2. `images.scaffold_only_paths(image, run_tree) -> list[str]`, and where `.git` is handled

Lives in `images.py` (it is a fact about an image), and splits into a daemon half and a pure half
so the arithmetic is unit-testable with no Docker:

```python
def scaffold_only_paths(image: str, run_tree: Path) -> list[str]:
    return sorted(_repo_paths_in_image(image) - _tree_paths(Path(run_tree)))
```

- `_repo_paths_in_image(image) -> set[str]` — `docker create --entrypoint true <image>`,
  `docker cp <cid>:/repo/. -` streamed through `tarfile.open(mode="r|")`, `docker rm -f` in a
  `finally`. Regular files, symlinks and hardlink members; `./` stripped; the bare `.` member
  dropped. **No `.git` filter.** Raises `ImageError` on a non-zero `docker cp`.
- `_tree_paths(root) -> set[str]` — `os.walk(root)` **pruning `.git` at every depth** (directories
  named `.git` removed from `dirnames`, files named `.git` skipped — a submodule's gitfile is a
  file), collecting files and symlinks (including symlinks *to* directories, which `os.walk`
  reports in `dirnames` and does not descend) as `/`-separated relative paths.

**Why the asymmetry, and why it is not "for walk cost".** The run tree's `.git` is that clone's own
repository metadata; it is not repository *content*, and it must not cancel build residue that
happens to share a path. Measured (§1.6): `docker cp` does export a `.git` an `image.build` step
created, and `git archive` never writes one, so in the normal case the image side contributes no
`.git` paths at all and the prune changes nothing. In the abnormal case — a build that ran
`git init` inside `/repo` — the residue is reported, which is exactly what an author needs to see.
Filtering `.git` on the image side would silently drop that, and would also make the tree-side
prune unobservable, which is what review 1 caught: a prune whose effect no test can separate is a
prune no mutation can catch.

**Why `docker create` and not `docker run`:** §1.6. An image without `find` would answer an empty
`/repo`, which is byte-identical to a build that wrote nothing.

**Why not `docker export` / `docker save`:** whole rootfs, gigabytes, for a directory that is tens
of megabytes.

### D3. Three evidence keys, because one cannot say which absence it is and prose is not a number

```
build_generated_paths : list[str] | None
build_generated_count : int | None
build_generated_state : str
```

| state | `_paths` | `_count` | meaning |
|---|---|---|---|
| `"not_attempted"` | `None` | `None` | the gate returned before the scan (the runner-gate early return) |
| `"scanned"` | `[]` or the full sorted list | `len(paths)` | the scan ran; the list is complete |
| `"truncated: <n> paths, first 100 listed"` | the first 100, sorted | `<n>`, the true total | the scan ran; the list is not complete |
| `"failed: <message>"` | `None` | `None` | the scan was attempted and could not complete |

`None` alone would mean "not attempted" and "failed" at once, and `[]` would mean "measured,
nothing" and "the scan died before it measured" at once — the shape `PREFLIGHT_VERSION` 11 already
had to fix for three other keys, and the shape `wire.metadata.resolved_state` exists for. The
**count** is a separate key rather than a number inside the state sentence because this repo's
precedent for "the record says how much of its own input it could not read" is a number beside the
derived field (`wire_entries_seen`/`wire_entries_distinct`, `transcript_malformed_lines`,
`stdout_malformed_lines`), never prose a reader has to regex — and because it lets the truncation
test assert an integer instead of an English string.

`_BUILD_GENERATED_LIMIT = 100` caps the list; a build that ran `npm install` inside `/repo` would
otherwise put twenty thousand paths in a verdict blob. **The cap bounds the verdict, not the
transfer**: `docker cp` has no names-only mode, so the scan streams the whole tree's content
regardless. Measured worst case in the corpus is `yaml-474` at 1842 paths / 1.75 s, and the one
node image keeps `node_modules` outside `/repo` (`ufo-214`, 40 paths); a task whose `image.build`
ran `npm install` inside `/repo` would stream hundreds of megabytes through the gate once per
uncached preflight.

The runner-gate early return leaves the seeded values in place rather than writing its own reason
string, where `bare_runner_skipped` one seed above writes one. That asymmetry is deliberate: the
`bare_runner_*` triple has two distinct skip reasons (a bad runner, and a node framework with no
addopts analogue) and needs to say which; this scan has exactly one, and `"not_attempted"` names
it.

### D4. The scan may not cost the gate, and where it sits

Wrapped in `except Exception`, recording `failed: <exc>` and continuing. A missing `docker` binary
(`FileNotFoundError`), a daemon that went away mid-gate (`ImageError`) and a malformed tar are each
a statement about this machine, not about the task, and this measurement refuses nothing — turning
any of them into a NO-GO would let a diagnostic break a gate that was otherwise green. Same
containment as `checkpoint_error`, and the state string is what keeps the containment from being
silent.

It runs **after** the runner-gate early return and **before** `with RunContainer(...)`, for two
reasons and not a third: the early return legitimately records `not_attempted` and starts no
container, and a manifest that is already refused should not pay a docker call. It is **not**
because the bind mount would hide anything — the scan creates its *own* container from the image
and never starts it, and that container has no mount. The scan returns the same set called before,
inside or after the `with` block; the placement is a cost and a shape choice, not a correctness
one, and the comment must say so.

### D5. `EVIDENCE_KEYS` (item 5)

All three keys are seeded in `preflight()` alongside `bare_runner_skipped` and appended to
`EVIDENCE_KEYS` in write order, so `PreflightResult.__post_init__` accepts both returns and
`test_evidence_keys_lists_exactly_what_preflight_writes` stays green. If item 5 has not landed when
this one is implemented, that step does not exist; the seeds still do.

### D6. `PREFLIGHT_VERSION` +1

Two things move, and the second is the stronger reason:

1. Three keys join the evidence schema. A verdict written by the previous gate carries none of
   them, and under item 5's post-condition an absent key is not a legal shape at all — so a reader
   with two blobs in hand could not tell "the gate did not look" from "the gate looked and found
   nothing".
2. **The gate's GO/NO-GO changes** (D9). A cached PASS written under the old version may describe a
   task this gate refuses, which is precisely what the version exists to retire. A cached NO-GO is
   unaffected: nothing here can turn a NO-GO into a GO.

Cost: one offline re-gate per task per driver (`preflight.json` and `preflight-grade.json`
invalidate together, both keyed through `preflight_cache_key`). Marginal cost in this round: zero —
items 1, 2, 3 and 5 each bump it already.

### D7. The driver prints it, on the cached path too

Evidence in a JSON blob nobody opens is not "the author sees what will vanish" — and that premise
applies with equal force to a warm gate, which is the normal case once a task is authored.
`run_matrix.resolve_tasks` returns from the cached branch *before* `preflight()` is called, so the
note is printed from **both** sites: from the fresh verdict after `preflight(...)` (on the NO-GO
path as well as the PASS path — a task whose gate fails *because* of a vanished file is precisely
this line's reader), and from `cache/preflight/<task_id>.json` on the cached branch. The cached
read is best-effort and swallows `OSError`/`JSONDecodeError`: the note is a courtesy and may not
break a warm gate that is otherwise fine.

`grade.py` does not print it: its audience is a grading run, and the manifest is frozen by then.

### D8. Nothing else moves

No Dockerfile text, no `render_dockerfile` output, no image id, no `RunRecord` field, no
`GradeRecord` field, no ladder command, no oracle input. The scan is read-only and runs outside
every container the gate starts.

### D9. Exit 1 from the bare-runner probe is refused

The probe's argv is `timeout <n> <python> -m pytest --co -q` — **collect-only**. No test is ever
executed, so a legitimately red start state cannot reach its exit code; collection failures answer
2 and conftest failures answer 4 (§1.7, measured twice). `1` was left in the accepted set
defensively, and the branch's own comment says why. The one thing that produces it is the
interpreter dying before pytest starts — which on the measured corpus is exactly the class this
item is about.

So: remove `EXIT_TESTS_FAILED` from the accepted tuple and give `1` its own branch with its own
message. Not the catch-all's message: `_PROCESS_EXIT_MEANING`/`adapter.explain` would render 1 as
*"tests failed"*, which are the wrong words for a command that never ran a test. The message quotes
the **last non-empty line of stderr** (measured: `ModuleNotFoundError: No module named
'_pytest._version'`, the useful line, where the usage-error branch's `": error:"` selector matches
nothing) and points at `evidence.build_generated_paths`, so the two halves of this commit meet
where the author reads them.

When stderr is empty the clause becomes *"stderr was empty -- the diagnostic went to stdout or
nowhere, so the exit code and `evidence.bare_runner_exit` are the whole of it"* rather than
`Last line of stderr: .` — reachable (chimera-228's exit 4 leaves an empty stderr, measured, and
nothing says a 1 cannot), and a refusal whose only human-readable clue is a bare full stop is
exactly what this file's other messages are written against. Pinned by
`test_the_refusal_says_so_when_stderr_is_empty`.

No stderr *matching* is used to decide the refusal — the exit code alone decides. A
`ModuleNotFoundError` grep would have a false-positive surface (a repo whose collection legitimately
prints one) that the exit code does not.

Zero false NO-GOs on the measured corpus: five of seven python images answer 0, `chimera-228`
answers 4 (refused by the pre-existing usage-error branch, not by this one — §7.9), and only
`pytest-10210` answers 1.

---

## 5. Tasks

### Task 0 — read the constants off disk

- [ ] `grep -n 'PREFLIGHT_VERSION: str' bakeoff/src/bakeoff/preflight.py` — record the value (it is
      `"14"` at HEAD 8232032, at `preflight.py:190`). The bump is that value plus one. Do **not**
      transcribe a literal from this plan.
- [ ] `grep -n 'EVIDENCE_KEYS' bakeoff/src/bakeoff/preflight.py` — record whether item 5 has landed.
      If it has, note the tuple's line and its current length.
- [ ] `cd bakeoff && .venv/bin/python -m pytest tests/ -q` — record the baseline
      `N passed, M deselected` before touching anything. §7.1 is that number plus the deltas.

### Task 1 — `images.py`: the measurement

*(re-locate by name; `images.py` is 538 lines at HEAD 8232032, `image_entrypoint` at :114,
`build_task_image` at :459)*

- [ ] Add `import os`, `import tarfile` at module scope (`subprocess` and `Path` are already
      imported).
- [ ] After `image_entrypoint`, add `_repo_paths_in_image`, `_tree_paths` and
      `scaffold_only_paths` per D2. Docstrings carry: §1.2's click-vs-pytest contrast, the
      `docker create` reason from §1.6, D2's `.git` asymmetry with the measured fact that a `.git`
      in an image *is* exported, and the sentence that this measurement refuses nothing.
- [ ] `_repo_paths_in_image` shape, exactly:

```python
def _repo_paths_in_image(image: str) -> set[str]:
    # "/repo" is render_dockerfile's `COPY repo /repo`, which is the only place
    # this path is decided.
    #
    # No `.git` filter here, on purpose: `git archive` never writes one, so in
    # the normal case there is nothing to filter, and an `image.build` step that
    # ran `git init` leaves residue `docker cp` DOES export (measured
    # 2026-09-02) and an author does need to see. `_tree_paths` prunes the run
    # tree's own `.git` instead -- that is the clone's metadata, not repository
    # content, and letting it cancel build residue by name would hide exactly
    # this case.
    container = _run(["docker", "create", "--entrypoint", "true", image])
    try:
        proc = subprocess.Popen(
            ["docker", "cp", f"{container}:/repo/.", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        paths: set[str] = set()
        try:
            with tarfile.open(mode="r|", fileobj=proc.stdout) as tar:
                for member in tar:
                    if not (member.isfile() or member.issym() or member.islnk()):
                        continue
                    name = member.name[2:] if member.name.startswith("./") else member.name
                    if name:
                        paths.add(name)
        finally:
            # stderr is read only after the stream is drained or closed. Docker
            # writes progress to stderr, so a pipe that filled DURING the stream
            # would deadlock -- measured 2026-09-02 across all nine probe images
            # (up to 15 MB streamed, 0.08-1.75 s, `wait()` 0 every time, no
            # truncation and no hang), and closing stdout first gives `docker cp`
            # an EPIPE rather than a reader that never returns.
            if proc.stdout is not None:
                proc.stdout.close()
            stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            code = proc.wait()
        if code != 0:
            raise ImageError(f"docker cp {image}:/repo failed (exit {code}):\n{stderr}")
        return paths
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
```

- [ ] `_tree_paths` shape, exactly:

```python
def _tree_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        here = Path(dirpath)
        for name in filenames:
            if name == ".git":       # a submodule's gitfile
                continue
            paths.add((here / name).relative_to(root).as_posix())
        for name in dirnames:
            if (here / name).is_symlink():
                paths.add((here / name).relative_to(root).as_posix())
    return paths
```

- [ ] `scaffold_only_paths` is the one-line difference in D2, in that operand order.

### Task 2 — `preflight.py`: seed, scan, record, and refuse exit 1

*(re-locate by name; at HEAD 8232032 the seeds sit at :907–909, `with RunContainer(` at :972, and
the bare-runner branch at :1134–1203)*

- [ ] `from bakeoff.images import scaffold_only_paths` at module scope (`preflight.py` already
      imports from `bakeoff.container`; `images` imports `tasks` only lazily, so there is no cycle).
- [ ] `_BUILD_GENERATED_LIMIT: int = 100` beside `PREFLIGHT_VERSION`.
- [ ] Seed all three keys immediately after `evidence["bare_runner_skipped"] = None`, with a `#:`
      comment in the file's style carrying §1.3's sqlglot measurement and D3's table:

```python
    evidence["build_generated_paths"] = None
    evidence["build_generated_count"] = None
    evidence["build_generated_state"] = "not_attempted"
```

- [ ] Immediately before `with RunContainer(image=image, ...)`:

```python
    # AFTER the runner-gate early return (which starts no container and records
    # `not_attempted` honestly) and before the run container, so a manifest that
    # is already refused pays no docker call. NOT because the bind mount would
    # hide anything: `scaffold_only_paths` creates its own container from the
    # image and never starts it, and that container has no mount -- the same set
    # comes back called before, inside or after the block below.
    #
    # Measured 2026-09-02 -- sqlglot-6927's build writes sqlglot/_version.py, the
    # run tree has no such file, and `import sqlglot` logs "Unable to set
    # __version__" on every arm of a task whose gate is green. Nothing HERE
    # refuses: "generated" and "required" cannot be told apart from a path list.
    # The refusal that can be made is the bare-runner exit code below.
    try:
        scaffold_only = scaffold_only_paths(image, Path(repo_path))
    except Exception as exc:  # noqa: BLE001 -- a diagnostic may not break a gate
        evidence["build_generated_state"] = f"failed: {exc}"
    else:
        evidence["build_generated_paths"] = scaffold_only[:_BUILD_GENERATED_LIMIT]
        evidence["build_generated_count"] = len(scaffold_only)
        evidence["build_generated_state"] = (
            "scanned" if len(scaffold_only) <= _BUILD_GENERATED_LIMIT
            else f"truncated: {len(scaffold_only)} paths, "
                 f"first {_BUILD_GENERATED_LIMIT} listed"
        )
```

- [ ] **The refusal (D9).** In the bare-runner branch, insert a new `elif` after the `124` branch
      and before the catch-all, and drop `EXIT_TESTS_FAILED` from the catch-all's tuple so the
      tuple names only codes that are genuinely accepted:

```python
            elif bare.exit_code == EXIT_TESTS_FAILED:
                stderr_lines = [
                    line for line in bare.stderr.strip().splitlines() if line.strip()
                ]
                last_line = stderr_lines[-1] if stderr_lines else ""
                # A refusal whose only human-readable clue renders as a bare full
                # stop is the shape this file's comments call out elsewhere. The
                # bare command can write its diagnostic to stdout or to nothing at
                # all, and the exit code plus evidence.bare_runner_exit still carry
                # the refusal -- so say that rather than printing "stderr: .".
                clue = (
                    f"Last line of stderr: {last_line}" if last_line
                    else "stderr was empty -- the diagnostic went to stdout or "
                         "nowhere, so the exit code and evidence.bare_runner_exit "
                         "are the whole of it"
                )
                problems.append(
                    "the bare pytest collection "
                    f"(`{' '.join(bare_argv)}`) exited 1, which under --co "
                    "cannot mean a failing test: no test is executed, so 1 "
                    "means `python -m pytest` could not start in this image -- "
                    "and that is the command the agent will naturally type. "
                    f"{clue}. Measured 2026-09-02: a "
                    "module-level import error in a test file exits 2, a syntax "
                    "error exits 2 and a conftest.py import error exits 4, so "
                    "nothing a legitimately red start state does reaches this "
                    "branch. The measured cause is an import of a module the "
                    "IMAGE BUILD generated inside /repo, which the run tree does "
                    "not have -- see evidence.build_generated_paths."
                )
```

      and

```python
            elif bare.exit_code not in (
                EXIT_ALL_PASSED, EXIT_COLLECTION_INTERRUPTED, EXIT_NOTHING_COLLECTED,
            ):
```

      The trailing `# 0, 2, 5 are not a problem: ...` comment is rewritten: its "and 1 cannot
      happen with --co (kept in the accepted set anyway ...)" clause becomes the reason 1 is now
      refused, citing §1.7's table and the pytest-10210 measurement. No `problem_codes` entry — 35
      of this file's 38 problems carry none, and the three that do exist for the scope checks.

- [ ] `PREFLIGHT_VERSION`: Task 0's value plus one, with a comment paragraph in the file's style
      whose header is `#: <old> -> <new>:` and whose body is D6 — three evidence keys, **and** a
      changed GO/NO-GO, so a cached PASS may describe a task this gate refuses.
- [ ] If item 5 landed: append all three keys to `EVIDENCE_KEYS` in write order (immediately after
      `bare_runner_skipped`).

### Task 3 — `run_matrix.py`: the note, at both sites

*(re-locate by name: `resolve_tasks`; at HEAD the cached branch is :285–290 and the
`result = preflight(` call is :291)*

- [ ] Two module-level helpers:

```python
def _stored_evidence(path: Path) -> dict:
    """The verdict beside the cache key, or {}.

    Best-effort by design: the note below is a courtesy and may not break a warm
    gate whose verdict file was hand-deleted, truncated or written by a driver
    that predates these keys.
    """
    try:
        return json.loads(Path(path).read_text()).get("evidence") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _print_build_generated(evidence: dict) -> None:
    generated = (evidence or {}).get("build_generated_paths") or []
    if not generated:
        return
    total = (evidence or {}).get("build_generated_count") or len(generated)
    shown = ", ".join(generated[:5])
    more = " ..." if total > 5 else ""
    print(
        f"note      the image build wrote {total} file(s) into /repo that the "
        f"run tree does not have; nothing the agent, the gate, the oracle or "
        f"the grader runs will see them: {shown}{more}"
    )
```

- [ ] In the cached branch, immediately after
      `print("preflight cached PASS (--force-preflight to re-run)")`:

```python
                _print_build_generated(
                    _stored_evidence(cache / "preflight" / f"{task.task_id}.json"))
```

- [ ] After the `preflight(...)` call and the `write_json(...)` beside it, before
      `if not result.ok:`:

```python
        _print_build_generated(result.evidence)
```

### Task 4 — tests

Every name below is final; see §6 for what each asserts.

### Task 5 — mutation anchors

Three entries in `scripts/mutation_check.py`, each a 6-tuple
`(name, file, old, new, test, marker)` with a comment in the file's style:

- [ ] **direction** — `"images: report what the run tree has and the image does not"`,
      `"src/bakeoff/images.py"`,
      old `"    return sorted(_repo_paths_in_image(image) - _tree_paths(Path(run_tree)))"`,
      new `"    return sorted(_tree_paths(Path(run_tree)) - _repo_paths_in_image(image))"`,
      test `"tests/test_images.py -k only_the_run_tree_has"`, marker `"not integration"`.
      *Comment:* the reversed set is every file the test half added plus everything
      `export-ignore` kept out of `git archive` — a long, plausible, entirely wrong list, printed
      by the gate as "what will vanish".
- [ ] **tree-side `.git` prune** — `"images: let the run tree's own git metadata cancel build residue"`,
      `"src/bakeoff/images.py"`,
      old `"        dirnames[:] = [name for name in dirnames if name != \".git\"]"`,
      new `"        dirnames[:] = list(dirnames)"`,
      test `"tests/test_images.py -k git_residue"`, marker `"not integration"`.
      *Comment:* the run tree always has a `.git`; the image normally has none. Under the mutation
      the tree's metadata enters the subtrahend and cancels, by path name, the residue an
      `image.build` step that ran `git init` left in the image — the one case where the image side
      carries `.git` paths at all, and the one this prune exists to keep visible.
- [ ] **the exit-1 refusal** — `"preflight: accept a bare pytest that cannot start as a failing suite"`,
      `"src/bakeoff/preflight.py"`,
      old `"            elif bare.exit_code == EXIT_TESTS_FAILED:"`,
      new `"            elif False:"`,
      test `"tests/test_preflight.py -k cannot_start"`, marker `"not integration"`.
      *Comment:* the mutation sends exit 1 to the catch-all, which renders it as "tests failed" —
      the words `_PROCESS_EXIT_MEANING` has for a code that under `--co` cannot mean that, and the
      exact reading that let pytest-10210's broken agent-side command through.

### Task 6 — docs

- [ ] `bakeoff/taskset/HARVESTING.md`, **The image** — three edits, §9.1:
      (a) amend the **No VCS-derived version** bullet in place; (b) add the new bullet after it;
      (c) correct the section's closing "preflight already runs the suite inside the image"
      sentence.
- [ ] `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` — append to the comment block
      immediately above the line `build: ["pip install -e ."]` (*re-locate by name*: that line, and
      the comment block ending `green-after check is what catches it.` — quoted verbatim and
      verified 2026-09-02. An earlier draft of this plan wrote "...catches **that**.", which greps
      to nothing: the exact failure that replacing a line number with a name exists to avoid, so
      the anchor is a `grep -n` before it is an edit), text in §9.2. This moves the
      manifest's `manifest_digest` and therefore that task's cached verdict; it is the cache key
      doing its job, and the `PREFLIGHT_VERSION` bump invalidates it anyway.
- [ ] `CLAUDE.md`, **Invariants** — one bullet, text in §9.3.
- [ ] `TASKS.md` — remove the item-14 bullet (`grep -n 'writes INTO' TASKS.md`), and add the one
      follow-up bullet from §11, which carries both the pytest-10210 verdict and the sqlglot note.
- [ ] `tasks/todo.md` — a review-log section for this item, per the round's process, recording that
      the exit-1 refusal was added on review 1's ruling and what it was measured against.

---

## 6. Tests, by name

### `bakeoff/tests/test_images.py` — unit, no daemon

All six monkeypatch `images._repo_paths_in_image` to return a literal set and build the run tree
under `tmp_path`.

- [ ] `test_a_path_only_the_image_has_is_reported` — image `{"src/pkg/_version.py",
      "src/pkg/__init__.py"}`, tree carrying only `src/pkg/__init__.py`; asserts the result is
      exactly `["src/pkg/_version.py"]`. §1.2's measured shape.
- [ ] `test_a_path_both_sides_have_is_not_reported` — identical sets; asserts `[]`. A file the
      build rewrote in place is invisible here **by design** (§10) and the docstring says so.
- [ ] `test_only_the_run_tree_has_it_and_it_is_not_reported` — the tree carries `tests/test_new.py`
      (the test half) and the image does not; asserts `[]`. The direction anchor's test.
- [ ] `test_git_residue_in_the_image_is_reported_and_the_trees_own_git_is_not` — image
      `{".git/config", "calc.py"}`; the run tree contains a real `.git/config`, a real
      `.git/objects/ab/cdef` and `calc.py`. Asserts the result is exactly `[".git/config"]`: the
      image's build residue survives the difference, and the tree's own metadata neither appears in
      the report nor cancels it. The `.git`-prune anchor's test — it goes to `[]` under the
      mutation.
- [ ] `test_the_report_is_sorted` — image-only paths `{"b.py", "a.py", "c/d.py"}` supplied against
      an empty tree; asserts `result == ["a.py", "b.py", "c/d.py"]` (the explicit list, not
      `sorted(result)`, which passes on `[]` and on any set that happens to iterate in order).
- [ ] `test_a_symlink_in_the_run_tree_counts_as_present` — the tree has `link.py` as a symlink and
      the image has `link.py` as a regular file; asserts `[]`, i.e. the walk records symlinks
      rather than skipping them into a false "generated".

### `bakeoff/tests/test_preflight.py` — unit

A module-scoped **autouse** fixture `_scaffold_scan_is_stubbed_by_default` monkeypatches
`preflight.scaffold_only_paths` to `lambda *a, **k: []`. Its docstring: the real function shells out
to `docker create`, and a unit suite that reaches a daemon is a unit suite that stops running
offline — and on a machine with no `docker` binary it would take the `FileNotFoundError` path
rather than the one under test. The five evidence tests below override it explicitly.

- [ ] `test_the_gate_records_what_the_build_wrote_into_the_scaffold` — stub returns
      `["sqlglot/_version.py"]`; asserts `evidence["build_generated_paths"] ==
      ["sqlglot/_version.py"]`, `evidence["build_generated_count"] == 1`, and
      `evidence["build_generated_state"] == "scanned"`.
- [ ] `test_the_gate_does_not_refuse_a_task_whose_build_wrote_into_the_scaffold` — same stub on an
      otherwise-green fixture; asserts `result.ok` and that no problem string mentions
      `_version.py`. The "the path list decides nothing" pin.
- [ ] `test_a_scan_that_could_not_run_says_so_rather_than_reporting_nothing` — stub raises
      `images.ImageError("daemon gone")`; asserts `build_generated_paths is None`,
      `build_generated_count is None`, that `build_generated_state.startswith("failed: ")` and
      carries `daemon gone`, and that `result.ok` is unchanged (D4).
- [ ] `test_a_long_list_is_truncated_and_the_count_survives` — stub returns 101 paths
      (`f"generated/{i:04d}.py"`); asserts the recorded list has exactly 100 entries and is the
      first 100 sorted, `build_generated_count == 101`, and the state is
      `"truncated: 101 paths, first 100 listed"`.
- [ ] `test_the_runner_gate_early_return_says_the_scan_was_not_attempted` — a manifest whose
      `tests.runner` does not match `tests.framework` (the early return). Overrides the autouse
      fixture with a **counting** stub (`calls = []; lambda *a, **k: calls.append(1) or []`) and
      asserts `calls == []`, plus `build_generated_state == "not_attempted"` and both other keys
      `None`.
- [ ] `test_a_bare_pytest_that_cannot_start_is_refused` — modelled on
      `test_a_bare_exit_code_outside_the_accepted_set_is_a_problem` (*re-locate by name*): the fake
      container is built with `bare_runner=_Exec(exit_code=EXIT_TESTS_FAILED, stderr="  File
      \"<frozen runpy>\", line 112\nModuleNotFoundError: No module named '_pytest._version'\n")`.
      Asserts `result.ok is False`, that some problem contains `exited 1, which under --co cannot
      mean a failing test`, and that `evidence["bare_runner_exit"] == EXIT_TESTS_FAILED`.
- [ ] `test_the_refusal_quotes_the_last_line_of_stderr_not_the_first` — same shape, stderr whose
      first line is the runpy frame and whose last is the `ModuleNotFoundError`; asserts the problem
      contains `Last line of stderr: ModuleNotFoundError: No module named '_pytest._version'` and
      does **not** contain `<frozen runpy>`. (Measured: the usage-error branch's `": error:"`
      selector matches nothing in this stderr, which is why this branch selects differently.)
- [ ] `test_the_refusal_says_so_when_stderr_is_empty` — same shape, `stderr=""`; asserts the
      problem contains `stderr was empty` and does **not** contain `Last line of stderr: .`
      (measured: `chimera-228`'s bare `--co` exits 4 with an empty stderr, so an exit that leaves
      nothing behind is a real shape on this corpus, not a hypothetical).
- [ ] `test_the_preflight_version_moved_with_the_new_assertion` — **existing**; update the literal
      to Task 0's value plus one and append one clause to the docstring enumeration naming the
      three keys, the exit-1 refusal, and D6's reason.

`test_a_bare_collection_error_is_not_a_problem` (existing, exit 2) and
`test_a_bare_usage_error_names_the_plugin_and_the_flag` (existing, exit 4) must both still pass
unchanged — they are what pins that this refusal did not widen past 1. No existing test asserts
that exit 1 is accepted (checked).

### `bakeoff/tests/test_run_matrix.py` — unit

All three use the existing `_stub_resolve_tasks(monkeypatch, rm, on_preflight)` helper
(*re-locate by name*), which already stubs `build_task_image`, `image_entrypoint`, `materialize`,
`write_json` and `preflight` — so no test here reaches a daemon or a mirror.

**Extend `_preflight_result(task, kw, problems=())` (*re-locate by name*, beside that helper) with
an `evidence=None` parameter** passed through to `PreflightResult(evidence=evidence or {})`. It
currently relies on the dataclass default, and all three tests below need a result that carries
evidence; transcribing an inline `PreflightResult(...)` into each instead would be three copies of
a constructor whose field list moves with item 5.

- [ ] `test_the_driver_prints_what_the_build_wrote_into_the_scaffold` — a faked `preflight`
      returning a PASS whose evidence carries two paths and `build_generated_count: 2`; asserts the
      captured stdout contains `the image build wrote 2 file(s) into /repo` and both path strings.
- [ ] `test_the_driver_prints_nothing_when_the_build_wrote_nothing` — evidence carries `[]`;
      asserts `"the image build wrote"` is absent from stdout. (Seven of the nine measured tasks in
      §1.2 hit this branch.)
- [ ] `test_a_cached_pass_still_prints_what_the_build_wrote` — writes a real
      `cache/preflight/<task_id>.json` carrying the evidence, primes `preflight.json` with the
      matching cache key, and asserts stdout carries both `preflight cached PASS` and the note.
      `write_json` is stubbed to a no-op by the helper, so the verdict file must be written by the
      test itself; that is the point of the test — the note on this path comes off disk, not out of
      a `PreflightResult`.

### `bakeoff/tests/test_integration_build_outputs.py` — new module, `integration` + `task_image`

Module docstring carries **both** standing notes from `test_integration_submodules.py:19–31`:
(a) the marker rationale — `verify_logger.py` selects `-m "integration and not task_image"` and this
module builds a task image; and (b) the `--basetemp` rule — everything Docker has to see must live
under `$HOME`, because on macOS the default `tmp_path` is under `/var/folders`, which the Docker VM
does not mount, and a run tree bind-mounted from there appears inside the container as a silently
**empty** `/repo`. `test_the_gate_names_that_file_and_still_passes` runs the real `preflight` and is
exposed to exactly that.

Fixtures, module-scoped, modelled on that file's `workspace` / `superproject` (a local git repo,
**no** submodule, so neither `local_urls` nor `protocol.file.allow` is needed): a `calc.py` +
`tests/test_calc.py` repo, a reference diff, and a manifest whose `image.build` is
`["printf 'GENERATED = 1\\n' > calc_generated.py"]`.

- [ ] `test_a_file_the_build_writes_into_repo_is_absent_from_the_run_tree` — **the contract
      itself.** `materialize` the run tree, `build_task_image`, then assert
      `(run_tree / "calc_generated.py").exists() is False` and that the file *is* in
      `images._repo_paths_in_image(image)`. The claim `HARVESTING.md` will state, pinned against a
      daemon rather than against a docstring.
- [ ] `test_the_gate_names_that_file_and_still_passes` — run the real `preflight` against that image
      and tree; assert `result.ok`, `result.evidence["build_generated_state"] == "scanned"`,
      `result.evidence["build_generated_count"] == 1`, and `"calc_generated.py" in
      result.evidence["build_generated_paths"]`. Also exercises `_repo_paths_in_image` end to end
      against a real daemon, which no unit test does.

---

## 7. Verification

1. **Unit.** `cd bakeoff && .venv/bin/python -m pytest tests/ -q`. Expected against Task 0's
   baseline: **+17 passed, +2 deselected** (19 new `def`s — 6 in `test_images.py`, 8 in
   `test_preflight.py`, 3 in `test_run_matrix.py`, 2 in the new integration module, which collect
   and are then deselected by `addopts = "-m 'not integration'"`; no parametrization). One existing
   test changes without changing the count (the `PREFLIGHT_VERSION` pin literal), and one existing
   helper grows a parameter without changing the count (`_preflight_result`, §6). **The gate is
   zero failures**; explain any other difference rather than absorbing it.
2. **The offline gate.** `cd bakeoff && .venv/bin/python scripts/verify_logger.py` → `GATE PASSED`.
   It selects `-m "integration and not task_image"`, so the new integration module is correctly
   excluded; confirm by grepping the gate's own selection output for `test_integration_build_outputs`
   and finding nothing.
3. **Mutation.** Run **solo**: `cd bakeoff && .venv/bin/python scripts/mutation_check.py`. All three
   new anchors must go red on their named tests, and the reported total must move by exactly three.
4. **Integration.**
   `cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" -k build_outputs --basetemp="$HOME/.cache/bakeoff-pytest"`.
5. **The reference task, a real gate, expecting an empty measurement.**

   ```
   cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
       --tasks click-3360-write-usage-empty-args
   ```

   Expect `preflight PASS`, **no** `note` line, and in
   `~/.cache/bakeoff/preflight/click-3360-write-usage-empty-args.json`:
   `"build_generated_paths": []`, `"build_generated_count": 0`,
   `"build_generated_state": "scanned"`. §1.2 measured click at 0 added paths and §1.7 measured its
   bare `--co` at 0, so anything else here is a defect in this commit, not a discovery. Then run the
   same command **without** `--force-preflight` and confirm the cached branch prints
   `preflight cached PASS` and (correctly) no note.

6. **A real task that generates, expecting the six measured paths and a PASS.**

   ```
   cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
       --task-set ~/.cache/bakeoff-probe/taskset --tasks sqlglot-6927-dremio-trycast
   ```

   Expect `preflight PASS` (its bare `--co` is 0, measured), a `note` line reading
   `the image build wrote 6 file(s) into /repo ...`, `build_generated_count: 6`,
   `build_generated_state: "scanned"`, and `build_generated_paths` equal to, in this order:

   ```
   ["sqlglot.egg-info/PKG-INFO", "sqlglot.egg-info/SOURCES.txt",
    "sqlglot.egg-info/dependency_links.txt", "sqlglot.egg-info/requires.txt",
    "sqlglot.egg-info/top_level.txt", "sqlglot/_version.py"]
   ```

   (egg-info first: `.` is 0x2E, `/` is 0x2F.) Then re-run **without** `--force-preflight` and
   confirm the cached branch prints the same note off disk.

7. **MANDATORY — the task this item is the measurement of.** ~380 s.

   ```
   cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --force-preflight \
       --task-set ~/.cache/bakeoff-probe/taskset --tasks pytest-10210-approx-nested-container
   ```

   Expected: **`preflight NO-GO`**, with the D9 problem naming `exited 1, which under --co cannot
   mean a failing test` and quoting `ModuleNotFoundError: No module named '_pytest._version'`, plus
   the `note` line naming 7 files including `src/_pytest/_version.py`. This is the run that turns
   §1.4's inference into an observation; quote its output in `tasks/todo.md`. If it PASSES, the
   refusal did not land and the commit is not done.

   **On a machine that does not have `~/.cache/bakeoff-probe/taskset`, this step cannot run, and a
   missing directory is not a failed prerequisite.** That tree is the 2026-09-02 probe's output,
   lives outside this repository and is not versioned with it. The **portable** proof of the
   refusal is `test_a_bare_pytest_that_cannot_start_is_refused` plus the third mutation anchor,
   both of which run offline on any checkout; §7.7 is this machine's end-to-end confirmation
   against the real task that produced the measurement, and it is why the two exist. Skipping it
   for want of the directory is allowed; skipping it while the directory is present is not.

8. **Timing.** Record the wall delta between a `--force-preflight` gate before and after this commit
   for click. Expected: well under 1 s (0.11 s and 0.26 s measured in the two passes).

9. **Do not attribute `chimera-228` to this commit.** Measured 2026-09-02: its bare `--co` exits
   **4**, so it is refused by the pre-existing usage-error branch the moment it is re-gated under
   `PREFLIGHT_VERSION` 13 or later — its stored verdict is 12. Anyone re-gating the probe set after
   this commit will see that NO-GO and it is not this one's doing.

---

## 8. Version constants

| constant | moves | why |
|---|---|---|
| `PREFLIGHT_VERSION` | **+1** from disk | D6 — three evidence keys join the schema **and** the GO/NO-GO changes (D9), so a cached PASS may describe a task this gate refuses |
| `SCHEMA_VERSION` | no | no `RunRecord` field changes |
| `GRADE_SCHEMA_VERSION` | no | no `GradeRecord` field changes |
| `GRADER_VERSION` | no | the ladder runs the same commands against the same oracle |
| `ORACLE_VERSION` | no | the oracle's input is unchanged |
| `container_image_digest` | no | no Dockerfile text moves; `render_dockerfile`'s output is byte-identical |
| `manifest_digest` (click only) | yes, incidentally | Task 6 edits that manifest's comments |

---

## 9. Docs, verbatim

### 9.1 `bakeoff/taskset/HARVESTING.md`, **The image** — three edits

**(a) amend the existing bullet in place.** It currently reads:

> - **No VCS-derived version**, unless `build:` supplies a pretend-version. Same cause: …

Append to that sentence:

```markdown
  — and note that a pretend-version fixes the BUILD and nothing else: the file
  `setuptools_scm` then generates still does not reach the run. That is exactly the
  escape hatch `pytest-10210` took (`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST=9.0.0
  pip install -e .`) on its way to a task whose agent-side `python -m pytest` is broken
  from turn one. See the next bullet before using it.
```

**(b) new bullet, immediately after it:**

```markdown
- **Nothing an `image.build` step writes into `/repo` reaches the run.** The image's `/repo` is
  a scaffold (`images.py`); at run time the materialized run tree is bind-mounted over it in
  full, and `materialize` has never seen anything the build wrote. Measured 2026-09-02 across
  nine gated images: **two** generate files this way, both `setuptools_scm` plus an egg-info —
  `sqlglot-6927` (`sqlglot/_version.py` and five `sqlglot.egg-info/*`) and `pytest-10210`
  (`src/_pytest/_version.py` and six `src/pytest.egg-info/*`) — and the other seven generate
  nothing. `click`'s `flit_core` backend writes into site-packages only, which is why the worked
  example has never surfaced this and why a `setuptools` repo will.

  Neither shape is a build failure, and what the missing file costs is a property of the
  repository:

  - `sqlglot` imports the generated module inside `try/except ImportError`, so the task is
    admissible and its suite is green — but every `import sqlglot` in the run logs *"Unable to
    set `__version__`, run `pip install -e .` ... first."* and `sqlglot.__version__` does not
    exist, on every arm, on a task whose gate passes. Measured.
  - `pytest` imports it unconditionally (`src/_pytest/assertion/rewrite.py:42`), so
    `python -m pytest` — the command §3.3's loop is built on — dies in the run tree with
    `ModuleNotFoundError: No module named '_pytest._version'` at exit **1**. Regenerating the
    file inside `tests.runner` fixes preflight's five invocations, the oracle's two and the
    grader's ladder, and fixes nothing for the agent, which never runs `tests.runner`. **A repo
    whose suite cannot import without a build-generated file is out**; a `--deselect`, a narrower
    `tests.paths` or a heredoc in the runner does not make it in.

  Two things now enforce that. The gate **records** what a build wrote —
  `build_generated_paths`, `build_generated_count` and `build_generated_state` in the preflight
  evidence, with `run_matrix --preflight-only` printing a `note` line naming the files, on a warm
  gate as well as a cold one. It does not refuse on that list: "generated" and "required" are
  different claims and no path name separates them. What it **refuses** is the consequence — the
  bare-runner probe no longer accepts exit 1, which under `--co` cannot mean a failing test
  (measured 2026-09-02: a module-level import error exits 2, a syntax error 2, a `conftest.py`
  import error 4; of seven python task images only the one above answers 1).
```

**(c) correct the section's closing paragraph.** It currently ends:

> Nothing asserts this: **preflight already runs the suite inside the image**, so a mismatch that
> breaks anything surfaces there.

Replace that clause with:

```markdown
  Nothing asserts this: preflight runs the suite in the image's *environment* but against the
  **run tree** — `/repo` is bind-mounted, never the image's own copy — so a version string
  derived at image-build time and one derived in the run tree can disagree, and the generated
  file that carried it may not be in the run tree at all. That is the class the
  build-writes-into-`/repo` bullet above names, and it is why that bullet exists.
```

### 9.2 `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, appended to the comment above `build:`

```yaml
  # And nothing this step writes into /repo survives it: at run time the
  # materialized tree is bind-mounted over the image's scaffold copy, so a
  # file the build generated is simply absent from every command the agent,
  # the gate, the oracle and the grader run. Measured 2026-09-02 -- click's
  # flit_core backend writes nothing into the tree (the image's /repo and the
  # build context agree file for file, 149 each), which is why this worked
  # example never hit it and a setuptools_scm repo does. See HARVESTING.md,
  # "The image".
```

### 9.3 `CLAUDE.md`, **Invariants**, new bullet

```markdown
- **The image's `/repo` is a scaffold, and the run tree is the product.** `image.build` runs
  against `git archive base_sha` at build time; `materialize` builds the run tree separately and
  the bind mount replaces `/repo` with it in full, so a file the build wrote there reaches no
  process in any container. Measured 2026-09-02: two of nine gated task images generate files this
  way, and neither failure is loud. `sqlglot-6927` gates green and then runs every arm with
  `sqlglot.__version__` missing and an error logged on every import; `pytest-10210`'s suite cannot
  import at all in the run tree, and the bare `python -m pytest` an agent types exits **1** — a
  code the bare-runner probe accepted defensively because its own comment says "1 cannot happen
  with `--co`". It cannot happen for a *test failure*; it happens when the interpreter dies before
  pytest starts, which is this class. So `preflight` now measures the difference
  (`build_generated_paths`/`_count`/`_state`) and refuses nothing on it — "generated" and
  "required" cannot be separated from a path list — while the bare-runner probe refuses exit 1,
  which measures the agent's own command in the run tree (collection errors exit 2, `conftest.py`
  errors exit 4; of seven python task images only that one answers 1). Inverting the order was
  measured and rejected: an offline `pip install -e .` is a per-repo property (click's `flit_core`
  backend is not in the image; pytest's is), and an editable install already points at `/repo` by
  absolute path, so there was never anything to fix about the import path.
```

---

## 10. What this does NOT do

- **The path-list measurement refuses nothing.** No problem string and no NO-GO comes from
  `build_generated_paths`, however long it is. The one new refusal (D9) is keyed on the bare-runner
  exit code, not on the list.
- **It does not widen the refusal past exit 1.** Exits 0, 2 and 5 stay accepted for the reasons
  already in that branch's comment, and the two existing tests that pin 2 and 4 stay green.
- **It does not restore anything into the run tree.** Option C is rejected (§3.2); the run tree is
  still `start_sha` and nothing else, in tracked *and* untracked content.
- **It does not detect a build step that MODIFIES a file in place.** The measurement is a set
  difference over path names; a `sed -i` during `image.build` leaves the name on both sides and is
  not reported. Content hashing was considered and left out: the two measured instances are both
  additions, and hashing means reading both trees in full rather than listing them.
- **It does not detect a build that creates only an empty directory** (tar directory members are
  skipped), or one that *deletes* from the scaffold (measured: none of the nine did).
- **It does not detect writes OUTSIDE `/repo`** — those survive into the run, which is the whole
  point of the `/opt`-style workarounds, and they are not this measurement's business.
- **It does not bound the bytes the scan transfers**, only the paths the verdict lists (D3).
- **It does not put the paths in `RunRecord`.** They are a property of the task and the image, not
  of an episode; the preflight verdict beside the log is where they live.
- **It does not touch the probe task set** (`~/.cache/bakeoff-probe/taskset`). §7.7 gates
  `pytest-10210` and expects a NO-GO; dropping or re-cutting it is §11's follow-up.
- **It does not change any image.** `render_dockerfile`'s output is byte-identical, so no stored
  `container_image_digest`, oracle fingerprint or task-image cache entry moves.

---

## 11. Follow-up (one `TASKS.md` bullet, P2)

Review 1's rulings on the four questions this plan raised are adopted: the exit-1 refusal is built
**here** (rulings 1 and 2, §D9), `sqlglot-6927` is kept and recorded (ruling 3), and the cap stays
at 100 with the count promoted to its own key (ruling 4, D3/F5). What remains is one bullet, for
whoever owns the probe task set — worded so both findings land where that set's next reader will
hit them, since neither manifest is editable from this repo:

```markdown
- [ ] **Two findings against the 2026-09-02 probe task set, from round-2 item 14.**
  `pytest-10210-approx-nested-container` is **not a valid task**: its suite imports
  `src/_pytest/_version.py`, which the image build generates and the bind mount discards, so the
  bare `python -m pytest` an agent types raises `ModuleNotFoundError` from turn one; its
  `tests.runner` heredoc fixes the gate, the oracle and the grader and cannot reach the agent.
  Since item 14 the gate refuses it (bare-runner exit 1) — re-gate to confirm, then drop or
  re-cut it. Separately, `sqlglot-6927-dremio-trycast` is **kept**: its
  `try/except ImportError` around the same kind of generated module is the admissible shape, its
  suite is green and its fix is unrelated to versioning — but every arm runs with
  `sqlglot.__version__` missing and `"Unable to set __version__"` logged on each import, which is
  what `evidence.build_generated_paths` now records. Do not re-discover either as a model failure.
  Also expect `chimera-228-equinox-numeric` to NO-GO on re-gate for an unrelated, pre-existing
  reason (bare `--co` exits 4, a usage error; its stored verdict predates the probe).
```

---

## Review 1 → changes

18 findings, 3 blocking. **All 18 addressed, none disputed.** Every measurement review 1 re-ran
reproduced, and the three it added (the `.git` export, the `--co` exit-code spread, the stored
verdict versions) were re-measured independently here before being acted on.

| # | finding | change |
|---|---|---|
| 1 | **BLOCKING** — `.git` handling inconsistent; the named test cannot pass on correct code and the anchor is dead | **Adopted in full.** D2 now filters `.git` on the **tree side only**, with the justification upgraded from "walk cost" to the real one: the clone's metadata is not repository content and must not cancel build residue by name. Task 1's `_repo_paths_in_image` loses its `.git` filter (and gains a comment saying why); `_tree_paths` prunes `.git` directories *and* a submodule's `.git` gitfile. Test 4 is renamed `test_git_residue_in_the_image_is_reported_and_the_trees_own_git_is_not` and now asserts `[".git/config"]` unmutated → `[]` mutated. Anchor 2 is renamed and is live. Re-measured here: `docker cp` does export a `.git` from an image that has one. |
| 2 | **BLOCKING** — the stated reason for not fixing the exit-1 branch is measurably false | **Adopted; the refusal is built here.** New D9, Task 2's third bullet, §1.7's two measured tables, two new tests, a third mutation anchor, and §7.7 promoted to mandatory. Re-measured independently: seven python images give 0 ×5, 4 (chimera), 1 (pytest-10210); injected module-level import error → 2, syntax error → 2, conftest error → 4. Also confirmed the branch's own comment already says *"1 cannot happen with `--co` (kept in the accepted set anyway ...)"*, which is now quoted as the defensive acceptance this item measured through. §10's false sentence is deleted. |
| 3 | **BLOCKING** — §1.4/§9.3/§11.1 state a gate result never produced | **Adopted.** §1.4 now separates the measured `docker run` (exit 1, stderr's last line) from the **inference** about the probe's accepted tuple, and cites the stored verdict versions (pytest 11, sqlglot 12, click 11; the probe landed at 13) as the reason no such gate run exists. §9.3's `CLAUDE.md` text is reworded to the code fact plus the measured exit code. §7.7 is mandatory and its output is to be quoted in `tasks/todo.md`. |
| 4 | MEDIUM — D4's "the bind mount is about to hide it" names a constraint that does not exist | **Adopted.** D4 and the Task 2 comment now give the two real reasons (the early return records `not_attempted` honestly; a refused manifest should not pay a docker call) and state explicitly that the scan's own container has no mount and the placement is a cost/shape choice, not a correctness one. |
| 5 | MEDIUM — the count survives only inside prose | **Adopted.** Third key `build_generated_count: int \| None` (`None` on `not_attempted`/`failed`, the true total otherwise), with the precedent named. D3's table, the seeds, the scan block, `_print_build_generated`, three tests and §7.5–§7.6's expected values all carry it; the truncation test now asserts an integer. |
| 6 | MEDIUM — the note is silent on every warm gate | **Adopted (the stronger option).** D7 and Task 3 print from **both** sites, with `_stored_evidence` reading `cache/preflight/<task_id>.json` best-effort on the cached branch, plus `test_a_cached_pass_still_prints_what_the_build_wrote` and a cached-run step in §7.5/§7.6. |
| 7 | MEDIUM — the trap is inside the bullet above the new one | **Adopted.** §9.1 is now three edits; (a) amends **No VCS-derived version** in place, naming the pretend-version escape hatch as the one `pytest-10210` took. |
| 8 | MEDIUM — the section closes by asserting the opposite | **Adopted.** §9.1(c) rewrites "preflight already runs the suite inside the image" to say it runs against the run tree in the image's environment, and points at the new bullet as the class that sentence waved away. |
| 9 | MEDIUM — "refuses nothing" over-generalised; (a) take, (b) do not build | **Adopted as ruled.** §2 now says the separation is impossible *from the path list* and lists the refusal as decision item 4; (a) is D9; (b) is recorded in new §3.4 as considered-and-rejected and **not filed**, per the ruling's "if at all". |
| 10 | LOW — §7.1's count is ambiguous | **Adopted.** "+16 passed, +2 deselected", with the per-file breakdown and Task 0 recording the baseline first. |
| 11 | LOW — `test_the_report_is_sorted` is nearly vacuous | **Adopted.** Asserts the explicit `["a.py", "b.py", "c/d.py"]`. |
| 12 | LOW — the early-return test asserts something its fixture cannot see | **Adopted.** That test now installs its own **counting** stub and asserts `calls == []`. |
| 13 | LOW — the integration docstring carries half the rationale | **Adopted.** It carries both the marker rationale and the `--basetemp`/`/var/folders` silently-empty-`/repo` note, with the reason it applies here. |
| 14 | LOW — Task 3's tests need `materialize` faked | **Adopted, and better than named line numbers**: all three use the existing `_stub_resolve_tasks(monkeypatch, rm, on_preflight)` helper, which already stubs `build_task_image`, `image_entrypoint`, `materialize`, `write_json` and `preflight`. |
| 15 | LOW — stderr is read after the stream is drained | **Adopted as the note it is.** The `finally` block in Task 1 now carries the failure mode, the measurement that it does not occur (nine images, ≤15 MB, `wait()` 0 every time), and why `stdout.close()` first turns a hang into an EPIPE. |
| 16 | LOW — the cap bounds the verdict, not the transfer | **Adopted.** D3 says so explicitly, with the yaml-474 worst case and the `npm install`-inside-`/repo` hypothetical; §10 repeats it as a non-goal. |
| 17 | LOW — click line reference not marked re-locate | **Adopted.** Task 6 names the comment by its closing sentence instead of `:202–205`. |
| 18 | LOW — §11.3's flag has nowhere to land | **Adopted.** §11 is now a single `TASKS.md` bullet carrying the pytest-10210 verdict, the sqlglot keep-and-record note **and** the chimera pre-existing NO-GO warning, addressed to the probe set's owner. |

**Rulings adopted as given.** (1) pytest-10210 is not a valid task — the task-set half is §11's
bullet, and the harness half is D9, so §7.7's expected outcome is a NO-GO rather than "not this
commit's business". (2) The bare-runner separation is fixed here, by exit code alone, with no
stderr heuristic. (3) `sqlglot-6927` is kept and recorded, with the note filed where its next
reader will hit it. (4) The cap stays 100, subject to findings 5 and 16.

**Noted, not changed:** review 1's observation that the runner-gate early return leans on the seed
rather than writing its own reason string, where `bare_runner_skipped` writes one. That asymmetry is
now stated as deliberate in D3, with the reason: the `bare_runner_*` triple has two distinct skip
reasons to tell apart and this scan has one.

### Review 2

**APPROVED**, 18/18 of review 1 addressed and independently re-measured, with four open LOW
findings (19–22) — all mechanical, none blocking. All four folded in place.

| # | finding | change |
|---|---|---|
| 19 | Task 6's re-locate anchor for the click manifest is off by one word — the file says *"...catches **it**."*, the plan quoted *"...catches **that**."*, which greps to nothing | **Fixed.** Verified verbatim at `click-3360-write-usage-empty-args/task.yaml:204`: *"-- preflight's green-after check is what catches it."* Task 6 now anchors on the `build: ["pip install -e ."]` line **and** the correct closing phrase, and says in as many words that the anchor is a `grep -n` before it is an edit — the failure mode replacing `:202–205` with a name was supposed to remove, reintroduced by a mis-transcribed quote. |
| 20 | D9's message renders as `Last line of stderr: .` when stderr is empty | **Fixed, with a test.** Task 2's code block gains a `clue` binding — `f"Last line of stderr: {last_line}"` when there is one, otherwise *"stderr was empty -- the diagnostic went to stdout or nowhere, so the exit code and `evidence.bare_runner_exit` are the whole of it"* — and the `problems.append` f-string interpolates `clue`. D9's prose states it and notes the shape is reachable rather than hypothetical: `chimera-228`'s bare `--co` exits 4 with an **empty** stderr (measured), and nothing makes a 1 different. New `test_the_refusal_says_so_when_stderr_is_empty` pins it (asserts `stderr was empty` present and `Last line of stderr: .` absent), because a new message branch with no test is what the code review finds. |
| 21 | `_preflight_result` has no `evidence` parameter, so finding 14's "use the existing helper" is incomplete | **Fixed.** §6's `test_run_matrix.py` section now instructs extending `_preflight_result(task, kw, problems=())` with an `evidence=None` parameter passed through as `evidence=evidence or {}`, with the reason: three inline `PreflightResult(...)` constructions would be three copies of a field list that item 5 moves. Same transcribes-never-invents rule finding 14 was decided on. |
| 22 | §7.7 is a commit prerequisite that depends on an unversioned local cache directory | **Fixed.** §7.7 now states that `~/.cache/bakeoff-probe/taskset` is the 2026-09-02 probe's output, lives outside this repository and is not versioned with it; that the **portable** proof of the refusal is `test_a_bare_pytest_that_cannot_start_is_refused` plus the third mutation anchor, both offline on any checkout; and that §7.7 is this machine's end-to-end confirmation. Explicitly: a missing directory is not a failed prerequisite, but skipping the step while the directory is present is not allowed. |

**Count arithmetic moved with finding 20**, and this is the only number in the plan that review 2
verified and this revision changes: §7.1 goes from **+16 passed, +2 deselected** (18 new `def`s;
6 / 7 / 3 / 2) to **+17 passed, +2 deselected** (19 new `def`s; 6 / **8** / 3 / 2), the extra
`def` being `test_the_refusal_says_so_when_stderr_is_empty` in `test_preflight.py`. §7.1 also now
notes that `_preflight_result` grows a parameter (finding 21) without changing any count. Nothing
else in §7 moves: the three mutation anchors, the offline gate, the integration selection and the
five real-gate steps are unchanged.

**Recorded from review 2's re-measurements, not acted on** (it is a strengthening of D9's premise,
already true in the plan): under `--co` a genuinely **failing test exits 0**, because collect-only
never runs it. So a red start state is not merely "answers 2 or 4" — it is invisible to this probe,
which is the premise of D9 in its strongest form. D9's existing statement (1 is unreachable for a
test failure) is correct as written and is left as it is.
