# Broadening 1 — `strip_paths` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manifest key `strip_paths` that removes declared paths from the start state inside the same fixed-identity setup commit that already applies the test half and `gitignore_extra`, so a repository carrying an agent file or a committed vendored tree at `base_sha` stops being a date floor on the whole corpus.

**Architecture:** One key, one delete, four places it has to be visible. `tasks.py` validates it at load (including the two ways it could remove the wrong thing, and the ambiguous overlap with the reference diff), `materialize` runs `git rm -r` before the test half and folds the deletions into the existing setup commit so `start_sha` stays a pure function of the manifest, `images.py` strips the same paths out of the unpacked build context so the scaffold matches the start state, and `preflight.py` checks the paths are actually gone from the tree it is gating rather than trusting the code that removed them.

**Tech Stack:** Python 3.12, pytest, git plumbing (`git rm`, `git ls-files`), Docker (integration tests only).

**Spec:** `docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md` (§3.7 the task record, §5.1 the pinned world, §5.2 the pinned session config, §5.6 the submission diff, §6.4 confounds). The shared context for this series of broadenings is `.superpowers/broaden/CONTEXT.md`; the rules a candidate task must satisfy are `bakeoff/taskset/HARVESTING.md`.

## Global Constraints

- Repo: `/Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden`, branch `broaden-taskset`. Do not touch any other checkout.
- Run everything from `bakeoff/` with its venv: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`. Baseline at the start of this work: **1129 passed, 46 deselected**.
- **Integration tests are deselected by default** (`pyproject.toml`: `addopts = "-m 'not integration'"`). Anything pinned *only* by an integration test is not pinned by the suite an implementer or CI actually runs, so every guarantee below has a default-suite test as well. Run the integration leg explicitly when a change touches container, image or preflight behaviour: `cd bakeoff && .venv/bin/python -m pytest -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`. The `--basetemp` under `$HOME` is mandatory on macOS — the Docker VM mounts `$HOME` but not `/var/folders`, and a repo bind-mounted from there appears inside the container as a silently empty directory.
- **Conventions from `CLAUDE.md` apply verbatim.** Module and function docstrings carry the *why* and the failure mode, cross-referenced to spec sections. A comment that says what a line does rather than what breaks without it does not fit this codebase. Claims about external behaviour are annotated with what they were verified against.
- **Do NOT edit `CLAUDE.md` in this branch.** An uncommitted edit to it exists on `main` and the merge would conflict. Invariant prose goes in module docstrings and in `HARVESTING.md`; the sentences that belong in `CLAUDE.md` are listed in this plan's final section so they can be applied later.
- **`PREFLIGHT_VERSION` moves for any change to what preflight asserts.** It is in the preflight cache key, so without the bump every warm cache serves a verdict written by the old gate.
- **`SCHEMA_VERSION` does not move in this broadening.** Nothing new is written into a `RunRecord`: `matrix.to_task_spec` carries `task_id`, `task_version`, `base_sha` (which is the *start* sha and therefore already moves when a strip is declared) and `task_set_commit`. There is no absent field for a reader to mistake for a negative claim. Do not bump it.
- **Backwards compatibility is a hard requirement.** `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` declares no `strip_paths`; its `start_sha: 33575cc0b75608fa5cbcb1d3ae3347b81eac437f` must be byte-identical at the end of this work. `git diff` on that file must show no change to the `start_sha:` line.
- **Defensive attribute access, one convention.** `getattr(task, "strip_paths", ())` is used **only** in `preflight.py`, whose `task` parameter is untyped, is duck-typed by its own tests, and already does exactly this for `grading` in `_declared_grading`. Everywhere else — `tasks.py`, `images.py` — the object is a real `TaskManifest` and the attribute is read directly, because a missing field there is a bug that must be loud.
- Stage files explicitly (`git add <paths>`), never `git add -A`/`-a`. End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Do not run `bakeoff/scripts/mutation_check.py` concurrently with anything else; it edits sources in place. **This plan adds no `MUTATIONS` entry, and that is only defensible because every guarantee below — including the two one-line CALL SITES — is pinned by a default-suite test that exercises the caller, not just the helper.** Task 4 pins `build_task_image`'s call to `_strip_build_context` by building an image with the archive and docker calls monkeypatched; Task 5 pins `preflight`'s use of the strip predicate through the existing `_ScriptedContainer`. If either of those tests is dropped during implementation, a `MUTATIONS` entry anchored on the deleted call site must be added in its place.
- YAGNI: build only what this broadening needs. No hooks for broadenings 2–7.

---

## Design decisions, and why

Read this section before Task 1. Every task below implements one of these; a reviewer rejecting a task is rejecting one of these decisions.

### 1. Key name and shape: top-level `strip_paths: [...]`, beside `gitignore_extra`

`repo:` holds `url`, `base_sha` and `start_sha` — three statements about upstream identity, two of which can be checked against GitHub. `strip_paths` is not a statement about upstream; it is a modification the harness makes, exactly like `gitignore_extra`, applied in the same commit, with the same effect on `start_sha`. Putting it under `repo:` would make that block a mixture of "what upstream is" and "what we did to it", which is the one thing that block is for not being. Top level, beside `gitignore_extra`, is where a reader already looks for "what the setup commit does".

Entries are **paths, files or directories alike**, matched with the same `PurePosixPath` component semantics `_under` already uses everywhere else (so `.claude` covers `.claude/settings.json` and does not cover `.claudeignore`). `_validate_prefixes` fits and is reused: it refuses empty, padded, absolute, and `..`-bearing entries with a message naming the key.

Three refusals `_validate_prefixes` does not make, added in `_validate_strip_paths` because this key's effect is a *delete*:

- `.` and `./`. Measured: `PurePosixPath(".").parts` is `()` and `PurePosixPath("a/b").is_relative_to(PurePosixPath("."))` is `True`, so `.` passes every existing check and names the entire tree. `git rm -r -- .` would empty the start state and `start_sha` would still be a pure function of the manifest — the failure would be perfectly reproducible and completely wrong.
- A first component of `.git`. The delete runs in the run tree, so this would destroy the repository the submission diff is taken against.
- Pathspec magic — a leading `:` or any of `* ? [ ]`. `git rm` takes pathspecs, not paths. `strip_paths: ["*.log"]` would glob, and the existence check in decision 3 would then pass on one accidental match while the author meant something else: a manifest key whose meaning is a property of the tree it is applied to, inside the one field that has to be a pure function of the manifest.

### 2. Where and how the strip happens: `git rm -r` in `materialize`, before the test half, inside the existing setup commit

`git rm -r -q -- <paths>` removes from the index *and* the worktree in one call and stages the deletions, so they are picked up by the `if staged:` commit that already exists. `--cached` is wrong: the point is that the agent must not read a stripped `CLAUDE.md` and `pip install -e .` must not resolve against a stripped vendored tree, and both need the file gone from disk, not only from the index.

It must be the same commit, not a second one, because `start_sha` is pinned in the manifest and verified on every materialization; two commits would still be deterministic but would make `start_sha` describe a two-step history for some tasks and a one-step history for others, and the pin's whole job is that one manifest names one tree. A test asserts `git rev-list --count <base>..<start> == 1` with a strip declared.

Declaring `strip_paths` therefore **moves `start_sha`** (a test pins this), and an absent or empty `strip_paths` performs no git call at all, so the click task's pinned `start_sha` does not move (a test pins `absent == empty`, and the unchanged `task.yaml` line is checked in Task 3's verification step).

### 3. A declared path that is not in the tree: a `TaskError` from `materialize`

Not a load error, because the loader never sees the tree — it does not clone, and making it clone to validate a manifest would move the network dependency out of `ensure_mirror`. Not a preflight problem either, for two reasons: preflight verdicts are **cached**, and the code that would silently do nothing is `materialize`, so that is where the check belongs. `materialize` raises `TaskError`, which `run_matrix` and `grade.py` already catch per task, and it runs before any container starts.

The failure being prevented is the specific one the task brief names: a typo (`.cluade/`) strips nothing, the file stays in the start state, and nothing downstream says so — preflight's `_CONTEXT_FILES` check knows four names, so a mistyped vendored tree or a fifth agent-file spelling passes every gate and the confound is permanent in an append-only log. `--ignore-unmatch` is deliberately absent for the same reason.

The existence check reads `git ls-files -z -- <path>`'s **output**, not its exit code. Measured 2026-09-01 with the system git: `git ls-files -z -- nope` exits **0** with empty stdout — the silent zero `container._checked_exec` exists to refuse. It asks about *tracked* content, because only tracked content is in the tree `start_sha` names. (`git rm` itself also refuses an unmatched pathspec with exit 128, measured; the explicit check is kept because it names the manifest key and reports every missing entry at once, and the tests assert on that message rather than on git's.)

**A consequence that has to be written down for task authors:** a strip only applies to a path *tracked at `base_sha`*. `allow_extra_paths` legitimately names a file the PR **creates** (a new changelog fragment), and that file does not exist at `base_sha` — combining the two on such a path makes `materialize` raise. HARVESTING.md and `BUILDING-A-TASK-SET.md` say so in Task 6.

### 4. A stripped path the reference diff touches: refused loudly; `allow_extra_paths` is the way to say "neither half"

Strip does **not** imply exclusion. If it did, a strip that accidentally covered a source file the PR fixes would silently shrink the fix half, and `solution_diff` would stop being the merged PR verbatim (§3.2). Preflight would catch that only when the missing hunk happens to be one the f2p tests need — so the surviving case is a reference that is no longer a reference, with every gate green. Silently dropping a chunk from the *test* half is worse: the oracle shrinks and every arm is graded against less than the task says it is graded against.

So: after the split, any `test_files` or `solution_files` entry lying under a `strip_paths` prefix is a `TaskError` naming the file and the half. `extra_files` under a strip prefix is **legal and is the sanctioned combination** — it is exactly what HARVESTING.md already tells an author to reach for, and it leaves the file named in `extra_files`, so the combination stays visible instead of being inferred from two keys that never mention each other. Renames are covered for free: `split_reference_diff` puts both endpoints into the file lists.

The check lives in `load_task` immediately after the split, not inside `split_reference_diff`. That function is a three-class partition of a diff by path and takes no manifest; adding a fourth prefix argument would invite the reading that strip is a fourth class of the partition, which is the reading this decision rejects.

No overlap rule is added between `strip_paths` and `tests.paths`. Stripping a directory the suite needs but the diff does not touch (test helpers, fixtures) turns the suite red, which preflight reports loudly on the very next step. A load-time rule there would be guessing at what a suite needs.

### 5. Ordering: strip → test half → `gitignore_extra` → one commit

A stripped path the test half re-creates cannot occur: decision 4 refuses any test-half file under a strip prefix at load, so the overlap does not exist by the time `materialize` runs.

The one path a later step can legitimately re-create under a strip prefix is `.gitignore`, via `gitignore_extra`. Strip-first makes that deterministic and readable: the upstream `.gitignore` is removed, and the manifest's lines land in a fresh one. Reversed, the strip would delete the file the manifest just asked for. A test pins the order by asserting the resulting `.gitignore` contains the manifest's line and *not* upstream's.

### 6. How it is recorded

- `manifest_digest` is `hashlib.sha256(raw_manifest + raw_reference).hexdigest()[:16]` — the raw manifest bytes, truncated to 16 hex characters. **No change needed**: declaring or editing `strip_paths` already invalidates the task's preflight cache entry. Say so in the code comment rather than adding a term.
- `TaskManifest.strip_paths: tuple[str, ...] = ()` — exposed, because `materialize`, `build_task_image` and preflight all read it, and a field that existed only inside opaque manifest bytes would be configuration nobody can report.
- **Preflight evidence lists them and asserts they are absent at HEAD.** Yes, and it is the cheap direct check this codebase's pruned-mirror work already argued for: verify the invariant against the artifact, not against the code or the digest that produced it. `evidence["stripped_paths"]` records what was declared (the same shape as `scope_prefixes`); `evidence["stripped_paths_present"]` records what was still there, and a non-empty value is a problem. This is a change to what preflight asserts, so **`PREFLIGHT_VERSION` moves `"2"` → `"3"`**.
- **The strip probe is `-e` OR `-L`, and `_CONTEXT_FILES` stays plain `-e`.** Measured 2026-09-01: for a symlink whose target does not exist, `[ -e dangling ]` exits **1** and `[ -L dangling ]` exits **0**. So a strip that removed a link's target and not the link itself — the exact sqlglot shape, where `CLAUDE.md` is a symlink to `AGENTS.md` — would leave a path the agent's `ls` still shows while an `-e`-only assertion called it absent. The strip check's claim is "this path is gone from the tree", so it must probe both. `_CONTEXT_FILES` keeps `-e` alone, because there the claim is "this file gives the agent context", and a dangling link gives none.

### 7. The image build context: strip it too

`build_task_image` unpacks `git archive base_sha` into `context/repo`. That is `base_sha`, not the start state, so a stripped path is still in the scaffold.

For agent files this is harmless — the bind mount replaces `/repo` at run time, and the image is never the thing the agent reads. The minimal correct answer would be "do nothing".

It is not the minimal correct answer, because of the other half of what this key is for. A committed venv or vendored tree left in the scaffold is on the import path when `image.build` runs `pip install -e .`, so the environment the image pins is resolved against a directory the run tree does not have, and nothing downstream compares the two. That is the same class of defect as a non-editable install: the image and the run disagree, silently, and preflight's green-after check only catches it when the disagreement happens to break the suite.

So the strip is applied to the unpacked context, in four lines, before the Dockerfile is written. It removes the named paths' **content**; a now-empty parent directory can remain (stripping `vendor/dep.py` leaves an empty `vendor/`, while git tracks no directories so the run tree has none). That residue is deliberate and harmless — an empty directory carries no module and no dependency — and the docstring says so rather than claiming a byte-equal tree.

A path that matches nothing in the context is **not** an error here: `materialize` raises on exactly that, and `run_matrix` builds the image *before* materializing (`image = build_task_image(...)` precedes `start_sha = materialize(...)` in `resolve_tasks`), so raising in both places means the author fixes one typo twice. `is_symlink()` is tested before `is_dir()`, because `is_dir()` follows the link and `shutil.rmtree` then raises `Cannot call rmtree on a symbolic link` — and upstream sqlglot's `CLAUDE.md` is a symlink to `AGENTS.md`, recorded in HARVESTING.md, so this is a real input rather than a hypothetical.

---

## File Structure

| File | Change |
|---|---|
| `bakeoff/src/bakeoff/tasks.py` | `TaskManifest.strip_paths`; `_validate_strip_paths`; `_refuse_stripped_halves`; `_strip_paths_from_tree`; `load_task` and `materialize` wiring |
| `bakeoff/src/bakeoff/images.py` | `_strip_build_context`; one call in `build_task_image` |
| `bakeoff/src/bakeoff/preflight.py` | `_present`; `_existing_prefixes` delegates to it; the strip assertion; `PREFLIGHT_VERSION` → `"3"` |
| `bakeoff/src/bakeoff/grader.py` | one stale prose reference to `PREFLIGHT_VERSION` 2 |
| `bakeoff/tests/test_tasks.py` | `upstream` fixture gains four files; load/validation/split/materialize tests |
| `bakeoff/tests/test_images.py` | `_strip_build_context` unit tests **and** a `build_task_image` call-site test |
| `bakeoff/tests/test_preflight.py` | `_FakeTask.strip_paths`, `_ScriptedContainer(dangling=…)`, `_smoke_task(extra_files=…, extra_yaml=…)`; three default-suite tests; two integration tests |
| `bakeoff/taskset/HARVESTING.md` | Layer 1 tables, Layer 2 "The start state", the two date-floor passages, "Cutting one" |
| `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml` | commented-out example key |
| `docs/BUILDING-A-TASK-SET.md` | §2 screening table, §3.5 manifest keys, §3.7 refusal table |
| `tasks/todo.md` | review section |

---

### Task 1: The manifest key — load, validate, expose

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` — add `_PATHSPEC_MAGIC` and `_validate_strip_paths` immediately after `def _validate_prefixes(...)`; add `strip_paths` to `TaskManifest` after the `gitignore_extra: tuple[str, ...] = ()` field; read and validate in `load_task`.
- Test: `bakeoff/tests/test_tasks.py` — validation section, after `test_a_misspelled_grading_key_is_refused_rather_than_dropped`.

**Interfaces:**
- Consumes: `_validate_prefixes(prefixes, where)`, `_strs(value, where)`, `TaskError`, `PurePosixPath` — all already in `tasks.py`.
- Produces: `TaskManifest.strip_paths: tuple[str, ...]` (default `()`), read by Tasks 3, 4 and 5. `_validate_strip_paths(paths: tuple[str, ...], where: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `bakeoff/tests/test_tasks.py`, in the validation section:

```python
def test_strip_paths_loads_and_is_exposed(tmp_path, upstream):
    """A manifest key nothing can read is configuration nobody can report.
    `materialize`, `build_task_image` and preflight all need this list."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml='strip_paths: ["CLAUDE.md", ".claude"]',
    )

    task = load_task(task_dir)

    assert task.strip_paths == ("CLAUDE.md", ".claude")


def test_a_manifest_with_no_strip_paths_still_loads(tmp_path, upstream):
    """Every manifest written before this key existed keeps loading, and the
    absent case is one value rather than a None every caller re-decides."""
    assert load_task(_write_task(tmp_path / "set", upstream)).strip_paths == ()


@pytest.mark.parametrize(
    "bad",
    ["", " CLAUDE.md", "/etc/passwd", "../outside", ".", "./", ".git",
     ".git/hooks", "*.log", "docs/*", ":(glob)**/x"],
)
def test_a_strip_path_that_would_remove_the_wrong_thing_is_refused(
    tmp_path, upstream, bad
):
    """This key's effect is a DELETE, so the validation is stricter than
    `_validate_prefixes` alone.

    `.` and `./` pass every check that function makes -- measured,
    `PurePosixPath(".").parts` is `()` and `is_relative_to(".")` is True for
    every path -- and name the whole tree, so the start state would be emptied
    and `start_sha` would still be a pure function of the manifest. `.git`
    would take the repository the submission diff is computed against. Glob
    and pathspec magic would make what gets removed a property of the tree
    rather than of the manifest, and the existence check in `materialize`
    would then pass on one accidental match."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml=f"strip_paths: [{bad!r}]",
    )

    with pytest.raises(TaskError, match="strip_paths"):
        load_task(task_dir)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k strip_path`
Expected: FAIL — `AttributeError: 'TaskManifest' object has no attribute 'strip_paths'` on the first two, and `DID NOT RAISE` on the parametrized cases.

- [ ] **Step 3: Add the field and the validation**

In `bakeoff/src/bakeoff/tasks.py`, add to `TaskManifest` immediately after the `gitignore_extra: tuple[str, ...] = ()` field:

```python
    #: Paths removed from the start state in the same setup commit that
    #: applies the test half. What lifts a repository's agent-file or
    #: vendored-tree date floor without hand-rewriting `base_sha`: the
    #: modification is in the manifest, so it is in `manifest_digest` and in
    #: `start_sha`, rather than in a rewritten history a reader diffing
    #: against upstream would not find there.
    strip_paths: tuple[str, ...] = ()
```

Add immediately after `_validate_prefixes`:

```python
#: Pathspec magic `git rm` would honour and this key does not accept. See
#: `_validate_strip_paths`.
_PATHSPEC_MAGIC = ("*", "?", "[", "]")


def _validate_strip_paths(paths: tuple[str, ...], where: str) -> None:
    """Refuse a strip entry that would remove something nobody asked for.

    `_validate_prefixes` covers empty, padded, absolute and `..`-bearing
    entries. The three refusals here are specific to a key whose effect is a
    DELETE rather than a classification.

    `.` and `./` pass every one of those checks and name the whole tree:
    measured, `PurePosixPath(".").parts` is `()` and
    `PurePosixPath("a/b").is_relative_to(PurePosixPath("."))` is True. The
    start state would be emptied and `start_sha` would still be a pure
    function of the manifest -- perfectly reproducible and completely wrong.

    `.git` is the same shape one level worse: the delete runs in the run tree,
    so it would destroy the repository the section 5.6 submission diff is
    taken against.

    Pathspec magic is refused because `git rm` takes PATHSPECS, not paths.
    `strip_paths: ["*.log"]` would glob, and `_strip_paths_from_tree`'s
    existence check would then pass on one accidental match while the author
    meant something else -- a manifest key whose meaning is a property of the
    tree it is applied to, inside the one field that has to be a pure function
    of the manifest.
    """
    _validate_prefixes(paths, where)
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts:
            raise TaskError(
                f"{where}: {path!r} names the whole tree; strip_paths removes "
                "what it names, so this would empty the start state"
            )
        if parts[0] == ".git":
            raise TaskError(
                f"{where}: {path!r} is inside the repository's own .git; the "
                "strip runs in the run tree and would destroy the repository "
                "the submission diff is taken against"
            )
        if path.startswith(":") or any(ch in path for ch in _PATHSPEC_MAGIC):
            raise TaskError(
                f"{where}: {path!r} carries pathspec magic; this key names "
                "paths, and a pattern would make what is removed a property "
                "of the tree rather than of the manifest"
            )
```

In `load_task`, immediately after the `extra_paths = _strs(` block and before the call to `split_reference_diff`:

```python
    # Validated before the split so a malformed entry is reported without
    # first paying for git's per-chunk parse of the whole reference.
    # `manifest_digest` needs no term for this key: it hashes the raw manifest
    # bytes, so declaring or editing a strip already invalidates the task's
    # preflight cache entry.
    strip_paths = _strs(data.get("strip_paths"), f"{where}:strip_paths")
    _validate_strip_paths(strip_paths, f"{where}:strip_paths")
```

And in the `TaskManifest(...)` constructor call, immediately after the `gitignore_extra=_strs(...)` argument:

```python
        strip_paths=strip_paths,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q`
Expected: PASS, no regressions in the file.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py
git commit -m "$(cat <<'EOF'
feat: strip_paths names what the start state must not carry

A repository with CLAUDE.md, .claude/ or a committed venv at base_sha is a
date floor on every candidate PR cut from it -- sqlglot loses everything after
2026-02-02, the internal repo everything before 2026-07-02. HARVESTING.md
already sanctions a manual strip; this is the key that records one.

The validation is stricter than `_validate_prefixes` because the effect is a
delete, not a classification. `.` passes every existing check and names the
whole tree (`PurePosixPath(".").parts` is `()`); `.git` would destroy the
repository the submission diff is taken against; and `git rm` reads pathspecs,
so an unrefused `*` would make what is removed a property of the tree rather
than of the manifest.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: A stripped path the reference diff touches is refused

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` — add `_refuse_stripped_halves` after `split_reference_diff`; call it in `load_task` after the `if not solution_diff.strip():` block.
- Test: `bakeoff/tests/test_tasks.py` — the `upstream` fixture gains four files; new tests in the split/validation section.

**Interfaces:**
- Consumes: `_under(path, prefixes)`, `TaskManifest.strip_paths` (Task 1), the `test_files`/`solution_files`/`extra_files` triple returned by `split_reference_diff`.
- Produces: `_refuse_stripped_halves(test_files, solution_files, strip_paths, where) -> None`. The extended `upstream` fixture and `CHANGELOG_CHUNK`, both used by Task 3.

- [ ] **Step 1: Extend the fixture and write the failing tests**

In `bakeoff/tests/test_tasks.py`, extend the `upstream` fixture. Add these files to the base commit, immediately before the first `_sh("git", "add", "-A", cwd=repo)`, and mention them in the fixture docstring:

```python
    # A repository whose base_sha carries an agent file, a vendored tree and a
    # changelog is the shape `strip_paths` exists for, and all three were
    # measured on real repositories (sqlglot's CLAUDE.md, the internal repo's
    # committed venv, click's CHANGES.rst). None of them is touched by the fix
    # commit, so the reference diff below is unchanged and every other test in
    # this module sees exactly the halves it saw before.
    (repo / "CLAUDE.md").write_text("# project notes\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}\n")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "dep.py").write_text("VERSION = '1.0'\n")
    (repo / "CHANGES.md").write_text("changelog\n")
```

Add near `BUGGY`/`FIXED` at the top of the file:

```python
# A chunk for a file the fix commit does not touch, appended to a reference so
# a test can exercise the three-class split. `git apply --numstat` PARSES a
# chunk rather than applying it -- the same property the `_chunks_of` tests
# further down already rely on.
CHANGELOG_CHUNK = (
    "diff --git a/CHANGES.md b/CHANGES.md\n"
    "--- a/CHANGES.md\n"
    "+++ b/CHANGES.md\n"
    "@@ -1 +1,2 @@\n"
    " changelog\n"
    "+- fixed add()\n"
)
```

Then the tests:

```python
def test_a_stripped_path_in_the_solution_half_is_refused(tmp_path, upstream):
    """Strip does NOT imply exclusion, and this is why.

    Dropping the chunk silently would make `solution_diff` something other
    than the merged PR (section 3.2's verbatim reference), and preflight would
    only notice when the missing hunk happened to be one the f2p tests need.
    The surviving case is a reference that is no longer a reference, with
    every gate green."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: ["calc.py"]',
    )

    with pytest.raises(TaskError, match=r"strip_paths.*calc\.py.*solution"):
        load_task(task_dir)


def test_a_stripped_path_in_the_test_half_is_refused(tmp_path, upstream):
    """The worse direction: the oracle shrinks, and every arm is then graded
    against less than the task says it is graded against."""
    task_dir = _write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: ["tests"]',
    )

    with pytest.raises(TaskError, match=r"strip_paths.*test_calc\.py.*test"):
        load_task(task_dir)


def test_a_stripped_path_excluded_from_both_halves_loads(tmp_path, upstream):
    """The sanctioned combination, and the one HARVESTING.md already sends an
    author to: `allow_extra_paths` puts the file in neither half, so nothing
    tries to apply a patch onto a path the strip removed -- and `extra_files`
    still names it, so the combination is visible rather than inferred from
    two keys that never mention each other.

    Load-only here on purpose: the refusal this task adds is a load-time one,
    and the strip that makes the combination true end to end lands in Task 3.
    `test_a_stripped_extra_path_is_gone_from_the_start_state` there is the
    other half."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            '  allow_extra_paths: ["CHANGES.md"]\n'
            'strip_paths: ["CHANGES.md"]'
        ),
    )
    (task_dir / "reference.diff").write_text(
        upstream["reference"] + CHANGELOG_CHUNK
    )

    task = load_task(task_dir)

    assert task.extra_files == ("CHANGES.md",)
    assert "CHANGES.md" not in task.solution_diff
    assert "CHANGES.md" not in task.test_diff
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k stripped`
Expected: the two refusal tests FAIL with `DID NOT RAISE`. The third asserts only what the split already does and passes once the fixture change is in — it is here so the legal combination is pinned beside the two refusals, not left implied by their absence.

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q`
Expected: every pre-existing test in the file still passes with the extended fixture. If one does not, the fixture change is wrong — fix it before going further.

- [ ] **Step 3: Add the refusal**

In `bakeoff/src/bakeoff/tasks.py`, immediately after `split_reference_diff`:

```python
def _refuse_stripped_halves(
    test_files: tuple[str, ...],
    solution_files: tuple[str, ...],
    strip_paths: tuple[str, ...],
    where: str,
) -> None:
    """A path the strip removes and the reference diff changes is refused.

    STRIP DOES NOT IMPLY EXCLUSION, deliberately. Silently dropping the chunk
    would make `solution_diff` something other than the merged PR that section
    3.2 requires verbatim, and preflight would only notice when the missing
    hunk happened to be one the f2p tests need -- so the case that survives
    every gate is a reference that is no longer a reference. From the TEST
    half it is worse: the oracle shrinks and every arm is graded against less
    than the manifest says.

    `allow_extra_paths` is the declared way to say "neither half", it is what
    taskset/HARVESTING.md already tells an author to reach for here, and it
    leaves the file named in `extra_files` -- so the combination stays visible
    instead of being inferred from two keys that never mention each other.

    Called from `load_task` rather than from `split_reference_diff`: that
    function is a three-class partition of a diff by path and takes no
    manifest, and a fourth prefix argument would invite exactly the reading
    this refusal rejects. Renames need no special case, because
    `split_reference_diff` puts both endpoints into the file lists.
    """
    for half, files in (("test", test_files), ("solution", solution_files)):
        caught = sorted(path for path in files if _under(path, strip_paths))
        if caught:
            raise TaskError(
                f"{where}: strip_paths removes {', '.join(caught)}, which the "
                f"reference diff's {half} half also changes -- the patch would "
                "be applied onto a path that no longer exists. List those "
                "paths in tests.allow_extra_paths to keep them out of both "
                "halves, or narrow the strip."
            )
```

In `load_task`, immediately after the `if not solution_diff.strip():` block:

```python
    _refuse_stripped_halves(test_files, solution_files, strip_paths, where)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q`
Expected: PASS. This task ends green — nothing here depends on Task 3.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py
git commit -m "$(cat <<'EOF'
fix: a stripped path the reference touches would apply a patch onto nothing

The tempting shape is for a strip to imply exclusion from both halves. It
cannot: dropping a chunk because its path is stripped makes solution_diff
something other than the merged PR, and preflight only catches that when the
missing hunk happens to be one the f2p tests need -- so the case that survives
every gate is a reference that is no longer a reference. From the test half
the oracle shrinks instead, and every arm is graded against less than the
manifest says.

allow_extra_paths already means "neither half" and already leaves the file
named in extra_files. Requiring it keeps the combination visible.

The upstream test fixture gains an agent file, a vendored tree and a changelog
so the strip tests have something real to remove. None is touched by the fix
commit, so the reference diff and every existing test are unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The strip lands in the setup commit

**Files:**
- Modify: `bakeoff/src/bakeoff/tasks.py` — add `_strip_paths_from_tree` immediately before `def materialize(`; replace the `staged = False` line inside `materialize`; extend the module docstring's third bullet and `materialize`'s docstring.
- Test: `bakeoff/tests/test_tasks.py` — materialization section.

**Interfaces:**
- Consumes: `_git(*args, cwd=…)`, `TaskManifest.strip_paths` (Task 1), the extended `upstream` fixture and `CHANGELOG_CHUNK` (Task 2).
- Produces: `_strip_paths_from_tree(repo: Path, strip_paths: tuple[str, ...], task_id: str) -> bool` — True when anything was staged.

- [ ] **Step 1: Write the failing tests**

Append to the materialization section of `bakeoff/tests/test_tasks.py`:

```python
def test_strip_paths_removes_the_path_in_the_setup_commit(tmp_path, upstream):
    """One commit onto base, not two.

    `start_sha` is pinned in the manifest and verified on every
    materialization; its job is that one manifest names one tree. A second
    commit would still be deterministic but would make `start_sha` describe a
    two-step history for some tasks and a one-step history for others."""
    task = load_task(_write_task(
        tmp_path / "set", upstream,
        extra_yaml='strip_paths: ["CLAUDE.md", ".claude", "vendor"]',
    ))
    repo = tmp_path / "run" / "repo"

    start = materialize(task, repo, tmp_path / "cache")

    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".claude").exists()
    assert not (repo / "vendor").exists()
    tracked = _sh("git", "ls-tree", "-r", "--name-only", start, cwd=repo)
    assert "CLAUDE.md" not in tracked
    assert "vendor/dep.py" not in tracked
    assert "calc.py" in tracked, "the strip must remove only what it names"
    assert not _sh("git", "status", "--porcelain", cwd=repo), \
        "committed, not left dirty"
    assert _sh("git", "rev-list", "--count", f"{upstream['base']}..{start}",
               cwd=repo) == "1"


def test_declaring_strip_paths_moves_the_start_state(tmp_path, upstream):
    """The modification is in the manifest, so it has to be in the sha every
    record names. A strip invisible to `start_sha` would be a change to what
    every arm was asked to do that no stored record could distinguish."""
    plain = load_task(_write_task(tmp_path / "a", upstream))
    stripped = load_task(_write_task(
        tmp_path / "b", upstream, extra_yaml='strip_paths: ["CLAUDE.md"]',
    ))

    assert materialize(plain, tmp_path / "ra" / "repo", tmp_path / "cache") \
        != materialize(stripped, tmp_path / "rb" / "repo", tmp_path / "cache")


def test_an_empty_strip_paths_does_not_move_the_start_state(tmp_path, upstream):
    """Backwards compatibility, stated as a property. Every manifest written
    before this key existed -- the click task among them, whose `start_sha` is
    pinned in its task.yaml -- must materialize to exactly what it did
    before, so an absent key and an empty list have to be the same state and
    neither may run a git call."""
    absent = load_task(_write_task(tmp_path / "a", upstream))
    empty = load_task(_write_task(
        tmp_path / "b", upstream, extra_yaml="strip_paths: []",
    ))

    assert materialize(absent, tmp_path / "ra" / "repo", tmp_path / "cache") \
        == materialize(empty, tmp_path / "rb" / "repo", tmp_path / "cache")


def test_a_declared_strip_path_that_is_not_in_the_tree_is_refused(
    tmp_path, upstream
):
    """The failure this key must not introduce. A typo strips nothing, the
    file stays in the start state, and nothing downstream says so: preflight's
    context-file check knows four names, so a mistyped vendored tree or a
    fifth agent-file spelling passes every gate and the confound is permanent
    in an append-only log. `--ignore-unmatch` is deliberately absent."""
    task = load_task(_write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: [".cluade"]',
    ))

    with pytest.raises(TaskError, match=r"strip_paths.*\.cluade"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_an_untracked_path_is_not_something_a_strip_can_remove(
    tmp_path, upstream
):
    """The existence check asks git about TRACKED content, because only
    tracked content is in the tree `start_sha` names. It also reads
    `ls-files`'s OUTPUT rather than its exit code: measured,
    `git ls-files -z -- nope` exits 0 with empty stdout, which is the silent
    zero `container._checked_exec` exists to refuse.

    The same rule is why `strip_paths` cannot name a file the PR CREATES,
    even when `allow_extra_paths` also names it."""
    (upstream["path"] / "scratch.txt").write_text("untracked\n")
    task = load_task(_write_task(
        tmp_path / "set", upstream, extra_yaml='strip_paths: ["scratch.txt"]',
    ))

    with pytest.raises(TaskError, match=r"strip_paths.*scratch\.txt"):
        materialize(task, tmp_path / "run" / "repo", tmp_path / "cache")


def test_a_stripped_extra_path_is_gone_from_the_start_state(tmp_path, upstream):
    """The other half of Task 2's `..._loads`: the exclusion and the strip are
    enforced in two different places, and only a materialization shows they
    agree -- `allow_extra_paths` keeps the file out of both halves, the strip
    removes it, and nothing tries to apply a patch onto a path that is gone.

    It also demonstrates the constraint task authors have to know about: the
    strip needs the path to be TRACKED at base_sha, so this combination works
    for a changelog the PR edits and not for one it creates."""
    task_dir = _write_task(
        tmp_path / "set", upstream,
        extra_yaml=(
            '  allow_extra_paths: ["CHANGES.md"]\n'
            'strip_paths: ["CHANGES.md"]'
        ),
    )
    (task_dir / "reference.diff").write_text(
        upstream["reference"] + CHANGELOG_CHUNK
    )
    task = load_task(task_dir)
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    assert not (repo / "CHANGES.md").exists()


def test_the_strip_runs_before_the_gitignore_is_written(tmp_path, upstream):
    """Order: strip, then the test half, then `gitignore_extra`, then one
    commit. A stripped path the TEST half re-creates cannot occur -- the
    loader refuses that overlap -- so `.gitignore` is the only path a later
    step can legitimately re-create under a strip prefix, and strip-first is
    what makes that deterministic. Reversed, the strip would delete the file
    the manifest just asked for."""
    task = load_task(_write_task(
        tmp_path / "set", upstream,
        extra_yaml='strip_paths: [".gitignore"]\ngitignore_extra: ["*.log"]',
    ))
    repo = tmp_path / "run" / "repo"

    materialize(task, repo, tmp_path / "cache")

    text = (repo / ".gitignore").read_text()
    assert "*.log" in text
    assert "__pycache__/" not in text, "upstream's .gitignore was stripped"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q -k "strip or gitignore"`
Expected: FAIL — the removal tests find the files still present; the refusal tests report `DID NOT RAISE`; `test_declaring_strip_paths_moves_the_start_state` fails on equality.

- [ ] **Step 3: Implement the strip**

In `bakeoff/src/bakeoff/tasks.py`, add immediately before `def materialize(`:

```python
def _strip_paths_from_tree(
    repo: Path, strip_paths: tuple[str, ...], task_id: str
) -> bool:
    """Remove the declared paths from index AND worktree. True if anything went.

    `git rm -r`, not `git rm -r --cached`: the point of the key is that the
    agent does not read a stripped CLAUDE.md and `pip install -e .` does not
    resolve against a stripped vendored tree, and both need the file gone from
    disk rather than only from the index. The deletions are staged, so the
    caller's existing setup commit carries them and `start_sha` moves.

    A path that matches nothing is a TaskError, and `--ignore-unmatch` is
    deliberately absent. A typo (`.cluade/`) strips nothing and leaves the
    file in the start state -- and nothing downstream says so, because
    preflight's `_CONTEXT_FILES` check knows four names, so a mistyped
    vendored tree or a fifth agent-file spelling passes every gate and the
    confound is permanent in an append-only log. That is the failure this key
    exists to prevent, so it cannot also be the failure the key introduces.

    The existence check reads `git ls-files`'s OUTPUT, not its exit code:
    measured 2026-09-01, `git ls-files -z -- nope` exits 0 with an empty
    stdout, which is the silent zero `container._checked_exec` exists to
    refuse. It asks about TRACKED content, because only tracked content is in
    the tree `start_sha` names -- which is also why a strip cannot name a file
    the reference diff CREATES, even one `allow_extra_paths` legitimately
    lists. (`git rm` also refuses an unmatched pathspec, exit 128; this check
    is kept because it names the manifest key and reports every missing entry
    at once instead of the first.)
    """
    if not strip_paths:
        return False
    missing = [
        path for path in strip_paths
        if not _git("ls-files", "-z", "--", path, cwd=repo).stdout
    ]
    if missing:
        raise TaskError(
            f"{task_id}: strip_paths names {', '.join(missing)}, which no "
            "tracked file is at or under in the start state. A typo strips "
            "nothing and silently leaves behind the file the task was cut to "
            "remove."
        )
    _git("rm", "-r", "-q", "--", *strip_paths, cwd=repo)
    return True
```

In `materialize`, replace the `staged = False` line with:

```python
    # FIRST, before the test half and before `gitignore_extra`. The loader
    # refuses a strip that covers either half of the reference, so nothing the
    # test patch writes can land under a stripped prefix; `.gitignore` is the
    # only path a later step can legitimately re-create there, and running the
    # strip first is what makes that deterministic rather than an order the
    # reader has to infer.
    staged = _strip_paths_from_tree(dest, task.strip_paths, task.task_id)
```

Append a paragraph to `materialize`'s docstring:

```
    `strip_paths` is applied first and lands in the same setup commit, so the
    commit's SHA stays a pure function of (base_sha, strip, test half,
    gitignore_extra) and `start_sha` remains pinnable. A strip that did not
    move `start_sha` would be a change to what every arm was asked to do that
    no stored record could distinguish.
```

Update the module docstring's third bullet — the sentence reading `its SHA is a pure function of (base_sha, test half, gitignore_extra)` — to name the current tuple: `(base_sha, strip_paths, test half, gitignore_extra)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_tasks.py -q`
Expected: PASS.

- [ ] **Step 5: Verify the click task's pinned start_sha did not move**

Run: `cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden && git diff -- bakeoff/taskset/`
Expected: **empty output.** The click manifest declares no `strip_paths`, so no git call runs and its `start_sha: 33575cc0b75608fa5cbcb1d3ae3347b81eac437f` is untouched.

- [ ] **Step 6: Run the whole unit suite**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS, at least the 1129 baseline plus the new tests.

- [ ] **Step 7: Commit**

```bash
git add bakeoff/src/bakeoff/tasks.py bakeoff/tests/test_tasks.py
git commit -m "$(cat <<'EOF'
feat: the strip lands in the setup commit, so start_sha still names the task

`git rm -r` (not --cached: the agent must not read a stripped CLAUDE.md and
pip install -e . must not resolve against a stripped vendored tree), staged
into the commit that already applies the test half and gitignore_extra. So
declaring a strip moves start_sha, and an absent or empty key runs no git call
at all -- the click task's pinned sha is unchanged.

A path matching nothing raises rather than being ignored. --ignore-unmatch
would make a typo strip nothing and leave the file in the start state, and
preflight's context-file check knows four names, so a mistyped vendored tree
would pass every gate into an append-only log. The check reads git ls-files's
OUTPUT: measured, `git ls-files -z -- nope` exits 0 with empty stdout.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: The image build context loses the same paths

**Files:**
- Modify: `bakeoff/src/bakeoff/images.py` — add `_strip_build_context` after `render_dockerfile`; call it in `build_task_image` between the `tar -x` error check and the `(context / "Dockerfile").write_text(` call.
- Test: `bakeoff/tests/test_images.py`.

**Interfaces:**
- Consumes: `TaskManifest.strip_paths` (Task 1), read directly (see the defensive-access convention in Global Constraints).
- Produces: `_strip_build_context(repo_dir: Path, strip_paths: list[str]) -> None`.

- [ ] **Step 1: Write the failing tests**

`bakeoff/tests/test_images.py` imports only `render_dockerfile` today. Replace that import and add the modules the new tests need:

```python
import io
import subprocess
import tarfile

from bakeoff.images import _strip_build_context, build_task_image, render_dockerfile
```

Then append:

```python
def test_the_build_context_loses_the_stripped_paths(tmp_path):
    """The context is `git archive base_sha`, so it carries paths the START
    state does not -- and the scaffold is what `image.build` resolves against.
    A committed venv or vendored tree left here puts `pip install -e .` on an
    import path the run tree does not have, so the environment the image pins
    is not the environment the agent works in and nothing downstream compares
    the two."""
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("{}\n")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "dep.py").write_text("VERSION = '1.0'\n")
    (repo / "CLAUDE.md").write_text("# notes\n")
    (repo / "calc.py").write_text("x = 1\n")

    _strip_build_context(repo, ["CLAUDE.md", ".claude", "vendor"])

    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".claude").exists()
    assert not (repo / "vendor").exists()
    assert (repo / "calc.py").exists(), "only what was named"


def test_a_stripped_symlink_is_removed_rather_than_followed(tmp_path):
    """`is_dir()` follows the link and `shutil.rmtree` then raises
    "Cannot call rmtree on a symbolic link". Measured upstream: sqlglot's
    CLAUDE.md is a symlink to AGENTS.md."""
    repo = tmp_path / "repo"
    (repo / "real").mkdir(parents=True)
    (repo / "real" / "a.md").write_text("x\n")
    (repo / "CLAUDE.md").symlink_to(repo / "real")

    _strip_build_context(repo, ["CLAUDE.md"])

    assert not (repo / "CLAUDE.md").is_symlink()
    assert (repo / "real" / "a.md").exists(), "the link's target is not the target"


def test_a_path_missing_from_the_build_context_is_not_an_error(tmp_path):
    """`materialize` raises on exactly this, and `run_matrix` builds the image
    BEFORE materializing -- raising in both places means one typo is reported
    twice and fixed twice."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _strip_build_context(repo, ["nope", "also/nope"])


def _archive_bytes(source) -> bytes:
    """A tar of `source`, in the shape `git archive --format=tar` emits."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(source.rglob("*")):
            # `recursive=False`: `rglob` already yields every descendant, and
            # tarfile.add recurses by default -- so the default adds each
            # directory's contents a second time.
            tar.add(path, arcname=str(path.relative_to(source)),
                    recursive=False)
    return buffer.getvalue()


def test_build_task_image_strips_the_context_it_unpacks(tmp_path, monkeypatch):
    """The CALL SITE, not the helper.

    Deleting the one `_strip_build_context(...)` line in `build_task_image`
    leaves all three helper tests above green, and an un-stripped scaffold is
    invisible in every record -- the exact shape a mutation anchor exists for.
    Pinned here instead, and in the DEFAULT suite: `addopts = "-m 'not
    integration'"` means an integration-only pin proves nothing on the run an
    implementer or CI actually makes.

    Only `git archive` and the docker calls are faked; the real `tar -x` runs,
    so the unpack this asserts against is the one production performs. The
    `subprocess.run` patch lands on the shared `subprocess` MODULE rather than
    on a name `images.py` owns, so for the duration it is process-wide --
    `real_run`, captured before the patch, is what keeps every other caller
    honest, and monkeypatch restores the attribute on the way out."""
    source = tmp_path / "source"
    (source / ".claude").mkdir(parents=True)
    (source / ".claude" / "settings.json").write_text("{}\n")
    (source / "CLAUDE.md").write_text("# notes\n")
    (source / "calc.py").write_text("x = 1\n")
    archive = _archive_bytes(source)
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if list(args)[:2] == ["git", "archive"]:
            return subprocess.CompletedProcess(args, 0, stdout=archive, stderr=b"")
        return real_run(args, **kwargs)

    # `build_task_image` imports ensure_mirror from bakeoff.tasks INSIDE the
    # function, so the patch has to land on the source module.
    monkeypatch.setattr("bakeoff.tasks.ensure_mirror",
                        lambda url, sha, cache: tmp_path / "mirror")
    monkeypatch.setattr("bakeoff.images.subprocess.run", fake_run)
    monkeypatch.setattr("bakeoff.images._run", lambda *a, **k: "sha256:fake")

    class _Image:
        apt = ()
        pip = ()
        build = ()

    class _Task:
        task_id = "t"
        task_version = 1
        repo_url = "file:///nowhere"
        base_sha = "0" * 40
        image = _Image()
        strip_paths = ("CLAUDE.md", ".claude")

    build_task_image(_Task(), "sha256:base", tmp_path / "build",
                     tmp_path / "cache")

    unpacked = tmp_path / "build" / "image-t" / "repo"
    assert (unpacked / "calc.py").exists(), "the archive was unpacked at all"
    assert not (unpacked / "CLAUDE.md").exists()
    assert not (unpacked / ".claude").exists()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q`
Expected: FAIL — `ImportError: cannot import name '_strip_build_context'`.

- [ ] **Step 3: Implement it**

In `bakeoff/src/bakeoff/images.py`, add after `render_dockerfile`:

```python
def _strip_build_context(repo_dir: Path, strip_paths: list[str]) -> None:
    """Remove the manifest's `strip_paths` from the unpacked build context.

    The context is `git archive base_sha`, not the start state, so a stripped
    path is still here. For an agent file that is harmless -- the bind mount
    replaces /repo at run time and the image is never what the agent reads --
    but for the other half of what the key is for it is not: a committed venv
    or vendored tree left in the scaffold is on the import path when
    `image.build` runs `pip install -e .`, so the environment the image pins
    is resolved against a directory the run tree does not have, and nothing
    downstream compares the two. Same class as a non-editable install: the
    image and the run disagree silently, and preflight's green-after check
    only catches it when the disagreement happens to break the suite.

    The named paths' CONTENT goes; an emptied parent directory can remain
    (stripping `vendor/dep.py` leaves `vendor/`, while git tracks no
    directories so the run tree has none). That residue is deliberate: an
    empty directory carries no module and no dependency, and pruning parents
    would start guessing at which of them the archive was supposed to have.

    A path that matches nothing is NOT an error. `tasks._strip_paths_from_tree`
    raises on exactly that, and `run_matrix` builds the image before
    materializing, so raising here too means one typo is reported twice.

    `is_symlink()` before `is_dir()`: `is_dir()` follows the link and
    `shutil.rmtree` then raises "Cannot call rmtree on a symbolic link".
    Measured upstream -- sqlglot's CLAUDE.md is a symlink to AGENTS.md, which
    is why taskset/HARVESTING.md records that repository's date floor as
    covering both names.

    Escaping `repo_dir` is impossible by construction rather than by a check
    here: `tasks._validate_strip_paths` refuses an absolute entry, one
    carrying `..`, and one carrying pathspec magic.
    """
    import shutil

    for path in strip_paths:
        target = Path(repo_dir) / path
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
```

In `build_task_image`, immediately after the `tar -x` error check and before the Dockerfile is written:

```python
    _strip_build_context(repo_dir, list(task.strip_paths))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_images.py -q`
Expected: PASS, all four new tests included.

- [ ] **Step 5: Commit**

```bash
git add bakeoff/src/bakeoff/images.py bakeoff/tests/test_images.py
git commit -m "$(cat <<'EOF'
fix: a stripped tree left in the build context shadows the editable install

The build context is `git archive base_sha`, not the start state, so a
stripped path is still in the scaffold. For an agent file that is harmless --
the bind mount replaces /repo at run time. For the committed venv this key
also exists to remove it is not: the tree is on the import path when
image.build runs `pip install -e .`, so the environment the image pins is
resolved against a directory the run tree does not have.

The call site is pinned by a test that builds an image with git archive and
the docker calls faked, because deleting one line in build_task_image leaves
every helper test green and an un-stripped scaffold is invisible in a record.

A missing path is not an error here: materialize raises on that, and
run_matrix builds the image first, so raising twice means fixing one typo
twice. is_symlink before is_dir, because rmtree refuses a symlink and
sqlglot's CLAUDE.md is one.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Preflight checks the strip against the tree

**Files:**
- Modify: `bakeoff/src/bakeoff/preflight.py` — `PREFLIGHT_VERSION` → `"3"`; add `_present`; `_existing_prefixes` delegates to it; the `_CONTEXT_FILES` comprehension becomes a `_present` call; the strip assertion follows it.
- Modify: `bakeoff/src/bakeoff/grader.py` — one stale prose sentence.
- Test: `bakeoff/tests/test_preflight.py`.

**Interfaces:**
- Consumes: `TaskManifest.strip_paths` (Task 1), read as `getattr(task, "strip_paths", ())` — this module only, per the Global Constraints convention, matching `_declared_grading`'s `getattr(task, "grading", None)`.
- Produces: `_present(container, names, *, dangling_counts: bool = False) -> list[str]`; evidence keys `stripped_paths` and `stripped_paths_present`; `PREFLIGHT_VERSION == "3"`.

- [ ] **Step 1: Extend the test doubles and write the failing tests**

Three edits to the machinery already in `bakeoff/tests/test_preflight.py`:

**(a)** `_ScriptedContainer.__init__` gains `dangling=()` beside `present=()`, stored as `self.dangling = set(dangling)`, and its `exec` gains a branch immediately after the `["test", "-e"]` one:

```python
        if cmd[:2] == ["test", "-L"]:
            # A dangling symlink: `-e` says absent, `-L` says there is still a
            # path here. Measured 2026-09-01: `[ -e dangling ]` exits 1 and
            # `[ -L dangling ]` exits 0.
            return _Exec(exit_code=0 if cmd[2] in self.dangling else 1)
```

**(b)** `_FakeTask` gains a field:

```python
    strip_paths: tuple = ()
```

**(c)** `_smoke_task` gains two keyword parameters:

```python
def _smoke_task(tmp_path, calc_body: str, reference: str, *,
                extra_files: dict[str, str] | None = None,
                extra_yaml: str = "") -> Path:
```

Inside it, before the `git init` loop:

```python
    for name, body in (extra_files or {}).items():
        target = upstream / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
```

and the manifest it writes gains `+ (f"{extra_yaml}\n" if extra_yaml else "")` after the `f2p:` line.

Then the tests. Three of them run in the default suite:

```python
def test_a_strip_that_did_not_happen_is_a_problem(monkeypatch, tmp_path):
    """The load-bearing direction, and the reason this is a check on the TREE
    rather than on the manifest: a strip that silently did not happen leaves
    the file in every arm's context and in every submission diff, and no later
    stage re-derives it.

    In the DEFAULT suite. `addopts = "-m 'not integration'"`, so a guarantee
    pinned only by the integration tests below is not pinned by the run an
    implementer or CI actually makes."""
    task = _FakeTask(strip_paths=("vendor",))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/", "vendor"))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("strip_paths" in problem for problem in result.problems)
    assert result.evidence["stripped_paths"] == ["vendor"]
    assert result.evidence["stripped_paths_present"] == ["vendor"]


def test_a_dangling_symlink_a_strip_left_behind_is_still_present(
    monkeypatch, tmp_path
):
    """`test -e` alone would call this absent. Measured 2026-09-01: for a
    symlink whose target is gone, `[ -e x ]` exits 1 and `[ -L x ]` exits 0 --
    so stripping a link's TARGET and not the link (the sqlglot shape, where
    CLAUDE.md is a symlink to AGENTS.md) would leave a path the agent's `ls`
    still shows while the gate reported it removed."""
    task = _FakeTask(strip_paths=("CLAUDE.md",))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",),
                                   dangling=("CLAUDE.md",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["stripped_paths_present"] == ["CLAUDE.md"]
    assert any("strip_paths" in problem for problem in result.problems)


def test_a_completed_strip_is_recorded_and_is_not_a_problem(
    monkeypatch, tmp_path
):
    """Absence is recorded, never implied: the gate says it looked, and says
    what it found, rather than leaving a reader to infer both from silence."""
    task = _FakeTask(strip_paths=("vendor",))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["stripped_paths"] == ["vendor"]
    assert result.evidence["stripped_paths_present"] == []
```

And two integration tests, which put the same guarantee against a real image and a real `materialize`:

```python
@pytest.mark.integration
def test_a_task_that_strips_a_vendored_tree_preflights(tmp_path, agent_image):
    """The pass direction against the real thing: `materialize` removed the
    paths, the container agrees, and the evidence says so."""
    task = load_task(_smoke_task(
        tmp_path, "def add(a, b):\n    return a - b\n", _reference(fix_source=True),
        extra_files={"CLAUDE.md": "# notes\n", "vendor/dep.py": "V = 1\n"},
        extra_yaml='strip_paths: ["CLAUDE.md", "vendor"]',
    ))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert result.ok, result.problems
    assert result.evidence["stripped_paths"] == ["CLAUDE.md", "vendor"]
    assert result.evidence["stripped_paths_present"] == []


@pytest.mark.integration
def test_a_strip_that_did_not_happen_is_refused_by_the_real_gate(
    tmp_path, agent_image
):
    """`vendor/`, not `CLAUDE.md`, so the strip check is the only thing that
    could name it -- the context-file check would otherwise answer for the
    same path and this assertion would pass with the strip check deleted.

    TWO problems are expected, not one: the re-created file is untracked, so
    the post-suite `git status --porcelain` check fires as well. That is why
    the assertion names the strip problem specifically rather than counting
    them."""
    task = load_task(_smoke_task(
        tmp_path, "def add(a, b):\n    return a - b\n", _reference(fix_source=True),
        extra_files={"vendor/dep.py": "V = 1\n"},
        extra_yaml='strip_paths: ["vendor"]',
    ))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")
    # Put it back, untracked -- the shape a build step or an image layer
    # leaves behind, and the one the manifest claims is gone.
    (repo / "vendor").mkdir()
    (repo / "vendor" / "dep.py").write_text("V = 1\n")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert any("strip_paths" in problem for problem in result.problems)
    assert result.evidence["stripped_paths_present"] == ["vendor"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q -k strip`
Expected: FAIL — `KeyError: 'stripped_paths'` on all three default-suite tests.

- [ ] **Step 3: Implement the check**

In `bakeoff/src/bakeoff/preflight.py`, bump the version constant and extend the comment above it (which currently ends `1 is the implicit version of every verdict cached before the scoped-p2p and grading assertions landed.`):

```python
# ... 3 adds the `strip_paths` assertion: a verdict cached under 2 was written
# by a gate that never looked at that key at all.
PREFLIGHT_VERSION: str = "3"
```

Add `_present` immediately before `_existing_prefixes`, and rewrite `_existing_prefixes` to delegate to it so there is genuinely one copy of the probe:

```python
def _present(container, names, *, dangling_counts: bool = False) -> list[str]:
    """Which of `names` exist in the container's tree, in declared order.

    `test -e` FOLLOWS symlinks, which is right for a context file -- sqlglot's
    CLAUDE.md is a symlink to AGENTS.md, and a DANGLING link named `.claude`
    gives the agent no context at all.

    `dangling_counts` adds a `-L` probe, and the strip assertion needs it for
    the opposite reason: its claim is "this path is gone from the tree", and a
    link whose target was stripped is a path the agent's `ls` still shows.
    Measured 2026-09-01: for a symlink to a missing target, `[ -e x ]` exits 1
    and `[ -L x ]` exits 0.

    One helper for every caller -- the context files, the scope filter (used
    by preflight and by the grader's check 6 alike) and the strip -- because a
    second copy of this probe is where the symlink semantics drift.
    """
    probes = ["-e", "-L"] if dangling_counts else ["-e"]
    return [
        name for name in names
        if any(container.exec(["test", probe, name]).exit_code == 0
               for probe in probes)
    ]
```

`_existing_prefixes` keeps its docstring (it explains the gated-argv/graded-argv parity, which is not `_present`'s subject) and its `tuple` return; its body becomes:

```python
    return tuple(_present(container, prefixes))
```

In `preflight`, replace the `_CONTEXT_FILES` comprehension with `present = _present(container, _CONTEXT_FILES)`, and add immediately after that block:

```python
        # The strip, checked against the TREE rather than against the manifest
        # or the code that performed it. `materialize` raises when a declared
        # path matches nothing, so this cannot fire on a typo -- what it
        # catches is the artifact disagreeing with the manifest for any other
        # reason (a build step re-creating the path, a stale preflight tree, a
        # future change to the strip that stops working). A strip that did not
        # happen is invisible: the file is in every arm's context and in every
        # submission diff, and no later stage re-derives it.
        #
        # `dangling_counts=True`: stripping a symlink's target and not the link
        # leaves a path `test -e` calls absent and `ls` still shows.
        #
        # `getattr`, like `_declared_grading`'s: this function takes an
        # untyped `task` and a manifest object predating the key must not
        # crash the gate.
        # Both keys are written unconditionally: "this task strips nothing"
        # and "the gate did not look" render identically as a missing key, and
        # absence is recorded rather than implied.
        stripped = tuple(getattr(task, "strip_paths", ()))
        still_there = (
            _present(container, stripped, dangling_counts=True)
            if stripped else []
        )
        evidence["stripped_paths"] = list(stripped)
        evidence["stripped_paths_present"] = still_there
        if still_there:
            problems.append(
                f"the start state still carries {', '.join(still_there)}, "
                "which the manifest's strip_paths says it removed. Section "
                "5.2 pins the session config and section 5.6 stages "
                "everything, so an un-stripped path is both a context this "
                "task has and the others do not, and a file in every "
                "submission diff."
            )
```

- [ ] **Step 4: Fix the stale `PREFLIGHT_VERSION` prose in the grader**

`bakeoff/src/bakeoff/grader.py` carries a comment reading `With PREFLIGHT_VERSION 2 in place both routes to SCOPE_COLLECTED_NOTHING should be unreachable`. Change it to read `With PREFLIGHT_VERSION 2 or later in place ...`, so the claim does not become false the moment the constant moves. Nothing else in that comment changes.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -q`
Expected: PASS, including the three new default-suite tests and every pre-existing scope test (which now reach `_present` through `_existing_prefixes`).

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.

Run: `cd bakeoff && .venv/bin/python -m pytest tests/test_preflight.py -v -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`
Expected: PASS, including the two new integration tests.

- [ ] **Step 6: Commit**

```bash
git add bakeoff/src/bakeoff/preflight.py bakeoff/src/bakeoff/grader.py \
        bakeoff/tests/test_preflight.py
git commit -m "$(cat <<'EOF'
feat: preflight checks the strip against the tree, not against the manifest

A strip that silently did not happen leaves the file in every arm's context
and in every submission diff, and no later stage re-derives it. So the gate
asserts the declared paths are absent at HEAD -- the cheap direct check on the
artifact that the pruned-mirror work already argued for, rather than trusting
the code or the digest that produced it. Evidence records what was declared
and what was still there.

The probe is `-e` OR `-L` for the strip and plain `-e` for the context files.
Measured: for a symlink whose target is gone, `[ -e x ]` exits 1 and
`[ -L x ]` exits 0 -- so stripping a link's target and not the link (sqlglot's
CLAUDE.md is a symlink to AGENTS.md) would leave a path `ls` still shows while
an -e-only assertion called it removed. A dangling link is not a context file,
which is why the two callers want opposite answers and the predicate takes a
flag.

PREFLIGHT_VERSION 2 -> 3: it is in the cache key, and a verdict cached under 2
was written by a gate that never looked at strip_paths at all. The grader's
prose reference to version 2 becomes "2 or later".

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Docs, the worked example, and the review log

**Files:**
- Modify: `bakeoff/taskset/HARVESTING.md`
- Modify: `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`
- Modify: `docs/BUILDING-A-TASK-SET.md`
- Modify: `tasks/todo.md`

No `TASKS.md` entry: this broadening leaves no follow-up work deliberately open. The vendored-tree floor on the internal repo stays a *judgment* (2,902 files is a different repository from the one the PR was merged into), which is recorded in HARVESTING.md rather than tracked as a task.

- [ ] **Step 1: HARVESTING.md — Layer 1, the loader table**

Add three rows to the loader table, after the `no rename crosses the test/solution boundary` row:

```markdown
| `strip_paths` entries are relative, `..`-free, glob-free, and are neither `.` nor `.git` | the key removes what it names: `.` matches every path (`PurePosixPath(".").parts` is `()`), `.git` would destroy the repository the submission diff is taken against, and a glob would make what is removed a property of the tree rather than of the manifest |
| no test-half or solution-half file lies under a `strip_paths` prefix | the patch would be applied onto a path that no longer exists — and dropping the chunk instead would make `solution_diff` something other than the merged PR, or shrink the oracle, with every gate still green |
| every `strip_paths` entry matches a file TRACKED at `base_sha` (checked in `materialize`) | a typo strips nothing and leaves the file the task was cut to remove; the preflight context-file check knows four names, so a mistyped vendored tree passes every gate. It also means a strip cannot name a file the PR *creates* |
```

- [ ] **Step 2: HARVESTING.md — Layer 1, the preflight bullets**

After the `No CLAUDE.md, AGENTS.md, .claude or .cursorrules` bullet:

```markdown
- **Every `strip_paths` entry is absent from the start state.** Checked in the
  container against the tree, not against the manifest: a strip that silently
  did not happen puts the file in every arm's context and in every submission
  diff, and no later stage re-derives it. The probe is `-e` **or** `-L`, so a
  symlink whose target was stripped still counts as present.
```

- [ ] **Step 3: HARVESTING.md — Layer 2, "The start state"**

Replace the bullet beginning **"Removing an offending path from `base_sha` is legitimate"** with:

```markdown
- **Removing an offending path is legitimate, and `strip_paths` is how it is
  recorded.** List the paths in the manifest's top-level `strip_paths` and they
  are removed — from the index and the worktree — inside the same
  fixed-identity setup commit that applies the test half and `gitignore_extra`.
  So the modification is in `manifest_digest` and in `start_sha`, and a reader
  who cannot find it upstream can find it in the manifest. Nothing has to be
  hand-rewritten and `base_sha` stays the true `merge_commit^1`, which is what
  keeps the reference applying.

  A hand-rewritten `base_sha` — a one-commit strip pushed to a fork — is still
  legitimate for anything `strip_paths` cannot express, and still has to be
  recorded under `provenance` (`base_modified`). For a `strip_paths` strip the
  manifest key *is* the record; a duplicate `provenance` note is not required.

  **The path has to be TRACKED at `base_sha`.** A strip that matches no
  tracked file is refused when the start state is built, on purpose — a typo
  that strips nothing is the failure this key exists to prevent. One
  consequence to plan around: `allow_extra_paths` legitimately names a file the
  PR *creates*, and such a file cannot also be stripped, because it is not
  there to remove.

  **Where the reference diff touches a stripped path, the load is refused**
  until that path is also in `tests.allow_extra_paths`, which excludes it from
  both halves. Strip does not imply exclusion: dropping a chunk because its
  path is stripped would make `solution_diff` something other than the merged
  PR, and the case that survives every gate is a reference that is no longer
  one.

  Judge the strip by size. Six agent-file paths is bookkeeping; 2,902 venv
  files is a different repository from the one the PR was merged into, and the
  cheaper answer is a later `base_sha`.

  **The confound, and it has to be recorded per task (§6.4):** the humans who
  wrote the PR had that file. A repository whose `CLAUDE.md` shaped how its
  contributors worked is not quite the repository the models are handed once it
  is stripped, and a stripped vendored tree may be what an import in the fix
  actually resolved against. Name the stripped paths and this caveat in the
  manifest's comments, next to the key.
```

- [ ] **Step 4: HARVESTING.md — the two date-floor passages**

In the sqlglot paragraph, after the sentence ending ``so **`base_sha` must``
/ ``predate 2026-02-02**`` (it is bolded and wraps mid-phrase — match on
``predate 2026-02-02**``), add:

```markdown
`strip_paths: ["CLAUDE.md", "AGENTS.md"]` lifts that floor — both are agent
files and neither is touched by a bug-fix PR — which reopens the 116
post-cutoff candidates. List **both** names: `CLAUDE.md` is a symlink to
`AGENTS.md` there, and stripping only the target leaves a dangling link that an
agent's `ls` still shows. Preflight catches that (its strip probe is `-e` or
`-L`), so the mistake is a NO-GO rather than a silent confound — but it is
cheaper to list both than to iterate on the gate.
```

In the internal-repository table, replace the two floor rows' `effect` column:

```markdown
| 2026-03-31 | `.claude/` added (6 files) | liftable: `strip_paths: [".claude"]` — six paths is bookkeeping, and the §6.4 confound goes in the manifest |
| **2026-07-02** | `#4` untracked a committed venv | `strip_paths` can express it, but 2,902 `site-packages` files is a different repository from the one the PR was merged into. Prefer a later `base_sha` |
```

- [ ] **Step 5: HARVESTING.md — "Cutting one"**

In step 2, after *"a committed venv or vendored tree makes every submission diff a diff of that tree, and preflight will not tell you"*, add:

```markdown
   An agent file or a small vendored tree there is not disqualifying: list it
   in `strip_paths` and it is removed in the setup commit. A large one is —
   see "The start state" above for where that line falls.
```

- [ ] **Step 6: The worked-example manifest**

In `bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`, insert between the end of the `tests:` block (the `allow_extra_paths:` line) and `image:`:

```yaml
# strip_paths: NOT USED by this task -- pallets/click carries no agent file
# and no vendored tree at this base_sha. Shown because this manifest is the
# documentation for the key.
#
# Top-level, beside gitignore_extra, because like gitignore_extra it is a
# modification the harness makes rather than a statement about upstream --
# `repo:` holds only what can be checked against GitHub. The listed paths are
# removed from the index AND the worktree in the same fixed-identity setup
# commit that applies the test half, so declaring one MOVES start_sha (re-pin
# it, and bump task_version).
#
#   strip_paths: ["CLAUDE.md", "AGENTS.md", ".claude"]
#
# Files or directories, matched by path component (".claude" covers
# ".claude/settings.json" and not ".claudeignore"). Refused at load: an
# absolute path, "..", a glob, "." and ".git". Refused when the start state is
# built: a path no TRACKED file is at or under -- a typo strips nothing, and
# nothing downstream would say so. That also means a file the PR creates
# cannot be stripped, since it is not there at base_sha to remove.
#
# A path the reference diff also touches must be listed in
# tests.allow_extra_paths as well, which keeps it out of both halves. Strip
# does not imply exclusion: silently dropping the chunk would make
# solution_diff something other than the merged PR.
#
# Symlinks: list the link AND its target. Stripping only the target leaves a
# dangling link the agent still sees; preflight's strip probe is `-e` or `-L`,
# so that is a NO-GO rather than a silent confound.
#
# Section 6.4 confound, to be recorded here by any task that uses it: the
# humans who wrote the PR had the stripped file.
```

- [ ] **Step 7: `docs/BUILDING-A-TASK-SET.md`**

In §2's screening table, replace the agent-file row's verdict:

```markdown
| `CLAUDE.md`, `AGENTS.md`, `.claude/` or `.cursorrules` present | declare them in `strip_paths` and they are removed in the setup commit — or pick a `base_sha` predating them. List symlinks *and* their targets: if `CLAUDE.md` links to `AGENTS.md`, list both |
```

and add a new row after it:

```markdown
| committed venv or vendored tree at `base_sha` | `strip_paths` can remove it, but judge by size — 2,902 `site-packages` files is a different repository from the one the PR was merged into, and a later `base_sha` is cheaper |
```

In §3.5, after the `gitignore_extra` paragraph:

```markdown
`strip_paths` is a **top-level** key too. It lists paths removed from the start
state — agent files, a small vendored tree — in the same setup commit, so it is
visible in `start_sha`. Declaring one moves `start_sha`: re-pin it and bump
`task_version`. Each entry must match a file **tracked at `base_sha`** (a typo
that strips nothing is refused, and a file the PR *creates* cannot be
stripped), and a path the reference diff also touches has to be in
`tests.allow_extra_paths` as well, or the load is refused.
```

In §3.7's refusal table, add three rows:

```markdown
| `strip_paths` names X, which no tracked file is at or under | a typo, or a path the PR creates rather than one that exists at `base_sha`. The check exists because a strip that removes nothing is otherwise invisible |
| `strip_paths` removes X, which the reference diff's test/solution half also changes | add X to `tests.allow_extra_paths`, or narrow the strip |
| the start state still carries X, which `strip_paths` says it removed | the preflight tree is stale, or a build step re-created it, or X is a symlink whose target was stripped and the link was not. Re-run with `--force-preflight` |
```

- [ ] **Step 8: `tasks/todo.md` review section**

Append a new section at the end of the file:

```markdown
## Broadening 1 — `strip_paths` — 2026-09-01

The corpus's binding constraint was not the loader's strictness but a *date*:
most public repositories added `CLAUDE.md` or `AGENTS.md` at some point, and
every candidate PR after that commit was out. sqlglot — the richest source by
an order of magnitude — lost 116 candidates to a file no bug-fix PR touches.
`strip_paths` removes the named paths in the same fixed-identity setup commit
that already applies the test half and `gitignore_extra`, so the modification
lands in `manifest_digest` and in `start_sha` instead of in a hand-rewritten
history nobody can find upstream.

Five decisions worth keeping:

- **Strip does not imply exclusion from the reference halves.** The tempting
  shape is for a stripped path to be dropped from `solution_diff`
  automatically. That makes the reference stop being the merged PR verbatim,
  and preflight only notices when the missing hunk happens to be one the f2p
  tests need — so the case that survives every gate is a reference that is no
  longer a reference. `allow_extra_paths` already means "neither half" and
  already leaves the file named in `extra_files`, so requiring it keeps the
  combination visible.
- **A path matching nothing raises, in `materialize`.** Not at load — the
  loader never sees the tree. Not in preflight — verdicts there are cached, and
  the code that would silently do nothing is `materialize`. `--ignore-unmatch`
  is what a typo needs to become permanent: preflight's `_CONTEXT_FILES` check
  knows four names, so a mistyped vendored tree would pass every gate into an
  append-only log. The cost is a constraint task authors have to know: a strip
  only applies to a path tracked at `base_sha`, so a file the PR *creates*
  cannot be stripped even when `allow_extra_paths` legitimately names it.
- **The existence check reads `git ls-files`'s output, not its exit code.**
  Measured: `git ls-files -z -- nope` exits **0** with an empty stdout. Same
  silent zero `container._checked_exec` exists to refuse.
- **The strip probe is `-e` OR `-L`; the context-file probe stays `-e`.**
  Measured: for a symlink whose target is gone, `[ -e x ]` exits 1 and
  `[ -L x ]` exits 0. sqlglot's `CLAUDE.md` is a symlink to `AGENTS.md`, so
  stripping the target alone leaves a path the agent's `ls` shows and an
  `-e`-only assertion calls removed. The two callers want opposite answers on
  that input, which is why the predicate takes a flag rather than picking one.
- **The build context is stripped too.** For agent files it does not matter —
  the bind mount replaces `/repo`. For a committed venv it does: the tree is on
  the import path when `image.build` runs `pip install -e .`, so the image
  pins an environment resolved against a directory the run tree does not have.

Two things the validation had to add beyond `_validate_prefixes`, both because
this key's effect is a delete rather than a classification: `.` passes every
existing check and names the whole tree (`PurePosixPath(".").parts` is `()`,
and `is_relative_to(".")` is True for everything), and `git rm` reads
pathspecs, so an unrefused `*` would make what is removed a property of the
tree rather than of the manifest.

Both one-line **call sites** are pinned by default-suite tests rather than by
mutation anchors, and that was round 1 of the plan review's finding: helper
tests leave `build_task_image`'s call and `preflight`'s assertion deletable
with the suite green, and an integration-only pin is deselected by
`addopts = "-m 'not integration'"` on the run anyone actually makes.

`PREFLIGHT_VERSION` 2 → 3, and the grader's prose reference to version 2 became
"2 or later". `SCHEMA_VERSION` did not move: nothing new is written into a
record, and the strip is already visible there as the start sha `to_task_spec`
carries in `base_sha`.

Confound recorded in HARVESTING.md rather than in code: the humans who wrote
the PR had the stripped file. A repository whose `CLAUDE.md` shaped how its
contributors worked is not quite the repository the models are handed once it
is gone, and that belongs in each manifest's comments as a §6.4 caveat.
```

- [ ] **Step 9: Full verification**

Run: `cd bakeoff && .venv/bin/python -m pytest tests/ -q`
Expected: PASS, ≥1129 + the new tests, 46 deselected.

Run: `cd bakeoff && .venv/bin/python -m pytest -q -m integration --basetemp="$HOME/.cache/bakeoff-pytest"`
Expected: PASS. This is the leg that matters for Tasks 4 and 5 — it builds a task image and runs the real gate.

Run: `cd bakeoff && .venv/bin/python scripts/verify_logger.py`
Expected: the §6.6 gate PASSes (it is offline and needs a Docker daemon; report `GATE INCOMPLETE` only if the daemon is down).

Run: `cd /Users/suhaassurapaneni/Pindrop/Pindrop-llm-eval-broaden && git diff -- bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml`
Expected: only the commented example block added; **no change to the `start_sha:` line**.

- [ ] **Step 10: Commit**

```bash
git add bakeoff/taskset/HARVESTING.md \
        bakeoff/taskset/click-3360-write-usage-empty-args/task.yaml \
        docs/BUILDING-A-TASK-SET.md tasks/todo.md
git commit -m "$(cat <<'EOF'
docs: strip_paths lifts the agent-file date floor, and records the confound

HARVESTING.md already sanctioned a manual strip and asked for it under
provenance; the key now carries it, so the docs say where it is recorded and
what refuses it. sqlglot's 2026-02-02 floor and the internal repo's 2026-03-31
floor are both liftable; the 2026-07-02 venv floor is expressible and still a
bad idea, and the size line is written down. Two constraints authors need: the
path must be tracked at base_sha, and a symlink needs both names listed.

The confound goes with it: the humans who wrote the PR had that file, so a
task that strips one is a section 6.4 caveat its manifest has to name.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes

- **Coverage.** Every design decision above has a task: key/shape and validation → Task 1; reference-half overlap and the fixture that makes it testable → Task 2; `materialize` placement, missing-path refusal, ordering, `start_sha` movement and backwards compatibility → Task 3; build context and its call site → Task 4; evidence, the `-e`/`-L` probe, the tree-level assertion, `PREFLIGHT_VERSION` and the grader's stale prose → Task 5; docs and the review log → Task 6.
- **Names used consistently across tasks:** `TaskManifest.strip_paths`, `_validate_strip_paths`, `_PATHSPEC_MAGIC`, `_refuse_stripped_halves`, `_strip_paths_from_tree`, `_strip_build_context`, `_present(container, names, *, dangling_counts=False)`, evidence keys `stripped_paths` / `stripped_paths_present`.
- **Both call sites are pinned in the default suite**, which is what licenses adding no `MUTATIONS` entry: `test_build_task_image_strips_the_context_it_unpacks` (Task 4) and the three `_ScriptedContainer` tests (Task 5).
- **Not done, deliberately:** no `strip_paths` × `tests.paths` overlap rule (stripping a helper the suite needs turns the suite red, which preflight reports on the next step; a load-time rule would be guessing at what a suite needs); no pruning of emptied parent directories in the build context (an empty directory carries no module, and pruning would guess at which parents the archive was meant to have); no `SCHEMA_VERSION` bump; no `TASKS.md` entry; nothing for broadenings 2–7.

---

## Deferred to CLAUDE.md

Do **not** edit `CLAUDE.md` in this branch. After this work merges, add the
following to the **Invariants** section, immediately after the paragraph
beginning "**The input path.**":

> **A modification to the start state lives in the manifest, or it lives
> nowhere a reader can find it.** `strip_paths` removes declared paths — agent
> files, a small vendored tree — inside the *same* fixed-identity setup commit
> that applies the test half and `gitignore_extra`, so the change is in
> `manifest_digest` and in `start_sha` rather than in a hand-rewritten
> `base_sha` a reader diffing against upstream would not find. Three refusals
> are load-bearing and each was a way to remove the wrong thing: `.` passes
> every check `_validate_prefixes` makes and names the whole tree
> (`PurePosixPath(".").parts` is `()`), `git rm` reads *pathspecs* so an
> unrefused `*` makes what is removed a property of the tree rather than of the
> manifest, and a path no *tracked* file is at or under raises rather than being
> ignored — `--ignore-unmatch` is what turns a typo like `.cluade/` into a file
> that stays in the start state forever, since preflight's `_CONTEXT_FILES`
> check knows exactly four names. The existence check reads `git ls-files`'s
> **output**: measured, `git ls-files -z -- nope` exits 0 with an empty stdout.
> **Strip does not imply exclusion from the reference halves** — a stripped
> path the diff touches must also be in `allow_extra_paths`, because silently
> dropping the chunk makes `solution_diff` something other than the merged PR
> and preflight only notices when the missing hunk is one the f2p tests need.
> Preflight asserts the paths are gone *from the tree*, not from the manifest,
> for the reason the pruned mirror does: the fingerprint is a change-detector,
> the invariant is re-checked against the artifact — and its probe is `-e` **or**
> `-L`, because a symlink whose target was stripped is a path `test -e` calls
> absent and `ls` still shows (measured: `[ -e dangling ]` exits 1,
> `[ -L dangling ]` exits 0), while a *context* file probe stays `-e` since a
> dangling link gives the agent no context.

And to the **Config gotchas** section:

> **The image build context is `base_sha`, not the start state, so `strip_paths`
> is applied to it separately.** An agent file left in the scaffold is harmless
> — the bind mount replaces `/repo` at run time — but a committed venv is on the
> import path when `image.build` runs `pip install -e .`, so the image pins an
> environment resolved against a directory the run tree does not have. A path
> missing from the context is *not* an error there: `materialize` raises on
> that, and `run_matrix` builds the image first.
