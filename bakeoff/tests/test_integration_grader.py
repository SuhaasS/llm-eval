"""The grader against the real task, in its pinned image, in real containers.

Everything else in the grader's test suite drives `run_ladder` through the
`env` seam with a fake. That is the right shape for the ladder's branch
semantics, and it is structurally incapable of catching the two defects this
file exists for, because both live BELOW the seam:

* **A scanner that saw nothing.** Measured 2026-08-17 on Docker Desktop for
  Mac: a scan dir under `/var/folders` is not shared with the Docker VM, so
  `-v` mounts a silently EMPTY directory and gitleaks reports `scanned ~0
  bytes / no leaks found / exit 0` against a live AKIA key. That is
  byte-identical to a genuine clean scan and is a permanent spec section 7
  pass on every record. So the secret case here PLANTS a well-formed key and
  requires `secret_found` -- a clean pass proves nothing, which is the whole
  reason `_scan_saw_input`'s post-condition exists and the whole reason a
  clean-only integration case would have been theatre.

* **The restore's `--index` + `git rm` pair.** An agent-ADDED test file is
  untracked unless `git apply --index` staged it, and `git rm` cannot remove
  an untracked path -- so the agent's own always-passing test survives the
  restore and grades itself. Nothing above the seam can witness that: it is a
  property of git's index, and the fake env has no index.

MARKERS. `pytestmark` carries BOTH `integration` and `task_image`, module-wide
rather than per test, so a case added later cannot be written without them.
The second marker is what keeps the section 6.6 logger gate offline:
`verify_logger.py` selects `-m "integration and not task_image"`, and CLAUDE.md
pins that gate as offline, no credentials, no spend. These tests build the base
image and the task image and, on a cold cache, clone the repo mirror.

    cd bakeoff && .venv/bin/python -m pytest -v -m "integration and task_image" \
        tests/test_integration_grader.py --basetemp="$HOME/.cache/bakeoff-pytest"

CACHE ROOT. `tmp_path` is NOT used for anything Docker has to see. On macOS it
resolves under `/var/folders`, which the Docker VM does not mount -- a repo
bind-mounted from there appears inside the container as a silently empty
directory (`tests/conftest.py` documents the same failure for the container
tests, where it made the snapshot tests compare nothing against nothing and
pass). `grade_run` bind-mounts `cache_root/grade-tree/<run_id>` and allocates
its gitleaks scan dirs under `cache_root/grade-scan`, so the cache root is an
explicit `$HOME` path here and does not depend on the caller remembering
`--basetemp`. Artifact roots stay on `tmp_path`: nothing reads them but this
process.

The cache root is created and its VOLATILE subdirectories are cleaned, but the
repo mirror under `repos/` is deliberately kept between sessions. Wiping it
would make every invocation re-clone pallets/click, and a gate that is slow is
a gate that stops being run.
"""

from __future__ import annotations

import dataclasses
import gzip
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff import oracle as oracle_module
from bakeoff.grade_schema import GradeFailure, NotGradedReason
from bakeoff.grader import GRADER_VERSION, grade_run
from bakeoff.images import build_base_images, build_task_image, image_entrypoint
from bakeoff.oracle import ensure_oracle
from bakeoff.preflight import EXIT_TESTS_FAILED
from bakeoff.schema import (
    Artifacts,
    Checkpoint,
    Outcome,
    RunRecord,
    TerminationReason,
    Versions,
)
from bakeoff.tasks import load_task, materialize

# Both, module-wide. See the module docstring: `task_image` is what keeps the
# section 6.6 logger gate offline, and a per-test decorator is a thing the next
# case can be written without.
pytestmark = [pytest.mark.integration, pytest.mark.task_image]

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_DIR = REPO_ROOT / "taskset" / "click-3360-write-usage-empty-args"

#: A syntactically valid AWS access key id -- `AKIA` plus sixteen uppercase
#: alphanumerics, which is exactly what gitleaks' `aws-access-token` rule
#: matches -- that has never named an account. Split so this file does not
#: itself trip a secret scanner over a constant whose whole purpose is to be
#: found. NOT AWS's documented `AKIAIOSFODNN7EXAMPLE`: a scanner is entitled to
#: allowlist a string with `EXAMPLE` in it, and a planted secret that a future
#: gitleaks quietly ignores turns this test back into the clean pass it exists
#: to replace. Verified against the pinned digest on 2026-08-17: exit 42,
#: `aws-access-token`.
PLANTED_SECRET = "AKIA" + "Z3PXQ7WK2LMNVBTY"

#: The measured gemma shape: a test the agent adds under the declared test
#: paths that passes unconditionally. Unmarked, so click's own
#: `addopts = "-m 'not stress'"` does not deselect it -- a freebie the suite
#: refuses to collect would make this case pass for the wrong reason.
FREEBIE = "def test_freebie():\n    assert True\n"
FREEBIE_PATH = "tests/test_freebie.py"

#: The f2p file the weakened-test case deletes. It is where all seven declared
#: f2p ids live, so deleting it is the maximal version of the attack: with no
#: restore, `pytest` cannot even collect the ids and would exit 4, which the
#: ladder reports as an ENVIRONMENT error rather than as a model failure.
F2P_FILE = "tests/test_formatting.py"

_PASSED = re.compile(r"(\d+) passed")


# ---------------------------------------------------------------------------
# fixtures: one image and one oracle for the whole session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def grader_cache() -> Path:
    """The Docker-visible cache root. See the module docstring."""
    root = Path.home() / ".cache" / "bakeoff-pytest-grader"
    root.mkdir(parents=True, exist_ok=True)
    # The volatile halves only. `repos/` (the pruned mirror) and `build/` (the
    # image build contexts) are what make a warm re-run cheap, and both are
    # keyed by content -- `repos/` by (repo, base_sha), `build/` rebuilt from
    # `git archive base_sha` on every call.
    for volatile in ("grade-tree", "grade-scan", "oracle-tree", "scratch"):
        shutil.rmtree(root / volatile, ignore_errors=True)
    return root


@pytest.fixture(scope="session")
def click_task():
    return load_task(TASK_DIR)


@pytest.fixture(scope="session")
def click_image(click_task, grader_cache) -> str:
    """The task's own pinned image, built exactly as `run_matrix` builds it.

    The ENTRYPOINT assertion is `run_matrix`'s and `grade.py`'s, repeated here
    because it is the difference between this file failing on its first exec
    with a confusing message and failing with the one an operator can act on:
    an inherited ENTRYPOINT makes `RunContainer`'s `sleep infinity` an argument
    to it and the container exits immediately.
    """
    # Keyed by version exactly as the drivers do it: the base a task gets is
    # the one its manifest names, never a default that happens to match.
    base = build_base_images(REPO_ROOT, [click_task.image.python])[
        click_task.image.python
    ]
    image = build_task_image(click_task, base, grader_cache / "build",
                             grader_cache)
    assert image.startswith("sha256:"), (
        f"{image!r} is not a content pin; the oracle refuses a tag and so does "
        "RunContainer"
    )
    assert not image_entrypoint(image), (
        "the task image declares an ENTRYPOINT; every exec in this file would "
        "fail against a container that exited immediately"
    )
    return image


@pytest.fixture(scope="session")
def derived_oracle(click_task, click_image, grader_cache):
    """A REAL derivation, forced, with `_derive` counted.

    The stored verdict is removed first. Without that, a warm cache from an
    earlier session makes the first call a hit and `test_oracle_derives...`
    asserts a cached value while claiming to have derived one -- the cache
    hiding the derivation is the exact shape this fixture has to rule out.

    Counting is a plain setattr rather than `monkeypatch`, which is
    function-scoped and cannot be used from a session fixture.
    """
    path = grader_cache / "oracle" / f"{click_task.task_id}.json"
    path.unlink(missing_ok=True)

    calls: list[str] = []
    real = oracle_module._derive

    def counted(*args, **kwargs):
        calls.append("derive")
        return real(*args, **kwargs)

    oracle_module._derive = counted
    try:
        oracle = ensure_oracle(click_task, click_image, grader_cache)
    finally:
        oracle_module._derive = real
    return SimpleNamespace(oracle=oracle, derive_calls=len(calls), path=path)


@pytest.fixture(scope="session")
def reference_grade(click_task, click_image, derived_oracle, grader_cache,
                    tmp_path_factory):
    """The reference solution, graded once and read by two tests.

    `task.solution_diff` VERBATIM. The test half is already committed at
    `start_sha` (`materialize` commits it), so the stitched
    test-half-plus-solution-half concatenation does NOT apply there -- measured
    in review, and it is the mistake that makes a reference case fail as
    `apply_failed` and read as a grader defect.

    Session-scoped because the freebie case compares its own p2p arithmetic
    against this one's: the witness for "the added test did not survive the
    restore" is that the two runs collected the same number of items.
    """
    record = _record("ref-solution", click_task.solution_diff, click_image)
    return grade_run(record, click_task, click_image, derived_oracle.oracle,
                     grader_cache, tmp_path_factory.mktemp("artifacts-ref"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _record(run_id: str, diff: str, image: str) -> RunRecord:
    """A well-formed record of a run that submitted `diff`.

    `versions.container_image_digest` is the image the grade will run in, so
    `image_matches_run` is a `True` a test can assert rather than the `None`
    that "we did not check" produces.
    """
    return RunRecord(
        run_id=run_id,
        task_id="click-3360-write-usage-empty-args",
        task_version=1,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-17T00:00:00Z",
        finished_at="2026-08-17T00:05:00Z",
        # The harness never grades, so a real record of a successful run says
        # FAILED here. Anything the grader concluded from this field would be
        # concluding it from a placeholder.
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=4,
        turns_streamed=4,
        collection_id="integration",
        checkpoints=[
            Checkpoint(turn=4, diff_vs_base=diff, files_touched=[],
                       elapsed_ms=0)
        ],
        artifacts=Artifacts(final_diff=diff),
        versions=Versions(container_image_digest=image),
    )


def _git(tree: Path, *args: str, stdin: str | None = None):
    return subprocess.run(
        ["git", *args], cwd=tree, input=stdin, text=True,
        capture_output=True, check=True,
    )


def _submission(task, cache_root: Path, name: str, mutate) -> str:
    """A start_sha-relative diff, produced the way the harness produces one.

    `git add -A` then `git diff --cached <start_sha>` is `RunContainer.
    snapshot_diff`'s own pair, and the staging is what makes an ADDED file
    (the freebie) and a DELETED one (the weakened test) both appear. A
    hand-written diff would have to get git's rename detection, index lines
    and hunk arithmetic right for a 600-line deletion; this cannot get them
    wrong.

    The scratch tree is removed on the way out, including on the failure
    paths: it holds the mutation applied on top of the start state, which is a
    trap for anyone inspecting the cache by hand -- and for the secret case it
    holds a planted key.
    """
    scratch = Path(cache_root) / "scratch" / name
    shutil.rmtree(scratch, ignore_errors=True)
    tree = scratch / "repo"
    try:
        start_sha = materialize(task, tree, Path(cache_root))
        mutate(tree)
        _git(tree, "add", "-A")
        return _git(tree, "diff", "--cached", start_sha).stdout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _apply_solution(task, tree: Path) -> None:
    """The reference fix, so a submission can reach the rungs past f2p.

    Both the freebie case and the secret case are about checks 6 and 8, and
    the ladder stops at the FIRST failed rung -- a submission carrying only
    the attack fails f2p at rung 5 and never reaches the thing under test.
    """
    _git(tree, "apply", "-", stdin=task.solution_diff)


def _check(grade, name: str):
    found = [c for c in grade.checks if c.name == name]
    assert found, f"no {name} check in {[c.name for c in grade.checks]}"
    return found[0]


def _output(grade, name: str) -> str:
    """A check's captured stdout+stderr, off the artifacts directory.

    Asserted present rather than defaulted: `output_path is None` means
    nothing was kept, and a test that read an empty string from it would be
    asserting against the absence instead of against the run.
    """
    check = _check(grade, name)
    assert check.output_path, f"{name} kept no output to read"
    with gzip.open(check.output_path, "rt", encoding="utf-8") as handle:
        return handle.read()


def _passed(text: str) -> int:
    """How many items pytest reported passing, from the LAST match.

    `findall()[-1]`, not `search()`. The captured output is a whole suite's
    stdout, and click's own tests drive `CliRunner`, which echoes program
    output into it -- a fixture whose expected text contains `N passed` would
    be matched by `search` in preference to the summary line pytest prints at
    the end. The summary is always last, so the last match is the only one
    that is reliably the summary.
    """
    found = _PASSED.findall(text)
    assert found, f"no pytest summary line to count in:\n{text[-2000:]}"
    return int(found[-1])


# ---------------------------------------------------------------------------
# the oracle
# ---------------------------------------------------------------------------


def test_oracle_derives_an_empty_quarantine_for_click(
    click_task, click_image, grader_cache, derived_oracle, monkeypatch
):
    """Two identical reference runs, no disagreement, and then a cache.

    An empty quarantine is the EXPECTED answer for a healthy suite, and it is
    also what a derivation that never ran would produce -- so `derive_calls`
    is asserted beside it. `quarantined == ()` on its own is the value the
    `None`/`()` distinction in `GradeRecord` exists to keep apart.
    """
    oracle = derived_oracle.oracle
    assert derived_oracle.derive_calls == 1, (
        "the fixture removed the stored verdict, so this must have been a real "
        "derivation and not a warm cache from an earlier session"
    )
    assert oracle.quarantined == ()
    assert oracle.fingerprint
    assert derived_oracle.path.exists(), "the derivation wrote no cache entry"

    # The second call must answer from that entry. `_derive` is replaced with
    # something that cannot succeed, so a re-derivation is a failure rather
    # than a slow pass -- the timing difference alone is not an assertion.
    def refuse(*args, **kwargs):
        raise AssertionError("re-derived a quarantine that was already cached")

    monkeypatch.setattr(oracle_module, "_derive", refuse)
    again = ensure_oracle(click_task, click_image, grader_cache)
    assert again == oracle


# ---------------------------------------------------------------------------
# the ladder, end to end
# ---------------------------------------------------------------------------


def test_the_reference_solution_grades_resolved(reference_grade, click_image,
                                                derived_oracle):
    """The merged PR's own fix, graded the way a submission is graded.

    This is the calibration case: if it does not resolve, no verdict this
    grader produces about any model means anything.
    """
    grade = reference_grade
    assert grade.resolved is True, (
        f"{grade.grade_failure} / {grade.not_graded_reason}: "
        f"{grade.not_graded_detail or _check(grade, 'f2p').detail}"
    )
    assert grade.grade_failure is None
    assert grade.not_graded_reason is None

    # The reference diff touches `src/click/formatting.py` and nothing else --
    # `CHANGES.rst` is in `allow_extra_paths` and the test half is committed at
    # start_sha -- so this is the arm of the comparison that must read False.
    assert grade.agent_modified_tests is False
    assert grade.binary_chunks_dropped is None, (
        "the apply succeeded, so nothing was inspected for binary chunks"
    )

    assert grade.image_matches_run is True
    assert grade.graded_in_image == click_image
    assert grade.grader_version == GRADER_VERSION
    assert grade.oracle_fingerprint == derived_oracle.oracle.fingerprint
    assert grade.quarantined == ()

    for check in grade.checks:
        assert check.status in ("pass", "not_configured"), (
            f"{check.name}: {check.status} -- {check.detail}"
        )
    # The task waives all three `grading:` keys on purpose, and the waiver is
    # only readable if it lands as `not_configured` rather than as a pass.
    for waived in ("build", "typecheck", "lint"):
        assert _check(grade, waived).status == "not_configured"

    assert grade.f2p_declared == 7
    assert grade.f2p_failed_node_ids == ()
    assert grade.p2p_failed_node_ids == ()
    assert grade.p2p_quarantine_requested == 0
    assert grade.p2p_deselect_requested == 7
    # An OBSERVATION, and it is larger than what the grader requested: click's
    # own `addopts = "-m 'not stress'"` deselects 30,000 stress items on top of
    # the seven f2p ids. The invariant is one-directional -- a SHORTFALL is
    # staleness -- so the assertion is `>=`, not equality.
    assert grade.p2p_deselected is not None
    assert grade.p2p_deselected >= grade.p2p_deselect_requested


def test_an_empty_submission_grades_empty_patch(click_task, click_image,
                                                derived_oracle, grader_cache,
                                                tmp_path):
    """An honest verdict, not a refusal.

    `EMPTY_PATCH` is a `GradeFailure` because the run had turns and cost
    tokens and changed no file -- a fact about the model, which belongs in the
    denominator. The `NO_FINAL_DIFF` refusal above it is for a `None` diff, and
    the two must not collapse: an empty string is a submission.
    """
    record = _record("empty-submission", "", click_image)
    grade = grade_run(record, click_task, click_image,
                      derived_oracle.oracle, grader_cache, tmp_path)

    assert grade.resolved is False
    assert grade.grade_failure == GradeFailure.EMPTY_PATCH.value
    assert grade.not_graded_reason is None
    assert grade.not_graded_reason != NotGradedReason.NO_FINAL_DIFF.value
    assert _check(grade, "patch_non_empty").status == "fail"
    # The ladder stopped, so nothing downstream is a claim about this run.
    assert _check(grade, "f2p").status == "skipped"
    assert grade.agent_modified_tests is None
    assert grade.f2p_declared is None


def test_a_weakened_test_is_restored_and_fails_f2p(click_task, click_image,
                                                   derived_oracle,
                                                   grader_cache, tmp_path):
    """Delete the file holding every f2p id, submit nothing else.

    The verdict has to be `f2p_failed`, and the EXIT CODE is what makes that
    claim honest: with the restore working, pytest collects the seven restored
    ids and exits 1 (tests ran and failed). Without it, pytest cannot collect
    them and exits 4 -- also non-zero, and the ladder correctly calls that an
    ENVIRONMENT error, not a model failure. So an assertion on `resolved is
    False` alone would pass either way.
    """
    diff = _submission(
        click_task, grader_cache, "weakened",
        lambda tree: (tree / F2P_FILE).unlink(),
    )
    assert f"a/{F2P_FILE}" in diff, "the submission does not delete the f2p file"

    record = _record("weakened-test", diff, click_image)
    grade = grade_run(record, click_task, click_image,
                      derived_oracle.oracle, grader_cache, tmp_path)

    assert grade.resolved is False
    # `environment_error` rides in the MESSAGE, not in an assert of its own: a
    # `grade_failure` and an environment refusal are mutually exclusive by
    # construction (`_State.environment` sets `not_graded_reason` and leaves
    # `grade_failure` `None`), so a separate `is None` line below this one
    # could never fail and would read as a check that had been made.
    assert grade.grade_failure == GradeFailure.F2P_FAILED.value, (
        f"environment_error={grade.environment_error}, "
        f"not_graded_reason={grade.not_graded_reason}"
    )
    assert grade.agent_modified_tests is True

    restore = _check(grade, "test_restore")
    assert restore.status == "pass", restore.detail
    f2p = _check(grade, "f2p")
    assert f2p.status == "fail"
    assert f2p.exit_code == EXIT_TESTS_FAILED, (
        f"f2p exited {f2p.exit_code}: the declared ids were not collected, so "
        "the deleted file was never restored"
    )
    assert grade.f2p_declared == 7
    assert set(grade.f2p_failed_node_ids) == set(click_task.tests.f2p)


def test_an_added_freebie_test_does_not_survive_the_restore(
    click_task, click_image, derived_oracle, grader_cache, tmp_path,
    reference_grade
):
    """The reference fix plus an always-passing test the agent wrote.

    THE WITNESS IS THE COUNT, not the absence of a name. A freebie that
    survived the restore would pass silently under `-q`, so its node id never
    appears in the output either way; what moves is how many items pytest
    collected. Measured on this task: 1616 passed without the freebie, 1617
    with it. The reference grade supplies the baseline, so the assertion is
    not a hard-coded number that goes stale the next time the suite grows.

    The fix rides along because the ladder stops at the first failed rung: a
    submission carrying only the freebie fails f2p at rung 5 and never reaches
    check 6, which is the check this case is about.
    """
    def mutate(tree: Path) -> None:
        _apply_solution(click_task, tree)
        (tree / FREEBIE_PATH).write_text(FREEBIE)

    diff = _submission(click_task, grader_cache, "freebie", mutate)
    assert FREEBIE_PATH in diff

    record = _record("freebie-test", diff, click_image)
    grade = grade_run(record, click_task, click_image,
                      derived_oracle.oracle, grader_cache, tmp_path)

    # The fix is real, so the freebie is the only thing that could change the
    # verdict -- and it does not.
    assert grade.resolved is True, (
        f"{grade.grade_failure} / {grade.not_graded_reason}: "
        f"{grade.not_graded_detail}"
    )
    assert grade.agent_modified_tests is True, (
        "the submission adds a file under the declared test paths"
    )

    p2p_output = _output(grade, "p2p")
    assert "test_freebie" not in p2p_output
    assert _passed(p2p_output) == _passed(_output(reference_grade, "p2p")), (
        "the graded p2p run collected a different number of items than the "
        "reference submission did, so the agent's own test survived "
        "`git rm` + `git checkout` and graded itself"
    )


def test_a_planted_secret_is_found_by_a_scanner_that_saw_the_input(
    click_task, click_image, derived_oracle, grader_cache, tmp_path
):
    """The reference fix plus a well-formed AWS key id in an added line.

    A CLEAN PASS PROVES NOTHING. Measured 2026-08-17: gitleaks handed an empty
    bind mount reports `scanned ~0 bytes / no leaks found / exit 0`, which is
    byte-identical to a genuine clean scan -- so every case above, all of which
    scan clean, would go on passing with the scanner blind. This is the only
    case that fails if `scan_secrets` stops delivering its input, and it is
    end to end: the key goes in as a `+` line of a stored diff, comes out as a
    `secret_found` verdict.

    The secret is a COMMENT in the file the fix already touches. A new module
    under `src/click/` would be an unrelated import surface for a 1,600-test
    suite, and this case has to reach rung 8, which means rungs 5 and 6 have
    to be green.
    """
    def mutate(tree: Path) -> None:
        _apply_solution(click_task, tree)
        target = tree / "src" / "click" / "formatting.py"
        target.write_text(
            target.read_text()
            + f'\n# leftover debugging credential: {PLANTED_SECRET}\n'
        )

    diff = _submission(click_task, grader_cache, "planted-secret", mutate)
    assert PLANTED_SECRET in diff

    record = _record("planted-secret", diff, click_image)
    grade = grade_run(record, click_task, click_image,
                      derived_oracle.oracle, grader_cache, tmp_path)

    assert grade.resolved is False, (
        "gitleaks did not report the planted key; if `secret_scan` passed, the "
        "scanner was handed nothing and every section 7 pass in this suite is "
        f"manufactured. detail: {_check(grade, 'secret_scan').detail}"
    )
    # Same fold as the weakened case: a `grade_failure` and an environment
    # refusal cannot both be set, so `environment_error is None` on its own
    # line is an assert that cannot fail.
    assert grade.grade_failure == GradeFailure.SECRET_FOUND.value, (
        f"environment_error={grade.environment_error}, "
        f"not_graded_reason={grade.not_graded_reason}"
    )

    # It got there by passing the rungs below, which is what makes the verdict
    # a statement about the secret rather than about a broken submission.
    for green in ("test_restore", "f2p", "p2p"):
        assert _check(grade, green).status == "pass"

    scan = _check(grade, "secret_scan")
    assert scan.status == "fail"
    assert "aws-access-token" in scan.detail, scan.detail

    # The stored artifact is REDACTED. The report carries the secret VALUES
    # under `Secret`/`Match`, and gzipping it into the grade artifacts would
    # copy every key an arm leaked out of the ephemeral scan dir and into a
    # directory that outlives the run.
    assert PLANTED_SECRET not in _output(grade, "secret_scan")


def test_a_task_images_env_reaches_both_the_gates_exec_and_the_agents(
    click_task, click_image, grader_cache, tmp_path
):
    """The real-image half, and the one that cannot be faked.

    Everything offline proves is that we render the right Dockerfile line and
    that container_env does not name the key. What no unit test can reach is
    the merge itself -- that an image ENV survives into an exec that carries
    the agent's OWN environment. If it did not, every determinism lever would
    apply to preflight and the grader and not to the loop section 3.3
    measures, and the only symptom would be a suite that sometimes passes.

    Both execs, deliberately: `container.exec(argv)` is the shape preflight,
    the oracle and the grader all make, and `exec(argv, env=container_env(...))`
    is the shape `claude_runner.ContainerBackend` makes for the agent.
    """
    from bakeoff.claude_runner import ClaudeCodeConfig, container_env
    from bakeoff.container import RunContainer
    from bakeoff.images import build_task_image

    declared = {"CI": "1", "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/bakeoff-hyp"}
    task = dataclasses.replace(
        click_task,
        task_id=click_task.task_id + "-envprobe",
        image=dataclasses.replace(click_task.image, env=declared),
    )
    base = build_base_images(REPO_ROOT, [task.image.python])[task.image.python]
    image = build_task_image(task, base, grader_cache / "build",
                             grader_cache)

    agent_env = container_env(ClaudeCodeConfig(
        model="m", base_url="http://litellm:4000", auth_token="t",
        settings_path="/eval/settings.json", config_dir="/eval/claude-config",
        max_turns=1, wall_clock_timeout_s=1,
        custom_headers="X-Bakeoff-Run-Id: r-envprobe",
    ))

    with RunContainer(image=image, repo_path=str(tmp_path),
                      base_sha="") as container:
        for key, value in declared.items():
            gate = container.exec(["printenv", key])
            agent = container.exec(["printenv", key], env=agent_env)

            assert gate.exit_code == 0, key
            assert gate.stdout.strip() == value
            # The merge, which is the whole claim: a key the exec env does not
            # name survives from the image.
            assert agent.exit_code == 0, key
            assert agent.stdout.strip() == value

        # And the eval's own keys still win where they ARE named, so this is
        # not passing because the exec env was ignored wholesale.
        model = container.exec(["printenv", "ANTHROPIC_MODEL"], env=agent_env)
        assert model.stdout.strip() == "m"
