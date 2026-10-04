"""Read-only: find which library files belong to each Google Photos album.

Members are the media in Takeout\\Google Photos\\<album> folders, in the source folder
or in the duplicate quarantines. A quarantined member is matched to the copy still in
the source by file name plus ExifTool's picture-only fingerprint (ImageDataHash), so
the match survives embedded-metadata changes.

    python find_albums.py --out albums.json
Output: {album folder name: {"title", "files", "recovered", "not_in_library", "lost_paths", "first", "last"}}
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "embed"))
import embed_metadata as em  # noqa: E402

MEDIA = re.compile(r"\.(jpe?g|heic|png|webp|gif|bmp|mp4|mov|avi|3gp|m4v)$", re.I)
YEAR = re.compile(r"^Photos from \d{4}$")


def base_name(name: str) -> str:
    stem, ext = os.path.splitext(name.lower())
    return re.sub(r"\(\d+\)$", "", stem) + ext


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=r"C:\Media\Imports")
    ap.add_argument("--quarantine", action="append", default=[], help="quarantine folder (repeatable)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    quarantines = args.quarantine or [r"C:\Media\DuplicateQuarantine", r"C:\Media\DuplicateQuarantine-2"]
    roots = [args.source] + [q for q in quarantines if os.path.isdir(q)]
    exe = em.find_exiftool()

    members = collections.defaultdict(list)
    titles = {}
    for root in roots:
        for dirpath, _, files in os.walk(root):
            parts = os.path.relpath(dirpath, root).split(os.sep)
            if len(parts) != 4 or parts[1:3] != ["Takeout", "Google Photos"] or YEAR.match(parts[3]):
                continue
            for f in files:
                p = os.path.join(dirpath, f)
                if MEDIA.search(f):
                    members[parts[3]].append((root, p))
                elif f.lower() == "metadata.json" and parts[3] not in titles:
                    try:
                        with open(p, encoding="utf-8") as fh:
                            titles[parts[3]] = json.load(fh).get("title") or parts[3]
                    except (OSError, ValueError):
                        pass

    by_name = collections.defaultdict(list)
    for dirpath, _, files in os.walk(args.source):
        for f in files:
            if MEDIA.search(f):
                by_name[base_name(f)].append(os.path.join(dirpath, f))

    need = set()
    for ms in members.values():
        for root, p in ms:
            if root != args.source:
                need.add(p)
                need.update(by_name.get(base_name(os.path.basename(p)), []))
    fingerprint = {}
    todo = sorted(need)
    for i in range(0, len(todo), 200):
        out = subprocess.run([exe, "-j", "-charset", "filename=utf8", "-api", "ImageHashType=SHA256", "-ImageDataHash", "-@", "-"],
                             input="\n".join(todo[i:i + 200]).encode("utf-8"), capture_output=True).stdout
        for r in json.loads(out or b"[]"):
            if r.get("ImageDataHash"):
                fingerprint[os.path.normcase(os.path.abspath(r["SourceFile"]))] = r["ImageDataHash"]
    fp = lambda p: fingerprint.get(os.path.normcase(os.path.abspath(p)))  # noqa: E731

    plan = em.build_plan(args.source, exe, "Asia/Hong_Kong")
    dates = {os.path.normcase(r["path"]): r["date_taken"][:10].replace(":", "-") for r in plan}

    result = {}
    for name, ms in sorted(members.items(), key=lambda kv: kv[0].lower()):
        current, recovered, lost = set(), 0, []
        for root, p in ms:
            if root == args.source:
                current.add(p)
                continue
            target = fp(p)
            match = next((c for c in by_name.get(base_name(os.path.basename(p)), []) if target and fp(c) == target), None)
            if match:
                recovered += match not in current
                current.add(match)
            else:
                lost.append(p)
        ds = sorted(d for d in (dates.get(os.path.normcase(p), "") for p in current) if d)
        result[name] = {"title": titles.get(name, name), "files": sorted(current), "recovered": recovered,
                        "not_in_library": len(lost), "lost_paths": lost, "first": ds[0] if ds else "", "last": ds[-1] if ds else ""}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    for name, r in result.items():
        print(f"{r['title'][:40]:<40} {len(r['files']):>5} photos  {r['first']} - {r['last']}"
              + (f"  [{r['not_in_library']} not in library]" if r["not_in_library"] else ""))
    print(f"Written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
