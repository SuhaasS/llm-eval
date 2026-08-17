# Harvesting a task

What a candidate has to satisfy before it belongs in this directory, and why
each rule is here. The bar is deliberately high in one direction: a task that
is refused costs a message, and a task that is accepted wrongly costs a matrix
— every arm scored on an unverifiable guess, in an append-only log.

Three layers. **Only the first is enforced by code.** The other two fail
silently, which is what makes them the expensive ones.

---

## Layer 1 — refused by code

Nothing here needs judgment. Run the gate and read the output.

```bash
cd bakeoff && .venv/bin/python scripts/run_matrix.py --preflight-only --tasks <task_id>
```

Offline, no credentials, no spend. Needs a Docker daemon, and network the first
time a repo is mirrored.

### The loader — `src/bakeoff/tasks.py`

Runs before anything builds.

| rule | the failure it prevents |
|---|---|
| `base_sha` is 40 hex, and the repo actually contains it | `git checkout --detach` onto a SHA that does not resolve leaves the tree wherever it was; every later diff is against a state nobody chose, and the record reads as an ordinary quiet run |
| `start_sha`, when declared, equals the recomputed one | a re-cut patch or an edited manifest silently moving what every arm was asked to do |
| `prompt` is non-empty | — |
| `tests.paths` is non-empty | it is what splits `reference.diff` into the oracle half and the fix half |
| `tests.f2p` is non-empty, has no duplicates, and is disjoint from `p2p` | a task with no discriminating set; a test required to start red and never go red, which no submission can satisfy |
| `reference.diff` begins with `diff --git`, with nothing before it | a reference that is not exactly a diff is a reference nobody can reproduce |
| both halves are non-empty after the split | no oracle for the agent to run, or no reference fix to validate against |
| no rename crosses the test/solution boundary | the file would be simultaneously the agent's oracle and part of its submission, and guessing puts one inside the other |
| `task_id` is unique in the set | `run_id` is `sha256(task|model|sample|attempt)`, so two tasks sharing an id collide in the event log — discovered at the far end of a matrix, after the tokens are spent |

### Preflight — `src/bakeoff/preflight.py`

Runs inside the pinned image, before the proxy starts.

- `tests.runner` contains `pytest`. The red/green distinction is built on
  pytest's exit codes and nothing else can currently supply it.
- The image is non-root, `claude --version` matches the base pin, `git` and
  `rg` are present.
- The container's HEAD is `start_sha`.
- **No `CLAUDE.md`, `AGENTS.md`, `.claude` or `.cursorrules` in the start
  state.** §5.2 pins the session config precisely because agent files
  substantially change behaviour; a task-local one gives this task a context
  the others do not have.
- f2p exits **1** at the start state — not `0` (already solved), not `2`/`4`/`5`
  (broken environment) — and every declared f2p id appears in pytest's
  FAILED/ERROR lines. Both halves matter: `returncode != 0` accepts a broken
  environment as evidence the bug is present, which is the Phase 0c failure.
- p2p exits 0 at the start state. A regression check against an already-red
  suite cannot mean anything.
- `git status` is clean after the suite runs. §5.6 stages everything, so
  anything the suite drops lands in every submission diff and diff size then
  measures the interpreter rather than the agent. Remedy is `gitignore_extra`
  in the manifest, which is applied in the setup commit and therefore visible
  in `start_sha`.
- The solution half applies cleanly to the start state.
- f2p exits 0 after the reference fix, and p2p still exits 0. If the reference
  cannot pass, no submission can.

---

## Layer 2 — required, and nothing checks it

This is where a task set goes wrong quietly. Every item below produces a task
that passes Layer 1 and measures the wrong thing.

### The oracle

- **Tests assert behaviour, not internal names.** This is the single most
  important judgment call per candidate, and no gate catches it. `click` #3678
  was rejected for exactly this: its tests pin an arbitrary storage name, so a
  different-but-correct fix would be scored as a failure. Prefer tests that
  compare rendered output, return values, or raised messages.
- **No property-based oracle.** A hypothesis-driven suite can pass a wrong fix
  on a lucky draw and fail a right one on an unlucky seed. `attrs` and `cattrs`
  are out for this reason.
- **The suite is deterministic.** Preflight runs p2p twice; a flake makes the
  gate a coin flip and the eval unreproducible.
- **An explicit `tests.p2p` lists LEAF node ids only** — `path::test_name`,
  never a bare module or a class. The oracle's swallow-refusal
  (`oracle.derive_quarantine`) compares declared ids against quarantined ids,
  which is exact only while every declared entry names one item. A declared
  non-leaf selects many items that leaf ids can never be a superset of, so the
  refusal **fails open** there: a selection deselected down to nothing gets
  past it and the graded run exits 5. That surfaces as a named
  `scope_collected_nothing` record rather than as silence, which is why the
  shape is a requirement here instead of a check in code — the set is authored
  here, and the predicate cannot be made exact from ids alone.

### Grading

- **Every task declares a `grading:` block, or records why each key is
  waived.** `build`, `typecheck` and `lint` are checks 3, 7 and 8 of the
  grader's ladder. An absent key grades as `not_configured`, which is a named
  absence and not a pass — but an *unrecorded* absence is indistinguishable
  from an oversight, and a §10.3 reader has no way to tell a task that
  deliberately has no lint gate from one whose author forgot. A comment in
  `task.yaml` naming each waived key and its reason is the whole requirement;
  see `click-3360-write-usage-empty-args/task.yaml`.
- **A declared command is pinned in the image.** A checker installed by
  version range is a moving oracle: the same submission grades differently on
  two passes and the grade cannot say which tool answered. Pin it in
  `image.pip`/`image.apt` in the same edit that adds the key, or waive it.

### Provenance

- **`base_sha` is `merge_commit^1`** — the real parent of the merge, not the
  PR's recorded `base.sha`. The base branch advances between a PR being opened
  and merged, and diffing against the stale one puts unrelated commits into the
  reference. Measured on `click` #3434: `7c99ebe` → `63274a7`.
- **The reference is the real merged PR, verbatim** (§3.2), without exception.
  It is what converts the judge's question from open-ended "is this diff good?"
  (κ ≈ 0.32, below the 0.6 acceptability bar) into reference-anchored "does this
  accomplish what the reference accomplished?"
- **The licence permits redistributing the diff and the prompt.**

### The prompt

- **Verbatim** (§3.3): the issue title, then the issue body exactly as filed,
  plus the ticket description where one exists. Nothing added. A
  harness-authored sentence — "the tests are in `tests/`", "do not edit the
  tests" — makes it a different task from the one a human actually opened, and
  this eval exists to measure the workflow that happens rather than a tidied
  one.
- **Human corrections are not replayed** (§3.3). A follow-up like "fix the
  import on line 40" was conditioned on the original model's output; against a
  candidate that wrote different code it is incoherent. Those turns become
  `rubric_items` instead — a free, human-authored list of what "done" required.

### The image

Three ways a task image fails at build time, silently, all found by screening
rather than by reasoning:

- **No git submodules.** The build context is `git archive base_sha`, which
  drops them. `tomlkit` cannot collect its suite for this reason: it needs
  `tests/toml-test`, and the directory arrives empty.
- **No VCS-derived version**, unless `build:` supplies a pretend-version. Same
  cause: `git archive` leaves no `.git`, and `setuptools_scm` refuses with
  *"unable to detect version"*. Measured across 12 candidates, only
  `setuptools_scm` is strict about this — `hatch-vcs` builds fine without it.
- **The suite is fast enough.** Preflight runs it four times, each under a 600 s
  timeout, and the agent re-runs it inside `wall_clock_timeout_s`. ~40 s is the
  practical ceiling; `click`'s 1.4 s is what comfortable looks like.

One consequence of the run tree being pruned to `base_sha`'s history, since it
shows up in exactly the repos the second bullet is about: **tags that are
ancestors of `base_sha` are kept**, so `git describe` still resolves and a
`setuptools_scm`/`hatch-vcs` repo can still derive a version. But the
abbreviated SHA shortens — `8.3.3-69-g63274a79` becomes `8.3.3-69-g63274a7`,
measured — because `core.abbrev` auto-sizes to the smaller object count. A
package installed at image-build time and one the agent rebuilds in the run tree
therefore disagree on version string. Nothing asserts this: preflight already
runs the suite inside the image, so a mismatch that breaks anything surfaces
there.

---

## Layer 3 — properties of the set, not of any task

- **~80 harvested to land ~60** (§3.5), stratified to the *measured* production
  distribution rather than a guessed split — files touched per session, turn
  count, output tokens, tool-call mix, duration. A 50/50 split on a workload
  that is 70% small edits measures the wrong thing and will not transfer to the
  bill. Include a **long-session stratum**: all three candidates claim 256K
  context and degrade unevenly well before it, and small tasks never surface
  that.
- **Calibration pilot before freeze: all four models, N=3, across every
  candidate.** Selecting tasks by one model's difficulty curve tilts the set
  toward what that model happens to discriminate on, which is precisely the bias
  this eval exists to avoid.
- **The drop rule is model-neutral.** Drop only if **all four** score 0% (floor
  — usually underspecified rather than hard) or **all four** score 100%
  (ceiling — no signal). **Keep every disagreement, however lopsided.** A task
  only Sonnet solves is among the most informative in the set.
- **`task_version` bumps on any edit.** It is not part of `run_id`, so an edited
  task leaves every finished cell looking complete while mixing two different
  tasks under one `task_id`. Resume hard-refuses on the mismatch, per cell.

**Read the drop rule against the measured variance.** On 2026-08-13 this task
set's one task went 4/4 resolved and then 2/4 on the same four arms, hours
apart, at temperature 1.0 and N=1 — between-run variance larger than the
between-arm spread. At N=3 a discriminating task can present as a floor. That is
an argument for more repeats in the pilot, not for a looser drop rule.

---

## Screened repositories

Measured 2026-08-13 in `python:3.12-slim-bookworm`: editable install, `git clean
-xfd`, full suite, then `git status`. Suite times are at HEAD, **not** at any
candidate's `base_sha` — green here does not guarantee green there, and
preflight remains the gate. The PR column counts merged pull requests linked to
an issue and created after 2024-01-01; it is an availability proxy only, since
each candidate still needs the Layer 2 read.

### Clean — pass every mechanical gate

| repo | suite | secs | PRs |
|---|---|---|---|
| pypa/packaging | 62,430 passed | 20 | 46 |
| python-jsonschema/jsonschema | 8,280 passed, 232 skipped | 6 | 4 |
| arrow-py/arrow | 1,904 passed | 13 | 8 |
| marshmallow-code/marshmallow | 1,188 passed | 2 | 28 |
| pallets/werkzeug | 1,003 passed | 9 | 36 |
| pallets/jinja | 911 passed | 1 | 9 |
| more-itertools/more-itertools | 736 passed | 9 | 69 |
| PyCQA/isort | 631 passed, 2 skipped | 35 | 43 |
| psf/requests | 619 passed, 15 skipped | 78 | 15 |
| pallets/flask | 494 passed | 1 | 23 |
| mahmoud/boltons | 468 passed | 3 | 9 |
| pallets/itsdangerous | 297 passed | 0 | 3 |

`pallets/click` is already in the set and is the worked example.

### Usable once a dependency is declared

| repo | what it needs | result | PRs |
|---|---|---|---|
| tobymao/sqlglot | `pip: [duckdb, pytz, pandas, python-dateutil]` and `SETUPTOOLS_SCM_PRETEND_VERSION` in `build:` | 1,217 passed, 19,201 subtests, 41.5 s | **795** |
| pygments/pygments | `pip: ["wcag_contrast_ratio"]` | 1 collection error without it | 64 |
| Textualize/rich | `pip: ["attrs"]` | 1 collection error without it | 40 |
| python-humanize/humanize | 6 import errors, undiagnosed | — | 21 |

**sqlglot carries a date constraint.** `CLAUDE.md` was added 2026-02-02
(`a65c8701a306`, PR #6899); `AGENTS.md` followed on 2026-03-19
(`7f0dd47f60a3`), and a later commit made `CLAUDE.md` a symlink to it. Both are
present at HEAD and preflight's `test -e` follows symlinks, so **`base_sha` must
predate 2026-02-02**. That leaves 680 clean candidates (301 of them from 2025),
against 116 after the cutoff — the constraint costs almost nothing, and sqlglot
remains the richest source by an order of magnitude. A SQL transpiler is also
close to the ideal bug shape: input SQL, exact expected output, tests that
assert rendered strings rather than internal names.

### Excluded

| repo | why |
|---|---|
| python-poetry/tomlkit | suite needs the `tests/toml-test` submodule; `git archive` drops it |
| python-attrs/attrs, python-attrs/cattrs | hypothesis property-based suites; the editable install also collides with a site-packages `attr` |
| un33k/python-slugify | no harvestable PRs |

---

## Cutting one

1. Find a merged PR that closes an issue and carries both a test and a fix.
   Read the tests first — Layer 2's behaviour-not-internals rule is what
   disqualifies most candidates, and it is cheapest to check before anything
   else.
2. `base_sha` is `git rev-parse <merge_commit>^1`.
3. Cut the reference with the flags pinned, and store it verbatim:

   ```bash
   git -c diff.noprefix=false -c diff.renames=true \
       diff --binary <base_sha> <merge_commit> > reference.diff
   ```

   **The flags are not cosmetic — without them the diff's SHAPE is a property of
   the harvester's `~/.gitconfig` rather than of the task**, and 80 references cut
   on different machines would not be the same kind of object.

   - `diff.noprefix=false` — a `--no-prefix` diff is refused at load, because
     `git apply`'s `-p1` would strip a *real* path component and silently swap
     which files are the oracle.
   - `diff.renames=true` — `copies` turns plain modifications into copy chunks.
   - `--binary` — a plain `git diff` renders a binary change as `Binary files …
     differ` with no payload. That loads clean and then fails in *preflight*
     ("cannot apply binary patch without full index line").

   Leave `core.quotepath` at its default. Quoted headers are never parsed for
   paths, and turning it off writes raw non-UTF-8 bytes into `reference.diff`,
   which fails to decode at load.
4. Write `task.yaml` per §3.7. Copy the structure from
   `click-3360-write-usage-empty-args/task.yaml`, whose comments explain what
   each key has to satisfy.
5. Leave `repo.start_sha` out on the first pass. Run the gate, then pin the
   value it reports.
6. Run the f2p tests by hand at the start state to get the node ids right —
   they are measured, never guessed.
7. `--preflight-only` until it prints PASS. Every problem it reports maps to a
   defect that would otherwise read as model capability.
