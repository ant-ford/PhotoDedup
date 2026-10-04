"""Write Google Takeout metadata into the photo and video files themselves.

Fills gaps only - nothing already stored in a file is changed:
    date taken   from the Google sidecar (UTC, converted to local time where the photo
                 was taken: GPS -> timezone, else --default-tz), else from a WhatsApp or
                 camera-style file name
    GPS          from the sidecar when the file has none
    description  from the sidecar when the file has none
    people       names tagged in Google Photos (XMP PersonInImage + keywords)
    file dates   Windows created/modified dates set to the date taken

Every rewritten file is checked before it replaces the original: the picture/video
data fingerprint (ExifTool ImageDataHash, SHA-256) must be identical and the new
tags must read back. Each change is logged so `undo` can remove it again.

    python embed_metadata.py plan                          # read-only; writes a plan CSV
    python embed_metadata.py apply --expected 24116        # write (count must match plan)
    python embed_metadata.py undo --log <EmbedLog_*.csv>   # remove what apply added
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "triage"))
import triage_core as core  # noqa: E402

VIDEO = {".mp4", ".mov", ".m4v", ".3gp"}
MEDIA = core.IMAGE_EXTENSIONS | VIDEO | {".avi"}
NOT_WRITABLE = {".avi", ".bmp"}  # ExifTool cannot write these; only file dates are set
EXIF_TYPES = {".jpg", ".jpeg", ".heic", ".webp", ".png"}
XMP_DATE_TYPES = {".png", ".gif"}  # also get an XMP date, which more apps read for these

WA_TIME = "12:00:00"  # WhatsApp names carry a date only
CAMERA_NAME_RE = re.compile(r"(?:^|\D)(20\d{2}|19\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?:\D|$)")
EXIF_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")

# Written tag -> key it reads back as (checked before a rewritten file replaces the original).
VERIFY = {
    "EXIF:DateTimeOriginal": "EXIF:DateTimeOriginal", "XMP-exif:DateTimeOriginal": "XMP:DateTimeOriginal",
    "QuickTime:CreateDate": "QuickTime:CreateDate", "XMP-dc:Description": "XMP:Description",
}

READ_TAGS = [
    "-EXIF:DateTimeOriginal", "-EXIF:CreateDate", "-EXIF:OffsetTimeOriginal",
    "-XMP:DateTimeOriginal", "-QuickTime:CreateDate", "-QuickTime:CreationDate",
    "-GPSLatitude#", "-GPSLongitude#", "-GPSCoordinates#",
    "-EXIF:ImageDescription", "-XMP-dc:Description", "-XMP-iptcExt:PersonInImage", "-XMP-dc:Subject",
    "-FileModifyDate", "-FileCreateDate",
]


def find_exiftool() -> str:
    for c in (shutil.which("exiftool"), os.path.expandvars(r"%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe")):
        if c and os.path.exists(c):
            return c
    sys.exit("ExifTool not found. Install it with:  winget install OliverBetz.ExifTool")


class ExifTool:
    """One long-running ExifTool process (-stay_open), safe to use from one thread."""

    def __init__(self, exe: str):
        self.p = subprocess.Popen(
            [exe, "-stay_open", "True", "-@", "-", "-common_args", "-charset", "filename=utf8",
             "-api", "QuickTimeUTC=1", "-api", "ImageHashType=SHA256", "-api", "LargeFileSupport=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def run(self, args: list[str]) -> tuple[str, str]:
        self.n += 1
        marker = f"{{ready{self.n}}}"
        text = "\n".join(args + ["-echo4", marker, f"-execute{self.n}"]) + "\n"
        self.p.stdin.write(text.encode("utf-8"))
        self.p.stdin.flush()
        return self._read(self.p.stdout, marker), self._read(self.p.stderr, marker)

    @staticmethod
    def _read(stream, marker: str) -> str:
        out = []
        while True:
            line = stream.readline()
            if not line:
                raise RuntimeError("ExifTool stopped unexpectedly")
            s = line.decode("utf-8", "replace").rstrip("\r\n")
            if s == marker:
                return "\n".join(out)
            out.append(s)

    def read_json(self, paths: list[str], tags: list[str]) -> list[dict]:
        out, err = self.run(["-j", "-G0"] + tags + paths)
        return json.loads(out) if out.strip() else []

    def close(self):
        try:
            self.p.stdin.write(b"-stay_open\nFalse\n")
            self.p.stdin.flush()
            self.p.wait(timeout=30)
        except Exception:  # noqa: BLE001
            self.p.kill()


# ----------------------------------------------------------------------------- facts

def read_sidecar_details(paths: list[str]) -> dict:
    out = {"taken": None, "lat": None, "lon": None, "alt": None, "description": "", "people": []}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        ts = (d.get("photoTakenTime") or {}).get("timestamp")
        if ts and out["taken"] is None:
            out["taken"] = int(ts)
        for key in ("geoData", "geoDataExif"):
            g = d.get(key) or {}
            lat, lon = float(g.get("latitude") or 0), float(g.get("longitude") or 0)
            if out["lat"] is None and (abs(lat) > 1e-6 or abs(lon) > 1e-6):
                out["lat"], out["lon"], out["alt"] = lat, lon, float(g.get("altitude") or 0)
        if not out["description"]:
            out["description"] = (d.get("description") or "").strip()
        for person in d.get("people") or []:
            name = (person.get("name") or "").strip()
            if name and name not in out["people"]:
                out["people"].append(name)
    return out


def as_list(v) -> list[str]:
    if v is None or v == "":
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def parse_exif_date(s) -> dt.datetime | None:
    m = EXIF_DATE_RE.match(str(s or ""))
    if not m or m.group(1) == "0000":
        return None
    try:
        return dt.datetime(*map(int, m.groups()))
    except ValueError:
        return None


def name_date(name: str) -> tuple[dt.datetime | None, str]:
    """Date from a WhatsApp (date only) or camera-style (date and time) file name."""
    wa = core.whatsapp_date(name)
    if wa:
        return dt.datetime.fromisoformat(wa + "T" + WA_TIME), "whatsapp-name"
    m = CAMERA_NAME_RE.search(name)
    if m:
        try:
            return dt.datetime(*map(int, m.groups())), "camera-name"
        except ValueError:
            pass
    return None, ""


class Zones:
    def __init__(self, default_tz: str):
        self.default = ZoneInfo(default_tz)
        self._tf = None

    def at(self, lat, lon) -> ZoneInfo:
        if lat is None or lon is None:
            return self.default
        if self._tf is None:
            from timezonefinder import TimezoneFinder
            self._tf = TimezoneFinder()
        try:
            name = self._tf.timezone_at(lat=lat, lng=lon)
            return ZoneInfo(name) if name else self.default
        except Exception:  # noqa: BLE001 - unusable coordinates: fall back to the default zone
            return self.default


def valid_gps(lat, lon) -> tuple[float | None, float | None]:
    """Decimal coordinates, or (None, None) for missing, malformed, out-of-range or 0,0 values."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if abs(la) > 90 or abs(lo) > 180 or (abs(la) < 1e-6 and abs(lo) < 1e-6):
        return None, None
    return la, lo


def offset_str(d: dt.datetime) -> str:
    off = d.utcoffset() or dt.timedelta(0)
    sign = "+" if off >= dt.timedelta(0) else "-"
    mins = abs(int(off.total_seconds())) // 60
    return f"{sign}{mins // 60:02d}:{mins % 60:02d}"


def fmt(d: dt.datetime) -> str:
    return d.strftime("%Y:%m:%d %H:%M:%S")


# ----------------------------------------------------------------------------- plan

def plan_file(path: str, existing: dict, side: dict, zones: Zones) -> dict:
    """Decides what to add to one file. Returns a plan row (tags as [tag, value, undo] lists)."""
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    is_video = ext in VIDEO or ext == ".avi"
    writable = ext not in NOT_WRITABLE
    tags: list[list[str]] = []
    notes: list[str] = []

    def add(tag: str, value: str, undo: list[str] | None = None):
        tags.append([tag, value, undo or [f"-{tag}="]])

    # Where was it taken? File GPS first, then the sidecar.
    lat, lon = valid_gps(existing.get("Composite:GPSLatitude", existing.get("EXIF:GPSLatitude")),
                         existing.get("Composite:GPSLongitude", existing.get("EXIF:GPSLongitude")))
    coords = existing.get("QuickTime:GPSCoordinates")
    if lat is None and coords:
        parts = str(coords).replace(",", " ").split()
        if len(parts) >= 2:
            lat, lon = valid_gps(parts[0], parts[1])
    # Any GPS field present (even unusable) counts as "the file has GPS": never overwrite it.
    file_has_gps = lat is not None or any(
        existing.get(k) not in (None, "") for k in ("EXIF:GPSLatitude", "Composite:GPSLatitude", "QuickTime:GPSCoordinates"))
    if lat is None and side["lat"] is not None:
        lat, lon = side["lat"], side["lon"]
    zone = zones.at(lat, lon)

    # --- date taken: existing value wins
    local = None
    source = ""
    if is_video:
        qt = existing.get("QuickTime:CreateDate")
        if qt and not str(qt).startswith("0000"):
            m = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})([+-]\d{2}:\d{2}|Z)?", str(qt))
            if m:
                base = dt.datetime(*map(int, m.groups()[:6]))
                if m.group(7) and m.group(7) != "Z":
                    sign = 1 if m.group(7)[0] == "+" else -1
                    h, mi = map(int, m.group(7)[1:].split(":"))
                    local = base.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=h, minutes=mi)))
                else:
                    local = base.replace(tzinfo=dt.timezone.utc).astimezone(zone)
                source = "existing"
    else:
        dto = parse_exif_date(existing.get("EXIF:DateTimeOriginal")) or parse_exif_date(existing.get("XMP:DateTimeOriginal"))
        if dto:
            off = existing.get("EXIF:OffsetTimeOriginal")
            if off and re.match(r"^[+-]\d{2}:\d{2}$", str(off)):
                sign = 1 if off[0] == "+" else -1
                h, mi = map(int, off[1:].split(":"))
                local = dto.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=h, minutes=mi)))
            else:
                local = dto.replace(tzinfo=zone)
            source = "existing"

    if local is None:
        from_name, name_source = name_date(name)
        if side["taken"] is not None:
            local = dt.datetime.fromtimestamp(side["taken"], zone)
            source = "sidecar"
            # Google uses the upload time when a file has no date of its own, so a sidecar date well
            # AFTER the name date is an upload date: trust the name. An earlier sidecar date comes
            # from the file itself (e.g. an old video forwarded later): trust the sidecar.
            if from_name and (local.replace(tzinfo=None) - from_name).total_seconds() > 36 * 3600:
                local, source = from_name.replace(tzinfo=zone), name_source
                notes.append("sidecar date disagrees with file name; used file name")
        elif from_name:
            local, source = from_name.replace(tzinfo=zone), name_source
        if name_source == "whatsapp-name" and source == "whatsapp-name":
            notes.append("date only; time set to 12:00")

        if local is not None and writable:
            stamp, off = fmt(local), offset_str(local)
            if is_video:
                for t in ("QuickTime:CreateDate", "QuickTime:ModifyDate", "QuickTime:TrackCreateDate", "QuickTime:MediaCreateDate"):
                    add(t, stamp + off, [f"-{t}=0000:00:00 00:00:00"])
                if not existing.get("QuickTime:CreationDate"):
                    add("Keys:CreationDate", stamp + off)
            else:
                if ext in EXIF_TYPES:
                    add("EXIF:DateTimeOriginal", stamp)
                    if not existing.get("EXIF:CreateDate"):
                        add("EXIF:CreateDate", stamp)
                    if not existing.get("EXIF:OffsetTimeOriginal"):
                        add("EXIF:OffsetTimeOriginal", off)
                if ext in XMP_DATE_TYPES:
                    add("XMP-exif:DateTimeOriginal", stamp + off)

    # --- GPS (only when the file has none)
    if writable and not file_has_gps and side["lat"] is not None:
        if is_video:
            add("Keys:GPSCoordinates", f"{side['lat']:.6f}, {side['lon']:.6f}, {side['alt']:.1f}")
        elif ext in EXIF_TYPES:
            add("EXIF:GPSLatitude*", f"{side['lat']:.6f}", ["-EXIF:GPSLatitude=", "-EXIF:GPSLatitudeRef="])
            add("EXIF:GPSLongitude*", f"{side['lon']:.6f}", ["-EXIF:GPSLongitude=", "-EXIF:GPSLongitudeRef="])
            add("EXIF:GPSAltitude*", f"{side['alt']:.1f}", ["-EXIF:GPSAltitude=", "-EXIF:GPSAltitudeRef="])

    # --- description
    has_desc = existing.get("XMP:Description") or existing.get("EXIF:ImageDescription")
    if writable and side["description"] and not has_desc:
        add("XMP-dc:Description", side["description"])
        if ext in {".jpg", ".jpeg", ".heic"}:
            add("EXIF:ImageDescription", side["description"])

    # --- people tagged in Google Photos
    if writable and ext != ".gif":
        have_people = set(as_list(existing.get("XMP:PersonInImage")))
        have_subjects = set(as_list(existing.get("XMP:Subject")))
        for person in side["people"]:
            if person not in have_people:
                tags.append(["XMP-iptcExt:PersonInImage+", person, [f"-XMP-iptcExt:PersonInImage-={person}"]])
            if person not in have_subjects:
                tags.append(["XMP-dc:Subject+", person, [f"-XMP-dc:Subject-={person}"]])

    file_dates = ""
    if local is not None:
        file_dates = fmt(local) + offset_str(local)

    return {
        "path": path, "type": ext.lstrip("."), "writable": writable,
        "date_source": source or "none", "date_taken": file_dates, "timezone": getattr(zone, "key", str(zone)),
        "tags": tags, "file_dates": file_dates,
        "old_modify": existing.get("File:FileModifyDate", ""), "old_create": existing.get("File:FileCreateDate", ""),
        "notes": "; ".join(notes),
    }


def build_plan(source: str, exe: str, default_tz: str, limit: int = 0, workers: int = 4,
               only: list[str] | None = None) -> list[dict]:
    media = core.scan_library(source, MEDIA)
    if only:
        wanted = {os.path.normcase(os.path.abspath(p)) for p in only}
        media = [m for m in media if os.path.normcase(m.path) in wanted]
    if limit:
        media = media[:limit]
    print(f"{len(media):,} photos and videos under {source}. Reading their metadata ...", flush=True)

    chunks = [media[i:i + 100] for i in range(0, len(media), 100)]
    existing: dict[str, dict] = {}
    lock = threading.Lock()
    tools: dict[int, ExifTool] = {}
    done = [0]

    def read_chunk(chunk):
        tid = threading.get_ident()
        with lock:
            if tid not in tools:
                tools[tid] = ExifTool(exe)
        rows = tools[tid].read_json([m.path for m in chunk], READ_TAGS)
        with lock:
            for r in rows:
                existing[os.path.normcase(os.path.abspath(r["SourceFile"]))] = r
            done[0] += len(chunk)
            if done[0] % 2000 < len(chunk):
                print(f"  {done[0]:,}/{len(media):,}", flush=True)

    try:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(read_chunk, chunks))
    finally:
        for t in tools.values():
            t.close()

    zones = Zones(default_tz)
    plan = []
    for m in media:
        ex_row = existing.get(os.path.normcase(os.path.abspath(m.path)), {})
        plan.append(plan_file(m.path, ex_row, read_sidecar_details(m.sidecars), zones))
    return plan


def summarize(plan: list[dict]) -> dict:
    s = {
        "files": len(plan),
        "files with tags to add": sum(1 for p in plan if p["tags"]),
        "date added": sum(1 for p in plan if p["date_source"] not in ("existing", "none") and p["tags"]),
        "  from Google sidecar": sum(1 for p in plan if p["date_source"] == "sidecar" and p["tags"]),
        "  from WhatsApp name": sum(1 for p in plan if p["date_source"] == "whatsapp-name" and p["tags"]),
        "  from camera-style name": sum(1 for p in plan if p["date_source"] == "camera-name" and p["tags"]),
        "date already in file": sum(1 for p in plan if p["date_source"] == "existing"),
        "no date found": sum(1 for p in plan if p["date_source"] == "none"),
        "GPS added": sum(1 for p in plan if any("GPS" in t[0] for t in p["tags"])),
        "description added": sum(1 for p in plan if any(t[0] == "XMP-dc:Description" for t in p["tags"])),
        "people added": sum(1 for p in plan if any(t[0].startswith("XMP-iptcExt:PersonInImage") for t in p["tags"])),
        "file dates set": sum(1 for p in plan if p["file_dates"]),
        "not writable (AVI/BMP: file dates only)": sum(1 for p in plan if not p["writable"]),
    }
    return s


def write_plan_csv(plan: list[dict], path: str):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Path", "Type", "DateSource", "DateTaken", "Timezone", "TagsToAdd", "FileDates", "Notes"])
        for p in plan:
            w.writerow([p["path"], p["type"], p["date_source"], p["date_taken"], p["timezone"],
                        " | ".join(f"{t[0]}={t[1]}" for t in p["tags"]), p["file_dates"], p["notes"]])


# ----------------------------------------------------------------------------- apply / undo

def tag_args(tags: list[list[str]]) -> list[str]:
    return [f"-{t}={v}" for t, v, _ in tags]


def read_check(et: ExifTool, path: str) -> dict:
    rows = et.read_json([path], ["-ImageDataHash"] + READ_TAGS)
    return rows[0] if rows else {}


def rewrite(et: ExifTool, path: str, args: list[str], expect: list[list[str]], writable: bool,
            ignore_minor: bool = False, repair: bool = False) -> str:
    """Applies ExifTool args to a temporary copy, verifies it, then replaces the original.

    Returns '' on success or an error message (original untouched on error).
    """
    if not writable:
        out, err = et.run(["-overwrite_original"] + [a for a in args if a.startswith("-File")] + [path])
        return "" if "1 image files updated" in out else (err or out).strip()[:300]

    before = read_check(et, path).get("File:ImageDataHash")
    folder, name = os.path.split(path)
    tmp = os.path.join(folder, f".embed-{uuid.uuid4().hex[:8]}-{name}")
    try:
        # -m: write despite minor problems in existing metadata. Repair: rebuild the whole
        # metadata block from its readable tags (ExifTool FAQ 20); drops unreadable parts
        # such as a broken embedded preview image.
        extra = ["-m"] if ignore_minor else []
        if repair:
            extra += ["-all=", "-tagsfromfile", "@", "-all:all", "-unsafe", "-icc_profile"]
        out, err = et.run(["-o", tmp] + extra + args + [path])
        if not os.path.exists(tmp):
            return ("ExifTool did not write the file: " + (err or out)).strip()[:300]
        after = read_check(et, tmp)
        if before and after.get("File:ImageDataHash") != before:
            return "picture/video data fingerprint changed - not replaced"
        for tag, _, _ in expect:
            key = VERIFY.get(tag)
            if key and str(after.get(key, "0000")).startswith("0000"):
                return f"{tag} did not read back - not replaced"
        os.replace(tmp, path)
        tmp = ""
        return ""
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)  # our own temporary copy; the original is untouched


def file_date_args(stamp: str) -> list[str]:
    return [f"-FileModifyDate={stamp}", f"-FileCreateDate={stamp}"] if stamp else []


def apply(plan: list[dict], exe: str, log_path: str, workers: int, ignore_minor: bool = False,
          repair: bool = False) -> tuple[int, int]:
    todo = [p for p in plan if p["tags"] or p["file_dates"]]
    lock = threading.Lock()
    tools: dict[int, ExifTool] = {}
    counts = {"ok": 0, "failed": 0, "n": 0}
    started = time.time()

    with open(log_path, "w", newline="", encoding="utf-8-sig") as logf:
        log = csv.writer(logf)
        log.writerow(["Path", "Result", "Added", "UndoArgs", "OldFileModifyDate", "OldFileCreateDate", "Detail"])

        def work(p):
            tid = threading.get_ident()
            with lock:
                if tid not in tools:
                    tools[tid] = ExifTool(exe)
            et = tools[tid]
            args = tag_args(p["tags"]) + file_date_args(p["file_dates"])
            try:
                # Only file dates to set: done in place; the file content is not rewritten.
                error = rewrite(et, p["path"], args, p["tags"], p["writable"] and bool(p["tags"]), ignore_minor, repair)
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"[:300]
            undo = [a for _, _, u in p["tags"] for a in u]
            with lock:
                counts["n"] += 1
                counts["ok" if not error else "failed"] += 1
                log.writerow([p["path"], "OK" if not error else "FAILED",
                              " | ".join(f"{t}={v}" for t, v, _ in p["tags"]) + (f" | FileDates={p['file_dates']}" if p["file_dates"] else ""),
                              json.dumps(undo), p["old_modify"], p["old_create"], error])
                if error:
                    print(f"  FAILED: {p['path']} :: {error}", flush=True)
                if counts["n"] % 500 == 0 or counts["n"] == len(todo):
                    rate = counts["n"] / max(time.time() - started, 1e-6)
                    print(f"  {counts['n']:,}/{len(todo):,}  {rate:.1f} files/s  about {(len(todo) - counts['n']) / rate / 60:.0f} min left", flush=True)
                    logf.flush()

        try:
            with ThreadPoolExecutor(workers) as ex:
                list(ex.map(work, todo))
        finally:
            for t in tools.values():
                t.close()
    return counts["ok"], counts["failed"]


def undo(log_path: str, exe: str, workers: int) -> tuple[int, int]:
    with open(log_path, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["Result"] == "OK"]
    lock = threading.Lock()
    tools: dict[int, ExifTool] = {}
    counts = {"ok": 0, "failed": 0}

    def work(r):
        tid = threading.get_ident()
        with lock:
            if tid not in tools:
                tools[tid] = ExifTool(exe)
        args = list(json.loads(r["UndoArgs"]))
        args += [f"-FileModifyDate={r['OldFileModifyDate']}"] if r["OldFileModifyDate"] else []
        args += [f"-FileCreateDate={r['OldFileCreateDate']}"] if r["OldFileCreateDate"] else []
        writable = os.path.splitext(r["Path"])[1].lower() not in NOT_WRITABLE and any(not a.startswith("-File") for a in args)
        try:
            error = rewrite(tools[tid], r["Path"], args, [], writable)
        except Exception as e:  # noqa: BLE001
            error = str(e)
        with lock:
            counts["ok" if not error else "failed"] += 1
            if error:
                print(f"  UNDO FAILED: {r['Path']} :: {error}", flush=True)

    try:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(work, rows))
    finally:
        for t in tools.values():
            t.close()
    return counts["ok"], counts["failed"]


# ----------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "apply", "undo"])
    ap.add_argument("--source", default=r"C:\Media\Imports")
    ap.add_argument("--report-root", default=r"C:\Media\DedupeReports")
    ap.add_argument("--default-tz", default="Asia/Hong_Kong", help="timezone for files without GPS (default Asia/Hong_Kong)")
    ap.add_argument("--expected", type=int, default=-1, help="apply: number of files to change; must match the plan")
    ap.add_argument("--log", help="undo: the EmbedLog_*.csv written by apply")
    ap.add_argument("--limit", type=int, default=0, help="only the first N files (for trials)")
    ap.add_argument("--only", action="append", default=[], help="only this file (repeatable)")
    ap.add_argument("--ignore-minor-errors", action="store_true",
                    help="let ExifTool write despite minor problems in a file's existing metadata (-m)")
    ap.add_argument("--repair-metadata", action="store_true",
                    help="rebuild damaged metadata from its readable tags before adding (use with --only)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--exiftool", default="")
    args = ap.parse_args(argv)

    exe = args.exiftool or find_exiftool()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.report_root, exist_ok=True)

    if args.command == "undo":
        if not args.log:
            ap.error("undo needs --log")
        ok, failed = undo(args.log, exe, args.workers)
        print(f"Undo finished: {ok:,} files restored, {failed:,} failed.")
        return 1 if failed else 0

    source = os.path.realpath(args.source)
    plan = build_plan(source, exe, args.default_tz, args.limit, args.workers, args.only)
    plan_csv = os.path.join(args.report_root, f"EmbedPlan_{stamp}.csv")
    write_plan_csv(plan, plan_csv)

    summary = summarize(plan)
    to_change = sum(1 for p in plan if p["tags"] or p["file_dates"])
    print()
    for k, v in summary.items():
        print(f"  {k:<42} {v:>8,}")
    print(f"  {'FILES TO CHANGE':<42} {to_change:>8,}")
    print(f"Plan: {plan_csv}")

    if args.command == "plan":
        print("Plan only - nothing was changed.")
        return 0

    if args.expected != to_change:
        print(f"The plan has {to_change:,} files to change, not --expected {args.expected:,}. Nothing was changed.")
        return 2

    log_path = os.path.join(args.report_root, f"EmbedLog_{stamp}.csv")
    print(f"Writing metadata to {to_change:,} files. Undo record: {log_path}")
    if args.repair_metadata and not args.only:
        ap.error("--repair-metadata rewrites all metadata; name the files with --only")
    ok, failed = apply(plan, exe, log_path, args.workers, args.ignore_minor_errors, args.repair_metadata)
    print(f"Done: {ok:,} files updated, {failed:,} failed (failed files were left unchanged).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
