"""Token usage to USD (spec section 8).

TWO BASES, ON PURPOSE, AND THE RECORD SAYS WHICH. Sonnet 5 is priced at
Anthropic's official list -- $2.00 / $10.00 per 1M -- because that is the price
the recommendation is against; the eval exists to ask whether a candidate beats
buying Sonnet, not whether one AWS SKU beats another. The three candidates have
no list price and are priced from the AWS Bedrock pricing page, US regions
(us-east-1, us-east-2, us-west-2), verified 2026-08-05. Other Bedrock regions
run 15-20% higher, so the candidate half of this book needs a region axis if the
eval ever runs outside the US; the Sonnet half has no region.

`PRICING_BASIS` is stamped onto every record. The log is append-only, so records
written before 2026-08-11 carry Sonnet at the Bedrock $3.00 / $15.00 and keep
those figures forever. Their tokens are stored, so they reprice offline -- but
only a reader that can tell which book made a number knows it has to.

CACHE MULTIPLIERS. 0.10x read, 1.25x write at the 5m TTL, 2.00x write at 1h --
the same ratios on Anthropic's list and on Bedrock, verified against the AWS
Bedrock prompt-caching page on 2026-08-11. The tier matters: a 1h write costs
60% more than a 5m one, and `TokenUsage` splits them because the total alone
cannot be repriced. An untiered write (the provider reported no TTL) is charged
at the 5m rate, which is what Claude Code asks Bedrock for today -- its TTL is
hardcoded to 5m, claude-code#32671 -- and is recorded as untiered rather than
rewritten to 5m so the assumption stays visible in the log.

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


# Bumped whenever a rate in PRICE_BOOK moves. Stamped into every record's
# Versions so a stored cost says which book produced it.
PRICING_BASIS = "sonnet-list-2026-08-11+bedrock-2026-08-05+openrouter-2026-09-08+k26-crusoe-2026-09-11"


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: float
    output_per_1m: float
    cache_read_multiplier: float | None = None
    # The 5m TTL rate, and the rate for a write whose TTL was not reported.
    cache_write_multiplier: float | None = None
    cache_write_1h_multiplier: float | None = None


PRICE_BOOK: dict[str, ModelPricing] = {
    "claude-sonnet-5": ModelPricing(
        input_per_1m=2.00,
        output_per_1m=10.00,
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
        cache_write_1h_multiplier=2.00,
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
#
# The aliases deliberately outrun the config. kimi-k2-5-runtime is currently
# commented out of litellm_config.yaml (its ids cannot survive Converse; see
# that file), and its price stays here anyway: a price for a route nobody
# serves costs nothing, while a missing one turns the day the deployment comes
# back into an UnknownModelError raised mid-parse, after the tokens are spent,
# on a record that then reports turns, tokens and cost all zero.
PRICE_BOOK.update(
    {
        f"{name}-runtime": pricing
        for name, pricing in PRICE_BOOK.items()
        if name != "gemma-4-31b"
    }
)

# The openrouter arms (spec 2026-09-08 §5). Rates are the ENDPOINT's, not the
# model's: OpenRouter serves one model from many upstreams at different prices,
# and the config pins each arm to one of them. Measured 2026-09-08 from
# GET /api/v1/models/moonshotai/<id>/endpoints.
#
# cache_read is a real discount here, unlike the bedrock candidates: both
# endpoints publish an input_cache_read rate. cache_write is 1.0 because no
# endpoint lists input_cache_write. A written prefix therefore bills at the
# plain input rate, so 1.0 is the PRICE and not a placeholder standing in for
# a rate nobody looked up. There is no 1h tier on this route; 1.0 keeps a run
# with no cache read at exactly prompt_tokens x input_per_1m.
#
# Added AFTER the -runtime aliasing above on purpose: an openrouter arm has no
# second transport, and an alias would price a deployment that cannot exist.
PRICE_BOOK.update(
    {
        # crusoe/bf16: $0.70 in, $3.50 out, $0.35 cache read per 1M. Re-pinned
        # from coreweave/fp4 ($0.65 / $3.41 / $0.15) on 2026-09-11 because
        # CoreWeave's cache is a per-request lottery (3/10 then 0/15 probe
        # hits) and Crusoe's fired 5/5; see the config header. The two live
        # records written before the re-pin carry the earlier PRICING_BASIS
        # and CoreWeave's rates, which is what the basis segment below is for.
        "kimi-k2-6": ModelPricing(
            input_per_1m=0.70, output_per_1m=3.50,
            cache_read_multiplier=0.35 / 0.70, cache_write_multiplier=1.0,
            cache_write_1h_multiplier=1.0,
        ),
        # fireworks: $3.00 in, $15.00 out, $0.30 cache read per 1M. If
        # probe_openrouter.py check 1 selects fireworks/us, this row becomes
        # 3.30 / 16.50 / 0.10 BEFORE the first paid run, never after.
        "kimi-k3": ModelPricing(
            input_per_1m=3.00, output_per_1m=15.00,
            cache_read_multiplier=0.10, cache_write_multiplier=1.0,
            cache_write_1h_multiplier=1.0,
        ),
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

    # Gated on the tiers too, not just the total. A TokenUsage carrying tier
    # tokens with a zero total is unrepresentable from `_usage_from`, which
    # raises the total to the tier sum -- but `cost_usd` is public and is also
    # reached by `RunRecord.from_dict` rehydrating a stored record for offline
    # repricing, and by scripts/smoke_bedrock.py building usage from the openai
    # shape. On `if usage.cache_write:` alone those tokens were charged nothing.
    if usage.cache_write or usage.cache_write_1h or usage.cache_write_5m:
        if price.cache_write_multiplier is None:
            raise ValueError(f"{model}: cache support unconfirmed, saw cache_write")
        # The tiers are PARTS of cache_write, never additions to it, so the
        # untiered remainder takes the 5m rate and the total is charged exactly
        # once. Splitting the other way -- 5m tokens plus 1h tokens -- would
        # bill nothing at all for a write whose TTL the provider did not report,
        # which is every write on the candidate arms.
        at_1h = usage.cache_write_1h
        if usage.cache_write_5m + at_1h > usage.cache_write:
            # Impossible as reported: more tokens in the tiers than were
            # written. Clamping the remainder to zero would still charge the
            # excess 1h tokens in full -- (1000 total, 1500 at 1h) billed 3x
            # the true ceiling -- and would hide the contradiction behind a
            # plausible number. This module reports gaps; it never estimates
            # them, and raising here costs the price and keeps the tokens.
            raise ValueError(
                f"{model}: cache_write {usage.cache_write} is smaller than its "
                f"tiers ({usage.cache_write_5m} at 5m + {at_1h} at 1h)"
            )
        if at_1h:
            if price.cache_write_1h_multiplier is None:
                raise ValueError(
                    f"{model}: 1h cache write rate unconfirmed, saw cache_write_1h"
                )
            total += (
                price.input_per_1m
                * price.cache_write_1h_multiplier
                * at_1h
                / _PER_MILLION
            )
        total += (
            price.input_per_1m
            * price.cache_write_multiplier
            * (usage.cache_write - at_1h)
            / _PER_MILLION
        )

    return total
