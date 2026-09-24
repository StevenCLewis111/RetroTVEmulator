"""
Tests for two DVD-player fixes:

  1. DVD picture fit while Bezel is ON now uses the exact same rule set TV
     stations use (target rect + Bezel squish correction), driven by
     dvd_screen_mode -- so Screen Mode actually does something in the DVD
     player, 16:9 gets bars top/bottom only, and 4:3 gets bars on the
     sides only.
  2. The "READING FOLDER..." box on the DVD/ROM list is drawn AFTER the
     list text, legend and arrows (on top), not before them (underneath).

Same approach as extract.py / test_broadcast_stability_suite.py: the REAL
functions are pulled out of retro_tv_emulator.py / game_deck.py by AST and
exec'd into an isolated namespace (neither file can be imported: the first
boots the whole app, the second needs Windows-only ctypes at import time).
Nothing is re-typed or mirrored.

Put this file in root/tests/. retro_tv_emulator.py is looked for in root/
(one folder up), and game_deck.py in root/ first, then in root/assets.

Run with: python3 test_dvd_bezel_fit.py
      or: python3 -m pytest test_dvd_bezel_fit.py -v
Run as a script, the output is also saved to root/temp/test_dvd_bezel_fit_output.txt
(next to app_log.txt); the previous run's is kept as ..._output_previous.txt.
"""
import ast
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

pygame.init()

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_HERE)


def _find(name):
    for d in (_ROOT_DIR, os.path.join(_ROOT_DIR, "assets")):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(name)


_TREE_CACHE = {}


def _read_tree(path):
    # retro_tv_emulator.py is ~36k lines; parse each file once per run.
    if path in _TREE_CACHE:
        return _TREE_CACHE[path]
    _TREE_CACHE[path] = _read_tree_uncached(path)
    return _TREE_CACHE[path]


def _read_tree_uncached(path):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    return src, ast.parse(src)


def _seg(src, node):
    lines = src.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1:node.end_lineno])


def _load_module_functions(path, names, ns):
    """Exec the named TOP-LEVEL functions of `path` into the shared `ns`."""
    src, tree = _read_tree(path)
    found = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in names:
        exec(compile(_seg(src, found[name]), f"<extracted:{name}>", "exec"), ns)
    return ns


def _load_class_methods(path, cls_name, names, ns):
    """Exec the named METHODS of class `cls_name`, dedented, into `ns`."""
    import textwrap
    src, tree = _read_tree(path)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    meths = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    for name in names:
        code = textwrap.dedent(_seg(src, meths[name]))
        exec(compile(code, f"<extracted:{cls_name}.{name}>", "exec"), ns)
    return ns


# --------------------------------------------------------------------------
# Part 1: DVD fit vs TV fit (retro_tv_emulator.py)
# --------------------------------------------------------------------------

_FIT_FUNCS = [
    "_full_tv_43_outer_rect", "_active_screen_mode", "_bezel_video_pixel_aspect",
    "_bezel_video_wants_tube_fit", "_video_pixel_aspect", "_video_screen_mode_rect",
    "_dvd_bezel_video_fit", "_video_bar_color", "_blit_video_with_screen_mode",
]

WIN_W, WIN_H = 1280, 720
# A Bezel cutout of roughly the shape the real log implies: its independent
# per-axis composite gives scale_y / scale_x == 1.3037 for a 1280x720 canvas
# (see "pixel_aspect=1.3037" on the 16:9 TV-station line in app_log.txt).
CUT_W, CUT_H = 903, 662
SCALE_X, SCALE_Y = CUT_W / WIN_W, CUT_H / WIN_H
BAR = (15, 15, 15)


class _Log:
    def info(self, *a, **k): pass
    warning = error = debug = info


class _DB:
    def __init__(self, **cfg):
        self.config = dict(cfg)


def _make_ns(config):
    ns = {
        "pygame": pygame, "time": __import__("time"), "log": _Log(),
        "db": _DB(**config),
        "_TRANSITION_GRAY": BAR,
        "_is_true_43_hardware": lambda: False,
        "_selected_package_aspect": lambda: 16.0 / 9.0,
        "_blit_video_log_state": {}, "_video_screen_mode_rect_log_state": {},
        "_PAN_SCAN_DEBUG_HUD": False,
        "_pan_scan_update": lambda surf, axis: 0.0,
        # Bezel composite maps the whole window canvas onto the cutout per axis.
        "_bezel_composite_geometry": lambda w, h: (
            pygame.Rect(0, 0, w, h), SCALE_X, SCALE_Y, 0, 0, CUT_W, CUT_H),
    }
    _load_module_functions(_find("retro_tv_emulator.py"), _FIT_FUNCS, ns)
    return ns


def _on_glass(geom, rect):
    """Visible picture size ON THE GLASS: the drawn picture clipped to
    `rect` (blit clips there), then scaled by the Bezel per-axis composite."""
    x0, y0 = geom["dest_x"], geom["dest_y"]
    x1, y1 = x0 + geom["draw_w"], y0 + geom["draw_h"]
    vx0, vy0 = max(x0, rect.left), max(y0, rect.top)
    vx1, vy1 = min(x1, rect.right), min(y1, rect.bottom)
    return (vx1 - vx0) * SCALE_X, (vy1 - vy0) * SCALE_Y


def _dvd_draw(ns, src_w, src_h, mode):
    """What the DVD path now does with Bezel on."""
    rect = pygame.Rect(0, 0, WIN_W, WIN_H)
    surf = pygame.Surface((src_w, src_h))
    target, pa = ns["_dvd_bezel_video_fit"](rect, WIN_W, WIN_H, src_w / src_h, mode)
    screen = pygame.Surface((WIN_W, WIN_H))
    geom = {}
    ns["_blit_video_with_screen_mode"](screen, surf, target, mode=mode,
                                       geom_out=geom, pixel_aspect=pa)
    return rect, target, pa, geom


def _tv_draw(ns, src_w, src_h):
    """What a TV station does with Bezel on (mode comes from screen_mode)."""
    content_rect = pygame.Rect(0, 0, WIN_W, WIN_H)
    surf = pygame.Surface((src_w, src_h))
    sa = src_w / src_h
    target = ns["_video_screen_mode_rect"](content_rect, WIN_W, WIN_H, source_aspect=sa)
    pa = ns["_video_pixel_aspect"](WIN_W, WIN_H, sa)
    screen = pygame.Surface((WIN_W, WIN_H))
    geom = {}
    ns["_blit_video_with_screen_mode"](screen, surf, target,
                                       mode=ns["_active_screen_mode"](),
                                       geom_out=geom, pixel_aspect=pa)
    return content_rect, target, pa, geom


SOURCES = {
    "16:9 (854x480)": (854, 480),
    "16:9 (960x540)": (960, 540),
    "4:3 (640x480)": (640, 480),
    "cinema 2.35:1": (940, 400),
}
MODES = ("normal", "stretch", "crop", "panscan")


class TestDvdMatchesTvWithBezelOn(unittest.TestCase):
    def test_dvd_uses_same_geometry_as_tv_for_every_source_and_mode(self):
        for label, (w, h) in SOURCES.items():
            for mode in MODES:
                with self.subTest(source=label, mode=mode):
                    # TV's screen_mode and DVD's dvd_screen_mode are set to
                    # DIFFERENT values on purpose: DVD must follow its own.
                    other = "stretch" if mode != "stretch" else "crop"
                    ns_tv = _make_ns({"fake_43_border_enabled": True, "screen_mode": mode,
                                      "dvd_screen_mode": other})
                    ns_dvd = _make_ns({"fake_43_border_enabled": True, "screen_mode": other,
                                       "dvd_screen_mode": mode})
                    _, tv_target, tv_pa, tv_geom = _tv_draw(ns_tv, w, h)
                    _, dvd_target, dvd_pa, dvd_geom = _dvd_draw(ns_dvd, w, h, mode)
                    self.assertEqual(tuple(tv_target), tuple(dvd_target))
                    self.assertAlmostEqual(tv_pa, dvd_pa, places=6)
                    self.assertEqual(tv_geom, dvd_geom)

    def test_16x9_normal_has_bars_top_bottom_only_and_is_undistorted(self):
        ns = _make_ns({"fake_43_border_enabled": True, "screen_mode": "stretch",
                       "dvd_screen_mode": "normal"})
        rect, target, pa, geom = _dvd_draw(ns, 854, 480, "normal")
        gw, gh = _on_glass(geom, target)
        self.assertEqual(tuple(target), (0, 0, WIN_W, WIN_H))       # whole screen, no 4:3 box
        self.assertEqual(geom["draw_w"], WIN_W)                       # full width -> no side bars
        self.assertLess(geom["draw_h"], WIN_H)                        # bars top and bottom
        self.assertAlmostEqual(gw / gh, 854 / 480, delta=0.01)        # real 16:9 proportions on the glass
        self.assertAlmostEqual(gw, CUT_W, delta=1.5)                  # fills the glass width

    def test_4x3_normal_has_bars_on_the_sides_only(self):
        ns = _make_ns({"fake_43_border_enabled": True, "screen_mode": "stretch",
                       "dvd_screen_mode": "normal"})
        rect, target, pa, geom = _dvd_draw(ns, 640, 480, "normal")
        self.assertEqual(tuple(target), (160, 0, 960, 720))           # true-4:3 box, same as TV
        self.assertEqual(geom["draw_h"], WIN_H)                       # full height -> no top/bottom bars
        self.assertEqual(geom["draw_w"], 960)                         # fills the box exactly

    def test_stretch_fills_the_glass(self):
        ns = _make_ns({"fake_43_border_enabled": True, "dvd_screen_mode": "stretch"})
        rect, target, pa, geom = _dvd_draw(ns, 854, 480, "stretch")
        gw, gh = _on_glass(geom, target)
        self.assertAlmostEqual(gw, CUT_W, delta=1.5)
        self.assertAlmostEqual(gh, CUT_H, delta=1.5)

    def test_crop_and_panscan_fill_the_glass_without_distortion(self):
        for mode in ("crop", "panscan"):
            with self.subTest(mode=mode):
                ns = _make_ns({"fake_43_border_enabled": True, "dvd_screen_mode": mode})
                rect, target, pa, geom = _dvd_draw(ns, 854, 480, mode)
                # the drawn picture covers the whole target (nothing left to bar)...
                self.assertGreaterEqual(geom["draw_w"], target.width)
                self.assertGreaterEqual(geom["draw_h"], target.height)
                # ...and the whole drawn picture keeps its real proportions on the glass
                full_w, full_h = geom["draw_w"] * SCALE_X, geom["draw_h"] * SCALE_Y
                self.assertAlmostEqual(full_w / full_h, 854 / 480, delta=0.01)

    def test_before_the_fix_16x9_came_out_squeezed(self):
        """Documents the reported bug: raw window rect + pixel_aspect 1.0
        (what the DVD path used to hand the blit) distorts 16:9 on the glass."""
        ns = _make_ns({"fake_43_border_enabled": True, "dvd_screen_mode": "normal"})
        rect = pygame.Rect(0, 0, WIN_W, WIN_H)
        geom = {}
        ns["_blit_video_with_screen_mode"](pygame.Surface((WIN_W, WIN_H)), pygame.Surface((854, 480)),
                                           rect, mode="normal", geom_out=geom, pixel_aspect=1.0)
        gw, gh = _on_glass(geom, rect)
        self.assertLess(gw / gh, 1.45)          # squeezed to ~glass shape (~1.36), not 1.78
        self.assertGreaterEqual(geom["draw_h"], WIN_H - 2)  # and no bars at all (719 = rounding)

    def test_dvd_ignores_the_tv_screen_mode_setting(self):
        a = _make_ns({"fake_43_border_enabled": True, "screen_mode": "stretch", "dvd_screen_mode": "normal"})
        b = _make_ns({"fake_43_border_enabled": True, "screen_mode": "crop", "dvd_screen_mode": "normal"})
        self.assertEqual(_dvd_draw(a, 854, 480, "normal")[3], _dvd_draw(b, 854, 480, "normal")[3])


class TestBezelOffIsUntouched(unittest.TestCase):
    def test_bezel_off_returns_canvas_and_1_0_for_every_case(self):
        for label, (w, h) in SOURCES.items():
            for mode in MODES:
                with self.subTest(source=label, mode=mode):
                    ns = _make_ns({"fake_43_border_enabled": False, "screen_mode": "stretch",
                                   "dvd_screen_mode": mode})
                    rect = pygame.Rect(0, 0, WIN_W, WIN_H)
                    target, pa = ns["_dvd_bezel_video_fit"](rect, WIN_W, WIN_H, w / h, mode)
                    self.assertEqual(tuple(target), tuple(rect))
                    self.assertEqual(pa, 1.0)

    def test_bezel_off_16x9_in_16x9_fills_the_screen(self):
        ns = _make_ns({"fake_43_border_enabled": False, "dvd_screen_mode": "normal"})
        rect, target, pa, geom = _dvd_draw(ns, 960, 540, "normal")
        self.assertEqual((geom["dest_x"], geom["dest_y"], geom["draw_w"], geom["draw_h"]),
                         (0, 0, WIN_W, WIN_H))


class TestTvPathUnchanged(unittest.TestCase):
    """The new optional `mode` parameter defaults to the TV's own
    screen_mode, so every existing TV call (which never passes it) must
    resolve exactly as it always did."""

    def test_default_mode_equals_explicit_tv_screen_mode(self):
        for bezel in (True, False):
            for label, (w, h) in SOURCES.items():
                for mode in MODES:
                    with self.subTest(bezel=bezel, source=label, mode=mode):
                        ns = _make_ns({"fake_43_border_enabled": bezel, "screen_mode": mode})
                        sa = w / h
                        cr = pygame.Rect(0, 0, WIN_W, WIN_H)
                        self.assertEqual(
                            ns["_bezel_video_wants_tube_fit"](sa),
                            ns["_bezel_video_wants_tube_fit"](sa, mode=mode))
                        self.assertEqual(
                            ns["_video_pixel_aspect"](WIN_W, WIN_H, sa),
                            ns["_video_pixel_aspect"](WIN_W, WIN_H, sa, mode=mode))
                        self.assertEqual(
                            tuple(ns["_video_screen_mode_rect"](cr, WIN_W, WIN_H, source_aspect=sa)),
                            tuple(ns["_video_screen_mode_rect"](cr, WIN_W, WIN_H, source_aspect=sa, mode=mode)))


# --------------------------------------------------------------------------
# Part 2: ExternalGameDeck._dvd_video_fit (outer bar fill + safe fallback)
# --------------------------------------------------------------------------

class TestGameDeckDvdVideoFit(unittest.TestCase):
    def _fit_method(self, main_ns):
        ns = {"pygame": pygame, "sys": sys, "_log_warn_throttled": lambda *a, **k: None}
        _load_class_methods(_find("game_deck.py"), "ExternalGameDeck", ["_dvd_video_fit"], ns)
        main = types.SimpleNamespace(**{k: main_ns[k] for k in
                                        ("_dvd_bezel_video_fit", "_video_bar_color")})
        return ns["_dvd_video_fit"], main

    def test_4x3_box_gets_outer_bars_painted(self):
        main_ns = _make_ns({"fake_43_border_enabled": True, "dvd_screen_mode": "normal"})
        fit, main = self._fit_method(main_ns)
        screen = pygame.Surface((WIN_W, WIN_H))
        screen.fill((200, 0, 0))                       # stale pixels from a previous screen
        with mock.patch.dict(sys.modules, {"__main__": main}):
            target, pa = fit(None, screen, pygame.Surface((640, 480)),
                             pygame.Rect(0, 0, WIN_W, WIN_H), "normal")
        self.assertEqual(tuple(target), (160, 0, 960, 720))
        self.assertEqual(tuple(screen.get_at((10, 360)))[:3], BAR)      # left of the box
        self.assertEqual(tuple(screen.get_at((1270, 360)))[:3], BAR)    # right of the box

    def test_16x9_normal_needs_no_outer_fill(self):
        main_ns = _make_ns({"fake_43_border_enabled": True, "dvd_screen_mode": "normal"})
        fit, main = self._fit_method(main_ns)
        screen = pygame.Surface((WIN_W, WIN_H))
        screen.fill((200, 0, 0))
        with mock.patch.dict(sys.modules, {"__main__": main}):
            target, pa = fit(None, screen, pygame.Surface((854, 480)),
                             pygame.Rect(0, 0, WIN_W, WIN_H), "normal")
        self.assertEqual(tuple(target), (0, 0, WIN_W, WIN_H))
        self.assertEqual(tuple(screen.get_at((10, 10)))[:3], (200, 0, 0))  # untouched here

    def test_bezel_off_touches_nothing(self):
        main_ns = _make_ns({"fake_43_border_enabled": False, "dvd_screen_mode": "normal"})
        fit, main = self._fit_method(main_ns)
        screen = pygame.Surface((WIN_W, WIN_H))
        screen.fill((200, 0, 0))
        with mock.patch.dict(sys.modules, {"__main__": main}):
            target, pa = fit(None, screen, pygame.Surface((640, 480)),
                             pygame.Rect(0, 0, WIN_W, WIN_H), "normal")
        self.assertEqual((tuple(target), pa), ((0, 0, WIN_W, WIN_H), 1.0))
        self.assertEqual(tuple(screen.get_at((10, 360)))[:3], (200, 0, 0))

    def test_falls_back_safely_when_main_module_has_no_fit_function(self):
        ns = {"pygame": pygame, "sys": sys, "_log_warn_throttled": lambda *a, **k: None}
        _load_class_methods(_find("game_deck.py"), "ExternalGameDeck", ["_dvd_video_fit"], ns)
        with mock.patch.dict(sys.modules, {"__main__": types.SimpleNamespace()}):
            target, pa = ns["_dvd_video_fit"](None, pygame.Surface((WIN_W, WIN_H)),
                                               pygame.Surface((854, 480)),
                                               pygame.Rect(0, 0, WIN_W, WIN_H), "normal")
        self.assertEqual((tuple(target), pa), ((0, 0, WIN_W, WIN_H), 1.0))


# --------------------------------------------------------------------------
# Part 3: READING FOLDER overlay is drawn last (on top)
# --------------------------------------------------------------------------

class _Font:
    def __init__(self, calls, tag):
        self.calls, self.tag = calls, tag

    def render(self, text, aa, color):
        self.calls.append(self.tag)
        return pygame.Surface((10, 10))


class TestReadingFolderOverlayOnTop(unittest.TestCase):
    def _run(self, roms, scan_pending, started_ago_ms):
        calls = []
        ns = {"pygame": pygame, "sys": sys, "CONSOLE_LABELS": {}, "_log_warn_throttled": lambda *a, **k: None}
        _load_class_methods(_find("game_deck.py"), "ExternalGameDeck", ["_render_romlist"], ns)

        now = pygame.time.get_ticks()
        deck = types.SimpleNamespace(
            active_console="DVD", consoles=["DVD", "SNES"], roms=roms, rom_index=0, rom_scroll=0,
            romlist_focus="list", romlist_arrow_sel="up", ui_color=(0, 255, 0),
            list_text_color=(255, 255, 255), status_msg="", _scan_pending=scan_pending,
            _scan_started_ticks=(now - started_ago_ms) if scan_pending else 0,
            _font_mid=_Font(calls, "row/text"), _font_big=_Font(calls, "title"),
            _get_boxed_logo=lambda *a: None,
            _poll_scan_pending=lambda: None, _poll_live_rescan=lambda: None,
            _refresh_dvd_root_labels=lambda: None, _poll_rom_hold=lambda: None,
            _draw_romlist_error_banner=lambda *a: None,
            _draw_romlist_console_arrows=lambda *a, **k: calls.append("arrows"),
            _draw_legend=lambda *a, **k: calls.append("legend"),
            _draw_romlist_busy_overlay=lambda *a, **k: calls.append("OVERLAY"),
        )
        ns["_render_romlist"](deck, pygame.Surface((1280, 720)), (0, 0, 1280, 720))
        return calls

    def test_overlay_is_last_when_list_has_rows(self):
        calls = self._run([("Movie A", "a"), ("Movie B", "b")], True, 5000)
        self.assertEqual(calls.count("OVERLAY"), 1)
        self.assertIn("row/text", calls)
        self.assertEqual(calls[-1], "OVERLAY")
        self.assertLess(max(i for i, c in enumerate(calls) if c != "OVERLAY"), calls.index("OVERLAY"))

    def test_overlay_is_last_when_list_is_empty(self):
        calls = self._run([], True, 5000)
        self.assertEqual(calls[-1], "OVERLAY")
        self.assertIn("legend", calls)
        self.assertLess(calls.index("legend"), calls.index("OVERLAY"))

    def test_overlay_still_waits_for_its_1s_debounce(self):
        self.assertNotIn("OVERLAY", self._run([("Movie A", "a")], True, 200))

    def test_no_overlay_when_no_scan_is_pending(self):
        self.assertNotIn("OVERLAY", self._run([("Movie A", "a")], False, 0))


if __name__ == "__main__":
    from extract import run_logged  # also saves this run's output to root/temp, next to app_log.txt
    run_logged(__file__, lambda: unittest.main(verbosity=2))
