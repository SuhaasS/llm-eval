"""Refuse to spend money on a task that cannot be shown to be a task.

This is the check the harness has never had. Everything else in the codebase
validates the HARNESS (capture, attribution, isolation) or a run's OUTPUT
(turns, tool calls, a diff). Nothing validated the INPUT, and that is the
defect that invalidated every Phase 0c capability figure: the image shipped
no pytest, the fixture was not importable, and the only verification command
available raised ModuleNotFoundError with the bug fixed and unfixed alike.
Gemma burned 30 of 30 turns on it and the 9/9 was read as capability.

`smoke_test.assert_agent_can_verify_its_work` was the first fix and it is too
weak: it proves a binary is on PATH. What has to be true is stronger and is
per task -- that THIS task, in THIS image, with the repo at THIS start state,
is red before the reference fix and green after it. A task that is green
before is a task that was already done; a task that is still red after a
correct fix makes a solved run and an idle run leave identical evidence, and
no amount of downstream logging can tell them apart.

Six of six "model failures" so far have been harness defects. That base rate
is the argument for running this before every matrix rather than trusting a
task author.

Offline: a Docker daemon and a materialized repo, no credentials, nothing
spent. Deliberately not folded into `verify_logger.py`, which is the section
6.6 LOGGING gate and whose defining property is that it needs neither a
daemon nor a network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bakeoff.container import RunContainer

# pytest's exit codes, which are the whole reason this module can tell "the
# bug is present" from "the environment is broken". Both are non-zero, and an
# `assert returncode != 0` passes on the second -- which is precisely the
# Phase 0c failure. See https://docs.pytest.org/en/stable/reference/exit-codes
EXIT_ALL_PASSED = 0
EXIT_TESTS_FAILED = 1
_EXIT_MEANING = {
    2: "collection was interrupted (an import error in a test module, most "
       "often a dependency the image does not ship)",
    3: "pytest hit an internal error",
    4: "usage error -- a selected node id does not exist",
    5: "no tests were collected",
    124: "the command hit the preflight timeout",
}

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# Files that would give one task a different agent context from another, and
# would do it invisibly: section 5.2 pins the session config precisely because
# CLAUDE.md and friends substantially change agent behaviour.
_CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md", ".claude", ".cursorrules")


@dataclass(frozen=True)
class PreflightResult:
    task_id: str
    task_version: int
    start_sha: str
    image: str
    manifest_digest: str
    problems: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "start_sha": self.start_sha,
            "image": self.image,
            "manifest_digest": self.manifest_digest,
            "problems": list(self.problems),
            "evidence": self.evidence,
            "ok": self.ok,
        }


def _explain(code: int) -> str:
    return _EXIT_MEANING.get(code, f"exit code {code}")


def failed_node_ids(output: str) -> set[str]:
    """Node ids pytest reported as FAILED or ERROR, from `-q` output.

    Parsed from the summary lines rather than from a JUnit report. The report
    would need `classname` mapped back to a node id, and that mapping is
    ambiguous -- a dotted segment is a package or a class and the XML does
    not say which -- so the parser becomes a second thing that can be wrong
    about what happened, sitting inside the check that exists to be right
    about it.
    """
    return {match.group(1) for match in _FAILED_LINE.finditer(output)}


class _Runner:
    """Test invocations inside the container, always under a timeout.

    `RunContainer.exec` blocks with no timeout of its own, so a suite that
    hangs would hang the gate that runs before every matrix. coreutils
    `timeout` is in the base image for exactly this -- Docker offers no way
    to kill a running exec from outside.
    """

    def __init__(self, container: RunContainer, runner: tuple[str, ...],
                 timeout_s: int):
        self.container = container
        self.runner = list(runner)
        self.timeout_s = timeout_s

    def run(self, extra: list[str]):
        return self.container.exec(
            ["timeout", str(self.timeout_s), *self.runner, *extra]
        )

    def select(self, node_ids: tuple[str, ...]):
        return self.run(list(node_ids))

    def deselect(self, node_ids: tuple[str, ...]):
        args: list[str] = []
        for node_id in node_ids:
            args += ["--deselect", node_id]
        return self.run(args)

    def pass_to_pass(self, tests):
        """The p2p set: whatever the manifest declared, or everything else.

        Both branches are real. An explicit list is what a task needs when
        part of its suite is legitimately red at base_sha and cannot be a
        regression check; the empty default is the honest one otherwise,
        because an enumerated copy of a pinned suite goes stale for no
        benefit. Reading the field only when it is non-empty is what keeps it
        from being a manifest key that looks like a measurement and is not.
        """
        if tests.p2p:
            return self.select(tests.p2p)
        return self.deselect(tests.f2p)


def preflight(
    task,
    image: str,
    repo_path: Path,
    start_sha: str,
    expected_claude_version: str = "",
    timeout_s: int = 600,
) -> PreflightResult:
    """Every reason this task is not a task. Empty `problems` means GO.

    Problems are COLLECTED rather than raised one at a time. A task with a
    missing dependency usually also fails red-before and green-after, and
    reporting the first one sends the author round the loop three times for
    one cause.
    """
    problems: list[str] = []
    evidence: dict = {}
    tests = task.tests

    if not any("pytest" in part for part in tests.runner):
        # The red/green distinction is built on pytest's exit codes. Another
        # runner may be addable later, but silently accepting one now would
        # mean `returncode != 0` again, which is the check that let Phase 0c
        # through.
        problems.append(
            f"tests.runner is {list(tests.runner)!r}; preflight can only "
            "distinguish 'tests failed' from 'the environment is broken' for "
            "pytest, and without that distinction the gate is worthless"
        )
        return PreflightResult(
            task_id=task.task_id, task_version=task.task_version,
            start_sha=start_sha, image=image,
            manifest_digest=task.manifest_digest,
            problems=tuple(problems), evidence=evidence,
        )

    with RunContainer(image=image, repo_path=str(repo_path),
                      base_sha=start_sha) as container:
        # --- the environment, because each of these reads as a model failure

        uid = container.exec(["id", "-u"]).stdout.strip()
        evidence["uid"] = uid
        if uid == "0":
            problems.append(
                "the image runs as root: Claude Code refuses bypassPermissions "
                "under root and exits before emitting a single event, so every "
                "arm would record zero turns"
            )

        version = container.exec(["claude", "--version"])
        evidence["claude_version"] = version.stdout.strip()
        if version.exit_code != 0:
            problems.append("`claude --version` failed: no agent in this image")
        elif expected_claude_version and not version.stdout.strip().startswith(
            expected_claude_version
        ):
            problems.append(
                f"claude is {version.stdout.strip()!r}, base image pins "
                f"{expected_claude_version!r}: two tasks would run different "
                "agents and the comparison across them is not one"
            )

        for tool in ("git", "rg"):
            if container.exec(["sh", "-c", f"command -v {tool}"]).exit_code != 0:
                problems.append(
                    f"{tool} is missing: "
                    + (
                        "snapshot_diff returns empty output, which is "
                        "byte-identical to a clean tree"
                        if tool == "git"
                        else "Claude Code needs it to search"
                    )
                )

        head = container.exec(["git", "rev-parse", "HEAD"]).stdout.strip()
        evidence["head"] = head
        if head != start_sha:
            problems.append(
                f"the container is at {head} but the start state is {start_sha}"
            )

        present = [
            name
            for name in _CONTEXT_FILES
            if container.exec(["test", "-e", name]).exit_code == 0
        ]
        if present:
            problems.append(
                f"the start state carries {', '.join(present)}: section 5.2 "
                "pins the session config, and a task-local agent file gives "
                "this task a context the others do not have"
            )

        runner = _Runner(container, tests.runner, timeout_s)

        # --- red before

        red = runner.select(tests.f2p)
        evidence["f2p_before_exit"] = red.exit_code
        if red.exit_code == EXIT_ALL_PASSED:
            problems.append(
                "the f2p tests PASS at the start state: the task is already "
                "done, and every arm would be scored on work it did not do"
            )
        elif red.exit_code != EXIT_TESTS_FAILED:
            problems.append(
                f"the f2p tests did not run at the start state -- "
                f"{_explain(red.exit_code)}. This is the Phase 0c failure: a "
                "broken environment is also a non-zero exit, and an agent "
                "reading the output cannot tell it from the bug.\n"
                + (red.stdout or red.stderr)[-2000:]
            )
        else:
            reported = failed_node_ids(red.stdout + red.stderr)
            missing = set(tests.f2p) - reported
            if missing:
                problems.append(
                    "declared f2p tests did not fail at the start state: "
                    + ", ".join(sorted(missing))
                )

        # --- p2p green before

        green = runner.pass_to_pass(tests)
        evidence["p2p_before_exit"] = green.exit_code
        if green.exit_code != EXIT_ALL_PASSED:
            problems.append(
                f"the rest of the suite is not green at the start state -- "
                f"{_explain(green.exit_code)}. A p2p regression check against "
                "an already-red suite cannot mean anything.\n"
                + (green.stdout or green.stderr)[-2000:]
            )

        # --- the suite does not dirty the tree

        status = container.exec(["git", "status", "--porcelain"]).stdout.strip()
        evidence["dirty_after_tests"] = status
        if status:
            problems.append(
                "running the suite leaves the tree dirty:\n"
                + status[:1000]
                + "\nSection 5.6 stages everything, so these land in every "
                "submission diff and diff size measures the interpreter rather "
                "than the agent. Add them to the manifest's gitignore_extra."
            )

        # --- green after

        patch = Path(repo_path) / ".bakeoff-solution.patch"
        patch.write_text(task.solution_diff)
        try:
            applied = container.exec(["git", "apply", ".bakeoff-solution.patch"])
        finally:
            patch.unlink(missing_ok=True)

        if applied.exit_code != 0:
            problems.append(
                "the reference fix does not apply to the start state: "
                + (applied.stderr or applied.stdout).strip()[:1000]
            )
        else:
            after_f2p = runner.select(tests.f2p)
            evidence["f2p_after_exit"] = after_f2p.exit_code
            if after_f2p.exit_code != EXIT_ALL_PASSED:
                problems.append(
                    f"the f2p tests do NOT pass after the reference fix -- "
                    f"{_explain(after_f2p.exit_code)}. A solved run and an "
                    "idle run would leave identical evidence. The usual cause "
                    "is a non-editable install: imports resolve to "
                    "site-packages, so nothing the agent writes to /repo has "
                    "any effect.\n"
                    + (after_f2p.stdout or after_f2p.stderr)[-2000:]
                )
            after_p2p = runner.pass_to_pass(tests)
            evidence["p2p_after_exit"] = after_p2p.exit_code
            if after_p2p.exit_code != EXIT_ALL_PASSED:
                problems.append(
                    "the reference fix regresses the rest of the suite -- "
                    f"{_explain(after_p2p.exit_code)}. The reference is the "
                    "oracle; if it cannot pass, no submission can.\n"
                    + (after_p2p.stdout or after_p2p.stderr)[-2000:]
                )

        # Leave the tree exactly as it was found.
        #
        # Not load-bearing today and deliberately kept anyway: the matrix
        # materializes a fresh tree per run and never reuses this one, so a
        # left-behind reference fix could not reach an arm. It is here because
        # that separation is a property of one caller rather than of this
        # function, and a preflight tree still holding the answer is a trap
        # for the next caller and for anyone who inspects it by hand.
        container.exec(["git", "checkout", "--force", "--detach", start_sha])
        container.exec(["git", "clean", "-xfd"])

    return PreflightResult(
        task_id=task.task_id,
        task_version=task.task_version,
        start_sha=start_sha,
        image=image,
        manifest_digest=task.manifest_digest,
        problems=tuple(problems),
        evidence=evidence,
    )
