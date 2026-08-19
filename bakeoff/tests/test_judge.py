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

import ast
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

import pytest

import bakeoff.judge
from bakeoff.grade_schema import CheckResult, GradeRecord
from bakeoff.judge import (
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    RUBRIC_DIMENSIONS,
    RUBRIC_FLAGS,
    RUBRIC_VERSION,
    MalformedVerdict,
    PayloadInputs,
    build_pairwise_payload,
    build_rubric_payload,
    judge_pair_vote,
    judge_rubric,
    majority,
    parse_pairwise_response,
    parse_rubric_response,
    payload_inputs_from,
    prompt_sha,
    render_pairwise_prompt,
    render_rubric_prompt,
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


def test_pairing_submissions_from_different_tasks_raises():
    """§4.2.3 pairs sample *i* against sample *i* ON THE SAME TASK.

    The builder takes the prompt and the anchor from `first` and would
    otherwise discard `second`'s silently, so a driver that mispaired would
    produce a well-formed, confident verdict comparing one task's prompt
    against another task's submission -- and nothing in the stored
    `JudgeRecord` would say so.
    """
    first = payload_inputs_from(_record(), _grade(), _task())

    other_task = payload_inputs_from(
        _record(),
        _grade(),
        _task(prompt="Fix the subtraction bug in math_utils.py."),
    )
    with pytest.raises(ValueError, match="same task"):
        build_pairwise_payload(first, other_task)

    # Same task, drifted manifest -- a re-harvest or a mid-collection edit to
    # `task.yaml`. The anchors differ, so the two submissions were graded
    # against different references and the comparison is not blind.
    drifted_anchor = payload_inputs_from(
        _record(),
        _grade(),
        _task(solution_diff=SOLUTION_DIFF + CHANGELOG_CHUNK),
    )
    with pytest.raises(ValueError, match="same task"):
        build_pairwise_payload(first, drifted_anchor)


# --- the structural guarantee ------------------------------------------------

#: What a Task 4 helper that reached back into a record would look like. The
#: detector is asserted against this, because a source scan that matches
#: nothing passes just as quietly on a clean module as on a leaking one.
TASK_4_STYLE_VIOLATIONS = '''
def render_vote_prompt(record: RunRecord, inputs: PayloadInputs) -> str:
    """Annotated -- the shape a type-aware reader would catch."""
    return f"Arm {record.model} submitted:\\n{inputs.candidate_diff}"


def _vote_label(record, grade) -> str:
    """Unannotated -- the shape only a name-based scan catches."""
    return record.run_id + grade.grader_version
'''


def _record_touchers(source: str) -> dict[str, list[str]]:
    """Functions that read an attribute off a `RunRecord` or `GradeRecord`.

    A name is treated as record-bound when its annotation mentions either
    class, OR when it is conventionally named -- `record`, `grade`,
    `run_record`, `grade_record`. The second half is not redundant: an
    unannotated helper is exactly the shape that slips past a reader looking
    for types, and this is a tripwire rather than a type checker.

    Returns `{function name: [attribute chains it read]}`.
    """
    conventional = {"record", "grade", "run_record", "grade_record"}
    found: dict[str, list[str]] = {}

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        args = node.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        params += [a for a in (args.vararg, args.kwarg) if a is not None]

        bound = {
            arg.arg
            for arg in params
            if arg.arg in conventional
            or (
                arg.annotation is not None
                and any(
                    cls in ast.unparse(arg.annotation)
                    for cls in ("RunRecord", "GradeRecord")
                )
            )
        }
        if not bound:
            continue

        reads = [
            f"{inner.value.id}.{inner.attr}"
            for inner in ast.walk(node)
            if isinstance(inner, ast.Attribute)
            and isinstance(inner.value, ast.Name)
            and inner.value.id in bound
        ]
        # A record-typed parameter with no attribute read is still a toucher:
        # passing the whole record on to a helper is the same leak one call
        # deeper, and the helper may live in another module.
        found[node.name] = sorted(set(reads))

    return found


def test_only_payload_inputs_from_touches_a_run_record_or_a_grade_record():
    """The whitelist's structure, as a regression guard rather than a review.

    The entire leak argument rests on one structural claim: the builders never
    see a record, so they cannot leak a record field. The sentinel sweep does
    not test that claim -- it exercises the two builders as they are written
    today, and a helper added tomorrow that reads `record.model` to label a
    vote would pass every other test in this file.

    Task 4 extends THIS module with prompts, parsing and vote handling, which
    is exactly when the claim is most likely to be broken by someone who never
    read the docstring asserting it.
    """
    # The detector works: a clean scan of a leaking module would be a test
    # that passes because it matches nothing.
    assert set(_record_touchers(TASK_4_STYLE_VIOLATIONS)) == {
        "render_vote_prompt",
        "_vote_label",
    }

    source = Path(bakeoff.judge.__file__).read_text(encoding="utf-8")
    assert set(_record_touchers(source)) == {"payload_inputs_from"}


# --- prompts, parsing and the vote protocol ----------------------------------

#: Five DISTINCT values, so a parser that returned a constant profile -- or
#: zipped the dimension names against the wrong order -- fails rather than
#: agreeing with itself.
GOOD_SCORES: dict[str, int] = {
    "functional_equivalence": 2,
    "completeness": 1,
    "cross_file_consistency": 2,
    "scope_discipline": 0,
    "convention_adherence": 1,
}

#: Mixed, for the same reason.
GOOD_FLAGS: dict[str, bool] = {
    "introduced_stub": False,
    "left_debug_artifacts": True,
    "wrote_tests": False,
}

GOOD_REASONING = "Different code, same result: operator.add for a + b."


def _rubric_json(**overrides) -> str:
    body: dict = {
        "dimension_scores": dict(GOOD_SCORES),
        "flags": dict(GOOD_FLAGS),
        "reasoning": GOOD_REASONING,
    }
    body.update(overrides)
    return json.dumps(body)


def _pairwise_json(verdict: str = "A", **overrides) -> str:
    body: dict = {"verdict": verdict, "reasoning": GOOD_REASONING}
    body.update(overrides)
    return json.dumps(body)


class _FakeComplete:
    """A scripted `CompleteFn`. Records every prompt it was handed.

    Raises rather than repeating the last response once the queue is empty: a
    fake that keeps answering hides a retry loop that ran more times than the
    test claims, which is the exact fact the retry tests assert on.
    """

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError(
                f"complete() called {len(self.prompts)} times, "
                "more often than the test scripted"
            )
        return self._responses.pop(0)


def _inputs(**kw) -> PayloadInputs:
    """One submission's payload inputs. `kw` goes to the `RunRecord`."""
    return payload_inputs_from(_record(**kw), _grade(), _task())


def _other_inputs() -> PayloadInputs:
    """A second, distinguishable submission on the SAME task."""
    return _inputs(artifacts=Artifacts(final_diff=SOLUTION_DIFF))


# --- parsing -----------------------------------------------------------------


def test_three_point_scores_reject_booleans_and_out_of_range_ints():
    """`isinstance(True, int)` is Python's trap, and it is a real one here.

    A model that answers `true` for a dimension is not answering the question,
    but `True == 1` and `isinstance(True, int)` both hold -- so an
    `isinstance` check silently records a boolean as a partial score, and the
    stored profile reads exactly like an honest 1.
    """
    good = parse_rubric_response(_rubric_json())
    assert good.dimension_scores == GOOD_SCORES
    assert good.flags == GOOD_FLAGS
    assert good.reasoning == GOOD_REASONING

    for bad in (True, False, 3, -1, 1.0, "2", None):
        scores = {**GOOD_SCORES, "completeness": bad}
        with pytest.raises(MalformedVerdict):
            parse_rubric_response(_rubric_json(dimension_scores=scores))

    # The whole in-range integer scale still parses.
    for value in (0, 1, 2):
        scores = {**GOOD_SCORES, "completeness": value}
        parsed = parse_rubric_response(_rubric_json(dimension_scores=scores))
        assert parsed.dimension_scores["completeness"] == value


def test_flags_are_real_booleans_and_exactly_the_three_names():
    """`1` is not `true`. The flags are unscored, so an int there is a
    category error that a truthiness check would launder into a flag."""
    for bad in (1, 0, "true", None):
        flags = {**GOOD_FLAGS, "wrote_tests": bad}
        with pytest.raises(MalformedVerdict):
            parse_rubric_response(_rubric_json(flags=flags))

    missing = {k: v for k, v in GOOD_FLAGS.items() if k != "wrote_tests"}
    with pytest.raises(MalformedVerdict):
        parse_rubric_response(_rubric_json(flags=missing))

    extra = {**GOOD_FLAGS, "looked_suspicious": True}
    with pytest.raises(MalformedVerdict):
        parse_rubric_response(_rubric_json(flags=extra))


def test_a_fenced_json_response_still_parses():
    """Models wrap JSON in ```json fences and preface it with prose."""
    fenced = (
        "Here is my assessment of the submission.\n\n"
        "```json\n" + _rubric_json() + "\n```\n"
        "Happy to expand on any dimension.\n"
    )
    assert parse_rubric_response(fenced).dimension_scores == GOOD_SCORES

    fenced_vote = "Thinking it over:\n```json\n" + _pairwise_json("B") + "\n```"
    assert parse_pairwise_response(fenced_vote) == "second"


def test_braces_in_the_prose_around_the_object_do_not_move_its_boundaries():
    """The scan is brace-balanced and string-aware, and BOTH halves matter.

    Reasoning about a diff quotes code, and code holds braces. Each naive
    extractor fails on one side and looks fine on the other, so both are
    asserted here: `find('{')` to the first `}` truncates inside the
    reasoning, and `find('{')` to `rfind('}')` swallows a brace in the prose
    the model wrote *after* its answer. Either way a perfectly good reply is
    called malformed and the whole retry budget is spent re-asking a model
    that was right the first time.
    """
    reasoning = "It writes `if (x) { return y; }` where the reference did not."
    parsed = parse_rubric_response(_rubric_json(reasoning=reasoning))
    assert parsed.reasoning == reasoning

    trailing = _rubric_json() + "\n\nHappy to expand on the `{...}` handling."
    assert parse_rubric_response(trailing).dimension_scores == GOOD_SCORES

    vote = _pairwise_json("B") + "\nB's guard clause `{ }` is the difference."
    assert parse_pairwise_response(vote) == "second"

    # String tracking starts at the opening brace, so an unbalanced quote in
    # the prose ABOVE the object cannot swallow the brace that opens it.
    prefaced = 'My "verdict follows.\n' + _pairwise_json("A")
    assert parse_pairwise_response(prefaced) == "first"


def test_missing_or_unknown_dimension_names_are_malformed():
    missing = {k: v for k, v in GOOD_SCORES.items() if k != "scope_discipline"}
    with pytest.raises(MalformedVerdict):
        parse_rubric_response(_rubric_json(dimension_scores=missing))

    renamed = {
        ("scope_creep" if k == "scope_discipline" else k): v
        for k, v in GOOD_SCORES.items()
    }
    with pytest.raises(MalformedVerdict):
        parse_rubric_response(_rubric_json(dimension_scores=renamed))

    extra = {**GOOD_SCORES, "elegance": 2}
    with pytest.raises(MalformedVerdict):
        parse_rubric_response(_rubric_json(dimension_scores=extra))

    with pytest.raises(MalformedVerdict):
        parse_rubric_response(_rubric_json(dimension_scores="excellent"))


def test_unknown_top_level_keys_are_tolerated():
    """Models editorialize. An extra sibling key is not a malformed verdict --
    the dimensions and flags are what is pinned, and refusing the whole answer
    over a `confidence` field spends three calls to learn nothing."""
    parsed = parse_rubric_response(
        _rubric_json(confidence="high", notes=["chatty"])
    )
    assert parsed.dimension_scores == GOOD_SCORES

    vote = parse_pairwise_response(_pairwise_json("TIE", confidence=0.4))
    assert vote == "tie"


def test_a_verdict_without_reasoning_is_malformed():
    """`full_reasoning_text` is the only thing that makes a verdict auditable
    by a human, and it is what a disputed kappa is re-examined against. An
    empty one is a verdict nobody can check."""
    for bad in ("", "   ", None, 42):
        with pytest.raises(MalformedVerdict):
            parse_rubric_response(_rubric_json(reasoning=bad))
        with pytest.raises(MalformedVerdict):
            parse_pairwise_response(_pairwise_json(reasoning=bad))

    with pytest.raises(MalformedVerdict):
        parse_rubric_response(json.dumps({"dimension_scores": GOOD_SCORES}))


def test_a_response_with_no_json_object_at_all_is_malformed():
    for text in ("", "I decline to answer.", "```\nnot json\n```", "{"):
        with pytest.raises(MalformedVerdict):
            parse_rubric_response(text)
        with pytest.raises(MalformedVerdict):
            parse_pairwise_response(text)


def test_pairwise_verdicts_are_case_insensitive_and_stay_in_shown_terms():
    """A/B is the WIRE vocabulary and first/second is the shown-order one.

    `parse_pairwise_response` translates the first into the second and must
    never emit canonical "a"/"b" -- those mean the caller's own pair order,
    which the parser has no way to know. Two vocabularies that look alike are
    exactly the pair a position-mapping bug hides between.
    """
    assert parse_pairwise_response(_pairwise_json("A")) == "first"
    assert parse_pairwise_response(_pairwise_json("b")) == "second"
    assert parse_pairwise_response(_pairwise_json(" tie ")) == "tie"
    assert parse_pairwise_response(_pairwise_json("Tie")) == "tie"

    for bad in ("C", "", "AB", "first", "neither", 1, True, None):
        with pytest.raises(MalformedVerdict):
            parse_pairwise_response(_pairwise_json(bad))


# --- prompts -----------------------------------------------------------------


def test_the_rendered_prompt_contains_the_payload_diffs_verbatim():
    """Rendering interpolates; it does not summarize, truncate or re-wrap.

    A renderer that trimmed a diff would change what was judged while the
    stored payload went on showing the whole thing, and `judge_prompt_sha`
    would attest to the trimmed text nobody kept.
    """
    inputs = _inputs()
    rubric = render_rubric_prompt(build_rubric_payload(inputs))
    assert inputs.task_prompt in rubric
    assert SOLUTION_DIFF in rubric
    assert CANDIDATE_DIFF in rubric
    # The check names and statuses the ladder established, and nothing more.
    assert "f2p" in rubric and "not_configured" in rubric

    pairwise = render_pairwise_prompt(
        build_pairwise_payload(inputs, _other_inputs())
    )
    assert inputs.task_prompt in pairwise
    assert CANDIDATE_DIFF in pairwise
    assert SOLUTION_DIFF in pairwise
    assert "Submission A" in pairwise and "Submission B" in pairwise


def test_the_rubric_prompt_anchors_every_dimension_and_flag():
    """Five dimensions on a 0/1/2 anchored scale, three unscored flags.

    Short scales are deliberate -- 1-10 scales show poor inter-rater
    agreement -- so a prompt that asks for the names without stating what 0,
    1 and 2 mean has quietly reintroduced an unanchored scale.
    """
    rendered = render_rubric_prompt(build_rubric_payload(_inputs()))

    for name in RUBRIC_DIMENSIONS:
        assert name in rendered, f"{name} is not named in the rubric prompt"
    for flag in RUBRIC_FLAGS:
        assert flag in rendered, f"{flag} is not named in the rubric prompt"
    for anchor in ("0 ", "1 ", "2 "):
        assert anchor in rendered

    # The one anchor the spec spells out in words, because it is the one a
    # judge gets wrong by default: equivalence is about the RESULT.
    assert "not" in rendered.lower()
    assert "same code" in rendered.lower()


def test_prompt_sha_is_the_sha256_of_the_exact_rendered_text():
    rendered = render_rubric_prompt(build_rubric_payload(_inputs()))
    expected = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    assert prompt_sha(rendered) == expected
    assert prompt_sha(rendered) != prompt_sha(rendered + "\n")


def test_prompt_sha_changes_when_position_changes():
    """Position changes the prompt text, so the sha is per CALL, not per pair.

    One sha stored for both orders would make the position-swap probe
    unverifiable after the fact: nothing would say which of the two texts the
    verdict answered.
    """
    a, b = _inputs(), _other_inputs()
    a_first = render_pairwise_prompt(build_pairwise_payload(a, b))
    b_first = render_pairwise_prompt(build_pairwise_payload(b, a))

    assert a_first != b_first
    assert prompt_sha(a_first) != prompt_sha(b_first)


# --- the vote protocol -------------------------------------------------------


def test_retry_on_malformed_re_sends_the_same_prompt_and_counts_calls():
    """A retry is a fresh independent call, never a repair turn.

    Handing the model back its own bad output would make attempt two a
    continuation of attempt one -- and the same reasoning that makes three
    votes three separate calls (a single call asked for three opinions is one
    vote wearing three hats) makes a repair turn a correction of a vote rather
    than a new one.
    """
    complete = _FakeComplete("I refuse.", '{"scores": "great"}', _rubric_json())
    inputs = _inputs()

    payload, rendered, result = judge_rubric(inputs, complete)

    assert len(complete.prompts) == 3
    assert len(set(complete.prompts)) == 1, "the prompt was not re-sent as-is"
    assert complete.prompts[0] == rendered
    assert "I refuse." not in rendered
    assert result.dimension_scores == GOOD_SCORES
    assert payload == build_rubric_payload(inputs)


def test_exhausted_retries_raise_malformed_verdict():
    complete = _FakeComplete("no", "still no", "no again")
    with pytest.raises(MalformedVerdict):
        judge_rubric(_inputs(), complete)
    assert len(complete.prompts) == 3, "default retries=2 means three attempts"

    once = _FakeComplete("no")
    with pytest.raises(MalformedVerdict):
        judge_rubric(_inputs(), once, retries=0)
    assert len(once.prompts) == 1

    voting = _FakeComplete("no", "no", "no")
    with pytest.raises(MalformedVerdict):
        judge_pair_vote(
            _inputs(), _other_inputs(), random.Random(0), voting
        )
    assert len(voting.prompts) == 3
    assert len(set(voting.prompts)) == 1


def test_a_first_attempt_that_parses_makes_exactly_one_call():
    complete = _FakeComplete(_rubric_json())
    judge_rubric(_inputs(), complete)
    assert len(complete.prompts) == 1


def test_majority_arithmetic_including_ties():
    assert majority(["a", "a", "b"]) == "a"
    assert majority(["a", "b", "tie"]) == "tie"
    assert majority(["tie", "tie", "a"]) == "tie"
    assert majority(["b", "b", "b"]) == "b"
    # A strict majority, not a plurality: 2 of 4 is not a majority.
    assert majority(["a", "a", "b", "b"]) == "tie"
    assert majority(["a", "a", "a", "b"]) == "a"
    assert majority(["a"]) == "a"


def test_majority_refuses_the_shown_order_vocabulary():
    """"first"/"second" are presentation terms and mean nothing here.

    A caller that passed raw parser output would get a confident answer in a
    vocabulary no `JudgeRecord.verdict` field accepts, and the mistake would
    survive as a stored verdict rather than as an exception.
    """
    with pytest.raises(ValueError):
        majority(["first", "first", "second"])
    with pytest.raises(ValueError):
        majority(["a", "a", "A"])
    with pytest.raises(ValueError):
        majority([])


def test_seeded_rng_produces_both_position_assignments_and_maps_verdicts_back_correctly():  # noqa: E501
    """Position is drawn per vote, and the verdict is mapped back through it.

    The failure this guards is silent and total: a vote protocol that
    randomizes position but forgets to invert the mapping records the loser as
    the winner on half the comparisons, and every Elo number downstream is
    computed from it without anything looking wrong.
    """
    a, b = _inputs(), _other_inputs()
    seen: set[str] = set()

    for seed in range(24):
        for wire, first_wins, second_wins in (
            ("A", "a", "b"),
            ("B", "b", "a"),
        ):
            complete = _FakeComplete(_pairwise_json(wire))
            outcome = judge_pair_vote(a, b, random.Random(seed), complete)
            seen.add(outcome.position_assignment)

            if outcome.position_assignment == "a_first":
                assert outcome.payload["submission_first"]["diff"] == (
                    CANDIDATE_DIFF
                )
                assert outcome.verdict == first_wins
            else:
                assert outcome.position_assignment == "b_first"
                assert outcome.payload["submission_first"]["diff"] == (
                    SOLUTION_DIFF
                )
                assert outcome.verdict == second_wins

    assert seen == {"a_first", "b_first"}, "position was not randomized"

    # A tie is a tie under either assignment -- the one verdict the mapping
    # must leave alone.
    for seed in range(8):
        complete = _FakeComplete(_pairwise_json("TIE"))
        assert judge_pair_vote(a, b, random.Random(seed), complete).verdict == (
            "tie"
        )


def test_the_vote_outcome_carries_the_payload_and_prompt_that_were_sent():
    """Task 6 stores `write_payload(outcome.payload)` and
    `prompt_sha(outcome.rendered_prompt)`, so a `VoteOutcome` that rebuilt
    either would attest to something other than what the model saw."""
    a, b = _inputs(), _other_inputs()
    complete = _FakeComplete(_pairwise_json("A"))
    outcome = judge_pair_vote(a, b, random.Random(1), complete)

    assert complete.prompts == [outcome.rendered_prompt]
    assert outcome.rendered_prompt == render_pairwise_prompt(outcome.payload)
    assert outcome.reasoning == GOOD_REASONING
    assert outcome.payload["kind"] == "pairwise"

    shown = (a, b) if outcome.position_assignment == "a_first" else (b, a)
    assert outcome.payload == build_pairwise_payload(*shown)


def test_the_position_is_drawn_before_the_payload_is_built():
    """Draw first, then build in shown order -- never build then swap.

    Building an `a_first` payload and reordering it afterwards is the same
    output today and the shape that lets `position_assignment` and the payload
    disagree tomorrow. Pinned by consuming exactly one draw from an rng whose
    stream is known.
    """
    a, b = _inputs(), _other_inputs()

    class _OneDraw(random.Random):
        def __init__(self, value: float) -> None:
            super().__init__(0)
            self.value = value
            self.draws = 0

        def random(self) -> float:
            self.draws += 1
            return self.value

    for value, expected in ((0.0, "a_first"), (0.5, "b_first")):
        rng = _OneDraw(value)
        outcome = judge_pair_vote(
            a, b, rng, _FakeComplete(_pairwise_json("A"))
        )
        assert outcome.position_assignment == expected
        assert rng.draws == 1, "one draw per vote, taken before the build"


def test_a_pairwise_vote_across_two_tasks_never_reaches_the_model():
    """The pairing guard fires before any call is made, in either order."""
    other_task = payload_inputs_from(
        _record(), _grade(), _task(prompt="Fix the parser in tokens.py.")
    )
    for pair in ((_inputs(), other_task), (other_task, _inputs())):
        complete = _FakeComplete(_pairwise_json("A"))
        with pytest.raises(ValueError, match="same task"):
            judge_pair_vote(*pair, random.Random(0), complete)
        assert complete.prompts == []


def test_the_pinned_judge_identity_constants():
    """Tasks 5 and 6 read these, and two of them are load-bearing.

    `judge_model_id` must be a PINNED variant -- luna/sol/terra are three
    models, not three names for one, and a floating alias is a moving oracle.
    The cap goes out as `max_completion_tokens`: the candidate arms need a
    litellm patch to rename `max_tokens`, and a judge call that shipped the
    old name would be silently uncapped.
    """
    assert JUDGE_MODEL_ID_DEFAULT == "openai.gpt-5.6-sol"
    assert JUDGE_PROMPT_VERSION == 1
    assert RUBRIC_VERSION == "1.0.0"
    assert JUDGE_SAMPLING["temperature"] == 0.0
    assert "max_completion_tokens" in JUDGE_SAMPLING
    assert "max_tokens" not in JUDGE_SAMPLING

    assert RUBRIC_DIMENSIONS == (
        "functional_equivalence",
        "completeness",
        "cross_file_consistency",
        "scope_discipline",
        "convention_adherence",
    )
    assert RUBRIC_FLAGS == (
        "introduced_stub",
        "left_debug_artifacts",
        "wrote_tests",
    )
