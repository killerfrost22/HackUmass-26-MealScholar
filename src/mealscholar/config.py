"""Settings from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "gemini-4-hydrogen"
DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
DEFAULT_PUBLIC_URL = "http://127.0.0.1:8000"
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
"""Gemini thinking levels the installed SDK accepts (google.genai.interactions.ThinkingLevel)."""
USER_AGENT = "MealScholar/0.1 (HackUMass prototype; https://github.com/hershey-harsh/HackUmass-26-MealScholar)"


@dataclass(frozen=True, slots=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    thinking_level: str
    home: Path
    nominatim_url: str
    public_url: str = DEFAULT_PUBLIC_URL
    """Where the app is reached, without a trailing slash: its host also passes the cross-site
    write check, and an https:// URL turns off the interactive API docs."""

    @property
    def food_db(self) -> Path:
        return self.home / "fdc.sqlite"

    @property
    def diary_db(self) -> Path:
        """Meals, learned portions and preferences (diary.py, prefs.py)."""
        return self.home / "diary.sqlite"

    @property
    def downloads(self) -> Path:
        return self.home / "downloads"

    @property
    def photos(self) -> Path:
        """Logged meals' photos (photos.py)."""
        return self.home / "photos"


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    return Settings(
        gemini_api_key=env.get("GEMINI_API_KEY") or None,
        gemini_model=env.get("MEALSCHOLAR_MODEL", DEFAULT_MODEL),
        thinking_level=_thinking_level(env.get("MEALSCHOLAR_THINKING") or "low"),
        home=Path(env.get("MEALSCHOLAR_HOME", "~/.mealscholar")).expanduser(),
        nominatim_url=env.get("MEALSCHOLAR_NOMINATIM_URL", DEFAULT_NOMINATIM_URL),
        public_url=_public_url(env.get("MEALSCHOLAR_PUBLIC_URL") or DEFAULT_PUBLIC_URL),
    )


def _public_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"MEALSCHOLAR_PUBLIC_URL must start with http:// or https://, not {value!r}.")
    return url


def _thinking_level(value: str) -> str:
    level = value.strip().lower()
    if level not in THINKING_LEVELS:
        raise ValueError(f"MEALSCHOLAR_THINKING must be one of {', '.join(THINKING_LEVELS)}, not {value!r}.")
    return level
