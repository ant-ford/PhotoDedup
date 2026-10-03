"""Tests for the triage tool. Need Pillow + numpy only (the image model is faked).

    python -m unittest discover -s triage/tests -v
"""

import csv
import json
import os
import random
import shutil
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import features  # noqa: E402
import triage  # noqa: E402
import triage_core as core  # noqa: E402

GP = os.path.join("Takeout", "Google Photos")


def picture(seed: int, size=(1600, 1200)) -> Image.Image:
    """A photo-like synthetic image: smooth background plus random shapes."""
    rnd = random.Random(seed)
    w, h = size
    x = np.linspace(0, 1, w)[None, :]
    y = np.linspace(0, 1, h)[:, None]
    base = np.stack([(x * rnd.random() + y * rnd.random()) * 255] * 3, axis=-1) % 256
    img = Image.fromarray(base.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rnd.randrange(w), rnd.randrange(h)
        box = [x0, y0, x0 + rnd.randrange(w // 8 + 1, w // 2 + 2), y0 + rnd.randrange(h // 8 + 1, h // 2 + 2)]
        fill = tuple(rnd.randrange(256) for _ in range(3))
        (d.ellipse if rnd.random() < .5 else d.rectangle)(box, fill=fill)
    return img


def save(img: Image.Image, path: str, exif=None, **kw) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if exif is not None:
        kw["exif"] = exif
    img.save(path, **kw)
    return path


def sidecar(path: str, **fields) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"title": os.path.basename(path), **fields}, f)
    return path


def camera_exif(orientation=1):
    ex = Image.Exif()
    ex[0x010F] = "samsung"
    ex[0x0110] = "SM-G991B"
    ex[0x0112] = orientation
    return ex


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="triage_test_")
        self.src = os.path.join(self.tmp, "Imports")
        self.cache = os.path.join(self.tmp, "Cache")
        self.reports = os.path.join(self.tmp, "Reports")
        os.makedirs(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.src, *parts)


class CoreTests(Fixture):
    def test_sidecar_names(self):
        self.assertEqual(core.sidecar_names("a.jpg"), ["a.jpg.supplemental-metadata.json"])
        self.assertIn("IMG.jpg.supplemental-metadata(1).json", core.sidecar_names("IMG(1).jpg"))
        self.assertIn("pic.jpg.supplemental-metadata.json", core.sidecar_names("pic-edited.jpg"))

    def test_scan_finds_sidecars_and_skips_trash(self):
        save(picture(1, (64, 64)), self.p("partA", GP, "Album", "x.jpg"))
        sidecar(self.p("partB", GP, "Album", "x.jpg.supplemental-metadata.json"))
        save(picture(2, (64, 64)), self.p("partA", GP, "Album", "IMG(1).jpg"))
        sidecar(self.p("partA", GP, "Album", "IMG.jpg.supplemental-metadata(1).json"))
        save(picture(3, (64, 64)), self.p("partA", GP, "Trash", "t.jpg"))
        save(picture(4, (64, 64)), self.p("partA", GP, "Bin", "b.jpg"))
        open(self.p("partA", GP, "Album", "empty.jpg"), "wb").close()

        imgs = {os.path.basename(i.path): i for i in core.scan_library(self.src)}
        self.assertEqual(sorted(imgs), ["IMG(1).jpg", "x.jpg"])
        self.assertEqual(len(imgs["x.jpg"].sidecars), 1, "cross-part sidecar found")
        self.assertEqual(len(imgs["IMG(1).jpg"].sidecars), 1, "numbered sidecar found")

    def test_read_sidecars(self):
        s = sidecar(os.path.join(self.tmp, "s.json"),
                    googlePhotosOrigin={"mobileUpload": {"deviceFolder": {"localFolderName": "WhatsApp Images"}}},
                    people=[{"name": "A"}, {"name": "B"}], geoData={"latitude": 22.3, "longitude": 114.1})
        info = core.read_sidecars([s])
        self.assertEqual(info["upload_folder"], "WhatsApp Images")
        self.assertEqual(info["people"], 2)
        self.assertTrue(info["gps"])
        s2 = sidecar(os.path.join(self.tmp, "s2.json"), geoData={"latitude": 0.0, "longitude": 0.0})
        self.assertFalse(core.read_sidecars([s2])["gps"])

    def test_whatsapp_detection(self):
        self.assertEqual(core.whatsapp_date("IMG-20230505-WA0013.jpg"), "2023-05-05")
        self.assertTrue(core.is_whatsapp("x/IMG-20230505-WA0013.jpg"))
        self.assertTrue(core.is_whatsapp("x/photo.jpg", "WhatsApp Images"))
        self.assertFalse(core.is_whatsapp("x/20230505_101010.jpg", ""))

    def test_phone_screen_like(self):
        self.assertTrue(core.phone_screen_like(1080, 2400))
        self.assertTrue(core.phone_screen_like(2532, 1170))
        self.assertFalse(core.phone_screen_like(4000, 3000))
        self.assertFalse(core.phone_screen_like(1080, 1080))
        self.assertFalse(core.phone_screen_like(1920, 1080), "16:9 camera photo is not a phone screen")
        self.assertTrue(core.phone_screen_like(750, 1334), "iPhone 8 screen")

    def test_camera_shaped(self):
        self.assertTrue(core.camera_shaped(1200, 1600), "WhatsApp-shrunk 3:4 photo")
        self.assertTrue(core.camera_shaped(768, 1024))
        self.assertTrue(core.camera_shaped(1920, 1080))
        self.assertFalse(core.camera_shaped(720, 721), "square meme")
        self.assertFalse(core.camera_shaped(552, 782))

    def test_decide(self):
        self.assertEqual(core.decide("meme", 0.9, [])[0], "reject")
        self.assertEqual(core.decide("meme", 0.6, [])[0], "review", "below default threshold 0.75")
        self.assertEqual(core.decide("meme", 0.9, ["GPS location"])[0], "review", "protected never rejected")
        self.assertEqual(core.decide("meme", 0.97, [], photo_probability=0.3)[0], "review", "could be a photo")
        self.assertEqual(core.decide("meme", 0.97, [], camera_shape=True)[0], "review", "camera proportions")
        self.assertEqual(core.decide("screenshot", 0.9, [], camera_shape=True)[0], "reject")
        self.assertEqual(core.decide("document", 0.99, [])[0], "review")
        self.assertEqual(core.decide("photo", 0.99, [])[0], "keep")

    def test_adjust_probabilities(self):
        probs = {c: 1 / 7 for c in core.CATEGORIES}
        adj, why = core.adjust_probabilities(probs, {"width": 1080, "height": 2400})
        self.assertEqual(max(adj, key=adj.get), "screenshot")
        self.assertTrue(why)
        adj, _ = core.adjust_probabilities(probs, {"width": 1080, "height": 2400, "camera": "Pixel 8"})
        self.assertEqual(max(adj, key=adj.get), "photo")
        self.assertAlmostEqual(sum(adj.values()), 1.0)


class FeatureTests(Fixture):
    def test_extract_reads_size_camera_and_orientation(self):
        path = save(picture(5, (1600, 1200)), self.p("a.jpg"), exif=camera_exif(orientation=6), quality=90)
        facts, rgb = features.extract(path, os.path.join(self.cache, "t.jpg"))
        self.assertEqual(facts["error"], "")
        self.assertEqual((facts["width"], facts["height"]), (1200, 1600), "rotated by EXIF orientation 6")
        self.assertIn("SM-G991B", facts["camera"])
        self.assertTrue(os.path.exists(os.path.join(self.cache, "t.jpg")))
        self.assertLessEqual(max(rgb.size), 512)

    def test_unreadable_file(self):
        path = self.p("broken.jpg")
        with open(path, "wb") as f:
            f.write(b"not an image")
        facts, rgb = features.extract(path, os.path.join(self.cache, "b.jpg"))
        self.assertTrue(facts["error"])
        self.assertIsNone(rgb)

    def test_phash_survives_whatsapp_style_recompression(self):
        img = picture(6, (2000, 1500))
        small = img.resize((1000, 750))
        small_path = save(small, self.p("wa.jpg"), quality=45)
        a = features.perceptual_hash(img)
        b = features.perceptual_hash(Image.open(small_path))
        other = features.perceptual_hash(picture(7, (2000, 1500)))
        self.assertLessEqual(core.hamming(a, b), 4)
        self.assertGreater(core.hamming(a, other), 12)


class FakeClassifier:
    """Stands in for ClipClassifier: 'embedding' is a one-hot of the category in the file name."""

    categories = core.CATEGORIES

    def embed(self, images):
        return np.vstack([np.eye(len(self.categories), dtype=np.float32)[0] for _ in images])

    def probabilities(self, embeddings):
        out = []
        for e in embeddings:
            probs = {c: 0.02 for c in self.categories}
            probs[self.categories[int(np.argmax(e))]] = 0.88
            out.append(probs)
        return out


class EndToEndTests(Fixture):
    def build_library(self):
        photo = picture(10, (2400, 1800))
        # Camera original with sidecar, and a smaller recompressed WhatsApp copy of it.
        save(photo, self.p("partA", GP, "Photos from 2023", "20230505_101010.jpg"), exif=camera_exif(), quality=92)
        sidecar(self.p("partA", GP, "Photos from 2023", "20230505_101010.jpg.supplemental-metadata.json"))
        save(photo.resize((1200, 900)), self.p("WhatsApp-001", "IMG-20230506-WA0001.jpg"), quality=50)
        # An unrelated WhatsApp image.
        save(picture(11, (1200, 900)), self.p("WhatsApp-001", "IMG-20230506-WA0002.jpg"), quality=60)
        # A phone-screen-sized PNG with no camera data.
        save(picture(12, (1080, 2400)), self.p("partB", GP, "Photos from 2024", "Screenshot_20240101.png"))
        # A larger WhatsApp image that matches a smaller original: never auto-rejected.
        big = picture(13, (2000, 1500))
        save(big.resize((800, 600)), self.p("partB", GP, "Photos from 2024", "small_original.jpg"), quality=85)
        save(big, self.p("WhatsApp-001", "IMG-20240101-WA0003.jpg"), quality=85)

    def read_csv(self):
        out = [d for d in os.listdir(self.reports) if d.startswith("Triage_")]
        self.assertEqual(len(out), 1)
        folder = os.path.join(self.reports, out[0])
        for f in ("index.html", "data.js"):
            self.assertTrue(os.path.exists(os.path.join(folder, f)), f)
        csv_path = os.path.join(folder, out[0] + ".csv")
        with open(csv_path, encoding="utf-8-sig") as f:
            return {r["name"]: r for r in csv.DictReader(f)}, folder

    def test_no_clip_run(self):
        self.build_library()
        rc = triage.main(["--source", self.src, "--cache", self.cache, "--report-root", self.reports, "--no-clip"])
        self.assertEqual(rc, 0)
        rows, folder = self.read_csv()

        copy = rows["IMG-20230506-WA0001.jpg"]
        self.assertEqual(copy["category"], "whatsapp_copy")
        self.assertEqual(copy["suggestion"], "reject")
        self.assertTrue(copy["copy_of"].endswith("20230505_101010.jpg"))

        self.assertEqual(rows["IMG-20230506-WA0002.jpg"]["category"], "unknown")
        self.assertEqual(rows["Screenshot_20240101.png"]["category"], "screenshot")
        self.assertEqual(rows["20230505_101010.jpg"]["suggestion"], "keep")

        bigger = rows["IMG-20240101-WA0003.jpg"]
        self.assertEqual(bigger["category"], "whatsapp_copy")
        self.assertEqual(bigger["suggestion"], "review", "a larger WhatsApp copy is not auto-rejected")

        # Thumbnails referenced by the page exist.
        with open(os.path.join(folder, "data.js"), encoding="utf-8") as f:
            data = json.loads(f.read()[len("window.TRIAGE = "):].rstrip().rstrip(";"))
        for r in data["rows"]:
            t = os.path.join(folder, data["thumbs"], r["id"][:2], r["id"] + ".jpg")
            self.assertTrue(os.path.exists(os.path.normpath(t)), t)

    def test_rerun_uses_cache(self):
        self.build_library()
        args = ["--source", self.src, "--cache", self.cache, "--report-root", self.reports, "--no-clip"]
        triage.main(args)
        shutil.rmtree(self.reports)
        cache = triage.Cache(self.cache)
        n = cache.db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        self.assertEqual(n, 6)
        triage.main(args)
        self.assertEqual(cache.db.execute("SELECT COUNT(*) FROM images").fetchone()[0], 6)

    def test_classifier_path_with_fake_model(self):
        self.build_library()
        images = core.scan_library(self.src)
        cache = triage.Cache(self.cache)
        clf = FakeClassifier()
        triage.process(images, cache, clf, batch_size=2)
        rows, embs = triage.classify(images, cache, clf, threshold=0.6)
        self.assertTrue(all(e is not None for e in embs))
        by = {r["name"]: r for r in rows}
        # The fake model calls everything "photo"; the phone-screen PNG is pushed to screenshot.
        self.assertEqual(by["20230505_101010.jpg"]["category"], "photo")
        self.assertIn("phone-screen", by["Screenshot_20240101.png"]["reasons"])

    def test_source_overlap_is_refused(self):
        with self.assertRaises(SystemExit):
            triage.main(["--source", self.tmp, "--cache", self.cache, "--report-root", self.reports, "--no-clip"])


if __name__ == "__main__":
    unittest.main()
