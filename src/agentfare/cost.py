"""Cost engine: ``tokens x price -> micro-cents`` with integer arithmetic."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, localcontext

from .models import Usage
from .money import EXACT_CONTEXT
from .pricing import ModelPrice, PriceTable

# tokens x ($ per 1M tokens) / 1e6 = dollars;  dollars x 100 cents x 1e6 ucents = tokens x price x 100
_UCENTS_PER_TOKEN_PER_USD_M = Decimal(100)


def usage_cost_ucents(usage: Usage, price: ModelPrice) -> int:
    """Exact cost of ``usage`` at ``price`` in micro-cents (rounded half-up once, at the end)."""
    if not price.priced:
        raise ValueError(f"{price.model} has no verified price")
    with localcontext(EXACT_CONTEXT):
        total = (
            Decimal(usage.input) * price.input  # type: ignore[operator]
            + Decimal(usage.cache_read) * price.cached_rate  # type: ignore[operator]
            + Decimal(usage.cache_write) * price.cache_write_rate  # type: ignore[operator]
            + Decimal(usage.cache_write_1h) * price.cache_write_1h_rate  # type: ignore[operator]
            + Decimal(usage.output) * price.output  # type: ignore[operator]
        ) * _UCENTS_PER_TOKEN_PER_USD_M
        return int(total.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass
class SessionCost:
    ucents: int = 0  # priced part only
    by_model: dict[str, int] = field(default_factory=dict)  # canonical model -> ucents
    unpriced: dict[str, Usage] = field(default_factory=dict)  # model as seen in log -> tokens

    @property
    def complete(self) -> bool:
        return not self.unpriced


def price_usage(usage_by_model: dict[str, Usage], table: PriceTable) -> SessionCost:
    """Price a session's per-model usage. Unverified/unknown models land in ``unpriced``."""
    out = SessionCost()
    for model, usage in usage_by_model.items():
        price = table.priced(model)
        if price is None:
            out.unpriced[model] = usage
            continue
        c = usage_cost_ucents(usage, price)
        out.ucents += c
        out.by_model[price.model] = out.by_model.get(price.model, 0) + c
    return out


def reprice(usage_by_model: dict[str, Usage], model: str, table: PriceTable) -> int | None:
    """What the same tokens would cost on ``model`` (all usage re-priced there); None if it has no verified price."""
    price = table.priced(model)
    if price is None:
        return None
    total = Usage()
    for u in usage_by_model.values():
        total = total + u
    return usage_cost_ucents(total, price)
