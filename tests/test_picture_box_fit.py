"""
Tests for the picture-box fit override: TV playback now feeds the
background picture-box scan's MEASURED aspect (see _picture_box_scan_one /
media_library.detect_picture_box) into the same fit machinery Screen Mode
/ Bezel already use, instead of the raw decoded frame's own width/height
ratio -- and crops the source's own baked-in bars out of the surface
before anything downstream (effects, Pan & Scan, the blit) touches it.

Before this: a widescreen transfer with bars baked into the encode (e.g.
Beauty and the Beast, scanned picture_aspect=1.9149) reported its raw
container ratio (close to 4:3) to _bezel_video_wants_tube_fit(), which
never recognised it as widescreen, so the source's own bars were never
stripped and stayed on screen no matter what Screen Mode was set to.

Same approach as extract.py / test_dvd_bezel_fit.py: the REAL functions
are pulled out of retro_tv_emulator.py by AST and exec'd into an isolated
namespace (the module can't be imported -- it boots the whole app).
Nothing is re-typed or mirrored.

Put this file in root/tests/ (retro_tv_emulator.py is one folder up, in root/).

Run with: python3 test_picture_box_fit.py
      or: python3 -m pytest test_picture_box_fit.py -v
Run as a script, the output is also saved to root/temp/test_picture_box_fit_output.txt
(next to app_log.txt); the previous run's is kept as ..._output_previous.txt.
"""
import ast
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

pygame.init()

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_HERE)
_SRC_PATH = os.path.join(_ROOT_DIR, "retro_tv_emulator.py")

with open(_SRC_PATH, "r", encoding="utf-8") as _f:
    _SRC = _f.read()
_TREE = ast.parse(_SRC)


def _seg(node):
    lines = _SRC.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1:node.end_lineno])


_FUNCS = {n.name: n for n in _TREE.body if isinstance(n, ast.FunctionDef)}


class _Log:
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k): pass
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


def _make_ns(get_cached_return=None, get_cached_raises=None):
    """Builds an isolated namespace with the real
    _picture_box_set_content / _apply_picture_box_crop / their real
    _pan_scan_content_key dependency, and a fake single-file probe
    library standing in for _get_probe_library()."""
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
        # The real module-level constant _picture_box_set_content reads as
        # a global -- not part of the function body being extracted, so it
        # has to be seeded here the same way it's seeded in the real file.
        "_PICTURE_BOX_FIT_REASONS": {"letterbox", "pillarbox", "windowbox"},
    }
    for name in ("_pan_scan_content_key", "_picture_box_set_content", "_apply_picture_box_crop"):
        exec(compile(_seg(_FUNCS[name]), f"<extracted:{name}>", "exec"), ns)
    # Module-level mutable state the real file initializes at import time --
    # _pan_scan_set_content()'s sibling needs the same starting point.
    ns["_picture_box_fit_state"] = {"content_key": None, "norm_rect": None, "picture_aspect": None}
    return ns, log, calls


def _tmp_video_path(_TmpVideoCache={}):
    # A real, stat-able path so _pan_scan_content_key() (path + size + mtime)
    # doesn't short-circuit to None -- reuse one temp file for the whole run.
    if "path" not in _TmpVideoCache:
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".mkv")
        os.write(fd, b"\0" * 1024)
        os.close(fd)
        _TmpVideoCache["path"] = p
    return _TmpVideoCache["path"]


# Beauty and the Beast, as logged: letterbox picture_aspect=1.9149
# rect=[0, 100, 720, 376] out of a 720x576 frame.
BATB_FRAME = [720, 576]
BATB_RECT = [0, 100, 720, 376]
BATB_NORM = [round(BATB_RECT[0] / BATB_FRAME[0], 5), round(BATB_RECT[1] / BATB_FRAME[1], 5),
             round(BATB_RECT[2] / BATB_FRAME[0], 5), round(BATB_RECT[3] / BATB_FRAME[1], 5)]
BATB_BOX = {
    "rect": BATB_RECT, "frame": BATB_FRAME, "norm": BATB_NORM, "has_bars": True,
    "reason": "letterbox", "pixel_aspect": 1.0, "picture_aspect": 1.9149,
    "samples": 4, "detector_version": 1,
}


class TestPictureBoxSetContent(unittest.TestCase):
    def test_no_scan_yet_is_a_clean_no_op(self):
        ns, log, calls = _make_ns(get_cached_return=None)
        ns["_picture_box_set_content"](_tmp_video_path())
        self.assertIsNone(ns["_picture_box_fit_state"]["norm_rect"])
        self.assertIsNone(ns["_picture_box_fit_state"]["picture_aspect"])
        self.assertEqual(calls["get_cached"], 1)

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
        src = pygame.Surface(BATB_FRAME)  # decoded at the same res the scan ran at
        out_surf, aspect = ns["_apply_picture_box_crop"](src)
        self.assertEqual(out_surf.get_size(), (BATB_RECT[2], BATB_RECT[3]))
        self.assertEqual(aspect, 1.9149)

    def test_crop_scales_proportionally_to_a_different_decode_resolution(self):
        # The scan ran on the file directly; the live decode can be a
        # different resolution (upscale/downscale) -- norm rect (fractions
        # of the frame) must still land in the right place.
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


# ---------------------------------------------------------------------------
# The render loop itself (calculate-frame-and-blit for a live TV channel)
# is not extractable/runnable in isolation -- same constraint documented on
# TestBootAnchorPolicyNeverTrustsColdAnchor in test_broadcast_stability_
# suite.py: it depends on live VLC frames, app_state, pygame ticks and
# dozens of sibling closures. This is a source-level characterization test
# proving both real TV call sites actually call the two functions above
# and feed their result into source_aspect, instead of just existing
# unused (which was the actual bug reported: the scanner ran and stored
# results, but nothing read them back).
# ---------------------------------------------------------------------------
class TestPictureBoxCropIsWiredIntoBothTvRenderSites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _SRC

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
            # The two TV render sites feed _video_sa; the TV Guide preview
            # crops with the same call and feeds _pv_fit_aspect instead.
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


if __name__ == "__main__":
    from extract import run_logged  # also saves this run's output to root/temp, next to app_log.txt
    run_logged(__file__, lambda: unittest.main(verbosity=2))
