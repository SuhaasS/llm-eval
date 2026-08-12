import pytest

from bakeoff.costs import PRICE_BOOK, UnknownModelError, cost_usd
from bakeoff.schema import TokenUsage


def test_plain_input_output_cost():
    """Sonnet 5 at Anthropic's list price, $2.00 / $10.00 per 1M -- the price
    the recommendation is against. Not the Bedrock SKU the candidates use."""
    usage = TokenUsage(input=1_000_000, output=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(12.00)


def test_cache_read_billed_at_ten_percent():
    usage = TokenUsage(cache_read=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(0.20)


def test_cache_write_billed_at_multiplier():
    usage = TokenUsage(cache_write=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(2.50)


def test_a_one_hour_cache_write_is_billed_at_double():
    """2.00x against the 5m tier's 1.25x, verified against the AWS Bedrock
    prompt-caching page 2026-08-11. The tier is a PART of cache_write, so the
    total must be charged once -- at the 1h rate, not 1.25x plus 2.00x."""
    usage = TokenUsage(cache_write=1_000_000, cache_write_1h=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(4.00)


def test_a_mixed_tier_write_charges_each_tier_once():
    usage = TokenUsage(
        cache_write=1_000_000, cache_write_5m=400_000, cache_write_1h=600_000
    )
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(
        2.00 * (1.25 * 0.4 + 2.00 * 0.6)
    )


def test_an_untiered_write_keeps_the_five_minute_rate():
    """LiteLLM's Converse bridge drops Bedrock's cacheDetails, so candidate
    writes arrive with no tier at all. Charging only the tiered part would
    bill those at zero; the remainder takes the 5m rate Claude Code asks
    Bedrock for (its TTL is hardcoded to 5m, claude-code#32671)."""
    usage = TokenUsage(cache_write=1_000_000)
    assert usage.cache_write_5m == 0 and usage.cache_write_1h == 0
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(2.50)


def test_tier_tokens_with_a_zero_total_are_not_free():
    """`_usage_from` raises the total to the tier sum, so this shape cannot
    come from a transcript -- but cost_usd is public and is also reached by
    from_dict rehydrating a stored record and by smoke_bedrock's openai-shaped
    usage. Gated on the total alone, a million cached tokens cost $0.00 and
    nothing said why."""
    usage = TokenUsage(cache_write=0, cache_write_1h=1_000_000)
    with pytest.raises(ValueError, match="smaller than its tiers"):
        cost_usd("claude-sonnet-5", usage)


def test_a_tier_larger_than_the_total_raises_rather_than_clamping():
    """More tokens in the tiers than were written is impossible as reported.
    Clamping the remainder still charged the excess 1h tokens in full -- 1000
    written, 1500 at 1h, billed 3x the true ceiling -- behind a number that
    looks fine. Raising costs the price and keeps the tokens."""
    usage = TokenUsage(cache_write=1000, cache_write_5m=0, cache_write_1h=1500)
    with pytest.raises(ValueError, match="smaller than its tiers"):
        cost_usd("claude-sonnet-5", usage)


def test_a_one_hour_write_on_a_model_without_a_one_hour_rate_raises():
    """Sonnet is the only arm with any cache rate at all, and 1h is a separate
    confirmation from 5m -- a model card listing 5m only says nothing about
    what 1h costs. Guessing 1.25x there understates by 60%."""
    for model in ("gemma-4-31b", "nemotron-3-super-120b", "kimi-k2-5"):
        assert PRICE_BOOK[model].cache_write_1h_multiplier is None


def test_reasoning_tokens_billed_as_output():
    usage = TokenUsage(reasoning=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(10.00)


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
