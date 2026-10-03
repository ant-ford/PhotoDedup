"""Find screenshots, memes, adverts and WhatsApp copies of photos you already have.

Read-only: scans the library, writes a CSV and a local review page. Nothing is moved.
Your review decisions are applied afterwards by Move-TriageRejects.ps1.

    python triage.py                     # whole library, with the image model
    python triage.py --sample 300        # quick trial on 300 images spread across the library
    python triage.py --no-clip           # file evidence only (no model download)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

import numpy as np

import features
import triage_core as core

HERE = os.path.dirname(os.path.abspath(__file__))


class Cache:
    """Per-image facts and embeddings, so re-runs only process new or changed files."""

    def __init__(self, folder: str):
        os.makedirs(folder, exist_ok=True)
        self.thumbs = os.path.join(folder, "thumbs")
        self.db = sqlite3.connect(os.path.join(folder, "triage.sqlite"))
        self.db.execute("CREATE TABLE IF NOT EXISTS images (id TEXT PRIMARY KEY, facts TEXT, emb BLOB)")

    @staticmethod
    def key(img: core.ImageFile) -> str:
        return hashlib.sha1(f"{img.path.lower()}|{img.size}|{img.mtime_ns}".encode()).hexdigest()[:20]

    def thumb_path(self, key: str) -> str:
        return os.path.join(self.thumbs, key[:2], key + ".jpg")

    def get(self, key: str):
        row = self.db.execute("SELECT facts, emb FROM images WHERE id = ?", (key,)).fetchone()
        if not row:
            return None, None
        emb = np.frombuffer(row[1], dtype=np.float32) if row[1] else None
        return json.loads(row[0]), emb

    def put(self, key: str, facts: dict, emb) -> None:
        blob = emb.astype(np.float32).tobytes() if emb is not None else None
        self.db.execute("INSERT OR REPLACE INTO images VALUES (?, ?, ?)", (key, json.dumps(facts), blob))

    def commit(self) -> None:
        self.db.commit()


def process(images, cache: Cache, clf, batch_size: int) -> None:
    """Extracts facts (and embeddings) for every image not already cached."""
    todo = []
    for img in images:
        facts, emb = cache.get(Cache.key(img))
        if facts is None or (clf is not None and emb is None and not facts.get("error")):
            todo.append(img)

    print(f"{len(images):,} images, {len(images) - len(todo):,} already cached, {len(todo):,} to process.")
    if not todo:
        return

    start = time.time()
    pending = []  # (key, facts, rgb)

    def flush():
        if not pending:
            return
        embs = clf.embed([p[2] for p in pending]) if clf is not None else [None] * len(pending)
        for (key, facts, _), emb in zip(pending, embs):
            cache.put(key, facts, emb)
        pending.clear()
        cache.commit()

    try:
        for n, img in enumerate(todo, 1):
            key = Cache.key(img)
            facts, rgb = features.extract(img.path, cache.thumb_path(key))
            if rgb is None or clf is None:
                cache.put(key, facts, None)
            else:
                pending.append((key, facts, rgb))
                if len(pending) >= batch_size:
                    flush()

            if n % 100 == 0 or n == len(todo):
                rate = n / max(time.time() - start, 1e-6)
                eta = (len(todo) - n) / rate
                print(f"  {n:,}/{len(todo):,}  {rate:.1f} img/s  about {eta / 60:.0f} min left", flush=True)
                if clf is None:
                    cache.commit()
        flush()
    except KeyboardInterrupt:
        flush()
        print("Interrupted - progress saved. Run the same command again to continue.")
        raise


def classify(images, cache: Cache, clf, threshold: float) -> tuple[list[dict], list]:
    rows = []
    embs = []
    for img in images:
        key = Cache.key(img)
        facts, emb = cache.get(key)
        side = core.read_sidecars(img.sidecars)
        wa = core.is_whatsapp(img.rel, side["upload_folder"])

        photo_p = 0.0
        if facts.get("error"):
            category, conf, reasons, top = "unreadable", 0.0, [facts["error"]], ""
        elif clf is not None and emb is not None:
            probs = clf.probabilities(emb[None, :])[0]
            probs, reasons = core.adjust_probabilities(probs, facts)
            ranked = sorted(probs.items(), key=lambda kv: -kv[1])
            category, conf = ranked[0]
            photo_p = probs.get("photo", 0.0)
            top = ", ".join(f"{k} {v:.0%}" for k, v in ranked[:3])
        else:
            category, conf, reasons = core.heuristic_category(facts)
            top = ""

        protected = core.protection_reasons(facts, side)
        suggestion, note = core.decide(category, conf, protected, threshold, photo_p,
                                       camera_shape=core.camera_shaped(facts.get("width", 0), facts.get("height", 0)))

        rows.append({
            "id": key, "path": img.path, "rel": img.rel, "name": os.path.basename(img.path),
            "category": category, "content": category, "confidence": round(conf, 3),
            "suggestion": suggestion, "note": note, "reasons": "; ".join(reasons), "top3": top,
            "width": facts.get("width", 0), "height": facts.get("height", 0), "bytes": img.size,
            "format": facts.get("format", ""), "camera": facts.get("camera", ""),
            "gps": bool(facts.get("gps") or side["gps"]), "people": side["people"],
            "upload_folder": side["upload_folder"], "whatsapp": wa,
            "date": core.whatsapp_date(os.path.basename(img.path)) or facts.get("taken", "")[:10].replace(":", "-"),
            "flatness": facts.get("flatness", 0.0), "phash": facts.get("phash", ""),
            "copy_of": "", "copy_distance": "", "copy_id": "", "copy_width": "", "copy_height": "",
        })
        embs.append(emb)
    return rows, embs


def mark_whatsapp_copies(rows: list[dict], embs, max_distance: int) -> int:
    hashed = [i for i, r in enumerate(rows) if r["phash"]]
    phashes = np.zeros(len(rows), dtype=np.uint64)
    for i in hashed:
        phashes[i] = np.uint64(int(rows[i]["phash"], 16))
    sizes = [(r["width"], r["height"]) for r in rows]

    wa = [i for i in hashed if rows[i]["whatsapp"]]
    orig = [i for i in hashed if not rows[i]["whatsapp"]]

    emb_matrix = None
    if embs and all(e is not None for e in embs):
        emb_matrix = np.vstack(embs)

    matches = core.find_whatsapp_copies(wa, orig, phashes, sizes, max_distance, emb_matrix)
    for w, (o, dist) in matches.items():
        r, src = rows[w], rows[o]
        r["category"] = "whatsapp_copy"
        r["copy_of"], r["copy_id"], r["copy_distance"] = src["path"], src["id"], dist
        r["copy_width"], r["copy_height"] = src["width"], src["height"]
        bigger = src["width"] * src["height"] >= 0.9 * r["width"] * r["height"]
        if dist <= 4 and bigger:
            r["suggestion"], r["note"] = "reject", "you have this photo in better quality"
        else:
            r["suggestion"] = "review"
            r["note"] = "similar to a photo you have" + ("" if bigger else " (but this copy is larger)")
    return len(matches)


def write_outputs(rows: list[dict], cache: Cache, report_root: str, stamp: str, source: str) -> str:
    out_dir = os.path.join(report_root, f"Triage_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    csv_path = os.path.join(out_dir, f"Triage_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["path"])
        w.writeheader()
        w.writerows(rows)

    thumbs_rel = os.path.relpath(cache.thumbs, out_dir).replace(os.sep, "/")
    data = {
        "stamp": stamp, "source": source, "thumbs": thumbs_rel,
        "categories": core.CATEGORIES + ["whatsapp_copy", "unreadable", "unknown"],
        "junk": sorted(core.JUNK),
        "rows": [{
            "id": r["id"], "p": r["path"], "r": r["rel"], "c": r["category"], "ct": r["content"],
            "cf": r["confidence"], "s": r["suggestion"], "nt": r["note"], "why": r["reasons"],
            "t3": r["top3"], "w": r["width"], "h": r["height"], "b": r["bytes"], "cam": r["camera"],
            "gps": r["gps"], "ppl": r["people"], "wa": r["whatsapp"], "d": r["date"],
            "o": r["copy_of"], "oid": r["copy_id"], "od": r["copy_distance"],
            "ow": r["copy_width"], "oh": r["copy_height"],
        } for r in rows],
    }
    with open(os.path.join(out_dir, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.TRIAGE = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    shutil.copyfile(os.path.join(HERE, "gallery.html"), os.path.join(out_dir, "index.html"))
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=r"C:\Media\Imports")
    ap.add_argument("--report-root", default=r"C:\Media\DedupeReports")
    ap.add_argument("--cache", default=r"C:\Media\TriageCache", help="thumbnails and per-image results (safe to delete)")
    ap.add_argument("--no-clip", action="store_true", help="skip the image model; only screenshots and WhatsApp copies are found")
    ap.add_argument("--sample", type=int, default=0, help="only process N images spread evenly across the library")
    ap.add_argument("--threshold", type=float, default=0.75, help="minimum confidence to suggest rejecting (default 0.75)")
    ap.add_argument("--copy-distance", type=int, default=6, help="max perceptual-hash difference for WhatsApp copies (default 6)")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--threads", type=int, default=0, help="CPU threads for the model (default: all)")
    args = ap.parse_args(argv)

    source = os.path.realpath(args.source)
    for p in (args.report_root, args.cache):
        if os.path.realpath(p).lower().startswith(source.lower() + os.sep):
            ap.error(f"{p} must not be inside the source folder")

    print(f"Scanning {source} ...")
    images = core.scan_library(source)
    if args.sample and args.sample < len(images):
        step = len(images) / args.sample
        images = [images[int(i * step)] for i in range(args.sample)]

    clf = None
    if not args.no_clip:
        print("Loading image model (first run downloads about 600 MB) ...")
        from clipmodel import ClipClassifier
        clf = ClipClassifier(threads=args.threads)

    cache = Cache(args.cache)
    process(images, cache, clf, args.batch)

    print("Classifying ...")
    rows, embs = classify(images, cache, clf, args.threshold)
    copies = mark_whatsapp_copies(rows, embs, args.copy_distance)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = write_outputs(rows, cache, args.report_root, stamp, source)

    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    rejects = [r for r in rows if r["suggestion"] == "reject"]
    print()
    print("Categories: " + ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    print(f"WhatsApp copies of photos you have: {copies:,}")
    print(f"Suggested for removal: {len(rejects):,} images, {sum(r['bytes'] for r in rejects) / 1e9:.2f} GB")
    print(f"Review page: {os.path.join(out_dir, 'index.html')}")
    print("Nothing was moved. Review the page, export your decisions, then run Move-TriageRejects.ps1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
