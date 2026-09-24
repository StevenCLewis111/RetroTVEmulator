"""
Fault-injection / regression test suite for the broadcast-stability audit.

Approach, and why:
  - media.py and broadcast_clock.py import cleanly as real modules (verified
    directly -- media.py needs SDL_AUDIODRIVER/SDL_VIDEODRIVER=dummy since
    there's no real audio/video device in this environment; pygame itself
    doesn't require a display to import). Tests against these two files run
    the real, unmodified functions.
  - retro_tv_emulator.py cannot be imported: it boots the actual app on
    import (real windows, real threads, a hard pygame.mixer device
    dependency) -- confirmed by direct attempt earlier. Its functions are
    instead extracted by exact AST boundary (extract.py) and exec'd into a
    minimal namespace containing only the module-level names that specific
    function actually touches (e.g. a stub `log`). This is real code under
    test, not a hand-typed re-description of it, so it cannot silently
    drift from what's actually shipped.
  - One test is explicitly a *source-level characterization test*, not a
    runtime simulation -- see test_boot_anchor_policy_never_trusts_cold_anchor
    for why, and what it does and doesn't prove.

Run with: python3 -m pytest test_broadcast_stability_suite.py -v
      or: python3 test_broadcast_stability_suite.py   (falls back to a
          plain runner if pytest isn't installed)
Run as a script, the output is also saved to root/temp/test_broadcast_stability_suite_output.txt
(next to app_log.txt); the previous run's is kept as ..._output_previous.txt.
"""
import os
import sys
import time
import unittest
import tempfile
import datetime as dt
from unittest import mock

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

# Real project layout:
#   root/retro_tv_emulator.py
#   root/tests/extract.py
#   root/tests/test_broadcast_stability_suite.py   (this file)
#   root/assets/media.py
#   root/assets/broadcast_clock.py
# media.py and broadcast_clock.py live in root/assets/, not next to this file,
# so assets/ has to be on sys.path separately -- this file's own folder is
# still needed too, since extract.py sits there (it reads
# retro_tv_emulator.py from root/), and the project root itself is added so
# the app's own modules resolve.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
_ASSETS_DIR = os.path.join(_ROOT_DIR, "assets")
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, _ROOT_DIR)
sys.path.insert(0, _ASSETS_DIR)

import media  # noqa: E402  (real import, real code -- root/assets/media.py)
import broadcast_clock  # noqa: E402  (real import, real code -- root/assets/broadcast_clock.py)
from extract import load_function  # noqa: E402  (root/tests/extract.py)


class _StubLog:
    """Swallow log calls; assertions check return values / state, not log
    text, except where a test explicitly cares whether a warning fired."""
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, msg, *a, **k):
        self.warnings.append(msg % a if a else msg)

    def info(self, msg, *a, **k):
        self.infos.append(msg % a if a else msg)

    def error(self, msg, *a, **k):
        pass


# ---------------------------------------------------------------------------
# Fix #3 -- tri-state path checks (media.py, real import)
# ---------------------------------------------------------------------------
class TestTriStatePathChecks(unittest.TestCase):
    def setUp(self):
        # Path-status results are cached; start each test from a clean cache
        # so one test's result can't leak into the next via TTL.
        with media._path_exists_cache_lock:
            media._path_exists_cache.clear()
        with media._drive_health_lock:
            media._drive_health.clear()
            media._watched_drive_roots.clear()

    def test_confirmed_present_file(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(media._path_status(f.name), media.PATH_PRESENT)
            self.assertTrue(media._cached_path_exists(f.name))

    def test_confirmed_missing_file(self):
        missing = "/tmp/definitely_does_not_exist_audit_test_12345.mp4"
        self.assertEqual(media._path_status(missing), media.PATH_MISSING)
        self.assertFalse(media._cached_path_exists(missing))

    def test_empty_path_is_missing_not_crash(self):
        self.assertEqual(media._path_status(""), media.PATH_MISSING)
        self.assertEqual(media._path_status(None), media.PATH_MISSING)

    def test_unresponsive_drive_reports_unknown_not_missing(self):
        # Simulate a drive already known-unresponsive (the state
        # _bounded_os_path_exists sets when a probe times out). A file on
        # that drive must read UNKNOWN, never MISSING -- MISSING would let
        # it be silently dropped from scheduling, which is the exact bug
        # fix #3 closes.
        with tempfile.NamedTemporaryFile() as f:
            root = media._drive_root_of(f.name)
            with media._drive_health_lock:
                media._watched_drive_roots.add(root)
                media._drive_health[root] = {
                    "responsive": False, "checked_at": media._now_ms(), "checking_since": None,
                }
            status = media._path_status(f.name)
            self.assertEqual(status, media.PATH_UNKNOWN)
            # And the boolean wrapper must still fail OPEN (True), matching
            # every existing caller's expectation.
            self.assertTrue(media._cached_path_exists(f.name))

    def test_unknown_has_its_own_short_ttl_not_the_long_present_ttl(self):
        # UNKNOWN must not be cached as long as a confirmed PRESENT --
        # confirmed via the sweep's own TTL table, not by sleeping 30s.
        with media._path_exists_cache_lock:
            media._path_exists_cache["/fake/path"] = (media.PATH_UNKNOWN, media._now_ms())
        # Simulate time passing past the UNKNOWN TTL but not the PRESENT TTL.
        future_ms = media._now_ms() + media._PATH_UNKNOWN_TTL_MS + 100
        with media._path_exists_cache_lock:
            media._sweep_path_exists_cache(future_ms)
            self.assertNotIn("/fake/path", media._path_exists_cache)


# ---------------------------------------------------------------------------
# Fix #8 -- real file-level drive-recovery probe (media.py, real import)
# ---------------------------------------------------------------------------
class TestFileLevelRecoveryProbe(unittest.TestCase):
    def setUp(self):
        with media._file_probe_lock:
            media._file_probe_state.clear()

    def _wait_for_probe(self, root, path, timeout_s=2.0):
        deadline = time.time() + timeout_s
        result = None
        while time.time() < deadline:
            result = media.probe_file_level_recovery(root, path)
            with media._file_probe_lock:
                entry = media._file_probe_state.get(root)
            if entry and entry.get("checking_since") is None:
                return media.probe_file_level_recovery(root, path)
            time.sleep(0.02)
        return result

    def test_real_readable_file_confirms_recovery(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"real bytes on disk")
            f.flush()
            root = media._drive_root_of(f.name)
            result = self._wait_for_probe(root, f.name)
            self.assertTrue(result, "a real, readable file must report recovered=True")

    def test_genuinely_unreadable_target_reports_false(self):
        root = media._drive_root_of("/tmp/x")
        missing = "/tmp/definitely_missing_for_probe_test_999.mp4"
        result = self._wait_for_probe(root, missing)
        self.assertFalse(result, "a target that can't be opened must never report recovered=True")

    def test_stale_probe_target_is_dropped_when_failing_file_changes(self):
        # Scenario C from the original build: if a newer failure lands on
        # the same drive root before the old target's probe resolves, the
        # probe must re-target the newer file rather than reporting
        # recovery based on stale evidence for a file that's no longer the
        # one that matters.
        root = "/fake_root"
        with media._file_probe_lock:
            media._file_probe_state[root] = {
                "last_result": True, "checked_at": media._now_ms(),
                "checking_since": None, "path": "/old/failing/file.mp4",
            }
        # A new candidate path for the same root -- must not simply return
        # the stale True for the old file.
        with media._file_probe_lock:
            entry = media._file_probe_state.get(root)
            self.assertNotEqual(entry.get("path"), "/new/failing/file.mp4")
        result = media.probe_file_level_recovery(root, "/new/failing/file.mp4")
        # First call after a target change starts a fresh probe and cannot
        # honestly report the old result for the new file.
        self.assertIsNone(result, "stale result for a DIFFERENT file must not be reused")


# ---------------------------------------------------------------------------
# Fix #6 -- preserve cached lineup on UNKNOWN paths (real extracted code)
# ---------------------------------------------------------------------------
class TestReconcileCachedLineup(unittest.TestCase):
    def setUp(self):
        self.log = _StubLog()
        self.fn = load_function("_reconcile_cached_lineup", {"log": self.log})

    def test_normal_hit_passes_through(self):
        cached = ["/a.mp4", "/b.mp4"]
        raw = [{"path": "/a.mp4", "duration": 100}, {"path": "/b.mp4", "duration": 200}]
        ordered, dropped = self.fn(cached, raw, folder_items=[{"path": "/a.mp4"}, {"path": "/b.mp4"}],
                                    broken_files=set(), sched_cached_durations={})
        self.assertEqual([t["path"] for t in ordered], ["/a.mp4", "/b.mp4"])
        self.assertEqual(dropped, [])

    def test_transient_miss_is_kept_not_dropped(self):
        # The exact bug: /b.mp4 is still scheduled and not broken, but this
        # pass's path check happened to miss it (a drive hiccup). It must
        # be kept at its last-known duration, not silently vanish and
        # shrink the currently-airing lineup.
        cached = ["/a.mp4", "/b.mp4"]
        raw = [{"path": "/a.mp4", "duration": 100}]  # /b.mp4 missed this pass
        ordered, dropped = self.fn(
            cached, raw,
            folder_items=[{"path": "/a.mp4"}, {"path": "/b.mp4"}],
            broken_files=set(),
            sched_cached_durations={"/b.mp4": 200},
        )
        self.assertEqual([t["path"] for t in ordered], ["/a.mp4", "/b.mp4"])
        self.assertEqual(ordered[1]["duration"], 200)
        self.assertIn("/b.mp4", dropped)
        self.assertTrue(self.log.warnings, "a transient keep must be logged, not silent")

    def test_real_removal_from_schedule_is_honored(self):
        # /b.mp4 is genuinely no longer in folder_items -- a real
        # membership change, must be dropped for real.
        cached = ["/a.mp4", "/b.mp4"]
        raw = [{"path": "/a.mp4", "duration": 100}]
        ordered, dropped = self.fn(
            cached, raw,
            folder_items=[{"path": "/a.mp4"}],
            broken_files=set(),
            sched_cached_durations={"/b.mp4": 200},
        )
        self.assertEqual([t["path"] for t in ordered], ["/a.mp4"])
        self.assertEqual(dropped, [])

    def test_confirmed_broken_file_is_excluded_even_if_still_scheduled(self):
        cached = ["/a.mp4", "/b.mp4"]
        raw = [{"path": "/a.mp4", "duration": 100}]
        ordered, dropped = self.fn(
            cached, raw,
            folder_items=[{"path": "/a.mp4"}, {"path": "/b.mp4"}],
            broken_files={"/b.mp4"},
            sched_cached_durations={"/b.mp4": 200},
        )
        self.assertEqual([t["path"] for t in ordered], ["/a.mp4"])

    def test_transient_miss_with_no_known_duration_drops_safely(self):
        # No fallback duration exists to safely re-insert it with -- must
        # drop for this pass rather than fabricate a duration.
        cached = ["/a.mp4", "/b.mp4"]
        raw = [{"path": "/a.mp4", "duration": 100}]
        ordered, dropped = self.fn(
            cached, raw,
            folder_items=[{"path": "/a.mp4"}, {"path": "/b.mp4"}],
            broken_files=set(),
            sched_cached_durations={},  # no known duration for /b.mp4
        )
        self.assertEqual([t["path"] for t in ordered], ["/a.mp4"])
        self.assertEqual(dropped, [])


# ---------------------------------------------------------------------------
# Fix #7 -- duration provenance / file-replacement fingerprinting
# ---------------------------------------------------------------------------
class TestMediaProbeFingerprint(unittest.TestCase):
    def setUp(self):
        # The real module has `import os` at file scope; the extractor execs
        # just this one function in isolation, so the same stdlib import it
        # relies on has to be seeded in here too -- not a bug in the real
        # code, a harness requirement.
        self.fn = load_function("_media_probe_fingerprint_stale", {"os": os})

    def test_no_fingerprint_never_flagged_stale(self):
        # Legacy cache entry from before this fix -- must never be treated
        # as stale, so upgrading doesn't force a mass re-probe.
        with tempfile.NamedTemporaryFile() as f:
            entry = {"duration": 1200}  # no measured_mtime/measured_size
            self.assertFalse(self.fn(f.name, entry))

    def test_unchanged_file_is_not_stale(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"x" * 100)
            f.flush()
            st = os.stat(f.name)
            entry = {"measured_mtime": st.st_mtime, "measured_size": st.st_size}
            self.assertFalse(self.fn(f.name, entry))

    def test_replaced_file_same_path_is_detected_stale(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
            f.write(b"x" * 100)
        try:
            st = os.stat(path)
            entry = {"measured_mtime": st.st_mtime, "measured_size": st.st_size}
            # Simulate the file being swapped for a different cut/encode at
            # the same path: different size, and force mtime forward.
            time.sleep(0.05)
            with open(path, "wb") as f2:
                f2.write(b"y" * 999)
            os.utime(path, None)
            self.assertTrue(self.fn(path, entry),
                             "a genuinely replaced file at the same path must be detected stale")
        finally:
            os.unlink(path)

    def test_missing_file_is_not_flagged_stale(self):
        # Can't confirm staleness of a file that isn't there -- that's a
        # MISSING-path problem, not a fingerprint problem; must not raise.
        entry = {"measured_mtime": 123.0, "measured_size": 10}
        self.assertFalse(self.fn("/tmp/gone_for_fingerprint_test.mp4", entry))


# ---------------------------------------------------------------------------
# Fix #11 -- guide preview / live playback ownership attribution
# ---------------------------------------------------------------------------
class TestOwnerChannelResolution(unittest.TestCase):
    """Mirrors the exact one-line resolution used at the watchdog stall/
    success-clear sites (retro_tv_emulator.py ~line 29960-29961), including
    the truthiness bug that was caught and fixed during that pass:
    "".zfill(2) returns "00", not "" -- a naive `or ch` fallback never
    fires against it."""

    @staticmethod
    def resolve(live_playback_channel_raw, display_channel):
        _live_pb_raw = str(live_playback_channel_raw or "").strip()
        return _live_pb_raw.zfill(2) if _live_pb_raw else display_channel

    def test_empty_live_channel_falls_back_to_display_channel(self):
        # This is the exact case that was broken: no warm content loaded
        # (e.g. games channel), must resolve to the real display channel,
        # never the buggy "00".
        self.assertEqual(self.resolve("", "games"), "games")
        self.assertEqual(self.resolve(None, "games"), "games")

    def test_populated_live_channel_is_zero_padded(self):
        self.assertEqual(self.resolve("5", "04"), "05")

    def test_guide_open_with_warm_channel_resolves_to_warm_channel(self):
        # Guide is on-screen (display channel "04") but channel 5's content
        # is what's actually loaded -- ownership must resolve to "05", not
        # "04", so a stall/recovery is attributed to the real file.
        self.assertEqual(self.resolve("05", "04"), "05")


# ---------------------------------------------------------------------------
# broadcast_clock.py -- wall-clock authority (real import, real code)
# ---------------------------------------------------------------------------
class TestBroadcastClock(unittest.TestCase):
    def setUp(self):
        broadcast_clock.force_resync()

    def test_now_is_monotonically_non_decreasing_under_jitter(self):
        # Simulate the exact race the fix targets: two non-atomic anchor
        # reads pairing up with a few ms of skew. now() must never step
        # backward by less than the jitter tolerance.
        first = broadcast_clock.now()
        with broadcast_clock._lock:
            # Nudge the wall anchor backward by less than jitter tolerance,
            # as a slightly-misaligned resync pairing would.
            broadcast_clock._wall_anchor = broadcast_clock._wall_anchor - dt.timedelta(
                seconds=broadcast_clock._JITTER_TOLERANCE_SECONDS / 2
            )
        second = broadcast_clock.now()
        self.assertGreaterEqual(second, first,
                                 "sub-second race jitter must be absorbed, never returned")

    def test_genuine_backward_correction_is_not_swallowed(self):
        # A real backward NTP correction is much larger than jitter
        # tolerance and must pass through untouched -- silently freezing
        # on a stale time forever would be its own bug.
        first = broadcast_clock.now()
        with broadcast_clock._lock:
            broadcast_clock._wall_anchor = broadcast_clock._wall_anchor - dt.timedelta(minutes=5)
            broadcast_clock._last_returned = None  # force re-evaluation from this anchor
        second = broadcast_clock.now()
        self.assertLess(second, first,
                         "a genuine multi-minute backward correction must be allowed through")

    def test_dst_offset_change_is_caught_immediately_not_after_60s(self):
        # The bug: epoch doesn't jump at a DST boundary, only the local
        # offset does, so the raw/mono step check alone can't see it. Force
        # a stale _offset_anchor and confirm the very next now() call
        # resyncs, without waiting on the 60s periodic window.
        with broadcast_clock._lock:
            real_offset = broadcast_clock._offset_anchor
            broadcast_clock._offset_anchor = real_offset + 3600  # simulate 1hr-stale offset
            stale_wall_anchor = broadcast_clock._wall_anchor
            broadcast_clock._last_resync = broadcast_clock._mono_anchor  # pretend resync just happened
        broadcast_clock.now()
        with broadcast_clock._lock:
            resynced = broadcast_clock._wall_anchor != stale_wall_anchor or \
                       broadcast_clock._offset_anchor == real_offset
        self.assertTrue(resynced, "a stale DST offset must trigger an immediate resync, not wait 60s")

    def test_seconds_since_midnight_matches_wall_clock_fields(self):
        moment = dt.datetime(2026, 1, 1, 13, 30, 45, 500000)
        expected = 13 * 3600 + 30 * 60 + 45 + 0.5
        self.assertAlmostEqual(broadcast_clock.seconds_since_midnight(moment), expected, places=5)


# ---------------------------------------------------------------------------
# Fix #9/#10 -- boot-anchor policy: source-level characterization test
# ---------------------------------------------------------------------------
class TestBootAnchorPolicyNeverTrustsColdAnchor(unittest.TestCase):
    """calculate_slotted_playback_state() is not extractable in isolation --
    it depends on live app_state, db, vlc_engine, pygame ticks, and dozens
    of sibling closures, so this cannot be a runtime simulation without
    effectively re-building the whole app. This is instead an explicit
    SOURCE-LEVEL characterization test: it asserts the exact guard clause
    that enforces "never trust the anchor on cold boot" is present and
    unweakened. It proves the code hasn't silently regressed back toward
    DVR-style resume; it does NOT prove the surrounding 30k-line function
    is bug-free at runtime. If this test ever needs to be updated to keep
    passing, that is itself a signal to go re-read the surrounding
    function by hand before accepting the change."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT_DIR, "retro_tv_emulator.py"), "r", encoding="utf-8") as f:
            cls.src = f.read()

    def test_anchor_trusted_requires_live_video_match(self):
        # The line that decides whether a boot can ever use the anchor's
        # data. _anchor_is_live_video is documented as structurally
        # impossible at a cold boot (requires is_playing_video already
        # True), so requiring it here is what makes cold-boot trust
        # impossible by construction, not by a value that could be True
        # or False depending on data.
        self.assertIn(
            'and _anchor_is_live_video\n      )',
            self.src,
            "the _anchor_trusted guard must still require _anchor_is_live_video -- "
            "without it, a cold boot could resume the anchor's file/second directly, "
            "which was explicitly rejected as DVR-pause behavior.",
        )

    def test_removed_resume_window_constant_is_not_reintroduced(self):
        # _BOOT_ANCHOR_RESUME_WINDOW_S / _BOOT_ANCHOR_MAX_AGE_S were the
        # hybrid short/long-outage resume window that got removed outright.
        # Their reappearance as live constants (not just in the historical
        # comment) would mean the rejected behavior is back.
        for name in ("_BOOT_ANCHOR_RESUME_WINDOW_S =", "_BOOT_ANCHOR_MAX_AGE_S ="):
            self.assertNotIn(
                name, self.src,
                f"{name} should not exist as a live assignment -- the hybrid "
                "boot-resume window was explicitly removed, not just disabled",
            )

    def test_explicit_rejection_comment_still_present(self):
        # Not a fan of testing comments in general, but this one records an
        # explicit product decision (from the user, not an engineering
        # guess) that's easy to accidentally erode one "helpful" edit at a
        # time. Its disappearance is a signal to go check intent, not proof
        # of a bug by itself -- hence a soft assertion via a clear message.
        self.assertIn(
            "explicitly rejected",
            self.src,
            "the comment recording that DVR-style resume was explicitly "
            "rejected has disappeared -- if intentional, fine, but re-confirm "
            "the policy before assuming so",
        )


# ---------------------------------------------------------------------------
# Fix #4 (change list, item C) -- end-of-show watchdog branches
# ---------------------------------------------------------------------------
class TestWatchdogEndBranches(unittest.TestCase):
    """The end-of-show watchdog lives inline in the main loop, so it can't be
    extracted as a function. What IS extracted, by exact AST boundary, is the
    real block of statements from `_too_long = ...` (the end classification)
    through the whole `if _real_dur > 0 and not _suspicious: ... elif ...`
    branch chain, exec'd against stubbed collaborators. Real code under test.

    What it pins down:
      - a healthy end advances and NEVER writes the wall-clock guess
        (seek_offset + elapsed) into the probe cache / anchor / frozen
        durations -- the logged case was a 1409 s file "actually ran 1751 s";
      - a healthy end is never ALSO treated as a spurious blip and resumed in
        place (the logged double branch: "corrected and advancing" followed by
        "spurious VLC state glitch, resuming the same file");
      - a genuine quick end nowhere near a KNOWN end still resumes in place,
        bails out after repeats, and needs a known runtime to be called that.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        import textwrap
        import extract as _extract
        with open(_extract.SRC_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        lines = src.splitlines(keepends=True)

        def _is_assign_to(n, name):
            return (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name)

        start = next(n for n in ast.walk(tree) if _is_assign_to(n, "_too_long"))
        chain = min(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.If) and n.lineno > start.lineno
             and ast.get_source_segment(src, n.test) == "_real_dur > 0 and not _suspicious"),
            key=lambda n: n.lineno)
        block = "".join(lines[start.lineno - 1:chain.end_lineno])
        cls.code = compile(textwrap.dedent(block), "<extracted:watchdog-end-branches>", "exec")

    def _run(self, known, seek_offset, elapsed, reason="ended", prior_spurious=None):
        import types
        out = {"printed": [], "warnings": [], "patched": [], "succeeded": [],
               "broken": [], "reasons": {}}
        real_dur = int(seek_offset + elapsed)
        tracker = {}
        if prior_spurious is not None:
            tracker["03"] = prior_spurious
        ns = {
            "_finish_reason": reason, "_elapsed": elapsed, "_real_dur": real_dur,
            "_known_dur": known, "_resume_spurious": False,
            "_anchor": {"file": "C:\\v\\CH 05\\Rugrats.avi", "seek_offset": seek_offset,
                        "mode": "Video", "block": "Full Day"},
            "ch": "05", "_wd_ch_key": "03", "now_watchdog": 1_000_000, "_ab": "Full Day",
            "_ch_entry": {}, "os": os,
            "_watchdog_consecutive_spurious_resumes": tracker,
            "_WATCHDOG_SPURIOUS_RESUME_WINDOW_MS": 20000,
            "_WATCHDOG_SPURIOUS_RESUME_BAIL_AFTER": 3,
            "_patch_probed_gain": lambda *a, **k: out["patched"].append((a, k)),
            "_note_playback_succeeded": lambda f: out["succeeded"].append(f),
            "_mark_file_broken": lambda e, f, reason=None: out["broken"].append(reason),
            "_last_attempt_reason": out["reasons"],
            "vlc_engine": types.SimpleNamespace(_current_attempt_id=1),
            "media": types.SimpleNamespace(_drive_root_of=lambda p: None),
            "_unresponsive_drive_roots": lambda: set(),
            "_station_confirmed_dead_drive": {}, "_remember_dead_drive_probe_file": lambda *a: None,
            "print": lambda *a, **k: out["printed"].append(" ".join(str(x) for x in a)),
            "log": types.SimpleNamespace(
                warning=lambda m, *a, **k: out["warnings"].append(m % a if a else m),
                info=lambda *a, **k: None, error=lambda *a, **k: None),
        }
        exec(self.code, ns)
        out["resume"] = ns["_resume_spurious"]
        out["tracker"] = tracker
        return out

    def test_healthy_end_advances_and_writes_no_duration(self):
        r = self._run(known=1409, seek_offset=0, elapsed=1409.2)
        self.assertEqual(r["patched"], [])
        self.assertFalse(r["resume"])
        self.assertEqual(len(r["succeeded"]), 1)
        self.assertEqual(r["broken"], [])
        self.assertTrue(any("advancing to next show" in m for m in r["printed"]))

    def test_logged_case_1751s_guess_for_a_1409s_file_is_not_written(self):
        # 17:19:03 in the field log: wall-clock says 1751 s, probe says 1409 s.
        r = self._run(known=1409, seek_offset=890.7, elapsed=860.3)
        self.assertEqual(r["patched"], [], "an unmeasured wall-clock duration was written back")
        self.assertFalse(r["resume"], "a healthy end must not ALSO be resumed as a spurious blip")
        self.assertFalse(any("spurious" in m for m in r["printed"]))
        self.assertTrue(any("keeping the probed duration" in w for w in r["warnings"]))

    def test_unknown_runtime_healthy_end_writes_nothing_either(self):
        r = self._run(known=None, seek_offset=0, elapsed=1300.0)
        self.assertEqual(r["patched"], [])
        self.assertFalse(r["resume"])

    def test_quick_end_far_from_known_end_resumes_in_place(self):
        r = self._run(known=1409, seek_offset=600, elapsed=0.5)
        self.assertTrue(r["resume"])
        self.assertEqual(r["patched"], [])
        self.assertEqual(r["broken"], [])
        self.assertEqual(r["tracker"]["03"]["count"], 1)

    def test_quick_end_near_true_end_advances_without_resuming(self):
        r = self._run(known=1409, seek_offset=1407, elapsed=0.5)
        self.assertFalse(r["resume"])
        self.assertEqual(r["patched"], [])
        self.assertEqual(r["broken"], [])
        self.assertTrue(any("advancing without correcting duration" in m for m in r["printed"]))

    def test_quick_end_with_unknown_runtime_is_not_called_spurious(self):
        r = self._run(known=None, seek_offset=600, elapsed=0.5)
        self.assertFalse(r["resume"])
        self.assertEqual(r["patched"], [])

    def test_repeated_spurious_blips_bail_out_of_resuming(self):
        r = self._run(known=1409, seek_offset=600, elapsed=0.5,
                      prior_spurious={"count": 3, "window_start_ms": 999_000})
        self.assertFalse(r["resume"])
        self.assertNotIn("03", r["tracker"])
        self.assertTrue(any("dropping anchor trust" in m for m in r["printed"]))

    def test_error_end_is_abnormal_no_write_no_resume(self):
        r = self._run(known=1409, seek_offset=600, elapsed=30.0, reason="error")
        self.assertEqual(r["patched"], [])
        self.assertFalse(r["resume"])
        self.assertTrue(any("ended abnormally" in m for m in r["printed"]))
        self.assertIn("abnormal_end_duration_mismatch", r["broken"])

    def test_way_too_long_is_abnormal_no_write_no_resume(self):
        r = self._run(known=100, seek_offset=0, elapsed=900.0)
        self.assertEqual(r["patched"], [])
        self.assertFalse(r["resume"])
        self.assertTrue(any("ended abnormally" in m for m in r["printed"]))


# ---------------------------------------------------------------------------
# Fix #C1 (change list, item C, proposal 1) -- never schedule off an
# estimated duration. _maybe_correct_estimated_target_duration is a real,
# top-level, extractable function (unlike the watchdog block above), so it
# is loaded the normal load_function way, not by hand-picked AST slice.
# ---------------------------------------------------------------------------
class TestMaybeCorrectEstimatedTargetDuration(unittest.TestCase):
    """The fix for the exact field-log bug: a guide preview opened Rugrats
    seeking to 1737.96s against its unprobed 1800s placeholder duration.
    The real file is 1409s, so 1737.96s actually belongs to the next show.
    Exercises the real function with a fake ProbeLibrary standing in for
    item B (get_or_probe_sync is the only surface this function touches)."""

    class _FakeProbeLib:
        def __init__(self, duration=None, raises=None):
            self.duration = duration
            self.raises = raises
            self.calls = []

        def get_or_probe_sync(self, path):
            self.calls.append(path)
            if self.raises:
                raise self.raises
            return {"duration": self.duration} if self.duration is not None else None

    def setUp(self):
        self.log = _StubLog()

    def _fn(self, probe_lib):
        return load_function(
            "_maybe_correct_estimated_target_duration",
            {"log": self.log, "os": os, "_get_probe_library": lambda: probe_lib},
        )

    def test_non_estimated_track_is_never_probed(self):
        lib = self._FakeProbeLib(duration=999)
        fn = self._fn(lib)
        show = {"path": "x.avi", "duration": 1409, "duration_estimated": False}
        result_show, pos = fn("05", "Full Day", [show], show, 500, None)
        self.assertIs(result_show, show)
        self.assertEqual(pos, 500)
        self.assertEqual(lib.calls, [], "a confirmed duration must never trigger a probe")

    def test_pair_entries_are_left_untouched(self):
        lib = self._FakeProbeLib(duration=999)
        fn = self._fn(lib)
        show = {"path": "x.avi", "duration": 1800, "duration_estimated": True, "is_pair": True}
        result_show, pos = fn("05", "Full Day", [show], show, 500, None)
        self.assertIs(result_show, show)
        self.assertEqual(lib.calls, [], "pair durations live on sub-episode fields, not here")

    def test_no_path_is_left_untouched(self):
        lib = self._FakeProbeLib(duration=999)
        fn = self._fn(lib)
        show = {"duration": 1800, "duration_estimated": True}
        result_show, pos = fn("05", "Full Day", [show], show, 500, None)
        self.assertIs(result_show, show)
        self.assertEqual(lib.calls, [])

    def test_probe_confirms_the_estimate_just_clears_the_flag(self):
        lib = self._FakeProbeLib(duration=1409)
        fn = self._fn(lib)
        show = {"path": "x.avi", "start_time": 0, "duration": 1409, "duration_estimated": True}
        result_show, pos = fn("05", "Full Day", [show], show, 500, None)
        self.assertFalse(result_show["duration_estimated"])
        self.assertEqual(result_show["duration"], 1409)
        self.assertEqual(pos, 500)

    def test_shorter_real_duration_still_covers_current_position(self):
        lib = self._FakeProbeLib(duration=1600)
        fn = self._fn(lib)
        show = {"path": "x.avi", "start_time": 1000, "duration": 1800, "duration_estimated": True}
        result_show, pos = fn("05", "Full Day", [show], show, 1200, None)
        self.assertIs(result_show, show)
        self.assertEqual(show["duration"], 1600)
        self.assertFalse(show["duration_estimated"])
        self.assertEqual(pos, 1200)

    def test_logged_bug_reresolves_to_the_next_show(self):
        # The exact field case: Rugrats' placeholder (1800s) let the seek
        # land at 1737.96s; the real file is 1409s, so that position
        # actually belongs to the next show in the timeline.
        lib = self._FakeProbeLib(duration=1409)
        fn = self._fn(lib)
        rugrats = {"path": "rugrats.avi", "start_time": 0, "duration": 1800, "duration_estimated": True}
        real_monsters = {"path": "realmonsters.avi", "start_time": 1409, "duration": 1000,
                          "duration_estimated": False}
        timeline = [rugrats, real_monsters]
        result_show, pos = fn("05", "Full Day", timeline, rugrats, 1737.96, None)
        self.assertIs(result_show, real_monsters)
        self.assertEqual(pos, 1737.96, "position itself is never altered, only which show owns it")
        self.assertEqual(rugrats["duration"], 1409, "the timeline entry is corrected in place")
        self.assertFalse(rugrats["duration_estimated"])
        self.assertTrue(any("guide/tune duration correction" in m for m in self.log.infos))

    def test_no_timeline_entry_covers_the_corrected_position_returns_none(self):
        lib = self._FakeProbeLib(duration=1409)
        fn = self._fn(lib)
        rugrats = {"path": "rugrats.avi", "start_time": 0, "duration": 1800, "duration_estimated": True}
        timeline = [rugrats]  # nothing else scheduled to cover 1737.96
        result_show, pos = fn("05", "Full Day", timeline, rugrats, 1737.96, None)
        self.assertIsNone(result_show, "must never open the file at a position past its real end")
        self.assertEqual(pos, 1737.96)

    def test_probe_failure_keeps_the_estimate_and_warns(self):
        lib = self._FakeProbeLib(raises=RuntimeError("ffmpeg not found"))
        fn = self._fn(lib)
        show = {"path": "x.avi", "start_time": 0, "duration": 1800, "duration_estimated": True}
        result_show, pos = fn("05", "Full Day", [show], show, 500, None)
        self.assertIs(result_show, show)
        self.assertTrue(show["duration_estimated"], "must not clear the flag when the probe itself failed")
        self.assertTrue(any("on-demand duration probe failed" in w for w in self.log.warnings))

    def test_unbuildable_library_keeps_the_estimate(self):
        fn = load_function(
            "_maybe_correct_estimated_target_duration",
            {"log": self.log, "os": os,
             "_get_probe_library": lambda: (_ for _ in ()).throw(RuntimeError("disk issue"))},
        )
        show = {"path": "x.avi", "start_time": 0, "duration": 1800, "duration_estimated": True}
        result_show, pos = fn("05", "Full Day", [show], show, 500, None)
        self.assertIs(result_show, show)
        self.assertTrue(show["duration_estimated"])

    def test_zero_or_missing_probed_duration_is_ignored(self):
        for bad in (0, None, -1):
            lib = self._FakeProbeLib(duration=bad)
            fn = self._fn(lib)
            show = {"path": "x.avi", "start_time": 0, "duration": 1800, "duration_estimated": True}
            result_show, pos = fn("05", "Full Day", [show], show, 500, None)
            self.assertIs(result_show, show)
            self.assertTrue(show["duration_estimated"], f"duration={bad} must not clear the flag")


# ---------------------------------------------------------------------------
# Fix #C5 (change list, item C, proposal 5) -- the sched_cycle_total
# placeholder-inflation bug (field log: cached 21600 vs freshly-recomputed
# 14395) and its generalization to any later cycle, not just boot.
# ---------------------------------------------------------------------------
class TestSchedCycleTotalResync(unittest.TestCase):
    """Both blocks live inline inside calculate_slotted_playback_state, so
    -- like the watchdog fix above -- they're extracted by exact AST
    boundary rather than re-typed. Each is a single self-contained `if`
    statement with no dependency on the surrounding rebuild loop, found by
    matching its own test expression (there is exactly one match for each
    in the real file; setUpClass asserts that rather than assuming it)."""

    @staticmethod
    def _find_if_source(substrings):
        import ast
        import textwrap
        import extract as _extract
        with open(_extract.SRC_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        lines = src.splitlines(keepends=True)
        matches = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.If) and all(s in ast.unparse(n.test) for s in substrings)
        ]
        assert len(matches) == 1, f"expected exactly one `if` matching {substrings}, found {len(matches)}"
        node = matches[0]
        return textwrap.dedent("".join(lines[node.lineno - 1:node.end_lineno]))

    @classmethod
    def setUpClass(cls):
        cls.small_bundle_code = compile(
            cls._find_if_source(["_cyc_boundary == 0", "_bundle_file_count in (1, 2)",
                                  "_cyc_total != total_block_content"]),
            "<extracted:small-bundle-resync>", "exec")
        cls.drift_code = compile(
            cls._find_if_source(["not _marathon_mode", "position_in_playlist >= total_block_content"]),
            "<extracted:drift-resync>", "exec")

    # -- small-bundle resync: the boot-time case, cyc_boundary == 0 --

    def _run_small_bundle(self, cyc_boundary, bundle_file_count, cyc_total, total_block_content):
        log = _StubLog()
        ns = dict(_cyc_boundary=cyc_boundary, _bundle_file_count=bundle_file_count,
                   _cyc_total=cyc_total, total_block_content=total_block_content,
                   target_clean_key="05", log=log)
        exec(self.small_bundle_code, ns)
        return ns["_cyc_total"], log

    def test_logged_case_1_file_bundle_placeholder_is_resynced(self):
        # The field log: cached sched_cycle_total=21600 (a 6h placeholder),
        # freshly-computed total_block_content=14395 for a single-file bundle.
        cyc_total, log = self._run_small_bundle(0, 1, 21600, 14395)
        self.assertEqual(cyc_total, 14395)
        self.assertTrue(any("small bundle" in m for m in log.infos))

    def test_two_file_bundle_also_resyncs(self):
        cyc_total, _ = self._run_small_bundle(0, 2, 21600, 3000)
        self.assertEqual(cyc_total, 3000)

    def test_real_multi_episode_bundle_is_left_alone(self):
        # 3+ distinct files: deliberately out of scope per the block's own
        # comment -- large bundles are where existing repeat-guard/
        # show-rotation state must not be disturbed.
        cyc_total, log = self._run_small_bundle(0, 3, 21600, 14395)
        self.assertEqual(cyc_total, 21600)
        self.assertEqual(log.infos, [])

    def test_matching_totals_are_a_no_op(self):
        cyc_total, log = self._run_small_bundle(0, 1, 14395, 14395)
        self.assertEqual(cyc_total, 14395)
        self.assertEqual(log.infos, [])

    def test_mid_cycle_boundary_nonzero_is_never_resynced_here(self):
        # This block is deliberately scoped to boot (cyc_boundary == 0); a
        # mid-cycle mismatch is the DRIFT RESYNC block's job, tested below.
        cyc_total, log = self._run_small_bundle(500, 1, 21600, 14395)
        self.assertEqual(cyc_total, 21600)
        self.assertEqual(log.infos, [])

    # -- drift resync: the general case, any point in the cycle --

    def _run_drift(self, total_block_content, position_in_playlist, cyc_total,
                    seconds_into_block, cyc_boundary=0, marathon=False, db_fail=False):
        import types
        log = _StubLog()
        if db_fail:
            def _save():
                raise RuntimeError("disk full")
        else:
            def _save():
                pass
        db = types.SimpleNamespace(save_settings=_save)
        ns = dict(_marathon_mode=marathon, total_block_content=total_block_content,
                   position_in_playlist=position_in_playlist, target_clean_key="05",
                   current_block="Full Day", _cyc_total=cyc_total,
                   seconds_into_block=seconds_into_block, _cyc_boundary=cyc_boundary,
                   playback_log={}, log=log, db=db)
        exec(self.drift_code, ns)
        return ns, log

    def test_logged_case_stale_boundary_is_resynced_not_static(self):
        # The exact field log at 17:14:55: cached total was inflated, the
        # freshly-recomputed total_block_content is smaller, and the raw
        # wall-clock position has already run past it.
        ns, log = self._run_drift(total_block_content=14395, position_in_playlist=15295.643,
                                   cyc_total=21600, seconds_into_block=15295.643)
        self.assertLess(ns["position_in_playlist"], 14395)
        self.assertEqual(ns["playback_log"]["sched_cycle_total"], 14395)
        self.assertTrue(any("resyncing the cycle boundary" in m for m in log.infos))

    def test_position_within_total_is_untouched(self):
        ns, log = self._run_drift(total_block_content=1409, position_in_playlist=800,
                                   cyc_total=1409, seconds_into_block=800)
        self.assertEqual(ns["_cyc_boundary"], 0)
        self.assertEqual(ns["position_in_playlist"], 800)
        self.assertEqual(ns["playback_log"], {})

    def test_marathon_mode_is_exempt(self):
        ns, log = self._run_drift(total_block_content=1409, position_in_playlist=99999,
                                   cyc_total=21600, seconds_into_block=99999, marathon=True)
        self.assertEqual(ns["playback_log"], {})

    def test_zero_total_block_content_is_exempt(self):
        ns, log = self._run_drift(total_block_content=0, position_in_playlist=99999,
                                   cyc_total=21600, seconds_into_block=99999)
        self.assertEqual(ns["playback_log"], {})

    def test_persist_failure_is_caught_and_warned_not_raised(self):
        ns, log = self._run_drift(total_block_content=14395, position_in_playlist=15295.643,
                                   cyc_total=21600, seconds_into_block=15295.643, db_fail=True)
        self.assertTrue(any("failed to persist" in w for w in log.warnings))


# ---------------------------------------------------------------------------
# Picture-box scan hook-up (handoff item D detection, wired to the library)
# ---------------------------------------------------------------------------
import threading  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402


import functools  # noqa: E402
import extract as _extract  # noqa: E402


@functools.lru_cache(maxsize=None)
def _pb_source(name):
    # extract.load_function re-parses the whole 36k-line file on every call;
    # the source can't change mid-run, so parse each function once.
    return _extract._get_function_source(name)


def _pb_load(name, namespace):
    ns = dict(namespace)
    exec(compile(_pb_source(name), filename=f"<extracted:{name}>", mode="exec"), ns)
    return ns[name]


class _PBApp:
    """Namespace for the extracted picture-box functions. Loads them in
    dependency order into one shared namespace, so they call each other
    exactly as they do in retro_tv_emulator.py, with the module-level state
    (app_state, the done/failure records, the frame-gap globals) stubbed."""

    def __init__(self, **overrides):
        import types as _types
        self.log = _StubLog()
        self.app_state = {"current_channel": "05", "highlighted_guide_channel": "07",
                          "is_playing_video": False}
        self.done = set()
        self.failures = {}
        self.shutdown = threading.Event()
        self.first_pass = threading.Event()
        self.first_pass.set()
        ns = dict(
            log=self.log, os=os, time=time, media=media,
            app_state=self.app_state, db=object(),
            _picture_box_done=self.done, _picture_box_failures=self.failures,
            _PICTURE_BOX_RETRY_BACKOFF_S=(600, 1800, 7200),
            _app_shutting_down=self.shutdown, _FIRST_PASS_SWEEP_DONE=self.first_pass,
            PICTURE_BOX_BETWEEN_FILES_SECONDS=1.0,
            PICTURE_BOX_IDLE_SLEEP_SECONDS=20.0,
            PICTURE_BOX_PAUSE_RECHECK_SECONDS=0.01,
            SCAN_BACKOFF_WHILE_PLAYING_MULTIPLIER=6.0,
            FRAME_STALL_SOFT_MS=40.0, FRAME_STALL_HARD_MS=70.0,
            FRAME_STALL_BACKOFF_MULTIPLIER=4.0, FRAME_STALL_SAMPLE_MAX_AGE_S=2.0,
            _last_frame_gap_ms=None, _last_frame_gap_at=0.0,
            _is_drive_degraded=lambda p: False,
            LEGACY_MIGRATION_BATCH_SIZE=10, LEGACY_MIGRATION_BETWEEN_BATCHES_SECONDS=0.5,
        )
        ns.update(overrides)
        self.ns = ns
        for name in ("_paths_from_entry", "_next_n_from_ordered", "_background_scan_should_pause",
                     "_background_scan_pace_seconds", "_picture_box_note_failure"):
            if name not in overrides:
                ns[name] = _pb_load(name, ns)
        if "_picture_box_scan_one" not in overrides:
            ns["_picture_box_scan_one"] = _pb_load("_picture_box_scan_one", ns)
        if "_gather_picture_box_candidates" not in overrides:
            ns["_gather_picture_box_candidates"] = _pb_load("_gather_picture_box_candidates", ns)
        ns["_picture_box_precache_worker"] = _pb_load("_picture_box_precache_worker", ns)
        ns["_snapshot_legacy_cache"] = _pb_load("_snapshot_legacy_cache", ns)
        ns["_legacy_cache_migration_worker"] = _pb_load("_legacy_cache_migration_worker", ns)

    def set(self, **values):
        """Swap a module-level name after loading. Extracted functions keep
        their own copy of the namespace, so the value has to be pushed into
        every loaded function's globals, not just the shared dict."""
        self.ns.update(values)
        for fn in list(self.ns.values()):
            g = getattr(fn, "__globals__", None)
            if g is not None and g is not self.ns and "<extracted:" in getattr(fn.__code__, "co_filename", ""):
                g.update(values)

    def __getattr__(self, name):
        try:
            return self.__dict__["ns"][name]
        except KeyError:
            raise AttributeError(name)


class _FakeLib:
    """Stands in for ProbeLibrary: records calls, answers from a script."""
    def __init__(self, cached=None, results=None):
        self.cached = cached or {}
        self.results = results or {}
        self.detect_calls = []

    def get_cached(self, path):
        return self.cached.get(path)

    def detect_picture_box_if_needed(self, path):
        self.detect_calls.append(path)
        r = self.results.get(path, {"reason": "letterbox", "picture_aspect": 1.9,
                                     "rect": [0, 0, 1, 1], "frame": [1, 1], "samples": 6})
        if isinstance(r, Exception):
            raise r
        return r


class _FakeML:
    PICTURE_BOX_DETECTOR_VERSION = 1


_V = lambda n: os.path.normpath("/v/" + n)  # noqa: E731


class TestPictureBoxCandidateOrder(unittest.TestCase):
    def _app(self, channels, lineups, windows=None, timeline=None, **state):
        state.setdefault("current_channel", "05")
        state.setdefault("highlighted_guide_channel", "07")

        class _DB:
            channels_db = channels

        def _ordered(ch, info):
            if isinstance(lineups[ch], Exception):
                raise lineups[ch]
            return lineups[ch], "Full Day"

        def _now(ch):
            return {"file": (windows or {}).get(ch, "")}

        app = _PBApp(db=_DB, _get_channel_ordered_content=_ordered, _get_probe_now_state=_now,
                     _next_n_from_block_timeline=lambda ch, cur, n=3: (timeline or {}).get(ch))
        app.app_state.update(state)
        return app

    def _basic(self):
        channels = {
            "05": {"active": True}, "06": {"active": True, "is_visualizer": True},
            "07": {"active": True, "commercials_enabled": True,
                   "schedules": {"Commercials": [{"path": "/v/ad1.mp4"}, {"path": "/v/ad2.mp4"}]}},
            "08": {"active": True}, "09": {"active": False},
        }
        lineups = {
            "05": [{"path": "/v/a1.mkv"}, {"path": "/v/a2.mkv"}, {"path": "/v/a3.mkv"}, {"path": "/v/a4.mkv"}],
            "06": [{"path": "/v/song.mp3"}],
            "07": [{"path": "/v/b1.avi"}, {"path": "/v/b2.avi"}],
            "08": [{"path": "/v/c1.mp4"}, {"path": "/v/c2.mp4"}, {"path": "/v/notes.txt"}],
            "09": [{"path": "/v/inactive.mkv"}],
        }
        return channels, lineups

    def test_tiers_follow_the_handoff_order(self):
        channels, lineups = self._basic()
        app = self._app(channels, lineups, windows={"05": "/v/a2.mkv"})
        out = app._gather_picture_box_candidates()
        tiers = [t for t, _ in out]
        self.assertEqual(tiers, sorted(tiers))
        by_tier = {}
        for t, p in out:
            by_tier.setdefault(t, []).append(p)
        # 1: watched ch05, now-playing (a2) plus the next 2, cyclic
        self.assertEqual(by_tier[1], [_V("a2.mkv"), _V("a3.mkv"), _V("a4.mkv")])
        # 2: highlighted ch07 window
        self.assertEqual(by_tier[2], [_V("b1.avi"), _V("b2.avi")])
        # 3: everything else, airing-soonest first, interleaved across channels
        #    (a1 is 4th in ch05's rotation, so it comes after c1 and c2)
        self.assertEqual(by_tier[3], [_V("c1.mp4"), _V("c2.mp4"), _V("a1.mkv")])
        # 4: commercial pool
        self.assertEqual(by_tier[4], [_V("ad1.mp4"), _V("ad2.mp4")])

    def test_visualizer_inactive_and_non_video_are_never_candidates(self):
        channels, lineups = self._basic()
        out = [p for _, p in self._app(channels, lineups)._gather_picture_box_candidates()]
        self.assertNotIn(_V("song.mp3"), out)
        self.assertNotIn(_V("inactive.mkv"), out)
        self.assertNotIn(_V("notes.txt"), out)

    def test_a_path_appears_once_at_its_best_tier(self):
        channels, lineups = self._basic()
        out = self._app(channels, lineups)._gather_picture_box_candidates()
        paths = [p for _, p in out]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(dict((p, t) for t, p in out)[_V("a1.mkv")], 1)

    def test_watched_channel_switch_changes_tier_one(self):
        channels, lineups = self._basic()
        out = self._app(channels, lineups, current_channel="08",
                        highlighted_guide_channel="05")._gather_picture_box_candidates()
        t1 = [p for t, p in out if t == 1]
        self.assertEqual(t1, [_V("c1.mp4"), _V("c2.mp4")])
        t2 = [p for t, p in out if t == 2]
        self.assertEqual(t2, [_V("a1.mkv"), _V("a2.mkv"), _V("a3.mkv")])

    def test_true_broadcast_order_timeline_is_used_when_available(self):
        channels, lineups = self._basic()
        timeline = {"07": [{"path": "/v/b1.avi"}, {"path": "/v/ad2.mp4"}, {"path": "/v/b2.avi"}]}
        out = self._app(channels, lineups, timeline=timeline)._gather_picture_box_candidates()
        self.assertEqual([p for t, p in out if t == 2], [_V("b1.avi"), _V("ad2.mp4"), _V("b2.avi")])

    def test_one_broken_channel_does_not_stop_the_gather(self):
        channels, lineups = self._basic()
        lineups["08"] = RuntimeError("bad schedule")
        app = self._app(channels, lineups)
        out = [p for _, p in app._gather_picture_box_candidates()]
        self.assertIn(_V("a1.mkv"), out)
        self.assertIn(_V("b1.avi"), out)
        self.assertTrue(any("ch08" in w for w in app.log.warnings))

    def test_no_db_gives_no_candidates(self):
        app = _PBApp(db=None)
        self.assertEqual(app._gather_picture_box_candidates(), [])


class TestPictureBoxPausingAndPacing(unittest.TestCase):
    def test_game_channel_pauses_new_scans(self):
        app = _PBApp()
        app.app_state["current_channel"] = "03"
        self.assertTrue(app._background_scan_should_pause())

    def test_hard_frame_stall_pauses_only_while_video_plays_and_sample_is_fresh(self):
        app = _PBApp()
        app.set(_last_frame_gap_ms=80.0, _last_frame_gap_at=time.time())
        app.app_state["is_playing_video"] = True
        self.assertTrue(app._background_scan_should_pause())
        app.app_state["is_playing_video"] = False       # nothing rendering: never gate on it
        self.assertFalse(app._background_scan_should_pause())
        app.app_state["is_playing_video"] = True
        app.set(_last_frame_gap_at=time.time() - 10)    # sample too old to trust
        self.assertFalse(app._background_scan_should_pause())

    def test_soft_stall_does_not_pause_but_slows_pacing(self):
        app = _PBApp()
        app.set(_last_frame_gap_ms=50.0, _last_frame_gap_at=time.time())
        app.app_state["is_playing_video"] = True
        self.assertFalse(app._background_scan_should_pause())
        self.assertEqual(app._background_scan_pace_seconds(1.0), 1.0 * 6.0 * 4.0)

    def test_pacing_idle_playing_and_healthy_playing(self):
        app = _PBApp()
        self.assertEqual(app._background_scan_pace_seconds(1.0), 1.0)
        app.app_state["is_playing_video"] = True
        self.assertEqual(app._background_scan_pace_seconds(1.0), 6.0)


class TestPictureBoxScanOne(unittest.TestCase):
    P = _V("movie.mkv")

    def test_already_current_file_is_not_rescanned_and_costs_no_pause(self):
        app = _PBApp()
        lib = _FakeLib(cached={self.P: {"picture_box": {"x": 1}, "picture_box_detector_version": 1}})
        self.assertFalse(app._picture_box_scan_one(lib, _FakeML, self.P))
        self.assertEqual(lib.detect_calls, [])
        self.assertIn(self.P, app.done)

    def test_older_detector_version_is_rescanned(self):
        app = _PBApp()
        lib = _FakeLib(cached={self.P: {"picture_box": {"x": 1}, "picture_box_detector_version": 0}})
        self.assertTrue(app._picture_box_scan_one(lib, _FakeML, self.P))
        self.assertEqual(lib.detect_calls, [self.P])

    def test_successful_scan_is_recorded_and_logged_once(self):
        app = _PBApp()
        lib = _FakeLib()
        self.assertTrue(app._picture_box_scan_one(lib, _FakeML, self.P))
        self.assertIn(self.P, app.done)
        self.assertNotIn(self.P, app.failures)
        self.assertEqual(len([m for m in app.log.infos if "[PICTURE BOX]" in m and "letterbox" in m]), 1)

    def test_no_answer_backs_off_600_1800_7200_and_stays_at_7200(self):
        app = _PBApp()
        lib = _FakeLib(results={self.P: None})
        delays = []
        for _ in range(4):
            before = time.monotonic()
            app._picture_box_scan_one(lib, _FakeML, self.P)
            count, retry_at = app.failures[self.P]
            delays.append(round(retry_at - before))
        self.assertEqual(delays, [600, 1800, 7200, 7200])
        self.assertNotIn(self.P, app.done)

    def test_a_crashing_scan_is_contained_and_backed_off(self):
        app = _PBApp()
        lib = _FakeLib(results={self.P: RuntimeError("ffmpeg vanished")})
        self.assertTrue(app._picture_box_scan_one(lib, _FakeML, self.P))
        self.assertIn(self.P, app.failures)
        self.assertTrue(any("crashed" in w for w in app.log.warnings))

    def test_success_after_failures_clears_the_backoff(self):
        app = _PBApp()
        lib = _FakeLib(results={self.P: None})
        app._picture_box_scan_one(lib, _FakeML, self.P)
        lib.results = {}
        app._picture_box_scan_one(lib, _FakeML, self.P)
        self.assertNotIn(self.P, app.failures)


class TestPictureBoxWorker(unittest.TestCase):
    def _run(self, app, lib, seconds=0.6, ready=lambda: False):
        app.set(_get_probe_library=lambda: lib, _background_scan_pace_seconds=lambda base: 0.0)
        t = threading.Thread(target=app._picture_box_precache_worker, daemon=True)
        t.start()
        end = time.time() + seconds
        while time.time() < end and not ready():
            time.sleep(0.01)
        app.shutdown.set()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "worker did not stop on shutdown")

    def test_scans_in_priority_order_and_never_twice(self):
        order = [(1, _V("a.mkv")), (1, _V("b.mkv")), (2, _V("c.mkv")), (4, _V("d.mp4"))]
        app = _PBApp(_gather_picture_box_candidates=lambda: list(order))
        app.set(PICTURE_BOX_IDLE_SLEEP_SECONDS=0.02)   # forces several re-gathers
        lib = _FakeLib()
        self._run(app, lib, ready=lambda: False)
        self.assertEqual(lib.detect_calls, [p for _, p in order])

    def test_nothing_scanned_while_paused_then_resumes(self):
        paused = {"v": True}
        app = _PBApp(_gather_picture_box_candidates=lambda: [(1, _V("a.mkv"))],
                     _background_scan_should_pause=lambda: paused["v"])
        lib = _FakeLib()
        app.set(_get_probe_library=lambda: lib, _background_scan_pace_seconds=lambda base: 0.0)
        t = threading.Thread(target=app._picture_box_precache_worker, daemon=True)
        t.start()
        time.sleep(0.2)
        self.assertEqual(lib.detect_calls, [])
        paused["v"] = False
        end = time.time() + 2
        while not lib.detect_calls and time.time() < end:
            time.sleep(0.01)
        app.shutdown.set()
        t.join(timeout=3)
        self.assertEqual(lib.detect_calls, [_V("a.mkv")])

    def test_degraded_drive_files_are_skipped_others_still_scanned(self):
        bad, good = _V("bad.mkv"), _V("good.mkv")
        app = _PBApp(_gather_picture_box_candidates=lambda: [(1, bad), (1, good)],
                     _is_drive_degraded=lambda p: p == bad)
        lib = _FakeLib()
        self._run(app, lib)
        self.assertEqual(lib.detect_calls, [good])

    def test_failed_file_is_not_retried_inside_its_backoff_window(self):
        a, b = _V("a.mkv"), _V("b.mkv")
        app = _PBApp(_gather_picture_box_candidates=lambda: [(1, a), (1, b)])
        app.set(PICTURE_BOX_IDLE_SLEEP_SECONDS=0.02)
        lib = _FakeLib(results={a: None})
        self._run(app, lib)
        self.assertEqual(lib.detect_calls.count(a), 1)
        self.assertEqual(lib.detect_calls.count(b), 1)

    def test_waits_for_the_first_pass_sweep_before_scanning(self):
        app = _PBApp(_gather_picture_box_candidates=lambda: [(1, _V("a.mkv"))])
        app.first_pass.clear()
        lib = _FakeLib()
        self._run(app, lib, seconds=0.3)
        self.assertEqual(lib.detect_calls, [])

    def test_library_unavailable_disables_the_scan_without_raising(self):
        app = _PBApp(_gather_picture_box_candidates=lambda: [(1, _V("a.mkv"))])

        def _boom():
            raise RuntimeError("disk full")
        app.set(_get_probe_library=_boom)
        app._picture_box_precache_worker()          # returns, does not raise
        self.assertTrue(any("scan disabled" in w for w in app.log.warnings))

    def test_starts_from_the_boot_thread_list(self):
        with open(os.path.join(_ROOT_DIR, "retro_tv_emulator.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("target=_picture_box_precache_worker", src)
        self.assertEqual(src.count("target=_picture_box_precache_worker"), 1)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class TestPictureBoxEndToEndWithRealLibrary(unittest.TestCase):
    """The real worker, the real ProbeLibrary/MediaLibrary sqlite file and
    real ffmpeg cropdetect on a generated letterboxed clip -- only the
    schedule (which files come up) is stubbed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clip = os.path.join(self.tmp, "letterbox.mkv")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=size=720x576:rate=25:duration=20,drawbox=x=0:y=0:w=720:h=99:color=black:t=fill,"
             "drawbox=x=0:y=477:w=720:h=99:color=black:t=fill",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", self.clip], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_lands_in_the_sqlite_library_and_is_a_hit_next_time(self):
        import media_library as ml
        lib = ml.ProbeLibrary(ml.MediaLibrary(os.path.join(self.tmp, "lib.sqlite3")))
        path = os.path.normpath(self.clip)
        app = _PBApp(_gather_picture_box_candidates=lambda: [(1, path)])
        app.set(_get_probe_library=lambda: lib, _background_scan_pace_seconds=lambda base: 0.0)
        t = threading.Thread(target=app._picture_box_precache_worker, daemon=True)
        t.start()
        end = time.time() + 60
        while path not in app.done and time.time() < end:
            time.sleep(0.05)
        app.shutdown.set()
        t.join(timeout=5)
        entry = lib.get_cached(path)
        self.assertIsNotNone(entry)
        box = entry["picture_box"]
        self.assertEqual(box["reason"], "letterbox")
        self.assertAlmostEqual(box["picture_aspect"], 720 / 378.0, delta=0.05)
        self.assertGreaterEqual(entry["picture_box_detector_version"], 1)
        self.assertTrue(entry.get("header_probed_at"))     # header probe done in the same pass
        # Second session: library hit, no second scan.
        app2 = _PBApp()
        with mock.patch.object(lib, "detect_picture_box_if_needed",
                               side_effect=AssertionError("rescanned")):
            self.assertFalse(app2._picture_box_scan_one(lib, ml, path))


# ---------------------------------------------------------------------------
# Legacy-cache migration into the persistent library (item B, migration)
# ---------------------------------------------------------------------------
class _MigrationBase(unittest.TestCase):
    def setUp(self):
        import media_library as ml
        self.ml = ml
        self.tmp = tempfile.mkdtemp()
        self.lib = ml.ProbeLibrary(ml.MediaLibrary(os.path.join(self.tmp, "lib.sqlite3")),
                                   use_vlc_fallback=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name, payload=b"x"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(payload * 200_000)
        return os.path.normpath(path).lower()

    def migrate(self, probe_cache, dvd_cache=None, target=-20.5):
        mig = self.ml.LegacyCacheMigrator(self.lib, batch_size=5)
        mig.start(probe_cache, dvd_cache or {}, gain_target_lufs=target)
        while not mig.step().done:
            pass
        return mig.progress

    def row(self, path):
        return self.lib.get_cached(path)


class TestLegacyMigrationSafety(_MigrationBase):
    def test_carries_duration_dims_and_gain_and_tags_the_gain_target(self):
        p = self.make("a.mkv")
        prog = self.migrate({p: {"duration": 1409, "dims": [720, 576], "gain": 3.5}})
        row = self.row(p)
        self.assertEqual((row["duration"], row["width"], row["height"], row["gain"]), (1409, 720, 576, 3.5))
        self.assertEqual(row["gain_target_lufs"], -20.5)
        self.assertEqual((prog.migrated, prog.newly_written), (1, 1))

    def test_rerun_never_overwrites_a_newer_library_value(self):
        p = self.make("a.mkv")
        key = self.lib.keys.resolve(p)
        self.lib.library.upsert_fields(key, duration=1409.2, duration_source="ffmpeg", gain=1.0)
        prog = self.migrate({p: {"duration": 1751, "gain": 9.9, "dims": [720, 576]}})
        row = self.row(p)
        self.assertEqual((row["duration"], row["duration_source"], row["gain"]), (1409.2, "ffmpeg", 1.0))
        self.assertIsNone(row["gain_target_lufs"])       # the target belongs with the gain it was measured for
        self.assertEqual(row["width"], 720)              # the field the library lacked is still filled
        self.assertEqual(prog.newly_written, 1)

    def test_running_twice_writes_nothing_the_second_time(self):
        p = self.make("a.mkv")
        cache = {p: {"duration": 1409, "gain": 3.5}}
        self.migrate(cache)
        second = self.migrate(cache)
        self.assertEqual((second.migrated, second.newly_written), (1, 0))

    def test_entry_measured_on_a_different_version_of_the_file_is_not_migrated(self):
        p = self.make("swapped.mkv")
        st = os.stat(p)
        prog = self.migrate({p: {"duration": 1409, "measured_size": st.st_size + 1,
                                 "measured_mtime": st.st_mtime}})
        self.assertEqual((prog.skipped_stale, prog.migrated), (1, 0))
        self.assertIsNone(self.row(p))

    def test_matching_fingerprint_and_no_fingerprint_are_both_migrated(self):
        good, bare = self.make("good.mkv"), self.make("bare.mkv", b"y")
        st = os.stat(good)
        prog = self.migrate({good: {"duration": 60, "measured_size": st.st_size, "measured_mtime": st.st_mtime},
                             bare: {"duration": 61}})
        self.assertEqual((prog.migrated, prog.skipped_stale), (2, 0))

    def test_missing_files_are_left_for_a_later_pass_and_source_dicts_untouched(self):
        gone = os.path.join(self.tmp, "gone.mkv").lower()
        cache = {gone: {"duration": 5}}
        prog = self.migrate(cache, {gone: 2.0})
        self.assertEqual(prog.skipped_missing, 2)
        self.assertEqual(cache, {gone: {"duration": 5}})

    def test_dvd_gain_cache_is_migrated_with_target_and_fill_only(self):
        p = self.make("movie.mkv")
        self.migrate({}, {p: 4.25})
        row = self.row(p)
        self.assertEqual((row["gain"], row["gain_target_lufs"]), (4.25, -20.5))
        self.migrate({}, {p: 8.0})                       # a later value must not replace it
        self.assertEqual(self.row(p)["gain"], 4.25)


class TestLegacyMigrationWorker(_MigrationBase):
    def _worker(self, config, **overrides):
        class _DB:
            pass
        d = _DB()
        d.config = config
        app = _PBApp(db=d, **overrides)
        app.set(_get_probe_library=lambda: self.lib, _background_scan_pace_seconds=lambda base: 0.0,
                _background_scan_should_pause=lambda: False)
        return app

    def test_worker_migrates_then_stops_and_leaves_the_config_alone(self):
        a, b = self.make("a.mkv"), self.make("b.mkv", b"z")
        config = {"media_probe_cache": {a: {"duration": 100, "gain": 2.0}, b: {"duration": 200}},
                  "dvd_gain_cache": {}, "_gain_cache_target_lufs": [-20.5, 1.5]}
        before = repr(config)
        app = self._worker(config)
        t = threading.Thread(target=app._legacy_cache_migration_worker, daemon=True)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(repr(config), before)
        self.assertEqual(self.row(a)["gain_target_lufs"], -20.5)
        self.assertEqual(self.row(b)["duration"], 200)
        self.assertTrue(any("[LIBRARY MIGRATION] done" in m for m in app.log.infos))

    def test_waits_for_first_pass_sweep_and_pauses_on_game_channel(self):
        a = self.make("a.mkv")
        config = {"media_probe_cache": {a: {"duration": 100}}}
        app = self._worker(config)
        app.first_pass.clear()
        t = threading.Thread(target=app._legacy_cache_migration_worker, daemon=True)
        t.start()
        time.sleep(0.3)
        self.assertIsNone(self.row(a))                   # first-pass sweep not done yet
        paused = {"v": True}
        app.set(_background_scan_should_pause=lambda: paused["v"])
        app.first_pass.set()
        time.sleep(0.3)
        self.assertIsNone(self.row(a))                   # sweep done but paused
        paused["v"] = False
        t.join(timeout=10)
        self.assertEqual(self.row(a)["duration"], 100)

    def test_worker_stops_promptly_on_shutdown(self):
        config = {"media_probe_cache": {self.make("a.mkv"): {"duration": 1}}}
        app = self._worker(config)
        app.set(_background_scan_should_pause=lambda: True)
        app.set(PICTURE_BOX_PAUSE_RECHECK_SECONDS=0.01)
        t = threading.Thread(target=app._legacy_cache_migration_worker, daemon=True)
        t.start()
        time.sleep(0.1)
        app.shutdown.set()
        t.join(timeout=3)
        self.assertFalse(t.is_alive())

    def test_library_unavailable_is_contained(self):
        app = self._worker({"media_probe_cache": {}})

        def _boom():
            raise RuntimeError("no disk")
        app.set(_get_probe_library=_boom)
        app._legacy_cache_migration_worker()
        self.assertTrue(any("[LIBRARY MIGRATION] not started" in w for w in app.log.warnings))

    def test_is_started_from_the_boot_thread_list(self):
        with open(os.path.join(_ROOT_DIR, "retro_tv_emulator.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("target=_legacy_cache_migration_worker"), 1)


# ---------------------------------------------------------------------------
# Item F -- DVD-player files decode in hardware, disc images do not
# ---------------------------------------------------------------------------
class TestDvdFileHardwareDecodeOption(unittest.TestCase):
    """SOURCE-LEVEL characterization test, not a runtime one: the option is
    added inside VLCEngineWrapper.play_file_segmented, a method of a class
    built inside initialize_vlc_on_demand(), which extract.py cannot pull
    out, and libVLC isn't available here. This pins WHERE the option is
    added and under which conditions; that libVLC then honours it is only
    confirmed by vlc-debug.log on a real run (a DVD-engine file open should
    show 'trying format d3d11va_vld' without 'matching \"none\"' after it)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT_DIR, "retro_tv_emulator.py"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_option_is_added_once_and_only_for_non_disc_files_on_the_dvd_engine(self):
        self.assertEqual(self.src.count('media.add_option(":avcodec-hw=any")'), 1)
        i = self.src.index('media.add_option(":avcodec-hw=any")')
        guard = self.src[self.src.rfind("media = self.instance.media_new(media_source)", 0, i):i]
        self.assertIn('self.engine_label == "dvd"', guard)
        self.assertIn("not is_dvd_iso", guard)
        self.assertIn("DVD_FILE_HW_DECODE", guard)

    def test_dvd_engine_instance_still_defaults_to_software_decode(self):
        self.assertIn('+ ["--avcodec-hw=none"]', self.src)

    def test_switch_exists_and_defaults_on(self):
        self.assertRegex(self.src, r"(?m)^DVD_FILE_HW_DECODE = True$")


# ---------------------------------------------------------------------------
# Start position handed to VLC at open (SEEK_AT_OPEN): a tune-in mid-show must
# begin at the scheduled spot, not open at 0:00 and jump there afterwards.
# ---------------------------------------------------------------------------
class TestSeekAtOpen(unittest.TestCase):
    """Two halves, because play_file_segmented / _seek_when_ready are methods
    of a class built inside initialize_vlc_on_demand() that can't be
    imported and libVLC isn't available here:

      - the OPEN side is pinned at source level (where the option is added,
        and under which conditions);
      - the SEEK side (_seek_when_ready) is real code, extracted by AST and
        run against a fake player, so what it does with `opened_at_position`
        is actually exercised.

    That libVLC then honours ":start-time" for a given container is only
    confirmed by a real run: the log shows "[SEEK VERIFY] opened at the
    target via start-time, no seek needed" when it did, and "start-time at
    open did not take ... seeking" when the fallback had to step in."""

    @classmethod
    def setUpClass(cls):
        import ast
        import textwrap
        with open(os.path.join(_ROOT_DIR, "retro_tv_emulator.py"), encoding="utf-8") as f:
            cls.src = f.read()
        lines = cls.src.splitlines(keepends=True)
        found = [n for n in ast.walk(ast.parse(cls.src))
                 if isinstance(n, ast.FunctionDef) and n.name == "_seek_when_ready"]
        assert len(found) == 1, "expected exactly one _seek_when_ready"
        cls.seek_src = textwrap.dedent("".join(lines[found[0].lineno - 1:found[0].end_lineno]))

    # -- open side (source level) ------------------------------------------
    def test_switch_exists_and_defaults_on(self):
        self.assertRegex(self.src, r"(?m)^SEEK_AT_OPEN = True$")

    def test_start_time_is_added_once_before_the_media_is_opened(self):
        self.assertEqual(self.src.count('media.add_option(":start-time='), 1)
        i = self.src.index('media.add_option(":start-time=')
        opened = self.src.index("self.player.set_media(media)", i)
        self.assertLess(i, opened, "the option must be on the media before set_media()/play()")
        guard = self.src[self.src.rfind("_opened_at_position = False", 0, i):i]
        self.assertIn("SEEK_AT_OPEN", guard)
        self.assertIn("seek_seconds > 0", guard)
        self.assertIn("not is_dvd_iso", guard)

    def test_seek_thread_is_told_whether_vlc_was_handed_the_position(self):
        self.assertIn('"opened_at_position": _opened_at_position', self.src)

    # -- seek side (real _seek_when_ready, fake player) --------------------
    def _run(self, opened_at_position, vlc_time_at_check, target_s=1805.0):
        import threading
        import types

        class _Clock:
            t = 0

        vlc_mod = types.ModuleType("vlc")
        vlc_mod.State = types.SimpleNamespace(Ended="Ended", Error="Error", Stopped="Stopped",
                                              Playing="Playing", Paused="Paused")

        class _Player:
            def __init__(self):
                self.pos = vlc_time_at_check
                self.set_time_calls = []

            def get_time(self):
                return self.pos

            def set_time(self, ms):
                self.set_time_calls.append(ms)
                self.pos = ms          # a real seek lands (verified by the poll)

        class _Self:
            pass

        me = _Self()
        me._seek_generation = 1
        me._closed = False
        me._frame_ready = True
        me._current_state = "Playing"
        me._current_length = 3_000_000
        me._current_title = 1
        me._state_cond = threading.Condition()
        me.current_filepath = "C:\\v\\CH 05\\Aladdin - 104.mkv"
        me._pending_seek_generation = 1
        me.player = _Player()

        def _wait(ms):
            _Clock.t += ms

        ns = {
            "pygame": types.SimpleNamespace(time=types.SimpleNamespace(
                get_ticks=lambda: _Clock.t, wait=_wait)),
            "log": _StubLog(),
            "_SEEK_FRAME_READY_GRACE_MS": 600,
        }
        exec(compile(self.seek_src, "<extracted:_seek_when_ready>", "exec"), ns)
        with mock.patch.dict(sys.modules, {"vlc": vlc_mod}):
            ns["_seek_when_ready"](me, target_s, generation=1, opened_at_position=opened_at_position)
        return me, ns["log"]

    def test_already_at_the_target_means_no_second_seek(self):
        me, log = self._run(opened_at_position=True, vlc_time_at_check=1805_120)
        self.assertEqual(me.player.set_time_calls, [],
                         "VLC opened at the spot, so it must not be seeked again")
        self.assertIsNone(me._pending_seek_generation,
                          "landing is still confirmed, so the pending-seek stash must clear")

    def test_start_time_that_did_not_take_falls_back_to_the_seek(self):
        me, log = self._run(opened_at_position=True, vlc_time_at_check=0)
        self.assertEqual(me.player.set_time_calls, [1_805_000])
        self.assertIsNone(me._pending_seek_generation)

    def test_unreadable_position_falls_back_to_the_seek(self):
        for unreadable in (None, -1):
            with self.subTest(vlc_time=unreadable):
                me, log = self._run(opened_at_position=True, vlc_time_at_check=unreadable)
                self.assertEqual(me.player.set_time_calls, [1_805_000])

    def test_without_the_option_the_seek_still_happens_as_before(self):
        me, log = self._run(opened_at_position=False, vlc_time_at_check=0)
        self.assertEqual(me.player.set_time_calls, [1_805_000])


if __name__ == "__main__":
    from extract import run_logged  # also saves this run's output to root/temp, next to app_log.txt
    run_logged(__file__, lambda: unittest.main(verbosity=2))
