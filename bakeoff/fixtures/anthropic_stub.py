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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # noqa: A003 - quieter than the default
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        if not body.get("stream"):
            payload = json.dumps(non_streaming(body)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        chunks = script(body)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104
