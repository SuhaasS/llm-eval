import gzip
import json
from datetime import UTC, datetime

import pytest

from bakeoff.wire import BakeoffCallback, WireLogger


def test_writes_gzipped_jsonl(tmp_path):
    path = tmp_path / "wire.jsonl.gz"
    logger = WireLogger(path)
    logger.log_call(
        request={"model": "gemma-4-31b", "messages": [{"role": "user", "content": "hi"}]},
        response={"choices": [{"message": {"content": "hello"}}]},
        metadata={"run_id": "r-1", "turn": 1},
    )
    logger.close()

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        entries = [json.loads(line) for line in handle]
    assert len(entries) == 1
    assert entries[0]["request"]["model"] == "gemma-4-31b"


def test_preserves_raw_completion_before_parsing(tmp_path):
    """Spec section 6.2: the raw completion must survive, or a malformed
    tool call can never be diagnosed after the fact."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    raw = '{"name": "Edit", "input": {broken json'
    logger.log_call(
        request={"model": "kimi-k2-5"},
        response={"raw_completion": raw, "parse_error": "unterminated object"},
        metadata={"run_id": "r-1", "turn": 4},
    )
    logger.close()
    assert logger.entries()[0]["response"]["raw_completion"] == raw


def test_captures_tool_schemas_and_system_prompt(tmp_path):
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.log_call(
        request={
            "model": "gemma-4-31b",
            "system": "You are a coding agent.",
            "tools": [{"name": "Edit", "input_schema": {"type": "object"}}],
        },
        response={},
        metadata={"run_id": "r-1", "turn": 1},
    )
    logger.close()
    entry = logger.entries()[0]
    assert entry["request"]["system"] == "You are a coding agent."
    assert entry["request"]["tools"][0]["name"] == "Edit"


def test_flags_secrets_without_redacting_payload(tmp_path):
    """Detection must not mutate the record — the eval needs the true
    payload. Flagging drives the pre-share scrub instead."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.log_call(
        request={"messages": [{"content": "AKIAIOSFODNN7EXAMPLE"}]},
        response={},
        metadata={"run_id": "r-1", "turn": 1},
    )
    logger.close()
    entry = logger.entries()[0]
    assert "aws_access_key_id" in entry["secret_flags"]
    assert "AKIAIOSFODNN7EXAMPLE" in json.dumps(entry["request"])


def test_records_stop_reason_and_request_id(tmp_path):
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.log_call(
        request={"model": "nemotron-3-super-120b"},
        response={"stop_reason": "max_tokens"},
        metadata={"run_id": "r-1", "turn": 9, "bedrock_request_id": "req-abc"},
    )
    logger.close()
    entry = logger.entries()[0]
    assert entry["response"]["stop_reason"] == "max_tokens"
    assert entry["metadata"]["bedrock_request_id"] == "req-abc"


def test_close_is_idempotent(tmp_path):
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.close()
    logger.close()


def test_refuses_to_overwrite_an_existing_log(tmp_path):
    """Global constraint: the event log is append-only and immutable, and
    files open with mode "x". Opening "wt" would silently destroy a prior
    run's wire log -- the one artifact that cannot be reconstructed."""
    path = tmp_path / "wire.jsonl.gz"
    WireLogger(path).close()
    with pytest.raises(FileExistsError):
        WireLogger(path)


def test_callback_is_dispatchable_by_litellm(tmp_path):
    """LiteLLM's success_handler dispatches on isinstance(callback,
    CustomLogger); its only other branch is plain callables. A duck-typed
    object with the right method names is skipped without an error, so the
    run would produce no wire log at all -- and spec section 6.2 makes wire
    logging mandatory. Verified against litellm 1.95.0.
    """
    from litellm.integrations.custom_logger import CustomLogger

    callback = BakeoffCallback(WireLogger(tmp_path / "wire.jsonl.gz"), run_id="r-1")
    assert isinstance(callback, CustomLogger)


def test_callback_records_measured_latency(tmp_path):
    """LiteLLM hands the callback real start/end timestamps. That is
    wire-level ground truth for generation time, where the trajectory
    parser can only infer it from transcript gaps -- and latency p95 is a
    stated success criterion (spec section 10)."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="r-1")

    start = datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 4, 0, 0, 2, 500000, tzinfo=UTC)
    callback.log_success_event({"model": "gemma-4-31b"}, {"id": "x"}, start, end)
    logger.close()

    assert logger.entries()[0]["metadata"]["latency_ms"] == 2500


def test_callback_counts_calls_not_turns(tmp_path):
    """LiteLLM fires once per API call, not per agent turn, and the proxy
    is configured with num_retries. Two failed attempts and a success are
    one turn but three calls, so labelling the counter "turn" would
    silently mis-join wire entries against trajectory turns."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="r-1")

    now = datetime(2026, 8, 4, tzinfo=UTC)
    callback.log_failure_event({"model": "kimi-k2-5"}, {}, now, now)
    callback.log_failure_event({"model": "kimi-k2-5"}, {}, now, now)
    callback.log_success_event({"model": "kimi-k2-5"}, {}, now, now)
    logger.close()

    entries = logger.entries()
    assert [e["metadata"]["call_index"] for e in entries] == [1, 2, 3]
    assert [e["metadata"]["failed"] for e in entries] == [True, True, False]
    assert not any("turn" in e["metadata"] for e in entries)
