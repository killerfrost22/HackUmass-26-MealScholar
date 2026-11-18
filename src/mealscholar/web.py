"""Web front end: a JSON API over the workflow, plus the static site in `site/`.

One diary, no sign-in: whoever can reach the server uses it. Photos arrive as the raw request body
and are read in memory. A logged meal keeps a re-encoded copy of its photo without metadata
(photos.py), deleted with the meal; the upload itself is never saved.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import USER_AGENT, Settings
from .context import NominatimPlaces, PlaceLookup
from .diary import Diary
from .insights import HealthScore, health_score, summarize_days
from .metadata import MAX_PHOTO_PIXELS
from .nutrients import NUTRIENTS
from .nutrition import FoodDatabase, FoodDatabaseMissing
from .photos import PhotoStore
from .pipeline import UnknownFood, Workflow, correct
from .prefs import PrefsStore, install_prefs
from .schemas import MealResult, Range
from .security import (
    Client,
    RateLimiter,
    RequestClient,
    Rule,
    SameOriginWrites,
    hide_coordinates_in_access_log,
    route_path,
)
from .vision import GeminiVision, VisionError, VisionModel

SITE = Path(__file__).parent / "site"
MAX_PHOTO_BYTES = 25 * 1024 * 1024
ANALYSIS_PER_IP = Rule("analysis-ip", 100, 10 * 60)  # photos and answers together; one Wi-Fi can share an address
PHOTO_CACHE = "private, max-age=86400"
PAGE_TYPES = ("text/html", "application/manifest+json")
JPEG_REPLY: dict[int | str, dict] = {200: {"content": {"image/jpeg": {}}, "description": "The photo."}}

log = logging.getLogger(__name__)


class Status(BaseModel):
    gemini_ready: bool
    food_db_ready: bool


class NutrientInfo(BaseModel):
    key: str
    label: str
    unit: str
    daily_value: float | None
    micro: bool


class FoodOption(BaseModel):
    fdc_id: int
    description: str
    data_type: str
    energy_kcal_per_100g: float | None


class FoodSearch(BaseModel):
    foods: list[FoodOption]
    exact: bool
    """False when no food contains every word of the main food; results are only suggestions."""


class Answer(BaseModel):
    answer: str = Field(min_length=1, max_length=500)


class Correction(BaseModel):
    grams: int | None = Field(default=None, gt=0, le=10_000)
    fdc_id: int | None = None


class Erased(BaseModel):
    meals: int = Field(description="How many meals were deleted.")


class DayView(BaseModel):
    day: date
    meals: list[MealResult]
    totals: dict[str, Range]
    daily_value_pct: dict[str, Range]
    health: HealthScore | None = None
    """The day's nutrient-balance score (insights.health_score); None for a day with no meals."""


def _limit_analysis(limiter: RateLimiter, client: Client) -> None:
    """Photos and clarifying answers share one budget per network."""
    wait = limiter.hit((ANALYSIS_PER_IP, client.network))
    if wait is not None:
        unit = "second" if wait == 1 else "seconds"
        raise HTTPException(
            429, f"Too many analysis requests. Try again in {wait} {unit}.", headers={"Retry-After": str(wait)}
        )


class _ResponseSecurity:
    """Protect the app's pages and API replies without restricting capture or inline scripts."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_protected(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["X-Frame-Options"] = "DENY"
                # The page and the manifest are checked with the server on every load (a 304 while
                # unchanged), so a new deploy reaches phones at once; ?v= assets keep their caching.
                if "cache-control" not in headers and headers.get("content-type", "").startswith(PAGE_TYPES):
                    headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_protected)


def create_app(
    settings: Settings,
    *,
    vision: VisionModel | None = None,
    places: PlaceLookup | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    """`vision` and `places` default to Gemini (when a key is set) and OpenStreetMap Nominatim."""
    if vision is None and settings.gemini_api_key:
        vision = GeminiVision(settings.gemini_api_key, settings.gemini_model, settings.thinking_level)
    if places is None:
        places = NominatimPlaces(settings.nominatim_url, USER_AGENT)
    limiter = limiter or RateLimiter()

    # The interactive API docs are for local development (an http:// public URL); a public server has none.
    local = settings.public_url.startswith("http://")
    app = FastAPI(
        title="MealScholar",
        docs_url="/api/docs" if local else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if local else None,
    )
    prefs = PrefsStore(settings.diary_db)
    install_prefs(app, prefs)
    app.add_middleware(SameOriginWrites, public_host=urlsplit(settings.public_url).netloc)
    app.add_middleware(_ResponseSecurity)
    hide_coordinates_in_access_log()
    photos = PhotoStore(settings.photos)

    # SQLite connections are opened per request: FastAPI runs sync handlers on a thread pool.
    @contextmanager
    def stores() -> Iterator[tuple[FoodDatabase, Diary]]:
        with closing(FoodDatabase(settings.food_db)) as foods, closing(Diary(settings.diary_db)) as diary:
            yield foods, diary

    def workflow(foods: FoodDatabase) -> Workflow:
        if vision is None:
            raise HTTPException(503, "Set GEMINI_API_KEY in .env, then restart `mealscholar serve`.")
        return Workflow(vision=vision, foods=foods, places=places)

    def logged(diary: Diary, meal_id: int) -> MealResult:
        result = diary.get(meal_id)
        if result is None:
            raise HTTPException(404, f"No meal #{meal_id}.")
        return result

    def error(status: int, reply: str | None = None):
        """`reply` replaces the exception's text in the response when that text can hold server
        paths or a provider's raw error; the full text goes to the server log instead."""

        async def handler(request: Request, exc: Exception) -> JSONResponse:
            if reply is None:
                return JSONResponse({"detail": str(exc)}, status_code=status)
            # The path only: the query can carry the device location.
            log.warning("%s %s answered %d: %s", request.method, request.url.path, status, exc)
            return JSONResponse({"detail": reply}, status_code=status)

        return handler

    app.add_exception_handler(FoodDatabaseMissing, error(503, "The food database isn't set up on the server yet."))
    app.add_exception_handler(VisionError, error(502, "Couldn't analyze the photo right now. Try again in a moment."))
    app.add_exception_handler(UnknownFood, error(404))

    @app.get("/api/status")
    def status() -> Status:
        return Status(gemini_ready=vision is not None, food_db_ready=settings.food_db.exists())

    @app.get("/api/nutrients")
    def nutrients() -> list[NutrientInfo]:
        return [
            NutrientInfo(key=n.key, label=n.label, unit=n.unit, daily_value=n.daily_value, micro=n.micro)
            for n in NUTRIENTS
        ]

    @app.post("/api/meals")
    async def analyze(
        request: Request,
        client: RequestClient,
        lat: float | None = None,
        lon: float | None = None,
        taken_at: datetime | None = None,
    ) -> MealResult:
        """Body: the photo bytes (JPEG, PNG, HEIC). `lat`/`lon`: device location, opt-in, used only
        to name the food place. `taken_at`: the phone's local time at capture with its UTC offset,
        ISO 8601 such as 2026-11-14T12:30:00-05:00 (URL-encode a '+'; an unencoded '+' is read as
        a space and answers 422). Used only when the photo carries no EXIF time;
        `context.time_source` is then 'device'. Send local time with its offset, not UTC: a 'Z' or
        +00:00 time is accepted but the diary files the meal under that clock's date, so an evening
        meal sent as UTC lands on the next day."""
        _limit_analysis(limiter, client)
        if (lat is None) != (lon is None):
            raise HTTPException(400, "lat and lon go together.")
        if taken_at is not None and taken_at.tzinfo is None:
            raise HTTPException(400, "taken_at needs a UTC offset, such as 2026-11-14T12:30:00-05:00.")
        photo = bytearray()
        async for chunk in request.stream():
            photo += chunk
            if len(photo) > MAX_PHOTO_BYTES:
                raise HTTPException(413, f"Photos are limited to {MAX_PHOTO_BYTES // 2**20} MB.")
        if not photo:
            raise HTTPException(400, "Send the photo as the request body.")
        location = (lat, lon) if lat is not None and lon is not None else None
        return await run_in_threadpool(_analyze, bytes(photo), location, taken_at)

    def _analyze(photo: bytes, location: tuple[float, float] | None, taken_at: datetime | None) -> MealResult:
        with stores() as (foods, diary):
            try:
                result = workflow(foods).analyze(
                    io.BytesIO(photo),
                    device_time=taken_at,
                    device_location=location,
                    usual_portions=diary.usual_portions(),
                )
            # Over 50 MP (metadata.load_image). Pillow's own warning from about 89 MP comes first, in
            # Image.open, and is raised where warnings are errors (the server's PYTHONWARNINGS).
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                limit = MAX_PHOTO_PIXELS // 1_000_000
                reply = f"Photos are limited to {limit} megapixels. Choose a lower-resolution photo."
                raise HTTPException(413, reply) from exc
            # Not an image, or cut off (UnidentifiedImageError is an OSError too). In this block only the
            # in-memory photo raises OSError: Gemini, place and database failures have their own types.
            except OSError as exc:
                raise HTTPException(415, "Couldn't read that image. Use a JPEG, PNG or HEIC photo.") from exc
            if not result.items:
                return result
            result = diary.save(result)
            # The photo is kept only for a logged meal, and storing it can't fail the analysis.
            if (version := photos.save(result.id, photo)) is not None:
                url = f"/api/meals/{result.id}/photo?v={version}"
                result = diary.save(result.model_copy(update={"photo_url": url}))
            return result

    @app.get("/api/meals/{meal_id}")
    def show(meal_id: int) -> MealResult:
        with closing(Diary(settings.diary_db)) as diary:
            return logged(diary, meal_id)

    @app.get("/api/meals/{meal_id}/photo", response_class=FileResponse, responses=JPEG_REPLY)
    def meal_photo(meal_id: int, size: Literal["full", "thumb"] = "full") -> FileResponse:
        """The meal's stored photo as a JPEG: 1280 px on the long edge, or 320 px for `thumb`. Link
        to it through the meal's `photo_url`, whose version changes with the photo (meal ids can be
        reused), so a cached copy never shows the wrong one."""
        with closing(Diary(settings.diary_db)) as diary:
            result = logged(diary, meal_id)
        path = photos.path(meal_id, thumb=size == "thumb")
        if result.photo_url is None or not path.is_file():
            raise HTTPException(404, f"Meal #{meal_id} has no photo.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": PHOTO_CACHE})

    @app.delete("/api/meals/{meal_id}", status_code=204)
    def delete(meal_id: int) -> None:
        """Remove a meal and its photo from the diary, such as one logged from the wrong photo."""
        with closing(Diary(settings.diary_db)) as diary:
            logged(diary, meal_id)
            photos.delete(meal_id)  # first, so a failure leaves the meal to delete again
            diary.delete(meal_id)

    @app.delete("/api/meals")
    def clear_history() -> Erased:
        """Delete every meal in the diary with its photo, and the portions learned from corrections;
        goals and settings stay."""
        photos.clear()
        with closing(Diary(settings.diary_db)) as diary:
            return Erased(meals=diary.clear())

    @app.delete("/api/me")
    def erase_me() -> Erased:
        """Delete everything kept: meals and their photos, learned portions, goals and settings, so
        the app starts over."""
        photos.clear()
        with closing(Diary(settings.diary_db)) as diary:
            meals = diary.clear()
        prefs.delete()
        return Erased(meals=meals)

    @app.post("/api/meals/{meal_id}/answer")
    def answer(meal_id: int, body: Answer, client: RequestClient) -> MealResult:
        """Answer the clarifying question: Gemini revises the analysis and everything is recomputed."""
        _limit_analysis(limiter, client)
        with stores() as (foods, diary):
            result = logged(diary, meal_id)
            if not result.question:
                raise HTTPException(409, "This meal has no open question.")
            return diary.save(workflow(foods).answer(result, body.answer.strip()))

    @app.patch("/api/meals/{meal_id}/items/{index}")
    def fix(meal_id: int, index: int, body: Correction) -> MealResult:
        """Exact grams collapse the item's range and become a usual portion; fdc_id swaps the match."""
        if body.grams is None and body.fdc_id is None:
            raise HTTPException(400, "Send grams and/or fdc_id.")
        with stores() as (foods, diary):
            result = logged(diary, meal_id)
            if not 0 <= index < len(result.items):
                raise HTTPException(404, f"Meal #{meal_id} has no item {index}.")
            result = diary.save(correct(result, index, foods, grams=body.grams, fdc_id=body.fdc_id))
            if body.grams is not None:
                diary.remember_portion(result.items[index].item.name, body.grams)
            return result

    @app.get("/api/foods")
    def search(q: str = Query(min_length=1, max_length=200), limit: int = Query(8, ge=1, le=25)) -> FoodSearch:
        with closing(FoodDatabase(settings.food_db)) as foods:
            found = foods.search(q, limit=limit)
        return FoodSearch(
            foods=[
                FoodOption(
                    fdc_id=f.fdc_id,
                    description=f.description,
                    data_type=f.data_type,
                    energy_kcal_per_100g=f.per_100g.get("energy_kcal"),
                )
                for f in found.foods
            ],
            exact=found.exact,
        )

    @app.get("/api/days/{day}")
    def day(day: date) -> DayView:
        """Meals and totals for one local date. Information only: no targets."""
        with closing(Diary(settings.diary_db)) as diary:
            summaries = summarize_days(diary.meals_between(day, day))
        if not summaries:
            return DayView(day=day, meals=[], totals={}, daily_value_pct={})
        s = summaries[0]
        return DayView(
            day=s.day,
            meals=s.meals,
            totals=s.totals,
            daily_value_pct=s.daily_value_pct,
            health=health_score(s.totals, s.daily_value_pct),
        )

    # The same app also answers under /app: the landing's domain serves it there (the tunnel sends
    # mealscholar.health/app/* here), so an icon added to a phone's Home Screen from the landing
    # opens the app on the same origin, without the browser's toolbar. The site's own paths are
    # relative, and app.js resolves server paths against where the page was loaded from.
    @app.get("/app", include_in_schema=False)
    def app_slash() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=308)

    # One level only: under /app, a path that starts with /app again is not found. Mounted in itself
    # with no such stop, every /app in a path nested the whole app once more, until a long one ran out
    # of stack (a 500 and a long traceback in the log).
    async def app_once(scope: Scope, receive: Receive, send: Send) -> None:
        path = route_path(scope)
        if path == "/app" or path.startswith("/app/"):
            raise HTTPException(404)
        await app(scope, receive, send)

    app.mount("/app", app_once, name="app-under-landing")
    app.mount("/", StaticFiles(directory=SITE, html=True), name="site")
    return app
