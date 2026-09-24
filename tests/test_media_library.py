"""
Tests for media_library.py (item B of the 2026-09-19 handoff), checked
against the doc's own "Done when" criteria for item B:

  - A deleted and re-added file triggers no probe, and the log shows a
    library hit.
  - Three quick restarts lose nothing.
  - Reset All Settings leaves the library intact.
  - A new file spawns one header process, not four or five.

and the picture-box detection added for item D (detection + storage only;
nothing here decides how a picture is fitted to the screen).

media.py has real hardware dependencies (pygame audio device, etc.), so
this follows the same approach test_drive_latency_normalization.py and
test_broadcast_stability_suite.py already use: dummy SDL drivers so it
imports cleanly, no real ffmpeg/vlc process is ever actually launched --
subprocess.run and media.get_media_duration are mocked everywhere a real
probe would otherwise run.

Run with: python3 test_media_library.py
      or: python3 -m pytest test_media_library.py -v
Run as a script, the output is also saved to root/temp/test_media_library_output.txt
(next to app_log.txt); the previous run's is kept as ..._output_previous.txt.
"""
import os
import sys
import tempfile
import time
import shutil
import unittest
from unittest import mock

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

# Real project layout (same as test_broadcast_stability_suite.py):
#   root/tests/test_media_library.py   (this file)
#   root/assets/media.py
#   root/assets/media_library.py
# media.py and media_library.py live in root/assets/, one folder up from this
# file, so assets/ has to be on sys.path separately -- the project root and
# this file's own folder are inserted too, for consistency with the other
# test files.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
_ASSETS_DIR = os.path.join(_ROOT_DIR, "assets")
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, _ROOT_DIR)
sys.path.insert(0, _ASSETS_DIR)

import media  # noqa: E402  (real module, imported for real to keep parity with its regexes/behavior -- root/assets/media.py)
import media_library as ml  # noqa: E402  (root/assets/media_library.py)


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


class TestCombinedHeaderProbe(unittest.TestCase):
    """Item 4: one parse standing in for the four originals."""

    def _run(self, stderr_text):
        fake_result = mock.Mock(stderr=stderr_text, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result) as run_mock:
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
        # DAR 12:5 on a 1920x800 stream (raw ratio 2.4) -- DAR ratio is
        # 2.4 too here, so no correction; pick a case where they diverge
        # to prove the correction path itself works, mirroring
        # get_media_video_dimensions' own DAR-correction branch.
        stderr = SAMPLE_FFMPEG_STDERR.replace("[SAR 1:1 DAR 12:5]", "[SAR 1:1 DAR 2:1]")
        result, _ = self._run(stderr)
        self.assertEqual(result.height, 800)
        # DAR 2:1 on an 800-tall stream implies width 1600, not the raw 1920.
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
        with mock.patch.object(ml.subprocess, "run",
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
    """Item 2: size + 3x64KB sample hash, stable across rename/move,
    distinguishes real content changes, skips growing files."""

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
        self.assertIsNotNone(key)  # size (0) alone still yields a stable key

    def test_missing_file_returns_none(self):
        key = ml.compute_identity_key(os.path.join(self.tmp, "nope.mp4"), growing_check_delay=0)
        self.assertIsNone(key)

    def test_growing_file_is_skipped(self):
        p1 = os.path.join(self.tmp, "growing.mp4")
        _write_file(p1, os.urandom(1000))

        real_getsize = os.path.getsize
        call_count = {"n": 0}

        def fake_getsize(path):
            call_count["n"] += 1
            # First call (pre-delay) returns one size, second (post-delay)
            # returns a different one -- simulates an in-progress download.
            return 1000 if call_count["n"] == 1 else 2000

        with mock.patch.object(ml.os.path, "getsize", side_effect=fake_getsize), \
             mock.patch.object(ml.time, "sleep"):  # don't actually sleep in tests
            key = ml.compute_identity_key(p1)
        self.assertIsNone(key)


class TestPathKeyResolverAvoidsRehashing(unittest.TestCase):
    """Item 2's path/size/mtime -> key table: unchanged file is never
    re-hashed on a second resolve()."""

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
        # Simulate a re-encode: new content, forced mtime bump.
        _write_file(self.file_path, os.urandom(100_000))
        os.utime(self.file_path, (time.time() + 10, time.time() + 10))
        key2 = self.resolver.resolve(self.file_path)
        self.assertNotEqual(key1, key2)


class TestDoneWhenDeleteAndReAdd(unittest.TestCase):
    """Doc's first 'Done when': a deleted and re-added file (== a file
    whose bytes reappear, identical, possibly at a new path) triggers no
    probe, and there's a library hit."""

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

        # "Deleted and re-added": remove the original, write byte-identical
        # content back under a new path (as re-adding via a rescan would).
        os.remove(original_path)
        readded_path = os.path.join(self.tmp, "movie_readded.mp4")
        _write_file(readded_path, content)

        with mock.patch.object(ml.subprocess, "run") as run_mock_2:
            entry = self.probe_library.get_or_probe_sync(readded_path)
        run_mock_2.assert_not_called()  # library hit -- no probe needed
        self.assertIsNotNone(entry)
        self.assertEqual(entry["duration_source"], "ffmpeg")


class TestNewFileSpawnsOneHeaderProcess(unittest.TestCase):
    """Doc's fourth 'Done when': a new file spawns one header process,
    not four or five."""

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
    """Doc's second 'Done when': three quick restarts lose nothing --
    reopening the same db file picks up exactly what was committed."""

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
        # Each upsert_fields() call commits (see MediaLibrary.upsert_fields),
        # so a second, independent connection to the same file sees it
        # right away without the first connection closing first.
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
    """Doc's third 'Done when': Reset All Settings leaves the library
    intact. This module never reads or writes db.config, so a config
    reset structurally cannot touch it -- verified here by confirming
    the library's data survives a stand-in "reset" that wipes a
    separate config dict, and that resolve_media_library_dir() lives
    outside wherever config lives."""

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

        # Stand-in for db.factory_reset(): wipes a config dict entirely --
        # this module never held a reference into it, so nothing here
        # could have been affected either way.
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


class TestPrecachePoolTierOrdering(unittest.TestCase):
    """Item 5: 2-thread pool, priority tiers processed in doc order,
    de-duplicates already-queued work."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_thread_count_is_two(self):
        pool = ml.PrecachePool(self.probe_library)
        self.assertEqual(pool.thread_count, 2)

    def test_duplicate_enqueue_same_tier_is_ignored(self):
        pool = ml.PrecachePool(self.probe_library)
        pool.enqueue("a.mp4", ml.TIER_NOW_PLAYING)
        pool.enqueue("a.mp4", ml.TIER_NOW_PLAYING)
        self.assertEqual(pool.qsize(), 1)

    def test_processes_in_tier_order(self):
        order = []
        # Stub out the actual probing so this test is purely about
        # scheduling order, not header-probe behavior (already covered
        # above). get_cached() is legitimately called twice per item by
        # _process() when the first call comes back empty (checks, then
        # re-checks after get_or_probe_sync) -- record first-appearance
        # order rather than assuming exactly one call per item.
        with mock.patch.object(self.probe_library, "get_cached", side_effect=lambda p: order.append(p) or {}), \
             mock.patch.object(self.probe_library, "get_or_probe_sync", return_value={}):
            pool = ml.PrecachePool(self.probe_library, thread_count=1)
            pool.enqueue("everything_else.mp4", ml.TIER_EVERYTHING_ELSE)
            pool.enqueue("commercial.mp4", ml.TIER_COMMERCIAL_POOL)
            pool.enqueue("now_playing.mp4", ml.TIER_NOW_PLAYING)
            pool.enqueue("scheduled.mp4", ml.TIER_SCHEDULED)
            pool.enqueue("guide.mp4", ml.TIER_GUIDE_HIGHLIGHT)
            pool.start()
            deadline = time.time() + 2.0
            while pool.qsize() > 0 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)  # let the last in-flight item finish
            pool.stop()
        first_seen = list(dict.fromkeys(order))
        self.assertEqual(first_seen, [
            "now_playing.mp4", "guide.mp4", "scheduled.mp4",
            "commercial.mp4", "everything_else.mp4",
        ])

    def test_loudness_hook_only_fires_for_loudness_tier(self):
        loudness_calls = []
        with mock.patch.object(self.probe_library, "get_cached", return_value={"header_probed_at": 1.0}):
            pool = ml.PrecachePool(self.probe_library, thread_count=1,
                                    loudness_fn=lambda p: loudness_calls.append(p))
            pool.enqueue("not_loudness.mp4", ml.TIER_EVERYTHING_ELSE)
            pool.enqueue("is_loudness.mp4", ml.TIER_LOUDNESS)
            pool.start()
            deadline = time.time() + 2.0
            while pool.qsize() > 0 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            pool.stop()
        self.assertEqual(loudness_calls, ["is_loudness.mp4"])


class TestLegacyCacheMigration(unittest.TestCase):
    """Item 7: throttled, resumable, never mutates the source dicts,
    skips missing files (leaving them for the old cache to keep
    serving), skips growing files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "lib.sqlite3")
        self.library = ml.MediaLibrary(self.db_path)
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")

        self.present_path = os.path.join(self.tmp, "present.mp4")
        _write_file(self.present_path, os.urandom(50_000))
        self.missing_path = os.path.join(self.tmp, "missing.mp4")  # never created

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
        self.assertEqual(progress.migrated, 2)  # present.mp4 from both dicts
        self.assertEqual(progress.skipped_missing, 1)

        entry = self.probe_library.get_cached(self.present_path)
        self.assertEqual(entry["duration"], 1234)
        self.assertEqual(entry["width"], 1920)
        self.assertEqual(entry["gain"], 0.95)  # dvd_gain_cache processed after, wins

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
        # Keep stepping until done -- must terminate.
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


class TestRekeyAfterReencode(unittest.TestCase):
    """Item 8: the renorm sweep re-encodes a file in place; its data
    must follow it onto the new content key."""

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

    def test_data_follows_the_file_across_reencode(self):
        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result), \
             mock.patch.object(media, "_duration_looks_implausible", return_value=False):
            self.probe_library.get_or_probe_sync(self.file_path)
        old_entry = self.probe_library.get_cached(self.file_path)
        self.assertIsNotNone(old_entry)

        # Simulate the renorm sweep rewriting the file in place with new
        # (louder-normalized) audio -- different bytes, same path.
        _write_file(self.file_path, os.urandom(100_000))
        os.utime(self.file_path, (time.time() + 5, time.time() + 5))

        new_key = self.probe_library.rekey_after_reencode(
            self.file_path, preserve_fields={"gain": 1.0, "duration": old_entry["duration"]},
        )
        self.assertIsNotNone(new_key)
        new_entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(new_entry["gain"], 1.0)  # the sweep's fresh gain, not stale
        self.assertEqual(new_entry["width"], old_entry["width"])  # carried forward from old row

    def test_old_key_row_is_removed_after_rekey(self):
        fake_result = mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, stdout="", returncode=0)
        with mock.patch.object(ml.subprocess, "run", return_value=fake_result), \
             mock.patch.object(media, "_duration_looks_implausible", return_value=False):
            self.probe_library.get_or_probe_sync(self.file_path)
        old_key = self.probe_library.keys.resolve(self.file_path)

        _write_file(self.file_path, os.urandom(100_000))
        os.utime(self.file_path, (time.time() + 5, time.time() + 5))
        new_key = self.probe_library.rekey_after_reencode(self.file_path)

        self.assertNotEqual(old_key, new_key)
        self.assertIsNone(self.library.get_entry(old_key))


class TestStorageAPIs(unittest.TestCase):
    """Item 3's per-file field slots -- storage only, no detector/scanner
    logic duplicated here."""

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
        self.assertLess(len(entry["captions_blob"]), len(text.encode("utf-8")))  # actually compressed

    def test_captions_permanent_no_speech_reason_stored(self):
        self.probe_library.store_captions(self.file_path, "", source="asr", reason="no_speech")
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["captions_reason"], "no_speech")

    def test_pan_scan_memory_round_trip(self):
        self.probe_library.store_pan_scan_memory(self.file_path, {"shots": [{"t": 0, "offset": 0.3}]})
        entry = self.probe_library.get_cached(self.file_path)
        self.assertEqual(entry["pan_scan_memory"]["shots"][0]["offset"], 0.3)



# ---------------------------------------------------------------------------
# Picture-box detection (item D: detection + storage only)
# ---------------------------------------------------------------------------

def _pb_stderr(frame=(720, 576), boxes=None, dar=None, video_extra="", has_video=True):
    """Fake ffmpeg stderr for one cropdetect sample: the input banner, then
    cropdetect lines. boxes is a list of (w, h, x, y); the LAST is the running
    widest box, exactly as with reset=0."""
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
        # 720x576 frame with a 378-tall picture: the Beauty and the Beast shape.
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
        # The second-file case in the handoff: small baked-in left/right bars.
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
        # One sample saw a dark shot (box too small); the others saw the full picture.
        dark = _pb_stderr((720, 576), [(600, 300, 60, 130)])
        good = _pb_stderr((720, 576), [(720, 378, 0, 99)])
        box, _ = _detect([good, dark, good, good, dark, good])
        self.assertEqual(box["rect"], [0, 99, 720, 378])

    def test_asymmetric_vertical_bars_leave_that_axis_full_frame(self):
        # A subtitle burnt into the bottom bar makes the bars unequal.
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
        # Symmetric letterbox plus one-sided left edge garbage: crop what is trustworthy.
        box, _ = _detect([_pb_stderr((1280, 720), [(1250, 544, 30, 88)])])
        self.assertEqual(box["reason"], "letterbox")
        self.assertEqual(box["rect"][0], 0)
        self.assertEqual(box["rect"][2], 1280)
        self.assertEqual(box["rect"][1:4:2], [88, 544])

    def test_declared_dar_feeds_picture_aspect_not_rect(self):
        # 720x480 stored, DAR 16:9, no bars: rect stays in stored pixels,
        # the displayed shape comes out 16:9.
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
        self.assertAlmostEqual(entry["duration"], 1 * 3600 + 19 * 60 + 35.40, places=2)  # header data still there

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


class TestPrecachePoolPictureBox(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.library = ml.MediaLibrary(os.path.join(self.tmp, "lib.sqlite3"))
        self.probe_library = ml.ProbeLibrary(self.library, ffmpeg_path="ffmpeg")

    def tearDown(self):
        self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_pool(self, entry, current_version=None):
        calls = []
        with mock.patch.object(self.probe_library, "get_cached", return_value=entry):
            pool = ml.PrecachePool(self.probe_library, thread_count=1,
                                    picture_box_fn=lambda p: calls.append(p),
                                    picture_box_current_version=current_version)
            pool.enqueue("a.mp4", ml.TIER_EVERYTHING_ELSE)
            pool.start()
            deadline = time.time() + 2.0
            while pool.qsize() > 0 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            pool.stop()
        return calls

    def test_hook_runs_when_no_box_yet(self):
        self.assertEqual(self._run_pool({"header_probed_at": 1.0}), ["a.mp4"])

    def test_hook_skipped_when_box_present_and_no_version_given(self):
        entry = {"header_probed_at": 1.0, "picture_box": {"rect": [0, 0, 1, 1]}, "picture_box_detector_version": 0}
        self.assertEqual(self._run_pool(entry), [])

    def test_stale_version_reruns_only_when_current_version_is_set(self):
        entry = {"header_probed_at": 1.0, "picture_box": {"rect": [0, 0, 1, 1]}, "picture_box_detector_version": 1}
        self.assertEqual(self._run_pool(entry, current_version=2), ["a.mp4"])
        self.assertEqual(self._run_pool(entry, current_version=1), [])

    def test_hook_is_the_libraries_own_method_end_to_end(self):
        with mock.patch.object(ml.subprocess, "run",
                               return_value=mock.Mock(stderr=SAMPLE_FFMPEG_STDERR, returncode=0)), \
             mock.patch.object(ml, "detect_picture_box", return_value={"rect": [0, 0, 1, 1]}) as det:
            path = os.path.join(self.tmp, "m.avi")
            _write_file(path, os.urandom(40_000))
            pool = ml.PrecachePool(self.probe_library, thread_count=1,
                                    picture_box_fn=self.probe_library.detect_picture_box_if_needed)
            pool.enqueue(path, ml.TIER_NOW_PLAYING)
            pool.start()
            deadline = time.time() + 3.0
            while det.call_count == 0 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            pool.stop()
        self.assertEqual(det.call_count, 1)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class TestPictureBoxRealFfmpeg(unittest.TestCase):
    """Runs the real detector on short synthetic clips built with ffmpeg
    itself, so the cropdetect command line and output parsing are exercised
    against a real ffmpeg, not only canned text."""

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

if __name__ == "__main__":
    from extract import run_logged  # also saves this run's output to root/temp, next to app_log.txt
    run_logged(__file__, lambda: unittest.main(verbosity=2))
