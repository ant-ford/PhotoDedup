"""Find junk videos (memes, adverts, greetings, news clips) and near-copies, for review.

Read-only. For each video three frames are taken (15%, 50% and 85% of its length) with
the ffmpeg bundled in imageio-ffmpeg; the image model classifies them and the averaged
result gives the video's category. Near-copies: another video whose length matches
within --max-duration-diff seconds and whose three frames all look alike (perceptual
hash within --max-distance bits) - typically the same clip forwarded and re-compressed.
Library videos (--library) are only compared against, never suggested for removal.

Writes VideoTriage_<stamp>\\index.html (the same review page as photo triage) and a
CSV. Export decisions from the page, then apply them with Move-TriageRejects.ps1.

    python video_triage.py --source C:\\Media\\Imports
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import features
import triage_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO = re.compile(r"\.(mp4|mov|3gp|m4v|avi)$", re.I)
POSITIONS = (0.15, 0.5, 0.85)


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_info(paths: list[str]) -> dict[str, dict]:
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "embed"))
    import embed_metadata as em
    et = em.ExifTool(em.find_exiftool())
    out = {}
    try:
        for i in range(0, len(paths), 200):
            for r in et.read_json(paths[i:i + 200], ["-n", "-QuickTime:Duration", "-QuickTime:ImageWidth", "-QuickTime:ImageHeight"]):
                out[os.path.normcase(os.path.abspath(r["SourceFile"]))] = {
                    "duration": float(r.get("QuickTime:Duration") or 0),
                    "width": int(r.get("QuickTime:ImageWidth") or 0), "height": int(r.get("QuickTime:ImageHeight") or 0)}
    finally:
        et.close()
    return out


def grab(ffmpeg: str, path: str, seconds: float) -> Image.Image | None:
    r = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{seconds:.2f}", "-i", path, "-frames:v", "1",
                        "-vf", "scale=512:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        return None
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def key_of(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{path.lower()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=r"C:\Media\Imports", help="folder of new videos to review")
    ap.add_argument("--library", default=r"C:\Media\Library", help="videos already kept (compared against only)")
    ap.add_argument("--report-root", default=r"C:\Media\DedupeReports")
    ap.add_argument("--cache", default=r"C:\Media\TriageCache")
    ap.add_argument("--threshold", type=float, default=0.8, help="confidence needed to suggest removing (default 0.8)")
    ap.add_argument("--max-duration-diff", type=float, default=0.5)
    ap.add_argument("--max-distance", type=int, default=8)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    new = sorted(os.path.join(d, f) for d, _, fs in os.walk(args.source) for f in fs if VIDEO.search(f) and os.path.getsize(os.path.join(d, f)))
    lib = sorted(os.path.join(d, f) for d, _, fs in os.walk(args.library) for f in fs if VIDEO.search(f))
    print(f"{len(new):,} new videos, {len(lib):,} library videos. Reading lengths ...", flush=True)
    info = video_info(new + lib)

    thumbs = os.path.join(args.cache, "video_thumbs")
    os.makedirs(thumbs, exist_ok=True)
    db = sqlite3.connect(os.path.join(args.cache, "videos.sqlite"), check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, hashes TEXT, emb BLOB, flat REAL)")
    ffmpeg = ffmpeg_exe()

    clf = None
    def frames_for(path):
        dur = info.get(os.path.normcase(path), {}).get("duration", 0) or 1.0
        return [grab(ffmpeg, path, max(0.05, p * dur)) for p in POSITIONS]

    todo = []
    for p in new + lib:
        row = db.execute("SELECT emb FROM videos WHERE id = ?", (key_of(p),)).fetchone()
        if row is None or (p in new and row[0] is None):
            todo.append(p)
    print(f"{len(todo):,} videos to read frames from (others cached).", flush=True)
    if any(p in set(new) for p in todo):
        from clipmodel import ClipClassifier
        clf = ClipClassifier()

    new_set = set(new)
    started, done = time.time(), [0]
    def work(p):
        fr = [f for f in frames_for(p) if f is not None]
        return p, fr

    with ThreadPoolExecutor(args.workers) as ex:
        for p, fr in ex.map(work, todo):
            k = key_of(p)
            hashes = [format(features.perceptual_hash(f), "016x") for f in fr]
            emb = None
            if fr and clf is not None and p in new_set:
                e = clf.embed(fr).mean(axis=0)
                emb = (e / (np.linalg.norm(e) or 1)).astype(np.float32).tobytes()
            if fr:
                os.makedirs(os.path.join(thumbs, k[:2]), exist_ok=True)
                strip_h = 180
                tiles = [f.resize((max(1, int(f.width * strip_h / f.height)), strip_h)) for f in fr]
                strip = Image.new("RGB", (sum(t.width for t in tiles) + 4 * (len(tiles) - 1), strip_h), "white")
                x = 0
                for t in tiles:
                    strip.paste(t, (x, 0))
                    x += t.width + 4
                strip.save(os.path.join(thumbs, k[:2], k + ".jpg"), quality=78)
            db.execute("INSERT OR REPLACE INTO videos VALUES (?, ?, ?, ?)",
                       (k, json.dumps(hashes), emb, features.flatness(fr[1]) if len(fr) > 1 else 0.0))
            done[0] += 1
            if done[0] % 200 == 0 or done[0] == len(todo):
                db.commit()
                rate = done[0] / (time.time() - started)
                print(f"  {done[0]:,}/{len(todo):,}  {rate:.1f} videos/s  about {(len(todo) - done[0]) / rate / 60:.0f} min left", flush=True)
    db.commit()

    def load(p):
        r = db.execute("SELECT hashes, emb, flat FROM videos WHERE id = ?", (key_of(p),)).fetchone()
        return ([int(h, 16) for h in json.loads(r[0])], np.frombuffer(r[1], dtype=np.float32) if r[1] else None, r[2]) if r else ([], None, 0)
    rec = {p: load(p) for p in new + lib}

    # Near-copies: same length, all frames alike. Kept copy: library first, then larger file.
    by_len = {}
    for p in new + lib:
        by_len.setdefault(round(info.get(os.path.normcase(p), {}).get("duration", 0)), []).append(p)
    copy_of = {}
    for p in new:
        dp = info.get(os.path.normcase(p), {}).get("duration", 0)
        hp = rec[p][0]
        if len(hp) < 3 or dp <= 0:
            continue
        best = None
        for b in (round(dp) - 1, round(dp), round(dp) + 1):
            for q in by_len.get(b, []):
                if q == p or abs(info.get(os.path.normcase(q), {}).get("duration", 0) - dp) > args.max_duration_diff:
                    continue
                hq = rec[q][0]
                if len(hq) == 3 and all(core.hamming(x, y) <= args.max_distance for x, y in zip(hp, hq)):
                    rank = (q not in new_set, os.path.getsize(q))
                    if best is None or rank > best[0]:
                        best = (rank, q)
        if best and best[0] > (False, os.path.getsize(p)):
            copy_of[p] = best[1]

    rows = []
    for p in new:
        i = info.get(os.path.normcase(p), {})
        k = key_of(p)
        hashes, emb, flat = rec[p]
        name = os.path.basename(p)
        why = [f"{i.get('duration', 0):.0f}s", f"{i.get('width', 0)}x{i.get('height', 0)}"]
        if not hashes:
            category, conf, suggestion, note, top = "unreadable", 0.0, "review", "frames could not be read", ""
        elif p in copy_of:
            q = copy_of[p]
            category, conf, top = "video_copy", 1.0, ""
            suggestion, note = "reject", ("already in your library" if q not in new_set else "a larger copy of this video is kept")
        elif emb is not None:
            if clf is None:  # everything came from the cache
                from clipmodel import ClipClassifier
                clf = ClipClassifier()
            probs = clf.probabilities(emb[None, :])[0]
            ranked = sorted(probs.items(), key=lambda kv: -kv[1])
            category, conf = ranked[0]
            top = ", ".join(f"{a} {b:.0%}" for a, b in ranked[:3])
            suggestion, note = core.decide(category, conf, [], args.threshold, probs.get("photo", 0.0))
        else:
            category, conf, suggestion, note, top = "unknown", 0.0, "keep", "", ""
        q = copy_of.get(p, "")
        rows.append({"id": k, "p": p, "r": os.path.relpath(p, args.source), "c": category, "ct": category,
                     "cf": round(conf, 3), "s": suggestion, "nt": note, "why": " · ".join(why), "t3": top,
                     "w": i.get("width", 0), "h": i.get("height", 0), "b": os.path.getsize(p), "cam": "", "gps": False,
                     "ppl": 0, "wa": True, "d": core.whatsapp_date(name),
                     "o": q, "oid": key_of(q) if q else "", "od": "", "ow": info.get(os.path.normcase(q), {}).get("width", "") if q else "",
                     "oh": info.get(os.path.normcase(q), {}).get("height", "") if q else ""})

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.report_root, f"VideoTriage_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"VideoTriage_{stamp}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["p"])
        w.writeheader()
        w.writerows(rows)
    data = {"stamp": stamp, "source": args.source, "thumbs": os.path.relpath(thumbs, out_dir).replace(os.sep, "/"), "rows": rows}
    with open(os.path.join(out_dir, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.TRIAGE = " + json.dumps(data, separators=(",", ":")) + ";\n")
    shutil.copyfile(os.path.join(HERE, "gallery.html"), os.path.join(out_dir, "index.html"))

    counts = {}
    for r in rows:
        counts[r["c"]] = counts.get(r["c"], 0) + 1
    rej = [r for r in rows if r["s"] == "reject"]
    print("Categories: " + ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    print(f"Suggested for removal: {len(rej):,} videos, {sum(r['b'] for r in rej) / 1e9:.2f} GB")
    print(f"Review page: {os.path.join(out_dir, 'index.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
