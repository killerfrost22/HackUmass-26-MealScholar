"""Step 7: meal log and the user's usual portions. Coordinates are never stored; a meal's photo is
kept beside the diary (photos.py) and deleted with the meal.

Rows carry a user_id column from when the web app had accounts; this diary is the local user's,
'', and rows that belonged to an account are left as they were and never read.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .compute import summarize
from .schemas import MealResult

_MEALS = """
CREATE TABLE IF NOT EXISTS meals (
    id         INTEGER PRIMARY KEY,
    user_id    TEXT NOT NULL DEFAULT '',
    local_date TEXT NOT NULL,
    meal_slot  TEXT NOT NULL,
    taken_at   TEXT NOT NULL,
    result     TEXT NOT NULL
);
"""
_USUAL_PORTIONS = """
CREATE TABLE IF NOT EXISTS usual_portions (
    user_id     TEXT    NOT NULL DEFAULT '',
    name        TEXT    NOT NULL,
    grams_total REAL    NOT NULL,
    corrections INTEGER NOT NULL,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, name)
);
"""
_MEALS_INDEX = "CREATE INDEX IF NOT EXISTS meals_by_user_date ON meals (user_id, local_date, taken_at);"


LOCAL_USER = ""


class Diary:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._user = LOCAL_USER
        self._con = sqlite3.connect(path)
        self._con.executescript(_MEALS + _USUAL_PORTIONS)
        _migrate(self._con)
        self._con.executescript(_MEALS_INDEX)

    def close(self) -> None:
        self._con.close()

    def save(self, result: MealResult) -> MealResult:
        ctx = result.context
        row = (ctx.taken_at.date().isoformat(), ctx.meal_slot, ctx.taken_at.isoformat())
        with self._con:
            if result.id is None:
                cur = self._con.execute(
                    "INSERT INTO meals (user_id, local_date, meal_slot, taken_at, result) VALUES (?, ?, ?, ?, '')",
                    (self._user, *row),
                )
                result = result.model_copy(update={"id": cur.lastrowid})
            self._con.execute(
                "UPDATE meals SET local_date = ?, meal_slot = ?, taken_at = ?, result = ? WHERE id = ? AND user_id = ?",
                (*row, result.model_dump_json(), result.id, self._user),
            )
        return result

    def get(self, meal_id: int) -> MealResult | None:
        row = self._con.execute(
            "SELECT result FROM meals WHERE id = ? AND user_id = ?", (meal_id, self._user)
        ).fetchone()
        return _meal(row[0]) if row else None

    def delete(self, meal_id: int) -> bool:
        """Remove one of this user's meals. False when they have no meal with that id. Usual portions
        learned from its corrections stay: they are kept as running totals, not per meal."""
        with self._con:
            cur = self._con.execute("DELETE FROM meals WHERE id = ? AND user_id = ?", (meal_id, self._user))
        return cur.rowcount > 0

    def clear(self) -> int:
        """Delete all of this user's meals and the portions learned from their corrections.
        Returns how many meals were deleted."""
        with self._con:
            cur = self._con.execute("DELETE FROM meals WHERE user_id = ?", (self._user,))
            self._con.execute("DELETE FROM usual_portions WHERE user_id = ?", (self._user,))
        return cur.rowcount

    def meals_between(self, first: date, last: date) -> list[MealResult]:
        rows = self._con.execute(
            "SELECT result FROM meals WHERE user_id = ? AND local_date BETWEEN ? AND ? ORDER BY local_date, taken_at",
            (self._user, first.isoformat(), last.isoformat()),
        ).fetchall()
        return [_meal(r[0]) for r in rows]

    def remember_portion(self, name: str, grams: int) -> None:
        with self._con:
            self._con.execute(
                """
                INSERT INTO usual_portions (user_id, name, grams_total, corrections) VALUES (?, ?, ?, 1)
                ON CONFLICT (user_id, name) DO UPDATE SET
                    grams_total = grams_total + excluded.grams_total,
                    corrections = corrections + 1,
                    updated_at  = datetime('now')
                """,
                (self._user, _portion_key(name), grams),
            )

    def usual_portions(self, limit: int = 20) -> dict[str, int]:
        rows = self._con.execute(
            """
            SELECT name, grams_total / corrections FROM usual_portions
            WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?
            """,
            (self._user, limit),
        ).fetchall()
        return {name: round(grams) for name, grams in rows}


def _meal(stored: str) -> MealResult:
    """Totals are recomputed from the items, so meals logged before a change to `compute` show
    the same totals as new ones."""
    return summarize(MealResult.model_validate_json(stored))


def _portion_key(name: str) -> str:
    return " ".join(name.lower().split())


def _migrate(con: sqlite3.Connection) -> None:
    """Upgrade a diary from before accounts: its meals and usual portions become the local user's."""
    if _has_user_id(con, "meals") and _has_user_id(con, "usual_portions"):
        return
    con.execute("BEGIN IMMEDIATE")  # one migrating connection at a time; check again under the lock
    try:
        if not _has_user_id(con, "meals"):
            con.execute("ALTER TABLE meals ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            con.execute("DROP INDEX IF EXISTS meals_by_date")
        if not _has_user_id(con, "usual_portions"):
            # The key changes from (name) to (user_id, name), which needs a new table.
            con.execute("ALTER TABLE usual_portions RENAME TO usual_portions_old")
            con.execute(_USUAL_PORTIONS)
            con.execute(
                """
                INSERT INTO usual_portions (user_id, name, grams_total, corrections, updated_at)
                SELECT '', name, grams_total, corrections, updated_at FROM usual_portions_old
                """
            )
            con.execute("DROP TABLE usual_portions_old")
        con.commit()
    except BaseException:
        con.rollback()
        raise


def _has_user_id(con: sqlite3.Connection, table: str) -> bool:
    return any(column[1] == "user_id" for column in con.execute(f"PRAGMA table_info({table})"))
