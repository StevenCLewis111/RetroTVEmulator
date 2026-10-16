"""
Retro TV Emulator: the whole test suite in one script.

Runs the real code in retro_tv_emulator.py (cut out by AST, never re-typed), media.py, media_library.py,
menu_nav.py, ui.py, game_deck.py and performance_governor.py against the behaviour they are meant to have.
retro_tv_emulator.py cannot be imported (it boots the whole app), so the tests pull individual functions or
handler blocks out of its source and run them in an isolated namespace.

Put this file in root/tests/. Layout it expects:
  root/retro_tv_emulator.py, root/ui.py, root/game_deck.py
  root/assets/media.py, media_library.py, menu_nav.py, performance_governor.py (anything in assets/, GPU/, server/
  or updater/ is found)

Run with: python3 test_suite.py                    (everything)
      or: python3 test_suite.py -k FileExplorer    (only tests whose name contains that text)
      or: python3 -m pytest test_suite.py -v
Run as a script, the output is also saved to root/temp/test_suite_output.txt (next to app_log.txt); the previous
run's is kept as test_suite_output_previous.txt.

Layout of this file:
  1. Shared setup (paths, imports, check(), function extraction, output logging)
  2. Media and playback: drive latency, ffmpeg header probes, media library, governor and Whisper, picture-box
     fit, screen saver
  3. Menu navigation: System tab, Channels / Video / Theme / Games, TV guide and game deck, tab bar / server /
     popups, key actions / sliders / held-key repeat, file explorer, wheel / joystick / digits / DVD, name entry

Owns: regression coverage for the pieces listed above.
Does not own: anything that needs a real VLC window, audio device or GPU (those cannot run headless), and the
one-off old-version-versus-new-version comparisons used while the menu code was being moved into menu_nav.py.
"""
import ast
import builtins
import collections
import contextlib
import ctypes
import datetime
import inspect
import io
import itertools
import math
import os
import random
import re
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import tokenize
import types
import unittest
from unittest import mock
from unittest.mock import MagicMock, patch

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")


# ============================================================================
# SECTION: Paths and imports
# ============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS_DIR = os.path.join(ROOT, "assets")
MAIN_SCRIPT = os.path.join(ROOT, "retro_tv_emulator.py")


def _media_script_path():
    root_copy = os.path.join(ROOT, "media.py")
    return root_copy if os.path.exists(root_copy) else os.path.join(ASSETS_DIR, "media.py")


TEMP_DIR = os.path.join(ROOT, "temp")

for _sub in ("server", "updater", "assets", "GPU"):
    sys.path.insert(0, os.path.join(ROOT, _sub))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

if not hasattr(builtins, "ctypes"):
    builtins.ctypes = ctypes

import pygame

pygame.init()

import menu_nav as mn
import ui
import media
import media_library as ml
import performance_governor as gov

menu_nav = mn

with open(MAIN_SCRIPT, "r", encoding="utf-8") as _main_file:
    MAIN_SRC = _main_file.read()
MAIN_TREE = ast.parse(MAIN_SRC)


# ============================================================================
# SECTION: Shared test helpers
# ============================================================================

_CHECKS = [0]
_FAILED = []


def check(ok, label):
    """Records one check. A section function calls this many times; the section's test fails if any failed."""
    _CHECKS[0] += 1
    if not ok:
        _FAILED.append(label)


_check_result = check


class _SectionCase(unittest.TestCase):
    """A test whose body is a section function full of check() calls; the test fails with the first labels that failed."""

    def run_section(self, fn, *args):
        before = len(_FAILED)
        fn(*args)
        failed = _FAILED[before:]
        if failed:
            shown = "\n  ".join(str(label) for label in failed[:20])
            more = "" if len(failed) <= 20 else "\n  ... and %d more" % (len(failed) - 20)
            self.fail("%d of the checks in %s failed:\n  %s%s" % (len(failed), fn.__name__, shown, more))


def get_function_source(name, start_hint=None):
    """Exact source of one top-level function of retro_tv_emulator.py, found with the real AST."""
    lines = MAIN_SRC.splitlines(keepends=True)
    candidates = [n for n in MAIN_TREE.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if not candidates:
        raise LookupError("function %r not found at module level in %s" % (name, MAIN_SCRIPT))
    if len(candidates) > 1 and start_hint is not None:
        candidates.sort(key=lambda n: abs(n.lineno - start_hint))
    node = candidates[0]
    return "".join(lines[node.lineno - 1:node.end_lineno])


def load_function(name, namespace=None, start_hint=None):
    """Execs the extracted function into a fresh namespace (seeded with `namespace`, e.g. a stub `log`) and returns it."""
    ns = dict(namespace or {})
    exec(compile(get_function_source(name, start_hint=start_hint), filename="<extracted:%s>" % name, mode="exec"), ns)
    return ns[name]


# ============================================================================
# SECTION: Drive-latency normalization (media.py)
# ============================================================================


PATH = "C:\\Users\\steve\\Desktop\\videos\\CH 06\\101 Dalmatians.avi" if sys.platform == "win32" \
    else "/videos/CH 06/101 Dalmatians.avi"


# ============================================================================
# SECTION: Test helpers
# ============================================================================


class _Clock:
    def __init__(self, t=None):
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


# ============================================================================
# SECTION: Drive-latency normalization
# ============================================================================


class TestNormalization(DriveLatencyBase):
    def test_logged_false_positive_is_now_healthy(self):
        for s in (12.0, 13.5, 12.7, 11.9, 15.7):
            self.record(s, 900)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_logged_worst_case_five_windows_at_300s_is_healthy(self):
        for _ in range(5):
            self.record(12.7, 300)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_measured_healthy_episodes_under_heavy_contention(self):
        for s, d in ((24.1, 711), (21.1, 719), (20.1, 715), (21.3, 717), (16.8, 704)):
            self.record(s, d)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_short_commercial_clip_overhead_is_not_slowness(self):
        for _ in range(4):
            self.record(3.0, 30)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_genuinely_slow_reads_are_flagged(self):
        for _ in range(4):
            self.record(150.0, 300)
        self.assertTrue(media._is_drive_degraded(PATH))

    def test_timeout_among_healthy_samples_is_flagged(self):
        self.record(12.0, 300)
        self.record(12.0, 300)
        self.record(180.0, 300)
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
            self.record(10.0, 900)
        self.assertFalse(media._is_drive_degraded(PATH))

    def test_summary_reports_normalized_figure(self):
        for _ in range(3):
            self.record(12.0, 300)
        text = media._drive_latency_summary(PATH)
        self.assertIn("33.3", text)
        self.assertIn("per audio second", text)


# ============================================================================
# SECTION: Real _lufs_window_ffmpeg recording path
# ============================================================================


class TestThroughRealLufsWindowFunction(DriveLatencyBase):
    """Drives the real _lufs_window_ffmpeg with a fake subprocess and a fake clock."""

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
        self.assertTrue(result[4])
        self.assertTrue(media._is_drive_degraded(PATH))

    def test_whole_file_scan_with_unknown_length_records_nothing(self):
        for _ in range(4):
            self._run_window(170.0, None)
        self.assertFalse(media._is_drive_degraded(PATH))


# ============================================================================
# SECTION: ffmpeg header probe wiring in media.py
# ============================================================================


# ============================================================================
# SECTION: Fixtures: realistic ffmpeg stderr
# ============================================================================


FAKE_STDERR_FULL = """
Input #0, matroska,webm, from 'fake.mkv':
  Duration: 00:24:13.42, start: 0.000000, bitrate: 3000 kb/s
    Stream #0:0: Video: h264, yuv420p, 1920x800 [SAR 1:1 DAR 12:5], 23.98 fps
    Stream #0:1(eng): Audio: aac, 48000 Hz, stereo, fltp
    Stream #0:2(eng): Subtitle: subrip
    Stream #0:3(spa): Subtitle: dvd_subtitle
"""

FAKE_STDERR_AUDIO_ONLY = """
Input #0, mp3, from 'fake.mp3':
  Duration: 00:03:30.00, start: 0.000000, bitrate: 192 kb/s
    Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp
"""

FAKE_STDERR_BROKEN = """fake.bad: Invalid data found when processing input"""


def _mock_result(stderr_text):
    m = MagicMock()
    m.stderr = stderr_text
    m.stdout = ""
    return m


# ============================================================================
# SECTION: ASR & subtitle probe parity
# ============================================================================


class TestAsrProbeParity(unittest.TestCase):
    def test_full_file_duration_and_audio(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_FULL)), \
             patch.object(media, "_cached_path_exists", return_value=True):
            result = media._asr_probe_media("fake.mkv")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["duration"], 24 * 60 + 13.42, places=2)
        self.assertTrue(result["has_audio"])

    def test_audio_only_file(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_AUDIO_ONLY)), \
             patch.object(media, "_cached_path_exists", return_value=True):
            result = media._asr_probe_media("fake.mp3")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["duration"], 3 * 60 + 30.0, places=2)
        self.assertTrue(result["has_audio"])

    def test_broken_file_returns_none(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_BROKEN)), \
             patch.object(media, "_cached_path_exists", return_value=True):
            result = media._asr_probe_media("fake.bad")
        self.assertIsNone(result)

    def test_missing_path_returns_none_without_subprocess(self):
        with patch.object(media, "_cached_path_exists", return_value=False), \
             patch("subprocess.run") as mock_run:
            result = media._asr_probe_media("nope.mkv")
        self.assertIsNone(result)
        mock_run.assert_not_called()


class TestSubtitleProbeParity(unittest.TestCase):
    def test_full_file_two_subtitle_streams(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_FULL)), \
             patch.object(media, "_cached_path_exists", return_value=True):
            streams = media._probe_embedded_subtitle_streams("fake.mkv")
        self.assertEqual(len(streams), 2)
        self.assertEqual(streams[0]["index"], 2)
        self.assertEqual(streams[0]["lang"], "eng")
        self.assertEqual(streams[0]["codec"], "subrip")
        self.assertTrue(streams[0]["is_text"])
        self.assertEqual(streams[1]["index"], 3)
        self.assertEqual(streams[1]["codec"], "dvd_subtitle")
        self.assertFalse(streams[1]["is_text"])

    def test_audio_only_file_no_subtitles(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_AUDIO_ONLY)), \
             patch.object(media, "_cached_path_exists", return_value=True):
            streams = media._probe_embedded_subtitle_streams("fake.mp3")
        self.assertEqual(streams, [])

    def test_broken_file_returns_empty_list(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_BROKEN)), \
             patch.object(media, "_cached_path_exists", return_value=True):
            streams = media._probe_embedded_subtitle_streams("fake.bad")
        self.assertEqual(streams, [])

    def test_missing_path_returns_empty_without_subprocess(self):
        with patch.object(media, "_cached_path_exists", return_value=False), \
             patch("subprocess.run") as mock_run:
            streams = media._probe_embedded_subtitle_streams("nope.mkv")
        self.assertEqual(streams, [])
        mock_run.assert_not_called()


# ============================================================================
# SECTION: Independent fallback probes
# ============================================================================


class TestSeriesDurationGuess(unittest.TestCase):
    def test_unknown_episode_uses_median_after_two_scanned_episodes(self):
        items = [
            {"path": "/tv/Show/Show S01E01.mkv"},
            {"path": "/tv/Show/Show S01E02.mkv"},
            {"path": "/tv/Show/Show S01E03.mkv"},
        ]
        cache = {
            items[0]["path"]: {"duration": 1200},
            items[1]["path"]: {"duration": 1500},
        }
        tracks = media.build_raw_tracks(items, cache, lambda _path: True)
        self.assertEqual([t["path"] for t in tracks], [item["path"] for item in items])
        self.assertEqual(tracks[-1]["duration"], 1500)
        self.assertNotIn("_duration_estimated", tracks[-1])

    def test_movies_and_commercials_are_not_guessed(self):
        items = [
            {"path": "/movies/Film Part 1.mkv"},
            {"path": "/movies/Film Part 2.mkv"},
            {"path": "/commercials/Show S01E03.mkv"},
        ]
        cache = {
            items[0]["path"]: {"duration": 5000},
            items[1]["path"]: {"duration": 5100},
        }
        tracks = media.build_raw_tracks(items, cache, lambda _path: True)
        self.assertEqual([t["path"] for t in tracks], items[0:2])


class TestDimsAndDurationUntouched(unittest.TestCase):
    """The duration and dims fallback probes work standalone with no media_library involvement."""

    def test_dims_still_independent_of_media_library(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_FULL)), \
             patch.object(media, "_cached_path_exists", return_value=True), \
             patch.dict(sys.modules, {"media_library": None}):
            dims = media.get_media_video_dimensions("fake.mkv")
        self.assertIsNotNone(dims)
        w, h = dims
        self.assertEqual(h, 800)

    def test_duration_still_independent_of_media_library(self):
        with patch("subprocess.run", return_value=_mock_result(FAKE_STDERR_FULL)), \
             patch.object(media, "_cached_path_exists", return_value=True), \
             patch.dict(sys.modules, {"media_library": None}):
            dur = media._probe_duration_seconds_fast("fake.mkv")
        self.assertAlmostEqual(dur, 24 * 60 + 13.42, places=2)


# ============================================================================
# SECTION: media_library.py: probing, identity keys, caches, picture-box detection
# ============================================================================


# ============================================================================
# SECTION: Test helpers
# ============================================================================


def _write_file(path, content: bytes):
    with open(path, "wb") as f:
        f.write(content)


SAMPLE_FFMPEG_STDERR = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'movie.mp4':
  Duration: 01:19:35.40, start: 0.000000, bitrate: 4021 kb/s
    Stream #0:0(eng): Video: h264 (High), yuv420p, 1920x800 [SAR 1:1 DAR 12:5], 3891 kb/s, 23.976 fps
    Stream #0:1(eng): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
    Stream #0:2(spa): Subtitle: subrip
    Stream #0:3(eng): Subtitle: hdmv_pgs_subtitle
"""

SAMPLE_FFMPEG_STDERR_NO_INPUT = "ffmpeg version 6.0\nUnrecognized option 'bogus'\n"


# ============================================================================
# SECTION: Library: probing, identity keys & cache lifecycle
# ============================================================================


class TestCombinedHeaderProbe(unittest.TestCase):
    """Item 4: one parse standing in for the four originals."""

    def _run(self, stderr_text):
        fake_result = mock.Mock(stderr=stderr_text, stdout="", returncode=0)
        with mock.patch.object(media, "_cached_path_exists", return_value=True), \
             mock.patch.object(ml.subprocess, "run", return_value=fake_result) as run_mock:
            result = ml.combined_header_probe("movie.mp4", ffmpeg_path="ffmpeg")
        return result, run_mock

    def test_one_subprocess_call_for_everything(self):
        result, run_mock = self._run(SAMPLE_FFMPEG_STDERR)
        self.assertEqual(run_mock.call_count, 1)
        self.assertTrue(result.ok)

    def test_duration_parsed(self):
        result, _ = self._run(SAMPLE_FFMPEG_STDERR)
        self.assertAlmostEqual(result.duration, 1 * 3600 + 19 * 60 + 35.40, places=2)

    def test_dims_and_dar_correction_matches_original_behavior(self):
        stderr = SAMPLE_FFMPEG_STDERR.replace("[SAR 1:1 DAR 12:5]", "[SAR 1:1 DAR 2:1]")
        result, _ = self._run(stderr)
        self.assertEqual(result.height, 800)
        self.assertEqual(result.width, 1600)

    def test_subtitle_streams_parsed_text_vs_bitmap(self):
        result, _ = self._run(SAMPLE_FFMPEG_STDERR)
        codecs = {s["codec"]: s["is_text"] for s in result.subtitle_streams}
        self.assertEqual(codecs.get("subrip"), True)
        self.assertEqual(codecs.get("hdmv_pgs_subtitle"), False)

    def test_audio_streams_parsed(self):
        result, _ = self._run(SAMPLE_FFMPEG_STDERR)
        self.assertEqual(len(result.audio_streams), 1)
        self.assertEqual(result.audio_streams[0]["codec"], "aac")
        self.assertTrue(result.has_audio)

    def test_no_input_stream_is_not_ok(self):
        result, _ = self._run(SAMPLE_FFMPEG_STDERR_NO_INPUT)
        self.assertFalse(result.ok)

    def test_timeout_is_not_ok(self):
        import subprocess as real_subprocess
        with mock.patch.object(media, "_cached_path_exists", return_value=True), \
             mock.patch.object(ml.subprocess, "run",
                                side_effect=real_subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15)):
            result = ml.combined_header_probe("movie.mp4", ffmpeg_path="ffmpeg")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")


class TestDurationFallback(unittest.TestCase):
    """Item 4: VLC stays the fallback when ffmpeg's number is implausible."""

    def test_plausible_ffmpeg_duration_is_used_directly(self):
        header = ml.HeaderProbeResult(ok=True, duration=1200.0)
        with mock.patch.object(media, "_duration_looks_implausible", return_value=False), \
             mock.patch.object(media, "get_media_duration") as vlc_mock:
            duration, source = ml.probe_duration_with_fallback("f.mp4", header)
        self.assertEqual(duration, 1200.0)
        self.assertEqual(source, "ffmpeg")
        vlc_mock.assert_not_called()

    def test_implausible_ffmpeg_duration_falls_back_to_vlc(self):
        header = ml.HeaderProbeResult(ok=True, duration=41.0)
        with mock.patch.object(media, "_duration_looks_implausible", return_value=True), \
             mock.patch.object(media, "get_media_duration", return_value=1320.0):
            duration, source = ml.probe_duration_with_fallback("f.mp4", header)
        self.assertEqual(duration, 1320.0)
        self.assertEqual(source, "vlc")

    def test_no_ffmpeg_duration_falls_back_to_vlc(self):
        header = ml.HeaderProbeResult(ok=True, duration=None)
        with mock.patch.object(media, "_duration_looks_implausible", return_value=False), \
             mock.patch.object(media, "get_media_duration", return_value=900.0):
            duration, source = ml.probe_duration_with_fallback("f.mp4", header)
        self.assertEqual(duration, 900.0)
        self.assertEqual(source, "vlc")

    def test_vlc_also_fails_returns_none(self):
        header = ml.HeaderProbeResult(ok=True, duration=None)
        with mock.patch.object(media, "_duration_looks_implausible", return_value=False), \
             mock.patch.object(media, "get_media_duration", return_value=None):
            duration, source = ml.probe_duration_with_fallback("f.mp4", header)
        self.assertIsNone(duration)
        self.assertIsNone(source)


class TestIdentityKey(unittest.TestCase):
    """The identity key is stable across rename and move, changes with content, and skips growing files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_content_same_key_even_at_different_path(self):
        content = os.urandom(200_000)
        p1 = os.path.join(self.tmp, "a.mp4")
        p2 = os.path.join(self.tmp, "sub", "b.mp4")
        os.makedirs(os.path.dirname(p2))
        _write_file(p1, content)
        _write_file(p2, content)
        self.assertEqual(
            ml.compute_identity_key(p1, growing_check_delay=0),
            ml.compute_identity_key(p2, growing_check_delay=0),
        )

    def test_different_content_different_key(self):
        p1 = os.path.join(self.tmp, "a.mp4")
        p2 = os.path.join(self.tmp, "b.mp4")
        _write_file(p1, os.urandom(200_000))
        _write_file(p2, os.urandom(200_000))
        self.assertNotEqual(
            ml.compute_identity_key(p1, growing_check_delay=0),
            ml.compute_identity_key(p2, growing_check_delay=0),
        )

    def test_small_file_does_not_crash_or_read_past_eof(self):
        p1 = os.path.join(self.tmp, "tiny.mp4")
        _write_file(p1, b"hello world")
        key = ml.compute_identity_key(p1, growing_check_delay=0)
        self.assertIsNotNone(key)

    def test_empty_file(self):
        p1 = os.path.join(self.tmp, "empty.mp4")
        _write_file(p1, b"")
        key = ml.compute_identity_key(p1, growing_check_delay=0)
        self.assertIsNotNone(key)

    def test_missing_file_returns_none(self):
        key = ml.compute_identity_key(os.path.join(self.tmp, "nope.mp4"), growing_check_delay=0)
        self.assertIsNone(key)

    def test_growing_file_is_skipped(self):
        p1 = os.path.join(self.tmp, "growing.mp4")
        _write_file(p1, os.urandom(1000))

        call_count = {"n": 0}

        def fake_getsize(path):
            call_count["n"] += 1
            return 1000 if call_count["n"] == 1 else 2000

        with mock.patch.object(ml.os.path, "getsize", side_effect=fake_getsize), \
             mock.patch.object(ml.time, "sleep"):
            key = ml.compute_identity_key(p1)
        self.assertIsNone(key)


class TestPathKeyResolverAvoidsRehashing(unittest.TestCase):
    """An unchanged file is never re-hashed on a second resolve()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.resolver = ml.PathKeyResolver(self.library)
        self.file_path = os.path.join(self.tmp, "movie.mp4")
        _write_file(self.file_path, os.urandom(100_000))

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_resolve_does_not_rehash(self):
        key1 = self.resolver.resolve(self.file_path)
        self.assertIsNotNone(key1)
        with mock.patch.object(ml, "compute_identity_key") as hash_mock:
            key2 = self.resolver.resolve(self.file_path)
        hash_mock.assert_not_called()
        self.assertEqual(key1, key2)

    def test_changed_mtime_triggers_rehash(self):
        key1 = self.resolver.resolve(self.file_path)
        _write_file(self.file_path, os.urandom(100_000))
        os.utime(self.file_path, (time.time() + 10, time.time() + 10))
        key2 = self.resolver.resolve(self.file_path)
        self.assertNotEqual(key1, key2)


class TestDoneWhenDeleteAndReAdd(unittest.TestCase):
    """A deleted and re-added file triggers no probe and gives a library hit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_readded_file_reuses_existing_entry_without_reprobing(self):
        content = os.urandom(150_000)
        original_path = os.path.join(self.tmp, "movie.mp4")
        _write_file(original_path, content)

        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result) as run_mock, \
             mock.patch.object(media, "_duration_looks_implausible", return_value=False):
            self.probe_library.get_or_probe_sync(original_path)
        self.assertEqual(run_mock.call_count, 1)

        os.remove(original_path)
        readded_path = os.path.join(self.tmp, "movie_readded.mp4")
        _write_file(readded_path, content)

        with mock.patch.object(ml.subprocess, "run") as run_mock_2:
            entry = self.probe_library.get_or_probe_sync(readded_path)
        run_mock_2.assert_not_called()
        self.assertIsNotNone(entry)
        self.assertEqual(entry["duration_source"], "ffmpeg")


class TestNewFileSpawnsOneHeaderProcess(unittest.TestCase):
    """A new file spawns one header process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")
        self.file_path = os.path.join(self.tmp, "movie.mp4")
        _write_file(self.file_path, os.urandom(100_000))

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exactly_one_subprocess_call(self):
        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result) as run_mock, \
             mock.patch.object(media, "_duration_looks_implausible", return_value=False):
            entry = self.probe_library.get_or_probe_sync(self.file_path)
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(entry["width"], 1920)
        self.assertEqual(entry["height"], 800)
        self.assertEqual(len(entry["subtitle_streams"]), 2)
        self.assertEqual(len(entry["audio_streams"]), 1)


class TestThreeQuickRestartsLoseNothing(unittest.TestCase):
    """Reopening the same db file after three quick restarts picks up exactly what was committed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.file_path = os.path.join(self.tmp, "movie.mp4")
        _write_file(self.file_path, os.urandom(100_000))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_data_survives_three_reopen_cycles(self):
        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        for i in range(3):
            library = ml.MediaLibrary(self.db_path)
            probe_library = ml.ProbeLibrary(library, ffmpeg_path="ffmpeg")
            if i == 0:
                with mock.patch.object(ml.subprocess, "run", return_value=fake_result), \
                     mock.patch.object(media, "_duration_looks_implausible", return_value=False):
                    probe_library.get_or_probe_sync(self.file_path)
            else:
                entry = probe_library.get_cached(self.file_path)
                self.assertIsNotNone(entry, f"lost data across restart #{i}")
                self.assertEqual(entry["width"], 1920)
            library.close()

    def test_commits_land_immediately_one_commit_per_finished_file(self):
        library1 = ml.MediaLibrary(self.db_path)
        probe_library1 = ml.ProbeLibrary(library1, ffmpeg_path="ffmpeg")
        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result), \
             mock.patch.object(media, "_duration_looks_implausible", return_value=False):
            probe_library1.get_or_probe_sync(self.file_path)

        library2 = ml.MediaLibrary(self.db_path)
        probe_library2 = ml.ProbeLibrary(library2, ffmpeg_path="ffmpeg")
        entry = probe_library2.get_cached(self.file_path)
        self.assertIsNotNone(entry)
        library1.close()
        library2.close()


class TestResetAllSettingsLeavesLibraryIntact(unittest.TestCase):
    """Reset All Settings leaves the library intact."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.file_path = os.path.join(self.tmp, "movie.mp4")
        _write_file(self.file_path, os.urandom(100_000))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_library_data_unaffected_by_simulated_config_reset(self):
        library = ml.MediaLibrary(self.db_path)
        probe_library = ml.ProbeLibrary(library, ffmpeg_path="ffmpeg")
        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result), \
             mock.patch.object(media, "_duration_looks_implausible", return_value=False):
            probe_library.get_or_probe_sync(self.file_path)

        fake_config = {"media_probe_cache": {"x": 1}, "dvd_gain_cache": {"y": 2}}
        fake_config.clear()

        entry = probe_library.get_cached(self.file_path)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["width"], 1920)

    def test_resolve_media_library_dir_is_independent_of_config_resolver(self):
        calls = []

        def fake_main_dir():
            calls.append(1)
            return self.tmp

        path = ml.resolve_media_library_dir(fake_main_dir)
        self.assertTrue(path.endswith("media_library"))
        self.assertTrue(os.path.isdir(path))

    def test_resolve_media_library_dir_makes_each_folder_it_is_given(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        first = ml.resolve_media_library_dir(lambda: other)
        self.assertTrue(os.path.isdir(first))
        second = ml.resolve_media_library_dir(lambda: self.tmp)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.isdir(second))


class TestLegacyCacheMigration(unittest.TestCase):
    """The legacy cache migration is throttled and resumable, never mutates the source dicts, and skips missing and growing files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")

        self.present_path = os.path.join(self.tmp, "present.mp4")
        _write_file(self.present_path, os.urandom(50_000))
        self.missing_path = os.path.join(self.tmp, "missing.mp4")

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _legacy_dicts(self):
        media_probe_cache = {
            self.present_path: {"duration": 1234, "dims": [1920, 1080], "gain": 1.1},
            self.missing_path: {"duration": 555, "dims": [640, 480]},
        }
        dvd_gain_cache = {self.present_path: 0.95}
        return media_probe_cache, dvd_gain_cache

    def test_migrates_present_file_skips_missing_file(self):
        media_probe_cache, dvd_gain_cache = self._legacy_dicts()
        migrator = ml.LegacyCacheMigrator(self.probe_library, batch_size=25)
        migrator.start(media_probe_cache, dvd_gain_cache)
        progress = migrator.step()
        self.assertTrue(progress.done)
        self.assertEqual(progress.migrated, 2)
        self.assertEqual(progress.skipped_missing, 1)

        entry = self.probe_library.get_cached(self.present_path)
        self.assertEqual(entry["duration"], 1234)
        self.assertEqual(entry["width"], 1920)
        self.assertEqual(entry["gain"], 0.95)

    def test_source_dicts_are_never_mutated(self):
        media_probe_cache, dvd_gain_cache = self._legacy_dicts()
        before = (dict(media_probe_cache), dict(dvd_gain_cache))
        migrator = ml.LegacyCacheMigrator(self.probe_library, batch_size=25)
        migrator.start(media_probe_cache, dvd_gain_cache)
        migrator.step()
        self.assertEqual(media_probe_cache, before[0])
        self.assertEqual(dvd_gain_cache, before[1])

    def test_batching_processes_a_few_entries_per_step(self):
        media_probe_cache, dvd_gain_cache = self._legacy_dicts()
        migrator = ml.LegacyCacheMigrator(self.probe_library, batch_size=1)
        migrator.start(media_probe_cache, dvd_gain_cache)
        progress = migrator.step()
        self.assertFalse(progress.done)
        self.assertEqual(progress.scanned, 1)
        steps = 1
        while not progress.done and steps < 20:
            progress = migrator.step()
            steps += 1
        self.assertTrue(progress.done)

    def test_growing_file_is_skipped_not_migrated(self):
        media_probe_cache = {self.present_path: {"duration": 10}}
        migrator = ml.LegacyCacheMigrator(self.probe_library, batch_size=25)
        with mock.patch.object(ml, "compute_identity_key", return_value=None):
            migrator.start(media_probe_cache, {})
            progress = migrator.step()
        self.assertEqual(progress.skipped_growing, 1)
        self.assertEqual(progress.migrated, 0)


class TestStorageAPIs(unittest.TestCase):
    """The per-file field slots store and return values."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")
        self.file_path = os.path.join(self.tmp, "movie.mp4")
        _write_file(self.file_path, os.urandom(50_000))

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_picture_box_round_trip(self):
        ok = self.probe_library.store_picture_box(self.file_path, (10, 0, 1900, 800), detector_version=1)
        self.assertTrue(ok)
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["picture_box"]["rect"], [10, 0, 1900, 800])
        self.assertEqual(entry["picture_box_detector_version"], 1)

    def test_gain_round_trip(self):
        self.probe_library.store_gain(self.file_path, gain=0.87, target_lufs=-20.5, scan_version=2)
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["gain"], 0.87)
        self.assertEqual(entry["gain_target_lufs"], -20.5)
        self.assertEqual(entry["gain_scan_version"], 2)

    def test_captions_round_trip_compressed(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nHello world\n" * 50
        self.probe_library.store_captions(self.file_path, text, source="embedded")
        got = self.probe_library.get_captions_text(self.file_path)
        self.assertEqual(got, text)
        entry = self.probe_library.get_cached(self.file_path)
        self.assertLess(len(entry["captions_blob"]), len(text.encode("utf-8")))

    def test_captions_permanent_no_speech_reason_stored(self):
        self.probe_library.store_captions(self.file_path, "", source="asr", reason="no_speech")
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["captions_reason"], "no_speech")

    def test_pan_scan_memory_round_trip(self):
        self.probe_library.store_pan_scan_memory(self.file_path, {"shots": [{"t": 0, "offset": 0.3}]})
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["pan_scan_memory"]["shots"][0]["offset"], 0.3)


# ============================================================================
# SECTION: Picture-box detection (detection + storage only)
# ============================================================================

def _pb_stderr(frame=(720, 576), boxes=None, dar=None, video_extra="", has_video=True):
    """Returns fake ffmpeg stderr for one cropdetect sample from boxes, a list of (w, h, x, y)."""
    dar_txt = f" DAR {dar[0]}:{dar[1]}" if dar else ""
    lines = ["Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'movie.mp4':",
             "  Duration: 00:00:30.00, start: 0.000000, bitrate: 1000 kb/s"]
    if has_video:
        lines.append(f"    Stream #0:0(und): Video: h264 (High), yuv420p, "
                     f"{frame[0]}x{frame[1]} [SAR 1:1{dar_txt}], 25 fps{video_extra}")
    lines.append("    Stream #0:1(und): Audio: aac (LC), 48000 Hz, stereo, fltp")
    for (w, h, x, y) in (boxes or []):
        lines.append(f"[Parsed_cropdetect_0 @ 0x1] x1:{x} x2:{x + w - 1} y1:{y} y2:{y + h - 1} "
                     f"w:{w} h:{h} x:{x} y:{y} pts:1 t:1.0 limit:32.0 crop={w}:{h}:{x}:{y}")
    return "\n".join(lines) + "\n"


class _FakeSamples:
    """Feeds one canned stderr per ffmpeg call, in order (the last repeats)."""
    def __init__(self, stderrs):
        self.stderrs = list(stderrs)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        text = self.stderrs[min(len(self.calls) - 1, len(self.stderrs) - 1)]
        if isinstance(text, Exception):
            raise text
        return mock.Mock(stderr=text, stdout="", returncode=0)


def _detect(stderrs, duration=3000.0):
    fake = _FakeSamples(stderrs)
    with mock.patch.object(ml.subprocess, "run", fake):
        box = ml.detect_picture_box("movie.mp4", duration=duration, ffmpeg_path="ffmpeg")
    return box, fake


class TestPictureBoxDetection(unittest.TestCase):

    def test_letterbox_found_and_measured(self):
        box, fake = _detect([_pb_stderr((720, 576), [(720, 378, 0, 99)])])
        self.assertEqual(box["rect"], [0, 99, 720, 378])
        self.assertEqual(box["frame"], [720, 576])
        self.assertEqual(box["reason"], "letterbox")
        self.assertTrue(box["has_bars"])
        self.assertAlmostEqual(box["picture_aspect"], 720 / 378, places=3)
        self.assertEqual(box["samples"], 6)
        self.assertEqual(len(fake.calls), 6)

    def test_norm_is_fractions_of_the_frame(self):
        box, _ = _detect([_pb_stderr((720, 576), [(720, 378, 0, 99)])])
        nx, ny, nw, nh = box["norm"]
        self.assertAlmostEqual(ny, 99 / 576, places=4)
        self.assertAlmostEqual(nh, 378 / 576, places=4)
        self.assertEqual((nx, nw), (0.0, 1.0))

    def test_pillarbox_small_baked_side_bars(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(1200, 720, 40, 0)])])
        self.assertEqual(box["reason"], "pillarbox")
        self.assertEqual(box["rect"], [40, 0, 1200, 720])

    def test_windowbox_is_both_axes(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(640, 480, 320, 120)])])
        self.assertEqual(box["reason"], "windowbox")
        self.assertAlmostEqual(box["picture_aspect"], 4 / 3, places=3)

    def test_no_bars_reports_full_frame(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(1280, 720, 0, 0)])])
        self.assertEqual(box["reason"], "no_bars")
        self.assertFalse(box["has_bars"])
        self.assertEqual(box["rect"], [0, 0, 1280, 720])

    def test_true_scope_film_keeps_its_own_shape(self):
        box, _ = _detect([_pb_stderr((1920, 1080), [(1920, 816, 0, 132)])])
        self.assertEqual(box["reason"], "letterbox")
        self.assertGreater(box["picture_aspect"], 2.3)

    def test_widest_box_across_samples_wins_over_a_dark_scene(self):
        dark = _pb_stderr((720, 576), [(600, 300, 60, 130)])
        good = _pb_stderr((720, 576), [(720, 378, 0, 99)])
        box, _ = _detect([good, dark, good, good, dark, good])
        self.assertEqual(box["rect"], [0, 99, 720, 378])

    def test_asymmetric_vertical_bars_leave_that_axis_full_frame(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(1280, 632, 0, 88)])])
        self.assertEqual(box["reason"], "asymmetric_vertical")
        self.assertEqual(box["rect"], [0, 0, 1280, 720])

    def test_one_sided_bar_is_asymmetric(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(1280, 620, 0, 0)])])
        self.assertEqual(box["reason"], "asymmetric_vertical")

    def test_insubstantial_bars_are_ignored(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(1276, 716, 2, 2)])])
        self.assertEqual(box["reason"], "no_bars")
        self.assertEqual(box["rect"], [0, 0, 1280, 720])

    def test_vertical_bars_kept_when_only_horizontal_axis_is_bad(self):
        box, _ = _detect([_pb_stderr((1280, 720), [(1250, 544, 30, 88)])])
        self.assertEqual(box["reason"], "letterbox")
        self.assertEqual(box["rect"][0], 0)
        self.assertEqual(box["rect"][2], 1280)
        self.assertEqual(box["rect"][1:4:2], [88, 544])

    def test_declared_dar_feeds_picture_aspect_not_rect(self):
        box, _ = _detect([_pb_stderr((720, 480), [(720, 480, 0, 0)], dar=(16, 9))])
        self.assertEqual(box["rect"], [0, 0, 720, 480])
        self.assertAlmostEqual(box["pixel_aspect"], (16 / 9) / (720 / 480), places=3)
        self.assertAlmostEqual(box["picture_aspect"], 16 / 9, places=3)

    def test_no_declared_dar_means_pixel_aspect_one(self):
        box, _ = _detect([_pb_stderr((720, 576), [(720, 378, 0, 99)])])
        self.assertEqual(box["pixel_aspect"], 1.0)

    def test_all_samples_timing_out_is_no_answer_not_no_bars(self):
        import subprocess as real_subprocess
        boom = real_subprocess.TimeoutExpired(cmd="ffmpeg", timeout=25)
        box, _ = _detect([boom])
        self.assertIsNone(box)

    def test_too_few_readable_samples_without_timeouts_gets_a_full_frame_label(self):
        good = _pb_stderr((720, 576), [(720, 378, 0, 99)])
        empty = _pb_stderr((720, 576), [])
        box, _ = _detect([good, good, empty, empty, empty, empty])
        self.assertEqual(box["reason"], "thin_samples_full_frame")
        self.assertEqual(box["rect"], [0, 0, 720, 576])
        self.assertFalse(box["has_bars"])

    def test_too_few_readable_samples_with_timeouts_is_no_answer(self):
        import subprocess as real_subprocess
        good = _pb_stderr((720, 576), [(720, 378, 0, 99)])
        boom = real_subprocess.TimeoutExpired(cmd="ffmpeg", timeout=25)
        box, _ = _detect([good, good, boom, boom, boom, boom])
        self.assertIsNone(box)

    def test_audio_only_file_is_recorded_as_no_video(self):
        box, _ = _detect([_pb_stderr(has_video=False)])
        self.assertEqual(box["reason"], "no_video")
        self.assertIsNone(box["rect"])

    def test_cover_art_is_not_video(self):
        box, _ = _detect([_pb_stderr((300, 300), [(300, 300, 0, 0)], video_extra=" (attached pic)")])
        self.assertEqual(box["reason"], "no_video")

    def test_unreadable_file_is_no_answer(self):
        box, _ = _detect(["ffmpeg version 6.0\nbroken.mp4: Invalid data found\n"])
        self.assertIsNone(box)

    def test_decode_uses_two_threads_and_no_audio(self):
        _, fake = _detect([_pb_stderr((720, 576), [(720, 378, 0, 99)])])
        cmd = fake.calls[0]
        self.assertEqual(cmd[cmd.index("-threads") + 1], "2")
        self.assertIn("-an", cmd)
        self.assertIn("-nostdin", cmd)

    def test_detector_version_is_stamped(self):
        box, _ = _detect([_pb_stderr((720, 576), [(720, 378, 0, 99)])])
        self.assertEqual(box["detector_version"], ml.PICTURE_BOX_DETECTOR_VERSION)


class TestPictureBoxSampleTimes(unittest.TestCase):

    def test_samples_stay_between_ten_and_eighty_five_percent(self):
        times = ml._pb_sample_times(6000.0)
        self.assertEqual(len(times), 6)
        self.assertAlmostEqual(times[0], 600.0)
        self.assertAlmostEqual(times[-1], 5100.0)

    def test_short_clip_never_seeks_past_the_end(self):
        for t in ml._pb_sample_times(30.0):
            self.assertLessEqual(t + ml._PB_SAMPLE_SECONDS, 30.0)

    def test_unknown_duration_uses_fixed_offsets(self):
        self.assertEqual(ml._pb_sample_times(None), list(ml._PB_FALLBACK_OFFSETS_S))


class TestPictureBoxInLibrary(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.library = ml.MediaLibrary(os.path.join(self.tmp, "lib.sqlite3"))
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg", use_vlc_fallback=False)
        self.file_path = os.path.join(self.tmp, "movie.avi")
        _write_file(self.file_path, os.urandom(50_000))
        self.box = {"rect": [0, 99, 720, 378], "frame": [720, 576], "norm": [0, 0.17, 1, 0.66],
                    "has_bars": True, "reason": "letterbox", "pixel_aspect": 1.0,
                    "picture_aspect": 1.9048, "samples": 6, "sample_boxes": [],
                    "detector_version": ml.PICTURE_BOX_DETECTOR_VERSION}

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _detect_with(self, box):
        return mock.patch.object(ml, "detect_picture_box", return_value=box)

    def test_result_lands_on_the_same_row_as_the_header_probe(self):
        with mock.patch.object(ml.subprocess, "run",
                               return_value=mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, returncode=0)), \
             self._detect_with(self.box):
            self.probe_library.detect_picture_box_if_needed(self.file_path)
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["picture_box"]["rect"], [0, 99, 720, 378])
        self.assertEqual(entry["picture_box_detector_version"], ml.PICTURE_BOX_DETECTOR_VERSION)
        self.assertAlmostEqual(entry["duration"], 1 * 3600 + 19 * 60 + 35.40, places=2)

    def test_header_probe_runs_first_and_its_duration_feeds_the_sampler(self):
        with mock.patch.object(ml.subprocess, "run",
                               return_value=mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, returncode=0)), \
             self._detect_with(self.box) as det:
            self.probe_library.detect_picture_box_if_needed(self.file_path)
        self.assertAlmostEqual(det.call_args.kwargs["duration"], 1 * 3600 + 19 * 60 + 35.40, places=2)

    def test_second_call_is_a_library_hit_with_no_rescan(self):
        with mock.patch.object(ml.subprocess, "run",
                               return_value=mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, returncode=0)), \
             self._detect_with(self.box) as det:
            self.probe_library.detect_picture_box_if_needed(self.file_path)
            again = self.probe_library.detect_picture_box_if_needed(self.file_path)
        self.assertEqual(det.call_count, 1)
        self.assertEqual(again["rect"], [0, 99, 720, 378])

    def test_deleted_and_readded_file_is_not_rescanned(self):
        with mock.patch.object(ml.subprocess, "run",
                               return_value=mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, returncode=0)), \
             self._detect_with(self.box) as det:
            self.probe_library.detect_picture_box_if_needed(self.file_path)
            with open(self.file_path, "rb") as f:
                data = f.read()
            moved = os.path.join(self.tmp, "renamed.avi")
            os.remove(self.file_path)
            _write_file(moved, data)
            hit = self.probe_library.detect_picture_box_if_needed(moved)
        self.assertEqual(det.call_count, 1)
        self.assertEqual(hit["reason"], "letterbox")

    def test_no_answer_is_not_stored_so_it_retries(self):
        with mock.patch.object(ml.subprocess, "run",
                               return_value=mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, returncode=0)):
            with self._detect_with(None):
                self.assertIsNone(self.probe_library.detect_picture_box_if_needed(self.file_path))
            self.assertIsNone(self.probe_library.get_cached(self.file_path).get("picture_box"))
            with self._detect_with(self.box) as det:
                self.probe_library.detect_picture_box_if_needed(self.file_path)
        self.assertEqual(det.call_count, 1)

    def test_older_detector_version_is_rescanned(self):
        self.probe_library.store_picture_box(self.file_path, (0, 0, 720, 576), detector_version=0)
        self.probe_library.library.upsert_fields(
            self.probe_library.keys.resolve(self.file_path), header_probed_at=time.time(), duration=1400.0)
        with self._detect_with(self.box) as det:
            self.probe_library.detect_picture_box_if_needed(self.file_path)
        self.assertEqual(det.call_count, 1)
        self.assertEqual(self.probe_library.get_cached(self.file_path)["picture_box"]["reason"], "letterbox")

    def test_rescan_touches_only_the_picture_box_field(self):
        key = self.probe_library.keys.resolve(self.file_path)
        self.probe_library.library.upsert_fields(key, header_probed_at=time.time(), duration=1400.0, gain=0.9)
        with self._detect_with(self.box):
            self.probe_library.detect_picture_box_if_needed(self.file_path)
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["gain"], 0.9)
        self.assertEqual(entry["duration"], 1400.0)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class TestPictureBoxRealFfmpeg(unittest.TestCase):
    """Runs the real detector on short synthetic clips built with ffmpeg."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.tmp = tempfile.mkdtemp()
        cls.ffmpeg = shutil.which("ffmpeg")

        def make(name, size, vf):
            out = os.path.join(cls.tmp, name)
            subprocess.run([cls.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", f"testsrc2=size={size}:rate=25:duration=8", "-vf", vf,
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", out], check=True)
            return out

        cls.letterbox = make("letterbox.mp4", "720x378", "pad=720:576:0:99:black")
        cls.fullframe = make("fullframe.mp4", "1280x720", "null")
        cls.pillar = make("pillar.mp4", "1200x720", "pad=1280:720:40:0:black")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _go(self, path):
        return ml.detect_picture_box(path, duration=8.0, ffmpeg_path=self.ffmpeg)

    def test_letterbox_matches_the_beauty_and_the_beast_shape(self):
        box = self._go(self.letterbox)
        self.assertEqual(box["reason"], "letterbox")
        self.assertEqual(box["frame"], [720, 576])
        self.assertAlmostEqual(box["picture_aspect"], 720 / 378, delta=0.02)

    def test_full_frame_has_no_bars(self):
        box = self._go(self.fullframe)
        self.assertEqual(box["reason"], "no_bars")
        self.assertAlmostEqual(box["picture_aspect"], 16 / 9, delta=0.01)

    def test_baked_in_side_bars_on_an_already_16_9_frame_are_found(self):
        box = self._go(self.pillar)
        self.assertEqual(box["reason"], "pillarbox")
        self.assertEqual(box["rect"][0], 40)
        self.assertEqual(box["rect"][2], 1200)

    def test_missing_file_is_no_answer(self):
        self.assertIsNone(self._go(os.path.join(self.tmp, "nope.mp4")))


# ============================================================================
# SECTION: Performance governor worker counts and Whisper model unloading
# ============================================================================


def with_profile(profile, mode, fn):
    old_profile, old_mode = gov.get_profile, gov._resolve_mode
    gov.get_profile = lambda: profile
    try:
        return fn()
    finally:
        gov.get_profile = old_profile


def suite_governor_workers():
    pcs = [(2, 4), (4, 8), (6, 12), (8, 16), (12, 24), (16, 32)]
    for physical, logical in pcs:
        profile = gov.build_profile(physical, 0, logical, 32.0, 24.0)
        for mode in gov.MODE_NAMES:
            ceiling = with_profile(profile, mode, lambda: gov.lane_ceiling("probe_and_duration", mode))
            idle = with_profile(profile, mode, lambda: gov.duration_workers(False, mode))
            playing = with_profile(profile, mode, lambda: gov.duration_workers(True, mode))
            check(1 <= idle <= max(1, ceiling), "duration idle %r within the probe lane ceiling" % ((physical, logical, mode, idle, ceiling),))
            check(1 <= playing <= idle, "duration while playing %r never above idle, never below 1" % ((physical, logical, mode, playing, idle),))
            check(playing == max(1, idle // gov.DURATION_WORKERS_WHILE_PLAYING_DIVISOR) or idle == 1, "duration while playing %r is idle divided" % ((physical, logical, mode),))
            base = with_profile(profile, mode, lambda: gov.loudness_dispatch_workers(False, None, mode))
            spare = with_profile(profile, mode, lambda: gov.loudness_dispatch_workers(True, None, mode))
            lane = with_profile(profile, mode, lambda: gov.lane_ceiling("loudness_and_bake", mode))
            check(1 <= base <= spare <= max(1, lane), "loudness workers %r" % ((physical, logical, mode, base, spare, lane),))
            capped = with_profile(profile, mode, lambda: gov.loudness_dispatch_workers(True, 1, mode))
            check(capped == 1, "loudness spare capped to 1 %r" % ((physical, logical, mode),))
    profile = gov.build_profile(6, 0, 12, 32.0, 24.0)
    lite = with_profile(profile, "lite", lambda: gov.duration_workers(False, "lite"))
    perf = with_profile(profile, "performance", lambda: gov.duration_workers(False, "performance"))
    check(lite == 1 and perf >= lite, "Lite uses 1 duration thread and Performance at least as many (%d, %d)" % (lite, perf))
    for name in ("DURATION_WORKERS_WHILE_PLAYING_DIVISOR", "DURATION_CHUNK_FACTOR_FIRST_PASS", "DURATION_CHUNK_FACTOR_BACKLOG", "LOUDNESS_DISPATCH_WORKERS_BASE", "LOUDNESS_DISPATCH_WORKERS_SPARE", "WHISPER_UNLOAD_WAIT_SECONDS"):
        check(hasattr(gov, name), "governor table has %s" % name)


class FakeModel:
    pass


def reset_media():
    media._whisper_model = FakeModel()
    media._whisper_model_load_failed = False
    media._LIVE_RUNNING_SESSIONS.clear()
    media._whisper_unload_thread = None


def wait_unloaded(limit=3.0):
    end = time.time() + limit
    while time.time() < end:
        if media._whisper_model is None:
            return True
        time.sleep(0.01)
    return media._whisper_model is None


def suite_whisper_unload():
    reset_media()
    check(media.release_whisper_model("test") is True, "unload scheduled when a model is loaded")
    check(wait_unloaded(), "model freed when nothing is running")

    media._whisper_model = None
    check(media.release_whisper_model("test") is False, "nothing to unload when no model is loaded")

    reset_media()
    sess = media.LiveCaptionSession("x.mkv")
    media._LIVE_RUNNING_SESSIONS.add(sess)
    media.release_whisper_model("test")
    time.sleep(0.3)
    check(media._whisper_model is not None, "model kept while a live session is running")
    sess.stop()
    check(sess.cues == [], "stop() clears the finished show's cues")
    media._whisper_unload_thread = None
    media.release_whisper_model("test")
    check(wait_unloaded(), "model freed once the session is stopped")

    reset_media()
    sess = media.LiveCaptionSession("x.mkv")
    sess.cues = [(0.0, 1.0, "hello")]
    sess.stop()
    check(sess.snapshot() == [], "snapshot after stop is empty")

    reset_media()
    held = media._ASR_SEMAPHORE.acquire(timeout=1)
    media._whisper_unload_thread = None
    old_wait = getattr(gov, "WHISPER_UNLOAD_WAIT_SECONDS")
    gov.WHISPER_UNLOAD_WAIT_SECONDS = 0.2
    media._perf_governor = gov
    media.release_whisper_model("test")
    time.sleep(0.6)
    check(media._whisper_model is not None, "model kept while a transcription holds the slot")
    media._ASR_SEMAPHORE.release()
    gov.WHISPER_UNLOAD_WAIT_SECONDS = old_wait
    check(held, "slot was taken for the test")

    reset_media()
    media._whisper_unload_thread = None
    started = []
    for _ in range(5):
        started.append(media.release_whisper_model("test"))
    check(started[0] is True, "repeated calls are safe")
    check(wait_unloaded(), "model freed after repeated calls")
    check(media._ASR_SEMAPHORE.acquire(timeout=1), "slot is released after an unload")
    media._ASR_SEMAPHORE.release()


def extract_source_by_header(name, text):
    start = text.index("def %s" % name)
    lines = text[start:].splitlines(True)
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line.startswith((" ", "\t", ")")) and not line.startswith("#"):
            break
        out.append(line)
    return "".join(out)


def suite_main_hooks():
    text = open(os.path.join(ROOT, "retro_tv_emulator.py"), encoding="utf-8").read()
    calls = []
    fake_media = types.SimpleNamespace(release_whisper_model=lambda reason: calls.append(reason) or True)
    ns = {"media": fake_media, "threading": threading, "_caption_prewarm_lock": threading.Lock(), "_caption_prewarm_started": True,
          "_tv_caption_cache": {"live_session": None, "pending_path": None},
          "_tv_caption_prefetch": {"session": None, "path": None}}
    exec(extract_source_by_header("_unload_caption_model(", text), ns)
    exec(extract_source_by_header("_maybe_unload_caption_model_after_show(", text), ns)
    ns["_maybe_unload_caption_model_after_show"]("show ended")
    check(calls == ["show ended"], "idle caption state: model released at the end of a show")
    check(ns["_caption_prewarm_started"] is False, "pre-warm flag reset so the next captions-on warms again")
    for cache, prefetch, label in (
        ({"live_session": object(), "pending_path": None}, {"session": None, "path": None}, "live session running"),
        ({"live_session": None, "pending_path": "a"}, {"session": None, "path": None}, "next show still resolving"),
        ({"live_session": None, "pending_path": None}, {"session": object(), "path": "b"}, "next show prefetch running"),
        ({"live_session": None, "pending_path": None}, {"session": None, "path": "b"}, "next show prefetch starting"),
    ):
        calls.clear()
        ns["_tv_caption_cache"], ns["_tv_caption_prefetch"] = cache, prefetch
        ns["_maybe_unload_caption_model_after_show"]("show ended")
        check(calls == [], "model kept while %s" % label)


class TestPerformanceGovernorAndWhisper(_SectionCase):
    """Performance governor worker counts and Whisper model unloading"""

    def test_governor_workers(self):
        self.run_section(suite_governor_workers)

    def test_whisper_unload(self):
        self.run_section(suite_whisper_unload)

    def test_main_hooks(self):
        self.run_section(suite_main_hooks)


# ============================================================================
# SECTION: Picture-box fit override (content state, crop, render-site wiring)
# ============================================================================


# ============================================================================
# SECTION: Test helpers
# ============================================================================


def _seg(node):
    lines = MAIN_SRC.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1:node.end_lineno])


_FUNCS = {n.name: n for n in MAIN_TREE.body if isinstance(n, ast.FunctionDef)}


class _Log:
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k): pass
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


def _make_ns(get_cached_return=None, get_cached_raises=None):
    """Builds an isolated namespace with the real picture box functions and a fake single-file probe library."""
    log = _Log()
    calls = {"get_cached": 0}

    class _FakeLib:
        def get_cached(self, path):
            calls["get_cached"] += 1
            if get_cached_raises is not None:
                raise get_cached_raises
            return get_cached_return

    ns = {
        "pygame": pygame, "os": os, "time": __import__("time"), "log": log,
        "_get_probe_library": lambda: _FakeLib(),
        "_PICTURE_BOX_FIT_REASONS": {"letterbox", "pillarbox", "windowbox"},
        "PICTURE_BOX_FIT_RETRY_SECONDS": 2.0,
        "_throttled_log_last_ts": {}, "_THROTTLED_LOG_INTERVAL_S": 30.0,
    }
    for name in ("_log_warn_throttled", "_log_probe_failure", "_pan_scan_content_key", "_picture_box_set_content", "_apply_picture_box_crop"):
        exec(compile(_seg(_FUNCS[name]), f"<extracted:{name}>", "exec"), ns)
    ns["_picture_box_fit_state"] = {"content_key": None, "norm_rect": None, "picture_aspect": None, "retry_at": 0.0}
    return ns, log, calls


def _tmp_video_path(_TmpVideoCache={}):
    if "path" not in _TmpVideoCache:
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".mkv")
        os.write(fd, b"\0" * 1024)
        os.close(fd)
        _TmpVideoCache["path"] = p
    return _TmpVideoCache["path"]


BATB_FRAME = [720, 576]
BATB_RECT = [0, 100, 720, 376]
BATB_NORM = [round(BATB_RECT[0] / BATB_FRAME[0], 5), round(BATB_RECT[1] / BATB_FRAME[1], 5),
             round(BATB_RECT[2] / BATB_FRAME[0], 5), round(BATB_RECT[3] / BATB_FRAME[1], 5)]
BATB_BOX = {
    "rect": BATB_RECT, "frame": BATB_FRAME, "norm": BATB_NORM, "has_bars": True,
    "reason": "letterbox", "pixel_aspect": 1.0, "picture_aspect": 1.9149,
    "samples": 4, "detector_version": 1,
}


# ============================================================================
# SECTION: Picture-box content state & crop
# ============================================================================


class TestPictureBoxSetContent(unittest.TestCase):
    def test_no_scan_yet_is_a_clean_no_op(self):
        ns, log, calls = _make_ns(get_cached_return=None)
        ns["_picture_box_set_content"](_tmp_video_path())
        self.assertIsNone(ns["_picture_box_fit_state"]["norm_rect"])
        self.assertIsNone(ns["_picture_box_fit_state"]["picture_aspect"])
        self.assertEqual(calls["get_cached"], 1)

    def test_no_scan_yet_is_looked_up_again_after_the_retry_window(self):
        ns, log, calls = _make_ns(get_cached_return=None)
        path = _tmp_video_path()
        ns["_picture_box_set_content"](path)
        ns["_picture_box_set_content"](path)
        self.assertEqual(calls["get_cached"], 1, "inside the retry window: no new lookup")
        later = __import__("time").monotonic() + ns["PICTURE_BOX_FIT_RETRY_SECONDS"] + 1.0
        ns["time"] = types.SimpleNamespace(monotonic=lambda: later)
        ns["_picture_box_set_content"](path)
        self.assertEqual(calls["get_cached"], 2, "after the retry window the scan result is looked up again")

    def test_scanned_letterbox_populates_state(self):
        ns, log, calls = _make_ns(get_cached_return={"picture_box": BATB_BOX})
        ns["_picture_box_set_content"](_tmp_video_path())
        self.assertEqual(ns["_picture_box_fit_state"]["norm_rect"], BATB_NORM)
        self.assertEqual(ns["_picture_box_fit_state"]["picture_aspect"], 1.9149)

    def test_no_bars_result_does_not_trigger_an_override(self):
        box = dict(BATB_BOX, reason="no_bars", has_bars=False)
        ns, log, calls = _make_ns(get_cached_return={"picture_box": box})
        ns["_picture_box_set_content"](_tmp_video_path())
        self.assertIsNone(ns["_picture_box_fit_state"]["norm_rect"])

    def test_asymmetric_bars_are_not_trusted(self):
        box = dict(BATB_BOX, reason="asymmetric_vertical")
        ns, log, calls = _make_ns(get_cached_return={"picture_box": box})
        ns["_picture_box_set_content"](_tmp_video_path())
        self.assertIsNone(ns["_picture_box_fit_state"]["norm_rect"])

    def test_pillarbox_and_windowbox_are_also_trusted(self):
        for reason in ("pillarbox", "windowbox"):
            with self.subTest(reason=reason):
                box = dict(BATB_BOX, reason=reason)
                ns, log, calls = _make_ns(get_cached_return={"picture_box": box})
                ns["_picture_box_set_content"](_tmp_video_path())
                self.assertIsNotNone(ns["_picture_box_fit_state"]["norm_rect"])

    def test_library_lookup_is_cached_per_content_key_not_per_frame(self):
        ns, log, calls = _make_ns(get_cached_return={"picture_box": BATB_BOX})
        path = _tmp_video_path()
        for _ in range(5):
            ns["_picture_box_set_content"](path)
        self.assertEqual(calls["get_cached"], 1, "same content_key must not re-query every frame")

    def test_library_exception_is_caught_and_leaves_state_clean(self):
        ns, log, calls = _make_ns(get_cached_raises=RuntimeError("db is locked"))
        ns["_picture_box_set_content"](_tmp_video_path())
        self.assertIsNone(ns["_picture_box_fit_state"]["norm_rect"])
        self.assertTrue(log.warnings)


class TestApplyPictureBoxCrop(unittest.TestCase):
    def _ns_with_state(self, norm_rect, picture_aspect):
        ns, log, calls = _make_ns()
        ns["_picture_box_fit_state"] = {"content_key": "x", "norm_rect": norm_rect,
                                        "picture_aspect": picture_aspect}
        return ns, log

    def test_no_override_returns_the_original_surface_untouched(self):
        ns, log = self._ns_with_state(None, None)
        src = pygame.Surface((720, 480))
        out_surf, aspect = ns["_apply_picture_box_crop"](src)
        self.assertIs(out_surf, src)
        self.assertIsNone(aspect)

    def test_crops_to_the_normalized_rect_at_the_surfaces_own_resolution(self):
        ns, log = self._ns_with_state(BATB_NORM, 1.9149)
        src = pygame.Surface(BATB_FRAME)
        out_surf, aspect = ns["_apply_picture_box_crop"](src)
        self.assertEqual(out_surf.get_size(), (BATB_RECT[2], BATB_RECT[3]))
        self.assertEqual(aspect, 1.9149)

    def test_crop_scales_proportionally_to_a_different_decode_resolution(self):
        ns, log = self._ns_with_state(BATB_NORM, 1.9149)
        scale = 2
        src = pygame.Surface((BATB_FRAME[0] * scale, BATB_FRAME[1] * scale))
        out_surf, aspect = ns["_apply_picture_box_crop"](src)
        self.assertEqual(out_surf.get_size(), (BATB_RECT[2] * scale, BATB_RECT[3] * scale))

    def test_full_frame_rect_is_a_no_op_but_still_returns_the_measured_aspect(self):
        ns, log = self._ns_with_state([0.0, 0.0, 1.0, 1.0], 1.3333)
        src = pygame.Surface((640, 480))
        out_surf, aspect = ns["_apply_picture_box_crop"](src)
        self.assertIs(out_surf, src)
        self.assertEqual(aspect, 1.3333)

    def test_zero_size_surface_is_handled_without_raising(self):
        ns, log = self._ns_with_state(BATB_NORM, 1.9149)
        src = pygame.Surface((0, 0))
        out_surf, aspect = ns["_apply_picture_box_crop"](src)
        self.assertIsNone(aspect)


# ============================================================================
# SECTION: Source-level wiring check (both TV render sites)
# ============================================================================
class TestPictureBoxCropIsWiredIntoBothTvRenderSites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = MAIN_SRC

    def test_both_video_sa_assignments_prefer_the_picture_box_aspect(self):
        count = self.src.count("_video_sa = _snap_standard_aspect(_pb_aspect or (video_surf.get_width() / video_surf.get_height()))")
        self.assertEqual(count, 2, "expected both TV render call sites to use the override")

    def test_both_sites_crop_before_computing_video_sa(self):
        idx = 0
        found = 0
        while True:
            idx = self.src.find("_apply_picture_box_crop(video_surf)", idx)
            if idx == -1:
                break
            found += 1
            after = self.src[idx:idx + 2000]
            self.assertTrue(
                "_video_sa = _snap_standard_aspect(_pb_aspect or" in after
                or "_pv_fit_aspect = _snap_standard_aspect(" in after,
                "crop call must precede the source_aspect it's meant to correct")
            idx += 1
        self.assertEqual(found, 3, "two TV render sites plus the TV Guide preview")

    def test_content_is_registered_before_the_crop_is_applied(self):
        idx = 0
        found = 0
        while True:
            idx = self.src.find("_picture_box_set_content(current_playback_state.get(\"file\", \"\"))", idx)
            if idx == -1:
                break
            after = self.src[idx:idx + 200]
            self.assertIn("_apply_picture_box_crop(video_surf)", after)
            found += 1
            idx += 1
        self.assertEqual(found, 2)


# ============================================================================
# SECTION: CH 03 screen saver edge bouncing
# ============================================================================


# ============================================================================
# SECTION: Test helpers
# ============================================================================
_CONSTANTS = {"pygame": pygame, "_SS_SPEED_FRAC_PER_S": 0.07, "_SS_VISIBLE_MIN_ALPHA": 24, "_SS_MAX_DT_S": 0.1}
_ns = dict(_CONSTANTS)
_ns["_screensaver_visible_rect"] = load_function("_screensaver_visible_rect", _CONSTANTS)
_ns["_screensaver_bounds"] = load_function("_screensaver_bounds", _CONSTANTS)
_visible_rect = _ns["_screensaver_visible_rect"]
_bounds = _ns["_screensaver_bounds"]
_step = load_function("_screensaver_step", _ns)

RESOLUTIONS = [(640, 480), (1280, 720), (1366, 768), (1920, 1080), (2560, 1440), (3840, 2160), (1024, 1024)]


def _padded_logo(w, h, pad_l, pad_t, pad_r, pad_b):
    """A logo surface with a transparent margin around an opaque body."""
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    surf.fill((0, 0, 0, 0))
    surf.fill((255, 255, 255, 255), pygame.Rect(pad_l, pad_t, w - pad_l - pad_r, h - pad_t - pad_b))
    return surf


def _run(scr_w, scr_h, logo, seconds=120, fps=60, vx=0.8, vy=0.6):
    """Simulate the saver and return the extremes reached by the VISIBLE pixels."""
    vis = _visible_rect(logo)
    x, y = float(scr_w) / 3, float(scr_h) / 3
    lo_l = lo_t = 10 ** 9
    hi_r = hi_b = -10 ** 9
    for _ in range(int(seconds * fps)):
        x, y, vx, vy = _step(x, y, vx, vy, 1.0 / fps, scr_w, scr_h, vis)
        bx, by = int(round(x)), int(round(y))
        lo_l, lo_t = min(lo_l, bx + vis.left), min(lo_t, by + vis.top)
        hi_r, hi_b = max(hi_r, bx + vis.right), max(hi_b, by + vis.bottom)
    return lo_l, lo_t, hi_r, hi_b


# ============================================================================
# SECTION: Visible-edge bouncing
# ============================================================================
class TestVisibleRect(unittest.TestCase):
    def test_transparent_padding_is_excluded(self):
        logo = _padded_logo(400, 200, 30, 20, 50, 40)
        self.assertEqual(tuple(_visible_rect(logo)), (30, 20, 320, 140))

    def test_faint_glow_does_not_count_as_the_edge(self):
        logo = _padded_logo(400, 200, 30, 20, 50, 40)
        logo.fill((255, 255, 255, 10), pygame.Rect(0, 0, 400, 5))
        self.assertEqual(_visible_rect(logo).top, 20)

    def test_fully_transparent_falls_back_to_the_whole_surface(self):
        logo = pygame.Surface((100, 50), pygame.SRCALPHA)
        logo.fill((0, 0, 0, 0))
        self.assertEqual(tuple(_visible_rect(logo)), (0, 0, 100, 50))


class TestBoundsReachEveryEdge(unittest.TestCase):
    def test_visible_pixels_touch_all_four_edges_at_every_resolution(self):
        for scr_w, scr_h in RESOLUTIONS:
            with self.subTest(res=(scr_w, scr_h)):
                logo_w = int(scr_w * 0.40)
                logo = _padded_logo(logo_w, logo_w // 3, logo_w // 10, logo_w // 20, logo_w // 8, logo_w // 25)
                l, t, r, b = _run(scr_w, scr_h, logo)
                self.assertEqual((l, t, r, b), (0, 0, scr_w, scr_h))

    def test_unpadded_logo_also_touches_every_edge(self):
        logo = _padded_logo(500, 200, 0, 0, 0, 0)
        self.assertEqual(_run(1920, 1080, logo), (0, 0, 1920, 1080))

    def test_never_goes_past_an_edge(self):
        logo = _padded_logo(300, 120, 20, 10, 20, 10)
        vis = _visible_rect(logo)
        x, y, vx, vy = 5.0, 5.0, -0.8, -0.6
        for _ in range(20000):
            x, y, vx, vy = _step(x, y, vx, vy, 1 / 60, 1280, 720, vis)
            self.assertGreaterEqual(x + vis.left, 0)
            self.assertLessEqual(x + vis.right, 1280)
            self.assertGreaterEqual(y + vis.top, 0)
            self.assertLessEqual(y + vis.bottom, 720)

    def test_logo_larger_than_the_area_is_centered_not_crashed(self):
        vis = pygame.Rect(0, 0, 900, 500)
        min_x, max_x, min_y, max_y = _bounds(800, 400, vis)
        self.assertEqual((min_x, max_x), (-50.0, -50.0))
        self.assertEqual((min_y, max_y), (-50.0, -50.0))


# ============================================================================
# SECTION: Speed independent of resolution and frame rate
# ============================================================================
class TestSpeed(unittest.TestCase):
    def test_horizontal_crossing_time_matches_at_every_resolution(self):
        for scr_w, scr_h in RESOLUTIONS[:-1]:
            with self.subTest(res=(scr_w, scr_h)):
                vis = pygame.Rect(0, 0, int(scr_w * 0.40), 10)
                x, y, vx, vy = 0.0, 0.0, 1.0, 0.0
                n = 0
                while vx > 0:
                    x, y, vx, vy = _step(x, y, vx, vy, 1 / 60, scr_w, scr_h, vis)
                    n += 1
                expected = (1 - vis.width / scr_w) / 0.07 * 60
                self.assertAlmostEqual(n, expected, delta=1.5)

    def test_distance_is_the_same_at_30_and_144_fps(self):
        vis = pygame.Rect(0, 0, 10, 10)
        d = []
        for fps in (30, 144):
            x, y, vx, vy = 100.0, 100.0, 1.0, 0.0
            for _ in range(fps):
                x, y, vx, vy = _step(x, y, vx, vy, 1.0 / fps, 1920, 1080, vis)
            d.append(x - 100.0)
        self.assertAlmostEqual(d[0], d[1], places=6)


# ============================================================================
# SECTION: System tab and Advanced Settings: every position, all four directions
# ============================================================================


OPPOSITE = {mn.UP: mn.DOWN, mn.DOWN: mn.UP, mn.LEFT: mn.RIGHT, mn.RIGHT: mn.LEFT}
KEY = {mn.UP: pygame.K_w, mn.DOWN: pygame.K_s, mn.LEFT: pygame.K_a, mn.RIGHT: pygame.K_d}

# WARNING: these moves have no way back that returns to the same cell; each is how the menu behaved before the core and is waiting on the owner (R9). Changing one is a decision, not a fix.
KNOWN_ONE_WAY = {
    "normal": {
        ("close", mn.UP, "mono_stereo_toggle", mn.DOWN, "advanced_settings"),
        ("exit_program", mn.DOWN, "close", mn.UP, "mono_stereo_toggle"),
        ("captions_color", mn.UP, "pixel_orbiter", mn.DOWN, "captions_toggle"),
        ("captions_color", mn.DOWN, "mono_stereo_toggle", mn.UP, "captions_toggle"),
    },
    "poweroff": {
        ("close", mn.UP, "mono_stereo_toggle", mn.DOWN, "poweroff"),
        ("exit_program", mn.DOWN, "close", mn.UP, "mono_stereo_toggle"),
        ("poweroff", mn.DOWN, "close", mn.UP, "mono_stereo_toggle"),
        ("captions_color", mn.UP, "pixel_orbiter", mn.DOWN, "captions_toggle"),
        ("captions_color", mn.DOWN, "mono_stereo_toggle", mn.UP, "captions_toggle"),
    },
    "kiosk": set(),
    "kiosk_poweroff": set(),
}

KNOWN_ONE_WAY_ADVANCED = {
    ("server_menu_open", mn.UP, "boot_on_start", mn.DOWN, "server_option"),
    ("server_menu_open", mn.DOWN, "close", mn.UP, "server_option"),
}

def n1_check(name, got, want):
    _check_result(got == want, "%s | got %r | want %r" % (name, got, want))


class FakeDB:
    def __init__(self, kiosk):
        self.config = {"kiosk_mode_enabled": kiosk}


CONFIGS = {
    "normal": (False, False),
    "poweroff": (False, True),
    "kiosk": (True, False),
    "kiosk_poweroff": (True, True),
}


def system_rows(kiosk, poweroff):
    return ui.get_system_menu_rows(FakeDB(kiosk), {"shell_takeover_active": poweroff})


def move(table, layer_state, layer, cell, direction):
    return mn.navigate(layer_state, layer, table, cell, direction)


def walk_table(label, table, valid_cells):
    for cell in sorted(table.cells):
        for d in mn.DIRECTIONS:
            to = table.step(cell, d, {})
            n1_check(f"{label}: {cell} {d} lands on a real cell", to is None or to == mn.TAB_BAR or to in valid_cells, True)
    first = next(iter(table.cells))
    seen = {first}
    todo = [first]
    while todo:
        cur = todo.pop()
        for d in mn.DIRECTIONS:
            to = table.step(cur, d, {})
            if to not in (None, mn.TAB_BAR) and to not in seen:
                seen.add(to)
                todo.append(to)
    n1_check(f"{label}: every cell can be reached from the first", seen, set(table.cells))


def way_back(label, table, expected):
    found = set()
    for cell in sorted(table.cells):
        for d in mn.DIRECTIONS:
            memory = {}
            to = table.step(cell, d, memory)
            if to is None or to == mn.TAB_BAR:
                continue
            back = table.step(to, OPPOSITE[d], memory)
            if back != cell:
                found.add((cell, d, to, OPPOSITE[d], back))
    n1_check(f"{label}: moves without a way back are exactly the known ones", found, expected)


def system_checks():
    for name, (kiosk, poweroff) in CONFIGS.items():
        rows = system_rows(kiosk, poweroff)
        table = mn.system_table(rows)
        valid = set(rows) | {mn.CLOSE}
        if "captions_toggle" in rows:
            valid.add(mn.SYSTEM_CAPTIONS_COLOR_CELL)
        walk_table(f"System {name}", table, valid)
        way_back(f"System {name}", table, KNOWN_ONE_WAY[name])
        for sel in list(range(len(rows) + 1)) + ([mn.SYSTEM_CAPTIONS_COLOR_SELECTION] if "captions_toggle" in rows else []):
            n1_check(f"System {name}: selection {sel} converts and back", mn.system_index(rows, mn.system_cell(rows, sel)), sel)
        n1_check(f"System {name}: table is cached", mn.system_table(rows) is table, True)

    rows = system_rows(False, False)
    table = mn.system_table(rows)
    state = {}

    def go(cell, d):
        return move(table, state, "SYSTEM_ROWS", cell, d)

    n1_check("Mono Audio S goes to Advanced Settings", go("mono_stereo_toggle", mn.DOWN), "advanced_settings")
    n1_check("W from Advanced goes back to Mono Audio", go("advanced_settings", mn.UP), "mono_stereo_toggle")
    n1_check("Kiosk Mode S goes to Advanced Settings", go("kiosk_mode", mn.DOWN), "advanced_settings")
    n1_check("W from Advanced goes back to Kiosk Mode", go("advanced_settings", mn.UP), "kiosk_mode")
    go("mono_stereo_toggle", mn.DOWN)
    n1_check("S from Advanced goes to Reset", go("advanced_settings", mn.DOWN), "reset_settings")
    n1_check("W from Reset goes to Advanced", go("reset_settings", mn.UP), "advanced_settings")
    n1_check("the way back survives going down to Reset and up again", go("advanced_settings", mn.UP), "mono_stereo_toggle")
    fresh = {}
    n1_check("with nothing remembered W from Advanced goes to Kiosk Mode", move(table, fresh, "SYSTEM_ROWS", "advanced_settings", mn.UP), "kiosk_mode")
    for direction in (mn.LEFT, mn.RIGHT):
        n1_check(f"Advanced Settings {direction} does nothing on the System tab", go("advanced_settings", direction), None)
    n1_check("W on the first left row leaves to the tab bar", go("game_ch", mn.UP), mn.TAB_BAR)
    n1_check("W on the first right row leaves to the tab bar", go("loudness_scan", mn.UP), mn.TAB_BAR)
    n1_check("D on Captions goes to its colour box", go("captions_toggle", mn.RIGHT), mn.SYSTEM_CAPTIONS_COLOR_CELL)
    n1_check("A on the colour box goes back to Captions", go(mn.SYSTEM_CAPTIONS_COLOR_CELL, mn.LEFT), "captions_toggle")

    rows = system_rows(False, True)
    table = mn.system_table(rows)
    n1_check("with Turn Off Computer showing, Mono Audio S goes to it", move(table, {}, "SYSTEM_ROWS", "mono_stereo_toggle", mn.DOWN), "poweroff")
    n1_check("with Turn Off Computer showing, W from it goes back to Mono Audio", move(table, {}, "SYSTEM_ROWS", "poweroff", mn.UP), "mono_stereo_toggle")

    s1, s2 = {}, {}
    rows = system_rows(False, False)
    table = mn.system_table(rows)
    move(table, s1, "SYSTEM_ROWS", "mono_stereo_toggle", mn.DOWN)
    n1_check("the way back is kept per layer", move(table, s2, "SYSTEM_ROWS", "advanced_settings", mn.UP), "kiosk_mode")


def advanced_checks():
    rows = ui.get_advanced_settings_menu_rows(None, {})
    table = mn.advanced_table(rows)
    visible = [r for r in rows if r != "server_menu_open"]
    walk_table("Advanced", table, set(rows) | {mn.CLOSE})
    way_back("Advanced", table, KNOWN_ONE_WAY_ADVANCED)
    for sel in range(len(rows) + 1):
        n1_check(f"Advanced: selection {sel} converts and back", mn.advanced_index(rows, mn.advanced_cell(rows, sel)), sel)
    for i, row in enumerate(visible):
        n1_check(f"Advanced: S from {row}", table.step(row, mn.DOWN, {}), visible[i + 1] if i + 1 < len(visible) else mn.CLOSE)
        n1_check(f"Advanced: W from {row}", table.step(row, mn.UP, {}), visible[i - 1] if i else None)
        n1_check(f"Advanced: A from {row}", table.step(row, mn.LEFT, {}), None)
        n1_check(f"Advanced: D from {row}", table.step(row, mn.RIGHT, {}), "server_menu_open" if row == "server_option" else None)
    n1_check("Advanced: W from Close goes to the last row", table.step(mn.CLOSE, mn.UP, {}), visible[-1])
    n1_check("Advanced: S, A and D from Close do nothing", [table.step(mn.CLOSE, d, {}) for d in (mn.DOWN, mn.LEFT, mn.RIGHT)], [None, None, None])
    n1_check("Advanced: A from the Menu button returns to Server Mode", table.step("server_menu_open", mn.LEFT, {}), "server_option")
    n1_check("Advanced: D from the Menu button does nothing", table.step("server_menu_open", mn.RIGHT, {}), None)
    n1_check("Advanced: W from the Menu button goes to the row above Server Mode", table.step("server_menu_open", mn.UP, {}), visible[visible.index("server_option") - 1])
    n1_check("Advanced: S from the Menu button goes to Close", table.step("server_menu_open", mn.DOWN, {}), mn.CLOSE)
    position = visible[0]
    for _ in range(len(visible) - 1):
        position = table.step(position, mn.DOWN, {})
    n1_check("Advanced: pressing S down the whole list ends on the last row", position, visible[-1])
    for _ in range(len(visible) - 1):
        position = table.step(position, mn.UP, {})
    n1_check("Advanced: pressing W back up ends on the first row", position, visible[0])


def block_between(source, head, tail):
    a = source.index(head) + len(head)
    b = source.index(tail, a)
    return textwrap.dedent(source[a:b])


def hookup_checks():
    with open(os.path.join(ROOT, "retro_tv_emulator.py"), encoding="utf-8") as f:
        main_source = f.read()

    sys_block = block_between(
        main_source,
        '                elif app_state["menu_layer"] == "SYSTEM_ROWS":\n',
        '                    if menu_nav.is_accept(event.key):\n                        is_enter = True\n')
    adv_block = block_between(
        main_source,
        '                elif app_state["menu_layer"] == "SYSTEM_ADVANCED_SUB_MENU":\n',
        '                    if menu_nav.is_accept(event.key):\n                        if _as_row_id == "server_option" and not UPDATER_AND_SERVER_FEATURES_ENABLED:')

    def build(block, extra):
        code = ("def run(event, app_state, db, get_system_menu_rows, get_advanced_settings_menu_rows, menu_nav,\n"
                "        SERVER_MENU_BTN_SEL, CAPTIONS_COLOR_BOX_SEL, SYSTEM_ROW_META, pygame):\n"
                "    for _once in [0]:\n" + textwrap.indent(block, "        ") + extra)
        ns = {}
        exec(code, ns)
        return ns["run"]

    run_sys = build(sys_block, "        return 'fell-through'\n    return 'moved'\n")
    run_adv = build(adv_block, "        return 'fell-through'\n    return 'moved'\n")

    class Ev:
        def __init__(self, key):
            self.key = key

    def call(runner, state, db, key):
        return runner(Ev(key), state, db, ui.get_system_menu_rows, ui.get_advanced_settings_menu_rows, mn,
                      ui.SERVER_MENU_BTN_SEL, ui.CAPTIONS_COLOR_BOX_SEL, ui.SYSTEM_ROW_META, pygame)

    for name, (kiosk, poweroff) in CONFIGS.items():
        db = FakeDB(kiosk)
        rows = system_rows(kiosk, poweroff)
        table = mn.system_table(rows)
        for sel in range(len(rows) + 1):
            for d in mn.DIRECTIONS:
                state = {"menu_selection_index": sel, "menu_layer": "SYSTEM_ROWS", "shell_takeover_active": poweroff}
                result = call(run_sys, state, db, KEY[d])
                want = mn.navigate({}, "SYSTEM_ROWS", table, mn.system_cell(rows, sel), d)
                if want == mn.TAB_BAR:
                    ok = state["menu_layer"] == "TAB_SELECTION" and state["menu_selection_index"] == sel
                elif want is None:
                    ok = state["menu_layer"] == "SYSTEM_ROWS" and state["menu_selection_index"] == sel
                else:
                    ok = state["menu_layer"] == "SYSTEM_ROWS" and state["menu_selection_index"] == mn.system_index(rows, want)
                n1_check(f"main script System {name}: selection {sel} {d} moves as the table says", (result, ok), ("moved", True))
        state = {"menu_selection_index": 0, "menu_layer": "SYSTEM_ROWS"}
        n1_check(f"main script System {name}: a non-direction key falls through to the old handler",
              call(run_sys, state, db, pygame.K_RETURN), "fell-through")

    adv_rows = ui.get_advanced_settings_menu_rows(None, {})
    adv_table = mn.advanced_table(adv_rows)
    for sel in range(len(adv_rows) + 1):
        for d in mn.DIRECTIONS:
            state = {"menu_selection_index": sel, "menu_layer": "SYSTEM_ADVANCED_SUB_MENU"}
            result = call(run_adv, state, None, KEY[d])
            want = mn.navigate({}, "SYSTEM_ADVANCED_SUB_MENU", adv_table, mn.advanced_cell(adv_rows, sel), d)
            expect = sel if want is None else mn.advanced_index(adv_rows, want)
            n1_check(f"main script Advanced: selection {sel} {d} moves as the table says",
                  (result, state["menu_selection_index"]), ("moved", expect))
    state = {"menu_selection_index": 0, "menu_layer": "SYSTEM_ADVANCED_SUB_MENU"}
    n1_check("main script Advanced: a non-direction key falls through to the Enter handler",
          call(run_adv, state, None, pygame.K_RETURN), "fell-through")

    sys_rows = system_rows(False, False)
    state = {"menu_selection_index": sys_rows.index("mono_stereo_toggle"), "menu_layer": "SYSTEM_ROWS"}
    call(run_sys, state, FakeDB(False), pygame.K_s)
    n1_check("main script: Mono Audio S lands on Advanced Settings", sys_rows[state["menu_selection_index"]], "advanced_settings")
    call(run_sys, state, FakeDB(False), pygame.K_RETURN)
    state["menu_selection_index"] = adv_rows.index("update_check")
    state["menu_layer"] = "SYSTEM_ROWS"
    state["menu_selection_index"] = sys_rows.index("advanced_settings")
    call(run_sys, state, FakeDB(False), pygame.K_w)
    n1_check("main script: W from Advanced Settings goes back to Mono Audio", sys_rows[state["menu_selection_index"]], "mono_stereo_toggle")

    inj = block_between(
        main_source,
        '        _mnav_held_set = app_state["_held_keys"]\n',
        '    _fgap_last = app_state.get("_frame_gap_last_ticks")')
    inj_code = ("def run(app_state, _mnav_held_set, _mnav_matrix, events, now_ticks, pygame, log, _menu_nav_trace, _MENU_NAV_REPEAT_DELAY_MS):\n"
                "    if True:\n" + textwrap.indent(inj, "        "))
    ns = {"menu_nav": mn}
    exec(inj_code, ns)

    def tick(app_state, held, now, events):
        matrix = {pygame.K_w: True, pygame.K_s: True, pygame.K_a: True, pygame.K_d: True}
        ns["run"](app_state, held, matrix, events, now, pygame, types.SimpleNamespace(warning=lambda *a, **k: None),
                  lambda *a, **k: None, 400)

    def repeats(events):
        return [e for e in events if getattr(e, "menu_repeat", False)]

    def hold(layer, selection, key, down_layer=None):
        state = {"menu_layer": layer, "menu_selection_index": selection, "_held_keys": {key}, "_held_keys_source": {},
                 "_held_key_down_layer": {key: down_layer or layer}, "_hold_menu": mn.HoldState()}
        events = []
        for now in (1000, 1500, 1700, 1900, 2100):
            tick(state, state["_held_keys"], now, events)
        return len(repeats(events))

    n1_check("a slider key held on a Video slider row repeats", hold("VIDEO_ROWS", 5, pygame.K_d) >= 1, True)
    n1_check("a slider key held on the last Video slider row repeats", hold("VIDEO_ROWS", 9, pygame.K_a) >= 1, True)
    n1_check("a slider key held on a Theme slider row repeats", hold("THEME_ROWS", 3, pygame.K_d) >= 1, True)
    n1_check("a slider key held on the last Theme slider row repeats", hold("THEME_ROWS", 5, pygame.K_a) >= 1, True)
    n1_check("W held on a Video slider row repeats", hold("VIDEO_ROWS", 5, pygame.K_w) >= 1, True)
    n1_check("S held on a Theme slider row repeats", hold("THEME_ROWS", 4, pygame.K_s) >= 1, True)
    n1_check("D held on a Video radio row repeats", hold("VIDEO_ROWS", 1, pygame.K_d) >= 1, True)
    n1_check("D held on the Theme transition row repeats", hold("THEME_ROWS", 2, pygame.K_d) >= 1, True)
    n1_check("S held on the tab bar repeats", hold("TAB_SELECTION", 4, pygame.K_s) >= 1, True)
    n1_check("S held in System repeats", hold("SYSTEM_ROWS", 3, pygame.K_s) >= 1, True)
    n1_check("A held in Advanced Settings repeats", hold("SYSTEM_ADVANCED_SUB_MENU", 0, pygame.K_a) >= 1, True)
    n1_check("a key held in the controller map never repeats", hold("GAMES_CTRL_MAP", 0, pygame.K_s), 0)
    n1_check("a key held in the DVD button map never repeats", hold("DVD_BTN_MAP", 0, pygame.K_d), 0)
    n1_check("a slider key that went down in another layer does not repeat", hold("VIDEO_ROWS", 5, pygame.K_d, "TAB_SELECTION"), 0)


def repeat_allowed_checks():
    for layer in ("VIDEO_ROWS", "THEME_ROWS", "TAB_SELECTION", "SYSTEM_ROWS", "SYSTEM_ADVANCED_SUB_MENU", "CHANNEL_LIST",
                  "CHANNEL_SETTINGS_SUB_MENU", "GAMES_LIST", "GAMES_SUB_MENU", "GAMES_SAVE_PROFILE", "DVD_BTN_MAP", "GAMES_CTRL_MAP",
                  "SERVER_MENU", "SERVER_SLOTS_MENU", "REMOTE_REMAP_LIST", "AUDIO_TRACK_PICKER", "HOLIDAY_SUB_MENU", None):
        for key in (pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d, pygame.K_RETURN, pygame.K_UP, pygame.K_LEFT):
            expected = key in (pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d) and layer not in mn.NO_REPEAT_LAYERS
            n1_check("repeat_allowed %s %s" % (layer, pygame.key.name(key)), mn.repeat_allowed(layer, key), expected)
            n1_check("repeat_allowed suppressed %s %s" % (layer, pygame.key.name(key)), mn.repeat_allowed(layer, key, True), False)
    n1_check("the capture layers are the four that map keys", tuple(mn.NO_REPEAT_LAYERS), ("GAMES_CTRL_MAP", "DVD_BTN_MAP", "REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"))


class TestMenuNavSystemTab(_SectionCase):
    """System tab and Advanced Settings: every position, all four directions"""

    def test_system(self):
        self.run_section(system_checks)

    def test_advanced(self):
        self.run_section(advanced_checks)

    def test_hookup(self):
        self.run_section(hookup_checks)

    def test_repeat_allowed(self):
        self.run_section(repeat_allowed_checks)


# ============================================================================
# SECTION: Channels, channel settings, Video, Theme, Games list and sub-menu, DVD button map, Save Profile, controller map
# ============================================================================


n2_KEYS = {"W": pygame.K_w, "A": pygame.K_a, "S": pygame.K_s, "D": pygame.K_d}
DIRS = {"W": mn.UP, "S": mn.DOWN, "A": mn.LEFT, "D": mn.RIGHT}


def go(table, cell, direction, memory=None):
    return table.step(cell, direction, {} if memory is None else memory)


# ============================================================================
# SECTION: Handler extraction
# ============================================================================

HEADERS = {
    "CHANNEL_LIST": 'elif app_state["menu_layer"] == "CHANNEL_LIST":',
    "CHANNEL_SETTINGS_SUB_MENU": 'elif app_state["menu_layer"] == "CHANNEL_SETTINGS_SUB_MENU":',
    "VIDEO_ROWS": 'elif app_state["menu_layer"] == "VIDEO_ROWS":',
    "THEME_ROWS": 'elif app_state["menu_layer"] == "THEME_ROWS":',
    "GAMES_LIST": 'elif app_state["menu_layer"] == "GAMES_LIST":',
    "GAMES_SUB_MENU": 'elif app_state["menu_layer"] == "GAMES_SUB_MENU":',
    "DVD_BTN_MAP": 'elif app_state["menu_layer"] == "DVD_BTN_MAP":',
    "GAMES_SAVE_PROFILE": 'elif app_state["menu_layer"] == "GAMES_SAVE_PROFILE":',
}

CONSOLES = ["MENU", "DVD", "A2600", "GB", "GBA", "GBC", "GG", "NGP", "NGPC", "N64", "NES", "PSX", "32X", "GENESIS", "MASTERSYSTEM", "SNES"]
GAME_DECK_STUB = types.SimpleNamespace(CONSOLE_ORDER=list(CONSOLES), CONSOLE_LABELS={})
_BRANCH_PREFIX = '                elif app_state["menu_layer"] == '


def extract_branch(script_path, layer):
    """Returns the body of one layer's key-handler branch, wrapped in a one-pass loop so its continue statements work."""
    with open(script_path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() == HEADERS[layer].strip() and line.startswith("                elif"):
            start = i
            break
    if start is None:
        raise RuntimeError("branch not found: " + layer)
    body = []
    for line in lines[start + 1:]:
        if line.startswith(_BRANCH_PREFIX) or (line.strip() and not line.startswith(" " * 20)):
            break
        body.append(line[20:] if line.strip() else "")
    text = "for _once in (0,):\n" + "\n".join("    " + b if b else "" for b in body) + "\n"
    return compile(text, script_path + ":" + layer, "exec")


class Outcome(object):
    def __init__(self, state, config):
        self.layer = state["menu_layer"]
        self.sel = state["menu_selection_index"]
        self.row2 = state.get("theme_row2_col", 0)
        self.config = dict(config)

    def key(self):
        return (self.layer, self.sel, self.row2, tuple(sorted(self.config.items())))


def run_handler(code, layer, rows, sel, key, row2=0, page=0, config=None, memory=None):
    state = {"menu_layer": layer, "menu_selection_index": sel, "theme_row2_col": row2, "channels_tab_page": page, "games_tab_page": page,
             "show_menu": True}
    if memory is not None:
        state["_nav_memory"] = {layer: dict(memory)}
    cfg = dict(config or {})
    db = types.SimpleNamespace(config=cfg, channels_db={}, save_settings=lambda: None)
    env = {
        "pygame": pygame, "menu_nav": mn, "app_state": state, "db": db,
        "event": types.SimpleNamespace(key=n2_KEYS[key]),
        "get_channels_settings_menu_rows": lambda _db, _state: list(rows or []),
        "_mark_settings_dirty": lambda: None, "now_ticks": 0, "log": types.SimpleNamespace(info=lambda *a, **k: None),
    }
    exec(code, env)
    return Outcome(state, cfg), state


# ============================================================================
# SECTION: Table checks
# ============================================================================

def _remember_names(table, cell, target):
    if not isinstance(target, mn.Remember):
        return [target]
    allowed = table.remember.get(cell, ())
    if isinstance(allowed, dict):
        allowed = [name for names in allowed.values() for name in names]
    return [target.default] + list(allowed)


def reachable(table, start):
    seen = {start}
    todo = [start]
    while todo:
        cell = todo.pop()
        for target in table.cells.get(cell, {}).values():
            names = _remember_names(table, cell, target)
            for name in names:
                if name != mn.TAB_BAR and name not in seen and name in table.cells:
                    seen.add(name)
                    todo.append(name)
    return seen


def structure(table, label, start):
    bad = []
    for cell, entry in table.cells.items():
        for direction, target in entry.items():
            names = _remember_names(table, cell, target)
            for name in names:
                if name != mn.TAB_BAR and name not in table.cells:
                    bad.append((cell, direction, name))
    check(not bad, "%s: every link lands on a known cell %s" % (label, bad))
    seen = reachable(table, start)
    check(seen == set(table.cells), "%s: every cell is reachable from %s, missing %s" % (label, start, sorted(set(table.cells) - seen)))
    for cell in table.cells:
        for direction in mn.DIRECTIONS:
            res = go(table, cell, direction)
            check(res is None or res == mn.TAB_BAR or res in table.cells, "%s: %s %s lands on a known cell" % (label, cell, direction))


def case_channels():
    for page in range(mn.CHANNELS_NUM_PAGES):
        t = mn.channels_table(page)
        structure(t, "channels page %d" % page, mn.channels_cell(page * mn.CHANNELS_PAGE_SIZE))
        base = page * mn.CHANNELS_PAGE_SIZE
        name = lambda k: mn.channels_cell(base + k)
        check(go(t, name(0), mn.UP) == mn.TAB_BAR and go(t, name(3), mn.UP) == mn.TAB_BAR, "channels p%d: top row goes to the tab bar" % page)
        check(go(t, name(0), mn.DOWN) == name(4) and go(t, name(4), mn.UP) == name(0), "channels p%d: down and up are one row" % page)
        check(go(t, name(0), mn.LEFT) is None and go(t, name(3), mn.RIGHT) is None, "channels p%d: no wrap sideways" % page)
        check(go(t, name(1), mn.RIGHT) == name(2) and go(t, name(2), mn.LEFT) == name(1), "channels p%d: left and right are one cell" % page)
        for k in (16, 17):
            check(go(t, name(k), mn.DOWN) == "settings", "channels p%d: bottom left half goes to Settings (%d)" % (page, k))
        for k in (18, 19):
            check(go(t, name(k), mn.DOWN) == "page_button", "channels p%d: bottom right half goes to the page button (%d)" % (page, k))
        check(go(t, "settings", mn.UP) == name(16), "channels p%d: Settings up defaults to column 0 of the last row" % page)
        check(go(t, "page_button", mn.UP) == name(19), "channels p%d: page button up defaults to the last cell" % page)
        check(go(t, "settings", mn.RIGHT) == "page_button" and go(t, "page_button", mn.LEFT) == "settings", "channels p%d: Settings and page button are neighbours" % page)
        check(go(t, "settings", mn.LEFT) is None and go(t, "page_button", mn.RIGHT) is None, "channels p%d: nothing beyond the two buttons" % page)
        check(go(t, "settings", mn.DOWN) == mn.CLOSE and go(t, "page_button", mn.DOWN) == mn.CLOSE, "channels p%d: both buttons go down to Close" % page)
        check(go(t, mn.CLOSE, mn.LEFT) is None and go(t, mn.CLOSE, mn.RIGHT) is None and go(t, mn.CLOSE, mn.DOWN) is None, "channels p%d: Close only goes up" % page)
        for k in range(16, 20):
            memory = {}
            down = go(t, name(k), mn.DOWN, memory)
            check(go(t, down, mn.UP, memory) == name(k), "channels p%d: way back from %s through %s" % (page, name(k), down))
        for button in ("settings", "page_button"):
            memory = {}
            go(t, button, mn.DOWN, memory)
            check(go(t, mn.CLOSE, mn.UP, memory) == button, "channels p%d: Close returns to %s" % (page, button))
        check(go(t, mn.CLOSE, mn.UP) == "settings", "channels p%d: Close with no memory goes to Settings" % page)
        memory = {"settings": name(17)}
        other = {"settings": mn.channels_cell(((page + 1) % mn.CHANNELS_NUM_PAGES) * mn.CHANNELS_PAGE_SIZE + 17)}
        check(go(t, "settings", mn.UP, memory) == name(17), "channels p%d: memory on this page is used" % page)
        if mn.CHANNELS_NUM_PAGES > 1:
            check(go(t, "settings", mn.UP, other) == name(16), "channels p%d: memory from another page is ignored" % page)
    for sel in list(range(mn.CHANNELS_TOTAL)) + [42, 43, 99]:
        check(mn.channels_index(mn.channels_cell(sel)) == sel, "channels: selection %d round trip" % sel)
    check(mn.channels_cell(41) is None and mn.channels_cell(-1) is None and mn.channels_cell(None) is None, "channels: unknown selection gives None")
    check(mn.channels_cell(0) == "ch05" and mn.channels_cell(39) == "ch44", "channels: cell names are channel numbers")
    check(mn.channels_page_of(25, 0) == 1 and mn.channels_page_of(19, 1) == 0, "channels: page of a channel cell")
    check(mn.channels_page_of(43, 1) == 1 and mn.channels_page_of(99, 7) == mn.CHANNELS_NUM_PAGES - 1, "channels: page of a button uses the stored page, kept in range")


ROWS_WITH_STYLE = ["bezel_toggle", "bezel_style_select", "warp_screen", "scan_lines", "watermark", "watermark_select",
                   "boot_anim", "static_noise", "reception_lines"]
ROWS_NO_STYLE = [r for r in ROWS_WITH_STYLE if r != "bezel_style_select"]


def case_channel_settings():
    for rows in (ROWS_WITH_STYLE, ROWS_NO_STYLE):
        label = "channel settings (%d rows)" % len(rows)
        t = mn.channel_settings_table(rows)
        structure(t, label, "bezel_toggle")
        check(mn.channel_settings_table(rows) is t, label + ": table is cached")
        check(go(t, "bezel_toggle", mn.UP) is None and go(t, "watermark", mn.UP) is None, label + ": first rows do not leave upward")
        check(go(t, "bezel_toggle", mn.DOWN) == "warp_screen" and go(t, "warp_screen", mn.DOWN) == "scan_lines", label + ": left column goes down")
        check(go(t, "scan_lines", mn.DOWN) == "reception_lines" and go(t, "static_noise", mn.DOWN) == "reception_lines", label + ": both columns end at Reception Lines")
        check(go(t, "reception_lines", mn.DOWN) == mn.CLOSE and go(t, mn.CLOSE, mn.UP) == "reception_lines", label + ": Reception Lines and Close")
        check(go(t, "reception_lines", mn.LEFT) is None and go(t, "reception_lines", mn.RIGHT) is None, label + ": Reception Lines has no sideways move")
        check(go(t, mn.CLOSE, mn.LEFT) is None and go(t, mn.CLOSE, mn.RIGHT) is None and go(t, mn.CLOSE, mn.DOWN) is None, label + ": Close only goes up")
        check(go(t, "watermark", mn.RIGHT) == "watermark_select" and go(t, "watermark_select", mn.LEFT) == "watermark", label + ": Add hangs off Water Mark")
        check(go(t, "watermark_select", mn.RIGHT) is None, label + ": nothing right of Add")
        check(go(t, "watermark_select", mn.UP) is None and go(t, "watermark_select", mn.DOWN) == "boot_anim", label + ": Add moves vertically like its row")
        check(go(t, "watermark", mn.LEFT) == "bezel_toggle" and go(t, "boot_anim", mn.LEFT) == "warp_screen" and go(t, "static_noise", mn.LEFT) == "scan_lines", label + ": right column goes left row by row")
        check(go(t, "scan_lines", mn.RIGHT) == "static_noise" and go(t, "warp_screen", mn.RIGHT) == "boot_anim", label + ": left column goes right row by row")
        if "bezel_style_select" in rows:
            check(go(t, "bezel_toggle", mn.RIGHT) == "bezel_style_select" and go(t, "bezel_style_select", mn.LEFT) == "bezel_toggle", label + ": the style box hangs off Bezel")
            check(go(t, "bezel_style_select", mn.RIGHT) == "watermark", label + ": right of the style box is the right column")
            check(go(t, "bezel_style_select", mn.DOWN) == "warp_screen" and go(t, "bezel_style_select", mn.UP) is None, label + ": the style box moves vertically like its row")
        else:
            check(go(t, "bezel_toggle", mn.RIGHT) == "watermark", label + ": Bezel goes straight to the right column")
        for end in ("scan_lines", "static_noise"):
            memory = {}
            go(t, end, mn.DOWN, memory)
            check(go(t, "reception_lines", mn.UP, memory) == end, label + ": Reception Lines returns to " + end)
        check(go(t, "reception_lines", mn.UP) == "scan_lines", label + ": Reception Lines with no memory goes to the left column")
        for index, row in enumerate(rows):
            check(mn.channel_settings_index(rows, mn.channel_settings_cell(rows, index)) == index, label + ": selection %d round trip" % index)
        check(mn.channel_settings_cell(rows, len(rows)) == mn.CLOSE and mn.channel_settings_index(rows, mn.CLOSE) == len(rows), label + ": Close is one past the rows")


def case_video():
    t = mn.video_table()
    structure(t, "video", "mode_normal")
    check(mn.video_table() is t, "video: table is cached")
    radios = mn.VIDEO_RADIOS
    check(radios == ("mode_normal", "mode_stretch", "mode_crop", "mode_panscan"), "video: four screen-mode radios in a row")
    check(mn.VIDEO_SLIDERS == ("brightness", "contrast", "color", "sharpness", "tint"), "video: five sliders in a column")
    for k, radio in enumerate(radios):
        check(go(t, radio, mn.UP) == mn.TAB_BAR and go(t, radio, mn.DOWN) == "brightness", "video: %s goes up and down" % radio)
        check(go(t, radio, mn.LEFT) == (radios[k - 1] if k else None), "video: %s left" % radio)
        check(go(t, radio, mn.RIGHT) == (radios[k + 1] if k + 1 < len(radios) else None), "video: %s right" % radio)
        memory = {}
        go(t, radio, mn.DOWN, memory)
        check(go(t, "brightness", mn.UP, memory) == radio, "video: way back to %s from Brightness" % radio)
    check(go(t, "brightness", mn.UP) == "mode_normal", "video: Brightness up with no memory goes to Normal Mode")
    sliders = mn.VIDEO_SLIDERS
    for k, slider in enumerate(sliders):
        check(go(t, slider, mn.DOWN) == (sliders[k + 1] if k + 1 < len(sliders) else mn.CLOSE), "video: %s down" % slider)
        if k:
            check(go(t, slider, mn.UP) == sliders[k - 1], "video: %s up" % slider)
        check(go(t, slider, mn.LEFT) is None and go(t, slider, mn.RIGHT) is None, "video: %s sideways is left to the value change" % slider)
    check(go(t, mn.CLOSE, mn.UP) == "tint" and go(t, mn.CLOSE, mn.DOWN) is None and go(t, mn.CLOSE, mn.LEFT) is None, "video: Close only goes up to Tint")
    for sel in range(10):
        check(mn.video_index(mn.video_cell(sel)) == sel, "video: selection %d round trip" % sel)
    check(mn.video_cell(9) == mn.CLOSE, "video: selection 9 is Close")
    check("aspect_ratio" not in t.cells, "video: there is no Aspect Ratio row")


def case_theme():
    t = mn.theme_table()
    structure(t, "theme", "volume_bar")
    check(mn.theme_table() is t, "theme: table is cached")
    check(go(t, "volume_bar", mn.UP) == mn.TAB_BAR and go(t, "static_color", mn.UP) == mn.TAB_BAR, "theme: the top pair goes up to the tab bar")
    check(go(t, "volume_bar", mn.RIGHT) == "static_color" and go(t, "static_color", mn.LEFT) == "volume_bar", "theme: Volume Bar and Channel Static are neighbours")
    check(go(t, "volume_bar", mn.LEFT) is None and go(t, "static_color", mn.RIGHT) is None, "theme: nothing beyond the top pair")
    check(go(t, "volume_bar", mn.DOWN) == "transition" and go(t, "static_color", mn.DOWN) == "letterbox", "theme: each top button goes down to the button under it")
    check(go(t, "transition", mn.RIGHT) == "letterbox" and go(t, "letterbox", mn.LEFT) == "transition", "theme: Transition and Letterboxing are neighbours")
    check(go(t, "transition", mn.LEFT) is None and go(t, "letterbox", mn.RIGHT) is None, "theme: nothing beyond the second pair")
    check(go(t, "transition", mn.UP) == "volume_bar" and go(t, "letterbox", mn.UP) == "static_color", "theme: each second-row button goes up to the button above it")
    check(go(t, "transition", mn.DOWN) == "opacity" and go(t, "letterbox", mn.DOWN) == "opacity", "theme: both second-row buttons go down to Menu Opacity")
    check(go(t, "opacity", mn.UP) == "transition", "theme: W from Opacity lands on Channel Transition, never on Letterboxing")
    for a, b in (("opacity", "bg_hue"), ("bg_hue", "ui_hue"), ("ui_hue", mn.CLOSE)):
        check(go(t, a, mn.DOWN) == b and go(t, b, mn.UP) == a, "theme: %s and %s are neighbours" % (a, b))
    for slider in mn.THEME_SLIDERS:
        check(go(t, slider, mn.LEFT) is None and go(t, slider, mn.RIGHT) is None, "theme: %s sideways is left to the value change" % slider)
    check(go(t, mn.CLOSE, mn.DOWN) is None and go(t, mn.CLOSE, mn.LEFT) is None and go(t, mn.CLOSE, mn.RIGHT) is None, "theme: Close only goes up")
    for sel in range(6):
        cell = mn.theme_cell(sel, 0)
        check(mn.theme_index(cell) == sel and mn.theme_row2_column(cell) == 0, "theme: selection %d round trip" % sel)
    check(mn.theme_cell(0, 1) == "static_color" and mn.theme_index("static_color") == 0 and mn.theme_row2_column("static_color") == 1, "theme: row 0 column 1 is Channel Static")
    check(mn.theme_cell(1, 1) == "letterbox" and mn.theme_index("letterbox") == 1 and mn.theme_row2_column("letterbox") == 1, "theme: row 1 column 1 is Letterboxing")
    check(mn.theme_cell(2, 1) == "opacity", "theme: a stale right column does not change other rows")
    check(mn.theme_cell(5, 0) == mn.CLOSE, "theme: selection 5 is Close")


# ============================================================================
# SECTION: Real handlers
# ============================================================================

def expected(table, cell, key, memory=None):
    return table.step(cell, DIRS[key], {} if memory is None else memory)


def case_handlers():
    if not os.path.exists(MAIN_SCRIPT):
        check(False, "main script not found at " + MAIN_SCRIPT)
        return
    code = {layer: extract_branch(MAIN_SCRIPT, layer) for layer in HEADERS}
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    for needle in ("menu_nav.channels_table(", "menu_nav.channel_settings_table(", "menu_nav.video_table()", "menu_nav.theme_table()"):
        check(text.count(needle) == 1, "main script: one hookup for " + needle)
    check("_cl_total = 40" not in text, "main script: the tab bar no longer carries its own copy of the channel count")

    for page in range(mn.CHANNELS_NUM_PAGES):
        table = mn.channels_table(page)
        for sel in [page * mn.CHANNELS_PAGE_SIZE + k for k in range(mn.CHANNELS_PAGE_SIZE)] + [42, 43, 99]:
            for key in "WASD":
                out, _ = run_handler(code["CHANNEL_LIST"], "CHANNEL_LIST", None, sel, key, page=page)
                want = expected(table, mn.channels_cell(sel), key)
                if want == mn.TAB_BAR:
                    ok = out.layer == "TAB_SELECTION"
                elif want is None:
                    ok = out.layer == "CHANNEL_LIST" and out.sel == sel
                else:
                    ok = out.layer == "CHANNEL_LIST" and out.sel == mn.channels_index(want)
                check(ok, "handler channels p%d sel %d %s -> %s %s (table says %s)" % (page, sel, key, out.layer, out.sel, want))
    out, _ = run_handler(code["CHANNEL_LIST"], "CHANNEL_LIST", None, 17, "S", page=0)
    out2, st = run_handler(code["CHANNEL_LIST"], "CHANNEL_LIST", None, 43, "W", page=0, memory={"settings": "ch22"})
    check(out.sel == 43 and out2.sel == 17, "handler channels: S then W returns to where it started (through remembered memory)")
    out, st = run_handler(code["CHANNEL_LIST"], "CHANNEL_LIST", None, 17, "S", page=0)
    out2, _ = run_handler(code["CHANNEL_LIST"], "CHANNEL_LIST", None, 43, "W", page=0, memory=st["_nav_memory"]["CHANNEL_LIST"])
    check(out2.sel == 17, "handler channels: the memory the handler wrote is the one that brings it back")

    for rows in (ROWS_WITH_STYLE, ROWS_NO_STYLE):
        table = mn.channel_settings_table(rows)
        for sel in range(len(rows) + 1):
            for key in "WASD":
                out, _ = run_handler(code["CHANNEL_SETTINGS_SUB_MENU"], "CHANNEL_SETTINGS_SUB_MENU", rows, sel, key)
                want = expected(table, mn.channel_settings_cell(rows, sel), key)
                ok = out.sel == (sel if want is None else mn.channel_settings_index(rows, want)) and out.layer == "CHANNEL_SETTINGS_SUB_MENU"
                check(ok, "handler channel settings (%d rows) sel %d %s -> %s (table says %s)" % (len(rows), sel, key, out.sel, want))

    table = mn.video_table()
    cfg = {name: 50 for name in ("brightness", "contrast", "color", "sharpness", "tint")}
    for sel in range(11):
        for key in "WASD":
            out, _ = run_handler(code["VIDEO_ROWS"], "VIDEO_ROWS", None, sel, key, config=cfg)
            cell = mn.video_cell(sel)
            want = expected(table, cell, key)
            is_value_edit = cell in mn.VIDEO_SLIDERS and key in "AD"
            if want == mn.TAB_BAR:
                ok = out.layer == "TAB_SELECTION"
            else:
                ok = out.layer == "VIDEO_ROWS" and out.sel == (sel if want is None else mn.video_index(want))
            if is_value_edit:
                ok = ok and out.config[cell] == (55 if key == "D" else 45) and all(out.config[o] == 50 for o in cfg if o != cell)
            else:
                ok = ok and out.config == cfg
            check(ok, "handler video sel %d %s -> %s %s %s (table says %s)" % (sel, key, out.layer, out.sel, out.config, want))

    table = mn.theme_table()
    cfg = {"menu_opacity": 50, "theme_bg_hue": 220, "theme_ui_hue": 140}
    cfg_key = {"opacity": "menu_opacity", "bg_hue": "theme_bg_hue", "ui_hue": "theme_ui_hue"}
    for sel in range(7):
        for col in ((0, 1) if sel == 2 else (0,)):
            for key in "WASD":
                out, _ = run_handler(code["THEME_ROWS"], "THEME_ROWS", None, sel, key, row2=col, config=cfg)
                cell = mn.theme_cell(sel, col)
                want = expected(table, cell, key)
                is_value_edit = cell in mn.THEME_SLIDERS and key in "AD"
                if want == mn.TAB_BAR:
                    ok = out.layer == "TAB_SELECTION"
                elif want is None:
                    ok = out.layer == "THEME_ROWS" and out.sel == sel and out.row2 == col
                else:
                    ok = out.layer == "THEME_ROWS" and out.sel == mn.theme_index(want) and out.row2 == mn.theme_row2_column(want)
                if is_value_edit:
                    changed = {k: v for k, v in out.config.items() if v != cfg[k]}
                    ok = ok and changed == {cfg_key[cell]: cfg[cfg_key[cell]] + (5 if key == "D" else -5)}
                else:
                    ok = ok and out.config == cfg
                check(ok, "handler theme sel %d col %d %s -> %s %s col %s %s (table says %s)" % (sel, col, key, out.layer, out.sel, out.row2, out.config, want))
    out, _ = run_handler(code["THEME_ROWS"], "THEME_ROWS", None, 3, "W", row2=1, config=cfg)
    check(out.sel == 2 and out.row2 == 0, "handler theme: W onto row 2 always lands on Channel Transition, even with a stale column")
    out, _ = run_handler(code["THEME_ROWS"], "THEME_ROWS", None, 1, "S", row2=1, config=cfg)
    check(out.sel == 2 and out.row2 == 0, "handler theme: S onto row 2 always lands on Channel Transition, even with a stale column")


# ============================================================================
# SECTION: Old handlers beside the new ones
# ============================================================================


def case_games_list():
    total = len(CONSOLES)
    pages = mn.games_page_count(total)
    check(pages == 2, "games: 16 consoles make 2 pages")
    for kiosk in (False, True):
        for page in range(pages):
            t = mn.games_table(page, total, kiosk)
            label = "games p%d kiosk=%s" % (page, kiosk)
            structure(t, label, "console%02d" % (page * mn.GAMES_PAGE_SIZE))
            check(mn.games_table(page, total, kiosk) is t, label + ": table is cached")
            start = page * mn.GAMES_PAGE_SIZE
            end = min(start + mn.GAMES_PAGE_SIZE, total)
            name = lambda i: "console%02d" % i
            for i in range(start, end):
                col = (i - start) % 4
                check(go(t, name(i), mn.UP) == (mn.TAB_BAR if i - start < 4 else name(i - 4)), label + ": up from %d" % i)
                check(go(t, name(i), mn.DOWN) == (name(i + 4) if i + 4 < end else "page_button"), label + ": down from %d" % i)
                check(go(t, name(i), mn.LEFT) == (name(i - 1) if col else None), label + ": left from %d" % i)
                check(go(t, name(i), mn.RIGHT) == (name(i + 1) if col < 3 and i + 1 < end else None), label + ": right from %d" % i)
            check(go(t, "page_button", mn.UP) == name(end - 1), label + ": page button up defaults to the last console of the page")
            check(go(t, "page_button", mn.DOWN) == "all_consoles", label + ": page button down defaults to All Consoles")
            check(go(t, "page_button", mn.LEFT) is None and go(t, "page_button", mn.RIGHT) is None, label + ": nothing beside the page button")
            bottoms = [name(i) for i in range(start, end) if i + 4 >= end]
            for cell in bottoms:
                memory = {}
                go(t, cell, mn.DOWN, memory)
                check(go(t, "page_button", mn.UP, memory) == cell, label + ": way back up to " + cell)
            for cell in ("all_consoles", "all_settings", "sync_profile"):
                memory = {}
                check(go(t, cell, mn.UP, memory) == "page_button", label + ": %s up is the page button" % cell)
                back = go(t, "page_button", mn.DOWN, memory)
                check(back == ("all_consoles" if cell == "all_consoles" else "all_settings"), label + ": way back down to %s from %s" % (back, cell))
                check(go(t, "page_button", mn.UP, memory) == go(t, "page_button", mn.UP), label + ": the up memory is not disturbed by the down trip")
            check(go(t, "all_consoles", mn.DOWN) == "screensaver" and go(t, "screensaver", mn.DOWN) == "menu_music" and go(t, "menu_music", mn.DOWN) == mn.CLOSE, label + ": left column goes down to Close")
            check(go(t, "menu_music", mn.UP) == "screensaver" and go(t, "screensaver", mn.UP) == "all_consoles", label + ": left column goes up")
            check(go(t, "all_consoles", mn.RIGHT) == "all_settings" and go(t, "all_settings", mn.LEFT) == "all_consoles", label + ": All Consoles and All Settings are neighbours")
            check(go(t, "all_settings", mn.RIGHT) == "sync_profile" and go(t, "sync_profile", mn.LEFT) == "all_settings" and go(t, "sync_profile", mn.RIGHT) is None, label + ": the profile badge hangs off All Settings")
            check(go(t, "sync_profile", mn.DOWN) == "ss_timer" and go(t, "all_settings", mn.DOWN) == "ss_timer", label + ": both go down to the timer")
            check(go(t, "ss_timer", mn.UP) == "all_settings", label + ": timer up lands on All Settings, not the badge")
            check(go(t, "screensaver", mn.RIGHT) == "ss_timer" and go(t, "ss_timer", mn.LEFT) == "screensaver" and go(t, "ss_timer", mn.RIGHT) is None, label + ": Screen Saver and its timer")
            check(go(t, "all_consoles", mn.LEFT) is None and go(t, "screensaver", mn.LEFT) is None and go(t, "menu_music", mn.LEFT) is None, label + ": nothing left of the left column")
            check(go(t, mn.CLOSE, mn.LEFT) is None and go(t, mn.CLOSE, mn.RIGHT) is None and go(t, mn.CLOSE, mn.DOWN) is None, label + ": Close only goes up")
            if kiosk:
                check("set_music" not in t.cells, label + ": Set Music is not a cell in kiosk mode")
                check(go(t, "menu_music", mn.RIGHT) is None and go(t, "ss_timer", mn.DOWN) == mn.CLOSE, label + ": kiosk skips Set Music")
                check(go(t, mn.CLOSE, mn.UP) == "menu_music", label + ": kiosk Close up is Game Music")
            else:
                check(go(t, "menu_music", mn.RIGHT) == "set_music" and go(t, "set_music", mn.LEFT) == "menu_music", label + ": Game Music and Set Music")
                check(go(t, "ss_timer", mn.DOWN) == "set_music" and go(t, "set_music", mn.UP) == "ss_timer" and go(t, "set_music", mn.DOWN) == mn.CLOSE, label + ": Set Music sits under the timer")
                check(go(t, "set_music", mn.RIGHT) is None, label + ": nothing right of Set Music")
                check(go(t, mn.CLOSE, mn.UP) == "set_music", label + ": Close up defaults to Set Music")
                for source in ("set_music", "menu_music"):
                    memory = {}
                    go(t, source, mn.DOWN, memory)
                    check(go(t, mn.CLOSE, mn.UP, memory) == source, label + ": Close returns to " + source)
    for sel in list(range(total)) + list(range(95, 104)):
        check(mn.games_index(mn.games_cell(sel, total)) == sel, "games: selection %d round trip" % sel)
    check(mn.games_cell(16, total) is None and mn.games_cell(94, total) is None and mn.games_cell(None, total) is None, "games: unknown selection gives None")
    check(mn.games_page_of(13, total, 0) == 1 and mn.games_page_of(102, total, 1) == 1 and mn.games_page_of(102, total, 9) == 1, "games: page of a cell")


def case_games_handler():
    code = extract_branch(MAIN_SCRIPT, "GAMES_LIST")
    total = len(CONSOLES)
    for kiosk in (False, True):
        cfg = {"kiosk_mode_enabled": kiosk}
        for page in range(mn.games_page_count(total)):
            table = mn.games_table(page, total, kiosk)
            sels = list(range(page * mn.GAMES_PAGE_SIZE, min((page + 1) * mn.GAMES_PAGE_SIZE, total))) + list(range(95, 104))
            for sel in sels:
                if kiosk and sel == 96:
                    continue
                for key in "WASD":
                    out, _ = run_handler(code, "GAMES_LIST", None, sel, key, page=page, config=cfg)
                    want = expected(table, mn.games_cell(sel, total), key)
                    if want == mn.TAB_BAR:
                        ok = out.layer == "TAB_SELECTION"
                    elif want is None:
                        ok = out.layer == "GAMES_LIST" and out.sel == sel
                    else:
                        ok = out.layer == "GAMES_LIST" and out.sel == mn.games_index(want)
                    check(ok, "handler games kiosk=%s p%d sel %d %s -> %s %s (table says %s)" % (kiosk, page, sel, key, out.layer, out.sel, want))
    out, st = run_handler(code, "GAMES_LIST", None, 101, "W", page=0, config={})
    out2, _ = run_handler(code, "GAMES_LIST", None, 102, "S", page=0, config={}, memory=st["_nav_memory"]["GAMES_LIST"])
    check(out.sel == 102 and out2.sel == 101, "handler games: All Settings, W, S returns to All Settings")
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    check(text.count("menu_nav.games_table(") == 1, "main script: one hookup for the Games list")
    check("_gl_page_size = 4 * 3" not in text, "main script: the tab bar no longer carries its own copy of the Games page size")


# ============================================================================
# SECTION: Games sub-menu (N3b)
# ============================================================================

SUB_LAYOUTS = {
    "MENU": ("MENU", True, False, False, False),
    "DVD": ("DVD", True, False, False, False),
    "NES": ("NES", True, False, False, False),
    "NES kiosk": ("NES", True, True, False, False),
    "PSX": ("PSX", True, False, True, True),
    "PSX kiosk": ("PSX", True, True, True, True),
    "BIOS": ("A2600", True, False, False, True),
    "BIOS kiosk": ("A2600", True, True, False, True),
    "disabled": ("NES", False, False, False, False),
}


def sub_rows_for(console_key, on, kiosk, psx, bios):
    """Returns the rows get_games_sub_menu_rows gives for one console setup (the same lists as ui.py)."""
    if console_key == "MENU":
        return ["controller_map", "stick_deadzone", "scan_for_roms", "clear_roms", "scan_for_cores", "clear_cores", "menu_bg_select_image",
                "multitap_toggle", "scroll_style", "text_color", "game_screen_mode"]
    if not on:
        return []
    if console_key == "DVD":
        return ["splash_toggle", "splash_select_image", "splash_select_video", "dvd_captions_toggle", "dvd_resume_toggle",
                "dvd_button_mapping", "dvd_screen_mode", "dvd_audio_track"]
    rows = [] if kiosk else ["core_select"]
    rows += ["controller_map", "profile1", "profile2", "profile3"]
    if psx:
        rows.append("disc")
    rows += ["splash_toggle", "splash_select_image", "splash_select_video"]
    if bios:
        rows.append("add_bios")
    return rows + ["relocate_roms_folder", "clear_console_roms"]


def sub_full_rows(console_key, sub_rows):
    return sub_rows + ["close"] if console_key == "MENU" else ["status"] + sub_rows + ["close"]


def sub_setup(layout):
    console_key, on, kiosk, psx, bios = SUB_LAYOUTS[layout]
    sub_rows = sub_rows_for(console_key, on, kiosk, psx, bios)
    return console_key, sub_rows, sub_full_rows(console_key, sub_rows)


def sub_lasts(layout):
    return (False, True) if SUB_LAYOUTS[layout][1] and SUB_LAYOUTS[layout][0] != "MENU" else (False,)


EXPECTED_SUB = {
    "MENU": {
        "controller_map": "W:- S:stick_deadzone A:- D:menu_bg_select_image",
        "stick_deadzone": "W:controller_map S:game_screen_mode A:- D:multitap_toggle",
        "scan_for_roms": "W:scroll_style S:clear_roms A:scan_for_cores D:-",
        "clear_roms": "W:scan_for_roms S:close A:clear_cores D:-",
        "scan_for_cores": "W:game_screen_mode S:clear_cores A:- D:scan_for_roms",
        "clear_cores": "W:scan_for_cores S:close A:- D:clear_roms",
        "menu_bg_select_image": "W:- S:multitap_toggle A:controller_map D:-",
        "multitap_toggle": "W:menu_bg_select_image S:scroll_style A:stick_deadzone D:-",
        "scroll_style": "W:multitap_toggle S:scan_for_roms A:game_screen_mode D:text_color",
        "text_color": "W:multitap_toggle S:scan_for_roms A:scroll_style D:-",
        "game_screen_mode": "W:stick_deadzone S:scan_for_cores A:- D:scroll_style",
        "close": "W:clear_cores S:- A:clear_cores D:-",
    },
    "DVD": {
        "status": "W:- S:dvd_button_mapping A:- D:splash_toggle",
        "splash_toggle": "W:status S:splash_select_image A:status D:-",
        "splash_select_image": "W:splash_toggle S:dvd_captions_toggle A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:dvd_captions_toggle A:dvd_button_mapping D:splash_select_image",
        "dvd_captions_toggle": "W:splash_select_video S:dvd_resume_toggle A:dvd_screen_mode D:-",
        "dvd_resume_toggle": "W:dvd_captions_toggle S:close A:dvd_audio_track D:-",
        "dvd_button_mapping": "W:status S:dvd_screen_mode A:- D:splash_select_video",
        "dvd_screen_mode": "W:dvd_button_mapping S:dvd_audio_track A:- D:dvd_captions_toggle",
        "dvd_audio_track": "W:dvd_screen_mode S:close A:- D:dvd_resume_toggle",
        "close": "W:dvd_resume_toggle S:- A:- D:-",
    },
    "NES": {
        "status": "W:- S:core_select A:- D:splash_toggle",
        "core_select": "W:status S:controller_map A:- D:splash_select_video",
        "controller_map": "W:core_select S:profile1 A:- D:splash_select_video",
        "profile1": "W:controller_map S:profile2 A:- D:clear_console_roms",
        "profile2": "W:profile1 S:profile3 A:- D:clear_console_roms",
        "profile3": "W:profile2 S:close A:- D:close",
        "splash_toggle": "W:status S:splash_select_image A:core_select D:-",
        "splash_select_image": "W:splash_toggle S:relocate_roms_folder A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:relocate_roms_folder A:core_select D:splash_select_image",
        "relocate_roms_folder": "W:splash_select_video S:clear_console_roms A:core_select D:-",
        "clear_console_roms": "W:relocate_roms_folder S:close A:core_select D:-",
        "close": "W:clear_console_roms S:- A:profile3 D:-",
    },
    "NES kiosk": {
        "status": "W:- S:controller_map A:- D:splash_toggle",
        "controller_map": "W:status S:profile1 A:- D:splash_toggle",
        "profile1": "W:controller_map S:profile2 A:- D:clear_console_roms",
        "profile2": "W:profile1 S:profile3 A:- D:clear_console_roms",
        "profile3": "W:profile2 S:close A:- D:close",
        "splash_toggle": "W:status S:splash_select_image A:controller_map D:-",
        "splash_select_image": "W:splash_toggle S:relocate_roms_folder A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:relocate_roms_folder A:controller_map D:splash_select_image",
        "relocate_roms_folder": "W:splash_select_video S:clear_console_roms A:controller_map D:-",
        "clear_console_roms": "W:relocate_roms_folder S:close A:controller_map D:-",
        "close": "W:clear_console_roms S:- A:profile3 D:-",
    },
    "PSX": {
        "status": "W:- S:core_select A:- D:splash_toggle",
        "core_select": "W:status S:controller_map A:- D:splash_select_video",
        "controller_map": "W:core_select S:profile1 A:- D:splash_select_video",
        "profile1": "W:controller_map S:profile2 A:- D:clear_console_roms",
        "profile2": "W:profile1 S:profile3 A:- D:clear_console_roms",
        "profile3": "W:profile2 S:disc A:- D:close",
        "disc": "W:profile3 S:close A:- D:close",
        "splash_toggle": "W:status S:splash_select_image A:core_select D:-",
        "splash_select_image": "W:splash_toggle S:add_bios A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:add_bios A:core_select D:splash_select_image",
        "add_bios": "W:splash_select_video S:relocate_roms_folder A:core_select D:-",
        "relocate_roms_folder": "W:add_bios S:clear_console_roms A:core_select D:-",
        "clear_console_roms": "W:relocate_roms_folder S:close A:core_select D:-",
        "close": "W:clear_console_roms S:- A:disc D:-",
    },
    "PSX kiosk": {
        "status": "W:- S:controller_map A:- D:splash_toggle",
        "controller_map": "W:status S:profile1 A:- D:splash_toggle",
        "profile1": "W:controller_map S:profile2 A:- D:clear_console_roms",
        "profile2": "W:profile1 S:profile3 A:- D:clear_console_roms",
        "profile3": "W:profile2 S:disc A:- D:close",
        "disc": "W:profile3 S:close A:- D:close",
        "splash_toggle": "W:status S:splash_select_image A:controller_map D:-",
        "splash_select_image": "W:splash_toggle S:add_bios A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:add_bios A:controller_map D:splash_select_image",
        "add_bios": "W:splash_select_video S:relocate_roms_folder A:controller_map D:-",
        "relocate_roms_folder": "W:add_bios S:clear_console_roms A:controller_map D:-",
        "clear_console_roms": "W:relocate_roms_folder S:close A:controller_map D:-",
        "close": "W:clear_console_roms S:- A:disc D:-",
    },
    "BIOS": {
        "status": "W:- S:core_select A:- D:splash_toggle",
        "core_select": "W:status S:controller_map A:- D:splash_select_video",
        "controller_map": "W:core_select S:profile1 A:- D:splash_select_video",
        "profile1": "W:controller_map S:profile2 A:- D:clear_console_roms",
        "profile2": "W:profile1 S:profile3 A:- D:clear_console_roms",
        "profile3": "W:profile2 S:close A:- D:close",
        "splash_toggle": "W:status S:splash_select_image A:core_select D:-",
        "splash_select_image": "W:splash_toggle S:add_bios A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:add_bios A:core_select D:splash_select_image",
        "add_bios": "W:splash_select_video S:relocate_roms_folder A:core_select D:-",
        "relocate_roms_folder": "W:add_bios S:clear_console_roms A:core_select D:-",
        "clear_console_roms": "W:relocate_roms_folder S:close A:core_select D:-",
        "close": "W:clear_console_roms S:- A:profile3 D:-",
    },
    "BIOS kiosk": {
        "status": "W:- S:controller_map A:- D:splash_toggle",
        "controller_map": "W:status S:profile1 A:- D:splash_toggle",
        "profile1": "W:controller_map S:profile2 A:- D:clear_console_roms",
        "profile2": "W:profile1 S:profile3 A:- D:clear_console_roms",
        "profile3": "W:profile2 S:close A:- D:close",
        "splash_toggle": "W:status S:splash_select_image A:controller_map D:-",
        "splash_select_image": "W:splash_toggle S:add_bios A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:add_bios A:controller_map D:splash_select_image",
        "add_bios": "W:splash_select_video S:relocate_roms_folder A:controller_map D:-",
        "relocate_roms_folder": "W:add_bios S:clear_console_roms A:controller_map D:-",
        "clear_console_roms": "W:relocate_roms_folder S:close A:controller_map D:-",
        "close": "W:clear_console_roms S:- A:profile3 D:-",
    },
    "disabled": {
        "status": "W:- S:close A:- D:-",
        "close": "W:status S:- A:- D:-",
    },
}

def parse_moves(text):
    return {part.split(":")[0]: (None if part.split(":")[1] == "-" else part.split(":")[1]) for part in text.split()}


def case_games_sub_rows_match_ui():
    from game_deck import BIOS_REQUIRED_CONSOLES
    compared = []
    for layout, (console_key, on, kiosk, psx, bios) in SUB_LAYOUTS.items():
        if console_key not in ("MENU", "DVD") and bios != (console_key in BIOS_REQUIRED_CONSOLES):
            continue
        compared.append(layout)
        real = ui.get_games_sub_menu_rows(FakeDbConfig({"kiosk_mode_enabled": kiosk}), {}, console_key, on)
        check(sub_rows_for(console_key, on, kiosk, psx, bios) == real,
              "games sub %s: the test's rows %s differ from ui.get_games_sub_menu_rows %s" % (layout, sub_rows_for(console_key, on, kiosk, psx, bios), real))
    check(sorted(compared) == ["DVD", "MENU", "NES", "NES kiosk", "PSX", "PSX kiosk", "disabled"], "the layouts compared against ui.py are %s" % compared)
    for gone in ("game_aspect_ratio", "dvd_aspect_ratio"):
        check(gone not in ui.get_games_sub_menu_rows(None, {}, "MENU", True) + ui.get_games_sub_menu_rows(None, {}, "DVD", True),
              "%s is not a Games sub-menu row" % gone)


def case_games_sub_tables():
    for layout in SUB_LAYOUTS:
        console_key, sub_rows, full = sub_setup(layout)
        for last in sub_lasts(layout):
            label = "games sub %s%s" % (layout, " (last console)" if last else "")
            t = mn.games_sub_table(full, last)
            structure(t, label, full[0])
            check(mn.games_sub_table(list(full), last) is t, label + ": table is cached")
            check(set(t.cells) == set(full), label + ": the cells are exactly the rows %s" % sorted(set(t.cells) ^ set(full)))
            for index, row in enumerate(full):
                check(mn.games_sub_cell(full, index) == row and mn.games_sub_index(full, row) == index, label + ": selection %d round trip" % index)
            check(mn.games_sub_cell(full, len(full) + 3) == mn.CLOSE and mn.games_sub_cell(full, -1) == mn.CLOSE and mn.games_sub_cell(full, None) == mn.CLOSE,
                  label + ": a selection outside the rows is Close")
            want = {cell: dict(parse_moves(text)) for cell, text in EXPECTED_SUB[layout].items()}
            if last:
                for cell in want:
                    want[cell] = {k: (None if v == "status" else v) for k, v in want[cell].items()}
            for cell in full:
                got = {k: go(t, cell, DIRS[k]) for k in "WSAD"}
                check(got == want[cell], "%s: neighbours of %s are %s, table says %s" % (label, cell, want[cell], got))
            if last and layout != "MENU":
                led_to_status = [(cell, k) for cell in full for k in "WSAD" if go(t, cell, DIRS[k]) == "status"]
                check(not led_to_status, label + ": no move leads to the greyed Status row %s" % led_to_status)


def case_games_sub_way_back():
    def walk(table, start, moves, memory=None):
        memory = {} if memory is None else memory
        cell = start
        for key in moves:
            nxt = table.step(cell, DIRS[key], memory)
            cell = nxt if nxt is not None else cell
        return cell

    nes = mn.games_sub_table(sub_setup("NES")[2], False)
    kiosk = mn.games_sub_table(sub_setup("NES kiosk")[2], False)
    psx = mn.games_sub_table(sub_setup("PSX")[2], False)
    dvd = mn.games_sub_table(sub_setup("DVD")[2], False)
    menu = mn.games_sub_table(sub_setup("MENU")[2], False)
    off = mn.games_sub_table(sub_setup("disabled")[2], False)

    for button in ("splash_select_video", "splash_select_image"):
        check(walk(nes, button, "WS") == button, "games sub NES: %s, W, S returns to %s" % (button, button))
        check(walk(nes, button, "SW") == button, "games sub NES: %s, S, W returns to %s" % (button, button))
        check(walk(dvd, button, "WS") == button, "games sub DVD: %s, W, S returns to %s" % (button, button))
        check(walk(dvd, button, "SW") == button, "games sub DVD: %s, S, W returns to %s" % (button, button))
    check(walk(nes, "splash_toggle", "S") == "splash_select_image", "games sub NES: the toggle goes down to Image with no memory")
    check(walk(nes, "relocate_roms_folder", "W") == "splash_select_video", "games sub NES: the row under the buttons goes up to Video with no memory")
    check(walk(nes, "splash_toggle", "SD") == "splash_select_image" and walk(nes, "splash_select_image", "A") == "splash_select_video", "games sub NES: Video and Image are neighbours")

    check(walk(nes, "core_select", "WS") == "core_select" and walk(nes, "splash_toggle", "WS") == "splash_toggle", "games sub NES: Status way back to the column you left")
    check(walk(dvd, "dvd_button_mapping", "WS") == "dvd_button_mapping" and walk(dvd, "splash_toggle", "WS") == "splash_toggle", "games sub DVD: Status way back to the column you left")
    check(walk(nes, "status", "S") == "core_select" and walk(kiosk, "status", "S") == "controller_map", "games sub: Status down with no memory goes to the first left row")

    check(walk(nes, "core_select", "DA") == "core_select" and walk(nes, "controller_map", "DA") == "controller_map", "games sub NES: Video back to the left row you came from")
    check(walk(nes, "splash_select_video", "A") == "core_select", "games sub NES: Video left with no memory goes to the first left row")
    check(walk(kiosk, "controller_map", "DA") == "controller_map", "games sub NES kiosk: the only left row goes to the toggle and comes back")
    check(walk(kiosk, "status", "DA") == "status" and walk(nes, "status", "DA") == "status", "games sub: Status, D, A returns to Status")
    check(walk(nes, "splash_toggle", "A") == "core_select" and walk(dvd, "splash_toggle", "A") == "status", "games sub: toggle left with no memory (console: first left row, DVD: Status)")

    check(walk(nes, "clear_console_roms", "SW") == "clear_console_roms", "games sub NES: Close way back to Clear Roms")
    check(walk(nes, "profile3", "SW") == "profile3", "games sub NES: Close way back to Profile 3")
    check(walk(nes, "profile3", "DA") == "profile3" and walk(nes, "profile3", "DW") == "clear_console_roms", "games sub NES: Close left returns to Profile 3, Close up is untouched by a sideways trip")
    check(walk(psx, "disc", "SW") == "disc" and walk(psx, "disc", "DA") == "disc", "games sub PSX: Close way back to Disc, up and left")
    check(walk(psx, "profile3", "DA") == "profile3" and walk(psx, "profile3", "DW") == "clear_console_roms", "games sub PSX: Profile 3 right then left returns to Profile 3, Close up still goes to Clear Roms")
    check(walk(psx, "clear_console_roms", "SA") == "disc", "games sub PSX: Close left with no sideways memory goes to Disc")
    check(walk(dvd, "dvd_resume_toggle", "SW") == "dvd_resume_toggle" and walk(dvd, "dvd_audio_track", "SW") == "dvd_audio_track",
          "games sub DVD: Close way back to Resume or Change Audio, from S")
    check(walk(dvd, "dvd_audio_track", "D") == "dvd_resume_toggle" and walk(dvd, "dvd_audio_track", "DA") == "dvd_audio_track"
          and walk(dvd, "dvd_resume_toggle", "AD") == "dvd_resume_toggle",
          "games sub DVD: Change Audio and Resume share a row, so each goes sideways to the other and back")
    check(walk(dvd, "dvd_screen_mode", "D") == "dvd_captions_toggle" and walk(dvd, "dvd_captions_toggle", "A") == "dvd_screen_mode",
          "games sub DVD: Screen Mode and Captions share a row")
    check(walk(dvd, "close", "W") == "dvd_resume_toggle", "games sub DVD: Close up with no memory goes to Resume")
    check(walk(menu, "clear_roms", "SW") == "clear_roms" and walk(menu, "clear_cores", "SW") == "clear_cores" and walk(menu, "close", "W") == "clear_cores", "games sub MENU: Close way back, default Clear Cores")
    check(walk(menu, "close", "A") == "clear_cores" and walk(menu, "clear_roms", "SA") == "clear_cores", "games sub MENU: Close left is always Clear Cores")
    check(walk(menu, "scroll_style", "DA") == "scroll_style" and walk(menu, "text_color", "W") == "multitap_toggle" and walk(menu, "text_color", "S") == "scan_for_roms",
          "games sub MENU: the colour swatch hangs off Scroll Style and moves vertically like it")
    check(walk(off, "status", "S") == "close" and walk(off, "close", "W") == "status", "games sub disabled console: Status and Close are neighbours")


def run_games_sub(code, layout, sel, key, last, state=None):
    console_key, sub_rows, full = sub_setup(layout)
    state = state if state is not None else {"menu_layer": "GAMES_SUB_MENU", "games_sub_row": sel, "games_selected_console": console_key, "show_menu": True}
    state["games_sub_row"] = sel
    state["menu_layer"] = "GAMES_SUB_MENU"
    db = types.SimpleNamespace(config={"consoles_enabled": {console_key: bool(sub_rows)}}, save_settings=lambda: None)
    env = {
        "pygame": pygame, "menu_nav": mn, "app_state": state, "db": db, "event": types.SimpleNamespace(key=n2_KEYS[key]),
        "get_games_sub_menu_rows": lambda _db, _state, _console, _on: list(sub_rows),
        "_count_active_consoles": lambda _db: 1 if last else 5, "game_deck": None, "print": lambda *a, **k: None,
    }
    exec(code, env)
    return state["games_sub_row"], state["menu_layer"], state


def case_games_sub_handler():
    code = extract_branch(MAIN_SCRIPT, "GAMES_SUB_MENU")
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    check(text.count("menu_nav.games_sub_table(") == 1, "main script: one hookup for the Games sub-menu")
    for layout in SUB_LAYOUTS:
        console_key, sub_rows, full = sub_setup(layout)
        for last in sub_lasts(layout):
            table = mn.games_sub_table(full, last)
            for sel, cell in enumerate(full):
                for key in "WASD":
                    to, layer, _ = run_games_sub(code, layout, sel, key, last)
                    want = expected(table, cell, key)
                    ok = layer == "GAMES_SUB_MENU" and to == (sel if want is None else mn.games_sub_index(full, want))
                    check(ok, "handler games sub %s last=%s sel %d (%s) %s -> %s (table says %s)" % (layout, last, sel, cell, key, to, want))
    console_key, sub_rows, full = sub_setup("NES")
    sel = full.index("splash_select_video")
    to, _, state = run_games_sub(code, "NES", sel, "W", False)
    check(full[to] == "splash_toggle", "handler games sub: Video up goes to the toggle")
    to, _, state = run_games_sub(code, "NES", to, "S", False, state)
    check(full[to] == "splash_select_video", "handler games sub: the toggle down returns to Video through the memory the handler wrote")
    check("GAMES_SUB_MENU:NES" in state["_nav_memory"], "handler games sub: the memory is kept per console")
    other_to, _, other_state = run_games_sub(code, "BIOS", 1, "S", False)
    check("GAMES_SUB_MENU:A2600" in other_state["_nav_memory"] and "GAMES_SUB_MENU:NES" not in other_state["_nav_memory"], "handler games sub: another console starts with no memory")
    to, _, _ = run_games_sub(code, "NES", full.index("splash_toggle"), "S", False, {"menu_layer": "GAMES_SUB_MENU", "games_sub_row": 0, "games_selected_console": "NES", "show_menu": True,
                                                                              "_nav_memory": {"GAMES_SUB_MENU:A2600": {"splash_toggle": "splash_select_video"}}})
    check(full[to] == "splash_select_image", "handler games sub: memory of another console is not used")
    for layout in ("MENU", "NES"):
        console_key, sub_rows, full = sub_setup(layout)
        for key in "WASD":
            to, layer, _ = run_games_sub(code, layout, len(full) + 4, key, False)
            check(layer == "GAMES_SUB_MENU", "handler games sub %s: a stale selection past the rows still moves as Close (%s)" % (layout, key))


def case_games_sub_sequences():
    """Runs the real handler over long random key sequences and checks every step against the table with the memory the handler itself has written."""
    import random
    code = extract_branch(MAIN_SCRIPT, "GAMES_SUB_MENU")
    random.seed(10102026)
    steps = 0
    wrong = []
    for layout in SUB_LAYOUTS:
        console_key, sub_rows, full = sub_setup(layout)
        for last in sub_lasts(layout):
            table = mn.games_sub_table(full, last)
            for _ in range(300):
                state = None
                cur = random.randrange(len(full))
                for _step in range(60):
                    key = random.choice("WASD")
                    memory = dict(((state or {}).get("_nav_memory", {}).get("GAMES_SUB_MENU:" + console_key, {})))
                    want = table.step(mn.games_sub_cell(full, cur), DIRS[key], memory)
                    to, layer, state = run_games_sub(code, layout, cur, key, last, state)
                    steps += 1
                    if to != (cur if want is None else mn.games_sub_index(full, want)) or layer != "GAMES_SUB_MENU":
                        wrong.append((layout, last, full[cur], key, full[to], want))
                    cur = to
    check(not wrong, "handler games sub: %d random steps all follow the table with the handler's own memory %s" % (steps, wrong[:3]))


# ============================================================================
# SECTION: DVD button map, Save Profile and controller map (N3c)
# ============================================================================

n2_ALL_KEYS = {"W": pygame.K_w, "A": pygame.K_a, "S": pygame.K_s, "D": pygame.K_d,
            "UP": pygame.K_UP, "LEFT": pygame.K_LEFT, "DOWN": pygame.K_DOWN, "RIGHT": pygame.K_RIGHT}
ALL_DIRS = {"W": mn.UP, "S": mn.DOWN, "A": mn.LEFT, "D": mn.RIGHT, "UP": mn.UP, "DOWN": mn.DOWN, "LEFT": mn.LEFT, "RIGHT": mn.RIGHT}


def _deck_constants():
    """Reads the console groups straight out of game_deck.py."""
    import ast
    wanted = {"HANDHELD_CONSOLES", "BORDER_CONSOLES_HOME", "BORDER_CONSOLES_HANDHELD", "SCREEN_SIZE_OPTIONS"}
    found = {}
    with open(os.path.join(ROOT, "game_deck.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted:
            found[node.targets[0].id] = ast.literal_eval(node.value)
    return found


DECK = _deck_constants()
HANDHELD = set(DECK["HANDHELD_CONSOLES"])
BORDER_HOME = set(DECK["BORDER_CONSOLES_HOME"])
BORDER_ANY = BORDER_HOME | set(DECK["BORDER_CONSOLES_HANDHELD"])
_stub = GAME_DECK_STUB
for _name, _value in (("HANDHELD_CONSOLES", HANDHELD), ("BORDER_CONSOLES_HOME", BORDER_HOME), ("SCREEN_SIZE_OPTIONS", DECK["SCREEN_SIZE_OPTIONS"])):
    setattr(_stub, _name, _value)
_stub.border_available_for_console = lambda console: console in BORDER_ANY

SAVE_CONSOLES = [c for c in CONSOLES if c not in ("MENU", "DVD")]


def save_layout(console):
    handheld = console in HANDHELD
    border = console in BORDER_ANY
    return handheld, border, border and console in BORDER_HOME


def _walk(table, start, moves, memory=None):
    memory = {} if memory is None else memory
    cell = start
    for move in moves:
        to = table.step(cell, DIRS[move], memory)
        cell = cell if to is None else to
    return cell


def test_n3c_directions():
    for name, key in n2_ALL_KEYS.items():
        check(mn.key_to_direction(key, arrows=True) == ALL_DIRS[name], "key_to_direction with arrows: %s" % name)
        check(mn.key_to_direction(key) == (ALL_DIRS[name] if name in "WASD" else None), "key_to_direction without arrows: %s" % name)
    check(mn.key_to_direction(pygame.K_RETURN, arrows=True) is None and mn.key_to_direction(pygame.K_ESCAPE) is None, "key_to_direction: other keys give None")


# ============================================================================
# SECTION: DVD button map
# ============================================================================

def test_dvd_map_tables():
    t = mn.dvd_map_table()
    structure(t, "dvd map", "map_buttons")
    check(mn.dvd_map_table() is t, "dvd map: table is cached")
    check(set(t.cells) == {"map_buttons", "close"}, "dvd map: the cells are exactly the two buttons")
    want = {"map_buttons": {"W": None, "S": "close", "A": None, "D": "close"}, "close": {"W": "map_buttons", "S": None, "A": "map_buttons", "D": None}}
    for cell in want:
        got = {k: go(t, cell, DIRS[k]) for k in "WSAD"}
        check(got == want[cell], "dvd map: neighbours of %s are %s, table says %s" % (cell, want[cell], got))
    check(mn.dvd_map_cell(0) == "map_buttons" and mn.dvd_map_cell(1) == "close" and mn.dvd_map_cell(7) == "close" and mn.dvd_map_cell(None) == "close", "dvd map: cursor to cell")
    check(mn.dvd_map_index("map_buttons") == 0 and mn.dvd_map_index("close") == 1, "dvd map: cell to cursor")


def run_dvd_map(code, cursor, key, capturing=False):
    state = {"menu_layer": "DVD_BTN_MAP", "dvd_map_cursor": cursor, "dvd_map_capturing": capturing, "show_menu": True}
    db = types.SimpleNamespace(config={}, save_settings=lambda: None)
    env = {"pygame": pygame, "menu_nav": mn, "app_state": state, "db": db, "event": types.SimpleNamespace(key=n2_ALL_KEYS.get(key, key)),
           "get_games_sub_menu_rows": lambda _db, _state, _console, _on: sub_rows_for("DVD", True, False, False, False),
           "CANONICAL_REMOTE_KEYS": {}, "DVD_MAP_ACTIONS": [("play", "PLAY"), ("stop", "STOP"), ("next", "NEXT")],
           "set_dvd_binding": lambda *a, **k: None, "_REMAP_CAPTURE_DEDUP_MS": 400, "print": lambda *a, **k: None}
    exec(code, env)
    return state


def test_dvd_map_handler():
    code = extract_branch(MAIN_SCRIPT, "DVD_BTN_MAP")
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    check(text.count("menu_nav.dvd_map_table()") == 1, "main script: one hookup for the DVD button map")
    pygame.display.init()
    table = mn.dvd_map_table()
    for cursor in (0, 1):
        cell = mn.dvd_map_cell(cursor)
        for key in n2_ALL_KEYS:
            state = run_dvd_map(code, cursor, key)
            want = table.step(cell, ALL_DIRS[key], {})
            got = state["dvd_map_cursor"]
            check(got == (cursor if want is None else mn.dvd_map_index(want)) and state["menu_layer"] == "DVD_BTN_MAP" and not state["dvd_map_capturing"],
                  "handler dvd map cursor %d %s -> %s (table says %s)" % (cursor, key, got, want))
    state = run_dvd_map(code, 0, pygame.K_RETURN)
    check(state["dvd_map_capturing"] is True and state["dvd_map_capture_idx"] == 0 and state["menu_layer"] == "DVD_BTN_MAP", "handler dvd map: Enter on Map Buttons starts the capture")
    state = run_dvd_map(code, 1, pygame.K_RETURN)
    check(state["menu_layer"] == "GAMES_SUB_MENU" and state["dvd_map_capturing"] is False and state["games_sub_row"] == 1 + sub_rows_for("DVD", True, False, False, False).index("dvd_button_mapping"), "handler dvd map: Enter on Close returns to the Games sub-menu on the DVD mapping row")
    state = run_dvd_map(code, 0, pygame.K_ESCAPE)
    check(state["menu_layer"] == "GAMES_SUB_MENU", "handler dvd map: Escape returns to the Games sub-menu")
    state = run_dvd_map(code, 0, pygame.K_x)
    check(state["dvd_map_cursor"] == 0 and state["menu_layer"] == "DVD_BTN_MAP", "handler dvd map: another key changes nothing")
    for key in n2_ALL_KEYS:
        state = run_dvd_map(code, 0, key, capturing=True)
        check(state["dvd_map_cursor"] == 0 and state["dvd_map_capture_idx"] == 1 and state["dvd_map_capturing"] is True,
              "handler dvd map: while capturing a direction key is captured as the binding and never moves the cursor (%s)" % key)


# ============================================================================
# SECTION: Save Profile
# ============================================================================

SAVE_LAYOUTS = {"home (with bezel style)": (True, True, True), "handheld (no bezel style)": (True, True, False)}

EXPECTED_SAVE_STYLE = {
    "active": "W:- S:save A:- D:-",
    "save": "W:active S:load A:- D:screen_size",
    "load": "W:save S:auto_save A:- D:tv_bezel",
    "auto_save": "W:load S:auto_load A:- D:bezel_style",
    "auto_load": "W:auto_save S:close A:- D:-",
    "screen_size": "W:active S:tv_bezel A:save D:-",
    "tv_bezel": "W:screen_size S:bezel_style A:load D:-",
    "bezel_style": "W:tv_bezel S:close A:auto_save D:-",
    "close": "W:bezel_style S:- A:- D:-",
}
EXPECTED_SAVE_PLAIN = dict(EXPECTED_SAVE_STYLE)
EXPECTED_SAVE_PLAIN.update({
    "auto_save": "W:load S:auto_load A:- D:-",
    "tv_bezel": "W:screen_size S:close A:load D:-",
    "close": "W:auto_load S:- A:- D:-",
})
del EXPECTED_SAVE_PLAIN["bezel_style"]


def test_save_profile_tables():
    check(mn.save_profile_rows(True, True, True) == ["active", "save", "load", "auto_save", "auto_load", "screen_size", "tv_bezel", "bezel_style", "close"], "save profile rows: home console")
    check(mn.save_profile_rows(True, True, False) == ["active", "save", "load", "auto_save", "auto_load", "screen_size", "tv_bezel", "close"], "save profile rows: handheld with bezel")
    check(mn.save_profile_rows(True, False, False) == ["active", "save", "load", "auto_save", "auto_load", "screen_size", "close"], "save profile rows: screen size only")
    check(mn.save_profile_rows(False, True, True)[-3:] == ["tv_bezel", "bezel_style", "close"], "save profile rows: bezel and style without screen size")
    check(mn.save_profile_rows(False, False, False) == ["active", "save", "load", "auto_save", "auto_load", "close"], "save profile rows: no right column")
    check(mn.save_profile_rows(True, False, True) == mn.save_profile_rows(True, False, False), "save profile rows: a style row never appears without the bezel row")
    for console in SAVE_CONSOLES:
        handheld, border, style = save_layout(console)
        check(handheld and border, "every console that has Save Profile is a handheld-group console with a bezel row (%s)" % console)
    check({save_layout(c) for c in SAVE_CONSOLES} == set(SAVE_LAYOUTS.values()), "the real consoles use exactly the two layouts tested by hand")
    layouts = {"home": (True, True, True), "handheld": (True, True, False), "screen only": (True, False, False),
               "bezel only": (False, True, False), "bezel and style": (False, True, True), "none": (False, False, False)}
    for label, layout in layouts.items():
        rows = mn.save_profile_rows(*layout)
        t = mn.save_profile_table(rows)
        structure(t, "save profile " + label, "active")
        check(mn.save_profile_table(list(rows)) is t, "save profile %s: table is cached" % label)
        check(set(t.cells) == set(rows), "save profile %s: the cells are exactly the rows" % label)
        for index, row in enumerate(rows):
            check(mn.save_profile_cell(rows, index) == row and mn.save_profile_index(rows, row) == index, "save profile %s: selection %d round trip" % (label, index))
        check(mn.save_profile_cell(rows, len(rows) + 2) == "close" and mn.save_profile_cell(rows, -1) == "close" and mn.save_profile_cell(rows, None) == "close", "save profile %s: a selection outside the rows is Close" % label)
        for cell in rows:
            check(go(t, cell, mn.UP) != "@tab_bar" and go(t, cell, mn.UP) in (None, *rows), "save profile %s: %s up never leaves the layer" % (label, cell))
    for label, layout in SAVE_LAYOUTS.items():
        rows = mn.save_profile_rows(*layout)
        t = mn.save_profile_table(rows)
        want = {cell: parse_moves(text) for cell, text in (EXPECTED_SAVE_STYLE if layout[2] else EXPECTED_SAVE_PLAIN).items()}
        check(set(want) == set(rows), "save profile %s: the hand-written neighbour list covers every row" % label)
        for cell in rows:
            got = {k: go(t, cell, DIRS[k]) for k in "WSAD"}
            check(got == want[cell], "save profile %s: neighbours of %s are %s, table says %s" % (label, cell, want[cell], got))


def test_save_profile_way_back():
    for label, layout in SAVE_LAYOUTS.items():
        rows = mn.save_profile_rows(*layout)
        t = mn.save_profile_table(rows)
        last_right = "bezel_style" if layout[2] else "tv_bezel"
        check(_walk(t, "active", "S") == "save", "save profile %s: Active down with no memory goes to Save Now" % label)
        check(_walk(t, "save", "WS") == "save" and _walk(t, "screen_size", "WS") == "screen_size", "save profile %s: Active down returns to the column you left" % label)
        check(_walk(t, "screen_size", "WSS") == "tv_bezel", "save profile %s: Screen Size up, then down twice, ends on TV Bezel" % label)
        check(_walk(t, "auto_load", "SW") == "auto_load", "save profile %s: Close up returns to Auto-Load" % label)
        check(_walk(t, last_right, "SW") == last_right, "save profile %s: Close up returns to %s" % (label, last_right))
    t = mn.save_profile_table(mn.save_profile_rows(*SAVE_LAYOUTS["handheld (no bezel style)"]))
    check(_walk(t, "close", "W") == "auto_load", "save profile handheld: Close up with no memory goes to Auto-Load (as before)")
    t = mn.save_profile_table(mn.save_profile_rows(*SAVE_LAYOUTS["home (with bezel style)"]))
    check(_walk(t, "close", "W") == "bezel_style", "save profile home: Close up with no memory goes to Bezel Style (as before)")
    mem = {}
    _walk(t, "auto_load", "S", mem)
    check(_walk(t, "close", "W", mem) == "auto_load", "save profile: way back through the memory dict")
    mem = {}
    _walk(t, "screen_size", "W", mem)
    check(_walk(t, "active", "S", mem) == "screen_size", "save profile: Active down way back through the memory dict")
    mem = {}
    _walk(t, "save", "WS", mem)
    _walk(t, "bezel_style", "S", mem)
    check(_walk(t, "active", "S", mem) == "save" and _walk(t, "close", "W", mem) == "bezel_style", "save profile: the Active and Close memories are separate")
    check(_walk(t, "screen_size", "D") == "screen_size" and _walk(t, "save", "A") == "save", "save profile: left of the left column and right of the right column stay put")


def run_save_profile(code, console, sel, key, state=None, game_deck_obj=None, config=None):
    handheld, border, style = save_layout(console)
    state = state if state is not None else {"menu_layer": "GAMES_SAVE_PROFILE", "games_selected_console": console, "games_profile_num": 1, "show_menu": True}
    state["games_profile_row"] = sel
    state["menu_layer"] = "GAMES_SAVE_PROFILE"
    db = types.SimpleNamespace(config=config if config is not None else {"console_profiles": {}}, save_settings=lambda: None)
    env = {"pygame": pygame, "menu_nav": mn, "app_state": state, "db": db, "event": types.SimpleNamespace(key=n2_ALL_KEYS.get(key, key)),
           "game_deck": game_deck_obj, "now_ticks": 0, "print": lambda *a, **k: None, "log": types.SimpleNamespace(info=lambda *a, **k: None),
           "_sync_profile_setting_across_consoles": lambda *a, **k: None, "_set_console_bezel_enabled": lambda *a, **k: None, "_set_console_bezel_style": lambda *a, **k: None}
    exec(code, env)
    return state["games_profile_row"], state["menu_layer"], state


def test_save_profile_handler():
    code = extract_branch(MAIN_SCRIPT, "GAMES_SAVE_PROFILE")
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    check(text.count("menu_nav.save_profile_table(") == 1, "main script: one hookup for Save Profile")
    pygame.display.init()
    for console in SAVE_CONSOLES:
        rows = mn.save_profile_rows(*save_layout(console))
        table = mn.save_profile_table(rows)
        for sel, cell in enumerate(rows):
            for key in "WASD":
                to, layer, _ = run_save_profile(code, console, sel, key)
                want = expected(table, cell, key)
                check(layer == "GAMES_SAVE_PROFILE" and to == (sel if want is None else mn.save_profile_index(rows, want)),
                      "handler save profile %s sel %d (%s) %s -> %s (table says %s)" % (console, sel, cell, key, to, want))
    for console in ("NES", "GB"):
        rows = mn.save_profile_rows(*save_layout(console))
        for key in "WASD":
            to, layer, _ = run_save_profile(code, console, len(rows) + 4, key)
            check(layer == "GAMES_SAVE_PROFILE", "handler save profile %s: a stale selection past the rows still moves as Close (%s)" % (console, key))
    rows = mn.save_profile_rows(*save_layout("NES"))
    to, _, state = run_save_profile(code, "NES", rows.index("screen_size"), "W")
    check(rows[to] == "active", "handler save profile: Screen Size up goes to the banner")
    to, _, state = run_save_profile(code, "NES", to, "S", state)
    check(rows[to] == "screen_size", "handler save profile: the banner down returns to Screen Size through the memory the handler wrote")
    check("GAMES_SAVE_PROFILE:NES" in state["_nav_memory"], "handler save profile: the memory is kept per console")
    other = run_save_profile(code, "GB", rows.index("auto_load"), "S")[2]
    check("GAMES_SAVE_PROFILE:GB" in other["_nav_memory"] and "GAMES_SAVE_PROFILE:NES" not in other["_nav_memory"], "handler save profile: another console starts with no memory")
    to, _, state = run_save_profile(code, "NES", rows.index("bezel_style"), "S")
    to, _, state = run_save_profile(code, "NES", to, "W", state)
    check(rows[to] == "bezel_style", "handler save profile: Close up returns to Bezel Style")
    to, _, state = run_save_profile(code, "NES", rows.index("auto_load"), "S")
    to, _, state = run_save_profile(code, "NES", to, "W", state)
    check(rows[to] == "auto_load", "handler save profile: Close up returns to Auto-Load")
    to, layer, state = run_save_profile(code, "NES", rows.index("close"), pygame.K_RETURN)
    check(layer == "GAMES_SUB_MENU", "handler save profile: Enter on Close returns to the Games sub-menu")
    config = {"console_profiles": {}}
    to, layer, state = run_save_profile(code, "NES", rows.index("active"), pygame.K_RETURN, config=config, state={"menu_layer": "GAMES_SAVE_PROFILE", "games_selected_console": "NES", "games_profile_num": 2, "show_menu": True})
    check(layer == "GAMES_SAVE_PROFILE" and config["console_profiles"]["NES"]["active"] == 2, "handler save profile: Enter on the banner makes the profile active")


def test_save_profile_sequences():
    import random
    code = extract_branch(MAIN_SCRIPT, "GAMES_SAVE_PROFILE")
    random.seed(10102027)
    steps = 0
    wrong = []
    for console in SAVE_CONSOLES:
        rows = mn.save_profile_rows(*save_layout(console))
        table = mn.save_profile_table(rows)
        for _ in range(300):
            state = None
            memory = {}
            cur = random.randrange(len(rows))
            for _step in range(60):
                key = random.choice("WASD")
                want = table.step(mn.save_profile_cell(rows, cur), DIRS[key], memory)
                cur_next, _, state = run_save_profile(code, console, cur, key, state)
                steps += 1
                if cur_next != (cur if want is None else mn.save_profile_index(rows, want)):
                    wrong.append((console, cur, key, cur_next, want))
                    break
                cur = cur_next
    check(not wrong, "handler save profile: %d random steps all follow the table with the memory a mirror table wrote %s" % (steps, wrong[:3]))


# ============================================================================
# SECTION: Controller map
# ============================================================================

EXPECTED_CTRL_STICK = {
    "squares": "W:- S:- A:map_buttons D:map_buttons",
    "map_buttons": "W:- S:stick_as_dpad A:squares D:stick_as_dpad",
    "stick_as_dpad": "W:map_buttons S:close A:squares D:close",
    "close": "W:stick_as_dpad S:- A:stick_as_dpad D:-",
}
EXPECTED_CTRL_N64 = {
    "squares": "W:- S:- A:map_buttons D:map_buttons",
    "map_buttons": "W:- S:close A:squares D:close",
    "close": "W:map_buttons S:- A:map_buttons D:-",
}


def test_ctrl_map_tables():
    check(mn.ctrl_map_rows(True) == ["squares", "map_buttons", "stick_as_dpad", "close"], "ctrl map rows: with the stick row")
    check(mn.ctrl_map_rows(False) == ["squares", "map_buttons", "close"], "ctrl map rows: N64 has no stick row")
    for has_stick, want_text in ((True, EXPECTED_CTRL_STICK), (False, EXPECTED_CTRL_N64)):
        label = "ctrl map " + ("with stick" if has_stick else "N64")
        rows = mn.ctrl_map_rows(has_stick)
        t = mn.ctrl_map_table(has_stick)
        structure(t, label, "squares")
        check(mn.ctrl_map_table(has_stick) is t, label + ": table is cached")
        check(set(t.cells) == set(rows), label + ": the cells are exactly the rows")
        for index, row in enumerate(rows):
            check(mn.ctrl_map_cell(rows, index) == row and mn.ctrl_map_index(rows, row) == index, label + ": cursor %d round trip" % index)
        check(mn.ctrl_map_cell(rows, len(rows) + 1) == "close" and mn.ctrl_map_cell(rows, None) == "close", label + ": a cursor outside the rows is Close")
        want = {cell: parse_moves(text) for cell, text in want_text.items()}
        for cell in rows:
            got = {k: go(t, cell, DIRS[k]) for k in "WSAD"}
            check(got == want[cell], "%s: neighbours of %s are %s, table says %s" % (label, cell, want[cell], got))
    t = mn.ctrl_map_table(True)
    check(_walk(t, "map_buttons", "AD") == "map_buttons" and _walk(t, "stick_as_dpad", "AD") == "stick_as_dpad", "ctrl map: the squares row returns to the row you left")
    check(_walk(t, "map_buttons", "AA") == "map_buttons" and _walk(t, "stick_as_dpad", "AA") == "stick_as_dpad", "ctrl map: either side key leaves the squares to the row you left")
    check(_walk(t, "map_buttons", "AW") == "squares" and _walk(t, "map_buttons", "AS") == "squares", "ctrl map: up and down do nothing on the squares row")
    mem = {}
    _walk(t, "stick_as_dpad", "A", mem)
    check(_walk(t, "squares", "D", mem) == "stick_as_dpad", "ctrl map: the way back is kept in the memory dict")
    check(_walk(t, "squares", "D") == "map_buttons", "ctrl map: with no memory the squares row leaves to Map Buttons")
    n64 = mn.ctrl_map_table(False)
    check(_walk(n64, "squares", "D", {"squares": "stick_as_dpad"}) == "map_buttons", "ctrl map N64: a remembered Stick row is not used (no such row)")


def test_ctrl_map_squares_step():
    import itertools
    pickable_sets = [[1]] + [[1] + list(extra) for n in (1, 2, 3) for extra in itertools.combinations((2, 3, 4), n)]
    count = 0
    for pickable in pickable_sets:
        length = len(pickable) + 1
        for selected in (1, 2, 3, 4):
            for on_pick in (False, True):
                for direction in (mn.LEFT, mn.RIGHT):
                    count += 1
                    start = length - 1 if on_pick else (pickable.index(selected) if selected in pickable else 0)
                    got = mn.ctrl_map_squares_step(pickable, selected, on_pick, direction, False)
                    if direction == mn.RIGHT and start == length - 1:
                        want = (selected, on_pick, True)
                    else:
                        new = (start + (1 if direction == mn.RIGHT else -1)) % length
                        want = (selected, True, False) if new == length - 1 else (pickable[new], False, False)
                    check(got == want, "squares step pickable=%s sel=%d pick=%s %s -> %s, expected %s" % (pickable, selected, on_pick, direction, got, want))
                    check(mn.ctrl_map_squares_step(pickable, selected, on_pick, direction, True) == (selected, on_pick, True), "squares step MENU: both keys leave")
        for direction in (mn.UP, mn.DOWN):
            check(mn.ctrl_map_squares_step(pickable, 1, False, direction, False) == (1, False, False), "squares step: up and down do nothing")
    check(mn.ctrl_map_squares_step([1], 1, False, mn.LEFT, False) == (1, True, False), "squares step: left from the only player wraps to the Pick button")
    check(mn.ctrl_map_squares_step([1], 1, True, mn.LEFT, False) == (1, False, False), "squares step: left from the Pick button goes to the only player")
    check(mn.ctrl_map_squares_step([], 1, False, mn.LEFT, False) == (1, True, False) and mn.ctrl_map_squares_step([], 1, False, mn.RIGHT, False) == (1, False, True),
          "squares step: with no pickable player the row is only the Pick button")
    check(mn.ctrl_map_squares_step([1, 2, 3], 3, False, mn.RIGHT, False) == (3, True, False), "squares step: right from the last player goes to the Pick button")
    check(mn.ctrl_map_squares_step([1, 2, 3], 2, True, mn.LEFT, False) == (3, False, False), "squares step: left from the Pick button goes to the last player")
    check(mn.ctrl_map_squares_step([1, 2], 4, False, mn.RIGHT, False) == (2, False, False), "squares step: a selected player that is not pickable counts as the first square")
    print("controller map squares step: %d cases" % count)


class n2_FakeDeck(object):
    def __init__(self, console, pickable):
        self._active_mapping_console = console
        self._active_mapping_player = 1
        self._pick_layout_popup_player = None
        self._seq_remap_active = False
        self.ctrl_mapping_active_btn = None
        self.ctrl_mapping = {}
        self._mapping_close_requested = False
        self.calls = []
        self.pickable = set(pickable)

    def mapping_reassign_pending(self):
        return False

    def _player_slot_pickable(self, player, console=None):
        return player in self.pickable

    def start_sequential_remap(self):
        self.calls.append("remap")

    def _save_controller_mapping(self):
        self.calls.append("save")

    def _switch_mapping_player(self, player):
        self.calls.append(("switch", player))

    def start_controller_pick(self, player):
        self.calls.append(("pick", player))

    def start_menu_controller_pick(self):
        self.calls.append("menu_pick")


def extract_ctrl_map(script_path):
    """Returns the body of the controller map key handler, wrapped in a one-pass loop so its continue statements work."""
    with open(script_path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    marker = None
    for i, line in enumerate(lines):
        if line == '                    app_state.get("menu_layer") == "GAMES_CTRL_MAP" and':
            marker = i
            break
    if marker is None:
        raise RuntimeError("controller map handler not found")
    start = marker + 2
    end = start
    while not lines[end].startswith('            _capture_layer = app_state.get("menu_layer")'):
        end += 1
    body = [line[16:] if line.strip() else "" for line in lines[start:end]]
    return compile("for _once in (0,):\n" + "\n".join("    " + b if b else "" for b in body) + "\n", script_path + ":GAMES_CTRL_MAP", "exec")


def run_ctrl_map(code, console, cursor, key, pickable=(1, 2), state=None, player_sel=1, on_pick=False, return_row=None, memory=None):
    deck = n2_FakeDeck(console, pickable)
    if state is None:
        state = {"menu_layer": "GAMES_CTRL_MAP", "show_menu": True, "ctrl_map_cursor": cursor, "ctrl_map_player_sel": player_sel,
                 "ctrl_map_on_pick": on_pick, "ctrl_map_return_row": return_row}
        if memory is not None:
            state["_nav_memory"] = {"GAMES_CTRL_MAP": dict(memory)}
    else:
        state["ctrl_map_cursor"] = cursor
    env = {"pygame": pygame, "menu_nav": mn, "app_state": state, "game_deck": deck, "event": types.SimpleNamespace(key=n2_ALL_KEYS.get(key, key)), "print": lambda *a, **k: None}
    exec(code, env)
    return state, deck


def ctrl_snapshot(state):
    return (state["ctrl_map_cursor"], state["ctrl_map_player_sel"], state["ctrl_map_on_pick"])


def test_ctrl_map_handler():
    code = extract_ctrl_map(MAIN_SCRIPT)
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    check(text.count("menu_nav.ctrl_map_table(") == 2 and text.count("menu_nav.ctrl_map_rows(") == 1, "main script: one hookup for the controller map")
    for console in ("GBA", "N64", "MENU", "NES"):
        has_stick = console != "N64"
        rows = mn.ctrl_map_rows(has_stick)
        table = mn.ctrl_map_table(has_stick)
        for cursor, cell in enumerate(rows):
            for key in n2_ALL_KEYS:
                state, _ = run_ctrl_map(code, console, cursor, key, memory={"squares": "map_buttons"})
                if cell == "squares":
                    continue
                want = table.step(cell, ALL_DIRS[key], {})
                got = state["ctrl_map_cursor"]
                check(got == (cursor if want is None else mn.ctrl_map_index(rows, want)), "handler ctrl map %s cursor %d (%s) %s -> %s (table says %s)" % (console, cursor, cell, key, got, want))
                if want == "squares":
                    check(state["ctrl_map_on_pick"] == (console == "MENU") and (console == "MENU" or state["ctrl_map_player_sel"] == 1),
                          "handler ctrl map %s: A into the squares row selects player 1 (MENU: the Pick button)" % console)
    state, deck = run_ctrl_map(code, "GBA", 1, pygame.K_RETURN)
    check(deck.calls == ["remap"], "handler ctrl map: Enter on Map Buttons starts the sequential remap")
    state, deck = run_ctrl_map(code, "GBA", 2, pygame.K_RETURN)
    check(deck.calls == ["save"] and deck.ctrl_mapping.get("stick_as_dpad") is True, "handler ctrl map: Enter on Stick as D-pad flips it and saves")
    state, deck = run_ctrl_map(code, "GBA", 3, pygame.K_RETURN)
    check(deck._mapping_close_requested is True, "handler ctrl map: Enter on Close asks to close")
    state, deck = run_ctrl_map(code, "N64", 2, pygame.K_RETURN)
    check(deck._mapping_close_requested is True, "handler ctrl map N64: Enter on Close (cursor 2) asks to close")
    state, deck = run_ctrl_map(code, "GBA", 0, pygame.K_RETURN, player_sel=2)
    check(deck.calls == [("switch", 2)], "handler ctrl map: Enter on a player square switches to that player")
    state, deck = run_ctrl_map(code, "GBA", 0, pygame.K_RETURN, on_pick=True, player_sel=2)
    check(deck.calls == [("pick", 2)], "handler ctrl map: Enter on the Pick button picks a controller for the selected player")
    state, deck = run_ctrl_map(code, "MENU", 0, pygame.K_RETURN, on_pick=True)
    check(deck.calls == ["menu_pick"], "handler ctrl map MENU: Enter on the Pick button starts the menu pick")
    state, deck = run_ctrl_map(code, "GBA", 1, pygame.K_x)
    check(ctrl_snapshot(state) == (1, 1, False), "handler ctrl map: another key changes nothing")


def case_n3c():
    test_n3c_directions()
    test_dvd_map_tables()
    test_dvd_map_handler()
    test_save_profile_tables()
    test_save_profile_way_back()
    test_save_profile_handler()
    test_save_profile_sequences()
    test_ctrl_map_tables()
    test_ctrl_map_squares_step()
    test_ctrl_map_handler()


class TestGamesSubRowsMatchUi(_SectionCase):
    """The Games sub-menu row lists the navigation tests use are the same lists ui.py builds"""

    def test_games_sub_rows_match_ui(self):
        self.run_section(case_games_sub_rows_match_ui)


class TestMenuNavChannelsVideoGames(_SectionCase):
    """Channels, channel settings, Video, Theme, Games list and sub-menu, DVD button map, Save Profile, controller map"""

    def setUp(self):
        self._real_game_deck = sys.modules.get("game_deck")
        sys.modules["game_deck"] = GAME_DECK_STUB

    def tearDown(self):
        if self._real_game_deck is None:
            sys.modules.pop("game_deck", None)
        else:
            sys.modules["game_deck"] = self._real_game_deck

    def test_games_list(self):
        self.run_section(case_games_list)

    def test_games_handler(self):
        self.run_section(case_games_handler)

    def test_games_sub_tables(self):
        self.run_section(case_games_sub_tables)

    def test_games_sub_way_back(self):
        self.run_section(case_games_sub_way_back)

    def test_games_sub_handler(self):
        self.run_section(case_games_sub_handler)

    def test_games_sub_sequences(self):
        self.run_section(case_games_sub_sequences)

    def test_channels(self):
        self.run_section(case_channels)

    def test_channel_settings(self):
        self.run_section(case_channel_settings)

    def test_video(self):
        self.run_section(case_video)

    def test_theme(self):
        self.run_section(case_theme)

    def test_handlers(self):
        self.run_section(case_handlers)

    def test_n3c(self):
        self.run_section(case_n3c)


# ============================================================================
# SECTION: TV guide (ch04) and game deck / DVD handlers
# ============================================================================


n4_KEYS = {"W": pygame.K_w, "A": pygame.K_a, "S": pygame.K_s, "D": pygame.K_d}


# ============================================================================
# SECTION: Pure functions
# ============================================================================


def case_guide_move():
    check(mn.guide_move(mn.UP) == (mn.GUIDE_ROW, -1), "guide up")
    check(mn.guide_move(mn.DOWN) == (mn.GUIDE_ROW, 1), "guide down")
    check(mn.guide_move(mn.LEFT) == (mn.GUIDE_TIME, -1), "guide left")
    check(mn.guide_move(mn.RIGHT) == (mn.GUIDE_TIME, 1), "guide right")
    check(mn.guide_move(None) is None, "guide none")
    check(mn.guide_move("accept") is None, "guide unknown")


def reference_row_step(allowed, selected, offset, rows, step):
    """The old W / S code of the guide, written out as it was."""
    if not allowed:
        return None
    selected = str(selected).zfill(2)
    pos = allowed.index(selected) if selected in allowed else 0
    if step < 0:
        nxt = pos - 1 if pos > 0 else len(allowed) - 1
        if nxt < offset:
            offset = nxt
        elif nxt >= offset + rows:
            offset = max(0, nxt - (rows - 1))
    else:
        nxt = pos + 1 if pos < len(allowed) - 1 else 0
        if nxt >= offset + rows:
            offset = nxt - (rows - 1)
        elif nxt < offset:
            offset = nxt
    return allowed[nxt], offset


def case_guide_row_step():
    check(mn.guide_row_step([], 5, 0, 5, 1) is None, "guide rows: no rows")
    check(mn.guide_row_step([], 5, 0, 5, -1) is None, "guide rows: no rows up")
    rng = random.Random(4)
    cases = 0
    for total in (1, 2, 3, 5, 6, 12, 40):
        allowed = [str(5 + i).zfill(2) for i in range(total)]
        for rows in (1, 2, 4, 5, 8, 40):
            for offset in range(0, max(1, total - rows + 1)):
                for selected in allowed + ["99", "04"]:
                    for step in (-1, 1):
                        want = reference_row_step(allowed, selected, offset, rows, step)
                        got = mn.guide_row_step(allowed, selected, offset, rows, step)
                        cases += 1
                        check(got == want, "guide rows total=%d rows=%d off=%d sel=%s step=%d got=%r want=%r" % (total, rows, offset, selected, step, got, want))
    check(cases > 5000, "guide rows cases ran: %d" % cases)
    check(mn.guide_row_step(["05", "06"], 5, 0, 5, -1)[0] == "06", "guide rows wrap up")
    check(mn.guide_row_step(["05", "06"], "06", 0, 5, 1)[0] == "05", "guide rows wrap down")
    check(mn.guide_row_step(["05", "06", "07"], 5, 0, 5, 1) == ("06", 0), "guide rows plain step")
    for _ in range(2000):
        total = rng.randint(1, 40)
        allowed = [str(5 + i).zfill(2) for i in range(total)]
        rows = rng.randint(1, 9)
        offset = rng.randint(0, max(0, total - 1))
        selected = rng.choice(allowed)
        step = rng.choice((-1, 1))
        check(mn.guide_row_step(allowed, selected, offset, rows, step) == reference_row_step(allowed, selected, offset, rows, step), "guide rows random")
    walk, offset = "05", 0
    allowed = [str(5 + i).zfill(2) for i in range(40)]
    seen = set()
    for _ in range(40):
        walk, offset = mn.guide_row_step(allowed, walk, offset, 5, 1)
        seen.add(walk)
        check(offset <= allowed.index(walk) < offset + 5, "guide rows keep selection visible going down")
    check(len(seen) == 40 and walk == "05", "guide rows walk all 40 and wrap")


def case_deck_functions():
    check(mn.deck_browse_step("side", mn.LEFT) == (-1, mn.LEFT), "browse side left")
    check(mn.deck_browse_step("side", mn.RIGHT) == (1, mn.RIGHT), "browse side right")
    check(mn.deck_browse_step("side", mn.UP) is None and mn.deck_browse_step("side", mn.DOWN) is None, "browse side up/down ignored")
    for style in ("stack", "vertical", "", None):
        check(mn.deck_browse_step(style, mn.UP) == (-1, mn.UP), "browse %r up" % (style,))
        check(mn.deck_browse_step(style, mn.DOWN) == (1, mn.DOWN), "browse %r down" % (style,))
        check(mn.deck_browse_step(style, mn.LEFT) is None and mn.deck_browse_step(style, mn.RIGHT) is None, "browse %r left/right ignored" % (style,))
    check(mn.deck_browse_step("side", None) is None, "browse none")
    check(mn.deck_list_step(mn.UP) == mn.DECK_PREV, "list up")
    check(mn.deck_list_step(mn.DOWN) == mn.DECK_NEXT, "list down")
    check(mn.deck_list_step(mn.RIGHT) == mn.DECK_TO_ARROWS, "list right")
    check(mn.deck_list_step(mn.LEFT) is None and mn.deck_list_step(None) is None, "list left and none")
    check(mn.deck_arrows_step("up", mn.UP) == mn.DECK_ACTIVATE, "arrows up on up")
    check(mn.deck_arrows_step("down", mn.UP) == mn.DECK_SELECT_UP, "arrows up on down")
    check(mn.deck_arrows_step("down", mn.DOWN) == mn.DECK_ACTIVATE, "arrows down on down")
    check(mn.deck_arrows_step("up", mn.DOWN) == mn.DECK_SELECT_DOWN, "arrows down on up")
    check(mn.deck_arrows_step("up", mn.LEFT) == mn.DECK_TO_LIST and mn.deck_arrows_step("down", mn.LEFT) == mn.DECK_TO_LIST, "arrows left")
    check(mn.deck_arrows_step("up", mn.RIGHT) is None and mn.deck_arrows_step("up", None) is None, "arrows right and none")
    check(mn.deck_confirm_choice(mn.LEFT) == "Yes" and mn.deck_confirm_choice(mn.RIGHT) == "No", "confirm left/right")
    check(mn.deck_confirm_choice(mn.UP) is None and mn.deck_confirm_choice(mn.DOWN) is None and mn.deck_confirm_choice(None) is None, "confirm up/down/none")
    check(mn.key_to_direction(pygame.K_UP) is None and mn.key_to_direction(pygame.K_UP, arrows=True) == mn.UP, "arrow keys only with arrows=True")


# ============================================================================
# SECTION: Guide key block
# ============================================================================

GUIDE_START = 'if app_state["current_channel"] == "04" and not app_state["show_menu"] and not app_state["show_quit_confirm"] and not app_state["show_splash"]:'
GUIDE_END = '            if app_state.get("show_menu", False):\n                pass\n'


def extract_guide(script_path):
    """Returns the guide key block of a main script, wrapped in a one-pass loop so its continue statements work."""
    with open(script_path, encoding="utf-8") as handle:
        text = handle.read()
    begin = text.index(GUIDE_START)
    line_start = text.rfind("\n", 0, begin) + 1
    indent = begin - line_start
    end = text.index(GUIDE_END, begin)
    body = text[line_start:end].split("\n")
    lines = [ln[indent:] if ln.strip() else "" for ln in body]
    code = "for _once in (0,):\n" + "\n".join("    " + ln if ln else "" for ln in lines) + "\n"
    return compile(code, script_path + ":guide", "exec")


def step_row_source():
    """Returns the source of the main script's _guide_step_row, which the guide key block calls for the row moves."""
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == "_guide_step_row":
            return textwrap.dedent(" " * node.col_offset + ast.get_source_segment(text, node))
    raise KeyError("_guide_step_row")


STEP_ROW_CODE = compile(step_row_source(), MAIN_SCRIPT + ":_guide_step_row", "exec")


class n4_FakeDb:
    def __init__(self, active):
        self.channels_db = {str(i).zfill(2): {"active": (i in active)} for i in range(5, 45)}


def n4_run_guide(code, active, selected, offset, rows, key, curr_col=0):
    calls = []
    state = {
        "current_channel": "04", "show_menu": False, "show_quit_confirm": False, "show_splash": False,
        "selected_guide_channel": selected, "guide_row_offset": offset, "highlighted_guide_channel": None,
    }
    env = {
        "app_state": state, "db": n4_FakeDb(active), "pygame": pygame, "event": types.SimpleNamespace(key=key),
        "menu_nav": mn, "get_guide_visible_rows": lambda _db: rows,
        "_clear_last_video_frame": lambda context=None: calls.append(("clear", context)),
        "_arm_guide_preview_transition": lambda: calls.append(("arm",)),
        "_resync_guide_stop_after_row_switch": lambda: calls.append(("resync",)),
        "_guide_jump_col": lambda step, _source="": calls.append(("jump", step, _source)),
        "change_channel": lambda ch, is_surfing=False: calls.append(("tune", ch, is_surfing)),
    }
    exec(STEP_ROW_CODE, env)
    exec(code, env)
    state.pop("guide_inactivity_timer", None)
    return calls, dict(state)


def guide_cases(rng):
    for _ in range(600):
        total = rng.randint(0, 40)
        active = set(rng.sample(range(5, 45), total))
        names = sorted(active)
        rows = rng.randint(1, 8)
        offset = rng.randint(0, max(0, total))
        selected = rng.choice(names) if names and rng.random() < 0.85 else rng.choice((5, 44, 12))
        yield active, selected, offset, rows


def case_guide_block():
    code = extract_guide(MAIN_SCRIPT)
    rng = random.Random(11)
    n = 0
    for active, selected, offset, rows in guide_cases(rng):
        names = [str(i).zfill(2) for i in sorted(active)]
        for label, key in n4_KEYS.items():
            calls, state = n4_run_guide(code, active, selected, offset, rows, key)
            n += 1
            if label in ("A", "D"):
                step = -1 if label == "A" else 1
                check(calls == [("jump", step, "keydown")], "guide %s jumps time %d: %r" % (label, step, calls))
                check(state["selected_guide_channel"] == selected and state["guide_row_offset"] == offset, "guide %s leaves rows alone" % label)
            else:
                step = -1 if label == "W" else 1
                want = mn.guide_row_step(names, selected, offset, rows, step)
                if want is None:
                    check(calls == [] and state["selected_guide_channel"] == selected, "guide %s with no rows does nothing" % label)
                else:
                    check(state["selected_guide_channel"] == int(want[0]) and state["highlighted_guide_channel"] == want[0], "guide %s selects %s" % (label, want[0]))
                    check(state["guide_row_offset"] == want[1], "guide %s offset" % label)
                    check(calls == [("clear", "preview"), ("arm",), ("resync",)], "guide %s calls %r" % (label, calls))
    calls, state = n4_run_guide(code, {5, 6}, 6, 0, 5, pygame.K_RETURN)
    check(calls == [("tune", "06", True)], "guide Enter tunes the selected row: %r" % (calls,))
    calls, state = n4_run_guide(code, {5, 6}, 6, 0, 5, pygame.K_x)
    check(calls == [], "guide other key does nothing")
    check(n == 2400, "guide block cases ran: %d" % n)


# ============================================================================
# SECTION: Game channel
# ============================================================================


def load_deck_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeVlc:
    def __init__(self, log):
        self.log = log
        self.player = types.SimpleNamespace(get_time=lambda: 0, get_length=lambda: 0, pause=lambda: None, set_time=lambda t: None)

    def navigate(self, what):
        self.log.append(("vlc.navigate", what))

    def stop(self):
        self.log.append(("vlc.stop",))


class FakeShadow:
    def __init__(self, log):
        self.log = log

    def mirror_navigate(self, what):
        self.log.append(("shadow", what))


_RECORDED = ("_enter_romlist", "_launch_selected_rom", "_scan_roms", "_snap_rom_cursor_if_scan_landed", "_set_dvd_osd",
             "_dvd_save_resume_position", "_close_dvd_highlight_shadow", "_get_console_rom_dir")


def make_deck(module, **attrs):
    cls = module.ExternalGameDeck
    deck = object.__new__(cls)
    log = []
    base = dict(
        mode="BROWSE", dvd_playback_active=False, scroll_style="side", consoles=["NES", "SNES", "GB", "DVD"], console_index=1,
        romlist_focus="list", romlist_arrow_sel="up", roms=[("a", "a"), ("b", "b"), ("c", "c")], rom_index=1, rom_scroll=0,
        active_console="SNES", current_browse_dir="", _console_arrow_flash_dir=None, _console_arrow_flash_until=0,
        dvd_exit_confirm=False, dvd_exit_selection="No", dvd_exit_confirm_shown_at=0,
        dvd_return_menu_confirm=False, dvd_return_menu_selection="No", dvd_return_menu_confirm_shown_at=0,
        dvd_splash_playing=False, dvd_is_disc_image=True, dvd_in_menu=True, _dvd_menu_manual_override=False,
        vlc_engine=FakeVlc(log), _dvd_highlight_shadow=FakeShadow(log), dvd_resume_enabled=False, dvd_file_path="",
        dvd_paused=False, dvd_hud_until=0, _dvd_skip_cooldown=0, _scan_pending=False, _CONSOLE_ARROW_FLASH_MS=200,
    )
    base.update(attrs)
    for name, value in base.items():
        setattr(deck, name, value)
    for name in _RECORDED:
        setattr(deck, name, (lambda n: (lambda *a, **k: log.append((n,) + a)))(name))
    deck._dvd_translate_key = lambda key: key
    deck._dvd_menu_is_up = lambda: bool(deck.dvd_in_menu) and deck.dvd_is_disc_image
    deck._cycle_console = lambda delta: (log.append(("cycle", delta)), setattr(deck, "console_index", (deck.console_index + delta) % len(deck.consoles)))
    deck._log = log
    return deck


def snapshot(deck):
    return {k: v for k, v in vars(deck).items() if not callable(v) and k not in ("vlc_engine", "_dvd_highlight_shadow", "_log")}


def run_event(module, setup, event):
    saved = pygame.time.get_ticks
    pygame.time.get_ticks = lambda: 5000
    try:
        deck = make_deck(module, **setup)
        result = deck.handle_event(event)
        return result, snapshot(deck), list(deck._log)
    finally:
        pygame.time.get_ticks = saved


def key_event(key):
    return pygame.event.Event(pygame.KEYDOWN, key=key, mod=0, unicode="")


def deck_setups():
    for style in ("side", "stack"):
        yield dict(mode="BROWSE", scroll_style=style)
    for focus in ("list", "arrows"):
        for sel in ("up", "down"):
            for index in (0, 1, 2):
                yield dict(mode="ROMLIST", romlist_focus=focus, romlist_arrow_sel=sel, rom_index=index)
            yield dict(mode="ROMLIST", romlist_focus=focus, romlist_arrow_sel=sel, roms=[], rom_index=0)
            yield dict(mode="ROMLIST", romlist_focus=focus, romlist_arrow_sel=sel, consoles=["NES"], console_index=0, rom_index=1)
    yield dict(mode="GAME")


n4_ALL_KEYS = list(n4_KEYS.values()) + [pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_x]


def case_deck_handler():
    module = load_deck_module(os.path.join(ROOT, "game_deck.py"), "game_deck_new")
    n = 0
    for setup in deck_setups():
        for key in n4_ALL_KEYS:
            result, snap, log = run_event(module, setup, key_event(key))
            n += 1
            mode = setup["mode"]
            if mode == "GAME":
                check(result is False and log == [], "deck GAME ignores keys")
                continue
            if mode == "BROWSE" and key in n4_KEYS.values():
                direction = {pygame.K_w: mn.UP, pygame.K_s: mn.DOWN, pygame.K_a: mn.LEFT, pygame.K_d: mn.RIGHT}[key]
                move = mn.deck_browse_step(setup["scroll_style"], direction)
                if move is None:
                    check(result is True and log == [], "deck browse %s %s unused direction" % (setup["scroll_style"], direction))
                else:
                    check(("cycle", move[0]) in log and snap["_console_arrow_flash_dir"] == move[1], "deck browse %s %s moves %s" % (setup["scroll_style"], direction, move))
            if mode == "ROMLIST" and setup["romlist_focus"] == "list" and key in (pygame.K_w, pygame.K_s):
                want = setup["rom_index"] + (-1 if key == pygame.K_w else 1)
                roms_count = len(setup["roms"]) if "roms" in setup else 3
                if not roms_count or not (0 <= want < roms_count):
                    want = setup["rom_index"]
                check(snap["rom_index"] == want and result is True, "deck list %s %d" % (key, setup["rom_index"]))
            if mode == "ROMLIST" and setup["romlist_focus"] == "list" and key == pygame.K_d:
                check(snap["romlist_focus"] == "arrows", "deck list D goes to arrows")
            if mode == "ROMLIST" and setup["romlist_focus"] == "arrows" and key == pygame.K_a:
                check(snap["romlist_focus"] == "list", "deck arrows A goes to list")
            if mode == "ROMLIST" and setup["romlist_focus"] == "arrows" and key in (pygame.K_w, pygame.K_s):
                which = "up" if key == pygame.K_w else "down"
                if setup["romlist_arrow_sel"] == which:
                    check(any(entry[0] == "cycle" for entry in log) or len(setup.get("consoles", [0, 1])) <= 1, "deck arrows %s activates" % which)
                else:
                    check(snap["romlist_arrow_sel"] == which and not log, "deck arrows %s selects" % which)
    check(n == 17 * len(n4_ALL_KEYS) - 0 or n > 200, "deck handler cases ran: %d" % n)


def dvd_setups():
    for confirm in ("exit", "return", "none"):
        for sel in ("Yes", "No"):
            for disc in (True, False):
                for in_menu in (True, False):
                    setup = dict(mode="DVD_PLAYER", dvd_playback_active=True, dvd_is_disc_image=disc, dvd_in_menu=in_menu)
                    if confirm == "exit":
                        setup.update(dvd_exit_confirm=True, dvd_exit_selection=sel)
                    elif confirm == "return":
                        setup.update(dvd_return_menu_confirm=True, dvd_return_menu_selection=sel)
                    yield setup


DVD_KEYS = list(n4_KEYS.values()) + [pygame.K_RETURN, pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_x, pygame.K_BACKSPACE]


def case_dvd_handler():
    module = load_deck_module(os.path.join(ROOT, "game_deck.py"), "game_deck_new2")
    n = 0
    for setup in dvd_setups():
        for key in DVD_KEYS:
            result, snap, log = run_event(module, setup, key_event(key))
            n += 1
            check(result is True, "dvd handler returns True")
            popup = setup.get("dvd_exit_confirm") or setup.get("dvd_return_menu_confirm")
            if popup:
                field = "dvd_exit_selection" if setup.get("dvd_exit_confirm") else "dvd_return_menu_selection"
                if key == pygame.K_a:
                    check(snap[field] == "Yes", "dvd popup A is Yes")
                elif key == pygame.K_d:
                    check(snap[field] == "No", "dvd popup D is No")
                elif key != pygame.K_RETURN:
                    check(snap[field] == setup[field], "dvd popup other keys keep the answer")
            elif setup["dvd_is_disc_image"] and setup["dvd_in_menu"]:
                expected = {pygame.K_w: "up", pygame.K_s: "down", pygame.K_a: "left", pygame.K_d: "right", pygame.K_RETURN: "activate"}
                if key in expected:
                    check(("vlc.navigate", expected[key]) in log and ("shadow", expected[key]) in log, "dvd menu %s navigates %s" % (key, expected[key]))
                else:
                    check(not any(entry[0] == "vlc.navigate" for entry in log) or key == pygame.K_BACKSPACE, "dvd menu other keys do not navigate")
    check(n > 200, "dvd handler cases ran: %d" % n)
    arrows = {pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left", pygame.K_RIGHT: "right", pygame.K_w: "up", pygame.K_s: "down",
              pygame.K_a: "left", pygame.K_d: "right", pygame.K_RETURN: "activate"}
    for key, want in arrows.items():
        deck = make_deck(module, mode="DVD_PLAYER")
        check(deck._handle_dvd_menu_nav(key) is True and ("vlc.navigate", want) in deck._log and ("shadow", want) in deck._log, "dvd menu direct %s -> %s" % (key, want))
    deck = make_deck(module, mode="DVD_PLAYER")
    deck._handle_dvd_menu_nav(pygame.K_x)
    check(deck._log == [], "dvd menu direct: other key does nothing")
    deck = make_deck(module, mode="DVD_PLAYER", _dvd_menu_manual_override=True)
    deck._handle_dvd_menu_nav(pygame.K_RETURN)
    check(deck._dvd_menu_manual_override is False, "dvd menu Enter clears the manual override")
    deck = make_deck(module, mode="DVD_PLAYER", vlc_engine=None)
    check(deck._handle_dvd_menu_nav(pygame.K_w) is True and deck._log == [], "dvd menu with no player does nothing")


class TestMenuNavGuideAndDeck(_SectionCase):
    """TV guide (ch04) and game deck / DVD handlers"""

    def test_guide_move(self):
        self.run_section(case_guide_move)

    def test_guide_row_step(self):
        self.run_section(case_guide_row_step)

    def test_deck_functions(self):
        self.run_section(case_deck_functions)

    def test_guide_block(self):
        self.run_section(case_guide_block)

    def test_deck_handler(self):
        self.run_section(case_deck_handler)

    def test_dvd_handler(self):
        self.run_section(case_dvd_handler)


# ============================================================================
# SECTION: Tab bar, Server menu and slots, Remote Remap, audio picker, Holiday sub-menu, popups
# ============================================================================


DIRECTION_KEYS = [pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d]
OTHER_KEYS = [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_x, pygame.K_ESCAPE]
n6_ALL_KEYS = DIRECTION_KEYS + OTHER_KEYS


# ============================================================================
# SECTION: Table functions
# ============================================================================


def case_tabs():
    pools = (["System", "Channels", "Games", "Video", "Theme"], ["System", "Games", "Video", "Theme"],
             ["System", "Channels", "Video", "Theme"], ["System", "Video", "Theme"], ["Video"])
    for pool in pools:
        table = mn.tabs_table(pool)
        check(mn.tabs_table(list(pool)) is table, "tabs table cached %r" % pool)
        for i, tab in enumerate(pool):
            memory = {}
            check(table.step(tab, mn.RIGHT, memory) == (pool[(i + 1) % len(pool)] if len(pool) > 1 else None), "tabs %s right in %r" % (tab, pool))
            check(table.step(tab, mn.LEFT, memory) == (pool[i - 1] if len(pool) > 1 else None), "tabs %s left in %r" % (tab, pool))
            check(table.step(tab, mn.DOWN, memory) == mn.PANEL, "tabs %s down leaves into the panel" % tab)
            check(table.step(tab, mn.UP, memory) is None, "tabs %s up does nothing" % tab)
            check(mn.tabs_cell(pool, tab) == tab, "tabs_cell keeps %s" % tab)
        check(mn.tabs_cell(pool, "Bogus") == pool[0] and mn.tabs_cell(pool, None) == pool[0], "tabs_cell unknown tab is the first")
        seen, walk = {pool[0]}, pool[0]
        for _ in range(len(pool)):
            walk = table.step(walk, mn.RIGHT, {}) or walk
            seen.add(walk)
        check(seen == set(pool), "tabs walk right reaches every tab in %r" % pool)
    check(set(mn.TAB_PANEL_LAYERS) == {"System", "Channels", "Games", "Video", "Theme"}, "tab panel layers cover the five tabs")
    check(mn.TAB_PANEL_LAYERS["Games"] == "GAMES_LIST" and mn.TAB_PANEL_LAYERS["System"] == "SYSTEM_ROWS", "tab panel layer names")


def old_clamp(selection, count, step):
    return max(0, min(count - 1, selection + step))


def case_column_move():
    for count in range(1, 12):
        for selection in range(-3, count + 4):
            for direction, step in ((mn.UP, -1), (mn.DOWN, 1)):
                got = mn.column_move({}, "T", selection, count, direction)
                check(got == old_clamp(selection, count, step), "column_move count=%d sel=%d %s got %d" % (count, selection, direction, got))
            for direction in (mn.LEFT, mn.RIGHT):
                got = mn.column_move({}, "T", selection, count, direction)
                check(got == selection, "column_move count=%d sel=%d %s does nothing" % (count, selection, direction))
    check(mn.column_move({}, "T", 3, 0, mn.DOWN) == 3, "column_move empty list leaves the number")
    walk = 0
    for _ in range(20):
        walk = mn.column_move({}, "T", walk, 6, mn.DOWN)
    check(walk == 5, "column_move down stops on the last row")
    check(mn.column_move({}, "T", 5, 6, mn.UP) == 4, "column_move up from the last row")


def case_ring_move():
    for count in range(1, 9):
        for selection in range(-4, count + 5):
            for direction in mn.DIRECTIONS:
                step = -1 if direction in (mn.UP, mn.LEFT) else 1
                got = mn.ring_move({}, "T", selection, count, direction)
                check(got == (selection + step) % count, "ring_move count=%d sel=%d %s got %d" % (count, selection, direction, got))
    check(mn.ring_move({}, "T", 0, 3, mn.UP) == 2 and mn.ring_move({}, "T", 2, 3, mn.DOWN) == 0, "ring_move wraps both ways")


def case_server_slots():
    table = mn.server_slots_table()
    check(mn.server_slots_table() is table, "server slots table cached")
    check(len(mn.SERVER_SLOT_CELLS) == 9 and mn.SERVER_SLOT_CELLS[6:] == ("create", "manage", mn.CLOSE), "server slots cells")
    for sel in range(9):
        check(mn.server_slots_index(mn.server_slots_cell(sel)) == sel, "server slots cell round trip %d" % sel)
        for direction in mn.DIRECTIONS:
            column, row = sel // 3, sel % 3
            if direction == mn.UP:
                row = max(0, row - 1)
            elif direction == mn.DOWN:
                row = min(2, row + 1)
            elif direction == mn.LEFT:
                column = max(0, column - 1)
            else:
                column = min(2, column + 1)
            want = column * 3 + row
            to = mn.navigate({}, "T", table, mn.server_slots_cell(sel), direction)
            got = sel if to is None else mn.server_slots_index(to)
            check(got == want, "server slots %d %s got %d want %d" % (sel, direction, got, want))
    check(mn.server_slots_cell(99) == mn.CLOSE and mn.server_slots_cell(-1) == mn.CLOSE, "server slots out of range is Close")
    seen, todo = {"slot0"}, ["slot0"]
    while todo:
        cur = todo.pop()
        for direction in mn.DIRECTIONS:
            to = table.step(cur, direction, {})
            if to not in (None, mn.TAB_BAR, mn.PANEL) and to not in seen:
                seen.add(to)
                todo.append(to)
    check(seen == set(table.cells), "server slots every cell reachable")


def case_channel_sub_table():
    seen_layouts = set()
    for info in channel_sub_infos():
        for count in (1, 2):
            layout = mn.channel_sub_layout(info, count)
            if layout in seen_layouts:
                continue
            seen_layouts.add(layout)
            table = mn.channel_sub_table(layout)
            check(mn.channel_sub_table(layout) is table, "channel sub table cached %r" % (layout,))
            close = mn.channel_sub_close_row(layout)
            on, music, marathon = layout[0], layout[1], layout[2]
            check(close == (1 if not on else 4 if music else 6 if marathon else 7), "channel sub close row %r" % (layout,))
            for row in range(close + 1):
                for col in range(mn.CHANNEL_SUB_MAX_COL + 1):
                    check(mn.channel_sub_index(mn.channel_sub_cell(row, col)) == (row, col), "channel sub cell round trip")
                    for direction in mn.DIRECTIONS:
                        to = table.step(mn.channel_sub_cell(row, col), direction, {})
                        if to is not None:
                            r, c = mn.channel_sub_index(to)
                            check(0 <= r <= close and 0 <= c <= mn.CHANNEL_SUB_MAX_COL, "channel sub %r (%d,%d) %s lands inside" % (layout, row, col, direction))
            seen, todo = {mn.channel_sub_cell(0, 0 if not layout[6] else 1)}, [mn.channel_sub_cell(0, 0 if not layout[6] else 1)]
            while todo:
                cur = todo.pop()
                for direction in mn.DIRECTIONS:
                    to = table.step(cur, direction, {})
                    if to is not None and to not in seen:
                        seen.add(to)
                        todo.append(to)
            check(mn.channel_sub_cell(close, 0) in seen, "channel sub Close reachable %r" % (layout,))
            check(all(any(mn.channel_sub_cell(r, c) in seen for c in range(mn.CHANNEL_SUB_MAX_COL + 1)) for r in range(close + 1)), "channel sub every row reachable %r" % (layout,))
    check(len(seen_layouts) > 20, "channel sub layouts covered: %d" % len(seen_layouts))
    fresh = {"active": True}
    layout = mn.channel_sub_layout(fresh, 1)
    table = mn.channel_sub_table(layout)
    check(table.step("r1c0", mn.UP, {}) == "r0c1" and table.step("r0c1", mn.LEFT, {}) is None, "only active channel: up lands on the Mode button and left cannot reach On")
    layout = mn.channel_sub_layout(fresh, 5)
    check(mn.channel_sub_table(layout).step("r1c0", mn.UP, {}) == "r0c0", "other channels on: up lands on the On button")
    layout = mn.channel_sub_layout(fresh, 5)
    table = mn.channel_sub_table(layout)
    check(table.step("r7c0", mn.DOWN, {}) is None or table.step("r7c0", mn.DOWN, {}) == "r7c1", "Clear to Close share the last row")


def case_popup_tables():
    for has_box in (False, True):
        table = mn.confirm_table(has_box)
        check(mn.confirm_table(has_box) is table, "confirm table cached %s" % has_box)
        for selection in ("Yes", "No", None, "x"):
            for focus in ("buttons", "checkbox"):
                cell = mn.confirm_cell(selection, focus)
                check((mn.confirm_focus(cell), mn.confirm_answer(cell)) == (focus, "Yes" if selection == "Yes" else "No"), "confirm cell round trip %s %s" % (selection, focus))
        check(table.step("no", mn.LEFT, {}) == "yes" and table.step("yes", mn.RIGHT, {}) == "no", "confirm left is Yes, right is No")
        check(table.step("yes", mn.LEFT, {}) is None and table.step("no", mn.RIGHT, {}) is None, "confirm stays on the answer it is on")
        check((table.step("yes", mn.DOWN, {}) == "check_yes") == has_box and (table.step("no", mn.DOWN, {}) == "check_no") == has_box, "confirm down reaches the box only when there is one")
        if has_box:
            check(table.step("check_no", mn.UP, {}) == "no" and table.step("check_yes", mn.UP, {}) == "yes", "confirm up returns to the same answer")
            check(table.step("check_yes", mn.LEFT, {}) is None and table.step("check_no", mn.RIGHT, {}) is None, "confirm sideways does nothing on the box")
    table = mn.splash_table()
    check(table.step("back", mn.RIGHT, {}) == "next" and table.step("next", mn.LEFT, {}) == "back", "splash left and right")
    check(table.step("back", mn.LEFT, {}) is None and table.step("next", mn.RIGHT, {}) is None and table.step("next", mn.UP, {}) is None, "splash edges")
    check(mn.splash_cell(0) == "back" and mn.splash_cell(1) == "next" and mn.splash_focus("back") == 0 and mn.splash_focus("next") == 1, "splash cell and focus")


def case_gate_key_classes():
    for key in list(range(0, 400)) + [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d]:
        old_gate = key in (pygame.K_a, pygame.K_d, pygame.K_RETURN)
        new_gate = mn.deck_confirm_choice(mn.key_to_direction(key)) is not None or key == pygame.K_RETURN
        check(old_gate == new_gate, "DVD gate key class %d" % key)
        old_nav = key in (pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d, pygame.K_ESCAPE, pygame.K_RETURN)
        new_nav = mn.key_to_direction(key) is not None or key in (pygame.K_ESCAPE, pygame.K_RETURN)
        check(old_nav == new_nav, "DVD deck key class %d" % key)


def case_tab_bar_exit_targets():
    table = mn.NavTable()
    table.known("a")
    table.link("a", mn.DOWN, mn.PANEL)
    table.link("a", mn.UP, mn.TAB_BAR)
    memory = {}
    check(table.step("a", mn.DOWN, memory) == mn.PANEL and table.step("a", mn.UP, memory) == mn.TAB_BAR and memory == {}, "PANEL and TAB_BAR are exits and record nothing")
    check(mn.navigate({}, "T", table, "a", mn.LEFT) is None, "an unlinked direction stays")


# ============================================================================
# SECTION: Handler blocks cut from a main script
# ============================================================================

LAYER = '                elif app_state["menu_layer"] == "%s":\n'
NEXT_LAYER = '                elif app_state["menu_layer"] == '


def cut_layer(text, layer):
    """Returns the handler of one menu layer, dedented, as the body of an if."""
    begin = text.index(LAYER % layer) + len(LAYER % layer)
    end = text.index(NEXT_LAYER, begin)
    return textwrap.dedent(text[begin:end])


def cut_tabs(text):
    """Returns the tab bar code with the tab pool set-up before it."""
    begin = text.index('                game_ch_on = db.config.get("game_channel_enabled", True)')
    end = text.index(LAYER % "CHANNEL_LIST", begin)
    return textwrap.dedent(text[begin:end])


class Quiet:
    def __init__(self):
        self.out = io.StringIO()

    def __enter__(self):
        self.ctx = contextlib.redirect_stdout(self.out)
        self.ctx.__enter__()

    def __exit__(self, *exc):
        return self.ctx.__exit__(*exc)


def build_runner(code_body):
    """Wraps a cut block in a function; continue works because the block runs inside a one-pass loop."""
    code = ("def run(event, app_state, db, env):\n"
            "    globals().update(env)\n"
            "    for _once in [0]:\n" + textwrap.indent(code_body, "        ") + "\n    return None\n")
    namespace = {}
    exec(compile(code, "<block>", "exec"), namespace)
    return namespace["run"]


class n6_FakeDb:
    SERVER_SLOT_IDS = ["slot_a", "slot_b", "slot_c", "slot_d", "slot_e", "slot_f"]

    def __init__(self, **config):
        self.config = {"game_channel_enabled": True, "kiosk_mode_enabled": False, "server_role": None, "server_slots": {}, "active_server_slot": None}
        self.config.update(config)
        self.channels_db = {}

    def save_settings(self):
        pass


def make_env():
    def boom(*a, **k):
        raise AssertionError("an Enter-only function ran on a direction key")
    return {
        "pygame": pygame, "menu_nav": mn, "log": types.SimpleNamespace(info=boom, warning=boom),
        "get_server_picker_entries": lambda db: list(db.config.get("_picker", [])),
        "get_server_picker_selectable_count": ui.get_server_picker_selectable_count,
        "get_advanced_settings_menu_rows": ui.get_advanced_settings_menu_rows,
        "dvd_engine": None, "vlc_engine": None, "static_audio": None,
        "_disconnect_remote_server": boom, "_save_audio_track_pref": boom, "_fe_open": boom,
        "get_games_sub_menu_rows": boom, "_server_breadcrumb": boom, "server_sync": None, "logging": None,
    }


def run_block(runner, state, db, key):
    event = types.SimpleNamespace(key=key)
    work = dict(state)
    with Quiet():
        runner(event, work, db, make_env())
    work.pop("_nav_memory", None)
    return work


# ============================================================================
# SECTION: Cases
# ============================================================================


def tab_cases():
    for game_ch in (True, False):
        for kiosk in (True, False):
            for tab in ("System", "Channels", "Games", "Video", "Theme", "Bogus"):
                for layer in ("TAB_SELECTION",):
                    yield n6_FakeDb(game_channel_enabled=game_ch, kiosk_mode_enabled=kiosk), {"menu_layer": layer, "active_menu_tab": tab, "menu_selection_index": 7,
                                                                                          "games_tab_page": 1, "channels_tab_page": 1}


def server_menu_cases():
    for role in (None, "server", "user"):
        for sel in range(-1, 6):
            yield n6_FakeDb(server_role=role), {"menu_layer": "SERVER_MENU", "menu_selection_index": sel}


def picker_cases():
    for count in range(0, 5):
        for sel in range(-1, 8):
            yield n6_FakeDb(_picker=[{"name": "n%d" % i} for i in range(count)]), {"menu_layer": "SERVER_PICKER_MENU", "menu_selection_index": sel}


def slots_cases():
    for named in ({}, {"slot_a": "A"}, {"slot_a": "A", "slot_c": "C", "slot_f": "F"}, {sid: sid for sid in n6_FakeDb.SERVER_SLOT_IDS}):
        for sel in range(0, 9):
            yield n6_FakeDb(server_slots=dict(named)), {"menu_layer": "SERVER_SLOTS_MENU", "menu_selection_index": sel}


def actions_cases():
    for pending in (False, True):
        for sel in range(-1, 6):
            for target in (0, 2, 5):
                yield n6_FakeDb(server_slots={"slot_a": "A", "slot_c": "C", "slot_f": "F"}), {
                    "menu_layer": "SERVER_SLOT_ACTIONS", "menu_selection_index": sel, "server_slot_pending_delete": pending, "server_slot_name_target_row": target}


def remap_cases():
    for sel in range(-1, 6):
        yield n6_FakeDb(), {"menu_layer": "REMOTE_REMAP_LIST", "menu_selection_index": sel}


def audio_cases():
    for count in range(0, 5):
        for sel in range(0, count + 1):
            for context in ("channel", "dvd"):
                yield n6_FakeDb(), {"menu_layer": "AUDIO_TRACK_PICKER", "sub_menu_row_index": sel, "audio_track_list": [(i, "t%d" % i) for i in range(count)], "audio_track_context": context}


def holiday_cases():
    for sel in range(0, 6):
        yield n6_FakeDb(), {"menu_layer": "HOLIDAY_SUB_MENU", "sub_menu_row_index": sel, "selected_channel_row": 7}


def channel_sub_infos():
    for on in (True, False):
        for music in (False, True):
            for mode in ("random_slots", "marathon"):
                for block in ("full_day", "six_block", "three_block"):
                    for commercials in (False, True):
                        yield {"active": on, "is_visualizer": music, "scheduling_mode": mode, "block_mode": block, "commercials_enabled": commercials, "name": "X"}


def channel_sub_cases():
    for info in channel_sub_infos():
        for only in (True, False):
            for locked in (False, True):
                db = n6_FakeDb()
                db.channels_db = {str(n).zfill(2): {"active": False} for n in range(5, 45)}
                db.channels_db["07"] = dict(info, server_locked=locked)
                if not only:
                    db.channels_db["05"]["active"] = True
                layout = mn.channel_sub_layout(db.channels_db["07"], 1 if only else 2)
                for row in range(mn.channel_sub_close_row(layout) + 1):
                    for col in range(mn.CHANNEL_SUB_MAX_COL + 1):
                        yield db, {"menu_layer": "CHANNEL_SUB_MENU", "sub_menu_row_index": row, "sub_menu_col_index": col, "selected_channel_row": 7}


LAYER_CASES = (
    ("TAB_SELECTION", None, tab_cases),
    ("SERVER_MENU", "SERVER_MENU", server_menu_cases),
    ("SERVER_PICKER_MENU", "SERVER_PICKER_MENU", picker_cases),
    ("SERVER_SLOTS_MENU", "SERVER_SLOTS_MENU", slots_cases),
    ("SERVER_SLOT_ACTIONS", "SERVER_SLOT_ACTIONS", actions_cases),
    ("REMOTE_REMAP_LIST", "REMOTE_REMAP_LIST", remap_cases),
    ("AUDIO_TRACK_PICKER", "AUDIO_TRACK_PICKER", audio_cases),
    ("HOLIDAY_SUB_MENU", "HOLIDAY_SUB_MENU", holiday_cases),
    ("CHANNEL_SUB_MENU", "CHANNEL_SUB_MENU", channel_sub_cases),
)


BURNIN_TARGETS = ("visualizer_style_change", "console_bezel_toggle", "program_bezel_toggle", "pixel_orbiter_toggle", "ch03_screensaver_toggle")
PLAIN_TARGETS = ("reset_settings", "poweroff", "clear_roms", None)


def confirm_cases():
    for target in BURNIN_TARGETS + PLAIN_TARGETS:
        for selection in ("Yes", "No", None):
            for focus in ("buttons", "checkbox", None):
                for dont in (False, True):
                    state = {"show_clear_confirm": True, "clear_confirm_target": target, "clear_confirm_selection": selection, "clear_confirm_shown_at": 0,
                             "clear_confirm_dont_show_again": dont}
                    if focus is not None:
                        state["clear_confirm_focus"] = focus
                    yield n6_FakeDb(), state


def splash_cases():
    for page in range(0, 7):
        for focus in (0, 1):
            yield n6_FakeDb(), {"show_splash": True, "controls_splash_page": page, "controls_splash_focus": focus, "splash_shown_at": 5}


class n6_FakeDeck:
    def __init__(self, exit_confirm, return_confirm):
        self.mode = "DVD_PLAYER"
        self.dvd_exit_confirm = exit_confirm
        self.dvd_return_menu_confirm = return_confirm
        self.handled = []

    def handle_event(self, event):
        self.handled.append(event.key)


def gate_cases():
    for exit_confirm, return_confirm in ((True, False), (False, True), (True, True)):
        yield n6_FakeDb(), {"current_channel": "03", "_deck": (exit_confirm, return_confirm)}


def cut_between(text, begin_marker, end_marker):
    begin = text.index(begin_marker)
    line_start = text.rfind("\n", 0, begin) + 1
    end = text.index(end_marker, begin)
    return textwrap.dedent(text[line_start:end])


def build_runner_fell(code_body):
    """Like build_runner, but returns True when the block ended without a continue."""
    code = ("def run(event, app_state, db, env):\n"
            "    globals().update(env)\n"
            "    fell = False\n"
            "    for _once in [0]:\n" + textwrap.indent(code_body, "        ") + "\n        fell = True\n    return fell\n")
    namespace = {}
    exec(compile(code, "<block>", "exec"), namespace)
    return namespace["run"]


POPUP_BLOCKS = {
    "CLEAR_CONFIRM": ('            if app_state.get("show_clear_confirm", False):\n', '            if (app_state.get("current_channel") == "03"\n                    and \'game_deck\' in globals()'),
    "DVD_GATE": ('            if (app_state.get("current_channel") == "03"\n                    and \'game_deck\' in globals()', '            if app_state.get("current_channel") == "03" and not is_any_menu_showing'),
    "CONTROLS_SPLASH": ('            if app_state.get("show_splash", False):\n                NUM_CONTROL_PAGES', '            if app_state.get("show_quit_confirm", False):\n'),
}
POPUP_CASES = (("CLEAR_CONFIRM", confirm_cases), ("DVD_GATE", gate_cases), ("CONTROLS_SPLASH", splash_cases))
POPUP_KEYS = n6_ALL_KEYS + [pygame.K_RETURN, pygame.K_c]


def run_popup(runner, name, state, db, key):
    work = dict(state)
    deck = None
    env = make_env()
    env["now_ticks"] = 777
    if name == "DVD_GATE":
        deck = n6_FakeDeck(*work.pop("_deck"))
        env["game_deck"] = deck
    env["print"] = lambda *a, **k: None
    event = types.SimpleNamespace(key=key)
    with Quiet():
        fell = runner(event, work, db, env)
    work.pop("_nav_memory", None)
    return fell, work, (deck.handled if deck else None)


def popup_keys(name, state):
    """Keys to run: Enter only where its branch is a plain toggle (the box on a burn-in popup), the splash, or the gate."""
    keys = [k for k in POPUP_KEYS if k != pygame.K_RETURN]
    if name in ("CONTROLS_SPLASH", "DVD_GATE"):
        keys.append(pygame.K_RETURN)
    if name == "CLEAR_CONFIRM" and state.get("clear_confirm_target") in BURNIN_TARGETS and state.get("clear_confirm_focus") == "checkbox":
        keys.append(pygame.K_RETURN)
    return keys


def case_popup_blocks():
    new_text = read(MAIN_SCRIPT)
    n = 0
    for name, cases in POPUP_CASES:
        begin, end = POPUP_BLOCKS[name]
        new_runner = build_runner_fell(cut_between(new_text, begin, end))
        for db, state in cases():
            for key in popup_keys(name, state):
                got = run_popup(new_runner, name, state, db, key)
                n += 1
                check_popup(name, state, key, got)
    check(n > 1500, "popup cases ran: %d" % n)


def check_popup(name, state, key, got):
    fell, after, handled = got
    label = "%s %s %s" % (name, state, pygame.key.name(key))
    if name == "CLEAR_CONFIRM":
        check(fell is False, label + " is swallowed by the popup")
        focus = state.get("clear_confirm_focus", "buttons")
        burn = state["clear_confirm_target"] in BURNIN_TARGETS
        want = dict(state)
        if key == pygame.K_w:
            if burn and focus == "checkbox":
                want["clear_confirm_focus"] = "buttons"
            want["clear_confirm_shown_at"] = 777
        elif key == pygame.K_s:
            if burn and focus == "buttons":
                want["clear_confirm_focus"] = "checkbox"
            want["clear_confirm_shown_at"] = 777
        elif key in (pygame.K_a, pygame.K_d) and focus == "buttons":
            want["clear_confirm_selection"] = "Yes" if key == pygame.K_a else "No"
            want["clear_confirm_shown_at"] = 777
        elif key == pygame.K_RETURN:
            want["clear_confirm_dont_show_again"] = not state["clear_confirm_dont_show_again"]
            want["clear_confirm_shown_at"] = 777
        check(after == want, label + " got %r want %r" % (after, want))
    elif name == "DVD_GATE":
        routed = key in (pygame.K_a, pygame.K_d, pygame.K_RETURN)
        check(handled == ([key] if routed else []), label + " routing")
        check(fell == (not routed and key in (pygame.K_UP, pygame.K_DOWN)), label + " swallow or fall through")
    elif name == "CONTROLS_SPLASH":
        page, focus = state["controls_splash_page"], state["controls_splash_focus"]
        if key in (pygame.K_a, pygame.K_LEFT) and page != 0 and key != pygame.K_RETURN:
            check(after["controls_splash_focus"] == 0, label + " focuses Back")
        if key in (pygame.K_d, pygame.K_RIGHT) and page != 0:
            check(after["controls_splash_focus"] == 1, label + " focuses Next")
        if key in (pygame.K_a, pygame.K_d, pygame.K_LEFT, pygame.K_RIGHT) and page == 0:
            check(after == state, label + " on the first page does nothing")
        if key in (pygame.K_w, pygame.K_s, pygame.K_UP, pygame.K_DOWN, pygame.K_x):
            check(after == state, label + " does nothing")


def runner_for(text, name, layer):
    return build_runner(cut_tabs(text) if layer is None else "if True:\n" + textwrap.indent(cut_layer(text, layer), "    "))


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def case_blocks_against_tables():
    text = read(MAIN_SCRIPT)
    ran = 0
    for name, layer, cases in LAYER_CASES:
        runner = runner_for(text, name, layer)
        for db, state in cases():
            for key in DIRECTION_KEYS:
                after = run_block(runner, state, db, key)
                ran += 1
                check_block(name, db, state, key, after)
            for key in OTHER_KEYS:
                after = run_block(runner, state, db, key)
                ran += 1
                check(after == expected_untouched(name, db, state), "%s %s key %s changes nothing" % (name, state, pygame.key.name(key)))
    check(ran > 1500, "handler cases ran: %d" % ran)


def expected_untouched(name, db, state):
    """The state after a key the layer ignores; the tab bar still puts a tab the bar does not show back on System."""
    want = dict(state)
    if name == "TAB_SELECTION":
        if (not db.config["game_channel_enabled"] and state["active_menu_tab"] == "Games") or (db.config["kiosk_mode_enabled"] and state["active_menu_tab"] == "Channels"):
            want["active_menu_tab"] = "System"
    return want


def check_block(name, db, state, key, after):
    direction = {pygame.K_w: mn.UP, pygame.K_a: mn.LEFT, pygame.K_s: mn.DOWN, pygame.K_d: mn.RIGHT}[key]
    label = "%s %s %s" % (name, {k: v for k, v in state.items() if k != "audio_track_list"}, direction)
    want = dict(state)
    if name == "TAB_SELECTION":
        game_ch, kiosk = db.config["game_channel_enabled"], db.config["kiosk_mode_enabled"]
        pool = ["System", "Channels", "Games", "Video", "Theme"] if game_ch else ["System", "Channels", "Video", "Theme"]
        if kiosk:
            pool = [t for t in pool if t != "Channels"]
        tab = state["active_menu_tab"]
        if not game_ch and tab == "Games":
            tab = want["active_menu_tab"] = "System"
            want["menu_layer"] = "TAB_SELECTION"
        if kiosk and tab == "Channels":
            tab = want["active_menu_tab"] = "System"
            want["menu_layer"] = "TAB_SELECTION"
        index = pool.index(tab) if tab in pool else 0
        if direction == mn.RIGHT:
            want["active_menu_tab"] = pool[(index + 1) % len(pool)]
        elif direction == mn.LEFT:
            want["active_menu_tab"] = pool[(index - 1) % len(pool)]
        elif direction == mn.DOWN:
            layer = mn.TAB_PANEL_LAYERS.get(want["active_menu_tab"], "SYSTEM_ROWS")
            want["menu_layer"] = layer
            want["menu_selection_index"] = {"GAMES_LIST": min(1, mn.games_page_count(len(__import__("game_deck").CONSOLE_ORDER)) - 1) * mn.GAMES_PAGE_SIZE,
                                            "CHANNEL_LIST": min(1, mn.CHANNELS_NUM_PAGES - 1) * mn.CHANNELS_PAGE_SIZE}.get(layer, 0)
        check(after == want, label + " got %r want %r" % (after, want))
        return
    if name == "CHANNEL_SUB_MENU":
        row, col = state["sub_menu_row_index"], state["sub_menu_col_index"]
        layout = mn.channel_sub_layout(db.channels_db["07"], sum(1 for v in db.channels_db.values() if v.get("active")))
        close = mn.channel_sub_close_row(layout)
        new_row, new_col = after["sub_menu_row_index"], after["sub_menu_col_index"]
        check(0 <= new_row <= close and 0 <= new_col <= mn.CHANNEL_SUB_MAX_COL, label + " stays inside the grid")
        check(abs(new_row - row) <= 1 or (row == close and new_row == close - 1), label + " moves at most one row")
        if direction in (mn.LEFT, mn.RIGHT):
            check(new_row == row, label + " sideways keeps the row")
        check({k: v for k, v in after.items() if k not in ("sub_menu_row_index", "sub_menu_col_index")} == {k: v for k, v in state.items() if k not in ("sub_menu_row_index", "sub_menu_col_index")}, label + " changes only the cursor")
        return
    sel_key = "sub_menu_row_index" if name in ("AUDIO_TRACK_PICKER", "HOLIDAY_SUB_MENU") else "menu_selection_index"
    sel = state[sel_key]
    if name == "SERVER_MENU":
        extra = db.config["server_role"] in ("server", "user")
        count = 4 if extra else 3
        want[sel_key] = old_move_column(sel, count, direction)
    elif name == "SERVER_PICKER_MENU":
        want[sel_key] = old_move_column(sel, len(db.config["_picker"]) + 1, direction)
    elif name == "SERVER_SLOT_ACTIONS":
        want[sel_key] = old_move_column(sel, 4, direction)
        if want[sel_key] != sel:
            want["server_slot_pending_delete"] = False
    elif name == "AUDIO_TRACK_PICKER":
        want[sel_key] = old_move_column(sel, len(state["audio_track_list"]) + 1, direction)
    elif name == "HOLIDAY_SUB_MENU":
        want[sel_key] = old_move_column(sel, 6, direction)
    elif name == "REMOTE_REMAP_LIST":
        want[sel_key] = (sel + (-1 if direction in (mn.UP, mn.LEFT) else 1)) % 3
    elif name == "SERVER_SLOTS_MENU":
        column, row = sel // 3, sel % 3
        row = max(0, row - 1) if direction == mn.UP else min(2, row + 1) if direction == mn.DOWN else row
        column = max(0, column - 1) if direction == mn.LEFT else min(2, column + 1) if direction == mn.RIGHT else column
        want[sel_key] = column * 3 + row
    check(after == want, label + " got %r want %r" % (after, want))


def old_move_column(sel, count, direction):
    """The old W and S clamp written out; A and D did nothing."""
    if direction == mn.UP:
        return max(0, min(count - 1, sel - 1))
    if direction == mn.DOWN:
        return max(0, min(count - 1, sel + 1))
    return sel


class TestMenuNavTabsServerPopups(_SectionCase):
    """Tab bar, Server menu and slots, Remote Remap, audio picker, Holiday sub-menu, popups"""

    def test_tabs(self):
        self.run_section(case_tabs)

    def test_column_move(self):
        self.run_section(case_column_move)

    def test_ring_move(self):
        self.run_section(case_ring_move)

    def test_server_slots(self):
        self.run_section(case_server_slots)

    def test_tab_bar_exit_targets(self):
        self.run_section(case_tab_bar_exit_targets)

    def test_popup_tables(self):
        self.run_section(case_popup_tables)

    def test_gate_key_classes(self):
        self.run_section(case_gate_key_classes)

    def test_channel_sub_table(self):
        self.run_section(case_channel_sub_table)

    def test_blocks_against_tables(self):
        self.run_section(case_blocks_against_tables)

    def test_popup_blocks(self):
        self.run_section(case_popup_blocks)


# ============================================================================
# SECTION: Accept/back keys, sliders, held-key repeat, action keys, gamepad repeat rule
# ============================================================================


# ============================================================================
# SECTION: Pure functions
# ============================================================================


def case_actions():
    for key in list(range(0, 1200)) + [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN, pygame.K_ESCAPE]:
        want = {pygame.K_w: mn.UP, pygame.K_s: mn.DOWN, pygame.K_a: mn.LEFT, pygame.K_d: mn.RIGHT, pygame.K_RETURN: mn.ACCEPT, pygame.K_ESCAPE: mn.BACK}.get(key)
        check(mn.key_to_action(key) == want, "key_to_action %d" % key)
        check(mn.is_accept(key) == (key == pygame.K_RETURN) and mn.is_back(key) == (key == pygame.K_ESCAPE), "is_accept and is_back %d" % key)
        arrows = {pygame.K_UP: mn.UP, pygame.K_DOWN: mn.DOWN, pygame.K_LEFT: mn.LEFT, pygame.K_RIGHT: mn.RIGHT}.get(key, want)
        check(mn.key_to_action(key, arrows=True) == arrows, "key_to_action arrows %d" % key)


def old_slider(value, direction, low, high):
    return max(low, min(high, ((value + (5 if direction == mn.RIGHT else -5)) // 5) * 5))


def case_sliders():
    check(set(mn.SLIDER_SPECS) == set(mn.VIDEO_SLIDERS) | set(mn.THEME_SLIDERS), "slider specs cover the Video and Theme sliders")
    for cell, (key, default, low, high) in mn.SLIDER_SPECS.items():
        for value in list(range(-12, 405)):
            for direction in (mn.LEFT, mn.RIGHT):
                config = {key: value}
                changed = mn.slider_adjust(config, cell, direction)
                check(changed is True and config[key] == old_slider(value, direction, low, high), "slider %s %d %s" % (cell, value, direction))
        config = {}
        mn.slider_adjust(config, cell, mn.RIGHT)
        check(config[key] == old_slider(default, mn.RIGHT, low, high), "slider %s starts from its default" % cell)
        for direction in (mn.UP, mn.DOWN):
            config = {key: 40}
            check(mn.slider_adjust(config, cell, direction) is False and config == {key: 40}, "slider %s ignores %s" % (cell, direction))
    config = {"x": 1}
    check(mn.slider_adjust(config, "aspect_ratio", mn.RIGHT) is False and config == {"x": 1}, "a cell that is not a slider is left alone")
    check(mn.slider_adjust({}, mn.CLOSE, mn.LEFT) is False, "Close is not a slider")
    check(mn.SLIDER_SPECS["bg_hue"][1:] == (220, 0, 360) and mn.SLIDER_SPECS["ui_hue"][1:] == (140, 0, 360) and mn.SLIDER_SPECS["opacity"][1:] == (50, 0, 100), "slider ranges and defaults match the Theme and Video drawing")
    held = {mn.repeat_allowed("VIDEO_ROWS", key) for key in (pygame.K_a, pygame.K_d)}
    check(held == {True}, "held slider keys still repeat on Video sliders")


# ============================================================================
# SECTION: Handler blocks
# ============================================================================


class Dirty:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def run_slider_block(runner, state, config, key):
    db = n6_FakeDb()
    db.config.update(config)
    dirty = Dirty()
    env = make_env()
    env["_mark_settings_dirty"] = dirty
    work = dict(state)
    with Quiet():
        fell = runner(types.SimpleNamespace(key=key), work, db, env)
    work.pop("_nav_memory", None)
    return fell, work, dict(db.config), dirty.calls


VIDEO_CONFIGS = [{}, {"brightness": 0, "contrast": 100, "color": 97, "sharpness": 3, "tint": 55}, {"brightness": 100, "contrast": 0, "color": 2, "sharpness": 50, "tint": 99}]
THEME_CONFIGS = [{}, {"menu_opacity": 0, "theme_bg_hue": 360, "theme_ui_hue": 0}, {"menu_opacity": 100, "theme_bg_hue": 358, "theme_ui_hue": 3}, {"menu_opacity": 47, "theme_bg_hue": 221, "theme_ui_hue": 219}]


def slider_cases():
    for config in VIDEO_CONFIGS:
        for row in range(0, 11):
            yield "VIDEO_ROWS", {"menu_layer": "VIDEO_ROWS", "menu_selection_index": row}, config
    for config in THEME_CONFIGS:
        for row in range(0, 8):
            for column in (0, 1):
                yield "THEME_ROWS", {"menu_layer": "THEME_ROWS", "menu_selection_index": row, "theme_row2_col": column}, config


SLIDER_KEYS = n6_ALL_KEYS + [pygame.K_c]


def cut_last_layer(text, layer):
    """Returns the handler of the last menu layer, which ends where the event loop ends."""
    begin = text.index(LAYER % layer) + len(LAYER % layer)
    end = text.index("    _menu_nav_frame_end(_nav_frame_begin, events)", begin)
    return textwrap.dedent(text[begin:end])


def block_runner(text, layer):
    body = cut_last_layer(text, layer) if layer == "THEME_ROWS" else cut_layer(text, layer)
    return build_runner_fell("if True:\n" + textwrap.indent(body, "    "))


def case_slider_blocks():
    new_text = read(MAIN_SCRIPT)
    runners = {layer: block_runner(new_text, layer) for layer in ("VIDEO_ROWS", "THEME_ROWS")}
    n = 0
    for layer, state, config in slider_cases():
        new_runner = runners[layer]
        for key in SLIDER_KEYS:
            got = run_slider_block(new_runner, state, config, key)
            n += 1
            fell, after, config_after, dirty = got
            cell = mn.video_cell(state["menu_selection_index"]) if layer == "VIDEO_ROWS" else mn.theme_cell(state["menu_selection_index"], state.get("theme_row2_col", 0))
            is_slider = cell in mn.SLIDER_SPECS
            if key in (pygame.K_a, pygame.K_d):
                check(fell is False, "%s %s %s ends the handler" % (layer, state, pygame.key.name(key)))
                check(dirty == (1 if is_slider else 0), "%s %s %s marks settings dirty only on a slider" % (layer, state, pygame.key.name(key)))
                if is_slider:
                    config_key, default, low, high = mn.SLIDER_SPECS[cell]
                    direction = mn.LEFT if key == pygame.K_a else mn.RIGHT
                    check(config_after[config_key] == old_slider(config.get(config_key, default), direction, low, high), "%s %s %s value" % (layer, state, pygame.key.name(key)))
            else:
                check(dirty == 0 and config_after == dict(n6_FakeDb(**config).config), "%s %s %s changes no setting" % (layer, state, pygame.key.name(key)))
    check(n > 1000, "slider cases ran: %d" % n)


def case_no_raw_action_keys():
    text = read(MAIN_SCRIPT)
    for name in ("pygame.K_RETURN", "pygame.K_ESCAPE", "pygame.K_KP_ENTER"):
        check(name not in text, "the main script does not test %s itself" % name)


# ============================================================================
# SECTION: N7b held-key repeat, pure functions
# ============================================================================


def case_hold_functions():
    check(mn.HOLD_RULES["vertical"] == mn.HoldRule(None, 100, False), "vertical rule is the old 100 ms")
    check(mn.HOLD_RULES["horizontal"] == mn.HoldRule(None, 110, False), "horizontal rule is the old 110 ms")
    check(mn.HOLD_RULES["rom_list"] == mn.HoldRule(380, 70, True), "ROM list rule is the old 380 ms then 70 ms")
    check(mn.HOLD_RULES["volume"] == mn.HoldRule(200, 180, False), "volume rule is 200 ms then 180 ms")
    check(mn.HOLD_STUCK_MS == {"menu": 1500, "guide": 3000, "rom_list": 1500, "volume": 1500}, "stuck limits are menu 1500, guide 3000, ROM list 1500, volume 1500")
    arrows = [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN]
    for key in list(range(0, 1200)) + arrows:
        want = (mn.HOLD_RULES["vertical"] if key in (pygame.K_w, pygame.K_s)
                else mn.HOLD_RULES["horizontal"] if key in (pygame.K_a, pygame.K_d)
                else mn.HOLD_RULES["volume"] if key in (pygame.K_LEFT, pygame.K_RIGHT) else None)
        check(mn.hold_rule(key) == want, "hold_rule %d" % key)
    old_menu = lambda held, since, interval: held > 400 and since >= interval
    old_rom = lambda held, since: held >= 380 and since >= 70
    for held in range(0, 1000):
        for since in list(range(0, 130)) + [200, 500]:
            check(mn.hold_due(mn.HOLD_RULES["vertical"], held, since, 400) == old_menu(held, since, 100), "vertical due %d %d" % (held, since))
            check(mn.hold_due(mn.HOLD_RULES["horizontal"], held, since, 400) == old_menu(held, since, 110), "horizontal due %d %d" % (held, since))
            check(mn.hold_due(mn.HOLD_RULES["rom_list"], held, since) == old_rom(held, since), "ROM list due %d %d" % (held, since))
    check(mn.hold_due(mn.HOLD_RULES["vertical"], 301, 100, 300) is True and mn.hold_due(mn.HOLD_RULES["vertical"], 300, 100, 300) is False, "the menu delay is the caller's value")
    for pressed in (True, False):
        for held in (0, 1499, 1500, 1501, 2999, 3000, 3001, 9000):
            for released in (True, False):
                for limit in (1500, 3000):
                    check(mn.hold_stuck(pressed, held, released, limit) is (not pressed and not released and held > limit), "hold_stuck %s %d %s %d" % (pressed, held, released, limit))
    for first in (True, False):
        for second in (True, False):
            for ticks in ((0, 0), (1, 2), (2, 1), (5, 5)):
                if first and not second:
                    want = (mn.DROP_SECOND, mn.DROP_SECOND)
                elif second and not first:
                    want = (mn.DROP_FIRST, mn.DROP_FIRST)
                elif first and second:
                    want = (mn.DROP_FIRST if ticks[0] <= ticks[1] else mn.DROP_SECOND, mn.DROP_BOTH)
                else:
                    want = (mn.DROP_BOTH, mn.DROP_BOTH)
                check(mn.hold_conflict(first, second, *ticks) == want[0], "hold_conflict older %s %s %s" % (first, second, ticks))
                check(mn.hold_conflict(first, second, *ticks, both_pressed=mn.DROP_BOTH) == want[1], "hold_conflict both %s %s %s" % (first, second, ticks))
    check(mn.hold_pick({pygame.K_s, pygame.K_d}, (pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d)) == pygame.K_s, "hold_pick takes the first key of the order")
    check(mn.hold_pick(set(), (pygame.K_w,)) is None and mn.hold_pick({pygame.K_x}, (pygame.K_w,)) is None, "hold_pick finds nothing")
    up = pygame.event.Event(pygame.KEYUP, key=pygame.K_d)
    down = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d)
    check(mn.hold_released([up], pygame.K_d) and not mn.hold_released([down], pygame.K_d) and not mn.hold_released([], pygame.K_d), "hold_released sees a key-up of the key only")
    check(not mn.hold_released([up], pygame.K_a), "hold_released ignores another key")
    check(mn.hold_released([pygame.event.Event(pygame.KEYUP, key=999)], pygame.K_d, {999: pygame.K_d}), "hold_released follows the remap source")
    state = mn.HoldState(pygame.K_d, 5, 9)
    state.clear()
    check((state.key, state.start, state.last) == (None, 0, 0), "HoldState.clear")
    rule = mn.HOLD_RULES["horizontal"]
    state = mn.HoldState()
    steps = [(1000, mn.HOLD_STARTED), (1400, mn.HOLD_WAIT), (1401, mn.HOLD_REPEAT), (1500, mn.HOLD_WAIT), (1511, mn.HOLD_REPEAT), (1621, mn.HOLD_REPEAT)]
    for now, want in steps:
        check(mn.hold_step(rule, state, pygame.K_d, now, True, False, 1500, 400) == want, "hold_step at %d" % now)
    check(mn.hold_step(rule, state, pygame.K_a, 1700, True, False, 1500, 400) == mn.HOLD_STARTED and state.key == pygame.K_a and state.start == 1700, "a new key restarts the hold")
    check(mn.hold_step(rule, state, pygame.K_a, 3300, False, False, 1500, 400) == mn.HOLD_STUCK and state.key is None, "a key that is not down and not released is stuck and clears the hold")
    state = mn.HoldState()
    mn.hold_step(rule, state, pygame.K_a, 0, True, False, 1500, 400)
    check(mn.hold_step(rule, state, pygame.K_a, 3300, False, True, 1500, 400) != mn.HOLD_STUCK, "a key released in this frame is not stuck")


# ============================================================================
# SECTION: N7b held-key repeat, the real blocks
# ============================================================================

INJECTOR_HEAD = '        _mnav_held_set = app_state["_held_keys"]\n'
INJECTOR_TAIL = '    _fgap_last = app_state.get("_frame_gap_last_ticks")'
GUIDE_HEAD = '        for _gh_a, _gh_b in ((pygame.K_w, pygame.K_s), (pygame.K_a, pygame.K_d)):\n            if _gh_a in app_state["_held_keys"]'
GUIDE_TAIL = '    _prof_mark("idle_input")'
WASD = (pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d)
FRAMES = 60


class Recorder:
    def __init__(self):
        self.lines = []

    def warning(self, text, *args):
        self.lines.append(text % args if args else text)

    info = warning


def cut(text, head, tail):
    begin = text.index(head)
    return textwrap.dedent(text[begin:text.index(tail, begin)])


def function_source(text, name):
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return textwrap.dedent(" " * node.col_offset + ast.get_source_segment(text, node))
    raise KeyError(name)


def kind_of(lines):
    return sorted("both" if "saw both" in line else "stuck" for line in lines)


def key_up_mimic(state, events):
    for event in events:
        if event.type == pygame.KEYUP:
            key = state["_held_keys_source"].pop(event.key, event.key)
            state["_held_keys"].discard(key)
            state["_held_keys"].discard(event.key)


def timeline(held, mode, release_frame, step_ms):
    """Yields (now, matrix, keyup keys) per frame: the keys stay down, are released with a key-up at release_frame, or stop being down at release_frame with the key-up swallowed."""
    for frame in range(FRAMES):
        now = 1000 + frame * step_ms
        gone = mode != "down" and frame >= release_frame
        matrix = collections.defaultdict(bool, {key: (not gone) for key in held})
        ups = list(held) if (mode == "released" and frame == release_frame) else []
        yield frame, now, matrix, ups


def injector_runner(text):
    block = cut(text, INJECTOR_HEAD, INJECTOR_TAIL)
    code = ("def run(app_state, _mnav_held_set, _mnav_matrix, events, now_ticks, pygame, log, _menu_nav_trace, _MENU_NAV_REPEAT_DELAY_MS):\n"
            "    if True:\n" + textwrap.indent(block, "        "))
    ns = {"menu_nav": mn}
    exec(code, ns)
    return ns["run"]


def injector_state(new, layer, selection, held, down_layer, down_ticks):
    state = {"menu_layer": layer, "menu_selection_index": selection, "_held_keys": set(held), "_held_keys_source": {},
             "_held_key_down_layer": {key: down_layer for key in held}, "_held_key_down_ticks": dict(down_ticks)}
    if new:
        state["_hold_menu"] = mn.HoldState()
    else:
        state.update({"_menu_nav_hold_key": None, "_menu_nav_hold_start": 0, "_menu_nav_last_repeat": 0})
    return state


def injector_hold(state):
    if "_hold_menu" in state:
        hold = state["_hold_menu"]
        return (hold.key, hold.start, hold.last) if hold.key is not None else (None,)
    key = state["_menu_nav_hold_key"]
    return (key, state["_menu_nav_hold_start"], state["_menu_nav_last_repeat"]) if key is not None else (None,)


def run_injector(runner, new, layer, selection, held, down_layer, down_ticks, mode, release_frame, step_ms):
    state = injector_state(new, layer, selection, held, down_layer, down_ticks)
    log = Recorder()
    trace = []
    frames = []
    for frame, now, matrix, ups in timeline(held, mode, release_frame, step_ms):
        events = [pygame.event.Event(pygame.KEYUP, key=key) for key in ups]
        before = len(events)
        runner(state, state["_held_keys"], matrix, events, now, pygame, log, lambda *a, **k: trace.append(a[:2]), 400)
        injected = [(event.key, getattr(event, "menu_repeat", False)) for event in events[before:]]
        lines, log.lines = list(log.lines), []
        key_up_mimic(state, events)
        frames.append((injected, sorted(state["_held_keys"]), injector_hold(state), kind_of(lines), bool(ups), len(trace)))
    return frames


def injector_expectations():
    """The false warning: a key held a long time and then released does not log a stuck key, but a key that stops being down without a key-up still does."""
    runner = injector_runner(read(MAIN_SCRIPT))
    for key in WASD:
        for mode, expect in (("released", False), ("swallowed", True)):
            frames = run_injector(runner, True, "VIDEO_ROWS", 5, (key,), "VIDEO_ROWS", {}, mode, 40, 50)
            warned = any("stuck" in frame[3] for frame in frames)
            check(warned is expect, "injector %s %s: stuck warning %s" % (pygame.key.name(key), mode, expect))
    frames = run_injector(runner, True, "VIDEO_ROWS", 5, (pygame.K_d,), "VIDEO_ROWS", {}, "down", 0, 16)
    check(sum(len(frame[0]) for frame in frames) >= 5, "a held D on a Video slider still repeats")
    frames = run_injector(runner, True, "VIDEO_ROWS", 5, (pygame.K_a, pygame.K_d), "VIDEO_ROWS", {pygame.K_a: 1, pygame.K_d: 2}, "released", 5, 16)
    check(not any("both" in frame[3] for frame in frames), "two keys let go in the same frame do not log a stuck pair")
    frames = run_injector(runner, True, "VIDEO_ROWS", 5, (pygame.K_a, pygame.K_d), "VIDEO_ROWS", {pygame.K_a: 1, pygame.K_d: 2}, "swallowed", 0, 16)
    check(any("both" in frame[3] for frame in frames), "two keys that vanish without a key-up still log a stuck pair")


GUIDE_CHANNELS = {"active_all": {}, "some_off": {"06": False, "09": False, "12": False, "44": False}, "two": {str(i).zfill(2): (i in (5, 20)) for i in range(5, 45)}}


def guide_namespace(text, new):
    ns = {"pygame": pygame, "menu_nav": mn, "_MENU_NAV_REPEAT_DELAY_MS": 400}
    ns["_key_is_held"] = lambda key: key in ns["app_state"]["_held_keys"]
    ns["_guide_dirty_event"] = types.SimpleNamespace(set=lambda: ns["calls"].append(("dirty",)))
    ns["get_guide_visible_rows"] = lambda db: ns["visible_rows"]
    ns["_clear_last_video_frame"] = lambda **kw: ns["calls"].append(("clear", kw.get("context")))
    ns["_arm_guide_preview_transition"] = lambda: ns["calls"].append(("arm",))
    ns["_resync_guide_stop_after_row_switch"] = lambda: ns["calls"].append(("resync",))
    ns["_guide_jump_col"] = lambda direction, _source="unknown": ns["calls"].append(("jump", direction, _source))
    if new:
        exec(function_source(text, "_guide_step_row"), ns)
    block = cut(text, GUIDE_HEAD, GUIDE_TAIL)
    exec("def run():\n    if True:\n" + textwrap.indent(block, "        "), ns)
    return ns


def guide_state(new, selected, offset, held, ticks):
    state = {"_held_keys": set(held), "_held_keys_source": {}, "_held_key_down_ticks": dict(ticks), "selected_guide_channel": selected,
             "highlighted_guide_channel": str(selected).zfill(2), "guide_row_offset": offset, "guide_inactivity_timer": 0}
    if new:
        state["_hold_guide"] = mn.HoldState()
    else:
        state.update({"_gh_held_key": None, "_gh_hold_start": 0, "_gh_last_repeat": 0})
    return state


def guide_hold(state):
    if "_hold_guide" in state:
        hold = state["_hold_guide"]
        return (hold.key, hold.start, hold.last) if hold.key is not None else (None,)
    key = state["_gh_held_key"]
    return (key, state["_gh_hold_start"], state["_gh_last_repeat"]) if key is not None else (None,)


def n7_run_guide(ns, new, channels, selected, offset, rows, held, ticks, mode, release_frame, step_ms):
    state = guide_state(new, selected, offset, held, ticks)
    ns["app_state"] = state
    ns["db"] = types.SimpleNamespace(channels_db={str(i).zfill(2): ({"active": channels[str(i).zfill(2)]} if str(i).zfill(2) in channels else {}) for i in range(5, 45)})
    ns["visible_rows"] = rows
    log = Recorder()
    ns["log"] = log
    frames = []
    for frame, now, matrix, ups in timeline(held, mode, release_frame, step_ms):
        ns["calls"] = []
        ns["now_ticks_ms"] = now
        ns["pressed_keys_matrix"] = matrix
        ns["run"]()
        lines, log.lines = list(log.lines), []
        for key in ups:
            state["_held_keys"].discard(key)
        frames.append((list(ns["calls"]), state["selected_guide_channel"], state["highlighted_guide_channel"], state["guide_row_offset"],
                       state["guide_inactivity_timer"], sorted(state["_held_keys"]), guide_hold(state), kind_of(lines)))
    return frames


def case_hold_blocks():
    new_text = read(MAIN_SCRIPT)
    injector_expectations()
    guide_expectations(new_text)


def guide_expectations(new_text):
    ns = guide_namespace(new_text, True)
    channels = GUIDE_CHANNELS["active_all"]
    frames = n7_run_guide(ns, True, channels, 5, 0, 5, (pygame.K_s,), {}, "down", 0, 16)
    check(any(("resync",) in frame[0] for frame in frames) is False, "a held row repeat does not re-aim the time stop, as before")
    check(any(frame[1] == 6 for frame in frames), "a held S moves the guide down")
    ns["calls"] = []
    ns["app_state"] = guide_state(True, 5, 0, (), {})
    ns["db"] = types.SimpleNamespace(channels_db={})
    ns["visible_rows"] = 5
    ns["_guide_step_row"](1, True)
    check(("resync",) in ns["calls"] and ns["app_state"]["selected_guide_channel"] == 6, "one key press moves a row and re-aims the time stop")
    ns["calls"] = []
    ns["db"] = None
    ns["_guide_step_row"](1, True)
    check(ns["calls"] == [], "no database, no row move")
    frames = n7_run_guide(ns, True, channels, 5, 0, 5, (pygame.K_s,), {}, "swallowed", 3, 250)
    check(sum("stuck" in frame[7] for frame in frames) == 1, "a stuck guide key logs once")


# ============================================================================
# SECTION: N7c action keys and game_deck
# ============================================================================


def case_action_keys():
    arrows_of = {mn.UP: pygame.K_UP, mn.DOWN: pygame.K_DOWN, mn.LEFT: pygame.K_LEFT, mn.RIGHT: pygame.K_RIGHT}
    first = {mn.UP: pygame.K_w, mn.DOWN: pygame.K_s, mn.LEFT: pygame.K_a, mn.RIGHT: pygame.K_d, mn.ACCEPT: pygame.K_RETURN, mn.BACK: pygame.K_ESCAPE}
    for action in (mn.UP, mn.DOWN, mn.LEFT, mn.RIGHT, mn.ACCEPT, mn.BACK):
        check(mn.action_keys(action) == (first[action],), "action_keys %s" % action)
        want = (first[action], arrows_of[action]) if action in arrows_of else (first[action],)
        check(mn.action_keys(action, arrows=True) == want, "action_keys arrows %s" % action)
        check(mn.action_key(action) == first[action], "action_key %s" % action)
        check(mn.key_to_action(mn.action_key(action)) == action, "action_key round trip %s" % action)
    for key in list(range(0, 1200)) + [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT]:
        for arrows in (False, True):
            action = mn.key_to_action(key, arrows)
            check((action is not None) == any(key in mn.action_keys(a, arrows) for a in (mn.UP, mn.DOWN, mn.LEFT, mn.RIGHT, mn.ACCEPT, mn.BACK)), "action_keys cover key %d %s" % (key, arrows))
            direction = mn.key_to_direction(key, arrows)
            check(direction == (action if action in mn.DIRECTIONS else None), "key_to_direction agrees with key_to_action %d %s" % (key, arrows))
    every = [pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d, pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_x]
    for mask in range(1 << len(every)):
        matrix = collections.defaultdict(bool)
        down = [key for index, key in enumerate(every) if mask >> index & 1]
        for key in down:
            matrix[key] = True
        for arrows in (False, True):
            want = {mn.key_to_action(key, arrows) for key in down} - {None}
            check(mn.pressed_actions(matrix, arrows) == want, "pressed_actions %d %s" % (mask, arrows))
    check(mn.HOLD_RULES["gamepad"] == mn.HoldRule(400, 180, True), "gamepad rule is 400 ms then 180 ms, inclusive")


def old_pad_due(held_s, since_s):
    return held_s >= 400 / 1000 and since_s >= 180 / 1000


def case_pad_rule_numbers():
    rule = mn.HOLD_RULES["gamepad"]
    steps = [i / 1000 for i in range(0, 1200)]
    differ = 0
    for held in steps[::3]:
        for since in steps[::3]:
            if mn.hold_due(rule, held * 1000.0, since * 1000.0) != old_pad_due(held, since):
                differ += 1
    check(differ == 0, "gamepad hold_due against the old seconds test over %d pairs: %d differ" % (len(steps[::3]) ** 2, differ))


def case_no_raw_deck_keys():
    deck = read(os.path.join(ROOT, "game_deck.py"))
    raw = [line.strip() for line in deck.splitlines() if "pygame.K_" in line]
    check(raw == [], "game_deck has no raw pygame key left (Backspace moved to menu_nav in v94): %s" % raw)
    check('_nav.HOLD_RULES["gamepad"]' in deck, "the gamepad worker takes its numbers from menu_nav")


class TestMenuNavKeysSlidersRepeat(_SectionCase):
    """Accept/back keys, sliders, held-key repeat, action keys, gamepad repeat rule"""

    def test_actions(self):
        self.run_section(case_actions)

    def test_sliders(self):
        self.run_section(case_sliders)

    def test_slider_blocks(self):
        self.run_section(case_slider_blocks)

    def test_no_raw_action_keys(self):
        self.run_section(case_no_raw_action_keys)

    def test_hold_functions(self):
        self.run_section(case_hold_functions)

    def test_hold_blocks(self):
        self.run_section(case_hold_blocks)

    def test_action_keys(self):
        self.run_section(case_action_keys)

    def test_pad_rule_numbers(self):
        self.run_section(case_pad_rule_numbers)

    def test_no_raw_deck_keys(self):
        self.run_section(case_no_raw_deck_keys)


# ============================================================================
# SECTION: File explorer: Backspace as parent, wheel, scrollbar, row selection, double click, drag select
# ============================================================================


# ============================================================================
# SECTION: Pure functions
# ============================================================================


def case_keys():
    wanted = {pygame.K_w: mn.UP, pygame.K_s: mn.DOWN, pygame.K_a: mn.LEFT, pygame.K_d: mn.RIGHT, pygame.K_RETURN: mn.ACCEPT, pygame.K_ESCAPE: mn.BACK}
    arrows = {pygame.K_UP: mn.UP, pygame.K_DOWN: mn.DOWN, pygame.K_LEFT: mn.LEFT, pygame.K_RIGHT: mn.RIGHT}
    for key in list(range(0, 1200)) + list(arrows):
        plain = wanted.get(key)
        check(mn.key_to_action(key) == plain, "default key_to_action %d is unchanged" % key)
        check(mn.key_to_action(key, arrows=True) == arrows.get(key, plain), "arrows key_to_action %d is unchanged" % key)
        parent = mn.PARENT if key == pygame.K_BACKSPACE else plain
        check(mn.key_to_action(key, parent=True) == parent, "parent key_to_action %d" % key)
        check(mn.key_to_action(key, arrows=True, parent=True) == (mn.PARENT if key == pygame.K_BACKSPACE else arrows.get(key, plain)), "arrows and parent key_to_action %d" % key)
        check(not mn.is_accept(key) or key == pygame.K_RETURN, "Backspace is not accept %d" % key)
        check(not mn.is_back(key) or key == pygame.K_ESCAPE, "Backspace is not back %d" % key)
    check(mn.PARENT not in mn.DIRECTIONS and mn.PARENT not in (mn.ACCEPT, mn.BACK), "PARENT is its own action")
    check(mn.key_to_direction(pygame.K_BACKSPACE) is None, "Backspace is no direction")
    check(pygame.K_BACKSPACE not in mn.action_keys(mn.BACK) and pygame.K_BACKSPACE not in mn.action_keys(mn.ACCEPT), "action_keys leaves Backspace out")
    down = collections.defaultdict(bool)
    down[pygame.K_BACKSPACE] = True
    check(mn.pressed_actions(down) == set() and mn.pressed_actions(down, arrows=True) == set(), "pressed_actions leaves Backspace out")


def case_wheel():
    for count in range(0, 41):
        for visible in range(1, 16):
            for scroll in range(0, count + 7):
                for notches in range(-4, 5):
                    old = max(0, min(max(0, count - visible), scroll - notches * 3))
                    check(mn.explorer_wheel(scroll, count, visible, notches) == old, "wheel %r" % ((count, visible, scroll, notches),))
                old_up = max(0, min(max(0, count - visible), scroll - 3))
                old_down = max(0, min(max(0, count - visible), scroll + 3))
                check(mn.explorer_wheel_button(4, scroll, count, visible) == old_up, "wheel button 4 %r" % ((count, visible, scroll),))
                check(mn.explorer_wheel_button(5, scroll, count, visible) == old_down, "wheel button 5 %r" % ((count, visible, scroll),))
    for button in range(0, 12):
        if button not in (4, 5):
            check(mn.explorer_wheel_button(button, 5, 50, 5) is None, "button %d is no wheel button" % button)


def case_select_and_clicks():
    rows = 10
    for index in range(-3, rows + 3):
        for anchor in (0, 3, 9):
            for selection in (set(), {2}, {2, 3, 4}, {index}):
                for shift in (False, True):
                    for ctrl in (False, True):
                        got = mn.explorer_select(index, rows, selection, anchor, shift, ctrl)
                        if 0 <= index < rows:
                            if shift:
                                want = (set(range(min(anchor, index), max(anchor, index) + 1)), anchor)
                            elif ctrl:
                                want = (set(selection) ^ {index}, index)
                            else:
                                want = ({index}, index)
                        else:
                            want = (selection if (shift or ctrl) else set(), anchor)
                        check(got == want, "select %r" % ((index, anchor, sorted(selection), shift, ctrl),))
    selection = {1, 2}
    mn.explorer_select(1, rows, selection, 0, False, True)
    check(selection == {1, 2}, "select does not change the set it is given")
    for gap in range(0, 1200):
        check(mn.explorer_double_click(10000 + gap, 10000, 4, 4) == (gap < 500), "double click gap %d" % gap)
        check(not mn.explorer_double_click(10000 + gap, 10000, 4, 5), "double click on another row %d" % gap)
    check(not mn.explorer_double_click(100, 0, -1, 4), "no earlier click is no double click")
    shift, ctrl = mn.explorer_modifiers(0)
    check((shift, ctrl) == (False, False), "no modifiers")
    for mods in range(0, 1 << 14):
        shift, ctrl = mn.explorer_modifiers(mods)
        check(shift == bool(mods & pygame.KMOD_SHIFT) and ctrl == bool(mods & pygame.KMOD_CTRL), "modifiers %d" % mods)


def case_rows():
    for y in range(-5, 300):
        for scroll in (0, 1, 7):
            want = -1 if (y < 100 or y >= 240) else int(scroll + (y - 100) // 34)
            check(mn.explorer_row_at(y, 100, 240, 34, scroll) == want, "row_at %r" % ((y, scroll),))
            check(mn.explorer_row_at(y + 0.5, 100, 240, 34, scroll) == (-1 if (y + 0.5 < 100 or y + 0.5 >= 240) else int(scroll + (y + 0.5 - 100) // 34)), "row_at float %r" % ((y, scroll),))
    for count in (0, 1, 4, 5, 9, 30):
        for scroll in (-3, 0, 2, 6, 40):
            for y in range(95, 100 + 5 * 40 + 6):
                got = mn.explorer_list_pick(y, 100, 5, 40, count, scroll)
                if not (100 <= y < 300):
                    want = None
                else:
                    held = max(0, min(max(0, count - 5), scroll))
                    index = held + int((y - 100) // 40)
                    want = index if 0 <= index < count else -1
                check(got == want, "list_pick %r" % ((count, scroll, y),))
    for x in range(0, 120):
        check(mn.explorer_in_remove_zone(x, 100, 24) == (x >= 76), "remove zone %d" % x)
    check(mn.explorer_visible_rows(0, 34) == 1 and mn.explorer_visible_rows(33, 34) == 1 and mn.explorer_visible_rows(68, 34) == 2 and mn.explorer_visible_rows(700, 34) == 20, "visible rows")
    rect = pygame.Rect(10, 20, 30, 40)
    for x in (9, 9.9, 10, 39, 39.9, 40, -0.5):
        for y in (19, 20, 59, 59.5, 60):
            check(mn.explorer_hit(rect, x, y) == rect.collidepoint(x, y), "hit %r" % ((x, y),))
    buttons = [(pygame.Rect(0, 0, 10, 10), "a"), (pygame.Rect(5, 5, 10, 10), "b"), (pygame.Rect(20, 0, 5, 5), "c")]
    check(mn.explorer_button_at(buttons, 7, 7) == "a" and mn.explorer_button_at(buttons, 12, 12) == "b" and mn.explorer_button_at(buttons, 21, 1) == "c" and mn.explorer_button_at(buttons, 30, 30) is None, "button_at picks the first match")
    check(mn.explorer_in_band(100, 100, 40) and not mn.explorer_in_band(140, 100, 40) and not mn.explorer_in_band(99, 100, 40), "in_band")
    check(mn.explorer_drag_moved((10, 10), 14, 10) is False and mn.explorer_drag_moved((10, 10), 15, 10) and mn.explorer_drag_moved((10, 10), 10, 5) and not mn.explorer_drag_moved((10, 10), 6, 14), "drag moved needs more than 4 pixels")


def case_drag_steps():
    for count in (0, 1, 5, 12, 40):
        for visible in (1, 5, 8):
            for scroll in range(0, max(1, count) + 2):
                for anchor in (0, 2, 4, 11):
                    for y in (50, 60, 99, 100, 101, 133, 134, 135, 269, 270, 271, 300, 320, 321, 400):
                        for wait in (0, 59, 60, 61, 119, 120, 200):
                            got = mn.explorer_drag_step(y, 100, 270, 34, count, visible, scroll, anchor, 1000, 1000 + wait)
                            max_scroll = max(0, count - visible)
                            delta = 0
                            speed = 120
                            if y < 100:
                                speed = max(60, 120 - int(60 * min(100 - y, 50) / 50))
                                delta = -1
                            elif y > 270:
                                speed = max(60, 120 - int(60 * min(y - 270, 50) / 50))
                                delta = 1
                            stamp = 1000
                            sc = scroll
                            if delta and wait >= speed:
                                sc = max(0, min(max_scroll, sc + delta))
                                stamp = 1000 + wait
                            if y <= 100:
                                index = sc
                            elif y >= 270:
                                index = sc + visible - 1
                            else:
                                index = sc + int((y - 100) // 34)
                            index = max(0, min(count - 1, index))
                            want = (sc, set(range(min(anchor, index), max(anchor, index) + 1)), stamp)
                            check(got == want, "drag step %r" % ((count, visible, scroll, anchor, y, wait),))


def case_scrollbar_press():
    for count in (0, 1, 14, 50, 300):
        for visible in (1, 8, 20):
            for y in range(112, 117 + 400 + 6, 3):
                for x in (895, 899, 900, 904, 907, 908, 910, 911.5):
                    for scroll in sorted({0, 1, max(0, count - visible) // 2, max(0, count - visible)}):
                        got = mn.explorer_scrollbar_press(x, y, 900, 117, 400, count, visible, scroll)
                        thumb_y, thumb_h = mn.explorer_thumb(117, 400, count, visible, scroll)
                        thumb = pygame.Rect(900, thumb_y, 8, thumb_h)
                        if thumb.collidepoint(x, y):
                            want = (None, y - thumb.y)
                        else:
                            grab = thumb.h // 2
                            new_ty = max(117, min(117 + 400 - thumb.h, y - grab))
                            want = (mn.explorer_scroll_from_thumb(new_ty, 117, 400, count, visible), grab)
                        check(got == want, "scrollbar press %r" % ((count, visible, x, y, scroll),))
    track = mn.explorer_scrollbar_track(900, 117, 400)
    check((track.x, track.y, track.w, track.h) == (896, 117, 16, 400), "scrollbar track is the thumb column plus the pad")


# ============================================================================
# SECTION: Static checks
# ============================================================================


def case_static():
    text = read(MAIN_SCRIPT)
    handler = function_source(text, "_fe_handle_event")
    for needle in ("collidepoint", "K_BACKSPACE", "KMOD_", "_ZONE", "< 500", "* 3", "scroll - 3", "scroll + 3", "_SB_PAD", "idx_at", "max(1, items_h"):
        check(needle not in handler, "the explorer handler no longer holds %r itself" % needle)
    check("_fe_scroll_from_thumb_y" not in text, "the old scroll-from-thumb helper is removed")
    thumb = function_source(text, "_fe_scrollbar_thumb_rect")
    check("max(20" not in thumb, "the thumb rectangle helper holds no sizes of its own")
    nav_text = read(os.path.join(os.path.dirname(HERE), "assets", "menu_nav.py"))
    section = nav_text[nav_text.index("# SECTION: File explorer"):nav_text.index("# SECTION: Exports")]
    check("# VALUES CHANGE HERE" in section, "the explorer numbers carry the VALUES CHANGE HERE tag")
    for name in ("EXPLORER_WHEEL_ROWS", "EXPLORER_DOUBLE_CLICK_MS", "EXPLORER_DRAG_START_PX", "EXPLORER_DRAG_EDGE_ZONE_PX", "EXPLORER_THUMB_MIN_H"):
        check(name in section and ('"%s"' % name) in nav_text, "%s is defined and exported" % name)
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if "pygame.K_BACKSPACE" in line:
            around = "\n".join(lines[number:number + 4])
            check("[:-1]" in around or "skip_current_remap_button" in around, "the Backspace on line %d deletes a typed character or skips a remap button" % (number + 1))
    check(sum("pygame.K_BACKSPACE" in line or "pygame.K_SPACE" in line for line in lines) == 0, "no raw Backspace or Space test remains in the main script (v95 moved them to menu_nav)")
    check(all(name in mn.__all__ for name in dir(mn) if name.startswith("explorer_")), "every explorer function is exported")


class TestMenuNavFileExplorer(_SectionCase):
    """File explorer: Backspace as parent, wheel, scrollbar, row selection, double click, drag select"""

    def test_keys(self):
        self.run_section(case_keys)

    def test_wheel(self):
        self.run_section(case_wheel)

    def test_select_and_clicks(self):
        self.run_section(case_select_and_clicks)

    def test_rows(self):
        self.run_section(case_rows)

    def test_drag_steps(self):
        self.run_section(case_drag_steps)

    def test_scrollbar_press(self):
        self.run_section(case_scrollbar_press)

    def test_static(self):
        self.run_section(case_static)


# ============================================================================
# SECTION: Explorer wheel clamp, joystick actions, channel number entry, DVD controls, guide hold re-aim
# ============================================================================


def n8_run_guide_hold(text, frames):
    """Runs the real held-repeat block frame by frame on a simulated clock; frames is a list of (now_ms, held keys)."""
    log_lines = []
    calls = []
    app_state = {"_hold_guide": mn.HoldState(), "_held_keys": set(), "guide_inactivity_timer": 0}
    ns = {
        "pygame": pygame, "menu_nav": mn, "app_state": app_state, "log": types.SimpleNamespace(warning=lambda *a: log_lines.append(a)),
        "_guide_dirty_event": types.SimpleNamespace(set=lambda: calls.append(("dirty",))),
        "_guide_jump_col": lambda step, _source: calls.append(("col", step, _source)),
        "_guide_step_row": lambda step, resync: calls.append(("row", step, resync)),
        "_resync_guide_stop_after_row_switch": lambda: calls.append(("resync",)),
        "_MENU_NAV_REPEAT_DELAY_MS": 400,
    }
    body = textwrap.dedent(text)
    src = "def frame(now_ticks_ms, held, pressed_keys_matrix):\n    app_state['_held_keys'] = set(held)\n    _key_is_held = lambda k: k in app_state['_held_keys']\n" + textwrap.indent(body, "    ")
    exec(src, ns)
    out = []
    for now_ms, held in frames:
        calls.clear()
        matrix = {k: True for k in held}
        ns["frame"](now_ms, held, types.SimpleNamespace(__getitem__=None) if False else n8_Matrix(matrix))
        out.append(tuple(calls))
    return out, app_state


class n8_Matrix:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data.get(key, False)


def n8_case_wheel_clamp():
    """Every wheel path clamps at the end of the list."""
    for count in range(0, 40):
        for visible in range(1, 14):
            for scroll in range(0, 45):
                top = max(0, count - visible)
                for notches in (-3, -2, -1, 1, 2, 3):
                    got = mn.explorer_wheel(scroll, count, visible, notches)
                    want = max(0, min(top, scroll - notches * mn.EXPLORER_WHEEL_ROWS))
                    check(got == want, f"wheel {scroll},{count},{visible},{notches}")
                up = mn.explorer_wheel_button(4, scroll, count, visible)
                down = mn.explorer_wheel_button(5, scroll, count, visible)
                check(up == max(0, min(top, scroll - mn.EXPLORER_WHEEL_ROWS)), f"btn4 {scroll},{count},{visible}")
                check(down == max(0, min(top, scroll + mn.EXPLORER_WHEEL_ROWS)), f"btn5 {scroll},{count},{visible}")
                check(up <= top and down <= top, f"wheel stays in list {scroll},{count},{visible}")
    for button in (0, 1, 2, 3, 6, 7, 8):
        check(mn.explorer_wheel_button(button, 5, 30, 10) is None, f"non-wheel button {button}")


def n8_case_joystick():
    """The one place a joystick event becomes an action."""
    def event(kind, **fields):
        return pygame.event.Event(kind, **fields)

    for value, want in (((0, 1), mn.UP), ((0, -1), mn.DOWN), ((-1, 0), mn.LEFT), ((1, 0), mn.RIGHT), ((0, 0), None),
                        ((1, 1), mn.UP), ((-1, -1), mn.DOWN), (1, mn.UP), (-1, mn.DOWN), (0, None)):
        check(mn.joystick_action(event(pygame.JOYHATMOTION, value=value, joy=0, hat=0)) == want, "hat %r" % (value,))
    limit = mn.JOY_AXIS_THRESHOLD
    for axis, low, high in ((mn.JOY_VERTICAL_AXIS, mn.UP, mn.DOWN), (mn.JOY_HORIZONTAL_AXIS, mn.LEFT, mn.RIGHT)):
        for value, want in ((-1.0, low), (-limit - 0.01, low), (-limit, None), (-0.1, None), (0.0, None), (0.1, None),
                            (limit, None), (limit + 0.01, high), (1.0, high)):
            check(mn.joystick_action(event(pygame.JOYAXISMOTION, axis=axis, value=value, joy=0)) == want, "axis %d %r" % (axis, value))
    for axis in range(0, 8):
        if axis not in (mn.JOY_VERTICAL_AXIS, mn.JOY_HORIZONTAL_AXIS):
            check(mn.joystick_action(event(pygame.JOYAXISMOTION, axis=axis, value=1.0, joy=0)) is None, "unmapped axis %d" % axis)
    for button in range(0, 20):
        want = mn.ACCEPT if button in mn.JOY_ACCEPT_BUTTONS else (mn.BACK if button in mn.JOY_BACK_BUTTONS else None)
        check(mn.joystick_action(event(pygame.JOYBUTTONDOWN, button=button, joy=0)) == want, "button down %d" % button)
        check(mn.joystick_action(event(pygame.JOYBUTTONUP, button=button, joy=0)) is None, "button up %d" % button)
    check(not (set(mn.JOY_ACCEPT_BUTTONS) & set(mn.JOY_BACK_BUTTONS)), "no button is both accept and back")
    check(mn.joystick_action(event(pygame.KEYDOWN, key=pygame.K_w, mod=0, unicode="w", scancode=0)) is None, "a key press is not a joystick action")
    check(mn.joystick_action(event(pygame.MOUSEBUTTONDOWN, pos=(0, 0), button=1)) is None, "a mouse click is not a joystick action")


def n8_case_digit_entry():
    """Channel number entry: keys, steps, deadline, channel text."""
    keycodes = list(range(0, 1200)) + list(range(0x40000000, 0x40000000 + 700)) + [pygame.K_KP0 + i for i in range(-3, 15)]
    digit_keys = {}
    for key in keycodes:
        digit = mn.key_to_digit(key)
        if digit is not None:
            digit_keys[key] = digit
    check(len(digit_keys) == 20, f"20 digit keys, got {len(digit_keys)}")
    old_map = {
        pygame.K_KP0: "0", pygame.K_KP1: "1", pygame.K_KP2: "2", pygame.K_KP3: "3", pygame.K_KP4: "4",
        pygame.K_KP5: "5", pygame.K_KP6: "6", pygame.K_KP7: "7", pygame.K_KP8: "8", pygame.K_KP9: "9",
    }
    for key in keycodes:
        want = None
        if key in [pygame.K_0, pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5, pygame.K_6, pygame.K_7, pygame.K_8, pygame.K_9]:
            want = chr(key)
        elif key in old_map:
            want = old_map[key]
        check(mn.key_to_digit(key) == want, f"key_to_digit {key}")


    def old_digit_block(buffer, digit, now):
        """The digit rule written out: a full number waits the confirm time, a short one waits the single-digit time."""
        state = {"digit_buffer": buffer}
        if len(state.get("digit_buffer", "")) < 2:
            state["digit_buffer"] = state.get("digit_buffer", "") + digit
        if len(state["digit_buffer"]) >= 2:
            state["digit_timer"] = now + 350
        else:
            state["digit_timer"] = now + 2500
        return state["digit_buffer"], state["digit_timer"]


    for buffer in ("", "1", "12", "07", "99"):
        for digit in "0123456789":
            for now in (0, 1, 999, 123456):
                check(mn.digit_entry_step(buffer, digit, now) == old_digit_block(buffer, digit, now), f"digit step {buffer!r} {digit} {now}")
        for deadline in (0, 100, 5000):
            for now in (0, 99, 100, 101, 5000, 6000):
                check(mn.digit_entry_due(buffer, deadline, now) == bool(buffer and now >= deadline), f"digit due {buffer!r} {deadline} {now}")
        check(mn.digit_entry_channel(buffer) == buffer.zfill(2), f"digit channel {buffer!r}")
    check((mn.DIGIT_ENTRY_MAX, mn.DIGIT_ENTRY_SINGLE_MS, mn.DIGIT_ENTRY_CONFIRM_MS) == (2, 2500, 350), "digit numbers match the rule: two digits, 2.5 seconds for a single digit, 0.35 seconds after the second digit")


def n8_case_dvd_controls():
    """DVD action ids, key translation, seek maths and hold timing."""
    check(mn.DVD_ACTIONS == ("play_pause", "rewind", "fast_forward", "return_to_menu"), "DVD action ids unchanged")
    check([mn.dvd_canonical(a) for a in mn.DVD_ACTIONS] == [-9001, -9002, -9003, pygame.K_BACKSPACE], "DVD canonical codes unchanged")
    check([mn.dvd_default_binding(a) for a in mn.DVD_ACTIONS] == [0x40000000 | 261, 0x40000000 | 285, 0x40000000 | 286, pygame.K_BACKSPACE], "DVD default keys unchanged")
    check((mn.DVD_SKIP_TAP_MS, mn.DVD_SKIP_HOLD_MS, mn.DVD_SKIP_END_GUARD_MS, mn.DVD_SKIP_COOLDOWN_MS, mn.DVD_SKIP_HOLD_START_MS) == (30000, 5000, 10000, 400, 500), "DVD numbers unchanged")
    for action in mn.DVD_ACTIONS:
        check(mn.dvd_action(mn.dvd_canonical(action)) == action, f"dvd_action round trip {action}")
    check(mn.dvd_action(mn.DVD_REMOTE_FAST_FORWARD) is None and mn.dvd_action(mn.DVD_REMOTE_FAST_FORWARD, remote=True) == mn.DVD_FAST_FORWARD, "remote fast forward only with remote=True")
    check(mn.dvd_action(mn.DVD_REMOTE_REWIND) is None and mn.dvd_action(mn.DVD_REMOTE_REWIND, remote=True) == mn.DVD_REWIND, "remote rewind only with remote=True")
    check(mn.dvd_action(mn.DVD_REMOTE_PLAY_PAUSE, remote=True) is None, "remote play is not a transport key of its own")
    for key in range(0, 1200):
        got = mn.dvd_action(key)
        check((got == mn.DVD_RETURN_MENU) == (key == pygame.K_BACKSPACE), f"only Backspace is return to menu {key}")
        check(mn.dvd_action(key, remote=True) == got, f"remote flag leaves ordinary keys alone {key}")

    for is_iso, menu_up in itertools.product((False, True), repeat=2):
        want = "ignore" if not is_iso else ("reissue" if menu_up else "confirm")
        check(mn.dvd_return_menu_step(is_iso, menu_up) == want, f"return step {is_iso} {menu_up}")
        for action in mn.DVD_ACTIONS:
            swallowed = mn.dvd_swallows_transport(mn.dvd_canonical(action), is_iso, menu_up)
            check(swallowed == (is_iso and menu_up and action != mn.DVD_RETURN_MENU), f"swallow {action} {is_iso} {menu_up}")

    for position in (0, 1, 4999, 5000, 29999, 30000, 60000, 99999, 100000, 7200000):
        for length in (None, 0, -1, 1, 9999, 10000, 10001, 40000, 100000, 7200000, 7200001):
            for direction in (1, -1):
                tap = position + direction * 30000
                hold = position + direction * 5000
                if direction > 0 and length and length > 0:
                    tap = min(tap, max(0, length - 10000))
                    hold = min(hold, max(0, length - 10000))
                check(mn.dvd_tap_seek(direction, position, length) == max(0, tap), f"tap {direction} {position} {length}")
                check(mn.dvd_hold_seek(direction, position, length) == max(0, hold), f"hold {direction} {position} {length}")
    for fwd, rew in itertools.product((False, True), repeat=2):
        check(mn.dvd_hold_direction(fwd, rew) == (1 if fwd else (-1 if rew else 0)), f"hold dir {fwd} {rew}")
    for held in (0, 499, 500, 501, 9000):
        for now, cooldown in ((100, 100), (101, 100), (99, 100), (0, 0)):
            check(mn.dvd_hold_due(held, now, cooldown) == (held > 500 and now > cooldown), f"hold due {held} {now} {cooldown}")
            check(mn.dvd_skip_blocked(now, cooldown) == (now <= cooldown), f"skip blocked {now} {cooldown}")
    for bindings in (None, {}, {"play_pause": 11, "rewind": 12, "fast_forward": 13, "return_to_menu": 14}, {"play_pause": 0x40000000 | 261}):
        for key in list(range(0, 40)) + [-9001, -9002, -9003, 0x40000000 | 261, 0x40000000 | 285, 0x40000000 | 286]:
            want = key
            if bindings:
                for action, canon in zip(mn.DVD_ACTIONS, (-9001, -9002, -9003, pygame.K_BACKSPACE)):
                    mapped = bindings.get(action)
                    if mapped is not None and key == mapped:
                        want = canon
                        break
            check(mn.dvd_translate(bindings, key) == want, f"dvd_translate {bindings} {key}")


def n8_poll_dvd_hold_function(broken=False):
    """The real GameDeck._poll_dvd_hold cut out of game_deck.py; broken=True puts the old attribute-exists test back."""
    path = os.path.join(ROOT, "game_deck.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_poll_dvd_hold":
                    code = textwrap.dedent(ast.get_source_segment(source, item))
                    if broken:
                        new_test = 'if not getattr(self, "_dvd_skip_hold_start", 0):'
                        old_test = 'if not hasattr(self, "_dvd_skip_hold_start"):'
                        code = code.replace(new_test, old_test)
                    return code
    raise LookupError("_poll_dvd_hold not found in game_deck.py")


def n8_run_dvd_hold(code, frames, start_attribute=None):
    """Runs the extracted _poll_dvd_hold once per (time, keys down, menu up) frame; returns the times VLC was asked to seek."""
    clock = [0]
    down = set()

    class Keys(object):
        def __getitem__(self, key_code):
            return key_code in down

    seeks = []
    vlc_player = types.SimpleNamespace(get_time=lambda: 600000, get_length=lambda: 7200000, set_time=lambda value: seeks.append(clock[0]))
    deck = types.SimpleNamespace(dvd_in_menu=False, dvd_bindings=None, vlc_engine=types.SimpleNamespace(player=vlc_player),
                                 _set_dvd_osd=lambda *a, **k: None, dvd_hud_until=0)
    if start_attribute is not None:
        deck._dvd_skip_hold_start = start_attribute
    env = {"pygame": types.SimpleNamespace(time=types.SimpleNamespace(get_ticks=lambda: clock[0]),
                                           key=types.SimpleNamespace(get_pressed=lambda: Keys())),
           "_nav": mn, "_xi_nav_state": {},
           "log": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)}
    exec(compile(code, "<extracted:_poll_dvd_hold>", "exec"), env)
    poll = env["_poll_dvd_hold"]
    for now, keys_down, menu_up in frames:
        clock[0] = now
        down.clear()
        down.update(keys_down)
        deck.dvd_in_menu = menu_up
        poll(deck)
    return seeks


def n8_case_dvd_hold_poll():
    """A held DVD fast-forward or rewind key waits the 500 ms start delay on every fresh press, then repeats after the cooldown."""
    forward = mn.dvd_default_binding(mn.DVD_FAST_FORWARD)
    rewind = mn.dvd_default_binding(mn.DVD_REWIND)
    good = n8_poll_dvd_hold_function()
    bad = n8_poll_dvd_hold_function(broken=True)
    for label, key_code in (("fast forward", forward), ("rewind", rewind)):
        frames = [(1000 + t, {key_code}, False) for t in range(0, 1600, 16)]
        frames += [(2600 + t, set(), False) for t in range(0, 160, 16)]
        frames += [(5000 + t, {key_code}, False) for t in range(0, 1600, 16)]
        seeks = n8_run_dvd_hold(good, frames)
        first = [t for t in seeks if t < 2600]
        second = [t for t in seeks if t >= 5000]
        check(bool(first) and first[0] - 1000 > mn.DVD_SKIP_HOLD_START_MS, f"{label}: first hold waits the start delay, seeks {first[:2]}")
        check(bool(second) and second[0] - 5000 > mn.DVD_SKIP_HOLD_START_MS, f"{label}: a fresh press after a release waits the start delay, seeks {second[:2]}")
        check(all(b - a >= mn.DVD_SKIP_COOLDOWN_MS for a, b in zip(seeks, seeks[1:]) if b < 2600 or a >= 5000), f"{label}: repeats keep the cooldown")
        old_seeks = n8_run_dvd_hold(bad, frames)
        old_second = [t for t in old_seeks if t >= 5000]
        check(bool(old_second) and old_second[0] - 5000 <= mn.DVD_SKIP_HOLD_START_MS, f"{label}: the old attribute test is caught (seeks at {old_second[:2]})")
    tap_then_hold = [(1000 + t, {forward}, False) for t in range(0, 1200, 16)]
    seeks = n8_run_dvd_hold(good, tap_then_hold, start_attribute=1000)
    check(bool(seeks) and seeks[0] - 1000 > mn.DVD_SKIP_HOLD_START_MS, "a hold that starts at a tap's time waits from the tap")
    in_menu = [(1000 + t, {forward}, True) for t in range(0, 1600, 16)]
    check(n8_run_dvd_hold(good, in_menu) == [], "no seek while the disc menu is up")
    nothing = [(1000 + t, set(), False) for t in range(0, 1600, 16)]
    check(n8_run_dvd_hold(good, nothing) == [], "no seek with no key down")
    both = [(1000 + t, {forward, rewind}, False) for t in range(0, 1600, 16)]
    only_forward = [(1000 + t, {forward}, False) for t in range(0, 1600, 16)]
    check(n8_run_dvd_hold(good, both) == n8_run_dvd_hold(good, only_forward), "both keys down act as fast forward, as dvd_hold_direction says")


def n8_case_guide_hold():
    """A held guide row repeat never re-aims; the time stop is re-aimed once when the hold ends."""
    block_start = MAIN_SRC.index("        _gh_any = False\n        _gh_hold = app_state[\"_hold_guide\"]")
    block = MAIN_SRC[block_start:MAIN_SRC.index("    _prof_mark(\"idle_input\")", block_start)]
    frames = []
    for t in range(0, 2400, 16):
        frames.append((1000 + t, {pygame.K_s} if t < 1200 else set()))
    new_out, new_state = n8_run_guide_hold(block, frames)
    rows = [c for f in new_out for c in f if c[0] == "row"]
    resyncs = [c for f in new_out for c in f if c[0] == "resync"]
    check(len(rows) > 5, "a held S repeats rows")
    check(all(c == ("row", 1, False) for c in rows), "held repeat rows never re-aim")
    check(len(resyncs) == 1, f"one re-aim when the hold ends, got {len(resyncs)}")
    last_row_frame = max(i for i, f in enumerate(new_out) if any(c[0] == "row" for c in f))
    resync_frames = [i for i, f in enumerate(new_out) if any(c[0] == "resync" for c in f)]
    check(bool(resync_frames) and resync_frames[0] > last_row_frame, "the re-aim comes after the last repeat")
    check("_guide_hold_moved_rows" not in new_state, "the flag is cleared")
    quick = [(1000 + t, {pygame.K_s} if t < 100 else set()) for t in range(0, 800, 16)]
    q_out, _ = n8_run_guide_hold(block, quick)
    check(not any(c[0] in ("row", "resync") for f in q_out for c in f), "a quick tap repeats nothing and re-aims nothing here")
    side = [(1000 + t, {pygame.K_d} if t < 1200 else set()) for t in range(0, 2400, 16)]
    s_out, _ = n8_run_guide_hold(block, side)
    check(any(c[0] == "col" for f in s_out for c in f) and not any(c[0] == "resync" for f in s_out for c in f), "a held D moves columns and does not re-aim")


class TestMenuNavWheelJoystickDigitsDvd(_SectionCase):
    """Explorer wheel clamp, joystick actions, channel number entry, DVD controls, guide hold re-aim"""

    def test_wheel_clamp(self):
        self.run_section(n8_case_wheel_clamp)

    def test_joystick(self):
        self.run_section(n8_case_joystick)

    def test_digit_entry(self):
        self.run_section(n8_case_digit_entry)

    def test_dvd_controls(self):
        self.run_section(n8_case_dvd_controls)

    def test_dvd_hold_poll(self):
        self.run_section(n8_case_dvd_hold_poll)

    def test_guide_hold(self):
        self.run_section(n8_case_guide_hold)


# ============================================================================
# SECTION: DVD captions (caption source choice, burned-in suppression, non-blocking track pick)
# ============================================================================

_DVD_CAP_METHODS = ("_apply_dvd_captions_state", "_dvd_caption_spu_ids", "_dvd_caption_flow_new", "_dvd_caption_lookup_job",
                    "_dvd_caption_slave_begin", "_dvd_caption_slave_step", "_dvd_caption_source_step",
                    "_dvd_suppress_vlc_subtitles", "_maybe_auto_apply_dvd_captions")


_DVD_CAP_NODES = []


def dvd_cap_method_nodes():
    """The real DVD caption methods of game_deck.py as (name, AST node, source) triples."""
    if _DVD_CAP_NODES:
        return _DVD_CAP_NODES
    with open(os.path.join(ROOT, "game_deck.py"), encoding="utf-8") as handle:
        source = handle.read()
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in _DVD_CAP_METHODS:
                    found[item.name] = (item, textwrap.dedent(ast.get_source_segment(source, item)))
    missing = [name for name in _DVD_CAP_METHODS if name not in found]
    if missing:
        raise LookupError("not found in game_deck.py: %s" % missing)
    _DVD_CAP_NODES.extend((name,) + found[name] for name in _DVD_CAP_METHODS)
    return _DVD_CAP_NODES


class DvdCapVlc(object):
    """A stand-in VLC: a list of subtitle track ids, a selected one, and slave files that appear after a delay."""

    def __init__(self, clock, tracks=(), selected=-1, appear_after_ms=700, never_lists=False):
        self.clock = clock
        self.player = self
        self.tracks = list(tracks)
        self.selected = selected
        self.appear_after_ms = appear_after_ms
        self.never_lists = never_lists
        self.slaves = []
        self.set_calls = []
        self.highlight_calls = 0
        self._due = []
        self._next_id = 100

    def video_get_spu_description(self):
        for due, track_id in list(self._due):
            if self.clock[0] >= due:
                self.tracks.append(track_id)
                self._due.remove((due, track_id))
        return [(-1, "Disable")] + [(t, "Track %d" % t) for t in self.tracks]

    def add_slave(self, kind, uri, select):
        self.slaves.append(uri)
        if not self.never_lists:
            self._next_id += 1
            self._due.append((self.clock[0] + self.appear_after_ms, self._next_id))

    def video_get_spu(self):
        return self.selected

    def video_set_spu(self, track_id):
        self.selected = track_id
        self.set_calls.append(track_id)

    def _ensure_menu_highlight_spu(self):
        self.highlight_calls += 1


def dvd_cap_build(source_transform=None, resolver=None, lookup_runs=True):
    """Builds a deck class from the real methods; returns (class, env) where env holds the stubs the methods use."""
    clock = [1000]
    logs = []
    calls = {"resolve": [], "asr": [], "threads": 0, "wait": 0}

    def wait(_ms):
        calls["wait"] += 1
        raise AssertionError("pygame.time.wait was called on the render thread")

    class Thread(object):
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self.target, self.args = target, args

        def start(self):
            calls["threads"] += 1
            if lookup_runs:
                self.target(*self.args)

    def resolve(path):
        calls["resolve"].append(path)
        if isinstance(resolver, Exception):
            raise resolver
        return resolver

    env = {"pygame": types.SimpleNamespace(time=types.SimpleNamespace(get_ticks=lambda: clock[0], wait=wait)),
           "os": os, "threading": types.SimpleNamespace(Thread=Thread), "_gov": gov,
           "resolve_caption_source": resolve,
           "log": types.SimpleNamespace(info=lambda *a, **k: logs.append(("info", a[0] % a[1:] if len(a) > 1 else a[0])),
                                        warning=lambda *a, **k: logs.append(("warning", a[0] % a[1:] if len(a) > 1 else a[0]))),
           "_log_warn_throttled": lambda key, msg, *a, **k: logs.append(("warning", msg))}
    namespace = {}
    for name, _node, source in dvd_cap_method_nodes():
        if source_transform:
            source = source_transform(name, source)
        exec(compile(source, "<extracted:%s>" % name, "exec"), env)
        namespace[name] = staticmethod(env[name]) if name == "_dvd_caption_lookup_job" else env[name]
    namespace["_start_dvd_captions_asr_job"] = lambda self, path: calls["asr"].append(path)
    return type("Deck", (), namespace), {"clock": clock, "logs": logs, "calls": calls}


def dvd_cap_deck(cls, disc=False, path=r"C:\movies\film.mkv", pending=True):
    deck = cls()
    deck.dvd_file_path, deck.dvd_is_disc_image = path, disc
    deck.dvd_in_menu, deck.dvd_splash_done = False, True
    deck.dvd_captions, deck.dvd_captions_enabled = False, True
    deck._dvd_captions_auto_pending, deck._dvd_captions_auto_done = pending, not pending
    deck._dvd_captions_auto_deadline = deck._dvd_captions_auto_next_try = 0
    deck._dvd_captions_asr_state, deck._dvd_captions_asr_srt_path = "idle", None
    deck._dvd_cap_flow = deck._dvd_cap_suppress = None
    return deck


def dvd_cap_run(deck, vlc, env, until_ms, step=100, hook=None):
    """Ticks the deck once per step from the current clock up to until_ms; hook(now) runs before each tick."""
    while env["clock"][0] <= until_ms:
        if hook:
            hook(env["clock"][0])
        deck._maybe_auto_apply_dvd_captions(vlc)
        env["clock"][0] += step


def dvd_cap_problems(source_transform=None):
    """Runs every scenario against the real methods (optionally with their source altered) and returns what went wrong."""
    problems = []

    def ok(condition, label):
        if not condition:
            problems.append(label)

    fake_vlc_module = types.SimpleNamespace(MediaSlaveType=types.SimpleNamespace(subtitle=1))
    with mock.patch.dict(sys.modules, {"vlc": fake_vlc_module}):
        cls, env = dvd_cap_build(source_transform, {"kind": "sidecar", "srt_path": r"C:\movies\film.srt"})
        vlc, deck = DvdCapVlc(env["clock"], tracks=[5]), None
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 1500)
        ok(vlc.slaves == ["file:///C:/movies/film.srt"], f"sidecar: file handed to VLC once, got {vlc.slaves}")
        ok(vlc.set_calls == [], f"sidecar: nothing selected before VLC lists the new track, got {vlc.set_calls}")
        dvd_cap_run(deck, vlc, env, 6000)
        ok(vlc.set_calls == [101], f"sidecar: the new track, not the embedded one, is selected, got {vlc.set_calls}")
        ok(deck.dvd_captions and deck._dvd_captions_auto_done and not deck._dvd_captions_auto_pending, "sidecar: captions on and the tick finished")
        ok(env["calls"]["asr"] == [] and env["calls"]["wait"] == 0, "sidecar: no speech recognition, no blocking wait")

        cls, env = dvd_cap_build(source_transform, {"kind": "sidecar", "srt_path": r"C:\movies\film.srt"})
        vlc = DvdCapVlc(env["clock"], tracks=[5], never_lists=True)
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 9000)
        ok(any("never listed" in text for kind, text in env["logs"]), "sidecar never listed: the fallback is logged")
        ok(env["calls"]["asr"] == [r"C:\movies\film.mkv"], "sidecar never listed: the old flow (speech recognition) takes over")
        ok(len(vlc.slaves) == 1, f"sidecar never listed: the file is handed over once, got {len(vlc.slaves)}")

        cls, env = dvd_cap_build(source_transform, {"kind": "baked", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[3], selected=3)
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 1500)
        ok(vlc.set_calls == [-1], f"burned-in: VLC's own track is switched off, got {vlc.set_calls}")
        vlc.selected = 3
        dvd_cap_run(deck, vlc, env, 6000)
        ok(vlc.selected == -1, "burned-in: a track VLC selects again is switched off again")
        ok(env["calls"]["asr"] == [] and vlc.slaves == [] and not deck.dvd_captions, "burned-in: no speech recognition, no file handed to VLC, captions not on")
        ok(deck._dvd_captions_auto_done and not deck._dvd_captions_auto_pending, "burned-in: the tick finished")
        dvd_cap_run(deck, vlc, env, 22000)
        vlc.selected = 3
        dvd_cap_run(deck, vlc, env, 24000)
        ok(vlc.selected == 3, "burned-in: after the watch window VLC is left alone")
        ok(deck._dvd_cap_suppress is None, "burned-in: the watch state is cleared")

        cls, env = dvd_cap_build(source_transform, {"kind": "baked", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[3], selected=3)
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 1200)
        deck.dvd_captions_enabled = False
        vlc.selected = 3
        dvd_cap_run(deck, vlc, env, 3000)
        ok(vlc.selected == 3 and deck._dvd_cap_suppress is None, "burned-in: with the DVD Captions setting off VLC is left alone")

        cls, env = dvd_cap_build(source_transform, {"kind": "embedded", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[4, 6])
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 3000)
        ok(vlc.set_calls == [4] and deck.dvd_captions, f"embedded: VLC's first track is selected, got {vlc.set_calls}")
        ok(env["calls"]["asr"] == [] and deck._dvd_captions_auto_done, "embedded: no speech recognition, tick finished")

        cls, env = dvd_cap_build(source_transform, {"kind": "embedded", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[])
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 11000)
        ok(any("lists no embedded track" in text for kind, text in env["logs"]), "embedded but not listed: the fallback is logged")
        ok(env["calls"]["asr"] == [r"C:\movies\film.mkv"], "embedded but not listed: speech recognition takes over")

        cls, env = dvd_cap_build(source_transform, {"kind": None, "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[])
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 3000)
        ok(env["calls"]["asr"] == [r"C:\movies\film.mkv"] and deck._dvd_captions_asr_state == "running", "no source: speech recognition starts once")

        def finish_asr(now):
            if now == 4000:
                deck._dvd_captions_asr_state = "done"
                deck._dvd_captions_asr_srt_path = r"C:\temp\asr.srt"

        dvd_cap_run(deck, vlc, env, 8000, hook=finish_asr)
        ok(vlc.slaves == ["file:///C:/temp/asr.srt"], f"speech recognition result handed to VLC, got {vlc.slaves}")
        ok(vlc.set_calls == [101] and deck.dvd_captions and deck._dvd_captions_auto_done, f"speech recognition track selected, got {vlc.set_calls}")
        ok(deck._dvd_captions_asr_state == "applying" and env["calls"]["wait"] == 0, "speech recognition result: state moves on, no blocking wait")

        cls, env = dvd_cap_build(source_transform, {"kind": None, "srt_path": None}, lookup_runs=False)
        vlc = DvdCapVlc(env["clock"], tracks=[])
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 19000)
        ok(env["calls"]["asr"] == [], "slow lookup: nothing starts before the timeout")
        dvd_cap_run(deck, vlc, env, 22000)
        ok(any("took over" in text for kind, text in env["logs"]), "slow lookup: the fallback is logged")
        ok(env["calls"]["asr"] == [r"C:\movies\film.mkv"], "slow lookup: the old flow takes over")

        cls, env = dvd_cap_build(source_transform, RuntimeError("boom"))
        vlc = DvdCapVlc(env["clock"], tracks=[])
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 2000)
        ok(env["calls"]["asr"] == [r"C:\movies\film.mkv"], "failed lookup: the old flow takes over")

        cls, env = dvd_cap_build(source_transform, {"kind": "baked", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[7])
        deck = dvd_cap_deck(cls, disc=True, path=r"D:\disc.iso")
        dvd_cap_run(deck, vlc, env, 2500)
        ok(env["calls"]["resolve"] == [] and env["calls"]["threads"] == 0, "disc image: no caption lookup")
        ok(vlc.set_calls == [7] and deck._dvd_captions_auto_done and env["calls"]["asr"] == [], "disc image: VLC's listed track is used as before, no speech recognition")

        cls, env = dvd_cap_build(source_transform, {"kind": "baked", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[7])
        deck = dvd_cap_deck(cls, pending=False)
        dvd_cap_run(deck, vlc, env, 2500)
        ok(env["calls"]["resolve"] == [] and vlc.set_calls == [], "captions off or locked: the tick does nothing")

        cls, env = dvd_cap_build(source_transform, {"kind": "embedded", "srt_path": None})
        vlc = DvdCapVlc(env["clock"], tracks=[4])
        deck = dvd_cap_deck(cls)
        dvd_cap_run(deck, vlc, env, 2500)
        dvd_cap_run(deck, vlc, env, 3000)
        ok(deck._dvd_cap_flow is None, "the per-title state is cleared once the tick is finished")
        deck._dvd_captions_auto_pending, deck._dvd_captions_auto_done = True, False
        deck._dvd_captions_auto_deadline = deck._dvd_captions_auto_next_try = 0
        deck._dvd_captions_asr_state, deck._dvd_captions_asr_srt_path = "idle", None
        dvd_cap_run(deck, vlc, env, 5000)
        ok(len(env["calls"]["resolve"]) == 2, f"turning the setting on again looks the file up again, got {len(env['calls']['resolve'])}")

        cls, env = dvd_cap_build(source_transform, None)
        vlc = DvdCapVlc(env["clock"], tracks=[4], selected=4)
        deck = dvd_cap_deck(cls)
        deck._apply_dvd_captions_state(False, vlc)
        ok(vlc.set_calls == [-1] and not deck.dvd_captions and vlc.highlight_calls == 1, "captions off: VLC's track is switched off and the highlight re-checked")
        deck._apply_dvd_captions_state(True, vlc)
        ok(vlc.set_calls == [-1, 4] and deck.dvd_captions, "captions on: VLC's listed track is selected")
    return problems


def dvd_cap_source_checks():
    """The caption methods hold no blocking wait and no bare tunable number; the governor table holds the numbers."""
    problems = []
    for name, node, _source in dvd_cap_method_nodes():
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == "wait":
                problems.append(f"{name} calls a wait")
            if isinstance(sub, ast.Constant) and isinstance(sub.value, int) and not isinstance(sub.value, bool) and sub.value not in (-1, 0, 1):
                problems.append(f"{name} holds the bare number {sub.value}")
    return problems


def dvd_cap_break_checks():
    """Deliberate breaks of the real source; each one has to make the scenarios fail."""
    breaks = {
        "burned-in watch skipped": ("_dvd_caption_source_step", 'self._dvd_cap_suppress = {"path": flow["path"],', 'self._dvd_cap_suppress = None; _unused = {"path": flow["path"],'),
        "burned-in does not finish": ("_dvd_caption_source_step", "self._dvd_captions_auto_done = True\n                self._dvd_captions_auto_pending = False\n                return True\n            if kind in", "return True\n            if kind in"),
        "old track picked instead of the new one": ("_dvd_caption_slave_step", "vlc.player.video_set_spu(added[-1])", "vlc.player.video_set_spu(slave['before'][0] if slave['before'] else added[-1])"),
        "no wait for the lookup": ("_dvd_caption_source_step", "                    return True\n                log.warning(\"[DVD CAPTIONS] Caption lookup for", "                    pass\n                log.warning(\"[DVD CAPTIONS] Caption lookup for"),
        "speech recognition always starts": ("_maybe_auto_apply_dvd_captions", "        elif self._dvd_caption_source_step(vlc, flow, now):\n            return\n", "        else:\n            self._dvd_caption_source_step(vlc, flow, now)\n"),
        "disc images looked up": ("_dvd_caption_flow_new", "if self.dvd_is_disc_image or not self.dvd_file_path:", "if not self.dvd_file_path:"),
    }
    results = {}
    for label, (method, old, new) in breaks.items():
        def transform(name, source, method=method, old=old, new=new):
            if name != method:
                return source
            if source.count(old) != 1:
                raise LookupError("break %r: text to change found %d times" % (label, source.count(old)))
            return source.replace(old, new)

        try:
            results[label] = bool(dvd_cap_problems(transform))
        except AssertionError:
            results[label] = True
    return results


def dvd_cap_case_flow():
    """DVD captions: caption file first, burned-in files keep VLC's own track off, embedded track before speech recognition, slow or failed lookups fall back, nothing blocks."""
    for label in dvd_cap_problems():
        check(False, label)
    check(True, "dvd caption scenarios ran")
    for label in dvd_cap_source_checks():
        check(False, label)
    check((gov.DVD_CAPTIONS_LOOKUP_TIMEOUT_MS, gov.DVD_CAPTIONS_TRACK_TIMEOUT_MS, gov.DVD_CAPTIONS_POLL_MS,
           gov.DVD_CAPTIONS_SUPPRESS_WATCH_MS, gov.DVD_CAPTIONS_SUPPRESS_POLL_MS) == (20000, 8000, 500, 20000, 400), "DVD caption numbers sit in the governor table")
    for label, caught in dvd_cap_break_checks().items():
        check(caught, f"deliberate break caught: {label}")


class TestDvdCaptions(_SectionCase):
    """DVD player captions for regular video files"""

    def test_flow(self):
        self.run_section(dvd_cap_case_flow)


# ============================================================================
# SECTION: Name entry steps and the File Explorer panel matching the Main Menu rectangle
# ============================================================================


# ============================================================================
# SECTION: Pulling the real code out of the main script
# ============================================================================

_CODE_CACHE = {}


def main_function_code(name):
    if ("f", name) not in _CODE_CACHE:
        for node in MAIN_TREE.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                _CODE_CACHE[("f", name)] = compile(ast.get_source_segment(MAIN_SRC, node), "<main %s>" % name, "exec")
                break
        else:
            raise KeyError(name)
    return _CODE_CACHE[("f", name)]


def main_function_source(name):
    for node in MAIN_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(MAIN_SRC, node)
    raise KeyError(name)


def main_assignment_code(name):
    if ("a", name) not in _CODE_CACHE:
        for node in MAIN_TREE.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                _CODE_CACHE[("a", name)] = compile(ast.get_source_segment(MAIN_SRC, node), "<main %s>" % name, "exec")
                break
        else:
            raise KeyError(name)
    return _CODE_CACHE[("a", name)]


# ============================================================================
# SECTION: Name entry, old block against new block
# ============================================================================


def suite_name_step_direct():
    max_len = 4
    r = menu_nav.name_entry_step(pygame.K_RETURN, "\r", "AB", False, max_len)
    check(r == (menu_nav.NAME_SAVE, "AB", False), "enter saves")
    r = menu_nav.name_entry_step(pygame.K_ESCAPE, "\x1b", "AB", True, max_len)
    check(r == (None, "AB", True), "escape does nothing when cancel_on_back is off")
    r = menu_nav.name_entry_step(pygame.K_ESCAPE, "\x1b", "AB", True, max_len, cancel_on_back=True)
    check(r == (menu_nav.NAME_CANCEL, "AB", True), "escape cancels when cancel_on_back is on")
    r = menu_nav.name_entry_step(pygame.K_BACKSPACE, "\x08", "AB", False, max_len)
    check(r == (menu_nav.NAME_EDIT, "A", False), "backspace removes one")
    r = menu_nav.name_entry_step(pygame.K_BACKSPACE, "\x08", "AB", True, max_len)
    check(r == (menu_nav.NAME_EDIT, "", False), "backspace clears a full selection")
    r = menu_nav.name_entry_step(pygame.K_BACKSPACE, "\x08", "", False, max_len)
    check(r == (menu_nav.NAME_EDIT, "", False), "backspace on empty text")
    r = menu_nav.name_entry_step(pygame.K_a, "a", "ABC", False, max_len)
    check(r == (menu_nav.NAME_EDIT, "ABCa", False), "letter added with room")
    r = menu_nav.name_entry_step(pygame.K_a, "a", "ABCD", False, max_len)
    check(r == (None, "ABCD", False), "letter refused when full")
    r = menu_nav.name_entry_step(pygame.K_a, "a", "ABCD", True, max_len)
    check(r == (menu_nav.NAME_EDIT, "a", False), "letter replaces a full selection even when full")
    r = menu_nav.name_entry_step(pygame.K_SPACE, " ", "AB", False, max_len)
    check(r == (menu_nav.NAME_EDIT, "AB ", False), "space added")
    r = menu_nav.name_entry_step(pygame.K_SPACE, " ", "AB", True, max_len)
    check(r == (menu_nav.NAME_EDIT, " ", False), "space replaces a selection")
    r = menu_nav.name_entry_step(pygame.K_MINUS, "-", "AB", False, max_len)
    check(r == (None, "AB", False), "punctuation ignored")
    r = menu_nav.name_entry_step(pygame.K_w, "w", "AB", False, max_len)
    check(r == (menu_nav.NAME_EDIT, "ABw", False), "W is a letter in a name box")
    check(menu_nav.NAME_ENTRY_CHANNEL_MAX_DEFAULT == 18 and menu_nav.NAME_ENTRY_SLOT_MAX == 24, "name limits")


# ============================================================================
# SECTION: File Explorer rectangle against the Main Menu rectangle
# ============================================================================

class FakeDbConfig:
    def __init__(self, config):
        self.config = config


class FakeGameDeck:
    def __init__(self, inner):
        self.inner = pygame.Rect(inner) if inner is not None else None

    def get_active_console_bezel_inner_rect(self, rect):
        return self.inner


def build_main_namespace(config, stem, warp_on, orbiter, channel, console_inner):
    ns = {"pygame": pygame, "menu_panel_rect": ui.menu_panel_rect, "fit_menu_envelope": ui.fit_menu_envelope,
          "db": FakeDbConfig(config), "app_state": {"current_channel": channel},
          "game_deck": FakeGameDeck(console_inner) if console_inner is not None else FakeGameDeck(None),
          "_active_bezel_stem": lambda: stem, "_setting_on": lambda key, default=False: warp_on,
          "_package_pillarbox_active": lambda w, h: False,
          "_log_warn_throttled": lambda *a, **k: None,
          "log": type("L", (), {"info": staticmethod(lambda *a, **k: None), "warning": staticmethod(lambda *a, **k: None)})()}
    margin = 3

    def orbiter_fn(rect):
        if not orbiter:
            return rect
        ox, oy = orbiter
        return pygame.Rect(rect.x + ox * margin - margin, rect.y + oy * margin - margin,
                           rect.width + margin * 2, rect.height + margin * 2)

    ns["_apply_pixel_orbiter"] = orbiter_fn
    for name in ("_MENU_BEZEL_SHRINK", "_MENU_BEZEL_SHRINK_DEFAULT", "_MENU_WARP_OFF_EXTRA_SHRINK",
                 "_OVERLAY_BORDER_FIT_MARGIN", "_overlay_content_rect_log_state"):
        exec(main_assignment_code(name), ns)
    for name in ("_overlay_content_rect", "_overlay_target", "_overlay_target_for_menu",
                 "_active_menu_bezel_shrink", "_menu_panel_rect_on"):
        exec(main_function_code(name), ns)
    return ns


def old_menu_rect_on_screen(ns, screen):
    target = ns["_overlay_target_for_menu"](screen)
    tx, ty = target.get_abs_offset()
    real_w, real_h = target.get_size()
    x, y, w, h = ui.menu_panel_rect(real_w, real_h, ns["db"])
    sx, sy = screen.get_abs_offset()
    return tx - sx + x, ty - sy + y, w, h


def suite_explorer_rect():
    sizes = [(1280, 720), (1920, 1080), (1024, 768), (800, 600), (640, 480), (1366, 768), (2560, 1440),
             (854, 480), (1600, 1200), (3440, 1440), (720, 720), (400, 300)]
    configs = [{"fake_43_border_enabled": False}]
    for style in (1, 2, 3, "x"):
        configs.append({"fake_43_border_enabled": True, "bezel_style": style})
    stems = [None, "bezel", "bezel1", "bezel2", "other"]
    orbiters = [None, (0, 0), (1, 0), (-1, 1), (0, -1)]
    consoles = [None, (100, 80, 600, 400), (0, 0, 400, 300)]
    surfaces = {}
    new_vs_menu_bad = 0
    runs = 0
    for (w, h) in sizes:
        screen = surfaces.setdefault((w, h), pygame.Surface((w, h)))
        for config in configs:
            for stem in stems:
                for warp_on in (True, False):
                    for orbiter in orbiters:
                        for channel, console in (("05", None), ("03", None), ("03", consoles[1]), ("03", consoles[2])):
                            ns = build_main_namespace(config, stem, warp_on, orbiter, channel, console)
                            menu = old_menu_rect_on_screen(ns, screen)
                            new = ns["_menu_panel_rect_on"](screen)
                            runs += 1
                            if tuple(new) != tuple(menu):
                                new_vs_menu_bad += 1
                                if new_vs_menu_bad <= 5:
                                    print("NEW EXPLORER != MENU", (w, h), config, stem, warp_on, orbiter, channel, console, new, menu)
    check(new_vs_menu_bad == 0, "explorer rect differs from the menu rect on %d of %d configurations" % (new_vs_menu_bad, runs))


def suite_video_mode_rect():
    def build(config, mode_name):
        ns = {"pygame": pygame, "db": FakeDbConfig(config), "_active_screen_mode": lambda: mode_name,
              "_package_pillarbox_active": lambda w, h: True,
              "_bezel_video_wants_tube_fit": lambda source_aspect, mode=None: False,
              "_apply_pixel_orbiter": lambda rect: rect,
              "_selected_package_aspect": lambda: 16.0 / 9.0,
              "_video_screen_mode_rect_log_state": {"key": None},
              "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
        exec(main_function_code("_full_tv_43_outer_rect"), ns)
        exec(main_function_code("_video_screen_mode_rect"), ns)
        ns["package_box"] = lambda w, h: ns["_full_tv_43_outer_rect"](w, h, aspect=16.0 / 9.0)
        return ns["_video_screen_mode_rect"]

    def package_box(w, h):
        return build({"fake_43_border_enabled": False}, "normal").__globals__["package_box"](w, h)

    def build_blit():
        ns = {"pygame": pygame, "time": time, "_blit_video_log_state": {"key": None}, "_last_screen_mode_picture_rect": None,
              "_video_bar_color": lambda: (0, 0, 0), "_active_screen_mode": lambda: "normal",
              "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
        exec(main_function_code("_blit_video_with_screen_mode"), ns)
        return ns["_blit_video_with_screen_mode"]

    box = pygame.Rect(0, 60, 1024, 576)
    for name in ("normal", "stretch", "crop", "panscan"):
        fn = build({"fake_43_border_enabled": False}, name)
        check(tuple(fn(box, 1024, 768, mode=name)) == (0, 0, 1024, 768), "%s gets the whole window with Bezel off" % name)
        check(tuple(fn(box, 1280, 720, mode=name)) == (0, 0, 1280, 720), "%s gets the whole window on a 16:9 screen" % name)
        fn = build({"fake_43_border_enabled": True}, name)
        check(tuple(fn(box, 1024, 768, mode=name)) == (128, 60, 768, 576), "with Bezel on %s keeps the 4:3 box inside the content rect" % name)

    blit = build_blit()
    rect_fn = build({"fake_43_border_enabled": False}, "normal")
    screens = ((1152, 864), (1024, 768), (1280, 1024), (1280, 800), (1280, 720), (1920, 1080), (2560, 1080))
    pictures = ((16, 9), (4, 3), (9, 16), (21, 9), (5, 4))
    for win_w, win_h in screens:
        for pic_w, pic_h in pictures:
            surf = pygame.Surface((pic_w * 40, pic_h * 40))
            screen = pygame.Surface((win_w, win_h))
            target = rect_fn(package_box(win_w, win_h), win_w, win_h)
            geom = {}
            blit(screen, surf, target, mode="normal", geom_out=geom, fit_aspect=pic_w / pic_h)
            side_bars = geom["dest_x"] > 1 or win_w - (geom["dest_x"] + geom["draw_w"]) > 1
            top_bars = geom["dest_y"] > 1 or win_h - (geom["dest_y"] + geom["draw_h"]) > 1
            check(not (side_bars and top_bars), "%dx%d screen, %d:%d picture has bars on both axes: %s" % (win_w, win_h, pic_w, pic_h, geom))
            check(abs(geom["draw_w"] / geom["draw_h"] - pic_w / pic_h) < 0.02, "%dx%d screen, %d:%d picture is distorted: %s" % (win_w, win_h, pic_w, pic_h, geom))
            for name in ("stretch", "crop"):
                geom = {}
                blit(screen, surf, target, mode=name, geom_out=geom, fit_aspect=pic_w / pic_h)
                covered = geom["draw_w"] >= win_w and geom["draw_h"] >= win_h
                check(covered, "%dx%d screen, %d:%d picture in %s does not fill the screen: %s" % (win_w, win_h, pic_w, pic_h, name, geom))


def suite_one_picture_path():
    class Gov:
        VIDEO_DECODE_CANVAS_SD = (854, 480)
        VIDEO_DECODE_CANVAS_HD = (1280, 720)

    ns = {"performance_governor": Gov}
    exec(main_function_code("_video_decode_canvas_size"), ns)
    size = ns["_video_decode_canvas_size"]
    check(size("sd") == (854, 480) and size("hd") == (1280, 720), "decode canvas sizes are %s and %s" % (size("sd"), size("hd")))
    ns_no_gov = {"performance_governor": None}
    exec(main_function_code("_video_decode_canvas_size"), ns_no_gov)
    check(ns_no_gov["_video_decode_canvas_size"]("sd") == size("sd") and ns_no_gov["_video_decode_canvas_size"]("hd") == size("hd"),
          "the fallback canvas sizes must equal the governor table")

    for gone in ("_canvas_is_genuinely_4_3", "genuine_4:3_fastpath", "_bezel_off_fit_aspect", "_filled_canvas_aspect_state", "canvas_genuinely_4_3"):
        check(gone not in MAIN_SRC, "%s is still in the main script" % gone)
    check(len(re.findall(r"= _video_decode_canvas_size\(", MAIN_SRC)) == 2, "the engine start and resize_canvas must both use _video_decode_canvas_size")
    check("(960, 720) if" not in MAIN_SRC and "(640, 480) if" not in MAIN_SRC, "a 4:3 decode canvas branch is still in the main script")

    import performance_governor as _pg
    check(tuple(_pg.VIDEO_DECODE_CANVAS_SD) == size("sd") and tuple(_pg.VIDEO_DECODE_CANVAS_HD) == size("hd"), "governor canvas table does not match")

    ns = {"pygame": pygame, "time": time, "_blit_video_log_state": {"key": None}, "_last_screen_mode_picture_rect": None,
          "_video_bar_color": lambda: (0, 0, 0), "_active_screen_mode": lambda: "normal",
          "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
    exec(main_function_code("_blit_video_with_screen_mode"), ns)
    blit = ns["_blit_video_with_screen_mode"]
    pictures = ((1280, 720), (640, 480), (1080, 1920), (1920, 804), (1280, 1024))
    screens = ((1152, 864), (1024, 768), (1280, 1024), (1280, 800), (1280, 720), (1920, 1080), (2560, 1080))
    bars = {}
    for win_w, win_h in screens:
        for pw, ph in pictures:
            surf = pygame.Surface((pw, ph))
            geom = {}
            blit(pygame.Surface((win_w, win_h)), surf, pygame.Rect(0, 0, win_w, win_h), mode="normal", geom_out=geom)
            label = "%dx%d screen, %dx%d picture: %s" % (win_w, win_h, pw, ph, geom)
            check(abs(geom["draw_w"] / geom["draw_h"] - pw / float(ph)) < 0.02, "distorted, " + label)
            check(geom["draw_w"] >= win_w - 1 or geom["draw_h"] >= win_h - 1, "bars on both axes, " + label)
            bars[(win_w, win_h, pw, ph)] = (geom["dest_x"], geom["dest_y"])
            stretch = {}
            blit(pygame.Surface((win_w, win_h)), surf, pygame.Rect(0, 0, win_w, win_h), mode="stretch", geom_out=stretch)
            check((stretch["draw_w"], stretch["draw_h"]) == (win_w, win_h), "stretch does not fill, " + label)
    for win_w, win_h in screens:
        for pw, ph in pictures:
            for other_w, other_h in screens:
                if (other_w, other_h) != (win_w, win_h) and (other_w / float(other_h) == win_w / float(win_h)):
                    x1, y1 = bars[(win_w, win_h, pw, ph)]
                    x2, y2 = bars[(other_w, other_h, pw, ph)]
                    check(abs(x1 / float(win_w) - x2 / float(other_w)) < 0.01 and abs(y1 / float(win_h) - y2 / float(other_h)) < 0.01,
                          "same-shape screens must have proportional bars")


def suite_one_shape_per_label():
    import performance_governor as _pg
    ns = {"performance_governor": _pg, "math": math}
    exec(main_function_code("_snap_standard_aspect"), {"_PICTURE_STANDARD_ASPECTS": tuple(_pg.PICTURE_STANDARD_ASPECTS), "math": math}, ns)
    snap = ns["_snap_standard_aspect"]
    wide, std, tall = 16.0 / 9.0, 4.0 / 3.0, 9.0 / 16.0
    expected = ((2.39, wide), (2.35, wide), (2.0, wide), (1.85, wide), (16.0 / 9.0, wide), (1.6, wide),
                (1.5, std), (4.0 / 3.0, std), (1.25, std), (1.0, std),
                (0.75, tall), (0.5625, tall), (0.5, tall), (0.4, tall))
    for aspect, want in expected:
        got = snap(aspect)
        check(abs(got - want) < 1e-9, "aspect %s snapped to %s, expected %s" % (aspect, got, want))
    check(snap(None) is None and snap(0) == 0, "no shape must pass through untouched")
    check(tuple(_pg.PICTURE_STANDARD_ASPECTS) == (wide, std, tall), "the governor table must hold 16:9, 4:3 and 9:16")
    for gone in ("_PICTURE_FIT_NEAR_169_MIN", "_PICTURE_FIT_NEAR_169_MAX", "_PICTURE_FIT_NEAR_43_MIN", "_PICTURE_FIT_NEAR_916_MIN", "_PICTURE_FIT_NEAR_916_MAX"):
        check(gone not in MAIN_SRC, "%s is still in the main script" % gone)
    check("performance_governor.PICTURE_STANDARD_ASPECTS" in MAIN_SRC, "the main script must read the governor table")

    blit_ns = {"pygame": pygame, "time": time, "_blit_video_log_state": {"key": None}, "_last_screen_mode_picture_rect": None,
               "_video_bar_color": lambda: (0, 0, 0), "_active_screen_mode": lambda: "normal",
               "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
    exec(main_function_code("_blit_video_with_screen_mode"), blit_ns)
    blit = blit_ns["_blit_video_with_screen_mode"]
    groups = {"16:9": ((1280, 720), (1920, 804), (1920, 1040), (1280, 534)), "4:3": ((640, 480), (720, 540), (1024, 768)), "9:16": ((1080, 1920), (720, 1280))}
    screens = ((1152, 864), (1024, 768), (1280, 1024), (1280, 800), (1280, 720), (1920, 1080), (2560, 1080))
    for win_w, win_h in screens:
        rect = pygame.Rect(0, 0, win_w, win_h)
        for label, sizes in groups.items():
            seen = set()
            for pw, ph in sizes:
                geom = {}
                blit(pygame.Surface((win_w, win_h)), pygame.Surface((pw, ph)), rect, mode="normal", geom_out=geom, fit_aspect=snap(pw / float(ph)))
                seen.add((geom["dest_x"], geom["dest_y"], geom["draw_w"], geom["draw_h"]))
                check(geom["draw_w"] >= win_w - 1 or geom["draw_h"] >= win_h - 1, "bars on both axes, %s on %dx%d" % (label, win_w, win_h))
            check(len(seen) == 1, "%s pictures got different boxes on %dx%d: %s" % (label, win_w, win_h, sorted(seen)))
            stretch = {}
            blit(pygame.Surface((win_w, win_h)), pygame.Surface(sizes[0]), rect, mode="stretch", geom_out=stretch, fit_aspect=snap(sizes[0][0] / float(sizes[0][1])))
            check((stretch["draw_w"], stretch["draw_h"]) == (win_w, win_h), "stretch must still fill %dx%d" % (win_w, win_h))
    for site in ("_video_sa = _snap_standard_aspect(", "_pv_fit_aspect = _snap_standard_aspect("):
        check(site in MAIN_SRC, "draw site missing: " + site)


def suite_picture_box_before_show():
    check(gov.PICTURE_BOX_PRE_SHOW_BUDGET_S == 8.0 and gov.PICTURE_BOX_PRE_SHOW_PARALLEL == 6, "the governor must hold the pre-show budget and parallel count")

    def fake_text(w, h, crop, dar=None):
        dar_text = (", DAR %d:%d" % dar) if dar else ""
        return "Input #0, matroska\n  Stream #0:0: Video: h264, yuv420p, %dx%d%s\n[Parsed_cropdetect_0] crop=%d:%d:%d:%d\n" % (w, h, dar_text, crop[0], crop[1], crop[2], crop[3])

    answers = {}

    def fake_sample(file_path, ffmpeg, t, timeout):
        time.sleep(0.05)
        return answers["text"]

    answers["text"] = fake_text(1920, 1080, (1440, 1080, 240, 0))
    with mock.patch.object(ml, "_pb_run_sample", side_effect=fake_sample), mock.patch.object(ml, "_resolve_ffmpeg", return_value="ffmpeg"):
        started = time.monotonic()
        serial = ml.detect_picture_box("a.mkv", duration=3000.0, parallel=1)
        serial_s = time.monotonic() - started
        started = time.monotonic()
        parallel = ml.detect_picture_box("a.mkv", duration=3000.0, parallel=6)
        parallel_s = time.monotonic() - started
    check(serial is not None and serial == parallel, "parallel scan must give the same box as the serial scan")
    check(serial["reason"] == "pillarbox" and abs(serial["picture_aspect"] - 4.0 / 3.0) < 0.01, "a 4:3 picture inside 16:9 must read as a pillarbox of 4:3: %s" % serial)
    check(parallel_s < serial_s * 0.7, "parallel samples must be faster: %.2f vs %.2f" % (parallel_s, serial_s))

    ns = {"os": os, "time": time, "threading": threading, "media": media, "ml_box": None}
    log_stub = type("L", (), {"info": staticmethod(lambda *a, **k: None), "warning": staticmethod(lambda *a, **k: None)})()
    store = {}

    class FakeLib:
        def get_cached(self, path):
            box = store.get(path)
            return {"picture_box": box, "picture_box_detector_version": ml.PICTURE_BOX_DETECTOR_VERSION} if box else None

    scans = []
    delay = {"s": 0.0}

    def fake_scan(lib, mlmod, path, parallel=1):
        scans.append(parallel)
        time.sleep(delay["s"])
        store[path] = {"reason": "letterbox", "frame": [1920, 1080], "pixel_aspect": 1.0, "picture_aspect": 1.78}
        return True

    ns.update({"log": log_stub, "_cached_path_exists": lambda p: True, "_get_probe_library": lambda: FakeLib(),
               "_picture_box_failures": {}, "_PICTURE_BOX_RETRY_BACKOFF_S": (600, 1800, 7200),
               "_is_drive_degraded": lambda p: False, "_picture_box_scan_one": fake_scan,
               "_PICTURE_BOX_PRE_SHOW_BUDGET_S": 0.5, "_PICTURE_BOX_PRE_SHOW_PARALLEL": 6})
    exec(main_function_code("_decide_picture_box_before_show"), ns)
    decide = ns["_decide_picture_box_before_show"]
    path = os.path.normpath(os.path.abspath("show.mkv"))

    box = decide("show.mkv")
    check(box is not None and scans == [6], "an unscanned file must be scanned with all samples at once before the show: %s" % scans)
    scans.clear()
    check(decide("show.mkv") == box and scans == [], "a file with a stored box must not be scanned again")
    store.clear()
    delay["s"] = 1.5
    started = time.monotonic()
    check(decide("show.mkv") is None, "a scan that misses the budget must give no box")
    check(time.monotonic() - started < 1.2, "the pre-show wait must stop at the budget")
    time.sleep(1.5)
    ns["_picture_box_failures"][path] = (3, 0.0)
    store.clear()
    scans.clear()
    check(decide("show.mkv") is None and scans == [], "a file out of retries must be played without a scan")
    check(decide("song.mp3") is None, "a non-video file must be skipped")

    play_src = MAIN_SRC
    gate_at = play_src.index("_pre_show_box = _decide_picture_box_before_show(filepath)")
    probe_at = play_src.index("if _src_dims is None and _norm_fp not in self._dim_probe_inflight:")
    geom_at = play_src.index("self._apply_screen_mode_geometry(False, _src_dims)")
    check(gate_at < probe_at < geom_at, "the picture box must be decided before the size probe and before the geometry is applied")
    frame_src = play_src[play_src.index("def get_display_frame(self):"):]
    check(frame_src.index("self._native_dims_pending and not self.is_dvd_iso") < frame_src.index("self.player.get_state()"),
          "frames must be withheld while the picture size is unknown")


def suite_random_start():
    import collections as _col
    raw = []
    for show in ("Rugrats", "Ben 10", "Real Monsters", "Hey Arnold"):
        for ep in range(1, 9):
            raw.append({"path": "/tv/%s/Season 1/%s S01E%02d.mkv" % (show, show, ep), "duration": 1320})
    shows = ("Rugrats", "Ben 10", "Real Monsters", "Hey Arnold")

    def build(order_seed, day_seed=20261012 + 105, fixed=None, pool=raw, mode="random_slots", pos=None):
        return media.build_show_rotation(pool, mode, dict(pos or {}), day_seed, episode_order_mode="sequential",
                                         fixed_show_order=fixed, block_duration_seconds=8 * 3600, channel_id="05",
                                         order_seed=order_seed)

    firsts = _col.Counter()
    for k in range(300):
        tracks, _pos, order = build("%d:100" % k)
        firsts[order[0]] += 1
    check(set(firsts) == set(shows) and min(firsts.values()) >= 40, "every show must start about equally often across fresh starts: %s" % dict(firsts))

    _t1, _p1, order_a = build("777:100", day_seed=1)
    _t2, _p2, order_b = build("777:100", day_seed=999999)
    check(order_a == order_b, "one start number must give the same order on every day")
    _t3, _p3, order_fixed = build("777:100", fixed=["Hey Arnold", "Rugrats"])
    check(order_fixed[:2] == ["Hey Arnold", "Rugrats"], "a saved show order must win over the start number")

    names = ["Rugrats", "Ben 10", "Real Monsters", "Hey Arnold"]
    expected = list(media.split_movies_and_series(raw, channel_id="05")[1])
    import random as _r
    _r.Random(12345).shuffle(expected)
    _t4, _p4, order_old = build(None, day_seed=12345)
    check(order_old == expected, "without a start number the date seed must behave as before")

    seq_a, _pa, _oa = build("1:100", fixed=names)
    seq_b, _pb, _ob = build("2:100", fixed=names)
    check([t["path"] for t in seq_a] == [t["path"] for t in seq_b], "the start number must not change episodes or slot counts once the order is fixed")
    counts_a = _col.Counter(os.path.basename(os.path.dirname(os.path.dirname(t["path"]))) for t in build("1:100")[0])
    counts_b = _col.Counter(os.path.basename(os.path.dirname(os.path.dirname(t["path"]))) for t in build("2:100")[0])
    check(counts_a == counts_b, "episodes per series in a block must not depend on the start number")

    movies = [{"path": "/movies/Movie%s.mkv" % c, "duration": 6000} for c in "ABCDE"]
    first_movies = set()
    for k in range(100):
        tracks, _pos, _order = build("%d:100" % k, pool=movies)
        first_movies.add(tracks[0]["path"])
    check(len(first_movies) >= 4, "the first movie must vary across fresh starts: %s" % sorted(first_movies))
    t_a = build("42:100", pool=movies)[0]
    t_b = build("42:100", pool=movies)[0]
    check([x["path"] for x in t_a] == [x["path"] for x in t_b], "one start number must give the same movies")

    with tempfile.TemporaryDirectory() as tmp:
        items = []
        for show in ("Alpha", "Bravo", "Charlie"):
            os.makedirs(os.path.join(tmp, show), exist_ok=True)
            for ep in range(1, 5):
                p = os.path.join(tmp, show, "%s S01E%02d.mkv" % (show, ep))
                open(p, "wb").close()
                items.append({"path": p, "duration": 1500})
        starters = _col.Counter()
        for k in range(60):
            lineup = media.build_ordered_tracks(items, "marathon", "%d:marathon" % k)
            names_in_order = [os.path.basename(os.path.dirname(x["path"])) for x in lineup]
            starters[names_in_order[0]] += 1
            runs = [key for key, _g in __import__("itertools").groupby(names_in_order)]
            check(len(runs) == 3 and len(lineup) == 12, "marathon must play each series through once, together")
            for show in ("Alpha", "Bravo", "Charlie"):
                eps = [os.path.basename(x["path"]) for x in lineup if os.path.basename(os.path.dirname(x["path"])) == show]
                check(eps == sorted(eps), "marathon episodes must stay in order")
        check(set(starters) == {"Alpha", "Bravo", "Charlie"}, "every series must start a marathon across fresh starts: %s" % dict(starters))
        fixed_seed = [media.build_ordered_tracks(items, "marathon", "9:marathon") for _ in range(3)]
        check(all([x["path"] for x in l] == [x["path"] for x in fixed_seed[0]] for l in fixed_seed), "one start number must give one marathon order")

    saved = []
    ns = {"threading": threading, "random": random, "isinstance": isinstance, "int": int, "bool": bool,
          "_intro_seed_lock": threading.Lock(),
          "db": type("D", (), {"save_settings_async": staticmethod(lambda: saved.append(1))})(),
          "log": type("L", (), {"warning": staticmethod(lambda *a, **k: None)})()}
    for name in ("_channel_intro_seed", "_start_order_seed", "_marathon_start_seed"):
        exec(main_function_code(name), ns)
    ch = {}
    first = ns["_channel_intro_seed"](ch)
    check(isinstance(first, int) and ch["playback_log"]["intro_seed"] == first and saved, "the start number must be saved with the channel")
    check(ns["_channel_intro_seed"](ch) == first, "the saved start number must be reused")
    check(ns["_start_order_seed"](ch, 100) != ns["_start_order_seed"](ch, 200), "blocks must not share one order seed")
    results = []
    threads = [threading.Thread(target=lambda: results.append(ns["_channel_intro_seed"]({"playback_log": ch["playback_log"]}))) for _ in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()
    check(set(results) == {first}, "every reader must get the same start number")
    ch.pop("playback_log")
    check(ns["_channel_intro_seed"](ch) != first, "clearing the channel must draw a new start number")
    check(len({ns["_channel_intro_seed"]({}) for _ in range(50)}) == 50, "fresh channels must draw different start numbers")

    for node in ast.walk(MAIN_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("build_show_rotation", "fast_forward_show_pos"):
            check(any(k.arg == "order_seed" for k in node.keywords), "%s at line %d must pass order_seed" % (node.func.id, node.lineno))
    check('"marathon", int(' not in MAIN_SRC, "marathon must not be seeded by the channel number")


def suite_caption_memory_guard():
    saved_read = gov._read_ram_gb
    try:
        gov._read_ram_gb = lambda: (8.0, 0.5)
        ok, total, avail, need = gov.caption_memory_check()
        check(not ok and need == gov.CAPTION_MODEL_MIN_AVAILABLE_RAM_GB, "0.5 GB free must be refused: %s" % ((ok, total, avail, need),))
        gov._read_ram_gb = lambda: (8.0, gov.CAPTION_MODEL_MIN_AVAILABLE_RAM_GB + 0.5)
        check(gov.caption_memory_check()[0], "enough free memory must be allowed")
        gov._read_ram_gb = lambda: (0.0, 0.0)
        check(gov.caption_memory_check()[0], "memory that cannot be read must not block captions")

        calls = []
        saved_gov, saved_handler = media._perf_governor, media._caption_memory_refusal_handler
        saved_model, saved_failed = media._whisper_model, media._whisper_model_load_failed
        try:
            media._perf_governor = gov
            media._whisper_model, media._whisper_model_load_failed = None, False
            media.set_caption_memory_refusal_handler(lambda t, a, n: calls.append((t, a, n)))
            gov._read_ram_gb = lambda: (4.0, 0.4)
            check(media._get_whisper_model() is None, "a refused load must return no model")
            check(len(calls) == 1 and calls[0][2] == gov.CAPTION_MODEL_MIN_AVAILABLE_RAM_GB, "the handler must be told once: %s" % calls)
            check(media._whisper_model_load_failed is False, "a memory refusal must not mark the model permanently failed")
            gov._read_ram_gb = lambda: (16.0, 8.0)
            check(media._caption_memory_allows_load() is True and len(calls) == 1, "enough memory must load without telling the handler")
        finally:
            media._perf_governor, media._caption_memory_refusal_handler = saved_gov, saved_handler
            media._whisper_model, media._whisper_model_load_failed = saved_model, saved_failed
    finally:
        gov._read_ram_gb = saved_read

    class Db:
        def __init__(self, config):
            self.config = config
            self.saves = 0

        def save_settings(self):
            self.saves += 1

    class Deck:
        refreshed = 0

        def refresh_from_config(self, config):
            Deck.refreshed += 1

    logs = []
    ns = {"performance_governor": gov, "log": type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                                             "warning": staticmethod(lambda *a, **k: logs.append(a))})(),
          "STATUS_BANNER_DURATION_MS": 30000, "_caption_prewarm_started": True, "_caption_prewarm_lock": threading.Lock(),
          "_caption_memory_refusal_pending": False, "_caption_memory_banner_pending": False, "game_deck": Deck(),
          "released": []}
    ns["_release_tv_caption_state_when_off"] = lambda reason="": ns["released"].append(reason)
    for name in ("_on_caption_memory_refused", "_apply_caption_memory_refusal", "_status_banner_is_active", "_status_banner_is_consumed",
                 "_mark_status_banner_consumed", "_status_banner_label", "_status_banner_duration_ms"):
        exec(main_function_code(name), ns)
    for const in ("CAPTION_MEMORY_BANNER_MS", "CAPTION_MEMORY_BANNER_LABEL"):
        exec(main_assignment_code(const), ns)
    ns["_drive_streak_signature"] = ns["_server_streak_signature"] = ns["_gamepad_streak_signature"] = ns["_missing_media_streak_signature"] = None
    ns["_server_streak_consumed"] = ns["_drive_streak_consumed"] = ns["_gamepad_streak_consumed"] = ns["_missing_media_streak_consumed"] = False
    ns["pygame"] = pygame

    check(ns["CAPTION_MEMORY_BANNER_MS"] == 5000, "banner is %s ms, want 5000" % ns["CAPTION_MEMORY_BANNER_MS"])
    check(ns["_status_banner_duration_ms"]("captions_memory") == 5000 and ns["_status_banner_duration_ms"]("drive") == 30000,
          "only the memory banner is 5 seconds")
    check("LACK OF MEMORY" in ns["_status_banner_label"]("captions_memory"), "banner text must say memory")

    db = Db({"tv_captions_enabled": True, "dvd_captions_enabled": True})
    ns["db"] = db
    ns["_apply_caption_memory_refusal"]()
    check(db.saves == 0 and not ns["_caption_memory_banner_pending"], "nothing happens before a refusal")
    ns["_on_caption_memory_refused"](4.0, 0.4, 1.5)
    ns["_apply_caption_memory_refusal"]()
    check(db.config == {"tv_captions_enabled": False, "dvd_captions_enabled": False} and db.saves == 1, "captions must be switched off and saved: %s" % db.config)
    check(ns["_caption_memory_banner_pending"] and ns["_status_banner_is_active"]("captions_memory"), "the banner must be raised")
    check(ns["_caption_prewarm_started"] is False and ns["released"], "the pre-warm must be re-armed and the caption state cleared")
    check(Deck.refreshed == 1, "the game deck must be refreshed")
    ns["_mark_status_banner_consumed"]("captions_memory")
    check(not ns["_status_banner_is_active"]("captions_memory"), "the banner ends once it has been shown")

    db2 = Db({"tv_captions_enabled": False})
    ns["db"] = db2
    ns["_on_caption_memory_refused"](4.0, 0.4, 1.5)
    ns["_apply_caption_memory_refusal"]()
    check(not ns["_caption_memory_banner_pending"], "no banner when captions were already off")

    check("_apply_caption_memory_refusal()" in main_function_source("_advance_status_banner_streaks"), "the refusal must be applied every frame")
    check('"captions_memory"' in main_function_source("_draw_status_banners"), "the draw loop must know the memory banner")
    check("set_caption_memory_refusal_handler(" in MAIN_SRC, "the main script must register the refusal handler")


def suite_real_menu_build():
    db = FakeDbConfig({"menu_opacity": 50, "fake_43_border_enabled": True, "bezel_style": 1})
    app_state = {"menu_layer": "TAB_SELECTION", "menu_tab_index": 0, "menu_selection_index": 0}
    for size in ((1280, 720), (1024, 768)):
        surface = pygame.Surface(size)
        try:
            stamp, pos = ui._build_settings_overlay_menu(surface, {}, app_state, db)
        except Exception as error:
            check(False, "real menu build failed at %s: %r" % (size, error))
            continue
        x, y, w, h = ui.menu_panel_rect(size[0], size[1], db)
        check(tuple(pos) == (x, y) and stamp.get_size() == (w, h), "real menu stamp %s %s != panel rect %s" % (stamp.get_size(), pos, (x, y, w, h)))


class TestMenuNavNameEntryAndExplorerRect(_SectionCase):
    """Name entry steps and the File Explorer panel matching the Main Menu rectangle"""

    def test_name_step_direct(self):
        self.run_section(suite_name_step_direct)

    def test_explorer_rect(self):
        self.run_section(suite_explorer_rect)

    def test_video_mode_rect(self):
        self.run_section(suite_video_mode_rect)

    def test_one_picture_path(self):
        self.run_section(suite_one_picture_path)

    def test_one_shape_per_label(self):
        self.run_section(suite_one_shape_per_label)

    def test_picture_box_before_show(self):
        self.run_section(suite_picture_box_before_show)

    def test_random_start(self):
        self.run_section(suite_random_start)

    def test_caption_memory_guard(self):
        self.run_section(suite_caption_memory_guard)

    def test_real_menu_build(self):
        self.run_section(suite_real_menu_build)


# ============================================================================
# SECTION: Loudness toggle (frame hold, visualizer resume, DVD file resume)
# ============================================================================


def loud_namespace(**extra):
    """A namespace holding the real loudness-toggle functions cut out of the main script, with stand-ins around them."""
    log = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    ns = {"log": log, "app_state": {}, "db": None, "game_deck": None, "performance_governor": gov, "time": time, "os": os}
    ns.update(extra)
    for name in ("_loudness_hold_last_frame_ms", "_loudness_hold_last_frame_active", "_loudness_retune_kind",
                 "_dvd_loudness_rebuild_plan", "_reload_visualizer_music_for_loudness_toggle"):
        exec(compile(get_function_source(name), "<extracted:%s>" % name, "exec"), ns)
    return ns


def loud_case_retune_kind():
    """Video channels reopen, the guide reopens its preview, the game channel, visualizer channels and a missing library leave playback alone."""
    db = types.SimpleNamespace(channels_db={"05": {}, "07": {"is_visualizer": True}})
    ns = loud_namespace(db=db)
    kind = ns["_loudness_retune_kind"]
    check(kind("05") == "video", "a video channel reopens the show")
    check(kind(5) == "video", "an integer channel number is zero padded")
    check(kind("04") == "guide", "the guide reopens its preview")
    check(kind("03") == "none", "the game channel is left alone")
    check(kind("07") == "none", "a visualizer channel is never retuned (the song would restart)")
    check(loud_namespace(db=None)["_loudness_retune_kind"]("05") == "none", "no library means no retune")


def loud_case_dvd_plan():
    """Only a plain video file playing in front is resumed; discs, disc images and background files are deferred; the rest rebuilds."""
    def plan(**attrs):
        deck = types.SimpleNamespace(**attrs) if attrs is not None else None
        return loud_namespace(game_deck=deck)["_dvd_loudness_rebuild_plan"]()
    base = dict(mode="DVD_PLAYER", dvd_file_path="x.mkv", dvd_playback_active=True, dvd_is_disc_image=False, dvd_iso_backgrounded=False)
    check(loud_namespace(game_deck=None)["_dvd_loudness_rebuild_plan"]() == "rebuild", "no deck rebuilds")
    check(plan(**dict(base, mode="GAME")) == "rebuild", "another deck mode rebuilds")
    check(plan(**dict(base, dvd_file_path=None)) == "rebuild", "no file rebuilds")
    check(plan(**dict(base, dvd_playback_active=False)) == "rebuild", "nothing playing rebuilds")
    check(plan(**base) == "resume", "a plain file playing in front resumes")
    check(plan(**dict(base, dvd_is_disc_image=True)) == "defer", "a disc or .iso is left untouched")
    check(plan(**dict(base, dvd_iso_backgrounded=True)) == "defer", "a backgrounded file is left untouched")


def loud_case_hold_window():
    """The held picture is active only until the governor's time has passed."""
    clock = [1000]
    pg = types.SimpleNamespace(time=types.SimpleNamespace(get_ticks=lambda: clock[0]))
    ns = loud_namespace(pygame=pg)
    ns["app_state"]["_hold_last_frame_until"] = 1000 + ns["_loudness_hold_last_frame_ms"]()
    check(ns["_loudness_hold_last_frame_active"](), "active right after the swap")
    clock[0] = 1000 + gov.LOUDNESS_HOLD_LAST_FRAME_MS - 1
    check(ns["_loudness_hold_last_frame_active"](), "still active just before the limit")
    clock[0] = 1000 + gov.LOUDNESS_HOLD_LAST_FRAME_MS
    check(not ns["_loudness_hold_last_frame_active"](), "over at the limit")
    check(not loud_namespace(pygame=pg)["_loudness_hold_last_frame_active"](), "never active when nothing set it")


class _LoudMusic(object):
    def __init__(self, busy=True, fail_load=False):
        self.busy, self.fail_load, self.calls, self.volume = busy, fail_load, [], None

    def get_busy(self):
        return self.busy

    def stop(self):
        self.calls.append("stop")

    def unload(self):
        self.calls.append("unload")

    def load(self, path):
        if self.fail_load:
            raise RuntimeError("cannot load")
        self.calls.append(("load", path))

    def set_volume(self, value):
        self.volume = value


def loud_run_visualizer(state, new_path, loaded_path="song.mp3", busy=True, fail_load=False, owner="live_visualizer", is_vis=True):
    """Runs the real visualizer reload; returns (result, app_state, music calls, seek handed to the song start, clock jump)."""
    music = _LoudMusic(busy, fail_load)
    seeks = []
    clock = [100.0]

    def monotonic():
        clock[0] += 0.4
        return clock[0]

    channel = types.SimpleNamespace(stop=lambda: music.calls.append("ch1stop"))
    pg = types.SimpleNamespace(mixer=types.SimpleNamespace(music=music, Channel=lambda n: channel))
    db = types.SimpleNamespace(channels_db={"07": {"is_visualizer": is_vis}})
    app = {"music_owner": owner, "current_channel": "07", "active_music_track_file": "song.mp3", "_music_loaded_playback_path": loaded_path}
    ns = loud_namespace(pygame=pg, db=db, app_state=app,
                        time=types.SimpleNamespace(monotonic=monotonic),
                        os=os,
                        _resolve_compressor_playback_path=lambda path, for_seek=False: new_path,
                        calculate_slotted_playback_state=lambda ch: state,
                        _lookup_file_gain=lambda ch, track: -3.0,
                        _effective_audio_gain=lambda min_db=None: 1.0,
                        _effective_loudness_gain=lambda gain: 1.0,
                        STACKED_GAIN_MIN_DB=-60, MUSIC_GAIN=1.0,
                        _start_visualizer_music=lambda track, seek: seeks.append(seek) or True)
    result = ns["_reload_visualizer_music_for_loudness_toggle"]()
    return result, app, music.calls, seeks


def loud_case_visualizer_resume():
    """The visualizer song is reloaded only when the mixer file must change, and restarts at the scheduled second plus the time the reload took."""
    state = {"mode": "Visualizer", "file": "song.mp3", "seek": 41.0, "duration": 210}
    result, app, calls, seeks = loud_run_visualizer(state, "song_leveled.mp3")
    check(result is True, "reloads when the file changes")
    check(("load", "song_leveled.mp3") in calls, "the new file is loaded")
    check(len(seeks) == 1 and 41.0 < seeks[0] < 43.0, "restarts about a second after the scheduled second, not from 0 (%r)" % seeks)
    check(app["_music_loaded_playback_path"] == "song_leveled.mp3", "the loaded path is remembered")
    result, app, calls, seeks = loud_run_visualizer(state, "song.mp3")
    check(result is False and seeks == [] and calls == [], "the same file is left playing: no stop, no restart")
    tail = dict(state, seek=209.5)
    result, app, calls, seeks = loud_run_visualizer(tail, "song_leveled.mp3")
    check(len(seeks) == 1 and seeks[0] <= 209.0, "the seek never passes the last second of the song (%r)" % seeks)
    check(loud_run_visualizer(dict(state, mode="Video"), "song_leveled.mp3")[0] is False, "a schedule that is not the Visualizer does nothing")
    check(loud_run_visualizer(dict(state, file="other.mp3"), "song_leveled.mp3")[0] is False, "a different scheduled song does nothing")
    check(loud_run_visualizer(state, "song_leveled.mp3", busy=False)[0] is False, "nothing playing does nothing")
    check(loud_run_visualizer(state, "song_leveled.mp3", owner="")[0] is False, "a song the Visualizer does not own is left alone")
    check(loud_run_visualizer(state, "song_leveled.mp3", is_vis=False)[0] is False, "a non-visualizer channel does nothing")
    result, app, calls, seeks = loud_run_visualizer(state, "song_leveled.mp3", fail_load=True)
    check(result is False and seeks == [] and app["music_owner"] == "", "a failed load hands the song back and starts nothing")


def loud_case_source_wiring():
    """The toggle site reloads the visualizer first and the rebuild path holds the frame only for video channels."""
    source = MAIN_SRC
    check("_reload_visualizer_music_for_loudness_toggle()" in source and "_rebuild_vlc_engines_for_loudness_toggle()" in source, "the toggle calls both")
    check("_run_pending_loudness_engine_swap()\n    _run_deferred_dvd_engine_rebuild()" in source, "the main loop runs the swap and the deferred DVD rebuild")
    swap = get_function_source("_rebuild_vlc_engines_for_loudness_toggle")
    check('if _retune_kind == "video":' in swap and "_hold_last_frame_until" in swap, "the frame hold is set only for a video retune")
    check("pygame.time.wait" not in swap, "no wait on the render thread")
    check("LOUDNESS_HOLD_LAST_FRAME_MS" in source and hasattr(gov, "LOUDNESS_HOLD_LAST_FRAME_MS"), "the hold time lives in the governor table (R7)")


class TestLoudnessToggle(_SectionCase):
    """Loudness toggle: held last frame, visualizer resume at the scheduled second, DVD file rules"""

    def test_retune_kind(self):
        self.run_section(loud_case_retune_kind)

    def test_dvd_plan(self):
        self.run_section(loud_case_dvd_plan)

    def test_hold_window(self):
        self.run_section(loud_case_hold_window)

    def test_visualizer_resume(self):
        self.run_section(loud_case_visualizer_resume)

    def test_source_wiring(self):
        self.run_section(loud_case_source_wiring)


# ============================================================================
# SECTION: Visualizer Equalizer Grid (smooth columns, no dot flicker)
# ============================================================================


def eq_case_ease():
    """The column smoothing covers the same share of the gap in the same time at 30, 60 and 144 frames a second."""
    import visualizers as vz
    tau = vz._eq_tunables()[0]
    expected = 1.0 - math.exp(-1.0 / tau)
    for fps in (30, 60, 144):
        value = 0.0
        for _ in range(fps):
            value = vz._eq_ease(value, 1.0, 1.0 / fps, tau)
        check(abs(value - expected) < 0.002, "one second at %d fps reaches %.4f, expected %.4f" % (fps, value, expected))
    check(vz._eq_ease(0.25, 1.0, 0.0, tau) == 0.25, "no time passed means no movement")
    check(vz._eq_ease(0.25, 1.0, 0.05, 0.0) == 1.0, "a zero smooth time snaps to the target")
    check(0.25 < vz._eq_ease(0.25, 1.0, 0.016, tau) < 1.0, "a normal step moves part of the way")


def eq_case_column():
    """A column level becomes whole dots plus a fade for the next dot, never fewer than one dot and never past the top."""
    import visualizers as vz
    col = vz._eq_column
    check(col(3.25, 13) == (3, 0.25), "3.25 is 3 dots and a quarter fade (%r)" % (col(3.25, 13),))
    check(col(3.0, 13) == (3, 0.0), "an exact step has no fade")
    check(col(0.0, 13) == (1, 0.0) and col(-5.0, 13) == (1, 0.0), "never fewer than one dot")
    check(col(0.4, 13) == (1, 0.0), "under one dot stays one full dot")
    check(col(13.0, 13) == (13, 0.0) and col(99.0, 13) == (13, 0.0), "never past the top dot, no fade there")
    whole, fade = col(12.9, 13)
    check(whole == 12 and abs(fade - 0.9) < 1e-9, "just under the top fades the top dot in (%r)" % ((whole, fade),))
    last = None
    for step in range(0, 1300):
        whole, fade = col(step / 100.0, 13)
        shown = whole + fade
        check(last is None or 0 <= shown - last < 0.011, "shown height rises smoothly at %.2f" % (step / 100.0))
        last = shown


def eq_run_deck(frames, fps, seed):
    """Drives the real Equalizer Grid with the main loop's own made-up level on a simulated clock; returns the shown height per column per frame, and the same bars shown with the old whole-dot rule."""
    import visualizers as vz
    clock = [1000.0]
    real_time = vz._time
    vz._time = types.SimpleNamespace(monotonic=lambda: clock[0] / 1000.0)
    random.seed(seed)
    try:
        deck = vz.VisualizerDeck()
        surface = pygame.Surface((960, 540))
        shown_rows, plain_rows = [], []
        max_dots = (540 - 10) // (max(4, 960 // 24 - 4) + 3)
        for _ in range(frames):
            clock[0] += 1000.0 / fps
            amp = 45 + int(math.sin(clock[0] * 0.045) * 45) + random.randint(0, 5)
            deck.render_active_engine(surface, {}, mock_amplitude=amp, style_override="EqualizerGrid")
            shown_rows.append([sum(vz._eq_column(b * max_dots, max_dots)) for b in deck.eq_bars])
            plain_rows.append([float(max(1, int(b * max_dots))) for b in deck.eq_bars])
        return shown_rows, plain_rows, max_dots
    finally:
        vz._time = real_time


def eq_jumps(rows, size):
    """Counts frame to frame changes of a column's shown height that are at least size dots, after the first 60 frames of climbing from empty."""
    return sum(1 for column in range(len(rows[0])) for k in range(61, len(rows)) if abs(rows[k][column] - rows[k - 1][column]) >= size)


def eq_motion(rows):
    """The average over columns of how far each column's shown height travels between its lowest and highest point."""
    spans = [max(row[c] for row in rows[60:]) - min(row[c] for row in rows[60:]) for c in range(len(rows[0]))]
    return sum(spans) / len(spans)


def eq_case_flicker():
    """On the real signal no column jumps a whole dot in one frame, the columns still visibly move, and the same signal gives the same picture."""
    for fps in (30, 60):
        shown, plain, max_dots = eq_run_deck(fps * 20, fps, seed=7)
        check(eq_jumps(plain, 1.0) > 100, "the old whole-dot rule jumps a dot in one frame often at %d fps (%d times), so the comparison means something" % (fps, eq_jumps(plain, 1.0)))
        check(eq_jumps(shown, 0.5) == 0, "%d fps: no column changes by half a dot in one frame (%d did)" % (fps, eq_jumps(shown, 0.5)))
        check(eq_motion(shown) > 0.4 * eq_motion(plain), "%d fps: the columns still move (%.2f dots against %.2f before)" % (fps, eq_motion(shown), eq_motion(plain)))
        check(all(1.0 <= v <= max_dots for row in shown for v in row), "%d fps: shown heights stay between 1 and %d" % (fps, max_dots))
    first, _, _ = eq_run_deck(600, 60, seed=3)
    second, _, _ = eq_run_deck(600, 60, seed=3)
    check(first == second, "the same signal gives the same picture")


def eq_case_fade_drawn():
    """Each column draws its whole dots plus one dimmer dot above them whose brightness follows the fade."""
    import visualizers as vz
    clock = [1000.0]
    real_time = vz._time
    vz._time = types.SimpleNamespace(monotonic=lambda: clock[0] / 1000.0)
    drawn = []
    real_rect = pygame.draw.rect
    pygame.draw.rect = lambda surf, color, rect, *a, **k: drawn.append((tuple(color), tuple(rect)))
    try:
        deck = vz.VisualizerDeck()
        surface = pygame.Surface((960, 540))
        for _ in range(180):
            clock[0] += 1000.0 / 60.0
            drawn.clear()
            deck.render_active_engine(surface, {}, mock_amplitude=60, style_override="EqualizerGrid")
    finally:
        pygame.draw.rect = real_rect
        vz._time = real_time
    h, bar_w = 540, 960 // 24
    dot_h, dot_w = max(4, bar_w - 4), max(4, bar_w - 6)
    cell_h = dot_h + 3
    max_dots = (h - 10) // cell_h
    peak_color = (255, 240, 80)
    faded_columns = 0
    for column in range(24):
        bar_x = column * bar_w + (bar_w - dot_w) // 2
        whole, fade = vz._eq_column(deck.eq_bars[column] * max_dots, max_dots)
        rects = [(c, r) for c, r in drawn if r[0] == bar_x and c != peak_color]
        check(len(rects) == whole + (1 if fade >= 0.04 else 0), "column %d draws %d dots plus the fade dot (fade %.2f), found %d" % (column, whole, fade, len(rects)))
        if fade >= 0.04:
            faded_columns += 1
            top = [c for c, r in rects if r[1] == h - dot_h - whole * cell_h]
            check(len(top) == 1, "column %d has one dot at the fade position" % column)
            row_frac = max(0.0, min(1.0, whole / float(max(1, max_dots))))
            if row_frac < 0.5:
                lo, hi, f = (255, 60, 170), (0, 210, 220), row_frac / 0.5
            else:
                lo, hi, f = (0, 210, 220), (210, 230, 60), (row_frac - 0.5) / 0.5
            row_color = tuple(int(lo[k] + (hi[k] - lo[k]) * f) for k in range(3))
            expected = tuple(int(1 + (c - 1) * fade) for c in row_color)
            check(bool(top) and top[0] == expected, "column %d fade dot is %r, expected %r" % (column, top[:1], expected))
    check(faded_columns > 0, "at least one column is mid-step, so the fade is exercised")


def eq_case_source():
    """The Equalizer Grid branch holds no old per-frame constants and the governor table holds the three numbers, matching the fallback."""
    import visualizers as vz
    with open(os.path.join(ROOT, "visualizers.py"), encoding="utf-8") as handle:
        source = handle.read()
    branch = source[source.index('elif style == "EqualizerGrid":'):source.index('elif style == "ParticleFlow":')]
    check("* 0.18" not in branch and "- 0.007" not in branch, "no per-frame smoothing or peak numbers left in the branch")
    check("int(self.eq_bars[bi] * max_dots)" not in branch.replace("peak_di = int(self.eq_peaks[bi] * max_dots)", ""), "the whole-dot rule is gone from the columns")
    check("_eq_column(" in branch and "_eq_ease(" in branch, "the branch uses both helpers")
    for name in ("EQ_GRID_SMOOTH_SECONDS", "EQ_GRID_PEAK_FALL_PER_SECOND", "EQ_GRID_MAX_STEP_SECONDS"):
        check(hasattr(gov, name), "governor has %s" % name)
    real = vz._eq_tunables()
    saved = vz._governor
    vz._governor = None
    try:
        fallback = vz._eq_tunables()
    finally:
        vz._governor = saved
    check(tuple(real) == tuple(fallback), "the import fallback %r matches the governor table %r" % (fallback, real))


class TestEqualizerGrid(_SectionCase):
    """Equalizer Grid: columns glide by elapsed time and the top dot fades instead of flickering"""

    def test_ease(self):
        self.run_section(eq_case_ease)

    def test_column(self):
        self.run_section(eq_case_column)

    def test_flicker(self):
        self.run_section(eq_case_flicker)

    def test_fade_drawn(self):
        self.run_section(eq_case_fade_drawn)

    def test_source(self):
        self.run_section(eq_case_source)


# ============================================================================
# SECTION: Comment rule (R4)
# ============================================================================

_R4_FILES = (
    "retro_tv_emulator.py", "game_deck.py", "libretro_core.py", "media.py", "media_library.py", "ui.py",
    "visualizers.py", "broadcast_clock.py", "performance_governor.py", os.path.join("assets", "menu_nav.py"),
    os.path.join("tests", "test_suite.py"),
    os.path.join("GPU", "d3d11_gpu_core.py"), os.path.join("GPU", "d3d11_gpu_frame.py"),
    os.path.join("GPU", "d3d11_gpu_gate.py"), os.path.join("GPU", "d3d11_gpu_reference.py"),
)
_R4_ALLOWED = re.compile(r"^#\s*(=+\s*$|SECTION:|VALUES CHANGE HERE|WARNING:|COMING SOON|-\*-|!)")
_R4_HISTORY = re.compile(r"\b(used to|no longer|any ?more|previously|formerly|the old (handler|code|flow|version)|item [A-D]\b)", re.I)
_R4_DOC_HISTORY = re.compile(r"\b(any ?more|previously|formerly|the old (handler|code|version)|item [A-D]\b)", re.I)


def _r4_comments(path):
    """Returns (line, text, previous line, next line) for every comment in a source file."""
    with open(path, "r", encoding="utf-8") as handle:
        src = handle.read()
    lines = src.split("\n")
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            i = tok.start[0] - 1
            prev_line = lines[i - 1].strip() if i > 0 else ""
            next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            found.append((tok.start[0], tok.string.strip(), prev_line, next_line))
    return src, found


def r4_case_comments():
    for rel in _R4_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        src, found = _r4_comments(path)
        for line_no, text, prev_line, next_line in found:
            check(bool(_R4_ALLOWED.match(text)), "%s line %d is a comment the rule does not allow: %s" % (rel, line_no, text[:80]))
            check(not _R4_HISTORY.search(text), "%s line %d comment says what used to be: %s" % (rel, line_no, text[:80]))
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    check(not _R4_DOC_HISTORY.search(doc), "%s docstring of %s says what used to be" % (rel, getattr(node, "name", "<module>")))
                    if not isinstance(node, ast.Module):
                        check("\n" not in doc.strip(), "%s docstring of %s is more than one line" % (rel, node.name))


class TestCommentRule(_SectionCase):
    """R4: comments are only section titles, warnings and VALUES CHANGE HERE tags; function docstrings are one line"""

    def test_comments(self):
        self.run_section(r4_case_comments)


# ============================================================================
# SECTION: DirectInput reader has no do-nothing branch
# ============================================================================


def dinput_dead_branch_case():
    with open(os.path.join(ROOT, "game_deck.py"), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    reader = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_try_dinput_read_all_impl")
    empty_ifs = [n for n in ast.walk(reader) if isinstance(n, ast.If) and len(n.body) == 1 and isinstance(n.body[0], ast.Pass) and not n.orelse]
    check(empty_ifs == [], "the DirectInput reader has no if-branch that only passes")
    used = [n for n in ast.walk(reader) if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "raw_changed_this_frame"]
    check(len(used) == 1, "the liveness block that uses raw_changed_this_frame is still there")
    check(any(isinstance(n, ast.Attribute) and n.attr == "add" for u in used for n in ast.walk(u)), "the liveness block still records ever-live pads")


class TestDirectInputNoDeadBranch(_SectionCase):
    """The DirectInput reader keeps its liveness block and has no do-nothing branch"""

    def test_no_dead_branch(self):
        self.run_section(dinput_dead_branch_case)


# ============================================================================
# SECTION: Broadcast clock numbers live in the governor table
# ============================================================================


def clock_table_case():
    import broadcast_clock as bc
    names = (("_RESYNC_SECONDS", "BROADCAST_CLOCK_RESYNC_SECONDS"),
             ("_STEP_TOLERANCE_SECONDS", "BROADCAST_CLOCK_STEP_TOLERANCE_SECONDS"),
             ("_JITTER_TOLERANCE_SECONDS", "BROADCAST_CLOCK_JITTER_TOLERANCE_SECONDS"))
    for clock_name, table_name in names:
        check(hasattr(gov, table_name), "%s is in the governor table" % table_name)
        check(getattr(bc, clock_name) == float(getattr(gov, table_name)), "%s follows %s" % (clock_name, table_name))
    with open(os.path.join(ROOT, "broadcast_clock.py"), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    fallback = {}
    for node in tree.body:
        if isinstance(node, ast.Try):
            for stmt in node.handlers[0].body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    fallback[stmt.targets[0].id] = stmt.value.value
    for clock_name, table_name in names:
        check(fallback.get(clock_name) == getattr(gov, table_name), "the fallback for %s matches %s" % (clock_name, table_name))
    top_level_numbers = [n.targets[0].id for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                         and isinstance(n.value.value, float) and isinstance(n.targets[0], ast.Name)]
    check(top_level_numbers == [], "broadcast_clock.py has no tunable number outside the governor table and its fallback: %s" % top_level_numbers)


class TestBroadcastClockTable(_SectionCase):
    """The broadcast clock's three tunables come from the governor table (R7)"""

    def test_clock_table(self):
        self.run_section(clock_table_case)


# ============================================================================
# SECTION: Stick activity numbers live in the governor table
# ============================================================================


def stick_activity_table_case():
    import menu_nav as mn
    import game_deck as gd
    for name in ("STICK_SS_ACTIVITY_DEADZONE", "STICK_MULTITAP_ACTIVITY_DEADZONE",
                 "STICK_CH03_AXIS_ACTIVITY_DEADZONE", "STICK_MENU_AXIS_THRESHOLD"):
        check(hasattr(gov, name), "%s is in the governor table" % name)
    check(mn.JOY_AXIS_THRESHOLD == float(gov.STICK_MENU_AXIS_THRESHOLD), "the menu joystick threshold follows the table")
    check(gd.ExternalGameDeck._MULTITAP_ACTIVITY_DZ == gov.STICK_MULTITAP_ACTIVITY_DEADZONE, "the multitap activity number follows the table")
    with open(os.path.join(ROOT, "game_deck.py"), "r", encoding="utf-8") as handle:
        deck_src = handle.read()
    check("_SS_STICK_ACTIVITY_DZ = _gov.STICK_SS_ACTIVITY_DEADZONE" in deck_src, "the screensaver stick number follows the table")
    with open(os.path.join(ROOT, "retro_tv_emulator.py"), "r", encoding="utf-8") as handle:
        main_src = handle.read()
    check("performance_governor.STICK_CH03_AXIS_ACTIVITY_DEADZONE" in main_src, "the Channel 03 axis wake number follows the table")
    check("8000 / 32767.0" in main_src and gov.STICK_CH03_AXIS_ACTIVITY_DEADZONE == 8000 / 32767.0, "its fallback matches the table")
    with open(os.path.join(ROOT, "menu_nav.py") if os.path.exists(os.path.join(ROOT, "menu_nav.py")) else os.path.join(ROOT, "assets", "menu_nav.py"), "r", encoding="utf-8") as handle:
        nav_src = handle.read()
    check("    JOY_AXIS_THRESHOLD = 0.6\n" in nav_src and gov.STICK_MENU_AXIS_THRESHOLD == 0.6, "its fallback matches the table")


class TestStickActivityTable(_SectionCase):
    """The stick activity and menu axis numbers come from the governor table (R7)"""

    def test_stick_activity_table(self):
        self.run_section(stick_activity_table_case)


def controller_read_table_case():
    import game_deck as gd
    expected = {"TRIGGER_PRESS_THRESHOLD": 30, "GAME_HOTKEY_TRIGGER_THRESHOLD": 128, "GAME_HOTKEY_COOLDOWN_MS": 2000,
                "DINPUT_AXIS_LIVENESS_THRESHOLD": 1500, "HID_IDENTITY_CACHE_TTL_SECONDS": 2.0,
                "DINPUT_FRAME_CACHE_WINDOW_SECONDS": 0.008, "JS_DEAD_TIMEOUT_SECONDS": 5.0, "XI_NAV_POLL_HZ": 30,
                "XI_NAV_NO_PAD_INTERVAL_SECONDS": 0.25, "XI_NAV_MISSES_BEFORE_SLOW": 30}
    for name, value in expected.items():
        check(getattr(gov, name, None) == value, "%s is in the governor table with the number the code had (%s)" % (name, value))
    check(gd._DINPUT_AXIS_LIVENESS_THRESHOLD == gov.DINPUT_AXIS_LIVENESS_THRESHOLD, "the liveness number follows the table")
    check(gd._HID_IDENTITY_CACHE_TTL == gov.HID_IDENTITY_CACHE_TTL_SECONDS, "the identity cache time follows the table")
    check(gd._DINPUT_FRAME_CACHE_WINDOW == gov.DINPUT_FRAME_CACHE_WINDOW_SECONDS, "the DirectInput frame cache follows the table")
    check(gd._JS_DEAD_TIMEOUT == gov.JS_DEAD_TIMEOUT_SECONDS, "the Linux joystick timeout follows the table")
    check((gd._XI_NAV_POLL_HZ, gd._XI_NAV_NO_PAD_INTERVAL, gd._XI_NAV_MISSES_BEFORE_SLOW)
          == (gov.XI_NAV_POLL_HZ, gov.XI_NAV_NO_PAD_INTERVAL_SECONDS, gov.XI_NAV_MISSES_BEFORE_SLOW), "the menu pad polling follows the table")
    with open(gd.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    local_names = {"TRIG_THRESH", "TRIGGER_PRESS_THRESHOLD", "_TRIG_THRESH", "_COOLDOWN_MS", "_DINPUT_AXIS_LIVENESS_THRESHOLD",
                   "_HID_IDENTITY_CACHE_TTL", "_DINPUT_FRAME_CACHE_WINDOW", "_JS_DEAD_TIMEOUT", "_XI_NAV_POLL_HZ",
                   "_XI_NAV_NO_PAD_INTERVAL", "_XI_NAV_MISSES_BEFORE_SLOW"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in local_names:
            seen.add(node.targets[0].id)
            check(not isinstance(node.value, ast.Constant), "%s is assigned a literal number in game_deck.py" % node.targets[0].id)
    check(seen == local_names, "every moved name is still assigned from the table: missing %s" % sorted(local_names - seen))


class TestControllerReadTable(_SectionCase):
    """The trigger, pad-liveness and pad-polling numbers come from the governor table (R7)"""

    def test_controller_read_table(self):
        self.run_section(controller_read_table_case)


# ============================================================================
# SECTION: Stick settings are kept per player
# ============================================================================


def stick_per_player_case():
    import game_deck as gd
    names = ("deadzone", "sensitivity", "axis_threshold")
    check(gd.stick_config_key("stick_deadzone_percent", 1) == "stick_deadzone_percent", "Player 1 keeps the plain key")
    check(gd.stick_config_key("stick_deadzone_percent", 3) == "stick_deadzone_percent_p3", "Players 2 to 4 add _pN")
    cfg = {}
    for player in gd.STICK_PLAYERS:
        check(gd.stick_switch_on(cfg, player) is gov.STICK_DZ_ENABLED_DEFAULT, "player %d starts with the default switch" % player)
    gd.adjust_stick_setting(cfg, "deadzone", 1, 2)
    gd.adjust_stick_setting(cfg, "sensitivity", 1, 3)
    gd.toggle_stick_switch(cfg, 4)
    check(gd.stick_setting_percent(cfg, "deadzone", 2) == 1 and gd.stick_setting_percent(cfg, "deadzone", 1) == 0, "a change to Player 2 leaves Player 1 alone")
    check(gd.stick_setting_percent(cfg, "sensitivity", 3) == 105 and gd.stick_setting_percent(cfg, "sensitivity", 2) == 100, "a change to Player 3 leaves Player 2 alone")
    check(gd.stick_switch_on(cfg, 4) is (not gov.STICK_DZ_ENABLED_DEFAULT) and gd.stick_switch_on(cfg, 1) is gov.STICK_DZ_ENABLED_DEFAULT, "the switch is per player")
    gd.reset_stick_defaults(cfg, 2)
    check(gd.stick_setting_percent(cfg, "deadzone", 2) == gov.STICK_DZ_SPECS["deadzone"][1], "reset acts on one player")
    check(gd.stick_setting_percent(cfg, "sensitivity", 3) == 105, "reset of Player 2 leaves Player 3's values")
    old = {gov.STICK_DZ_SPECS[n][0]: v for n, v in (("deadzone", 30), ("sensitivity", 120), ("axis_threshold", 40))}
    check(gd.stick_settings_from_config(old, 1).deadzone == 0.30, "an old saved config still drives Player 1")
    check(gd.stick_settings_from_config(old, 2).deadzone == 0.0, "an old saved config does not leak into Player 2")

    previous = {p: gd.current_stick_settings(p) for p in gd.STICK_PLAYERS}
    saved_slots = dict(gd._player_slot_of)
    try:
        cfg = {gov.STICK_DZ_ENABLED_KEY: True, gd.stick_config_key(gov.STICK_DZ_SPECS["deadzone"][0], 2): 50,
               gd.stick_config_key(gov.STICK_DZ_SPECS["axis_threshold"][0], 2): 80,
               gd.stick_config_key(gov.STICK_DZ_ENABLED_KEY, 3): False}
        gd.apply_stick_config(cfg)
        check(gd.current_stick_settings(2).deadzone == 0.5 and gd.current_stick_settings(1).deadzone == 0.0, "apply keeps four separate settings")
        check(gd.stick_axis_threshold_raw(2) == int(0.8 * gd.STICK_MAX), "Player 2's threshold is its own")
        check(gd.stick_axis_threshold_raw(1) == int(0.5 * gd.STICK_MAX), "Player 1's threshold is its own")
        check(gd.stick_axis_threshold_raw(3) == gov.STICK_DZ_OFF_DPAD_THRESHOLD and gd.stick_dinput_threshold_raw(3) == gov.STICK_DZ_OFF_DINPUT_THRESHOLD, "Player 3 with the switch off keeps the fixed numbers")
        gd._player_slot_of.clear()
        gd._player_slot_of.update({1: 0, 2: 1, 3: "di7"})
        check(gd.stick_player_of_slot(1) == 2 and gd.stick_player_of_slot("di7") == 3 and gd.stick_player_of_slot(9) == 1, "a pad slot belongs to its player; an unassigned pad counts as Player 1")

        def pad(lx, ly):
            g = gd._GPState()
            g.sLX, g.sLY = lx, ly
            return g

        g1 = gd.apply_stick_correction(pad(10000, 0), 0)
        g2 = gd.apply_stick_correction(pad(10000, 0), 1)
        g3 = gd.apply_stick_correction(pad(10000, 0), "di7")
        check(g1.sLX == 10000, "the pad of Player 1 (no deadzone) is left as read")
        check(g2.sLX == 0 and g2.rawLX == 10000, "the pad of Player 2 is cut by Player 2's deadzone and keeps its raw value")
        check(g3.sLX == 10000, "the pad of Player 3 (switch off) is left as read")
        gd._player_slot_of[2] = "js:/dev/input/js0"
        reader = gd._LinuxJoystickReader.__new__(gd._LinuxJoystickReader)
        reader._path, reader._fd, reader._state = "/dev/input/js0", None, gd._GPState()
        reader._fd = os.open(os.devnull, os.O_RDONLY)
        reader._state.sLX = 10000
        try:
            with mock.patch.object(gd.os, "read", side_effect=BlockingIOError):
                lg = reader.read()
        finally:
            os.close(reader._fd)
        check(lg is not None and lg.sLX == 0 and lg.rawLX == 10000, "a Linux (Wine) joystick follows the settings of the player it is assigned to")
    finally:
        gd._player_slot_of.clear()
        gd._player_slot_of.update(saved_slots)
        for p, st in previous.items():
            gd._stick_current[p] = st


class TestStickPerPlayer(_SectionCase):
    """Stick deadzone settings are kept for each of the four players and every stick read uses the owning player's"""

    def test_stick_per_player(self):
        self.run_section(stick_per_player_case)


# ============================================================================
# SECTION: The DVD and Games Aspect Ratio settings are gone
# ============================================================================


def aspect_keys_gone_case():
    import media as _media
    for gone in ("dvd_aspect_ratio", "game_aspect_ratio", "_apply_bezel_on_aspect_lock", "_restore_aspect_ratio_after_bezel_off",
                 "_aspect_ratio_locked_by_bezel", "_before_bezel_boot_aspect", "_is_true_43_hardware"):
        check(gone not in MAIN_SRC, "%s is gone from the main script" % gone)
    with open(_media.__file__, encoding="utf-8") as handle:
        media_src = handle.read()
    check("_aspect_ratio_before_bezel" not in media_src.replace("\"_aspect_ratio_before_bezel\"", "").replace("_dvd_aspect_ratio_before_bezel", "").replace("_game_aspect_ratio_before_bezel", ""), "media.py no longer restores aspect_ratio from a stash entry")
    check("game_aspect_ratio\"] = " not in media_src and "\"dvd_aspect_ratio\": " not in media_src, "media.py no longer sets either key")
    db = _media.RetroDatabase.__new__(_media.RetroDatabase)
    config = db._apply_config_defaults({"aspect_ratio": "4:3", "dvd_aspect_ratio": "4:3", "game_aspect_ratio": "4:3",
                                        "_aspect_ratio_before_bezel": "4:3", "_dvd_aspect_ratio_before_bezel": "4:3", "_game_aspect_ratio_before_bezel": "16:9"})
    check(not any(k in config for k in ("dvd_aspect_ratio", "game_aspect_ratio", "_aspect_ratio_before_bezel", "_dvd_aspect_ratio_before_bezel", "_game_aspect_ratio_before_bezel")),
          "an old saved config loses the five old keys")
    check(config["aspect_ratio"] == "16:9", "the main aspect ratio is still forced to 16:9")
    block_start = MAIN_SRC.index('self.target_aspect = "16:9"')
    check('engine_label == "dvd"' in MAIN_SRC[block_start - 120:block_start], "the DVD engine's target aspect is the fixed 16:9")


class TestAspectKeysGone(_SectionCase):
    """The unused DVD and Games Aspect Ratio keys and the dead Bezel aspect helpers are removed"""

    def test_aspect_keys_gone(self):
        self.run_section(aspect_keys_gone_case)


# ============================================================================
# SECTION: Game deck backgrounds and splash images fit the canvas
# ============================================================================


def canvas_fit_case():
    import game_deck as gd
    with open(os.path.join(ROOT, "game_deck.py"), "r", encoding="utf-8") as handle:
        src = handle.read()
    check("_cover_scale_bg" not in src, "the cropping background scaler is gone")
    check(src.count("_scale_to_canvas(") >= 4, "the menu background, console splash and DVD splash all use the canvas fit")
    for w, h in ((640, 480), (1152, 864), (1280, 720), (500, 900)):
        src_surf = pygame.Surface((160, 90))
        src_surf.fill((10, 20, 30))
        src_surf.set_at((0, 0), (255, 0, 0))
        src_surf.set_at((159, 0), (0, 255, 0))
        src_surf.set_at((0, 89), (0, 0, 255))
        src_surf.set_at((159, 89), (255, 255, 0))
        big = pygame.transform.scale(src_surf, (1600, 900))
        out = gd._scale_to_canvas(big, w, h)
        check(out.get_size() == (w, h), "%dx%d: the picture is scaled to exactly the canvas" % (w, h))
        for name, pos, want in (("top left", (1, 1), (255, 0, 0)), ("top right", (w - 2, 1), (0, 255, 0)),
                                ("bottom left", (1, h - 2), (0, 0, 255)), ("bottom right", (w - 2, h - 2), (255, 255, 0))):
            got = tuple(out.get_at(pos))[:3]
            check(sum(abs(a - b) for a, b in zip(got, want)) < 400 and max(got) > 60, "%dx%d: %s corner of the picture is still shown (no crop): %s" % (w, h, name, got))
        check(tuple(out.get_at((w // 2, h // 2)))[:3] != (0, 0, 0), "%dx%d: no black bars in the middle" % (w, h))
        for edge_pos in ((0, h // 2), (w - 1, h // 2), (w // 2, 0), (w // 2, h - 1)):
            check(tuple(out.get_at(edge_pos))[:3] != (0, 0, 0), "%dx%d: no black bar at edge %s" % (w, h, edge_pos))
    check(gd._scale_to_canvas(pygame.Surface((8, 8)), 0, 0).get_size() == (1, 1), "a zero-size canvas gives one pixel, not a crash")

    class _Deck:
        dvd_splash_enabled = True
        DVD_LOAD_SCREEN_MS = 1000

        def _ensure_fonts(self):
            pass

        def _get_dvd_custom_splash_surface(self):
            pic = pygame.Surface((160, 90))
            pic.fill((200, 50, 50))
            return pic

    for w, h in ((640, 480), (1152, 864)):
        screen = pygame.Surface((w, h))
        screen.fill((0, 0, 0))
        gd.ExternalGameDeck._render_dvd_load_screen(_Deck(), screen, pygame.Rect(0, 0, w, h))
        for pos in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 2), (w // 2, h - 3)):
            check(tuple(screen.get_at(pos))[:3] != (0, 0, 0), "%dx%d: the DVD splash image fills the screen with no bars at %s" % (w, h, pos))


class TestGameDeckCanvasFit(_SectionCase):
    """Menu background, console splash image and DVD splash image are scaled to the whole canvas, never cropped and never letterboxed"""

    def test_canvas_fit(self):
        self.run_section(canvas_fit_case)


# ============================================================================
# SECTION: Game and DVD picture box follows the TV station rule
# ============================================================================


def game_dvd_picture_box_case():
    import game_deck as gd
    for gone in ("_dvd_pillarbox_active", "_game_pillarbox_active"):
        check(gone not in MAIN_SRC, "%s is still in the main script" % gone)
    screens = ((1152, 864), (1024, 768), (1280, 1024), (1280, 800), (1280, 720), (1920, 1080), (2560, 1080))
    pictures = ((16, 9), (4, 3), (9, 16), (21, 9), (5, 4))
    for bezel in (False, True):
        for ratio in ("16:9", "4:3"):
            config = {"fake_43_border_enabled": bezel, "aspect_ratio": ratio}
            ns = {"pygame": pygame, "db": FakeDbConfig(config), "_PICTURE_STANDARD_ASPECTS": (16 / 9, 4 / 3, 9 / 16), "math": math}
            for name in ("_compute_dvd_content_rect", "_compute_game_content_rect", "_dvd_bezel_video_fit", "_snap_standard_aspect"):
                exec(main_function_code(name), ns)
            for w, h in screens:
                check(tuple(ns["_compute_dvd_content_rect"](w, h)) == (0, 0, w, h), "%dx%d bezel=%s %s: the DVD canvas is not the whole window" % (w, h, bezel, ratio))
                check(tuple(ns["_compute_game_content_rect"](w, h)) == (0, 0, w, h), "%dx%d bezel=%s %s: the game canvas is not the whole window" % (w, h, bezel, ratio))
    ns = {"pygame": pygame, "db": FakeDbConfig({"fake_43_border_enabled": False}), "_PICTURE_STANDARD_ASPECTS": (16 / 9, 4 / 3, 9 / 16), "math": math}
    for name in ("_dvd_bezel_video_fit", "_snap_standard_aspect"):
        exec(main_function_code(name), ns)
    blit_ns = {"pygame": pygame, "time": time, "_blit_video_log_state": {"key": None}, "_last_screen_mode_picture_rect": None,
               "_video_bar_color": lambda: (0, 0, 0), "_active_screen_mode": lambda: "normal",
               "log": type("L", (), {"info": staticmethod(lambda *a, **k: None)})()}
    exec(main_function_code("_blit_video_with_screen_mode"), blit_ns)
    dvd_blit = blit_ns["_blit_video_with_screen_mode"]

    def bars_of(geom, w, h):
        sides = geom["dest_x"] > 1 or w - (geom["dest_x"] + geom["draw_w"]) > 1
        tops = geom["dest_y"] > 1 or h - (geom["dest_y"] + geom["draw_h"]) > 1
        return sides, tops

    for w, h in screens:
        for pw, ph in pictures:
            surf = pygame.Surface((pw * 40, ph * 40))
            label = "%dx%d screen, %d:%d picture" % (w, h, pw, ph)
            for mode in ("normal", "stretch"):
                screen = pygame.Surface((w, h))
                geom = {}
                rect, pa = ns["_dvd_bezel_video_fit"](pygame.Rect(0, 0, w, h), w, h, ns["_snap_standard_aspect"](pw / ph), mode)
                check(tuple(rect) == (0, 0, w, h), "DVD %s: the fit rect is not the whole window" % label)
                dvd_blit(screen, surf, rect, mode=mode, geom_out=geom, pixel_aspect=pa, fit_aspect=ns["_snap_standard_aspect"](pw / ph))
                sides, tops = bars_of(geom, w, h)
                check(not (sides and tops), "DVD %s in %s has bars on both axes: %s" % (label, mode, geom))
                if mode == "stretch":
                    check(geom["draw_w"] >= w and geom["draw_h"] >= h, "DVD %s in Stretch does not fill the screen: %s" % (label, geom))
            for mode in ("normal", "stretch"):
                screen = pygame.Surface((w, h))
                screen.fill((0, 0, 0))
                frame = pygame.Surface((pw * 40, ph * 40))
                frame.fill((200, 200, 200))
                gd._libretro_blit_scaled(screen, frame, pygame.Rect(0, 0, w, h), mode=mode)
                lit = [(x, y) for x in range(0, w, 4) for y in range(0, h, 4) if screen.get_at((x, y))[0] > 100]
                xs = [p[0] for p in lit]
                ys = [p[1] for p in lit]
                left, right, top, bottom = min(xs), w - 1 - max(xs), min(ys), h - 1 - max(ys)
                side_bars = left > 6 or right > 6
                top_bars = top > 6 or bottom > 6
                check(not (side_bars and top_bars), "Game %s in %s has bars on both axes: %s" % (label, mode, (left, right, top, bottom)))
                if mode == "stretch":
                    check(not side_bars and not top_bars, "Game %s in Stretch does not fill the screen: %s" % (label, (left, right, top, bottom)))
                else:
                    drawn = (max(xs) - min(xs) + 4) / float(max(ys) - min(ys) + 4)
                    check(abs(drawn - pw / ph) < 0.05, "Game %s in Normal is distorted: %.3f" % (label, drawn))


def game_no_second_box_case():
    import game_deck as gd
    screens = ((1152, 864), (1024, 768), (1280, 1024), (1280, 800), (1280, 720), (1920, 1080), (2560, 1080))
    frames = {"NES": (256, 240), "SNES": (256, 224), "GENESIS": (320, 224), "PSX": (320, 240), "N64": (640, 480),
              "GBA": (240, 160), "GB": (160, 144), "ARCADE43": (1024, 768)}
    four_three = {"PSX", "N64", "ARCADE43", "NES", "SNES"}
    check(gov.GAME_DISPLAY_ASPECTS == {"NES": 4.0 / 3.0, "SNES": 4.0 / 3.0}, "the governor table makes NES and SNES 4:3 and lists no other console")

    def shape_of(name, fw, fh):
        return gov.GAME_DISPLAY_ASPECTS.get(name, fw / float(fh))

    def lit_box(screen, w, h):
        xs = [x for x in range(w) if screen.get_at((x, h // 2))[0] > 100]
        ys = [y for y in range(h) if screen.get_at((w // 2, y))[0] > 100]
        return xs[0], w - 1 - xs[-1], ys[0], h - 1 - ys[-1], xs[-1] - xs[0] + 1, ys[-1] - ys[0] + 1

    deck = gd.ExternalGameDeck.__new__(gd.ExternalGameDeck)
    deck._libretro_border_enabled = False
    deck._libretro_border_surf = None
    for mode in ("normal", "stretch"):
        with mock.patch.object(gd, "_active_game_screen_mode", lambda mode=mode: mode):
            for w, h in screens:
                for name, (fw, fh) in frames.items():
                    deck._libretro_console = name
                    label = "%dx%d screen, %s frame %dx%d, %s" % (w, h, name, fw, fh, mode)
                    screen = pygame.Surface((w, h))
                    screen.fill((0, 0, 0))
                    frame = pygame.Surface((fw, fh))
                    frame.fill((200, 200, 200))
                    deck._blit_libretro_frame_impl(screen, frame, pygame.Rect(0, 0, w, h))
                    left, right, top, bottom, drawn_w, drawn_h = lit_box(screen, w, h)
                    side_bars = left > 2 or right > 2
                    top_bars = top > 2 or bottom > 2
                    check(not (side_bars and top_bars), "%s: bars on both axes %s" % (label, (left, right, top, bottom)))
                    if mode == "stretch":
                        check(not side_bars and not top_bars, "%s: does not fill the screen %s" % (label, (left, right, top, bottom)))
                    else:
                        want = shape_of(name, fw, fh)
                        check(abs(drawn_w / float(drawn_h) - want) < 0.02, "%s: drawn shape %.3f, wanted %.3f" % (label, drawn_w / float(drawn_h), want))
                        check(side_bars or top_bars or abs(w / float(h) - want) < 0.02, "%s: no bars but the shape differs" % label)
                        if (w, h) in ((1152, 864), (1024, 768)) and name in four_three:
                            check(not side_bars and not top_bars, "%s: a 4:3 picture does not fill a 4:3 screen %s" % (label, (left, right, top, bottom)))
    with mock.patch.object(gd, "_active_game_screen_mode", lambda: "normal"):
        for name, fw, fh, want in (("SNES", 256, 224, (1152, 864)), ("NES", 256, 240, (1152, 864)), ("GENESIS", 320, 224, (1152, 806)), ("PSX", 320, 240, (1152, 864))):
            deck._libretro_console = name
            screen = pygame.Surface((1152, 864))
            screen.fill((0, 0, 0))
            frame = pygame.Surface((fw, fh))
            frame.fill((200, 200, 200))
            deck._blit_libretro_frame_impl(screen, frame, pygame.Rect(0, 0, 1152, 864))
            check(lit_box(screen, 1152, 864)[4:] == want, "%s %dx%d draws %s on a 1152x864 screen, got %s" % (name, fw, fh, want, lit_box(screen, 1152, 864)[4:]))
        snes_w, snes_h = gd._game_layout_shape("SNES", 256, 224)
        check(snes_h == 224.0 and abs(snes_w / snes_h - 4.0 / 3.0) < 1e-9, "the SNES layout shape is 4:3 of its frame height")
        check(gd._game_layout_shape("GENESIS", 320, 224) == (320.0, 224.0) and gd._game_layout_shape(None, 320, 224) == (320.0, 224.0), "a console not in the table keeps its frame shape")

        deck._libretro_border_enabled = True
        deck._libretro_border_path = None
        deck._border_blit_cache = None
        deck._libretro_border_rect_frac = (0.1, 0.1, 0.8, 0.8)
        for name, fw, fh, fills in (("SNES", 256, 224, True), ("NES", 256, 240, True), ("PSX", 320, 240, True), ("GENESIS", 320, 224, False)):
            deck._libretro_console = name
            deck._border_blit_cache = None
            deck._libretro_border_surf = pygame.Surface((1152, 864), pygame.SRCALPHA)
            screen = pygame.Surface((1152, 864))
            screen.fill((0, 0, 0))
            frame = pygame.Surface((fw, fh))
            frame.fill((200, 200, 200))
            deck._blit_libretro_frame_impl(screen, frame, pygame.Rect(0, 0, 1152, 864))
            inner_x, inner_y = 115, 86
            inner_w, inner_h = 921, 691
            corners = [screen.get_at((inner_x + 3, inner_y + 3))[0], screen.get_at((inner_x + inner_w - 4, inner_y + inner_h - 4))[0]]
            if fills:
                check(all(c > 100 for c in corners), "%s %dx%d fills the 4:3 console bezel window, corners %s" % (name, fw, fh, corners))
            else:
                check(screen.get_at((inner_x + 3, inner_y + inner_h // 2))[0] < 100 or screen.get_at((inner_x + inner_w // 2, inner_y + 3))[0] < 100,
                      "%s %dx%d keeps its own shape inside the console bezel window" % (name, fw, fh))
        deck._libretro_border_enabled = False

    ns = {"pygame": pygame, "db": FakeDbConfig({"fake_43_border_enabled": True, "aspect_ratio": "16:9"}), "_real_screen_matches_aspect": lambda *a, **k: False,
          "_selected_package_aspect": lambda: 16 / 9}
    exec(main_function_code("_package_pillarbox_active"), ns)
    for w, h in screens:
        check(ns["_package_pillarbox_active"](w, h) is False, "%dx%d: with Bezel on the package box is never cut out of a game canvas" % (w, h))

    with open(gd.__file__, encoding="utf-8") as handle:
        deck_tree = ast.parse(handle.read())
    for name in ("_blit_libretro_frame_impl", "_render_libretro_impl"):
        node = next(n for c in ast.walk(deck_tree) if isinstance(c, ast.ClassDef) and c.name == "ExternalGameDeck"
                    for n in c.body if isinstance(n, ast.FunctionDef) and n.name == name)
        called = {x.func.id for x in ast.walk(node) if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)}
        called |= {x.func.attr for x in ast.walk(node) if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)}
        for banned in ("_snap_standard_aspect", "_compute_content_rect", "_video_screen_mode_rect", "_dvd_fit_aspect", "_dvd_video_fit", "_aspect_fit_blit"):
            check(banned not in called, "%s calls %s, which would add a second box to a game" % (name, banned))
    check("else _compute_game_content_rect(*screen.get_size())" in MAIN_SRC, "the Channel 03 draw hands a game the whole-window canvas")
    check(MAIN_SRC.count("_compute_game_content_rect(") == 2, "the game canvas is computed in one place only")


class TestGameDvdPictureBox(_SectionCase):
    """Game and DVD pictures are boxed exactly like a TV station: the whole window, one fit, bars on one axis only"""

    def test_game_dvd_picture_box(self):
        self.run_section(game_dvd_picture_box_case)

    def test_game_no_second_box(self):
        self.run_section(game_no_second_box_case)


# ============================================================================
# SECTION: Stick deadzone settings and maths
# ============================================================================


def stick_case_maths_and_settings():
    import game_deck as gd
    key_on = gov.STICK_DZ_ENABLED_KEY
    check(gd.correct_stick_pair(0, 0, 0.0, 1.0) == (0, 0), "centre stays centre")
    check(gd.correct_stick_pair(20000, -12345, 0.0, 1.0) == (20000, -12345), "no deadzone and sensitivity 1 leaves a stick as it is")
    check(gd.correct_stick_pair(3000, 3000, 0.2, 1.0) == (0, 0), "inside the circle gives zero")
    check(gd.correct_stick_pair(32767, 0, 0.2, 1.0) == (32767, 0), "full push still reaches full range")
    check(gd.correct_stick_pair(-32767, 0, 0.2, 1.0) == (-32767, 0), "full push left still reaches full range")
    x, y = gd.correct_stick_pair(20000, 20000, 0.2, 1.0)
    check(x == y and 0 < x < 20000, "diagonal keeps its angle and is rescaled")
    check(gd.correct_stick_pair(10000, 0, 0.0, 2.0) == (20000, 0), "sensitivity 2 doubles")
    check(gd.correct_stick_pair(30000, 0, 0.0, 2.0) == (32767, 0), "sensitivity result is clamped")
    check(gd.correct_stick_pair(99999, -99999, 0.0, 1.0) == (32767, -32767), "input past full range is clamped to each axis limit")

    default = gd.stick_settings_from_config({})
    check(default == gd.StickSettings(True, 0.0, 1.0, 0.5), "with nothing saved the switch is on with the default values")
    check(gd.stick_switch_on({}) is True, "the switch is on by default")
    off = gd.stick_settings_from_config({key_on: False})
    check(off.enabled is False and off.deadzone == 0.0 and off.sensitivity == 1.0, "switch off gives neutral values")
    off_with_values = gd.stick_settings_from_config({key_on: False, gov.STICK_DZ_SPECS["deadzone"][0]: 50})
    check(off_with_values.deadzone == 0.0, "switch off ignores saved values")
    on = gd.stick_settings_from_config({key_on: True, gov.STICK_DZ_SPECS["deadzone"][0]: 30,
                                        gov.STICK_DZ_SPECS["sensitivity"][0]: 150,
                                        gov.STICK_DZ_SPECS["axis_threshold"][0]: 40})
    check(on == gd.StickSettings(True, 0.30, 1.5, 0.40), "switch on reads the saved values")
    bad = gd.stick_settings_from_config({key_on: True, gov.STICK_DZ_SPECS["deadzone"][0]: "junk",
                                         gov.STICK_DZ_SPECS["sensitivity"][0]: 9999,
                                         gov.STICK_DZ_SPECS["axis_threshold"][0]: -4})
    check(bad.deadzone == 0.0 and bad.sensitivity == 2.0 and bad.axis_threshold == 0.05, "bad values are defaulted or clamped")

    previous = gd.current_stick_settings()
    try:
        gd.apply_stick_config({key_on: False})
        check(gd.stick_axis_threshold_raw() == gov.STICK_DZ_OFF_DPAD_THRESHOLD, "off uses the old fixed threshold")
        check(gd.correct_stick_with_current(5000, 5000) == (5000, 5000), "off corrects nothing")
        gd.apply_stick_config({key_on: True, gov.STICK_DZ_SPECS["axis_threshold"][0]: 50})
        check(gd.stick_axis_threshold_raw() == int(0.5 * gd.STICK_MAX), "on uses the menu threshold")
    finally:
        gd.apply_stick_config({key_on: previous.enabled})

    cfg = {key_on: True}
    for name in gd.STICK_SETTING_NAMES:
        cfg[gov.STICK_DZ_SPECS[name][0]] = gov.STICK_DZ_SPECS[name][2]
    gd.reset_stick_defaults(cfg)
    check(all(cfg[gov.STICK_DZ_SPECS[n][0]] == gov.STICK_DZ_SPECS[n][1] for n in gd.STICK_SETTING_NAMES), "reset writes every default")
    check(cfg[key_on] is True, "reset leaves the switch")
    for name, (_key, default, low, high, step) in gov.STICK_DZ_SPECS.items():
        check(low <= default <= high and step > 0, "spec for %s is consistent" % name)


def stick_case_screen():
    import game_deck as gd
    key_on = gov.STICK_DZ_ENABLED_KEY
    check(mn.stick_dz_rows(False) == [mn.STICK_DZ_SWITCH, mn.STICK_DZ_PLAYERS, mn.CLOSE], "off shows only the switch, the player squares and Close")
    check(mn.stick_dz_rows(True) == [mn.STICK_DZ_SWITCH, mn.STICK_DZ_DEADZONE, mn.STICK_DZ_SENSITIVITY, mn.STICK_DZ_AXIS, mn.STICK_DZ_RESET, mn.STICK_DZ_PLAYERS, mn.CLOSE], "on shows the settings, Reset and the player squares")
    check(mn.stick_dz_cell(mn.stick_dz_rows(True), 99) == mn.CLOSE, "a number outside the rows is Close")
    for enabled in (False, True):
        rows = mn.stick_dz_rows(enabled)
        table = mn.stick_dz_table(enabled)
        down = [rows[0]]
        while True:
            nxt = table.cells[down[-1]].get(mn.DOWN)
            if nxt is None:
                break
            down.append(nxt)
        check(down == rows, "down walks every row in order (switch %s)" % enabled)
        check(table.cells[mn.CLOSE].get(mn.UP) == rows[-2], "up from Close reaches the row above it (switch %s)" % enabled)
    for enabled in (False, True):
        table = mn.stick_dz_table(enabled)
        check(table.cells[mn.STICK_DZ_PLAYERS].get(mn.RIGHT) == mn.CLOSE and table.cells[mn.CLOSE].get(mn.LEFT) == mn.STICK_DZ_PLAYERS,
              "Right from the squares reaches Close and Left from Close returns to them (switch %s)" % enabled)
    check([mn.stick_dz_player_step(1, mn.LEFT), mn.stick_dz_player_step(1, mn.RIGHT), mn.stick_dz_player_step(3, mn.RIGHT),
           mn.stick_dz_player_step(4, mn.RIGHT), mn.stick_dz_player_step(4, mn.LEFT)] == [1, 2, 4, None, 3],
          "Left and Right step the player square, and Right from Player 4 leaves toward Close")
    check("stick_deadzone" in mn.GAMES_SUB_MENU_LEFT, "the MENU page has the Stick Deadzone row")
    check(mn.GAMES_SUB_MENU_LEFT.index("stick_deadzone") == mn.GAMES_SUB_MENU_LEFT.index("controller_map") + 1, "the row sits under Controller Mapping")
    rows = ui.get_games_sub_menu_rows(None, {}, "MENU", True)
    check("stick_deadzone" in rows, "the MENU console lists the row")
    table = mn.games_sub_table(rows + [mn.CLOSE], False)
    check(table.cells["controller_map"].get(mn.DOWN) == "stick_deadzone", "down from Controller Mapping reaches Stick Deadzone")

    cfg = {key_on: True}
    check(gd.adjust_stick_setting(cfg, "deadzone", 1) == 1, "deadzone steps up by 1")
    check(gd.adjust_stick_setting(cfg, "deadzone", -1) == 0 and gd.adjust_stick_setting(cfg, "deadzone", -1) == 0, "deadzone stops at its low end")
    check(gd.adjust_stick_setting(cfg, "sensitivity", 1) == 105, "sensitivity steps up by 5")
    cfg[gov.STICK_DZ_SPECS["axis_threshold"][0]] = 95
    check(gd.adjust_stick_setting(cfg, "axis_threshold", 1) == 95, "threshold stops at its high end")
    check(gd.toggle_stick_switch(cfg) is False and gd.stick_switch_on(cfg) is False, "toggle turns the switch off")
    check(gd.toggle_stick_switch(cfg) is True, "toggle turns the switch on")

    check(gd.stick_direction_names(9000, 0, 8000) == ["RIGHT"] and gd.stick_direction_names(-9000, 9000, 8000) == ["UP", "LEFT"], "direction names follow the threshold")
    check(gd.stick_direction_names(8000, -8000, 8000) == [], "exactly at the threshold fires nothing")
    check(gd.stick_test_open() is False, "the screen is not open before it is drawn")
    gd.mark_stick_test_open()
    check(gd.stick_test_open() is True, "drawing the screen marks it open")
    check(gd.stick_test_sticks() is None or len(gd.stick_test_sticks()) == 2, "stick read gives nothing or two pairs")

    class _Db:
        config = {key_on: True, "menu_opacity": 50}
    for enabled in (False, True):
        _Db.config[key_on] = enabled
        surface = pygame.Surface((1036, 680), pygame.SRCALPHA)
        rows = mn.stick_dz_rows(enabled)
        for cursor in range(len(rows)):
            gd.render_stick_deadzone_ui(surface, (20, 138, 996, 525), (0, 255, 128), _Db, cursor, rows, ((20000, -5000), (-32767, 32767)))
        check(surface.get_at((20 + 996 - 60, 138 + 525 - 30))[:3] != (0, 0, 0), "the Close button is drawn (switch %s)" % enabled)
        for shown in gd.STICK_PLAYERS:
            _Db.config[key_on] = True
            surface = pygame.Surface((1036, 680), pygame.SRCALPHA)
            rows = mn.stick_dz_rows(True)
            gd.render_stick_deadzone_ui(surface, (20, 138, 996, 525), (0, 255, 128), _Db, rows.index(mn.STICK_DZ_PLAYERS), rows, None, shown)
            square_x = 20 + 16 + (shown - 1) * 42 + 17
            check(surface.get_at((square_x, 138 + 525 - 14 - 30))[:3] != (0, 0, 0), "the square of Player %d is drawn" % shown)


def stick_case_wiring():
    import game_deck as gd
    key_on = gov.STICK_DZ_ENABLED_KEY
    for old in ("DEFAULT_STICK_DEADZONE", "_get_stick_deadzone", "_STICK_DPAD_RELEASE_FRAC"):
        check(not hasattr(gd, old), "%s is gone" % old)

    def pad(lx, ly, rx, ry):
        g = gd._GPState()
        g.sLX, g.sLY, g.sRX, g.sRY = lx, ly, rx, ry
        return g

    previous = gd.current_stick_settings()
    try:
        gd.apply_stick_config({key_on: False})
        g = gd.apply_stick_correction(pad(-32768, 5000, 123, -4567))
        check((g.sLX, g.sLY, g.sRX, g.sRY) == (-32768, 5000, 123, -4567), "off leaves every stick value exactly as read")
        check((g.rawLX, g.rawLY, g.rawRX, g.rawRY) == (-32768, 5000, 123, -4567), "off still keeps the raw values")
        check(gd.stick_dinput_threshold_raw() == gov.STICK_DZ_OFF_DINPUT_THRESHOLD, "off keeps the old DirectInput threshold")
        check(gd.stick_axis_threshold_raw() == gov.STICK_DZ_OFF_DPAD_THRESHOLD, "off keeps the old D-pad threshold")

        gd.apply_stick_config({key_on: True, gov.STICK_DZ_SPECS["deadzone"][0]: 20, gov.STICK_DZ_SPECS["axis_threshold"][0]: 40})
        g = gd.apply_stick_correction(pad(3000, 3000, 32767, 0))
        check((g.sLX, g.sLY) == (0, 0), "on zeroes a stick inside the deadzone")
        check(g.sRX == 32767 and g.sRY == 0, "on keeps full push at full range")
        check((g.rawLX, g.rawLY, g.rawRX) == (3000, 3000, 32767), "on keeps the raw values beside the corrected ones")
        check(gd.stick_dinput_threshold_raw() == gd.stick_axis_threshold_raw() == int(0.4 * gd.STICK_MAX), "on, both thresholds follow the menu setting")
        check(gd.stick_test_sticks() is None or gd.stick_test_sticks()[0] == (gd.player1_gpstate().rawLX, gd.player1_gpstate().rawLY), "the test panel reads the raw values")
    finally:
        gd.apply_stick_config({key_on: previous.enabled})


def stick_case_main_script_calls():
    import game_deck as gd
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    module_defs = {n.name for n in ast.parse(open(gd.__file__, encoding="utf-8").read()).body if isinstance(n, ast.FunctionDef)}
    deck_names = set()
    for node in ast.walk(ast.parse(open(gd.__file__, encoding="utf-8").read())):
        if isinstance(node, ast.ClassDef) and node.name == "ExternalGameDeck":
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef):
                    deck_names.add(sub.name)
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self" and isinstance(sub.ctx, ast.Store):
                    deck_names.add(sub.attr)
    stick_names = {name for name in module_defs if "stick" in name and name not in deck_names}
    bad = sorted({node.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id == "game_deck" and node.attr in stick_names})
    check(not bad, "the main script calls module-level stick helpers on the game deck object: %s" % bad)
    check(len(stick_names) >= 8, "the stick helpers were found")
    with open(MAIN_SCRIPT, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index('app_state.get("menu_layer") == "GAMES_STICK_DZ" and game_deck is not None')
    block = text[start:text.index('app_state.get("menu_layer") == "GAMES_CTRL_MAP" and game_deck is not None', start)]
    check("menu_panel_rect(" in block and "_overlay_target_for_menu(screen)" in block,
          "the stick screen takes its place from the same surface and rectangle the menu uses")
    check("fit_design_canvas" not in block, "the stick screen does not size itself with a separate canvas")


class TestStickDeadzone(_SectionCase):
    """Stick deadzone settings, config handling and maths"""

    def test_stick_settings_and_maths(self):
        self.run_section(stick_case_maths_and_settings)

    def test_stick_screen(self):
        self.run_section(stick_case_screen)

    def test_stick_wiring(self):
        self.run_section(stick_case_wiring)

    def test_stick_main_script_calls(self):
        self.run_section(stick_case_main_script_calls)


# ============================================================================
# SECTION: TV guide highlight stays in view
# ============================================================================


def guide_highlight_case():
    cols = 4
    total = 336
    clock = {"t": datetime.datetime(2026, 10, 12, 14, 10, 0)}

    class _Clock:
        @staticmethod
        def now():
            return clock["t"]

    def clamp(raw, n):
        return max(0, min(raw, total - n))

    def make_schedule(rng):
        t, out = -86400, []
        while t < 8 * 86400:
            d = rng.choice([1200, 1800, 1800, 3600, 5400, 7200, 9000, 18000])
            out.append((t, t + d))
            t += d
            if rng.random() < 0.25:
                t += rng.choice([600, 1800, 3000, 7200])
        return out

    scheds = {}

    def stops_for(ch, time_idx):
        vs, ve = time_idx * 1800, time_idx * 1800 + cols * 1800
        stops, cursor = [], vs
        for a, b in scheds[ch]:
            if b <= vs:
                continue
            if a >= ve:
                break
            cs = max(vs, a)
            if cs > cursor:
                for c in range(int((cursor - vs) / 1800.0), int((cs - vs - 1) / 1800.0) + 1):
                    stops.append({"col": c, "abs": vs + c * 1800, "key": ("offair", c)})
            stops.append({"col": int((cs - vs) / 1800.0 + 1e-6), "abs": a, "key": ("show", round(a))})
            cursor = min(ve, b)
        for c in range(int((cursor - vs) / 1800.0) if cursor < ve else cols, cols):
            stops.append({"col": c, "abs": vs + c * 1800, "key": ("offair", c)})
        return stops

    state = {}

    def live_idx():
        return clock["t"].hour * 2 + (1 if clock["t"].minute >= 30 else 0)

    def view_idx():
        return clamp(live_idx() + state.get("selected_guide_time_idx", 0), cols)

    def refresh():
        state["guide_current_row_stops"] = stops_for(str(state["selected_guide_channel"]).zfill(2), view_idx())

    log = types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    ns = dict(app_state=state, log=log, broadcast_clock=_Clock, db=object(), str=str,
              get_guide_num_cols=lambda db: cols, clamp_guide_time_idx=clamp,
              _guide_dirty_event=threading.Event(), _refresh_guide_row_stops_offscreen=refresh,
              get_current_guide_time_idx=lambda a, d: view_idx(),
              get_cached_row_stops=lambda ch, expected_time_idx=None: None,
              get_guide_visible_rows=lambda d: 4)
    for name in ("_guide_jump_col", "_resync_guide_stop_after_row_switch", "_guide_keep_highlight_in_view"):
        exec(compile(get_function_source(name), "<extracted:%s>" % name, "exec"), ns)

    def visible_problem():
        vs, ve = view_idx() * 1800, view_idx() * 1800 + cols * 1800
        target = state.get("guide_selected_abs_time")
        if target is None:
            return None
        hit = [x for x in scheds[str(state["selected_guide_channel"]).zfill(2)] if x[0] <= target < x[1]]
        if not hit:
            return None if vs <= target < ve else "gap target outside the window"
        a, b = hit[0]
        return "highlighted show outside the window" if (b <= vs or a >= ve) else None

    broken = []
    for seed in range(120):
        rng = random.Random(seed)
        for c in range(5, 9):
            scheds[str(c).zfill(2)] = make_schedule(rng)
        clock["t"] = datetime.datetime(2026, 10, 12, 14, 10, 0)
        state.clear()
        state.update({"guide_col_pos": 0, "selected_guide_channel": 5, "selected_guide_time_idx": 0})
        refresh()
        ns["_resync_guide_stop_after_row_switch"]()
        ns["_guide_keep_highlight_in_view"]()
        history = ""
        for _ in range(60):
            act = rng.choice("LRRUDT")
            history += act
            if act == "T":
                clock["t"] += datetime.timedelta(minutes=rng.choice([5, 20, 30, 45]))
            elif act in "LR":
                ns["_guide_jump_col"](1 if act == "R" else -1)
            else:
                state["selected_guide_channel"] = 5 + ((state["selected_guide_channel"] - 5 + (1 if act == "D" else -1)) % 4)
                ns["_resync_guide_stop_after_row_switch"]()
            ns["_guide_keep_highlight_in_view"]()
            problem = visible_problem()
            if problem:
                broken.append("seed %d after %s: %s" % (seed, history[-10:], problem))
                break
    check(not broken, "guide highlight left the window: %s" % broken[:3])

    main_src = MAIN_SRC
    frame_block = main_src.index('    if app_state["current_channel"] == "04":\n        _guide_keep_highlight_in_view()\n        if (now_ticks >= app_state.get("guide_inactivity_timer", 0)')
    check(frame_block > 0, "the per-frame guide block calls the keeper before the inactivity check")
    check('app_state["_guide_live_idx_seen"] = None\n        app_state["guide_inactivity_timer"]' in main_src, "entering the guide clears the keeper's last seen column")


class TestGuideHighlightInView(_SectionCase):
    """The TV guide highlight never ends up outside the visible columns, including when the live clock slides the window"""

    def test_guide_highlight_in_view(self):
        self.run_section(guide_highlight_case)


# ============================================================================
# SECTION: Scan of every block of a block-mode channel
# ============================================================================


def _block_scan_ns(block_mode, now_hour, schedules, scanned=()):
    """Real scan-snapshot, progress and deep-queue functions cut out of retro_tv_emulator.py, run against a fake one-channel database."""
    fake_db = types.SimpleNamespace(channels_db={"05": {
        "active": True, "block_mode": block_mode, "scheduling_mode": "random_slots",
        "schedules": schedules, "commercials_enabled": False}}, config={})
    fixed_now = datetime.datetime(2026, 10, 13, now_hour, 30)

    def lineup(ch, info, block, now, peek_only=False):
        files = [item["path"] for item in info["schedules"].get(block, [])]
        return [{"path": f, "duration": 1800} for f in files], block

    ns = {"db": fake_db, "time": time, "os": os, "log": MagicMock(), "copy": __import__("copy"),
          "SIX_HOUR_BLOCK_NAMES": list(media.SIX_HOUR_BLOCK_NAMES),
          "SIX_HOUR_BLOCKS": list(media.SIX_HOUR_BLOCKS),
          "build_pending_tracks": media.build_pending_tracks,
          "SCAN_GATHER_LIBRARY_LOOKUPS": 400,
          "broadcast_clock": types.SimpleNamespace(now=lambda: fixed_now),
          "_get_channel_ordered_content": lambda ch, info: lineup(ch, info, _airing_block(block_mode, now_hour), fixed_now),
          "_get_probe_now_state": lambda ch: {"file": ""},
          "_get_block_ordered_content": lineup,
          "_paths_from_entry": lambda e: [e["path"]] if isinstance(e, dict) else [str(e)],
          "_cached_path_exists": lambda p: True,
          "_scan_stages_pending": lambda p, g, d, lk: os.path.normpath(p) not in {os.path.normpath(x) for x in scanned},
          "_scan_file_is_done": lambda p, g, d, mo, lk: os.path.normpath(p) in {os.path.normpath(x) for x in scanned},
          "_scan_snapshot_channel": lambda ch: ns["_build_scan_snapshot"]()["channels"].get(ch),
          "_front_load_watched_channel": lambda items: items}
    for name in ("_airing_block_name", "_block_names_after_airing", "_get_block_ordered_content_peek", "_build_scan_snapshot",
                 "_compute_channel_scan_progress", "_gather_deep_backlog_queue", "_gather_probe_priority_queue"):
        exec(compile(get_function_source(name), "<extracted:%s>" % name, "exec"), ns)
    return ns, fixed_now


def _airing_block(block_mode, hour):
    if block_mode == "six_block":
        return media.SIX_HOUR_BLOCKS[hour // 4][0]
    if block_mode == "full_day":
        return "Full Day"
    return "Morning" if 4 <= hour < 12 else ("Evening" if 12 <= hour < 20 else "Night")


def suite_block_scan():
    def sched(names):
        return {n: [{"path": "/m/%s_%d.mkv" % (n.replace(" ", ""), i)} for i in range(2)] for n in names}

    eight = ["Morning", "Evening", "Night"]
    six = list(media.SIX_HOUR_BLOCK_NAMES)

    ns, now = _block_scan_ns("eight_hour", 9, sched(eight))
    check(ns["_block_names_after_airing"](ns["db"].channels_db["05"], now) == ["Evening", "Night"], "8 hour at 09:30 (Morning): Evening then Night follow")
    ns2, now2 = _block_scan_ns("eight_hour", 21, sched(eight))
    check(ns2["_block_names_after_airing"](ns2["db"].channels_db["05"], now2) == ["Morning", "Evening"], "8 hour at 21:30 (Night): Morning then Evening follow")
    ns3, now3 = _block_scan_ns("six_block", 17, sched(six))
    check(ns3["_block_names_after_airing"](ns3["db"].channels_db["05"], now3) == ["8-12am", "12-4am", "4-8am", "8-12pm", "12-4pm"], "4 hour at 17:30 (4-8pm): the other five blocks follow in airing order")
    for mode in ("full_day",):
        nsf, nowf = _block_scan_ns(mode, 9, sched(["Full Day"]))
        check(nsf["_block_names_after_airing"](nsf["db"].channels_db["05"], nowf) == [], "Full Day has no other blocks")
    nsm, nowm = _block_scan_ns("eight_hour", 9, sched(eight))
    nsm["db"].channels_db["05"]["scheduling_mode"] = "marathon"
    check(nsm["_block_names_after_airing"](nsm["db"].channels_db["05"], nowm) == [], "Marathon has no other blocks")
    nsv, nowv = _block_scan_ns("eight_hour", 9, sched(eight))
    nsv["db"].channels_db["05"]["is_visualizer"] = True
    check(nsv["_block_names_after_airing"](nsv["db"].channels_db["05"], nowv) == [], "a visualizer has no other blocks")

    for label, mode, hour, names, total in (("8 hour", "eight_hour", 9, eight, 6), ("4 hour", "six_block", 17, six, 12)):
        n, _ = _block_scan_ns(mode, hour, sched(names))
        snap = n["_build_scan_snapshot"]()
        done, tot = n["_compute_channel_scan_progress"](snap, [400])["05"]
        check(tot == total, "%s: the bar counts every block's files (%d, got %d)" % (label, total, tot))
        queue = n["_gather_deep_backlog_queue"]()
        queued = [it[3] for it in queue]
        check(len(queued) == total - 2 and len(set(queued)) == len(queued), "%s: the deep queue holds every off-air block's file once, the airing block's two files stay in the priority tiers (%d, got %d)" % (label, total - 2, len(queued)))

    n, _ = _block_scan_ns("six_block", 17, sched(six))
    blocks_in_queue = [it[2] for it in n["_gather_deep_backlog_queue"]()]
    first_seen = list(dict.fromkeys(blocks_in_queue))
    check(first_seen[0] == "8-12am" and first_seen[-1] == "12-4pm", "4 hour: the other blocks are queued by how soon they air, next block first (got %s)" % first_seen)

    n, _ = _block_scan_ns("eight_hour", 9, sched(eight), scanned=["/m/Evening_0.mkv", "/m/Night_1.mkv"])
    queued = [it[3] for it in n["_gather_deep_backlog_queue"]()]
    check("/m/Evening_0.mkv" not in queued and "/m/Night_1.mkv" not in queued, "files already scanned are not queued again")
    done, tot = n["_compute_channel_scan_progress"](n["_build_scan_snapshot"](), [400])["05"]
    check((done, tot) == (2, 6), "scanned files count as done in the bar (got %s)" % ((done, tot),))

    shared = sched(eight)
    shared["Night"][0] = dict(shared["Morning"][0])
    n, _ = _block_scan_ns("eight_hour", 9, shared)
    done, tot = n["_compute_channel_scan_progress"](n["_build_scan_snapshot"](), [400])["05"]
    queued = [it[3] for it in n["_gather_deep_backlog_queue"]()]
    check(tot == 5, "a file reused in two blocks counts once in the bar (got %d)" % tot)
    ns_order = {"SCAN_CLASS_NOW": 0, "SCAN_CLASS_COMMERCIAL": 1, "SCAN_CLASS_NEXT": 2, "SCAN_CLASS_DEEP": 3}
    exec(compile(get_function_source("_order_scan_pipeline"), "<extracted:_order_scan_pipeline>", "exec"), ns_order)
    tiers = [[("channel", "05", "Morning", os.path.normpath("/m/Morning_0.mkv"))], [], [], []]
    items = ns_order["_order_scan_pipeline"](tiers, [("channel", "05", it[2], it[3]) for it in n["_gather_deep_backlog_queue"]()])
    paths = [it[5] for it in items]
    check(len(paths) == len(set(paths)), "the pipeline scans a file reused across blocks once")

    def stuck_ns(block_mode, hour, names):
        n, now_ = _block_scan_ns(block_mode, hour, sched(names))
        n["_get_channel_ordered_content"] = lambda ch, info: ([], None)
        n["_get_block_ordered_content"] = lambda ch, info, block, now_, peek_only=False: ([], None)
        n["_next_n_from_ordered"] = lambda ordered, cur, n=4: []
        n["_next_n_from_block_timeline"] = lambda ch, cur, n=4: None
        n["_log_missing_probe_path_once"] = lambda *a: None
        return n, now_

    n, now_ = stuck_ns("eight_hour", 9, eight)
    snap_rec = n["_build_scan_snapshot"]()["channels"]["05"]
    check(snap_rec["pending_block"] == "Morning" and len(snap_rec["pending"]) == 2,
          "a channel with no lineup still lists its airing block's unscanned files in schedule order (got %s)" % snap_rec["pending"])
    check([o["block_key"] for o in snap_rec["pending_other"]] == ["Evening", "Night"],
          "its other blocks' unscanned files are listed by how soon they air")
    tiers = n["_gather_probe_priority_queue"]()
    check([it[3] for it in tiers[0]] == [os.path.normpath("/m/Morning_0.mkv")] and tiers[0][0][2] == "Morning",
          "a channel with no lineup has its first unscanned file in the airing tier so it can leave Static (got %s)" % tiers[0])
    deep_blocks = list(dict.fromkeys(it[2] for it in n["_gather_deep_backlog_queue"]()))
    check(deep_blocks == ["Morning", "Evening", "Night"],
          "the deep queue follows with the rest of the airing block, then the next blocks by distance (got %s)" % deep_blocks)

    n, now_ = stuck_ns("six_block", 17, six)
    deep_blocks = list(dict.fromkeys(it[2] for it in n["_gather_deep_backlog_queue"]()))
    check(deep_blocks[0] == _airing_block("six_block", 17),
          "4 hour: the airing block is queued before the others (got %s)" % deep_blocks)

    n, now_ = stuck_ns("eight_hour", 9, eight)
    n["_scan_stages_pending"] = lambda p, g, d, lk: False
    check(not n["_gather_probe_priority_queue"]()[0] and not n["_gather_deep_backlog_queue"](),
          "files already scanned are not queued for a channel with no lineup")

    n, now_ = stuck_ns("eight_hour", 9, eight)
    n["db"].channels_db["05"]["broken_files"] = ["/m/Morning_0.mkv"]
    check("/m/Morning_0.mkv" not in n["_build_scan_snapshot"]()["channels"]["05"]["pending"], "a file confirmed broken is not queued")

    def prime_ns(stuck, broken=()):
        n, now_ = stuck_ns("eight_hour", 9, eight) if stuck else _block_scan_ns("eight_hour", 9, sched(eight))
        info = n["db"].channels_db["05"]
        info["broken_files"] = list(broken)
        probed = []
        n.update({"_priming_inflight": set(), "_priming_inflight_lock": __import__("threading").Lock(),
                  "_PRIME_DURATIONS_NEAR_TERM_N": 8, "threading": types.SimpleNamespace(Thread=lambda **kw: types.SimpleNamespace(start=lambda: probed.append(kw["args"][1]))),
                  "_prime_channel_block_durations_worker": lambda *a: None, "_get_cached_media_probe": lambda p: {}, "_get_probe_now_state": lambda ch: {"file": ""},
                  "_next_n_from_ordered": lambda ordered, cur, n=4: ordered[:n]})
        exec(compile(get_function_source("_prime_channel_block_durations"), "<extracted:_prime_channel_block_durations>", "exec"), n)
        return n, probed

    n, probed = prime_ns(True)
    n["_prime_channel_block_durations"]("05")
    check([p for p, _np in probed[0]] == ["/m/Morning_0.mkv", "/m/Morning_1.mkv"],
          "tuning to a channel with no lineup probes the unscanned files of its airing block in schedule order (got %s)" % probed)
    n, probed = prime_ns(True, broken=["/m/Morning_0.mkv"])
    n["_prime_channel_block_durations"]("05")
    check([p for p, _np in probed[0]] == ["/m/Morning_1.mkv"], "a confirmed broken file is not probed")
    n, probed = prime_ns(False)
    n["_prime_channel_block_durations"]("05")
    check(len(probed) == 1 and len(probed[0]) == 2, "a channel with a lineup still probes its near-term files as before")
    check(items[0][0] == 0, "the airing file still comes first, ahead of every off-air block file")


class TestBlockScan(_SectionCase):
    """8 hour and 4 hour channels count and scan every block, in airing order, skipping scanned files"""

    def test_block_scan(self):
        self.run_section(suite_block_scan)



def suite_block_peek_isolated():
    plog = {"show_pos": {"__FAIRNESS__": {"run": 3, "used": {"Show A": 2}}, "Show A": 1}}
    eps = lambda name, n: [{"path": "/m/%s S01E%02d.mkv" % (name, i + 1), "duration": 1500} for i in range(n)]
    info = {"scheduling_mode": "random_slots", "block_mode": "eight_hour", "playback_log": plog,
            "schedules": {"Evening": eps("Show A", 6) + eps("Show B", 6)}, "pair_threshold_minutes": 0}
    fake_db = types.SimpleNamespace(config={"media_probe_cache": {}}, channels_db={"05": info}, save_settings_async=lambda **k: None)
    ns = {"db": fake_db, "time": time, "log": MagicMock(),
          "_ORDERED_CONTENT_CACHE": {}, "_ORDERED_CONTENT_CACHE_TTL_MS": 8000,
          "_prune_ordered_content_cache": lambda ms: None, "_cached_path_exists": lambda p: True,
          "_start_order_seed": lambda i, o: "1:%d" % o, "read_block_lineup": media.read_block_lineup}
    exec(compile(get_function_source("_get_block_ordered_content"), "<extracted:_get_block_ordered_content>", "exec"), ns)
    before = __import__("copy").deepcopy(plog)
    lineup, name = ns["_get_block_ordered_content"]("05", info, "Evening", datetime.datetime(2026, 10, 13, 9, 30), peek_only=True)
    check(name == "Evening" and len(lineup) > 0, "an off-air block's lineup is built from the real rotation")
    check(plog == before, "reading an off-air block leaves the saved show positions and fairness counters unchanged")


class TestBlockPeekIsolated(_SectionCase):
    """Reading a block that is not on air never moves the real scheduler's saved state"""

    def test_block_peek_isolated(self):
        self.run_section(suite_block_peek_isolated)



# ============================================================================
# SECTION: Broken episode swap, sequential order
# ============================================================================

def suite_broken_swap_sequential():
    import copy as _cp
    PATH_MISSING, PATH_UNKNOWN = "missing", "unknown"
    ns = {"db": types.SimpleNamespace(config={"media_probe_cache": {}}, save_settings=lambda **k: None),
          "log": MagicMock(), "media": media, "random": __import__("random"),
          "PATH_MISSING": PATH_MISSING, "PATH_UNKNOWN": PATH_UNKNOWN,
          "_path_status": lambda p: "ok", "_log_info_throttled": lambda *a, **k: None,
          "_mark_file_broken": lambda *a, **k: None,
          "resolve_track_duration": media.resolve_track_duration,
          "split_movies_and_series": media.split_movies_and_series,
          "pair_short_episodes": media.pair_short_episodes, "build_raw_tracks": media.build_raw_tracks, "build_pending_tracks": media.build_pending_tracks,
          "_cached_path_exists": lambda p: True,
          "note_random_episode_played": media.note_random_episode_played}
    for fn in ("_get_show_episode_context", "_show_rotation_entries", "_show_raw_episodes", "_keep_swap_groups", "_resolve_broken_episode"):
        exec(compile(get_function_source(fn), "<extracted:%s>" % fn, "exec"), ns)

    def eps(name, n, dur):
        return [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": dur} for i in range(n)]

    def expand(lineup):
        out = []
        for t in lineup:
            out += [t.get("pair_ep1_path", t["path"]), t.get("pair_ep2_path", "")] if t.get("is_pair") else [t["path"]]
        return [p for p in out if p]

    def alpha(paths):
        return [p for p in paths if "/Alpha/" in p]

    for pair_minutes, dur, count, broken_first in ((0, 1500, 12, False), (15, 600, 24, False), (0, 1500, 12, True)):
        raw = eps("Alpha", count, dur) + eps("Beta", count, dur)
        info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": pair_minutes, "episode_order_mode": "sequential",
                "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
        lineup, show_pos, _ = media.build_block_rotation(raw, info, {}, 2026100, 28800, "05", None, "1:200")
        info["playback_log"]["show_pos"] = show_pos
        info["playback_log"]["sched_cached_tracks"] = list(dict.fromkeys(expand(lineup)))
        label = "pairing %d min" % pair_minutes + (", another file of the show already broken" if broken_first else "")
        if broken_first:
            first = alpha(info["playback_log"]["sched_cached_tracks"])[0]
            info["broken_files"] = [first]
            info["playback_log"]["sched_cached_tracks"] = [p for p in info["playback_log"]["sched_cached_tracks"] if p != first]
        planned = alpha(info["playback_log"]["sched_cached_tracks"])
        failed = planned[1] if not broken_first else planned[0]
        sub = ns["_resolve_broken_episode"](info, failed, "Evening", channel_id="05")
        check(sub is not None and sub["file"] != failed, label + ": a substitute is chosen")
        swapped = alpha(info["playback_log"]["sched_cached_tracks"])
        check(failed not in swapped and sub["file"] in swapped, label + ": the saved lineup holds the substitute, not the failed file")
        pos = info["playback_log"]["show_pos"]
        check("Alpha" not in pos and isinstance(pos.get("05:alpha"), dict) and "next" in pos.get("05:alpha"), label + ": the position is kept under the channel-scoped key only, as the file that airs next")
        nxt, _, _ = media.build_block_rotation(raw, info, _cp.deepcopy(pos), 2026101, 28800, "05", None, "1:200")
        again = set(alpha(expand(nxt))) & set(swapped)
        check(not again, label + ": the next block does not start on an episode that already aired (%s)" % sorted(again))

    raw = eps("Paired", 4, 600)
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 15, "episode_order_mode": "sequential",
            "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
    lineup, show_pos, _ = media.build_block_rotation(raw, info, {}, 2026100, 28800, "05", None, "1:200")
    info["playback_log"]["show_pos"] = show_pos
    info["playback_log"]["sched_cached_tracks"] = expand(lineup)
    failed = raw[0]["path"]
    replacement = ns["_resolve_broken_episode"](info, failed, "Evening", channel_id="05")
    paired_paths = [raw[2]["path"], raw[3]["path"]]
    check(replacement is not None and replacement["file"] == paired_paths[0], "paired sequential swap: replaces the failed pair with the next rotation entry")
    check(info["playback_log"]["sched_cached_tracks"] == paired_paths, "paired sequential swap: saved lineup contains the replacement pair once")
    check(info["playback_log"]["show_pos"]["05:paired"]["next"] is None, "paired sequential swap: saved position advances by rotation entry")


class TestBrokenSwapSequential(_SectionCase):
    """A broken episode in sequential order moves the show's real position, so the substitute never airs twice"""

    def test_broken_swap_sequential(self):
        self.run_section(suite_broken_swap_sequential)


# ============================================================================
# SECTION: Broken episode swap, sequential order, show fills its block
# ============================================================================

def suite_broken_swap_sequential_full():
    import copy as _cp
    ns = {"db": types.SimpleNamespace(config={"media_probe_cache": {}}, save_settings=lambda **k: None),
          "log": MagicMock(), "media": media, "random": __import__("random"),
          "PATH_MISSING": "missing", "PATH_UNKNOWN": "unknown",
          "_path_status": lambda p: "ok", "_log_info_throttled": lambda *a, **k: None,
          "_mark_file_broken": lambda *a, **k: None,
          "resolve_track_duration": media.resolve_track_duration,
          "split_movies_and_series": media.split_movies_and_series,
          "pair_short_episodes": media.pair_short_episodes, "build_raw_tracks": media.build_raw_tracks, "build_pending_tracks": media.build_pending_tracks,
          "_cached_path_exists": lambda p: True,
          "note_random_episode_played": media.note_random_episode_played}
    for fn in ("_get_show_episode_context", "_show_rotation_entries", "_show_raw_episodes", "_keep_swap_groups", "_resolve_broken_episode"):
        exec(compile(get_function_source(fn), "<extracted:%s>" % fn, "exec"), ns)

    def expand(lineup):
        out = []
        for t in lineup:
            out += [t.get("pair_ep1_path", t["path"]), t.get("pair_ep2_path", "")] if t.get("is_pair") else [t["path"]]
        return [p for p in out if p]

    refused = swapped_count = 0
    for mode in ("random_slots", "two_slots"):
        for count in (2, 3, 4, 5, 6):
            raw = [{"path": "/m/tv/Solo/Solo S01E%02d.mkv" % (i + 1), "duration": 1500} for i in range(count)]
            base = {"scheduling_mode": mode, "pair_threshold_minutes": 0, "episode_order_mode": "sequential",
                    "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
            lineup, show_pos, _ = media.build_block_rotation(raw, base, {}, 2026100, 28800, "05", None, "1:200")
            base["playback_log"]["show_pos"] = show_pos
            base["playback_log"]["sched_cached_tracks"] = list(dict.fromkeys(expand(lineup)))
            planned = list(base["playback_log"]["sched_cached_tracks"])
            for failed in planned:
                label = "%s, %d episodes, %s fails" % (mode, count, failed[-7:-4])
                info = _cp.deepcopy(base)
                before_lineup = list(info["playback_log"]["sched_cached_tracks"])
                before_pos = _cp.deepcopy(info["playback_log"]["show_pos"])
                sub = ns["_resolve_broken_episode"](info, failed, "Evening", channel_id="05")
                after = info["playback_log"]["sched_cached_tracks"]
                check(len(after) == len(set(after)), label + ": no episode is in the saved lineup twice (%s)" % [p[-7:-4] for p in after])
                if sub is None:
                    refused += 1
                    check(after == before_lineup and info["playback_log"]["show_pos"] == before_pos,
                          label + ": a refused swap leaves the saved lineup and positions unchanged")
                else:
                    swapped_count += 1
                    check(sub["file"] in after and sub["file"] != failed, label + ": the saved lineup holds the substitute")
    check(refused > 0 and swapped_count > 0, "both refused and made swaps were exercised (%d refused, %d made)" % (refused, swapped_count))

    raw = eps("Paired", 4, 600)
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 15, "episode_order_mode": "sequential",
            "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
    lineup, show_pos, _ = media.build_block_rotation(raw, info, {}, 2026100, 28800, "05", None, "1:200")
    info["playback_log"]["show_pos"] = show_pos
    info["playback_log"]["sched_cached_tracks"] = list(dict.fromkeys(expand(lineup)))
    failed = info["playback_log"]["sched_cached_tracks"][0]
    sub = ns["_resolve_broken_episode"](info, failed, "Evening", channel_id="05")
    check(sub is not None, "paired sequential swap chooses a substitute")
    state = info["playback_log"]["show_pos"]["05:paired"]
    entries = media.pair_short_episodes(raw, 900)
    expected = media.sequential_state_from_entry_index(raw, entries, 1)
    check(state == expected, "paired sequential swap advances the saved position by rotation entry")


# ============================================================================
# SECTION: Saved lineup keeps every file of a leftover bundle
# ============================================================================

def suite_lineup_paths_bundle():
    def eps(name, durs):
        return [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": d} for i, d in enumerate(durs)]

    def aired(lineup):
        out = []
        for t in lineup:
            if t.get("is_pair"):
                out += [t["pair_ep1_path"], t["pair_ep2_path"]]
            elif t.get("is_bundle"):
                out += [e["path"] for e in t["bundle_items"]]
            else:
                out.append(t["path"])
        return out

    plain = [{"path": "/m/a.mkv", "duration": 1500}, {"path": "/m/b.mkv", "duration": 1500}]
    check(media.lineup_paths(plain) == ["/m/a.mkv", "/m/b.mkv"], "plain entries give their own path, in order")
    pair = media.pair_short_episodes(eps("P", [600, 600]), threshold_seconds=900)
    check(media.lineup_paths(pair) == [pair[0]["pair_ep1_path"], pair[0]["pair_ep2_path"]], "a pair gives both of its files")
    bundle = media.pair_short_episodes(eps("B", [600, 1500, 600, 1500, 600]), threshold_seconds=900)
    got = media.lineup_paths(bundle)
    check(all(p in got for p in aired(bundle)) and len(got) == len(aired(bundle)),
          "a leftover bundle gives every one of its files, not only the first (%s)" % [p[-7:-4] for p in got])
    check(media.lineup_paths([]) == [] and media.lineup_paths(None) == [], "no tracks give no paths")
    check(media.lineup_paths([{"path": ""}]) == [], "an empty path is left out")

    raw = eps("Alt", [600, 1500] * 6)
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 15, "episode_order_mode": "sequential",
            "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
    seen_bundle = False
    for b in range(12):
        lineup, sp, _ = media.build_block_rotation(raw, info, info["playback_log"].get("show_pos", {}), 2026100 + b, 28800, "05", None, "1:200")
        info["playback_log"]["show_pos"] = sp
        saved = list(dict.fromkeys(media.lineup_paths(lineup)))
        seen_bundle = seen_bundle or any(t.get("is_bundle") for t in lineup)
        missing = [p[-7:-4] for p in aired(lineup) if p not in saved]
        check(not missing, "block %d: every file the first pass airs is in the saved list (missing %s)" % (b, missing))
    check(seen_bundle, "the test blocks included a leftover bundle")

    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(src.count("lineup_paths(") >= 5, "the scheduler saves its lineups through media.lineup_paths")
    check(src.count('_ct.get("pair_ep2_path", "")') == 0 and src.count('_ct2.get("pair_ep2_path", "")') == 0,
          "the scheduler holds no hand-written flatten of a lineup into saved paths")


class TestLineupPathsBundle(_SectionCase):
    """A block's saved path list holds every file of a leftover bundle"""

    def test_lineup_paths_bundle(self):
        self.run_section(suite_lineup_paths_bundle)


def _lineup_signature(lineup):
    return [("pair" if x.get("is_pair") else "bundle" if x.get("is_bundle") else "single", tuple(media.lineup_paths([x]))) for x in lineup]


def _mixed_short_content():
    def eps(name, durs):
        return [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": d} for i, d in enumerate(durs)]
    return eps("X", [600, 1500, 600, 600, 600, 1500, 600]) + eps("Y", [600, 600, 600, 1500, 600, 1500, 600, 600, 600])


def _mixed_short_channel(content):
    _, _, ads = _sched_record_library()
    info = {"active": True, "scheduling_mode": "random_slots", "commercials_enabled": True,
            "commercial_placement": "interrupt_half_hour", "pair_threshold_minutes": 15, "block_mode": "eight_hour",
            "schedules": {n: [dict(x) for x in content] for n in ("Morning", "Evening", "Night")},
            "playback_log": {"intro_seed": 123456789}}
    info["schedules"]["Commercials"] = ads
    return info


def suite_lineup_groups():
    import copy as _copy

    def eps(name, durs):
        return [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": d} for i, d in enumerate(durs)]

    content = _mixed_short_content()
    raw = [dict(x) for x in content]
    info = _mixed_short_channel(content)
    differ = 0
    for b in range(12):
        lineup, sp, _ = media.build_block_rotation(raw, info, info["playback_log"].get("show_pos", {}), 2026100 + b, 28800, "05", None, "1:200")
        info["playback_log"]["show_pos"] = sp
        paths = list(dict.fromkeys(media.lineup_paths(lineup)))
        record = media.lineup_groups(lineup, 900)
        folder = info["schedules"]["Evening"]
        re_paired = media.saved_block_lineup(paths, {}, raw, folder, [], 900)
        with_groups = media.saved_block_lineup(paths, {}, raw, folder, [], 900, cached_groups=record)
        differ += _lineup_signature(re_paired) != _lineup_signature(lineup)
        check(_lineup_signature(with_groups) == _lineup_signature(lineup),
              "block %d: a lineup read back with its saved groups is exactly the lineup the rotation built" % b)
    check(differ > 0, "the test blocks include lineups that pairing the saved path list again would change (%d of 12)" % differ)

    lineup = media.pair_short_episodes(eps("Q", [600, 600, 1500, 600, 600, 600]), threshold_seconds=900)
    paths = list(dict.fromkeys(media.lineup_paths(lineup)))
    raw_q = eps("Q", [600, 600, 1500, 600, 600, 600])
    folder_q = [dict(x) for x in raw_q]
    record = media.lineup_groups(lineup, 900)
    check(record["version"] == media.LINEUP_GROUPS_VERSION and record["threshold"] == 900
          and record["pairing_rule_version"] == media.SHORT_EPISODE_PAIRING_VERSION, "the record holds its version, the pairing rule version and the threshold")
    check([g["kind"] for g in record["groups"]] == ["pair", "single", "pair", "bundle"][:len(record["groups"])] or len(record["groups"]) == len(lineup),
          "the record has one group per lineup entry")
    check(sorted(sum((g["paths"] for g in record["groups"]), [])) == sorted(paths), "the groups hold exactly the saved paths")

    def read(rec, saved=paths, thresh=900, broken=()):
        live = [x for x in raw_q if x["path"] not in broken]
        folder_live = [x for x in folder_q if x["path"] not in broken]
        return _lineup_signature(media.saved_block_lineup(saved, {}, live, folder_live, list(broken), thresh, cached_groups=rec))

    base = _lineup_signature(lineup)
    check(read(record) == base, "matching groups give the saved lineup")
    check(read(record, thresh=1800) != base or read(record, thresh=1800) == _lineup_signature(media.pair_short_episodes(raw_q, threshold_seconds=1800)),
          "a changed threshold ignores the groups and pairs the list again")

    paired = eps("Swap", [600, 600, 1500, 600, 600])
    first_entries, first_state = media.take_sequential_entries(paired, {"next": None, "owed": []}, 1, 900)
    second_entries, second_state = media.take_sequential_entries(paired, first_state, 1, 900)
    shifted = media.sequential_state_shifted(paired, {"next": None, "owed": []}, 2, 900)
    check(len(first_entries) == 1 and first_entries[0].get("is_pair"), "sequential rotation starts with the paired entry")
    check(len(second_entries) == 1 and second_entries[0].get("path") == paired[2]["path"], "the next sequential entry follows the pair")
    check(shifted == second_state, "sequential swap advances by rotation entries, not raw episode indexes")
    check(read(record, saved=paths[:-1]) == _lineup_signature(media.saved_block_lineup(paths[:-1], {}, raw_q, folder_q, [], 900)),
          "groups that no longer describe the saved path list are ignored")
    stale = _copy.deepcopy(record)
    stale["pairing_rule_version"] = media.SHORT_EPISODE_PAIRING_VERSION + 1
    check(read(stale) == _lineup_signature(media.saved_block_lineup(paths, {}, raw_q, folder_q, [], 900)), "groups from another pairing rule version are ignored")
    for bad in (None, {}, {"version": media.LINEUP_GROUPS_VERSION}, "x", {"version": media.LINEUP_GROUPS_VERSION, "pairing_rule_version": media.SHORT_EPISODE_PAIRING_VERSION, "threshold": 900, "groups": [{"kind": "nope", "paths": paths}]}):
        check(read(bad) == _lineup_signature(media.saved_block_lineup(paths, {}, raw_q, folder_q, [], 900)), "a damaged record is ignored: %s" % (str(bad)[:30],))

    pair_group = next(g for g in record["groups"] if g["kind"] == "pair")
    shrunk = read(record, broken=[pair_group["paths"][0]])
    check(("single", (pair_group["paths"][1],)) in shrunk, "a pair that loses a file airs the other file alone, in its place")
    check(len(shrunk) == len(base), "a lost file shrinks only its own group; no other entry moves")
    raw_r = eps("R", [600, 1500, 600, 1500, 600])
    lineup_r = media.pair_short_episodes(raw_r, threshold_seconds=900)
    paths_r = list(dict.fromkeys(media.lineup_paths(lineup_r)))
    record_r = media.lineup_groups(lineup_r, 900)
    bundle_group = next(g for g in record_r["groups"] if g["kind"] == "bundle")
    check(len(bundle_group["paths"]) == 3, "the test show leaves a bundle of three files")
    live_r = [x for x in raw_r if x["path"] != bundle_group["paths"][0]]
    left = _lineup_signature(media.saved_block_lineup(paths_r, {}, live_r, live_r, [bundle_group["paths"][0]], 900, cached_groups=record_r))
    check(any(k == "bundle" and tuple(p) == tuple(bundle_group["paths"][1:]) for k, p in left), "a bundle that loses a file keeps the rest as one bundle")

    channels = {"05": _mixed_short_channel(content)}
    times = [datetime.datetime(2026, 10, 13, 12, 0, 5) + datetime.timedelta(minutes=37 * i) for i in range(13)]
    clock, ticks = [times[0]], [0]
    ns = _sched_record_ns(channels, clock, ticks)
    built, used = [], []
    real_rotation, real_timeline = ns["build_block_rotation"], ns["build_channel_timeline"]

    def spy_rotation(*args, **kwargs):
        out = real_rotation(*args, **kwargs)
        built.append(_lineup_signature(out[0]))
        return out

    def spy_timeline(video_tracks, *args, **kwargs):
        used.append(_lineup_signature(video_tracks))
        return real_timeline(video_tracks, *args, **kwargs)

    ns["build_block_rotation"], ns["build_channel_timeline"] = spy_rotation, spy_timeline
    for moment in times:
        clock[0] = moment
        ticks[0] += 10000
        with patch.object(media, "_cached_path_exists", lambda p: True):
            ns["calculate_slotted_playback_state"]("05")
    check(len(used) >= len(times) and len(built) >= 1, "the scheduler ran on the mixed short-episode channel")
    stray = [i for i, s in enumerate(used) if s not in built]
    check(not stray, "every pass of the scheduler airs a lineup the rotation built, never a re-paired one (passes %s)" % stray[:5])
    plog = channels["05"]["playback_log"]
    saved = plog.get("sched_cached_groups")
    check(isinstance(saved, dict) and saved.get("groups"), "the scheduler saves the groups with the block")
    check(list(dict.fromkeys(sum((g["paths"] for g in saved["groups"]), []))) == list(plog["sched_cached_tracks"]), "the saved groups describe exactly the saved path list")

    now = times[-1]
    probe = {os.path.normpath(x["path"]): {"duration": x["duration"]} for x in content}
    with patch.object(media, "_cached_path_exists", lambda p: True):
        read_back, _ = media.read_block_lineup("05", channels["05"], "Evening", now, probe, lambda p: True, lambda ci, off: 1)
    last_used = used[-1]
    check(_lineup_signature(read_back) == last_used, "the read-only read of the block on air gives the lineup the station airs")

    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(src.count("lineup_groups(") >= 3, "the scheduler saves the groups at its new-block, re-flavor and cycle saves")
    check(src.count('cached_groups=playback_log.get("sched_cached_groups")') >= 2, "both scheduler reads give the saved groups to the shared function")
    check('cached_groups=playback_log.get("sched_cached_groups")' in open(_media_script_path(), encoding="utf-8").read(), "the read-only read gives the saved groups to the shared function")

    ns2 = _sched_record_ns({"05": _sched_record_channel("full_day", "interrupt_half_hour", "random_slots", True)}, [datetime.datetime(2026, 10, 13, 0, 0, 5)], [0])
    freeze = ns2["_freeze_aired_prefix_durations"]
    pieces = [{"path": "/m/a.mkv", "duration": 880, "start_time": 0, "is_commercial": False, "seek_offset": 0},
              {"path": "/m/ad.mkv", "duration": 30, "start_time": 880, "is_commercial": True, "seek_offset": 0},
              {"path": "/m/a.mkv", "duration": 880, "start_time": 910, "is_commercial": False, "seek_offset": 880},
              {"path": "/m/ad.mkv", "duration": 30, "start_time": 1790, "is_commercial": True, "seek_offset": 0},
              {"path": "/m/a.mkv", "duration": 880, "start_time": 1820, "is_commercial": False, "seek_offset": 1760},
              {"path": "/m/b.mkv", "duration": 1500, "start_time": 2700, "is_commercial": False, "seek_offset": 0}]
    durs, frozen = {}, set()
    freeze(pieces, pieces[0], durs, frozen)
    check(durs == {"/m/a.mkv": 2640}, "an episode airing in commercial-split pieces is frozen at its whole length, not the first piece's (%s)" % durs)
    durs, frozen = {}, set()
    freeze(pieces, pieces[5], durs, frozen)
    check(durs == {"/m/a.mkv": 2640, "/m/ad.mkv": 30, "/m/b.mkv": 1500} or durs.get("/m/a.mkv") == 2640 and durs.get("/m/b.mkv") == 1500,
          "every episode up to the one airing is frozen at its whole length")


def suite_lineup_groups_swap():
    import copy as _cp
    ns = {"db": types.SimpleNamespace(config={"media_probe_cache": {}}, save_settings=lambda **k: None),
          "log": MagicMock(), "media": media, "random": __import__("random"),
          "PATH_MISSING": "missing", "PATH_UNKNOWN": "unknown",
          "_path_status": lambda p: "ok", "_log_info_throttled": lambda *a, **k: None,
          "_mark_file_broken": lambda *a, **k: None,
          "resolve_track_duration": media.resolve_track_duration,
          "split_movies_and_series": media.split_movies_and_series,
          "pair_short_episodes": media.pair_short_episodes, "build_raw_tracks": media.build_raw_tracks, "build_pending_tracks": media.build_pending_tracks,
          "_cached_path_exists": lambda p: True,
          "note_random_episode_played": media.note_random_episode_played}
    for fn in ("_get_show_episode_context", "_show_rotation_entries", "_show_raw_episodes", "_keep_swap_groups", "_resolve_broken_episode"):
        exec(compile(get_function_source(fn), "<extracted:%s>" % fn, "exec"), ns)

    def eps(name, durs):
        return [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": d} for i, d in enumerate(durs)]

    checked = 0
    survived = 0
    for order_mode in ("sequential", "random"):
        for block in range(8):
            raw = eps("Alpha", [600] * 8 + [1500, 600, 600, 1500, 600] ) + eps("Beta", [600] * 7)
            info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 15, "episode_order_mode": order_mode,
                    "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
            lineup, show_pos, _ = media.build_block_rotation(raw, info, {}, 2026100 + block, 28800, "05", None, "1:200")
            plog = info["playback_log"]
            plog["show_pos"] = show_pos
            plog["sched_cached_tracks"] = list(dict.fromkeys(media.lineup_paths(lineup)))
            plog["sched_cached_groups"] = media.lineup_groups(lineup, 900)
            before_sig = _lineup_signature(lineup)
            alpha_paths = [p for p in plog["sched_cached_tracks"] if "/Alpha/" in p]
            if not alpha_paths:
                continue
            failed = alpha_paths[len(alpha_paths) // 2]
            result = ns["_resolve_broken_episode"](info, failed, "Evening", "05")
            info["broken_files"] = [failed]
            if result is None:
                check("sched_cached_groups" not in plog or media._matching_lineup_groups(plog["sched_cached_groups"], plog["sched_cached_tracks"], 900) is not None,
                      "%s block %d: a refused swap leaves matching groups" % (order_mode, block))
                continue
            checked += 1
            new_paths = plog["sched_cached_tracks"]
            record = plog.get("sched_cached_groups")
            check(record is not None and media._matching_lineup_groups(record, new_paths, 900) is not None,
                  "%s block %d: after a swap the saved groups describe exactly the saved path list" % (order_mode, block))
            if record is None:
                continue
            live = [x for x in raw if x["path"] not in info["broken_files"]]
            after = media.saved_block_lineup(new_paths, {}, live, live, info["broken_files"], 900, cached_groups=record)
            after_sig = _lineup_signature(after)
            flat = list(dict.fromkeys(p for _, ps in after_sig for p in ps))
            check(flat == list(dict.fromkeys(new_paths)), "%s block %d: the swapped lineup airs every saved file once, in the saved order" % (order_mode, block))
            check(failed not in flat, "%s block %d: the broken file is gone from the lineup" % (order_mode, block))
            beta_before = [e for e in before_sig if "/Beta/" in e[1][0]]
            beta_after = [e for e in after_sig if "/Beta/" in e[1][0]]
            check(beta_before == beta_after, "%s block %d: another show's entries are untouched by the swap" % (order_mode, block))
            idx = next(i for i, (k, ps) in enumerate(before_sig) if failed in ps)
            check(after_sig[:idx] == before_sig[:idx], "%s block %d: nothing before the failed entry moves" % (order_mode, block))
            survived += 1
    check(checked > 0 and survived > 0, "the swap blocks ran (%d swapped)" % checked)

    raw_b = eps("Gamma", [600, 1500, 600, 1500, 600])
    lineup_b = media.pair_short_episodes(raw_b, threshold_seconds=900)
    cached_b = list(dict.fromkeys(media.lineup_paths(lineup_b)))
    record_b = media.lineup_groups(lineup_b, 900)
    first = next(g for g in record_b["groups"] if g["kind"] == "bundle")["paths"]
    swapped = media.swap_lineup_groups(record_b, cached_b, [p for p in cached_b if p != first[1]] if False else cached_b,
                                       {}, 900, lambda p: 600)
    check(swapped is not None and swapped["groups"] == record_b["groups"], "a swap with no replacement keeps every group")
    check(media.swap_lineup_groups(None, cached_b, cached_b, {}, 900, lambda p: 600) is None, "no record gives no groups")
    wrong = media.swap_lineup_groups(record_b, cached_b, cached_b[:-1], {}, 900, lambda p: 600)
    check(wrong is None, "a result that does not describe the new path list is dropped")

    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(src.count("_keep_swap_groups(") >= 3, "both swap branches rewrite the saved groups")


def suite_bundle_freeze():
    ns = _sched_record_ns({"05": _sched_record_channel("full_day", "interrupt_half_hour", "random_slots", True)}, [datetime.datetime(2026, 10, 13, 0, 0, 5)], [0])
    items = [{"path": "/m/s/a.mkv", "duration": 600}, {"path": "/m/s/x.mkv", "duration": 1500}, {"path": "/m/s/b.mkv", "duration": 540},
             {"path": "/m/s/y.mkv", "duration": 1500}, {"path": "/m/s/c.mkv", "duration": 660}]
    bundle = media.pair_short_episodes(items, threshold_seconds=900)
    bundle = [b for b in bundle if b.get("is_bundle")]
    check(len(bundle) == 1, "three separated short episodes make one leftover bundle")
    timeline, total, _ = media.build_channel_timeline(bundle, [], {"commercial_placement": "end_of_show", "block_mode": "full_day"}, 1)
    entry = next(e for e in timeline if not e.get("is_commercial"))
    check(entry.get("is_bundle") and entry.get("bundle_item_paths") == ["/m/s/a.mkv", "/m/s/b.mkv", "/m/s/c.mkv"]
          and entry.get("bundle_item_durations") == [600, 540, 660], "a bundle's timeline entry carries each of its files and its own length")
    durs, frozen = {}, set()
    ns["_freeze_aired_prefix_durations"](timeline, entry, durs, frozen)
    check(durs == {"/m/s/a.mkv": 600, "/m/s/b.mkv": 540, "/m/s/c.mkv": 660}, "the freeze stores each bundle file's own length, not the sum under the first file (%s)" % durs)


class TestLineupGroupsSwap(_SectionCase):
    """A swap keeps the saved groups in step with the saved path list, and a bundle freezes each of its files"""

    def test_lineup_groups_swap(self):
        self.run_section(suite_lineup_groups_swap)

    def test_bundle_freeze(self):
        self.run_section(suite_bundle_freeze)


class TestLineupGroups(_SectionCase):
    """A block's saved groups make every pass air the lineup the rotation built, and the freeze keeps whole episode lengths"""

    def test_lineup_groups(self):
        self.run_section(suite_lineup_groups)


class TestBrokenSwapSequentialFull(_SectionCase):
    """A sequential swap on a show that fills its block never puts an episode in the block twice; it refuses instead"""

    def test_broken_swap_sequential_full(self):
        self.run_section(suite_broken_swap_sequential_full)


# ============================================================================
# SECTION: Broken episode swap, random order
# ============================================================================

def suite_broken_swap_random():
    import copy as _cp
    import random as _rnd
    ns = {"db": types.SimpleNamespace(config={"media_probe_cache": {}}, save_settings=lambda **k: None),
          "log": MagicMock(), "media": media, "random": _rnd, "PATH_MISSING": "missing", "PATH_UNKNOWN": "unknown",
          "_path_status": lambda p: "ok", "_log_info_throttled": lambda *a, **k: None,
          "_mark_file_broken": lambda *a, **k: None,
          "resolve_track_duration": media.resolve_track_duration, "split_movies_and_series": media.split_movies_and_series,
          "pair_short_episodes": media.pair_short_episodes, "build_raw_tracks": media.build_raw_tracks, "build_pending_tracks": media.build_pending_tracks,
          "_cached_path_exists": lambda p: True, "note_random_episode_played": media.note_random_episode_played}
    for fn in ("_get_show_episode_context", "_show_rotation_entries", "_show_raw_episodes", "_keep_swap_groups", "_resolve_broken_episode"):
        exec(compile(get_function_source(fn), "<extracted:%s>" % fn, "exec"), ns)

    def eps(name, n, dur):
        return [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": dur} for i in range(n)]

    def expand(lineup):
        out = []
        for t in lineup:
            out += [t.get("pair_ep1_path", t["path"]), t.get("pair_ep2_path", "")] if t.get("is_pair") else [t["path"]]
        return [p for p in out if p]

    for pair_minutes, dur, count in ((0, 1500, 12), (15, 600, 24)):
        raw = eps("Alpha", count, dur) + eps("Beta", count, dur)
        info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": pair_minutes, "episode_order_mode": "random",
                "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
        _, pos1, _ = media.build_block_rotation(raw, info, {}, 2026100, 28800, "05", None, "1:200")
        lineup2, pos2, _ = media.build_block_rotation(raw, info, pos1, 2026101, 28800, "05", None, "1:200")
        info["playback_log"]["show_pos"] = pos2
        info["playback_log"]["sched_cached_tracks"] = list(dict.fromkeys(expand(lineup2)))
        label = "pairing %d min" % pair_minutes
        entries = ns["_show_rotation_entries"](info, "Evening", "Alpha")
        in_bag = {p for i, e in enumerate(entries) if i in set(media.random_bag_entry_indices(pos2, "Alpha", entries, channel_id="05"))
                  for p in ([e.get("pair_ep1_path"), e.get("pair_ep2_path")] if e.get("is_pair") else [e["path"]])}
        before = list(info["playback_log"]["sched_cached_tracks"])
        planned = [p for p in before if "/Alpha/" in p]
        for trial in range(25):
            trial_info = _cp.deepcopy(info)
            _rnd.seed(trial)
            failed = planned[trial % len(planned)]
            sub = ns["_resolve_broken_episode"](trial_info, failed, "Evening", channel_id="05")
            after = trial_info["playback_log"]["sched_cached_tracks"]
            ok = (sub is not None and sub["file"] in in_bag and sub["file"] not in before and sub["file"] != failed)
            check(ok, label + ", trial %d: the substitute is an episode still in the shuffle bag and not in this block" % trial)
            if not ok:
                break
            at = before.index(failed)
            new_paths = after[at:at + (len(after) - len(before) + 1)]
            check(after[:at] == before[:at] and after[at + len(new_paths):] == before[at + 1:] and new_paths[0] == sub["file"],
                  label + ", trial %d: only the failed file is replaced, nothing aired is removed" % trial)
            bag_after = trial_info["playback_log"]["show_pos"]["05:alpha"]["bag"]
            check(len(bag_after) == len(pos2["05:alpha"]["bag"]) - 1,
                  label + ", trial %d: the substitute leaves the bag, so it cannot be drawn again this cycle" % trial)

    tiny = eps("Tiny", 3, 1500)
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 0, "episode_order_mode": "random",
            "schedules": {"Evening": tiny}, "playback_log": {}, "broken_files": []}
    lineup, pos, _ = media.build_block_rotation(tiny, info, {}, 2026100, 28800, "05", None, "1:200")
    info["playback_log"]["show_pos"] = pos
    info["playback_log"]["sched_cached_tracks"] = list(dict.fromkeys(expand(lineup)))
    saved = list(info["playback_log"]["sched_cached_tracks"])
    failed = next(p for p in saved if "/Tiny/" in p)
    sub = ns["_resolve_broken_episode"](info, failed, "Evening", channel_id="05")
    check(sub is None and info["playback_log"]["sched_cached_tracks"] == saved,
          "a show whose other episodes are all in this block or already aired gets no substitute and its saved lineup is left as it was")


class TestBrokenSwapRandom(_SectionCase):
    """A broken episode in random order is replaced only by an episode still in the shuffle bag"""

    def test_broken_swap_random(self):
        self.run_section(suite_broken_swap_random)


# ============================================================================
# SECTION: Two slot mode with small shows
# ============================================================================

def suite_two_slots_small_shows():
    sizes = {"S1": 1, "S2": 2, "S3": 3, "S5": 5}
    raw = [{"path": "/m/tv/%s/%s S01E%02d.mkv" % (name, name, i + 1), "duration": 1500}
           for name, n in sizes.items() for i in range(n)]
    for order_mode in ("sequential", "random"):
        info = {"scheduling_mode": "two_slots", "pair_threshold_minutes": 0, "episode_order_mode": order_mode}
        pos, drawn, dup_in_block = {}, {name: [] for name in sizes}, []
        for block in range(12):
            lineup, pos, _ = media.build_block_rotation(raw, info, pos, 2026100 + block, 28800, "05", None, "1:200")
            paths = [t["path"] for t in lineup]
            if len(paths) != len(set(paths)):
                dup_in_block.append(block)
            for path in paths:
                drawn[path.split("/")[3]].append(path)
        check(not dup_in_block, "%s: no file airs twice inside one block (blocks %s)" % (order_mode, dup_in_block))
        check(all(len(drawn[name]) > 0 for name in sizes), "%s: every show airs" % order_mode)
        for name, n in sizes.items():
            seq = drawn[name]
            cycles_ok = all(len(set(seq[k:k + n])) == len(seq[k:k + n]) for k in range(0, len(seq) - n + 1, n))
            check(cycles_ok, "%s: show %s (%d episodes) airs every episode once before any comes round again" % (order_mode, name, n))
    info = {"scheduling_mode": "two_slots", "pair_threshold_minutes": 15, "episode_order_mode": "sequential"}
    paired = [{"path": "/m/tv/Tiny/Tiny S01E%02d.mkv" % (i + 1), "duration": 600} for i in range(2)]
    paired += [{"path": "/m/tv/Big/Big S01E%02d.mkv" % (i + 1), "duration": 1500} for i in range(6)]
    lineup, _, _ = media.build_block_rotation(paired, info, {}, 2026100, 28800, "05", None, "1:200")
    tiny = [t for t in lineup if "/Tiny/" in t["path"]]
    check(len(tiny) == 1 and tiny[0].get("is_pair"), "two short episodes that pair into one entry give that entry once, not twice")


class TestTwoSlotsSmallShows(_SectionCase):
    """In two slot mode a show never gets more entries in a block than it has"""

    def test_two_slots_small_shows(self):
        self.run_section(suite_two_slots_small_shows)


# ============================================================================
# SECTION: Commercial tracks (one shared builder)
# ============================================================================

def suite_commercial_tracks():
    cache = {os.path.normpath("/c/probed.mp4"): {"duration": 45.7}}
    items = [
        {"path": "/c/probed.mp4", "duration": 10},
        {"path": "/c/stored.mp4", "duration": 62},
        {"path": "/c/nostore.mp4"},
        {"path": "/c/zero.mp4", "duration": 0},
        {"path": "/c/long.mp4", "duration": 301},
        {"path": "/c/missing.mp4", "duration": 20},
        "/c/plain.mp4",
        {"file": "/c/viafile.mp4", "duration": 15},
        {"path": "", "duration": 15},
    ]
    exists = lambda p: "missing" not in p
    out = media.build_commercial_tracks(items, cache, exists)
    got = {t["path"]: t["duration"] for t in out}
    check(got.get("/c/probed.mp4") == 45, "a probed duration wins over the stored one and is a whole number")
    check(got.get("/c/stored.mp4") == 62, "the stored duration is used when nothing was probed")
    check(got.get("/c/nostore.mp4") == 30 and got.get("/c/zero.mp4") == 30, "no duration or zero becomes the 30 second default")
    check("/c/long.mp4" not in got, "a commercial over the longest allowed length is left out")
    check("/c/missing.mp4" not in got, "a path that does not exist is left out")
    check(got.get("/c/plain.mp4") == 30 and got.get("/c/viafile.mp4") == 15, "a plain string and a file key are both read")
    check(len(out) == 6 and [t["path"] for t in out] == [p for p in ["/c/probed.mp4", "/c/stored.mp4", "/c/nostore.mp4", "/c/zero.mp4", "/c/plain.mp4", "/c/viafile.mp4"]], "order is kept and nothing else is added")
    check(all(set(t) == {"path", "duration"} and isinstance(t["duration"], int) for t in out), "each track holds only a path and an int duration")
    uncapped = media.build_commercial_tracks(items, cache, exists, max_seconds=None)
    check(any(t["path"] == "/c/long.mp4" and t["duration"] == 301 for t in uncapped), "with no cap the long commercial is kept")
    check(media.COMMERCIAL_DEFAULT_SECONDS == 30 and media.COMMERCIAL_MAX_SECONDS == 300, "default and longest length are 30 and 300 seconds")
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(src.count("build_commercial_tracks(") == 2, "the main script builds commercial tracks only through the shared function")
    check("COMMERCIAL_DEFAULT_DUR" not in src and "_H_COMMERCIAL_MAX_SECONDS" not in src, "the old local copies are gone from the main script")


class TestCommercialTracks(_SectionCase):
    """One function builds the commercial tracks for the scheduler"""

    def test_commercial_tracks(self):
        self.run_section(suite_commercial_tracks)


# ============================================================================
# SECTION: Block seeds (one shared seed maths)
# ============================================================================

def suite_block_seeds():
    expected = {"Morning": 100, "Evening": 200, "Night": 300, "Full Day": 500}
    for i, name in enumerate(media.SIX_HOUR_BLOCK_NAMES):
        expected[name] = 600 + i * 100
    check(media.BLOCK_SEED_OFFSETS == expected, "the offset table holds the three blocks, Full Day and the six 4 hour blocks")
    check(all(media.block_seed_offset(n) == v for n, v in expected.items()), "every block name gives its offset")
    check(media.block_seed_offset("Marathon", 0) == 0 and media.block_seed_offset("Marathon", 400) == 400, "a name with no offset gives the caller's default")
    d = datetime.date(2026, 10, 13)
    check(media.block_day_seed(d, "05", 300) == 2026 * 1000 + 286 + 5 + 300, "the seed is year x 1000 + day of year + channel + offset")
    check(media.block_day_seed(datetime.datetime(2026, 1, 1, 3, 0), 7, 0) == 2026 * 1000 + 1 + 7, "a datetime and a plain channel number work too")
    check(media.block_day_seed(d, 5, 100) == media.block_day_seed(d, "05", 100), "channel 5 and 05 give the same seed")
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check("_block_seed_offsets" not in src and "_peek_offsets" not in src, "the main script keeps no offset table of its own")
    check(src.count("block_day_seed(") == 3, "the scheduler, the overlap and the catch-up use the shared seed; the read-only lineup in media.py uses it too")
    check("tm_yday + int(target_clean_key) + block_offset" not in src, "the scheduler no longer works out its seed by hand")


def suite_block_rotation():
    d = datetime.datetime(2026, 10, 13, 2, 30)
    check(media.block_start_date(d, "Night") == datetime.datetime(2026, 10, 12, 2, 30), "the Night block between midnight and 04:00 started yesterday")
    check(media.block_start_date(datetime.datetime(2026, 10, 13, 4, 0), "Night") == datetime.datetime(2026, 10, 13, 4, 0), "at 04:00 and after it is today")
    check(media.block_start_date(d, "Morning") == d and media.block_start_date(d, "Block 1") == d, "every other block and name starts today")
    eps = [{"path": "/m/A/S01E%02d.mkv" % i, "duration": 1500} for i in range(1, 7)] + [{"path": "/m/B/S01E%02d.mkv" % i, "duration": 1500} for i in range(1, 7)]
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 0, "episode_order_mode": "sequential"}
    direct = media.build_show_rotation(eps, "random_slots", {}, 123, pair_threshold_seconds=0, episode_order_mode="sequential",
                                       fixed_show_order=None, block_duration_seconds=8 * 3600, channel_id="05", order_seed="1:200")
    wrapped = media.build_block_rotation(eps, info, {}, 123, 8 * 3600, "05", None, "1:200")
    check(wrapped == direct, "the wrapper gives what build_show_rotation gives with the channel's settings")
    info2 = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 30, "episode_order_mode": "random"}
    direct2 = media.build_show_rotation(eps, "random_slots", {}, 123, pair_threshold_seconds=1800, episode_order_mode="random",
                                        fixed_show_order=None, block_duration_seconds=8 * 3600, channel_id="05", order_seed="1:200")
    check(media.build_block_rotation(eps, info2, {}, 123, 8 * 3600, "05", None, "1:200") == direct2, "pairing threshold and episode order come from the channel")
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(src.count("build_block_rotation(") == 3, "the scheduler (three places) builds a rotation through the wrapper; the read-only lineup in media.py does too")
    check("_seed_now = (now - datetime.timedelta" not in src, "the next-up builder takes the block's start date from the shared function")


class TestBlockRotation(_SectionCase):
    """One wrapper builds a block's rotation from the channel's settings"""

    def test_block_rotation(self):
        self.run_section(suite_block_rotation)


class TestBlockSeeds(_SectionCase):
    """One shared function holds the block offsets and the date-and-channel seed"""

    def test_block_seeds(self):
        self.run_section(suite_block_seeds)


# ============================================================================
# SECTION: Sequential position follows the file
# ============================================================================

def _seq_paths(entry):
    if entry.get("is_pair"):
        return [entry["pair_ep1_path"], entry["pair_ep2_path"]]
    if entry.get("is_bundle"):
        return [item["path"] for item in entry["bundle_items"]]
    return [entry["path"]]


def _seq_show(durations, name="Show X"):
    return [{"path": "/m/%s/Season 1/%s S01E%02d.mkv" % (name, name, i + 1), "duration": d} for i, d in enumerate(durations)]


def suite_sequential_position_by_file():
    import random as _rnd
    mismatches = 0
    for seed in range(300):
        rng = _rnd.Random(seed)
        eps = _seq_show([rng.choice([600, 700, 800, 1500, 1800, 2700]) for _ in range(rng.randint(1, 12))])
        thr = rng.choice([0, 900])
        paired = media.pair_short_episodes(eps, thr) if thr > 0 else list(eps)
        old = new = rng.randrange(len(paired))
        for _ in range(8):
            count = rng.randint(1, 3)
            expected = [_seq_paths(paired[(old + i) % len(paired)]) for i in range(count)]
            old = (old + count) % len(paired)
            got, new = media.take_sequential_entries(eps, new, count, thr)
            mismatches += expected != [_seq_paths(e) for e in got]
    check(mismatches == 0, "with no file joining or leaving, 2400 blocks give the lineups the list-number position gave (%d differ)" % mismatches)

    long_eps = _seq_show([1800] * 6)
    entries, state = media.take_sequential_entries(long_eps, 0, 2, 900)
    check(isinstance(state, dict) and state["next"] == long_eps[2]["path"] and state["owed"] == [], "the saved position is the file that airs next, with nothing owed")
    entries, state = media.take_sequential_entries(long_eps, 4, 3, 900)
    check([_seq_paths(e)[0] for e in entries] == [long_eps[4]["path"], long_eps[5]["path"], long_eps[0]["path"]] and state["next"] == long_eps[1]["path"], "an old saved number is turned into the file it pointed at and the show carries on")
    check(media.take_sequential_entries([], {"next": None, "owed": []}, 2, 900) == ([], {"next": None, "owed": []}), "an empty show gives nothing and keeps an empty position")

    violations = skips = 0
    for seed in range(400):
        rng = _rnd.Random(seed)
        pool = list(range(1, 15))
        present = sorted(rng.sample(pool, rng.randint(3, 8)))
        make = lambda ids: [{"path": "/m/Show X/Season 1/Show X S01E%02d.mkv" % i, "duration": 1800} for i in ids]
        state = 0
        aired = set()
        pass_start = set(present)
        for _ in range(25):
            roll = rng.random()
            if roll < 0.25 and len(present) < 12:
                present = sorted(present + [rng.choice([i for i in pool if i not in present])])
            elif roll < 0.45 and len(present) > 3:
                gone = rng.choice(present)
                present = [i for i in present if i != gone]
                aired.discard("/m/Show X/Season 1/Show X S01E%02d.mkv" % gone)
                pass_start.discard("/m/Show X/Season 1/Show X S01E%02d.mkv" % gone)
            eps2 = make(present)
            current = {e["path"] for e in eps2}
            got, state = media.take_sequential_entries(eps2, state, rng.randint(1, 3), 900)
            for entry in got:
                for path in _seq_paths(entry):
                    if path in aired:
                        needed = pass_start & current
                        skips += not needed <= aired
                        aired = set()
                        pass_start = set(current)
                    aired.add(path)
                    violations += path not in current
    check(violations == 0 and skips == 0, "with files joining and leaving between blocks, a file never airs again before the others of its pass (repeats or gone files %d, skipped %d)" % (violations, skips))

    shorts = _seq_show([600, 600, 600, 600, 1500, 600])
    entries, state = media.take_sequential_entries(shorts, 0, 1, 900)
    check(_seq_paths(entries[0]) == [shorts[0]["path"], shorts[1]["path"]] and state["next"] == shorts[2]["path"], "a short episode takes the next episode in line and they air as one guide block")
    joined = _seq_show([600, 600, 600, 600, 1500, 600], "Show X")
    joined.insert(0, {"path": "/m/Show X/Season 1/Show X S00E09.mkv", "duration": 600})
    entries, state = media.take_sequential_entries(joined, {"next": shorts[2]["path"], "owed": []}, 1, 900)
    check(_seq_paths(entries[0]) == [shorts[2]["path"], shorts[3]["path"]], "an episode that already aired is never pulled into a pair when a short file joins before it")
    entries, state = media.take_sequential_entries(shorts, {"next": shorts[2]["path"], "owed": []}, 3, 900)
    check([p for e in entries for p in _seq_paths(e)] == [shorts[2]["path"], shorts[3]["path"], shorts[4]["path"], shorts[5]["path"]], "pairing runs forward from the file that airs next, and the leftover short file airs in the bundle after the long one")
    flat = [p for e in entries for p in _seq_paths(e)]
    check(len(flat) == len(set(flat)), "no file is in the lineup twice in one take")

    odd = _seq_show([600, 600, 600])
    seen = []
    state = 0
    for _ in range(4):
        got, state = media.take_sequential_entries(odd, state, 1, 900)
        seen.append([os.path.basename(p)[-10:-4] for p in _seq_paths(got[0])])
    check(seen == [["S01E01", "S01E02"], ["S01E03"], ["S01E01", "S01E02"], ["S01E03"]], "a short episode with no partner airs once per pass in the leftover bundle (%s)" % seen)

    swapped = media.sequential_state_after_path(long_eps, long_eps[2]["path"], 0, 900)
    check(swapped == {"next": long_eps[3]["path"], "owed": []}, "after a swap the position is the file after the last one aired")
    check(media.sequential_state_after_path(long_eps, long_eps[5]["path"], 0, 900) == {"next": None, "owed": []}, "after the last file of the show the position wraps to the top")
    check(media.sequential_state_after_path(long_eps, "/m/gone.mkv", 0, 900) is None, "a path that is not in the show gives no position")
    shifted = media.sequential_state_shifted(long_eps, {"next": long_eps[1]["path"], "owed": []}, 2, 900)
    check(shifted == {"next": long_eps[3]["path"], "owed": []}, "a shifted position moves forward by files")

    show_pos = {}
    block_a = long_eps
    block_b = long_eps[1:]
    info = {"scheduling_mode": "one_slot", "pair_threshold_minutes": 15, "episode_order_mode": "sequential"}
    order = []
    for index in range(10):
        raw = block_a if index % 2 == 0 else block_b
        lineup, show_pos, _ = media.build_block_rotation(raw, info, show_pos, 2026100 + index, 28800, "05", None, "1:200")
        order.append(os.path.basename(lineup[0]["path"])[-10:-4])
    check(order == ["S01E01", "S01E02", "S01E03", "S01E04", "S01E05", "S01E06", "S01E01", "S01E02", "S01E03", "S01E04"], "two blocks with different file lists share one position that follows the file (%s)" % order)

    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check("def _show_raw_episodes(" in src and "sequential_state_after_path(" in src, "the broken-episode swap sets the position by file")
    check("% _rot_n" not in src, "the swap writes no list number as a position any more")


class TestSequentialPositionByFile(_SectionCase):
    """A sequential show's saved position is the file that airs next, and pairing runs forward from it"""

    def test_sequential_position_by_file(self):
        self.run_section(suite_sequential_position_by_file)


# ============================================================================
# SECTION: Random show and movie list position by file
# ============================================================================

def suite_random_position_by_file():
    import random as _rnd
    import copy as _cp

    def entries_for(numbers, name="Show X"):
        return [{"path": "/m/%s/Season 1/%s S01E%02d.mkv" % (name, name, n), "duration": 1800} for n in numbers]

    def paths(picked):
        return [e["path"] for e in picked]

    mismatches = 0
    for seed in range(300):
        rng = _rnd.Random(seed)
        pool = entries_for(range(1, rng.randint(1, 14) + 1))
        old_state = new_state = None
        for block in range(10):
            count = rng.randint(1, 4)
            prefix = "%d:%d:S" % (seed, block)
            idxs, old_state = media._draw_from_shuffle_bag(old_state, len(pool), count, prefix)
            picked, new_state = media.draw_from_shuffle_bag_by_file(new_state, pool, count, prefix)
            mismatches += paths(picked) != [pool[i]["path"] for i in idxs]
    check(mismatches == 0, "with no file joining or leaving, 3000 blocks give the lineups the list-number bag gave (%d differ)" % mismatches)

    mismatches = 0
    for seed in range(300):
        rng = _rnd.Random(seed)
        pool = entries_for(range(1, rng.randint(2, 14) + 1))
        old_state = None
        for block in range(rng.randint(1, 6)):
            _idx, old_state = media._draw_from_shuffle_bag(old_state, len(pool), rng.randint(1, 4), "%d:%d:S" % (seed, block))
        idxs, _ = media._draw_from_shuffle_bag(_cp.deepcopy(old_state), len(pool), 3, "%d:x:S" % seed)
        picked, saved = media.draw_from_shuffle_bag_by_file(_cp.deepcopy(old_state), pool, 3, "%d:x:S" % seed)
        mismatches += paths(picked) != [pool[i]["path"] for i in idxs]
    check(mismatches == 0, "an old saved bag of list numbers is turned into files once and carries on identically (%d of 300 differ)" % mismatches)
    check(isinstance(saved, dict) and saved.get("v") == media.RANDOM_STATE_VERSION
          and all(isinstance(k, str) for k in saved["bag"] + saved["recent"]), "the saved bag holds file paths, not numbers")

    duplicates = skipped = passes = 0
    for seed in range(400):
        rng = _rnd.Random(seed)
        numbers = list(range(1, rng.randint(4, 12) + 1))
        next_number = max(numbers) + 1
        state = None
        aired, start_pool = {}, {}
        for block in range(25):
            roll = rng.random()
            if roll < 0.25:
                numbers.append(next_number)
                next_number += 1
            elif roll < 0.45 and len(numbers) > 3:
                numbers.pop(rng.randrange(len(numbers)))
            numbers.sort()
            pool = entries_for(numbers)
            for _ in range(rng.randint(1, 3)):
                picked, state = media.draw_from_shuffle_bag_by_file(state, pool, 1, "%d:%d:S" % (seed, block))
                cycle = state["cycle"]
                aired.setdefault(cycle, [])
                start_pool.setdefault(cycle, set(paths(pool)))
                duplicates += picked[0]["path"] in aired[cycle]
                aired[cycle].append(picked[0]["path"])
        for cycle in aired:
            if cycle + 1 in aired:
                passes += 1
                skipped += not start_pool[cycle + 1] <= set(aired[cycle])
    check(passes > 1500 and duplicates == 0 and skipped == 0,
          "in 400 random shows where files join and leave between blocks, no file airs twice in a pass and none is skipped (%d passes, %d repeats, %d skipped)" % (passes, duplicates, skipped))

    pool = entries_for(range(1, 7))
    _picked, state = media.draw_from_shuffle_bag_by_file(None, pool, 2, "j:S")
    joined = pool + entries_for([7])
    seen = []
    for _ in range(len(state["bag"]) + 1):
        picked, state = media.draw_from_shuffle_bag_by_file(state, joined, 1, "j:S")
        seen.append(picked[0]["path"])
    check(joined[-1]["path"] in seen[:len(seen)], "a file that joins mid-pass airs in that pass")
    check(len(set(seen)) == len(seen) and len(seen) == 5, "the pass finishes with each file once, the joined file included (%s)" % [x[-6:-4] for x in seen])

    pool = entries_for(range(1, 6))
    _picked, state = media.draw_from_shuffle_bag_by_file(None, pool, 3, "l:S")
    gone = pool[:2] + pool[3:]
    picked, state = media.draw_from_shuffle_bag_by_file(state, gone, 1, "l:S")
    check(pool[2]["path"] not in state["bag"] + state["recent"] + state["drawn"] + [state["last"]] and pool[2]["path"] not in paths(picked),
          "a file that leaves is taken out of the bag, the recent list and the last played")

    pool = entries_for(range(1, 7))
    _picked, state = media.draw_from_shuffle_bag_by_file(None, pool, 3, "p:S")
    aired_alone = state["drawn"][0]
    other = next(e for e in pool if e["path"] not in state["drawn"])
    pair_entry = media._build_pair_track(next(e for e in pool if e["path"] == aired_alone), other)
    changed = [pair_entry if e["path"] == aired_alone else e for e in pool if e["path"] != other["path"]]
    drawn_after = []
    for _ in range(3):
        picked, state = media.draw_from_shuffle_bag_by_file(state, changed, 1, "p:S")
        drawn_after.append(picked[0])
    check(not any(e.get("is_pair") for e in drawn_after), "an entry that holds a file already aired this pass is kept out of the bag until the next pass")

    played_pool = entries_for(range(1, 6))
    show_pos = {}
    _picked, show_pos["05:show-x"] = media.draw_from_shuffle_bag_by_file(None, played_pool, 1, "n:S")
    target = next(i for i, e in enumerate(played_pool) if e["path"] in show_pos["05:show-x"]["bag"])
    key = played_pool[target]["path"]
    media.note_random_episode_played(show_pos, "Show X", played_pool, target, channel_id="05")
    saved = show_pos["05:show-x"]
    check(key not in saved["bag"] and saved["last"] == key and saved["recent"][-1] == key and key in saved["drawn"],
          "a swapped-in episode leaves the bag, becomes the last played and counts as aired in the pass")
    in_bag_state = {"05:show-x": {"v": media.RANDOM_STATE_VERSION, "bag": [key], "cycle": 1, "last": None, "recent": [],
                                "drawn": [e["path"] for e in played_pool if e["path"] != key]}}
    check(media.random_bag_entry_indices(in_bag_state, "Show X", played_pool, channel_id="05") == [target],
          "the swap reads the entries still in the bag by file")
    old_style = {"05:show-x": {"bag": [1, 3], "cycle": 1, "last": 0, "recent": [0]}}
    check(media.random_bag_entry_indices(old_style, "Show X", played_pool, channel_id="05") == [1, 3],
          "the swap reads an old bag of list numbers once, as the files they pointed at")
    check(media.random_last_entry({"last": 2}, played_pool) is played_pool[2] and media.random_last_entry({"last": played_pool[4]["path"]}, played_pool) is played_pool[4]
          and media.random_last_entry({"last": None}, played_pool) is None, "the catch-up label finds the last played by file or by an old number")

    movies = [{"path": "/m/Movies/Movie %d (200%d).mkv" % (i, i), "duration": 6000} for i in range(1, 6)]
    show_pos = {}
    picked = media._rotation_take_random(show_pos, media.MOVIE_ROTATION_KEY, 2, movies, 123)
    state = show_pos[media.MOVIE_ROTATION_KEY]
    check(len(picked) == 2 and state["v"] == media.RANDOM_STATE_VERSION and all(k.startswith("/m/Movies/") for k in state["bag"]),
          "the movie list saves its bag by file")
    movies2 = movies + [{"path": "/m/Movies/Movie 0 (1999).mkv", "duration": 6000}]
    movies2.sort(key=lambda t: t["path"])
    seen = []
    for _ in range(4):
        seen += paths(media._rotation_take_random(show_pos, media.MOVIE_ROTATION_KEY, 1, movies2, 124))
    check(len(set(seen)) == len(seen) and "/m/Movies/Movie 0 (1999).mkv" in seen and not set(seen) & set(paths(picked)),
          "a movie added at the front of the list airs once before any movie repeats (%s)" % [x[-14:-4] for x in seen])
    show_pos = {media.MOVIE_ROTATION_KEY: {"bag": [3, 1], "cycle": 1, "last": 0, "recent": [0]}}
    picked = media._rotation_take_random(show_pos, media.MOVIE_ROTATION_KEY, 2, movies, 125)
    check(paths(picked) == [movies[3]["path"], movies[1]["path"]], "an old movie bag of list numbers carries on from the same movies")

    raw = entries_for(range(1, 9), "Alpha") + entries_for(range(1, 9), "Beta") + movies
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 15, "episode_order_mode": "random",
            "schedules": {"Evening": raw}, "playback_log": {}, "broken_files": []}
    show_pos = {}
    for block in range(4):
        _lineup, show_pos, _order = media.build_block_rotation(raw, info, show_pos, 2026100 + block, 28800, "05", None, "1:200")
    states = [v for k, v in show_pos.items() if k != "__FAIRNESS__"]
    check(len(states) >= 3 and all(isinstance(v, dict) and v.get("v") == media.RANDOM_STATE_VERSION
                                   and all(isinstance(k, str) for k in v["bag"] + v["recent"]) for v in states),
          "a block build saves every random show and the movie list by file")

    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check("random_bag_entry_indices(" in src and "len(_rot_eps), _idx" not in src,
          "the broken-episode swap reads and writes the bag through the shared functions")


class TestRandomPositionByFile(_SectionCase):
    """A random show's shuffle bag and the movie list's are saved by file, so a file joining or leaving never makes another air twice or be skipped"""

    def test_random_position_by_file(self):
        self.run_section(suite_random_position_by_file)


# ============================================================================
# SECTION: Files not scanned yet are not placed
# ============================================================================

def suite_unscanned_not_placed():
    import copy as _cp

    def exists(_p):
        return True

    def names(tracks):
        return [os.path.basename(t["path"]).replace(".mkv", "") for t in tracks]

    items = [{"path": "/m/A/Season 1/A S01E01.mkv", "duration": 1800},
             {"path": "/m/A/Season 1/A S01E02.mkv", "duration": 0},
             {"path": "/m/Movies/Big (1999).mkv", "duration": 0}]
    raw = media.build_raw_tracks(items, {}, exists)
    check([t["path"] for t in raw] == [items[0]["path"]], "a file with no known duration is left out of the raw tracks")
    check(all(not t.get("_duration_estimated") for t in raw), "no placed track carries the estimated mark")
    check([t["path"] for t in media.build_pending_tracks(items, {}, exists)] == [items[1]["path"], items[2]["path"]], "the files not scanned yet are listed, in schedule order")
    check(len(media.build_raw_tracks(items, {}, exists, include_unknown=True)) == 3, "include_unknown puts every file back, marked estimated")
    probe = {os.path.normpath(items[1]["path"]): {"duration": 1500}}
    check(len(media.build_raw_tracks(items, probe, exists)) == 2 and len(media.build_pending_tracks(items, probe, exists)) == 1, "a duration from the probe cache makes the file known")
    check(media.build_raw_tracks(items, {}, lambda p: "E01" in p) == media.build_raw_tracks(items[:1], {}, exists), "a path that does not exist is neither placed nor pending")
    check(media.build_pending_tracks(items, {}, lambda p: "E01" in p) == [], "a missing file is not listed as pending")

    def run_sequential(known_numbers, pending_numbers, passes, mode="one_slot", thr=900, durations=None, show="Show X"):
        durations = durations or {}
        known = [{"path": "/m/%s/Season 1/%s S01E%02d.mkv" % (show, show, n), "duration": durations.get(n, 1800)} for n in known_numbers]
        pending = [{"path": "/m/%s/Season 1/%s S01E%02d.mkv" % (show, show, n)} for n in pending_numbers]
        state = {}
        aired = []
        for _ in range(passes):
            lineup, state, _o = media.build_show_rotation(known, mode, state, 5, pair_threshold_seconds=thr, channel_id="05", pending_tracks=pending)
            aired.append(names(lineup))
        return aired, state

    aired, _ = run_sequential([1, 2, 4, 5], [3], 4)
    check(aired == [["Show X S01E01"], ["Show X S01E02"], [], []], "a sequential show waits at its first unscanned file and does not step over it: %s" % aired)
    known = [{"path": "/m/Show X/Season 1/Show X S01E%02d.mkv" % n, "duration": 1800} for n in (1, 2, 4, 5)]
    pending = [{"path": "/m/Show X/Season 1/Show X S01E03.mkv"}]
    state = {}
    for _ in range(2):
        _l, state, _o = media.build_show_rotation(known, "one_slot", state, 5, channel_id="05", pending_tracks=pending)
    known.insert(2, {"path": "/m/Show X/Season 1/Show X S01E03.mkv", "duration": 1800})
    out = []
    for _ in range(5):
        lineup, state, _o = media.build_show_rotation(known, "one_slot", state, 5, channel_id="05", pending_tracks=[])
        out.append(names(lineup))
    check(out == [["Show X S01E03"], ["Show X S01E04"], ["Show X S01E05"], ["Show X S01E01"], ["Show X S01E02"]], "when the file is scanned the show carries on with it, then the rest, then starts over: %s" % out)

    aired, _ = run_sequential([1, 2, 3], [4, 5], 5, mode="two_slots")
    check(aired == [["Show X S01E01", "Show X S01E02"], ["Show X S01E03"], [], [], []], "two slots: the show airs what is scanned and waits at the first unscanned file: %s" % aired)

    aired, _ = run_sequential([1, 2, 3], [], 4, mode="two_slots")
    check(aired == [["Show X S01E01", "Show X S01E02"], ["Show X S01E03", "Show X S01E01"], ["Show X S01E02", "Show X S01E03"], ["Show X S01E01", "Show X S01E02"]], "with nothing pending the show wraps as it did before: %s" % aired)

    aired, _ = run_sequential([1, 2, 3], [], 3, mode="one_slot", durations={1: 600})
    short_pending, _ = run_sequential([1], [2], 3, mode="one_slot", durations={1: 600})
    check(short_pending == [[], [], []], "a short episode whose partner is not scanned waits and airs nothing: %s" % short_pending)
    paired, _ = run_sequential([1, 2], [], 1, mode="one_slot", durations={1: 600, 2: 600})
    check(paired == [["Show X S01E01"]], "a short episode and its scanned partner air as one pair")

    lineup, _s, _o = media.build_show_rotation([], "one_slot", {}, 5, channel_id="05", pending_tracks=[{"path": "/m/Show Y/Season 1/Show Y S01E01.mkv"}])
    check(lineup == [], "a series with no scanned episode is not placed")

    def run_random(pool_numbers, pending_numbers, passes, count_mode="one_slot"):
        known = [{"path": "/m/Show R/Season 1/Show R S01E%02d.mkv" % n, "duration": 1800} for n in pool_numbers]
        pending = [{"path": "/m/Show R/Season 1/Show R S01E%02d.mkv" % n} for n in pending_numbers]
        state = {}
        out = []
        for i in range(passes):
            lineup, state, _o = media.build_show_rotation(known, count_mode, state, 7 + i, episode_order_mode="random", channel_id="05", pending_tracks=pending)
            out.append(names(lineup))
        return out, state, known

    out, state, known = run_random([1, 2, 3, 4], [5], 7)
    flat = [n for block in out for n in block]
    check(len(flat) == 4 and len(set(flat)) == 4, "a random show holds its pass: each scanned file airs once, then nothing until the last file is scanned: %s" % out)
    known.append({"path": "/m/Show R/Season 1/Show R S01E05.mkv", "duration": 1800})
    after = []
    for i in range(5):
        lineup, state, _o = media.build_show_rotation(known, "one_slot", state, 40 + i, episode_order_mode="random", channel_id="05", pending_tracks=[])
        after.append(names(lineup))
    flat_after = [n for block in after for n in block]
    check(flat_after[0] == "Show R S01E05" and len(set(flat_after[:5])) == 5, "the file scanned late airs once before any file repeats: %s" % after)

    out, _st, _k = run_random([1], [2], 4)
    check([n for block in out for n in block] == ["Show R S01E01"], "a one-file random show airs it once and holds while another file is not scanned: %s" % out)

    def movie_runs(known_names, pending_names, passes):
        known = [{"path": "/m/Movies/%s.mkv" % n, "duration": 6000} for n in known_names]
        pending = [{"path": "/m/Movies/%s.mkv" % n} for n in pending_names]
        state = {}
        out = []
        for i in range(passes):
            lineup, state, _o = media.build_show_rotation(known, "one_slot", state, 9 + i, channel_id="05", pending_tracks=pending, block_duration_seconds=8 * 3600)
            out.append(names(lineup))
        return out, state, known

    out, state, known = movie_runs(["M1", "M2"], ["M3"], 6)
    flat = [n for block in out for n in block]
    check(len(flat) == 2 and set(flat) == {"M1", "M2"}, "the movie list holds its pass while a movie is not scanned: %s" % out)
    known.append({"path": "/m/Movies/M3.mkv", "duration": 6000})
    lineup, state, _o = media.build_show_rotation(known, "one_slot", state, 99, channel_id="05", pending_tracks=[], block_duration_seconds=8 * 3600)
    check(names(lineup)[0] == "M3", "the movie scanned late airs before any movie repeats: %s" % names(lineup))

    parts = [{"path": "/m/Movies/Epic Part 1.mkv", "duration": 4000}]
    lineup, _s, _o = media.build_show_rotation(parts, "one_slot", {}, 3, channel_id="05", pending_tracks=[{"path": "/m/Movies/Epic Part 2.mkv"}])
    check(lineup == [], "a movie part is not placed while the other part is not scanned")

    old_style = {"next": None, "owed": []}
    migrated, _o = run_sequential([1, 2, 3], [4], 1)[0], None
    known3 = [{"path": "/m/Show X/Season 1/Show X S01E%02d.mkv" % n, "duration": 1800} for n in (1, 2, 3)]
    lineup, state, _o = media.build_show_rotation(known3, "one_slot", {"05:show-x": 2}, 5, channel_id="05", pending_tracks=[{"path": "/m/Show X/Season 1/Show X S01E04.mkv"}])
    check(names(lineup) == ["Show X S01E03"] and state["05:show-x"]["next"].endswith("E04.mkv"), "an old saved list number turns into the file it pointed at, with the pending file ignored in the count")

    fix = media.group_pending_tracks([{"path": "/m/Show X/Season 1/Show X S01E02.mkv"}], ["Show X"], {"Show X": [{"path": "/m/Show X/Season 1/Show X S01E02.mkv", "_season_number": 1, "_episode_number": 2}]})
    check(fix[0] == {}, "a pending file that is the same episode as a scanned one does not block the show")

    main_src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(main_src.count('"reason": "not_scanned"') >= 5, "the scheduler returns Static with the reason not_scanned in every case it has nothing scanned to air")
    check("build_pending_tracks(folder_items" in main_src and "pending_tracks=_pending_tracks" in main_src, "the scheduler passes the files not scanned to every rotation call")
    ui_src = open(os.path.join(os.path.dirname(MAIN_SCRIPT), "ui.py"), encoding="utf-8").read() if os.path.exists(os.path.join(os.path.dirname(MAIN_SCRIPT), "ui.py")) else ""
    if ui_src:
        check("media.build_raw_tracks(" in ui_src and "_duration_estimated" not in ui_src, "the guide builds its tracks through the shared function and holds no copy of the estimated mark")

    rec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_scan_record.json")
    if os.path.exists(rec_path):
        import json as _json
        rec = _json.load(open(rec_path, encoding="utf-8"))
        check(all(calls[0]["result"].get("mode") == "Static" and calls[0]["result"].get("reason") == "not_scanned" for calls in rec.values()), "in every scan scenario the first call, with nothing scanned, is Static")


class TestUnscannedNotPlaced(_SectionCase):
    """A file whose duration is not known is not placed: the rotation waits for it and the station falls back to Static"""

    def test_unscanned_not_placed(self):
        self.run_section(suite_unscanned_not_placed)


def suite_fairness_counts_unscanned():
    def mk(show, nums):
        return [{"path": "/m/%s/Season 1/%s S01E%02d.mkv" % (show, show, n), "duration": 1500} for n in nums]

    def run(c_known, c_pending, cycles, mode):
        raw = mk("Show A", range(1, 9)) + mk("Show B", range(1, 9)) + mk("Show C", c_known)
        pend = [{"path": "/m/Show C/Season 1/Show C S01E%02d.mkv" % n} for n in c_pending]
        state = {}
        out = []
        for _ in range(cycles):
            lineup, state, _o = media.build_show_rotation(raw, mode, state, 5, channel_id="05", pending_tracks=pend,
                                                          fixed_show_order=["Show A", "Show B", "Show C"])
            out.append([os.path.basename(t["path"])[:-4] for t in lineup])
        return out

    partial = run([1, 2], [3, 4, 5, 6], 3, "one_slot")
    whole = run([1, 2, 3, 4, 5, 6], [], 3, "one_slot")
    check(partial == whole, "one slot: a show with 2 of its 6 files scanned joins the same cycles as when all 6 are scanned: %s" % partial)
    check("Show C S01E01" in partial[0], "one slot: the partly scanned show airs in the first cycle, not two cycles late")
    partial = run([1, 2], [3, 4, 5, 6], 2, "two_slots")
    whole = run([1, 2, 3, 4, 5, 6], [], 2, "two_slots")
    check(partial == whole, "two slots: a partly scanned show joins the same cycles as when all its files are scanned: %s" % partial)
    check(run([1, 2, 3, 4, 5, 6], [], 6, "one_slot") == run([1, 2, 3, 4, 5, 6], [], 6, "one_slot"), "with nothing unscanned the result is unchanged between runs")
    late = run([1, 2], [3, 4, 5, 6], 5, "one_slot")
    check(all("Show C S01E03" not in cyc for cyc in late), "the partly scanned show still waits at its first unscanned file")


class TestFairnessCountsUnscanned(_SectionCase):
    """A show's size for the fairness step counts its files not scanned yet, so a partly scanned show is not held out"""

    def test_fairness_counts_unscanned(self):
        self.run_section(suite_fairness_counts_unscanned)


# ============================================================================
# SECTION: Saved block lineups (one shared function)
# ============================================================================

def suite_saved_lineup():
    ep = lambda n, d: {"path": "/m/Show/S01E%02d.mkv" % n, "duration": d}
    raw = [ep(1, 1500), ep(2, 1500), ep(3, 1500)]
    folder = [{"path": t["path"]} for t in raw] + [{"path": "/m/Show/S01E04.mkv"}, {"path": "/m/Show/S01E05.mkv"}]
    saved = [t["path"] for t in raw] + ["/m/Show/S01E04.mkv", "/m/Show/S01E05.mkv", "/m/Gone/x.mkv", ""]
    durs = {"/m/Show/S01E02.mkv": 1320, "/m/Show/S01E04.mkv": 1400, "/m/Show/S01E05.mkv": 1400}
    out, kept = media.reconcile_cached_lineup(saved, raw, folder, ["/m/Show/S01E05.mkv"], durs)
    check([t["path"] for t in out] == [t["path"] for t in raw] + ["/m/Show/S01E04.mkv"], "reconcile keeps files still present, keeps a scheduled file that missed the path check, drops broken, unscheduled and empty paths")
    check(kept == ["/m/Show/S01E04.mkv"] and out[-1] == {"path": "/m/Show/S01E04.mkv", "duration": 1400}, "a file kept only from its stored duration is reported and carries that duration")
    check(media.apply_cached_durations(raw, {}) is raw, "no stored durations returns the same list")
    applied = media.apply_cached_durations(raw, durs)
    check([t["duration"] for t in applied] == [1500, 1320, 1500] and raw[1]["duration"] == 1500, "a stored duration replaces the track's own on a copy and leaves the input alone")
    lineup = media.saved_block_lineup(saved, durs, raw, folder, ["/m/Show/S01E05.mkv"], 0)
    check([t["path"] for t in lineup] == [t["path"] for t in out], "with pairing off the lineup is the reconciled tracks in order")
    check(lineup[1]["duration"] == 1320, "the saved duration is in the lineup")
    paired = media.saved_block_lineup(saved, durs, raw, folder, [], 1800)
    check(any(t.get("is_pair") for t in paired) and len(paired) < 4, "short episodes are paired under the threshold")
    check(media.saved_block_lineup([], {}, raw, folder, [], 900) == [], "no saved paths gives an empty lineup")
    check(media.saved_block_lineup(["/m/Gone/x.mkv"], {}, raw, folder, [], 900) == [], "a lineup with nothing left gives an empty lineup")
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check("def _reconcile_cached_lineup" not in src and "def _apply_cached_durations" not in src, "the main script keeps no copy of the two helpers")
    check(src.count("saved_block_lineup(") == 2, "the scheduler (twice) builds a saved lineup through the shared function; the read-only lineup in media.py does too")


class TestSavedLineup(_SectionCase):
    """One shared function turns a block's saved paths into its lineup"""

    def test_saved_lineup(self):
        self.run_section(suite_saved_lineup)


# ============================================================================
# SECTION: Scheduler record (what calculate_slotted_playback_state returns today)
# ============================================================================

SCHED_RECORD_PATH = os.path.join(HERE, "scheduler_record.json")

SCHED_RECORD_FUNCS = [
    "_apply_repeat_guard", "_cached_lineup_durations_stale", "_channel_intro_seed",
    "_channel_schedule_path_set", "_checkpoint_probe_cache", "_file_in_channel_schedule", "_freeze_aired_prefix_durations",
    "_get_cached_media_probe", "_get_or_set_block_anchor", "_get_probe_library", "_governor_priority_scope",
    "_log_info_throttled", "_log_probe_failure", "_log_warn_throttled", "_marathon_start_seed",
    "_maybe_correct_estimated_target_duration", "_missed_block_day_seeds", "_mixer_seekable_formats",
    "_prime_block_durations_on_cold_start", "_probe_and_persist_duration", "_probe_checkpoint_save_now",
    "_snapshot_track_durations", "_start_order_seed", "_store_cached_media_probe", "_timeline_whole_episode_durations",
    "_visualizer_admission_inputs", "_visualizer_frozen_lineup", "_visualizer_unseekable_paths", "calculate_slotted_playback_state",
]

SCHED_RECORD_GLOBALS = [
    "_EMPTY_FOLDER_DIAG_LOGGED", "_ONDEMAND_DURATION_PROBE_COOLDOWN_S", "_PROBE_CHECKPOINT_MIN_INTERVAL_S",
    "_SCHED_MEMBERSHIP_CACHE_SECONDS", "_THROTTLED_LOG_INTERVAL_S", "_cold_primed_channels", "_cold_primed_channels_lock",
    "_duration_precache_inflight", "_intro_seed_lock", "_ondemand_duration_probe_next", "_probe_block_timeline_cache",
    "_probe_checkpoint_last", "_probe_checkpoint_lock", "_probe_checkpoint_timer", "_probe_library_lock",
    "_probe_library_singleton", "_resolve_log_state", "_throttled_log_last_ts", "_visualizer_lineup_lock",
]

SCHED_RECORD_SCENARIOS = [
    ("eight_hour_interrupt_random", "eight_hour", "interrupt_half_hour", "random_slots", True),
    ("eight_hour_end_of_show_random", "eight_hour", "end_of_show", "random_slots", True),
    ("eight_hour_no_ads_one_slot", "eight_hour", "interrupt_half_hour", "one_slot", False),
    ("eight_hour_two_slots", "eight_hour", "interrupt_half_hour", "two_slots", True),
    ("six_block_random", "six_block", "interrupt_half_hour", "random_slots", True),
    ("full_day_random", "full_day", "interrupt_half_hour", "random_slots", True),
    ("marathon", "full_day", "interrupt_half_hour", "marathon", True),
]


def _sched_record_times(kind):
    base = datetime.datetime(2026, 10, 13, 0, 0, 5)
    if kind == "continuous":
        marks = [base + datetime.timedelta(minutes=37 * i) for i in range(0, 58)]
        for hh, mm, ss in ((3, 59, 30), (4, 0, 5), (19, 59, 50), (20, 0, 5), (23, 59, 59)):
            marks.append(datetime.datetime(2026, 10, 13, hh, mm, ss))
            marks.append(datetime.datetime(2026, 10, 14, hh, mm, ss))
        marks.append(datetime.datetime(2026, 10, 14, 0, 0, 5))
        return sorted(set(marks))
    return [datetime.datetime(2026, 10, 13, 9, 0, 0), datetime.datetime(2026, 10, 13, 21, 0, 0),
            datetime.datetime(2026, 10, 16, 3, 0, 0), datetime.datetime(2026, 10, 16, 10, 0, 0),
            datetime.datetime(2026, 10, 16, 10, 20, 0), datetime.datetime(2026, 10, 18, 22, 0, 0),
            datetime.datetime(2026, 10, 19, 2, 0, 0), datetime.datetime(2026, 10, 19, 13, 0, 0)]


def _sched_record_library():
    def eps(name, n, dur):
        return [{"path": "/m/%s/Season 1/%s S01E%02d.mkv" % (name, name, i + 1), "duration": dur} for i in range(n)]
    shows = eps("Show A", 8, 1320) + eps("Show B", 8, 1500) + eps("Show C", 6, 2640)
    movies = [{"path": "/m/Movies/Big Movie (1999).mkv", "duration": 6300},
              {"path": "/m/Movies/Long Movie (2004).mkv", "duration": 7500}]
    ads = [{"path": "/m/Ads/ad%d.mkv" % i, "duration": d} for i, d in enumerate((15, 30, 30, 45, 60, 30, 15, 60))]
    return shows, movies, ads


def _sched_record_channel(block_mode, placement, sched, commercials):
    shows, movies, ads = _sched_record_library()
    content = shows + movies
    info = {"active": True, "scheduling_mode": sched, "commercials_enabled": commercials,
            "commercial_placement": placement, "pair_threshold_minutes": 15, "block_mode": block_mode,
            "schedules": {}, "playback_log": {"intro_seed": 123456789}}
    names = {"eight_hour": ["Morning", "Evening", "Night"], "six_block": list(media.SIX_HOUR_BLOCK_NAMES),
             "full_day": ["Full Day"]}[block_mode]
    if sched == "marathon":
        names = ["Marathon"]
    for i, name in enumerate(names):
        info["schedules"][name] = [dict(x) for x in (content if block_mode == "full_day" else content[i % 3:])]
    info["schedules"]["Commercials"] = ads
    return info


def _sched_record_ns(channels, clock, ticks):
    probe = {}
    for info in channels.values():
        for items in info["schedules"].values():
            for item in items:
                probe[os.path.normpath(item["path"])] = {"duration": item["duration"]}
    fake_db = types.SimpleNamespace(channels_db=channels, config={"media_probe_cache": probe},
                                    save_settings_async=lambda **k: None, save_settings=lambda **k: None, config_path=None)
    ns = {"db": fake_db, "app_state": {"guide_refresh_token": 0},
          "broadcast_clock": types.SimpleNamespace(now=lambda: clock[0]), "log": MagicMock(), "vlc_engine": None,
          "vlc": None, "datetime": datetime, "os": os, "random": random, "threading": threading, "time": time,
          "pygame": types.SimpleNamespace(time=types.SimpleNamespace(get_ticks=lambda: ticks[0])), "media": media,
          "SHORT_EPISODE_PAIRING_VERSION": media.SHORT_EPISODE_PAIRING_VERSION, "SIX_HOUR_BLOCKS": media.SIX_HOUR_BLOCKS,
          "SIX_HOUR_BLOCK_NAMES": media.SIX_HOUR_BLOCK_NAMES, "build_block_timeline": media.build_block_timeline,
          "build_ordered_tracks": media.build_ordered_tracks, "build_show_rotation": media.build_show_rotation,
          "fast_forward_show_pos": media.fast_forward_show_pos, "pair_short_episodes": media.pair_short_episodes,
          "resolve_track_duration": media.resolve_track_duration,
          "track_duration_is_estimated": media.track_duration_is_estimated,
          "build_raw_tracks": media.build_raw_tracks, "build_pending_tracks": media.build_pending_tracks, "build_commercial_tracks": media.build_commercial_tracks,
          "block_seed_offset": media.block_seed_offset, "block_day_seed": media.block_day_seed,
          "block_start_date": media.block_start_date, "block_key_for": media.block_key_for, "block_length_seconds": media.block_length_seconds,
          "build_block_rotation": media.build_block_rotation,
          "build_channel_timeline": media.build_channel_timeline,
          "reconcile_cached_lineup": media.reconcile_cached_lineup, "apply_cached_durations": media.apply_cached_durations,
          "saved_block_lineup": media.saved_block_lineup, "lineup_paths": media.lineup_paths, "lineup_groups": media.lineup_groups,
          "_cached_path_exists": lambda p: True, "_is_drive_responsive": lambda p: True,
          "_path_exists_cache": {}, "_path_exists_cache_lock": threading.Lock(), "_unresponsive_drive_roots": set(),
          "PATH_MISSING": 0, "PATH_UNKNOWN": 2}
    assigns = {}
    for node in MAIN_TREE.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigns.setdefault(target.id, node)
    lines = MAIN_SRC.splitlines(keepends=True)
    for name in SCHED_RECORD_GLOBALS:
        node = assigns[name]
        exec(compile("".join(lines[node.lineno - 1:node.end_lineno]), "<global:%s>" % name, "exec"), ns)
    for name in SCHED_RECORD_FUNCS:
        exec(compile(get_function_source(name), "<extracted:%s>" % name, "exec"), ns)
    return ns


def _sched_record_round(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {str(k): _sched_record_round(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_sched_record_round(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _sched_record_run(scenario, kind):
    import hashlib
    import json
    name, block_mode, placement, sched, commercials = scenario
    channels = {"05": _sched_record_channel(block_mode, placement, sched, commercials)}
    times = _sched_record_times(kind)
    clock, ticks = [times[0]], [0]
    ns = _sched_record_ns(channels, clock, ticks)
    out = []
    for moment in times:
        clock[0] = moment
        ticks[0] += 10000
        with patch.object(media, "_cached_path_exists", lambda p: True):
            result = _sched_record_round(ns["calculate_slotted_playback_state"]("05"))
        plog = _sched_record_round(channels["05"]["playback_log"])
        digest = hashlib.sha1(json.dumps({k: v for k, v in plog.items() if k != "sched_cached_groups"}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        out.append({"at": moment.isoformat(), "result": result, "state_sha": digest,
                    "show_pos": plog.get("show_pos"), "show_order": plog.get("show_order"),
                    "block_key": plog.get("sched_block_key"), "cycle_total": plog.get("sched_cycle_total"),
                    "commercial_pos": plog.get("commercial_pos"), "refresh": ns["app_state"]["guide_refresh_token"]})
    return out


def _sched_record_all():
    return {"%s/%s" % (s[0], kind): _sched_record_run(s, kind)
            for s in SCHED_RECORD_SCENARIOS for kind in ("continuous", "gappy")}


def suite_scheduler_record():
    import json
    needed = set()
    tops = {n.name: n for n in MAIN_TREE.body if isinstance(n, ast.FunctionDef)}
    pending = ["calculate_slotted_playback_state"]
    while pending:
        current = pending.pop()
        if current in needed or current not in tops:
            continue
        needed.add(current)
        pending.extend(x.id for x in ast.walk(tops[current]) if isinstance(x, ast.Name))
    missing = sorted(needed - set(SCHED_RECORD_FUNCS))
    check(not missing, "the record harness holds every function the scheduler reaches (missing: %s)" % missing)
    extra = sorted(set(SCHED_RECORD_FUNCS) - needed)
    check(not extra, "the record harness holds no function the scheduler no longer reaches (extra: %s)" % extra)

    first = _sched_record_all()
    second = _sched_record_all()
    check(first == second, "the scheduler record is the same on two fresh runs")

    for key, rows in first.items():
        videos = [r for r in rows if r["result"].get("mode") == "Video"]
        check(len(videos) > 0, "%s: the channel plays video at some moment" % key)
        for row in rows:
            res = row["result"]
            check(res.get("mode") in ("Video", "Commercial", "Static", "Visualizer"), "%s %s: a known mode (%s)" % (key, row["at"], res.get("mode")))
            if res.get("mode") in ("Video", "Commercial"):
                check(res.get("file") and res.get("seek", 0) >= 0, "%s %s: a file and a seek of zero or more" % (key, row["at"]))

    if os.environ.get("SCHED_RECORD_WRITE"):
        with open(SCHED_RECORD_PATH, "w", encoding="utf-8") as handle:
            json.dump(first, handle, indent=1, sort_keys=True)
    check(os.path.exists(SCHED_RECORD_PATH), "the stored scheduler record exists at %s" % SCHED_RECORD_PATH)
    if os.path.exists(SCHED_RECORD_PATH):
        with open(SCHED_RECORD_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        check(sorted(stored) == sorted(first), "the stored record covers the same scenarios")
        for key in sorted(set(stored) & set(first)):
            same = json.loads(json.dumps(first[key])) == stored[key]
            if not same:
                bad = next((i for i, (a, b) in enumerate(zip(json.loads(json.dumps(first[key])), stored[key])) if a != b), None)
                where = "" if bad is None else " first difference at %s" % stored[key][bad]["at"]
                check(False, "%s: the scheduler now returns something different from the stored record.%s" % (key, where))
            else:
                check(True, "%s matches the stored record" % key)


class TestSchedulerRecord(_SectionCase):
    """The scheduler's output on a fixed library, channels and clock matches the stored record"""

    def test_scheduler_record(self):
        self.run_section(suite_scheduler_record)


# ============================================================================
# SECTION: Scheduler scan record (files scanned one at a time)
# ============================================================================

SCHED_SCAN_RECORD_PATH = os.path.join(HERE, "scheduler_scan_record.json")

SCHED_SCAN_SCENARIOS = [
    ("scan_eight_hour_two_slots", "eight_hour", "interrupt_half_hour", "two_slots", True, "sequential"),
    ("scan_eight_hour_one_slot", "eight_hour", "interrupt_half_hour", "one_slot", False, "sequential"),
    ("scan_eight_hour_random_slots", "eight_hour", "interrupt_half_hour", "random_slots", True, "sequential"),
    ("scan_eight_hour_two_slots_random_order", "eight_hour", "end_of_show", "two_slots", True, "random"),
    ("scan_six_block_random_slots", "six_block", "interrupt_half_hour", "random_slots", True, "sequential"),
    ("scan_full_day_one_slot", "full_day", "interrupt_half_hour", "one_slot", True, "sequential"),
]


def _sched_scan_order(content):
    """The files in the order their scans arrive: one file of each show and the movies in turn, so no show is scanned whole before the next starts."""
    groups = {}
    for item in content:
        key = item["path"].split("/")[2]
        groups.setdefault(key, []).append(item)
    order = []
    for rank in range(max(len(v) for v in groups.values())):
        for key in sorted(groups):
            if rank < len(groups[key]):
                order.append(groups[key][rank])
    return order


def _sched_scan_channel(block_mode, placement, sched, commercials, order_mode):
    info = _sched_record_channel(block_mode, placement, sched, commercials)
    info["episode_order_mode"] = order_mode
    known = {}
    for items in info["schedules"].values():
        for item in items:
            known[item["path"]] = item["duration"]
            if not item["path"].startswith("/m/Ads/"):
                item["duration"] = 0
    return info, known


def _sched_scan_ns(channels, clock, ticks):
    ns = _sched_record_ns(channels, clock, ticks)
    ns["db"].config["media_probe_cache"].clear()
    no_probe = types.SimpleNamespace(get_or_probe_sync=lambda path, max_age_s=None: None)
    ns["shared_probe_library"] = lambda: no_probe
    return ns


def _sched_scan_run(scenario):
    import hashlib
    import json
    name, block_mode, placement, sched, commercials, order_mode = scenario
    info, known = _sched_scan_channel(block_mode, placement, sched, commercials, order_mode)
    channels = {"05": info}
    shows, movies, ads = _sched_record_library()
    scan_order = _sched_scan_order(shows + movies)
    times = _sched_record_times("continuous")
    clock, ticks = [times[0]], [0]
    ns = _sched_scan_ns(channels, clock, ticks)
    probe = ns["db"].config["media_probe_cache"]
    for ad in ads:
        probe[os.path.normpath(ad["path"])] = {"duration": ad["duration"]}
    out = []
    scanned = 0
    for index, moment in enumerate(times):
        if index > 0 and scanned < len(scan_order):
            item = scan_order[scanned]
            probe[os.path.normpath(item["path"])] = {"duration": known[item["path"]]}
            scanned += 1
        clock[0] = moment
        ticks[0] += 10000
        with patch.object(media, "_cached_path_exists", lambda p: True):
            result = _sched_record_round(ns["calculate_slotted_playback_state"]("05"))
        plog = _sched_record_round(channels["05"]["playback_log"])
        digest = hashlib.sha1(json.dumps({k: v for k, v in plog.items() if k != "sched_cached_groups"}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        out.append({"at": moment.isoformat(), "scanned": scanned, "result": result, "state_sha": digest,
                    "show_pos": plog.get("show_pos"), "show_order": plog.get("show_order"),
                    "block_key": plog.get("sched_block_key"), "cycle_total": plog.get("sched_cycle_total"),
                    "commercial_pos": plog.get("commercial_pos"), "refresh": ns["app_state"]["guide_refresh_token"]})
    return out


def _sched_scan_all():
    return {s[0]: _sched_scan_run(s) for s in SCHED_SCAN_SCENARIOS}


def suite_scheduler_scan_record():
    import json
    first = _sched_scan_all()
    second = _sched_scan_all()
    check(first == second, "the scan record is the same on two fresh runs")

    for key, rows in first.items():
        check(rows[0]["scanned"] == 0 and rows[-1]["scanned"] == 24, "%s: the run starts with no file scanned and ends with all 24 scanned" % key)
        check(len({r["scanned"] for r in rows}) == 25, "%s: every step of the scan is seen by at least one scheduler call" % key)
        for row in rows:
            res = row["result"]
            check(res.get("mode") in ("Video", "Commercial", "Static", "Visualizer"), "%s %s: a known mode (%s)" % (key, row["at"], res.get("mode")))
            if res.get("mode") in ("Video", "Commercial"):
                check(res.get("file") and res.get("seek", 0) >= 0, "%s %s: a file and a seek of zero or more" % (key, row["at"]))

    if os.environ.get("SCHED_RECORD_WRITE"):
        with open(SCHED_SCAN_RECORD_PATH, "w", encoding="utf-8") as handle:
            json.dump(first, handle, indent=1, sort_keys=True)
    check(os.path.exists(SCHED_SCAN_RECORD_PATH), "the stored scan record exists at %s" % SCHED_SCAN_RECORD_PATH)
    if os.path.exists(SCHED_SCAN_RECORD_PATH):
        with open(SCHED_SCAN_RECORD_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        check(sorted(stored) == sorted(first), "the stored scan record covers the same scenarios")
        for key in sorted(set(stored) & set(first)):
            now_rows = json.loads(json.dumps(first[key]))
            if now_rows != stored[key]:
                bad = next((i for i, (a, b) in enumerate(zip(now_rows, stored[key])) if a != b), None)
                where = "" if bad is None else " first difference at %s" % stored[key][bad]["at"]
                check(False, "%s: the scheduler now returns something different from the stored scan record.%s" % (key, where))
            else:
                check(True, "%s matches the stored scan record" % key)


class TestSchedulerScanRecord(_SectionCase):
    """The scheduler's output while files are scanned one at a time matches the stored scan record"""

    def test_scheduler_scan_record(self):
        self.run_section(suite_scheduler_scan_record)


# ============================================================================
# SECTION: Night block date
# ============================================================================

NIGHT_DATE_SCENARIOS = [s for s in SCHED_RECORD_SCENARIOS if s[1] == "eight_hour"]
NIGHT_STATE_KEYS = ("sched_block_key", "sched_cached_tracks", "sched_cached_durations", "show_pos", "show_order",
                    "commercial_pos", "sched_cycle_total")


def _night_date_run(channels, moments):
    clock, ticks = [moments[0]], [0]
    ns = _sched_record_ns(channels, clock, ticks)
    caught = []
    real_fast_forward = ns["fast_forward_show_pos"]

    def spy(raw, mode, show_pos, seeds, *args, **kwargs):
        caught.append(list(seeds))
        return real_fast_forward(raw, mode, show_pos, seeds, *args, **kwargs)

    ns["fast_forward_show_pos"] = spy
    for moment in moments:
        clock[0] = moment
        ticks[0] += 10000
        with patch.object(media, "_cached_path_exists", lambda p: True):
            ns["calculate_slotted_playback_state"]("05")
    plog = _sched_record_round(channels["05"]["playback_log"])
    return {key: plog.get(key) for key in NIGHT_STATE_KEYS}, caught


def suite_night_block_date():
    library_ready = set(ml._library_dirs_ready)
    try:
        _night_block_date_checks()
    finally:
        ml._library_dirs_ready.clear()
        ml._library_dirs_ready.update(library_ready)


def _night_block_date_checks():
    import copy
    source = main_function_source("calculate_slotted_playback_state")
    check("_seed_date = block_start_date(now, current_block)" in source, "the scheduler takes its seed date from block_start_date")
    check("block_day_seed(_seed_date, target_clean_key, block_offset)" in source, "the scheduler seeds the block from that date")
    check(re.search(r"_missed_block_day_seeds\(\s*_prev_sched_block_key, current_block, target_clean_key, block_offset, _seed_date\)", source) is not None,
          "the catch-up is given the same date as the seed")
    check("block_day_seed(now," not in source, "no seed in the scheduler is made from the clock's date")

    evening = datetime.datetime(2026, 10, 13, 22, 30, 0)
    after_midnight = datetime.datetime(2026, 10, 14, 1, 0, 0)
    for name, block_mode, placement, sched, commercials in NIGHT_DATE_SCENARIOS:
        def fresh():
            return {"05": _sched_record_channel(block_mode, placement, sched, commercials)}

        at_twenty, _ = _night_date_run(fresh(), [evening])
        at_one, _ = _night_date_run(fresh(), [after_midnight])
        for key in NIGHT_STATE_KEYS:
            check(at_twenty[key] == at_one[key], "%s: a Night block built at 01:00 has the same %s as one built at 22:30" % (name, key))

        first = fresh()
        _night_date_run(first, [datetime.datetime(2026, 10, 10, 21, 0, 0)])
        before_state, before_seeds = _night_date_run(copy.deepcopy(first), [datetime.datetime(2026, 10, 13, 23, 0, 0)])
        after_state, after_seeds = _night_date_run(copy.deepcopy(first), [datetime.datetime(2026, 10, 14, 1, 0, 0)])
        expected = [[media.block_day_seed(datetime.date(2026, 10, day), "05", media.block_seed_offset("Night", 0)) for day in (11, 12)]]
        check(before_seeds == expected, "%s: starting at 23:00 catches up over the two missed Night blocks" % name)
        check(after_seeds == expected, "%s: starting at 01:00 catches up over the same two Night blocks, not three" % name)
        for key in NIGHT_STATE_KEYS:
            check(before_state[key] == after_state[key], "%s: after the same time off, 23:00 and 01:00 leave the same %s" % (name, key))


class TestNightBlockDate(_SectionCase):
    """A Night block keeps one date, one lineup and one catch-up from 20:00 to 04:00"""

    def test_night_block_date(self):
        self.run_section(suite_night_block_date)


# ============================================================================
# SECTION: Block key and block length
# ============================================================================


def suite_movie_part_duration():
    def part(name, dur, est=False):
        t = {"path": "/m/%s.mkv" % name, "duration": dur}
        if est:
            t["_duration_estimated"] = True
        return t

    def paths(tracks):
        return [t["path"] for t in tracks]

    thr = media.MOVIE_LENGTH_THRESHOLD_SECONDS
    half = thr // 2 + 600
    both = media.pair_movie_parts([part("Big Movie Part 1", half), part("Big Movie Part 2", half)])
    check(len(both) == 1 and both[0].get("is_pair") and both[0]["duration"] == 2 * half, "two parts with known durations make one pair of the summed length")
    check(both[0] == media._build_pair_track(part("Big Movie Part 1", half), part("Big Movie Part 2", half)), "the movie pair is the entry the shared pair builder makes")

    one_est = media.pair_movie_parts([part("Big Movie Part 1", half), part("Big Movie Part 2", 1800, est=True)])
    check(one_est == [], "a movie with one part of unknown length is not placed at all, neither part")
    both_est = media.pair_movie_parts([part("Big Movie Part 1", 1800, est=True), part("Big Movie Part 2", 1800, est=True)])
    check(both_est == [], "a movie with both parts of unknown length is not placed")
    no_dur = media.pair_movie_parts([{"path": "/m/Big Movie Part 1.mkv"}, part("Big Movie Part 2", half)])
    check(no_dur == [], "a part with no duration at all is not placed")
    zero = media.pair_movie_parts([part("Big Movie Part 1", 0), part("Big Movie Part 2", half)])
    check(zero == [], "a part with a zero duration is not placed")

    short = media.pair_movie_parts([part("Tiny Part 1", 600), part("Tiny Part 2", 600)])
    check(paths(short) == ["/m/Tiny Part 1.mkv", "/m/Tiny Part 2.mkv"], "known parts that stay under the movie length are placed as they were")

    lone_known = media.pair_movie_parts([part("Solo Part 1", half)])
    lone_unknown = media.pair_movie_parts([part("Solo Part 1", 1800, est=True)])
    check(paths(lone_known) == ["/m/Solo Part 1.mkv"], "a lone part with a known length is placed")
    check(lone_unknown == [], "a lone part with an unknown length is not placed")

    other = part("Plain Movie", 1800, est=True)
    check(media.pair_movie_parts([other]) == [other], "a file that is not a movie part is left to the rest of the rotation")

    media._split_movies_cache.pop("cache_test", None)
    est = [part("Big Movie Part 1", 1800, est=True), part("Big Movie Part 2", 1800, est=True)]
    first = media.split_movies_and_series(est, channel_id="cache_test")
    check(first[0] == [], "the splitter places nothing while both parts are unknown")
    known = [part("Big Movie Part 1", 1800), part("Big Movie Part 2", 1800)]
    second = media.split_movies_and_series(known, channel_id="cache_test", threshold_seconds=3000)
    check(len(second[0]) == 1 and second[0][0].get("is_pair"), "the same durations, now known, are placed (the cache does not hold the old answer)")
    third = media.split_movies_and_series(known, channel_id="cache_test", threshold_seconds=3000)
    check(third[0] == second[0], "a repeat read with nothing changed gives the same movie list")
    media._split_movies_cache.pop("cache_test", None)

    check(media.track_duration_is_known(part("X", 100)) and not media.track_duration_is_known(part("X", 100, est=True))
          and not media.track_duration_is_known({"path": "x"}) and not media.track_duration_is_known(None), "track_duration_is_known says known only for a positive length that is not an estimate")


def suite_block_key_length():
    d = datetime.datetime
    check(media.block_key_for(d(2026, 10, 14, 1, 0), "Night") == "2026_286_Night", "the Night block before 04:00 is keyed by the date it started")
    check(media.block_key_for(d(2026, 10, 14, 4, 0), "Night") == "2026_287_Night", "from 04:00 a Night block is keyed by today")
    check(media.block_key_for(d(2026, 10, 14, 1, 0), "Morning") == "2026_287_Morning", "every other block is keyed by today")
    check(media.block_key_for(d(2026, 10, 14, 9, 0), "Full Day") == "2026_287_Full Day", "the Full Day key")
    check(media.block_key_for(d(2026, 10, 14, 9, 0), "Marathon") == "2026_287_Marathon", "the Marathon key")
    check(media.block_key_for(d(2027, 1, 1, 2, 0), "Night") == "2026_365_Night", "a Night block after midnight on New Year's Day is keyed by the old year")
    check(media.block_key_for(d(2024, 1, 1, 2, 0), "Night") == "2023_365_Night", "the same across a leap year boundary")
    for name, start, end in media.SIX_HOUR_BLOCKS:
        check(media.block_length_seconds(name) == (end - start) * 3600 == 14400, "%s airs for 4 hours" % name)
        check(media.block_key_for(d(2026, 10, 14, 1, 0), name) == "2026_287_%s" % name, "%s is keyed by today" % name)
    for name in ("Morning", "Evening", "Night"):
        check(media.block_length_seconds(name) == 28800, "%s airs for 8 hours" % name)
    check(media.block_length_seconds("Full Day") == 86400 and media.block_length_seconds("Marathon") == 86400, "Full Day and Marathon air for 24 hours")
    check(media.block_length_seconds("Nothing") == 28800 and media.block_length_seconds("Nothing", 100) == 100, "an unknown name gets the default")
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    calls = lambda fn: sum(1 for n in ast.walk(MAIN_TREE) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == fn)
    check(calls("block_key_for") == 5, "the scheduler (five places) takes a block's key from the shared function; the read-only lineup in media.py does too")
    check(calls("block_length_seconds") == 7, "the scheduler (five places, two for the previous block) takes a block's length from the shared table; the read-only lineup in media.py does too")
    check("_block_len_lut" not in src and "_prev_block_secs = 14400" not in src and "_prev_block_secs = 28800" not in src, "the main script keeps no length table of its own")
    check("tm_yday}_Night" not in src and "tm_yday}_Full Day" not in src and "tm_yday}_Marathon" not in src and "tm_yday}_{current_block}" not in src, "no block key is built by hand")
    check("block_duration_seconds = 8 * 3600" not in src and "block_duration_seconds = 86400" not in src and "(block_end_hour - block_start_hour) * 3600" not in src,
          "the scheduler sets no block length by hand")


class TestBlockKeyLength(_SectionCase):
    """One function gives a block's saved key and one table gives its length"""

    def test_block_key_length(self):
        self.run_section(suite_block_key_length)


# ============================================================================
# SECTION: Read-only block lineup
# ============================================================================


def suite_block_lineup():
    import copy as _copy
    exists = lambda p: "gone" not in p
    series = {name: [{"path": "/m/%s/S01E%02d.mkv" % (name, i + 1), "duration": 1500} for i in range(6)] for name in ("A", "B", "C", "D", "E")}
    schedule = [dict(t) for name in series for t in series[name]]
    schedule[0]["gain"] = 1.25
    schedule[7]["gain"] = 0.8
    info = {"scheduling_mode": "random_slots", "pair_threshold_minutes": 0, "episode_order_mode": "sequential",
            "schedules": {"Evening": schedule, "Night": schedule, "Odd": schedule, "Empty": [{"path": "/m/gone/x.mkv"}]},
            "playback_log": {"show_pos": {"A": 2, "__FAIRNESS__": {"run": 3, "used": {"A": 2}}}, "show_order": {"Evening": ["C", "A", "B", "E", "D"]}}}
    seeds = []

    def start_seed(ch_info, offset):
        seeds.append(offset)
        return "1:%d" % offset

    def read(block, now, ch_info=None, peek=False, probe=None):
        seeds.clear()
        return media.read_block_lineup("5", ch_info or info, block, now, probe or {}, exists, start_seed, peek_only=peek)

    evening = datetime.datetime(2026, 10, 13, 14, 0)
    before = _copy.deepcopy(info)
    lineup, name = read("Empty", evening)
    check(lineup == [] and name is None, "a block with no existing file gives an empty lineup and no name")
    check(seeds == [] and info == before, "an empty block never asks for the start number and writes nothing")

    raw = media.build_raw_tracks(info["schedules"]["Evening"], {}, exists, keep_gain=True)
    expected = media.build_block_rotation(raw, info, _copy.deepcopy(info["playback_log"]["show_pos"]),
                                          media.block_day_seed(evening, "05", 200), 28800, "5", ["C", "A", "B", "E", "D"], "1:200")[0]
    lineup, name = read("Evening", evening, peek=True)
    check(name == "Evening" and lineup == expected and len(lineup) > 0, "with no saved lineup the block is built from the block's own date seed, offset, length, show order and start number")
    check(seeds == [200], "the start number is asked for once, for the block's offset")
    check(any("gain" in t for t in lineup) and not all("gain" in t for t in lineup), "each track keeps its schedule entry's gain and tracks with none get none")

    lineup, _ = read("Odd", evening, peek=True)
    odd_raw = media.build_raw_tracks(info["schedules"]["Odd"], {}, exists, keep_gain=True)
    odd_expected = media.build_block_rotation(odd_raw, info, _copy.deepcopy(info["playback_log"]["show_pos"]),
                                              media.block_day_seed(evening, "05", 400), 28800, "5", None, "1:400")[0]
    check(lineup == odd_expected and seeds == [400], "an unknown block name gets the offset 400 and the 8 hour length")

    target = expected[0]["path"]
    probed, _ = read("Evening", evening, peek=True, probe={os.path.normpath(target): {"duration": 1111}})
    check(any(t["path"] == target and t["duration"] == 1111 for t in probed), "a probed duration is used in place of the stored one")

    snapshot = _copy.deepcopy(info)
    read("Evening", evening, peek=True)
    check(info == snapshot, "peek_only leaves the channel's saved state, including the nested fairness counters, unchanged")
    read("Evening", evening)
    check(info == snapshot, "a read without peek_only also leaves the channel's saved state, including the nested fairness counters, unchanged")

    paths = [schedule[i]["path"] for i in (9, 3, 15)]
    saved_info = _copy.deepcopy(info)
    saved_info["playback_log"].update({
        "sched_block_key": media.block_key_for(evening, "Evening"),
        "sched_pairing_rule_version": media.SHORT_EPISODE_PAIRING_VERSION,
        "sched_cached_tracks": [paths[0], paths[1], paths[0], paths[2], "/m/gone/x.mkv"],
        "sched_cached_durations": {paths[1]: 1320},
    })
    lineup, name = read("Evening", evening, ch_info=saved_info)
    expected_saved = media.saved_block_lineup(paths, {paths[1]: 1320}, raw, info["schedules"]["Evening"], [], 0)
    check(name == "Evening" and lineup == expected_saved, "a saved lineup for the block on air is read back, with the saved durations")
    check([t["path"] for t in lineup] == paths, "a path saved twice is listed once")
    check(any(t["path"] == paths[0] and t.get("gain") == schedule[9].get("gain") for t in lineup), "a track read from a saved lineup keeps its schedule entry's gain")
    check(saved_info["playback_log"]["show_pos"] == info["playback_log"]["show_pos"], "reading a saved lineup writes nothing")

    stale = _copy.deepcopy(saved_info)
    stale["playback_log"]["sched_block_key"] = media.block_key_for(evening - datetime.timedelta(days=1), "Evening")
    lineup, _ = read("Evening", evening, ch_info=stale, peek=True)
    check(lineup == expected, "a saved lineup with another block's key is not used")
    old_rule = _copy.deepcopy(saved_info)
    old_rule["playback_log"]["sched_pairing_rule_version"] = media.SHORT_EPISODE_PAIRING_VERSION - 1
    lineup, _ = read("Evening", evening, ch_info=old_rule, peek=True)
    check(lineup == expected, "a saved lineup from an older pairing rule is not used")
    gone = _copy.deepcopy(saved_info)
    gone["playback_log"]["sched_cached_tracks"] = ["/m/gone/x.mkv"]
    lineup, _ = read("Evening", evening, ch_info=gone, peek=True)
    check(lineup == expected, "a saved lineup with nothing left falls through to a fresh build")

    night_early = datetime.datetime(2026, 10, 14, 1, 0)
    night_info = _copy.deepcopy(info)
    night_info["playback_log"]["sched_block_key"] = "2026_286_Night"
    night_info["playback_log"]["sched_pairing_rule_version"] = media.SHORT_EPISODE_PAIRING_VERSION
    night_info["playback_log"]["sched_cached_tracks"] = paths
    lineup, _ = read("Night", night_early, ch_info=night_info)
    check([t["path"] for t in lineup] == paths, "the Night block after midnight is found under the key of the date it started")
    night_fresh = _copy.deepcopy(info)
    night_fresh["episode_order_mode"] = "random"
    started = datetime.datetime(2026, 10, 13, 1, 0)
    night_expected = media.build_block_rotation(raw, night_fresh, _copy.deepcopy(info["playback_log"]["show_pos"]),
                                                media.block_day_seed(started, "05", 300), 28800, "5", None, "1:300")[0]
    night_today = media.build_block_rotation(raw, night_fresh, _copy.deepcopy(info["playback_log"]["show_pos"]),
                                             media.block_day_seed(night_early, "05", 300), 28800, "5", None, "1:300")[0]
    lineup, _ = read("Night", night_early, ch_info=night_fresh, peek=True)
    check(night_expected != night_today, "the two dates give different lineups, so the check below can tell them apart")
    check(lineup == night_expected, "a Night block built after midnight is seeded with the date it started")

    with open(_media_script_path(), encoding="utf-8") as handle:
        media_tree = ast.parse(handle.read())
    fn = next(n for n in media_tree.body if isinstance(n, ast.FunctionDef) and n.name == "read_block_lineup")
    names = {getattr(n.func, "id", "") for n in ast.walk(fn) if isinstance(n, ast.Call)}
    check({"build_raw_tracks", "block_seed_offset", "block_day_seed", "block_start_date", "block_key_for", "block_length_seconds",
           "saved_block_lineup", "build_block_rotation"} <= names, "the read-only lineup is built only from the shared functions")

    main_fn = next(n for n in MAIN_TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_get_block_ordered_content")
    main_calls = [getattr(n.func, "id", "") for n in ast.walk(main_fn) if isinstance(n, ast.Call)]
    check(main_calls.count("read_block_lineup") == 1, "the next-up builder in the main script calls the shared lineup function once")
    check(not ({"build_raw_tracks", "build_block_rotation", "saved_block_lineup", "block_day_seed", "block_seed_offset", "block_key_for",
                "block_length_seconds", "block_start_date", "deepcopy", "fromkeys"} & set(main_calls)),
          "the main script's next-up builder holds no lineup code of its own")

    calls_seen = []

    def fake_lineup(ch_key, ch_info, block_name, now, media_probe_cache, path_exists, start_order_seed, peek_only=False):
        calls_seen.append((ch_key, block_name, media_probe_cache, path_exists, start_order_seed, peek_only))
        return (([], None) if block_name == "Empty" else ([{"path": "/m/a.mkv", "duration": 1}], block_name))

    sentinel_exists, sentinel_seed = (lambda p: True), (lambda i, o: "x")
    probe_cache = {"/m/a.mkv": {"duration": 1}}
    wns = {"db": types.SimpleNamespace(config={"media_probe_cache": probe_cache}), "time": time, "log": MagicMock(),
           "_ORDERED_CONTENT_CACHE": {}, "_ORDERED_CONTENT_CACHE_TTL_MS": 8000, "_prune_ordered_content_cache": lambda ms: None,
           "_cached_path_exists": sentinel_exists, "_start_order_seed": sentinel_seed, "read_block_lineup": fake_lineup}
    exec(compile(get_function_source("_get_block_ordered_content"), "<extracted:_get_block_ordered_content>", "exec"), wns)
    wrapper = wns["_get_block_ordered_content"]
    one = {"schedules": {"Evening": [{"path": "/m/a.mkv"}], "Empty": []}}
    first = wrapper("05", one, "Evening", evening, peek_only=True)
    second = wrapper("05", one, "Evening", evening, peek_only=True)
    check(first == second == ([{"path": "/m/a.mkv", "duration": 1}], "Evening") and len(calls_seen) == 1, "the result is kept for a few seconds, so the second read does not build again")
    check(calls_seen[0][2] is probe_cache and calls_seen[0][3] is sentinel_exists and calls_seen[0][4] is sentinel_seed and calls_seen[0][5] is True,
          "the probe cache, the path check, the start number function and peek_only are handed through")
    empty_a = wrapper("05", one, "Empty", evening)
    empty_b = wrapper("05", one, "Empty", evening)
    check(empty_a == empty_b == ([], None) and len(calls_seen) == 2, "an empty block is kept for a few seconds too")
    one["schedules"]["Evening"].append({"path": "/m/b.mkv"})
    wrapper("05", one, "Evening", evening)
    check(len(calls_seen) == 3, "a change in the block's file count is read again at once")


class TestMoviePartDuration(_SectionCase):
    """A movie part is placed only when its duration is known"""

    def test_movie_part_duration(self):
        self.run_section(suite_movie_part_duration)


class TestBlockLineup(_SectionCase):
    """One read-only function gives a block's lineup for the next-up lists and the scan"""

    def test_block_lineup(self):
        self.run_section(suite_block_lineup)


# ============================================================================
# SECTION: Channel timeline
# ============================================================================


def suite_channel_timeline():
    check(media.commercial_interrupt_floor_seconds({"pair_threshold_minutes": 0}) == 900, "a pairing threshold of 0 gives the 15 minute floor")
    check(media.commercial_interrupt_floor_seconds({}) == 900, "no pairing threshold gives the 15 minute floor")
    check(media.commercial_interrupt_floor_seconds({"pair_threshold_minutes": 20}) == 1200, "the floor is the channel's pairing threshold in seconds")
    shows = [{"path": "/m/S/S01E%02d.mkv" % i, "duration": d} for i, d in enumerate((1320, 1500, 2640, 600, 1500, 7000), 1)]
    ads = [{"path": "/m/Ads/ad%d.mkv" % i, "duration": d} for i, d in enumerate((15, 30, 30, 45, 60, 90), 1)]
    cache = {os.path.normpath(t["path"]): {"duration": t["duration"]} for t in shows}
    for placement in ("interrupt_half_hour", "end_of_show"):
        for minutes in (0, 15, 25, 45):
            info = {"commercial_placement": placement, "pair_threshold_minutes": minutes}
            floor = (minutes if minutes > 0 else 15) * 60
            direct = media.build_block_timeline(shows, ads, placement, 4242, interrupt_floor_seconds=floor, media_probe_cache=cache, commercial_state=None)
            wrapped = media.build_channel_timeline(shows, ads, info, 4242, media_probe_cache=cache)
            check(wrapped == direct, "%s, threshold %s: the wrapper gives what build_block_timeline gives with the channel's placement and floor" % (placement, minutes))
            state = direct[2]
            again = media.build_block_timeline(shows, ads, placement, 4243, interrupt_floor_seconds=floor, commercial_state=state)
            check(media.build_channel_timeline(shows, ads, info, 4243, commercial_state=state) == again,
                  "%s, threshold %s: the saved commercial state and a missing probe cache pass straight through" % (placement, minutes))
    default = media.build_block_timeline(shows, ads, "interrupt_half_hour", 7, interrupt_floor_seconds=900)
    check(media.build_channel_timeline(shows, ads, {}, 7) == default, "a channel with no settings uses interrupt placement and the 15 minute floor")
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    check(src.count("build_channel_timeline(") == 5, "the scheduler builds all five timelines (two holiday, three main) through the wrapper")
    check("build_block_timeline(" not in src, "the main script no longer calls build_block_timeline itself")
    check("INTERRUPT_FLOOR_SECONDS" not in src and "h_commercial_placement" not in src, "the scheduler keeps no floor or placement of its own")
    calls = [n for n in ast.walk(MAIN_TREE) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "build_channel_timeline"]
    check(len(calls) == 5 and all("media_probe_cache" in [k.arg for k in n.keywords] for n in calls),
          "all five scheduler timelines are built with the probe cache")


class TestChannelTimeline(_SectionCase):
    """One wrapper builds a block's timeline from the channel's own settings"""

    def test_channel_timeline(self):
        self.run_section(suite_channel_timeline)


# ============================================================================
# SECTION: Channel Up/Down hesitation
# ============================================================================


def _surf_ns(active, current="05", game=True, guide=True, music_busy=False, mode="Video", visualizer=False, playing=True, transition="black"):
    clock = [10_000]
    calls = {"change": [], "music_stop": 0, "music_unload": 0, "vlc_mute": [], "vlc_stop": 0, "static_stop": 0,
             "transition_static_stop": 0, "game_quiet": 0, "ch1_stop": 0, "frame_clear": 0}
    chans = {str(i).zfill(2): {"active": str(i).zfill(2) in active} for i in range(5, 45)}
    if visualizer:
        chans[current.zfill(2)] = {"active": True, "is_visualizer": True}
    fake_db = types.SimpleNamespace(channels_db=chans, config={"game_channel_enabled": game, "tv_guide_enabled": guide, "transition_type": transition})
    busy = [music_busy]
    def _music_stop():
        calls["music_stop"] += 1
        busy[0] = False
    music = types.SimpleNamespace(get_busy=lambda: busy[0], stop=_music_stop,
                                  unload=lambda: calls.__setitem__("music_unload", calls["music_unload"] + 1))
    mixer = types.SimpleNamespace(music=music, Channel=lambda n: types.SimpleNamespace(
        stop=lambda: calls.__setitem__("ch1_stop", calls["ch1_stop"] + 1)))
    fake_pygame = types.SimpleNamespace(time=types.SimpleNamespace(get_ticks=lambda: clock[0]), mixer=mixer)
    media_obj = types.SimpleNamespace(get_mrl=lambda: "file:///m/show%20one.mkv")
    player = types.SimpleNamespace(
        get_state=lambda: "playing", get_time=lambda: 61_000, get_length=lambda: 1_500_000, get_media=lambda: media_obj,
        audio_set_mute=lambda v: calls["vlc_mute"].append(v))
    engine = types.SimpleNamespace(player=player, stop=lambda block=True: calls.__setitem__("vlc_stop", calls["vlc_stop"] + 1))
    fake_vlc = types.SimpleNamespace(State=types.SimpleNamespace(Ended="ended", Error="error"))
    fake_clock = types.SimpleNamespace(seconds_since_midnight=lambda: 5000.0,
                                       now=lambda: types.SimpleNamespace(timestamp=lambda: 1_700_000_000.0))
    state = {"current_channel": current, "osd_channel_pending": False, "surf_target": "", "surf_commit_at": 0,
             "surf_origin": "", "surf_leave_position": None, "is_playing_video": playing,
             "channel_switch_black_until": 777, "music_owner": "live_visualizer", "guide_preview_music_track": "x",
             "osd_channel_timer": 0, "digit_buffer": "", "digit_timer": 0}
    ns = {"db": fake_db, "pygame": fake_pygame, "app_state": state, "_CHANNEL_SURF_HESITATE_MS": 400,
          "vlc": fake_vlc, "vlc_engine": engine, "static_audio": types.SimpleNamespace(
              stop=lambda: calls.__setitem__("static_stop", calls["static_stop"] + 1)),
          "broadcast_clock": fake_clock, "os": os, "log": MagicMock(),
          "menu_nav": mn,
          "_stop_transition_static": lambda: calls.__setitem__("transition_static_stop", calls["transition_static_stop"] + 1),
          "_quiet_game_channel": lambda: calls.__setitem__("game_quiet", calls["game_quiet"] + 1),
          "_clear_last_video_frame": lambda **k: calls.__setitem__("frame_clear", calls["frame_clear"] + 1),
          "calculate_slotted_playback_state": lambda ch: {"mode": mode, "file": "/m/song.mp3"},
          "change_channel": lambda ch, **k: (calls["change"].append((ch, k)),
                                             state.update(surf_origin="", surf_leave_position=None))}
    for name in ("_surf_allowed_channels", "_surf_origin_active", "_surf_cancel", "_surf_capture_leave_position",
                 "_surf_begin_quiet", "_surf_step", "_surf_digit_typed", "_typed_channel_valid", "_tune_after_flip", "_surf_commit_if_due",
                 "_surf_recover_if_stuck", "_bezel_led_channel_text", "_configured_transition_style"):
        exec(compile(get_function_source(name), "<extracted:%s>" % name, "exec"), ns)
    return ns, state, clock, calls


def _tuned(calls):
    return [c[0] for c in calls["change"]]


def suite_channel_surf():
    ns, st, clock, calls = _surf_ns(["05", "06", "07"])
    check(ns["_surf_allowed_channels"]() == ["03", "04", "05", "06", "07"], "Up and Down step through game, guide, then active channels")

    ns["_surf_step"](1)
    check(st["surf_origin"] == "05", "the first press marks the channel on air as shut down")
    check(calls["vlc_mute"] == [True] and calls["vlc_stop"] == 1, "the first press mutes VLC and starts stopping it")
    check(calls["static_stop"] == 1 and calls["transition_static_stop"] == 1 and st["channel_switch_black_until"] == 0,
          "the first press silences any static and ends a running transition")
    check(calls["frame_clear"] == 1 and st["music_owner"] == "", "the first press hides the last picture and drops the music owner")
    check(st["surf_leave_position"] == {"file": os.path.normpath("m/show one.mkv"),
                                         "pos_ms": 61_000, "dur_ms": 1_500_000, "wall_start": 5000.0, "written_at": 1_700_000_000.0},
          "the first press keeps the file, position, length and wall time of the show (got %s)" % st["surf_leave_position"])
    clock[0] += 50
    ns["_surf_step"](1)
    clock[0] += 50
    last_press = clock[0] - 50
    check(calls["vlc_stop"] == 1 and calls["vlc_mute"] == [True] and calls["frame_clear"] == 1, "only the first press shuts the show down")
    check(calls["change"] == [], "flipping fast loads nothing")
    check(st["osd_channel_pending"] and st["surf_target"] == "07", "two quick Up presses land two channels up (got %s)" % st["surf_target"])
    check(st["current_channel"] == "05", "the channel on air does not change while flipping")
    check(ns["_bezel_led_channel_text"]() == "07", "the letters show the channel being flipped to")
    ns["_surf_commit_if_due"](last_press + 399)
    check(calls["change"] == [], "still waiting before the hesitation time has passed")
    ns["_surf_commit_if_due"](last_press + 400)
    check(_tuned(calls) == ["07"], "one channel change to the flipped-to channel once you hesitate (got %s)" % _tuned(calls))
    kw = calls["change"][0][1]
    check(kw["force_reload"] is False and kw["pre_captured_position"] is not None and kw["pre_captured_position"]["pos_ms"] == 61_000,
          "the tune hands the old show's saved position to change_channel")
    check(not st["osd_channel_pending"] and st["surf_target"] == "" and st["surf_origin"] == "", "the flip state is cleared after tuning")
    ns["_surf_commit_if_due"](last_press + 5000)
    check(_tuned(calls) == ["07"], "it does not tune twice")

    ns2, st2, clock2, calls2 = _surf_ns(["05", "06"], current="03")
    ns2["_surf_step"](-1)
    check(st2["surf_target"] == "06", "Down from the first channel wraps to the last (got %s)" % st2["surf_target"])
    check(calls2["game_quiet"] == 1 and st2["surf_leave_position"] is None, "flipping from the game channel pauses, hides and mutes the game; it has no show position")

    ns3, st3, clock3, calls3 = _surf_ns(["05", "06"], current="05")
    ns3["_surf_step"](1)
    ns3["_surf_step"](-1)
    check(calls3["game_quiet"] == 0, "the game channel is left alone when it is not the one on air")
    ns3["_surf_commit_if_due"](clock3[0] + 1000)
    check(_tuned(calls3) == ["05"] and calls3["change"][0][1]["force_reload"] is True and calls3["change"][0][1]["pre_captured_position"] is not None,
          "flipping away and back brings the shut-down show back (reload, with its saved position)")
    check(not st3["osd_channel_pending"] and st3["surf_origin"] == "", "the flip state is cleared after the show returns")

    ns4, st4, clock4, calls4 = _surf_ns(["05", "06"], current="05", mode="Visualizer", music_busy=True, visualizer=True)
    ns4["_surf_step"](1)
    check(calls4["music_stop"] == 1 and calls4["ch1_stop"] == 1, "the first press stops a visualizer channel's music")
    ns4["_surf_commit_if_due"](clock4[0] + 1000)
    check(_tuned(calls4) == ["06"] and st4["active_music_track_file"] == "/m/song.mp3",
          "a visualizer target names its song before tuning")

    ns5, st5, clock5, calls5 = _surf_ns(["05", "06"], current="05")
    st5["digit_buffer"] = "12"
    check(ns5["_bezel_led_channel_text"]() == "12", "typed digits show over the current channel")
    ns5["_surf_step"](1)
    check(st5["digit_buffer"] == "" and ns5["_bezel_led_channel_text"]() == "06", "a flip replaces typed digits")
    ns5["_surf_cancel"]()
    check(ns5["_bezel_led_channel_text"]() == "05", "cancelling shows the current channel again")
    check(st5["surf_origin"] == "05", "cancelling the flip does not bring the shut-down show back by itself")

    ns6, st6, clock6, calls6 = _surf_ns(["05", "06"], current="05")
    ns6["_surf_step"](1)
    ns6["_surf_cancel"]()
    st6["digit_buffer"] = "9"
    ns6["_surf_recover_if_stuck"]()
    check(calls6["change"] == [], "while digits are being typed the shut-down show waits")
    st6["digit_buffer"] = ""
    ns6["_surf_recover_if_stuck"]()
    check(_tuned(calls6) == ["05"] and calls6["change"][0][1]["force_reload"] is True, "digits that come to nothing bring the shut-down show back")
    ns6["_surf_recover_if_stuck"]()
    check(_tuned(calls6) == ["05"], "the safety net fires once")

    ns7, st7, clock7, calls7 = _surf_ns(["05", "06"], current="05")
    ns7["_surf_step"](1)
    ns7["_surf_cancel"]()
    ns7["_tune_after_flip"]("06", is_surfing=True)
    check(calls7["change"][0][0] == "06" and calls7["change"][0][1]["is_surfing"] is True and calls7["change"][0][1]["pre_captured_position"] is not None,
          "typed digits that resolve after a flip tune with the saved position")

    ns8, st8, clock8, calls8 = _surf_ns(["05", "06"], current="05", playing=False)
    ns8["_surf_step"](1)
    check(st8["surf_leave_position"] is None, "a channel with no show playing has no position to keep")

    src = MAIN_SRC
    up = src[src.index("elif event.key == pygame.K_UP:"):src.index("elif event.key == pygame.K_DOWN:")]
    down = src[src.index("elif event.key == pygame.K_DOWN:"):src.index("game_ch_on = db.config.get(\"game_channel_enabled\"")]
    check("_surf_step(1)" in up and "change_channel(" not in up and "calculate_slotted_playback_state" not in up, "the Up key only steps the flip; it does not tune or work out the schedule")
    check("_surf_step(-1)" in down and "change_channel(" not in down and "calculate_slotted_playback_state" not in down, "the Down key only steps the flip; it does not tune or work out the schedule")
    digit_helper = get_function_source("_surf_digit_typed")
    check(digit_helper.index("_surf_cancel()") < digit_helper.index("menu_nav.digit_entry_step("), "typing a digit drops the flipped-to number")
    check("    _surf_commit_if_due(now_ticks)\n    _surf_recover_if_stuck()\n" in src, "the main loop checks every frame whether the flip is due and whether the shut-down show must come back")
    cc = get_function_source("change_channel")
    check(cc.count('app_state["surf_target"] = ""') == 1 and cc.index('app_state["osd_channel_pending"] = False') < cc.index('app_state["surf_target"] = ""')
          and 'app_state["surf_origin"] = ""' in cc, "any real tune clears a flip in progress")
    check('app_state.get("surf_origin") != "03"' in cc and "_quiet_game_channel()" in cc, "leaving the game channel after a flip does not pause it a second time")
    check("pre_captured_position.get(\"wall_start\"" in cc and "pre_captured_position.get(\"written_at\"" in cc, "a flip's saved leave time is the one written into the anchor")

    loop = src[src.index("while True:"):]
    check("_surf_quiet = _surf_origin_active()" in loop, "the loop reads once per frame whether a show is shut down")
    stall = loop[loop.index("# SECTION: Video end & stall detection"):loop.index("# SECTION: Video render (TV channels)")]
    check(stall.count("not _surf_quiet") == 3, "end detection, the background watchdog and the restart watchdog all stand down while flipping (%d)" % stall.count("not _surf_quiet"))
    check('if _surf_quiet and ch not in ("03", "04"):\n        if _configured_transition_style() == "static":\n            _render_transition_static(screen, _full_window_rect)\n        else:\n            screen.fill(_TRANSITION_GRAY, _full_window_rect)\n    elif ch != "04":' in loop,
          "the picture draw shows static for a shut-down show in Static style and grey otherwise")
    check('if _surf_quiet and ch not in ("03", "04"):\n        pass\n    elif ch == "03":' in loop, "the playback and stall-recovery section does nothing for a shut-down show")
    ov = loop[loop.index("# SECTION: Overlays: captions"):loop.index('_prof_mark("overlays")')]
    check("    if not _surf_quiet:\n        _draw_caption_overlay(" in ov and ov.count("_apply_static_noise_effect(") >= 1 and ov.index("if not _surf_quiet:") < ov.index("_apply_static_noise_effect("),
          "captions, static noise, reception lines, the watermark and the static transition are not drawn while flipping")
    check(ov.index("if not _surf_quiet:") < ov.index("_render_transition_static("), "the static transition is inside the flipping gate")
    grey = loop[loop.index("# SECTION: Final compositing & present"):]
    check(grey.index("if (_surf_quiet") < grey.index("screen.fill(_TRANSITION_GRAY)") < grey.index('_draw_channel_volume_osd("picture")'), "the grey cover is drawn for a shut-down show, before the channel letters")
    cover = grey[grey.index("if (_surf_quiet"):grey.index('_draw_channel_volume_osd("picture")')]
    check('if _configured_transition_style() != "static":\n            screen.fill(_TRANSITION_GRAY)' in cover,
          "the grey cover is drawn only when the transition style is not Static")
    check('elif ch in ("03", "04"):\n            _render_transition_static(screen, _full_window_rect)' in cover,
          "in Static style the game channel and guide are covered with static, not grey")
    _mid = cc[cc.index("_mid_static_burst = ("):cc.index("if (not _mid_static_burst")]
    check('== "static"' in _mid and "_surf_origin_active()" in _mid,
          "a flip in progress counts as a running static burst, so the static sound is not cut at the tune")
    check("if _surf_origin_active():\n                _tune_after_flip(final_ch, is_surfing=True)" in src, "typed digits tune through the flip so the old show's position is kept")
    gov = __import__("performance_governor")
    check(gov.CHANNEL_SURF_HESITATE_MS == 400 and "if performance_governor is not None else 400" in src.split("_CHANNEL_SURF_HESITATE_MS =", 1)[1].split("\n", 1)[0], "the hesitation time lives in the governor table and its fallback matches")

    nss, sts, clocks, callss = _surf_ns(["05", "06", "07"], transition="static")
    sts["channel_switch_static_until"] = 99_999
    nss["_surf_step"](1)
    check(callss["static_stop"] == 0 and callss["transition_static_stop"] == 0,
          "in Static style the first press leaves the running static sound alone")
    check(sts["channel_switch_black_until"] == 0 and sts["channel_switch_static_until"] == 0 and sts["surf_origin"] == "05",
          "in Static style the first press clears both transition timers and shuts the show down")
    check(callss["vlc_mute"] == [True] and callss["vlc_stop"] == 1 and callss["frame_clear"] == 1,
          "in Static style the first press still mutes, stops and hides the show")
    _mm_line = 'if app_state.get("current_channel") == "03" and not app_state.get("is_loading", False) and game_deck is not None and not _surf_quiet:\n        _mm_mode ='
    check(_mm_line in loop, "the game channel's menu music is not restarted while a flip has the channel shut down")
    _stop_src = get_function_source("_stop_transition_static")
    check("_static_transition_running()" in _stop_src and _stop_src.index("_static_transition_running()") < _stop_src.index("static_audio.stop()"),
          "stopping the transition static checks first that no Static-style burst or flip is running")

    def _static_ns(style, static_until, origin, now=10_000):
        _stops = []
        _ns = {"db": types.SimpleNamespace(config={"transition_type": style}),
               "pygame": types.SimpleNamespace(time=types.SimpleNamespace(get_ticks=lambda: now)),
               "app_state": {"channel_switch_static_until": static_until, "surf_origin": origin},
               "static_audio": types.SimpleNamespace(stop=lambda: _stops.append(1))}
        for _name in ("_configured_transition_style", "_surf_origin_active", "_static_transition_running", "_stop_transition_static"):
            exec(compile(get_function_source(_name), "<extracted:%s>" % _name, "exec"), _ns)
        return _ns, _stops
    _n1, _s1 = _static_ns("static", 11_000, "")
    _n1["_stop_transition_static"]()
    check(_n1["_static_transition_running"]() and _s1 == [], "a running Static burst keeps the static sound playing")
    _n2, _s2 = _static_ns("static", 0, "03")
    _n2["_stop_transition_static"]()
    check(_n2["_static_transition_running"]() and _s2 == [], "a Static-style flip keeps the static sound playing")
    _n3, _s3 = _static_ns("static", 9_000, "")
    _n3["_stop_transition_static"]()
    check(not _n3["_static_transition_running"]() and _s3 == [1], "the static sound is stopped once the burst is over and no flip is waiting")
    _n4, _s4 = _static_ns("black", 11_000, "03")
    _n4["_stop_transition_static"]()
    check(not _n4["_static_transition_running"]() and _s4 == [1], "in Black style the static sound is always stopped")

    nsb, stb, clockb, callsb = _surf_ns(["05", "06", "07"], transition="black")
    nsb["_surf_step"](1)
    check(callsb["static_stop"] == 1 and callsb["transition_static_stop"] == 1,
          "in Black style the first press still silences any static")


def suite_channel_digits():
    ns, st, clock, calls = _surf_ns(["05", "06", "12"])
    now = clock[0]
    ns["_surf_digit_typed"]("1", now)
    check(st["digit_buffer"] == "1" and st["digit_timer"] == now + 2500, "the first digit waits the single-digit time for a second digit")
    check(st["surf_origin"] == "" and calls["vlc_stop"] == 0 and calls["vlc_mute"] == [] and calls["frame_clear"] == 0,
          "the first digit leaves the show on air playing")
    check(ns["_bezel_led_channel_text"]() == "1", "the letters show the digit typed so far")

    ns["_surf_digit_typed"]("2", now + 300)
    check(st["digit_buffer"] == "12" and st["digit_timer"] == now + 300 + 350, "the second digit sets the tune time to the confirm time after it")
    check(st["surf_origin"] == "05", "a second digit that makes a channel that exists marks the show on air as shut down")
    check(calls["vlc_mute"] == [True] and calls["vlc_stop"] == 1, "the second digit mutes VLC and starts stopping it")
    check(calls["static_stop"] == 1 and calls["transition_static_stop"] == 1 and st["channel_switch_black_until"] == 0,
          "the second digit silences any static and ends a running transition")
    check(calls["frame_clear"] == 1 and st["music_owner"] == "", "the second digit hides the last picture and drops the music owner")
    check(st["surf_leave_position"] is not None and st["surf_leave_position"]["pos_ms"] == 61_000, "the second digit keeps the show's position for the tune")
    check(ns["_bezel_led_channel_text"]() == "12", "the letters show the full number while the show is down")
    check(calls["change"] == [], "the second digit tunes nothing itself")

    ns["_surf_digit_typed"]("3", now + 500)
    check(st["digit_buffer"] == "12" and calls["vlc_stop"] == 1 and calls["frame_clear"] == 1, "a third digit changes nothing and shuts the show down only once")
    check(not mn.digit_entry_due(st["digit_buffer"], st["digit_timer"], now + 500 + 349), "the number is not due before the confirm time has passed")
    check(mn.digit_entry_due(st["digit_buffer"], st["digit_timer"], now + 500 + 350), "the number is due when the confirm time has passed")
    ns["_surf_recover_if_stuck"]()
    check(calls["change"] == [], "while the typed number waits, the shut-down show is not brought back")

    ns1, st1, clock1, calls1 = _surf_ns(["05", "06", "12"])
    for first, second in (("0", "0"), ("0", "1"), ("0", "2"), ("1", "3"), ("0", "7"), ("4", "4")):
        ns1["_surf_digit_typed"](first, clock1[0])
        ns1["_surf_digit_typed"](second, clock1[0] + 50)
        label = first + second
        check(st1["digit_buffer"] == "" and st1["digit_timer"] == 0, "%s: a full number that cannot be tuned is dropped at once" % label)
        check(st1["surf_origin"] == "" and calls1["vlc_stop"] == 0 and calls1["vlc_mute"] == [] and calls1["frame_clear"] == 0 and calls1["change"] == [],
              "%s: the show is never stopped, muted, hidden or reloaded" % label)
    check(ns1["_bezel_led_channel_text"]() == "05", "after a number that cannot be tuned the letters show the channel on air")

    ns1b, st1b, clock1b, calls1b = _surf_ns(["05", "06"])
    ns1b["_surf_step"](1)
    ns1b["_surf_cancel"]()
    ns1b["_surf_digit_typed"]("0", clock1b[0] + 10)
    ns1b["_surf_digit_typed"]("1", clock1b[0] + 20)
    check(st1b["digit_buffer"] == "", "after a flip, a number that cannot be tuned is still dropped")
    ns1b["_surf_recover_if_stuck"]()
    check(_tuned(calls1b) == ["05"] and calls1b["change"][0][1]["force_reload"] is True and calls1b["change"][0][1]["pre_captured_position"] is not None,
          "after a flip, a number that cannot be tuned brings the shut-down show back with its saved position")
    check(st1b["surf_origin"] == "", "the shut-down state is cleared once the show is back")

    ns1c, st1c, clock1c, calls1c = _surf_ns(["05", "06"])
    ns1c["_surf_digit_typed"]("6", clock1c[0])
    check(st1c["digit_buffer"] == "6" and st1c["surf_origin"] == "" and calls1c["vlc_stop"] == 0, "a single digit alone leaves the show playing while it waits")
    ns1c["_surf_recover_if_stuck"]()
    check(calls1c["change"] == [], "a single digit waiting does nothing to the show")

    for digit in "012":
        check(not ns["_typed_channel_valid"](digit.zfill(2)), "single digit %s makes channel 0%s, which does not exist" % (digit, digit))
    nsv, stv, clockv, callsv = _surf_ns(["05", "06", "09"])
    check([ns_ch for ns_ch in ("03", "04", "05", "06", "07", "08", "09") if nsv["_typed_channel_valid"](ns_ch)] == ["03", "04", "05", "06", "09"],
          "a single digit tunes the single-digit channels that are on: the game channel, the guide and the active channels")
    nsg, stg, clockg, callsg = _surf_ns(["05"], game=False, guide=False)
    check(not nsg["_typed_channel_valid"]("03") and not nsg["_typed_channel_valid"]("04") and nsg["_typed_channel_valid"]("05"),
          "the game channel and the guide are not tunable while turned off in the menu")
    check(nsv["_typed_channel_valid"]("05") and not nsv["_typed_channel_valid"]("45") and not nsv["_typed_channel_valid"]("99"),
          "numbers above 44 cannot be tuned")

    ns2, st2, clock2, calls2 = _surf_ns(["05", "06", "12"])
    ns2["_surf_digit_typed"]("1", clock2[0])
    ns2["_surf_digit_typed"]("2", clock2[0] + 100)
    ns2["_tune_after_flip"]("12", is_surfing=True)
    check(_tuned(calls2) == ["12"] and calls2["change"][0][1]["force_reload"] is False and calls2["change"][0][1]["pre_captured_position"] is not None,
          "a number that exists tunes through the shut-down path and keeps the old show's position")

    ns3, st3, clock3, calls3 = _surf_ns(["05", "06", "12"])
    ns3["_surf_digit_typed"]("0", clock3[0])
    ns3["_surf_digit_typed"]("5", clock3[0] + 100)
    ns3["_tune_after_flip"]("05", is_surfing=True)
    check(_tuned(calls3) == ["05"] and calls3["change"][0][1]["force_reload"] is True, "typing the number of the channel on air reloads it, because the show was shut down")

    ns4, st4, clock4, calls4 = _surf_ns(["05", "06"], current="03")
    ns4["_surf_digit_typed"]("0", clock4[0])
    check(calls4["game_quiet"] == 0, "the game channel keeps running after only a first digit")
    ns4["_surf_digit_typed"]("6", clock4[0] + 100)
    check(calls4["game_quiet"] == 1 and st4["surf_origin"] == "03", "the second digit pauses, hides and mutes the game channel")

    ns5, st5, clock5, calls5 = _surf_ns(["05", "06"])
    ns5["_surf_step"](1)
    ns5["_surf_digit_typed"]("0", clock5[0] + 10)
    ns5["_surf_digit_typed"]("6", clock5[0] + 20)
    check(calls5["vlc_stop"] == 1 and calls5["frame_clear"] == 1 and st5["surf_origin"] == "05",
          "digits typed after a flip do not shut the show down a second time")
    check(not st5["osd_channel_pending"] and st5["surf_target"] == "", "typing digits drops the flipped-to number")

    src = MAIN_SRC
    handler = src[src.index("pressed_digit = menu_nav.key_to_digit(event.key)"):]
    handler = handler[:handler.index("if event.key == pygame.K_n:")]
    check("_surf_digit_typed(pressed_digit, now_ticks)" in handler and "digit_entry_step(" not in handler,
          "the digit key handler only calls the shared digit helper")
    resolve = src[src.index("if menu_nav.digit_entry_due("):src.index("_surf_commit_if_due(now_ticks)\n    _surf_recover_if_stuck()")]
    check("if _typed_channel_valid(final_ch):" in resolve and "valid_set" not in resolve and "range(5, 45)" not in resolve,
          "the main loop uses the shared check for which numbers can be tuned")
    check("if _surf_origin_active():\n                _tune_after_flip(final_ch, is_surfing=True)" in resolve,
          "a number that exists tunes through the shut-down path in the main loop")
    gov = __import__("performance_governor")
    with open(os.path.abspath(mn.__file__), encoding="utf-8") as nav_file:
        nav_src = nav_file.read()
    fallback = nav_src.split("DIGIT_ENTRY_CONFIRM_MS = int(", 1)[1].split("except Exception:", 1)[1].split("\n")[1]
    check(gov.CHANNEL_DIGIT_TUNE_DELAY_MS == 350 and mn.DIGIT_ENTRY_CONFIRM_MS == gov.CHANNEL_DIGIT_TUNE_DELAY_MS,
          "the delay after the second digit lives in the governor table and menu_nav reads it")
    check("DIGIT_ENTRY_CONFIRM_MS = 350" in fallback, "the menu_nav fallback matches the governor value")


class TestChannelDigits(_SectionCase):
    """A full typed channel number that can be tuned hides, mutes and shuts the show down; one that cannot be tuned leaves the show alone"""

    def test_channel_digits(self):
        self.run_section(suite_channel_digits)


class TestChannelSurf(_SectionCase):
    """Up and Down only flip the number on a grey screen; the channel loads once you hesitate"""

    def test_channel_surf(self):
        self.run_section(suite_channel_surf)



# ============================================================================
# SECTION: Output logging and entry point
# ============================================================================


class _Tee:
    """Writes to the real stream and to the output file, so the console shows exactly what it always did."""

    def __init__(self, stream, file):
        self._stream = stream
        self._file = file

    def write(self, text):
        self._stream.write(text)
        try:
            self._file.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        self._stream.flush()
        try:
            self._file.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def run_logged(script_path, run):
    """Runs `run()` with everything it prints also saved to root/temp/<script>_output.txt; the previous run's file is kept as <script>_output_previous.txt."""
    name = os.path.splitext(os.path.basename(script_path))[0]
    os.makedirs(TEMP_DIR, exist_ok=True)
    out_path = os.path.join(TEMP_DIR, name + "_output.txt")
    try:
        os.replace(out_path, os.path.join(TEMP_DIR, name + "_output_previous.txt"))
    except FileNotFoundError:
        pass
    old_out, old_err = sys.stdout, sys.stderr
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("=== %s run %s ===\n" % (name, datetime.datetime.now().isoformat(timespec="seconds")))
        sys.stdout, sys.stderr = _Tee(old_out, f), _Tee(old_err, f)
        try:
            run()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            f.flush()
            print("[test output saved to %s]" % out_path)


def tearDownModule():
    print("\n%d checks ran inside the menu / media sections, %d failed" % (_CHECKS[0], len(_FAILED)))


# ============================================================================
# SECTION: Visualizer guide row follows the station's admission rules
# ============================================================================

def suite_visualizer_guide_admission():
    import inspect
    A, B, C, D, E = ("/m/a.mp3", "/m/b.mp3", "/m/c.mp3", "/m/d.flac", "/m/e.mp3")
    stack = [{"path": p} for p in (A, B, C, D, E)]
    probe = {os.path.normpath(A): {"duration": 200}, os.path.normpath(B): {"duration": 0},
             os.path.normpath(C): {"duration": 180, "confirmed_unplayable": True},
             os.path.normpath(D): {"duration": 150}, os.path.normpath(E): {"duration": 120, "confirmed_no_audio": True}}
    got = [p for p, _a in media.admit_visualizer_assets(stack, probe, {}, set())]
    check(got == [A, D], "only measured, playable songs with audio are admitted")
    got = [p for p, _a in media.admit_visualizer_assets(stack, probe, {os.path.normpath(A): True}, set())]
    check(got == [D], "a song the mixer cannot seek is not admitted")
    got = [p for p, _a in media.admit_visualizer_assets(stack, probe, {}, {".flac"})]
    check(got == [A], "a song in an unseekable format is not admitted")
    got = [p for p, _a in media.admit_visualizer_assets([{"path": B, "duration": 90}], {}, {}, set())]
    check(got == [B], "a stored duration admits a song the probe cache has not measured")
    got = [p for p, _a in media.admit_visualizer_assets([A, D], probe, {}, set())]
    check(got == [A, D], "plain path strings are read the same as asset dictionaries")
    check(media.admit_visualizer_assets([], probe, {}, set()) == [] and media.admit_visualizer_assets(None, probe, {}, set()) == [],
          "an empty channel admits nothing")

    for label, cache, unseek, exts in (
        ("measured and playable", probe, {}, set()),
        ("one unseekable", probe, {os.path.normpath(A): True}, set()),
        ("an unseekable format", probe, {}, {".flac"}),
        ("nothing measured", {}, {}, set()),
        ("everything unplayable", {os.path.normpath(p): {"duration": 100, "confirmed_unplayable": True} for p in (A, B, C, D, E)}, {}, set()),
    ):
        channels = {"08": {"name": "Music", "is_visualizer": True, "schedules": {"Music Tracks": [{"path": p, "duration": 0} for p in (A, B, C, D, E)]},
                           "playback_log": {}}}
        clock, ticks = [datetime.datetime(2026, 10, 15, 12, 0, 0)], [0]
        ns = _sched_record_ns(channels, clock, ticks)
        ns["db"].config["media_probe_cache"] = cache
        ns["_visualizer_unseekable_paths"] = lambda u=unseek: u
        ns["_mixer_seekable_formats"] = lambda e=exts: {x: False for x in e}
        track_list, total, _origin = ns["_visualizer_frozen_lineup"]("08", channels["08"], clock[0])
        station = sorted(t["path"] for t in track_list)
        guide = sorted(p for p, _a in media.admit_visualizer_assets(channels["08"]["schedules"]["Music Tracks"], cache, unseek, exts))
        check(station == guide, "%s: the guide admits exactly the songs the station airs" % label)
        check(bool(guide) == bool(track_list) and (total > 0) == bool(track_list), "%s: the guide shows music exactly when the station airs music" % label)

    ch_info = {"is_visualizer": True, "schedules": {"Music Tracks": [{"path": A}, {"path": D}]}}
    fake_db = types.SimpleNamespace(config={"media_probe_cache": probe})
    state = {"visualizer_admission_inputs": lambda: ({os.path.normpath(A): True}, {".flac"})}
    check(ui._guide_visualizer_admitted(ch_info, state, fake_db) == [], "the guide row is empty when the station publishes every song as unseekable")
    state = {"visualizer_admission_inputs": lambda: ({}, set())}
    check(len(ui._guide_visualizer_admitted(ch_info, state, fake_db)) == 2, "the guide row has music when the station admits the songs")
    check(len(ui._guide_visualizer_admitted(ch_info, {}, fake_db)) == 2, "a missing station publication applies no unseekable filter")
    check(ui._guide_visualizer_admitted(ch_info, state, types.SimpleNamespace(config={})) == [],
          "songs with no measured length leave the guide row empty")

    ui_src = inspect.getsource(ui)
    check("_row_viz_admitted" in ui_src, "the guide row cache key carries whether the Visualizer admits music")
    check("sched_cached_tracks" not in ui_src and "build_scheduled_block_content" not in ui_src,
          "the unreachable lineup builder is gone from the guide")
    app_src = MAIN_SRC
    check('app_state["visualizer_admission_inputs"] = _visualizer_admission_inputs' in app_src, "the station publishes the admission inputs to the guide")


class TestVisualizerGuideAdmission(_SectionCase):
    """The guide's Visualizer row shows music only when the station's lineup admits a song"""

    def test_visualizer_guide_admission(self):
        self.run_section(suite_visualizer_guide_admission)


# ============================================================================
# SECTION: Duration scan runs ahead of the scan chain
# ============================================================================

def suite_duration_scan_ahead():
    class _Stop:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.waits.append(seconds)
            return seconds == 20.0

    probed, failed, order_log = [], [], []
    cache = {"/m/known.mkv": {"duration": 1200}}
    items = [
        (0, 0, "channel", "05", "Full Day", "/m/known.mkv"),
        (0, 0, "channel", "05", "Full Day", "/m/now.mkv"),
        (2, 1, "channel", "05", "Full Day", "/m/next.mkv"),
        (3, 4, "channel", "06", "Full Day", "/m/bad.mkv"),
        (3, 4, "channel", "06", "Full Day", "/m/deep.mkv"),
    ]
    stop = _Stop()

    def fake_store(kind, ch, block, p):
        probed.append(p)
        if p == "/m/bad.mkv":
            return 0
        cache[p] = {"duration": 900}
        return 900

    ns = {
        "db": types.SimpleNamespace(save_settings_async=lambda **k: order_log.append("saved")),
        "log": MagicMock(), "time": time, "os": os,
        "_app_shutting_down": stop,
        "_background_scan_should_pause": lambda: False,
        "_order_scan_pipeline": lambda tiers, deep: items,
        "_gather_probe_priority_queue": lambda: [[], [], [], []],
        "_gather_deep_backlog_queue": lambda: [],
        "_get_cached_media_probe": lambda p: cache.get(p, {}),
        "_cached_path_exists": lambda p: True,
        "_is_drive_degraded": lambda p: False,
        "_scan_hold_drive": lambda p, w: None,
        "_drive_latency_summary": lambda p: "",
        "_scan_drive_on_hold": lambda p: False,
        "_governor_priority_scope": contextlib.nullcontext,
        "_governor_nullscope": contextlib.nullcontext,
        "_scan_store_duration": fake_store,
        "_scan_duration_failed": lambda p: failed.append(p) or False,
        "_background_scan_pace_seconds": lambda base: base,
        "SCAN_CLASS_NOW": 0,
        "BETWEEN_LOUDNESS_PROBES_SECONDS": 0.5,
        "SCAN_PIPELINE_PREEMPT_CHECK_SECONDS": 5.0,
        "DURATION_PRECACHE_PAUSE_RECHECK_SECONDS": 5.0,
        "DURATION_PRECACHE_IDLE_SLEEP_SECONDS": 20.0,
    }
    exec(compile(get_function_source("_duration_scan_worker"), "<extracted:_duration_scan_worker>", "exec"), ns)
    ns["_duration_scan_worker"]()
    check(probed == ["/m/now.mkv", "/m/next.mkv", "/m/bad.mkv", "/m/deep.mkv"],
          "durations are stored in the scan chain's order, files with a stored duration skipped (got %s)" % probed)
    check(failed == ["/m/bad.mkv"], "a file with no duration goes to the failure handling once")
    check(order_log == ["saved"], "stored durations are saved once the pass ends")
    check(stop.waits[0] == 0.5 and stop.waits[-1] == 20.0, "the airing file is not paced, later files are, and the pass ends idle")
    worker_src = get_function_source("_duration_scan_worker")
    check("_probe_one_loudness" not in worker_src and "_pipeline_stage_picture_box" not in worker_src,
          "the duration scan never waits for a picture box or loudness scan")
    check("threading.Thread(target=_duration_scan_worker" in MAIN_SRC, "the duration scan thread is started with the other scans")
    chain_stage_src = MAIN_SRC.split("def _pipeline_stage_duration")[1].split("def _pipeline_scan_file")[0]
    check("_probe_duration_gated" not in chain_stage_src, "the scan chain no longer probes duration itself")


class TestDurationScanAhead(_SectionCase):
    """Duration is stored by its own scan, in the chain's order, ahead of picture box and loudness"""

    def test_duration_scan_ahead(self):
        self.run_section(suite_duration_scan_ahead)


# ============================================================================
# SECTION: Guide history ledger and 7-day window
# ============================================================================

def suite_guide_history_ledger():
    day = datetime.datetime(2026, 10, 15, 19, 52, 54)
    today = day.toordinal()
    base = today * 86400

    def tl(*items):
        t, out = 0, []
        for path, dur in items:
            out.append({"path": path, "duration": dur, "start_time": t, "is_commercial": False, "seek_offset": 0})
            t += dur
        return out

    plog = {}
    media.aired_ledger_upsert(plog, "Full Day", "2026_288_Full Day", base + 70521, tl(("/m/FG106.mkv", 1320), ("/m/FG101.mkv", 1320)), 2640, day)
    ledger = plog[media.GUIDE_AIRED_LEDGER_KEY]
    check(len(ledger) == 1, "a cycle is recorded once")
    first = next(iter(ledger.values()))
    media.aired_ledger_upsert(plog, "Full Day", "2026_288_Full Day", base + 70521, tl(("/m/FG106.mkv", 1320), ("/m/FG101.mkv", 1320)), 2640, day)
    check(next(iter(plog[media.GUIDE_AIRED_LEDGER_KEY].values())) is first, "an unchanged cycle is not copied again")
    media.aired_ledger_upsert(plog, "Full Day", "2026_288_Full Day", base + 70521, tl(("/m/FG106.mkv", 1320), ("/m/FG102.mkv", 1320)), 2640, day)
    check(next(iter(plog[media.GUIDE_AIRED_LEDGER_KEY].values()))["timeline"][1]["path"] == "/m/FG102.mkv", "a changed cycle replaces its older copy")

    media.aired_ledger_upsert(plog, "Full Day", "2026_287_Full Day", base - 86400 + 3600, tl(("/m/Old.mkv", 1800)), 1800, day)
    media.aired_ledger_upsert(plog, "Full Day", "2026_286_Full Day", base - 2 * 86400 + 3600, tl(("/m/TooOld.mkv", 1800)), 1800, day)
    paths = sorted(e["path"] for r in plog[media.GUIDE_AIRED_LEDGER_KEY].values() for e in r["timeline"])
    check("/m/Old.mkv" in paths and "/m/TooOld.mkv" not in paths, "cycles back to the previous midnight are kept, older ones are dropped")

    next_day = datetime.datetime(2026, 10, 16, 0, 0, 5)
    media.aired_ledger_upsert(plog, "Full Day", "2026_289_Full Day", next_day.toordinal() * 86400, tl(("/m/New.mkv", 1800)), 1800, next_day)
    paths = sorted(e["path"] for r in plog[media.GUIDE_AIRED_LEDGER_KEY].values() for e in r["timeline"])
    check("/m/Old.mkv" not in paths and "/m/New.mkv" in paths, "at midnight the oldest day falls out of the ledger")

    rows = media.aired_ledger_entries(plog, next_day.toordinal())
    check([r[3] for r in rows][:1] == ["Full Day"] and rows == sorted(rows, key=lambda r: r[1]), "ledger entries come back in start order with their block name")
    check(any(r[1] == -86400 + 70521 for r in rows) or any(abs(r[1] - (70521 - 86400)) < 1 for r in rows), "ledger times are seconds from today's midnight, negative for yesterday")

    overlap = {}
    media.aired_ledger_upsert(overlap, "A", "k1", base + 0, tl(("/m/a.mkv", 3600)), 3600, day)
    media.aired_ledger_upsert(overlap, "B", "k2", base + 1800, tl(("/m/b.mkv", 3600)), 3600, day)
    check(len(media.aired_ledger_entries(overlap, today)) == 1, "an entry overlapping an earlier one is dropped")

    ns = {"media": media, "log": MagicMock()}
    exec(compile(get_function_source("_note_aired_cycle"), "<extracted:_note_aired_cycle>", "exec"), ns)
    plog2 = {}
    ns["_note_aired_cycle"](plog2, "Full Day", "2026_288_Full Day", day, 71574.0, 71574.0 - 12600, 57921, tl(("/m/FG106.mkv", 1320)), 1320)
    rec = next(iter(plog2[media.GUIDE_AIRED_LEDGER_KEY].values()))
    check(rec["start"] == base + 70521, "the cycle's absolute start is the block anchor plus the cycle boundary (19:35:21)")
    ns["_note_aired_cycle"](plog2, "Full Day", "k", day, 0, 0, 0, None, 0)
    ns["media"] = types.SimpleNamespace(aired_ledger_upsert=lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    exec(compile(get_function_source("_note_aired_cycle"), "<extracted:_note_aired_cycle>", "exec"), ns)
    ns["_note_aired_cycle"](plog2, "Full Day", "k", day, 0, 0, 0, tl(("/m/x.mkv", 60)), 60)
    check(True, "a ledger failure never reaches the scheduler")

    cols = 6
    check(ui.clamp_guide_time_idx(-500, cols) == -48, "the guide stops at the previous midnight")
    check(ui.clamp_guide_time_idx(10000, cols) == 336 - 48 - cols, "the last visible column ends 6 days after today's midnight")
    check(ui.GUIDE_TOTAL_HALF_HOURS * 1800 == 7 * 86400, "7 days are always visible")
    ui_src = inspect.getsource(ui)
    check("aired_ledger_entries" in ui_src, "the guide draws past cells from the ledger")
    check(MAIN_SRC.count("_note_aired_cycle(playback_log, current_block, block_key") == 2,
          "the scheduler records a cycle when it ends and every call for the live cycle")


class TestGuideHistoryLedger(_SectionCase):
    """The guide remembers back to the previous midnight and always shows 7 days"""

    def test_guide_history_ledger(self):
        self.run_section(suite_guide_history_ledger)


# ============================================================================
# SECTION: Guide past rebuild from the schedule
# ============================================================================

def suite_guide_past_replay():
    import copy as _copy

    def eps(show, n):
        return [{"path": "/m/%s/S01E%02d.mkv" % (show, i)} for i in range(1, n + 1)]

    items = eps("Alpha", 6) + eps("Beta", 5)
    cache = {}
    for it in items:
        p = it["path"]
        if "Alpha" in p and not p.endswith(("E05.mkv", "E06.mkv")):
            cache[p] = {"duration": 1320}
        if "Beta" in p and not p.endswith("E05.mkv"):
            cache[p] = {"duration": 2700}
    exists = lambda p: True

    guesses = media.estimated_track_durations(items + [{"path": "/m/Gamma/S01E01.mkv"}, {"path": "/m/Movies/Heat.mkv"}], cache, exists)
    check(guesses["/m/Alpha/S01E05.mkv"] == 1320 and guesses["/m/Beta/S01E05.mkv"] == 2700,
          "an unscanned episode is guessed from its own series' scanned episodes")
    check(guesses["/m/Gamma/S01E01.mkv"] in (1320, 2700),
          "an unscanned episode of a series with nothing scanned takes the channel's middle episode length")
    check(guesses["/m/Movies/Heat.mkv"] == media._MOVIE_DURATION_FALLBACK_SECONDS,
          "an unscanned movie takes the movie default")
    check(media.estimated_track_durations([{"path": "/m/Solo/S01E01.mkv"}], {}, exists)["/m/Solo/S01E01.mkv"] == media.GUIDE_REPLAY_EPISODE_GUESS_SECONDS,
          "with nothing scanned anywhere an episode takes the default episode length")
    filled = {t["path"]: t for t in media.build_guess_filled_tracks(items, cache, exists)}
    check(len(filled) == len(items) and filled["/m/Alpha/S01E05.mkv"]["duration"] == 1320 and "_duration_estimated" not in filled["/m/Alpha/S01E05.mkv"],
          "the guess-filled tracks keep every file, unscanned ones at their guessed length")
    check("/m/Gamma/S01E01.mkv" not in {t["path"] for t in media.build_raw_tracks(items + [{"path": "/m/Gamma/S01E01.mkv"}], cache, exists)},
          "the scheduler's own builder still leaves out an unscanned file it cannot guess")
    check("/m/Gamma/S01E01.mkv" in {t["path"] for t in media.build_guess_filled_tracks(items + [{"path": "/m/Gamma/S01E01.mkv"}], cache, exists)},
          "the guess-filled builder keeps it")

    ch = {"scheduling_mode": "one_slot", "block_mode": "full_day", "schedules": {"Full Day": items},
          "playback_log": {"show_pos": {}}, "pair_threshold_minutes": 15}
    now = datetime.datetime(2026, 10, 15, 19, 0, 0)
    key = lambda d, name: "%d_%d_%s" % (d.year, d.timetuple().tm_yday, name)
    d = datetime.date
    before = _copy.deepcopy(ch)
    recs = media.replay_missed_blocks("05", ch, {}, key(d(2026, 10, 10), "Full Day"), key(d(2026, 10, 15), "Full Day"),
                                      now, cache, exists, [], lambda off: "7:%d" % off)
    check(ch == before, "the rebuild changes nothing in the channel")
    check(recs and all(r[2] + r[4] > (now.toordinal() - 1) * 86400 for r in recs),
          "only cycles that reach back no further than the previous midnight are returned")
    check(all(a[2] + a[4] <= b[2] + 0.5 for a, b in zip(recs, recs[1:])), "rebuilt cycles follow each other without overlap")
    check(recs[-1][2] // 86400 == d(2026, 10, 14).toordinal() and recs[-1][2] + recs[-1][4] <= d(2026, 10, 15).toordinal() * 86400 + 7200,
          "the last rebuilt cycle is on the day before the current block")
    placed = {e["path"]: e["duration"] for r in recs for e in r[3]}
    check(placed.get("/m/Alpha/S01E05.mkv") == 1320, "an unscanned file is placed in the past at its guessed length")
    again = media.replay_missed_blocks("05", ch, {}, key(d(2026, 10, 10), "Full Day"), key(d(2026, 10, 15), "Full Day"),
                                       now, cache, exists, [], lambda off: "7:%d" % off)
    check([(r[1], r[2], r[3]) for r in again] == [(r[1], r[2], r[3]) for r in recs], "the same gap always rebuilds the same past")

    check(media.replay_missed_blocks("05", ch, {}, key(d(2026, 10, 14), "Full Day"), key(d(2026, 10, 15), "Full Day"),
                                     now, cache, exists, [], lambda off: "x") == [], "no gap, nothing to rebuild")
    check(media.replay_missed_blocks("05", ch, {}, "", key(d(2026, 10, 15), "Full Day"),
                                     now, cache, exists, [], lambda off: "x") == [], "a channel with no earlier block has no past to rebuild")
    marathon = dict(ch, scheduling_mode="marathon")
    check(media.replay_missed_blocks("05", marathon, {}, key(d(2026, 10, 10), "Full Day"), key(d(2026, 10, 15), "Full Day"),
                                     now, cache, exists, [], lambda off: "x") == [], "marathon channels are left alone")
    holiday = media.replay_missed_blocks("05", ch, {}, key(d(2026, 10, 10), "Full Day"), key(d(2026, 10, 15), "Full Day"),
                                         now, cache, exists, [], lambda off: "7:%d" % off, is_holiday_date=lambda dd: dd == d(2026, 10, 14))
    check(all(r[2] // 86400 != d(2026, 10, 14).toordinal() or r[2] % 86400 < 7200 for r in holiday),
          "a holiday day is not rebuilt from the normal schedule")

    eight = dict(ch, block_mode="eight_hour", schedules={n: items for n in ("Morning", "Evening", "Night")})
    night = media.replay_missed_blocks("05", eight, {}, key(d(2026, 10, 14), "Evening"), key(d(2026, 10, 15), "Morning"),
                                       now, cache, exists, [], lambda off: "7:%d" % off)
    check(night and all(r[0] == "Night" for r in night) and night[0][2] == d(2026, 10, 14).toordinal() * 86400 + 20 * 3600,
          "after an evening and a morning start, the missed Night block is rebuilt from 20:00")
    six = dict(ch, block_mode="six_block", schedules={n: items for n in media.SIX_HOUR_BLOCK_NAMES})
    missed = media.replay_missed_blocks("05", six, {}, key(d(2026, 10, 15), "12-4am"), key(d(2026, 10, 15), "8-12pm"),
                                        now, cache, exists, [], lambda off: "7:%d" % off)
    check({r[0] for r in missed} == {"4-8am"}, "six-block channels rebuild exactly the blocks that were missed")

    ns = {"media": media, "time": time, "log": MagicMock(), "_cached_path_exists": exists,
          "_start_order_seed": lambda c, o: "7:%d" % o}
    exec(compile(get_function_source("_note_missed_blocks"), "<extracted:_note_missed_blocks>", "exec"), ns)
    plog_ch = _copy.deepcopy(ch)
    ns["_note_missed_blocks"](plog_ch, "05", {}, key(d(2026, 10, 10), "Full Day"), key(d(2026, 10, 15), "Full Day"),
                              now, cache, [], lambda dd: False)
    check(plog_ch["playback_log"].get(media.GUIDE_AIRED_LEDGER_KEY), "the rebuilt cycles are written to the channel's guide ledger")
    check(plog_ch["playback_log"]["show_pos"] == {}, "writing the ledger leaves the saved show positions alone")
    ns["media"] = types.SimpleNamespace(replay_missed_blocks=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    exec(compile(get_function_source("_note_missed_blocks"), "<extracted:_note_missed_blocks>", "exec"), ns)
    ns["_note_missed_blocks"](_copy.deepcopy(ch), "05", {}, "a", "b", now, cache, [], None)
    check(True, "a rebuild failure never reaches the scheduler")

    sched_src = get_function_source("calculate_slotted_playback_state")
    check(sched_src.index("_note_missed_blocks(") < sched_src.index("media.catch_up_state("),
          "the past is rebuilt from the saved positions before the catch-up moves them")
    check("GUIDE_REPLAY_MAX_BLOCKS" in inspect.getsource(__import__("performance_governor")),
          "the rebuild's numbers live in the governor table")


def suite_scheduler_catch_up():
    import copy as _copy

    sched_src = get_function_source("calculate_slotted_playback_state")
    items = [{"path": "/m/%s/S01E%02d.mkv" % (show, i), "duration": 1500} for show in ("Alpha", "Beta") for i in range(1, 31)]
    cache = {os.path.normpath(it["path"]): {"duration": 1500} for it in items}
    exists = lambda p: True
    order_seed = lambda offset: "7:%d" % offset

    def channel(mode):
        return {"scheduling_mode": "random_slots", "block_mode": mode, "episode_order_mode": "sequential",
                "schedules": {n: list(items) for n in ("Morning", "Evening", "Night", "Full Day") + tuple(media.SIX_HOUR_BLOCK_NAMES)},
                "playback_log": {"show_pos": {}}}

    eight = channel("eight_hour")
    before = _copy.deepcopy(eight)
    state = media.catch_up_state("07", eight, {}, "2026_280_Morning", "2026_283_Morning", cache, exists, [], order_seed)
    check(state is not None and state[3] == 8,
          "an 8 hour channel off for two and a half days replays all 8 missed blocks, not one per missed day")
    check(eight == before, "the catch-up changes nothing in the channel")
    again = media.catch_up_state("07", eight, {}, "2026_280_Morning", "2026_283_Morning", cache, exists, [], order_seed)
    check(again[0] == state[0], "the same gap always replays to the same positions")
    one_block = media.catch_up_state("07", eight, {}, "2026_280_Morning", "2026_280_Evening", cache, exists, [], order_seed)
    check(one_block is None, "blocks that follow each other have nothing missed between them")
    full = media.catch_up_state("07", channel("full_day"), {}, "2026_280_Full Day", "2026_283_Full Day", cache, exists, [], order_seed)
    check(full is not None and full[3] == 2, "a Full Day channel replays each missed day")
    six = media.catch_up_state("07", channel("six_block"), {}, "2026_280_%s" % media.SIX_HOUR_BLOCK_NAMES[0], "2026_282_%s" % media.SIX_HOUR_BLOCK_NAMES[0], cache, exists, [], order_seed)
    check(six is not None and six[3] == len(media.SIX_HOUR_BLOCK_NAMES) * 2 - 1, "a six block channel replays every missed block of its day")
    marathon = channel("full_day")
    marathon["scheduling_mode"] = "marathon"
    check(media.catch_up_state("07", marathon, {}, "2026_280_Full Day", "2026_283_Full Day", cache, exists, [], order_seed) is None,
          "marathon channels are not replayed")
    mixed = [{"path": "/m/%s/%s S01E%02d.mkv" % (s, s, i), "duration": d} for s, d in (("Alpha", 1320), ("Beta", 2700), ("Gamma", 1500)) for i in range(1, 26)]
    mixed_cache = {os.path.normpath(it["path"]): {"duration": it["duration"]} for it in mixed}

    def run_continuous(ch_info, name, ordinal):
        date = datetime.date.fromordinal(ordinal)
        offset = media.block_seed_offset(name, 0)
        seed = media.block_day_seed(date, "07", offset)
        secs = media.block_length_seconds(name)
        raw = media.build_raw_tracks(ch_info["schedules"][name], mixed_cache, exists)
        pos, commercial, cursor, first = {}, None, 0, None
        while cursor < secs:
            lineup, pos, _o = media.build_block_rotation(raw, ch_info, pos, seed, secs, "07", None, order_seed(offset), pending_tracks=[])
            timeline, total, commercial = media.build_channel_timeline(lineup, [], ch_info, seed, media_probe_cache=mixed_cache, commercial_state=commercial)
            first = first or (_copy.deepcopy(pos), total, commercial)
            cursor += total
        return pos, first

    for mode, name, prev_key, next_key in (("full_day", "Full Day", "2026_289_Full Day", "2026_290_Full Day"),
                                           ("eight_hour", "Morning", "2026_289_Morning", "2026_289_Evening"),
                                           ("eight_hour", "Night", "2026_289_Night", "2026_290_Morning")):
        ch_info = {"scheduling_mode": "random_slots", "block_mode": mode, "episode_order_mode": "sequential",
                   "schedules": {name: list(mixed)}, "playback_log": {"show_pos": {}}}
        ordinal = (datetime.date(2026, 1, 1) + datetime.timedelta(days=int(prev_key.split("_")[1]) - 1)).toordinal()
        whole, first = run_continuous(ch_info, name, ordinal)
        ch_info["playback_log"].update({"sched_cycle_boundary": 0, "sched_cycle_total": first[1], "commercial_pos": first[2]})
        resumed = media.catch_up_state("07", ch_info, first[0], prev_key, next_key, mixed_cache, exists, [], order_seed)
        check(resumed is not None and resumed[0] == whole,
              "%s: stopping after a block's first cycle and catching up lands on the same positions as running it all the way through" % name)
        ch_info["playback_log"]["sched_cycle_boundary"] = media.block_length_seconds(name)
        check(media.catch_up_state("07", ch_info, whole, prev_key, next_key, mixed_cache, exists, [], order_seed) is None,
              "%s: a block that already ran to its end has nothing left to catch up" % name)
    check("media.catch_up_state(" in sched_src and "CATCH_UP_MAX_BLOCKS" in inspect.getsource(__import__("performance_governor")),
          "the scheduler uses the replay and its numbers live in the governor table")


class TestSchedulerCatchUp(_SectionCase):
    """When a block starts, every block missed since the last one built is replayed, so series positions follow the schedule"""

    def test_scheduler_catch_up(self):
        self.run_section(suite_scheduler_catch_up)


def suite_commercial_cycle_state():
    import copy as _copy
    import math as _math
    import re as _re

    scenario = ("eight_hour_two_slots", "eight_hour", "interrupt_half_hour", "two_slots", True)

    def build():
        channels = {"05": _sched_record_channel(*scenario[1:])}
        clock, ticks = [datetime.datetime(2026, 10, 13, 0, 0, 5)], [0]
        ns = _sched_record_ns(channels, clock, ticks)
        for mod in (_copy, _math, _re):
            ns.setdefault(mod.__name__, mod)
        return channels, clock, ticks, ns

    def poll(ctx, moment):
        channels, clock, ticks, ns = ctx
        clock[0] = moment
        ticks[0] += 10000
        while True:
            try:
                with patch.object(media, "_cached_path_exists", lambda p: True):
                    result = ns["calculate_slotted_playback_state"]("05")
                return (result.get("mode"), os.path.basename(str(result.get("file", ""))), round(float(result.get("seek", 0) or 0)))
            except NameError as missing:
                name = str(missing).split("'")[1]
                exec(compile(get_function_source(name), "<extracted:%s>" % name, "exec"), ns)

    step = datetime.timedelta(minutes=3)
    start, off_at, back_at, until = (datetime.datetime(2026, 10, 13, 0, 0, 5), datetime.datetime(2026, 10, 13, 21, 0, 5),
                                     datetime.datetime(2026, 10, 14, 1, 18, 5), datetime.datetime(2026, 10, 14, 4, 0, 5))
    always_on, restarted = build(), build()
    aired, moment = {}, start
    while moment <= until:
        aired[moment] = poll(always_on, moment)
        if moment <= off_at:
            poll(restarted, moment)
        moment += step
    moment, differing, polls = back_at, [], 0
    while moment <= until:
        polls += 1
        if poll(restarted, moment) != aired[moment]:
            differing.append(moment)
        moment += step
    check(polls > 40 and not differing,
          "an app that was off across a block change airs exactly what an app that kept running airs, commercials included (%d polls, %d differ)" % (polls, len(differing)))

    plog = always_on[0]["05"]["playback_log"]
    check(isinstance(plog.get("commercial_pos"), dict) and isinstance(plog.get("commercial_next_pos"), dict)
          and plog["commercial_pos"] != plog["commercial_next_pos"],
          "the scheduler saves the state the airing cycle was built from and, apart, the state left after it")

    held = build()
    poll(held, datetime.datetime(2026, 10, 13, 4, 0, 5))
    poll(held, datetime.datetime(2026, 10, 13, 4, 21, 5))
    first_poll_state = _copy.deepcopy(held[0]["05"]["playback_log"]["commercial_pos"])
    poll(held, datetime.datetime(2026, 10, 13, 4, 42, 5))
    check(held[0]["05"]["playback_log"]["commercial_pos"] == first_poll_state,
          "later polls of the same cycle keep building it from the state it started from")

    check(media.carried_commercial_state({"commercial_pos": {"a": 1}, "commercial_next_pos": {"b": 2}}) == {"b": 2}
          and media.carried_commercial_state({"commercial_pos": {"a": 1}}) == {"a": 1}
          and media.carried_commercial_state({}) is None,
          "the carried commercial state is the one left after the cycle, and an older save falls back to its single saved state")


class TestCommercialCycleState(_SectionCase):
    """A block's commercials come from the state it started with on every poll, so a restart airs what continuous running would"""

    def test_commercial_cycle_state(self):
        self.run_section(suite_commercial_cycle_state)


def suite_block_anchor_real_start():
    import logging as _logging

    class _Db:
        saves = 0

        def save_settings(self):
            _Db.saves += 1

    anchor_for = load_function("_get_or_set_block_anchor", {"db": _Db(), "log": _logging.getLogger("anchor_test")})
    noon_ish = 12 * 3600 + 17 * 60
    aired = {"playback_log": {"sched_block_key": "2026_288_Full Day"}}
    check(anchor_for(aired, "2026_289_Full Day", noon_ish, real_start_seconds=0) == 0,
          "a channel that has aired before starts a new block at its real clock start, not at the half hour the app first saw it")
    check(anchor_for(aired, "2026_289_Full Day", 15 * 3600, real_start_seconds=0) == 0 and aired["playback_log"]["block_anchors"]["2026_289_Full Day"] == 0,
          "the anchor is saved and never changes once set")
    night = {"playback_log": {"sched_block_key": "2026_288_Evening"}}
    check(anchor_for(night, "2026_289_Night", 24 * 3600 + 1800, real_start_seconds=20 * 3600) == 20 * 3600,
          "a Night block seen after midnight is still measured from 20:00")
    fresh = {}
    check(anchor_for(fresh, "2026_289_Full Day", noon_ish, real_start_seconds=0) == 12 * 3600,
          "a channel that never aired before starts at the half hour it went on air")
    same = {"playback_log": {"sched_block_key": "2026_289_Full Day"}}
    check(anchor_for(same, "2026_289_Full Day", noon_ish, real_start_seconds=0) == 12 * 3600,
          "a block that is already the last one built keeps the old half hour anchor instead of moving mid-block")
    holiday = {"playback_log": {"sched_block_key": "2026_288_Full Day"}}
    check(anchor_for(holiday, "2026_289_holiday", noon_ish) == 12 * 3600,
          "a caller that gives no clock start gets the old behaviour")
    sched = get_function_source("calculate_slotted_playback_state")
    check(sched.count("real_start_seconds=0)") == 1 and sched.count("real_start_seconds=block_start_hour * 3600)") == 3,
          "every scheduler block passes its real clock start")


class TestBlockAnchorRealStart(_SectionCase):
    """A block the app first sees late is measured from its real clock start once the channel has aired before"""

    def test_block_anchor_real_start(self):
        self.run_section(suite_block_anchor_real_start)


class TestGuidePastReplay(_SectionCase):
    """The guide's past is rebuilt from the schedule for time the app was off, with guessed lengths for unscanned files"""

    def test_guide_past_replay(self):
        self.run_section(suite_guide_past_replay)


if __name__ == "__main__":
    run_logged(__file__, lambda: unittest.main(verbosity=2))
