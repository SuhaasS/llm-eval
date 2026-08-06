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


def test_resolved_params_win_over_the_raw_body(wire_dir):
    """optional_params is populated on routes that resolve params and is
    closer to what the provider received, so it takes precedence where it
    exists."""
    callback = BakeoffProxyCallback()
    callback.log_success_event(
        kwargs_for(optional_params={"temperature": 0.2}), {"ok": True}, None, None
    )
    assert read_run_entries(wire_dir, "run-abc")[0]["request"]["temperature"] == 0.2


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
