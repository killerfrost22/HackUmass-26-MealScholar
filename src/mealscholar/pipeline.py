"""Steps 1-7 wired together: photo -> metadata -> context -> Gemini -> USDA -> math -> result."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from .compute import item_nutrients, summarize
from .context import PlaceLookup, build_context
from .metadata import extract_metadata, load_image
from .nutrition import Food, FoodDatabase
from .schemas import CookingMethod, FoodItem, FoodMatch, ItemResult, MealAnalysis, MealContext, MealResult
from .vision import VisionModel, context_prompt, prepare_image

# Extra ranking words for the USDA lookup, from what the photo shows.
_COOKING_HINTS: dict[CookingMethod, tuple[str, ...]] = {
    "raw": ("raw",),
    "boiled": ("cooked",),
    "steamed": ("cooked",),
    "grilled": ("cooked",),
    "roasted": ("cooked",),
    "baked": ("cooked",),
    "pan_fried": ("fried", "cooked"),
    "stir_fried": ("fried", "cooked"),
    "deep_fried": ("fried",),
    "unknown": (),
}


class UnknownFood(LookupError):
    pass


@dataclass
class Workflow:
    vision: VisionModel
    foods: FoodDatabase
    places: PlaceLookup | None = None

    def analyze(
        self,
        photo: Path | BinaryIO,
        *,
        device_time: datetime | None = None,
        device_location: tuple[float, float] | None = None,
        read_photo_gps: bool = False,
        usual_portions: Mapping[str, int] | None = None,
    ) -> MealResult:
        img = load_image(photo)
        meta = extract_metadata(
            img,
            device_time=device_time,
            device_location=device_location,
            read_photo_gps=read_photo_gps,
        )
        context = build_context(meta, self.places)
        prompt = context_prompt(context, usual_portions or {})
        analysis = self.vision.analyze(prepare_image(img), prompt)
        return self._result(context, analysis)

    def answer(self, result: MealResult, answer: str) -> MealResult:
        """Apply the user's answer to the clarifying question and recompute everything."""
        if not result.question:
            raise ValueError("This meal has no open question.")
        analysis = self.vision.refine(result.analysis(), result.question, answer)
        refined = self._result(result.context, analysis.model_copy(update={"question": None}))
        return refined.model_copy(update={"id": result.id, "answer": answer, "photo_url": result.photo_url})

    def _result(self, context: MealContext, analysis: MealAnalysis) -> MealResult:
        notes: list[str] = []
        items: list[ItemResult] = []
        for item in analysis.items if analysis.is_food else ():
            hints = _COOKING_HINTS[item.cooking_method] + (("oil",) if item.hidden_fat_likely else ())
            found = self.foods.search(item.fdc_query, hints=hints)
            if not found.exact:
                by_name = self.foods.search(item.name, hints=hints)
                if by_name.exact or not found.foods:
                    found = by_name
            if found.exact:
                items.append(_item_result(item, found.foods[0], [_match(f) for f in found.foods[1:]]))
                continue
            # A loose match ("Ramen bowl" for an acai bowl) would put wrong numbers in the totals;
            # offer it as a suggestion instead.
            items.append(ItemResult(item=item, match=None, alternatives=[_match(f) for f in found.foods]))
            notes.append(f"No close USDA match for '{item.name}'; not counted until you pick one.")
        if not analysis.is_food:
            notes.append("No food or drink detected in the photo.")
        return summarize(
            MealResult(
                context=context,
                items=items,
                name=meal_name([r.item for r in items]),
                scale_reference=analysis.scale_reference,
                # Nothing to ask about a photo that isn't a meal, and answering would re-run Gemini.
                question=analysis.question if analysis.is_food else None,
                notes=notes,
            )
        )


def correct(
    result: MealResult,
    item_index: int,
    foods: FoodDatabase,
    *,
    grams: int | None = None,
    fdc_id: int | None = None,
) -> MealResult:
    """User correction: exact grams collapse the amount (the user typed it); fdc_id swaps the
    USDA match."""
    items = list(result.items)
    current = items[item_index]
    item = current.item
    if fdc_id is not None:
        food = foods.get(fdc_id)
        if food is None:
            raise UnknownFood(f"No USDA food with FDC id {fdc_id}.")
        alternatives = [a for a in (current.match, *current.alternatives) if a and a.fdc_id != fdc_id]
    else:
        food = foods.get(current.match.fdc_id) if current.match else None
        alternatives = current.alternatives
    if grams is not None:
        item = item.model_copy(update={"grams_low": grams, "grams_likely": grams, "grams_high": grams})
    items[item_index] = _item_result(item, food, alternatives)
    return summarize(result.model_copy(update={"items": items}))


def meal_name(items: Sequence[FoodItem]) -> str | None:
    """'rice', 'rice and pizza', 'rice, pizza and sauce', 'rice, pizza, sauce and 2 more'."""
    names = [item.name for item in items]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    if len(names) > 3:
        return f"{', '.join(names[:3])} and {len(names) - 3} more"
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _match(food: Food) -> FoodMatch:
    return FoodMatch(fdc_id=food.fdc_id, description=food.description, data_type=food.data_type)


def _item_result(item: FoodItem, food: Food | None, alternatives: list[FoodMatch]) -> ItemResult:
    if food is None:
        return ItemResult(item=item, match=None, alternatives=alternatives)
    return ItemResult(
        item=item,
        match=_match(food),
        alternatives=alternatives,
        nutrients=item_nutrients(food.per_100g, item),
    )
