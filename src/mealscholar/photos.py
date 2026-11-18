"""Meal photos, kept with the diary so the app can show them, and deleted with their meals.

A logged meal's photo is stored as two JPEGs in `photos/local/` under the data folder:
`<meal id>.jpg`, 1280 px on the long edge, and `<meal id>-thumb.jpg`, 320 px. They are re-encoded
from the pixels alone: upright, converted to sRGB, and without EXIF (so no GPS or camera details),
XMP, comments or color profile. The upload as sent is never written to disk.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from PIL import Image, ImageCms, ImageOps
from pillow_heif import register_heif_opener

FULL_SIDE = 1280
THUMB_SIDE = 320
FULL_QUALITY = 82
THUMB_QUALITY = 80

# iPhones save HEIC by default (as in metadata.py).
register_heif_opener()

_SRGB = ImageCms.createProfile("sRGB")

log = logging.getLogger(__name__)


def encode(photo: bytes) -> tuple[bytes, bytes]:
    """The full-size and thumbnail JPEGs of `photo`: upright, in sRGB, and carrying no metadata."""
    with Image.open(io.BytesIO(photo)) as img:
        img.draft(None, (FULL_SIDE, FULL_SIDE))  # a JPEG decodes at a reduced scale, still >= 1280 px
        img.thumbnail((FULL_SIDE, FULL_SIDE))
        full = _srgb(ImageOps.exif_transpose(img))
    thumb = full.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    return _jpeg(full, FULL_QUALITY, progressive=True), _jpeg(thumb, THUMB_QUALITY)


class PhotoStore:
    """The photos of the diary's meals, in `root`/local (the folder name dates from accounts)."""

    def __init__(self, root: Path) -> None:
        self.folder = root / "local"

    def path(self, meal_id: int, *, thumb: bool = False) -> Path:
        return self.folder / f"{meal_id}{'-thumb' if thumb else ''}.jpg"

    def save(self, meal_id: int, photo: bytes) -> str | None:
        """Store the photo of meal `meal_id`. Returns its version, a short hash of the stored JPEG,
        or None when it couldn't be stored: that is logged and never fails the meal."""
        try:
            full, thumb = encode(photo)
            self.folder.mkdir(parents=True, exist_ok=True)
            _write(self.path(meal_id), full)
            _write(self.path(meal_id, thumb=True), thumb)
        except Exception as exc:
            log.warning("Couldn't store the photo of meal #%d: %s: %s", meal_id, type(exc).__name__, exc)
            with suppress(OSError):  # no half-stored photo, nor one left from an older meal with this id
                self.delete(meal_id)
            return None
        return hashlib.sha256(full).hexdigest()[:12]

    def delete(self, meal_id: int) -> None:
        """Delete the photo of meal `meal_id`, if there is one."""
        for thumb in (False, True):
            self.path(meal_id, thumb=thumb).unlink(missing_ok=True)

    def clear(self) -> None:
        """Delete the photos of all the meals."""
        with suppress(FileNotFoundError):
            shutil.rmtree(self.folder)


def _srgb(img: Image.Image) -> Image.Image:
    """The pixels in sRGB with nothing else: phones often shoot in a wider color space (Display P3),
    which would look washed out once its profile is left out."""
    if icc := img.info.get("icc_profile"):
        try:
            img = ImageCms.profileToProfile(img, io.BytesIO(icc), _SRGB, outputMode="RGB") or img
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass  # a profile Little CMS can't read: keep the pixels as they are
    clean = img.convert("RGB")
    clean.info = {}  # the JPEG encoder would copy a comment from here
    return clean


def _jpeg(img: Image.Image, quality: int, *, progressive: bool = False) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=progressive)
    return buf.getvalue()


def _write(path: Path, data: bytes) -> None:
    """Write `data` to a temporary file beside `path`, then move it into place, so a reader sees the
    old photo or the new one and never half of one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
