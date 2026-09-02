"""The node path, end to end, in a real container.

Every unit test above this replays a captured JSON report. This file is the
only place four things measured separately on a scratch probe meet each other:
`/node_modules` resolution under a bind mount that replaces `/repo`, the fixed
report path at `/tmp`, `--no-cache`, and the eval user at uid 1000 on a base
image that shipped its own. `smoke_task` exists for the same reason on the
pytest side, and for the same reason it is a fixture rather than a real repo:
what is under test is the harness, not the task.

MARKERS: both, module-wide, like `test_integration_grader.py`'s. `task_image`
is what keeps the section 6.6 logger gate offline -- `verify_logger.py` selects
`-m "integration and not task_image"`.

    cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" \
        tests/test_integration_node_task.py --basetemp="$HOME/.cache/bakeoff-pytest"

CACHE ROOT. An explicit `$HOME` path, never `tmp_path`. On macOS `tmp_path`
resolves under `/var/folders`, which the Docker VM does not mount, so a repo
bind-mounted from there appears inside the container as a **silently empty
directory** -- every assertion below would compare nothing against nothing and
pass. The probe this file was written from hit exactly that on its first run.

THE EMPTY MOUNT HAS A SECOND SHAPE and this file hit that one too, so both are
defended: a path the VM *does* share, deleted and rebuilt on the host between
containers, is served from a stale cache and appears empty roughly every other
time (measured -- see `node_tree`). Hence `container.fresh_tree` per run tree,
never a shared one, and `_mounted`'s post-condition beside it: under both node
frameworks an empty `/repo` exits **1** with `No test files found`, which is
the same exit a failing test gives, so an empty mount does not make this file
flaky -- it makes it pass while measuring nothing.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path

import pytest

from bakeoff.container import RunContainer, fresh_tree
from bakeoff.grade_schema import GradeFailure
from bakeoff.images import build_base_image, build_task_image, image_entrypoint
from bakeoff.oracle import ensure_oracle
from bakeoff.preflight import preflight
from bakeoff.schema import (
    Artifacts,
    Checkpoint,
    Outcome,
    RunRecord,
    TerminationReason,
    Versions,
)
from bakeoff.tasks import load_task, materialize

pytestmark = [pytest.mark.integration, pytest.mark.task_image]

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "fixtures" / "node_task"
#: An explicit $HOME path, not tmp_path. See the module docstring.
CACHE_ROOT = Path.home() / ".cache" / "bakeoff-node-integration"

#: The one node id this fixture declares, spelled the way the reporter emits
#: it: the file, `::`, then `fullName` -- the enclosing describe titles and the
#: test title joined by single spaces. This fixture has no describe block, so
#: fullName is the title.
F2P_ID = "tests/calc.test.js::adds two numbers"

#: The p2p baseline. Without one a green p2p run is vacuous and this fixture
#: would not exercise the grader's check 6 at all.
P2P_ID = "tests/calc.test.js::keeps subtracting elsewhere"

#: The reference diff, in the two halves `tasks.py` splits it into by path:
#: the TEST half adds the f2p test (so the start state is base_sha plus this,
#: exactly as a real bug-fix PR carries the test that proves the fix), and the
#: SOLUTION half flips the operator. The swap preserves byte count on purpose
#: -- see the fixture's README.
_REFERENCE = """diff --git a/tests/calc.test.js b/tests/calc.test.js
--- a/tests/calc.test.js
+++ b/tests/calc.test.js
@@ -1,2 +1,4 @@
 import { it, expect } from 'vitest';
+import { add } from '../src/calc.js';
+it('adds two numbers', () => { expect(add(2, 3)).toBe(5); });
 it('keeps subtracting elsewhere', () => { expect(3 - 1).toBe(2); });
diff --git a/src/calc.js b/src/calc.js
--- a/src/calc.js
+++ b/src/calc.js
@@ -1 +1 @@
-export function add(a, b) { return a - b; }
+export function add(a, b) { return a + b; }
"""

#: `--no-cache` is not decoration and not staleness insurance: measured, a
#: `vitest run` without it creates `<cwd>/node_modules/.vite` inside the
#: bind-mounted tree, and section 5.6 stages everything, so the artifact lands
#: in every submission diff. `test_the_suite_leaves_no_node_modules_in_the_tree`
#: below runs both spellings and asserts the difference.
_RUNNER = '["/node_modules/.bin/vitest", "run", "--no-cache"]'


def _manifest(upstream: Path, base: str) -> str:
    """The task.yaml text.

    NO `start_sha`. A real task pins it, so that a re-cut patch or a different
    git silently moving the start state is a load error; a fixture whose
    upstream is regenerated per module run cannot, because the value does not
    exist until this module has made the commit.

    NO `image.build` either, and that is a claim rather than an omission: this
    fixture declares no dependencies, and vitest is resolved from
    `/node_modules` by the base image, which node's resolver reaches by walking
    up from `/repo/tests/`. A task with real dependencies adds
    `build: ["npm install --prefix / --omit=dev"]`.

    `image.node`, never `image.python` -- `load_task` refuses the latter
    beside a node framework, because two keys that each imply a runtime is two
    sources for one fact.
    """
    return (
        "task_id: node-smoke\n"
        "task_version: 1\n"
        "repo:\n"
        f"  url: {upstream}\n"
        f"  base_sha: {base}\n"
        "prompt: |\n"
        "  fix add()\n"
        "tests:\n"
        "  framework: vitest\n"
        '  paths: ["tests/"]\n'
        f"  runner: {_RUNNER}\n"
        f'  f2p: ["{F2P_ID}"]\n'
        "image:\n"
        '  node: "22"\n'
    )


@pytest.fixture(scope="module")
def task_dir(tmp_path_factory):
    """The fixture tree, git-initialised, plus a task.yaml naming its HEAD.

    The same shape as `test_preflight._smoke_task`, and for the same reason a
    checked-in `task.yaml` cannot replace it: `repo.base_sha` does not exist
    until the commit does. Module-scoped, so the git init and the two image
    builds below are paid once.

    `calc.js` is written here with the BUG (`a - b`), and `tests/calc.test.js`
    is written with only the p2p test -- the f2p test arrives with the test
    half, exactly as it does for a real task, because the start state is
    base_sha plus that half.
    """
    root = tmp_path_factory.mktemp("node-task")
    upstream = root / "upstream"
    shutil.copytree(FIXTURE, upstream)
    (upstream / "src" / "calc.js").write_text(
        "export function add(a, b) { return a - b; }\n")
    (upstream / "tests" / "calc.test.js").write_text(
        "import { it, expect } from 'vitest';\n"
        "it('keeps subtracting elsewhere', () => { expect(3 - 1).toBe(2); });\n")
    for args in (["init", "-q"], ["config", "user.email", "t@t.test"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=upstream, check=True,
                       capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream,
                          check=True, capture_output=True,
                          text=True).stdout.strip()

    task_dir = root / "task"
    task_dir.mkdir()
    (task_dir / "reference.diff").write_text(_REFERENCE)
    (task_dir / "task.yaml").write_text(_manifest(upstream, base))
    return task_dir


@pytest.fixture(scope="module")
def node_image(task_dir):
    """The node base, then this task's image on top of it.

    The ENTRYPOINT assertion is `run_matrix`'s and `grade.py`'s, repeated here
    because it is the difference between this file failing on its first exec
    with a confusing message and failing with the one an operator can act on:
    an inherited ENTRYPOINT makes `RunContainer`'s `sleep infinity` an argument
    to it and the container exits immediately.
    """
    base = build_base_image(REPO_ROOT, "node", "22")
    image = build_task_image(load_task(task_dir), base,
                             CACHE_ROOT / "build", CACHE_ROOT)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; RunContainer refuses a tag")
    assert not image_entrypoint(image)
    return image


@pytest.fixture
def node_tree(task_dir):
    """A fresh run tree at the start state, at a path NO OTHER TEST EVER USES.

    The unique component is the whole point and it was not the first draft.
    Draft one was `CACHE_ROOT / "tree"`, rmtree'd and re-materialized per test
    -- which is what `materialize`'s refusal of an existing destination
    invites, and it is WRONG on macOS. Measured 2026-09-02, four cycles of
    rmtree -> materialize -> new container on the same host path:

        cycle 0  ls /repo -> ['README.md', 'package.json', 'src', 'tests']
        cycle 1  ls /repo -> []
        cycle 2  ls /repo -> ['README.md', 'package.json', 'src', 'tests']
        cycle 3  ls /repo -> []

    Every other container saw an EMPTY /repo. That is the `$HOME` mount trap
    in its second shape: the first is a path the Docker VM does not share
    (`/var/folders`), and this one is a path it shares and has cached, whose
    inodes the host replaced underneath it. Both hand the container a silently
    empty directory, and here vitest answers that with `No test files found`
    and **exit 1** -- byte-identical to a failing test, which is the exact
    ambiguity `bakeoff.runners` exists because of. So a shared path does not
    make this file flaky; it makes it PASS while measuring nothing.

    This measurement is the evidence the production fix rests on. A uuid per
    tree removes the cause, and since round 2 item 3 (2026-09-03) that
    allocator is `container.fresh_tree`, used by every host path the harness
    bind-mounts -- so this fixture is one of its callers rather than a
    parallel implementation of it. `_mounted` below is the post-condition that
    would catch the cause coming back.
    """
    tree = fresh_tree(CACHE_ROOT / "tree")
    task = load_task(task_dir)
    start_sha = materialize(task, tree / "repo", CACHE_ROOT)
    yield task, tree / "repo", start_sha
    shutil.rmtree(tree, ignore_errors=True)


@contextlib.contextmanager
def _mounted(image: str, repo: Path, start_sha: str):
    """A `RunContainer` whose bind mount is PROVEN to have landed.

    `container._checked_exec` exists because a failed `git diff` returns empty
    output byte-identical to a clean tree; this is the same rule one layer up.
    An empty `/repo` is byte-identical to a red suite under both node
    frameworks -- measured, `No test files found` exits 1, and so does a
    failing assertion -- so every assertion in this file about what a suite
    did is worthless without this one first. It is a plain `ls`, on purpose:
    the file the tests read is the thing to look for, and a check that went
    through git would answer for the mount and for git at once.

    Not redundant with `RunContainer._assert_repo_mounted`, which landed
    later: that guard refuses a mount holding nothing at all, and this one
    asserts the expected CONTENT is there -- a strictly stronger claim, and
    the one every assertion in this file depends on.
    """
    with RunContainer(image=image, repo_path=str(repo),
                      base_sha=start_sha) as container:
        listing = container.exec(["ls", "-A", "/repo"]).stdout.split()
        assert "tests" in listing, (
            f"/repo is {listing!r} inside the container: the bind mount of "
            f"{repo} did not land. Nothing below this measures anything -- an "
            "empty tree exits 1 with `No test files found`, which is the same "
            "exit a failing test gives."
        )
        yield container


def test_the_start_state_declares_the_f2p_test_and_the_bug(node_tree):
    """`start_sha` is base_sha PLUS the committed test half, so the f2p test
    exists in the tree the agent gets and the fix does not. Asserted before
    anything is run, because every claim below is about that state."""
    task, repo, _ = node_tree

    assert F2P_ID in task.tests.f2p
    assert "adds two numbers" in (repo / "tests" / "calc.test.js").read_text()
    assert "a - b" in (repo / "src" / "calc.js").read_text()


def test_the_node_fixture_gates_green(node_tree, node_image):
    """The whole claim preflight makes, on the one task shape that has never
    been run end to end: red before the reference fix, green after it, p2p
    green on both sides, a clean tree, and none of the three node refusals
    firing on a task that satisfies them.

    The interpreter read-back is asserted as a pair of `None`s rather than
    skipped silently. The node base ships no `python` at all, so before the
    gate learned to ask `task_runtime` first this check probed for an
    interpreter a node manifest is FORBIDDEN from declaring, got a non-zero
    exit, and refused every node task in the set. `None` is "the gate did not
    look", which is a different absence from the `""` that a probe which ran
    and answered nothing files.
    """
    task, repo, start_sha = node_tree

    result = preflight(task, image=node_image, repo_path=repo,
                       start_sha=start_sha)

    assert result.ok, result.problems
    assert result.evidence["framework"] == "vitest"
    assert result.evidence["f2p_red_kind"] == "failed"
    assert result.evidence["f2p_before_not_run"] == []
    # `[]`, not `None`, and the pair is the assertion. Both keys are `None`
    # when the run that would fill them wrote no report -- what a broken
    # config gives on both node frameworks -- so a real green gate is the one
    # place the `[]` side of that distinction can be measured end to end.
    assert result.evidence["duplicate_full_names"] == []
    assert result.evidence["scope_files_outside"] == []
    assert result.evidence["runner_cache_flags_missing"] == []
    assert result.evidence["dirty_after_tests"] == ""
    assert result.evidence["python_declared"] is None
    assert result.evidence["python_observed"] is None


def test_the_suite_leaves_no_node_modules_in_the_tree(node_tree, node_image):
    """Measured on the probe: WITHOUT `--no-cache`, vitest creates
    `<cwd>/node_modules/.vite` and `git status` reports `?? node_modules/`.

    The assertion is over the FILESYSTEM rather than over `git status`, and
    that is the point: this fixture's `.gitignore` carries `node_modules/`, as
    every JavaScript repository's does, so the dirty-tree check preflight
    already makes is blind to exactly this artifact.
    """
    task, repo, start_sha = node_tree

    with _mounted(node_image, repo, start_sha) as container:
        container.exec(["timeout", "600", *task.tests.runner, "tests/"])

        assert container.exec(["test", "-e", "/repo/node_modules"]).exit_code != 0
        assert container.exec(
            ["git", "status", "--porcelain"]).stdout.strip() == ""

        # The NEGATIVE half, which is what makes the assertion above mean
        # something: without `--no-cache` the directory appears. A test that
        # only ever saw the flag's presence would stay green if the flag
        # stopped doing anything.
        stripped = [a for a in task.tests.runner if a != "--no-cache"]
        container.exec(["timeout", "600", *stripped, "tests/"])

        assert container.exec(["test", "-e", "/repo/node_modules"]).exit_code == 0
        container.exec(["rm", "-rf", "/repo/node_modules"])


def test_a_fix_at_the_same_byte_count_in_the_same_second_is_seen(
    node_tree, node_image
):
    """There is no `.pyc` analogue here, and this is where that stops being a
    probe result.

    `src/calc.js`'s bug is an operator swap, so the fix preserves the file's
    byte count, and nothing between the two runs clears a cache. That is the
    half this test controls, and it is the half CPython's invalidation key --
    (source mtime in whole seconds, source size) -- makes cheap to defeat: it
    is what served stale bytecode in this repo's own eval image on 2026-08-13,
    feeding section 3.3's self-correction loop the OLD behaviour after a
    correct fix. The seconds half is NOT controlled here and is not claimed:
    a run that crossed a second boundary would pass this test too. The
    measurement that settles the node side is the negative one recorded in
    `fixtures/node_task/README.md` -- vite's `.vite` is a dependency-optimiser
    cache, not a source-transform one, and jest's is content-hash keyed -- and
    this test is what would notice if either stopped being true.
    """
    task, repo, start_sha = node_tree
    runner = ["timeout", "600", *task.tests.runner, "tests/calc.test.js",
              "-t", "^(?:adds two numbers)$"]

    with _mounted(node_image, repo, start_sha) as container:
        assert container.exec(runner).exit_code == 1

        fixed = "export function add(a, b) { return a + b; }\n"
        (repo / "src" / "calc.js").write_text(fixed)

        assert container.exec(runner).exit_code == 0


def test_the_report_lands_outside_the_tree_and_a_stale_one_cannot_be_read(
    node_tree, node_image
):
    """The report path is FIXED because it is an argv element and the gated
    argv must equal the graded argv. What makes a fixed name safe is the `rm
    -f` `_Runner.run` issues before the measured command -- measured, a config
    error writes no file at all, so a leftover report would stand in as the
    next run's evidence, and a passing one would stand in for a run that never
    happened.
    """
    from bakeoff.preflight import _Runner
    from bakeoff.runners import KIND_ENVIRONMENT, for_framework

    task, repo, start_sha = node_tree
    adapter = for_framework("vitest")
    path = adapter.report_path()

    with _mounted(node_image, repo, start_sha) as container:
        runner = _Runner(container, task.tests.runner, 600, adapter)
        runner.select((F2P_ID,))

        # The report is in the container's /tmp, which is never bind-mounted,
        # so it cannot reach a submission diff.
        assert container.exec(["test", "-e", path]).exit_code == 0
        assert (repo / Path(path).name).exists() is False
        assert container.exec(
            ["git", "status", "--porcelain"]).stdout.strip() == ""

        # A run whose command cannot start must not inherit that report.
        result = runner.run(["--config", "/tmp/does-not-exist.mjs"])
        assert runner.last_report is None
        assert runner.classify(result).kind == KIND_ENVIRONMENT


def test_the_runners_resolve_on_PATH_for_the_commands_the_agent_invents(
    node_tree, node_image
):
    """Section 3.3 measures a loop run with commands THE AGENT INVENTS, and
    `npm install --prefix /` changes no environment -- measured, `command -v
    vitest` finds nothing on the stock base, so a bare `vitest` is exit 127
    while `npx vitest` works. Leaving the spellings inconsistent makes the
    agent discover the rule by failing, inside the turn budget it is scored
    on.

    So both shims resolve ONLY because `eval-agent-node.Dockerfile` puts
    `/node_modules/.bin` on PATH: the install itself exported nothing. That is
    the claim, and it is the reason this test asserts the PATH entry beside the
    lookups rather than the lookups alone -- a `command -v` that succeeded
    because some later step happened to add a symlink somewhere else would
    satisfy the pair on its own.
    """
    task, repo, start_sha = node_tree

    with _mounted(node_image, repo, start_sha) as container:
        assert container.exec(["sh", "-c", "command -v vitest"]).exit_code == 0
        assert container.exec(["sh", "-c", "command -v jest"]).exit_code == 0

        path = container.exec(["sh", "-c", "printf %s \"$PATH\""]).stdout
        assert "/node_modules/.bin" in path.split(":")
        assert container.exec(
            ["sh", "-c", "command -v vitest"]).stdout.strip() == (
            "/node_modules/.bin/vitest")


# ---------------------------------------------------------------------------
# the grader, over the same image
# ---------------------------------------------------------------------------


def _record_with_diff(task, diff: str) -> RunRecord:
    """A well-formed record of a run that submitted `diff`.

    Copied from `test_integration_grader._record` rather than imported: that
    one hardcodes click's task id, and a shared builder that took the task as
    a parameter would have to be edited in the file whose whole subject is the
    pytest path. `versions.container_image_digest` is left empty here on
    purpose -- `image_matches_run` then records the `None` that "we did not
    check" produces, which is the honest answer for a record this file
    fabricated.
    """
    return RunRecord(
        run_id=f"node-{'empty' if not diff else 'reference'}",
        task_id=task.task_id,
        task_version=task.task_version,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-09-02T00:00:00Z",
        finished_at="2026-09-02T00:05:00Z",
        # The harness never grades, so a real record of a successful run says
        # FAILED here. Anything the grader concluded from this field would be
        # concluding it from a placeholder.
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=4,
        turns_streamed=4,
        collection_id="integration-node",
        checkpoints=[
            Checkpoint(turn=4, diff_vs_base=diff, files_touched=[],
                       elapsed_ms=0)
        ],
        artifacts=Artifacts(final_diff=diff),
        versions=Versions(),
    )


@pytest.fixture(scope="module")
def node_oracle(task_dir, node_image):
    """The quarantine, derived in this task's own image.

    Two full suite runs, module-scoped so the two grading cases below pay for
    them once. It is derived rather than passed as `None` because a `None`
    oracle is the not-graded path, and what these cases are about is the
    ladder actually running.
    """
    return ensure_oracle(load_task(task_dir), node_image, CACHE_ROOT)


def test_the_grader_resolves_the_reference_fix(task_dir, node_image,
                                               node_oracle, tmp_path_factory):
    """Checks 5 and 6 over a real node submission. The reference diff IS the
    oracle, so if it does not grade `resolved: True` no submission can.

    `task.solution_diff` VERBATIM: the test half is already committed at
    `start_sha`, so a stitched test-half-plus-solution-half concatenation
    would not apply and would read as a grader defect.
    """
    from bakeoff.grader import grade_run

    # `task_dir`, not `node_tree`: the grader materializes its own tree from
    # the manifest, so taking the fixture that builds one costs a `materialize`
    # per test and pins nothing.
    task = load_task(task_dir)
    record = _record_with_diff(task, task.solution_diff)

    grade = grade_run(record, task, node_image, node_oracle, CACHE_ROOT,
                      tmp_path_factory.mktemp("artifacts-node-ref"))

    assert grade.resolved is True, grade.not_graded_detail or grade.grade_failure
    assert grade.framework == "vitest"
    assert grade.f2p_failed_node_ids == ()


def test_an_empty_submission_is_EMPTY_PATCH_and_never_reaches_the_suite(
    task_dir, node_image, node_oracle, tmp_path_factory
):
    """`EMPTY_PATCH` is a GradeFailure rather than a NotGradedReason: the run
    produced turns, cost tokens and changed no file, which is a fact about the
    model and belongs in the denominator."""
    from bakeoff.grader import grade_run

    task = load_task(task_dir)
    record = _record_with_diff(task, "")

    grade = grade_run(record, task, node_image, node_oracle, CACHE_ROOT,
                      tmp_path_factory.mktemp("artifacts-node-empty"))

    assert grade.resolved is False
    assert grade.grade_failure == GradeFailure.EMPTY_PATCH.value
