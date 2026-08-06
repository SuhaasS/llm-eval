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
    ) -> None:
        if self._closed:
            raise RuntimeError("WireLogger is closed")

        payload = json.dumps({"request": request, "response": response}, default=str)
        entry = {
            "logged_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "request": request,
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


class BakeoffCallback:
    """LiteLLM CustomLogger hook. Registered via litellm.callbacks.

    LiteLLM invokes log_success_event / log_failure_event with the full
    kwargs (the outbound request), response_obj, and the real start/end
    timestamps of the call.
    """

    def __init__(self, logger: WireLogger, run_id: str) -> None:
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

        self.logger.log_call(
            request={
                "model": kwargs.get("model"),
                "messages": kwargs.get("messages"),
                "tools": kwargs.get("tools"),
                "system": kwargs.get("system"),
                "temperature": kwargs.get("temperature"),
                "max_tokens": kwargs.get("max_tokens"),
            },
            response=raw if isinstance(raw, dict) else {"raw_completion": str(raw)},
            metadata={
                "run_id": self.run_id,
                "call_index": self._call_index,
                "failed": failed,
                # Measured generation time, as opposed to the trajectory
                # parser's estimate from transcript timestamps.
                "latency_ms": self._latency_ms(start_time, end_time),
                "bedrock_request_id": (kwargs.get("litellm_params") or {}).get(
                    "request_id"
                ),
            },
        )

    def log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._record(kwargs, response_obj, False, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        self._record(kwargs, response_obj, True, start_time, end_time)
