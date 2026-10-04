"""Find photos that are the same picture even when rotated, recompressed or resized.

Read-only. For every image: a perceptual hash at 0/90/180/270 degrees (as displayed,
EXIF orientation applied). Candidate pairs share a hash within --max-distance at some
rotation and are confirmed by comparing the pixels (64x64 grey, after rotation). Each
pair is then classed by its worst-differing patch: "copy" (resized, recompressed or
rotated - default keep the larger) or "shot" (a second photo of the same moment, e.g.
a burst or repeated group photo - default keep both).

Writes Similar_<stamp>\\index.html (review page: keep A / keep B / keep both) and a CSV.
The page exports decisions in the format Move-TriageRejects.ps1 reads.

    python find_similar.py --source C:\\Media\\Library
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageOps

import features
import triage_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"}
ROTATIONS = (0, 90, 180, 270)


def describe(path: str, thumbs: str, key: str):
    """(size, [hash per rotation], 64x64 grey array, thumbnail rel path) or None if unreadable."""
    try:
        with Image.open(path) as im:
            im.draft("RGB", (512, 512))
            img = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    w, h = img.size
    img.thumbnail((512, 512))
    hashes = [features.perceptual_hash(img.rotate(r, expand=True)) for r in ROTATIONS]
    grey = np.asarray(img.convert("L").resize((64, 64)), dtype=np.uint8)
    thumb = os.path.join(thumbs, key + ".jpg")
    if not os.path.exists(thumb):
        t = img.copy()
        t.thumbnail((360, 360))
        t.save(thumb, "JPEG", quality=80)
    return (w, h), hashes, grey


def worst_patch(a_path: str, b_path: str, rotation: int) -> float:
    """Largest mean difference over a 16x16 grid of patches (256 px grey, brightness-matched).

    A resized/recompressed/rotated copy differs evenly and a little everywhere; a second
    shot of the same scene matches most patches but differs clearly where someone moved.
    """
    def grey(p, rot):
        with Image.open(p) as im:
            im.draft("RGB", (1024, 1024))
            g = ImageOps.exif_transpose(im).convert("L")
        if rot:
            g = g.rotate(rot, expand=True)
        return np.asarray(g.resize((256, 256), Image.LANCZOS), dtype=np.float32)

    a, b = grey(a_path, rotation), grey(b_path, 0)
    a = (a - a.mean()) / (a.std() + 1e-6) * b.std() + b.mean()
    return float(np.abs(a - b).reshape(16, 16, 16, 16).mean(axis=(1, 3)).max())


def classify(worst: float, a_dims: str, b_dims: str, rotation: int) -> str:
    """'copy' (same picture) or 'shot' (probably a separate photo of the same moment)."""
    different_size = sorted(map(int, a_dims.split("x"))) != sorted(map(int, b_dims.split("x")))
    if worst <= 4 or (different_size and worst <= 10) or (rotation and worst <= 10):
        return "copy"
    return "shot"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=r"C:\Media\Library")
    ap.add_argument("--new", action="append", default=[],
                    help="folder of newly added photos (repeatable): compared with --source and each other; "
                         "only pairs involving a new photo are reported")
    ap.add_argument("--report-root", default=r"C:\Media\DedupeReports")
    ap.add_argument("--max-distance", type=int, default=3, help="perceptual hash bits that may differ (default 3)")
    ap.add_argument("--max-pixel-diff", type=float, default=6.0, help="mean grey difference 0-255 to count as the same picture (default 6)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.report_root, f"Similar_{stamp}")
    thumbs = os.path.join(out_dir, "thumbs")
    os.makedirs(thumbs, exist_ok=True)

    roots = [args.source] + args.new
    paths = [os.path.join(d, f) for root in roots for d, _, fs in os.walk(root) for f in fs if os.path.splitext(f)[1].lower() in IMAGES]
    new_prefixes = tuple(os.path.normcase(os.path.abspath(n)) + os.sep for n in args.new)
    is_new = lambda p: bool(new_prefixes) and os.path.normcase(p).startswith(new_prefixes)  # noqa: E731
    print(f"{len(paths):,} images under {args.source}. Fingerprinting ...", flush=True)
    keys = [f"{i:06d}" for i in range(len(paths))]
    info = [None] * len(paths)
    started = time.time()

    def work(i):
        info[i] = describe(paths[i], thumbs, keys[i])

    with ThreadPoolExecutor(args.workers) as ex:
        for n, _ in enumerate(ex.map(work, range(len(paths))), 1):
            if n % 2000 == 0:
                rate = n / (time.time() - started)
                print(f"  {n:,}/{len(paths):,}  about {(len(paths) - n) / rate / 60:.0f} min left", flush=True)

    # Multi-index hashing: two 64-bit hashes within 3 bits share at least one of four 16-bit chunks.
    index = collections.defaultdict(list)
    for i, d in enumerate(info):
        if d:
            h0 = d[1][0]
            for c in range(4):
                index[(c, (h0 >> (16 * c)) & 0xFFFF)].append(i)

    pairs = {}
    for i, d in enumerate(info):
        if not d:
            continue
        for r, hr in enumerate(d[1]):
            for c in range(4):
                for j in index.get((c, (hr >> (16 * c)) & 0xFFFF), []):
                    if j <= i or (i, j) in pairs:
                        continue
                    if new_prefixes and not (is_new(paths[i]) or is_new(paths[j])):
                        continue  # both already in the library: reviewed separately
                    if core.hamming(hr, info[j][1][0]) > args.max_distance:
                        continue
                    # Same picture? Compare pixels with i rotated by the matching angle.
                    gi = np.rot90(d[2], k=ROTATIONS[r] // 90).astype(np.int16)  # PIL and numpy both rotate anticlockwise
                    diff = float(np.abs(gi - info[j][2].astype(np.int16)).mean())
                    if diff <= args.max_pixel_diff:
                        pairs[(i, j)] = (ROTATIONS[r], diff)

    rows = []
    for (i, j), (rot, diff) in sorted(pairs.items()):
        a, b = paths[i], paths[j]
        sa, sb = os.path.getsize(a), os.path.getsize(b)
        pa, pb = info[i][0][0] * info[i][0][1], info[j][0][0] * info[j][0][1]
        a_dims, b_dims = "x".join(map(str, info[i][0])), "x".join(map(str, info[j][0]))
        worst = worst_patch(a, b, rot)
        kind = classify(worst, a_dims, b_dims, rot)
        # Copies: keep more pixels, then the larger file - but at about the same resolution
        # prefer a copy in an album folder, so the album stays complete. Separate shots: keep both.
        in_album = lambda p: f"{os.sep}Albums{os.sep}" in p  # noqa: E731
        keep = ("B" if (pb, sb) > (pa, sa) else "A") if kind == "copy" else "both"
        if kind == "copy" and in_album(a) != in_album(b) and 0.95 <= pa / max(pb, 1) <= 1.05:
            keep = "A" if in_album(a) else "B"
        rows.append({"a": a, "b": b, "ka": keys[i], "kb": keys[j], "rotation": rot, "pixel_diff": round(diff, 2),
                     "worst_patch": round(worst, 2), "kind": kind,
                     "a_size": sa, "b_size": sb, "a_dims": a_dims, "b_dims": b_dims, "suggest": keep})

    with open(os.path.join(out_dir, f"Similar_{stamp}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["a"])
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(out_dir, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.SIMILAR = ")
        json.dump({"stamp": stamp, "source": args.source, "pairs": rows}, f)
        f.write(";\n")
    with open(os.path.join(HERE, "similar.html"), encoding="utf-8") as src, open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as dst:
        dst.write(src.read())

    rotated = sum(1 for r in rows if r["rotation"])
    groups = collections.Counter()
    for r in rows:
        groups[r["a"]] += 1
        groups[r["b"]] += 1
    copies = sum(1 for r in rows if r["kind"] == "copy")
    print(f"Similar pairs: {len(rows):,} - likely copies {copies:,}, probably separate shots {len(rows) - copies:,} "
          f"({rotated:,} rotated); "
          f"unreadable images: {sum(1 for d in info if not d):,}; images in more than one pair: {sum(1 for v in groups.values() if v > 1):,}")
    print(f"Review page: {os.path.join(out_dir, 'index.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
