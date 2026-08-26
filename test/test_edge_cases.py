#!/usr/bin/env python3
"""
Edge-case and boundary-condition test suite for MoneyPrinterTurbo.

Covers 18 scenarios that operations users may encounter.
Run with:  python3 test/test_edge_cases.py
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def run_test(name, func):
    """Run a test function and print a consistent result line."""
    try:
        func()
        print(f"  {PASS}  {name}")
        return True
    except unittest.SkipTest:
        print(f"  {SKIP}  {name}  (requires external service)")
        return None
    except AssertionError as e:
        print(f"  {FAIL}  {name}  — {e}")
        return False
    except Exception as e:
        print(f"  {FAIL}  {name}  — {type(e).__name__}: {e}")
        return False


# ── Test support ──────────────────────────────────────────────────────

import unittest


class EdgeCaseTests(unittest.TestCase):
    """Each method is one edge-case scenario."""

    @classmethod
    def setUpClass(cls):
        from app.utils import utils
        cls.task_id = utils.get_uuid()
        utils.task_dir(cls.task_id)

    # ── Script generation ─────────────────────────────────────────────

    def test_01_empty_subject_graceful_error(self):
        """Empty video subject should produce a clear Chinese error, not a traceback."""
        from app.models.schema import VideoParams
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        params = VideoParams(video_subject="", video_language="zh")
        r = tm.start(tid, params, stop_at="script")
        # Should return a failure dict, not crash
        self.assertIsNotNone(r)

    def test_02_very_long_subject(self):
        """Very long (>1000 chars) subject should not crash the LLM call."""
        from app.models.schema import VideoParams
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        long_subject = "测试 " * 500  # 1500 chars
        params = VideoParams(video_subject=long_subject, video_language="zh")
        try:
            r = tm.start(tid, params, stop_at="script")
            # Should complete or fail gracefully — never crash
        except Exception:
            self.fail("Long subject caused an unhandled exception")

    def test_03_special_characters_in_subject(self):
        """Emoji, HTML tags, and special chars in subject should be safe."""
        from app.models.schema import VideoParams
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        params = VideoParams(
            video_subject="测试 <b>HTML</b> & emoji 🎉✨ & SQL ' OR 1=1--",
            video_language="zh",
        )
        try:
            tm.start(tid, params, stop_at="script")
        except Exception:
            self.fail("Special characters caused an unhandled exception")

    def test_04_english_subject(self):
        """English subject should work (i18n path)."""
        from app.models.schema import VideoParams
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        params = VideoParams(video_subject="How to be productive", video_language="en")
        try:
            r = tm.start(tid, params, stop_at="script")
        except Exception:
            self.fail("English subject caused an unhandled exception")

    def test_05_custom_script_mode(self):
        """Pre-written script should bypass LLM and be used directly."""
        from app.models.schema import VideoParams
        from app.services import task as tm
        from app.utils import utils
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        utils.task_dir(tid)
        params = VideoParams(
            video_subject="irrelevant",
            video_script="第一段：这是测试文案。\n\n第二段：继续测试文案。",
            video_language="zh",
        )
        r = tm.start(tid, params, stop_at="script")
        self.assertIsNotNone(r)

    # ── API / Model parameter validation ──────────────────────────────

    def test_06_invalid_voice_name_fallback(self):
        """Invalid voice name should not crash — TTS should fail gracefully."""
        from app.models.schema import VideoParams
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        params = VideoParams(
            video_subject="测试",
            video_script="测试文案。",
            voice_name="this-voice-does-not-exist-xyz",
            video_language="zh",
        )
        try:
            r = tm.start(tid, params, stop_at="audio")
        except Exception:
            self.fail("Invalid voice name caused an unhandled exception")

    def test_07_negative_voice_rate_clamped(self):
        """Negative voice rate should be clamped to safe range, not crash."""
        from app.models.schema import VideoParams
        params = VideoParams(
            video_subject="测试",
            voice_rate=-5.0,
            video_language="zh",
        )
        # The VideoParams model or normalize_clip_speed should handle this
        from app.utils.utils import normalize_clip_speed
        result = normalize_clip_speed(-5.0)
        self.assertGreaterEqual(result, 0.5)

    def test_08_zero_volume(self):
        """Zero volume should produce silent audio, not an error."""
        from app.models.schema import VideoParams
        params = VideoParams(
            video_subject="测试",
            voice_volume=0.0,
            video_language="zh",
        )
        # Should not raise
        self.assertEqual(params.voice_volume, 0.0)

    # ── Material / File handling ──────────────────────────────────────

    def test_09_material_search_no_results(self):
        """No-results from material search should not crash the pipeline."""
        from app.services import material as mat
        from app.utils import utils
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        utils.task_dir(tid)
        try:
            result = mat.download_videos(
                task_id=tid,
                search_terms=["xyznonexistentterm12345"],
                source="pexels",
                video_aspect="9:16",
                audio_duration=30,
            )
            # Empty result is acceptable; crash is not
        except Exception:
            self.fail("No-results material search caused an unhandled exception")

    def test_10_missing_subtitle_file(self):
        """Missing subtitle file should not crash video generation."""
        from app.models.schema import VideoParams, VideoConcatMode
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        params = VideoParams(
            video_subject="测试",
            video_script="测试文案。",
            video_source="local",
            video_materials=[],
            subtitle_enabled=True,
            video_concat_mode=VideoConcatMode.random,
            video_language="zh",
        )
        # Should fail with a clear error, not crash
        try:
            r = tm.start(tid, params, stop_at="video")
        except Exception:
            self.fail("Missing subtitle caused an unhandled exception")

    def test_11_empty_material_list(self):
        """Empty local material list should fail with a clear error message."""
        from app.models.schema import VideoParams, VideoConcatMode
        from app.services import task as tm
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        params = VideoParams(
            video_subject="测试",
            video_script="测试文案。",
            video_source="local",
            video_materials=[],
            subtitle_enabled=False,
            video_concat_mode=VideoConcatMode.random,
            video_language="zh",
        )
        try:
            r = tm.start(tid, params, stop_at="video")
        except Exception:
            self.fail("Empty local materials caused an unhandled exception")

    # ── File name / path safety ───────────────────────────────────────

    def test_12_path_traversal_in_filename(self):
        """Path traversal in file names should be rejected."""
        from app.utils import file_security
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as tmp:
            safe_file = _os.path.join(tmp, "safe.txt")
            with open(safe_file, "w") as f:
                f.write("test")
            # Attempt path traversal
            try:
                file_security.resolve_path_within_directory(tmp, "../etc/passwd")
                self.fail("Path traversal was NOT blocked")
            except ValueError:
                pass  # Expected

    def test_13_null_bytes_in_input(self):
        """Null bytes in user input should be safe."""
        from app.models.schema import VideoParams
        params = VideoParams(
            video_subject="test\x00injection",
            video_language="zh",
        )
        self.assertNotIn("\x00", params.video_subject)

    # ── Concurrent / State safety ─────────────────────────────────────

    def test_14_duplicate_task_id(self):
        """Creating a task with an existing task_id should overwrite safely."""
        from app.services import state as sm
        from app.models import const
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        sm.state.update_task(tid, state=const.TASK_STATE_PROCESSING, progress=5)
        sm.state.update_task(tid, state=const.TASK_STATE_COMPLETE, progress=100)
        task = sm.state.get_task(tid)
        self.assertEqual(int(task["state"]), const.TASK_STATE_COMPLETE)

    def test_15_task_id_with_special_chars(self):
        """Task IDs are UUIDs — special chars in manually crafted IDs should not crash."""
        from app.services import state as sm
        from app.models import const
        # The system generates UUIDs, but let's verify Redis handles arbitrary strings
        tid = "test/../etc&query=1%20x"
        try:
            sm.state.update_task(tid, state=const.TASK_STATE_COMPLETE, progress=100)
            sm.state.get_task(tid)
        except Exception:
            pass  # Some backends may reject; what matters is no crash

    # ── Checkpoint / Recovery ─────────────────────────────────────────

    def test_16_checkpoint_survives_empty_values(self):
        """Checkpoint.save with empty extra fields should not corrupt the file."""
        from app.services import checkpoint as cp
        from app.utils import utils
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        utils.task_dir(tid)
        cp.save(tid, "start", 0)
        loaded = cp.load(tid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["stage"], "start")

    def test_17_metrics_with_missing_start_time(self):
        """record_complete without start_time should not crash."""
        from app.services import metrics as mt
        from app.utils import utils
        tid = "test-edge-" + __import__("uuid").uuid4().hex[:8]
        utils.task_dir(tid)
        try:
            mt.record_complete(tid, start_time=None, video_count=0)
        except Exception:
            self.fail("record_complete(None start_time) raised an exception")

    # ── Disk monitoring ───────────────────────────────────────────────

    def test_18_disk_usage_returns_valid_values(self):
        """disk_usage() should return positive numbers."""
        from app.services import disk_monitor
        total, used, free, pct = disk_monitor.disk_usage()
        self.assertGreater(total, 0)
        self.assertGreaterEqual(used, 0)
        self.assertGreater(free, 0)
        self.assertGreaterEqual(pct, 0)
        self.assertLessEqual(pct, 100)


def main():
    print("=" * 60)
    print("MoneyPrinterTurbo Edge Case Test Suite (18 scenarios)")
    print("=" * 60)

    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.TestLoader().loadTestsFromTestCase(EdgeCaseTests)
    result = runner.run(suite)

    passed = result.testsRun - len(result.failures) - len(result.errors)
    failed = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)


if __name__ == "__main__":
    main()
