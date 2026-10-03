"""Per-image facts: size, camera EXIF, perceptual hash, thumbnail. Needs Pillow + numpy."""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageOps

try:  # HEIC support is optional.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

Image.MAX_IMAGE_PIXELS = 200_000_000

EXIF_MAKE, EXIF_MODEL, EXIF_SOFTWARE = 0x010F, 0x0110, 0x0131
EXIF_IFD, GPS_IFD = 0x8769, 0x8825
EXIF_DATETIME_ORIGINAL = 0x9003

_DCT_N = 32
_DCT = np.array([[np.cos(np.pi * (2 * n + 1) * k / (2 * _DCT_N)) for n in range(_DCT_N)] for k in range(_DCT_N)])


def perceptual_hash(img: Image.Image) -> int:
    """64-bit DCT pHash: robust to resizing and recompression, not to cropping."""
    small = np.asarray(img.convert("L").resize((_DCT_N, _DCT_N), Image.LANCZOS), dtype=np.float64)
    low = (_DCT @ small @ _DCT.T)[:8, :8].flatten()
    bits = low > np.median(low[1:])
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return value


def flatness(img: Image.Image) -> float:
    """Share of pixels in the 4 most common coarse colours (high for graphics and screenshots)."""
    a = np.asarray(img.convert("RGB").resize((64, 64)), dtype=np.uint8) >> 4
    keys = (a[..., 0].astype(np.int32) << 8) | (a[..., 1].astype(np.int32) << 4) | a[..., 2]
    counts = np.bincount(keys.ravel(), minlength=4096)
    return float(np.sort(counts)[-4:].sum() / keys.size)


def read_exif(img: Image.Image) -> dict:
    out = {"camera": "", "gps": False, "software": "", "taken": ""}
    try:
        exif = img.getexif()
    except Exception:
        return out
    make = str(exif.get(EXIF_MAKE) or "").strip("\x00 ").strip()
    model = str(exif.get(EXIF_MODEL) or "").strip("\x00 ").strip()
    out["camera"] = (model if make and model.lower().startswith(make.lower()) else f"{make} {model}").strip()
    out["software"] = str(exif.get(EXIF_SOFTWARE) or "").strip("\x00 ").strip()
    try:
        gps = exif.get_ifd(GPS_IFD)
        out["gps"] = bool(gps) and any(k in gps for k in (2, 4))
        out["taken"] = str(exif.get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL) or "")
    except Exception:
        pass
    return out


def extract(path: str, thumb_path: str, thumb_size: int = 320) -> tuple[dict, Image.Image | None]:
    """Returns (facts, small RGB image for the model). facts['error'] is set when unreadable."""
    facts = {"width": 0, "height": 0, "format": "", "camera": "", "gps": False, "software": "",
             "taken": "", "phash": "", "flatness": 0.0, "error": ""}
    try:
        with Image.open(path) as im:
            facts["format"] = im.format or ""
            facts.update(read_exif(im))
            # True size as displayed (EXIF orientations 5-8 rotate by 90 degrees).
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
            facts["width"], facts["height"] = w, h
            # Fast reduced-size JPEG decode; full size is never needed.
            im.draft("RGB", (1024, 1024))
            rgb = ImageOps.exif_transpose(im).convert("RGB")
            rgb.thumbnail((512, 512))
    except Exception as e:  # noqa: BLE001 - any decode problem means "review by hand"
        facts["error"] = f"{type(e).__name__}: {e}"[:200]
        return facts, None

    facts["phash"] = format(perceptual_hash(rgb), "016x")
    facts["flatness"] = round(flatness(rgb), 3)

    if not os.path.exists(thumb_path):
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        t = rgb.copy()
        t.thumbnail((thumb_size, thumb_size))
        t.save(thumb_path, "JPEG", quality=80)

    return facts, rgb
