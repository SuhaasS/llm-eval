import pytest

from bakeoff.costs import PRICE_BOOK, UnknownModelError, cost_usd
from bakeoff.schema import TokenUsage


def test_plain_input_output_cost():
    usage = TokenUsage(input=1_000_000, output=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(18.00)


def test_cache_read_billed_at_ten_percent():
    usage = TokenUsage(cache_read=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(0.30)


def test_cache_write_billed_at_multiplier():
    usage = TokenUsage(cache_write=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(3.75)


def test_reasoning_tokens_billed_as_output():
    usage = TokenUsage(reasoning=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(15.00)


def test_candidate_pricing_matches_spec():
    usage = TokenUsage(input=1_000_000, output=1_000_000)
    assert cost_usd("gemma-4-31b", usage) == pytest.approx(0.54)
    assert cost_usd("nemotron-3-super-120b", usage) == pytest.approx(0.80)
    assert cost_usd("kimi-k2-5", usage) == pytest.approx(3.60)


def test_candidates_have_cache_support_unconfirmed():
    """Spec section 8: candidate Bedrock cache support is unconfirmed, so
    their cache multipliers must be explicitly None until measured."""
    for model in ("gemma-4-31b", "nemotron-3-super-120b", "kimi-k2-5"):
        assert PRICE_BOOK[model].cache_read_multiplier is None


def test_cache_tokens_on_model_without_cache_support_raises():
    with pytest.raises(ValueError, match="cache support unconfirmed"):
        cost_usd("gemma-4-31b", TokenUsage(cache_read=100))


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        cost_usd("not-a-model", TokenUsage(input=1))


def test_zero_usage_is_zero_cost():
    assert cost_usd("claude-sonnet-5", TokenUsage()) == 0.0
