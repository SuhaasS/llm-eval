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

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest

from bakeoff.preflight import (
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_FAILURES,
    EXIT_COLLECTION_INTERRUPTED,
    EXIT_NOTHING_COLLECTED,
    EXIT_TESTS_FAILED,
    EXIT_USAGE_ERROR,
    _gitlink_paths,
    _parse_python_version,
    _parse_submodule_status,
    collection_error_modules,
    f2p_modules,
    failed_node_ids,
    preflight,
)
from bakeoff.tasks import TaskGrading, load_task, materialize

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "smoke_task"


# --- the distinction the gate is built on ------------------------------------


def test_exit_usage_error_is_checked_before_exit_collection_interrupted():
    """The tuple's order is a documented decision nothing else reads yet: 4
    first, because that is the one preflight's own f2p run produces."""
    assert EXIT_COLLECTION_FAILURES == (EXIT_USAGE_ERROR, EXIT_COLLECTION_INTERRUPTED)


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


def test_selecting_a_node_id_whose_module_will_not_import_is_exit_4_not_2(tmp_path):
    """The measurement this whole broadening turns on, taken against the real
    runner rather than asserted.

    `_Runner.select` passes node ids POSITIONALLY, and pytest answers a node
    id whose module raises on import with a USAGE ERROR (4), not with the
    collection-interrupted code (2). 2 is what a module-path or directory run
    gives -- which is how the `trucking-doc-extraction` #3 measurement was
    taken, and why an acceptance written for 2 alone would be dead code on
    every task preflight actually runs.

    Measured 2026-09-01 against pytest 9.1.1 (the venv and the base image pin)
    and pytest 8.3.5 (what `click-3360`'s image.pip pins); both agree.
    """
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "mypkg.py").write_text("def other():\n    return 1\n")
    (repo / "tests" / "test_new.py").write_text(
        "from mypkg import added_symbol\n\n\n"
        "def test_added():\n    assert added_symbol() == 1\n"
    )
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

    selected = subprocess.run(
        [*argv, "tests/test_new.py::test_added"],
        cwd=repo, capture_output=True, text=True,
    )
    whole_module = subprocess.run(
        [*argv, "tests/test_new.py"], cwd=repo, capture_output=True, text=True
    )

    assert selected.returncode == EXIT_USAGE_ERROR
    assert whole_module.returncode == EXIT_COLLECTION_INTERRUPTED
    # And both name the MODULE, with no `::`, in the summary.
    assert collection_error_modules(selected.stdout + selected.stderr) == {
        "tests/test_new.py"
    }


def test_nothing_runs_at_all_when_collection_fails(tmp_path):
    """So "every declared f2p id appears in FAILED/ERROR" is UNSATISFIABLE on
    this shape and has to become a claim about modules.

    A selection spanning a module that will not import and a module that
    imports and fails reports ONLY the collection error -- the failing test
    never runs. A gate that kept the id-level rule would refuse every task of
    this shape while believing it was checking something."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_broken.py").write_text("import nonexistent_module\n")
    (repo / "tests" / "test_red.py").write_text(
        "def test_red():\n    assert 1 == 2\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_broken.py::test_x", "tests/test_red.py::test_red"],
        cwd=repo, capture_output=True, text=True,
    )

    assert result.returncode == EXIT_USAGE_ERROR
    # Measured (row J): the output names only the erroring module, so a
    # direct absence check is exact. A `.split(header)[-1]` form would return
    # the WHOLE output when the header is absent (row K), and assert nothing.
    assert "test_red.py" not in result.stdout + result.stderr


def test_every_erroring_module_is_reported_not_only_the_first(tmp_path):
    """The equality in preflight's acceptance rests on this and nothing else.

    If pytest stopped at the first import failure a two-module f2p set would
    report one module, the equality could never hold, and every such task would
    be refused for a reason that is a property of the reporter rather than of
    the task -- a gate that looks strict and is arbitrary."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    for name in ("test_one.py", "test_two.py"):
        (repo / "tests" / name).write_text("import nonexistent_module\n")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_one.py::test_a", "tests/test_two.py::test_b"],
        cwd=repo, capture_output=True, text=True,
    )

    assert result.returncode == EXIT_USAGE_ERROR
    assert collection_error_modules(result.stdout + result.stderr) == {
        "tests/test_one.py", "tests/test_two.py"
    }


def test_collection_error_modules_separates_a_dead_module_from_a_dead_test():
    """The discriminator is the absent `::`, and it is the whole parser.

    pytest writes `ERROR <module>` for a module that would not import and
    `FAILED <mod>::<test>` / `ERROR <mod>::<test>` for a test that failed or
    whose fixture blew up. Reading a fixture error as "the module did not
    import" would let a task through whose declared tests never ran for a
    reason the gate is supposed to refuse."""
    collection = (
        "ERROR: found no collectors for /repo/tests/new.py::test_added\n"
        "ERROR tests/new.py\n"
        "1 error in 0.01s\n"
    )
    fixture_error = (
        "ERROR tests/new.py::test_added - ValueError: closed file\n"
        "1 error in 0.01s\n"
    )
    mixed = "ERROR tests/new.py\nFAILED tests/other.py::test_x\n"

    assert collection_error_modules(collection) == {"tests/new.py"}
    assert collection_error_modules(fixture_error) is None
    assert collection_error_modules(mixed) is None


def test_a_typoed_node_id_is_not_a_collection_error():
    """The two exit-4 shapes have to stay apart: a manifest naming a test that
    was renamed upstream must keep stopping the matrix, not be accepted as
    "the module could not be collected".

    Measured: both `ERROR: not found:` and `ERROR: file or directory not
    found:` carry a COLON after ERROR, so `_FAILED_LINE` never matches them and
    the reported set is empty."""
    not_found = (
        "ERROR: not found: /repo/tests/a.py::test_gone\n"
        "(no match in any of [<Module a.py>])\n\n\nno tests ran in 0.00s\n"
    )
    no_file = (
        "ERROR: file or directory not found: tests/nope.py::test_x\n\n"
        "no tests ran in 0.00s\n"
    )

    assert collection_error_modules(not_found) is None
    assert collection_error_modules(no_file) is None


def test_f2p_modules_is_the_part_before_the_first_colons():
    """Parametrized ids carry `::` inside brackets on the RIGHT of the split,
    so splitting once from the left is the only correct reading."""
    assert f2p_modules(
        ("tests/a.py::test_one[x::y]", "tests/a.py::Klass::test_two",
         "tests/b.py::test_three")
    ) == {"tests/a.py", "tests/b.py"}


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
            framework = "pytest"

        # Required since preflight reads `task_runtime` -- the ONE source for
        # which base a task needs -- to decide whether the interpreter
        # read-back applies at all (D12). `image` is NOT one of the keys this
        # module reads defensively with `getattr`: those are keys that arrived
        # after some manifest was already written, and `image` has been on
        # `TaskManifest` since the first one, with a default factory, so no
        # real task object can be missing it.
        class image:  # noqa: N801 - mirrors the manifest shape
            python = "3.12"
            node = "22"

        class budget:  # noqa: N801 - mirrors the manifest shape
            suite_timeout_s = 600

    result = preflight(_Task(), image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert not result.ok
    assert "can only distinguish" in result.problems[0]


# --- the real thing ----------------------------------------------------------


def _smoke_task(tmp_path, calc_body: str, reference: str, *,
                extra_files: dict[str, str] | None = None,
                extra_yaml: str = "") -> Path:
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
    for name, body in (extra_files or {}).items():
        target = upstream / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
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
        + (f"{extra_yaml}\n" if extra_yaml else "")
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
    # A repository with no submodules, measured in the real container rather
    # than through the scripted one: `git submodule status` exits 0 with empty
    # stdout, and `git config -f .gitmodules` exits 1 because the file is not
    # there. Both are observations of NONE and both must reach the evidence as
    # `[]` -- the scripted default asserts the same pair, and this is what says
    # the two agree about the tree every ordinary task is in.
    assert result.evidence["submodules"] == []
    assert result.evidence["submodules_orphaned"] == []


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
def test_a_confined_collection_error_with_no_p2p_baseline_is_refused(
    tmp_path, agent_image
):
    """The Phase 0c fixture, and what broadening 2 made of it.

    Its broken import lives INSIDE the declared f2p module, so the error set is
    confined and the exit-code branch no longer owns this input. What refuses
    it now is the second conjunct: this fixture's `tests/` holds only that one
    module, so ignoring it leaves the rootdir sweep with nothing to collect
    (pytest exit 5) and there is no regression baseline at all.

    Kept as an integration test rather than folded into the scripted ones
    because the exit codes it turns on -- 4 from the selection, 5 from the
    ignored sweep -- are the real runner's, in the real image, and that is the
    whole reason this file has an integration half."""
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
        _smoke_task(tmp_path, "def add(a, b):\n    return a - b\n",
                    test_half + solution)
    )
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert result.evidence["f2p_red_kind"] == "collection_error"
    assert result.evidence["p2p_before_ignored"] == ["tests/test_calc.py"]
    assert result.evidence["p2p_before_exit"] == EXIT_NOTHING_COLLECTED
    assert any("could not be collected" in p and "p2p" in p
               for p in result.problems), result.problems
    # And green-after refuses it independently: the dependency is still absent.
    assert result.evidence["f2p_after_exit"] != EXIT_ALL_PASSED


@pytest.mark.integration
def test_a_task_whose_tests_cannot_even_run_is_refused(tmp_path, agent_image):
    """The Phase 0c failure, reproduced end to end -- with a fixture broadening
    2 cannot absorb.

    Same defect as before: a module the image does not have. It lives in
    `tests/conftest.py` rather than in the f2p module, which is what keeps the
    exit-code branch owning this input. Measured 2026-09-01, pytest 9.1.1 and
    8.3.5 alike: a broken conftest under a node-id selection exits 4 and prints
    NO `short test summary info` section at all -- no `ERROR <path>` line -- so
    the reported set is EMPTY, `collection_error_modules` returns `None`, and
    the run is unconfined.

    That is what `assert returncode != 0` reads as "the bug is present", which
    is exactly what happened for the whole of Phase 0c: `python3
    tests/test_calc.py` raised ModuleNotFoundError with the bug fixed and
    unfixed alike, Gemma burned 30 of 30 turns on it, and the 9/9 was recorded
    as capability.

    Refusing here costs a message. Accepting it costs a matrix."""
    task = load_task(_smoke_task(
        tmp_path, "def add(a, b):\n    return a - b\n",
        _reference(fix_source=True),
        extra_files={"tests/conftest.py":
                     "import a_module_this_image_does_not_have\n"},
    ))
    repo = tmp_path / "run" / "repo"
    start = materialize(task, repo, tmp_path / "cache")

    result = preflight(task, image=agent_image, repo_path=repo, start_sha=start)

    assert not result.ok
    assert result.evidence["f2p_red_kind"] == "unknown"
    assert result.evidence["f2p_collection_errors"] == []
    assert result.evidence["p2p_before_ignored"] == []
    assert any("did not run at the start state" in p for p in result.problems), (
        result.problems
    )


class _Recorder:
    """Captures the argv preflight would run, without a container.

    `exits` scripts one exit code per exec, in order, so a test about the
    MERGED exit code of a multi-command check can state what each command
    did. The default -- every command exits 0 -- is what every older test
    here expects. The returned object carries no `duration_ms` on purpose:
    `_Runner`'s merge defaults it, and a stub that grew the field would hide
    that.
    """

    def __init__(self, exits=None):
        self.commands = []
        self.results = []
        self._exits = list(exits or ())

    def exec(self, cmd, env=None):
        self.commands.append(cmd)

        class _R:
            exit_code = self._exits.pop(0) if self._exits else 0
            stdout = ""
            stderr = ""

        result = _R()
        self.results.append(result)
        return result


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


# --- the seams the offline grader rides --------------------------------------


def test_grading_p2p_with_no_extras_is_the_argv_preflight_validated():
    """The whole reason the grader goes through this method rather than
    hand-building the branch: with every keyword argument left at its default
    the argv is byte-identical to the one the gate validated. The moment the
    graded command and the gated command drift apart, the oracle stops
    describing the thing being graded.

    Written against the LITERAL argv rather than against a second call of the
    same method: `pass_to_pass(t) == pass_to_pass(t, extra_deselect=(),
    scope=(), ignore=())` is symmetric and holds no matter what the body emits,
    so it would stay green through an inserted flag or a reordered segment --
    the two changes the property exists to catch. Both branches are spelled out
    because the explicit-p2p branch has its own `*extra` splice.

    `ignore` is the third keyword and the one broadening 2 added. It is used by
    ONE caller (preflight's p2p run at the START state, which the grader never
    makes) and defaults inert everywhere else, which is what keeps this literal
    true for the run the grader does make. It is spliced into `extra`, so it
    reaches BOTH branches -- and on the explicit-`tests.p2p` branch it is a
    NO-OP, because that branch selects node ids and pytest imports only the
    modules those ids name. Spliced there anyway rather than guarded: one
    splice is one thing to keep right, and a guard would be a second place the
    two branches could diverge, for a saving of nothing.
    """
    from bakeoff.preflight import _Runner

    deselect_branch = _Recorder()
    _Runner(deselect_branch, _Tests().runner, 60).pass_to_pass(_Tests())
    assert deselect_branch.commands == [
        ["timeout", "60", "python", "-m", "pytest", "-q",
         "--deselect", "tests/a.py::test_one"]
    ]

    explicit_branch = _Recorder()
    tests = _Tests(p2p=("tests/b.py::test_two",))
    _Runner(explicit_branch, tests.runner, 60).pass_to_pass(tests)
    assert explicit_branch.commands == [
        ["timeout", "60", "python", "-m", "pytest", "-q",
         "tests/b.py::test_two"]
    ]

    # Passing the new keywords empty is the same thing as omitting them --
    # checked against the same literal, never against the other call, so the
    # comparison cannot pass by symmetry.
    supplied = _Recorder()
    _Runner(supplied, _Tests().runner, 60).pass_to_pass(
        _Tests(), extra_deselect=(), scope=(), ignore=()
    )
    assert supplied.commands == deselect_branch.commands


def test_ignore_lands_as_one_flag_per_path_after_the_deselects():
    """Position is pinned, not just presence.

    `--ignore=<path>` is one argument, not a flag and a value: measured
    2026-09-01, pytest 9.1.1 and 8.3.5 both accept `--ignore=tests/x.py` and
    both silently accept a path that does not exist (so a stale entry degrades
    to no effect rather than to the exit-4 usage error the gate would refuse).
    A test that only asserted "the string appears somewhere" would stay green
    through a splice that put it before the runner, where it is not a pytest
    argument at all."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    _Runner(recorder, _Tests().runner, 60).pass_to_pass(
        _Tests(), ignore=("tests/new.py", "tests/other.py")
    )

    assert recorder.commands == [
        ["timeout", "60", "python", "-m", "pytest", "-q",
         "--deselect", "tests/a.py::test_one",
         "--ignore=tests/new.py", "--ignore=tests/other.py"]
    ]


def test_ignore_reaches_the_explicit_p2p_branch_too():
    """One splice point, both branches -- pinned, because "emitted and inert"
    is a claim about the argv and not about the flag's effect. On this branch
    the ignore cannot change what is collected (node ids import only the
    modules they name), so nothing else would notice a guard that dropped it,
    and the two branches would drift with no test going red."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    tests = _Tests(p2p=("tests/b.py::test_two",))
    _Runner(recorder, tests.runner, 60).pass_to_pass(
        tests, ignore=("tests/new.py",)
    )

    assert recorder.commands[0][-1] == "--ignore=tests/new.py"


def test_the_quarantine_rides_as_deselect_flags():
    """The flake quarantine is a set of node ids the grader subtracts from
    check 6, and it has to reach pytest the same way the f2p deselects do --
    a second hand-built copy of the flag loop is a second thing that can be
    wrong about what was actually run."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    _Runner(recorder, _Tests().runner, 60).pass_to_pass(
        _Tests(), extra_deselect=("tests/t.py::flaky",)
    )

    argv = recorder.commands[0]
    assert argv[-2:] == ["--deselect", "tests/t.py::flaky"]
    # after the f2p deselects, not instead of them
    assert argv.index("tests/a.py::test_one") < argv.index("tests/t.py::flaky")


def test_scope_prefixes_lead_the_extra_segment():
    """`scope` is positional collection scoping, so it has to lead the
    arguments pytest is given -- but the argv still opens with the timeout and
    the pinned runner. The explicit-p2p branch ignores it by design: node ids
    already scope that selection."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder()
    runner_argv = _Tests().runner
    _Runner(recorder, runner_argv, 60).pass_to_pass(_Tests(), scope=("tests/",))

    argv = recorder.commands[0]
    head = ["timeout", "60", *runner_argv]
    assert argv[: len(head)] == head
    assert argv[len(head)] == "tests/"

    explicit = _Recorder()
    tests = _Tests(p2p=("tests/b.py::test_two",))
    _Runner(explicit, runner_argv, 60).pass_to_pass(tests, scope=("tests/",))
    assert "tests/" not in explicit.commands[0]


def test_the_gate_and_the_grader_bound_one_manifest_by_one_number(
    monkeypatch, tmp_path
):
    """The invariant this broadening is FOR, asserted across the seam rather
    than on either side of it.

    Preflight and the ladder run the same commands -- the f2p selection, the
    scoped p2p run, each declared `grading.*` argv -- and each wraps them in
    its own `timeout` prefix. Written as two independent reads of the manifest
    (which is what Tasks 2 and 4 are), reverting either one to a constant
    leaves both files' own tests green: preflight would gate at 600 and pass a
    slow task, the ladder would kill the same suite at the manifest's 1234, or
    the reverse. What comes out is `timed_out` -- a GradeFailure, so `resolved:
    False` -- on every arm of that task, permanently, in an append-only store,
    over a number the model never saw.

    Asserted on the ARGVs both sides actually emitted, never on "both read the
    same attribute": a mock that watches the attribute is green through a
    consumer that reads it and then discards it.

    The grader's checks are called directly rather than through `run_ladder`,
    because the ladder's early rungs need a patch, a tree and a `git apply`
    that have nothing to do with this property.
    """
    from bakeoff import grader

    task = _FakeTask(
        budget=_FakeBudget(suite_timeout_s=1234, wall_clock_timeout_s=3600),
        grading=TaskGrading(lint=("ruff", "check", ".")),
    )

    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))
    result = _run_preflight(monkeypatch, tmp_path, task, container)
    assert result.ok, result.problems
    gated = {cmd[1] for cmd in container.commands if cmd[0] == "timeout"}
    assert gated == {"1234"}, container.commands

    graded_commands: list[list[str]] = []

    class _Env:
        def exec(self, argv):
            graded_commands.append(list(argv))
            return _Exec(exit_code=0)

    state = grader._State()
    env = _Env()
    grader._check_command(state, "lint", task, env)
    grader._check_f2p(state, task, env)
    grader._check_p2p(state, task, env, None)

    graded = {cmd[1] for cmd in graded_commands if cmd[0] == "timeout"}
    assert graded == gated
    assert state.suite_timeout_s == 1234


# --- the two new gate assertions ---------------------------------------------


class _Exec:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedContainer:
    """Preflight's container, answered by command shape.

    Every environment check gets the healthy answer by default so a test
    states only the one thing it is about. The suite invocations are the
    interesting ones and are told apart by content rather than by position:
    a run carrying a scope prefix is the scoped assertion, a run carrying
    `--deselect` or an explicit p2p list is a p2p run, and anything else is
    an f2p selection.
    """

    def __init__(self, *, start_sha, tests, present=(), dangling=(),
                 scoped_exit=0, grading_exits=None,
                 f2p_before=None, f2p_after=None, p2p_before=None,
                 env=None, hypothesis_importable=False,
                 hypothesis_in_suite=False, rg_exit=None,
                 python="Python 3.12.13",
                 submodule_status="", submodule_status_exit=0,
                 gitlinks=(), gitmodules_declared=None, gitmodules_exit=None,
                 ls_entries=None, ls_exits=None,
                 reports=None, bare_runner=None):
        self.commands = []
        #: The JSON report each suite invocation writes, keyed by which run it
        #: is: `f2p_before`, `f2p_after`, `p2p_before`, `p2p_after`, `scoped`.
        #: `None` -- the default -- keeps the pytest scripting below EXACTLY
        #: as it was: a pytest adapter's `report_path()` is `None`, so nothing
        #: is deleted, nothing is read back, and every existing test's argv is
        #: byte-identical to what it was.
        #:
        #: The report is handed back the way the real container hands it back:
        #: the run writes it, and `_Runner` reads it with `cat <path>`. There
        #: is no side channel, because the thing under test includes `run`'s
        #: `rm -f`-then-`cat` bracket -- measured, a config error writes NO
        #: file, so a stale report standing in for this run's evidence is the
        #: defect that bracket exists to prevent.
        self.reports = reports
        #: What the LAST suite invocation left at the report path. `None` is a
        #: run that wrote no file at all, which `cat` answers with exit 1.
        self._report_text = None
        self.report_reads = 0
        self.report_removals = 0
        self.start_sha = start_sha
        self.tests = tests
        self.present = set(present)
        self.dangling = set(dangling)
        self.env = dict(env or {})
        self.hypothesis_importable = hypothesis_importable
        self.hypothesis_in_suite = hypothesis_in_suite
        #: An explicit override for `rg`'s exit code, because the boolean
        #: above cannot express the third answer -- "the probe could not
        #: answer" -- which is the one a quiet False would swallow.
        self.rg_exit = rg_exit
        #: What `python --version` answers, verbatim; `None` for an image with
        #: no interpreter on PATH at all.
        self.python = python
        #: `git submodule status`'s stdout, verbatim. Real lines carry the
        #: describe suffix -- ` <sha> <path> (heads/main)` -- because after
        #: `materialize`'s init the submodule HEAD sits on the pruned mirror's
        #: `refs/heads/main`; the parser must survive it without reading it.
        self.submodule_status = submodule_status
        #: Scripted SEPARATELY from the text above, because that pair is what
        #: the gate has to tell apart: a non-zero `git submodule status`
        #: returns EMPTY stdout, which parses to `[]` -- an observation of
        #: "there are none" manufactured out of a failure.
        self.submodule_status_exit = submodule_status_exit
        #: The index's 160000 entries, the AUTHORITATIVE path set. `None` is
        #: an `ls-files` that could not be read at all, which is a different
        #: absence than `()` -- a tree with no gitlinks in it.
        self.gitlinks = gitlinks
        #: `.gitmodules`' declared paths; `None` derives them from `gitlinks`,
        #: which is the tree every other test in this module is already in.
        self.gitmodules_declared = gitmodules_declared
        #: `git config --get-regexp`'s exit code. `None` picks git's own: 0
        #: when something matched, 1 when nothing did. An explicit value is
        #: how a test reaches the >1 branch, where the file exists and could
        #: not be read -- the answer that must not be reported as "no orphans".
        self.gitmodules_exit = gitmodules_exit
        #: What `ls -A -- <path>` reports, keyed by path. An ABSENT key is an
        #: empty directory at exit 0, which is the state every existing test
        #: in this module is in. A value may be a tuple of tuples, in which
        #: case it is consumed BY CALL INDEX on that path -- preflight reads a
        #: declared path twice (before the suite and after it) and the whole
        #: point of the second read is that its answer can differ.
        self.ls_entries = dict(ls_entries or {})
        #: A non-zero exit for `ls -A -- <path>`, keyed by path. The "could
        #: not be read" answer, which must reach the evidence as `None` and
        #: never as the measured claim `False`. A value may be a tuple, read
        #: by call index, for the same reason `ls_entries` may be.
        self.ls_exits = dict(ls_exits or {})
        #: How many times each path has been listed, so the by-call-index
        #: scripting above has something to index on.
        self.ls_calls: dict[str, int] = {}
        self.scoped_exit = scoped_exit
        self.grading_exits = dict(grading_exits or {})
        self.f2p_runs = 0
        self.scoped_runs = 0
        #: Which CHECK the node branch is in the middle of, since one node
        #: check is 1 + K commands. `_f2p_open` is the selection's own flag
        #: (its groups all carry a `-t` and cannot be told from a deselect
        #: continuation by shape alone); `_open` names the deselect-branch
        #: check whose group 0 was last seen. Both are node-only -- the pytest
        #: scripting emits one command per check and never reads them.
        self._f2p_open = False
        self._open = None
        #: The scoped check's own expected argv groups, memoized on first use
        #: (needs `tests.framework`'s adapter, not available yet at
        #: construction). Recognised by MATCHING against this list -- the
        #: same rule the f2p check already uses `select_argvs` for -- rather
        #: than by the `-t`-absent-means-opening heuristic the deselect-branch
        #: p2p checks still use: a `tests.paths` entry may be a FILE that is
        #: also the sole deselected one, which drops the scoped check's group
        #: 0 (`node_adapter.p2p_argvs`'s own comment) and leaves every one of
        #: its commands carrying a `-t` -- the shape the heuristic reads as
        #: "not opening". Matching against the adapter's own groups survives
        #: that, the way asking it for `select_argvs` already does for f2p.
        self._scoped_groups = None
        #: Override the default red-before / green-after / p2p-before answers.
        #: Defaults stay the healthy exit-1 task so a test states only the one
        #: thing it is about.
        self.f2p_before = f2p_before
        self.f2p_after = f2p_after
        self.p2p_before = p2p_before
        self.p2p_runs = 0
        self.p2p_argvs = []
        #: Fix 2's bare-runner probe. `None` -- the default -- answers the
        #: healthy exit 0, since the probe's argv (`--co` present) never
        #: matches `tests.runner` as a prefix and so would otherwise fall
        #: through `_timeout`'s `grading_exits` branch, which can express an
        #: exit code but not the stderr line the problem message is built
        #: from. Scripted separately, like `f2p_before`, for the same reason:
        #: a test about this probe states only the one thing it is about.
        self.bare_runner = bare_runner
        self.bare_runner_calls = 0
        self.bare_runner_argvs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, cmd, env=None):
        self.commands.append(list(cmd))
        if cmd[:2] == ["rm", "-f"]:
            self.report_removals += 1
            self._report_text = None
            return _Exec()
        if cmd[:1] == ["cat"]:
            self.report_reads += 1
            if self._report_text is None:
                # What a config error leaves: no file. `cat` exits 1 and
                # `_Runner._read_report` answers `None`, which the node
                # adapter classifies as an ENVIRONMENT outcome.
                return _Exec(exit_code=1, stderr="No such file or directory\n")
            return _Exec(stdout=self._report_text)
        if cmd[:3] == ["ls", "-A", "--"]:
            path = cmd[3]
            index = self.ls_calls.get(path, 0)
            self.ls_calls[path] = index + 1

            def _at(value, per_call):
                # `per_call` is True when the scripted value is a SEQUENCE of
                # per-call answers. The last element repeats, so a test that
                # scripts one read need not script the other.
                if not per_call:
                    return value
                return value[min(index, len(value) - 1)]

            exit_code = self.ls_exits.get(path, 0)
            exit_code = _at(exit_code, isinstance(exit_code, tuple))
            if exit_code:
                return _Exec(exit_code=exit_code,
                             stderr=f"ls: cannot access '{path}'\n")
            entries = self.ls_entries.get(path, ())
            entries = _at(entries, bool(entries)
                          and isinstance(entries[0], tuple))
            return _Exec(stdout="".join(f"{name}\n" for name in entries))
        if cmd[:2] == ["id", "-u"]:
            return _Exec(stdout="1000\n")
        if cmd[0] == "claude":
            return _Exec(stdout="2.1.220\n")
        if cmd == ["python", "--version"]:
            # Measured 2026-09-01: CPython writes this to STDOUT, not stderr
            # (`docker run ... python --version 2>&1 >/dev/null` is empty).
            # Python 2 wrote it to stderr; 3.4+ does not.
            if self.python is None:
                return _Exec(exit_code=127, stderr="python: not found\n")
            return _Exec(stdout=self.python + "\n")
        if cmd[0] == "sh":
            return _Exec()
        if cmd[:2] == ["test", "-e"]:
            return _Exec(exit_code=0 if cmd[2] in self.present else 1)
        if cmd[:2] == ["test", "-L"]:
            # A dangling symlink: `-e` says absent, `-L` says there is still a
            # path here. Measured 2026-09-01: `[ -e dangling ]` exits 1 and
            # `[ -L dangling ]` exits 0.
            return _Exec(exit_code=0 if cmd[2] in self.dangling else 1)
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
        # The three submodule probes come BEFORE every other `git` branch:
        # the catch-all `cmd[0] == "git"` below would swallow all three and
        # answer each of them exit 0 with empty stdout -- which is the
        # healthy-and-empty answer, so nothing would fail.
        if cmd[:2] == ["git", "submodule"]:
            return _Exec(exit_code=self.submodule_status_exit,
                         stdout=self.submodule_status)
        if cmd[:3] == ["git", "ls-files", "-s"]:
            if self.gitlinks is None:
                return _Exec(exit_code=128,
                             stderr="fatal: not a git repository\n")
            # `-z` emits `<mode> <sha> <stage>\t<path>` NUL-TERMINATED, so the
            # last record is empty. Written out rather than joined, because
            # that trailing empty is what the parser has to survive.
            return _Exec(stdout="".join(
                f"160000 {'a' * 40} 0\t{path}\0" for path in self.gitlinks
            ))
        if cmd[:4] == ["git", "config", "-f", ".gitmodules"]:
            # The `-z` is asserted, not tolerated. Without it git emits
            # `<key> <value>` on one line and a submodule NAME containing a
            # space makes `<key>` unsplittable -- so a scripted container that
            # answered either argv the same way would let the parser regress
            # to the space split with every test still green.
            assert "-z" in cmd, cmd
            declared = (self.gitlinks or ()
                        if self.gitmodules_declared is None
                        else self.gitmodules_declared)
            if self.gitmodules_exit is not None and self.gitmodules_exit > 1:
                return _Exec(exit_code=self.gitmodules_exit,
                             stderr="fatal: bad config line 1\n")
            return _Exec(
                # 1 is git config's ORDINARY "no key matched": no .gitmodules
                # at all, or one whose stanzas all have gitlinks.
                exit_code=(self.gitmodules_exit if self.gitmodules_exit
                           is not None else (0 if declared else 1)),
                # `<key>\n<value>\0` per record, measured against git 2.50.1.
                # The submodule NAME is the path here (what `git submodule
                # add` writes), so a path carrying a space produces a key
                # carrying one -- which is the shape `-z` exists for.
                stdout="".join(
                    f"submodule.{path}.path\n{path}\0" for path in declared
                ),
            )
        if cmd[:2] == ["git", "rev-parse"]:
            return _Exec(stdout=self.start_sha + "\n")
        if cmd[:2] == ["git", "status"]:
            return _Exec(stdout="")
        if cmd[0] == "git":
            return _Exec()
        # Recognised on `--co`, which appears in no OTHER argv this container
        # answers -- not the gated f2p/p2p/scoped runs (their `tests.runner`
        # never carries it) and not a grading argv. Matched BEFORE the
        # generic `timeout` branch below so a test can script this probe's
        # exit code and stderr independently of `grading_exits`, which can
        # only express the former.
        if cmd[0] == "timeout" and "--co" in cmd:
            self.bare_runner_calls += 1
            self.bare_runner_argvs.append(list(cmd))
            return self.bare_runner if self.bare_runner is not None else _Exec()
        if cmd[0] == "timeout":
            return self._timeout(cmd[2:])
        raise AssertionError(f"unscripted exec: {cmd!r}")

    def _timeout(self, argv):
        runner = list(self.tests.runner)
        if argv[: len(runner)] != runner:
            return _Exec(exit_code=self.grading_exits.get(tuple(argv), 0))
        rest = argv[len(runner):]
        if self.reports is not None:
            return self._node_timeout(rest)
        if rest == list(self.tests.f2p):
            self.f2p_runs += 1
            if self.f2p_runs == 1:  # red before the reference fix
                return self.f2p_before or _Exec(
                    exit_code=EXIT_TESTS_FAILED,
                    stdout="".join(f"FAILED {n}\n" for n in self.tests.f2p),
                )
            return self.f2p_after or _Exec()
        if any(arg in self.tests.paths for arg in rest):
            self.scoped_runs += 1
            return _Exec(exit_code=self.scoped_exit)
        self.p2p_runs += 1
        self.p2p_argvs.append(list(rest))
        if self.p2p_runs == 1 and self.p2p_before is not None:
            return self.p2p_before
        return _Exec()

    def _node_timeout(self, rest):
        """The same five suite CHECKS, each of which is now one or more commands.

        A node selection is one command per file and a node deselection is
        1 + K of them, so a fixture keyed on an invocation ORDINAL breaks
        silently: group 1 of the p2p-BEFORE check would increment `p2p_runs`
        to 2 and be served the p2p_after report, and every assertion above
        would still be green over the wrong evidence. Every check's FIRST
        command is served the scripted report; every continuation gets an
        EMPTY one instead, because the adapter's `merge_reports` concatenates
        `testResults` -- re-serving would file the same file twice in
        `files_run` and double every count derived from it (see the `#:`
        comment below). The counters move once per check either way.

        A separate branch rather than a widened one: the pytest scripting
        above is what `test_grading_p2p_with_no_extras_is_the_argv_preflight_
        validated` -- the argv-identity gate on this whole refactor -- runs
        through, and a discriminator rewritten to cover both is a gate
        rewritten to accommodate the change it gates.

        The f2p check is recognised by asking the ADAPTER what a selection of
        the declared ids looks like and matching ANY of its groups, rather
        than by re-deriving the argv here: a second copy of that rule is a
        second thing that can be wrong about which run a report belongs to.
        The scoped check is recognised the same way, against `p2p_argvs`'
        own deselect-branch groups for the full declared scope -- NOT by a
        `-t`-absent-means-opening heuristic, which mis-keys the one shape
        `node_adapter.p2p_argvs` documents: a `tests.paths` entry that is
        also the sole deselected file drops the scoped check's group 0, so
        its only command carries a `-t` and would otherwise be read as a
        continuation of whatever check happened to be open last (or, on a
        framework whose bare file filter happens to equal that same `paths`
        entry, as an unopened "scoped" check served an empty report instead
        of its scripted one). The p2p checks are the one place the `-t`
        heuristic still applies -- they always call `p2p_argvs` with
        `scope=()`, which never omits group 0, so the heuristic's premise
        holds there.
        """
        from bakeoff.runners import for_framework

        adapter = for_framework(self.tests.framework)
        select = adapter.select_argvs(tuple(self.tests.f2p))
        if self._scoped_groups is None:
            self._scoped_groups = adapter.p2p_argvs(
                selected=(), scope=tuple(self.tests.paths),
                deselected=tuple(self.tests.f2p), ignored=())
        scoped_index = next(
            (i for i, group in enumerate(self._scoped_groups)
             if group and rest[:len(group)] == group), None)
        if any(group and rest[:len(group)] == group for group in select):
            # Every group of one selection belongs to the same check; the
            # check opens on the FIRST of them.
            opening = not self._f2p_open
            if opening:
                self.f2p_runs += 1
                self._f2p_open = True
            key = "f2p_before" if self.f2p_runs == 1 else "f2p_after"
        else:
            self._f2p_open = False
            if scoped_index is not None:
                opening = scoped_index == 0
                if opening:
                    self.scoped_runs += 1
                    self._open = "scoped"
                key = "scoped"
            else:
                # Group 0 of a deselect-branch check carries no `-t`; groups
                # 1..K each carry one plus a single file positional and
                # continue it.
                opening = "-t" not in rest
                if not opening and self._open is not None:
                    key = self._open
                else:
                    self.p2p_runs += 1
                    self.p2p_argvs.append(list(rest))
                    key = "p2p_before" if self.p2p_runs == 1 else "p2p_after"
                    self._open = key
        #: The scripted report describes what the check's FIRST command ran.
        #: A continuation group is served an EMPTY report rather than a copy:
        #: the merge CONCATENATES `testResults`, so re-serving would file the
        #: same file twice in `files_run` and double every count derived from
        #: it. Empty is also what these fixtures' continuation groups really
        #: produce -- each names one deselected file whose only test is the
        #: one being deselected, so nothing reaches a terminal status. It is
        #: a dict rather than `None` because `None` is the broken-config
        #: shape and poisons the whole merge, which a test scripting `None`
        #: for the key itself still gets.
        report = self.reports.get(key) if opening else {"testResults": []}
        self._report_text = None if report is None else json.dumps(report)
        # The exit code is DERIVED from the report and is never what a test
        # scripts, because the whole reason this adapter exists is that the
        # number carries no information: measured, both frameworks answer a
        # failing test, an unresolvable import, a syntax error, a nonexistent
        # file argument and a broken config with 1 alike, and a `-t` matching
        # nothing with 0. A test that could set it independently could pin a
        # classification the real runner cannot produce.
        return _Exec(exit_code=_node_exit(report))


@dataclass(frozen=True)
class _FakeTests:
    paths: tuple = ("tests/",)
    runner: tuple = ("python", "-m", "pytest", "-q")
    f2p: tuple = ("tests/a.py::test_one",)
    p2p: tuple = ()
    #: `tests.framework` (broadening 7). Defaulted here for the same reason
    #: `preflight` reads it with `getattr`: the manifest key itself lands in
    #: Task 4, and until it does every task in the set is a pytest one.
    framework: str = "pytest"


@dataclass(frozen=True)
class _FakeImage:
    env: dict = field(default_factory=dict)
    python: str = "3.12"
    #: Both keys carry their default, exactly as `TaskImage` does. A real
    #: manifest may declare only the one belonging to its framework --
    #: `load_task` refuses the other -- but `task_runtime`, which preflight now
    #: reads, indexes whichever one the framework selects, so a stub missing
    #: the node half would raise `AttributeError` out of the gate on every
    #: vitest and jest test in this file.
    node: str = "22"


@dataclass(frozen=True)
class _FakeBudget:
    suite_timeout_s: int = 600
    wall_clock_timeout_s: int = 900
    max_turns: int = 40


@dataclass(frozen=True)
class _FakeTask:
    tests: _FakeTests = field(default_factory=_FakeTests)
    image: _FakeImage = field(default_factory=_FakeImage)
    grading: TaskGrading = field(default_factory=TaskGrading)
    budget: _FakeBudget = field(default_factory=_FakeBudget)
    task_id: str = "t"
    task_version: int = 1
    manifest_digest: str = "d"
    solution_diff: str = "diff --git a/x b/x\n"
    strip_paths: tuple = ()
    submodules_unneeded: tuple = ()


def _run_preflight(monkeypatch, tmp_path, task, container):
    monkeypatch.setattr(
        "bakeoff.preflight.RunContainer",
        lambda **kwargs: container,
    )
    return preflight(task, image="sha256:x", repo_path=tmp_path,
                     start_sha="s" * 40)


#: What pytest prints, under the pinned `-q -p no:cacheprovider`, when the
#: module holding a selected node id raises on import. Measured 2026-09-01,
#: pytest 9.1.1 and 8.3.5 identically -- the `ERROR:` lines carry a colon and
#: are not node ids; the summary line names the MODULE with no `::`.
_COLLECTION_ERROR_OUT = (
    "ERROR: found no collectors for /repo/tests/a.py::test_one\n"
    "\n"
    "==================================== ERRORS ===================================\n"
    "________________________ ERROR collecting tests/a.py __________________________\n"
    "ImportError while importing test module '/repo/tests/a.py'.\n"
    "tests/a.py:1: in <module>\n"
    "    from app import added_symbol\n"
    "E   ImportError: cannot import name 'added_symbol' from 'app'\n"
    "=========================== short test summary info ===========================\n"
    "ERROR tests/a.py\n"
    "1 error in 0.01s\n"
)


@pytest.mark.parametrize("exit_code,f2p_entry", [
    # 4: `_Runner.select` passes node ids positionally, and pytest answers a
    # selected node id whose module will not import with a usage error --
    # this is the shape preflight's f2p run actually makes (row A).
    (4, "tests/a.py::test_one"),
    # 2: unpinned before this task -- reachable when an author declares a
    # BARE MODULE as an f2p entry, so `_Runner.select` collects the module
    # path rather than a node id and gets the collection-interrupted exit a
    # directory/module-path run gives (row B). `f2p_modules` splits on "::"
    # regardless, so the confinement equality holds identically either way.
    (2, "tests/a.py"),
])
def test_an_f2p_module_that_will_not_import_is_accepted_when_p2p_is_green(
    monkeypatch, tmp_path, exit_code, f2p_entry
):
    """The broadening. A PR that ADDS a symbol puts it in the solution half, so
    the test half cannot import at the start state and pytest exits 4 -- the
    code that also means "a broken image".

    Acceptance is a THREE-way conjunction and never a parse: the errors are
    confined to the declared f2p modules, p2p is green before, AND f2p is green
    after. The f2p selection imports ONLY the f2p modules, so a dependency that
    only that module needs is confined AND leaves p2p green -- the first two
    conjuncts cannot see it and green-after is what refuses it (see
    `test_a_collection_error_after_the_reference_fix_is_still_a_refusal`).
    This container scripts all three healthy, which is what makes it a GO.
    """
    task = _FakeTask(tests=_FakeTests(f2p=(f2p_entry,)))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(exit_code=exit_code, stdout=_COLLECTION_ERROR_OUT),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["f2p_before_exit"] == exit_code
    assert result.evidence["f2p_red_kind"] == "collection_error"
    assert result.evidence["f2p_collection_errors"] == ["tests/a.py"]
    assert result.evidence["p2p_before_ignored"] == ["tests/a.py"]
    # The ignore reached the run that needed it, and only that run.
    assert "--ignore=tests/a.py" in container.p2p_argvs[0]
    assert all("--ignore=tests/a.py" not in argv
               for argv in container.p2p_argvs[1:])


def test_an_ordinary_red_task_records_the_other_red_kind(monkeypatch, tmp_path):
    """Absence is recorded, never implied. "This task has no collection errors"
    and "the gate did not look" render identically as a missing key, so both
    keys are written on every path -- which is also what lets a reader of a
    cached verdict tell the two task shapes apart."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["f2p_red_kind"] == "failed"
    assert result.evidence["f2p_collection_errors"] == []
    assert result.evidence["p2p_before_ignored"] == []
    assert all("--ignore" not in " ".join(argv) for argv in container.p2p_argvs)


def test_a_declared_f2p_id_failing_only_through_subtest_still_gates_go(
    monkeypatch, tmp_path
):
    """Fix 3's regression. sqlglot's `validate_all` wraps each assertion in
    `unittest.subTest`, and pytest 9's core-integrated subtests report a
    subtest-only failure as `SUBFAILED(label) <id> - <msg>` rather than
    `FAILED <id>` -- measured 2026-09-02. Under `PREFLIGHT_VERSION` 11 that
    line matched neither `FAILED` nor `ERROR`, `failed_node_ids` came back
    empty on a run whose exit code was 1, and `missing = set(tests.f2p) -
    set(red_outcome.failed_ids)` read the declared id as never having failed
    -- a NO-GO on a task the start state genuinely proves red (measured in
    `w1-sqlglot-6927.md`: `f2p_before_exit: 1, f2p_red_kind: "failed"` beside
    a refusal reading "declared f2p tests did not fail at the start state").
    A fake runner that reports exit 1 with ONLY SUBFAILED lines for the
    declared f2p id must now pass the before-check."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(
            exit_code=EXIT_TESTS_FAILED,
            stdout=(
                "SUBFAILED(i=0) tests/a.py::test_one - AssertionError: 0 != 1\n"
                "SUBFAILED(i=2) tests/a.py::test_one - AssertionError: 2 != 1\n"
                "2 failed, 1 passed, 2 subtests passed in 0.01s\n"
            ),
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["f2p_red_kind"] == "failed"


def test_a_collection_error_with_a_red_p2p_is_still_refused(monkeypatch, tmp_path):
    """The second conjunct, and what it does and does not rule out.

    p2p-green-before rules out a GLOBALLY broken image and establishes the
    regression baseline -- a p2p check against an already-red suite means
    nothing. It does NOT rule out a dependency imported only by the f2p module:
    that one is confined AND leaves p2p green, and green-after is what catches
    it. Naming the wrong conjunct here is how the first draft of this plan
    talked itself into a two-way guarantee.

    Note what this test does NOT assert: that the task is a NO-GO. It would be
    one either way, because the `if not p2p_green:` block below is
    unconditional. What the branch buys is a refusal that NAMES the coupling
    and prints the argv it ran, instead of an author on a collection-error task
    reading a bare "p2p is not green" with no way to know the two are related
    -- which is why the assertion is on the message and why the MUTATIONS entry
    for this broadening anchors on the equality instead."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
        p2p_before=_Exec(exit_code=EXIT_TESTS_FAILED,
                         stdout="FAILED tests/z.py::test_other\n"),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("could not be collected" in p and "p2p" in p
               for p in result.problems), result.problems
    assert result.evidence["f2p_red_kind"] == "collection_error"


def test_a_collection_error_outside_the_declared_f2p_modules_is_refused(
    monkeypatch, tmp_path
):
    """Equality, not containment, and in both directions.

    A module erroring that no f2p id names is a broken environment. A declared
    f2p module that did NOT error is a declared id nobody checked -- and
    measured, a partial collection error hides the rest of the selection
    entirely, so an id-level rule is unsatisfiable and a subset rule would
    silently accept the gap the id-level rule existed to close."""
    tests = _FakeTests(f2p=("tests/a.py::test_one", "tests/b.py::test_two"))
    task = _FakeTask(tests=tests)

    stranger = _ScriptedContainer(
        start_sha="s" * 40, tests=tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout="ERROR tests/zzz.py\n"),
    )
    result = _run_preflight(monkeypatch, tmp_path, task, stranger)
    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems)

    partial = _ScriptedContainer(
        start_sha="s" * 40, tests=tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout="ERROR tests/a.py\n"),
    )
    result = _run_preflight(monkeypatch, tmp_path, task, partial)
    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems)


def test_a_typoed_f2p_id_still_stops_the_matrix(monkeypatch, tmp_path):
    """Same exit code, opposite verdict. `ERROR: not found:` carries a colon,
    so nothing is reported and the confinement predicate refuses -- which is
    what keeps a manifest naming a renamed test from being read as a task
    shape to accept."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(
            exit_code=4,
            stdout="ERROR: not found: /repo/tests/a.py::test_one\n"
                   "(no match in any of [<Module a.py>])\n\nno tests ran\n",
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("did not run at the start state" in p for p in result.problems)


def test_a_collection_error_after_the_reference_fix_is_still_a_refusal(
    monkeypatch, tmp_path
):
    """The third conjunct, and the only one that can see this defect.

    Green-after is NOT relaxed, and the symmetry that suggests relaxing it is
    the trap. Red-before accepts a confined collection error because the
    missing symbol IS the bug; after the reference fix that symbol exists, so
    the module imports and the tests exit 0. A reference that leaves it
    unimportable has fixed nothing, and a solved run and an idle run would
    leave identical evidence -- the failure `preflight`'s module docstring
    opens with.

    It is also the ENVIRONMENT DISCRIMINATOR, which is the part that is easy to
    miss. A dependency imported only by the f2p module is confined under
    conjunct (i) -- the f2p selection imports only those modules -- and leaves
    p2p green under conjunct (ii), because p2p never imports it. Neither of the
    first two conjuncts can refuse it. This scripted shape is that defect
    exactly: the error set is perfectly confined and p2p is green, so nothing
    but this block keeps the refusal."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        f2p_before=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
        f2p_after=_Exec(exit_code=4, stdout=_COLLECTION_ERROR_OUT),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("do NOT pass after the reference" in p for p in result.problems)
    assert result.evidence["f2p_after_exit"] == 4
    # The environment discriminator: both of the earlier conjuncts held, so
    # nothing but green-after is what refuses this task.
    assert result.evidence["f2p_red_kind"] == "collection_error"
    assert result.evidence["p2p_before_exit"] == EXIT_ALL_PASSED


def test_a_scope_that_collects_nothing_is_a_named_problem_code(monkeypatch, tmp_path):
    """Exit 5 is the whole detection. The pinned runner carries `-q`, which
    suppresses the "collected 0 items" summary line (measured), so a guard
    that matched that string could not fire under the configuration actually
    used. The driver branches on the code, not on a prose prefix."""
    from bakeoff.preflight import SCOPE_COLLECTS_NOTHING

    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), scoped_exit=5)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert SCOPE_COLLECTS_NOTHING in result.problem_codes
    assert not result.ok
    assert result.evidence["p2p_scoped_after_exit"] == 5

    # The other route to the same code: nothing declared survives the filter,
    # so there is no scoped run to exit 5 in the first place.
    empty = _ScriptedContainer(start_sha="s" * 40, tests=task.tests, present=())
    result = _run_preflight(monkeypatch, tmp_path, task, empty)
    assert SCOPE_COLLECTS_NOTHING in result.problem_codes
    assert empty.scoped_runs == 0


def test_a_declared_prefix_missing_at_the_postfix_state_is_noted_not_fatal(
    monkeypatch, tmp_path
):
    """`ok` is `not problems`, so appending a problem here would re-arm the
    NO-GO the filter exists to prevent -- on exactly the input the grader's
    restore step already tolerates. The author error stays loud through the
    typed channel and the evidence, and stays non-fatal."""
    from bakeoff.preflight import SCOPE_PREFIX_MISSING

    tests = _FakeTests(paths=("tests/", "docs/tests/"))
    task = _FakeTask(tests=tests)
    container = _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert SCOPE_PREFIX_MISSING in result.problem_codes
    assert result.evidence["scope_prefixes_absent"] == ["docs/tests/"]
    assert result.evidence["scope_prefixes"] == ["tests/"]
    assert not any("docs/tests/" in problem for problem in result.problems)


def test_the_scoped_assertion_is_skipped_on_an_explicit_p2p_list(monkeypatch, tmp_path):
    """`pass_to_pass` ignores `scope` on that branch, so running the
    assertion there would pay a full suite invocation to re-assert a
    selection preflight already validated."""
    tests = _FakeTests(p2p=("tests/b.py::test_two",))
    task = _FakeTask(tests=tests)
    container = _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert container.scoped_runs == 0
    assert "p2p_scoped_after_exit" not in result.evidence
    assert result.problem_codes == ()


def test_preflight_fails_a_broken_grading_declaration(monkeypatch, tmp_path):
    """A typo'd `typecheck:` against an image without mypy is otherwise
    stamped as `typecheck_failed` on every record of the task, permanently,
    instead of stopping the matrix before anything is spent."""
    task = _FakeTask(grading=TaskGrading(typecheck=("mypy", "src")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        grading_exits={("mypy", "src"): 127},
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("typecheck" in p and "127" in p for p in result.problems)
    assert result.evidence["grading_typecheck_exit"] == 127


def test_the_grading_argvs_run_before_the_scoped_p2p_like_the_ladder_does(
    monkeypatch, tmp_path
):
    """The grader's ladder runs build/typecheck (checks 3-4) before p2p
    (check 6). Preflight has to agree, or the scoped run it validates is
    measured on a tree the grading commands have not touched -- and a build
    artifact or a mypy cache is exactly the kind of thing that changes what
    the next collection sees. Pinned as an order rather than left to the
    comment, because nothing else makes the two ladders stay in step."""
    task = _FakeTask(grading=TaskGrading(build=("make", "build")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    grading_at = next(i for i, cmd in enumerate(container.commands)
                      if cmd[2:] == ["make", "build"])
    scoped_at = next(i for i, cmd in enumerate(container.commands)
                     if "tests/" in cmd and cmd[0] == "timeout")
    assert grading_at < scoped_at


def test_a_healthy_task_declares_the_gate_that_produced_its_verdict(
    monkeypatch, tmp_path
):
    """A cached verdict outlives the gate that wrote it, so it has to say
    which gate that was."""
    from bakeoff.preflight import PREFLIGHT_VERSION

    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.preflight_version == PREFLIGHT_VERSION
    assert result.to_dict()["preflight_version"] == PREFLIGHT_VERSION
    assert result.to_dict()["problem_codes"] == []


def test_the_gate_bounds_every_command_by_the_manifests_number(
    monkeypatch, tmp_path
):
    """Not `timeout_s=`. A defaulted parameter beside a manifest key is two
    sources for one number, and a driver left on the default is the silent
    divergence this whole broadening exists to close -- a suite that fits the
    gate's bound and is killed under the grader's stamps `timed_out`, which is
    `resolved: False`, an accusation against the model, over a number it never
    saw.

    EVERY command, not just the suite: the declared grading.* argvs go through
    the same prefix, because the grader runs those too and a second key for
    them would be a second thing that can diverge. The bare-runner probe
    (fix 2) is one more: it is a `container.exec` like any other, and a bare
    collect-only run that hung with no bound would hang the gate exactly as
    an unbounded suite run would."""
    task = _FakeTask(
        budget=_FakeBudget(suite_timeout_s=1234, wall_clock_timeout_s=3600),
        grading=TaskGrading(lint=("ruff", "check", ".")),
    )
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    bounded = [cmd for cmd in container.commands if cmd[0] == "timeout"]
    # five suite runs on the deselect branch + one declared grading argv
    # + one bare-runner probe (fix 2)
    assert len(bounded) == 7, bounded
    assert {cmd[1] for cmd in bounded} == {"1234"}


def test_the_evidence_records_the_bound_off_the_argv_not_off_the_manifest(
    monkeypatch, tmp_path
):
    """`last_argv` exists so a problem can name what was actually run rather
    than a reconstruction of it, and the same reasoning covers this: reading
    the manifest a second time is a second thing that can be right about what
    was ASKED for while the argv carried something else. Configuration is
    never reported as observation."""
    task = _FakeTask(budget=_FakeBudget(suite_timeout_s=1234,
                                        wall_clock_timeout_s=3600))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["suite_timeout_s"] == 1234


def test_no_bound_is_recorded_when_no_command_ever_ran(monkeypatch, tmp_path):
    """The non-pytest `tests.runner` refusal returns before the container is
    even entered. An absent key is honest there -- no command ran under any
    bound -- and a manifest value written anyway would be a claim about a run
    that did not happen."""
    task = _FakeTask(tests=_FakeTests(runner=("go", "test", "./...")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert "suite_timeout_s" not in result.evidence


def test_last_timeout_s_is_none_before_any_invocation():
    """`None` is "nobody bounded this", which is a different claim from any
    number -- the same distinction `wire_unattributed` and `cache_state.warm`
    make. A `0` or a fallback to the constructor argument would make "the
    argv carried no timeout prefix" unrepresentable.

    The argument is now a SEQUENCE of argvs, and one empty argv is still one
    invocation -- `[]` means no groups at all, which `run` refuses."""
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest", "-q"), 1234)
    assert runner.last_timeout_s is None
    runner.run([[]])
    assert runner.last_timeout_s == 1234


def test_preflight_takes_no_timeout_parameter():
    """The parameter is DELETED, not defaulted. With it gone, "a consumer left
    on the constant" is unrepresentable rather than merely tested for -- which
    is why neither driver needed a line changed."""
    import inspect

    from bakeoff.preflight import preflight

    assert "timeout_s" not in inspect.signature(preflight).parameters


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


# --- the environment, read back out of the container --------------------------


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
                                   present=("tests/",),
                                   env={})  # nothing set in the container

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("CI" in problem and "image.env" in problem
               for problem in result.problems)
    assert result.evidence["image_env_mismatch"] == ["CI"]


def test_a_declared_env_that_matches_is_not_a_problem(monkeypatch, tmp_path):
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), env={"CI": "1"})

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
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

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
                                   present=("tests/",), env={"CI": ""})

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    # "does not match the manifest's image.env" is unique to the mismatch
    # message -- plain "image.env" also appears in the hypothesis NO-GO's
    # remedy text ("Add `image.env: {CI: ...`"), so filtering on that alone
    # would pass even if this scenario silently grew a second, unrelated
    # problem.
    assert result.problems == tuple(
        p for p in result.problems if "does not match the manifest's image.env" in p
    )  # the mismatch is the ONLY reason this is a NO-GO
    assert result.evidence["image_env_observed"] == {"CI": ""}

    absent = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                present=("tests/",), env={})
    result = _run_preflight(monkeypatch, tmp_path, task, absent)

    assert result.evidence["image_env_observed"] == {"CI": None}


def test_the_env_is_read_before_the_suite_runs(monkeypatch, tmp_path):
    """Order, not presence. A broken environment produces five downstream
    suite failures, and reporting them instead of the cause sends the task
    author round the loop for the wrong reason.

    `"--co" not in cmd` excludes the bare-runner probe (fix 2): it is ALSO a
    `timeout`-prefixed command, and it runs earlier still (beside the
    context-file probe), so the naive first `timeout` call is now this
    probe's own rather than the suite's -- this test's claim is about the
    suite specifically, so it must look past it."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={"CI": "1"})

    _run_preflight(monkeypatch, tmp_path, task, container)

    printenv = next(i for i, cmd in enumerate(container.commands)
                    if cmd[:1] == ["printenv"])
    first_suite = next(i for i, cmd in enumerate(container.commands)
                       if cmd[:1] == ["timeout"] and "--co" not in cmd)

    assert printenv < first_suite


def test_the_hypothesis_import_probe_runs_before_the_suite(monkeypatch,
                                                            tmp_path):
    """Same ordering claim as the env read-back, for the other reason a bad
    environment must be reported as itself: a task whose author never
    declared CI is a NO-GO, and that has to be decided before five downstream
    suite failures bury the cause.

    `"--co" not in cmd` excludes the bare-runner probe (fix 2), the same
    reason `test_the_env_is_read_before_the_suite_runs` needs it."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={"CI": "1"}, hypothesis_importable=True)

    _run_preflight(monkeypatch, tmp_path, task, container)

    import_probe = next(
        i for i, cmd in enumerate(container.commands)
        if len(cmd) >= 3 and cmd[1] == "-c" and "import hypothesis" in cmd[2]
    )
    first_suite = next(i for i, cmd in enumerate(container.commands)
                       if cmd[:1] == ["timeout"] and "--co" not in cmd)

    assert import_probe < first_suite


def test_the_rg_probe_runs_before_the_suite(monkeypatch, tmp_path):
    """Same ordering claim, for the probe that decides whether an undeclared
    property-based suite is a NO-GO. `"--co" not in cmd` excludes the
    bare-runner probe (fix 2), the same reason the two tests above need it."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), env={"CI": "1"},
                                   hypothesis_importable=True,
                                   hypothesis_in_suite=True)

    _run_preflight(monkeypatch, tmp_path, task, container)

    rg_probe = next(i for i, cmd in enumerate(container.commands)
                    if cmd[:1] == ["rg"])
    first_suite = next(i for i, cmd in enumerate(container.commands)
                       if cmd[:1] == ["timeout"] and "--co" not in cmd)

    assert rg_probe < first_suite


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
    that render identically as `false` are the same defect one layer down.

    And the ambiguity is not swallowed. `scanned` is non-empty here -- the
    probe RAN and could not answer, which is a controller ruling and not the
    "never ran" shape `test_no_declared_test_path_exists_so_the_probe_never_ran`
    covers -- so it must become a NO-GO naming the exit code and the argv,
    not a silent None that lets the task through on an environment defect
    preflight could not see through."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), rg_exit=2)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert result.evidence["hypothesis_imported_by_suite"] is None
    assert any("rg" in problem and "exited 2" in problem
               for problem in result.problems)


def test_no_declared_test_path_exists_so_the_probe_never_ran(monkeypatch,
                                                             tmp_path):
    """`_present` filters the prefixes, for `_existing_prefixes`' reason: a
    declared path absent at the start state is an input this gate tolerates,
    and handing rg a path that does not exist makes it exit 2. With nothing to
    scan the answer is None -- not False.

    And it is a QUIET None, unlike the controller-ruling ambiguity in
    `test_an_rg_probe_that_could_not_answer_is_None_and_not_False`: no exec
    ran, so there is nothing to be a problem about."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=(),  # tests/ is not there
                                   hypothesis_importable=True)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["hypothesis_imported_by_suite"] is None
    assert not any(cmd[:1] == ["rg"] for cmd in container.commands)
    assert not any("rg" in problem or "hypothesis-import scan" in problem
                   for problem in result.problems)


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
    assert result.evidence["hypothesis_imported_by_suite"] is None


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
                                   present=("tests/",), python="Python 3.13.15")

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("'3.1'" in p and "3.13.15" in p for p in result.problems)


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
                                   present=("tests/",), python="Python 3.11.16")

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
                                   present=("tests/",), python="Python 3.13.15")

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
                                   present=("tests/",), python="Python 3.11.99")

    assert _run_preflight(monkeypatch, tmp_path, task, container).ok


def test_an_image_with_no_python_at_all_is_a_named_problem(monkeypatch, tmp_path):
    """The container started and the probe ran -- it exited 127. That is an
    OBSERVED empty answer, not the unobserved absence the pre-container path
    leaves behind, so it is recorded as "" rather than reusing that None."""
    task = _FakeTask(image=_FakeImage(python="3.12"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python=None)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any("python --version" in p for p in result.problems)
    assert result.evidence["python_observed"] == ""


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


def test_the_preflight_version_moved_with_the_new_assertion():
    """It is in the cache key, and it is the only component that moves when
    THIS file changes -- a manifest digest describes the task, an image id the
    environment, a start sha the tree. Without the bump every warm cache
    serves a verdict written by an older gate. 4 -> 5 was the gate starting
    to look at image.env; 5 -> 6 is the gate's bound becoming
    budget.suite_timeout_s -- a manifest already carrying the key loads under
    the older loader, so manifest_digest does not move and a warm cache would
    serve a verdict gated at 600; 6 -> 7 is the image.python read-back, and a
    verdict cached under 6 was written by a gate that never asked which
    interpreter the container runs; 7 -> 8 is the submodule read-back, and a
    verdict cached under 7 was written by a gate that never asked whether the
    tree's submodules are populated -- which `git status --porcelain` reports
    as CLEAN when they are not; 8 -> 9 is a round that LOOKS additive and is
    not, which is the case worth spelling out. It changes what 8's own
    submodule assertions assert: the status-to-gitlink match became
    boundary-anchored (a tree with `vendor/lib` beside `vendor/libdep` could
    file an entry under the wrong path and flip GO/NO-GO under the old
    `startswith`), an index gitlink that no `git submodule status` line names
    is now a problem rather than a silently shorter list, and the orphan read
    moved to `--get-regexp -z` so a submodule name containing a space stops
    being reported as an orphan under a path that is not a path.

    9 -> 10 is the runner adapter (broadening 7). A verdict cached under 9 was
    written by a gate whose every red/green branch read a pytest EXIT CODE,
    which classifies a vitest or jest run by rules those frameworks do not
    follow, and which made none of the three node assertions: that every
    declared f2p id actually RAN, that the scoped p2p run stayed inside
    tests.paths, and that the framework's cache flags are in tests.runner. It
    is the first commit of that broadening to change what the gate ASSERTS --
    the ones before it are a refactor whose argv is byte-identical on both
    branches -- so it is the first that must invalidate a cache, and it
    carries `evidence["framework"]` with it.

    10 -> 11 is that broadening's own final review, and it moves for the same
    reason 9 -> 10 did while changing no verdict at all. Three evidence keys
    changed what an ABSENCE means: `duplicate_full_names` and
    `scope_files_outside` went from `[]` to `None` for a scoped run that wrote
    no report, and `f2p_before_not_run` joined them -- it was `[]` on the
    runner-gate early return, which starts no container, and on a node f2p run
    that produced no report, so it claimed a measurement it never made. A blob
    cached before and one written after would otherwise share a version string
    with `[]` meaning two different things in them, which is the one thing
    this number exists to let a reader rule out.

    12 -> 13 is the bare-runner probe (fix 2). A verdict cached under 12 was
    written by a gate that never asked whether the repo's OWN pytest
    configuration -- its addopts, read with none of `tests.runner`'s extra
    arguments -- is itself a usage error in this image: `bidict-389-putall-
    rollback-clean`'s gated runner bypasses that with
    `--override-ini=addopts=` and passes, while the bare command an agent
    naturally types exits 4 from turn one, bug fixed and unfixed alike.

    13 -> 14 is not a new assertion. It retires cached PASS verdicts observed
    through a container this code can no longer interrogate:
    `preflight-tree/<task_id>` was one path per task, reused across
    invocations, and the Docker VM served the second container an empty
    `/repo` -- which on a vitest task is `No test files found` at exit 1, the
    shape a PASS cannot be told apart from (round 2 item 3, 2026-09-03). Both
    caches key through `preflight_cache_key`, so `preflight.json` and
    `preflight-grade.json` invalidate together.

    14 -> 15 is the node per-file selection (round 2 item 1, 2026-09-03), and
    it moves a cached verdict in BOTH directions. A v14 NO-GO for a cross-file
    duplicate `fullName` is stale, because that refusal is gone; a v14 PASS on
    a VITEST task whose executed files include one path contained in another's
    is stale the other way, because `ambiguous_file_filters` is a new refusal.
    Between them, the jest positional gained its mount anchor and its escape
    and jest stopped emitting `--testPathIgnorePatterns` at all. And
    `duplicate_full_names`' CONTENT grows for an unchanged task -- group 0 now
    runs the f2p files' siblings unfiltered where v14's global deselection
    skipped them -- so a reader diffing two cached verdicts across this bump
    must not read that growth as a regression.

    15 -> 16 is `submodules_unneeded` (round 2 item 2, 2026-09-02), and the
    EVIDENCE SHAPE is the reason rather than the new assertion. Every
    `submodules` entry gained `declared_unneeded` and `empty`, on every task
    whether or not it declares the key, and a new top-level
    `submodules_empty_after_suite` joined `submodules` and
    `submodules_orphaned`. Those manifests' digests do not move, so a warm v15
    blob and a fresh v16 blob would otherwise sit in one cache describing
    different shapes. The narrower residual comes second: a v15 NO-GO on a
    manifest that already declared the key -- reachable only because
    `load_task` has no top-level unknown-key refusal -- would otherwise be
    served forever."""
    from bakeoff.preflight import PREFLIGHT_VERSION

    assert PREFLIGHT_VERSION == "16"


# --- fix 2: the bare-runner probe ---------------------------------------------


def test_a_bare_usage_error_names_the_plugin_and_the_flag(monkeypatch, tmp_path):
    """bidict-389's measured shape: the gated runner's --override-ini=addopts=
    throws away the repo's own pytest-xdist requirement (--numprocesses=auto
    in pyproject.toml), and the command an agent naturally types exits 4 from
    turn one, bug fixed and unfixed alike. The problem must name both the
    addopts flag pytest reported and the plugin `image.pip` is missing, and
    the evidence must carry the exit code and the argv that produced it.

    stderr is the REAL two-line shape (docker run against the actual task
    image, 2026-09-02): argparse's `error()` prints the usage banner as line
    1 and the flag it choked on as line 2, so a lookup keyed on only the
    first line -- which is what the message quotes -- would find nothing on
    this exact task."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        bare_runner=_Exec(
            exit_code=EXIT_USAGE_ERROR,
            stderr=(
                "ERROR: usage: python -m pytest [options] [file_or_dir] "
                "[file_or_dir] [...]\n"
                "python -m pytest: error: unrecognized arguments: "
                "--numprocesses=auto\n"
                "  inifile: /repo/pyproject.toml\n"
                "  rootdir: /repo\n"
            ),
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert any(
        "--numprocesses" in p and "pytest-xdist" in p for p in result.problems
    ), result.problems
    assert result.evidence["bare_runner_exit"] == EXIT_USAGE_ERROR
    assert result.evidence["bare_runner_argv"] == container.bare_runner_argvs[0]


def test_a_bare_collection_error_is_not_a_problem(monkeypatch, tmp_path):
    """Exit 2 is a collection error the task may legitimately carry at the
    start state (broadening 2) -- the f2p/p2p checks judge that, not this
    probe, which only asks whether the repo's own config parses at all.
    Evidence still records what was measured."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        bare_runner=_Exec(exit_code=EXIT_COLLECTION_INTERRUPTED),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["bare_runner_exit"] == EXIT_COLLECTION_INTERRUPTED


def test_a_bare_exit_code_outside_the_accepted_set_is_a_problem(
    monkeypatch, tmp_path
):
    """127 is "command not found" -- the interpreter this probe resolved is
    not on PATH, which is exactly "the agent cannot run the command it will
    naturally type". Falling through the 0/1/2/4/5/124 ladder in silence
    would record `bare_runner_exit: 127` as evidence with no problem beside
    it, reading as a clean gate."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        bare_runner=_Exec(exit_code=127, stderr="exec: python: not found\n"),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    # Not just the digits: "exited 127 (exit code 127)" is `adapter.explain`'s
    # fallback for a code its own `_EXIT_MEANING` does not carry, and it would
    # satisfy a bare "127" in p check while naming no cause at all.
    assert any(
        "127" in p and "not on PATH" in p for p in result.problems
    ), result.problems
    assert result.evidence["bare_runner_exit"] == 127


def test_a_node_task_never_runs_a_bare_pytest_at_all(monkeypatch, tmp_path):
    """vitest and jest have no addopts analogue -- both exit 1 for a broken
    config, a failing test and an unresolvable import alike (measured), so a
    bare invocation could tell none of them apart. The probe must not even be
    attempted on a node task, and the evidence must say why it was skipped
    rather than recording a false 'no problem'."""
    container, task = _node_container()

    result = _preflight_over((container, task))

    assert result.ok, result.problems
    assert container.bare_runner_calls == 0
    assert result.evidence["bare_runner_exit"] is None
    assert result.evidence["bare_runner_skipped"]


def test_the_bare_argv_carries_none_of_tests_runners_extra_arguments(
    monkeypatch, tmp_path
):
    """The whole point of the probe: an addopts-bypass runner
    (--override-ini=addopts=) must not leak into the bare invocation, or the
    probe would just be re-running the gated command under a new name and
    would never SEE the usage error bidict-389 hides."""
    task = _FakeTask(tests=_FakeTests(
        runner=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--override-ini=addopts=", "tests/"),
    ))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    argv = result.evidence["bare_runner_argv"]
    assert "--override-ini=addopts=" not in argv
    assert "tests/" not in argv
    assert argv == ["timeout", "600", "python", "-m", "pytest", "--co", "-q"]


def test_the_bare_probe_runs_before_the_reference_is_applied(
    monkeypatch, tmp_path
):
    """The start state, not the fixed-up one: a bare probe run after the
    reference patch lands would prove nothing about what an agent sees on
    turn one, which is the whole question this probe exists to answer."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    bare_index = next(
        i for i, cmd in enumerate(container.commands) if "--co" in cmd
    )
    apply_index = next(
        i for i, cmd in enumerate(container.commands)
        if cmd[:2] == ["git", "apply"]
    )
    assert bare_index < apply_index


# --- every submodule is initialised at its gitlink ---------------------------


def test_the_submodule_status_parse_reads_the_leading_character():
    """Measured 2026-09-01, git 2.50.1: `-` uninitialised (which is also what a
    transient `-c submodule.<n>.url=` leaves behind), `+` initialised at a
    different commit than the gitlink, and a leading SPACE the one acceptable
    state. The prefix is the whole verdict -- an empty submodule directory
    leaves `git status --porcelain` clean, so nothing else reports it.

    The describe suffix is on the healthy lines because that is the shape
    `materialize` actually leaves behind: the pruned mirror publishes
    `refs/heads/main` at the gitlink sha, so the initialised submodule's HEAD
    sits on `heads/main` rather than detached, and git says so.
    """
    out = (
        " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep (heads/main)\n"
        "-0000000000000000000000000000000000000000 vendor/other\n"
        "+1111111111111111111111111111111111111111 vendor/third (heads/x)\n"
    )

    parsed, unmatched = _parse_submodule_status(
        out, ("vendor/libdep", "vendor/other", "vendor/third"))

    assert unmatched == []
    assert parsed == [
        {"path": "vendor/libdep",
         "sha": "942c381d88cecca36be86b2e902f554ad145ec44",
         "initialised": True, "marker": " "},
        {"path": "vendor/other",
         "sha": "0" * 40, "initialised": False, "marker": "-"},
        {"path": "vendor/third", "sha": "1" * 40, "initialised": False,
         "marker": "+"},
    ]

    # `initialised` collapses `-` and `+`; the raw marker is what separates
    # "the directory is empty" from "the directory holds the WRONG revision".
    # Both are NO-GO and the remedy differs, so the record keeps the marker.
    assert [entry["marker"] for entry in parsed] == [" ", "-", "+"]


def test_a_status_line_whose_path_contains_a_paren_is_parsed_from_the_index():
    """The status line is human-readable and has no `-z` form, so splitting it
    on `" ("` mis-parses. The path set comes from `git ls-files -s -z`; the
    line contributes its first character and nothing else.
    """
    out = " 942c381d88cecca36be86b2e902f554ad145ec44 ven (dor)/lib (a)\n"

    parsed, unmatched = _parse_submodule_status(out, ("ven (dor)/lib",))

    assert unmatched == []
    assert parsed == [{"path": "ven (dor)/lib",
                       "sha": "942c381d88cecca36be86b2e902f554ad145ec44",
                       "initialised": True, "marker": " "}]


def test_a_status_line_the_index_has_no_gitlink_for_is_a_problem():
    """The two readers disagreeing is not resolvable here, and it must not be
    papered over with the display line's own text -- a display-derived path in
    the evidence is what taking paths from `ls-files` exists to prevent.
    """
    out = " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/ghost\n"

    parsed, unmatched = _parse_submodule_status(out, ("vendor/libdep",))

    assert parsed == []
    assert unmatched == [out.rstrip("\n")]


def test_a_sibling_prefix_path_does_not_claim_another_submodules_line():
    """The bare-`startswith` defect, and it is NOT the nested-prefix one below.

    Here `vendor/libdep` is not in the index at all -- the only gitlink is
    `vendor/lib`. A bare `startswith` matches, so the line is filed under
    `vendor/lib`: a path that IS in the authoritative set, which is precisely
    what makes the mistake undetectable downstream. `stale` then names a
    submodule the line was never about, and the NO-GO points the operator at
    the wrong directory.

    The boundary match sends it to `unmatched` instead, where it becomes the
    "the two readers disagree about this tree" problem -- the true description
    of an index and a status listing that name different paths.
    """
    out = " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep (heads/main)\n"

    parsed, unmatched = _parse_submodule_status(out, ("vendor/lib",))

    assert parsed == []
    assert unmatched == [out.rstrip("\n")]


def test_a_sibling_prefix_with_no_separator_at_all_does_not_match():
    """The same defect with neither `/` nor space: ` <sha> vendorx` against the
    gitlink `vendor`. `startswith` says yes; there is no boundary to speak of.
    """
    out = " " + "9" * 40 + " vendorx\n"

    parsed, unmatched = _parse_submodule_status(out, ("vendor",))

    assert parsed == []
    assert unmatched == [out.rstrip("\n")]


def test_a_gitlink_record_with_no_tab_yields_no_path_and_never_raises():
    """`git ls-files -s -z` records are `<mode> <sha> <stage>\\t<path>`. A
    record beginning `160000 ` with no TAB made the old `split("\\t", 1)[1]`
    raise IndexError -- out of a gate `run_matrix` does not wrap, so a
    traceback instead of the NO-GO the driver knows how to handle.

    It contributes no path, which is not a silent drop: any status line for it
    then matches nothing and is reported as the two readers disagreeing.
    """
    class _TablessIndex:
        def exec(self, cmd, **kw):
            assert cmd[:3] == ["git", "ls-files", "-s"]
            return _Exec(stdout="160000 " + "a" * 40 + " 0\0")

    assert _gitlink_paths(_TablessIndex()) == ()


def test_a_space_containing_sibling_path_takes_the_longest_match():
    """What is left for `max(key=len)` once the match is boundary-anchored.

    A tree carrying both `vendor/lib` and `vendor/lib dep` is the one shape
    where two index paths genuinely match ONE status line: the separator the
    boundary rule looks for is itself part of the longer path. The shorter one
    would rename the submodule in the evidence, so the longest match is the
    only one that can be right.
    """
    out = " " + "9" * 40 + " vendor/lib dep (heads/main)\n"

    parsed, unmatched = _parse_submodule_status(
        out, ("vendor/lib", "vendor/lib dep"))

    assert unmatched == []
    assert [entry["path"] for entry in parsed] == ["vendor/lib dep"]


def test_a_nested_gitlink_prefix_does_not_claim_the_longer_path():
    """`startswith` matches BOTH `vendor/lib` and `vendor/libdep` against a
    line naming the second, and the shorter one would silently rename the
    submodule in the evidence. The longest match is the only one that can be
    right, because the tail begins with the path.
    """
    out = " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep (heads/main)\n"

    parsed, _ = _parse_submodule_status(out, ("vendor/lib", "vendor/libdep"))

    assert [entry["path"] for entry in parsed] == ["vendor/libdep"]


def test_an_initialised_submodule_at_its_gitlink_is_a_GO(monkeypatch, tmp_path):
    """The pass direction, and the shape `materialize` leaves: a leading space
    and a `(heads/main)` suffix the parser never reads."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        # An INITIALISED submodule has content, and `empty` is measured for
        # every entry rather than only declared ones -- so that a `None` there
        # can only ever mean "the read failed" and never "the read was not
        # attempted".
        ls_entries={"vendor/libdep": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    assert result.evidence["submodules"] == [
        {"path": "vendor/libdep",
         "sha": "942c381d88cecca36be86b2e902f554ad145ec44",
         "initialised": True, "marker": " ",
         "declared_unneeded": False, "empty": False}
    ]
    assert result.evidence["submodules_orphaned"] == []
    # `{}` is "measured, this task declares no unneeded submodules"; `None` is
    # "not measured", which is the pre-container early return.
    assert result.evidence["submodules_empty_after_suite"] == {}


def test_an_uninitialised_submodule_is_a_preflight_problem(monkeypatch,
                                                           tmp_path):
    """NO-GO, not evidence. An empty submodule directory leaves
    `git status --porcelain` clean, so the suite would simply fail to collect
    and every arm would be scored on an environment defect -- the Phase 0c
    shape, arriving through the dataset instead of the image.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), gitlinks=("vendor/libdep",),
        submodule_status=(
            "-0000000000000000000000000000000000000000 vendor/libdep\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("vendor/libdep" in p for p in result.problems)
    assert result.evidence["submodules"] == [
        {"path": "vendor/libdep", "sha": "0" * 40, "initialised": False,
         "marker": "-", "declared_unneeded": False, "empty": True}
    ]


def test_a_submodule_at_the_wrong_commit_is_a_preflight_problem(monkeypatch,
                                                                tmp_path):
    """`+`, not `-`: the directory is populated and the suite imports SOMETHING
    -- a different revision of the dependency than the task was cut against.
    Louder than empty and no less fatal, and `git status --porcelain` in the
    superproject is the only place it shows at all.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), gitlinks=("vendor/libdep",),
        submodule_status=("+" + "1" * 40 + " vendor/libdep (heads/main)\n"),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("not initialised at their gitlink" in p for p in result.problems)
    assert result.evidence["submodules"] == [
        {"path": "vendor/libdep", "sha": "1" * 40, "initialised": False,
         "marker": "+", "declared_unneeded": False, "empty": True}
    ]


def test_a_task_with_no_submodules_records_an_empty_list(monkeypatch, tmp_path):
    """Absence is recorded, never implied. `[]` is an observation of NONE; a
    missing key reads as "not measured", and the two must not render alike.

    This is the state every other test in this module is already in, which is
    what makes the "adds no problem" half load-bearing: a check that fired on
    the ordinary task would take the whole task set down with it.
    """
    container = _ScriptedContainer(start_sha="s" * 40, tests=_FakeTests(),
                                   submodule_status="")

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.evidence["submodules"] == []
    assert result.evidence["submodules_orphaned"] == []
    assert not any("submodule" in p for p in result.problems)


def test_a_failed_submodule_status_records_None_and_a_problem(monkeypatch,
                                                              tmp_path):
    """A null says which kind of null it is.

    A non-zero `git submodule status` returns EMPTY stdout, which parses to
    `[]` -- a positive observation of "there are none" manufactured out of a
    failure. `None` is "the measurement did not happen", and the two must not
    render identically. This is the same silent zero `container._checked_exec`
    exists for: a failed `git diff` returns output byte-identical to a clean
    tree.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), submodule_status_exit=128,
        submodule_status="fatal: not a git repository\n",
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules"] is None
    assert result.evidence["submodules_orphaned"] is None
    assert any("git submodule status" in p for p in result.problems)


def test_an_unreadable_index_records_None_and_a_problem(monkeypatch, tmp_path):
    """Same shape, different cause: `git submodule status` answered and the
    index did not, so there is no authoritative path set and every path in the
    display lines would have to be trusted -- which is the one thing this
    check refuses to do.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), gitlinks=None,
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules"] is None
    assert result.evidence["submodules_orphaned"] is None
    assert any("ls-files" in p for p in result.problems)


def test_a_status_line_with_no_gitlink_is_reported_as_a_problem(monkeypatch,
                                                                tmp_path):
    """The parse's `unmatched` reaching the gate. Not a fabricated path in the
    evidence and not silence: the two readers disagree about this tree, which
    is a state nothing here can interpret.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/ghost\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("no gitlink for" in p for p in result.problems)
    assert result.evidence["submodules"] == []


def test_a_gitlink_with_no_status_line_is_reported_as_a_problem(monkeypatch,
                                                                tmp_path):
    """The REVERSE of the neighbour above, and the quieter of the two.

    An index gitlink that `git submodule status` says nothing about
    contributes no entry, so `evidence["submodules"]` is a positive `[]` --
    byte-identical to a task with no submodules at all -- and `stale` reads
    that list, so the one submodule this whole check exists to catch is
    exactly the one it cannot see. Without this problem the gate is GO on a
    tree whose dependency directory may be empty.

    Scripted with an empty status listing rather than a mismatched one, so
    the `unmatched` problem cannot fire and answer for this assertion.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",), submodule_status="",
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("said nothing about" in p and "vendor/libdep" in p
               for p in result.problems)
    assert result.evidence["submodules"] == []
    # Not an orphan: the stanza and the gitlink both exist, it is the status
    # listing that is missing. The two must not be confused.
    assert result.evidence["submodules_orphaned"] == []


def test_a_submodule_name_with_a_space_is_not_reported_as_an_orphan(
        monkeypatch, tmp_path):
    """`--get-regexp -z`, and the name is where the space lands.

    `[submodule "my sub"]` gives the key `submodule.my sub.path`, so without
    `-z` the line is `submodule.my sub.path vendor/libdep` and
    `split(" ", 1)[1]` returns `sub.path vendor/libdep` -- a string that is
    not in `gitlinks`, so a perfectly healthy submodule is reported as an
    orphan under a path that is not a path. `-z` records `<key>\\n<value>\\0`,
    which is unambiguous for both halves.

    The `_ScriptedContainer` uses the path as the submodule name (what
    `git submodule add` writes), so a gitlink path carrying a space is what
    produces the key that carries one.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/my dep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/my dep"
            " (heads/main)\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    assert result.evidence["submodules_orphaned"] == []
    assert [e["path"] for e in result.evidence["submodules"]] \
        == ["vendor/my dep"]


def test_an_orphaned_gitmodules_stanza_is_recorded_and_not_refused(monkeypatch,
                                                                   tmp_path):
    """Inert, therefore recorded rather than refused. Measured 2026-09-01: git
    drives submodules off the INDEX, so a stanza whose path has no gitlink is
    never listed, never fetched and creates no directory -- the shape a
    `git rm --cached` with the stanza left behind produces. `derive_submodules`
    accepts it on the host for that reason; this is the same fact observed in
    the tree the suite will actually run against.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        gitmodules_declared=("vendor/libdep", "vendor/gone"),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    assert result.evidence["submodules_orphaned"] == ["vendor/gone"]


def test_an_unreadable_gitmodules_is_not_reported_as_no_orphans(monkeypatch,
                                                                tmp_path):
    """git config exits 1 for "no key matched" and >1 for "I could not read
    it". Collapsing them makes an unreadable or malformed `.gitmodules` render
    as the measured claim `[]` -- an answer, from a probe that gave none.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), gitlinks=("vendor/libdep",),
        gitmodules_exit=3,
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules_orphaned"] is None
    # The submodule reading itself still happened and is still recorded: one
    # probe failing does not un-observe the other.
    assert result.evidence["submodules"] == [
        {"path": "vendor/libdep",
         "sha": "942c381d88cecca36be86b2e902f554ad145ec44",
         "initialised": True, "marker": " ",
         "declared_unneeded": False, "empty": True}
    ]
    assert any(".gitmodules" in p for p in result.problems)


def test_the_submodule_keys_are_written_on_the_early_return(tmp_path):
    """The non-pytest guard returns before a container ever starts. A key
    absent there and present on the normal path is the same defect one layer
    down -- a reader diffing two evidence files cannot tell "no submodules"
    from "this gate stopped before it looked".
    """
    task = _FakeTask(tests=_FakeTests(runner=("nose",)))

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.evidence["submodules"] is None
    assert result.evidence["submodules_orphaned"] is None
    assert result.evidence["submodules_empty_after_suite"] is None


# --- submodules_unneeded: the gate asserts the declared state ----------------


def _unneeded_task(*paths):
    return _FakeTask(submodules_unneeded=tuple(paths))


def test_a_declared_unneeded_submodule_is_a_GO_while_uninitialised(
        monkeypatch, tmp_path):
    """The item, at the gate. `-` and an EMPTY directory are what
    `materialize` leaves when `_init_submodules` skips the path, and that is
    now the asserted state rather than the `stale` NO-GO.
    """
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert result.ok, result.problems
    assert result.evidence["submodules"] == [
        {"path": "vendor/libdep", "sha": "0" * 40, "initialised": False,
         "marker": "-", "declared_unneeded": True, "empty": True}
    ]
    assert result.evidence["submodules_empty_after_suite"] == \
        {"vendor/libdep": True}


def test_a_needed_submodule_that_is_uninitialised_is_still_a_problem(
        monkeypatch, tmp_path):
    """The regression this change could silently take out: the same tree with
    NOTHING declared is the Phase 0c shape arriving through the dataset, and
    `stale` still has to fire on it."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("not initialised at their gitlink" in p for p in result.problems)


def test_a_declared_unneeded_submodule_that_is_populated_is_a_problem(
        monkeypatch, tmp_path):
    """Something populated a path the harness was told to leave alone, so the
    suite is reading content this task was not cut against. The run tree is
    bind-mounted over the image's own /repo, so only a host-side write after
    materialization can produce it."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert not result.ok
    assert any("leave alone" in p for p in result.problems)


def test_a_declared_unneeded_submodule_with_content_is_a_problem(
        monkeypatch, tmp_path):
    """HARVESTING's "a suite that writes inside the submodule is out" rule,
    enforced at the only place it still can be. Measured 2026-09-02: a file
    inside an UNINITIALISED submodule directory is invisible to `git status
    --porcelain`, to `git ls-files -o` and to `git add -A`, so the marker is
    still `-` and every other check in this gate is silent."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
        ls_entries={"vendor/libdep": ("junk.txt",)},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert not result.ok
    assert any("not empty" in p for p in result.problems)
    assert result.evidence["submodules"][0]["empty"] is False


def test_an_unlistable_submodule_directory_records_None_and_a_problem(
        monkeypatch, tmp_path):
    """`None`, never `False`. An unlistable directory is the "not measured"
    absence, and rendering it as the measured claim `False` would let a
    permission error read as "the gate checked and the directory is empty".

    The message names the exit code and the read's own output (re-read
    rather than threaded through `entry["empty"]`, which stays `bool | None`
    everywhere else it is checked), and its tail claims the declaration
    because this path IS declared unneeded."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
        ls_exits={"vendor/libdep": 2},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert not result.ok
    assert result.evidence["submodules"][0]["empty"] is None
    problem = next(p for p in result.problems
                   if "could not list vendor/libdep" in p)
    assert "(exit 2)" in problem
    assert "cannot access 'vendor/libdep'" in problem
    assert "declared-unneeded submodule" in problem


def test_an_unlistable_needed_submodule_directory_does_not_claim_a_declaration(
        monkeypatch, tmp_path):
    """`empty` is measured for every submodule, needed ones included -- but
    the "only claim this gate can check" sentence is about a DECLARATION the
    manifest does not carry for a needed path, so it must not appear here."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_exits={"vendor/libdep": 2},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules"][0]["empty"] is None
    problem = next(p for p in result.problems
                   if "could not list vendor/libdep" in p)
    assert "(exit 2)" in problem
    assert "declared-unneeded submodule" not in problem


def test_an_unneeded_declaration_the_index_has_no_gitlink_for_is_a_problem(
        monkeypatch, tmp_path):
    """`derive_submodules` refuses this on the HOST, so reaching it means the
    container's tree is not the one that was derived. Recorded as an
    observation rather than trusted to the host-side refusal, for the reason
    `_parse_submodule_status`'s docstring gives about not restating
    configuration."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/gone"), container)

    assert not result.ok
    assert any("vendor/gone" in p and "disagree" in p for p in result.problems)


def test_a_suite_that_writes_into_a_declared_unneeded_submodule_is_a_problem(
        monkeypatch, tmp_path):
    """The SECOND read is the whole test: empty before the suite and not empty
    after it. `git status --porcelain` -- the dirty-tree check immediately
    above this one -- cannot see into a gitlink path, so without this read
    "the suite ran with the directory empty" is an assumption."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
        ls_entries={"vendor/libdep": ((), ("scratch.db",))},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert not result.ok
    assert result.evidence["submodules_empty_after_suite"] == \
        {"vendor/libdep": False}
    assert any("left content in vendor/libdep" in p for p in result.problems)


def test_an_unreadable_directory_after_the_suite_is_not_reported_as_content(
        monkeypatch, tmp_path):
    """A mapping, not a list, and this is why: an entry built from `empty is
    None or not empty` would mean either "measured, has content" or "the read
    failed", collapsing two absences at the top level. The problem text has to
    say which of the two it is, and so does the evidence."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
        ls_exits={"vendor/libdep": (0, 2)},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert not result.ok
    assert result.evidence["submodules_empty_after_suite"] == \
        {"vendor/libdep": None}
    assert any("UNKNOWN" in p and "the read failed" in p
               for p in result.problems)
    assert not any("left content in" in p for p in result.problems)


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


# --- the runner adapter seam -------------------------------------------------


def test_the_runner_routes_every_argv_and_verdict_through_its_adapter():
    """`_Runner` builds no argv of its own and reads no exit code of its own.

    That is the whole of Task 2: every branch in the gate used to interpret
    pytest's numbers inline, so the gate could only ever gate pytest. A
    `_Runner` that still spelled a selection or a deselection itself would
    keep one framework's grammar in a class that now serves three -- and it
    fails silently, because vitest and jest accept an unknown flag shape by
    running nothing at exit 0.
    """
    from bakeoff.preflight import _Runner
    from bakeoff.runners import KIND_NOTHING_RAN, Outcome

    class _Adapter:
        name = "fake"

        def select_argvs(self, node_ids):
            return [["--only", *node_ids]]

        def p2p_argvs(self, *, selected, scope, deselected, ignored):
            return [["--p2p", *selected, *scope, *deselected, *ignored]]

        def merge_reports(self, reports):
            return None

        def file_filter_matches(self, declared_path, candidate_path):
            return declared_path == candidate_path

        def report_path(self):
            return None

        def report_args(self, report_path):
            return []

        def classify(self, *, exit_code, stdout, stderr, report):
            return Outcome(kind=KIND_NOTHING_RAN, exit_code=exit_code,
                           explain="the adapter answered")

    recorder = _Recorder()
    runner = _Runner(recorder, ("run",), 60, _Adapter())

    result = runner.select(("a::b",))
    runner.pass_to_pass(_Tests(), extra_deselect=("q::r",), scope=("tests/",),
                        ignore=("tests/broken.py",))

    assert recorder.commands == [
        ["timeout", "60", "run", "--only", "a::b"],
        ["timeout", "60", "run", "--p2p", "tests/", "tests/a.py::test_one",
         "q::r", "tests/broken.py"],
    ]
    outcome = runner.classify(result)
    assert outcome.kind == KIND_NOTHING_RAN
    assert outcome.explain == "the adapter answered"


def test_the_runner_records_what_it_selected_and_the_deselect_branch_clears_it():
    """`_selected` exists from CONSTRUCTION, and is now written.

    The earlier version of this test pinned the field's UNWRITTEN state, under
    a docstring saying in as many words that it would fail once `classify` had
    to report `not_run`. This is that failure, taken up: `select` records what
    it asked for, the explicit-p2p branch records the p2p list, and the
    DESELECT branch clears it.

    That clear is the load-bearing third assertion. A leftover selection there
    would make a correct scoped run report the f2p ids as "did not run" --
    which is exactly what a correct p2p run does to them -- so preflight would
    refuse every healthy node task for the one thing it was built to do.

    Initialised at construction rather than on first use because an unset
    attribute raises `AttributeError` out of `classify` on the deselect
    branch, which never selects by id -- a crash on the path whose whole job
    is to report an absence.
    """
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest"), 60)
    assert runner._selected == ()

    runner.select(("tests/a.py::test_one",))
    assert runner._selected == ("tests/a.py::test_one",)

    runner.pass_to_pass(_Tests(p2p=("tests/b.py::test_two",)))
    assert runner._selected == ("tests/b.py::test_two",)

    runner.pass_to_pass(_Tests())
    assert runner._selected == ()


def test_the_runner_adapter_default_is_pytest_and_nothing_may_rely_on_it():
    """The plan asked for NO default: `oracle._derive` and the grader's two
    checks each build a `_Runner`, and a default lets one of the three go on
    classifying a jest run with pytest's exit codes -- where 1 is what a
    config error, an import error and a failing assertion all return alike --
    stamping a model failure on an environment defect, permanently, in an
    append-only store.

    It has one anyway, because a harder constraint pulls the other way:
    `test_grading_p2p_with_no_extras_is_the_argv_preflight_validated` is the
    argv-identity gate on this refactor, it constructs a `_Runner` with three
    positional arguments, and a gate rewritten to accommodate the change it
    gates has stopped gating anything. So the default stays and every call
    site passes the adapter EXPLICITLY instead -- `preflight` does below;
    `oracle` and the grader are Task 3's.
    """
    import inspect

    from bakeoff.preflight import _Runner
    from bakeoff.runners import for_framework

    default = inspect.signature(_Runner.__init__).parameters["adapter"].default
    assert default is for_framework("pytest")


def test_the_runner_gate_names_the_declared_framework_and_the_marker():
    """The gate used to be `any("pytest" in part)`, which could only ever mean
    one framework. It now asserts the DECLARED framework and the argv agree --
    each catching the other's typo, which is why neither is derived from the
    other. A manifest declaring vitest whose runner invokes jest would
    otherwise be classified by the wrong adapter."""
    from bakeoff.runners import for_framework

    assert for_framework("pytest").runner_marker == "pytest"
    assert for_framework("vitest").runner_marker == "vitest"
    assert for_framework("jest").runner_marker == "jest"


def test_preflight_refuses_a_runner_that_does_not_match_the_framework(tmp_path):
    """The early return, before any container is started -- the same shape the
    old `pytest`-substring gate had, so a bad manifest costs no daemon. The
    message names BOTH sides, because the fix is either one: the framework is
    wrong for this runner, or the runner is wrong for this framework."""
    task = _FakeTask(tests=_FakeTests(runner=("python", "-m", "unittest")))

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert not result.ok
    assert "'pytest'" in result.problems[0]
    assert "unittest" in result.problems[0]
    # No container was started, so the container-only evidence keys stay None.
    assert result.evidence["image_env_observed"] is None
    assert result.evidence["bare_runner_exit"] is None
    # The OTHER path that skips the bare-runner probe: `bare_runner_skipped`
    # must carry a reason here too, not just on the node early-return path
    # covered by `test_a_node_task_never_runs_a_bare_pytest_at_all` -- an
    # unset key would render identically to "the probe ran and skipped
    # silently", which is exactly the absence this key exists to distinguish.
    assert result.evidence["bare_runner_skipped"]


def test_preflight_evidence_names_the_framework_it_judged_under():
    """A cached verdict outlives the code that wrote it, and every other
    evidence key now means something framework-dependent -- `f2p_before_exit`
    most of all, since 1 means "a test failed" under pytest and means nothing
    at all under vitest."""
    task = _FakeTask(tests=_FakeTests(runner=("python", "-m", "unittest")))

    result = preflight(task, image="sha256:x", repo_path=Path("/nonexistent"),
                       start_sha="0" * 40)

    assert result.evidence["framework"] == "pytest"


def test_the_hypothesis_probe_is_asked_of_the_adapter():
    """It is a Python-ecosystem check, so the adapter owns it. pytest reads
    the interpreter off `tests.runner`; the node adapters answer `None` and
    preflight then skips the block entirely, leaving both evidence keys `None`
    -- a recorded ABSENCE, never a claim that the suite is deterministic."""
    from bakeoff.runners import for_framework

    assert for_framework("pytest").hypothesis_interpreter(
        ("python", "-m", "pytest")) == "python"
    for framework in ("vitest", "jest"):
        assert for_framework(framework).hypothesis_interpreter(
            ("/node_modules/.bin/vitest", "run")) is None, framework


def test_an_adapter_that_declines_the_probe_leaves_both_keys_absent(
    monkeypatch, tmp_path
):
    """`None` from `hypothesis_interpreter` skips both probes AND the CI
    refusal, and the two evidence keys stay `None` -- a RECORDED ABSENCE,
    never a claim that a JavaScript suite is deterministic. `False` there
    would be the claim "this suite does not import hypothesis", asserted by a
    gate that never ran a scan.

    Driven through `preflight` rather than through the adapter, because the
    thing under test is the GUARD: an adapter answering `None` into an
    unguarded block runs `container.exec([None, "-c", ...])`.
    """
    from bakeoff.runners import for_framework

    task = _FakeTask()

    class _NoProbe:
        def __getattr__(self, name):
            return getattr(for_framework("pytest"), name)

        def hypothesis_interpreter(self, runner):
            return None

    monkeypatch.setattr("bakeoff.preflight.for_framework",
                        lambda name: _NoProbe())
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",),
                                   hypothesis_importable=True,
                                   hypothesis_in_suite=True)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert result.evidence["hypothesis_importable"] is None
    assert result.evidence["hypothesis_imported_by_suite"] is None
    assert not any("hypothesis" in problem for problem in result.problems)


# --- the node-only assertions -------------------------------------------------
#
# Three shapes that read as a GREEN GATE on vitest and jest and have no pytest
# analogue, plus the cache flag whose absence the dirty-tree check cannot see.
# Every one of them is a fact about the framework rather than about the model,
# and each would otherwise be scored as capability on every arm of the task.


#: The node report, reduced to the fields this codebase reads: `testResults[]`
#: with an ABSOLUTE `name` under the bind mount, a suite `status`, and
#: `assertionResults[]` carrying `status` and `fullName`. Written out here
#: rather than loaded from `tests/fixtures/node_reports/` because these tests
#: are about preflight's assertions over a report, not about the eight captured
#: shapes -- those are `test_runners.py`'s subject and are replayed there.
def _node_report(entries, *, status="passed"):
    """`[(relpath, [(status, fullName), ...])]` -> one report document."""
    return {
        "testResults": [
            {
                "name": "/repo/" + path,
                "status": status,
                "assertionResults": [
                    {"status": state, "title": name, "fullName": name}
                    for state, name in assertions
                ],
            }
            for path, assertions in entries
        ],
    }


def _node_exit(report):
    """What the frameworks measurably return for a report of this shape.

    0 when nothing failed -- INCLUDING the all-skipped report a `-t` pattern
    matching nothing produces, which is the whole hazard -- and 1 otherwise.
    `None` (no file at all) is the broken-config shape, which also exits 1.
    """
    if report is None:
        return 1
    for suite in report["testResults"]:
        if suite["status"] == "failed":
            return 1
        if any(a["status"] == "failed" for a in suite["assertionResults"]):
            return 1
    return 0


_NODE_F2P = "tests/a.test.js::does a thing"


def _node_container(*, f2p_ran=True, scope_names=None, scope_files=None,
                    p2p=(), framework="vitest", runner=None):
    """A node task and the container that answers its five suite CHECKS.

    Each check is one or more commands now -- a node selection is one per
    file and a node deselection is 1 + K -- and the container serves every
    group of one check the same key. See `_ScriptedContainer._node_timeout`.

    `framework` picks the flavour, and the default `runner` follows it: the
    gate refuses a manifest whose declared framework and declared argv
    disagree (`runner_marker`), so the two cannot be varied independently
    here either.

    `f2p_ran=False` is M1's shape verbatim: the declared f2p test is reported
    SKIPPED at exit 0, which is what both frameworks do with a `-t` pattern
    that matches nothing -- a renamed test, most often.

    `scope_names` and `scope_files` both describe the scoped p2p run's report,
    from the two directions the two assertions read it: `scope_names` names
    `(file, fullName)` pairs directly, and `scope_files` names files and gives
    each a distinct title so the duplicate-name rule stays quiet and the scope
    rule is the only thing under test.
    """
    if runner is None:
        runner = (("/node_modules/.bin/vitest", "run", "--no-cache")
                  if framework == "vitest" else ("/node_modules/.bin/jest",))
    tests = _FakeTests(paths=("tests/",), runner=runner, f2p=(_NODE_F2P,),
                       p2p=tuple(p2p), framework=framework)
    task = _FakeTask(tests=tests)

    if scope_names is not None:
        scoped = list(scope_names)
    elif scope_files is not None:
        scoped = [(path, f"keeps working {index}")
                  for index, path in enumerate(scope_files)]
    else:
        scoped = [("tests/b.test.js", "keeps working")]
    # One suite per distinct file, in first-seen order -- the shape the
    # reporters emit, and the shape `files_run` is derived from.
    by_file: dict[str, list] = {}
    for path, name in scoped:
        by_file.setdefault(path, []).append(("passed", name))

    p2p_ids = tuple(p2p) or ("tests/b.test.js::keeps working",)
    p2p_report = _node_report([
        (node_id.partition("::")[0],
         [("passed", node_id.partition("::")[2])]) for node_id in p2p_ids
    ])
    reports = {
        "f2p_before": _node_report(
            [("tests/a.test.js",
              [("failed" if f2p_ran else "skipped", "does a thing")])],
            status="failed" if f2p_ran else "passed",
        ),
        "f2p_after": _node_report(
            [("tests/a.test.js", [("passed", "does a thing")])]),
        "p2p_before": p2p_report,
        "p2p_after": p2p_report,
        "scoped": _node_report(list(by_file.items())),
    }
    container = _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                   present=tests.paths, reports=reports)
    return container, task


def _pytest_container(*, runner=("python", "-m", "pytest", "-q")):
    """The pytest task, at whatever runner a test wants to state something about."""
    tests = _FakeTests(runner=runner)
    task = _FakeTask(tests=tests)
    container = _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                   present=tests.paths)
    return container, task


def _preflight_over(scripted):
    """One preflight over a scripted container, with no fixture plumbing.

    `mock.patch` rather than `monkeypatch` so a test that is about the
    evidence can take no arguments at all, and a real temporary directory
    because `preflight` writes the reference patch into `repo_path` before
    handing it to the container.
    """
    container, task = scripted
    with tempfile.TemporaryDirectory() as repo:
        with mock.patch("bakeoff.preflight.RunContainer",
                        lambda **kwargs: container):
            return preflight(task, image="sha256:x", repo_path=Path(repo),
                             start_sha="s" * 40)


def test_a_declared_f2p_id_that_never_RAN_is_a_problem(monkeypatch):
    """`-t` with a pattern matching no test exits **0** on both vitest and jest
    (measured 2026-09-01), with `Tests 3 skipped (3)`. So a manifest naming a
    renamed test makes an unsatisfiable task read as a green gate, and then as
    a solved run for every arm of it. pytest answers the same input with exit 4
    and needs no such check.

    The assertion is on `did not RUN` and not on the lower-case phrase the
    plan first wrote: an f2p run that executed nothing ALSO trips the older
    "the f2p tests did not run at the start state" refusal, so the loose
    substring would have been satisfied by a message this check did not
    write -- and the mutation anchor is what said so."""
    result = _preflight_over(_node_container(f2p_ran=False))

    assert not result.ok
    assert any("did not RUN" in p for p in result.problems)
    assert result.evidence["f2p_before_not_run"] == [
        "tests/a.test.js::does a thing"]


def test_f2p_before_not_run_is_recorded_even_when_everything_ran():
    """Absence is recorded, never implied: "nothing failed to run" and "the
    gate did not look" render identically as a missing key, and a cached
    verdict outlives the code that wrote it."""
    result = _preflight_over(_node_container(f2p_ran=True))

    assert result.evidence["f2p_before_not_run"] == []


def test_a_cross_file_duplicate_full_name_is_recorded_and_no_longer_a_problem():
    """This was a NO-GO, and the refusal is gone (round 2 item 1).

    It existed because a quarantine of one test silently removed the other
    from the regression check: `-t` matched `fullName` and one invocation
    carried every file. Selection and deselection are now one argv per FILE,
    each pattern holding only that file's titles, so the hazard is
    unreachable -- and the collision itself is still a fact about the task, so
    it stays as EVIDENCE, in the same string shape a v14 verdict carries.

    Measured cost of the old refusal: `eemeli/yaml` at `tests/` gated NO-GO on
    four collisions under `describe('circular references')`, none of them a
    test the manifest named, forcing `tests.paths` down to one file.
    """
    result = _preflight_over(_node_container(
        scope_names=[("tests/a.test.js", "works"), ("tests/b.test.js", "works")]
    ))

    assert result.ok, result.problems
    assert result.evidence["duplicate_full_names"] == [
        "'works' in tests/a.test.js and tests/b.test.js"]


def test_duplicate_full_names_is_recorded_when_there_are_none():
    result = _preflight_over(_node_container())

    assert result.evidence["duplicate_full_names"] == []


def test_a_healthy_node_task_passes_the_whole_gate():
    """The assertion the three node refusals are measured against, and it is
    not decoration: without it each of them is "some problem was raised", and
    the two scoped-run mutation anchors reported MISSED for exactly that
    reason -- `declared f2p tests did not fail at the start state` was firing
    on every node task, because that check re-parsed pytest's `FAILED <id>`
    summary out of stdout and neither node framework writes one."""
    result = _preflight_over(_node_container())

    assert result.ok, result.problems
    assert result.problem_codes == ()


def test_every_new_evidence_key_is_present_on_an_explicit_p2p_task():
    """Three of the four are measured inside `if not tests.p2p:`, which an
    explicit p2p list skips entirely -- so written only where they are
    measured, they would be ABSENT from every explicit-p2p verdict. A key
    present on one task shape and missing on another cannot be read across a
    set of cached verdicts, which outlive the code that wrote them.

    `None` here is "no scoped run was made", not "nothing was found"; the two
    must not render identically."""
    result = _preflight_over(_node_container(p2p=("tests/a.test.js::other",)))

    assert result.evidence["runner_cache_flags"] == ["--no-cache"]
    assert result.evidence["f2p_before_not_run"] == []
    assert result.evidence["duplicate_full_names"] is None
    assert result.evidence["scope_files_run"] is None
    assert result.evidence["scope_files_outside"] is None


def test_the_new_evidence_keys_survive_the_early_return():
    """The runner-gate refusal returns before any container starts. The two
    keys knowable without one are still written; the rest stay `None`.

    `f2p_before_not_run` is one of the rest, and it used to be `[]` here. No
    container started, no f2p selection ran, and nothing read a report -- so
    `[]`, which everywhere else in this file means "measured, every declared
    id ran", was a claim about a measurement this path never made. It is the
    same correction `duplicate_full_names` and `scope_files_outside` already
    carry, and it is what moved `PREFLIGHT_VERSION` to 11."""
    result = _preflight_over(_node_container(runner=("python", "-m", "pytest")))

    assert not result.ok
    assert result.evidence["scope_files_run"] is None
    assert result.evidence["runner_cache_flags"] == ["--no-cache"]
    assert result.evidence["f2p_before_not_run"] is None


def test_an_f2p_run_that_wrote_no_report_did_not_measure_what_ran():
    """The second shape, and the one a cached verdict actually gets wrong.

    Measured 2026-09-01 on both node frameworks: a broken config exits 1 and
    writes NO report at all. `verify_selected` never runs (`classify` guards on
    `last_report is not None`), so `Outcome.not_run` stays empty and
    `sorted(...)` gave `[]` -- byte-identical to "every declared id reached a
    verdict" -- for a run that produced no evidence whatever. The verdict is
    still NO-GO, because a report-less run is an ENVIRONMENT outcome and the
    red-before assertion refuses it; but the evidence outlives the verdict, and
    it was saying the gate had checked and cleared.

    `report_path() is None` is NOT this absence, which is why the guard is not
    a bare `last_report is not None`: pytest writes no report BY DESIGN and
    answers a selection matching nothing with exit 4 and an `ERROR: not found:`
    line, so `[]` there is a claim the framework backs. A missing report is an
    absence only on a framework that writes one."""
    container, task = _node_container()
    container.reports["f2p_before"] = None

    with tempfile.TemporaryDirectory() as repo:
        with mock.patch("bakeoff.preflight.RunContainer",
                        lambda **kwargs: container):
            result = preflight(task, image="sha256:x", repo_path=Path(repo),
                               start_sha="s" * 40)

    assert not result.ok
    assert result.evidence["f2p_before_not_run"] is None
    # Not a claim that every id ran, and not a claim that none did.
    assert not any("did not RUN" in p for p in result.problems)


def test_a_scoped_run_that_left_the_declared_paths_is_a_problem():
    """Measured 2026-09-01: `vitest run tests/` matched `/repo/jtests/
    fail.test.cjs` -- the positional is a SUBSTRING FILTER over the absolute
    path, not a path. The scoped p2p run exists to keep the agent's scratch
    files out of the regression check (eight of them in one stored record); a
    filter that over-matches restores exactly what it was added to remove."""
    result = _preflight_over(
        _node_container(scope_files=("tests/a.test.js", "jtests/b.test.cjs"))
    )

    assert not result.ok
    assert result.evidence["scope_files_outside"] == ["jtests/b.test.cjs"]


def test_a_node_scoped_run_that_collected_nothing_gets_the_named_code():
    """The last raw exit-code branch in this file, and it could only ever fire
    for pytest.

    `SCOPE_COLLECTS_NOTHING` is what `grade.py` turns into
    `NotGradedReason.SCOPE_COLLECTED_NOTHING` -- a mis-scoped task and a task
    that fails its red-before assertion are different author errors with
    different remedies, and the generic reason sends the author looking for a
    bug in a task whose only defect is a `tests.paths` that selects nothing.
    The branch read pytest's exit **5** for it, and neither node framework has
    one: measured, vitest and jest answer a scope that matched no file with 1,
    and a `-t` that matched nothing inside a file that DID load with 0. So a
    mis-scoped node task got the generic reason and its author got sent to the
    wrong place.

    `KIND_NOTHING_RAN` is the same fact stated in terms no framework owns, and
    it is pytest-identical: `pytest_adapter.classify` returns exactly this kind
    for exit 5."""
    from bakeoff.preflight import SCOPE_COLLECTS_NOTHING

    result = _preflight_over(_node_container(scope_names=[]))

    assert not result.ok
    assert SCOPE_COLLECTS_NOTHING in result.problem_codes


def test_the_scope_check_matches_by_path_component_not_by_prefix_string():
    """`startswith` is the bug the runner already has. `tests_helpers/x` starts
    with `tests` and is not under `tests/`."""
    from bakeoff.tasks import _under

    assert _under("tests/a.test.js", ("tests/",))
    assert not _under("tests_helpers/a.test.js", ("tests/",))


def test_a_vitest_runner_without_no_cache_is_a_problem():
    """Asymmetric on purpose. For pytest an absent `-p no:cacheprovider`
    writes .pytest_cache/ and preflight's existing dirty-tree check fires
    loudly. For vitest the artifact is node_modules/.vite, and every JavaScript
    repository's .gitignore carries node_modules/ -- so that check is BLIND and
    the flag's absence is invisible. Measured: with `--no-cache` the tree stays
    clean; without it, `?? node_modules/`."""
    result = _preflight_over(
        _node_container(runner=("/node_modules/.bin/vitest", "run"))
    )

    assert not result.ok
    assert any("--no-cache" in p for p in result.problems)
    assert result.evidence["runner_cache_flags"] == ["--no-cache"]
    assert result.evidence["runner_cache_flags_missing"] == ["--no-cache"]


def test_a_pytest_runner_without_no_cacheprovider_is_NOT_newly_refused():
    """Backwards compatibility as a rule: a new assertion on the pytest branch
    could refuse a manifest that loads today, and the failure it would catch
    is already loud."""
    result = _preflight_over(_pytest_container(runner=("python", "-m", "pytest")))

    assert not any("no:cacheprovider" in p for p in result.problems)
    assert result.evidence["runner_cache_flags_missing"] == [
        "-p", "no:cacheprovider"]


def test_a_pytest_task_records_the_node_keys_as_measured_absences():
    """`[]` where the gate looked and found nothing, `None` where the
    framework cannot answer.

    `duplicate_full_names` is `[]` because the scoped run DID happen and
    pytest's ids carry their file, so the hazard does not exist -- that is a
    CLAIM, and it is why the guard on this key is not a bare `last_report is
    not None`: pytest writes no report by design.

    `scope_files_run` and `scope_files_outside` are BOTH `None`, and the pair
    used to disagree: the first said "the pytest adapter reports no file list
    at all" while the second said `[]`, "measured, nothing left the scope",
    derived from that same absent list. Nothing measured what a positional
    argument matched, so neither key may claim it did."""
    result = _preflight_over(_pytest_container())

    assert result.ok, result.problems
    assert result.evidence["duplicate_full_names"] == []
    assert result.evidence["scope_files_run"] is None
    assert result.evidence["scope_files_outside"] is None


# --- ambiguous file filters (round 2 item 1) ---------------------------------


def test_a_file_filter_that_selects_a_second_executed_file_is_refused():
    """vitest's positional is a SUBSTRING filter that no anchoring reaches.

    Measured 2026-09-02: `/repo/tests/doc/a.test.js` still matched
    `/repo/pkg/tests/doc/a.test.js`. The per-file grouping this harness
    selects and deselects with would then carry a name pattern into a file it
    does not name -- so a quarantine of `tests/doc/a.test.js::works` would
    also remove `works` from the colliding file, which is the defect the
    grouping exists to close, reached one level down.

    Both files are under `tests/`, so `scope_files_outside` stays `[]` and
    this refusal is the only thing that can fire."""
    result = _preflight_over(_node_container(scope_files=(
        "tests/doc/a.test.js", "tests/vendor/tests/doc/a.test.js")))

    assert not result.ok
    assert result.evidence["scope_files_outside"] == []
    assert result.evidence["ambiguous_file_filters"] == [
        "'tests/doc/a.test.js' also selects tests/vendor/tests/doc/a.test.js"]
    assert any("also selects another executed file" in problem
               for problem in result.problems)


def test_ambiguous_file_filters_cannot_fire_on_jest():
    r"""jest's positional is a JS RegExp, and the per-file filter is anchored
    at the mount and escaped -- `^/repo/tests/doc/a\.test\.js$` matched
    exactly one file (measured 2026-09-02). The same report that refuses a
    vitest task must therefore pass a jest one, or the refusal is a claim
    about paths rather than about what a positional does."""
    result = _preflight_over(_node_container(framework="jest", scope_files=(
        "tests/doc/a.test.js", "tests/vendor/tests/doc/a.test.js")))

    assert result.ok, result.problems
    assert result.evidence["ambiguous_file_filters"] == []


def test_ambiguous_file_filters_is_measured_off_the_f2p_run_too():
    """The gap a scoped-only version left. `select_argvs` groups by file as
    well, so the f2p check carries the same positional -- and a task with an
    explicit `tests.p2p` makes NO scoped run at all, so a check reading the
    scoped run alone would be covered by nothing on exactly those tasks."""
    container, task = _node_container(p2p=("tests/b.test.js::keeps working",))
    container.reports["f2p_before"] = _node_report(
        [("tests/a.test.js", [("failed", "does a thing")]),
         ("pkg/tests/a.test.js", [("passed", "unrelated")])],
        status="failed",
    )

    result = _preflight_over((container, task))

    # No scoped run was made at all, which is the whole point of the shape.
    assert "p2p_scoped_after_exit" not in result.evidence
    assert result.evidence["duplicate_full_names"] is None
    assert result.evidence["ambiguous_file_filters"] == [
        "'tests/a.test.js' also selects pkg/tests/a.test.js"]
    assert not result.ok


def test_ambiguous_file_filters_is_None_when_no_node_run_reported_files():
    """Two absences, one key. pytest's adapter reports no file list at all,
    and a node gate whose runs wrote no report measured nothing either --
    `[]` would say "measured, no filter reaches a second file" for a gate
    that never looked."""
    pytest_result = _preflight_over(_pytest_container())
    assert pytest_result.evidence["ambiguous_file_filters"] is None

    container, task = _node_container()
    for key in container.reports:
        container.reports[key] = None
    node_result = _preflight_over((container, task))

    assert node_result.evidence["ambiguous_file_filters"] is None


# --- one check, several commands ---------------------------------------------


def test_every_argv_group_carries_the_suite_timeout_prefix():
    """The bound is PER INVOCATION, so a check that is 1 + K commands is
    bounded by `suite_timeout_s` x (1 + K) of wall clock. That is the true
    statement and `GradeRecord.suite_timeout_s` still says what bound each
    command carried; dividing the budget by K would make the gated bound a
    function of the quarantine, so two tasks declaring the same number would
    get different ones."""
    container, task = _node_container()

    _preflight_over((container, task))

    suite = [cmd for cmd in container.commands
             if cmd[:1] == ["timeout"] and "--reporter=json" in cmd]
    assert len(suite) > 5
    for command in suite:
        assert command[:2] == ["timeout", "600"], command


def test_a_problem_message_names_every_argv_group_that_ran():
    """A message showing one command of a check that ran three names a
    command that is not the whole of what happened -- the reconstruction
    `last_argvs` exists to avoid."""
    container, task = _node_container()
    container.reports["scoped"] = _node_report(
        [("tests/b.test.js", [("failed", "keeps working")])], status="failed")

    result = _preflight_over((container, task))

    problem = next(p for p in result.problems if "the p2p run the GRADER" in p)
    assert problem.count("\n  timeout 600 ") == 2


def test_running_no_argv_groups_raises_rather_than_running_the_bare_runner():
    """`select_argvs(())` is `[]`. Falling through to the bare runner would
    execute the WHOLE SUITE as the selection and classify it as one -- green
    f2p at the start state, or a p2p check over tests nobody selected."""
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest", "-q"), 600)

    with pytest.raises(ValueError):
        runner.run([])


def test_running_a_flat_argv_raises_rather_than_splatting_it():
    """The old signature's shape reaching the new one iterates a list of
    STRINGS and splats each character into a command made of letters -- which
    does not fail loudly on either node framework, since both accept an
    unknown positional by running nothing."""
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest", "-q"), 600)

    with pytest.raises(TypeError):
        runner.run(["--config", "/tmp/nope.mjs"])


def test_pass_to_pass_does_not_raise_when_every_explicit_p2p_id_is_ignored():
    """The production call path deviation 2 left unguarded (`node_adapter.
    NodeAdapter.p2p_argvs`'s explicit-`selected` branch). `preflight`'s
    p2p-BEFORE run passes `ignore` naming the f2p module(s) that failed to
    load at the start state, and on an explicit-`tests.p2p` task every
    declared id can live in that one module -- so before the adapter's own
    guard, EVERY group was dropped unconditionally, `p2p_argvs` returned `[]`,
    and `_Runner.run` raised `ValueError` on the empty sequence. `run_matrix.
    py` has no `except` around `preflight(...)`, so that one task's shape used
    to take every remaining task's gate down with it. `pass_to_pass` must
    return a result here -- a NO-GO problem for THIS task, not a crash for
    every task after it -- whatever that result later classifies as."""
    from bakeoff.preflight import _Runner
    from bakeoff.runners import KIND_PASSED, for_framework

    tests = _FakeTests(paths=("tests/",), f2p=("tests/a.test.js::does a thing",),
                       p2p=("tests/f.test.js::red",),
                       runner=("/node_modules/.bin/vitest", "run", "--no-cache"),
                       framework="vitest")
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=tests, present=tests.paths,
        reports={"p2p_before": _node_report([]), "p2p_after": _node_report([])})
    runner = _Runner(container, tests.runner, 600, adapter=for_framework("vitest"))

    result = runner.pass_to_pass(tests, ignore=("tests/f.test.js",))

    assert result is not None
    assert runner.classify(result).kind != KIND_PASSED


def test_the_merged_exit_code_prefers_a_timeout_over_an_ordinary_failure():
    """`grader._TIMEOUT_EXIT` (124) is branched on BEFORE a failure is read,
    and `timed_out` is a distinct recorded fact. Group 0 of a node deselect
    run exits 1 whenever its post-exclusion scope is empty (measured), so a
    "first non-zero wins" merge would report 1 for a check whose second
    command was killed at the bound."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder(exits=[1, 124])
    runner = _Runner(recorder, ("run",), 600)

    assert runner.run([["a"], ["b"]]).exit_code == 124


def test_the_merged_exit_code_prefers_an_infra_code_over_an_ordinary_failure():
    """`grader._INFRA_EXITS` (125/126/127/137) is read before a failure for
    the same reason: 127 is "no such command", which is an environment
    problem and not a statement about the code under test."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder(exits=[1, 127])
    runner = _Runner(recorder, ("run",), 600)

    assert runner.run([["a"], ["b"]]).exit_code == 127


def test_the_merged_exit_code_is_the_first_non_zero_when_none_outrank():
    """Ordinary codes keep group order, so the first thing that went wrong is
    what the check reports."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder(exits=[0, 1, 2])
    runner = _Runner(recorder, ("run",), 600)

    assert runner.run([["a"], ["b"], ["c"]]).exit_code == 1


def test_a_single_group_returns_the_containers_own_result_object():
    """Identity, so no existing single-command path changes shape: every
    pytest check is one group, and a rebuilt result there would drop whatever
    fields the container's own object carries."""
    from bakeoff.preflight import _Runner

    recorder = _Recorder(exits=[0])
    runner = _Runner(recorder, ("run",), 600)

    result = runner.run([["a"]])

    assert result is recorder.results[0]


def test_last_timeout_s_reads_the_first_group():
    """`last_argv` is `last_argvs[0]`, derived rather than stored, so the two
    cannot drift. Every group of one check carries the same prefix, which is
    what lets the first answer for the check."""
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest", "-q"), 1234)
    runner.run([["a"], ["b"]])

    assert runner.last_argv == runner.last_argvs[0]
    assert runner.last_timeout_s == 1234


def test_the_report_is_deleted_before_every_node_run_and_read_back_after():
    """A config error writes NO file (measured, both frameworks), so a stale
    report from the previous invocation would stand in as this run's evidence
    -- a passing report for a run that never happened. The path is fixed
    because it is an argv element and the gated argv must equal the graded
    one; the `rm` is what makes a fixed name safe.

    PER GROUP, not per check. The five suite checks are eight commands on this
    task -- the p2p-before, p2p-after and scoped checks each carry a group for
    the one file holding a deselection -- and group 1 inheriting group 0's
    report is the same defect one command down: the merge would fold a
    duplicate of the previous group's evidence in as this group's."""
    container, _ = _node_container()

    _preflight_over((container, _FakeTask(tests=container.tests)))

    assert container.report_removals == 8
    assert container.report_reads == 8
    for command in container.commands:
        if command[:1] == ["timeout"]:
            assert "--reporter=json" in command, command


def test_a_node_task_is_not_gated_on_an_interpreter_it_never_declared():
    """D12. `image.python` is REFUSED on a node manifest (`load_task`), and
    `node:22-bookworm-slim` ships no `python` at all -- so the read-back probed
    for an interpreter the task is forbidden from naming, got a non-zero exit,
    and refused every node task in the set with a message about a key the
    author could not have written.

    `container.python = None` is that image verbatim: the probe exits 127. The
    assertion that matters is the third one -- the command is never ISSUED --
    because a gate that ran the probe and ignored the answer would satisfy the
    first two and still pay for it the day the message changed.

    Both evidence keys stay `None`: the gate did not look. Not `""`, which is
    the OTHER absence one branch over -- a probe that ran and answered
    nothing -- and not `"3.12"`, which is what `_declared_python`'s fallback
    would have published as a thing this manifest declared."""
    container, task = _node_container()
    container.python = None

    result = _preflight_over((container, task))

    assert result.ok, result.problems
    assert ["python", "--version"] not in container.commands
    assert result.evidence["python_declared"] is None
    assert result.evidence["python_observed"] is None


def test_a_scoped_run_that_wrote_no_report_measured_neither_node_rule():
    """The config-error shape, which is the one both keys were wrong about.

    Measured on both frameworks: a broken config exits 1 and writes NO file.
    `executed_names(None)` then yields nothing and `Outcome.files_run` is
    absent, so the duplicate rule recorded `[]` and the scope rule recorded
    `[]` -- "measured, nothing found", from a run that measured nothing. The
    verdict was always NO-GO (the scoped run is not KIND_PASSED), but a
    preflight verdict is cached and outlives the code that wrote it, and these
    keys are read by a human comparing task shapes."""
    container, task = _node_container()
    container.reports["scoped"] = None

    result = _preflight_over((container, task))

    assert not result.ok
    assert result.evidence["duplicate_full_names"] is None
    assert result.evidence["scope_files_run"] is None
    assert result.evidence["scope_files_outside"] is None
