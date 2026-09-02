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
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bakeoff.preflight import (
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_FAILURES,
    EXIT_COLLECTION_INTERRUPTED,
    EXIT_NOTHING_COLLECTED,
    EXIT_TESTS_FAILED,
    EXIT_USAGE_ERROR,
    _parse_python_version,
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
                 python="Python 3.12.13"):
        self.commands = []
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
        self.scoped_exit = scoped_exit
        self.grading_exits = dict(grading_exits or {})
        self.f2p_runs = 0
        self.scoped_runs = 0
        #: Override the default red-before / green-after / p2p-before answers.
        #: Defaults stay the healthy exit-1 task so a test states only the one
        #: thing it is about.
        self.f2p_before = f2p_before
        self.f2p_after = f2p_after
        self.p2p_before = p2p_before
        self.p2p_runs = 0
        self.p2p_argvs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, cmd, env=None):
        self.commands.append(list(cmd))
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
        if cmd[:2] == ["git", "rev-parse"]:
            return _Exec(stdout=self.start_sha + "\n")
        if cmd[:2] == ["git", "status"]:
            return _Exec(stdout="")
        if cmd[0] == "git":
            return _Exec()
        if cmd[0] == "timeout":
            return self._timeout(cmd[2:])
        raise AssertionError(f"unscripted exec: {cmd!r}")

    def _timeout(self, argv):
        runner = list(self.tests.runner)
        if argv[: len(runner)] != runner:
            return _Exec(exit_code=self.grading_exits.get(tuple(argv), 0))
        rest = argv[len(runner):]
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


@dataclass(frozen=True)
class _FakeTests:
    paths: tuple = ("tests/",)
    runner: tuple = ("python", "-m", "pytest", "-q")
    f2p: tuple = ("tests/a.py::test_one",)
    p2p: tuple = ()


@dataclass(frozen=True)
class _FakeImage:
    env: dict = field(default_factory=dict)
    python: str = "3.12"


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
    them would be a second thing that can diverge."""
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
    assert len(bounded) == 6, bounded
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
    argv carried no timeout prefix" unrepresentable."""
    from bakeoff.preflight import _Runner

    runner = _Runner(_Recorder(), ("python", "-m", "pytest", "-q"), 1234)
    assert runner.last_timeout_s is None
    runner.run([])
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


def test_the_hypothesis_import_probe_runs_before_the_suite(monkeypatch,
                                                            tmp_path):
    """Same ordering claim as the env read-back, for the other reason a bad
    environment must be reported as itself: a task whose author never
    declared CI is a NO-GO, and that has to be decided before five downstream
    suite failures bury the cause."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   env={"CI": "1"}, hypothesis_importable=True)

    _run_preflight(monkeypatch, tmp_path, task, container)

    import_probe = next(
        i for i, cmd in enumerate(container.commands)
        if len(cmd) >= 3 and cmd[1] == "-c" and "import hypothesis" in cmd[2]
    )
    first_suite = next(i for i, cmd in enumerate(container.commands)
                       if cmd[:1] == ["timeout"])

    assert import_probe < first_suite


def test_the_rg_probe_runs_before_the_suite(monkeypatch, tmp_path):
    """Same ordering claim, for the probe that decides whether an undeclared
    property-based suite is a NO-GO."""
    task = _FakeTask(image=_FakeImage(env={"CI": "1"}))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), env={"CI": "1"},
                                   hypothesis_importable=True,
                                   hypothesis_in_suite=True)

    _run_preflight(monkeypatch, tmp_path, task, container)

    rg_probe = next(i for i, cmd in enumerate(container.commands)
                    if cmd[:1] == ["rg"])
    first_suite = next(i for i, cmd in enumerate(container.commands)
                       if cmd[:1] == ["timeout"])

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
    interpreter the container runs."""
    from bakeoff.preflight import PREFLIGHT_VERSION

    assert PREFLIGHT_VERSION == "7"


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
