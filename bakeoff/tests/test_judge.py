"""Leak tests for the judge's whitelist payload builders.

The spec asks for these by name (§ "Testing"): build a `RunRecord` with every
field populated, build a payload, and assert the model name, the arm name,
every cost field, every timing field and every artifact path are absent from
the serialized bytes -- plus a companion test that a field added TOMORROW is
absent too, which is the whole point of whitelisting over redaction.

Two things make these tests worth more than an eyeball over `judge.py`:

* The assertions are on `json.dumps` OUTPUT, not on the payload dict. A leak
  that arrives as a nested object, a dataclass that serializes lazily, or a
  path buried three levels down inside `artifacts` is invisible to a key-set
  check and plainly visible in the bytes that go to the model.
* `LEAK_SURFACE` is asserted BOTH ways -- every token must appear in the
  record's own serialization and must not appear in any payload. A sentinel
  sweep that only checks absence passes perfectly against a fixture that
  stopped populating the field, which is the failure mode where a leak test
  stops testing and nothing says so.

Some fields cannot carry a sentinel and are covered by the other half of the
guard, `test_payload_key_sets_are_closed`, which pins the payload to an exact
key set at every nesting level so no record field can arrive under a key that
is not in that set. Three groups:

* enums and bools -- `outcome`, `terminated_by`, `failure_class`,
  `exclusion.cls`, `isolated`, and `GradeRecord.resolved`, which is the prior
  verdict itself and is the one this matters most for;
* the two schema version strings, left real because a `GradeRecord` carrying
  an unparsable `record_schema_version` is a record `schema_at_least` could
  not read, and a fixture modelling an impossible record teaches the next
  reader the wrong shape;
* `artifacts.final_diff`, which is the one record field the judge is MEANT to
  see -- a sentinel there would have the sweep assert against its own payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from bakeoff.grade_schema import CheckResult, GradeRecord
from bakeoff.judge import (
    build_pairwise_payload,
    build_rubric_payload,
    payload_inputs_from,
)
from bakeoff.schema import (
    Artifacts,
    CacheState,
    Checkpoint,
    DestructiveCategory,
    DestructiveEvent,
    Exclusion,
    ExclusionClass,
    FailureClass,
    HostMetrics,
    Outcome,
    RunRecord,
    Severity,
    TerminationReason,
    TimingBreakdown,
    TokenUsage,
    ToolCallStats,
    TurnRecord,
    Versions,
)
# Underscored on import: pytest tries to COLLECT any module-level name starting
# `Test`, and warns that it cannot because the dataclass has a constructor.
from bakeoff.schema import TestResult as _TestResult
from bakeoff.tasks import TaskBudget, TaskImage, TaskManifest, TaskTests

# --- diffs -------------------------------------------------------------------

CANDIDATE_DIFF = (
    "diff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@ def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)

# The merged PR's fix half, and the anchor every payload is built against.
SOLUTION_DIFF = (
    "diff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@ def add(a, b):\n"
    "-    return a - b\n"
    "+    return operator.add(a, b)\n"
)

# The test half, which the agent was HANDED at the start state. It is the half
# that makes `TaskManifest.reference_diff` the wrong anchor.
TEST_HALF_DIFF = (
    "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
    "--- a/tests/test_calc.py\n"
    "+++ b/tests/test_calc.py\n"
    "@@ -1,2 +1,3 @@ def test_add():\n"
    "+    assert add(2, 2) == 4\n"
)

REFERENCE_DIFF = SOLUTION_DIFF + TEST_HALF_DIFF

# Both sides touch a changelog `allow_extra_paths` excluded from both halves.
CHANGELOG_CHUNK = (
    "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
    "--- a/CHANGELOG.md\n"
    "+++ b/CHANGELOG.md\n"
    "@@ -1,1 +1,2 @@ # Changelog\n"
    "+- fixed add\n"
)

# --- what must never reach the judge -----------------------------------------

#: Every leak-surface field, mapped to the token it serializes to. Asserted in
#: both directions: present in the record's own JSON (the fixture is honest)
#: and absent from every payload (the whitelist holds).
LEAK_SURFACE: dict[str, str] = {
    # Model identity -- the obvious half.
    "model": "SENTINEL_MODEL",
    "harness": "SENTINEL_HARNESS",
    "versions.bedrock_model_id": "SENTINEL_BEDROCK_MODEL_ID",
    "versions.claude_code": "SENTINEL_CLAUDE_CODE",
    "versions.litellm": "SENTINEL_LITELLM",
    "versions.litellm_patches": "SENTINEL_LITELLM_PATCH",
    "versions.litellm_proxy_version": "SENTINEL_PROXY_VERSION",
    "versions.container_image_digest": "SENTINEL_IMAGE_DIGEST",
    "versions.harness_commit": "SENTINEL_HARNESS_COMMIT",
    "versions.task_set_commit": "SENTINEL_TASK_SET_COMMIT",
    # Model identity -- the less obvious half. Sampling, the finish reasons and
    # the two prompt hashes fingerprint an arm as well as its name does.
    "sampling.temperature": "SENTINEL_TEMPERATURE",
    "sampling.top_p": "SENTINEL_TOP_P",
    "sampling.max_tokens": "SENTINEL_MAX_TOKENS",
    "sampling_source": "SENTINEL_SAMPLING_SOURCE",
    "finish_reasons": "SENTINEL_FINISH_REASON",
    "terminal_finish_reason": "SENTINEL_TERMINAL_FINISH_REASON",
    "system_prompt_sha": "SENTINEL_SYSTEM_PROMPT_SHA",
    "tool_schema_sha": "SENTINEL_TOOL_SCHEMA_SHA",
    "config_digest": "SENTINEL_CONFIG_DIGEST",
    "per_turn[].stop_reason": "SENTINEL_STOP_REASON",
    "per_turn[].bedrock_request_id": "SENTINEL_BEDROCK_REQUEST_ID",
    "per_turn[].api_message_id": "SENTINEL_API_MESSAGE_ID",
    # Cost. `cost_usd` and the cache signature identify arms almost as well as
    # names do -- Sonnet's cache_read profile, the candidates' null pricing.
    "cost_usd": "77777.77",
    "tokens.cache_read": "888888",
    "tokens.cache_write": "855855",
    "tokens.cache_write_5m": "844844",
    "tokens.cache_write_1h": "877877",
    "tokens.input": "811811",
    "tokens.output": "822822",
    "tokens.reasoning": "833833",
    "versions.pricing_basis": "SENTINEL_PRICING_BASIS",
    "pricing_error": "SENTINEL_PRICING_ERROR",
    "cache_state.prior_same_task_run_id": "SENTINEL_PRIOR_RUN_ID",
    "cache_state.seconds_since_prior_run": "66666.5",
    # Timing.
    "time.wall_clock_total_ms": "911911911",
    "time.inference_ms": "922922922",
    "time.tool_exec_ms": "933933933",
    "time.retry_backoff_ms": "944944944",
    "time.time_to_first_edit_ms": "955955955",
    "started_at": "SENTINEL_STARTED_AT",
    "finished_at": "SENTINEL_FINISHED_AT",
    # Artifact paths, every one of which embeds the arm in a directory
    # component. This is the leak a builder that copies `artifacts` verbatim
    # ships while looking like it copied no model name at all.
    "artifacts.trajectory_jsonl_gz": "/runs/SENTINEL_ARM/trajectory.jsonl.gz",
    "artifacts.wire_log_gz": "/runs/SENTINEL_ARM/wire.jsonl.gz",
    "artifacts.container_stdout": "/runs/SENTINEL_ARM/stdout.txt",
    "artifacts.container_stderr": "/runs/SENTINEL_ARM/stderr.txt",
    "artifacts.harness_traceback": "/runs/SENTINEL_ARM/traceback.txt",
    "artifacts.test_output_gz": "/runs/SENTINEL_ARM/tests.log.gz",
    # Identity and provenance the payload has no key for at all.
    "run_id": "SENTINEL_RUN_ID",
    "parent_run_id": "SENTINEL_PARENT_RUN_ID",
    "task_id": "SENTINEL_TASK_ID",
    "collection_id": "SENTINEL_COLLECTION_ID",
    "invocation_stamp": "SENTINEL_INVOCATION_STAMP",
    "schema_version": "SENTINEL_SCHEMA_VERSION",
    "isolation_evidence": "SENTINEL_ISOLATION_EVIDENCE",
    "diff_stats": "SENTINEL_DIFF_STAT",
    "p2p_regressions": "SENTINEL_P2P_REGRESSION",
    "exclusion.reason_code": "SENTINEL_EXCLUSION_REASON_CODE",
    "tool_calls.by_name": "SENTINEL_TOOL_NAME",
    "tool_calls.total": "515151",
    "host.error": "SENTINEL_HOST_ERROR",
    "host.samples": "525252",
    "host.mem_peak_mb": "535353",
    "host.cpu_pct_p95": "545454.5",
    "host.load_p95": "555555.5",
    "host.vm_cpus": "565656",
    "truncation_events": "SENTINEL_TRUNCATION_EVENT",
    "destructive_events[].command": "SENTINEL_DESTRUCTIVE_COMMAND",
    "destructive_events[].paths_touched": "SENTINEL_DESTRUCTIVE_PATH",
    # The per-turn diffs. §5.5's curve is computed over these, so a builder
    # that reached for "the agent's work" could plausibly grab them, and each
    # one is a full intermediate submission.
    "checkpoints[].diff_vs_base": "SENTINEL_CHECKPOINT_DIFF",
    "checkpoints[].files_touched": "SENTINEL_CHECKPOINT_FILE",
    "checkpoints[].per_test[].name": "SENTINEL_PER_TEST_NAME",
    "checkpoints[].per_test[].stdout_ref": "SENTINEL_PER_TEST_STDOUT_REF",
    # Every error field, each of which names the harness or the arm.
    "wire_log_error": "SENTINEL_WIRE_LOG_ERROR",
    "trajectory_parse_error": "SENTINEL_TRAJECTORY_PARSE_ERROR",
    "scanner_error": "SENTINEL_SCANNER_ERROR",
    "checkpoint_error": "SENTINEL_CHECKPOINT_ERROR",
    "crash_error": "SENTINEL_CRASH_ERROR",
    "finalize_error": "SENTINEL_FINALIZE_ERROR",
    "assembly_error": "SENTINEL_ASSEMBLY_ERROR",
    # Counts. Turn and token counts are effort signatures.
    "turns_used": "313131",
    "assistant_records": "323232",
    "turns_streamed": "333333",
    "wire_entries_seen": "343434",
    "wire_entries_distinct": "353535",
    "wire_unattributed": "363636",
    "agent_exit_code": "373737",
    "transcript_malformed_lines": "383838",
    "stdout_malformed_lines": "393939",
    "wire_malformed_lines": "404040",
    "sample_index": "464646",
    "attempt_number": "474747",
    "task_version": "787878",
}

#: The same, for the grade. A prior verdict turns three independent votes into
#: one vote and two confirmations, and the check output paths embed the arm.
GRADE_LEAK_SURFACE: dict[str, str] = {
    "grade.model": "SENTINEL_GRADE_MODEL",
    "grade.run_id": "SENTINEL_GRADE_RUN_ID",
    "grade.grader_version": "SENTINEL_GRADER_VERSION",
    "grade.grader_commit": "SENTINEL_GRADER_COMMIT",
    "grade.graded_at": "SENTINEL_GRADED_AT",
    "grade.graded_in_image": "SENTINEL_GRADED_IN_IMAGE",
    "grade.oracle_fingerprint": "SENTINEL_ORACLE_FINGERPRINT",
    "grade.grade_failure": "SENTINEL_GRADE_FAILURE",
    "grade.not_graded_reason": "SENTINEL_NOT_GRADED_REASON",
    "grade.not_graded_detail": "SENTINEL_NOT_GRADED_DETAIL",
    "grade.exclusion_class": "SENTINEL_EXCLUSION_CLASS",
    "grade.crash_error": "SENTINEL_GRADE_CRASH_ERROR",
    "grade.assembly_error": "SENTINEL_GRADE_ASSEMBLY_ERROR",
    "grade.environment_error": "SENTINEL_ENVIRONMENT_ERROR",
    "grade.environment_error_check": "SENTINEL_ENVIRONMENT_ERROR_CHECK",
    "grade.oracle_version": "SENTINEL_ORACLE_VERSION",
    "grade.graded_under_preflight_version": "SENTINEL_PREFLIGHT_VERSION",
    "grade.graded_against_manifest_digest": "SENTINEL_MANIFEST_DIGEST",
    "grade.graded_against_task_set_commit": "SENTINEL_GRADE_TASK_SET_COMMIT",
    "grade.binary_chunks_dropped": "SENTINEL_BINARY_CHUNK",
    "grade.f2p_declared": "616161",
    "grade.p2p_quarantine_requested": "626262",
    "grade.p2p_deselect_requested": "636363",
    "grade.p2p_deselected": "646464",
    "grade.artifacts_dir": "/runs/SENTINEL_ARM/grade",
    "grade.f2p_failed_node_ids": "SENTINEL_F2P_NODE",
    "grade.p2p_failed_node_ids": "SENTINEL_P2P_NODE",
    "grade.quarantined": "SENTINEL_QUARANTINED_NODE",
    "grade.checks[].output_path": "/runs/SENTINEL_ARM/checks/f2p.log",
    "grade.checks[].detail": "SENTINEL_CHECK_DETAIL",
    "grade.checks[].duration_s": "6543.21",
    "grade.checks[].exit_code": "98765",
}


# --- fixtures ----------------------------------------------------------------


def _record(**kw) -> RunRecord:
    """A run record with a sentinel in every leak-surface field.

    `artifacts.final_diff` is left as real diff text -- it is the one field
    the judge is meant to see, and a sentinel there would make the sweep
    assert against its own payload.
    """
    fields = dict(
        run_id="SENTINEL_RUN_ID",
        parent_run_id="SENTINEL_PARENT_RUN_ID",
        task_id="SENTINEL_TASK_ID",
        task_version=787878,
        model="SENTINEL_MODEL",
        harness="SENTINEL_HARNESS",
        sample_index=464646,
        attempt_number=474747,
        started_at="SENTINEL_STARTED_AT",
        finished_at="SENTINEL_FINISHED_AT",
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        failure_class=FailureClass.WRONG_BUT_CONFIDENT,
        exclusion=Exclusion(
            cls=ExclusionClass.INFRA_FAILURE,
            reason_code="SENTINEL_EXCLUSION_REASON_CODE",
            pre_registered=True,
        ),
        schema_version="SENTINEL_SCHEMA_VERSION",
        turns_used=313131,
        assistant_records=323232,
        turns_streamed=333333,
        collection_id="SENTINEL_COLLECTION_ID",
        invocation_stamp="SENTINEL_INVOCATION_STAMP",
        config_digest="SENTINEL_CONFIG_DIGEST",
        system_prompt_sha="SENTINEL_SYSTEM_PROMPT_SHA",
        tool_schema_sha="SENTINEL_TOOL_SCHEMA_SHA",
        sampling={
            "temperature": "SENTINEL_TEMPERATURE",
            "top_p": "SENTINEL_TOP_P",
            "max_tokens": "SENTINEL_MAX_TOKENS",
        },
        sampling_source="SENTINEL_SAMPLING_SOURCE",
        wire_entries_seen=343434,
        wire_entries_distinct=353535,
        wire_unattributed=363636,
        finish_reasons={"SENTINEL_FINISH_REASON": 2},
        terminal_finish_reason="SENTINEL_TERMINAL_FINISH_REASON",
        wire_log_error="SENTINEL_WIRE_LOG_ERROR",
        versions=Versions(
            claude_code="SENTINEL_CLAUDE_CODE",
            litellm="SENTINEL_LITELLM",
            bedrock_model_id="SENTINEL_BEDROCK_MODEL_ID",
            container_image_digest="SENTINEL_IMAGE_DIGEST",
            harness_commit="SENTINEL_HARNESS_COMMIT",
            task_set_commit="SENTINEL_TASK_SET_COMMIT",
            litellm_patches=["SENTINEL_LITELLM_PATCH"],
            litellm_proxy_version="SENTINEL_PROXY_VERSION",
            pricing_basis="SENTINEL_PRICING_BASIS",
        ),
        time=TimingBreakdown(
            wall_clock_total_ms=911911911,
            inference_ms=922922922,
            tool_exec_ms=933933933,
            retry_backoff_ms=944944944,
            time_to_first_edit_ms=955955955,
        ),
        tokens=TokenUsage(
            input=811811,
            output=822822,
            reasoning=833833,
            cache_read=888888,
            cache_write=855855,
            cache_write_5m=844844,
            cache_write_1h=877877,
        ),
        cost_usd=77777.77,
        cache_state=CacheState(
            warm=True,
            prior_same_task_run_id="SENTINEL_PRIOR_RUN_ID",
            seconds_since_prior_run=66666.5,
        ),
        per_turn=[
            TurnRecord(
                turn=1,
                tokens=TokenUsage(cache_read=888888),
                cost_usd=77777.77,
                inference_ms=922922922,
                tool_exec_ms=933933933,
                stop_reason="SENTINEL_STOP_REASON",
                bedrock_request_id="SENTINEL_BEDROCK_REQUEST_ID",
                api_message_id="SENTINEL_API_MESSAGE_ID",
            )
        ],
        checkpoints=[
            Checkpoint(
                turn=1,
                diff_vs_base="SENTINEL_CHECKPOINT_DIFF",
                files_touched=["SENTINEL_CHECKPOINT_FILE"],
                elapsed_ms=922922922,
                tests_pass=False,
                per_test=[
                    _TestResult(
                        name="SENTINEL_PER_TEST_NAME",
                        status="fail",
                        duration_ms=933933933,
                        stdout_ref="SENTINEL_PER_TEST_STDOUT_REF",
                    )
                ],
            )
        ],
        tool_calls=ToolCallStats(
            total=515151,
            malformed=515151,
            errored=515151,
            api_calls_failed=515151,
            by_name={"SENTINEL_TOOL_NAME": 1},
        ),
        truncation_events=[{"reason": "SENTINEL_TRUNCATION_EVENT"}],
        destructive_events=[
            DestructiveEvent(
                turn=1,
                command="SENTINEL_DESTRUCTIVE_COMMAND",
                paths_touched=["SENTINEL_DESTRUCTIVE_PATH"],
                category=DestructiveCategory.TEST_DELETION,
                reverted_by_agent=False,
                affected_outcome=True,
                severity=Severity.HIGH,
            )
        ],
        host=HostMetrics(
            cpu_pct_p95=545454.5,
            mem_peak_mb=535353,
            contention_flag=True,
            load_p95=555555.5,
            vm_cpus=565656,
            samples=525252,
            error="SENTINEL_HOST_ERROR",
        ),
        isolation_evidence="SENTINEL_ISOLATION_EVIDENCE",
        trajectory_parse_error="SENTINEL_TRAJECTORY_PARSE_ERROR",
        pricing_error="SENTINEL_PRICING_ERROR",
        scanner_error="SENTINEL_SCANNER_ERROR",
        checkpoint_error="SENTINEL_CHECKPOINT_ERROR",
        crash_error="SENTINEL_CRASH_ERROR",
        agent_exit_code=373737,
        transcript_malformed_lines=383838,
        stdout_malformed_lines=393939,
        wire_malformed_lines=404040,
        finalize_error="SENTINEL_FINALIZE_ERROR",
        assembly_error="SENTINEL_ASSEMBLY_ERROR",
        diff_stats={"SENTINEL_DIFF_STAT": 1},
        p2p_regressions=["SENTINEL_P2P_REGRESSION"],
        artifacts=Artifacts(
            trajectory_jsonl_gz="/runs/SENTINEL_ARM/trajectory.jsonl.gz",
            wire_log_gz="/runs/SENTINEL_ARM/wire.jsonl.gz",
            container_stdout="/runs/SENTINEL_ARM/stdout.txt",
            container_stderr="/runs/SENTINEL_ARM/stderr.txt",
            harness_traceback="/runs/SENTINEL_ARM/traceback.txt",
            test_output_gz="/runs/SENTINEL_ARM/tests.log.gz",
            final_diff=CANDIDATE_DIFF,
        ),
    )
    fields.update(kw)
    return RunRecord(**fields)


def _grade(**kw) -> GradeRecord:
    """A grade whose every field but the check names and statuses is a sentinel.

    `checks[].name` and `checks[].status` are real, because those two are the
    whole of what the judge is allowed to see about the deterministic gate.
    """
    fields = dict(
        run_id="SENTINEL_GRADE_RUN_ID",
        collection_id="SENTINEL_COLLECTION_ID",
        task_id="SENTINEL_TASK_ID",
        model="SENTINEL_GRADE_MODEL",
        record_schema_version="3.8.0",
        graded_at="SENTINEL_GRADED_AT",
        grader_version="SENTINEL_GRADER_VERSION",
        grader_commit="SENTINEL_GRADER_COMMIT",
        graded_under_preflight_version="SENTINEL_PREFLIGHT_VERSION",
        graded_in_image="SENTINEL_GRADED_IN_IMAGE",
        graded_against_manifest_digest="SENTINEL_MANIFEST_DIGEST",
        graded_against_task_set_commit="SENTINEL_GRADE_TASK_SET_COMMIT",
        oracle_fingerprint="SENTINEL_ORACLE_FINGERPRINT",
        oracle_version="SENTINEL_ORACLE_VERSION",
        quarantined=("SENTINEL_QUARANTINED_NODE",),
        grade_failure="SENTINEL_GRADE_FAILURE",
        not_graded_reason="SENTINEL_NOT_GRADED_REASON",
        not_graded_detail="SENTINEL_NOT_GRADED_DETAIL",
        exclusion_class="SENTINEL_EXCLUSION_CLASS",
        crash_error="SENTINEL_GRADE_CRASH_ERROR",
        assembly_error="SENTINEL_GRADE_ASSEMBLY_ERROR",
        environment_error="SENTINEL_ENVIRONMENT_ERROR",
        environment_error_check="SENTINEL_ENVIRONMENT_ERROR_CHECK",
        agent_modified_tests=True,
        binary_chunks_dropped=("SENTINEL_BINARY_CHUNK",),
        f2p_declared=616161,
        f2p_failed_node_ids=("SENTINEL_F2P_NODE",),
        p2p_quarantine_requested=626262,
        p2p_deselect_requested=636363,
        p2p_deselected=646464,
        p2p_failed_node_ids=("SENTINEL_P2P_NODE",),
        artifacts_dir="/runs/SENTINEL_ARM/grade",
        checks=(
            CheckResult(
                name="f2p",
                status="pass",
                exit_code=98765,
                duration_s=6543.21,
                timed_out=True,
                output_path="/runs/SENTINEL_ARM/checks/f2p.log",
                detail="SENTINEL_CHECK_DETAIL",
            ),
            CheckResult(name="lint", status="not_configured"),
        ),
    )
    fields.update(kw)
    return GradeRecord(**fields)


def _task(**kw) -> TaskManifest:
    fields = dict(
        task_id="calc-1",
        task_version=3,
        tier="tier-1",
        stratum="bugfix",
        repo_url="https://example.invalid/calc.git",
        base_sha="0" * 40,
        prompt="Fix the addition bug in calc.py.",
        tests=TaskTests(
            paths=("tests/",), runner=("pytest",), f2p=("tests/test_calc.py",)
        ),
        image=TaskImage(),
        budget=TaskBudget(),
        reference_diff=REFERENCE_DIFF,
        test_diff=TEST_HALF_DIFF,
        solution_diff=SOLUTION_DIFF,
        test_files=("tests/test_calc.py",),
        root=Path("/tasks/calc-1"),
    )
    fields.update(kw)
    return TaskManifest(**fields)


def _payloads(record=None, grade=None, task=None):
    """Both payload kinds over one set of inputs, as serialized bytes.

    The pairwise side is fed the same inputs twice on purpose: both slots are
    then swept, so a leak that only reaches `submission_second` is caught.
    """
    inputs = payload_inputs_from(
        record if record is not None else _record(),
        grade if grade is not None else _grade(),
        task if task is not None else _task(),
    )
    return (
        json.dumps(build_rubric_payload(inputs)),
        json.dumps(build_pairwise_payload(inputs, inputs)),
    )


# --- the leak tests ----------------------------------------------------------


def test_no_run_record_leak_surface_field_reaches_the_serialized_payload():
    record, grade, task = _record(), _grade(), _task()
    record_blob = json.dumps(record.to_dict()) + json.dumps(grade.to_dict())
    surface = {**LEAK_SURFACE, **GRADE_LEAK_SURFACE}

    # The fixture is honest: every token this test claims to guard is really
    # in the record. Without this half, a field the fixture stopped setting
    # would pass the sweep silently and the leak test would test nothing.
    for name, token in surface.items():
        assert token in record_blob, f"fixture does not populate {name}"

    rubric, pairwise = _payloads(record, grade, task)
    for name, token in surface.items():
        assert token not in rubric, f"{name} leaked into the rubric payload"
        assert token not in pairwise, f"{name} leaked into the pairwise payload"


def test_a_run_record_field_added_tomorrow_is_absent_from_the_payload_by_default():  # noqa: E501
    @dataclass(frozen=True)
    class _WiderRunRecord(RunRecord):
        tomorrow_field: str = "SENTINEL_NEW"

    wider = _WiderRunRecord(
        **{
            f: getattr(_record(), f)
            for f in (
                "run_id",
                "task_id",
                "task_version",
                "model",
                "harness",
                "sample_index",
                "started_at",
                "finished_at",
                "outcome",
                "terminated_by",
                "turns_used",
                "artifacts",
            )
        }
    )
    assert "SENTINEL_NEW" in json.dumps(wider.to_dict())

    rubric, pairwise = _payloads(record=wider)
    assert "SENTINEL_NEW" not in rubric
    assert "SENTINEL_NEW" not in pairwise


def test_payload_key_sets_are_closed():
    inputs = payload_inputs_from(_record(), _grade(), _task())

    rubric = build_rubric_payload(inputs)
    assert set(rubric) == {
        "kind",
        "task_prompt",
        "reference_diff",
        "candidate_diff",
        "checks",
        "similarity",
    }
    assert rubric["kind"] == "rubric"
    _assert_similarity_keys(rubric["similarity"])
    for check in rubric["checks"]:
        assert set(check) == {"name", "status"}

    pairwise = build_pairwise_payload(inputs, inputs)
    assert set(pairwise) == {
        "kind",
        "task_prompt",
        "reference_diff",
        "submission_first",
        "submission_second",
    }
    assert pairwise["kind"] == "pairwise"
    for slot in ("submission_first", "submission_second"):
        submission = pairwise[slot]
        assert set(submission) == {"diff", "checks", "similarity"}
        _assert_similarity_keys(submission["similarity"])
        for check in submission["checks"]:
            assert set(check) == {"name", "status"}


def _assert_similarity_keys(similarity: dict) -> None:
    assert set(similarity) == {
        "file_overlap",
        "symbol_overlap",
        "diff_size_ratio",
    }
    for key in ("file_overlap", "symbol_overlap"):
        assert set(similarity[key]) == {
            "common",
            "candidate_only",
            "reference_only",
        }


def test_check_output_path_detail_and_timings_never_enter_the_payload():
    inputs = payload_inputs_from(_record(), _grade(), _task())

    assert inputs.checks == (
        {"name": "f2p", "status": "pass"},
        {"name": "lint", "status": "not_configured"},
    )
    for check in build_rubric_payload(inputs)["checks"]:
        assert set(check) == {"name", "status"}


def test_reference_anchor_is_the_solution_diff_not_the_reference_diff():
    """The anchor is `solution_diff`, and `reference_diff` is the answer key.

    `reference_diff` is the merged PR whole: fix half PLUS test half. The
    agent was handed the test half in its start state, so anchoring on the
    whole both leaks the oracle for the `wrote_tests` rubric flag and compares
    the submission against a diff it was never asked to produce.
    """
    inputs = payload_inputs_from(_record(), _grade(), _task())

    assert inputs.reference_diff == SOLUTION_DIFF
    assert inputs.reference_diff != REFERENCE_DIFF

    rubric, pairwise = _payloads()
    assert "tests/test_calc.py" not in rubric
    assert "tests/test_calc.py" not in pairwise


def test_a_none_final_diff_raises_rather_than_building_an_empty_payload():
    record = _record(artifacts=Artifacts(final_diff=None))
    with pytest.raises(ValueError, match="final_diff"):
        payload_inputs_from(record, _grade(), _task())


def test_extra_files_are_dropped_from_the_similarity_context():
    """`TaskManifest.extra_files` reaches `similarity_context` as `drop_paths`.

    A changelog both sides touched otherwise inflates the file overlap with
    work neither side was asked to do.
    """
    record = _record(
        artifacts=Artifacts(final_diff=CANDIDATE_DIFF + CHANGELOG_CHUNK)
    )
    task = _task(
        solution_diff=SOLUTION_DIFF + CHANGELOG_CHUNK,
        extra_files=("CHANGELOG.md",),
    )

    inputs = payload_inputs_from(record, _grade(), task)
    overlap = inputs.similarity.file_overlap
    assert overlap.common == ("calc.py",)
    # Not merely out of `common` -- out of the context entirely, on both sides.
    assert "CHANGELOG.md" not in overlap.candidate_only
    assert "CHANGELOG.md" not in overlap.reference_only


def test_pairwise_shows_the_two_submissions_in_the_order_given():
    """First and second are SHOWN order -- position is the caller's business.

    Randomizing here would put the assignment somewhere `position_assignment`
    could not record it, and a stored assignment that does not match what was
    sent makes the position-swap probe measure nothing.
    """
    first = payload_inputs_from(_record(), _grade(), _task())
    second = payload_inputs_from(
        _record(artifacts=Artifacts(final_diff=SOLUTION_DIFF)),
        _grade(),
        _task(),
    )

    pairwise = build_pairwise_payload(first, second)
    assert pairwise["submission_first"]["diff"] == CANDIDATE_DIFF
    assert pairwise["submission_second"]["diff"] == SOLUTION_DIFF

    swapped = build_pairwise_payload(second, first)
    assert swapped["submission_first"]["diff"] == SOLUTION_DIFF
    assert swapped["submission_second"]["diff"] == CANDIDATE_DIFF
