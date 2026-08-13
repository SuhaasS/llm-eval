"""Wire capture from inside the LiteLLM proxy. See spec section 6.2.

WHY THIS EXISTS. `wire.BakeoffCallback` registers on `litellm.callbacks` in
the harness process -- and the harness process makes no LLM calls. The agent
runs in its own container and talks to the proxy over HTTP, so every call the
eval cares about happens in a THIRD process. An in-process callback observes
none of them: `wire_entries` comes back empty on every real run, and with it
`sampling`, `system_prompt_sha`, `tool_schema_sha` and the API error status
that decides infra-failure exclusion. Silently, with the record still
well-formed.

Section 6.2 makes wire logging mandatory, so capture has to live where the
calls are: inside the proxy.

REGISTRATION. `callbacks: bakeoff.proxy_callback.instance` in the proxy's
config. The dotted path must resolve to the INSTANCE below, never to the
class -- `get_instance_fn` returns whatever the path resolves to as-is, and
LiteLLM's success handler dispatches on `isinstance(callback, CustomLogger)`.
A class object fails that check and is skipped in silence.

ATTRIBUTION. One file per run, named by the run_id the harness stamps on
every request via ANTHROPIC_CUSTOM_HEADERS. Verified against claude 2.1.220:
`ANTHROPIC_CUSTOM_HEADERS="X-Bakeoff-Run-Id: <id>"` appears on every
`POST /v1/messages`, and LiteLLM's `clean_headers` forwards any header it
does not recognise into request metadata.

A call that arrives with no run_id is written to `unattributed.jsonl` rather
than dropped or guessed at. Guessing would attach one run's calls to another
-- worse than a gap, because it is invisible. The gate treats a non-empty
unattributed file as a failure.

Files open in append mode, unlike `WireLogger`'s "x". The proxy is long-lived
and a run makes many calls, so exclusive creation would fail on the second
one. Collision protection stays where it is meaningful: the harness folds
these lines into the canonical gzipped artifact exactly once, with "x".
"""

from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from litellm.integrations.custom_logger import CustomLogger

RUN_ID_HEADER = "x-bakeoff-run-id"
UNATTRIBUTED = "unattributed"
DEFAULT_WIRE_DIR = "/eval/wire"

# The projection's key set, shared with wire.BakeoffCallback so the two capture
# paths cannot drift. A run's canonical artifact is written from whichever path
# was live -- the proxy one on a real run, the in-process one in tests and the
# dry run -- so a field present in one and absent from the other reads as "not
# sent on this arm" rather than "not captured on this path". That used to be a
# convention enforced by one test; it is now a constant both sides read.
REQUEST_KEYS = (
    "model",
    "messages",
    "tools",
    "system",
    "temperature",
    "max_tokens",
    # Both spellings, because the arms do not agree on one and the record must
    # say which was sent. openai_max_completion_tokens_rename moves the cap to
    # max_completion_tokens on the three candidate arms; Sonnet keeps
    # max_tokens on its native Anthropic body.
    "max_completion_tokens",
    # The params `additional_drop_params` removes, plus the beta values the two
    # transports carry differently. Recorded BECAUSE they are dropped, not
    # despite it: the drops are per-arm -- reasoning_effort is listed on each
    # candidate deployment, Sonnet's runtime deployment lists nothing -- and
    # whether a drop actually reached a given route is a section 6.4 question
    # about the arms this eval is comparing.
    "thinking",
    "reasoning_effort",
    "context_management",
    "output_config",
    "anthropic_beta",
    # Every real call streams; a False here means capture is looking at
    # something other than the agent's traffic.
    "stream",
)


def _wire_dir() -> Path:
    return Path(os.environ.get("BAKEOFF_WIRE_DIR", DEFAULT_WIRE_DIR))


# --- the resolved-params side channel ----------------------------------------
#
# WHY IT EXISTS. Measured 2026-08-12 against a real streaming provider call: the
# callback fires exactly once per request with `call_type: "anthropic_messages"`
# -- the OUTER call -- and the nested `acompletion` fires nothing of its own.
# Both openai param interventions run inside that nested call, so what the
# callback sees is the request before either of them. Worse than absent:
# `optional_params` and `standard_logging_object.model_parameters` are both
# POPULATED there and both carry `max_tokens: 16384` with no
# `max_completion_tokens` and no `reasoning_effort` -- the pre-rename state,
# reported with the provenance of a resolved param. The wire log named as the
# authority on what went over the wire was describing Claude Code instead.
#
# So the resolved params have to be carried up from where they are produced.
# This module owns the channel rather than bakeoff.litellm_patches, because the
# harness must be able to import the reader and importing that module APPLIES
# its patches. The handoff already runs in this direction -- litellm_patches
# imports write_manifest from here -- and never the reverse.
#
# TWO HOPS, because no single mechanism spans both ends.
#
# HOP 1, hook -> param mapping: a ContextVar. The hook runs once per request in
# the request's own context and the mapping runs deeper in the same one, so a
# value set at the top is visible at the bottom. That direction is the one this
# codebase has already measured working -- `_SEEN_TOOL_USE_IDS` reaches
# `AnthropicStreamWrapper.__init__` by exactly this route.
#
# HOP 2, param mapping -> callback: a keyed dict in this module, NOT a
# ContextVar. Measured 2026-08-12 in the real proxy: a ContextVar set in the
# hook reads back as unset in the success callback, on every arm and every call
# -- 9 for 9 across the offline gate. The callback runs from a context copied
# before the hook, which is the same boundary the per-chunk translation hits and
# which litellm_patches already records. A library-level reproduction does NOT
# show this, because there everything runs in one coroutine; that is why the
# offline gate and not a unit test is what settled it.
#
# The dict works because both ends are in ONE PROCESS and share an identity:
# litellm's own `litellm_call_id`, which the hook and the callback both receive
# and which is measured to hold the same value at both ends. No context
# propagation is involved in hop 2 at all.
#
# The ContextVar's default is None and must stay None. A shared mutable default
# is one object for every context that never called `set()`, so a request whose
# hook never ran would read another request's id and file its params under the
# wrong call -- on a proxy serving several arms at once, that is one arm's
# configuration recorded against another. None means "no request context reached
# here", which is a state the reader can detect and refuse to guess at.
_CURRENT_CALL_ID: ContextVar[str | None] = ContextVar(
    "bakeoff_resolved_call_id", default=None
)

_RESOLVED_BY_CALL: dict[str, dict[str, Any]] = {}
_RESOLVED_LOCK = threading.Lock()
# Bounded, because the proxy is long-lived and a capture nobody reads is a leak.
# A capture still unread after this many later calls is stale by any measure --
# the callback fires within milliseconds of the mapping.
_RESOLVED_MAX = 512


def open_resolved_capture(call_id: str | None = None) -> str | None:
    """Name the call whose resolved params are about to be produced.

    Called from the pre-request hook, the one place that runs once per request
    AND can see litellm's call id. The mapping reads it back out of the request
    context; without it the mapping cannot say which call its params belong to,
    and an unkeyed capture can only be attributed by guessing.
    """
    _CURRENT_CALL_ID.set(call_id)
    return call_id


def record_resolved_params(params: Mapping[str, Any]) -> None:
    """File what the provider is actually being sent, under the call it is for.

    A no-op when the request context carries no call id -- a route that never
    reached the hook records nothing rather than something it cannot attribute.
    Overwrites rather than merges: a retry maps its params again, and the last
    mapping is the one the attempt being logged actually used.
    """
    call_id = _CURRENT_CALL_ID.get()
    if not isinstance(call_id, str) or not call_id:
        return
    with _RESOLVED_LOCK:
        _RESOLVED_BY_CALL[call_id] = dict(params)
        while len(_RESOLVED_BY_CALL) > _RESOLVED_MAX:
            _RESOLVED_BY_CALL.pop(next(iter(_RESOLVED_BY_CALL)))


def resolved_state(call_id: str | None = None) -> str:
    """Why `resolved` is null, when it is. One of:

        captured      the provider boundary was observed
        no_call_id    the callback carries no litellm_call_id, so there is
                      nothing to join on
        not_recorded  no mapping filed params under this call -- either the
                      route does not resolve openai params (the ordinary state
                      of an `anthropic/` deployment) or hop 1 did not propagate

    A null with no reason is the shape this module exists to avoid: a route that
    legitimately resolves nothing and a hand-off that silently broke render as
    the same missing field, and only one of them is a defect. Not hypothetical
    -- this field is what identified the broken mechanism this channel replaced.
    """
    if not isinstance(call_id, str) or not call_id:
        return "no_call_id"
    with _RESOLVED_LOCK:
        return "captured" if call_id in _RESOLVED_BY_CALL else "not_recorded"


def resolved_params(call_id: str | None = None) -> dict[str, Any] | None:
    """What the mapping filed for THIS call, or None.

    Read without consuming: a failed call fires the failure callback twice, and
    a capture that vanished on the first fire would make the duplicate look like
    a different request. The bound above is what keeps the store finite.

    The key match is the whole guarantee. Attributing an unkeyed capture on the
    strength of proximity is the failure this exists to prevent -- one arm's
    configuration under another arm's record, at full plausibility, with nothing
    to notice it by.
    """
    if not isinstance(call_id, str) or not call_id:
        return None
    with _RESOLVED_LOCK:
        captured = _RESOLVED_BY_CALL.get(call_id)
    return dict(captured) if captured else None


def _headers(kwargs: dict) -> dict[str, str]:
    """Request headers, wherever this LiteLLM version put them.

    The proxy copies them into metadata under more than one key depending on
    the route and version, and reading only one would fail open -- every call
    unattributed, which looks exactly like a missing header on the agent
    side. Case is normalised because HTTP header case is not significant and
    Starlette lowercases on the way in.
    """
    params = kwargs.get("litellm_params") or {}
    for source in (
        params.get("metadata"),
        params.get("litellm_metadata"),
        kwargs.get("metadata"),
        kwargs.get("litellm_metadata"),
        # Last resort, and the most durable: the proxy records the raw
        # request here regardless of how metadata is keyed. Note it sits
        # under litellm_params, NOT at the top level -- verified against a
        # live proxy. Reading only the top level makes this branch dead.
        params.get("proxy_server_request"),
        kwargs.get("proxy_server_request"),
    ):
        headers = (source or {}).get("headers")
        if isinstance(headers, dict) and headers:
            return {str(k).lower(): v for k, v in headers.items()}
    return {}


def _status_code(kwargs: dict) -> int | None:
    status = getattr(kwargs.get("exception"), "status_code", None)
    return status if isinstance(status, int) else None


def project(source: Mapping[str, Any], model: Any = None) -> dict[str, Any]:
    """`source` reduced to REQUEST_KEYS, every key present, absent ones None.

    An explicit allowlist, never `**source`: the log carries a known shape, the
    secret scan in wire.py depends on it, and a missing key is not the same as
    a null. A null says this arm sent no thinking block; a missing key is also
    what an OLD log line looks like, and conflating the two is the version
    confusion the schema notes exist to prevent.
    """
    projected = {key: source.get(key) for key in REQUEST_KEYS}
    if model is not None:
        projected["model"] = model
    return projected


def _request(kwargs: dict) -> dict[str, Any]:
    """What the CLIENT sent. Claude Code's own body, with no fallback.

    Verified against litellm 1.95.0 on POST /v1/messages: max_tokens,
    temperature, system and tools are NOT top-level callback kwargs. The
    payload lives in litellm_params.proxy_server_request.body. Reading the top
    level -- the obvious choice, and the one the OpenAI route would reward --
    yields None for every sampling field, which is indistinguishable from a
    caller that sent none.

    This used to prefer `optional_params` and fall back to the body, under one
    flat key set. Measured 2026-08-12 and that was backwards: on this route
    `optional_params` belongs to the OUTER anthropic_messages call, so it
    carries `max_tokens: 16384` with no `max_completion_tokens` and no
    `reasoning_effort` -- the state BEFORE the two openai interventions, given
    the provenance of a resolved param. Mixing the two sources into one dict
    meant the entry could not say which field came from where, and on the one
    field the rename touches it reported a parameter the wire did not carry, at
    full plausibility. The sources are separate now: this is the request, and
    `_resolved` below is what the provider was actually sent.
    """
    body = dict(
        ((kwargs.get("litellm_params") or {}).get("proxy_server_request") or {}).get(
            "body"
        )
        or {}
    )
    # Injected by the proxy on the way in; not part of what the caller sent.
    body.pop("litellm_metadata", None)
    return project(
        body,
        # The resolved deployment, not the alias the agent asked for: the alias
        # is what was requested, this is what answered.
        model=(kwargs.get("litellm_params") or {}).get("model")
        or kwargs.get("model"),
    )


def _resolved(kwargs: dict) -> dict[str, Any] | None:
    """What the provider was sent, or None when nothing observed it.

    Comes from the side channel, never from `optional_params` -- see the
    channel's own comment for why the obvious source is the wrong one. None is
    load-bearing: it is what an arm whose resolved params were never captured
    must record, so that a reader falling back to `request` knows it is reading
    the client's intent rather than the wire.

    IT ALSO SETTLES SECTION 5.3. This function's predecessor carried a standing
    "KNOWN GAP, to verify at Phase 0c": a temperature configured on the
    DEPLOYMENT appeared nowhere the callback could see, so the wire log could
    not confirm the sampling section 5.3 puts entirely in proxy config was ever
    applied -- Claude Code has no temperature flag, so nothing else could
    confirm it either. Measured through this channel on the offline gate,
    2026-08-12: the `openai/` arm's configured `temperature: 1.0` is in the
    resolved params. It was reaching the provider all along and was simply not
    observable. On the `anthropic/` arms it still is not, and those record
    `not_recorded` rather than a claim.
    """
    captured = resolved_params(kwargs.get("litellm_call_id"))
    if not captured:
        return None
    return project(captured, model=captured.get("model"))


def _error(kwargs: dict) -> dict[str, Any] | None:
    """The failure, as much of it as the exception carries.

    `response_obj` is None on a failure, so without this a 400 lands as
    `{"raw_completion": "None"}` and the log says only that something went
    wrong. Every diagnosis so far -- the mantle Sonnet arm's `invalid beta
    flag`, Gemma's `-32602`, Gemma's `max_tokens` rejection -- had to come from
    grepping the proxy's own stdout instead, and in a real run the proxy log is
    not an artifact of any record. Section 6.2 exists so a failure can be
    diagnosed after the fact.

    The provider's own words are the point. `str(exception)` on a LiteLLM
    exception already embeds the upstream message, and `.response` carries the
    raw body when the failure came back over HTTP rather than from a
    client-side guard -- which is itself the distinction section 6.4 turns on.
    """
    exc = kwargs.get("exception")
    if exc is None:
        return None
    body: Any = None
    response = getattr(exc, "response", None)
    if response is not None:
        body = getattr(response, "text", None)
        if body is None:
            body = getattr(response, "content", None)
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "status_code": _status_code(kwargs),
        "provider": getattr(exc, "llm_provider", None),
        "body": body,
    }


def finish_reason(response: dict[str, Any]) -> str | None:
    """The provider's own word for why generation stopped, or None.

    The record already carries `per_turn[].stop_reason`, but that is Claude
    Code's TRANSLATED value read from the transcript -- Anthropic vocabulary
    (`end_turn` / `tool_use` / `max_tokens`) that litellm produced from whatever
    the route returned. `terminated_by` is derived from the last of those, so
    every claim the record makes about how a run ended passes through one
    mapping that nothing recorded.

    Measured 2026-08-13 across all four arms: the logged response is an
    OpenAI-shaped ModelResponse dump, so `choices[0].finish_reason` is present
    on every arm -- including claude-sonnet-5-runtime, whose bedrock Invoke
    route carries a native Anthropic body. `stop_reason` is the fallback for a
    route that logs the Anthropic shape instead.

    None on a failure and on an unparsed `raw_completion`: no stopping decision
    was reached there, and any string would be a claim invented from an absence.
    """
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("finish_reason")
        if isinstance(value, str) and value:
            return value
    value = response.get("stop_reason")
    return value if isinstance(value, str) and value else None


def _iso(value: Any) -> str | None:
    """A callback timestamp as ISO-8601, or None if it was not one.

    None rather than str(value): a datetime that failed to arrive must read as
    unmeasured, not as a string a consumer will try to parse.
    """
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return None
    try:
        return str(isoformat()).replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _serializable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return value


class BakeoffProxyCallback(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        # The proxy serves requests concurrently; two runs writing the same
        # file would interleave partial lines.
        self._lock = threading.Lock()
        self._call_index: dict[str, int] = {}

    def _write(self, kwargs: dict, response_obj: Any, failed: bool,
               start_time: Any, end_time: Any) -> None:
        run_id = _headers(kwargs).get(RUN_ID_HEADER) or UNATTRIBUTED
        raw = _serializable(response_obj)
        latency = None
        try:
            latency = int((end_time - start_time).total_seconds() * 1000)
        except (TypeError, AttributeError):
            pass

        error = _error(kwargs) if failed else None
        if error is not None:
            # Under `response`, deliberately. WireLogger builds the secret-scan
            # payload from `request` and `response` only, so an error body
            # anywhere else in the entry would be the one field the scan cannot
            # see -- and a provider error body is exactly the place a request
            # gets echoed back.
            response: dict[str, Any] = {"error": error}
        elif isinstance(raw, dict):
            response = raw
        else:
            response = {"raw_completion": str(raw)}

        with self._lock:
            self._call_index[run_id] = self._call_index.get(run_id, 0) + 1
            index = self._call_index[run_id]
            entry = {
                "logged_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "request": _request(kwargs),
                "resolved": _resolved(kwargs),
                "response": response,
                "metadata": {
                    "run_id": None if run_id == UNATTRIBUTED else run_id,
                    "call_index": index,
                    "failed": failed,
                    "status_code": _status_code(kwargs) if failed else None,
                    # The route's own word, beside the status. Kept here rather
                    # than left inside `choices` so a reader does not have to
                    # know which shape this route logs.
                    "finish_reason": finish_reason(response),
                    "latency_ms": latency,
                    # The real call boundaries, not the moment this callback got
                    # around to writing. Nothing else in the artifact carries
                    # them: `logged_at` is the callback's own clock and is
                    # re-stamped again when the harness folds these entries into
                    # the canonical log, so before this every timestamp in the
                    # gzipped artifact was post-run.
                    "started_at": _iso(start_time),
                    "ended_at": _iso(end_time),
                    # What ties several entries to one logical call. Measured
                    # 2026-08-12: a single FAILED provider call fires the
                    # failure callback TWICE with the same litellm_call_id, so
                    # `wire_entries_seen` over-counts every failure -- and the
                    # 39-served/42-captured surplus recorded in TASKS.md as
                    # three retries is three double-logged failures. Retries
                    # land here too. Nothing else in the entry can separate
                    # either from a genuinely distinct call.
                    "litellm_call_id": kwargs.get("litellm_call_id"),
                    "litellm_trace_id": kwargs.get("litellm_trace_id"),
                    # Why `resolved` is null when it is. Three unrelated states
                    # render as the same missing field and only one of them is
                    # a defect; see resolved_state.
                    "resolved_state": resolved_state(kwargs.get("litellm_call_id")),
                },
            }
            directory = _wire_dir()
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / f"{run_id}.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
                handle.flush()

    def log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._write(kwargs, response_obj, False, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._write(kwargs, response_obj, True, start_time, end_time)

    async def async_log_success_event(
        self, kwargs, response_obj, start_time, end_time
    ) -> None:
        # The proxy serves async; without this override the sync handler is
        # never reached on the async path and nothing is captured.
        self._write(kwargs, response_obj, False, start_time, end_time)

    async def async_log_failure_event(
        self, kwargs, response_obj, start_time, end_time
    ) -> None:
        self._write(kwargs, response_obj, True, start_time, end_time)


# The dotted path in the proxy config points HERE. Pointing it at the class
# above yields a callback that is silently skipped.
instance = BakeoffProxyCallback()


# --- the adapter-patch manifest ---------------------------------------------
#
# Lives here rather than in bakeoff.litellm_patches because the harness must be
# able to READ it, and importing that module applies its patches -- which would
# patch litellm in the harness process and in every pytest run. This module is
# already the proxy/harness file-handoff contract (see read_run_entries below),
# so the manifest belongs with it.

MANIFEST_NAME = "adapter_patches.json"


def write_manifest(
    patches: list[str], litellm_version: str, wire_dir: Path
) -> Path | None:
    """Proxy side: record what this process did to its own litellm.

    Written by the proxy about itself, deliberately. Section 6.1's rule is that
    configuration is never reported as observation -- a record must not claim a
    patch was active because a config file asked for one -- and the harness's
    own litellm version says nothing about the container, which pins its own.
    """
    try:
        wire_dir.mkdir(parents=True, exist_ok=True)
        path = wire_dir / MANIFEST_NAME
        path.write_text(
            json.dumps(
                {"patches": sorted(patches), "litellm": litellm_version},
                sort_keys=True,
            )
        )
        return path
    except OSError:
        # A manifest that cannot be written must not take the proxy down; the
        # absence is itself readable as "no claim made".
        return None


def read_manifest(wire_dir: Path) -> tuple[list[str], str]:
    """Harness side: what the proxy reported, or nothing if it reported nothing.

    An absent manifest returns empty values. That is distinguishable from a
    proxy reporting an empty patch set only by the litellm version also being
    blank, and both readings are honest: neither invents a claim.
    """
    path = Path(wire_dir) / MANIFEST_NAME
    if not path.exists():
        return [], ""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return [], ""
    return [str(p) for p in (data.get("patches") or [])], str(data.get("litellm") or "")


def read_run_entries(wire_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Entries the proxy recorded for one run, in call order.

    Tolerant of a partial final line: the proxy may be mid-write when the run
    ends, and losing every captured call over one truncated line is the data
    loss this whole module exists to prevent.
    """
    path = Path(wire_dir) / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def unattributed_count(wire_dir: Path) -> int:
    """Calls the proxy could not attribute to a run. Any non-zero value is a
    gate failure: those calls happened, were paid for, and are not joined to
    any record."""
    path = Path(wire_dir) / f"{UNATTRIBUTED}.jsonl"
    if not path.is_file():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])
