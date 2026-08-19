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
* The same sweep runs over the RENDERED PROMPTS, which is the text that is
  actually sent and the text `judge_prompt_sha` attests to. The renderers do
  real work between the payload and the wire -- headings, the check list, the
  similarity block, a `None` ratio rendered as words -- so a payload proven
  clean is not by itself a prompt proven clean.

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
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import bakeoff.judge
from bakeoff.grade_schema import CheckResult, GradeRecord
from bakeoff.judge import (
    ALLOW_NON_NEUTRAL_JUDGE_ENV,
    JUDGE_MODEL_ID_DEFAULT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SAMPLING,
    MANTLE_BASE,
    NON_NEUTRAL_FAMILY_TOKENS,
    NON_NEUTRAL_VENDOR_PREFIXES,
    RUBRIC_DIMENSIONS,
    RUBRIC_FLAGS,
    RUBRIC_VERSION,
    VOTE_POSITIONS,
    MalformedVerdict,
    NonNeutralJudge,
    PayloadInputs,
    assert_neutral_judge,
    build_pairwise_payload,
    build_rubric_payload,
    is_auth_failure,
    judge_pair_vote,
    judge_rubric,
    live_completion,
    majority,
    new_usage_totals,
    parse_pairwise_response,
    parse_rubric_response,
    payload_inputs_from,
    prompt_sha,
    render_pairwise_prompt,
    render_rubric_prompt,
)
from bakeoff.judge_schema import PayloadSecretsFound, write_payload
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

# A SECOND submission, for the pairwise. Deliberately distinct from both
# `CANDIDATE_DIFF` and `SOLUTION_DIFF`: reusing the solution diff here reads as
# harmless -- it is a plausible submission and it differs from the candidate --
# but it is also the payload's reference anchor, so any assertion of the form
# `SOLUTION_DIFF in prompt` is satisfied by the reference section and pins
# nothing about submission B. Two renderer bugs survived that collision: a
# renderer that dropped `submission_second` entirely, and one that showed the
# two submissions under swapped labels.
OTHER_CANDIDATE_DIFF = (
    "diff --git a/calc.py b/calc.py\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@ def add(a, b):\n"
    "-    return a - b\n"
    "+    return sum((a, b))\n"
)

# A submission carrying a live-looking credential. The double-quoted shape on
# purpose: `generic_api_key` anchors on the quote immediately after `=`, and
# `json.dumps` puts a backslash there -- so this line is DIRTY as a raw string
# and CLEAN as canonical JSON, and only a scan that walks the payload's own
# strings catches it. See `test_judge_schema.DOUBLE_QUOTED_SECRET_LINE`, which
# pins both directions against the real scanner.
SECRET_BEARING_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,3 @@ def main():\n"
    '+    api_key = "sk-live-abcdefghijklmnopqrstuv"\n'
    "     return app\n"
)

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

#: The same, for the grade. A prior verdict turns two independent votes into
#: one vote and one confirmation, and the check output paths embed the arm.
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


def test_no_record_sentinel_survives_into_a_rendered_prompt():
    """The same sweep over the text that is actually SENT.

    Every other leak test in this file stops at the payload, and the payload is
    not what the judge reads: `render_*_prompt` interpolates it into prose, and
    the rendered string is what goes on the wire and what `judge_prompt_sha`
    attests to. The gap between the two is real work -- headings, the check
    list, the similarity block, the `None` ratio rendered as words -- and it is
    work done by functions that take a payload dict and could reach a value the
    payload sweep never reads, or add one of their own.

    The fixture-honesty half is asserted first, for the reason the payload
    sweep asserts it: a sentinel sweep that only checks absence passes
    perfectly against a fixture that stopped populating the field, which is the
    failure mode where a leak test stops testing and nothing says so.
    """
    record, grade, task = _record(), _grade(), _task()
    record_blob = json.dumps(record.to_dict()) + json.dumps(grade.to_dict())
    surface = {**LEAK_SURFACE, **GRADE_LEAK_SURFACE}

    for name, token in surface.items():
        assert token in record_blob, f"fixture does not populate {name}"

    inputs = payload_inputs_from(record, grade, task)
    rubric = render_rubric_prompt(build_rubric_payload(inputs))
    pairwise = render_pairwise_prompt(build_pairwise_payload(inputs, inputs))

    # The prompts really were rendered, so the sweep below is over text rather
    # than over two empty strings.
    assert CANDIDATE_DIFF in rubric and CANDIDATE_DIFF in pairwise
    for name, token in surface.items():
        assert token not in rubric, f"{name} leaked into the rubric prompt"
        assert token not in pairwise, f"{name} leaked into the pairwise prompt"


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


def _tally_row(run, rr) -> str:
    """Innocently named -- what somebody writes when `record` is already taken
    by an enclosing scope, or when one line felt short enough not to matter."""
    return run.model + rr.run_id


def _score_row(rec, grade_rec) -> str:
    """The abbreviation half. `rec` and `grade_rec` read as local shorthand
    rather than as a record, which is exactly what made them invisible."""
    return rec.task_id + grade_rec.grader_version


_leak = lambda record: record.model
'''

#: Where the lambda violation sits inside the block above, computed rather than
#: written down. A lambda carries no `def` name to report, so the scan labels
#: it by line; a hand-counted constant would go stale the first time a shape is
#: added above it.
_VIOLATION_LAMBDA_LINE = next(
    number
    for number, line in enumerate(TASK_4_STYLE_VIOLATIONS.splitlines(), start=1)
    if "lambda" in line
)


def _record_touchers(source: str) -> dict[str, list[str]]:
    """Functions that read an attribute off a `RunRecord` or `GradeRecord`.

    A name is treated as record-bound when its annotation mentions either
    class, OR when it is conventionally named. The second half is not
    redundant: an unannotated helper is exactly the shape that slips past a
    reader looking for types, and this is a tripwire rather than a type
    checker.

    The conventional set is deliberately wider than the four names this module
    happens to use. `run`, `rr`, `rec` and `grade_rec` are what an unannotated
    helper is actually called when `record` is taken by the enclosing scope or
    the line felt short enough not to matter, and a leak arriving under one of
    those names would satisfy every leak test in this file AND the guard
    written to catch what the leak tests cannot. Widening it costs a false
    positive on any future parameter genuinely named `run` -- which is a
    docstring and a rename, against a silent hole.

    LAMBDAS COUNT. `_leak = lambda record: record.model` is a whole leaking
    function, and walking `FunctionDef` alone did not look at it -- nothing
    about a lambda makes it less able to leak, it only makes it shorter to
    write, which is the wrong direction for a guard to be blind in. A
    `key=lambda record: record.cost_usd` handed to a sort is the plausible
    accident. A lambda has no name to report, so it is labelled by line, which
    is what a reader needs out of a failure anyway.

    Returns `{function name: [attribute chains it read]}`.
    """
    conventional = {
        "record",
        "grade",
        "run_record",
        "grade_record",
        "run",
        "rr",
        "rec",
        "grade_rec",
    }
    found: dict[str, list[str]] = {}

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            label = node.name
        elif isinstance(node, ast.Lambda):
            label = f"<lambda at line {node.lineno}>"
        else:
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
        found[label] = sorted(set(reads))

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
        "_tally_row",
        "_score_row",
        f"<lambda at line {_VIOLATION_LAMBDA_LINE}>",
    }

    source = Path(bakeoff.judge.__file__).read_text(encoding="utf-8")
    assert set(_record_touchers(source)) == {"payload_inputs_from"}


def test_an_innocently_named_helper_that_reads_a_record_still_trips_the_source_scan():  # noqa: E501
    """The tripwire's blind spot was the PARAMETER NAME, not the attribute.

    The scan binds a parameter when its annotation names a record class or when
    it is conventionally named, and the conventional set was `record`, `grade`,
    `run_record`, `grade_record`. An unannotated helper is the whole reason the
    name half exists -- and `run`, `rr`, `rec` and `grade_rec` are what an
    unannotated helper is actually called, because `record` is usually taken by
    the enclosing scope and the abbreviation reads as local shorthand rather
    than as a record. A leak arriving under one of those names satisfied every
    leak test in this file AND the structural guard that was written to catch
    what the leak tests cannot.

    The reads are asserted, not just the function names, so a widened set that
    matched the parameter but stopped following the attribute chain would fail
    here rather than pass with an empty list.
    """
    caught = _record_touchers(TASK_4_STYLE_VIOLATIONS)

    assert caught["_tally_row"] == ["rr.run_id", "run.model"]
    assert caught["_score_row"] == ["grade_rec.grader_version", "rec.task_id"]


def test_a_lambda_that_reads_a_record_is_not_invisible_to_the_source_scan():
    """`ast.FunctionDef` is not the only way to write a function.

    The scan walked `FunctionDef` and `AsyncFunctionDef` only, so
    `_leak = lambda record: record.model` -- a whole leaking function, bound to
    a module-level name, reading the arm off a record -- was not looked at.
    Nothing about a lambda makes it less able to leak; it only makes it shorter
    to write, which is the wrong direction for a guard to be blind in. A
    `key=lambda record: record.cost_usd` handed to a sort is the plausible
    accident, and it would have shipped the field into whatever the caller did
    with it.

    Labelled by LINE because there is no name to report. The label is what an
    operator reads out of a failure, so it has to point at something.
    """
    caught = _record_touchers(TASK_4_STYLE_VIOLATIONS)

    assert caught[f"<lambda at line {_VIOLATION_LAMBDA_LINE}>"] == [
        "record.model"
    ]


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
    """A second submission on the SAME task, distinct from the reference too.

    See `OTHER_CANDIDATE_DIFF`: a fixture that reused `SOLUTION_DIFF` here
    would collide with the payload's own reference anchor, and every prompt
    assertion about submission B would be satisfied by the reference section.
    """
    return _inputs(artifacts=Artifacts(final_diff=OTHER_CANDIDATE_DIFF))


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

    Asserted by POSITION, not by membership. Membership is the weaker claim
    and it is weak in a way that matters: it holds for a renderer that shows a
    diff under the wrong heading, and the pairwise labels ARE the vote. A
    consistent label swap inverts every verdict in the file while the payload,
    the position assignment and the sha all stay exactly right, which is the
    "complete, confident, inverted Elo table" this module is built to prevent.
    """
    inputs = _inputs()

    rubric = render_rubric_prompt(build_rubric_payload(inputs))
    assert inputs.task_prompt in rubric
    # The check names and statuses the ladder established, and nothing more.
    assert "f2p" in rubric and "not_configured" in rubric

    reference_at = rubric.index("## Reference change")
    submitted_at = rubric.index("## Submitted change")
    assert reference_at < submitted_at
    assert reference_at < rubric.index(SOLUTION_DIFF, reference_at)
    assert rubric.index(SOLUTION_DIFF, reference_at) < submitted_at
    assert submitted_at < rubric.index(CANDIDATE_DIFF, submitted_at)

    pairwise = render_pairwise_prompt(
        build_pairwise_payload(inputs, _other_inputs())
    )
    assert inputs.task_prompt in pairwise

    a_at = pairwise.index("## Submission A")
    b_at = pairwise.index("## Submission B")
    assert a_at < b_at
    assert a_at < pairwise.index(CANDIDATE_DIFF, a_at) < b_at
    assert b_at < pairwise.index(OTHER_CANDIDATE_DIFF, b_at)
    # The reference anchor sits in its own section above both, and -- now that
    # no submission reuses it -- appears exactly once.
    assert pairwise.count(SOLUTION_DIFF) == 1
    assert pairwise.index(SOLUTION_DIFF) < a_at


# --- the pinned prompt text --------------------------------------------------
#
# The prompt IS the instrument. Everything below is pinned because a mutation
# sweep deleted each of these pieces with the whole suite green, and the two
# reasons it could are both fixed here rather than patched over:
#
# * **The fixture answered the assertion.** The old anchor test asked for
#   `"0 "`, `"1 "` and `"2 "` anywhere in the rendered text -- and a unified
#   diff hunk header (`@@ -1,2 +1,2 @@`) carries all three, so the entire
#   0/1/2 anchored scale could be deleted from `_RUBRIC_DIMENSIONS_BLOCK` and
#   the interpolated diff went on satisfying the test. The pins below are FULL
#   LITERAL LINES, positioned inside the dimension block they belong to, and
#   `test_no_pinned_prompt_text_can_be_supplied_by_the_fixture_payload` keeps a
#   later fixture edit from re-opening the same door.
# * **Three blocks answered each other.** Every dimension name appears in
#   `_RUBRIC_DIMENSIONS_BLOCK` and again in `_RUBRIC_RESPONSE_FORMAT`, and
#   every flag name in `_RUBRIC_FLAGS_BLOCK` and again in the response format,
#   so `name in rendered` held after either block was deleted whole. The pin is
#   therefore an occurrence COUNT, which no single surviving block satisfies.
#
# Sentence pins are asserted against WHITESPACE-NORMALISED text. Re-wrapping a
# paragraph is caught by the golden sha below, which is the right owner for
# "one character moved"; what these own is "this instruction is still in front
# of the model", and a pin that also fails on a re-flow reports the wrong fact.

#: The 0/1/2 anchor lines, VERBATIM, keyed by the dimension whose numbered
#: block they must sit inside. Triple-quoted so each line is a byte-for-byte
#: copy of `_RUBRIC_DIMENSIONS_BLOCK` rather than a re-typed paraphrase -- a
#: pin assembled out of fragments is a pin on the assembly.
#:
#: Short scales are deliberate (1-10 scales show poor inter-rater agreement),
#: so a prompt that names the five dimensions without saying what 0, 1 and 2
#: MEAN has quietly reintroduced an unanchored scale while still looking like a
#: rubric.
RUBRIC_ANCHOR_LINES: dict[str, str] = {
    "functional_equivalence": """\
   0 -- does not accomplish what the reference accomplished.
   1 -- accomplishes part of it, or only on some inputs or code paths.
   2 -- accomplishes what the reference accomplished.""",
    "completeness": """\
   0 -- most of the task is unaddressed, or the change is a stub.
   1 -- the main part is addressed, with TODOs or unhandled cases left.
   2 -- every part of the task is addressed, with no stubs and no TODOs.""",
    "cross_file_consistency": """\
   0 -- callers, signatures or imports are left inconsistent with the change.
   1 -- mostly consistent, with a call site or a signature missed.
   2 -- every caller updated and every signature aligned.""",
    "scope_discipline": """\
   0 -- substantial unrelated edits or a gratuitous refactor rides along.
   1 -- mostly on target, with incidental churn.
   2 -- only what the task required.""",
    "convention_adherence": """\
   0 -- ignores the surrounding idiom, naming and error handling.
   1 -- broadly follows them, with local departures.
   2 -- matches the surrounding idiom, naming and error handling.""",
}

#: The blindness instruction. The payload is blind by construction and this
#: sentence is what tells the model not to go looking anyway.
RUBRIC_BLINDNESS = (
    "The submission is anonymous: nothing below says who or what produced it, "
    "and there is nothing to infer."
)

#: The one anchor the spec spells out in words, because it is the one a judge
#: gets wrong by default: equivalence is about the RESULT. Deleted, the rubric
#: silently becomes a diff-similarity score wearing five dimension names.
RUBRIC_RESULT_NOT_SAME_CODE = (
    'Judge the RESULT. This is explicitly NOT "is the same code": a different '
    "design, different names and a different structure that reach the same "
    "behaviour score 2."
)

#: Identical in both prompts, and load-bearing in both: `SimilarityContext` has
#: no total and no ordering precisely so nobody sums the three facts into a
#: score, and this sentence is that same refusal aimed at the model.
NOT_A_SIMILARITY_SCORE = (
    "The overlap figures are three independent facts and deliberately not a "
    "similarity score: a diff that is bigger or smaller than the reference is "
    "not by that fact better or worse."
)

#: Task 2's replacement for the "shown in a random order" claim. Both halves
#: matter: the harness stopped randomizing (every comparison is now shown in
#: BOTH orders, one forced vote each), so the old sentence was false about the
#: text in front of the model -- and a model that catches the harness being
#: wrong about its own procedure has been handed a reason to discount the rest
#: of it. The instruction the sentence exists for has to survive the rewrite.
PAIRWISE_ORDER = (
    "Both submissions are anonymous, nothing below identifies either one, and "
    "they are shown in an order chosen by the harness that carries no "
    "information about the submissions -- do not prefer a submission for "
    "appearing first or second."
)

#: TIE is a permitted verdict at the vote level AND at the majority level, and
#: `majority` invents no tie-break. A prompt that stops saying so pushes the
#: model off ties, which shows up downstream as a Bradley-Terry table fit to
#: preferences the judge did not have.
PAIRWISE_TIE = (
    "TIE is a real answer, not a way of declining the question. Use it when "
    "the two are genuinely equivalent, not when one is slightly ahead."
)

#: The sentence that makes a reply parseable at all, in both prompts.
ONE_JSON_OBJECT = "Reply with ONE JSON object and nothing else."

#: `parse_rubric_response` rejects `true` for a dimension and `1` for a flag,
#: by type and not by truthiness. This is the half of that contract the model
#: is told, and dropping it turns a strict parser into a retry budget.
RUBRIC_SCALE_RULE = (
    "Scores are the integers 0, 1 or 2 -- never true, false or a decimal. "
    "Flags are JSON true or false -- never 0 or 1."
)

#: The pairwise wire vocabulary, stated to the model. A/B/TIE is what
#: `_SHOWN_BY_WIRE` accepts and nothing else is coerced.
PAIRWISE_VERDICT_RULE = (
    '`verdict` is exactly "A", "B" or "TIE"; `reasoning` names the specific '
    "differences that decided it."
)

#: THE deletion that passed all 906 tests: strike this one line and every
#: pairwise reply arrives in whatever shape the model felt like, every parse
#: raises `MalformedVerdict`, and a forty-hour batch spends its whole retry
#: budget on a prompt that never asked for JSON.
PAIRWISE_VERDICT_SHAPE = (
    '{"verdict": "<A, B or TIE>", "reasoning": "<what decided it>"}'
)

#: Without it a literal `<A, B or TIE>` comes back as the verdict.
ANGLE_BRACKETS_ARE_SLOTS = (
    "The angle brackets mark slots to fill and must not appear in your reply."
)

#: Every literal this file pins in a rendered prompt, for the one test whose
#: job is to prove the FIXTURE supplies none of it. Membership assertions are
#: only as strong as the text they are made against, and the collision this
#: replaces was invisible for exactly that reason.
PINNED_PROMPT_TEXT: tuple[str, ...] = (
    *RUBRIC_DIMENSIONS,
    *RUBRIC_FLAGS,
    *(
        line
        for block in RUBRIC_ANCHOR_LINES.values()
        for line in block.splitlines()
    ),
    RUBRIC_BLINDNESS,
    RUBRIC_RESULT_NOT_SAME_CODE,
    RUBRIC_SCALE_RULE,
    NOT_A_SIMILARITY_SCORE,
    PAIRWISE_ORDER,
    PAIRWISE_TIE,
    PAIRWISE_VERDICT_RULE,
    PAIRWISE_VERDICT_SHAPE,
    ONE_JSON_OBJECT,
    ANGLE_BRACKETS_ARE_SLOTS,
)


def _normalised(text: str) -> str:
    """One space between words, so a sentence pin survives a re-wrap.

    The prompt templates are hard-wrapped source strings; a sentence spans two
    or three lines and the wrap point moves whenever a word does. What the
    sentence pins assert is presence, not layout -- layout is the golden sha's
    job.
    """
    return " ".join(text.split())


def _supplied_strings(payload: Any) -> list[str]:
    """Every string a payload contributes to a rendered prompt.

    Walked rather than `json.dumps`ed: the renderers interpolate these values
    raw, and a dumped payload escapes the newlines a diff carries, which would
    make a multi-line collision invisible to the very test that exists to find
    collisions.
    """
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        values = payload.values()
        return [s for value in values for s in _supplied_strings(value)]
    if isinstance(payload, (list, tuple)):
        return [s for value in payload for s in _supplied_strings(value)]
    return []


def test_the_rubric_prompt_anchors_every_dimension_inside_its_own_block():
    """Each dimension's 0/1/2 anchors sit under THAT dimension's heading.

    Positional, not membership. Membership is what let the entire anchored
    scale be deleted while a hunk header (`@@ -1,2 +1,2 @@`) kept the old
    `"0 "`/`"1 "`/`"2 "` assertions green -- and membership alone would also
    hold for a prompt that listed all fifteen anchor lines under dimension one,
    which is a rubric with four unanchored dimensions and no way to tell.

    The failure this prevents is not a crash. An unanchored scale still returns
    integers, the parser still accepts them, and the profile still prints: what
    changes is that 1 stops meaning the same thing between two passes, which is
    the exact reason `RUBRIC_VERSION` exists.
    """
    rendered = render_rubric_prompt(build_rubric_payload(_inputs()))

    # Each numbered heading appears once, and they run in `RUBRIC_DIMENSIONS`
    # order -- the order the scores are asked in and stored in.
    starts = []
    for number, name in enumerate(RUBRIC_DIMENSIONS, start=1):
        heading = f"{number}. {name}"
        assert rendered.count(heading) == 1, f"{heading!r} is not the one head"
        starts.append(rendered.index(heading))
    assert starts == sorted(starts), "the dimensions are out of asked order"

    # Each block runs to the next heading; the last runs to the Flags section.
    flags_at = rendered.index("## Flags")
    assert starts[-1] < flags_at
    ends = starts[1:] + [flags_at]

    for name, start, end in zip(RUBRIC_DIMENSIONS, starts, ends):
        block = rendered[start:end]
        seen = []
        for anchor in RUBRIC_ANCHOR_LINES[name].splitlines():
            assert anchor in block, (
                f"{anchor!r} is missing from the {name} block: that dimension "
                "is being scored on an unanchored scale"
            )
            seen.append(block.index(anchor))
        assert seen == sorted(seen), f"{name}'s anchors are out of 0/1/2 order"

    assert _normalised(RUBRIC_RESULT_NOT_SAME_CODE) in _normalised(rendered)


def test_every_rubric_dimension_and_flag_name_is_carried_by_two_blocks():
    """Names are counted, because three blocks answer each other's questions.

    `_RUBRIC_DIMENSIONS_BLOCK` and `_RUBRIC_RESPONSE_FORMAT` both name all five
    dimensions; `_RUBRIC_FLAGS_BLOCK` and the response format both name all
    three flags. So `name in rendered` survives the deletion of ANY ONE of the
    three blocks, and each deletion is a different silent failure: no
    dimensions block is an unanchored scale, no flags block is three flags
    invented at reply time, and no response format is a reply nothing can
    parse.

    An exact count of 2 fails on each of those alone -- and fails just as
    loudly if a fourth mention appears, which would mean a name arrived
    somewhere this test does not know about.
    """
    rendered = render_rubric_prompt(build_rubric_payload(_inputs()))

    for name in RUBRIC_DIMENSIONS + RUBRIC_FLAGS:
        assert rendered.count(name) == 2, (
            f"{name!r} appears {rendered.count(name)} times, not twice: the "
            "dimensions block, the flags block and the reply format each name "
            "these, and one of those blocks has moved or gone"
        )

    normalised = _normalised(rendered)
    assert normalised.count(_normalised(ONE_JSON_OBJECT)) == 1
    assert _normalised(RUBRIC_SCALE_RULE) in normalised
    assert _normalised(ANGLE_BRACKETS_ARE_SLOTS) in normalised
    assert _normalised(RUBRIC_BLINDNESS) in normalised
    assert _normalised(NOT_A_SIMILARITY_SCORE) in normalised


def test_the_pairwise_prompt_carries_the_reply_shape_the_parser_requires():
    """The response format is what makes a vote a vote, and it was deletable.

    Deleting `_PAIRWISE_RESPONSE_FORMAT` from the render list passed all 906
    tests that existed when the mutation sweep found it: nothing asserted that
    the prompt asks for JSON at all. In production that is not a wrong number,
    it is a batch where every reply is prose, every parse raises
    `MalformedVerdict`, and both votes on every comparison burn three attempts
    apiece on a question that was never asked properly.

    Pinned by SHAPE and then round-tripped: the literal skeleton the prompt
    shows, with its two slots filled, is fed to the real parser. A prompt and a
    parser that drift apart is the failure a membership assertion on either one
    alone cannot see.
    """
    rendered = render_pairwise_prompt(
        build_pairwise_payload(_inputs(), _other_inputs())
    )
    normalised = _normalised(rendered)

    assert PAIRWISE_VERDICT_SHAPE in rendered
    assert normalised.count(_normalised(ONE_JSON_OBJECT)) == 1
    assert _normalised(PAIRWISE_VERDICT_RULE) in normalised
    assert _normalised(ANGLE_BRACKETS_ARE_SLOTS) in normalised
    assert _normalised(PAIRWISE_TIE) in normalised
    assert _normalised(NOT_A_SIMILARITY_SCORE) in normalised

    # The reply format is the LAST thing the model reads, after both
    # submissions -- an instruction above two full diffs is an instruction two
    # diffs away from the answer.
    assert rendered.index("## Submission B") < rendered.index(
        PAIRWISE_VERDICT_SHAPE
    )

    # The shape the prompt shows is the shape the parser accepts. Filled in
    # exactly as instructed: the angle-bracket slots go, nothing else moves.
    for wire, shown in (("A", "first"), ("B", "second"), ("TIE", "tie")):
        filled = PAIRWISE_VERDICT_SHAPE.replace(
            "<A, B or TIE>", wire
        ).replace("<what decided it>", GOOD_REASONING)
        assert parse_pairwise_response(filled) == shown


def test_the_pairwise_prompt_no_longer_claims_the_order_is_random():
    """The prompt may not tell the model something the harness stopped doing.

    Every comparison is now shown in BOTH orders, one forced vote each, so
    "a RANDOM order" is false about the text in front of the model -- and a
    false statement in the instructions is not a harmless leftover: the claim
    that order is random is itself an argument for ignoring order, and a model
    that catches the harness being wrong about its own procedure has been
    handed a reason to discount the rest of it.

    The replacement still has to carry the instruction the sentence existed
    for, so the whole sentence is pinned rather than the two phrases that
    changed: the do-not-prefer-by-position clause is the working half, and a
    rewrite that kept "chosen by the harness" while dropping that clause would
    have satisfied the phrase-level version of this test.
    """
    rendered = render_pairwise_prompt(
        build_pairwise_payload(_inputs(), _other_inputs())
    )

    assert "random" not in rendered.lower()
    assert _normalised(PAIRWISE_ORDER) in _normalised(rendered)


def test_no_pinned_prompt_text_can_be_supplied_by_the_fixture_payload():
    """The pins above are only as strong as the fixture they are read against.

    This is the collision test, and it is the whole reason the anchor pins are
    full literal lines. `"0 "`, `"1 "` and `"2 "` were all supplied by a
    unified diff's hunk header, so the assertions that were supposed to prove
    the prompt anchors its scale were being answered by the submission being
    judged -- and every one of them stayed green while the anchored scale was
    deleted.

    Asserted over the payload's own strings rather than over the rendered
    prompt, because those are exactly the bytes the renderers interpolate. Both
    the raw and the whitespace-normalised forms, since the sentence pins read a
    normalised prompt and a diff line could otherwise smuggle a match across a
    wrap.
    """
    payloads = [
        build_rubric_payload(_inputs()),
        build_pairwise_payload(_inputs(), _other_inputs()),
        GOLDEN_RUBRIC_PAYLOAD,
        GOLDEN_PAIRWISE_PAYLOAD,
    ]
    for payload in payloads:
        supplied = "\n".join(_supplied_strings(payload))
        haystacks = (supplied, _normalised(supplied))
        for pinned in PINNED_PROMPT_TEXT:
            for needle in {pinned, _normalised(pinned)}:
                for haystack in haystacks:
                    assert needle not in haystack, (
                        f"the {payload['kind']} fixture supplies {needle!r}, "
                        "which this file pins in the prompt: every assertion "
                        "about that text is now answered by the payload"
                    )


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


# --- the golden shas ---------------------------------------------------------
#
# The pins above catch a piece of the prompt going MISSING. These catch a piece
# of it CHANGING -- a reworded anchor, a re-wrapped paragraph, a heading with a
# different capital -- which the membership pins are deliberately blind to and
# which moves model output all the same. `JUDGE_PROMPT_VERSION`'s docstring
# says the version moves on any change "including one that reads as cosmetic";
# this is the only thing in the repo that puts that sentence in front of the
# person editing the text. It cannot require the bump -- re-record both
# constants without one and this file passes -- and the golden test's own
# docstring below says why that is attention rather than enforcement.

#: A payload frozen HERE rather than built from `_record()`/`_grade()`/
#: `_task()`, and that is the whole design of this fixture. A golden sha over a
#: record-built payload moves whenever a sentinel is added to the record
#: fixture, so the test would fire on an edit that changed no prompt text at
#: all -- and a golden test that cries wolf gets re-recorded reflexively, which
#: is the same as not having one. Nothing but the templates can move this sha.
#:
#: The values are inert markers, chosen to collide with nothing this file pins
#: (`test_no_pinned_prompt_text_can_be_supplied_by_the_fixture_payload` checks
#: these two payloads too). Both `_names` branches are exercised -- a populated
#: list and an empty one rendering as `(none)` -- and a non-null
#: `diff_size_ratio` renders through the `.2f` path, so the sha covers the
#: renderer's own formatting and not just the static template text.
GOLDEN_SIMILARITY: dict[str, Any] = {
    "file_overlap": {
        "common": ["GOLDEN_COMMON_FILE"],
        "candidate_only": ["GOLDEN_CANDIDATE_FILE"],
        "reference_only": [],
    },
    "symbol_overlap": {
        "common": [],
        "candidate_only": ["GOLDEN_CANDIDATE_SYMBOL"],
        "reference_only": ["GOLDEN_REFERENCE_SYMBOL"],
    },
    "diff_size_ratio": 1.25,
}

GOLDEN_CHECKS: list[dict[str, str]] = [
    {"name": "GOLDEN_CHECK", "status": "pass"},
    {"name": "GOLDEN_OTHER_CHECK", "status": "not_configured"},
]

GOLDEN_RUBRIC_PAYLOAD: dict[str, Any] = {
    "kind": "rubric",
    "task_prompt": "GOLDEN_TASK_PROMPT",
    "reference_diff": "GOLDEN_REFERENCE_DIFF",
    "candidate_diff": "GOLDEN_CANDIDATE_DIFF",
    "checks": GOLDEN_CHECKS,
    "similarity": GOLDEN_SIMILARITY,
}

GOLDEN_PAIRWISE_PAYLOAD: dict[str, Any] = {
    "kind": "pairwise",
    "task_prompt": "GOLDEN_TASK_PROMPT",
    "reference_diff": "GOLDEN_REFERENCE_DIFF",
    "submission_first": {
        "diff": "GOLDEN_FIRST_DIFF",
        "checks": GOLDEN_CHECKS,
        "similarity": GOLDEN_SIMILARITY,
    },
    "submission_second": {
        "diff": "GOLDEN_SECOND_DIFF",
        "checks": GOLDEN_CHECKS,
        "similarity": GOLDEN_SIMILARITY,
    },
}

#: Recorded from the templates as they stand at `JUDGE_PROMPT_VERSION` 2. These
#: are DATA, not a claim about what the prompt should say: re-record them
#: whenever the text is deliberately changed, in the same commit that bumps the
#: version.
GOLDEN_RUBRIC_PROMPT_SHA = (
    "168aed666128bbe823070b2febfc4cd9b15b633878d22b2a4b27d369d251595f"
)
GOLDEN_PAIRWISE_PROMPT_SHA = (
    "de8592286d7489bef3c8204d58cf1c6651c57540195552ae481aabdc36adc87e"
)

#: The message on the two sha assertions, and the whole value of them -- a
#: golden failure with no instruction attached is answered by deleting the
#: test. It ASKS for the bump; nothing here can require one, which is why the
#: version is asserted separately below rather than folded in.
_RE_RECORD = (
    "the prompt text changed: bump JUDGE_PROMPT_VERSION and re-record both "
    "constants"
)

#: The message on the version assertion, which fires under the OPPOSITE
#: condition and must not borrow the one above. `JUDGE_PROMPT_VERSION` moves on
#: protocol changes as well as text changes, so this assertion's realistic
#: cause is a protocol bump with the templates untouched -- and `_RE_RECORD`
#: would answer that with three wrong instructions: the text did not change,
#: the bump has already happened, and re-recording is not what is being asked
#: for.
_VERSION_MOVED = (
    "JUDGE_PROMPT_VERSION moved: re-record both shas from the current "
    "templates and update this assertion"
)


def test_the_prompt_version_is_coupled_to_the_rendered_template_text():
    """The rendered templates are pinned byte for byte, and named as v2's.

    `judge_prompt_version` is what keeps two generations of verdict from being
    averaged, and it is declared by hand: nothing else in this repo notices
    that the text moved. Whitespace and ordering move model output, so an edit
    that reads as cosmetic still produces verdicts that must not be pooled with
    the old ones -- and the failure is silent in the worst way, because the
    file looks like one clean generation and the disagreement between the two
    halves reads as judge noise.

    WHAT THIS CANNOT DO is make the bump mandatory. Re-record both constants
    and leave the version at 2 and this test passes, because the shas describe
    the text and the version is a separate declaration: what stands between a
    silent prompt edit and a mixed file is `_RE_RECORD`, read by whoever is
    re-recording. That is attention, not enforcement, and the difference
    matters to anyone deciding how much this test is worth.

    The version is asserted here anyway, and beside the shas rather than
    elsewhere, because these three constants are one statement -- *this text is
    what v2 means* -- and a reader who lands on a failure needs all three in
    front of them. It carries its own message: it fires when the version moved
    off 2, which is the reverse of what the shas are complaining about.

    A failure here is not a bug report. It says the prompt is not the prompt
    these constants were recorded from, and the answer is to look at the diff:
    if the change was intended, bump `JUDGE_PROMPT_VERSION` and re-record both
    constants in the same commit; if it was not, the diff is the finding.
    """
    rubric = render_rubric_prompt(GOLDEN_RUBRIC_PAYLOAD)
    pairwise = render_pairwise_prompt(GOLDEN_PAIRWISE_PAYLOAD)

    assert prompt_sha(rubric) == GOLDEN_RUBRIC_PROMPT_SHA, _RE_RECORD
    assert prompt_sha(pairwise) == GOLDEN_PAIRWISE_PROMPT_SHA, _RE_RECORD
    assert JUDGE_PROMPT_VERSION == 2, _VERSION_MOVED


# --- the vote protocol -------------------------------------------------------


def test_retry_on_malformed_re_sends_the_same_prompt_and_counts_calls():
    """A retry is a fresh independent call, never a repair turn.

    Handing the model back its own bad output would make attempt two a
    continuation of attempt one -- and the same reasoning that makes the two
    forced positions two separate calls (a single call asked for both orders
    is one vote wearing two hats) makes a repair turn a correction of a vote
    rather than a new one.

    Kept at temperature 0 for a reason the protocol change does not remove:
    the retry is against TRANSPORT nondeterminism -- a truncated reply, an
    empty completion -- which a fresh identical call still fixes, and it costs
    nothing whenever parsing succeeds the first time.
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
        judge_pair_vote(_inputs(), _other_inputs(), "a_first", voting)
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


def test_two_agreeing_votes_aggregate_as_that_verdict_and_a_split_as_a_tie():
    """n=2 is the size every comparison now has, and `majority` already
    handles it: strict majority over two votes IS agreement-else-tie.

    Pinned separately from the arithmetic above because it is the arithmetic
    the forced-position protocol actually runs on. A rule invented for two --
    "on a split, take the a_first vote" -- would manufacture a preference from
    the very position effect the second vote exists to expose.
    """
    assert majority(["a", "a"]) == "a"
    assert majority(["b", "b"]) == "b"
    assert majority(["tie", "tie"]) == "tie"
    # A split is a position-inconsistent judge, and that is a tie rather than
    # a coin flip between the two orders.
    assert majority(["a", "b"]) == "tie"
    assert majority(["b", "a"]) == "tie"
    assert majority(["a", "tie"]) == "tie"
    assert majority(["tie", "b"]) == "tie"


def test_both_forced_positions_map_the_shown_verdict_back_to_canonical_terms():
    """Each position is asked for, and the verdict is mapped back through it.

    The failure this guards is silent and total: a vote protocol that shows
    both orders and forgets to invert records the loser as the winner on
    exactly one of the two votes, and every Elo number downstream is computed
    from it without anything looking wrong. Under forced positions that bug is
    no longer half-invisible -- it makes every comparison split 1-1, which
    reads as a judge with zero position consistency.

    Driven directly through `judge_pair_vote` rather than through a seeded
    generator: at temperature 0 the position is an ARGUMENT, so there is no
    draw to replay and the map-back is a total function of it.
    """
    a, b = _inputs(), _other_inputs()

    for wire, first_wins, second_wins in (("A", "a", "b"), ("B", "b", "a")):
        a_first = judge_pair_vote(
            a, b, "a_first", _FakeComplete(_pairwise_json(wire))
        )
        assert a_first.position_assignment == "a_first"
        assert a_first.payload["submission_first"]["diff"] == CANDIDATE_DIFF
        assert a_first.verdict == first_wins

        b_first = judge_pair_vote(
            a, b, "b_first", _FakeComplete(_pairwise_json(wire))
        )
        assert b_first.position_assignment == "b_first"
        assert b_first.payload["submission_first"]["diff"] == (
            OTHER_CANDIDATE_DIFF
        )
        assert b_first.verdict == second_wins

    # A tie is a tie under either position -- the one verdict the mapping must
    # leave alone.
    for position in VOTE_POSITIONS:
        outcome = judge_pair_vote(
            a, b, position, _FakeComplete(_pairwise_json("TIE"))
        )
        assert outcome.verdict == "tie"


def test_judge_pair_vote_refuses_a_position_outside_the_two_it_knows():
    """A position this function does not know is a caller bug, and the only
    alternatives are worse.

    Defaulting to `a_first` would judge the comparison in one order while the
    record claimed another; passing the string through to `_CANONICAL_VERDICT`
    would raise a `KeyError` AFTER the paid call, naming a dict rather than the
    argument. Raised BEFORE anything is built, so the model is never asked.
    """
    a, b = _inputs(), _other_inputs()

    for bad in ("A_FIRST", "a-first", "first", "random", "", None):
        complete = _FakeComplete(_pairwise_json("A"))
        with pytest.raises(ValueError, match="position"):
            judge_pair_vote(a, b, bad, complete)
        assert complete.prompts == []


def test_the_vote_outcome_carries_the_payload_and_prompt_that_were_sent():
    """Task 6 stores `write_payload(outcome.payload)` and
    `prompt_sha(outcome.rendered_prompt)`, so a `VoteOutcome` that rebuilt
    either would attest to something other than what the model saw."""
    a, b = _inputs(), _other_inputs()
    complete = _FakeComplete(_pairwise_json("A"))
    outcome = judge_pair_vote(a, b, "a_first", complete)

    assert complete.prompts == [outcome.rendered_prompt]
    assert outcome.rendered_prompt == render_pairwise_prompt(outcome.payload)
    assert outcome.reasoning == GOOD_REASONING
    assert outcome.payload["kind"] == "pairwise"
    assert outcome.payload == build_pairwise_payload(a, b)


def test_the_payload_is_built_in_shown_order_rather_than_built_then_swapped():
    """Build in the position's order -- never build `a_first` and reorder.

    Same bytes today, and the shape that lets `position_assignment` and the
    payload disagree tomorrow, at which point the position-consistency figure
    is computed over pairs of votes that were not shown what they claim.
    Pinned by comparing the stored payload against the builder called in the
    order the position names.
    """
    a, b = _inputs(), _other_inputs()

    for position, shown in (("a_first", (a, b)), ("b_first", (b, a))):
        outcome = judge_pair_vote(
            a, b, position, _FakeComplete(_pairwise_json("A"))
        )
        assert outcome.payload == build_pairwise_payload(*shown)
        assert outcome.rendered_prompt == render_pairwise_prompt(
            build_pairwise_payload(*shown)
        )


def test_a_pairwise_vote_across_two_tasks_never_reaches_the_model():
    """The pairing guard fires before any call is made, in either order."""
    other_task = payload_inputs_from(
        _record(), _grade(), _task(prompt="Fix the parser in tokens.py.")
    )
    for pair in ((_inputs(), other_task), (other_task, _inputs())):
        complete = _FakeComplete(_pairwise_json("A"))
        with pytest.raises(ValueError, match="same task"):
            judge_pair_vote(*pair, "a_first", complete)
        assert complete.prompts == []


def test_a_secret_shaped_payload_never_reaches_the_complete_seam():
    """The scan moved in front of the wire, where the exposure actually is.

    Scanning at WRITE time protects the disk and nothing else: by then the
    payload has been rendered into a prompt and sent to a third-party model, so
    the credential has already left the machine and the only copy refused is
    the one that would have stayed. §6.2 has wire logs scanned for exactly this
    exposure, and the judge's payload carries the same repository source.

    Refused here it costs the unit and nothing else -- no prompt rendered, no
    call made, no file, no line -- and the counting fake is what pins that: it
    raises on any call at all, and `prompts` is asserted empty besides, so a
    seam invoked once before the raise cannot pass as never invoked.

    Both kinds, both positions, and the secret on each SIDE of the pairwise:
    the payload builders take the two submissions separately, so a scan reached
    through one slot only would refuse half of these and look like a working
    guard on the other half.
    """
    dirty = _inputs(artifacts=Artifacts(final_diff=SECRET_BEARING_DIFF))
    clean = _other_inputs()

    complete = _FakeComplete()
    with pytest.raises(PayloadSecretsFound) as excinfo:
        judge_rubric(dirty, complete)
    assert "generic_api_key" in str(excinfo.value)
    assert complete.prompts == []

    for position in VOTE_POSITIONS:
        for pair in ((dirty, clean), (clean, dirty)):
            complete = _FakeComplete()
            with pytest.raises(PayloadSecretsFound) as excinfo:
                judge_pair_vote(*pair, position, complete)
            assert "generic_api_key" in str(excinfo.value)
            assert complete.prompts == []

    # The same call over a clean submission still reaches the seam, so the four
    # refusals above are the scanner and not a fixture nothing can judge.
    assert judge_rubric(clean, _FakeComplete(_rubric_json())) is not None


def test_the_write_time_scan_survives_as_a_backstop_behind_the_build_time_one(
    tmp_path: Path, monkeypatch
):
    """Two layers, and either one alone still refuses the payload.

    The build-time scan is the one that matters -- it is the only one in front
    of the paid call -- but it is also new, reachable only through the two
    functions here, and a payload assembled by some later caller that skipped
    them would arrive at the store unscanned. So `write_payload` keeps its own
    scan rather than trusting the layer above, and this test proves that layer
    is load-bearing on its own: the build-time scan is stubbed blind, the vote
    goes all the way to the model, and the store still refuses the payload and
    still leaves nothing on disk.

    Stubbing `scan_payload` in `bakeoff.judge` and not in `bakeoff.judge_schema`
    is the whole point -- patching the shared name would disable both layers and
    the test would be pinning nothing.
    """
    monkeypatch.setattr(bakeoff.judge, "scan_payload", lambda payload: set())

    dirty = _inputs(artifacts=Artifacts(final_diff=SECRET_BEARING_DIFF))
    complete = _FakeComplete(_rubric_json())
    payload, _, _ = judge_rubric(dirty, complete)

    # The build-time guard really was blind: the call happened.
    assert len(complete.prompts) == 1

    payloads = tmp_path / "payloads"
    payloads.mkdir()
    with pytest.raises(PayloadSecretsFound) as excinfo:
        write_payload(payloads, "j-backstop", payload)

    assert "generic_api_key" in str(excinfo.value)
    assert list(payloads.iterdir()) == []


def test_the_protocol_change_bumped_the_prompt_version():
    """v2 is the forced-position protocol, and the bump is REQUIRED rather
    than tidy.

    A v1 line carries `vote_index` 0 or 1 too -- drawn under a random position
    -- so the resume key, which holds the index and the version and not the
    position, would match a v1 line against a v2 unit and skip it. The batch
    would report a clean resume while half the forced-position votes it was
    asked for were never bought, and the file would hold one random-position
    vote where the protocol says two forced ones.
    """
    assert JUDGE_PROMPT_VERSION == 2
    # Two positions, in the order the driver walks them: `vote_index` 0 is
    # `a_first` and 1 is `b_first`, which is what makes the index readable
    # off an old line without re-deriving anything.
    assert VOTE_POSITIONS == ("a_first", "b_first")


def test_the_pinned_judge_identity_constants():
    """Tasks 5 and 6 read these, and two of them are load-bearing.

    `judge_model_id` must be a PINNED variant -- luna/sol/terra are three
    models, not three names for one, and a floating alias is a moving oracle.
    The cap goes out as `max_completion_tokens`: the candidate arms need a
    litellm patch to rename `max_tokens`, and a judge call that shipped the
    old name would be silently uncapped.
    """
    assert JUDGE_MODEL_ID_DEFAULT == "openai.gpt-5.6-sol"
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


# --- the neutral-family guard ------------------------------------------------
#
# §4.3 makes a neutral judge MANDATORY, and the failure it forbids is the
# quietest one this harness can produce: a Claude judge returns well-formed
# verdicts, writes complete payloads, fills every column of the matrix and
# shifts every number in it the same way. Nothing downstream can see it -- the
# intervals are honest about sampling error and silent about bias -- so the
# refusal has to happen at the identity, before the first call.

#: Ids where the VENDOR NAMESPACE is the only signal: no compared-family token
#: appears anywhere in the name, so a guard matching tokens alone lets every one
#: of these through. `anthropic.opus-6` is the realistic shape -- a vendor ships
#: a model whose name never says "claude" -- and it is still a judge from the
#: family the eval is measuring. Keyed by the constant each one is caught by, so
#: the test can assert the table covers `NON_NEUTRAL_VENDOR_PREFIXES` whole.
PREFIX_ONLY_JUDGES = {
    "anthropic.": "anthropic.opus-6",
    "google.": "google.palm-2-unicorn",
    "nvidia.": "nvidia.mistral-nemo-12b",
    "moonshot.": "moonshot.moonshot-v1-128k",
}

#: Ids where the FAMILY TOKEN is the only signal: the vendor namespace is
#: neutral or simply somebody else's, which is what a re-host looks like. The
#: weights are what prefer their own backbone, not the string in front of the
#: dot, so a compared family served from another namespace is the same
#: self-preference path -- and it is the one a prefix-only guard cannot see.
TOKEN_ONLY_JUDGES = {
    "claude": "bedrock.claude-sonnet-5",
    "gemma": "openrouter.gemma-4-31b",
    "gemini": "vertex.gemini-3-pro",
    "nemotron": "together.nemotron-4-340b",
    "kimi": "groq.kimi-k2-instruct",
}


def test_a_judge_from_a_compared_family_is_refused_with_the_spec_section_named(
    monkeypatch,
):
    """All four compared families, in both the shapes they arrive in.

    Two match rules rather than one, and each table above is what makes the
    other one load-bearing. `anthropic.opus-6` carries no family token, so
    dropping the prefix rule admits a judge from the family Sonnet 5 belongs
    to; `bedrock.claude-sonnet-5` carries no compared vendor prefix, so
    dropping the token rule admits Claude itself under a re-host. A guard with
    either half missing passes a table full of biased numbers, and every one of
    them looks exactly like a clean one.

    The message NAMES the family it matched and NAMES §4.3, because the
    operator reading it typed a model id on purpose and needs to know which
    rule caught it and where that rule comes from. "invalid judge model" would
    read as a typo and get answered with a second guess.

    The env is cleared first: this refusal must not depend on what the shell
    was carrying, and an ambient override would otherwise turn the whole table
    green.
    """
    monkeypatch.delenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, raising=False)

    # The tables cover the constants WHOLE, so a family added to either tuple
    # without a case here fails at this assertion rather than going untested.
    assert set(PREFIX_ONLY_JUDGES) == set(NON_NEUTRAL_VENDOR_PREFIXES)
    assert set(TOKEN_ONLY_JUDGES) == set(NON_NEUTRAL_FAMILY_TOKENS)

    for family, model_id in {**PREFIX_ONLY_JUDGES, **TOKEN_ONLY_JUDGES}.items():
        with pytest.raises(NonNeutralJudge) as exc:
            assert_neutral_judge(model_id)
        message = str(exc.value)
        # QUOTED, so the family is named as a family. A bare `in` would pass on
        # the token cases against a message that only echoed the model id back.
        assert f"{family!r}" in message, message
        assert f"{model_id!r}" in message, message
        assert "§4.3" in message, message

    # Case-folded before either rule runs. `--judge-model` is typed by hand and
    # a capitalised vendor is a plausible way to type it.
    for shouted in ("ANTHROPIC.Claude-Sonnet-5", "Vertex.Gemini-3-Pro"):
        with pytest.raises(NonNeutralJudge):
            assert_neutral_judge(shouted)

    # The two that must pass. The default is what every unconfigured pass uses,
    # and Qwen is §4.3's named drop-in if residency policy changes -- a guard
    # that refused either one would be a guard nobody could run behind.
    assert assert_neutral_judge(JUDGE_MODEL_ID_DEFAULT) is None
    assert assert_neutral_judge("openai.gpt-5.6-sol") is None
    assert assert_neutral_judge("qwen.qwen3-235b-a22b-2507") is None


def test_the_env_override_admits_a_non_neutral_judge_with_a_loud_warning_and_nothing_else_does(  # noqa: E501
    monkeypatch,
):
    """Exactly `"1"` opens the door, and the door is never quiet.

    An override exists because a deliberate self-preference PROBE is a real
    experiment -- measuring how much a Claude judge inflates Sonnet 5 is how
    §4.3's rule gets re-confirmed on this endpoint -- and because a rule with
    no escape gets deleted rather than obeyed. What it must never become is a
    convenience: the returned text is carried into `warnings` and printed by
    the driver, so the pass that used it says so on the terminal and in its own
    result, and the numbers cannot be quoted as if a neutral judge produced
    them.

    `== "1"` and not truthiness, which is the half that actually protects
    anything. `BAKEOFF_ALLOW_NON_NEUTRAL_JUDGE=0` is what somebody writes to
    turn the override OFF, and under a presence check it turns it on -- the
    exact inversion, arrived at by a person trying to be careful.
    """
    monkeypatch.setenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, "1")

    warning = assert_neutral_judge("anthropic.claude-sonnet-5")

    assert warning is not None
    # LOUD: a banner a reader cannot skim past, the id, the rule, the variable
    # that admitted it, and the measured size of the effect -- 33.7% against
    # 14.13% is what makes "biased" a number rather than an adjective.
    assert warning.startswith("NON-NEUTRAL JUDGE")
    assert "'anthropic.claude-sonnet-5'" in warning
    assert "§4.3" in warning
    assert ALLOW_NON_NEUTRAL_JUDGE_ENV in warning
    assert "33.7" in warning and "14.13" in warning

    # The override says nothing about a neutral judge: no warning, no text to
    # carry, nothing on the terminal.
    assert assert_neutral_judge(JUDGE_MODEL_ID_DEFAULT) is None

    # NOTHING ELSE DOES. Every one of these is a way somebody actually spells
    # "on", and every one of them refuses.
    for value in ("0", "", "true", "TRUE", "yes", "on", "01", " 1", "1 ", "2"):
        monkeypatch.setenv(ALLOW_NON_NEUTRAL_JUDGE_ENV, value)
        with pytest.raises(NonNeutralJudge):
            assert_neutral_judge("anthropic.claude-sonnet-5")

    monkeypatch.delenv(ALLOW_NON_NEUTRAL_JUDGE_ENV)
    with pytest.raises(NonNeutralJudge):
        assert_neutral_judge("anthropic.claude-sonnet-5")


# --- the live completion seam ------------------------------------------------
#
# CONSTRUCTION-ONLY, and that is a safety property rather than a convenience.
# Every test below drives `live_completion` to the exact point where the real
# code would open a socket, and a recording stand-in for `litellm.Router` is
# what stands there instead. A unit test here that reached Bedrock would be a
# PAID test in the default run -- which is the thing the `judge_live` marker
# and the section 6.6 gate's selector exist to prevent, so it cannot be the
# thing this file does while asserting the marker works.

#: The env var the harness carries the mantle bearer token under. Written out
#: rather than imported from `scripts.smoke_bedrock`, because this name is the
#: contract: `config/litellm_config.yaml` names it on every mantle deployment,
#: and a rename that these tests followed automatically would be a rename that
#: broke the proxy with the suite still green.
MANTLE_ENV_NAME = "BAKEOFF_MANTLE_TOKEN"

#: `bakeoff/`, the directory pytest is run from and the one `pythonpath = ["."]`
#: puts on the path. Two files below are asserted as SOURCE TEXT, so they are
#: read rather than imported.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The name LiteLLM itself consults, and the one that must never be set: the
#: bedrock/ handler falls back to it when a deployment has no api_key, so a
#: process holding it bearer-authenticates every SigV4 arm and fails them all
#: with `bedrock:CallWithBearerToken` (smoke_bedrock.py:125).
LITELLM_BEARER_ENV_NAME = "AWS_BEARER_TOKEN_BEDROCK"

#: Shaped like nothing real. Its job is to be FINDABLE in the constructed
#: deployment: "not in os.environ" passes just as happily for a token that
#: never reached the router at all, so the bearer-env test asserts both halves.
FAKE_MANTLE_TOKEN = "fake-mantle-bearer-token-not-a-credential"

STUB_REPLY = '{"verdict": "A", "reasoning": "stubbed; no model was called"}'

#: What a re-minted token looks like here. Distinguishable from
#: `FAKE_MANTLE_TOKEN` on purpose: "the router was rebuilt" is satisfied just
#: as happily by a rebuild around the SAME expired credential, which is the
#: half of the fix that would leave a batch retrying into the same 401.
FRESH_TOKEN = "freshly-minted-mantle-token"


class _StatusOnlyAuthError(Exception):
    """A 401 the way a generic `APIError` carries one: a status code and
    nothing else in the name.

    Some routes on this endpoint do not raise litellm's own
    `AuthenticationError` -- they raise `APIError` with `status_code` set, and
    a classifier that read only the class name would let an expired token look
    like a model failure and take the retry budget with it.
    """

    def __init__(self, status_code: int = 401):
        super().__init__(f"HTTP {status_code}: the security token has expired")
        self.status_code = status_code


class AuthenticationError(Exception):
    """Named exactly as litellm names it, and carrying NO status attribute.

    The other half of the classifier. `bakeoff.judge` cannot catch litellm's
    class without importing litellm at module scope -- which the payload
    builders and the leak tests would then pay for -- so it matches on the
    name, and this stand-in is what pins that path independently of the status
    one.
    """


class PermissionDeniedError(Exception):
    """litellm's 403 -- the other name the same dead credential arrives
    under, and a separate class here so the set is pinned name by name."""


class _SubclassedAuthError(AuthenticationError):
    """A library subclassing its own auth error. The classifier walks the MRO
    rather than reading `type(exc).__name__`, so the name on a base class
    still counts -- otherwise one litellm release adding a subclass turns the
    whole fix off silently."""


#: The usage block a mantle completion comes back carrying. Three distinct
#: numbers, none of them a multiple of another, so an accumulator that added
#: the wrong field into the wrong total would not land on a plausible sum.
STUB_PROMPT_TOKENS = 11
STUB_COMPLETION_TOKENS = 7
STUB_TOTAL_TOKENS = 18


def _stub_response(text: str, usage: Any = "default"):
    """The attribute hops `live_completion` makes into a litellm response.

    Deliberately not a real `ModelResponse`: building one would couple these
    tests to the pinned version's response model, and the contract under test
    is only that the reply text is read out of `choices[0].message.content`
    and the token counts out of `usage`.

    `usage=None` is the response a route that reports none sends back, which
    is a real shape rather than a hypothetical: the totals have to say how
    many calls they could not see rather than silently under-report.
    """
    if usage == "default":
        usage = SimpleNamespace(
            prompt_tokens=STUB_PROMPT_TOKENS,
            completion_tokens=STUB_COMPLETION_TOKENS,
            total_tokens=STUB_TOTAL_TOKENS,
        )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=usage,
    )


@pytest.fixture
def dotenv_settled():
    """Import litellm BEFORE any test here touches the environment.

    `import litellm` calls `load_dotenv()` -- so `bakeoff/.env` does not reach
    `os.environ` until it has run. On any machine that has run the live smoke
    that file carries `AWS_BEARER_TOKEN_BEDROCK`, so a test that cleared the
    variable first would watch litellm put it straight back and fail for a
    reason that has nothing to do with the code under test. Measured here on
    2026-08-18: this test file passed as a whole and failed when its bearer
    case was run alone, which is the same import-order accident
    `scrub_placeholders` documents costing an AWS_PROFILE.
    """
    import litellm  # noqa: F401 -- imported for load_dotenv's side effect


@pytest.fixture
def routers(monkeypatch, dotenv_settled):
    """Replace `litellm.Router` with a recorder; yield the list it builds into.

    Patched on the litellm module rather than on `bakeoff.judge`, because the
    import is deliberately inside the closure -- patching a name judge.py never
    binds at module level would stub nothing and let the real Router through.
    """
    import litellm

    built = []

    class _RecordingRouter:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.calls = []
            built.append(self)

        def completion(self, **kwargs):
            self.calls.append(kwargs)
            return _stub_response(STUB_REPLY)

    monkeypatch.setattr(litellm, "Router", _RecordingRouter)
    return built


@pytest.fixture
def scripted_routers(monkeypatch, dotenv_settled):
    """`litellm.Router` replaced by a recorder driven by a per-CALL script.

    Per call rather than per router, because the behaviour under test spans
    both: one call fails on the first router, the next lands on a router the
    closure rebuilt. A script of "what the i-th completion does" says that in
    one list, and it is the only shape that can express a batch whose SECOND
    credential also expires an hour later.
    """
    import litellm

    built = []

    def install(script):
        """`script[i]` is what the i-th completion call does: an exception
        instance to raise, or `None` to answer. Past the end, it answers."""
        calls = iter(list(script))

        class _ScriptedRouter:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs
                self.calls = []
                built.append(self)

            @property
            def api_key(self):
                return self.init_kwargs["model_list"][0]["litellm_params"][
                    "api_key"
                ]

            def completion(self, **kwargs):
                self.calls.append(kwargs)
                error = next(calls, None)
                if error is not None:
                    raise error
                return _stub_response(STUB_REPLY)

        monkeypatch.setattr(litellm, "Router", _ScriptedRouter)
        return built

    return install


@pytest.fixture
def minted(monkeypatch, dotenv_settled):
    """Every mint the closure asks for, and a distinguishable token each time.

    Patched on `scripts.smoke_bedrock` because that module is where the
    harness's ONE token derivation lives -- `bakeoff.judge` calls it rather
    than reimplementing it, so this is the seam a re-mint has to come through.
    """
    import scripts.smoke_bedrock as smoke_bedrock

    regions = []

    def _mint(region):
        regions.append(region)
        return f"{FRESH_TOKEN}-{len(regions)}"

    monkeypatch.setattr(smoke_bedrock, "derive_mantle_token", _mint)
    return regions


@pytest.fixture
def mantle_token(monkeypatch, dotenv_settled):
    """A token under the harness's own name, and no AWS-named copy anywhere.

    Both halves are set explicitly rather than inherited, so these tests say
    the same thing on a laptop holding live credentials and in CI holding
    none.
    """
    monkeypatch.setenv(MANTLE_ENV_NAME, FAKE_MANTLE_TOKEN)
    monkeypatch.delenv(LITELLM_BEARER_ENV_NAME, raising=False)


def test_live_completion_sends_max_completion_tokens_not_max_tokens(
    mantle_token, routers
):
    """The cap's SPELLING, as sent, plus the rest of `JUDGE_SAMPLING`.

    The candidate arms reach `max_completion_tokens` through
    `bakeoff.litellm_patches`, which renames `max_tokens` -- and that module is
    never imported in process (it patches litellm globally on import), so this
    router is unpatched by design and has to spell the cap by hand. A judge
    call that shipped `max_tokens` to the /openai/v1 route would 400 at best
    and go out silently uncapped at worst; `smoke_bedrock.live` reproduces the
    same rename by hand at line 379 for exactly this reason.
    """
    complete = live_completion()
    assert complete("RENDERED PROMPT") == STUB_REPLY

    [router] = routers
    [call] = router.calls
    assert call["max_completion_tokens"] == 4096
    assert (
        call["max_completion_tokens"]
        == JUDGE_SAMPLING["max_completion_tokens"]
    )
    assert "max_tokens" not in call
    assert call["temperature"] == JUDGE_SAMPLING["temperature"] == 0.0
    assert call["model"] == JUDGE_MODEL_ID_DEFAULT
    # Verbatim, in one user turn: the rendered prompt IS the prompt whose sha
    # the record attests to, so a wrapper or a system split here would make
    # `judge_prompt_sha` describe text no model saw.
    assert call["messages"] == [{"role": "user", "content": "RENDERED PROMPT"}]


def test_live_completion_never_sets_the_litellm_bearer_env(
    monkeypatch, mantle_token, routers
):
    """The AWS-named bearer variable is gone afterwards, however it got there.

    `AWS_BEARER_TOKEN_BEDROCK` is what LiteLLM's bedrock/ handler falls back to
    for any deployment carrying no api_key. A process holding it
    bearer-authenticates every bedrock-runtime arm and fails them all with
    `bedrock:CallWithBearerToken` while the judge, which passes its key
    literally, stays green -- a credential fault that reads as three broken
    models.

    Set here on purpose rather than merely left unset: the judge does not have
    to SET it to hold it, because `import litellm` runs `load_dotenv()` and
    `bakeoff/.env` carries that name. Asserting "we never assigned it" would
    be a test of an irrelevant fact. The api_key assertion is the other half --
    absence from the environment means nothing unless the token demonstrably
    reached the deployment in memory.
    """
    monkeypatch.setenv(LITELLM_BEARER_ENV_NAME, "ambient-token-from-a-dotenv")

    complete = live_completion()
    complete("RENDERED PROMPT")

    assert LITELLM_BEARER_ENV_NAME not in os.environ

    [router] = routers
    [deployment] = router.init_kwargs["model_list"]
    assert deployment["litellm_params"]["api_key"] == FAKE_MANTLE_TOKEN
    assert deployment["litellm_params"]["api_base"] == MANTLE_BASE
    # `openai/` twice is neither a typo nor cosmetic. The prefix picks
    # LiteLLM's OpenAI-compatible chat-completions handler; `openai.` is
    # Bedrock's vendor namespace inside the model id. Without the prefix the
    # call falls through to the bedrock/ SigV4 handler -- which reaches for
    # AWS_BEARER_TOKEN_BEDROCK, the variable this test has just proved is
    # gone, and then signs a request for an endpoint that does not take one.
    assert deployment["litellm_params"]["model"] == (
        f"openai/{JUDGE_MODEL_ID_DEFAULT}"
    )


def test_an_ambient_aws_named_token_is_adopted_rather_than_discarded(
    monkeypatch, mantle_token, routers
):
    """Scrubbing an operator's only credential would be worse than holding it.

    A token pasted under the AWS name is a WORKING credential -- the console
    hands Bedrock API keys over under that name, which is why
    `normalize_mantle_token` exists. The sequence therefore relocates it onto
    the name the harness reads and then removes the AWS-named copy, so the
    judge runs on the credential the operator supplied and the SigV4 arms stay
    on SigV4.
    """
    pasted = "a-real-token-pasted-under-the-aws-name"
    monkeypatch.delenv(MANTLE_ENV_NAME, raising=False)
    monkeypatch.setenv(LITELLM_BEARER_ENV_NAME, pasted)

    live_completion()("RENDERED PROMPT")

    [router] = routers
    [deployment] = router.init_kwargs["model_list"]
    assert deployment["litellm_params"]["api_key"] == pasted
    assert LITELLM_BEARER_ENV_NAME not in os.environ


def test_the_router_is_not_built_until_the_first_call(monkeypatch, routers):
    """A fully gate-decided batch must not mint a token or build a router.

    The `task_resolver` pattern (`scripts/grade.py:317`): the expensive setup
    lives inside the closure so that a batch whose comparisons are all decided
    by the deterministic ladder pays nothing for a judge it never asks. Minting
    at construction would also start the ~1h credential clock before the first
    call, so a long grading pass would reach the judge with a dead token.

    The failure when no credential exists must NAME the variable -- a
    `KeyError` or LiteLLM's own "Invalid API Key format" sends the operator to
    the config rather than to `aws sso login`.
    """
    import scripts.smoke_bedrock as smoke_bedrock

    monkeypatch.delenv(MANTLE_ENV_NAME, raising=False)
    # Both names, or `normalize_mantle_token` adopts the ambient AWS-named copy
    # this machine's .env supplies and there is no missing-credential case left
    # to test.
    monkeypatch.delenv(LITELLM_BEARER_ENV_NAME, raising=False)
    minted = []

    def _no_token(region):
        minted.append(region)
        return None

    monkeypatch.setattr(smoke_bedrock, "derive_mantle_token", _no_token)

    complete = live_completion()
    assert routers == [], "a router was built before anything was judged"
    assert minted == [], "a token was minted before anything was judged"

    with pytest.raises(RuntimeError, match=MANTLE_ENV_NAME):
        complete("this must never reach a model")

    assert minted == ["us-east-1"], "the default region reaches the minter"
    assert routers == [], "a router was built without a credential to use"


def test_a_missing_scripts_package_fails_at_construction_not_at_the_first_vote(
    monkeypatch, routers
):
    """Mis-wiring is reported before the batch, not after it is paid for.

    `live_completion` reaches out of the package into `scripts.smoke_bedrock`
    for the credential helpers, which resolves only with the repo root on
    `sys.path` -- every script inserts it (`grade.py:76-77`) and a driver that
    forgets is a one-line mistake. Left inside the closure, that import raises
    `ModuleNotFoundError: scripts` at the FIRST VOTE: after a grading pass has
    already run and spent on the candidate arms, which is the worst moment in
    the pass to learn about a typo. Resolved at construction it costs one
    import and fails while the run has spent nothing.

    `None` in `sys.modules` is the import system's own "this is unavailable"
    sentinel, and is used rather than editing `sys.path`, which would leave
    the module cached and importable anyway.
    """
    monkeypatch.setitem(sys.modules, "scripts", None)

    with pytest.raises(ImportError):
        live_completion()

    assert routers == []


def test_the_router_is_built_once_and_carries_transport_retries(
    mantle_token, routers
):
    """One router across every vote, and `num_retries` is the TRANSPORT count.

    Built once because the two votes over one comparison are two calls, and a
    router per call would re-mint the token and re-resolve the deployment on
    every vote of a 2,400-run batch.

    `num_retries=2` retries a connection or a 5xx. It is a different counter
    from the malformed-verdict retries in `_ask_and_parse`, and conflating them
    hides which one a batch is burning: a model that cannot produce JSON and a
    model that cannot be reached fail the same number of times and need
    opposite responses.

    `disable_cooldowns` is asserted beside it because the two interact, and
    the interaction is what makes an auth failure loud. Measured against
    litellm 1.95.0 and written up in `config/litellm_config.yaml`'s
    router_settings: `_should_cooldown_deployment`'s
    `litellm._should_retry(status_code) is False` branch is unguarded, and
    _should_retry(401) and (403) are both False -- so ONE auth failure cools
    this deployment down. It is a single-deployment group, so there is nothing
    to fail over to; the retries above return `RouterRateLimitError`, a plain
    ValueError with no status code, and the operator reads "No deployments
    available" instead of the expired token that actually stopped the batch.
    """
    complete = live_completion()
    complete("first vote")
    complete("second vote")

    assert len(routers) == 1
    [router] = routers
    assert len(router.calls) == 2
    assert router.init_kwargs["num_retries"] == 2
    assert router.init_kwargs["disable_cooldowns"] is True


@pytest.mark.parametrize(
    "error",
    [
        _StatusOnlyAuthError(401),
        _StatusOnlyAuthError(403),
        AuthenticationError("the security token included in the request is "
                            "expired"),
        PermissionDeniedError("not authorized to perform bedrock:InvokeModel"),
        _SubclassedAuthError("a release added a subclass"),
    ],
    ids=["status-401", "status-403", "authentication", "permission-denied",
         "subclass"],
)
def test_a_token_that_died_mid_batch_is_re_minted_once_and_the_call_retried(
    error, mantle_token, minted, scripted_routers
):
    """The ~1h window against a multi-hour pass, at the layer that can fix it.

    The Identity Center session policy caps the mantle token at about an hour,
    a real 60-task pass is ~7,200 pairwise calls (two forced positions per
    comparison) plus ~2,400 rubric and runs for many more hours than that, and
    the router is built once on the first vote. So the token dies in the middle
    of every real batch. Left to propagate, every remaining unit fails on its
    own dead credential and the operator gets one hour of judged units per
    invocation plus thousands of error lines.

    One rebuild around a FRESHLY MINTED token, then the same call again. The
    fresh token is asserted on the second deployment because "a second router
    was built" is satisfied just as well by a rebuild around the expired copy
    still sitting in `MANTLE_ENV` -- which would retry straight into the same
    401.

    Both signals are parametrized because one route gives one: litellm raises
    `AuthenticationError`/`PermissionDeniedError` on this handler, and some
    paths raise a generic `APIError` carrying only `status_code`.
    """
    built = scripted_routers([error])

    complete = live_completion()
    assert complete("RENDERED PROMPT") == STUB_REPLY

    assert len(built) == 2, "the dead router was not replaced"
    assert minted == ["us-east-1"], "no fresh token was minted"
    assert built[0].api_key == FAKE_MANTLE_TOKEN
    assert built[1].api_key == f"{FRESH_TOKEN}-1"
    assert len(built[0].calls) == len(built[1].calls) == 1
    # Verbatim on the retry: the record's `judge_prompt_sha` attests to the
    # text the model saw, and a rebuild that re-rendered anything would make
    # the sha describe a prompt the second call never sent.
    assert built[1].calls[0]["messages"] == [
        {"role": "user", "content": "RENDERED PROMPT"}
    ]

    # And the rebuilt router is CACHED. A mint per vote would spend the whole
    # batch on credentials and start a new clock on every one of them.
    assert complete("second vote") == STUB_REPLY
    assert len(built) == 2
    assert minted == ["us-east-1"]


def test_a_second_consecutive_auth_failure_propagates_rather_than_churning(
    mantle_token, minted, scripted_routers
):
    """A token minted seconds ago that still 401s is a real credential problem
    -- a revoked role, the wrong region, a principal that never had access --
    and the design's answer to that is to fail loud. A closure that kept
    re-minting would turn it into an unbounded mint loop against every unit in
    the batch, and the error an operator finally read would be about the last
    attempt rather than the first."""
    built = scripted_routers([_StatusOnlyAuthError(401),
                              _StatusOnlyAuthError(401)])

    with pytest.raises(_StatusOnlyAuthError):
        live_completion()("RENDERED PROMPT")

    assert len(built) == 2, "more than one rebuild for one call"
    assert minted == ["us-east-1"]


def test_each_call_carries_its_own_rebuild_so_a_long_batch_survives_two(
    mantle_token, minted, scripted_routers
):
    """The budget is per CALL, not per closure. A pass long enough to outlive
    two tokens is the ordinary case at ~9,600 calls (7,200 pairwise plus
    2,400 rubric), so a one-shot rebuild
    would buy the second hour and no more."""
    built = scripted_routers([
        _StatusOnlyAuthError(401), None, _StatusOnlyAuthError(401),
    ])

    complete = live_completion()
    assert complete("first hour") == STUB_REPLY
    assert complete("third hour") == STUB_REPLY

    assert len(built) == 3
    assert minted == ["us-east-1", "us-east-1"]
    assert [router.api_key for router in built] == [
        FAKE_MANTLE_TOKEN, f"{FRESH_TOKEN}-1", f"{FRESH_TOKEN}-2",
    ]


def test_a_failure_that_is_not_an_auth_failure_never_mints_a_credential(
    mantle_token, minted, scripted_routers
):
    """A connection reset, a 5xx and a rate limit are the TRANSPORT retries
    `Router(num_retries=2)` already owns. Answering them with a fresh
    credential hides a broken endpoint behind a token churn, and doubles the
    call count of every failing unit for nothing."""
    built = scripted_routers([RuntimeError("connection reset by peer")])

    with pytest.raises(RuntimeError, match="connection reset"):
        live_completion()("RENDERED PROMPT")

    assert len(built) == 1, "a non-auth failure rebuilt the router"
    assert minted == [], "a non-auth failure minted a credential"


def test_an_auth_failure_with_no_replacement_token_raises_the_auth_error(
    monkeypatch, mantle_token, scripted_routers
):
    """`aws sso login` has expired too, so there is nothing to retry with. The
    operator must see the 401 that actually stopped the batch -- not a "no
    mantle credential: set BAKEOFF_MANTLE_TOKEN" message about a variable that
    is still set, which sends them looking for a config error that is not
    there."""
    import scripts.smoke_bedrock as smoke_bedrock

    monkeypatch.setattr(smoke_bedrock, "derive_mantle_token", lambda r: None)
    built = scripted_routers([_StatusOnlyAuthError(401)])

    with pytest.raises(_StatusOnlyAuthError):
        live_completion()("RENDERED PROMPT")

    assert len(built) == 1


def test_usage_totals_accumulate_across_calls_and_survive_the_auth_retry(
    mantle_token, minted, scripted_routers
):
    """What a ~40-hour paid pass actually spent, counted where the wire is.

    Nothing else in the harness can reconstruct it. The judgment file records
    one line per unit and no token counts, `costs.PRICE_BOOK` deliberately has
    no entry for the judge models (mantle pricing is unpublished), and the
    router is the only object that sees a response at all -- so a total not
    accumulated here is a number that does not exist anywhere afterwards.

    THE AUTH RETRY IS THE CASE THAT DECIDES THE SHAPE. One logical call that
    401s makes TWO completion requests, and a count taken per `complete()`
    rather than per request would report the batch as having made half the
    calls it made -- under-reporting spend, which is the one direction a spend
    figure must never fail in. So: two prompts, three requests, and the token
    totals over the two that came back with a response.
    """
    built = scripted_routers([_StatusOnlyAuthError(401)])
    totals = new_usage_totals()

    complete = live_completion(usage_totals=totals)
    assert complete("first vote") == STUB_REPLY
    assert complete("second vote") == STUB_REPLY

    assert len(built) == 2 and minted == ["us-east-1"]
    assert totals["calls"] == 3, "the auth-retried call counted as one request"
    assert totals["auth_refreshes"] == 1
    assert totals["prompt_tokens"] == 2 * STUB_PROMPT_TOKENS
    assert totals["completion_tokens"] == 2 * STUB_COMPLETION_TOKENS
    assert totals["total_tokens"] == 2 * STUB_TOTAL_TOKENS
    assert totals["calls_without_usage"] == 0


def test_a_response_carrying_no_usage_is_counted_rather_than_dropped(
    mantle_token, monkeypatch, dotenv_settled
):
    """A route that reports no usage makes the totals an UNDER-count, and the
    printout has to say so rather than quietly report the rest.

    Silence here is the failure: an operator reading 1,200 calls and 400,000
    tokens has no way to tell a batch where every call reported usage from one
    where a third of them reported none, and the second is a far bigger bill
    than the number says.
    """
    import litellm

    class _NoUsageRouter:
        def __init__(self, **kwargs):
            pass

        def completion(self, **kwargs):
            return _stub_response(STUB_REPLY, usage=None)

    monkeypatch.setattr(litellm, "Router", _NoUsageRouter)
    totals = new_usage_totals()

    live_completion(usage_totals=totals)("RENDERED PROMPT")

    assert totals["calls"] == 1
    assert totals["calls_without_usage"] == 1
    assert totals["prompt_tokens"] == 0
    assert totals["total_tokens"] == 0


def test_a_half_readable_usage_block_is_both_counted_and_contributed(
    mantle_token, monkeypatch, dotenv_settled
):
    """A partial block is the case where the two halves of this accounting pull
    apart, and both have to happen.

    A response carrying `prompt_tokens` and nothing else is real spend on the
    prompt side and unmeasured spend on the completion side. Folding in only
    what was readable and leaving `calls_without_usage` at zero reports a total
    that is short by the whole completion half while claiming to be a
    measurement -- which is precisely the silence the counter exists to break,
    and it is worse than reporting nothing because it looks complete. Dropping
    the readable number instead would throw away spend that WAS measured.

    So: the prompt tokens land, and the call is counted as incomplete.
    """
    import litellm

    class _PartialUsageRouter:
        def __init__(self, **kwargs):
            pass

        def completion(self, **kwargs):
            return _stub_response(
                STUB_REPLY,
                usage=SimpleNamespace(prompt_tokens=STUB_PROMPT_TOKENS),
            )

    monkeypatch.setattr(litellm, "Router", _PartialUsageRouter)
    totals = new_usage_totals()

    live_completion(usage_totals=totals)("RENDERED PROMPT")

    assert totals["calls"] == 1
    assert totals["prompt_tokens"] == STUB_PROMPT_TOKENS, "readable spend lost"
    assert totals["completion_tokens"] == 0
    assert totals["total_tokens"] == 0
    assert totals["calls_without_usage"] == 1, (
        "a total missing the completion half reported itself as a measurement"
    )


def test_the_auth_classifier_answers_both_callers_and_never_reads_the_message():
    """`is_auth_failure` is public because it has a second caller with a
    different stake, and one narrowness rule has to serve both.

    Here a false positive costs one mint and one retry. In
    `scripts/judge.py` it costs the operator the abort message: a run of failed
    units classified as auth prints the credential paragraph, and the two fixes
    for a data failure -- exclude the task, raise the limit -- go unsaid. A
    false NEGATIVE swaps the two. So the rule stays exactly as narrow as it was
    and both callers ask the same function rather than each carrying a copy.

    THE TEXT IS NEVER READ, which is the property the driver depends on most.
    The last two cases below are the ones a message matcher gets wrong in both
    directions at once: a data failure whose text says "token", and a real 401
    from a route whose text says nothing about auth.
    """
    class _VendorSubclass(AuthenticationError):
        """A library release that subclasses its own auth error."""

    assert is_auth_failure(_StatusOnlyAuthError(401))
    assert is_auth_failure(_StatusOnlyAuthError(403))
    assert is_auth_failure(AuthenticationError("no status attribute at all"))
    assert is_auth_failure(PermissionDeniedError("the 403 by name"))
    assert is_auth_failure(_VendorSubclass("through the MRO"))

    # The transport failures `Router(num_retries=2)` already owns, and the
    # data-shaped ones the driver must report as data.
    assert not is_auth_failure(_StatusOnlyAuthError(429))
    assert not is_auth_failure(_StatusOnlyAuthError(503))
    assert not is_auth_failure(RuntimeError("connection reset by peer"))
    assert not is_auth_failure(MalformedVerdict("no JSON in the reply"))
    assert not is_auth_failure(ValueError("expired token in the diff header"))


def test_verify_logger_selector_excludes_judge_live():
    """The section 6.6 gate is offline, no credentials, no spend -- keep it so.

    `verify_logger.py` runs `-m "integration and not task_image"`, and Task 8's
    live judge test is marked `integration` too. Without `not judge_live` in
    that selector the gate an operator runs BEFORE a collection makes a paid
    Bedrock call, in the one check whose docstring promises it will not.

    Asserted against the source text rather than by running the gate, because
    what is under test is the string an operator would read in the file.
    """
    source = (REPO_ROOT / "scripts" / "verify_logger.py").read_text(
        encoding="utf-8"
    )

    assert "integration and not task_image and not judge_live" in source
    # The old selector must be GONE, not merely joined by the new one: a stale
    # copy left in a second CHECKS entry or in the comment above it is how the
    # gate ends up documented one way and run another.
    assert 'integration and not task_image"' not in source


def test_the_judge_live_marker_is_registered_beside_task_image():
    """An unregistered marker is a warning, not an error -- so it stays broken.

    Task 8 carries `pytest.mark.judge_live`, and pytest applies an unknown mark
    happily with a `PytestUnknownMarkWarning` nobody reads in a green run. The
    registration is also where the "subset of `integration`, never alone"
    contract is written down, which is the sentence that keeps a future author
    from marking a paid test `judge_live` only and having it run by default.
    """
    config = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    markers = config["tool"]["pytest"]["ini_options"]["markers"]

    judge_live = [m for m in markers if m.startswith("judge_live:")]
    assert len(judge_live) == 1, markers
    assert judge_live[0].split(":", 1)[1].strip()
