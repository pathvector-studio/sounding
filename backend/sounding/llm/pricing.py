"""Built-in price table (USD per 1M tokens) and the cost estimate (R8).

Only models we know the prices of are listed. An unknown model is estimated with the most
expensive prices of the same provider in the table (or of the whole table if the provider has
none), and a warning is logged once per model. OpenAI has no entries on purpose: model names
change fast, so its calls are always estimated at the highest known prices until someone adds one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sounding.llm.base import Usage

log = logging.getLogger(__name__)

_M = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    provider: str
    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


def _p(provider: str, i: str, o: str, cw: str, cr: str) -> Price:
    return Price(provider, Decimal(i), Decimal(o), Decimal(cw), Decimal(cr))


PRICES: dict[str, Price] = {
    "claude-opus-5-5": _p("anthropic", "4.00", "20.00", "5.00", "0.20"),
    "claude-sonnet-5-5": _p("anthropic", "2.00", "10.00", "2.50", "0.20"),
    "claude-haiku-5-5": _p("anthropic", "0.10", "0.50", "0.125", "0.01"),
}

_warned: set[tuple[str, str]] = set()


def price_for(provider: str, model: str) -> Price:
    known = PRICES.get(model)
    if known is not None:
        return known
    pool = [p for p in PRICES.values() if p.provider == provider] or list(PRICES.values())
    if (provider, model) not in _warned:
        _warned.add((provider, model))
        log.warning("no price for model %r (%s); estimating with the highest known prices", model, provider)
    return Price(
        provider,
        max(p.input for p in pool),
        max(p.output for p in pool),
        max(p.cache_write for p in pool),
        max(p.cache_read for p in pool),
    )


def estimate_cost(provider: str, model: str, usage: Usage) -> Decimal:
    price = price_for(provider, model)
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_write_tokens * price.cache_write
        + usage.cache_read_tokens * price.cache_read
    ) / _M
    return total.quantize(Decimal("0.000001"))
