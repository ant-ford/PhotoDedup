"""Tests for embed_metadata.py against real ExifTool on a temporary fixture.

    python -m unittest discover -s embed/tests -v
Needs ExifTool, Pillow, pillow-heif and timezonefinder (the triage .venv has them).
"""

import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import embed_metadata as em  # noqa: E402

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

EXE = em.find_exiftool()
GP = os.path.join("partA", "Takeout", "Google Photos", "Photos from 2018")
SAMPLE_VIDEO = next(iter(sorted(glob.glob(r"C:\Media\Imports\**\*.mp4", recursive=True)
                                + glob.glob(r"C:\Media\Library\**\*.mp4", recursive=True),
                                key=os.path.getsize)), None)


def read(path, *tags):
    out = subprocess.run([EXE, "-j", "-G0", "-n", "-api", "QuickTimeUTC=1", "-api", "ImageHashType=SHA256", *tags, path],
                         capture_output=True, text=True, encoding="utf-8").stdout
    return json.loads(out)[0]


def sidecar(path, **fields):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"title": os.path.basename(path), **fields}, f)


class EmbedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="embed_test_"))
        self.src = os.path.join(self.tmp, "Imports")
        self.rep = os.path.join(self.tmp, "Reports")
        os.makedirs(os.path.join(self.src, GP))
        os.makedirs(os.path.join(self.src, "WhatsApp-001"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.src, *parts)

    def build(self):
        img = Image.new("RGB", (64, 48), (200, 120, 40))
        # 1. No EXIF date; sidecar has UTC time, GPS in Hong Kong, description and people.
        img.save(self.p(GP, "plain.jpg"), quality=90)
        sidecar(self.p(GP, "plain.jpg.supplemental-metadata.json"),
                photoTakenTime={"timestamp": "1530860833"},  # 2018-07-06 07:07:13 UTC
                geoData={"latitude": 22.271106, "longitude": 114.130844, "altitude": 182.2},
                description="Beach day", people=[{"name": "Alex"}, {"name": "Sam"}])
        # 2. Camera date already present: must not change; sidecar time differs.
        ex = Image.Exif()
        ex[0x8769] = {0x9003: "2018:07:06 09:00:00"}
        img.save(self.p(GP, "camera.jpg"), exif=ex, quality=90)
        sidecar(self.p(GP, "camera.jpg.supplemental-metadata.json"), photoTakenTime={"timestamp": "1530860833"})
        # 3. WhatsApp image with no sidecar: date from name, 12:00, Hong Kong.
        img.save(self.p("WhatsApp-001", "IMG-20230505-WA0013.jpg"), quality=70)
        # 4. PNG with sidecar.
        img.save(self.p(GP, "graphic.png"))
        sidecar(self.p(GP, "graphic.png.supplemental-metadata.json"), photoTakenTime={"timestamp": "1530860833"})
        # 5. BMP: not writable, file dates only.
        img.save(self.p(GP, "old.bmp"))
        sidecar(self.p(GP, "old.bmp.supplemental-metadata.json"), photoTakenTime={"timestamp": "1530860833"})
        # 6. Sidecar date (upload time) far from a WhatsApp name date: name wins.
        img.save(self.p(GP, "IMG-20170101-WA0001.jpg"), quality=70)
        sidecar(self.p(GP, "IMG-20170101-WA0001.jpg.supplemental-metadata.json"), photoTakenTime={"timestamp": "1700000000"})
        # 7. Sidecar date EARLIER than the WhatsApp name (forwarded old photo): sidecar wins.
        img.save(self.p(GP, "IMG-20190101-WA0002.jpg"), quality=70)
        sidecar(self.p(GP, "IMG-20190101-WA0002.jpg.supplemental-metadata.json"), photoTakenTime={"timestamp": "1530860833"})
        if HEIF:
            img.save(self.p(GP, "phone.heic"))
            sidecar(self.p(GP, "phone.heic.supplemental-metadata.json"), photoTakenTime={"timestamp": "1530860833"})

    def run_cmd(self, *args):
        return em.main([*args, "--source", self.src, "--report-root", self.rep, "--workers", "2"])

    def latest(self, pattern):
        return sorted(glob.glob(os.path.join(self.rep, pattern)))[-1]

    def test_plan_changes_nothing(self):
        self.build()
        before = {f: os.path.getmtime(f) for f in glob.glob(self.p("**", "*.*"), recursive=True)}
        self.assertEqual(self.run_cmd("plan"), 0)
        after = {f: os.path.getmtime(f) for f in before}
        self.assertEqual(before, after)
        with open(self.latest("EmbedPlan_*.csv"), encoding="utf-8-sig") as f:
            rows = {os.path.basename(r["Path"]): r for r in csv.DictReader(f)}
        self.assertEqual(rows["plain.jpg"]["DateSource"], "sidecar")
        self.assertEqual(rows["plain.jpg"]["DateTaken"], "2018:07:06 15:07:13+08:00")
        self.assertEqual(rows["camera.jpg"]["DateSource"], "existing")
        self.assertNotIn("DateTimeOriginal", rows["camera.jpg"]["TagsToAdd"])
        self.assertEqual(rows["IMG-20230505-WA0013.jpg"]["DateTaken"], "2023:05:05 12:00:00+08:00")
        self.assertEqual(rows["IMG-20170101-WA0001.jpg"]["DateSource"], "whatsapp-name", "later sidecar = upload date")
        self.assertEqual(rows["IMG-20190101-WA0002.jpg"]["DateSource"], "sidecar", "earlier sidecar = real date")

    def test_apply_verify_and_undo(self):
        self.build()
        files = [f for f in glob.glob(self.p("**", "*.*"), recursive=True) if not f.endswith(".json")]
        hashes = {f: read(f, "-ImageDataHash").get("File:ImageDataHash") for f in files}
        pixels = {f: Image.open(f).tobytes() for f in files}

        self.assertEqual(self.run_cmd("apply", "--expected", "999"), 2, "count mismatch refuses")
        self.assertFalse(glob.glob(os.path.join(self.rep, "EmbedLog_*.csv")))

        self.run_cmd("plan")
        with open(self.latest("EmbedPlan_*.csv"), encoding="utf-8-sig") as f:
            n = sum(1 for r in csv.DictReader(f) if r["TagsToAdd"] or r["FileDates"])
        self.assertEqual(self.run_cmd("apply", "--expected", str(n)), 0)

        plain = read(self.p(GP, "plain.jpg"))
        self.assertEqual(plain["EXIF:DateTimeOriginal"], "2018:07:06 15:07:13")
        self.assertEqual(plain["EXIF:OffsetTimeOriginal"], "+08:00")
        self.assertAlmostEqual(plain["EXIF:GPSLatitude"], 22.271106, places=5)
        self.assertAlmostEqual(plain["EXIF:GPSLongitude"], 114.130844, places=5)
        self.assertEqual(plain["XMP:Description"], "Beach day")
        self.assertEqual(plain["XMP:PersonInImage"], ["Alex", "Sam"])
        self.assertTrue(str(plain["File:FileModifyDate"]).startswith("2018:07:06 15:07:13"))

        self.assertEqual(read(self.p(GP, "camera.jpg"))["EXIF:DateTimeOriginal"], "2018:07:06 09:00:00", "existing date kept")
        self.assertEqual(read(self.p("WhatsApp-001", "IMG-20230505-WA0013.jpg"))["EXIF:DateTimeOriginal"], "2023:05:05 12:00:00")
        self.assertEqual(read(self.p(GP, "graphic.png"))["XMP:DateTimeOriginal"], "2018:07:06 15:07:13+08:00")
        self.assertTrue(str(read(self.p(GP, "old.bmp"))["File:FileModifyDate"]).startswith("2018:07:06"))
        if HEIF:
            self.assertEqual(read(self.p(GP, "phone.heic"))["EXIF:DateTimeOriginal"], "2018:07:06 15:07:13")

        for f in files:
            self.assertEqual(read(f, "-ImageDataHash").get("File:ImageDataHash"), hashes[f], f"image data unchanged: {f}")
            self.assertEqual(Image.open(f).tobytes(), pixels[f], f"pixels unchanged: {f}")
        self.assertFalse(glob.glob(self.p("**", ".embed-*"), recursive=True), "no temporary files left")

        self.assertEqual(em.main(["undo", "--log", self.latest("EmbedLog_*.csv"), "--workers", "2"]), 0)
        plain = read(self.p(GP, "plain.jpg"))
        for tag in ("EXIF:DateTimeOriginal", "EXIF:GPSLatitude", "XMP:Description", "XMP:PersonInImage"):
            self.assertNotIn(tag, plain, f"undo removed {tag}")
        self.assertEqual(read(self.p(GP, "camera.jpg"))["EXIF:DateTimeOriginal"], "2018:07:06 09:00:00")
        for f in files:
            self.assertEqual(Image.open(f).tobytes(), pixels[f])

    def test_keywords(self):
        self.build()
        target = self.p(GP, "plain.jpg")
        subprocess.run([EXE, "-overwrite_original", "-XMP-dc:Subject=Existing", "-FileModifyDate=2018:07:06 15:07:13+08:00", target],
                       capture_output=True)
        before = read(target, "-FileModifyDate", "-ImageDataHash")
        kmap = os.path.join(self.tmp, "map.json")
        with open(kmap, "w", encoding="utf-8") as f:
            json.dump({target: ["Trip 2022/23", "Existing"], self.p(GP, "old.bmp"): ["Not writable"]}, f)
        self.assertEqual(em.main(["keywords", "--map", kmap, "--report-root", self.rep]), 0)
        self.assertEqual(em.main(["keywords", "--map", kmap, "--report-root", self.rep, "--apply", "--expected", "2"]), 2)
        self.assertEqual(em.main(["keywords", "--map", kmap, "--report-root", self.rep, "--apply", "--expected", "1"]), 0)
        after = read(target, "-XMP:Subject", "-FileModifyDate", "-ImageDataHash")
        self.assertEqual(after["XMP:Subject"], ["Existing", "Trip 2022/23"], "added once, existing kept")
        self.assertEqual(after["File:FileModifyDate"], before["File:FileModifyDate"], "Windows date kept")
        self.assertEqual(after["File:ImageDataHash"], before["File:ImageDataHash"])
        log = self.latest("EmbedLog_*_keywords.csv")
        self.assertEqual(em.main(["undo", "--log", log, "--workers", "1"]), 0)
        self.assertEqual(read(target, "-XMP:Subject")["XMP:Subject"], "Existing")

    def test_force_name_date(self):
        p = self.p(GP, "20140215_193544.jpg")
        ex = Image.Exif()
        ex[0x8769] = {0x9003: "2004:02:10 02:16:14"}
        Image.new("RGB", (32, 24), (9, 9, 9)).save(p, exif=ex, quality=90)
        with self.assertRaises(SystemExit):  # refused without --only
            self.run_cmd("plan", "--force-name-date")
        self.assertEqual(self.run_cmd("apply", "--only", p, "--force-name-date", "--expected", "1"), 0)
        self.assertEqual(read(p)["EXIF:DateTimeOriginal"], "2014:02:15 19:35:44")
        self.assertEqual(em.main(["undo", "--log", self.latest("EmbedLog_*.csv"), "--workers", "1"]), 0)
        self.assertEqual(read(p)["EXIF:DateTimeOriginal"], "2004:02:10 02:16:14", "undo restores the old date")

    def test_fallback_date_only_when_nothing_else(self):
        undated = self.p(GP, "scan.jpg")
        dated = self.p(GP, "IMG-20230505-WA0001.jpg")
        Image.new("RGB", (32, 24), (5, 5, 5)).save(undated, quality=90)
        Image.new("RGB", (32, 24), (6, 6, 6)).save(dated, quality=90)
        fb = os.path.join(self.tmp, "fallback.json")
        json.dump({undated: "2015-06-05T12:00:00", dated: "2015-06-05T12:00:00"}, open(fb, "w"))
        self.assertEqual(self.run_cmd("apply", "--fallback-dates", fb, "--expected", "2"), 0)
        self.assertEqual(read(undated)["EXIF:DateTimeOriginal"], "2015:06:05 12:00:00")
        self.assertEqual(read(dated)["EXIF:DateTimeOriginal"], "2023:05:05 12:00:00", "name date beats the fallback")

    def test_copy_meta_keeps_backup_and_picture(self):
        src, dst = self.p(GP, "old.jpg"), self.p(GP, "better.jpg")
        ex = Image.Exif()
        ex[0x8769] = {0x9003: "2016:05:22 10:00:00"}
        Image.new("RGB", (32, 24), (40, 40, 40)).save(src, exif=ex, quality=60)
        Image.new("RGB", (64, 48), (40, 40, 40)).save(dst, quality=95)
        subprocess.run([EXE, "-overwrite_original", "-XMP-dc:Subject=Trip", src], capture_output=True)
        h = read(dst, "-ImageDataHash")["File:ImageDataHash"]
        m = os.path.join(self.tmp, "pairs.json")
        json.dump({dst: src}, open(m, "w"))
        backup = os.path.join(self.tmp, "backup")
        self.assertEqual(em.main(["copy-meta", "--map", m, "--report-root", self.rep, "--apply", "--expected", "1",
                                  "--backup-root", backup]), 0)
        after = read(dst, "-ImageDataHash", "-XMP:Subject", "-EXIF:DateTimeOriginal")
        self.assertEqual(after["EXIF:DateTimeOriginal"], "2016:05:22 10:00:00")
        self.assertEqual(after["XMP:Subject"], "Trip")
        self.assertEqual(after["File:ImageDataHash"], h, "picture unchanged")
        self.assertTrue(glob.glob(os.path.join(backup, "**", "better.jpg"), recursive=True), "original kept")

    @unittest.skipUnless(SAMPLE_VIDEO, "no MP4 in Imports to copy")
    def test_video_placeholder_date(self):
        dest = self.p(GP, "IMG_6397.MOV".replace(".MOV", ".mp4"))
        shutil.copyfile(SAMPLE_VIDEO, dest)
        subprocess.run([EXE, "-overwrite_original", "-api", "QuickTimeUTC=1", "-QuickTime:CreateDate=1970:01:01 00:00:00",
                        "-Keys:CreationDate=2016:11:22 23:30:47+08:00", dest], capture_output=True)
        self.assertEqual(self.run_cmd("apply", "--expected", "1"), 0)
        v = read(dest, "-QuickTime:CreateDate")
        self.assertTrue(str(v["QuickTime:CreateDate"]).startswith("2016:11:22"), v)

    @unittest.skipUnless(SAMPLE_VIDEO, "no MP4 in Imports to copy")
    def test_video_placeholder_uses_modify_date_over_upload_date(self):
        dest = self.p(GP, "IMG_5842.mp4")
        shutil.copyfile(SAMPLE_VIDEO, dest)
        subprocess.run([EXE, "-overwrite_original", "-api", "QuickTimeUTC=1", "-QuickTime:CreateDate=1970:01:01 00:00:00",
                        "-Keys:CreationDate=", "-QuickTime:ModifyDate=2016:11:22 23:21:17+08:00", dest], capture_output=True)
        sidecar(dest + ".supplemental-metadata.json", photoTakenTime={"timestamp": "1769490360"})  # 2026 upload
        self.assertEqual(self.run_cmd("apply", "--expected", "1"), 0)
        self.assertTrue(str(read(dest, "-QuickTime:CreateDate")["QuickTime:CreateDate"]).startswith("2016:11:22"))

    @unittest.skipUnless(SAMPLE_VIDEO, "no MP4 in Imports to copy")
    def test_video(self):
        dest = self.p(GP, "clip.mp4")
        shutil.copyfile(SAMPLE_VIDEO, dest)
        # Clear its date in the copy so there is something to fill.
        subprocess.run([EXE, "-overwrite_original", "-QuickTime:CreateDate=0000:00:00 00:00:00",
                        "-QuickTime:ModifyDate=0000:00:00 00:00:00", "-Keys:CreationDate=", "-GPSCoordinates=", dest],
                       capture_output=True)
        sidecar(dest + ".supplemental-metadata.json", photoTakenTime={"timestamp": "1530860833"},
                geoData={"latitude": 51.5, "longitude": -0.12, "altitude": 10})
        h0 = read(dest, "-ImageDataHash")["File:ImageDataHash"]
        self.run_cmd("plan")
        with open(self.latest("EmbedPlan_*.csv"), encoding="utf-8-sig") as f:
            row = next(r for r in csv.DictReader(f) if r["Path"].endswith("clip.mp4"))
        self.assertEqual(row["Timezone"], "Europe/London", "timezone from GPS")
        self.assertEqual(row["DateTaken"], "2018:07:06 08:07:13+01:00")
        self.assertEqual(self.run_cmd("apply", "--expected", "1"), 0)
        v = read(dest, "-QuickTime:CreateDate", "-GPSCoordinates", "-ImageDataHash")
        self.assertEqual(v["File:ImageDataHash"], h0)
        self.assertTrue(str(v["QuickTime:CreateDate"]).startswith("2018:07:06"))
        self.assertIn("51.5", str(v.get("QuickTime:GPSCoordinates")))


if __name__ == "__main__":
    unittest.main()
