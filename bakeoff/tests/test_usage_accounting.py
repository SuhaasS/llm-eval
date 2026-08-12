"""`input` must exclude the cache tokens, on every route into the record.

`costs.cost_usd` charges `input` at the full rate and ADDS cache read and write
on top. That is correct, and it is correct only because the number reaching it
is the non-cached input. Verified against the AWS Bedrock prompt-caching page,
2026-08-11: "When prompt caching is enabled, the `inputTokens` field represents
only the non-cached input tokens... total input tokens = inputTokens +
cacheReadInputTokens + cacheWriteInputTokens."

The candidate arms reach that through a round trip that nearly loses it.
LiteLLM's Converse transform FOLDS the cache tokens into `prompt_tokens` --
OpenAI's usage is inclusive where Anthropic's is not -- and the anthropic
adapter subtracts them back out on the way to Claude Code's transcript. Net
correct, and load-bearing: drop the subtraction and `input` silently counts the
cached prefix twice, which on a warm Sonnet turn is ~30k of a ~34k prompt. No
exception, no zero, just a cost that is wrong by an order of magnitude on the
input line.

Nothing else in the suite would catch that, so it is pinned here against the
installed litellm rather than assumed. Verified against litellm 1.95.0.
"""

from bakeoff.costs import cost_usd
from bakeoff.trajectory import _usage_from

# A warm Sonnet turn 1, from the 2026-08-11 N=3 set: 30,506 read, 11,187
# written, and a handful of genuinely new input tokens.
_READ = 30_506
_WRITE = 11_187
_UNCACHED_INPUT = 4


def test_native_anthropic_usage_reports_input_exclusive_of_cache():
    usage = _usage_from(
        {
            "input_tokens": _UNCACHED_INPUT,
            "output_tokens": 215,
            "cache_read_input_tokens": _READ,
            "cache_creation_input_tokens": _WRITE,
            "cache_creation": {
                "ephemeral_5m_input_tokens": _WRITE,
                "ephemeral_1h_input_tokens": 0,
            },
        }
    )
    assert usage.input == _UNCACHED_INPUT
    assert usage.cache_read == _READ
    assert usage.cache_write == _WRITE
    assert usage.cache_write_5m == _WRITE


def test_the_converse_bridge_round_trip_keeps_input_exclusive_of_cache():
    """Bedrock Converse to openai to anthropic, through litellm's own code.

    Both halves are asserted. The middle assertion is the one that explains the
    test: `prompt_tokens` is deliberately INCLUSIVE there, so the harness would
    double-count if it ever read the openai shape directly -- as
    scripts/smoke_bedrock.py does, on purpose, for a different question.
    """
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (  # noqa: E501
        LiteLLMAnthropicMessagesAdapter,
    )
    from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig

    openai_usage = AmazonConverseConfig()._transform_usage(
        {
            "inputTokens": _UNCACHED_INPUT,
            "outputTokens": 215,
            "totalTokens": _UNCACHED_INPUT + _READ + _WRITE + 215,
            "cacheReadInputTokens": _READ,
            "cacheWriteInputTokens": _WRITE,
        }
    )
    assert openai_usage.prompt_tokens == _UNCACHED_INPUT + _READ + _WRITE

    anthropic_usage = (
        LiteLLMAnthropicMessagesAdapter._translate_openai_usage_to_anthropic_usage(
            openai_usage
        )
    )
    usage = _usage_from(dict(anthropic_usage))
    assert usage.input == _UNCACHED_INPUT, "the subtraction was dropped upstream"
    assert usage.cache_read == _READ
    assert usage.cache_write == _WRITE


def test_the_three_token_classes_are_each_charged_exactly_once():
    """The arithmetic the two tests above exist to protect."""
    usage = _usage_from(
        {
            "input_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        }
    )
    assert cost_usd("claude-sonnet-5", usage) == 2.00 + 0.20 + 2.50
