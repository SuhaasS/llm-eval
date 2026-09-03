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
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from bakeoff.preflight import (
    BOUNDED_RUN_KEYS,
    EARLY_RETURN_RUNNER_MISMATCH,
    EVIDENCE_KEYS,
    EXIT_ALL_PASSED,
    EXIT_COLLECTION_FAILURES,
    EXIT_COLLECTION_INTERRUPTED,
    EXIT_NOTHING_COLLECTED,
    EXIT_TESTS_FAILED,
    EXIT_USAGE_ERROR,
    PreflightResult,
    _evidence_seed,
    _gitlink_paths,
    _parse_python_version,
    _parse_submodule_status,
    collection_error_modules,
    f2p_modules,
    failed_node_ids,
    preflight,
    preflight_cache_key,
    verdict_matches_key,
)
from bakeoff.tasks import (
    TaskGrading,
    _GRADING_KEYS,
    load_task,
    materialize,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "smoke_task"


@pytest.fixture(scope="module", autouse=True)
def _scaffold_scan_is_stubbed_by_default():
    """`scaffold_only_paths` shells out to `docker create`, and a unit suite
    that reaches a daemon is a unit suite that stops running offline -- and
    on a machine with no `docker` binary it would take the
    `FileNotFoundError` path rather than the one under test. Stubbed to `[]`
    -- "the build wrote nothing into the scaffold" -- for every test in this
    module; the five evidence tests below override it explicitly."""
    mp = pytest.MonkeyPatch()
    mp.setattr("bakeoff.preflight.scaffold_only_paths", lambda *a, **k: [])
    yield
    mp.undo()


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
    def __init__(self, exit_code=0, stdout="", stderr="", duration_ms=0):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        # The field `RunContainer.exec` fills from `time.monotonic()` on the
        # host. Defaulted to 0 rather than omitted so every scripted answer
        # carries it: `preflight._elapsed_s` reads the attribute directly (no
        # `getattr` default), which is what makes a forgotten field a loud
        # `AttributeError` in a test rather than a `None` recorded as if it
        # were a measurement.
        #
        # The other result stubs in this module do NOT need it and must not
        # be given it: `_Recorder`'s `_R` and `_TablessIndex` are driven
        # through `_Runner` or a single git command directly and never reach
        # `preflight()`, so `_elapsed_s` never sees one. If a red test says
        # otherwise, the fix is to give THAT stub the field -- never to relax
        # `_elapsed_s` to a `getattr` default, which is the line D2 exists to
        # hold.
        self.duration_ms = duration_ms


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
                 property_imported=False, property_pinned=False,
                 property_import_rg_exit=None, property_pin_rg_exit=None,
                 python="Python 3.12.13",
                 submodule_status="", submodule_status_exit=0,
                 gitlinks=(), gitmodules_declared=None, gitmodules_exit=None,
                 gitmodules_urls=None, persisted_urls=None,
                 persisted_exit=None,
                 inner_gitlinks=None, own_repo=None,
                 inner_gitmodules=None, inner_gitmodules_exits=None,
                 ls_entries=None, ls_exits=None,
                 reports=None, bare_runner=None, apply_exit=0,
                 durations_ms=None):
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
        #: The node property-scan probes (round-2 item 12): whether a swept
        #: file imports a property-based framework, and whether one pins a
        #: seed. Told apart from `hypothesis_in_suite`/`rg_exit` above by the
        #: PATTERN the `rg` invocation carries, since three probes share the
        #: same `exec` branch now.
        self.property_imported = property_imported
        self.property_pinned = property_pinned
        #: The same "could not answer" overrides `rg_exit` gives the
        #: hypothesis probe, one per property probe since a single shared
        #: `rg_exit` cannot express "only the pin scan failed to answer".
        self.property_import_rg_exit = property_import_rg_exit
        self.property_pin_rg_exit = property_pin_rg_exit
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
        #: round 2 item 16: `.gitmodules`' declared urls, keyed by the same
        #: name `gitmodules_declared`'s paths use (the submodule NAME, which
        #: this stub sets equal to the path -- what `git submodule add`
        #: writes). `None` is "no test scripted a url", the state every
        #: existing test in this module is in, and produces no `.url` record
        #: at all -- not an empty string, which would be a stanza that
        #: DECLARES no url, a different absence.
        self.gitmodules_urls = gitmodules_urls
        #: round 2 item 16: what `_init_submodules` wrote into the run tree's
        #: OWN config, keyed by name. `None` is the same "not scripted"
        #: default, and produces no matching record either.
        self.persisted_urls = persisted_urls
        #: round 2 item 16: `git config --local --get-regexp`'s exit code.
        #: `None` picks git's own: 0 when `persisted_urls` names something, 1
        #: when it does not -- the ordinary "no submodule initialised" case
        #: every test but the failure ones is in. An explicit value is how a
        #: test reaches the >1 branch.
        self.persisted_exit = persisted_exit
        #: round 2 item 18. What `git -C <prefix> ls-files -s -z` answers,
        #: keyed by the submodule path the recursion descended into. Values
        #: are the LEVEL-LOCAL paths that repository's index carries (the gate
        #: joins them onto the prefix itself); a value of `None` is an
        #: `ls-files` that could not be read at all. An ABSENT key is a
        #: repository with no gitlinks of its own -- the flat shape every
        #: existing test in this module is in, and the reason the default
        #: cannot be "the root's own gitlinks", which would file
        #: `vendor/libdep/vendor/libdep` on every one of them.
        self.inner_gitlinks = dict(inner_gitlinks or {})
        #: round 2 item 18. What `git -C <path> rev-parse --show-prefix`
        #: answers, as a verdict: `True` is its own repository (empty prefix),
        #: `False` is a directory inside its parent's (a non-empty prefix,
        #: which is what an UNINITIALISED submodule directory gives), and
        #: `None` is a probe that exited non-zero. `None` for the whole dict
        #: DERIVES the verdict from `submodule_status`' markers -- a `-` line
        #: is uninitialised -- which is what git actually does and what keeps
        #: every existing test in this module scripting nothing new.
        self.own_repo = own_repo
        #: round 2 item 18. A deeper level's `.gitmodules`, keyed by prefix:
        #: a tuple of `(level-local path, url)` pairs. An absent key is a
        #: submodule with no `.gitmodules` of its own (exit 1, git's ordinary
        #: "no key matched"), which is every flat fixture in this module.
        self.inner_gitmodules = dict(inner_gitmodules or {})
        #: round 2 item 18. A deeper level's `.gitmodules` read EXIT, keyed by
        #: prefix, for the >1 branch that must null the whole orphan key
        #: rather than report the levels that did answer.
        self.inner_gitmodules_exits = dict(inner_gitmodules_exits or {})
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
        #: An EXPLICIT `tests.p2p` takes `p2p_argvs`' SELECTED branch, whose
        #: groups all carry a `-t` for the same reason `_f2p_open`'s comment
        #: gives for f2p's own selection -- so the deselect-branch's
        #: `-t`-absent-means-opening heuristic below would read every one of
        #: them as a continuation and serve `{"testResults": []}` for the
        #: whole check. Measured at 8912fb0: the first group also left
        #: `p2p_runs` at 1, so the p2p-AFTER check resolved to the
        #: `p2p_before` key -- a second mis-keying the same match closes.
        #: Recognised by MATCHING against the adapter's own selected-branch
        #: groups, exactly as the f2p check and the scoped check already
        #: are; `None` when `tests.p2p` is empty, since the deselect branch
        #: is what runs then and this check must stay out of its way.
        self._p2p_select_groups = None
        self._p2p_open = False
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
        #: Indexes a RUN on three shapes (pytest; the node deselect branch,
        #: which appends only on its opening group) and a GROUP on the
        #: fourth: the node selected branch (an explicit `tests.p2p`) appends
        #: on every group of the check, since `_p2p_select_groups` matching
        #: fires per group rather than per opening. The two existing
        #: consumers index it as runs, and both are pytest tasks, so nothing
        #: reads it wrong today -- stated here because the meaning is now
        #: shape-dependent with nothing else saying so.
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
        #: What `git apply` of the reference patch answers. The reference fix
        #: FAILING to apply is a real route through the gate -- it skips
        #: `f2p_after_exit`, `p2p_after_exit`, every `grading_*_exit` and all
        #: three scope keys -- and the catch-all `git` branch below answered it
        #: a bare exit 0, so no test in this module could reach it.
        self.apply_exit = apply_exit
        #: What each bounded invocation "took", in milliseconds, keyed by the
        #: run this container already tells apart. String keys for the five
        #: suite runs and the bare-runner probe; a TUPLE key for a grading
        #: argv, exactly as `grading_exits` is keyed, because that branch is
        #: discriminated by argv and not by name. Absent means 0, so every
        #: test that does not care records `0.0` and stays unedited.
        #:
        #: The vocabulary is this container's, not the evidence's: the scoped
        #: run is `scoped` here and `p2p_scoped_after` there. The one test
        #: that asserts exact seconds maps them in its own body, which is
        #: what keeps the mapping readable instead of implied.
        self.durations_ms = dict(durations_ms or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _timed(self, key, result):
        result.duration_ms = self.durations_ms.get(key, 0)
        return result

    def _uninitialised_paths(self):
        """The paths `submodule_status` marks `-`, which is what makes the
        default `rev-parse --show-prefix` answer git's own rather than a
        second thing a test has to remember to script."""
        out = set()
        for line in self.submodule_status.splitlines():
            if not line.strip():
                continue
            _sha, _, tail = line[1:].partition(" ")
            if line[0] == "-":
                out.add(tail.split(" ")[0])
        return out

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
            # Three probes run rg now, told apart by their PATTERN rather than
            # by position -- the hypothesis one, the property-import one and
            # the seed-pin one. `-U` is on the pin probe's argv only, which is
            # what a test asserting "the pin scan was never asked" reads.
            #
            # Dispatched on the PATTERN ELEMENT alone (`cmd[cmd.index("--") -
            # 1]`, `_rg_probe`'s own argv shape: `["rg", "-q", ["-U"]?,
            # pattern, "--", *scanned]`), not on the whole joined argv (round
            # 2 item 12's fix wave, non-blocking finding 7,
            # impl-12-review.md, 2026-09-03): a joined string also matches
            # against the SCANNED PATHS after `--`, so a swept file named
            # e.g. `tests/configureGlobal.spec.ts` would have routed the
            # import probe to the pin branch.
            pattern = cmd[cmd.index("--") - 1]
            if "configureGlobal" in pattern:
                if self.property_pin_rg_exit is not None:
                    return _Exec(exit_code=self.property_pin_rg_exit)
                return _Exec(exit_code=0 if self.property_pinned else 1)
            if "fast-check" in pattern:
                if self.property_import_rg_exit is not None:
                    return _Exec(exit_code=self.property_import_rg_exit)
                return _Exec(exit_code=0 if self.property_imported else 1)
            # rg's three exit codes are the point (0 match, 1 no match,
            # anything else could-not-answer), so `rg_exit` overrides the
            # boolean when a test is about the third one.
            if self.rg_exit is not None:
                return _Exec(exit_code=self.rg_exit)
            return _Exec(exit_code=0 if self.hypothesis_in_suite else 1)
        # round 2 item 18: every read below the root is a `git -C <path>`,
        # because `container.exec` has no workdir argument. The prefix is
        # split off HERE so each branch answers one question and the level it
        # was asked about is a parameter rather than a fourth argv shape.
        prefix = ""
        if cmd[:2] == ["git", "-C"]:
            prefix, cmd = cmd[2], ["git"] + list(cmd[3:])
        if cmd[:3] == ["git", "rev-parse", "--show-prefix"]:
            own = (self.own_repo.get(prefix)
                   if self.own_repo is not None
                   else prefix not in self._uninitialised_paths())
            if own is None:
                return _Exec(exit_code=128,
                             stderr="fatal: not a git repository\n")
            # EMPTY iff the directory is its own repository. The non-empty
            # answer is the path git filtered the enclosing repository's index
            # by, which is the last component of an uninitialised submodule's
            # own path -- measured 2026-09-02, `vendor/deep/` for
            # `vendor/lib/vendor/deep`.
            return _Exec(stdout="" if own
                         else PurePosixPath(prefix).name + "/\n")
        # The three submodule probes come BEFORE every other `git` branch:
        # the catch-all `cmd[0] == "git"` below would swallow all three and
        # answer each of them exit 0 with empty stdout -- which is the
        # healthy-and-empty answer, so nothing would fail.
        if cmd[:2] == ["git", "submodule"]:
            # `--recursive` is ANSWERED, not tolerated (round 2 item 18).
            # Without it git lists only the SUPERPROJECT's own gitlinks --
            # measured 2026-09-02, a level-2 line appears only under
            # `--recursive` -- so a container that returned the same text for
            # both argvs would let the gate drop the flag with every test
            # still green, which is the one reader that can see an empty
            # nested submodule at all.
            lines = self.submodule_status
            if "--recursive" not in cmd:
                lines = "".join(
                    line + "\n" for line in lines.splitlines()
                    if line.strip()
                    and line[1:].partition(" ")[2].split(" ")[0]
                    in (self.gitlinks or ())
                )
            return _Exec(exit_code=self.submodule_status_exit, stdout=lines)
        if cmd[:3] == ["git", "ls-files", "-s"]:
            entries = (self.gitlinks if not prefix
                       else self.inner_gitlinks.get(prefix, ()))
            if entries is None:
                return _Exec(exit_code=128,
                             stderr="fatal: not a git repository\n")
            # `-z` emits `<mode> <sha> <stage>\t<path>` NUL-TERMINATED, so the
            # last record is empty. Written out rather than joined, because
            # that trailing empty is what the parser has to survive.
            return _Exec(stdout="".join(
                f"160000 {'a' * 40} 0\t{path}\0" for path in entries
            ))
        if cmd[:4] == ["git", "config", "-f", ".gitmodules"]:
            # The `-z` is asserted, not tolerated. Without it git emits
            # `<key> <value>` on one line and a submodule NAME containing a
            # space makes `<key>` unsplittable -- so a scripted container that
            # answered either argv the same way would let the parser regress
            # to the space split with every test still green.
            assert "-z" in cmd, cmd
            if prefix:
                # A DEEPER level's `.gitmodules`, scripted per prefix. Absent
                # means the submodule has none of its own, which is git's
                # ordinary exit-1 "no key matched" and the state every flat
                # fixture in this module is in.
                exit_code = self.inner_gitmodules_exits.get(prefix)
                if exit_code is not None and exit_code > 1:
                    return _Exec(exit_code=exit_code,
                                 stderr="fatal: bad config line 1\n")
                pairs = self.inner_gitmodules.get(prefix, ())
                records = "".join(
                    f"submodule.{path}.path\n{path}\0"
                    f"submodule.{path}.url\n{url}\0"
                    for path, url in pairs
                )
                return _Exec(
                    exit_code=(exit_code if exit_code is not None
                               else (0 if records else 1)),
                    stdout=records,
                )
            declared = (self.gitlinks or ()
                        if self.gitmodules_declared is None
                        else self.gitmodules_declared)
            if self.gitmodules_exit is not None and self.gitmodules_exit > 1:
                return _Exec(exit_code=self.gitmodules_exit,
                             stderr="fatal: bad config line 1\n")
            # round 2 item 16: `.url` records, keyed by the same NAME the
            # `.path` records use (this stub sets name == path). `None` --
            # the default -- adds nothing, so every existing test's stdout is
            # byte-identical to what it was before this key existed.
            urls = self.gitmodules_urls or {}
            records = "".join(
                f"submodule.{path}.path\n{path}\0" for path in declared
            ) + "".join(
                f"submodule.{name}.url\n{url}\0" for name, url in urls.items()
            )
            return _Exec(
                # 1 is git config's ORDINARY "no key matched": no .gitmodules
                # at all, or one whose stanzas all have gitlinks.
                exit_code=(self.gitmodules_exit if self.gitmodules_exit
                           is not None else (0 if records else 1)),
                # `<key>\n<value>\0` per record, measured against git 2.50.1.
                # The submodule NAME is the path here (what `git submodule
                # add` writes), so a path carrying a space produces a key
                # carrying one -- which is the shape `-z` exists for.
                stdout=records,
            )
        if cmd[:3] == ["git", "config", "--local"]:
            # round 2 item 16: what `_init_submodules` actually wrote into
            # THIS tree's own config -- told apart from the tracked
            # `.gitmodules` blob above by `--local` rather than `-f
            # .gitmodules`. `None` -- the default -- produces no record and
            # exit 1, git's own "no key matched" for a tree with no
            # initialised submodule, which is the state every existing test
            # in this module is in.
            assert "-z" in cmd, cmd
            if prefix:
                # A deeper level's OWN config. Nothing in this module scripts
                # one, so it answers git's ordinary "no key matched" -- which
                # leaves every nested entry's `url_persisted` `None`, honestly,
                # rather than repeating the root's answer one level down.
                return _Exec(exit_code=1)
            urls = self.persisted_urls or {}
            if self.persisted_exit is not None and self.persisted_exit > 1:
                return _Exec(exit_code=self.persisted_exit,
                             stderr="fatal: unable to read config file\n")
            return _Exec(
                exit_code=(self.persisted_exit if self.persisted_exit
                           is not None else (0 if urls else 1)),
                stdout="".join(
                    f"submodule.{name}.url\n{url}\0"
                    for name, url in urls.items()
                ),
            )
        if cmd[:2] == ["git", "rev-parse"]:
            return _Exec(stdout=self.start_sha + "\n")
        if cmd[:2] == ["git", "status"]:
            return _Exec(stdout="")
        if cmd[:2] == ["git", "apply"]:
            # The stderr is conditional on the exit code, like the report
            # `cat` branch above: a fixture that says "does not apply" while
            # exiting 0 is one a later test can read the wrong way round.
            # `preflight` reads this stderr only under `applied.exit_code != 0`.
            return _Exec(exit_code=self.apply_exit,
                         stderr="error: patch does not apply\n"
                         if self.apply_exit else "")
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
            return self._timed(
                "bare_runner",
                self.bare_runner if self.bare_runner is not None else _Exec(),
            )
        if cmd[0] == "timeout":
            return self._timeout(cmd[2:])
        raise AssertionError(f"unscripted exec: {cmd!r}")

    def _timeout(self, argv):
        runner = list(self.tests.runner)
        if argv[: len(runner)] != runner:
            return self._timed(
                tuple(argv),
                _Exec(exit_code=self.grading_exits.get(tuple(argv), 0)),
            )
        rest = argv[len(runner):]
        if self.reports is not None:
            return self._node_timeout(rest)
        if rest == list(self.tests.f2p):
            self.f2p_runs += 1
            if self.f2p_runs == 1:  # red before the reference fix
                return self._timed(
                    "f2p_before",
                    self.f2p_before or _Exec(
                        exit_code=EXIT_TESTS_FAILED,
                        stdout="".join(f"FAILED {n}\n" for n in self.tests.f2p),
                    ),
                )
            return self._timed("f2p_after", self.f2p_after or _Exec())
        if any(arg in self.tests.paths for arg in rest):
            self.scoped_runs += 1
            return self._timed("scoped", _Exec(exit_code=self.scoped_exit))
        self.p2p_runs += 1
        self.p2p_argvs.append(list(rest))
        if self.p2p_runs == 1 and self.p2p_before is not None:
            return self._timed("p2p_before", self.p2p_before)
        key = "p2p_before" if self.p2p_runs == 1 else "p2p_after"
        return self._timed(key, _Exec())

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
        of its scripted one). The DESELECT-branch p2p checks are the one
        place the `-t` heuristic still applies -- that branch's group 0
        carries no `-t`, and `scope=()` never omits it. The SELECTED branch
        (an explicit `tests.p2p`) has no such group, so it is matched
        against `_p2p_select_groups` above.
        """
        from bakeoff.runners import for_framework

        adapter = for_framework(self.tests.framework)
        # Round 2 item 12's fix wave (`_Runner.run` now emits
        # `report_args` FIRST rather than last, 2026-09-03): every group's
        # `rest` opens with the fixed 2-element report-args prefix, and the
        # matching below is front-anchored against the adapter's OWN
        # argv-building calls, which build no report args at all. Strip the
        # known prefix once, here, rather than teaching every match below
        # to skip it -- a mismatch is a real defect (report args changed
        # shape, or a group somehow carries none), so this asserts loudly
        # instead of silently matching the wrong check.
        report_args = adapter.report_args(adapter.report_path())
        assert rest[:len(report_args)] == report_args, (rest, report_args)
        rest = rest[len(report_args):]
        select = adapter.select_argvs(tuple(self.tests.f2p))
        if self._scoped_groups is None:
            self._scoped_groups = adapter.p2p_argvs(
                selected=(), scope=tuple(self.tests.paths),
                deselected=tuple(self.tests.f2p), ignored=())
        scoped_index = next(
            (i for i, group in enumerate(self._scoped_groups)
             if group and rest[:len(group)] == group), None)
        if self._p2p_select_groups is None and self.tests.p2p:
            self._p2p_select_groups = adapter.p2p_argvs(
                selected=tuple(self.tests.p2p), scope=(),
                deselected=(), ignored=())
        p2p_select_index = next(
            (i for i, group in enumerate(self._p2p_select_groups or ())
             if group and rest[:len(group)] == group), None)
        if any(group and rest[:len(group)] == group for group in select):
            # Every group of one selection belongs to the same check; the
            # check opens on the FIRST of them.
            self._p2p_open = False
            opening = not self._f2p_open
            if opening:
                self.f2p_runs += 1
                self._f2p_open = True
            key = "f2p_before" if self.f2p_runs == 1 else "f2p_after"
        elif p2p_select_index is not None:
            # An EXPLICIT `tests.p2p` takes `p2p_argvs`' SELECTED branch,
            # whose groups all carry a `-t` for the same reason `_f2p_open`'s
            # own comment gives -- so the deselect-branch heuristic below
            # would read every one of them as a continuation. Matched
            # against the adapter's own groups instead, exactly as the f2p
            # check is.
            self._f2p_open = False
            opening = not self._p2p_open
            if opening:
                self.p2p_runs += 1
                self._p2p_open = True
            self.p2p_argvs.append(list(rest))
            key = "p2p_before" if self.p2p_runs == 1 else "p2p_after"
        else:
            self._f2p_open = False
            self._p2p_open = False
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
        return self._timed(key, _Exec(exit_code=_node_exit(report)))


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
    # `preflight` now inspects the image's own labels, host-side. This suite is
    # documented as offline and `verify_logger.py` runs it as the section 6.6
    # gate, so the subprocess is faked here rather than 58 times; the tests that
    # care override it afterwards. `{}` is the honest default: an image that
    # exists and carries no bakeoff.base.* labels.
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: {})
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
    assert result.evidence["p2p_scoped_after_exit"] is None
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


def test_the_bound_is_null_when_no_command_ever_ran(monkeypatch, tmp_path):
    """The non-pytest `tests.runner` refusal returns before the container is
    even entered, so a manifest value written here would be a claim about a
    run that did not happen.

    `None`, not an absent key. Absence is NOT honest here, whatever an earlier
    version of this docstring said: it renders identically to a verdict
    written by a gate that predates `budget.suite_timeout_s` at all
    (`PREFLIGHT_VERSION` 6), which is the one thing the version string exists
    to let a reader rule out."""
    task = _FakeTask(tests=_FakeTests(runner=("go", "test", "./...")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    assert result.evidence["suite_timeout_s"] is None


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


# --- round 2, item 13: the base image says what it is -------------------------


def test_the_image_labels_are_recorded_beside_the_interpreter(monkeypatch, tmp_path):
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.11.16")
    labels = {
        "bakeoff.base.runtime": "python",
        "bakeoff.base.version": "3.11",
        "bakeoff.base.dockerfile_sha": "a" * 64,
    }
    monkeypatch.setattr("bakeoff.preflight.RunContainer", lambda **kwargs: container)
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: labels)

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.ok, result.problems
    assert result.evidence["base_image_labels"] == labels
    assert result.evidence["python_observed"] == "Python 3.11.16"


def test_a_label_that_disagrees_with_the_interpreter_is_recorded_and_not_refused(
    monkeypatch, tmp_path
):
    """Pins that the label is not a gate and the read-back is the authority."""
    task = _FakeTask(image=_FakeImage(python="3.13"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.13.15")
    labels = {
        "bakeoff.base.runtime": "python",
        "bakeoff.base.version": "3.11",
        "bakeoff.base.dockerfile_sha": "a" * 64,
    }
    monkeypatch.setattr("bakeoff.preflight.RunContainer", lambda **kwargs: container)
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: labels)

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.ok, result.problems
    assert result.evidence["base_image_labels"] == labels
    assert result.evidence["python_observed"] == "Python 3.13.15"


def test_a_label_that_agrees_does_not_rescue_a_wrong_interpreter(
    monkeypatch, tmp_path
):
    """The converse: the label cannot vouch for the interpreter."""
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.13.15")
    labels = {
        "bakeoff.base.runtime": "python",
        "bakeoff.base.version": "3.11",
        "bakeoff.base.dockerfile_sha": "a" * 64,
    }
    monkeypatch.setattr("bakeoff.preflight.RunContainer", lambda **kwargs: container)
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: labels)

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert not result.ok
    assert any("3.11" in p and "3.13" in p for p in result.problems)


def test_the_mismatch_refusal_names_a_remedy_that_still_works_after_the_skip(
    monkeypatch, tmp_path
):
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.13.15")

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert not result.ok
    problem = next(p for p in result.problems if "3.11" in p and "3.13" in p)
    assert "docker rmi bakeoff-eval-agent:base-python-3.11" in problem
    assert "--pull --no-cache" in problem


def test_only_the_bakeoff_base_keys_are_recorded(monkeypatch, tmp_path):
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.11.16")
    labels = {
        "bakeoff.base.runtime": "python",
        "bakeoff.base.version": "3.11",
        "bakeoff.base.dockerfile_sha": "a" * 64,
        "maintainer": "someone",
        "org.opencontainers.image.source": "https://example.invalid",
    }
    monkeypatch.setattr("bakeoff.preflight.RunContainer", lambda **kwargs: container)
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: labels)

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.evidence["base_image_labels"] == {
        "bakeoff.base.runtime": "python",
        "bakeoff.base.version": "3.11",
        "bakeoff.base.dockerfile_sha": "a" * 64,
    }


def test_an_image_with_no_bakeoff_labels_records_an_empty_set_not_a_null(
    monkeypatch, tmp_path
):
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.11.16")
    monkeypatch.setattr("bakeoff.preflight.RunContainer", lambda **kwargs: container)
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: {})

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.evidence["base_image_labels"] == {}
    assert result.evidence["base_image_labels"] is not None


def test_a_label_probe_that_could_not_answer_is_a_null_not_an_empty_set(
    monkeypatch, tmp_path
):
    task = _FakeTask(image=_FakeImage(python="3.11"))
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), python="Python 3.11.16")
    monkeypatch.setattr("bakeoff.preflight.RunContainer", lambda **kwargs: container)
    monkeypatch.setattr("bakeoff.preflight.image_labels", lambda image: None)

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.evidence["base_image_labels"] is None
    assert result.ok, result.problems


def test_the_label_evidence_is_none_on_the_path_that_never_starts_a_container(
    tmp_path
):
    task = _FakeTask(tests=_FakeTests(runner=("nose",)),
                     image=_FakeImage(python="3.11"))

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert result.evidence["base_image_labels"] is None
    assert result.evidence["python_observed"] is None
    assert result.evidence["early_return"] == EARLY_RETURN_RUNNER_MISMATCH


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
    served forever.

    16 -> 17 is one evidence schema (round 2 item 5, 2026-09-03), and it moves
    for the same reason 15 -> 16 did: the SHAPE, not the assertions. Twenty of
    the forty-two keys were written where they were measured and are simply
    absent from any path that did not measure them -- four of those
    (`grading_build_exit`, `grading_typecheck_exit`, `grading_lint_exit`,
    `scope_prefixes_absent`) from the ordinary healthy GO verdict. Absent
    renders identically to "written by a gate too old to have this key", which
    is the one thing this string exists to let a reader rule out. Since 17
    every verdict carries every key of `EVIDENCE_KEYS`, `None` where the gate
    did not look, and `early_return` names the pre-container refusal. No
    verdict moves: a cached 16 PASS was a PASS for the same reasons, so what
    the bump re-runs is the READING and not the judgement.

    17 -> 18 is the gate recording how long its own bounded runs took (round 2
    item 7, 2026-09-03), and it is the SHAPE again: a v17 verdict carries
    `suite_timeout_s` -- the bound the argv held -- beside nothing at all
    saying what the run under it cost, so every `budget.suite_timeout_s` in
    the task set was sized from a number measured by hand in a shell, outside
    the image the gate runs in. Since 18 every verdict carries
    `bounded_run_durations_s` -- one entry per bounded invocation, `null` for
    a run that did not happen -- and `bounded_run_duration_max_s` beside it.
    No verdict moves: a cached 17 PASS was a PASS for the same reasons, so
    what the bump re-runs is the MEASUREMENT and not the judgement.

    18 -> 19 is a refusal a node manifest can trip and a pytest one cannot
    (round 2 item 11, 2026-09-03): a declared f2p or p2p id that names MORE
    THAN ONE test in its own file. A node id is `<file>::<fullName>` with no
    positional index, so two identically titled tests in one file are the
    same id -- measured 2026-09-02, an exact anchored `-t` runs both (one
    passed and one failed in the same run) and the deselection skips both, so
    the red-before check was satisfied by whichever one fails, the
    green-after check by both passing, and no count downstream disagreed. A
    cached 18 PASS on a NODE task is stale: it was taken by a gate that could
    not see the shape. A cached NO-GO is unaffected -- nothing this version
    adds turns a NO-GO into a GO -- and no PYTEST verdict moves at all, since
    `pytest_adapter.duplicate_ids` is `{}` as a claim its node ids back. The
    new evidence key `same_file_duplicate_ids` is `{}` on every verdict this
    version writes for a task at least one of whose node runs reported, and
    `None` where nothing counted.

    19 -> 20 is a property-based determinism check for the node frameworks
    (round 2 item 12, 2026-09-03). Measured 2026-09-02 against fast-check
    3.23.2 and 2.25.0, the seed defaults to
    `Date.now() ^ (Math.random() * 0x100000000)` when nothing configures one,
    the package reads no environment variable anywhere, and ten fresh vitest
    runs of one property over unchanged code gave `0 0 0 1 1 0 0 0 0 0` -- so
    a node task whose p2p-before sweep runs an unpinned fast-check/jest-fuzz/
    jsverify suite was certified on whichever draw the gate happened to get.
    Since 20 the gate scans the p2p-before run's `Outcome.files_run` and
    refuses a task where a swept file imports one of those frameworks and
    nothing swept pins a seed with `configureGlobal(... seed: ...)`. There is
    no `image.env` lever for this one, unlike hypothesis's `CI=1`: the one
    external seed lever that works, `NODE_OPTIONS` preloading a module that
    calls `fc.configureGlobal`, is defeated by jest's module registry. A
    cached PASS under 19 on a NODE task whose sweep runs a property-based
    suite is stale: it was taken by a gate that could not see the shape. A
    cached NO-GO is unaffected -- nothing this version adds turns a NO-GO
    into a GO -- and no PYTEST verdict moves at all, since
    `PytestAdapter.property_scan` returns `None`. The three new evidence
    keys (`property_framework_imported_by_suite`,
    `property_framework_seed_pinned`, `property_scan_files`) are `None` on
    every verdict this version writes where the node scan did not run.

    20 -> 21 is the round-2 item 12 fix wave's blocking finding 1
    (impl-12-review.md, 2026-09-03): the 19->20 refusal's own worked remedy
    -- add the framework's ignore flag to `tests.runner` -- did not clear
    the gate. `_Runner.run` built every check's argv as `tests.runner +
    <that check's own suffix>`, and a manifest-declared array-valued flag
    (jest's `--testPathIgnorePatterns`) at the tail of `tests.runner`
    swallowed the next bare token -- the f2p SELECT check's own file
    positional and the scoped p2p run's own scope positional, both
    measured turning into more ignore patterns instead of a selection.
    Since 21 `_Runner.run` emits `adapter.report_args` FIRST rather than
    last: both spellings open with a token starting with `-`, which ends a
    yargs array, so a trailing manifest flag can now only ever swallow
    report args this code does not read back. A cached PASS under 20 on a
    node task whose `tests.runner` carries a trailing array-valued flag is
    stale. No PYTEST verdict moves, since `PytestAdapter.report_args` is
    `[]` and the reorder is inert on an empty list.

    21 -> 22 is `base_image_labels` (round 2 items 13+15, 2026-09-03). A
    verdict cached under 21 was written by a gate that recorded what the
    interpreter answered and never what the image CLAIMED to be: the base
    tag is local and mutable, and before this round the driver's own
    unconditional `docker build -t` silently repaired a hand-mutated tag by
    a cache-hit retag -- measured 2026-09-02, which is how a probe of this
    very read-back recorded PASS on a base it had deliberately broken.
    Since 22 the gate reads the task image's own `bakeoff.base.*` labels
    (inherited verbatim from its base, since `render_dockerfile` emits no
    LABEL of its own) and records them BESIDE `python_observed` -- never in
    place of it, and never refused on: the interpreter read-back stays the
    sole authority on whether a python task may run. No verdict moves: a
    cached 21 PASS was a PASS for the same reasons, so what the bump adds
    is a second, independent fact a reader can compare the first against.

    22 -> 23 is round 2, item 14. Three evidence keys join the schema --
    `build_generated_paths`, `build_generated_count`, `build_generated_state`
    -- recording what an `image.build` step wrote into the image's `/repo`
    that the run tree, bind-mounted over it in full at container start, does
    not have. AND the gate's GO/NO-GO changes: the bare-runner probe no
    longer accepts exit 1 from `python -m pytest --co -q`, which under
    collect-only cannot mean a failing test -- measured 2026-09-02, a
    module-level import error and a syntax error in a test file both exit 2,
    a conftest.py import error exits 4, and of seven python task images only
    the one whose suite imports a module the image build generated answers
    1. A verdict cached under 22 may describe a task this gate now refuses,
    so a cached PASS is stale and must be re-gated; a cached NO-GO is
    unaffected, since nothing here turns a NO-GO into a GO.

    23 -> 24 is round 2, item 16 (relative `.gitmodules` urls). Adds
    `url_declared` and `url_persisted` to every `submodules` entry -- the
    first read from the tree's `.gitmodules`, the second from the run tree's
    own `--local` config -- and one problem for a relative url that reached
    the run tree unresolved. Both are PER-ENTRY fields inside the existing
    `submodules` key, so `EVIDENCE_KEYS` does not move. A verdict cached
    under 23 was written by a gate that recorded neither key, so a reader of
    a stored blob cannot tell "this task's submodule url is absolute" from
    "this gate did not look." GO/NO-GO is unchanged for every task in the
    corpus, since none declares a relative submodule url; what moved is what
    a stored verdict's evidence can be read to say.

    24 -> 25 is round 2, item 18 (nested submodules). Both submodule readers
    recurse: `git submodule status` gained `--recursive`, and the
    authoritative path set is now one `git ls-files -s -z` per initialised
    level rather than one at the repository root. A verdict cached under 24
    was written by a gate that could not NAME a level-2 submodule at all --
    measured 2026-09-02 with level 1 populated and level 2 empty, the
    superproject's `git status --porcelain`, the inner's own, `git diff HEAD`
    and non-recursive `git submodule status` are ALL clean, and only
    `--recursive` says anything. The evidence shape moves for every task,
    nested or not: each `submodules` entry gains `depth`, and
    `submodules_orphaned` now means "every initialised level was read" where
    it meant "the root was read". `EVIDENCE_KEYS` does not move -- `depth` is
    per-entry -- and no flat task's GO/NO-GO changes."""
    from bakeoff.preflight import PREFLIGHT_VERSION

    assert PREFLIGHT_VERSION == "25"


# --- round 2, item 14: what an image.build step wrote into the scaffold ------


def test_the_gate_records_what_the_build_wrote_into_the_scaffold(
    monkeypatch, tmp_path
):
    """sqlglot-6927's measured shape (2026-09-02): the build's
    `setuptools_scm` step writes `sqlglot/_version.py` into the image's
    `/repo`, and the run tree -- bind-mounted over it at container start --
    never has it."""
    monkeypatch.setattr(
        "bakeoff.preflight.scaffold_only_paths",
        lambda *a, **k: ["sqlglot/_version.py"],
    )
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["build_generated_paths"] == ["sqlglot/_version.py"]
    assert result.evidence["build_generated_count"] == 1
    assert result.evidence["build_generated_state"] == "scanned"


def test_the_gate_does_not_refuse_a_task_whose_build_wrote_into_the_scaffold(
    monkeypatch, tmp_path
):
    """"Generated" and "required" are different claims, and no property of a
    path name separates them -- sqlglot-6927 is a perfectly good task. This
    measurement refuses nothing; what refuses is the bare-runner exit-code
    check below, keyed on the CONSEQUENCE and not the path list."""
    monkeypatch.setattr(
        "bakeoff.preflight.scaffold_only_paths",
        lambda *a, **k: ["sqlglot/_version.py"],
    )
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok, result.problems
    assert not any("_version.py" in p for p in result.problems)


def test_a_scan_that_could_not_run_says_so_rather_than_reporting_nothing(
    monkeypatch, tmp_path
):
    """A missing docker binary, a daemon that went away mid-gate, a malformed
    tar -- each is a statement about this MACHINE, not about the task, and
    turning any of them into a NO-GO would let a diagnostic break a gate that
    was otherwise green (D4). The state string is what keeps the containment
    from being silent."""
    import bakeoff.images as images

    def _raise(*a, **k):
        raise images.ImageError("daemon gone")

    monkeypatch.setattr("bakeoff.preflight.scaffold_only_paths", _raise)
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["build_generated_paths"] is None
    assert result.evidence["build_generated_count"] is None
    assert result.evidence["build_generated_state"].startswith("failed: ")
    assert "daemon gone" in result.evidence["build_generated_state"]
    assert result.ok, result.problems


def test_a_long_list_is_truncated_and_the_count_survives(monkeypatch, tmp_path):
    """`_BUILD_GENERATED_LIMIT` bounds the VERDICT, not the transfer (D3): a
    build that ran `npm install` inside `/repo` must not put twenty thousand
    paths in a preflight blob."""
    generated = [f"generated/{i:04d}.py" for i in range(101)]
    monkeypatch.setattr(
        "bakeoff.preflight.scaffold_only_paths", lambda *a, **k: generated,
    )
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",))

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.evidence["build_generated_paths"] == generated[:100]
    assert len(result.evidence["build_generated_paths"]) == 100
    assert result.evidence["build_generated_count"] == 101
    assert result.evidence["build_generated_state"] == (
        "truncated: 101 paths, first 100 listed"
    )


def test_the_runner_gate_early_return_says_the_scan_was_not_attempted(
    monkeypatch, tmp_path
):
    """The early return starts no container and pays no docker call -- a
    manifest that is already refused should not pay for a scan whose result
    it cannot use. The seeded `"not_attempted"` is left as is rather than
    the runner-gate branch writing its own reason string, unlike
    `bare_runner_skipped` one seed above it: that triple has two distinct
    skip reasons to tell apart and this scan has exactly one."""
    calls = []
    monkeypatch.setattr(
        "bakeoff.preflight.scaffold_only_paths",
        lambda *a, **k: calls.append(1) or [],
    )
    task = _FakeTask(tests=_FakeTests(runner=("go", "test", "./...")))

    result = preflight(task, image="sha256:x", repo_path=tmp_path,
                       start_sha="s" * 40)

    assert calls == []
    assert result.evidence["build_generated_state"] == "not_attempted"
    assert result.evidence["build_generated_paths"] is None
    assert result.evidence["build_generated_count"] is None


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


def test_a_bare_pytest_that_cannot_start_is_refused(monkeypatch, tmp_path):
    """pytest-10210's measured shape (round 2, item 14; 2026-09-02): the
    suite imports a module the image build generated into `/repo`, which the
    run tree does not have, and the interpreter dies before pytest starts.
    Under `--co` no test is ever executed, so 1 cannot mean a failing test --
    it means `python -m pytest` could not start in this image, which is the
    command the agent will naturally type."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        bare_runner=_Exec(
            exit_code=EXIT_TESTS_FAILED,
            stderr=(
                "  File \"<frozen runpy>\", line 112\n"
                "ModuleNotFoundError: No module named '_pytest._version'\n"
            ),
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    assert result.ok is False
    assert any(
        "exited 1, which under --co cannot mean a failing test" in p
        for p in result.problems
    ), result.problems
    assert result.evidence["bare_runner_exit"] == EXIT_TESTS_FAILED


def test_the_refusal_quotes_the_last_line_of_stderr_not_the_first(
    monkeypatch, tmp_path
):
    """Measured: the usage-error branch's `": error:"` selector matches
    nothing in this stderr, which is why this branch selects the LAST
    non-empty line rather than the first -- the runpy frame names no cause,
    and the `ModuleNotFoundError` line does."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        bare_runner=_Exec(
            exit_code=EXIT_TESTS_FAILED,
            stderr=(
                "  File \"<frozen runpy>\", line 112\n"
                "ModuleNotFoundError: No module named '_pytest._version'\n"
            ),
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    joined = " ".join(result.problems)
    assert (
        "Last line of stderr: ModuleNotFoundError: No module named "
        "'_pytest._version'" in joined
    )
    assert "<frozen runpy>" not in joined


def test_the_refusal_says_so_when_stderr_is_empty(monkeypatch, tmp_path):
    """chimera-228's bare `--co` exits 4 with an empty stderr (measured), so
    an exit that leaves nothing behind is a real shape on this corpus, not a
    hypothetical -- and a refusal whose only human-readable clue renders as a
    bare full stop is exactly the shape this file's other messages are
    written against."""
    task = _FakeTask()
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        bare_runner=_Exec(exit_code=EXIT_TESTS_FAILED, stderr=""),
    )

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    joined = " ".join(result.problems)
    assert "stderr was empty" in joined
    assert "Last line of stderr: ." not in joined


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
         "declared_unneeded": False, "empty": False, "depth": 1,
         # round 2 item 16: neither url is scripted by this fixture, so both
         # are the "not measured" None -- not an empty string, which would
         # claim a stanza that declares no url.
         "url_declared": None, "url_persisted": None}
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
         "marker": "-", "declared_unneeded": False, "empty": True,
         "depth": 1,
         "url_declared": None, "url_persisted": None}
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
         "marker": "+", "declared_unneeded": False, "empty": True,
         "depth": 1,
         "url_declared": None, "url_persisted": None}
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
         "declared_unneeded": False, "empty": True, "depth": 1,
         # round 2 item 16: the failed .gitmodules read leaves BOTH
         # `path_by_name` and `url_by_name` empty (hoisted above the
         # exit-code branch precisely so this stays `None` rather than
         # raising `UnboundLocalError`), so this entry's url cannot be
         # joined to a name and reads as "not measured", not "declares no
         # url".
         "url_declared": None, "url_persisted": None}
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
         "marker": "-", "declared_unneeded": True, "empty": True,
         "depth": 1,
         "url_declared": None, "url_persisted": None}
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


# --- round 2 item 16: submodule url evidence ----------------------------------


def test_every_submodule_entry_carries_both_url_keys(monkeypatch, tmp_path):
    """The pass direction: an absolute url, declared and persisted alike."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
        gitmodules_urls={"vendor/libdep": "https://github.com/org/libdep.git"},
        persisted_urls={"vendor/libdep": "https://github.com/org/libdep.git"},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    entry = result.evidence["submodules"][0]
    assert entry["url_declared"] == "https://github.com/org/libdep.git"
    assert entry["url_persisted"] == "https://github.com/org/libdep.git"


def test_the_url_keys_are_present_even_when_the_name_cannot_be_joined(
        monkeypatch, tmp_path):
    """A gitlink `.gitmodules` names no stanza for at all: both url keys read
    `None`, honestly, since neither file has anything to say about it --
    `name_by_path.get` returning `None` is what this pins. Not an orphan (the
    reverse direction: a stanza with no gitlink) and not refused: preflight
    only OBSERVES this pairing, it never judges it."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",), gitmodules_declared=(),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    entry = result.evidence["submodules"][0]
    assert entry["url_declared"] is None
    assert entry["url_persisted"] is None


def test_an_unreadable_run_tree_config_nulls_the_persisted_url_and_files_a_problem(
        monkeypatch, tmp_path):
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
        persisted_exit=2,
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    entry = result.evidence["submodules"][0]
    assert entry["url_persisted"] is None
    assert any(
        "reading the run tree's submodule urls at the repository root failed"
        in p
        for p in result.problems
    )


def test_an_absent_run_tree_config_is_not_a_problem(monkeypatch, tmp_path):
    """N4: exit 1 is git config's ORDINARY "no key matched" for a tree with
    no initialised submodule -- the state every other test in this section
    is already in."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    entry = result.evidence["submodules"][0]
    assert entry["url_persisted"] is None
    assert not any("submodule urls failed" in p for p in result.problems)


def test_an_unresolved_relative_url_in_the_run_tree_is_a_problem(
        monkeypatch, tmp_path):
    """The new assertion (D8), and the one that would have caught this
    item's own defect: a relative url that reached the run tree unresolved."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
        gitmodules_urls={"vendor/libdep": "../libdep"},
        persisted_urls={"vendor/libdep": "../libdep"},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("was never resolved" in p for p in result.problems)


def test_a_resolved_relative_url_in_the_run_tree_is_not_a_problem(
        monkeypatch, tmp_path):
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
        gitmodules_urls={"vendor/libdep": "../libdep"},
        persisted_urls={
            "vendor/libdep": "https://github.com/org/libdep.git"},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    assert not any("was never resolved" in p for p in result.problems)


def test_a_relative_url_resolved_to_a_local_path_is_not_a_problem(
        monkeypatch, tmp_path):
    """The fixture branch of the predicate: a persisted url starting with
    `/` (a local path, exactly what a test fixture's url looks like under
    `_SUBMODULE_URL_PREFIX`'s relaxation) must not be reported as
    unresolved."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
        gitmodules_urls={"vendor/libdep": "../libdep"},
        persisted_urls={"vendor/libdep": "/cache/lib.git"},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    assert not any("was never resolved" in p for p in result.problems)


def test_the_orphan_set_is_unchanged_by_the_widened_regex(
        monkeypatch, tmp_path):
    """The `.gitmodules` regex now matches `.path` AND `.url`; `declared_paths`
    is still built from exactly the `path` half, so the orphan set this
    section's other tests already pin is byte-identical -- except on one
    shape, a stanza that repeats its own `path` key. `--get-regexp` returns
    BOTH records (measured 2026-09-03, git 2.50.1), so the old union filed
    both values while `path_by_name[name] = value` is last-wins. Last-wins is
    git's own reading and `derive_submodules`' too, so the two parses now
    agree where before they could not."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        gitmodules_declared=("vendor/libdep", "vendor/gone"),
        gitmodules_urls={
            "vendor/libdep": "https://github.com/org/libdep.git",
            "vendor/gone": "https://github.com/org/gone.git",
        },
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.ok, result.problems
    assert result.evidence["submodules_orphaned"] == ["vendor/gone"]


def test_an_unreadable_gitmodules_nulls_both_url_keys_without_crashing(
        monkeypatch, tmp_path):
    """The `UnboundLocalError` finding, pinned directly: `path_by_name` and
    `url_by_name` are hoisted ABOVE the exit-code branch precisely so the
    else path -- an unreadable `.gitmodules` -- leaves both dicts empty
    instead of undefined. No traceback; both keys read `None` on every
    entry, `submodules_orphaned` is `None`, and the only problem filed is
    the existing orphan-read one."""
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
    entry = result.evidence["submodules"][0]
    assert entry["url_declared"] is None
    assert entry["url_persisted"] is None
    assert result.evidence["submodules_orphaned"] is None
    assert sum(1 for p in result.problems if ".gitmodules" in p) == 1


def test_an_unreadable_run_tree_config_does_not_also_claim_the_url_was_unresolved(
        monkeypatch, tmp_path):
    """The `is not None` half of the predicate, isolated: a `.gitmodules`
    relative url PLUS a failed `--local` read must file exactly the read
    failure, never ALSO the "never resolved" problem -- that would be a
    positive claim about something this gate did not look at."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
        gitmodules_urls={"vendor/libdep": "../libdep"},
        persisted_exit=2,
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    url_problems = [p for p in result.problems if "submodule urls" in p
                    or "was never resolved" in p]
    assert len(url_problems) == 1
    assert ("reading the run tree's submodule urls at the repository root "
            "failed") in url_problems[0]


def test_a_declared_unneeded_submodule_with_a_relative_url_is_a_go(
        monkeypatch, tmp_path):
    """The repository class items 2 and 16 exist together to reopen: a
    declared-unneeded submodule's relative url is never registered by
    `_init_submodules`, so `url_persisted` reads `None` -- not measured --
    and that must not be reported as "never resolved"."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=("-" + "0" * 40 + " vendor/libdep\n"),
        gitmodules_urls={"vendor/libdep": "../libdep"},
    )

    result = _run_preflight(monkeypatch, tmp_path,
                            _unneeded_task("vendor/libdep"), container)

    assert result.ok, result.problems
    entry = result.evidence["submodules"][0]
    assert entry["url_persisted"] is None
    assert not any("was never resolved" in p for p in result.problems)


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


def _node_container(*, f2p_ran=True, f2p_twice=False, p2p_twice=False,
                    missing=(), scope_names=None, scope_files=None,
                    p2p=(), framework="vitest", runner=None,
                    p2p_before_files=(), **container_kwargs):
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

    `f2p_twice` / `p2p_twice` put the declared id at TWO assertions inside
    the SAME `testResults` entry -- M11.2's measured shape, since one file is
    one entry. `missing` blanks a run's report entirely (the measured
    broken-config shape: exit 1, no file), which `_ScriptedContainer` already
    answers the way the real container does (`cat` exit 1).

    `scope_names` and `scope_files` both describe the scoped p2p run's report,
    from the two directions the two assertions read it: `scope_names` names
    `(file, fullName)` pairs directly, and `scope_files` names files and gives
    each a distinct title so the duplicate-name rule stays quiet and the scope
    rule is the only thing under test.

    `p2p_before_files` appends extra file entries (each with one passing
    test) to the P2P-BEFORE REPORT ONLY -- what puts a file such as
    `tests/properties.ts` into that run's `files_run` without touching
    `tests.paths`, the f2p report, or the scoped report. This is the property
    scan's scope (round-2 item 12): the scan reads `Outcome.files_run` off
    the p2p-before run, not the declared paths.

    `**container_kwargs` is forwarded verbatim into the `_ScriptedContainer`
    call below, which is how the property-scan parameters
    (`property_imported`, `property_pinned`, `property_import_rg_exit`,
    `property_pin_rg_exit`) and a `present=()` override reach it.
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
    p2p_entries = [
        (node_id.partition("::")[0],
         [("passed", node_id.partition("::")[2])] * (2 if p2p_twice else 1))
        for node_id in p2p_ids
    ]
    p2p_report = _node_report(p2p_entries)
    # `p2p_before_files` is appended to the BEFORE report only, each with one
    # passing assertion -- extra files the p2p-before sweep "loaded" without
    # widening `tests.paths` or the after report.
    p2p_before_report = _node_report(
        p2p_entries
        + [(path, [("passed", "a property holds")])
           for path in p2p_before_files]
    )
    reports = {
        "f2p_before": _node_report(
            [("tests/a.test.js",
              [("failed" if f2p_ran else "skipped", "does a thing")]
              + ([("passed", "does a thing")] if f2p_twice else []))],
            status="failed" if f2p_ran else "passed",
        ),
        "f2p_after": _node_report(
            [("tests/a.test.js",
              [("passed", "does a thing")] * (2 if f2p_twice else 1))]),
        "p2p_before": p2p_before_report,
        "p2p_after": p2p_report,
        "scoped": _node_report(list(by_file.items())),
    }
    for key in missing:
        reports[key] = None
    # DEVIATION from the plan's literal `present=tests.paths`: the property
    # scan (round-2 item 12) is the first node check to filter REPORT-derived
    # paths (`files_run`) through `_present`, rather than manifest-declared
    # ones -- every prior `_present` caller in a node context only ever asked
    # about `tests.paths` itself. `test -e` in the scripted double is exact
    # SET membership, so a swept file such as `tests/properties.ts` needs its
    # own entry or the scan reads it as absent and every property-scan test
    # would see an empty `property_scan_files` regardless of what the report
    # says ran. The default is broadened to every path this container's own
    # reports claim to have executed -- what a real tree would answer `test
    # -e` for, since these are exactly the files whose reports this container
    # is scripting -- so a test need not separately declare "the file the
    # report says ran also exists". An explicit `present=` in
    # `container_kwargs` still overrides it entirely (`setdefault`).
    default_present = (
        set(tests.paths) | {"tests/a.test.js"} | set(by_file)
        | {node_id.partition("::")[0] for node_id in p2p_ids}
        | set(p2p_before_files)
    )
    container_kwargs.setdefault("present", tuple(sorted(default_present)))
    container = _ScriptedContainer(start_sha="s" * 40, tests=tests,
                                   reports=reports, **container_kwargs)
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
    assert result.evidence["p2p_scoped_after_exit"] is None
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


# --- same-file duplicate ids (round 2 item 11) --------------------------------


def test_a_declared_f2p_id_that_names_more_than_one_test_is_a_problem():
    """M11.2: the red-before check is satisfied by whichever of the pair
    fails and the green-after check by both passing, so the gate's two
    conjuncts stop meaning "this test went red, then green". The `== 2` is
    also what fails a summing accumulator, since this shape puts the id at
    count 2 in BOTH f2p reports."""
    result = _preflight_over(_node_container(f2p_twice=True))

    assert not result.ok
    assert any("names 2 tests" in p for p in result.problems)
    assert result.evidence["same_file_duplicate_ids"] == {
        "tests/a.test.js::does a thing": 2}


def test_a_declared_p2p_id_that_names_more_than_one_test_is_a_problem():
    """This is also the task shape that makes NO scoped run at all (an
    explicit `tests.p2p`), pinning that the check does not depend on one."""
    result = _preflight_over(_node_container(
        p2p=("tests/b.test.js::keeps working",), p2p_twice=True))

    assert not result.ok
    assert any("names 2 tests" in p for p in result.problems)
    assert result.evidence["same_file_duplicate_ids"] == {
        "tests/b.test.js::keeps working": 2}


def test_an_undeclared_same_file_duplicate_is_recorded_and_not_refused():
    """D1's boundary: the gate can prove a DECLARED id's verdict is
    ambiguous and cannot prove an UNDECLARED twin's hazard, because the
    quarantine is `oracle._derive`'s, computed at GRADE time from two
    reference runs this gate never makes. The accepted residual -- a flaky
    undeclared twin CAN be quarantined and take its healthy sibling out of
    check 6 -- is recorded in this same key and audited by hand against
    `GradeRecord`; nothing here joins the two."""
    result = _preflight_over(_node_container(scope_names=[
        ("tests/b.test.js", "works"), ("tests/b.test.js", "works")]))

    assert result.ok, result.problems
    assert result.evidence["same_file_duplicate_ids"] == {
        "tests/b.test.js::works": 2}
    assert not any("names" in p for p in result.problems)


def test_the_declared_duplicate_refusal_reads_the_f2p_run_not_the_scoped_one():
    """M11.4: the scoped run DESELECTS the f2p ids, and a deselected test is
    `skipped` on vitest and `pending` on jest -- neither terminal, so a check
    written over the scoped report alone would see nothing. The scoped
    report here is the default, clean one, and the refusal must still fire
    off the f2p runs."""
    result = _preflight_over(_node_container(f2p_twice=True))

    assert not result.ok
    assert result.evidence["same_file_duplicate_ids"] == {
        "tests/a.test.js::does a thing": 2}


def test_same_file_duplicate_ids_is_recorded_when_there_are_none():
    result = _preflight_over(_node_container())

    assert result.evidence["same_file_duplicate_ids"] == {}
    assert result.evidence["duplicate_full_names"] == []


def test_same_file_duplicate_ids_is_None_when_no_node_run_wrote_a_report():
    """The verdict is a NO-GO for other reasons and control still reaches
    the end of `preflight` (there are exactly two `return PreflightResult(`
    sites, and only the runner-gate one returns early), so this asserts the
    ACCUMULATOR's absence and not the seed's."""
    result = _preflight_over(_node_container(missing=(
        "f2p_before", "f2p_after", "p2p_before", "p2p_after", "scoped")))

    assert not result.ok
    assert result.evidence["same_file_duplicate_ids"] is None


def test_a_run_that_wrote_no_report_contributes_nothing_and_does_not_erase_what_did():
    """The flag's exact semantics: `{}` is "at least one node run was read
    and no id in the runs that were read names two tests", never "every run
    was counted" -- a run that wrote no report contributes nothing and is
    named by its own exit and kind evidence rather than by this key."""
    result = _preflight_over(_node_container(f2p_twice=True, missing=("scoped",)))

    assert not result.ok
    assert result.evidence["same_file_duplicate_ids"] == {
        "tests/a.test.js::does a thing": 2}


def test_a_pytest_task_records_an_empty_same_file_duplicate_map():
    """`{}`, a claim pytest's ids back, never `None`."""
    result = _preflight_over(_pytest_container())

    assert result.evidence["same_file_duplicate_ids"] == {}


def test_same_file_duplicate_ids_is_None_on_the_runner_gate_early_return():
    """The `tests.runner`/`tests.framework` mismatch return, which starts no
    container: `None`, straight from the seed."""
    result = _preflight_over(_node_container(runner=("python", "-m", "pytest")))

    assert not result.ok
    assert result.evidence["same_file_duplicate_ids"] is None


# --- node property-based determinism (round 2 item 12) -----------------------


def test_a_swept_file_that_imports_fast_check_with_no_seed_pin_is_refused():
    """A real, already-gated node task does this today: `yaml-474`'s
    `tests/properties.ts` imports fast-check and nothing the p2p-before
    sweep runs pins a seed. Measured 2026-09-02 against fast-check 3.23.2
    and 2.25.0, `readSeed` falls back to `Date.now() ^ (Math.random() *
    0x100000000)` and ten fresh runs of one property over unchanged code
    gave `0 0 0 1 1 0 0 0 0 0` -- so the p2p verdict this gate publishes,
    and the grader's checks 5 and 6 after it, are a draw. There is no
    `image.env` lever to add for this one: fast-check reads no environment
    variable at all, and the one external lever that works for vitest
    (`NODE_OPTIONS` preloading a `configureGlobal` call) is defeated by
    jest's module registry."""
    result = _preflight_over(_node_container(
        p2p_before_files=("tests/properties.ts",),
        property_imported=True, property_pinned=False))

    assert not result.ok
    assert result.evidence["property_framework_imported_by_suite"] is True
    assert result.evidence["property_framework_seed_pinned"] is False
    assert "tests/properties.ts" in result.evidence["property_scan_files"]
    assert any("fast-check" in p and "pins a seed" in p
               for p in result.problems)


def test_a_swept_path_the_tree_does_not_have_is_not_handed_to_rg():
    """`_present(container, swept)` is what a mangled or absolute report
    `name` reads as, since `_relpath` never raises. Round 2 item 12's fix
    wave, non-blocking finding 4 (impl-12-review.md, 2026-09-03): before this
    test, no property-scan test overrode `present=`, so every swept file was
    always present by construction and a mutation to `_present(container,
    swept)` returning `list(swept)` unfiltered went uncaught."""
    container, task = _node_container(
        p2p_before_files=("/repo/tests/properties.ts",),
        property_imported=True,
        present=("tests/", "tests/a.test.js", "tests/b.test.js"))

    result = _preflight_over((container, task))

    assert "/repo/tests/properties.ts" not in result.evidence["property_scan_files"]


def test_the_scan_reads_the_p2p_before_runs_files_run_not_tests_paths():
    """The load-bearing scope decision (review 1). Measured on yaml-474: the
    p2p-before sweep loads 25 suites and 3,497 tests while `tests.paths` is
    the single file `tests/doc/stringify.ts` -- scanning the declared paths
    answers a question about a file the certified verdict barely depends on
    and says nothing about the 24 others it does. So the scan reads
    `Outcome.files_run` off the p2p-BEFORE run, not `tests.paths`."""
    container, task = _node_container(
        p2p_before_files=("tests/properties.ts",),
        property_imported=True, property_pinned=False)

    _preflight_over((container, task))

    import_scan = next(
        cmd for cmd in container.commands
        if any("fast-check" in part for part in cmd))
    assert "tests/properties.ts" in import_scan
    assert "tests/" not in import_scan


def test_the_scan_runs_after_the_p2p_before_sweep_that_gives_it_its_scope():
    """Not the environment-defect ordering rule (preflight collects problems
    rather than raising, so order carries no verdict) -- it is that
    `classify` reads `_Runner.last_report`, which ANY subsequent
    `_Runner.run` overwrites, so the scope must be taken from the Outcome
    bound at the p2p-before run and used before the next suite invocation,
    whichever one that is. Bounding on the p2p-AFTER run instead would
    permit a placement the rationale forbids (review 2, C3): the f2p-after
    run sits between the two p2p runs."""
    container, task = _node_container(
        p2p_before_files=("tests/properties.ts",),
        property_imported=True, property_pinned=False)

    _preflight_over((container, task))

    scan_index = next(
        i for i, cmd in enumerate(container.commands)
        if any("fast-check" in part for part in cmd))
    suite_indices = [i for i, cmd in enumerate(container.commands)
                     if cmd[:1] == ["timeout"] and "--co" not in cmd]
    before = max(i for i in suite_indices if i < scan_index)
    after = min(i for i in suite_indices if i > scan_index)

    assert before < scan_index < after


def test_a_swept_file_whose_scope_pins_the_seed_is_accepted():
    """Deliberately weaker than `image.env`, which preflight reads back OUT
    of the container: a `configureGlobal` match proves the repository has
    thought about the problem, not that it executes, that it runs before
    every property, or that a `describe`-local `fc.assert` overrides it."""
    result = _preflight_over(_node_container(
        p2p_before_files=("tests/properties.ts",),
        property_imported=True, property_pinned=True))

    assert result.evidence["property_framework_imported_by_suite"] is True
    assert result.evidence["property_framework_seed_pinned"] is True
    assert not any("pins a seed" in p for p in result.problems)


def test_a_sweep_with_no_property_import_is_not_asked_about_a_pin():
    """The second probe is not run when the first says no: `imported: false`
    means the pin was never asked about, which is a different fact from
    `pinned: false` -- "asked, and no pin found"."""
    container, task = _node_container(property_imported=False)

    result = _preflight_over((container, task))

    assert result.evidence["property_framework_imported_by_suite"] is False
    assert result.evidence["property_framework_seed_pinned"] is None
    assert result.evidence["property_scan_files"]
    assert not any("-U" in cmd for cmd in container.commands)


def test_the_property_import_scan_that_could_not_answer_is_None_and_a_NO_GO():
    """A quiet `False` here reads as "no property framework" and disarms the
    one check that catches an unpinned property-based suite on an
    environment defect preflight CAN see through."""
    container, task = _node_container(property_import_rg_exit=2)

    result = _preflight_over((container, task))

    assert not result.ok
    assert result.evidence["property_framework_imported_by_suite"] is None
    assert result.evidence["property_framework_seed_pinned"] is None
    assert any("property-framework-import scan" in p and "exited 2" in p
               for p in result.problems)
    assert not any("-U" in cmd for cmd in container.commands)


def test_the_seed_pin_scan_that_could_not_answer_is_None_and_a_NO_GO():
    """The asymmetry `_rg_probe`'s `consequence` parameter exists for:
    misreading THIS probe REFUSES a task that was already fine, which is not
    what misreading the other two probes does -- so a single shared rg_exit
    cannot express this case, and the fake dispatches on the pattern
    instead."""
    result = _preflight_over(_node_container(
        property_imported=True, property_pin_rg_exit=2))

    assert not result.ok
    assert result.evidence["property_framework_seed_pinned"] is None
    assert any("property-seed-pin scan" in p and "exited 2" in p
               for p in result.problems)


def test_the_pin_scan_is_the_only_probe_that_asks_rg_for_multiline():
    """A real `configureGlobal({\\n seed: 1234\\n})` spans lines and rg is
    line-based without `-U`, so dropping it turns a pinned suite into a
    refused one; putting `-U` on the IMPORT probe would change what that
    pattern can match across file boundaries."""
    container, task = _node_container(
        property_imported=True, property_pinned=True)

    _preflight_over((container, task))

    multiline = [cmd for cmd in container.commands if "-U" in cmd]
    assert len(multiline) == 1
    assert any("configureGlobal" in part for part in multiline[0])


def test_a_pytest_task_records_all_three_property_keys_as_absent():
    """`files_run` is `None` for pytest by the adapter's own contract, so the
    check is node-only by CONSTRUCTION rather than by a framework branch --
    and the null is a recorded absence, never a claim that a Python suite
    has no property framework, which the hypothesis probe answers with a
    stronger instrument."""
    container, task = _pytest_container()

    result = _preflight_over((container, task))

    assert result.evidence["property_framework_imported_by_suite"] is None
    assert result.evidence["property_framework_seed_pinned"] is None
    assert result.evidence["property_scan_files"] is None
    assert not any("fast-check" in part for cmd in container.commands
                  for part in cmd)


def test_a_sweep_that_wrote_no_report_leaves_the_property_keys_absent():
    """A broken config exits 1 and writes NO file on both frameworks
    (measured), which is `KIND_ENVIRONMENT` -- and that shape already NO-GOes
    for the missing-report reason, so the scan adds no problem of its own
    here. A second message for one cause is how a reader learns to skim
    both."""
    container, task = _node_container(missing=("p2p_before",))

    result = _preflight_over((container, task))

    assert not result.ok
    assert result.evidence["property_framework_imported_by_suite"] is None
    assert result.evidence["property_framework_seed_pinned"] is None
    assert result.evidence["property_scan_files"] is None
    assert not any("fast-check" in part for cmd in container.commands
                  for part in cmd)


def test_the_property_scan_keys_say_which_absence_on_the_early_return():
    """The keys are written on every path, because a key present on one
    branch and absent on another is the same defect one layer down. A NEW
    test, not an edit to the existing runner-gate one: round-2 item 5 is
    about that function's early-return evidence and the two must not
    collide."""
    result = _preflight_over(_node_container(runner=("python", "-m", "pytest")))

    assert not result.ok
    assert result.evidence["property_framework_imported_by_suite"] is None
    assert result.evidence["property_framework_seed_pinned"] is None
    assert result.evidence["property_scan_files"] is None


def test_the_hypothesis_rg_message_is_byte_identical_after_the_extraction(
    monkeypatch, tmp_path
):
    """The plan behind this extraction is on record wanting the hypothesis
    probe's message stable through the `_rg_probe` refactor, and nothing
    pinned it before -- the two existing tests
    (`test_an_rg_probe_that_could_not_answer_is_None_and_not_False`,
    `test_the_hypothesis_import_probe_runs_before_the_suite`) assert only
    substrings, which survive any rewording."""
    task = _FakeTask()
    container = _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                                   present=("tests/",), rg_exit=2)

    result = _run_preflight(monkeypatch, tmp_path, task, container)

    expected = (
        "the hypothesis-import scan (rg over tests.paths) could not answer: "
        "`rg -q ^\\s*(from|import)\\s+hypothesis\\b -- tests/` exited 2, "
        "not 0 (match) or 1 (no match). rg exits 2 on an unreadable path or "
        "a bad pattern and it is asserted present above, so this names an "
        "environment problem preflight cannot see through -- silently "
        "reading it as 'not imported' would disarm the one check that "
        "catches an undeclared property-based suite."
    )
    assert expected in result.problems


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


def test_no_suite_argv_lets_tests_runner_swallow_the_checks_own_positional():
    """Measured 2026-09-03 in bakeoff-task-yaml-474-…:v1 (jest 30.5.0):
    `--testPathIgnorePatterns=<p>` at the tail of tests.runner swallows the
    next bare token -- 23 files listed against 1 -- and the `=` form is no
    less greedy than the space form. HARVESTING.md's own worked remedy puts
    that flag there, so the f2p SELECT check ran 23 suites with 3279 skipped
    ("did not RUN"), the scoped run left tests.paths, and the p2p-before
    sweep INVERTED to run only the f2p file. `--` is not the fix: after it
    yargs stops parsing and `-t` becomes another OR'd path pattern (measured:
    "Ran all test suites matching <path>|-t|<pattern>"). The report flag is
    emitted FIRST so every group opens with a token starting with `-`, which
    is what ends a yargs array -- the rule node_adapter.p2p_argvs already
    applies to its own internally-built groups."""
    container, task = _node_container(
        framework="jest",
        runner=("/node_modules/.bin/jest", "--config", "config/jest.config.js",
                "--testPathIgnorePatterns=tests/properties\\.ts"))

    _preflight_over((container, task))

    suites = [cmd for cmd in container.commands
              if cmd[:1] == ["timeout"] and "--co" not in cmd]
    assert suites
    for cmd in suites:
        tail = cmd[2 + len(task.tests.runner):]
        assert tail and tail[0].startswith("-"), cmd


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


# --- one evidence schema -----------------------------------------------------


def test_a_preflight_result_whose_evidence_is_not_the_schema_is_refused():
    """The enforcement lives in the gate and not only in the tests.

    A test can only assert the routes it enumerates, and the route somebody
    adds next is the one that has already gone wrong twice inside this file
    (`PREFLIGHT_VERSION` 11's three keys, and `bare_runner_skipped`'s "left
    out of this seed once"). Both were found by review rather than by a test,
    which is what this raise replaces."""
    with pytest.raises(ValueError) as caught:
        PreflightResult(task_id="t", task_version=1, start_sha="s" * 40,
                        image="i", manifest_digest="d",
                        evidence={"framework": "pytest"})

    assert "missing" in str(caught.value)
    assert "'uid'" in str(caught.value)
    assert "unlisted" in str(caught.value)


def test_an_unlisted_evidence_key_is_refused_in_both_directions():
    """The other direction, and the reason the message names both: a MISSING
    key means a path that builds the result by hand, an UNLISTED one means a
    write that was never added to `EVIDENCE_KEYS`. The remedies differ."""
    with pytest.raises(ValueError) as caught:
        PreflightResult(task_id="t", task_version=1, start_sha="s" * 40,
                        image="i", manifest_digest="d",
                        evidence=_evidence_seed() | {"invented": 1})

    assert "invented" in str(caught.value)


def test_the_grading_evidence_keys_read_the_grading_dataclass_and_not_a_copy():
    """All three grading names are in the schema and correctly spelled.

    It does NOT detect a transcribed copy -- `<=` holds for the star-unpack
    and for three literal strings alike -- and neither does
    `test_evidence_keys_lists_exactly_what_preflight_writes`' equality, both
    of whose sides read `_GRADING_KEYS`. What a transcribed copy costs is one
    round rather than silence: add a fourth check to `TaskGrading` and the
    gate writes `grading_<new>_exit`, `__post_init__` raises, and that test
    fails on the missing member. Its `ast.Starred` assertion is what closes
    the gap at transcription time."""
    assert {f"grading_{key}_exit" for key in _GRADING_KEYS} <= set(EVIDENCE_KEYS)


def _apply_fails():
    tests = _FakeTests()
    task = _FakeTask(tests=tests)
    return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                              present=("tests/",), apply_exit=1), task


def _grading_declared():
    task = _FakeTask(grading=TaskGrading(typecheck=("mypy", "src")))
    return _ScriptedContainer(start_sha="s" * 40, tests=task.tests,
                              present=("tests/",),
                              grading_exits={("mypy", "src"): 0}), task


def _no_prefix_exists():
    tests = _FakeTests()
    task = _FakeTask(tests=tests)
    return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                              present=()), task


def _one_prefix_missing():
    tests = _FakeTests(paths=("tests/", "docs/tests/"))
    task = _FakeTask(tests=tests)
    return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                              present=("tests/",)), task


def _submodule_status_failed():
    tests = _FakeTests()
    task = _FakeTask(tests=tests)
    return _ScriptedContainer(start_sha="s" * 40, tests=tests,
                              present=("tests/",),
                              submodule_status_exit=1), task


#: The routes through `preflight`, each skipping a different part of the key
#: set. No route writes all of them, which is the whole finding.
_SCHEMA_ROUTES = [
    # id, build() -> (container, task)
    ("runner_gate", lambda: _pytest_container(runner=("go", "test", "./..."))),
    ("pytest_happy", lambda: _pytest_container()),
    ("node_happy", lambda: _node_container()),
    ("explicit_p2p", lambda: _node_container(p2p=("tests/a.test.js::other",))),
    ("patch_does_not_apply", lambda: _apply_fails()),
    ("grading_declared", lambda: _grading_declared()),
    ("scope_collects_nothing", lambda: _no_prefix_exists()),
    ("prefix_absent", lambda: _one_prefix_missing()),
    ("submodule_status_failed", lambda: _submodule_status_failed()),
]


@pytest.mark.parametrize("build", [route[1] for route in _SCHEMA_ROUTES],
                         ids=[route[0] for route in _SCHEMA_ROUTES])
def test_every_evidence_key_is_present_on_every_route(build):
    """`PreflightResult.__post_init__` already guarantees this equality for
    every result that exists, so what this parametrization actually pins is
    that each ENUMERATED route reaches a return at all. Remove the seed and
    every one of them raises instead, which is how the mutation anchor goes
    red.

    It does not and cannot cover a route it does not enumerate; that coverage
    is `__post_init__`'s, and the coverage of the tuple itself is
    `test_evidence_keys_lists_exactly_what_preflight_writes`. The assertion is
    kept in this readable form because it is the STATEMENT of the invariant,
    not because it is the thing that can fail.

    What each route skips, and why none of them is redundant: `runner_gate`
    skips every container-measured key; `pytest_happy` skips the three
    `grading_*_exit` (`_FakeTask`'s `TaskGrading()` is empty) and
    `scope_prefixes_absent` (every declared prefix exists); `node_happy` skips
    `python_observed` (the node branch is a bare `pass`; `python_declared` IS
    written, as the `None` that says a node manifest may not declare
    `image.python`), both `bare_runner_*` and both `hypothesis_*` (whose
    writes sit inside `if interpreter is not None:` and the node adapter
    answers `None`); `explicit_p2p` skips the whole scoped block;
    `patch_does_not_apply` skips everything downstream of the fix, which is
    the row set no "many nulls means no container" heuristic can see;
    `grading_declared` writes one `grading_*_exit` and skips two;
    `scope_collects_nothing` writes `scope_prefixes_absent` and then refuses
    before the scoped run; `prefix_absent` is the only route that writes
    `scope_prefixes_absent` AND still reaches the scoped run; and
    `submodule_status_failed` leaves `submodules_orphaned` at its branch's own
    explicit `None`."""
    result = _preflight_over(build())

    assert set(result.evidence) == set(EVIDENCE_KEYS)


def test_evidence_keys_lists_exactly_what_preflight_writes():
    """The one assertion here that fails on an `EVIDENCE_KEYS` edit rather
    than following it.

    `__post_init__` catches a key written on a path that RUNS; this catches a
    key written on a path nothing exercises, at edit time -- and it is what
    keeps the tuple from going stale the next time an item adds a key, which
    is how this commit's own plan came to be written two keys short against
    its round's landing order.

    `early_return` needs no special case: it is a literal subscript in the
    early-return block, so the walk finds it. Only the `f`-string grading
    subscript is dynamic, and its names come off `_GRADING_KEYS`, which is the
    same source `EVIDENCE_KEYS` reads -- so this asserts the two agree on a
    set that is derived twice rather than transcribed twice -- and the
    `ast.Starred` check reads the definition to prove the derivation is still
    a derivation, which is the one thing neither this equality nor
    `test_the_grading_evidence_keys_read_the_grading_dataclass_and_not_a_copy`
    can see.

    `bounded_run_durations_s` is never a top-level Store target of `evidence`
    -- round 2 item 7 seeds its shape from `_evidence_seed()` and writes only
    INTO it, `evidence["bounded_run_durations_s"]["bare_runner"] = ...`, which
    parses as a Subscript of a Subscript: the OUTER node's `.value` is a
    Subscript (not the Name `evidence`, so the direct check below misses it)
    and the INNER node -- `evidence["bounded_run_durations_s"]`, the outer
    node's own `.value` -- carries `ctx=Load`, because in a chained
    assignment target only the outermost Subscript is `Store` (verified
    2026-09-03 with `ast.dump` against exactly this shape). So a Store-only
    filter finds neither node for this key, which is what the second
    comprehension below exists to fix: it reads the same chain from the
    OUTER Store node's `.value` instead of requiring `ctx=Store` on the inner
    one directly.
    """
    import ast
    import pathlib

    import bakeoff.preflight as pf

    tree = ast.parse(pathlib.Path(pf.__file__).read_text())
    # MODULE-WIDE, not scoped to `preflight`. Every write is inside that
    # function today, but a helper taking the evidence dict and writing a key
    # into it would be invisible to a scoped walk while
    # `PreflightResult.__post_init__` accepted the result -- the one shape
    # this test exists to catch, missed by the test itself. Re-measured
    # 2026-09-03 over the whole module in both subscript shapes, plus a scan
    # for `evidence.update` and `|=` (there are none): exact agreement with
    # the scoped extraction, no extras either way.
    written = {n.slice.value for n in ast.walk(tree)
               if isinstance(n, ast.Subscript)
               and isinstance(n.value, ast.Name) and n.value.id == "evidence"
               and isinstance(n.slice, ast.Constant)
               and isinstance(n.ctx, ast.Store)}
    # A chained subscript assignment, `evidence["outer"]["inner"] = value`,
    # nests `evidence["outer"]` -- ctx=Load -- inside the outer Store node's
    # `.value`. Read it from there rather than requiring `ctx=Store` on the
    # inner node, which it never carries.
    written |= {
        n.value.slice.value for n in ast.walk(tree)
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
        and isinstance(n.value, ast.Subscript)
        and isinstance(n.value.value, ast.Name) and n.value.value.id == "evidence"
        and isinstance(n.value.slice, ast.Constant)
    }
    written |= {f"grading_{key}_exit" for key in _GRADING_KEYS}

    assert set(pf.EVIDENCE_KEYS) == written
    assert len(pf.EVIDENCE_KEYS) == len(set(pf.EVIDENCE_KEYS))

    # The grading names are DERIVED, not transcribed. The subset check in
    # `test_the_grading_evidence_keys_read_the_grading_dataclass_and_not_a_copy`
    # cannot see the difference -- `<=` holds for the star-unpack and for
    # three literal strings alike -- and neither can the equality above, since
    # both of its sides read `_GRADING_KEYS`. This reads the definition itself.
    assign = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AnnAssign)
                  and getattr(n.target, "id", None) == "EVIDENCE_KEYS")
    assert any(isinstance(e, ast.Starred) for e in assign.value.elts)


# --- round 2 item 7: the gate records how long its own bounded runs took ---

def test_the_bounded_run_keys_include_every_declared_grading_command():
    """Adding a check to `TaskGrading` must move this tuple, because
    `preflight` writes `grading_<key>` off the same dataclass and a key it
    writes but does not list is refused by its own schema."""
    assert {f"grading_{k}" for k in _GRADING_KEYS} <= set(BOUNDED_RUN_KEYS)
    assert len(BOUNDED_RUN_KEYS) == 6 + len(_GRADING_KEYS)
    assert len(BOUNDED_RUN_KEYS) == len(set(BOUNDED_RUN_KEYS))


def test_a_bounded_run_dict_that_is_not_the_schema_is_refused():
    """The third construction is the one that matters: a guard that merely
    SKIPPED a non-dict would accept `[]`, and `PREFLIGHT_VERSION` 11 exists
    because `[]` claimed a measurement never made."""
    with pytest.raises(ValueError, match="missing.*unlisted"):
        PreflightResult(
            task_id="t", task_version=1, start_sha="s" * 40, image="i",
            manifest_digest="d",
            evidence=_evidence_seed() | {
                "bounded_run_durations_s": {"f2p_before": 1.0}},
        )
    with pytest.raises(ValueError, match="invented"):
        PreflightResult(
            task_id="t", task_version=1, start_sha="s" * 40, image="i",
            manifest_digest="d",
            evidence=_evidence_seed() | {
                "bounded_run_durations_s":
                    dict.fromkeys(BOUNDED_RUN_KEYS) | {"invented": 1.0}},
        )
    with pytest.raises(ValueError, match="list"):
        PreflightResult(
            task_id="t", task_version=1, start_sha="s" * 40, image="i",
            manifest_digest="d",
            evidence=_evidence_seed() | {"bounded_run_durations_s": []},
        )


def test_the_seed_carries_the_full_bounded_run_schema():
    """A module-level constant here would alias one mutable dict across every
    `PreflightResult` in the process, and one gate's measurements would
    surface in the next gate's verdict -- the aliasing bug that never fails
    in a single-task test run."""
    assert _evidence_seed()["bounded_run_durations_s"] == dict.fromkeys(
        BOUNDED_RUN_KEYS)
    assert (_evidence_seed()["bounded_run_durations_s"] is not
            _evidence_seed()["bounded_run_durations_s"])


def test_a_fresh_verdict_matches_the_key_it_was_written_under():
    """With `_key_parts` shared, the join cannot drift -- what this pins is
    the half a shared formatter cannot cover, that `verdict_matches_key`
    reads the blob's four fields under the names `to_dict` writes them."""
    container, task = _pytest_container()
    result = _preflight_over((container, task))
    image = "sha256:x"
    start_sha = "s" * 40
    key = preflight_cache_key(task, image, start_sha)

    assert verdict_matches_key(result.to_dict(), key)
    assert not verdict_matches_key(result.to_dict() | {"image": "sha256:other"}, key)
    assert not verdict_matches_key(result.to_dict() | {"start_sha": "t" * 40}, key)
    assert not verdict_matches_key(
        result.to_dict() | {"preflight_version": "0"}, key)


def test_every_bounded_run_records_its_own_wall_clock():
    task = _FakeTask(grading=TaskGrading(build=("make",),
                                         typecheck=("mypy", "src"),
                                         lint=("ruff", "check")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        grading_exits={("make",): 0, ("mypy", "src"): 0, ("ruff", "check"): 0},
    )
    result = _preflight_over((container, task))

    durations = result.evidence["bounded_run_durations_s"]
    assert set(durations) == set(BOUNDED_RUN_KEYS)
    assert all(isinstance(v, float) for v in durations.values())


def test_a_bounded_run_that_never_happened_records_no_duration():
    """The outer key set is item 5's schema test's claim and is not
    re-asserted here -- what this asserts is that a run that did not happen
    is a null INSIDE the dict rather than a missing entry, which is the same
    rule one layer down and the layer where a nested value stops being
    covered."""
    refused = _preflight_over(_pytest_container(runner=("go", "test", "./...")))
    durations = refused.evidence["bounded_run_durations_s"]
    assert set(durations) == set(BOUNDED_RUN_KEYS)
    assert all(v is None for v in durations.values())
    assert refused.evidence["bounded_run_duration_max_s"] is None

    apply_failed = _preflight_over(_apply_fails())
    durations = apply_failed.evidence["bounded_run_durations_s"]
    ran = {"bare_runner", "f2p_before", "p2p_before"}
    for key in BOUNDED_RUN_KEYS:
        if key in ran:
            assert isinstance(durations[key], float), key
        else:
            assert durations[key] is None, key


def test_the_recorded_duration_is_the_execs_own_clock():
    """Distinct values per run, so a duration copied from the wrong result --
    or a constant -- fails rather than passing on nine equal numbers.

    Vocabulary note: the scripted container calls the scoped run `scoped`;
    the evidence calls it `p2p_scoped_after`.
    """
    task = _FakeTask(grading=TaskGrading(typecheck=("mypy", "src")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        grading_exits={("mypy", "src"): 0},
        durations_ms={
            "bare_runner": 500, "f2p_before": 12_300, "p2p_before": 41_000,
            "f2p_after": 1_500, "p2p_after": 2_250, "scoped": 999,
            ("mypy", "src"): 7_000,
        },
    )
    result = _preflight_over((container, task))

    assert result.evidence["bounded_run_durations_s"] == {
        "bare_runner": 0.5,
        "f2p_before": 12.3,
        "p2p_before": 41.0,
        "f2p_after": 1.5,
        "p2p_after": 2.25,
        "grading_build": None,
        "grading_typecheck": 7.0,
        "grading_lint": None,
        "p2p_scoped_after": 0.999,
    }


def test_the_slowest_bounded_run_is_recorded():
    """The max is what `run_matrix` prints beside the bound and what a
    `timed_out` grade is read against; it is computed once, in the gate,
    because a null-aware maximum over a dict with skipped runs in it is the
    computation a reader gets wrong, and a scripted 60 s for a run that never
    happened is what proves it is skipping rather than defaulting."""
    task = _FakeTask(grading=TaskGrading(typecheck=("mypy", "src")))
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=task.tests, present=("tests/",),
        grading_exits={("mypy", "src"): 0},
        durations_ms={
            "bare_runner": 500, "f2p_before": 12_300, "p2p_before": 41_000,
            "f2p_after": 1_500, "p2p_after": 2_250, "scoped": 999,
            ("mypy", "src"): 7_000,
        },
    )
    result = _preflight_over((container, task))
    assert result.evidence["bounded_run_duration_max_s"] == 41.0

    # The container's LARGEST scripted number (60 s) belongs to the scoped
    # run, and an explicit tests.p2p means that run never happens.
    tests = _FakeTests(p2p=("tests/b.py::test_two",))
    task2 = _FakeTask(tests=tests)
    container2 = _ScriptedContainer(
        start_sha="s" * 40, tests=tests, present=("tests/",),
        durations_ms={
            "bare_runner": 500, "f2p_before": 3_000, "p2p_before": 9_000,
            "f2p_after": 2_000, "p2p_after": 4_000, "scoped": 60_000,
        },
    )
    result2 = _preflight_over((container2, task2))
    assert result2.evidence["bounded_run_durations_s"]["p2p_scoped_after"] is None
    assert result2.evidence["bounded_run_duration_max_s"] == 9.0


@pytest.mark.parametrize("build", [route[1] for route in _SCHEMA_ROUTES],
                         ids=[route[0] for route in _SCHEMA_ROUTES])
def test_a_recorded_exit_code_always_has_a_duration_beside_it(build):
    """Seven statement pairs covering nine invocations, one rule: a recorded
    exit code always has a duration beside it. The failure this catches is a
    tenth bounded command added later with an exit code and no clock, which
    is how the gate got to nine commands and eight documented ones."""
    result = _preflight_over(build())
    evidence = result.evidence
    pairs = [
        ("bare_runner_exit", "bare_runner"),
        ("f2p_before_exit", "f2p_before"),
        ("p2p_before_exit", "p2p_before"),
        ("f2p_after_exit", "f2p_after"),
        ("p2p_after_exit", "p2p_after"),
        *[(f"grading_{k}_exit", f"grading_{k}") for k in _GRADING_KEYS],
        ("p2p_scoped_after_exit", "p2p_scoped_after"),
    ]
    for exit_key, duration_key in pairs:
        assert (evidence[exit_key] is None) == (
            evidence["bounded_run_durations_s"][duration_key] is None), (
            exit_key, duration_key)


def test_the_durations_move_no_verdict():
    """This commit changes what a stored verdict SAYS, never what it
    DECIDES; that is what makes the `PREFLIGHT_VERSION` bump a re-read
    rather than a re-judgement."""
    happy = _preflight_over(_pytest_container())
    assert happy.ok
    assert happy.problem_codes == ()
    assert len(happy.problems) == 0

    refused = _preflight_over(_pytest_container(runner=("go", "test", "./...")))
    assert not refused.ok
    assert refused.problem_codes == ()
    assert len(refused.problems) == 1


def test_the_early_return_names_itself():
    """With a uniform schema, "many nulls" stops being a proxy for "no
    container started": a node task, an explicit-`p2p` task and an ordinary GO
    each leave their own null-set, and none of them is this one. So the route
    says which route it was rather than leaving a reader to intersect them.

    A second pre-container return must add a second constant and extend this
    test."""
    refused = _preflight_over(_pytest_container(runner=("go", "test", "./...")))
    healthy = _preflight_over(_pytest_container())

    assert refused.evidence["early_return"] == EARLY_RETURN_RUNNER_MISMATCH
    assert healthy.evidence["early_return"] is None


def test_the_schema_moves_no_verdict():
    """This commit changes what a stored verdict SAYS and never what it
    DECIDES. That is what makes a cached PASS from `PREFLIGHT_VERSION` 16
    still true and the bump a re-READING rather than a re-judgement."""
    refused = _preflight_over(_pytest_container(runner=("go", "test", "./...")))
    healthy = _preflight_over(_pytest_container())

    assert healthy.ok, healthy.problems
    assert healthy.problem_codes == ()
    assert healthy.problems == ()
    assert not refused.ok
    assert refused.problem_codes == ()
    assert len(refused.problems) == 1


# --- round 2, item 18: nested submodules --------------------------------------


_NESTED_STATUS = (
    " 1111111111111111111111111111111111111111 vendor/lib (heads/main)\n"
    " 2222222222222222222222222222222222222222 vendor/lib/vendor/deep"
    " (heads/main)\n"
)


def _nested_container(**overrides):
    """The healthy two-level tree every test in this section starts from."""
    kwargs = dict(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/lib",),
        inner_gitlinks={"vendor/lib": ("vendor/deep",)},
        submodule_status=_NESTED_STATUS,
        ls_entries={"vendor/lib": ("libdep",),
                    "vendor/lib/vendor/deep": ("deepdep",)},
    )
    kwargs.update(overrides)
    return _ScriptedContainer(**kwargs)


def test_a_nested_submodule_status_line_matches_only_its_own_path():
    """Pure, over `_parse_submodule_status`, which item 18 does NOT modify --
    and that is worth pinning because it looks like it should need work.
    `tail == "vendor/lib"` is false for the deeper line and
    `tail.startswith("vendor/lib" + " ")` is false too (the next byte is `/`),
    so only the long path matches and `max(key=len)` is never asked."""
    parsed, unmatched = _parse_submodule_status(
        _NESTED_STATUS, ("vendor/lib", "vendor/lib/vendor/deep"))

    assert unmatched == []
    assert [entry["path"] for entry in parsed] == [
        "vendor/lib", "vendor/lib/vendor/deep"]
    assert [entry["sha"] for entry in parsed] == ["1" * 40, "2" * 40]
    assert all(entry["initialised"] for entry in parsed)


def test_an_uninitialised_parent_is_not_descended_into(monkeypatch, tmp_path):
    """THE M11 PIN. Measured 2026-09-02: `git -C <empty submodule dir> ls-files
    -s -z` exits 0 and returns the PARENT's own gitlink as `./`, because git
    walked up to the enclosing repository and filtered its index by the cwd
    prefix. Joined onto the prefix that is `vendor/lib/.` -- a gitlink that
    does not exist, which is a FABRICATED observation and worse than the empty
    directory the recursion was looking for. `rev-parse --show-prefix` is what
    stops it, and this container answers `ls-files` the way git really does so
    that removing the guard produces the fabrication rather than nothing."""
    container = _nested_container(
        submodule_status=(
            "-1111111111111111111111111111111111111111 vendor/lib\n"
        ),
        inner_gitlinks={"vendor/lib": ("./",)},
        ls_entries={},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    paths = [entry["path"] for entry in result.evidence["submodules"]]
    assert paths == ["vendor/lib"]
    assert not any("." in path.split("/") for path in paths)
    assert result.evidence["submodules"][0]["depth"] == 1
    # Not a GO -- the parent really is uninitialised -- but for the RIGHT
    # reason, and with no fabricated path in the problems either.
    assert not result.ok
    assert any("not initialised at their gitlink" in p for p in result.problems)
    assert not any("vendor/lib/." in p for p in result.problems)


def test_an_uninitialised_inner_is_not_descended_into(monkeypatch, tmp_path):
    """The same guard one level down, which needs a cap of 3 to be reachable at
    all: at the shipped cap of 2 the recursion never probes a depth-2 path,
    because it has nowhere left to descend to. Measured 2026-09-02, the walk-up
    happens at EVERY level -- `git -C vendor/lib/vendor/deep ls-files` returns
    `./` for an empty inner exactly as it does for an empty outer."""
    monkeypatch.setattr("bakeoff.preflight._MAX_SUBMODULE_DEPTH", 3)
    container = _nested_container(
        submodule_status=(
            " 1111111111111111111111111111111111111111 vendor/lib"
            " (heads/main)\n"
            "-2222222222222222222222222222222222222222 vendor/lib/vendor/deep\n"
        ),
        inner_gitlinks={"vendor/lib": ("vendor/deep",),
                        "vendor/lib/vendor/deep": ("./",)},
        ls_entries={"vendor/lib": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    paths = [entry["path"] for entry in result.evidence["submodules"]]
    assert paths == ["vendor/lib", "vendor/lib/vendor/deep"]
    assert not any(path.endswith("/.") for path in paths)


def test_a_failed_prefix_probe_makes_the_submodule_evidence_none(
        monkeypatch, tmp_path):
    """`None`, never a shorter positive list: a level was reached and could not
    be interrogated, so nothing below it was read and no submodule claim can be
    made about the tree."""
    container = _nested_container(own_repo={"vendor/lib": None})

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules"] is None
    assert result.evidence["submodules_orphaned"] is None
    assert any("reading inside the submodule vendor/lib failed" in p
               for p in result.problems)
    assert any("rev-parse --show-prefix" in p for p in result.problems)


def test_every_submodule_entry_carries_its_depth(monkeypatch, tmp_path):
    """On EVERY entry, depth 1 included: a reader who cannot see the field on
    the flat entries cannot tell "this tree is flat" from "this gate did not
    know about nesting"."""
    flat = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/libdep",),
        submodule_status=(
            " 942c381d88cecca36be86b2e902f554ad145ec44 vendor/libdep"
            " (heads/main)\n"
        ),
        ls_entries={"vendor/libdep": ("libdep",)},
    )
    flat_result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), flat)
    assert [e["depth"] for e in flat_result.evidence["submodules"]] == [1]

    nested_result = _run_preflight(monkeypatch, tmp_path, _FakeTask(),
                                   _nested_container())
    assert nested_result.ok, nested_result.problems
    assert [(e["path"], e["depth"])
            for e in nested_result.evidence["submodules"]] == [
        ("vendor/lib", 1), ("vendor/lib/vendor/deep", 2)]
    assert nested_result.evidence["submodules_orphaned"] == []


def test_an_empty_inner_submodule_is_a_no_go(monkeypatch, tmp_path):
    """The whole point of the recursion. Measured 2026-09-02 (M16): with level
    1 populated and level 2 empty, `git status --porcelain`, the inner's own,
    `git diff HEAD` and NON-recursive `git submodule status` are all clean --
    `--recursive` is the only reader that says anything, and what it says is
    this `-`."""
    container = _nested_container(
        submodule_status=(
            " 1111111111111111111111111111111111111111 vendor/lib"
            " (heads/main)\n"
            "-2222222222222222222222222222222222222222 vendor/lib/vendor/deep\n"
        ),
        ls_entries={"vendor/lib": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    stale = [p for p in result.problems
             if "not initialised at their gitlink" in p]
    assert len(stale) == 1
    assert "vendor/lib/vendor/deep" in stale[0]


def test_a_tree_deeper_than_the_cap_is_named_before_the_unmatched_problem(
        monkeypatch, tmp_path):
    """Ruled: the gate READS a tree, the loader REFUSES a manifest. Without the
    explicit problem the depth-3 line falls into `unmatched` and is reported as
    the two readers disagreeing -- true, and about the wrong cause."""
    container = _nested_container(
        submodule_status=_NESTED_STATUS + (
            " 3333333333333333333333333333333333333333"
            " vendor/lib/vendor/deep/vendor/bottom (heads/main)\n"
        ),
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    deeper = [i for i, p in enumerate(result.problems)
              if "deeper than this eval populates" in p]
    disagree = [i for i, p in enumerate(result.problems)
                if "the index has no gitlink for" in p]
    assert len(deeper) == 1 and len(disagree) == 1
    assert deeper[0] < disagree[0]
    assert "vendor/lib/vendor/deep/vendor/bottom" in result.problems[deeper[0]]
    assert "at most 2 levels" in result.problems[deeper[0]]


def test_submodules_orphaned_is_read_at_every_level(monkeypatch, tmp_path):
    """A stanza with no gitlink is inert and is RECORDED, at every initialised
    level -- and the value is joined onto the level's prefix, because every
    other reader in this file speaks superproject-relative paths."""
    container = _nested_container(
        inner_gitmodules={"vendor/lib": (("vendor/deep", "https://x/deep.git"),
                                         ("gone", "https://x/gone.git"))},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert result.evidence["submodules_orphaned"] == ["vendor/lib/gone"]
    entries = {e["path"]: e for e in result.evidence["submodules"]}
    assert entries["vendor/lib/vendor/deep"]["url_declared"] \
        == "https://x/deep.git"


def test_one_unreadable_level_makes_submodules_orphaned_none(
        monkeypatch, tmp_path):
    """ALL-OR-`None`. Returning the levels that DID answer would render a
    partial answer as a complete positive list -- "absence is recorded, never
    implied", broken in the block whose comments argue for it."""
    container = _nested_container(
        inner_gitmodules_exits={"vendor/lib": 128},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert result.evidence["submodules_orphaned"] is None
    assert any("reading .gitmodules inside the submodule vendor/lib failed "
               "(exit 128)" in p for p in result.problems)


def test_a_status_initialised_path_that_is_not_a_repository_is_a_problem(
        monkeypatch, tmp_path):
    """THE THIRD DISAGREEMENT, and the only one the recursion can see. A
    leading-space status line says "initialised, HEAD at the gitlink"; a
    non-empty `--show-prefix` says the directory is not a repository at all.
    One reader is wrong about the tree the suite will run in, and the descent
    stopped there, so anything below is unread."""
    container = _ScriptedContainer(
        start_sha="s" * 40, tests=_FakeTests(), present=("tests/",),
        gitlinks=("vendor/lib",),
        submodule_status=(
            " 1111111111111111111111111111111111111111 vendor/lib"
            " (heads/main)\n"
        ),
        own_repo={"vendor/lib": False},
        ls_entries={"vendor/lib": ("libdep",)},
    )

    result = _run_preflight(monkeypatch, tmp_path, _FakeTask(), container)

    assert not result.ok
    assert any("is not a repository of its own" in p and "vendor/lib" in p
               for p in result.problems)
