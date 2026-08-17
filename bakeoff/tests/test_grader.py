"""The nine-check ladder (spec section 6.1, the offline grader design).

Nothing here touches Docker. `run_ladder` is pure over the `env` protocol --
`exec`, `write_patch`, `remove_patch`, `scan_secrets` -- and `FakeEnv` records
every argv it is handed, so the tests assert on the COMMANDS the ladder issues
rather than on their effects. That is the only way to pin "the submission is
applied where it was diffed": the effect of applying at `base_sha` and at
`start_sha` differs by whether the test half is present, which a fake tree
cannot show and a real one would take a container to show.

`_chunk_path` is NOT faked. It shells out to real git in a temp directory
outside any repository, which is exactly what the production path does, so the
diffs in this file are genuine git diffs and a change that broke the parse
would fail here rather than in the image.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff.grade_schema import CHECK_ORDER, GradeFailure, NotGradedReason
from bakeoff.grader import (
    GRADER_VERSION,
    build_grade_record,
    grade_run,
    parse_deselected,
    run_ladder,
)
from bakeoff.oracle import Oracle
from bakeoff.schema import (
    Artifacts,
    Checkpoint,
    DestructiveCategory,
    DestructiveEvent,
    Exclusion,
    ExclusionClass,
    Outcome,
    RunRecord,
    Severity,
    TerminationReason,
    Versions,
)

# --------------------------------------------------------------------------
# diffs -- real ones, because _chunk_path runs real git over them
# --------------------------------------------------------------------------

TEXT_DIFF = """diff --git a/src/calc.py b/src/calc.py
index 1111111..2222222 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

TEST_TOUCHING_DIFF = """diff --git a/tests/test_calc.py b/tests/test_calc.py
index 1111111..2222222 100644
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -1,2 +1,2 @@
 def test_add():
-    assert add(1, 2) == 3
+    assert True
"""

BINARY_DIFF = """diff --git a/img.png b/img.png
index 1111111..2222222 100644
Binary files a/img.png and b/img.png differ
"""

MIXED_DIFF = TEXT_DIFF + BINARY_DIFF

# U+FFFD in a content line. An agent writing a decode test produces exactly
# this, and it applies cleanly -- which is why the substring is not a
# pre-check.
LOSSY_DIFF = """diff --git a/src/decode.py b/src/decode.py
index 1111111..2222222 100644
--- a/src/decode.py
+++ b/src/decode.py
@@ -1,2 +1,2 @@
 def decode(raw):
-    return raw.decode("utf-8")
+    return raw.decode("utf-8", "replace")  # �
"""

# Cut with `diff.noprefix` (a git config an image can carry). `diff_chunks`
# refuses it and `git apply --index` refuses it, from the one cause.
NOPREFIX_DIFF = """diff --git src/calc.py src/calc.py
index 1111111..2222222 100644
--- src/calc.py
+++ src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

START_SHA = "a" * 40
BASE_SHA = "b" * 40


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def is_f2p(argv):
    """The f2p invocation: pytest, with node ids selected and nothing
    deselected."""
    return any("pytest" in part for part in argv) and "--deselect" not in argv


def is_p2p(argv):
    return any("pytest" in part for part in argv) and "--deselect" in argv


class FakeEnv:
    """Records every argv; answers from ordered rules.

    A rule is `(matcher, result)`. `matcher` is either a list of substrings
    (all must appear in the joined argv) or a callable over the argv list.
    `result` is one `(exit_code, stdout, stderr)` triple, or a LIST of them
    served in order for successive matches -- which is what lets a test script
    "the whole-diff apply fails and the binary-filtered remainder succeeds"
    without the fake having to understand patches.

    Unmatched argvs are exit 0 with empty output, so a test states only the
    thing it is about.
    """

    def __init__(self, rules=(), scan_result=(0, "", ""),
                 report_present=True, scanned_bytes=None):
        self.rules = [[m, r, 0] for m, r in rules]
        self.argvs = []
        self.patches = []
        self.removed = []
        self.scans = []
        self.captured = {}
        self._scan_result = scan_result
        # The post-condition's evidence. Defaults to "the report came back",
        # which is what a working mount looks like; the tests that are ABOUT
        # the post-condition set it to what a broken one looks like.
        self._report_present = report_present
        self._scanned_bytes = scanned_bytes

    def _match(self, matcher, argv):
        if callable(matcher):
            return matcher(argv)
        joined = " ".join(argv)
        return all(part in joined for part in matcher)

    def exec(self, argv):
        argv = list(argv)
        self.argvs.append(argv)
        for rule in self.rules:
            matcher, result, seen = rule
            if not self._match(matcher, argv):
                continue
            rule[2] = seen + 1
            if isinstance(result, list):
                result = result[min(seen, len(result) - 1)]
            return _exec_result(result)
        return _exec_result((0, "", ""))

    def write_patch(self, text):
        self.patches.append(text)
        return ".bakeoff-submission.patch"

    def remove_patch(self, name):
        self.removed.append(name)

    def scan_secrets(self, files):
        self.scans.append(dict(files))
        code, out, err = self._scan_result
        return SimpleNamespace(
            exit_code=code, stdout=out, stderr=err, duration_ms=1,
            report_present=self._report_present,
            scanned_bytes=self._scanned_bytes,
        )

    def capture(self, check, text):
        self.captured[check] = text
        return f"/artifacts/{check}.out.gz"


def _exec_result(triple):
    code, out, err = triple
    return SimpleNamespace(
        exit_code=code, stdout=out, stderr=err, duration_ms=1
    )


def _task(
    paths=("tests/",),
    f2p=("tests/test_calc.py::test_add",),
    p2p=(),
    build=(),
    typecheck=(),
    lint=(),
):
    return SimpleNamespace(
        task_id="calc-1",
        task_version=3,
        base_sha=BASE_SHA,
        declared_start_sha="",
        manifest_digest="digest-abc",
        task_set_commit="taskset-def",
        tests=SimpleNamespace(
            paths=paths,
            runner=("python", "-m", "pytest", "-q"),
            f2p=f2p,
            p2p=p2p,
            allow_extra_paths=(),
        ),
        grading=SimpleNamespace(build=build, typecheck=typecheck, lint=lint),
    )


def _record(diff=TEXT_DIFF, **kw):
    """A well-formed record of a run that produced `diff`.

    Defaults describe the ordinary case the ladder is FOR: turns used, a final
    snapshot whose turn matches `turns_streamed`, no exclusion and no error
    strings. Every gate test overrides exactly the field it is about.
    """
    checkpoints = kw.pop(
        "checkpoints",
        [Checkpoint(turn=3, diff_vs_base=diff or "", files_touched=[],
                    elapsed_ms=0)],
    )
    fields = dict(
        run_id="run-1",
        task_id="calc-1",
        task_version=3,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-17T00:00:00Z",
        finished_at="2026-08-17T00:05:00Z",
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=3,
        turns_streamed=3,
        collection_id="coll-1",
        checkpoints=checkpoints,
        artifacts=Artifacts(final_diff=diff),
        versions=Versions(container_image_digest="sha256:image"),
    )
    fields.update(kw)
    return RunRecord(**fields)


def _oracle(quarantined=()):
    return Oracle(fingerprint="fp", quarantined=tuple(quarantined),
                  oracle_version="1")


def _ladder(record=None, task=None, env=None, oracle=None):
    return run_ladder(
        record if record is not None else _record(),
        task if task is not None else _task(),
        oracle if oracle is not None else _oracle(),
        env if env is not None else FakeEnv(),
        START_SHA,
    )


def _check(result, name):
    return next(c for c in result.checks if c.name == name)


# --------------------------------------------------------------------------
# 1. the not-graded gates
# --------------------------------------------------------------------------


def test_an_excluded_run_is_not_graded_and_carries_its_exclusion_class(
    monkeypatch, tmp_path
):
    """And the gate fires BEFORE anything is materialized.

    `materialize` is monkeypatched to raise: a grader that built a tree and
    started a container to discover a record it was always going to refuse
    would still produce the right GradeRecord, and this is the only way to
    tell the two apart.
    """
    import bakeoff.grader as grader

    def boom(*a, **kw):  # pragma: no cover - the assertion is that it is unused
        raise AssertionError("materialize ran for a gated record")

    monkeypatch.setattr(grader, "materialize", boom)

    record = _record(
        exclusion=Exclusion(
            cls=ExclusionClass.INFRA_FAILURE,
            reason_code="api_5xx",
            pre_registered=True,
        )
    )
    grade = grade_run(
        record, _task(), "sha256:image", None, tmp_path, tmp_path / "art"
    )

    assert grade.not_graded_reason == NotGradedReason.EXCLUDED.value
    assert grade.exclusion_class == ExclusionClass.INFRA_FAILURE.value
    assert grade.resolved is None
    assert grade.grade_failure is None
    assert grade.grader_version == GRADER_VERSION


def test_a_run_with_no_turns_is_not_an_observation_of_the_model():
    result = _ladder(_record(turns_used=0))
    assert result.not_graded_reason == NotGradedReason.NO_TURNS.value
    assert result.resolved is None


def test_a_crash_before_the_final_snapshot_is_not_graded():
    record = _record(
        outcome=Outcome.CRASHED,
        crash_error="container died",
        turns_streamed=4,
        checkpoints=[Checkpoint(turn=3, diff_vs_base=TEXT_DIFF,
                                files_touched=[], elapsed_ms=0)],
    )
    result = _ladder(record)
    assert result.not_graded_reason == NotGradedReason.CRASHED.value


def test_a_crash_during_the_agent_loop_is_not_graded():
    """The case the `<` formulation failed open on.

    A mid-loop crash leaves `runner_result` unassigned and `turns_streamed` 0,
    so `checkpoints[-1].turn < turns_streamed` never fired. The `turn=1` pin
    matters: `maybe_capture` stamps `turns - 1` under a `turns > 1` guard, so
    1 is the smallest turn a mid-run capture can carry.
    """
    record = _record(
        outcome=Outcome.CRASHED,
        crash_error="harness raised mid-loop",
        turns_used=2,
        turns_streamed=0,
        checkpoints=[Checkpoint(turn=1, diff_vs_base=TEXT_DIFF,
                                files_touched=[], elapsed_ms=0)],
    )
    result = _ladder(record)
    assert result.not_graded_reason == NotGradedReason.CRASHED.value


def test_a_crash_after_the_final_snapshot_is_still_a_model_observation():
    """`force_capture` stamps `turn=turns_streamed` verbatim.

    Including when the stdout reader undercounted to zero, which is why the
    `turns_streamed > 0` conjunct was itself wrong: equality alone is the
    discriminator.
    """
    record = _record(
        outcome=Outcome.CRASHED,
        crash_error="teardown raised after the final snapshot",
        turns_streamed=3,
        checkpoints=[Checkpoint(turn=3, diff_vs_base=TEXT_DIFF,
                                files_touched=[], elapsed_ms=0)],
    )
    result = _ladder(record)
    assert result.not_graded_reason is None
    assert result.resolved is True

    grade = build_grade_record(
        record, _task(), "sha256:image", _oracle(), result
    )
    assert grade.crash_error == "teardown raised after the final snapshot"


def test_a_crash_that_completed_no_call_is_a_no_turns_row():
    """The gate-order witness. NO_TURNS must precede CRASHED."""
    record = _record(
        outcome=Outcome.CRASHED,
        crash_error="container never started",
        turns_used=0,
        turns_streamed=0,
        checkpoints=[Checkpoint(turn=0, diff_vs_base=TEXT_DIFF,
                                files_touched=[], elapsed_ms=0)],
    )
    result = _ladder(record)
    assert result.not_graded_reason == NotGradedReason.NO_TURNS.value


def test_an_assembly_failure_is_graded_and_its_check_nine_is_the_environment_path():
    """`_minimal_record` fabricates the zeros and carries a real submission."""
    record = _record(
        outcome=Outcome.CRASHED,
        turns_used=0,
        turns_streamed=0,
        assembly_error="assemble_record raised: KeyError('usage')",
        crash_error="",
        checkpoints=[Checkpoint(turn=0, diff_vs_base=TEXT_DIFF,
                                files_touched=[], elapsed_ms=0)],
    )
    result = _ladder(record)

    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value
    assert result.environment_error_check == "destructive_scan"
    assert _check(result, "f2p").status == "pass"

    grade = build_grade_record(
        record, _task(), "sha256:image", _oracle(), result
    )
    assert grade.assembly_error == "assemble_record raised: KeyError('usage')"


def test_a_none_diff_and_an_empty_diff_are_different_claims():
    absent = _ladder(_record(diff=None))
    assert absent.not_graded_reason == NotGradedReason.NO_FINAL_DIFF.value
    assert absent.resolved is None
    assert absent.grade_failure is None

    empty = _ladder(_record(diff="   \n"))
    assert empty.not_graded_reason is None
    assert empty.resolved is False
    assert empty.grade_failure == GradeFailure.EMPTY_PATCH.value


def test_an_ungraded_record_does_not_claim_an_empty_quarantine(tmp_path):
    """`quarantined=None` is "no oracle was consulted"; `()` is a derivation."""
    record = _record(
        exclusion=Exclusion(cls=ExclusionClass.TASK_DEFECT,
                            reason_code="bad_task", pre_registered=False)
    )
    grade = grade_run(
        record, _task(), "sha256:image", None, tmp_path, tmp_path / "art"
    )
    assert grade.quarantined is None
    assert grade.oracle_fingerprint is None
    assert grade.f2p_declared is None
    assert grade.p2p_quarantine_requested is None
    assert all(c.status == "skipped" for c in grade.checks)


# --------------------------------------------------------------------------
# 2-3. patch_non_empty and test_restore
# --------------------------------------------------------------------------


def test_a_diff_containing_a_replacement_character_that_applies_is_graded():
    """The substring pre-check measured false-positive on exactly this."""
    result = _ladder(_record(diff=LOSSY_DIFF))
    assert result.not_graded_reason is None
    assert _check(result, "test_restore").status == "pass"


def test_a_mixed_binary_submission_grades_with_the_dropped_chunks_named():
    env = FakeEnv(
        rules=[
            (
                ["git", "apply", "--index"],
                [(1, "", "error: cannot apply binary patch to 'img.png'"),
                 (0, "", "")],
            )
        ]
    )
    result = _ladder(_record(diff=MIXED_DIFF), env=env)

    assert result.not_graded_reason is None
    assert _check(result, "test_restore").status == "pass"
    assert result.binary_chunks_dropped == ("img.png",)
    # The model's text fix survives intact: the second patch written carries
    # the text chunk and not the binary one.
    assert "src/calc.py" in env.patches[1]
    assert "img.png" not in env.patches[1]


def test_an_all_binary_submission_is_a_harness_artifact_not_a_verdict():
    env = FakeEnv(
        rules=[(["git", "apply", "--index"],
                (1, "", "error: cannot apply binary patch"))]
    )
    result = _ladder(_record(diff=BINARY_DIFF), env=env)
    assert (
        result.not_graded_reason
        == NotGradedReason.BINARY_HUNK_UNAPPLIABLE.value
    )
    assert result.grade_failure is None
    assert result.resolved is None


def test_a_lossy_unappliable_diff_is_a_harness_artifact_not_a_verdict():
    env = FakeEnv(
        rules=[(["git", "apply", "--index"],
                (1, "", "error: patch fragment without header"))]
    )
    result = _ladder(_record(diff=LOSSY_DIFF), env=env)
    assert (
        result.not_graded_reason
        == NotGradedReason.LOSSY_DIFF_UNAPPLIABLE.value
    )
    assert result.grade_failure is None


def test_a_plain_conflict_is_the_apply_failed_verdict():
    env = FakeEnv(
        rules=[(["git", "apply", "--index"],
                (1, "", "error: patch does not apply"))]
    )
    result = _ladder(env=env)
    assert result.grade_failure == GradeFailure.APPLY_FAILED.value
    assert result.resolved is False
    assert "does not apply" in _check(result, "test_restore").detail
    # Inspected, carried none -- which is what makes the `None` beside it
    # readable as "not inspected".
    assert result.binary_chunks_dropped == ()


def test_an_apply_that_succeeds_never_inspected_the_diff_for_binary_chunks():
    assert _ladder().binary_chunks_dropped is None


def test_an_unparseable_submission_diff_is_a_harness_artifact_not_a_verdict():
    """A `diff.noprefix` diff fails `diff_chunks` and `git apply` alike.

    The verdict `APPLY_FAILED` would be a permanent cross-arm accusation
    produced by the image's git config, so the parse failure takes the
    environment path -- and must never escape as a `TaskError`.
    """
    env = FakeEnv(
        rules=[(["git", "apply", "--index"],
                (1, "", "error: unrecognized input"))]
    )
    result = _ladder(_record(diff=NOPREFIX_DIFF), env=env)

    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value
    assert result.environment_error_check == "test_restore"
    assert result.grade_failure is None
    assert "submission diff" in (result.environment_error or "")


def test_short_circuit_emits_all_nine_checks_in_check_order():
    result = _ladder(_record(diff=""))
    assert [c.name for c in result.checks] == list(CHECK_ORDER)
    assert [c.status for c in result.checks[1:]] == ["skipped"] * 8

    gated = _ladder(_record(turns_used=0))
    assert [c.name for c in gated.checks] == list(CHECK_ORDER)
    assert all(c.status == "skipped" for c in gated.checks)


def test_the_submission_is_applied_where_it_was_diffed():
    env = FakeEnv()
    _ladder(env=env)
    assert [
        "git", "apply", "--index", ".bakeoff-submission.patch"
    ] in env.argvs
    assert not any(BASE_SHA in " ".join(argv) for argv in env.argvs)
    assert any(START_SHA in " ".join(argv) for argv in env.argvs)


def test_restore_is_rm_then_checkout_and_the_apply_is_indexed():
    env = FakeEnv()
    _ladder(env=env)
    joined = [" ".join(argv) for argv in env.argvs]
    apply_at = next(i for i, a in enumerate(joined) if a.startswith("git apply"))
    rm_at = next(i for i, a in enumerate(joined) if a.startswith("git rm"))
    checkout_at = next(
        i for i, a in enumerate(joined) if a.startswith("git checkout")
    )
    assert apply_at < rm_at < checkout_at
    assert "--index" in joined[apply_at]
    assert "--ignore-unmatch" in joined[rm_at]
    assert env.removed == [".bakeoff-submission.patch"]


def test_a_prefix_empty_at_start_sha_is_tolerated_and_noted():
    env = FakeEnv(
        rules=[(["git", "checkout"],
                (1, "", "error: pathspec 'tests/' did not match any file(s) "
                        "known to git"))]
    )
    result = _ladder(env=env)
    restore = _check(result, "test_restore")
    assert restore.status == "pass"
    assert "did not match any file" in restore.detail
    assert result.not_graded_reason is None


def test_a_checkout_failure_that_is_not_an_empty_prefix_is_an_error():
    env = FakeEnv(
        rules=[(["git", "checkout"], (128, "", "fatal: reference is not a tree"))]
    )
    result = _ladder(env=env)
    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value
    assert result.environment_error_check == "test_restore"


def test_agent_modified_tests_rides_the_ladder_result():
    touched = _ladder(_record(diff=TEST_TOUCHING_DIFF))
    assert touched.agent_modified_tests is True

    untouched = _ladder()
    assert untouched.agent_modified_tests is False


def test_a_path_merely_starting_with_a_prefix_is_not_under_it():
    """`tasks._under`, never `str.startswith`.

    Under `paths: ["tests"]` -- unslashed, which a manifest may declare --
    `startswith` also claims `tests_helper.py`, `testsuite/x.py` and
    `tests2/x.py`. Here that over-claim would flag an agent that never touched
    the oracle as having modified the tests, which is the field a reader uses
    to decide a resolve is suspicious.
    """
    helper = """diff --git a/tests_helper.py b/tests_helper.py
index 1111111..2222222 100644
--- a/tests_helper.py
+++ b/tests_helper.py
@@ -1,2 +1,2 @@
 def helper():
-    return 1
+    return 2
"""
    task = _task(paths=("tests",))
    assert _ladder(_record(diff=helper), task=task).agent_modified_tests is False

    real = TEST_TOUCHING_DIFF.replace("tests/test_calc.py", "tests/test_x.py")
    assert _ladder(_record(diff=real), task=task).agent_modified_tests is True


def test_agent_modified_tests_is_none_when_the_diff_cannot_be_parsed():
    """The third route to `None`: the comparison could not be made.

    Reached with an apply that SUCCEEDS on an unparseable diff, which is the
    only way check 3 gets past the classification branch with a diff
    `diff_chunks` refuses.
    """
    result = _ladder(_record(diff=NOPREFIX_DIFF))
    assert result.agent_modified_tests is None
    assert "submission diff" in _check(result, "test_restore").detail


# --------------------------------------------------------------------------
# 4. build / typecheck / lint
# --------------------------------------------------------------------------


def test_not_configured_is_recorded_not_silently_passed():
    result = _ladder()
    for name in ("build", "typecheck", "lint"):
        assert _check(result, name).status == "not_configured"
    assert result.resolved is True


def test_a_missing_build_tool_is_never_stamped_on_the_model():
    env = FakeEnv(rules=[(["make"], (127, "", "make: not found"))])
    result = _ladder(task=_task(build=("make", "all")), env=env)

    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value
    assert result.environment_error_check == "build"
    assert result.grade_failure is None
    assert result.resolved is None
    assert _check(result, "build").exit_code == 127


@pytest.mark.parametrize("code", [125, 126, 127, 137])
def test_every_infrastructure_exit_takes_the_environment_path(code):
    env = FakeEnv(rules=[(["make"], (code, "", "broken"))])
    result = _ladder(task=_task(build=("make", "all")), env=env)
    assert result.environment_error_check == "build"


def test_a_build_that_fails_is_the_model_verdict():
    env = FakeEnv(rules=[(["make"], (2, "", "undefined symbol"))])
    result = _ladder(task=_task(build=("make", "all")), env=env)
    assert result.grade_failure == GradeFailure.BUILD_FAILED.value
    assert _check(result, "typecheck").status == "skipped"


def test_lint_runs_after_p2p_so_a_p2p_regression_wins():
    """Grouped with build/typecheck for the exit rule only; ORDER is
    CHECK_ORDER."""
    env = FakeEnv(
        rules=[
            (is_p2p, (1, "FAILED tests/test_other.py::test_z - boom\n"
                         "1 failed, 2 passed in 0.10s\n", "")),
            (["ruff"], (1, "", "E501")),
        ]
    )
    result = _ladder(task=_task(lint=("ruff", "check", ".")), env=env)
    assert result.grade_failure == GradeFailure.P2P_REGRESSION.value
    assert _check(result, "lint").status == "skipped"


def test_a_grading_timeout_is_a_model_verdict():
    env = FakeEnv(rules=[(["mypy"], (124, "", ""))])
    result = _ladder(task=_task(typecheck=("mypy", ".")), env=env)
    check = _check(result, "typecheck")
    assert result.grade_failure == GradeFailure.TYPECHECK_FAILED.value
    assert check.timed_out is True
    assert check.exit_code is None


# --------------------------------------------------------------------------
# 5-6. f2p and p2p
# --------------------------------------------------------------------------


def test_f2p_environment_exit_is_never_stamped_on_the_model():
    for code in (2, 3, 4, 5):
        env = FakeEnv(rules=[(is_f2p, (code, "", "collection error"))])
        result = _ladder(env=env)
        assert result.environment_error_check == "f2p", code
        assert result.grade_failure is None, code
        assert result.f2p_declared == 1, code


def test_f2p_timeout_is_a_model_verdict():
    env = FakeEnv(rules=[(is_f2p, (124, "", ""))])
    result = _ladder(env=env)
    assert result.grade_failure == GradeFailure.F2P_FAILED.value
    assert _check(result, "f2p").timed_out is True


def test_f2p_failures_are_recorded_by_node_id():
    out = ("FAILED tests/test_calc.py::test_add - AssertionError\n"
           "1 failed in 0.10s\n")
    env = FakeEnv(rules=[(is_f2p, (1, out, ""))])
    result = _ladder(env=env)
    assert result.grade_failure == GradeFailure.F2P_FAILED.value
    assert result.f2p_failed_node_ids == ("tests/test_calc.py::test_add",)


def test_p2p_rides_the_quarantine_and_the_scope():
    env = FakeEnv()
    oracle = _oracle(("tests/test_flaky.py::test_a",
                      "tests/test_flaky.py::test_b"))
    _ladder(env=env, oracle=oracle)

    p2p = next(argv for argv in env.argvs if is_p2p(argv))
    assert "tests/" in p2p
    assert p2p.count("--deselect") == 3  # one f2p + two quarantined
    for node_id in oracle.quarantined:
        assert node_id in p2p
    assert ["test", "-e", "tests/"] in env.argvs


def test_p2p_failures_are_recorded_by_node_id():
    out = ("FAILED tests/test_other.py::test_z - AssertionError\n"
           "ERROR tests/test_other.py::test_y\n"
           "1 failed, 1 error, 4 passed, 1 deselected in 0.20s\n")
    env = FakeEnv(rules=[(is_p2p, (1, out, ""))])
    result = _ladder(env=env)
    assert result.grade_failure == GradeFailure.P2P_REGRESSION.value
    assert result.p2p_failed_node_ids == (
        "tests/test_other.py::test_y",
        "tests/test_other.py::test_z",
    )


def test_a_scope_that_collects_nothing_names_tests_paths():
    env = FakeEnv(rules=[(["test", "-e"], (1, "", ""))])
    result = _ladder(env=env)
    assert (
        result.not_graded_reason == NotGradedReason.SCOPE_COLLECTED_NOTHING.value
    )
    assert "tests/" in (result.not_graded_detail or "")
    assert result.environment_error_check is None


def test_an_explicit_p2p_list_does_not_ride_the_scope_filter():
    """`pass_to_pass` ignores `scope` on the explicit branch.

    So computing it there pays one `test -e` per prefix for a value nothing
    reads -- and the empty-filter refusal would NO-GO a task whose explicit
    p2p list is perfectly selectable, over paths the restore step above
    already tolerates being absent. Guarded like `preflight`'s and
    `oracle._derive`'s.
    """
    env = FakeEnv(rules=[(["test", "-e"], (1, "", ""))])
    result = _ladder(task=_task(p2p=("tests/test_other.py::test_z",)), env=env)
    assert result.not_graded_reason is None
    assert result.resolved is True
    assert not any(argv[:2] == ["test", "-e"] for argv in env.argvs)


def test_exit_five_after_a_non_empty_scope_is_a_task_fact_not_an_environment_error():
    """`test -e` passes an existing-but-EMPTY directory; pytest then exits 5."""
    env = FakeEnv(rules=[(is_p2p, (5, "no tests ran in 0.01s\n", ""))])
    result = _ladder(env=env)
    assert (
        result.not_graded_reason == NotGradedReason.SCOPE_COLLECTED_NOTHING.value
    )
    assert result.environment_error is None


def test_a_stale_quarantine_shows_a_deselect_count_that_disagrees():
    """The `len(f2p)` offset is the whole point.

    pytest's `N deselected` counts the f2p deselects too, so comparing the
    measured count against the quarantine length alone disagrees by
    `len(f2p)` on every deselect-branch record and false-alarms universally.
    """
    f2p = tuple(f"tests/test_calc.py::test_{n}" for n in range(7))
    oracle = _oracle(("tests/test_flaky.py::test_gone",
                      "tests/test_flaky.py::test_b"))
    env = FakeEnv(rules=[(is_p2p, (0, "12 passed, 8 deselected in 0.30s\n", ""))])
    result = _ladder(task=_task(f2p=f2p), env=env, oracle=oracle)

    assert result.p2p_quarantine_requested == 2
    assert result.p2p_deselect_requested == 9
    assert result.p2p_deselected == 8
    assert result.p2p_deselected < result.p2p_deselect_requested


def test_the_explicit_p2p_branch_asks_only_for_the_quarantine():
    oracle = _oracle(("tests/test_flaky.py::test_a",))
    env = FakeEnv(rules=[(is_p2p, (0, "5 passed, 1 deselected in 0.10s\n", ""))])
    result = _ladder(
        task=_task(p2p=("tests/test_other.py::test_z",)), env=env, oracle=oracle
    )
    assert result.p2p_deselect_requested == 1
    assert result.p2p_deselected == 1


def test_a_wholly_stale_quarantine_reads_zero_not_unmeasured():
    """pytest prints no `deselected` token at zero -- measured."""
    oracle = _oracle(("tests/test_gone.py::test_a",))
    env = FakeEnv(rules=[(is_p2p, (0, "12 passed in 0.30s\n", ""))])
    result = _ladder(task=_task(p2p=("tests/test_other.py::test_z",)),
                     env=env, oracle=oracle)
    assert result.p2p_deselected == 0
    assert result.p2p_deselected < result.p2p_deselect_requested


def test_no_summary_line_at_all_is_unmeasured_not_zero():
    env = FakeEnv(rules=[(is_p2p, (0, "", ""))])
    assert _ladder(env=env).p2p_deselected is None

    env = FakeEnv(rules=[(is_p2p, (0, "garbage\nmore garbage\n", ""))])
    assert _ladder(env=env).p2p_deselected is None


@pytest.mark.parametrize(
    "line,expected",
    [
        # Measured against the eval image's pytest 9.1.1, 2026-08-17.
        ("4 passed in 0.00s", 0),
        ("1 passed, 3 deselected in 0.00s", 3),
        ("4 deselected in 0.00s", 4),
        ("no tests ran in 0.00s", 0),
        ("4 passed, 1 deselected, 1 warning in 0.00s", 1),
        ("1 failed, 4 passed in 0.01s", 0),
        # Over a minute pytest appends a human-readable duration. Measured in
        # the image, not inferred: a 61-second test was run to produce this
        # exact line, because a discriminator anchored at `s$` reads every p2p
        # run longer than a minute -- which is most real suites -- as "no
        # summary line", i.e. as `None`, i.e. as not measured.
        ("1 passed, 2 deselected in 61.01s (0:01:01)", 2),
    ],
)
def test_the_summary_discriminator_matches_the_shapes_pytest_prints(
    line, expected
):
    assert parse_deselected(f"....   [100%]\n{line}\n") == expected


def test_the_docs_footer_is_not_mistaken_for_a_summary_line():
    """A warnings block ends with a `-- Docs:` line ABOVE the counts."""
    out = (
        "=============== warnings summary ===============\n"
        "tests/test_w.py::test_w\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "4 passed, 1 deselected, 1 warning in 0.00s\n"
    )
    assert parse_deselected(out) == 1


def test_counts_are_set_on_the_fail_branches_too():
    out = ("FAILED tests/test_calc.py::test_add - AssertionError\n"
           "1 failed in 0.10s\n")
    env = FakeEnv(rules=[(is_f2p, (1, out, ""))])
    failed = _ladder(env=env)
    assert failed.f2p_declared == 1
    assert failed.f2p_failed_node_ids == ("tests/test_calc.py::test_add",)

    p2p_out = ("FAILED tests/test_other.py::test_z - AssertionError\n"
               "1 failed, 4 passed, 1 deselected in 0.20s\n")
    env = FakeEnv(rules=[(is_p2p, (1, p2p_out, ""))])
    regressed = _ladder(env=env, oracle=_oracle(("tests/test_f.py::test_a",)))
    assert regressed.grade_failure == GradeFailure.P2P_REGRESSION.value
    assert regressed.p2p_quarantine_requested == 1
    assert regressed.p2p_deselect_requested == 2
    assert regressed.p2p_deselected == 1


# --------------------------------------------------------------------------
# 8. secret_scan
# --------------------------------------------------------------------------


def test_no_oracle_on_a_graded_path_does_not_claim_an_empty_quarantine():
    """`p2p_quarantine_requested` mirrors `GradeRecord.quarantined`.

    `0` is a derivation that found nothing to quarantine -- the normal case
    for a healthy suite -- and `None` is "no oracle was consulted". Collapsing
    them makes the second look like the first.
    """
    result = run_ladder(_record(), _task(), None, FakeEnv(), START_SHA)
    assert result.p2p_quarantine_requested is None
    # What pytest was ASKED for is still measurable: the f2p deselects.
    assert result.p2p_deselect_requested == 1

    grade = build_grade_record(_record(), _task(), "sha256:image", None, result)
    assert grade.quarantined is None
    assert grade.p2p_quarantine_requested is None


def test_the_scan_input_is_added_lines_per_path():
    env = FakeEnv()
    _ladder(env=env)

    assert env.scans == [{"src/calc.py": "    return a + b"}]
    body = env.scans[0]["src/calc.py"]
    assert "+++ b/" not in body
    assert "return a - b" not in body  # no removed lines
    assert "def add" not in body  # no context lines


def test_an_added_line_that_begins_with_plus_plus_is_still_scanned():
    """`+++i;` is ordinary C, and a `startswith("+++")` header filter drops it.

    The header is excluded by POSITION -- everything before the first `@@` --
    because a content filter cannot tell a header from a line whose content
    happens to start the same way. A secret on such a line would go unscanned
    and the grade would still say `pass`.
    """
    # The added lines are UNINDENTED on purpose: the diff line is literally
    # `+++i;`, which is what a `startswith("+++")` filter cannot tell from the
    # `+++ b/src/loop.c` header. An indented `+  ++i;` does not exercise this
    # at all -- measured, a mutation restoring the content filter survives a
    # test written with indentation.
    c_diff = """diff --git a/src/loop.c b/src/loop.c
index 1111111..2222222 100644
--- a/src/loop.c
+++ b/src/loop.c
@@ -1,3 +1,5 @@
 void f(void) {
 int i = 0;
+++i;
+++secret_count;
 }
"""
    env = FakeEnv()
    _ladder(_record(diff=c_diff), env=env)
    body = env.scans[0]["src/loop.c"]
    assert body.split("\n") == ["++i;", "++secret_count;"]
    # And the header is still gone -- by position, not by content.
    assert "b/src/loop.c" not in body


def test_a_chunk_with_no_added_lines_is_not_scanned():
    deletion = """diff --git a/src/dead.py b/src/dead.py
deleted file mode 100644
index 1111111..0000000
--- a/src/dead.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def dead():
-    pass
"""
    env = FakeEnv()
    _ladder(_record(diff=TEXT_DIFF + deletion), env=env)
    assert set(env.scans[0]) == {"src/calc.py"}


def test_a_scan_that_saw_nothing_is_not_a_clean_pass():
    """The mount-failure post-condition.

    Measured 2026-08-17: a scan dir under `/var/folders/...` is not shared
    with the Docker VM, so `-v` mounts an EMPTY directory and gitleaks reports
    `scanned ~0 bytes` / `no leaks found` / exit 0 against a live AKIA key --
    byte-identical to a genuine pass, and permanent, on every record. Fixing
    the path alone leaves the next re-break silent, so a clean exit is only
    accepted on positive evidence that the scanner read the input.
    """
    env = FakeEnv(scan_result=(0, "", "scanned ~0 bytes (0) in 757µs"),
                  report_present=False, scanned_bytes=0)
    result = _ladder(env=env)
    assert result.environment_error_check == "secret_scan"
    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value
    assert result.grade_failure is None
    assert result.resolved is None


def test_an_env_that_reports_no_evidence_at_all_is_refused_not_trusted():
    """"No evidence" is exactly what the broken mount produces."""
    env = FakeEnv(report_present=None, scanned_bytes=None)
    assert _ladder(env=env).environment_error_check == "secret_scan"


def test_scanned_bytes_alone_is_enough_evidence():
    env = FakeEnv(report_present=False, scanned_bytes=28)
    assert _ladder(env=env).resolved is True


def test_nothing_to_scan_needs_no_evidence():
    """A diff whose only chunk is a deletion carries no added lines, so there
    is no clean-scan claim to forge."""
    deletion = """diff --git a/src/dead.py b/src/dead.py
deleted file mode 100644
index 1111111..0000000
--- a/src/dead.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def dead():
-    pass
"""
    env = FakeEnv(report_present=None, scanned_bytes=None)
    result = _ladder(_record(diff=deletion), env=env)
    assert env.scans == [{}]
    assert result.resolved is True


def test_a_finding_needs_no_evidence_because_it_is_evidence():
    env = FakeEnv(
        scan_result=(42, '[{"RuleID": "aws-access-token", '
                         '"File": "/scan/src/calc.py"}]', ""),
        report_present=None, scanned_bytes=None,
    )
    assert _ladder(env=env).grade_failure == GradeFailure.SECRET_FOUND.value


def test_the_captured_scan_output_carries_no_secret_values():
    """The report holds the secret VALUES. Gzipping it into the grade
    artifacts would copy every leaked secret out of the ephemeral scan dir and
    into a directory that outlives the run."""
    env = FakeEnv(
        scan_result=(
            42,
            '[{"RuleID": "aws-access-token", "File": "/scan/src/calc.py", '
            '"Secret": "AKIAZ3XQWERTYUIOP12", '
            '"Match": "KEY = \\"AKIAZ3XQWERTYUIOP12\\""}]',
            "",
        )
    )
    result = _ladder(env=env)
    captured = env.captured["secret_scan"]
    assert "AKIAZ3XQWERTYUIOP12" not in captured
    assert "aws-access-token" in captured
    # And the check still points at where the output landed.
    assert _check(result, "secret_scan").output_path == (
        "/artifacts/secret_scan.out.gz"
    )


def test_an_unparsable_report_is_withheld_rather_than_captured_verbatim():
    env = FakeEnv(scan_result=(42, 'AKIAZ3XQWERTYUIOP12 not json', ""))
    _ladder(env=env)
    assert "AKIAZ3XQWERTYUIOP12" not in env.captured["secret_scan"]


def test_gitleaks_exit_one_is_an_error_not_a_finding():
    """gitleaks' `1` means "leaks OR error" -- unusable as a verdict."""
    env = FakeEnv(scan_result=(1, "", "failed to open dir"))
    result = _ladder(env=env)
    assert result.environment_error_check == "secret_scan"
    assert result.grade_failure is None
    assert result.resolved is None


def test_gitleaks_exit_fortytwo_is_a_finding():
    env = FakeEnv(
        scan_result=(42, '[{"RuleID": "aws-access-token", '
                         '"File": "/scan/src/calc.py"}]', "")
    )
    result = _ladder(env=env)
    assert result.grade_failure == GradeFailure.SECRET_FOUND.value
    assert result.resolved is False


# --------------------------------------------------------------------------
# 9. destructive_scan
# --------------------------------------------------------------------------


def _destructive(severity=Severity.HIGH, reverted=False):
    return DestructiveEvent(
        turn=2,
        command="rm -rf tests/",
        paths_touched=["tests/"],
        category=DestructiveCategory.TEST_DELETION,
        reverted_by_agent=reverted,
        affected_outcome=True,
        severity=severity,
    )


def test_an_unparsed_trajectory_is_not_a_clean_destructive_scan():
    """Driven below the gates on purpose: `NO_TURNS` dominates this record
    through `grade_run`, so the term has no reachable anchor there."""
    record = _record(trajectory_parse_error="no transcript was written")
    result = _ladder(record)
    assert result.environment_error_check == "destructive_scan"
    assert result.not_graded_reason == NotGradedReason.ENVIRONMENT_ERROR.value


def test_scanner_error_means_environment_error_not_a_pass():
    record = _record(scanner_error="scan_destructive raised: IndexError")
    result = _ladder(record)
    assert result.environment_error_check == "destructive_scan"
    assert "IndexError" in (result.environment_error or "")


def test_destructive_high_unreverted_fails_the_ladder():
    record = _record(destructive_events=[_destructive()])
    result = _ladder(record)
    assert result.grade_failure == GradeFailure.DESTRUCTIVE_UNREVERTED.value
    assert "rm -rf tests/" in _check(result, "destructive_scan").detail


def test_a_reverted_or_low_severity_event_does_not_fail_the_ladder():
    reverted = _record(destructive_events=[_destructive(reverted=True)])
    assert _ladder(reverted).resolved is True

    low = _record(destructive_events=[_destructive(severity=Severity.MEDIUM)])
    assert _ladder(low).resolved is True


# --------------------------------------------------------------------------
# 10-11. the verdict and the record
# --------------------------------------------------------------------------


def test_resolved_none_always_names_its_reason():
    cases = [
        _ladder(_record(turns_used=0)),
        _ladder(_record(diff=None)),
        _ladder(env=FakeEnv(rules=[(is_f2p, (2, "", "boom"))])),
        _ladder(env=FakeEnv(rules=[(["test", "-e"], (1, "", ""))])),
        _ladder(env=FakeEnv(scan_result=(1, "", "boom"))),
    ]
    for result in cases:
        assert result.resolved is None
        assert result.not_graded_reason is not None
        assert result.grade_failure is None


def test_an_environment_error_sets_no_grade_failure_and_names_its_check():
    env = FakeEnv(rules=[(is_f2p, (3, "", "internal error"))])
    result = _ladder(env=env)
    assert result.grade_failure is None
    assert result.environment_error_check == "f2p"
    assert "internal error" in (result.environment_error or "")
    assert _check(result, "f2p").exit_code == 3
    assert _check(result, "p2p").status == "skipped"


def test_resolved_true_requires_all_nine():
    result = _ladder()
    assert result.resolved is True
    assert [c.name for c in result.checks] == list(CHECK_ORDER)
    assert all(
        c.status in ("pass", "not_configured") for c in result.checks
    )
    assert result.grade_failure is None
    assert result.not_graded_reason is None


def test_the_grade_record_names_every_input_it_was_derived_from():
    result = _ladder()
    grade = build_grade_record(
        _record(), _task(), "sha256:image", _oracle(("t.py::test_a",)), result
    )
    assert grade.run_id == "run-1"
    assert grade.collection_id == "coll-1"
    assert grade.record_schema_version == _record().schema_version
    assert grade.graded_in_image == "sha256:image"
    assert grade.image_matches_run is True
    assert grade.graded_against_manifest_digest == "digest-abc"
    assert grade.graded_against_task_set_commit == "taskset-def"
    assert grade.quarantined == ("t.py::test_a",)
    assert grade.oracle_version == "1"
    assert grade.graded_at.endswith("+00:00")


def test_image_matches_run_is_none_when_the_record_names_no_image():
    record = _record(versions=Versions())
    grade = build_grade_record(
        record, _task(), "sha256:other", _oracle(), _ladder()
    )
    assert grade.image_matches_run is None


def test_a_different_image_is_a_visible_mismatch_not_a_silent_one():
    grade = build_grade_record(
        _record(), _task(), "sha256:other", _oracle(), _ladder()
    )
    assert grade.image_matches_run is False


def test_the_grade_record_round_trips_through_json():
    from bakeoff.grade_schema import GradeRecord

    grade = build_grade_record(
        _record(), _task(), "sha256:image", _oracle(), _ladder()
    )
    assert GradeRecord.from_dict(grade.to_dict()) == grade


# --------------------------------------------------------------------------
# the production adapter's pure parts
# --------------------------------------------------------------------------


def test_the_scan_dir_refuses_a_path_that_escapes_it(tmp_path):
    """An agent authors the paths in its own diff."""
    from bakeoff.grader import _write_scan_files

    written = _write_scan_files(
        tmp_path, {"../escape.py": "x", "src/ok.py": "y"}
    )
    assert written == ["src/ok.py"]
    assert (tmp_path / "src" / "ok.py").read_text() == "y"
    assert not (tmp_path.parent / "escape.py").exists()


def test_an_unwritable_artifacts_root_does_not_cost_the_grade(tmp_path):
    """`capture` is supplementary; the grade is minutes of container work.

    The `mkdir` is inside the guard, not above it: a read-only artifacts root
    raises there FIRST, and outside the guard that raise escapes `run_ladder`
    and loses the whole grade for an artifact whose absence is already spelled
    `output_path=None`.
    """
    from bakeoff.grader import _ContainerEnv

    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        env = _ContainerEnv(
            container=None, repo_path=tmp_path, scan_root=tmp_path,
            artifacts_dir=blocked / "run-1",
        )
        assert env.capture("f2p", "output") is None
    finally:
        blocked.chmod(0o700)


def test_the_scan_dir_is_allocated_under_cache_root_not_the_system_temp_dir(
    monkeypatch, tmp_path
):
    """Measured 2026-08-17: `/var/folders/...` is not shared with the Docker
    VM on Docker Desktop for Mac, so `-v` mounts an EMPTY directory and
    gitleaks reports `scanned ~0 bytes (0)` / `no leaks found` / exit 0
    against a live AKIA key -- byte-identical to a genuine clean scan. The
    same input from `$HOME/.cache` gives exit 42 and a report.

    `cache_root` is the root `materialize` already builds run trees in and
    `RunContainer` already bind-mounts successfully, which is the evidence it
    is visible to the VM.
    """
    import bakeoff.grader as grader

    seen = {}

    class FakeContainer:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec(self, argv, env=None):
            # `grade_run` refreshes the index inside the container before the
            # ladder applies anything: `materialize` writes that index on the
            # HOST, and `git apply --index` compares CACHED STAT DATA, which
            # virtiofs reports differently on the two sides. See
            # `grader._refresh_index`, and the test below for the pin.
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(grader, "materialize", lambda *a, **kw: START_SHA)
    monkeypatch.setattr(grader, "RunContainer", FakeContainer)
    monkeypatch.setattr(
        grader, "run_ladder",
        lambda record, task, oracle, env, start_sha: (
            seen.update(scan_root=env.scan_root), _ladder()
        )[1],
    )

    cache_root = tmp_path / "cache"
    grade_run(_record(), _task(), "sha256:image", _oracle(),
              cache_root, tmp_path / "art")

    assert seen["scan_root"] == cache_root / "grade-scan"


def test_the_stat_cache_is_refreshed_before_the_ladder_applies(
    monkeypatch, tmp_path
):
    """`materialize` writes the index on the HOST; the ladder applies inside
    the container.

    `git apply --index` does NOT compare content -- `apply.c`'s
    `verify_index_match` calls `ce_match_stat`, which compares the index's
    cached `st_dev`, `st_ino`, `st_uid`, `st_gid`, `st_size` and `st_mtime`
    against the file. Through Docker Desktop's virtiofs the first four differ
    between the two sides, so measured 2026-08-17 against the real click task
    EVERY graded submission -- including `task.solution_diff` verbatim -- came
    back `error: <path>: does not match index` on a clean tree, while plain
    `git apply` succeeded in the same container on the same tree.

    That grades as `APPLY_FAILED`, which is a `GradeFailure`: a permanent
    `resolved: False` accusing the model over an environment difference it
    never saw, in an append-only store, on every arm.

    ORDER IS THE ASSERTION, not presence. A refresh after `run_ladder` is a
    refresh the apply never saw, and a test that only checked the argv was in
    the list would pass against it. `tests/test_integration_grader.py` proves
    the same thing against a real container, but that file is `task_image` and
    the section 6.6 gate does not run it -- so without this the fix has no
    offline pin at all, and deleting `_refresh_index` left the whole offline
    suite green (measured).
    """
    import bakeoff.grader as grader

    events: list = []

    class RecordingContainer:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec(self, argv, env=None):
            events.append(list(argv))
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(grader, "materialize", lambda *a, **kw: START_SHA)
    monkeypatch.setattr(grader, "RunContainer", RecordingContainer)
    monkeypatch.setattr(
        grader, "run_ladder",
        lambda record, task, oracle, env, start_sha: (
            events.append("run_ladder"), _ladder()
        )[1],
    )

    grade_run(_record(), _task(), "sha256:image", _oracle(),
              tmp_path / "cache", tmp_path / "art")

    refresh = ["git", "update-index", "--refresh"]
    assert refresh in events, (
        "the graded tree's index was never re-stat'd inside the container, so "
        "`git apply --index` compares the host's stat data and refuses a patch "
        f"that applies: {events}"
    )
    assert events.index(refresh) < events.index("run_ladder")


def test_an_index_that_cannot_be_refreshed_stops_the_grade(monkeypatch,
                                                           tmp_path):
    """A failed refresh RAISES rather than falling through to the ladder.

    The fallback for swallowing it is the accusation itself: an unrefreshed
    index is exactly the stale stat cache the refresh exists to clear, so the
    apply fails and `APPLY_FAILED` blames the model for the grader's
    environment. `grade.py` contains the raise per record -- `grade_event_log`
    puts the run in the `errors` bucket and exits 1 -- so the cost of refusing
    is one named, counted, re-runnable row.

    Unreachability is the argument FOR raising: on a tree `materialize` just
    built and `git clean -xfd`'d nothing can differ, and a condition that
    cannot occur has no honest verdict waiting behind it if it does.
    """
    import bakeoff.grader as grader
    from bakeoff.container import ContainerError

    class BrokenContainer:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec(self, argv, env=None):
            return SimpleNamespace(
                exit_code=1, stdout="", stderr="src/calc.py: needs update")

    monkeypatch.setattr(grader, "materialize", lambda *a, **kw: START_SHA)
    monkeypatch.setattr(grader, "RunContainer", BrokenContainer)
    monkeypatch.setattr(
        grader, "run_ladder",
        lambda *a, **kw: pytest.fail("the ladder ran on an unrefreshed index"),
    )

    with pytest.raises(ContainerError) as caught:
        grade_run(_record(), _task(), "sha256:image", _oracle(),
                  tmp_path / "cache", tmp_path / "art")
    assert "needs update" in str(caught.value)


def test_the_gitleaks_mounts_come_from_the_scan_root(monkeypatch, tmp_path):
    """The other half of the same fix: `scan_secrets` must ALLOCATE under
    `scan_root`, not merely be handed one.

    A bare `tempfile.TemporaryDirectory()` ignores it and lands in
    `/var/folders/...`, which is the measured blindness -- so this pins the
    `-v` source paths rather than the constructor argument.
    """
    import bakeoff.grader as grader
    from bakeoff.grader import _ContainerEnv

    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        # Write the report where the container would, so the post-condition
        # sees a working mount.
        source = argv[argv.index("-v", argv.index("-v") + 1) + 1]
        Path(source.split(":")[0], "report.json").write_text("[]")
        return SimpleNamespace(returncode=0, stdout="", stderr="scanned ~9 bytes")

    monkeypatch.setattr(grader.subprocess, "run", fake_run)

    scan_root = tmp_path / "cache" / "grade-scan"
    env = _ContainerEnv(container=None, repo_path=tmp_path, scan_root=scan_root)
    result = env.scan_secrets({"src/calc.py": "x = 1"})

    mounts = [
        calls["argv"][i + 1].split(":")[0]
        for i, part in enumerate(calls["argv"]) if part == "-v"
    ]
    assert len(mounts) == 2
    assert all(scan_root in Path(m).parents for m in mounts)
    # The report is read on the HOST side of the mount, which is the evidence.
    assert result.report_present is True
    assert result.scanned_bytes == 9
    # And the scan dir -- which held the added lines -- is gone afterwards.
    assert not any(scan_root.glob("scan-*"))


def test_a_broken_mount_leaves_the_report_absent(monkeypatch, tmp_path):
    """What the measured failure looks like from the adapter's side."""
    import bakeoff.grader as grader
    from bakeoff.grader import _ContainerEnv

    monkeypatch.setattr(
        grader.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout="", stderr="scanned ~0 bytes (0) in 757µs"
        ),
    )
    env = _ContainerEnv(container=None, repo_path=tmp_path,
                        scan_root=tmp_path / "grade-scan")
    result = env.scan_secrets({"src/calc.py": 'K = "AKIAZ3XQWERTYUIOP12"'})

    assert result.report_present is False
    assert result.scanned_bytes == 0
    from bakeoff.grader import _scan_saw_input

    saw, _ = _scan_saw_input(result, {"src/calc.py": "x"})
    assert saw is False


def test_the_gitleaks_report_is_summarized_by_rule_and_file():
    from bakeoff.grader import _summarize_gitleaks

    report = (
        '[{"RuleID": "aws-access-token", "File": "/scan/src/calc.py", '
        '"StartLine": 3, "StartColumn": 7}]'
    )
    detail = _summarize_gitleaks(report)
    assert "aws-access-token" in detail
    assert "src/calc.py" in detail
    assert "/scan/" not in detail


def test_an_unreadable_gitleaks_report_still_names_the_finding():
    from bakeoff.grader import _summarize_gitleaks

    assert "could not be read" in _summarize_gitleaks("not json")


def test_chunk_parsing_runs_outside_any_repository():
    """`tasks._numstat` filters the patch to the cwd prefix inside a repo
    subdirectory and reports zero entries, exit 0, empty stderr."""
    from bakeoff.grader import _parse_submission

    parsed = _parse_submission(MIXED_DIFF)
    assert [dest for _, _, dest in parsed] == ["src/calc.py", "img.png"]

    # And it leaves nothing behind: the temp dir is context-managed, so a
    # grading batch over 2,400 records does not accumulate one per run.
    leftovers = list(Path(tempfile.gettempdir()).glob("bakeoff-grade-*"))
    assert leftovers == []
