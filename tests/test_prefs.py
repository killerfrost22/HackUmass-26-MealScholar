"""Preferences over HTTP: defaults, validation, partial updates and storage in the diary database."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mealscholar.config import load_settings
from mealscholar.prefs import GOAL_MAX, NutrientGoals, Prefs, PrefsStore, utc_now
from mealscholar.web import create_app

NOT_AN_OBJECT = {"detail": "Send the preferences as a JSON object."}
TOO_LARGE = {"detail": "That request is too large."}
FDA_DAILY_VALUES = {"protein_g": 50, "carbs_g": 275, "fat_g": 78, "fiber_g": 28, "sugars_g": None, "sodium_mg": 2300}
DEFAULTS = {
    "onboarded": False,
    "calorie_goal": None,
    "nutrient_goals": FDA_DAILY_VALUES,
    "show_ranges": False,
    "updated_at": None,
}


@pytest.fixture
def make_app(tmp_path):
    """App factory: environment variables as keyword arguments, everything kept under tmp_path."""

    def make(**env):
        return create_app(load_settings({"MEALSCHOLAR_HOME": str(tmp_path), **env}))

    return make


def browser(app) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def patch(client, body):
    return client.patch("/api/me/prefs", json=body)


# --- reading and updating -----------------------------------------------------


def test_defaults_until_anything_is_saved(make_app):
    client = browser(make_app())
    res = client.get("/api/me/prefs")
    assert res.status_code == 200
    assert res.json() == DEFAULTS
    assert res.headers["cache-control"] == "no-store"


def test_patch_changes_only_what_it_sends(make_app):
    client = browser(make_app())

    before = utc_now()
    res = patch(client, {"calorie_goal": 2200, "nutrient_goals": {"protein_g": 120, "sugars_g": 36.27}})
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    prefs = res.json()
    goals = {**FDA_DAILY_VALUES, "protein_g": 120, "sugars_g": 36.3}
    assert prefs == {**DEFAULTS, "calorie_goal": 2200, "nutrient_goals": goals, "updated_at": prefs["updated_at"]}
    assert before <= datetime.fromisoformat(prefs["updated_at"]) <= utc_now()

    # nutrient_goals merges into what is saved; null means no goal
    prefs = patch(client, {"onboarded": True, "nutrient_goals": {"sodium_mg": None}}).json()
    assert prefs["onboarded"] is True
    assert prefs["nutrient_goals"] == {**goals, "sodium_mg": None}
    assert prefs["calorie_goal"] == 2200
    assert client.get("/api/me/prefs").json() == prefs

    cleared = patch(client, {"calorie_goal": None}).json()
    assert cleared == {**prefs, "calorie_goal": None, "updated_at": cleared["updated_at"]}


def test_ranges_are_off_until_switched_on(make_app):
    """show_ranges only changes what the app shows; it is a plain flag that round-trips."""
    app = make_app()
    client = browser(app)
    store = app.state.prefs.store
    assert client.get("/api/me/prefs").json()["show_ranges"] is False

    on = patch(client, {"show_ranges": True})
    assert on.status_code == 200
    assert on.json()["show_ranges"] is True
    assert client.get("/api/me/prefs").json() == on.json()
    assert store.get() == {"show_ranges": True, "updated_at": store.get()["updated_at"]}

    # the other fields are untouched, and the flag goes back off on its own
    patch(client, {"calorie_goal": 2000})
    assert client.get("/api/me/prefs").json()["show_ranges"] is True
    assert patch(client, {"show_ranges": False}).json() == {
        **DEFAULTS,
        "calorie_goal": 2000,
        "updated_at": client.get("/api/me/prefs").json()["updated_at"],
    }


def test_what_get_returns_can_be_sent_back(make_app):
    """updated_at is the server's: a client may send it back, and it is ignored."""
    app = make_app()
    client = browser(app)

    assert patch(client, {}).json() == DEFAULTS  # nothing to change, nothing saved
    assert patch(client, {"updated_at": "2020-01-01T00:00:00Z"}).json() == DEFAULTS
    assert app.state.prefs.store.get() is None

    saved = patch(client, {"onboarded": True, "show_ranges": True, "nutrient_goals": {"fat_g": 70}}).json()
    again = patch(client, saved)
    assert again.status_code == 200
    assert again.json() == {**saved, "updated_at": again.json()["updated_at"]}
    assert again.json()["updated_at"] >= saved["updated_at"]


def test_prefs_survive_a_restart(make_app):
    """They live in the diary database beside the meals, so a new server on the same home has them."""
    saved = patch(browser(make_app()), {"onboarded": True, "calorie_goal": 1800, "nutrient_goals": {"fiber_g": 35}})
    assert saved.status_code == 200
    assert browser(make_app()).get("/api/me/prefs").json() == saved.json()


# --- validation ---------------------------------------------------------------

CALORIES = "Choose a calorie goal from 500 to 6,000, or none."


@pytest.mark.parametrize(
    ("body", "field", "detail"),
    [
        ({"onboarded": "yes"}, "onboarded", "Set onboarded to true or false."),
        ({"onboarded": 1}, "onboarded", "Set onboarded to true or false."),
        ({"onboarded": None}, "onboarded", "Set onboarded to true or false."),
        ({"calorie_goal": 499}, "calorie_goal", CALORIES),
        ({"calorie_goal": 6001}, "calorie_goal", CALORIES),
        ({"calorie_goal": 2000.5}, "calorie_goal", CALORIES),
        ({"calorie_goal": "2000"}, "calorie_goal", CALORIES),
        ({"calorie_goal": True}, "calorie_goal", CALORIES),
        ({"nutrient_goals": [50]}, "nutrient_goals", "Send nutrient_goals as an object."),
        ({"nutrient_goals": None}, "nutrient_goals", "Send nutrient_goals as an object."),
        (
            {"nutrient_goals": {"protein_g": -1}},
            "nutrient_goals.protein_g",
            "Choose a protein goal from 0 to 400 g, or none.",
        ),
        (
            {"nutrient_goals": {"carbs_g": 1000.1}},
            "nutrient_goals.carbs_g",
            "Choose a carbohydrate goal from 0 to 1,000 g, or none.",
        ),
        ({"nutrient_goals": {"fat_g": "lots"}}, "nutrient_goals.fat_g", "Choose a fat goal from 0 to 400 g, or none."),
        (
            {"nutrient_goals": {"fiber_g": True}},
            "nutrient_goals.fiber_g",
            "Choose a fiber goal from 0 to 150 g, or none.",
        ),
        (
            {"nutrient_goals": {"sugars_g": 401}},
            "nutrient_goals.sugars_g",
            "Choose a total sugars goal from 0 to 400 g, or none.",
        ),
        (
            {"nutrient_goals": {"sodium_mg": 10_001}},
            "nutrient_goals.sodium_mg",
            "Choose a sodium goal from 0 to 10,000 mg, or none.",
        ),
        (
            {"nutrient_goals": {"vitamin_c_mg": 90}},
            "nutrient_goals.vitamin_c_mg",
            "Unknown field: nutrient_goals.vitamin_c_mg.",
        ),
        ({"nutrient_goals": {"$gt": 1}}, "nutrient_goals", "Unknown field."),
        ({"show_ranges": "yes"}, "show_ranges", "Set show_ranges to true or false."),
        ({"show_ranges": 1}, "show_ranges", "Set show_ranges to true or false."),
        ({"show_ranges": None}, "show_ranges", "Set show_ranges to true or false."),
        ({"theme": "dark"}, "theme", "Unknown field: theme."),
        ({"email": "someone@example.com"}, "email", "Unknown field: email."),
        ({"$set": {"onboarded": True}}, None, "Unknown field."),  # odd names aren't repeated back
        ({"x" * 41: 1}, None, "Unknown field."),
    ],
)
def test_patch_says_which_field_is_wrong(make_app, body, field, detail):
    client = browser(make_app())
    res = patch(client, body)
    assert res.status_code == 400
    assert res.json() == ({"detail": detail, "field": field} if field else {"detail": detail})
    assert client.get("/api/me/prefs").json() == DEFAULTS


@pytest.mark.parametrize(
    "body",
    [
        {"dining_halls": [1]},
        {"diets": ["Halal"]},
        {"avoid_allergens": ["Milk"]},
        {"favorites": [{"name": "Pad Thai"}]},
        {"notify": {"email": True}},
    ],
)
def test_settings_of_the_removed_campus_and_account_features_are_unknown(make_app, body):
    """An app cached from before the change may still send them; they are refused, not kept."""
    client = browser(make_app())
    (name,) = body
    res = patch(client, {"onboarded": True, **body})
    assert (res.status_code, res.json()) == (400, {"detail": f"Unknown field: {name}.", "field": name})
    assert client.get("/api/me/prefs").json() == DEFAULTS


@pytest.mark.parametrize(
    "content",
    [
        b'{"nutrient_goals": {"protein_g": NaN}}',
        b'{"nutrient_goals": {"protein_g": Infinity}}',
        b'{"nutrient_goals": {"protein_g": 1e999}}',
    ],
)
def test_numbers_json_allows_but_prefs_dont(make_app, content):
    client = browser(make_app())
    res = client.patch("/api/me/prefs", content=content, headers={"Content-Type": "application/json"})
    assert res.status_code == 400
    assert res.json()["field"] == "nutrient_goals.protein_g"


def test_a_rejected_patch_saves_nothing(make_app):
    client = browser(make_app())
    saved = patch(client, {"calorie_goal": 2000, "show_ranges": True}).json()
    res = patch(client, {"calorie_goal": 2500, "nutrient_goals": {"protein_g": 100}, "show_ranges": "yes"})
    assert res.status_code == 400 and res.json()["field"] == "show_ranges"
    assert client.get("/api/me/prefs").json() == saved


@pytest.mark.parametrize(
    "content",
    [b"[]", b'"onboarded"', b"null", b"7", b"{", b"", b"\xff\xfe\xfd", b"[" * 5000 + b"]" * 5000],
)
def test_patch_needs_a_json_object(make_app, content):
    client = browser(make_app())
    res = client.patch("/api/me/prefs", content=content, headers={"Content-Type": "application/json"})
    assert res.status_code == 400
    assert res.json() == NOT_AN_OBJECT


def test_patch_bodies_are_capped(make_app):
    client = browser(make_app())
    parts = [b'{"nutrient_goals": {', b'"protein_g": 50, ' * 3000, b'"protein_g": 50}}']
    res = client.patch("/api/me/prefs", content=b"".join(parts), headers={"Content-Type": "application/json"})
    assert res.status_code == 413 and res.json() == TOO_LARGE
    chunks = iter(parts)  # sent without Content-Length
    res = client.patch("/api/me/prefs", content=chunks, headers={"Content-Type": "application/json"})
    assert res.status_code == 413 and res.json() == TOO_LARGE
    assert client.get("/api/me/prefs").json() == DEFAULTS


def test_goal_limits_cover_every_goal():
    assert set(GOAL_MAX) == set(NutrientGoals.model_fields)


def test_cross_site_patches_are_refused(make_app):
    client = browser(make_app())
    res = client.patch("/api/me/prefs", json={"onboarded": True}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert client.get("/api/me/prefs").json()["onboarded"] is False


# --- storage ------------------------------------------------------------------


def test_store_sets_dotted_paths_and_forgets(tmp_path):
    store = PrefsStore(tmp_path / "diary.sqlite")
    assert store.get() is None
    assert store.update({"onboarded": True, "nutrient_goals.protein_g": 90.0}) == {
        "onboarded": True,
        "nutrient_goals": {"protein_g": 90.0},
    }
    store.update({"nutrient_goals.sodium_mg": None, "show_ranges": True})

    store.get()["nutrient_goals"]["protein_g"] = 1.0  # callers get copies
    prefs = Prefs.model_validate(PrefsStore(tmp_path / "diary.sqlite").get())  # and so does a new store
    assert (prefs.onboarded, prefs.show_ranges) == (True, True)
    assert (prefs.nutrient_goals.protein_g, prefs.nutrient_goals.sodium_mg) == (90.0, None)
    assert prefs.nutrient_goals.carbs_g == 275  # unset goals keep their defaults

    store.delete()
    assert store.get() is None
