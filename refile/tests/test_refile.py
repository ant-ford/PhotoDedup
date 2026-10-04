"""Tests for refile_library.py on a temporary fixture (needs ExifTool + Pillow).

    python -m unittest discover -s refile/tests -v
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
import refile_library as rl  # noqa: E402

EXE = rl.em.find_exiftool()
RESTORE = os.path.join(os.path.dirname(HERE), "Restore-Quarantine.ps1")
GP = os.path.join("Takeout", "Google Photos")


class RefileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="refile_test_"))
        self.src = os.path.join(self.tmp, "Imports")
        self.lib = os.path.join(self.tmp, "Library")
        self.rep = os.path.join(self.tmp, "Reports")
        os.makedirs(self.rep)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def photo(self, rel, date=None, colour=(10, 20, 30)):
        p = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        Image.new("RGB", (32, 24), colour).save(p, quality=90)
        if date:
            subprocess.run([EXE, "-overwrite_original", f"-EXIF:DateTimeOriginal={date}", p], capture_output=True)
        return p

    def test_plan_apply_and_restore(self):
        a = self.photo(os.path.join("partA", GP, "Photos from 2023", "IMG_1.jpg"), "2023:05:05 10:00:00", (1, 2, 3))
        b = self.photo(os.path.join("partB", GP, "Photos from 2023", "IMG_1.jpg"), "2023:05:20 10:00:00", (4, 5, 6))
        c = self.photo(os.path.join("partA", GP, "Japan", "IMG_9.jpg"), "2022:12:30 10:00:00")
        d = self.photo(os.path.join("WhatsApp-001", "mystery.jpg"))
        side = os.path.join(self.src, "partA", GP, "Photos from 2023", "IMG_1.jpg.supplemental-metadata.json")
        open(side, "w").write("{}")
        albums = os.path.join(self.tmp, "albums.json")
        json.dump({c: "2022-12 Trip 2022/23"}, open(albums, "w"))

        base = ["--source", self.src, "--library", self.lib, "--albums", albums, "--report-root", self.rep]
        self.assertEqual(rl.main(["plan", *base]), 0)
        self.assertFalse(os.path.exists(self.lib), "plan moves nothing")
        self.assertEqual(rl.main(["apply", *base, "--expected", "99"]), 2)
        self.assertEqual(rl.main(["apply", *base, "--expected", "4"]), 0)

        self.assertTrue(os.path.exists(os.path.join(self.lib, "2023", "2023-05", "IMG_1.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.lib, "2023", "2023-05", "IMG_1 (2).jpg")), "clash renamed")
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Albums", "2022-12 Trip 2022-23", "IMG_9.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.lib, "Undated", "mystery.jpg")))
        for p in (a, b, c, d):
            self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(side), "sidecars stay behind")

        manifest = glob.glob(os.path.join(self.rep, "Refile_*_Actions.csv"))[0]
        rows = list(csv.DictReader(open(manifest, encoding="utf-8-sig")))
        self.assertEqual(sum(r["Action"] == "MovedMedia" for r in rows), 4)
        subprocess.run(["pwsh", "-NoProfile", "-File", RESTORE, "-ManifestPath", manifest], capture_output=True)
        for p in (a, b, c, d):
            self.assertTrue(os.path.exists(p), f"restored {p}")

    def test_albums_only_moves_listed_files_within_library(self):
        lib = self.lib
        x = os.path.join(lib, "2023", "2023-05", "x.jpg")
        y = os.path.join(lib, "2023", "2023-05", "y.jpg")
        for p in (x, y):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            Image.new("RGB", (16, 16), (1, 1, 1)).save(p)
            subprocess.run([EXE, "-overwrite_original", "-EXIF:DateTimeOriginal=2023:05:05 10:00:00", p], capture_output=True)
        albums = os.path.join(self.tmp, "albums.json")
        json.dump({x: "2023-05 Trip"}, open(albums, "w"))
        base = ["--source", lib, "--library", lib, "--albums", albums, "--albums-only", "--report-root", self.rep]
        self.assertEqual(rl.main(["apply", *base, "--expected", "1"]), 0)
        self.assertTrue(os.path.exists(os.path.join(lib, "Albums", "2023-05 Trip", "x.jpg")))
        self.assertTrue(os.path.exists(y), "unlisted file untouched")
        json.dump({os.path.join(lib, "Albums", "2023-05 Trip", "x.jpg"): "2023-05 Trip"}, open(albums, "w"))
        self.assertEqual(rl.main(["apply", *base, "--expected", "0"]), 0, "already in place: nothing to move")
        with self.assertRaises(SystemExit):
            rl.main(["plan", "--source", lib, "--library", lib, "--report-root", self.rep])

    def test_refuses_library_inside_source(self):
        with self.assertRaises(SystemExit):
            rl.main(["plan", "--source", self.tmp, "--library", os.path.join(self.tmp, "Library"), "--report-root", self.rep])


if __name__ == "__main__":
    unittest.main()
