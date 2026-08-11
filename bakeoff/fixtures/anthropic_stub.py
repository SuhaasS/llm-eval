#!/usr/bin/env python3
"""A streaming Anthropic Messages endpoint, for the offline half of Phase 0c.

Why this exists rather than a `mock_response` deployment.

LiteLLM's mock short-circuits inside anthropic_messages BEFORE the provider
call, so a streaming request returns a plain dict and the streaming wrapper
-- the thing that fires the success callback when a stream completes --
never runs. Measured against litellm 1.95.0 with claude 2.1.220: the agent
made two calls, one streaming and one not, and only the non-streaming one
reached the proxy-side callback. Since every real Claude Code call is
streaming, a gate built on mocks would certify capture that does not
happen.

This stub speaks real Anthropic SSE, so litellm takes its ordinary provider
and streaming path and the capture being tested is the capture that will
run. It answers with canned tool calls and then a canned end_turn, which also
lets the offline gate check tool translation and a real diff instead of
declaring both untestable.

What it deliberately does NOT do is model anything. It is a fixed script:
turn one writes the fix, turn two stops. No model behaviour is being
measured here -- that is what --mode live is for.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080

# The corrected fixture. Written by the stub's canned tool call, so a diff
# lands and the section 5.6 staged-diff extraction has something to extract.
FIXED = "def add(a, b):\n    return a + b\n"


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def tool_use_stream(name: str, arguments: dict) -> list[bytes]:
    """One tool_use block, streamed the way Anthropic streams one.

    input_json_delta rather than a whole input object: partial JSON is how
    tool arguments actually arrive, and reassembling them is exactly where
    an adapter breaks (spec section 6.4). A stub that skipped it would test
    a shape no provider sends.
    """
    payload = json.dumps(arguments)
    return [
        sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stub_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1200, "output_tokens": 1},
                },
            },
        ),
        sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": f"toolu_stub_{name.lower()}",
                    "name": name,
                    "input": {},
                },
            },
        ),
        sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": payload},
            },
        ),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 90},
            },
        ),
        sse("message_stop", {"type": "message_stop"}),
    ]


def text_stream() -> list[bytes]:
    return [
        sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stub_2",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1400, "output_tokens": 1},
                },
            },
        ),
        sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Fixed the sign bug."},
            },
        ),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 12},
            },
        ),
        sse("message_stop", {"type": "message_stop"}),
    ]


def non_streaming(body: dict) -> dict:
    """Claude Code also issues a non-streaming call. Answered plainly so it
    is one more captured entry rather than an error the gate has to
    special-case."""
    return {
        "id": "msg_stub_ns",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "claude-sonnet-5"),
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 4},
    }


def tool_results(body: dict) -> int:
    """How many tool results have come back so far.

    Counted from the conversation rather than from a counter on the server:
    the proxy retries, and a per-process counter would advance on a retry,
    so the stub would answer turn two to a repeat of turn one.
    """
    seen = 0
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    seen += 1
    return seen


def script(body: dict) -> list[bytes]:
    """Read, then Write, then stop.

    The Read is not decoration: Claude Code refuses to write a file it has
    not read -- "File has not been read yet. Read it first before writing to
    it." -- so a stub that goes straight to Write lands no diff and the gate
    reports a failure that belongs to the stub. Observed against claude
    2.1.220.
    """
    done = tool_results(body)
    if done == 0:
        return tool_use_stream("Read", {"file_path": "/repo/calc.py"})
    if done == 1:
        return tool_use_stream(
            "Write", {"file_path": "/repo/calc.py", "content": FIXED}
        )
    return text_stream()


# --- the OpenAI-compatible half --------------------------------------------
#
# Served from the same process, on /v1/chat/completions, so the offline gate
# can reach the openai->anthropic adapter. The Anthropic half above cannot:
# an `anthropic/` deployment bypasses that adapter entirely, which is why the
# 2026-08-07 tool-id defect was invisible offline and cost a live run to find.
#
# This one id carries BOTH live adapter defects, which is why it is shaped the
# way it is and why it is issued twice.
#
# Kimi-shaped, because litellm's normalize_anthropic_tool_use_id rewrites every
# character outside [a-zA-Z0-9_-] to an underscore: without the passthrough
# patch it comes back as `functions_Read_0`. An id already inside Anthropic's
# pattern would make that half of the check vacuous.
#
# Issued TWICE, because Gemma's ids are indexed within a response and it emits
# one call per response, so every response came back as the same `call_0`.
# Without the uniquify patch Claude Code cannot pair the duplicate, drops the
# second call, and this arm lands no diff.
COLLIDING_ID = "functions.Read:0"

# What the round trip may legitimately come back as. The bare id on the first
# result; the uniquified form on the second, since the patch rewrites the
# duplicate before Claude Code ever sees it.
ISSUED_IDS = {COLLIDING_ID, f"{COLLIDING_ID}_1"}


def validate_tool_call_ids(body: dict) -> str | None:
    """Reject a tool_call_id this stub never issued.

    Without this the round trip is unchecked: a mangled id would come back, the
    stub would answer anyway, and the gate would pass while the adapter
    silently corrupted every tool call.

    Two different defects land here now, so the message says which. A
    `functions_Read_0` means the passthrough patch died and the character class
    was rewritten. A second bare `functions.Read:0` means the uniquify patch
    died -- though that one usually shows up as a missing Write and an empty
    diff rather than reaching this check at all, because Claude Code drops the
    unpairable call before it is ever echoed back.

    Returns an error string, or None.
    """
    echoed = [
        m.get("tool_call_id")
        for m in body.get("messages", [])
        if m.get("role") == "tool"
    ]
    for seen in echoed:
        if seen not in ISSUED_IDS:
            return (
                f"tool_call_id {seen!r} was never issued by this stub "
                f"(issued {sorted(ISSUED_IDS)}) -- the adapter rewrote it, so "
                "anthropic_tool_use_id_passthrough is not applying"
            )
    if len(echoed) != len(set(echoed)):
        return (
            f"tool_call_id echoed twice: {echoed} -- the duplicate the stub "
            "issued was never uniquified, so "
            "anthropic_tool_use_id_collision_uniquify is not applying"
        )
    return None


def validate_no_property_names(body: dict) -> str | None:
    """Reject a request whose tool schemas still carry ``propertyNames``.

    The 2026-08-10 defect: Gemma 4 31B failed 9/9 live runs with `JSON-RPC
    error -32602`, and the cause was this one JSON-Schema keyword on two of
    Claude Code's 24 tools (TaskCreate and TaskUpdate, both on
    `properties.metadata`). `bakeoff.litellm_patches` strips it in a
    pre-request hook.

    That hook is the half of the fix nothing else can verify. `apply()` can
    check that the strip function works, but not that litellm ever calls it --
    that depends on the class being registered AND on `anthropic_messages`
    still dispatching pre-request hooks, either of which a litellm bump could
    take away in silence. This check is the only thing standing between that
    and a green gate over a dead hook.

    No fixture arm is needed to reach it: the gate runs the real pinned Claude
    Code, which DECLARES all 24 tools on every request whether or not it calls
    them. Both routes are checked, because the hook runs before litellm
    branches on provider and so must clean the `anthropic/` arms too.

    Returns an error string, or None.
    """
    def offenders(obj, path: str = "") -> list[str]:
        if isinstance(obj, dict):
            found = [path] if "propertyNames" in obj else []
            for key, value in obj.items():
                found += offenders(value, f"{path}.{key}" if path else key)
            return found
        if isinstance(obj, list):
            return [f for i, v in enumerate(obj) for f in offenders(v, f"{path}[{i}]")]
        return []

    for field in ("tools", "messages", "system"):
        hits = offenders(body.get(field), field)
        if hits:
            return (
                f"propertyNames survived in {hits[0]!r} -- the "
                "anthropic_tool_schema_property_names_strip hook did not run. "
                "Gemma's Bedrock engine answers -32602 to this."
            )
    return None


def validate_loop_progress(body: dict) -> str | None:
    """Reject a conversation that is not advancing through the script.

    The openai script is exactly three calls -- Read, Write, done -- so the
    stub is never legitimately asked a fourth time. More than that means the
    tool loop is spinning: the client could not pair a tool call, so no result
    came back, so the stub reissues the same step forever.

    This check exists because the obvious signals do not fire. Measured
    2026-08-11 with the uniquify patch disabled: the arm ran 60 turns and 30
    tool calls, and STILL landed the 157-byte diff, so `diff_b` and the run
    verdict both stayed green. `validate_tool_call_ids` did not fire either --
    Claude Code drops the unpairable call rather than echoing a duplicate, so
    nothing wrong ever comes back over the wire. Without this the gate reports
    GO over Gemma's exact failure.

    Returns an error string, or None.
    """
    assistant_turns = sum(
        1 for m in body.get("messages", []) if m.get("role") == "assistant"
    )
    if assistant_turns > 2:
        return (
            f"the openai script is 3 calls but the conversation already has "
            f"{assistant_turns} assistant turns -- the tool loop is not "
            "advancing, so a tool call went unpaired and "
            "anthropic_tool_use_id_collision_uniquify is not applying"
        )
    return None


def _openai_tool_call(call_id: str, name: str, arguments: dict) -> list[bytes]:
    call = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    first = {
        "choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]
    }
    last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
    return [
        b"data: " + json.dumps(first).encode() + b"\n\n",
        b"data: " + json.dumps(last).encode() + b"\n\n",
        b"data: [DONE]\n\n",
    ]


def openai_chunks(body: dict) -> list[bytes]:
    """Read, then Write, then stop -- the same shape as the Anthropic script.

    The Read is not decoration here either: Claude Code refuses to write a
    file it has not read, so a stub that goes straight to Write lands no diff
    and the gate reports a failure that belongs to the stub.
    """
    done = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
    if done == 0:
        return _openai_tool_call(
            COLLIDING_ID, "Read", {"file_path": "/repo/calc.py"}
        )
    if done == 1:
        # The SAME id again, deliberately -- this is Gemma's defect reproduced.
        # Its ids are indexed within a response and it emits one call per
        # response, so every response came back as `call_0`; Claude Code could
        # not pair the duplicate, the second call was dropped, and the arm made
        # no diff. Without the uniquify patch this Write never runs and this
        # arm goes red, which is the only offline evidence that the patch is
        # not just present but actually reached on the streaming path.
        return _openai_tool_call(
            COLLIDING_ID,
            "Write",
            {"file_path": "/repo/calc.py", "content": FIXED},
        )
    payload = {
        "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]
    }
    return [b"data: " + json.dumps(payload).encode() + b"\n\n", b"data: [DONE]\n\n"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # noqa: A003 - quieter than the default
        pass

    def _json(self, status: int, payload: dict) -> None:
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _sse(self, chunks: list[bytes]) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        # Anything that is neither route is a routing regression, and it must
        # be loud. Answering /v1/responses with an Anthropic-shaped body -- the
        # old behaviour, by falling through -- turned a wrong-endpoint bug into
        # an unhelpful parse error two layers away. Measured while adding the
        # openai arm: without use_chat_completions_url_for_anthropic_messages
        # LiteLLM routes here, and the fall-through hid why.
        if not ("chat/completions" in self.path or self.path.endswith("/messages")):
            self._json(
                404,
                {
                    "error": {
                        "message": f"stub serves /v1/messages and "
                        f"/v1/chat/completions, not {self.path}",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        # Checked on BOTH routes: the propertyNames strip is a pre-request hook
        # that runs before litellm branches on provider, so the `anthropic/`
        # arms must come through clean too. A check on the openai route alone
        # would leave three of the four offline arms unguarded.
        problem = validate_no_property_names(body)
        if problem:
            self._json(400, {"error": {"message": problem, "type": "invalid_request_error"}})
            return

        if "chat/completions" in self.path:
            problem = validate_tool_call_ids(body) or validate_loop_progress(body)
            if problem:
                # 400 rather than a silent retry: the gate has to SEE this.
                self._json(400, {"error": {"message": problem, "type": "invalid_request_error"}})
                return
            self._sse(openai_chunks(body))
            return

        if not body.get("stream"):
            self._json(200, non_streaming(body))
            return

        self._sse(script(body))

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104
