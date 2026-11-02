"""Portrait review pixels stay readable without invalidating legacy media caches."""
from contextlib import nullcontext
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_editor_contract as contract
from orchestrator.runtime import platform_env, runtime_policy
from orchestrator.selection import ai_editor
from orchestrator.vision import material_cache, vision_analyzer


class PaperReviewMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="paper review media ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "replay"
        self.cache = self.root / "media cache"
        source = self.root / "synthetic.mp4"
        source.write_bytes(b"Offline source identity; no decoder or API is used")
        self.shot = {"shot_id": "portrait", "video": str(source),
                     "source_start": 0, "source_end": 6}
        self.frame = self.root / "portrait frame.jpg"
        image = Image.new("RGB", (180, 320), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 130, 119, 189), fill=(255, 0, 0))
        draw.rectangle((0, 0, 19, 19), fill=(0, 255, 0))
        draw.rectangle((160, 300, 179, 319), fill=(0, 0, 255))
        image.save(self.frame, quality=95)
        self.frames = [{"path": str(self.frame), "timestamp": n / 3} for n in range(16)]
        self.ffmpeg = platform_env.find_ffmpeg()[0]
        self.patch(material_cache, "cache_root", return_value=self.cache)
        self.patch(platform_env, "find_ffmpeg", return_value=("unused-ffmpeg", None))
        self.patch(runtime_policy, "media_slot",
                   side_effect=lambda **kwargs: nullcontext(SimpleNamespace(acquired=True)))
        self.frame_cache = self.patch(material_cache, "cached_frames", side_effect=self.prepared_frames)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def prepared_frames(self, shot, directory, prepare, *, protocol):
        self.assertEqual(shot, self.shot)
        self.assertEqual(directory, self.directory)
        count = 16 if protocol == "candidate-even-window-16-v1" else 8
        self.assertIn(protocol, ("candidate-even-window-8-v1", "candidate-even-window-16-v1"))
        return self.frames[:count]

    def legacy_sheet(self, dense):
        """Preseed the on-disk identity used before fit was a review argument."""
        count = 16 if dense else 8
        tiles = [{**frame, "label": f"t={frame['timestamp']:.3f}s"} for frame in self.frames[:count]]
        identity = {
            "items": [{**tile, "source_stat": contract.source_identity(tile["path"])} for tile in tiles],
            "parameters": {"columns": 4, "tile_width": 420, "tile_height": 300},
            "protocol": "labelled-sheet-v1",
        }
        path = self.cache / "sheets" / f"candidate{count}_{contract.digest(identity)}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Existing legacy cache bytes must survive")
        return path

    def marker_ratio(self, path):
        with Image.open(path) as image:
            # Read only the first tile's frame area, excluding timestamp text.
            crop = image.convert("RGB").crop((0, 0, 270, 480))
            points = [(x, y) for y in range(crop.height) for x in range(crop.width)
                      if (lambda p: p[0] > 180 and p[1] < 70 and p[2] < 70)(crop.getpixel((x, y)))]
            self.assertTrue(points, "The actual source marker must remain visible")
            width = max(x for x, _ in points) - min(x for x, _ in points) + 1
            height = max(y for _, y in points) - min(y for _, y in points) + 1
            return width / height

    def assert_portrait_pixels(self, sheet):
        with Image.open(sheet) as image:
            self.assertEqual(image.size, (1080, 2056))  # 4 x 4 portrait tiles, including labels.
            pixels = image.convert("RGB")
            green = pixels.getpixel((10, 10))
            blue = pixels.getpixel((260, 470))
            self.assertGreater(green[1], green[0] + 100)
            self.assertGreater(blue[2], blue[0] + 100)
        self.assertAlmostEqual(self.marker_ratio(sheet), 1, delta=.03)

    def test_default_and_explicit_false_reuse_original_sheet_identity(self):
        for dense in (False, True):
            with self.subTest(dense=dense):
                old = self.legacy_sheet(dense)
                with mock.patch.object(vision_analyzer, "_write_sheet",
                                       side_effect=AssertionError("Legacy cache was invalidated")):
                    default = ai_editor._review_media(self.shot, self.directory, dense=dense)
                    explicit = ai_editor._review_media(self.shot, self.directory, dense=dense, fit=False)
                self.assertEqual(default["sheet"], old)
                self.assertEqual(explicit["sheet"], old)
                self.assertEqual(old.read_bytes(), b"Existing legacy cache bytes must survive")

    def test_uncached_legacy_path_keeps_previous_parameters(self):
        def write(tiles, target, **kwargs):
            target.write_bytes(b"Legacy pixels")
            return True
        with mock.patch.object(vision_analyzer, "_write_sheet", side_effect=write) as writer, \
                mock.patch.object(material_cache, "cached_sheet", wraps=material_cache.cached_sheet) as cache:
            ai_editor._review_media(self.shot, self.directory)
        self.assertEqual(cache.call_args.kwargs["parameters"],
                         {"columns": 4, "tile_width": 420, "tile_height": 300})
        self.assertEqual(writer.call_args.kwargs,
                         {"columns": 4, "tile_width": 420, "tile_height": 300, "ffmpeg": "unused-ffmpeg"})

    def test_fit_has_separate_reusable_sheet_and_shares_frame_protocol(self):
        old = self.legacy_sheet(True)
        with mock.patch.object(vision_analyzer, "_write_sheet", wraps=vision_analyzer._write_sheet) as writer, \
                mock.patch.object(material_cache, "cached_sheet", wraps=material_cache.cached_sheet) as cache:
            fitted = ai_editor._review_media(self.shot, self.directory, dense=True, fit=True)
            reused = ai_editor._review_media(self.shot, self.directory, dense=True, fit=True)
            legacy = ai_editor._review_media(self.shot, self.directory, dense=True)
        self.assertNotEqual(fitted["sheet"], old)
        self.assertEqual(reused["sheet"], fitted["sheet"])
        self.assertEqual(legacy["sheet"], old)
        self.assertEqual(old.read_bytes(), b"Existing legacy cache bytes must survive")
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(writer.call_args.kwargs,
                         {"columns": 4, "tile_width": 270, "tile_height": 514,
                          "fit": True, "ffmpeg": "unused-ffmpeg"})
        self.assertEqual(cache.call_args_list[0].kwargs["parameters"],
                         {"columns": 4, "tile_width": 270, "tile_height": 514, "fit": True})
        self.assertEqual({call.kwargs["protocol"] for call in self.frame_cache.call_args_list},
                         {"candidate-even-window-16-v1"})
        self.assert_portrait_pixels(fitted["sheet"])

    def test_pillow_fallback_preserves_portrait_marker_geometry(self):
        with mock.patch.dict(sys.modules, {"cv2": None}):
            media = ai_editor._review_media(self.shot, self.directory, dense=True, fit=True)
        self.assert_portrait_pixels(media["sheet"])

    def test_ffmpeg_fallback_preserves_portrait_marker_geometry(self):
        if not self.ffmpeg or not (Path(self.ffmpeg).is_file() or shutil.which(str(self.ffmpeg))):
            self.skipTest("No local ffmpeg binary for the dependency-free fallback")
        with mock.patch.dict(sys.modules, {"cv2": None, "PIL": None}), \
                mock.patch.object(platform_env, "find_ffmpeg", return_value=(self.ffmpeg, None)):
            media = ai_editor._review_media(self.shot, self.directory, dense=True, fit=True)
        self.assert_portrait_pixels(media["sheet"])

    def test_fitted_landscape_frame_is_padded_without_cropping_or_stretching(self):
        image = Image.new("RGB", (320, 180), (240, 240, 240))
        ImageDraw.Draw(image).rectangle((130, 60, 189, 119), fill=(255, 0, 0))
        image.save(self.frame, quality=95)
        media = ai_editor._review_media(self.shot, self.directory, dense=True, fit=True)
        self.assertAlmostEqual(self.marker_ratio(media["sheet"]), 1, delta=.04)
        with Image.open(media["sheet"]) as sheet:
            pixels = sheet.convert("RGB")
            self.assertLess(max(pixels.getpixel((135, 100))), 40)
            self.assertGreater(min(pixels.getpixel((10, 170))), 200)
            self.assertLess(max(pixels.getpixel((135, 380))), 40)


if __name__ == "__main__":
    unittest.main()
