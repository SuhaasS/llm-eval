import json
from pathlib import Path

import pytest

from bakeoff.trajectory import parse_trajectory

FIXTURE = Path(__file__).parent / "fixtures" / "trajectory_sample.jsonl"


@pytest.fixture
def parsed():
    return parse_trajectory(FIXTURE, model="claude-sonnet-5")


def test_one_turn_per_assistant_record(parsed):
    assert len(parsed.turns) == 4
    assert [t.turn for t in parsed.turns] == [1, 2, 3, 4]


# --- one API call is one turn ------------------------------------------------


def _write(tmp_path, records) -> Path:
    path = tmp_path / "split.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _assistant(message_id, blocks, *, usage=None, stop_reason=None, minute=0):
    message = {"model": "claude-sonnet-5", "content": blocks}
    if message_id is not None:
        message["id"] = message_id
    if usage is not None:
        message["usage"] = usage
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {
        "type": "assistant",
        "timestamp": f"2026-08-11T00:0{minute}:00.000Z",
        "version": "2.1.220",
        "message": message,
    }


def test_one_api_call_is_one_turn_even_when_split_across_content_blocks(tmp_path):
    """Claude Code writes one transcript record per CONTENT BLOCK, and every
    one repeats the whole `usage`. Measured on a real Kimi transcript: 10
    assistant records, 5 distinct `message.id`s, shaped ['text'], ['tool_use'],
    ['tool_use'] for a single reply.

    Counting records instead of calls doubled every token total in the log --
    stored Sonnet run 7be2933b recorded cache_write 83,867 against a true
    42,171, and $0.3726 against $0.2148.
    """
    usage = {"input_tokens": 2, "output_tokens": 93, "cache_creation_input_tokens": 41695}
    path = _write(
        tmp_path,
        [
            _assistant("msg_a", [{"type": "text", "text": "hi"}], usage=usage),
            _assistant(
                "msg_a",
                [{"type": "tool_use", "name": "Read", "input": {}}],
                usage=usage,
            ),
            _assistant(
                "msg_a",
                [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                usage=usage,
                stop_reason="tool_use",
            ),
        ],
    )
    parsed = parse_trajectory(path, model="claude-sonnet-5")

    assert len(parsed.turns) == 1, "three records, one API call"
    assert parsed.assistant_records == 3, "the collapse stays visible"
    assert parsed.total_tokens.cache_write == 41695, "counted once, not three times"
    assert parsed.total_tokens.output == 93
    assert parsed.tool_calls.total == 2, "both tool calls belong to that turn"
    assert parsed.bash_commands == [(1, "ls")]
    assert parsed.turns[0].api_message_id == "msg_a"
    assert parsed.turns[0].stop_reason == "tool_use", "the last block carries it"


def test_records_without_a_message_id_are_not_merged(tmp_path):
    """Merging on absence is the same error in the expensive direction: it
    would collapse genuinely distinct calls and understate a run's cost."""
    usage = {"input_tokens": 10, "output_tokens": 5}
    path = _write(
        tmp_path,
        [
            _assistant(None, [{"type": "text", "text": "a"}], usage=usage, minute=0),
            _assistant(None, [{"type": "text", "text": "b"}], usage=usage, minute=1),
        ],
    )
    parsed = parse_trajectory(path, model="claude-sonnet-5")

    assert len(parsed.turns) == 2
    assert parsed.total_tokens.output == 10
    assert [t.api_message_id for t in parsed.turns] == [None, None]


def test_a_malformed_usage_block_costs_the_field_not_the_record(tmp_path):
    """`_usage_from` runs outside the pricing guard, so anything it raises
    aborts the parse and assemble_record discards the whole trajectory -- the
    2026-08-07 row-of-zeroes defect. `usage` comes from a proxy translation
    layer this repo documents as shape-shifting, so a `cache_creation` arriving
    as a list, or a tier arriving as null, must cost that field and nothing
    else. The null is the quieter one: it stores None and raises a TypeError
    inside TokenUsage.__add__ on the NEXT turn.
    """
    path = _write(
        tmp_path,
        [
            _assistant(
                "msg_a",
                [],
                usage={"input_tokens": 10, "output_tokens": 5, "cache_creation": [1, 2]},
                minute=0,
            ),
            _assistant(
                "msg_b",
                [],
                usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 700,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": None,
                        "ephemeral_1h_input_tokens": 200,
                    },
                },
                minute=1,
            ),
        ],
    )
    parsed = parse_trajectory(path, model="claude-sonnet-5")

    assert len(parsed.turns) == 2, "neither record cost the parse"
    assert parsed.total_tokens.input == 20
    assert parsed.total_tokens.cache_write == 700
    assert parsed.total_tokens.cache_write_5m == 0, "unreadable, not invented"
    assert parsed.total_tokens.cache_write_1h == 200


def test_a_total_smaller_than_its_own_tiers_is_raised_to_them(tmp_path):
    """cost_usd charges cache_write once and reads the 1h tier out of it, so a
    total below the tier sum would bill more tokens than were written."""
    path = _write(
        tmp_path,
        [
            _assistant(
                "msg_a",
                [],
                usage={
                    "cache_creation_input_tokens": 100,
                    "cache_creation": {"ephemeral_1h_input_tokens": 500},
                },
            )
        ],
    )
    parsed = parse_trajectory(path, model="claude-sonnet-5")

    assert parsed.total_tokens.cache_write == 500
    assert parsed.total_tokens.cache_write_1h == 500


def test_a_reply_split_around_a_tool_result_is_still_one_turn(tmp_path):
    """Keyed on message.id globally, not on the previous record, so anything
    interleaved between two blocks of one reply cannot split it."""
    usage = {"input_tokens": 4, "output_tokens": 20}
    path = _write(
        tmp_path,
        [
            _assistant("msg_a", [{"type": "text", "text": "x"}], usage=usage, minute=0),
            {
                "type": "user",
                "timestamp": "2026-08-11T00:01:00.000Z",
                "toolUseResult": {"stdout": ""},
                "message": {"content": []},
            },
            _assistant(
                "msg_a",
                [{"type": "tool_use", "name": "Read", "input": {}}],
                usage=usage,
                minute=2,
            ),
        ],
    )
    parsed = parse_trajectory(path, model="claude-sonnet-5")

    assert len(parsed.turns) == 1
    assert parsed.total_tokens.output == 20


def test_tokens_summed_across_turns(parsed):
    assert parsed.total_tokens.input == 25
    assert parsed.total_tokens.output == 75
    assert parsed.total_tokens.cache_read == 1000
    assert parsed.total_tokens.cache_write == 50


def test_per_turn_tokens_reconstruct_the_total(parsed):
    """Spec section 6.6 fault-injection gate: per-turn records must sum
    exactly to run-level totals, or the log is silently lossy."""
    summed_in = sum(t.tokens.input for t in parsed.turns)
    summed_out = sum(t.tokens.output for t in parsed.turns)
    assert summed_in == parsed.total_tokens.input
    assert summed_out == parsed.total_tokens.output


def test_per_turn_cost_is_populated(parsed):
    """Every turn is priced here because claude-sonnet-5 has confirmed cache
    pricing. The sum is asserted strictly, not defensively: if a turn ever
    comes back None on this fixture, the reconstruction guarantee has broken
    and the test should say so loudly rather than skip the None."""
    assert all(t.cost_usd > 0 for t in parsed.turns)
    assert sum(t.cost_usd for t in parsed.turns) == pytest.approx(
        parsed.total_cost_usd
    )


# --- pricing is not parsing --------------------------------------------------
#
# The same fixture, read as a model whose cache pricing is unconfirmed. It
# carries cache_read=1000, so cost_usd raises on every turn. That used to
# abort the parse and cost the whole trajectory.


@pytest.fixture
def unpriced():
    return parse_trajectory(FIXTURE, model="kimi-k2-5")


def test_an_unpriceable_model_keeps_every_turn(unpriced):
    """The transcript parsed. Only the price is missing.

    Before this split, cost_usd raising took parse_trajectory down with it and
    assemble_record replaced the result with an empty ParsedTrajectory -- so a
    run that had worked was recorded with turns, tokens and tool calls all
    zero. Measured live 2026-08-07 on a kimi-k2-5 run that produced the
    correct diff.
    """
    assert len(unpriced.turns) == 4
    assert [t.turn for t in unpriced.turns] == [1, 2, 3, 4]


def test_an_unpriceable_model_keeps_its_tokens_so_the_run_can_be_repriced(unpriced):
    """The tokens are the whole reason an unknown price is survivable: AWS may
    publish cache rates later, and the run is recoverable only if its usage
    was recorded at the time."""
    assert unpriced.total_tokens.input == 25
    assert unpriced.total_tokens.output == 75
    assert unpriced.total_tokens.cache_read == 1000
    assert unpriced.tool_calls.total == 3
    assert unpriced.bash_commands


def test_an_unpriceable_model_reports_cost_unknown_not_zero(unpriced):
    """None, never 0.0. Zero is a positive claim that the run was free, and
    it is indistinguishable from an arm that died before spending anything --
    a shape this very run produced three times for Gemma."""
    assert unpriced.total_cost_usd is None
    assert all(t.cost_usd is None for t in unpriced.turns)


def test_the_reason_names_the_model_and_the_cause(unpriced):
    assert "kimi-k2-5" in unpriced.pricing_error
    assert "cache support unconfirmed" in unpriced.pricing_error


@pytest.mark.parametrize("model", ["claude-sonnet-5", "kimi-k2-5", "not-in-price-book"])
def test_cost_is_none_exactly_when_a_pricing_error_is_recorded(model):
    """The invariant. A None cost with no stated reason is the silent absence
    the event log exists to prevent, and a reason with a number beside it
    would make the number a lie."""
    result = parse_trajectory(FIXTURE, model=model)
    assert (result.total_cost_usd is None) == bool(result.pricing_error)


def test_tool_calls_counted_by_name(parsed):
    assert parsed.tool_calls.total == 3
    assert parsed.tool_calls.by_name == {"Read": 1, "Edit": 1, "Bash": 1}


def test_first_edit_turn_identified(parsed):
    assert parsed.first_edit_turn == 2


def test_bash_commands_extracted_with_turn_numbers(parsed):
    assert parsed.bash_commands == [(3, "pytest -q")]


def test_model_and_version_captured(parsed):
    assert parsed.model == "claude-sonnet-5"
    assert parsed.claude_code_version == "2.1.209"


def test_stop_reason_recorded_per_turn(parsed):
    assert parsed.turns[-1].stop_reason == "end_turn"
    assert parsed.turns[0].stop_reason == "tool_use"


def test_truncated_file_parses_what_exists(tmp_path):
    """A killed run leaves a half-written final line. That must yield a
    partial-but-valid parse, never an exception."""
    lines = FIXTURE.read_text().splitlines()
    broken = tmp_path / "broken.jsonl"
    broken.write_text("\n".join(lines[:4]) + '\n{"type":"assis')

    result = parse_trajectory(broken, model="claude-sonnet-5")
    assert len(result.turns) == 2
    assert result.malformed_lines == 1


def test_inference_excludes_tool_execution_time(parsed):
    """Spec section 6.1 defines inference_ms as model generation time and
    tool_exec_ms as test runs and builds. Measuring inference as the gap
    between consecutive assistant records folds tool execution into it and
    leaves tool_exec_ms permanently zero, which silently corrupts the
    latency success criterion (section 10)."""
    assert [t.inference_ms for t in parsed.turns] == [5000, 6000, 7000, 5000]
    assert [t.tool_exec_ms for t in parsed.turns] == [1000, 1000, 5000, 0]


def test_inference_plus_tool_exec_accounts_for_the_whole_session(parsed):
    """The two must partition the transcript span. If they overlap, one is
    double-counting; if they undershoot, time is unaccounted for."""
    span_ms = 30_000  # first record 00:00:00, last 00:00:30
    inference = sum(t.inference_ms for t in parsed.turns)
    tool_exec = sum(t.tool_exec_ms for t in parsed.turns)
    assert inference + tool_exec == span_ms
