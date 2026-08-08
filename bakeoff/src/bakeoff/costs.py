"""Token usage to USD. Standard-tier Bedrock pricing (spec section 8).

Prices are US regions (us-east-1, us-east-2, us-west-2), verified against the
AWS Bedrock pricing page on 2026-08-05. Other regions run 15-20% higher; if the
eval ever runs outside the US, this book needs a region axis.

Candidate cache multipliers are None on purpose: spec section 8 records
that Bedrock prompt-cache support for the three candidates is unconfirmed,
and AWS publishes no cache pricing for any of them. Guessing here would
silently corrupt the headline cost metric, so any cache tokens observed on
those models raise instead.

CALLER CONTRACT. Raising is right, but only the price may be lost by it.
`trajectory.parse_trajectory` catches per turn and records the reason on
`ParsedTrajectory.pricing_error`, so the turn count, the tokens and the tool
calls all survive and the run stays repriceable offline the moment AWS
publishes rates. Calling this where an exception aborts a parse is the
2026-08-07 defect: a kimi-k2-5 run that produced the correct diff was written
to the log as turns=0, tokens=0, cost=0.

Bedrock returns no dollar figure on any call -- every cost anywhere in this
harness is tokens times a published rate -- so this table is the only pricing
authority and a gap in it must be reported, never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass

from bakeoff.schema import TokenUsage


class UnknownModelError(KeyError):
    pass


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: float
    output_per_1m: float
    cache_read_multiplier: float | None = None
    cache_write_multiplier: float | None = None


PRICE_BOOK: dict[str, ModelPricing] = {
    "claude-sonnet-5": ModelPricing(
        input_per_1m=3.00,
        output_per_1m=15.00,
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
    ),
    "gemma-4-31b": ModelPricing(input_per_1m=0.14, output_per_1m=0.40),
    "nemotron-3-super-120b": ModelPricing(input_per_1m=0.15, output_per_1m=0.65),
    "kimi-k2-5": ModelPricing(input_per_1m=0.60, output_per_1m=3.00),
}

# The proxy exposes the bedrock-runtime route under a "-runtime" suffix so the
# two transports stay distinguishable in the record (LiteLLM would otherwise
# load-balance across same-named entries). Transport does not change token
# pricing, so these alias the same ModelPricing. Gemma has no runtime route --
# it is served only on bedrock-mantle -- so it gets no alias.
PRICE_BOOK.update(
    {
        f"{name}-runtime": pricing
        for name, pricing in PRICE_BOOK.items()
        if name != "gemma-4-31b"
    }
)

_PER_MILLION = 1_000_000


def cost_usd(model: str, usage: TokenUsage) -> float:
    try:
        price = PRICE_BOOK[model]
    except KeyError as exc:
        raise UnknownModelError(model) from exc

    total = price.input_per_1m * usage.input / _PER_MILLION
    total += price.output_per_1m * (usage.output + usage.reasoning) / _PER_MILLION

    if usage.cache_read:
        if price.cache_read_multiplier is None:
            raise ValueError(f"{model}: cache support unconfirmed, saw cache_read")
        total += (
            price.input_per_1m
            * price.cache_read_multiplier
            * usage.cache_read
            / _PER_MILLION
        )

    if usage.cache_write:
        if price.cache_write_multiplier is None:
            raise ValueError(f"{model}: cache support unconfirmed, saw cache_write")
        total += (
            price.input_per_1m
            * price.cache_write_multiplier
            * usage.cache_write
            / _PER_MILLION
        )

    return total
