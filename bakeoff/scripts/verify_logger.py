#!/usr/bin/env python3
"""Spec section 6.6 gate. Run before any real eval batch.

Exits non-zero if the logging layer cannot be trusted with 2,400 runs.

The logger is the one component whose failures are silent. A model that
breaks produces an obviously bad record; a logger that breaks produces a
record that looks fine and says something false -- and the event log has no
update API, so a wrong record is wrong permanently. Hence a gate, run
before collection rather than after.

Everything here is offline: no credentials, no spend. That is deliberate.
A gate that costs money to run is a gate that stops being run.

Usage:
    python scripts/verify_logger.py
    python scripts/verify_logger.py --no-docker   # unit checks only, FAILS
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Under $HOME, not /var/folders: the Docker VM on macOS mounts $HOME only, and
# a repo bind-mounted from elsewhere appears inside the container as a
# silently EMPTY directory. Snapshot tests then compare nothing against
# nothing and pass. tests/conftest.py documents this; the gate must not be
# the one place that forgets it.
BASETEMP = Path.home() / ".cache" / "bakeoff-gate"


def _pytest(*args: str) -> list[str]:
    # sys.executable, never bare "python": the deps live in this venv, and
    # bare python resolves to whichever interpreter is first on PATH.
    return [sys.executable, "-m", "pytest", *args]


# test_fault_injection.py is NOT listed separately. It lives under tests/ and
# is already collected by the unit and integration runs -- naming it again
# would run every case twice and make the gate slower for no added coverage.
CHECKS: list[tuple[str, list[str], bool]] = [
    (
        "unit suite (schema, log, pricing, parsing, scanners, classification)",
        _pytest("tests/", "-q", "-m", "not integration"),
        False,
    ),
    (
        "container + proxy integration (live capture, wire log, fault injection)",
        # Both exclusions are on purpose, and neither is optional. The
        # grader's integration tests build a task image and hit the repo
        # mirror; the judge's live tests make a real, BILLED call to the
        # pinned judge model -- `judge_live` through Bedrock and `codex_live`
        # through the Codex seat, two markers because the two are unblocked by
        # different credentials. This gate is documented as offline, no
        # credentials, no spend, and a marker that widened it would change
        # what the section 6.6 gate NEEDS without changing what it is called
        # -- an operator who ran it before a collection would find it failing
        # for want of a network it was promised not to use, or find it
        # quietly spending on a judge it was promised not to call.
        _pytest("tests/", "-q", "-m",
                "integration and not task_image and not judge_live "
                "and not codex_live",
                f"--basetemp={BASETEMP}"),
        True,
    ),
    (
        "end-to-end dry run (whole orchestrator, real container, real git)",
        [sys.executable, "scripts/dry_run.py"],
        True,
    ),
    # The real Claude Code binary, streaming, through a real proxy. Here
    # rather than left to the paid run because the streaming path is the
    # only one a live call uses, and it was NOT covered before: a
    # mock_response deployment short-circuits before litellm's streaming
    # wrapper, so the success callback never fires and capture silently
    # misses the call. That is the same class of defect as Task 11's
    # defect 15, and it is invisible to every other check here.
    (
        "offline smoke (real agent, streaming proxy, tool call, diff)",
        [sys.executable, "scripts/smoke_test.py", "--mode", "offline"],
        True,
    ),
]


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="skip the container checks (the gate still fails; see below)",
    )
    args = parser.parse_args()

    have_docker = docker_available()
    failures: list[str] = []
    skipped: list[str] = []

    for name, command, needs_docker in CHECKS:
        if needs_docker and (args.no_docker or not have_docker):
            skipped.append(name)
            continue
        print(f"\n=== {name} ===", flush=True)
        if subprocess.run(command, cwd=REPO).returncode != 0:
            failures.append(name)

    if failures:
        print(f"\nGATE FAILED: {', '.join(failures)}")
        print("Do not start an eval batch until these pass.")
        return 1

    if skipped:
        # NOT a pass. Half of section 6.6 -- container killed mid-run, the
        # proxy-side wire log, live checkpoint capture -- is only observable
        # against a real daemon. Reporting "passed" here would certify the
        # capture path on the strength of the checks that cannot see it,
        # which is the failure this gate exists to prevent.
        print("\nGATE INCOMPLETE: could not run " + "; ".join(skipped))
        reason = "--no-docker was passed" if args.no_docker else "no Docker daemon"
        print(f"Reason: {reason}.")
        print("The container and proxy cases are where the wire log, live")
        print("checkpoints, and mid-run kill are verified. Start Docker and")
        print("re-run before collecting any data.")
        return 1

    print("\nGATE PASSED: logging layer verified.")
    print("Verified offline: no model was called and nothing was spent.")
    print("Sampling reaches the wire on both routes now -- the streaming")
    print("Anthropic path and, via the resolved-params channel, the openai/")
    print("one, where the record reports the renamed cap and the pinned")
    print("reasoning_effort rather than Claude Code's request.")
    print("NOT verified here: that a real model completes a real task, or")
    print("that Bedrock routing and credentials work. Both are Phase 0c")
    print("(Task 12).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
