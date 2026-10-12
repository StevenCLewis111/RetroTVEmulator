"""External game deck: console carousel, ROM lists, DVD player screen, gamepad input and launchers.

WARNING: surfaces drawn here are filled with (1, 1, 1), never pure black, wherever they can end up
under the game overlay. (0, 0, 0) is the overlay transparency key.
"""
import os
import sys
import re
import json
import time
import shutil
import struct
import threading
import subprocess
import zipfile
import logging
import collections
import math
import pygame
import pygame.gfxdraw
import menu_nav as _nav
import performance_governor as _gov

from media import (SELECTION_BORDER_WIDTH, resolve_main_dir, is_optical_drive_root, get_disc_info,
                    is_dvd_disc_source, apply_video_effects,
                    generate_captions_via_asr_chunked, _asr_slot, resolve_caption_source)


# ============================================================================
# SECTION: Logger & optional DVD highlight module
# ============================================================================


log = logging.getLogger(__name__)

try:
    from dvdnav_highlight import DvdnavShadowSession
except Exception as _e:
    DvdnavShadowSession = None
    log.warning(f"[DVD HIGHLIGHT] dvdnav_highlight module unavailable ({_e}) -- "
          f"menu highlight overlay disabled, playback unaffected.")


# ============================================================================
# SECTION: Archive helpers (7-Zip, zip/7z/rar entries)
# ============================================================================


def _find_7zip_exe():
    """Return path to 7z.exe, or None when 7-Zip is not installed / not on PATH."""
    candidates = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        "7z",
        "7z.exe",
    ]
    for path in candidates:
        try:
            r = subprocess.run(
                [path, "i"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if r.returncode == 0:
                return path
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    return None


ARCHIVE_EXTS = (".zip", ".7z", ".rar")


def _list_archive_entries(archive_path):
    """List the (entry_name, size) pairs inside a .zip/.7z/.rar without extracting anything."""
    ext = os.path.splitext(archive_path)[1].lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                return [(info.filename, info.file_size)
                        for info in zf.infolist() if not info.is_dir()]
        elif ext in (".7z", ".rar"):
            if _7ZIP_EXE is None:
                return None
            list_r = subprocess.run(
                [_7ZIP_EXE, "l", "-slt", archive_path],
                capture_output=True, text=True, timeout=30,
            )
            if list_r.returncode != 0:
                return None
            entries = []
            cur = None
            for line in list_r.stdout.splitlines():
                if line.startswith("Path = "):
                    cur = line[7:].strip()
                elif line.startswith("Size = ") and cur is not None:
                    try:
                        size = int(line[7:].strip())
                    except ValueError:
                        size = 0
                    entries.append((cur, size))
                    cur = None
            return entries
    except Exception as e:
        _log_warn_throttled("list_archive_contents", f"Could not list archive contents for "
              f"{os.path.basename(archive_path)}: {e}")
        return None
    return None


_ARCHIVE_CONTENT_CACHE = collections.OrderedDict()
_ARCHIVE_CONTENT_CACHE_LOCK = threading.Lock()
_ARCHIVE_CONTENT_CACHE_MAX_ENTRIES = 5000


def _archive_matches_console(archive_path, console):
    """Checks that a .zip/.7z/.rar passing the extension filter for `console` contains a file with that console's extension, cached per (path, size, mtime, console)."""
    ext = os.path.splitext(archive_path)[1].lower()
    if ext not in (".zip", ".7z", ".rar"):
        return True

    real_exts = [e for e in ROM_EXTENSIONS.get(console, []) if e not in (".zip", ".7z", ".rar")]
    if not real_exts:
        return True

    cache_key = None
    try:
        st = os.stat(archive_path)
        cache_key = (os.path.abspath(archive_path), st.st_size, st.st_mtime, console)
    except OSError:
        pass

    if cache_key is not None:
        with _ARCHIVE_CONTENT_CACHE_LOCK:
            if cache_key in _ARCHIVE_CONTENT_CACHE:
                _ARCHIVE_CONTENT_CACHE.move_to_end(cache_key)
                return _ARCHIVE_CONTENT_CACHE[cache_key]

    entries = _list_archive_entries(archive_path)
    if entries is None:
        result = True
    else:
        result = any(os.path.splitext(name)[1].lower() in real_exts
                     for name, _size in entries)

    if cache_key is not None:
        with _ARCHIVE_CONTENT_CACHE_LOCK:
            _ARCHIVE_CONTENT_CACHE[cache_key] = result
            _ARCHIVE_CONTENT_CACHE.move_to_end(cache_key)
            while len(_ARCHIVE_CONTENT_CACHE) > _ARCHIVE_CONTENT_CACHE_MAX_ENTRIES:
                _ARCHIVE_CONTENT_CACHE.popitem(last=False)
    return result

# WARNING: importing this module runs _find_7zip_exe() (up to 4 subprocess calls).
# WARNING: retro_tv_emulator.py imports game_deck lazily inside functions on purpose; a top-level import moves that cost into app boot.
_7ZIP_EXE = _find_7zip_exe()


# ============================================================================
# SECTION: Shared helpers: throttled log, fonts, drive checks
# ============================================================================


def _log_warn_throttled(_key, msg, *args, **_kwargs):
    log.error(msg, *args)


_blit_libretro_frame_log_state = {"key": None}

try:
    from libretro_core import (
        build_core_for_console, lr_id_for,
        RETRO_DEVICE_ID_JOYPAD_UP,     RETRO_DEVICE_ID_JOYPAD_DOWN,
        RETRO_DEVICE_ID_JOYPAD_LEFT,   RETRO_DEVICE_ID_JOYPAD_RIGHT,
        RETRO_DEVICE_INDEX_ANALOG_LEFT,
        RETRO_DEVICE_ID_ANALOG_X,       RETRO_DEVICE_ID_ANALOG_Y,
        GAME_FRAME_TIMER,
    )
    _LIBRETRO_AVAILABLE = True
    log.info("[GAME DECK] libretro_core loaded OK")
except ImportError as _e:
    class _NoFrameTimer:
        def add(self, *_a):
            pass

        def count(self, *_a):
            pass

        def tick_start(self):
            pass

        def tick_end(self, *_a):
            pass

        def set_pad_stats_source(self, *_a):
            pass

    GAME_FRAME_TIMER = _NoFrameTimer()
    _LIBRETRO_AVAILABLE = False
    log.warning(f"[GAME DECK] libretro_core not found — .exe fallback mode: {_e}")

pygame_hwnd_ref = [None]

_WIN = sys.platform == "win32"
if _WIN:
    import ctypes
    from ctypes import wintypes

_gd_font_cache = {}
def _cached_font(name, size, bold=False):
    k = (name, size, bold)
    if k not in _gd_font_cache:
        _gd_font_cache[k] = pygame.font.SysFont(name, size, bold=bold)
    return _gd_font_cache[k]

def _gd_is_drive_responsive(path):
    try:
        main_module = sys.modules.get("__main__")
        fn = getattr(main_module, "_is_drive_responsive", None)
        if fn is not None:
            return fn(path)
    except Exception:
        pass
    return True


def _gd_drive_root_of(path):
    """Return a path's drive root; a local copy of media.py's _drive_root_of()."""
    if not path:
        return None
    if sys.platform == "win32":
        drive, _tail = os.path.splitdrive(str(path))
        return (drive + "\\") if drive else None
    return "/"


# ============================================================================
# SECTION: Per-process audio (WASAPI)
# ============================================================================


def _wasapi_set_process_audio(pid, *, mute=None, volume_pct=None):
    if not _WIN or (mute is None and volume_pct is None):
        return False
    try:
        import uuid as _uuid
        _c   = ctypes
        _ole = _c.windll.ole32
        _ole.CoInitializeEx(None, 0)

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

        def _vt(iface, idx, restype, argtypes, *args):
            """Call COM vtable method[idx] on a raw c_void_p interface pointer."""
            vt   = _c.cast(iface, _c.POINTER(PVOID)).contents.value
            fptr = _c.cast(_c.cast(vt, _c.POINTER(PVOID))[idx],
                           _c.WINFUNCTYPE(restype, PVOID, *argtypes))
            return fptr(iface, *args)

        pE = PVOID()
        if _ole.CoCreateInstance(
                _c.byref(CLSID_MMDE), None, 1,
                _c.byref(IID_MMDE), _c.byref(pE)) != 0 or not pE.value:
            return False

        pDev = PVOID()
        hr = _vt(pE, 4, _c.c_long,
                 [_c.c_int, _c.c_int, _c.POINTER(PVOID)],
                 0, 0, _c.byref(pDev))
        _vt(pE, 2, _c.c_long, [])
        if hr != 0 or not pDev.value:
            return False

        pMgr = PVOID()
        hr = _vt(pDev, 3, _c.c_long,
                 [_c.POINTER(GUID_T), _c.c_uint, PVOID, _c.POINTER(PVOID)],
                 _c.byref(IID_ASM2), 23, None, _c.byref(pMgr))
        _vt(pDev, 2, _c.c_long, [])
        if hr != 0 or not pMgr.value:
            return False

        pSE = PVOID()
        hr = _vt(pMgr, 5, _c.c_long, [_c.POINTER(PVOID)], _c.byref(pSE))
        _vt(pMgr, 2, _c.c_long, [])
        if hr != 0 or not pSE.value:
            return False

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

            pCtl2 = PVOID()
            qhr = _vt(pCtl, 0, _c.c_long,
                      [_c.POINTER(GUID_T), _c.POINTER(PVOID)],
                      _c.byref(IID_ASC2), _c.byref(pCtl2))
            spid = _c.c_ulong(0)
            if qhr == 0 and pCtl2.value:
                _vt(pCtl2, 14, _c.c_long,
                    [_c.POINTER(_c.c_ulong)], _c.byref(spid))
                _vt(pCtl2, 2, _c.c_long, [])

            if spid.value == pid:
                pVol = PVOID()
                vhr = _vt(pCtl, 0, _c.c_long,
                          [_c.POINTER(GUID_T), _c.POINTER(PVOID)],
                          _c.byref(IID_SAV), _c.byref(pVol))
                if vhr == 0 and pVol.value:
                    if volume_pct is not None:
                        gain = max(0.0, min(1.0, float(volume_pct) / 100.0))
                        _vt(pVol, 3, _c.c_long,
                            [_c.c_float, PVOID], gain, None)
                    if mute is not None:
                        _vt(pVol, 5, _c.c_long,
                            [_c.c_int, PVOID], int(bool(mute)), None)
                    _vt(pVol, 2, _c.c_long, [])
                    found = True

            _vt(pCtl, 2, _c.c_long, [])

        _vt(pSE, 2, _c.c_long, [])
        return found

    except Exception as e:
        _log_warn_throttled("wasapi_ctypes_init", f"[WASAPI ctypes] error: {e}")
        return False


# ============================================================================
# SECTION: Console tables & folder matching
# ============================================================================


CONSOLE_ORDER = ["MENU", "DVD",
                  "A2600",
                  "GB", "GBA", "GBC", "GG",
                  "NGP", "NGPC", "N64", "NES",
                  "PSX",
                  "32X", "GENESIS", "MASTERSYSTEM",
                  "SNES"]

CONSOLE_LABELS = {
    "GB":      "GAME BOY",
    "GBC":     "GAME BOY COLOR",
    "GBA":     "GAME BOY ADVANCE",
    "NES":     "NINTENDO ENTERTAINMENT SYSTEM",
    "SNES":    "SUPER NINTENDO",
    "GENESIS": "SEGA GENESIS",
    "N64":     "NINTENDO 64",
    "PSX":     "PLAYSTATION",
    "GG":      "GAME GEAR",
    "NGP":     "NEO GEO POCKET",
    "DVD":     "DVD VIDEO INTERACTIVE",
    "MENU":    "MENU OPTIONS",
    "A2600":        "ATARI 2600",
    "NGPC":         "NEO GEO POCKET COLOR",
    "32X":          "SEGA 32X",
    "MASTERSYSTEM": "SEGA MASTER SYSTEM",
}

ROM_EXTENSIONS = {
    "GB":      [".gb", ".zip", ".7z", ".rar"],
    "GBC":     [".gbc", ".gb", ".zip", ".7z", ".rar"],
    "GBA":     [".gba", ".zip", ".7z", ".rar"],
    "NES":     [".nes", ".zip", ".7z", ".rar"],
    "SNES":    [".smc", ".sfc", ".zip", ".7z", ".rar"],
    "GENESIS": [".md", ".gen", ".bin", ".smd", ".zip", ".7z", ".rar"],
    "N64":     [".n64", ".z64", ".v64", ".zip", ".7z", ".rar"],
    "PSX":     [".cue", ".bin", ".iso", ".chd", ".pbp", ".img", ".zip", ".7z", ".rar"],
    "GG":      [".gg", ".zip", ".7z", ".rar"],
    "NGP":     [".ngp", ".zip", ".7z", ".rar"],
    "DVD":     [".mp4", ".mkv", ".iso", ".vob", ".mpg", ".mpeg", ".avi", ".zip", ".7z", ".rar"],
    "A2600":        [".a26", ".bin", ".zip", ".7z", ".rar"],
    "NGPC":         [".ngc", ".zip", ".7z", ".rar"],
    "32X":          [".32x", ".bin", ".smd", ".md", ".zip", ".7z", ".rar"],
    "MASTERSYSTEM": [".sms", ".zip", ".7z", ".rar"],
}

CONSOLE_FOLDER_ALIASES = {
    "GB":           ["gb", "gameboy", "game boy"],
    "GBC":          ["gbc", "gameboy color", "game boy color", "gb color"],
    "GBA":          ["gba", "gameboy advance", "game boy advance"],
    "NES":          ["nes", "nintendo", "nintendo entertainment system", "famicom"],
    "SNES":         ["snes", "super nintendo", "super nes", "super famicom", "sfc"],
    "GENESIS":      ["genesis", "sega genesis", "megadrive", "mega drive", "sega md", "md"],
    "N64":          ["n64", "nintendo64", "nintendo 64"],
    "PSX":          ["psx", "playstation", "playstation1", "playstation 1",
                      "ps1", "psone", "ps one", "sony playstation"],
    "GG":           ["gg", "gamegear", "game gear", "sega game gear"],
    "NGP":          ["ngp", "neogeopocket", "neo geo pocket"],
    "NGPC":         ["ngpc", "neogeopocketcolor", "neo geo pocket color"],
    "DVD":          ["dvd", "dvds", "dvd video", "movies", "movie"],
    "A2600":        ["a2600", "atari2600", "atari 2600", "2600"],
    "32X":          ["32x", "sega32x", "sega 32x"],
    "MASTERSYSTEM": ["mastersystem", "master system", "sega master system", "sms"],
}


def _normalize_folder_name(name):
    """Lowercase and collapse every run of non-alphanumeric characters into a single space, e.g."""
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


_CONSOLE_ALIAS_PAIRS = sorted(
    (
        (_normalize_folder_name(alias), _normalize_folder_name(alias).replace(" ", ""), console)
        for console, aliases in CONSOLE_FOLDER_ALIASES.items()
        for alias in aliases
    ),
    key=lambda pair: len(pair[1]),
    reverse=True,
)


def match_console_for_folder_name(name):
    """Return the console key whose folder-name aliases best match `name`, or None."""
    norm = _normalize_folder_name(name)
    if not norm:
        return None
    joined = norm.replace(" ", "")
    padded_norm = f" {norm} "
    for alias_norm, alias_joined, console in _CONSOLE_ALIAS_PAIRS:
        if not alias_joined:
            continue
        if f" {alias_norm} " in padded_norm:
            return console
        if " " in alias_norm and alias_joined in joined:
            return console
    return None


# ============================================================================
# SECTION: Console capability & option constants
# ============================================================================


HANDHELD_CONSOLES = {"GB", "GBC", "GBA", "GG", "NGP", "NES", "SNES", "GENESIS", "N64", "PSX",
                      "A2600", "NGPC", "32X", "MASTERSYSTEM"}

BIOS_REQUIRED_CONSOLES = {"PSX"}

DVD_SPLASH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif"}
DVD_SPLASH_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg"}

CONSOLE_SCROLL_STYLES = ["carousel", "side", "updown"]
CONSOLE_SCROLL_STYLE_LABELS = {
    "carousel": "CAROUSEL",
    "side":     "SIDE TO SIDE",
    "updown":   "UP / DOWN",
}

LIST_TEXT_COLOR_NAMES = ["white", "black", "red", "orange", "yellow", "green", "blue", "purple"]
LIST_TEXT_COLOR_RGB = {
    "white":  (255, 255, 255),
    "black":  (0, 0, 0),
    "red":    (220, 60, 60),
    "orange": (230, 140, 40),
    "yellow": (230, 210, 60),
    "green":  (80, 220, 120),
    "blue":   (80, 140, 230),
    "purple": (170, 90, 220),
}


# ============================================================================
# SECTION: Libretro core matching
# ============================================================================


CORE_NAME_TO_CONSOLES = {
    "fceumm":          ["NES"],
    "nestopia":        ["NES"],
    "mesen":           ["NES"],
    "quicknes":        ["NES"],
    "snes9x":          ["SNES"],
    "snes9x2002":      ["SNES"],
    "snes9x2005plus":  ["SNES"],
    "snes9x2005":      ["SNES"],
    "snes9x2010":      ["SNES"],
    "bsnesmercury":    ["SNES"],
    "bsneshdbeta":     ["SNES"],
    "bsnes2014":       ["SNES"],
    "bsnesjg":         ["SNES"],
    "bsnes":           ["SNES"],
    "mesens":          ["SNES"],
    "genesisplusgxwide": ["GENESIS", "MASTERSYSTEM", "GG"],
    "genesisplusgx":   ["GENESIS", "MASTERSYSTEM", "GG"],
    "picodrive":       ["GENESIS", "32X", "MASTERSYSTEM"],
    "gambatte":        ["GB", "GBC"],
    "gearboy":         ["GB", "GBC"],
    "sameboy":         ["GB", "GBC"],
    "tgbdual":         ["GB", "GBC"],
    "mgba":            ["GBA", "GB", "GBC"],
    "vbanext":         ["GBA"],
    "vbam":            ["GBA"],
    "meteor":          ["GBA"],
    "smsplus":         ["GG", "MASTERSYSTEM"],
    "mednafenngp":     ["NGP", "NGPC"],
    "beetlengp":       ["NGP", "NGPC"],
    "stella2023":      ["A2600"],
    "stella2014":      ["A2600"],
    "stella":          ["A2600"],
    "mupen64plusnext": ["N64"],
    "parallel64":      ["N64"],
    "mednafenpsxhw":   ["PSX"],
    "mednafenpsx":     ["PSX"],
    "beetlepsx":       ["PSX"],
    "pcsxrearmed":     ["PSX"],
    "swanstation":     ["PSX"],
}


PREFERRED_CORE_ORDER = {
    "NES":          ["fceumm", "mesen", "nestopia", "quicknes"],
    "SNES":         ["snes9x", "bsnesmercury", "bsnes", "mesens",
                      "snes9x2010", "snes9x2005plus", "snes9x2005", "snes9x2002",
                      "bsnes2014balanced", "bsneshdbeta", "bsnesjg"],
    "GENESIS":      ["genesisplusgx", "picodrive", "genesisplusgxwide"],
    "32X":          ["picodrive"],
    "MASTERSYSTEM": ["genesisplusgx", "smsplus", "picodrive", "genesisplusgxwide"],
    "GB":           ["sameboy", "gambatte", "mgba", "gearboy", "tgbdual"],
    "GBC":          ["sameboy", "gambatte", "mgba", "gearboy", "tgbdual"],
    "GBA":          ["mgba", "vbanext", "vbam", "meteor"],
    "GG":           ["genesisplusgx", "smsplus", "genesisplusgxwide"],
    "NGP":          ["mednafenngp", "beetlengp"],
    "NGPC":         ["mednafenngp", "beetlengp"],
    "A2600":        ["stella2023", "stella2014", "stella"],
    "N64":          ["mupen64plusnext", "parallel64"],
    "PSX":          ["mednafenpsx", "beetlepsx", "pcsxrearmed", "swanstation", "mednafenpsxhw"],
}

_LIBRETRO_LOAD_TAU_MS = {
    "PSX": 9000,
}
_LIBRETRO_LOAD_TAU_MS_DEFAULT = 1200

_LIBRETRO_HOLD_AT_100_MS = 350

_LIBRETRO_FIRST_FRAME_GIVEUP_MS = {
    "PSX": 75000,
}
_LIBRETRO_FIRST_FRAME_GIVEUP_MS_DEFAULT = 20000

_CORE_NAME_PAIRS = sorted(CORE_NAME_TO_CONSOLES.items(), key=lambda kv: len(kv[0]), reverse=True)


def _normalize_core_key(filename):
    """'Snes9x_2010_libretro.DLL' -> 'snes9x2010'."""
    base = os.path.splitext(str(filename))[0]
    base = re.sub(r"[^a-z0-9]+", "", base.lower())
    if base.endswith("libretro"):
        base = base[: -len("libretro")]
    return base


def match_consoles_for_core_filename(filename):
    """Return the console keys a core .dll plays, or [] if it matches no known RetroArch core name."""
    norm = _normalize_core_key(filename)
    if not norm:
        return []
    for key, consoles in _CORE_NAME_PAIRS:
        if key and key in norm:
            return consoles
    return []


def resolve_preferred_core_filename(console, present_filenames):
    """Return the best core filename from a console's core folder: first PREFERRED_CORE_ORDER match, else first alphabetically, else None."""
    if not present_filenames:
        return None
    for pref in PREFERRED_CORE_ORDER.get(console, []):
        for fname in present_filenames:
            if _normalize_core_key(fname) == pref:
                return fname
    return sorted(present_filenames)[0]


def get_bios_dir_for_console(base_dir, console):
    if console not in BIOS_REQUIRED_CONSOLES:
        return None
    return os.path.join(base_dir, "main", "cores", console, "system")


# ============================================================================
# SECTION: Borders & bezel availability
# ============================================================================


BORDER_CONSOLES_HANDHELD = {"GB", "GBC", "GBA", "GG", "NGP", "NGPC"}
BORDER_CONSOLES_HOME     = {"NES", "SNES", "GENESIS", "N64", "PSX",
                             "A2600", "32X", "MASTERSYSTEM"}
BORDER_CONSOLES = BORDER_CONSOLES_HANDHELD | BORDER_CONSOLES_HOME


def _discover_border_style_files(bezel_dir):
    """List the bezel style files in a single console's own border folder."""
    files = []
    if os.path.isdir(bezel_dir):
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
        unnumbered_path = None
        numbered = {}
        try:
            for f in os.listdir(bezel_dir):
                stem, ext = os.path.splitext(f)
                if ext.lower() not in exts:
                    continue
                if stem.lower() in ("bezel", "border") and unnumbered_path is None:
                    unnumbered_path = os.path.join(bezel_dir, f)
                    continue
                m = re.match(r"^(?:bezel|border)(\d+)$", stem, re.IGNORECASE)
                if m:
                    numbered[int(m.group(1))] = os.path.join(bezel_dir, f)
        except Exception as e:
            log.warning("_discover_border_style_files: could not list %s: %s", bezel_dir, e)

        ordered_numbered = [numbered[n] for n in sorted(numbered.keys())]
        if unnumbered_path is not None:
            files = [unnumbered_path] + ordered_numbered
        else:
            files = ordered_numbered
    return files


def border_available_for_console(console):
    """Whether this console's TV bezel toggle is reachable at all."""
    return console in BORDER_CONSOLES


def console_bezel_currently_active(console, db):
    """True when `console`'s own decorative bezel is being composited on screen for its active profile."""
    # WARNING: mirrors the gates _blit_libretro_frame and _outer_tv_bezel_blocks_console_bezel use at render time so it never disagrees with the screen.
    if db is None or console not in BORDER_CONSOLES_HOME:
        return False
    if not border_available_for_console(console):
        return False
    if bool(db.config.get("fake_43_border_enabled", False)):
        return False
    con_prof_cfg = db.config.get("console_profiles", {}).get(console, {})
    try:
        active = int(con_prof_cfg.get("active", 1))
    except (TypeError, ValueError):
        active = 1
    profiles_map = con_prof_cfg.get("profiles", {})
    this_prof = profiles_map.get(active, profiles_map.get(str(active), {}))
    return bool(this_prof.get("border_enabled", this_prof.get("gb_border", False)))


# ============================================================================
# SECTION: Players, screen sizes & PSX discs
# ============================================================================


MULTITAP_CONSOLES = {"SNES", "NES", "GENESIS", "PSX"}

CONSOLE_MAX_PLAYERS = {
    "GB": 1, "GBC": 1, "GBA": 1, "GG": 1, "NGP": 1, "NGPC": 1,
    "A2600": 2, "32X": 2, "MASTERSYSTEM": 2,
    "NES": 4, "SNES": 4, "GENESIS": 4, "N64": 4, "PSX": 4,
}

SCREEN_SIZE_OPTIONS = [
    ("full", "FULL", 1.0),
    ("75",   "75%",  0.75),
    ("50",   "50%",  0.5),
]
SCREEN_SIZE_FACTORS = {key: factor for key, _label, factor in SCREEN_SIZE_OPTIONS}

_DISC_MARKER_RE = re.compile(
    r'[\(\[\s_\-]*(?:disc|disk|cd)[\s_\-]*(\d+)(?:\s*(?:of|/)\s*\d+)?[\)\]]?',
    re.IGNORECASE
)


def find_psx_disc_siblings(rom_path):
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
        log.warning(f"[LIBRETRO] Disc sibling scan failed: {e}")
        return []


# ============================================================================
# SECTION: Gamepad input: XInput, DirectInput & HID (Windows)
# ============================================================================


_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_INFORMATION = 0x0400

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

_XI_UP    = 0x0001;  _XI_DOWN  = 0x0002
_XI_LEFT  = 0x0004;  _XI_RIGHT = 0x0008
_XI_START = 0x0010;  _XI_BACK  = 0x0020
_XI_LS    = 0x0040;  _XI_RS    = 0x0080
_XI_LB    = 0x0100;  _XI_RB    = 0x0200
_XI_A     = 0x1000;  _XI_B     = 0x2000
_XI_X     = 0x4000;  _XI_Y     = 0x8000
_XI_LT    = 0x10000;  _XI_RT    = 0x20000

_DI_RAW_BUTTON_BASE = 1 << 24

def _di_raw_button_bit(index):
    return _DI_RAW_BUTTON_BASE << int(index)

_XI_LABELS = {
    _XI_UP: "Dpad U", _XI_DOWN: "Dpad D", _XI_LEFT: "Dpad L", _XI_RIGHT: "Dpad R",
    _XI_START: "Start", _XI_BACK: "Select",
    _XI_LS: "L-Stick", _XI_RS: "R-Stick",
    _XI_LB: "L", _XI_RB: "R",
    _XI_A: "A", _XI_B: "B", _XI_X: "X", _XI_Y: "Y",
    _XI_LT: "LT", _XI_RT: "RT",
}
for _di_label_index in range(32):
    _XI_LABELS.setdefault(
        _di_raw_button_bit(_di_label_index),
        f"Button {_di_label_index + 1}",
    )
_XI_LEGACY_LABELS = {
    "D-Up": _XI_UP, "D-Down": _XI_DOWN, "D-Left": _XI_LEFT, "D-Right": _XI_RIGHT,
    "LB": _XI_LB, "RB": _XI_RB,
    "A-btn": _XI_A, "B-btn": _XI_B, "X-btn": _XI_X, "Y-btn": _XI_Y,
}
_XI_LABEL_TO_BIT = {label: bit for bit, label in _XI_LABELS.items()}
for _legacy_label, _legacy_bit in _XI_LEGACY_LABELS.items():
    _XI_LABEL_TO_BIT.setdefault(_legacy_label, _legacy_bit)
del _legacy_label, _legacy_bit


_wine_check_done = False
_wine_detected    = False

def _is_running_under_wine():
    global _wine_check_done, _wine_detected
    if _wine_check_done:
        return _wine_detected
    _wine_check_done = True
    try:
        ver_fn = ctypes.windll.ntdll.wine_get_version
        ver_fn.restype = ctypes.c_char_p
        ver = ver_fn()
        _wine_detected = bool(ver)
        if _wine_detected:
            log.info(f"[GAMEPAD] Detected Wine/Proton (wine_get_version={ver}) "
                  f"-- Linux joystick fallback available if XInput finds no pad.")
    except Exception:
        _wine_detected = False
    return _wine_detected


class _GPState:
    __slots__ = (
        "wButtons", "bLT", "bRT", "sLX", "sLY", "sRX", "sRY", "rawLX", "rawLY", "rawRX", "rawRY",
        "source", "raw_buttons", "raw_button_count",
    )

    def __init__(self):
        self.wButtons = 0
        self.bLT = 0
        self.bRT = 0
        self.sLX = 0
        self.sLY = 0
        self.sRX = 0
        self.sRY = 0
        self.rawLX = 0
        self.rawLY = 0
        self.rawRX = 0
        self.rawRY = 0
        self.source = "xinput"
        self.raw_buttons = 0
        self.raw_button_count = 0


_xinput_seen_logged = set()
_xinput_last_raw     = {}


# ============================================================================
# SECTION: XInput structures and DLL handle cache
# ============================================================================
class _XI_GP(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLT", ctypes.c_ubyte), ("bRT", ctypes.c_ubyte),
                ("sLX", ctypes.c_short), ("sLY", ctypes.c_short),
                ("sRX", ctypes.c_short), ("sRY", ctypes.c_short)]


class _XI_STATE(ctypes.Structure):
    _fields_ = [("dwPkt", ctypes.c_ulong), ("Gamepad", _XI_GP)]


# WARNING: fallback order must stay newest first; the first DLL that loads is the one used for every later call.
_XI_LIB_NAMES = ("xinput1_4", "xinput1_3", "xinput9_1_0")
_xinput_dll = {"handle": None}


def _xinput_handle():
    xi = _xinput_dll["handle"]
    if xi is not None:
        return xi
    for lib in _XI_LIB_NAMES:
        try:
            xi = ctypes.windll.LoadLibrary(lib)
        except Exception:
            continue
        _xinput_dll["handle"] = xi
        return xi
    return None


# ============================================================================
# SECTION: XInput reads
# ============================================================================
def _try_xinput_read():
    xi = _xinput_handle()
    if xi is None:
        return None
    # WARNING: the state buffer is per call, never module level; the menu-nav thread and the main thread both call this.
    state = _XI_STATE()
    for idx in range(4):
        try:
            rc = xi.XInputGetState(idx, ctypes.byref(state))
            if rc == 0:
                g = _GPState()
                g.wButtons = state.Gamepad.wButtons
                g.bLT = state.Gamepad.bLT
                g.bRT = state.Gamepad.bRT
                g.sLX = state.Gamepad.sLX
                g.sLY = state.Gamepad.sLY
                g.sRX = state.Gamepad.sRX
                g.sRY = state.Gamepad.sRY
                apply_stick_correction(g)
                if idx not in _xinput_seen_logged:
                    _xinput_seen_logged.add(idx)
                snap = (g.wButtons, g.bLT, g.bRT, g.sLX, g.sLY, g.sRX, g.sRY)
                if _xinput_last_raw.get(idx) != snap:
                    _xinput_last_raw[idx] = snap
                return g
        except Exception:
            continue
    return None


_xinput_connected_slots = set()


def _try_xinput_read_all():
    global _xinput_connected_slots
    out = {}
    xi = _xinput_handle()
    if xi is None:
        return out
    for idx in range(4):
        state = _XI_STATE()
        try:
            if xi.XInputGetState(idx, ctypes.byref(state)) == 0:
                g = _GPState()
                g.wButtons = state.Gamepad.wButtons
                g.bLT = state.Gamepad.bLT
                g.bRT = state.Gamepad.bRT
                g.sLX = state.Gamepad.sLX
                g.sLY = state.Gamepad.sLY
                g.sRX = state.Gamepad.sRX
                g.sRY = state.Gamepad.sRY
                apply_stick_correction(g)
                out[idx] = g
        except Exception:
            continue

    _now_connected = set(out.keys())
    for _idx in _now_connected - _xinput_connected_slots:
        log.info("[CONTROLLER] XInput controller connected at slot %s", _idx)
    for _idx in _xinput_connected_slots - _now_connected:
        log.warning("[CONTROLLER] XInput controller at slot %s disconnected", _idx)
    _xinput_connected_slots = _now_connected
    return out


class _JOYCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort),
        ("szPname", ctypes.c_wchar * 32),
        ("wXmin", ctypes.c_uint), ("wXmax", ctypes.c_uint),
        ("wYmin", ctypes.c_uint), ("wYmax", ctypes.c_uint),
        ("wZmin", ctypes.c_uint), ("wZmax", ctypes.c_uint),
        ("wNumButtons", ctypes.c_uint),
        ("wPeriodMin", ctypes.c_uint), ("wPeriodMax", ctypes.c_uint),
        ("wRmin", ctypes.c_uint), ("wRmax", ctypes.c_uint),
        ("wUmin", ctypes.c_uint), ("wUmax", ctypes.c_uint),
        ("wVmin", ctypes.c_uint), ("wVmax", ctypes.c_uint),
        ("wCaps", ctypes.c_uint), ("wMaxAxes", ctypes.c_uint),
        ("wNumAxes", ctypes.c_uint), ("wMaxButtons", ctypes.c_uint),
        ("szRegKey", ctypes.c_wchar * 32),
        ("szOEMVxD", ctypes.c_wchar * 260),
    ]


class _JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
        ("dwXpos", ctypes.c_uint), ("dwYpos", ctypes.c_uint),
        ("dwZpos", ctypes.c_uint), ("dwRpos", ctypes.c_uint),
        ("dwUpos", ctypes.c_uint), ("dwVpos", ctypes.c_uint),
        ("dwButtons", ctypes.c_uint), ("dwButtonNumber", ctypes.c_uint),
        ("dwPOV", ctypes.c_uint),
        ("dwReserved1", ctypes.c_uint), ("dwReserved2", ctypes.c_uint),
    ]


_JOY_RETURNALL_CENTERED = 0xFF | 0x400
_JOYERR_NOERROR = 0
_JOYCAPS_HASPOV = 0x0010

_DI_BUTTON_ORDER = [_XI_A, _XI_B, _XI_X, _XI_Y, _XI_LB, _XI_RB,
                     _XI_BACK, _XI_START, _XI_LS, _XI_RS]

_DI_BUTTON_ORDER_DPAD_EXT = [_XI_UP, _XI_DOWN, _XI_LEFT, _XI_RIGHT]

_dinput_names_logged = set()
_dinput_no_pov_logged = set()
_dinput_diag_logged  = set()
_dinput_last_raw     = {}

_dinput_first_seen_raw = {}
_dinput_ever_live      = set()
_dinput_dpad_latch     = {}

_DINPUT_AXIS_LIVENESS_THRESHOLD = 1500

_dinput_signature_of   = {}


_GENERIC_READ  = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ  = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_DIGCF_PRESENT          = 0x02
_DIGCF_DEVICEINTERFACE  = 0x10
_SPDRP_LOCATION_PATHS   = 0x23
_INVALID_HANDLE_VALUE   = ctypes.c_void_p(-1).value if _WIN else None


if _WIN:
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    _GUID_DEVINTERFACE_HID = _GUID(
        0x4D1E55B2, 0xF16F, 0x11CF,
        (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))

    class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("InterfaceClassGuid", _GUID),
                    ("Flags", ctypes.c_ulong),
                    ("Reserved", ctypes.POINTER(ctypes.c_ulong))]

    class _SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("ClassGuid", _GUID),
                    ("DevInst", ctypes.c_ulong),
                    ("Reserved", ctypes.POINTER(ctypes.c_ulong))]

    class _HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Size", ctypes.c_ulong), ("VendorID", ctypes.c_ushort),
                    ("ProductID", ctypes.c_ushort),
                    ("VersionNumber", ctypes.c_ushort)]


_hid_identity_cache = None
# VALUES CHANGE HERE
_HID_IDENTITY_CACHE_TTL = 2.0
_hid_scan_failed_logged = False


def _scan_hid_identities():
    global _hid_scan_failed_logged
    out = []
    if not _WIN or _is_running_under_wine():
        return out
    try:
        setupapi = ctypes.windll.setupapi
        hid      = ctypes.windll.hid
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return out

    h_devinfo = None
    try:
        h_devinfo = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(_GUID_DEVINTERFACE_HID), None, None,
            _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
        if not h_devinfo or h_devinfo == _INVALID_HANDLE_VALUE:
            return out

        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    h_devinfo, None, ctypes.byref(_GUID_DEVINTERFACE_HID),
                    index, ctypes.byref(iface)):
                break
            index += 1

            needed = ctypes.c_ulong(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                h_devinfo, ctypes.byref(iface), None, 0,
                ctypes.byref(needed), None)
            if needed.value == 0:
                continue

            devinfo_data = _SP_DEVINFO_DATA()
            devinfo_data.cbSize = ctypes.sizeof(devinfo_data)
            buf = ctypes.create_string_buffer(needed.value)
            fixed_cbsize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            ctypes.memmove(buf, ctypes.byref(ctypes.c_ulong(fixed_cbsize)), 4)
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                h_devinfo, ctypes.byref(iface), buf, needed,
                ctypes.byref(needed), ctypes.byref(devinfo_data))
            if not ok:
                continue
            device_path = ctypes.wstring_at(ctypes.byref(buf, 4))

            entry = {"vid": None, "pid": None, "serial": None, "location": None}

            try:
                loc_buf = ctypes.create_unicode_buffer(512)
                loc_needed = ctypes.c_ulong(0)
                if setupapi.SetupDiGetDeviceRegistryPropertyW(
                        h_devinfo, ctypes.byref(devinfo_data),
                        _SPDRP_LOCATION_PATHS, None, ctypes.byref(loc_buf),
                        ctypes.sizeof(loc_buf), ctypes.byref(loc_needed)):
                    first = ctypes.wstring_at(loc_buf)
                    entry["location"] = first or None
            except Exception:
                pass

            handle = None
            try:
                handle = kernel32.CreateFileW(
                    device_path, _GENERIC_READ | _GENERIC_WRITE,
                    _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                    _OPEN_EXISTING, 0, None)
                if handle and handle != _INVALID_HANDLE_VALUE:
                    attrs = _HIDD_ATTRIBUTES()
                    attrs.Size = ctypes.sizeof(attrs)
                    if hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                        entry["vid"] = attrs.VendorID
                        entry["pid"] = attrs.ProductID
                    ser_buf = ctypes.create_unicode_buffer(256)
                    if hid.HidD_GetSerialNumberString(
                            handle, ser_buf, ctypes.sizeof(ser_buf)):
                        s = (ser_buf.value or "").strip()
                        if s:
                            entry["serial"] = s
            except Exception:
                pass
            finally:
                if handle and handle != _INVALID_HANDLE_VALUE:
                    try:
                        kernel32.CloseHandle(handle)
                    except Exception:
                        pass

            if entry["vid"] is not None:
                out.append(entry)
    except Exception:
        if not _hid_scan_failed_logged:
            _hid_scan_failed_logged = True
    finally:
        if h_devinfo:
            try:
                setupapi.SetupDiDestroyDeviceInfoList(h_devinfo)
            except Exception:
                pass
    return out


def _get_hid_identities():
    global _hid_identity_cache
    now = time.monotonic()
    if _hid_identity_cache is not None and now - _hid_identity_cache[0] < _HID_IDENTITY_CACHE_TTL:
        return _hid_identity_cache[1]
    _hid_t0 = time.perf_counter()
    identities = _scan_hid_identities()
    _hid_identity_cache = (time.monotonic(), identities)
    _PAD_COUNTERS["hid_scans"] += 1
    _PAD_COUNTERS["hid_ms"] += (time.perf_counter() - _hid_t0) * 1000.0
    return identities


def _enrich_signature_with_hid_identity(jid, vid, pid, base_sig):
    try:
        identities = _get_hid_identities()
        matches = [h for h in identities if h["vid"] == vid and h["pid"] == pid]
        if not matches:
            return base_sig
        if len(matches) == 1:
            h = matches[0]
        else:
            prefix = f"legacy:{vid:04x}:{pid:04x}:"
            sibling_jids = sorted(
                {j for j, sig in _dinput_signature_of.items() if sig.startswith(prefix)} | {jid}
            )
            matches_sorted = sorted(matches, key=lambda h: (h["location"] or "", h["serial"] or ""))
            pos = sibling_jids.index(jid) if jid in sibling_jids else 0
            h = matches_sorted[min(pos, len(matches_sorted) - 1)]
        if h["serial"]:
            return f"{base_sig}:serial:{h['serial']}"
        if h["location"]:
            return f"{base_sig}:port:{h['location']}"
        return base_sig
    except Exception:
        return base_sig


def _norm_axis(raw, lo, hi):
    if hi <= lo:
        return 0
    frac = (raw - lo) / float(hi - lo)
    return max(-32768, min(32767, int(frac * 65535) - 32768))


_dinput_frame_cache = None
# VALUES CHANGE HERE
_DINPUT_FRAME_CACHE_WINDOW = 0.008


def _try_dinput_read_all():
    global _dinput_frame_cache
    now = time.monotonic()
    if _dinput_frame_cache is not None:
        cached_ts, cached_result = _dinput_frame_cache
        if now - cached_ts < _DINPUT_FRAME_CACHE_WINDOW:
            return cached_result
    result = _try_dinput_read_all_impl()
    # WARNING: the cache stamp must be taken AFTER the read, or a read slower than the window is expired the moment it is stored.
    _dinput_frame_cache = (time.monotonic(), result)
    _PAD_COUNTERS["joy_reads"] += 1
    return result


def _try_dinput_read_all_impl():
    if _is_running_under_wine():
        return {}
    out = {}
    try:
        winmm = ctypes.windll.winmm
    except Exception:
        return out
    try:
        num_devs = winmm.joyGetNumDevs()
    except Exception:
        return out
    if num_devs == 0 and "num_devs_zero" not in _dinput_diag_logged:
        _dinput_diag_logged.add("num_devs_zero")

    for jid in range(min(int(num_devs), 16)):
        caps = _JOYCAPSW()
        try:
            caps_rc = winmm.joyGetDevCapsW(jid, ctypes.byref(caps), ctypes.sizeof(caps))
        except Exception:
            if (jid, "caps_exc") not in _dinput_diag_logged:
                _dinput_diag_logged.add((jid, "caps_exc"))
            continue
        if caps_rc != _JOYERR_NOERROR:
            if (jid, "caps_rc") not in _dinput_diag_logged:
                _dinput_diag_logged.add((jid, "caps_rc"))
            continue

        name = caps.szPname or ""
        _base_sig = f"legacy:{caps.wMid:04x}:{caps.wPid:04x}:{name.strip()}"
        _dinput_signature_of[jid] = _enrich_signature_with_hid_identity(
            jid, caps.wMid, caps.wPid, _base_sig)
        if (jid, "seen", name) not in _dinput_diag_logged:
            _dinput_diag_logged.add((jid, "seen", name))
        if "xinput" in name.lower():
            continue

        info = _JOYINFOEX()
        info.dwSize = ctypes.sizeof(info)
        info.dwFlags = _JOY_RETURNALL_CENTERED
        try:
            pos_rc = winmm.joyGetPosEx(jid, ctypes.byref(info))
        except Exception:
            if (jid, "pos_exc") not in _dinput_diag_logged:
                _dinput_diag_logged.add((jid, "pos_exc"))
            continue
        if pos_rc != _JOYERR_NOERROR:
            if (jid, "pos_rc") not in _dinput_diag_logged:
                _dinput_diag_logged.add((jid, "pos_rc"))
            continue

        if jid not in _dinput_names_logged:
            _dinput_names_logged.add(jid)
            log.info(f"[GAMEPAD] Legacy joystick API found a non-XInput pad: "
                  f"id={jid} name={name!r} -- available as an extra player slot.")

        raw_snapshot = (info.dwButtons, info.dwPOV, info.dwXpos, info.dwYpos)
        raw_changed_this_frame = _dinput_last_raw.get(jid) != raw_snapshot
        if raw_changed_this_frame:
            _dinput_last_raw[jid] = raw_snapshot
            if jid not in _dinput_first_seen_raw:
                _dinput_first_seen_raw[jid] = raw_snapshot
            elif raw_snapshot != _dinput_first_seen_raw[jid]:
                first_btn, first_pov, first_x, first_y = _dinput_first_seen_raw[jid]
                btn_or_pov_changed = (info.dwButtons != first_btn) or (info.dwPOV != first_pov)
                axis_moved = (
                    abs(info.dwXpos - first_x) > _DINPUT_AXIS_LIVENESS_THRESHOLD
                    or abs(info.dwYpos - first_y) > _DINPUT_AXIS_LIVENESS_THRESHOLD
                )
                if btn_or_pov_changed or axis_moved:
                    _dinput_ever_live.add(jid)

        g = _GPState()
        g.source = "dinput"
        g.raw_buttons = int(info.dwButtons)
        _xlo, _xhi = (caps.wXmin, caps.wXmax) if caps.wXmax > caps.wXmin else (0, 65535)
        _ylo, _yhi = (caps.wYmin, caps.wYmax) if caps.wYmax > caps.wYmin else (0, 65535)
        g.sLX = _norm_axis(info.dwXpos, _xlo, _xhi)
        g.sLY = -_norm_axis(info.dwYpos, _ylo, _yhi)
        if caps.wZmax > caps.wZmin:
            g.sRX = _norm_axis(info.dwZpos, caps.wZmin, caps.wZmax)
        if caps.wRmax > caps.wRmin:
            g.sRY = -_norm_axis(info.dwRpos, caps.wRmin, caps.wRmax)
        apply_stick_correction(g)

        _di_btn_count = caps.wNumButtons if caps.wNumButtons else len(_DI_BUTTON_ORDER)
        g.raw_button_count = min(int(_di_btn_count), 32)
        for i in range(g.raw_button_count):
            if info.dwButtons & (1 << i):
                g.wButtons |= _di_raw_button_bit(i)
        for i, bit in enumerate(_DI_BUTTON_ORDER):
            if i >= _di_btn_count:
                break
            if info.dwButtons & (1 << i):
                g.wButtons |= bit
        for j, bit in enumerate(_DI_BUTTON_ORDER_DPAD_EXT):
            i = len(_DI_BUTTON_ORDER) + j
            if i >= _di_btn_count:
                break
            if info.dwButtons & (1 << i):
                g.wButtons |= bit

        has_pov = bool(caps.wCaps & _JOYCAPS_HASPOV)
        if not has_pov and jid not in _dinput_no_pov_logged:
            _dinput_no_pov_logged.add(jid)
        pov = info.dwPOV
        if has_pov and pov != 0xFFFF:
            if pov >= 31500 or pov <= 4500:
                g.wButtons |= _XI_UP
            if 4500 <= pov <= 13500:
                g.wButtons |= _XI_RIGHT
            if 13500 <= pov <= 22500:
                g.wButtons |= _XI_DOWN
            if 22500 <= pov <= 31500:
                g.wButtons |= _XI_LEFT

        _DPAD_AXIS_THRESH = stick_dinput_threshold_raw()
        _di_latch = _dinput_dpad_latch.setdefault(jid, {})
        _di_up, _di_down, _di_left, _di_right = _stick_to_dpad_hysteresis(
            g.sLX, g.sLY, _DPAD_AXIS_THRESH, _di_latch)
        if _di_up:    g.wButtons |= _XI_UP
        if _di_down:  g.wButtons |= _XI_DOWN
        if _di_left:  g.wButtons |= _XI_LEFT
        if _di_right: g.wButtons |= _XI_RIGHT

        if raw_changed_this_frame:
            pass

        out[jid] = g
    return out


_player_slot_of = {}
_slot_player_of = {}
_known_slots     = set()
_reassign_armed_player = None

_last_player_signature = {}


# ============================================================================
# SECTION: Player slot assignment
# ============================================================================


def _slot_signature(slot):
    if isinstance(slot, str) and slot.startswith("di") and slot[2:].isdigit():
        return _dinput_signature_of.get(int(slot[2:]))
    if isinstance(slot, str) and slot.startswith("js:"):
        reader = _linux_joystick_readers.get(slot[3:])
        return getattr(reader, "signature", None) if reader else None
    return None


# ============================================================================
# SECTION: Background controller reader and per-tick snapshot
# ============================================================================
_PAD_COUNTERS = {"joy_reads": 0, "hid_scans": 0, "hid_ms": 0.0}

try:
    import performance_governor as _pad_governor
except Exception:
    _pad_governor = None

_pad_snapshot = {"slots": None, "thread": None}
_pad_poller = None
_pad_poller_lock = threading.Lock()


def _gp_clone(gp):
    out = _GPState()
    for name in _GPState.__slots__:
        try:
            setattr(out, name, getattr(gp, name))
        except AttributeError:
            pass
    return out


def _clone_slots(slots):
    return {k: _gp_clone(v) for k, v in slots.items()}


class _PadPoller:
    """Background thread that reads every controller at GAME_PAD_POLL_HZ and keeps the newest result for the game thread."""

    def __init__(self):
        gov = _pad_governor
        self.period = 1.0 / max(1.0, float(gov.GAME_PAD_POLL_HZ))
        self.max_age = float(gov.GAME_PAD_SNAPSHOT_MAX_AGE_SECONDS)
        self.idle_stop = float(gov.GAME_PAD_POLL_IDLE_STOP_SECONDS)
        self.latest = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.started_at = time.monotonic()
        self.last_touch = self.started_at
        self.cap_logged = False
        self._stats_lock = threading.Lock()
        self._stats = {"scans": 0, "ms": 0.0, "worst": 0.0, "xinput": 0.0, "joy": 0.0}
        self.thread = threading.Thread(target=self._run, name="GamePadPoller", daemon=True)

    def touch(self):
        self.last_touch = time.monotonic()

    def fresh_slots(self):
        latest = self.latest
        if latest is None or time.monotonic() - latest[1] > self.max_age:
            return None
        return latest[0]

    def stats(self):
        with self._stats_lock:
            out = dict(self._stats)
            self._stats["worst"] = 0.0
        out.update(_PAD_COUNTERS)
        return out

    def _run(self):
        global _pad_poller
        first = True
        while not self.stop_event.is_set():
            if time.monotonic() - self.last_touch > self.idle_stop:
                with _pad_poller_lock:
                    if time.monotonic() - self.last_touch > self.idle_stop:
                        self.stop_event.set()
                        if _pad_poller is self:
                            _pad_poller = None
                        break
            t0 = time.perf_counter()
            parts = {}
            try:
                slots = _player_connected_slots_scan(parts=parts)
            except Exception as e:
                slots = None
                _log_warn_throttled("game_pad_poller_scan", "[GAME PAD] background controller read failed: %s", e)
            ms = (time.perf_counter() - t0) * 1000.0
            if slots is not None:
                self.latest = (slots, time.monotonic())
                with self._stats_lock:
                    st = self._stats
                    st["scans"] += 1
                    st["ms"] += ms
                    st["worst"] = max(st["worst"], ms)
                    st["xinput"] += parts.get("xinput", 0.0)
                    st["joy"] += parts.get("joy", 0.0)
                if first:
                    first = False
                    log.info("[GAME PAD] first controller read finished %.0f ms after the reader started "
                             "(read %.1f ms: xinput %.1f, joystick %.1f)",
                             (time.monotonic() - self.started_at) * 1000.0, ms,
                             parts.get("xinput", 0.0), parts.get("joy", 0.0))
                self.ready.set()
            self.stop_event.wait(max(0.001, self.period - (time.perf_counter() - t0)))


def pad_poller_ensure():
    """Starts the background controller reader if it is not running and returns it, or None when it cannot be used."""
    global _pad_poller
    if _pad_governor is None or _is_running_under_wine():
        return None
    with _pad_poller_lock:
        p = _pad_poller
        if p is not None and not p.stop_event.is_set() and p.thread.is_alive():
            p.touch()
            return p
        try:
            p = _PadPoller()
            _pad_poller = p
            p.thread.start()
        except Exception as e:
            _pad_poller = None
            log.warning("[GAME PAD] could not start the background controller reader: %s", e)
            return None
        return p


def pad_poller_stop():
    """Stops the background controller reader."""
    global _pad_poller
    with _pad_poller_lock:
        p = _pad_poller
        _pad_poller = None
    if p is not None:
        p.stop_event.set()


def pad_warmup_done(elapsed_ms):
    """True once the first background controller read has finished, or the load-bar wait cap has passed."""
    p = _pad_poller
    if p is None or p.ready.is_set():
        return True
    if elapsed_ms >= float(_pad_governor.GAME_PAD_WARMUP_MAX_WAIT_SECONDS) * 1000.0:
        if not p.cap_logged:
            p.cap_logged = True
            log.warning("[GAME PAD] load bar stopped waiting for the first controller read after %d ms", elapsed_ms)
        return True
    return False


def pad_poller_stats():
    p = _pad_poller
    return p.stats() if p is not None else None


GAME_FRAME_TIMER.set_pad_stats_source(pad_poller_stats)


def _count_served():
    if _pad_snapshot["thread"] == threading.get_ident():
        GAME_FRAME_TIMER.count("pad_served")


def _current_pad_slots():
    """The tick snapshot on the game thread, else the newest background read, else None."""
    if _pad_snapshot["slots"] is not None and _pad_snapshot["thread"] == threading.get_ident():
        return _pad_snapshot["slots"]
    p = _pad_poller
    return p.fresh_slots() if p is not None else None


def pad_snapshot_begin():
    """Pins one controller read for this game tick: the newest background read, or one direct read if none is fresh."""
    _pad_snapshot["slots"] = None
    _pad_snapshot["thread"] = threading.get_ident()
    p = pad_poller_ensure()
    if p is not None:
        slots = p.fresh_slots()
        if slots is not None:
            _pad_snapshot["slots"] = slots
            GAME_FRAME_TIMER.add("pad_age", (time.monotonic() - p.latest[1]) * 1000.0)
            GAME_FRAME_TIMER.count("pad_pinned")
            return
        GAME_FRAME_TIMER.count("pad_stale")
    t0 = time.perf_counter()
    try:
        slots = _player_connected_slots_scan()
    except Exception:
        return
    _pad_snapshot["slots"] = slots
    ms = (time.perf_counter() - t0) * 1000.0
    GAME_FRAME_TIMER.add("pad_scan", ms)
    GAME_FRAME_TIMER.add("input", ms)
    GAME_FRAME_TIMER.count("pad_scan")


def pad_snapshot_end():
    """Ends the per-tick controller snapshot."""
    _pad_snapshot["slots"] = None
    _pad_snapshot["thread"] = None


def _player_connected_slots():
    slots = _current_pad_slots()
    if slots is not None:
        _count_served()
        return _clone_slots(slots)
    return _player_connected_slots_scan()


def _snapshot_gamepad_state(slots):
    """First XInput pad, else the first pressed legacy pad, else the first legacy pad, from a snapshot."""
    for key in sorted(k for k in slots if isinstance(k, int)):
        return _gp_clone(slots[key])
    legacy = [gp for k, gp in slots.items() if isinstance(k, str) and k.startswith("di")]
    for gp in legacy:
        if gp.wButtons or gp.bLT > 20 or gp.bRT > 20:
            return _gp_clone(gp)
    return _gp_clone(legacy[0]) if legacy else None


def _player_connected_slots_scan(parts=None):
    out = {}
    _t0 = time.perf_counter()
    try:
        out.update(_try_xinput_read_all())
    except Exception:
        pass
    if parts is not None:
        parts["xinput"] = (time.perf_counter() - _t0) * 1000.0
    _t0 = time.perf_counter()
    try:
        for did, gp in _try_dinput_read_all().items():
            # WARNING: treat the common Xbox/XInput aliases as one physical pad, or one controller becomes Player 1 and Player 2.
            sig = _dinput_signature_of.get(did, "") or ""
            sig_lower = sig.lower()
            if out and any(marker in sig_lower for marker in (
                    "xinput", "xbox", "xbox 360", "xbox one",
                    "xbox series", "microsoft")):
                continue
            out[f"di{did}"] = gp
    except Exception:
        pass
    if parts is not None:
        parts["joy"] = (time.perf_counter() - _t0) * 1000.0
    if _is_running_under_wine():
        try:
            for path, reader in _get_all_linux_joystick_readers().items():
                gp = reader.read()
                if gp is not None:
                    out[f"js:{path}"] = gp
        except Exception:
            pass
    return out


def _ensure_player_slots_assigned():
    global _known_slots
    connected = _player_connected_slots()

    for player, slot in list(_player_slot_of.items()):
        if slot not in connected:
            del _player_slot_of[player]
            _slot_player_of.pop(slot, None)
    _known_slots &= set(connected.keys())

    for slot in sorted(connected.keys(), key=lambda s: (0, s) if isinstance(s, int) else (1, s)):
        if slot in _known_slots:
            continue
        if isinstance(slot, str) and slot.startswith("di") and slot[2:].isdigit():
            if int(slot[2:]) not in _dinput_ever_live:
                continue
        _known_slots.add(slot)
        if slot in _slot_player_of:
            continue

        sig = _slot_signature(slot)

        restored_player = None
        if sig is not None:
            for player in range(1, 5):
                if player not in _player_slot_of and _last_player_signature.get(player) == sig:
                    restored_player = player
                    break

        if restored_player is not None:
            _player_slot_of[restored_player] = slot
            _slot_player_of[slot] = restored_player
            log.info(f"[GAMEDECK MULTIPLAYER] Recognized returning controller ({sig}) -- "
                  f"restored to Player {restored_player}")
            continue

        for player in range(1, 5):
            if player not in _player_slot_of:
                _player_slot_of[player] = slot
                _slot_player_of[slot] = player
                if sig is not None:
                    _last_player_signature[player] = sig
                log.info(f"[GAMEDECK MULTIPLAYER] Auto-assigned controller slot {slot} -> Player {player}")
                break


def _libretro_port_for_player(player_num):
    return player_num - 1


def player_is_connected(player_num):
    return player_num in _player_slot_of


def player_gpstate(player_num):
    slot = _player_slot_of.get(player_num)
    if slot is None:
        return None
    return _player_connected_slots().get(slot)


def player1_gpstate():
    slot = _player_slot_of.get(1)
    if slot is not None:
        gp = _player_connected_slots().get(slot)
        if gp is not None:
            return gp
    return _read_gamepad_state()


def _player_reassign_arm(player_num):
    global _reassign_armed_player
    _reassign_armed_player = player_num
    log.info(f"[GAMEDECK MULTIPLAYER] Reassign armed for Player {player_num} -- press a button on the controller to use")


def _player_reassign_cancel():
    global _reassign_armed_player
    _reassign_armed_player = None


def _player_reassign_poll():
    global _reassign_armed_player
    if _reassign_armed_player is None:
        return None
    player_num = _reassign_armed_player
    connected = _player_connected_slots()
    for slot, gp in connected.items():
        if gp.wButtons or gp.bLT > 40 or gp.bRT > 40:
            old_player = _slot_player_of.get(slot)
            if old_player is not None and old_player != player_num:
                _player_slot_of.pop(old_player, None)
            old_slot = _player_slot_of.get(player_num)
            if old_slot is not None:
                _slot_player_of.pop(old_slot, None)
            _player_slot_of[player_num] = slot
            _slot_player_of[slot] = player_num
            _known_slots.add(slot)
            sig = _slot_signature(slot)
            if sig is not None:
                _last_player_signature[player_num] = sig
            _reassign_armed_player = None
            log.info(f"[GAMEDECK MULTIPLAYER] Player {player_num} reassigned -> controller slot {slot}")
            return player_num
    return None


_menu_nav_slot = None
_menu_reassign_armed = False


def _menu_nav_gpstate():
    if _menu_nav_slot is not None:
        gp = _player_connected_slots().get(_menu_nav_slot)
        if gp is not None:
            return gp
    return _read_gamepad_state()


def menu_nav_connected():
    return _menu_nav_gpstate() is not None


def _menu_reassign_arm():
    global _menu_reassign_armed
    _menu_reassign_armed = True
    log.info("[GAMEDECK MULTIPLAYER] Reassign armed for MENU nav -- press a button on the controller to use")


def _menu_reassign_cancel():
    global _menu_reassign_armed
    _menu_reassign_armed = False


def _menu_reassign_poll():
    global _menu_reassign_armed, _menu_nav_slot
    if not _menu_reassign_armed:
        return False
    connected = _player_connected_slots()
    for slot, gp in connected.items():
        if gp.wButtons or gp.bLT > 40 or gp.bRT > 40:
            _menu_nav_slot = slot
            _menu_reassign_armed = False
            log.info(f"[GAMEDECK MULTIPLAYER] MENU nav reassigned -> controller slot {slot}")
            return True
    return False


# ============================================================================
# SECTION: Gamepad input: Linux joystick
# ============================================================================


_JS_EVENT_FMT    = "<Ihbb"
_JS_EVENT_SIZE   = struct.calcsize(_JS_EVENT_FMT)
_JS_EVENT_BUTTON = 0x01
_JS_EVENT_AXIS   = 0x02
_JS_EVENT_INIT   = 0x80

_JS_BUTTON_MAP = {
    0: _XI_A, 1: _XI_B, 2: _XI_X, 3: _XI_Y,
    4: _XI_LB, 5: _XI_RB, 6: _XI_BACK, 7: _XI_START,
    8: _XI_LS, 9: _XI_RS,
    10: _XI_UP, 11: _XI_DOWN, 12: _XI_LEFT, 13: _XI_RIGHT,
}
_JS_AXIS_MAP         = {0: "sLX", 1: "sLY", 3: "sRX", 4: "sRY"}
_JS_TRIGGER_AXIS_MAP = {2: "bLT", 5: "bRT"}
_JS_HAT_X_AXIS = 6
_JS_HAT_Y_AXIS = 7


# VALUES CHANGE HERE
_JS_DEAD_TIMEOUT = 5.0

_gamepad_perm_denied_path = None


def gamepad_permission_denied_path():
    return _gamepad_perm_denied_path

def _candidate_joystick_paths():
    try:
        entries = os.listdir("Z:\\dev\\input")
        nodes = sorted(
            (e for e in entries if re.fullmatch(r"js\d+", e)),
            key=lambda e: int(e[2:]),
        )
        if nodes:
            return [f"Z:\\dev\\input\\{n}" for n in nodes]
    except Exception:
        pass
    return [f"Z:\\dev\\input\\js{i}" for i in range(8)]


def _linux_js_serial(path):
    try:
        m = re.search(r"js(\d+)$", path)
        if not m:
            return None
        js_num = m.group(1)
        base = f"Z:\\sys\\class\\input\\js{js_num}\\device"
        cur = base
        for _ in range(5):
            try:
                with open(cur + "\\serial", "r") as f:
                    s = f.read().strip()
                    if s:
                        return s
            except Exception:
                pass
            cur += "\\.."
        return None
    except Exception:
        return None


def _linux_js_port_path(path):
    try:
        m = re.search(r"js(\d+)$", path)
        if not m:
            return None
        js_num = m.group(1)
        target = os.readlink(f"Z:\\sys\\class\\input\\js{js_num}\\device")
        m2 = re.search(r"/(\d+-[\d.]+)(?::[\d.]+)?/", target.replace("\\", "/") + "/")
        return m2.group(1) if m2 else None
    except Exception:
        return None


def _linux_js_sysfs_signature(path):
    try:
        m = re.search(r"js(\d+)$", path)
        if not m:
            return None
        js_num = m.group(1)
        base = f"Z:\\sys\\class\\input\\js{js_num}\\device"
        with open(base + "\\id\\vendor", "r") as f:
            vendor = f.read().strip()
        with open(base + "\\id\\product", "r") as f:
            product = f.read().strip()
        name = ""
        try:
            with open(base + "\\name", "r") as f:
                name = f.read().strip()
        except Exception:
            pass
        base_sig = f"linux:{vendor}:{product}:{name}"
        serial = _linux_js_serial(path)
        if serial:
            return f"{base_sig}:serial:{serial}"
        port = _linux_js_port_path(path)
        if port:
            return f"{base_sig}:port:{port}"
        return base_sig
    except Exception:
        return None


class _LinuxJoystickReader:

    def __init__(self, path):
        self._path          = path
        self._fd            = None
        self._state         = _GPState()
        self._failed        = False
        self._perm_denied   = False
        self._opened_at     = None
        self._last_event_at = None
        self.signature       = _linux_js_sysfs_signature(path)
        self._open()

    def _open(self):
        global _gamepad_perm_denied_path
        try:
            _o_nonblock = getattr(os, "O_NONBLOCK", 0o4000)
            self._fd = os.open(self._path, os.O_RDONLY | _o_nonblock)
            self._opened_at = time.monotonic()
            log.info(f"[GAMEPAD] Linux joystick fallback opened {self._path}")
            _gamepad_perm_denied_path = None
        except PermissionError as e:
            self._fd = None
            self._failed = True
            self._perm_denied = True
            log.warning("Linux joystick fallback found %s but couldn't read it "
                        "(permission denied: %s)", self._path, e)
            _gamepad_perm_denied_path = self._path
        except Exception as e:
            self._fd = None
            self._failed = True
            log.warning(f"[GAMEPAD] Linux joystick fallback could not open {self._path}: {e}")

    def seems_dead(self, timeout=_JS_DEAD_TIMEOUT):
        if self._fd is None or self._last_event_at is not None:
            return False
        return self._opened_at is not None and (time.monotonic() - self._opened_at) > timeout

    def read(self):
        if self._fd is None:
            return None
        try:
            saw_event = False
            while True:
                try:
                    chunk = os.read(self._fd, _JS_EVENT_SIZE)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if len(chunk) < _JS_EVENT_SIZE:
                    break
                saw_event = True
                _t, value, ev_type, number = struct.unpack(_JS_EVENT_FMT, chunk)
                ev_type &= ~_JS_EVENT_INIT
                if ev_type == _JS_EVENT_BUTTON:
                    bit = _JS_BUTTON_MAP.get(number)
                    if bit:
                        if value:
                            self._state.wButtons |= bit
                        else:
                            self._state.wButtons &= ~bit
                elif ev_type == _JS_EVENT_AXIS:
                    if number in _JS_AXIS_MAP:
                        setattr(self._state, _JS_AXIS_MAP[number], value)
                    elif number in _JS_TRIGGER_AXIS_MAP:
                        scaled = max(0, min(255, int((value + 32768) / 65535 * 255)))
                        setattr(self._state, _JS_TRIGGER_AXIS_MAP[number], scaled)
                    elif number == _JS_HAT_X_AXIS:
                        self._state.wButtons &= ~(_XI_LEFT | _XI_RIGHT)
                        if value < -1000: self._state.wButtons |= _XI_LEFT
                        elif value > 1000: self._state.wButtons |= _XI_RIGHT
                    elif number == _JS_HAT_Y_AXIS:
                        self._state.wButtons &= ~(_XI_UP | _XI_DOWN)
                        if value < -1000: self._state.wButtons |= _XI_UP
                        elif value > 1000: self._state.wButtons |= _XI_DOWN
            if saw_event:
                self._last_event_at = time.monotonic()
        except Exception as e:
            _log_warn_throttled("linux_joystick_read", f"[GAMEPAD] Linux joystick fallback read error: {e}")
            return None
        corrected = _GPState()
        for name in _GPState.__slots__:
            setattr(corrected, name, getattr(self._state, name))
        return apply_stick_correction(corrected)


_linux_joystick_readers    = {}
_linux_joystick_dead_paths = set()
_linux_joystick_none_found_logged = False

def _get_all_linux_joystick_readers():
    global _linux_joystick_none_found_logged

    for path, reader in list(_linux_joystick_readers.items()):
        if reader.seems_dead():
            log.warning(f"[GAMEPAD] {path} opened but produced no events after "
                  f"{_JS_DEAD_TIMEOUT:.0f}s -- likely the wrong device node, dropping it.")
            del _linux_joystick_readers[path]
            _linux_joystick_dead_paths.add(path)

    all_candidates = _candidate_joystick_paths()
    for path in all_candidates:
        if path in _linux_joystick_readers or path in _linux_joystick_dead_paths:
            continue
        candidate = _LinuxJoystickReader(path)
        if candidate._failed:
            _linux_joystick_dead_paths.add(path)
        else:
            _linux_joystick_readers[path] = candidate

    if (not _linux_joystick_readers and not _linux_joystick_none_found_logged
            and all(p in _linux_joystick_dead_paths for p in all_candidates)):
        log.warning("[GAMEPAD] No usable Linux joystick device found under Z:\\dev\\input -- "
              "falling back to keyboard input only for this session. If a controller is "
              "plugged in, check 'ls -l /dev/input/js*' and group membership on the Linux host.")
        _linux_joystick_none_found_logged = True

    return _linux_joystick_readers


def _get_linux_joystick_reader():
    readers = _get_all_linux_joystick_readers()
    return next(iter(readers.values()), None)


# ============================================================================
# SECTION: Stick deadzone settings & maths
# ============================================================================

STICK_MAX = 32767.0
STICK_SETTING_NAMES = ("deadzone", "sensitivity", "axis_threshold")


class StickSettings(collections.namedtuple("StickSettings", "enabled deadzone sensitivity axis_threshold")):
    """The stick settings in use: the switch, then the deadzone, sensitivity and axis threshold as fractions (1.0 = 100 percent)."""


def _stick_percent(config, name, enabled):
    key, default, low, high, _step = _gov.STICK_DZ_SPECS[name]
    if not enabled:
        return default
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def stick_settings_from_config(config):
    """Returns the StickSettings a config holds; with the switch off every value is its default."""
    enabled = bool(config.get(_gov.STICK_DZ_ENABLED_KEY, _gov.STICK_DZ_ENABLED_DEFAULT))
    return StickSettings(
        enabled,
        _stick_percent(config, "deadzone", enabled) / 100.0,
        _stick_percent(config, "sensitivity", enabled) / 100.0,
        _stick_percent(config, "axis_threshold", enabled) / 100.0,
    )


_stick_current = stick_settings_from_config({})


def apply_stick_config(config):
    """Makes the settings of a config the ones every pad read uses, and returns them."""
    global _stick_current
    _stick_current = stick_settings_from_config(config)
    return _stick_current


def current_stick_settings():
    """Returns the StickSettings in use."""
    return _stick_current


def reset_stick_defaults(config):
    """Writes every stick setting's default into the config; the switch is left as it is."""
    for name in STICK_SETTING_NAMES:
        key, default, _low, _high, _step = _gov.STICK_DZ_SPECS[name]
        config[key] = default


def correct_stick_pair(x, y, deadzone, sensitivity):
    """Returns the raw (x, y) of one stick after the circular deadzone with rescale and then the sensitivity, each kept inside -32767..32767."""
    limit = int(STICK_MAX)
    if deadzone <= 0.0 and sensitivity == 1.0:
        return max(-limit, min(limit, int(x))), max(-limit, min(limit, int(y)))
    fx = max(-1.0, min(1.0, x / STICK_MAX))
    fy = max(-1.0, min(1.0, y / STICK_MAX))
    magnitude = math.hypot(fx, fy)
    if deadzone > 0.0:
        if magnitude <= deadzone:
            return 0, 0
        scaled = min(1.0, (magnitude - deadzone) / (1.0 - deadzone))
        factor = scaled / magnitude
        fx *= factor
        fy *= factor
    rx = int(fx * STICK_MAX)
    ry = int(fy * STICK_MAX)
    if sensitivity != 1.0:
        rx = int(rx * sensitivity)
        ry = int(ry * sensitivity)
    return max(-limit, min(limit, rx)), max(-limit, min(limit, ry))


def correct_stick_with_current(x, y):
    """Returns the raw (x, y) of one stick after the settings in use."""
    settings = _stick_current
    return correct_stick_pair(x, y, settings.deadzone, settings.sensitivity)


def apply_stick_correction(g):
    """Keeps a pad state's raw stick values in rawLX to rawRY and replaces its sticks with the values after the stick settings in use; with the switch off the sticks are left as read."""
    g.rawLX, g.rawLY, g.rawRX, g.rawRY = g.sLX, g.sLY, g.sRX, g.sRY
    if _stick_current.enabled:
        g.sLX, g.sLY = correct_stick_with_current(g.sLX, g.sLY)
        g.sRX, g.sRY = correct_stick_with_current(g.sRX, g.sRY)
    return g


def stick_dinput_threshold_raw():
    """Returns the stick-to-D-pad threshold of a DirectInput pad in raw stick units; with the switch off it is the fixed value the app used before the menu."""
    if not _stick_current.enabled:
        return int(_gov.STICK_DZ_OFF_DINPUT_THRESHOLD)
    return stick_axis_threshold_raw()


def stick_axis_threshold_raw():
    """Returns the stick-to-D-pad threshold in raw stick units; with the switch off it is the fixed value the app used before the menu."""
    if not _stick_current.enabled:
        return int(_gov.STICK_DZ_OFF_DPAD_THRESHOLD)
    return int(_stick_current.axis_threshold * STICK_MAX)


def stick_switch_on(config):
    """Returns True when the stick deadzone switch is on in the config."""
    return bool(config.get(_gov.STICK_DZ_ENABLED_KEY, _gov.STICK_DZ_ENABLED_DEFAULT))


def toggle_stick_switch(config):
    """Flips the stick deadzone switch in the config and returns the new state."""
    config[_gov.STICK_DZ_ENABLED_KEY] = not stick_switch_on(config)
    return config[_gov.STICK_DZ_ENABLED_KEY]


def stick_setting_percent(config, name):
    """Returns the saved percent of one stick setting, repaired and clamped, whatever the switch says."""
    return _stick_percent(config, name, True)


def adjust_stick_setting(config, name, direction):
    """Moves one stick setting a step down (direction -1) or up (direction 1) inside its range, saves it in the config and returns the new percent."""
    key, _default, low, high, step = _gov.STICK_DZ_SPECS[name]
    value = max(low, min(high, stick_setting_percent(config, name) + step * (1 if direction > 0 else -1)))
    config[key] = value
    return value


_stick_test_until = 0.0


def mark_stick_test_open():
    """Marks the Stick Deadzone screen as open for the next few frames; the menu-nav thread ignores the sticks while it is."""
    global _stick_test_until
    _stick_test_until = time.monotonic() + _gov.STICK_DZ_TEST_HOLD_SECONDS


def stick_test_open():
    """Returns True while the Stick Deadzone screen is open."""
    return time.monotonic() < _stick_test_until


def stick_test_sticks():
    """Returns ((left x, left y), (right x, right y)) of player 1's pad, or None when there is no pad."""
    try:
        gp = player1_gpstate()
    except Exception:
        return None
    if gp is None:
        return None
    return (gp.rawLX, gp.rawLY), (gp.rawRX, gp.rawRY)


def stick_direction_names(x, y, threshold):
    """Returns the names of the D-pad directions a stick pair fires at a threshold."""
    names = []
    if y > threshold:
        names.append("UP")
    if y < -threshold:
        names.append("DOWN")
    if x < -threshold:
        names.append("LEFT")
    if x > threshold:
        names.append("RIGHT")
    return names


# ============================================================================
# SECTION: Stick deadzone screen
# ============================================================================


def _draw_stick_test(panel, center_x, center_y, sticks, ui_color, font, config):
    radius = int(_gov.STICK_DZ_TEST_RADIUS)
    settings = stick_settings_from_config(config)
    threshold = settings.axis_threshold * STICK_MAX if settings.enabled else float(_gov.STICK_DZ_OFF_DPAD_THRESHOLD)
    for index, title in enumerate(("LEFT", "RIGHT")):
        cx = center_x - 350 + radius + index * 360
        raw = sticks[index] if sticks else (0, 0)
        out = correct_stick_pair(raw[0], raw[1], settings.deadzone, settings.sensitivity)
        pygame.draw.circle(panel, (25, 38, 62), (cx, center_y), radius)
        pygame.draw.circle(panel, (90, 90, 110), (cx, center_y), radius, 1)
        if settings.deadzone > 0.0:
            pygame.draw.circle(panel, (200, 160, 40), (cx, center_y), max(1, int(radius * settings.deadzone)), 1)
        line = int(radius * threshold / STICK_MAX)
        pygame.draw.line(panel, (70, 110, 160), (cx - line, center_y - radius), (cx - line, center_y + radius), 1)
        pygame.draw.line(panel, (70, 110, 160), (cx + line, center_y - radius), (cx + line, center_y + radius), 1)
        pygame.draw.line(panel, (70, 110, 160), (cx - radius, center_y - line), (cx + radius, center_y - line), 1)
        pygame.draw.line(panel, (70, 110, 160), (cx - radius, center_y + line), (cx + radius, center_y + line), 1)
        for pair, color, size in ((raw, (150, 150, 165), 4), (out, ui_color, 5)):
            fx, fy = pair[0] / STICK_MAX, pair[1] / STICK_MAX
            magnitude = math.hypot(fx, fy)
            if magnitude > 1.0:
                fx, fy = fx / magnitude, fy / magnitude
            pygame.draw.circle(panel, color, (cx + int(fx * radius), center_y - int(fy * radius)), size)
        text_x = cx + radius + 14
        names = " ".join(stick_direction_names(out[0], out[1], threshold)) or "-"
        for row, (text, color) in enumerate((
                ("%s STICK" % title, (180, 180, 180)),
                ("RAW  %6d %6d" % raw, (150, 150, 165)),
                ("OUT  %6d %6d" % out, ui_color),
                ("FIRES  %s" % names, (180, 180, 180)))):
            panel.blit(font.render(text, True, color), (text_x, center_y - radius + 6 + row * 22))


def render_stick_deadzone_ui(surface, rect, ui_color, db, cursor, rows, sticks=None):
    """Draws the Stick Deadzone screen into rect: the switch, the setting rows and Reset to Defaults while the switch is on, and the Close button; cursor is the index into rows and sticks the pair from stick_test_sticks."""
    import colorsys
    target_x, target_y, target_w, target_h = rect
    base_w, base_h = 996, 525
    panel = pygame.Surface((base_w, base_h), pygame.SRCALPHA)
    config = db.config if db is not None else {}
    hue = config.get("theme_bg_hue", 220) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.6, 0.15)
    opacity = int((config.get("menu_opacity", 50) / 100.0) * 255)
    bg_layer = pygame.Surface((base_w, base_h), pygame.SRCALPHA)
    bg_layer.fill((int(r * 255), int(g * 255), int(b * 255), opacity))
    panel.blit(bg_layer, (0, 0))

    f_title = _cached_font("Courier New", 24, bold=True)
    f_opt = _cached_font("Courier New", 19, bold=True)
    f_small = _cached_font("Courier New", 14, bold=True)
    center_x = base_w // 2
    enabled = stick_switch_on(config)
    labels = {
        _nav.STICK_DZ_DEADZONE: "ANALOG DEADZONE",
        _nav.STICK_DZ_SENSITIVITY: "ANALOG SENSITIVITY",
        _nav.STICK_DZ_AXIS: "BUTTON AXIS THRESHOLD",
    }
    selected = _nav.stick_dz_cell(rows, cursor)

    def option_color(cell):
        return ui_color if selected == cell else (180, 180, 180)

    title = f_title.render("STICK DEADZONE", True, ui_color)
    panel.blit(title, (center_x - title.get_width() // 2, 20))

    row_y = 90
    row_gap = 56
    lbl = f_opt.render("STICK DEADZONE", True, option_color(_nav.STICK_DZ_SWITCH))
    sw_w, sw_h = 55, 24
    group_w = lbl.get_width() + 14 + sw_w
    left = center_x - group_w // 2
    panel.blit(lbl, (left, row_y))
    sw_x, sw_y = left + lbl.get_width() + 14, row_y + lbl.get_height() // 2 - sw_h // 2
    pygame.draw.rect(panel, (40, 167, 69) if enabled else (45, 65, 100), (sw_x, sw_y, sw_w, sw_h), border_radius=12)
    pygame.draw.circle(panel, (255, 255, 255), (sw_x + sw_w - 14 if enabled else sw_x + 14, sw_y + sw_h // 2), 9)
    if selected == _nav.STICK_DZ_SWITCH:
        pygame.draw.rect(panel, ui_color, (sw_x, sw_y, sw_w, sw_h), SELECTION_BORDER_WIDTH, border_radius=12)

    if enabled:
        bar_w, bar_h = 260, 10
        label_x = center_x - 330
        bar_x = center_x + 10
        for i, name in enumerate(_nav.STICK_DZ_SETTINGS):
            y_pos = row_y + row_gap * (i + 1)
            _key, _default, low, high, _step = _gov.STICK_DZ_SPECS[name]
            value = stick_setting_percent(config, name)
            text = f_opt.render(labels[name], True, option_color(name))
            panel.blit(text, (label_x, y_pos))
            bar_y = y_pos + text.get_height() // 2 - bar_h // 2
            pygame.draw.rect(panel, (45, 65, 100), (bar_x, bar_y, bar_w, bar_h), border_radius=5)
            fill = int(bar_w * (value - low) / float(high - low))
            pygame.draw.rect(panel, (40, 167, 69), (bar_x, bar_y, fill, bar_h), border_radius=5)
            pygame.draw.circle(panel, (255, 255, 255), (bar_x + fill, bar_y + bar_h // 2), 9)
            if selected == name:
                pygame.draw.circle(panel, ui_color, (bar_x + fill, bar_y + bar_h // 2), 9, SELECTION_BORDER_WIDTH)
            number = f_opt.render("%d%%" % value, True, option_color(name))
            panel.blit(number, (bar_x + bar_w + 22, y_pos))
        reset = f_opt.render("[ RESET TO DEFAULTS ]", True, option_color(_nav.STICK_DZ_RESET))
        panel.blit(reset, (center_x - reset.get_width() // 2, row_y + row_gap * (len(_nav.STICK_DZ_SETTINGS) + 1)))
        hint = f_small.render("LEFT / RIGHT change the highlighted value", True, (120, 120, 140))
        panel.blit(hint, (center_x - hint.get_width() // 2, 54))
        _draw_stick_test(panel, center_x, 408, sticks, ui_color, f_small, config)

    off_red = (220, 53, 69)
    close_w, close_h = 110, 40
    close_x = base_w - close_w - 25
    close_y = base_h - 52
    pygame.draw.rect(panel, off_red, (close_x, close_y, close_w, close_h), border_radius=4)
    if selected == _nav.CLOSE:
        pygame.draw.rect(panel, ui_color, (close_x, close_y, close_w, close_h), SELECTION_BORDER_WIDTH, border_radius=4)
    close_lbl = f_opt.render("Close", True, (255, 255, 255))
    panel.blit(close_lbl, (close_x + close_w // 2 - close_lbl.get_width() // 2, close_y + close_h // 2 - close_lbl.get_height() // 2))

    if target_w > 0 and target_h > 0:
        surface.blit(pygame.transform.smoothscale(panel, (target_w, target_h)), (target_x, target_y))


# ============================================================================
# SECTION: Stick deadzone & unified gamepad read
# ============================================================================


def _stick_to_dpad_hysteresis(sLX, sLY, dead, latch):
    """Convert raw stick X/Y values into up/down/left/right booleans with hysteresis."""
    release = dead * _gov.STICK_DZ_DPAD_RELEASE_FRACTION
    up    = (sLY >  dead) or (latch.get("up", False)    and sLY >  release)
    down  = (sLY < -dead) or (latch.get("down", False)  and sLY < -release)
    left  = (sLX < -dead) or (latch.get("left", False)  and sLX < -release)
    right = (sLX >  dead) or (latch.get("right", False) and sLX >  release)
    latch["up"], latch["down"], latch["left"], latch["right"] = up, down, left, right
    return up, down, left, right


def _read_gamepad_state():
    if not _is_running_under_wine():
        _slots = _current_pad_slots()
        if _slots is not None:
            _count_served()
            return _snapshot_gamepad_state(_slots)
    gp = _try_xinput_read()
    if gp is not None:
        return gp
    if _is_running_under_wine():
        reader = _get_linux_joystick_reader()
        if reader is not None:
            return reader.read()
    else:
        di = _try_dinput_read_all()
        if di:
            for gp_candidate in di.values():
                if gp_candidate.wButtons or gp_candidate.bLT > 20 or gp_candidate.bRT > 20:
                    return gp_candidate
            return next(iter(di.values()))
    return None


# ============================================================================
# SECTION: Console button definitions & menu-nav mapping
# ============================================================================


CONSOLE_BUTTON_DEFS = {
    "GB": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["A", "B", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.234, 0.521),
            "Left":   (0.072, 0.633),
            "Right":  (0.397, 0.633),
            "Down":   (0.234, 0.744),
            "A":      (0.832, 0.613),
            "B":      (0.680, 0.661),
            "Start":  (0.537, 0.834),
            "Select": (0.389, 0.835),
        },
        "default_map": {
            "A":      0x5A,
            "B":      0x58,
            "Start":  0x0D,
            "Select": 0x08,
            "Up":     0x26,  "Down":  0x28,
            "Left":   0x25,  "Right": 0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "B", _XI_B: "A",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },
    "GBC": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["A", "B", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.247, 0.527),
            "Down":   (0.247, 0.705),
            "Left":   (0.112, 0.616),
            "Right":  (0.381, 0.616),
            "A":      (0.808, 0.599),
            "B":      (0.649, 0.638),
            "Select": (0.416, 0.791),
            "Start":  (0.563, 0.791),
        },
        "default_map": {
            "A":      0x5A, "B":      0x58,
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "B", _XI_B: "A",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    "GBA": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "L", "R", "Start", "Select"],
        "left_btns":  ["L", "Up", "Left", "Right", "Down"],
        "right_btns": ["R", "A", "B", "Start", "Select"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "L", "R", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.145, 0.280),
            "Down":   (0.145, 0.570),
            "Left":   (0.066, 0.424),
            "Right":  (0.225, 0.424),
            "A":      (0.899, 0.413),
            "B":      (0.812, 0.493),
            "Start":  (0.158, 0.665),
            "Select": (0.158, 0.772),
            "L":      (0.130, 0.062),
            "R":      (0.857, 0.056),
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
            _XI_A: "B", _XI_B: "A",
            _XI_LB: "L", _XI_RB: "R",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    "NES": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Start", "Select"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["A", "B", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "A", "B", "Start", "Select"],
        "hotspots": {
            "Up":     (0.279, 0.364),
            "Down":   (0.279, 0.673),
            "Left":   (0.195, 0.519),
            "Right":  (0.364, 0.519),
            "B":      (0.689, 0.575),
            "A":      (0.782, 0.575),
            "Select": (0.456, 0.620),
            "Start":  (0.557, 0.620),
        },
        "default_map": {
            "A":      0x5A, "B":      0x58,
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "B", _XI_B: "A",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    "SNES": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "X", "Y", "L", "R", "Start", "Select"],
        "left_btns":  ["L", "Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["R", "X", "Y", "A", "B", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "L", "R", "A", "B", "X", "Y", "Start", "Select"],
        "hotspots": {
            "Up":     (0.251, 0.338),
            "Left":   (0.152, 0.486),
            "Right":  (0.350, 0.486),
            "Down":   (0.251, 0.635),
            "A":      (0.820, 0.546),
            "B":      (0.738, 0.622),
            "X":      (0.779, 0.402),
            "Y":      (0.680, 0.496),
            "L":      (0.260, 0.177),
            "R":      (0.740, 0.177),
            "Start":  (0.536, 0.593),
            "Select": (0.447, 0.593),
        },
        "default_map": {
            "A":      0x58, "B":      0x5A,
            "X":      0x53, "Y":      0x41,
            "L":      0x51, "R":      0x57,
            "Start":  0x0D, "Select": 0x08,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "B", _XI_B: "A", _XI_X: "Y", _XI_Y: "X",
            _XI_LB: "L", _XI_RB: "R",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    "GENESIS": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "C", "X", "Y", "Z", "Start"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Start"],
        "right_btns": ["X", "Y", "Z", "A", "B", "C",],
        "btn_seq":    ["Up", "Left", "Right", "Down", "Start", "A", "B", "C", "X", "Y", "Z"],
        "hotspots": {
            "Up":     (0.249, 0.335),
            "Left":   (0.150, 0.475),
            "Right":  (0.348, 0.475),
            "Down":   (0.249, 0.625),
            "A":      (0.697, 0.666),
            "B":      (0.772, 0.607),
            "C":      (0.854, 0.555),
            "X":      (0.635, 0.477),
            "Y":      (0.725, 0.418),
            "Z":      (0.809, 0.363),
            "Start":  (0.501, 0.525),
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

    "N64": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Z", "L", "R", "CUp", "CDown", "CLeft", "CRight", "Start"],
        "left_btns":  ["L", "Up", "Left", "Right", "Down", "Start", "Z"],
        "right_btns": ["R", "CUp", "CLeft", "CRight", "CDown", "A", "B"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "A", "B", "Z", "L", "R", "CUp", "CDown", "CLeft", "CRight", "Start"],
        "hotspots": {
            "Up":     (0.271, 0.245),
            "Left":   (0.216, 0.323),
            "Right":  (0.327, 0.323),
            "Down":   (0.271, 0.401),
            "A":      (0.676, 0.435),
            "B":      (0.630, 0.360),
            "Z":      (0.380, 0.592),
            "L":      (0.271, 0.120),
            "R":      (0.736, 0.120),
            "CUp":    (0.740, 0.243),
            "CDown":  (0.736, 0.384),
            "CLeft":  (0.691, 0.309),
            "CRight": (0.783, 0.314),
            "Start":  (0.499, 0.339),
        },
        "joy_hotspots": {
            "JoyUp":    (0.499, 0.446),
            "JoyDown":  (0.499, 0.632),
            "JoyLeft":  (0.437, 0.539),
            "JoyRight": (0.561, 0.539),
        },
        "default_map": {
            "A":      0x5A,
            "B":      0x58,
            "Z":      0,
            "L":      0x41,
            "R":      0x53,
            "CUp":    0x49,
            "CDown":  0x4B,
            "CLeft":  0x43,
            "CRight": 0x4C,
            "Start":  0x0D,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_X: "B",
            _XI_LB: "L", _XI_RB: "R", _XI_LT: "Z",
            _XI_START: "Start",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

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
            _XI_A: "Cross", _XI_B: "Circle", _XI_X: "Square", _XI_Y: "Triangle",
            _XI_LB: "L1", _XI_RB: "R1",
            _XI_LT: "L2", _XI_RT: "R2",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    "MENU": {
        "buttons":    ["Up", "Down", "Left", "Right", "Cross", "Circle", "Square", "Triangle", "L1", "L2", "R1", "R2", "Start", "Select"],
        "left_btns":  ["L2", "L1", "Up", "Left", "Right", "Down", "Select"],
        "right_btns": ["R2", "R1", "Triangle", "Circle", "Square", "Cross", "Start"],
        "btn_seq":    ["Up", "Left", "Right", "Down", "Cross", "Circle",
                        "Square", "Triangle", "L1", "R1", "L2", "R2", "Start", "Select"],
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
            "Cross":  _nav.action_key(_nav.ACCEPT),
            "Circle": _nav.action_key(_nav.BACK),
            "Up":     _nav.action_key(_nav.UP),
            "Down":   _nav.action_key(_nav.DOWN),
            "Left":   _nav.action_key(_nav.LEFT),
            "Right":  _nav.action_key(_nav.RIGHT),
            "Square": 0, "Triangle": 0, "L1": 0, "L2": 0, "R1": 0, "R2": 0,
            "Start":  0, "Select":  0,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "Cross", _XI_B: "Circle", _XI_X: "Square", _XI_Y: "Triangle",
            _XI_LB: "L1", _XI_RB: "R1",
            _XI_LT: "L2", _XI_RT: "R2",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

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

    "NGP": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Option"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["Option", "B", "A"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "A", "B", "Option"],
        "hotspots": {
            "Up":     (0.139, 0.264),
            "Left":   (0.049, 0.425),
            "Right":  (0.228, 0.425),
            "Down":   (0.139, 0.582),
            "A":      (0.824, 0.499),
            "B":      (0.891, 0.378),
            "Option": (0.885, 0.174),
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

    "A2600": {
        "buttons":    ["Up", "Down", "Left", "Right", "A"],
        "left_btns":  ["A", "Left", "Down"],
        "right_btns": ["Up", "Right"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "A"],
        "hotspots": {
            "Up":     (0.500, 0.459),
            "Left":   (0.305, 0.552),
            "Right":  (0.696, 0.552),
            "Down":   (0.500, 0.644),
            "A":      (0.186, 0.512),
        },
        "default_map": {
            "A":      0x5A,
            "Up":     0x26, "Down":   0x28,
            "Left":   0x25, "Right":  0x27,
            "stick_as_dpad": False,
        },
        "xinput_map": {
            _XI_A: "A", _XI_B: "A",
            _XI_UP: "Up", _XI_DOWN: "Down", _XI_LEFT: "Left", _XI_RIGHT: "Right",
        },
    },

    "NGPC": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "Option"],
        "left_btns":  ["Up", "Left", "Right", "Down"],
        "right_btns": ["Option", "B", "A"],
        "btn_seq":    ["Up", "Down", "Left", "Right", "A", "B", "Option"],
        "hotspots": {
            "Up":     (0.139, 0.264),
            "Left":   (0.049, 0.425),
            "Right":  (0.228, 0.425),
            "Down":   (0.139, 0.582),
            "A":      (0.824, 0.499),
            "B":      (0.891, 0.378),
            "Option": (0.885, 0.174),
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

    "32X": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "C", "X", "Y", "Z", "Start"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Start"],
        "right_btns": ["X", "Y", "Z", "A", "B", "C",],
        "btn_seq":    ["Up", "Left", "Right", "Down", "Start", "A", "B", "C", "X", "Y", "Z"],
        "hotspots": {
            "Up":     (0.249, 0.335),
            "Left":   (0.150, 0.475),
            "Right":  (0.348, 0.475),
            "Down":   (0.249, 0.625),
            "A":      (0.697, 0.666),
            "B":      (0.772, 0.607),
            "C":      (0.854, 0.555),
            "X":      (0.635, 0.477),
            "Y":      (0.725, 0.418),
            "Z":      (0.809, 0.363),
            "Start":  (0.501, 0.525),
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

    "MASTERSYSTEM": {
        "buttons":    ["Up", "Down", "Left", "Right", "A", "B", "C", "X", "Y", "Z", "Start"],
        "left_btns":  ["Up", "Left", "Right", "Down", "Start"],
        "right_btns": ["X", "Y", "Z", "A", "B", "C",],
        "btn_seq":    ["Up", "Left", "Right", "Down", "Start", "A", "B", "C", "X", "Y", "Z"],
        "hotspots": {
            "Up":     (0.249, 0.335),
            "Left":   (0.150, 0.475),
            "Right":  (0.348, 0.475),
            "Down":   (0.249, 0.625),
            "A":      (0.697, 0.666),
            "B":      (0.772, 0.607),
            "C":      (0.854, 0.555),
            "X":      (0.635, 0.477),
            "Y":      (0.725, 0.418),
            "Z":      (0.809, 0.363),
            "Start":  (0.501, 0.525),
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
}

CONSOLE_BUTTON_DEFS["DVD"] = {
    "buttons":    [],
    "left_btns":  [],
    "right_btns": [],
    "btn_seq":    [],
    "hotspots":   {},
    "default_map": {"stick_as_dpad": False},
    "xinput_map":  {},
}


def _get_console_def(console):
    """Return the button-def dict for *console*, falling back to GBA."""
    return CONSOLE_BUTTON_DEFS.get(console, CONSOLE_BUTTON_DEFS["GBA"])

DEFAULT_GBA_MAPPING = CONSOLE_BUTTON_DEFS["GBA"]["default_map"]

XINPUT_TO_GBA_BTN = CONSOLE_BUTTON_DEFS["GBA"]["xinput_map"]

GBA_BTN_HOTSPOTS = CONSOLE_BUTTON_DEFS["GBA"]["hotspots"]


_NAV_MAP = {
    _XI_UP:    _nav.action_key(_nav.UP),
    _XI_DOWN:  _nav.action_key(_nav.DOWN),
    _XI_LEFT:  _nav.action_key(_nav.LEFT),
    _XI_RIGHT: _nav.action_key(_nav.RIGHT),
    _XI_A:     _nav.action_key(_nav.ACCEPT),
    _XI_B:     _nav.action_key(_nav.BACK),
}

_MENU_NAV_ACTIONS = [
    ("Up",     _XI_UP,    _nav.UP),
    ("Down",   _XI_DOWN,  _nav.DOWN),
    ("Left",   _XI_LEFT,  _nav.LEFT),
    ("Right",  _XI_RIGHT, _nav.RIGHT),
    ("Cross",  _XI_A,     _nav.ACCEPT),
    ("Circle", _XI_B,     _nav.BACK),
    ("Start",  _XI_START, _nav.ACCEPT),
    ("Select", _XI_BACK,  _nav.BACK),
]


def apply_menu_nav_mapping(bit_by_button):
    _NAV_MAP.clear()
    for btn_name, default_bit, action in _MENU_NAV_ACTIONS:
        bit = bit_by_button.get(btn_name, default_bit)
        _NAV_MAP[bit] = _nav.action_key(action)


_xi_nav_state = {
    "ch03_active":   False,
    "game_running":  False,
    "browse_mode":   False,
    "dvd_active":    False,
    "dvd_in_menu":   False,
    "dvd_left_held":  False,
    "dvd_right_held": False,
    "running":       True,
}

# ============================================================================
# SECTION: Menu-nav wake signal & sync
# ============================================================================
_xi_nav_wake = threading.Event()

# VALUES CHANGE HERE
_XI_NAV_POLL_HZ = 30
_XI_NAV_NO_PAD_INTERVAL = 0.25
_XI_NAV_MISSES_BEFORE_SLOW = 30


def _xi_nav_is_active():
    return bool(_xi_nav_state["ch03_active"]) and not _xi_nav_state["game_running"]


def xi_nav_sync(ch03_active, game_running, browse_mode, dvd_active, dvd_in_menu):
    was_active = _xi_nav_is_active()
    _xi_nav_state["ch03_active"]  = bool(ch03_active)
    _xi_nav_state["game_running"] = bool(game_running)
    _xi_nav_state["browse_mode"]  = bool(browse_mode)
    _xi_nav_state["dvd_active"]   = bool(dvd_active)
    _xi_nav_state["dvd_in_menu"]  = bool(dvd_in_menu)
    if _xi_nav_is_active() != was_active:
        _xi_nav_wake.set()


# ============================================================================
# SECTION: Menu-nav gamepad worker
# ============================================================================


def _xi_nav_worker():
    POLL_HZ    = _XI_NAV_POLL_HZ
    _pad_rule  = _nav.HOLD_RULES["gamepad"]

    _DVD_NAV_MAP = {
        _XI_DOWN:  _nav.dvd_canonical(_nav.DVD_PLAY_PAUSE),
        _XI_LEFT:  _nav.dvd_canonical(_nav.DVD_REWIND),
        _XI_RIGHT: _nav.dvd_canonical(_nav.DVD_FAST_FORWARD),
        _XI_Y:     _nav.dvd_canonical(_nav.DVD_RETURN_MENU),
    }

    prev_bits   = 0
    hold_since  = {}
    last_repeat = {}
    held_key    = {}
    stick_dpad_latch = {}

    def _post(key):
        try:
            pygame.event.post(pygame.event.Event(
                pygame.KEYDOWN,
                key=key, mod=0, scancode=0, unicode="", synthetic=True
            ))
        except Exception as e:
            log.warning("Failed to post synthetic KEYDOWN for ch03 nav key %s: %s", key, e)

    def _post_up(key):
        try:
            pygame.event.post(pygame.event.Event(
                pygame.KEYUP,
                key=key, mod=0, scancode=0, unicode="", synthetic=True
            ))
        except Exception as e:
            log.warning("Failed to post synthetic KEYUP for ch03 nav key %s: %s", key, e)

    def _release_held(bits_to_release):
        for _bit in list(held_key.keys()):
            if bits_to_release & _bit:
                _post_up(held_key.pop(_bit))

    just_resumed = True

    no_pad_streak = 0

    while _xi_nav_state["running"]:
        # WARNING: release every key this worker holds before resetting, or a synthetic key is stranded when ch03 nav ends or a game starts.
        if not _xi_nav_is_active():
            _release_held(prev_bits)
            prev_bits = 0
            hold_since.clear()
            last_repeat.clear()
            _xi_nav_state["dvd_left_held"]  = False
            _xi_nav_state["dvd_right_held"] = False
            just_resumed = True
            no_pad_streak = 0
            _xi_nav_wake.clear()
            # WARNING: re-check after clear() or a wake-up set between the check and the clear is lost
            if not _xi_nav_is_active():
                _xi_nav_wake.wait()
            continue

        buttons = 0
        gp = _menu_nav_gpstate()
        if gp is not None:
            DEAD = stick_axis_threshold_raw()
            buttons = gp.wButtons
            if not stick_test_open():
                up, down, left, right = _stick_to_dpad_hysteresis(gp.sLX, gp.sLY, DEAD, stick_dpad_latch)
                if up:    buttons |= _XI_UP
                if down:  buttons |= _XI_DOWN
                if left:  buttons |= _XI_LEFT
                if right: buttons |= _XI_RIGHT

        now = time.monotonic()

        nav_mask = (_XI_UP | _XI_DOWN | _XI_LEFT | _XI_RIGHT |
                    _XI_A | _XI_B | _XI_START | _XI_BACK | _XI_Y)
        for _mapped_bit in _NAV_MAP:
            nav_mask |= _mapped_bit
        nav_bits = buttons & nav_mask

        if _xi_nav_state.get("dvd_active", False):
            _xi_nav_state["dvd_left_held"]  = bool(nav_bits & _XI_LEFT)
            _xi_nav_state["dvd_right_held"] = bool(nav_bits & _XI_RIGHT)
        else:
            _xi_nav_state["dvd_left_held"]  = False
            _xi_nav_state["dvd_right_held"] = False

        if just_resumed:
            prev_bits = nav_bits
            just_resumed = False

        if _xi_nav_state.get("dvd_active", False) and not _xi_nav_state.get("dvd_in_menu", False):
            active_map = dict(_NAV_MAP)
            active_map.update(_DVD_NAV_MAP)
        else:
            active_map = _NAV_MAP

        _release_held(prev_bits & ~nav_bits)

        just_pressed = nav_bits & ~prev_bits
        for bit, key in active_map.items():
            if just_pressed & bit:
                if _nav.is_back(key) and _xi_nav_state.get("browse_mode", False):
                    hold_since.pop(bit, None)
                    last_repeat.pop(bit, None)
                    continue
                _post(key)
                held_key[bit]    = key
                hold_since[bit]  = now
                last_repeat[bit] = now

        if _xi_nav_state.get("dvd_active", False):
            hold_since.clear()
            last_repeat.clear()
        else:
            for bit, key in active_map.items():
                if nav_bits & bit and not (just_pressed & bit):
                    # WARNING: never repeat the back key, and skip it entirely in browse_mode.
                    if _nav.is_back(key):
                        continue
                    since = hold_since.get(bit, now)
                    last  = last_repeat.get(bit, now)
                    if _nav.hold_due(_pad_rule, (now - since) * 1000.0, (now - last) * 1000.0):
                        _post(key)
                        last_repeat[bit] = now
                elif not (nav_bits & bit):
                    hold_since.pop(bit, None)
                    last_repeat.pop(bit, None)

        prev_bits = nav_bits

        if gp is None:
            no_pad_streak += 1
        else:
            no_pad_streak = 0
        if no_pad_streak >= _XI_NAV_MISSES_BEFORE_SLOW:
            _xi_nav_wake.clear()
            _xi_nav_wake.wait(_XI_NAV_NO_PAD_INTERVAL)
        else:
            time.sleep(1.0 / POLL_HZ)

    log.info("[XI NAV] Thread exited.")

if _WIN:
    threading.Thread(target=_xi_nav_worker, daemon=True).start()


# ============================================================================
# SECTION: Emulator config path
# ============================================================================


def resolve_emulator_config_path():
    """Return the path to emulator_config.json under <root>/controllers/."""
    root_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    controllers_dir = os.path.join(root_dir, "controllers")
    new_path = os.path.join(controllers_dir, "emulator_config.json")
    try:
        os.makedirs(controllers_dir, exist_ok=True)
    except Exception as e:
        log.warning("[CONFIG] Could not create controllers/ folder (%s) -- "
                     "emulator_config.json will fail to save: %s", controllers_dir, e)
        return new_path
    if not os.path.exists(new_path):
        old_path = os.path.join(root_dir, "emulator_config.json")
        if os.path.exists(old_path):
            try:
                shutil.move(old_path, new_path)
                log.info("[CONFIG] Migrated emulator_config.json from %s to %s", old_path, new_path)
            except Exception as e:
                log.warning("[CONFIG] Could not migrate emulator_config.json from %s to %s: %s",
                             old_path, new_path, e)
    return new_path


# ============================================================================
# SECTION: External game deck
# ============================================================================


class ExternalGameDeck:

    # ========================================================================
    # SECTION: State & setup
    # ========================================================================

    def __init__(self, vlc_engine=None):
        self.base_dir = os.path.dirname(resolve_main_dir())
        self.cores_dir = os.path.join(self.base_dir, "main", "cores")
        self.roms_dir = os.path.join(self.base_dir, "main", "roms")
        self.logos_dir = os.path.join(self.base_dir, "main", "images", "logos")
        self.config_path = resolve_emulator_config_path()
        self.last_gameplay_input_ticks = 0

        self.mode = "BROWSE"
        self.consoles = []
        self.console_index = 0
        self.roms = []
        self.rom_index = 0
        self.rom_scroll = 0
        self.status_msg = ""

        self.romlist_focus     = "list"
        self.romlist_arrow_sel = "up"

        self._console_arrow_flash_dir   = None
        self._console_arrow_flash_until = 0
        self._CONSOLE_ARROW_FLASH_MS    = 220

        self._scan_lock          = threading.Lock()
        self._scan_thread        = None
        self._scan_request_id    = 0
        self._scan_pending       = False
        self._scan_started_ticks = 0
        self._scan_timeout_ms    = 6000

        self._scan_is_background   = False
        self._pending_scan_is_background = False
        self._last_live_rescan_ticks = 0
        self._scan_result        = None

        self._romlist_error_msg   = ""
        self._romlist_error_until = 0

        self._rom_hold = _nav.HoldState()

        self.deck_process = None
        self.game_hwnd = None
        self.ui_overlay_hwnd = None
        self._fit_rect = None
        self.parent_hwnd = None
        self.is_visible = False
        self.is_embedded = False
        self.is_frozen = False
        self._pause_reasons = set()
        self.original_style = None
        self.active_console = None
        self.current_browse_dir = ""

        self.ready_flag = False
        self.ui_color = (0, 255, 128)
        self.scroll_style = "carousel"
        self.list_text_color = (255, 255, 255)
        self.emulator_loading  = False
        self.emulator_load_pct = 0
        self.emulator_load_msg = ""

        self._viewport_lock = __import__("threading").Lock()
        self.overlay_ready = False

        self._audio_vol_lock = threading.Lock()
        self._audio_vol_pending = None
        self._audio_vol_worker_running = False

        self._child_visible = None
        self._vis_lock = __import__("threading").Lock()

        self._logo_cache = {}
        self._logo_raw_cache = {}
        self._header_logo_raw_cache = {}
        self._boxed_logo_cache = {}
        self._font_big = None
        self._font_mid = None
        self._font_small = None

        self.vlc_engine = vlc_engine
        self.dvd_gain_lookup = None

        self.dvd_last_dir = ""
        self.dvd_file_path = ""
        self._dvd_highlight_shadow = None
        self.dvd_splash_playing = False
        self.dvd_hud_until = 0
        self.dvd_splash_done = False
        self.dvd_splash_enabled = False
        self.dvd_splash_path = ""
        self._dvd_custom_splash_surf = None
        self._dvd_custom_splash_surf_path = None
        self._dvd_splash_is_video = False
        self._dvd_splash_video_last_frame = None
        self.console_splash_enabled = {}
        self.console_splash_path    = {}
        self.active_bios            = {}
        self._console_splash_surf_cache = {}
        self.menu_bg_path = ""
        self._menu_bg_surf_cache = None
        self._console_splash_is_video = False
        self._console_splash_start_time = 0
        self._console_splash_video_last_frame = None
        self.dvd_playback_active = False
        self.dvd_paused = False
        self.dvd_resume_pending = False
        self.dvd_paused_frame = None
        self._dvd_last_fit_geom = None
        self.dvd_awaiting_resume = False
        self.dvd_captions = False
        self.dvd_captions_enabled = False
        self.dvd_resume_enabled = False
        self.dvd_resume_positions = {}
        self._dvd_resume_seek_seconds = 0
        self._dvd_captions_auto_pending = False
        self._dvd_captions_auto_done = True
        self._dvd_captions_auto_deadline = 0
        self._dvd_captions_auto_next_try = 0
        self._dvd_captions_asr_state = "idle"
        self._dvd_captions_asr_srt_path = None
        self._dvd_cap_flow = None
        self._dvd_cap_suppress = None
        self.dvd_exit_confirm = False
        self.dvd_exit_selection = "No"
        self.dvd_exit_confirm_shown_at = 0
        self.dvd_return_menu_confirm = False
        self.dvd_return_menu_selection = "No"
        self.dvd_return_menu_confirm_shown_at = 0
        self.dvd_osd_msg = ""
        self.dvd_osd_until = 0

        self.dvd_is_disc_image = False
        self.dvd_in_menu = False
        self._dvd_menu_flip_since = 0
        self._dvd_menu_flip_candidate = None
        self._dvd_end_state_since = None
        self._dvd_menu_manual_override = False
        self._dvd_iso_watchdog_armed = False
        self._dvd_iso_watchdog_deadline = 0
        self._dvd_iso_watchdog_armed_at = 0

        self.dvd_was_in_menu_on_leave = False
        self.dvd_saved_title = 1

        self.dvd_iso_backgrounded = False

        self.game_exit_confirm = False
        self.game_exit_selection = "No"
        self._game_exit_prev_buttons = frozenset()
        self._game_exit_toggle_held = False
        self._exit_key_event_at = -1
        self.game_exit_confirm_shown_at = 0
        self._prev_overlay_open = False
        self._libretro_suppress_start_select = False

        self._active_mapping_console = "GBA"
        self._console_mappings = {}
        self.ctrl_mapping = self._load_controller_mapping("GBA")
        self.ctrl_mapping_active_btn = None
        self.ctrl_mapping_mode = "controller"
        self._wf_surface_cache = {}
        self._wf_tinted_cache  = {}
        self._wf_tint_color_cache = {}
        self._gba_wf_surface = None
        self._gba_wf_tinted  = None
        self._gba_wf_tint_color = None
        self._seq_remap_active = False
        self._ctrl_labels = {}
        self._last_xinput_btns = 0
        self._mapping_close_requested = False
        self._ctrl_map_cursor = 0

        self._active_mapping_player = 1
        self._ctrl_map_player_sel = 1
        self._mapping_player_by_console = {}
        self._mapping_reassign_wait_started = None
        self._ctrl_map_on_pick = False
        self._pick_flow_pending_player = None
        self._pick_layout_popup_player = None
        self._gameplay_ctrl_mapping_cache = {}
        self._ctrl_test_lit = set()
        self._ctrl_test_lit_joy = set()

        self.console_rom_dir = {}
        self.console_roms_manual = {}

        self.emulator_config = self._load_emulator_config()
        self._refresh_consoles()

        self._libretro_core           = None
        self._libretro_console        = ""
        self._libretro_run_accum_s    = 0.0
        self._libretro_last_run_wall  = None
        self._prev_libretro_console   = ""

        self._libretro_rom_path       = ""
        self._libretro_loading        = False
        self._libretro_load_start     = 0
        self._libretro_load_min_ms    = 2500
        self._libretro_launch_pending_timeout_ms = 20000
        self._libretro_first_frame_giveup_ms = 20000
        self._libretro_load_tau_ms = 1200
        self._libretro_100_reached_at = None

        self._libretro_launch_lock       = threading.Lock()
        self._libretro_launch_pending    = False
        self._libretro_launch_result     = None
        self._libretro_launch_request_id = 0
        self._libretro_launch_thread     = None
        self._libretro_auto_save      = False
        self._libretro_auto_load      = False
        self._libretro_active_profile = 1
        self._libretro_screen_size    = "full"
        self._libretro_border_enabled       = False
        self._libretro_border_style         = 1
        self._libretro_border_path          = None
        self._libretro_border_surf          = None
        self._libretro_border_rect_frac     = None
        self._libretro_border_src_key       = None
        self._border_rect_cache             = {}
        self._border_blit_cache             = None
        self._libretro_xinput_map     = {}
        self._libretro_hotkey_cooldown = 0
        self._libretro_osd_msg        = ""
        self._libretro_osd_until      = 0
        self._libretro_prev_connected_players = set()
        self._libretro_disconnect_pause  = False
        self._libretro_disconnect_player = None
        self._libretro_multitap_auto_engaged = False
        self._libretro_multitap_pending = False
        threading.Thread(target=self._preload_logos_async, daemon=True).start()

    # ========================================================================
    # SECTION: Config, console folders & profile sync
    # ========================================================================

    def _load_emulator_config(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            log.warning("Failed to load emulator_config.json, using defaults: %s", e)
        return {}

    def _get_console_rom_dir(self, console):
        override = self.console_rom_dir.get(console, "")
        if override and os.path.isdir(override):
            return override
        return os.path.join(self.roms_dir, console)

    def _console_bezel_dir(self, console):
        if console in BORDER_CONSOLES_HOME:
            _shared_singular = os.path.join(self.base_dir, "main", "image", "bezel")
            _shared_plural    = os.path.join(self.base_dir, "main", "images", "bezel")
            if os.path.isdir(_shared_singular):
                return _shared_singular
            return _shared_plural
        rom_dir = self._get_console_rom_dir(console)
        bezel_dir = os.path.join(rom_dir, "bezel")
        if not os.path.isdir(bezel_dir):
            bezel_dir = os.path.join(rom_dir, "border")
        return bezel_dir

    _bezel_style_debug_seen = {}

    def console_bezel_style_count(self, console):
        """Return how many border image styles this console's own frame has (see BORDER_CONSOLES)."""
        if console not in BORDER_CONSOLES_HOME:
            return 1
        bezel_dir  = self._console_bezel_dir(console)
        discovered = _discover_border_style_files(bezel_dir)
        count      = max(1, len(discovered))
        _key = (console, bezel_dir)
        _cur = (count, tuple(discovered))
        if self._bezel_style_debug_seen.get(_key) != _cur:
            self._bezel_style_debug_seen[_key] = _cur
            log.info("[CONSOLE BEZEL STYLE] console=%s dir=%s exists=%s found=%s -> count=%d",
                      console, bezel_dir, os.path.isdir(bezel_dir),
                      [os.path.basename(f) for f in discovered], count)
        return count

    def _refresh_consoles(self, consoles_enabled=None):
        if consoles_enabled is None:
            consoles_enabled = {}
        found = []
        for c in CONSOLE_ORDER:
            if c == "MENU":
                continue
            default_on = (c == "DVD")
            if not consoles_enabled.get(c, default_on):
                continue
            if c == "DVD":
                found.append(c)
                continue
            emu_folder = os.path.join(self.cores_dir, c)
            rom_folder = self._get_console_rom_dir(c)
            if (os.path.isdir(emu_folder)
                    or os.path.isdir(rom_folder)
                    or bool(self.console_roms_manual.get(c))):
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

    def sync_profile_from_db(self, console, db_config):
        all_profiles = db_config.get("console_profiles", {})
        con_data     = all_profiles.get(console, {})
        active       = int(con_data.get("active", 1))
        profiles     = con_data.get("profiles", {})
        prof_cfg     = profiles.get(active, profiles.get(str(active), {}))
        self._libretro_active_profile = active
        self._libretro_auto_save      = bool(prof_cfg.get("auto_save", False))
        self._libretro_auto_load      = bool(prof_cfg.get("auto_load", False))
        if console in HANDHELD_CONSOLES:
            self._libretro_screen_size = prof_cfg.get("screen_size", "full")
            if self._libretro_screen_size not in SCREEN_SIZE_FACTORS:
                self._libretro_screen_size = "full"
        else:
            self._libretro_screen_size = "full"
        if border_available_for_console(console):
            self._libretro_border_enabled = bool(
                prof_cfg.get("border_enabled", prof_cfg.get("gb_border", False)))
            try:
                self._libretro_border_style = int(prof_cfg.get("border_style", 1))
            except (TypeError, ValueError):
                self._libretro_border_style = 1
        else:
            self._libretro_border_enabled = False
            self._libretro_border_style = 1
        self._libretro_border_surf      = None
        self._libretro_border_rect_frac = None
        self._libretro_border_src_key   = None
        self._border_blit_cache         = None
        self._libretro_multitap_enabled = (bool(db_config.get("multitap_enabled_global", False))
                                            if console in MULTITAP_CONSOLES else None)
        self._libretro_multitap_auto_engaged = False
        self._libretro_multitap_pending = False
        if self._libretro_core is not None and hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(active)
        log.info(f"[GAMEDECK PROFILE] {console} → profile {active}, "
              f"auto_save={self._libretro_auto_save}, auto_load={self._libretro_auto_load}")

    # ========================================================================
    # SECTION: Border & multitap live toggles
    # ========================================================================

    def set_border_live(self, enabled):
        if enabled and not border_available_for_console(self._libretro_console):
            enabled = False
        self._libretro_border_enabled = bool(enabled)

    def set_border_style_live(self, style):
        """Apply a new border style number to the running console's own frame (home consoles only)."""
        if self._libretro_console not in BORDER_CONSOLES_HOME:
            return
        n = self.console_bezel_style_count(self._libretro_console)
        try:
            style = int(style)
        except (TypeError, ValueError):
            style = 1
        self._libretro_border_style     = max(1, min(style, n))
        self._libretro_border_surf      = None
        self._libretro_border_rect_frac = None
        self._libretro_border_src_key   = None
        self._border_blit_cache         = None

    def toggle_multitap_live(self, enabled):
        core = self._libretro_core
        if core is None:
            return None
        self._libretro_multitap_enabled = bool(enabled)
        self._libretro_multitap_auto_engaged = False
        self._libretro_multitap_pending = False
        if enabled:
            if hasattr(core, "enable_multitap") and core.enable_multitap():
                return "ON"
            return "UNSUPPORTED"
        if hasattr(core, "disable_multitap"):
            core.disable_multitap()
        return "OFF"

    def _reconcile_multitap_live(self, prev_bound, bound_players):
        """Keep multitap in sync with controller count while a game is running."""
        core = self._libretro_core
        if core is None or not core.is_loaded:
            return
        if getattr(self, "_libretro_console", None) not in MULTITAP_CONSOLES:
            return
        if not hasattr(core, "multitap_available") or not core.multitap_available():
            return

        crossed_up   = prev_bound < 3 <= bound_players
        crossed_down = prev_bound >= 3 > bound_players
        if not (crossed_up or crossed_down):
            return

        is_active = core.multitap_active() if hasattr(core, "multitap_active") else False
        # WARNING: None (never decided) must not read as an explicit off, or a forced-off decision is re-armed by the next connect/disconnect.
        forced_state = getattr(self, "_libretro_multitap_enabled", None)

        if crossed_up and not is_active and forced_state is not False:
            self._libretro_multitap_pending = True
        elif crossed_down:
            self._libretro_multitap_pending = False
            if is_active and self._libretro_multitap_auto_engaged:
                core.disable_multitap()
                self._libretro_multitap_auto_engaged = False
                log.info(f"[LIBRETRO MULTIPLAYER] Multitap auto-disabled live "
                      f"({bound_players} controllers bound)")
                self._libretro_osd_msg   = "MULTITAP DISABLED"
                self._libretro_osd_until = pygame.time.get_ticks() + 2000

    _MULTITAP_ACTIVITY_DZ = 6000

    def _gp_has_activity(self, gp):
        if gp is None:
            return False
        dz = self._MULTITAP_ACTIVITY_DZ
        return bool(
            gp.wButtons or gp.bLT or gp.bRT
            or abs(gp.sLX) > dz or abs(gp.sLY) > dz
            or abs(gp.sRX) > dz or abs(gp.sRY) > dz
        )

    def _resolve_pending_multitap(self, player_gps):
        """Commit the deferred multitap enable once player 3/4 shows real input."""
        if not self._libretro_multitap_pending:
            return
        core = self._libretro_core
        if core is None or not core.is_loaded:
            return
        if not any(self._gp_has_activity(player_gps.get(_p)) for _p in (3, 4)):
            return

        self._libretro_multitap_pending = False
        already_active = core.multitap_active() if hasattr(core, "multitap_active") else False
        if already_active:
            return
        if hasattr(core, "enable_multitap") and core.enable_multitap():
            self._libretro_multitap_auto_engaged = True
            log.info("[LIBRETRO MULTIPLAYER] Multitap auto-enabled on player 3/4 input activity")
            self._libretro_osd_msg   = "MULTITAP ENABLED"
            self._libretro_osd_until = pygame.time.get_ticks() + 2000

    _BORDER_DEFAULT_RECT_FRAC = (48 / 256, 40 / 224, 160 / 256, 144 / 224)

    _HANDHELD_BEZEL_FIT_MARGIN = 0.90

    _CONFIRM_BURN_IN_MS = 20000
    _LIBRETRO_MAX_CATCHUP_FRAMES = 4

    # ========================================================================
    # SECTION: Border loading & drawing
    # ========================================================================

    def _detect_border_cutout_frac(self, raw_surf):
        """Return rect_frac, the (x, y, w, h) fraction of the image occupied by the transparent screen cutout, or the SGB-style default."""
        try:
            import numpy as np
            w, h = raw_surf.get_size()
            alpha = pygame.surfarray.array_alpha(raw_surf)
            transparent = alpha < 24
            try:
                import cv2
            except ImportError:
                cv2 = None
            if cv2 is not None:
                rect = self._cutout_rect_cv2(transparent, w, h, np, cv2)
            else:
                rect = self._cutout_rect_python(transparent, w, h, np)
            log.info(f"[LIBRETRO] Border cutout auto-detected: {rect}")
            return rect
        except Exception as e:
            log.warning(f"[LIBRETRO] Border cutout auto-detect unavailable ({e}); "
                  f"using default SGB-style proportions")
            return self._BORDER_DEFAULT_RECT_FRAC

    @staticmethod
    def _cutout_rect_cv2(transparent, w, h, np, cv2):
        """Finds the largest transparent region not touching the image edge with OpenCV connected components; the GIL is released while it runs."""
        # WARNING: the array stays indexed [x, y]; transposing it changes which region wins a size tie.
        mask = np.ascontiguousarray(transparent, dtype=np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        outside = np.zeros(count, dtype=bool)
        outside[labels[0, :]] = True
        outside[labels[-1, :]] = True
        outside[labels[:, 0]] = True
        outside[labels[:, -1]] = True
        outside[0] = True
        areas = stats[:, cv2.CC_STAT_AREA]
        interior_areas = np.where(outside, 0, areas)
        if int(interior_areas.sum()) < (w * h * 0.005):
            raise ValueError("no enclosed transparent cutout found")
        best_size = int(interior_areas.max())
        tied = np.nonzero(interior_areas == best_size)[0]
        if len(tied) == 1:
            best = int(tied[0])
        else:
            flat = labels.ravel()
            best = min((int(t) for t in tied), key=lambda t: int(np.argmax(flat == t)))
        minx = int(stats[best, cv2.CC_STAT_TOP])
        maxx = minx + int(stats[best, cv2.CC_STAT_HEIGHT]) - 1
        miny = int(stats[best, cv2.CC_STAT_LEFT])
        maxy = miny + int(stats[best, cv2.CC_STAT_WIDTH]) - 1
        return (minx / w, miny / h, (maxx - minx + 1) / w, (maxy - miny + 1) / h)

    @staticmethod
    def _cutout_rect_python(transparent, w, h, np):
        """Finds the largest transparent region not touching the image edge with a per-pixel flood fill; used only when OpenCV is missing."""
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
        return rect

    def _border_source_path(self, console, style):
        """Returns the bezel image path for this console and style number, or None when it has none."""
        bezel_dir = self._console_bezel_dir(console)
        path = None
        style_files = _discover_border_style_files(bezel_dir)
        if style_files:
            if console in BORDER_CONSOLES_HOME:
                n = len(style_files)
                idx = max(1, min(int(style or 1), n)) - 1
            else:
                idx = 0
            path = style_files[idx]
        elif os.path.isdir(bezel_dir):
            exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
            try:
                for f in sorted(os.listdir(bezel_dir)):
                    if os.path.splitext(f)[1].lower() in exts:
                        path = os.path.join(bezel_dir, f); break
            except Exception:
                path = None
        return path

    def _prepare_border_asset(self, console):
        """Finds and loads the bezel image and finds its screen cutout; safe to run off the main thread."""
        # WARNING: no display conversion (convert_alpha) and no draw-state writes may run here; they stay on the main thread.
        style = self._libretro_border_style
        prepared = {"console": console, "style": style, "path": None, "thread": threading.current_thread().name}
        path = self._border_source_path(console, style)
        if path is None:
            return prepared
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0
        key = (console, path, mtime)
        prepared["path"] = path
        prepared["key"] = key
        if key == self._libretro_border_src_key and self._libretro_border_surf is not None:
            prepared["reuse"] = True
            return prepared
        _t0 = time.perf_counter()
        try:
            raw = pygame.image.load(path)
        except Exception as e:
            prepared["error"] = e
            return prepared
        prepared["load_ms"] = (time.perf_counter() - _t0) * 1000.0
        prepared["raw"] = raw
        prepared["rect"] = self._border_rect_cache.get((path, mtime))
        prepared["rect_cached"] = prepared["rect"] is not None
        prepared["detect_ms"] = 0.0
        _masks = raw.get_masks()
        if prepared["rect"] is None and raw.get_bitsize() == 32 and _masks[3] != 0:
            _t0 = time.perf_counter()
            prepared["rect"] = self._detect_border_cutout_frac(raw)
            prepared["detect_ms"] = (time.perf_counter() - _t0) * 1000.0
            self._border_rect_cache[(path, mtime)] = prepared["rect"]
        return prepared

    def _border_reuse_valid(self, prepared):
        return prepared.get("key") == self._libretro_border_src_key and self._libretro_border_surf is not None

    def _install_prepared_border(self, console, prepared):
        """Main-thread step: converts the prepared bezel image for drawing and makes it the active border; prepares it here when no valid prepared copy was given."""
        if (prepared is None or prepared.get("console") != console
                or prepared.get("style") != self._libretro_border_style
                or (prepared.get("reuse") and not self._border_reuse_valid(prepared))):
            prepared = self._prepare_border_asset(console)

        path = prepared.get("path")
        if path is None:
            self._libretro_border_path      = None
            self._libretro_border_surf      = None
            self._libretro_border_rect_frac = None
            self._libretro_border_src_key   = None
            return
        if prepared.get("reuse"):
            return

        _t0 = time.perf_counter()
        raw_surf = None
        err = prepared.get("error")
        if err is None:
            try:
                raw_surf = prepared["raw"].convert_alpha()
            except Exception as e:
                err = e
        if err is not None:
            log.warning(f"[LIBRETRO] Could not load bezel image '{path}': {err}")
            self._libretro_border_path      = None
            self._libretro_border_surf      = None
            self._libretro_border_rect_frac = None
            self._libretro_border_src_key   = None
            return

        rect = prepared.get("rect")
        detect_ms = prepared.get("detect_ms", 0.0)
        if rect is None:
            _t1 = time.perf_counter()
            rect = self._detect_border_cutout_frac(raw_surf)
            detect_ms = (time.perf_counter() - _t1) * 1000.0
            self._border_rect_cache[(path, prepared["key"][2])] = rect
        self._libretro_border_rect_frac = rect
        self._libretro_border_path      = path
        self._libretro_border_surf      = raw_surf
        self._libretro_border_src_key   = prepared["key"]
        log.info("[LIBRETRO BORDER] %s: prepared on %s (image load %.0f ms, cutout %s %.0f ms); main-thread install %.1f ms",
                 console, prepared.get("thread", "?"), prepared.get("load_ms", 0.0),
                 "from cache" if prepared.get("rect_cached") else "detected", detect_ms,
                 (time.perf_counter() - _t0) * 1000.0)

    def _load_border_surface(self, console):
        """Loads and installs the console's bezel image on the calling thread."""
        self._install_prepared_border(console, None)

    def _start_border_prep(self, console):
        """Starts preparing the console's bezel image on a background thread and returns (thread, holder), or None when the console has no bezel."""
        if not border_available_for_console(console):
            return None
        holder = {}

        def _run():
            try:
                holder["prepared"] = self._prepare_border_asset(console)
            except Exception as e:
                log.warning("[LIBRETRO] Bezel prepare failed for %s: %s", console, e)

        th = threading.Thread(target=_run, name="BorderPrep", daemon=True)
        th.start()
        return th, holder

    def _outer_tv_bezel_blocks_console_bezel(self, console):
        """True when the console's own bezel must not be drawn because the outer System Bezel is on."""
        if console not in BORDER_CONSOLES_HOME:
            return False
        try:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            if _main and hasattr(_main, "db") and _main.db:
                return bool(_main.db.config.get("fake_43_border_enabled", False))
        except Exception:
            pass
        return False

    def _blit_libretro_frame(self, screen, surf, dest_rect):
        _gft_t0 = time.perf_counter()
        try:
            self._blit_libretro_frame_impl(screen, surf, dest_rect)
        finally:
            GAME_FRAME_TIMER.add("blit", (time.perf_counter() - _gft_t0) * 1000.0)

    def _blit_libretro_frame_impl(self, screen, surf, dest_rect):
        _in_border_consoles = self._libretro_console in BORDER_CONSOLES
        _border_avail = border_available_for_console(self._libretro_console)
        _outer_blocks = self._outer_tv_bezel_blocks_console_bezel(self._libretro_console)
        _use_bordered = (_in_border_consoles and self._libretro_border_enabled
                          and _border_avail and not _outer_blocks)
        _log_key = (self._libretro_console, _use_bordered, self._libretro_border_enabled, _outer_blocks)
        if _blit_libretro_frame_log_state.get("key") != _log_key:
            _blit_libretro_frame_log_state["key"] = _log_key
            log.info(
                "[GAMEDECK BLIT PATH] console=%s using_console_bezel=%s "
                "(console_bezel_toggle=%s, outer_bezel_blocks_it=%s, "
                "in_border_consoles=%s, border_art_available=%s)",
                self._libretro_console, _use_bordered,
                self._libretro_border_enabled, _outer_blocks,
                _in_border_consoles, _border_avail)
        if _use_bordered:
            if self._libretro_border_surf is None:
                self._load_border_surface(self._libretro_console)
            if self._libretro_border_surf is not None:
                rect_frac = self._libretro_border_rect_frac or self._BORDER_DEFAULT_RECT_FRAC
                self._blit_bordered_cached(screen, surf, self._libretro_border_surf, rect_frac, dest_rect)
                return
        _libretro_blit_scaled(screen, surf, dest_rect)

    def _compute_border_inner_geometry(self, border_surf, rect_frac, dest_rect):
        dw, dh = dest_rect[2], dest_rect[3]
        bw, bh = border_surf.get_size()
        scale   = min(dw / max(bw, 1), dh / max(bh, 1))
        if self._libretro_console in BORDER_CONSOLES_HANDHELD:
            scale *= self._HANDHELD_BEZEL_FIT_MARGIN
        draw_bw = int(bw * scale); draw_bh = int(bh * scale)
        ox = dest_rect[0] + (dw - draw_bw) // 2
        oy = dest_rect[1] + (dh - draw_bh) // 2
        rx, ry, rw, rh = rect_frac
        inner_x = ox + int(draw_bw * rx)
        inner_y = oy + int(draw_bh * ry)
        inner_w = max(1, int(draw_bw * rw))
        inner_h = max(1, int(draw_bh * rh))
        return (ox, oy, draw_bw, draw_bh), (inner_x, inner_y, inner_w, inner_h)

    def get_active_console_bezel_inner_rect(self, dest_rect):
        return None

    def _console_bezel_shader_enabled(self):
        """Whether the console's own bezel warps its picture with the CRT shader, gated on crt_shader_enabled, warp_screen_enabled and the Performance Mode lock."""
        if self._libretro_console in BORDER_CONSOLES_HANDHELD:
            return False
        try:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            if not (_main and hasattr(_main, "db") and _main.db):
                return False
            if not _main.db.config.get("crt_shader_enabled", True):
                return False
            if not _main.db.config.get("warp_screen_enabled", False) or _main._feature_locked("warp_screen_enabled"):
                return False
            return True
        except Exception:
            return False

    def _draw_console_bezel_channel_led(self, screen, border_rect):
        """Draws the live 'CH NN' readout and a black hide-patch over the bezel's baked-in channel number; border_rect is this bezel's drawn (ox, oy, draw_bw, draw_bh)."""
        # WARNING: retro_tv_emulator.py is __main__ and imports this file, so reach it through __main__; never import it here.
        if self._libretro_console in BORDER_CONSOLES_HANDHELD:
            return
        try:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            if not (_main and hasattr(_main, "db") and _main.db
                    and hasattr(_main, "_draw_bezel_channel_led")
                    and hasattr(_main, "_BEZEL_CHANNEL_LED_FRAC")):
                return
            stem = (os.path.splitext(os.path.basename(self._libretro_border_path))[0].lower()
                    if self._libretro_border_path else None)
            frac = _main._BEZEL_CHANNEL_LED_FRAC.get(stem) if hasattr(_main, "_BEZEL_CHANNEL_LED_FRAC") else None
            if frac is None:
                return
            fx, fy, fw, fh = frac
            bx, by, bw, bh = border_rect
            led_rect = pygame.Rect(
                bx + int(bw * fx), by + int(bh * fy),
                max(1, int(bw * fw)), max(1, int(bh * fh)))
            num_text = _main.app_state.get("digit_buffer") or _main.app_state.get("current_channel", "")
            import colorsys as _osd_colorsys
            hue = _main.db.config.get("theme_ui_hue", 140) / 360.0
            r, g, b = _osd_colorsys.hsv_to_rgb(hue, 0.9, 1.0)
            color = (int(r * 255), int(g * 255), int(b * 255))
            _main._draw_bezel_channel_led(screen, led_rect, stem, "CH", str(num_text), color)
        except Exception as e:
            _log_warn_throttled("console_bezel_led", "Console bezel channel-LED draw failed: %s", e)

    def _blit_bordered_cached(self, screen, surf, border_surf, rect_frac, dest_rect):
        key = (id(border_surf), tuple(dest_rect), tuple(rect_frac))
        cached = self._border_blit_cache
        if cached is None or cached[0] != key:
            _gft_rebuild_t0 = time.perf_counter()
            try:
                (ox, oy, draw_bw, draw_bh), (inner_x, inner_y, inner_w, inner_h) = \
                    self._compute_border_inner_geometry(border_surf, rect_frac, dest_rect)
                if self._libretro_console in BORDER_CONSOLES_HANDHELD:
                    scaled_border = pygame.transform.scale(border_surf, (draw_bw, draw_bh))
                else:
                    scaled_border = pygame.transform.smoothscale(border_surf, (draw_bw, draw_bh))
                self._border_blit_cache = (key, scaled_border, (ox, oy), (draw_bw, draw_bh), (inner_x, inner_y, inner_w, inner_h))
                cached = self._border_blit_cache
            except Exception as e:
                _log_warn_throttled("libretro_border_cache_build", "Border cache rebuild failed, falling back to plain scaled frame: %s", e)
                self._border_blit_cache = None
                _libretro_blit_scaled(screen, surf, dest_rect)
                return
            finally:
                GAME_FRAME_TIMER.add("cache_rebuild", (time.perf_counter() - _gft_rebuild_t0) * 1000.0)
                GAME_FRAME_TIMER.count("cache_rebuild")

        _key, scaled_border, (ox, oy), (draw_bw, draw_bh), (inner_x, inner_y, inner_w, inner_h) = cached

        _fit_x = _fit_y = _fit_w = _fit_h = 0
        try:
            if _active_game_screen_mode() == "stretch":
                _fit_x, _fit_y, _fit_w, _fit_h = inner_x, inner_y, inner_w, inner_h
            else:
                _fit_x, _fit_y, _fit_w, _fit_h = _contain_fit_rect(
                    surf.get_width(), surf.get_height(), (inner_x, inner_y, inner_w, inner_h))
            if _fit_w < inner_w or _fit_h < inner_h:
                _bezel_bar_color = (1, 1, 1)
                try:
                    _main_gd = sys.modules.get("__main__")
                    _bezel_bar_fn = getattr(_main_gd, "_video_bar_color", None)
                    if callable(_bezel_bar_fn):
                        _bezel_bar_color = _bezel_bar_fn()
                except Exception:
                    pass
                screen.fill(_bezel_bar_color, (inner_x, inner_y, inner_w, inner_h))
            if _fit_w > 0 and _fit_h > 0:
                _gft_t0 = time.perf_counter()
                screen.blit(pygame.transform.scale(surf, (_fit_w, _fit_h)), (_fit_x, _fit_y))
                GAME_FRAME_TIMER.add("scale", (time.perf_counter() - _gft_t0) * 1000.0)
                if self._console_bezel_shader_enabled():
                    try:
                        import sys as _sys
                        _main = _sys.modules.get("__main__")
                        if _main and hasattr(_main, "_apply_crt_shader"):
                            _scanlines_on = bool(_main.db.config.get("fake_43_scanlines_enabled", False)) and not _main._feature_locked("fake_43_scanlines_enabled")
                            _stem = (os.path.splitext(os.path.basename(self._libretro_border_path))[0].lower()
                                     if self._libretro_border_path else None)
                            _curv_map = getattr(_main, "_CRT_SHADER_CURVATURE", {})
                            _curv_default = getattr(_main, "_CRT_SHADER_CURVATURE_DEFAULT", 0.20)
                            _curvature = _curv_map.get(_stem, _curv_default) if _stem else _curv_default
                            _gft_t0 = time.perf_counter()
                            _main._apply_crt_shader(
                                screen, pygame.Rect(_fit_x, _fit_y, _fit_w, _fit_h),
                                scanlines=_scanlines_on, curvature=_curvature)
                            GAME_FRAME_TIMER.add("shader", (time.perf_counter() - _gft_t0) * 1000.0)
                    except Exception as _e:
                        _log_warn_throttled("console_bezel_shader", "Console bezel CRT shader failed: %s", _e)
        except Exception:
            log.exception("Console bezel picture blit failed this frame (border/LED still drawn below)")
            if _fit_w <= 0 or _fit_h <= 0:
                _libretro_blit_scaled(screen, surf, dest_rect)

        try:
            _gft_t0 = time.perf_counter()
            screen.blit(scaled_border, (ox, oy))
            self._draw_console_bezel_channel_led(screen, (ox, oy, draw_bw, draw_bh))
            GAME_FRAME_TIMER.add("border", (time.perf_counter() - _gft_t0) * 1000.0)
        except Exception:
            log.exception("Console bezel frame (border art + LED) blit failed this frame")

    # ========================================================================
    # SECTION: Libretro profile save/load & PSX discs
    # ========================================================================

    def libretro_save_to_profile(self, console=None, profile=None):
        if self._libretro_core is None:
            return False
        if profile is None:
            profile = self._libretro_active_profile
        slot = int(profile)
        if hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(slot)
        ok = self._libretro_core.save_state(slot=slot)
        log.info(f"[GAMEDECK PROFILE] Save → profile{slot}/slot{slot}: {'OK' if ok else 'FAILED'}")
        return ok

    def libretro_load_from_profile(self, console=None, profile=None):
        if self._libretro_core is None:
            return False
        if profile is None:
            profile = self._libretro_active_profile
        slot = int(profile)
        if hasattr(self._libretro_core, "set_active_profile"):
            self._libretro_core.set_active_profile(slot)
        if not self._libretro_core.has_state(slot=slot):
            log.info(f"[GAMEDECK PROFILE] Load profile{slot}/slot{slot}: no save found")
            return False
        ok = self._libretro_core.load_state(slot=slot)
        log.info(f"[GAMEDECK PROFILE] Load ← profile{slot}/slot{slot}: {'OK' if ok else 'FAILED'}")
        return ok

    def get_psx_disc_info(self):
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
        log.info(f"[GAMEDECK] Change disc → disc {disc_num}: {'OK' if ok else 'FAILED'}")
        return ok

    # ========================================================================
    # SECTION: Pause state
    # ========================================================================

    def set_paused(self, paused, hard=False, reason="", muted=None):
        """Single authoritative pause entry point for the libretro core."""
        core = self._libretro_core
        if core is None or not core.is_loaded:
            return

        if paused:
            self._pause_reasons.add(reason)
            if not core.is_frozen:
                if hard:
                    if self._libretro_auto_save:
                        core.save_state(slot=self._libretro_active_profile)
                    core.save_sram()
                core.freeze()
                log.info(f"[GAMEDECK] Paused ({reason})")
        else:
            self._pause_reasons.discard(reason)
            if not self._pause_reasons and core.is_frozen:
                if muted is None:
                    import sys as _sys
                    _main = _sys.modules.get("__main__")
                    muted = False
                    if _main and hasattr(_main, "db") and _main.db:
                        muted = _main.db.config.get("is_muted", False)
                core.unfreeze(muted=muted)
                log.info(f"[GAMEDECK] Resumed (last reason cleared: {reason})")
            elif self._pause_reasons and core.is_frozen:
                _stuck_sig = (reason, tuple(sorted(self._pause_reasons)))
                if getattr(self, "_last_stuck_pause_sig", None) != _stuck_sig:
                    self._last_stuck_pause_sig = _stuck_sig
                    log.info(f"[GAMEDECK] '{reason}' cleared but still frozen -- "
                          f"remaining pause reasons: {sorted(self._pause_reasons)}")

    DVD_LOAD_SCREEN_MS = 1600

    DVD_CUSTOM_SPLASH_MS = 3000

    DVD_SPLASH_VIDEO_EXTS = DVD_SPLASH_VIDEO_EXTS
    DVD_SPLASH_IMAGE_EXTS = DVD_SPLASH_IMAGE_EXTS

    DVD_SPLASH_VIDEO_MAX_MS = 60000

    DVD_ISO_OPEN_TIMEOUT_MS = 9000

    DVD_ISO_OPEN_FAIL_GRACE_MS = 700

    DVD_MENU_FLIP_DEBOUNCE_MS = 400

    DVD_END_STATE_GRACE_MS = 2500

    # ========================================================================
    # SECTION: Input: key translation, config refresh & events
    # ========================================================================

    def _dvd_translate_key(self, key):
        return _nav.dvd_translate(getattr(self, "dvd_bindings", None), key)

    def refresh_from_config(self, db_config):
        apply_stick_config(db_config)
        self.console_rom_dir = dict(db_config.get("console_rom_dir", {}) or {})
        manual_cfg = db_config.setdefault("console_roms_manual", {})

        ngp_paths = list(manual_cfg.get("NGP", []) or [])
        ngpc_paths = list(manual_cfg.get("NGPC", []) or [])
        migrated_ngp = []
        migrated_ngpc = []
        migration_changed = False
        for owner, paths in (("NGP", ngp_paths), ("NGPC", ngpc_paths)):
            for path in paths:
                ext = os.path.splitext(str(path))[1].lower()
                if ext == ".ngc":
                    target = "NGPC"
                elif ext == ".ngp":
                    target = "NGP"
                else:
                    target = owner
                if target != owner:
                    migration_changed = True
                if target == "NGPC":
                    migrated_ngpc.append(path)
                else:
                    migrated_ngp.append(path)

        def _dedupe_paths(paths):
            result = []
            seen = set()
            for path in paths:
                key = os.path.normcase(os.path.normpath(str(path)))
                if key in seen:
                    continue
                seen.add(key)
                result.append(path)
            return result

        migrated_ngp = _dedupe_paths(migrated_ngp)
        migrated_ngpc = _dedupe_paths(migrated_ngpc)
        if (migrated_ngp != ngp_paths
                or migrated_ngpc != ngpc_paths
                or "NGP" not in manual_cfg
                or "NGPC" not in manual_cfg):
            migration_changed = True
            manual_cfg["NGP"] = migrated_ngp
            manual_cfg["NGPC"] = migrated_ngpc

        self.console_roms_manual = {
            console: list(paths)
            for console, paths in manual_cfg.items()
        }

        self._refresh_consoles(db_config.get("consoles_enabled", {}))
        if self.active_console and self.active_console not in self.consoles:
            self.mode = "BROWSE"
            self.active_console = None
        stored = db_config.get("dvd_bindings", {}) or {}
        self.dvd_bindings = {
            action: stored.get(action, _nav.dvd_default_binding(action))
            for action in _nav.DVD_ACTIONS
        }

        self.dvd_splash_enabled = db_config.get("dvd_splash_enabled", False)
        self.dvd_splash_path = db_config.get("dvd_splash_path", "") or ""

        self.dvd_captions_enabled = db_config.get("dvd_captions_enabled", False)

        self.dvd_resume_enabled = db_config.get("dvd_resume_enabled", False)
        self.dvd_resume_positions = dict(db_config.get("dvd_resume_positions", {}) or {})

        _scroll_style = db_config.get("console_scroll_style", "carousel")
        self.scroll_style = _scroll_style if _scroll_style in CONSOLE_SCROLL_STYLES else "carousel"
        _text_color_name = db_config.get("menu_text_color", "white")
        self.list_text_color = LIST_TEXT_COLOR_RGB.get(_text_color_name, (255, 255, 255))
        if migration_changed:
            log.info("[ROM MIGRATION] Reassigned Neo Geo Pocket files by extension: "
                     "NGP=%d (.ngp), NGPC=%d (.ngc)",
                     len(migrated_ngp), len(migrated_ngpc))

        self.console_splash_enabled = dict(db_config.get("console_splash_enabled", {}) or {})
        self.console_splash_path    = dict(db_config.get("console_splash_path", {}) or {})

        self.active_bios = dict(db_config.get("active_bios", {}) or {})

        self.menu_bg_path = db_config.get("menu_bg_path", "") or ""

        if "MENU" not in self._libretro_xinput_map:
            self._build_default_libretro_xinput_map("MENU")
            self._apply_menu_nav_mapping_from_libretro_map()
        return migration_changed

    def handle_event(self, event):
        if self.mode == "GAME":
            return False

        if self.dvd_playback_active or self.mode == "DVD_PLAYER":
            if event.type != pygame.KEYDOWN:
                return False
            self.mode = "DVD_PLAYER"
            return self._handle_dvd_input(event.key)

        action = _nav.joystick_action(event)

        if event.type == pygame.KEYDOWN:
            _key_action = _nav.key_to_action(event.key)
            if _key_action is not None: action = _key_action

        if action is None:
            return False

        if self.mode == "BROWSE":
            _browse_move = _nav.deck_browse_step(self.scroll_style, action)
            if _browse_move is not None:
                self._cycle_console(_browse_move[0])
                self._flash_console_arrow(_browse_move[1])
            elif action == _nav.ACCEPT:    self._enter_romlist()
            return True

        if self.mode == "ROMLIST":
            if self.romlist_focus == "arrows":
                _arrows_move = _nav.deck_arrows_step(self.romlist_arrow_sel, action)
                if _arrows_move == _nav.DECK_ACTIVATE:
                    self._activate_romlist_arrow()
                elif _arrows_move == _nav.DECK_SELECT_UP:
                    self.romlist_arrow_sel = "up"
                elif _arrows_move == _nav.DECK_SELECT_DOWN:
                    self.romlist_arrow_sel = "down"
                elif _arrows_move == _nav.DECK_TO_LIST:
                    self.romlist_focus = "list"
                elif action == _nav.ACCEPT:
                    self._activate_romlist_arrow()
                elif action == _nav.BACK:
                    log.info("[GAMEDECK INFO] Escaped list. Returning to main console selector carousel.")
                    self.mode = "BROWSE"
                    self.romlist_focus = "list"
                return True

            _list_move = _nav.deck_list_step(action)
            if _list_move == _nav.DECK_PREV:
                if self.roms and self.rom_index > 0:
                    self.rom_index -= 1
                return True

            elif _list_move == _nav.DECK_NEXT:
                if self.roms and self.rom_index < len(self.roms) - 1:
                    self.rom_index += 1
                return True

            elif _list_move == _nav.DECK_TO_ARROWS:
                self.romlist_focus = "arrows"
                return True

            elif action == _nav.ACCEPT:
                self._launch_selected_rom()
                return True

            elif action == _nav.BACK:
                log.info("[GAMEDECK INFO] Escaped list. Returning to main console selector carousel.")
                self.mode = "BROWSE"
                self.romlist_focus = "list"
                return True

        return False

    # ========================================================================
    # SECTION: Input: ROM & DVD hold polling
    # ========================================================================

    def _poll_rom_hold(self):
        if not self.roms or self.romlist_focus != "list":
            return

        now  = pygame.time.get_ticks()
        matrix = pygame.key.get_pressed()

        _main_mod = sys.modules.get("__main__")
        _held = getattr(_main_mod, "app_state", {}).get("_held_keys", set()) if _main_mod else set()

        _up_keys   = _nav.action_keys(_nav.UP, arrows=True)
        _down_keys = _nav.action_keys(_nav.DOWN, arrows=True)
        held_up   = any(k in _held for k in _up_keys)
        held_down = any(k in _held for k in _down_keys)

        if held_up and held_down:
            _up_physical   = any(bool(matrix[k]) for k in _up_keys)
            _down_physical = any(bool(matrix[k]) for k in _down_keys)
            _drop = _nav.hold_conflict(_up_physical, _down_physical, both_pressed=_nav.DROP_BOTH)
            if _drop == _nav.DROP_BOTH and not _up_physical and not _down_physical:
                log.warning("[GAMEDECK NAV] SUSPECTED STUCK KEY: rom list saw both "
                            "up and down held via _held_keys with matrix=(up=%s, down=%s) "
                            "-- clearing both and stopping repeat", _up_physical, _down_physical)
            if _drop in (_nav.DROP_FIRST, _nav.DROP_BOTH):
                held_up = False
                for k in _up_keys:
                    _held.discard(k)
            if _drop in (_nav.DROP_SECOND, _nav.DROP_BOTH):
                held_down = False
                for k in _down_keys:
                    _held.discard(k)

        if not held_up and not held_down:
            self._rom_hold.clear()
            return

        direction = -1 if held_up else 1

        _hold_keys = _up_keys if held_up else _down_keys
        _hold_key = _nav.hold_pick(_held, _hold_keys) or _hold_keys[-1]
        _held_ms = now - self._rom_hold.start if self._rom_hold.key == direction else 0
        _step = _nav.hold_step(_nav.HOLD_RULES["rom_list"], self._rom_hold, direction, now,
                               bool(matrix[_hold_key]), False, _nav.HOLD_STUCK_MS["rom_list"])
        if _step == _nav.HOLD_STUCK:
            log.warning("[GAMEDECK NAV] SUSPECTED STUCK KEY: key=%s has been 'held' "
                        "for %sms via _held_keys only (matrix says NOT pressed) -- "
                        "self-healing by releasing it now", pygame.key.name(_hold_key), _held_ms)
            _held.discard(_hold_key)
            return
        if _step != _nav.HOLD_REPEAT:
            return

        if direction == -1 and self.rom_index > 0:
            self.rom_index -= 1
        elif direction == 1 and self.rom_index < len(self.roms) - 1:
            self.rom_index += 1

    def _poll_dvd_hold(self):
        if self.dvd_in_menu:
            self._dvd_skip_hold_start = 0
            self._dvd_skip_dir = 0
            return

        now = pygame.time.get_ticks()
        keys = pygame.key.get_pressed()
        dir_held = 0

        _b = getattr(self, "dvd_bindings", None) or {}
        _fwd_key = _b.get(_nav.DVD_FAST_FORWARD, _nav.dvd_default_binding(_nav.DVD_FAST_FORWARD))
        _rew_key = _b.get(_nav.DVD_REWIND, _nav.dvd_default_binding(_nav.DVD_REWIND))

        def _held(kc):
            try:
                return bool(keys[kc])
            except (IndexError, TypeError):
                return False

        dir_held = _nav.dvd_hold_direction(
            _held(_fwd_key) or _xi_nav_state.get("dvd_right_held", False),
            _held(_rew_key) or _xi_nav_state.get("dvd_left_held", False))

        if dir_held == 0:
            self._dvd_skip_hold_start = 0
            self._dvd_skip_dir = 0
            return

        if not getattr(self, "_dvd_skip_hold_start", 0):
            self._dvd_skip_hold_start = now
            self._dvd_skip_dir = dir_held

        held_ms = now - self._dvd_skip_hold_start
        cooldown = getattr(self, "_dvd_skip_cooldown", 0)
        if _nav.dvd_hold_due(held_ms, now, cooldown):
            vlc = self.vlc_engine
            if vlc:
                try:
                    t = vlc.player.get_time()
                    length = vlc.player.get_length()
                    new_t = _nav.dvd_hold_seek(dir_held, t, length)
                    vlc.player.set_time(new_t)
                    self._set_dvd_osd(">>" if dir_held > 0 else "<<")
                    log.info("[DVD SEEK] hold %s: %sms -> %sms (length=%sms)",
                              ">>" if dir_held > 0 else "<<", t, new_t, length)
                except Exception as e:
                    log.warning("DVD seek during held skip failed: %s", e)
            self.dvd_hud_until = now + 3000
            self._dvd_skip_cooldown = now + _nav.DVD_SKIP_COOLDOWN_MS

    # ========================================================================
    # SECTION: DVD on-screen display & input
    # ========================================================================

    def _set_dvd_osd(self, msg, duration_ms=2000):
        self.dvd_osd_msg = msg
        self.dvd_osd_until = pygame.time.get_ticks() + duration_ms

    _DVD_OSD_ICON_MAP = {
        "PLAYING":   "PLAY",
        "PAUSED":    "PAUSE",
        "<<":        "RW_FAST",
        ">>":        "FF_FAST",
        "<< -30s":   "RW",
        ">> +30s":   "FF",
    }

    def _draw_dvd_osd_icon(self, screen, cx, cy, icon_key, ui_col, radius=46):
        white = (255, 255, 255)

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
        vlc = self.vlc_engine
        if vlc is None:
            return True

        _dir = _nav.key_to_direction(key, arrows=True)
        if _dir is None and _nav.is_accept(key):
            _dir = "activate"

        if _dir is not None:
            vlc.navigate(_dir)
            if self._dvd_highlight_shadow is not None:
                self._dvd_highlight_shadow.mirror_navigate(_dir)
            if _dir == "activate":
                self._dvd_menu_manual_override = False

        return True

    def _dvd_menu_is_up(self):
        if not self.dvd_is_disc_image:
            return False
        if getattr(self, "_dvd_menu_manual_override", False):
            return True
        return bool(self.dvd_in_menu)

    def _handle_dvd_input(self, key):
        vlc = self.vlc_engine

        key = self._dvd_translate_key(key)

        _confirm_choice = _nav.deck_confirm_choice(_nav.key_to_direction(key))

        if self.dvd_exit_confirm:
            if _confirm_choice is not None:
                self.dvd_exit_selection = _confirm_choice
                self.dvd_exit_confirm_shown_at = pygame.time.get_ticks()
                log.info(f"[DVD INPUT] Quit-confirm: selection -> {_confirm_choice}")
            elif _nav.is_accept(key):
                log.info(f"[DVD INPUT] Quit-confirm: committing '{self.dvd_exit_selection}'")
                if self.dvd_exit_selection == "Yes":
                    if vlc:
                        try:
                            if (self.dvd_resume_enabled and self.dvd_file_path
                                    and not self.dvd_in_menu and not self.dvd_splash_playing):
                                _t_ms = vlc.player.get_time()
                                if _t_ms is not None and _t_ms > 0:
                                    self._dvd_save_resume_position(self.dvd_file_path, _t_ms / 1000.0)
                        except Exception as e:
                            log.warning("Failed to capture DVD position before close: %s", e)
                        try: vlc.stop()
                        except Exception as e:
                            log.warning("VLC stop failed while confirming DVD exit: %s", e)
                    self.dvd_exit_confirm = False
                    self.dvd_playback_active = False
                    self.dvd_splash_playing = False
                    self.dvd_splash_done = False
                    self._dvd_splash_is_video = False
                    self.dvd_paused_frame = None
                    self.dvd_in_menu = False
                    self._close_dvd_highlight_shadow()
                    self.mode = "ROMLIST"
                    self._scan_roms("DVD")
                else:
                    self.dvd_exit_confirm = False
            else:
                log.info(f"[DVD INPUT] Quit-confirm open: ignoring key {key} (only A/D/Enter are live).")
            return True

        if self.dvd_return_menu_confirm:
            if _confirm_choice is not None:
                self.dvd_return_menu_selection = _confirm_choice
                self.dvd_return_menu_confirm_shown_at = pygame.time.get_ticks()
                log.info(f"[DVD INPUT] Back-to-main-confirm: selection -> {_confirm_choice}")
            elif _nav.is_accept(key):
                log.info(f"[DVD INPUT] Back-to-main-confirm: committing '{self.dvd_return_menu_selection}'")
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
                    log.info("[DVD INPUT] Back-to-main confirmed "
                          "(WASD now controls menu navigation).")
                else:
                    self.dvd_return_menu_confirm = False
            else:
                log.info(f"[DVD INPUT] Back-to-main-confirm open: ignoring key {key} (only A/D/Enter are live).")
            return True

        if self.dvd_splash_playing:
            return True

        is_iso  = self.dvd_is_disc_image
        menu_up = self._dvd_menu_is_up()

        if _nav.is_back(key):
            self.dvd_exit_confirm = True
            self.dvd_exit_selection = "No"
            self.dvd_exit_confirm_shown_at = pygame.time.get_ticks()
            log.info(f"[DVD INPUT] ESC -> opening quit-to-TV confirm (is_iso={is_iso}).")
            return True

        if _nav.dvd_action(key) == _nav.DVD_RETURN_MENU:
            _return_step = _nav.dvd_return_menu_step(is_iso, menu_up)
            if _return_step == _nav.DVD_RETURN_IGNORE:
                log.info("[DVD INPUT] Backspace ignored (regular video file has no disc menu).")
                return True
            if _return_step == _nav.DVD_RETURN_REISSUE:
                self.dvd_in_menu = True
                self._dvd_menu_flip_candidate = True
                self._dvd_menu_flip_since = pygame.time.get_ticks()
                self._dvd_menu_manual_override = True
                if vlc:
                    try:
                        vlc.navigate("menu")
                    except Exception as e:
                        log.warning("DVD return-to-menu navigate failed: %s", e)
                log.info("[DVD INPUT] Backspace -- already on a menu, re-issued return-to-root.")
            else:
                self.dvd_return_menu_confirm = True
                self.dvd_return_menu_selection = "No"
                self.dvd_return_menu_confirm_shown_at = pygame.time.get_ticks()
                log.info("[DVD INPUT] Backspace -> opening back-to-main-menu confirm.")
            return True

        if _nav.key_to_direction(key) is not None or _nav.is_accept(key):
            if is_iso and menu_up:
                return self._handle_dvd_menu_nav(key)
            return True

        if _nav.dvd_swallows_transport(key, is_iso, menu_up):
            return True

        _transport = _nav.dvd_action(key, remote=True)

        if _transport == _nav.DVD_PLAY_PAUSE:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            if vlc:
                try:
                    vlc.player.pause()
                    self.dvd_paused = not self.dvd_paused
                    self._set_dvd_osd("PAUSED" if self.dvd_paused else "PLAYING")
                except Exception as e:
                    log.warning("DVD pause/resume toggle failed: %s", e)
            return True

        if _transport == _nav.DVD_FAST_FORWARD:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            _now_ff = pygame.time.get_ticks()
            if _nav.dvd_skip_blocked(_now_ff, getattr(self, "_dvd_skip_cooldown", 0)):
                return True
            if vlc:
                try:
                    t = vlc.player.get_time()
                    length = vlc.player.get_length()
                    new_t = _nav.dvd_tap_seek(1, t, length)
                    vlc.player.set_time(new_t)
                    self._set_dvd_osd(f">> +{_nav.DVD_SKIP_TAP_MS // 1000}s")
                    self._dvd_skip_hold_start = _now_ff
                    self._dvd_skip_dir = 1
                    self._dvd_skip_cooldown = _now_ff + _nav.DVD_SKIP_COOLDOWN_MS
                    log.info("[DVD SEEK] +30s: %sms -> %sms (length=%sms)", t, new_t, length)
                except Exception as e:
                    log.warning("DVD +30s skip failed: %s", e)
            return True

        if _transport == _nav.DVD_REWIND:
            self.dvd_hud_until = pygame.time.get_ticks() + 5000
            _now_rw = pygame.time.get_ticks()
            if _nav.dvd_skip_blocked(_now_rw, getattr(self, "_dvd_skip_cooldown", 0)):
                return True
            if vlc:
                try:
                    t = vlc.player.get_time()
                    new_t = _nav.dvd_tap_seek(-1, t, 0)
                    vlc.player.set_time(new_t)
                    self._set_dvd_osd(f"<< -{_nav.DVD_SKIP_TAP_MS // 1000}s")
                    self._dvd_skip_hold_start = _now_rw
                    self._dvd_skip_dir = -1
                    self._dvd_skip_cooldown = _now_rw + _nav.DVD_SKIP_COOLDOWN_MS
                    log.info("[DVD SEEK] -30s: %sms -> %sms", t, new_t)
                except Exception as e:
                    log.warning("DVD -30s skip failed: %s", e)
            return True

        log.info(f"[DVD INPUT] Ignored key {key} (no DVD action bound to it).")
        return True

    # ========================================================================
    # SECTION: Console carousel & ROM list navigation
    # ========================================================================

    def _cycle_console(self, delta):
        if not self.consoles: return
        self.console_index = (self.console_index + delta) % len(self.consoles)

    def _flash_console_arrow(self, direction):
        """Briefly highlight the console-switch arrow for `direction` after it is pressed."""
        self._console_arrow_flash_dir   = direction
        self._console_arrow_flash_until = pygame.time.get_ticks() + self._CONSOLE_ARROW_FLASH_MS

    def _enter_romlist(self):
        if not self.consoles: return
        console = self.consoles[self.console_index]
        self.active_console = console
        if console == "DVD":
            self.current_browse_dir = getattr(self, "dvd_last_dir", "")
        else:
            self.current_browse_dir = self._get_console_rom_dir(console)
        self._scan_roms(console)
        self._snap_rom_cursor_if_scan_landed()
        self.romlist_focus = "list"
        self.mode = "ROMLIST"

    def _first_content_index(self):
        if not self.roms:
            return 0
        if self.roms[0][0].startswith("[.. "):
            return 1 if len(self.roms) > 1 else 0
        return 0

    def _dvd_folder_has_content(self, folder_path, exts):
        try:
            with os.scandir(folder_path) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            return True
                        if entry.is_file():
                            ext = os.path.splitext(entry.name.lower())[1]
                            if ext in exts:
                                return True
                    except OSError:
                        continue
        except OSError:
            pass
        return False

    # ========================================================================
    # SECTION: ROM & core scanning
    # ========================================================================

    def _get_windows_drives(self):
        import ctypes
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if bitmask & 1:
                drives.append(f"{letter}:\\")
            bitmask >>= 1
        return drives

    def _normalize_rom_title(self, filename):
        """Normalize a rom filename to a duplicate-detection key: strip extension and (region)/[tag] notes, lowercase, collapse whitespace."""
        base = os.path.splitext(os.path.basename(filename))[0]
        key = self._clean_rom_display_name(str(base)).strip().lower()
        disc_match = _DISC_MARKER_RE.search(base)
        if disc_match:
            key = f"{key}|disc{disc_match.group(1)}"
        return key

    def add_manual_roms(self, console, paths):
        """Append file paths to this console's manually-added ROM list."""
        existing_paths = set(self.console_roms_manual.get(console, []))
        existing_titles = {self._normalize_rom_title(p) for p in existing_paths}

        try:
            rom_dir = self._get_console_rom_dir(console)
            if _gd_is_drive_responsive(rom_dir) and os.path.isdir(rom_dir):
                for entry in os.scandir(rom_dir):
                    if entry.is_file():
                        existing_titles.add(self._normalize_rom_title(entry.name))
        except Exception:
            pass

        new_paths = []
        skipped = 0
        for p in paths:
            if not p:
                continue
            if p in existing_paths:
                skipped += 1
                continue
            title = self._normalize_rom_title(p)
            if title in existing_titles:
                skipped += 1
                continue
            existing_titles.add(title)
            new_paths.append(p)

        if new_paths:
            self.console_roms_manual.setdefault(console, [])
            self.console_roms_manual[console].extend(new_paths)
        return len(new_paths), skipped

    def bulk_scan_for_roms(self, parent_folder, max_depth=4, progress_cb=None):
        """"Scan For Roms": walk `parent_folder` for console-named subfolders and collect their ROMs."""
        matches = {}
        unresponsive_roots = set()

        def _candidate_files(folder, console):
            """Return every file under `folder` whose extension belongs to `console`; no archive-content check."""
            exts = set(ROM_EXTENSIONS.get(console, []))
            found = []
            if not _gd_is_drive_responsive(folder):
                unresponsive_roots.add(_gd_drive_root_of(folder))
                return found
            for root, dirs, files in os.walk(folder):
                if not _gd_is_drive_responsive(root):
                    unresponsive_roots.add(_gd_drive_root_of(root))
                    dirs[:] = []
                    continue
                for fn in files:
                    if fn.startswith(".") or fn.startswith("$"):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if not exts or ext in exts:
                        found.append(os.path.join(root, fn))
            return found

        def _iter_matched_folders(folder, depth):
            """Yield (folder, console) for each folder under `folder` matching a console name; name matching only, no file access."""
            if depth > max_depth:
                return
            if not _gd_is_drive_responsive(folder):
                unresponsive_roots.add(_gd_drive_root_of(folder))
                return
            base_name = os.path.basename(folder.rstrip("\\/"))
            console = match_console_for_folder_name(base_name)
            if console:
                yield (folder, console)
                return
            try:
                entries = list(os.scandir(folder))
            except Exception:
                return
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")):
                        yield from _iter_matched_folders(entry.path, depth + 1)
                except OSError:
                    continue

        candidates_by_folder = []
        total = 0
        for folder, console in _iter_matched_folders(parent_folder, 0):
            files = _candidate_files(folder, console)
            if files:
                candidates_by_folder.append((console, files))
                total += len(files)

        if progress_cb:
            progress_cb(0, total)

        done = 0
        for console, files in candidates_by_folder:
            kept = []
            for full_path in files:
                ext = os.path.splitext(full_path)[1].lower()
                if ext not in (".zip", ".7z", ".rar") or _archive_matches_console(full_path, console):
                    kept.append(full_path)
                done += 1
                if progress_cb:
                    progress_cb(done, total)
            if kept:
                matches.setdefault(console, []).extend(kept)

        results = {}
        for console, paths in matches.items():
            n_added, n_skipped = self.add_manual_roms(console, paths)
            if n_added or n_skipped:
                results[console] = (n_added, n_skipped)
        return results, sorted(r for r in unresponsive_roots if r)

    def bulk_scan_for_cores(self, parent_folder, max_depth=6):
        """"Scan For Cores": walk `parent_folder` for *_libretro.dll files and copy each match into main/cores/<CONSOLE>/."""
        found = {}
        unresponsive_roots = set()

        def _walk(folder, depth):
            if depth > max_depth:
                return
            if not _gd_is_drive_responsive(folder):
                unresponsive_roots.add(_gd_drive_root_of(folder))
                return
            try:
                entries = list(os.scandir(folder))
            except Exception:
                return
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")):
                        _walk(entry.path, depth + 1)
                    elif entry.is_file() and entry.name.lower().endswith("_libretro.dll"):
                        for console in match_consoles_for_core_filename(entry.name):
                            found.setdefault(console, []).append(entry.path)
                except OSError:
                    continue

        _walk(parent_folder, 0)

        results = {}
        for console, src_paths in found.items():
            dest_dir = os.path.join(self.cores_dir, console)
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except Exception as e:
                log.warning(f"[CORES] Could not create {dest_dir}: {e}")
                continue
            n_new, n_present = 0, 0
            for src in src_paths:
                dest = os.path.join(dest_dir, os.path.basename(src))
                if os.path.exists(dest):
                    n_present += 1
                    continue
                try:
                    shutil.copy2(src, dest)
                    n_new += 1
                    log.info(f"[CORES] Copied {os.path.basename(src)} -> {dest_dir}")
                except Exception as e:
                    log.warning(f"[CORES] Could not copy {src} -> {dest}: {e}")
            if n_new or n_present:
                results[console] = (n_new, n_present)

        if results:
            self._assign_default_cores_after_scan(list(results.keys()))
        return results, sorted(r for r in unresponsive_roots if r)

    def _assign_default_cores_after_scan(self, consoles):
        """Give each console without an active core in emulator_config.json the best match in its folder per PREFERRED_CORE_ORDER."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
        except Exception:
            cfg = {}
        changed = False
        for console in consoles:
            if cfg.get(console):
                continue
            dest_dir = os.path.join(self.cores_dir, console)
            try:
                present = [f for f in os.listdir(dest_dir) if f.lower().endswith(".dll")]
            except Exception:
                present = []
            if not present:
                continue
            chosen = resolve_preferred_core_filename(console, present)
            if not chosen:
                continue
            cfg[console] = chosen
            changed = True
            log.info(f"[CORES] {console}: default active core set to {chosen}")
        if changed:
            try:
                with open(self.config_path, "w", encoding="utf-8") as fp:
                    json.dump(cfg, fp, indent=2)
            except Exception as e:
                log.warning(f"[CORES] Could not save {self.config_path}: {e}")

    def _scan_roms(self, console):
        target_folder = getattr(self, "current_browse_dir", "")

        self._last_live_rescan_ticks = pygame.time.get_ticks()

        is_background = self._scan_is_background
        self._scan_is_background = False

        if console == "DVD":
            self.dvd_last_dir = target_folder

        exts = ROM_EXTENSIONS.get(console, [])

        if console == "DVD" and target_folder == "":
            self.roms = []
            drives_pool = self._get_windows_drives()
            for drive in drives_pool:
                if is_optical_drive_root(drive):
                    info = get_disc_info(drive)
                    letter = drive.rstrip("\\")
                    if info.get("has_video_ts"):
                        shown = info.get("label") or "DVD Drive"
                        self.roms.append((f"[DVD] {shown} ({letter})", drive))
                    else:
                        shown = info.get("label") or "Optical Drive"
                        self.roms.append((f"[DRIVE] {shown} ({letter})", drive))
                else:
                    self.roms.append((f"[DRIVE] {drive}", drive))
            self.status_msg = ""
            self._scan_pending = False
            if self.roms:
                self.rom_index = min(self.rom_index, len(self.roms) - 1)
            else:
                self.rom_index = 0
            pygame.event.clear(pygame.KEYDOWN)
            return

        self._scan_request_id += 1
        req_id = self._scan_request_id
        with self._scan_lock:
            self._scan_result = None
        self._scan_pending = True
        self._scan_started_ticks = pygame.time.get_ticks()
        self._pending_scan_is_background = is_background

        self._scan_thread = threading.Thread(
            target=self._scan_roms_worker,
            args=(req_id, console, target_folder, exts),
            daemon=True,
        )
        self._scan_thread.start()

    _ROM_TAG_RE = re.compile(
        r"\s*[\(\[](?!\s*(?:disc|disk|cd)\s*\d)[^\(\)\[\]]*[\)\]]",
        re.IGNORECASE,
    )

    _LIVE_RESCAN_INTERVAL_MS = 1500

    @classmethod
    def _clean_rom_display_name(cls, name):
        cleaned = cls._ROM_TAG_RE.sub("", name).strip(" -_")
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return cleaned if cleaned else name

    def _scan_roms_worker(self, req_id, console, target_folder, exts):
        roms = []
        status_msg = ""

        manual_entries = []
        if console != "DVD":
            manual_paths = self.console_roms_manual.get(console, [])
            if manual_paths:
                fallback_root_chk = self._get_console_rom_dir(console)
                if os.path.abspath(target_folder) == os.path.abspath(fallback_root_chk):
                    for p in manual_paths:
                        base_name_only = os.path.splitext(os.path.basename(p))[0]
                        clean_name = self._clean_rom_display_name(str(base_name_only))
                        if not _gd_is_drive_responsive(p):
                            # WARNING: do not call os.path.exists() on an unresponsive drive path (it can block) and do not drop the path from the list.
                            shown = "[OFFLINE] " + clean_name
                            manual_entries.append((shown, p))
                            continue
                        if not os.path.exists(p):
                            shown = "[MISSING] " + clean_name
                            manual_entries.append((shown, p))
                            continue
                        manual_entries.append((clean_name, p))

        try:
            if _gd_is_drive_responsive(target_folder) and os.path.exists(target_folder):
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
                    fallback_root = self._get_console_rom_dir(console)
                    if os.path.abspath(target_folder) != os.path.abspath(fallback_root):
                        parent_path = os.path.dirname(target_folder)
                        dir_entries.append(("[.. BACK TO PREVIOUS FOLDER]", parent_path))

                with os.scandir(target_folder) as _it:
                    scanned = sorted(_it, key=lambda e: e.name.lower())
                for entry in scanned:
                    name = entry.name
                    full_path = entry.path
                    if name.startswith("$") or name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    if is_dir:
                        if console == "DVD":
                            if self._dvd_folder_has_content(full_path, set(exts)):
                                dir_entries.append((f"[DIR] {name}", full_path))
                        continue
                    base_name_only, ext_only = os.path.splitext(name)
                    ext_str = str(ext_only).lower()
                    if not exts or ext_str in exts:
                        if (console != "DVD" and ext_str in (".zip", ".7z", ".rar")
                                and not _archive_matches_console(full_path, console)):
                            continue
                        _shown_name = (self._clean_rom_display_name(str(base_name_only))
                                       if console != "DVD" else str(base_name_only))
                        file_entries.append((_shown_name, full_path))

                if console == "PSX" and file_entries:
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

                roms = dir_entries + manual_entries + file_entries
                status_msg = "" if roms else f"No files found in: {console}"
            elif manual_entries:
                roms = manual_entries
                status_msg = ""
        except Exception as e:
            status_msg = "Access Error"
            log.warning(f"[GAMEDECK CRITICAL BUG] Directory index scan failed! Error details: {e}")
        else:
            if not os.path.exists(target_folder) and not manual_entries:
                _log_warn_throttled("scan_folder_missing", f"_scan_roms_worker({console}): folder does not exist: "
                      f"{target_folder!r} -- nothing to scan (check the console's ROM folder "
                      f"was actually created / spelled correctly under main/roms/{console})")

        with self._scan_lock:
            if req_id == self._scan_request_id:
                self._scan_result = (roms, status_msg)

    def _refresh_dvd_root_labels(self):
        if self.active_console != "DVD" or self.current_browse_dir != "":
            return
        for i, (display_name, full_path) in enumerate(self.roms):
            if display_name.startswith("[DRIVE] ") and is_optical_drive_root(full_path):
                drive_root = str(full_path)[0] + ":\\"
                info = get_disc_info(drive_root)
                if info.get("has_video_ts"):
                    letter = drive_root.rstrip("\\")
                    shown = info.get("label") or "DVD Drive"
                    self.roms[i] = (f"[DVD] {shown} ({letter})", drive_root)

    def _poll_scan_pending(self):
        if not self._scan_pending:
            return

        with self._scan_lock:
            result = self._scan_result

        if result is not None:
            roms, status_msg = result
            is_background = getattr(self, "_pending_scan_is_background", False)

            if is_background:
                old_path = None
                if self.roms and 0 <= self.rom_index < len(self.roms):
                    old_path = self.roms[self.rom_index][1]
                self.roms = roms
                self.status_msg = status_msg
                if not self.roms:
                    self.rom_index = 0
                    self.rom_scroll = 0
                else:
                    new_index = next(
                        (i for i, (_, p) in enumerate(self.roms) if p == old_path),
                        None,
                    )
                    self.rom_index = new_index if new_index is not None \
                        else min(self.rom_index, len(self.roms) - 1)
                    self.rom_scroll = min(self.rom_scroll, max(0, len(self.roms) - 1))
                self._scan_pending = False
                return

            self.roms = roms
            self.status_msg = status_msg
            self.rom_index = self._first_content_index()
            self.rom_scroll = 0
            self._scan_pending = False
            pygame.event.clear(pygame.KEYDOWN)
            return

        elapsed = pygame.time.get_ticks() - self._scan_started_ticks
        if elapsed > self._scan_timeout_ms:
            self._scan_request_id += 1
            self._scan_pending = False
            pygame.event.clear(pygame.KEYDOWN)

    def _poll_live_rescan(self):
        if self._scan_pending or not self.active_console:
            return
        now = pygame.time.get_ticks()
        if now - self._last_live_rescan_ticks < self._LIVE_RESCAN_INTERVAL_MS:
            return
        self._scan_is_background = True
        self._scan_roms(self.active_console)

    def _activate_romlist_arrow(self):
        if len(self.consoles) > 1:
            delta = -1 if self.romlist_arrow_sel == "up" else 1
            self._cycle_console(delta)
            console = self.consoles[self.console_index]
            self.active_console = console
            self.current_browse_dir = "" if console == "DVD" else self._get_console_rom_dir(console)
            self._scan_roms(console)
            self._snap_rom_cursor_if_scan_landed()

    def _snap_rom_cursor_if_scan_landed(self):
        if not self._scan_pending:
            self.rom_index = self._first_content_index()
            self.rom_scroll = 0

    # ========================================================================
    # SECTION: Launching the selected ROM
    # ========================================================================

    def _launch_selected_rom(self):
        if not self.roms: return
        display_name, full_path = self.roms[self.rom_index]

        if display_name == "[.. BACK TO THIS PC]":
            self.current_browse_dir = ""
            self._scan_roms(self.active_console)
            self._snap_rom_cursor_if_scan_landed()
            return

        if display_name == "[.. BACK TO PREVIOUS FOLDER]":
            self.current_browse_dir = full_path
            self._scan_roms(self.active_console)
            self._snap_rom_cursor_if_scan_landed()
            return

        if display_name.startswith("[DVD] "):
            self._start_dvd_playback(full_path)
            pygame.event.clear(pygame.KEYDOWN)
            return

        if display_name.startswith("[DRIVE] ") or display_name.startswith("[DIR] "):
            self.current_browse_dir = full_path
            self._scan_roms(self.active_console)
            self._snap_rom_cursor_if_scan_landed()
            return

        console = self.active_console
        if console == "DVD":
            self._start_dvd_playback(full_path)
            pygame.event.clear(pygame.KEYDOWN)
            return

        if not _gd_is_drive_responsive(full_path):
            self._romlist_error_msg   = "Drive not responding — can't launch this rom right now"
            self._romlist_error_until = pygame.time.get_ticks() + 4000
            pygame.event.clear(pygame.KEYDOWN)
            return
        if not os.path.exists(full_path):
            self._romlist_error_msg   = "Rom file not found — it may have been moved or deleted"
            self._romlist_error_until = pygame.time.get_ticks() + 4000
            pygame.event.clear(pygame.KEYDOWN)
            return

        _core_dll = self._has_libretro_core(console)
        _use_libretro = bool(_LIBRETRO_AVAILABLE and _core_dll)
        if _use_libretro:
            self._launch_libretro(console, full_path)
        else:
            exe = self._resolve_emulator_exe(console)
            if exe:
                self._launch_emulator(exe, full_path, console)
            else:
                log.warning(f"[LIBRETRO DBG] {console}: no libretro core AND no fallback .exe "
                      f"found -- nothing to launch this ROM with. Check that "
                      f"main/cores/{console}/ contains a *_libretro.dll (preferred) or "
                      f"that an external emulator .exe is configured for this console.")

    # ========================================================================
    # SECTION: DVD captions
    # ========================================================================

    def _apply_dvd_captions_state(self, enable, vlc, silent=True):
        """Turn DVD captions on or off against the given VLC engine, using the subtitle tracks VLC already lists."""
        applied = False
        self.dvd_captions = enable
        if vlc is None:
            return applied
        try:
            if not enable:
                vlc.player.video_set_spu(-1)
                try:
                    vlc._ensure_menu_highlight_spu()
                except Exception as _hl_e:
                    log.warning("DVD captions-off highlight re-check failed: %s", _hl_e)
            else:
                track_ids = self._dvd_caption_spu_ids(vlc)
                if track_ids:
                    vlc.player.video_set_spu(track_ids[0])
                    applied = True
                else:
                    self.dvd_captions = False
        except Exception as e:
            log.warning(f"[DVD CAPTIONS ERROR] {e}")
        return applied

    def _dvd_caption_spu_ids(self, vlc):
        """Ids of the subtitle tracks VLC lists right now, without the off entry."""
        try:
            description = vlc.player.video_get_spu_description()
        except Exception as e:
            log.warning("[DVD CAPTIONS] Could not read VLC's subtitle tracks: %s", e)
            return []
        return [track_id for track_id, _name in (description or []) if track_id != -1]

    def _dvd_caption_flow_new(self, now):
        """Starts the per-title caption state; a video file also starts its caption-source lookup on a thread."""
        flow = {"stage": "fallback", "path": self.dvd_file_path, "started": now,
                "result": None, "slave": None, "deadline": 0, "next": 0, "legacy_tried": False}
        if self.dvd_is_disc_image or not self.dvd_file_path:
            return flow
        flow["stage"] = "lookup"
        threading.Thread(target=self._dvd_caption_lookup_job, args=(flow,), daemon=True,
                         name="DVDCaptionsLookup").start()
        return flow

    @staticmethod
    def _dvd_caption_lookup_job(flow):
        """Thread body: finds where this file's captions come from and stores the answer in the flow."""
        try:
            flow["result"] = resolve_caption_source(flow["path"])
        except Exception as e:
            log.warning("[DVD CAPTIONS] Caption lookup failed for %s: %s", flow["path"], e)
            flow["result"] = {"kind": None, "srt_path": None}

    def _dvd_caption_slave_begin(self, vlc, flow, path, now):
        """Hands a caption file to VLC and starts waiting, without blocking, for its new track to be listed."""
        before = self._dvd_caption_spu_ids(vlc)
        try:
            import vlc as _vlc_mod
            file_uri = 'file:///' + path.replace('\\', '/').lstrip('/')
            vlc.player.add_slave(_vlc_mod.MediaSlaveType.subtitle, file_uri, True)
        except Exception as sub_err:
            _log_warn_throttled("dvd_captions_sidecar", f"[DVD CAPTIONS] Sidecar load error: {sub_err}")
            return False
        flow["slave"] = {"path": path, "before": before,
                         "deadline": now + _gov.DVD_CAPTIONS_TRACK_TIMEOUT_MS,
                         "next": now + _gov.DVD_CAPTIONS_POLL_MS}
        return True

    def _dvd_caption_slave_step(self, vlc, flow, now):
        """Checks once for the track a caption file added; returns "wait", "selected" or "failed"."""
        slave = flow["slave"]
        if now < slave["next"]:
            return "wait"
        slave["next"] = now + _gov.DVD_CAPTIONS_POLL_MS
        added = [t for t in self._dvd_caption_spu_ids(vlc) if t not in slave["before"]]
        if added:
            flow["slave"] = None
            try:
                vlc.player.video_set_spu(added[-1])
            except Exception as e:
                log.warning("[DVD CAPTIONS] Could not select the new track: %s", e)
                flow["stage"] = "fallback"
                return "failed"
            self.dvd_captions = True
            return "selected"
        if now >= slave["deadline"]:
            log.warning("[DVD CAPTIONS] VLC never listed a track for %s; using the old flow",
                        os.path.basename(slave["path"]))
            flow["slave"] = None
            flow["stage"] = "fallback"
            return "failed"
        return "wait"

    def _dvd_caption_source_step(self, vlc, flow, now):
        """Advances the lookup and the choice that follows it; True while it owns the frame, False when the old flow should run."""
        if flow["stage"] == "lookup":
            result = flow["result"]
            if result is None:
                if now - flow["started"] < _gov.DVD_CAPTIONS_LOOKUP_TIMEOUT_MS:
                    return True
                log.warning("[DVD CAPTIONS] Caption lookup for %s took over %d ms; using the old flow",
                            os.path.basename(flow["path"]), _gov.DVD_CAPTIONS_LOOKUP_TIMEOUT_MS)
                flow["stage"] = "fallback"
                return False
            kind = result.get("kind")
            log.info("[DVD CAPTIONS] %s: caption source = %s", os.path.basename(flow["path"]), kind)
            if kind == "baked":
                self._dvd_cap_suppress = {"path": flow["path"],
                                          "until": now + _gov.DVD_CAPTIONS_SUPPRESS_WATCH_MS, "next": 0}
                self.dvd_captions = False
                self._dvd_captions_auto_done = True
                self._dvd_captions_auto_pending = False
                return True
            if kind in ("sidecar", "cache") and result.get("srt_path"):
                if self._dvd_caption_slave_begin(vlc, flow, result["srt_path"], now):
                    flow["stage"] = "sidecar"
                    return True
                flow["stage"] = "fallback"
                return False
            if kind == "embedded":
                flow["stage"] = "embedded"
                flow["deadline"] = now + _gov.DVD_CAPTIONS_TRACK_TIMEOUT_MS
                flow["next"] = 0
            else:
                flow["stage"] = "fallback"
                return False
        if flow["stage"] == "embedded":
            if now < flow["next"]:
                return True
            flow["next"] = now + _gov.DVD_CAPTIONS_POLL_MS
            track_ids = self._dvd_caption_spu_ids(vlc)
            if track_ids:
                try:
                    vlc.player.video_set_spu(track_ids[0])
                except Exception as e:
                    log.warning("[DVD CAPTIONS] Could not select the embedded track: %s", e)
                    flow["stage"] = "fallback"
                    return False
                self.dvd_captions = True
                self._dvd_captions_auto_done = True
                self._dvd_captions_auto_pending = False
                return True
            if now >= flow["deadline"]:
                log.warning("[DVD CAPTIONS] VLC lists no embedded track for %s; using the old flow",
                            os.path.basename(flow["path"]))
                flow["stage"] = "fallback"
                return False
            return True
        return False

    def _dvd_suppress_vlc_subtitles(self, vlc):
        """Keeps VLC's own subtitle track off for a while after a burned-in file starts."""
        state = self._dvd_cap_suppress
        if state is None or vlc is None:
            return
        if not self.dvd_captions_enabled or self.dvd_file_path != state["path"]:
            self._dvd_cap_suppress = None
            return
        now = pygame.time.get_ticks()
        if now > state["until"]:
            self._dvd_cap_suppress = None
            return
        if now < state["next"]:
            return
        state["next"] = now + _gov.DVD_CAPTIONS_SUPPRESS_POLL_MS
        try:
            spu = vlc.player.video_get_spu()
            if spu is not None and spu >= 0:
                vlc.player.video_set_spu(-1)
                log.info("[DVD CAPTIONS] VLC's own subtitle track switched off for %s (burned-in captions)",
                         os.path.basename(state["path"]))
        except Exception as e:
            log.warning("[DVD CAPTIONS] Could not switch VLC's subtitle track off: %s", e)

    def _start_dvd_captions_asr_job(self, video_path):
        """Run background transcription for video_path on a daemon thread and store the result in self._dvd_captions_asr_srt_path and self._dvd_captions_asr_state."""
        # WARNING: never block the render/VLC-callback thread that _maybe_auto_apply_dvd_captions ticks from every frame.
        def _job():
            srt_path = None
            try:
                with _asr_slot(f"dvd_captions({os.path.basename(video_path)})"):
                    srt_path, _reason = generate_captions_via_asr_chunked(
                        video_path, start_sec=getattr(self, "_dvd_resume_seek_seconds", 0) or 0,
                        should_stop=lambda: self.dvd_file_path != video_path,
                    )
            except Exception as e:
                log.warning(f"[DVD CAPTIONS] ASR job failed for {video_path}: {e}")
            finally:
                # WARNING: apply the result only while the same title is still active.
                if self.dvd_file_path == video_path:
                    self._dvd_captions_asr_srt_path = srt_path
                    self._dvd_captions_asr_state = "done"
        threading.Thread(target=_job, daemon=True, name="DVDCaptionsASR").start()

    def _maybe_auto_apply_dvd_captions(self, vlc):
        """Per-frame tick that turns DVD captions on once, shortly after a title starts, when the DVD Captions toggle is on."""
        # WARNING: nothing in this tick may block; it runs every frame on the render thread.
        self._dvd_suppress_vlc_subtitles(vlc)
        if self._dvd_captions_auto_done or not self._dvd_captions_auto_pending:
            self._dvd_cap_flow = None
            return
        if vlc is None or self.dvd_in_menu or not self.dvd_splash_done:
            return
        now = pygame.time.get_ticks()
        flow = self._dvd_cap_flow
        if flow is None:
            flow = self._dvd_cap_flow = self._dvd_caption_flow_new(now)

        if flow["slave"] is not None:
            outcome = self._dvd_caption_slave_step(vlc, flow, now)
            if outcome == "wait":
                return
            if outcome == "selected":
                self._dvd_captions_auto_done = True
                self._dvd_captions_auto_pending = False
                return
        elif self._dvd_caption_source_step(vlc, flow, now):
            return

        if self._dvd_captions_asr_state == "done":
            srt_path = self._dvd_captions_asr_srt_path
            if srt_path and self._dvd_caption_slave_begin(vlc, flow, srt_path, now):
                self._dvd_captions_asr_state = "applying"
                return
            self._dvd_captions_auto_done = True
            self._dvd_captions_auto_pending = False
            return

        if (self._dvd_captions_asr_state == "idle"
                and not self.dvd_is_disc_image and self.dvd_file_path):
            self._dvd_captions_asr_state = "running"
            self._start_dvd_captions_asr_job(self.dvd_file_path)

        if now < self._dvd_captions_auto_next_try:
            return
        if not self._dvd_captions_auto_deadline:
            self._dvd_captions_auto_deadline = now + _gov.DVD_CAPTIONS_TRACK_TIMEOUT_MS
        applied = self._apply_dvd_captions_state(True, vlc, silent=True)
        if applied:
            self._dvd_captions_auto_done = True
            self._dvd_captions_auto_pending = False
            return
        if not flow["legacy_tried"] and self.dvd_file_path:
            flow["legacy_tried"] = True
            base = os.path.splitext(self.dvd_file_path)[0]
            for sub_ext in ['.srt', '.ass', '.ssa', '.vtt', '.sub', '.smi']:
                candidate = base + sub_ext
                if os.path.exists(candidate):
                    if self._dvd_caption_slave_begin(vlc, flow, candidate, now):
                        return
                    break
        if now >= self._dvd_captions_auto_deadline:
            if self._dvd_captions_asr_state != "running":
                self._dvd_captions_auto_done = True
                self._dvd_captions_auto_pending = False
        else:
            self._dvd_captions_auto_next_try = now + _gov.DVD_CAPTIONS_POLL_MS

    # ========================================================================
    # SECTION: DVD resume position
    # ========================================================================

    def _dvd_resume_save_to_db(self):
        """Persist self.dvd_resume_positions back to db.config + disk."""
        import sys
        main_module = sys.modules.get("__main__")
        db = getattr(main_module, "db", None) if main_module else None
        if db is None:
            return
        db.config["dvd_resume_positions"] = dict(self.dvd_resume_positions)
        try:
            db.save_settings()
        except Exception as e:
            log.warning("Failed to save dvd_resume_positions: %s", e)

    def _dvd_get_resume_seconds(self, video_path):
        """Return the saved resume position in seconds for video_path, or 0 if resume is off, nothing is saved, or it is too close to the start."""
        if not self.dvd_resume_enabled or not video_path:
            return 0
        pos = self.dvd_resume_positions.get(video_path)
        if not pos or pos < 5:
            return 0
        return pos

    def _dvd_save_resume_position(self, video_path, seconds):
        """Record how far video_path got so it can resume; clears any entry when the position is too close to the start."""
        if not video_path:
            return
        if not seconds or seconds < 5:
            self._dvd_clear_resume_position(video_path)
            return
        self.dvd_resume_positions[video_path] = seconds
        self._dvd_resume_save_to_db()

    def _dvd_clear_resume_position(self, video_path):
        if video_path and video_path in self.dvd_resume_positions:
            del self.dvd_resume_positions[video_path]
            self._dvd_resume_save_to_db()

    # ========================================================================
    # SECTION: DVD playback start
    # ========================================================================

    def _start_dvd_playback(self, video_path):
        import sys
        main_module = sys.modules.get("__main__")

        if main_module and hasattr(main_module, "initialize_vlc_on_demand"):
            main_module.initialize_vlc_on_demand()

        vlc = getattr(main_module, "dvd_engine", None)

        if vlc is None:
            log.warning("[GAMEDECK BUG ERROR] Playback blocked: dvd_engine link is currently None!")
            return

        self.vlc_engine = vlc
        self.dvd_file_path = video_path
        self._dvd_resume_seek_seconds = self._dvd_get_resume_seconds(video_path)
        self.mode = "DVD_PLAYER"
        self.dvd_playback_active = True

        self.dvd_paused = False
        self.dvd_paused_frame = None
        self.dvd_captions = False
        _dvd_caps_on = bool(self.dvd_captions_enabled) and not _dvd_captions_locked()
        self._dvd_captions_auto_pending = _dvd_caps_on
        self._dvd_captions_auto_done = not _dvd_caps_on
        self._dvd_captions_auto_deadline = 0
        self._dvd_captions_auto_next_try = 0
        self._dvd_captions_asr_state = "idle"
        self._dvd_captions_asr_srt_path = None
        self._dvd_cap_flow = None
        self._dvd_cap_suppress = None
        self.dvd_exit_selection = "No"
        self.dvd_return_menu_confirm = False
        self.dvd_return_menu_selection = "No"
        self.dvd_osd_msg = ""
        self.dvd_osd_until = 0

        self.dvd_is_disc_image = is_dvd_disc_source(video_path)
        self.dvd_in_menu = self.dvd_is_disc_image
        self._dvd_menu_flip_since = 0
        self._dvd_menu_flip_candidate = None
        self._dvd_menu_manual_override = False
        self.dvd_was_in_menu_on_leave = False
        self._dvd_iso_watchdog_armed = False
        self._dvd_iso_watchdog_deadline = 0

        try: vlc.stop()
        except Exception as e:
            log.warning("VLC stop before DVD load screen failed: %s", e)
        try: vlc.clear_frame_buffer()
        except Exception as e:
            log.warning("VLC clear_frame_buffer before DVD load screen failed: %s", e)

        self._dvd_splash_is_video = False
        if self.dvd_splash_enabled and self.dvd_splash_path:
            if self._dvd_path_is_video(self.dvd_splash_path) and os.path.exists(self.dvd_splash_path):
                try:
                    _handed = vlc.play_file_segmented(self.dvd_splash_path, 0, gain=1.0)
                    self._dvd_splash_is_video = bool(_handed)
                    if _handed:
                        log.info(f"[DVD] Playing custom splash video: {os.path.basename(self.dvd_splash_path)}")
                    else:
                        log.warning("DVD splash video was not accepted by VLC, falling back to default load screen.")
                except Exception as e:
                    log.warning("Failed to start DVD splash video %s: %s", self.dvd_splash_path, e)
                    self._dvd_splash_is_video = False

        self.dvd_splash_playing = True
        self.dvd_splash_done = False
        self._dvd_splash_start_time = pygame.time.get_ticks()
        self._dvd_main_ever_played = False
        log.info("[DVD] Showing load screen before starting playback.")

    def _close_dvd_highlight_shadow(self):
        if self._dvd_highlight_shadow is not None:
            try:
                self._dvd_highlight_shadow.close()
            except Exception as e:
                log.warning("DVD highlight shadow session close failed: %s", e)
            self._dvd_highlight_shadow = None

    def _play_dvd_main(self, seek_seconds=0):
        vlc = self.vlc_engine
        if vlc is None or not self.dvd_file_path:
            return
        try:
            log.info(f"[DVD] Now playing main video file: {os.path.basename(self.dvd_file_path)}"
                  + (f" (resuming at {seek_seconds:.1f}s)" if seek_seconds else ""))
            _gain = 1.0
            if callable(self.dvd_gain_lookup):
                try:
                    _gain = self.dvd_gain_lookup(self.dvd_file_path)
                except Exception as e:
                    log.warning("dvd_gain_lookup failed: %s", e)
            _handed_to_vlc = vlc.play_file_segmented(self.dvd_file_path, seek_seconds, gain=_gain)
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
            if self.dvd_is_disc_image and _handed_to_vlc:
                self._dvd_iso_watchdog_armed = True
                self._dvd_iso_watchdog_armed_at = pygame.time.get_ticks()
                self._dvd_iso_watchdog_deadline = self._dvd_iso_watchdog_armed_at + self.DVD_ISO_OPEN_TIMEOUT_MS

                if DvdnavShadowSession is not None:
                    try:
                        if self._dvd_highlight_shadow is not None:
                            self._dvd_highlight_shadow.close()
                        self._dvd_highlight_shadow = DvdnavShadowSession()
                        self._dvd_highlight_shadow.open(self.dvd_file_path)
                    except Exception as _hl_e:
                        log.warning("DVD highlight shadow session open failed: %s", _hl_e)
                        self._dvd_highlight_shadow = None
            else:
                self._dvd_iso_watchdog_armed = False
                if self.dvd_is_disc_image:
                    log.warning("[DVD DEBUG] Disc image but play_file_segmented() reported failure -- watchdog NOT armed.")
        except Exception as e:
            log.warning(f"[DVD] Failed to play {self.dvd_file_path}: {e}")

    # ========================================================================
    # SECTION: External emulator launch & monitoring
    # ========================================================================

    def _resolve_emulator_exe(self, console):
        folder = os.path.join(self.cores_dir, console)
        chosen = self.emulator_config.get(console)

        candidates = []
        try:
            for root, dirs, files in os.walk(folder):
                for name in files:
                    if name.lower().endswith(".exe"):
                        candidates.append(os.path.join(root, name))
        except Exception as e:
            log.warning(f"[GAMEDECK RESOLVE] Walk error: {e}")

        if not candidates:
            log.warning(f"[GAMEDECK RESOLVE] No exe found. Put emulator in {folder}")
            return None

        if chosen:
            for c in candidates:
                if os.path.basename(c).lower() == chosen.lower():
                    return c
            log.info(f"[GAMEDECK RESOLVE] Saved name {chosen!r} not found in tree, auto-detecting.")

        gui = [c for c in candidates if "sdl" not in os.path.basename(c).lower()]
        pool = gui if gui else candidates
        pool.sort(key=lambda p: os.path.getsize(p), reverse=True)
        return pool[0]

    def reload_emulator_config(self):
        self.emulator_config = self._load_emulator_config()

    def _patch_mgba_config(self, exe_dir):
        targets = {
            "ports.qt": {
                "fullscreen":           "1",
                "audiodriver":          "sdl",
                "allowbackgroundinput": "1",
                "key.a":                "90",
                "key.b":                "88",
                "key.l":                "65",
                "key.r":                "83",
                "key.start":            "16777221",
                "key.select":           "16777219",
                "key.up":               "16777235",
                "key.down":             "16777237",
                "key.left":             "16777234",
                "key.right":            "16777236",
            },
            "": {
                "allowbackgroundinput": "1",
            },
        }

        paths_to_patch = [os.path.join(exe_dir, "config.ini")]
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            roaming = os.path.join(appdata, "mGBA", "config.ini")
            if os.path.exists(roaming):
                paths_to_patch.append(roaming)
                log.info(f"[GAMEDECK] Also patching roaming config: {roaming}")

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
                log.info(f"[GAMEDECK] Patched {config_path} "
                      f"(allowBackgroundInput confirmed: {ok})")
            except Exception as e:
                log.warning(f"[GAMEDECK] Could not patch {config_path}: {e}")

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

            launch_env = os.environ.copy()
            launch_env["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"]       = "1"
            launch_env["SDL_GAMECONTROLLER_ALLOW_BACKGROUND_EVENTS"]  = "1"

            if _WIN:
                ctypes.windll.user32.AllowSetForegroundWindow(-1)

            if _WIN:
                import subprocess as _sp
                si = _sp.STARTUPINFO()
                si.dwFlags     = _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
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
            log.info(f"[GAMEDECK LAUNCH] PID={self.deck_process.pid}")
            threading.Thread(target=self._monitor_emulator, daemon=True).start()
            threading.Thread(target=self._controller_exit_watcher, daemon=True).start()

        except Exception:
            log.exception("[GAMEDECK LAUNCH] FAILED")
            self.emulator_loading = False
            self.mode = "ROMLIST"

    def _monitor_emulator(self):
        """Animate loading bar, find window, mount as child, wait for close."""
        deadline   = time.time() + 90
        found_hwnd = None
        poll_n     = 0
        pid        = self.deck_process.pid if self.deck_process else None
        log.info("[GAMEDECK MONITOR] Watching PID=%s for up to 90s...", pid)

        while time.time() < deadline:
            if self.deck_process is None:
                log.warning("[GAMEDECK MONITOR] deck_process gone.")
                break
            code = self.deck_process.poll()
            if code is not None:
                log.warning("[GAMEDECK MONITOR] Process exited (code=%s).", code)
                break

            elapsed = time.time() - (deadline - 90)
            self.emulator_load_pct = min(90, int(elapsed / 90 * 90))
            self.emulator_load_msg = f"Loading {self.active_console or 'emulator'}..."

            if _WIN:
                hwnd = self._find_emulator_hwnd()
                if hwnd:
                    found_hwnd = hwnd
                    log.info("[GAMEDECK MONITOR] Window found after %.1fs (HWND=%s)", elapsed, hwnd)
                    break

            poll_n += 1
            time.sleep(0.05)

        self.emulator_load_pct = 100
        self.emulator_load_msg = "Mounting to Channel 03..."

        if found_hwnd and _WIN:
            try:
                pygame_hwnd = pygame_hwnd_ref[0]
                if not pygame_hwnd:
                    log.warning("[GAMEDECK MONITOR] No pygame HWND — cannot mount.")
                elif not ctypes.windll.user32.IsWindow(pygame_hwnd):
                    log.warning("[GAMEDECK MONITOR] Cached pygame HWND is no "
                                "longer a valid window — skipping mount to "
                                "avoid attaching to a stale/reused handle.")
                else:
                    GWL_STYLE           = -16
                    GWL_EXSTYLE         = -20
                    WS_CHILD            = 0x40000000
                    WS_VISIBLE          = 0x10000000
                    WS_CLIPSIBLINGS     = 0x04000000
                    WS_CLIPCHILDREN     = 0x02000000
                    WS_EX_TOOLWINDOW    = 0x00000080
                    WS_EX_APPWINDOW     = 0x00040000
                    WS_EX_NOACTIVATE    = 0x08000000

                    ctypes.windll.user32.SetParent(found_hwnd, pygame_hwnd)

                    new_style = (WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN)
                    ctypes.windll.user32.SetWindowLongW(found_hwnd, GWL_STYLE, new_style)

                    ex = ctypes.windll.user32.GetWindowLongW(found_hwnd, GWL_EXSTYLE)
                    ctypes.windll.user32.SetWindowLongW(
                        found_hwnd, GWL_EXSTYLE,
                        (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

                    SWP_NOMOVE       = 0x0002
                    SWP_NOSIZE       = 0x0001
                    SWP_NOZORDER     = 0x0004
                    SWP_NOACTIVATE   = 0x0010
                    SWP_FRAMECHANGED = 0x0020
                    ctypes.windll.user32.SetWindowPos(
                        found_hwnd, None, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)

                    if self._fit_rect:
                        fx, fy, cw, ch = (int(v) for v in self._fit_rect)
                    else:
                        rc = wintypes.RECT()
                        ctypes.windll.user32.GetClientRect(pygame_hwnd, ctypes.byref(rc))
                        fx, fy = 0, 0
                        cw = rc.right  - rc.left
                        ch = rc.bottom - rc.top
                    ctypes.windll.user32.MoveWindow(found_hwnd, fx, fy, cw, ch, True)

                    SWP_SHOWWINDOW = 0x0040
                    HWND_TOP       = 0
                    ctypes.windll.user32.SetWindowPos(
                        found_hwnd, HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    ctypes.windll.user32.ShowWindow(found_hwnd, 9)
                    log.info("[GAMEDECK MONITOR] mGBA shown as WS_CHILD inside pygame.")

                    self.game_hwnd  = found_hwnd
                    self.is_embedded = True
                    self._child_visible = True

                    ctypes.windll.user32.SetFocus(pygame_hwnd)
                    ctypes.windll.user32.SetForegroundWindow(pygame_hwnd)

            except Exception:
                log.exception("[GAMEDECK MONITOR] Child mount failed")

        self.emulator_loading = False
        self.overlay_ready    = True
        log.info("[GAMEDECK MONITOR] Mount complete. Game running.")

        if self.deck_process:
            self.deck_process.wait()

        self.overlay_ready = False
        log.info("[GAMEDECK] Emulator closed. Restoring Retro TV...")
        self._on_emulator_closed()

    def _find_emulator_hwnd(self):
        if not self.deck_process or not _WIN:
            return None
        target_pid = self.deck_process.pid
        found = []

        # WARNING: the EnumWindows callback must stay alive through a persistent reference.
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

        def _cb(hwnd, _lp):
            try:
                pid = ctypes.c_ulong(0)
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == target_pid:
                    rc = wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
                    w = max(0, rc.right  - rc.left)
                    h = max(0, rc.bottom - rc.top)
                    found.append((w * h, hwnd))
            except Exception as e:
                log.warning("GetWindowRect failed while scoring a candidate mGBA child window: %s", e)
            return True

        _enum_cb = WNDENUMPROC(_cb)
        try:
            ctypes.windll.user32.EnumWindows(_enum_cb, 0)
        except Exception as e:
            log.warning(f"[GAMEDECK FIND] EnumWindows error: {e}")

        if not found:
            return None
        found.sort(reverse=True)
        return found[0][1]

    def _on_emulator_closed(self):
        if self.game_hwnd and _WIN:
            try:
                ctypes.windll.user32.SetParent(self.game_hwnd, None)
                log.info("[GAMEDECK] mGBA child window detached.")
            except Exception as e:
                log.warning("SetParent (detach mGBA child window) failed: %s", e)

        if self.ui_overlay_hwnd and _WIN:
            try:
                ctypes.windll.user32.DestroyWindow(self.ui_overlay_hwnd)
                log.info("[GAMEDECK] UI overlay window destroyed.")
            except Exception as e:
                log.warning("DestroyWindow for UI overlay child failed: %s", e)
        self.ui_overlay_hwnd = None

        self.deck_process = None
        self.game_hwnd    = None
        self.is_visible   = False
        self.is_embedded  = False
        self.is_frozen    = False
        self.emulator_loading = False
        self._child_visible = None

        if not self.roms and self.active_console:
            log.info(f"[GAMEDECK] ROM list empty on return — re-scanning {self.active_console}.")
            self._scan_roms(self.active_console)

        self.mode = "ROMLIST"
        log.info(f"[GAMEDECK] Retro TV restored. Back in ROM list at index {self.rom_index}.")

    def _controller_exit_watcher(self):
        HOLD_MS = 600

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
            if not vk:
                return
            inp = _INPUT()
            inp.type       = INPUT_KEYBOARD
            inp._.ki.wVk   = vk
            inp._.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
            inp._.ki.dwExtraInfo = ctypes.cast(
                ctypes.pointer(ctypes.c_ulong(0)), ctypes.POINTER(ctypes.c_ulong))
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

        my_process = self.deck_process

        log.info("[GAMEDECK CTRL WATCHER] Started — waiting for overlay_ready...")

        wait_deadline = time.time() + 120
        while not getattr(self, "overlay_ready", False) and time.time() < wait_deadline:
            time.sleep(0.15)

        if my_process is None or my_process.poll() is not None:
            log.info("[GAMEDECK CTRL WATCHER] Emulator gone before watcher activated.")
            return

        log.info("[GAMEDECK CTRL WATCHER] Live — hold Start+Select 0.6s to exit.")

        def _active_controller_map(console):
            """Return the effective physical-bit -> logical-button map."""
            try:
                return self._ensure_xi_map(console, 1)
            except Exception:
                # WARNING: the watcher must stay usable during early startup.
                return {
                    logical: bit
                    for bit, logical in _get_console_def(console)["xinput_map"].items()
                }

        hold_since   = None
        prev_buttons = 0
        # VALUES CHANGE HERE
        TRIG_THRESH  = 30

        while my_process.poll() is None:
            time.sleep(0.016)

            _main_mod = sys.modules.get("__main__")
            _overlay_open = bool(_main_mod and getattr(_main_mod, "app_state", None) and (
                _main_mod.app_state.get("show_menu", False) or
                _main_mod.app_state.get("show_splash", False) or
                _main_mod.app_state.get("show_quit_confirm", False) or
                _main_mod.app_state.get("show_restart_confirm", False) or
                _main_mod.app_state.get("file_explorer_active", False)
            ))
            if self.is_frozen or _overlay_open:
                if prev_buttons:
                    _active_con_f = getattr(self, "active_console", "GBA") or "GBA"
                    _xi_map_f     = _active_controller_map(_active_con_f)
                    for console_btn, bit in _xi_map_f.items():
                        if prev_buttons & bit:
                            vk = self.ctrl_mapping.get(console_btn, 0)
                            if vk:
                                _send_key(vk, key_up=True)
                    prev_buttons = 0
                    hold_since   = None
                continue

            buttons = 0
            gp = _read_gamepad_state()
            if gp is not None:
                buttons = gp.wButtons
                if self.ctrl_mapping.get("stick_as_dpad", False):
                    DEAD = stick_axis_threshold_raw()
                    if not hasattr(self, "_ctrl_test_dpad_latch"):
                        self._ctrl_test_dpad_latch = {}
                    up, down, left, right = _stick_to_dpad_hysteresis(
                        gp.sLX, gp.sLY, DEAD, self._ctrl_test_dpad_latch)
                    if up:    buttons |= _XI_UP
                    if down:  buttons |= _XI_DOWN
                    if left:  buttons |= _XI_LEFT
                    if right: buttons |= _XI_RIGHT
                if gp.bLT >= TRIG_THRESH: buttons |= _XI_LT
                if gp.bRT >= TRIG_THRESH: buttons |= _XI_RT

            _active_con_now = getattr(self, "active_console", "GBA") or "GBA"
            _active_map_now = _active_controller_map(_active_con_now)
            _start_bit = _active_map_now.get("Start", _XI_START)
            _select_bit = _active_map_now.get("Select", _XI_BACK)
            if (buttons & _start_bit) and (buttons & _select_bit):
                if hold_since is None:
                    hold_since = time.time()
                    log.info("[GAMEDECK CTRL WATCHER] Start+Select held — confirming...")
                elif (time.time() - hold_since) * 1000 >= HOLD_MS:
                    log.info("[GAMEDECK CTRL WATCHER] Confirmed. Killing emulator.")
                    _active_con_e = getattr(self, "active_console", "GBA") or "GBA"
                    _xi_map_e     = _active_controller_map(_active_con_e)
                    for console_btn, bit in _xi_map_e.items():
                        if prev_buttons & bit:
                            vk = self.ctrl_mapping.get(console_btn, 0)
                            _send_key(vk, key_up=True)
                    try:
                        my_process.kill()
                    except Exception as e:
                        log.warning("Failed to kill deck_process during controller-mapping key-release cleanup: %s", e)
                    break
                prev_buttons = buttons
                continue
            else:
                if hold_since is not None:
                    log.info("[GAMEDECK CTRL WATCHER] Released early — reset hold timer.")
                hold_since = None

            changed = buttons ^ prev_buttons
            if changed:
                _active_con = getattr(self, "active_console", "GBA") or "GBA"
                _xi_map     = _active_controller_map(_active_con)
                for console_btn, bit in _xi_map.items():
                    if changed & bit:
                        vk = self.ctrl_mapping.get(console_btn, 0)
                        if vk:
                            pressed = bool(buttons & bit)
                            _send_key(vk, key_up=not pressed)

            prev_buttons = buttons

        log.info("[GAMEDECK CTRL WATCHER] Exited.")

    # ========================================================================
    # SECTION: Emulator window embedding & process control
    # ========================================================================

    def _set_child_visible(self, visible):
        if not _WIN or not self.game_hwnd:
            return
        want = bool(visible)
        with self._vis_lock:
            if self._child_visible == want:
                return
            try:
                HWND_TOP       = 0
                SWP_NOMOVE     = 0x0002
                SWP_NOSIZE     = 0x0001
                SWP_NOACTIVATE = 0x0010
                SWP_SHOWWINDOW = 0x0040
                if want:
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
                    ctypes.windll.user32.SetWindowPos(
                        self.game_hwnd, HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    ctypes.windll.user32.ShowWindow(self.game_hwnd, 8)
                else:
                    ctypes.windll.user32.ShowWindow(self.game_hwnd, 0)
                self._child_visible = want
            except Exception as e:
                _log_warn_throttled("set_child_visible", f"[GAMEDECK VIS] _set_child_visible({want}) failed: {e}")

    def sync_child_window(self, show, fit_rect=None):
        if not _WIN or not self.game_hwnd:
            return
        if fit_rect is not None:
            new_rect = tuple(int(v) for v in fit_rect)
            if new_rect != self._fit_rect and self._child_visible and show:
                self._fit_rect = new_rect
                with self._vis_lock:
                    self._child_visible = None
            else:
                self._fit_rect = new_rect
        self._set_child_visible(bool(show))

    def set_viewport_state(self, visible=True, target_rect=None):
        if not _WIN:
            return True
        if getattr(self, "emulator_loading", False):
            return True
        if not self.game_hwnd:
            return True

        try:
            if not visible:
                self._set_child_visible(False)
                # WARNING: call NtSuspendProcess inline so the process is confirmed suspended before returning; a quick channel change could land in the gap.
                try:
                    self._suspend_process()
                except Exception as e:
                    log.warning("_suspend_process() call failed: %s", e)
                log.info("[GAMEDECK VIEWPORT] Left ch03 — child hidden, mGBA suspended.")

            else:
                self._resume_process()
                self.is_frozen = False
                with self._vis_lock:
                    self._child_visible = None
                self._set_child_visible(True)
                log.info("[GAMEDECK VIEWPORT] Returned to ch03 — mGBA resumed + child visible.")

        except Exception:
            log.exception("[GAMEDECK VIEWPORT] set_viewport_state failed")
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

    # ========================================================================
    # SECTION: Deck lifecycle: freeze, audio & shutdown
    # ========================================================================

    def pre_boot_background_deck(self, parent_hwnd=None):
        if parent_hwnd: self.parent_hwnd = parent_hwnd
        self.ready_flag = True
        return True

    def freeze_inputs(self):
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

            vk_codes = set()
            for v in self.ctrl_mapping.values():
                if isinstance(v, int):
                    vk_codes.add(v)
            # WARNING: TV shell keys must never leak into the game.
            tv_keys = [
                0x4D,
                0x43,
                0x4E,
                0x1B,
                0x26,
                0x28,
                0x25,
                0x27,
                0x0D,
                0x09,
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
            log.warning(f"[GAMEDECK] freeze_inputs error: {e}")

    def set_audio_state(self, muted=True, retry_deadline_ms=None):
        proc = self.deck_process
        if proc is None or not self.is_process_running():
            return False
        pid = proc.pid
        _deadline = (time.monotonic() + retry_deadline_ms / 1000.0
                     if retry_deadline_ms is not None else None)
        while True:
            try:
                from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
                for session in AudioUtilities.GetAllSessions():
                    if session.Process and session.Process.pid == pid:
                        vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                        vol.SetMute(1 if muted else 0, None)
                        return True
            except Exception as e:
                log.warning("pycaw mute attempt failed, falling back to WASAPI: %s", e)
            ok = _wasapi_set_process_audio(pid, mute=muted)
            if ok:
                return True
            if _deadline is None or time.monotonic() >= _deadline:
                _log_warn_throttled("audio_mute_session_lookup", f"[GAMEDECK AUDIO] set_audio_state: session not found yet (pid={pid}).")
                return False
            time.sleep(0.05)

    def set_audio_volume(self, level_pct, muted=False):
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
                    applied = False
                    try:
                        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
                        for session in AudioUtilities.GetAllSessions():
                            if session.Process and session.Process.pid == job_pid:
                                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                                vol.SetMasterVolume(job_gain, None)
                                applied = True
                                break
                    except Exception as e:
                        log.warning("pycaw volume-set attempt failed, falling back to WASAPI: %s", e)
                    if not applied:
                        ok = _wasapi_set_process_audio(job_pid, volume_pct=job_curved_pct)
                        if ok:
                            pass
                        else:
                            _log_warn_throttled("audio_volume_session_lookup", f"[GAMEDECK AUDIO] set_audio_volume: session not found yet (pid={job_pid}).")
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
        self._libretro_rom_path = ""
        self.dvd_playback_active  = False
        self.dvd_splash_playing   = False
        self.dvd_splash_done      = False
        self._dvd_splash_is_video = False
        self.dvd_exit_confirm     = False
        self.dvd_return_menu_confirm = False
        self.dvd_paused           = False
        self.dvd_was_in_menu_on_leave = False

    # ========================================================================
    # SECTION: Controller mapping: storage & player slots
    # ========================================================================

    @staticmethod
    def _mapping_key(console, player=1):
        if player is None or player <= 1:
            return f"{console}_mapping"
        return f"{console}_mapping_p{player}"

    def _load_controller_mapping(self, console=None, player=1):
        if console is None:
            console = "GBA"
        cdef    = _get_console_def(console)
        default = dict(cdef["default_map"])
        try:
            cfg_path = resolve_emulator_config_path()
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                saved = {}
                if player and player > 1:
                    saved = data.get(self._mapping_key(console, player), {})
                if not saved:
                    saved = data.get(f"{console}_mapping", {})
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
            log.warning(f"[GAMEDECK MAPPING] Load failed for {console} player {player} (using defaults): {e}")
        return default

    def _save_controller_mapping(self, console=None, player=None):
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        if player is None:
            player = getattr(self, "_active_mapping_player", 1) or 1
        try:
            cfg_path = resolve_emulator_config_path()
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            serialised = {}
            for k, v in self.ctrl_mapping.items():
                if k == "stick_as_dpad":
                    serialised[k] = v
                else:
                    serialised[k] = VK_NAMES.get(v, v)
            data[self._mapping_key(console, player)] = serialised
            if console == "GBA" and (not player or player <= 1):
                data["GBA_mapping"] = serialised
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info(f"[GAMEDECK MAPPING] Saved {console} (player {player or 1}): {serialised}")
            self._gameplay_ctrl_mapping_cache.get(console, {}).pop(player or 1, None)
        except Exception as e:
            log.warning(f"[GAMEDECK MAPPING] Save failed for {console} player {player}: {e}")

    def _gameplay_ctrl_mapping_for_player(self, console, player_num):
        cache = self._gameplay_ctrl_mapping_cache.setdefault(console, {})
        if player_num not in cache:
            cache[player_num] = self._load_controller_mapping(console, player_num)
        return cache[player_num]

    def _switch_mapping_player(self, player_num, console=None):
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        self._active_mapping_player = player_num
        self._mapping_player_by_console[console] = player_num
        self.ctrl_mapping = self._load_controller_mapping(console, player_num)
        self.ctrl_mapping_active_btn = None
        self._seq_remap_active = False
        self._ctrl_labels = {}
        self._player_reassign_cancel_if_other(player_num)

    def _player_reassign_cancel_if_other(self, player_num):
        if _reassign_armed_player is not None and _reassign_armed_player != player_num:
            _player_reassign_cancel()
            self._mapping_reassign_wait_started = None

    def _player_slot_pickable(self, player_num, console=None):
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        if player_num > CONSOLE_MAX_PLAYERS.get(console, 4):
            return False
        if player_num <= 1:
            return True
        return player_is_connected(player_num)

    def start_controller_pick(self, player_num):
        self.start_mapping_reassign(player_num)
        self._pick_flow_pending_player = player_num

    def start_menu_controller_pick(self):
        _menu_reassign_arm()
        self._mapping_reassign_wait_started = time.monotonic()

    def _after_controller_picked(self, player_num):
        slot = _player_slot_of.get(player_num)
        console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        self._switch_mapping_player(player_num, console)
        if isinstance(slot, int):
            cdef = _get_console_def(console)
            per_console = self._libretro_xinput_map.setdefault(console, {})
            fresh_map = {}
            for xi_bit, btn_name in cdef.get("xinput_map", {}).items():
                fresh_map[btn_name] = xi_bit
            per_console[player_num] = fresh_map
            self._save_libretro_xinput_map(console, player_num)
            log.info(f"[GAMEDECK MULTIPLAYER] Player {player_num}: XInput pad picked -- "
                  f"default layout auto-applied for {console}.")
        else:
            self._pick_layout_popup_player = player_num
            log.info(f"[GAMEDECK MULTIPLAYER] Player {player_num}: non-XInput pad picked -- "
                  f"layout unknown, forcing remap for {console}.")

    def confirm_pick_layout_popup(self):
        player_num = getattr(self, "_pick_layout_popup_player", None)
        self._pick_layout_popup_player = None
        if player_num is not None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
            self._switch_mapping_player(player_num, console)
            self.start_sequential_remap(console)

    def dismiss_pick_layout_popup(self):
        self._pick_layout_popup_player = None

    def skip_current_remap_button(self):
        if not getattr(self, "_seq_remap_active", False) or self.ctrl_mapping_active_btn is None:
            return
        console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        cdef    = _get_console_def(console)
        BTN_SEQ = cdef["btn_seq"]
        active  = self.ctrl_mapping_active_btn
        self.ctrl_mapping[active] = 0
        remap_player = getattr(self, "_active_mapping_player", 1) or 1
        self._libretro_xinput_map.setdefault(console, {}).setdefault(
            remap_player, {}).pop(active, None)
        if not hasattr(self, "_ctrl_labels"):
            self._ctrl_labels = {}
        self._ctrl_labels[active] = "---"
        log.info(f"[GAMEDECK MAPPING] Skipped {active} -- left unbound.")
        if active in BTN_SEQ:
            idx = BTN_SEQ.index(active)
            if idx + 1 < len(BTN_SEQ):
                self.ctrl_mapping_active_btn = BTN_SEQ[idx + 1]
                self._remap_await_release  = True
                self._remap_last_capture_ts = time.monotonic()
                return
        self._seq_remap_active = False
        self.ctrl_mapping_active_btn = None
        self._save_controller_mapping(console)
        self._save_libretro_xinput_map(console)
        if console == "MENU":
            self._apply_menu_nav_mapping_from_libretro_map()

    def poll_controller_test_lights(self, console=None):
        if self.ctrl_mapping_active_btn is not None or getattr(self, "_seq_remap_active", False):
            self._ctrl_test_lit = set()
            self._ctrl_test_lit_joy = set()
            return
        if self.mapping_reassign_pending():
            self._ctrl_test_lit = set()
            self._ctrl_test_lit_joy = set()
            return
        if not _WIN:
            self._ctrl_test_lit = set()
            self._ctrl_test_lit_joy = set()
            return
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        player = getattr(self, "_active_mapping_player", 1) or 1
        if player > 1 and not player_is_connected(player):
            self._ctrl_test_lit = set()
            self._ctrl_test_lit_joy = set()
            return
        gp = player1_gpstate() if player == 1 else player_gpstate(player)
        if gp is None:
            self._ctrl_test_lit = set()
            self._ctrl_test_lit_joy = set()
            return

        TRIGGER_PRESS_THRESHOLD = 30
        btns = gp.wButtons
        pressed_bits = set()
        for bit in (_XI_UP, _XI_DOWN, _XI_LEFT, _XI_RIGHT, _XI_A, _XI_B, _XI_X, _XI_Y,
                    _XI_LB, _XI_RB, _XI_START, _XI_BACK):
            if btns & bit:
                pressed_bits.add(bit)
        if gp.bLT >= TRIGGER_PRESS_THRESHOLD:
            pressed_bits.add(_XI_LT)
        if gp.bRT >= TRIGGER_PRESS_THRESHOLD:
            pressed_bits.add(_XI_RT)

        STICK_LIGHT_THRESH = stick_axis_threshold_raw()
        joy_lit = set()
        if not hasattr(self, "_ctrl_test_lit_dpad_latch"):
            self._ctrl_test_lit_dpad_latch = {}
        _lit_up, _lit_down, _lit_left, _lit_right = _stick_to_dpad_hysteresis(
            gp.sLX, gp.sLY, STICK_LIGHT_THRESH, self._ctrl_test_lit_dpad_latch)
        if _lit_up:
            if console != "N64": pressed_bits.add(_XI_UP)
            joy_lit.add("JoyUp")
        if _lit_down:
            if console != "N64": pressed_bits.add(_XI_DOWN)
            joy_lit.add("JoyDown")
        if _lit_left:
            if console != "N64": pressed_bits.add(_XI_LEFT)
            joy_lit.add("JoyLeft")
        if _lit_right:
            if console != "N64": pressed_bits.add(_XI_RIGHT)
            joy_lit.add("JoyRight")
        self._ctrl_test_lit_joy = joy_lit

        console_map = self._ensure_xi_map(console, player)
        lit = {btn_name for btn_name, xi_bit in console_map.items()
               if xi_bit in pressed_bits}

        if console == "N64":
            if not hasattr(self, "_ctrl_test_lit_cstick_latch"):
                self._ctrl_test_lit_cstick_latch = {}
            _c_up, _c_down, _c_left, _c_right = _stick_to_dpad_hysteresis(
                gp.sRX, gp.sRY, STICK_LIGHT_THRESH, self._ctrl_test_lit_cstick_latch)
            if _c_up:    lit.add("CUp")
            if _c_down:  lit.add("CDown")
            if _c_left:  lit.add("CLeft")
            if _c_right: lit.add("CRight")

        self._ctrl_test_lit = lit

    def start_mapping_reassign(self, player_num):
        _player_reassign_arm(player_num)
        self._mapping_reassign_wait_started = time.monotonic()

    def mapping_reassign_pending(self):
        return _reassign_armed_player is not None or _menu_reassign_armed

    def mapping_reassign_pending_player(self):
        return _reassign_armed_player

    def menu_reassign_pending(self):
        return _menu_reassign_armed

    def poll_menu_reassign(self):
        return _menu_reassign_poll()

    def cancel_any_mapping_reassign(self):
        self._player_reassign_cancel_if_other(-1)
        _menu_reassign_cancel()
        self._mapping_reassign_wait_started = None

    def poll_player_reassign(self):
        result = _player_reassign_poll()
        if result is not None and getattr(self, "_pick_flow_pending_player", None) == result:
            self._pick_flow_pending_player = None
            self._after_controller_picked(result)
        return result

    def refresh_player_slots(self):
        _ensure_player_slots_assigned()

    # ========================================================================
    # SECTION: Controller mapping: UI, clicks & sequential remap
    # ========================================================================

    def render_controller_mapping_ui(self, surface, rect, ui_color=(0, 255, 128), db=None, console=None):
        if console is None:
            console = "GBA"
        console_label = CONSOLE_LABELS.get(console, console)

        _ensure_player_slots_assigned()
        if getattr(self, "_active_mapping_console", None) != console:
            self._active_mapping_console = console
            remembered_player = self._mapping_player_by_console.get(console, 1)
            if (remembered_player > CONSOLE_MAX_PLAYERS.get(console, 4)
                    or (remembered_player > 1 and not player_is_connected(remembered_player))):
                remembered_player = 1
            self._active_mapping_player = remembered_player
            self._ctrl_map_player_sel = remembered_player
            self.ctrl_mapping = self._load_controller_mapping(console, self._active_mapping_player)
            self.ctrl_mapping_active_btn = None
            self._seq_remap_active = False
            self._ctrl_labels = {}
            self._last_xinput_btns = 0
        elif ((getattr(self, "_active_mapping_player", 1) or 1)
              > CONSOLE_MAX_PLAYERS.get(console, 4)
              or ((getattr(self, "_active_mapping_player", 1) or 1) > 1
                  and not player_is_connected(self._active_mapping_player))):
            self._switch_mapping_player(1, console)
            self._ctrl_map_player_sel = 1
        if console not in self._libretro_xinput_map:
            self._build_default_libretro_xinput_map(console)

        cdef       = _get_console_def(console)
        BTN_SEQ    = cdef["btn_seq"]
        LEFT_BTNS  = cdef["left_btns"]
        RIGHT_BTNS = cdef["right_btns"]
        HOTSPOTS   = cdef["hotspots"]

        x, y, w, h = rect

        _target_x, _target_y, _target_w, _target_h = x, y, w, h
        BASE_W, BASE_H = 996, 525
        _real_surface = surface
        surface = pygame.Surface((BASE_W, BASE_H), pygame.SRCALPHA)
        x, y, w, h = 0, 0, BASE_W, BASE_H

        import colorsys
        _bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220 / 360.0
        _br, _bg, _bb = colorsys.hsv_to_rgb(_bg_hue, 0.6, 0.15)
        MIDNIGHT_NAVY = (int(_br * 255), int(_bg * 255), int(_bb * 255))
        _slider_val = db.config.get("menu_opacity", 50) if db is not None else 50
        menu_opacity = int((_slider_val / 100.0) * 255)
        bg_layer = pygame.Surface((w, h), pygame.SRCALPHA)
        bg_layer.fill(MIDNIGHT_NAVY + (menu_opacity,))
        surface.blit(bg_layer, (x, y))

        f_label  = _cached_font("Courier New", 17, bold=True)
        f_small  = _cached_font("Courier New", 14, bold=True)
        f_title  = _cached_font("Courier New", 22, bold=True)
        f_opt    = _cached_font("Courier New", 19, bold=True)

        title_lbl = f_title.render(f"CONTROLLER MAPPING  —  {console}", True, ui_color)
        surface.blit(title_lbl, (x + w // 2 - title_lbl.get_width() // 2, y + 10))

        content_top = y + 4 + title_lbl.get_height() + 6
        logo_h_tgt = max(110, int(h * 0.36))
        if console == "PSX":
            logo_h_tgt = max(90, int(logo_h_tgt * 0.78))
        logo_surf  = self._get_logo(console, logo_h_tgt)
        f_logo_fallback = None
        lbl_fallback = None
        if logo_surf is not None:
            logo_height = logo_surf.get_height()
        else:
            f_logo_fallback = pygame.font.SysFont("Courier New", logo_h_tgt // 3, bold=True)
            lbl_fallback = f_logo_fallback.render(console_label, True, ui_color)
            logo_height = lbl_fallback.get_height()

        if console == "N64":
            _n64_shift = int(h * 0.05)
            img_y_n64_target = content_top - _n64_shift + logo_height - int(h * 0.15)
            logo_top = content_top - int(h * 0.09)
        else:
            _NONN64_CONTENT_SHIFT = int(h * 0.038)
            if console in ("GG", "GB"):
                _NONN64_CONTENT_SHIFT += int(h * 0.03)
            if console == "MENU":
                _NONN64_CONTENT_SHIFT += int(h * 0.05)
            elif console == "PSX":
                _NONN64_CONTENT_SHIFT -= int(h * 0.017)
            logo_top = content_top - _NONN64_CONTENT_SHIFT

        if logo_surf is not None:
            surface.blit(logo_surf, (x + w // 2 - logo_surf.get_width() // 2, logo_top))
            logo_bottom = logo_top + logo_surf.get_height()
        else:
            surface.blit(lbl_fallback, (x + w // 2 - lbl_fallback.get_width() // 2, logo_top))
            logo_bottom = logo_top + lbl_fallback.get_height()

        if console == "MENU":
            wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", "PSX_wireframe.png")
        elif console == "NGPC":
            wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", "NGP_wireframe.png")
        elif console in ("32X", "MASTERSYSTEM"):
            wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", "GENESIS_wireframe.png")
        else:
            wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", f"{console}_wireframe.png")
        if not os.path.exists(wf_path):
            wf_path = os.path.join(self.base_dir, "main", "images", "Wireframes", "GBA_wireframe.png")
        if console not in self._wf_surface_cache and os.path.exists(wf_path):
            try:
                raw = pygame.image.load(wf_path).convert_alpha()
                self._wf_surface_cache[console] = raw
                self._wf_tinted_cache[console]     = None
                self._wf_tint_color_cache[console] = None
                if console == "GBA":
                    self._gba_wf_surface    = raw
                    self._gba_wf_tinted     = None
                    self._gba_wf_tint_color = None
            except Exception as e:
                log.warning(f"[GAMEDECK MAPPING UI] Could not load wireframe for {console}: {e}")
                self._wf_surface_cache[console] = None

        if console == "N64":
            img_area_h = int(h * 0.46)
            img_area_w = int(w * 0.55)
        elif console == "GG":
            img_area_h = int(h * 0.42)
            img_area_w = int(w * 0.60)
        elif console in ("NES", "GBC", "PSX", "MENU"):
            img_area_h = int(h * 0.39)
            img_area_w = int(w * 0.55)
        elif console == "GB":
            img_area_h = int(h * 0.42)
            img_area_w = int(w * 0.60)
        else:
            img_area_h = int(h * 0.35)
            img_area_w = int(w * 0.50)
        img_x = x + (w - img_area_w) // 2
        if console == "N64":
            img_y = img_y_n64_target
        elif console == "NES":
            img_y = logo_bottom - int(h * 0.05) - int(h * 0.04)
        else:
            img_y = logo_bottom - int(h * 0.05)

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
                    if src_a is not None:
                        out_a[:,:] = (src_a.astype(np.uint16) * darkness // 255).astype(np.uint8)
                    else:
                        out_a[:,:] = darkness
                    del out_arr, out_a, src_arr
                    if src_a is not None:
                        del src_a
                except Exception as _te:
                    _log_warn_throttled("mapping_numpy_tint", f"[GAMEDECK MAPPING] numpy tint unavailable, using blend ({_te})")
                    inv = scaled.copy()
                    inv.fill((255, 255, 255, 255), special_flags=pygame.BLEND_RGBA_SUB)
                    tint_ovl = pygame.Surface((tw, th), pygame.SRCALPHA)
                    tint_ovl.fill((ui_color[0], ui_color[1], ui_color[2], 255))
                    inv.blit(tint_ovl, (0, 0), special_flags=pygame.BLEND_MULT)
                    tinted.blit(inv, (0, 0))
                self._wf_tinted_cache[console]     = tinted
                self._wf_tint_color_cache[console] = ui_color
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

        wireframe_bottom = draw_y + ih

        _XI_LABELS = {
            _XI_UP: "Dpad U", _XI_DOWN: "Dpad D", _XI_LEFT: "Dpad L", _XI_RIGHT: "Dpad R",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_LB: "L", _XI_RB: "R",
            _XI_A: "A", _XI_B: "B", _XI_X: "X", _XI_Y: "Y",
            _XI_LT: "LT", _XI_RT: "RT",
        }
        for _di_label_index in range(32):
            _XI_LABELS.setdefault(
                _di_raw_button_bit(_di_label_index),
                f"Button {_di_label_index + 1}",
            )

        _N64_C_BTN_DEFAULT_LABELS = {
            "CUp": "R-Joy U", "CDown": "R-Joy D", "CLeft": "R-Joy L", "CRight": "R-Joy R",
        }

        def _ctrl_label_for_btn(btn):
            _label_player = getattr(self, "_active_mapping_player", 1) or 1
            xi_bit = self._ensure_xi_map(console, _label_player).get(btn)
            if xi_bit is not None:
                return _XI_LABELS.get(xi_bit, f"XI{xi_bit:04X}")
            custom = getattr(self, "_ctrl_labels", {}).get(btn)
            if custom:
                return custom
            if console == "N64" and btn in _N64_C_BTN_DEFAULT_LABELS:
                return _N64_C_BTN_DEFAULT_LABELS[btn]
            return "---"

        # VALUES CHANGE HERE
        left_col_right_edge  = x + int(w * 0.22)
        right_col_left_edge  = x + int(w * 0.78)

        label_h         = int(ih * 0.20) if not LEFT_BTNS else max(int(ih * 0.10), int(ih * 0.96 / max(len(LEFT_BTNS), len(RIGHT_BTNS), 1)))
        label_block_top = draw_y + int(ih * 0.02)

        active_btn = self.ctrl_mapping_active_btn
        seq_running = getattr(self, "_seq_remap_active", False)

        def _btn_screen_pos(btn_name):
            """Hotspot fraction → screen px within the drawn image."""
            fx, fy = HOTSPOTS.get(btn_name, (0.5, 0.5))
            return (int(draw_x + fx * iw), int(draw_y + fy * ih))

        def _draw_btn_label(btn, right_edge, row_y, align_right=True, fixed_box_x=None):
            is_active = (active_btn == btn)

            name_col = (255, 220, 0) if is_active else (180, 180, 180)
            name_surf = f_label.render(btn, True, name_col)
            value_text = "..." if is_active else _ctrl_label_for_btn(btn)

            VAL_BOX_W, VAL_BOX_H = 56, name_surf.get_height() + 6
            BOX_GAP = 6

            if align_right:
                box_x  = right_edge - VAL_BOX_W
                name_x = box_x - BOX_GAP - name_surf.get_width()
            else:
                if fixed_box_x is not None:
                    box_x  = fixed_box_x
                    name_x = box_x - BOX_GAP - name_surf.get_width()
                else:
                    name_x = right_edge
                    box_x  = name_x + name_surf.get_width() + BOX_GAP

            name_y = row_y - name_surf.get_height() // 2
            box_y  = row_y - VAL_BOX_H // 2

            surface.blit(name_surf, (name_x, name_y))

            box_bg = (90, 30, 30) if is_active else (60, 60, 80)
            pygame.draw.rect(surface, box_bg, (box_x, box_y, VAL_BOX_W, VAL_BOX_H), border_radius=4)
            box_border = ui_color if is_active else (120, 120, 140)
            pygame.draw.rect(surface, box_border, (box_x, box_y, VAL_BOX_W, VAL_BOX_H), SELECTION_BORDER_WIDTH, border_radius=4)

            val_surf = f_small.render(value_text, True, (255, 255, 255))
            if val_surf.get_width() > VAL_BOX_W - 6:
                f_val_tiny = _cached_font("Courier New", 10, bold=True)
                val_surf = f_val_tiny.render(value_text, True, (255, 255, 255))
            surface.blit(val_surf, (box_x + VAL_BOX_W // 2 - val_surf.get_width() // 2,
                                     box_y + VAL_BOX_H // 2 - val_surf.get_height() // 2))

            hotspot = _btn_screen_pos(btn)
            if align_right:
                line_anchor = (right_edge + 4, row_y)
            else:
                line_anchor = (name_x - 4, row_y)
            dim = (ui_color[0] // 4, ui_color[1] // 4, ui_color[2] // 4)
            bright = ui_color if is_active else (ui_color[0] // 2, ui_color[1] // 2, ui_color[2] // 2)
            is_lit = (not is_active) and (btn in getattr(self, "_ctrl_test_lit", ()))
            if is_lit:
                pygame.draw.line(surface, (60, 200, 90), line_anchor, hotspot, 2)
                pygame.draw.circle(surface, (90, 255, 130), hotspot, 6)
                pygame.draw.circle(surface, (90, 255, 130), hotspot, 6, 1)
            else:
                pygame.draw.line(surface, dim, line_anchor, hotspot, 2)
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

        BOX_GAP = 6
        right_col_max_name_w = max((f_label.render(b, True, (0, 0, 0)).get_width() for b in RIGHT_BTNS), default=0)
        right_col_box_x = right_col_left_edge + right_col_max_name_w + BOX_GAP

        for i, btn in enumerate(RIGHT_BTNS):
            row_y = label_block_top + i * label_h + label_h // 2
            r = _draw_btn_label(btn, right_col_left_edge, row_y, align_right=False,
                                fixed_box_x=right_col_box_x)
            btn_rects[btn] = r

        joy_block_bottom = wireframe_bottom
        if console == "N64":
            joy_hotspots = cdef.get("joy_hotspots", {})
            joy_labels = {"JoyUp": "Joy Up", "JoyDown": "Joy Down",
                          "JoyLeft": "Joy Left", "JoyRight": "Joy Right"}
            joy_defaults = {"JoyUp": "L-Joy U", "JoyDown": "L-Joy D",
                            "JoyLeft": "L-Joy L", "JoyRight": "L-Joy R"}

            sample_h  = f_label.get_height()
            VAL_BOX_W, VAL_BOX_H = 56, sample_h + 6
            BOX_GAP   = 6
            f_joy_dim = (ui_color[0] // 4, ui_color[1] // 4, ui_color[2] // 4)

            row_pitch     = VAL_BOX_H + 10
            # WARNING: must match the sq_size/sq_y math further down.
            _joy_anchor_sq_y   = y + h - 34 - 14
            _joy_block_h       = 2 * VAL_BOX_H + 10
            _map_label_gap     = 14
            _map_label_h       = f_opt.get_height()
            _group_bottom_margin = 26
            _group_h = _joy_block_h + _map_label_gap + _map_label_h
            row_area_top  = max(wireframe_bottom + 8,
                                 (_joy_anchor_sq_y - _group_bottom_margin) - _group_h)
            left_cx  = draw_x + int(0.12 * iw)
            right_cx = draw_x + int(0.88 * iw)

            joy_lit_set = getattr(self, "_ctrl_test_lit_joy", ())

            def _draw_joy_row(jkey, cx, row_idx, box_x):
                row_y = row_area_top + row_idx * row_pitch + VAL_BOX_H // 2
                is_lit = jkey in joy_lit_set
                name_surf = f_label.render(joy_labels[jkey], True, (180, 180, 180))
                name_x  = box_x - BOX_GAP - name_surf.get_width()
                box_y   = row_y - VAL_BOX_H // 2
                name_y  = row_y - name_surf.get_height() // 2
                surface.blit(name_surf, (name_x, name_y))
                pygame.draw.rect(surface, (60, 60, 80), (box_x, box_y, VAL_BOX_W, VAL_BOX_H), border_radius=4)
                pygame.draw.rect(surface, (120, 120, 140), (box_x, box_y, VAL_BOX_W, VAL_BOX_H), 2, border_radius=4)
                val_surf = f_small.render(joy_defaults[jkey], True, (255, 255, 255))
                if val_surf.get_width() > VAL_BOX_W - 6:
                    f_val_tiny = _cached_font("Courier New", 10, bold=True)
                    val_surf = f_val_tiny.render(joy_defaults[jkey], True, (255, 255, 255))
                surface.blit(val_surf, (box_x + VAL_BOX_W // 2 - val_surf.get_width() // 2,
                                         box_y + VAL_BOX_H // 2 - val_surf.get_height() // 2))
                fx, fy = joy_hotspots.get(jkey, (0.5, 0.5))
                hotspot = (int(draw_x + fx * iw), int(draw_y + fy * ih))
                line_anchor = (box_x + VAL_BOX_W // 2, box_y)
                if is_lit:
                    pygame.draw.line(surface, (60, 200, 90), line_anchor, hotspot, 2)
                    pygame.draw.circle(surface, (90, 255, 130), hotspot, 6)
                    pygame.draw.circle(surface, (90, 255, 130), hotspot, 6, 1)
                else:
                    pygame.draw.line(surface, f_joy_dim, line_anchor, hotspot, 2)
                    joy_dot_color = (ui_color[0] // 2, ui_color[1] // 2, ui_color[2] // 2)
                    pygame.draw.circle(surface, joy_dot_color, hotspot, 3)
                return name_y, box_y + VAL_BOX_H

            left_max_w  = max(f_label.render(joy_labels["JoyLeft"],  True, (0,0,0)).get_width(),
                               f_label.render(joy_labels["JoyRight"], True, (0,0,0)).get_width())
            right_max_w = max(f_label.render(joy_labels["JoyUp"],   True, (0,0,0)).get_width(),
                               f_label.render(joy_labels["JoyDown"], True, (0,0,0)).get_width())
            left_box_x  = left_cx  - (left_max_w  + BOX_GAP + VAL_BOX_W) // 2 + left_max_w  + BOX_GAP
            right_box_x = right_cx - (right_max_w + BOX_GAP + VAL_BOX_W) // 2 + right_max_w + BOX_GAP

            _draw_joy_row("JoyLeft",  left_cx,  0, left_box_x)
            _, joy_block_bottom_l = _draw_joy_row("JoyRight", left_cx,  1, left_box_x)
            _draw_joy_row("JoyUp",    right_cx, 0, right_box_x)
            _, joy_block_bottom_r = _draw_joy_row("JoyDown",  right_cx, 1, right_box_x)
            joy_block_bottom = max(joy_block_bottom_l, joy_block_bottom_r)

        wireframe_bottom = joy_block_bottom

        sq_size  = 34
        sq_gap   = 8
        sq_y     = y + h - sq_size - 14
        sq_x0    = x + 16
        is_focused_row = (getattr(self, "_ctrl_map_cursor", 0) == 0)

        if console == "MENU":
            lit = menu_nav_connected()
            fill    = ui_color if lit else (35, 35, 45)
            txt_col = (20, 20, 20) if lit else (90, 90, 95)
            pygame.draw.rect(surface, fill, (sq_x0, sq_y, sq_size, sq_size), border_radius=6)
            if is_focused_row and not getattr(self, "_ctrl_map_on_pick", False):
                pygame.draw.rect(surface, (255, 255, 255), (sq_x0, sq_y, sq_size, sq_size), 2, border_radius=6)
            num_surf = f_opt.render("1", True, txt_col)
            surface.blit(num_surf, (sq_x0 + sq_size // 2 - num_surf.get_width() // 2,
                                     sq_y + sq_size // 2 - num_surf.get_height() // 2))
            squares_right_edge = sq_x0 + sq_size
        else:
            active_player = getattr(self, "_active_mapping_player", 1)
            nav_player    = getattr(self, "_ctrl_map_player_sel", active_player)
            reassign_pending_player = self.mapping_reassign_pending_player()
            max_players = CONSOLE_MAX_PLAYERS.get(console, 4)
            for p in range(1, max_players + 1):
                sq_x = sq_x0 + (p - 1) * (sq_size + sq_gap)
                connected = player_is_connected(p) if p > 1 else True
                is_active = (p == active_player)
                is_navved = (is_focused_row and not getattr(self, "_ctrl_map_on_pick", False)
                             and (p == nav_player))
                is_armed  = (reassign_pending_player == p)
                if is_armed:
                    fill = (200, 160, 30)
                elif is_active:
                    fill = ui_color
                elif connected:
                    fill = (70, 70, 95)
                else:
                    fill = (35, 35, 45)
                txt_col = (20, 20, 20) if (is_active or is_armed) else (
                    (200, 200, 210) if connected else (90, 90, 95))
                pygame.draw.rect(surface, fill, (sq_x, sq_y, sq_size, sq_size), border_radius=6)
                if is_navved:
                    pygame.draw.rect(surface, (255, 255, 255), (sq_x, sq_y, sq_size, sq_size), 2, border_radius=6)
                num_surf = f_opt.render(str(p), True, txt_col)
                surface.blit(num_surf, (sq_x + sq_size // 2 - num_surf.get_width() // 2,
                                         sq_y + sq_size // 2 - num_surf.get_height() // 2))
            squares_right_edge = sq_x0 + max_players * (sq_size + sq_gap) - sq_gap

        pick_x = squares_right_edge + 8 + sq_gap
        _pick_text_w = f_small.size("PICK CONTROLLER")[0]
        pick_w = max(108, _pick_text_w + 20)
        pick_rect = pygame.Rect(pick_x, sq_y, pick_w, sq_size)
        pick_is_navved = is_focused_row and getattr(self, "_ctrl_map_on_pick", False)
        if console == "MENU":
            pick_pickable = True
        else:
            active_player = getattr(self, "_active_mapping_player", 1)
            nav_player     = getattr(self, "_ctrl_map_player_sel", active_player)
            pick_target    = nav_player if is_focused_row else active_player
            pick_pickable  = self._player_slot_pickable(pick_target)
        pick_col = (90, 90, 95) if not pick_pickable else (ui_color if pick_is_navved else (200, 200, 210))
        pygame.draw.rect(surface, (45, 45, 55), pick_rect, border_radius=6)
        pygame.draw.rect(surface, pick_col, pick_rect, SELECTION_BORDER_WIDTH, border_radius=6)
        pick_lbl = f_small.render("PICK CONTROLLER", True, pick_col)
        surface.blit(pick_lbl, (pick_rect.centerx - pick_lbl.get_width() // 2,
                                 pick_rect.centery - pick_lbl.get_height() // 2))

        cursor = getattr(self, "_ctrl_map_cursor", 0)

        # VALUES CHANGE HERE
        opt_gap    = 40
        gap_top    = wireframe_bottom + 14
        if console == "NES":
            gap_top -= int(h * 0.04)
        if console == "N64":
            opt_y = gap_top
        else:
            opt_y = gap_top + 20
        center_x   = x + w // 2

        def _draw_option(idx, label, y_pos, extra_right=None, warn=False):
            is_sel = (cursor == idx)
            col    = (255, 90, 90) if warn else (ui_color if is_sel else (180, 180, 180))
            lbl    = f_opt.render(label, True, col)
            lx     = center_x - lbl.get_width() // 2
            if extra_right:
                lx = center_x - (lbl.get_width() + 10 + extra_right.get_width()) // 2

            surface.blit(lbl, (lx, y_pos))
            if extra_right:
                surface.blit(extra_right, (lx + lbl.get_width() + 10, y_pos))
            return pygame.Rect(lx - 5, y_pos - 3,
                               lbl.get_width() + 10 + (10 + extra_right.get_width() if extra_right else 0),
                               lbl.get_height() + 6)

        _reject_ts = getattr(self, "_remap_reject_flash", None)
        _reject_active = (active_btn is not None and _reject_ts is not None
                           and (time.monotonic() - _reject_ts) < 1.5)
        if _reject_active:
            _reject_lbl = getattr(self, "_remap_reject_label", "that button")
            map_label = f"{_reject_lbl} ALREADY MAPPED — press a different button"
        elif seq_running and active_btn and active_btn in BTN_SEQ:
            seq_idx  = BTN_SEQ.index(active_btn)
            total_btns = len(BTN_SEQ)
            map_label = f"MAPPING {seq_idx + 1}/{total_btns}  —  press button for: {active_btn}"
        elif seq_running and active_btn:
            map_label = f"MAPPING  —  press button for: {active_btn}"
        else:
            map_label = "[ MAP CONTROLLER ]"
        r0 = _draw_option(1, map_label, opt_y, warn=_reject_active)

        is_n64 = (console == "N64")
        if not is_n64:
            sad      = self.ctrl_mapping.get("stick_as_dpad", False)
            sw_bg    = (40, 167, 69) if sad else (45, 65, 100)
            sw_w, sw_h = 55, 24
            sw_surf  = pygame.Surface((sw_w, sw_h), pygame.SRCALPHA)
            pygame.draw.rect(sw_surf, sw_bg, (0, 0, sw_w, sw_h), border_radius=12)
            handle_x = sw_w - 14 if sad else 14
            pygame.draw.circle(sw_surf, (255, 255, 255), (handle_x, sw_h // 2), 9)
            r1 = _draw_option(2, "L-STICK AS D-PAD", opt_y + opt_gap, sw_surf)
            close_idx = 3
        else:
            r1 = None
            close_idx = 2

        OFF_RED = (220, 53, 69)
        close_w, close_h = 110, 40
        close_x = x + w - close_w - 25
        close_y = y + h - 52
        is_close_sel = (cursor == close_idx)
        pygame.draw.rect(surface, OFF_RED, (close_x, close_y, close_w, close_h), border_radius=4)
        if is_close_sel:
            pygame.draw.rect(surface, ui_color, (close_x, close_y, close_w, close_h), SELECTION_BORDER_WIDTH, border_radius=4)
        lbl_close = f_opt.render("Close", True, (255, 255, 255))
        surface.blit(lbl_close, (close_x + close_w // 2 - lbl_close.get_width() // 2,
                                  close_y + close_h // 2 - lbl_close.get_height() // 2))
        r2 = pygame.Rect(close_x, close_y, close_w, close_h)

        if seq_running and active_btn:
            ctrl_bottom = y + h - 6
            hint_lbl = f_small.render("BACKSPACE to skip button   ESC to cancel remap", True, (120, 120, 60))
            surface.blit(hint_lbl, (center_x - hint_lbl.get_width() // 2, ctrl_bottom - 14))

        if _target_w > 0 and _target_h > 0:
            _scaled_panel = pygame.transform.smoothscale(surface, (_target_w, _target_h))
            _real_surface.blit(_scaled_panel, (_target_x, _target_y))

    def start_sequential_remap(self, console=None):
        """Begin the auto-advance sequential remap flow (all buttons in order)."""
        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        cdef    = _get_console_def(console)
        BTN_SEQ = cdef["btn_seq"]
        if not BTN_SEQ:
            log.info(f"[GAMEDECK MAPPING] {console} has no mappable buttons.")
            return
        self._seq_remap_active = True
        self.ctrl_mapping_active_btn = BTN_SEQ[0]
        self._remap_await_release  = True
        self._remap_last_capture_ts = time.monotonic()
        if not hasattr(self, "_ctrl_labels"):
            self._ctrl_labels = {}
        self._remap_used_bits = []
        self._remap_reject_flash = None
        log.info(f"[GAMEDECK MAPPING] Sequential remap started for {console} — press controller button for: {BTN_SEQ[0]}")

    def poll_xinput_for_remap(self, console=None):
        """Capture one physical button from any supported controller backend."""
        if self.ctrl_mapping_active_btn is None:
            return

        if console is None:
            console = getattr(self, "_active_mapping_console", "GBA") or "GBA"
        cdef    = _get_console_def(console)
        BTN_SEQ = cdef["btn_seq"]

        _XI_LABELS = {
            _XI_UP: "Dpad U", _XI_DOWN: "Dpad D", _XI_LEFT: "Dpad L", _XI_RIGHT: "Dpad R",
            _XI_START: "Start", _XI_BACK: "Select",
            _XI_LB: "L", _XI_RB: "R",
            _XI_A: "A", _XI_B: "B", _XI_X: "X", _XI_Y: "Y",
            _XI_LT: "LT", _XI_RT: "RT",
        }
        for _di_label_index in range(32):
            _XI_LABELS.setdefault(
                _di_raw_button_bit(_di_label_index),
                f"Button {_di_label_index + 1}",
            )

        # VALUES CHANGE HERE
        TRIGGER_PRESS_THRESHOLD = 30

        remap_player = getattr(self, "_active_mapping_player", 1) or 1
        gp = player1_gpstate() if remap_player == 1 else player_gpstate(remap_player)

        _remap_diag_snap = (gp.wButtons, gp.bLT, gp.bRT) if gp is not None else None
        if _remap_diag_snap != getattr(self, "_remap_diag_last", "unset"):
            self._remap_diag_last = _remap_diag_snap

        if gp is None:
            return

        is_dinput = getattr(gp, "source", "xinput") == "dinput"
        raw_buttons = int(getattr(gp, "raw_buttons", 0)) if is_dinput else 0
        btns   = gp.wButtons
        cur_lt = gp.bLT >= TRIGGER_PRESS_THRESHOLD
        cur_rt = gp.bRT >= TRIGGER_PRESS_THRESHOLD
        fully_released = (
            (raw_buttons == 0 if is_dinput else btns == 0)
            and not cur_lt and not cur_rt
        )

        if getattr(self, "_remap_await_release", False):
            if fully_released:
                self._remap_await_release = False
                self._last_xinput_btns = 0
                self._last_remap_raw_buttons = 0
                self._last_xinput_lt = False
                self._last_xinput_rt = False
            else:
                self._last_remap_raw_buttons = raw_buttons
            return

        last_btns = getattr(self, "_last_xinput_btns", 0)
        last_raw  = getattr(self, "_last_remap_raw_buttons", 0)
        last_lt   = getattr(self, "_last_xinput_lt", False)
        last_rt   = getattr(self, "_last_xinput_rt", False)
        state_unchanged = (
            (raw_buttons == last_raw if is_dinput else btns == last_btns)
            and cur_lt == last_lt and cur_rt == last_rt
        )
        if state_unchanged:
            return
        self._last_xinput_btns = btns
        self._last_remap_raw_buttons = raw_buttons
        self._last_xinput_lt = cur_lt
        self._last_xinput_rt = cur_rt

        if fully_released:
            return

        pressed_bit = None
        if is_dinput and raw_buttons:
            new_raw = raw_buttons & ~last_raw
            if new_raw:
                low_bit = new_raw & -new_raw
                raw_index = low_bit.bit_length() - 1
                pressed_bit = _di_raw_button_bit(raw_index)

        if pressed_bit is None:
            for bit in (
                _XI_UP, _XI_DOWN, _XI_LEFT, _XI_RIGHT, _XI_START, _XI_BACK,
                _XI_LS, _XI_RS, _XI_LB, _XI_RB, _XI_A, _XI_B, _XI_X, _XI_Y,
            ):
                if btns & bit:
                    pressed_bit = bit
                    break

        if pressed_bit is None:
            if cur_lt and not last_lt:
                pressed_bit = _XI_LT
            elif cur_rt and not last_rt:
                pressed_bit = _XI_RT
        if pressed_bit is None:
            return

        REMAP_CAPTURE_COOLDOWN = 0.30
        now = time.monotonic()
        if now - getattr(self, "_remap_last_capture_ts", 0.0) < REMAP_CAPTURE_COOLDOWN:
            return

        active = self.ctrl_mapping_active_btn
        xi_label = _XI_LABELS.get(pressed_bit, f"XI{pressed_bit:04X}")
        if pressed_bit in getattr(self, "_remap_used_bits", []):
            self._remap_reject_flash = time.monotonic()
            self._remap_reject_label = xi_label
            log.info(f"[GAMEDECK MAPPING] Rejected {xi_label} for {active} -- "
                  f"already mapped to another button on {console} this pass.")
            self._remap_await_release = True
            self._remap_last_capture_ts = now
            return

        console_default = cdef["default_map"]
        target_vk = console_default.get(active, 0)
        self.ctrl_mapping[active] = target_vk
        remap_player = getattr(self, "_active_mapping_player", 1) or 1
        self._libretro_xinput_map.setdefault(console, {}).setdefault(
            remap_player, {}
        )[active] = pressed_bit

        if not hasattr(self, "_remap_used_bits"):
            self._remap_used_bits = []
        self._remap_used_bits.append(pressed_bit)

        if not hasattr(self, "_ctrl_labels"):
            self._ctrl_labels = {}
        self._ctrl_labels[active] = xi_label
        log.info(f"[GAMEDECK MAPPING] {active} → {xi_label} (VK {target_vk:#04x})")

        self._remap_await_release = True
        self._remap_last_capture_ts = now

        seq_running = getattr(self, "_seq_remap_active", False)
        if seq_running:
            if active in BTN_SEQ:
                idx = BTN_SEQ.index(active)
                if idx + 1 < len(BTN_SEQ):
                    next_btn = BTN_SEQ[idx + 1]
                    self.ctrl_mapping_active_btn = next_btn
                    log.info(f"[GAMEDECK MAPPING] Next: {next_btn}")
                else:
                    self._seq_remap_active = False
                    self.ctrl_mapping_active_btn = None
                    self._save_controller_mapping(console)
                    self._save_libretro_xinput_map(console)
                    if console == "MENU":
                        self._apply_menu_nav_mapping_from_libretro_map()
                    log.info(f"[GAMEDECK MAPPING] Sequential remap complete for {console} — saved.")
            else:
                self._seq_remap_active = False
                self.ctrl_mapping_active_btn = None
                self._save_controller_mapping(console)
                self._save_libretro_xinput_map(console)
                if console == "MENU":
                    self._apply_menu_nav_mapping_from_libretro_map()
        else:
            self.ctrl_mapping_active_btn = None
            self._save_controller_mapping(console)
            self._save_libretro_xinput_map(console)
            if console == "MENU":
                self._apply_menu_nav_mapping_from_libretro_map()

    # ========================================================================
    # SECTION: Rendering: fonts, logos & legend
    # ========================================================================

    def _ensure_fonts(self):
        if self._font_big is None:
            self._font_big   = pygame.font.SysFont("Arial", 30, bold=True)
            self._font_mid   = pygame.font.SysFont("Arial", 22, bold=True)
            self._font_small = pygame.font.SysFont("Courier New", 19, bold=True)

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

    def _get_boxed_logo(self, console, box_w, box_h):
        """Fit the console logo into a (box_w, box_h) box with the same crop and fit math as the Games-tab menu buttons."""
        # WARNING: must match ui.py's _crop_logo_to_artwork / _fit_logo_to_box (84% of box width, 76% of box height).
        cache = self._boxed_logo_cache.setdefault(console, {})
        key = (box_w, box_h)
        if key in cache:
            return cache[key]

        if console in self._header_logo_raw_cache:
            raw = self._header_logo_raw_cache[console]
        else:
            path = os.path.join(self.logos_dir, f"{console}.png")
            raw = pygame.image.load(path).convert_alpha() if os.path.exists(path) else None
            if raw is not None:
                try:
                    bounds = raw.get_bounding_rect(min_alpha=10)
                    if (bounds.width > 0 and bounds.height > 0
                            and (bounds.width != raw.get_width() or bounds.height != raw.get_height())):
                        raw = raw.subsurface(bounds).copy()
                except Exception:
                    pass
            self._header_logo_raw_cache[console] = raw

        surf = None
        if raw is not None:
            try:
                raw_w, raw_h = raw.get_width(), raw.get_height()
                if raw_w > 0 and raw_h > 0:
                    target_w = max(1, int(box_w * 0.84))
                    target_h = max(1, int(box_h * 0.76))
                    ratio = min(target_w / raw_w, target_h / raw_h)
                    scaled_w = max(1, int(round(raw_w * ratio)))
                    scaled_h = max(1, int(round(raw_h * ratio)))
                    surf = pygame.transform.smoothscale(raw, (scaled_w, scaled_h))
            except Exception:
                surf = None

        cache[key] = surf
        return surf

    def set_theme(self, ui_color):
        if ui_color: self.ui_color = ui_color

    def _draw_legend(self, screen, cx, y, h, groups):
        f     = self._font_small
        col_w = 130
        # VALUES CHANGE HERE
        row_h = 20
        total_w = col_w * len(groups)
        block_x = cx - total_w // 2
        y_title = y + h - 3 * row_h - 8
        y_ctrl  = y_title + row_h
        y_kb    = y_ctrl  + row_h
        tc = self.list_text_color
        title_col = tc
        key_col   = (max(0, tc[0] - 60), max(0, tc[1] - 60), max(0, tc[2] - 60))
        for i, (action, ctrl_key, kb_key) in enumerate(groups):
            col_cx = block_x + i * col_w + col_w // 2
            for text, color, row_y in [
                (action,   title_col, y_title),
                (ctrl_key, key_col,   y_ctrl),
                (kb_key,   key_col,   y_kb),
            ]:
                lbl = f.render(text, True, color)
                screen.blit(lbl, (col_cx - lbl.get_width() // 2, row_y))

    # ========================================================================
    # SECTION: Rendering: console carousel
    # ========================================================================

    def render(self, screen, rect, crt_cfg=None):
        self._ensure_fonts()
        self._active_crt_cfg = crt_cfg
        x, y, w, h = rect
        cx = x + w // 2

        bg_surf = self._get_menu_bg_surface()
        if bg_surf is not None:
            try:
                scaled_bg = self._cover_scale_bg(bg_surf, w, h)
                screen.blit(scaled_bg, (x, y))
            except Exception:
                screen.fill((0, 0, 0), rect)
        else:
            screen.fill((0, 0, 0), rect)

        if self.mode == "GAME":
            if getattr(self, "emulator_loading", False):
                screen.fill((0, 0, 0), rect)

                custom_splash_shown = False
                if self.active_console and self._is_console_splash_enabled(self.active_console):
                    if self._console_splash_is_video:
                        self._render_console_splash_video_frame(screen, rect)
                        return
                    custom_surf = self._get_console_custom_splash_surface(self.active_console)
                    if custom_surf is not None:
                        scaled = pygame.transform.smoothscale(custom_surf, (w, h))
                        screen.blit(scaled, (x, y))
                        custom_splash_shown = True

                pct = getattr(self, "emulator_load_pct", 0)
                msg = getattr(self, "emulator_load_msg", "Loading...")

                logo_target_h = max(1, int(h * 0.32))
                logo_centre_y = y + int(h * 0.28)
                logo_surf = None
                if self.active_console:
                    logo_surf = self._get_logo(self.active_console, logo_target_h)
                if logo_surf is not None:
                    screen.blit(logo_surf,
                                (cx - logo_surf.get_width() // 2,
                                 logo_centre_y - logo_surf.get_height() // 2))
                elif not custom_splash_shown:
                    f_big = self._font_big or self._font_mid
                    lbl_c = f_big.render(
                        CONSOLE_LABELS.get(self.active_console, self.active_console or ""),
                        True, self.ui_color)
                    screen.blit(lbl_c, (cx - lbl_c.get_width() // 2,
                                        logo_centre_y - lbl_c.get_height() // 2))

                if not custom_splash_shown:
                    lbl_m = self._font_mid.render(msg, True, (150, 150, 150))
                    screen.blit(lbl_m, (cx - lbl_m.get_width() // 2, y + h // 2 - 30))

                display_pct = pct
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

            else:
                screen.fill((0, 0, 0), rect)

            return

        if self.mode == "DVD_PLAYER":
            self._render_dvd_player(screen, rect)
            return

        if self.mode == "BROWSE":
            if self.scroll_style == "side":
                self._render_console_side(screen, rect)
            elif self.scroll_style == "updown":
                self._render_console_updown(screen, rect)
            else:
                self._render_carousel(screen, rect)
        elif self.mode == "ROMLIST": self._render_romlist(screen, rect)

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
                lbl  = font.render(CONSOLE_LABELS.get(console, console), True, self.list_text_color)
                screen.blit(lbl, (cx - lbl.get_width() // 2, slot_y - lbl.get_height() // 2))

        self._draw_legend(screen, cx, y, h, [
            ("Navigate", "←↑↓→", "W/A/S/D"),
            ("Select",   "A",   "Enter"),
            ("Back",     "B",   "ESC"),
        ])

    # VALUES CHANGE HERE
    _NAV_ARROW_SIZE = 15

    def _draw_console_switch_arrow(self, screen, direction, cx, cy, can_switch, flashed):
        """Draw a filled triangle pointing `direction` ('left'/'right'/'up'/'down') instead of a font glyph."""
        if flashed:
            color = self.ui_color
        elif not can_switch:
            color = (90, 90, 90)
        else:
            color = self.list_text_color

        s = self._NAV_ARROW_SIZE
        if direction == "left":
            pts = [(cx + s, cy - s), (cx + s, cy + s), (cx - s, cy)]
        elif direction == "right":
            pts = [(cx - s, cy - s), (cx - s, cy + s), (cx + s, cy)]
        elif direction == "up":
            pts = [(cx - s, cy + s), (cx + s, cy + s), (cx, cy - s)]
        else:
            pts = [(cx - s, cy - s), (cx + s, cy - s), (cx, cy + s)]

        pygame.draw.polygon(screen, color, pts)

    def _render_console_single(self, screen, rect, orientation):
        """Shared renderer for the 'side' and 'updown' console-selector styles: one centered logo/label with a pair of arrows hugging it."""
        x, y, w, h = rect
        cx = x + w // 2
        cy = y + h // 2
        n = len(self.consoles)
        if n == 0:
            return

        can_switch = n > 1
        console = self.consoles[self.console_index]
        logo_h  = int(h * 0.38)
        logo    = self._get_logo(console, logo_h)
        if logo is not None:
            logo_w, logo_h_actual = logo.get_width(), logo.get_height()
            screen.blit(logo, (cx - logo_w // 2, cy - logo_h_actual // 2))
        else:
            lbl = self._font_big.render(CONSOLE_LABELS.get(console, console), True, self.list_text_color)
            logo_w, logo_h_actual = lbl.get_width(), lbl.get_height()
            screen.blit(lbl, (cx - logo_w // 2, cy - logo_h_actual // 2))

        now = pygame.time.get_ticks()
        flash_active = (self._console_arrow_flash_dir is not None and
                        now < self._console_arrow_flash_until)
        if not flash_active:
            self._console_arrow_flash_dir = None

        # VALUES CHANGE HERE
        ARROW_PAD = 46
        if orientation == "side":
            arrow_gap = logo_w // 2 + ARROW_PAD
            self._draw_console_switch_arrow(
                screen, "left", cx - arrow_gap, cy, can_switch,
                flash_active and self._console_arrow_flash_dir == "left")
            self._draw_console_switch_arrow(
                screen, "right", cx + arrow_gap, cy, can_switch,
                flash_active and self._console_arrow_flash_dir == "right")
            nav_hint = ("Navigate", "\u2190/\u2192", "A/D")
        else:
            arrow_gap = logo_h_actual // 2 + ARROW_PAD
            self._draw_console_switch_arrow(
                screen, "up", cx, cy - arrow_gap, can_switch,
                flash_active and self._console_arrow_flash_dir == "up")
            self._draw_console_switch_arrow(
                screen, "down", cx, cy + arrow_gap, can_switch,
                flash_active and self._console_arrow_flash_dir == "down")
            nav_hint = ("Navigate", "\u2191/\u2193", "W/S")

        self._draw_legend(screen, cx, y, h, [
            nav_hint,
            ("Select", "A", "Enter"),
            ("Back",   "B", "ESC"),
        ])

    def _render_console_side(self, screen, rect):
        self._render_console_single(screen, rect, "side")

    def _render_console_updown(self, screen, rect):
        self._render_console_single(screen, rect, "updown")

    # ========================================================================
    # SECTION: Rendering: ROM list
    # ========================================================================

    def _draw_romlist_error_banner(self, screen, cx, top_y):
        if not self._romlist_error_msg:
            return
        now = pygame.time.get_ticks()
        remaining = self._romlist_error_until - now
        if remaining <= 0:
            self._romlist_error_msg = ""
            return
        try:
            FADE_MS = 500
            alpha = 255 if remaining > FADE_MS else int(255 * remaining / FADE_MS)
            lbl = self._font_mid.render(self._romlist_error_msg, True, (255, 90, 90))
            lbl.set_alpha(alpha)
            screen.blit(lbl, (cx - lbl.get_width() // 2, top_y + 6))
        except Exception:
            pass

    def _draw_romlist_console_arrows(self, screen, x, y, w, h, list_top_offset, groups=None):
        cx = x + w // 2

        col_w   = 130
        groups  = groups or []
        back_i  = len(groups) - 1
        total_w = col_w * len(groups) if groups else col_w
        block_x = cx - total_w // 2
        back_col_cx = block_x + back_i * col_w + col_w // 2 if groups else cx

        # VALUES CHANGE HERE
        PUSH_PAST_BACK = 140

        arrow_col_x = back_col_cx + PUSH_PAST_BACK

        _arrow_right_margin = 40
        arrow_col_x = min(arrow_col_x, x + w - _arrow_right_margin)

        list_area_top    = y + list_top_offset
        # VALUES CHANGE HERE
        list_area_bottom = y + h - 90
        list_area_mid    = (list_area_top + list_area_bottom) // 2
        arrow_gap = 46
        arrow_up_y   = list_area_mid - arrow_gap
        arrow_down_y = list_area_mid + arrow_gap

        focused = (self.romlist_focus == "arrows")
        can_switch = len(self.consoles) > 1

        for direction, cy, is_up in (("up", arrow_up_y, True), ("down", arrow_down_y, False)):
            sel = focused and (self.romlist_arrow_sel == ("up" if is_up else "down"))
            self._draw_console_switch_arrow(screen, direction, arrow_col_x, cy, can_switch, sel)

    def _render_romlist(self, screen, rect):
        x, y, w, h = rect
        cx = x + w // 2
        console = self.active_console or ""

        _BTN_INSET_BOX_W = 226
        _BTN_INSET_BOX_H = 72
        # VALUES CHANGE HERE
        _HEADER_TALLER   = 1.35
        _scale  = w / 1036.0
        box_w   = _BTN_INSET_BOX_W * _scale
        box_h   = _BTN_INSET_BOX_H * _scale * _HEADER_TALLER
        box_x   = cx - box_w / 2
        box_y   = y + 20
        logo_header = self._get_boxed_logo(console, box_w, box_h)

        if logo_header is not None:
            lx = box_x + (box_w - logo_header.get_width()) / 2
            ly = box_y + (box_h - logo_header.get_height()) / 2
            screen.blit(logo_header, (int(lx), int(ly)))
            list_top_offset = 20 + int(box_h) + 20
        else:
            title = self._font_big.render(CONSOLE_LABELS.get(console, console), True, self.list_text_color)
            screen.blit(title, (cx - title.get_width() // 2, y + 24))
            list_top_offset = 90

        _romlist_groups = [
            ("Navigate", "\u2190\u2191\u2193\u2192", "W/A/S/D"),
            ("Launch",   "A",                        "Enter"),
            ("Back",     "B",                        "ESC"),
        ]

        self._poll_scan_pending()
        self._poll_live_rescan()
        self._refresh_dvd_root_labels()

        # WARNING: draw the READING FOLDER box last, at every exit, so it sits on top of the list.
        _busy_due = bool(self._scan_pending and self._scan_started_ticks and
                         (pygame.time.get_ticks() - self._scan_started_ticks) >= 1000)

        self._poll_rom_hold()

        self._draw_romlist_error_banner(screen, cx, y + list_top_offset)

        if not self.roms:
            if not self._scan_pending:
                msg = self._font_mid.render(self.status_msg or "No files found.", True, (200, 80, 80))
                screen.blit(msg, (cx - msg.get_width() // 2, y + h // 2 - 12))
            self._draw_romlist_console_arrows(screen, x, y, w, h, list_top_offset, groups=_romlist_groups)
            self._draw_legend(screen, cx, y, h, _romlist_groups)
            if _busy_due:
                self._draw_romlist_busy_overlay(screen, x, y, w, h)
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
            selected     = (i == self.rom_index) and self.romlist_focus == "list"
            if display_name.startswith("[MISSING] ") or display_name.startswith("[OFFLINE] "):
                color = (255, 150, 90) if selected else (200, 100, 60)
            else:
                color        = self.ui_color if selected else self.list_text_color
            display_str  = f"> {display_name} <" if selected else display_name
            lbl = self._font_mid.render(display_str, True, color)
            screen.blit(lbl, (cx - lbl.get_width() // 2, list_start_y + row * line_h))

        self._draw_romlist_console_arrows(screen, x, y, w, h, list_top_offset, groups=_romlist_groups)

        self._draw_legend(screen, cx, y, h, _romlist_groups)

        if _busy_due:
            self._draw_romlist_busy_overlay(screen, x, y, w, h)

    def _draw_romlist_busy_overlay(self, screen, x, y, w, h):
        import colorsys
        main_module   = sys.modules.get("__main__")
        theme_bg_hue  = 220
        theme_ui_hue  = 140
        if main_module and hasattr(main_module, "db") and main_module.db:
            theme_bg_hue = main_module.db.config.get("theme_bg_hue", 220)
            theme_ui_hue = main_module.db.config.get("theme_ui_hue", 140)

        r_panel, g_panel, b_panel = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.45, 0.16)
        r_track, g_track, b_track = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.55, 0.10)
        r_ui,    g_ui,    b_ui    = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
        panel_c = (int(r_panel * 255), int(g_panel * 255), int(b_panel * 255))
        track_c = (int(r_track * 255), int(g_track * 255), int(b_track * 255))
        ui_c    = (int(r_ui * 255),    int(g_ui * 255),    int(b_ui * 255))

        box_w, box_h = 340, 92
        bx = x + (w - box_w) // 2
        by = y + (h - box_h) // 2

        dim = pygame.Surface((w, h), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 90))
        screen.blit(dim, (x, y))

        pygame.draw.rect(screen, panel_c, (bx, by, box_w, box_h))
        pygame.draw.rect(screen, ui_c, (bx, by, box_w, box_h), 2)

        f_lbl = _cached_font("Courier New", 18, bold=True)
        lbl   = f_lbl.render("READING FOLDER...", True, ui_c)
        screen.blit(lbl, (bx + (box_w - lbl.get_width()) // 2, by + 16))

        track_w, track_h = box_w - 40, 14
        tx = bx + 20
        ty = by + box_h - 18 - track_h
        pygame.draw.rect(screen, track_c, (tx, ty, track_w, track_h))
        pygame.draw.rect(screen, ui_c, (tx, ty, track_w, track_h), 1)

        chunk_w   = max(30, track_w // 3)
        period_ms = 1100
        t     = (pygame.time.get_ticks() % period_ms) / period_ms
        phase = t * 2
        if phase > 1:
            phase = 2 - phase
        chunk_x = tx + int((track_w - chunk_w) * phase)
        pygame.draw.rect(screen, ui_c, (chunk_x, ty, chunk_w, track_h))

    # ========================================================================
    # SECTION: Splash & DVD load screens
    # ========================================================================

    def _dvd_path_is_video(self, path):
        return os.path.splitext(path)[1].lower() in self.DVD_SPLASH_VIDEO_EXTS

    def _dvd_active_splash_ms(self):
        if self.dvd_splash_enabled and self._get_dvd_custom_splash_surface() is not None:
            return self.DVD_CUSTOM_SPLASH_MS
        return self.DVD_LOAD_SCREEN_MS

    def _get_dvd_custom_splash_surface(self):
        path = self.dvd_splash_path
        if not path or self._dvd_path_is_video(path):
            self._dvd_custom_splash_surf = None
            self._dvd_custom_splash_surf_path = None
            return None
        if self._dvd_custom_splash_surf is not None and self._dvd_custom_splash_surf_path == path:
            return self._dvd_custom_splash_surf
        try:
            surf = pygame.image.load(path).convert_alpha()
        except Exception as e:
            log.warning("Failed to load custom DVD splash image %s: %s", path, e)
            self._dvd_custom_splash_surf = None
            self._dvd_custom_splash_surf_path = path
            return None
        self._dvd_custom_splash_surf = surf
        self._dvd_custom_splash_surf_path = path
        return surf

    def _is_console_splash_enabled(self, console_key):
        if console_key == "DVD":
            return self.dvd_splash_enabled
        return bool(self.console_splash_enabled.get(console_key, False))

    def _get_console_splash_path(self, console_key):
        if console_key == "DVD":
            return self.dvd_splash_path
        return self.console_splash_path.get(console_key, "") or ""

    def _get_console_custom_splash_surface(self, console_key):
        path = self._get_console_splash_path(console_key)
        if not path or self._dvd_path_is_video(path):
            self._console_splash_surf_cache.pop(console_key, None)
            return None
        cached = self._console_splash_surf_cache.get(console_key)
        if cached is not None and cached[0] == path:
            return cached[1]
        try:
            surf = pygame.image.load(path).convert_alpha()
        except Exception as e:
            log.warning("Failed to load custom splash image for %s (%s): %s", console_key, path, e)
            self._console_splash_surf_cache[console_key] = (path, None)
            return None
        self._console_splash_surf_cache[console_key] = (path, surf)
        return surf

    def _cover_scale_bg(self, surf, w, h):
        """Fit `surf` to fill a (w, h) box exactly with no distortion."""
        sw, sh = surf.get_width(), surf.get_height()
        w = max(1, int(w))
        h = max(1, int(h))
        if sw <= 0 or sh <= 0:
            return pygame.transform.smoothscale(surf, (w, h))
        target_ratio = w / h
        src_ratio = sw / sh
        if src_ratio > target_ratio:
            crop_w = max(1, min(sw, round(sh * target_ratio)))
            crop_h = sh
            crop_x = (sw - crop_w) // 2
            crop_y = 0
        else:
            crop_w = sw
            crop_h = max(1, min(sh, round(sw / target_ratio)))
            crop_x = 0
            crop_y = (sh - crop_h) // 2
        try:
            cropped = surf.subsurface(pygame.Rect(crop_x, crop_y, crop_w, crop_h))
        except Exception:
            cropped = surf
        return pygame.transform.smoothscale(cropped, (w, h))

    def _get_menu_bg_surface(self):
        path = self.menu_bg_path
        if not path:
            self._menu_bg_surf_cache = None
            return None
        cached = self._menu_bg_surf_cache
        if cached is not None and cached[0] == path:
            return cached[1]
        try:
            surf = pygame.image.load(path).convert_alpha()
        except Exception as e:
            log.warning("Failed to load custom menu background (%s): %s", path, e)
            self._menu_bg_surf_cache = (path, None)
            return None
        self._menu_bg_surf_cache = (path, surf)
        return surf

    def _render_console_splash_video_frame(self, screen, rect):
        x, y, w, h = rect
        vlc = self.vlc_engine
        if vlc is None:
            screen.fill((0, 0, 0), rect)
            return
        try:
            frame = vlc.get_display_frame()
            if frame:
                surf   = pygame.image.frombuffer(frame, (vlc.width, vlc.height), "BGRA")
                scaled = pygame.transform.smoothscale(surf, (w, h))
                screen.blit(scaled, (x, y))
                self._console_splash_video_last_frame = scaled.copy()
                return
        except Exception as e:
            _log_warn_throttled("console_splash_video_blit", "Console splash video frame grab/blit failed: %s", e)
        if self._console_splash_video_last_frame is not None:
            screen.blit(self._console_splash_video_last_frame, (x, y))
            return
        screen.fill((0, 0, 0), rect)

    def _render_dvd_load_screen(self, screen, rect):
        self._ensure_fonts()
        x, y, w, h = rect
        cx = x + w // 2
        screen.fill((0, 0, 0), rect)

        if self.dvd_splash_enabled:
            custom_surf = self._get_dvd_custom_splash_surface()
            if custom_surf is not None:
                sw, sh = custom_surf.get_size()
                if sw > 0 and sh > 0:
                    scale = min(w / sw, h / sh)
                    dw, dh = max(1, int(sw * scale)), max(1, int(sh * scale))
                    scaled = pygame.transform.smoothscale(custom_surf, (dw, dh))
                    screen.blit(scaled, (cx - dw // 2, y + h // 2 - dh // 2))
                    return

        elapsed = pygame.time.get_ticks() - getattr(self, "_dvd_splash_start_time", 0)
        pct = max(0, min(100, int(elapsed * 100 / self.DVD_LOAD_SCREEN_MS)))
        msg = "Loading DVD..."

        logo_target_h = max(1, int(h * 0.32))
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

    def _render_dvd_splash_video_frame(self, screen, rect):
        x, y, w, h = rect
        vlc = self.vlc_engine
        if vlc is None:
            screen.fill((0, 0, 0), rect)
            return
        try:
            frame = vlc.get_display_frame()
            if frame:
                surf = pygame.image.frombuffer(frame, (vlc.width, vlc.height), "BGRA")
                surf = self._apply_crt_cfg_to_surface(surf)
                surf = self._dvd_true_content_surf(vlc, surf)
                # WARNING: the splash video uses the DVD Screen Mode, not the global Video-tab setting.
                _dvd_mode = "normal"
                try:
                    import sys as _sys
                    _main = _sys.modules.get("__main__")
                    if _main and getattr(_main, "db", None) is not None:
                        _dvd_mode = _main.db.config.get("dvd_screen_mode", "normal")
                except Exception:
                    pass
                _main_blit = None
                try:
                    import sys as _sys
                    _main = _sys.modules.get("__main__")
                    _main_blit = getattr(_main, "_blit_video_with_screen_mode", None)
                except Exception:
                    pass
                if callable(_main_blit):
                    _sp_target, _sp_pa = self._dvd_video_fit(screen, surf, pygame.Rect(x, y, w, h), _dvd_mode)
                    _sp_kw = {}
                    _sp_fit_a = self._dvd_fit_aspect(surf)
                    if _sp_fit_a:
                        _sp_kw["fit_aspect"] = _sp_fit_a
                    _main_blit(screen, surf, _sp_target, mode=_dvd_mode, pixel_aspect=_sp_pa, **_sp_kw)
                else:
                    _aspect_fit_blit(screen, surf, rect)
                _safe_rect = pygame.Rect(rect).clip(screen.get_rect())
                if _safe_rect.width > 0 and _safe_rect.height > 0:
                    self._dvd_splash_video_last_frame = screen.subsurface(_safe_rect).copy()
                return
        except Exception as e:
            _log_warn_throttled("dvd_splash_video_blit", "DVD splash video frame grab/blit failed: %s", e)
        if self._dvd_splash_video_last_frame is not None:
            screen.blit(self._dvd_splash_video_last_frame, (x, y))
            return
        screen.fill((0, 0, 0), rect)

    # ========================================================================
    # SECTION: DVD player rendering
    # ========================================================================

    def _dvd_true_content_surf(self, vlc, surf):
        """Crop VLC's baked-in letterbox/pillarbox bars out of a decoded DVD frame."""
        try:
            dw, dh = vlc._decode_w, vlc._decode_h
            cw, ch = vlc.width, vlc.height
            if dw <= 0 or dh <= 0 or (dw == cw and dh == ch):
                return surf
            ox, oy = vlc._letterbox_offset_x, vlc._letterbox_offset_y
            if ox < 0 or oy < 0 or ox + dw > cw or oy + dh > ch:
                return surf
            return surf.subsurface((ox, oy, dw, dh)).copy()
        except Exception as e:
            _log_warn_throttled("dvd_true_content_surf", "DVD frame crop failed, using padded canvas: %s", e)
            return surf

    def _dvd_fit_aspect(self, surf):
        """Return the standard shape (4:3 / 16:9 / 9:16) Normal mode lays this DVD picture out as."""
        # WARNING: must match _snap_standard_aspect in retro_tv_emulator.py so a disc gets the same bars as a TV station of that shape.
        try:
            main = sys.modules.get("__main__")
            snap = getattr(main, "_snap_standard_aspect", None)
            h = surf.get_height()
            if callable(snap) and h > 0:
                return snap(surf.get_width() / h)
        except Exception as e:
            _log_warn_throttled("dvd_fit_aspect",
                                "DVD standard-shape fit unavailable: %s", e)
        return None

    def _dvd_video_fit(self, screen, surf, rect, mode):
        """(target_rect, pixel_aspect) to hand _blit_video_with_screen_mode for a DVD picture."""
        target = pygame.Rect(rect)
        try:
            main = sys.modules.get("__main__")
            fit = getattr(main, "_dvd_bezel_video_fit", None)
            sh = surf.get_height()
            if callable(fit) and sh > 0:
                aspect = surf.get_width() / sh
                snap = getattr(main, "_snap_standard_aspect", None)
                if callable(snap):
                    aspect = snap(aspect) or aspect
                t, pa = fit(target, screen.get_width(), screen.get_height(),
                            aspect, mode)
                if t.width < target.width or t.height < target.height:
                    bar = getattr(main, "_video_bar_color", None)
                    if callable(bar):
                        screen.fill(bar(), target)
                return t, pa
        except Exception as e:
            _log_warn_throttled("dvd_video_fit",
                                "DVD Bezel fit failed, using plain canvas fit: %s", e)
        return target, 1.0

    def _apply_crt_cfg_to_surface(self, surf):
        """Apply the Video-tab Brightness/Contrast/Color/Sharpness/Tint filter to a raw VLC frame surface, mapping crt_cfg keys onto apply_video_effects() parameters."""
        cfg = getattr(self, "_active_crt_cfg", None)
        if not cfg:
            return surf
        return apply_video_effects(
            surf,
            brightness=cfg.get("brightness", 50),
            contrast=cfg.get("contrast", 50),
            color=cfg.get("saturation", 50),
            sharpness=cfg.get("sharpness", 50),
            tint=cfg.get("hue_shift", 50),
        )

    def _render_dvd_player(self, screen, rect):
        x, y, w, h = rect
        cx = x + w // 2

        import sys
        main_module = sys.modules.get("__main__")
        if main_module and getattr(main_module, "dvd_engine", None) is not None:
            self.vlc_engine = main_module.dvd_engine

        vlc = self.vlc_engine
        now = pygame.time.get_ticks()

        self._maybe_auto_apply_dvd_captions(vlc)

        if self.dvd_exit_confirm and now - self.dvd_exit_confirm_shown_at >= self._CONFIRM_BURN_IN_MS:
            log.info("[DVD] Quit-confirm popup timed out (burn-in protection) -- dismissing.")
            self.dvd_exit_confirm = False
        if self.dvd_return_menu_confirm and now - self.dvd_return_menu_confirm_shown_at >= self._CONFIRM_BURN_IN_MS:
            log.info("[DVD] Back-to-main-confirm popup timed out (burn-in protection) -- dismissing.")
            self.dvd_return_menu_confirm = False

        if self.dvd_splash_playing:
            splash_elapsed = now - getattr(self, "_dvd_splash_start_time", 0)
            splash_ended = False
            if self._dvd_splash_is_video:
                try:
                    import vlc as raw_vlc
                    if vlc is not None and vlc.player.get_state() in (raw_vlc.State.Ended, raw_vlc.State.Error):
                        splash_ended = True
                except Exception as e:
                    _log_warn_throttled("dvd_splash_video_state", "DVD splash video state check failed: %s", e)
                if splash_elapsed >= self.DVD_SPLASH_VIDEO_MAX_MS:
                    if not splash_ended:
                        log.info("[DVD] Splash video hit its max-duration safety cap -- cutting to main feature.")
                    splash_ended = True
            else:
                splash_ended = splash_elapsed >= self._dvd_active_splash_ms()

            if splash_ended:
                self.dvd_splash_playing = False
                self.dvd_splash_done    = True
                self._dvd_main_start_time  = pygame.time.get_ticks()
                self._dvd_main_ever_played = False
                _resume_secs = getattr(self, "_dvd_resume_seek_seconds", 0)
                self._dvd_resume_seek_seconds = 0
                self._play_dvd_main(seek_seconds=_resume_secs)
                if _resume_secs:
                    self._set_dvd_osd("RESUMING...", duration_ms=2500)
            else:
                if self._dvd_splash_is_video:
                    self._render_dvd_splash_video_frame(screen, rect)
                else:
                    self._render_dvd_load_screen(screen, rect)
                return

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
                    if _wall_delta > 0 and (abs(_pos_delta - _wall_delta) > 1500):
                        _title_now = getattr(vlc, "_current_title", "?")
                        _log_warn_throttled("dvd_time_jump_detected", f"[DVD DEBUG] TIME-JUMP DETECTED: pos {_last_pos}ms -> {_t_pos}ms "
                              f"(delta={_pos_delta}ms) over {_wall_delta}ms of wall time, "
                              f"title={_title_now}")
                        _tj_title_is_menu = None
                        _tj_title_now = getattr(vlc, "_current_title", None)
                        try:
                            _tj_title_is_menu = vlc._title_is_menu(_tj_title_now)
                        except Exception:
                            pass
                        _tj_overridden = False
                        if _tj_title_is_menu is False and _tj_title_now is not None:
                            try:
                                _tj_overridden = vlc.record_time_jump_veto_evidence(_tj_title_now)
                            except Exception as _e:
                                _log_warn_throttled("dvd_veto_evidence",
                                                     "record_time_jump_veto_evidence() failed: %s", _e)
                        if (self.dvd_in_menu and _tj_title_is_menu is not False) or _tj_overridden:
                            try:
                                vlc.note_menu_activity()
                            except Exception as _e:
                                _log_warn_throttled("dvd_note_menu_activity",
                                                     "note_menu_activity() failed: %s", _e)
                        try:
                            _tail = vlc.get_recent_vlc_log()
                            if _tail:
                                pass
                        except Exception:
                            pass
                self._dvd_jump_watch_wall = _t_now_wall
                self._dvd_jump_watch_pos = _t_pos
            except Exception as _e:
                _log_warn_throttled("dvd_time_jump_watch", "DVD time-jump watch failed this frame: %s", _e)

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
                    self._dvd_iso_watchdog_armed = False
                else:
                    _armed_elapsed = now - self._dvd_iso_watchdog_armed_at
                    _fail_reason = None
                    if (state in (raw_vlc.State.Ended, raw_vlc.State.Error)
                            and _armed_elapsed >= self.DVD_ISO_OPEN_FAIL_GRACE_MS):
                        _fail_reason = f"dvdnav reported {state} after {_armed_elapsed}ms -- not a playable disc image"
                    elif now >= self._dvd_iso_watchdog_deadline:
                        _fail_reason = f"timed out after {self.DVD_ISO_OPEN_TIMEOUT_MS}ms (last state={state})"

                    if _fail_reason:
                        self._dvd_iso_watchdog_armed = False
                        try:
                            _vlc_err = vlc.get_last_error()
                        except Exception:
                            _vlc_err = ""
                        try:
                            _vlc_log_tail = vlc.get_recent_vlc_log()
                        except Exception:
                            _vlc_log_tail = ""
                        log.warning(f"[DVD DEBUG] disc-open watchdog FAILED (source={self.dvd_file_path!r}): "
                              f"{_fail_reason} -- libVLC error: {_vlc_err or '(none reported -- check vlc-debug.log tail below, libvlc_errmsg() is unreliable across threads)'} "
                              f"-- bailing out to DVD list.")
                        if _vlc_log_tail:
                            pass
                        else:
                            pass
                        try: vlc.stop()
                        except Exception as e:
                            log.warning("VLC stop after ISO-open watchdog failure failed: %s", e)
                        self.dvd_playback_active = False
                        self.dvd_splash_done = False
                        self.dvd_paused_frame = None
                        self.dvd_in_menu = False
                        self._close_dvd_highlight_shadow()
                        self.mode = "ROMLIST"
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
                    self._dvd_end_state_since = None
                if (state in (raw_vlc.State.Ended, raw_vlc.State.Stopped)
                        and getattr(self, "_dvd_main_ever_played", False)
                        and not getattr(self, "dvd_resume_pending", False)):
                    if self._dvd_end_state_since is None:
                        self._dvd_end_state_since = now
                    if now - self._dvd_end_state_since >= self.DVD_END_STATE_GRACE_MS:
                        try:
                            _title_at_bail = getattr(vlc, "_current_title", "?")
                        except Exception:
                            _title_at_bail = "?"
                        log.warning(f"[DVD DEBUG] END-STATE WATCHDOG FIRED: state={state} "
                              f"title={_title_at_bail} grace_elapsed_ms={now - self._dvd_end_state_since} "
                              f"(threshold={self.DVD_END_STATE_GRACE_MS}ms) -- bailing out to ROMLIST.")
                        try:
                            _tail = vlc.get_recent_vlc_log()
                            if _tail:
                                pass
                        except Exception:
                            pass
                        self.dvd_playback_active = False
                        self.dvd_splash_done     = False
                        self.dvd_paused_frame    = None
                        self.dvd_in_menu         = False
                        self._close_dvd_highlight_shadow()
                        if self.dvd_resume_enabled and self.dvd_file_path:
                            self._dvd_clear_resume_position(self.dvd_file_path)
                        self.mode = "ROMLIST"
                        self._scan_roms("DVD")
                        return
            except Exception as e:
                _log_warn_throttled("dvd_main_end_check", "DVD main-video-ended state check failed this frame: %s", e)

        frame_drawn = False
        if getattr(self, 'dvd_awaiting_resume', False):
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
                    try:
                        _main_mod_mute = sys.modules.get("__main__")
                        if _main_mod_mute and hasattr(_main_mod_mute, "_lift_buffering_mute"):
                            _mute_ch = (_main_mod_mute.app_state.get("current_channel", "03")
                                        if hasattr(_main_mod_mute, "app_state") else "03")
                            _main_mod_mute._lift_buffering_mute(_mute_ch, "dvd-first-frame")
                    except Exception as _e:
                        _log_warn_throttled("dvd_lift_buffering_mute",
                                             "DVD first-frame mute-guard lift failed: %s", _e)
                    surf = pygame.image.frombuffer(frame, (vlc.width, vlc.height), "BGRA")
                    surf = self._apply_crt_cfg_to_surface(surf)
                    surf = self._dvd_true_content_surf(vlc, surf)
                    _dvd_mode = "normal"
                    _main_mod_bl = sys.modules.get("__main__")
                    if _main_mod_bl and hasattr(_main_mod_bl, "db") and _main_mod_bl.db is not None:
                        _dvd_mode = _main_mod_bl.db.config.get("dvd_screen_mode", "normal")
                    _dvd_blit_geom = {}
                    _dvd_blit_timing = {}
                    _dvd_fit_rect, _dvd_fit_pa = pygame.Rect(x, y, w, h), 1.0
                    if _main_mod_bl and hasattr(_main_mod_bl, "_blit_video_with_screen_mode"):
                        _dvd_fit_rect, _dvd_fit_pa = self._dvd_video_fit(
                            screen, surf, pygame.Rect(x, y, w, h), _dvd_mode)
                        _dvd_fit_kw = {}
                        _dvd_fit_a = self._dvd_fit_aspect(surf)
                        if _dvd_fit_a:
                            _dvd_fit_kw["fit_aspect"] = _dvd_fit_a
                        _main_mod_bl._blit_video_with_screen_mode(
                            screen, surf, _dvd_fit_rect, mode=_dvd_mode,
                            geom_out=_dvd_blit_geom, timing_out=_dvd_blit_timing,
                            pixel_aspect=_dvd_fit_pa, **_dvd_fit_kw)
                        _dvd_rbg_key = (
                            (x, y, w, h), surf.get_size(), _dvd_mode,
                            tuple(sorted(_dvd_blit_geom.items())) if _dvd_blit_geom else None,
                            tuple(_dvd_fit_rect), round(_dvd_fit_pa, 4),
                        )
                        if getattr(self, "_dvd_real_blit_log_key", None) != _dvd_rbg_key:
                            self._dvd_real_blit_log_key = _dvd_rbg_key
                            _dw = _dvd_blit_geom.get("draw_w")
                            _dh = _dvd_blit_geom.get("draw_h")
                            log.info(
                                "[DVD REAL VIDEO BLIT] mode=%s target_rect=%s fit_rect=%s pixel_aspect=%.4f "
                                "true_content_surf=%sx%s "
                                "actual_draw=(%s,%s,%s,%s) actual_draw_aspect=%s",
                                _dvd_mode, (x, y, w, h), tuple(_dvd_fit_rect), _dvd_fit_pa,
                                surf.get_width(), surf.get_height(),
                                _dvd_blit_geom.get("dest_x"), _dvd_blit_geom.get("dest_y"), _dw, _dh,
                                ("%.4f" % (_dw / _dh)) if _dw and _dh else None,
                            )
                    else:
                        _aspect_fit_blit(screen, surf, (x, y, w, h))
                    # WARNING: clip before subsurface() so the rectangle stays inside the screen.
                    _safe_rect = pygame.Rect(x, y, w, h).clip(screen.get_rect())
                    if _safe_rect.width > 0 and _safe_rect.height > 0:
                        self.dvd_paused_frame = screen.subsurface(_safe_rect).copy()
                    if all(k in _dvd_blit_geom for k in ("dest_x", "dest_y", "draw_w", "draw_h")):
                        self._dvd_last_fit_geom = (
                            surf.get_width(), surf.get_height(),
                            _dvd_blit_geom["dest_x"], _dvd_blit_geom["dest_y"],
                            _dvd_blit_geom["draw_w"], _dvd_blit_geom["draw_h"])
                    else:
                        self._dvd_last_fit_geom = (surf.get_width(), surf.get_height(),
                                                    *_contain_fit_rect(surf.get_width(), surf.get_height(), (x, y, w, h)))
                    frame_drawn = True
            except Exception as e:
                _log_warn_throttled("dvd_frame_blit", "DVD frame grab/blit failed this frame: %s", e)
        if not frame_drawn and self.dvd_paused_frame is not None:
            screen.blit(self.dvd_paused_frame, (x, y))
            frame_drawn = True
        if not frame_drawn:
            screen.fill((0, 0, 0), rect)

        if (frame_drawn and self._dvd_menu_is_up() and vlc is not None
                and self._dvd_highlight_shadow is not None
                and self._dvd_highlight_shadow.is_open):
            try:
                _fit_geom = getattr(self, "_dvd_last_fit_geom", None) or (vlc.width, vlc.height, x, y, w, h)
                _hl_rect = self._dvd_highlight_shadow.get_highlight_rect_px(*_fit_geom)
                if _hl_rect:
                    pygame.draw.rect(screen, (255, 220, 0), _hl_rect, 3, border_radius=3)
            except Exception as _hl_draw_e:
                _log_warn_throttled("dvd_highlight_draw",
                                     "DVD highlight overlay draw failed: %s", _hl_draw_e)

        if not self.dvd_exit_confirm and not self.dvd_return_menu_confirm:
            _main_mod = sys.modules.get("__main__")
            _menu_open = bool(_main_mod and (
                _main_mod.app_state.get("show_menu", False) or
                _main_mod.app_state.get("show_splash", False) or
                _main_mod.app_state.get("show_quit_confirm", False)
            ))
            if not _menu_open:
                self._poll_dvd_hold()

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
            theme_bg_hue  = 220
            theme_ui_hue  = 140
            if main_module2 and hasattr(main_module2, "db"):
                theme_bg_hue  = main_module2.db.config.get("theme_bg_hue", 220)
                theme_ui_hue  = main_module2.db.config.get("theme_ui_hue", 140)

            box_w, box_h = 500, 200
            box_x = cx - box_w // 2
            box_y = y + h // 2 - box_h // 2

            r_bg, g_bg, b_bg = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.85, 0.18)
            r_ui, g_ui, b_ui = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
            rgb_bg     = (int(r_bg * 255), int(g_bg * 255), int(b_bg * 255))
            accent_col = (int(r_ui * 255), int(g_ui * 255), int(b_ui * 255))

            # WARNING: "Are You Sure" boxes always render fully opaque, regardless of the menu transparency slider.
            alpha_val = 255
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
            focus_thickness = SELECTION_BORDER_WIDTH
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
            theme_bg_hue  = 220
            theme_ui_hue  = 140
            if main_module2 and hasattr(main_module2, "db"):
                theme_bg_hue  = main_module2.db.config.get("theme_bg_hue", 220)
                theme_ui_hue  = main_module2.db.config.get("theme_ui_hue", 140)

            box_w, box_h = 500, 200
            box_x = cx - box_w // 2
            box_y = y + h // 2 - box_h // 2

            r_bg, g_bg, b_bg = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.85, 0.18)
            r_ui, g_ui, b_ui = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
            rgb_bg     = (int(r_bg * 255), int(g_bg * 255), int(b_bg * 255))
            accent_col = (int(r_ui * 255), int(g_ui * 255), int(b_ui * 255))

            # WARNING: "Are You Sure" boxes always render fully opaque, regardless of the menu transparency slider.
            alpha_val = 255
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
            focus_thickness = SELECTION_BORDER_WIDTH
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

    # ========================================================================
    # SECTION: Libretro: cores & XInput map
    # ========================================================================

    def _has_libretro_core(self, console):
        emu_dir = os.path.join(self.cores_dir, console)
        if not os.path.isdir(emu_dir):
            log.warning(f"[LIBRETRO DBG] _has_libretro_core({console}): core folder does not "
                  f"exist: {emu_dir}")
            return None
        _all = os.listdir(emu_dir)
        for fname in _all:
            fl = fname.lower()
            if fl.endswith("_libretro.dll") or fl.endswith("libretro.dll"):
                return os.path.join(emu_dir, fname)
        for fname in _all:
            if fname.lower().endswith(".dll"):
                return os.path.join(emu_dir, fname)
        log.warning(f"[LIBRETRO DBG] _has_libretro_core({console}): folder exists ({emu_dir}) "
              f"but contains no .dll -- found: {_all if _all else '(empty folder)'}")
        return None

    def _apply_menu_nav_mapping_from_libretro_map(self):
        apply_menu_nav_mapping(self._libretro_xinput_map.get("MENU", {}).get(1, {}))

    def _ensure_xi_map(self, console, player=1):
        per_console = self._libretro_xinput_map.get(console)
        if per_console is None or player not in per_console:
            self._build_default_libretro_xinput_map(console, player)
        return self._libretro_xinput_map[console][player]

    def _build_default_libretro_xinput_map(self, console, player=1):
        per_console = self._libretro_xinput_map.setdefault(console, {})
        console_map = per_console.setdefault(player, {})
        saved = self._load_libretro_xinput_map(console, player)

        if console == "MENU":
            if not saved and player != 1:
                if 1 not in per_console:
                    self._build_default_libretro_xinput_map("MENU", 1)
                console_map.update(per_console[1])
                return
            for btn_name, xi_bit in saved.items():
                console_map.setdefault(btn_name, xi_bit)
            cdef = _get_console_def("MENU")
            for xi_bit, btn_name in cdef.get("xinput_map", {}).items():
                if btn_name not in console_map:
                    console_map[btn_name] = xi_bit
            return

        if not saved and player != 1:
            if 1 not in per_console:
                self._build_default_libretro_xinput_map(console, 1)
            console_map.update(per_console[1])
            return

        # WARNING: personal per-console overrides load first and always win.
        for btn_name, xi_bit in saved.items():
            console_map.setdefault(btn_name, xi_bit)

        cdef = _get_console_def(console)
        static_xinput_map = cdef.get("xinput_map", {})
        static_by_name = {name: bit for bit, name in static_xinput_map.items()}
        for btn_name in cdef.get("buttons", []):
            if btn_name in console_map:
                continue
            xi_bit = static_by_name.get(btn_name)
            if xi_bit is not None:
                console_map[btn_name] = xi_bit

    @staticmethod
    def _xi_map_key(console, player=1):
        if player is None or player <= 1:
            return f"{console}_xinput_map"
        return f"{console}_xinput_map_p{player}"

    def _load_libretro_xinput_map(self, console, player=1):
        try:
            cfg_path = resolve_emulator_config_path()
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                saved = data.get(self._xi_map_key(console, player), {})
                out = {}
                for btn_name, xi_label in saved.items():
                    xi_bit = _XI_LABEL_TO_BIT.get(xi_label)
                    if xi_bit is not None:
                        out[btn_name] = xi_bit
                return out
        except Exception as e:
            log.warning(f"[GAMEDECK MAPPING] Gamepad-map load failed for {console} player {player} (using defaults): {e}")
        return {}

    def _save_libretro_xinput_map(self, console, player=None):
        if player is None:
            player = getattr(self, "_active_mapping_player", 1) or 1
        try:
            cfg_path = resolve_emulator_config_path()
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            console_map = self._libretro_xinput_map.get(console, {}).get(player, {})
            key = self._xi_map_key(console, player)
            data[key] = {
                btn_name: _XI_LABELS.get(xi_bit, "")
                for btn_name, xi_bit in console_map.items()
                if xi_bit in _XI_LABELS
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info(f"[GAMEDECK MAPPING] Saved {console} player {player} gamepad map: {data[key]}")
        except Exception as e:
            log.warning(f"[GAMEDECK MAPPING] Gamepad-map save failed for {console} player {player}: {e}")

    _MAX_ARCHIVE_ENTRIES = 20000
    # VALUES CHANGE HERE
    _MAX_EXTRACT_ENTRY_BYTES = 8 * 1024 ** 3
    _MAX_ARCHIVE_COMPRESSION_RATIO = 300

    # ========================================================================
    # SECTION: Libretro: ROM archive extraction
    # ========================================================================

    def _extract_rom_from_zip(self, console, archive_path):
        ext = os.path.splitext(archive_path)[1].lower()
        if ext not in (".zip", ".7z"):
            return archive_path

        real_exts = [e for e in ROM_EXTENSIONS.get(console, []) if e not in (".zip", ".7z")]
        cache_dir = os.path.join(self.base_dir, "main", "roms", console, ".extracted_cache")
        base_name = os.path.basename(archive_path)

        if not real_exts:
            return archive_path

        try:
            with open(archive_path, "rb") as _f:
                magic = _f.read(8)
        except Exception as _e:
            magic = b""
            log.warning(f"[LIBRETRO DBG] Could not read archive for sniffing: {_e}")

        try:
            if ext == ".zip":
                with zipfile.ZipFile(archive_path, "r") as zf:
                    all_entries = zf.infolist()
                    if len(all_entries) > self._MAX_ARCHIVE_ENTRIES:
                        log.warning(f"[LIBRETRO] Refusing to extract {base_name}: "
                              f"{len(all_entries)} entries exceeds the "
                              f"{self._MAX_ARCHIVE_ENTRIES}-entry safety cap")
                        return None
                    candidates = [
                        info for info in all_entries
                        if not info.is_dir()
                        and os.path.splitext(info.filename)[1].lower() in real_exts
                    ]
                    if not candidates:
                        log.warning(f"[LIBRETRO] No {console} ROM found inside zip: {base_name}")
                        return None
                    chosen = max(candidates, key=lambda info: info.file_size)

                    if chosen.file_size > self._MAX_EXTRACT_ENTRY_BYTES:
                        log.warning(f"[LIBRETRO] Refusing to extract {base_name}: "
                              f"declared size {chosen.file_size} bytes for "
                              f"{os.path.basename(chosen.filename)} exceeds the "
                              f"{self._MAX_EXTRACT_ENTRY_BYTES}-byte safety cap")
                        return None
                    _ratio = chosen.file_size / max(chosen.compress_size, 1)
                    if _ratio > self._MAX_ARCHIVE_COMPRESSION_RATIO:
                        log.warning(f"[LIBRETRO] Refusing to extract {base_name}: "
                              f"{os.path.basename(chosen.filename)} has a "
                              f"{_ratio:.0f}x compression ratio, exceeding the "
                              f"{self._MAX_ARCHIVE_COMPRESSION_RATIO}x safety cap "
                              f"(possible archive bomb)")
                        return None

                    os.makedirs(cache_dir, exist_ok=True)
                    zip_stem   = os.path.splitext(base_name)[0]
                    entry_base = os.path.basename(chosen.filename)
                    out_path   = os.path.join(cache_dir, f"{zip_stem}__{entry_base}")

                    if not (os.path.exists(out_path) and os.path.getsize(out_path) == chosen.file_size):
                        with zf.open(chosen, "r") as src, open(out_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        log.info(f"[LIBRETRO] Extracted {entry_base} from {base_name}")
                    return out_path

            else:
                if _7ZIP_EXE is None:
                    log.warning(f"[LIBRETRO] Cannot extract {base_name} — 7-Zip not found")
                    return None

                list_r = subprocess.run(
                    [_7ZIP_EXE, "l", "-slt", archive_path],
                    capture_output=True, text=True, timeout=30,
                )
                if list_r.returncode != 0:
                    log.warning(f"[LIBRETRO] 7z list failed for {base_name}: "
                          f"{list_r.stderr.strip()}")
                    return None

                names        = []
                sizes        = {}
                packed_sizes = {}
                _cur         = None
                for _line in list_r.stdout.splitlines():
                    if _line.startswith("Path = "):
                        _cur = _line[7:].strip()
                    elif _line.startswith("Size = ") and _cur is not None:
                        try:
                            sizes[_cur] = int(_line[7:].strip())
                        except ValueError:
                            sizes[_cur] = 0
                        names.append(_cur)
                    elif _line.startswith("Packed Size = ") and _cur is not None:
                        try:
                            packed_sizes[_cur] = int(_line[14:].strip())
                        except ValueError:
                            pass
                    elif _line == "" and _cur is not None:
                        _cur = None

                if len(names) > self._MAX_ARCHIVE_ENTRIES:
                    log.warning(f"[LIBRETRO] Refusing to extract {base_name}: "
                          f"{len(names)} entries exceeds the "
                          f"{self._MAX_ARCHIVE_ENTRIES}-entry safety cap")
                    return None

                candidates = [
                    n for n in names
                    if os.path.splitext(n)[1].lower() in real_exts
                ]
                if not candidates:
                    log.warning(f"[LIBRETRO] No {console} ROM found inside 7z: {base_name}")
                    return None

                chosen = max(candidates, key=lambda n: sizes.get(n, 0))

                _chosen_size = sizes.get(chosen, 0)
                if _chosen_size > self._MAX_EXTRACT_ENTRY_BYTES:
                    log.warning(f"[LIBRETRO] Refusing to extract {base_name}: "
                          f"declared size {_chosen_size} bytes for "
                          f"{os.path.basename(chosen)} exceeds the "
                          f"{self._MAX_EXTRACT_ENTRY_BYTES}-byte safety cap")
                    return None
                _chosen_packed = packed_sizes.get(chosen)
                if _chosen_packed is not None:
                    _ratio = _chosen_size / max(_chosen_packed, 1)
                    if _ratio > self._MAX_ARCHIVE_COMPRESSION_RATIO:
                        log.warning(f"[LIBRETRO] Refusing to extract {base_name}: "
                              f"{os.path.basename(chosen)} has a {_ratio:.0f}x "
                              f"compression ratio, exceeding the "
                              f"{self._MAX_ARCHIVE_COMPRESSION_RATIO}x safety cap "
                              f"(possible archive bomb)")
                        return None

                zip_stem   = os.path.splitext(base_name)[0]
                target_dir = os.path.join(cache_dir, f"{zip_stem}__7z")
                out_path   = os.path.join(target_dir, os.path.basename(chosen))

                cached_ok = (
                    os.path.exists(out_path)
                    and os.path.getsize(out_path) == sizes.get(chosen, -1)
                )
                if not cached_ok:
                    os.makedirs(target_dir, exist_ok=True)
                    ext_r = subprocess.run(
                        [_7ZIP_EXE, "e", archive_path,
                         "-o" + target_dir, chosen, "-y"],
                        capture_output=True, text=True, timeout=60,
                    )
                    if ext_r.returncode != 0:
                        log.warning(f"[LIBRETRO] 7z extraction failed for {base_name}: "
                              f"{ext_r.stderr.strip()}")
                        return None
                    log.info(f"[LIBRETRO] Extracted {os.path.basename(chosen)} "
                          f"from {base_name}")
                else:
                    pass

                if not os.path.exists(out_path):
                    log.warning(f"[LIBRETRO] 7z extraction reported success but output "
                          f"not found: {base_name}")
                    return None
                return out_path

        except Exception as e:
            log.warning(f"[LIBRETRO] {ext[1:].upper()} extraction failed for {archive_path}: {e}")
            if ext == ".zip" and magic[:6] == b"7z\xbc\xaf'\x1c":
                log.warning(f"[LIBRETRO DBG] *** FIX: '{base_name}' is a 7-ZIP archive with a .zip "
                      f"extension. Rename it to '{os.path.splitext(base_name)[0]}.7z' "
                      f"and retry — 7-Zip will handle it automatically. ***")
            elif ext == ".zip" and magic[:4] == b"Rar!":
                log.warning(f"[LIBRETRO DBG] *** '{base_name}' is a RAR archive — cannot extract. "
                      f"Use 7-Zip or WinRAR to manually extract the ROM file and place it "
                      f"directly in the roms/{console}/ folder. ***")
            return None

    # ========================================================================
    # SECTION: Libretro: launch & loading
    # ========================================================================

    def _launch_libretro(self, console, rom_path):
        self.emulator_loading  = True
        self.emulator_load_pct = 0
        self.emulator_load_msg = f"Loading {console}..."
        self.active_console    = console
        self.mode               = "GAME"
        # WARNING: clear the previous game's source path so a newly-starting ROM never looks like an active reader.
        self._libretro_rom_path = ""
        self.is_frozen          = False
        self._pause_reasons.clear()

        self._console_splash_is_video = False
        self._console_splash_video_last_frame = None
        if self._is_console_splash_enabled(console):
            _c_splash_path = self._get_console_splash_path(console)
            if _c_splash_path and self._dvd_path_is_video(_c_splash_path) and os.path.exists(_c_splash_path):
                try:
                    _handed = self.vlc_engine.play_file_segmented(_c_splash_path, 0, gain=1.0)
                    self._console_splash_is_video = bool(_handed)
                except Exception as e:
                    log.warning("Failed to start console splash video for %s: %s", console, e)
                    self._console_splash_is_video = False
        self._console_splash_start_time = pygame.time.get_ticks()

        _display_name = os.path.basename(rom_path)

        if (self._libretro_core is not None
                and self._prev_libretro_console
                and self._prev_libretro_console != console):
            if self._libretro_auto_save:
                self._libretro_core.save_state(slot=self._libretro_active_profile)
            self._libretro_core.unload()
            self._libretro_core = None

        _reuse_core = None
        if self._libretro_core is not None and self._libretro_core.is_loaded:
            if self._libretro_auto_save:
                self._libretro_core.save_state(slot=self._libretro_active_profile)
            self._libretro_core.unload_game()
            _reuse_core = self._libretro_core

        self._libretro_core       = None
        self._libretro_console    = console
        self._libretro_loading    = True
        self._libretro_load_start = pygame.time.get_ticks()
        pad_poller_ensure()
        self._libretro_100_reached_at = None
        self._libretro_load_tau_ms = _LIBRETRO_LOAD_TAU_MS.get(console, _LIBRETRO_LOAD_TAU_MS_DEFAULT)
        self._libretro_first_frame_giveup_ms = _LIBRETRO_FIRST_FRAME_GIVEUP_MS.get(
            console, _LIBRETRO_FIRST_FRAME_GIVEUP_MS_DEFAULT)
        log.info(f"[LIBRETRO] Launching {console}: {_display_name}")

        self._libretro_launch_request_id += 1
        req_id = self._libretro_launch_request_id
        with self._libretro_launch_lock:
            self._libretro_launch_result = None
        self._libretro_launch_pending = True
        self._libretro_launch_thread = threading.Thread(
            target=self._launch_libretro_worker,
            args=(req_id, console, rom_path, _display_name, _reuse_core),
            daemon=True,
        )
        self._libretro_launch_thread.start()

    def _launch_libretro_worker(self, req_id, console, rom_path, display_name, reuse_core):
        result = {"req_id": req_id, "console": console, "display_name": display_name}
        _owned_core = None
        _border_job = self._start_border_prep(console)
        try:
            extracted = self._extract_rom_from_zip(console, rom_path)
            if extracted is None:
                result["error"] = f"Could not open: {display_name} (bad or unsupported archive)"
                self._publish_libretro_launch_result(result)
                return

            if reuse_core is not None:
                core = reuse_core
            else:
                core = build_core_for_console(console, self.base_dir, self.emulator_config.get(console),
                                               self.active_bios.get(console))
                if core is None:
                    result["error"] = f"Could not load {console} core"
                    self._publish_libretro_launch_result(result)
                    return
                _owned_core = core

            if hasattr(core, "set_variable"):
                core.set_variable("mgba_sgb_borders", "OFF")

            if not _gd_is_drive_responsive(extracted):
                result["error"] = f"Drive not responding: {display_name}"
                self._publish_libretro_launch_result(result)
                return

            if not core.load_rom(extracted):
                result["error"] = f"Failed to load: {display_name}"
                self._publish_libretro_launch_result(result)
                return

            result["core"]     = core
            result["rom_path"] = extracted
            if _border_job is not None:
                _border_job[0].join()
                result["border"] = _border_job[1].get("prepared")
            _owned_core = None
        except Exception as e:
            log.warning("libretro launch worker failed for %s: %s", console, e)
            result["error"] = f"Failed to load: {display_name}"
        finally:
            if _owned_core is not None:
                try:
                    _owned_core.unload()
                except Exception as _ue:
                    log.warning("_launch_libretro_worker: cleanup unload failed: %s", _ue)
        self._publish_libretro_launch_result(result)

    def _publish_libretro_launch_result(self, result):
        with self._libretro_launch_lock:
            self._libretro_launch_result = result

    def _reap_abandoned_libretro_launch(self, abandoned_req_id):
        """Reap a core.load_rom() result that arrives after its loading screen already gave up; that background call cannot be cancelled."""
        # VALUES CHANGE HERE
        deadline = time.time() + 600
        while time.time() < deadline:
            with self._libretro_launch_lock:
                result = self._libretro_launch_result
            if result is not None and result.get("req_id") == abandoned_req_id:
                core = result.get("core")
                if core is not None:
                    try: core.unload()
                    except Exception as e:
                        log.warning("_reap_abandoned_libretro_launch: unload failed: %s", e)
                    log.warning(f"[LIBRETRO] Reaped a late-arriving core load from a "
                          f"timed-out launch (req_id={abandoned_req_id}) that "
                          f"finished after all -- unloaded it.")
                return
            time.sleep(1.0)

    def _poll_libretro_launch(self):
        if not self._libretro_launch_pending:
            return False

        with self._libretro_launch_lock:
            result = self._libretro_launch_result

        if result is None:
            return True

        self._libretro_launch_pending = False

        if result["req_id"] != self._libretro_launch_request_id:
            _stale_core = result.get("core")
            if _stale_core is not None:
                try: _stale_core.unload()
                except Exception: pass
            return False

        if "error" in result:
            log.warning(f"[LIBRETRO] {result['error']}")
            self.emulator_loading = False; self._libretro_loading = False
            self._libretro_rom_path = ""
            self.mode = "ROMLIST"
            self._romlist_error_msg   = result["error"]
            self._romlist_error_until = pygame.time.get_ticks() + 4000
            return False

        console = result["console"]
        core    = result["core"]
        self._libretro_rom_path = result["rom_path"]
        self._libretro_core     = core

        try:
            import sys as _sys
            _main = _sys.modules.get("__main__")
            if _main and hasattr(_main, "db") and _main.db:
                self.sync_profile_from_db(console, _main.db.config)
                core.set_volume(_main.db.config.get("global_volume", 70))
                core.set_muted(True)
        except Exception as e:
            log.warning("Failed to sync profile/volume settings before libretro launch: %s", e)

        try:
            _ensure_player_slots_assigned()
            _bound_players = sum(1 for _p in (1, 2, 3, 4) if player_is_connected(_p))
            _profile_forced = self._libretro_multitap_enabled
            self._libretro_multitap_auto_engaged = False
            self._libretro_multitap_pending = False
            if _profile_forced is True and core.multitap_available():
                if core.enable_multitap():
                    log.info(f"[LIBRETRO MULTIPLAYER] Multitap enabled for {console} "
                          f"(profile setting)")
            elif _profile_forced is not False and _bound_players >= 3 and core.multitap_available():
                self._libretro_multitap_pending = True
                log.info(f"[LIBRETRO MULTIPLAYER] Multitap pending for {console} "
                      f"({_bound_players} controllers bound, awaiting player 3/4 input)")
            _seed_connected = set()
            if _read_gamepad_state() is not None:
                _seed_connected.add(1)
            for _p in (2, 3, 4):
                if player_is_connected(_p):
                    _seed_connected.add(_p)
            self._libretro_prev_connected_players = _seed_connected
        except Exception as e:
            log.warning("Multitap auto-enable check failed for %s: %s", console, e)

        if border_available_for_console(console):
            self._install_prepared_border(console, result.get("border"))
        else:
            self._libretro_border_surf      = None
            self._libretro_border_rect_frac = None

        self._prev_libretro_console = console
        self._build_default_libretro_xinput_map(console)
        slot = self._libretro_active_profile
        if self._libretro_auto_load and core.has_state(slot=slot):
            core.load_state(slot=slot)
            log.info(f"[LIBRETRO] Auto-loaded save state slot {slot} (profile {slot})")
        return False

    def _libretro_tick_loading(self):
        """Runs one loading-splash tick; wraps the real work so the frame timing line covers the splash too."""
        GAME_FRAME_TIMER.tick_start()
        try:
            return self._libretro_tick_loading_impl()
        finally:
            GAME_FRAME_TIMER.tick_end("loading", self._libretro_console, 1, self._LIBRETRO_MAX_CATCHUP_FRAMES)

    def _libretro_tick_loading_impl(self):
        if not self._libretro_loading:
            return False

        pad_poller_ensure()
        now     = pygame.time.get_ticks()
        elapsed = now - self._libretro_load_start

        import math as _math
        def _asymptotic_pct(elapsed_ms, cap):
            tau = max(1, self._libretro_load_tau_ms)
            return cap * (1.0 - _math.exp(-elapsed_ms / tau))

        if self._libretro_launch_pending:
            self.emulator_load_pct = int(_asymptotic_pct(elapsed, 90))

            if elapsed >= self._libretro_launch_pending_timeout_ms:
                log.warning(f"[LIBRETRO] Launch of {self._libretro_console} timed out after "
                      f"{elapsed}ms waiting on core.load_rom() -- likely a stalled "
                      f"drive; abandoning the launch.")
                _abandoned_req_id = self._libretro_launch_request_id
                self._libretro_launch_request_id += 1
                self._libretro_launch_pending = False
                self.emulator_loading  = False
                self._libretro_loading = False
                self._libretro_rom_path = ""
                self.mode = "ROMLIST"
                self._romlist_error_msg   = f"{self._libretro_console}: drive not responding, load timed out"
                self._romlist_error_until = pygame.time.get_ticks() + 4000
                threading.Thread(target=self._reap_abandoned_libretro_launch,
                                  args=(_abandoned_req_id,), daemon=True,
                                  name="libretro-launch-reaper").start()
                return False

            if self._poll_libretro_launch():
                return True
            if not self._libretro_loading:
                return False

        core = self._libretro_core
        if core and core.is_loaded:
            try: core.run_frame()
            except Exception as e:
                _log_warn_throttled("libretro_tick_run_frame", "core.run_frame() during loading-splash tick failed: %s", e)
        first_frame = core is not None and core.has_visible_frame
        min_done    = elapsed >= self._libretro_load_min_ms
        timed_out   = elapsed >= self._libretro_first_frame_giveup_ms

        _pad_ok = pad_warmup_done(elapsed)
        if (first_frame or timed_out) and _pad_ok:
            self.emulator_load_pct = 100
        else:
            self.emulator_load_pct = max(self.emulator_load_pct, min(99, int(_asymptotic_pct(elapsed, 99))))

        splash_video_done = True
        if self._console_splash_is_video:
            splash_elapsed = now - getattr(self, "_console_splash_start_time", 0)
            ended = False
            try:
                import vlc as _raw_vlc
                if self.vlc_engine.player.get_state() in (_raw_vlc.State.Ended, _raw_vlc.State.Error):
                    ended = True
            except Exception as e:
                _log_warn_throttled("console_splash_video_state", "Console splash video state check failed: %s", e)
            if splash_elapsed >= self.DVD_SPLASH_VIDEO_MAX_MS:
                ended = True
            splash_video_done = ended

        _ready = (first_frame or timed_out) and min_done and splash_video_done and _pad_ok
        if _ready:
            self.emulator_load_pct = 100
            if self._libretro_100_reached_at is None:
                self._libretro_100_reached_at = now
        else:
            self._libretro_100_reached_at = None

        if _ready and (now - self._libretro_100_reached_at) >= _LIBRETRO_HOLD_AT_100_MS:
            self._libretro_loading = False
            self.emulator_loading  = False
            try:
                import sys as _sys
                _main = _sys.modules.get("__main__")
                if _main and hasattr(_main, "db") and _main.db:
                    _muted = _main.db.config.get("is_muted", False)
                    core.set_muted(_muted)
            except Exception as e:
                log.warning("Failed to restore mute state after libretro splash: %s", e)
            log.info(f"[LIBRETRO] Splash done after {elapsed}ms")
            return False
        return True

    # ========================================================================
    # SECTION: Libretro: input & disconnect handling
    # ========================================================================

    def _poll_libretro_input(self):
        core = self._libretro_core
        if core is None or not core.is_loaded or core.is_frozen:
            return
        if getattr(self, "_libretro_loading", False):
            core._input.clear()
            return

        _ensure_player_slots_assigned()

        gp1 = player1_gpstate()
        player_gps = {}
        if gp1 is not None:
            player_gps[1] = gp1
        for _p in (2, 3, 4):
            _g = player_gpstate(_p)
            if _g is not None:
                player_gps[_p] = _g

        if not player_gps:
            core._input.clear()
            return

        self._resolve_pending_multitap(player_gps)

        if gp1 is not None:
            btns = gp1.wButtons

            _SS_STICK_ACTIVITY_DZ = 6000
            if (btns or gp1.bLT or gp1.bRT
                    or abs(gp1.sLX) > _SS_STICK_ACTIVITY_DZ or abs(gp1.sLY) > _SS_STICK_ACTIVITY_DZ
                    or abs(gp1.sRX) > _SS_STICK_ACTIVITY_DZ or abs(gp1.sRY) > _SS_STICK_ACTIVITY_DZ):
                self.last_gameplay_input_ticks = pygame.time.get_ticks()

            _XI_LB   = 0x0100
            _XI_RB   = 0x0200
            _XI_BACK = 0x0020
            _TRIG_THRESH = 128
            _COOLDOWN_MS = 2000

            if getattr(self, "_libretro_suppress_start_select", False):
                if btns & (_XI_START | _XI_BACK):
                    btns &= ~(_XI_START | _XI_BACK)
                else:
                    self._libretro_suppress_start_select = False

            now = pygame.time.get_ticks()
            if now >= self._libretro_hotkey_cooldown:
                if (btns & _XI_LB) and (btns & _XI_RB) and (btns & _XI_BACK):
                    ok = core.save_state(slot=self._libretro_active_profile)
                    self._libretro_osd_msg   = f"STATE SAVED (P{self._libretro_active_profile})" if ok else "SAVE FAILED"
                    self._libretro_osd_until = now + 2000
                    self._libretro_hotkey_cooldown = now + _COOLDOWN_MS
                    log.info(f"[LIBRETRO] Save hotkey: {self._libretro_osd_msg}")
                elif (gp1.bLT >= _TRIG_THRESH and gp1.bRT >= _TRIG_THRESH
                      and (btns & _XI_BACK)):
                    if core.has_state(slot=self._libretro_active_profile):
                        ok = core.load_state(slot=self._libretro_active_profile)
                        self._libretro_osd_msg = f"STATE LOADED (P{self._libretro_active_profile})" if ok else "LOAD FAILED"
                    else:
                        self._libretro_osd_msg = "NO SAVE FOUND"
                    self._libretro_osd_until = now + 2000
                    self._libretro_hotkey_cooldown = now + _COOLDOWN_MS
                    log.info(f"[LIBRETRO] Load hotkey: {self._libretro_osd_msg}")

            gp1.wButtons = btns

        _active_console_for_input = self.active_console or self._libretro_console

        for player_num, gp in player_gps.items():
            port = _libretro_port_for_player(player_num)
            console_xinput_map = self._ensure_xi_map(_active_console_for_input, player_num)
            xi_to_names = {}
            for btn_name, xi_bit in console_xinput_map.items():
                xi_to_names.setdefault(xi_bit, []).append(btn_name)

            btns = gp.wButtons
            libretro_state = {}
            for xi_bit, btn_names in xi_to_names.items():
                pressed = bool(btns & xi_bit)
                for btn_name in btn_names:
                    lr_id = lr_id_for(_active_console_for_input, btn_name)
                    if lr_id is not None:
                        libretro_state[lr_id] = libretro_state.get(lr_id, False) or pressed

            player_ctrl_mapping = self._gameplay_ctrl_mapping_for_player(_active_console_for_input, player_num)
            if _active_console_for_input != "N64" and player_ctrl_mapping.get("stick_as_dpad", False):
                DZ = stick_axis_threshold_raw()
                if not hasattr(self, "_gameplay_dpad_latch"):
                    self._gameplay_dpad_latch = {}
                _latch = self._gameplay_dpad_latch.setdefault(player_num, {})
                up, down, left, right = _stick_to_dpad_hysteresis(gp.sLX, gp.sLY, DZ, _latch)
                if up:    libretro_state[RETRO_DEVICE_ID_JOYPAD_UP]    = True
                if down:  libretro_state[RETRO_DEVICE_ID_JOYPAD_DOWN]  = True
                if left:  libretro_state[RETRO_DEVICE_ID_JOYPAD_LEFT]  = True
                if right: libretro_state[RETRO_DEVICE_ID_JOYPAD_RIGHT] = True

            core._input.set_buttons(port, libretro_state)

            if _active_console_for_input == "N64":
                DZ = stick_axis_threshold_raw()
                if abs(gp.sRX) > DZ or abs(gp.sRY) > DZ:
                    stick_x, stick_y = gp.sRX, gp.sRY
                else:
                    stick_x, stick_y = gp.sLX, gp.sLY
                core._input.set_analog(port, RETRO_DEVICE_INDEX_ANALOG_LEFT, RETRO_DEVICE_ID_ANALOG_X, stick_x)
                core._input.set_analog(port, RETRO_DEVICE_INDEX_ANALOG_LEFT, RETRO_DEVICE_ID_ANALOG_Y, -stick_y)

        for player_num in (1, 2, 3, 4):
            if player_num not in player_gps:
                core._input.clear(_libretro_port_for_player(player_num))

    def _poll_disconnect_pause(self):
        core = self._libretro_core
        if core is None or not core.is_loaded:
            return

        _ensure_player_slots_assigned()
        connected = set()
        if _read_gamepad_state() is not None:
            connected.add(1)
        for _p in (2, 3, 4):
            if player_is_connected(_p):
                connected.add(_p)

        prev = self._libretro_prev_connected_players
        newly_gone = prev - connected
        self._libretro_prev_connected_players = connected
        self._reconcile_multitap_live(len(prev), len(connected))

        if self._libretro_disconnect_pause:
            if self._libretro_disconnect_player in connected:
                log.info(f"[LIBRETRO MULTIPLAYER] Player {self._libretro_disconnect_player}'s "
                      f"controller reconnected -- resuming.")
                self._libretro_disconnect_pause  = False
                self._libretro_disconnect_player = None
                self.set_paused(False, reason="disconnect")
            return

        if newly_gone:
            player_num = min(newly_gone)
            self._libretro_disconnect_pause  = True
            self._libretro_disconnect_player = player_num
            log.info(f"[LIBRETRO MULTIPLAYER] Player {player_num}'s controller was "
                  f"unplugged -- auto-pausing.")
            self.set_paused(True, reason="disconnect")

    # ========================================================================
    # SECTION: Libretro: rendering
    # ========================================================================

    def render_libretro(self, screen, dest_rect, crt_cfg=None, overlay_open=False, pixel_cfg=None):
        """Draws one game frame; wraps the real work so the frame timing line can account for the whole call."""
        GAME_FRAME_TIMER.tick_start()
        self._gft_runs = 0
        try:
            return self._render_libretro_impl(screen, dest_rect, crt_cfg=crt_cfg,
                                              overlay_open=overlay_open, pixel_cfg=pixel_cfg)
        finally:
            pad_snapshot_end()
            GAME_FRAME_TIMER.tick_end("play" if self._gft_runs > 0 else "idle", self._libretro_console,
                                      self._gft_runs, self._LIBRETRO_MAX_CATCHUP_FRAMES)

    def _render_libretro_impl(self, screen, dest_rect, crt_cfg=None, overlay_open=False, pixel_cfg=None):
        core = self._libretro_core
        if core is None or not core.is_loaded:
            return False

        pad_snapshot_begin()
        self._poll_disconnect_pause()
        self.set_paused(overlay_open, reason="overlay")

        factor = SCREEN_SIZE_FACTORS.get(self._libretro_screen_size, 1.0)
        if factor < 1.0:
            dx, dy, dw, dh = dest_rect
            new_w = int(dw * factor)
            new_h = int(dh * factor)
            dest_rect = (dx + (dw - new_w) // 2, dy + (dh - new_h) // 2, new_w, new_h)

        if overlay_open:
            if self.game_exit_confirm:
                self.game_exit_confirm = False
                self.set_paused(False, reason="exit_confirm")
            self._poll_game_exit_toggle(sync_only=True)
            self._prev_overlay_open = True
            surf = core.reprocess_current_frame(crt_cfg=crt_cfg, pixel_cfg=pixel_cfg)
            if surf: self._blit_libretro_frame(screen, surf, dest_rect)
            # WARNING: an open menu or dialog must not accrue catch-up debt.
            self._libretro_last_run_wall = None
            return True

        if self._prev_overlay_open:
            self._prev_overlay_open = False
            self._poll_game_exit_toggle(sync_only=True)
        elif self._poll_game_exit_toggle():
            if self.game_exit_confirm:
                self._close_game_exit_confirm()
            else:
                self._open_game_exit_confirm()

        if self.game_exit_confirm:
            if pygame.time.get_ticks() - self.game_exit_confirm_shown_at >= self._CONFIRM_BURN_IN_MS:
                log.info("[LIBRETRO] Exit-confirm popup timed out (burn-in protection) -- dismissing.")
                self._close_game_exit_confirm()
            else:
                surf = core.reprocess_current_frame(crt_cfg=crt_cfg, pixel_cfg=pixel_cfg)
                if surf: self._blit_libretro_frame(screen, surf, dest_rect)
                self._poll_game_exit_confirm_input()
                _popup_rect = self.get_active_console_bezel_inner_rect(dest_rect) or dest_rect
                self._render_game_exit_confirm(screen, _popup_rect)
                self._libretro_last_run_wall = None
                return True

        if self._libretro_disconnect_pause:
            surf = core.reprocess_current_frame(crt_cfg=crt_cfg, pixel_cfg=pixel_cfg)
            if surf: self._blit_libretro_frame(screen, surf, dest_rect)
            _libretro_draw_disconnect_banner(screen, dest_rect, self._libretro_disconnect_player)
            self._libretro_last_run_wall = None
            return True

        if core.is_frozen:
            surf = core.reprocess_current_frame(crt_cfg=crt_cfg, pixel_cfg=pixel_cfg)
            if surf: self._blit_libretro_frame(screen, surf, dest_rect)
            self._libretro_last_run_wall = None
            return True
        _gft_t0 = time.perf_counter()
        self._poll_libretro_input()
        GAME_FRAME_TIMER.add("input", (time.perf_counter() - _gft_t0) * 1000.0)

        # WARNING: run catch-up steps so a slow picture never slows the game itself (animation, physics, audio pitch).
        _now_wall = time.perf_counter()
        try:
            _core_fps = float(core.fps)
            if not (40.0 <= _core_fps <= 120.0):
                _core_fps = 60.0
        except Exception:
            _core_fps = 60.0
        _frame_dur = 1.0 / _core_fps
        if self._libretro_last_run_wall is None:
            # WARNING: the first run after a load or a paused/frozen/overlay stretch is exactly one step, never a catch-up burst.
            _n_runs = 1
            self._libretro_run_accum_s = 0.0
        else:
            self._libretro_run_accum_s += _now_wall - self._libretro_last_run_wall
            _n_runs = int(self._libretro_run_accum_s // _frame_dur)
            if _n_runs < 1:
                _n_runs = 1
            elif _n_runs > self._LIBRETRO_MAX_CATCHUP_FRAMES:
                _n_runs = self._LIBRETRO_MAX_CATCHUP_FRAMES
                self._libretro_run_accum_s = 0.0
            else:
                self._libretro_run_accum_s -= _n_runs * _frame_dur
        self._libretro_last_run_wall = _now_wall
        self._gft_runs = _n_runs

        surf = None
        for _ in range(_n_runs):
            surf = core.run_frame(crt_cfg=crt_cfg, pixel_cfg=pixel_cfg)
        if surf:
            self._blit_libretro_frame(screen, surf, dest_rect)
        now = pygame.time.get_ticks()
        if self._libretro_osd_msg and now < self._libretro_osd_until:
            _gft_t0 = time.perf_counter()
            _libretro_draw_osd(screen, dest_rect, self._libretro_osd_msg,
                               self._libretro_osd_until - now, 2000)
            GAME_FRAME_TIMER.add("osd", (time.perf_counter() - _gft_t0) * 1000.0)
        elif now >= self._libretro_osd_until:
            self._libretro_osd_msg = ""
        return True

    # ========================================================================
    # SECTION: Game exit confirm
    # ========================================================================

    def _poll_game_exit_toggle(self, sync_only=False):
        esc_held = _nav.BACK in _nav.pressed_actions(pygame.key.get_pressed())
        ss_held  = False
        try:
            gp = _read_gamepad_state()
            if gp is not None:
                _con = self.active_console or self._libretro_console or "GBA"
                _map = self._ensure_xi_map(_con, 1)
                _start_bit = _map.get("Start", _XI_START)
                _select_bit = _map.get("Select", _XI_BACK)
                ss_held = bool(gp.wButtons & _start_bit) and bool(gp.wButtons & _select_bit)
        except Exception as e:
            _log_warn_throttled("game_exit_toggle_xinput", "Per-frame controller read for exit-toggle failed: %s", e)

        held = esc_held or ss_held
        prev = getattr(self, "_game_exit_toggle_held", False)
        self._game_exit_toggle_held = held
        # WARNING: the Escape KEYDOWN event is the reliable signal; polling get_pressed() alone misses a tap that goes down and up between two frames.
        _ev_at = getattr(self, "_exit_key_event_at", -1)
        self._exit_key_event_at = -1
        if sync_only:
            return False
        _event_press = _ev_at >= 0 and pygame.time.get_ticks() - _ev_at <= 250
        _fired = (held or _event_press) and not prev
        if _fired:
            log.info("[GAME EXIT] toggle fired: esc_polled=%s start+select=%s esc_event=%s popup_open=%s",
                     esc_held, ss_held, _event_press, self.game_exit_confirm)
        return _fired

    def _open_game_exit_confirm(self):
        if self.game_exit_confirm:
            return
        log.info("[LIBRETRO] Exit requested — opening confirm prompt.")
        self.game_exit_confirm   = True
        self.game_exit_selection = "No"
        self.game_exit_confirm_shown_at = pygame.time.get_ticks()
        self._game_exit_prev_buttons = frozenset()
        self.set_paused(True, reason="exit_confirm")

    def _close_game_exit_confirm(self):
        """Cancel out of the exit-confirm prompt and resume the game."""
        self.game_exit_confirm = False
        self._libretro_suppress_start_select = True
        self.set_paused(False, reason="exit_confirm", muted=False)

    def _poll_game_exit_confirm_input(self):
        _down = _nav.pressed_actions(pygame.key.get_pressed(), arrows=True)
        left  = _nav.LEFT in _down
        right = _nav.RIGHT in _down
        a_btn = _nav.ACCEPT in _down
        b_btn = False

        try:
            gp = _read_gamepad_state()
            if gp is not None:
                btns = gp.wButtons
                DZ   = stick_axis_threshold_raw()
                left  = left  or bool(btns & _XI_LEFT)  or gp.sLX < -DZ
                right = right or bool(btns & _XI_RIGHT) or gp.sLX >  DZ
                a_btn = a_btn or bool(btns & _XI_A)
                b_btn = bool(btns & _XI_B)
        except Exception as e:
            _log_warn_throttled("game_exit_confirm_xinput", "Per-frame controller read for exit-confirm input failed: %s", e)

        cur  = frozenset(action for action, on in ((_nav.LEFT, left), (_nav.RIGHT, right),
                                                   (_nav.ACCEPT, a_btn), (_nav.BACK, b_btn)) if on)
        prev = self._game_exit_prev_buttons
        just = cur - prev
        self._game_exit_prev_buttons = cur

        for _direction in (_nav.LEFT, _nav.RIGHT):
            if _direction in just:
                self.game_exit_selection = _nav.deck_confirm_choice(_direction)
                self.game_exit_confirm_shown_at = pygame.time.get_ticks()
        if _nav.ACCEPT in just:
            if self.game_exit_selection == "Yes":
                self.game_exit_confirm = False
                self._libretro_quit_to_romlist(self._libretro_core)
            else:
                self._close_game_exit_confirm()
        if _nav.BACK in just:
            self._close_game_exit_confirm()

    def _render_game_exit_confirm(self, screen, dest_rect):
        import colorsys
        x, y, w, h = dest_rect
        cx = x + w // 2

        theme_bg_hue  = 220
        theme_ui_hue  = 140
        main_module2  = sys.modules.get("__main__")
        if main_module2 and hasattr(main_module2, "db"):
            theme_bg_hue = main_module2.db.config.get("theme_bg_hue", 220)
            theme_ui_hue = main_module2.db.config.get("theme_ui_hue", 140)

        box_w, box_h = 500, 200
        box_x = cx - box_w // 2
        box_y = y + h // 2 - box_h // 2

        r_bg, g_bg, b_bg = colorsys.hsv_to_rgb(theme_bg_hue / 360.0, 0.85, 0.18)
        r_ui, g_ui, b_ui = colorsys.hsv_to_rgb(theme_ui_hue / 360.0, 0.90, 1.00)
        rgb_bg     = (int(r_bg * 255), int(g_bg * 255), int(b_bg * 255))
        accent_col = (int(r_ui * 255), int(g_ui * 255), int(b_ui * 255))

        # WARNING: "Are You Sure" boxes always render fully opaque, regardless of the menu transparency slider.
        alpha_val = 255
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
        focus_thickness = SELECTION_BORDER_WIDTH
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

    # ========================================================================
    # SECTION: Libretro: quit & volume
    # ========================================================================

    def _libretro_quit_to_romlist(self, core):
        log.info("[LIBRETRO] Start+Select: quitting to ROM list")
        pad_poller_stop()
        if self._libretro_auto_save:
            core.save_state(slot=self._libretro_active_profile)
        try:
            core.unload()
        except Exception as e:
            log.warning("core.unload() failed while quitting to ROM list: %s", e)
        self._libretro_core    = None
        self._libretro_rom_path = ""
        self._libretro_loading = False
        self.emulator_loading  = False
        self.mode              = "ROMLIST"
        self._pause_reasons.clear()
        import pygame as _pg
        _pg.event.clear(_pg.KEYDOWN)
        if self.active_console:
            self._scan_roms(self.active_console)
        self.rom_index = 0; self.rom_scroll = 0

    def return_active_rom_to_list_for_drive_failure(self):
        """Stop a loaded libretro ROM whose source drive has failed."""
        if (self.mode != "GAME"
                or getattr(self, "emulator_loading", False)
                or not self._libretro_rom_path):
            return False
        core = self._libretro_core
        if core is None or not getattr(core, "is_loaded", False):
            return False
        rom_name = os.path.basename(self._libretro_rom_path) or "this ROM"
        try:
            self._libretro_quit_to_romlist(core)
        except Exception as e:
            log.warning("[LIBRETRO] Failed returning to ROM list after drive failure: %s", e)
            return False
        self._romlist_error_msg = (
            f"Drive not responding — can't continue {rom_name}"
        )
        self._romlist_error_until = pygame.time.get_ticks() + 4000
        return True

    def set_libretro_volume(self, volume_pct, muted=False):
        core = self._libretro_core
        if core:
            core.set_volume(volume_pct)
            core.set_muted(muted)


# ============================================================================
# SECTION: Libretro frame drawing helpers
# ============================================================================


def _contain_fit_rect(sw, sh, rect):
    """Compute the centered, aspect-preserving destination rect (dx, dy, dw, dh) for scaling an `sw` x `sh` source into `rect`; never crops or stretches."""
    x, y, w, h = rect
    if sw <= 0 or sh <= 0 or w <= 0 or h <= 0:
        return x, y, 0, 0
    scale = min(w / sw, h / sh)
    dw = max(1, round(sw * scale))
    dh = max(1, round(sh * scale))
    dx = x + (w - dw) // 2
    dy = y + (h - dh) // 2
    return dx, dy, dw, dh


def _aspect_fit_blit(dest_surface, src_surface, rect, bar_color=(0, 0, 0)):
    """Scale `src_surface` to fit inside `rect` preserving its aspect ratio, centered, with leftover space filled by `bar_color`."""
    x, y, w, h = rect
    sw, sh = src_surface.get_size()
    dx, dy, dw, dh = _contain_fit_rect(sw, sh, rect)
    if dw <= 0 or dh <= 0:
        return
    if dw < w or dh < h:
        dest_surface.fill(bar_color, rect)
    dest_surface.blit(pygame.transform.smoothscale(src_surface, (dw, dh)), (dx, dy))


def _active_game_screen_mode():
    """Read the Launched Games "game_screen_mode" setting through the __main__.db lookup."""
    # WARNING: this setting stays independent of the Video tab's screen_mode and DVD's dvd_screen_mode; game_deck.py cannot import retro_tv_emulator.py.
    try:
        import sys as _sys
        _main = _sys.modules.get("__main__")
        if _main and hasattr(_main, "db") and _main.db:
            mode = _main.db.config.get("game_screen_mode", "normal")
            return mode if mode == "stretch" else "normal"
    except Exception:
        pass
    return "normal"


def _dvd_captions_locked():
    """True while the current performance mode locks the DVD captions setting off."""
    try:
        import sys as _sys
        _fn = getattr(_sys.modules.get("__main__"), "_feature_locked", None)
        return bool(_fn and _fn("dvd_captions_enabled"))
    except Exception:
        return False


def _libretro_blit_scaled(screen, surf, dest_rect, mode=None):
    """Scale `surf` into `dest_rect` per the Launched Games Screen Mode: "normal" contain-fits, "stretch" fills dest_rect exactly."""
    if mode is None:
        mode = _active_game_screen_mode()
    sw, sh = surf.get_size()
    dw, dh = dest_rect[2], dest_rect[3]
    if mode == "stretch":
        draw_w, draw_h = dw, dh
    else:
        scale  = min(dw / max(sw, 1), dh / max(sh, 1))
        draw_w = int(sw * scale); draw_h = int(sh * scale)
    ox = dest_rect[0] + (dw - draw_w) // 2
    oy = dest_rect[1] + (dh - draw_h) // 2
    try:
        screen.blit(pygame.transform.scale(surf, (draw_w, draw_h)), (ox, oy))
    except Exception as e:
        _log_warn_throttled("libretro_blit_scaled", "Libretro frame blit failed this frame: %s", e)


def _libretro_draw_osd(screen, dest_rect, msg, ms_remaining, ms_total):
    try:
        FADE_MS  = 500
        alpha    = 255 if ms_remaining > FADE_MS else int(255 * ms_remaining / FADE_MS)
        alpha    = max(0, min(255, alpha))
        font_size = max(14, dest_rect[3] // 20)
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


def _libretro_draw_disconnect_banner(screen, dest_rect, player_num):
    try:
        label = f"PLAYER {player_num}'S CONTROLLER DISCONNECTED — RECONNECT TO RESUME"
        font_size = max(16, dest_rect[3] // 22)
        font      = _cached_font("Arial", font_size, bold=True)
        text_surf = font.render(label, True, (255, 255, 255))
        tw, th    = text_surf.get_size()
        pad_x, pad_y = 20, 12
        bar_w = min(tw + pad_x * 2, dest_rect[2] - 20)
        bar_h = th + pad_y * 2
        bar_x = dest_rect[0] + (dest_rect[2] - bar_w) // 2
        bar_y = dest_rect[1] + (dest_rect[3] - bar_h) // 2
        bar = pygame.Surface((bar_w, bar_h), pygame.SRCALPHA)
        bar.fill((120, 20, 20, 225))
        pygame.draw.rect(bar, (255, 90, 90, 255), bar.get_rect(), width=3)
        bar.blit(text_surf, (pad_x, pad_y))
        screen.blit(bar, (bar_x, bar_y))
    except Exception as e:
        _log_warn_throttled("libretro_draw_disconnect_banner",
                             "Disconnect-pause banner draw failed this frame: %s", e)


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "ARCHIVE_EXTS",
    "BIOS_REQUIRED_CONSOLES",
    "BORDER_CONSOLES",
    "BORDER_CONSOLES_HANDHELD",
    "BORDER_CONSOLES_HOME",
    "CONSOLE_BUTTON_DEFS",
    "CONSOLE_FOLDER_ALIASES",
    "CONSOLE_LABELS",
    "CONSOLE_MAX_PLAYERS",
    "CONSOLE_ORDER",
    "CONSOLE_SCROLL_STYLES",
    "CONSOLE_SCROLL_STYLE_LABELS",
    "CORE_NAME_TO_CONSOLES",
    "DEFAULT_GBA_MAPPING",
    "DVD_SPLASH_IMAGE_EXTS",
    "DVD_SPLASH_VIDEO_EXTS",
    "ExternalGameDeck",
    "GBA_BTN_HOTSPOTS",
    "HANDHELD_CONSOLES",
    "LIST_TEXT_COLOR_NAMES",
    "LIST_TEXT_COLOR_RGB",
    "MULTITAP_CONSOLES",
    "PREFERRED_CORE_ORDER",
    "ROM_EXTENSIONS",
    "SCREEN_SIZE_FACTORS",
    "SCREEN_SIZE_OPTIONS",
    "VK_FROM_NAME",
    "VK_NAMES",
    "XINPUT_TO_GBA_BTN",
    "apply_menu_nav_mapping",
    "border_available_for_console",
    "console_bezel_currently_active",
    "find_psx_disc_siblings",
    "gamepad_permission_denied_path",
    "get_bios_dir_for_console",
    "match_console_for_folder_name",
    "match_consoles_for_core_filename",
    "menu_nav_connected",
    "player1_gpstate",
    "player_gpstate",
    "player_is_connected",
    "pygame_hwnd_ref",
    "resolve_emulator_config_path",
    "resolve_preferred_core_filename",
]
