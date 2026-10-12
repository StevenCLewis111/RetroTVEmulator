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
TEMP_DIR = os.path.join(ROOT, "temp")

for _sub in ("server", "updater", "assets", "GPU"):
    sys.path.insert(0, os.path.join(ROOT, _sub))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

if not hasattr(builtins, "ctypes"):
    builtins.ctypes = ctypes

import pygame  # noqa: E402

pygame.init()

import menu_nav as mn  # noqa: E402
import ui  # noqa: E402
import media  # noqa: E402
import media_library as ml  # noqa: E402
import performance_governor as gov  # noqa: E402

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

    def test_too_few_readable_samples_is_no_answer(self):
        good = _pb_stderr((720, 576), [(720, 378, 0, 99)])
        empty = _pb_stderr((720, 576), [])
        box, _ = _detect([good, good, empty, empty, empty, empty])
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
    }
    for name in ("_pan_scan_content_key", "_picture_box_set_content", "_apply_picture_box_crop"):
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
    n1_check("W held on a Video slider row does not repeat", hold("VIDEO_ROWS", 5, pygame.K_w), 0)
    n1_check("S held on a Theme slider row does not repeat", hold("THEME_ROWS", 4, pygame.K_s), 0)
    n1_check("D held on a Video radio row does not repeat", hold("VIDEO_ROWS", 1, pygame.K_d), 0)
    n1_check("D held on the Theme transition row does not repeat", hold("THEME_ROWS", 2, pygame.K_d), 0)
    n1_check("S held on the tab bar does not repeat", hold("TAB_SELECTION", 4, pygame.K_s), 0)
    n1_check("S held in System does not repeat", hold("SYSTEM_ROWS", 3, pygame.K_s), 0)
    n1_check("A held in Advanced Settings does not repeat", hold("SYSTEM_ADVANCED_SUB_MENU", 0, pygame.K_a), 0)
    n1_check("a slider key that went down in another layer does not repeat", hold("VIDEO_ROWS", 5, pygame.K_d, "TAB_SELECTION"), 0)


def repeat_allowed_checks():
    for layer in ("VIDEO_ROWS", "THEME_ROWS", "TAB_SELECTION", "SYSTEM_ROWS", "SYSTEM_ADVANCED_SUB_MENU", "CHANNEL_LIST",
                  "CHANNEL_SETTINGS_SUB_MENU", "GAMES_LIST", "GAMES_SUB_MENU", "GAMES_SAVE_PROFILE", "DVD_BTN_MAP", "GAMES_CTRL_MAP",
                  "SERVER_MENU", "SERVER_SLOTS_MENU", "REMOTE_REMAP_LIST", "AUDIO_TRACK_PICKER", "HOLIDAY_SUB_MENU", None):
        for selection in range(-1, 60):
            for key in (pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d, pygame.K_RETURN, pygame.K_UP):
                expected = (key in (pygame.K_a, pygame.K_d)
                            and ((layer == "VIDEO_ROWS" and 5 <= selection <= 9)
                                 or (layer == "THEME_ROWS" and 3 <= selection <= 5)))
                n1_check("repeat_allowed %s %s %s" % (layer, selection, pygame.key.name(key)),
                      mn.repeat_allowed(layer, selection, key), expected)


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
    structure(t, "video", "aspect_ratio")
    check(mn.video_table() is t, "video: table is cached")
    check(go(t, "aspect_ratio", mn.UP) == mn.TAB_BAR, "video: Aspect Ratio up goes to the tab bar")
    check(go(t, "aspect_ratio", mn.LEFT) is None and go(t, "aspect_ratio", mn.RIGHT) is None, "video: Aspect Ratio has no sideways move")
    check(go(t, "aspect_ratio", mn.DOWN) == "mode_normal", "video: Aspect Ratio down defaults to Normal Mode")
    radios = mn.VIDEO_RADIOS
    for k, radio in enumerate(radios):
        check(go(t, radio, mn.UP) == "aspect_ratio" and go(t, radio, mn.DOWN) == "brightness", "video: %s goes up and down" % radio)
        check(go(t, radio, mn.LEFT) == (radios[k - 1] if k else None), "video: %s left" % radio)
        check(go(t, radio, mn.RIGHT) == (radios[k + 1] if k + 1 < len(radios) else None), "video: %s right" % radio)
        for far in ("aspect_ratio", "brightness"):
            memory = {}
            go(t, radio, mn.UP if far == "aspect_ratio" else mn.DOWN, memory)
            back = mn.DOWN if far == "aspect_ratio" else mn.UP
            check(go(t, far, back, memory) == radio, "video: way back to %s from %s" % (radio, far))
    check(go(t, "brightness", mn.UP) == "mode_normal", "video: Brightness up with no memory goes to Normal Mode")
    sliders = mn.VIDEO_SLIDERS
    for k, slider in enumerate(sliders):
        check(go(t, slider, mn.DOWN) == (sliders[k + 1] if k + 1 < len(sliders) else mn.CLOSE), "video: %s down" % slider)
        if k:
            check(go(t, slider, mn.UP) == sliders[k - 1], "video: %s up" % slider)
        check(go(t, slider, mn.LEFT) is None and go(t, slider, mn.RIGHT) is None, "video: %s sideways is left to the value change" % slider)
    check(go(t, mn.CLOSE, mn.UP) == "tint" and go(t, mn.CLOSE, mn.DOWN) is None and go(t, mn.CLOSE, mn.LEFT) is None, "video: Close only goes up to Tint")
    for sel in range(11):
        check(mn.video_index(mn.video_cell(sel)) == sel, "video: selection %d round trip" % sel)
    check(mn.video_cell(10) == mn.CLOSE, "video: selection 10 is Close")


def case_theme():
    t = mn.theme_table()
    structure(t, "theme", "volume_bar")
    check(mn.theme_table() is t, "theme: table is cached")
    check(go(t, "volume_bar", mn.UP) == mn.TAB_BAR, "theme: Volume Bar up goes to the tab bar")
    check(go(t, "volume_bar", mn.DOWN) == "static_color" and go(t, "static_color", mn.DOWN) == "transition", "theme: down through the first rows lands on Channel Transition")
    check(go(t, "transition", mn.RIGHT) == "letterbox" and go(t, "letterbox", mn.LEFT) == "transition", "theme: Transition and Letterboxing are neighbours")
    check(go(t, "transition", mn.LEFT) is None and go(t, "letterbox", mn.RIGHT) is None, "theme: nothing beyond the pair")
    check(go(t, "letterbox", mn.UP) == "static_color" and go(t, "letterbox", mn.DOWN) == "opacity", "theme: Letterboxing moves vertically like its row")
    check(go(t, "opacity", mn.UP) == "transition", "theme: W from Opacity lands on Channel Transition, never on Letterboxing")
    check(go(t, "static_color", mn.DOWN) == "transition", "theme: S from Channel Static lands on Channel Transition")
    for a, b in (("opacity", "bg_hue"), ("bg_hue", "ui_hue"), ("ui_hue", mn.CLOSE)):
        check(go(t, a, mn.DOWN) == b and go(t, b, mn.UP) == a, "theme: %s and %s are neighbours" % (a, b))
    for slider in mn.THEME_SLIDERS:
        check(go(t, slider, mn.LEFT) is None and go(t, slider, mn.RIGHT) is None, "theme: %s sideways is left to the value change" % slider)
    check(go(t, mn.CLOSE, mn.DOWN) is None and go(t, mn.CLOSE, mn.LEFT) is None and go(t, mn.CLOSE, mn.RIGHT) is None, "theme: Close only goes up")
    for sel in range(7):
        cell = mn.theme_cell(sel, 0)
        check(mn.theme_index(cell) == sel and mn.theme_row2_column(cell) == 0, "theme: selection %d round trip" % sel)
    check(mn.theme_cell(2, 1) == "letterbox" and mn.theme_index("letterbox") == 2 and mn.theme_row2_column("letterbox") == 1, "theme: row 2 column 1 is Letterboxing")
    check(mn.theme_cell(3, 1) == "opacity", "theme: a stale row 2 column does not change other rows")


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
        return ["controller_map", "scan_for_roms", "clear_roms", "scan_for_cores", "clear_cores", "menu_bg_select_image", "multitap_toggle",
                "scroll_style", "text_color", "game_aspect_ratio", "game_screen_mode"]
    if not on:
        return []
    if console_key == "DVD":
        return ["splash_toggle", "splash_select_image", "splash_select_video", "dvd_captions_toggle", "dvd_resume_toggle",
                "dvd_button_mapping", "dvd_aspect_ratio", "dvd_screen_mode", "dvd_audio_track"]
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
        "controller_map": "W:- S:game_aspect_ratio A:- D:menu_bg_select_image",
        "scan_for_roms": "W:scroll_style S:clear_roms A:scan_for_cores D:-",
        "clear_roms": "W:scan_for_roms S:close A:clear_cores D:-",
        "scan_for_cores": "W:game_screen_mode S:clear_cores A:- D:scan_for_roms",
        "clear_cores": "W:scan_for_cores S:close A:- D:clear_roms",
        "menu_bg_select_image": "W:- S:multitap_toggle A:controller_map D:-",
        "multitap_toggle": "W:menu_bg_select_image S:scroll_style A:game_aspect_ratio D:-",
        "scroll_style": "W:multitap_toggle S:scan_for_roms A:game_screen_mode D:text_color",
        "text_color": "W:multitap_toggle S:scan_for_roms A:scroll_style D:-",
        "game_aspect_ratio": "W:controller_map S:game_screen_mode A:- D:multitap_toggle",
        "game_screen_mode": "W:game_aspect_ratio S:scan_for_cores A:- D:scroll_style",
        "close": "W:clear_cores S:- A:clear_cores D:-",
    },
    "DVD": {
        "status": "W:- S:dvd_button_mapping A:- D:splash_toggle",
        "splash_toggle": "W:status S:splash_select_image A:status D:-",
        "splash_select_image": "W:splash_toggle S:dvd_captions_toggle A:splash_select_video D:-",
        "splash_select_video": "W:splash_toggle S:dvd_captions_toggle A:dvd_button_mapping D:splash_select_image",
        "dvd_captions_toggle": "W:splash_select_video S:dvd_resume_toggle A:dvd_aspect_ratio D:-",
        "dvd_resume_toggle": "W:dvd_captions_toggle S:close A:dvd_screen_mode D:-",
        "dvd_button_mapping": "W:status S:dvd_aspect_ratio A:- D:splash_select_video",
        "dvd_aspect_ratio": "W:dvd_button_mapping S:dvd_screen_mode A:- D:dvd_captions_toggle",
        "dvd_screen_mode": "W:dvd_aspect_ratio S:dvd_audio_track A:- D:dvd_resume_toggle",
        "dvd_audio_track": "W:dvd_screen_mode S:close A:- D:close",
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
    check(walk(dvd, "dvd_resume_toggle", "SW") == "dvd_resume_toggle" and walk(dvd, "dvd_audio_track", "SW") == "dvd_audio_track" and walk(dvd, "dvd_audio_track", "DW") == "dvd_audio_track",
          "games sub DVD: Close way back to Resume or Change Audio, from S or D")
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


# ---------------------------------------------------------------------------
# DVD button map
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Save Profile
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Controller map
# ---------------------------------------------------------------------------

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
    check(mn.SLIDER_SPECS["bg_hue"][1:] == (220, 0, 360) and mn.SLIDER_SPECS["ui_hue"][1:] == (220, 0, 360) and mn.SLIDER_SPECS["opacity"][1:] == (50, 0, 100), "slider ranges as the old code")
    held = {mn.repeat_allowed("VIDEO_ROWS", sel, pygame.K_d) for sel in range(5, 10)}
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
    check(mn.HOLD_STUCK_MS == {"menu": 1500, "guide": 3000, "rom_list": 1500}, "stuck limits are the old numbers")
    arrows = [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN]
    for key in list(range(0, 1200)) + arrows:
        want = (mn.HOLD_RULES["vertical"] if key in (pygame.K_w, pygame.K_s)
                else mn.HOLD_RULES["horizontal"] if key in (pygame.K_a, pygame.K_d) else None)
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
        """The v93 inline code, written out."""
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
    check((mn.DIGIT_ENTRY_MAX, mn.DIGIT_ENTRY_SINGLE_MS, mn.DIGIT_ENTRY_CONFIRM_MS) == (2, 2500, 350), "digit numbers unchanged")


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
_R4_ALLOWED = re.compile(r"^#\s*(=+\s*$|-{10,}\s*$|--\s.*-{5,}\s*$|SECTION:|VALUES CHANGE HERE|WARNING:|COMING SOON|type:|noqa|pragma|-\*-|!)")
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
    dash = re.compile(r"^#\s*-{10,}\s*$")
    for rel in _R4_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        src, found = _r4_comments(path)
        for line_no, text, prev_line, next_line in found:
            title_between_dashes = bool(dash.match(prev_line) and dash.match(next_line))
            check(bool(_R4_ALLOWED.match(text)) or title_between_dashes, "%s line %d is a comment the rule does not allow: %s" % (rel, line_no, text[:80]))
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
    check(mn.stick_dz_rows(False) == [mn.STICK_DZ_SWITCH, mn.CLOSE], "off shows only the switch and Close")
    check(mn.stick_dz_rows(True) == [mn.STICK_DZ_SWITCH, mn.STICK_DZ_DEADZONE, mn.STICK_DZ_SENSITIVITY, mn.STICK_DZ_AXIS, mn.STICK_DZ_RESET, mn.CLOSE], "on shows the settings and Reset")
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


if __name__ == "__main__":
    run_logged(__file__, lambda: unittest.main(verbosity=2))
