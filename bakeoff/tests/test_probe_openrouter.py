"""The pure parts of the openrouter probe. The script spends money and is
not in the unit run; what is testable offline is how it reads a response and
how it decides a check."""

import json

import pytest

from scripts.probe_openrouter import (
    assemble_sse,
    cached_tokens,
    check_cache_fires,
    check_provider_pin,
    check_usage_exclusive,
    filler,
    parse_checks,
    unknown_arms,
)


def test_filler_is_deterministic_and_roughly_the_size_asked_for():
    a, b = filler("seed", 4000), filler("seed", 4000)
    assert a == b
    assert 3000 <= len(a.split()) <= 5000


def test_cached_tokens_reads_the_openai_details_shape_and_is_none_when_absent():
    assert cached_tokens({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 96}}) == 96
    assert cached_tokens({"prompt_tokens": 100}) is None


def test_the_provider_pin_check_wants_the_named_upstream_in_every_response():
    assert check_provider_pin("coreweave", [{"provider": "CoreWeave"}, {"provider": "CoreWeave"}])["pass"] is True
    result = check_provider_pin("coreweave", [{"provider": "CoreWeave"}, {"provider": "Fireworks"}])
    assert result["pass"] is False and "Fireworks" in result["seen"]


def test_the_cache_check_is_a_hit_rate_over_replicates_not_one_success():
    pairs = [({"prompt_tokens": 4000, "cost": 1.0}, {"prompt_tokens": 4000, "prompt_tokens_details": {"cached_tokens": 3968}, "cost": 0.5})] * 4
    pairs.append(({"prompt_tokens": 4000, "cost": 1.0}, {"prompt_tokens": 4000, "cost": 1.0}))
    result = check_cache_fires(pairs)
    assert result["hits"] == 4 and result["replicates"] == 5
    assert result["pass"] is True  # >= 3/5 with cost dropping on each hit


def test_usage_exclusivity_decides_the_subtraction():
    # Anthropic-shaped output_tokens 50 and reasoning 30 against OpenAI
    # completion_tokens 50 (inclusive): output already contains reasoning.
    result = check_usage_exclusive(anthropic={"output_tokens": 50, "reasoning_tokens": 30}, openai={"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}})
    assert result["output_is_inclusive_of_reasoning"] is True
    result = check_usage_exclusive(anthropic={"output_tokens": 20, "reasoning_tokens": 30}, openai={"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}})
    assert result["output_is_inclusive_of_reasoning"] is False


def test_parse_checks_accepts_a_comma_separated_subset_of_1_through_6():
    assert parse_checks("1,2,3") == {1, 2, 3}
    assert parse_checks("6") == {6}
    assert parse_checks("1,2,3,4,5,6") == {1, 2, 3, 4, 5, 6}


def test_parse_checks_rejects_non_integers_and_out_of_range_values():
    with pytest.raises(ValueError):
        parse_checks("x")
    with pytest.raises(ValueError):
        parse_checks("7")
    with pytest.raises(ValueError):
        parse_checks("0")
    with pytest.raises(ValueError):
        parse_checks("")


def test_unknown_arms_is_none_when_every_requested_arm_is_configured():
    assert unknown_arms(None, {"kimi-k2-6", "kimi-k3"}) is None
    assert unknown_arms(["kimi-k3"], {"kimi-k2-6", "kimi-k3"}) is None


def test_unknown_arms_names_the_first_unconfigured_arm():
    result = unknown_arms(["kimi-k3", "kimi-k9"], {"kimi-k2-6", "kimi-k3"})
    assert result is not None and "kimi-k9" in result


def _sse(*events: dict) -> list[str]:
    """Renders events as SSE `data:` lines the way litellm's stream would,
    one blank-separated block per event, mirroring the shape `assemble_sse`
    reads (an `event:` line the parser ignores, then the JSON payload)."""
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}")
        lines.append(f"data: {json.dumps(event)}")
        lines.append("")
    return lines


def test_assemble_sse_reassembles_a_canned_streaming_transcript():
    """A canned SSE transcript with a text block, a tool_use block built
    from input_json_delta chunks, and a message_delta usage/stop_reason --
    the shape a real streamed turn-1 response takes when the model replies
    and calls the Bash tool in the same turn."""
    lines = _sse(
        {"type": "message_start", "message": {"id": "msg_1", "role": "assistant", "usage": {"input_tokens": 100, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Sure, "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "running it."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"command"'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "ls"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    )

    body = assemble_sse(lines)

    assert body["usage"]["output_tokens"] == 12
    assert body["stop_reason"] == "tool_use"
    assert [b["type"] for b in body["content"]] == ["text", "tool_use"]
    assert body["content"][0]["text"] == "Sure, running it."
    tool_use = body["content"][1]
    assert tool_use["id"] == "toolu_1" and tool_use["name"] == "Bash"
    assert tool_use["input"] == {"command": "ls"}


def test_assemble_sse_ignores_blank_lines_and_done_sentinel():
    lines = ["", "data: [DONE]", "event: content_block_stop", "data: {\"type\": \"content_block_stop\", \"index\": 0}"]
    assert assemble_sse(lines) == {"content": [], "usage": {}}
