"""Wire-level request/response capture. See spec section 6.2.

Without the raw completion, `malformed: true` is a dead end — you can never
determine what was malformed, or whether the fault was LiteLLM's tool
translation rather than the model. That distinction decides whether to fix
the adapter or drop the candidate (spec section 6.4).

Secrets are FLAGGED, never redacted: the eval needs the true payload, and
flags drive the pre-share scrub (spec section 3.6).

Logs open with mode "x" like the event log (global constraints): a wire log
is the one artifact that cannot be reconstructed after the fact, so a
name collision must fail rather than silently truncate.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

# The projection contract lives in proxy_callback because that is the module
# whose docstring owns it. Importing it here is safe and importing
# bakeoff.litellm_patches would not be -- that one applies its patches on
# import. proxy_callback holds no patches, only the file-handoff contract.
from bakeoff.proxy_callback import (
    _error,
    _iso,
    finish_reason,
    project,
    upstream,
    upstream_state,
)
from bakeoff.scanners import scan_secrets


class WireLogger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = gzip.open(self.path, "xt", encoding="utf-8")
        self._closed = False
        self._entries: list[dict[str, Any]] = []

    def log_call(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
        resolved: dict[str, Any] | None = None,
        logged_at: str | None = None,
    ) -> None:
        """Fold one call into the canonical artifact.

        `logged_at` is the ORIGINAL capture time and must be passed through when
        replaying proxy entries. Without it this method stamped `now()` over
        every replayed line, so each timestamp in the gzipped artifact was the
        harness's post-run replay time rather than the call time -- the whole
        artifact carried one clock reading, spread across the calls it was
        supposed to order. `replayed_at` keeps the harness's own stamp beside
        it rather than in place of it.

        `resolved` is what the provider was actually sent, or None when nothing
        observed it. Kept separate from `request` on purpose: see
        proxy_callback._request.
        """
        if self._closed:
            raise RuntimeError("WireLogger is closed")

        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        payload = json.dumps(
            {"request": request, "resolved": resolved, "response": response},
            default=str,
        )
        entry = {
            "logged_at": logged_at or now,
            "replayed_at": now if logged_at else None,
            "request": request,
            "resolved": resolved,
            "response": response,
            "metadata": metadata,
            "secret_flags": scan_secrets(payload),
        }
        self._entries.append(entry)
        self._handle.write(json.dumps(entry, default=str) + "\n")
        self._handle.flush()

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def close(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True


class BakeoffCallback(CustomLogger):
    """LiteLLM callback hook. Registered via litellm.callbacks.

    LiteLLM invokes log_success_event / log_failure_event with the full
    kwargs (the outbound request), response_obj, and the real start/end
    timestamps of the call.

    Subclassing CustomLogger is load-bearing, not decorative. LiteLLM's
    success_handler dispatches on `isinstance(callback, CustomLogger)`, with
    the only other branch being plain callables. A duck-typed object with
    matching method names is skipped in silence — no wire log, no error —
    and spec section 6.2 makes wire logging mandatory.

    Register an INSTANCE, per run:

        litellm.callbacks = [BakeoffCallback(wire_logger, run_id)]

    The proxy's `litellm_settings.callbacks: dotted.path` form cannot work
    here: get_instance_fn resolves the dotted path with getattr and returns
    it as-is, so the class object lands in the callback list, fails the
    isinstance check, and logs nothing.
    """

    def __init__(self, logger: WireLogger, run_id: str) -> None:
        super().__init__()
        self.logger = logger
        self.run_id = run_id
        # Deliberately NOT a turn counter. LiteLLM fires once per API call,
        # and the proxy retries, so one agent turn can produce several
        # calls. Naming this "turn" would invite a silent mis-join against
        # trajectory turn numbers.
        self._call_index = 0

    @staticmethod
    def _latency_ms(start: Any, end: Any) -> int | None:
        try:
            return int((end - start).total_seconds() * 1000)
        except (TypeError, AttributeError):
            return None

    @staticmethod
    def _status_code(kwargs: dict) -> int | None:
        """HTTP status of a failed call.

        This is the only place the harness ever sees one. LiteLLM puts the
        exception object on the failure kwargs, and its exceptions carry the
        provider status (RateLimitError 429, Timeout 408,
        ServiceUnavailableError 503) -- exactly the codes classify_exclusion
        maps to pre-registered infra reasons. Without it a Bedrock throttle
        is indistinguishable from the model giving up, and gets scored
        against the model.
        """
        status = getattr(kwargs.get("exception"), "status_code", None)
        return status if isinstance(status, int) else None

    def _record(
        self,
        kwargs: dict,
        response_obj: Any,
        failed: bool,
        start_time: Any = None,
        end_time: Any = None,
    ) -> None:
        self._call_index += 1
        raw = response_obj
        if hasattr(response_obj, "model_dump"):
            raw = response_obj.model_dump()
        elif hasattr(response_obj, "__dict__"):
            raw = dict(response_obj.__dict__)

        error = _error(kwargs) if failed else None
        # Built once, so the projection below and the finish_reason stamp in
        # `metadata` are reading the same object.
        payload = (
            {"error": error}
            if error is not None
            else raw if isinstance(raw, dict) else {"raw_completion": str(raw)}
        )
        upstream_provider, native_finish_reason, usage_cost, upstream_usage = upstream(kwargs, payload)
        self.logger.log_call(
            # Projected through the shared allowlist, so this path and the proxy
            # one cannot drift: a run's canonical artifact is written from
            # whichever was live, and a field present in one projection and
            # absent from the other would read as "not sent on this arm".
            request=project(kwargs),
            # None, always, on this path. There is no separate resolution to
            # observe here -- the harness IS the caller, so `kwargs` is both the
            # request and what goes out. Reporting them as a resolution would
            # claim an observation of the provider boundary that this path never
            # makes.
            resolved=None,
            response=payload,
            metadata={
                "run_id": self.run_id,
                "call_index": self._call_index,
                "failed": failed,
                "status_code": self._status_code(kwargs) if failed else None,
                # Same key as proxy_callback._write. A field on one capture
                # path only reads as "this arm did not report one".
                "finish_reason": finish_reason(payload),
                # Same keys, and the same reader, as proxy_callback._write.
                # NOT hardcoded None: unlike `resolved`, these are properties
                # of the RESPONSE, so this path observes them exactly as well
                # as the proxy path does whenever the route reports them --
                # this callback receives the same kwargs and the same response
                # object. Hardcoding None here would have filed "no upstream
                # named itself" as a measurement on a run where one did.
                # `upstream_state` says which kind of null a None is.
                "upstream_provider": upstream_provider,
                "native_finish_reason": native_finish_reason,
                "usage_cost": usage_cost,
                # Same key as proxy_callback._write, and the same rule: never
                # the logged dump's rebuilt usage, only the raw chunk's or
                # original_response's. See upstream()'s docstring.
                "upstream_usage": upstream_usage,
                "upstream_state": upstream_state(upstream_provider),
                # Measured generation time, as opposed to the trajectory
                # parser's estimate from transcript timestamps.
                "latency_ms": self._latency_ms(start_time, end_time),
                "started_at": _iso(start_time),
                "ended_at": _iso(end_time),
                "litellm_call_id": kwargs.get("litellm_call_id"),
                "litellm_trace_id": kwargs.get("litellm_trace_id"),
                "bedrock_request_id": (kwargs.get("litellm_params") or {}).get(
                    "request_id"
                ),
            },
        )

    def log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._record(kwargs, response_obj, False, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._record(kwargs, response_obj, True, start_time, end_time)
