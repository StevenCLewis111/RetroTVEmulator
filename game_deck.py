# ==============================================================================
# game_deck.py  --  Retro TV custom emulator frontend for CHANNEL 03
# ------------------------------------------------------------------------------
# PART 1 OF 13: GAME FRONTEND REGISTRIES & DATA TABLES
# ==============================================================================

import os
import sys
import re
import json
import time
import threading
import subprocess
import zipfile
import logging
import pygame
import pygame.gfxdraw

log = logging.getLogger(__name__)

# Throttled warning helper — for the handful of except-blocks that live in an
# unconditional per-frame render/poll path (DVD player render, libretro tick
# loading, exit-confirm input polling, libretro frame blit, OSD toast draw,
# aspect-ratio lookup used by the per-frame border check). A persistent
# failure there would otherwise log dozens of times per second and blow
# through the rotating app_log.txt's whole size+backup budget in seconds.
# Everywhere else in this file just uses log.warning() directly since those
# call sites are gated behind discrete, infrequent triggers (a key press, a
# channel change, a process exit, etc.) and can't spam like this.
_throttled_warn_last = {}
def _log_warn_throttled(key, msg, *args, interval=30.0):
    _now = time.time()
    if _now - _throttled_warn_last.get(key, 0.0) >= interval:
        _throttled_warn_last[key] = _now
        log.warning(msg, *args)

# ── Built-in libretro frontend ────────────────────────────────────────────────
try:
    from libretro_core import (
        build_core_for_console, lr_id_for,
        RETRO_DEVICE_ID_JOYPAD_UP,     RETRO_DEVICE_ID_JOYPAD_DOWN,
        RETRO_DEVICE_ID_JOYPAD_LEFT,   RETRO_DEVICE_ID_JOYPAD_RIGHT,
        RETRO_DEVICE_INDEX_ANALOG_LEFT, RETRO_DEVICE_INDEX_ANALOG_RIGHT,
        RETRO_DEVICE_ID_ANALOG_X,       RETRO_DEVICE_ID_ANALOG_Y,
        FRAME_PERF, _perf_timer,
    )
    _LIBRETRO_AVAILABLE = True
    print("[GAME DECK] libretro_core loaded OK")
except ImportError as _e:
    _LIBRETRO_AVAILABLE = False
    print(f"[GAME DECK] libretro_core not found — .exe fallback mode: {_e}")

    class _perf_timer:  # no-op fallback so blit helpers below stay import-safe
        def __init__(self, stage): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _NullFramePerf:
        def set_console(self, label): pass
        def record(self, stage, ms): pass
        def maybe_report(self): pass

    FRAME_PERF = _NullFramePerf()

# Mutable reference to the pygame window HWND — set at boot by retro_tv_emulator.
# Using a list so the background monitor thread can always read the current value.
pygame_hwnd_ref = [None]

_WIN = sys.platform == "win32"
if _WIN:
    import ctypes
    from ctypes import wintypes

# Module-level font cache — pygame.font.SysFont() is expensive; never call it
# per-frame in a render path. Use _cached_font() for any repeated text work.
_gd_font_cache = {}
def _cached_font(name, size, bold=False):
    k = (name, size, bold)
    if k not in _gd_font_cache:
        _gd_font_cache[k] = pygame.font.SysFont(name, size, bold=bold)
    return _gd_font_cache[k]

# ==============================================================================
# WASAPI PER-PROCESS AUDIO CONTROL (pure ctypes — no pycaw dependency)
# ==============================================================================

def _wasapi_set_process_audio(pid, *, mute=None, volume_pct=None):
    """Set per-process volume / mute via Windows Core Audio (WASAPI).
    Pure ctypes — no pycaw / comtypes dependency.
    Returns True if the session was found and updated.

    WASAPI vtable layout used here:
      IMMDeviceEnumerator : QI=0, AddRef=1, Release=2,
                            EnumAudioEndpoints=3, GetDefaultAudioEndpoint=4
      IMMDevice           : QI=0, AddRef=1, Release=2, Activate=3
      IAudioSessionManager2 : QI=0,AddRef=1,Release=2,
                              GetAudioSessionControl=3, GetSimpleAudioVolume=4,
                              GetSessionEnumerator=5
      IAudioSessionEnumerator : QI=0,AddRef=1,Release=2, GetCount=3, GetSession=4
      IAudioSessionControl2   : QI=0..Release=2, [IAudioSessionControl 3-11],
                                GetSessionIdentifier=12, GetSessionInstanceIdentifier=13,
                                GetProcessId=14
      ISimpleAudioVolume      : QI=0,AddRef=1,Release=2,
                                SetMasterVolume=3, GetMasterVolume=4,
                                SetMute=5, GetMute=6
    """
    if not _WIN or (mute is None and volume_pct is None):
        return False
    try:
        import uuid as _uuid
        _c   = ctypes
        _ole = _c.windll.ole32
        # CoInitializeEx — COINIT_MULTITHREADED=0; ignore return code (may be
        # S_FALSE if already initialised, or RPC_E_CHANGED_MODE which is OK)
        _ole.CoInitializeEx(None, 0)

        # ── GUID factory ──────────────────────────────────────────────────────
        def _mk_guid(s):
            b = _uuid.UUID(s).bytes_le
            return (_c.c_byte * 16)(*b)

        CLSID_MMDE = _mk_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        IID_MMDE   = _mk_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        IID_ASM2   = _mk_guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        IID_ASC2   = _mk_guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
        IID_SAV    = _mk_guid("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
        GUID_T     = _c.c_byte * 16
        PVOID      = _c.c_void_p

        # ── vtable call helper ────────────────────────────────────────────────
        def _vt(iface, idx, restype, argtypes, *args):
            """Call COM vtable method[idx] on a raw c_void_p interface pointer."""
            vt   = _c.cast(iface, _c.POINTER(PVOID)).contents.value
            fptr = _c.cast(_c.cast(vt, _c.POINTER(PVOID))[idx],
                           _c.WINFUNCTYPE(restype, PVOID, *argtypes))
            return fptr(iface, *args)

        # ── CoCreateInstance → IMMDeviceEnumerator ────────────────────────────
        pE = PVOID()
        if _ole.CoCreateInstance(
                _c.byref(CLSID_MMDE), None, 1,          # CLSCTX_INPROC_SERVER
                _c.byref(IID_MMDE), _c.byref(pE)) != 0 or not pE.value:
            return False

        # GetDefaultAudioEndpoint(eRender=0, eConsole=0) → IMMDevice
        pDev = PVOID()
        hr = _vt(pE, 4, _c.c_long,
                 [_c.c_int, _c.c_int, _c.POINTER(PVOID)],
                 0, 0, _c.byref(pDev))
        _vt(pE, 2, _c.c_long, [])          # Release IMMDeviceEnumerator
        if hr != 0 or not pDev.value:
            return False

        # IMMDevice::Activate(IID_ASM2, CLSCTX_ALL=23, NULL) → IAudioSessionManager2
        pMgr = PVOID()
        hr = _vt(pDev, 3, _c.c_long,
                 [_c.POINTER(GUID_T), _c.c_uint, PVOID, _c.POINTER(PVOID)],
                 _c.byref(IID_ASM2), 23, None, _c.byref(pMgr))
        _vt(pDev, 2, _c.c_long, [])        # Release IMMDevice
        if hr != 0 or not pMgr.value:
            return False

        # IAudioSessionManager2::GetSessionEnumerator
        pSE = PVOID()
        hr = _vt(pMgr, 5, _c.c_long, [_c.POINTER(PVOID)], _c.byref(pSE))
        _vt(pMgr, 2, _c.c_long, [])        # Release IAudioSessionManager2
        if hr != 0 or not pSE.value:
            return False

        # GetCount
        n = _c.c_int(0)
        _vt(pSE, 3, _c.c_long, [_c.POINTER(_c.c_int)], _c.byref(n))

        found = False
        for i in range(n.value):
            pCtl = PVOID()
            if _vt(pSE, 4, _c.c_long,
                   [_c.c_int, _c.POINTER(PVOID)], i, _c.byref(pCtl)) != 0:
                continue
            if not pCtl.value:
                continue

            # QI → IAudioSessionControl2 to read the process ID
            pCtl2 = PVOID()
            qhr = _vt(pCtl, 0, _c.c_long,
                      [_c.POINTER(GUID_T), _c.POINTER(PVOID)],
                      _c.byref(IID_ASC2), _c.byref(pCtl2))
            spid = _c.c_ulong(0)
            if qhr == 0 and pCtl2.value:
                _vt(pCtl2, 14, _c.c_long,          # GetProcessId
                    [_c.POINTER(_c.c_ulong)], _c.byref(spid))
                _vt(pCtl2, 2, _c.c_long, [])       # Release IAudioSessionControl2

            if spid.value == pid:
                # QI → ISimpleAudioVolume to set volume / mute
                pVol = PVOID()
                vhr = _vt(pCtl, 0, _c.c_long,
                          [_c.POINTER(GUID_T), _c.POINTER(PVOID)],
                          _c.byref(IID_SAV), _c.byref(pVol))
                if vhr == 0 and pVol.value:
                    if volume_pct is not None:
                        gain = max(0.0, min(1.0, float(volume_pct) / 100.0))
                        _vt(pVol, 3, _c.c_long,    # SetMasterVolume
                            [_c.c_float, PVOID], gain, None)
                    if mute is not None:
                        _vt(pVol, 5, _c.c_long,    # SetMute
                            [_c.c_int, PVOID], int(bool(mute)), None)
                    _vt(pVol, 2, _c.c_long, [])    # Release ISimpleAudioVolume
                    found = True

            _vt(pCtl, 2, _c.c_long, [])            # Release IAudioSessionControl

        _vt(pSE, 2, _c.c_long, [])                 # Release IAudioSessionEnumerator
        return found

    except Exception as e:
        print(f"[WASAPI ctypes] error: {e}")
        return False


# ------------------------------------------------------------------------------
# CONSOLE CATALOGUE DATA REGISTRIES
# ------------------------------------------------------------------------------
CONSOLE_ORDER = ["DVD", "GB", "GBC", "GBA", "NES", "SNES", "GENESIS", "N64", "PSX", "MAME", "GG", "NGP"]

CONSOLE_LABELS = {
    "GB":      "GAME BOY",
    "GBC":     "GAME BOY COLOR",
    "GBA":     "GAME BOY ADVANCE",
    "NES":     "NINTENDO ENTERTAINMENT SYSTEM",
    "SNES":    "SUPER NINTENDO",
    "GENESIS": "SEGA GENESIS",
    "N64":     "NINTENDO 64",
    "PSX":     "PLAYSTATION",
    "MAME":    "MAME ARCADE",
    "GG":      "GAME GEAR",
    "NGP":     "NEO GEO POCKET",
    "DVD":     "DVD VIDEO INTERACTIVE",
}

ROM_EXTENSIONS = {
    "GB":      [".gb", ".zip", ".7z"],
    "GBC":     [".gbc", ".gb", ".zip", ".7z"],
    "GBA":     [".gba", ".zip", ".7z"],
    "NES":     [".nes", ".zip", ".7z"],
    "SNES":    [".smc", ".sfc", ".zip", ".7z"],
    "GENESIS": [".md", ".gen", ".bin", ".smd", ".zip", ".7z"],
    "N64":     [".n64", ".z64", ".v64", ".zip", ".7z"],
    "PSX":     [".cue", ".bin", ".iso", ".chd", ".pbp", ".img", ".zip", ".7z"],
    "MAME":    [".zip", ".chd", ".7z"],
    "GG":      [".gg", ".zip", ".7z"],
    "NGP":     [".ngp", ".ngc", ".zip", ".7z"],
    "DVD":     [".mp4", ".mkv", ".iso", ".vob", ".mpg", ".mpeg", ".avi", ".zip", ".7z"],
}

# Consoles that get the per-profile "Screen Size" option (smaller-than-full
# display sizes). Originally handheld-only (name kept for history/back-compat
# with existing saved profiles); now covers every emulated console so the
# same 100%/75%/50% option is available everywhere. DVD is excluded — it's
# video playback, not an emulator, and has no per-profile system at all.
# Consoles not in this set always render at FULL / max coverage.
HANDHELD_CONSOLES = {"GB", "GBC", "GBA", "GG", "NGP", "NES", "SNES", "GENESIS", "N64", "PSX", "MAME"}

# Consoles that support the per-profile TV-border overlay. Each looks for its
# border image at roms/<console>/border/border.png — GB pulls from roms/GB,
# GBC from roms/GBC, GBA from roms/GBA, GG from roms/GG, NGP from roms/NGP,
# and so on for every console below, all independently. Missing artwork
# degrades cleanly (see _load_border_surface) — the row still shows and can
# be toggled, it just renders the plain scaled frame until a border.png is
# dropped in. DVD is excluded — same reason as HANDHELD_CONSOLES above.
#
# Split into two groups because of the CRT-bezel rule below:
#   - BORDER_CONSOLES_HANDHELD: true handheld devices. The border is an
#     authentic device-shell overlay (a GBA in your hands looks like that
#     regardless of what it's displayed on), so it's always available.
#   - BORDER_CONSOLES_HOME: home-console / arcade systems whose border art
#     is a fake CRT-TV bezel. On a display we've auto-detected as actually
#     being 4:3 (i.e. a real CRT), that fake bezel makes no sense stacked on
#     top of the real thing — so the option is hidden and forced off for
#     this group specifically when aspect_ratio == "4:3". See
#     border_available_for_console().
BORDER_CONSOLES_HANDHELD = {"GB", "GBC", "GBA", "GG", "NGP"}
BORDER_CONSOLES_HOME     = {"NES", "SNES", "GENESIS", "N64", "PSX", "MAME"}
BORDER_CONSOLES = BORDER_CONSOLES_HANDHELD | BORDER_CONSOLES_HOME


def border_available_for_console(console, aspect_ratio):
    """Whether the TV-border overlay option should exist at all for
    *console* right now, given the display's detected aspect_ratio
    ('4:3' or '16:9'). False means: force the overlay off AND hide its
    toggle in the profile menu — not just grey it out."""
    if console not in BORDER_CONSOLES:
        return False
    if aspect_ratio == "4:3" and console in BORDER_CONSOLES_HOME:
        return False
    return True

# Ordered (key, display label, scale factor) — cycled in this order by A/D.
SCREEN_SIZE_OPTIONS = [
    ("full", "FULL", 1.0),
    ("75",   "75%",  0.75),
    ("50",   "50%",  0.5),
]
SCREEN_SIZE_FACTORS = {key: factor for key, _label, factor in SCREEN_SIZE_OPTIONS}

# ------------------------------------------------------------------------------
# PSX multi-disc detection
# ------------------------------------------------------------------------------
# Matches a "Disc N" / "Disk N" / "CD N" marker in a filename, with or without
# surrounding parens/brackets, and an optional "of N" / "/N" suffix — e.g.
# "Final Fantasy VII (Disc 1).cue", "Xenogears (Disc 2 of 2).cue", "Game [CD1].bin".
_DISC_MARKER_RE = re.compile(
    r'[\(\[\s_\-]*(?:disc|disk|cd)[\s_\-]*(\d+)(?:\s*(?:of|/)\s*\d+)?[\)\]]?',
    re.IGNORECASE
)


def find_psx_disc_siblings(rom_path):
    """
    Given the path to a currently-loaded PSX disc image, scan its folder for
    sibling disc images that belong to the same multi-disc game (same base
    title, same extension, differing only by a "Disc N" marker) and return
    them as a list of (disc_num, path) tuples sorted by disc number.

    Returns [] if rom_path doesn't look like part of a numbered set, or if
    only one disc is found (nothing to switch between).
    """
    try:
        folder    = os.path.dirname(rom_path)
        fname     = os.path.basename(rom_path)
        stem, ext = os.path.splitext(fname)
        if not _DISC_MARKER_RE.search(stem):
            return []
        base_key = _DISC_MARKER_RE.sub('', stem).strip().lower()

        discs = []
        for other in os.listdir(folder):
            o_stem, o_ext = os.path.splitext(other)
            if o_ext.lower() != ext.lower():
                continue
            om = _DISC_MARKER_RE.search(o_stem)
            if not om:
                continue
            if _DISC_MARKER_RE.sub('', o_stem).strip().lower() != base_key:
                continue
            try:
                disc_num = int(om.group(1))
            except (TypeError, ValueError):
                continue
            discs.append((disc_num, os.path.join(folder, other)))

        discs.sort(key=lambda t: t[0])
        return discs if len(discs) > 1 else []
    except Exception as e:
        print(f"[LIBRETRO] Disc sibling scan failed: {e}")
        return []

_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_INFORMATION = 0x0400

# ------------------------------------------------------------------------------
# VK CODE TABLE (for controller mapping display and storage)
# ------------------------------------------------------------------------------
VK_NAMES = {
    0x08: "Backspace", 0x0D: "Enter",    0x1B: "Escape",
    0x20: "Space",     0x25: "Left",     0x26: "Up",
    0x27: "Right",     0x28: "Down",
    0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
    0x41: "A", 0x42: "B", 0x43: "C", 0x44: "D", 0x45: "E",
    0x46: "F", 0x47: "G", 0x48: "H", 0x49: "I", 0x4A: "J",
    0x4B: "K", 0x4C: "L", 0x4D: "M", 0x4E: "N", 0x4F: "O",
    0x50: "P", 0x51: "Q", 0x52: "R", 0x53: "S", 0x54: "T",
    0x55: "U", 0x56: "V", 0x57: "W", 0x58: "X", 0x59: "Y",
    0x5A: "Z",
}
VK_FROM_NAME = {v: k for k, v in VK_NAMES.items()}

# pygame key → VK code (for keyboard rebind mode)
PYGAME_KEY_TO_VK = {
    pygame.K_a: 0x41, pygame.K_b: 0x42, pygame.K_c: 0x43, pygame.K_d: 0x44,
    pygame.K_e: 0x45, pygame.K_f: 0x46, pygame.K_g: 0x47, pygame.K_h: 0x48,
    pygame.K_i: 0x49, pygame.K_j: 0x4A, pygame.K_k: 0x4B, pygame.K_l: 0x4C,
    pygame.K_m: 0x4D, pygame.K_n: 0x4E, pygame.K_o: 0x4F, pygame.K_p: 0x50,
    pygame.K_q: 0x51, pygame.K_r: 0x52, pygame.K_s: 0x53, pygame.K_t: 0x54,
    pygame.K_u: 0x55, pygame.K_v: 0x56, pygame.K_w: 0x57, pygame.K_x: 0x58,
    pygame.K_y: 0x59, pygame.K_z: 0x5A,
    pygame.K_0: 0x30, pygame.K_1: 0x31, pygame.K_2: 0x32, pygame.K_3: 0x33,
    pygame.K_4: 0x34, pygame.K_5: 0x35, pygame.K_6: 0x36, pygame.K_7: 0x37,
    pygame.K_8: 0x38, pygame.K_9: 0x39,
    pygame.K_RETURN: 0x0D, pygame.K_BACKSPACE: 0x08, pygame.K_ESCAPE: 0x1B,
    pygame.K_SPACE:  0x20,
    pygame.K_LEFT:   0x25, pygame.K_UP:    0x26,
    pygame.K_RIGHT:  0x27, pygame.K_DOWN:  0x28,
}

# DVD transport canonical action IDs -- deliberately NOT real pygame key
# constants (unlike the old K_x/K_z/K_c reuse). A physical keyboard press
# can never produce these values, so only a key the user has actually
# bound (via DVD Button Mapping, default = the media Play/Rewind/FF
# buttons) can ever satisfy the DVD_ACTION_CANONICAL checks in
# _handle_dvd_input. Previously these reused pygame.K_x/K_z/K_c directly,
# which meant a literal keyboard X/Z/C press ALSO triggered play/rewind/ff
# even when nothing was bound to those letters -- reported as "play/pause
# double-mapped to X" and rewind/fast-forward being "stuck on Z and C"
# instead of responding to the actual remote's Rewind/FF buttons.
_DVD_CANON_PLAY_PAUSE   = -9001
_DVD_CANON_REWIND       = -9002
_DVD_CANON_FASTFORWARD  = -9003

# XInput bitmask constants
_XI_UP    = 0x0001;  _XI_DOWN  = 0x0002
_XI_LEFT  = 0x0004;  _XI_RIGHT = 0x0008
_XI_START = 0x0010;  _XI_BACK  = 0x0020
_XI_LB    = 0x0100;  _XI_RB    = 0x0200
_XI_A     = 0x1000;  _XI_B     = 0x2000
_XI_X     = 0x4000;  _XI_Y     = 0x8000
# LT/RT aren't part of XInput's wButtons bitmask — they're analog bytes
# (0-255) read separately as bLeftTrigger/bRightTrigger. These two values
# sit outside the real ushort button range so they can share the same
# bit-keyed xinput_map/_XI_LABELS lookups once poll_xinput_for_remap
# turns a trigger pull past the threshold into one of these pseudo-bits.
_XI_LT    = 0x10000;  _XI_RT    = 0x20000

# Human-readable labels for the xi_bit constants above, used to persist
# gamepad bindings to emulator_config.json as readable strings (mirrors how
# VK_NAMES/VK_FROM_NAME round-trip keyboard bindings) instead of raw ints
# that would break if a bitmask constant above ever changed.
_XI_LABELS = {
    _XI_UP: "D-Up", _XI_DOWN: "D-Down", _XI_LEFT: "D-Left", _XI_RIGHT: "D-Right",
    _XI_START: "Start", _XI_BACK: "Select",
    _XI_LB: "LB", _XI_RB: "RB",
    _XI_A: "A-btn", _XI_B: "B-btn", _XI_X: "X-btn", _XI_Y: "Y-btn",
    _XI_LT: "LT", _XI_RT: "RT",
}
_XI_LABEL_TO_BIT = {label: bit for bit, label in _XI_LABELS.items()}

# ==============================================================================
# PER-CONSOLE BUTTON DEFINITIONS
# Each entry defines:
#   buttons      : ordered list of button names for this console
#   left_btns    : buttons shown in left column of mapping UI
#   right_btns   : buttons shown in right column of mapping UI
#   btn_seq      : sequential remap order
#   hotspots     : fraction (x, y) of wireframe image per button
#   default_map  : button → default VK code
#   xinput_map   : XInput bitmask → button name
# ==============================================================================

CONSOLE_BUTTON_DEFS = {
    # ── Game Boy (original) ──────────────────────────────────────────────────
    "GB": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["A", "B", "Start", "Select"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.240, 0.570),
            "Left":   (0.130, 0.645),
            "Right":  (0.360, 0.645),
            "Down":   (0.240, 0.725),
            "A":      (0.810, 0.565),
            "B":      (0.655, 0.610),
            "Start":  (0.550, 0.750),
            "Select": (0.400, 0.750),
        },
        "default_map": {
            "A":      0x5A,  # Z
            "B":      0x58,  # X
            "Start":  0x0D,  # Return
            "Select": 0x08,  # Backspace
            # NOTE: Left is intentionally left bound to the same physical key
            # as Up (0x26) here -- that's exactly what's in the source
            # emulator_config.json this was set from. Every other console in
            # this table binds Left to its own key (0x25), and GBC right below
            # this (same two-button family) does too, so this looks like it
            # may not be what was intended. Left flagged rather than silently
            # changed -- see the chat message for details.
            "Up":     0x26,  "Down":  0x28,
            "Left":   0x25,  "Right": 0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── Game Boy Color ───────────────────────────────────────────────────────
    "GBC": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["A", "B", "Start", "Select"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "A", "B", "Start", "Select"],      
        "hotspots": {
            "Up":     (0.250, 0.560),
            "Left":   (0.140, 0.635),
            "Right":  (0.370, 0.635),
            "Down":   (0.250, 0.705),
            "A":      (0.790, 0.575),
            "B":      (0.650, 0.600),
            "Start":  (0.560, 0.750),
            "Select": (0.435, 0.750),
        },
        "default_map": {
            "A":      0x5A, "B":      0x58,
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── Game Boy Advance ─────────────────────────────────────────────────────
    "GBA": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "L", "R", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Start"],
        "right_btns": ["R", "L", "A", "B", "Select"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "L", "R", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.170, 0.32),
            "Left":   (0.099, 0.45),
            "Right":  (0.241, 0.45),
            "Down":   (0.170, 0.57),
            "B":      (0.790, 0.40),
            "A":      (0.875, 0.35),
            "Select": (0.190, 0.725),
            "Start":  (0.190, 0.635),
            "L":      (0.095, 0.18),
            "R":      (0.912, 0.18),
        },
        "default_map": {
            "A":      0x5A, "B":      0x58,
            "L":      0x41, "R":      0x53,
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B",
            _XI_LB: "L", _XI_RB: "R",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── NES ──────────────────────────────────────────────────────────────────
    "NES": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["A", "B", "Start", "Select"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.235, 0.390),
            "Left":   (0.145, 0.505),
            "Right":  (0.325, 0.505),
            "Down":   (0.235, 0.635),
            "A":      (0.805, 0.490),
            "B":      (0.690, 0.490),
            "Start":  (0.535, 0.565),
            "Select": (0.425, 0.565),
        },
        "default_map": {
            "A":      0x5A, "B":      0x58,
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── SNES ─────────────────────────────────────────────────────────────────
    "SNES": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "X", "Y", "L", "R", "Start", "Select"],
        "left_btns":  ["L", "Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["R", "X", "Y", "A", "B", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "L", "R", "A", "B", "X", "Y", "Start", "Select"],
        "hotspots": {
            "Up":     (0.238, 0.345),
            "Left":   (0.148, 0.475),
            "Right":  (0.335, 0.475),
            "Down":   (0.238, 0.615),
            "A":      (0.835, 0.415),
            "B":      (0.748, 0.515),
            "X":      (0.748, 0.310),
            "Y":      (0.655, 0.405),
            "L":      (0.168, 0.195),
            "R":      (0.832, 0.195),
            "Start":  (0.520, 0.570),
            "Select": (0.415, 0.570),
        },
        "default_map": {
            "A":      0x58, "B":      0x5A,  # X, Z -- unchanged from before
            "X":      0x53, "Y":      0x41,  # S, A -- unchanged from before
            "L":      0x51, "R":      0x57,  # Q, W -- unchanged from before
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            # Xbox pad diamond: Y-top / X-left / B-right / A-bottom.
            # SNES pad diamond: X-top / Y-left / A-right / B-bottom.
            # Every face button is mapped by PHYSICAL POSITION, not by label,
            # so a button in a given spot on your controller behaves like the
            # SNES button in that same spot — that's why all four cross over.
            _XI_A: "B", _XI_B: "A", _XI_X: "Y", _XI_Y: "X",
            _XI_LB: "L", _XI_RB: "R",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── Sega Genesis / Mega Drive ─────────────────────────────────────────────
    "GENESIS": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "C", "X", "Y", "Z", "Start"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Start"],
        "right_btns": ["X", "Y", "Z", "A", "B", "C",],
        "btn_seq":    ["Up", "Left", "Right", "Down", "Start", "A", "B", "C", "X", "Y", "Z"],
        "hotspots": {
            "Up":     (0.260, 0.325),
            "Left":   (0.175, 0.445),
            "Right":  (0.345, 0.445),
            "Down":   (0.260, 0.570),
            "A":      (0.705, 0.610),
            "B":      (0.770, 0.560),
            "C":      (0.840, 0.520),
            "X":      (0.645, 0.445),
            "Y":      (0.720, 0.400),
            "Z":      (0.800, 0.370),
            "Start":  (0.510, 0.490),
        },
        "default_map": {
            "A":      0x41, "B":      0x53, "C":      0x45,
            "X":      0x44, "Y":      0x51, "Z":      0x57,
            "Start":  0x0D, "Mode":   0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B", _XI_X: "C", _XI_Y: "X",
            _XI_LB: "Y", _XI_RB: "Z",
            _XI_START: "Start", _XI_BACK: "Mode",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── Nintendo 64 ───────────────────────────────────────────────────────────
    "N64": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Z", "L", "R", "CUp", "CDown", "CLeft", "CRight", "Start"],
        "left_btns":  ["L", "Up", "Left", "Right", "Down", "Start", "Z"],
        "right_btns": ["R", "CUp", "CLeft", "CRight", "CDown", "A", "B"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "A", "B", "Z", "L", "R", "CUp", "CDown", "CLeft", "CRight", "Start"],
        "hotspots": {
            "Up":     (0.225, 0.336),
            "Left":   (0.156, 0.381),
            "Right":  (0.293, 0.381),
            "Down":   (0.225, 0.427),
            "A":      (0.732, 0.439),
            "B":      (0.672, 0.400),
            "Z":      (0.500, 0.592),
            "L":      (0.225, 0.280),
            "R":      (0.803, 0.290),
            "CUp":    (0.805, 0.339),
            "CDown":  (0.804, 0.411),
            "CLeft":  (0.744, 0.374),
            "CRight": (0.859, 0.374),
            "Start":  (0.500, 0.391),
        },
        "default_map": {
            "A":      0x5A,  # Z
            "B":      0x58,  # X
            # NOTE: the source emulator_config.json has Z bound to 0. That
            # matches this codebase's own "unbound" convention (see
            # ctrl_mapping.get(btn, 0) + "if vk:" guards elsewhere in this
            # file), so it's kept as 0 here rather than guessed at -- a key
            # can be bound to it later from the in-app remapping screen.
            "Z":      0,
            "L":      0x41,  # A
            "R":      0x53,  # S
            # NOTE: CUp/CRight land on the same key as A (Z), and CDown lands
            # on the same key as B (X) -- straight from the source file, not
            # something introduced here. That means, e.g., pressing Z presses
            # A, C-Up, and C-Right all at once. Left exactly as given rather
            # than guessed at -- see the chat message for details.
            "CUp":    0x5A,  # Z
            "CDown":  0x58,  # X
            "CLeft":  0x43,  # C
            "CRight": 0x5A,  # Z
            "Start":  0x0D,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B", _XI_X: "Z", _XI_Y: "B",
            _XI_LB: "L", _XI_RB: "R",
            _XI_START: "Start", _XI_BACK: "Z",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── PlayStation ────────────────────────────────────────────────────────────
    "PSX": {
        "buttons":    ["Up", "Down", "Left", "Right", "Cross", "Circle", "Square", "Triangle", "L1", "L2", "R1", "R2", "Start", "Select"],
        "left_btns":  ["L2", "L1", "Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["R2", "R1", "Triangle", "Circle", "Square", "Cross", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "L1", "L2", "R1", "R2", "Cross", "Circle", "Square", "Triangle", "Start", "Select"],
        "hotspots": {
            "Up":     (0.280, 0.285),
            "Left":   (0.188, 0.405),
            "Right":  (0.372, 0.405),
            "Down":   (0.280, 0.530),
            "Cross":  (0.720, 0.480),
            "Circle": (0.785, 0.400),
            "Square": (0.660, 0.400),
            "Triangle":(0.720, 0.320),
            "L1":     (0.200, 0.175),
            "L2":     (0.235, 0.125),
            "R1":     (0.795, 0.175),
            "R2":     (0.760, 0.125),
            "Start":  (0.545, 0.450),
            "Select": (0.455, 0.450),
        },
        "default_map": {
            "Cross":    0x58, "Circle":  0x43, "Square":  0x5A, "Triangle": 0x53,
            "L1":       0x41, "L2":      0x51, "R1":      0x44, "R2":       0x45,
            "Start":    0x0D, "Select":  0x08,
            "Up":       0x26, "Down":    0x28,
            "Left":     0x25, "Right":   0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "Circle", _XI_B: "Cross", _XI_X: "Triangle", _XI_Y: "Square",
            _XI_LB: "L1", _XI_RB: "R1",
            _XI_LT: "L2", _XI_RT: "R2",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── MAME Arcade ────────────────────────────────────────────────────────────
    # Layout matches MAME_wireframe.png: joystick + 2 small buttons (Coin/Start)
    # up top, then a 4x2 grid of action buttons below. Button-role mapping is
    # per the researched MAME/arcade convention:
    #   A → Button1, B → Button2, R1 → Button3, R2 → Button4,
    #   X → Button5, Y → Button6, L1 → Button7, L2 → Button8
    # On the wireframe: bottom grid row (closer to player) = Button1-4 left to
    # right (the primary/most-reached buttons), top grid row = Button5-8 left
    # to right (secondary buttons) -- standard fightstick-style convention.
    "MAME": {
        "buttons":    ["Up", "Down", "Left", "Right",
                        "Button1", "Button2", "Button3", "Button4",
                        "Button5", "Button6", "Button7", "Button8",
                        "Start", "Coin"],
        "left_btns":  ["Coin", "Up", "Left", "Right", "Down", "Button1", "Button2"],
        "right_btns": ["Start", "Button5", "Button6", 
                        "Button7", "Button8", "Button3", "Button4"],
        "btn_seq":    ["Up", "Down", "Left", "Right",
                        "Button1", "Button2", "Button3", "Button4",
                        "Button5", "Button6", "Button7", "Button8",
                        "Start", "Coin"],
        "hotspots": {
            # Joystick directions -- dots placed around the joystick graphic
            # itself (ball ~y=0.29, base ~y=0.41 on the wireframe).
            "Up":      (0.247, 0.322),
            "Left":    (0.177, 0.412),
            "Right":   (0.317, 0.412),
            "Down":    (0.247, 0.502),
            # Bottom grid row (Button1-4, left to right)
            "Button1": (0.439, 0.537),
            "Button2": (0.547, 0.510),
            "Button3": (0.652, 0.537),
            "Button4": (0.755, 0.533),
            # Top grid row (Button5-8, left to right)
            "Button5": (0.436, 0.393),
            "Button6": (0.544, 0.357),
            "Button7": (0.650, 0.391),
            "Button8": (0.752, 0.398),
            # Small top pair -- left = Coin, right = Start (mirrors View/Menu
            # left-to-right ordering on a standard controller)
            "Coin":    (0.409, 0.254),
            "Start":   (0.480, 0.266),
        },
        "default_map": {
            "Button1": 0x58, "Button2": 0x5A, "Button3": 0x41, "Button4": 0x53,
            "Button5": 0x44, "Button6": 0x46, "Button7": 0x47, "Button8": 0x48,
            "Start":   0x31, "Coin":    0x35,
            "Up":      0x26, "Down":    0x28,
            "Left":    0x25, "Right":   0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "Button1", _XI_B: "Button2", _XI_RB: "Button3", _XI_RT: "Button4",
            _XI_X: "Button5", _XI_Y: "Button6", _XI_LB: "Button7", _XI_LT: "Button8",
            _XI_START: "Start", _XI_BACK: "Coin",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── Game Gear ───────────────────────────────────────────────────────────────
    "GG": {
        "buttons":    ["Up", "Down", "Left", "Right", "1", "2", "Start"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["Start", "2", "1"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "1", "2", "Start"],
        "hotspots": {
            "Up":    (0.235, 0.395),
            "Left":  (0.178, 0.465),
            "Right": (0.285, 0.465),
            "Down":  (0.235, 0.545),
            "1":     (0.740, 0.490),
            "2":     (0.790, 0.425),
            "Start": (0.750, 0.325),
        },
        "default_map": {
            "1":      0x5A, "2":      0x58,
            "Start":  0x0D,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "1", _XI_B: "2", _XI_X: "2", _XI_Y: "1",
            _XI_START: "Start",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    # ── Neo Geo Pocket ──────────────────────────────────────────────────────────
    "NGP": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Option"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["Option", "B", "A"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "A", "B", "Option"],
        "hotspots": {
            "Up":     (0.170, 0.320),
            "Left":   (0.090, 0.445),
            "Right":  (0.250, 0.445),
            "Down":   (0.170, 0.570),
            "A":      (0.790, 0.480),
            "B":      (0.865, 0.390),
            "Option": (0.885, 0.210),
        },
        "default_map": {
            "A":      0x5A, "B":      0x58,
            "Option": 0x0D,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "B", _XI_X: "B", _XI_Y: "A",
            _XI_START: "Option",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },
}

# DVD has no controller mapping — use a minimal stub so the UI gracefully skips it
CONSOLE_BUTTON_DEFS["DVD"] = {
    "buttons":    [],
    "left_btns":  [],
    "right_btns": [],
    "btn_seq":    [],
    "hotspots":   {},
    "default_map": {"stick_as_dpad": False},
    "xinput_map":  {},
}

# --------------------------------------------------------------------------
# Convenience helpers — dispatch through the per-console table below so
# callers using the GBA-only globals still work.
# --------------------------------------------------------------------------

def _get_console_def(console):
    """Return the button-def dict for *console*, falling back to GBA."""
    return CONSOLE_BUTTON_DEFS.get(console, CONSOLE_BUTTON_DEFS["GBA"])

# Legacy alias so old code that directly references DEFAULT_GBA_MAPPING still works
DEFAULT_GBA_MAPPING = CONSOLE_BUTTON_DEFS["GBA"]["default_map"]

# Legacy alias — callers that reference XINPUT_TO_GBA_BTN still work (GBA mapping)
XINPUT_TO_GBA_BTN = CONSOLE_BUTTON_DEFS["GBA"]["xinput_map"]

# Legacy alias — callers that reference GBA_BTN_HOTSPOTS still work
GBA_BTN_HOTSPOTS = CONSOLE_BUTTON_DEFS["GBA"]["hotspots"]


# ==============================================================================
# XINPUT CH03 NAVIGATION THREAD
# ------------------------------------------------------------------------------
# Polls XInput directly via ctypes at ~30 Hz — zero SDL joystick involvement.
# When on ch03 (BROWSE / ROMLIST / file picker modes, not GAME), translates
# D-pad + left-stick + A/B into synthetic pygame KEYDOWN events posted to the
# main event queue so handle_event() sees them identically to keyboard input.
# This is the only controller path that avoids pygame.joystick.init() entirely.
#
# The same D-pad also doubles as DVD player controls (up=captions,
# down=pause, left=rewind 30s, right=fast-forward 30s, A=confirm, B=back/
# exit) while dvd_active AND not sitting on an on-disc dvdnav menu -- see
# the dvd_active branch in the posting loop below, which swaps the posted
# keys from the browse W/A/S/D to DVD_ACTION_CANONICAL's V/Z/X/C so the
# D-pad still drives transport even though those two key sets no longer
# overlap (see DVD_ACTION_CANONICAL's comment for why they were split).
# While an on-disc menu IS up, dvd_in_menu is True and the D-pad instead
# posts the ordinary W/A/S/D that _handle_dvd_menu_nav reads for menu-nav.
# DVD mode intentionally disables the held-repeat behavior below — repeating
# would spam-toggle captions/pause and fire repeated 30s skips while a
# direction is just held down, which makes no sense for a single button press.
# ==============================================================================

# Shared state written by retro_tv_emulator.py each frame so the thread knows
# when ch03 is active and when a game is actually running.
_xi_nav_state = {
    "ch03_active":   False,   # True while current_channel == "03"
    "game_running":  False,   # True while mode == "GAME" and emulator live
    "browse_mode":   False,   # True while on the top-level console logo page (BROWSE)
    "dvd_active":    False,   # True while the DVD player is mounted (mode == "DVD_PLAYER")
    "dvd_in_menu":   False,   # True while an on-disc dvdnav menu is on screen (see game_deck.dvd_in_menu)
    "dvd_left_held":  False,  # live (non-edge) D-pad/stick-left state while dvd_active
    "dvd_right_held": False,  # live (non-edge) D-pad/stick-right state while dvd_active
    "running":       True,    # set to False on clean shutdown
}

def _xi_nav_worker():
    """Background thread: poll XInput, emit pygame nav keys for ch03 browsing."""
    import ctypes

    # Load XInput DLL — try newest first, fall back gracefully
    _xi = None
    for _lib in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            _xi = ctypes.windll.LoadLibrary(_lib)
            print(f"[XI NAV] Loaded {_lib}")
            break
        except Exception:
            continue
    if _xi is None:
        print("[XI NAV] XInput unavailable — ch03 controller nav disabled.")
        return

    # Minimal XInput structs
    class _GP(ctypes.Structure):
        _fields_ = [
            ("wButtons",      ctypes.c_ushort),
            ("bLeftTrigger",  ctypes.c_ubyte),
            ("bRightTrigger", ctypes.c_ubyte),
            ("sThumbLX",      ctypes.c_short),
            ("sThumbLY",      ctypes.c_short),
            ("sThumbRX",      ctypes.c_short),
            ("sThumbRY",      ctypes.c_short),
        ]

    class _XS(ctypes.Structure):
        _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _GP)]

    DEAD       = 10000   # analog stick deadzone (out of 32767)
    POLL_HZ    = 30      # polls per second while browsing
    REPEAT_MS  = 180     # ms between held-direction repeats
    REPEAT_DEL = 400     # ms before first repeat fires

    # Key map: XInput bitmask → pygame key constant.
    # _XI_START and _XI_BACK (Start / Select) are intentionally excluded:
    #   • Their only in-game role is the Start+Select quit combo, handled by
    #     both_pressed() inside render_libretro — not by this nav thread.
    #   • Including them caused K_RETURN / K_ESCAPE to fire on the ROM list
    #     the moment the player released Start+Select after quitting, which
    #     auto-launched the first game and opened the minimize dialog.
    #   • A (confirm) and B (back) already cover all menu navigation needs.
    _NAV_MAP = {
        _XI_UP:    pygame.K_w,
        _XI_DOWN:  pygame.K_s,
        _XI_LEFT:  pygame.K_a,
        _XI_RIGHT: pygame.K_d,
        _XI_A:     pygame.K_RETURN,
        _XI_B:     pygame.K_ESCAPE,
    }

    # DVD-transport variant of the four directional bits, used instead of
    # _NAV_MAP whenever dvd_active is True and no on-disc menu is up (see
    # dvd_in_menu below) -- matches DVD_ACTION_CANONICAL exactly so the
    # D-pad drives play/pause/rewind/ff/captions the same way the keyboard
    # does. A/B keep posting RETURN/ESCAPE unchanged in both cases.
    _DVD_NAV_MAP = {
        _XI_UP:    pygame.K_v,          # captions (still a real, single-purpose key)
        _XI_DOWN:  _DVD_CANON_PLAY_PAUSE,
        _XI_LEFT:  _DVD_CANON_REWIND,
        _XI_RIGHT: _DVD_CANON_FASTFORWARD,
    }

    prev_bits   = 0
    hold_bits   = 0          # bits currently held
    hold_since  = {}         # bit → time.monotonic() when hold started
    last_repeat = {}         # bit → time.monotonic() of last repeat fire

    def _post(key):
        """Post a synthetic KEYDOWN into pygame's event queue.
        The `synthetic=True` attribute distinguishes these from real keyboard
        presses so remap-capture handlers can skip them (controller buttons
        must not appear as keyboard bindings in the Remote or Number remap
        lists, and must not be captured as the poweroff hotkey)."""
        try:
            pygame.event.post(pygame.event.Event(
                pygame.KEYDOWN,
                key=key, mod=0, scancode=0, unicode="", synthetic=True
            ))
        except Exception as e:
            log.warning("Failed to post synthetic KEYDOWN for ch03 nav key %s: %s", key, e)

    # True on the first active iteration after a pause (e.g. just quit out of
    # a game back to the ROM list). Used to resync prev_bits to whatever the
    # controller is physically holding at that instant — without this, a
    # still-held "A" from confirming the exit prompt reads as a brand-new
    # press the moment the thread wakes back up, which immediately fires
    # K_RETURN and auto-launches whatever ROM is under the cursor.
    just_resumed = True

    while _xi_nav_state["running"]:
        time.sleep(1.0 / POLL_HZ)

        # Only active on ch03 when not in a live game
        if not _xi_nav_state["ch03_active"] or _xi_nav_state["game_running"]:
            prev_bits = 0
            hold_bits = 0
            hold_since.clear()
            last_repeat.clear()
            _xi_nav_state["dvd_left_held"]  = False
            _xi_nav_state["dvd_right_held"] = False
            just_resumed = True
            continue

        # Read first connected controller (check all 4 slots)
        state = _XS()
        buttons = 0
        for idx in range(4):
            try:
                if _xi.XInputGetState(idx, ctypes.byref(state)) == 0:
                    buttons = state.Gamepad.wButtons
                    lx = state.Gamepad.sThumbLX
                    ly = state.Gamepad.sThumbLY
                    if ly >  DEAD: buttons |= _XI_UP
                    if ly < -DEAD: buttons |= _XI_DOWN
                    if lx < -DEAD: buttons |= _XI_LEFT
                    if lx >  DEAD: buttons |= _XI_RIGHT
                    break
            except Exception:
                continue

        now = time.monotonic()

        # Only track bits we have nav keys for
        nav_bits = buttons & (_XI_UP | _XI_DOWN | _XI_LEFT | _XI_RIGHT |
                               _XI_A | _XI_B | _XI_START | _XI_BACK)

        # Publish live left/right held state for DVD's hold-to-fast-skip
        # logic (_poll_dvd_hold), independent of the edge/repeat handling
        # below — this needs to see "currently held", not "just pressed".
        if _xi_nav_state.get("dvd_active", False):
            _xi_nav_state["dvd_left_held"]  = bool(nav_bits & _XI_LEFT)
            _xi_nav_state["dvd_right_held"] = bool(nav_bits & _XI_RIGHT)
        else:
            _xi_nav_state["dvd_left_held"]  = False
            _xi_nav_state["dvd_right_held"] = False

        # First frame back after a pause: sync prev_bits to whatever's
        # currently held so a still-pressed button (e.g. A held from
        # confirming the exit prompt) doesn't look like a brand-new press.
        if just_resumed:
            prev_bits = nav_bits
            just_resumed = False

        # Pick which keys the D-pad posts this frame: DVD transport keys
        # (Z/X/C/V) while a movie/menu is mounted and no on-disc menu is up,
        # otherwise the ordinary browse/menu-nav W/A/S/D -- see _DVD_NAV_MAP
        # and DVD_ACTION_CANONICAL's comments for why these can't overlap
        # anymore. A/B always post RETURN/ESCAPE regardless.
        if _xi_nav_state.get("dvd_active", False) and not _xi_nav_state.get("dvd_in_menu", False):
            active_map = dict(_NAV_MAP)
            active_map.update(_DVD_NAV_MAP)
        else:
            active_map = _NAV_MAP

        # New presses — fire immediately
        just_pressed = nav_bits & ~prev_bits
        for bit, key in active_map.items():
            if just_pressed & bit:
                # B / Back (K_ESCAPE) must not fire on the top-level console
                # logo page — there's nowhere further back to go and we don't
                # want the controller accidentally opening the minimize dialog.
                if key == pygame.K_ESCAPE and _xi_nav_state.get("browse_mode", False):
                    hold_since.pop(bit, None)
                    last_repeat.pop(bit, None)
                    continue
                _post(key)
                hold_since[bit]  = now
                last_repeat[bit] = now

        # Held buttons — fire repeats (menu-scrolling behavior only; DVD
        # player wants single-press semantics, so it's excluded entirely)
        if _xi_nav_state.get("dvd_active", False):
            hold_since.clear()
            last_repeat.clear()
        else:
            for bit, key in active_map.items():
                if nav_bits & bit and not (just_pressed & bit):
                    # Never repeat K_ESCAPE, and skip it entirely in browse_mode
                    if key == pygame.K_ESCAPE:
                        continue
                    since = hold_since.get(bit, now)
                    last  = last_repeat.get(bit, now)
                    if (now - since) >= REPEAT_DEL / 1000:
                        if (now - last) >= REPEAT_MS / 1000:
                            _post(key)
                            last_repeat[bit] = now
                elif not (nav_bits & bit):
                    hold_since.pop(bit, None)
                    last_repeat.pop(bit, None)

        prev_bits = nav_bits

    print("[XI NAV] Thread exited.")

# Start the nav thread immediately — it idles until ch03 becomes active
if _WIN:
    threading.Thread(target=_xi_nav_worker, daemon=True).start()


# ==============================================================================
# PART 2 OF 13: DECK INITIALIZER & ENGINE STATE BUILDERS
# ==============================================================================

class ExternalGameDeck:
    def __init__(self, vlc_engine=None):
        # FIXED (possible cause of exe-only missing game-channel audio):
        # this used to be a naive os.path.dirname(os.path.abspath(__file__))
        # path with no sys._MEIPASS check -- the same bug class already
        # identified and fixed for static.wav in retro_tv_emulator.py's
        # load_custom_static_sound(), and the exact issue this class's own
        # config_path comment below already describes for __file__ in a
        # packaged build. If __file__ doesn't land inside the bundled data
        # in the exe, cores_dir/roms_dir/logos_dir below can silently point
        # at the wrong place, which can mean the wrong (or no) libretro
        # core -- and therefore no audio callback ever getting registered --
        # while the emulator frontend itself still renders visually.
        self.base_dir = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        self.cores_dir = os.path.join(self.base_dir, "main", "cores")
        self.roms_dir = os.path.join(self.base_dir, "main", "roms")
        self.logos_dir = os.path.join(self.base_dir, "main", "images", "logos")
        # NOTE: config_path is deliberately anchored to sys.argv[0] (the real,
        # persistent exe/script location), NOT self.base_dir (__file__'s
        # location) -- same reasoning as RetroDatabase.config_path in
        # media.py. In a packaged PyInstaller onefile build, __file__ resolves
        # inside the ephemeral per-launch extraction folder, which is wiped
        # after the process exits, so anything saved there (like which
        # libretro core is selected per console) would silently reset to
        # nothing on every relaunch.
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "emulator_config.json")
        # Timestamp (pygame ticks) of the last real gameplay controller
        # input seen by _poll_libretro_input. Gameplay input bypasses the
        # pygame event queue entirely (read straight from XInput), so the
        # ch03 screensaver's idle timer -- which only listens for pygame
        # KEYDOWN/JOY* events -- reads this too, to know the player is
        # still active mid-game.
        self.last_gameplay_input_ticks = 0

        self.mode = "BROWSE"
        self.consoles = []
        self.console_index = 0
        self.roms = []
        self.rom_index = 0
        self.rom_scroll = 0
        self.status_msg = ""

        # Hold-to-scroll state for the ROM / DVD-file list (see _poll_rom_hold)
        self._rom_hold_dir   = 0   # -1 = up, 1 = down, 0 = nothing held
        self._rom_hold_start = 0   # ticks when current direction was first held
        self._rom_hold_last  = 0   # ticks of last auto-step

        self.deck_process = None
        self.game_hwnd = None
        self.ui_overlay_hwnd = None   # layered child window that sits above mGBA for OSD/menus
        # On-screen TV "screen" rect (pygame client coords) the game window should
        # fill when shown. Set each frame by the main loop via sync_child_window().
        # None = fill the whole client area.
        self._fit_rect = None
        self.parent_hwnd = None
        self.is_visible = False
        self.is_embedded = False
        self.is_frozen = False
        self.original_style = None
        self.active_console = None
        self.current_browse_dir = ""

        self.ready_flag = False
        self.ui_color = (0, 255, 128)
        self.emulator_loading  = False
        self.emulator_load_pct = 0
        self.emulator_load_msg = ""

        self._viewport_lock = __import__("threading").Lock()
        self.overlay_ready = False

        # External-process (mGBA/WASAPI) audio control state -- see
        # set_audio_volume(). A worker thread does the actual pycaw/WASAPI
        # COM call, which can take long enough (session enumeration) that
        # rapid slider input (held-key repeat fires every ~180ms) can have
        # several calls in flight at once. Without serialization, whichever
        # thread's COM call happens to finish last "wins" and applies its
        # volume -- not necessarily the most recent slider position -- which
        # is what produced the "same slider spot sounds different depending
        # on whether I got there going up or down" instability. This lock +
        # pending-value pattern collapses any burst of calls down to a single
        # in-flight worker that always applies the LATEST requested value.
        self._audio_vol_lock = threading.Lock()
        self._audio_vol_pending = None
        self._audio_vol_worker_running = False

        # Single source of truth for the mGBA child window's on-screen visibility.
        # All show/hide goes through _set_child_visible() so the main render thread
        # and the channel-change daemon thread can never fight over ShowWindow.
        # _child_visible is the last applied state (None = unknown / not yet mounted)
        # so ShowWindow is only ever called on a real transition (kills the flicker).
        self._child_visible = None
        self._vis_lock = __import__("threading").Lock()

        self._logo_cache = {}
        self._logo_raw_cache = {}
        self._font_big = None
        self._font_mid = None
        self._font_small = None

        self.vlc_engine = vlc_engine
        # Set externally (see _background_subsystem_worker in
        # retro_tv_emulator.py), same pattern as self.vlc_engine above.
        # Callable: (file_path) -> float gain, returning the cached/probed
        # loudness gain for a DVD file if one exists (or already-queued for
        # probing) -- keeps the actual ffmpeg/db plumbing entirely in
        # retro_tv_emulator.py/media.py, which this module deliberately
        # doesn't import.
        self.dvd_gain_lookup = None

        # DVD player state
        self.dvd_last_dir = ""
        self.dvd_file_path = ""
        self.dvd_splash_playing = False
        self.dvd_hud_until = 0
        self.dvd_splash_done = False
        self.dvd_playback_active = False
        self.dvd_paused = False
        # True from the moment a channel-switch queues a DVD resume (leaving
        # ch03 mid-movie and coming back) until that resume closure actually
        # calls play_file_segmented(). Guards the end-of-movie watchdog below
        # against mistaking our OWN leave-time vlc_engine.stop() for the movie
        # having genuinely ended -- see _render_dvd_player().
        self.dvd_resume_pending = False
        self.dvd_paused_frame = None
        # True only between leaving ch03 with the movie PLAYING and the return-resume
        # decoding its first fresh frame. While set, _render_dvd_player shows a clean
        # black bridge instead of the stale leave-frame or a leftover frame from the
        # channel visited in between, so there's no flash of the old image on return.
        self.dvd_awaiting_resume = False
        self.dvd_captions = False
        self.dvd_exit_confirm = False
        self.dvd_exit_selection = "No"
        # RETURN-TO-MAIN-MENU confirm dialog (Backspace while the movie is
        # actually playing -- see _handle_dvd_input / DVD_ACTION_CANONICAL's
        # "return_to_menu" action). Mirrors dvd_exit_confirm's pattern but
        # sends the disc back to its own start menu instead of quitting.
        self.dvd_return_menu_confirm = False
        self.dvd_return_menu_selection = "No"
        self.dvd_osd_msg = ""
        self.dvd_osd_until = 0

        # DVD MENU-NAV LAYER (see _handle_dvd_menu_nav / _handle_dvd_input).
        # dvd_is_disc_image: True for a .iso backup (opened via dvdnav, see
        #   VLCEngineWrapper.play_file_segmented), False for a plain video
        #   file (.mp4/.mkv/etc) that has no menu at all.
        # dvd_in_menu: True while an interactive dvdnav menu is on screen.
        #   Only ever True when dvd_is_disc_image is also True. While True,
        #   WASD (and arrow keys) drive menu navigation (up/down/left/right +
        #   Enter=select) instead of the play/rewind/ff/captions transport
        #   mapping -- see _render_dvd_player's auto-detect polling for how
        #   this flips back to False once the feature actually starts.
        self.dvd_is_disc_image = False
        self.dvd_in_menu = False
        self._dvd_menu_flip_since = 0
        self._dvd_menu_flip_candidate = None
        # Timestamp (pygame ticks) of when get_state() first read
        # Stopped/Ended without interruption -- None while it isn't
        # currently reading that way. See DVD_END_STATE_GRACE_MS.
        self._dvd_end_state_since = None
        # Frozen True by a deliberate "return to menu" (Backspace, either
        # the immediate re-affirm or the confirmed dialog -- see
        # _handle_dvd_input) until the user presses activate again in
        # _handle_dvd_menu_nav. Needed because dvdnav's popup/menu call
        # shows an interactive menu OVER the still-loaded title without
        # changing get_title() at all -- confirmed in app_log.txt (title
        # stayed 1 straight through the popup call) -- so the title-based
        # auto-detect in _render_dvd_player can't tell "showing the popup
        # menu" from "movie resumed playing" on its own here. This is the
        # one case a deliberate user action (not a guess) is more
        # trustworthy than the auto-detect, so it's allowed to override it
        # until the next deliberate activate hands control back.
        self._dvd_menu_manual_override = False
        self._dvd_iso_watchdog_armed = False
        self._dvd_iso_watchdog_deadline = 0
        self._dvd_iso_watchdog_armed_at = 0

        # CHANNEL-LEAVE/RETURN POSITION MEMORY: a channel-return resume does
        # a full dvdnav:// close+reopen (see change_channel's ch03 resume
        # closure), and libdvdnav has no API to reopen already-inside-a-
        # submenu or already-mid-title -- a fresh open always lands back on
        # the disc's own root menu (dvdnav-menu default). Without this,
        # EVERY channel-return dumps the user back on the main menu, even
        # if they were three submenus deep or mid-movie. These two fields
        # let the resume closure reconstruct where they actually were:
        #
        # dvd_was_in_menu_on_leave: snapshot of dvd_in_menu at the moment
        #   they left. If True, they were on a menu/submenu and simply
        #   return to the disc's root menu on channel-return (a fresh dvdnav
        #   reopen always lands there). If False, they were watching the
        #   movie -- jump straight into dvd_saved_title instead of surfacing
        #   the menu at all (see VLCEngineWrapper._enter_title_when_ready).
        self.dvd_was_in_menu_on_leave = False
        # Last known title index while a real title was playing (not the
        # menu domain) -- captured live off vlc_engine.player.get_title()
        # right before leaving. Defaults to 1 (nearly always the feature
        # title) purely as a last-resort fallback if that read ever fails.
        self.dvd_saved_title = 1

        # ISO BACKGROUND-KEEP-ALIVE (channel-leave, .iso disc images ONLY --
        # see dvd_is_disc_image above; a plain .mp4/.mkv/.vob file never sets
        # this and keeps using the position/frame logic above instead).
        # Replaces the old "stop VLC, remember where we were, reopen +
        # replay/re-navigate on return" dance for disc images: instead of
        # tearing dvdnav down at all, channel-leave just mutes the player and
        # leaves it running exactly as-is (see change_channel's ch03-leave
        # block), so whatever was on screen -- root menu, submenu, or the
        # movie -- is simply still there, still advancing in real time, when
        # the user comes back. True from the moment that mute happens until
        # the matching channel-return unmute runs; nothing else reads it, it
        # just marks "VLC is alive and quietly playing in the background
        # right now" so the return path knows a plain unmute is all that's
        # needed (no reopen, no black bridge, no menu-nav replay).
        self.dvd_iso_backgrounded = False

        # Game (libretro core) exit-confirm overlay state — Start+Select and
        # ESC both open this "ARE YOU SURE?" prompt instead of quitting the
        # game immediately, same idea as the DVD player's exit confirm above.
        self.game_exit_confirm = False
        self.game_exit_selection = "No"
        self._game_exit_prev_buttons = 0
        self._game_exit_toggle_held = False   # tracks ESC/Start+Select held state for open/close toggle
        self._prev_overlay_open = False       # was a TV-shell overlay (menu/splash/quit-confirm) open last frame?
        self._libretro_suppress_start_select = False  # masks Start/Select from the game until released, right after closing the exit-confirm box

        # Controller mapping state
        self._active_mapping_console = "GBA"     # which console the mapping UI is open for
        self._console_mappings = {}               # cache: console → mapping dict
        self.ctrl_mapping = self._load_controller_mapping("GBA")
        self.ctrl_mapping_active_btn = None   # Which button is awaiting remap
        self.ctrl_mapping_mode = "controller" # "controller" or "keyboard"
        self._wf_surface_cache = {}           # console_key → raw wireframe pygame surface
        self._wf_tinted_cache  = {}           # console_key → tinted surface
        self._wf_tint_color_cache = {}        # console_key → last tint color used
        # Legacy aliases kept for backward compat (GBA)
        self._gba_wf_surface = None
        self._gba_wf_tinted  = None
        self._gba_wf_tint_color = None
        self._seq_remap_active = False         # Sequential auto-advance remap in progress
        self._ctrl_labels = {}                 # btn → human-readable controller button name
        self._last_xinput_btns = 0             # XInput debounce state
        self._mapping_close_requested = False  # Close button was clicked
        self._ctrl_map_cursor = 0              # Bottom-option cursor (0=Map 1=Stick 2=Close)
        self._mapping_opt_rects = []           # Clickable rects for bottom options

        self.emulator_config = self._load_emulator_config()
        self._refresh_consoles()

        # ── Libretro state ─────────────────────────────────────────────────────
        self._libretro_core           = None    # LibretroCore; stays alive across channels
        self._libretro_console        = ""
        self._prev_libretro_console   = ""
        self._libretro_rom_path       = ""
        self._libretro_loading        = False
        self._libretro_load_start     = 0
        self._libretro_load_min_ms    = 2500
        self._libretro_auto_save      = False   # off by default; set per-console profile
        self._libretro_auto_load      = False   # off by default; set per-console profile
        self._libretro_active_profile = 1       # 1, 2, or 3; per-console
        self._libretro_screen_size    = "full"  # "full"/"75"/"50"; handhelds only, per-console profile
        # TV-border overlay (GB/GBC/GBA) — OFF by default, per-console/profile.
        # Drawn entirely on our side (see _load_border_surface / _blit_libretro_frame),
        # never by the core, so toggling it is instant and works even on cores
        # (like GBA's) that have no native border concept at all. Displayed
        # as-authored — no theme-color retinting, so any border art you drop
        # in just works without having to be designed around it.
        self._libretro_border_enabled       = True   # current console's overlay toggle (default ON; see sync_profile_from_db)
        self._libretro_border_surf          = None   # cached pygame.Surface, or None
        self._libretro_border_rect_frac     = None   # detected (x,y,w,h) screen-cutout fractions, or None
        self._libretro_border_src_key       = None   # cache key: (console, path, mtime)
        # Cached pre-scaled border blit (see _blit_bordered_cached): avoids
        # re-running pygame.transform.scale() on the full border image every
        # single frame. Tuple of (key, scaled_border_surf, (ox,oy), inner_rect)
        # or None; auto-invalidates whenever the key (border identity /
        # dest_rect / rect_frac) changes.
        self._border_blit_cache             = None
        self._libretro_xinput_map     = {}      # console → {btn_name → xi_bit}; see BUG note at _build_default_libretro_xinput_map
        self._libretro_hotkey_cooldown = 0      # ticks; blocks re-fire until expired
        self._libretro_osd_msg        = ""
        self._libretro_osd_until      = 0
        threading.Thread(target=self._preload_logos_async, daemon=True).start()

    # --------------------------------------------------------------------------

    def _load_emulator_config(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            log.warning("Failed to load emulator_config.json, using defaults: %s", e)
        return {}

    def _refresh_consoles(self, consoles_enabled=None):
        if consoles_enabled is None:
            consoles_enabled = {}
        found = []
        for c in CONSOLE_ORDER:
            default_on = (c == "DVD")
            if not consoles_enabled.get(c, default_on):
                continue
            if c == "DVD":
                found.append(c)
                continue
            emu_folder = os.path.join(self.cores_dir, c)
            rom_folder = os.path.join(self.roms_dir, c)
            if os.path.isdir(emu_folder) or os.path.isdir(rom_folder):
                found.append(c)
        self.consoles = found if found else ["DVD"]
        if self.console_index >= len(self.consoles):
            self.console_index = 0

    def _preload_logos_async(self):
        for console in CONSOLE_ORDER:
            path = os.path.join(self.logos_dir, f"{console}.png")
            if os.path.exists(path):
                try:
                    self._logo_raw_cache[console] = pygame.image.load(path).convert_alpha()
                except Exception:
                    self._logo_raw_cache[console] = None
            else:
                self._logo_raw_cache[console] = None

    def _get_aspect_ratio(self):
        """Best-effort read of the current auto-detected display aspect ratio
        ('4:3' or '16:9') from the main module's db. Used by border logic
        that doesn't already have db_config handed to it directly (unlike
        sync_profile_from_db, which does). Defaults to '16:9' (i.e. border
        behavior unchanged) if db isn't reachable for any reason."""
        try:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            if _main and hasattr(_main, "db") and _main.db:
                return _main.db.config.get("aspect_ratio", "16:9")
        except Exception as e:
            _log_warn_throttled("get_aspect_ratio", "Per-frame aspect-ratio lookup failed, defaulting to 16:9: %s", e)
        return "16:9"

    def sync_profile_from_db(self, console, db_config):
        """Load active profile + its auto-save/auto-load flags for *console* from db.config.
        Call this before launching a ROM and whenever returning to ch03.

        db.config["console_profiles"] layout:
          {
            "GBA": { "active": 1,
                     "profiles": { 1: {"auto_save": false, "auto_load": false},
                                   2: {"auto_save": false, "auto_load": false},
                                   3: {"auto_save": false, "auto_load": false} } },
            ...
          }
        """
        all_profiles = db_config.get("console_profiles", {})
        con_data     = all_profiles.get(console, {})
        active       = int(con_data.get("active", 1))
        profiles     = con_data.get("profiles", {})
        prof_cfg     = profiles.get(active, profiles.get(str(active), {}))
        self._libretro_active_profile = active
        self._libretro_auto_save      = bool(prof_cfg.get("auto_save", False))
        self._libretro_auto_load      = bool(prof_cfg.get("auto_load", False))
        # Screen size only applies to handhelds; everything else always renders FULL.
        if console in HANDHELD_CONSOLES:
            self._libretro_screen_size = prof_cfg.get("screen_size", "full")
            if self._libretro_screen_size not in SCREEN_SIZE_FACTORS:
                self._libretro_screen_size = "full"
        else:
            self._libretro_screen_size = "full"
        # TV-border overlay — GB/GBC/GBA; ON by default. "gb_border" is the
        # old config key from when this was GB-only; still honored as a
        # fallback so existing profiles don't lose a setting they already made.
        # An explicit False a person already saved (i.e. they turned it off
        # on purpose) is still respected — this default only applies the
        # first time a profile is touched, before it has an opinion either way.
        if border_available_for_console(console, db_config.get("aspect_ratio", "16:9")):
            self._libretro_border_enabled = bool(
                prof_cfg.get("border_enabled", prof_cfg.get("gb_border", True)))
        else:
            self._libretro_border_enabled = False
        # Keep the core in sync so saves/loads land in the right profile folder
        if self._libretro_core is not None and hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(active)
        print(f"[GAMEDECK PROFILE] {console} → profile {active}, "
              f"auto_save={self._libretro_auto_save}, auto_load={self._libretro_auto_load}")

    def set_border_live(self, enabled):
        """Toggle the TV-border overlay WHILE a game is already running, so
        it takes effect on the very next rendered frame — no restart, no
        core reload. The border is composited entirely on our side (see
        render_libretro / _blit_libretro_frame), so flipping this flag is
        all it takes; the underlying native frame from the core never
        changes."""
        if enabled and not border_available_for_console(self._libretro_console, self._get_aspect_ratio()):
            enabled = False
        self._libretro_border_enabled = bool(enabled)

    # Last-resort fallback screen-cutout rect (x_frac, y_frac, w_frac, h_frac)
    # used only if a border image's transparent cutout can't be auto-detected
    # (e.g. no numpy available, or a border with no enclosed transparent hole).
    # These are the classic Super Game Boy 256x224-canvas / 160x144-window
    # proportions, which look reasonable as a generic guess.
    _BORDER_DEFAULT_RECT_FRAC = (48 / 256, 40 / 224, 160 / 256, 144 / 224)

    def _detect_border_cutout_frac(self, raw_surf):
        """Auto-detect where the game screen goes inside a border image by
        flood-filling its transparent pixels inward from every edge. Any
        transparent pixels left over — unreachable from the outside — form
        an enclosed hole, which is exactly the screen cutout the artist
        punched into the border. Returns (x_frac, y_frac, w_frac, h_frac)
        describing that hole as a fraction of the full image, or the
        classic-SGB fallback proportions if no clear hole is found."""
        try:
            import numpy as np
            w, h = raw_surf.get_size()
            alpha = pygame.surfarray.array_alpha(raw_surf)   # shape (w, h)
            transparent = alpha < 24
            visited = np.zeros_like(transparent, dtype=bool)
            edge_mask = np.zeros_like(transparent)
            edge_mask[0, :] = True; edge_mask[-1, :] = True
            edge_mask[:, 0] = True; edge_mask[:, -1] = True
            seed_xs, seed_ys = np.nonzero(transparent & edge_mask)
            stack = list(zip(seed_xs.tolist(), seed_ys.tolist()))
            visited[seed_xs, seed_ys] = True
            while stack:
                x, y = stack.pop()
                if x > 0 and transparent[x - 1, y] and not visited[x - 1, y]:
                    visited[x - 1, y] = True; stack.append((x - 1, y))
                if x < w - 1 and transparent[x + 1, y] and not visited[x + 1, y]:
                    visited[x + 1, y] = True; stack.append((x + 1, y))
                if y > 0 and transparent[x, y - 1] and not visited[x, y - 1]:
                    visited[x, y - 1] = True; stack.append((x, y - 1))
                if y < h - 1 and transparent[x, y + 1] and not visited[x, y + 1]:
                    visited[x, y + 1] = True; stack.append((x, y + 1))
            interior = transparent & ~visited
            if interior.sum() < (w * h * 0.005):
                raise ValueError("no enclosed transparent cutout found")
            # `interior` can contain more than one disconnected enclosed
            # region — e.g. stray transparent padding left over elsewhere in
            # the canvas that also happens to be unreachable from the edges.
            # Bounding-boxing ALL of it together (old behavior) let a stray
            # blob stretch the detected rect far past the real screen hole,
            # which is what caused the game picture to hang out past the
            # bottom of the border art. Instead, split into connected
            # components and keep only the largest one — the actual screen
            # cutout is always the dominant enclosed hole in a border image.
            comp_visited = np.zeros_like(interior, dtype=bool)
            ixs, iys = np.nonzero(interior)
            best_pixels = None
            best_size = 0
            for sx, sy in zip(ixs.tolist(), iys.tolist()):
                if comp_visited[sx, sy]:
                    continue
                comp_stack = [(sx, sy)]
                comp_visited[sx, sy] = True
                comp_xs = [sx]; comp_ys = [sy]
                while comp_stack:
                    x, y = comp_stack.pop()
                    if x > 0 and interior[x - 1, y] and not comp_visited[x - 1, y]:
                        comp_visited[x - 1, y] = True; comp_stack.append((x - 1, y))
                        comp_xs.append(x - 1); comp_ys.append(y)
                    if x < w - 1 and interior[x + 1, y] and not comp_visited[x + 1, y]:
                        comp_visited[x + 1, y] = True; comp_stack.append((x + 1, y))
                        comp_xs.append(x + 1); comp_ys.append(y)
                    if y > 0 and interior[x, y - 1] and not comp_visited[x, y - 1]:
                        comp_visited[x, y - 1] = True; comp_stack.append((x, y - 1))
                        comp_xs.append(x); comp_ys.append(y - 1)
                    if y < h - 1 and interior[x, y + 1] and not comp_visited[x, y + 1]:
                        comp_visited[x, y + 1] = True; comp_stack.append((x, y + 1))
                        comp_xs.append(x); comp_ys.append(y + 1)
                if len(comp_xs) > best_size:
                    best_size = len(comp_xs)
                    best_pixels = (comp_xs, comp_ys)
            xs, ys = best_pixels
            minx, maxx = min(xs), max(xs)
            miny, maxy = min(ys), max(ys)
            rect = (minx / w, miny / h, (maxx - minx + 1) / w, (maxy - miny + 1) / h)
            print(f"[LIBRETRO] Border cutout auto-detected: {rect}")
            return rect
        except Exception as e:
            print(f"[LIBRETRO] Border cutout auto-detect unavailable ({e}); "
                  f"using default SGB-style proportions")
            return self._BORDER_DEFAULT_RECT_FRAC

    def _load_border_surface(self, console):
        """Load & prepare the TV-border overlay for *console* from
        roms/<console>/border/border.png, shown as-authored (no theme-color
        retinting). Cached by (console, path, mtime) so repeated calls —
        e.g. once at launch and again on every live border toggle — don't
        re-read/re-decode the file from disk unless the image itself
        changed. Clears self._libretro_border_surf / _rect_frac to None if
        no usable border image is found."""
        border_dir = os.path.join(self.roms_dir, console, "border")
        path = None
        if os.path.isdir(border_dir):
            exact = os.path.join(border_dir, "border.png")
            if os.path.isfile(exact):
                path = exact
            else:
                exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
                try:
                    for f in sorted(os.listdir(border_dir)):
                        if os.path.splitext(f)[1].lower() in exts:
                            path = os.path.join(border_dir, f); break
                except Exception:
                    path = None

        if path is None:
            self._libretro_border_surf      = None
            self._libretro_border_rect_frac = None
            self._libretro_border_src_key   = None
            return

        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0

        cache_key = (console, path, mtime)
        if cache_key == self._libretro_border_src_key and self._libretro_border_surf is not None:
            return  # already loaded for this exact image

        try:
            raw_surf = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[LIBRETRO] Could not load border image '{path}': {e}")
            self._libretro_border_surf      = None
            self._libretro_border_rect_frac = None
            self._libretro_border_src_key   = None
            return

        self._libretro_border_rect_frac = self._detect_border_cutout_frac(raw_surf)
        self._libretro_border_surf      = raw_surf
        self._libretro_border_src_key   = cache_key

    def _blit_libretro_frame(self, screen, surf, dest_rect):
        """Draw one libretro frame to dest_rect, compositing the TV-border
        overlay around it when enabled/available for the current console.
        Central chokepoint used by every render_libretro() blit call so the
        border shows up consistently whether the game is running, frozen,
        or showing the exit-confirm prompt — and disappears instantly on
        toggle since there's no core state involved. Border art is shown
        as-authored (no theme-color retinting)."""
        if (self._libretro_console in BORDER_CONSOLES and self._libretro_border_enabled
                and border_available_for_console(self._libretro_console, self._get_aspect_ratio())):
            if self._libretro_border_surf is None:
                self._load_border_surface(self._libretro_console)
            if self._libretro_border_surf is not None:
                rect_frac = self._libretro_border_rect_frac or self._BORDER_DEFAULT_RECT_FRAC
                self._blit_bordered_cached(screen, surf, self._libretro_border_surf, rect_frac, dest_rect)
                return
        _libretro_blit_scaled(screen, surf, dest_rect)

    def _blit_bordered_cached(self, screen, surf, border_surf, rect_frac, dest_rect):
        """Same visual result as the module-level _libretro_blit_bordered(),
        but caches the scaled border surface + inner-rect geometry instead of
        recomputing them every call.

        _libretro_blit_bordered() used to call pygame.transform.scale() on
        the FULL border image (often a large, full-resolution TV-bezel PNG)
        on every single frame, even though the border art and its target
        size are constant the entire time the console/window stay the same.
        Since the TV-border overlay defaults to ON and covers almost every
        console (see BORDER_CONSOLES), this was a fixed, wasted rescale
        running 60x/sec on nearly every console regardless of whether
        anything on screen had actually changed -- stealing frame budget
        from retro_run()/LibretroAudio.tick() every single frame from boot.
        That shows up exactly as reported: gameplay running under native
        speed (fewer retro_run() calls fit in each real second) plus audio
        stutter/crackle (the reservoir gets fed less often than it drains).

        Only the live game frame actually needs re-scaling every frame (its
        pixel content changes); the border doesn't. Now the expensive
        border rescale only happens once, the first time this exact
        (border image, dest_rect, rect_frac) combination is seen, and is
        reused every frame after that until something actually changes
        (window resize, console switch, border toggle/reload)."""
        key = (id(border_surf), tuple(dest_rect), tuple(rect_frac))
        cached = self._border_blit_cache
        if cached is None or cached[0] != key:
            try:
                dw, dh = dest_rect[2], dest_rect[3]
                bw, bh = border_surf.get_size()
                scale   = min(dw / max(bw, 1), dh / max(bh, 1))
                draw_bw = int(bw * scale); draw_bh = int(bh * scale)
                ox = dest_rect[0] + (dw - draw_bw) // 2
                oy = dest_rect[1] + (dh - draw_bh) // 2
                rx, ry, rw, rh = rect_frac
                inner_x = ox + int(draw_bw * rx)
                inner_y = oy + int(draw_bh * ry)
                inner_w = max(1, int(draw_bw * rw))
                inner_h = max(1, int(draw_bh * rh))
                scaled_border = pygame.transform.scale(border_surf, (draw_bw, draw_bh))
                self._border_blit_cache = (key, scaled_border, (ox, oy), (inner_x, inner_y, inner_w, inner_h))
                cached = self._border_blit_cache
            except Exception as e:
                _log_warn_throttled("libretro_border_cache_build", "Border cache rebuild failed, falling back to plain scaled frame: %s", e)
                self._border_blit_cache = None
                _libretro_blit_scaled(screen, surf, dest_rect)
                return

        _key, scaled_border, (ox, oy), (inner_x, inner_y, inner_w, inner_h) = cached
        try:
            with _perf_timer("blit_scale"):
                screen.blit(pygame.transform.scale(surf, (inner_w, inner_h)), (inner_x, inner_y))
            with _perf_timer("border_blit"):
                screen.blit(scaled_border, (ox, oy))
        except Exception as e:
            _log_warn_throttled("libretro_border_cache_blit", "Cached border blit failed this frame, falling back to plain scaled frame: %s", e)
            self._border_blit_cache = None
            _libretro_blit_scaled(screen, surf, dest_rect)

    def libretro_save_to_profile(self, console=None, profile=None):
        """Save state to the given profile slot (1/2/3). Returns True on success.
        Saves land in saves/profile{N}/ so each profile has isolated save files."""
        if self._libretro_core is None:
            return False
        if profile is None:
            profile = self._libretro_active_profile
        slot = int(profile)
        # Route the core to the correct profile folder before saving
        if hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(slot)
        ok = self._libretro_core.save_state(slot=slot)
        print(f"[GAMEDECK PROFILE] Save → profile{slot}/slot{slot}: {'OK' if ok else 'FAILED'}")
        return ok

    def libretro_load_from_profile(self, console=None, profile=None):
        """Load state from the given profile slot (1/2/3). Returns True on success.
        Loads from saves/profile{N}/ so each profile reads its own isolated saves."""
        if self._libretro_core is None:
            return False
        if profile is None:
            profile = self._libretro_active_profile
        slot = int(profile)
        # Route the core to the correct profile folder before loading
        if hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(slot)
        if not self._libretro_core.has_state(slot=slot):
            print(f"[GAMEDECK PROFILE] Load profile{slot}/slot{slot}: no save found")
            return False
        ok = self._libretro_core.load_state(slot=slot)
        print(f"[GAMEDECK PROFILE] Load ← profile{slot}/slot{slot}: {'OK' if ok else 'FAILED'}")
        return ok

    def libretro_has_profile_state(self, profile=None):
        """True if a save file exists for the given profile slot (checks saves/profile{N}/)."""
        if self._libretro_core is None:
            return False
        if profile is None:
            profile = self._libretro_active_profile
        slot = int(profile)
        # Temporarily point the core at the right folder just for this check,
        # then restore whatever was active so normal gameplay is not disrupted.
        orig = getattr(self._libretro_core, "active_profile", slot)
        if hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(slot)
        result = self._libretro_core.has_state(slot=slot)
        if hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(orig)
        return result

    # ------------------------------------------------------------------
    # PSX live disc swapping — for multi-disc games (Disc 1 / Disc 2 / ...)
    # ------------------------------------------------------------------
    def get_psx_disc_info(self):
        """
        Returns (discs, current_index) for the multi-disc set the CURRENTLY
        LOADED PSX game belongs to, where discs is [(disc_num, path), ...]
        sorted by disc number, and current_index is the loaded game's
        position in that list (-1 if not found). discs is [] whenever PSX
        isn't the live console, nothing is loaded, or no sibling discs were
        detected — callers should treat that as "no disc switching available".
        """
        if self._libretro_console != "PSX" or not self._libretro_rom_path:
            return [], -1
        discs = find_psx_disc_siblings(self._libretro_rom_path)
        if not discs:
            return [], -1
        cur = os.path.normcase(os.path.abspath(self._libretro_rom_path))
        cur_idx = next((i for i, (_n, p) in enumerate(discs)
                         if os.path.normcase(os.path.abspath(p)) == cur), -1)
        return discs, cur_idx

    def change_psx_disc(self, disc_index):
        """
        Live-swap the inserted PSX disc to discs[disc_index] from the set
        returned by get_psx_disc_info() — the core keeps running the whole
        time (no unload/reload), so in-progress RAM state and save data are
        untouched. Sets a brief on-screen confirmation message. Returns True
        on success.
        """
        discs, _cur_idx = self.get_psx_disc_info()
        if not discs or not (0 <= disc_index < len(discs)):
            self._libretro_osd_msg   = "NO OTHER DISC FOUND"
            self._libretro_osd_until = pygame.time.get_ticks() + 2000
            return False

        core = self._libretro_core
        if core is None or not core.is_loaded or not hasattr(core, "change_disc"):
            self._libretro_osd_msg   = "DISC CHANGE UNAVAILABLE"
            self._libretro_osd_until = pygame.time.get_ticks() + 2000
            return False

        disc_num, disc_path = discs[disc_index]
        ok = core.change_disc(disc_path)
        if ok:
            self._libretro_rom_path = disc_path
            self._libretro_osd_msg  = f"DISC {disc_num} INSERTED"
        else:
            self._libretro_osd_msg = "DISC CHANGE FAILED"
        self._libretro_osd_until = pygame.time.get_ticks() + 2000
        print(f"[GAMEDECK] Change disc → disc {disc_num}: {'OK' if ok else 'FAILED'}")
        return ok

    # ------------------------------------------------------------------
    # Soft freeze/unfreeze — for menus/overlays (does NOT auto-save).
    # Hard freeze (freeze_libretro) is for channel-leave and DOES auto-save.
    # ------------------------------------------------------------------
    def freeze_libretro_soft(self):
        """Freeze the libretro core without auto-saving.
        Used when a menu or overlay opens on top of ch03."""
        core = self._libretro_core
        if core and core.is_loaded and not core.is_frozen:
            core.freeze()
            print("[GAMEDECK] Soft-freeze: core paused (menu open)")

    def unfreeze_libretro_soft(self):
        """Unfreeze the libretro core without touching save state.
        Used when a menu or overlay closes on ch03."""
        core = self._libretro_core
        if core and core.is_loaded and core.is_frozen:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            muted = False
            if _main and hasattr(_main, "db") and _main.db:
                muted = _main.db.config.get("is_muted", False)
            core.unfreeze(muted=muted)
            print("[GAMEDECK] Soft-unfreeze: core resumed (menu closed)")

    # DVD PLAYER BUTTON MAPPING (DVD-ONLY).
    # Canonical playback keys the rest of _handle_dvd_input / _poll_dvd_hold are
    # written against. The DVD "Button Mapping" menu stores the user's chosen
    # physical keys under db.config["dvd_bindings"] as {action: key_code}; those
    # are translated back to these canonical keys ONLY inside the DVD player.
    # This is a deliberately separate namespace from remote_bindings and from
    # the WASD used for menu/console navigation, so remapping DVD transport
    # never changes WASD (or anything else) anywhere outside DVD playback.
    #
    # Deliberately OFF WASD: these used to be S/W/A/D, which are the exact
    # same physical keys _handle_dvd_menu_nav uses for on-disc menu
    # navigation (see that method + _render_dvd_player's menu auto-detect).
    # A one-frame misdetection of "in menu" vs "title playing" meant a
    # play/rewind/ff/caption press could get read as menu-nav (or vice
    # versa). Moving transport onto its own key row (Z/X/C/V) means the two
    # layers never share a physical key at all, so a misdetected frame just
    # does nothing instead of doing the wrong thing.
    # NOTE: these are INTERNAL canonical keys only -- _handle_dvd_input
    # always sees these values after _dvd_translate_key folds the user's
    # actual binding back onto them. The user-facing defaults (what new
    # installs start with) are media keys defined in _DVD_DEFAULTS below.
    DVD_ACTION_CANONICAL = {
        "play_pause":     _DVD_CANON_PLAY_PAUSE,
        "captions":       pygame.K_v,
        "rewind":         _DVD_CANON_REWIND,
        "fast_forward":   _DVD_CANON_FASTFORWARD,
        "return_to_menu": pygame.K_BACKSPACE,
    }

    # Default physical key bindings exposed to the user via the "Button
    # Mapping" popup. Separate from DVD_ACTION_CANONICAL so the user-facing
    # defaults (OS media keys) can differ from the internal canonical keys
    # without changing _handle_dvd_input. refresh_from_config falls back to
    # these when no override is saved; existing saved configs are untouched.
    _SDLK_MASK_DVD = 0x40000000
    _DVD_DEFAULTS = {
        "play_pause":     _SDLK_MASK_DVD | 261,  # SDL_SCANCODE_AUDIOPLAY  (VK_MEDIA_PLAY_PAUSE)
        "rewind":         _SDLK_MASK_DVD | 285,  # SDL_SCANCODE_AUDIOREWIND      (APPCOMMAND)
        "fast_forward":   _SDLK_MASK_DVD | 286,  # SDL_SCANCODE_AUDIOFASTFORWARD (APPCOMMAND)
        "return_to_menu": pygame.K_BACKSPACE,    # back to Backspace -- not every remote/keyboard
                                                   # has a dedicated Stop button, and this used to
                                                   # default to SDL_SCANCODE_AUDIOSTOP (VK_MEDIA_STOP),
                                                   # which left "Return to Main Menu" silently
                                                   # unreachable on hardware without one.
        "captions":       pygame.K_v,
    }

    # How long the DVD boot screen (logo + progress bar, see
    # _render_dvd_load_screen) is shown before the main video starts.
    DVD_LOAD_SCREEN_MS = 1600

    # Backup DVD .iso images go through libdvdnav (see
    # VLCEngineWrapper.play_file_segmented), which has to parse the disc's
    # VIDEO_TS IFO tables before anything can play -- slower than opening a
    # plain video file, and occasionally a malformed/non-video .iso will
    # never produce a playable state at all. If dvdnav hasn't reached
    # Playing/Paused within this many ms of _play_dvd_main() calling
    # play_file_segmented(), _render_dvd_player's watchdog treats it as a
    # failed open (bad disc image, missing libdvdcss for an encrypted
    # commercial disc, etc.) and backs out to the DVD list with an error
    # message instead of sitting on a frozen black screen.
    DVD_ISO_OPEN_TIMEOUT_MS = 9000

    # A definitive Ended/Error state right after play() can be a genuine
    # instant failure (e.g. TinyIsoTest.iso -- a synthetic test file with no
    # real VIDEO_TS/IFO structure, which dvdnav can't parse and immediately
    # reports Ended for -- confirmed in app_log.txt: state was already
    # Ended well before the 9s timeout, but the old watchdog only ever
    # checked the deadline, so the black screen sat there the full 9s
    # anyway) -- OR it can be a harmless transient VLC reports for a frame
    # or two while it's still spinning up the async open. This grace period
    # is how long to give VLC before trusting Ended/Error as real; after it,
    # Ended/Error bails out immediately rather than waiting for the full
    # DVD_ISO_OPEN_TIMEOUT_MS.
    DVD_ISO_OPEN_FAIL_GRACE_MS = 700

    # A disc's get_length()==0 (menu) vs >0 (title playing) reading can
    # blip for a single frame during a title transition (e.g. a menu that
    # briefly plays a short intro clip, or the moment the feature itself
    # starts). Require the new reading to hold steady for this long before
    # _render_dvd_player actually flips dvd_in_menu, so WASD's meaning
    # never flickers back and forth mid-transition.
    DVD_MENU_FLIP_DEBOUNCE_MS = 400

    # A Stopped/Ended reading from get_state() can be transient during a
    # legitimate in-disc domain jump (e.g. a DVD game branching choice like
    # "No Deal" that moves off the VTS title through the VMGM root) while
    # the video/audio decoders tear down and rebuild -- not just when the
    # disc has genuinely finished. Require the reading to hold for this
    # long before the main-video-ended watchdog acts on it, so a one- or
    # two-frame blip during a decoder rebuild doesn't get mistaken for the
    # movie actually ending and kick the player out to ROMLIST mid-game.
    DVD_END_STATE_GRACE_MS = 800

    def _dvd_translate_key(self, key):
        """Map a user-remapped DVD key back to its canonical transport key.
        Only the five DVD actions are ever remapped; every other key (ESC,
        RETURN, arrows, the remote FF/RW scancodes, etc.) passes through
        untouched. No-ops to identity when nothing is remapped."""
        bindings = getattr(self, "dvd_bindings", None)
        if not bindings:
            return key
        for action, canon in self.DVD_ACTION_CANONICAL.items():
            mapped = bindings.get(action)
            if mapped is not None and key == mapped:
                return canon
        return key

    def refresh_from_config(self, db_config):
        self._refresh_consoles(db_config.get("consoles_enabled", {}))
        if self.active_console and self.active_console not in self.consoles:
            self.mode = "BROWSE"
            self.active_console = None
        # Refresh DVD-only transport bindings (see DVD_ACTION_CANONICAL).
        # Missing entries fall back to _DVD_DEFAULTS (OS media keys for new
        # installs). Existing saved configs with custom keys are untouched
        # because stored.get(action) returns their saved value, not the default.
        stored = db_config.get("dvd_bindings", {}) or {}
        self.dvd_bindings = {
            action: stored.get(action, self._DVD_DEFAULTS.get(action, canon))
            for action, canon in self.DVD_ACTION_CANONICAL.items()
        }


# ==============================================================================
# PART 3a OF 13: REAL-TIME INPUT FREQUENCY GATES
# ==============================================================================

    def handle_event(self, event):
        if self.mode == "GAME":
            return False

        if self.dvd_playback_active or self.mode == "DVD_PLAYER":
            if event.type != pygame.KEYDOWN:
                return False
            self.mode = "DVD_PLAYER"
            return self._handle_dvd_input(event.key)

        action = self._event_to_action(event)

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_w:        action = "up"
            elif event.key == pygame.K_s:      action = "down"
            elif event.key == pygame.K_a:      action = "left"
            elif event.key == pygame.K_d:      action = "right"
            elif event.key == pygame.K_RETURN: action = "confirm"
            elif event.key == pygame.K_ESCAPE: action = "back"

        if action is None:
            return False

        if self.mode == "BROWSE":
            if action == "up":        self._cycle_console(-1)
            elif action == "down":    self._cycle_console(1)
            elif action == "confirm": self._enter_romlist()
            return True

# ==============================================================================
# PART 3b OF 13: UNTOUCHED SCROLL LIST MATRIX
# ==============================================================================

        if self.mode == "ROMLIST":
            if action == "up":
                if not self.roms or self.rom_index == 0:
                    self._cycle_console(-1)
                    console = self.consoles[self.console_index]
                    self.active_console = console
                    self.current_browse_dir = "" if console == "DVD" else os.path.join(self.roms_dir, console)
                    self._scan_roms(console)
                    self.rom_index = 0
                    self.rom_scroll = 0
                else:
                    self.rom_index = (self.rom_index - 1) % len(self.roms)
                return True

            elif action == "down":
                if not self.roms or self.rom_index == len(self.roms) - 1:
                    self._cycle_console(1)
                    console = self.consoles[self.console_index]
                    self.active_console = console
                    self.current_browse_dir = "" if console == "DVD" else os.path.join(self.roms_dir, console)
                    self._scan_roms(console)
                    self.rom_index = 0
                    self.rom_scroll = 0
                else:
                    self.rom_index = (self.rom_index + 1) % len(self.roms)
                return True

            elif action == "confirm":
                self._launch_selected_rom()
                return True

            elif action == "back":
                print("[GAMEDECK INFO] Escaped list. Returning to main console selector carousel.")
                self.mode = "BROWSE"
                return True

        return False


# ==============================================================================
# PART 4 OF 13: MULTIMEDIA TIMELINE DIALS & JOYSTICK HAT TRANSLATORS
# ==============================================================================

    def _poll_rom_hold(self):
        """Hold-to-scroll for the ROM / DVD-file list. Called every frame
        from _render_romlist. After an initial hold delay, moves rom_index
        continuously while W/Up or S/Down is held.

        Does NOT console-wrap during hold — wrapping to a different console
        only happens on a discrete keypress via handle_event(), so you can't
        accidentally fly past the end of a long list onto the next console."""
        if not self.roms:
            return

        now  = pygame.time.get_ticks()
        keys = pygame.key.get_pressed()

        held_up   = keys[pygame.K_w] or keys[pygame.K_UP]
        held_down = keys[pygame.K_s] or keys[pygame.K_DOWN]

        if not held_up and not held_down:
            self._rom_hold_dir   = 0
            self._rom_hold_start = 0
            self._rom_hold_last  = 0
            return

        direction = -1 if held_up else 1

        if direction != self._rom_hold_dir:
            # New direction just pressed — handle_event() already moved one
            # step on the KEYDOWN; just arm the hold timer here.
            self._rom_hold_dir   = direction
            self._rom_hold_start = now
            self._rom_hold_last  = now
            return

        _INITIAL_DELAY_MS = 380   # ms before repeat starts
        _REPEAT_RATE_MS   = 70    # ms between repeat steps while held

        if (now - self._rom_hold_start) < _INITIAL_DELAY_MS:
            return
        if (now - self._rom_hold_last) < _REPEAT_RATE_MS:
            return

        self._rom_hold_last = now

        if direction == -1 and self.rom_index > 0:
            self.rom_index -= 1
        elif direction == 1 and self.rom_index < len(self.roms) - 1:
            self.rom_index += 1

    def _poll_dvd_hold(self):
        if self.dvd_in_menu:
            # Menu-nav layer is discrete taps only (see _handle_dvd_menu_nav)
            # -- A/D mean menu left/right while a dvdnav menu is up, not
            # hold-to-rewind/fast-forward, so don't let a held key here seek.
            self._dvd_skip_hold_start = 0
            self._dvd_skip_dir = 0
            return

        now = pygame.time.get_ticks()
        keys = pygame.key.get_pressed()
        dir_held = 0

        # DVD-ONLY remap: honor the user's mapped rewind/fast-forward keys for
        # the hold-to-seek behavior. get_pressed() only reflects real keyboard
        # hardware, so a mapped physical keyboard key works here; keys that only
        # ever arrive as synthetic KEYDOWN (remote buttons) still drive seeking
        # through repeated taps in _handle_dvd_input, exactly as before.
        _b = getattr(self, "dvd_bindings", None) or {}
        _fwd_key = _b.get("fast_forward", self._DVD_DEFAULTS.get("fast_forward"))
        _rew_key = _b.get("rewind", self._DVD_DEFAULTS.get("rewind"))

        def _held(kc):
            # get_pressed() is indexed by key constant; a large SDLK-masked
            # mapped key can be out of range, so guard it.
            try:
                return bool(keys[kc])
            except (IndexError, TypeError):
                return False

        # Physical keyboard OR the live controller-held state published by
        # the XInput nav thread (pygame.key.get_pressed() only reflects real
        # keyboard hardware, never the synthetic/controller side). Only the
        # user's ACTUAL bound key (default: the media FF/RW buttons) drives
        # this -- literal 'c'/'z' keyboard-letter checks were removed here,
        # they caused holding the plain C/Z keys to always fast-skip even
        # when nothing was bound to those letters (same bug as the
        # single-tap double-mapping fixed in DVD_ACTION_CANONICAL above).
        if _held(_fwd_key) or _xi_nav_state.get("dvd_right_held", False): dir_held = 1
        elif _held(_rew_key) or _xi_nav_state.get("dvd_left_held", False): dir_held = -1

        if dir_held == 0:
            self._dvd_skip_hold_start = 0
            self._dvd_skip_dir = 0
            return

        if not hasattr(self, "_dvd_skip_hold_start"):
            self._dvd_skip_hold_start = now
            self._dvd_skip_dir = dir_held

        held_ms = now - self._dvd_skip_hold_start
        cooldown = getattr(self, "_dvd_skip_cooldown", 0)
        if held_ms > 500 and now > cooldown:
            vlc = self.vlc_engine
            if vlc:
                try:
                    t = vlc.player.get_time()
                    vlc.player.set_time(max(0, t + dir_held * 5000))
                    self._set_dvd_osd(">>" if dir_held > 0 else "<<")
                except Exception as e:
                    log.warning("DVD seek during held skip failed: %s", e)
            self.dvd_hud_until = now + 3000
            self._dvd_skip_cooldown = now + 400

    def _set_dvd_osd(self, msg, duration_ms=2000):
        self.dvd_osd_msg = msg
        self.dvd_osd_until = pygame.time.get_ticks() + duration_ms

    # Some remotes' dedicated FAST-FORWARD / REWIND buttons never generate a
    # normal pygame/VK keycode at all -- they arrive as a Windows
    # WM_APPCOMMAND message (APPCOMMAND_MEDIA_FASTFORWARD / _REWIND), which
    # the main app's OS hook layer translates into a synthetic KEYDOWN using
    # SDL_SCANCODE_AUDIOFASTFORWARD (286) / SDL_SCANCODE_AUDIOREWIND (285).
    # Recognize those alongside the existing D/A keyboard bindings so the
    # remote's FF/RW buttons drive DVD-menu seeking too.
    _SDLK_MASK = 0x40000000
    K_REMOTE_FASTFORWARD = _SDLK_MASK | 286
    K_REMOTE_REWIND       = _SDLK_MASK | 285

    # Semantic transport messages that get drawn as themed circular icons
    # instead of text. Anything not in this map (e.g. "CAPTIONS ON") still
    # renders as plain text OSD.
    _DVD_OSD_ICON_MAP = {
        "PLAYING":   "PLAY",
        "PAUSED":    "PAUSE",
        "<<":        "RW_FAST",     # held — continuous rewind
        ">>":        "FF_FAST",     # held — continuous fast-forward
        "<< -30s":   "RW",          # single tap — rewind 30s
        ">> +30s":   "FF",          # single tap — fast-forward 30s
    }

    def _draw_dvd_osd_icon(self, screen, cx, cy, icon_key, ui_col, radius=46):
        """Draws a themed transport icon: circle background in the theme
        color (was black), glyph stays pure white on top — same look as the
        reference art, just generated so it always matches theme_ui_hue."""
        white = (255, 255, 255)

        # Anti-aliased filled circle background (theme color)
        pygame.gfxdraw.aacircle(screen, cx, cy, radius, ui_col)
        pygame.gfxdraw.filled_circle(screen, cx, cy, radius, ui_col)

        def _tri(points):
            pts = [(round(px), round(py)) for px, py in points]
            pygame.gfxdraw.aapolygon(screen, pts, white)
            pygame.gfxdraw.filled_polygon(screen, pts, white)

        if icon_key == "PLAY":
            w, h = radius * 0.42, radius * 0.8
            _tri([(cx - w * 0.55, cy - h / 2), (cx - w * 0.55, cy + h / 2), (cx + w * 0.75, cy)])

        elif icon_key == "PAUSE":
            bar_w, bar_h, gap = radius * 0.24, radius * 0.8, radius * 0.16
            for sign in (-1, 1):
                bx = cx + sign * gap - (bar_w if sign < 0 else 0)
                pygame.draw.rect(screen, white,
                                  (round(bx), round(cy - bar_h / 2), round(bar_w), round(bar_h)),
                                  border_radius=3)

        elif icon_key in ("RW", "RW_FAST"):
            n = 2 if icon_key == "RW" else 3
            tri_w, tri_h, spacing = radius * 0.36, radius * 0.56, radius * 0.34
            start_x = cx + spacing * (n - 1) / 2
            for i in range(n):
                tx = start_x - i * spacing
                _tri([(tx + tri_w / 2, cy - tri_h / 2), (tx + tri_w / 2, cy + tri_h / 2), (tx - tri_w / 2, cy)])

        elif icon_key in ("FF", "FF_FAST"):
            n = 2 if icon_key == "FF" else 3
            tri_w, tri_h, spacing = radius * 0.36, radius * 0.56, radius * 0.34
            start_x = cx - spacing * (n - 1) / 2
            for i in range(n):
                tx = start_x + i * spacing
                _tri([(tx - tri_w / 2, cy - tri_h / 2), (tx - tri_w / 2, cy + tri_h / 2), (tx + tri_w / 2, cy)])

    def _handle_dvd_menu_nav(self, key):
        """DVD MENU-NAV LAYER: called unconditionally for W/A/S/D/RETURN by
        _handle_dvd_input, regardless of dvd_in_menu (see that call site's
        comment for why the old dvd_in_menu gate had to be removed -- it
        froze input on any game/submenu authored as a real VTS Title).
        Maps WASD/arrows to dvdnav's up/down/left/right and RETURN/Enter to
        activate, via VLCEngineWrapper.navigate() -- see that method's
        comment for how libdvdnav resolves this against the disc's own menu
        button layout. It's a harmless no-op when there's no active
        highlighted button on screen (ordinary movie playback)."""
        vlc = self.vlc_engine
        if vlc is None:
            return True

        _dir = None
        if key in (pygame.K_w, pygame.K_UP):
            _dir = "up"
        elif key in (pygame.K_s, pygame.K_DOWN):
            _dir = "down"
        elif key in (pygame.K_a, pygame.K_LEFT):
            _dir = "left"
        elif key in (pygame.K_d, pygame.K_RIGHT):
            _dir = "right"
        elif key == pygame.K_RETURN:
            _dir = "activate"

        if _dir is not None:
            print(f"[DVD DEBUG] Menu-nav: {_dir}")
            vlc.navigate(_dir)
            if _dir == "activate":
                # Deliberately does NOT force dvd_in_menu here anymore.
                # activate fires identically whether the button just chosen
                # leads to a submenu (still menu domain -- WASD should stay
                # menu-nav) or the real movie (transport should take over):
                # forcing dvd_in_menu False on every activate press is what
                # left WASD dead in submenus. Now get_title() is confirmed
                # reliable (see VLCEngineWrapper.is_menu_active) -- 0 on any
                # menu screen, a real positive index once the movie is
                # actually playing -- so the MENU-VS-TITLE AUTO-DETECT in
                # _render_dvd_player is left to make this call on its own,
                # in both directions.
                #
                # Clearing the manual override here specifically: if the
                # return-to-menu confirm forced dvd_in_menu True (see
                # _handle_dvd_input), that freeze needs to end the moment
                # the user actually picks something off the resulting
                # popup menu (e.g. Resume) -- otherwise the auto-detect
                # stays frozen out forever and WASD never gets transport
                # back. Harmless no-op if the flag wasn't set.
                self._dvd_menu_manual_override = False

        return True



    def _dvd_menu_is_up(self):
        """SINGLE SOURCE OF TRUTH for "is an interactive disc menu currently
        on screen right now".

        This is the one signal the whole channel-03 DVD control contract
        keys off of:
          * WASD + Enter are LIVE only when this returns True (navigate the
            disc menu / select a highlighted button).
          * The z/x/c/v transport keys are LIVE only when this returns False
            (a real title/feature is actually playing and is seekable).
          * Backspace's "return to main menu" only ever applies to a disc.

        A plain (non-.iso) video file has no menu system at all, so this is
        always False for it -- that's what makes WASD/Enter/Backspace inert
        on a regular file, exactly as intended.

        IMPLEMENTATION NOTE: on the current libVLC dvdnav:// path this reads
        the debounced dvd_in_menu flag (derived from libVLC title-index
        heuristics in _render_dvd_player) plus any deliberate manual
        override. That heuristic is NOT fully reliable on game discs that
        author their menus as real VTS titles."""
        if not self.dvd_is_disc_image:
            return False
        if getattr(self, "_dvd_menu_manual_override", False):
            return True
        return bool(self.dvd_in_menu)

    def _handle_dvd_input(self, key):
        vlc = self.vlc_engine

        # DVD-ONLY remap: fold any user-mapped transport key back onto its
        # canonical key BEFORE the literal K_s/K_w/K_a/K_d checks below. Scoped
        # entirely to DVD playback — see _dvd_translate_key / DVD_ACTION_CANONICAL.
        _raw_key = key
        key = self._dvd_translate_key(key)
        # Unconditional (not gated behind a debug flag): only fires once per
        # actual DVD button press, never per-frame, so it's safe to leave on
        # permanently. Gives full visibility into what a given physical
        # button is actually sending -- e.g. whether the Rewind/FF buttons
        # arrive here at all, and if so, whether translation matched them to
        # the bound action. Bound key for rewind/fast_forward/play_pause
        # comes from self.dvd_bindings (see refresh_from_config).
        print(f"[DVD INPUT DEBUG] raw_key={_raw_key} -> translated_key={key} "
              f"bindings={getattr(self, 'dvd_bindings', None)}", flush=False)

        # ------------------------------------------------------------------
        # CONFIRM POPUPS (quit-to-TV via ESC, back-to-main via Backspace).
        # While either is open the ONLY live keys are A / D (move the Yes/No
        # highlight) and Enter (commit). NO other key does anything -- the
        # popup demands an explicit choice. ESC/Backspace deliberately do
        # NOT cancel here (a previous revision let ESC close it; per the
        # control contract the user must pick Yes/No). Left/Right are the
        # program's VOLUME keys and are intentionally NOT selectors -- the
        # main event valve in retro_tv_emulator.py already swallows volume
        # while a popup is open, so they never reach here anyway; this just
        # makes the deck side agree.
        # ------------------------------------------------------------------
        if self.dvd_exit_confirm:
            if key == pygame.K_a:
                self.dvd_exit_selection = "Yes"
                print("[DVD INPUT] Quit-confirm: selection -> Yes")
            elif key == pygame.K_d:
                self.dvd_exit_selection = "No"
                print("[DVD INPUT] Quit-confirm: selection -> No")
            elif key == pygame.K_RETURN:
                print(f"[DVD INPUT] Quit-confirm: committing '{self.dvd_exit_selection}'")
                if self.dvd_exit_selection == "Yes":
                    if vlc:
                        try: vlc.stop()
                        except Exception as e:
                            log.warning("VLC stop failed while confirming DVD exit: %s", e)
                    self.dvd_exit_confirm = False
                    self.dvd_playback_active = False
                    self.dvd_splash_playing = False
                    self.dvd_splash_done = False
                    self.dvd_paused_frame = None
                    self.dvd_in_menu = False
                    self.mode = "ROMLIST"
                    self._scan_roms("DVD")
                else:
                    self.dvd_exit_confirm = False
            else:
                print(f"[DVD INPUT] Quit-confirm open: ignoring key {key} (only A/D/Enter are live).")
            return True

        # RETURN-TO-MAIN-MENU confirm dialog: opened by Backspace while the
        # movie is actually playing (see the Backspace handler below). Same
        # A/D-select, Enter-confirm pattern as dvd_exit_confirm above, just
        # with a different action on "Yes". Backspace does NOT cancel it --
        # a choice must be made.
        if self.dvd_return_menu_confirm:
            if key == pygame.K_a:
                self.dvd_return_menu_selection = "Yes"
                print("[DVD INPUT] Back-to-main-confirm: selection -> Yes")
            elif key == pygame.K_d:
                self.dvd_return_menu_selection = "No"
                print("[DVD INPUT] Back-to-main-confirm: selection -> No")
            elif key == pygame.K_RETURN:
                print(f"[DVD INPUT] Back-to-main-confirm: committing '{self.dvd_return_menu_selection}'")
                if self.dvd_return_menu_selection == "Yes":
                    self.dvd_return_menu_confirm = False
                    self.dvd_in_menu = True
                    self._dvd_menu_flip_candidate = True
                    self._dvd_menu_flip_since = pygame.time.get_ticks()
                    self._dvd_menu_manual_override = True
                    if vlc:
                        try:
                            vlc.navigate("menu")
                        except Exception as e:
                            log.warning("DVD return-to-menu navigate failed: %s", e)
                    print("[DVD INPUT] Back-to-main confirmed "
                          "(WASD now controls menu navigation).")
                else:
                    self.dvd_return_menu_confirm = False
            else:
                print(f"[DVD INPUT] Back-to-main-confirm open: ignoring key {key} (only A/D/Enter are live).")
            return True

        if self.dvd_splash_playing:
            return True

        # is_iso: this DVD source is a real disc image (.iso, opened via
        #   dvdnav) that HAS a menu system. False for a plain video file.
        # menu_up: an interactive disc menu is currently on screen (see
        #   _dvd_menu_is_up -- always False for a plain video file).
        # These two drive the entire key contract below.
        is_iso  = self.dvd_is_disc_image
        menu_up = self._dvd_menu_is_up()

        # ESC: open the quit-to-TV confirm. Universal -- works for a plain
        # video file and an ISO alike (it's how you leave the DVD player).
        if key == pygame.K_ESCAPE:
            self.dvd_exit_confirm = True
            self.dvd_exit_selection = "No"
            print(f"[DVD INPUT] ESC -> opening quit-to-TV confirm (is_iso={is_iso}).")
            return True

        # BACKSPACE: open the back-to-main-menu confirm. ISO-ONLY -- a plain
        # video file has no disc menu to return to, so Backspace does
        # nothing for it (per the control contract, Backspace must ONLY ever
        # do this and nothing else). On an ISO: if an interactive menu is
        # already up there's nothing to lose, so re-issue the return-to-root
        # command immediately; if a title is playing, open the confirm first
        # so the feature isn't cut off without warning.
        if key == pygame.K_BACKSPACE:
            if not is_iso:
                print("[DVD INPUT] Backspace ignored (regular video file has no disc menu).")
                return True
            if menu_up:
                self.dvd_in_menu = True
                self._dvd_menu_flip_candidate = True
                self._dvd_menu_flip_since = pygame.time.get_ticks()
                self._dvd_menu_manual_override = True
                if vlc:
                    try:
                        vlc.navigate("menu")
                    except Exception as e:
                        log.warning("DVD return-to-menu navigate failed: %s", e)
                print("[DVD INPUT] Backspace -- already on a menu, re-issued return-to-root.")
            else:
                self.dvd_return_menu_confirm = True
                self.dvd_return_menu_selection = "No"
                print("[DVD INPUT] Backspace -> opening back-to-main-menu confirm.")
            return True

        # ------------------------------------------------------------------
        # DVD MENU NAVIGATION -- W / A / S / D + Enter.
        # These are LIVE ONLY on an ISO while an interactive disc menu is
        # actually on screen (menu_up). In every other situation they do
        # NOTHING:
        #   * regular video file  -> no menu ever, so inert
        #   * ISO, title playing  -> inert (must NOT skip chapters, resume,
        #                            or otherwise disturb playback -- that
        #                            was the reported "nav keys skip / Enter
        #                            skips during video" bug)
        # The event is still consumed (return True) so it can never bleed
        # through to a global handler.
        #
        # menu_up must be a reliable "a highlighted button is on screen"
        # signal for this to feel right on every disc -- see _dvd_menu_is_up.
        # Only WASD are nav keys; the arrow keys are the program's global
        # channel/volume controls and never reach here (the channel-03 valve
        # in retro_tv_emulator.py keeps them global).
        # ------------------------------------------------------------------
        if key in (pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d, pygame.K_RETURN):
            if is_iso and menu_up:
                print(f"[DVD INPUT] Menu-nav key {key} accepted (menu is up).")
                return self._handle_dvd_menu_nav(key)
            print(f"[DVD INPUT] Nav key {key} ignored "
                  f"(is_iso={is_iso} menu_up={menu_up} -- nothing to navigate).")
            return True

        # TRANSPORT keys (rewind/play-pause/fast-forward/captions) are live
        # during real video playback. On an ISO they are suppressed while a
        # menu is up -- a menu screen isn't seekable, exactly like a real
        # DVD player where transport does nothing on the menu.
        if is_iso and menu_up and key in (
                _DVD_CANON_REWIND, _DVD_CANON_PLAY_PAUSE, _DVD_CANON_FASTFORWARD, pygame.K_v):
            print(f"[DVD INPUT] Transport key {key} ignored (menu is up -- not seekable).")
            return True

        if key == _DVD_CANON_PLAY_PAUSE:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            if vlc:
                try:
                    vlc.player.pause()
                    self.dvd_paused = not self.dvd_paused
                    self._set_dvd_osd("PAUSED" if self.dvd_paused else "PLAYING")
                except Exception as e:
                    log.warning("DVD pause/resume toggle failed: %s", e)
            return True

        if key == pygame.K_v:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            self.dvd_captions = not self.dvd_captions
            if vlc:
                try:
                    if not self.dvd_captions:
                        vlc.player.video_set_spu(-1)
                        self._set_dvd_osd("CAPTIONS OFF")
                    else:
                        chosen_id = None
                        spu_desc = vlc.player.video_get_spu_description()
                        if spu_desc:
                            for track_id, _name in spu_desc:
                                if track_id != -1:
                                    chosen_id = track_id
                                    break
                        if chosen_id is None and self.dvd_file_path:
                            base = os.path.splitext(self.dvd_file_path)[0]
                            for sub_ext in ['.srt', '.ass', '.ssa', '.vtt', '.sub', '.smi']:
                                candidate = base + sub_ext
                                if os.path.exists(candidate):
                                    try:
                                        import vlc as _vlc_mod
                                        file_uri = 'file:///' + candidate.replace('\\', '/').lstrip('/')
                                        vlc.player.add_slave(_vlc_mod.MediaSlaveType.subtitle, file_uri, True)
                                        pygame.time.wait(300)
                                        spu_desc2 = vlc.player.video_get_spu_description()
                                        if spu_desc2:
                                            for track_id, _name in spu_desc2:
                                                if track_id != -1:
                                                    chosen_id = track_id
                                                    break
                                    except Exception as sub_err:
                                        print(f"[DVD CAPTIONS] Sidecar load error: {sub_err}")
                                    break
                        if chosen_id is not None:
                            vlc.player.video_set_spu(chosen_id)
                            self._set_dvd_osd("CAPTIONS ON")
                        else:
                            self.dvd_captions = False
                            self._set_dvd_osd("NO CAPTIONS AVAILABLE")
                except Exception as e:
                    print(f"[DVD CAPTIONS ERROR] {e}")
            else:
                self._set_dvd_osd("CAPTIONS ON" if self.dvd_captions else "CAPTIONS OFF")
            return True

        if key == _DVD_CANON_FASTFORWARD or key == self.K_REMOTE_FASTFORWARD:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            if vlc:
                try:
                    t = vlc.player.get_time()
                    vlc.player.set_time(max(0, t + 30000))
                    self._set_dvd_osd(">> +30s")
                    self._dvd_skip_hold_start = pygame.time.get_ticks()
                    self._dvd_skip_dir = 1
                except Exception as e:
                    log.warning("DVD +30s skip failed: %s", e)
            return True

        if key == _DVD_CANON_REWIND or key == self.K_REMOTE_REWIND:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            if vlc:
                try:
                    t = vlc.player.get_time()
                    vlc.player.set_time(max(0, t - 30000))
                    self._set_dvd_osd("<< -30s")
                    self._dvd_skip_hold_start = pygame.time.get_ticks()
                    self._dvd_skip_dir = -1
                except Exception as e:
                    log.warning("DVD -30s skip failed: %s", e)
            return True

        # RETURN / Enter is deliberately NOT bound to anything during normal
        # (non-menu) playback. Enter is a select key: it activates a menu
        # option while dvd_in_menu is True (see _handle_dvd_menu_nav) and
        # does nothing when there's no menu on screen to select from. X is
        # the dedicated pause/play key -- do not re-add a pause binding
        # here, that was the exact behavior reported as a bug (Enter
        # pausing playback / cutting off menu audio when it should only
        # ever select).

        return True

    def _event_to_action(self, event):
        if event.type == pygame.JOYHATMOTION:
            x, y = event.value if isinstance(event.value, tuple) else (0, event.value)
            if y == 1:  return "up"
            if y == -1: return "down"
            if x == -1: return "left"
            if x == 1:  return "right"
        elif event.type == pygame.JOYAXISMOTION:
            if event.axis == 1:   # left stick Y
                if event.value < -0.6: return "up"
                if event.value > 0.6:  return "down"
            elif event.axis == 0: # left stick X
                if event.value < -0.6: return "left"
                if event.value > 0.6:  return "right"
        elif event.type == pygame.JOYBUTTONDOWN:
            if event.button == 0: return "confirm"   # A
            if event.button == 1: return "back"      # B
        return None


# ==============================================================================
# PART 5 OF 13: MEDIA EXPLORER FILE DIRECTORY & LAUNCH ENGINES
# ==============================================================================

    def _cycle_console(self, delta):
        if not self.consoles: return
        self.console_index = (self.console_index + delta) % len(self.consoles)

    def _enter_romlist(self):
        if not self.consoles: return
        console = self.consoles[self.console_index]
        self.active_console = console
        if console == "DVD":
            self.current_browse_dir = getattr(self, "dvd_last_dir", "")
        else:
            self.current_browse_dir = os.path.join(self.roms_dir, console)
        self._scan_roms(console)
        self.rom_index = 0
        self.rom_scroll = 0
        self.mode = "ROMLIST"

    def _first_content_index(self):
        """Return the index of the first non-back-button entry after a
        _scan_roms() call, so navigation always lands on real browsable
        content instead of the '[.. BACK ...]' sentinel at index 0.
        Falls back to 0 when the list is empty or has no back sentinel."""
        if not self.roms:
            return 0
        if self.roms[0][0].startswith("[.. "):
            return 1 if len(self.roms) > 1 else 0
        return 0

    def _dvd_folder_has_content(self, folder_path, exts):
        """Shallow check: does this folder contain at least one DVD-playable
        file (extension in exts) or at least one subfolder (which might
        contain files deeper)? Used to hide empty / non-video directories
        from the DVD browser so only folders worth navigating are shown.
        One scandir call per folder — fast on SSDs, acceptable on HDDs."""
        try:
            with os.scandir(folder_path) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            return True   # has subdirs → might contain files
                        if entry.is_file():
                            ext = os.path.splitext(entry.name.lower())[1]
                            if ext in exts:
                                return True   # direct match found
                    except OSError:
                        continue
        except OSError:
            pass
        return False

    def _get_windows_drives(self):
        import ctypes
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if bitmask & 1:
                drives.append(f"{letter}:\\")
            bitmask >>= 1
        return drives

    def _scan_roms(self, console):
        self.roms = []
        target_folder = getattr(self, "current_browse_dir", "")

        if console == "DVD":
            self.dvd_last_dir = target_folder

        exts = ROM_EXTENSIONS.get(console, [])

        try:
            if console == "DVD" and target_folder == "":
                drives_pool = self._get_windows_drives()
                for drive in drives_pool:
                    self.roms.append((f"[DRIVE] {drive}", drive))
                self.status_msg = ""
                pygame.event.clear(pygame.KEYDOWN)
                return

            if os.path.exists(target_folder):
                dir_entries = []
                file_entries = []

                if console == "DVD":
                    if len(os.path.abspath(target_folder).strip("\\")) == 2:
                        dir_entries.append(("[.. BACK TO THIS PC]", ""))
                    else:
                        parent_path = os.path.dirname(target_folder.rstrip("\\"))
                        if parent_path.endswith(":"): parent_path += "\\"
                        dir_entries.append(("[.. BACK TO PREVIOUS FOLDER]", parent_path))
                else:
                    fallback_root = os.path.join(self.roms_dir, console)
                    if os.path.abspath(target_folder) != os.path.abspath(fallback_root):
                        parent_path = os.path.dirname(target_folder)
                        dir_entries.append(("[.. BACK TO PREVIOUS FOLDER]", parent_path))

                # os.scandir() reads the directory once and carries each
                # entry's is_dir() flag from that single read, avoiding the
                # per-entry os.path.isdir() stat() syscall the old
                # os.listdir() loop did. Those synchronous stat calls were the
                # "Not Responding" freeze when opening large or slow (optical /
                # network) folders on the game channel's DVD browser.
                with os.scandir(target_folder) as _it:
                    scanned = sorted(_it, key=lambda e: e.name.lower())
                for entry in scanned:
                    name = entry.name
                    full_path = entry.path
                    if name.startswith("$") or name.startswith("."):
                        continue
                    # ROM consoles are flat lists of game files — any
                    # subfolder (saves/, border/, or anything else a
                    # player drops in there) is internal bookkeeping, not
                    # a browsable game, so it's hidden entirely. DVD is
                    # the one console that's a real filesystem browser and
                    # still needs its folders visible/navigable.
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    if is_dir:
                        if console == "DVD":
                            # Only show folders that contain at least one
                            # video/iso file (directly) or a subfolder —
                            # this hides directories full of unrelated files
                            # (photos, documents, etc.) so the browser only
                            # surfaces folders worth navigating into.
                            if self._dvd_folder_has_content(full_path, set(exts)):
                                dir_entries.append((f"[DIR] {name}", full_path))
                        continue
                    base_name_only, ext_only = os.path.splitext(name)
                    ext_str = str(ext_only).lower()
                    if not exts or ext_str in exts:
                        file_entries.append((str(base_name_only), full_path))

                if console == "PSX" and file_entries:
                    # bin/cue rips: the .cue is what the core actually needs
                    # to read the disc TOC — loading the bare .bin directly
                    # is a classic silent black-screen failure. If a game has
                    # both, hide the .bin from the list so it can't be picked
                    # by mistake; the .cue is still the way in.
                    cue_stems = {
                        os.path.splitext(os.path.basename(p))[0].lower()
                        for _n, p in file_entries
                        if os.path.splitext(p)[1].lower() == ".cue"
                    }
                    if cue_stems:
                        file_entries = [
                            (n, p) for n, p in file_entries
                            if not (os.path.splitext(p)[1].lower() == ".bin"
                                     and os.path.splitext(os.path.basename(p))[0].lower() in cue_stems)
                        ]

                self.roms = dir_entries + file_entries
                self.status_msg = "" if self.roms else f"No files found in: {console}"
        except Exception as e:
            self.status_msg = "Access Error"
            print(f"[GAMEDECK CRITICAL BUG] Directory index scan failed! Error details: {e}")

        # If the directory read above stalled at all (slow/network/optical
        # drive), Windows keeps queuing keystrokes while the window is marked
        # "Not Responding". Drop those stale nav keydowns so they can't "catch
        # up" and jump the ROM/DVD list to a different console or page the
        # instant the scan returns. Mirrors the existing flush already done
        # after DVD launch and libretro core unload.
        pygame.event.clear(pygame.KEYDOWN)


# ==============================================================================
# PART 6a OF 13: SEQUENTIAL DVD MEDIA ROUTER
# ==============================================================================

    def _launch_selected_rom(self):
        if not self.roms: return
        display_name, full_path = self.roms[self.rom_index]

        if display_name == "[.. BACK TO THIS PC]":
            self.current_browse_dir = ""
            self._scan_roms(self.active_console)
            self.rom_index = self._first_content_index()
            self.rom_scroll = 0
            return

        if display_name == "[.. BACK TO PREVIOUS FOLDER]":
            self.current_browse_dir = full_path
            self._scan_roms(self.active_console)
            self.rom_index = self._first_content_index()
            self.rom_scroll = 0
            return

        if display_name.startswith("[DRIVE] ") or display_name.startswith("[DIR] "):
            # Opening any drive or folder drops you into a listing whose first entry
            # is always the "[.. BACK ...]" go-back button. Start the highlight one
            # down -- on the first real file/folder -- so you land straight on
            # browsable content instead of the back button, for EVERY layer you
            # navigate, in both directions (see _first_content_index).
            self.current_browse_dir = full_path
            self._scan_roms(self.active_console)
            self.rom_index = self._first_content_index()
            self.rom_scroll = 0
            return

        console = self.active_console
        if console == "DVD":
            self._start_dvd_playback(full_path)
            pygame.event.clear(pygame.KEYDOWN)
            return

        # Prefer libretro core (.dll) if available; fall back to .exe
        if _LIBRETRO_AVAILABLE and self._has_libretro_core(console):
            self._launch_libretro(console, full_path)
        else:
            exe = self._resolve_emulator_exe(console)
            if exe: self._launch_emulator(exe, full_path, console)

    def _start_dvd_playback(self, video_path):
        import sys
        main_module = sys.modules.get("__main__")

        if main_module and hasattr(main_module, "initialize_vlc_on_demand"):
            main_module.initialize_vlc_on_demand()

        # DVD playback uses the dedicated background DVD engine, never the
        # shared main engine -- so the disc can stay alive (muted) when the
        # user tunes away and the main engine is free to play other channels.
        vlc = getattr(main_module, "dvd_engine", None)

        if vlc is None:
            print("[GAMEDECK BUG ERROR] Playback blocked: dvd_engine link is currently None!")
            return

        self.vlc_engine = vlc
        self.dvd_file_path = video_path
        self.mode = "DVD_PLAYER"
        self.dvd_playback_active = True

        self.dvd_paused = False
        self.dvd_paused_frame = None
        self.dvd_captions = False
        self.dvd_exit_selection = "No"
        self.dvd_return_menu_confirm = False
        self.dvd_return_menu_selection = "No"
        self.dvd_osd_msg = ""
        self.dvd_osd_until = 0

        # DVD MENU-NAV LAYER RESET (see _handle_dvd_menu_nav / dvd_in_menu
        # comment block above). A .iso backup may open on an interactive
        # dvdnav menu, so assume menu-nav controls to start -- the
        # get_length()-based auto-detect in _render_dvd_player flips
        # dvd_in_menu to False the moment the feature itself starts
        # playing. A plain video file (.mp4/.mkv/etc) has no menu at all,
        # so it never enters menu-nav mode.
        self.dvd_is_disc_image = os.path.splitext(video_path)[1].lower() == ".iso"
        self.dvd_in_menu = self.dvd_is_disc_image
        self._dvd_menu_flip_since = 0
        self._dvd_menu_flip_candidate = None
        self._dvd_menu_manual_override = False
        self.dvd_was_in_menu_on_leave = False
        # ISO-open watchdog (see DVD_ISO_OPEN_TIMEOUT_MS) is armed by
        # _play_dvd_main once play_file_segmented() actually hands the
        # disc image to dvdnav, not here -- nothing has been opened yet.
        self._dvd_iso_watchdog_armed = False
        self._dvd_iso_watchdog_deadline = 0
        print(f"[DVD DEBUG] Launch: {os.path.basename(video_path)} "
              f"is_disc_image={self.dvd_is_disc_image} dvd_in_menu={self.dvd_in_menu}")

        try: vlc.stop()
        except Exception as e:
            log.warning("VLC stop before DVD load screen failed: %s", e)
        try: vlc.clear_frame_buffer()
        except Exception as e:
            log.warning("VLC clear_frame_buffer before DVD load screen failed: %s", e)

        # Show the synthetic load screen (DVD logo + progress bar, same
        # layout/metrics as the game console loading screens — see
        # _render_dvd_load_screen) for DVD_LOAD_SCREEN_MS, then start the
        # real video. This replaces the old DVD_LoadVideo.mp4 splash clip;
        # dvd_splash_playing/dvd_splash_done still gate the same control
        # flow in _render_dvd_player / _handle_dvd_input as before.
        self.dvd_splash_playing = True
        self.dvd_splash_done = False
        self._dvd_splash_start_time = pygame.time.get_ticks()
        self._dvd_main_ever_played = False
        print("[DVD] Showing load screen before starting playback.")


# ==============================================================================
# PART 6b OF 13: FIXED MEDIA INTERCONNECT LINES
# ==============================================================================

    def _play_dvd_main(self):
        vlc = self.vlc_engine
        if vlc is None or not self.dvd_file_path:
            return
        try:
            # The real movie is actual video content, not decorative UI — it
            # should behave exactly like a TV channel: match whatever aspect
            # ratio was detected from Windows (vlc.target_aspect), with VLC
            # adding proper letterbox/pillarbox padding to preserve the
            # movie's own aspect ratio within that shape. Re-apply it here
            # in case the splash intro cleared it for its own stretch-to-fill
            # look (see _start_dvd_playback).
            try:
                vlc.player.video_set_aspect_ratio(getattr(vlc, "target_aspect", None))
            except Exception as e:
                log.warning("Restoring VLC aspect ratio for DVD main video failed: %s", e)
            print(f"[DVD] Now playing main video file: {os.path.basename(self.dvd_file_path)}")
            _gain = 1.0
            if callable(self.dvd_gain_lookup):
                try:
                    _gain = self.dvd_gain_lookup(self.dvd_file_path)
                except Exception as e:
                    log.warning("dvd_gain_lookup failed: %s", e)
            _handed_to_vlc = vlc.play_file_segmented(self.dvd_file_path, 0, gain=_gain)
            # Apply the user's global volume/mute to the dedicated DVD engine
            # so the disc is audible at the right level from the first frame.
            # play_file_segmented() doesn't set volume itself (it leaves that
            # to the caller), and this engine is separate from the main one,
            # so nothing else has applied the slider to it yet.
            try:
                import sys as _sys
                _main = _sys.modules.get("__main__")
                if _main and getattr(_main, "db", None) is not None:
                    vlc.set_volume(_main.db.config.get("global_volume", 70),
                                   _main.db.config.get("is_muted", False))
            except Exception as _ve:
                log.warning("Applying global volume to DVD engine on launch failed: %s", _ve)
            self.mode = "DVD_PLAYER"
            self.dvd_playback_active = True
            # Arm the ISO-open watchdog (see DVD_ISO_OPEN_TIMEOUT_MS) only
            # for disc images and only once VLC actually accepted the
            # media -- _render_dvd_player's watchdog check backs out to the
            # DVD list with an error message if dvdnav never reaches a
            # Playing/Paused state in time (bad/corrupt image, encrypted
            # commercial disc missing libdvdcss, etc.) instead of sitting
            # on a frozen black screen.
            if self.dvd_is_disc_image and _handed_to_vlc:
                self._dvd_iso_watchdog_armed = True
                self._dvd_iso_watchdog_armed_at = pygame.time.get_ticks()
                self._dvd_iso_watchdog_deadline = self._dvd_iso_watchdog_armed_at + self.DVD_ISO_OPEN_TIMEOUT_MS
                print(f"[DVD DEBUG] ISO-open watchdog armed, deadline in {self.DVD_ISO_OPEN_TIMEOUT_MS}ms.")
            else:
                self._dvd_iso_watchdog_armed = False
                if self.dvd_is_disc_image:
                    print(f"[DVD DEBUG] Disc image but play_file_segmented() reported failure -- watchdog NOT armed.")
        except Exception as e:
            print(f"[DVD] Failed to play {self.dvd_file_path}: {e}")

    def _resolve_emulator_exe(self, console):
        folder = os.path.join(self.cores_dir, console)
        print(f"[GAMEDECK RESOLVE] Looking for {console} emulator in: {folder}")
        chosen = self.emulator_config.get(console)

        candidates = []
        try:
            for root, dirs, files in os.walk(folder):
                for name in files:
                    if name.lower().endswith(".exe"):
                        candidates.append(os.path.join(root, name))
        except Exception as e:
            print(f"[GAMEDECK RESOLVE] Walk error: {e}")

        print(f"[GAMEDECK RESOLVE] Found {len(candidates)} exe(s): {[os.path.basename(c) for c in candidates]}")
        if not candidates:
            print(f"[GAMEDECK RESOLVE] No exe found. Put emulator in {folder}")
            return None

        if chosen:
            for c in candidates:
                if os.path.basename(c).lower() == chosen.lower():
                    print(f"[GAMEDECK RESOLVE] Using saved config: {c}")
                    return c
            print(f"[GAMEDECK RESOLVE] Saved name {chosen!r} not found in tree, auto-detecting.")

        gui = [c for c in candidates if "sdl" not in os.path.basename(c).lower()]
        pool = gui if gui else candidates
        pool.sort(key=lambda p: os.path.getsize(p), reverse=True)
        print(f"[GAMEDECK RESOLVE] Auto-selected: {pool[0]}")
        return pool[0]

    def reload_emulator_config(self):
        self.emulator_config = self._load_emulator_config()
        print(f"[GAMEDECK] Emulator config reloaded: {self.emulator_config}")


# ==============================================================================
# PART 7 OF 13: OS EMULATOR SUBPROCESS LAUNCH ENGINE
# ==============================================================================

    def _patch_mgba_config(self, exe_dir):
        """Patch mGBA config.ini in two locations:
          1. Next to the exe (exe_dir/config.ini) — local override
          2. %APPDATA%/mGBA/config.ini — roaming profile (takes priority on some builds)

        Sets fullscreen=1, audioDriver=sdl, allowBackgroundInput=1, and the
        keyboard bindings that match DEFAULT_GBA_MAPPING so GetAsyncKeyState
        picks up the SendInput events from the controller watcher.
        """
        # Qt key integer values for the keys in DEFAULT_GBA_MAPPING:
        #   Letter keys: Qt value == ASCII value (e.g. 'X'=88, 'Z'=90)
        #   Special: Qt::Key_Return=16777221, Key_Backspace=16777219,
        #            Key_Up=16777235, Key_Down=16777237,
        #            Key_Left=16777234, Key_Right=16777236
        targets = {
            "ports.qt": {
                "fullscreen":           "1",
                "audiodriver":          "sdl",
                "allowbackgroundinput": "1",
                "key.a":                "90",        # Z key  (VK_Z  0x5A)
                "key.b":                "88",        # X key  (VK_X  0x58)
                "key.l":                "65",        # A key  (VK_A  0x41)
                "key.r":                "83",        # S key  (VK_S  0x53)
                "key.start":            "16777221",  # Return (VK_RETURN 0x0D)
                "key.select":           "16777219",  # Back   (VK_BACK  0x08)
                "key.up":               "16777235",  # Up     (VK_UP   0x26)
                "key.down":             "16777237",  # Down   (VK_DOWN 0x28)
                "key.left":             "16777234",  # Left   (VK_LEFT 0x25)
                "key.right":            "16777236",  # Right  (VK_RIGHT 0x27)
            },
            "": {
                "allowbackgroundinput": "1",
            },
        }

        # Build the list of paths to patch — always do the exe-local one,
        # and also the APPDATA roaming profile if it exists.
        paths_to_patch = [os.path.join(exe_dir, "config.ini")]
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            roaming = os.path.join(appdata, "mGBA", "config.ini")
            if os.path.exists(roaming):
                paths_to_patch.append(roaming)
                print(f"[GAMEDECK] Also patching roaming config: {roaming}")

        def _patch_one(config_path):
            try:
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                else:
                    lines = []

                current_section = None
                patched = set()
                new_lines = []

                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        current_section = stripped[1:-1].lower()
                        new_lines.append(line)
                        continue
                    if (current_section in targets
                            and "=" in stripped
                            and not stripped.startswith("#")):
                        key = stripped.split("=", 1)[0].strip().lower()
                        if key in targets[current_section]:
                            new_lines.append(
                                f"{key} = {targets[current_section][key]}\n")
                            patched.add((current_section, key))
                            continue
                    new_lines.append(line)

                for section, kvs in targets.items():
                    missing = [(k, v) for k, v in kvs.items()
                               if (section, k) not in patched]
                    if not missing:
                        continue
                    if section == "":
                        insert_lines = [f"{k} = {v}\n" for k, v in missing]
                        new_lines = insert_lines + new_lines
                        continue
                    header = f"[{section}]"
                    has_section = any(
                        l.strip().lower() == header.lower() for l in new_lines)
                    if not has_section:
                        new_lines.append(f"\n[{section}]\n")
                        for k, v in missing:
                            new_lines.append(f"{k} = {v}\n")
                    else:
                        insert_at = None
                        in_sec = False
                        for i, l in enumerate(new_lines):
                            s = l.strip()
                            if s.lower() == header.lower():
                                in_sec = True
                                insert_at = i + 1
                            elif in_sec:
                                if s.startswith("[") and s.endswith("]"):
                                    break
                                insert_at = i + 1
                        if insert_at is not None:
                            for k, v in missing:
                                new_lines.insert(insert_at, f"{k} = {v}\n")
                                insert_at += 1

                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)

                ok = any("allowbackgroundinput" in l.lower() for l in new_lines)
                print(f"[GAMEDECK] Patched {config_path} "
                      f"(allowBackgroundInput confirmed: {ok})")
            except Exception as e:
                print(f"[GAMEDECK] Could not patch {config_path}: {e}")

        for p in paths_to_patch:
            _patch_one(p)

    def _launch_emulator(self, exe, rom_path, console):
        """Show loading bar while emulator boots, then mount as true child window."""
        try:
            self.emulator_loading  = True
            self.emulator_load_pct = 0
            self.emulator_load_msg = f"Loading {console}..."
            self.active_console    = console
            self.mode              = "GAME"
            self.is_frozen         = False

            self._patch_mgba_config(os.path.dirname(exe))

            print(f"[GAMEDECK LAUNCH] exe       : {exe}")
            print(f"[GAMEDECK LAUNCH] ROM       : {rom_path}")
            print(f"[GAMEDECK LAUNCH] exe exists: {os.path.exists(exe)}")
            print(f"[GAMEDECK LAUNCH] ROM exists: {os.path.exists(rom_path)}")

            launch_env = os.environ.copy()
            launch_env["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"]       = "1"
            launch_env["SDL_GAMECONTROLLER_ALLOW_BACKGROUND_EVENTS"]  = "1"

            # !! IMPORTANT — DO NOT REMOVE: fixes 40-sec emulator boot lag on Windows.
            # Without this, Windows foreground-lock blocks the emulator's SetForegroundWindow
            # call during its init, causing a 30-40 sec freeze before its window appears.
            # AllowSetForegroundWindow(-1) grants foreground permission to ALL processes
            # for this session so the emulator window appears instantly on launch.
            # This affects mGBA, RetroArch, and any other emulator launched through here.
            if _WIN:
                ctypes.windll.user32.AllowSetForegroundWindow(-1)

            # Launch hidden so we can mount it before it appears anywhere
            if _WIN:
                import subprocess as _sp
                si = _sp.STARTUPINFO()
                si.dwFlags     = _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0   # SW_HIDE — we reveal it after SetParent
                self.deck_process = _sp.Popen(
                    [exe, rom_path],
                    cwd=os.path.dirname(exe),
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                    env=launch_env,
                    startupinfo=si,
                )
            else:
                self.deck_process = subprocess.Popen(
                    [exe, rom_path],
                    cwd=os.path.dirname(exe),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=launch_env,
                )
            print(f"[GAMEDECK LAUNCH] PID={self.deck_process.pid}")
            threading.Thread(target=self._monitor_emulator, daemon=True).start()
            threading.Thread(target=self._controller_exit_watcher, daemon=True).start()

        except Exception as e:
            print(f"[GAMEDECK LAUNCH] FAILED: {e}")
            import traceback; traceback.print_exc()
            self.emulator_loading = False
            self.mode = "ROMLIST"

    # --------------------------------------------------------------------------
    # TRUE CHILD WINDOW MOUNTING (SetParent approach)
    # mGBA becomes a child of pygame's HWND — locked in permanently, no z-order
    # fighting, no taskbar icon, never slips behind other windows.
    # --------------------------------------------------------------------------
    def _monitor_emulator(self):
        """Animate loading bar, find window, mount as child, wait for close."""
        deadline   = time.time() + 90
        found_hwnd = None
        poll_n     = 0
        pid        = self.deck_process.pid if self.deck_process else None
        print(f"[GAMEDECK MONITOR] Watching PID={pid} for up to 90s...")

        while time.time() < deadline:
            if self.deck_process is None:
                print("[GAMEDECK MONITOR] deck_process gone.")
                break
            code = self.deck_process.poll()
            if code is not None:
                print(f"[GAMEDECK MONITOR] Process exited (code={code}).")
                break

            elapsed = time.time() - (deadline - 90)
            self.emulator_load_pct = min(90, int(elapsed / 90 * 90))
            self.emulator_load_msg = f"Loading {self.active_console or 'emulator'}..."

            if _WIN:
                hwnd = self._find_emulator_hwnd()
                if hwnd:
                    found_hwnd = hwnd
                    print(f"[GAMEDECK MONITOR] Window found after {elapsed:.1f}s (HWND={hwnd})")
                    break

            poll_n += 1
            if poll_n % 20 == 0:
                print(f"[GAMEDECK MONITOR] Still waiting... {elapsed:.0f}s elapsed.")
            time.sleep(0.05)  # poll every 50ms — catch the window before SDL finishes init

        self.emulator_load_pct = 100
        self.emulator_load_msg = "Mounting to Channel 03..."
        # No sleep here — we mount immediately while mGBA is still initializing.
        # Reparenting before SDL finishes setup is what makes SetParent stick.

        if found_hwnd and _WIN:
            try:
                pygame_hwnd = pygame_hwnd_ref[0]
                if not pygame_hwnd:
                    print("[GAMEDECK MONITOR] No pygame HWND — cannot mount.")
                else:
                    print(f"[GAMEDECK MOUNT] pygame_hwnd={pygame_hwnd}, mgba_hwnd={found_hwnd}")
                    # Verify pygame hwnd is still a valid window
                    is_valid = ctypes.windll.user32.IsWindow(pygame_hwnd)
                    print(f"[GAMEDECK MOUNT] IsWindow(pygame_hwnd)={is_valid}")
                    GWL_STYLE           = -16
                    GWL_EXSTYLE         = -20
                    WS_CHILD            = 0x40000000
                    WS_VISIBLE          = 0x10000000
                    WS_CLIPSIBLINGS     = 0x04000000
                    WS_CLIPCHILDREN     = 0x02000000
                    WS_EX_TOOLWINDOW    = 0x00000080
                    WS_EX_APPWINDOW     = 0x00040000
                    # WS_EX_NOACTIVATE prevents Windows from sending synchronous
                    # WM_ACTIVATE / WM_SETFOCUS messages to this child window when
                    # the user presses Win-key or Alt-Tab.  Without it, focus
                    # messages route through pygame's message queue to the child,
                    # and if the child is suspended (NtSuspendProcess) Windows
                    # blocks waiting for a reply → "not responding" on the parent.
                    WS_EX_NOACTIVATE    = 0x08000000

                    # Step 1: Attach mGBA as a true child of pygame's window.
                    ctypes.windll.user32.SetParent(found_hwnd, pygame_hwnd)
                    print(f"[GAMEDECK MOUNT] SetParent done. Ancestor={ctypes.windll.user32.GetAncestor(found_hwnd,1)}")

                    # Step 2: Strip WS_POPUP and replace with WS_CHILD.
                    # SDL2 creates its window as WS_POPUP which always renders above
                    # its own parent. Clearing it and setting WS_CHILD makes Windows
                    # treat mGBA as a true embedded child that respects z-order.
                    WS_POPUP = 0x80000000
                    new_style = (WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN)
                    ctypes.windll.user32.SetWindowLongW(found_hwnd, GWL_STYLE, new_style)
                    print(f"[GAMEDECK MOUNT] Style set: cleared WS_POPUP, set WS_CHILD")

                    # Step 3: Remove taskbar button, Alt-Tab entry, and activation.
                    ex = ctypes.windll.user32.GetWindowLongW(found_hwnd, GWL_EXSTYLE)
                    ctypes.windll.user32.SetWindowLongW(
                        found_hwnd, GWL_EXSTYLE,
                        (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

                    # Step 4: Force Windows to reprocess the style change.
                    # SWP_FRAMECHANGED tells the window manager to recalculate the
                    # frame now that WS_POPUP is gone and WS_CHILD is set.
                    SWP_NOMOVE       = 0x0002
                    SWP_NOSIZE       = 0x0001
                    SWP_NOZORDER     = 0x0004
                    SWP_NOACTIVATE   = 0x0010
                    SWP_FRAMECHANGED = 0x0020
                    ctypes.windll.user32.SetWindowPos(
                        found_hwnd, None, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)

                    # Step 5: Size and position mGBA to the TV "screen" rect.
                    # _fit_rect is the on-screen content rectangle (set by the main
                    # loop); if unset we fall back to filling the whole client area.
                    if self._fit_rect:
                        fx, fy, cw, ch = (int(v) for v in self._fit_rect)
                    else:
                        rc = wintypes.RECT()
                        ctypes.windll.user32.GetClientRect(pygame_hwnd, ctypes.byref(rc))
                        fx, fy = 0, 0
                        cw = rc.right  - rc.left
                        ch = rc.bottom - rc.top
                    ctypes.windll.user32.MoveWindow(found_hwnd, fx, fy, cw, ch, True)

                    # Step 6: Show mGBA. As a true WS_CHILD it renders on top of
                    # pygame's client area within its rectangle — pygame CANNOT paint
                    # over it. The TV-shell UI (menus, OSD, banners) is made visible
                    # instead by hiding this window whenever that UI is up; see
                    # sync_child_window() / _set_child_visible().
                    SWP_SHOWWINDOW = 0x0040
                    HWND_TOP       = 0
                    ctypes.windll.user32.SetWindowPos(
                        found_hwnd, HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    ctypes.windll.user32.ShowWindow(found_hwnd, 9)  # SW_RESTORE
                    print("[GAMEDECK MONITOR] mGBA shown as WS_CHILD inside pygame.")

                    self.game_hwnd  = found_hwnd
                    self.is_embedded = True
                    self._child_visible = True
                    print(f"[GAMEDECK MONITOR] mGBA mounted as child window ({cw}x{ch}).")

                    # Step 7: Return keyboard focus to pygame immediately.
                    # SetParent makes mGBA a child but does not strip its focus.
                    # If mGBA holds focus, Win32 delivers WM_KEYDOWN to mGBA first
                    # and pygame never sees TV keys (M, ESC, arrows, volume etc).
                    # SetFocus + SetForegroundWindow back to pygame fixes this so
                    # the TV shell always owns keyboard events via its event loop.
                    ctypes.windll.user32.SetFocus(pygame_hwnd)
                    ctypes.windll.user32.SetForegroundWindow(pygame_hwnd)

            except Exception as e:
                print(f"[GAMEDECK MONITOR] Child mount failed: {e}")
                import traceback; traceback.print_exc()

        self.emulator_loading = False
        self.overlay_ready    = True
        print("[GAMEDECK MONITOR] Mount complete. Game running.")

        if self.deck_process:
            self.deck_process.wait()

        self.overlay_ready = False
        print("[GAMEDECK] Emulator closed. Restoring Retro TV...")
        self._on_emulator_closed()

    def _find_emulator_hwnd(self):
        """Find the main window belonging to the emulator process.
        Pure ctypes — NO pywin32 dependency.
        Does NOT require IsWindowVisible: the process starts hidden (SW_HIDE)
        so EnumWindows must find it before it has been shown."""
        if not self.deck_process or not _WIN:
            return None
        target_pid = self.deck_process.pid
        found = []

        # EnumWindows callback — must survive via a persistent reference
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

        def _cb(hwnd, _lp):
            try:
                pid = ctypes.c_ulong(0)
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == target_pid:
                    # Accept ANY window belonging to the process — including hidden
                    # ones (SW_HIDE at launch). We must reparent before mGBA finishes
                    # initializing or SetParent silently fails.
                    # Score by area so we prefer the main surface if multiple exist.
                    rc = wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
                    w = max(0, rc.right  - rc.left)
                    h = max(0, rc.bottom - rc.top)
                    found.append((w * h, hwnd))
            except Exception as e:
                log.warning("GetWindowRect failed while scoring a candidate mGBA child window: %s", e)
            return True

        _enum_cb = WNDENUMPROC(_cb)   # keep reference so GC doesn't collect it
        try:
            ctypes.windll.user32.EnumWindows(_enum_cb, 0)
        except Exception as e:
            print(f"[GAMEDECK FIND] EnumWindows error: {e}")

        if not found:
            return None
        # Prefer the largest window — most likely to be the main game surface
        found.sort(reverse=True)
        return found[0][1]

    def _on_emulator_closed(self):
        """Called when the emulator process exits.
        Detaches the child window (if still alive) and returns to ROMLIST.
        No pygame window style restoration needed — we never changed it."""
        if self.game_hwnd and _WIN:
            try:
                ctypes.windll.user32.SetParent(self.game_hwnd, None)
                print("[GAMEDECK] mGBA child window detached.")
            except Exception as e:
                log.warning("SetParent (detach mGBA child window) failed: %s", e)

        # Destroy the UI overlay child window — re-created on next launch.
        if self.ui_overlay_hwnd and _WIN:
            try:
                ctypes.windll.user32.DestroyWindow(self.ui_overlay_hwnd)
                print("[GAMEDECK] UI overlay window destroyed.")
            except Exception as e:
                log.warning("DestroyWindow for UI overlay child failed: %s", e)
        self.ui_overlay_hwnd = None

        self.deck_process = None
        self.game_hwnd    = None
        self.is_visible   = False
        self.is_embedded  = False
        self.is_frozen    = False
        self.emulator_loading = False
        self._child_visible = None  # next launch re-seeds the visibility state

        if not self.roms and self.active_console:
            print(f"[GAMEDECK] ROM list empty on return — re-scanning {self.active_console}.")
            self._scan_roms(self.active_console)

        self.mode = "ROMLIST"
        print(f"[GAMEDECK] Retro TV restored. Back in ROM list at index {self.rom_index}.")

    # --------------------------------------------------------------------------
    # CONTROLLER INPUT — XInput → SendInput (direct, no focus tricks)
    # mGBA uses GetAsyncKeyState (allowBackgroundInput=1) which reads synthetic
    # SendInput events globally without needing window focus.  We therefore never
    # call SetFocus / AttachThreadInput — that was the source of the focus-ping-
    # pong and the stuck-taskbar that the user was seeing.
    # --------------------------------------------------------------------------
    def _controller_exit_watcher(self):
        """Background thread: XInput poll → SendInput to mGBA.

        Two roles:
        1. EXIT — Start+Select held 600ms kills the emulator.
        2. INPUT — Reads controller, sends VK keypresses via SendInput directly.
           mGBA's allowBackgroundInput=1 makes it poll GetAsyncKeyState, which
           reads SendInput events globally — no SetFocus / AttachThreadInput needed.
        """
        HOLD_MS = 600

        # ── SendInput structures ─────────────────────────────────────────────
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD  = 1

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk",         ctypes.c_ushort),
                ("wScan",       ctypes.c_ushort),
                ("dwFlags",     ctypes.c_ulong),
                ("time",        ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 24)]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("_", _INPUT_UNION)]

        def _send_key(vk, key_up=False):
            """Inject a VK keypress into the global input stream via SendInput.
            mGBA's GetAsyncKeyState picks this up without needing focus."""
            if not vk:
                return
            inp = _INPUT()
            inp.type       = INPUT_KEYBOARD
            inp._.ki.wVk   = vk
            inp._.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
            inp._.ki.dwExtraInfo = ctypes.cast(
                ctypes.pointer(ctypes.c_ulong(0)), ctypes.POINTER(ctypes.c_ulong))
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

        # ── XInput structures ────────────────────────────────────────────────
        class _XINPUT_GAMEPAD(ctypes.Structure):
            _fields_ = [
                ("wButtons",      ctypes.c_ushort),
                ("bLeftTrigger",  ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX",      ctypes.c_short),
                ("sThumbLY",      ctypes.c_short),
                ("sThumbRX",      ctypes.c_short),
                ("sThumbRY",      ctypes.c_short),
            ]

        class _XINPUT_STATE(ctypes.Structure):
            _fields_ = [
                ("dwPacketNumber", ctypes.c_ulong),
                ("Gamepad",        _XINPUT_GAMEPAD),
            ]

        # Snapshot the exact process this watcher was launched for. is_process_
        # running() is shared instance state — it also returns True whenever
        # _libretro_core.is_loaded, and that core "stays alive across channels"
        # by design. If this watcher used is_process_running() as its loop
        # condition, quitting THIS mGBA session while a libretro core (e.g. an
        # NES/SNES game) is still loaded from an earlier channel would make the
        # loop think the emulator is still running — it would never exit, and
        # would poll XInput / relay SendInput forever alongside whatever polls
        # the controller next, fighting over the same physical device.
        my_process = self.deck_process

        print("[GAMEDECK CTRL WATCHER] Started — waiting for overlay_ready...")

        # Wait until the child window is fully mounted
        wait_deadline = time.time() + 120
        while not getattr(self, "overlay_ready", False) and time.time() < wait_deadline:
            time.sleep(0.15)

        if my_process is None or my_process.poll() is not None:
            print("[GAMEDECK CTRL WATCHER] Emulator gone before watcher activated.")
            return

        print("[GAMEDECK CTRL WATCHER] Live — hold Start+Select 0.6s to exit.")

        xinput = None
        for lib in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                xinput = ctypes.windll.LoadLibrary(lib)
                print(f"[GAMEDECK CTRL WATCHER] Loaded {lib}")
                break
            except Exception:
                continue

        if xinput is None:
            print("[GAMEDECK CTRL WATCHER] XInput unavailable — controller disabled.")
            return

        hold_since   = None
        prev_buttons = 0
        DEAD         = 8000   # analog stick deadzone
        TRIG_THRESH  = 30     # analog trigger press threshold (0-255 range)

        while my_process.poll() is None:
            time.sleep(0.016)   # ~60 Hz poll

            # ── Frozen (channel switched away from ch03) ──────────────────────
            # Don't relay any inputs while the game is suspended — mGBA's audio/
            # input threads are frozen by NtSuspendProcess.  If any buttons were
            # held when we froze, release them now so the OS key-state is clean.
            if self.is_frozen:
                if prev_buttons:
                    _active_con_f = getattr(self, "active_console", "GBA") or "GBA"
                    _xi_map_f     = _get_console_def(_active_con_f)["xinput_map"]
                    for bit, console_btn in _xi_map_f.items():
                        if prev_buttons & bit:
                            vk = self.ctrl_mapping.get(console_btn, 0)
                            if vk:
                                _send_key(vk, key_up=True)
                    prev_buttons = 0
                    hold_since   = None
                continue

            # Read first connected controller
            buttons = 0
            state = _XINPUT_STATE()
            for idx in range(4):
                try:
                    if xinput.XInputGetState(idx, ctypes.byref(state)) == 0:
                        buttons = state.Gamepad.wButtons
                        # Left stick → D-pad (if enabled in mapping)
                        if self.ctrl_mapping.get("stick_as_dpad", False):
                            lx = state.Gamepad.sThumbLX
                            ly = state.Gamepad.sThumbLY
                            if ly >  DEAD: buttons |= _XI_UP
                            if ly < -DEAD: buttons |= _XI_DOWN
                            if lx < -DEAD: buttons |= _XI_LEFT
                            if lx >  DEAD: buttons |= _XI_RIGHT
                        # Analog triggers (PSX L2/R2) aren't part of wButtons —
                        # fold a threshold-crossing pull in as the same
                        # pseudo-bits used by xinput_map/poll_xinput_for_remap,
                        # so the bitmask diff/send logic below picks them up.
                        if state.Gamepad.bLeftTrigger  >= TRIG_THRESH: buttons |= _XI_LT
                        if state.Gamepad.bRightTrigger >= TRIG_THRESH: buttons |= _XI_RT
                        break
                except Exception:
                    continue

            # ── Exit combo: Start+Select held ────────────────────────────────
            if (buttons & _XI_START) and (buttons & _XI_BACK):
                if hold_since is None:
                    hold_since = time.time()
                    print("[GAMEDECK CTRL WATCHER] Start+Select held — confirming...")
                elif (time.time() - hold_since) * 1000 >= HOLD_MS:
                    print("[GAMEDECK CTRL WATCHER] Confirmed. Killing emulator.")
                    # Release any held keys so OS isn't left with stuck keys
                    _active_con_e = getattr(self, "active_console", "GBA") or "GBA"
                    _xi_map_e     = _get_console_def(_active_con_e)["xinput_map"]
                    for bit, console_btn in _xi_map_e.items():
                        if prev_buttons & bit:
                            vk = self.ctrl_mapping.get(console_btn, 0)
                            _send_key(vk, key_up=True)
                    try:
                        self.deck_process.kill()
                    except Exception as e:
                        log.warning("Failed to kill deck_process during controller-mapping key-release cleanup: %s", e)
                    break
                prev_buttons = buttons
                continue
            else:
                if hold_since is not None:
                    print("[GAMEDECK CTRL WATCHER] Released early — reset hold timer.")
                hold_since = None

            #    ─ Relay changed buttons → emulator via SendInput ───────────────
            # No SetFocus / AttachThreadInput needed:
            # mGBA polls GetAsyncKeyState globally (allowBackgroundInput=1).
            changed = buttons ^ prev_buttons
            if changed:
                # Use the active console's XInput button map
                _active_con = getattr(self, "active_console", "GBA") or "GBA"
                _xi_map     = _get_console_def(_active_con)["xinput_map"]
                for bit, console_btn in _xi_map.items():
                    if changed & bit:
                        vk = self.ctrl_mapping.get(console_btn, 0)
                        if vk:
                            pressed = bool(buttons & bit)
                            _send_key(vk, key_up=not pressed)

            prev_buttons = buttons

        print("[GAMEDECK CTRL WATCHER] Exited.")


# ==============================================================================
# PART 8 OF 13: PROCESS VISIBILITY, FREEZE & AUDIO MANAGERS
# ==============================================================================

    def _set_child_visible(self, visible):
        """Single, edge-triggered controller for the mGBA child window's visibility.

        Both the main render thread (overlay/OSD handling) and the channel-change
        daemon thread call this. A lock + last-applied-state flag guarantee:
          * ShowWindow is only called on an actual transition (no per-frame spam,
            which was the source of the menu/OSD "flashing for a sec" behaviour), and
          * the two threads can never interleave half-applied show/hide calls.

        mGBA is a true WS_CHILD: it always renders on top of pygame's client area
        within its rectangle, and pygame cannot paint over it. So menus/OSD are
        shown by HIDING this window (this method, driven by sync_child_window())
        whenever shell UI is on screen, and showing + re-fitting it during clean
        gameplay. When shown it is fitted to self._fit_rect (the TV screen rect).
        """
        if not _WIN or not self.game_hwnd:
            return
        want = bool(visible)
        with self._vis_lock:
            if self._child_visible == want:
                return  # already in the desired state — nothing to do
            try:
                HWND_TOP       = 0
                SWP_NOMOVE     = 0x0002
                SWP_NOSIZE     = 0x0001
                SWP_NOACTIVATE = 0x0010
                SWP_SHOWWINDOW = 0x0040
                if want:
                    # Re-fit mGBA to the TV screen rect (self._fit_rect) so it lands
                    # in the right place; fall back to the full client area if unset.
                    pygame_hwnd = pygame_hwnd_ref[0]
                    if pygame_hwnd:
                        if self._fit_rect:
                            fx, fy, cw, ch = (int(v) for v in self._fit_rect)
                        else:
                            rc = wintypes.RECT()
                            ctypes.windll.user32.GetClientRect(pygame_hwnd, ctypes.byref(rc))
                            fx, fy = 0, 0
                            cw = rc.right - rc.left
                            ch = rc.bottom - rc.top
                        ctypes.windll.user32.MoveWindow(self.game_hwnd, fx, fy, cw, ch, True)
                    # mGBA at HWND_TOP — visible inside its rectangle. The shell UI is
                    # kept visible by hiding this window when that UI is up (see
                    # sync_child_window), not by trying to paint over the child.
                    ctypes.windll.user32.SetWindowPos(
                        self.game_hwnd, HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    ctypes.windll.user32.ShowWindow(self.game_hwnd, 8)  # SW_SHOWNA
                else:
                    ctypes.windll.user32.ShowWindow(self.game_hwnd, 0)  # SW_HIDE
                self._child_visible = want
            except Exception as e:
                print(f"[GAMEDECK VIS] _set_child_visible({want}) failed: {e}")

    def sync_child_window(self, show, fit_rect=None):
        """Reconcile the mGBA child window with the TV-shell UI, called every frame
        by the main render loop.

        `show` must be True ONLY during clean channel-03 gameplay (no menu, OSD,
        guide, file explorer, or channel-transition on screen). When False the
        child window is hidden so the pygame-drawn shell UI (which is painted
        underneath it) becomes fully visible and usable. `fit_rect` is the
        on-screen TV "screen" rectangle (pygame client coords, x, y, w, h) the
        game should fill when shown.

        The game process is NOT suspended here — it keeps running/audible while
        briefly hidden behind a menu/OSD. Suspending only happens when actually
        leaving the channel (see set_viewport_state).
        """
        if not _WIN or not self.game_hwnd:
            return
        if fit_rect is not None:
            new_rect = tuple(int(v) for v in fit_rect)
            # If the rect moved while the game is already visible, force a re-fit
            # by re-running the show path (edge-trigger would otherwise skip it).
            if new_rect != self._fit_rect and self._child_visible and show:
                self._fit_rect = new_rect
                with self._vis_lock:
                    self._child_visible = None
            else:
                self._fit_rect = new_rect
        self._set_child_visible(bool(show))

    def set_viewport_state(self, visible=True, target_rect=None):
        """Called by change_channel() when leaving/returning to channel 03.

        Child window approach: simply show/hide the already-mounted child window.
        No z-order or colorkey manipulation needed — DWM handles compositing.

        visible=False: freeze + hide the child window so other channels show cleanly.
        visible=True:  thaw + restore the child window to fill the client area.
        """
        if not _WIN:
            return True
        if getattr(self, "emulator_loading", False):
            print("[GAMEDECK VIEWPORT] Skipped — still loading.")
            return True
        if not self.game_hwnd:
            print("[GAMEDECK VIEWPORT] Skipped — no game_hwnd yet.")
            return True

        try:
            if not visible:
                # ── Leaving channel 03 ────────────────────────────────────────
                # Hide the child FIRST (instant + idempotent, never skipped) so the
                # game is pushed back the moment the new channel loads, THEN suspend
                # on a separate thread so NtSuspendProcess can't block the main loop.
                self._set_child_visible(False)
                import threading as _t
                def _do_suspend():
                    try:
                        self._suspend_process()
                    except Exception as e:
                        log.warning("Background channel-switch _suspend_process() call failed: %s", e)
                _t.Thread(target=_do_suspend, daemon=True).start()
                print("[GAMEDECK VIEWPORT] Left ch03 — child hidden, mGBA suspending.")

            else:
                # ── Returning to channel 03 ───────────────────────────────────
                # Resume FIRST so the game is ready, then re-fit + show via the
                # shared visibility controller (handles z-order + resize).
                self._resume_process()
                # Clear is_frozen immediately (optimistic) so the render loop's
                # "elif not self.is_frozen" restore path can fire even if
                # _resume_process() is racing with the render thread.
                self.is_frozen = False
                # Reset the edge-trigger flag so _set_child_visible(True) is
                # guaranteed to fire even if the OSD hide path drove
                # _child_visible to False during this channel transition.
                with self._vis_lock:
                    self._child_visible = None
                self._set_child_visible(True)
                print("[GAMEDECK VIEWPORT] Returned to ch03 — mGBA resumed + child visible.")

        except Exception as e:
            print(f"[GAMEDECK VIEWPORT] set_viewport_state failed: {e}")
            import traceback; traceback.print_exc()
        return True

    def _suspend_process(self):
        if not (_WIN and self.is_process_running()) or self.is_frozen: return
        try:
            h = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_SUSPEND_RESUME | _PROCESS_QUERY_INFORMATION, False, self.deck_process.pid)
            if h:
                ctypes.windll.ntdll.NtSuspendProcess(h)
                ctypes.windll.kernel32.CloseHandle(h)
                self.is_frozen = True
        except Exception as e:
            log.warning("NtSuspendProcess failed, emulator process not suspended: %s", e)

    def _resume_process(self):
        if not (_WIN and self.is_process_running()) or not self.is_frozen: return
        try:
            h = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_SUSPEND_RESUME | _PROCESS_QUERY_INFORMATION, False, self.deck_process.pid)
            if h:
                ctypes.windll.ntdll.NtResumeProcess(h)
                ctypes.windll.kernel32.CloseHandle(h)
                self.is_frozen = False
        except Exception as e:
            log.warning("NtResumeProcess failed, emulator process not resumed: %s", e)

    def is_process_running(self):
        """Returns True if a libretro core is loaded, or a legacy .exe is running."""
        if self._libretro_core is not None and self._libretro_core.is_loaded:
            return True
        return bool(self.deck_process and self.deck_process.poll() is None)

    def pre_boot_background_deck(self, parent_hwnd=None):
        if parent_hwnd: self.parent_hwnd = parent_hwnd
        self.ready_flag = True
        return True

    def freeze_inputs(self):
        """Send synthetic key-up for every key mGBA could be polling via
        GetAsyncKeyState. Called whenever a TV UI overlay opens (menu, OSD,
        channel switch). Without this, mGBA allowBackgroundInput=1 polling
        sees TV keys (M, ESC, arrows, volume) as game inputs simultaneously,
        because GetAsyncKeyState reads raw hardware state regardless of focus.
        Safe to call every frame - key-up on an already-up key is a no-op.
        """
        if not _WIN or not self.is_process_running():
            return
        try:
            KEYEVENTF_KEYUP = 0x0002
            INPUT_KEYBOARD  = 1

            class _KI(ctypes.Structure):
                _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                             ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                             ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

            class _IU(ctypes.Union):
                _fields_ = [("ki", _KI), ("padding", ctypes.c_byte * 24)]

            class _IN(ctypes.Structure):
                _fields_ = [("type", ctypes.c_ulong), ("_", _IU)]

            # All VK codes mGBA could be polling: mapped game keys + TV control keys
            vk_codes = set()
            for v in self.ctrl_mapping.values():
                if isinstance(v, int):
                    vk_codes.add(v)
            # TV shell keys that must never leak into the game
            tv_keys = [
                0x4D,  # M   - main menu
                0x43,  # C   - controls splash
                0x4E,  # N   - channel up
                0x1B,  # ESC
                0x26,  # Up arrow   (volume up)
                0x28,  # Down arrow (volume down)
                0x25,  # Left arrow
                0x27,  # Right arrow
                0x0D,  # Enter
                0x09,  # Tab
            ]
            for k in tv_keys:
                vk_codes.add(k)

            inputs = []
            for vk in sorted(vk_codes):
                inp = _IN()
                inp.type = INPUT_KEYBOARD
                inp._.ki.wVk = vk
                inp._.ki.dwFlags = KEYEVENTF_KEYUP
                inp._.ki.dwExtraInfo = ctypes.cast(
                    ctypes.pointer(ctypes.c_ulong(0)), ctypes.POINTER(ctypes.c_ulong))
                inputs.append(inp)

            if inputs:
                arr = (_IN * len(inputs))(*inputs)
                ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(_IN))
        except Exception as e:
            print(f"[GAMEDECK] freeze_inputs error: {e}")

    def set_audio_state(self, muted=True):
        """Mute or unmute mGBA's audio session.
        Tries pycaw first; falls back to pure-ctypes WASAPI if pycaw is absent."""
        proc = self.deck_process
        if proc is None or not self.is_process_running():
            return
        pid = proc.pid
        import threading as _t
        def _do():
            # ── pycaw path (preferred, simpler) ──────────────────────────────
            try:
                from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
                for session in AudioUtilities.GetAllSessions():
                    if session.Process and session.Process.pid == pid:
                        vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                        vol.SetMute(1 if muted else 0, None)
                        print(f"[GAMEDECK AUDIO] pycaw mute={muted} ok.")
                        return
            except Exception as e:
                log.warning("pycaw mute attempt failed, falling back to WASAPI: %s", e)
            # ── ctypes WASAPI fallback (no external packages needed) ──────────
            ok = _wasapi_set_process_audio(pid, mute=muted)
            if ok:
                print(f"[GAMEDECK AUDIO] WASAPI mute={muted} ok.")
            else:
                print(f"[GAMEDECK AUDIO] set_audio_state: session not found yet (pid={pid}).")
        _t.Thread(target=_do, daemon=True).start()

    def set_audio_volume(self, level_pct, muted=False):
        """Set an external emulator process's (e.g. mGBA) per-process volume
        to match the TV volume slider (0-100). Tries pycaw first; falls back
        to pure-ctypes WASAPI if pycaw is absent.

        Curve: this sets the OS session volume directly on the raw output --
        there's no separate hotness-compensation multiplier stacked on top of
        it the way LOUDNESS_GAIN/MUSIC_GAIN are for the in-process audio
        paths (see libretro_core.LibretroAudio.set_volume and
        _perceptual_volume_pct in retro_tv_emulator.py). So this uses the
        standard -40dB floor, not the shallower STACKED_GAIN_MIN_DB one --
        slider=100 lands at this process's true unity volume, same as the
        TV/static channels. Duplicated locally rather than imported to avoid
        a cross-module import here (same reasoning as libretro_core.py).

        Calls are serialized through a single reusable worker thread rather
        than spawning a new thread per call. Held-key volume repeat fires
        every ~180ms, and each pycaw/WASAPI call enumerates live audio
        sessions over COM, which isn't instant -- spawning an unbounded
        thread per call let multiple in-flight calls finish out of order, so
        a slightly slower earlier thread could apply its (now stale) volume
        AFTER a newer one, making the same slider position sound different
        depending on whether you arrived there going up or down. This queues
        only the latest requested value and lets one worker apply it,
        dropping any values that were superseded before it got to them.
        """
        proc = self.deck_process
        if proc is None or not self.is_process_running():
            return
        pid = proc.pid

        pct = max(0.0, min(100.0, float(level_pct)))
        if muted or pct <= 0:
            curved_pct = 0.0
        else:
            frac = pct / 100.0
            db_level = -40.0 * (1.0 - frac)
            curved_pct = (10 ** (db_level / 20.0)) * 100.0
        gain = max(0.0, min(1.0, curved_pct / 100.0))

        with self._audio_vol_lock:
            self._audio_vol_pending = (pid, curved_pct, gain)
            if self._audio_vol_worker_running:
                # A worker is already draining the queue -- it will pick up
                # this newer value itself. No need to start a second one.
                return
            self._audio_vol_worker_running = True

        def _worker():
            try:
                while True:
                    with self._audio_vol_lock:
                        job = self._audio_vol_pending
                        self._audio_vol_pending = None
                        if job is None:
                            self._audio_vol_worker_running = False
                            return
                    job_pid, job_curved_pct, job_gain = job
                    # ── pycaw path (preferred, simpler) ──────────────────
                    applied = False
                    try:
                        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
                        for session in AudioUtilities.GetAllSessions():
                            if session.Process and session.Process.pid == job_pid:
                                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                                vol.SetMasterVolume(job_gain, None)
                                print(f"[GAMEDECK AUDIO] pycaw volume={job_gain:.2f} ok.")
                                applied = True
                                break
                    except Exception as e:
                        log.warning("pycaw volume-set attempt failed, falling back to WASAPI: %s", e)
                    # ── ctypes WASAPI fallback ────────────────────────────
                    if not applied:
                        ok = _wasapi_set_process_audio(job_pid, volume_pct=job_curved_pct)
                        if ok:
                            print(f"[GAMEDECK AUDIO] WASAPI volume={job_gain:.2f} ok.")
                        else:
                            print(f"[GAMEDECK AUDIO] set_audio_volume: session not found yet (pid={job_pid}).")
            except Exception as e:
                log.warning("set_audio_volume worker error: %s", e)
                with self._audio_vol_lock:
                    self._audio_vol_worker_running = False

        threading.Thread(target=_worker, daemon=True).start()

    def shutdown_deck(self):
        if self.deck_process:
            try:
                self._resume_process()
                self.deck_process.terminate()
                start = time.time()
                while self.deck_process.poll() is None and time.time() - start < 3:
                    time.sleep(0.1)
                if self.deck_process.poll() is None:
                    self.deck_process.kill()
            except Exception as e:
                log.warning("Failed to terminate/kill deck_process during shutdown: %s", e)
        # Destroy the UI overlay child window on full shutdown.
        if self.ui_overlay_hwnd and _WIN:
            try:
                ctypes.windll.user32.DestroyWindow(self.ui_overlay_hwnd)
            except Exception as e:
                log.warning("DestroyWindow for UI overlay child failed during shutdown: %s", e)
        self.ui_overlay_hwnd = None
        self.deck_process = None
        self.game_hwnd    = None
        self.is_visible   = False
        self.is_embedded  = False
        self.is_frozen    = False
        self.active_console = None
        self.mode = "BROWSE"
        self.ready_flag = False
        self.dvd_playback_active  = False
        self.dvd_splash_playing   = False
        self.dvd_splash_done      = False
        self.dvd_exit_confirm     = False
        self.dvd_return_menu_confirm = False
        self.dvd_paused           = False
        self.dvd_was_in_menu_on_leave = False


# ==============================================================================
# PART 9 OF 13: CONTROLLER MAPPING — STORAGE & EVENT HANDLER
# ==============================================================================

    def _load_controller_mapping(self, console=None):
        """Load button→VK mapping for *console* from emulator_config.json.
        Falls back to the per-console factory defaults.
        When console is None, loads GBA mapping (backward compat)."""
        if console is None:
            console = "GBA"
        cdef    = _get_console_def(console)
        default = dict(cdef["default_map"])
        try:
            cfg_path = os.path.join(
                os.path.dirname(os.path.abspath(sys.argv[0])),
                "emulator_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # New per-console key: "<CONSOLE>_mapping"
                saved = data.get(f"{console}_mapping", {})
                # Legacy fallback: if this is GBA and no new key exists, try old key
                if not saved and console == "GBA":
                    saved = data.get("GBA_mapping", {})
                mapping = dict(default)
                for k, v in saved.items():
                    if k == "stick_as_dpad":
                        mapping[k] = bool(v)
                    elif isinstance(v, int):
                        mapping[k] = v
                    elif isinstance(v, str) and v in VK_FROM_NAME:
                        mapping[k] = VK_FROM_NAME[v]
                return mapping
        except Exception as e:
            print(f"[GAMEDECK MAPPING] Load failed for {console} (using defaults): {e}")
        return default

    def _save_controller_mapping(self, console=None):
        """Persist the current mapping for *console* to emulator_config.json."""
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        try:
            cfg_path = os.path.join(
                os.path.dirname(os.path.abspath(sys.argv[0])),
                "emulator_config.json")
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            # Serialize VK codes as human-readable names where possible
            serialised = {}
            for k, v in self.ctrl_mapping.items():
                if k == "stick_as_dpad":
                    serialised[k] = v
                else:
                    serialised[k] = VK_NAMES.get(v, v)
            data[f"{console}_mapping"] = serialised
            # Keep legacy GBA key in sync for backward compat
            if console == "GBA":
                data["GBA_mapping"] = serialised
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"[GAMEDECK MAPPING] Saved {console}: {serialised}")
        except Exception as e:
            print(f"[GAMEDECK MAPPING] Save failed for {console}: {e}")

    def handle_controller_mapping_event(self, event):
        """Call this from the TV settings menu when the mapping UI is visible.
        Returns True if the event was consumed."""
        if self.ctrl_mapping_active_btn is None:
            return False

        btn = self.ctrl_mapping_active_btn

        if self.ctrl_mapping_mode == "keyboard":
            if event.type == pygame.KEYDOWN:
                # Skip synthetic events posted by the XInput nav thread —
                # controller button presses must not appear as keyboard bindings.
                if getattr(event, "synthetic", False):
                    return False
                vk = PYGAME_KEY_TO_VK.get(event.key)
                if vk:
                    self.ctrl_mapping[btn] = vk
                    self._save_controller_mapping()
                    print(f"[GAMEDECK MAPPING] {btn} → {VK_NAMES.get(vk, hex(vk))} (keyboard)")
                self.ctrl_mapping_active_btn = None
                return True
        else:
            # Controller mode: capture next XInput button press in a quick poll
            # We handle this inline in render_controller_mapping_ui via a flag;
            # the actual capture happens in the next watcher cycle — here we just
            # mark the button as waiting and the watcher (if running) would intercept.
            # For the settings menu (no game running), we do a quick direct poll.
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                self.ctrl_mapping_active_btn = None
                return True

        return False


# ==============================================================================
# PART 10 OF 13: CONTROLLER MAPPING UI — GBA WIREFRAME RENDERER
# ==============================================================================

    def render_controller_mapping_ui(self, surface, rect, ui_color=(0, 255, 128), db=None, console=None):
        """
        Draws the controller mapping panel inside `rect` for the given console.

        Layout:
          - Title, then console logo, then the console wireframe image
          - Left column labels (Up/Left/Right/Down/Start) sit inside the panel,
            ~20% from the left edge — lines connect them to button hotspots
          - Right column labels (R/L/A/B/Select) sit ~20% from right edge
          - Labels show: button-name  →  current XInput button assignment
          - Sequential remap mode: Enter starts it, controller press logs + advances,
            finishes by auto-saving and returning navigation to the menu
          - Bottom section: Map Controller / L-Stick toggle / Close, anchored
            near the bottom of the panel; the focused option is shown via a
            theme-colored text highlight (no surrounding box)
        """
        # Resolve which console we're rendering for (default to GBA for backward compat)
        if console is None:
            console = "GBA"
        console_label = CONSOLE_LABELS.get(console, console)

        # ── Load / cache the per-console mapping ─────────────────────────────
        # When the console changes we reload the saved mapping for that console
        # so each system shows its own independent button assignments.
        if getattr(self, "_active_mapping_console", None) != console:
            self._active_mapping_console = console
            self.ctrl_mapping = self._load_controller_mapping(console)
            self.ctrl_mapping_active_btn = None
            self._seq_remap_active = False
            self._ctrl_labels = {}
            self._last_xinput_btns = 0
            if console not in self._libretro_xinput_map:
                self._build_default_libretro_xinput_map(console)

        cdef       = _get_console_def(console)
        BTN_SEQ    = cdef["btn_seq"]
        LEFT_BTNS  = cdef["left_btns"]
        RIGHT_BTNS = cdef["right_btns"]
        HOTSPOTS   = cdef["hotspots"]

        x, y, w, h = rect

        # ── Background ────────────────────────────────────────────────────────
        # Match the same theme-driven navy background + opacity used by every
        # other menu panel (see render_settings_overlay_menu in ui.py), instead
        # of a hardcoded flat color, so this panel tracks theme hue/opacity changes.
        import colorsys
        _bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220 / 360.0
        _br, _bg, _bb = colorsys.hsv_to_rgb(_bg_hue, 0.6, 0.15)
        MIDNIGHT_NAVY = (int(_br * 255), int(_bg * 255), int(_bb * 255))
        _slider_val = db.config.get("menu_opacity", 50) if db is not None else 50
        menu_opacity = int((_slider_val / 100.0) * 255)
        bg_layer = pygame.Surface((w, h), pygame.SRCALPHA)
        bg_layer.fill(MIDNIGHT_NAVY + (menu_opacity,))
        surface.blit(bg_layer, (x, y))

        # ── Fonts ─────────────────────────────────────────────────────────────
        f_label  = _cached_font("Courier New", 14, bold=True)
        f_small  = _cached_font("Courier New", 12, bold=True)
        f_title  = _cached_font("Courier New", 17, bold=True)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = f_title.render(f"CONTROLLER MAPPING  —  {console}", True, ui_color)
        surface.blit(title_lbl, (x + w // 2 - title_lbl.get_width() // 2, y + 15))

        # ── Console logo (reuses the same logo art shown on the console picker) ──
        logo_top   = y + 6 + title_lbl.get_height() + 10
        logo_h_tgt = max(80, int(h * 0.24))   # doubled from 0.12 to 0.24
        logo_surf  = self._get_logo(console, logo_h_tgt)
        if logo_surf is not None:
            surface.blit(logo_surf, (x + w // 2 - logo_surf.get_width() // 2, logo_top))
            logo_bottom = logo_top + logo_surf.get_height()
        else:
            f_logo_fallback = pygame.font.SysFont("Courier New", logo_h_tgt // 3, bold=True)
            lbl_fallback = f_logo_fallback.render(console_label, True, ui_color)
            surface.blit(lbl_fallback, (x + w // 2 - lbl_fallback.get_width() // 2, logo_top))
            logo_bottom = logo_top + lbl_fallback.get_height()

        # ── Tinted wireframe image ─────────────────────────────────────────
        # Try console-specific wireframe first, fall back to GBA wireframe
        wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", f"{console}_wireframe.png")
        if not os.path.exists(wf_path):
            wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", "GBA_wireframe.png")
        if console not in self._wf_surface_cache and os.path.exists(wf_path):
            try:
                raw = pygame.image.load(wf_path).convert_alpha()
                self._wf_surface_cache[console] = raw
                self._wf_tinted_cache[console]     = None   # force re-tint
                self._wf_tint_color_cache[console] = None
                # Keep legacy GBA aliases in sync
                if console == "GBA":
                    self._gba_wf_surface    = raw
                    self._gba_wf_tinted     = None
                    self._gba_wf_tint_color = None
            except Exception as e:
                print(f"[GAMEDECK MAPPING UI] Could not load wireframe for {console}: {e}")
                self._wf_surface_cache[console] = None

        # Per-console wireframe image sizes
        if console == "N64":
            img_area_h = int(h * 0.730)   # doubled (0.456*2) then -20%
            img_area_w = int(w * 0.784)   # doubled (0.653*2) then -20%
        elif console == "GG":
            img_area_h = int(h * 0.456)   # default 0.38 * 1.2
            img_area_w = int(w * 0.648)   # default 0.54 * 1.2
        elif console in ("NES", "GBC", "PSX"):
            img_area_h = int(h * 0.418)   # default 0.38 * 1.1
            img_area_w = int(w * 0.594)   # default 0.54 * 1.1
        elif console == "GB":
            img_area_h = int(h * 0.456)
            img_area_w = int(w * 0.653)
        else:
            img_area_h = int(h * 0.38)
            img_area_w = int(w * 0.54)
        img_x = x + (w - img_area_w) // 2
        if console == "N64":
            img_y = logo_bottom - int(h * 0.12)  # shift up so controller fills the space better
        else:
            img_y = logo_bottom + 10     # wireframe image sits just below the logo

        _wf_raw = self._wf_surface_cache.get(console)
        if _wf_raw is not None:
            if self._wf_tint_color_cache.get(console) != ui_color:
                raw = _wf_raw
                aspect = raw.get_width() / raw.get_height()
                th = img_area_h
                tw = int(th * aspect)
                if tw > img_area_w:
                    tw = img_area_w
                    th = int(tw / aspect)
                scaled = pygame.transform.smoothscale(raw, (tw, th))
                tinted = pygame.Surface((tw, th), pygame.SRCALPHA)
                tinted.fill((0, 0, 0, 0))
                try:
                    import numpy as np
                    src_arr  = pygame.surfarray.pixels3d(scaled)
                    src_a    = pygame.surfarray.pixels_alpha(scaled) if scaled.get_flags() & pygame.SRCALPHA else None
                    darkness = (255 - src_arr.mean(axis=2)).astype(np.uint8)
                    out_arr  = pygame.surfarray.pixels3d(tinted)
                    out_a    = pygame.surfarray.pixels_alpha(tinted)
                    r_t, g_t, b_t = ui_color
                    out_arr[:,:,0] = (darkness.astype(np.uint16) * r_t // 255).astype(np.uint8)
                    out_arr[:,:,1] = (darkness.astype(np.uint16) * g_t // 255).astype(np.uint8)
                    out_arr[:,:,2] = (darkness.astype(np.uint16) * b_t // 255).astype(np.uint8)
                    # Use the original alpha from the PNG — transparent pixels stay
                    # transparent. Multiply by darkness so faint lines stay faint.
                    if src_a is not None:
                        out_a[:,:] = (src_a.astype(np.uint16) * darkness // 255).astype(np.uint8)
                    else:
                        out_a[:,:] = darkness
                    del out_arr, out_a, src_arr
                    if src_a is not None:
                        del src_a
                except Exception as _te:
                    print(f"[GAMEDECK MAPPING] numpy tint unavailable, using blend ({_te})")
                    inv = scaled.copy()
                    inv.fill((255, 255, 255, 255), special_flags=pygame.BLEND_RGBA_SUB)
                    tint_ovl = pygame.Surface((tw, th), pygame.SRCALPHA)
                    tint_ovl.fill((ui_color[0], ui_color[1], ui_color[2], 255))
                    inv.blit(tint_ovl, (0, 0), special_flags=pygame.BLEND_MULT)
                    tinted.blit(inv, (0, 0))
                self._wf_tinted_cache[console]     = tinted
                self._wf_tint_color_cache[console] = ui_color
                # Keep legacy GBA aliases in sync
                if console == "GBA":
                    self._gba_wf_tinted     = tinted
                    self._gba_wf_tint_color = ui_color

            tinted_surf = self._wf_tinted_cache[console]
            draw_x = img_x + (img_area_w - tinted_surf.get_width())  // 2
            draw_y = img_y + (img_area_h - tinted_surf.get_height()) // 2
            surface.blit(tinted_surf, (draw_x, draw_y))
            iw = tinted_surf.get_width()
            ih = tinted_surf.get_height()
        else:
            draw_x = img_x
            draw_y = img_y
            iw = img_area_w
            ih = img_area_h
            # Draw a dashed outline box to indicate where the wireframe would be
            pygame.draw.rect(surface, (40, 50, 40), (draw_x, draw_y, iw, ih))
            pygame.draw.rect(surface, (60, 80, 60), (draw_x, draw_y, iw, ih), 2)
            if not hasattr(self, '_wf_fallback_fonts'):
                self._wf_fallback_fonts = (
                    pygame.font.SysFont("Courier New", 18, bold=True),
                    pygame.font.SysFont("Courier New", 12),
                )
            f_nf_big, f_nf_sml = self._wf_fallback_fonts
            lbl_nf1  = f_nf_big.render("IMAGE NOT FOUND", True, (100, 120, 100))
            lbl_nf2  = f_nf_sml.render(f"Place {console}_wireframe.png in main/images/Wireframes/", True, (60, 80, 60))
            surface.blit(lbl_nf1, (draw_x + iw // 2 - lbl_nf1.get_width() // 2, draw_y + ih // 2 - 20))
            surface.blit(lbl_nf2, (draw_x + iw // 2 - lbl_nf2.get_width() // 2, draw_y + ih // 2 + 8))

        # ── Sequential remap state ─────────────────────────────────────────────
        # Button order used for sequential auto-advance remapping (console-specific)
        # BTN_SEQ is already set above from cdef

        # Button label lookup (for display)
        _XI_LABELS = {
            _XI_UP: "D-Up", _XI_DOWN: "D-Down", _XI_LEFT: "D-Left", _XI_RIGHT: "D-Right",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_LB: "LB", _XI_RB: "RB",
            _XI_A: "A-btn", _XI_B: "B-btn", _XI_X: "X-btn", _XI_Y: "Y-btn",
            _XI_LT: "LT", _XI_RT: "RT",
        }

        def _ctrl_label_for_btn(btn):
            """Return readable controller button label for this button.
            Reads self._libretro_xinput_map directly (the actual runtime
            gameplay binding) rather than reverse-deriving a label from VK
            codes -- that old approach silently collapsed GG's/NGP's
            intentional two-physical-buttons-per-action defaults down to
            whichever alias was processed last, which is why a GG button
            could display as bound to a face button GG doesn't even have."""
            custom = getattr(self, "_ctrl_labels", {}).get(btn)
            if custom:
                return custom
            xi_bit = self._libretro_xinput_map.get(console, {}).get(btn)
            if xi_bit is not None:
                return _XI_LABELS.get(xi_bit, f"XI{xi_bit:04X}")
            return "---"

        # ── Button layout: labels closer to image ──────────────────────────────
        # LEFT_BTNS and RIGHT_BTNS already set from cdef above

        # Columns sit ~18% inward from edges — much closer to the console image
        left_col_right_edge  = x + int(w * 0.22)   # right edge of left label block
        right_col_left_edge  = x + int(w * 0.78)   # left edge of right label block

        label_h         = int(ih * 0.20) if not LEFT_BTNS else max(int(ih * 0.10), int(ih * 0.96 / max(len(LEFT_BTNS), len(RIGHT_BTNS), 1)))
        label_block_top = draw_y + int(ih * 0.02)

        active_btn = self.ctrl_mapping_active_btn
        seq_running = getattr(self, "_seq_remap_active", False)

        def _btn_screen_pos(btn_name):
            """Hotspot fraction → screen px within the drawn image."""
            fx, fy = HOTSPOTS.get(btn_name, (0.5, 0.5))
            return (int(draw_x + fx * iw), int(draw_y + fy * ih))

        def _draw_btn_label(btn, right_edge, row_y, align_right=True):
            """Draw a label right-aligned to right_edge (left col) or left-aligned (right col).

            Matches the Remote Remapping style (System tab): the button name
            sits as plain text, and right next to it is a small persistent
            box showing what's currently bound to it (e.g. name "A" next to
            a box reading "B" if A and B got swapped). The box never hides —
            it just switches to "..." and a highlighted border while this
            button is awaiting a press, instead of popping in and out.
            """
            is_active = (active_btn == btn)
            is_next   = seq_running and not is_active and (
                BTN_SEQ.index(btn) == BTN_SEQ.index(active_btn)
                if (active_btn in BTN_SEQ and btn in BTN_SEQ) else False)

            name_col = (255, 220, 0) if is_active else (180, 180, 180)
            name_surf = f_label.render(btn, True, name_col)
            value_text = "..." if is_active else _ctrl_label_for_btn(btn)

            # Value box — half the left-to-right size of the old pop-in/out
            # highlight box, and always visible instead of only while active.
            VAL_BOX_W, VAL_BOX_H = 45, name_surf.get_height() + 6
            BOX_GAP = 6

            if align_right:
                # Left column: name text right-aligned so the value box's
                # right edge lands exactly on right_edge (same anchor the
                # old combined label used).
                box_x  = right_edge - VAL_BOX_W
                name_x = box_x - BOX_GAP - name_surf.get_width()
            else:
                # Right column: name text left-aligned to right_edge, box
                # follows immediately after it.
                name_x = right_edge
                box_x  = name_x + name_surf.get_width() + BOX_GAP

            name_y = row_y - name_surf.get_height() // 2
            box_y  = row_y - VAL_BOX_H // 2

            surface.blit(name_surf, (name_x, name_y))

            box_bg = (90, 30, 30) if is_active else (60, 60, 80)
            pygame.draw.rect(surface, box_bg, (box_x, box_y, VAL_BOX_W, VAL_BOX_H), border_radius=4)
            box_border = ui_color if is_active else (120, 120, 140)
            pygame.draw.rect(surface, box_border, (box_x, box_y, VAL_BOX_W, VAL_BOX_H), 2, border_radius=4)

            val_surf = f_small.render(value_text, True, (255, 255, 255))
            if val_surf.get_width() > VAL_BOX_W - 6:
                f_val_tiny = _cached_font("Courier New", 10, bold=True)
                val_surf = f_val_tiny.render(value_text, True, (255, 255, 255))
            surface.blit(val_surf, (box_x + VAL_BOX_W // 2 - val_surf.get_width() // 2,
                                     box_y + VAL_BOX_H // 2 - val_surf.get_height() // 2))

            # Connector line from the assembled label+box edge → button hotspot dot
            hotspot = _btn_screen_pos(btn)
            if align_right:
                line_anchor = (right_edge + 4, row_y)
            else:
                line_anchor = (name_x - 4, row_y)
            dim = (ui_color[0] // 4, ui_color[1] // 4, ui_color[2] // 4)
            bright = ui_color if is_active else (ui_color[0] // 2, ui_color[1] // 2, ui_color[2] // 2)
            pygame.draw.line(surface, dim, line_anchor, hotspot, 1)
            pygame.draw.circle(surface, bright, hotspot, 4 if is_active else 3)

            rect_left   = min(name_x, box_x) - 4
            rect_right  = max(name_x + name_surf.get_width(), box_x + VAL_BOX_W) + 4
            rect_top    = min(name_y, box_y) - 2
            rect_bottom = max(name_y + name_surf.get_height(), box_y + VAL_BOX_H) + 2
            return pygame.Rect(rect_left, rect_top, rect_right - rect_left, rect_bottom - rect_top)


        btn_rects = {}

        for i, btn in enumerate(LEFT_BTNS):
            row_y = label_block_top + i * label_h + label_h // 2
            r = _draw_btn_label(btn, left_col_right_edge, row_y, align_right=True)
            btn_rects[btn] = r

        for i, btn in enumerate(RIGHT_BTNS):
            row_y = label_block_top + i * label_h + label_h // 2
            r = _draw_btn_label(btn, right_col_left_edge, row_y, align_right=False)
            btn_rects[btn] = r

        self._mapping_btn_rects = btn_rects

        # ── Bottom section: 3 navigable options ─────────────────────────────
        # cursor comes from app_state via the db-less approach: stored on self
        cursor = getattr(self, "_ctrl_map_cursor", 0)  # 0=Map 1=Stick 2=Close

        bottom_y = y + int(h * 0.82)
        # separator line removed

        opt_y     = bottom_y + 18
        opt_gap   = 34
        center_x  = x + w // 2

        def _draw_option(idx, label, y_pos, extra_right=None):
            """Draw one of the three bottom options. extra_right is drawn to the right (e.g. toggle).
            The focused option is shown purely by switching the text to the current theme
            color — no surrounding highlight box."""
            is_sel = (cursor == idx)
            col    = ui_color if is_sel else (180, 180, 180)
            lbl    = f_small.render(label, True, col)
            lx     = center_x - lbl.get_width() // 2
            if extra_right:
                # If there's a toggle widget we shift the text left a bit
                lx = center_x - (lbl.get_width() + 10 + extra_right.get_width()) // 2

            surface.blit(lbl, (lx, y_pos))
            if extra_right:
                surface.blit(extra_right, (lx + lbl.get_width() + 10, y_pos))
            return pygame.Rect(lx - 5, y_pos - 3,
                               lbl.get_width() + 10 + (10 + extra_right.get_width() if extra_right else 0),
                               lbl.get_height() + 6)

        # Option 0: Map Controller
        if seq_running and active_btn and active_btn in BTN_SEQ:
            seq_idx  = BTN_SEQ.index(active_btn)
            total_btns = len(BTN_SEQ)
            map_label = f"MAPPING {seq_idx + 1}/{total_btns}  —  press button for: {active_btn}"
        elif seq_running and active_btn:
            map_label = f"MAPPING  —  press button for: {active_btn}"
        else:
            map_label = "[ MAP CONTROLLER ]"
        r0 = _draw_option(0, map_label, opt_y)

        # Option 1: L-Stick as D-Pad  (on/off toggle matching rest of menu style)
        sad      = self.ctrl_mapping.get("stick_as_dpad", False)
        # Draw the pill-style toggle the same way the rest of the menus do
        sw_bg    = (40, 167, 69) if sad else (45, 65, 100)
        sw_w, sw_h = 46, 20
        sw_surf  = pygame.Surface((sw_w, sw_h), pygame.SRCALPHA)
        pygame.draw.rect(sw_surf, sw_bg, (0, 0, sw_w, sw_h), border_radius=10)
        handle_x = sw_w - 12 if sad else 4
        pygame.draw.circle(sw_surf, (255, 255, 255), (handle_x + 6, sw_h // 2), 8)
        r1 = _draw_option(1, "L-STICK AS D-PAD", opt_y + opt_gap, sw_surf)
        self._mapping_sad_rect = r1

        # Option 2: Close — same red pill button used at the bottom-right of
        # every other sub-menu's content area (see GAMES_SUB_MENU / System /
        # Channels / Theme close buttons in render_settings_overlay_menu).
        OFF_RED = (220, 53, 69)
        close_w, close_h = 85, 30
        close_x = x + w - 110
        close_y = y + h - 42
        is_close_sel = (cursor == 2)
        pygame.draw.rect(surface, OFF_RED, (close_x, close_y, close_w, close_h), border_radius=4)
        if is_close_sel:
            pygame.draw.rect(surface, ui_color, (close_x, close_y, close_w, close_h), 2, border_radius=4)
        lbl_close = f_title.render("Close", True, (255, 255, 255))
        surface.blit(lbl_close, (close_x + close_w // 2 - lbl_close.get_width() // 2,
                                  close_y + close_h // 2 - lbl_close.get_height() // 2))
        r2 = pygame.Rect(close_x, close_y, close_w, close_h)
        self._mapping_close_rect = r2

        # Store rects for click detection (options)
        self._mapping_opt_rects = [r0, r1, r2]

        # Hint only while a remap capture is actually in progress — the
        # static "W/S navigate / Enter select / ESC back" hint has been removed.
        if seq_running and active_btn:
            ctrl_bottom = y + h - 6
            hint_lbl = f_small.render("ESC to cancel remap", True, (120, 120, 60))
            surface.blit(hint_lbl, (center_x - hint_lbl.get_width() // 2, ctrl_bottom - 14))

    def handle_mapping_click(self, mouse_pos):
        """Route a mouse click inside the mapping UI. Returns True if consumed."""
        console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        # Check the three bottom option rects first (Map / Stick / Close)
        opt_rects = getattr(self, "_mapping_opt_rects", [])
        for idx, rect in enumerate(opt_rects):
            if rect and rect.collidepoint(mouse_pos):
                self._ctrl_map_cursor = idx   # move cursor to clicked option
                if idx == 0:
                    if not getattr(self, "_seq_remap_active", False):
                        self.start_sequential_remap(console)
                elif idx == 1:
                    self.ctrl_mapping["stick_as_dpad"] = not self.ctrl_mapping.get("stick_as_dpad", False)
                    self._save_controller_mapping(console)
                elif idx == 2:
                    self._mapping_close_requested = True
                    self.ctrl_mapping_active_btn = None
                    self._seq_remap_active = False
                return True

        # Individual GBA button label click in the wireframe diagram area
        rects = getattr(self, "_mapping_btn_rects", {})
        for btn, rect in rects.items():
            if rect.collidepoint(mouse_pos):
                if self.ctrl_mapping_active_btn == btn:
                    self.ctrl_mapping_active_btn = None
                    self._seq_remap_active = False
                else:
                    self.ctrl_mapping_active_btn = btn
                    self._seq_remap_active = False
                    # Same one-capture-per-press gate for single remaps.
                    self._remap_await_release  = True
                    self._remap_last_capture_ts = time.monotonic()
                    print(f"[GAMEDECK MAPPING] Single remap: waiting for {btn}")
                return True

        return False


    def start_sequential_remap(self, console=None):
        """Begin the auto-advance sequential remap flow (all buttons in order)."""
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        cdef    = _get_console_def(console)
        BTN_SEQ = cdef["btn_seq"]
        if not BTN_SEQ:
            print(f"[GAMEDECK MAPPING] {console} has no mappable buttons.")
            return
        self._seq_remap_active = True
        self.ctrl_mapping_active_btn = BTN_SEQ[0]
        # Require the pad to be released before the first capture, so whatever
        # button/key launched the remap can't be recorded into slot 1.
        self._remap_await_release  = True
        self._remap_last_capture_ts = time.monotonic()
        if not hasattr(self, "_ctrl_labels"):
            self._ctrl_labels = {}
        print(f"[GAMEDECK MAPPING] Sequential remap started for {console} — press controller button for: {BTN_SEQ[0]}")

    def poll_xinput_for_remap(self, console=None):
        """Call every frame while mapping UI is open.
        In sequential mode: captures XInput press, logs it, auto-advances to next button.
        In single mode: captures one press, saves, clears active_btn."""
        if self.ctrl_mapping_active_btn is None:
            return
        if not _WIN:
            return

        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        cdef    = _get_console_def(console)
        BTN_SEQ = cdef["btn_seq"]

        # XInput state structs
        class _GP(ctypes.Structure):
            _fields_ = [("wButtons", ctypes.c_ushort),
                        ("bLT", ctypes.c_ubyte), ("bRT", ctypes.c_ubyte),
                        ("sLX", ctypes.c_short), ("sLY", ctypes.c_short),
                        ("sRX", ctypes.c_short), ("sRY", ctypes.c_short)]
        class _XS(ctypes.Structure):
            _fields_ = [("dwPkt", ctypes.c_ulong), ("Gamepad", _GP)]

        _XI_LABELS = {
            _XI_UP: "D-Up", _XI_DOWN: "D-Down", _XI_LEFT: "D-Left", _XI_RIGHT: "D-Right",
            _XI_START: "Start", _XI_BACK: "Back",
            _XI_LB: "LB", _XI_RB: "RB",
            _XI_A: "A-btn", _XI_B: "B-btn", _XI_X: "X-btn", _XI_Y: "Y-btn",
            _XI_LT: "LT", _XI_RT: "RT",
        }

        # LT/RT are analog bytes (0-255), not bits in wButtons, so a trigger
        # pull needs its own threshold-crossing check below instead of a
        # bitmask test — that's why L1/R1 registered here before but L2/R2
        # (which this frontend maps onto the LT/RT triggers) never did.
        TRIGGER_PRESS_THRESHOLD = 30  # ignores light/accidental trigger touches

        for lib in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                xi    = ctypes.windll.LoadLibrary(lib)
                state = _XS()
                if xi.XInputGetState(0, ctypes.byref(state)) == 0:
                    btns   = state.Gamepad.wButtons
                    cur_lt = state.Gamepad.bLT >= TRIGGER_PRESS_THRESHOLD
                    cur_rt = state.Gamepad.bRT >= TRIGGER_PRESS_THRESHOLD

                    fully_released = (btns == 0 and not cur_lt and not cur_rt)

                    # ── One-capture-per-press gate ──────────────────────────
                    # After ANY button is captured we set _remap_await_release
                    # and refuse to capture anything else until the pad returns
                    # to a fully-neutral state (no buttons, no triggers). This
                    # is what stops a button that's held a beat too long — whose
                    # raw XInput reading can flicker/bounce for a frame or two —
                    # from having its single press rolled into the NEXT mapping
                    # slot as well. It also blocks the button that was used to
                    # start the remap from bleeding into the first slot.
                    if getattr(self, "_remap_await_release", False):
                        if fully_released:
                            self._remap_await_release = False
                            self._last_xinput_btns = 0
                            self._last_xinput_lt   = False
                            self._last_xinput_rt   = False
                        return

                    # Debounce: skip if nothing changed since last frame —
                    # "nothing" now covers the triggers too, not just wButtons.
                    last_btns = getattr(self, "_last_xinput_btns", 0)
                    last_lt   = getattr(self, "_last_xinput_lt", False)
                    last_rt   = getattr(self, "_last_xinput_rt", False)
                    if btns == last_btns and cur_lt == last_lt and cur_rt == last_rt:
                        return
                    self._last_xinput_lt = cur_lt
                    self._last_xinput_rt = cur_rt

                    if btns == 0 and not cur_lt and not cur_rt:
                        # Everything released — reset debounce
                        self._last_xinput_btns = 0
                        return
                    self._last_xinput_btns = btns

                    # Find the first pressed input: digital buttons first, then
                    # a freshly-pulled (not already held) trigger.
                    #
                    # IMPORTANT: this must check every physical button that
                    # could possibly exist on the pad (A/B/X/Y/LB/RB/Start/
                    # Back/D-pad), NOT just whichever ones happen to appear in
                    # the console currently being remapped's own xinput_map.
                    # It previously looped over XINPUT_TO_GBA_BTN (GBA's map)
                    # as a stand-in for "all buttons" — but GBA has no X/Y/L/R,
                    # so those bits were simply absent from that dict. Remapping
                    # any console that DOES use them (SNES, Genesis, N64...)
                    # meant pressing X/Y/L/R was invisible to this loop: the
                    # sequential remap would just sit there waiting forever,
                    # and any OTHER button you pressed trying to get a
                    # response would get captured into whatever slot was
                    # active instead — which is how every SNES button ends up
                    # bound to the same key after a confused remap session.
                    pressed_bit  = None
                    pressed_name = None
                    for bit, xi_name in _XI_LABELS.items():
                        if bit in (_XI_LT, _XI_RT):
                            continue  # analog triggers, handled separately below
                        if btns & bit:
                            pressed_bit  = bit
                            pressed_name = xi_name
                            break

                    if pressed_bit is None:
                        if cur_lt and not last_lt:
                            pressed_bit = _XI_LT
                        elif cur_rt and not last_rt:
                            pressed_bit = _XI_RT

                    if pressed_bit is None:
                        return

                    # Double-press cooldown: even after a clean release, ignore
                    # a new press that lands within this window of the last
                    # capture. A fast double-tap (press → release → press) would
                    # otherwise satisfy the release gate above and fill the next
                    # slot with the same button; this makes a single mapping
                    # accept exactly one press.
                    REMAP_CAPTURE_COOLDOWN = 0.30  # seconds
                    now = time.monotonic()
                    if now - getattr(self, "_remap_last_capture_ts", 0.0) < REMAP_CAPTURE_COOLDOWN:
                        return

                    active    = self.ctrl_mapping_active_btn
                    xi_label  = _XI_LABELS.get(pressed_bit, f"XI{pressed_bit:04X}")

                    # Map this XInput button → the VK code of whatever console action
                    # it natively corresponds to in the console's default mapping.
                    console_xinput_map = cdef["xinput_map"]
                    console_default    = cdef["default_map"]
                    pressed_console_btn = console_xinput_map.get(pressed_bit, "")
                    target_vk = console_default.get(pressed_console_btn, 0)
                    self.ctrl_mapping[active] = target_vk
                    # console-scoped — see _build_default_libretro_xinput_map for why
                    # this must never be a single flat dict keyed by btn_name alone.
                    self._libretro_xinput_map.setdefault(console, {})[active] = pressed_bit

                    # Store a human-readable controller label for display
                    if not hasattr(self, "_ctrl_labels"):
                        self._ctrl_labels = {}
                    self._ctrl_labels[active] = xi_label

                    print(f"[GAMEDECK MAPPING] {active} → {xi_label} (VK {target_vk:#04x})")

                    # Arm the one-capture-per-press gate + cooldown so this same
                    # press can't spill into the button we're about to advance
                    # to. Cleared only once the pad is fully released again.
                    self._remap_await_release  = True
                    self._remap_last_capture_ts = now

                    seq_running = getattr(self, "_seq_remap_active", False)
                    if seq_running:
                        # Advance to next button in sequence
                        if active in BTN_SEQ:
                            idx = BTN_SEQ.index(active)
                            if idx + 1 < len(BTN_SEQ):
                                next_btn = BTN_SEQ[idx + 1]
                                self.ctrl_mapping_active_btn = next_btn
                                print(f"[GAMEDECK MAPPING] Next: {next_btn}")
                            else:
                                # Sequence complete
                                self._seq_remap_active = False
                                self.ctrl_mapping_active_btn = None
                                self._save_controller_mapping(console)
                                self._save_libretro_xinput_map(console)
                                print(f"[GAMEDECK MAPPING] Sequential remap complete for {console} — saved.")
                        else:
                            self._seq_remap_active = False
                            self.ctrl_mapping_active_btn = None
                            self._save_controller_mapping(console)
                            self._save_libretro_xinput_map(console)
                    else:
                        # Single-button remap — done
                        self.ctrl_mapping_active_btn = None
                        self._save_controller_mapping(console)
                        self._save_libretro_xinput_map(console)
                break
            except Exception:
                continue


# ==============================================================================
# PART 10a OF 13: RAW PNG TEXTURE PRESERVATIONS
# ==============================================================================

    def _ensure_fonts(self):
        if self._font_big is None:
            self._font_big   = pygame.font.SysFont("Arial", 30, bold=True)
            self._font_mid   = pygame.font.SysFont("Arial", 22, bold=True)
            self._font_small = pygame.font.SysFont("Courier New", 18, bold=True)

    def _get_logo(self, console, target_h):
        cache = self._logo_cache.setdefault(console, {})
        if target_h in cache:
            return cache[target_h]
        surf = None
        if console in self._logo_raw_cache:
            raw = self._logo_raw_cache[console]
        else:
            path = os.path.join(self.logos_dir, f"{console}.png")
            raw = pygame.image.load(path).convert_alpha() if os.path.exists(path) else None
        if raw is not None:
            try:
                ratio = target_h / raw.get_height()
                surf  = pygame.transform.smoothscale(
                    raw, (int(raw.get_width() * ratio), int(target_h)))
            except Exception:
                surf = None
        cache[target_h] = surf
        return surf

    def set_theme(self, ui_color):
        if ui_color: self.ui_color = ui_color


# ==============================================================================
# PART 10b OF 13: CORE SCREEN RENDERS & DUPLICATE-SAFE CAROUSEL
# ==============================================================================

    def _draw_legend(self, screen, cx, y, h, groups):
        f     = self._font_small
        col_w = 130
        row_h = 18
        total_w = col_w * len(groups)
        block_x = cx - total_w // 2
        y_title = y + h - 3 * row_h - 8
        y_ctrl  = y_title + row_h
        y_kb    = y_ctrl  + row_h
        title_col = (130, 130, 130)
        key_col   = (80, 80, 80)
        for i, (action, ctrl_key, kb_key) in enumerate(groups):
            col_cx = block_x + i * col_w + col_w // 2
            for text, color, row_y in [
                (action,   title_col, y_title),
                (ctrl_key, key_col,   y_ctrl),
                (kb_key,   key_col,   y_kb),
            ]:
                lbl = f.render(text, True, color)
                screen.blit(lbl, (col_cx - lbl.get_width() // 2, row_y))

    def render(self, screen, rect):
        self._ensure_fonts()
        x, y, w, h = rect
        cx = x + w // 2
        screen.fill((0, 0, 0), rect)

        if self.mode == "GAME":
            if getattr(self, "emulator_loading", False):
                # ── LOADING SCREEN ────────────────────────────────────────────
                screen.fill((0, 0, 0), rect)

                pct = getattr(self, "emulator_load_pct", 0)
                msg = getattr(self, "emulator_load_msg", "Loading...")

                logo_target_h = max(160, int(h * 0.32))
                logo_centre_y = y + int(h * 0.28)
                logo_surf = None
                if self.active_console:
                    logo_surf = self._get_logo(self.active_console, logo_target_h)
                if logo_surf is not None:
                    screen.blit(logo_surf,
                                (cx - logo_surf.get_width() // 2,
                                 logo_centre_y - logo_surf.get_height() // 2))
                else:
                    f_big = self._font_big or self._font_mid
                    lbl_c = f_big.render(
                        CONSOLE_LABELS.get(self.active_console, self.active_console or ""),
                        True, self.ui_color)
                    screen.blit(lbl_c, (cx - lbl_c.get_width() // 2,
                                        logo_centre_y - lbl_c.get_height() // 2))

                lbl_m = self._font_mid.render(msg, True, (150, 150, 150))
                screen.blit(lbl_m, (cx - lbl_m.get_width() // 2, y + h // 2 - 30))

                import math as _math
                display_pct = int(_math.sqrt(pct / 100.0) * 100) if pct < 100 else 100
                bw = int(w * 0.5); bh = 10
                bx = cx - bw // 2;  by = y + h // 2 + 12
                pygame.draw.rect(screen, (22, 22, 22), (bx, by, bw, bh), border_radius=5)
                filled = int(bw * display_pct / 100)
                if filled > 0:
                    pygame.draw.rect(screen, self.ui_color,
                                     (bx, by, filled, bh), border_radius=5)
                    pygame.draw.circle(screen, self.ui_color,
                                       (bx + filled, by + bh // 2), bh)

                lbl_p = self._font_small.render(f"{display_pct}%", True, (80, 80, 80))
                screen.blit(lbl_p, (cx - lbl_p.get_width() // 2, by + bh + 10))

                lbl_h = self._font_small.render("CH+/CH\u2212  cancel", True, (35, 35, 35))
                screen.blit(lbl_h, (cx - lbl_h.get_width() // 2, y + h - 28))

            else:
                # ── GAME RUNNING ───────────────────────────────────────────────
                # mGBA is a child window z-ordered to HWND_BOTTOM so the layered
                # UI overlay child (above it) and pygame renders (menus, OSD,
                # volume bar) always appear on top without any hide/show needed.
                # Just fill the client area with black so it shows through the
                # transparent UI overlay wherever no UI element is drawn.
                screen.fill((0, 0, 0), rect)

            return

        if self.mode == "DVD_PLAYER":
            self._render_dvd_player(screen, rect)
            return

        if self.mode == "BROWSE":    self._render_carousel(screen, rect)
        elif self.mode == "ROMLIST": self._render_romlist(screen, rect)


# ==============================================================================
# PART 10c OF 13: COLUMN-ALIGNED CAROUSEL & ROM LIST PIPELINES
# ==============================================================================

    def _render_carousel(self, screen, rect):
        x, y, w, h = rect
        cx = x + w // 2
        cy = y + h // 2
        n = len(self.consoles)
        if n == 0: return

        if n == 1:
            slots = [(0, cy, int(h * 0.38), (255, 255, 255))]
        elif n == 2:
            if self.console_index == 0:
                slots = [
                    (0, cy,                 int(h * 0.38), (255, 255, 255)),
                    (1, cy + int(h * 0.22), int(h * 0.22), (255, 255, 255)),
                ]
            else:
                slots = [
                    (-1, cy - int(h * 0.22), int(h * 0.22), (255, 255, 255)),
                    (0,  cy,                 int(h * 0.38), (255, 255, 255)),
                ]
        else:
            slots = [
                (-1, cy - int(h * 0.22), int(h * 0.22), (255, 255, 255)),
                (0,  cy,                 int(h * 0.38), (255, 255, 255)),
                (1,  cy + int(h * 0.22), int(h * 0.22), (255, 255, 255)),
            ]

        for offset, slot_y, logo_h, color in slots:
            idx     = (self.console_index + offset) % n
            console = self.consoles[idx]
            logo    = self._get_logo(console, logo_h)
            if logo is not None:
                screen.blit(logo, (cx - logo.get_width() // 2, slot_y - logo.get_height() // 2))
            else:
                font = self._font_big if offset == 0 else self._font_mid
                lbl  = font.render(CONSOLE_LABELS.get(console, console), True, self.ui_color)
                screen.blit(lbl, (cx - lbl.get_width() // 2, slot_y - lbl.get_height() // 2))

        self._draw_legend(screen, cx, y, h, [
            ("Navigate", "↑↓",  "W/S"),
            ("Select",   "A",   "Enter"),
            ("Back",     "B",   "ESC"),
        ])

    def _render_romlist(self, screen, rect):
        x, y, w, h = rect
        cx = x + w // 2
        console = self.active_console or ""

        header_logo_h = int(h * 0.15)
        logo_header   = self._get_logo(console, header_logo_h)

        if logo_header is not None:
            screen.blit(logo_header, (cx - logo_header.get_width() // 2, y + 20))
            list_top_offset = 20 + logo_header.get_height() + 20
        else:
            title = self._font_big.render(CONSOLE_LABELS.get(console, console), True, self.ui_color)
            screen.blit(title, (cx - title.get_width() // 2, y + 24))
            list_top_offset = 90

        _romlist_groups = [
            ("Scroll", "↑↓",  "UP/DOWN"),
            ("Launch", "A",   "Enter"),
            ("Back",   "B",   "ESC"),
        ]

        # Hold-to-scroll: fire continuous steps while W/Up or S/Down is held.
        # Must run every frame regardless of roms being empty so the hold
        # state resets cleanly when the list clears between console switches.
        self._poll_rom_hold()

        if not self.roms:
            msg = self._font_mid.render(self.status_msg or "No files found.", True, (200, 80, 80))
            screen.blit(msg, (cx - msg.get_width() // 2, y + h // 2 - 12))
            self._draw_legend(screen, cx, y, h, _romlist_groups)
            return

        line_h  = 35
        visible = max(3, (h - list_top_offset - 80) // line_h)
        if self.rom_index < self.rom_scroll:
            self.rom_scroll = self.rom_index
        elif self.rom_index >= self.rom_scroll + visible:
            self.rom_scroll = self.rom_index - visible + 1

        list_start_y = y + list_top_offset
        for row in range(visible):
            i = self.rom_scroll + row
            if i >= len(self.roms): break
            display_name, full_path = self.roms[i]
            selected     = (i == self.rom_index)
            color        = self.ui_color if selected else (200, 200, 200)
            display_str  = f"> {display_name} <" if selected else display_name
            lbl = self._font_mid.render(display_str, True, color)
            screen.blit(lbl, (cx - lbl.get_width() // 2, list_start_y + row * line_h))

        self._draw_legend(screen, cx, y, h, _romlist_groups)


# ==============================================================================
# PART 11a OF 13: CORE DVD PLAYHEAD HOOKS
# ==============================================================================

    def _render_dvd_load_screen(self, screen, rect):
        """DVD's boot screen. Deliberately mirrors the game console loading
        screen drawn in render() (the 'emulator_loading' branch) — same logo
        position/size, same progress-bar geometry/easing, same fonts — just
        swapping in the DVD logo/message in place of the emulated console's.
        Replaces the old DVD_LoadVideo.mp4 video splash clip.
        """
        self._ensure_fonts()
        x, y, w, h = rect
        cx = x + w // 2
        screen.fill((0, 0, 0), rect)

        elapsed = pygame.time.get_ticks() - getattr(self, "_dvd_splash_start_time", 0)
        pct = max(0, min(100, int(elapsed * 100 / self.DVD_LOAD_SCREEN_MS)))
        msg = "Loading DVD..."

        logo_target_h = max(160, int(h * 0.32))
        logo_centre_y = y + int(h * 0.28)
        logo_surf = self._get_logo("DVD", logo_target_h)
        if logo_surf is not None:
            screen.blit(logo_surf,
                        (cx - logo_surf.get_width() // 2,
                         logo_centre_y - logo_surf.get_height() // 2))
        else:
            f_big = self._font_big or self._font_mid
            lbl_c = f_big.render(CONSOLE_LABELS.get("DVD", "DVD"), True, self.ui_color)
            screen.blit(lbl_c, (cx - lbl_c.get_width() // 2,
                                logo_centre_y - lbl_c.get_height() // 2))

        lbl_m = self._font_mid.render(msg, True, (150, 150, 150))
        screen.blit(lbl_m, (cx - lbl_m.get_width() // 2, y + h // 2 - 30))

        import math as _math
        display_pct = int(_math.sqrt(pct / 100.0) * 100) if pct < 100 else 100
        bw = int(w * 0.5); bh = 10
        bx = cx - bw // 2;  by = y + h // 2 + 12
        pygame.draw.rect(screen, (22, 22, 22), (bx, by, bw, bh), border_radius=5)
        filled = int(bw * display_pct / 100)
        if filled > 0:
            pygame.draw.rect(screen, self.ui_color,
                             (bx, by, filled, bh), border_radius=5)
            pygame.draw.circle(screen, self.ui_color,
                               (bx + filled, by + bh // 2), bh)

        lbl_p = self._font_small.render(f"{display_pct}%", True, (80, 80, 80))
        screen.blit(lbl_p, (cx - lbl_p.get_width() // 2, by + bh + 10))

    def _render_dvd_player(self, screen, rect):
        x, y, w, h = rect
        cx = x + w // 2

        import sys
        main_module = sys.modules.get("__main__")
        if main_module and getattr(main_module, "dvd_engine", None) is not None:
            self.vlc_engine = main_module.dvd_engine

        vlc = self.vlc_engine
        now = pygame.time.get_ticks()

        if self.dvd_splash_playing:
            splash_elapsed = now - getattr(self, "_dvd_splash_start_time", 0)
            if splash_elapsed >= self.DVD_LOAD_SCREEN_MS:
                self.dvd_splash_playing = False
                self.dvd_splash_done    = True
                self._dvd_main_start_time  = pygame.time.get_ticks()
                self._dvd_main_ever_played = False
                self._play_dvd_main()
            else:
                # Load screen still up — paint it (DVD logo + progress bar,
                # same position/size/style as the game console loading
                # screens) and skip the video-frame/OSD/exit-confirm drawing
                # below entirely, same as the old video-splash path did.
                self._render_dvd_load_screen(screen, rect)
                return

        # THIRD (SUPPLEMENTARY) MENU-SIGNAL SOURCE: a position discontinuity
        # like this IS the defining behavior of any DVD menu system
        # (looping/jumping cells), so on top of logging, a jump seen WHILE
        # dvd_in_menu is already True reinforces vlc's rolling menu-signal
        # window via note_menu_activity() (see VLCEngineWrapper's v3
        # DVD MENU-SIGNAL SUBSYSTEM in __init__/_poll_dvd_menu_signals for
        # the two PRIMARY sources -- libdvdnav's own DVDNAV_HIGHLIGHT and
        # DVDNAV_CELL_CHANGE events, parsed straight from vlc-debug.log).
        # This is a fully independent, derived signal (position vs.
        # wall-clock) that has empirically caught at least one real
        # transition the two primary sources alone missed. Gated on
        # dvd_in_menu already being True so this can only ever reinforce an
        # established menu state, never assert one from cold -- a real
        # transport seek can't trigger it since transport controls are
        # blocked while dvd_in_menu is True in the first place, so there's
        # no path for a real seek to be mistaken for menu activity here.
        if (not self.dvd_splash_playing and self.dvd_splash_done
                and not self.dvd_exit_confirm and vlc is not None
                and self.dvd_is_disc_image):
            try:
                _t_now_wall = now
                _t_pos = vlc.player.get_time()
                _last_wall = getattr(self, "_dvd_jump_watch_wall", None)
                _last_pos = getattr(self, "_dvd_jump_watch_pos", None)
                if _last_wall is not None and _last_pos is not None and _t_pos is not None and _t_pos >= 0:
                    _wall_delta = _t_now_wall - _last_wall
                    _pos_delta = _t_pos - _last_pos
                    # Real playback position should track wall time 1:1 (give
                    # or take normal frame jitter). A position delta wildly
                    # bigger than the wall-clock delta -- or a big jump
                    # backward -- means dvdnav just moved the read head to a
                    # different cell/PGC, not that time is "running fast."
                    if _wall_delta > 0 and (abs(_pos_delta - _wall_delta) > 1500):
                        _title_now = getattr(vlc, "_current_title", "?")
                        print(f"[DVD DEBUG] TIME-JUMP DETECTED: pos {_last_pos}ms -> {_t_pos}ms "
                              f"(delta={_pos_delta}ms) over {_wall_delta}ms of wall time, "
                              f"title={_title_now}")
                        if self.dvd_in_menu:
                            try:
                                vlc.note_menu_activity()
                            except Exception as _e:
                                _log_warn_throttled("dvd_note_menu_activity",
                                                     "note_menu_activity() failed: %s", _e)
                        try:
                            _tail = vlc.get_recent_vlc_log()
                            if _tail:
                                print(f"[DVD DEBUG] vlc-debug.log tail at time-jump:\n{_tail}")
                        except Exception as _e:
                            print(f"[DVD DEBUG] could not fetch vlc-debug.log tail: {_e}")
                self._dvd_jump_watch_wall = _t_now_wall
                self._dvd_jump_watch_pos = _t_pos
            except Exception as _e:
                _log_warn_throttled("dvd_time_jump_watch", "DVD time-jump watch failed this frame: %s", _e)

        # DVD-ISO OPEN WATCHDOG + MENU-VS-TITLE AUTO-DETECT (see
        # DVD_ISO_OPEN_TIMEOUT_MS / DVD_MENU_FLIP_DEBOUNCE_MS and the
        # dvd_in_menu comment block in __init__). Runs every frame the main
        # disc-image video is active. Deliberately NOT gated behind the
        # 2s-delayed main-video-ended check below (that delay exists to
        # avoid false positives on a video that's already playing fine --
        # the watchdog has to be able to act well before that if dvdnav
        # never opens the image at all).
        if (not self.dvd_splash_playing and self.dvd_splash_done
                and not self.dvd_exit_confirm and vlc is not None
                and self.dvd_is_disc_image):
            import vlc as raw_vlc
            state = None
            try:
                state = vlc.player.get_state()
            except Exception as e:
                _log_warn_throttled("dvd_iso_state_check", "DVD disc-image state check failed this frame: %s", e)

            if self._dvd_iso_watchdog_armed:
                if state in (raw_vlc.State.Playing, raw_vlc.State.Paused):
                    # dvdnav reached a playable state -- disc image opened
                    # successfully, nothing more for the watchdog to do.
                    self._dvd_iso_watchdog_armed = False
                    print(f"[DVD DEBUG] ISO-open watchdog disarmed -- state={state}, opened successfully.")
                else:
                    _armed_elapsed = now - self._dvd_iso_watchdog_armed_at
                    _fail_reason = None
                    if (state in (raw_vlc.State.Ended, raw_vlc.State.Error)
                            and _armed_elapsed >= self.DVD_ISO_OPEN_FAIL_GRACE_MS):
                        # Definitive failure -- dvdnav could not find a
                        # playable title in this image at all (bad/corrupt
                        # disc image, or a file with no real VIDEO_TS
                        # structure at all -- see DVD_ISO_OPEN_FAIL_GRACE_MS).
                        # No point waiting out the rest of the timeout on a
                        # black screen when VLC has already told us it's not
                        # going anywhere.
                        _fail_reason = f"dvdnav reported {state} after {_armed_elapsed}ms -- not a playable disc image"
                    elif now >= self._dvd_iso_watchdog_deadline:
                        # dvdnav never reached a playable state in time --
                        # a bad/corrupt disc image, or an encrypted commercial
                        # disc missing libdvdcss. Back out to the DVD list with
                        # an error instead of sitting on a frozen black screen.
                        _fail_reason = f"timed out after {self.DVD_ISO_OPEN_TIMEOUT_MS}ms (last state={state})"

                    if _fail_reason:
                        self._dvd_iso_watchdog_armed = False
                        try:
                            _vlc_err = vlc.get_last_error()
                        except Exception:
                            _vlc_err = ""
                        # get_last_error() (libvlc_errmsg) has proven
                        # unreliable for these failures -- it returned "No
                        # active input" on the SAW.ISO/Matrix.ISO tests,
                        # which is generic noise, not the real cause. Pull
                        # the actual module-level error out of
                        # vlc-debug.log instead (see get_recent_vlc_log's
                        # comment / --file-logging in vlc_args).
                        try:
                            _vlc_log_tail = vlc.get_recent_vlc_log()
                        except Exception:
                            _vlc_log_tail = ""
                        print(f"[DVD DEBUG] ISO-open watchdog FAILED: {_fail_reason} -- "
                              f"libVLC error: {_vlc_err or '(none reported)'} -- bailing out to DVD list.")
                        if _vlc_log_tail:
                            print(f"[DVD DEBUG] Recent vlc-debug.log lines:\n{_vlc_log_tail}")
                        else:
                            print("[DVD DEBUG] vlc-debug.log had no DVD/access/demux-relevant lines "
                                  "-- either the file wasn't created (check disk permissions / "
                                  "VLC_PLUGIN_PATH boot warnings) or the failure produced no module log output.")
                        try: vlc.stop()
                        except Exception as e:
                            log.warning("VLC stop after ISO-open watchdog failure failed: %s", e)
                        self.dvd_playback_active = False
                        self.dvd_splash_done = False
                        self.dvd_paused_frame = None
                        self.dvd_in_menu = False
                        self.mode = "ROMLIST"
                        # Turn the raw libVLC string into an actionable
                        # on-screen message where possible -- these two
                        # causes (missing CSS decryption vs. missing dvdnav
                        # access module entirely) both produce the same
                        # fast State.Ended, but need different fixes on
                        # disk, and the [BOOT WARNING] lines that identify
                        # which one applies only go to the log file, not
                        # the screen.
                        _err_lower = (_vlc_err or "").lower() + " " + (_vlc_log_tail or "").lower()
                        if "css" in _err_lower or "scrambl" in _err_lower or "decrypt" in _err_lower:
                            self.status_msg = "Disc is CSS-encrypted -- libdvdcss missing (see log)"
                        elif "access module" in _err_lower or "no suitable" in _err_lower:
                            self.status_msg = "DVD support missing from this VLC install (see log)"
                        elif _vlc_err:
                            self.status_msg = f"Could not open disc image ({_vlc_err})"
                        else:
                            self.status_msg = "Could not open disc image"
                        self._scan_roms("DVD")
                        return

            # MENU-VS-TITLE AUTO-DETECT: see VLCEngineWrapper.is_menu_active.
            # Debounced so a single mid-transition frame can't flicker
            # WASD's meaning back and forth.
            #
            # BIDIRECTIONAL, AND THE ONLY THING THAT SETS dvd_in_menu
            # DURING PLAY: is_menu_active() is title-based now (get_title()
            # >= 1 only once a real title is actually playing -- see its
            # comment for how EIKEN.iso's log confirmed this). That makes
            # it trustworthy in both directions, so pressing Enter/activate
            # in _handle_dvd_menu_nav no longer forces dvd_in_menu False
            # itself -- it used to, and that was the bug: activate fires
            # identically whether the button just chosen leads to a
            # submenu (still menu domain) or the real movie, so forcing
            # transport controls on every activate press left WASD dead in
            # submenus. Now transport is only granted once this check
            # confirms a real title is actually playing, and the menu
            # comes back the same way if the disc auto-returns to it after
            # the movie ends -- so the default, safe state everywhere in
            # the disc's menu system (top menu or any submenu) is always
            # menu-nav, exactly as requested.
            #
            # EXCEPT while _dvd_menu_manual_override is set: a deliberate
            # "return to menu" (Backspace confirm) forces dvd_in_menu True
            # even though get_title() stays >= 1 the whole time -- dvdnav's
            # popup/menu call shows an interactive menu OVER the
            # still-loaded title without changing its title index at all
            # (confirmed in app_log.txt), so this check would otherwise
            # immediately see "title still playing" and flip transport
            # controls right back on within one debounce window. Skip it
            # entirely until the user picks something off that menu (see
            # the activate handler in _handle_dvd_menu_nav, which clears
            # the override and hands control back to this check).
            if getattr(self, "_dvd_menu_manual_override", False):
                pass
            else:
                try:
                    candidate_in_menu = vlc.is_menu_active()
                except Exception:
                    candidate_in_menu = self.dvd_in_menu
                if candidate_in_menu != self._dvd_menu_flip_candidate:
                    self._dvd_menu_flip_candidate = candidate_in_menu
                    self._dvd_menu_flip_since = now
                elif (candidate_in_menu != self.dvd_in_menu
                        and now - self._dvd_menu_flip_since >= self.DVD_MENU_FLIP_DEBOUNCE_MS):
                    self.dvd_in_menu = candidate_in_menu
                    print(f"[DVD DEBUG] dvd_in_menu flipped to {candidate_in_menu} "
                      f"(WASD now controls {'menu navigation' if candidate_in_menu else 'transport'}; "
                      f"movie-playing check).")

        if (not self.dvd_splash_playing and self.dvd_splash_done
                and not self.dvd_exit_confirm and vlc is not None
                and not getattr(self, '_dvd_pending_pause', False)
                and pygame.time.get_ticks() > getattr(self, "_dvd_main_start_time", 0) + 2000):
            try:
                import vlc as raw_vlc
                state = vlc.player.get_state()
                if state == raw_vlc.State.Playing:
                    self._dvd_main_ever_played = True
                if state not in (raw_vlc.State.Ended, raw_vlc.State.Stopped):
                    # Any non-terminal state (including the moment it flips
                    # back to Playing) clears the debounce below immediately
                    # -- this check only fires on a Stopped/Ended reading
                    # that's still true after sitting there a while, not one
                    # single frame's glimpse of it.
                    self._dvd_end_state_since = None
                if (state in (raw_vlc.State.Ended, raw_vlc.State.Stopped)
                        and getattr(self, "_dvd_main_ever_played", False)
                        and not getattr(self, "dvd_resume_pending", False)):
                    # dvd_resume_pending guards against a false positive here:
                    # change_channel() calls vlc_engine.stop() the instant you
                    # LEAVE ch03 (so the outgoing audio doesn't bleed through
                    # the static transition), which puts VLC in the Stopped
                    # state well before the queued DVD-resume closure actually
                    # restarts playback (that's deferred until the transition
                    # shield expires). Without this guard, the very next frame
                    # after returning to ch03 sees that same stale Stopped
                    # state, "_dvd_main_ever_played" is already True from
                    # before you left, and this block mistook that for the
                    # movie having genuinely ended -- kicking you out to
                    # ROMLIST instead of resuming.
                    #
                    # SECOND FALSE POSITIVE (this is the "No Deal sends me
                    # back to the main menu" bug): picking a game-branching
                    # choice on a DVD game makes dvdnav jump domains -- off
                    # the VTS title, briefly through the VMGM root -- the
                    # same kind of transition that produces the case-reveal
                    # clock-jitter elsewhere. vlc-debug.log shows the video/
                    # audio decoders getting torn down and rebuilt
                    # ("killing decoder fourcc `mpgv'/`a52 '") during that
                    # jump, and get_state() can read back Stopped for a
                    # frame or two while that happens -- even though the
                    # disc is mid-navigation, not actually finished. Without
                    # a grace window this looked identical to the disc
                    # really ending, so a single Stopped frame during that
                    # domain jump was enough to kick you all the way out to
                    # ROMLIST. Require the state to still read Stopped/Ended
                    # after DVD_END_STATE_GRACE_MS of real time before
                    # believing it -- long enough to outlast a decoder
                    # rebuild, short enough not to delay a genuine end.
                    if self._dvd_end_state_since is None:
                        self._dvd_end_state_since = now
                    if now - self._dvd_end_state_since >= self.DVD_END_STATE_GRACE_MS:
                        # DIAGNOSTIC (added to catch the "No Deal sends me back
                        # to the main menu" bug in the act): this block used to
                        # bail out silently, so there was no way to confirm from
                        # app_log.txt alone that THIS watchdog -- as opposed to
                        # some other code path -- was what kicked the DVD back
                        # to ROMLIST. Log everything we have at the instant it
                        # fires, including the tail of vlc-debug.log, so the
                        # next repro nails down exactly what libVLC was doing
                        # (jitter rejection? decoder rebuild? something else?)
                        # in the moment this decided the disc had "ended."
                        try:
                            _title_at_bail = getattr(vlc, "_current_title", "?")
                        except Exception:
                            _title_at_bail = "?"
                        print(f"[DVD DEBUG] END-STATE WATCHDOG FIRED: state={state} "
                              f"title={_title_at_bail} grace_elapsed_ms={now - self._dvd_end_state_since} "
                              f"(threshold={self.DVD_END_STATE_GRACE_MS}ms) -- bailing out to ROMLIST.")
                        try:
                            _tail = vlc.get_recent_vlc_log()
                            if _tail:
                                print(f"[DVD DEBUG] vlc-debug.log tail at bailout:\n{_tail}")
                        except Exception as _e:
                            print(f"[DVD DEBUG] could not fetch vlc-debug.log tail: {_e}")
                        self.dvd_playback_active = False
                        self.dvd_splash_done     = False
                        self.dvd_paused_frame    = None
                        self.dvd_in_menu         = False
                        self.mode = "ROMLIST"
                        self._scan_roms("DVD")
                        return
            except Exception as e:
                _log_warn_throttled("dvd_main_end_check", "DVD main-video-ended state check failed this frame: %s", e)

        frame_drawn = False
        if getattr(self, 'dvd_awaiting_resume', False):
            # CLEAN RESUME BRIDGE (was playing when we left ch03): the stale
            # where-you-left frame was deliberately cleared on channel leave, and
            # we're waiting for the return-resume to restart VLC and decode its
            # first genuinely fresh DVD frame. Show plain black here -- do NOT blit
            # get_display_frame() (VLC can still be handing back frames from the
            # channel we just visited until play_file_segmented() swaps the media)
            # and do NOT blit dvd_paused_frame. This is what kills the "flash of the
            # old image after the static" on return. The flag is dropped by the
            # DVD-resume activation closure the instant it restarts playback, after
            # which the normal path below takes over and the first real frame shows.
            screen.fill((0, 0, 0), rect)
            frame_drawn = True
        if not frame_drawn and vlc is not None:
            try:
                if getattr(self, '_dvd_pending_pause', False):
                    try:
                        import vlc as _rvlc
                        if vlc.player.get_state() == _rvlc.State.Playing:
                            vlc.player.pause()
                            self._dvd_pending_pause = False
                    except Exception as e:
                        _log_warn_throttled("dvd_pending_pause", "Deferred DVD pause application failed this frame: %s", e)

                frame = vlc.get_display_frame()
                if frame:
                    surf   = pygame.image.frombuffer(frame, (vlc.width, vlc.height), "BGRA")
                    scaled = pygame.transform.smoothscale(surf, (w, h))
                    screen.blit(scaled, (x, y))
                    self.dvd_paused_frame = scaled.copy()
                    frame_drawn = True
            except Exception as e:
                _log_warn_throttled("dvd_frame_blit", "DVD frame grab/blit failed this frame: %s", e)
        if not frame_drawn and self.dvd_paused_frame is not None:
            screen.blit(self.dvd_paused_frame, (x, y))
            frame_drawn = True
        if not frame_drawn:
            screen.fill((0, 0, 0), rect)

        # NOTE: libVLC's dvdnav:// path burns the menu button highlight into
        # the decoded frame itself (native SPU compositing), so there's no
        # separate highlight rectangle to draw here.

        if not self.dvd_exit_confirm and not self.dvd_return_menu_confirm:
            _main_mod = sys.modules.get("__main__")
            _menu_open = bool(_main_mod and (
                _main_mod.app_state.get("show_menu", False) or
                _main_mod.app_state.get("show_splash", False) or
                _main_mod.app_state.get("show_quit_confirm", False)
            ))
            if not _menu_open:
                self._poll_dvd_hold()


# ==============================================================================
# PART 11b OF 13: BOTTOM INTERFACE STAMPS
# ==============================================================================

        try:
            import colorsys as _cs, sys as _sys2
            _main_mod2 = _sys2.modules.get("__main__")
            _hue = (_main_mod2.db.config.get("theme_ui_hue", 140)
                    if (_main_mod2 and hasattr(_main_mod2, "db") and _main_mod2.db)
                    else 140)
            _r, _g, _b = _cs.hsv_to_rgb(_hue / 360.0, 0.90, 1.00)
            ui_col = (int(_r * 255), int(_g * 255), int(_b * 255))
        except Exception:
            ui_col = getattr(self, 'ui_color', (0, 255, 128))

        if self.dvd_osd_msg and now < self.dvd_osd_until:
            icon_key = self._DVD_OSD_ICON_MAP.get(self.dvd_osd_msg)
            if icon_key:
                self._draw_dvd_osd_icon(screen, cx, y + h - 94, icon_key, ui_col, radius=46)
            else:
                f_osd   = _cached_font("Courier New", 44, bold=True)
                osd_lbl = f_osd.render(self.dvd_osd_msg, True, ui_col)
                screen.blit(osd_lbl, (cx - osd_lbl.get_width() // 2, y + h - 120))

        if self.dvd_exit_confirm:
            import colorsys
            main_module2  = sys.modules.get("__main__")
            menu_opacity  = 50
            theme_bg_hue  = 220
            theme_ui_hue  = 140
            if main_module2 and hasattr(main_module2, "db"):
                menu_opacity  = main_module2.db.config.get("menu_opacity",  50)
                theme_bg_hue  = main_module2.db.config.get("theme_bg_hue", 220)
                theme_ui_hue  = main_module2.db.config.get("theme_ui_hue", 140)

            box_w, box_h = 500, 200
            box_x = cx - box_w // 2
            box_y = y + h // 2 - box_h // 2

            r_bg, g_bg, b_bg = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.85, 0.18)
            r_ui, g_ui, b_ui = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
            rgb_bg     = (int(r_bg * 255), int(g_bg * 255), int(b_bg * 255))
            accent_col = (int(r_ui * 255), int(g_ui * 255), int(b_ui * 255))

            alpha_val = int((menu_opacity / 100.0) * 255)
            overlay   = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            overlay.fill((rgb_bg[0], rgb_bg[1], rgb_bg[2], alpha_val))
            screen.blit(overlay, (box_x, box_y))
            pygame.draw.rect(screen, accent_col, (box_x, box_y, box_w, box_h), 2, border_radius=4)

            f_title = _cached_font("Courier New", 22, bold=True)
            f_btn   = _cached_font("Courier New", 17, bold=True)
            f_hint2 = _cached_font("Courier New", 17, bold=True)

            lbl_q = f_title.render("EXIT DVD PLAYER?", True, (255, 220, 0))
            screen.blit(lbl_q, (cx - lbl_q.get_width() // 2, box_y + 25))


            box_radius      = 4
            focus_thickness = 2
            btn_w, btn_h    = 110, 36
            btn_y           = box_y + 85
            OFF_RED         = (220, 53, 69)
            NEUTRAL_BTN     = (45, 80, 130)

            yes_btn_x = cx - 100 - btn_w // 2
            no_btn_x  = cx + 100 - btn_w // 2

            pygame.draw.rect(screen, OFF_RED, (yes_btn_x, btn_y, btn_w, btn_h), border_radius=box_radius)
            if self.dvd_exit_selection == "Yes":
                pygame.draw.rect(screen, accent_col, (yes_btn_x, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
            lbl_yes = f_btn.render("YES", True, (255, 255, 255))
            screen.blit(lbl_yes, (yes_btn_x + btn_w // 2 - lbl_yes.get_width() // 2, btn_y + btn_h // 2 - lbl_yes.get_height() // 2))

            pygame.draw.rect(screen, NEUTRAL_BTN, (no_btn_x, btn_y, btn_w, btn_h), border_radius=box_radius)
            if self.dvd_exit_selection == "No":
                pygame.draw.rect(screen, accent_col, (no_btn_x, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
            lbl_no = f_btn.render("NO", True, (255, 255, 255))
            screen.blit(lbl_no, (no_btn_x + btn_w // 2 - lbl_no.get_width() // 2, btn_y + btn_h // 2 - lbl_no.get_height() // 2))

            lbl_h = f_hint2.render("A / D  SELECT     ENTER  CONFIRM", True, (160, 160, 160))
            screen.blit(lbl_h, (cx - lbl_h.get_width() // 2, box_y + box_h - 35))

        if self.dvd_return_menu_confirm:
            import colorsys
            main_module2  = sys.modules.get("__main__")
            menu_opacity  = 50
            theme_bg_hue  = 220
            theme_ui_hue  = 140
            if main_module2 and hasattr(main_module2, "db"):
                menu_opacity  = main_module2.db.config.get("menu_opacity",  50)
                theme_bg_hue  = main_module2.db.config.get("theme_bg_hue", 220)
                theme_ui_hue  = main_module2.db.config.get("theme_ui_hue", 140)

            box_w, box_h = 500, 200
            box_x = cx - box_w // 2
            box_y = y + h // 2 - box_h // 2

            r_bg, g_bg, b_bg = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.85, 0.18)
            r_ui, g_ui, b_ui = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
            rgb_bg     = (int(r_bg * 255), int(g_bg * 255), int(b_bg * 255))
            accent_col = (int(r_ui * 255), int(g_ui * 255), int(b_ui * 255))

            alpha_val = int((menu_opacity / 100.0) * 255)
            overlay   = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            overlay.fill((rgb_bg[0], rgb_bg[1], rgb_bg[2], alpha_val))
            screen.blit(overlay, (box_x, box_y))
            pygame.draw.rect(screen, accent_col, (box_x, box_y, box_w, box_h), 2, border_radius=4)

            f_title = _cached_font("Courier New", 22, bold=True)
            f_btn   = _cached_font("Courier New", 17, bold=True)
            f_hint2 = _cached_font("Courier New", 17, bold=True)

            lbl_q = f_title.render("RETURN TO MAIN MENU?", True, (255, 220, 0))
            screen.blit(lbl_q, (cx - lbl_q.get_width() // 2, box_y + 25))

            box_radius      = 4
            focus_thickness = 2
            btn_w, btn_h    = 110, 36
            btn_y           = box_y + 85
            CONFIRM_BLUE    = (45, 110, 200)
            NEUTRAL_BTN     = (45, 80, 130)

            yes_btn_x = cx - 100 - btn_w // 2
            no_btn_x  = cx + 100 - btn_w // 2

            pygame.draw.rect(screen, CONFIRM_BLUE, (yes_btn_x, btn_y, btn_w, btn_h), border_radius=box_radius)
            if self.dvd_return_menu_selection == "Yes":
                pygame.draw.rect(screen, accent_col, (yes_btn_x, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
            lbl_yes = f_btn.render("YES", True, (255, 255, 255))
            screen.blit(lbl_yes, (yes_btn_x + btn_w // 2 - lbl_yes.get_width() // 2, btn_y + btn_h // 2 - lbl_yes.get_height() // 2))

            pygame.draw.rect(screen, NEUTRAL_BTN, (no_btn_x, btn_y, btn_w, btn_h), border_radius=box_radius)
            if self.dvd_return_menu_selection == "No":
                pygame.draw.rect(screen, accent_col, (no_btn_x, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
            lbl_no = f_btn.render("NO", True, (255, 255, 255))
            screen.blit(lbl_no, (no_btn_x + btn_w // 2 - lbl_no.get_width() // 2, btn_y + btn_h // 2 - lbl_no.get_height() // 2))

            lbl_h = f_hint2.render("A / D  SELECT     ENTER  CONFIRM", True, (160, 160, 160))
            screen.blit(lbl_h, (cx - lbl_h.get_width() // 2, box_y + box_h - 35))

    # ==========================================================================
    # LIBRETRO CORE — methods
    # ==========================================================================

    def _has_libretro_core(self, console):
        emu_dir = os.path.join(self.cores_dir, console)
        if not os.path.isdir(emu_dir):
            return None
        for fname in os.listdir(emu_dir):
            fl = fname.lower()
            if fl.endswith("_libretro.dll") or fl.endswith("libretro.dll"):
                return os.path.join(emu_dir, fname)
        for fname in os.listdir(emu_dir):
            if fname.lower().endswith(".dll"):
                return os.path.join(emu_dir, fname)
        return None

    def _build_default_libretro_xinput_map(self, console):
        """
        BUG FIX (was the cause of the GG/NGP "both buttons do the same thing"
        report): this used to write into ONE flat self._libretro_xinput_map
        dict keyed only by btn_name ("A", "B", "Start"...) shared across
        EVERY console. Almost every console reuses "A"/"B"/"Start"/"Select"
        as button names, so whichever console loaded first "claimed" those
        keys for the whole app session — every other console with an A/B
        (GB, GBC, GBA, NES, SNES, NGP) silently inherited that first
        console's physical-button binding instead of its own, and a
        half-finished/confused remap session on ANY of them (see the
        capture-loop comment below) could leave two of those shared keys
        pointing at the identical xi_bit — which then reappeared on every
        other console using the same names, including ones you never
        touched. GG's "1"/"2" don't collide with anything else, but they
        still went through the same broken flat-dict code path.

        Now scoped per console (self._libretro_xinput_map[console]), and
        loads any previously-saved binding for THIS console before falling
        back to its own defaults — previously a custom binding was never
        written to disk at all, so it also silently reset on every restart.
        """
        console_map = self._libretro_xinput_map.setdefault(console, {})
        saved = self._load_libretro_xinput_map(console)
        for btn_name, xi_bit in saved.items():
            console_map.setdefault(btn_name, xi_bit)
        cdef = _get_console_def(console)
        xinput_map = cdef.get("xinput_map", {})
        for xi_bit, btn_name in xinput_map.items():
            if btn_name not in console_map:
                console_map[btn_name] = xi_bit

    def _load_libretro_xinput_map(self, console):
        """Load this console's saved physical-button (gamepad) bindings from
        emulator_config.json, stored under '<CONSOLE>_xinput_map' as
        {btn_name: xi_label_string} — mirrors _load_controller_mapping's
        keyboard-VK persistence, which this previously had no equivalent of
        at all (bindings only ever lived in memory for the current run)."""
        try:
            cfg_path = os.path.join(
                os.path.dirname(os.path.abspath(sys.argv[0])),
                "emulator_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                saved = data.get(f"{console}_xinput_map", {})
                out = {}
                for btn_name, xi_label in saved.items():
                    xi_bit = _XI_LABEL_TO_BIT.get(xi_label)
                    if xi_bit is not None:
                        out[btn_name] = xi_bit
                return out
        except Exception as e:
            print(f"[GAMEDECK MAPPING] Gamepad-map load failed for {console} (using defaults): {e}")
        return {}

    def _save_libretro_xinput_map(self, console):
        """Persist console's current physical-button bindings so a remap
        survives an app restart (previously never saved at all — see
        _build_default_libretro_xinput_map for the full story)."""
        try:
            cfg_path = os.path.join(
                os.path.dirname(os.path.abspath(sys.argv[0])),
                "emulator_config.json")
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            console_map = self._libretro_xinput_map.get(console, {})
            data[f"{console}_xinput_map"] = {
                btn_name: _XI_LABELS.get(xi_bit, "")
                for btn_name, xi_bit in console_map.items()
                if xi_bit in _XI_LABELS
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"[GAMEDECK MAPPING] Saved {console} gamepad map: {data[f'{console}_xinput_map']}")
        except Exception as e:
            print(f"[GAMEDECK MAPPING] Gamepad-map save failed for {console}: {e}")

    # ------------------------------------------------------------------
    # Zip-archive ROM extraction — libretro cores (mGBA included) expect a
    # real ROM file on disk / real ROM bytes, not a zip archive's raw bytes.
    # ------------------------------------------------------------------
    def _extract_rom_from_zip(self, console, zip_path):
        """If zip_path is a .zip archive, extract the matching ROM entry to a
        per-console cache folder and return its path. Picks the entry whose
        extension is in this console's ROM_EXTENSIONS (excluding .zip/.7z
        themselves), preferring the largest such file if there are several
        (e.g. a ROM alongside a small readme/manual). Returns the original
        path unchanged if it isn't a .zip, or on any extraction failure."""
        if not zip_path.lower().endswith(".zip"):
            return zip_path
        try:
            real_exts = [e for e in ROM_EXTENSIONS.get(console, []) if e not in (".zip", ".7z")]
            with zipfile.ZipFile(zip_path, "r") as zf:
                candidates = [
                    info for info in zf.infolist()
                    if not info.is_dir()
                    and os.path.splitext(info.filename)[1].lower() in real_exts
                ]
                if not candidates:
                    print(f"[LIBRETRO] No {console} ROM found inside zip: {os.path.basename(zip_path)}")
                    return zip_path
                # Largest matching file is almost always the actual ROM.
                chosen = max(candidates, key=lambda info: info.file_size)

                cache_dir = os.path.join(self.base_dir, "main", "roms", console, ".extracted_cache")
                os.makedirs(cache_dir, exist_ok=True)
                # Stable, collision-safe name: <zip-stem>__<entry-basename>
                zip_stem    = os.path.splitext(os.path.basename(zip_path))[0]
                entry_base  = os.path.basename(chosen.filename)
                out_path    = os.path.join(cache_dir, f"{zip_stem}__{entry_base}")

                # Skip re-extracting if we already have a same-size copy cached.
                if not (os.path.exists(out_path) and os.path.getsize(out_path) == chosen.file_size):
                    with zf.open(chosen, "r") as src, open(out_path, "wb") as dst:
                        dst.write(src.read())
                    print(f"[LIBRETRO] Extracted {entry_base} from {os.path.basename(zip_path)}")
                return out_path
        except Exception as e:
            print(f"[LIBRETRO] Zip extraction failed for {zip_path}: {e}")
            return zip_path

    def _launch_libretro(self, console, rom_path):
        self.emulator_loading  = True
        self.emulator_load_pct = 0
        self.emulator_load_msg = f"Loading {console}..."
        self.active_console    = console
        self.mode               = "GAME"
        self.is_frozen          = False
        # If rom_path points at a .zip, extract the actual ROM file first —
        # libretro cores need a real ROM file/bytes, not a zip archive's raw
        # compressed bytes. Save-state slots, the loading screen, and the
        # core itself all use the EXTRACTED path from here on; only the
        # display name (already captured above the print line) still shows
        # the original .zip filename to the user.
        _display_name   = os.path.basename(rom_path)
        rom_path         = self._extract_rom_from_zip(console, rom_path)
        self._libretro_console    = console
        self._libretro_rom_path   = rom_path
        self._libretro_loading    = True
        self._libretro_load_start = pygame.time.get_ticks()
        if _LIBRETRO_AVAILABLE:
            FRAME_PERF.set_console(console)
        print(f"[LIBRETRO] Launching {console}: {_display_name}")
        if (self._libretro_core is not None
                and self._prev_libretro_console
                and self._prev_libretro_console != console):
            if self._libretro_auto_save:
                self._libretro_core.save_state(slot=self._libretro_active_profile)
            self._libretro_core.unload()
            self._libretro_core = None
        if self._libretro_core is None or not self._libretro_core.is_loaded:
            core = build_core_for_console(console, self.base_dir)
            if core is None:
                print(f"[LIBRETRO] Could not build core for {console}")
                self.emulator_loading = False; self._libretro_loading = False
                self.mode = "ROMLIST"; return
            self._libretro_core = core
        else:
            # Same console, and a game is already loaded on this core — e.g.
            # restarting the same ROM, or picking a different one without
            # leaving the console. retro_load_game() can't be called again
            # on top of an already-loaded game, so auto-save (if enabled)
            # and cleanly unload it first before loading the new one below.
            core = self._libretro_core
            if self._libretro_auto_save:
                core.save_state(slot=self._libretro_active_profile)
            core.unload_game()
        try:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            if _main and hasattr(_main, "db") and _main.db:
                # Sync the active profile settings for this console before launch
                self.sync_profile_from_db(console, _main.db.config)
                core.set_volume(_main.db.config.get("global_volume", 70))
                # Mute during load screen — unsilenced once loading clears
                core.set_muted(True)
        except Exception as e:
            log.warning("Failed to sync profile/volume settings before libretro launch: %s", e)
        if hasattr(core, "set_variable"):
            # mGBA's own SGB-style border (GB/GBC only — it doesn't exist for
            # GBA at all) only reconfigures at load time, which is what made
            # an on/off toggle require a restart. We draw the border
            # ourselves instead (see _load_border_surface / render_libretro),
            # so always tell the core OFF — harmless no-op on cores/consoles
            # that don't have this variable — so it hands back the plain
            # native frame and never draws its own border underneath ours.
            core.set_variable("mgba_sgb_borders", "OFF")
        if border_available_for_console(console, self._get_aspect_ratio()):
            self._load_border_surface(console)
        else:
            self._libretro_border_surf      = None
            self._libretro_border_rect_frac = None
        if not core.load_rom(rom_path):
            print("[LIBRETRO] ROM load failed — returning to list")
            self.emulator_loading = False; self._libretro_loading = False
            self.mode = "ROMLIST"; return
        self._prev_libretro_console = console
        self._build_default_libretro_xinput_map(console)
        slot = self._libretro_active_profile
        if self._libretro_auto_load and core.has_state(slot=slot):
            core.load_state(slot=slot)
            print(f"[LIBRETRO] Auto-loaded save state slot {slot} (profile {slot})")

    def _libretro_tick_loading(self):
        if not self._libretro_loading:
            return False
        now     = pygame.time.get_ticks()
        elapsed = now - self._libretro_load_start
        t = min(1.0, elapsed / max(1, self._libretro_load_min_ms))
        self.emulator_load_pct = int(t * (90 if elapsed < self._libretro_load_min_ms else 100))
        core = self._libretro_core
        if core and core.is_loaded:
            try: core.run_frame()
            except Exception as e:
                _log_warn_throttled("libretro_tick_run_frame", "core.run_frame() during loading-splash tick failed: %s", e)
        # Wait for a genuinely visible frame, not just "the core produced
        # pixels" — a booting core (e.g. PSX during its BIOS sequence) calls
        # the video callback with real, legitimate black frames for as long
        # as boot takes. Gating on get_surface() alone meant the splash
        # dropped away immediately and dumped the player straight into
        # whatever black frames the core was still churning out — this is
        # the actual "long black screen" that was being reported. Now the
        # loading animation stays up until content is actually on screen.
        first_frame = core is not None and core.has_visible_frame
        min_done    = elapsed >= self._libretro_load_min_ms
        # Safety valve: don't hold the splash forever if a game just has a
        # legitimately all-black opening (rare, but exists) — cap the wait.
        timed_out   = elapsed >= 20000
        if (first_frame or timed_out) and min_done:
            self.emulator_load_pct = 100
            self._libretro_loading = False
            self.emulator_loading  = False
            # Restore correct mute state now that the load screen is done
            try:
                import sys as _sys
                _main = _sys.modules.get("__main__")
                if _main and hasattr(_main, "db") and _main.db:
                    _muted = _main.db.config.get("is_muted", False)
                    core.set_muted(_muted)
            except Exception as e:
                log.warning("Failed to restore mute state after libretro splash: %s", e)
            print(f"[LIBRETRO] Splash done after {elapsed}ms")
            return False
        return True

    def _poll_libretro_input(self):
        """
        Read XInput and translate to libretro button IDs via _libretro_xinput_map.
        Hotkeys:
          LB + RB + Select  →  save state slot 0
          LT + RT + Select  →  load state slot 0
        2-second debounce prevents accidental repeat fires.
        """
        core = self._libretro_core
        if core is None or not core.is_loaded or core.is_frozen:
            return
        # Block game inputs while the load screen is still showing
        if getattr(self, "_libretro_loading", False):
            core._input.clear()
            return
        try:
            xi = ctypes.windll.LoadLibrary("xinput1_4")
        except Exception:
            try:
                xi = ctypes.windll.LoadLibrary("xinput1_3")
            except Exception:
                return

        class _GP(ctypes.Structure):
            _fields_ = [("wButtons", ctypes.c_ushort),
                        ("bLT",      ctypes.c_ubyte),
                        ("bRT",      ctypes.c_ubyte),
                        ("sLX",      ctypes.c_short),
                        ("sLY",      ctypes.c_short),
                        ("sRX",      ctypes.c_short),
                        ("sRY",      ctypes.c_short)]
        class _XS(ctypes.Structure):
            _fields_ = [("dwPkt", ctypes.c_ulong), ("Gamepad", _GP)]

        state = _XS()
        if xi.XInputGetState(0, ctypes.byref(state)) != 0:
            core._input.clear()
            return

        gp   = state.Gamepad
        btns = gp.wButtons

        # SCREENSAVER: stamp last real gameplay input so the ch03 screensaver's
        # idle timer (which only listens for pygame KEYDOWN/JOY* events) can
        # see activity here too. Real gameplay input never reaches the pygame
        # event queue -- it's read directly via XInput above -- so without
        # this the screensaver has no way to know the player is still active
        # mid-game and can fire the burn-in saver right on top of gameplay.
        _SS_STICK_ACTIVITY_DZ = 6000
        if (btns or gp.bLT or gp.bRT
                or abs(gp.sLX) > _SS_STICK_ACTIVITY_DZ or abs(gp.sLY) > _SS_STICK_ACTIVITY_DZ
                or abs(gp.sRX) > _SS_STICK_ACTIVITY_DZ or abs(gp.sRY) > _SS_STICK_ACTIVITY_DZ):
            self.last_gameplay_input_ticks = pygame.time.get_ticks()

        _XI_LB   = 0x0100
        _XI_RB   = 0x0200
        _XI_BACK = 0x0020
        _TRIG_THRESH = 128
        _COOLDOWN_MS = 2000

        # If the player just closed the exit-confirm box with Start+Select
        # (or it was open and they cancelled while still holding it), don't
        # let that same still-held press fall through into the game as a
        # real Start/Select input. Keep masking both out until they're
        # fully released, then stop masking.
        if getattr(self, "_libretro_suppress_start_select", False):
            if btns & (_XI_START | _XI_BACK):
                btns &= ~(_XI_START | _XI_BACK)
            else:
                self._libretro_suppress_start_select = False

        now = pygame.time.get_ticks()
        if now >= self._libretro_hotkey_cooldown:
            # LB + RB + Select → SAVE  (L1 + R1 + Select)
            if (btns & _XI_LB) and (btns & _XI_RB) and (btns & _XI_BACK):
                ok = core.save_state(slot=self._libretro_active_profile)
                self._libretro_osd_msg   = f"STATE SAVED (P{self._libretro_active_profile})" if ok else "SAVE FAILED"
                self._libretro_osd_until = now + 2000
                self._libretro_hotkey_cooldown = now + _COOLDOWN_MS
                print(f"[LIBRETRO] Save hotkey: {self._libretro_osd_msg}")
            # LT + RT + Select → LOAD  (L2 + R2 + Select)
            elif (gp.bLT >= _TRIG_THRESH and gp.bRT >= _TRIG_THRESH
                  and (btns & _XI_BACK)):
                if core.has_state(slot=self._libretro_active_profile):
                    ok = core.load_state(slot=self._libretro_active_profile)
                    self._libretro_osd_msg = f"STATE LOADED (P{self._libretro_active_profile})" if ok else "LOAD FAILED"
                else:
                    self._libretro_osd_msg = "NO SAVE FOUND"
                self._libretro_osd_until = now + 2000
                self._libretro_hotkey_cooldown = now + _COOLDOWN_MS
                print(f"[LIBRETRO] Load hotkey: {self._libretro_osd_msg}")

        # Build button state from _libretro_xinput_map (now console-scoped —
        # see _build_default_libretro_xinput_map for why it has to be)
        _active_console_for_input = self.active_console or self._libretro_console
        console_xinput_map = self._libretro_xinput_map.get(_active_console_for_input, {})
        xi_to_names = {}
        for btn_name, xi_bit in console_xinput_map.items():
            xi_to_names.setdefault(xi_bit, []).append(btn_name)

        libretro_state = {}
        for xi_bit, btn_names in xi_to_names.items():
            pressed = bool(btns & xi_bit)
            for btn_name in btn_names:
                lr_id = lr_id_for(_active_console_for_input, btn_name)
                if lr_id is not None:
                    libretro_state[lr_id] = libretro_state.get(lr_id, False) or pressed

        if self.ctrl_mapping.get("stick_as_dpad", False):
            DZ = 8000
            if gp.sLY >  DZ: libretro_state[RETRO_DEVICE_ID_JOYPAD_UP]    = True
            if gp.sLY < -DZ: libretro_state[RETRO_DEVICE_ID_JOYPAD_DOWN]  = True
            if gp.sLX < -DZ: libretro_state[RETRO_DEVICE_ID_JOYPAD_LEFT]  = True
            if gp.sLX >  DZ: libretro_state[RETRO_DEVICE_ID_JOYPAD_RIGHT] = True

        core._input.set_buttons(libretro_state)

        # ── N64: real analog stick support ────────────────────────────────
        # N64 cores (Mupen64Plus-Next, ParaLLEl) read the actual control
        # stick AND the C-buttons through RETRO_DEVICE_ANALOG, not the
        # digital joypad bits — see _cb_input_state. Left stick drives the
        # N64 control stick directly; right stick drives the C-buttons in
        # the same "C-buttons on right stick" scheme RetroArch itself uses
        # by default, so no extra config is needed inside the core.
        # XInput's Y axis is inverted relative to retropad's analog Y
        # (XInput: up = positive; retropad: up = negative), hence the
        # negation below.
        if (self.active_console or self._libretro_console) == "N64":
            core._input.set_analog(RETRO_DEVICE_INDEX_ANALOG_LEFT,  RETRO_DEVICE_ID_ANALOG_X, gp.sLX)
            core._input.set_analog(RETRO_DEVICE_INDEX_ANALOG_LEFT,  RETRO_DEVICE_ID_ANALOG_Y, -gp.sLY)
            core._input.set_analog(RETRO_DEVICE_INDEX_ANALOG_RIGHT, RETRO_DEVICE_ID_ANALOG_X, gp.sRX)
            core._input.set_analog(RETRO_DEVICE_INDEX_ANALOG_RIGHT, RETRO_DEVICE_ID_ANALOG_Y, -gp.sRY)

    def render_libretro(self, screen, dest_rect, crt_cfg=None, overlay_open=False):
        """Run one core frame and blit to dest_rect. Draws OSD on top.

        overlay_open: True while the TV-shell's own settings menu, controls
        splash, or quit/restart confirm is on screen. That UI already reads
        ESC itself for back/close navigation, so while it's up we must NOT
        also poll raw ESC here for the in-game exit-confirm toggle — doing
        so stole every one of those ESC presses, popping the "EXIT GAME?"
        box open/closed on top of the menu and forcing spurious
        freeze/unfreeze cycles underneath it."""
        core = self._libretro_core
        if core is None or not core.is_loaded:
            return False

        # Shrink the destination rect (centered) for handhelds with a non-FULL
        # screen-size setting. Done once here so every blit path below
        # (exit-confirm, frozen/menu-open, normal run-frame) plus the OSD stay
        # consistent — they all just use dest_rect as given.
        factor = SCREEN_SIZE_FACTORS.get(self._libretro_screen_size, 1.0)
        if factor < 1.0:
            dx, dy, dw, dh = dest_rect
            new_w = int(dw * factor)
            new_h = int(dh * factor)
            dest_rect = (dx + (dw - new_w) // 2, dy + (dh - new_h) // 2, new_w, new_h)

        if overlay_open:
            # A TV-shell overlay owns input right now. If the exit-confirm
            # box happened to be open already, drop it without calling
            # _close_game_exit_confirm() — that forces an unfreeze, and
            # while an overlay is open, freeze state belongs to it.
            if self.game_exit_confirm:
                self.game_exit_confirm = False
            # Keep the ESC held-state tracker current (see _poll_game_exit_toggle's
            # sync_only docs) without acting on it — the overlay owns ESC for its
            # own back/close navigation while it's up.
            self._poll_game_exit_toggle(sync_only=True)
            self._prev_overlay_open = True
            if not core.is_frozen:
                core.freeze()
            surf = core.reprocess_current_frame(crt_cfg=crt_cfg)
            if surf: self._blit_libretro_frame(screen, surf, dest_rect)
            return True

        # ESC is LOCKED for the in-game exit-confirm toggle on the exact frame
        # the TV-shell overlay closes. app_state["show_menu"] flips to False
        # in the SAME frame's event handling as the ESC keydown that closed
        # it, so by the time we get here overlay_open is already False and
        # the key is (correctly) still reading as held — that's the very
        # press that just closed the menu, not a new one aimed at this
        # exit-confirm toggle. Eat it with a sync-only poll instead of a
        # consuming one so it can never pop the "EXIT GAME?" box open right
        # on top of the menu closing; only a genuinely fresh ESC press made
        # after the menu is gone will open it from here on.
        if self._prev_overlay_open:
            self._prev_overlay_open = False
            self._poll_game_exit_toggle(sync_only=True)
        # ESC and the Start+Select combo are a TOGGLE for the exit-confirm
        # prompt — same key opens it and closes it again, like a light
        # switch — rather than an open-only trigger. Checked first, every
        # frame, regardless of which branch below ends up running, so the
        # edge-tracking stays correct across the open/closed transition.
        elif self._poll_game_exit_toggle():
            if self.game_exit_confirm:
                self._close_game_exit_confirm()
            else:
                self._open_game_exit_confirm()

        # Exit-confirm overlay open — render the frozen frame underneath
        # (same look as the menu-frozen branch below) and draw the "EXIT
        # GAME?" prompt on top instead of running/advancing the game.
        if self.game_exit_confirm:
            if not core.is_frozen:
                core.freeze()
            surf = core.reprocess_current_frame(crt_cfg=crt_cfg)
            if surf: self._blit_libretro_frame(screen, surf, dest_rect)
            self._poll_game_exit_confirm_input()
            self._render_game_exit_confirm(screen, dest_rect)
            return True

        if core.is_frozen:
            # Frozen (menu/overlay open) — don't advance emulation, but DO
            # re-apply CRT effects (brightness/contrast/hue/color/sharpness)
            # to the current frame each call so slider changes made while the
            # menu is open show up immediately, without needing to unfreeze.
            surf = core.reprocess_current_frame(crt_cfg=crt_cfg)
            if surf: self._blit_libretro_frame(screen, surf, dest_rect)
            return True
        self._poll_libretro_input()
        surf = core.run_frame(crt_cfg=crt_cfg)
        if surf:
            self._blit_libretro_frame(screen, surf, dest_rect)
        now = pygame.time.get_ticks()
        if self._libretro_osd_msg and now < self._libretro_osd_until:
            _libretro_draw_osd(screen, dest_rect, self._libretro_osd_msg,
                               self._libretro_osd_until - now, 2000)
        elif now >= self._libretro_osd_until:
            self._libretro_osd_msg = ""
        return True

    def _poll_game_exit_toggle(self, sync_only=False):
        """Returns True on the rising edge of ESC or the Start+Select combo.
        Used to OPEN the exit-confirm prompt when it's closed and CLOSE it
        again when it's already open — the same key/combo toggles it both
        ways, so holding it down doesn't repeatedly fire and it doesn't
        immediately reopen the instant it's closed.
        Reads fresh keyboard/XInput state directly (not core._input, which
        stops updating once the core is frozen for the confirm box).

        sync_only=True updates the held-state tracker (so it doesn't go
        stale) WITHOUT reporting a rising edge to the caller. Used while a
        TV-shell overlay (main menu/splash/quit-confirm) owns input: if we
        simply stopped calling this at all during that time, the tracker
        would freeze at whatever it last saw — so if the user closes that
        overlay with ESC while still physically holding the key down, the
        very next non-overlay frame would see esc_held=True next to a
        stale prev=False and misread the still-held key as a brand-new
        press, popping the exit-confirm box open right on top of the menu
        closing. Keeping the tracker in sync every frame (even when we
        aren't allowed to act on it) means a key that was already down
        when the overlay closed is correctly seen as "still held", not
        "just pressed"."""
        esc_held = pygame.key.get_pressed()[pygame.K_ESCAPE]
        ss_held  = False
        try:
            try:
                xi = ctypes.windll.LoadLibrary("xinput1_4")
            except Exception:
                xi = ctypes.windll.LoadLibrary("xinput1_3")

            class _GP(ctypes.Structure):
                _fields_ = [("wButtons", ctypes.c_ushort), ("bLT", ctypes.c_ubyte),
                            ("bRT", ctypes.c_ubyte), ("sLX", ctypes.c_short),
                            ("sLY", ctypes.c_short), ("sRX", ctypes.c_short), ("sRY", ctypes.c_short)]
            class _XS(ctypes.Structure):
                _fields_ = [("dwPkt", ctypes.c_ulong), ("Gamepad", _GP)]

            state = _XS()
            if xi.XInputGetState(0, ctypes.byref(state)) == 0:
                btns = state.Gamepad.wButtons
                ss_held = bool(btns & _XI_START) and bool(btns & _XI_BACK)
        except Exception as e:
            _log_warn_throttled("game_exit_toggle_xinput", "Per-frame XInput read for exit-toggle failed: %s", e)

        held = esc_held or ss_held
        prev = getattr(self, "_game_exit_toggle_held", False)
        self._game_exit_toggle_held = held
        if sync_only:
            return False
        return held and not prev

    def _open_game_exit_confirm(self):
        """Open the 'EXIT GAME?' prompt instead of quitting immediately —
        triggered by the Start+Select controller combo or the ESC key."""
        if self.game_exit_confirm:
            return
        print("[LIBRETRO] Exit requested — opening confirm prompt.")
        self.game_exit_confirm   = True
        self.game_exit_selection = "No"
        self._game_exit_prev_buttons = 0
        core = self._libretro_core
        if core and core.is_loaded and not core.is_frozen:
            core.freeze()

    def _close_game_exit_confirm(self):
        """Cancel out of the exit-confirm prompt and resume the game."""
        self.game_exit_confirm = False
        # Start/Select are usually still physically held at this exact
        # instant (that's how the player closed the box). Without this,
        # _poll_libretro_input reads that same held Start/Select on the
        # very next frame and feeds it straight into the game as a real
        # button press. Mask both out of the game's input until the player
        # actually releases them.
        self._libretro_suppress_start_select = True
        core = self._libretro_core
        if core and core.is_loaded:
            core.unfreeze()

    def _poll_game_exit_confirm_input(self):
        """Dedicated input poll while the exit-confirm box is open: D-pad/
        stick left-right (or A/D keys) move the Yes/No selection, A / Enter
        confirms, the controller's B button cancels. (ESC and Start+Select
        are handled separately by _poll_game_exit_toggle so they can close
        the box the same way they opened it.) Runs instead of
        _poll_libretro_input so game buttons can't leak through to the
        (frozen) game while this prompt is up. Keyboard and XInput are
        merged into one bitmask so either input method works whether or
        not a controller is plugged in."""
        # ── Keyboard ─────────────────────────────────────────────────────
        keys = pygame.key.get_pressed()
        left  = keys[pygame.K_a] or keys[pygame.K_LEFT]
        right = keys[pygame.K_d] or keys[pygame.K_RIGHT]
        a_btn = keys[pygame.K_RETURN]
        b_btn = False

        # ── XInput (optional — keyboard above already works without it) ──
        try:
            try:
                xi = ctypes.windll.LoadLibrary("xinput1_4")
            except Exception:
                xi = ctypes.windll.LoadLibrary("xinput1_3")

            class _GP(ctypes.Structure):
                _fields_ = [("wButtons", ctypes.c_ushort), ("bLT", ctypes.c_ubyte),
                            ("bRT", ctypes.c_ubyte), ("sLX", ctypes.c_short),
                            ("sLY", ctypes.c_short), ("sRX", ctypes.c_short), ("sRY", ctypes.c_short)]
            class _XS(ctypes.Structure):
                _fields_ = [("dwPkt", ctypes.c_ulong), ("Gamepad", _GP)]

            state = _XS()
            if xi.XInputGetState(0, ctypes.byref(state)) == 0:
                gp   = state.Gamepad
                btns = gp.wButtons
                DZ   = 8000
                left  = left  or bool(btns & _XI_LEFT)  or gp.sLX < -DZ
                right = right or bool(btns & _XI_RIGHT) or gp.sLX >  DZ
                a_btn = a_btn or bool(btns & _XI_A)
                b_btn = bool(btns & _XI_B)
        except Exception as e:
            _log_warn_throttled("game_exit_confirm_xinput", "Per-frame XInput read for exit-confirm input failed: %s", e)

        cur  = (1 if left else 0) | (2 if right else 0) | (4 if a_btn else 0) | (8 if b_btn else 0)
        prev = self._game_exit_prev_buttons
        just = cur & ~prev
        self._game_exit_prev_buttons = cur

        if just & 1: self.game_exit_selection = "Yes"
        if just & 2: self.game_exit_selection = "No"
        if just & 4:
            if self.game_exit_selection == "Yes":
                self.game_exit_confirm = False
                self._libretro_quit_to_romlist(self._libretro_core)
            else:
                self._close_game_exit_confirm()
        if just & 8:
            self._close_game_exit_confirm()

    def _render_game_exit_confirm(self, screen, dest_rect):
        """Draws the 'EXIT GAME?' Yes/No box — visual twin of the DVD
        player's exit-confirm overlay, scaled to the game's dest_rect."""
        import colorsys
        x, y, w, h = dest_rect
        cx = x + w // 2

        menu_opacity  = 50
        theme_bg_hue  = 220
        theme_ui_hue  = 140
        main_module2  = sys.modules.get("__main__")
        if main_module2 and hasattr(main_module2, "db"):
            menu_opacity = main_module2.db.config.get("menu_opacity",  50)
            theme_bg_hue = main_module2.db.config.get("theme_bg_hue", 220)
            theme_ui_hue = main_module2.db.config.get("theme_ui_hue", 140)

        box_w, box_h = 500, 200
        box_x = cx - box_w // 2
        box_y = y + h // 2 - box_h // 2

        r_bg, g_bg, b_bg = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.85, 0.18)
        r_ui, g_ui, b_ui = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
        rgb_bg     = (int(r_bg * 255), int(g_bg * 255), int(b_bg * 255))
        accent_col = (int(r_ui * 255), int(g_ui * 255), int(b_ui * 255))

        alpha_val = int((menu_opacity / 100.0) * 255)
        overlay   = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
        overlay.fill((rgb_bg[0], rgb_bg[1], rgb_bg[2], alpha_val))
        screen.blit(overlay, (box_x, box_y))
        pygame.draw.rect(screen, accent_col, (box_x, box_y, box_w, box_h), 2, border_radius=4)

        f_title = _cached_font("Courier New", 22, bold=True)
        f_btn   = _cached_font("Courier New", 17, bold=True)
        f_hint2 = _cached_font("Courier New", 17, bold=True)

        lbl_q = f_title.render("EXIT GAME?", True, (255, 220, 0))
        screen.blit(lbl_q, (cx - lbl_q.get_width() // 2, box_y + 25))


        box_radius      = 4
        focus_thickness = 2
        btn_w, btn_h    = 110, 36
        btn_y           = box_y + 85
        OFF_RED         = (220, 53, 69)
        NEUTRAL_BTN     = (45, 80, 130)

        yes_btn_x = cx - 100 - btn_w // 2
        no_btn_x  = cx + 100 - btn_w // 2

        pygame.draw.rect(screen, OFF_RED, (yes_btn_x, btn_y, btn_w, btn_h), border_radius=box_radius)
        if self.game_exit_selection == "Yes":
            pygame.draw.rect(screen, accent_col, (yes_btn_x, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
        lbl_yes = f_btn.render("YES", True, (255, 255, 255))
        screen.blit(lbl_yes, (yes_btn_x + btn_w // 2 - lbl_yes.get_width() // 2, btn_y + btn_h // 2 - lbl_yes.get_height() // 2))

        pygame.draw.rect(screen, NEUTRAL_BTN, (no_btn_x, btn_y, btn_w, btn_h), border_radius=box_radius)
        if self.game_exit_selection == "No":
            pygame.draw.rect(screen, accent_col, (no_btn_x, btn_y, btn_w, btn_h), focus_thickness, border_radius=box_radius)
        lbl_no = f_btn.render("NO", True, (255, 255, 255))
        screen.blit(lbl_no, (no_btn_x + btn_w // 2 - lbl_no.get_width() // 2, btn_y + btn_h // 2 - lbl_no.get_height() // 2))

        lbl_h = f_hint2.render("\u2190/\u2192 OR A/D  SELECT     A  CONFIRM     B  CANCEL", True, (160, 160, 160))
        screen.blit(lbl_h, (cx - lbl_h.get_width() // 2, box_y + box_h - 35))

    def _libretro_quit_to_romlist(self, core):
        print("[LIBRETRO] Start+Select: quitting to ROM list")
        if self._libretro_auto_save:
            core.save_state(slot=self._libretro_active_profile)
        # Fully unload the core so the render loop doesn't re-enter loading.
        try:
            core.unload()
        except Exception as e:
            log.warning("core.unload() failed while quitting to ROM list: %s", e)
        self._libretro_core    = None
        self._libretro_loading = False
        self.emulator_loading  = False
        self.mode              = "ROMLIST"
        # Flush any stale KEYDOWN events (e.g. from held Start+Select) so the
        # ROM list doesn't receive a spurious K_RETURN on its first frame.
        import pygame as _pg
        _pg.event.clear(_pg.KEYDOWN)
        if self.active_console:
            self._scan_roms(self.active_console)
        self.rom_index = 0; self.rom_scroll = 0

    def freeze_libretro(self):
        core = self._libretro_core
        if core and core.is_loaded:
            if self._libretro_auto_save:
                core.save_state(slot=self._libretro_active_profile)
            # SRAM/battery save persistence is unconditional -- it's not the
            # optional quick-save-state feature auto_save gates, it's the
            # game's own normal in-game save data, so it's flushed every time
            # the game gets backgrounded (not just on a clean quit) as a
            # safety net against a crash/force-close losing progress.
            core.save_sram()
            core.freeze()

    def unfreeze_libretro(self, muted=False):
        core = self._libretro_core
        if core and core.is_loaded:
            core.unfreeze(muted=muted)

    def set_libretro_volume(self, volume_pct, muted=False):
        core = self._libretro_core
        if core:
            core.set_volume(volume_pct)
            core.set_muted(muted)

    def libretro_save_state(self, slot=0):
        return self._libretro_core.save_state(slot) if self._libretro_core else False

    def libretro_load_state(self, slot=0):
        return self._libretro_core.load_state(slot) if self._libretro_core else False

    def libretro_has_state(self, slot=0):
        return bool(self._libretro_core and self._libretro_core.has_state(slot))

    def libretro_list_states(self, max_slots=5):
        return self._libretro_core.list_states(max_slots) if self._libretro_core else []



# ==============================================================================
# Libretro module-level helpers
# ==============================================================================

def _libretro_blit_scaled(screen, surf, dest_rect):
    sw, sh = surf.get_size()
    dw, dh = dest_rect[2], dest_rect[3]
    scale  = min(dw / max(sw, 1), dh / max(sh, 1))
    draw_w = int(sw * scale); draw_h = int(sh * scale)
    ox = dest_rect[0] + (dw - draw_w) // 2
    oy = dest_rect[1] + (dh - draw_h) // 2
    try:
        with _perf_timer("blit_scale"):
            screen.blit(pygame.transform.scale(surf, (draw_w, draw_h)), (ox, oy))
    except Exception as e:
        _log_warn_throttled("libretro_blit_scaled", "Libretro frame blit failed this frame: %s", e)


def _libretro_blit_bordered(screen, surf, border_surf, rect_frac, dest_rect):
    """Fit border_surf into dest_rect (letterboxed, preserving its own
    aspect ratio), then draw the actual game frame scaled into the
    detected screen-cutout rectangle (rect_frac = x/y/w/h as fractions of
    the border image — see GameDeck._detect_border_cutout_frac), so any
    correctly-punched-out border image lines up automatically regardless of
    its real pixel size or where the cutout sits within it. The game frame
    is drawn first and the border on top (its cutout is transparent, so the
    game shows through) — that way, even a slightly-off inset can't cause
    the game frame to bleed past the border's edge; the border's opaque
    art simply covers it."""
    dw, dh = dest_rect[2], dest_rect[3]
    bw, bh = border_surf.get_size()
    scale  = min(dw / max(bw, 1), dh / max(bh, 1))
    draw_bw = int(bw * scale); draw_bh = int(bh * scale)
    ox = dest_rect[0] + (dw - draw_bw) // 2
    oy = dest_rect[1] + (dh - draw_bh) // 2
    try:
        rx, ry, rw, rh = rect_frac
        inner_x = ox + int(draw_bw * rx)
        inner_y = oy + int(draw_bh * ry)
        inner_w = max(1, int(draw_bw * rw))
        inner_h = max(1, int(draw_bh * rh))
        screen.blit(pygame.transform.scale(surf, (inner_w, inner_h)), (inner_x, inner_y))
        screen.blit(pygame.transform.scale(border_surf, (draw_bw, draw_bh)), (ox, oy))
    except Exception:
        # Fall back to a plain (unbordered) fit — never let a bad/mismatched
        # border image take gameplay down with it.
        _libretro_blit_scaled(screen, surf, dest_rect)


def _libretro_draw_osd(screen, dest_rect, msg, ms_remaining, ms_total):
    try:
        FADE_MS  = 500
        alpha    = 255 if ms_remaining > FADE_MS else int(255 * ms_remaining / FADE_MS)
        alpha    = max(0, min(255, alpha))
        font_size = max(14, dest_rect[3] // 20)
        # Use the module-level font cache (see _cached_font above) instead of
        # calling pygame.font.SysFont() directly -- SysFont() queries the OS
        # font system and is expensive to call per frame. This OSD is only
        # shown briefly (save/load-state toast) but still redraws every
        # frame for up to ~2s, so an uncached SysFont() call here still adds
        # up to real, avoidable per-frame cost while it's on screen.
        font      = _cached_font("consolas", font_size, bold=True)
        text_surf = font.render(msg, True, (255, 255, 180))
        tw, th    = text_surf.get_size()
        pad_x, pad_y = 18, 8
        pill_w = tw + pad_x * 2; pill_h = th + pad_y * 2
        pill_x = dest_rect[0] + (dest_rect[2] - pill_w) // 2
        pill_y = dest_rect[1] + dest_rect[3] - pill_h - max(12, dest_rect[3] // 16)
        pill = pygame.Surface((pill_w, pill_h), pygame.SRCALPHA)
        pill.fill((0, 0, 0, 0))
        pygame.draw.rect(pill, (30, 30, 30, int(200 * alpha / 255)),
                         (0, 0, pill_w, pill_h), border_radius=pill_h // 2)
        pygame.draw.rect(pill, (180, 180, 100, int(160 * alpha / 255)),
                         (0, 0, pill_w, pill_h), width=2, border_radius=pill_h // 2)
        text_surf.set_alpha(alpha)
        pill.blit(text_surf, (pad_x, pad_y))
        screen.blit(pill, (pill_x, pill_y))
    except Exception as e:
        _log_warn_throttled("libretro_draw_osd", "Libretro OSD toast draw failed this frame: %s", e)

