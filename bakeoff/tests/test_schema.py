import json

from bakeoff.schema import (
    SCHEMA_VERSION,
    CacheState,
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


def test_an_undetermined_cache_state_survives_the_json_boundary():
    """`warm` has three states and only two of them are booleans. JSON turns
    None into null and back, but a decoder that coerced -- or a reader that
    treated a missing key as False -- would put the record back to claiming
    every unmeasured run was cold, which is the defect 2.1.0 exists to end."""
    record = RunRecord(
        run_id="r-003",
        task_id="t-001",
        task_version=1,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-04T00:00:00Z",
        finished_at="2026-08-04T00:05:00Z",
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.CRASH,
        turns_used=0,
        cache_state=CacheState(warm=None, prior_same_task_run_id="r-002"),
    )
    restored = RunRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert restored.cache_state.warm is None
    assert restored.cache_state.prior_same_task_run_id == "r-002"
    assert restored == record


def test_a_schema_2_0_0_record_still_loads_with_its_own_version():
    """Records written before cache_state was measured keep saying 2.0.0, and
    that string is how a reader knows their `warm: false` was a default rather
    than an observation. Loading must not silently restamp it."""
    old = {
        "run_id": "r-old",
        "task_id": "t-001",
        "task_version": 1,
        "model": "claude-sonnet-5",
        "harness": "claude-code",
        "sample_index": 0,
        "started_at": "2026-08-04T00:00:00Z",
        "finished_at": "2026-08-04T00:05:00Z",
        "outcome": "failed",
        "terminated_by": "agent_finish",
        "turns_used": 7,
        "schema_version": "2.0.0",
        "cache_state": {"warm": False, "prior_same_task_run_id": None},
    }
    restored = RunRecord.from_dict(old)
    assert restored.schema_version == "2.0.0"
    assert restored.cache_state.warm is False


def test_a_record_from_a_later_schema_loads_without_its_unknown_fields():
    """Unknown TOP-LEVEL keys were always filtered; nested classes were built
    with a bare `klass(**data)`, so one field added to TokenUsage, Versions or
    CacheState by a later schema raised TypeError out of EventLog.read_run and
    cost the whole record. Every bump since 2.2.0 has added nested fields.

    Dropping what this version cannot represent is right here and wrong at the
    top of an analysis: `schema_version` survives, so the reader can still tell
    it is holding a newer record and decide. Raising decides for it, in the
    direction of losing the data.
    """
    future = {
        "run_id": "r-future",
        "task_id": "t-001",
        "task_version": 1,
        "model": "claude-sonnet-5",
        "harness": "claude-code",
        "sample_index": 0,
        "started_at": "2026-08-04T00:00:00Z",
        "finished_at": "2026-08-04T00:05:00Z",
        "outcome": "failed",
        "terminated_by": "agent_finish",
        "turns_used": 5,
        "schema_version": "9.9.9",
        "a_field_from_the_future": True,
        "tokens": {"input": 10, "cache_write_2h": 999},
        "cache_state": {"warm": True, "evicted_by": "someone"},
        "per_turn": [
            {
                "turn": 1,
                "tokens": {"input": 10, "cache_write_2h": 999},
                "cost_usd": 0.5,
                "inference_ms": 1,
                "tool_exec_ms": 0,
                "a_future_turn_field": 1,
            }
        ],
    }
    restored = RunRecord.from_dict(future)

    assert restored.schema_version == "9.9.9", "the reader must still see this"
    assert restored.tokens.input == 10
    assert restored.cache_state.warm is True
    assert restored.per_turn[0].cost_usd == 0.5


def test_the_api_call_count_and_the_record_count_are_both_stored():
    """`turns_used` counts API calls as of 3.0.0; Claude Code writes one
    transcript record per content block. Storing only one of the two is what
    let a 2x token inflation sit in every record unnoticed -- their difference
    IS the collapse, and it has to be readable without the transcript."""
    record = RunRecord(
        run_id="r-004",
        task_id="t-001",
        task_version=1,
        model="claude-sonnet-5",
        harness="claude-code",
        sample_index=0,
        started_at="2026-08-04T00:00:00Z",
        finished_at="2026-08-04T00:05:00Z",
        outcome=Outcome.FAILED,
        terminated_by=TerminationReason.AGENT_FINISH,
        turns_used=5,
        assistant_records=7,
        turns_streamed=5,
    )
    restored = RunRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert (restored.turns_used, restored.assistant_records) == (5, 7)
    assert restored.turns_streamed == 5


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


def test_schema_3_9_0_adds_the_provider_fields_with_no_claim_defaults():
    from bakeoff.schema import Versions
    import dataclasses

    assert SCHEMA_VERSION == "3.9.0"
    fields = {f.name: f for f in dataclasses.fields(RunRecord)}
    assert fields["terminal_native_finish_reason"].default is None
    assert fields["cost_usd_provider"].default is None
    assert fields["upstream_providers"].default_factory() == []
    assert {f.name: f.default for f in dataclasses.fields(Versions)}["provider_route"] == ""
