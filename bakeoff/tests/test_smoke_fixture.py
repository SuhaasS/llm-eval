"""The smoke fixture has to be red before the fix and green after it.

This is the assertion Phase 0c ran without. `docker/eval-agent.Dockerfile`
shipped no test runner, and `tests/test_calc.py` imports `calc` from the repo
root, so the only verification command available inside the container --
`python3 tests/test_calc.py` -- raised ModuleNotFoundError whether or not the
bug had been fixed. Gemma spent 30 of 30 turns on it and was recorded as 9/9
failures, which read as capability (spec section 6.4 calls that class an
adapter/environment failure, not a model finding).

Both halves matter and neither alone is sufficient:

  red-before  a fixture that passes with the bug still in it means the agent
              is scored on a task that was already done.
  green-after a fixture that fails after a correct fix means a solved run and
              an idle run leave identical evidence, and no amount of
              downstream logging can tell them apart.

Offline and daemon-free on purpose: this gates the environment the paid run
depends on, and a gate that needs the paid run to execute is not a gate. The
container half -- that `pytest` is actually ON PATH in the built image -- is
covered by `test_runner_integration.py`, because only a real image can show
that.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "smoke_task"

# The fix the smoke prompt asks for, verbatim in shape: one operator.
FIXED_CALC = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def smoke_repo(tmp_path):
    """A throwaway copy, so a failed assertion cannot leave the real fixture
    holding the fixed `calc.py` -- which would make every later run of this
    test pass for the wrong reason."""
    repo = tmp_path / "smoke_task"
    shutil.copytree(FIXTURE, repo)
    return repo


def _pytest_in(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_fixture_fails_with_the_bug_present(smoke_repo):
    result = _pytest_in(smoke_repo)

    assert result.returncode != 0, "the fixture passes with the bug still in it"
    # An assertion failure, NOT a collection or import error. This is the
    # distinction the missing pythonpath erased: ModuleNotFoundError is also a
    # non-zero exit, and an agent reading it cannot tell it from the bug.
    assert "assert -1 == 5" in result.stdout, result.stdout
    assert "ModuleNotFoundError" not in result.stdout + result.stderr


def test_fixture_passes_once_the_bug_is_fixed(smoke_repo):
    (smoke_repo / "calc.py").write_text(FIXED_CALC)

    result = _pytest_in(smoke_repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_the_fixture_ships_the_config_that_makes_calc_importable():
    """`pytest.ini` is load-bearing, not tidiness.

    Without `pythonpath = .` pytest puts tests/ on sys.path and `from calc
    import add` cannot resolve, in the image and on a developer's laptop
    alike. Deleting this file reproduces the Phase 0c environment exactly, so
    the file's existence is pinned rather than left to review.
    """
    ini = (FIXTURE / "pytest.ini").read_text()

    assert "pythonpath" in ini
    assert "." in ini.split("pythonpath", 1)[1].split("\n", 1)[0]
