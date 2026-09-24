"""
Regression tests for #7a: false "degraded drive" from raw ffmpeg wall time.

Runs the real, unmodified functions in media.py (same approach as
test_broadcast_stability_suite.py: media.py imports cleanly with dummy SDL
drivers). Put this file in root/tests/; media.py is in root/assets/.

Run with: python3 test_drive_latency_normalization.py
      or: python3 -m pytest test_drive_latency_normalization.py -v
Run as a script, the output is also saved to root/temp/test_drive_latency_normalization_output.txt
(next to app_log.txt); the previous run's is kept as ..._output_previous.txt.

Numbers used below come from app_log.txt / retro_config.json:
  - Healthy whole-episode scans with video playing and every prober running
    took 16.8-24.1 s for 704-719 s episodes (23.8-33.9 ms per audio second).
  - The 14:55 launch flagged C:\\ as degraded at "avg 12762ms over last 5
    recent reads" even though nothing was wrong with the drive.
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT_DIR)
sys.path.insert(0, os.path.join(_ROOT_DIR, "assets"))

import media  # noqa: E402

PATH = "C:\\Users\\steve\\Desktop\\videos\\CH 06\\101 Dalmatians.avi" if sys.platform == "win32" \
    else "/videos/CH 06/101 Dalmatians.avi"


class _Clock:
    def __init__(self, t=None):
        # Start at the real current time: sample expiry compares against
        # the real clock once a test's patch has been removed.
        self.t = time.time() if t is None else t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class DriveLatencyBase(unittest.TestCase):
    def setUp(self):
        with media._drive_op_latency_lock:
            media._drive_op_latency.clear()

    def record(self, elapsed_s, audio_sec):
        media._record_drive_op_latency(PATH, elapsed_s * 1000.0, audio_sec=audio_sec)


class TestNormalization(DriveLatencyBase):
    def test_logged_false_positive_is_now_healthy(self):
        # Five 900 s windows at the ~12-15 s the log shows.
        for s in (12.0, 13.5, 12.7, 11.9, 15.7):
            self.record(s, 900)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_logged_worst_case_five_windows_at_300s_is_healthy(self):
        # Same 12.7 s figure if the windows had been the shorter 300 s ones.
        for _ in range(5):
            self.record(12.7, 300)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_measured_healthy_episodes_under_heavy_contention(self):
        for s, d in ((24.1, 711), (21.1, 719), (20.1, 715), (21.3, 717), (16.8, 704)):
            self.record(s, d)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_short_commercial_clip_overhead_is_not_slowness(self):
        for _ in range(4):
            self.record(3.0, 30)   # 30 s clip, 3 s of process start + seek
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_genuinely_slow_reads_are_flagged(self):
        for _ in range(4):
            self.record(150.0, 300)   # 300 s window taking 150 s
        self.assertTrue(media._is_drive_degraded(PATH))

    def test_timeout_among_healthy_samples_is_flagged(self):
        self.record(12.0, 300)
        self.record(12.0, 300)
        self.record(180.0, 300)   # chunk timeout hit
        self.assertTrue(media._is_drive_degraded(PATH))

    def test_needs_minimum_samples(self):
        self.record(180.0, 300)
        self.record(180.0, 300)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_unknown_audio_length_is_not_recorded(self):
        for _ in range(5):
            media._record_drive_op_latency(PATH, 170_000.0, audio_sec=None)
            media._record_drive_op_latency(PATH, 170_000.0, audio_sec=0)
        self.assertFalse(media._is_drive_degraded(PATH))
        self.assertIsNone(media._drive_latency_summary(PATH))

    def test_old_samples_expire_so_flag_cannot_lock(self):
        clock = _Clock()
        with mock.patch.object(media, "_now_ms", lambda: clock() * 1000.0):
            for _ in range(4):
                self.record(150.0, 300)
            self.assertTrue(media._is_drive_degraded(PATH))
            clock.advance(media._DRIVE_LATENCY_SAMPLE_MAX_AGE_MS / 1000.0 + 1)
            self.assertFalse(media._is_drive_degraded(PATH))

    def test_recovery_after_fast_samples(self):
        for _ in range(3):
            self.record(150.0, 300)
        self.assertTrue(media._is_drive_degraded(PATH))
        for _ in range(6):
            self.record(10.0, 900)   # window keeps only the last 6
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_summary_reports_normalized_figure(self):
        for _ in range(3):
            self.record(12.0, 300)   # (12000-2000)/300 = 33.3
        text = media._drive_latency_summary(PATH)
        self.assertIn("33.3", text)
        self.assertIn("per audio second", text)


class TestThroughRealLufsWindowFunction(DriveLatencyBase):
    """Drive the real _lufs_window_ffmpeg with a fake subprocess and a fake
    clock, so the actual timing/recording code is what is under test."""

    def _run_window(self, ffmpeg_seconds, length_sec, timeout=False):
        clock = _Clock()

        def fake_run(cmd, **kw):
            clock.advance(ffmpeg_seconds)
            if timeout:
                import subprocess
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
            return types.SimpleNamespace(stderr="", stdout="", returncode=0)

        import subprocess
        with mock.patch.object(time, "time", clock), \
                mock.patch.object(media, "_resolve_ffmpeg_path", lambda: "ffmpeg"), \
                mock.patch.object(media, "_yield_to_live_playback", lambda *a, **k: None), \
                mock.patch.object(subprocess, "run", fake_run):
            return media._lufs_window_ffmpeg(PATH, -20.5, start_sec=0.0, length_sec=length_sec)

    def test_healthy_900s_window_taking_12s_is_not_degraded(self):
        for _ in range(5):
            self._run_window(12.7, 900.0)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_slow_windows_are_degraded(self):
        for _ in range(3):
            self._run_window(150.0, 300.0)
        self.assertTrue(media._is_drive_degraded(PATH))

    def test_timeout_is_recorded_as_slow(self):
        self._run_window(12.0, 300.0)
        self._run_window(12.0, 300.0)
        result = self._run_window(180.0, 300.0, timeout=True)
        self.assertTrue(result[4])          # reported as timed out
        self.assertTrue(media._is_drive_degraded(PATH))

    def test_whole_file_scan_with_unknown_length_records_nothing(self):
        for _ in range(4):
            self._run_window(170.0, None)
        self.assertFalse(media._is_drive_degraded(PATH))


if __name__ == "__main__":
    from extract import run_logged  # also saves this run's output to root/temp, next to app_log.txt
    run_logged(__file__, lambda: unittest.main(verbosity=2))
