"""
Tests for image aspect ratio preprocessing (blurred background fill).

Covers:
  - _preprocess_image_for_aspect: unit tests with synthetic images
  - combine_videos: integration with real image-to-video pipeline
  - Shot-by-shot mode: aspect adjustment in shot mode

Run:
  cd /home/ta/111 && .venv/bin/python -m pytest test/services/test_aspect_preprocess.py -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import video as vd


def _make_test_image(width: int, height: int, color: tuple = (100, 150, 200)) -> str:
    """Create a temporary solid-color image and return its path."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    img = Image.new("RGB", (width, height), color)
    img.save(path, "PNG")
    return path


def _get_image_size(path: str) -> tuple:
    """Return (width, height) of an image file."""
    with Image.open(path) as img:
        return img.size


class TestAspectPreprocessUnit(unittest.TestCase):
    """Unit tests for _preprocess_image_for_aspect function."""

    def setUp(self):
        self.output_dir = tempfile.mkdtemp(prefix="aspect-test-")
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        import shutil
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def _call(self, image_path, target_w, target_h):
        result = vd._preprocess_image_for_aspect(
            image_path, target_w, target_h, self.output_dir
        )
        if result != image_path:
            self.temp_files.append(result)
        return result

    # ── Matching aspect ratio: returns original path unchanged ──

    def test_same_ratio_returns_original(self):
        """16:9 image -> 16:9 target should skip processing."""
        img = _make_test_image(1920, 1080)
        self.temp_files.append(img)

        result = self._call(img, 1920, 1080)
        self.assertEqual(result, img)

    def test_close_ratio_within_tolerance_returns_original(self):
        """1920x1080 (1.778) vs 1080x608 (1.776) — within 3% tolerance."""
        img = _make_test_image(1920, 1080)
        self.temp_files.append(img)

        result = self._call(img, 1080, 608)
        self.assertEqual(result, img)

    # ── Landscape → Portrait conversion ──

    def test_landscape_to_portrait_creates_new_file(self):
        """16:9 landscape -> 9:16 portrait should create a new image."""
        img = _make_test_image(1920, 1080)
        self.temp_files.append(img)

        result = self._call(img, 1080, 1920)
        self.assertNotEqual(result, img)
        self.assertTrue(os.path.exists(result))

    def test_landscape_to_portrait_correct_dimensions(self):
        """Output should have exact target dimensions."""
        img = _make_test_image(1920, 1080)
        self.temp_files.append(img)

        result = self._call(img, 1080, 1920)
        w, h = _get_image_size(result)
        self.assertEqual(w, 1080)
        self.assertEqual(h, 1920)

    def test_landscape_to_square(self):
        """16:9 landscape -> 1:1 square."""
        img = _make_test_image(1920, 1080)
        self.temp_files.append(img)

        result = self._call(img, 1080, 1080)
        w, h = _get_image_size(result)
        self.assertEqual(w, 1080)
        self.assertEqual(h, 1080)

    # ── Portrait → Landscape conversion ──

    def test_portrait_to_landscape_correct_dimensions(self):
        """9:16 portrait -> 16:9 landscape."""
        img = _make_test_image(1080, 1920)
        self.temp_files.append(img)

        result = self._call(img, 1920, 1080)
        w, h = _get_image_size(result)
        self.assertEqual(w, 1920)
        self.assertEqual(h, 1080)

    # ── Square → Portrait / Landscape ──

    def test_square_to_portrait(self):
        """1:1 square -> 9:16 portrait."""
        img = _make_test_image(1080, 1080)
        self.temp_files.append(img)

        result = self._call(img, 1080, 1920)
        w, h = _get_image_size(result)
        self.assertEqual((w, h), (1080, 1920))

    # ── Very small images ──

    def test_tiny_image_skips(self):
        """Images smaller than 10px in any dimension are skipped."""
        img = _make_test_image(5, 100)
        self.temp_files.append(img)

        result = self._call(img, 1080, 1920)
        self.assertEqual(result, img)

    # ── RGBA images (transparency) ──

    def test_rgba_image_handled(self):
        """RGBA images should be processed without error."""
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.temp_files.append(path)
        # Create a semi-transparent RGBA image
        img = Image.new("RGBA", (1920, 1080), (100, 150, 200, 128))
        img.save(path, "PNG")

        result = self._call(path, 1080, 1920)
        w, h = _get_image_size(result)
        self.assertEqual((w, h), (1080, 1920))

    # ── Output is readable by PIL ──

    def test_output_is_valid_png(self):
        """Output should be a valid image file."""
        img = _make_test_image(1920, 1080, color=(255, 0, 0))
        self.temp_files.append(img)

        result = self._call(img, 1080, 1920)
        with Image.open(result) as out:
            self.assertEqual(out.size, (1080, 1920))
            # Result should be RGB (or RGBA), not corrupted
            self.assertIn(out.mode, ("RGB", "RGBA"))


class TestAspectPreprocessVisual(unittest.TestCase):
    """Visual validation: the output image must contain the original content centered."""

    def setUp(self):
        self.output_dir = tempfile.mkdtemp(prefix="aspect-visual-")
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        import shutil
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_foreground_is_centered_in_landscape_to_portrait(self):
        """
        Create a landscape image with a known pixel at center (red dot).
        After conversion to portrait, the red pixel should still be
        near the center of the output image.
        """
        w, h = 640, 360  # 16:9 landscape
        target_w, target_h = 360, 640  # 9:16 portrait

        # Create a green image with a large red square at center
        # (must be big enough to survive LANCZOS resampling to smaller size)
        img = Image.new("RGB", (w, h), (0, 255, 0))
        red_size = 40
        for dx in range(-red_size//2, red_size//2):
            for dy in range(-red_size//2, red_size//2):
                img.putpixel((w // 2 + dx, h // 2 + dy), (255, 0, 0))
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.temp_files.append(path)
        img.save(path, "PNG")

        result = vd._preprocess_image_for_aspect(
            path, target_w, target_h, self.output_dir
        )
        self.temp_files.append(result)

        with Image.open(result) as out:
            # The foreground should be centered:
            # fg_w = 360, fg_h = 360 * (360/640) = 360 * 0.5625 = 202
            # fg_x = (360 - 360) // 2 = 0
            # fg_y = (640 - 202) // 2 = 219
            # So the red pixel (at fg center) should be at approximately (180, 219+101) = (180, 320)
            # The blurred background should dominate the top and bottom strips
            center_x, center_y = target_w // 2, target_h // 2
            pixel = out.getpixel((center_x, center_y))

            # The center should NOT be pure green (background) — it should be red-ish
            # Actually, the red dot is at the center of the FOREGROUND, not the output.
            # The foreground center in the output is: fg_x + fg_w/2, fg_y + fg_h/2
            fg_h = max(1, int(target_w / (w / h)))
            fg_y = (target_h - fg_h) // 2
            fg_center_y = fg_y + fg_h // 2
            fg_center_x = target_w // 2

            center_pixel = out.getpixel((fg_center_x, fg_center_y))
            # The red pixel should be at this position — it should be very red
            self.assertGreater(center_pixel[0], 200, f"Expected red-ish at center, got {center_pixel}")
            self.assertLess(center_pixel[1], 50, f"Expected low green at center, got {center_pixel}")

    def test_top_and_bottom_are_blurred_not_solid_black(self):
        """
        The top and bottom strips (outside the foreground) should not be
        solid black — they should be a blurred version of the original.
        """
        w, h = 640, 360
        target_w, target_h = 360, 640

        # Create a known-color image
        img = Image.new("RGB", (w, h), (100, 200, 50))
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.temp_files.append(path)
        img.save(path, "PNG")

        result = vd._preprocess_image_for_aspect(
            path, target_w, target_h, self.output_dir
        )
        self.temp_files.append(result)

        with Image.open(result) as out:
            # Top-left corner should be in the blurred background area
            top_pixel = out.getpixel((10, 10))
            # Should not be pure black
            self.assertFalse(
                top_pixel[0] == 0 and top_pixel[1] == 0 and top_pixel[2] == 0,
                f"Top pixel should not be solid black, got {top_pixel}"
            )
            # Should be somewhat close to the original color (green-ish, blurred)
            self.assertGreater(top_pixel[1], 100, f"Expected green-ish blur, got {top_pixel}")

            # Bottom-right corner should also be blurred background
            bottom_pixel = out.getpixel((target_w - 10, target_h - 10))
            self.assertFalse(
                bottom_pixel[0] == 0 and bottom_pixel[1] == 0 and bottom_pixel[2] == 0,
                f"Bottom pixel should not be solid black, got {bottom_pixel}"
            )


class TestCombineVideosWithAspectAdjustment(unittest.TestCase):
    """Integration: verify that combine_videos calls _preprocess_image_for_aspect."""

    def setUp(self):
        self.output_dir = tempfile.mkdtemp(prefix="combine-aspect-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.output_dir, ignore_errors=True)

    @mock.patch("app.services.video._preprocess_image_for_aspect")
    def test_main_loop_calls_preprocess_for_images(self, mock_preprocess):
        """In the main image conversion loop, images trigger the preprocess call."""
        # Create a test image
        img = _make_test_image(1920, 1080)
        self.addCleanup(lambda: os.unlink(img) if os.path.exists(img) else None)

        # Mock everything heavy
        mock_preprocess.return_value = img  # return same path (no change needed)

        from moviepy import AudioFileClip
        audio_path = os.path.join(self.output_dir, "test_audio.mp3")
        # Create a minimal valid MP3
        import subprocess
        subprocess.run([
            "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1", "-q:a", "9", "-acodec", "libmp3lame", audio_path, "-y"
        ], capture_output=True)

        output = os.path.join(self.output_dir, "output.mp4")

        with mock.patch("app.services.video.get_ffmpeg_binary", return_value="ffmpeg"):
            result = vd.combine_videos(
                combined_video_path=output,
                video_paths=[img],
                audio_file=audio_path,
                video_aspect="9:16",
                max_clip_duration=3,
                threads=1,
            )

        # Should have called preprocess at least once for the image
        mock_preprocess.assert_called()
        # First positional arg should be the image path
        self.assertEqual(mock_preprocess.call_args[0][0], img)

    @mock.patch("app.services.video._preprocess_image_for_aspect")
    def test_shot_mode_calls_preprocess_for_images(self, mock_preprocess):
        """In shot-by-shot mode, images also trigger the preprocess call."""
        img = _make_test_image(1920, 1080)
        self.addCleanup(lambda: os.unlink(img) if os.path.exists(img) else None)

        mock_preprocess.return_value = img

        audio_path = os.path.join(self.output_dir, "test_audio.mp3")
        import subprocess
        subprocess.run([
            "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1", "-q:a", "9", "-acodec", "libmp3lame", audio_path, "-y"
        ], capture_output=True)

        output = os.path.join(self.output_dir, "output_shot.mp4")

        with mock.patch("app.services.video.get_ffmpeg_binary", return_value="ffmpeg"):
            result = vd.combine_videos(
                combined_video_path=output,
                video_paths=[img],
                audio_file=audio_path,
                video_aspect="9:16",
                max_clip_duration=3,
                threads=1,
                clip_durations=[3.0],
            )

        mock_preprocess.assert_called()
        self.assertEqual(mock_preprocess.call_args[0][0], img)


if __name__ == "__main__":
    unittest.main(verbosity=2)
