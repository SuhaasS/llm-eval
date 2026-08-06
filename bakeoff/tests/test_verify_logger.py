"""Tests for the section 6.6 gate itself.

The gate is the deliverable an operator actually runs, and its most
important behaviour is a REFUSAL: reporting incomplete rather than passing
when it could not run the checks that see the capture path. Nothing else
tests that, and a gate that returns 0 when it skipped half its work is worse
than no gate -- it converts an unknown into a false assurance.

The decision logic is exercised in-process rather than by invoking the
script. Running it for real would execute pytest, which would collect this
file, which would run it again.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_logger.py"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("verify_logger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_runner(failing: str | None = None):
    """subprocess.run stand-in: fails only the command containing `failing`."""
    seen: list[list[str]] = []

    def run(command, **_kwargs):
        seen.append(command)
        joined = " ".join(command)
        code = 1 if failing and failing in joined else 0
        return subprocess.CompletedProcess(command, code)

    run.seen = seen  # type: ignore[attr-defined]
    return run


def test_gate_passes_when_every_check_passes(gate, monkeypatch):
    monkeypatch.setattr(gate, "docker_available", lambda: True)
    monkeypatch.setattr(gate.subprocess, "run", _fake_runner())
    monkeypatch.setattr(gate.sys, "argv", ["verify_logger.py"])

    assert gate.main() == 0


def test_gate_fails_when_any_check_fails(gate, monkeypatch):
    monkeypatch.setattr(gate, "docker_available", lambda: True)
    monkeypatch.setattr(gate.subprocess, "run", _fake_runner(failing="dry_run.py"))
    monkeypatch.setattr(gate.sys, "argv", ["verify_logger.py"])

    assert gate.main() == 1


def test_gate_refuses_to_pass_without_a_docker_daemon(gate, monkeypatch, capsys):
    """The load-bearing case.

    Container-killed-mid-run, the proxy-side wire log and live checkpoint
    capture are only observable against a real daemon. Passing on the
    strength of the checks that cannot see them would certify the capture
    path using evidence that says nothing about it.
    """
    monkeypatch.setattr(gate, "docker_available", lambda: False)
    monkeypatch.setattr(gate.subprocess, "run", _fake_runner())
    monkeypatch.setattr(gate.sys, "argv", ["verify_logger.py"])

    assert gate.main() == 1
    out = capsys.readouterr().out
    assert "GATE INCOMPLETE" in out
    assert "GATE PASSED" not in out


def test_no_docker_flag_is_an_escape_hatch_not_a_pass(gate, monkeypatch, capsys):
    monkeypatch.setattr(gate, "docker_available", lambda: True)
    monkeypatch.setattr(gate.subprocess, "run", _fake_runner())
    monkeypatch.setattr(gate.sys, "argv", ["verify_logger.py", "--no-docker"])

    assert gate.main() == 1
    assert "GATE INCOMPLETE" in capsys.readouterr().out


def test_docker_checks_are_skipped_rather_than_run_and_failed(gate, monkeypatch):
    """Without a daemon the container commands must not be executed at all.
    Running them produces a Docker connection traceback, which buries the
    one thing the operator needs to read: start Docker."""
    monkeypatch.setattr(gate, "docker_available", lambda: False)
    runner = _fake_runner()
    monkeypatch.setattr(gate.subprocess, "run", runner)
    monkeypatch.setattr(gate.sys, "argv", ["verify_logger.py"])

    gate.main()
    joined = [" ".join(c) for c in runner.seen]
    assert not any("-m integration" in c or "dry_run.py" in c for c in joined)
    assert any("not integration" in c for c in joined)


def test_the_gate_runs_the_interpreter_that_holds_the_dependencies(gate):
    """A bare `python` resolves to whatever is first on PATH, which on this
    machine is not the venv the harness is installed into. The gate would
    then fail on an import error that has nothing to do with the logger."""
    commands = [command for _name, command, _docker in gate.CHECKS]
    assert all(command[0] == gate.sys.executable for command in commands)
    assert not any(part == "python" for command in commands for part in command)


def test_the_integration_check_sets_a_basetemp_under_home(gate):
    """tests/conftest.py documents this: the Docker VM on macOS mounts $HOME
    but not /var/folders, so a repo bind-mounted from pytest's default
    tmp_path appears inside the container as a silently EMPTY directory. The
    snapshot tests then compare nothing against nothing and pass."""
    integration = next(
        command for name, command, _d in gate.CHECKS if "integration" in name
    )
    basetemp = next(a for a in integration if a.startswith("--basetemp="))
    assert basetemp.split("=", 1)[1].startswith(str(Path.home()))


def test_the_gate_does_not_run_the_same_suite_twice(gate):
    """test_fault_injection.py lives under tests/ and is already collected.
    Naming it again doubles the runtime for no added coverage, and a slow
    gate is a gate that gets skipped."""
    commands = [" ".join(command) for _n, command, _d in gate.CHECKS]
    assert not any("test_fault_injection.py" in c for c in commands)
