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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

RUN_ID_HEADER = "x-bakeoff-run-id"
UNATTRIBUTED = "unattributed"
DEFAULT_WIRE_DIR = "/eval/wire"


def _wire_dir() -> Path:
    return Path(os.environ.get("BAKEOFF_WIRE_DIR", DEFAULT_WIRE_DIR))


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


def _request(kwargs: dict) -> dict[str, Any]:
    """What was actually sent, as far as the proxy can see it.

    Verified against litellm 1.95.0 on POST /v1/messages: max_tokens,
    temperature, system and tools are NOT top-level callback kwargs and
    optional_params is empty. The payload lives in
    litellm_params.proxy_server_request.body. Reading the top level -- the
    obvious choice, and the one the OpenAI route would reward -- yields None
    for every sampling field, which is indistinguishable from a caller that
    sent none.

    KNOWN GAP, to verify at Phase 0c: a temperature configured on the
    DEPLOYMENT (litellm_params in the model list) does not appear anywhere
    the callback can see on this route -- not in optional_params, not in
    litellm_params, not in standard_logging_object.model_parameters. That
    was observed against a mock_response deployment, which short-circuits
    before provider param transformation, so it does not prove the value is
    dropped on a real call. It does mean the wire log cannot yet confirm
    section 5.3 sampling was applied, and section 5.3 puts sampling entirely
    in proxy config because Claude Code has no temperature flag. Task 12
    must check this against a real endpoint; if the value is genuinely
    dropped, two arms run at unspecified sampling.
    """
    body = dict(
        ((kwargs.get("litellm_params") or {}).get("proxy_server_request") or {}).get(
            "body"
        )
        or {}
    )
    # Injected by the proxy on the way in; not part of what the caller sent.
    body.pop("litellm_metadata", None)
    # Populated on routes that resolve params (chat/completions); empty on
    # the anthropic passthrough. Wins where present, since it is closer to
    # what the provider received.
    resolved = kwargs.get("optional_params") or {}

    def pick(name: str) -> Any:
        return resolved.get(name, body.get(name))

    return {
        # The resolved deployment, not the alias the agent asked for: the
        # alias is what was requested, this is what answered.
        "model": (kwargs.get("litellm_params") or {}).get("model")
        or kwargs.get("model"),
        "messages": pick("messages"),
        "tools": pick("tools"),
        "system": pick("system"),
        "temperature": pick("temperature"),
        "max_tokens": pick("max_tokens"),
        # Both spellings, because the arms do not agree on one and the record
        # must say which was sent. openai_max_completion_tokens_rename moves
        # the cap to max_completion_tokens on the three candidate arms; Sonnet
        # keeps max_tokens on its native Anthropic body.
        #
        # Recording only max_tokens would be worse than losing the field.
        # `pick` prefers optional_params and falls back to the raw body, and
        # the raw body is Claude Code's request, which still carries
        # max_tokens on every arm -- so a renamed call would report a
        # parameter the wire did not carry, at full plausibility. That is
        # configuration reported as observation, on the one field the rename
        # touches.
        "max_completion_tokens": pick("max_completion_tokens"),
        # The params `litellm_settings.additional_drop_params` removes, plus
        # the beta values the two transports carry differently. Recorded
        # BECAUSE they are dropped, not despite it: the drops are per-arm --
        # reasoning_effort is listed on each candidate deployment, Sonnet's
        # runtime deployment lists nothing -- and whether a drop actually
        # reached a given route is a section 6.4 question about the arms this
        # eval is comparing.
        #
        # Until 3.1.0 this projection was six keys and these were not among
        # them, so the log designated as the authority on "what was sent"
        # discarded exactly the fields whose divergence was open. The only
        # answer available was an outcome proxy: 856 calls across five arms,
        # zero reasoning tokens on any of them, which says no arm was
        # thinking but cannot say what any arm was asked to do.
        #
        # An explicit allowlist, never **body: the design is that the log
        # carries a known shape, and the secret scan in wire.py depends on
        # it. A new field here is a deliberate act.
        "thinking": pick("thinking"),
        "reasoning_effort": pick("reasoning_effort"),
        "context_management": pick("context_management"),
        "output_config": pick("output_config"),
        "anthropic_beta": pick("anthropic_beta"),
        # Every real call streams; a False here means capture is looking at
        # something other than the agent's traffic.
        "stream": pick("stream"),
    }


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

        with self._lock:
            self._call_index[run_id] = self._call_index.get(run_id, 0) + 1
            index = self._call_index[run_id]
            entry = {
                "logged_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "request": _request(kwargs),
                "response": raw if isinstance(raw, dict)
                else {"raw_completion": str(raw)},
                "metadata": {
                    "run_id": None if run_id == UNATTRIBUTED else run_id,
                    "call_index": index,
                    "failed": failed,
                    "status_code": _status_code(kwargs) if failed else None,
                    "latency_ms": latency,
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
