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
from bakeoff.preflight import preflight
from bakeoff.tasks import load_task, materialize

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
    base = build_base_images(REPO_ROOT, [task.image.python])[task.image.python]
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
    assert result.evidence["submodules"] == [
        {"path": SUB_PATH, "sha": superproject["pinned"],
         "initialised": True, "marker": " "}
    ]
    assert result.evidence["submodules_orphaned"] == []

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
