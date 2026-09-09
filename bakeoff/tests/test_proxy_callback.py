"""Direct tests for the proxy-side wire capture.

This module is the least visible code in the harness and the most
consequential. It executes inside the LiteLLM proxy container, so harness-side
coverage reports it as barely covered no matter how well it is tested, and the
integration tests can only observe its OUTPUT -- a file appearing with the
right name. Everything about how it decides that name, and what it puts in the
file, is untestable from there.

So it is exercised here directly, in-process, against kwargs shaped like the
real ones. The shapes are not invented: they were dumped from a live
litellm 1.95.0 proxy handling a real POST /v1/messages, which is why
`proxy_server_request.body` carries the sampling fields and the top level does
not.
"""

from __future__ import annotations

import json
import threading

import pytest

from bakeoff.proxy_callback import (
    RUN_ID_HEADER,
    BakeoffProxyCallback,
    read_run_entries,
    unattributed_count,
)


@pytest.fixture
def wire_dir(tmp_path, monkeypatch):
    path = tmp_path / "wire"
    monkeypatch.setenv("BAKEOFF_WIRE_DIR", str(path))
    return path


def kwargs_for(
    run_id: str | None = "run-abc",
    *,
    body: dict | None = None,
    optional_params: dict | None = None,
    exception: object | None = None,
    metadata_key: str = "metadata",
) -> dict:
    """A callback kwargs dict shaped like the real one.

    Verified against a live proxy: on /v1/messages the sampling fields live
    in litellm_params.proxy_server_request.body, `optional_params` is empty,
    and the headers are lowercased by Starlette on the way in.
    """
    headers = {"content-type": "application/json"}
    if run_id is not None:
        headers[RUN_ID_HEADER] = run_id
    return {
        "model": "mock-ok",
        "litellm_params": {
            "model": "anthropic/claude-sonnet-5",
            metadata_key: {"headers": headers},
            "proxy_server_request": {
                "url": "http://litellm:4000/v1/messages",
                "headers": headers,
                "body": body
                if body is not None
                else {
                    "model": "mock-ok",
                    "max_tokens": 4096,
                    "temperature": 0.7,
                    "system": "you are a coding agent",
                    "tools": [{"name": "Bash"}],
                    "messages": [{"role": "user", "content": "hi"}],
                    # Injected by the proxy, not sent by the caller.
                    "litellm_metadata": {"headers": headers},
                },
            },
        },
        "optional_params": optional_params or {},
        "exception": exception,
    }


def test_call_is_attributed_to_the_run_that_made_it(wire_dir):
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for("run-abc"), {"ok": True}, None, None)

    entries = read_run_entries(wire_dir, "run-abc")
    assert len(entries) == 1
    assert entries[0]["metadata"]["run_id"] == "run-abc"
    assert entries[0]["metadata"]["call_index"] == 1
    assert unattributed_count(wire_dir) == 0


def test_sampling_comes_from_the_request_body_not_the_top_level(wire_dir):
    """The trap this module exists to avoid.

    On POST /v1/messages, max_tokens/temperature/system/tools are NOT
    top-level callback kwargs and optional_params is {}. Reading the obvious
    place records None for every sampling field -- indistinguishable from a
    caller that sent none, which is exactly the claim section 5.3 needs the
    wire log to settle.
    """
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)

    request = read_run_entries(wire_dir, "run-abc")[0]["request"]
    assert request["max_tokens"] == 4096
    assert request["temperature"] == 0.7
    assert request["system"] == "you are a coding agent"
    assert request["tools"] == [{"name": "Bash"}]
    # The resolved deployment, not the alias.
    assert request["model"] == "anthropic/claude-sonnet-5"


def test_the_dropped_params_are_recorded_because_they_are_dropped(wire_dir):
    """The projection has to carry what `additional_drop_params` removes.

    Those drops are PER ARM -- `reasoning_effort` is listed on each of the
    three candidate deployments and on neither Sonnet deployment -- so
    whether a drop actually reached a given route is a section 6.4 question
    about the arms being compared. Through 3.0.0 the projection was six keys
    and none of these were among them, which left the log designated as the
    authority on "what was sent" unable to answer it: 856 stored calls across
    five arms carry no trace of the request side of this at all.

    `stream` is here for a different reason. Every real call streams, so a
    False would mean capture is looking at something other than the agent's
    traffic -- the `mock_response` short-circuit that skips the streaming
    wrapper is the known way to get one.
    """
    callback = BakeoffProxyCallback()
    callback.log_success_event(
        kwargs_for(
            body={
                "model": "mock-ok",
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "reasoning_effort": "medium",
                "context_management": {"edits": []},
                "output_config": {"format": "text"},
                "anthropic_beta": ["context-management-2025-06-27"],
                "stream": True,
            }
        ),
        {"ok": True},
        None,
        None,
    )

    request = read_run_entries(wire_dir, "run-abc")[0]["request"]
    assert request["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert request["reasoning_effort"] == "medium"
    assert request["context_management"] == {"edits": []}
    assert request["output_config"] == {"format": "text"}
    assert request["anthropic_beta"] == ["context-management-2025-06-27"]
    assert request["stream"] is True


def test_a_request_that_carried_none_of_them_records_none_not_absence(wire_dir):
    """The key has to be present with a null, not missing.

    A missing key and a null read the same to `dict.get`, but not to anyone
    diffing two arms' entries or counting how many calls carried a field: an
    absent key is also what an OLD log line looks like, and conflating "this
    arm sent no thinking block" with "this line predates the projection" is
    the version-confusion the schema notes exist to prevent.
    """
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)

    request = read_run_entries(wire_dir, "run-abc")[0]["request"]
    for name in (
        "thinking",
        "reasoning_effort",
        "context_management",
        "output_config",
        "anthropic_beta",
        "stream",
    ):
        assert name in request, f"{name} missing from the projection entirely"
        assert request[name] is None


def test_the_two_capture_paths_project_the_same_request_shape(wire_dir, tmp_path):
    """proxy_callback and wire.py must not drift apart.

    A run's canonical artifact is written from whichever path was live -- the
    proxy one on a real run, the in-process one in tests and the dry run --
    so a field present in one projection and absent from the other reads as
    "not sent on this arm" rather than "not captured on this path".
    """
    from bakeoff.wire import BakeoffCallback, WireLogger

    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)
    proxy_keys = set(read_run_entries(wire_dir, "run-abc")[0]["request"])

    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    BakeoffCallback(logger, "run-abc").log_success_event(
        {"model": "mock-ok"}, {"ok": True}, None, None
    )
    logger.close()
    in_process_keys = set(logger.entries()[0]["request"])

    assert proxy_keys == in_process_keys


def test_optional_params_is_not_treated_as_what_the_provider_received(wire_dir):
    """The rule this replaces was backwards, and measurably so.

    `request` used to prefer `optional_params` and fall back to the client
    body, on the reasoning that optional_params is "closer to what the provider
    received". Measured 2026-08-12 against a real streaming provider call: on
    the anthropic-messages route optional_params belongs to the OUTER call, so
    it carries `max_tokens: 16384` with no `max_completion_tokens` and no
    `reasoning_effort` -- the state BEFORE both openai interventions, handed the
    provenance of a resolved param. Preferring it meant the log reported a
    parameter the wire did not carry, at full plausibility, on the one field the
    rename touches.

    `request` is now the client's body and nothing else. What the provider got
    lives in `resolved`, and comes from the side channel or not at all.
    """
    callback = BakeoffProxyCallback()
    callback.log_success_event(
        kwargs_for(optional_params={"temperature": 0.2}), {"ok": True}, None, None
    )
    entry = read_run_entries(wire_dir, "run-abc")[0]
    assert entry["request"]["temperature"] == 0.7, "the client's value, not the outer call's"
    assert entry["resolved"] is None, "nothing observed the provider boundary"


def test_the_resolved_params_the_side_channel_captured_are_recorded(wire_dir):
    """What the provider was actually sent, when something could see it.

    The channel exists because nothing on the callback's kwargs can answer
    this: capture fires on the outer anthropic_messages call and the nested
    acompletion -- where the cap is renamed and reasoning_effort is pinned --
    fires no callback of its own.
    """
    from bakeoff.proxy_callback import open_resolved_capture, record_resolved_params

    open_resolved_capture("call-1")
    record_resolved_params(
        {"max_completion_tokens": 16384, "reasoning_effort": "none", "stream": True}
    )

    callback = BakeoffProxyCallback()
    callback.log_success_event(
        {**kwargs_for(), "litellm_call_id": "call-1"}, {"ok": True}, None, None
    )

    entry = read_run_entries(wire_dir, "run-abc")[0]
    assert entry["resolved"]["max_completion_tokens"] == 16384
    assert entry["resolved"]["reasoning_effort"] == "none"
    # The cap under the OTHER spelling is what the client asked for, and both
    # readings have to survive: the whole point is being able to see them
    # disagree.
    assert entry["resolved"]["max_tokens"] is None
    assert entry["request"]["max_tokens"] == 4096


def test_a_request_with_no_capture_opened_records_resolved_as_null(wire_dir):
    """None, never {}. A route that never reached the hook observed nothing,
    and an empty dict would read as "the provider was sent no parameters"."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)
    assert read_run_entries(wire_dir, "run-abc")[0]["resolved"] is None


def test_another_calls_resolved_params_are_never_attributed_to_this_one(wire_dir):
    """The failure this guard exists for is misattribution, not a gap.

    A ContextVar that outlives its request hands the next call the previous
    one's resolved params -- and on a proxy serving several arms concurrently
    that is one arm's configuration recorded against another, at full
    plausibility, with nothing in the record to notice. A gap says "not
    observed" and can be acted on; this cannot.
    """
    from bakeoff.proxy_callback import open_resolved_capture, record_resolved_params

    open_resolved_capture("call-1")
    record_resolved_params({"max_completion_tokens": 4})

    callback = BakeoffProxyCallback()
    callback.log_success_event(
        {**kwargs_for(), "litellm_call_id": "call-2"}, {"ok": True}, None, None
    )
    assert read_run_entries(wire_dir, "run-abc")[0]["resolved"] is None


def test_proxy_injected_metadata_is_not_reported_as_part_of_the_request(wire_dir):
    """The proxy adds litellm_metadata to the body on the way in. Reporting
    it as something the caller sent would put the proxy's own bookkeeping --
    including a copy of every header -- into the record of what the agent
    asked for."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)
    assert "litellm_metadata" not in read_run_entries(wire_dir, "run-abc")[0]["request"]


@pytest.mark.parametrize("metadata_key", ["metadata", "litellm_metadata"])
def test_headers_are_found_under_either_metadata_key(wire_dir, metadata_key):
    """The proxy copies headers under more than one key depending on route
    and version. Reading only one fails OPEN -- every call unattributed,
    which looks identical to a missing header on the agent side."""
    callback = BakeoffProxyCallback()
    kwargs = kwargs_for("run-xyz", metadata_key=metadata_key)
    if metadata_key != "metadata":
        kwargs["litellm_params"].pop("metadata", None)
    callback.log_success_event(kwargs, {"ok": True}, None, None)

    assert len(read_run_entries(wire_dir, "run-xyz")) == 1


def test_headers_fall_back_to_the_raw_proxy_request(wire_dir):
    """With every metadata key stripped, attribution must still work off
    proxy_server_request -- the proxy's own record of the request, and the
    most durable of the sources.

    It sits under litellm_params, not at the top level. Checking only the
    top level leaves this fallback permanently dead, so the failure mode it
    exists to cover would still be a total attribution loss.
    """
    callback = BakeoffProxyCallback()
    kwargs = kwargs_for("run-psr")
    kwargs["litellm_params"].pop("metadata", None)
    kwargs["litellm_params"].pop("litellm_metadata", None)
    callback.log_success_event(kwargs, {"ok": True}, None, None)

    assert len(read_run_entries(wire_dir, "run-psr")) == 1
    assert unattributed_count(wire_dir) == 0


def test_header_case_does_not_decide_attribution(wire_dir):
    """HTTP header case is not significant. A capitalised header that failed
    to match would silently unattribute every call."""
    callback = BakeoffProxyCallback()
    kwargs = kwargs_for(None)
    kwargs["litellm_params"]["metadata"]["headers"]["X-Bakeoff-Run-Id"] = "run-caps"
    callback.log_success_event(kwargs, {"ok": True}, None, None)

    assert len(read_run_entries(wire_dir, "run-caps")) == 1


def test_a_call_with_no_run_id_is_recorded_as_unattributed_not_dropped(wire_dir):
    """The positive half of the attribution guarantee.

    Asserting only `unattributed_count == 0` on a good run passes vacuously
    -- the count is 0 when nothing was written at all, for any reason. This
    pins that a header-less call is KEPT and flagged: it happened and was
    paid for, so a gap is honest and a guess would not be.
    """
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(None), {"ok": True}, None, None)

    assert unattributed_count(wire_dir) == 1
    assert (wire_dir / "unattributed.jsonl").is_file()
    entry = json.loads((wire_dir / "unattributed.jsonl").read_text().strip())
    assert entry["metadata"]["run_id"] is None
    assert entry["request"]["max_tokens"] == 4096  # the call itself is intact


def test_failure_carries_the_status_code_the_exclusion_rule_needs(wire_dir):
    class Throttled(Exception):
        status_code = 429

    callback = BakeoffProxyCallback()
    callback.log_failure_event(
        kwargs_for(exception=Throttled()), None, None, None
    )

    metadata = read_run_entries(wire_dir, "run-abc")[0]["metadata"]
    assert metadata["failed"] is True
    assert metadata["status_code"] == 429


def test_an_exception_without_a_status_code_does_not_invent_one(wire_dir):
    """A connection reset has no HTTP status. Defaulting to 500 would
    manufacture an api_5xx exclusion out of a local failure."""
    callback = BakeoffProxyCallback()
    callback.log_failure_event(
        kwargs_for(exception=RuntimeError("connection reset")), None, None, None
    )
    assert read_run_entries(wire_dir, "run-abc")[0]["metadata"]["status_code"] is None


def test_a_successful_call_carries_no_status_code(wire_dir):
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)
    metadata = read_run_entries(wire_dir, "run-abc")[0]["metadata"]
    assert metadata["failed"] is False
    assert metadata["status_code"] is None


def test_call_index_counts_per_run_not_globally(wire_dir):
    """One agent turn can produce several calls, and the proxy serves many
    runs. A global counter would make call_index meaningless for joining a
    call back to its position within its own run."""
    callback = BakeoffProxyCallback()
    for run in ("run-a", "run-b", "run-a"):
        callback.log_success_event(kwargs_for(run), {"ok": True}, None, None)

    assert [e["metadata"]["call_index"] for e in read_run_entries(wire_dir, "run-a")] == [1, 2]
    assert [e["metadata"]["call_index"] for e in read_run_entries(wire_dir, "run-b")] == [1]


def test_the_write_is_serialized_against_a_thread_switch(wire_dir, monkeypatch):
    """Whether the lock actually does anything.

    The volume test below passes with the lock removed: CPython's GIL plus
    O_APPEND make small writes and a dict increment effectively atomic, so
    the race never materialises by luck alone. A test that cannot fail when
    the protection is deleted is not evidence the protection works.

    This forces the interleaving instead, by yielding inside the critical
    section. With the lock, `_call_index` is read-modify-written under
    exclusion and every call gets a distinct index; without it, two threads
    read the same value and two calls claim to be call 1 -- silently, in the
    artifact every other measurement is checked against.
    """
    import time

    class SlowRead(dict):
        """Yields between the counter's read and its write -- the exact
        window a lock exists to close."""

        def get(self, *args):
            value = super().get(*args)
            # AFTER the read, not before. Sleeping first leaves the real read
            # and the write adjacent, so no yield point separates them and
            # the race cannot occur however many threads pile up.
            time.sleep(0.01)
            return value

    callback = BakeoffProxyCallback()
    callback._call_index = SlowRead()
    threads = [
        threading.Thread(
            target=lambda: callback.log_success_event(
                kwargs_for("run-race"), {"ok": True}, None, None
            )
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    indices = [e["metadata"]["call_index"] for e in read_run_entries(wire_dir, "run-race")]
    assert sorted(indices) == list(range(1, 9)), f"call indices collided: {indices}"


def test_concurrent_runs_do_not_interleave_or_lose_calls(wire_dir):
    """The proxy serves requests concurrently and the eval runs samples in
    parallel. Attribution is by filename, so two runs cannot land partial
    lines in each other's log.

    This asserts the OUTCOME at volume. It is not sensitive to the lock (see
    the test above, which is); it is here to catch a design-level mistake --
    a shared file, a global counter, an overwritten path -- that would show
    up as lost or misattributed calls.
    """
    callback = BakeoffProxyCallback()
    calls_per_run = 25
    runs = [f"run-{i}" for i in range(6)]

    def hammer(run_id: str) -> None:
        for _ in range(calls_per_run):
            callback.log_success_event(kwargs_for(run_id), {"ok": True}, None, None)

    threads = [threading.Thread(target=hammer, args=(r,)) for r in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for run_id in runs:
        entries = read_run_entries(wire_dir, run_id)
        assert len(entries) == calls_per_run, f"{run_id} lost or gained calls"
        assert {e["metadata"]["run_id"] for e in entries} == {run_id}
        assert sorted(e["metadata"]["call_index"] for e in entries) == list(
            range(1, calls_per_run + 1)
        )
    assert unattributed_count(wire_dir) == 0


def test_a_partial_final_line_does_not_cost_the_rest(wire_dir):
    """The proxy can be mid-write when a run ends. Losing every captured
    call over one truncated line is the data loss this module exists to
    prevent."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for("run-cut"), {"ok": True}, None, None)
    with open(wire_dir / "run-cut.jsonl", "a", encoding="utf-8") as handle:
        handle.write('{"request": {"model": "mock-ok"')  # killed mid-write

    assert len(read_run_entries(wire_dir, "run-cut")) == 1


def test_a_run_that_made_no_calls_reads_back_empty_not_missing(wire_dir):
    assert read_run_entries(wire_dir, "never-ran") == []
    assert unattributed_count(wire_dir) == 0


def test_a_non_dict_response_is_kept_as_text_rather_than_dropped(wire_dir):
    """Section 6.2: without the raw completion, `malformed: true` is a dead
    end. Whatever comes back is preserved even when it is not a dict."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), "a bare string completion", None, None)

    response = read_run_entries(wire_dir, "run-abc")[0]["response"]
    assert response["raw_completion"] == "a bare string completion"


# --- what a failure looks like in the log ------------------------------------


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _ProviderError(Exception):
    def __init__(self, message: str, status_code: int, body: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.llm_provider = "openai"
        self.response = _FakeResponse(body)


def test_a_failure_records_the_provider_body_not_the_string_none(wire_dir):
    """`response_obj` is None on a failure, so a 400 landed as
    `{"raw_completion": "None"}` -- the log said something went wrong and
    nothing else. Every diagnosis so far (the mantle Sonnet arm's `invalid beta
    flag`, Gemma's -32602, Gemma's max_tokens rejection) had to come from
    grepping the proxy's stdout, and in a real run the proxy log is not an
    artifact of any record. Section 6.2 exists so a failure can be diagnosed
    after the fact.
    """
    exc = _ProviderError(
        "OpenAIException - Unsupported parameter: 'max_tokens' is not supported",
        400,
        '{"error":{"message":"Unsupported parameter: \'max_tokens\'"}}',
    )
    callback = BakeoffProxyCallback()
    callback.log_failure_event(kwargs_for(exception=exc), None, None, None)

    entry = read_run_entries(wire_dir, "run-abc")[0]
    error = entry["response"]["error"]
    assert error["type"] == "_ProviderError"
    assert "max_tokens" in error["message"]
    assert error["status_code"] == 400
    assert error["provider"] == "openai"
    assert "Unsupported parameter" in error["body"]
    assert "raw_completion" not in entry["response"]


def test_a_failure_body_is_reachable_by_the_secret_scan(wire_dir, tmp_path):
    """The error goes under `response` and not beside it, because WireLogger
    builds the scan payload from `request` and `response` only. A provider
    error body is exactly where a request gets echoed back, so an error stored
    outside the scanned fields is the one place a secret could leave the
    harness unflagged."""
    from bakeoff.wire import WireLogger

    logger = WireLogger(tmp_path / "wire.jsonl.gz")
    logger.log_call(
        request={"model": "m"},
        response={"error": {"body": "rejected credentials for AKIA" + "H7QW3ZP2NM4RTX6B"}},
        metadata={},
    )
    logger.close()
    assert logger.entries()[0]["secret_flags"], "an error body escaped the secret scan"


def test_a_successful_call_still_records_the_completion(wire_dir):
    """The error projection must not displace the ordinary path."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"id": "msg_1", "ok": True}, None, None)
    assert read_run_entries(wire_dir, "run-abc")[0]["response"]["id"] == "msg_1"


def test_the_real_call_boundaries_are_recorded(wire_dir):
    """`logged_at` is the callback's own clock and is re-stamped again when the
    harness folds these entries into the canonical artifact. Without the call's
    own start and end, nothing in the gzipped log says when a call actually
    happened -- so no per-call latency can ever be attached to the turn that
    incurred it."""
    from datetime import UTC, datetime

    start = datetime(2026, 8, 12, 17, 55, 13, tzinfo=UTC)
    end = datetime(2026, 8, 12, 17, 55, 16, tzinfo=UTC)
    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, start, end)

    metadata = read_run_entries(wire_dir, "run-abc")[0]["metadata"]
    assert metadata["started_at"] == "2026-08-12T17:55:13Z"
    assert metadata["ended_at"] == "2026-08-12T17:55:16Z"
    assert metadata["latency_ms"] == 3000


def test_the_call_identity_that_separates_a_retry_from_a_new_call(wire_dir):
    """Measured 2026-08-12: one failed provider call fires the failure callback
    TWICE under a single litellm_call_id, and num_retries adds more entries for
    one client request. Without the id nothing in the log can tell a duplicate
    from a distinct call -- which is how a 39-served/42-captured surplus was
    read as three retries when it was three double-logged failures."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(
        {**kwargs_for(), "litellm_call_id": "abc", "litellm_trace_id": "trace-1"},
        {"ok": True},
        None,
        None,
    )
    metadata = read_run_entries(wire_dir, "run-abc")[0]["metadata"]
    assert metadata["litellm_call_id"] == "abc"
    assert metadata["litellm_trace_id"] == "trace-1"


def test_a_null_resolved_says_which_kind_of_null_it_is(wire_dir):
    """Three unrelated states render as the same missing field and only one is
    a defect: a route that legitimately resolves no openai params, a callback
    with no id to join on, and a hand-off that silently broke.

    Not hypothetical. The first mechanism built for this channel passed every
    unit test and recorded nothing in the real proxy on any arm -- a ContextVar
    set in the pre-request hook reads back unset in the callback, which runs
    from a context copied before the hook. `resolved_state` is what said so;
    without it the null was indistinguishable from an `anthropic/` arm behaving
    correctly.
    """
    from bakeoff.proxy_callback import open_resolved_capture, record_resolved_params

    callback = BakeoffProxyCallback()
    callback.log_success_event(kwargs_for(), {"ok": True}, None, None)
    assert read_run_entries(wire_dir, "run-abc")[0]["metadata"][
        "resolved_state"
    ] == "no_call_id"

    callback.log_success_event(
        {**kwargs_for(), "litellm_call_id": "unmapped"}, {"ok": True}, None, None
    )
    assert read_run_entries(wire_dir, "run-abc")[1]["metadata"][
        "resolved_state"
    ] == "not_recorded"

    open_resolved_capture("mapped")
    record_resolved_params({"max_completion_tokens": 16})
    callback.log_success_event(
        {**kwargs_for(), "litellm_call_id": "mapped"}, {"ok": True}, None, None
    )
    assert read_run_entries(wire_dir, "run-abc")[2]["metadata"][
        "resolved_state"
    ] == "captured"


def test_the_manifest_round_trips_the_provider_route(tmp_path):
    from bakeoff.proxy_callback import read_manifest, write_manifest

    write_manifest(["a", "b"], "1.95.0", tmp_path, provider_route="openrouter")
    assert read_manifest(tmp_path) == (["a", "b"], "1.95.0", "openrouter")


def test_an_old_manifest_without_a_route_reads_as_no_claim(tmp_path):
    (tmp_path / "adapter_patches.json").write_text('{"patches": ["a"], "litellm": "1.95.0"}')
    from bakeoff.proxy_callback import read_manifest

    assert read_manifest(tmp_path) == (["a"], "1.95.0", "")


OPENROUTER_RAW = {
    "id": "gen-123",
    "provider": "CoreWeave",
    "model": "moonshotai/kimi-k2.6",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "native_finish_reason": "tool_calls", "message": {"role": "assistant", "content": ""}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.000123},
}


def test_the_upstream_provider_and_cost_are_read_from_the_raw_provider_body(wire_dir):
    """Spec §4. litellm's ModelResponse is not guaranteed to keep OpenRouter's
    top-level `provider`, so the callback reads kwargs["original_response"]
    -- the raw JSON litellm hands every success callback -- before the dump."""
    kwargs = kwargs_for("run-or")
    kwargs["original_response"] = json.dumps(OPENROUTER_RAW)
    BakeoffProxyCallback().log_success_event(kwargs, {"choices": [{"finish_reason": "tool_calls"}]}, None, None)
    metadata = read_run_entries(wire_dir, "run-or")[0]["metadata"]
    assert metadata["upstream_provider"] == "CoreWeave"
    assert metadata["native_finish_reason"] == "tool_calls"
    assert metadata["usage_cost"] == pytest.approx(0.000123)
    assert metadata["upstream_state"] == "captured"


def test_the_upstream_fields_fall_back_to_the_response_dump_then_to_none(wire_dir):
    kwargs = kwargs_for("run-dump")
    BakeoffProxyCallback().log_success_event(kwargs, dict(OPENROUTER_RAW), None, None)
    metadata = read_run_entries(wire_dir, "run-dump")[0]["metadata"]
    assert metadata["upstream_provider"] == "CoreWeave"

    kwargs = kwargs_for("run-none")
    BakeoffProxyCallback().log_success_event(kwargs, {"choices": [{"finish_reason": "stop"}]}, None, None)
    metadata = read_run_entries(wire_dir, "run-none")[0]["metadata"]
    assert metadata["upstream_provider"] is None
    assert metadata["native_finish_reason"] is None
    assert metadata["usage_cost"] is None
    assert metadata["upstream_state"] == "not_in_response"


# What litellm 1.95.0 hands a STREAMING success callback: the output of
# `stream_chunk_builder`, which rebuilds a ModelResponse from the chunks. It
# carries `usage` (cost included) and drops OpenRouter's top-level `provider`
# and `choices[].native_finish_reason`. `original_response` is unset too --
# `llms/openai/openai.py::async_streaming` never calls `logging_obj.post_call`.
ASSEMBLED_STREAM_DUMP = {
    "id": "chatcmpl-abc",
    "model": "moonshotai/kimi-k2.6",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.000123},
}


def test_a_streaming_dump_with_no_provider_is_recorded_as_not_in_response(wire_dir):
    """The shape every real OpenRouter call produces when the streaming hook
    did not file anything. The cost survives the rebuild and the identity does
    not, so `upstream_provider: null` here has to be distinguishable from a
    bedrock entry's -- `upstream_state` is what distinguishes it, and
    `upstream_providers` on the record reads None off it rather than []."""
    kwargs = kwargs_for("run-stream")
    assert "original_response" not in kwargs
    BakeoffProxyCallback().log_success_event(kwargs, dict(ASSEMBLED_STREAM_DUMP), None, None)
    metadata = read_run_entries(wire_dir, "run-stream")[0]["metadata"]
    assert metadata["upstream_provider"] is None
    assert metadata["native_finish_reason"] is None
    assert metadata["upstream_state"] == "not_in_response"
    # Cost still lands: stream_chunk_builder carries usage across, which is
    # why the recovery channel does not need to carry it.
    assert metadata["usage_cost"] == pytest.approx(0.000123)


def test_the_streaming_hook_channel_supplies_what_the_rebuilt_dump_lost(wire_dir):
    """Hop 2 for the upstream identity: a keyed dict joined on
    litellm_call_id, because the value is produced in the streaming hook and
    read in a callback that runs from a context copied before it."""
    from bakeoff.proxy_callback import record_upstream

    call_id = "call-hooked"
    kwargs = {**kwargs_for("run-hooked"), "litellm_call_id": call_id}
    # Two chunks, each carrying one half: OpenRouter names the provider on the
    # first and the native finish reason on the last.
    record_upstream(call_id, provider="CoreWeave")
    record_upstream(call_id, native_finish_reason="tool_calls")
    BakeoffProxyCallback().log_success_event(kwargs, dict(ASSEMBLED_STREAM_DUMP), None, None)
    metadata = read_run_entries(wire_dir, "run-hooked")[0]["metadata"]
    assert metadata["upstream_provider"] == "CoreWeave"
    # Merged, not overwritten: the second chunk carried no provider and must
    # not blank the first one's.
    assert metadata["native_finish_reason"] == "tool_calls"
    assert metadata["upstream_state"] == "captured"


def test_an_unkeyed_upstream_capture_is_dropped_rather_than_guessed_at():
    """Same rule as record_resolved_params: attributing a capture on
    proximity puts one arm's upstream under another arm's entry."""
    from bakeoff.proxy_callback import record_upstream, upstream_for

    record_upstream(None, provider="CoreWeave")
    record_upstream("", provider="CoreWeave")
    assert upstream_for(None) is None
    assert upstream_for("no-such-call") is None
    record_upstream("call-empty", provider="", native_finish_reason=None)
    assert upstream_for("call-empty") is None


def test_the_upstream_channel_is_bounded():
    """The proxy is long-lived; a capture nobody reads is a leak."""
    from bakeoff import proxy_callback as pc

    for index in range(pc._UPSTREAM_MAX + 40):
        pc.record_upstream(f"bound-{index}", provider="CoreWeave")
    assert len(pc._UPSTREAM_BY_CALL) <= pc._UPSTREAM_MAX


def test_the_upstream_fields_never_come_from_the_configured_order(wire_dir):
    """The config's `order` is what was asked for. Only the response says
    who answered; absent, the field is None, not the yaml value."""
    kwargs = kwargs_for("run-cfg")
    kwargs["litellm_params"]["extra_body"] = {"provider": {"order": ["coreweave"]}}
    BakeoffProxyCallback().log_success_event(kwargs, {"choices": [{"finish_reason": "stop"}]}, None, None)
    assert read_run_entries(wire_dir, "run-cfg")[0]["metadata"]["upstream_provider"] is None
