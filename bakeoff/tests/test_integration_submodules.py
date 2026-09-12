"""Materialize and build from ONE superproject, and compare the two.

Every other test in broadening 6 checks one half. `test_tasks.py` drives
`materialize` against a scratch superproject and `test_images.py` drives
`build_task_image` against another; neither can see the failure this file
exists for, which is a DISAGREEMENT between them.

The image's `pip install -e .` resolves against the BUILD CONTEXT's copy of
the submodule -- a second `git archive` extracted into the gitlink's path --
and the agent's suite runs against the RUN TREE's copy, which came from a
`git submodule update` out of a pruned bare mirror. Two archives of two
different objects, produced by two functions, and nothing downstream compares
them. Same class as a non-editable install: the image and the run disagree
silently, and preflight's green-after check only catches it when the
disagreement happens to break the suite.

So this file takes the two artifacts and compares the bytes.

MARKERS. `pytestmark` carries BOTH `integration` and `task_image`, module-wide
rather than per test, so a case added later cannot be written without them.
The second marker is what keeps the section 6.6 logger gate offline:
`verify_logger.py` selects `-m "integration and not task_image"`, and CLAUDE.md
pins that gate as offline, no credentials, no spend.

    cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" \
        tests/test_integration_submodules.py --basetemp="$HOME/.cache/bakeoff-pytest"

`--basetemp` UNDER `$HOME` IS MANDATORY, not hygiene. On macOS the default
`tmp_path` resolves under `/var/folders`, which the Docker VM does not mount,
and a repo bind-mounted from there appears inside the container as a silently
EMPTY DIRECTORY -- so `preflight` would start a container over nothing and the
byte comparison would be between two files that are not there. Measured on the
first attempt at M13. That is also why the comparison below asserts the file
EXISTS on both sides before it asserts the bytes are equal: `read_bytes() ==
read_bytes()` on two absent paths raises, but an emptiness that merely made
both sides equal would have passed, and this class of bug has passed here
before.

The superproject is synthetic and local, for the reason `test_tasks.py`'s
`upstream` fixture gives: a test that needed the network is a test that stops
running. The submodule's upstream carries a commit PAST the gitlink so the
prune has something to fail to remove.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bakeoff import tasks
from bakeoff.images import build_base_images, build_task_image, image_entrypoint
from bakeoff.oracle import ensure_oracle
from bakeoff.preflight import preflight
from bakeoff.schema import (
    Artifacts,
    Checkpoint,
    Outcome,
    RunRecord,
    TerminationReason,
    Versions,
)
from bakeoff.tasks import load_task, materialize, task_runtime

pytestmark = [pytest.mark.integration, pytest.mark.task_image]

REPO_ROOT = Path(__file__).resolve().parent.parent

SUB_PATH = "vendor/libdep"
SUB_LIB = "VALUE = 1\n"
SUB_LIB_FUTURE = "VALUE = 999\n"

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"

#: The suite IMPORTS FROM THE SUBMODULE, on purpose. `result.ok` asserted over
#: a suite that never touched `vendor/libdep` would pass with the submodule
#: directory empty, which is exactly the state this file exists to rule out --
#: an assertion that cannot fail for the reason it is written for proves
#: nothing.
#:
#: Two tests, not one. `Runner.pass_to_pass` with no explicit `tests.p2p`
#: deselects the f2p ids and runs the rest; a suite whose only test IS the f2p
#: collects nothing there, and pytest's exit 5 is a preflight problem. So
#: `test_submodule_is_populated` is the p2p member, and it is also the one
#: that fails loudly rather than at import time if the gitlink content is not
#: where it should be.
OLD_TEST = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "libdep"))

from calc import add
from libdep import VALUE


def test_submodule_is_populated():
    assert VALUE == 1


def test_add():
    assert add(1, 1) == 0
"""

NEW_TEST = OLD_TEST.replace("assert add(1, 1) == 0", "assert add(1, 2) == 3")

MANIFEST = """\
task_id: sub-int-001
task_version: 1
repo:
  url: {url}
  base_sha: {base_sha}
prompt: |
  fix add()
tests:
  paths: ["tests/"]
  runner: ["python", "-m", "pytest", "-q"]
  f2p: ["tests/test_calc.py::test_add"]
"""


def _sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    """Everything Docker has to see, under `--basetemp`. See the module docstring."""
    return tmp_path_factory.mktemp("submodule-integration")


@pytest.fixture(scope="module")
def superproject(workspace) -> dict:
    """A superproject pinning one submodule, whose upstream has moved PAST the pin.

    `-c protocol.file.allow=always` on every submodule-touching command: git
    refuses the file transport for submodules by default since the
    CVE-2022-39253 hardening, and a local path is a file transport. Production
    needs the same flag for the same reason -- the pruned mirror is a local
    path -- so this is not a fixture-only concession.
    """
    lib = workspace / "libdep"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB)
    _sh("git", "init", "-q", cwd=lib)
    _sh("git", "config", "user.email", "t@t.test", cwd=lib)
    _sh("git", "config", "user.name", "t", cwd=lib)
    _sh("git", "add", "-A", cwd=lib)
    _sh("git", "commit", "-q", "-m", "libdep v1", cwd=lib)
    pinned = _sh("git", "rev-parse", "HEAD", cwd=lib)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB_FUTURE)
    _sh("git", "commit", "-q", "-am", "libdep FUTURE", cwd=lib)
    future = _sh("git", "rev-parse", "HEAD", cwd=lib)

    repo = workspace / "super"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    (repo / ".gitignore").write_text("__pycache__/\n")
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(lib), SUB_PATH, cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "-C", SUB_PATH,
        "checkout", "-q", pinned, cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "pin the submodule", cwd=repo)
    base = _sh("git", "rev-parse", "HEAD", cwd=repo)

    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "fix", cwd=repo)
    head = _sh("git", "rev-parse", "HEAD", cwd=repo)
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout

    task_dir = workspace / "taskset" / "sub-int-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        MANIFEST.format(url=str(repo), base_sha=base))
    (task_dir / "reference.diff").write_text(reference)
    return {"task_dir": task_dir, "lib": lib, "pinned": pinned,
            "future": future}


@pytest.fixture(scope="module")
def local_urls():
    """Accept the fixture's local-path submodule url.

    `monkeypatch` is function-scoped and this fixture is not, so the attribute
    is swapped by hand and restored in the teardown. `_SUBMODULE_URL_PREFIX`
    is emptied rather than widened, which makes its `startswith` vacuously
    true -- the same shape `test_tasks.py`'s `local_urls` uses, and for the
    same reason: a fixture that needed the network is a fixture that stops
    running.
    """
    original = tasks._SUBMODULE_URL_PREFIX
    tasks._SUBMODULE_URL_PREFIX = ""
    try:
        yield
    finally:
        tasks._SUBMODULE_URL_PREFIX = original


def test_the_image_and_the_run_tree_carry_the_same_submodule_blob(
        workspace, superproject, local_urls):
    """One superproject, both halves, and the four things that can disagree."""
    task = load_task(superproject["task_dir"])
    cache = workspace / "cache"

    run_tree = workspace / "run"
    start_sha = materialize(task, run_tree, cache)

    # Keyed by version exactly as the drivers do it: the base a task gets is
    # the one its MANIFEST names, never a default that happens to match.
    # Broadening 5 made the base per version, and a hard-coded "3.12" here
    # would go on passing while testing a base the task never asked for.
    base = build_base_images(REPO_ROOT, [task_runtime(task)])[task_runtime(task)].image_id
    image = build_task_image(task, base, workspace / "build", cache)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; RunContainer refuses a tag"
    )
    assert not image_entrypoint(image), (
        "the task image declares an ENTRYPOINT; RunContainer's `sleep "
        "infinity` would become an argument to it and the container would "
        "exit immediately"
    )

    result = preflight(task, image=image, repo_path=run_tree,
                       start_sha=start_sha)

    # 1. The gate passes at all. The suite imports from the submodule, so an
    #    empty `vendor/libdep` is a collection error in both the red-before
    #    and the green-after run -- this assertion cannot pass over an
    #    unpopulated submodule.
    assert result.ok, result.problems

    # 2. The gate OBSERVED the submodule, at the gitlink, initialised. `[]`
    #    here would be an observation of "there are none" -- which is what the
    #    check wrote for every task before this broadening, and is exactly the
    #    false negative a bare `result.ok` would let through.
    #    `declared_unneeded` and `empty` are written for EVERY entry, declared
    #    or not, so a reader who cannot see the fields on the other entries
    #    cannot mistake "this task declared none" for "this gate did not know
    #    about the key". `empty: False` here is the real `ls -A` against a
    #    real populated submodule, in the real container.
    assert result.evidence["submodules"] == [
        {"path": SUB_PATH, "sha": superproject["pinned"],
         "initialised": True, "marker": " ",
         "declared_unneeded": False, "empty": False,
         # round 2 item 18: written on every entry, depth 1 included, so a
         # reader can tell a flat tree from a gate that did not know about
         # nesting.
         "depth": 1,
         # round 2 item 16: an already-absolute url resolves to itself, so
         # both the declared and the persisted value are the submodule's
         # real local path.
         "url_declared": str(superproject["lib"]),
         "url_persisted": str(superproject["lib"])}
    ]
    assert result.evidence["submodules_orphaned"] == []
    # `{}` is "measured, this task declares no unneeded submodules".
    assert result.evidence["submodules_empty_after_suite"] == {}

    # 3. THE COMPARISON THIS FILE EXISTS FOR. Two archives of two objects,
    #    from two functions, and nothing else in the codebase puts them side
    #    by side. Existence is asserted first: two absent files would raise,
    #    but an emptiness that made both sides equal would have PASSED, and
    #    that is precisely the `/var/folders` bind-mount failure the module
    #    docstring records.
    in_run = run_tree / SUB_PATH / "libdep" / "__init__.py"
    in_image = (workspace / "build" / f"image-{task.task_id}" / "repo"
                / SUB_PATH / "libdep" / "__init__.py")
    assert in_run.is_file(), f"{in_run} is missing: the run tree has no submodule"
    assert in_image.is_file(), f"{in_image} is missing: the context has no submodule"
    assert in_run.read_bytes() == in_image.read_bytes()
    assert in_run.read_bytes() == SUB_LIB.encode()

    # 4. The prune holds in the ARTIFACT THE AGENT GETS, not only in the
    #    cache. `materialize` clones the submodule from a pruned bare mirror,
    #    and the object that must not be reachable is the upstream commit
    #    AFTER the gitlink -- `git log --all` in the submodule is what hands
    #    it over, differentially, to an arm that looks. `cat-file -e` rather
    #    than a ref check: an unreferenced object is still reachable by
    #    `cat-file -p` and `fsck --lost-found`, which is the whole argument
    #    `ensure_pruned_mirror` was written on.
    reachable = subprocess.run(
        ["git", "cat-file", "-e", superproject["future"]],
        cwd=run_tree / SUB_PATH, capture_output=True, text=True,
    )
    assert reachable.returncode != 0, (
        f"the run tree's submodule can reach {superproject['future']}, which "
        "is one commit PAST the gitlink -- the pruned mirror did not prune, "
        "or the clone came from somewhere else"
    )


# ---------------------------------------------------------------------------
# fix-1: the submodule sits UNDER the declared test prefix
# ---------------------------------------------------------------------------
#
# The fixture above puts the submodule at `vendor/libdep`, outside
# `tests.paths` -- `_check_test_restore`'s `git rm -r -f -- vendor/` never
# touches `tests/`, so that layout is silent on the defect. Measured
# 2026-09-02 against `tomlkit-514-inline-table-comment-separator`: its
# submodule (`tests/toml-test`) sits INSIDE the declared prefix, which is the
# normal layout, and grading its reference solution failed
# `not_graded_reason: environment_error` at `test_restore` before this fix --
# `git rm -r -f -- tests/` deleted the submodule's working tree and
# `git checkout <start_sha> -- tests/` restored only the gitlink to the
# index, never its content.

SUB_PATH_UNDER_TESTS = "tests/vendor/libdep"

#: The f2p member. Deliberately in a file that never touches the
#: submodule -- tomlkit-514's own `tests/test_items.py` (the f2p home) does
#: not import from `tests/toml-test` either, which is why the measured
#: failure landed on `p2p`, not `f2p`: a version of this fixture that put both
#: the f2p test and the submodule import in ONE file (an earlier draft) makes
#: `f2p_failed` fire first and never exercises `p2p`'s environment-error
#: branch at all.
TEST_CALC_UNDER_PREFIX = """\
from calc import add


def test_add():
    assert add(1, 1) == 0
"""

NEW_TEST_CALC_UNDER_PREFIX = TEST_CALC_UNDER_PREFIX.replace(
    "assert add(1, 1) == 0", "assert add(1, 2) == 3"
)

#: The p2p member, in its OWN file -- tomlkit-514's `tests/test_toml_tests.py`
#: shape. It reads the submodule AT IMPORT TIME (module-level `sys.path`
#: insert then `import`), so an unpopulated submodule fails LOUDLY, at
#: collection, rather than by a missed assertion; and it is NEVER EDITED
#: between base and head, exactly like the real fixture's regression suite,
#: so it never appears in the reference diff at all.
TEST_SUBDATA_UNDER_PREFIX = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor" / "libdep"))

from libdep import VALUE


def test_submodule_is_populated():
    assert VALUE == 1
"""

MANIFEST_UNDER_PREFIX = """\
task_id: sub-int-002
task_version: 1
repo:
  url: {url}
  base_sha: {base_sha}
prompt: |
  fix add()
tests:
  paths: ["tests/"]
  runner: ["python", "-m", "pytest", "-q"]
  f2p: ["tests/test_calc.py::test_add"]
"""


@pytest.fixture(scope="module")
def superproject_under_test_prefix(workspace) -> dict:
    """The fix-1 regression fixture: same shape as `superproject`, but the
    submodule is pinned INSIDE `tests/` instead of beside it.

    A fresh submodule (`libdep2`), never the `superproject` fixture's --
    module-scoped fixtures in this file run in an unspecified order and a
    shared submodule mirror would make one test's clone the reason the
    other's prune assertion holds.
    """
    lib = workspace / "libdep2"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB)
    _sh("git", "init", "-q", cwd=lib)
    _sh("git", "config", "user.email", "t@t.test", cwd=lib)
    _sh("git", "config", "user.name", "t", cwd=lib)
    _sh("git", "add", "-A", cwd=lib)
    _sh("git", "commit", "-q", "-m", "libdep v1", cwd=lib)

    repo = workspace / "super2"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(TEST_CALC_UNDER_PREFIX)
    (repo / "tests" / "test_subdata.py").write_text(TEST_SUBDATA_UNDER_PREFIX)
    (repo / ".gitignore").write_text("__pycache__/\n")
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(lib), SUB_PATH_UNDER_TESTS, cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "pin the submodule under tests/",
        cwd=repo)
    base = _sh("git", "rev-parse", "HEAD", cwd=repo)

    # `tests/test_subdata.py` is NOT touched here -- it stays identical from
    # base to head, exactly like tomlkit-514's own regression suite, so the
    # reference diff never carries it and the p2p failure this fixture is
    # for cannot be blamed on the diff instead of the restore.
    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST_CALC_UNDER_PREFIX)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "fix", cwd=repo)
    head = _sh("git", "rev-parse", "HEAD", cwd=repo)
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout

    task_dir = workspace / "taskset" / "sub-int-002"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        MANIFEST_UNDER_PREFIX.format(url=str(repo), base_sha=base))
    (task_dir / "reference.diff").write_text(reference)
    return {"task_dir": task_dir}


def _record_with_diff(
    task, diff: str, *, run_id: str | None = None,
    collection_id: str = "integration-submodule-test-prefix",
    submodules_dirty_at_exit: dict[str, str] | None = None,
) -> RunRecord:
    """Copied from `test_integration_node_task._record_with_diff` rather than
    imported -- that one hardcodes the node fixture's task id. `run_id` is
    namespaced to this fixture so a shared artifacts cache never collides
    with `test_integration_node_task`'s rows.

    The three keyword parameters default to what every existing call already
    produced, so no caller changes. They exist because a SECOND test building
    an empty-diff record would otherwise reuse `sub-int-002-empty` -- the
    collision this namespacing exists to prevent -- and would inherit the
    fix-1 fixture's collection id while using a different fixture's tree.
    """
    return RunRecord(
        run_id=run_id or f"sub-int-002-{'empty' if not diff else 'reference'}",
        task_id=task.task_id,
        task_version=task.task_version,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-09-02T00:00:00Z",
        finished_at="2026-09-02T00:05:00Z",
        # The harness never grades, so a real record of a successful run says
        # FAILED here -- anything the grader concluded from this field would
        # be concluding it from a placeholder.
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=4,
        turns_streamed=4,
        collection_id=collection_id,
        checkpoints=[
            Checkpoint(turn=4, diff_vs_base=diff, files_touched=[],
                       elapsed_ms=0)
        ],
        artifacts=Artifacts(final_diff=diff),
        versions=Versions(),
        submodules_dirty_at_exit=submodules_dirty_at_exit,
    )


def test_the_grader_resolves_a_reference_fix_whose_submodule_is_under_tests(
    workspace, superproject_under_test_prefix, local_urls, tmp_path_factory,
):
    """Fix-1's regression case, graded end to end in a real container.

    Under grader v6 this failed `not_graded_reason: environment_error` at
    `test_restore`: `git rm -r -f -- tests/` deletes
    `tests/vendor/libdep`'s working tree, `git checkout <start_sha> --
    tests/` restores only the gitlink, and `tests/test_subdata.py`'s
    module-level `sys.path`/`import` of the submodule then raises at
    collection while `f2p` (a different file, never touching the submodule)
    still passes -- byte-identical in shape to the tomlkit-514 measurement:
    `not_graded_reason: environment_error` at `test_restore` for a submission
    that never touched a gitlink. Grading the REFERENCE solution -- the
    genuine fix -- must resolve `True`; anything else here is the defect
    fix-1 exists to close.
    """
    task = load_task(superproject_under_test_prefix["task_dir"])
    cache = workspace / "cache2"

    base = build_base_images(REPO_ROOT, [task_runtime(task)])[task_runtime(task)].image_id
    image = build_task_image(task, base, workspace / "build2", cache)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; RunContainer refuses a tag"
    )
    assert not image_entrypoint(image), (
        "the task image declares an ENTRYPOINT; RunContainer's `sleep "
        "infinity` would become an argument to it and the container would "
        "exit immediately"
    )

    from bakeoff.grader import grade_run

    oracle = ensure_oracle(task, image, cache)
    record = _record_with_diff(task, task.solution_diff)

    grade = grade_run(record, task, image, oracle, cache,
                      tmp_path_factory.mktemp("artifacts-sub-int-002"))

    assert grade.resolved is True, (
        grade.not_graded_detail or grade.grade_failure
    )
    assert grade.framework == "pytest"
    assert grade.f2p_failed_node_ids == ()


# ---------------------------------------------------------------------------
# round-2 item 17: the in-submodule edit the submission diff cannot carry
# ---------------------------------------------------------------------------


def test_an_uncommitted_edit_inside_a_submodule_is_recorded_then_refused(
    workspace, superproject, local_urls, tmp_path_factory,
):
    """The defect and its closure, in a real container against a real
    submodule.

    `git add -A` stages NOTHING for a tracked file edited inside an
    initialised submodule, so `container.snapshot_diff` returns zero bytes --
    byte-identical to an agent that changed nothing. The offline grader then
    stops at `EMPTY_PATCH`, which is a `GradeFailure`: `resolved: False`, an
    accusation that the model produced nothing, over a limitation of the
    harness's own capture, in an append-only file. `assert
    checkpoint.diff_vs_base == ""` below is that defect, and it is the
    assertion that would have caught it.

    A SEPARATE run tree from the first test's. Module-scoped fixtures run in
    an unspecified order, and `fresh_tree`'s rule -- a host path that has been
    bind-mounted once is never bind-mounted again -- applies to these mounts
    too.

    The TASK image, not the base image: `RunContainer.__enter__` asserts
    non-root and no `ENTRYPOINT`, and a failure of either on the base image
    would read as this item's defect. Docker's layer cache makes the second
    build of an image the first test already built a no-op.
    """
    from bakeoff.checkpoints import CheckpointRecorder
    from bakeoff.container import RunContainer
    from bakeoff.grade_schema import NotGradedReason
    from bakeoff.grader import grade_run

    task = load_task(superproject["task_dir"])
    cache = workspace / "cache"
    run_tree = workspace / "run-edit"
    start_sha = materialize(task, run_tree, cache)

    base = build_base_images(REPO_ROOT, [task_runtime(task)])[
        task_runtime(task)].image_id
    image = build_task_image(task, base, workspace / "build", cache)

    stamps = [".git/index", f".git/modules/{SUB_PATH}/index"]
    with RunContainer(image=image, repo_path=str(run_tree),
                      base_sha=start_sha) as container:
        container.exec(
            ["sh", "-c", f"echo EDITED >> {SUB_PATH}/libdep/__init__.py"]
        )

        recorder = CheckpointRecorder(container, start_sha, every_k_turns=1)
        checkpoint = recorder.force_capture(turn=1, elapsed_ms=0)

        # `--no-optional-locks`, in the environment that matters. Stamped
        # AFTER the capture and read after a second `submodule_states()`, so
        # the assertion is about THIS read and nothing else.
        #
        # Measured 2026-09-03 in this image, one command per stamp:
        #
        #     read-tree   super=1577865600  sub=1577865600
        #     add -A      super=1577865600  sub=REWRITTEN
        #     diff        super=1577865600  sub=1577865600
        #     status v2   super=1577865600  sub=1577865600
        #     ls-files    super=1577865600  sub=1577865600
        #
        # So the SUBMODULE's index is refreshed by `git add -A` -- which is
        # `snapshot_diff`'s and predates this field entirely: `add -A` stats
        # the gitlink to decide whether it moved, and what it refreshes is a
        # stat cache and not content. The superproject's own `.git/index` is
        # untouched throughout, which is the rule `SNAPSHOT_INDEX`'s comment
        # states. Asserting the whole capture sequence leaves both alone
        # would be asserting something false, about a command this item did
        # not add.
        container.exec(["touch", "-d", "@1577865600", *stamps])
        container.submodule_states()
        mtimes = container.exec(["stat", "-c", "%Y", *stamps]).stdout.split()
        assert mtimes == ["1577865600", "1577865600"], mtimes

    assert checkpoint.diff_vs_base == "", (
        "the defect: `git add -A` stages nothing for an in-submodule edit, so "
        "the submission is byte-identical to an agent that changed nothing"
    )
    assert checkpoint.submodules_dirty == {SUB_PATH: "S.M."}
    assert recorder.errors == []

    # The grader half. `_record_with_diff` sets `submodules_dirty_at_exit`
    # DIRECTLY, because this test does not run `assemble_record` and so the
    # record field does not appear on its own from the Checkpoint above.
    record = _record_with_diff(
        task, "", run_id="sub-int-003-edit",
        collection_id="integration-submodule-edit",
        submodules_dirty_at_exit=dict(checkpoint.submodules_dirty),
    )

    grade = grade_run(record, task, image, None, cache,
                      tmp_path_factory.mktemp("artifacts-sub-int-003"))

    assert grade.resolved is None
    assert grade.not_graded_reason == \
        NotGradedReason.SUBMODULE_EDIT_UNGRADABLE.value
    assert grade.grade_failure is None
    assert SUB_PATH in grade.not_graded_detail


# ---------------------------------------------------------------------------
# round-2 item 10: the leak guards are total, and given a post-condition
# ---------------------------------------------------------------------------


def test_the_materialized_run_tree_carries_no_host_mirror_path_under_dot_git(
        workspace, superproject, local_urls):
    """The real init this item asks for, on the module's real superproject
    rather than a function-scoped scratch one.
    """
    task = load_task(superproject["task_dir"])
    run_tree = workspace / "run-leak"
    cache = workspace / "cache"

    materialize(task, run_tree, cache)

    needle = str(cache / "repos").encode()
    hits = [
        path for path in (run_tree / ".git").rglob("*")
        if path.is_file() and not path.is_symlink()
        and needle in path.read_bytes()
    ]
    assert hits == []

    # Non-vacuity, in the corrected per-file form: the submodule's
    # `logs/HEAD` -- whose expire is the last write to it -- is present at
    # 0 bytes; the superproject's is present, NON-EMPTY (the setup commit
    # appends to it after the expire runs) and needle-free.
    sub_head = run_tree / ".git" / "modules" / SUB_PATH / "logs" / "HEAD"
    assert sub_head.is_file() and sub_head.stat().st_size == 0

    super_head = run_tree / ".git" / "logs" / "HEAD"
    assert super_head.is_file() and super_head.stat().st_size > 0


def test_a_skipped_reflog_expire_is_refused_on_a_real_run_tree(
        workspace, superproject, local_urls):
    """The red half. `tasks.subprocess.run` is swapped by hand in a
    try/finally -- the module's fixtures are module-scoped and `monkeypatch`
    is function-scoped, so the neighbouring `local_urls` fixture already
    restores an attribute this way.

    Considered and rejected: asserting from INSIDE a `RunContainer` with
    `grep -rlF <cache>/repos /repo/.git`. `/repo` is a bind mount of exactly
    the tree the host-side scan reads, so the container makes no independent
    observation, and `grep`'s presence in the eval-agent image is not
    something this plan measured. The bind-mount identity is already covered
    by this module's blob-comparison test.
    """
    task = load_task(superproject["task_dir"])
    run_tree = workspace / "run-leak-red"
    cache = workspace / "cache"
    dest_sub = run_tree / SUB_PATH

    real_run = subprocess.run

    def fake(*popenargs, **kwargs):
        argv = popenargs[0]
        if (list(argv[:2]) == ["git", "reflog"]
                and str(kwargs.get("cwd")) == str(dest_sub)):
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return real_run(*popenargs, **kwargs)

    tasks.subprocess.run = fake
    try:
        with pytest.raises(tasks.TaskError, match=r"logs/HEAD") as excinfo:
            materialize(task, run_tree, cache)
    finally:
        tasks.subprocess.run = real_run

    assert task.task_id in str(excinfo.value)


# ---------------------------------------------------------------------------
# round-2 item 16: a relative .gitmodules url, resolved against repo.url
# ---------------------------------------------------------------------------

MANIFEST_RELATIVE_URL = """\
task_id: sub-int-003
task_version: 1
repo:
  url: {url}
  base_sha: {base_sha}
prompt: |
  fix add()
tests:
  paths: ["tests/"]
  runner: ["python", "-m", "pytest", "-q"]
  f2p: ["tests/test_calc.py::test_add"]
"""


@pytest.fixture(scope="module")
def superproject_relative_url(workspace) -> dict:
    """`superproject`'s shape, with the committed `.gitmodules` url rewritten
    to the RELATIVE form `../libdep3` before `base_sha` is cut.

    A fresh submodule (`libdep3`) and a fresh superproject (`super3`) --
    never `superproject`'s or `superproject_under_test_prefix`'s. This
    file's own established rule, stated in that fixture's docstring and
    quoted here because it is the reason: module-scoped fixtures in this
    file run in an unspecified order, and a shared submodule mirror would
    make one test's clone the reason another's prune assertion holds.

    Offline throughout, exactly as `superproject` is: every repository is a
    local `git init` and every submodule-touching command carries
    `-c protocol.file.allow=always`. The resolution this fixture exercises is
    `<workspace>/super3` + `../libdep3` -> `<workspace>/libdep3`, the
    directory it just built.
    """
    lib = workspace / "libdep3"
    (lib / "libdep").mkdir(parents=True)
    (lib / "libdep" / "__init__.py").write_text(SUB_LIB)
    _sh("git", "init", "-q", cwd=lib)
    _sh("git", "config", "user.email", "t@t.test", cwd=lib)
    _sh("git", "config", "user.name", "t", cwd=lib)
    _sh("git", "add", "-A", cwd=lib)
    _sh("git", "commit", "-q", "-m", "libdep v1", cwd=lib)
    pinned = _sh("git", "rev-parse", "HEAD", cwd=lib)

    repo = workspace / "super3"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST)
    (repo / ".gitignore").write_text("__pycache__/\n")
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(lib), SUB_PATH, cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "-C", SUB_PATH,
        "checkout", "-q", pinned, cwd=repo)
    # THE REWRITE. The committed `.gitmodules` url is now relative --
    # `base_sha` carries it, which is what makes derivation see the RAW fact
    # (M3) rather than a git-supplied resolution.
    _sh("git", "config", "-f", ".gitmodules", "submodule.vendor/libdep.url",
        "../libdep3", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "pin the submodule (relative url)",
        cwd=repo)
    base = _sh("git", "rev-parse", "HEAD", cwd=repo)

    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "fix", cwd=repo)
    head = _sh("git", "rev-parse", "HEAD", cwd=repo)
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout

    task_dir = workspace / "taskset" / "sub-int-003"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        MANIFEST_RELATIVE_URL.format(url=str(repo), base_sha=base))
    (task_dir / "reference.diff").write_text(reference)
    return {"task_dir": task_dir, "lib": lib, "pinned": pinned}


def test_a_relative_url_superproject_materializes_and_the_image_matches(
        workspace, superproject_relative_url, local_urls):
    """The one test that proves the whole path end to end: the resolver, the
    mirror key, `_init_submodules`'s preset-and-rewrite, and
    `_extract_submodules`. Same assertion
    `test_the_image_and_the_run_tree_carry_the_same_submodule_blob` makes for
    an absolute url, over a task whose `.gitmodules` declares a relative
    one."""
    task = load_task(superproject_relative_url["task_dir"])
    cache = workspace / "cache3"

    run_tree = workspace / "run3"
    start_sha = materialize(task, run_tree, cache)

    # 1. Populated at its gitlink. `git submodule status` reads a leading
    #    space (M1b) -- initialised, not `-` or `+`.
    status = subprocess.run(
        ["git", "submodule", "status"], cwd=run_tree,
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.startswith(" ")

    # 2. THE RUN TREE PERSISTS THE RESOLVED URL, never the raw relative one
    #    (D7, M5). `str(superproject_relative_url["lib"])` is exactly what
    #    a clone with a reachable remote has git itself write into
    #    `.git/config`.
    persisted = _sh(
        "git", "config", "--get", f"submodule.{SUB_PATH}.url", cwd=run_tree,
    )
    assert persisted == str(superproject_relative_url["lib"])

    base = build_base_images(
        REPO_ROOT, [task_runtime(task)])[task_runtime(task)].image_id
    image = build_task_image(task, base, workspace / "build3", cache)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; RunContainer refuses a tag"
    )
    assert not image_entrypoint(image), (
        "the task image declares an ENTRYPOINT; RunContainer's `sleep "
        "infinity` would become an argument to it and the container would "
        "exit immediately"
    )

    result = preflight(task, image=image, repo_path=run_tree,
                       start_sha=start_sha)
    assert result.ok, result.problems

    # 3. THE SAME BLOB COMPARISON `test_the_image_and_the_run_tree_carry_
    #    the_same_submodule_blob` makes for an absolute url -- the image
    #    context's mirror is keyed on the RESOLVED url too (`_extract_
    #    submodules`), so both archives come from the same object set.
    in_run = run_tree / SUB_PATH / "libdep" / "__init__.py"
    in_image = (workspace / "build3" / f"image-{task.task_id}" / "repo"
                / SUB_PATH / "libdep" / "__init__.py")
    assert in_run.is_file(), f"{in_run} is missing: the run tree has no submodule"
    assert in_image.is_file(), (
        f"{in_image} is missing: the context has no submodule"
    )
    assert in_run.read_bytes() == in_image.read_bytes()
    assert in_run.read_bytes() == SUB_LIB.encode()


# ---------------------------------------------------------------------------
# round-2 item 18: a submodule of a submodule
# ---------------------------------------------------------------------------

DEEP_PATH = "vendor/libdep/vendor/deep"
SUB_DEEP = "DEEP = 1\n"
SUB_DEEP_FUTURE = "DEEP = 999\n"

#: The suite imports from BOTH levels, for the reason this file's own header
#: gives: an assertion that cannot fail for the reason it is written for
#: proves nothing. With `vendor/libdep/vendor/deep` empty this is a collection
#: error in the red-before AND the green-after run, so `result.ok` is a claim
#: about level 2 and not only about level 1.
OLD_TEST_NESTED = """\
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "vendor" / "libdep"))
sys.path.insert(0, str(_repo / "vendor" / "libdep" / "vendor" / "deep"))

from calc import add
from libdep import VALUE
from deepdep import DEEP


def test_both_submodule_levels_are_populated():
    assert VALUE == 1
    assert DEEP == 1


def test_add():
    assert add(1, 1) == 0
"""

NEW_TEST_NESTED = OLD_TEST_NESTED.replace(
    "assert add(1, 1) == 0", "assert add(1, 2) == 3")

MANIFEST_NESTED = """\
task_id: sub-int-004
task_version: 1
repo:
  url: {url}
  base_sha: {base_sha}
prompt: |
  fix add()
tests:
  paths: ["tests/"]
  runner: ["python", "-m", "pytest", "-q"]
  f2p: ["tests/test_calc.py::test_add"]
"""


@pytest.fixture(scope="module")
def nested_superproject(workspace) -> dict:
    """`super4` -> `vendor/libdep` -> `vendor/libdep/vendor/deep`.

    Fresh repositories throughout, never another fixture's: module-scoped
    fixtures in this file run in an unspecified order, and a shared submodule
    mirror would make one test's clone the reason another's prune assertion
    holds.

    THE BUILD ORDER IS FORCED BY THE NESTING and is written out because it has
    to be re-derived otherwise: the innermost repository is committed first
    because the inner's pinned commit has to name it, and the inner's pinned
    commit has to exist before the superproject can pin THAT. Each level
    carries a commit PAST its own gitlink, so the two prune assertions below
    have something to fail to remove.

    Offline: every repository is a local `git init` and every
    submodule-touching command carries `-c protocol.file.allow=always`.
    """
    innermost = workspace / "deepdep4"
    (innermost / "deepdep").mkdir(parents=True)
    (innermost / "deepdep" / "__init__.py").write_text(SUB_DEEP)
    _sh("git", "init", "-q", cwd=innermost)
    _sh("git", "config", "user.email", "t@t.test", cwd=innermost)
    _sh("git", "config", "user.name", "t", cwd=innermost)
    _sh("git", "add", "-A", cwd=innermost)
    _sh("git", "commit", "-q", "-m", "deepdep v1", cwd=innermost)
    deep_pinned = _sh("git", "rev-parse", "HEAD", cwd=innermost)
    (innermost / "deepdep" / "__init__.py").write_text(SUB_DEEP_FUTURE)
    _sh("git", "commit", "-q", "-am", "deepdep FUTURE", cwd=innermost)
    deep_future = _sh("git", "rev-parse", "HEAD", cwd=innermost)

    inner = workspace / "libdep4"
    (inner / "libdep").mkdir(parents=True)
    (inner / "libdep" / "__init__.py").write_text(SUB_LIB)
    _sh("git", "init", "-q", cwd=inner)
    _sh("git", "config", "user.email", "t@t.test", cwd=inner)
    _sh("git", "config", "user.name", "t", cwd=inner)
    _sh("git", "add", "-A", cwd=inner)
    _sh("git", "commit", "-q", "-m", "libdep v1", cwd=inner)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(innermost), "vendor/deep", cwd=inner)
    _sh("git", "-c", "protocol.file.allow=always", "-C", "vendor/deep",
        "checkout", "-q", deep_pinned, cwd=inner)
    _sh("git", "add", "-A", cwd=inner)
    _sh("git", "commit", "-q", "-m", "pin the inner submodule", cwd=inner)
    inner_pinned = _sh("git", "rev-parse", "HEAD", cwd=inner)
    (inner / "libdep" / "__init__.py").write_text(SUB_LIB_FUTURE)
    _sh("git", "commit", "-q", "-am", "libdep FUTURE", cwd=inner)
    inner_future = _sh("git", "rev-parse", "HEAD", cwd=inner)

    repo = workspace / "super4"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(OLD_TEST_NESTED)
    (repo / ".gitignore").write_text("__pycache__/\n")
    _sh("git", "init", "-q", cwd=repo)
    _sh("git", "config", "user.email", "t@t.test", cwd=repo)
    _sh("git", "config", "user.name", "t", cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "base", cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(inner), SUB_PATH, cwd=repo)
    _sh("git", "-c", "protocol.file.allow=always", "-C", SUB_PATH,
        "checkout", "-q", inner_pinned, cwd=repo)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "pin the outer submodule", cwd=repo)
    base = _sh("git", "rev-parse", "HEAD", cwd=repo)

    (repo / "calc.py").write_text(FIXED)
    (repo / "tests" / "test_calc.py").write_text(NEW_TEST_NESTED)
    _sh("git", "add", "-A", cwd=repo)
    _sh("git", "commit", "-q", "-m", "fix", cwd=repo)
    head = _sh("git", "rev-parse", "HEAD", cwd=repo)
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout

    task_dir = workspace / "taskset" / "sub-int-004"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        MANIFEST_NESTED.format(url=str(repo), base_sha=base))
    (task_dir / "reference.diff").write_text(reference)
    return {"task_dir": task_dir, "inner": inner, "innermost": innermost,
            "inner_pinned": inner_pinned, "inner_future": inner_future,
            "deep_pinned": deep_pinned, "deep_future": deep_future}


def test_the_image_and_the_run_tree_carry_the_same_nested_submodule_blob(
        workspace, nested_superproject, local_urls):
    """The two-level end of everything this file compares, and the only place
    a nested tree is materialized, built, gated and diffed against itself."""
    task = load_task(nested_superproject["task_dir"])
    cache = workspace / "cache4"

    run_tree = workspace / "run4"
    start_sha = materialize(task, run_tree, cache)

    base = build_base_images(
        REPO_ROOT, [task_runtime(task)])[task_runtime(task)].image_id
    image = build_task_image(task, base, workspace / "build4", cache)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; RunContainer refuses a tag"
    )

    result = preflight(task, image=image, repo_path=run_tree,
                       start_sha=start_sha)

    # 1. The gate passes. The suite imports from BOTH levels, so an empty
    #    `vendor/libdep/vendor/deep` is a collection error in the red-before
    #    and the green-after run alike.
    assert result.ok, result.problems

    # 2. The gate SAW both levels, at their gitlinks, initialised -- which no
    #    reader in this file could do before item 18: measured 2026-09-02, a
    #    tree with level 1 populated and level 2 empty is CLEAN to `git status
    #    --porcelain`, to the inner's own, and to non-recursive `git submodule
    #    status`.
    assert result.evidence["submodules"] == [
        {"path": SUB_PATH, "sha": nested_superproject["inner_pinned"],
         "initialised": True, "marker": " ",
         "declared_unneeded": False, "empty": False, "depth": 1,
         "url_declared": str(nested_superproject["inner"]),
         "url_persisted": str(nested_superproject["inner"])},
        {"path": DEEP_PATH, "sha": nested_superproject["deep_pinned"],
         "initialised": True, "marker": " ",
         "declared_unneeded": False, "empty": False, "depth": 2,
         "url_declared": str(nested_superproject["innermost"]),
         "url_persisted": str(nested_superproject["innermost"])},
    ]
    assert result.evidence["submodules_orphaned"] == []

    # 3. THE COMPARISON, at level 2. Existence first, on both sides: two
    #    absent files would raise, but an emptiness that made both sides equal
    #    would have PASSED -- the `/var/folders` bind-mount failure this
    #    module's docstring records.
    in_run = run_tree / DEEP_PATH / "deepdep" / "__init__.py"
    in_image = (workspace / "build4" / f"image-{task.task_id}" / "repo"
                / DEEP_PATH / "deepdep" / "__init__.py")
    assert in_run.is_file(), f"{in_run} is missing: the run tree has no level 2"
    assert in_image.is_file(), f"{in_image} is missing: the context has no level 2"
    assert in_run.read_bytes() == in_image.read_bytes()
    assert in_run.read_bytes() == SUB_DEEP.encode()

    # 4. BOTH mirrors pruned, in the artifact the agent gets. One pruned
    #    mirror per `(url, sha)` at every level, so each level's own upstream
    #    commit past its gitlink has to be unreachable -- and the level-1 one
    #    is asserted too, because a recursion that built the inner mirror from
    #    an unpruned outer clone would still pass assertion 5 alone.
    for label, cwd, future in (
        ("level 2", run_tree / DEEP_PATH, nested_superproject["deep_future"]),
        ("level 1", run_tree / SUB_PATH, nested_superproject["inner_future"]),
    ):
        reachable = subprocess.run(
            ["git", "cat-file", "-e", future], cwd=cwd,
            capture_output=True, text=True)
        assert reachable.returncode != 0, (
            f"the run tree's {label} submodule can reach {future}, which is "
            "one commit PAST its gitlink -- that mirror did not prune, or the "
            "clone came from somewhere else"
        )

    # 5. NO HOST CACHE PATH ANYWHERE UNDER `.git`, at either level. Item 10's
    #    `_refuse_host_mirror_path` walks `dest/.git` with `os.walk` and so
    #    covers `.git/modules/<outer NAME>/modules/<inner NAME>` by
    #    construction -- which is asserted here rather than inferred, because
    #    measured 2026-09-02 BOTH module directories leak the path in four
    #    files each (`config`, `logs/HEAD`, `logs/refs/heads/main`,
    #    `logs/refs/remotes/origin/HEAD`) before the guards run, and the
    #    guard pair reaches only the level it runs at.
    needle = str(cache / "repos").encode()
    assert [path for path in (run_tree / ".git").rglob("*")
            if path.is_file() and not path.is_symlink()
            and needle in path.read_bytes()] == []
    # Non-vacuity, per file: the INNER module directory exists and its
    # `logs/HEAD` -- whose expire is the last write to it -- is present at
    # 0 bytes.
    inner_head = (run_tree / ".git" / "modules" / SUB_PATH / "modules"
                  / "vendor/deep" / "logs" / "HEAD")
    assert inner_head.is_file() and inner_head.stat().st_size == 0
