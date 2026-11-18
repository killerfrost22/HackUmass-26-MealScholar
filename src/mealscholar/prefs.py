"""Preferences: daily goals, whether ranges are shown, and whether onboarding is done.

One JSON document in the `prefs` table of the diary database (settings.diary_db), beside the
meals. `install_prefs` adds GET and PATCH /api/me/prefs.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .nutrients import BY_KEY

MIN_CALORIE_GOAL, MAX_CALORIE_GOAL = 500, 6000
GOAL_MAX: dict[str, int] = {
    "protein_g": 400,
    "carbs_g": 1000,
    "fat_g": 400,
    "fiber_g": 150,
    "sugars_g": 400,
    "sodium_mg": 10_000,
}
"""The highest daily goal for each nutrient. The lowest is 0, and null means no goal."""
MAX_PREFS_BODY = 32 * 1024
"""Bytes. The preferences fit many times over; bigger bodies are refused before they are parsed."""

NOT_AN_OBJECT = "Send the preferences as a JSON object."
BODY_TOO_LARGE = "That request is too large."
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_]{1,40}")


# --- the preferences ----------------------------------------------------------


class NutrientGoals(BaseModel):
    """Daily goals. The defaults are the FDA Daily Values; sugars have none. None: no goal."""

    protein_g: float | None = BY_KEY["protein_g"].daily_value
    carbs_g: float | None = BY_KEY["carbs_g"].daily_value
    fat_g: float | None = BY_KEY["fat_g"].daily_value
    fiber_g: float | None = BY_KEY["fiber_g"].daily_value
    sugars_g: float | None = BY_KEY["sugars_g"].daily_value
    sodium_mg: float | None = BY_KEY["sodium_mg"].daily_value


class Prefs(BaseModel):
    onboarded: bool = False
    calorie_goal: int | None = None
    nutrient_goals: NutrientGoals = Field(default_factory=NutrientGoals)
    show_ranges: bool = False
    """Show the low–high range next to each likely amount. Off, the app prints the likely value alone;
    the server computes the ranges either way."""
    updated_at: datetime | None = None
    """The last change; None until anything is saved."""


class PrefsError(Exception):
    """An answer for the client: HTTP status, a plain message, and the field it is about."""

    def __init__(self, status: int, detail: str, *, field: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.field = field


# --- checking a PATCH body ----------------------------------------------------


def validate_changes(body: Any) -> dict[str, Any]:
    """The changes in a PATCH body: whole fields, and dotted paths for the members of
    nutrient_goals, which merge into the saved ones. Raises PrefsError (400, naming the field) at
    the first thing that is wrong."""
    if not isinstance(body, dict):
        raise PrefsError(400, NOT_AN_OBJECT)
    changes: dict[str, Any] = {}
    for field, value in body.items():
        match field:
            case "onboarded" | "show_ranges":
                changes[field] = _flag(value, field)
            case "calorie_goal":
                changes[field] = _calorie_goal(value)
            case "nutrient_goals":
                changes.update(_nutrient_goals(value))
            case "updated_at":
                pass  # set by the server; accepted so a client can send back what it read
            case _:
                raise _unknown(field)
    return changes


def _bad(detail: str, field: str | None) -> PrefsError:
    return PrefsError(400, detail, field=field)


def _unknown(name: str, parent: str | None = None) -> PrefsError:
    """The client chose the name, so it is repeated back only when it is plain."""
    path = f"{parent}.{name}" if parent else name
    if _PLAIN_NAME.fullmatch(name):
        return _bad(f"Unknown field: {path}.", path)
    return _bad("Unknown field.", parent)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)  # JSON true is not a number


def _flag(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"Set {field} to true or false.", field)
    return value


def _calorie_goal(value: Any) -> int | None:
    if value is not None and not (_whole(value) and MIN_CALORIE_GOAL <= value <= MAX_CALORIE_GOAL):
        raise _bad(f"Choose a calorie goal from {MIN_CALORIE_GOAL} to {MAX_CALORIE_GOAL:,}, or none.", "calorie_goal")
    return value


def _nutrient_goals(value: Any) -> dict[str, float | None]:
    if not isinstance(value, dict):
        raise _bad("Send nutrient_goals as an object.", "nutrient_goals")
    changes: dict[str, float | None] = {}
    for key, goal in value.items():
        if key not in GOAL_MAX:
            raise _unknown(key, "nutrient_goals")
        field = f"nutrient_goals.{key}"
        if goal is not None:
            # NaN and infinity fail the range check too
            if isinstance(goal, bool) or not isinstance(goal, int | float) or not 0 <= goal <= GOAL_MAX[key]:
                nutrient = BY_KEY[key]
                raise _bad(
                    f"Choose a {nutrient.label.lower()} goal from 0 to {GOAL_MAX[key]:,} {nutrient.unit}, or none.",
                    field,
                )
            goal = round(float(goal), 1)
        changes[field] = goal
    return changes


# --- storage ------------------------------------------------------------------

_PREFS = """
CREATE TABLE IF NOT EXISTS prefs (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    doc TEXT    NOT NULL
);
"""


class PrefsStore:
    """The one saved document, as JSON in the diary database. A connection per call, as FastAPI
    runs the handlers on a thread pool."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.executescript(_PREFS)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None)  # transactions are explicit

    def get(self) -> dict[str, Any] | None:
        """The saved document, or None if nothing was ever saved."""
        with closing(self._connect()) as con:
            row = con.execute("SELECT doc FROM prefs WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Set fields (dotted paths reach into nutrient_goals), creating the document if there is
        none, and return it as saved."""
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")  # read and write under one lock, so no change is lost
            try:
                row = con.execute("SELECT doc FROM prefs WHERE id = 1").fetchone()
                doc = json.loads(row[0]) if row else {}
                for path, value in changes.items():
                    *parents, name = path.split(".")
                    target = doc
                    for parent in parents:
                        target = target.setdefault(parent, {})
                    target[name] = value
                con.execute("INSERT OR REPLACE INTO prefs (id, doc) VALUES (1, ?)", (json.dumps(doc),))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return doc

    def delete(self) -> None:
        """Forget the preferences; get() then returns None."""
        with closing(self._connect()) as con:
            con.execute("DELETE FROM prefs")


# --- service and HTTP ---------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


class PrefsService:
    def __init__(self, store: PrefsStore, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.store = store
        self._clock = clock

    def get(self) -> Prefs:
        return Prefs.model_validate(self.store.get() or {})

    def update(self, body: Any) -> Prefs:
        """Apply a PATCH body. If any part of it is wrong, raises PrefsError and saves nothing."""
        changes = validate_changes(body)
        if not changes:
            return self.get()
        return Prefs.model_validate(self.store.update({**changes, "updated_at": self._clock().isoformat()}))


def no_store(response: Response) -> None:
    """/api/me answers are personal: no browser or proxy cache keeps them."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/me", tags=["me"], dependencies=[Depends(no_store)])


def _service(request: Request) -> PrefsService:
    return request.app.state.prefs


Service = Annotated[PrefsService, Depends(_service)]


@router.get("/prefs")
def read_prefs(prefs: Service) -> Prefs:
    """The saved preferences, with defaults for anything not set yet (then updated_at is null)."""
    return prefs.get()


_ANY_PREFS = {"$ref": "#/components/schemas/Prefs"}  # for /api/docs: every field is optional


@router.patch("/prefs", openapi_extra={"requestBody": {"content": {"application/json": {"schema": _ANY_PREFS}}}})
async def update_prefs(request: Request, prefs: Service) -> Prefs:
    """Body: any subset of the fields. nutrient_goals merges into the saved goals. Anything invalid
    is a 400 naming the field, and nothing is saved."""
    body = await _json_body(request)
    return await run_in_threadpool(prefs.update, body)


async def _json_body(request: Request) -> Any:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_PREFS_BODY:
        raise PrefsError(413, BODY_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_PREFS_BODY:
            raise PrefsError(413, BODY_TOO_LARGE)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested absurdly deep
        raise PrefsError(400, NOT_AN_OBJECT) from None


async def _prefs_error(request: Request, exc: PrefsError) -> JSONResponse:
    content = {"detail": exc.detail}
    if exc.field:
        content["field"] = exc.field
    return JSONResponse(content, status_code=exc.status)


def install_prefs(app: FastAPI, store: PrefsStore) -> None:
    """Add GET and PATCH /api/me/prefs."""
    app.state.prefs = PrefsService(store)
    app.include_router(router)
    app.add_exception_handler(PrefsError, _prefs_error)
