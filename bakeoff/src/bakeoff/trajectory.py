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
    # Raw `type: "assistant"` records. `len(turns)` counts API calls, and the
    # difference between them is how many content blocks the calls were split
    # across -- the quantity that silently doubled every token total until
    # 3.0.0. Kept so the collapse is visible without the transcript.
    assistant_records: int = 0
    # Why the cost is unknown. Non-empty exactly when total_cost_usd is None.
    pricing_error: str = ""


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return int((end - start).total_seconds() * 1000)


def _usage_from(raw: dict) -> TokenUsage:
    """Project `message.usage`. Absent fields are 0 -- see the caller's guard.

    Zero here is ambiguous by construction: a provider that reports no cache
    accounting at all and one that reports a genuine 0 project identically.
    `runner._cache_warm` is what resolves it, using the whole run rather than
    one field, so nothing downstream may read a 0 in this struct as an
    observation on its own.

    The ephemeral split (spec section 3.1) is nested under `cache_creation`
    when the provider sends it; the flat `cache_creation_input_tokens` is the
    total either way. Only Sonnet's native-Anthropic route carries the nested
    object -- LiteLLM's Converse bridge emits the flat field alone -- so the
    tiers routinely sum to less than the total, and that gap is an untiered
    write, not a 5m one.

    THIS FUNCTION MUST NOT RAISE. It runs outside `parse_trajectory`'s pricing
    guard, so anything escaping it aborts the parse and `assemble_record`
    discards the whole trajectory -- turns, tokens, tool calls and destructive
    events all zeroed for a run that worked. `usage` comes from a proxy
    translation layer this repo documents as lossy and shape-shifting, so every
    field is coerced rather than trusted: a `cache_creation` that arrives as a
    list, or a tier that arrives as `null`, would otherwise cost the record.
    The `null` case is the quieter one -- it stores `None` here and raises a
    TypeError in `TokenUsage.__add__` on the NEXT turn, one call away from
    anything that could name the cause.
    """
    tiers = raw.get("cache_creation")
    if not isinstance(tiers, dict):
        tiers = {}
    write_5m = _int(tiers.get("ephemeral_5m_input_tokens"))
    write_1h = _int(tiers.get("ephemeral_1h_input_tokens"))
    return TokenUsage(
        input=_int(raw.get("input_tokens")),
        output=_int(raw.get("output_tokens")),
        reasoning=_int(raw.get("reasoning_tokens")),
        cache_read=_int(raw.get("cache_read_input_tokens")),
        # The nested sum is the fallback, not the source: a response carrying
        # only the tiers must not report a zero total and price as free. `max`
        # rather than `or`, so the invariant `cache_write >= 5m + 1h` holds for
        # anything this function returns -- `cost_usd` charges the total once
        # and reads the 1h tier out of it, and a total smaller than its own
        # tiers makes that arithmetic bill more tokens than were written.
        cache_write=max(
            _int(raw.get("cache_creation_input_tokens")), write_5m + write_1h
        ),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
    )


def _int(value: object) -> int:
    """A token count, or 0 for anything that is not one.

    `null` is the case that matters: JSON nulls reach here as `None`, store as
    `None`, and raise a TypeError inside `TokenUsage.__add__` on a later turn --
    far from the field that caused it, and costing the whole record.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _is_tool_result(record: dict) -> bool:
    return "toolUseResult" in record


def parse_trajectory(path: Path, model: str) -> ParsedTrajectory:
    result = ParsedTrajectory(model=model)
    by_name: dict[str, int] = {}
    # message.id -> index into result.turns. See the dedupe below.
    by_message_id: dict[str, int] = {}
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
        result.assistant_records += 1

        if not result.claude_code_version:
            result.claude_code_version = record.get("version", "")

        # ONE API CALL IS ONE TURN, and Claude Code does not write it that way.
        # It emits one transcript record per CONTENT BLOCK -- a reply with text
        # and two tool calls is three records -- and every one of them repeats
        # the whole `usage`. Counting records therefore inflated both the turn
        # count and the token total: measured on stored records 2026-08-11, a
        # five-call Sonnet run recorded 6 turns and `cache_write` 83,867 against
        # a true 42,171, doubling its cost.
        #
        # Keyed on `message.id` globally rather than on the previous record, so
        # a tool_result landing between two blocks of the same reply cannot
        # split it. A record with NO id gets a key nothing else can match, so
        # it stays its own turn: merging on absence would collapse genuinely
        # distinct calls, the same error in the expensive direction.
        message_id = message.get("id")
        key = message_id or f"\0record-{result.assistant_records}"
        seen = by_message_id.get(key)

        if seen is None:
            turn_no += 1
        usage = _usage_from(message.get("usage", {}))

        if seen is None:
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
        if seen is not None:
            # A later block of a call already counted. Its tool calls are on
            # the turn above; its usage is a repeat, and its `stop_reason` is
            # the real one -- the first block of a reply carries None while the
            # last carries `tool_use` or `end_turn`, which is what
            # assemble_record reads to decide the outcome.
            existing = result.turns[seen]
            if message.get("stop_reason") is not None:
                result.turns[seen] = replace(
                    existing, stop_reason=message.get("stop_reason")
                )
            continue

        try:
            turn_cost: float | None = cost_usd(model, usage)
        except (ValueError, UnknownModelError) as exc:
            turn_cost = None
            result.pricing_error = result.pricing_error or f"{type(exc).__name__}: {exc}"
        else:
            if result.total_cost_usd is not None:
                result.total_cost_usd += turn_cost
        by_message_id[key] = len(result.turns)
        result.turns.append(
            TurnRecord(
                turn=turn_no,
                tokens=usage,
                cost_usd=turn_cost,
                inference_ms=inference_ms,
                tool_exec_ms=0,
                stop_reason=message.get("stop_reason"),
                bedrock_request_id=record.get("requestId"),
                api_message_id=message_id,
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
