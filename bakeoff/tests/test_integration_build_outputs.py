"""What `image.build` writes into `/repo`, against a real image and a real
run tree.

Round 2, item 14. `images.build_task_image` runs each `image.build` command
against the build-time scaffold -- a `git archive base_sha` context -- and
`tasks.materialize` builds the run tree SEPARATELY, from a `--local` clone of
the pruned mirror, and has never seen anything the build wrote.
`container.RunContainer.__enter__` bind-mounts the run tree over `/repo` in
full, so a file the build generated is visible only during the build and to
no process afterward. Measured against the real probe corpus (2026-09-02):
`sqlglot-6927`'s `setuptools_scm` step writes `sqlglot/_version.py` into the
image and the gate passes green while every import of `sqlglot` in the run
logs an error and `sqlglot.__version__` does not exist. Nothing on the host
alone can see this -- it is a disagreement between an image and a tree built
by two different functions, from two different inputs, at two different
times -- which is why this is an integration module and not a unit one.

MARKERS. `pytestmark` carries BOTH `integration` and `task_image`,
module-wide rather than per test, so a case added later cannot be written
without them. The second marker is what keeps the section 6.6 logger gate
offline: `verify_logger.py` selects `-m "integration and not task_image"`,
and CLAUDE.md pins that gate as offline, no credentials, no spend.

    cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" \\
        tests/test_integration_build_outputs.py --basetemp="$HOME/.cache/bakeoff-pytest"

`--basetemp` UNDER `$HOME` IS MANDATORY, not hygiene -- the same standing
note `test_integration_submodules.py` carries. On macOS the default
`tmp_path` resolves under `/var/folders`, which the Docker VM does not
mount, and a repo bind-mounted from there appears inside the container as a
silently EMPTY `/repo`. `test_the_gate_names_that_file_and_still_passes` runs
the real `preflight` and is exposed to exactly that: an empty run tree would
make `calc_generated.py` look identical to a build that wrote nothing, which
is the false negative this whole module exists to rule out.

The fixture repo is synthetic and local (no submodule -- neither
`local_urls` nor `protocol.file.allow` is needed here, unlike
`test_integration_submodules.py`), for the same reason that file's `upstream`
fixture is local: a test that needs the network is a test that stops
running.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import bakeoff.images as images
from bakeoff.images import build_base_images, build_task_image, image_entrypoint
from bakeoff.preflight import preflight
from bakeoff.tasks import load_task, materialize, task_runtime

pytestmark = [pytest.mark.integration, pytest.mark.task_image]

REPO_ROOT = Path(__file__).resolve().parent.parent

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"

#: The test itself moves between base and fix, mirroring
#: `test_integration_submodules.py`'s OLD_TEST/NEW_TEST -- `split_reference_diff`
#: requires a real hunk under `tests.paths`, which an unchanged test file does
#: not produce (a merged-PR task's start state is base_sha PLUS the committed
#: test half). `test_other` is the p2p member: with only the f2p test declared,
#: deselecting it leaves the p2p sweep nothing to collect, which is its own
#: preflight refusal (`scope_collects_nothing`) unrelated to this file's
#: measurement -- it is NEVER EDITED between base and head, exactly like a
#: real fixture's regression suite.
OLD_TEST_CALC = """\
from calc import add


def test_add():
    assert add(1, 1) == 0


def test_other():
    assert True
"""

NEW_TEST_CALC = OLD_TEST_CALC.replace("assert add(1, 1) == 0", "assert add(1, 1) == 2")

MANIFEST = """\
task_id: build-int-001
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
image:
  # `echo`, not `printf 'GENERATED = 1\\n'`: YAML's DOUBLE-quoted scalar
  # processes `\\n` as a real newline escape at LOAD time, which corrupts the
  # rendered `RUN cd /repo && <command>` Dockerfile line into two ("dockerfile
  # parse error on line 8: unknown instruction", measured) -- `echo` needs no
  # backslash and demonstrates the same class of defect (a file the build
  # writes into /repo that the run tree never sees).
  build: ["echo 'GENERATED = 1' > calc_generated.py"]
"""


def _sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    """Everything Docker has to see, under `--basetemp`. See the module docstring."""
    return tmp_path_factory.mktemp("build-outputs-integration")


@pytest.fixture(scope="module")
def repo(workspace) -> dict:
    """A repo whose `image.build` writes a file `materialize`'s run tree
    never sees. No submodule -- this task needs none."""
    src = workspace / "src"
    (src / "tests").mkdir(parents=True)
    (src / "calc.py").write_text(BUGGY)
    (src / "tests" / "test_calc.py").write_text(OLD_TEST_CALC)
    _sh("git", "init", "-q", cwd=src)
    _sh("git", "config", "user.email", "t@t.test", cwd=src)
    _sh("git", "config", "user.name", "t", cwd=src)
    _sh("git", "add", "-A", cwd=src)
    _sh("git", "commit", "-q", "-m", "base", cwd=src)
    base = _sh("git", "rev-parse", "HEAD", cwd=src)

    (src / "calc.py").write_text(FIXED)
    (src / "tests" / "test_calc.py").write_text(NEW_TEST_CALC)
    _sh("git", "add", "-A", cwd=src)
    _sh("git", "commit", "-q", "-m", "fix", cwd=src)
    head = _sh("git", "rev-parse", "HEAD", cwd=src)
    reference = subprocess.run(
        ["git", "diff", base, head], cwd=src, check=True,
        capture_output=True, text=True,
    ).stdout

    task_dir = workspace / "taskset" / "build-int-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(MANIFEST.format(url=str(src), base_sha=base))
    (task_dir / "reference.diff").write_text(reference)
    return {"task_dir": task_dir}


@pytest.fixture(scope="module")
def built(workspace, repo) -> dict:
    """`materialize` the run tree and `build_task_image` once for both tests
    below -- module-scoped because both read the same artifact."""
    task = load_task(repo["task_dir"])
    cache = workspace / "cache"

    run_tree = workspace / "run"
    start_sha = materialize(task, run_tree, cache)

    base = build_base_images(
        REPO_ROOT, [task_runtime(task)])[task_runtime(task)].image_id
    image = build_task_image(task, base, workspace / "build", cache)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; RunContainer refuses a tag"
    )
    assert not image_entrypoint(image), (
        "the task image declares an ENTRYPOINT; RunContainer's `sleep "
        "infinity` would become an argument to it and the container would "
        "exit immediately"
    )
    return {"task": task, "run_tree": run_tree, "image": image,
            "start_sha": start_sha}


def test_a_file_the_build_writes_into_repo_is_absent_from_the_run_tree(built):
    """The contract itself. `calc_generated.py` exists in the image's own
    `/repo` -- the build ran there -- and does not exist in the run tree,
    which `materialize` built separately and the bind mount replaces `/repo`
    with in full at container start."""
    assert not (built["run_tree"] / "calc_generated.py").exists()
    assert "calc_generated.py" in images._repo_paths_in_image(built["image"])


def test_the_gate_names_that_file_and_still_passes(built):
    """The measurement refuses nothing: the task is a perfectly good one (the
    generated file is not imported by the suite), and the gate records what
    the build wrote while still passing."""
    result = preflight(built["task"], image=built["image"],
                       repo_path=built["run_tree"], start_sha=built["start_sha"])

    assert result.ok, result.problems
    assert result.evidence["build_generated_state"] == "scanned"
    assert result.evidence["build_generated_count"] == 1
    assert "calc_generated.py" in result.evidence["build_generated_paths"]
