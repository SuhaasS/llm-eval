"""The batch driver: one line per selected record, and nothing kills the batch.

Nothing here touches Docker. `grade_event_log` composes two seams -- `grade_one`
(the ladder, one run) and `resolve_env` (the per-task setup: image, preflight,
oracle) -- and every test injects both. The EVENT LOG is real, built in
`tmp_path` with real `RunRecord`s, because the thing being pinned is which
records get a line and in what order, and a fake log would let the driver read
the wrong ones and still pass.

The fake grader assembles through the REAL `build_grade_record`. The driver's
contract is that every line carries its provenance -- the manifest digest, the
task-set commit, the grader commit -- and a fake that stamped those itself would
prove the test's arithmetic rather than the composition's.

The exit contract is the property most of these tests are really about: exit 0
means every selected record produced a line. A task whose image will not build,
an oracle that cannot be derived, a container that dies inside preflight -- each
is one exception away from a batch that stops with half the collection ungraded
and no line to say which half.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff.container import ContainerError
from bakeoff.grade_schema import (
    CHECK_ORDER,
    CheckResult,
    GradeRecord,
    NotGradedReason,
    append_grade,
    load_grades,
)
from bakeoff.grader import (
    GRADER_VERSION,
    LadderResult,
    build_grade_record,
    not_graded_gate,
)
from bakeoff.images import ImageError
from bakeoff.oracle import Oracle, OracleError
from bakeoff.preflight import (
    PREFLIGHT_VERSION,
    SCOPE_COLLECTS_NOTHING,
    PreflightResult,
    preflight_cache_key,
)
from bakeoff.eventlog import EventLog
from bakeoff.schema import (
    Artifacts,
    Checkpoint,
    Exclusion,
    ExclusionClass,
    Outcome,
    RunRecord,
    TerminationReason,
    Versions,
)
from bakeoff.tasks import TaskError

from scripts.grade import (
    ResumeRefused,
    TaskSetup,
    VerdictInvariantError,
    artifacts_root,
    grade_event_log,
    grades_path,
    preflight_cached,
    record_preflight_pass,
    resolve_task,
)

IMAGE = "sha256:image"
DIFF = """diff --git a/src/calc.py b/src/calc.py
index 1111111..2222222 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _task(task_id="calc-1", task_version=3):
    return SimpleNamespace(
        task_id=task_id,
        task_version=task_version,
        manifest_digest=f"digest-{task_id}",
        task_set_commit="taskset-def",
        base_sha="b" * 40,
        declared_start_sha="",
        tests=SimpleNamespace(
            paths=("tests/",),
            runner=("python", "-m", "pytest", "-q"),
            f2p=("tests/test_calc.py::test_add",),
            p2p=(),
            allow_extra_paths=(),
        ),
        grading=SimpleNamespace(build=(), typecheck=(), lint=()),
    )


def _record(run_id="run-a", task_id="calc-1", task_version=3,
            model="claude-sonnet-5", image=IMAGE, **kw):
    """A well-formed record of a run that produced `DIFF`.

    The defaults describe the ordinary gradable case; every test overrides
    exactly the field it is about.
    """
    fields = dict(
        run_id=run_id,
        task_id=task_id,
        task_version=task_version,
        model=model,
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-17T00:00:00Z",
        finished_at="2026-08-17T00:05:00Z",
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=3,
        turns_streamed=3,
        collection_id="coll-1",
        checkpoints=[
            Checkpoint(turn=3, diff_vs_base=DIFF, files_touched=[],
                       elapsed_ms=0)
        ],
        artifacts=Artifacts(final_diff=DIFF),
        versions=Versions(container_image_digest=image),
    )
    fields.update(kw)
    return RunRecord(**fields)


def _log(tmp_path, *records) -> Path:
    """A real event log holding `records`."""
    root = tmp_path / "eventlog"
    log = EventLog(root)
    for record in records:
        log.write_run(record)
    return root


def _oracle(quarantined=()):
    return Oracle(fingerprint="fp", quarantined=tuple(quarantined),
                  oracle_version="1")


def _setup(image=IMAGE, oracle=None, refusal=None,
           preflight_version=PREFLIGHT_VERSION):
    return TaskSetup(
        image=image,
        preflight_version=preflight_version,
        oracle=_oracle() if oracle is None and refusal is None else oracle,
        refusal=refusal,
    )


class FakeGrader:
    """Stands in for `grade_run`, and honours the same gate it does.

    Records every `(record, task, image, oracle)` it was handed, so a test can
    assert what the DRIVER passed -- `oracle=None` for a gated record is a
    driver decision, and it is invisible in the resulting line.
    """

    def __init__(self, checks=(), resolved=True, environment_error_check=None,
                 raises=None):
        self.calls = []
        self.checks = tuple(checks)
        self.resolved = resolved
        self.environment_error_check = environment_error_check
        self.raises = raises

    def __call__(self, record, task, image, oracle, cache_root,
                 artifacts_root_):
        self.calls.append(
            SimpleNamespace(record=record, task=task, image=image,
                            oracle=oracle, cache_root=Path(cache_root),
                            artifacts_root=Path(artifacts_root_))
        )
        if self.raises is not None:
            raise self.raises
        gate = not_graded_gate(record)
        if gate is not None:
            reason, detail = gate
            ladder = LadderResult(
                checks=tuple(
                    CheckResult(name=n, status="skipped") for n in CHECK_ORDER
                ),
                resolved=None,
                not_graded_reason=reason.value,
                not_graded_detail=detail,
            )
        elif self.environment_error_check is not None:
            ladder = LadderResult(
                checks=self.checks,
                resolved=None,
                not_graded_reason=NotGradedReason.ENVIRONMENT_ERROR.value,
                not_graded_detail="the grading environment broke",
                environment_error="boom",
                environment_error_check=self.environment_error_check,
            )
        else:
            ladder = LadderResult(checks=self.checks, resolved=self.resolved)
        return build_grade_record(record, task, image, oracle, ladder)


def _lines(root: Path) -> list[GradeRecord]:
    records, malformed = load_grades(grades_path(root))
    assert malformed == 0
    return records


def _run(root, tasks, tmp_path, grade_one=None, resolve_env=None, **kw):
    grade_one = grade_one if grade_one is not None else FakeGrader()
    resolve_env = resolve_env if resolve_env is not None else (
        lambda task: _setup()
    )
    return grade_event_log(
        root, tasks, tmp_path / "cache",
        grade_one=grade_one, resolve_env=resolve_env, **kw
    )


# --------------------------------------------------------------------------
# 1. one line per selected record
# --------------------------------------------------------------------------


def test_every_selected_record_gets_exactly_one_line(tmp_path):
    """The exit contract, stated positively. Everything below is a way this
    stops being true without the batch saying so."""
    root = _log(tmp_path, _record("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path)

    assert len(result["graded"]) == 2
    assert result["not_graded"] == []
    assert result["errors"] == []
    assert sorted(g.run_id for g in _lines(root)) == ["run-a", "run-b"]


def test_the_batch_walks_runs_in_sorted_order(tmp_path):
    """`list_runs` globs, and glob order is nondeterministic. Sorted-by-run_id
    is arbitrary but DETERMINISTIC, which is what a resumable batch needs --
    a killed batch that resumes in a different order re-does different work."""
    root = _log(tmp_path, _record("run-c"), _record("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path)

    assert [g.run_id for g in result["graded"]] == ["run-a", "run-b", "run-c"]


def test_only_selects_the_named_runs(tmp_path):
    root = _log(tmp_path, _record("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path, only=["run-b"])

    assert [g.run_id for g in result["graded"]] == ["run-b"]


def test_every_line_names_the_inputs_it_was_derived_from(tmp_path):
    """A grade is a derived view and is worth nothing without its inputs. The
    manifest digest and task-set commit come from the LOADED manifest, and the
    preflight version from the verdict actually used."""
    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path)

    grade = result["graded"][0]
    assert grade.graded_against_manifest_digest == "digest-calc-1"
    assert grade.graded_against_task_set_commit == "taskset-def"
    assert grade.graded_under_preflight_version == PREFLIGHT_VERSION
    assert grade.grader_commit
    assert grade.grader_version == GRADER_VERSION
    assert grade.graded_in_image == IMAGE


# --------------------------------------------------------------------------
# 2. resume
# --------------------------------------------------------------------------


def test_a_second_pass_skips_what_the_first_already_graded(tmp_path):
    root = _log(tmp_path, _record("run-a"), _record("run-b"))
    _run(root, [_task()], tmp_path)

    second = _run(root, [_task()], tmp_path)

    assert second["graded"] == []
    assert sorted(second["skipped"]) == ["run-a", "run-b"]
    assert len(_lines(root)) == 2


def test_re_grade_appends_a_second_line_rather_than_replacing_the_first(
    tmp_path
):
    """The file is append-only for the reason the event log is: two verdicts
    disagreeing IS the finding, and an update would delete it."""
    root = _log(tmp_path, _record("run-a"))
    _run(root, [_task()], tmp_path)

    second = _run(root, [_task()], tmp_path, re_grade=True)

    assert len(second["graded"]) == 1
    assert [g.run_id for g in _lines(root)] == ["run-a", "run-a"]


def test_a_damaged_grades_file_refuses_to_resume(tmp_path):
    """A resume is keyed on WHICH runs are already graded, so it cannot afford
    to mistake an unreadable grade for an absent one -- it would re-grade a run
    that already has a line and leave the collection looking larger than it is.

    The count and the path are in the message because the remedy is an operator
    reading that file."""
    root = _log(tmp_path, _record("run-a"))
    path = grades_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json\n", encoding="utf-8")

    with pytest.raises(ResumeRefused) as exc:
        _run(root, [_task()], tmp_path)

    assert "1" in str(exc.value)
    assert str(path) in str(exc.value)


def test_a_resume_spanning_two_grader_commits_is_bannered(tmp_path):
    """`grader_version` gates the resume; `grader_commit` is the evidence the
    gate was honest. One version string over two working trees is exactly the
    case the version cannot see."""
    root = _log(tmp_path, _record("run-a"), _record("run-b"))
    append_grade(grades_path(root), GradeRecord(
        run_id="run-a", collection_id="coll-1", task_id="calc-1",
        model="claude-sonnet-5", record_schema_version="3.8.0",
        graded_at="2026-08-17T00:00:00Z", grader_version=GRADER_VERSION,
        grader_commit="deadbee-dirty", resolved=True,
    ))

    result = _run(root, [_task()], tmp_path)

    assert any("deadbee-dirty" in w for w in result["warnings"])


# --------------------------------------------------------------------------
# 3. the driver's own not-graded reasons
# --------------------------------------------------------------------------


def test_a_record_whose_task_is_gone_is_named_task_not_found(tmp_path):
    root = _log(tmp_path, _record("run-a", task_id="vanished"))

    result = _run(root, [_task()], tmp_path)

    assert result["graded"] == []
    assert result["errors"] == []
    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.TASK_NOT_FOUND.value
    assert "vanished" in line.not_graded_detail
    # Nothing was preflighted, so claiming a preflight version would be a
    # provenance that does not exist.
    assert line.graded_under_preflight_version == ""


def test_a_line_that_ran_in_no_image_claims_no_image_comparison(tmp_path):
    """`image_matches_run is False` says the grade ran somewhere else. A
    driver-level refusal ran NOWHERE -- no image was ever resolved -- and
    `stored_digest == ""` is `False`, so every one of these lines used to
    claim a mismatch: the summary counted them and the cross-image banner
    fired over zero real mismatches. `None` is "the comparison could not be
    made", and it has two causes, not one."""
    root = _log(tmp_path, _record("run-a", task_id="vanished"),
                _record("run-b", task_version=99))

    result = _run(root, [_task()], tmp_path)

    assert [g.image_matches_run for g in result["not_graded"]] == [None, None]
    assert result["summary"]["claude-sonnet-5"]["image_mismatch"] == 0
    assert result["warnings"] == []


def test_a_task_edited_since_the_run_is_named_task_version_mismatch(tmp_path):
    root = _log(tmp_path, _record("run-a", task_version=3))

    result = _run(root, [_task(task_version=4)], tmp_path)

    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.TASK_VERSION_MISMATCH.value
    assert "3" in line.not_graded_detail and "4" in line.not_graded_detail


def test_a_record_below_the_schema_floor_is_not_graded(tmp_path):
    root = _log(tmp_path, _record("run-a", schema_version="2.1.0"))

    result = _run(root, [_task()], tmp_path)

    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.RECORD_SCHEMA_TOO_OLD.value
    assert line.record_schema_version == "2.1.0"


def test_an_unparsable_record_schema_version_does_not_kill_the_batch(tmp_path):
    """`schema_at_least` RAISES on a version that is not dotted integers, on
    purpose: an unparsable version is not evidence about age. Contained per
    record -- uncaught it takes the whole batch down over one hand-edited
    field, and the detail carries the cause the reason cannot."""
    root = _log(tmp_path, _record("run-a", schema_version="3.8.0-rc1"),
                _record("run-b"))

    result = _run(root, [_task()], tmp_path)

    assert result["errors"] == []
    assert [g.run_id for g in result["graded"]] == ["run-b"]
    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.RECORD_SCHEMA_TOO_OLD.value
    assert "3.8.0-rc1" in line.not_graded_detail
    assert "could not be" in line.not_graded_detail


def _excluded(run_id="run-a", **kw):
    return _record(
        run_id,
        exclusion=Exclusion(cls=ExclusionClass.INFRA_FAILURE,
                            reason_code="api_5xx", pre_registered=True),
        **kw,
    )


def test_an_excluded_run_is_not_graded_and_no_oracle_is_consulted(tmp_path):
    """The gate is the ladder's, asked by the driver so a gated record costs
    no container -- and so its line carries `quarantined is None`. An oracle
    on that row would claim a quarantine was consulted on a run nobody graded,
    and `()` is the healthy-suite derivation."""
    root = _log(tmp_path, _excluded())
    grader = FakeGrader()

    result = _run(root, [_task()], tmp_path, grade_one=grader)

    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.EXCLUDED.value
    assert line.quarantined is None
    assert line.oracle_fingerprint is None
    assert grader.calls == []


def test_a_gated_task_never_pays_for_a_setup_it_cannot_use(tmp_path):
    """An image build, a preflight and TWO full suite runs for the oracle, to
    produce refusals that look at none of it. The gate is above the setup."""
    resolved = []

    def resolve(task):
        resolved.append(task.task_id)
        return _setup()

    root = _log(tmp_path, _excluded("run-a"), _excluded("run-b"))

    result = _run(root, [_task()], tmp_path, resolve_env=resolve)

    assert len(result["not_graded"]) == 2
    assert resolved == []


def test_an_excluded_record_keeps_its_reason_when_the_task_setup_also_failed(
    tmp_path
):
    """`EXCLUDED` is a RECORD-level refusal; `TASK_SETUP_FAILED` is a
    TASK-level one, and the design's section 4.2.2 pairing constraint branches
    on exactly that distinction -- a record-level refusal breaks one arm's
    pair, a task-level one drops the task for every arm. Resolving the setup
    first wrote the excluded row as `task_setup_failed`, which is the wrong
    authority AND the wrong blast radius."""
    def resolve(task):
        raise ImageError("the base image build failed")

    root = _log(tmp_path, _excluded("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path, resolve_env=resolve)

    by_run = {g.run_id: g for g in result["not_graded"]}
    assert by_run["run-a"].not_graded_reason == NotGradedReason.EXCLUDED.value
    assert (by_run["run-b"].not_graded_reason
            == NotGradedReason.TASK_SETUP_FAILED.value)


# --------------------------------------------------------------------------
# 4. per-task setup: every failure marks the task's records
# --------------------------------------------------------------------------


def test_a_preflight_no_go_marks_every_record_of_that_task(tmp_path):
    root = _log(tmp_path, _record("run-a"), _record("run-b"))
    refusal = (NotGradedReason.PREFLIGHT_FAILED, "f2p green at the start state")

    result = _run(root, [_task()], tmp_path,
                  resolve_env=lambda task: _setup(oracle=None, refusal=refusal))

    assert result["graded"] == []
    assert len(result["not_graded"]) == 2
    for line in result["not_graded"]:
        assert line.not_graded_reason == NotGradedReason.PREFLIGHT_FAILED.value
        assert "f2p green at the start state" in line.not_graded_detail


def test_a_scope_no_go_is_named_not_genericized(tmp_path):
    """A mis-scoped task and a task that fails its red-before assertion are
    different author errors with different remedies. The driver branches on
    the TYPED `problem_codes`, never on a prose prefix."""
    from scripts.grade import preflight_refusal

    result = preflight_refusal(PreflightResult(
        task_id="calc-1", task_version=3, start_sha="a" * 40, image=IMAGE,
        manifest_digest="digest",
        problems=("the scoped p2p run collected nothing",),
        problem_codes=(SCOPE_COLLECTS_NOTHING,),
        preflight_version=PREFLIGHT_VERSION,
    ))

    assert result[0] is NotGradedReason.SCOPE_COLLECTED_NOTHING


def test_an_ordinary_no_go_stays_preflight_failed(tmp_path):
    from scripts.grade import preflight_refusal

    result = preflight_refusal(PreflightResult(
        task_id="calc-1", task_version=3, start_sha="a" * 40, image=IMAGE,
        manifest_digest="digest",
        problems=("f2p is green at the start state", "the tree is dirty"),
        problem_codes=(),
        preflight_version=PREFLIGHT_VERSION,
    ))

    assert result[0] is NotGradedReason.PREFLIGHT_FAILED
    assert "f2p is green at the start state" in result[1]
    assert "the tree is dirty" in result[1]


def test_a_broken_oracle_names_its_cause_not_just_its_bucket(tmp_path):
    """`OracleError` is the loudest failure `oracle.py` can raise. In the
    generic per-record `errors` bucket a broken oracle is indistinguishable
    from a grader bug, and neither gets a line."""
    def resolve(task):
        raise OracleError("these tests failed in BOTH reference runs: t::a")

    root = _log(tmp_path, _record("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path, resolve_env=resolve)

    assert result["errors"] == []
    assert len(result["not_graded"]) == 2
    for line in result["not_graded"]:
        assert line.not_graded_reason == NotGradedReason.ORACLE_FAILED.value
        assert "BOTH reference runs" in line.not_graded_detail


def test_a_task_whose_image_will_not_build_marks_its_records_not_the_error_bucket(
    tmp_path
):
    """Uncaught, `ImageError` kills the batch and every record of the task is
    left with no line at all -- which breaks "exit 0 means every selected
    record produced a line" in both directions at once."""
    def resolve(task):
        raise ImageError("the base image build failed")

    root = _log(tmp_path, _record("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path, resolve_env=resolve)

    assert result["errors"] == []
    assert len(result["not_graded"]) == 2
    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.TASK_SETUP_FAILED.value
    assert "ImageError" in line.not_graded_detail
    assert "the base image build failed" in line.not_graded_detail


def test_a_container_failure_in_per_task_setup_marks_its_records_not_the_error_bucket(
    tmp_path
):
    """The setup region runs `preflight()` and `ensure_oracle`, which start
    containers -- ~20 execs each. A named-tuple catch re-arms the kill-the-batch
    door for every type nobody listed, and docker's own exceptions are not in
    anyone's list."""
    def resolve(task):
        raise ContainerError("exec failed: no such container")

    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path, resolve_env=resolve)

    assert result["errors"] == []
    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.TASK_SETUP_FAILED.value
    assert "ContainerError" in line.not_graded_detail


def test_a_task_error_in_per_task_setup_marks_its_records(tmp_path):
    def resolve(task):
        raise TaskError("materialize refused an existing destination")

    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path, resolve_env=resolve)

    line = result["not_graded"][0]
    assert line.not_graded_reason == NotGradedReason.TASK_SETUP_FAILED.value
    assert "TaskError" in line.not_graded_detail


def test_the_per_task_setup_runs_once_for_all_of_its_records(tmp_path):
    """Two suite runs for the oracle and a full preflight, per task, not per
    record. 80 tasks x 30 records is the difference between a grading pass an
    operator re-runs and one they do not."""
    seen = []

    def resolve(task):
        seen.append(task.task_id)
        return _setup()

    root = _log(tmp_path, _record("run-a"), _record("run-b"),
                _record("run-c", task_id="other"))

    _run(root, [_task(), _task("other")], tmp_path, resolve_env=resolve)

    assert sorted(seen) == ["calc-1", "other"]


def test_one_tasks_setup_failure_does_not_touch_another_task(tmp_path):
    def resolve(task):
        if task.task_id == "calc-1":
            raise ImageError("build failed")
        return _setup()

    root = _log(tmp_path, _record("run-a"), _record("run-b", task_id="other"))

    result = _run(root, [_task(), _task("other")], tmp_path, resolve_env=resolve)

    assert [g.run_id for g in result["graded"]] == ["run-b"]
    assert [g.run_id for g in result["not_graded"]] == ["run-a"]


# --------------------------------------------------------------------------
# 5. the image is recorded, not refused
# --------------------------------------------------------------------------


def test_a_rebuilt_image_is_recorded_not_refused(tmp_path):
    """`run_matrix` refuses a digest mismatch because it is about to SPEND;
    the grader spends nothing, and the task-image build is non-hermetic, so off
    this machine a rebuilt digest differs almost surely. A refusal would become
    routine `--allow-mixed-images` noise; the record is the point."""
    root = _log(tmp_path, _record("run-a", image="sha256:built-elsewhere"))

    result = _run(root, [_task()], tmp_path)

    grade = result["graded"][0]
    assert grade.image_matches_run is False
    assert grade.graded_in_image == IMAGE
    assert result["summary"]["claude-sonnet-5"]["image_mismatch"] == 1
    assert any("image" in w for w in result["warnings"])


def test_a_matching_image_raises_no_banner(tmp_path):
    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path)

    assert result["graded"][0].image_matches_run is True
    assert result["warnings"] == []


# --------------------------------------------------------------------------
# 6. per-record failure is contained
# --------------------------------------------------------------------------


def test_a_failure_grading_one_record_does_not_cost_the_rest(tmp_path):
    """Loud and counted, never swallowed: the errors bucket is what makes the
    exit code 1, and a caller that believed the batch was complete would
    compute a resolve rate over a collection with a hole in it."""
    calls = []
    good = FakeGrader()

    def grade_one(record, task, image, oracle, cache_root, artifacts):
        calls.append(record.run_id)
        if record.run_id == "run-a":
            raise RuntimeError("something the ladder did not expect")
        return good(record, task, image, oracle, cache_root, artifacts)

    root = _log(tmp_path, _record("run-a"), _record("run-b"))

    result = _run(root, [_task()], tmp_path, grade_one=grade_one)

    assert calls == ["run-a", "run-b"]
    assert [g.run_id for g in result["graded"]] == ["run-b"]
    assert len(result["errors"]) == 1
    assert "run-a" in result["errors"][0]
    assert "RuntimeError" in result["errors"][0]
    assert [g.run_id for g in _lines(root)] == ["run-b"]


def test_a_grade_that_violates_the_resolved_invariant_is_not_written(tmp_path):
    """`resolved is None` if and only if `not_graded_reason is not None`. A
    `False` beside a reason is an ACCUSATION stamped on a run the grader
    refused to look at, and a `None` without one is a verdict nobody can
    interpret. Either way it is a DRIVER bug, so it goes to the errors bucket
    and the line is never appended -- the file is append-only, so a bad line
    written once is permanent."""
    def grade_one(record, task, image, oracle, cache_root, artifacts):
        return build_grade_record(
            record, task, image, oracle,
            LadderResult(checks=(), resolved=False,
                         not_graded_reason=NotGradedReason.CRASHED.value,
                         not_graded_detail="both at once"),
        )

    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path, grade_one=grade_one)

    assert result["graded"] == [] and result["not_graded"] == []
    assert len(result["errors"]) == 1
    assert VerdictInvariantError.__name__ in result["errors"][0]
    assert _lines(root) == []


# --------------------------------------------------------------------------
# 7. the summary
# --------------------------------------------------------------------------


def test_the_summary_buckets_environment_errors_by_check(tmp_path):
    """A per-check histogram, not a count. One arm's bucket being ALL `p2p` is
    plausibly model-caused not-grading -- a section 6.4 exclusion under another
    name -- and a single number cannot show it."""
    root = _log(tmp_path, _record("run-a", model="gemma"),
                _record("run-b", model="gemma"))

    result = _run(root, [_task()], tmp_path,
                  grade_one=FakeGrader(environment_error_check="p2p"))

    assert result["summary"]["gemma"]["environment_error_check"] == {"p2p": 2}


def test_the_summary_counts_not_configured_checks(tmp_path):
    """`not_configured` is "this task declares no linter", `skipped` is "the
    linter was cut short". A column that folded them would report every task
    without a typechecker as a truncated ladder."""
    checks = (
        CheckResult(name="build", status="not_configured"),
        CheckResult(name="f2p", status="pass"),
    )
    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path, grade_one=FakeGrader(checks=checks))

    assert result["summary"]["claude-sonnet-5"]["not_configured"] == 1


def test_the_summary_is_per_model(tmp_path):
    root = _log(tmp_path, _record("run-a", model="gemma"),
                _record("run-b", model="kimi"))

    result = _run(root, [_task()], tmp_path)

    assert set(result["summary"]) == {"gemma", "kimi"}
    assert result["summary"]["gemma"]["resolved"] == 1
    assert result["summary"]["kimi"]["graded"] == 1


def test_the_summary_counts_not_graded_by_reason(tmp_path):
    root = _log(tmp_path, _record("run-a", task_id="vanished"))

    result = _run(root, [_task()], tmp_path)

    assert result["summary"]["claude-sonnet-5"]["not_graded"] == {
        NotGradedReason.TASK_NOT_FOUND.value: 1
    }


# --------------------------------------------------------------------------
# 8. paths and the preflight cache
# --------------------------------------------------------------------------


def test_the_grades_live_beside_the_log_they_never_touch(tmp_path):
    root = tmp_path / "eventlog"

    assert grades_path(root) == root / "grades" / "grades.jsonl"
    assert artifacts_root(root) == root / "grades" / "artifacts"


def test_the_artifacts_root_is_handed_to_the_grader(tmp_path):
    root = _log(tmp_path, _record("run-a"))
    grader = FakeGrader()

    _run(root, [_task()], tmp_path, grade_one=grader)

    assert grader.calls[0].artifacts_root == artifacts_root(root)
    assert grader.calls[0].cache_root == tmp_path / "cache"


def test_a_verdict_from_the_drivers_cache_is_served(tmp_path):
    """`run_matrix`'s `preflight.json` is read for hits and never written: its
    own `--force-preflight` path seeds `{}` and writes the whole file back, so
    a grader entry in there would erase every other task's verdict the next
    time the collection driver is forced."""
    cache = tmp_path / "cache"
    task, start_sha = _task(), "a" * 40

    cache.mkdir(parents=True)
    (cache / "preflight.json").write_text(json.dumps(
        {task.task_id: {"key": preflight_cache_key(task, IMAGE, start_sha)}}
    ))

    assert preflight_cached(task, IMAGE, start_sha, cache) is True


def test_a_verdict_from_an_older_gate_is_not_served(tmp_path):
    cache = tmp_path / "cache"
    task, start_sha = _task(), "a" * 40

    cache.mkdir(parents=True)
    (cache / "preflight.json").write_text(json.dumps(
        {task.task_id: {"key": f"{task.manifest_digest}|{IMAGE}|{start_sha}"}}
    ))

    assert preflight_cached(task, IMAGE, start_sha, cache) is False


def test_the_grader_writes_its_own_cache_and_not_the_drivers(tmp_path):
    cache = tmp_path / "cache"
    task, start_sha = _task(), "a" * 40
    cache.mkdir(parents=True)
    (cache / "preflight.json").write_text(json.dumps({"other": {"key": "k"}}))

    record_preflight_pass(task, IMAGE, start_sha, cache)

    assert preflight_cached(task, IMAGE, start_sha, cache) is True
    assert json.loads((cache / "preflight.json").read_text()) == {
        "other": {"key": "k"}
    }
    assert (cache / "preflight-grade.json").exists()


def test_an_unreadable_grader_cache_is_a_miss_not_a_crash(tmp_path):
    """The same posture `ensure_oracle` takes: a damaged verdict re-earns
    itself. Raising leaves the file on disk and the task ungradable until an
    operator deletes it by hand."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "preflight-grade.json").write_text("{not json")

    assert preflight_cached(_task(), IMAGE, "a" * 40, cache) is False


def test_the_grader_cache_write_leaves_no_half_written_file(tmp_path):
    """Whole-file rewrite plus `os.replace`. The file holds EVERY task's
    verdict, so a torn write does not lose one entry -- it loses the file, and
    the next pass re-preflights the whole collection."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)

    record_preflight_pass(_task("a"), IMAGE, "a" * 40, cache)
    record_preflight_pass(_task("b"), IMAGE, "b" * 40, cache)

    stored = json.loads((cache / "preflight-grade.json").read_text())
    assert sorted(stored) == ["a", "b"]
    assert list((cache).glob("*.tmp")) == []


# --------------------------------------------------------------------------
# 9. resolve_task, without a daemon
# --------------------------------------------------------------------------


def _stub_setup(monkeypatch, calls):
    """Everything `resolve_task` shells out to, replaced.

    The four are the whole Docker surface of the per-task setup: the image
    build, the ENTRYPOINT probe, the tree, and the two container-running
    gates. Stubbed together so the CACHE branch -- which is the only logic
    `resolve_task` has of its own -- is reachable offline.
    """
    import scripts.grade as grade

    monkeypatch.setattr(grade, "build_task_image",
                        lambda *a, **k: IMAGE)
    monkeypatch.setattr(grade, "image_entrypoint", lambda image: [])
    monkeypatch.setattr(grade, "materialize", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(grade, "ensure_oracle",
                        lambda *a, **k: _oracle())

    def fake_preflight(task, **kw):
        calls.append(task.task_id)
        return PreflightResult(
            task_id=task.task_id, task_version=task.task_version,
            start_sha=kw["start_sha"], image=kw["image"],
            manifest_digest=task.manifest_digest,
            preflight_version=PREFLIGHT_VERSION,
        )

    monkeypatch.setattr(grade, "preflight", fake_preflight)


def test_a_cached_pass_spares_the_task_a_second_preflight(monkeypatch,
                                                          tmp_path):
    calls = []
    _stub_setup(monkeypatch, calls)
    cache, task = tmp_path / "cache", _task()
    cache.mkdir(parents=True)
    (cache / "preflight.json").write_text(json.dumps(
        {task.task_id: {"key": preflight_cache_key(task, IMAGE, "a" * 40)}}
    ))

    setup = resolve_task(task, cache, base_image="sha256:base")

    assert calls == []
    assert setup.refusal is None
    # The module constant, and it is sound because the version is IN the key:
    # a hit can only have been written by this gate.
    assert setup.preflight_version == PREFLIGHT_VERSION


def test_force_preflight_re_earns_a_verdict_the_cache_already_holds(
    monkeypatch, tmp_path
):
    """The escape hatch for a gate that is warm and wrong -- an image rebuilt
    under the same digest, a task edited without a digest bump. Without it the
    only way to re-run the gate is to delete a file by hand."""
    calls = []
    _stub_setup(monkeypatch, calls)
    cache, task = tmp_path / "cache", _task()
    cache.mkdir(parents=True)
    (cache / "preflight.json").write_text(json.dumps(
        {task.task_id: {"key": preflight_cache_key(task, IMAGE, "a" * 40)}}
    ))

    setup = resolve_task(task, cache, base_image="sha256:base",
                         force_preflight=True)

    assert calls == [task.task_id]
    assert setup.refusal is None


def test_an_inherited_entrypoint_is_refused_before_anything_is_materialized(
    monkeypatch, tmp_path
):
    """`RunContainer`'s `sleep infinity` would become an argument to it and the
    container would exit immediately. Raised, so it joins the setup bucket
    rather than being a special case with its own reason."""
    import scripts.grade as grade

    calls = []
    _stub_setup(monkeypatch, calls)
    monkeypatch.setattr(grade, "image_entrypoint", lambda image: ["/entry.sh"])
    monkeypatch.setattr(grade, "materialize", lambda *a, **k: pytest.fail(
        "materialized a task whose image cannot host a container"
    ))

    with pytest.raises(TaskError) as exc:
        resolve_task(_task(), tmp_path / "cache", base_image="sha256:base")

    assert "ENTRYPOINT" in str(exc.value)


# --------------------------------------------------------------------------
# 10. saying what the batch could not do
# --------------------------------------------------------------------------


def test_only_naming_a_run_this_log_does_not_hold_is_said_out_loud(tmp_path):
    """Not an error -- the exit contract is about records that were selected
    AND exist. But silence reads as "graded, nothing to report", and the usual
    cause is an operator pointing at the wrong event log, where every id is
    missing and the batch reports a clean zero."""
    root = _log(tmp_path, _record("run-a"))

    result = _run(root, [_task()], tmp_path, only=["run-a", "run-typo"])

    assert [g.run_id for g in result["graded"]] == ["run-a"]
    assert result["errors"] == []
    assert any("run-typo" in w for w in result["warnings"])


def test_re_grade_over_a_damaged_file_says_what_it_could_not_read(tmp_path):
    """`--re-grade` skips nothing, so the damage cannot cause a wrong skip --
    which is why it is a warning here and a refusal otherwise. Said out loud
    because the grader_commit banner is computed off the same list, so a
    damaged file can silently stop THAT banner from firing."""
    root = _log(tmp_path, _record("run-a"))
    path = grades_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json\n", encoding="utf-8")

    result = _run(root, [_task()], tmp_path, re_grade=True)

    assert len(result["graded"]) == 1
    assert any("unreadable" in w for w in result["warnings"])
