"""Parse a Claude Code session JSONL into turns, tool calls, and usage.

Field layout verified against real transcripts: assistant records carry
message.model, message.usage, timestamp, uuid, parentUuid, requestId, and
version. Tool results arrive as separate records carrying toolUseResult.

Parsing is deliberately tolerant: a run killed mid-write leaves a partial
final line, and losing the whole trajectory over one truncated line would
violate the spec's "no data loss" requirement (section 6).

Timing follows spec section 6.1, which defines inference_ms as model
generation time and tool_exec_ms as test runs and builds. Both are read off
record timestamps: an assistant record's inference is the gap since the
record that unblocked it (the user prompt, or the preceding tool result),
and a tool result's gap since its assistant record is that turn's tool
execution. Measuring inference as the span between consecutive assistant
records instead would fold tool execution into it and leave tool_exec_ms
permanently zero -- a silent corruption of the latency success criterion,
since nothing downstream can distinguish a slow model from a slow test run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from bakeoff.costs import UnknownModelError, cost_usd
from bakeoff.schema import TokenUsage, ToolCallStats, TurnRecord

EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})


@dataclass
class ParsedTrajectory:
    turns: list[TurnRecord] = field(default_factory=list)
    tool_calls: ToolCallStats = field(default_factory=ToolCallStats)
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    # None means "at least one turn could not be priced", never "free". The
    # default stays 0.0 because a trajectory with no turns is an honest empty
    # sum; only the guard below may turn it into None. See pricing_error.
    total_cost_usd: float | None = 0.0
    model: str = ""
    claude_code_version: str = ""
    first_edit_turn: int | None = None
    first_edit_offset_ms: int | None = None
    bash_commands: list[tuple[int, str]] = field(default_factory=list)
    malformed_lines: int = 0
    # Why the cost is unknown. Non-empty exactly when total_cost_usd is None.
    pricing_error: str = ""


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return int((end - start).total_seconds() * 1000)


def _usage_from(raw: dict) -> TokenUsage:
    return TokenUsage(
        input=raw.get("input_tokens", 0),
        output=raw.get("output_tokens", 0),
        reasoning=raw.get("reasoning_tokens", 0),
        cache_read=raw.get("cache_read_input_tokens", 0),
        cache_write=raw.get("cache_creation_input_tokens", 0),
    )


def _is_tool_result(record: dict) -> bool:
    return "toolUseResult" in record


def parse_trajectory(path: Path, model: str) -> ParsedTrajectory:
    result = ParsedTrajectory(model=model)
    by_name: dict[str, int] = {}
    started_at: datetime | None = None
    # Previous record of any type -- an assistant turn's inference begins
    # when the record that unblocked it landed, not at the previous turn.
    prev_ts: datetime | None = None
    turn_no = 0

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            result.malformed_lines += 1
            continue

        timestamp = record.get("timestamp")
        now = _parse_ts(timestamp) if timestamp else None
        if now and started_at is None:
            started_at = now

        if record.get("type") != "assistant":
            # A tool result closes out the tool call the current turn issued.
            if _is_tool_result(record) and result.turns and now:
                last = result.turns[-1]
                result.turns[-1] = replace(
                    last,
                    tool_exec_ms=last.tool_exec_ms + _elapsed_ms(prev_ts, now),
                )
            if now:
                prev_ts = now
            continue

        message = record.get("message", {})
        turn_no += 1

        if not result.claude_code_version:
            result.claude_code_version = record.get("version", "")

        usage = _usage_from(message.get("usage", {}))
        result.total_tokens = result.total_tokens + usage

        inference_ms = _elapsed_ms(prev_ts, now)
        if now:
            prev_ts = now

        for block in message.get("content", []) or []:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            by_name[name] = by_name.get(name, 0) + 1

            if name in EDIT_TOOLS and result.first_edit_turn is None:
                result.first_edit_turn = turn_no
                result.first_edit_offset_ms = _elapsed_ms(started_at, now)
            if name == "Bash":
                command = (block.get("input") or {}).get("command", "")
                if command:
                    result.bash_commands.append((turn_no, command))

        # Pricing is not parsing, and conflating them destroyed data. cost_usd
        # raises for the three candidates on any cache token (their Bedrock
        # cache pricing is unconfirmed) and for a model absent from the price
        # book. Letting that escape aborts the loop, and assemble_record then
        # replaces the whole ParsedTrajectory with an empty one -- so a run
        # whose transcript parsed perfectly is recorded with turns, tokens and
        # tool calls all zero. Observed live on 2026-08-07: a kimi-k2-5 run
        # that produced the correct diff was written as a row of zeroes, a
        # shape indistinguishable from an arm that died on its first call.
        #
        # UnknownModelError subclasses KeyError, not ValueError, so both are
        # named. Both mean the same thing -- the tokens are known and the
        # price is not -- and the tokens are what make the run repriceable
        # offline once rates are published.
        try:
            turn_cost: float | None = cost_usd(model, usage)
        except (ValueError, UnknownModelError) as exc:
            turn_cost = None
            result.pricing_error = result.pricing_error or f"{type(exc).__name__}: {exc}"
        else:
            if result.total_cost_usd is not None:
                result.total_cost_usd += turn_cost
        result.turns.append(
            TurnRecord(
                turn=turn_no,
                tokens=usage,
                cost_usd=turn_cost,
                inference_ms=inference_ms,
                tool_exec_ms=0,
                stop_reason=message.get("stop_reason"),
                bedrock_request_id=record.get("requestId"),
            )
        )

    result.tool_calls = ToolCallStats(total=sum(by_name.values()), by_name=by_name)
    if result.pricing_error:
        # All-or-nothing, deliberately. Summing only the priceable turns would
        # understate the run's cost while looking like a precise figure, which
        # is worse than saying nothing -- section 8's cost metric would be
        # quietly biased low for exactly the arms whose pricing is in doubt.
        result.total_cost_usd = None
    return result
