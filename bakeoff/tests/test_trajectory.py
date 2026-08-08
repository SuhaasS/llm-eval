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
