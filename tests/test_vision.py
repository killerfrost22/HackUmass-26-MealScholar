"""What Gemini is told (the output schema as sent, the prompts, the context) and how one call is
bounded: a timeout, one retry on an unusable reply, none on an SDK error or a final status."""

import hashlib
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mealscholar.schemas import FoodItem, MealAnalysis, MealContext, Place
from mealscholar.vision import REFINE_PROMPT, SYSTEM_PROMPT, GeminiVision, VisionError, context_prompt, response_schema

OLD_MENU_FIELDS = ("menu_item", "servings_low", "servings_likely", "servings_high")


def test_every_field_is_required_for_gemini_and_old_menu_fields_are_ignored():
    food_item = response_schema()["$defs"]["FoodItem"]
    assert sorted(food_item["required"]) == sorted(FoodItem.model_fields)
    assert not set(OLD_MENU_FIELDS) & set(food_item["properties"])
    # Bench runs recorded while campus menus were sent still validate; the menu fields are dropped.
    old = {
        "is_food": True,
        "items": [
            {
                "name": "white rice",
                "fdc_query": "Rice, white, cooked",
                "cooking_method": "steamed",
                "grams_low": 150,
                "grams_likely": 200,
                "grams_high": 260,
                "box_2d": None,
                "hidden_fat_likely": False,
                "menu_item": "Jasmine Rice",
                "servings_low": 1,
                "servings_likely": 1.5,
                "servings_high": 2,
            }
        ],
        "scale_reference": None,
        "question": None,
    }
    (item,) = MealAnalysis.model_validate_json(json.dumps(old)).items
    assert (item.name, item.grams_likely) == ("white rice", 200)
    assert not set(OLD_MENU_FIELDS) & set(item.model_dump())
    assert "menu_item" not in SYSTEM_PROMPT and "menu_item" not in REFINE_PROMPT


def test_context_prompt_names_the_date_the_place_and_usual_portions():
    context = MealContext(taken_at=datetime(2026, 11, 13, 12, 30), time_source="device", meal_slot="lunch")
    assert context_prompt(context, {}) == "Photo taken Fri 2026-11-13 12:30 local time (lunch)."

    place = Place(name="Antonio's", kind="fast_food", cuisine="pizza", chain=True)
    prompt = context_prompt(context.model_copy(update={"place": place}), {"white rice": 200, "cola": 355})
    assert prompt.splitlines() == [
        "Photo taken Fri 2026-11-13 12:30 local time (lunch).",
        "Taken at or near Antonio's (chain) (fast food, cuisine: pizza).",
        "This user's usual portions from past corrections: white rice: 200 g; cola: 355 g.",
    ]


def test_the_question_is_rare_and_easy_to_answer():
    """Rarely a question: mostly a drink the photo can't settle, sometimes an easy food question,
    never how it was cooked; one sentence, a choice phrased for one tap."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "usually null" in flat and "at a glance" in flat and "diet or regular soda" in flat
    assert "a beef or a black bean burger" in flat and "Never ask how it was cooked" in flat
    assert 'Phrase a choice as "<A> or <B>?"' in flat and "Ask it in one sentence." in flat


def test_bench_fingerprint_hashes_the_schema_as_sent():
    path = Path(__file__).resolve().parent.parent / "bench" / "run.py"
    spec = importlib.util.spec_from_file_location("bench_run", path)
    run = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = run  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(run)
    told = SYSTEM_PROMPT + REFINE_PROMPT + json.dumps(response_schema(), sort_keys=True)
    assert run.PROMPT == hashlib.sha256(told.encode()).hexdigest()[:8]


VALID = json.dumps(
    {
        "is_food": True,
        "items": [
            {
                "name": "cheese pizza",
                "fdc_query": "Pizza, cheese, from restaurant or fast food, thin crust",
                "cooking_method": "baked",
                "grams_low": 100,
                "grams_likely": 130,
                "grams_high": 170,
                "box_2d": [100, 100, 900, 900],
                "hidden_fat_likely": False,
            }
        ],
        "scale_reference": "dinner plate",
        "question": None,
    }
)


class FakeInteractions:
    """Stands in for client.interactions (the seam bench/run.py uses): records every create() call
    and answers with the scripted replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(text, status="completed"):
    return SimpleNamespace(status=status, output_text=text)


def vision_with(*replies):
    vision = GeminiVision("k", "m", "low")
    fake = FakeInteractions(*replies)
    vision._client = SimpleNamespace(interactions=fake)
    return vision, fake


def test_create_kwargs():
    vision, fake = vision_with(reply(VALID))
    analysis = vision.analyze(b"jpegbytes", "Photo taken Fri 2026-11-13 12:30 local time (lunch).")
    assert analysis.items[0].name == "cheese pizza"
    (kwargs,) = fake.calls
    assert kwargs["model"] == "m"
    assert kwargs["timeout"] == 60.0
    assert kwargs["store"] is False
    assert kwargs["generation_config"] == {"thinking_level": "low"}
    assert kwargs["response_format"]["mime_type"] == "application/json"
    assert kwargs["response_format"]["schema"] == response_schema()
    assert "cut off by the edge" in kwargs["system_instruction"] and "one sentence" in kwargs["system_instruction"]
    text, image = kwargs["input"]
    assert text["type"] == "text" and text["text"].startswith("Photo taken Fri 2026-11-13 12:30")
    assert image == {"type": "image", "data": "anBlZ2J5dGVz", "mime_type": "image/jpeg"}

    custom, fake = vision_with(reply(VALID))
    custom._timeout = 5.0
    custom.analyze(b"x", "ctx")
    assert fake.calls[0]["timeout"] == 5.0
    assert GeminiVision("k", "m", "low", timeout=12.5)._timeout == 12.5


def test_truncated_output_is_retried_once(caplog):
    vision, fake = vision_with(reply('{"is_food": true, "items": [', status="incomplete"), reply(VALID))
    with caplog.at_level("WARNING", logger="mealscholar.vision"):
        analysis = vision.analyze(b"jpeg", "ctx")
    assert analysis.is_food and len(analysis.items) == 1
    assert len(fake.calls) == 2 and fake.calls[0]["input"] == fake.calls[1]["input"]
    assert "incomplete" in caplog.text


def test_schema_miss_twice_is_a_vision_error():
    vision, fake = vision_with(reply('{"foo": 1}'), reply('{"foo": 1}'))
    with pytest.raises(VisionError, match="schema"):
        vision.analyze(b"jpeg", "ctx")
    assert len(fake.calls) == 2


def test_empty_reply_twice_is_a_vision_error():
    vision, fake = vision_with(reply("", status="completed"), reply(None, status="completed"))
    with pytest.raises(VisionError, match="no text"):
        vision.analyze(b"jpeg", "ctx")
    assert len(fake.calls) == 2


def test_failed_status_is_not_retried():
    vision, fake = vision_with(reply("", status="failed"), reply(VALID))
    with pytest.raises(VisionError, match="failed"):
        vision.analyze(b"jpeg", "ctx")
    assert len(fake.calls) == 1


def test_sdk_exception_is_a_vision_error():
    vision, fake = vision_with(RuntimeError("boom"), reply(VALID))
    with pytest.raises(VisionError, match="boom"):
        vision.analyze(b"jpeg", "ctx")
    assert len(fake.calls) == 1


def test_a_refused_request_is_final():
    refused = RuntimeError("400 INVALID_ARGUMENT: schema too complex")
    refused.status_code = 400
    vision, fake = vision_with(refused, reply(VALID))
    with pytest.raises(VisionError, match="INVALID_ARGUMENT"):
        vision.analyze(b"jpeg", "ctx")
    assert len(fake.calls) == 1


def test_refine_sends_text_only():
    vision, fake = vision_with(reply(VALID))
    analysis = MealAnalysis.model_validate_json(VALID)
    vision.refine(analysis, "How many slices?", "two")
    (kwargs,) = fake.calls
    (part,) = kwargs["input"]
    assert part["type"] == "text"
    assert analysis.model_dump_json() in part["text"] and "two" in part["text"]
    assert kwargs["timeout"] == 60.0
    assert kwargs["response_format"]["schema"] == response_schema()


def test_prompts_carry_the_benchmark_fixes():
    assert "never add a second entry" in REFINE_PROMPT
    assert "edge of the photo" in SYSTEM_PROMPT and "one sentence" in SYSTEM_PROMPT
