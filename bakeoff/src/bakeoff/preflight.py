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
from dataclasses import fields as dataclass_fields
from pathlib import Path, PurePosixPath

from bakeoff.container import RunContainer

# pytest's exit codes, which are the whole reason this module can tell "the
# bug is present" from "the environment is broken". Both are non-zero, and an
# `assert returncode != 0` passes on the second -- which is precisely the
# Phase 0c failure. See https://docs.pytest.org/en/stable/reference/exit-codes
EXIT_ALL_PASSED = 0
EXIT_TESTS_FAILED = 1
#: Collection was interrupted. Reached by a run that collects a DIRECTORY or a
#: MODULE PATH -- not by the f2p selection, which is positional node ids. See
#: EXIT_USAGE_ERROR.
EXIT_COLLECTION_INTERRUPTED = 2
#: Two causes, and telling them apart is the whole of broadening 2. Measured
#: 2026-09-01 against pytest 9.1.1 and 8.3.5: selecting `mod.py::test` when
#: `mod.py` raises on import exits 4 with an `ERROR mod.py` summary line, and
#: selecting a node id that does not exist in a module that imports fine ALSO
#: exits 4 -- with `ERROR: not found:` (colon), which `_FAILED_LINE` does not
#: match, so the reported set is empty. The first is a task shape to accept;
#: the second is a manifest typo that must keep stopping the matrix.
EXIT_USAGE_ERROR = 4
#: The scoped run's failure mode, and the whole of its detection. No "collected
#: 0 items" summary matching: the pinned runners carry `-q`, which suppresses
#: that line (measured), so a guard on the string could not fire under the
#: configuration actually used -- the dead-guard shape a mutation cannot catch.
#: `-q` does NOT suppress "Interrupted: N error during collection" (also
#: measured); nothing parses that line and nothing should.
EXIT_NOTHING_COLLECTED = 5
#: The two codes a collection error can arrive as. 4 first, because that is the
#: one preflight's own f2p run produces.
EXIT_COLLECTION_FAILURES = (EXIT_USAGE_ERROR, EXIT_COLLECTION_INTERRUPTED)
_EXIT_MEANING = {
    2: "collection was interrupted (an import error in a test module, most "
       "often a dependency the image does not ship)",
    3: "pytest hit an internal error",
    4: "usage error -- a selected node id does not exist, OR the module it "
       "names could not be imported",
    5: "no tests were collected",
    124: "the command hit the preflight timeout",
}

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

#: What this gate asserts, as a version. It joins `run_matrix`'s preflight
#: cache key and the grader's, because none of the other three components
#: (manifest digest, image id, start sha) moves when THIS file changes -- so
#: without it every warm cache serves a verdict written by the old gate and a
#: newly added assertion is inert on exactly the tasks about to be run. Bump
#: it with any change to what preflight asserts. 1 is the implicit version of
#: every verdict cached before the scoped-p2p and grading assertions landed.
#: 3 adds the `strip_paths` assertion: a verdict cached under 2 was written
#: by a gate that never looked at that key at all.
#: 4 accepts an f2p run that could not COLLECT (broadening 2). A verdict
#: cached under 3 was written by a gate that refused that task shape outright,
#: and one cached under 4 was written by a gate whose p2p-before run carries
#: `--ignore` on exactly those tasks.
#: 5 adds two environment assertions: the `image.env` read-back, and the
#: refusal of a task whose declared test paths IMPORT hypothesis while the
#: manifest declares no CI (availability alone is not the trigger -- the
#: package is a common transitive dependency). A verdict cached under 4 was
#: written by a gate that looked at neither, so a task whose determinism lever
#: silently failed to apply -- or was never declared -- would keep serving a
#: PASS.
PREFLIGHT_VERSION: str = "5"


def preflight_cache_key(task, image: str, start_sha: str) -> str:
    """What a cached PASS is keyed on -- including the gate that produced it.

    `PREFLIGHT_VERSION` is in here because none of the other three components
    moves when `preflight.py` changes: a manifest digest describes the task, an
    image id describes the environment, a start sha describes the tree, and a
    new assertion touches none of them. Without the version every warm cache
    serves a verdict written by the OLD gate, and an assertion added to catch a
    defect is inert on exactly the tasks about to be run -- the pruned mirror's
    "an older revision's output is served forever" defect, one subsystem over.

    It lives HERE, beside the constant it depends on, and not in either driver.
    Two consumers now read the same caches -- `run_matrix` writes
    `preflight.json`, `grade.py` reads it and writes `preflight-grade.json` --
    and a key defined in one of them and imported by the other makes the
    collection driver a dependency of the offline grader for one f-string. Two
    COPIES would be worse still: that is how a verdict written under one gate
    gets served to another.
    """
    return f"{task.manifest_digest}|{image}|{start_sha}|{PREFLIGHT_VERSION}"

#: A declared `tests.paths` prefix that does not exist at the post-fix state.
#: NOT a problem: `PreflightResult.ok` is `not problems`, and the grader's
#: restore step tolerates exactly this input, so a problem here would NO-GO a
#: task the ladder was built to grade. The code and the evidence keep the
#: author error loud without making it fatal.
SCOPE_PREFIX_MISSING = "scope_prefix_missing"

#: The scoped p2p run collected nothing -- pytest exit 5, or no declared
#: prefix survived the existence filter. This one IS a NO-GO, and the driver
#: branches on the code rather than on a prose prefix: closed sets, not
#: composite strings, applies to this repo's own dataclass first.
SCOPE_COLLECTS_NOTHING = "scope_collects_nothing"

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
    #: Which gate produced this verdict. A stored verdict outlives the code
    #: that wrote it, and every grade copies this as
    #: `graded_under_preflight_version` -- discrimination is preflight's
    #: claim, so a grade that requires it has to name the gate that made it.
    preflight_version: str = ""
    #: A typed channel beside the prose `problems`, for the outcomes a caller
    #: has to BRANCH on. Nothing machine-reads `problems`; the grade driver
    #: would have been the first, through a string-prefix match no test can
    #: really guard. Not every code is a problem -- see SCOPE_PREFIX_MISSING.
    problem_codes: tuple[str, ...] = ()

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
            "problem_codes": list(self.problem_codes),
            "evidence": self.evidence,
            "preflight_version": self.preflight_version,
            "ok": self.ok,
        }


def _explain(code: int) -> str:
    return _EXIT_MEANING.get(code, f"exit code {code}")


def _present(container, names, *, dangling_counts: bool = False) -> list[str]:
    """Which of `names` exist in the container's tree, in declared order.

    `test -e` FOLLOWS symlinks, which is right for a context file -- sqlglot's
    CLAUDE.md is a symlink to AGENTS.md, and a DANGLING link named `.claude`
    gives the agent no context at all.

    `dangling_counts` adds a `-L` probe, and the strip assertion needs it for
    the opposite reason: its claim is "this path is gone from the tree", and a
    link whose target was stripped is a path the agent's `ls` still shows.
    Measured 2026-09-01: for a symlink to a missing target, `[ -e x ]` exits 1
    and `[ -L x ]` exits 0.

    One helper for every caller -- the context files, the scope filter (used
    by preflight and by the grader's check 6 alike) and the strip -- because a
    second copy of this probe is where the symlink semantics drift.
    """
    probes = ["-e", "-L"] if dangling_counts else ["-e"]
    return [
        name for name in names
        if any(container.exec(["test", probe, name]).exit_code == 0
               for probe in probes)
    ]


def _observed_env(container, keys: tuple[str, ...]) -> dict[str, str | None]:
    """What the container's environment actually holds, per declared key.

    `printenv`, never `sh -c 'echo $KEY'`, because the exit code is the whole
    discriminator: measured 2026-09-01, `printenv KEY` exits 0 with the value
    even when that value is the empty string, and exits 1 with empty stdout
    when the key is unset. `echo` cannot tell those apart, and two different
    absences that render identically are the same defect one layer down.
    `None` here means "not set"; `""` means "set to nothing".

    Only the DECLARED keys are read. A dump of the whole environment would put
    values nobody asked about into `preflight.json`, which outlives the run
    and is read by hand.
    """
    observed: dict[str, str | None] = {}
    for key in keys:
        result = container.exec(["printenv", key])
        observed[key] = (
            result.stdout.rstrip("\n") if result.exit_code == 0 else None
        )
    return observed


def _declared_env(task) -> dict[str, str]:
    """The manifest's `image.env`, or {}.

    `getattr` twice, like `_declared_grading`'s and the strip's: this module's
    entry point takes an untyped `task`, and a manifest object predating the
    key must not crash the gate.
    """
    return dict(getattr(getattr(task, "image", None), "env", {}) or {})


#: Basenames that mean "this argv element is a Python interpreter".
#: `python`, `python3`, and `pythonX.Y` -- the three shapes a `tests.runner`
#: actually carries. Matched on the BASENAME so an absolute
#: `/usr/local/bin/python3.12` counts, and anchored so `pythonish-wrapper`
#: does not.
_PYTHON_BASENAME = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")


def _runner_python(runner: tuple[str, ...]) -> str:
    """The interpreter this task's suite runs under, or "python".

    The availability probe has to ask the interpreter the RUNNER uses, not
    whichever `python` is first on PATH: a task whose runner is
    `["/opt/venv/bin/python", "-m", "pytest", ...]` resolves imports against
    that venv's site-packages, and probing the system interpreter would answer
    a question about a different environment. `click-3360`'s runner is
    `["python", "-m", "pytest", ...]`, so the common case is unchanged.

    Falls back to "python" when `runner[0]` is not an interpreter at all --
    `["pytest", "-q", ...]` is a legal runner, and the console script gives no
    interpreter path to reuse. The fallback is a guess and is allowed to be:
    this probe decides whether to ASK for `CI`, and preflight's runner check
    has already established that `pytest` is in the argv.
    """
    if runner and _PYTHON_BASENAME.match(PurePosixPath(runner[0]).name):
        return runner[0]
    return "python"


def _existing_prefixes(container, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """The declared prefixes that exist in the tree, in declared order.

    The same filter the grader's check 6 applies, and it has to be the same
    one: an unfiltered positional prefix makes pytest exit 4 (usage error) on
    exactly the input the grader's restore step already tolerates -- a
    declared path absent at the start state, which `_validate_prefixes`
    accepts on purpose. Filtered here and unfiltered there, or the reverse,
    and the gated argv is not the graded argv.
    """
    return tuple(_present(container, prefixes))


def _declared_grading(task) -> list[tuple[str, tuple[str, ...]]]:
    """The non-empty `grading.*` argvs, keyed by check name.

    The keys come off the dataclass rather than a hand-written list here, for
    the reason `tasks._GRADING_KEYS` gives: a copy goes stale the first time a
    check is added, and its failure is the silent one -- the new key is
    accepted by the loader and never validated by the gate.
    """
    grading = getattr(task, "grading", None)
    if grading is None:
        return []
    return [
        (spec.name, tuple(getattr(grading, spec.name)))
        for spec in dataclass_fields(grading)
        if getattr(grading, spec.name)
    ]


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


def collection_error_modules(output: str) -> frozenset[str] | None:
    """The test modules pytest could not COLLECT -- or `None` for anything else.

    Section 3.3's loop ends in "runs tests, sees failures, self-corrects", and
    a PR that ADDS a symbol hands the agent an `ImportError` instead of an
    assertion. That is a real task shape, and it is one preflight refused
    outright until broadening 2, because both of its exit codes (4 for a
    selection, 2 for a directory sweep) are also what a broken image gives.

    The discriminator is `::`, and it is the whole parser. Measured 2026-09-01
    against pytest 9.1.1 and 8.3.5, under the pinned `-q -p no:cacheprovider`:

    * a module that raised on import          -> `ERROR tests/new.py`
    * a test that failed                      -> `FAILED tests/new.py::test_x`
    * a test whose fixture raised             -> `ERROR tests/new.py::test_x`
    * a node id that does not exist           -> `ERROR: not found: ...`
    * a path that does not exist              -> `ERROR: file or directory ...`
    * a broken `tests/conftest.py`            -> no `short test summary info`
      section at all -- exit 4 with the reported set EMPTY, so the run is
      UNCONFINED (measured 2026-09-01, pytest 9.1.1 and 8.3.5)

    The last three carry no `ERROR <path>` line matching `_FAILED_LINE` --
    the first two are prefixed with a colon `_FAILED_LINE` does not match, the
    third prints no summary section for the parser to find at all -- so they
    arrive here as an empty set, which is refused: a manifest naming a renamed
    test, or a task whose conftest cannot even import, must keep stopping the
    matrix rather than being read as "the module could not be collected".

    `None` rather than an empty frozenset for the refusal: "no collection
    errors" and "collection errors mixed with test results" are different
    facts, and a caller comparing an empty set against the declared modules
    would silently accept the second on a task that declares no f2p ids.

    The comparison against `f2p_modules` is deliberately NOT made here. The two
    callers need different ones -- preflight equality, the grader containment
    (see the offline-grader spec, check 5) -- and folding both behind a flag
    would make each call site unreadable about which claim it is making.
    """
    reported = failed_node_ids(output)
    if not reported or any("::" in item for item in reported):
        return None
    return frozenset(reported)


def f2p_modules(f2p: tuple[str, ...]) -> frozenset[str]:
    """The module half of each declared f2p node id.

    Split once, from the LEFT: a parametrized id can carry `::` inside its
    brackets (`tests/a.py::test_one[x::y]`) and a class-scoped id carries two,
    so `rsplit` or an unbounded `split` would name something that is not a
    module and the equality in `preflight` would never hold.
    """
    return frozenset(node_id.split("::", 1)[0] for node_id in f2p)


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
        #: The argv of the most recent invocation, so a problem can name what
        #: was actually run rather than a reconstruction of it -- a second
        #: copy of the branch logic is a second thing that can be wrong about
        #: what happened, inside the check that exists to be right about it.
        self.last_argv: list[str] = []

    def run(self, extra: list[str]):
        self.last_argv = ["timeout", str(self.timeout_s), *self.runner, *extra]
        return self.container.exec(self.last_argv)

    def select(self, node_ids: tuple[str, ...]):
        return self.run(list(node_ids))

    def pass_to_pass(self, tests, extra_deselect: tuple[str, ...] = (),
                     scope: tuple[str, ...] = (),
                     ignore: tuple[str, ...] = ()):
        """The p2p set: whatever the manifest declared, or everything else.

        Both branches are real. An explicit list is what a task needs when
        part of its suite is legitimately red at base_sha and cannot be a
        regression check; the empty default is the honest one otherwise,
        because an enumerated copy of a pinned suite goes stale for no
        benefit. Reading the field only when it is non-empty is what keeps it
        from being a manifest key that looks like a measurement and is not.

        The first two keyword parameters exist for the offline grader and
        default to inert: `extra_deselect` appends --deselect for the flake
        quarantine, and `scope` prepends path prefixes to the deselect branch
        so check 6 grades the repo's declared suite rather than whatever
        scratch files an agent left at the rootdir (measured: eight of them in
        one stored record). With both empty, the argv is byte-identical to what
        preflight validated -- a test pins that, because the moment the graded
        command and the gated command drift apart, the oracle stops describing
        the thing being graded.

        `ignore` appends `--ignore=<path>` and has exactly ONE caller:
        preflight's p2p run at the START state, on a task whose f2p module does
        not import there. That run sweeps the rootdir, so the erroring module
        aborts collection before `--deselect` is ever applied -- measured
        2026-09-01, pytest 9.1.1 and 8.3.5, exit 2 -- and the p2p baseline the
        acceptance depends on cannot be observed at all without it. The
        flag is spliced into both branches below, so on a task with an
        explicit `tests.p2p` it is emitted and inert -- positional ids never
        collect the f2p module -- which is why one splice point, not two, is
        the honest shape.

        It is safe here and only here because preflight's p2p-BEFORE run is not
        an argv the grader ever makes: the graded p2p runs at the
        post-submission state, and the preflight run that must match it
        byte-for-byte is p2p-AFTER, which gets no `ignore`. What the ignore
        hides -- a non-f2p test inside the ignored module -- is measured by
        that same p2p-after run, which collects the module once the reference
        lands and must exit 0.
        """
        extra = [arg for node_id in extra_deselect
                 for arg in ("--deselect", node_id)]
        extra += [f"--ignore={path}" for path in ignore]
        if tests.p2p:
            return self.run([*tests.p2p, *extra])
        args: list[str] = [*scope]
        for node_id in tests.f2p:
            args += ["--deselect", node_id]
        return self.run([*args, *extra])


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
    problem_codes: list[str] = []
    evidence: dict = {}
    tests = task.tests

    # Written BEFORE the guard below, because `image.env` is a property of the
    # manifest and is knowable with no daemon. The other three keys stay None
    # on this path: `observed: {}` and `mismatch: []` would be a CLAIM that
    # the gate looked and agreed, from a gate that never started a container.
    # Two absences that render identically are the same defect one layer down.
    evidence["image_env_declared"] = declared = _declared_env(task)
    evidence["image_env_observed"] = None
    evidence["image_env_mismatch"] = None
    evidence["hypothesis_importable"] = None
    evidence["hypothesis_imported_by_suite"] = None

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
            preflight_version=PREFLIGHT_VERSION,
            problem_codes=tuple(problem_codes),
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

        present = _present(container, _CONTEXT_FILES)
        if present:
            problems.append(
                f"the start state carries {', '.join(present)}: section 5.2 "
                "pins the session config, and a task-local agent file gives "
                "this task a context the others do not have"
            )

        # The strip, checked against the TREE rather than against the manifest
        # or the code that performed it. `materialize` raises when a declared
        # path matches nothing, so this cannot fire on a typo -- what it
        # catches is the artifact disagreeing with the manifest for any other
        # reason (a build step re-creating the path, a stale preflight tree, a
        # future change to the strip that stops working). A strip that did not
        # happen is invisible: the file is in every arm's context and in every
        # submission diff, and no later stage re-derives it.
        #
        # `dangling_counts=True`: stripping a symlink's target and not the link
        # leaves a path `test -e` calls absent and `ls` still shows.
        #
        # `getattr`, like `_declared_grading`'s: this function takes an
        # untyped `task` and a manifest object predating the key must not
        # crash the gate.
        # Both keys are written unconditionally: "this task strips nothing"
        # and "the gate did not look" render identically as a missing key, and
        # absence is recorded rather than implied.
        stripped = tuple(getattr(task, "strip_paths", ()))
        still_there = (
            _present(container, stripped, dangling_counts=True)
            if stripped else []
        )
        evidence["stripped_paths"] = list(stripped)
        evidence["stripped_paths_present"] = still_there
        if still_there:
            problems.append(
                f"the start state still carries {', '.join(still_there)}, "
                "which the manifest's strip_paths says it removed. Section "
                "5.2 pins the session config and section 5.6 stages "
                "everything, so an un-stripped path is both a context this "
                "task has and the others do not, and a file in every "
                "submission diff."
            )

        # The environment, checked against the CONTAINER rather than against
        # the manifest. `image.env` is configuration; what the container holds
        # is the observation, and this is the only place the two meet.
        #
        # An un-applied image.env is invisible in the worst way. Its whole job
        # is determinism -- measured 2026-09-01 against hypothesis 6.167.1, a
        # property-based suite gives `0 0 0 0 1 1 1 1 0 0` over ten fresh runs
        # of unchanged code and `1 1 1 1 1 1` under CI=1 -- so a value that
        # did not take means the gate passed on a lucky draw and every arm is
        # scored against an oracle that answers differently per run.
        #
        # Not redundant with "we generated the Dockerfile ourselves": a stale
        # tag (a task edited without a task_version bump moves manifest_digest
        # and need not move the image id), a base image whose own ENV later
        # collides, and a future edit that emits the lines in the wrong place
        # all leave the rendering tests green.
        #
        # `getattr`, like `_declared_grading`'s and the strip's: this function
        # takes an untyped `task`, and a manifest object predating the key
        # must not crash the gate.
        #
        # BEFORE `_Runner` is built, so a bad environment is reported as
        # itself rather than as five downstream suite failures.
        #
        # `image_env_declared` was written before the guard above, on every
        # path including the one that never reaches a container. What this
        # block writes is the pair only a container can answer -- `observed`
        # and `mismatch` -- overwriting the `None`s set there. Both are
        # written whether or not anything was declared: "this task declares no
        # environment" and "the gate did not look" render identically as a
        # missing key, and a cached verdict outlives the code that wrote it.
        observed = _observed_env(container, tuple(sorted(declared)))
        mismatch = sorted(
            key for key, value in declared.items() if observed.get(key) != value
        )
        evidence["image_env_observed"] = observed
        evidence["image_env_mismatch"] = mismatch
        if mismatch:
            problems.append(
                "the container's environment does not match the manifest's "
                "image.env for "
                + ", ".join(
                    f"{key} (declared {declared[key]!r}, container "
                    f"{observed.get(key)!r})" for key in mismatch
                )
                + ". That key is baked as a Dockerfile ENV so it reaches "
                "every process in the container, including the ones the agent "
                "invents; a value that did not take is silent -- the suite "
                "goes back to being nondeterministic and the gate passes on a "
                "lucky draw. Rebuild the task image (a task edited without a "
                "task_version bump moves manifest_digest and need not move "
                "the image id)."
            )

        # The other direction, and the only check that catches the manifest
        # nobody wrote. Everything above compares DECLARED against OBSERVED,
        # so a property-based task whose author never declared CI passes all
        # of it -- declared is {}, observed is {}, mismatch is empty, GO --
        # and the ladder then runs on a lucky draw. Measured 2026-09-01
        # against hypothesis 6.167.1, that draw is `0 0 0 0 1 1 1 1 0 0` over
        # ten fresh runs of unchanged code on an unchanged tree.
        #
        # TWO probes, and the second is what keeps the refusal honest.
        # INSTALLED is not USED: hypothesis is a common transitive dependency
        # (a dev-extra, a `pip install -e .[test]` in image.build, a
        # dependency of a dependency), and a NO-GO on availability alone would
        # refuse a task whose suite never imports it -- with a remedy the
        # author cannot apply, since nothing they wrote put it there.
        #
        # The interpreter comes off `tests.runner` rather than being
        # hardcoded: a runner of ["/opt/venv/bin/python", "-m", "pytest"]
        # resolves imports against that venv, so probing whichever `python` is
        # first on PATH would answer a question about a different environment.
        importable = container.exec(
            [_runner_python(tests.runner), "-c", "import hypothesis"]
        ).exit_code == 0
        evidence["hypothesis_importable"] = importable

        # `rg` is asserted present above, so this adds no dependency. Filtered
        # through `_present` for the reason `_existing_prefixes` gives: a
        # declared prefix absent at the start state is an input this gate
        # tolerates, and handing rg a path that does not exist makes it exit 2
        # -- which must read as "could not answer", not as "no match".
        scanned = _present(container, tests.paths)
        used: bool | None = None
        if scanned:
            probe_argv = ["rg", "-q", r"^\s*(from|import)\s+hypothesis\b",
                          *scanned]
            probe = container.exec(probe_argv)
            # Three-valued on purpose. 0 is a match, 1 is no match, and
            # anything else (an unreadable path, a bad pattern, no rg) is
            # UNKNOWN -- a quiet False there would silently disarm the only
            # check that catches an undeclared property-based suite.
            #
            # UNKNOWN is not silent either. `scanned` is non-empty here, so
            # the probe RAN and did not answer -- a controller-ruling
            # ambiguity, not the "never ran" shape below where `scanned` is
            # empty and no exec happens at all. The two render identically as
            # `None` in `evidence`, which is exactly the pair this repo's
            # "a null says which kind of null it is" rule exists for, so the
            # exit-code case gets a problem naming what was run and what came
            # back, and the never-ran case stays a quiet `None`.
            if probe.exit_code == 0:
                used = True
            elif probe.exit_code == 1:
                used = False
            else:
                problems.append(
                    "the hypothesis-import probe could not answer: `"
                    + " ".join(probe_argv) + f"` exited {probe.exit_code}, "
                    "not 0 (match) or 1 (no match). rg exits 2 on an "
                    "unreadable path or a bad pattern and it is asserted "
                    "present above, so this names an environment problem "
                    "preflight cannot see through -- silently reading it as "
                    "'not imported' would disarm the one check that catches "
                    "an undeclared property-based suite."
                )
        evidence["hypothesis_imported_by_suite"] = used

        if used and "CI" not in declared:
            problems.append(
                "the declared test paths import hypothesis and the manifest "
                "declares no image.env CI. A property-based suite without it "
                "is a coin flip -- measured, ten fresh runs of one property "
                "test over unchanged code gave `0 0 0 0 1 1 1 1 0 0`, and six "
                "under CI=1 gave `1 1 1 1 1 1` -- so the gate would be "
                "certifying a task whose red-before/green-after verdict is a "
                "draw. Add `image.env: {CI: \"1\", "
                "HYPOTHESIS_STORAGE_DIRECTORY: \"/tmp/bakeoff-hypothesis\"}` "
                "and read HARVESTING.md's Layer 2 bullet before doing so -- "
                "determinism makes this oracle reproducible, not correct. "
                "(This fires on an IMPORT in tests.paths, not on the package "
                "being installed: hypothesis arriving transitively through "
                "image.build in a suite that never uses it is fine and is "
                "recorded as hypothesis_importable without a problem.)"
            )

        runner = _Runner(container, tests.runner, timeout_s)

        # --- red before, and the p2p baseline it is judged against
        #
        # Two runs, ONE verdict, and the verdict comes last. A task whose fix
        # ADDS a symbol puts that symbol in the solution half, so the test half
        # raises ImportError at the start state and pytest exits 4 (measured
        # 2026-09-01, pytest 9.1.1 and 8.3.5: a positional NODE ID whose module
        # will not import is a usage error, not the collection-interrupted 2 a
        # directory sweep gives). That is a real task shape -- section 3.3's
        # loop still runs, the agent just reads an ImportError instead of an
        # assertion, which is exactly what the human who filed the issue read.
        #
        # It is accepted only under a THREE-way conjunction, because the parse
        # alone cannot carry it -- the f2p selection imports ONLY the f2p
        # modules, so an environment defect there produces exactly the confined
        # error set the task shape produces. The three, and what each one and
        # only it can see:
        #
        #   (i)   the errors are confined to the declared f2p modules -- no
        #         stranger module errored, and no declared module stayed quiet;
        #   (ii)  p2p is green BEFORE -- a globally broken image is refused
        #         here, and the regression baseline exists at all;
        #   (iii) f2p is green AFTER -- and this is the ONLY one that refuses a
        #         dependency imported solely by the f2p module, which is
        #         confined under (i) and leaves (ii) green. That is exactly the
        #         Phase 0c integration fixture.
        #
        # (iii) is asserted unconditionally by the green-after block further
        # down and needs nothing here. It is named here because a reader who
        # believes (i) and (ii) suffice will eventually simplify it away.
        #
        # The runs stay in this order -- their order is what each sees of the
        # tree -- and the JUDGEMENT is deferred instead. Problems are collected
        # rather than raised, so nothing else about this function has to move.

        red = runner.select(tests.f2p)
        evidence["f2p_before_exit"] = red.exit_code

        collected = (
            collection_error_modules(red.stdout + red.stderr)
            if red.exit_code in EXIT_COLLECTION_FAILURES else None
        )
        # EQUALITY, in both directions. A module erroring that no f2p id names
        # is a broken environment; a declared f2p module that did NOT error is
        # a declared id nobody checked -- and measured, a partial collection
        # error hides the rest of the selection entirely, so the id-level rule
        # ("every declared f2p id appears in FAILED/ERROR") is unsatisfiable
        # here and this is that same claim at module granularity.
        confined = collected is not None and collected == f2p_modules(tests.f2p)
        # Written on every path, all three keys: "this task has no collection
        # errors" and "the gate did not look" render identically as a missing
        # key, and a cached verdict outlives the code that wrote it.
        evidence["f2p_collection_errors"] = sorted(collected or ())
        # Four values, not three: "the task is already done" and "the gate
        # could not classify this run" are different facts about a cached
        # verdict, and one name for both is the defect one layer down that
        # "absence is recorded, never implied" exists to prevent.
        evidence["f2p_red_kind"] = (
            "collection_error" if confined
            else "failed" if red.exit_code == EXIT_TESTS_FAILED
            else "passed" if red.exit_code == EXIT_ALL_PASSED
            else "unknown"
        )

        # `--ignore` on THIS run only. Without it the erroring module aborts
        # collection of the whole rootdir sweep before `--deselect` is applied
        # (measured: exit 2 on the p2p-before argv verbatim), so the baseline
        # this acceptance depends on cannot be observed at all. Safe here
        # because preflight's p2p-BEFORE is not an argv the grader makes: the
        # graded run is at the post-submission state, matched by p2p-AFTER,
        # which gets no ignore -- and the one thing the ignore hides, a non-f2p
        # test inside the ignored module, is measured by that same p2p-after
        # run once the reference lands.
        ignore = tuple(sorted(collected)) if confined else ()
        evidence["p2p_before_ignored"] = list(ignore)
        green = runner.pass_to_pass(tests, ignore=ignore)
        evidence["p2p_before_exit"] = green.exit_code
        p2p_green = green.exit_code == EXIT_ALL_PASSED

        if red.exit_code == EXIT_ALL_PASSED:
            problems.append(
                "the f2p tests PASS at the start state: the task is already "
                "done, and every arm would be scored on work it did not do"
            )
        elif confined and p2p_green:
            pass  # accepted: the bug is a collection error, and it is confined
        elif confined:
            problems.append(
                "the f2p tests could not be collected at the start state "
                f"({', '.join(sorted(collected))}), which is an accepted task "
                "shape ONLY while the rest of the suite is green there -- and "
                "the p2p run exited "
                f"{green.exit_code} ({_explain(green.exit_code)}). The f2p "
                "selection imports only the f2p modules, so a broken image "
                "produces exactly this error set; p2p is what separates them. "
                "If the run below collected nothing, this task's test tree "
                "holds no regression baseline outside the erroring module. If "
                "it still reports that module, the --ignore missed: those "
                "paths come from pytest's ROOTDIR-relative ERROR lines and "
                "--ignore resolves against the working directory, and an "
                "--ignore naming a path that does not exist is accepted "
                "silently (measured).\n"
                f"  {' '.join(runner.last_argv)}\n"
                + (green.stdout or green.stderr)[-2000:]
            )
        elif red.exit_code != EXIT_TESTS_FAILED:
            # `collected` is `None` for two different reasons, and the message
            # must not say "empty" for both: the parse ran and found nothing
            # (exit code was a collection failure, but `collection_error_modules`
            # saw no bare-module ERROR lines) versus the parse never ran at all
            # (this exit code -- e.g. 5, EXIT_NOTHING_COLLECTED, or a timeout --
            # is outside EXIT_COLLECTION_FAILURES). "empty" for the second case
            # would read as "pytest reported nothing", when what actually
            # happened is that this branch never looked.
            reported_desc = (
                sorted(collected) if collected
                else "empty" if red.exit_code in EXIT_COLLECTION_FAILURES
                else f"not parsed (exit {red.exit_code} is not a collection failure)"
            )
            problems.append(
                f"the f2p tests did not run at the start state -- "
                f"{_explain(red.exit_code)}. This is the Phase 0c failure: a "
                "broken environment is also a non-zero exit, and an agent "
                "reading the output cannot tell it from the bug. A collection "
                "error IS accepted, but only when every reported ERROR names a "
                "declared f2p module and no other, and here the reported set "
                f"is {reported_desc} against "
                f"declared {sorted(f2p_modules(tests.f2p))}.\n"
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

        if not p2p_green:
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
            # NO `--index`, and its ABSENCE is load-bearing. `materialize`
            # writes this tree's index on the HOST; `git apply --index`
            # compares the index's CACHED STAT DATA rather than content
            # (`ce_match_stat`), and virtiofs reports `st_dev`, `st_ino`,
            # `st_uid` and `st_gid` differently inside the container -- so
            # `--index` refuses a patch that applies, on a clean tree, and
            # preflight would NO-GO every task on a Docker Desktop host.
            # Nothing here needs the patch staged, so nothing here needs the
            # refresh `grader._refresh_index` performs for the one call site
            # that does.
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

            # --- each declared grading argv runs clean on the reference
            #
            # A typo'd `typecheck:`, or a tool the image does not ship, is
            # otherwise stamped as typecheck_failed on every record of this
            # task, permanently, in an append-only store -- an accusation
            # against every arm for the task author's error.
            #
            # BEFORE the scoped p2p, because the grader's ladder runs
            # build/typecheck (checks 3-4) before p2p (check 6): a grading
            # command can write into the tree -- a build artifact, a mypy or
            # ruff cache -- and the scoped run has to be measured on the tree
            # the graded one will actually see, not on a cleaner one.
            for key, argv in _declared_grading(task):
                checked = container.exec(["timeout", str(timeout_s), *argv])
                evidence[f"grading_{key}_exit"] = checked.exit_code
                if checked.exit_code != EXIT_ALL_PASSED:
                    problems.append(
                        f"the declared grading.{key} command exits "
                        f"{checked.exit_code} at the post-fix state: "
                        f"{' '.join(argv)}. The reference is the oracle; a "
                        f"command it cannot satisfy would record "
                        f"{key}_failed against every submission.\n"
                        + (checked.stdout or checked.stderr)[-2000:]
                    )

            # --- the SCOPED p2p is green, which is what the grader runs
            #
            # Check 6 scopes p2p to tests.paths, because an agent's scratch
            # files at the rootdir get collected and counted otherwise. The
            # first draft of that design claimed rootdir-wide green "strictly
            # implies" scoped green; it does not, since scoping changes
            # fixture setup and ordering. So it is measured here instead.
            #
            # Deselect branch only: pass_to_pass ignores `scope` when an
            # explicit p2p list is declared, so running this there pays a full
            # suite invocation to re-assert the selection already validated
            # ten lines up.
            if not tests.p2p:
                scope = _existing_prefixes(container, tests.paths)
                evidence["scope_prefixes"] = list(scope)
                absent = [p for p in tests.paths if p not in scope]
                if absent:
                    # Recorded, never a problem: `ok` is `not problems`, and
                    # the grader's restore step tolerates exactly this input.
                    # A problem here re-arms the NO-GO the filter prevents.
                    evidence["scope_prefixes_absent"] = absent
                    problem_codes.append(SCOPE_PREFIX_MISSING)
                if not scope:
                    problem_codes.append(SCOPE_COLLECTS_NOTHING)
                    problems.append(
                        "none of the declared tests.paths "
                        f"({', '.join(tests.paths)}) exist after the reference "
                        "fix, so the grader's scoped p2p run would collect "
                        "nothing and every submission would be graded against "
                        "an empty regression check"
                    )
                else:
                    scoped = runner.pass_to_pass(tests, scope=scope)
                    evidence["p2p_scoped_after_exit"] = scoped.exit_code
                    if scoped.exit_code != EXIT_ALL_PASSED:
                        if scoped.exit_code == EXIT_NOTHING_COLLECTED:
                            problem_codes.append(SCOPE_COLLECTS_NOTHING)
                        problems.append(
                            "the p2p run the GRADER will make is not green "
                            f"after the reference fix -- "
                            f"{_explain(scoped.exit_code)}. Scoping to "
                            "tests.paths changes what is collected, so a green "
                            "rootdir run does not settle this one.\n"
                            f"  {' '.join(runner.last_argv)}\n"
                            + (scoped.stdout or scoped.stderr)[-2000:]
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
        preflight_version=PREFLIGHT_VERSION,
        problem_codes=tuple(problem_codes),
    )
