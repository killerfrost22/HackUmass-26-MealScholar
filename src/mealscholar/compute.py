"""Step 5: nutrient arithmetic. Plain code, no model: grams x per-100 g values."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from .nutrients import NUTRIENTS
from .schemas import FoodItem, ItemResult, MealResult, Range


def item_nutrients(per_100g: Mapping[str, float], item: FoodItem) -> dict[str, Range]:
    return {
        key: Range(
            low=value * item.grams_low / 100,
            likely=value * item.grams_likely / 100,
            high=value * item.grams_high / 100,
        )
        for key, value in per_100g.items()
    }


SHARED_ERROR = 0.3
"""Correlation assumed between the errors of two estimates that are added up: two foods on one
plate, or two meals in one day. Part of the error is shared, such as a misjudged plate size or a
model that serves everything too big. At 0 the errors would be independent. At 1 every estimate
is off in the same direction at once, which adds up all the lows and all the highs. On the
benchmark's photos with several foods, 0.3 keeps as many reference amounts inside the range as
1 does for the production model, and 0 doesn't (BENCHMARK.md, "Range width"). Revisit once
meals are weighed."""


def add_ranges(ranges: Iterable[Mapping[str, Range]], correlation: float = SHARED_ERROR) -> dict[str, Range]:
    """Likely values add up. The distances from likely down to low and up to high add up as
    partly correlated errors, sqrt((1 - c)·Σw² + c·(Σw)²): that's Σw at c = 1 and sqrt(Σw²) at c = 0."""
    sums: dict[str, list[float]] = {}
    for nutrients in ranges:
        for key, r in nutrients.items():
            below, above = r.likely - r.low, r.high - r.likely
            s = sums.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
            s[0] += r.likely
            s[1] += below
            s[2] += below * below
            s[3] += above
            s[4] += above * above

    def spread(total: float, squares: float) -> float:
        return math.sqrt((1 - correlation) * squares + correlation * total * total)

    return {
        key: Range(low=likely - spread(b, b2), likely=likely, high=likely + spread(a, a2))
        for key, (likely, b, b2, a, a2) in sums.items()
    }


def daily_value_pct(totals: Mapping[str, Range]) -> dict[str, Range]:
    pct: dict[str, Range] = {}
    for n in NUTRIENTS:
        if n.daily_value is not None and (r := totals.get(n.key)) is not None:
            pct[n.key] = Range(
                low=100 * r.low / n.daily_value,
                likely=100 * r.likely / n.daily_value,
                high=100 * r.high / n.daily_value,
            )
    return pct


def summarize(result: MealResult) -> MealResult:
    """Recompute totals, % Daily Value, and which totals are missing data from some food."""
    matched: list[ItemResult] = [r for r in result.items if r.match is not None]
    totals = add_ranges(r.nutrients for r in matched)
    partial = sorted(
        {n.key for n in NUTRIENTS for r in matched if n.key not in r.nutrients},
        key=[n.key for n in NUTRIENTS].index,
    )
    return result.model_copy(
        update={
            "totals": totals,
            "daily_value_pct": daily_value_pct(totals),
            "partial_nutrients": partial,
        }
    )
