"""Library scanning, sidecar lookup and decision rules for photo triage.

Standard library only (numpy is used by find_whatsapp_copies when passed arrays),
so this module can be unit-tested without the image model installed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"}
SIDECAR_RE = re.compile(r"\.supplemental-metadata(\(\d+\))?\.json$", re.IGNORECASE)
WHATSAPP_NAME_RE = re.compile(r"^(IMG|VID|STK)-(\d{8})-WA\d+", re.IGNORECASE)
TRASH_RE = re.compile(r"(^|[\\/])Google Photos[\\/](Trash|Bin)([\\/]|$)", re.IGNORECASE)

# Categories the classifier can return. JUNK ones are suggested for removal.
CATEGORIES = ["photo", "screenshot", "meme", "advert", "greeting", "document", "news"]
JUNK = {"screenshot", "meme", "advert", "greeting", "news"}

# Common phone screen widths (portrait) in pixels; screenshots are exactly these.
PHONE_SCREEN_WIDTHS = {640, 720, 750, 828, 1080, 1125, 1170, 1179, 1242, 1284, 1290, 1440, 1536, 1620, 1644, 1668}


@dataclass
class ImageFile:
    path: str
    rel: str
    size: int
    mtime_ns: int
    sidecars: list[str] = field(default_factory=list)


def part_relative(rel: str) -> str:
    """'partA/Takeout/Google Photos/X.jpg' -> 'Takeout/Google Photos/X.jpg'."""
    rel = rel.replace("/", os.sep)
    cut = rel.find(os.sep)
    return rel if cut < 0 else rel[cut + 1:]


def sidecar_names(media_name: str) -> list[str]:
    """Sidecar file names that can hold a media file's metadata, best first."""
    stem, ext = os.path.splitext(media_name)
    names = [media_name + ".supplemental-metadata.json"]
    m = re.match(r"^(.+)\((\d+)\)$", stem)
    if m:
        names.append(f"{m.group(1)}{ext}.supplemental-metadata({m.group(2)}).json")
    m = re.match(r"^(.+)-edited$", stem)
    if m:
        names.append(f"{m.group(1)}{ext}.supplemental-metadata.json")
    return names


def scan_library(source_root: str) -> list[ImageFile]:
    """All images under source_root (Trash/Bin excluded), each with its sidecar paths."""
    # realpath expands 8.3 short names (C:\Users\ABC~1) so paths match PowerShell's.
    source_root = os.path.realpath(source_root)
    images: list[ImageFile] = []
    sidecars_full: set[str] = set()
    sidecars_by_part: dict[str, list[str]] = {}

    for dirpath, dirnames, filenames in os.walk(source_root):
        rel_dir = os.path.relpath(dirpath, source_root)
        if TRASH_RE.search(rel_dir if rel_dir != "." else ""):
            dirnames[:] = []
            continue
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, source_root)
            if SIDECAR_RE.search(name):
                sidecars_full.add(full.lower())
                sidecars_by_part.setdefault(part_relative(rel).lower(), []).append(full)
            elif os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                st = os.stat(full)
                if st.st_size > 0:
                    images.append(ImageFile(full, rel, st.st_size, st.st_mtime_ns))

    for img in images:
        folder = os.path.dirname(img.path)
        part_dir = os.path.dirname(part_relative(img.rel))
        found: list[str] = []
        for sc in sidecar_names(os.path.basename(img.path)):
            sibling = os.path.join(folder, sc)
            if sibling.lower() in sidecars_full and sibling not in found:
                found.append(sibling)
            for p in sidecars_by_part.get(os.path.join(part_dir, sc).lower(), []):
                if p not in found:
                    found.append(p)
        img.sidecars = found

    images.sort(key=lambda i: i.rel.lower())
    return images


def read_sidecars(paths: list[str]) -> dict:
    """Merged facts from Google sidecars: upload folder, tagged people, GPS, taken time."""
    info = {"upload_folder": "", "people": 0, "gps": False, "taken": ""}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        folder = (((data.get("googlePhotosOrigin") or {}).get("mobileUpload") or {})
                  .get("deviceFolder") or {}).get("localFolderName") or ""
        info["upload_folder"] = info["upload_folder"] or folder
        info["people"] = max(info["people"], len(data.get("people") or []))
        geo = data.get("geoData") or {}
        if abs(float(geo.get("latitude") or 0)) > 1e-6 or abs(float(geo.get("longitude") or 0)) > 1e-6:
            info["gps"] = True
        taken = (data.get("photoTakenTime") or {}).get("timestamp")
        if taken and not info["taken"]:
            info["taken"] = str(taken)
    return info


def whatsapp_date(name: str) -> str:
    """'IMG-20230505-WA0013.jpg' -> '2023-05-05', else ''."""
    m = WHATSAPP_NAME_RE.match(name)
    if not m:
        return ""
    d = m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def is_whatsapp(img_rel: str, upload_folder: str = "") -> bool:
    name = os.path.basename(img_rel)
    return bool(WHATSAPP_NAME_RE.match(name)) or "whatsapp" in img_rel.lower() or upload_folder.lower().startswith("whatsapp")


# Older 16:9 iPhones; other 16:9 sizes (1080x1920) are also ordinary camera photos.
PHONE_SCREEN_16_9 = {(640, 1136), (750, 1334), (1242, 2208)}
CAMERA_RATIOS = (4 / 3, 3 / 2, 16 / 9)


def phone_screen_like(width: int, height: int) -> bool:
    """True when the pixel size looks like a full phone screen capture."""
    if not width or not height:
        return False
    short, long_ = sorted((width, height))
    if (short, long_) in PHONE_SCREEN_16_9:
        return True
    return short in PHONE_SCREEN_WIDTHS and long_ / short >= 1.9


def camera_shaped(width: int, height: int) -> bool:
    """True for exact camera proportions (4:3, 3:2, 16:9), as WhatsApp keeps when it shrinks a photo.

    Memes and graphics come in arbitrary sizes; a photo of a child in a slogan T-shirt
    still has camera proportions, so this guards against rejecting it as a meme.
    """
    if not width or not height:
        return False
    short, long_ = sorted((width, height))
    return any(abs(long_ / short - r) <= 0.01 * r for r in CAMERA_RATIOS)


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def adjust_probabilities(probs: dict[str, float], feats: dict) -> tuple[dict[str, float], list[str]]:
    """Nudges model probabilities with file evidence; returns new probs and reasons."""
    p = dict(probs)
    reasons: list[str] = []
    camera = bool(feats.get("camera"))

    if phone_screen_like(feats.get("width", 0), feats.get("height", 0)) and not camera:
        p["screenshot"] = p.get("screenshot", 0) * 2.0
        reasons.append("phone-screen size, no camera data")
    if camera:
        p["photo"] = p.get("photo", 0) * 1.5
    if feats.get("format") == "PNG" and not camera:
        p["screenshot"] = p.get("screenshot", 0) * 1.3

    total = sum(p.values()) or 1.0
    return {k: v / total for k, v in p.items()}, reasons


def heuristic_category(feats: dict) -> tuple[str, float, list[str]]:
    """Category without the image model: only screenshots can be told from file evidence."""
    if phone_screen_like(feats.get("width", 0), feats.get("height", 0)) and not feats.get("camera"):
        return "screenshot", 0.7, ["phone-screen size, no camera data"]
    return "unknown", 0.0, ["no image model (--no-clip)"]


def protection_reasons(feats: dict, sidecar: dict) -> list[str]:
    """Evidence that an image is a real personal photo; such images are never auto-rejected."""
    out = []
    if sidecar.get("people"):
        out.append(f"{sidecar['people']} tagged people")
    if feats.get("camera"):
        out.append("camera data (" + feats["camera"] + ")")
    if feats.get("gps") or sidecar.get("gps"):
        out.append("GPS location")
    return out


def decide(category: str, confidence: float, protected: list[str], threshold: float = 0.75,
           photo_probability: float = 0.0, max_photo_probability: float = 0.15,
           camera_shape: bool = False) -> tuple[str, str]:
    """Returns (suggestion, note). suggestion: 'reject', 'review' (kept but flagged) or 'keep'.

    Removal is only suggested when the junk category is clear AND 'photo' is a distant
    alternative: on family libraries, photos of children wrongly score as meme or
    greeting far more often than real junk scores as photo.
    """
    if category == "unreadable":
        return "review", "could not be opened"
    if category in JUNK:
        if protected:
            return "review", "looks like " + category + " but has " + ", ".join(protected)
        if camera_shape and category != "screenshot":
            return "review", "looks like " + category + " but has camera photo proportions"
        if photo_probability > max_photo_probability:
            return "review", f"could also be a photo ({photo_probability:.0%})"
        if confidence >= threshold:
            return "reject", ""
        return "review", "low confidence"
    if category == "document":
        return "review", "documents may be important"
    return "keep", ""


def find_whatsapp_copies(wa_idx, orig_idx, phashes, sizes, max_distance: int = 6, embeddings=None, min_cosine: float = 0.92):
    """For each WhatsApp image, the closest-looking non-WhatsApp image.

    phashes: numpy uint64 array; sizes: list of (w, h); embeddings: optional
    numpy float array of unit vectors. Returns {wa_index: (orig_index, distance)}.
    """
    import numpy as np

    if len(wa_idx) == 0 or len(orig_idx) == 0:
        return {}

    orig = np.asarray(orig_idx)
    orig_hash = phashes[orig]
    orig_aspect = np.array([sizes[i][0] / max(sizes[i][1], 1) for i in orig_idx])
    # Popcount lookup for 8-bit chunks.
    pop = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

    result = {}
    for w in wa_idx:
        x = np.bitwise_xor(orig_hash, phashes[w])
        dist = pop[x.view(np.uint8).reshape(-1, 8)].sum(axis=1)
        cand = np.nonzero(dist <= max_distance)[0]
        if cand.size == 0:
            continue
        aspect = sizes[w][0] / max(sizes[w][1], 1)
        cand = cand[np.abs(orig_aspect[cand] - aspect) <= 0.03 * aspect]
        if embeddings is not None and cand.size:
            cos = embeddings[orig[cand]] @ embeddings[w]
            cand = cand[cos >= min_cosine]
        if cand.size == 0:
            continue
        # Closest first, then the largest original.
        best = min(cand, key=lambda c: (int(dist[c]), -sizes[orig[c]][0] * sizes[orig[c]][1]))
        result[int(w)] = (int(orig[best]), int(dist[best]))
    return result
