"""Move every photo and video from the import folders into one tidy library.

    Library\\<YYYY>\\<YYYY-MM>\\<file>        by date taken (read from the embedded metadata)
    Library\\Albums\\<album folder>\\<file>    files listed in --albums
    Library\\Undated\\<file>                 no date anywhere

Each file is moved exactly once (same drive: a rename, the content is not touched)
and checked afterwards (present at the destination with the same size, gone from the
source). Same-named files never overwrite each other: "name (2).jpg" and so on.
An Actions-style manifest is written so Restore-Quarantine.ps1 can move everything
back. Only media moves; sidecar JSON, zips and other files stay where they are.

    python refile_library.py plan  --albums album_folders.json
    python refile_library.py apply --albums album_folders.json --expected 32313
--albums: JSON {file path: album folder name}.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "embed"))
import embed_metadata as em  # noqa: E402

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_folder(name: str) -> str:
    """A Windows-safe folder name ('Trip 2022/23' -> 'Trip 2022-23')."""
    return INVALID.sub("-", name).strip(" .") or "Album"


def unique(dest: str, taken: set[str]) -> str:
    """dest, or 'name (2).ext', 'name (3).ext', ... if that name is planned or already exists."""
    stem, ext = os.path.splitext(dest)
    candidate, n = dest, 1
    while os.path.normcase(candidate) in taken or os.path.exists(candidate):
        n += 1
        candidate = f"{stem} ({n}){ext}"
    taken.add(os.path.normcase(candidate))
    return candidate


def build(source: str, library: str, albums: dict[str, str], exe: str, default_tz: str) -> list[dict]:
    dated = em.build_plan(source, exe, default_tz)  # reads each file's date taken
    album_of = {os.path.normcase(os.path.abspath(p)): safe_folder(f) for p, f in albums.items() if f}
    rows = []
    for d in sorted(dated, key=lambda r: r["path"].lower()):
        path = d["path"]
        folder = album_of.get(os.path.normcase(path))
        stamp = d["date_taken"]  # 'YYYY:MM:DD HH:MM:SS+hh:mm' or ''
        if folder:
            target_dir, reason = os.path.join(library, "Albums", folder), "album"
        elif stamp:
            y, m = stamp[0:4], stamp[5:7]
            target_dir, reason = os.path.join(library, y, f"{y}-{m}"), "date"
        else:
            target_dir, reason = os.path.join(library, "Undated"), "undated"
        rows.append({"source": path, "dir": target_dir, "reason": reason, "date": stamp, "size": os.path.getsize(path)})

    taken: set[str] = set()
    for r in sorted(rows, key=lambda r: (r["dir"].lower(), os.path.basename(r["source"]).lower(), r["source"].lower())):
        r["dest"] = unique(os.path.join(r["dir"], os.path.basename(r["source"])), taken)
        r["renamed"] = os.path.basename(r["dest"]) != os.path.basename(r["source"])
    return rows


def write_plan(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["SourcePath", "DestinationPath", "Reason", "DateTaken", "SizeBytes", "Renamed"])
        for r in rows:
            w.writerow([r["source"], r["dest"], r["reason"], r["date"], r["size"], r["renamed"]])


def apply(rows: list[dict], manifest: str) -> tuple[int, int]:
    ok = failed = 0
    with open(manifest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["TimeStamp", "Action", "SourcePath", "DestinationPath", "SizeBytes", "Hash", "Detail"])
        for i, r in enumerate(rows, 1):
            src, dest = r["source"], r["dest"]
            try:
                if os.path.exists(dest):
                    raise RuntimeError("destination already exists; not overwriting")
                if os.path.getsize(src) != r["size"]:
                    raise RuntimeError("file changed since the plan")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(src, dest)  # same drive: atomic rename, content untouched
                if os.path.exists(src) or os.path.getsize(dest) != r["size"]:
                    raise RuntimeError("post-move check failed")
                ok += 1
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "MovedMedia", src, dest, r["size"], "", f"Re-filed ({r['reason']})"])
            except Exception as e:  # noqa: BLE001
                failed += 1
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "Failed", src, dest, r["size"], "", str(e)[:300]])
                print(f"  FAILED: {src} :: {e}", flush=True)
            if i % 2000 == 0 or i == len(rows):
                f.flush()
                print(f"  {i:,}/{len(rows):,}", flush=True)
    return ok, failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "apply"])
    ap.add_argument("--source", default=r"C:\Media\Imports")
    ap.add_argument("--library", default=r"C:\Media\Library")
    ap.add_argument("--albums", help="JSON {file path: album folder name}")
    ap.add_argument("--report-root", default=r"C:\Media\DedupeReports")
    ap.add_argument("--default-tz", default="Asia/Hong_Kong")
    ap.add_argument("--expected", type=int, default=-1)
    args = ap.parse_args(argv)

    source, library = os.path.realpath(args.source), os.path.realpath(args.library)
    if library.lower().startswith(source.lower() + os.sep) or source.lower().startswith(library.lower() + os.sep):
        ap.error("library and source must not be inside each other")
    if os.path.splitdrive(source)[0].lower() != os.path.splitdrive(library)[0].lower():
        ap.error("library must be on the same drive as the source (moves are renames)")
    albums = {}
    if args.albums:
        with open(args.albums, encoding="utf-8") as f:
            albums = json.load(f)

    rows = build(source, library, albums, em.find_exiftool(), args.default_tz)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    plan_csv = os.path.join(args.report_root, f"RefilePlan_{stamp}.csv")
    write_plan(rows, plan_csv)

    by_reason = {k: sum(1 for r in rows if r["reason"] == k) for k in ("date", "album", "undated")}
    years = sorted({r["dest"][len(library) + 1:].split(os.sep)[0] for r in rows if r["reason"] == "date"})
    print(f"Files to move: {len(rows):,} ({sum(r['size'] for r in rows) / 1e9:.1f} GB)")
    print(f"  into date folders: {by_reason['date']:,} ({years[0] if years else ''}-{years[-1] if years else ''}, {len(years)} years)")
    print(f"  into album folders: {by_reason['album']:,} in {len({r['dir'] for r in rows if r['reason'] == 'album'})} albums")
    print(f"  undated: {by_reason['undated']:,}")
    print(f"  renamed to avoid a clash: {sum(r['renamed'] for r in rows):,}")
    print(f"Plan: {plan_csv}")
    if args.command == "plan":
        print("Plan only - nothing was moved.")
        return 0
    if args.expected != len(rows):
        print(f"The plan has {len(rows):,} files, not --expected {args.expected:,}. Nothing was moved.")
        return 2

    manifest = os.path.join(args.report_root, f"Refile_{stamp}_Actions.csv")
    print(f"Moving {len(rows):,} files. Manifest (rollback for Restore-Quarantine.ps1): {manifest}")
    ok, failed = apply(rows, manifest)
    print(f"Done: {ok:,} moved, {failed:,} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
