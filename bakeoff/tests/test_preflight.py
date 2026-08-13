"""The gate that decides whether a task is a task.

Two halves. The unit half pins the distinction the whole gate rests on --
pytest's exit code 1 ("tests ran and failed") against 2/4/5 ("the
environment is broken") -- because `assert returncode != 0` passes on both,
and that is exactly what let Phase 0c through: `python3 tests/test_calc.py`
raised ModuleNotFoundError with the bug fixed and unfixed alike, and Gemma's
30-of-30 failure was read as capability.

The integration half runs the real thing against a real image, because the
defect it exists to catch lived in an image and nothing on the host could
see it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bakeoff.preflight import (
    EXIT_ALL_PASSED,
    EXIT_TESTS_FAILED,
    failed_node_ids,
    preflight,
)
from bakeoff.tasks import load_task, materialize

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "smoke_task"


# --- the distinction the gate is built on ------------------------------------


def test_a_broken_environment_and_a_present_bug_are_different_exit_codes(tmp_path):
    """Measured against the real runner rather than asserted.

    A repo whose test cannot even import exits 2 (collection interrupted); the
    same repo with the import fixed and the bug present exits 1. Both are
    non-zero. The Phase 0c image produced the first and it was read as the
    second for the entire phase."""
    broken = tmp_path / "broken"
    (broken / "tests").mkdir(parents=True)
    (broken / "tests" / "test_x.py").write_text("import nonexistent_module\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=broken, capture_output=True, text=True,
    )
    assert result.returncode != EXIT_ALL_PASSED
    assert result.returncode != EXIT_TESTS_FAILED, (
        "a collection error must NOT be readable as 'the bug is present'"
    )

    real = tmp_path / "real"
    (real / "tests").mkdir(parents=True)
    (real / "tests" / "test_x.py").write_text("def test_x():\n    assert 1 == 2\n")
    assert subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=real, capture_output=True, text=True,
    ).returncode == EXIT_TESTS_FAILED


def test_a_node_id_that_does_not_exist_is_a_usage_error(tmp_path):
    """So a manifest naming a test that was renamed upstream stops the matrix
    instead of reporting a task that is 'red before' for the wrong reason."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_x.py::test_gone"],
        cwd=repo, capture_output=True, text=True,
    )

    assert result.returncode not in (EXIT_ALL_PASSED, EXIT_TESTS_FAILED)


def test_failed_node_ids_reads_both_failures_and_errors():
    """ERROR lines count. A test whose fixture blows up never runs, and
    treating that as 'not failing' would let a task pass red-before while its
    declared f2p tests were never executed at all."""
    output = (
        "FAILED tests/a.py::test_one[x-y] - AssertionError: nope\n"
        "ERROR tests/b.py::test_two - ValueError: I/O operation on closed file\n"
        "1 failed, 2 passed in 0.1s\n"
    )

    assert failed_node_ids(output) == {
        "tests/a.py::test_one[x-y]",
        "tests/b.py::test_two",
    }


def test_a_non_pytest_runner_is_refused_rather_than_guessed_at(tmp_path):
    """Every verdict here is built on pytest's exit codes. Accepting another
    runner would silently fall back to `returncode != 0`, which is the check
    that let the Phase 0c environment through."""

    class _Task:
        task_id = "t"
        task_version = 1
        manifest_digest = "d"

        class tests:  # noqa: N801 - mirrors the manifest shape
            runner = ("make", "test")
            f2p = ("x",)

    result = preflight(_Task(), image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert not result.ok
    assert "can only distinguish" in result.problems[0]


# --- the real thing ----------------------------------------------------------


def _smoke_task(tmp_path, calc_body: str, reference: str) -> Path:
    """A task built from the smoke fixture, so preflight can be run for real
    without the network or a 20k-line repository."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    for item in FIXTURE.iterdir():
        target = upstream / item.name
        if item.is_dir():
            import shutil

            shutil.copytree(item, target)
        else:
            target.write_bytes(item.read_bytes())
    (upstream / "calc.py").write_text(calc_body)
    # The oracle is added by the test half, so it must not be here already.
    (upstream / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
    )
    for args in (["init", "-q"], ["config", "user.email", "t@t.test"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=upstream, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    task_dir = tmp_path / "set" / "smoke"
    task_dir.mkdir(parents=True)
    (task_dir / "reference.diff").write_text(reference)
    (task_dir / "task.yaml").write_text(
        "task_id: smoke\n"
        "task_version: 1\n"
        "repo:\n"
        f"  url: {upstream}\n"
        f"  base_sha: {base}\n"
        "prompt: |\n"
        "  fix add()\n"
        "tests:\n"
        '  paths: ["tests/"]\n'
        '  runner: ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]\n'
        '  f2p: ["tests/test_calc.py::test_add"]\n'
    )
    return task_dir


def _reference(fix_source: bool) -> str:
    """A reference diff: the oracle half always, the fix half optionally.

    `fix_source=False` builds a task whose reference does NOT fix the bug,
    which is how the green-after check gets exercised for real.
    """
    test_half = (
        "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
        "--- a/tests/test_calc.py\n"
        "+++ b/tests/test_calc.py\n"
        "@@ -1,5 +1,9 @@\n"
        " from calc import add\n"
        " \n"
        " \n"
        " def test_zero():\n"
        "     assert add(0, 0) == 0\n"
        "+\n"
        "+\n"
        "+def test_add():\n"
        "+    assert add(2, 3) == 5\n"
    )
    solution = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        + ("-    return a - b\n+    return a + b\n" if fix_source else
           "-    return a - b\n+    return a - b  # not a fix\n")
    )
    return test_half + solution


@pytest.fixture(scope="module")
def agent_image():
    """The real eval image. Built here rather than assumed present: the whole
    point is that image-level defects are invisible anywhere else."""
    repo_root = Path(__file__).resolve().parent.parent
    subprocess.run(
        ["docker", "build", "-q", "-f", str(repo_root / "docker" / "eval-agent.Dockerfile"),
         "-t", "bakeoff-eval-agent:preflight-test", str(repo_root)],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", "bakeoff-eval-agent:preflight-test"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.integration
def test_a_real_task_passes_every_check(tmp_path, agent_image):
    task = load_task(_smoke_task(tmp_path, "def add(a, b):\n    return a - b\n",
                                 _reference(fix_source=True)))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert result.ok, result.problems
    assert result.evidence["f2p_before_exit"] == EXIT_TESTS_FAILED
    assert result.evidence["f2p_after_exit"] == EXIT_ALL_PASSED
    assert result.evidence["uid"] != "0"


@pytest.mark.integration
def test_a_task_that_is_already_done_is_refused(tmp_path, agent_image):
    """Green before the fix means the arm is scored on work it did not do."""
    task = load_task(_smoke_task(tmp_path, "def add(a, b):\n    return a + b\n",
                                 _reference(fix_source=True)))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert any("already done" in problem for problem in result.problems)


@pytest.mark.integration
def test_a_task_whose_reference_does_not_fix_it_is_refused(tmp_path, agent_image):
    """The other half, and the one that catches a non-editable install: if
    the reference cannot turn the suite green, a solved run and an idle run
    leave identical evidence and no downstream stage can separate them."""
    task = load_task(_smoke_task(tmp_path, "def add(a, b):\n    return a - b\n",
                                 _reference(fix_source=False)))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert any("do NOT pass after the reference" in p for p in result.problems)


@pytest.mark.integration
def test_a_suite_that_dirties_the_tree_is_refused(tmp_path, agent_image):
    """Section 5.6 stages everything, so whatever the suite drops lands in
    every submission diff and diff size measures the interpreter rather than
    the agent. The first live smoke run produced a diff whose first hunk was
    a binary `__pycache__/calc.cpython-312.pyc`.

    Stated as a property -- running the suite leaves the tree clean -- rather
    than as "the repo has a .gitignore", because a .gitignore that does not
    cover what THIS suite drops passes the second check and fails the first.

    The dirt is a file the SUITE writes, not `__pycache__`. It used to be the
    bytecode, and that stopped working when the image set
    PYTHONDONTWRITEBYTECODE=1 to close the stale-pyc defect -- which is the
    right outcome for the image and would have left this property untested. A
    suite that drops a report or a fixture artifact is the more general case
    anyway, and it is the one a real repository actually does.
    """
    task_dir = _smoke_task(tmp_path, "def add(a, b):\n    return a - b\n",
                           _reference(fix_source=True))
    upstream = next(
        line.split(": ", 1)[1].strip()
        for line in (task_dir / "task.yaml").read_text().splitlines()
        if line.strip().startswith("url:")
    )
    # Remove the fixture's .gitignore and give the suite something to drop.
    (Path(upstream) / ".gitignore").unlink()
    (Path(upstream) / "tests" / "test_writes.py").write_text(
        "def test_writes_a_report():\n"
        "    with open('suite-report.txt', 'w') as handle:\n"
        "        handle.write('ok\\n')\n"
    )
    for args in (["add", "-A"], ["commit", "-q", "-m", "no gitignore"]):
        subprocess.run(["git", *args], cwd=upstream, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    manifest = (task_dir / "task.yaml").read_text().splitlines()
    manifest = [
        f"  base_sha: {head}" if line.strip().startswith("base_sha:") else line
        for line in manifest
    ]
    (task_dir / "task.yaml").write_text("\n".join(manifest) + "\n")

    task = load_task(task_dir)
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert any("leaves the tree dirty" in p for p in result.problems)


@pytest.mark.integration
def test_a_task_whose_tests_cannot_even_run_is_refused(tmp_path, agent_image):
    """The Phase 0c failure, reproduced end to end.

    The f2p test here imports a module the image does not have, so pytest
    exits 2 with a collection error. That is non-zero, and `assert
    returncode != 0` reads it as "the bug is present" -- which is exactly
    what happened for the whole of Phase 0c: `python3 tests/test_calc.py`
    raised ModuleNotFoundError with the bug fixed and unfixed alike, Gemma
    burned 30 of 30 turns on it, and the 9/9 was recorded as capability.

    Refusing here costs a message. Accepting it costs a matrix.
    """
    test_half = (
        "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
        "--- a/tests/test_calc.py\n"
        "+++ b/tests/test_calc.py\n"
        "@@ -1,5 +1,10 @@\n"
        " from calc import add\n"
        " \n"
        " \n"
        " def test_zero():\n"
        "     assert add(0, 0) == 0\n"
        "+\n"
        "+import a_module_this_image_does_not_have\n"
        "+\n"
        "+def test_add():\n"
        "+    assert add(2, 3) == 5\n"
    )
    solution = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    task = load_task(
        _smoke_task(tmp_path, "def add(a, b):\n    return a - b\n", test_half + solution)
    )
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems), (
        result.problems
    )


class _Recorder:
    """Captures the argv preflight would run, without a container."""

    def __init__(self):
        self.commands = []

    def exec(self, cmd, env=None):
        self.commands.append(cmd)

        class _R:
            exit_code = 0
            stdout = ""
            stderr = ""

        return _R()


class _Tests:
    runner = ("python", "-m", "pytest", "-q")
    f2p = ("tests/a.py::test_one",)

    def __init__(self, p2p=()):
        self.p2p = p2p


def test_an_undeclared_p2p_set_means_everything_except_f2p():
    """The honest default. An enumerated copy of a pinned suite goes stale
    for no benefit, so the manifest is allowed to leave it empty."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    _Runner(recorder, _Tests().runner, 60).pass_to_pass(_Tests())

    assert recorder.commands[0][-2:] == ["--deselect", "tests/a.py::test_one"]


def test_a_declared_p2p_set_is_actually_used():
    """Otherwise `tests.p2p` is a manifest key that reads like a measurement
    and is inert -- the same defect class as a schema field nothing
    populates. A task needs the explicit form when part of its suite is
    legitimately red at base_sha and cannot serve as a regression check."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    tests = _Tests(p2p=("tests/b.py::test_two",))
    _Runner(recorder, tests.runner, 60).pass_to_pass(tests)

    assert recorder.commands[0][-1] == "tests/b.py::test_two"
    assert "--deselect" not in recorder.commands[0]
