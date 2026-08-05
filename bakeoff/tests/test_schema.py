import json

from bakeoff.schema import (
    SCHEMA_VERSION,
    Outcome,
    RunRecord,
    TerminationReason,
    TokenUsage,
)


def test_token_usage_defaults_to_zero():
    usage = TokenUsage()
    assert usage.input == 0
    assert usage.output == 0
    assert usage.reasoning == 0
    assert usage.cache_read == 0
    assert usage.cache_write == 0


def test_token_usage_adds():
    a = TokenUsage(input=10, output=5, cache_read=100)
    b = TokenUsage(input=3, output=2, cache_write=50)
    total = a + b
    assert total.input == 13
    assert total.output == 7
    assert total.cache_read == 100
    assert total.cache_write == 50


def test_run_record_round_trips_through_json():
    record = RunRecord(
        run_id="r-001",
        task_id="t-001",
        task_version=1,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-04T00:00:00Z",
        finished_at="2026-08-04T00:05:00Z",
        outcome=Outcome.RESOLVED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=7,
    )
    blob = json.dumps(record.to_dict())
    restored = RunRecord.from_dict(json.loads(blob))
    assert restored == record
    assert restored.schema_version == SCHEMA_VERSION


def test_run_record_stamps_schema_version_automatically():
    record = RunRecord(
        run_id="r-002",
        task_id="t-001",
        task_version=1,
        model="gemma-4-31b",
        harness="claude-code",
        sample_index=1,
        started_at="2026-08-04T00:00:00Z",
        finished_at="2026-08-04T00:01:00Z",
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.TURNS,
        turns_used=40,
    )
    assert record.schema_version == SCHEMA_VERSION
