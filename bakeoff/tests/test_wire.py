import gzip
import json
from datetime import UTC, datetime

import pytest

from bakeoff.proxy_callback import finish_reason
from bakeoff.wire import BakeoffCallback, WireLogger


def test_the_providers_own_finish_reason_is_stamped_on_every_entry(tmp_path):
    """Measured 2026-08-13 across all four arms: the logged response is an
    OpenAI-shaped ModelResponse dump, so `choices[0].finish_reason` is uniform
    -- including on claude-sonnet-5-runtime, whose bedrock Invoke route carries
    a native Anthropic body. The record's per_turn[].stop_reason is Claude
    Code's TRANSLATED value from the transcript; this is the route's own word.

    Through the CALLBACK, not WireLogger.log_call: log_call stores metadata
    verbatim and must keep doing so -- it is the shared sink for both capture
    paths and for replayed proxy entries."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="r-1")

    callback.log_success_event(
        {"model": "kimi-k2-5"},
        {"choices": [{"finish_reason": "tool_calls"}]},
        datetime(2026, 8, 13, tzinfo=UTC),
        datetime(2026, 8, 13, tzinfo=UTC),
    )
    logger.close()

    assert logger.entries()[0]["metadata"]["finish_reason"] == "tool_calls"


def test_an_anthropic_shaped_response_still_yields_a_finish_reason():
    """The fallback, and not dead code: `stop_reason` is what an
    Anthropic-shaped dump carries, and a route that logs one must not read as
    uncaptured."""
    assert finish_reason({"stop_reason": "end_turn"}) == "end_turn"


def test_a_failed_call_has_no_finish_reason_rather_than_a_default():
    """A failure's response is `{"error": ...}` -- no stopping decision was
    reached, and "" or "stop" there is a claim invented from an absence."""
    assert finish_reason({"error": {"message": "boom"}}) is None
    assert finish_reason({"raw_completion": "None"}) is None
    assert finish_reason({"choices": [{"finish_reason": None}]}) is None


def test_both_capture_paths_stamp_the_same_key():
    """The two metadata dicts are built independently. A key on one path only
    reads as "this arm did not report one" -- the failure mode the shared
    REQUEST_KEYS constant exists to prevent on the request side."""
    import inspect

    from bakeoff import proxy_callback, wire

    for source in (
        inspect.getsource(proxy_callback.BakeoffProxyCallback._write),
        inspect.getsource(wire.BakeoffCallback._record),
    ):
        assert '"finish_reason"' in source


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


def test_callback_captures_the_http_status_of_a_failed_call(tmp_path):
    """The only place the harness ever sees an HTTP status.

    LiteLLM puts the exception on the failure kwargs, and its exceptions
    carry the provider status -- 429 throttle, 408 timeout, 503 unavailable
    -- which are exactly the codes classify_exclusion maps to pre-registered
    infra reasons. Without it a Bedrock throttle is indistinguishable from
    the model giving up, and gets scored against the model.
    """
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="r-1")
    now = datetime(2026, 8, 4, tzinfo=UTC)

    class Throttled(Exception):
        status_code = 429

    callback.log_failure_event(
        {"model": "gemma-4-31b", "exception": Throttled()}, {}, now, now
    )
    assert logger.entries()[0]["metadata"]["status_code"] == 429


def test_callback_does_not_invent_a_status_for_a_local_failure(tmp_path):
    """A connection reset or a client-side bug has no HTTP status.
    Defaulting to 500 would manufacture an api_5xx exclusion -- dropping a
    run from the results on the strength of a status nobody reported."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="r-1")
    now = datetime(2026, 8, 4, tzinfo=UTC)

    callback.log_failure_event(
        {"model": "gemma-4-31b", "exception": RuntimeError("connection reset")},
        {}, now, now,
    )
    callback.log_success_event({"model": "gemma-4-31b"}, {"id": "x"}, now, now)

    assert logger.entries()[0]["metadata"]["status_code"] is None
    assert logger.entries()[1]["metadata"]["status_code"] is None


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


def test_a_replayed_entry_keeps_the_time_it_was_captured(tmp_path):
    """The proxy makes the calls; the harness folds its entries into the
    canonical artifact after the run. `log_call` stamped `now()` over every one
    of them, so each timestamp in the gzipped log was the harness's post-run
    replay time -- one clock reading spread across the calls it was supposed to
    order, and the only per-call time the artifact had.
    """
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.log_call(
        request={"model": "m"},
        response={"ok": True},
        metadata={"call_index": 1},
        logged_at="2026-08-12T17:55:13Z",
    )
    logger.close()

    entry = logger.entries()[0]
    assert entry["logged_at"] == "2026-08-12T17:55:13Z"
    # The harness's own stamp is kept BESIDE it, not instead of it: when the
    # entry was folded in is real information, it is just not when the call
    # happened.
    assert entry["replayed_at"] and entry["replayed_at"] != entry["logged_at"]


def test_an_entry_captured_in_process_is_not_marked_as_replayed(tmp_path):
    """`replayed_at` present means the line came from the proxy's file. A
    non-null on a live capture would make every entry look second-hand."""
    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.log_call(request={"model": "m"}, response={"ok": True}, metadata={})
    logger.close()

    entry = logger.entries()[0]
    assert entry["replayed_at"] is None
    assert entry["logged_at"]


def _proxy_kwargs(run_id: str) -> dict:
    """A callback kwargs dict shaped like `test_proxy_callback.kwargs_for`.

    Built inline rather than imported: `tests/` has no `__init__.py`, and no
    other test module imports across that boundary, so this module does not
    start the pattern.
    """
    from bakeoff.proxy_callback import RUN_ID_HEADER

    headers = {"content-type": "application/json", RUN_ID_HEADER: run_id}
    return {
        "model": "mock-ok",
        "litellm_params": {
            "model": "anthropic/claude-sonnet-5",
            "metadata": {"headers": headers},
            "proxy_server_request": {
                "url": "http://litellm:4000/v1/messages",
                "headers": headers,
                "body": {
                    "model": "mock-ok",
                    "max_tokens": 4096,
                    "temperature": 0.7,
                    "system": "you are a coding agent",
                    "tools": [{"name": "Bash"}],
                    "messages": [{"role": "user", "content": "hi"}],
                    "litellm_metadata": {"headers": headers},
                },
            },
        },
        "optional_params": {},
        "exception": None,
    }


def test_the_in_process_path_reads_the_upstream_off_the_response_too(tmp_path):
    """Not hardcoded None. These are properties of the RESPONSE, not of the
    caller, so this path can observe them exactly as well as the proxy path --
    and filing None where an upstream did name itself would be a measurement
    the callback never made. (`resolved` stays None here for the opposite
    reason: no second resolution exists on this path to observe.)"""
    logger = WireLogger(tmp_path / "upstream.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="run-up")
    callback.log_success_event(
        _proxy_kwargs("run-up"),
        {
            "provider": "CoreWeave",
            "choices": [{"finish_reason": "stop", "native_finish_reason": "stop"}],
            "usage": {"cost": 0.5},
        },
        None,
        None,
    )
    metadata = logger.entries()[0]["metadata"]
    assert metadata["upstream_provider"] == "CoreWeave"
    assert metadata["native_finish_reason"] == "stop"
    assert metadata["usage_cost"] == 0.5
    assert metadata["upstream_state"] == "captured"

    callback.log_success_event(
        _proxy_kwargs("run-up"), {"choices": [{"finish_reason": "stop"}]}, None, None
    )
    metadata = logger.entries()[1]["metadata"]
    assert metadata["upstream_provider"] is None
    assert metadata["upstream_state"] == "not_in_response"


def test_both_capture_paths_write_the_same_metadata_keys(tmp_path, monkeypatch):
    """A key on one path only reads as 'this arm did not report one'."""
    from bakeoff.proxy_callback import BakeoffProxyCallback, read_run_entries

    monkeypatch.setenv("BAKEOFF_WIRE_DIR", str(tmp_path / "wire"))
    BakeoffProxyCallback().log_success_event(
        _proxy_kwargs("run-keys"), {"choices": [{"finish_reason": "stop"}]}, None, None
    )
    proxy_keys = set(read_run_entries(tmp_path / "wire", "run-keys")[0]["metadata"])

    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    callback = BakeoffCallback(logger, run_id="run-keys")
    callback.log_success_event(
        _proxy_kwargs("run-keys"), {"choices": [{"finish_reason": "stop"}]}, None, None
    )
    inproc_keys = set(logger.entries()[0]["metadata"])

    assert {
        "upstream_provider", "native_finish_reason", "usage_cost", "upstream_state",
    } <= proxy_keys
    assert proxy_keys - {"resolved_state"} == inproc_keys - {"bedrock_request_id"}
