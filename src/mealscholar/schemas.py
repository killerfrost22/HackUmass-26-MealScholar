"""Data contracts passed between workflow steps."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MealSlot = Literal["breakfast", "lunch", "snack", "dinner"]
TimeSource = Literal["exif", "device", "upload"]
LocationSource = Literal["device", "exif"]
CookingMethod = Literal[
    "raw",
    "boiled",
    "steamed",
    "grilled",
    "roasted",
    "baked",
    "pan_fried",
    "stir_fried",
    "deep_fried",
    "unknown",
]


# --- Step 1: metadata -------------------------------------------------------


class PhotoMetadata(BaseModel):
    taken_at: datetime
    """Local wall-clock time of the meal; timezone-aware only when the offset is known."""
    time_source: TimeSource
    latitude: float | None = None
    longitude: float | None = None
    location_source: LocationSource | None = None


# --- Step 2: context (coordinates are dropped here) -------------------------


class Place(BaseModel):
    name: str
    kind: str
    """OpenStreetMap type, e.g. restaurant, fast_food, cafe."""
    cuisine: str | None = None
    chain: bool = False


class MealContext(BaseModel):
    taken_at: datetime
    time_source: TimeSource
    meal_slot: MealSlot
    place: Place | None = None
    notes: list[str] = []


# --- Step 3: vision output (Gemini structured output schema) ----------------
# Nullable fields have no default so the JSON schema marks them required-but-nullable,
# which is what Gemini's constrained decoding handles best.


class FoodItem(BaseModel):
    """One food or drink as seen in the photo. Deliberately carries no nutrient numbers."""

    name: str = Field(description="Plain name, e.g. 'chicken fried rice'.")
    fdc_query: str = Field(
        description=(
            "USDA FNDDS-style description: main food first, then comma-separated "
            "descriptors, e.g. 'Rice, fried, with chicken'."
        )
    )
    cooking_method: CookingMethod
    grams_low: int = Field(description="Low estimate of edible weight in grams.")
    grams_likely: int = Field(description="Most likely edible weight in grams.")
    grams_high: int = Field(description="High estimate of edible weight in grams.")
    box_2d: list[int] | None = Field(description="Bounding box [ymin, xmin, ymax, xmax] normalized to 0-1000.")
    hidden_fat_likely: bool = Field(
        description="True when oil, butter, dressing or sauce is probably present but not visible."
    )

    @model_validator(mode="after")
    def _ordered_grams(self) -> FoodItem:
        low, likely, high = sorted(max(0, g) for g in (self.grams_low, self.grams_likely, self.grams_high))
        self.grams_low, self.grams_likely, self.grams_high = low, likely, high
        if self.box_2d is not None and len(self.box_2d) != 4:
            self.box_2d = None
        return self


class MealAnalysis(BaseModel):
    is_food: bool = Field(description="False when the photo shows no food or drink.")
    items: list[FoodItem]
    scale_reference: str | None = Field(
        description="Object used to judge size, e.g. 'dinner fork'; null if none is visible."
    )
    question: str | None = Field(
        description=(
            "Usually null. At most one short question the eater can answer at a glance, when "
            "the answer changes the calories a lot: most often a drink the photo can't settle "
            "(diet or regular), sometimes which of two similar foods it is or whether a plate "
            "is shared; never how it was cooked."
        )
    )


# --- Steps 4-6: nutrition and results ----------------------------------------


class FoodMatch(BaseModel):
    fdc_id: int
    description: str
    data_type: str


class Range(BaseModel):
    low: float
    likely: float
    high: float


class ItemResult(BaseModel):
    item: FoodItem
    match: FoodMatch | None
    """The USDA food; None when no close match was found, so the item isn't counted."""
    alternatives: list[FoodMatch] = []
    """Other database candidates the user can switch to."""
    nutrients: dict[str, Range] = {}


class MealResult(BaseModel):
    id: int | None = None
    context: MealContext
    items: list[ItemResult]
    name: str | None = None
    """The item names joined, e.g. 'white rice, pizza slice and mystery sauce'."""
    scale_reference: str | None = None
    question: str | None = None
    answer: str | None = None
    totals: dict[str, Range] = {}
    daily_value_pct: dict[str, Range] = {}
    partial_nutrients: list[str] = []
    """Nutrient keys some matched food has no data for, so the total undercounts."""
    notes: list[str] = []
    photo_url: str | None = None
    """Where the app loads the meal's stored photo (GET /api/meals/{id}/photo, with a version that
    changes with the photo); None when no photo is kept, as for a meal that isn't logged."""

    def analysis(self) -> MealAnalysis:
        return MealAnalysis(
            is_food=bool(self.items),
            items=[r.item for r in self.items],
            scale_reference=self.scale_reference,
            question=self.question,
        )
