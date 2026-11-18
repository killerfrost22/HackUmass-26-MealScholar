import math
from datetime import datetime

import pytest
from PIL import ExifTags, Image

from mealscholar.diary import Diary
from mealscholar.insights import SCORE_MINUS, SCORE_PLUS, SUGARS_DV_G, health_score, summarize_days
from mealscholar.pipeline import UnknownFood, Workflow, correct, meal_name
from mealscholar.schemas import FoodItem, MealAnalysis, Range


def item(name, query, low, likely, high, *, method="unknown", hidden_fat=False):
    return FoodItem(
        name=name,
        fdc_query=query,
        cooking_method=method,
        grams_low=low,
        grams_likely=likely,
        grams_high=high,
        box_2d=None,
        hidden_fat_likely=hidden_fat,
    )


class FakeVision:
    def __init__(self, analysis):
        self.analysis = analysis
        self.context = None
        self.refined_with = None

    def analyze(self, jpeg, context):
        self.context = context
        return self.analysis

    def refine(self, analysis, question, answer):
        self.refined_with = (question, answer)
        rice = analysis.items[0].model_copy(update={"hidden_fat_likely": True})
        return analysis.model_copy(update={"items": [rice, *analysis.items[1:]]})


@pytest.fixture
def meal_photo(tmp_path):
    path = tmp_path / "meal.jpg"
    Image.new("RGB", (64, 48), "tan").save(path)
    return path


@pytest.fixture
def analysis():
    return MealAnalysis(
        is_food=True,
        items=[
            item("white rice", "Rice, white, cooked", 150, 200, 260, method="steamed"),
            item("pizza slice", "Pizza, cheese, from restaurant or fast food, thin crust", 100, 120, 150),
            item("mystery sauce", "Zzyzx", 10, 20, 30),
        ],
        scale_reference="dinner fork",
        question="Was the rice cooked with oil?",
    )


def one(food_item, question=None):
    return MealAnalysis(is_food=True, items=[food_item], scale_reference=None, question=question)


def stamp(path, when):
    """Give the photo an EXIF capture time, as a camera does, or with `when` None take it away, as a
    screenshot or an AirDropped photo has none."""
    img = Image.open(path)
    img.load()
    exif = Image.Exif()
    if when is not None:
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = when.strftime("%Y:%m:%d %H:%M:%S")
    img.save(path, exif=exif)


def analyze(food_db, analysis, meal_photo, *, device_time=None, dated=True, usual_portions=None):
    """`dated`: the photo carries the time in EXIF (a camera photo); otherwise only the device sends it."""
    at = device_time or datetime(2026, 11, 13, 12, 30)
    stamp(meal_photo, at if dated else None)
    workflow = Workflow(vision=FakeVision(analysis), foods=food_db)
    result = workflow.analyze(meal_photo, device_time=at, usual_portions=usual_portions)
    return workflow, result


def test_totals_are_grams_times_database_values_with_ranges(food_db, analysis, meal_photo):
    _, result = analyze(food_db, analysis, meal_photo)

    rice, pizza, sauce = result.items
    assert (rice.match.fdc_id, pizza.match.fdc_id, sauce.match) == (1, 6, None)
    kcal = result.totals["energy_kcal"]
    assert kcal.likely == pytest.approx(129 * 2.0 + 266 * 1.2)
    # The foods aren't all off in the same direction at once, so the meal range is narrower than
    # every low and every high added up. Part of the error is shared, so it's wider than for
    # independent errors.
    for side, widths in (
        (kcal.likely - kcal.low, (129 * 0.5, 266 * 0.2)),
        (kcal.high - kcal.likely, (129 * 0.6, 266 * 0.3)),
    ):
        assert math.hypot(*widths) < side < sum(widths)
    assert result.daily_value_pct["protein_g"].likely == pytest.approx(100 * (2.6 * 2.0 + 11 * 1.2) / 50)
    # Rice has no vitamin D row, so the vitamin D total covers the pizza only.
    assert "vitamin_d_ug" in result.partial_nutrients
    assert any("No close USDA match for 'mystery sauce'" in n for n in result.notes)
    assert result.context.meal_slot == "lunch"
    assert result.name == "white rice, pizza slice and mystery sauce"


def test_loose_lookup_is_suggested_not_counted(food_db, meal_photo):
    # "Cola bowl" shares only "cola" with "Soft drink, cola"; counting it would put wrong numbers in the totals.
    bowl_only = one(item("cola bowl", "Cola bowl", 100, 150, 200))
    _, result = analyze(food_db, bowl_only, meal_photo)

    (bowl,) = result.items
    assert bowl.match is None and 4 in [a.fdc_id for a in bowl.alternatives]
    assert result.totals == {}
    # Grams alone don't make it count; picking the suggestion does.
    assert correct(result, 0, food_db, grams=300).totals == {}
    picked = correct(result, 0, food_db, fdc_id=4)
    assert picked.items[0].match.fdc_id == 4 and 4 not in [a.fdc_id for a in picked.items[0].alternatives]
    assert picked.totals["energy_kcal"].likely == pytest.approx(42 * 1.5)


def test_answer_recomputes_from_refined_analysis(food_db, analysis, meal_photo):
    workflow, result = analyze(food_db, analysis, meal_photo)
    answered = workflow.answer(result.model_copy(update={"id": 7}), "yes, oil")

    assert workflow.vision.refined_with == ("Was the rice cooked with oil?", "yes, oil")
    assert answered.items[0].match.fdc_id == 3  # "made with oil"
    assert (answered.id, answered.answer, answered.question) == (7, "yes, oil", None)


def test_correction_collapses_range_and_swaps_match(food_db, analysis, meal_photo):
    _, result = analyze(food_db, analysis, meal_photo)
    old_match = result.items[0].match

    fixed = correct(result, 0, food_db, grams=300, fdc_id=2)

    rice = fixed.items[0]
    assert (rice.item.grams_low, rice.item.grams_likely, rice.item.grams_high) == (300, 300, 300)
    assert rice.match.fdc_id == 2 and old_match in rice.alternatives
    assert rice.nutrients["energy_kcal"].likely == pytest.approx(96 * 3)
    # A weighed food adds no width: the meal's range is the pizza's alone.
    kcal = fixed.totals["energy_kcal"]
    assert (kcal.low, kcal.likely, kcal.high) == pytest.approx((288 + 266 * 1.0, 288 + 266 * 1.2, 288 + 266 * 1.5))
    with pytest.raises(UnknownFood):
        correct(result, 0, food_db, fdc_id=999_999)


def test_prompt_carries_the_photos_time_and_usual_portions(food_db, analysis, meal_photo):
    workflow, _ = analyze(food_db, analysis, meal_photo)
    assert workflow.vision.context == "Photo taken Fri 2026-11-13 12:30 local time (lunch)."

    workflow, _ = analyze(food_db, analysis, meal_photo, usual_portions={"white rice": 300})
    assert workflow.vision.context.splitlines() == [
        "Photo taken Fri 2026-11-13 12:30 local time (lunch).",
        "This user's usual portions from past corrections: white rice: 300 g.",
    ]


@pytest.mark.parametrize("when", [datetime(1, 1, 1, 0, 30), datetime(9999, 12, 31, 23)])
def test_a_device_time_at_either_end_of_the_calendar_is_analyzed(food_db, analysis, meal_photo, when):
    """taken_at is the client's: the first and last days a datetime can hold still give a meal."""
    _, result = analyze(food_db, analysis, meal_photo, device_time=when, dated=False)
    assert result.context.taken_at == when and result.context.time_source == "device"
    assert result.totals["energy_kcal"].likely == pytest.approx(129 * 2.0 + 266 * 1.2)


def test_diary_round_trip_and_usual_portions(tmp_path, food_db, analysis, meal_photo):
    _, result = analyze(food_db, analysis, meal_photo)
    diary = Diary(tmp_path / "diary.sqlite")

    saved = diary.save(result)
    assert diary.get(saved.id) == saved
    assert diary.meals_between(saved.context.taken_at.date(), saved.context.taken_at.date()) == [saved]
    # Meals logged before a change to the totals math are shown with today's totals.
    diary.save(saved.model_copy(update={"totals": {"energy_kcal": Range(low=1, likely=2, high=3)}}))
    assert diary.get(saved.id).totals == saved.totals

    diary.remember_portion("White  Rice", 300)
    diary.remember_portion("white rice", 400)
    assert diary.usual_portions() == {"white rice": 350}


def test_day_total_is_narrower_than_its_meals_added_up(food_db, analysis, meal_photo):
    _, lunch = analyze(food_db, analysis, meal_photo)

    (day,) = summarize_days([lunch, lunch])

    meal, total = lunch.totals["energy_kcal"], day.totals["energy_kcal"]
    assert total.likely == pytest.approx(2 * meal.likely)
    assert meal.high - meal.likely < total.high - total.likely < 2 * (meal.high - meal.likely)


def test_meal_name_joins_the_items():
    names = ["rice", "pizza", "sauce", "salad", "water"]
    items = [item(n, n, 1, 2, 3) for n in names]
    assert meal_name([]) is None
    assert meal_name(items[:1]) == "rice"
    assert meal_name(items[:2]) == "rice and pizza"
    assert meal_name(items[:3]) == "rice, pizza and sauce"
    assert meal_name(items) == "rice, pizza, sauce and 2 more"


# --- the day's nutrient-balance score, the same rules as site/app.js healthScore ----------


def flat(value):
    return Range(low=value, likely=value, high=value)


SCORE_ORDER = (*SCORE_PLUS, *SCORE_MINUS, "sugars_g")


def test_health_score_matches_the_apps_rules():
    dv_pct = {"protein_g": flat(100), "fiber_g": flat(50), "sodium_mg": flat(150), "sat_fat_g": flat(80)}
    totals = {"sugars_g": flat(60)}  # no Daily Value for total sugars: scored against 50 g
    health = health_score(totals, dv_pct)
    assert [p.key for p in health.parts] == list(SCORE_ORDER)
    by_key = {p.key: p for p in health.parts}
    assert (by_key["protein_g"].pct, by_key["protein_g"].points, by_key["protein_g"].plus) == (100, 12.5, True)
    assert (by_key["fiber_g"].pct, by_key["fiber_g"].points) == (50, 6.25)
    assert (by_key["calcium_mg"].pct, by_key["calcium_mg"].points) == (None, 0)
    assert (by_key["sodium_mg"].pct, by_key["sodium_mg"].points, by_key["sodium_mg"].plus) == (150, -5.0, False)
    assert (by_key["sat_fat_g"].pct, by_key["sat_fat_g"].points) == (80, 0)
    assert (by_key["sugars_g"].pct, by_key["sugars_g"].points) == (pytest.approx(120), pytest.approx(-2.0))
    assert by_key["sugars_g"].label == "Total sugars" and by_key["vitamin_a_ug"].label == "Vitamin A (RAE)"
    assert health.score == 12  # 12.5 + 6.25 - 5 - 2 = 11.75
    assert health.note == "A rough guide, not medical advice."

    plenty = {key: flat(200) for key in SCORE_PLUS}
    assert health_score({}, plenty).score == 100  # clamped per part, never over 100
    assert health_score({}, {"protein_g": flat(100)}).score == 13  # 12.5: halves round up like Math.round
    nothing = health_score({}, {})
    assert nothing.score is None and len(nothing.parts) == 11 and all(p.pct is None for p in nothing.parts)

    added = health_score({"added_sugars_g": flat(75), "sugars_g": flat(10)}, {})
    sugars = added.parts[-1]
    assert (sugars.key, sugars.label, sugars.pct, sugars.points) == ("added_sugars_g", "Added sugars", 150, -5.0)


def test_health_score_pct_falls_back_to_totals_over_daily_value():
    totals = {"protein_g": flat(25), "sodium_mg": flat(4600), "sugars_g": flat(25), "energy_kcal": flat(800)}
    health = health_score(totals, {})
    by_key = {p.key: p for p in health.parts}
    assert by_key["protein_g"].pct == 50 and by_key["protein_g"].points == 6.25  # 25 g of the 50 g DV
    assert by_key["sodium_mg"].pct == 200 and by_key["sodium_mg"].points == -10  # 2 x 2300 mg
    assert by_key["sugars_g"].pct == 100 * 25 / SUGARS_DV_G and by_key["sugars_g"].points == 0
    assert health.score == 0  # 6.25 - 10 clamps at 0
