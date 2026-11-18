import io
import json
import logging
import shutil
import sqlite3
import struct
import zlib
from contextlib import closing
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_metadata import IPHONE_HEIC, heic
from test_prefs import DEFAULTS

import mealscholar.web
from mealscholar import cli
from mealscholar.config import load_settings
from mealscholar.diary import Diary
from mealscholar.insights import SCORE_MINUS, SCORE_PLUS
from mealscholar.schemas import FoodItem, MealAnalysis, MealContext, MealResult, Place
from mealscholar.security import RateLimiter, Rule
from mealscholar.vision import VisionError
from mealscholar.web import create_app

JPEG = {"Content-Type": "image/jpeg"}


def food(name: str, query: str, low: int, likely: int, high: int) -> FoodItem:
    return FoodItem(
        name=name,
        fdc_query=query,
        cooking_method="unknown",
        grams_low=low,
        grams_likely=likely,
        grams_high=high,
        box_2d=None,
        hidden_fat_likely=False,
    )


class FailingVision:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc

    def analyze(self, jpeg, context):
        raise self.exc or AssertionError("the photo should have been refused before Gemini")

    def refine(self, analysis, question, answer):
        raise AssertionError("not used")


class DisplayCaseVision:
    """What Gemini said for plastic display food (#49): dishes and a question, but not a meal."""

    def analyze(self, jpeg, context):
        crepe = food("crepe", "Crepe, plain", 120, 160, 200)
        question = "Which of these items from the display case are you eating?"
        return MealAnalysis(is_food=False, items=[crepe], scale_reference=None, question=question)

    def refine(self, analysis, question, answer):
        raise AssertionError("not used")


class RiceVision:
    """Sees a plate of white rice."""

    def analyze(self, jpeg, context):
        rice = food("white rice", "Rice, white, cooked", 150, 200, 260).model_copy(update={"cooking_method": "steamed"})
        return MealAnalysis(is_food=True, items=[rice], scale_reference=None, question=None)

    def refine(self, analysis, question, answer):
        raise AssertionError("not used")


class FakeVision:
    """Sees rice and a cola and asks one question; remembers the context of every photo."""

    def __init__(self) -> None:
        self.contexts: list[str] = []
        self.answers = 0

    def analyze(self, jpeg: bytes, context: str) -> MealAnalysis:
        self.contexts.append(context)
        return MealAnalysis(
            is_food=True,
            items=[
                food("white rice", "Rice, white, cooked", 150, 200, 260),
                food("cola", "Soft drink, cola", 330, 355, 400),
            ],
            scale_reference="dinner fork",
            question="Was the rice cooked with oil?",
        )

    def refine(self, analysis: MealAnalysis, question: str, answer: str) -> MealAnalysis:
        self.answers += 1
        return analysis


class NoPlaces:
    def at(self, latitude, longitude):
        return None


def client(home, vision, *, places=None, limiter=None) -> TestClient:
    settings = load_settings({"MEALSCHOLAR_HOME": str(home)})
    app = create_app(settings, vision=vision, places=places or NoPlaces(), limiter=limiter)
    return TestClient(app, raise_server_exceptions=False)


def browser(app, headers=None, ip="testclient") -> TestClient:
    return TestClient(app, base_url="https://testserver", headers=headers, client=(ip, 50000))


@pytest.fixture
def home(tmp_path, food_db_path):
    shutil.copy(food_db_path, tmp_path / "fdc.sqlite")  # a copy: symlinks need extra rights on Windows
    return tmp_path


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "tan").save(buf, "JPEG")
    return buf.getvalue()


def png_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims a huge size, as a decompression bomb would."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b"")


@pytest.mark.parametrize(
    ("path", "status"),
    [("/", 200), ("/styles.css", 200), ("/api/meals/999", 404), ("/missing-page", 404)],
)
def test_pages_and_api_errors_have_browser_protections(tmp_path, path, status):
    app = TestClient(create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path)})))
    response = app.get(path)
    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-frame-options"] == "DENY"


def test_pages_are_checked_for_a_new_deploy_and_assets_keep_their_caching(tmp_path):
    """The page and the manifest are revalidated on every load (no-cache, then a 304 while unchanged),
    so a deploy reaches a phone at once; the assets the page links with ?v= and the API are left alone."""
    client = TestClient(create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path)})))
    for path in ("/", "/app/", "/manifest.webmanifest", "/app/manifest.webmanifest"):
        res = client.get(path)
        assert res.headers["cache-control"] == "no-cache", path
        assert client.get(path, headers={"If-None-Match": res.headers["etag"]}).status_code == 304, path
    for path in ("/styles.css", "/app/app.js", "/api/status"):
        assert "cache-control" not in client.get(path).headers, path


@pytest.mark.parametrize(("public_url", "status"), [(None, 200), ("https://app.example.com", 404)])
def test_api_docs_only_for_local_development(tmp_path, public_url, status):
    env = {"MEALSCHOLAR_HOME": str(tmp_path)} | ({"MEALSCHOLAR_PUBLIC_URL": public_url} if public_url else {})
    client = TestClient(create_app(load_settings(env)))
    for path in ("/api/docs", "/api/openapi.json", "/app/api/docs", "/app/api/openapi.json"):
        assert client.get(path).status_code == status, path


def test_status_says_what_is_set_up(home, tmp_path_factory):
    assert client(home, FakeVision()).get("/api/status").json() == {"gemini_ready": True, "food_db_ready": True}
    empty = TestClient(create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path_factory.mktemp("empty"))})))
    assert empty.get("/api/status").json() == {"gemini_ready": False, "food_db_ready": False}


@pytest.mark.parametrize(
    ("photo", "status"),
    [
        (png_claiming(40_000, 40_000), 413),
        (png_claiming(10_000, 6_000), 413),  # below Pillow's default warning threshold
        (jpeg()[:300], 415),  # cut off mid-upload
        (b"not an image at all", 415),
    ],
    ids=["Pillow decompression bomb", "over 50 megapixels", "truncated", "not an image"],
)
def test_photos_that_wont_decode_get_a_client_error(home, photo, status):
    res = client(home, FailingVision()).post("/api/meals", content=photo)
    assert res.status_code == status
    assert "detail" in res.json()


@pytest.mark.filterwarnings("error:Image size:RuntimeWarning")  # as the server runs (PYTHONWARNINGS)
def test_a_photo_over_pillows_warning_size_gets_the_megapixel_reply(home):
    res = client(home, FailingVision()).post("/api/meals", content=png_claiming(12_000, 9_000))  # 108 MP
    assert res.status_code == 413
    assert res.json()["detail"] == "Photos are limited to 50 megapixels. Choose a lower-resolution photo."


def test_gemini_failure_reply_hides_the_providers_error(home, caplog):
    raw = 'Gemini request failed: 403 PERMISSION_DENIED {"message": "API key not valid"}'
    with caplog.at_level(logging.WARNING, logger="mealscholar.web"):
        app = client(home, FailingVision(VisionError(raw)))
        res = app.post("/api/meals?lat=42.389123&lon=-72.528456", content=jpeg())
    assert res.status_code == 502
    assert "PERMISSION_DENIED" not in res.text and "API key" not in res.text
    assert "PERMISSION_DENIED" in caplog.text  # still there for whoever runs the server
    assert "42.38" not in caplog.text  # the log line names the path, not the query


def test_missing_food_database_reply_hides_the_server_path(tmp_path):
    res = client(tmp_path, FailingVision()).post("/api/meals", content=jpeg())
    assert res.status_code == 503
    assert str(tmp_path) not in res.text and "fdc.sqlite" not in res.text


def test_a_photo_that_is_not_a_meal_is_not_logged(home):
    app = client(home, DisplayCaseVision())
    res = app.post("/api/meals", content=jpeg())
    assert res.status_code == 200
    meal = res.json()
    assert (meal["id"], meal["items"], meal["totals"], meal["question"]) == (None, [], {}, None)
    assert meal["notes"] == ["No food or drink detected in the photo."]
    day = app.get(f"/api/days/{meal['context']['taken_at'][:10]}").json()
    assert day["meals"] == [] and day["health"] is None


def test_day_view_carries_the_score(home):
    app = client(home, RiceVision())
    meal = app.post("/api/meals", content=jpeg()).json()
    day = app.get(f"/api/days/{meal['context']['taken_at'][:10]}").json()
    assert len(day["meals"]) == 1
    health = day["health"]
    assert isinstance(health["score"], int) and 0 <= health["score"] <= 100
    expected = [*SCORE_PLUS, *SCORE_MINUS, "sugars_g"]
    assert [p["key"] for p in health["parts"]] == expected
    assert health["parts"][0]["plus"] is True and health["parts"][-1]["plus"] is False
    assert health["note"] == "A rough guide, not medical advice."


class OnePlace:
    """Every location is the same pizzeria; remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[float, float]] = []

    def at(self, latitude, longitude):
        self.asked.append((latitude, longitude))
        return Place(name="Antonio's", kind="fast_food", cuisine="pizza")


def test_lat_and_lon_name_the_place_and_go_together(home):
    places, vision = OnePlace(), FakeVision()
    app = client(home, vision, places=places)
    res = app.post("/api/meals?lat=42.3932825&lon=-72.5250891", content=jpeg())
    assert res.status_code == 200
    meal = res.json()
    assert meal["context"]["place"] == {"name": "Antonio's", "kind": "fast_food", "cuisine": "pizza", "chain": False}
    assert "Taken at or near Antonio's (fast food, cuisine: pizza)." in vision.contexts[-1]
    # The coordinates only name the place: never stored, never sent to the model.
    assert "42.39" not in json.dumps(app.get(f"/api/meals/{meal['id']}").json()) and "42.39" not in vision.contexts[-1]

    for query in ("lat=42.39", "lon=-72.52"):
        res = app.post(f"/api/meals?{query}", content=jpeg())
        assert (res.status_code, res.json()) == (400, {"detail": "lat and lon go together."})
    assert places.asked == [(42.3932825, -72.5250891)] and len(vision.contexts) == 1


# --- when the meal happened: EXIF from a HEIC, then the phone's clock, then the upload -----


def on_day(app, day: str, meal_id: int) -> bool:
    return meal_id in [m["id"] for m in app.get(f"/api/days/{day}").json()["meals"]]


def test_heic_upload_is_dated_from_its_exif(home):
    app = client(home, RiceVision())
    res = app.post("/api/meals", content=heic(**IPHONE_HEIC), headers={"Content-Type": "image/heic"})
    assert res.status_code == 200
    meal = res.json()
    assert meal["context"]["time_source"] == "exif"
    assert meal["context"]["taken_at"].startswith("2026-11-13T12:34:56-05:00")
    assert meal["context"]["place"] is None  # photo GPS stays opt-in, so no place lookup
    assert on_day(app, "2026-11-13", meal["id"])


def test_taken_at_dates_a_photo_without_exif(home):
    app = client(home, RiceVision())
    res = app.post("/api/meals?taken_at=2026-11-13T23:50:00%2B09:00", content=jpeg())
    assert res.status_code == 200
    meal = res.json()
    assert meal["context"]["time_source"] == "device"
    assert meal["context"]["taken_at"] == "2026-11-13T23:50:00+09:00"
    assert not any("using upload time" in n for n in meal["notes"])
    assert on_day(app, "2026-11-13", meal["id"]) and not on_day(app, "2026-11-14", meal["id"])


def test_taken_at_in_utc_is_accepted_and_filed_under_that_clock(home):
    app = client(home, RiceVision())
    res = app.post("/api/meals?taken_at=2026-11-13T23:50:00Z", content=jpeg())
    assert res.status_code == 200
    meal = res.json()
    assert meal["context"]["time_source"] == "device"
    assert meal["context"]["taken_at"].endswith(("+00:00", "Z"))
    assert on_day(app, "2026-11-13", meal["id"])  # UTC's date, not the phone's: send local time


def test_taken_at_without_an_offset_is_refused(home):
    app = client(home, RiceVision())
    res = app.post("/api/meals?taken_at=2026-11-13T23:50:00", content=jpeg())
    assert res.status_code == 400
    assert res.json()["detail"] == "taken_at needs a UTC offset, such as 2026-11-14T12:30:00-05:00."
    assert app.post("/api/meals?taken_at=2026-11-13T23:50:00+09:00", content=jpeg()).status_code == 422


def test_exif_time_beats_taken_at(home):
    app = client(home, RiceVision())
    res = app.post("/api/meals?taken_at=2026-11-14T08:00:00-05:00", content=heic(**IPHONE_HEIC))
    assert res.status_code == 200
    assert res.json()["context"]["time_source"] == "exif"
    assert res.json()["context"]["taken_at"].startswith("2026-11-13T12:34:56")


# --- the diary over HTTP ----------------------------------------------------------------


def test_the_web_app_and_the_cli_share_one_diary(home):
    vision = FakeVision()
    app = client(home, vision)
    res = app.post("/api/meals", content=jpeg(), headers=JPEG)
    assert res.status_code == 200
    meal = res.json()
    url = f"/api/meals/{meal['id']}"
    assert app.get(url).json() == meal
    assert app.patch(f"{url}/items/0", json={"grams": 250}).status_code == 200
    other = app.post("/api/meals", content=jpeg(), headers=JPEG).json()
    assert "usual portions from past corrections: white rice: 250 g" in vision.contexts[-1]  # the next photo's

    assert app.post(f"{url}/answer", json={"answer": ""}).status_code == 422  # FastAPI's usual
    answered = app.post(f"{url}/answer", json={"answer": " yes, oil "})
    assert answered.status_code == 200
    assert (answered.json()["answer"], answered.json()["question"], vision.answers) == ("yes, oil", None, 1)
    again = app.post(f"{url}/answer", json={"answer": "yes"})
    assert (again.status_code, again.json()) == (409, {"detail": "This meal has no open question."})
    day = meal["context"]["taken_at"][:10]
    assert [m["id"] for m in app.get(f"/api/days/{day}").json()["meals"]] == [meal["id"], other["id"]]
    with closing(Diary(home / "diary.sqlite")) as local:  # the same rows the CLI sees
        assert local.get(meal["id"]) == MealResult.model_validate(answered.json())
        assert local.usual_portions() == {"white rice": 250}

    deleted = app.delete(url)
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert app.get(url).json() == {"detail": f"No meal #{meal['id']}."}
    assert [m["id"] for m in app.get(f"/api/days/{day}").json()["meals"]] == [other["id"]]


def test_a_missing_meal_is_not_found_everywhere(home):
    vision = FakeVision()
    app = client(home, vision)
    for res in (
        app.get("/api/meals/999"),
        app.get("/api/meals/999/photo"),
        app.post("/api/meals/999/answer", json={"answer": "yes, oil"}),
        app.patch("/api/meals/999/items/0", json={"grams": 1}),
        app.delete("/api/meals/999"),
    ):
        assert (res.status_code, res.json()) == (404, {"detail": "No meal #999."})
    assert vision.answers == 0


def test_grams_and_food_corrections_through_the_api(home):
    app = client(home, FakeVision())
    meal = app.post("/api/meals", content=jpeg(), headers=JPEG).json()
    url = f"/api/meals/{meal['id']}/items/0"
    estimated = meal["items"][0]

    res = app.patch(url, json={"grams": 300})
    assert res.status_code == 200
    rice = res.json()["items"][0]
    assert (rice["item"]["grams_low"], rice["item"]["grams_likely"], rice["item"]["grams_high"]) == (300, 300, 300)
    assert rice["match"] == estimated["match"]
    kcal, weighed = rice["nutrients"]["energy_kcal"], estimated["nutrients"]["energy_kcal"]["likely"] * 300 / 200
    assert kcal["low"] == kcal["likely"] == kcal["high"] == pytest.approx(weighed)
    assert app.get(f"/api/meals/{meal['id']}").json() == res.json()  # the diary keeps it

    res = app.patch(url, json={"fdc_id": 2})
    rice = res.json()["items"][0]
    assert rice["match"]["fdc_id"] == 2 and estimated["match"] in rice["alternatives"]
    assert rice["item"]["grams_likely"] == 300  # the amount stays
    assert rice["nutrients"]["energy_kcal"]["likely"] == pytest.approx(96 * 3)
    fixed = res.json()

    res = app.patch(url, json={})
    assert (res.status_code, res.json()) == (400, {"detail": "Send grams and/or fdc_id."})
    res = app.patch(url, json={"fdc_id": 999_999})
    assert (res.status_code, res.json()) == (404, {"detail": "No USDA food with FDC id 999999."})
    assert app.patch(url, json={"grams": 0}).status_code == 422
    assert app.patch(url, json={"grams": 10_001}).status_code == 422
    res = app.patch(f"/api/meals/{meal['id']}/items/2", json={"grams": 1})
    assert (res.status_code, res.json()) == (404, {"detail": f"Meal #{meal['id']} has no item 2."})
    assert app.get(f"/api/meals/{meal['id']}").json() == fixed  # the refusals changed nothing


def test_clearing_history_deletes_meals_and_portions_and_keeps_settings(home):
    app = client(home, FakeVision())
    first = app.post("/api/meals", content=jpeg(), headers=JPEG).json()
    assert app.patch(f"/api/meals/{first['id']}/items/0", json={"grams": 300}).status_code == 200
    app.post("/api/meals", content=jpeg(), headers=JPEG)
    assert app.patch("/api/me/prefs", json={"onboarded": True}).status_code == 200
    day = first["context"]["taken_at"][:10]

    assert app.delete("/api/meals").json() == {"meals": 2}
    assert app.get(f"/api/meals/{first['id']}").status_code == 404
    assert app.get(f"/api/days/{day}").json()["meals"] == []
    with closing(Diary(home / "diary.sqlite")) as diary:
        assert diary.usual_portions() == {}  # learned from the deleted meals, so gone too
    assert app.get("/api/me/prefs").json()["onboarded"] is True  # settings stay
    assert app.delete("/api/meals").json() == {"meals": 0}


def test_erasing_everything_starts_over(home):
    vision = FakeVision()
    app = client(home, vision)
    meal = app.post("/api/meals", content=jpeg(), headers=JPEG).json()
    assert app.patch(f"/api/meals/{meal['id']}/items/0", json={"grams": 300}).status_code == 200
    assert app.patch("/api/me/prefs", json={"onboarded": True, "calorie_goal": 2200}).status_code == 200
    assert app.get(meal["photo_url"]).status_code == 200

    res = app.delete("/api/me")
    assert (res.status_code, res.json()) == (200, {"meals": 1})
    assert app.get(f"/api/meals/{meal['id']}").status_code == 404
    assert app.get(meal["photo_url"]).status_code == 404
    assert not (home / "photos" / "local").exists()
    assert app.get("/api/me/prefs").json() == DEFAULTS
    app.post("/api/meals", content=jpeg(), headers=JPEG)
    assert "usual portions" not in vision.contexts[-1]  # the learned portions went too
    assert app.delete("/api/me").json() == {"meals": 1}


# --- diaries from before this change ----------------------------------------------------

OLD_DIARY = """
CREATE TABLE meals (
    id         INTEGER PRIMARY KEY,
    local_date TEXT NOT NULL,
    meal_slot  TEXT NOT NULL,
    taken_at   TEXT NOT NULL,
    result     TEXT NOT NULL
);
CREATE INDEX meals_by_date ON meals (local_date, taken_at);
CREATE TABLE usual_portions (
    name        TEXT PRIMARY KEY,
    grams_total REAL    NOT NULL,
    corrections INTEGER NOT NULL,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def test_a_diary_from_before_accounts_keeps_its_rows(tmp_path):
    path = tmp_path / "diary.sqlite"
    context = MealContext(taken_at=datetime(2026, 11, 13, 12, 30), time_source="device", meal_slot="lunch")
    meal = MealResult(id=1, context=context, items=[])
    with closing(sqlite3.connect(path)) as con:
        con.executescript(OLD_DIARY)
        stored = (context.taken_at.isoformat(), meal.model_dump_json())
        con.execute("INSERT INTO meals VALUES (1, '2026-11-13', 'lunch', ?, ?)", stored)
        con.execute("INSERT INTO usual_portions (name, grams_total, corrections) VALUES ('white rice', 700, 2)")
        con.commit()

    day = date(2026, 11, 13)
    with closing(Diary(path)) as local:
        assert local.get(1) == meal
        assert local.meals_between(day, day) == [meal]
        assert local.usual_portions() == {"white rice": 350}
    with closing(Diary(path)) as local:  # opening again changes nothing
        assert local.get(1) == meal
        assert local.usual_portions() == {"white rice": 350}

    with closing(sqlite3.connect(path)) as con:
        key = sorted((row[5], row[1]) for row in con.execute("PRAGMA table_info(usual_portions)") if row[5])
        assert [name for _, name in key] == ["user_id", "name"]
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "meals_by_user_date" in indexes and "meals_by_date" not in indexes
        # A meal an account logged while the web app had accounts stays in the file, never shown.
        con.execute(
            "INSERT INTO meals (id, user_id, local_date, meal_slot, taken_at, result) VALUES (2, 'user-1', ?, ?, ?, ?)",
            ("2026-11-13", "lunch", *stored),
        )
        con.commit()
    with closing(Diary(path)) as local:
        assert local.get(2) is None and local.meals_between(day, day) == [meal]
        assert not local.delete(2)


def old_item(name, query, grams, kcal_per_gram, **old_fields):
    low, likely, high = grams
    return {
        "item": {
            "name": name,
            "fdc_query": query,
            "cooking_method": "steamed",
            "grams_low": low,
            "grams_likely": likely,
            "grams_high": high,
            "box_2d": None,
            "hidden_fat_likely": False,
            "menu_item": old_fields.pop("menu_item", None),
            "servings_low": old_fields.pop("servings_low", None),
            "servings_likely": old_fields.pop("servings_likely", None),
            "servings_high": old_fields.pop("servings_high", None),
        },
        "nutrients": {
            "energy_kcal": {"low": low * kcal_per_gram, "likely": likely * kcal_per_gram, "high": high * kcal_per_gram}
        },
        "alternatives": [],
        **old_fields,
    }


def test_a_meal_matched_to_a_campus_menu_still_loads(home):
    """Meals logged with campus menus carry `menu`, `source`, `dish`, `menu_item` and servings. They
    load without them, and a campus dish that had no USDA match is no longer counted."""
    usda_match = {"fdc_id": 1, "description": "Rice, white, cooked, NS as to fat", "data_type": "Survey (FNDDS)"}
    rice = old_item(
        "white rice", "Rice, white, cooked", (150, 200, 260), 1.29, match=usda_match, source="usda", dish=None
    )
    dish = {
        "location_id": 1,
        "location_name": "Worcester Dining Commons",
        "date": "2026-11-13",
        "meal": "lunch",
        "name": "Cheese Pizza",
        "serving_size": "1 Slice",
        "serving_grams": None,
        "per_serving": {"energy_kcal": 212},
        "healthfulness_score": 4,
    }
    pizza = old_item(
        "pizza slice",
        "Zzyzx",
        (100, 120, 150),
        212 / 80,
        match=None,
        source="campus",
        dish=dish,
        menu_item="Cheese Pizza",
        servings_low=1,
        servings_likely=1.5,
        servings_high=2,
    )
    stored = {
        "id": 41,
        "context": {"taken_at": "2026-11-13T12:30:00-05:00", "time_source": "exif", "meal_slot": "lunch"},
        "items": [rice, pizza],
        "name": "white rice and pizza slice",
        "menu": {
            "location_id": 1,
            "location_name": "Worcester Dining Commons",
            "date": "2026-11-13",
            "dishes": 7,
            "meal": "lunch",
            "certain": True,
            "start": None,
            "end": None,
        },
        "totals": {"energy_kcal": {"low": 460, "likely": 576, "high": 733}},  # counted the campus slice
        "notes": ["Campus menu: Worcester Dining Commons, 2026-11-13."],
    }
    with closing(Diary(home / "diary.sqlite")):
        pass  # the tables as the app makes them
    with closing(sqlite3.connect(home / "diary.sqlite")) as con:
        con.execute(
            "INSERT INTO meals (id, user_id, local_date, meal_slot, taken_at, result) VALUES (41, '', ?, ?, ?, ?)",
            ("2026-11-13", "lunch", "2026-11-13T12:30:00-05:00", json.dumps(stored)),
        )
        con.commit()

    app = client(home, FakeVision())
    res = app.get("/api/meals/41")
    assert res.status_code == 200
    meal = res.json()
    assert "menu" not in meal and meal["notes"] == stored["notes"] and meal["name"] == "white rice and pizza slice"
    assert [(r["item"]["name"], r["match"]) for r in meal["items"]] == [
        ("white rice", usda_match),
        ("pizza slice", None),
    ]
    for r in meal["items"]:
        assert not {"source", "dish"} & set(r)
        assert not {"menu_item", "servings_low", "servings_likely", "servings_high"} & set(r["item"])
    # Only the rice counts now; the totals are recomputed from the items.
    assert set(meal["totals"]) == {"energy_kcal"}
    kcal = meal["totals"]["energy_kcal"]
    assert (kcal["low"], kcal["likely"], kcal["high"]) == pytest.approx((150 * 1.29, 200 * 1.29, 260 * 1.29))
    assert [m["id"] for m in app.get("/api/days/2026-11-13").json()["meals"]] == [41]

    # Picking a USDA food for the slice counts it again.
    res = app.patch("/api/meals/41/items/1", json={"fdc_id": 6})
    assert res.status_code == 200
    assert res.json()["totals"]["energy_kcal"]["likely"] == pytest.approx(200 * 1.29 + 266 * 1.2)


# --- analysis limits, cross-site writes and logs ------------------------------------------


def test_rate_limiter_slides_and_counts_nothing_it_refuses():
    now = [1000.0]
    limiter = RateLimiter(clock=lambda: now[0])
    per_ip, per_email = Rule("ip", 3, 60), Rule("email", 2, 60)

    def attempt(email):
        return limiter.hit((per_ip, "192.0.2.1"), (per_email, email))

    assert attempt("a") is None and attempt("a") is None
    now[0] = 1010.0
    assert attempt("a") == 50  # the first attempt leaves the window at 1060
    assert attempt("b") is None  # the refused attempt was not counted against the IP
    assert attempt("c") == 50
    now[0] = 1060.0
    assert attempt("c") is None


def test_photos_and_answers_share_one_budget_per_network(home, monkeypatch):
    monkeypatch.setattr(mealscholar.web, "ANALYSIS_PER_IP", Rule("analysis-ip", 2, 600))
    now = [1000.0]
    vision = FakeVision()
    settings = load_settings({"MEALSCHOLAR_HOME": str(home)})
    app = create_app(settings, vision=vision, places=NoPlaces(), limiter=RateLimiter(clock=lambda: now[0]))
    first = browser(app, ip="198.51.100.1")
    second = browser(app, ip="198.51.100.1")
    outside = browser(app, ip="203.0.113.1")

    meal = first.post("/api/meals", content=jpeg(), headers=JPEG).json()
    assert second.post("/api/meals", content=jpeg(), headers=JPEG).status_code == 200
    limited = first.post(f"/api/meals/{meal['id']}/answer", json={"answer": "yes"})
    assert (limited.status_code, limited.headers["Retry-After"]) == (429, "600")
    assert limited.json() == {"detail": "Too many analysis requests. Try again in 600 seconds."}
    assert first.post("/api/meals", content=b"not an image").status_code == 429  # refused before it is read
    assert (len(vision.contexts), vision.answers) == (2, 0)  # neither refusal called the model
    assert outside.post("/api/meals", content=jpeg(), headers=JPEG).status_code == 200

    now[0] += 600
    assert first.post(f"/api/meals/{meal['id']}/answer", json={"answer": "yes"}).status_code == 200
    assert (len(vision.contexts), vision.answers) == (3, 1)


@pytest.mark.parametrize(
    ("first_ip", "second_ip", "limited"),
    [
        ("127.0.0.1", "127.0.0.1", False),  # the trusted tunnel forwards distinct visitors
        ("::1", "::1", False),
        ("192.168.1.50", "192.168.1.50", True),  # a LAN caller cannot spoof that header
        ("2001:db8::1", "2001:db8::2", True),  # one machine can pick any address in its /64
        ("2001:db8::1", "2001:db8:0:1::1", False),  # the next /64 is someone else
        ("::ffff:198.51.100.1", "198.51.100.1", True),
    ],
)
def test_analysis_uses_the_trusted_network_identity(home, monkeypatch, first_ip, second_ip, limited):
    monkeypatch.setattr(mealscholar.web, "ANALYSIS_PER_IP", Rule("analysis-ip", 1, 600))
    vision = FakeVision()
    app = create_app(load_settings({"MEALSCHOLAR_HOME": str(home)}), vision=vision, places=NoPlaces())
    first = browser(app, {"CF-Connecting-IP": "203.0.113.1"}, ip=first_ip)
    second = browser(app, {"CF-Connecting-IP": "203.0.113.2"}, ip=second_ip)
    assert first.post("/api/meals", content=jpeg(), headers=JPEG).status_code == 200
    result = second.post("/api/meals", content=jpeg(), headers=JPEG)
    assert result.status_code == (429 if limited else 200)
    assert len(vision.contexts) == (1 if limited else 2)
    assert first.post("/api/meals", content=jpeg(), headers=JPEG).status_code == 429


def test_cross_site_writes_are_refused(home):
    vision = FakeVision()
    settings = load_settings({"MEALSCHOLAR_HOME": str(home), "MEALSCHOLAR_PUBLIC_URL": "https://mealscholar.example"})
    app = TestClient(create_app(settings, vision=vision, places=NoPlaces()))
    meal = app.post("/api/meals", content=jpeg(), headers=JPEG).json()  # no Origin header
    url = f"/api/meals/{meal['id']}"
    for origin in ("https://evil.example", "null", "https://testserver.evil.example", "http://[::1"):
        res = app.post("/api/meals", content=jpeg(), headers={**JPEG, "Origin": origin})
        assert (res.status_code, res.json()) == (403, {"detail": "Cross-site requests are not allowed."}), origin
    evil = {"Origin": "https://evil.example"}
    for res in (
        app.post(f"{url}/answer", json={"answer": "yes"}, headers=evil),
        app.patch(f"{url}/items/0", json={"grams": 5}, headers=evil),
        app.patch("/api/me/prefs", json={"onboarded": True}, headers=evil),
        app.delete(url, headers=evil),
        app.delete("/api/meals", headers=evil),
        app.delete("/api/me", headers=evil),
        app.post("/app/api/meals", content=jpeg(), headers=evil),
    ):
        assert res.status_code == 403
    assert app.get(url, headers=evil).json() == meal  # reads pass, and nothing was changed
    assert app.get("/api/me/prefs").json()["onboarded"] is False
    assert (len(vision.contexts), vision.answers) == (1, 0)

    for origin in ("http://testserver", "https://mealscholar.example"):  # this site, and its public URL
        assert app.post("/api/meals", content=jpeg(), headers={**JPEG, "Origin": origin}).status_code == 200


def test_access_log_hides_coordinates(tmp_path, caplog):
    """The app sends the device location as ?lat=&lon= on POST /api/meals, and uvicorn logs paths with their query."""
    create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path)}))
    access = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for path in (
            "/api/meals?lat=42.389123&lon=-72.528456",
            "/app/api/meals?taken_at=2026-11-13T12:30:00-05:00&lon=-72.528456&lat=42.389123",
        ):
            access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.9:50000", "POST", path, "1.1", 200)
    assert "42.38" not in caplog.text and "72.52" not in caplog.text
    assert '"POST /api/meals?lat=[hidden]&lon=[hidden] HTTP/1.1" 200' in caplog.text
    assert "taken_at=2026-11-13T12:30:00-05:00&lon=[hidden]&lat=[hidden]" in caplog.text


# --- settings ---------------------------------------------------------------------------


def test_settings_from_the_environment():
    defaults = load_settings({})
    assert (defaults.public_url, defaults.thinking_level) == ("http://127.0.0.1:8000", "low")
    custom = load_settings({"MEALSCHOLAR_PUBLIC_URL": "https://meals.example/", "MEALSCHOLAR_THINKING": "High"})
    assert (custom.public_url, custom.thinking_level) == ("https://meals.example", "high")
    # An .env from when the app had accounts and campus menus still loads; those settings are ignored.
    old = {
        "MONGODB_URI": "mongodb://localhost",
        "MEALSCHOLAR_AUTH_REQUIRED": "maybe",
        "MEALSCHOLAR_COOKIE_SECURE": "2",
        "MEALSCHOLAR_EMAIL_PROVIDER": "pigeon",
        "MEALSCHOLAR_DINING_DB": "umass_dining",
    }
    assert load_settings(old) == defaults

    for name, value in (("MEALSCHOLAR_PUBLIC_URL", "meals.example"), ("MEALSCHOLAR_THINKING", "ultra")):
        with pytest.raises(ValueError, match=name):
            load_settings({name: value})


def test_a_malformed_setting_is_a_clean_cli_error(monkeypatch, tmp_path, capsys):
    """Every command reads the settings, so a typo in one must not be a traceback."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("MEALSCHOLAR_HOME", str(tmp_path))
    monkeypatch.setenv("MEALSCHOLAR_THINKING", "ultra")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["day", "2026-11-13"])
    assert stopped.value.code == 1
    expected = "error: MEALSCHOLAR_THINKING must be one of minimal, low, medium, high, xhigh, not 'ultra'.\n"
    assert capsys.readouterr().err == expected


# --- the app under /app -----------------------------------------------------------------


def test_the_app_also_answers_under_app_for_the_landing_domain(tmp_path):
    """mealscholar.health/app/* is routed to this server, so an icon added to a phone's Home Screen
    from the landing opens the app on the same origin. Every page path is relative, so the page
    and its assets work at / and at /app/ alike."""
    client = TestClient(create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path)})))
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/"
    page = client.get("/app/")
    assert page.status_code == 200 and "<html" in page.text
    assert (
        'href="styles.css' in page.text and 'src="app.js' in page.text and 'href="/' not in page.text.split("<body")[0]
    )
    assert client.get("/app/api/status").json() == client.get("/api/status").json()
    assert client.get("/app/manifest.webmanifest").json()["scope"] == "./"
    assert client.get("/app/icons/icon-192.png").headers["content-type"] == "image/png"
    assert client.get("/app/nope").status_code == 404


def test_the_app_answers_under_app_one_level_deep_only(tmp_path):
    """/app/app/... is not a path of the site. The app under /app doesn't nest itself again, so a path
    with many /app in it is a plain 404, not a recursion error with a long traceback in the log."""
    client = TestClient(create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path)})))
    deep = "/app" * 61 + "/api/status"
    for res in (client.get("/app/app/api/status"), client.get("/app/app"), client.get(deep), client.post(deep)):
        assert (res.status_code, res.json()) == (404, {"detail": "Not Found"})
