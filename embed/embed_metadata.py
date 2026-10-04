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

LIST_VERIFY = {"XMP-dc:Subject+": "XMP:Subject", "XMP-iptcExt:PersonInImage+": "XMP:PersonInImage"}

READ_TAGS = [
    "-EXIF:DateTimeOriginal", "-EXIF:CreateDate", "-EXIF:OffsetTimeOriginal",
    "-XMP:DateTimeOriginal", "-QuickTime:CreateDate", "-QuickTime:CreationDate", "-QuickTime:ModifyDate",
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


def parse_video_date(value, zone: ZoneInfo) -> dt.datetime | None:
    """A QuickTime date as an aware local time; None for missing or placeholder dates
    (before 1971, or more than a year in the future such as the 2036 overflow value)."""
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([+-]\d{2}:\d{2}|Z)?", str(value or ""))
    if not m or int(m.group(1)) < 1971 or int(m.group(1)) > dt.date.today().year + 1:
        return None
    try:
        base = dt.datetime(*map(int, m.groups()[:6]))
    except ValueError:
        return None
    if m.group(7) and m.group(7) != "Z":
        sign = 1 if m.group(7)[0] == "+" else -1
        h, mi = map(int, m.group(7)[1:].split(":"))
        return base.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=h, minutes=mi)))
    return base.replace(tzinfo=dt.timezone.utc).astimezone(zone)


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

def plan_file(path: str, existing: dict, side: dict, zones: Zones, force_name_date: bool = False,
              fallback_date: str = "") -> dict:
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
    fill_video_dates = forced = False
    if force_name_date:
        from_name, name_source = name_date(name)
        if from_name is not None and name_source == "camera-name":
            local, source, forced = from_name.replace(tzinfo=zone), "camera-name (forced)", True
            notes.append("date taken from the file name on request; existing date replaced")
    if forced:
        pass
    elif is_video:
        local = parse_video_date(existing.get("QuickTime:CreateDate"), zone)
        if local is not None:
            source = "existing"
        else:
            # A zero/1970 CreateDate is a placeholder. Apple's CreationDate holds the real date
            # when present; otherwise the sidecar date if not after the file's ModifyDate (later
            # sidecar dates are upload dates); otherwise ModifyDate (an edit/export time, close).
            creation = parse_video_date(existing.get("QuickTime:CreationDate"), zone)
            modify = parse_video_date(existing.get("QuickTime:ModifyDate"), zone)
            side_local = dt.datetime.fromtimestamp(side["taken"], zone) if side["taken"] is not None else None
            if creation is not None:
                local, source, fill_video_dates = creation, "existing", True
                notes.append("video date was a placeholder; used Apple CreationDate")
            elif modify is not None:
                fill_video_dates = True
                if side_local is not None and side_local <= modify + dt.timedelta(days=1):
                    local, source = side_local, "sidecar"
                    notes.append("video date was a placeholder; used sidecar date")
                else:
                    local, source = modify, "existing"
                    notes.append("video date was a placeholder; approximate date from its last edit")
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
        elif fallback_date:
            # Last resort, e.g. the start date of the album the photo came from.
            local, source = dt.datetime.fromisoformat(fallback_date).replace(tzinfo=zone), "fallback (approximate)"
            notes.append("no date anywhere; approximate date supplied (e.g. album start)")
        if name_source == "whatsapp-name" and source == "whatsapp-name":
            notes.append("date only; time set to 12:00")

    def undo_to(tag: str, old_key: str, empty: str = "") -> list[str]:
        old = existing.get(old_key)
        return [f"-{tag}={old}"] if forced and old else [f"-{tag}={empty}"]

    if local is not None and writable and (source != "existing" or fill_video_dates or forced):
        stamp, off = fmt(local), offset_str(local)
        if is_video:
            keep_modify = parse_video_date(existing.get("QuickTime:ModifyDate"), zone) is not None
            for t in ("QuickTime:CreateDate", "QuickTime:ModifyDate", "QuickTime:TrackCreateDate", "QuickTime:MediaCreateDate"):
                if t == "QuickTime:ModifyDate" and keep_modify and not forced:
                    continue  # a real ModifyDate (e.g. when the clip was edited) is left as it is
                add(t, stamp + off, undo_to(t, t, "0000:00:00 00:00:00") if t == "QuickTime:CreateDate" else [f"-{t}=0000:00:00 00:00:00"])
            if forced or not existing.get("QuickTime:CreationDate"):
                add("Keys:CreationDate", stamp + off, undo_to("Keys:CreationDate", "QuickTime:CreationDate"))
        else:
            if ext in EXIF_TYPES:
                add("EXIF:DateTimeOriginal", stamp, undo_to("EXIF:DateTimeOriginal", "EXIF:DateTimeOriginal"))
                if forced or not existing.get("EXIF:CreateDate"):
                    add("EXIF:CreateDate", stamp, undo_to("EXIF:CreateDate", "EXIF:CreateDate"))
                if forced or not existing.get("EXIF:OffsetTimeOriginal"):
                    add("EXIF:OffsetTimeOriginal", off, undo_to("EXIF:OffsetTimeOriginal", "EXIF:OffsetTimeOriginal"))
            if ext in XMP_DATE_TYPES:
                add("XMP-exif:DateTimeOriginal", stamp + off, undo_to("XMP-exif:DateTimeOriginal", "XMP:DateTimeOriginal"))

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
               only: list[str] | None = None, force_name_date: bool = False,
               fallback_dates: dict[str, str] | None = None) -> list[dict]:
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
        fallback = (fallback_dates or {}).get(os.path.normcase(os.path.abspath(m.path)), "")
        plan.append(plan_file(m.path, ex_row, read_sidecar_details(m.sidecars), zones, force_name_date, fallback))
    return plan


def summarize(plan: list[dict]) -> dict:
    s = {
        "files": len(plan),
        "files with tags to add": sum(1 for p in plan if p["tags"]),
        "date added": sum(1 for p in plan if p["date_source"] not in ("existing", "none") and p["tags"]),
        "  from Google sidecar": sum(1 for p in plan if p["date_source"] == "sidecar" and p["tags"]),
        "  from WhatsApp name": sum(1 for p in plan if p["date_source"] == "whatsapp-name" and p["tags"]),
        "  from camera-style name": sum(1 for p in plan if p["date_source"] == "camera-name" and p["tags"]),
        "  approximate (supplied fallback)": sum(1 for p in plan if p["date_source"].startswith("fallback") and p["tags"]),
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
        for tag, value, _ in expect:
            key = VERIFY.get(tag)
            if key and str(after.get(key, "0000")).startswith("0000"):
                return f"{tag} did not read back - not replaced"
            list_key = LIST_VERIFY.get(tag)
            if list_key and value not in as_list(after.get(list_key)):
                return f"{tag} {value!r} did not read back - not replaced"
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
            if p.get("keep_dates"):
                # Rewriting creates a new file; carry the current Windows dates over unchanged.
                args += [f"-FileModifyDate={p['old_modify']}"] if p["old_modify"] else []
                args += [f"-FileCreateDate={p['old_create']}"] if p["old_create"] else []
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


def copy_metadata(pairs: dict[str, str], exe: str, log_path: str, backup_root: str) -> tuple[int, int]:
    """Copies all metadata from source to target (e.g. a better-quality copy replacing an
    older, already-tagged one). Each target is first copied to backup_root; the rewrite is
    verified like any other (picture fingerprint unchanged, date read back).
    pairs: {target path: source path}.
    """
    et = ExifTool(exe)
    ok = failed = 0
    try:
        with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Target", "Source", "Result", "BackupOfTarget", "Detail"])
            for target, source in pairs.items():
                backup = os.path.join(backup_root, os.path.splitdrive(target)[1].lstrip("\\/"))
                try:
                    if os.path.exists(backup):
                        raise RuntimeError("a backup of this file already exists; not overwriting it")
                    os.makedirs(os.path.dirname(backup), exist_ok=True)
                    shutil.copy2(target, backup)
                    src = read_check(et, source)
                    expect = [["EXIF:DateTimeOriginal", "", []]] if src.get("EXIF:DateTimeOriginal") else []
                    args = ["-tagsfromfile", source, "-all:all", "-unsafe",
                            "-FileModifyDate<FileModifyDate", "-FileCreateDate<FileCreateDate"]
                    error = rewrite(et, target, args, expect, True)
                    if not error and src.get("EXIF:DateTimeOriginal"):
                        if read_check(et, target).get("EXIF:DateTimeOriginal") != src["EXIF:DateTimeOriginal"]:
                            error = "date did not match the source after copying"
                except Exception as e:  # noqa: BLE001
                    error = str(e)[:300]
                ok += not error
                failed += bool(error)
                w.writerow([target, source, "OK" if not error else "FAILED", backup, error])
                if error:
                    print(f"  FAILED: {target} :: {error}", flush=True)
    finally:
        et.close()
    return ok, failed


def build_keyword_plan(keyword_map: dict[str, list[str]], exe: str) -> list[dict]:
    """Plan rows that add keywords (XMP dc:Subject) to files, e.g. album names.

    keyword_map: {file path: [keyword or "person:Name", ...]}. Values already present are skipped;
    files ExifTool cannot write (AVI/BMP) are left out. Windows dates are kept.
    """
    paths = [p for p in keyword_map if os.path.splitext(p)[1].lower() not in NOT_WRITABLE and os.path.exists(p)]
    et = ExifTool(exe)
    existing = {}
    try:
        for i in range(0, len(paths), 100):
            for r in et.read_json(paths[i:i + 100], READ_TAGS):
                existing[os.path.normcase(os.path.abspath(r["SourceFile"]))] = r
    finally:
        et.close()

    plan = []
    for p in paths:
        ex = existing.get(os.path.normcase(os.path.abspath(p)), {})
        have = set(as_list(ex.get("XMP:Subject")))
        have_people = set(as_list(ex.get("XMP:PersonInImage")))
        tags = []
        for item in keyword_map[p]:
            # "person:Name" adds a tagged person (PersonInImage) as well as the keyword.
            k = item[len("person:"):] if item.startswith("person:") else item
            if item.startswith("person:") and k not in have_people:
                tags.append(["XMP-iptcExt:PersonInImage+", k, [f"-XMP-iptcExt:PersonInImage-={k}"]])
            if k not in have:
                tags.append(["XMP-dc:Subject+", k, [f"-XMP-dc:Subject-={k}"]])
        if not tags:
            continue
        plan.append({"path": p, "type": os.path.splitext(p)[1].lstrip("."), "writable": True,
                     "date_source": "", "date_taken": "", "timezone": "", "tags": tags, "file_dates": "",
                     "keep_dates": True, "old_modify": ex.get("File:FileModifyDate", ""),
                     "old_create": ex.get("File:FileCreateDate", ""), "notes": ""})
    return plan


# ----------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "apply", "undo", "keywords", "copy-meta"],
                    help="keywords: add keywords from --map (plan only unless --apply); "
                         "copy-meta: copy all metadata, --map {target: source} (needs --apply and --expected)")
    ap.add_argument("--backup-root", default=r"C:\Media\DuplicateQuarantine-3\BeforeMetadataCopy",
                    help="copy-meta: where each target's original is kept")
    ap.add_argument("--map", help="keywords: JSON file {file path: [keyword, ...]}")
    ap.add_argument("--apply", action="store_true", help="keywords: write them (needs --expected)")
    ap.add_argument("--source", default=r"C:\Media\Imports")
    ap.add_argument("--report-root", default=r"C:\Media\DedupeReports")
    ap.add_argument("--default-tz", default="Asia/Hong_Kong", help="timezone for files without GPS (default Asia/Hong_Kong)")
    ap.add_argument("--expected", type=int, default=-1, help="apply: number of files to change; must match the plan")
    ap.add_argument("--log", help="undo: the EmbedLog_*.csv written by apply")
    ap.add_argument("--limit", type=int, default=0, help="only the first N files (for trials)")
    ap.add_argument("--only", action="append", default=[], help="only this file (repeatable)")
    ap.add_argument("--ignore-minor-errors", action="store_true",
                    help="let ExifTool write despite minor problems in a file's existing metadata (-m)")
    ap.add_argument("--fallback-dates", help="JSON {file path: 'YYYY-MM-DDTHH:MM:SS'} used only when a file has no date anywhere")
    ap.add_argument("--force-name-date", action="store_true",
                    help="replace the date with the one in a camera-style file name (use with --only)")
    ap.add_argument("--repair-metadata", action="store_true",
                    help="rebuild damaged metadata from its readable tags before adding (use with --only)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--exiftool", default="")
    args = ap.parse_args(argv)

    exe = args.exiftool or find_exiftool()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.report_root, exist_ok=True)

    if args.command == "copy-meta":
        if not args.map:
            ap.error("copy-meta needs --map")
        with open(args.map, encoding="utf-8") as f:
            pairs = json.load(f)
        print(f"Copy metadata: {len(pairs):,} files.")
        if not args.apply:
            print("Plan only - nothing was changed.")
            return 0
        if args.expected != len(pairs):
            print(f"The map has {len(pairs):,} files, not --expected {args.expected:,}. Nothing was changed.")
            return 2
        log_path = os.path.join(args.report_root, f"EmbedLog_{stamp}_copymeta.csv")
        ok, failed = copy_metadata(pairs, exe, log_path, args.backup_root)
        print(f"Done: {ok:,} files updated, {failed:,} failed. Log: {log_path}; originals kept in {args.backup_root}")
        return 1 if failed else 0

    if args.command == "keywords":
        if not args.map:
            ap.error("keywords needs --map")
        with open(args.map, encoding="utf-8") as f:
            plan = build_keyword_plan(json.load(f), exe)
        added = sum(len(p["tags"]) for p in plan)
        print(f"Keywords: {added:,} to add across {len(plan):,} files.")
        if not args.apply:
            print("Plan only - nothing was changed.")
            return 0
        if args.expected != len(plan):
            print(f"The plan has {len(plan):,} files to change, not --expected {args.expected:,}. Nothing was changed.")
            return 2
        log_path = os.path.join(args.report_root, f"EmbedLog_{stamp}_keywords.csv")
        print(f"Writing keywords to {len(plan):,} files. Undo record: {log_path}")
        ok, failed = apply(plan, exe, log_path, args.workers)
        print(f"Done: {ok:,} files updated, {failed:,} failed (failed files were left unchanged).")
        return 1 if failed else 0

    if args.command == "undo":
        if not args.log:
            ap.error("undo needs --log")
        ok, failed = undo(args.log, exe, args.workers)
        print(f"Undo finished: {ok:,} files restored, {failed:,} failed.")
        return 1 if failed else 0

    source = os.path.realpath(args.source)
    if args.force_name_date and not args.only:
        ap.error("--force-name-date replaces existing dates; name the files with --only")
    fallback_dates = {}
    if args.fallback_dates:
        with open(args.fallback_dates, encoding="utf-8") as f:
            fallback_dates = {os.path.normcase(os.path.abspath(p)): v for p, v in json.load(f).items()}
    plan = build_plan(source, exe, args.default_tz, args.limit, args.workers, args.only, args.force_name_date, fallback_dates)
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
    while os.path.exists(log_path):  # never reuse an undo log
        stamp += "x"
        log_path = os.path.join(args.report_root, f"EmbedLog_{stamp}.csv")
    print(f"Writing metadata to {to_change:,} files. Undo record: {log_path}")
    if args.repair_metadata and not args.only:
        ap.error("--repair-metadata rewrites all metadata; name the files with --only")
    ok, failed = apply(plan, exe, log_path, args.workers, args.ignore_minor_errors, args.repair_metadata)
    print(f"Done: {ok:,} files updated, {failed:,} failed (failed files were left unchanged).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
