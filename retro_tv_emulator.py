# ==============================================================================
# PART 1a OF 28: CORE ENGINE SETUP
# ==============================================================================

import os
import sys
import array
import subprocess
import random
import datetime
import ctypes
import queue
import threading
import math
import time  # Added for boot timing diagnostics
import logging
import logging.handlers
import numpy as np

# --- CRASH LOG: write any uncaught exception to disk before it disappears ---
# Nothing else in this codebase logs exceptions anywhere, so a crash in a
# windowed build (or a console that closes on exit) leaves no record at all.
# This writes the full traceback to crash_log.txt next to the exe/script,
# for both the main thread and any background thread.
def _write_crash_log(exc_type, exc_value, exc_tb):
    try:
        _log_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        _crash_path = os.path.join(_log_dir, "crash_log.txt")
        import traceback as _tb
        with open(_crash_path, "a", encoding="utf-8") as _f:
            _f.write(f"\n=== CRASH {datetime.datetime.now().isoformat()} ===\n")
            _tb.print_exception(exc_type, exc_value, exc_tb, file=_f)
    except Exception:
        pass
    # Also fold the crash into app_log.txt (via the root logger's rotating
    # handler set up below) so a crash shows up in the one log people
    # actually attach to bug reports, instead of only in the separate
    # crash-only file. Best-effort: if the app-log handler failed to attach
    # (e.g. read-only install dir), `log` calls are still safe no-ops the
    # same way every other logging call in this file is.
    try:
        import traceback as _tb
        _formatted = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
        log.critical("=== CRASH ===\n%s", _formatted)
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

def _thread_crash_hook(args):
    _write_crash_log(args.exc_type, args.exc_value, args.exc_traceback)

sys.excepthook = _write_crash_log
threading.excepthook = _thread_crash_hook

# --- APP LOG: rotating general-purpose log, separate from the crash-only log ---
# crash_log.txt (above) only ever captures uncaught exceptions. This is for
# everything else worth a permanent record — warnings, recoverable errors,
# notable state transitions — so future tasks (e.g. replacing silent
# `except Exception: pass` blocks with logged warnings) have somewhere to
# write to. Lives next to the exe/script, same as crash_log.txt. Rotates at
# 2MB with 3 backups kept (app_log.txt, app_log.txt.1, .2, .3) so it can't
# grow unbounded over a long-running TV session.
#
# Configured on the ROOT logger (not a named one) so any module in the
# project can just do `logging.getLogger(__name__)` and get routed to
# app_log.txt automatically, regardless of import order — no need to pass a
# logger instance around or re-run this setup elsewhere.
try:
    _app_log_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    _app_log_path = os.path.join(_app_log_dir, "app_log.txt")
    _app_log_handler = logging.handlers.RotatingFileHandler(
        _app_log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _app_log_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _root_logger = logging.getLogger()
    _root_logger.setLevel(logging.INFO)
    _root_logger.addHandler(_app_log_handler)
except Exception:
    # If the log file can't be created (e.g. read-only install dir), the app
    # should still run — logging calls just become no-ops in that case.
    pass

log = logging.getLogger(__name__)
log.info("=== App start %s ===", datetime.datetime.now().isoformat())

# --- CAPTURE print() INTO app_log.txt -----------------------------------
# This is a windowed/frozen build (no console attached), so every plain
# print(...) call sprinkled through this file — [DEBUG TUNER], [WATCHDOG],
# [VISUALIZER RANDOM], etc. — has been writing to a stdout that either
# doesn't exist or isn't visible to the user, and was never actually landing
# anywhere it could be inspected after the fact. Only logging.* calls
# (log.info/log.warning) were ever reaching app_log.txt. Re-point the
# built-in print() at the same rotating log file so these diagnostics are
# actually captured for bug reports, without having to touch every call
# site individually.
import builtins as _builtins
_original_print = _builtins.print

def _logged_print(*args, **kwargs):
    try:
        _original_print(*args, **kwargs)
    except Exception:
        pass  # no console to write to (or a closed one) -- fine, log below still captures it
    try:
        _msg = " ".join(str(a) for a in args)
        log.info(_msg)
    except Exception:
        pass

_builtins.print = _logged_print

# Throttled warning helper — for the handful of except-blocks that live in the
# unconditional per-frame path (main loop tick pacing, nav-state sync, theme
# recompute). A persistent failure there would otherwise log 60x/sec and blow
# through the rotating log's whole 2MB+3-backup budget in seconds, wiping out
# any earlier history. Everywhere else in the file just uses log.warning()
# directly since those call sites are gated behind discrete, infrequent
# triggers (a menu action, a channel change, a track transition, etc.) and
# can't spam like this.
_throttled_warn_last = {}
def _log_warn_throttled(key, msg, *args, interval=30.0):
    _now = time.time()
    if _now - _throttled_warn_last.get(key, 0.0) >= interval:
        _throttled_warn_last[key] = _now
        log.warning(msg, *args)

# --- WINDOWS DPI AWARENESS (must be set before ANY window/display query) ---
# Without this, Windows treats the process as DPI-unaware and silently
# virtualizes it: every size this app asks for/reads (pygame.display.Info(),
# set_mode(), SetWindowPos, etc.) is in scaled-down "logical" pixels, while
# the OS compositor then stretches the actual rendered window back up to the
# real physical resolution to compensate. That stretch is exactly what a
# "zoomed in / cutting off the edges" screen looks like -- the app draws a
# perfectly correct frame for the (smaller, wrong) resolution it thinks it
# has, and Windows blows that frame up to fill the real monitor, pushing
# menus/guide/edges outside the visible area. This is extremely common:
# any PC with display scaling set above 100% (Windows' own default on most
# modern displays) triggers it. Declaring per-monitor-DPI-awareness here
# tells Windows to hand us the TRUE physical pixel dimensions instead, so
# NATIVE_WIDTH/HEIGHT below reflect reality and nothing gets rescaled out
# from under us.
if sys.platform == "win32":
    try:
        # Per-Monitor v2 (value 2) is preferred on Win10 1703+; fall back to
        # the older System-DPI-Aware call if shcore isn't available.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception as _dpi_err:
            print(f"[BOOT TIMING] Could not set DPI awareness: {_dpi_err}", flush=True)

# --- SDL JOYSTICK BACKEND HINTS (must be set before `import pygame`) ---
# SDL's newer HIDAPI and Windows RawInput joystick backends do a slow device
# scan that's known to hang for anywhere from several seconds to a full
# minute - especially with wireless/Bluetooth controller receivers, virtual
# controller layers (e.g. Steam Input), or other USB HID devices that get
# mis-probed as game controllers. That hang happens inside SDL's C code,
# which does not release Python's GIL - so it freezes the entire process
# (every thread, not just whichever one called it), which is why simply
# moving the call to a background thread did not fix it. Disabling these two
# backends drops SDL back to the fast legacy enumeration path. XInput-based
# controllers (the vast majority of Xbox-style pads) are unaffected.
os.environ["SDL_JOYSTICK_HIDAPI"] = "0"   # hard-set: wins even if Steam/DS4Windows already set "1"
os.environ["SDL_JOYSTICK_RAWINPUT"] = "0"  # hard-set: same reason

import pygame

# --- LOCATE LIBVLC *BEFORE* IMPORTING THE `vlc` MODULE ---------------------
# python-vlc loads libvlc.dll via ctypes at IMPORT TIME (inside vlc.py's own
# module-level code), so any DLL-search-path setup that happens after
# `import vlc` is too late to help that import. On the dev machine this was
# masked completely because the desktop VLC app happens to be installed at
# the standard location, so Windows' normal DLL search finds it regardless.
# On a machine WITHOUT VLC installed (e.g. a recycled old PC being turned
# into a cable box), `import vlc` fails here, `vlc` becomes None, and every
# video call downstream (playback AND the duration probing that
# schedules/guide data are built from) silently no-ops forever — exactly the
# "still static in the guide / black screen on channels" symptom.
#
# Fix: ship libVLC's own DLLs + plugins folder alongside the exe (they are
# NOT pulled in automatically by PyInstaller — only pure-Python packages
# are) and point python-vlc at that bundled copy first, before falling back
# to a system-wide VLC install if one happens to exist.
def _bootstrap_libvlc():
    def _log(msg):
        print(msg, flush=True)
        # Routed straight to the rotating app_log.txt (see "APP LOG" setup
        # near the top of this file) instead of a separate vlc_boot_log.txt
        # -- one log for users to send instead of two. `log` is already
        # initialized by this point since app-log setup runs first.
        log.info(msg)

    base_dir = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    bundled_dir = os.path.join(base_dir, "vlc")

    candidates = [
        bundled_dir,
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ]

    chosen = None
    for cand in candidates:
        if os.path.exists(os.path.join(cand, "libvlc.dll")):
            chosen = cand
            break

    if chosen:
        plugins_dir = os.path.join(chosen, "plugins")
        os.environ["PYTHON_VLC_MODULE_PATH"] = chosen
        if os.path.isdir(plugins_dir):
            os.environ["VLC_PLUGIN_PATH"] = plugins_dir
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(chosen)
            except Exception as e:
                _log(f"[BOOT] add_dll_directory failed for {chosen}: {e}")
        _log(f"[BOOT TIMING] libVLC located at: {chosen}")

        # DVD/ISO BACKUP SUPPORT CHECK: backup .iso playback (see
        # play_file_segmented's dvdnav:// routing) depends on the dvdnav
        # access plugin actually being present in this VLC install --
        # some slimmed-down / portable VLC bundles trim DVD-related
        # plugins out to cut package size. If it's missing, EVERY .iso
        # will fail to open identically and near-instantly regardless of
        # which disc it is (indistinguishable in-app from a genuinely bad
        # disc image, which is why this check exists -- see
        # get_last_error() in VLCEngineWrapper for the per-failure detail,
        # this is the one-time "is it even possible" check).
        if os.path.isdir(plugins_dir):
            _found_dvdnav_plugin = False
            for _root, _dirs, _files in os.walk(plugins_dir):
                for _f in _files:
                    if "dvdnav" in _f.lower() or "dvdread" in _f.lower():
                        _found_dvdnav_plugin = True
                        _log(f"[BOOT] DVD-image support: found {os.path.join(_root, _f)}")
            if not _found_dvdnav_plugin:
                _log("[BOOT WARNING] No dvdnav/dvdread plugin found under "
                     f"{plugins_dir} -- backup DVD .iso playback WILL FAIL for "
                     "every disc image (regular video files are unaffected). "
                     "This VLC bundle appears to have been built/trimmed without "
                     "DVD support. Fix: copy the full 'plugins\\access' folder "
                     "(and libdvdnav/libdvdread .dll files, if separate) from a "
                     "full desktop VLC install of the SAME bit-width/version "
                     "into this app's vlc\\plugins folder, or point this app at "
                     "a full VLC install instead of the trimmed bundled copy.")

        # LIBDVDCSS CHECK: separate from the dvdnav/dvdread plugin check
        # above -- libdvdcss decrypts the CSS-scrambled VOB sectors on
        # COMMERCIAL backup discs specifically (a disc ripped from a
        # store-bought DVD, e.g. a movie). A disc image made from
        # non-commercial/homemade content, or a disc that was already
        # decrypted during ripping, doesn't need it and will open fine
        # without it -- which is why this failure mode looks identical to
        # a missing dvdnav plugin for commercial rips (both give a fast,
        # silent, near-instant State.Ended from libdvdnav) but does NOT
        # explain a synthetic/placeholder test image failing (that's
        # covered by DVD_ISO_OPEN_FAIL_GRACE_MS's "no real VIDEO_TS
        # structure" case instead -- see game_deck's watchdog). libdvdcss
        # ships as a loose DLL next to libvlc.dll, not inside plugins\,
        # since libdvdread dlopens it directly rather than going through
        # VLC's module system.
        _found_libdvdcss = False
        for _scan_dir in (chosen, plugins_dir):
            if not os.path.isdir(_scan_dir):
                continue
            for _f in os.listdir(_scan_dir):
                if "dvdcss" in _f.lower():
                    _found_libdvdcss = True
                    _log(f"[BOOT] CSS decryption support: found {os.path.join(_scan_dir, _f)}")
        if not _found_libdvdcss:
            _log("[BOOT WARNING] No libdvdcss DLL found alongside libvlc.dll or "
                 f"under {plugins_dir} -- backup .iso images ripped from "
                 "commercial/store-bought DVDs (CSS-encrypted) will fail to "
                 "open (homemade/already-decrypted disc images are "
                 "unaffected). Symptom looks identical to the missing-dvdnav "
                 "case above: a fast State.Ended shortly after open, for "
                 "every commercial disc regardless of title. Fix: place "
                 "libdvdcss-2.dll (or libdvdcss.dll, depending on version) "
                 "next to libvlc.dll in this app's vlc folder -- it ships "
                 "with any full desktop VLC install of the SAME "
                 "bit-width/version, or can be obtained standalone from "
                 "videolan.org.")
    else:
        _log("[BOOT WARNING] No libvlc.dll found in bundled 'vlc' folder or "
             "standard install paths. Video playback will not work on this "
             "machine unless VLC (matching 32/64-bit) is installed.")

    return chosen is not None

_libvlc_located = _bootstrap_libvlc()

# Needed at module scope: the end-of-show watchdog in the main loop checks
# vlc.State.Ended/Stopped/Error directly. It used to rely on `vlc` only ever
# being imported inside other functions (initialize_vlc_on_demand, etc.), so
# that bare-name lookup raised a NameError every single time, silently
# swallowed by the watchdog's own try/except — meaning "video finished"
# never actually triggered and a channel could never self-heal/advance once
# left running.
try:
    import vlc
except Exception as _vlc_import_err:
    print(f"[BOOT WARNING] Could not import vlc at module scope: {_vlc_import_err}")
    vlc = None

# FIXED: Pre-initialize Tkinter backend immediately to eliminate the File Explorer freeze
try:
    import tkinter as tk
    root_preload = tk.Tk()
    root_preload.withdraw()
    root_preload.destroy()
    print("[BOOT TIMING] Tkinter backend preloader initialized successfully.", flush=True)
except Exception as tk_err:
    print(f"[BOOT TIMING] Tkinter preloader skipped: {tk_err}", flush=True)

print("[BOOT TIMING] Starting script at", time.time(), flush=True)

# Initialize ONLY the pygame subsystems this app uses (display + font).
pygame.display.init()
pygame.font.init()
# NOTE: pygame.joystick.init() is intentionally NOT called here.
# On this system SDL's joystick subsystem init triggers a full device scan
# internally, causing a 30-60s freeze before the loading screen appears.
# It is started on the background thread inside _background_subsystem_worker().

# FIXED: Sets repeat to 0 on boot to ensure your standard up/down channel surfing and WASD guide keys work as normal taps
pygame.key.set_repeat(0, 0)
print("[BOOT TIMING] pygame.init() display and font subsystems completed at", time.time(), flush=True)

# ── OS FUNCTION SUPPRESSION — KEYS STAY MAPPABLE (Windows only) ─────────────
# Uses a WH_KEYBOARD_LL low-level hook (ctypes only, no extra packages).
#
# The hook does TWO things for each intercepted key:
#   1. Blocks the OS-level FUNCTION (volume change, mail open, browser nav…)
#      by returning 1 so Windows never acts on it.
#   2. Forwards the bare keypress to the pygame window via PostMessageW so
#      the program can still see the button and remap it to anything.
#
# Result: pressing your remote's volume-up button no longer changes the PC
# speaker volume, but the program receives the keypress and you can remap it
# to the program's own volume control (or anything else).
#
# FUNCTION SUPPRESSED (OS never acts on these):
#   F1-F12, Volume Mute/Down/Up, Media Play/Pause/Stop/Next/Prev,
#   Browser Back/Forward/Refresh/Stop/Search/Favorites/Home,
#   Mail, Media-Select, App1, App2
#
# FULLY UNTOUCHED (hook never fires for these at all):
#   All letters a-z, digits 0-9, WASD, arrows, Esc, Enter, Space,
#   Backspace, Tab, Shift, Ctrl, Alt, Windows key, Ctrl+Alt+Delete
#
# The hook runs on a background daemon thread with its own Win32 message pump.
if os.name == "nt":
    import ctypes as _ctypes
    import ctypes.wintypes as _ctwt
    import threading as _hook_threading

    # DEBUG SWITCH: when True, every keypress the hook sees is printed to
    # app_log.txt (VK code, suppressed or not, whether a pygame event was
    # posted). Needed to diagnose remote buttons that won't map on some PCs.
    #
    # MUST stay False in normal operation. This print (with flush=True) runs
    # INSIDE the low-level keyboard hook callback, on every single keystroke.
    # Windows enforces a hard time limit on how long a WH_KEYBOARD_LL hook is
    # allowed to take to return (LowLevelHooksTimeout) -- if it runs too slow
    # too often, Windows silently rips the hook out and never calls it again
    # for the rest of the process's life. No crash, no error: suppressed keys
    # (OK/Back/etc.) just stop being seen at all. Confirmed via a real user
    # log: after several rapid button presses each doing a synchronous disk
    # flush, EVERY subsequent keystroke -- including totally unrelated keys
    # elsewhere in the app -- stopped generating any hook activity whatsoever,
    # with no crash or freeze. Only flip this on briefly on a dev machine
    # while diagnosing a specific new remote, and only tap it a few times
    # (never "spam test" with it on), then turn it back off.
    _HOOK_DEBUG = False  # Must stay False in normal operation -- see warning above.
    # WAS True: left on after the OK/Back remote-diagnosis session. With it
    # True, every single keystroke in the whole app (not just the suppressed
    # ones) hits a synchronous, flush=True disk write from inside the
    # WH_KEYBOARD_LL hook callback. Holding a key (e.g. a volume button)
    # fires that callback dozens of times a second; enough consecutive slow
    # callbacks and Windows' LowLevelHooksTimeout silently UNINSTALLS the
    # hook for the rest of the process's life -- no crash, no log entry.
    # Once that happens: (1) suppressed keys stop reaching pygame at all,
    # which is why a held slider would stop moving mid-hold, and (2) Windows
    # goes back to handling volume/media/browser keys itself, which is why
    # the OS volume changed even though a remap was in place. Both symptoms
    # trace back to this one flag.

    # Only keys that have OS-level side-effects go here.
    # Normal letters, digits, arrows, ESC, Enter, WASD are absent on purpose.
    _SUPPRESSED_VKS = frozenset({
        # F1–F12  (can invoke Windows help dialogs, accessibility, OSD, etc.)
        0x70, 0x71, 0x72, 0x73,   # F1  F2  F3  F4
        0x74, 0x75, 0x76, 0x77,   # F5  F6  F7  F8
        0x78, 0x79, 0x7A, 0x7B,   # F9  F10 F11 F12
        # Volume keys (hardware multimedia row)
        0xAD,  # VK_VOLUME_MUTE
        0xAE,  # VK_VOLUME_DOWN
        0xAF,  # VK_VOLUME_UP
        # Media transport keys
        0xB0,  # VK_MEDIA_NEXT_TRACK
        0xB1,  # VK_MEDIA_PREV_TRACK
        0xB2,  # VK_MEDIA_STOP
        0xB3,  # VK_MEDIA_PLAY_PAUSE
        # Browser hot-keys (dedicated keyboard buttons)
        0xA6,  # VK_BROWSER_BACK
        0xA7,  # VK_BROWSER_FORWARD
        0xA8,  # VK_BROWSER_REFRESH
        0xA9,  # VK_BROWSER_STOP
        0xAA,  # VK_BROWSER_SEARCH
        0xAB,  # VK_BROWSER_FAVORITES
        0xAC,  # VK_BROWSER_HOME
        # Application-launch keys
        0xB4,  # VK_LAUNCH_MAIL
        0xB5,  # VK_LAUNCH_MEDIA_SELECT
        0xB6,  # VK_LAUNCH_APP1  (My Computer / custom)
        0xB7,  # VK_LAUNCH_APP2  (Calculator / custom)
    })

    _WH_KEYBOARD_LL  = 13
    _WM_KEYDOWN      = 0x0100
    _WM_KEYUP        = 0x0101
    _WM_SYSKEYDOWN   = 0x0104
    _WM_SYSKEYUP     = 0x0105

    # VK code → SDL2 scancode for every key we suppress.
    # SDL2 keycode = scancode | 0x40000000  (SDLK_SCANCODE_MASK).
    # These values match SDL_scancode.h in SDL2 2.x and SDL3/SDL2-compat.
    # Verified: VK_BROWSER_HOME (0xAC) → scancode 269 → pygame key 1073742093
    # matches the live remap log where the user's menu button produced #269.
    _SDLK_MASK = 0x40000000
    _VK_TO_SDL_SC = {
        # F-keys
        0x70: 58,   # F1
        0x71: 59,   # F2
        0x72: 60,   # F3
        0x73: 61,   # F4
        0x74: 62,   # F5
        0x75: 63,   # F6
        0x76: 64,   # F7
        0x77: 65,   # F8
        0x78: 66,   # F9
        0x79: 67,   # F10
        0x7A: 68,   # F11
        0x7B: 69,   # F12
        # Volume / audio
        0xAD: 127,  # VK_VOLUME_MUTE      → SDL_SCANCODE_MUTE
        #            NOT 262 (AUDIOMUTE). A real user log proved this remote's
        #            mute button reaches SDL as scancode 127 (SDL_SCANCODE_MUTE)
        #            on the native path -- "[REMOTE REMAP DEBUG] ... key=1073741951
        #            scancode=127" (1073741951 == 127 | 0x40000000). The saved
        #            binding in retro_config.json is likewise "n": 1073741951.
        #            When the hook DOES suppress 0xAD it must re-post that SAME
        #            scancode (127), or the posted event wouldn't match the key
        #            the user actually bound and the remap would silently miss.
        0xAE: 129,  # VK_VOLUME_DOWN      → SDL_SCANCODE_VOLUMEDOWN
        0xAF: 128,  # VK_VOLUME_UP        → SDL_SCANCODE_VOLUMEUP
        # Media transport
        0xB0: 258,  # VK_MEDIA_NEXT_TRACK → SDL_SCANCODE_AUDIONEXT
        0xB1: 259,  # VK_MEDIA_PREV_TRACK → SDL_SCANCODE_AUDIOPREV
        0xB2: 260,  # VK_MEDIA_STOP       → SDL_SCANCODE_AUDIOSTOP
        0xB3: 261,  # VK_MEDIA_PLAY_PAUSE → SDL_SCANCODE_AUDIOPLAY
        # Browser / navigation hotkeys
        0xA6: 270,  # VK_BROWSER_BACK     → SDL_SCANCODE_AC_BACK
        0xA7: 271,  # VK_BROWSER_FORWARD  → SDL_SCANCODE_AC_FORWARD
        0xA8: 273,  # VK_BROWSER_REFRESH  → SDL_SCANCODE_AC_REFRESH
        0xA9: 272,  # VK_BROWSER_STOP     → SDL_SCANCODE_AC_STOP
        0xAA: 274,  # VK_BROWSER_SEARCH   → SDL_SCANCODE_AC_SEARCH
        0xAB: 275,  # VK_BROWSER_FAVORITES→ SDL_SCANCODE_AC_BOOKMARKS
        0xAC: 269,  # VK_BROWSER_HOME     → SDL_SCANCODE_AC_HOME
        # Application-launch keys
        0xB4: 265,  # VK_LAUNCH_MAIL      → SDL_SCANCODE_MAIL
        0xB5: 263,  # VK_LAUNCH_MEDIA_SEL → SDL_SCANCODE_MEDIASELECT
        0xB6: 266,  # VK_LAUNCH_APP1      → SDL_SCANCODE_COMPUTER
        0xB7: 267,  # VK_LAUNCH_APP2      → SDL_SCANCODE_CALCULATOR
    }

    # WINFUNCTYPE signature — must match the 64-bit Windows ABI exactly.
    # LRESULT / LPARAM are signed pointer-width; WPARAM is unsigned pointer-width.
    # Pinned at module level so GC never collects it while the hook is live.
    _KbdHookProcType = _ctypes.WINFUNCTYPE(
        _ctypes.c_ssize_t,   # LRESULT  (signed, pointer-sized)
        _ctypes.c_int,       # nCode
        _ctypes.c_size_t,    # wParam   (WPARAM — unsigned, pointer-sized)
        _ctypes.c_ssize_t,   # lParam   (LPARAM — signed, pointer-sized)
    )

    # Declare argtypes/restype on every user32 call we make.
    # Without this, ctypes defaults all args to 32-bit c_int, which overflows
    # on 64-bit Python when lParam carries a 64-bit address.
    _u32 = _ctypes.windll.user32
    _u32.SetWindowsHookExW.restype  = _ctypes.c_void_p
    _u32.SetWindowsHookExW.argtypes = [
        _ctypes.c_int,       # idHook
        _ctypes.c_void_p,    # lpfn  (the HOOKPROC — void* is fine here)
        _ctypes.c_void_p,    # hMod
        _ctypes.c_ulong,     # dwThreadId
    ]
    _u32.CallNextHookEx.restype  = _ctypes.c_ssize_t
    _u32.CallNextHookEx.argtypes = [
        _ctypes.c_void_p,    # hhk   (ignored on modern Windows; pass None)
        _ctypes.c_int,       # nCode
        _ctypes.c_size_t,    # wParam (WPARAM)
        _ctypes.c_ssize_t,   # lParam (LPARAM)
    ]
    _u32.GetMessageW.restype  = _ctypes.c_int
    _u32.GetMessageW.argtypes = [
        _ctypes.c_void_p,    # lpMsg
        _ctypes.c_void_p,    # hWnd
        _ctypes.c_uint,      # wMsgFilterMin
        _ctypes.c_uint,      # wMsgFilterMax
    ]
    _u32.TranslateMessage.argtypes  = [_ctypes.c_void_p]
    _u32.DispatchMessageW.argtypes  = [_ctypes.c_void_p]
    _u32.UnhookWindowsHookEx.argtypes = [_ctypes.c_void_p]

    def _kbd_hook_callback(nCode, wParam, lParam):
        """Low-level keyboard hook.

        For keys in _SUPPRESSED_VKS:
          - Return 1 immediately — Windows never generates WM_APPCOMMAND, so no
            volume change, no browser launch, no OS side-effect of any kind.
          - Instead of re-injecting through Win32 (which would re-trigger OS
            functions), call pygame.event.post() directly from this thread.
            pygame-ce's event queue is thread-safe; the main loop sees the event
            exactly as if SDL2 had produced it from hardware, and the remap
            system can bind the button to anything.
        For all other keys: CallNextHookEx — completely unchanged.
        try/except guarantees a bug here can NEVER eat an unrelated key.

        _HOOK_DEBUG (module flag just below) logs every single key the hook
        sees — VK code, whether it's in _SUPPRESSED_VKS, and whether a
        pygame event was posted for it — to app_log.txt via the same logger
        used everywhere else.  Turn OFF after the remote-mapping issue is
        confirmed fixed; it prints on every keystroke.
        """
        try:
            if (nCode >= 0
                    and lParam
                    and wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN,
                                   _WM_KEYUP,   _WM_SYSKEYUP)):
                vk = _ctypes.c_ulong.from_address(lParam).value
                is_down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                if vk in _SUPPRESSED_VKS:
                    sc = _VK_TO_SDL_SC.get(vk)
                    if not sc:
                        # Unmapped suppressed VK -- don't drop it silently.
                        # Fabricate a stable pseudo-scancode from the VK
                        # itself (offset well clear of real SDL scancodes
                        # 0-283 and the WM_APPCOMMAND fallback range 500+)
                        # so it still shows up as a bindable "#NNN" key.
                        sc = 400 + vk
                    pk = sc | _SDLK_MASK
                    if is_down:
                        pygame.event.post(pygame.event.Event(
                            pygame.KEYDOWN,
                            key=pk, scancode=sc, mod=0, unicode=""))
                    else:
                        pygame.event.post(pygame.event.Event(
                            pygame.KEYUP,
                            key=pk, scancode=sc, mod=0, unicode=""))
                    if _HOOK_DEBUG:
                        print(f"[KBD HOOK DEBUG] vk=0x{vk:02X} {'DOWN' if is_down else 'UP'} "
                              f"SUPPRESSED -> posted pygame key={pk} (sc={sc})", flush=True)
                    return 1  # OS function blocked; pygame got the event directly
                elif _HOOK_DEBUG and is_down:
                    print(f"[KBD HOOK DEBUG] vk=0x{vk:02X} DOWN not suppressed -> CallNextHookEx (normal path)", flush=True)
        except Exception as _hcb_err:
            print(f"[KBD HOOK] callback error (key passed through): {_hcb_err}", flush=True)
        return _u32.CallNextHookEx(None, nCode, wParam, lParam)

    _KBD_HOOK_PROC_PTR = _KbdHookProcType(_kbd_hook_callback)  # pinned — must not be GC'd
    _kbd_hook_handle   = None

    def _kbd_hook_pump():
        global _kbd_hook_handle
        try:
            _kbd_hook_handle = _u32.SetWindowsHookExW(
                _WH_KEYBOARD_LL, _KBD_HOOK_PROC_PTR, None, 0)
            if not _kbd_hook_handle:
                print("[KBD HOOK] SetWindowsHookExW returned NULL — hook inactive.", flush=True)
                return
            msg = _ctwt.MSG()
            while _u32.GetMessageW(_ctypes.byref(msg), None, 0, 0) != 0:
                _u32.TranslateMessage(_ctypes.byref(msg))
                _u32.DispatchMessageW(_ctypes.byref(msg))
        except Exception as _kbd_err:
            print(f"[KBD HOOK] thread error: {_kbd_err}", flush=True)

    _hook_threading.Thread(
        target=_kbd_hook_pump, daemon=True, name="OsKeyHookPump"
    ).start()
    print("[BOOT] OS interrupt-key suppression active (F1-F12, volume, media, mail, browser).", flush=True)

    # ── SELF-HEALING WATCHDOG ────────────────────────────────────────────
    # Windows enforces LowLevelHooksTimeout (~a few hundred ms) on every
    # single WH_KEYBOARD_LL callback. If the callback (or the thread that
    # installed the hook) is ever slow enough, enough times in a row --
    # e.g. this thread getting starved because the main thread is stalled
    # / not responding during a rough boot, heavy disk I/O, GC pause under
    # load, etc. -- Windows silently UNINSTALLS the hook for the rest of
    # the process's life. No crash, no log entry, nothing recoverable from
    # inside the callback itself. Once that happens: suppressed keys (the
    # remote's volume/mute buttons) stop reaching pygame AND Windows goes
    # back to handling them itself, which is exactly "the remap stopped
    # working" + "volume keeps changing the Windows volume" happening
    # together, mid-session, with no code change in between.
    #
    # Rather than trying to detect the silent removal (there's no reliable
    # Win32 signal for "is my hook still installed"), this just tears down
    # and reinstalls the hook on a fixed interval as cheap preventive
    # maintenance -- if it silently died, this puts it back within one
    # interval; if it's still fine, unhook+rehook is a no-op the user never
    # notices (a few ms gap, no visible key ever falls in that gap in
    # practice).
    _HOOK_REINSTALL_SEC = 45

    def _kbd_hook_watchdog():
        global _kbd_hook_handle
        while True:
            time.sleep(_HOOK_REINSTALL_SEC)
            try:
                old_handle = _kbd_hook_handle
                new_handle = _u32.SetWindowsHookExW(
                    _WH_KEYBOARD_LL, _KBD_HOOK_PROC_PTR, None, 0)
                if new_handle:
                    _kbd_hook_handle = new_handle
                    if old_handle:
                        _u32.UnhookWindowsHookEx(old_handle)
                else:
                    print("[KBD HOOK WATCHDOG] Reinstall attempt returned NULL; keeping previous handle.", flush=True)
            except Exception as _wd_err:
                print(f"[KBD HOOK WATCHDOG] Reinstall attempt failed: {_wd_err}", flush=True)

    _hook_threading.Thread(
        target=_kbd_hook_watchdog, daemon=True, name="OsKeyHookWatchdog"
    ).start()

    # ── WINDOWS SYSTEM-MUTE GUARD (Core Audio) ───────────────────────────────
    # Belt-and-suspenders companion to the keyboard hook above. The hook stops
    # the mute *key* from reaching the OS -- but ONLY when it actually fires.
    # A real user log proved this remote's mute button can slip past the
    # WH_KEYBOARD_LL hook and still reach the OS mute path: the app saw the key
    # natively as scancode 127 with synthetic=False (i.e. NOT via the hook's
    # re-post), while the RawInput trace showed VKey=0xAD -- so Windows muted
    # its own endpoint at the same instant the program toggled its own mute.
    # Net effect the user reported: pressing the remapped mute key muted BOTH
    # the program AND the whole PC.
    #
    # This guard keeps the default audio *render* endpoint forcibly UNMUTED
    # while the program runs, via the Core Audio IAudioEndpointVolume interface
    # driven through raw ctypes/COM (no third-party dependency). A light polling
    # thread reads GetMute() and, the moment anything (the remote, the volume
    # OSD, another app) mutes the system endpoint, calls SetMute(False) again.
    # The program's OWN mute is a separate VLC/mixer volume state that this does
    # not touch, so the mute button ends up affecting only the program.
    #
    # Entirely best-effort and self-contained: any failure (audio API absent,
    # COM init refused, headless/RDP session, etc.) is swallowed and simply
    # leaves prior behaviour in place -- it can never break audio or the app.
    _SYS_MUTE_GUARD_POLL_SEC = 0.12

    class _GUID(_ctypes.Structure):
        _fields_ = [("Data1", _ctypes.c_ulong),
                    ("Data2", _ctypes.c_ushort),
                    ("Data3", _ctypes.c_ushort),
                    ("Data4", _ctypes.c_ubyte * 8)]

    def _make_guid(guid_str):
        s = guid_str.strip("{}")
        p = s.split("-")
        d1 = int(p[0], 16)
        d2 = int(p[1], 16)
        d3 = int(p[2], 16)
        tail = p[3] + p[4]
        d4 = (_ctypes.c_ubyte * 8)(*[int(tail[i:i + 2], 16) for i in range(0, 16, 2)])
        return _GUID(d1, d2, d3, d4)

    def _com_method(iface_addr, index, restype, argtypes):
        # A COM interface pointer's first machine word points at its vtable.
        # vtable[index] is the function pointer; every method takes the
        # interface ('this') as an implicit first argument.
        vtbl = _ctypes.cast(iface_addr, _ctypes.POINTER(_ctypes.c_void_p))[0]
        fptr = _ctypes.cast(vtbl, _ctypes.POINTER(_ctypes.c_void_p))[index]
        proto = _ctypes.WINFUNCTYPE(restype, _ctypes.c_void_p, *argtypes)
        return proto(fptr)

    def _com_release(iface_addr):
        try:
            if iface_addr:
                _com_method(iface_addr, 2, _ctypes.c_ulong, [])(iface_addr)
        except Exception:
            pass

    # VOLUME-LEVEL companion to the mute guard below. The keyboard hook only
    # ever sees the VK_VOLUME_UP/DOWN message this remote ALSO sends -- but
    # the RawInput trace shows the same button press also fires a raw HID
    # Consumer Control usage report (Usage Page 0x0C) on a separate logical
    # collection of the same physical device:
    #   [RAWINPUT DEBUG] HID dwSizeHid=4 dwCount=1 bytes=01e90000 dev=131145
    #   [RAWINPUT DEBUG] KEYBOARD MakeCode=0 Flags=2 VKey=0xAF ... dev=None
    # Windows' HID audio class driver acts on that Consumer Control report
    # directly -- it changes the system volume without ever going through
    # window messages, so no WH_KEYBOARD_LL hook (or anything else at the
    # message-pump level) can see or block it. Same root cause as the mute
    # leak this guard was originally built for, just for volume level
    # instead of the mute flag.
    #
    # Fix: pin the master render endpoint's volume level, the same way the
    # mute guard pins the mute flag. The level in effect the moment this
    # thread first reads it (i.e. whatever the user had Windows set to when
    # the program launched) becomes the locked baseline; any drift away from
    # it -- remote leak, volume OSD, another app, anything -- is reverted on
    # the next poll tick. The program's OWN volume slider is a separate VLC/
    # mixer value untouched by any of this, exactly like the mute guard.
    def _sys_mute_guard():
        nonlocal_state = {"locked_level": None}
        try:
            _ole32 = _ctypes.windll.ole32
            _ole32.CoCreateInstance.restype = _ctypes.c_long
            _ole32.CoCreateInstance.argtypes = [
                _ctypes.POINTER(_GUID), _ctypes.c_void_p, _ctypes.c_ulong,
                _ctypes.POINTER(_GUID), _ctypes.POINTER(_ctypes.c_void_p)]
            # COINIT_APARTMENTTHREADED (0x2) on this dedicated thread.
            _ole32.CoInitializeEx(None, 0x2)
            CLSID_MMDeviceEnumerator = _make_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
            IID_IMMDeviceEnumerator  = _make_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            IID_IAudioEndpointVolume = _make_guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        except Exception as _mg_init_err:
            print(f"[SYS MUTE GUARD] disabled (init failed): {_mg_init_err}", flush=True)
            return

        CLSCTX_ALL = 0x17
        eRender = 0
        eConsole = 0
        print("[SYS MUTE GUARD] active -- system render endpoint will be kept unmuted "
              "and its volume level locked.", flush=True)

        while True:
            enum_p = _ctypes.c_void_p()
            dev_p = _ctypes.c_void_p()
            epv_p = _ctypes.c_void_p()
            try:
                hr = _ole32.CoCreateInstance(
                    _ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                    _ctypes.byref(IID_IMMDeviceEnumerator), _ctypes.byref(enum_p))
                if hr != 0 or not enum_p.value:
                    raise OSError(f"CoCreateInstance failed hr=0x{hr & 0xFFFFFFFF:08X}")

                # IMMDeviceEnumerator::GetDefaultAudioEndpoint == vtable index 4
                get_default = _com_method(
                    enum_p.value, 4, _ctypes.c_long,
                    [_ctypes.c_int, _ctypes.c_int, _ctypes.POINTER(_ctypes.c_void_p)])
                hr = get_default(enum_p.value, eRender, eConsole, _ctypes.byref(dev_p))
                if hr != 0 or not dev_p.value:
                    raise OSError(f"GetDefaultAudioEndpoint failed hr=0x{hr & 0xFFFFFFFF:08X}")

                # IMMDevice::Activate == vtable index 3
                activate = _com_method(
                    dev_p.value, 3, _ctypes.c_long,
                    [_ctypes.POINTER(_GUID), _ctypes.c_ulong, _ctypes.c_void_p,
                     _ctypes.POINTER(_ctypes.c_void_p)])
                hr = activate(dev_p.value, _ctypes.byref(IID_IAudioEndpointVolume),
                              CLSCTX_ALL, None, _ctypes.byref(epv_p))
                if hr != 0 or not epv_p.value:
                    raise OSError(f"Activate(IAudioEndpointVolume) failed hr=0x{hr & 0xFFFFFFFF:08X}")

                # IAudioEndpointVolume vtable: SetMute=14, GetMute=15,
                # SetMasterVolumeLevelScalar=7, GetMasterVolumeLevelScalar=9.
                set_mute = _com_method(
                    epv_p.value, 14, _ctypes.c_long,
                    [_ctypes.c_int, _ctypes.c_void_p])
                get_mute = _com_method(
                    epv_p.value, 15, _ctypes.c_long,
                    [_ctypes.POINTER(_ctypes.c_int)])
                set_vol = _com_method(
                    epv_p.value, 7, _ctypes.c_long,
                    [_ctypes.c_float, _ctypes.c_void_p])
                get_vol = _com_method(
                    epv_p.value, 9, _ctypes.c_long,
                    [_ctypes.POINTER(_ctypes.c_float)])

                # A fresh interface (first run, or rebuilt after a device
                # change) means any previous locked level is stale -- grab
                # a new baseline from whatever Windows is set to right now.
                nonlocal_state["locked_level"] = None

                # Reuse this one activated interface for the whole session;
                # only rebuild if a call starts failing (e.g. default device
                # changed / was unplugged).
                while True:
                    muted = _ctypes.c_int(0)
                    hr = get_mute(epv_p.value, _ctypes.byref(muted))
                    if hr != 0:
                        raise OSError(f"GetMute failed hr=0x{hr & 0xFFFFFFFF:08X}")
                    if muted.value:
                        set_mute(epv_p.value, 0, None)

                    level = _ctypes.c_float(0.0)
                    hr = get_vol(epv_p.value, _ctypes.byref(level))
                    if hr != 0:
                        raise OSError(f"GetMasterVolumeLevelScalar failed hr=0x{hr & 0xFFFFFFFF:08X}")
                    locked = nonlocal_state["locked_level"]
                    if locked is None:
                        # First read this session -- lock in whatever level
                        # Windows already had (e.g. set by the user before
                        # launching the program).
                        nonlocal_state["locked_level"] = level.value
                    elif abs(level.value - locked) > 0.001:
                        # Drifted (remote leak, volume OSD, another app...)
                        # -- put it back. 0.001 tolerance absorbs float
                        # round-trip noise so this never fights itself.
                        set_vol(epv_p.value, locked, None)

                    time.sleep(_SYS_MUTE_GUARD_POLL_SEC)
            except Exception as _mg_err:
                print(f"[SYS MUTE GUARD] rebuilding audio interface: {_mg_err}", flush=True)
                time.sleep(1.0)
            finally:
                _com_release(epv_p.value)
                _com_release(dev_p.value)
                _com_release(enum_p.value)

    _hook_threading.Thread(
        target=_sys_mute_guard, daemon=True, name="SysMuteGuard"
    ).start()

    # ── VOLUME/MUTE OSD SUPPRESSION ──────────────────────────────────────
    # Neither the WH_KEYBOARD_LL hook above nor SYS MUTE GUARD stops the
    # native Windows volume flyout (the on-screen "speaker + slider" toast)
    # from popping up. That's because the flyout isn't drawn in response to
    # the VK_VOLUME_UP/DOWN/MUTE message the hook suppresses -- it's drawn
    # by the shell in direct response to the raw HID Consumer Control usage
    # report the SAME remote button also fires (see the long comment above
    # _sys_mute_guard: that raw HID report bypasses window messages and the
    # keyboard hook entirely, which is also why SYS MUTE GUARD has to be a
    # poll-and-revert loop instead of a true block). The hook blocking the
    # OS-level volume *change* and this watcher hiding the *toast* are two
    # separate problems with two separate fixes.
    #
    # Fix: watch for the OS creating/showing the flyout's host window and
    # hide it the instant it appears. Since Windows 10, that flyout is
    # hosted in a top-level window of class "NativeHWNDHost" -- this is the
    # same technique long used by third-party "disable volume OSD" utilities.
    # Entirely best-effort: if the class name ever changes on some future
    # Windows build, this simply stops finding it and does nothing (falls
    # back to today's behavior) -- it can never break real volume/mute keys
    # or anything else.
    _EVENT_OBJECT_SHOW    = 0x8002
    _EVENT_OBJECT_CREATE  = 0x8000
    _WINEVENT_OUTOFCONTEXT = 0x0000
    _SW_HIDE = 0
    _OSD_HOST_CLASS_NAMES = {"NativeHWNDHost"}  # Win10/11 volume+brightness flyout host

    _u32.SetWinEventHook.restype  = _ctypes.c_void_p
    _u32.SetWinEventHook.argtypes = [
        _ctypes.c_uint, _ctypes.c_uint, _ctypes.c_void_p, _ctypes.c_void_p,
        _ctypes.c_uint, _ctypes.c_uint, _ctypes.c_uint,
    ]
    _u32.GetClassNameW.restype  = _ctypes.c_int
    _u32.GetClassNameW.argtypes = [_ctypes.c_void_p, _ctypes.c_wchar_p, _ctypes.c_int]
    _u32.ShowWindow.restype  = _ctypes.c_int
    _u32.ShowWindow.argtypes = [_ctypes.c_void_p, _ctypes.c_int]

    _WinEventProcType = _ctypes.WINFUNCTYPE(
        None,               # void return
        _ctypes.c_void_p,   # hWinEventHook
        _ctypes.c_uint,     # event
        _ctypes.c_void_p,   # hwnd
        _ctypes.c_long,     # idObject
        _ctypes.c_long,     # idChild
        _ctypes.c_uint,     # idEventThread
        _ctypes.c_uint,     # dwmsEventTime
    )

    def _volume_osd_event_callback(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime):
        try:
            if not hwnd:
                return
            _buf = _ctypes.create_unicode_buffer(64)
            _u32.GetClassNameW(hwnd, _buf, 64)
            if _buf.value in _OSD_HOST_CLASS_NAMES:
                _u32.ShowWindow(hwnd, _SW_HIDE)
        except Exception:
            pass  # never let a watcher bug affect the rest of the app

    # Pinned at module level so GC never collects it while the hook is live
    # (same requirement as _KBD_HOOK_PROC_PTR above).
    _VOLUME_OSD_PROC_PTR = _WinEventProcType(_volume_osd_event_callback)

    def _volume_osd_watcher_pump():
        try:
            # Listen for both CREATE and SHOW -- different Windows builds have
            # been observed to fire one or the other first for this host window.
            hook = _u32.SetWinEventHook(
                _EVENT_OBJECT_CREATE, _EVENT_OBJECT_SHOW,
                None, _VOLUME_OSD_PROC_PTR, 0, 0, _WINEVENT_OUTOFCONTEXT)
            if not hook:
                print("[VOLUME OSD GUARD] SetWinEventHook returned NULL — OSD suppression inactive.", flush=True)
                return
            print("[VOLUME OSD GUARD] active -- native volume/mute flyout will be hidden on appearance.", flush=True)
            msg = _ctwt.MSG()
            while _u32.GetMessageW(_ctypes.byref(msg), None, 0, 0) != 0:
                _u32.TranslateMessage(_ctypes.byref(msg))
                _u32.DispatchMessageW(_ctypes.byref(msg))
        except Exception as _osd_err:
            print(f"[VOLUME OSD GUARD] thread error: {_osd_err}", flush=True)

    _hook_threading.Thread(
        target=_volume_osd_watcher_pump, daemon=True, name="VolumeOsdGuard"
    ).start()
    # ── END VOLUME/MUTE OSD SUPPRESSION ──────────────────────────────────
# ── END OS INTERRUPT-KEY SUPPRESSION ─────────────────────────────────────────

# NOTE: pygame.mixer.init() used to run right here, synchronously, before the
# window even exists. On Windows, if the audio subsystem isn't fully spun up
# yet (very common right after boot / when this app is set to launch on
# startup), mixer.init() can block for tens of seconds waiting on the audio
# device -- and since nothing had been drawn to screen yet, that showed up as
# a plain black screen with no window, no loading art, nothing, for however
# long the audio driver took. Moved below (see "AUDIO MIXER INIT" further
# down) so the window is created and a first frame is on screen BEFORE this
# potentially-slow call happens.

# Hardware display monitor resolution auto-tracking parameters.
# Prefer get_desktop_sizes(): it reports the true PHYSICAL monitor resolution
# independent of any current window. pygame.display.Info().current_w/h, called
# before the first set_mode, is only *documented* to return the desktop size --
# in practice, if DPI awareness didn't fully apply it can hand back the SCALED
# (logical) size (e.g. 1536x864 for a 1920x1080 panel at 125%). A too-small
# NATIVE here shrinks the fullscreen surface AND the 4:3 letterbox computed from
# it, which is what made restored/boot fullscreen look smaller and "not 4:3".
monitor_info = pygame.display.Info()
NATIVE_WIDTH = monitor_info.current_w
NATIVE_HEIGHT = monitor_info.current_h
try:
    _desktop_sizes = pygame.display.get_desktop_sizes()
    if _desktop_sizes:
        NATIVE_WIDTH, NATIVE_HEIGHT = _desktop_sizes[0]
except Exception as _desk_err:
    print(f"[BOOT TIMING] get_desktop_sizes() unavailable, using display.Info() size: {_desk_err}", flush=True)
WINDOW_WIDTH, WINDOW_HEIGHT = NATIVE_WIDTH, NATIVE_HEIGHT

# AUTO ASPECT RATIO DETECTION: read the actual aspect ratio off whatever Windows
# is currently set to (the monitor/TV's native resolution) instead of relying on
# a manual setting. 4:3 == 1.333..., 16:9 == 1.778...; classify the native
# resolution against whichever it's closer to so any oddball native resolution
# (e.g. 1366x768) still lands on the right side. This drives both the menu/TV
# guide letterboxing AND the VLC decode/output aspect further down — see
# DETECTED_ASPECT_RATIO usage in PART 20 (menu letterboxing) and in
# VLCEngineWrapper.__init__ (video decode buffer + video_set_aspect_ratio).
_native_ratio = (NATIVE_WIDTH / NATIVE_HEIGHT) if NATIVE_HEIGHT else (16.0 / 9.0)
DETECTED_ASPECT_RATIO = "4:3" if abs(_native_ratio - (4.0 / 3.0)) < abs(_native_ratio - (16.0 / 9.0)) else "16:9"
print(f"[BOOT TIMING] Native display {NATIVE_WIDTH}x{NATIVE_HEIGHT} detected as {DETECTED_ASPECT_RATIO}.", flush=True)
# Routed to the rotating app_log.txt (see "APP LOG" setup just above) instead
# of a separate aspect_ratio_detected.txt file -- one log for users to send
# instead of two. Includes the same "if this looks wrong" guidance the old
# standalone file used to carry, so nothing is lost by dropping it.
log.info(
    "Aspect ratio detection: native display %sx%s, ratio=%.4f, classified as %s. "
    "If this says 16:9 but the screen is physically 4:3, Windows' current "
    "display resolution is set to a 16:9-shaped mode (e.g. 1280x720, "
    "1920x1080) rather than a true 4:3 mode (e.g. 640x480, 800x600, "
    "1024x768) -- check Settings > System > Display > Display resolution "
    "and switch to a 4:3 resolution if one is available; this app has no "
    "way to know a CRT's physical shape beyond what Windows reports.",
    NATIVE_WIDTH, NATIVE_HEIGHT, _native_ratio, DETECTED_ASPECT_RATIO)


# --- BOOT DISPLAY MODE: windowed vs fullscreen ------------------------------
# Decide this BEFORE creating the window, straight from the saved config file
# on disk. RetroDatabase's own settings load happens asynchronously on a
# background thread that doesn't even start until _background_subsystem_worker
# runs (well after this window already exists) -- waiting on it here would
# mean staring at a blank/wrong window for however long that load takes.
# Reading the same JSON file directly, synchronously, is the only way to have
# these two settings this early.
#
# Rule: if BOTH Kiosk Mode and Boot on Start are off, boot windowed -- that's
# the "just testing/tinkering at a desk" case. If either one is on (or both),
# boot fullscreen -- that's the "this is the cable box now" case, whether it
# got there via Kiosk Mode alone or via an actual shell-takeover reboot.
def _read_boot_fullscreen_pref():
    try:
        _cfg_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "retro_config.json")
        if os.path.exists(_cfg_path):
            import json as _json
            with open(_cfg_path, "r") as _f:
                _cfg = _json.load(_f).get("config", {})
            _kiosk = _cfg.get("kiosk_mode_enabled", False)
            _boot  = _cfg.get("start_on_boot", False)
            return bool(_kiosk or _boot)
    except Exception as _boot_pref_err:
        print(f"[BOOT TIMING] Could not read boot display prefs, defaulting to windowed: {_boot_pref_err}", flush=True)
    return False  # no config yet (first-ever launch) -- default windowed


def _compute_boot_window_size(native_w, native_h, aspect):
    """Windowed-boot size: comfortably smaller than the full screen, in the
    ballpark of how big the main settings menu (a fixed 1036x680 panel)
    reads on screen, so booting windowed looks like "a window" rather than
    borderless-fullscreen-in-disguise.

    Shaped to match the DETECTED screen aspect rather than always reusing a
    16:9 size -- a 4:3 CRT-era monitor can run as small as 1024x768 or even
    800x600, so blindly using a 16:9-sized window (e.g. 1280x720) could
    literally be wider than the whole screen. Using a 4:3-shaped target
    instead, then clamping to the actual screen with margin, guarantees it
    always fits AND still looks proportionate on that monitor.
    """
    if aspect == "4:3":
        target_w, target_h = 1024, 768
    else:
        target_w, target_h = 1280, 720

    # Leave a visible margin (don't let the "window" quietly become
    # edge-to-edge) and never exceed the real screen size, shrinking the
    # other dimension to match so the aspect ratio is preserved.
    max_w = max(320, native_w - 80)
    max_h = max(240, native_h - 80)
    if target_w > max_w or target_h > max_h:
        _scale = min(max_w / target_w, max_h / target_h)
        target_w = max(1, int(target_w * _scale))
        target_h = max(1, int(target_h * _scale))
    return target_w, target_h


def _finalize_window_mode(screen, *, windowed, client_w, client_h, pos_x, pos_y):
    """Settle the OS window immediately after a pygame.display.set_mode() that
    flips between NOFRAME (borderless fullscreen) and RESIZABLE (windowed).

    Consolidates what used to be three near-identical SetWindowPos blocks
    (minimize, restore, Kiosk-fullscreen) and fixes the two recurring
    minimize glitches they shared on Windows:

      * "sides missing" (edges clipped) — set_mode sizes the GL *client* to
        client_w x client_h, but calling SetWindowPos with SWP_NOSIZE |
        SWP_FRAMECHANGED recomputes the *new* frame against the RETAINED outer
        size. When the style just changed NOFRAME->RESIZABLE (WS_POPUP ->
        WS_OVERLAPPEDWINDOW), that shrinks the client under the freshly-added
        title bar/borders, so the GL viewport (still client_w x client_h) spills
        past the real client and the right/bottom edges fall off. Here we
        instead compute the correct OUTER size for the wanted client via
        AdjustWindowRectExForDpi (DPI-aware; we run Per-Monitor v2) and pass a
        real size with NO SWP_NOSIZE, so client == client_w x client_h exactly.

      * "black flash / lag" — set_mode leaves a blank surface until the next
        main-loop flip; while Windows finishes the synchronous frame change that
        gap shows as a black (often partially-framed) window. We pump the
        frame-change messages and present one clean frame right here, mirroring
        the boot path, so the transition reads as a clean cut instead of a stall.
    """
    if sys.platform == "win32":
        try:
            win_hwnd = pygame.display.get_wm_info().get("window")
            if win_hwnd:
                SWP_NOZORDER     = 0x0004
                SWP_FRAMECHANGED = 0x0020
                SWP_SHOWWINDOW   = 0x0040
                GWL_STYLE, GWL_EXSTYLE = -16, -20

                out_w, out_h = client_w, client_h
                if windowed:
                    # Grow the requested client rect out to the full window rect
                    # so borders/title bar sit OUTSIDE client_w x client_h.
                    class _RECT(ctypes.Structure):
                        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
                    rect = _RECT(0, 0, int(client_w), int(client_h))
                    style   = ctypes.windll.user32.GetWindowLongW(win_hwnd, GWL_STYLE)
                    exstyle = ctypes.windll.user32.GetWindowLongW(win_hwnd, GWL_EXSTYLE)
                    dpi = 96
                    try:
                        dpi = ctypes.windll.user32.GetDpiForWindow(win_hwnd) or 96
                    except Exception:
                        dpi = 96
                    _ok = False
                    try:
                        _ok = bool(ctypes.windll.user32.AdjustWindowRectExForDpi(
                            ctypes.byref(rect), ctypes.c_ulong(style), False,
                            ctypes.c_ulong(exstyle), ctypes.c_uint(dpi)))
                    except Exception:
                        _ok = False
                    if not _ok:
                        # Older Windows without the ForDpi variant.
                        try:
                            _ok = bool(ctypes.windll.user32.AdjustWindowRectEx(
                                ctypes.byref(rect), ctypes.c_ulong(style), False,
                                ctypes.c_ulong(exstyle)))
                        except Exception:
                            _ok = False
                    if _ok:
                        out_w = rect.right - rect.left
                        out_h = rect.bottom - rect.top

                ctypes.windll.user32.SetWindowPos(
                    win_hwnd, 0, int(pos_x), int(pos_y), int(out_w), int(out_h),
                    SWP_NOZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW)
        except Exception as pos_err:
            print(f"[WINDOW] Could not finalize window mode: {pos_err}")

    # Present one clean frame right now so the mode switch doesn't expose the
    # blank/garbage surface set_mode just created before the next render. Pump
    # first so Windows processes the frame-change (WM_NCCALCSIZE/WM_SIZE) before
    # we blit, and again after so the flip is actually composited.
    try:
        pygame.event.pump()
        screen.fill((0, 0, 0))
        pygame.display.flip()
        pygame.event.pump()
    except Exception as present_err:
        print(f"[WINDOW] Could not present frame after mode switch: {present_err}")


_BOOT_FULLSCREEN = _read_boot_fullscreen_pref()
print(f"[BOOT TIMING] Boot display mode: {'FULLSCREEN' if _BOOT_FULLSCREEN else 'WINDOWED'} "
      f"(kiosk_mode_enabled/start_on_boot based). at", time.time(), flush=True)

print("[BOOT TIMING] Display info retrieved. Creating boot window... at", time.time(), flush=True)

# FIXED BOOT LAG: Use NOFRAME windowed mode first for instant display, then switch to fullscreen
if _BOOT_FULLSCREEN:
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.NOFRAME)
else:
    WINDOW_WIDTH, WINDOW_HEIGHT = _compute_boot_window_size(NATIVE_WIDTH, NATIVE_HEIGHT, DETECTED_ASPECT_RATIO)
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.RESIZABLE)
pygame.display.set_caption("Retro TV Emulator")

# Present a blank frame immediately so there is SOMETHING on screen (a real
# window instead of nothing) before the potentially-slow mixer init below.
try:
    screen.fill((0, 0, 0))
    pygame.display.flip()
except Exception as _first_paint_err:
    print(f"[BOOT TIMING] Initial blank-frame present failed: {_first_paint_err}", flush=True)

# --- AUDIO MIXER INIT ---
# Moved here (after the window exists and a first frame is on screen) so a
# slow-to-wake audio device -- common right after Windows boot -- delays
# only the audio, not the very first thing the user sees. See the NOTE left
# at the old call site above for the full story.
try:
    # 44100Hz frequency, 16-bit signed audio, 2 stereo channels, 2048 buffer allocation size
    pygame.mixer.pre_init(44100, -16, 2, 2048)
    pygame.mixer.init()
    print("[BOOT TIMING] High-Fidelity Audio mixer initialized successfully with secure buffer allocations. at", time.time(), flush=True)
except Exception as e:
    try:
        pygame.mixer.init(buffer=2048)
        print("[BOOT TIMING] Audio mixer fallback triggered successfully with secure buffer allocations.")
    except Exception as fallback_err:
        print(f"[BOOT TIMING] Hardware Audio mixer completely blocked: {fallback_err}")

# --- APPLICATION ICON (desktop + taskbar) ---
def resource_path(rel_path):
    try:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, rel_path)
    except Exception as e:
        log.warning("resource_path: _MEIPASS lookup failed for %s: %s", rel_path, e)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_path)


# --- 4:3 TEST MODE: WHOLE-SCREEN CRT-BORDER OVERLAY ---
# Same border artwork used for the PSX / home-console CRT bezel
# (see game_deck.py's border_available_for_console / _load_border_surface),
# but composited over the ENTIRE window rather than just a libretro game
# frame, so every channel (video, static, menu, DVD, screensaver, etc.)
# gets the bezel look while 4:3 Test Mode + Border are both on. Lives at
# main/images/border/ -- same folder convention as loading/screensaver art
# (see resource_path usage elsewhere: main/images/<subfolder>/<file>). Looks
# for border.png first, then falls back to the first image file found in
# that folder, exactly like game_deck._load_border_surface's per-console
# fallback, so this degrades the same way (no crash, no border) if the
# asset is renamed or briefly missing.
_tv_border_cache = {"key": None, "surf": None}

def _load_full_tv_border_surface():
    """Load & cache the whole-screen 4:3 border overlay image. Returns a
    pygame Surface (with alpha) or None if no usable image is found. Cached
    by (path, mtime) so this only re-reads/re-decodes the file when it
    actually changes on disk, not every frame."""
    border_dir = resource_path(os.path.join("main", "images", "border"))
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
                        path = os.path.join(border_dir, f)
                        break
            except Exception as e:
                log.warning("_load_full_tv_border_surface: could not list %s: %s", border_dir, e)

    if path is None:
        _tv_border_cache["key"] = None
        _tv_border_cache["surf"] = None
        return None

    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0
    cache_key = (path, mtime)
    if cache_key == _tv_border_cache["key"] and _tv_border_cache["surf"] is not None:
        return _tv_border_cache["surf"]

    try:
        raw_surf = pygame.image.load(path).convert_alpha()
    except Exception as e:
        print(f"[4:3 BORDER] Could not load border image '{path}': {e}")
        _tv_border_cache["key"] = None
        _tv_border_cache["surf"] = None
        return None

    _tv_border_cache["key"] = cache_key
    _tv_border_cache["surf"] = raw_surf
    return raw_surf


# Cutout auto-detection for the whole-screen 4:3 border, cached alongside the
# border surface. Same flood-fill idea as GameDeck._detect_border_cutout_frac:
# transparent pixels that can't be reached from the image edges form the
# enclosed "screen hole" the artist punched out; its bounding box (as fractions
# of the image) tells us where content should be drawn so it lines up with the
# bezel opening. (0,0,1,1) means "no enclosed hole found" -> treat the whole
# image as the screen (no inset), which keeps a plain full-frame border safe.
_tv_border_cutout_cache = {"key": None, "frac": None}
_TV_BORDER_DEFAULT_CUTOUT = (0.0, 0.0, 1.0, 1.0)

def _full_tv_border_cutout_frac(border_surf):
    """Return (x_frac, y_frac, w_frac, h_frac) of the border image's enclosed
    transparent screen cutout, or (0,0,1,1) if none is found. Cached by the
    surface's identity so the (heavy) flood fill only runs when the border
    image actually changes."""
    key = id(border_surf)
    if _tv_border_cutout_cache["key"] == key and _tv_border_cutout_cache["frac"] is not None:
        return _tv_border_cutout_cache["frac"]
    frac = _TV_BORDER_DEFAULT_CUTOUT
    try:
        import numpy as np
        w, h = border_surf.get_size()
        alpha = pygame.surfarray.array_alpha(border_surf)   # shape (w, h)
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
        if interior.sum() >= (w * h * 0.005):
            # Keep only the largest connected enclosed region -- stray
            # transparent padding elsewhere shouldn't stretch the detected rect.
            comp_visited = np.zeros_like(interior, dtype=bool)
            ixs, iys = np.nonzero(interior)
            best = None; best_size = 0
            for sx, sy in zip(ixs.tolist(), iys.tolist()):
                if comp_visited[sx, sy]:
                    continue
                cstack = [(sx, sy)]; comp_visited[sx, sy] = True
                cxs = [sx]; cys = [sy]
                while cstack:
                    x, y = cstack.pop()
                    if x > 0 and interior[x - 1, y] and not comp_visited[x - 1, y]:
                        comp_visited[x - 1, y] = True; cstack.append((x - 1, y)); cxs.append(x - 1); cys.append(y)
                    if x < w - 1 and interior[x + 1, y] and not comp_visited[x + 1, y]:
                        comp_visited[x + 1, y] = True; cstack.append((x + 1, y)); cxs.append(x + 1); cys.append(y)
                    if y > 0 and interior[x, y - 1] and not comp_visited[x, y - 1]:
                        comp_visited[x, y - 1] = True; cstack.append((x, y - 1)); cxs.append(x); cys.append(y - 1)
                    if y < h - 1 and interior[x, y + 1] and not comp_visited[x, y + 1]:
                        comp_visited[x, y + 1] = True; cstack.append((x, y + 1)); cxs.append(x); cys.append(y + 1)
                if len(cxs) > best_size:
                    best_size = len(cxs); best = (cxs, cys)
            if best is not None:
                xs, ys = best
                minx, maxx = min(xs), max(xs)
                miny, maxy = min(ys), max(ys)
                frac = (minx / w, miny / h, (maxx - minx + 1) / w, (maxy - miny + 1) / h)
                print(f"[4:3 BORDER] Cutout auto-detected: {frac}")
    except Exception as e:
        log.warning("_full_tv_border_cutout_frac: detect failed, using full-image cutout: %s", e)
        frac = _TV_BORDER_DEFAULT_CUTOUT
    _tv_border_cutout_cache["key"] = key
    _tv_border_cutout_cache["frac"] = frac
    return frac


def _full_tv_43_outer_rect(win_w, win_h):
    """The centered 4:3 letterbox region within a window. This is the SAME
    math as the aspect_ratio=='4:3' branch in PART 20's content-rect block,
    factored out here so the border overlay and the content rect are computed
    from one place and can never drift apart."""
    target_width = int(win_h * (4.0 / 3.0))
    if target_width > win_w:
        target_width = win_w
        target_height = int(win_w * (3.0 / 4.0))
        bar_x = 0
        bar_y = (win_h - target_height) // 2
    else:
        target_height = win_h
        bar_x = (win_w - target_width) // 2
        bar_y = 0
    return pygame.Rect(bar_x, bar_y, target_width, target_height)


def _full_tv_border_geometry(win_w, win_h):
    """Whole-screen 4:3 test-border geometry, or None when it shouldn't draw
    (4:3 test mode off, Border off, not currently in 4:3, or no border image).

    Returns (outer_rect, border_draw_rect, inner_cutout_rect):
      - outer_rect        : the centered 4:3 letterbox region (black bars fill
                            the rest of the window).
      - border_draw_rect  : the border image scaled to fit outer_rect while
                            preserving the image's own aspect ratio, centered
                            -- identical fit rule to the per-console game
                            bezels (see _libretro_blit_bordered in game_deck).
      - inner_cutout_rect : the detected transparent screen hole inside
                            border_draw_rect; content is drawn here so it lines
                            up with the bezel opening instead of sitting behind
                            a stretched border.
    """
    if db is None:
        return None
    if not db.config.get("fake_43_test_mode_enabled", False):
        return None
    if not db.config.get("fake_43_border_enabled", True):
        return None
    if db.config.get("aspect_ratio", "16:9") != "4:3":
        return None
    border_surf = _load_full_tv_border_surface()
    if border_surf is None:
        return None
    outer = _full_tv_43_outer_rect(win_w, win_h)
    bw, bh = border_surf.get_size()
    scale = min(outer.width / max(bw, 1), outer.height / max(bh, 1))
    draw_bw = max(1, int(bw * scale))
    draw_bh = max(1, int(bh * scale))
    ox = outer.x + (outer.width - draw_bw) // 2
    oy = outer.y + (outer.height - draw_bh) // 2
    border_rect = pygame.Rect(ox, oy, draw_bw, draw_bh)
    rx, ry, rw, rh = _full_tv_border_cutout_frac(border_surf)
    inner = pygame.Rect(
        ox + int(draw_bw * rx),
        oy + int(draw_bh * ry),
        max(1, int(draw_bw * rw)),
        max(1, int(draw_bh * rh)),
    )
    return outer, border_rect, inner


def _compute_content_rect(win_w, win_h):
    """The on-screen region the main viewport's content occupies:
      - 16:9 / true-4:3 : the whole window.
      - 4:3 Test Mode, border ON  : the TV border's detected screen cutout.
      - 4:3 Test Mode, border OFF : the centered 4:3 letterbox region.
    This is the single source of truth for that rect, shared by the main
    render loop and the DVD leave-frame capture so a paused DVD returns at the
    exact size/position it will be redrawn at (the paused-frame path blits
    without rescaling, so a size mismatch would misalign it)."""
    if db is not None and db.config.get("aspect_ratio", "16:9") == "4:3":
        geom = _full_tv_border_geometry(win_w, win_h)
        if geom is not None:
            return geom[2]  # inner cutout
        return _full_tv_43_outer_rect(win_w, win_h)
    return pygame.Rect(0, 0, win_w, win_h)


def _overlay_target(screen):
    """Surface that popup overlays (settings menu, controls splash, quit/restart
    prompts) should render into. In 4:3 Test Mode this is a subsurface confined
    to the 4:3 content region (border cutout / letterbox), so the menus size and
    center themselves within the 4:3 area and sit inside the TV border instead
    of centering across -- and spilling past -- the full 16:9 window.

    Every overlay derives all of its geometry from surface.get_size() and is
    keyboard-driven (no absolute mouse hit-testing), so a subsurface -- whose
    (0,0) maps to the content region's on-screen origin -- is a safe, drop-in
    swap for `screen`. In 16:9 / true-4:3 modes content_rect IS the whole
    window, so this returns the real screen and behavior is unchanged there."""
    cr = _compute_content_rect(*screen.get_size())
    if cr.width >= screen.get_width() and cr.height >= screen.get_height():
        return screen
    try:
        return screen.subsurface(cr)
    except Exception:
        return screen


def _draw_full_tv_border_overlay(screen):
    """Composite the whole-screen border art on top of whatever this frame
    already drew, whenever 4:3 Test Mode + Border are both on. Call this as
    the very last thing before pygame.display.flip() so it overlays every
    channel/menu/screensaver uniformly, same as real TV bezel art would.

    The border is fitted into the 4:3 letterbox region (NOT stretched across
    the whole 16:9 window) exactly the way per-console bezels are fitted to
    the game screen -- see _full_tv_border_geometry. PART 20 has already inset
    this frame's content into the same border's detected cutout, so the two
    line up: content shows through the transparent hole and the bezel art
    frames it, instead of a full-window border stretched over a 16:9 picture."""
    try:
        geom = _full_tv_border_geometry(*screen.get_size())
        if geom is None:
            return
        _outer, border_rect, _inner = geom
        border_surf = _load_full_tv_border_surface()
        if border_surf is None:
            return
        scaled = pygame.transform.smoothscale(border_surf, (border_rect.width, border_rect.height))
        screen.blit(scaled, (border_rect.x, border_rect.y))
    except Exception as e:
        log.warning("_draw_full_tv_border_overlay: draw failed: %s", e)


_STARTUP_REG_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon"
_STARTUP_REG_NAME = "Shell"
_DEFAULT_SHELL = "explorer.exe"

# Legacy location used by older builds of this app (a plain Run-key entry,
# fired alongside everything else at login and staggered behind a 30s sleep
# to dodge startup contention). Kept here only so _set_start_on_boot can
# clean up a stale entry left behind by an upgrade -- the Run-key approach
# is no longer used.
_LEGACY_STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LEGACY_STARTUP_REG_NAME = "RetroTVEmulator"


def _get_startup_command():
    """Command line written into the per-user Winlogon Shell value.

    This makes the app itself the user's Windows shell -- it launches the
    instant that user's session starts, in place of explorer.exe, rather
    than racing explorer/antivirus/OneDrive/tray icons as just another
    Run-key entry. There's nothing to wait out or dodge contention with,
    since nothing else is starting at the same time -- so no delay wrapper
    is needed (or wanted: a delay here would just be a black screen with no
    shell running yet).

    Winlogon reads this value as a literal command line it spawns directly,
    so no Start-Process/PowerShell wrapper is needed either -- just the
    quoted path (plus interpreter, for a raw .py dev/test run).

    The trailing --as-shell flag is how a running instance tells whether
    IT PERSONALLY was launched as the OS shell, as opposed to just having
    the start_on_boot setting turned on in config. The setting doesn't take
    over until the next reboot re-launches the app via this exact command
    line -- see the shell_takeover_active read of sys.argv near app_state
    init, and the ESC-minimize gate further down that keys off of it.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --as-shell'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --as-shell'


def _launch_explorer_fallback():
    """Start a normal desktop shell before this app exits.

    When this app IS the Windows shell, closing it ends the session --
    Windows logs the user out the moment the shell process exits, since no
    shell is left running. Spawning explorer.exe first hands the session a
    normal shell to fall back to, so quitting the app lands on an ordinary
    Windows desktop instead of a logoff or black screen. This only matters
    when shell-replacement is actually active; harmless no-op otherwise
    beyond briefly starting an extra explorer.exe.
    """
    if os.name != "nt":
        return
    try:
        subprocess.Popen(["explorer.exe"])
    except Exception as e:
        print(f"[STARTUP] Could not launch explorer.exe fallback: {e}")


def _set_start_on_boot(enabled):
    """Add/remove this app as the CURRENT USER's Windows shell (HKCU
    ...\\Winlogon\\Shell), so it's the first and only thing that loads after
    that user logs in -- no desktop, no taskbar, no explorer, matching a
    dedicated "cable box" PC. HKCU rather than HKLM deliberately: it needs
    no admin rights and only affects the account that turned it on -- the
    right scope for a single recycled PC running as a cable box under one
    Windows user. Returns True on success so the caller only persists the
    config flag once the actual registry change stuck, rather than letting
    the on-screen toggle claim a state that isn't real."""
    if os.name != "nt":
        print("[STARTUP] Not running on Windows — start-on-boot toggle has no effect here.")
        return False
    try:
        import winreg
        # Clean up any stale Run-key entry from older builds so it can't
        # double-launch the app alongside the new shell-replacement launch.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LEGACY_STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as legacy_key:
                try:
                    winreg.DeleteValue(legacy_key, _LEGACY_STARTUP_REG_NAME)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _get_startup_command())
            else:
                # Restore the normal desktop shell rather than deleting the
                # value -- HKCU's Shell value overrides the system default,
                # so leaving it unset could vary by machine, while writing
                # "explorer.exe" back guarantees a normal login every time.
                winreg.SetValueEx(key, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _DEFAULT_SHELL)
        return True
    except Exception as e:
        print(f"[STARTUP] Failed to {'enable' if enabled else 'disable'} start-on-boot: {e}")
        return False


def _mark_settings_dirty():
    """Record that a setting changed, instead of writing it to disk immediately.

    db.save_settings() does a synchronous full JSON serialize + file write of
    the entire config AND every channel's schedules/playback logs. Calling it
    on every single tick of a held slider (volume, brightness, hue, etc. — all
    on a 180ms repeat) was firing that full disk write ~5x/second for as long
    as a key was held. On a fast dev-machine SSD that's invisible; on an older
    PC (slower disk, possibly antivirus scanning every write) it blocks the
    same thread pygame renders on, which is exactly what a "freeze while
    holding volume/brightness" looks like. The in-memory value already takes
    effect immediately either way — this only changes *when it hits disk*.
    """
    now = pygame.time.get_ticks()
    if not app_state.get("_settings_dirty", False):
        app_state["_settings_dirty_first"] = now
    app_state["_settings_dirty"] = True
    app_state["_settings_dirty_since"] = now


def _flush_settings_if_dirty(force=False):
    """Write pending settings to disk once the user has paused (quiet period),
    or after a hard cap even if they're still actively holding the slider, or
    unconditionally when force=True (call this before every real app exit so
    a change made right before closing is never lost)."""
    if db is None or not app_state.get("_settings_dirty", False):
        return
    now = pygame.time.get_ticks()
    quiet_elapsed = now - app_state.get("_settings_dirty_since", now)
    total_elapsed = now - app_state.get("_settings_dirty_first", now)
    if force or quiet_elapsed > 400 or total_elapsed > 1500:
        db.save_settings_async(force=force)
        app_state["_settings_dirty"] = False


def _letterbox_fit(img_w, img_h, win_w, win_h):
    """Fit an img_w x img_h image into a win_w x win_h window, preserving the
    image's own aspect ratio (no stretching/distortion) and centering it with
    bars on whichever axis has leftover space. Returns (dest_w, dest_h, dest_x, dest_y).

    Used for the loading-screen artwork (authored at 16:9) so it pillarboxes
    cleanly instead of warping when the real window/TV is a different shape,
    e.g. 4:3.
    """
    if img_h <= 0 or win_h <= 0 or img_w <= 0 or win_w <= 0:
        return win_w, win_h, 0, 0
    img_aspect = img_w / img_h
    win_aspect = win_w / win_h
    if img_aspect > win_aspect:
        # Image is relatively wider than the window -> bars on top/bottom
        dest_w = win_w
        dest_h = max(1, int(win_w / img_aspect))
        dest_x = 0
        dest_y = (win_h - dest_h) // 2
    else:
        # Image is relatively taller/narrower than the window -> bars on sides
        dest_h = win_h
        dest_w = max(1, int(win_h * img_aspect))
        dest_x = (win_w - dest_w) // 2
        dest_y = 0
    return dest_w, dest_h, dest_x, dest_y

try:
    _icon_path = resource_path(os.path.join("main", "images", "loading", "icon.png"))
    if os.path.exists(_icon_path):
        _icon_surface = pygame.image.load(_icon_path)
        pygame.display.set_icon(_icon_surface)
        print("[BOOT TIMING] Window/taskbar icon set from icon.png")
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("retro.tv.emulator.app")
            except Exception as appid_err:
                print(f"[BOOT TIMING] Could not set AppUserModelID: {appid_err}")
    else:
        print("[BOOT TIMING] icon.png not found - skipping icon assignment.")
except Exception as icon_err:
    print(f"[BOOT TIMING] Icon load failed: {icon_err}")

clock = pygame.time.Clock()

# Module-level font cache — pygame.font.SysFont() is expensive; never call it
# inside the render loop. Use _cached_sys_font() for any per-frame text work.
_rt_font_cache = {}
def _cached_sys_font(name, size, bold=False):
    k = (name, size, bold)
    if k not in _rt_font_cache:
        _rt_font_cache[k] = pygame.font.SysFont(name, size, bold=bold)
    return _rt_font_cache[k]

# Last successfully-decoded video frame (raw VLC-native BGRA surface). Used as a
# bridge so switching channels / guide previews doesn't flash black for the
# ~1-1.5s VLC takes to buffer the first frame of the newly-loaded source.
_last_video_frame = None
def _store_last_video_frame(surf):
    global _last_video_frame
    try:
        _last_video_frame = surf.copy()  # detach from VLC's reused byte buffer
    except Exception as e:
        log.warning("_store_last_video_frame: surf.copy() failed: %s", e)
def _blit_last_video_frame(screen, x, y, w, h):
    """Scale-and-blit the last good video frame into the given rect.
    Returns True if a frame was drawn, False if none is cached yet."""
    if _last_video_frame is None:
        return False
    try:
        screen.blit(pygame.transform.smoothscale(_last_video_frame, (w, h)), (x, y))
        return True
    except Exception:
        return False
def _clear_last_video_frame():
    """Drop the cached bridge frame. Must be called any time the video SOURCE
    changes (live channel switch, or the TV Guide preview moving to a
    different highlighted channel) — otherwise the next buffering gap will
    use _blit_last_video_frame() to paper over the wait with a frame that
    belongs to whatever was playing before, which reads as a flash of the
    previous channel/preview before the new one appears."""
    global _last_video_frame
    _last_video_frame = None

def _arm_guide_preview_transition():
    """Call this any time the TV Guide's highlighted channel changes (moving
    the preview highlight up/down to a new row/channel).

    If transition_type == "static", arms app_state["guide_preview_static_until"]
    -- a DEDICATED timer, separate from channel_switch_black_until -- so the
    guide's small preview box shows a burst of static (visual+audio) when
    scrolling (see _ch04_shield_active below, which now checks both timers).

    This deliberately does NOT touch channel_switch_black_until. That timer
    is shared with the real channel-switch transition, including the
    full-screen black/static flash that plays over the whole screen the
    moment the TV Guide itself is opened (see the "INLINE LIVE TV GUIDE
    INTERRUPT PREVIEW DRIVER CHECK" / "RETRO UNIFIED BLACK/STATIC TRANSITION
    SHIELD GATE" blocks, which paint content_rect -- i.e. the entire screen
    -- while it's active). Arming THAT timer on every highlight move would
    replay that full-screen flash on every single scroll instead of confining
    the effect to the small preview box, which is exactly the bug this
    dedicated timer avoids.

    If transition_type == "black" (or anything else), this deliberately does
    NOTHING — leaving both timers untouched preserves the existing
    instant-cut preview behavior for that mode exactly as-is.
    """
    if db is not None and db.config.get("transition_type", "black") == "static":
        app_state["guide_preview_static_until"] = pygame.time.get_ticks() + 1000

# Per-CHANNEL guide-preview frame cache (channel key -> last decoded
# post-effects surface), separate from the single-slot bridge above. The
# bridge only ever remembers the ONE most recently seen frame, so scrolling
# the guide from channel to channel wipes it on every highlight move — VLC's
# genuine ~1-1.5s decode-buffer startup latency then means most previews just
# show the black/gray transition fill and never a real frame at all unless
# you pause on one channel for over a second. This cache remembers one frame
# per channel for the rest of the session, so re-highlighting any channel
# already seen once shows its last-known frame instantly while VLC re-buffers
# a fresh one underneath, instead of going black every single time.
_guide_channel_frame_cache = {}
def _store_guide_channel_frame(ch_key, surf):
    try:
        _guide_channel_frame_cache[str(ch_key).zfill(2)] = surf.copy()
    except Exception as e:
        log.warning("_store_guide_channel_frame: surf.copy() failed for ch%s: %s", ch_key, e)
def _blit_guide_channel_frame(ch_key, screen, x, y, w, h):
    """Scale-and-blit channel ch_key's last known preview frame. Returns True
    if a frame was drawn, False if this channel has never been cached yet."""
    surf = _guide_channel_frame_cache.get(str(ch_key).zfill(2))
    if surf is None:
        return False
    try:
        screen.blit(pygame.transform.smoothscale(surf, (w, h)), (x, y))
        return True
    except Exception:
        return False

def _clear_guide_channel_frame_cache():
    """Wipe every channel's cached guide-preview frame.

    _guide_channel_frame_cache is intentionally long-lived WITHIN a single
    guide session -- that's what lets scrolling back to an already-highlighted
    channel bridge instantly instead of going black again while VLC re-buffers
    (see _store_guide_channel_frame's docstring). But nothing ever cleared it
    BETWEEN sessions, so the fallback in the Part 22 preview renderer (used
    for the ~1-1.5s VLC startup gap right after the transition shield expires)
    could reach back and blit a frame from an entirely different visit to the
    guide -- possibly minutes or hours earlier, whatever that channel happened
    to be showing back then. That's the "frozen image of the last thing I was
    watching" symptom on returning to the guide: real content hadn't buffered
    yet, so the stale per-channel cache silently filled in instead of the
    guide honestly showing its buffering fill (matching how every other
    transition in the app already behaves). Called once per fresh entry into
    channel 04 (see change_channel(), alongside the existing
    loaded_preview_path_cache reset) so every guide session starts with a
    clean slate; a channel highlighted more than once THIS session still gets
    the smooth-scroll benefit exactly as before.
    """
    _guide_channel_frame_cache.clear()

def _get_video_effects(db):
    """Single source of truth for the CRT/video filter values read from saved
    config. Returns (brightness, contrast, color_sat, sharpness, tint, fx_active)
    where fx_active is True if any value differs from the neutral 50 default —
    i.e. whether it's worth running the (expensive) apply_video_effects pass.
    Collapses the identical 5-line read + 'any non-default?' check that was
    duplicated across every playback mode's render path."""
    brightness = db.config.get("brightness", 50) if db else 50
    contrast   = db.config.get("contrast",   50) if db else 50
    color_sat  = db.config.get("color",      50) if db else 50
    sharpness  = db.config.get("sharpness",  50) if db else 50
    tint       = db.config.get("tint",       50) if db else 50
    fx_active  = (brightness != 50 or contrast != 50 or color_sat != 50
                  or sharpness != 50 or tint != 50)
    return brightness, contrast, color_sat, sharpness, tint, fx_active

pygame_hwnd = pygame.display.get_wm_info().get("window")

print("[BOOT TIMING] Initial window created successfully! at", time.time(), flush=True)

# Center the boot window when starting windowed -- mirrors the centering the
# manual fullscreen-toggle does further down when switching TO windowed mode.
# Fullscreen boot doesn't need this: the NOFRAME window already covers the
# whole screen at (0,0).
if not _BOOT_FULLSCREEN and sys.platform == "win32" and pygame_hwnd:
    try:
        _boot_pos_x = max(0, (NATIVE_WIDTH - WINDOW_WIDTH) // 2)
        _boot_pos_y = max(0, (NATIVE_HEIGHT - WINDOW_HEIGHT) // 2)
        ctypes.windll.user32.SetWindowPos(pygame_hwnd, 0, _boot_pos_x, _boot_pos_y, 0, 0, 0x0001 | 0x0040)  # SWP_NOSIZE | SWP_SHOWWINDOW
    except Exception as _boot_center_err:
        print(f"[BOOT TIMING] Could not center windowed boot window: {_boot_center_err}", flush=True)

# --- HARDWARE APPLICATION WINDOW FOCUS LAYER LOCKS ---
if sys.platform == "win32" and pygame_hwnd:
    HWND_TOPMOST    = -1   
    HWND_NOTOPMOST  = -2   
    SWP_NOMOVE      = 0x0002
    SWP_NOSIZE      = 0x0001
    SWP_SHOWWINDOW  = 0x0040
    SW_RESTORE      = 9
    SW_SHOWMAXIMIZED = 3
    
    ctypes.windll.user32.AllowSetForegroundWindow(-1)
    pygame.time.wait(100)
    ctypes.windll.user32.SetWindowPos(pygame_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    ctypes.windll.user32.ShowWindow(pygame_hwnd, SW_RESTORE)
    ctypes.windll.user32.SetForegroundWindow(pygame_hwnd)
    ctypes.windll.user32.SetActiveWindow(pygame_hwnd)
    ctypes.windll.user32.BringWindowToTop(pygame_hwnd)
    ctypes.windll.user32.SetFocus(pygame_hwnd)
    pygame.time.wait(50)
    ctypes.windll.user32.SetWindowPos(pygame_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    
    print("[BOOT TIMING] Window focus acquired; shell layer restored above window. at", time.time(), flush=True)

# ── WM_APPCOMMAND DIAGNOSTIC / FORWARDING LAYER ──────────────────────────────
# WHY THIS EXISTS:
# The WH_KEYBOARD_LL hook above only sees messages that enter the standard
# keyboard input stack (WM_KEYDOWN/WM_SYSKEYDOWN/etc). Evidence from
# app_log.txt (2026-07-07 session) shows every other remote button --
# including ones the remote encodes as consumer keys like VOLUME_UP/DOWN and
# BROWSER_HOME -- shows up in that hook's log, suppressed or not. But during
# an 18-second window where the user's remote OK/Back buttons were pressed,
# the hook logged NOTHING AT ALL -- not even a "not suppressed" pass-through
# line. That is only possible if those two buttons never generate a
# WM_(SYS)KEY(UP/DOWN) message in the first place.
# The other common delivery path on Windows for HTPC/IR remotes is
# WM_APPCOMMAND, sent directly to the foreground window rather than through
# the keyboard input stack -- which a WH_KEYBOARD_LL hook structurally cannot
# see. This subclasses the pygame window's own WNDPROC so we can intercept
# WM_APPCOMMAND before Windows' default handling, exactly the way the LL hook
# intercepts WM_KEYDOWN before Windows' default handling.
# THIS BUILD IS DIAGNOSTIC-ONLY: it logs every WM_APPCOMMAND command ID that
# arrives (so we can find out what OK/Back actually send) but does not yet
# forward any of them to pygame, since we don't know the real IDs yet. Once
# the log gives us concrete IDs, mapping + suppression can be added the same
# way _SUPPRESSED_VKS/_VK_TO_SDL_SC work for the keyboard hook.
if sys.platform == "win32" and pygame_hwnd:
    _WM_APPCOMMAND   = 0x0319
    _GWLP_WNDPROC    = -4
    _FAPPCOMMAND_MASK = 0xF000

    # Command ID -> SDL scancode. Confirmed from app_log.txt 2026-07-07 23:08
    # session: this remote's FF/RW buttons send WM_APPCOMMAND (there is no
    # standard VK_MEDIA_FAST_FORWARD/VK_MEDIA_REWIND virtual key at all --
    # that's why the WH_KEYBOARD_LL hook never saw them). UPDATE 2026-07-08:
    # OK/Back are NOT VK_MEDIA_PLAY_PAUSE/PREV_TRACK as originally guessed
    # here -- testing with _HOOK_DEBUG on showed zero output for OK/Back on
    # BOTH this path and the keyboard hook. See the RAW INPUT DIAGNOSTIC
    # LAYER below for the current theory (unmapped Consumer Control HID
    # usage) and why it's the next thing being tested.
    # UPDATE 2026-07-18: app_log.txt from a live DVD-playback test (with the
    # new unconditional [APPCOMMAND DEBUG]/[DVD INPUT DEBUG] logging) shows
    # the FF/Rewind-SYMBOL buttons on this keyboard actually fire cmd_id 11
    # (APPCOMMAND_MEDIA_NEXTTRACK) and 12 (APPCOMMAND_MEDIA_PREVIOUSTRACK) --
    # NOT 49/50. This keyboard prints seek-style (>>/<<) icons on what
    # Windows classifies as plain track-skip buttons, a common hardware
    # quirk. Mapped straight to the same AUDIOFASTFORWARD/AUDIOREWIND
    # scancodes as 49/50 so they resolve to the same DVD action; 49/50 are
    # kept in case a different remote/keyboard does send the "correct" IDs.
    _APPCOMMAND_TO_SDL_SC = {
        11: 286,   # APPCOMMAND_MEDIA_NEXTTRACK     → SDL_SCANCODE_AUDIOFASTFORWARD (this hardware's "FF" button)
        12: 285,   # APPCOMMAND_MEDIA_PREVIOUSTRACK → SDL_SCANCODE_AUDIOREWIND      (this hardware's "RW" button)
        49: 286,   # APPCOMMAND_MEDIA_FASTFORWARD   → SDL_SCANCODE_AUDIOFASTFORWARD
        50: 285,   # APPCOMMAND_MEDIA_REWIND        → SDL_SCANCODE_AUDIOREWIND
    }

    # WINFUNCTYPE signature must match the 64-bit Windows WNDPROC ABI exactly,
    # same reasoning as _KbdHookProcType above.
    _WndProcType = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,   # LRESULT (signed, pointer-sized)
        ctypes.c_void_p,    # HWND
        ctypes.c_uint,      # Msg
        ctypes.c_size_t,    # WPARAM (unsigned, pointer-sized)
        ctypes.c_ssize_t,   # LPARAM (signed, pointer-sized)
    )

    ctypes.windll.user32.GetWindowLongPtrW.restype  = ctypes.c_void_p
    ctypes.windll.user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ctypes.windll.user32.SetWindowLongPtrW.restype  = ctypes.c_void_p
    ctypes.windll.user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    ctypes.windll.user32.CallWindowProcW.restype  = ctypes.c_ssize_t
    ctypes.windll.user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,   # lpPrevWndFunc
        ctypes.c_void_p,   # hWnd
        ctypes.c_uint,     # Msg
        ctypes.c_size_t,   # wParam
        ctypes.c_ssize_t,  # lParam
    ]

    # ── RAW INPUT DIAGNOSTIC LAYER (WM_INPUT) ────────────────────────────────
    # WHY THIS EXISTS: real-world testing (2026-07-08 session) showed OK/Back
    # produce ZERO log output on BOTH the WH_KEYBOARD_LL hook above (no
    # WM_(SYS)KEY(UP/DOWN) at all) and WM_APPCOMMAND below (no message at
    # all either) -- confirmed with _HOOK_DEBUG on and "filler" key presses
    # around them proving the capture screen itself wasn't frozen. If
    # neither of those ever fires, Windows never translated the press into
    # a VK or a legacy app-command in the first place -- there's nothing
    # for either hook to see. The remaining possibility: this remote sends
    # those buttons as raw HID "Consumer Control" (or vendor-specific)
    # usages that aren't on Windows' short built-in translation list, so
    # they're invisible to both hooks above but ARE visible via the Raw
    # Input API, which delivers the actual HID report bytes before any of
    # that translation/filtering happens.
    # THIS BUILD IS DIAGNOSTIC-ONLY: logs the raw bytes for any keyboard-page
    # or consumer-control-page HID event; does not forward anything to
    # pygame yet, since we don't know what OK/Back's raw report looks like.
    #
    # DEBUG SWITCH: this fires on EVERY keystroke and EVERY HID report the
    # whole app sees, not just the remote's -- it was left permanently on
    # after the OK/Back investigation and was the second-biggest source of
    # log spam in the app (thousands of lines a session, each a synchronous
    # flush=True disk write). MUST stay False in normal operation; only
    # flip on briefly on a dev machine while diagnosing a new remote/button,
    # then turn it back off -- see the identical warning on _HOOK_DEBUG
    # above for why leaving a per-event flush=True print on is dangerous.
    _RAWINPUT_DEBUG = True  # TEMP: flipped on to capture what OK/Back actually
    # send as raw HID reports -- both are invisible to WH_KEYBOARD_LL and
    # WM_APPCOMMAND (see the RAW INPUT DIAGNOSTIC LAYER comment above), so
    # this is the only path left that can see them at all. Turn this back to
    # False once the OK/Back HID bytes are captured -- see the warning above
    # about per-keystroke flush=True prints being able to trip
    # LowLevelHooksTimeout and silently kill the WH_KEYBOARD_LL hook if left
    # on during normal use.
    _RID_INPUT = 0x10000003
    _RIM_TYPEKEYBOARD = 1
    _RIM_TYPEHID = 2

    class _RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", ctypes.c_ushort),
                    ("usUsage", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong),
                    ("hwndTarget", ctypes.c_void_p)]

    class _RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", ctypes.c_ulong),
                    ("dwSize", ctypes.c_ulong),
                    ("hDevice", ctypes.c_void_p),
                    ("wParam", ctypes.c_size_t)]

    class _RAWKEYBOARD(ctypes.Structure):
        _fields_ = [("MakeCode", ctypes.c_ushort),
                    ("Flags", ctypes.c_ushort),
                    ("Reserved", ctypes.c_ushort),
                    ("VKey", ctypes.c_ushort),
                    ("Message", ctypes.c_ulong),
                    ("ExtraInformation", ctypes.c_ulong)]

    def _handle_wm_input(lParam):
        try:
            size = ctypes.c_uint(0)
            ctypes.windll.user32.GetRawInputData(
                ctypes.c_void_p(lParam), _RID_INPUT, None,
                ctypes.byref(size), ctypes.sizeof(_RAWINPUTHEADER))
            if size.value == 0:
                return
            buf = ctypes.create_string_buffer(size.value)
            got = ctypes.windll.user32.GetRawInputData(
                ctypes.c_void_p(lParam), _RID_INPUT, buf,
                ctypes.byref(size), ctypes.sizeof(_RAWINPUTHEADER))
            if got != size.value:
                return
            header = _RAWINPUTHEADER.from_buffer_copy(buf, 0)
            hdr_sz = ctypes.sizeof(_RAWINPUTHEADER)
            if header.dwType == _RIM_TYPEKEYBOARD:
                kb = _RAWKEYBOARD.from_buffer_copy(buf, hdr_sz)
                if _RAWINPUT_DEBUG:
                    print(f"[RAWINPUT DEBUG] KEYBOARD MakeCode={kb.MakeCode} Flags={kb.Flags} "
                          f"VKey=0x{kb.VKey:02X} Message=0x{kb.Message:04X} "
                          f"dev={header.hDevice}", flush=True)
            elif header.dwType == _RIM_TYPEHID:
                dwSizeHid = ctypes.c_ulong.from_buffer_copy(buf, hdr_sz).value
                dwCount = ctypes.c_ulong.from_buffer_copy(buf, hdr_sz + 4).value
                raw_off = hdr_sz + 8
                raw_bytes = buf.raw[raw_off: raw_off + dwSizeHid * dwCount]
                if _RAWINPUT_DEBUG:
                    print(f"[RAWINPUT DEBUG] HID dwSizeHid={dwSizeHid} dwCount={dwCount} "
                          f"bytes={raw_bytes.hex()} dev={header.hDevice}", flush=True)
        except Exception as _ri_err:
            print(f"[RAWINPUT] parse error: {_ri_err}", flush=True)

    try:
        _raw_devices = (_RAWINPUTDEVICE * 2)(
            _RAWINPUTDEVICE(0x01, 0x06, 0, pygame_hwnd),  # Generic Desktop / Keyboard
            _RAWINPUTDEVICE(0x0C, 0x01, 0, pygame_hwnd),  # Consumer Control page
        )
        if ctypes.windll.user32.RegisterRawInputDevices(
                _raw_devices, 2, ctypes.sizeof(_RAWINPUTDEVICE)):
            print("[BOOT] Raw Input diagnostic listener registered (keyboard + consumer control).", flush=True)
        else:
            print("[RAWINPUT] RegisterRawInputDevices failed.", flush=True)
    except Exception as _reg_err:
        print(f"[RAWINPUT] registration error: {_reg_err}", flush=True)
    # ── END RAW INPUT DIAGNOSTIC LAYER ────────────────��──────────────────────

    _orig_wndproc_ptr = None  # set below; pinned raw address of pygame/SDL's own WNDPROC

    def _app_wndproc(hwnd, msg, wParam, lParam):
        try:
            if msg == 0x00FF:  # WM_INPUT
                _handle_wm_input(lParam)
                # fall through to default processing below -- WM_INPUT must
                # still reach DefWindowProc for Windows to clean up its
                # internal buffer for this message
            elif msg == _WM_APPCOMMAND:
                lp = lParam & 0xFFFFFFFF
                hiword = (lp >> 16) & 0xFFFF
                cmd_id = hiword & ~_FAPPCOMMAND_MASK
                device = hiword & _FAPPCOMMAND_MASK
                # NOTE: intentionally no per-message flush=True print here.
                # This subclassed WNDPROC runs on every WM_APPCOMMAND, and a
                # synchronous disk flush on the window-message thread carries
                # the same class of risk that took down the WH_KEYBOARD_LL
                # hook (see _HOOK_DEBUG above) -- keep this path lean.
                # Unlike _HOOK_DEBUG (gates the per-KEYSTROKE LL hook log,
                # genuinely spammy), WM_APPCOMMAND only fires on real
                # app-command button presses -- rare enough to log
                # unconditionally so a DVD FF/Rewind report always has this
                # evidence without needing a special debug build first.
                print(f"[APPCOMMAND DEBUG] cmd_id={cmd_id} device_flag=0x{device:04X} "
                      f"raw_lParam=0x{lp:08X}", flush=False)
                sc = _APPCOMMAND_TO_SDL_SC.get(cmd_id)
                if not sc:
                    # Unmapped command ID -- don't drop it silently like before.
                    # Fabricate a stable pseudo-scancode from the cmd_id itself
                    # (offset well clear of real SDL scancodes 0-283 and the
                    # keyboard-hook VK fallback range below) so any future
                    # remote button on this delivery path still shows up in
                    # capture as a bindable "#NNN" key instead of vanishing.
                    sc = 500 + cmd_id
                pk = sc | _SDLK_MASK
                pygame.event.post(pygame.event.Event(
                    pygame.KEYDOWN, key=pk, scancode=sc, mod=0, unicode=""))
                pygame.event.post(pygame.event.Event(
                    pygame.KEYUP, key=pk, scancode=sc, mod=0, unicode=""))
                return 1  # tell Windows we handled it; suppress default action
        except Exception as _wndproc_err:
            print(f"[APPCOMMAND] wndproc error (message passed through): {_wndproc_err}", flush=True)
        return ctypes.windll.user32.CallWindowProcW(_orig_wndproc_ptr, hwnd, msg, wParam, lParam)

    try:
        _APPCMD_WNDPROC_PTR = _WndProcType(_app_wndproc)  # pinned — must not be GC'd
        _orig_wndproc_ptr = ctypes.windll.user32.SetWindowLongPtrW(
            pygame_hwnd, _GWLP_WNDPROC, ctypes.cast(_APPCMD_WNDPROC_PTR, ctypes.c_void_p))
        if _orig_wndproc_ptr:
            print("[BOOT] WM_APPCOMMAND diagnostic subclass installed.", flush=True)
        else:
            print("[APPCOMMAND] SetWindowLongPtrW returned NULL — subclass inactive.", flush=True)
    except Exception as _subclass_err:
        print(f"[APPCOMMAND] subclass install failed: {_subclass_err}", flush=True)
# ── END WM_APPCOMMAND DIAGNOSTIC LAYER ───────────────────────────────────────

# ==============================================================================
# PART 1b OF 28: ZERO-FRAME GRAPHICS ALIGNMENT
# ==============================================================================

pygame.mouse.set_visible(False)

base_dir = os.path.dirname(os.path.abspath(__file__))
img_path = os.path.join(base_dir, "main", "images", "loading", "retro_tv_loading.png")

if os.path.exists(img_path):
    loading_bg_surface = pygame.image.load(img_path).convert()
    _lbg_w, _lbg_h = loading_bg_surface.get_size()
    _lbg_dest_w, _lbg_dest_h, _lbg_dest_x, _lbg_dest_y = _letterbox_fit(
        _lbg_w, _lbg_h, WINDOW_WIDTH, WINDOW_HEIGHT)
    screen.fill((1, 1, 1))  # bar color — not pure black, which is reserved as a transparency key elsewhere
    screen.blit(pygame.transform.scale(loading_bg_surface, (_lbg_dest_w, _lbg_dest_h)),
                (_lbg_dest_x, _lbg_dest_y))

    # SCALE-AWARE DIMENSIONS: matches the same art_scale approach used by the
    # main loading loop (PART 19a/19b) further down, so this frame-zero
    # preload frame lines up exactly with the bar/text position the real
    # loading loop then takes over drawing -- no jump/resize flash between
    # them, and it scales correctly at any window size (not just full-screen).
    _init_art_scale = _lbg_dest_w / _lbg_w if _lbg_w else 1.0
    init_w = max(60, int(160 * _init_art_scale))
    init_h = max(3, int(10 * _init_art_scale))
    _init_offset_left = int(14 * _init_art_scale)
    _init_offset_up = int(6 * _init_art_scale)
    
    # FIXED COORDINATES: Shifted left and pulled up to match the main loop.
    # Anchored to the letterboxed picture area (not the raw window) so the line stays aligned
    # with the artwork itself even when it's pillarboxed on a non-16:9 screen.
    init_x = (_lbg_dest_x + (_lbg_dest_w - init_w) // 2) - _init_offset_left
    init_y = _lbg_dest_y + int(_lbg_dest_h * 0.72) - _init_offset_up
    
    # Paint the static base container rail tracker slot matching your green paintbrush stroke
    pygame.draw.rect(screen, (20, 30, 20), (init_x, init_y, init_w, init_h), border_radius=3)
    
    # FIXED TEXT FORMATTING: Renders the initial 0% text aligned with your new low-profile position
    # 4:3 mode skips this label entirely — same reasoning as the main loop's
    # progress text gate below (PART 19b): the letterboxed boot artwork leaves
    # no room for it there, and the bar alone is enough to show progress.
    if DETECTED_ASPECT_RATIO != "4:3":
        try:
            _init_font_size = max(9, int(18 * _init_art_scale))
            _init_text_gap = max(2, int(4 * _init_art_scale))
            f_init_load = pygame.font.SysFont("Courier New", _init_font_size, bold=True)
            lbl_init_txt = f_init_load.render("0%", True, (0, 255, 128))
            
            # Centers the 0% digits exactly above your line location
            init_txt_x = (_lbg_dest_x + (_lbg_dest_w - lbl_init_txt.get_width()) // 2) - _init_offset_left
            init_txt_y = init_y - lbl_init_txt.get_height() - _init_text_gap
            screen.blit(lbl_init_txt, (init_txt_x, init_txt_y))
        except Exception as txt_init_err:
            print(f"[BOOT TIMING WARNING] Could not pre-stamp frame-zero typography: {txt_init_err}")
    
    pygame.display.flip()
    print("[BOOT TIMING] Loading image displayed immediately. at", time.time(), flush=True)
else:
    loading_bg_surface = None
    screen.fill((5, 5, 5))
    pygame.display.flip()
    print("[BOOT TIMING] No loading image - using fallback black screen.")

pygame.event.pump()
print("[BOOT TIMING] Loading screen visible - window has popped up. at", time.time(), flush=True)

# Joystick subsystem init is intentionally NOT done here on the main thread.
# SDL's joystick enumeration can take 30-40+ seconds on some systems with
# wireless/Bluetooth controller receivers attached, and that call blocks
# whatever thread calls it. Doing it here - even after the window/loading
# screen appear - still blocks the main thread before the game loop's
# `while running:` even starts, so no messages get pumped and Windows marks
# the window "Not Responding" for the whole delay. It's initialized instead
# inside _background_subsystem_worker() (see PART 3 below), on the same
# daemon thread already used to load the database/game deck/visualizer in
# parallel, so the main thread keeps pumping events and rendering the
# loading bar the entire time.

pygame.event.pump()
print("[BOOT TIMING] Part 1 completed. Moving to next sections... at", time.time(), flush=True)

# ==============================================================================
# PART 2 OF 28: CENTRAL RECORDING REGISTRIES & CORE EMULATOR DICTIONARIES
# ==============================================================================

import time
print("[BOOT TIMING] Entering Part 2 at", time.time())

# BOOT PATH-EXISTS CACHE: populated by _prewarm_boot_channel_paths() on a
# background thread while db/game_deck/visualizer/VLC are also warming up.
# _get_channel_ordered_content() (called synchronously on the main thread
# the instant the loading bar dismisses, via change_channel()) checks this
# cache first so the hundreds/thousands of os.path.exists() calls it needs
# for a big channel don't all happen cold, on the main thread, at the exact
# moment the screen would otherwise go black. See _cached_path_exists() and
# _prewarm_boot_channel_paths() below.
_path_exists_cache = {}
_path_exists_cache_lock = threading.Lock()


def _cached_path_exists(p):
    """os.path.exists() that remembers the answer. Safe to call from any
    thread (prewarm thread or main thread) and safe to call before the
    prewarm pass has reached a given path -- it just falls through to a
    normal (cached-from-here-on) filesystem check."""
    if not p:
        return False
    with _path_exists_cache_lock:
        cached = _path_exists_cache.get(p)
    if cached is not None:
        return cached
    result = os.path.exists(p)
    with _path_exists_cache_lock:
        _path_exists_cache[p] = result
    return result


app_state = {
    "is_loading": True,
    "load_progress": 0,
    "_boot_prewarm_done": False,
    "show_splash": True,
    "splash_shown_at": 0,  # Timestamp when splash was shown (for 60s auto-close)
    "show_menu": False,
    "menu_shown_at": 0,  # Timestamp when main menu was shown (for 60s auto-close)
    "show_quit_confirm": False,
    "quit_confirm_shown_at": 0,  # Timestamp when minimize window was shown (for 60s auto-close)
    # --- Ch03 screen saver ---
    "ch03_screensaver_active": False,   # True while the bouncing-logo saver is showing
    "ch03_screensaver_last_input": 0,   # wall-clock ms of last keyboard/controller event on ch03
    "ch03_ss_x": None,                  # float: current logo X position (initialised on first show)
    "ch03_ss_y": None,                  # float: current logo Y position
    "ch03_ss_vx": 2.5,                  # float: X velocity (pixels/frame at 60 fps)
    "ch03_ss_vy": 1.8,                  # float: Y velocity
    # --- Ch03 menu music ---
    "ch03_menu_music_idx":   0,         # index into db.config["ch03_menu_music"] playlist
    "ch03_menu_music_track": "",        # path of the currently loaded track (or "")
    "ch03_menu_music_started_at": 0,    # tick when the current play() was issued (stall detection)

    "current_channel": "05",
    "osd_channel_timer": 0,
    "osd_channel_pending": False,  # True = waiting for the first real video frame before arming the OSD countdown
    "osd_volume_timer": 0,
    "active_menu_tab": "System",
    "selected_guide_channel": 5,
    "selected_guide_time_idx": 0,
    "menu_selection_index": 0,
    "menu_selection_col": 0,
    "digit_buffer": "",
    "digit_timer": 0,
    "highlighted_guide_channel": "05",
    "guide_row_offset": 0,
    "guide_col_pos": 0,
    "guide_inactivity_timer": 0,
    "is_playing_video": False,
    "is_fullscreen": _BOOT_FULLSCREEN,
    "window_state_flag": "FULLSCREEN" if _BOOT_FULLSCREEN else "WINDOWED",
    "menu_layer": "TAB_SELECTION",
    "remote_remap_index": 0,
    "selected_channel_row": 5,
    "sub_menu_row_index": 0,
    "sub_menu_col_index": 0,
    "slider_scroll_cooldown": 0, # FIXED: Tracks timing to prevent values from scrolling too fast when holding down a button
    # --- Manual held-key tracking (see PART 27 "RESTORED DYNAMIC VOLUME &
    # SLIDER REPEAT ENGINE" below for why this exists alongside
    # pygame.key.get_pressed()) ---
    "_held_keys": set(),
    "_transition_static_fired": False,  # no longer used by the static transition (which now loops via _render_transition_static/_stop_transition_static) -- left as a harmless no-op default in case any saved/older code path still reads it
    "_pending_channel_activation": None,  # set by change_channel(), fired once by _run_pending_channel_activation() the instant the transition shield expires -- see that function's docstring. Replaced the old per-subsystem _ch03_*/_chaudio_* deferred-unmute flags.
    "_boot_suppress_static": False,  # set True by change_channel(is_boot=True); tells the first-frame buffering bridge to use the plain gray fill instead of channel-change static so boot tunes in clean. Cleared on the first real frame and on any real (non-boot) channel change.
    # --- Debounced settings save: see _mark_settings_dirty()/_flush_settings_if_dirty() ---
    "_settings_dirty":       False,
    "_settings_dirty_since": 0,   # ticks of the most recent change (resets the quiet timer)
    "_settings_dirty_first": 0,   # ticks of the first unsaved change (hard cap so a long
                                   # continuous slider hold still gets saved periodically)
    # --- Visualizer early/late-end skip: set by the live-tick music watchdog
    # (pygame.mixer.music.get_busy() is the real source of truth for when a
    # song finishes) so calculate_slotted_playback_state's wall-clock playlist
    # math lands on the same next track the watchdog just moved to, instead of
    # replaying the song that just ended or drifting to a different pick.
    "visualizer_skip_ch":  "",
    "visualizer_skip_pos": None,
    # --- Pygame-native file explorer overlay (Windows-style) ---
    "file_explorer_active":         False,
    "file_explorer_title":          "",
    "file_explorer_exts":           set(),
    "file_explorer_cwd":            None,
    "file_explorer_items":          [],
    "file_explorer_scroll":         0,
    "file_explorer_selection":      set(),   # set of selected item indices
    "file_explorer_sel_anchor":     0,        # anchor index for shift-click range
    "file_explorer_drag_start":     None,     # (mx,my) start of drag, or None
    "file_explorer_drag_cur":       None,     # current drag position
    "file_explorer_last_click_ms":  0,        # ms timestamp of last click (double-click detection)
    "file_explorer_last_click_idx": -1,       # item index of last click
    "file_explorer_checked":        [],       # accumulated queued file paths
    "file_explorer_context":        {},
    "file_explorer_left_sel":       -1,       # active left-panel item index
    "file_explorer_rect":           None,
}

# --- Shell-takeover detection ------------------------------------------
# db.config["start_on_boot"] is just the persisted SETTING -- turning it on
# doesn't make this app the Windows shell until the NEXT login/reboot
# actually relaunches it via the Winlogon Shell command line (see
# _get_startup_command). Checking sys.argv for the --as-shell marker that
# gets baked into that exact command line tells us whether THIS specific
# running instance is the shell right now, regardless of what the setting
# is. Until a reboot happens, this stays False even with the setting on,
# so ESC-minimize keeps working normally like it always has.
app_state["shell_takeover_active"] = "--as-shell" in sys.argv

print("[BOOT TIMING] Basic app state created.")

# (libVLC discovery now happens in _bootstrap_libvlc(), above, BEFORE
# `import vlc` runs — that's the only point where it can actually affect
# whether the import succeeds. See the comment there for why this old
# post-import add_dll_directory() call never did anything useful.)

print("[BOOT TIMING] Starting minimal imports...")

from media import RetroDatabase, get_media_duration, get_media_loudness_gain, build_show_rotation, pair_short_episodes, build_ordered_tracks, build_block_timeline, split_movies_and_series, note_random_episode_played, resolve_track_duration
# NOTE: game_deck (ExternalGameDeck) is intentionally NOT imported here.
# game_deck.py imports libretro_core (loads a native .dll), win32gui, ctypes,
# and does WASAPI COM init — all of which block the main thread long enough to
# cause a "Not Responding" hang at 0% before the loading bar even starts.
# It is imported lazily inside _background_subsystem_worker() below so the
# main thread is already painting the loading screen when the heavy work runs.
from ui import render_loading_screen, render_controls_splash, render_tv_guide, render_settings_overlay_menu, render_quit_prompt, get_guide_visible_rows, get_guide_num_cols, get_system_menu_rows, get_games_sub_menu_rows, preload_games_tab_assets, REMOTE_REMAP_ACTIONS, NUMBER_REMAP_ACTIONS, DEFAULT_REMOTE_BINDINGS as CANONICAL_REMOTE_KEYS, _remote_key_label as remote_key_label, render_dvd_button_mapping_popup, DVD_MAP_ACTIONS, DVD_MAP_DEFAULTS
from visualizers import VisualizerDeck

explorer_queue = queue.Queue()

# ==============================================================================
# PYGAME-NATIVE FILE EXPLORER OVERLAY  (Windows Explorer style)
# Left panel: This PC / drives / quick-access folders
# Right panel: folders + files with click / shift-click / drag-rect multi-select
# Mouse cursor is visible and confined to the overlay each frame.
# ==============================================================================

# -- Shared layout constants --------------------------------------------------
# Bumped for CRT readability: bigger, bolder text throughout the file
# explorer needs taller rows / wider fixed-size boxes so nothing clips.
_FE_TITLE_H    = 40
_FE_TOOLBAR_H  = 36
_FE_FOOTER_H   = 60
_FE_LEFT_W     = 285      # widened from 215 — stops long folder names from being clipped
_FE_LEFT_ROW_H = 32
_FE_LEFT_REMOVE_W = 24
_FE_COL_H      = 30
_FE_ROW_H      = 27
_FE_BTN_H      = 38
_FE_BTN_W      = 152      # "REMOVE SELECTED" / "ADD SELECTED" (wide action buttons)
_FE_BTN_W_SM   = 85       # "CANCEL" / "DONE" (same narrow size — shorter labels)
_FE_BTN_PAD    = 10
_FE_HEADER_H   = _FE_TITLE_H + _FE_TOOLBAR_H


def _fe_get_left_panel(max_rows=None):
    """Build left-panel nav items: drives under This PC, Quick Access folders,
    and a user-editable 'Add File Location' section.

    max_rows, if given, caps how many rows are returned so the panel never
    overflows its own height — the add-button row is the first thing
    dropped once room runs out, so existing saved locations are never
    hidden or pushed off by the button itself.
    """
    items = [{"label": "This PC",      "path": None, "indent": 0, "is_header": True}]
    if sys.platform == "win32":
        try:
            import ctypes as _cfe
            bitmask = _cfe.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = chr(65 + i)
                    drive  = f"{letter}:\\"
                    items.append({"label": drive, "path": drive, "indent": 1, "is_header": False})
        except Exception:
            for letter in "CDEF":
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    items.append({"label": drive, "path": drive, "indent": 1, "is_header": False})
    else:
        items.append({"label": "/  (root)", "path": "/", "indent": 1, "is_header": False})

    items.append({"label": "Quick Access", "path": None, "indent": 0, "is_header": True})
    home = os.path.expanduser("~")
    seen = set()
    for name in ["Desktop", "Downloads", "Videos", "Documents", "Pictures", "Music"]:
        candidates = [os.path.join(home, name)]
        if sys.platform == "win32":
            candidates.append(os.path.join(os.environ.get("USERPROFILE", home), name))
        for p in candidates:
            if os.path.isdir(p) and p not in seen:
                seen.add(p)
                items.append({"label": name, "path": p, "indent": 1, "is_header": False})
                break

    # ── Add File Location (user-saved shortcuts) ───────────────────────────
    items.append({"label": "Add File Location", "path": None, "indent": 0, "is_header": True})
    saved = db.config.get("fe_saved_locations", []) if db is not None else []
    for p in saved:
        items.append({
            "label": os.path.basename(p.rstrip("\\/")) or p,
            "path": p, "indent": 1, "is_header": False, "removable": True,
        })

    # The "+" add-button row only appears while there's still room for at
    # least one more row in the panel. Once the panel is full, it's removed
    # entirely rather than crowding out a saved location.
    if max_rows is None or len(items) < max_rows:
        items.append({"label": "+", "path": None, "indent": 1, "is_header": False, "is_add_btn": True})

    return items


def _fe_max_left_rows(content_h):
    """How many _FE_LEFT_ROW_H rows fit in the left panel's available height."""
    return max(1, content_h // _FE_LEFT_ROW_H)


def _fe_add_saved_location():
    """Add the single currently-highlighted right-panel folder to the
    'Add File Location' shortcuts list. No-ops if zero or more than one
    item is selected, or if the selected item isn't a folder, or if the
    panel has no room left for another row.
    """
    if db is None:
        return
    items = app_state.get("file_explorer_items", [])
    sel   = app_state.get("file_explorer_selection", set())
    if len(sel) != 1:
        return
    idx = next(iter(sel))
    if not (0 <= idx < len(items)) or items[idx]["type"] != "dir":
        return
    p = os.path.normpath(items[idx]["path"])
    saved = db.config.setdefault("fe_saved_locations", [])
    if p not in saved:
        saved.append(p)
        db.save_settings()


def _fe_remove_saved_location(path):
    """Remove a folder from the saved 'Add File Location' shortcuts list."""
    if db is None:
        return
    saved = db.config.get("fe_saved_locations", [])
    norm  = os.path.normpath(path)
    if norm in saved:
        saved.remove(norm)
        db.save_settings()


def _fe_get_items(path, valid_exts):
    """List folders then matching files in path.

    Uses os.scandir() instead of os.listdir() + os.path.isdir()/isfile().
    scandir reads the directory ONCE and carries each entry's file-type flag
    from that single read, so classifying N entries costs ~one enumeration
    rather than the ~2N extra stat() syscalls the old listdir path did. Those
    per-entry stat calls, run synchronously on the main thread, were the
    "Not Responding" bottleneck when navigating large or slow (network /
    optical) folders.
    """
    if not path:
        return []
    folders = []
    files   = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        folders.append(entry.name)
                    elif entry.is_file() and os.path.splitext(entry.name.lower())[1] in valid_exts:
                        files.append(entry.name)
                except OSError:
                    # One unreadable/vanished entry must not abort the whole listing.
                    continue
    except (PermissionError, OSError):
        return []
    folders.sort(key=str.lower)
    files.sort(key=str.lower)
    result = []
    for f in folders:
        result.append({"type": "dir",  "name": f, "path": os.path.normpath(os.path.join(path, f))})
    for f in files:
        result.append({"type": "file", "name": f, "path": os.path.normpath(os.path.join(path, f))})
    return result


def _fe_context_key(context):
    """Build a stable key for remembering 'last folder visited' per context.

    Only TV-station / visualizer pickers (schedule, commercial, holiday)
    get a key — the Games console picker always lands in that console's
    emulator folder on purpose and must never reuse a remembered cwd, so
    it returns None here and is excluded from the memory feature.
    """
    if not context:
        return None
    mode = context.get("mode")
    if mode == "schedule":
        return f"schedule:{context.get('ch','')}:{context.get('block','')}"
    if mode == "commercial":
        return f"commercial:{context.get('ch','')}:{context.get('block','')}"
    if mode == "holiday":
        return f"holiday:{context.get('ch','')}:{context.get('hkey','')}"
    if mode == "ch03_menu_music":
        return "ch03_menu_music"
    return None


def _fe_open(title, valid_exts, context):
    """Launch the file explorer overlay. Non-blocking — main loop drives it."""
    app_state["file_explorer_active"]         = True
    app_state["file_explorer_title"]          = title
    app_state["file_explorer_exts"]           = valid_exts
    app_state["file_explorer_cwd"]            = None
    app_state["file_explorer_items"]          = []
    app_state["file_explorer_scroll"]         = 0
    app_state["file_explorer_selection"]      = set()
    app_state["file_explorer_sel_anchor"]     = 0
    app_state["file_explorer_drag_start"]     = None
    app_state["file_explorer_drag_cur"]       = None
    app_state["file_explorer_last_click_ms"]  = 0
    app_state["file_explorer_last_click_idx"] = -1
    app_state["file_explorer_checked"]        = []
    app_state["file_explorer_context"]        = context
    app_state["file_explorer_left_sel"]       = -1
    app_state["file_explorer_rect"]           = None
    pygame.mouse.set_visible(True)
    surf = pygame.display.get_surface()
    if surf:
        sw, sh = surf.get_size()
        pygame.mouse.set_pos(sw // 2, sh // 2)
        # Snapshot the current frame so the fast-path can repaint a stable
        # backdrop each tick without running the heavy video/guide pipeline.
        snap = pygame.Surface((sw, sh))
        snap.blit(surf, (0, 0))
        app_state["_fe_snapshot"] = snap

    # Restore the last folder browsed for this context (TV station /
    # visualizer pickers only — see _fe_context_key). The Games console
    # picker has no key here, so it always lands on the left-panel/drives
    # view and relies on its own explicit _fe_navigate(emu_folder) call.
    ck = _fe_context_key(context)
    if ck and db is not None:
        remembered = db.config.get("fe_last_cwd", {}).get(ck)
        if remembered and os.path.isdir(remembered):
            _fe_navigate(remembered)


def _fe_navigate(path):
    """Navigate to path and read the directory immediately (synchronously),
    the way this explorer originally worked -- folders open instantly. The
    directory read uses os.scandir() (see _fe_get_items), which enumerates a
    folder in a single pass, so there is no need for a background scan thread
    or a 'SCANNING...' loading state; those were added later and only made
    navigation feel laggy/stuck, so they've been removed."""
    app_state["file_explorer_cwd"]        = path
    app_state["file_explorer_scroll"]     = 0
    app_state["file_explorer_selection"]  = set()
    app_state["file_explorer_sel_anchor"] = 0
    app_state["file_explorer_drag_start"] = None
    app_state["file_explorer_drag_cur"]   = None
    pygame.event.clear(pygame.KEYDOWN)
    if path is None:
        app_state["file_explorer_items"] = []
        return
    _valid_exts = app_state.get("file_explorer_exts", set())
    app_state["file_explorer_items"] = _fe_get_items(path, _valid_exts)


def _fe_go_up():
    cwd = app_state.get("file_explorer_cwd")
    if not cwd:
        return
    parent = os.path.dirname(cwd)
    _fe_navigate(None if parent == cwd else parent)


# ---------------------------------------------------------------------------
# Ch03 Menu Music helpers — plays through pygame's mixer on its own dedicated
# Channel, the same approach static.wav already uses successfully (see
# load_custom_static_sound). This used to run through a separate VLC
# instance because pygame.mixer produced garbled/morse-code-sounding audio
# for this MP3 -- but that was specifically pygame.mixer.music's STREAMING
# decoder (Mix_Music) choking on this file, a known weak spot for some
# VBR-encoded MP3s. Loading it as a pygame.mixer.Sound instead decodes the
# whole file up front rather than streaming it chunk-by-chunk, which is a
# completely different code path in SDL_mixer and sidesteps that decoder
# entirely. It also fixes the volume-change lag the VLC path had: pygame's
# mixer applies a volume change on the very next audio chunk, with no
# lookahead/cache buffer to drain first.
#
# A dedicated Channel (rather than the shared pygame.mixer.music slot the
# other music/visualizer channels use) keeps this independent of whatever
# track those channels have loaded, so switching to/from channel 03 can't
# stomp on or get stomped by that shared slot's load/stop/unload calls.
# ---------------------------------------------------------------------------

CH03_MUSIC_CHANNEL_ID = 2   # pygame.mixer.Channel reserved for ch03 menu
                            # music -- Channel(1) is used elsewhere for
                            # music-adjacent cues, Channel(6) is reserved for
                            # libretro game audio (see libretro_core.CHANNEL_ID)

_ch03_music_sound_cache = {}     # track path -> loaded pygame.mixer.Sound


def _get_ch03_music_channel():
    """Return the dedicated pygame Channel used for ch03 menu music."""
    return pygame.mixer.Channel(CH03_MUSIC_CHANNEL_ID)


def _load_ch03_music_sound(path):
    """Load (or fetch from cache) the Sound for a menu-music track. Cached
    so a looping playlist doesn't re-decode the same file from disk every
    single time it ends and restarts."""
    snd = _ch03_music_sound_cache.get(path)
    if snd is not None:
        return snd
    try:
        snd = pygame.mixer.Sound(path)
        _ch03_music_sound_cache[path] = snd
        return snd
    except Exception as e:
        print(f"[CH03 MUSIC] Failed to load '{path}': {e}")
        return None


def _ch03_music_is_playing():
    """True if the dedicated ch03 music channel is actively playing."""
    return _get_ch03_music_channel().get_busy()


def _ch03_music_is_stalled():
    """Always False for the pygame-backed player. pygame.mixer.Sound()
    decodes the whole file up front, so there's no async Opening/Buffering
    state to get stuck in the way the old VLC-backed player could on a
    fresh install. Kept as a function (rather than removing it) so the
    per-frame watchdog call site doesn't need to change."""
    return False


def _set_ch03_menu_music_volume(raw_vol, muted=False):
    """Apply the global volume slider (0-100) to the dedicated ch03 menu-music
    channel. Safe to call any time, even if nothing is currently playing on
    it — it's just a no-op in that case. Call this from every place that
    updates the other audio engines' volume so menu music stays in sync with
    the slider instead of being stuck at whatever level it started at."""
    try:
        # Curved here (not by the caller) so every existing call site keeps
        # working unchanged and stays perceptually consistent with the main
        # VLC/pygame volume paths -- see _perceptual_volume_pct. Uses the
        # shallower STACKED_GAIN_MIN_DB curve because MUSIC_GAIN is stacked
        # on top of this (same reasoning as the game channel's LOUDNESS_GAIN).
        eff_vol = 0 if muted else _perceptual_volume_pct(raw_vol, min_db=STACKED_GAIN_MIN_DB)
        track_gain = app_state.get("ch03_menu_music_gain", 1.0)
        gain = max(0.0, min(1.0, (eff_vol / 100.0) * MUSIC_GAIN * track_gain))
        _get_ch03_music_channel().set_volume(gain)
    except Exception as e:
        log.warning("_set_ch03_menu_music_volume: set_volume failed: %s", e)


def _set_dvd_volume(raw_vol, muted=False):
    """Apply the global volume slider (0-100) / mute to the dedicated DVD
    engine. The DVD runs on its own dvd_engine (never the shared main
    vlc_engine), so the normal vlc_engine.set_volume() calls in the volume
    key handlers don't reach it -- this keeps the disc's audio tracking the
    slider while it's the channel you're actually watching.

    Crucially this is a NO-OP while the DVD is backgrounded (dvd_iso_
    backgrounded): leaving channel 03 mutes dvd_engine on purpose so the show
    you tuned to is what you hear, and a volume nudge on that other channel
    must not un-mute the disc playing silently in the background. The real
    unmute happens only on channel-return (see the ch03 resume block)."""
    try:
        if dvd_engine is None or game_deck is None:
            return
        if not getattr(game_deck, "dvd_playback_active", False):
            return
        if getattr(game_deck, "dvd_iso_backgrounded", False):
            return
        dvd_engine.set_volume(int(raw_vol), bool(muted))
    except Exception as e:
        log.warning("_set_dvd_volume: set_volume failed: %s", e)


# ==============================================================================
# REMOTE REMAPPING: physical-key-to-canonical-key translation for the global
# "remote" buttons (W/A/S/D, arrows, Enter, Esc, N, M) plus the top-row digit
# keys (0-9). This is designed so a real USB remote control — which sends
# whatever scancodes it sends — can be mapped onto the keys this app already
# listens for everywhere.
#
# HOW IT WORKS: every action's "canonical" key is the literal pygame key
# constant the rest of the codebase already checks for (e.g. action "w" →
# pygame.K_w). The user can rebind an action to a different physical key;
# we then build a reverse lookup (physical key → canonical key) and rewrite
# event.key to the canonical value ONCE, right at the top of the keydown
# handler. Every existing `event.key == pygame.K_w` / `pygame.K_RETURN` /
# `pygame.K_5` check elsewhere in the file keeps working completely
# unchanged — it just may now be triggered by a different physical key.
#
# Numpad digits (K_KP0-K_KP9) are intentionally NOT part of this system —
# only the top-row 0-9 keys are remappable. The numpad keeps its existing
# behavior untouched either way.
#
# REMOTE_REMAP_ACTIONS, NUMBER_REMAP_ACTIONS, CANONICAL_REMOTE_KEYS, and
# remote_key_label() are imported from ui.py (see the import line above) —
# that's the single canonical copy now; this file no longer keeps its own.
# ==============================================================================

ALL_REMOTE_ACTION_IDS = [a for a, _ in REMOTE_REMAP_ACTIONS] + [a for a, _ in NUMBER_REMAP_ACTIONS]

# Some remotes' OK/Back buttons are wired (at the driver/firmware level) to
# emit a literal, indistinguishable-from-keyboard VK_ESCAPE — confirmed via
# real logs where pressing the remote's OK button during remap capture
# produced key=27 scancode=41 synthetic=False, i.e. a genuine hardware Escape
# event, not one of our synthetic media-key posts. Because that's identical
# to a real keyboard Escape press, a single ESC keydown during capture can no
# longer safely mean "cancel" -- it might be the exact remote button the user
# is trying to bind to a non-esc action. RESOLUTION: single ESC now binds
# normally like any other key; a SECOND ESC keydown arriving within
# _ESC_DOUBLE_TAP_MS of the first is what cancels the sequence instead. This
# custom event fires once that window closes with no second press, so the
# single tap can be committed as a normal binding.
_ESC_CAPTURE_RESOLVE_EVENT = pygame.USEREVENT + 1
_ESC_DOUBLE_TAP_MS = 500

# REMOTE-REMAP CAPTURE DE-DUPLICATION WINDOW.
# A single physical remote button can be delivered to this app through MORE
# THAN ONE Windows input path at the same time. Confirmed from a real user log
# (2026-07-10): one press of the remote's BACK-ARROW button (VK_BROWSER_BACK,
# 0xA6) produced TWO separate pygame KEYDOWN events -- scancode 501 from the
# WM_APPCOMMAND subclass (500 + APPCOMMAND id) AND scancode 270 from the
# WH_KEYBOARD_LL hook (VK_BROWSER_BACK -> SDL_SCANCODE_AC_BACK). During remap
# capture the first event bound the intended row ("Back/Minimize") and the
# SECOND one immediately bound the very next row ("Mute") too -- exactly the
# reported "mapping Back also auto-maps the button below it" bug. It only
# happened on Back because it's one of the few buttons this remote emits on
# BOTH paths at once; single-path buttons (e.g. Home, which only came through
# WM_APPCOMMAND) never doubled, which is why it looked tied to that one
# mapping slot. A static "these are duplicates" table can't fix it because
# WHICH buttons double is hardware/driver specific. Instead: once capture
# accepts a binding, ignore any further capture keydown that lands within this
# window -- the duplicate from the alternate delivery path always arrives a few
# hundred ms of the first, while a human moving to the NEXT prompt/row takes far
# longer. Kept comfortably above the observed ~200ms duplicate spacing and well
# below the ~1s cadence of deliberate button-per-row presses.
_REMAP_CAPTURE_DEDUP_MS = 600

# Music/visualizer channel gain relative to video channels (was 0.61 -- lowered
# ~20% per user request, those channels were too loud relative to video).
# Previously redefined locally in 8 separate places; hoisted here as the
# single source of truth.
MUSIC_GAIN = 0.488


def check_remap_conflict(db_inst, app_state, action_id, key_code):
    """
    Shared duplicate-binding guard for the remap CAPTURE flow (not for
    set_remote_binding itself, which intentionally allows duplicates -- see
    its docstring). Returns a human-readable label describing what the key
    is already bound to, or None if the key is free to use.

    This must be called from EVERY path that can commit a binding during
    REMOTE_REMAP_CAPTURE/NUMBER_REMAP_CAPTURE -- including both ESC-commit
    paths (the deferred double-tap-resolve timer and the "resolved early"
    path when the next button arrives first) -- not just the main
    non-ESC keydown path. ESC previously bypassed this check entirely,
    which let a single physical ESC/Back button get bound to every
    remaining action in the sequence with no rejection.

    IMPORTANT: this only checks (1) the DVD-player button mapping (a
    separate, untouched namespace -- db.config["dvd_bindings"]) and (2)
    keys already committed earlier in THIS remap session
    (_remap_used_keys). It deliberately does NOT check the old/persisted
    db.config["remote_bindings"] for actions that are part of this same
    walk-through: every action in REMOTE_REMAP_ACTIONS + NUMBER_REMAP_ACTIONS
    gets rebound in this same pass, so its old value is either about to be
    overwritten (not yet reached) or already overwritten (already reached,
    in which case it's covered by _remap_used_keys instead). Checking the
    stale old value caused false rejections of keys that "belonged" to a
    row further down the list that hadn't been remapped yet.
    """
    dvd_bindings = db_inst.config.get("dvd_bindings", {}) if db_inst is not None else {}
    dvd_label_by_action = dict(DVD_MAP_ACTIONS)
    for dvd_action_id, dvd_key in dvd_bindings.items():
        if dvd_key == key_code:
            return dvd_label_by_action.get(dvd_action_id, dvd_action_id) + " (DVD remote)"
    session_used = app_state.get("_remap_used_keys", [])
    if key_code in session_used:
        return "an action already mapped this session"
    return None


def set_remote_binding(db_inst, action, key_code):
    """
    Bind `action` to the physical key `key_code`.

    Duplicate bindings are allowed on purpose: the same physical button can
    be assigned to more than one action (e.g. re-entering an unchanged key
    while walking back through the full remap sequence to tweak just one
    entry, or deliberately doubling up a button across two actions that are
    never needed at the same time). We used to auto-swap the two actions
    whenever a key collision was detected so no binding was ever "silently
    lost" -- but in practice that made it impossible to redo the sequence
    and keep everything the same except one entry: re-confirming an
    unrelated action's original key could bounce a key back onto/off of the
    action you were actually trying to change, several steps later, with no
    visual indication anything happened. Plain overwrite (no swap, no
    uniqueness check) is simpler and matches what a "remap this button"
    screen should do -- it always does exactly what you just pressed.

    Note: if the exact same key ends up bound to two actions that CAN both
    be relevant in the same context, only the first match found (by
    ALL_REMOTE_ACTION_IDS order) wins when translating a raw keypress back
    to a canonical action -- see translate_remote_key(). That's an
    inherent limit of one-physical-key-to-one-live-action dispatch, not a
    bug; it only matters if both actions could ever apply at once.
    """
    if db_inst is None:
        return
    bindings = db_inst.config.setdefault("remote_bindings", {})
    for act in ALL_REMOTE_ACTION_IDS:
        bindings.setdefault(act, CANONICAL_REMOTE_KEYS.get(act))

    bindings[action] = key_code

    db_inst.config["remote_bindings"] = bindings
    db_inst.save_settings()
    print(f"[REMOTE REMAP] '{action}' bound to {remote_key_label(key_code)}")


def translate_remote_key(db_inst, raw_key):
    """
    Reverse-lookup: if `raw_key` (whatever physical key was actually pressed)
    has been bound to one of our actions, return that action's canonical key
    instead, so downstream literal `pygame.K_x` checks keep working as-is.
    If the key isn't part of any remapping, it's returned unchanged.
    """
    if db_inst is None:
        return raw_key
    bindings = db_inst.config.get("remote_bindings", {})
    if not bindings:
        return raw_key
    for action, canon_key in CANONICAL_REMOTE_KEYS.items():
        if bindings.get(action, canon_key) == raw_key:
            return canon_key
    return raw_key


def set_dvd_binding(db_inst, action, key_code):
    """Bind a DVD transport `action` (play_pause / rewind / fast_forward /
    captions / return_to_menu) to a physical key, in the DVD-ONLY db.config["dvd_bindings"]
    namespace. Kept fully separate from remote_bindings and from menu WASD so
    remapping here changes nothing outside DVD playback. Pushes the new
    bindings into the live game_deck immediately so playback honors them
    without needing a restart."""
    if db_inst is None:
        return
    bindings = db_inst.config.setdefault("dvd_bindings", {})
    for act, canon in DVD_MAP_DEFAULTS.items():
        bindings.setdefault(act, canon)
    bindings[action] = key_code
    db_inst.config["dvd_bindings"] = bindings
    db_inst.save_settings()
    if game_deck is not None:
        try:
            game_deck.refresh_from_config(db_inst.config)
        except Exception as _e:
            print(f"[DVD REMAP] game_deck refresh failed: {_e}", flush=True)
    print(f"[DVD REMAP] '{action}' bound to {remote_key_label(key_code)}")


def _start_ch03_menu_music():
    """Begin ch03 menu music playback via the dedicated pygame channel.

    Only ever plays while the user is actually on channel 03 — this menu
    setting can be toggled from the menu while sitting on any channel, and
    without this guard flipping it on would start playback immediately
    regardless of what channel is currently showing.
    """
    if db is None:
        return
    if app_state.get("current_channel") != "03":
        return
    if not db.config.get("ch03_menu_music_enabled", True):
        return
    playlist = db.config.get("ch03_menu_music", [])
    if not playlist:
        _default = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "main", "audio", "gamemusic.mp3"
        )
        if os.path.isfile(_default):
            db.config["ch03_menu_music"] = [_default]
            db.save_settings()
            playlist = db.config["ch03_menu_music"]
            print(f"[CH03 MUSIC] Seeded default track: {os.path.basename(_default)}")
    if not playlist:
        return
    if _ch03_music_is_playing():
        return
    # Try each playlist slot at most once. This used to recurse into itself
    # to skip a missing file, which is fine if SOME track exists -- but if
    # EVERY track in the playlist is missing (e.g. the playlist points at
    # files on a drive/path that isn't present on this machine), the
    # recursion cycles through every index, wraps back around, and keeps
    # going forever until Python hits its recursion limit and crashes with
    # RecursionError. A bounded loop tries each slot once and then gives up.
    playlist_len = len(playlist)
    track = None
    track_gain = 1.0
    for _attempt in range(playlist_len):
        idx = app_state.get("ch03_menu_music_idx", 0) % playlist_len
        candidate = playlist[idx]
        # Playlist entries are plain path strings until the loudness
        # pre-cache (_patch_probed_gain) probes one and migrates it in
        # place to a {"path", "gain"} dict -- handle both shapes.
        cand_path = candidate.get("path", "") if isinstance(candidate, dict) else str(candidate)
        if cand_path and os.path.isfile(cand_path):
            track = cand_path
            track_gain = candidate.get("gain", 1.0) if isinstance(candidate, dict) else 1.0
            break
        app_state["ch03_menu_music_idx"] = (idx + 1) % playlist_len
    if track is None:
        print("[CH03 MUSIC] No playable tracks found in playlist -- all files missing.")
        return
    sound = _load_ch03_music_sound(track)
    if sound is None:
        app_state["ch03_menu_music_idx"] = (idx + 1) % len(playlist)
        return
    try:
        app_state["ch03_menu_music_gain"] = track_gain
        _set_ch03_menu_music_volume(
            db.config.get("global_volume", 70), db.config.get("is_muted", False))
        _get_ch03_music_channel().play(sound)
        app_state["ch03_menu_music_track"] = track
        app_state["ch03_menu_music_started_at"] = pygame.time.get_ticks()
        print(f"[CH03 MUSIC] Playing track {idx + 1}/{len(playlist)}: {os.path.basename(track)}")
    except Exception as _e:
        print(f"[CH03 MUSIC] Failed to play '{track}': {_e}")
        app_state["ch03_menu_music_idx"] = (idx + 1) % len(playlist)


def _stop_ch03_menu_music():
    """Stop ch03 menu music if it is currently playing."""
    try:
        _get_ch03_music_channel().stop()
    except Exception as e:
        log.warning("_stop_ch03_menu_music: channel.stop() failed: %s", e)
    app_state["ch03_menu_music_track"] = ""
    app_state["ch03_menu_music_started_at"] = 0


def _adjust_ss_timer(delta, wrap=False):
    """Adjust the ch03 screen-saver idle timeout (minutes) and persist it.
    delta<0 clamps down to 5, delta>0 clamps up to 30; wrap=True cycles back to
    5 once past 30 (used by the ENTER handler). Consolidates the three copies
    that lived in the K_a / K_d / K_RETURN handlers on the Games tab."""
    _cur_min = db.config.get("ch03_screensaver_timeout_min", 5)
    if wrap:
        _new_min = 5 if _cur_min >= 30 else _cur_min + delta
    elif delta < 0:
        _new_min = max(5, _cur_min + delta)
    else:
        _new_min = min(30, _cur_min + delta)
    db.config["ch03_screensaver_timeout_min"] = _new_min
    db.save_settings()
    print(f"[GAMES] ch03 screen saver timeout = {db.config['ch03_screensaver_timeout_min']} min")


def _toggle_ss():
    """Flip the ch03 screen-saver enabled flag and persist it."""
    db.config["ch03_screensaver_enabled"] = not db.config.get("ch03_screensaver_enabled", True)
    db.save_settings()
    print(f"[GAMES] ch03 screen saver = {db.config['ch03_screensaver_enabled']}")


def _toggle_menu_music():
    """Flip the ch03 menu-music enabled flag, start/stop playback, and persist."""
    db.config["ch03_menu_music_enabled"] = not db.config.get("ch03_menu_music_enabled", True)
    db.save_settings()
    if db.config["ch03_menu_music_enabled"]:
        _start_ch03_menu_music()
    else:
        _stop_ch03_menu_music()
    print(f"[GAMES] ch03 menu music enabled = {db.config['ch03_menu_music_enabled']}")


def _toggle_all_consoles():
    """Bulk on/off switch for every game console except DVD. DVD always stays
    on — it's the default channel-03 source and isn't affected by this button.
    If every non-DVD console is currently on, this turns them all off;
    otherwise it turns them all on."""
    from game_deck import CONSOLE_ORDER
    enabled_map = db.config.setdefault("consoles_enabled", {})
    others = [c for c in CONSOLE_ORDER if c != "DVD"]
    all_on = bool(others) and all(enabled_map.get(c, False) for c in others)
    new_state = not all_on
    for c in others:
        enabled_map[c] = new_state
    db.save_settings()
    if game_deck is not None:
        game_deck.refresh_from_config(db.config)
    print(f"[GAMES] all consoles set to {'ON' if new_state else 'OFF'} (DVD unaffected)")


def _toggle_all_profiles():
    """Toggle the 'all_profiles_sync' flag on/off.

    When ON: every time the user changes auto_save, auto_load, or the
    border toggle inside Profile N of ANY console, that same value is
    immediately mirrored to Profile N on every OTHER console. E.g. editing
    Profile 1 on NES mirrors to Profile 1 on SNES, Genesis, N64, etc. --
    it does NOT touch Profiles 2/3 of the console you're actually editing.
    This saves having to open every console's menu separately when you
    want one profile slot to behave consistently everywhere. The flag only
    governs future edits — existing profile values are left as-is when the
    toggle is first switched on."""
    db.config["all_profiles_sync"] = not db.config.get("all_profiles_sync", False)
    _mark_settings_dirty()
    print(f"[GAMES] all_profiles_sync = {db.config['all_profiles_sync']}")


def _sync_profile_setting_across_consoles(source_console, profile_num, key, value):
    """When all_profiles_sync is ON, mirror a single profile setting (auto_save,
    auto_load, or border_enabled) from Profile `profile_num` on
    `source_console` onto Profile `profile_num` of every OTHER console.
    Does nothing to other profile numbers on any console -- this is a
    same-profile-number, across-console mirror, not a within-console one.
    No-ops entirely if all_profiles_sync is OFF. Screen size is deliberately
    NOT covered here -- it stays per-console/per-profile only."""
    if db is None or not db.config.get("all_profiles_sync", False):
        return
    from game_deck import CONSOLE_ORDER
    all_profiles_db = db.config.setdefault("console_profiles", {})
    for con in CONSOLE_ORDER:
        if con == source_console:
            continue
        profiles = (all_profiles_db
                    .setdefault(con, {"active": 1, "profiles": {}})
                    .setdefault("profiles", {}))
        # Same string/int key migration _get_prof_cfg does, so a mirrored
        # write can't orphan an existing saved profile under the other key type.
        if profile_num not in profiles and str(profile_num) in profiles:
            profiles[profile_num] = profiles.pop(str(profile_num))
        cfg = profiles.setdefault(profile_num, {"auto_save": False, "auto_load": False})
        cfg[key] = value
        # Push live if THIS other console happens to be the one actually
        # loaded right now and profile_num is its active profile -- same
        # rule _is_live_target uses for the console being directly edited.
        if (game_deck is not None
                and getattr(game_deck, "_libretro_console", None) == con
                and int(db.config.get("console_profiles", {}).get(con, {}).get("active", 1)) == profile_num):
            if key == "auto_save":
                game_deck._libretro_auto_save = value
            elif key == "auto_load":
                game_deck._libretro_auto_load = value
            elif key == "border_enabled" and hasattr(game_deck, "set_border_live"):
                game_deck.set_border_live(value)
    print(f"[GAMES] all_profiles_sync mirrored {key}={value} from {source_console} "
          f"Profile {profile_num} to Profile {profile_num} on every other console")


def _fe_add_selected_to_queue():
    """Expand selected items and append matching files to the checked queue.

    PERF: dedup used to be `if fp not in checked` against the plain list --
    an O(n) scan on every single file, so expanding a folder with a few
    thousand files became an O(n^2) crawl that could visibly stall the UI
    for seconds on ADD SELECTED alone (before DONE / save is even reached).
    Track a parallel set of already-queued paths for O(1) membership checks;
    `checked` stays a plain list (order + other call sites unaffected).
    """
    items      = app_state.get("file_explorer_items", [])
    sel        = app_state.get("file_explorer_selection", set())
    checked    = app_state.setdefault("file_explorer_checked", [])
    valid_exts = app_state.get("file_explorer_exts", set())
    checked_set = set(checked)   # one O(n) pass, not one per candidate file
    for idx in sorted(sel):
        if not (0 <= idx < len(items)):
            continue
        p = items[idx]["path"]
        if os.path.isdir(p):
            for rd, dirs, files in os.walk(p):
                dirs.sort()
                for fn in sorted(files):
                    if os.path.splitext(fn.lower())[1] in valid_exts:
                        fp = os.path.normpath(os.path.join(rd, fn))
                        if fp not in checked_set:
                            checked_set.add(fp)
                            checked.append(fp)
        elif os.path.isfile(p):
            fp = os.path.normpath(p)
            if fp not in checked_set:
                checked_set.add(fp)
                checked.append(fp)
    app_state["file_explorer_selection"] = set()


def _get_cached_media_probe(norm_path):
    """
    Global, path-keyed cache of already-probed duration/gain (db.config
    ["media_probe_cache"], normalized path -> {"duration":..., "gain":...}),
    shared across every channel/schedule that happens to reference the same
    physical file.

    Without this, the same file added to two different channels' schedules
    (e.g. the same song added to channel 11 AND channel 14) got probed
    twice, completely independently -- so a copy on a channel whose probe
    thread hadn't reached it yet kept sitting at the fallback duration and
    getting cut short, even though ANOTHER channel already knew the real
    length. Every probe path (fresh-add probing, the current+next-3
    precache, everything) checks this first and only launches VLC/ffmpeg if
    truly nothing has probed this exact path before.

    Returns {} (not None) if nothing is cached yet, so callers can always do
    `.get("duration")`/`.get("gain")` without a None-check.
    """
    if db is None:
        return {}
    return db.config.get("media_probe_cache", {}).get(norm_path, {})


def _store_cached_media_probe(norm_path, duration=None, gain=None):
    """Writes a freshly-probed duration/gain into the global cross-channel
    cache (see _get_cached_media_probe). Only overwrites the fields actually
    passed in -- calling this with just a duration (or just a gain) never
    clobbers whichever half was already known for this path."""
    if db is None or not norm_path:
        return
    cache = db.config.setdefault("media_probe_cache", {})
    entry = cache.setdefault(norm_path, {})
    if duration and duration > 0:
        entry["duration"] = int(duration)
    if gain:
        entry["gain"] = gain


def _fe_remove_selected_from_queue():
    """Remove selected items (and their expanded files) from the checked queue."""
    items      = app_state.get("file_explorer_items", [])
    sel        = app_state.get("file_explorer_selection", set())
    checked    = app_state.get("file_explorer_checked", [])
    valid_exts = app_state.get("file_explorer_exts", set())
    to_remove  = set()
    for idx in sorted(sel):
        if not (0 <= idx < len(items)):
            continue
        p = items[idx]["path"]
        if os.path.isdir(p):
            for rd, dirs, files in os.walk(p):
                dirs.sort()
                for fn in sorted(files):
                    if os.path.splitext(fn.lower())[1] in valid_exts:
                        to_remove.add(os.path.normpath(os.path.join(rd, fn)))
        elif os.path.isfile(p):
            to_remove.add(os.path.normpath(p))
    app_state["file_explorer_checked"]  = [f for f in checked if f not in to_remove]
    app_state["file_explorer_selection"] = set()


def _probe_and_patch_durations(ch_key, container_key, sub_key, items_pool):
    """
    Background worker: probes real runtime for each newly-added file via
    get_media_duration() and patches the -1 sentinel duration in
    db.channels_db[ch_key][container_key][sub_key] once known. Also measures
    each file's loudness via get_media_loudness_gain() and patches a "gain"
    field the same way, so newly-added commercials/holiday content gets
    volume-equalized against everything else without a separate pass.

    container_key is "schedules" (sub_key = block name, e.g. "Commercials")
    or "holiday_schedules" (sub_key = holiday key, e.g. "halloween").

    Mirrors the probing done for the "schedule" (show) add-path so that
    commercial/holiday entries get accurate durations instead of being
    stuck at the -1 sentinel forever.
    """
    import time as _time
    initialize_vlc_on_demand()
    pygame.time.wait(300)
    save_counter = 0
    pool_size = len(items_pool)
    batch_size = 5 if pool_size < 50 else 25
    for i, item_path in enumerate(items_pool):
        norm_p = os.path.normpath(item_path)
        _cached = _get_cached_media_probe(norm_p)
        probed = _cached.get("duration")
        probed_gain = _cached.get("gain")
        # Only actually launch VLC/ffmpeg for whichever half (duration/gain)
        # some OTHER channel hasn't already probed for this exact file.
        if probed is None:
            try:
                probed = get_media_duration(norm_p)
            except Exception:
                probed = None
        if probed_gain is None:
            try:
                probed_gain = get_media_loudness_gain(norm_p)
            except Exception:
                probed_gain = None
        if (not probed or probed <= 0) and not probed_gain:
            continue
        _store_cached_media_probe(norm_p, duration=probed, gain=probed_gain)
        entries = db.channels_db.get(ch_key, {}).get(container_key, {}).get(sub_key, [])
        for entry in entries:
            if isinstance(entry, dict) and os.path.normpath(entry.get("path", "")) == norm_p:
                if probed and probed > 0:
                    entry["duration"] = int(probed)
                if probed_gain:
                    entry["gain"] = probed_gain
                break
        save_counter += 1
        if save_counter % batch_size == 0:
            # ASYNC WRITE: was db.save_settings() -- same synchronous
            # full config+every-channel JSON dump as everywhere else in
            # this file, just called from INSIDE the probe loop instead
            # of once at the end. For a large batch that's a full-database
            # write every 5-25 files for the entire scan -- on slower
            # disks/CPUs each one is long enough to steal the GIL from
            # pygame's render/input thread, which is exactly the "video
            # settings lag while media is loading" symptom. Async here
            # for the same reason it was made async at every other call
            # site in this file.
            try: db.save_settings_async()
            except Exception as e: log.warning("Periodic settings save failed during duration probe for %s/%s: %s", container_key, sub_key, e)
        if i < 2:
            _time.sleep(0.05)
        elif i < 15:
            _time.sleep(0.2)
        else:
            _time.sleep(0.8 if pool_size > 100 else 0.4)
    # Final save is forced synchronous so this worker thread doesn't exit
    # (daemon thread -- no join anywhere) before the last write lands.
    try: db.save_settings_async(force=True)
    except Exception as e: log.warning("Final settings save failed after duration probe for %s/%s: %s", container_key, sub_key, e)
    print(f"[DURATION PROBE] {container_key}/{sub_key} for ch {ch_key} -> probed {save_counter}/{pool_size} file(s)")


def _dedupe_incoming_files(existing_entries, incoming_paths):
    """Filters incoming_paths against files already present in
    existing_entries (schedule/commercial/holiday entries, each a
    {"path": ...} dict) -- plus against each other -- so accidentally
    adding the same episode more than once (re-picking the same file, or
    picking multiple copies of it from different folders) doesn't create
    duplicate airings that eventually surface as "didn't I already watch
    this?" repeats.

    Matches on the exact filename ONLY (e.g.
    "Naked.and.Afraid.S01E01.mp4"), never on a fuzzy/partial name, so an
    entire similarly-named season/library is never touched -- just true
    exact-name duplicates.
    """
    seen_names = {os.path.basename(e.get("path", "")) for e in existing_entries if isinstance(e, dict)}
    out = []
    for p in incoming_paths:
        name = os.path.basename(p)
        if name in seen_names:
            continue
        seen_names.add(name)
        out.append(p)
    return out


def _fe_close(commit):
    """Close the explorer, committing queued files to their destination if commit=True."""
    if commit:
        checked = app_state.get("file_explorer_checked", [])
        context = app_state.get("file_explorer_context", {})
        mode    = context.get("mode", "schedule")
        ch_key  = context.get("ch", "")
        block   = context.get("block", "")
        hkey    = context.get("hkey", "")
        if checked:
            if mode == "schedule":
                explorer_queue.put(list(checked))
            elif mode == "commercial" and ch_key and block:
                ch_data  = db.channels_db.setdefault(ch_key, {})
                existing = ch_data.setdefault("schedules", {}).get(block, [])
                new_paths = _dedupe_incoming_files(existing, checked)
                entries  = [{"path": fp, "duration": -1} for fp in new_paths]
                ch_data["schedules"][block] = existing + entries
                # ASYNC WRITE: was db.save_settings() -- a synchronous full
                # config+every-channel JSON dump on the main thread. Fine for
                # a handful of files, but a big commercial-block batch (or
                # simply a large existing schedules dict elsewhere in the
                # save) made this a felt freeze right as DONE was pressed.
                db.save_settings_async()
                print(f"[FILE EXPLORER] {block} -> {len(entries)} file(s) for ch {ch_key} "
                      f"({len(checked) - len(new_paths)} exact-name duplicate(s) skipped)")
                # 1800 above is just a placeholder so the entry exists immediately;
                # probe real runtimes in the background and patch them in place,
                # same as the "schedule" (show) path does via the metadata worker.
                threading.Thread(
                    target=_probe_and_patch_durations,
                    args=(ch_key, "schedules", block, list(new_paths)),
                    daemon=True
                ).start()
                # INSTANT PLAY: mirrors the "schedule" mode behavior above --
                # if this commercial/intro/outro block was just added to the
                # channel currently tuned in, re-tune right now (using the
                # placeholder -1 -> _safe_dur duration) instead of leaving the
                # old playback running until the user backs all the way out
                # of the menu.
                if str(app_state.get("current_channel", "")).zfill(2) == str(ch_key).zfill(2):
                    app_state["is_playing_video"] = False
                    change_channel(ch_key, is_surfing=False)
            elif mode == "holiday" and ch_key and hkey:
                ch_data  = db.channels_db.setdefault(ch_key, {})
                existing = ch_data.setdefault("holiday_schedules", {}).get(hkey, [])
                new_paths = _dedupe_incoming_files(existing, checked)
                entries  = [{"path": fp, "duration": -1} for fp in new_paths]
                ch_data["holiday_schedules"][hkey] = existing + entries
                # ASYNC WRITE: same reasoning as the "commercial" branch above.
                db.save_settings_async()
                print(f"[FILE EXPLORER] Holiday {hkey} -> {len(entries)} files for ch {ch_key} "
                      f"({len(checked) - len(new_paths)} exact-name duplicate(s) skipped)")
                threading.Thread(
                    target=_probe_and_patch_durations,
                    args=(ch_key, "holiday_schedules", hkey, list(new_paths)),
                    daemon=True
                ).start()
                # INSTANT PLAY: same reasoning as the "commercial" branch above.
                if str(app_state.get("current_channel", "")).zfill(2) == str(ch_key).zfill(2):
                    app_state["is_playing_video"] = False
                    change_channel(ch_key, is_surfing=False)
            elif mode == "ch03_menu_music":
                # Append picked files to the ch03 menu music playlist
                existing = db.config.setdefault("ch03_menu_music", [])
                # Entries are plain path strings until the loudness pre-cache
                # migrates a probed one to a {"path", "gain"} dict -- compare
                # by extracted path so an already-probed track doesn't look
                # like a "new" file and get added a second time.
                existing_paths = {e.get("path", "") if isinstance(e, dict) else str(e) for e in existing}
                new_tracks = [fp for fp in checked if fp not in existing_paths]
                existing.extend(new_tracks)
                # ASYNC WRITE: same reasoning as the "commercial" branch above --
                # a big menu-music batch shouldn't stall DONE on a full config save.
                db.save_settings_async()
                print(f"[GAMES] Menu music: added {len(new_tracks)} track(s), total={len(existing)}")
                # If we're on ch03 right now and no game/DVD is active, kick off playback
                if (app_state.get("current_channel") == "03"
                        and game_deck is not None
                        and getattr(game_deck, "mode", "BROWSE") == "BROWSE"
                        and not getattr(game_deck, "dvd_playback_active", False)):
                    _start_ch03_menu_music()
            elif mode == "libretro_core":                # Single-file pick — take only the first checked file
                ck      = context.get("console_key", "")
                folder  = context.get("emu_folder", "")
                chosen  = checked[0] if checked else ""
                if chosen and ck:
                    import json as _json, shutil as _shutil
                    dest = os.path.join(folder, os.path.basename(chosen))
                    try:
                        if os.path.abspath(chosen) != os.path.abspath(dest):
                            _shutil.copy2(chosen, dest)
                            print(f"[GAMES] Copied core into {folder}")
                    except Exception as _ce:
                        print(f"[GAMES] Could not copy core: {_ce}")
                        dest = chosen
                    cfg_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "emulator_config.json")
                    try:
                        with open(cfg_path, "r", encoding="utf-8") as fp:
                            cfg = _json.load(fp)
                    except Exception:
                        cfg = {}
                    cfg[ck] = os.path.basename(dest)
                    with open(cfg_path, "w", encoding="utf-8") as fp:
                        _json.dump(cfg, fp, indent=2)
                    print(f"[GAMES] Libretro core for {ck} set to: {os.path.basename(dest)}")
                    if game_deck is not None and hasattr(game_deck, "reload_emulator_config"):
                        game_deck.reload_emulator_config()

    # Remember the folder the user ended up in, keyed by context, so the
    # TV-station / visualizer pickers reopen there next time. Saved on
    # close regardless of commit/cancel — "where you left off" means the
    # last folder you were browsing, not just where you picked a file.
    # Games console picker has no key (see _fe_context_key) and is excluded.
    _ck  = _fe_context_key(app_state.get("file_explorer_context", {}))
    _cwd = app_state.get("file_explorer_cwd")
    if _ck and _cwd and db is not None:
        db.config.setdefault("fe_last_cwd", {})[_ck] = _cwd
        # ASYNC WRITE: was db.save_settings() -- a synchronous full
        # config+every-channel JSON dump on the main thread, firing on
        # EVERY file-explorer close (i.e. every single "add media" commit,
        # schedule mode included). This was the missed spot in an
        # otherwise-completed migration to save_settings_async() elsewhere
        # in this function -- the exact freeze-on-DONE pattern documented
        # at the other call sites above, just for the "remember last
        # folder" write specifically.
        db.save_settings_async()

    app_state["file_explorer_active"]  = False
    app_state["file_explorer_checked"] = []
    app_state["file_explorer_items"]   = []
    pygame.mouse.set_visible(False)  # Always hide mouse on close
    # Reset the burn-in timer so the menu doesn't auto-close the instant the
    # file explorer is dismissed (browsing can take well over 60 s). Uses
    # last_input_time — the field the burn-in check actually reads — NOT the
    # stale menu_shown_at field that was here before and had no effect.
    if app_state.get("show_menu", False):
        app_state["last_input_time"] = pygame.time.get_ticks()


def _fe_get_btn_rects(fx, fy, fw, fh):
    """Return [(rect, key), ...] for the four footer buttons.

    Cancel and Done use a narrower width (_FE_BTN_W_SM) — they're short
    labels and don't need the same space as Remove Selected / Add Selected.
    Having them the same narrow size keeps the row visually balanced and
    frees up horizontal space for the file-count text on the left."""
    footer_y = fy + fh - _FE_FOOTER_H
    btn_y    = footer_y + (_FE_FOOTER_H - _FE_BTN_H) // 2
    widths   = [_FE_BTN_W_SM, _FE_BTN_W, _FE_BTN_W, _FE_BTN_W_SM]
    keys     = ["cancel", "remove_sel", "add_sel", "done"]
    total_w  = sum(widths) + _FE_BTN_PAD * (len(widths) - 1)
    x        = fx + fw - total_w - 8   # 8px right margin
    rects    = []
    for w, key in zip(widths, keys):
        rects.append((pygame.Rect(x, btn_y, w, _FE_BTN_H), key))
        x += w + _FE_BTN_PAD
    return rects


def _fe_handle_event(event):
    """Route a pygame event to the file explorer (mouse-driven, Windows-style)."""
    # ESC always cancels
    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
        _fe_close(False)
        return
    # BACKSPACE = go up one level
    if event.type == pygame.KEYDOWN and event.key == pygame.K_BACKSPACE:
        _fe_go_up()
        return

    fe_rect = app_state.get("file_explorer_rect")
    if not fe_rect:
        return
    fx, fy, fw, fh = fe_rect

    # ── Geometry (must match _fe_render) ────────────────────────────────────
    content_y  = fy + _FE_HEADER_H
    content_h  = fh - _FE_HEADER_H - _FE_FOOTER_H
    right_x    = fx + _FE_LEFT_W + 1
    right_w    = fw - _FE_LEFT_W - 1
    items_y    = content_y + _FE_COL_H
    items_h    = content_h - _FE_COL_H
    footer_y   = fy + fh - _FE_FOOTER_H
    tb_y       = fy + _FE_TITLE_H

    left_rect   = pygame.Rect(fx, content_y, _FE_LEFT_W, content_h)
    items_rect  = pygame.Rect(right_x, items_y, right_w - 12, items_h)
    footer_rect = pygame.Rect(fx, footer_y, fw, _FE_FOOTER_H)
    up_rect     = pygame.Rect(fx + 6, tb_y + (_FE_TOOLBAR_H - 26) // 2, 60, 26)

    items  = app_state.get("file_explorer_items", [])
    scroll = app_state.get("file_explorer_scroll", 0)
    sel    = app_state.get("file_explorer_selection", set())

    def idx_at(my):
        if my < items_rect.y or my >= items_rect.bottom:
            return -1
        return scroll + (my - items_rect.y) // _FE_ROW_H

    if event.type == pygame.MOUSEBUTTONDOWN:
        mx, my = event.pos
        btn = event.button
        # Any mouse activity in the file explorer counts as user input — reset
        # the burn-in timer so the menu behind the explorer never auto-closes
        # while the user is actively browsing (mouse-only, no keyboard needed).
        app_state["last_input_time"] = pygame.time.get_ticks()

        # Older pygame scroll (button 4/5)
        if btn == 4:  # Legacy scroll up
            app_state["file_explorer_scroll"] = max(0, scroll - 3)
            return
        if btn == 5:  # Legacy scroll down
            n = len(items)
            visible_rows = max(1, (app_state.get("file_explorer_rect", (0,0,0,500))[3] - _FE_HEADER_H - _FE_FOOTER_H - _FE_COL_H) // _FE_ROW_H)
            app_state["file_explorer_scroll"] = max(0, min(max(0, n - visible_rows), scroll + 3))
            return
        if btn != 1:
            return

        # [UP] toolbar button
        if up_rect.collidepoint(mx, my):
            _fe_go_up()
            return

        # Left panel navigation
        if left_rect.collidepoint(mx, my):
            max_rows = _fe_max_left_rows(content_h)
            for i, li in enumerate(_fe_get_left_panel(max_rows)):
                iy = content_y + i * _FE_LEFT_ROW_H
                if not (iy <= my < iy + _FE_LEFT_ROW_H):
                    continue
                if li.get("is_add_btn"):
                    _fe_add_saved_location()
                    return
                if li["is_header"] or not li["path"]:
                    return
                # Removable (saved-location) rows have a "-" zone on the
                # right edge of the row; clicking it removes the shortcut
                # instead of navigating into it.
                if li.get("removable") and mx >= fx + _FE_LEFT_W - _FE_LEFT_REMOVE_W:
                    _fe_remove_saved_location(li["path"])
                    return
                app_state["file_explorer_left_sel"] = i
                _fe_navigate(li["path"])
                return

        # Footer buttons
        if footer_rect.collidepoint(mx, my):
            for rect, key in _fe_get_btn_rects(fx, fy, fw, fh):
                if rect.collidepoint(mx, my):
                    if key == "cancel":
                        _fe_close(False)
                    elif key == "remove_sel":
                        _fe_remove_selected_from_queue()
                    elif key == "add_sel":
                        _fe_add_selected_to_queue()
                    elif key == "done":
                        _fe_close(True)
                    return

        # Right-panel item list
        if items_rect.collidepoint(mx, my):
            idx  = idx_at(my)
            mods = pygame.key.get_mods()
            if 0 <= idx < len(items):
                if mods & pygame.KMOD_SHIFT:
                    anchor = app_state.get("file_explorer_sel_anchor", 0)
                    lo, hi = min(anchor, idx), max(anchor, idx)
                    app_state["file_explorer_selection"] = set(range(lo, hi + 1))
                elif mods & pygame.KMOD_CTRL:
                    new_sel = set(sel)
                    if idx in new_sel:
                        new_sel.discard(idx)
                    else:
                        new_sel.add(idx)
                    app_state["file_explorer_selection"] = new_sel
                    app_state["file_explorer_sel_anchor"] = idx
                else:
                    app_state["file_explorer_selection"]  = {idx}
                    app_state["file_explorer_sel_anchor"] = idx

                # Double-click detection
                now = pygame.time.get_ticks()
                lct = app_state.get("file_explorer_last_click_ms", 0)
                lci = app_state.get("file_explorer_last_click_idx", -1)
                if lci == idx and (now - lct) < 500:
                    item = items[idx]
                    if item["type"] == "dir":
                        _fe_navigate(item["path"])
                        return
                app_state["file_explorer_last_click_ms"]  = now
                app_state["file_explorer_last_click_idx"] = idx
            else:
                if not (mods & (pygame.KMOD_SHIFT | pygame.KMOD_CTRL)):
                    app_state["file_explorer_selection"] = set()

            # Begin drag
            app_state["file_explorer_drag_start"] = (mx, my)
            app_state["file_explorer_drag_cur"]   = (mx, my)

    elif event.type == pygame.MOUSEMOTION:
        if event.buttons[0] and app_state.get("file_explorer_drag_start"):
            mx, my = event.pos
            app_state["file_explorer_drag_cur"] = (mx, my)
            sx, sy = app_state["file_explorer_drag_start"]
            if abs(mx - sx) > 4 or abs(my - sy) > 4:
                # ── Auto-scroll when dragging near / past the list edges ─────────
                # Speed ramps from 1 row/120ms at the inner edge of the zone to
                # 1 row/60ms when the cursor is fully outside the list rect.
                # Shift-click remains available as a keyboard-friendly alternative
                # for selecting across hundreds of items without dragging.
                _ZONE  = 50   # px inside the edge where auto-scroll activates
                _now_t = pygame.time.get_ticks()
                _last_t = app_state.get("_fe_drag_scroll_t", 0)
                _n     = len(items)
                _vis   = max(1, items_h // _FE_ROW_H)
                _max_sc = max(0, _n - _vis)
                _cur_sc = app_state.get("file_explorer_scroll", 0)
                _delta  = 0
                _speed  = 120

                _d_top = my - items_rect.y
                _d_bot = items_rect.bottom - my

                if my < items_rect.y or _d_top < _ZONE:
                    # Cursor approaching or past the top edge → scroll up
                    _speed = max(60, int(120 * max(0, _d_top) / _ZONE)) if _d_top >= 0 else 60
                    _delta = -1
                elif my > items_rect.bottom or _d_bot < _ZONE:
                    # Cursor approaching or past the bottom edge → scroll down
                    _speed = max(60, int(120 * max(0, _d_bot) / _ZONE)) if _d_bot >= 0 else 60
                    _delta = 1

                if _delta and (_now_t - _last_t) >= _speed:
                    _cur_sc = max(0, min(_max_sc, _cur_sc + _delta))
                    app_state["file_explorer_scroll"] = _cur_sc
                    app_state["_fe_drag_scroll_t"]    = _now_t

                # ── Range-select: anchor → cursor item (continuous shift-click) ──
                # The anchor idx was set on MOUSEBUTTONDOWN. Extend the selection
                # to whichever item is under the cursor (clamped to the list).
                # Auto-scroll reveals new items that immediately enter the range.
                _anchor  = app_state.get("file_explorer_sel_anchor", 0)
                if my <= items_rect.y:
                    _cur_idx = _cur_sc               # above list → top visible item
                elif my >= items_rect.bottom:
                    _cur_idx = _cur_sc + _vis - 1    # below list → bottom visible item
                else:
                    _cur_idx = _cur_sc + (my - items_rect.y) // _FE_ROW_H
                _cur_idx = max(0, min(_n - 1, _cur_idx))
                _lo, _hi = min(_anchor, _cur_idx), max(_anchor, _cur_idx)
                app_state["file_explorer_selection"] = set(range(_lo, _hi + 1))

    elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
        app_state["file_explorer_drag_start"] = None
        app_state["file_explorer_drag_cur"]   = None

    elif event.type == pygame.MOUSEWHEEL:
        n = len(items)
        # event.y: positive = scroll wheel up, negative = scroll wheel down
        # Subtract so scrolling down moves the list down (higher index)
        visible_rows = max(1, (app_state.get("file_explorer_rect", (0,0,0,500))[3] - _FE_HEADER_H - _FE_FOOTER_H - _FE_COL_H) // _FE_ROW_H)
        max_scroll = max(0, n - visible_rows)
        app_state["file_explorer_scroll"] = max(0, min(max_scroll, scroll - event.y * 3))


def _fe_render(screen, active_theme, fe_rect):
    """Render the Windows-style file explorer overlay."""
    import colorsys as _fecs
    fx, fy, fw, fh = fe_rect
    app_state["file_explorer_rect"] = fe_rect

    # ── Theme colours ────────────────────────────────────────────────────────
    ui_hue   = db.config.get("theme_ui_hue", 140) / 360.0 if db else 0.39
    bg_hue   = db.config.get("theme_bg_hue", 220) / 360.0 if db else 0.61
    _r,_g,_b = _fecs.hsv_to_rgb(ui_hue, 0.88, 1.0)
    UI_C     = (int(_r*255), int(_g*255), int(_b*255))
    _r,_g,_b = _fecs.hsv_to_rgb(bg_hue, 0.55, 0.10)
    BG_C     = (int(_r*255), int(_g*255), int(_b*255))
    _r,_g,_b = _fecs.hsv_to_rgb(bg_hue, 0.45, 0.16)
    PANEL_C  = (int(_r*255), int(_g*255), int(_b*255))
    _r,_g,_b = _fecs.hsv_to_rgb(ui_hue, 0.65, 0.50)
    SEL_C    = (int(_r*255), int(_g*255), int(_b*255))
    _r,_g,_b = _fecs.hsv_to_rgb(bg_hue, 0.35, 0.14)
    ITEM_BG  = (int(_r*255), int(_g*255), int(_b*255))
    WHITE    = (255, 255, 255)
    GREY     = (150, 150, 150)
    GREEN    = (80, 220, 100)

    bg_alpha = min(255, int((db.config.get("menu_opacity", 50) / 100.0) * 255) + 90) if db else 235

    # ── Geometry ─────────────────────────────────────────────────────────────
    content_y = fy + _FE_HEADER_H
    content_h = fh - _FE_HEADER_H - _FE_FOOTER_H
    right_x   = fx + _FE_LEFT_W + 1
    right_w   = fw - _FE_LEFT_W - 1
    items_y   = content_y + _FE_COL_H
    items_h   = content_h - _FE_COL_H
    footer_y  = fy + fh - _FE_FOOTER_H
    tb_y      = fy + _FE_TITLE_H
    visible   = max(1, items_h // _FE_ROW_H)

    # ── State ────────────────────────────────────────────────────────────────
    items    = app_state.get("file_explorer_items", [])
    scroll   = app_state.get("file_explorer_scroll", 0)
    sel      = app_state.get("file_explorer_selection", set())
    checked  = app_state.get("file_explorer_checked", [])
    cwd      = app_state.get("file_explorer_cwd")
    title    = app_state.get("file_explorer_title", "FILE BROWSER")
    left_sel = app_state.get("file_explorer_left_sel", -1)
    drag_s   = app_state.get("file_explorer_drag_start")
    drag_c   = app_state.get("file_explorer_drag_cur")
    mx, my   = pygame.mouse.get_pos()

    # ── Overall background ───────────────────────────────────────────────────
    bg = pygame.Surface((fw, fh), pygame.SRCALPHA)
    bg.fill(BG_C + (bg_alpha,))
    screen.blit(bg, (fx, fy))

    # ── Title bar ────────────────────────────────────────────────────────────
    tb_surf = pygame.Surface((fw, _FE_TITLE_H), pygame.SRCALPHA)
    tb_surf.fill(UI_C + (80,))
    screen.blit(tb_surf, (fx, fy))
    f_title = _cached_sys_font("Courier New", 19, bold=True)
    t_lbl   = f_title.render(title, True, WHITE)
    screen.blit(t_lbl, (fx + 12, fy + (_FE_TITLE_H - t_lbl.get_height()) // 2))

    # ── Toolbar ──────────────────────────────────────────────────────────────
    pygame.draw.rect(screen, (10, 10, 10), (fx, tb_y, fw, _FE_TOOLBAR_H))
    up_rect = pygame.Rect(fx + 6, tb_y + (_FE_TOOLBAR_H - 26) // 2, 60, 26)
    is_up   = up_rect.collidepoint(mx, my)
    pygame.draw.rect(screen, UI_C if is_up else (40, 40, 40), up_rect)
    pygame.draw.rect(screen, UI_C, up_rect, 1)
    f_sm  = _cached_sys_font("Courier New", 14, bold=True)
    up_lbl = f_sm.render("UP ^", True, WHITE)
    screen.blit(up_lbl, (up_rect.x + (up_rect.w - up_lbl.get_width()) // 2,
                          up_rect.y + (up_rect.h - up_lbl.get_height()) // 2))
    # Path bar
    pb_x    = up_rect.right + 6
    pb_h    = 26
    pb_rect = pygame.Rect(pb_x, tb_y + (_FE_TOOLBAR_H - pb_h) // 2, fw - (pb_x - fx) - 6, pb_h)
    pygame.draw.rect(screen, (18, 18, 18), pb_rect)
    pygame.draw.rect(screen, (60, 60, 60), pb_rect, 1)
    path_tx = cwd if cwd else "Computer"
    f_path  = _cached_sys_font("Courier New", 14, bold=True)
    p_lbl   = f_path.render(path_tx, True, UI_C)
    clip_s  = pygame.Surface((pb_rect.w - 8, pb_rect.h), pygame.SRCALPHA)
    clip_s.fill((0, 0, 0, 0))
    clip_s.blit(p_lbl, (0, (pb_rect.h - p_lbl.get_height()) // 2))
    screen.blit(clip_s, (pb_rect.x + 4, pb_rect.y))

    # ── Left panel ───────────────────────────────────────────────────────────
    lp = pygame.Surface((_FE_LEFT_W, content_h), pygame.SRCALPHA)
    lp.fill(PANEL_C + (bg_alpha,))
    screen.blit(lp, (fx, content_y))
    pygame.draw.line(screen, (50, 50, 50), (fx + _FE_LEFT_W, content_y), (fx + _FE_LEFT_W, content_y + content_h), 1)

    max_rows   = _fe_max_left_rows(content_h)
    left_items = _fe_get_left_panel(max_rows)
    f_lhdr = _cached_sys_font("Courier New", 13, bold=True)
    f_litem = _cached_sys_font("Courier New", 15, bold=True)
    f_btn_sym = _cached_sys_font("Courier New", 17, bold=True)
    for i, li in enumerate(left_items):
        ly = content_y + i * _FE_LEFT_ROW_H
        if ly + _FE_LEFT_ROW_H < content_y or ly > content_y + content_h:
            continue
        is_lhov    = (fx <= mx < fx + _FE_LEFT_W and ly <= my < ly + _FE_LEFT_ROW_H)
        is_lactive = (i == left_sel)
        if li.get("is_add_btn"):
            # Same dark bar treatment as a section title, per spec — just
            # with a centered "+" instead of a label, and a hover state
            # so it reads as clickable.
            pygame.draw.rect(screen, (8, 8, 8), (fx, ly, _FE_LEFT_W, _FE_LEFT_ROW_H))
            if is_lhov:
                hs = pygame.Surface((_FE_LEFT_W, _FE_LEFT_ROW_H), pygame.SRCALPHA)
                hs.fill(UI_C + (45,))
                screen.blit(hs, (fx, ly))
            plus_clr = UI_C if is_lhov else (140, 140, 140)
            lbl = f_btn_sym.render("+", True, plus_clr)
            screen.blit(lbl, (fx + (_FE_LEFT_W - lbl.get_width()) // 2,
                              ly + (_FE_LEFT_ROW_H - lbl.get_height()) // 2))
            continue
        if li["is_header"]:
            pygame.draw.rect(screen, (8, 8, 8), (fx, ly, _FE_LEFT_W, _FE_LEFT_ROW_H))
            lbl = f_lhdr.render("  " + li["label"].upper(), True, (100, 100, 100))
            screen.blit(lbl, (fx + 2, ly + (_FE_LEFT_ROW_H - lbl.get_height()) // 2))
            continue

        is_removable = li.get("removable", False)
        row_w = _FE_LEFT_W - (_FE_LEFT_REMOVE_W if is_removable else 0)
        if is_lactive:
            pygame.draw.rect(screen, SEL_C, (fx, ly, row_w, _FE_LEFT_ROW_H))
        elif is_lhov:
            hs = pygame.Surface((row_w, _FE_LEFT_ROW_H), pygame.SRCALPHA)
            hs.fill(UI_C + (45,))
            screen.blit(hs, (fx, ly))
        prefix = "[D] " if (":\\" in li["label"] or li["label"] == "/  (root)") else "[F] "
        tc_l   = WHITE if (is_lactive or is_lhov) else (200, 200, 200)
        lbl    = f_litem.render("  " + "  " * li["indent"] + prefix + li["label"], True, tc_l)
        # Clip to row_w so long folder names never bleed into the right panel
        _lp_old_clip = screen.get_clip()
        screen.set_clip(pygame.Rect(fx, ly, row_w, _FE_LEFT_ROW_H))
        screen.blit(lbl, (fx + 2, ly + (_FE_LEFT_ROW_H - lbl.get_height()) // 2))
        screen.set_clip(_lp_old_clip)

        if is_removable:
            rm_x    = fx + _FE_LEFT_W - _FE_LEFT_REMOVE_W
            rm_rect = pygame.Rect(rm_x, ly, _FE_LEFT_REMOVE_W, _FE_LEFT_ROW_H)
            is_rmhov = rm_rect.collidepoint(mx, my)
            if is_rmhov:
                hs = pygame.Surface((_FE_LEFT_REMOVE_W, _FE_LEFT_ROW_H), pygame.SRCALPHA)
                hs.fill((180, 40, 40, 90))
                screen.blit(hs, (rm_x, ly))
            minus_clr = (255, 120, 120) if is_rmhov else (130, 70, 70)
            mlbl = f_btn_sym.render("-", True, minus_clr)
            screen.blit(mlbl, (rm_x + (_FE_LEFT_REMOVE_W - mlbl.get_width()) // 2,
                               ly + (_FE_LEFT_ROW_H - mlbl.get_height()) // 2))

    # ── Right panel: column header ────────────────────────────────────────────
    pygame.draw.rect(screen, (15, 15, 15), (right_x, content_y, right_w, _FE_COL_H))
    pygame.draw.line(screen, (45, 45, 45), (right_x, content_y + _FE_COL_H - 1),
                     (right_x + right_w, content_y + _FE_COL_H - 1), 1)
    f_col = _cached_sys_font("Courier New", 14, bold=True)
    screen.blit(f_col.render("Name", True, GREY), (right_x + 8, content_y + (_FE_COL_H - f_col.size("Name")[1]) // 2))
    screen.blit(f_col.render("Type", True, GREY), (right_x + right_w - 115, content_y + (_FE_COL_H - f_col.size("Type")[1]) // 2))

    # ── Right panel: item list ──────────────────────────────────���──────────
    ib = pygame.Surface((right_w, items_h), pygame.SRCALPHA)
    ib.fill(ITEM_BG + (bg_alpha,))
    screen.blit(ib, (right_x, items_y))

    f_dir  = _cached_sys_font("Courier New", 17, bold=True)
    f_file = _cached_sys_font("Courier New", 17, bold=True)

    # ── Per-row overlay surfaces: cached by size+color so they are only
    # re-allocated when the explorer window resizes or the theme changes.
    # The old code created 3-4 new SRCALPHA surfaces per visible row per
    # frame (alternating tint, selection highlight, hover highlight, name-
    # clip). At 60fps with ~20 visible rows that was ~4 800 allocations/sec
    # — a primary source of stuttering on mid-range GPUs. Surfaces are
    # now allocated once and reused; the name column uses screen.set_clip()
    # (zero allocations) instead of a throw-away SRCALPHA clip surface.
    _rsw   = right_w - 12
    _sc_key = (_rsw, _FE_ROW_H, SEL_C, UI_C)
    if app_state.get("_fe_surf_cache_key") != _sc_key:
        app_state["_fe_surf_cache_key"] = _sc_key
        _a = pygame.Surface((_rsw, _FE_ROW_H), pygame.SRCALPHA)
        _a.fill((255, 255, 255, 8))
        app_state["_fe_surf_alt"] = _a
        _s = pygame.Surface((_rsw, _FE_ROW_H), pygame.SRCALPHA)
        _s.fill(SEL_C + (185,))
        app_state["_fe_surf_sel"] = _s
        _h = pygame.Surface((_rsw, _FE_ROW_H), pygame.SRCALPHA)
        _h.fill(UI_C + (35,))
        app_state["_fe_surf_hov"] = _h
    _alt_surf = app_state.get("_fe_surf_alt")
    _sel_surf = app_state.get("_fe_surf_sel")
    _hov_surf = app_state.get("_fe_surf_hov")

    for i in range(visible):
        idx   = scroll + i
        if idx >= len(items):
            break
        item  = items[idx]
        row_y = items_y + i * _FE_ROW_H
        is_sel = idx in sel
        is_chk = item.get("path", "") in checked
        is_hov = (right_x <= mx < right_x + right_w - 12 and row_y <= my < row_y + _FE_ROW_H and not drag_s)

        if i % 2 == 1 and _alt_surf:
            screen.blit(_alt_surf, (right_x, row_y))
        if is_sel and _sel_surf:
            screen.blit(_sel_surf, (right_x, row_y))
        elif is_hov and _hov_surf:
            screen.blit(_hov_surf, (right_x, row_y))

        t      = item["type"]
        is_dir = (t == "dir")
        prefix = "[DIR]  " if is_dir else ("  [+]  " if is_chk else "       ")
        f_use  = f_dir if is_dir else f_file
        tc_row = (200, 225, 255) if is_sel else (GREEN if is_chk else WHITE)

        name_lbl = f_use.render(prefix + item["name"], True, tc_row)
        # screen.set_clip clips the blit without allocating a new surface
        _old_clip = screen.get_clip()
        screen.set_clip(pygame.Rect(right_x + 4, row_y, right_w - 125, _FE_ROW_H))
        screen.blit(name_lbl, (right_x + 4, row_y + (_FE_ROW_H - name_lbl.get_height()) // 2))
        screen.set_clip(_old_clip)

        type_txt = "Folder" if is_dir else os.path.splitext(item["name"])[1].upper()
        type_lbl = f_file.render(type_txt, True, GREY)
        screen.blit(type_lbl, (right_x + right_w - 115, row_y + (_FE_ROW_H - type_lbl.get_height()) // 2))

    # Scrollbar
    if len(items) > visible:
        sb_x = right_x + right_w - 10
        th   = max(20, items_h * visible // max(1, len(items)))
        ty   = items_y + (items_h - th) * scroll // max(1, len(items) - visible)
        pygame.draw.rect(screen, (28, 28, 28), (sb_x, items_y, 8, items_h))
        pygame.draw.rect(screen, UI_C, (sb_x, ty, 8, th))

    # Drag selection rect
    if drag_s and drag_c:
        sx, sy = drag_s
        ex, ey = drag_c
        if abs(ex - sx) > 3 or abs(ey - sy) > 3:
            rx, ry = min(sx, ex), min(sy, ey)
            rw, rh = abs(ex - sx), abs(ey - sy)
            dr = pygame.Surface((max(1, rw), max(1, rh)), pygame.SRCALPHA)
            dr.fill(UI_C + (40,))
            screen.blit(dr, (rx, ry))
            pygame.draw.rect(screen, UI_C, (rx, ry, rw, rh), 1)

    # ── Footer ───────────────────────────────────────────────────────────────
    pygame.draw.line(screen, UI_C, (fx, footer_y), (fx + fw, footer_y), 1)
    pygame.draw.rect(screen, (10, 10, 10), (fx, footer_y, fw, _FE_FOOTER_H))
    n_chk = len(checked)
    f_cnt = _cached_sys_font("Courier New", 15, bold=True)
    c_clr = GREEN if n_chk > 0 else GREY
    screen.blit(f_cnt.render(f"{n_chk} file(s) queued   |   {len(sel)} currently selected", True, c_clr),
                (fx + 12, footer_y + (_FE_FOOTER_H - f_cnt.size("x")[1]) // 2))

    f_btn   = _cached_sys_font("Courier New", 15, bold=True)
    labels  = {"cancel": "CANCEL", "remove_sel": "REMOVE SELECTED", "add_sel": "ADD SELECTED", "done": "DONE"}
    for rect, key in _fe_get_btn_rects(fx, fy, fw, fh):
        is_hov = rect.collidepoint(mx, my)
        if key == "cancel":
            bc = (210, 50, 50) if is_hov else (80, 20, 20)
        elif key == "remove_sel":
            bc = (200, 100, 30) if is_hov else (100, 50, 10)
        elif key == "done":
            bc = UI_C if is_hov else (20, 70, 20)
        else:
            bc = (40, 80, 150) if is_hov else (25, 50, 95)
        pygame.draw.rect(screen, bc, rect)
        pygame.draw.rect(screen, UI_C, rect, 1)
        lum = bc[0]*299 + bc[1]*587 + bc[2]*114
        bl  = f_btn.render(labels[key], True, (0, 0, 0) if lum > 128000 else WHITE)
        screen.blit(bl, (rect.x + (rect.w - bl.get_width()) // 2, rect.y + (rect.h - bl.get_height()) // 2))

    # Outer border
    pygame.draw.rect(screen, UI_C, (fx, fy, fw, fh), 2)


# ==============================================================================
# END PYGAME FILE EXPLORER
# ==============================================================================

print("[BOOT TIMING] Imports completed.")
print("[TELEMETRY - PART 2] Core ready. Heavy work deferred.")

# ==============================================================================
# VIDEO EFFECTS PROCESSOR - CRT-STYLE ADJUSTMENTS (OPTIMIZED)
# ==============================================================================

# Per-channel uint8 LUT cache — rebuilt only when a slider value changes.
# Scratch buffers are pre-allocated once and reused every frame to eliminate
# per-frame malloc (the #1 cause of lag when effects sliders are non-default).
_video_effects_cache = {
    "params":      None,
    "channel_lut": None,   # np.uint8 (3, 256): [R_lut, G_lut, B_lut]
    "sat_factor":  1.0,
}
_effect_scratch = {
    "surf":      None,   # reusable pygame.Surface (downscaled work buffer) — avoids per-frame allocation
    "float_buf": None,   # reusable float32 ndarray for saturation pass
    "out_surf":  None,   # reusable pygame.Surface (full-size upscaled output) — avoids per-frame allocation
}

def apply_video_effects(surface, brightness=50, contrast=50, color=50, sharpness=50, tint=50):
    """
    Apply CRT-style video effects to a pygame surface.
    All values range 0-100; 50 is neutral.

    Performance notes:
      - Fast path: all sliders at 50 → returns the original surface untouched at
        FULL quality, zero cost. This is the normal "settings off" playback path.
      - When any slider IS active, the per-pixel LUT + saturation math is done on
        a downscaled copy of the frame (EFFECTS_DOWNSCALE), then the result is
        scaled back up to the original size. Color/brightness/contrast/tint don't
        need full source resolution to look correct, and cutting pixel count
        this way is what actually removes the lag on high-resolution video —
        the numpy math itself was already minimal (cached LUT, reused buffers).
      - Scratch Surfaces and float32 buffer are allocated once and reused every
        frame, eliminating the per-frame malloc that caused lag on high-quality video.
      - LUT is rebuilt only when a slider value actually changes.
    """
    global _video_effects_cache, _effect_scratch

    # ── Fast path: nothing to do when every slider is at neutral ──────────────
    # Includes sharpness so a sharpness-only nudge doesn't trigger the slow path
    # (sharpness math is a no-op at 50 anyway).
    if brightness == 50 and contrast == 50 and color == 50 and tint == 50 and sharpness == 50:
        return surface

    # No internal downscale here anymore. The caller (main loop) now passes a
    # work-sized surface (vlc decode res) rather than the full display-sized surface,
    # so the numpy math runs on a small buffer and the single upscale to display size
    # happens AFTER effects — not before. This eliminates the double-scale that was
    # visibly degrading quality whenever any slider was active.

    try:
        current_params = (brightness, contrast, color, tint, sharpness)

        if _video_effects_cache["params"] != current_params:
            # Rebuild LUT only when a slider value changes (not every frame).
            brightness_offset = (brightness - 50) * 2.55
            contrast_factor   = contrast / 50.0

            base = np.arange(256, dtype=np.float32)
            base = base + brightness_offset
            base = (base - 128) * contrast_factor + 128

            tint_shift = (tint - 50) * 1.5
            r_lut = np.clip(base + tint_shift * 0.5, 0, 255).astype(np.uint8)
            g_lut = np.clip(base - tint_shift,        0, 255).astype(np.uint8)
            b_lut = np.clip(base + tint_shift * 0.5, 0, 255).astype(np.uint8)

            _video_effects_cache["params"]      = current_params
            _video_effects_cache["channel_lut"] = np.stack([r_lut, g_lut, b_lut])
            _video_effects_cache["sat_factor"]  = color / 50.0

        channel_lut = _video_effects_cache["channel_lut"]
        sat_factor  = _video_effects_cache["sat_factor"]

        w, h = surface.get_width(), surface.get_height()

        # ── Reuse cached scratch Surface at input resolution ───────────────────
        # Always 24-bit so pixels3d gives a stable (w,h,3) RGB view regardless of whether
        # the input is 32-bit BGRA (VLC) or 24-bit RGB. Blitting handles the conversion
        # safely via SDL — using transform.scale with a mismatched dest raises ValueError.
        # No internal downscale: the caller passes a work-sized surface (854x480 or decode
        # res), so numpy runs on small data and one smoothscale happens after, not two.
        cached_surf = _effect_scratch["surf"]
        if cached_surf is None or cached_surf.get_width() != w or cached_surf.get_height() != h:
            _effect_scratch["surf"]      = pygame.Surface((w, h), 0, 24)
            _effect_scratch["float_buf"] = None   # size changed — invalidate float buf
            cached_surf = _effect_scratch["surf"]

        # Blit into 24-bit scratch so SDL handles BGRA→RGB conversion cleanly.
        cached_surf.blit(surface, (0, 0))
        arr = pygame.surfarray.pixels3d(cached_surf)

        # ── Per-channel LUT: bakes brightness + contrast + tint in one pass ───
        arr[:, :, 0] = channel_lut[0][arr[:, :, 0]]
        arr[:, :, 1] = channel_lut[1][arr[:, :, 1]]
        arr[:, :, 2] = channel_lut[2][arr[:, :, 2]]

        # ── Saturation pass — only runs when color slider is off-neutral ───────
        if color != 50:
            shape = arr.shape  # (sw, sh, 3)

            # Reuse pre-allocated float32 scratch buffer; never allocate mid-frame
            fb = _effect_scratch["float_buf"]
            if fb is None or fb.shape != shape:
                fb = np.empty(shape, dtype=np.float32)
                _effect_scratch["float_buf"] = fb

            # Copy uint8 pixels into float32 buffer in-place (no new allocation)
            np.copyto(fb, arr, casting="unsafe")

            # Luminance-weighted grayscale, then blend toward/away from gray
            gray = (0.299 * fb[:, :, 0] +
                    0.587 * fb[:, :, 1] +
                    0.114 * fb[:, :, 2])[:, :, None]
            # Equivalent to: result = gray + (fb - gray) * sat_factor
            # Written as in-place ops to avoid temp-array allocations:
            fb -= gray
            fb *= sat_factor
            fb += gray
            np.clip(fb, 0, 255, out=fb)

            # Write back into the uint8 surface array in-place
            np.copyto(arr, fb, casting="unsafe")

        del arr

        # Return the color-graded surface at input resolution.
        # The caller does the single smoothscale to display size AFTER effects,
        # keeping quality high and scale count at 1 instead of 2.
        return cached_surf

    except Exception:
        return surface

# ==============================================================================
# PART 3 OF 28: TRUE DEFERRED SUBSYSTEM STARTUP MATRIX
# ==============================================================================

print("[BOOT TIMING] Entering Part 2A at", time.time())

db = None
vlc_engine = None
# Dedicated SECOND libVLC engine used ONLY for the DVD .iso player
# (channel 03). It runs completely independently of the main vlc_engine
# (which every TV station / media channel shares) so that leaving the DVD
# channel can simply MUTE this engine and leave the disc playing in the
# background -- preserving exact menu/submenu/movie position -- while the
# main engine keeps producing real audio for whatever show the user tuned
# to. See initialize_vlc_on_demand (creation) and the ch03 leave/return
# blocks (mute/unmute in place).
dvd_engine = None
game_deck = None
visualizer_deck = None

# Last time the bottom of the main loop was reached — used to measure whole
# real-world loop-iteration time (see the loop_total perf record near
# clock.tick() below). Deliberately measured at one fixed point every
# iteration rather than wrapped around the loop body, since the loop body
# has many early `continue`s for different channel/menu states and a
# fixed measurement point is the only one guaranteed to run every pass
# regardless of which state was active.
_last_loop_ts = None

vlc_boot_requested = False

def _audio_track_pref_key(filepath):
    """Normalizes a filepath into the lookup key used by the saved
    audio-track-preference store, so the same file always resolves to the
    same entry regardless of how its path was spelled (slashes, case on
    case-insensitive filesystems, relative vs absolute, etc.)."""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(filepath)))
    except Exception:
        return filepath


def _get_saved_audio_track(filepath):
    """Returns (track_id, track_name) previously saved for this file via
    "Change Current Playing Audio Track", or None if nothing's been saved.
    """
    if db is None:
        return None
    prefs = db.config.get("audio_track_prefs", {})
    entry = prefs.get(_audio_track_pref_key(filepath))
    if not entry:
        return None
    return entry.get("track_id"), entry.get("track_name", "")


def _save_audio_track_pref(filepath, track_id, track_name):
    """Remembers the chosen audio track for this exact file so it's
    restored automatically the next time this file plays (see
    VLCEngineWrapper._apply_saved_audio_track_when_ready)."""
    if db is None:
        return
    prefs = db.config.setdefault("audio_track_prefs", {})
    prefs[_audio_track_pref_key(filepath)] = {"track_id": track_id, "track_name": track_name}
    db.save_settings()


def _open_audio_track_picker(target_ch_str):
    """Opens the "Change Current Playing Audio Track" popup for the given
    channel. Only works while that channel is the one actually live on
    screen -- there's no VLC media loaded for a channel you're merely
    browsing in the guide, so there'd be nothing to list tracks for."""
    is_live = str(app_state.get("current_channel", "")).zfill(2) == target_ch_str
    if not is_live or vlc_engine is None:
        print(f"[AUDIO TRACK] Channel {target_ch_str} isn't the one currently on screen -- "
              f"tune to it first, then use this option.")
        return

    tracks = vlc_engine.get_audio_tracks()
    if not tracks:
        print(f"[AUDIO TRACK] No audio track info available yet for channel {target_ch_str} "
              f"(the file may still be loading) -- try again in a moment.")
        return

    current_id = vlc_engine.get_current_audio_track()
    try:
        highlight = next(i for i, (tid, _n) in enumerate(tracks) if tid == current_id)
    except StopIteration:
        highlight = 0

    app_state["audio_track_list"] = tracks
    app_state["audio_track_source_channel"] = target_ch_str
    app_state["audio_track_active_id"] = current_id
    app_state["menu_layer"] = "AUDIO_TRACK_PICKER"
    app_state["sub_menu_row_index"] = highlight
    app_state["sub_menu_col_index"] = 0


def initialize_vlc_on_demand():
    """
    Initializes the libVLC framework engine instance synchronously to prevent
    race condition lockouts on multimedia pipelines.
    """
    global vlc_engine, dvd_engine, vlc_boot_requested, game_deck
    if vlc_engine is None and not vlc_boot_requested:
        vlc_boot_requested = True
        print("[BOOT TIMING] VLC initialization triggered synchronously.")
        try:
            import vlc
            import ctypes
            
            # VLC's own detailed module-level log (dvdnav/dvdread/access
            # errors -- "cannot find access module", "css: decrypt failed",
            # etc.) used to be fully suppressed by --quiet below. That's
            # WHY get_last_error()'s libvlc_errmsg() was coming back with
            # useless/stale text like "No active input" on the SAW/Matrix
            # ISO failures -- --quiet sets verbosity so low that libVLC
            # never even generates the real error text in the first place,
            # so there was nothing for libvlc_errmsg() to return. Swapping
            # --quiet for an explicit --verbose=2 plus a dedicated
            # --file-logging sink writes the real messages to
            # vlc-debug.log next to app_log.txt, WITHOUT spamming the
            # (nonexistent, this is a windowed build) console -- the
            # file-logging module is a separate sink from the console
            # interface --quiet used to silence.
            try:
                _vlc_debug_log_path = os.path.join(_app_log_dir, "vlc-debug.log")
            except Exception:
                _vlc_debug_log_path = os.path.join(
                    os.path.dirname(os.path.abspath(sys.argv[0])), "vlc-debug.log")
            vlc_args = [
                "--no-video-title-show",
                # REMOVED "--no-osd": this is the strongest lead for the
                # missing Deal/No Deal highlight. A DVD menu's button
                # highlight isn't part of the base decoded picture -- like
                # real hardware players, libVLC composites it as an overlay
                # on top, through the same OSD/SPU region layer used for
                # volume-bar/message OSD elements. --no-osd was disabling
                # that whole layer, which would silently take the highlight
                # out with it while leaving ordinary video/audio untouched
                # -- consistent with the highlight simply never having
                # appeared, in every test so far, regardless of the timing
                # fixes elsewhere. This isn't confirmed from a log the way
                # the clock-jitter/end-state fixes were (OSD suppression
                # doesn't leave a distinct line in vlc-debug.log) -- it's
                # the best remaining explanation after ruling out timing,
                # so it's worth an isolated test on its own.
                "--verbose=2",
                "--file-logging",
                f"--logfile={_vlc_debug_log_path}",
                "--avcodec-hw=any",
                # Lowered from 1500 -- see set_volume()'s docstring. This is
                # the SAME mechanism that made ch03 menu music feel laggy:
                # libVLC decodes/queues this many ms of audio+video ahead of
                # what's actually playing, so a volume change doesn't become
                # audible until whatever was already queued at the OLD
                # volume finishes draining. Unlike ch03's small background
                # mp3 though, this engine plays actual video and needs SOME
                # real lookahead buffer to survive disk read hiccups and to
                # support the segmented-seek logic in _seek_when_ready --
                # dropping this as low as ch03's 50 risks trading slider lag
                # for visible video stutter/frame drops, which is worse.
                # 400 is a middle-ground guess to cut the lag noticeably
                # without removing that safety margin -- this needs a real
                # playback test on your end (watch for stutter/buffering
                # hiccups during normal playback and seeking) since it can't
                # be verified in this sandbox. If it stutters, raise it back
                # up in steps of ~200 until stutter goes away; if it's still
                # laggy with no stutter, it can likely go lower.
                "--file-caching=400",
                "--avcodec-threads=2",
                "--audio-filter=compressor",
                "--compressor-rms-peak=0.0",
                "--compressor-attack=25.0",
                "--compressor-release=150.0",
                "--compressor-threshold=-24.0",
                "--compressor-ratio=5.0",
                "--compressor-knee=6.0",
                "--compressor-makeup-gain=7.5",
                # Backup DVD .iso images are opened through the dvdnav access/
                # demux (see play_file_segmented's is_dvd_iso branch below), the
                # same engine real DVD hardware menus use. --dvdnav-menu is
                # already libVLC's own default, but it's pinned explicitly here
                # so a machine-level vlcrc override can't silently disable menu
                # navigation and make backup DVDs jump straight past their menus.
                "--dvdnav-menu",
                # DVD CELL-CHANGE DESYNC FIX: DVD games (Deal or No Deal,
                # and likely others) branch between non-contiguous cells to
                # hide content until you pick (e.g. an unopened case). That
                # cell jump carries a large timestamp discontinuity in the
                # underlying MPEG stream. libVLC's default --clock-jitter
                # (5000ms) is too small to absorb it -- vlc-debug.log shows
                # a 9833ms jitter being REJECTED at exactly that moment
                # ("ES_OUT_SET_(GROUP_)PCR is called too late"), after which
                # the clock desyncs and every subsequent audio/video buffer
                # gets silently discarded ("Timestamp conversion failed" /
                # "discarded audio buffer", repeated hundreds of times).
                # That's the choppy-then-cut-off playback. The dvdnav
                # highlight ("buttonUpdate not done") that shows which case
                # you've selected is carried in that same discarded stream,
                # so it disappears at the same instant for the same reason.
                #
                # UPDATE: this disc's per-case reveal cells (cellN=2, pgN=2
                # -- one per briefcase) don't share one fixed jump size.
                # A full playthrough logged SIX separate rejections, from
                # 21451ms up to 25644ms -- all bigger than the 20000ms
                # ceiling set after the first (single-case) test, so most
                # case reveals were still hitting the same cascade. The
                # single worst moment in the whole session (-1999013, a
                # ~2s clock fault right as the last case's reveal cell
                # hands off to the outro cell) is what produced the
                # decoder-teardown freeze into "thank you, see you next
                # time" -- VLC rebuilds the mpgv/a52 decoders instead of
                # playing the reveal, and by the time it's done the disc's
                # own navigation has already moved on.
                #
                # 60000ms gives >2x headroom over the worst case seen so
                # far (25644ms) so every case in a playthrough should clear
                # it, not just the ones under the old ceiling. If a future
                # vlc-debug.log still shows a "jitter of Nms ignored" line,
                # that N is the new floor to beat -- set this comfortably
                # above it, not just past it, since different cases on the
                # same disc clearly need different amounts of headroom.
                "--clock-jitter=60000",
            ]
            
            class VLCEngineWrapper:
                def __init__(self):
                    self.instance = vlc.Instance(*vlc_args)
                    self.player = self.instance.media_player_new()
                    self._vlc_debug_log_path = _vlc_debug_log_path

                    _decode_res = db.config.get("vlc_decode_res", "sd") if db else "sd"
                    self.target_aspect = db.config.get("aspect_ratio", "16:9") if db else "16:9"
                    _is_4_3 = (self.target_aspect == "4:3")
                    if _decode_res == "hd":
                        self.width, self.height = (960, 720) if _is_4_3 else (1280, 720)
                    else:
                        self.width, self.height = (640, 480) if _is_4_3 else (854, 480)

                    self.buf_size = self.width * self.height * 4
                    self.raw_buffer = (ctypes.c_ubyte * self.buf_size)()
                    self.pixel_pointer = ctypes.cast(self.raw_buffer, ctypes.c_void_p).value
                    self._frame_ready = False
                    # WARM-UP FRAME GUARD: with --avcodec-hw=any, libVLC hands
                    # this file's first couple of frames off to a hardware
                    # decoder (DXVA2/D3D11VA on Windows) while it negotiates
                    # the copy-back into our CPU-side BGRA buffer. Not every
                    # codec/file engages hardware decode the same way, so
                    # this only shows up on SOME channels/files -- exactly
                    # the "some channels get a white pixelated overlay that
                    # clears after a few seconds" symptom. The first frames
                    # written into raw_buffer during that negotiation window
                    # can be partially-copied/garbage rather than real
                    # picture data. _warmup_skip counts down frames to
                    # discard (not display) right after a new file starts,
                    # so the caller keeps showing the cached previous frame
                    # (_blit_last_video_frame) until decode has stabilized.
                    self._warmup_skip = 0
                    # LOUDNESS NORMALIZATION: linear multiplier applied on top of
                    # whatever volume set_volume() is asked for, so every existing
                    # caller (global volume slider, mute toggle, etc.) automatically
                    # gets the currently-loaded file's per-file gain without needing
                    # to know about it. Updated by play_file_segmented() whenever a
                    # new file actually starts playing.
                    self.file_gain = 1.0
                    # Set True/False by play_file_segmented() for the file
                    # currently loaded -- lets game_deck's DVD input layer
                    # know whether this file may open on an interactive
                    # dvdnav menu (see dvd_is_disc_image / dvd_in_menu).
                    self.is_dvd_iso = False

                    # ISO playback is driven entirely by libVLC's own built-in
                    # dvdnav:// access module (native SPU-composited menu
                    # highlight, same code VLC's own UI uses). An earlier
                    # from-scratch ctypes libdvdnav binding + custom VLC
                    # stream bridge (for a custom-drawn highlight box) was
                    # tried and abandoned in favor of this native path.

                    # EVENT-DRIVEN TITLE/LENGTH TRACKING: replaces the old
                    # pattern of _seek_when_ready/_enter_title_when_ready
                    # polling get_title()/get_length() in sleep(20) loops.
                    # libVLC already fires MediaPlayerTitleChanged and
                    # MediaPlayerLengthChanged the instant dvdnav's own
                    # internal state actually changes -- polling was
                    # re-deriving that same transition up to 20ms late
                    # (worse under load), which is exactly the kind of gap
                    # a menu-vs-title race can slip through. Cached here
                    # and updated ONLY from real libVLC events; nothing
                    # else should call get_title()/get_length() directly
                    # anymore -- read self._current_title /
                    # self._current_length instead, or call
                    # is_menu_active() which already does.
                    self._current_title = -1
                    self._current_length = -1
                    # Condition, not a bare Event -- an Event that gets
                    # set() then immediately clear()'d from the libVLC
                    # callback thread can be missed entirely by a waiter
                    # that isn't blocked on wait() at that exact instant.
                    # A Condition's notify_all() has no such gap: any
                    # thread already inside wait() wakes, and the shared
                    # state (_current_title/_current_length) is safe to
                    # read the moment the lock is released either way.
                    self._state_cond = threading.Condition()
                    self._last_menu_signal = None
                    # Per-disc cache of {title_index: is_menu} built lazily from
                    # libVLC's get_full_title_descriptions() menu/interactive
                    # flags. None = not built yet; reset on every new open in
                    # play_file_segmented. See _title_is_menu / is_menu_active
                    # -- this is what lets a submenu authored as a real VTS
                    # title (e.g. Deal or No Deal's title 6) still register as
                    # a menu instead of silently killing WASD navigation.
                    self._dvd_title_menu_flags = None

                    # =========================================================
                    # DVD MENU-SIGNAL SUBSYSTEM (v3 -- event-based, not line-scraped)
                    # =========================================================
                    # WHY THIS EXISTS: libVLC's dvdnav access module is the
                    # thing actually driving ISO menu playback (native
                    # SPU-composited highlight, real dvdnav state machine --
                    # no custom bridge). The one thing python-vlc does NOT
                    # expose is "is a button genuinely on screen right now" as
                    # a queryable property or clean event -- that only exists
                    # inside libdvdnav's own event stream, which reaches us
                    # ONLY as free-text lines in vlc-debug.log (--verbose=2
                    # --file-logging, see vlc_args). So this subsystem tails
                    # that file and reconstructs the real signal from it.
                    #
                    # ATTEMPT HISTORY (why v1/v2 broke -- keep for the next
                    # person debugging this):
                    #   v1: matched only the "buttonUpdate not done b=N t=T"
                    #       trace line, required an exact match for the
                    #       CURRENT title. Broke because this disc's menu
                    #       hops across many real titles as you move the
                    #       highlight (confirmed: title 6->1->2 on one "up"
                    #       press), so any hop that didn't get a fresh line
                    #       for its new title read as "no button."
                    #   v2: same line, but "confirmed sticky, cleared only on
                    #       title change." Broke because on THIS disc, every
                    #       single up/down press IS a title change (list
                    #       items are encoded as separate titles/PGCs), so
                    #       confirmation got wiped on literally every
                    #       keypress and rarely won the race to reconfirm.
                    #   v3 (this version): stopped scraping "buttonUpdate not
                    #       done" -- it's an internal libdvdnav trace, not a
                    #       real state event, and empirically doesn't fire on
                    #       every transition. Parses the REAL, structured
                    #       events libdvdnav actually emits instead:
                    #         DVDNAV_HIGHLIGHT / display=N / buttonN=N
                    #             -- the authoritative "button shown/hidden"
                    #             event. display=1 = a button is genuinely
                    #             highlighted on screen right now.
                    #         DVDNAV_CELL_CHANGE
                    #             -- fires on real navigation within a menu
                    #             PGC (confirmed this fires on hops the
                    #             HIGHLIGHT event alone missed).
                    #       Both feed one rolling "menu is genuinely active"
                    #       window (_DVD_MENU_SIGNAL_WINDOW_MS) instead of a
                    #       single fragile flag -- multiple independent,
                    #       real dvdnav events corroborating each other, any
                    #       one of which refreshes the window. game_deck.py's
                    #       TIME-JUMP watcher (position vs wall-clock) feeds
                    #       the same window as a third, fully independent
                    #       corroborating signal via note_menu_activity().
                    #       Title changes no longer reset anything by
                    #       themselves -- see _on_title_changed below --
                    #       since on this disc a title change is often just
                    #       routine highlight movement, not a real domain
                    #       exit.
                    #
                    # DEBUGGING: every parse, every window refresh, and every
                    # gate decision below logs through _dvd_sig_log() with a
                    # consistent "[DVD SIGNAL]" tag and a full state dump, so
                    # a future test log should tell the whole story without
                    # needing to be reverse-engineered by hand.
                    self._DVD_ACTIVATE_GRACE_MS = 75000
                    self._DVD_MENU_SIGNAL_WINDOW_MS = 4000
                    self._dvd_sig_log_pos = None  # lazy-inits to EOF on first poll -- see _poll_dvd_menu_signals
                    self._dvd_sig_poll_next_tick = 0
                    self._dvd_sig_last_active_tick = -10**9  # last time ANY corroborating signal fired
                    self._dvd_sig_last_active_source = None  # which signal fired most recently, for logging
                    self._dvd_sig_highlight_display = None   # last DVDNAV_HIGHLIGHT display= value seen (0/1), None = never seen
                    self._dvd_sig_highlight_button = None    # last DVDNAV_HIGHLIGHT buttonN= value seen
                    self._dvd_sig_cell_change_count = 0
                    self._dvd_sig_highlight_count = 0
                    self._dvd_sig_ever_seen = False
                    self._dvd_title_entered_at = 0
                    self._dvd_menu_active_last_logged = None  # for edge-triggered "signal changed" logging
                    # NO-TIMEOUT PER-TITLE CONFIRMATION (v3.1): the rolling
                    # window above is a DECAY model -- it re-reads as "no
                    # menu" the instant _DVD_MENU_SIGNAL_WINDOW_MS passes with
                    # no NEW event, which is fundamentally the wrong model
                    # for a signal that only ever fires on CHANGE. Confirmed
                    # on the Deal-or-No-Deal prompt screen: dvdnav's own
                    # DVDNAV_HIGHLIGHT display=1 was set once and NEVER once
                    # went back to display=0 for the rest of the session
                    # (checked: zero display=0 lines in the whole log) -- the
                    # button plainly never left the screen, dvdnav just went
                    # quiet because nothing changed while the user sat there
                    # deciding, and the 4s decay window killed WASD control
                    # mid-decision regardless (app_log.txt: ms_since_active
                    # =4007 at the exact flip). So on top of the rolling
                    # window (still useful for fast title-hopping mid-nav,
                    # where the CURRENT title briefly lags the confirmation),
                    # remember WHICH title was most recently confirmed with
                    # no expiry at all -- as long as get_title() keeps
                    # returning that exact title, treat it as confirmed no
                    # matter how long you sit there. It only stops being
                    # trusted the moment the title actually changes to
                    # something that was never itself confirmed.
                    self._dvd_sig_confirmed_title = None

                    def _on_title_changed(event):
                        with self._state_cond:
                            try:
                                self._current_title = self.player.get_title()
                            except Exception:
                                pass
                            self._state_cond.notify_all()
                        # v2 unconditionally cleared confirmation here -- see
                        # the ATTEMPT HISTORY note above for why that broke
                        # this disc specifically. v3 deliberately does NOT
                        # reset the rolling signal window on a title change:
                        # on a disc where every menu keypress is itself a
                        # title change, doing so makes the window
                        # meaningless. The window still expires naturally
                        # from genuine inactivity (_DVD_MENU_SIGNAL_WINDOW_MS
                        # with no corroborating events at all).
                        self._dvd_title_entered_at = pygame.time.get_ticks()
                        self._log_menu_signal_if_changed()
                        self._ensure_menu_highlight_spu()
                        if getattr(self, "is_dvd_iso", False):
                            self._log_dvd_transition("TITLE")

                    def _on_length_changed(event):
                        with self._state_cond:
                            try:
                                self._current_length = self.player.get_length()
                            except Exception:
                                pass
                            self._state_cond.notify_all()

                    self._on_title_changed = _on_title_changed
                    self._on_length_changed = _on_length_changed
                    _em = self.player.event_manager()
                    _em.event_attach(vlc.EventType.MediaPlayerTitleChanged, self._on_title_changed)
                    _em.event_attach(vlc.EventType.MediaPlayerLengthChanged, self._on_length_changed)

                    # DVD FIRST-PLAY DIAGNOSTIC: chapter changes aren't tracked
                    # for normal files, but on a dvdnav:// disc they're the
                    # finest-grained signal of the First-Play PGC / warning /
                    # copyright / intro / preview / menu / title walk. ISO
                    # sessions only (guarded by is_dvd_iso) so ordinary
                    # channels stay quiet. This is purely observational --
                    # it never issues a navigate/seek, so it cannot change
                    # what the disc does.
                    self._current_chapter = -1

                    def _on_chapter_changed(event):
                        try:
                            self._current_chapter = self.player.get_chapter()
                        except Exception:
                            pass
                        if getattr(self, "is_dvd_iso", False):
                            self._log_dvd_transition("CHAPTER")

                    self._on_chapter_changed = _on_chapter_changed
                    _em.event_attach(vlc.EventType.MediaPlayerChapterChanged,
                                     self._on_chapter_changed)

                    # Same treatment for player state (Playing/Paused/Ended/
                    # Error/Stopped) -- _seek_when_ready/_enter_title_when_ready
                    # used to poll get_state() in the same sleep(20) loop as
                    # title/length; now all three live under one Condition
                    # so those waits can react the instant ANY of them
                    # changes instead of on the next 20ms tick.
                    self._current_state = None

                    def _on_state_changed(event):
                        with self._state_cond:
                            try:
                                self._current_state = self.player.get_state()
                            except Exception:
                                pass
                            self._state_cond.notify_all()
                        if getattr(self, "is_dvd_iso", False):
                            self._log_dvd_transition("STATE")

                    self._on_state_changed = _on_state_changed
                    for _evt in (vlc.EventType.MediaPlayerPlaying,
                                 vlc.EventType.MediaPlayerPaused,
                                 vlc.EventType.MediaPlayerEndReached,
                                 vlc.EventType.MediaPlayerEncounteredError,
                                 vlc.EventType.MediaPlayerStopped,
                                 vlc.EventType.MediaPlayerOpening):
                        _em.event_attach(_evt, self._on_state_changed)

                    # Create bound references to class-level callback instances
                    self._lock_cb = vlc.CallbackDecorators.VideoLockCb(self._vid_lock_callback)
                    self._unlock_cb = vlc.CallbackDecorators.VideoUnlockCb(self._vid_unlock_callback)
                    self._display_cb = vlc.CallbackDecorators.VideoDisplayCb(self._vid_display_callback)
                        
                    self.player.video_set_callbacks(self._lock_cb, self._unlock_cb, self._display_cb, None)
                    self.player.video_set_format("BGRA", self.width, self.height, self.width * 4)
                    
                    try:
                        self.player.video_set_aspect_ratio(self.target_aspect)
                    except Exception as _ar_err:
                        print(f"[VLC] Could not set aspect ratio: {_ar_err}")
                    self.ready_flag = True

                def _vid_lock_callback(self, opaque, planes):
                    planes[0] = self.pixel_pointer
                    return None

                def _vid_unlock_callback(self, opaque, picture, planes):
                    return None

                def _vid_display_callback(self, opaque, picture):
                    if self._warmup_skip > 0:
                        # Discard this frame -- decode hasn't stabilized yet
                        # (see _warmup_skip's comment in __init__). Leave
                        # _frame_ready False so get_display_frame() keeps
                        # returning None and the caller falls back to the
                        # last good cached frame instead of showing this one.
                        self._warmup_skip -= 1
                        return None
                    self._frame_ready = True
                    return None
                    
                def _log_dvd_transition(self, tag):
                    """One-line timestamped dump of the disc's current dvdnav
                    position (title / chapter / state / counts). Fired from the
                    libVLC title/chapter/state callbacks for .iso sessions only.
                    This is the trace that reveals whether the disc walks its
                    First-Play / warning / copyright / intro / preview PGCs or
                    jumps straight to the root menu the moment dvdnav:// opens.
                    Read-only: it never navigates or seeks."""
                    try:
                        import time as _tm
                        p = self.player
                        print(f"[DVD TRACE] {tag} t+{_tm.monotonic():.3f} "
                              f"title={p.get_title()}/{p.get_title_count()} "
                              f"chapter={p.get_chapter()}/{p.get_chapter_count()} "
                              f"len={p.get_length()}ms state={p.get_state()}")
                    except Exception as e:
                        print(f"[DVD TRACE] {tag} logging failed: {e!r}")

                def _dump_dvd_title_map(self, timeout_ms=8000):
                    """Once dvdnav has parsed the disc, dump the full title/menu
                    table a single time so the [DVD TRACE] transitions can be
                    read against the disc's real structure (how many titles,
                    which look like menus, chapter counts). ISO diagnostic only;
                    runs in its own daemon thread so it never blocks playback."""
                    import time as _tm
                    try:
                        import vlc
                    except Exception:
                        return
                    deadline = pygame.time.get_ticks() + timeout_ms
                    while pygame.time.get_ticks() < deadline:
                        try:
                            if self.player.get_state() in (vlc.State.Playing,
                                                           vlc.State.Paused):
                                break
                        except Exception:
                            pass
                        _tm.sleep(0.05)
                    try:
                        descs = self.player.get_full_title_descriptions()
                        print(f"[DVD TRACE] TITLE-MAP count={len(descs) if descs else 0}")
                        for i, d in enumerate(descs or []):
                            print(f"[DVD TRACE]   title[{i}] "
                                  f"name={getattr(d, 'name', None)!r} "
                                  f"dur={getattr(d, 'duration', None)} "
                                  f"flags={getattr(d, 'flags', None)}")
                    except Exception as e:
                        print(f"[DVD TRACE] TITLE-MAP failed: {e!r}")
                    self._log_dvd_transition("OPEN")

                def play_file_segmented(self, filepath, seek_seconds, gain=1.0):
                    """Returns True if playback was actually handed to VLC,
                    False if it wasn't (missing file or a set_media/play
                    failure). Callers MUST check this instead of assuming
                    success -- a silent no-op here used to leave
                    is_playing_video set True with nothing actually decoding,
                    which is what produced the black-screen bug.

                    `gain` is this file's loudness-normalization multiplier
                    (from the ffmpeg loudness probe, looked up by the caller).
                    It's stashed on the engine and applied by every future
                    set_volume() call for as long as this file stays loaded,
                    so the global volume slider/mute toggle/etc. don't need
                    to know anything about per-file gain to keep respecting it.

                    IMMEDIATE APPLY: set_volume() is only ever called again
                    on the NEXT slider/mute action, so just stashing file_gain
                    here left this file playing at the PREVIOUS file's gain
                    until the user happened to touch the volume -- which is
                    what made it look like loudness correction "needed" a
                    volume nudge to kick in. Re-issuing set_volume() with the
                    current slider/mute state right here bakes the new gain
                    into the player immediately, the same way the precache
                    hot-patch does for a file that's already playing (see
                    the [PRECACHE] Hot-patched comment below)."""
                    self.file_gain = gain if gain and gain > 0 else 1.0
                    try:
                        self.set_volume(
                            db.config.get("global_volume", 70) if db else 70,
                            db.config.get("is_muted", False) if db else False,
                        )
                    except Exception as e:
                        log.warning("play_file_segmented: immediate gain apply failed: %s", e)
                    if not os.path.exists(filepath):
                        log.warning("play_file_segmented: file does not exist, refusing to play: %s", filepath)
                        return False

                    # BACKUP DVD .ISO HANDLING: a .iso is a raw disc image (an
                    # ISO9660/UDF filesystem containing a VIDEO_TS structure),
                    # not a video stream. Handing it straight to media_new()
                    # like a normal file used to be the freeze -- libVLC would
                    # try to demux the disc filesystem itself as if it were
                    # one long video elementary stream, find no valid
                    # container/codec at the top of the image, and just sit
                    # there buffering forever with no error.
                    #
                    # dvdnav:// tells libVLC to hand the image to the same
                    # libdvdnav/libdvdread engine real DVD hardware uses: it
                    # parses the VIDEO_TS IFO tables, opens on the disc's
                    # menu (or straight to the feature for menu-less
                    # backups), and responds to player.navigate() for
                    # up/down/left/right/activate -- which is what makes menu
                    # navigation possible at all. Modern libdvdread reads
                    # directly out of the .iso file; no mounting required.
                    #
                    # is_dvd_iso is reported back to the caller so game_deck's
                    # DVD-menu input layer knows this file may open on an
                    # interactive menu (see dvd_is_disc_image / dvd_in_menu).
                    is_dvd_iso = os.path.splitext(filepath)[1].lower() == ".iso"
                    self.is_dvd_iso = is_dvd_iso
                    self._last_menu_signal = None
                    # New disc -> discard the previous disc's title menu-flag
                    # map so is_menu_active() rebuilds it for this image.
                    self._dvd_title_menu_flags = None

                    if is_dvd_iso:
                        _iso_mrl_path = os.path.abspath(filepath).replace("\\", "/")
                        media_source = "dvdnav:///" + _iso_mrl_path
                    else:
                        media_source = filepath
                    print(f"[VLC DEBUG] play_file_segmented: {os.path.basename(filepath)} "
                          f"-> routed as {'dvdnav (disc image)' if is_dvd_iso else 'plain file'}, "
                          f"seek={seek_seconds}")

                    # ALWAYS stop before loading the next file, even if
                    # is_playing() already reports False. A normal
                    # episode-to-episode transition happens because the
                    # PREVIOUS file already reached VLC's own Ended state on
                    # its own, before this function is ever called for the
                    # next episode -- so is_playing() is already False here,
                    # and the old `if is_playing(): stop()` guard skipped
                    # stop() entirely for exactly that (extremely common)
                    # case. Calling set_media()/play() straight out of Ended
                    # without an intervening stop() is a known libVLC quirk:
                    # the custom video callbacks (our BGRA buffer pipeline)
                    # keep decoding/displaying frames fine, but the audio
                    # OUTPUT device can come up dead/detached -- picture
                    # plays, sound doesn't. That's exactly the "no audio
                    # after a new episode starts, fixed by changing the
                    # channel and back" bug: tuning away calls an explicit
                    # vlc_engine.stop() from outside, and tuning back then
                    # calls play_file_segmented() on a properly-stopped
                    # player, which re-attaches audio correctly.
                    # stop() is safe/idempotent to call even when the
                    # player is already stopped/ended, so there's no
                    # downside to always calling it here.
                    try:
                        self.player.stop()
                    except Exception as e:
                        log.warning("play_file_segmented: pre-emptive stop() failed: %s", e)

                    try:
                        media = self.instance.media_new(media_source)
                        self._frame_ready = False
                        # Zero out any bytes left over from whatever was
                        # previously decoded into this buffer, and arm the
                        # warm-up guard so the first couple of real decoded
                        # frames from the hardware decoder's negotiation
                        # window get discarded rather than displayed (see
                        # _warmup_skip's comment in __init__). Previously
                        # this buffer was never cleared between files at
                        # all -- clear_frame_buffer() existed but nothing
                        # called it.
                        self.clear_frame_buffer()
                        self._warmup_skip = 2
                        self.player.set_media(media)
                        self.player.play()
                    except Exception as e:
                        log.warning("play_file_segmented: VLC failed to start playback for %s: %s", filepath, e)
                        return False

                    if seek_seconds and seek_seconds > 0:
                        import threading as _t
                        _t.Thread(target=self._seek_when_ready,
                                  args=(seek_seconds,),
                                  kwargs={"is_disc_image": is_dvd_iso,
                                          "timeout_ms": 8000 if is_dvd_iso else 4000},
                                  daemon=True).start()

                    # DVD FIRST-PLAY DIAGNOSTIC: once the ISO is handed to the
                    # dvdnav:// access module, dump the disc's title map a
                    # single time so the [DVD TRACE] title/chapter/state lines
                    # can be read against the real disc structure. Own daemon
                    # thread, read-only -- does not affect playback.
                    if is_dvd_iso:
                        import threading as _t3
                        _t3.Thread(target=self._dump_dvd_title_map,
                                   daemon=True).start()

                    # AUDIO TRACK MEMORY: if the viewer previously picked a
                    # non-default audio track for this exact file (see
                    # "Change Current Playing Audio Track" in the channel
                    # sub-menu), re-apply it now. Track descriptions aren't
                    # available from libVLC until the media has actually
                    # started decoding, so this has to poll in a background
                    # thread the same way _seek_when_ready does rather than
                    # being set synchronously right after play().
                    self.current_filepath = filepath
                    import threading as _t2
                    _t2.Thread(target=self._apply_saved_audio_track_when_ready,
                               args=(filepath,), daemon=True).start()
                    return True

                def _apply_saved_audio_track_when_ready(self, filepath, timeout_ms=4000):
                    import vlc as _vlc
                    saved = _get_saved_audio_track(filepath)
                    if saved is None:
                        return
                    saved_id, saved_name = saved
                    deadline = pygame.time.get_ticks() + timeout_ms
                    tracks = []
                    while pygame.time.get_ticks() < deadline:
                        # Bail out if a newer play_file_segmented() call has
                        # already moved on to a different file -- don't stomp
                        # on whatever that later call is trying to set up.
                        if getattr(self, "current_filepath", None) != filepath:
                            return
                        try:
                            state = self.player.get_state()
                        except Exception:
                            return
                        if state in (_vlc.State.Ended, _vlc.State.Error, _vlc.State.Stopped):
                            return
                        if state in (_vlc.State.Playing, _vlc.State.Paused):
                            try:
                                tracks = self.player.audio_get_track_description() or []
                            except Exception:
                                tracks = []
                            if tracks:
                                break
                        pygame.time.wait(50)
                    else:
                        return

                    if not tracks or getattr(self, "current_filepath", None) != filepath:
                        return

                    # Prefer matching by the exact track id (stable for a given
                    # file's stream order). Fall back to matching by the
                    # track's display name in case the id ever shifts (e.g.
                    # after a re-encode changed stream order).
                    ids = [t[0] for t in tracks]
                    target_id = None
                    if saved_id in ids:
                        target_id = saved_id
                    elif saved_name:
                        for tid, tname in tracks:
                            _label = tname.decode("utf-8", "ignore") if isinstance(tname, bytes) else str(tname)
                            if _label == saved_name:
                                target_id = tid
                                break
                    if target_id is not None:
                        try:
                            self.player.audio_set_track(target_id)
                            log.info("[AUDIO TRACK] Restored saved track %s for %s", target_id, filepath)
                        except Exception as e:
                            log.warning("[AUDIO TRACK] Failed to restore saved track for %s: %s", filepath, e)

                def get_audio_tracks(self):
                    """Returns a list of (track_id, label) for every real
                    audio track on the currently loaded media (the libVLC
                    "Disable" pseudo-track, id -1, is filtered out -- there's
                    always at least one real track to pick from here)."""
                    try:
                        raw = self.player.audio_get_track_description() or []
                    except Exception:
                        raw = []
                    out = []
                    for tid, tname in raw:
                        if tid == -1:
                            continue
                        label = tname.decode("utf-8", "ignore") if isinstance(tname, bytes) else str(tname)
                        out.append((tid, label or f"Track {tid}"))
                    return out

                def get_current_audio_track(self):
                    try:
                        return self.player.audio_get_track()
                    except Exception:
                        return -1

                def set_audio_track(self, track_id):
                    try:
                        return self.player.audio_set_track(track_id) == 0
                    except Exception as e:
                        log.warning("set_audio_track: failed to set track %s: %s", track_id, e)
                        return False

                def _seek_when_ready(self, seek_seconds, timeout_ms=4000, is_disc_image=False):
                    import vlc as _vlc
                    target_ms = int(seek_seconds * 1000)
                    deadline = pygame.time.get_ticks() + timeout_ms
                    _still_in_menu_at_timeout = False
                    _timed_out = False
                    # Was a sleep(20) polling loop re-checking get_state()/
                    # get_length()/get_title() every 20ms. Now waits on the
                    # Condition those three are pushed into by real libVLC
                    # events (see __init__) -- wakes the instant any of
                    # them actually changes instead of up to 20ms late,
                    # which is what let a mid-movie resume target race
                    # ahead of the menu->title domain switch in the first
                    # place (DISC-IMAGE GUARD comment below is unchanged
                    # from before -- only the wait mechanism changed).
                    with self._state_cond:
                        while True:
                            remaining_ms = deadline - pygame.time.get_ticks()
                            if remaining_ms <= 0:
                                _timed_out = True
                                break
                            state = self._current_state
                            if state in (_vlc.State.Ended, _vlc.State.Error, _vlc.State.Stopped):
                                return
                            if state in (_vlc.State.Playing, _vlc.State.Paused) and self._current_length > 0:
                                # DISC-IMAGE GUARD: a dvdnav:// open lands on
                                # the disc's own menu FIRST (dvdnav-menu
                                # default is on), and that menu is
                                # frequently itself a real, playable video
                                # loop with a genuine nonzero get_length()
                                # -- EIKEN.iso's is 60000ms (see
                                # app_log.txt: "length=60000 title=0" right
                                # after a channel-return reopen). Without
                                # this guard, a mid-movie resume target
                                # (e.g. 112110ms) gets blindly set_time()'d
                                # against that 60s menu loop -- an
                                # out-of-range seek into the wrong domain
                                # entirely -- which is what was corrupting
                                # dvdnav's state into a black screen on
                                # channel-return, not a genuinely no-op
                                # seek like the caller assumed. Wait for
                                # title >= 1 (a real title actually
                                # playing, not the menu domain -- see
                                # is_menu_active's comment) before
                                # committing the seek.
                                if is_disc_image and self._current_title < 1:
                                    _still_in_menu_at_timeout = True
                                    self._state_cond.wait(remaining_ms / 1000.0)
                                    continue
                                _still_in_menu_at_timeout = False
                                break
                            self._state_cond.wait(remaining_ms / 1000.0)
                    if _timed_out:
                        # Timed out. Plain files: fire the seek anyway, same
                        # best-effort fallback as before this fix -- VLC
                        # will just clamp/ignore an invalid target. Disc
                        # images still sitting on the menu: skip it. If the
                        # user is still on the menu after 4s, there's no
                        # title domain to seek into yet -- committing here
                        # would reproduce the exact out-of-range-seek bug
                        # this guard exists to prevent.
                        if is_disc_image and _still_in_menu_at_timeout:
                            return
                    self.player.set_time(target_ms)
                    # FIXED (episode "restarts" instead of resuming/landing on
                    # schedule): this used to check ONCE, 100ms after the
                    # initial set_time(), and if that single check still
                    # missed the target by >1.5s it fired exactly one more
                    # set_time() and then gave up permanently -- no further
                    # verification, no further retries. Confirmed via real
                    # logs: a requested seek of 1360s (23 min in) left the
                    # player sitting at ~44s a full 46 REAL seconds later --
                    # i.e. the seek never actually landed at all, playback
                    # just silently kept running from ~0, and every
                    # anchor/resume calculation downstream (change_channel's
                    # leave-anchor snapshot, calculate_slotted_playback_state's
                    # resume math) faithfully built on that wrong position
                    # from then on. That's what read as "the episode
                    # restarted" -- the resume math was fine, the INITIAL
                    # seek silently failed to stick.
                    #
                    # Some files (large 1080p remuxes, especially on a
                    # secondary/network drive) genuinely need more than
                    # 100ms for the demuxer to become seek-ready after
                    # set_media()/play(). Retry with real verification for
                    # up to 3 seconds instead of giving up after one check --
                    # still bounded, so a file that's truly never going to
                    # seek (corrupt index, etc.) doesn't hang this thread
                    # forever, it just stops trying after the window closes.
                    _seek_deadline = pygame.time.get_ticks() + 3000
                    while pygame.time.get_ticks() < _seek_deadline:
                        pygame.time.wait(150)
                        try:
                            _cur = self.player.get_time()
                        except Exception as e:
                            log.warning("_seek_when_ready: get_time() failed during seek verification: %s", e)
                            break
                        if _cur is not None and _cur >= 0 and abs(_cur - target_ms) <= 1500:
                            break  # landed close enough -- done
                        try:
                            self.player.set_time(target_ms)
                        except Exception as e:
                            log.warning("_seek_when_ready: retry set_time failed: %s", e)
                            break
                    else:
                        try:
                            _final = self.player.get_time()
                        except Exception:
                            _final = None
                        log.warning(
                            "_seek_when_ready: seek to %sms never landed within 3s (ended near %sms) -- "
                            "playback may start from the wrong position for this file.",
                            target_ms, _final)

                def _enter_title_when_ready(self, title, timeout_ms=6000):
                    """CHANNEL-RETURN, MOVIE WAS PLAYING: a fresh dvdnav://
                    reopen always lands on the disc's root menu, not back in
                    the title (see change_channel's resume closure /
                    dvd_saved_title comment in game_deck.py). Waits for the
                    reopened menu to actually be ready, then jumps straight
                    into `title` via set_title() -- the same DEMUX_SET_TITLE
                    path confirmed against VLC's dvdnav.c source (Control()'s
                    `case DEMUX_SET_TITLE: ... dvdnav_title_play(dvdnav, i)`
                    for any i != 0) -- bypassing the menu entirely instead of
                    leaving the user stuck looking at it. Once get_title()
                    actually reflects the new title, the already-running
                    _seek_when_ready thread (spawned by play_file_segmented
                    whenever a seek target was given) picks it up on its own
                    and applies the saved+elapsed-time position -- this
                    method doesn't need to touch the seek itself."""
                    import vlc as _vlc
                    deadline = pygame.time.get_ticks() + timeout_ms
                    ready = False
                    with self._state_cond:
                        while True:
                            remaining_ms = deadline - pygame.time.get_ticks()
                            if remaining_ms <= 0:
                                break
                            state = self._current_state
                            if state in (_vlc.State.Ended, _vlc.State.Error, _vlc.State.Stopped):
                                return
                            if state in (_vlc.State.Playing, _vlc.State.Paused) and self._current_length > 0:
                                ready = True
                                break
                            self._state_cond.wait(remaining_ms / 1000.0)
                    if not ready:
                        return
                    try:
                        self.player.set_title(title if title and title >= 1 else 1)
                    except Exception as e:
                        log.warning("_enter_title_when_ready: set_title(%s) failed: %s", title, e)

                def stop(self):
                    if self.player: 
                        self.player.stop()

                # DVD MENU NAVIGATION: only meaningful for a disc image
                # opened via dvdnav:// (see is_dvd_iso / play_file_segmented).
                # libVLC's MediaPlayer.navigate() sends the same up/down/
                # left/right/activate signal a real DVD remote's menu keys
                # send -- libdvdnav interprets it against the disc's own
                # button layout (VIDEO_TS highlight regions), so the game_deck
                # UI never needs to know where the disc's buttons actually
                # are on screen. direction is one of "up"/"down"/"left"/
                # "right"/"activate".
                #
                # NOTE: this deliberately does NOT reference vlc.Navigate.
                # That enum only exists in some generated vlc.py bindings --
                # confirmed missing from the one bundled at
                # C:\RetroTVappTest\vlc ("module 'vlc' has no attribute
                # 'Navigate'" on every nav press, app_log.txt 01:17:36).
                # navigate() only ever needs the raw libvlc_navigate_mode_t
                # int, which is stable across all libVLC versions (it's a
                # plain C enum baked into libvlc.dll itself, not something
                # the Python binding generates) -- 0=activate, 1=up, 2=down,
                # 3=left, 4=right, 5=popup. Using the int directly works
                # regardless of which vlc.py happens to be bundled.
                _NAVIGATE_IDS = {
                    "activate": 0,
                    "up":       1,
                    "down":     2,
                    "left":     3,
                    "right":    4,
                    "popup":    5,
                }

                # DVD MENU-SIGNAL REGEXES: match libdvdnav's own real,
                # structured event lines in vlc-debug.log (--verbose=2
                # --file-logging, see vlc_args), not an internal trace line.
                # Both are DOTALL-free, line-anchored, and compiled once at
                # class-definition time.
                #
                #   DVDNAV_HIGHLIGHT block (3 lines):
                #       dvdnav debug: DVDNAV_HIGHLIGHT
                #       dvdnav debug:      - display=1
                #       dvdnav debug:      - buttonN=1
                #   display=1 means a button is genuinely highlighted/shown
                #   right now; buttonN is which one. This is the actual
                #   authoritative "is there a real button on screen" signal
                #   -- see the v1/v2 history note above for why the old
                #   "buttonUpdate not done" line (an internal trace, not a
                #   real state event) was the wrong thing to parse.
                #
                #   DVDNAV_CELL_CHANGE: fires on real PGC/cell navigation
                #   inside a menu domain -- confirmed to fire on at least
                #   one hop the HIGHLIGHT event alone missed, so it's kept
                #   as an independent corroborating signal, not a
                #   replacement.
                import re as _re_dvd_sig
                _DVD_HIGHLIGHT_RE = _re_dvd_sig.compile(
                    r"DVDNAV_HIGHLIGHT\s*\r?\n"
                    r"dvdnav debug:\s*- display=(\d+)\s*\r?\n"
                    r"dvdnav debug:\s*- buttonN=(\d+)"
                )
                _DVD_CELL_CHANGE_RE = _re_dvd_sig.compile(r"DVDNAV_CELL_CHANGE")

                def _dvd_sig_log(self, msg):
                    """Single consistent tag for every menu-signal decision
                    and state change, so a test log tells the whole story
                    without needing to be reverse-engineered by hand. Always
                    includes a full state dump."""
                    with self._state_cond:
                        title = self._current_title
                    now = pygame.time.get_ticks()
                    log.info(
                        "[DVD SIGNAL] %s | title=%s confirmed_title=%s "
                        "highlight_display=%s highlight_button=%s cell_changes=%d "
                        "highlight_events=%d ms_since_active=%d active_source=%s "
                        "ever_seen=%s ms_since_title_entered=%d",
                        msg, title, self._dvd_sig_confirmed_title,
                        self._dvd_sig_highlight_display, self._dvd_sig_highlight_button,
                        self._dvd_sig_cell_change_count, self._dvd_sig_highlight_count,
                        now - self._dvd_sig_last_active_tick, self._dvd_sig_last_active_source,
                        self._dvd_sig_ever_seen, now - self._dvd_title_entered_at,
                    )

                def note_dvd_menu_signal_active(self, source):
                    """Mark the rolling confirmation window as freshly
                    active, AND remember the current title as no-timeout
                    confirmed (see the v3.1 comment in __init__ for why the
                    rolling window alone isn't enough). Called from three
                    independent places: the DVDNAV_HIGHLIGHT parser and
                    DVDNAV_CELL_CHANGE parser below (both real libdvdnav
                    events), and note_menu_activity() in game_deck.py's
                    TIME-JUMP watcher (a fully independent, derived signal --
                    position vs. wall-clock -- that empirically catches
                    transitions the log-parsed events alone have missed).
                    Any ONE of these refreshes the window AND the confirmed
                    title; see _DVD_MENU_SIGNAL_WINDOW_MS /
                    _dvd_sig_confirmed_title."""
                    prev_source = self._dvd_sig_last_active_source
                    self._dvd_sig_last_active_tick = pygame.time.get_ticks()
                    self._dvd_sig_last_active_source = source
                    self._dvd_sig_ever_seen = True
                    with self._state_cond:
                        self._dvd_sig_confirmed_title = self._current_title
                    if prev_source != source:
                        self._dvd_sig_log(f"window refreshed by '{source}'")

                # Back-compat alias: game_deck.py's TIME-JUMP watcher already
                # calls note_menu_activity() -- keep that name working.
                def note_menu_activity(self):
                    self.note_dvd_menu_signal_active("time-jump")

                def _poll_dvd_menu_signals(self):
                    """Incrementally tails vlc-debug.log for libdvdnav's real
                    DVDNAV_HIGHLIGHT / DVDNAV_CELL_CHANGE events (see the
                    class-level regexes above and the v3 design note in
                    __init__). Cursor-based so this stays cheap to call every
                    frame from is_menu_active()/navigate() instead of
                    re-reading the whole (ever-growing, never-truncated)
                    file the way the diagnostic get_recent_vlc_log() does.
                    Throttled to once per ~120ms on top of that.

                    LAZY EOF INIT: the first call seeks straight to the
                    file's CURRENT end instead of reading from byte 0 -- we
                    only care about live signals from this point forward.
                    Starting from 0 previously stalled the render thread for
                    several real seconds on a file already containing prior
                    test sessions' worth of --verbose=2 logging (this poll
                    runs every frame from the instant playback starts) --
                    that showed up as a black-screen lag before the menu
                    appeared."""
                    now = pygame.time.get_ticks()
                    if now < self._dvd_sig_poll_next_tick:
                        return
                    self._dvd_sig_poll_next_tick = now + 120
                    path = getattr(self, "_vlc_debug_log_path", None)
                    if not path:
                        return
                    try:
                        size = os.path.getsize(path)
                    except Exception:
                        return
                    if self._dvd_sig_log_pos is None:
                        self._dvd_sig_log_pos = size
                        self._dvd_sig_log("log cursor initialized at EOF (lazy init)")
                        return
                    if size < self._dvd_sig_log_pos:
                        self._dvd_sig_log_pos = size  # file shrank/rotated
                        return
                    if size == self._dvd_sig_log_pos:
                        return  # nothing new since last poll
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(self._dvd_sig_log_pos)
                            chunk = f.read()
                            self._dvd_sig_log_pos = f.tell()
                    except Exception as e:
                        log.warning("_poll_dvd_menu_signals: log read failed: %s", e)
                        return
                    if not chunk:
                        return

                    hl_matches = self._DVD_HIGHLIGHT_RE.findall(chunk)
                    if hl_matches:
                        d_str, b_str = hl_matches[-1]  # most recent in this chunk
                        try:
                            d = int(d_str)
                            b = int(b_str)
                        except ValueError:
                            d = b = None
                        if d is not None:
                            self._dvd_sig_highlight_count += len(hl_matches)
                            self._dvd_sig_highlight_display = d
                            self._dvd_sig_highlight_button = b
                            if d >= 1:
                                self.note_dvd_menu_signal_active("highlight")
                            else:
                                self._dvd_sig_log(
                                    f"DVDNAV_HIGHLIGHT display=0 (button cleared, button={b})")

                    cell_matches = self._DVD_CELL_CHANGE_RE.findall(chunk)
                    if cell_matches:
                        self._dvd_sig_cell_change_count += len(cell_matches)
                        self.note_dvd_menu_signal_active("cell-change")

                def navigate(self, direction):
                    if not self.player:
                        return

                    if direction == "activate":
                        # GATE: only forward "activate" (dvdnav_button_
                        # activate on whatever button is currently
                        # selected) once the disc has actually confirmed a
                        # real button on screen. Two independent checks (see
                        # is_menu_active() for the fuller comment on why
                        # both exist): a no-timeout "is the CURRENT title
                        # exactly the one last confirmed" check first (covers
                        # sitting still and thinking, however long), then
                        # the rolling window as a fallback (covers a fast
                        # title-hop mid-navigation that hasn't gotten its
                        # own confirmation yet). Without this gate entirely,
                        # an early Enter press during content libVLC still
                        # reports as "menu domain" but where nothing is
                        # genuinely clickable yet would get forwarded
                        # straight through to dvdnav_button_activate() on
                        # whatever button index happens to be selected by
                        # default.
                        #
                        # BOOTSTRAP: gating runs unconditionally (not gated
                        # behind _dvd_sig_ever_seen) since a disc's very
                        # first Enter press necessarily fires before any
                        # signal has ever been confirmed. Bounded by
                        # _DVD_ACTIVATE_GRACE_MS measured from
                        # _dvd_title_entered_at, so a disc/VLC build that
                        # never emits either event still lets Enter through
                        # eventually rather than locking it out forever.
                        self._poll_dvd_menu_signals()
                        now = pygame.time.get_ticks()
                        with self._state_cond:
                            cur_title = self._current_title
                        _title_match = (self._dvd_sig_confirmed_title is not None
                                         and cur_title == self._dvd_sig_confirmed_title)
                        _window_fresh = (now - self._dvd_sig_last_active_tick) < self._DVD_MENU_SIGNAL_WINDOW_MS
                        _confirmed = _title_match or _window_fresh
                        if not _confirmed:
                            _elapsed = now - self._dvd_title_entered_at
                            if _elapsed < self._DVD_ACTIVATE_GRACE_MS:
                                self._dvd_sig_log(
                                    f"navigate(activate) HELD -- title doesn't match "
                                    f"confirmed_title and no fresh signal window "
                                    f"(grace {_elapsed}/{self._DVD_ACTIVATE_GRACE_MS}ms elapsed)")
                                return
                            self._dvd_sig_log(
                                f"navigate(activate) PASSED via safety fallback -- no "
                                f"signal confirmed after {_elapsed}ms grace period")
                        elif _title_match:
                            self._dvd_sig_log(
                                f"navigate(activate) PASSED -- current title matches "
                                f"no-timeout confirmed_title={self._dvd_sig_confirmed_title}")
                        else:
                            self._dvd_sig_log(
                                f"navigate(activate) PASSED -- signal confirmed "
                                f"{now - self._dvd_sig_last_active_tick}ms ago via "
                                f"'{self._dvd_sig_last_active_source}'")

                    if direction == "menu":
                        # RETURN TO DISC MENU: deliberately NOT routed through
                        # the _NAVIGATE_IDS "popup" (int 5) case below.
                        # libvlc_media_player_navigate's popup mode maps to
                        # VLC_PLAYER_NAV_POPUP, which only toggles an
                        # already-registered popup overlay on top of the
                        # title that's still playing underneath -- some
                        # discs (EIKEN.iso included, per app_log.txt: title
                        # stayed 1 the whole time) don't expose one at all,
                        # so it's a silent no-op and the screen never
                        # changes. It never touches the underlying title
                        # index, which is why get_title() stayed >= 1 and
                        # the auto-detect had to be forcibly frozen out to
                        # even show "menu navigation" in the log.
                        #
                        # set_title(0) is the actual "return to disc menu"
                        # command: it calls all the way down into VLC's
                        # dvdnav access module's Control(DEMUX_SET_TITLE, 0),
                        # which runs dvdnav_menu_call(DVD_MENU_Root) (falling
                        # back to DVD_MENU_Title) -- see
                        # modules/access/dvdnav.c in VLC's source. That's a
                        # real domain change, so get_title() genuinely drops
                        # to 0 afterward and is_menu_active() picks it up on
                        # its own -- the manual-override freeze becomes a
                        # safety net for the debounce window instead of the
                        # only thing making this work.
                        try:
                            self.player.set_title(0)
                        except Exception as e:
                            log.warning("DVD set_title(0) (return-to-menu) failed: %s", e)
                        return
                    nav_id = self._NAVIGATE_IDS.get(direction)
                    if nav_id is None:
                        log.warning("DVD menu navigate(%s): unknown direction", direction)
                        return
                    try:
                        self.player.navigate(nav_id)
                        self._dvd_sig_log(f"navigate({direction}) sent to libVLC (nav_id={nav_id})")
                    except Exception as e:
                        log.warning("DVD menu navigate(%s) failed: %s", direction, e)

                # DVD MENU DETECTION: libVLC doesn't expose a direct "is the
                # dvdnav menu currently showing" flag through python-vlc, so
                # this infers it from get_title().
                #
                # get_length() was tried first and rejected: EIKEN.iso's
                # menu is itself a real, playable video loop, so it reports
                # a genuine nonzero duration immediately -- length can't
                # tell "menu" from "movie" on a disc like that.
                #
                # get_title() looked unreliable too, from early testing --
                # it read 0 on every single sample taken while sitting on
                # the disc's menus. But that was because those samples
                # never actually reached real playback. Once real playback
                # was reached (app_log.txt, title=3 once the movie
                # actually started), the pattern became clear: get_title()
                # stays 0 (or -1, before dvdnav has entered any domain yet)
                # for as long as you're anywhere in the disc's menu system
                # -- top menu, or any submenu -- and only becomes a
                # specific positive title index once a real, playable
                # title is actually running. That makes title >= 1 the one
                # reliable "the movie (not a menu screen) is actually
                # playing" signal for this disc, and by extension the
                # right general rule: default to menu-nav, only grant
                # transport controls once a real title is confirmed
                # playing, per the user's explicit request.
                #
                # game_deck debounces this on top (see dvd_in_menu /
                # _dvd_menu_flip_since in _render_dvd_player) so a single
                # mid-transition frame can't flicker WASD's meaning back
                # and forth.
                def _ensure_menu_highlight_spu(self):
                    """NATIVE-PATH HIGHLIGHT FIX: libVLC's dvdnav access module
                    renders the DVD button highlight by compositing an SPU
                    (subpicture) stream on top of the decoded video -- it is
                    NOT a separate overlay layer we can query geometry from.
                    --no-osd being removed (see vlc_args) was necessary but
                    may not be sufficient: a fresh menu DOMAIN (root menu ->
                    a submenu is a real VTS/PGC change) can hand VLC a new
                    set of elementary streams with SPU deselected by default,
                    even though video/audio keep playing normally -- which
                    would show the menu picture but silently drop JUST the
                    highlight on that specific menu, exactly matching "some
                    menus highlight, some don't, menu to menu". This
                    re-selects SPU track 0 whenever we land back in a menu
                    domain (title < 1) and no SPU track is currently active,
                    so the highlight has a stream to render onto every time,
                    not only on first open. No-op (and cheap) if a track's
                    already selected, and never touches the title/length/
                    state tracking the boot-sequence guard (_seek_when_ready
                    / is_disc_image) relies on."""
                    if not self.is_dvd_iso:
                        return
                    if not self.player:
                        return
                    try:
                        with self._state_cond:
                            title = self._current_title
                        if title >= 1:
                            return  # real title playing, not a menu -- leave SPU alone
                        current_spu = self.player.video_get_spu()
                        if current_spu is not None and current_spu >= 0:
                            return  # already have an active SPU track -- nothing to do
                        count = self.player.video_get_spu_count()
                        if count and count > 0:
                            self.player.video_set_spu(0)
                            print(f"[DVD DEBUG] _ensure_menu_highlight_spu: menu domain "
                                  f"entered with no active SPU track (count={count}) -- "
                                  f"re-selected track 0 so the highlight can render here.")
                    except Exception as e:
                        log.warning("_ensure_menu_highlight_spu failed (non-fatal): %s", e)

                def is_menu_active(self):
                    if not self.player:
                        return False
                    self._poll_dvd_menu_signals()
                    # self._current_title is pushed by libVLC's own
                    # MediaPlayerTitleChanged event the instant dvdnav's
                    # state changes (see __init__), so this stays a cheap,
                    # never-stale read.
                    with self._state_cond:
                        title = self._current_title
                    now = pygame.time.get_ticks()
                    result = None
                    result_reason = None
                    # PRIMARY SIGNAL (no timeout): are we STILL sitting on
                    # the exact title that was last confirmed to have a real
                    # highlighted button? See the v3.1 comment in __init__ --
                    # dvdnav never explicitly clears DVDNAV_HIGHLIGHT
                    # (display never once dropped back to 0 across an entire
                    # session's worth of log), it just goes quiet while
                    # nothing changes, so ANY time-based decay eventually
                    # misreads "the user is thinking" as "the button is
                    # gone" -- confirmed happening exactly at the 4s mark on
                    # the Deal-or-No-Deal prompt (app_log.txt). No decay: as
                    # long as the title hasn't actually changed, the button
                    # confirmed on it is still trusted, however long ago
                    # that confirmation happened.
                    if self._dvd_sig_confirmed_title is not None and title == self._dvd_sig_confirmed_title:
                        result = True
                        result_reason = f"title matches no-timeout confirmed_title={self._dvd_sig_confirmed_title}"
                    # SECONDARY SIGNAL: the rolling menu-signal window (see
                    # the v3 design note in __init__ and
                    # _poll_dvd_menu_signals) -- fed by libdvdnav's own
                    # DVDNAV_HIGHLIGHT and DVDNAV_CELL_CHANGE events, plus
                    # game_deck.py's independent TIME-JUMP watcher. This is
                    # what covers a fast title-hop mid-navigation, where the
                    # CURRENT title has already moved on but hasn't gotten
                    # its own confirmation yet -- the no-timeout check above
                    # can't help there since it requires an exact match.
                    if result is None and (now - self._dvd_sig_last_active_tick) < self._DVD_MENU_SIGNAL_WINDOW_MS:
                        result = True
                        result_reason = (f"rolling signal window active "
                                          f"({now - self._dvd_sig_last_active_tick}ms since "
                                          f"'{self._dvd_sig_last_active_source}')")
                    # TERTIARY SIGNAL: does the CURRENT dvdnav title carry the
                    # menu/interactive flag? Menus can live at ANY title index
                    # on multi-menu / game discs -- Deal or No Deal authors its
                    # submenu as title 6 -- so the old "title < 1" rule reported
                    # "not a menu" the moment you left the root menu and every
                    # subsequent WASD/Enter press got dropped (app_log.txt:
                    # "menu_up=False -- nothing to navigate"). libVLC publishes
                    # each title's menu bit via get_full_title_descriptions();
                    # that's the same signal VLC's own UI uses.
                    if result is None:
                        _menu = self._title_is_menu(title)
                        if _menu is not None:
                            result = _menu
                            result_reason = f"static title-flags table says menu={_menu}"
                    # FALLBACK: title table not published yet (first frames
                    # right after open) -- treat title 0/-1 as the menu.
                    if result is None:
                        result = title < 1
                        result_reason = f"fallback: title<1 ({title})"
                    # Edge-triggered logging: only log when the final answer
                    # actually flips, not every single frame -- keeps the log
                    # readable while still catching every real transition.
                    if result != self._dvd_menu_active_last_logged:
                        self._dvd_sig_log(f"is_menu_active() -> {result} ({result_reason})")
                        self._dvd_menu_active_last_logged = result
                    return result

                def _title_is_menu(self, title):
                    """Return True/False if the disc has told us whether
                    `title` is a menu (dvdnav menu/interactive flag), or None
                    if the title table isn't available yet. Built once per disc
                    and cached in self._dvd_title_menu_flags -- the table never
                    changes for a given image, and get_full_title_descriptions()
                    is too heavy to call every render frame."""
                    flags_map = self._dvd_title_menu_flags
                    if not flags_map:
                        flags_map = {}
                        try:
                            descs = self.player.get_full_title_descriptions()
                        except Exception:
                            descs = None
                        if descs:
                            # libvlc title flag bits: menu = 0x1, interactive
                            # = 0x2. Either one means "this title is a menu
                            # screen, not a plain playable feature".
                            for i, d in enumerate(descs):
                                f = getattr(d, "flags", 0) or 0
                                flags_map[i] = bool(f & 0x3)
                            # Cache only once it's actually populated so a too-
                            # early empty read doesn't get frozen in.
                            self._dvd_title_menu_flags = flags_map
                    if not flags_map:
                        return None
                    if title < 0:
                        return True  # pre-domain (dvdnav not in a title yet)
                    return flags_map.get(title, title < 1)

                def _log_menu_signal_if_changed(self):
                    """Called from the MediaPlayerTitleChanged event
                    callback itself (see __init__) instead of being
                    re-derived every render frame -- one log line per
                    actual transition, driven by the same event that
                    updates _current_title, not a separate polling pass."""
                    with self._state_cond:
                        title = self._current_title
                        length = self._current_length
                    # Match is_menu_active() exactly: prefer the title menu
                    # flag, fall back to title < 1 only until the title table
                    # is available.
                    _menu = self._title_is_menu(title)
                    _active = (title < 1) if _menu is None else _menu
                    _sig = (_active,)
                    if _sig != self._last_menu_signal:
                        self._last_menu_signal = _sig
                        print(f"[DVD DEBUG] is_menu_active signal changed: length={length} title={title} flag_menu={_menu} -> {_active}")

                # DIAGNOSTIC: libVLC's own last-recorded error string (set
                # internally by libvlc_errmsg() whenever an access/demux
                # module reports a failure -- e.g. "cannot find access
                # module" if the dvdnav plugin isn't present in this VLC
                # install, vs. a genuinely unreadable/corrupt disc image).
                # State.Ended/State.Error alone can't tell those apart;
                # this is what actually can. Called by game_deck's
                # ISO-open watchdog the moment it detects a failed open,
                # so the resulting log line names the real cause instead
                # of just "State.Ended".
                def get_last_error(self):
                    try:
                        msg = vlc.libvlc_errmsg()
                        if msg:
                            return msg.decode("utf-8", "ignore") if isinstance(msg, bytes) else str(msg)
                    except Exception as e:
                        log.warning("get_last_error: libvlc_errmsg() call failed: %s", e)
                    return ""

                # DIAGNOSTIC: get_last_error() above relies on
                # libvlc_errmsg(), which turned out to return stale/generic
                # text ("No active input") for the SAW.ISO/Matrix.ISO
                # failures instead of the real dvdnav/dvdread error --
                # libvlc_errmsg() only reflects the last error recorded on
                # THIS thread, and the actual access-module failure can be
                # logged from a different internal VLC thread, so it never
                # lands there. This reads the tail of vlc-debug.log instead
                # (see --file-logging/--verbose=2 in vlc_args above, which
                # replaced --quiet specifically so this file gets populated
                # with the real module-level error text). Filtered to
                # DVD/access/demux-relevant lines so a failure report isn't
                # buried in routine decoder/video-output chatter.
                def get_recent_vlc_log(self, max_lines=15):
                    path = getattr(self, "_vlc_debug_log_path", None)
                    if not path or not os.path.exists(path):
                        return ""
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()
                    except Exception as e:
                        log.warning("get_recent_vlc_log: could not read %s: %s", path, e)
                        return ""
                    _keywords = ("dvdnav", "dvdread", "dvdcss", "css",
                                 "access", "demux", "error", "cannot", "fail")
                    relevant = [ln.rstrip("\n") for ln in lines[-300:]
                                if any(k in ln.lower() for k in _keywords)]
                    return "\n".join(relevant[-max_lines:])

                def clear_frame_buffer(self):
                    try:
                        ctypes.memset(self.raw_buffer, 0, self.buf_size)
                    except Exception as e:
                        log.warning("clear_frame_buffer: ctypes.memset failed: %s", e)
                        
                def set_volume(self, val, mute_flag=False):
                    if self.player:
                        # val comes in as the raw 0-100 slider position from
                        # every call site; curve it onto a perceptual scale
                        # (see _perceptual_volume_pct) before applying the
                        # per-file loudness-normalization multiplier, so a
                        # slider tick always changes perceived loudness by
                        # roughly the same amount instead of doing nothing
                        # from 100->50 and then diving from 50->15.
                        #
                        # Note on responsiveness: this call itself is
                        # instant, but how quickly it becomes AUDIBLE depends
                        # on --file-caching (see vlc_args above) -- that many
                        # ms of audio+video are decoded/queued ahead of what's
                        # currently playing, and whatever was already queued
                        # at the old volume has to finish draining first.
                        # That's the source of any perceived slider lag on
                        # this engine specifically (pygame paths don't have
                        # this lookahead buffer at all).
                        curved = _perceptual_volume_pct(val, min_db=VLC_GAIN_MIN_DB)
                        boosted = int(curved * self.file_gain)
                        # DIAGNOSTIC: the reported "0% audio below 65% slider" bug
                        # doesn't reproduce in the math above given file_gain's
                        # [0.4, 2.5] clamp in media.get_media_loudness_gain (and
                        # ffmpeg being absent means file_gain is ~1.0 for
                        # everyone right now, which shouldn't hit true zero until
                        # well under 5% slider). Logging the actual inputs here
                        # so the next repro pins down what's really happening
                        # instead of guessing further. Safe to remove once the
                        # real cause is confirmed.
                        log.info("[VOLUME DEBUG] slider=%s%% curved=%.2f file_gain=%.3f boosted=%d",
                                  val, curved, self.file_gain, boosted)
                        self.player.audio_set_volume(0 if mute_flag else max(0, min(200, boosted)))

                        # MUTE-STATE SYNC FIX: audio_set_volume() above and the
                        # player's real audio_set_mute() flag are two independent
                        # VLC controls. The transition/buffering hard-mute dance
                        # (change_channel's pre-play mute + the first-frame
                        # unmute checkpoint) only ever touches audio_set_mute(),
                        # and only reconciles it at the next channel switch's
                        # first real frame. That left a gap: toggling mute with
                        # the N key (or any other set_volume() caller) changed
                        # the volume but never touched audio_set_mute, so if the
                        # low-level flag happened to be stuck True (e.g. left
                        # muted by the checkpoint above), un-muting via N had no
                        # audible effect until a later channel change happened
                        # to land on a Video frame and clear it -- the "have to
                        # switch channels a few times before unmute registers"
                        # bug. Every set_volume() call now also forces the real
                        # mute flag to match mute_flag directly, so mute/unmute
                        # is instant regardless of what the transition dance
                        # last left it at.
                        try:
                            self.player.audio_set_mute(bool(mute_flag))
                        except Exception as e:
                            log.warning("set_volume: audio_set_mute sync failed: %s", e)
                        
                def get_display_frame(self):
                    import vlc as _vlc
                    s = self.player.get_state()
                    live = (_vlc.State.Playing, _vlc.State.Paused)
                    if s in live and self._frame_ready and self.raw_buffer is not None:
                        # Consume the flag: it's only set True again by
                        # _vid_display_callback when VLC hands us a genuinely
                        # NEW decoded frame. Video plays at its native fps
                        # (commonly 24-30) while the game loop polls at up to
                        # 60Hz, so without this, the same already-shown frame
                        # was being re-copied out of the buffer (a full
                        # 1-3.6MB memcpy via bytes()) and re-processed/
                        # re-smoothscaled on every extra poll for nothing.
                        # Callers already fall back to _blit_last_video_frame()
                        # (the cached, already-processed previous frame) when
                        # this returns None, so returning None here on repeat
                        # polls is free -- it just skips redundant work instead
                        # of producing a blank frame.
                        self._frame_ready = False
                        return bytes(self.raw_buffer)
                    return None

            vlc_engine = VLCEngineWrapper()
            print("[DEBUG VLC MASTER] Core framework ready and operational.")

            # Second, independent engine dedicated to the DVD .iso player.
            # It has its own libVLC instance/player/frame buffer, so the disc
            # can keep running (just muted) in the background while the main
            # vlc_engine plays a different channel's audio. game_deck routes
            # ALL of its DVD operations through this engine (see the two
            # main_module.dvd_engine grabs in game_deck.py).
            dvd_engine = VLCEngineWrapper()
            print("[DEBUG VLC MASTER] Dedicated DVD engine ready and operational.")

            if game_deck is not None:
                # game_deck.vlc_engine is used EXCLUSIVELY for DVD playback,
                # so it points at the dedicated dvd_engine, never the shared
                # main engine.
                game_deck.vlc_engine = dvd_engine
                game_deck.dvd_gain_lookup = _get_or_queue_dvd_gain
                
        except Exception as e:
            print(f"[VLC INITIALIZE FAILURE] Failed to load libVLC module hooks: {e}")
            vlc_engine = None
            dvd_engine = None

_dvd_probe_inflight = set()


def _get_or_queue_dvd_gain(file_path):
    """Called by ExternalGameDeck (via game_deck.dvd_gain_lookup) right
    before starting/resuming DVD playback. DVDs aren't part of any
    channel's "schedules" -- they're picked ad-hoc via the file explorer --
    so their probed gains live in their own small persistent cache instead:
    db.config["dvd_gain_cache"] = {normalized_path: gain}.

    Returns the cached gain immediately if this exact file has been probed
    before (so a DVD you've already watched once starts pre-normalized
    right away, no re-scan). If it hasn't, returns 1.0 (unchanged) so
    playback is never delayed, and kicks off a background probe that
    patches the cache -- and, if this DVD is STILL the one playing when the
    probe finishes, nudges the live VLC engine's gain too, so a first-time
    DVD corrects itself mid-play instead of requiring a restart.
    """
    if db is None or not file_path or not os.path.exists(file_path):
        return 1.0
    norm_p = os.path.normpath(file_path)
    cache = db.config.setdefault("dvd_gain_cache", {})
    cached = cache.get(norm_p)
    if cached:
        return cached
    if os.path.splitext(norm_p)[1].lower() == ".iso":
        # ffmpeg's loudnorm probe reads a plain audio/video elementary
        # stream -- it can't parse a VIDEO_TS/IFO disc structure any more
        # than play_file_segmented()'s old direct-file path could (see the
        # dvdnav:// routing comment there). Probing would just fail after
        # spawning ffmpeg for nothing, so skip straight to the same 1.0
        # default that failure would eventually return anyway, and cache
        # it immediately -- no per-launch subprocess cost for disc images.
        print(f"[DVD DEBUG] Skipping loudness probe for disc image {os.path.basename(norm_p)} (gain=1.0).")
        cache[norm_p] = 1.0
        return 1.0
    if norm_p in _dvd_probe_inflight:
        return 1.0
    _dvd_probe_inflight.add(norm_p)

    def _probe_and_apply():
        try:
            gain = get_media_loudness_gain(norm_p)
            cache[norm_p] = gain
            try:
                db.save_settings()
            except Exception as e:
                log.warning("DVD gain cache save failed: %s", e)
            if (vlc_engine is not None
                    and game_deck is not None
                    and getattr(game_deck, "dvd_playback_active", False)
                    and os.path.normpath(getattr(game_deck, "dvd_file_path", "") or "") == norm_p):
                vlc_engine.file_gain = gain
                try:
                    vlc_engine.set_volume(
                        db.config.get("global_volume", 70),
                        db.config.get("is_muted", False),
                    )
                except Exception as e:
                    log.warning("DVD gain hot-patch: immediate apply failed: %s", e)
            print(f"[DVD LOUDNESS] {os.path.basename(norm_p)}: gain x{gain:.2f} (cached for future plays)")
        finally:
            _dvd_probe_inflight.discard(norm_p)

    threading.Thread(target=_probe_and_apply, daemon=True).start()
    return 1.0


def _paths_from_entry(entry):
    """A schedule/timeline entry usually represents one file, but a paired
    short-episode entry (see media.pair_short_episodes) represents TWO real
    files aired back-to-back as a single slot. Returns every real,
    on-disk-checkable path this entry actually stands for."""
    if not isinstance(entry, dict):
        return [str(entry)] if entry else []
    if entry.get("is_pair"):
        out = []
        p1 = entry.get("pair_ep1_path", "")
        p2 = entry.get("pair_ep2_path", "")
        if p1:
            out.append(p1)
        if p2:
            out.append(p2)
        return out
    p = entry.get("path", entry.get("file", ""))
    return [p] if p else []


def _get_channel_ordered_content(ch_key, ch_info):
    """Returns (ordered_list, block_key): the same ordered lineup
    calculate_slotted_playback_state itself draws from for this channel
    right now (Marathon's fixed full-catalog list, Full Day/Morning/Evening/
    Night's rotation, or a visualizer's shuffled Music Tracks), plus the
    schedules[] bucket name it came from so a probed gain can be written
    back to the REAL stored entry. This is a read-only PEEK -- it never
    advances show_pos or writes anything -- so calling it repeatedly from
    the pre-cache scheduler can never desync actual playback.

    Returns ([], None) if the channel has no orderable content right now.
    """
    now = datetime.datetime.now()

    if ch_info.get("is_visualizer", False):
        schedule_stack = ch_info.get("schedules", {}).get("Music Tracks", [])
        track_list = []
        for asset in schedule_stack:
            path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
            if path and _cached_path_exists(path):
                track_list.append(asset if isinstance(asset, dict) else {"path": path})
        if not track_list:
            return [], None
        # Same deterministic per-day shuffle as calculate_slotted_playback_state's
        # visualizer branch, so "what's next" always matches what will actually air.
        day_seed = now.year * 1000 + now.timetuple().tm_yday + int(str(ch_key).zfill(2)) + 400
        rng = random.Random(day_seed)
        rng.shuffle(track_list)
        return track_list, "Music Tracks"

    scheduling_mode = ch_info.get("scheduling_mode", "random_slots")
    is_marathon = scheduling_mode == "marathon"
    is_full_day = (not is_marathon) and (ch_info.get("block_mode", "full_day") == "full_day")

    if is_marathon:
        block_name = "Marathon"
    elif is_full_day:
        block_name = "Full Day"
    else:
        hr = now.hour
        block_name = "Morning" if 5 <= hr < 13 else ("Evening" if 13 <= hr < 21 else "Night")

    if is_marathon:
        folder_items = ch_info.get("schedules", {}).get("Marathon", [])
        ordered = build_ordered_tracks(folder_items, "natural", 0)
        if not ordered:
            return [], None
        pt = max(0, ch_info.get("pair_threshold_minutes", 15)) * 60
        return pair_short_episodes(ordered, threshold_seconds=pt), "Marathon"

    folder_items = ch_info.get("schedules", {}).get(block_name, [])
    raw_tracks = []
    _media_probe_cache = (db.config.get("media_probe_cache", {}) if db is not None else {})
    for item in folder_items:
        p = item.get("path", item.get("file", "")) if isinstance(item, dict) else str(item)
        if p and _cached_path_exists(p):
            # SINGLE SOURCE OF TRUTH (media.resolve_track_duration) — see the
            # main scheduler's raw_tracks builder for why this must always
            # check the probe cache instead of trusting the stored value.
            stored = item.get("duration", 0) if isinstance(item, dict) else 0
            d = resolve_track_duration(_media_probe_cache, p, stored)
            track = {"path": p, "duration": d}
            # Carry forward an already-probed gain from the REAL stored
            # entry. Without this, every entry rebuilt here looks freshly
            # unprobed (no "gain" key at all) even for files _patch_probed_gain
            # already wrote a gain back to -- so _gather_probe_priority_queue's
            # `if "gain" in entry: continue` skip-check never fires for any
            # regular (non-Marathon) channel, and _probe_upcoming_media just
            # keeps re-probing the same current+3 files forever instead of
            # going idle after one pass.
            if isinstance(item, dict) and "gain" in item:
                track["gain"] = item["gain"]
            raw_tracks.append(track)
    if not raw_tracks:
        return [], None

    block_offset = {"Morning": 100, "Evening": 200, "Night": 300, "Full Day": 500}.get(block_name, 400)
    day_seed = now.year * 1000 + now.timetuple().tm_yday + int(str(ch_key).zfill(2)) + block_offset
    playback_log = ch_info.get("playback_log", {})
    pair_thresh_sec = max(0, ch_info.get("pair_threshold_minutes", 15)) * 60

    # CURRENTLY-PLAYING STARVATION FIX: this used to ALWAYS re-derive a fresh
    # rotation via build_show_rotation() below, ignoring whatever lineup was
    # actually committed to playback_log["sched_cached_tracks"] for the block
    # that's live right now. calculate_slotted_playback_state() itself never
    # does that once a block is committed -- it replays sched_cached_tracks
    # verbatim (see its "not _is_new_block" branch) specifically so a file
    # already airing never gets swapped out from under the viewer. Rebuilding
    # fresh here instead meant this function's "ordered" list could be a
    # DIFFERENT episode sequence than what's really on screen (different
    # show_pos advancement, different shuffle outcome) -- so current_file
    # (the file calculate_slotted_playback_state says is airing RIGHT NOW)
    # could fail to appear in it at all. _next_n_from_ordered() then silently
    # fell back to "start from the top", queuing four unrelated files for
    # probing while the file actually on screen was skipped every single
    # pass -- its duration never got probed, calculate_slotted_playback_state
    # kept using the 1800s placeholder for it indefinitely, and any watchdog
    # skip-past math built on that placeholder landed nowhere near the real
    # end of the episode. Reconstructing from the SAME committed cache this
    # peek is supposed to mirror fixes that at the source: the currently
    # airing file is now guaranteed to be tier 0 whenever a block is already
    # committed, exactly like the real scheduler sees it.
    block_key = f"{now.year}_{now.timetuple().tm_yday}_{block_name}"
    if playback_log.get("sched_block_key") == block_key:
        _cached_paths = playback_log.get("sched_cached_tracks", [])
        _by_path = {t["path"]: t for t in raw_tracks}
        _raw_ordered = [_by_path[p] for p in _cached_paths if p in _by_path]
        if _raw_ordered:
            _raw_ordered = _apply_cached_durations(
                _raw_ordered, playback_log.get("sched_cached_durations", {}))
            video_tracks = pair_short_episodes(_raw_ordered, threshold_seconds=pair_thresh_sec)
            if video_tracks:
                return video_tracks, block_name
            # Cache existed but expanded to nothing playable (e.g. every cached
            # path vanished from disk) -- fall through to a fresh rebuild below.

    show_pos_copy = dict(playback_log.get("show_pos", {}))
    fixed_order = playback_log.get("show_order", {}).get(block_name)
    # Same block-length lookup calculate_slotted_playback_state uses (see its
    # "TIME-BLOCK BASED VIDEO SCHEDULING" section) so movie counts here always
    # match what actually airs -- this peek is read-only and must never fall
    # out of sync with the real scheduler's idea of how long this block is.
    _block_len_lut = {"Marathon": 86400, "Full Day": 86400, "Morning": 8 * 3600, "Evening": 8 * 3600, "Night": 8 * 3600}
    block_duration_seconds = _block_len_lut.get(block_name, 8 * 3600)
    video_tracks, _, _ = build_show_rotation(
        raw_tracks, scheduling_mode, show_pos_copy, day_seed,
        pair_threshold_seconds=pair_thresh_sec,
        episode_order_mode=ch_info.get("episode_order_mode", "sequential"),
        fixed_show_order=fixed_order,
        block_duration_seconds=block_duration_seconds
    )
    return video_tracks, block_name



def _next_n_from_ordered(ordered, current_file, n=4):
    """Given an ordered content list and the file currently airing, returns
    up to n entries starting at the current one (current + the next n-1),
    wrapping around the list. If current_file can't be found (e.g. an
    anchor is holding an older pick that's since rotated out of the list),
    starts from the top instead so there's still a useful lookahead."""
    if not ordered:
        return []
    start_idx = 0
    for i, e in enumerate(ordered):
        if current_file and current_file in _paths_from_entry(e):
            start_idx = i
            break
    L = len(ordered)
    return [ordered[(start_idx + k) % L] for k in range(min(n, L))]


def _gather_probe_priority_queue():
    """Builds the 4-tier probe queue described in _probe_upcoming_media's
    docstring. Each tier item is (kind, ch_key, block_key_or_playlist_idx,
    normalized_path); "kind" says which patch path _patch_probed_gain
    should use to write the result back to the real stored entry."""
    tiers = [[], [], [], []]
    if db is None:
        return tiers

    # --- Currently-playing DVD, if any: always tier 0 (it's playing right now) ---
    if (game_deck is not None and getattr(game_deck, "dvd_playback_active", False)
            and getattr(game_deck, "dvd_file_path", "")):
        dvd_p = game_deck.dvd_file_path
        if os.path.exists(dvd_p):
            norm_dvd = os.path.normpath(dvd_p)
            if norm_dvd not in db.config.get("dvd_gain_cache", {}) and norm_dvd not in _dvd_probe_inflight:
                tiers[0].append(("dvd", None, None, norm_dvd))

    # --- Channel 03: game/menu music playlist ---
    if db.config.get("game_channel_enabled", False) and db.config.get("ch03_menu_music_enabled", True):
        playlist = db.config.get("ch03_menu_music", [])
        if playlist:
            cur_idx = app_state.get("ch03_menu_music_idx", 0) % len(playlist)
            for k in range(min(4, len(playlist))):
                idx = (cur_idx + k) % len(playlist)
                entry = playlist[idx]
                # Plain-string entries (the format this playlist has always
                # used) get migrated to a {"path": ...} dict the first time
                # they're probed -- see _patch_probed_gain -- so nothing
                # here needs to special-case the un-migrated shape beyond
                # reading the path out of either form.
                p = entry.get("path", "") if isinstance(entry, dict) else str(entry)
                if isinstance(entry, dict) and "gain" in entry:
                    continue
                if p and os.path.exists(p):
                    tiers[k].append(("ch03_menu_music", None, idx, os.path.normpath(p)))

    # --- Channels 05-44: TV/visualizer stations ---
    for i in range(5, 45):
        ch_str = str(i).zfill(2)
        ch_info = db.channels_db.get(ch_str, {})
        if not ch_info or not ch_info.get("active", True):
            continue
        try:
            now_state = calculate_slotted_playback_state(ch_str)
        except Exception:
            continue
        current_file = now_state.get("file", "")
        ordered, block_key = _get_channel_ordered_content(ch_str, ch_info)
        if not ordered or not block_key:
            continue
        upcoming = _next_n_from_ordered(ordered, current_file, n=4)
        for k, entry in enumerate(upcoming):
            for p in _paths_from_entry(entry):
                if not (p and os.path.exists(p)):
                    continue
                norm_p = os.path.normpath(p)
                # Consult the GLOBAL cross-channel cache (not just this entry's own
                # "gain" key) so a file already probed via a different channel is
                # never re-queued here -- this is what lets a song probed on
                # channel 11 be instantly known-good when it later rotates onto
                # channel 14, instead of every channel independently re-probing
                # the exact same physical file.
                _cached = _get_cached_media_probe(norm_p)
                has_gain = "gain" in _cached or (isinstance(entry, dict) and "gain" in entry)
                has_dur  = "duration" in _cached
                if has_gain and has_dur:
                    continue
                tiers[k].append(("channel", ch_str, block_key, norm_p))
    return tiers


def _patch_probed_gain(kind, ch_key, block_key, norm_p, gain=None, duration=None):
    """Writes a freshly-probed gain and/or duration back into the REAL stored
    entry (not a throwaway copy from _get_channel_ordered_content's rotation
    build), so it's actually persisted to disk on the next save_settings()
    and never re-probed again.

    Also always writes through to the global, path-keyed
    media_probe_cache (see _store_cached_media_probe) regardless of "kind" --
    that's the piece that lets a song probed while airing on channel 11
    already show up known-good the moment it later rotates onto channel 14,
    instead of every channel independently re-probing the same physical
    file.
    """
    _store_cached_media_probe(norm_p, duration=duration, gain=gain)
    if kind == "dvd":
        if gain is not None:
            db.config.setdefault("dvd_gain_cache", {})[norm_p] = gain
            if (vlc_engine is not None
                    and game_deck is not None
                    and getattr(game_deck, "dvd_playback_active", False)
                    and os.path.normpath(getattr(game_deck, "dvd_file_path", "") or "") == norm_p):
                vlc_engine.file_gain = gain
                try:
                    vlc_engine.set_volume(
                        db.config.get("global_volume", 70),
                        db.config.get("is_muted", False),
                    )
                except Exception as e:
                    log.warning("_patch_probed_gain (dvd): immediate apply failed: %s", e)
        return
    if kind == "ch03_menu_music":
        if gain is not None:
            playlist = db.config.get("ch03_menu_music", [])
            idx = block_key
            if 0 <= idx < len(playlist):
                entry = playlist[idx]
                if not isinstance(entry, dict):
                    entry = {"path": entry}
                    playlist[idx] = entry
                entry["gain"] = gain
        return
    ch_info = db.channels_db.get(ch_key, {})

    # --- GUIDE/SCHEDULE ACCURACY: propagate a freshly-learned REAL duration ---
    # A probe just measured this file's true length. Two stored copies of a
    # duration drive what the TV guide believes is on-air, and until now NEITHER
    # was updated here -- which is the root cause of the guide naming the wrong
    # episode/movie (worse the longer the real runtime differs from the 1800s
    # estimate default the anchor/schedule fall back to):
    #
    #   (a) db.channels_db[ch]["playback_anchor"]["duration"] -- the live anchor
    #       that calculate_slotted_playback_state() short-circuits on for the
    #       show that is ACTUALLY airing right now. It stored only an estimate.
    #       Correcting it just extends/trims the anchor's "remaining" window for
    #       the file genuinely on screen; "seek" is computed as seek_off+elapsed
    #       independent of duration, so this can never reseek or restart the show.
    #
    #   (b) playback_log["sched_cached_durations"][path] -- the per-airing frozen
    #       durations the guide's live-block parity path and the engine's re-tune
    #       path both replay so already-committed layout stays stable. Freezing an
    #       *estimate* is what made future slots drift; converging an existing
    #       frozen key onto the real value fixes "what's next / when" without
    #       injecting paths that weren't part of the committed rotation.
    if duration is not None and duration > 0 and isinstance(ch_info, dict):
        _real_dur = int(duration)
        _anchor = ch_info.get("playback_anchor")
        if isinstance(_anchor, dict) and _anchor.get("file"):
            try:
                if os.path.normpath(str(_anchor["file"])) == norm_p:
                    _anchor["duration"] = _real_dur
            except Exception:
                pass
        _plog = ch_info.get("playback_log")
        if isinstance(_plog, dict):
            _scd = _plog.get("sched_cached_durations")
            # Only converge keys already present -- never add new paths, so the
            # frozen cache keeps mirroring exactly the committed rotation. Keys
            # here are the RAW schedule-entry paths (see _snapshot_track_durations
            # / _apply_cached_durations), which may not be normalized, so match
            # on the normalized form rather than a direct `norm_p in _scd`.
            if isinstance(_scd, dict):
                for _k in list(_scd.keys()):
                    try:
                        if os.path.normpath(str(_k)) == norm_p and _scd.get(_k) != _real_dur:
                            _scd[_k] = _real_dur
                    except Exception:
                        pass

    for container_key in ("schedules", "holiday_schedules"):
        pool = ch_info.get(container_key, {}).get(block_key, [])
        for entry in pool:
            if not isinstance(entry, dict):
                continue
            # Standard entry: matched by its "path" or "file" key.
            _ep = os.path.normpath(entry.get("path", entry.get("file", "")))
            if _ep == norm_p:
                if gain is not None:
                    entry["gain"] = gain
                if duration is not None and duration > 0:
                    entry["duration"] = int(duration)
                return
            # Paired short-episode entry: two real files aired back-to-back
            # share one schedule slot. The slot dict uses pair_ep1_path /
            # pair_ep2_path instead of "path", so the check above never
            # matches it. Previously, paired content was NEVER written back
            # — gain stayed at 1.0 permanently and was re-queued every pass.
            if entry.get("is_pair"):
                _p1 = os.path.normpath(entry.get("pair_ep1_path", ""))
                _p2 = os.path.normpath(entry.get("pair_ep2_path", ""))
                if norm_p in (_p1, _p2):
                    if gain is not None:
                        # Store a per-episode gain keyed to which episode this
                        # path belongs to, so both can differ if they need to.
                        if norm_p == _p1:
                            entry["pair_ep1_gain"] = gain
                        else:
                            entry["pair_ep2_gain"] = gain
                        # Also stamp the top-level "gain" key with whichever
                        # episode we just probed — _lookup_file_gain uses this
                        # as the fallback average when a caller only has the
                        # pair slot (not the individual path), so it gets
                        # something real instead of 1.0.
                        if "gain" not in entry:
                            entry["gain"] = gain
                    if duration is not None and duration > 0:
                        if norm_p == _p1:
                            entry["pair_ep1_duration"] = int(duration)
                        else:
                            entry["pair_ep2_duration"] = int(duration)
                    return


def _probe_upcoming_media():
    """
    Continuous, priority-ordered loudness pre-cache.

    Rather than sweeping the whole library in arbitrary order (the old
    _backfill_loudness_gains), this repeatedly figures out what's ACTUALLY
    about to be heard and probes only that:
      tier 0 -- every channel's CURRENTLY PLAYING file (video, visualizer
                song, or game-menu-music track), covered first and in full
                before tier 1 is even looked at.
      tier 1 -- every channel's next-up pick.
      tier 2 -- the pick after that.
      tier 3 -- the pick after that.

    Anything already carrying both a known gain AND a known duration (either
    directly, or via the global cross-channel media_probe_cache -- see
    _get_cached_media_probe) is skipped outright -- once a file is probed it
    is NEVER re-scanned again, on this run or any future one, so a restart
    resumes exactly where this left off instead of re-sweeping anything.

    DURATION is folded into this exact same current+lookahead pass (not a
    separate slower sweep) precisely because an unprobed duration is what
    causes calculate_slotted_playback_state's wall-clock scheduler to cut a
    still-playing song/show short (or restart one that ended early) -- the
    file that's about to be heard/watched right now is the single most
    urgent thing to get a real duration for, on every channel, before
    anything deeper in anyone's queue.

    After a full pass (current + next 3, everywhere) is covered, this goes
    idle instead of scanning deeper into anyone's schedule. As shows
    advance and a new file rotates into the 3rd-slot-out position, the next
    periodic recheck picks up exactly that one new file -- nothing that's
    already done gets touched again.

    NOTE ON SCOPE: this covers real on-disk media -- TV station videos,
    visualizer songs, and the channel-03 menu-music playlist. It does NOT
    (and can't) cover audio from an actively-running game -- that's
    synthesized live by the emulator core frame-by-frame, so there is no
    file on disk to hand to ffmpeg.
    """
    import time as _time
    if db is None:
        return
    pygame.time.wait(1000)  # let boot settle and the first channel start playing first

    IDLE_SLEEP = 20.0       # how often to recheck for newly-revealed slots
    BETWEEN_PROBES = 0.5    # keep pacing gentle even though -vn made each probe cheap

    while True:
        tiers = _gather_probe_priority_queue()
        total_pending = sum(len(t) for t in tiers)
        if total_pending == 0:
            _time.sleep(IDLE_SLEEP)
            continue

        print(f"[MEDIA PRECACHE] {total_pending} upcoming file(s) need a duration/loudness "
              f"reading (current + next 3, every channel) -- probing now.")
        probed_this_pass = 0
        # PASS-LEVEL DEDUP: _gather_probe_priority_queue() only skips
        # re-queuing a path once it has BOTH a cached gain AND a cached
        # duration -- so a file that's scheduled as "upcoming" on several
        # channels at once (a song shared across multiple music channels'
        # playlists, a rerun airing on more than one channel, etc.) shows up
        # as multiple separate tier entries for the SAME norm_p, all built
        # before any of them have been probed yet. Without this, each of
        # those occurrences independently called ffmpeg/VLC on the identical
        # file within the same pass -- see app_log.txt/.1, where the same
        # single physical file times out 2-4 separate times over the run.
        # _this_pass_results remembers what THIS pass already found for a
        # given path (success OR the 1.0 fallback) so every later occurrence
        # in the same pass reuses it and still calls _patch_probed_gain (each
        # channel's own schedule entry still needs writing), just without
        # repeating the expensive probe itself.
        _this_pass_results = {}
        for tier_idx, tier_items in enumerate(tiers):
            for kind, ch_key, block_key, norm_p in tier_items:
                _cached = _get_cached_media_probe(norm_p)
                probed_dur  = _cached.get("duration")
                probed_gain = _cached.get("gain")
                _fresh = _this_pass_results.get(norm_p, {})
                if probed_dur is None:
                    probed_dur = _fresh.get("duration")
                if probed_gain is None:
                    probed_gain = _fresh.get("gain")
                # Duration only matters for real channel content (TV/visualizer
                # schedules) -- DVDs and the ch03 menu-music playlist don't feed
                # calculate_slotted_playback_state's per-channel timeline math,
                # so only their loudness is worth this pass's time.
                if kind == "channel" and probed_dur is None:
                    try:
                        probed_dur = get_media_duration(norm_p)
                    except Exception as e:
                        probed_dur = None
                        print(f"[DURATION PRECACHE] {norm_p}: {e}")
                if probed_gain is None:
                    try:
                        probed_gain = get_media_loudness_gain(norm_p)
                    except Exception as e:
                        probed_gain = None
                        print(f"[LOUDNESS PRECACHE] {norm_p}: {e}")
                if probed_dur is not None or probed_gain is not None:
                    _slot = _this_pass_results.setdefault(norm_p, {})
                    if probed_dur is not None:
                        _slot["duration"] = probed_dur
                    if probed_gain is not None:
                        _slot["gain"] = probed_gain
                if probed_gain or (probed_dur and probed_dur > 0):
                    _patch_probed_gain(kind, ch_key, block_key, norm_p, gain=probed_gain, duration=probed_dur)
                    probed_this_pass += 1
                    # HOT-PATCH: for the currently-playing channel's own file
                    # (tier 0), apply the freshly-probed gain to the live VLC
                    # engine immediately so the CURRENT play benefits, not just
                    # future ones. DVDs already get this treatment via
                    # _patch_probed_gain's dvd branch; regular TV video never
                    # did — the gain was only ever applied when the NEXT file
                    # started. A first-time play of an unprobed file would run
                    # the whole episode at 1.0 even though the probe finished
                    # after ~1-2s. Now it self-corrects as soon as the probe
                    # completes.
                    if (tier_idx == 0
                            and kind == "channel"
                            and probed_gain is not None
                            and probed_gain > 0
                            and vlc_engine is not None
                            and app_state.get("is_playing_video", False)
                            and str(app_state.get("current_channel", "")).zfill(2) == str(ch_key).zfill(2)):
                        try:
                            vlc_engine.file_gain = probed_gain
                            vlc_engine.set_volume(
                                db.config.get("global_volume", 70),
                                db.config.get("is_muted", False)
                            )
                            log.info("[PRECACHE] Hot-patched live VLC gain x%.2f for ch%s", probed_gain, ch_key)
                        except Exception as _hp_e:
                            log.warning("[PRECACHE] VLC gain hot-patch failed for ch%s: %s", ch_key, _hp_e)
                _time.sleep(BETWEEN_PROBES)
        if probed_this_pass:
            try:
                # ASYNC WRITE: this pass can (and on a large library
                # regularly did) fire a save every ~20s, each one a full
                # serialize+write of the whole config/channels blob. Doing
                # that synchronously on THIS thread was harmless to pygame's
                # loop directly, but it held _save_lock for the whole write,
                # and change_channel() used to make its own synchronous
                # db.save_settings() call on the MAIN thread for every
                # channel change -- so a save from here in flight could stall
                # a channel switch until this write finished. Using the async
                # writer here too means both sides only ever queue a
                # background write instead of contending over a long-held
                # lock on the main thread's time.
                db.save_settings_async()
            except Exception as e:
                log.warning("Settings save failed during media pre-cache: %s", e)
            print(f"[MEDIA PRECACHE] Probed {probed_this_pass} file(s) this pass.")
            # GUIDE CACHE BUST: any newly-probed duration means the guide's
            # cached timeline (keyed on guide_refresh_token) may have stale
            # 1800s placeholder durations baked in. Incrementing the token
            # once per pass forces a fresh build the next time the guide
            # renders, so movies/shows immediately show their real lengths
            # and correct names instead of waiting for the next viewport shift.
            # One increment per pass (not per file) is enough — the cache
            # rebuilds the whole visible window in one shot regardless.
            try:
                app_state["guide_refresh_token"] = app_state.get("guide_refresh_token", 0) + 1
            except Exception:
                pass
        _time.sleep(IDLE_SLEEP)


def _prewarm_boot_channel_paths():
    """Runs on its own daemon thread, started as soon as `db` is ready
    (i.e. concurrently with game_deck/visualizer_deck/VLC warm-up, not
    after it), so the os.path.exists() checks _get_channel_ordered_content()
    needs are already warm in _path_exists_cache by the time the loading
    bar dismisses and change_channel() runs synchronously on the main
    thread. Without this, a channel with hundreds/thousands of files hits
    every single one of those checks cold, on the main thread, in the
    exact frame the loading screen goes away -- which is the black screen
    the boot sequence otherwise shows right after a lot of media gets added.

    Only warms the channel that's actually about to load on boot (last_channel,
    same fallback-to-05 rule the boot sequence itself uses), since that's the
    only channel change_channel() will touch in that first synchronous call.
    (Channel 03 is never the boot channel -- the same fallback rule always
    redirects it to 05 -- and its menu music lives in a flat db.config list
    rather than channels_db[ch]["schedules"] anyway, so it isn't part of
    what _get_channel_ordered_content() needs warmed here.) Deliberately
    swallows all errors -- this is a pure optimization, never something the
    boot sequence should be able to fail on -- and always sets the done flag
    in a `finally` so a bug here can never hang the loading bar forever.
    """
    try:
        if db is None:
            return
        boot_ch = str(db.config.get("last_channel", "05")).zfill(2)
        if boot_ch in ("03", "04") or not boot_ch:
            boot_ch = "05"
        ch_info = db.channels_db.get(boot_ch)
        if ch_info:
            for block in ch_info.get("schedules", {}).values():
                for item in block:
                    p = item.get("path", item.get("file", "")) if isinstance(item, dict) else str(item)
                    if p:
                        _cached_path_exists(p)
    except Exception as e:
        print(f"[BOOT PREWARM] Path prewarm failed (non-fatal, boot continues normally): {e}")
    finally:
        app_state["_boot_prewarm_done"] = True


def _background_subsystem_worker():
    global db, game_deck, visualizer_deck, vlc_engine

    _BOOT_YIELD = 0.05

    print("[BOOT TIMING] Starting database...")
    # DETECTED_ASPECT_RATIO is passed IN rather than set on db.config right
    # here, because RetroDatabase.__init__ kicks off its settings load on a
    # background thread and returns immediately — setting it here raced
    # against that thread's "self.config = data.get('config', {})", which
    # replaces self.config wholesale and silently discarded this boot's
    # real detection in favor of a stale saved value. RetroDatabase now
    # re-stamps the value we hand it AFTER that replacement happens, so
    # there's no window where it can be lost. See RetroDatabase.__init__
    # in media.py for the full explanation.
    db = RetroDatabase(detected_aspect_ratio=DETECTED_ASPECT_RATIO)
    print("[BOOT TIMING] Database ready.")

    # Kick off the boot-channel path-exists prewarm the instant db is ready,
    # on its own thread, so it runs concurrently with game_deck/visualizer/VLC
    # warm-up below rather than after it. See _prewarm_boot_channel_paths().
    threading.Thread(target=_prewarm_boot_channel_paths, daemon=True).start()
    time.sleep(_BOOT_YIELD)

    # Re-apply the startup registry entry every boot if the toggle is on. This
    # is what keeps it working after the exe/folder gets moved (a real risk on
    # a repurposed old PC) — the Run key would otherwise still point at the
    # old, now-missing path.
    if db.config.get("start_on_boot", False):
        _set_start_on_boot(True)

    print("[BOOT TIMING] Creating game frontend deck...")
    try:
        from game_deck import ExternalGameDeck
        game_deck = ExternalGameDeck()
        print("[BOOT TIMING] Game frontend deck ready.")
        import game_deck as _gd_module
        _gd_module.pygame_hwnd_ref[0] = pygame_hwnd
        if db is not None:
            game_deck.refresh_from_config(db.config)
    except Exception as e:
        print(f"[GAME DECK] Failed to init game frontend: {e}")
        game_deck = None
    time.sleep(_BOOT_YIELD)

    print("[BOOT TIMING] Creating visualizer deck...")
    visualizer_deck = VisualizerDeck()
    print("[BOOT TIMING] Visualizer deck ready.")
    time.sleep(_BOOT_YIELD)

    print("[BOOT TIMING] Warming up VLC engine...")
    initialize_vlc_on_demand()
    print("[BOOT TIMING] VLC engine warm-up complete.")
    app_state["_joystick_init_done"] = True

    # LOUDNESS NORMALIZATION: continuous, priority-ordered pre-cache -- see
    # _probe_upcoming_media's docstring. Its own daemon thread, started here
    # rather than run inline, so it never delays boot or competes with
    # whatever channel starts playing first.
    threading.Thread(target=_probe_upcoming_media, daemon=True).start()

    print("[TELEMETRY - PART 2A] All background systems loaded.")

print("[BOOT TIMING] Launching background thread...")
threading.Thread(target=_background_subsystem_worker, daemon=True).start()

# ==============================================================================
# PART 4a OF 28: PRECISION TIMELINE CALCULATIONS
# ==============================================================================

def _safe_dur(entry, default=1800):
    """Return an entry's duration, treating -1 (unprobed sentinel) as default."""
    d = entry.get("duration", default) if isinstance(entry, dict) else default
    return d if d and d > 0 else default

def _safe_gain(entry, default=1.0):
    """Return an entry's loudness-normalization gain (a linear volume
    multiplier from the ffmpeg loudness probe). Missing/unprobed entries and
    corrupt <=0 values fall back to 1.0 -- 'play at normal volume' -- rather
    than silence or an unbounded boost."""
    g = entry.get("gain", default) if isinstance(entry, dict) else default
    return g if g and g > 0 else default

def _lookup_file_gain(ch_key, file_path, default=1.0):
    """Find the stored loudness-normalization gain for a specific file,
    searching every block/folder in that channel's "schedules" (this covers
    Morning/Evening/Night/Full Day/Marathon/Commercials AND "Music Tracks",
    since they all live under the same schedules dict) plus
    "holiday_schedules".

    Search order:
      1. Per-entry "gain" key in this channel's schedule dicts (standard
         entries AND paired short-episode entries).
      2. Global media_probe_cache (path-keyed, cross-channel) — this is the
         catch-all for files probed via a DIFFERENT channel, or for files
         whose _patch_probed_gain write-back hasn't fired yet on THIS
         channel's entry (e.g. very first play right after adding files).

    Returns `default` (1.0 = no change) if the file hasn't been probed yet
    or can't be found — always safe for a caller to use without a None-check.
    """
    if not file_path or db is None:
        return default
    norm_target = os.path.normpath(file_path)
    ch_info = db.channels_db.get(str(ch_key).zfill(2), {})
    pools = list(ch_info.get("schedules", {}).values()) + list(ch_info.get("holiday_schedules", {}).values())
    for pool in pools:
        if not isinstance(pool, list):
            continue
        for entry in pool:
            if not isinstance(entry, dict):
                continue
            # Standard entry
            if os.path.normpath(entry.get("path", entry.get("file", ""))) == norm_target:
                return _safe_gain(entry, default)
            # Paired short-episode entry: two real files in one schedule slot.
            # The slot uses pair_ep1_path / pair_ep2_path, not "path", so the
            # standard check above never matches. Each episode may carry its
            # own per-episode gain key written by _patch_probed_gain.
            if entry.get("is_pair"):
                _p1 = entry.get("pair_ep1_path", "")
                _p2 = entry.get("pair_ep2_path", "")
                if _p1 and os.path.normpath(_p1) == norm_target:
                    g = entry.get("pair_ep1_gain") or entry.get("gain")
                    return float(g) if g and float(g) > 0 else default
                if _p2 and os.path.normpath(_p2) == norm_target:
                    g = entry.get("pair_ep2_gain") or entry.get("gain")
                    return float(g) if g and float(g) > 0 else default
    # Fallback: the global cross-channel probe cache (see _get_cached_media_probe).
    # Covers files probed via a different channel, or files where the per-entry
    # write-back (_patch_probed_gain) hasn't fired yet on this channel's entry.
    # Without this, a file already known to the probe system plays at 1.0 gain
    # any time _patch_probed_gain happened to be racing or writing a different
    # channel's copy first.
    _cached_gain = _get_cached_media_probe(norm_target).get("gain")
    if _cached_gain and float(_cached_gain) > 0:
        return float(_cached_gain)
    return default

def _reset_marathon_anchor(ch_info):
    """Anchor episode 1 to the CURRENT half-hour block. Called when a channel is
    switched INTO marathon mode, so the marathon (re)starts from the top of the
    library at the :00/:30 mark of the moment it was enabled and then advances
    continuously from there (see the marathon branch in the scheduler)."""
    _n = datetime.datetime.now()
    _cs = _n.hour * 3600 + _n.minute * 60 + _n.second
    ch_info["marathon_anchor_epoch"] = _n.toordinal() * 86400 + (_cs // 1800) * 1800


def _get_or_set_block_anchor(ch_info, block_key, blk_clock_now):
    """LAZY PER-BLOCK-INSTANCE ANCHOR: the first time THIS EXACT block instance
    (e.g. "2026_206_Night" -- one specific day's Night window) is asked to
    produce live content, its anchor is floored to the nearest half-hour of
    THAT MOMENT (not the block's nominal 5am/1pm/9pm/midnight start) and
    persisted. Every later call for the same block_key reuses that exact same
    anchor, so the whole block chains forward from one fixed point using real
    show durations -- no re-snapping, ever, until the block_key itself changes
    (tomorrow's occurrence of this block gets its own fresh anchor the same
    way).

    Why floor to the moment first *needed*, not the nominal start hour: if you
    set up a channel's Night programming at 11:47 PM, there was never any real
    content decided for 9:00-11:30 -- flooring to 11:30 and starting there
    means "what's on and where" is always answerable from simple wall-clock
    math (elapsed = now - anchor) without inventing a fake history for a
    window nobody was ever going to see. The show-rotation position (show_pos)
    is untouched by this -- it isn't force-advanced to "cover" the missed
    stretch, so tomorrow's Night just continues from wherever it already was.
    Any resulting drift self-heals every day since each day's block gets its
    own independent anchor.

    blk_clock_now must be in the SAME continuous coordinate space the caller
    will later subtract this anchor from (e.g. plain seconds-since-midnight
    for Morning/Evening, or a continuous "seconds since 21:00 the day this
    Night block started" value for Night's midnight-crossing case) -- see the
    call sites in calculate_slotted_playback_state.
    """
    anchors = ch_info.setdefault("playback_log", {}).setdefault("block_anchors", {})
    anchor = anchors.get(block_key)
    if anchor is None:
        anchor = (int(blk_clock_now) // 1800) * 1800
        anchors[block_key] = anchor
        # Keep this from growing forever -- only recent block instances are
        # ever looked up again (block_key includes the day-of-year), so once
        # there are more than a handful just drop the oldest by key order.
        if len(anchors) > 12:
            for _old_key in sorted(anchors.keys())[:-12]:
                anchors.pop(_old_key, None)
        try:
            db.save_settings()
        except Exception as e:
            log.warning("_get_or_set_block_anchor: failed to persist anchor for %s: %s", block_key, e)
    return anchor


def _apply_scheduling_mode_change(ch_info, new_mode, target_ch_str):
    """Shared handler for all three scheduling-mode cycle controls (D/A/ENTER on the
    sub-menu).

    RULE FOR BLOCK-TO-BLOCK SWITCHES (random_slots/one_slot/two_slots, neither side
    marathon): lock whatever is airing RIGHT NOW in place and reschedule only the
    slots AFTER it. These transitions must NEVER restart the show currently on
    screen.
      - The show airing now is snapped to its live VLC position + real file length
        and pinned as playback_anchor, so it plays out untouched for its true
        remaining runtime; only the FUTURE schedule adopts the new mode (via the
        debounced rotation-cache rebuild).

    MARATHON BOUNDARY (either direction) is a deliberate hard cut. Marathon is a
    separate content source (its own dedicated, continuously-ordered playlist) that
    you switch ONTO and OFF of:
      - Entering (block -> marathon): "selecting marathon" means "start the
        marathon", so we anchor marathon_anchor_epoch to now (episode 1 at the
        current half-hour) and clear any block anchor.
      - Leaving (marathon -> block): return to normal block programming at the
        correct wall-clock position, so we clear the anchor and let the block
        schedule compute what airs now.
      Either way we DROP the outgoing source's anchor and immediately re-tune the
      live channel via change_channel(force_reload=True), rather than waiting for
      the (sub-menu-suppressed, ~2.5s-poll) end-of-show watchdog. Pinning the
      outgoing show on the way OUT was the "stuck on marathon" bug: the marathon
      episode lingered as a plain "Video" anchor the scheduler kept returning for
      its full runtime, so it played under every mode and re-selecting marathon
      just restarted it.

    ROTATION-CACHE INVALIDATION (playback_log's sched_block_key/sched_cached_tracks,
    read by both the scheduler and the TV guide for "what's scheduled the rest of
    this block") is debounced for block-to-block switches only. Someone cycling
    through the modes to see the options would otherwise force a full future-
    schedule rebuild on every keypress. Instead this stamps a "probably landed
    here" timestamp; the actual invalidation (rebuild of FUTURE slots only) fires
    once ~5s pass with no further mode change (see the debounce check at the top of
    calculate_slotted_playback_state). Marathon computes its timeline live from
    marathon_anchor_epoch and ignores this cache entirely."""
    old_mode = ch_info.get("scheduling_mode", "random_slots")

    # Capture whatever is airing RIGHT NOW (under the OLD mode) BEFORE the mode
    # is reassigned, so a plain block-to-block switch can pin it and reschedule
    # only the slots AFTER it. This must run first: once scheduling_mode flips,
    # the rotation engine would resolve a different lineup.
    #
    # We only care when this exact channel is the one live on screen. Prefer the
    # existing anchor (it already identifies the current show); otherwise ask the
    # scheduler what's on right now (the rotation cache still holds the OLD
    # lineup until the debounce commits, so this stays accurate).
    _live_now = None
    _is_live_ch = (str(app_state.get("current_channel", "")).zfill(2) == target_ch_str
                   and app_state.get("is_playing_video", False))
    if _is_live_ch:
        _existing_anc = ch_info.get("playback_anchor", {})
        if _existing_anc and _existing_anc.get("file"):
            _live_now = dict(_existing_anc)
        else:
            try:
                _ls = calculate_slotted_playback_state(target_ch_str)
                if _ls.get("mode") in ("Video", "Commercial") and _ls.get("file"):
                    _live_now = {
                        "file":        _ls["file"],
                        "seek_offset": _ls.get("seek", 0),
                        "duration":    _ls.get("duration", 1800),
                        "mode":        _ls.get("mode", "Video"),
                        "block":       _ls.get("block", "Morning"),
                    }
            except Exception:
                _live_now = None

    # Snap the captured show to the TRUE live VLC position and stamp it with the
    # current wall-clock start, so the pinned show keeps computing the correct
    # mid-episode position from here on.
    #
    # CRITICAL: keep "duration" as the SCHEDULED slot duration — do NOT override
    # it with the real VLC file length. The TV guide builds its timeline from the
    # same scheduled durations (media.build_block_timeline, unprobed files
    # normalized to 1800s), so if the anchor used the real file length instead, a
    # file longer than its scheduled slot would keep playing after the guide had
    # already advanced to the next show — which is exactly the "guide says X but
    # Y is playing" desync. Matching the scheduled duration keeps playback and the
    # guide in lockstep: the pinned show ends when its guide slot ends.
    if _live_now and _live_now.get("file"):
        _now_l = datetime.datetime.now()
        _live_now["wall_start"] = _now_l.hour * 3600 + _now_l.minute * 60 + _now_l.second
        try:
            if vlc_engine is not None and getattr(vlc_engine, "player", None) is not None:
                _pos_ms = vlc_engine.player.get_time()
                if _pos_ms is not None and _pos_ms >= 0:
                    _live_now["seek_offset"] = int(_pos_ms // 1000)
        except Exception as e:
            log.warning("_apply_scheduling_mode_change: failed to read current VLC position for seek_offset: %s", e)

    ch_info["scheduling_mode"] = new_mode
    crossing_marathon_boundary = (old_mode == "marathon") != (new_mode == "marathon")
    entering_marathon = (old_mode != "marathon") and (new_mode == "marathon")

    if old_mode != new_mode:
        if crossing_marathon_boundary:
            # Marathon is a separate content source you switch ONTO and OFF of, so
            # crossing the boundary in EITHER direction is a clean hard cut — NOT a
            # "lock the current show" transition:
            #   * Entering: start the marathon fresh at episode 1 (anchor the epoch
            #     to the current half-hour).
            #   * Leaving:  return to normal block programming at the correct
            #     wall-clock position.
            # In both cases DROP the block/anchor for the outgoing source. Pinning
            # the outgoing show on the way OUT was the bug behind "stuck on
            # marathon": it left the marathon episode as a plain "Video" anchor
            # that the scheduler's anchor short-circuit kept returning for the
            # episode's full ~45-min runtime, so the marathon kept playing on
            # screen under every mode and re-selecting marathon just restarted it.
            if entering_marathon:
                _reset_marathon_anchor(ch_info)
            ch_info["playback_anchor"] = {}
            # No debounced future-slot rebuild here: entering marathon ignores the
            # block cache entirely, and leaving it re-tunes straight to the live
            # block schedule below. Cancel any dangling block debounce so it can't
            # fire later and reshuffle behind us.
            ch_info["_sched_debounce_pending_since"] = None
        else:
            # Block-to-block: lock whatever is airing right now so the reschedule
            # can only touch UPCOMING slots — the current show is never restarted.
            # _live_now was snapped above to the LIVE VLC position and REAL file
            # length, so it holds the current show for its true remaining runtime
            # while the future schedule adopts the new mode via the debounced
            # rotation-cache rebuild.
            if _live_now and _live_now.get("file"):
                ch_info["playback_anchor"] = dict(_live_now)
            ch_info["_sched_debounce_pending_since"] = pygame.time.get_ticks()

    if crossing_marathon_boundary and str(app_state.get("current_channel", "")).zfill(2) == target_ch_str:
        # Force an immediate real-time re-tune (BOTH directions) so playback
        # reflects the new source now, instead of waiting for the sub-menu-
        # suppressed end-of-show watchdog. Bypass change_channel's same-channel
        # early-return by clearing is_playing_video first; force_reload guarantees
        # VLC reloads even if the new pick resolves to the same path.
        app_state["is_playing_video"] = False
        try:
            change_channel(target_ch_str, is_surfing=False, force_reload=True)
            if entering_marathon:
                print(f"[MARATHON] Started marathon at episode 1 on ch{target_ch_str}.")
            else:
                print(f"[MARATHON] Left marathon on ch{target_ch_str}; resumed {new_mode} schedule.")
        except Exception as e:
            print(f"[MARATHON] Boundary re-tune failed: {e}")
            import traceback
            traceback.print_exc()

    if crossing_marathon_boundary:
        # Row layout differs between marathon (no Episode Order row) and the
        # block modes — reposition the cursor in EITHER direction so it can't
        # end up pointing at a control that no longer means what it used to.
        # Purely a UI-layout fixup; playback is untouched.
        if str(app_state.get("selected_channel_row", "")).zfill(2) == target_ch_str \
                and app_state.get("menu_layer") == "CHANNEL_SUB_MENU":
            app_state["sub_menu_row_index"] = 1
            app_state["sub_menu_col_index"] = 0

    db.save_settings()

def _snapshot_track_durations(video_tracks):
    """Freeze each track's duration (path -> seconds) at the exact moment a
    block's rotation is committed to playback_log["sched_cached_tracks"].

    ROOT CAUSE this exists to fix: raw_tracks/video_tracks are built by
    reading each schedule entry's "duration" field LIVE out of
    db.channels_db on every single call to calculate_slotted_playback_state
    (and the TV guide's equivalent lookup in ui.py) -- including entries
    that are still airing right now. The background duration-probe workers
    patch that same "duration" field in place, at any time, including while
    the file is mid-airing. For a show split around a mid-episode commercial
    break (commercial_placement == "interrupt_half_hour"), the split point
    is computed as duration // 2 -- so if the probe corrects a file's
    duration between the first half airing and the second half resuming
    after the break, the SECOND half's seek_offset is recalculated from a
    different total than the FIRST half used, and playback resumes at the
    wrong point -- observed as the episode suddenly "backing up" several
    minutes right as it resumes from a break. (Same instability would also
    let ANY in-flight timeline recompute a different start_time/duration
    for a show already on air, any time a probe lands mid-airing.)

    Once a rotation is committed to sched_cached_tracks (i.e. this exact
    airing has begun), every subsequent read of it -- both live playback and
    the TV guide -- should replay the SAME durations that were in effect the
    moment it was committed, not whatever the probe has since corrected them
    to. A probe's correction still takes effect normally the next time this
    show's slot gets freshly rebuilt (new block, new cycle, etc.) -- it's
    only retroactive application mid-airing that this guards against.
    """
    out = {}

    def _best_dur(path, fallback):
        # Prefer a REAL probed length from the global cross-channel probe cache
        # over the entry's stored value, which may still be the placeholder
        # estimate (the 1800s-ish default). Freezing the real length at the
        # moment of commit means the guide/layout start out correct for this
        # airing instead of drifting onto the wrong episode/movie until a later
        # probe pass happens to land. If nothing real is cached yet, we keep the
        # entry's own value exactly as before (no behavior change).
        try:
            _c = _get_cached_media_probe(os.path.normpath(str(path))).get("duration")
            if _c and _c > 0:
                return int(_c)
        except Exception:
            pass
        return fallback

    for t in video_tracks:
        if t.get("is_pair"):
            p1, p2 = t.get("pair_ep1_path", t.get("path")), t.get("pair_ep2_path", "")
            if p1:
                out[p1] = _best_dur(p1, t.get("pair_ep1_duration", 0))
            if p2:
                out[p2] = _best_dur(p2, t.get("pair_ep2_duration", 0))
        else:
            p = t.get("path", "")
            if p:
                out[p] = _best_dur(p, t.get("duration", 0))
    return out


def _apply_cached_durations(raw_ordered, cached_durations):
    """Return raw_ordered with each entry's "duration" overridden from
    cached_durations where a frozen value exists, without mutating the
    original dicts (those are shared with the live raw_tracks list).
    See _snapshot_track_durations for why this override is needed."""
    if not cached_durations:
        return raw_ordered
    out = []
    for t in raw_ordered:
        cached_dur = cached_durations.get(t.get("path", ""))
        if cached_dur and cached_dur > 0:
            t2 = dict(t)
            t2["duration"] = cached_dur
            out.append(t2)
        else:
            out.append(t)
    return out


def calculate_slotted_playback_state(channel_id):
      """
      Precision 24-Hour Continuous Timeline Calculator Node.
      Phase 3: Holiday overrides, scheduling modes (random_slots/one_slot/two_slots/marathon),
      and commercial insertion after show segments.
      """
      target_clean_key = str(channel_id).zfill(2)
      ch_info = db.channels_db.get(target_clean_key, {}) if db is not None else {}

      if not ch_info or not ch_info.get("active", True):
          return {"mode": "Static", "file": "", "seek": 0}

      # NOTE: the block-to-block sched-change debounce used to commit here by
      # wholesale-clearing sched_block_key/sched_cached_tracks, which forced a
      # full rebuild of the ENTIRE block (including whatever had already aired
      # or was currently airing) the next time it was read — see the
      # "SCHED-CHANGE DEBOUNCE COMMIT" block further down (inside the
      # non-marathon block-cache section) for the future-slots-only version
      # that replaced it.

      now = datetime.datetime.now()
      today = now.date()
      current_seconds = now.hour * 3600 + now.minute * 60 + now.second

      # --- 0. PLAYBACK ANCHOR ---
      # When the user just switched to this channel we honour the exact file/seek
      # position that was live at switch time.  This prevents the timeline from
      # re-shuffling under a running show when new media is added to the library.
      # When a block-to-block scheduling-mode change is waiting to commit its
      # future-slots reschedule, we must let that commit run even though the
      # anchor below would normally short-circuit this whole function. Returning
      # the anchor immediately would keep the reschedule pending for as long as
      # the current show holds the anchor alive, so the NEXT upcoming show would
      # never pick up the new mode until the current one ended. In that case we
      # DEFER the anchor return: stash it here, fall through far enough for the
      # reschedule to commit (it only rebuilds slots AFTER the current show and
      # never touches this anchor), then return the stashed anchor unchanged.
      _deferred_anchor_state = None
      anchor = ch_info.get("playback_anchor", {})
      # ROOT-CAUSE FIX (episodes restarting / repeating on channel change and
      # on returning to a channel):
      # The playback_anchor exists ONLY to keep the file VLC currently has
      # loaded playing smoothly against duration-estimate drift. It must NEVER
      # be used to decide "what is on air" for a channel we are tuning into or
      # returning to -- that is the job of the deterministic wall-clock timeline
      # further down (sections 3/4/4a). That pure path is the exact same one the
      # never-actively-switched default channel uses, and the default channel is
      # the one that always stayed correct -- proof the pure path is right and
      # the anchor overlay is what corrupts the channels you switch between.
      #
      # By the time change_channel() calls this on tune-in, current_channel is
      # already the new channel and VLC has just been stopped, so a leftover
      # anchor from the last time we were on this channel (built from an
      # ESTIMATED duration) would otherwise short-circuit the real schedule and
      # replay/restart the episode. So: only honour a VIDEO anchor when THIS
      # channel is truly the live channel AND VLC actually has the anchored file
      # loaded and playing right now. Otherwise ignore it and fall through to
      # the wall-clock calculation, which resumes at exactly the scheduled
      # position (i.e. "N seconds further" if you were away N seconds).
      # Visualizer/music anchors are left on their original behaviour -- they
      # ride pygame.mixer, not vlc_engine, and are handled/pinned differently.
      _anchor_is_live_video = False
      if anchor and anchor.get("file") and anchor.get("mode") != "Visualizer":
          try:
              if (str(app_state.get("current_channel", "")).zfill(2) == target_clean_key
                      and app_state.get("is_playing_video", False)
                      and vlc is not None
                      and vlc_engine is not None
                      and getattr(vlc_engine, "player", None) is not None):
                  _astate = vlc_engine.player.get_state()
                  if _astate not in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                      _amedia = vlc_engine.player.get_media()
                      if _amedia:
                          from urllib.parse import unquote as _unquote_a
                          _aloaded = os.path.normpath(_unquote_a(_amedia.get_mrl().replace("file:///", ""))).lower()
                          _afile   = os.path.normpath(str(anchor.get("file", ""))).lower()
                          _anchor_is_live_video = (_aloaded == _afile)
          except Exception as e:
              log.warning("calculate_slotted_playback_state: anchor live-match check failed: %s", e)
      _anchor_trusted = bool(
          anchor and anchor.get("file") and os.path.exists(str(anchor.get("file", "")))
          and (anchor.get("mode") == "Visualizer" or _anchor_is_live_video)
      )
      if _anchor_trusted:
          wall_start = anchor.get("wall_start", 0)
          seek_off   = anchor.get("seek_offset", 0)
          duration   = anchor.get("duration", 1800)
          elapsed    = current_seconds - wall_start
          if elapsed < 0:
              elapsed += 86400          # midnight rollover
          # BUG FIX (split-show second-half anchor expires immediately):
          # The old formula was `duration - seek_off - elapsed`, which treated
          # seek_off as "seconds already consumed in this slot". That is correct
          # when seek_off is the file position at a mid-slot channel-join (e.g.
          # viewer tunes in 22 min into a 30-min slot: seek_off=1340, so
          # remaining = 1800 - 1340 - elapsed = 460 - elapsed).
          #
          # But for mid-stream resyncs the anchor is stamped at a slot
          # BOUNDARY — the scheduler is handing off to a new timeline entry
          # (e.g. the second half of a split show: seek_off=mid_point=900,
          # duration=remaining_half=900). The old formula gave
          # 900 - 900 - elapsed = -elapsed immediately, so the anchor expired
          # at frame 0 and every subsequent frame fell through to the
          # "is VLC still playing?" fallback. During the brief VLC state
          # transition of the resync itself that fallback returned False, so
          # the scheduler re-served the slot at seek=0 (start of file) instead
          # of seek=mid_point — replaying the first half of the episode.
          #
          # Fix: anchors now carry `slot_start_seek` (the file position at the
          # START of this timeline slot — 0 for whole shows, mid_point for
          # second halves, etc.). Only the intra-slot elapsed time matters:
          #   remaining = duration - (seek_off - slot_start_seek) - elapsed
          # At a slot boundary seek_off == slot_start_seek, so the offset
          # cancels and remaining = duration - elapsed. For a mid-slot join
          # seek_off > slot_start_seek and we get the same result as before.
          # Old anchors without `slot_start_seek` default to 0, preserving the
          # pre-fix behaviour for first segments (slot_start_seek == 0).
          _slot_start = anchor.get("slot_start_seek", 0)
          remaining = duration - (seek_off - _slot_start) - elapsed
          if remaining > 0:
              _anchor_state = {
                  "mode":             anchor.get("mode", "Video"),
                  "file":             anchor["file"],
                  "seek":             seek_off + elapsed,
                  "duration":         duration,
                  "block":            anchor.get("block", "Morning"),
                  # Always echo slot_start_seek back so downstream anchor
                  # re-writes (change_channel, mid-stream resync) receive the
                  # correct value via current_playback_state.get("seek_slot_offset").
                  "seek_slot_offset": anchor.get("slot_start_seek", 0),
              }
              _pending = ch_info.get("_sched_debounce_pending_since")
              if (_pending is not None
                      and not ch_info.get("is_visualizer", False)
                      and ch_info.get("scheduling_mode", "random_slots") != "marathon"
                      and pygame.time.get_ticks() - _pending >= 5000):
                  _deferred_anchor_state = _anchor_state
              else:
                  return _anchor_state
          else:
              # BUG FIX (episode restarts instead of advancing): the anchor's
              # ESTIMATED duration has elapsed on the wall clock, but that estimate
              # can undershoot the real file length by a little -- VLC's own
              # "Ended" event is the only thing that really knows when a file is
              # truly done. This function runs every single frame, BEFORE the
              # end-of-show watchdog in the main loop even gets a chance to look
              # at anything. If we used to fall straight through to a fresh
              # schedule calculation here while the anchored file is still
              # verifiably playing for real, the MID-STREAM SCHEDULE CHANGE
              # DETECTION block (main loop) would notice the scheduler's new
              # opinion of "what should be on" no longer matches what VLC
              # actually has loaded, and force a reseek/reload mid-viewing --
              # which is exactly what looked like "the episode restarted from
              # the beginning" even though the real file hadn't finished yet.
              #
              # So: keep honouring the anchor past its estimated duration as
              # long as VLC confirms this exact file, on this exact live
              # channel, hasn't actually ended. Real expiration is left
              # entirely to the end-of-show watchdog, which checks VLC's own
              # Ended/Stopped/Error state (not wall-clock guesswork) and is
              # also the thing that clears playback_anchor once it has used it
              # to compute a forward "skip past this slot" position. We
              # deliberately no longer clear the anchor here: doing so used to
              # race ahead of that watchdog and wipe the very wall_start/
              # duration/block fields it needs, silently defeating the skip
              # logic any time the real file ran a little longer than its
              # stored estimate (i.e. almost always, since durations are
              # probed/approximate).
              _is_live_ch = (str(app_state.get("current_channel", "")).zfill(2) == target_clean_key
                             and app_state.get("is_playing_video", False))
              _still_playing_live = False
              if _is_live_ch and vlc_engine is not None and getattr(vlc_engine, "player", None) is not None:
                  try:
                      _vstate = vlc_engine.player.get_state()
                      if vlc is not None and _vstate not in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                          _media = vlc_engine.player.get_media()
                          if _media:
                              from urllib.parse import unquote as _unquote
                              _loaded_p = os.path.normpath(_unquote(_media.get_mrl().replace("file:///", ""))).lower()
                              _anchor_p = os.path.normpath(str(anchor["file"])).lower()
                              if _loaded_p == _anchor_p:
                                  _still_playing_live = True
                  except Exception as e:
                      log.warning("calculate_slotted_playback_state: live-playback verification failed: %s", e)
              if _still_playing_live:
                  return {
                      "mode":             anchor.get("mode", "Video"),
                      "file":             anchor["file"],
                      "seek":             seek_off + elapsed,
                      "duration":         duration,
                      "block":            anchor.get("block", "Morning"),
                      # Carry slot_start_seek forward so any anchor re-write that
                      # uses this state (mid-stream resync, change_channel) gets the
                      # correct slot_start_seek instead of defaulting to 0.  Without
                      # this, a split-show second-half anchor loses slot_start_seek
                      # after the first nominal expiry, making the remaining formula
                      # compute a large negative value, expiring immediately, and
                      # triggering a spurious mid-stream resync → "replay last Ns" bug.
                      "seek_slot_offset": anchor.get("slot_start_seek", 0),
                  }
              # Not verifiably still playing (different/no live channel, VLC
              # itself already confirms Ended/Stopped/Error, or a different
              # file is loaded than what the anchor names) -- safe to let the
              # scheduler compute fresh below. The anchor dict is left as-is;
              # the watchdog reads it moments later in this same frame and
              # clears it once it's done with it.

      # --- 1. MUSIC VISUALIZER TIMELINE LOGIC POOL ---
      if ch_info.get("is_visualizer", False):
          schedule_stack = ch_info.get("schedules", {}).get("Music Tracks", [])
          if not schedule_stack:
              return {"mode": "Visualizer_Empty", "file": "", "seek": 0}

          track_list = []
          for asset in schedule_stack:
              path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
              if path and _cached_path_exists(path):
                  duration = _safe_dur(asset, default=210)
                  track_list.append({"path": path, "duration": duration})

          if not track_list:
              return {"mode": "Visualizer_Empty", "file": "", "seek": 0}

          # FIXED: Replaced unsafe string hash with static integer offset for TV Guide consistency
          day_seed = now.year * 1000 + now.timetuple().tm_yday + int(target_clean_key) + 400
          rng = random.Random(day_seed)
          rng.shuffle(track_list)

          total_playlist_seconds = sum(t["duration"] for t in track_list)
          if total_playlist_seconds <= 0:
              return {"mode": "Visualizer_Empty", "file": "", "seek": 0}

          position_in_playlist = current_seconds % total_playlist_seconds

          # --- EARLY/LATE-END SKIP OVERRIDE (visualizer) ---
          # Same idea as the video watchdog's correct-then-recompute approach:
          # the live-tick watchdog (the "Visualizer" render branch) is the
          # actual source of truth for when a song has really finished, via
          # pygame.mixer.music.get_busy() -- not this wall-clock modulo math,
          # which is only ever as accurate as each track's (possibly still-
          # unprobed/placeholder) stored duration.
          # When the watchdog sees a track truly end, it records where the
          # NEXT track actually starts (in this same shuffled-playlist position
          # space) here, so this call lands on that track instead of either
          # replaying the one that just ended (assumed duration was too long)
          # or skipping past several tracks it never really played (assumed
          # duration was too short).
          if 'app_state' in globals():
              _vsk_ch  = app_state.get("visualizer_skip_ch", "")
              _vsk_pos = app_state.get("visualizer_skip_pos", None)
              if _vsk_ch == target_clean_key and _vsk_pos is not None:
                  position_in_playlist = _vsk_pos % total_playlist_seconds
                  app_state["visualizer_skip_ch"]  = ""
                  app_state["visualizer_skip_pos"] = None

          target_track = None
          elapsed = 0
          for track in track_list:
              if elapsed + track["duration"] > position_in_playlist:
                  seek_position = position_in_playlist - elapsed
                  target_track = {
                      "mode": "Visualizer",
                      "file": track["path"],
                      "seek": seek_position,
                      "duration": track["duration"]
                  }
                  break
              elapsed += track["duration"]

          if target_track is None:
              target_track = {"mode": "Visualizer", "file": track_list[0]["path"], "seek": 0, "duration": track_list[0]["duration"]}

          return target_track

      # --- 1b. HOLIDAY DATE OVERRIDE CHECK ---
      def _get_holiday_key(d):
          """Return holiday key if today falls in a holiday window, else None."""
          m, day = d.month, d.day
          if m == 10 and 25 <= day <= 31:
              return "halloween"
          if m == 12 and 20 <= day <= 26:
              return "christmas"
          if m == 2 and day == 14:
              return "valentine"
          if (m == 12 and day == 31) or (m == 1 and day == 1):
              return "new_year"
          if m == 11:
              thursdays = [
                  datetime.date(d.year, 11, x)
                  for x in range(1, 31)
                  if datetime.date(d.year, 11, x).weekday() == 3
              ]
              if len(thursdays) >= 4 and d == thursdays[3]:
                  return "thanksgiving"
          return None

  # ==============================================================================
# PART 4b OF 28: DYNAMIC STATION SCHEDULING MODES
# ==============================================================================

      holiday_key = _get_holiday_key(today)
      holiday_schedules = ch_info.get("holiday_schedules", {})

      if (_deferred_anchor_state is None
              and holiday_key and holiday_key in holiday_schedules and holiday_schedules[holiday_key]):
          h_entries = holiday_schedules[holiday_key]
          h_tracks = []
          _media_probe_cache_h = (db.config.get("media_probe_cache", {}) if db is not None else {})
          for asset in h_entries:
              path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
              if path and os.path.exists(path):
                  # SINGLE SOURCE OF TRUTH (media.resolve_track_duration) — a
                  # movie dropped into a holiday lineup deserves the same
                  # real-duration lookup as any other slot; see the main
                  # scheduler's raw_tracks builder for the full rationale.
                  stored = asset.get("duration", 0) if isinstance(asset, dict) else 0
                  duration = resolve_track_duration(_media_probe_cache_h, path, stored)
                  h_tracks.append({"path": path, "duration": duration})
          if h_tracks:
              total_h = sum(t["duration"] for t in h_tracks)
              if total_h > 0:
                  pos = current_seconds % total_h
                  elapsed = 0
                  for t in h_tracks:
                      if elapsed + t["duration"] > pos:
                          return {"mode": "Video", "file": t["path"], "seek": pos - elapsed,
                                  "duration": t["duration"], "block": "Holiday"}
                      elapsed += t["duration"]
                  return {"mode": "Video", "file": h_tracks[0]["path"], "seek": 0,
                          "duration": h_tracks[0]["duration"], "block": "Holiday"}

      # --- 2. TIME-BLOCK BASED VIDEO SCHEDULING ---
      # MARATHON MODE: instead of the 3 time-of-day blocks, marathon plays one
      # dedicated folder as a single continuous stream that NEVER resets at a
      # block boundary. The position is driven by a continuous epoch clock
      # (whole days * 86400 + seconds-into-today) so when the playlist is shorter
      # than a day it loops, and when it's longer it simply carries on into the
      # next day — always landing on "the next episode in line", in season/
      # episode order, until the library is exhausted and then restarts at ep 1.
      _marathon_mode = (ch_info.get("scheduling_mode", "random_slots") == "marathon")
      # FULL-DAY BLOCK LENGTH: like Marathon, this collapses the 3 time-of-day
      # blocks into one 24-hour pool ("Full Day"), but — unlike Marathon — it
      # keeps using this channel's own scheduling_mode (random_slots/one_slot/
      # two_slots) to pick content, instead of Marathon's fixed full-catalog
      # playlist. Meaningless (and hidden in the UI) when scheduling_mode is
      # already "marathon", since that's inherently 24-hour/continuous already.
      _full_day_mode = (not _marathon_mode) and (ch_info.get("block_mode", "full_day") == "full_day")
      if _marathon_mode:
          current_block = "Marathon"
          block_start_seconds = 0
          block_duration_seconds = 86400
          # Episode 1 begins at the half-hour mark of the moment marathon was
          # enabled (the "anchor"), NOT at an arbitrary date-derived offset.
          # elapsed = now - anchor; the playlist modulo (applied later) turns this
          # into "how far into the marathon we are", so it always starts at ep 1
          # and keeps advancing continuously across day resets.
          epoch_now = now.toordinal() * 86400 + current_seconds
          anchor = ch_info.get("marathon_anchor_epoch")
          if not anchor:
              # Fallback for profiles that were on marathon before anchoring
              # existed: anchor to the current half-hour block and persist it.
              anchor = now.toordinal() * 86400 + (current_seconds // 1800) * 1800
              ch_info["marathon_anchor_epoch"] = anchor
              try: db.save_settings()
              except Exception as e: log.warning("calculate_slotted_playback_state: failed to persist marathon_anchor_epoch: %s", e)
          seconds_into_block = epoch_now - anchor
      elif _full_day_mode:
          # Unlike Marathon, Full Day still resets/reshuffles once per calendar
          # day (same as any other block) — it's simply ONE block that spans
          # the whole day instead of three 8-hour ones. block_key below
          # (built from current_block) naturally becomes a new key at midnight,
          # which is what drives the daily reshuffle for random_slots/etc.
          #
          # ANCHOR (not midnight): position is measured from the moment THIS
          # day's Full Day block first went live, floored to the half-hour —
          # not from literal midnight. Without this, adding/enabling a Full
          # Day channel at, say, 3:12 PM would compute seconds_into_block as
          # if 14+ hours of programming had already silently aired since
          # midnight, and immediately jump into whatever the cumulative
          # real-duration timeline says is airing 14 hours in — instead of
          # actually starting programming from right now.
          current_block = "Full Day"
          _this_block_key = f"{now.year}_{now.timetuple().tm_yday}_Full Day"
          block_start_seconds = _get_or_set_block_anchor(ch_info, _this_block_key, current_seconds)
          seconds_into_block = current_seconds - block_start_seconds
          if seconds_into_block < 0:
              seconds_into_block += 86400
          block_duration_seconds = 86400
      else:
          current_hour = now.hour
          if 5 <= current_hour < 13:
              current_block = "Morning"
              block_start_hour = 5
              block_end_hour = 13
          elif 13 <= current_hour < 21:
              current_block = "Evening"
              block_start_hour = 13
              block_end_hour = 21
          else:
              current_block = "Night"
              block_start_hour = 21
              block_end_hour = 5

      if _marathon_mode:
          # Marathon already set its continuous epoch position above; skip the
          # per-block time math entirely.
          pass
      elif _full_day_mode:
          # Full Day already set its position (whole-day block) above; skip
          # the per-block time math entirely, same as Marathon.
          pass
      elif current_block == "Night" and current_hour < 5:
          # Night block spans 21:00 -> 05:00, crossing midnight. Its key AND
          # anchor must both use YESTERDAY's date so 11:47 PM and 2:00 AM
          # resolve to the exact same block instance -- the rotation cache
          # below (_is_new_block) uses this same _this_block_key, so without
          # this the lineup would also silently reshuffle mid-night at the
          # calendar rollover, on top of getting a fresh (wrong) anchor.
          _yesterday = now - datetime.timedelta(days=1)
          _this_block_key = f"{_yesterday.year}_{_yesterday.timetuple().tm_yday}_Night"
          _blk_clock = (24 - 21) * 3600 + current_seconds
          block_start_seconds = _get_or_set_block_anchor(ch_info, _this_block_key, _blk_clock)
          seconds_into_block = _blk_clock - block_start_seconds
          block_duration_seconds = 8 * 3600
      elif current_block == "Night":
          _this_block_key = f"{now.year}_{now.timetuple().tm_yday}_Night"
          _blk_clock = current_seconds
          block_start_seconds = _get_or_set_block_anchor(ch_info, _this_block_key, _blk_clock)
          seconds_into_block = _blk_clock - block_start_seconds
          block_duration_seconds = 8 * 3600
      else:
          _this_block_key = f"{now.year}_{now.timetuple().tm_yday}_{current_block}"
          _blk_clock = current_seconds
          block_start_seconds = _get_or_set_block_anchor(ch_info, _this_block_key, _blk_clock)
          seconds_into_block = _blk_clock - block_start_seconds
          block_duration_seconds = (block_end_hour - block_start_hour) * 3600

      # NOTE: full-catalog ordering (natural episode order within each show
      # folder, folders sorted/shuffled by scheduling mode) now lives in the
      # shared media.build_ordered_tracks() — see the Marathon branch and the
      # block-overflow lookahead below. This is the SAME function the TV guide
      # calls (get_marathon_content() in ui.py), so Marathon can never show a
      # different lineup than what's actually airing.

      # Marathon draws from its own dedicated folder; Full Day draws from its
      # own dedicated 24-hour folder; the time-blocks draw from their own
      # Morning/Evening/Night folders. current_block already resolves to the
      # right bucket name ("Marathon" / "Full Day" / Morning/Evening/Night)
      # from the branch above, so this lookup is generic.
      folder_items = ch_info.get("schedules", {}).get(current_block, [])

      raw_tracks = []
      _media_probe_cache = (db.config.get("media_probe_cache", {}) if db is not None else {})
      for asset in folder_items:
          path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
          if path and _cached_path_exists(path):
              # SINGLE SOURCE OF TRUTH (media.resolve_track_duration): always
              # prefer a real probed length from the global media_probe_cache
              # over the entry's stored value, regardless of what placeholder
              # the stored value happens to be (-1, 0, missing, or a stale
              # estimate). The old check here only looked the probe cache up
              # when the RAW stored duration was exactly -1, which silently
              # never fired for entries stored with 0/missing durations -- a
              # real 2-hour film could sit fully probed in media_probe_cache
              # forever while this call site kept using its 1800s placeholder,
              # falling below is_movie_track's 4800s threshold and getting
              # misclassified as a series episode (and, for a flat
              # all-movies-in-one-folder library, grouped into ONE giant
              # group_shows() pseudo-show with every other movie in that
              # folder -- see resolve_track_duration's docstring).
              stored = asset.get("duration", 0) if isinstance(asset, dict) else 0
              duration = resolve_track_duration(_media_probe_cache, path, stored)
              raw_tracks.append({"path": path, "duration": duration})

      # --- 2a. (removed) EARLY-END SKIP OVERRIDE ---
      # This used to consume a block-position "skip past the stale slot"
      # target left by the watchdog when a clip ended before its assumed
      # duration. That guesswork is gone: the watchdog now corrects the
      # just-ended file's REAL duration in place the instant it ends (see
      # the WATCHDOG "CORRECT-THEN-RECOMPUTE" block in the main loop) and
      # simply clears the anchor, so this function's own cumulative-duration
      # timeline walk already lands on the right next show on its very next
      # call -- no separate skip signal to relay here anymore.

      # --- 2b. BLOCK OVERFLOW: let the previous block's last show finish --------
      # If we are within the first 2 hours of a new block, check whether the
      # previous block's last scheduled episode would still be mid-play right now.
      # If yes, return that show so episodes never hard-cut at the 5 AM / 1 PM /
      # 9 PM block boundaries. Marathon has no block boundaries, so it is exempt.
      if _deferred_anchor_state is None and not _marathon_mode and not _full_day_mode and 0 < seconds_into_block < 7200:
          _prev_name   = {"Morning": "Night", "Evening": "Morning", "Night": "Evening"}[current_block]
          _prev_offset = {"Morning": 100, "Evening": 200, "Night": 300}.get(_prev_name, 0)
          _prev_seed   = now.year * 1000 + now.timetuple().tm_yday + int(target_clean_key) + _prev_offset
          _prev_sched  = ch_info.get("scheduling_mode", "random_slots")
          _prev_items  = ch_info.get("schedules", {}).get(_prev_name, [])
          _prev_ordered = build_ordered_tracks(_prev_items, _prev_sched, _prev_seed)
          if _prev_ordered:
              _prev_total = sum(t["duration"] for t in _prev_ordered)
              if _prev_total > 0:
                  _prev_block_secs = 28800          # all blocks are 8 hours
                  _p_pos = _prev_block_secs % _prev_total
                  _acc = 0
                  for _pshow in _prev_ordered:
                      if _acc + _pshow["duration"] > _p_pos:
                          _inner     = _p_pos - _acc              # how far into show at boundary
                          _remaining = _pshow["duration"] - _inner
                          if seconds_into_block < _remaining:
                              return {"mode": "Video",
                                      "file": _pshow["path"],
                                      "seek": _inner + seconds_into_block,
                                      "duration": _pshow["duration"],
                                      "block": current_block}
                          break
                      _acc += _pshow["duration"]

      if not raw_tracks:
          return {"mode": "Static", "file": "", "seek": 0}

      # --- 3. SCHEDULING MODE ENGINE ---
      scheduling_mode = ch_info.get("scheduling_mode", "random_slots")
      
      # FIXED: Replaced unsafe string hash mapping to stop system drift mismatches
      block_offset = {"Morning": 100, "Evening": 200, "Night": 300, "Full Day": 500}.get(current_block, 0)
      day_seed = now.year * 1000 + now.timetuple().tm_yday + int(target_clean_key) + block_offset

      playback_log = ch_info.setdefault("playback_log", {})
      _show_pos = playback_log.setdefault("show_pos", {})   # show_name -> next episode index, persists forever

      _pair_thresh_sec = max(0, ch_info.get("pair_threshold_minutes", 15)) * 60

      # --- COMMERCIAL CONFIG (moved up from section 4) ---
      # Needed earlier now: the future-slots-only debounce-commit logic below
      # (non-marathon block-cache section) has to re-run build_block_timeline
      # on the OLD cached lineup to find "what's already aired vs not", and
      # that requires the exact same commercial pool/placement the live
      # timeline build uses — otherwise the "already aired" boundary it finds
      # wouldn't match reality.
      commercials_enabled  = ch_info.get("commercials_enabled", False)
      commercial_placement = ch_info.get("commercial_placement", "interrupt_half_hour")
      commercial_items     = ch_info.get("schedules", {}).get("Commercials", [])

      # Minimum show length (seconds) required to interrupt a show mid-episode in
      # "interrupt" mode. Anything shorter only gets commercials at the END of the
      # show. When short-episode pairing is ON, short shows have already been
      # combined into longer blocks, so the floor is the pairing threshold; when
      # pairing is OFF (threshold 0) we fall back to a 15-minute floor so tiny
      # shows (e.g. under 15 min) aren't chopped up.
      _pair_thresh_min = ch_info.get("pair_threshold_minutes", 15)
      INTERRUPT_FLOOR_SECONDS = (_pair_thresh_min if _pair_thresh_min > 0 else 15) * 60

      COMMERCIAL_DEFAULT_DUR = 30   # sane fallback for an unprobed/failed commercial

      commercial_tracks = []
      if commercials_enabled and commercial_items:
          for asset in commercial_items:
              path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
              if path and _cached_path_exists(path):
                  duration = asset.get("duration", COMMERCIAL_DEFAULT_DUR) if isinstance(asset, dict) else COMMERCIAL_DEFAULT_DUR
                  # Newly-added commercials are stored as duration=-1 (unprobed
                  # sentinel) until a background probe fills them in; a failed probe
                  # can leave -1 permanently. A non-positive duration fed into the
                  # break packer sends `used` negative and spins the fill loop
                  # forever (the real reason commercials appeared "broken"). Clamp
                  # to a short default so every commercial always has real airtime.
                  if not duration or duration <= 0:
                      duration = COMMERCIAL_DEFAULT_DUR
                  commercial_tracks.append({"path": path, "duration": int(duration)})

      if _marathon_mode:
          # MARATHON: one fixed, fully-ordered playlist of the ENTIRE library,
          # ordered folder-by-folder then by natural episode order within each
          # (pilots/ep1/season1 first, then season2, ... across every show).
          # The continuous epoch position (seconds_into_block) is taken modulo
          # the playlist's total length below, so it plays "the next episode in
          # line" nonstop, carries into the next day, and only wraps back to
          # episode 1 once every file has aired. There is no per-day reshuffle
          # and no show_pos advancement — the order must stay identical every
          # call so the epoch clock maps to a stable position.
          _marathon_ordered = build_ordered_tracks(raw_tracks, "natural", 0)
          video_tracks = pair_short_episodes(_marathon_ordered, threshold_seconds=_pair_thresh_sec) if _marathon_ordered else []
          # Skip the block-cache machinery entirely for marathon.
          _is_new_block = False
      else:
        # A "block" = this Morning/Evening/Night window TODAY. Tomorrow's Morning
        # is a different block instance, even though it's the same time-of-day slot.
        # _this_block_key was already computed above (with the special-cased
        # yesterday's-date key for post-midnight Night) -- reusing it here
        # instead of recomputing keeps the rotation cache and the block anchor
        # from ever disagreeing about which day's Night this is.
        block_key = _this_block_key
        _is_new_block = playback_log.get("sched_block_key") != block_key

        # --- SCHED-CHANGE DEBOUNCE COMMIT (future-slots-only reschedule) ---
        # See _apply_scheduling_mode_change: a plain block-to-block mode switch
        # (random_slots/one_slot/two_slots) doesn't touch the cache right away —
        # it stamps a "probably landed here" tick and waits ~5s for another mode
        # change before committing, so rapidly cycling through the three options
        # doesn't force a rebuild on every keypress.
        #
        # What "committing" means is deliberately NOT "wipe the block and start
        # over" — that used to nuke sched_block_key/sched_cached_tracks wholesale,
        # which forced a full re-run of build_show_rotation for the ENTIRE block
        # (every slot, front to back) the next time anything read it. Two things
        # went wrong because of that: (1) any slot that had ALREADY aired earlier
        # in this exact block got a brand-new episode assigned retroactively
        # (silently skipping/repeating content), and (2) the slot that's
        # CURRENTLY airing got a freshly-repicked episode the instant the guide
        # (or, once playback_anchor naturally expires, live playback itself)
        # re-read this channel — which looked exactly like the current show
        # restarting, even though the anchor was technically still "protecting"
        # it in the meantime.
        #
        # Instead: reconstruct the OLD committed lineup, find exactly how far
        # into it "right now" falls, and only regenerate the portion AFTER
        # that point — under the NEW scheduling mode. Everything already aired,
        # plus whatever's airing this exact instant, is carried over byte-for-
        # byte into the new cache, so neither the guide nor playback ever see
        # it change out from under them.
        _pending_since = ch_info.get("_sched_debounce_pending_since")
        if _pending_since is not None and pygame.time.get_ticks() - _pending_since >= 5000:
            ch_info["_sched_debounce_pending_since"] = None
            if not _is_new_block:
                _old_cached = playback_log.get("sched_cached_tracks", [])
                _by_path_old = {t["path"]: t for t in raw_tracks}
                _old_raw_ordered = [_by_path_old[p] for p in _old_cached if p in _by_path_old]
                # Freeze durations to what was actually committed for this
                # airing -- see _snapshot_track_durations docstring. Without
                # this, a probe correction landing here would let the "kept"
                # (already-aired/currently-airing) prefix silently recompute
                # with different durations than what's actually on screen.
                _old_raw_ordered = _apply_cached_durations(
                    _old_raw_ordered, playback_log.get("sched_cached_durations", {}))
                _old_video_tracks = (pair_short_episodes(_old_raw_ordered, threshold_seconds=_pair_thresh_sec)
                                     if _old_raw_ordered else [])
                _keep_n = 0
                if _old_video_tracks:
                    _old_timeline, _old_total = build_block_timeline(
                        _old_video_tracks, commercial_tracks, commercial_placement, day_seed,
                        interrupt_floor_seconds=INTERRUPT_FLOOR_SECONDS
                    )
                    if _old_total > 0:
                        _old_pos = seconds_into_block % _old_total
                        # Walk the old timeline in the same order video_tracks was
                        # built in, advancing a video-track pointer every time a
                        # new (non-commercial) show/pair begins, until we find the
                        # chunk that covers "right now". Everything up to and
                        # including that entry's whole video_track is preserved;
                        # only strictly-later entries get regenerated.
                        _vt_idx = -1
                        _last_path = None
                        _found = False
                        for _item in _old_timeline:
                            if _item["is_commercial"]:
                                continue
                            if _item["path"] != _last_path:
                                _vt_idx += 1
                                _last_path = _item["path"]
                            if _item["start_time"] <= _old_pos < _item["start_time"] + _item["duration"]:
                                _keep_n = _vt_idx + 1
                                _found = True
                                break
                        if not _found:
                            # Position fell in a gap (e.g. trailing dead air) —
                            # safest fallback is to keep the whole old lineup as-is
                            # rather than guess; it'll naturally reschedule once
                            # the block rolls over or another change is confirmed.
                            _keep_n = len(_old_video_tracks)
                if 0 < _keep_n < len(_old_video_tracks):
                    # Preserve the already-aired/currently-airing prefix exactly;
                    # regenerate only the remainder under the NEW scheduling mode.
                    # NOTE: playback_log["show_pos"] already reflects every episode
                    # the OLD full-block build had assigned (including whatever
                    # future slots we're about to discard here), so a show whose
                    # only-just-discarded pick never actually aired simply picks up
                    # one step further along next time it comes up. That's a minor,
                    # harmless numbering skip (never a repeat, never a restart) —
                    # the acceptable trade-off for never disturbing what's already
                    # on air.
                    _kept_video_tracks = _old_video_tracks[:_keep_n]
                    _kept_paths = []
                    for _ct in _kept_video_tracks:
                        if _ct.get("is_pair"):
                            _kept_paths.append(_ct.get("pair_ep1_path", _ct["path"]))
                            _kept_paths.append(_ct.get("pair_ep2_path", ""))
                        else:
                            _kept_paths.append(_ct.get("path", ""))
                    _show_order_by_block = playback_log.setdefault("show_order", {})
                    _new_tail_tracks, _new_show_pos, _new_show_order = build_show_rotation(
                        raw_tracks, scheduling_mode, _show_pos, day_seed,
                        pair_threshold_seconds=_pair_thresh_sec,
                        episode_order_mode=ch_info.get("episode_order_mode", "sequential"),
                        fixed_show_order=_show_order_by_block.get(current_block),
                        block_duration_seconds=block_duration_seconds
                    )
                    playback_log["show_pos"] = _new_show_pos
                    _show_order_by_block[current_block] = _new_show_order
                    _tail_paths = []
                    for _ct in _new_tail_tracks:
                        if _ct.get("is_pair"):
                            _tail_paths.append(_ct.get("pair_ep1_path", _ct["path"]))
                            _tail_paths.append(_ct.get("pair_ep2_path", ""))
                        else:
                            _tail_paths.append(_ct.get("path", ""))
                    playback_log["sched_cached_tracks"] = [p for p in (_kept_paths + _tail_paths) if p]
                    # Kept prefix carries its ALREADY-frozen durations forward
                    # unchanged (it's already airing/aired); the fresh tail
                    # gets its current durations frozen for the first time,
                    # right now, at commit. See _snapshot_track_durations.
                    _old_durs = playback_log.get("sched_cached_durations", {})
                    playback_log["sched_cached_durations"] = {
                        **{p: _old_durs[p] for p in _kept_paths if p in _old_durs},
                        **_snapshot_track_durations(_new_tail_tracks),
                    }
                    # block_key is intentionally left as-is: this is still the same
                    # block, just re-flavored from here forward, not a new one.
                    playback_log["sched_block_key"] = block_key
                    if db is not None:
                        try: db.save_settings()
                        except Exception as e: log.warning("calculate_slotted_playback_state: failed to persist re-flavored sched_cached_tracks: %s", e)
                # _keep_n == 0 or _keep_n >= len(_old_video_tracks): nothing safe/
                # useful to reschedule yet — leave the cache untouched rather than
                # guess; falls through to the normal cache-read path below.

        # The future-slots reschedule (if any) has now committed to the cache.
        # If we deferred an in-flight anchor return to get here, hand back the
        # currently-airing show unchanged — it keeps playing to its real end
        # while every upcoming slot already reflects the new scheduling mode.
        if _deferred_anchor_state is not None:
            return _deferred_anchor_state

        video_tracks = []
        if not _is_new_block:
          # Re-tuning into the same block: replay exactly what was already
          # chosen for it — do NOT advance any show's position again.
          # The cache stores ALL constituent raw paths (pairs expanded to two
          # entries) so we can reconstruct the ordered list and re-apply pairing.
          _cached = playback_log.get("sched_cached_tracks", [])
          _by_path = {t["path"]: t for t in raw_tracks}
          _raw_ordered = [_by_path[p] for p in _cached if p in _by_path]
          # Freeze durations to what was committed for this already-in-
          # progress airing -- see _snapshot_track_durations docstring.
          _raw_ordered = _apply_cached_durations(
              _raw_ordered, playback_log.get("sched_cached_durations", {}))
          video_tracks = pair_short_episodes(_raw_ordered, threshold_seconds=_pair_thresh_sec) if _raw_ordered else []
          if not video_tracks:
              _is_new_block = True   # cache missing/stale (e.g. files changed) — rebuild below

        if _is_new_block:
          # SINGLE SOURCE OF TRUTH: this exact same function is also called by
          # the TV guide (ui.py) so the guide always matches what actually plays.
          # Day-to-day POSITION persistence (all three scheduling modes):
          # reuse whatever
          # show-slot order was frozen the first time this block was built,
          # instead of reshuffling every day. Content still advances day to
          # day (show_pos below); only WHERE each show sits in the block
          # holds still.
          _show_order_by_block = playback_log.setdefault("show_order", {})
          video_tracks, _new_show_pos, _new_show_order = build_show_rotation(
              raw_tracks, scheduling_mode, _show_pos, day_seed,
              pair_threshold_seconds=_pair_thresh_sec,
              episode_order_mode=ch_info.get("episode_order_mode", "sequential"),
              fixed_show_order=_show_order_by_block.get(current_block),
              block_duration_seconds=block_duration_seconds
          )
          playback_log["show_pos"] = _new_show_pos
          _show_order_by_block[current_block] = _new_show_order
          playback_log["sched_block_key"] = block_key
          # Expand pairs back to individual paths so the cache survives restarts.
          _cache_paths = []
          for _ct in video_tracks:
              if _ct.get("is_pair"):
                  _cache_paths.append(_ct.get("pair_ep1_path", _ct["path"]))
                  _cache_paths.append(_ct.get("pair_ep2_path", ""))
              else:
                  _cache_paths.append(_ct.get("path", ""))
          playback_log["sched_cached_tracks"] = [p for p in _cache_paths if p]
          # Freeze this cycle's durations at the moment it's committed --
          # see _snapshot_track_durations docstring.
          playback_log["sched_cached_durations"] = _snapshot_track_durations(video_tracks)
          # Reset per-cycle counters so a fresh block always starts at cycle 0.
          playback_log["sched_cycle_boundary"] = 0
          playback_log["sched_cycle_total"]    = 0
          try: db.save_settings()
          except Exception as e: log.warning("calculate_slotted_playback_state: failed to persist sched_cached_tracks: %s", e)

      # --- 4. TIMELINE WITH OPTIONAL COMMERCIAL INSERTION ---
      # commercials_enabled/commercial_placement/commercial_items/
      # INTERRUPT_FLOOR_SECONDS/commercial_tracks are now computed earlier
      # (see "COMMERCIAL CONFIG" above _pair_thresh_sec) so the debounce-commit
      # future-slots-only reschedule can reuse them too.

      # SINGLE SOURCE OF TRUTH: exact show/commercial start-stop timing is built
      # by media.build_block_timeline() — the TV guide (ui.py) calls this exact
      # same function so a show's displayed start/stop time in the guide can
      # never diverge from when it actually starts/stops on air. See Task 6 /
      # build_block_timeline's docstring for why this replaced an inline,
      # guide-side approximation.
      timeline, total_block_content = build_block_timeline(
          video_tracks, commercial_tracks, commercial_placement, day_seed,
          interrupt_floor_seconds=INTERRUPT_FLOOR_SECONDS
      )

      if not timeline or total_block_content <= 0:
          return {"mode": "Static", "file": "", "seek": 0}

      # --- 4a. WITHIN-BLOCK CYCLE ADVANCEMENT ---
      # When the block's playlist is shorter than the block window (e.g. one_slot
      # with 3 shows × 30 min = 90 min inside an 8-hour block) the old code used
      # `% total_block_content` to loop — which replayed the SAME episodes over
      # and over for the whole block. Instead, we track how many full cycles have
      # elapsed and call build_show_rotation again for each completed one, so
      # every show advances to its NEXT episode whenever the playlist wraps.
      #
      # sched_cycle_boundary: seconds_into_block where the current cycle started.
      # sched_cycle_total:    total_block_content of the current cycle.
      # Both reset to 0 on every new block (above) and persist across restarts.
      #
      # Marathon drives its own epoch clock and never loops within a block, so
      # it is exempt from this logic.
      if not _marathon_mode:
          _cyc_boundary = playback_log.get("sched_cycle_boundary", 0)
          _cyc_total    = playback_log.get("sched_cycle_total", 0) or total_block_content
          # Guard against backward time jumps (e.g. clock change): if the stored
          # boundary is ahead of where we are now, reset to the block start so we
          # never produce a negative position.
          if _cyc_boundary > seconds_into_block:
              _cyc_boundary = 0
              _cyc_total    = total_block_content
          _cycle_changed    = False
          _show_order_by_block2 = playback_log.setdefault("show_order", {})
          _sp2              = dict(playback_log.get("show_pos", {}))
          _new_show_order2  = _show_order_by_block2.get(current_block)
          while total_block_content > 0 and seconds_into_block >= _cyc_boundary + _cyc_total:
              # A full cycle just finished — advance every show to its next
              # episode set by running build_show_rotation one more time.
              _cyc_boundary += _cyc_total
              _cycle_changed = True
              video_tracks, _sp2, _new_show_order2 = build_show_rotation(
                  raw_tracks, scheduling_mode, _sp2, day_seed,
                  pair_threshold_seconds=_pair_thresh_sec,
                  episode_order_mode=ch_info.get("episode_order_mode", "sequential"),
                  fixed_show_order=_show_order_by_block2.get(current_block),
                  block_duration_seconds=block_duration_seconds
              )
              timeline, total_block_content = build_block_timeline(
                  video_tracks, commercial_tracks, commercial_placement, day_seed,
                  interrupt_floor_seconds=INTERRUPT_FLOOR_SECONDS
              )
              if not timeline or total_block_content <= 0:
                  break
              _cyc_total = total_block_content
          if _cycle_changed and timeline and total_block_content > 0:
              # Persist the advanced show_pos, show_order, and updated cached tracks
              # so the next call (and the guide) sees the current cycle's lineup.
              playback_log["show_pos"] = _sp2
              if _new_show_order2 is not None:
                  _show_order_by_block2[current_block] = _new_show_order2
              _cache_paths2 = []
              for _ct2 in video_tracks:
                  if _ct2.get("is_pair"):
                      _cache_paths2.append(_ct2.get("pair_ep1_path", _ct2["path"]))
                      _cache_paths2.append(_ct2.get("pair_ep2_path", ""))
                  else:
                      _cache_paths2.append(_ct2.get("path", ""))
              playback_log["sched_cached_tracks"] = [p for p in _cache_paths2 if p]
              # Freeze the new cycle's durations at the moment it's committed --
              # see _snapshot_track_durations docstring.
              playback_log["sched_cached_durations"] = _snapshot_track_durations(video_tracks)
              try: db.save_settings()
              except Exception as e: log.warning("calculate_slotted_playback_state: failed to persist cycle-advanced sched: %s", e)
              # Invalidate the guide's timeline cache so it rebuilds with the
              # new cycle's episodes rather than serving the previous cycle's
              # stale cached timeline on every subsequent frame.
              app_state["guide_refresh_token"] = app_state.get("guide_refresh_token", 0) + 1
          playback_log["sched_cycle_boundary"] = _cyc_boundary
          playback_log["sched_cycle_total"]    = _cyc_total
          if not timeline or total_block_content <= 0:
              return {"mode": "Static", "file": "", "seek": 0}
          position_in_playlist = seconds_into_block - _cyc_boundary
      else:
          position_in_playlist = seconds_into_block % total_block_content

      target_show = None
      for item in timeline:
          if item["start_time"] <= position_in_playlist < (item["start_time"] + item["duration"]):
              target_show = item
              break

      if target_show is None:
          return {"mode": "Static", "file": "", "seek": 0}

      seek_pos    = position_in_playlist - target_show["start_time"]
      actual_seek = target_show.get("seek_offset", 0) + seek_pos
      # Expose the slot's own starting file position so anchor-setters can
      # store it as `slot_start_seek` for the corrected remaining-time formula.
      slot_start_seek = target_show.get("seek_offset", 0)
      mode = "Commercial" if target_show.get("is_commercial") else "Video"
      # Resolve paired episodes: the timeline treats the pair as one slot, but
      # VLC must play one real file at a time.  actual_seek is the position within
      # the COMBINED duration, so we redirect to ep2 once ep1's runtime is past.
      resolved_file = target_show["path"]
      resolved_seek = actual_seek
      resolved_dur  = target_show["duration"]
      if target_show.get("is_pair") and mode == "Video":
          ep1_dur = target_show.get("pair_ep1_duration", 0)
          if actual_seek >= ep1_dur:
              resolved_file = target_show.get("pair_ep2_path", resolved_file)
              resolved_seek = actual_seek - ep1_dur
              resolved_dur  = target_show.get("pair_ep2_duration",
                                              max(0, target_show["duration"] - ep1_dur))
          else:
              resolved_file = target_show.get("pair_ep1_path", resolved_file)
              resolved_dur  = ep1_dur
      return {"mode": mode, "file": resolved_file, "seek": resolved_seek,
              "duration": resolved_dur, "block": current_block,
              "seek_slot_offset": slot_start_seek}


# ==============================================================================
# BLACK-SCREEN FIX: BROKEN-FILE DETECTION & LIVE RESCHEDULE
# ==============================================================================
# calculate_slotted_playback_state() only filters out files that were already
# missing at BLOCK-BUILD time (os.path.exists at gather time). A file that
# existed then but fails to actually load when its slot comes up (moved,
# corrupted, a network-drive hiccup, permissions) used to be handed to VLC
# silently -- play_file_segmented() no-op'd, is_playing_video was set True
# anyway, and the viewer got a black screen until the ~1.5s watchdog forced a
# retune (which just failed again in a loop until the slot naturally rolled
# over). The functions below let change_channel() react to that failure
# immediately: they pick a substitute EPISODE OF THE SAME SHOW (honoring
# episode_order_mode) and rewrite this block's cached future schedule so
# upcoming slots for that show stay consistent, instead of leaving the bad
# file sitting in rotation to fail again next time it comes up.

def _mark_file_broken(ch_info, file_path):
    """Permanently excludes file_path from future scheduling for this
    channel (persisted), so a confirmed-unplayable file is never handed to
    VLC again -- not this block, not any future one."""
    broken = ch_info.setdefault("broken_files", [])
    if file_path not in broken:
        broken.append(file_path)
        log.warning("Marking file unplayable, removing from future scheduling: %s", file_path)
    try:
        if db is not None:
            db.save_settings()
    except Exception as e:
        log.warning("_mark_file_broken: failed to persist broken_files for %s: %s", file_path, e)


def _get_show_episode_context(ch_info, current_block, file_path):
    """
    Rebuilds the show/episode grouping for this channel's current block
    (same grouping media.split_movies_and_series/group_shows use everywhere
    else) and returns (show_name, natural_ordered_episode_list) for whichever
    show owns file_path. Returns (None, []) if file_path isn't part of a
    tracked show (e.g. a movie, or the schedule no longer contains it) --
    callers should treat that as "nothing to substitute against."
    """
    folder_items = ch_info.get("schedules", {}).get(current_block, [])
    broken = set(ch_info.get("broken_files", []))
    raw = []
    _media_probe_cache_ctx = (db.config.get("media_probe_cache", {}) if db is not None else {})
    for asset in folder_items:
        path = asset.get("path", asset.get("file", "")) if isinstance(asset, dict) else str(asset)
        if path and path not in broken and os.path.exists(path):
            # SINGLE SOURCE OF TRUTH (media.resolve_track_duration) — must
            # classify movies/series the same way every other call site
            # does, or an unprobed movie gets misread as "part of a show"
            # here even after split_movies_and_series has correctly pulled
            # it out everywhere else.
            stored = asset.get("duration", 0) if isinstance(asset, dict) else 0
            raw.append({"path": path, "duration": resolve_track_duration(_media_probe_cache_ctx, path, stored)})
    _movie_pool, show_names, show_episodes = split_movies_and_series(raw)
    for sn in show_names:
        eps = show_episodes.get(sn, [])
        if any(e["path"] == file_path for e in eps):
            return sn, eps
    return None, []


def _resolve_broken_episode(ch_info, failed_file, current_block):
    """
    Called the moment `failed_file` (already scheduled/attempted) refuses to
    play. Marks it broken, then picks a substitute episode of the SAME show
    and updates ch_info["playback_log"]["sched_cached_tracks"] (this block's
    future schedule) to match, per episode_order_mode:

      - "sequential": substitute = next PLAYABLE episode after the failed
        one in natural order. Every LATER occurrence of this show already
        sitting in the cached block is shifted forward the same number of
        steps, so e.g. failing S01E01 plays S01E02 now and the slot that was
        going to be S01E02 later becomes S01E03 -- the whole sequence moves
        up rather than repeating what just got played early.
      - "random": substitute = a random PLAYABLE episode of the show that
        hasn't already aired earlier in this block. If the show's entire
        playable rotation has already aired this block (nothing "yet to
        play" left), that show's footprint is cleared from the cache and
        the rotation starts over from its full playable episode list.
        The chosen substitute is recorded via note_random_episode_played()
        so it also counts toward that show's cross-day "every episode airs
        before any repeat" cycle tracked in show_pos, not just this block.

    Returns {"file", "seek": 0, "duration"} for the substitute to try next,
    or None once every episode of the show has been confirmed unplayable --
    callers should leave the black-screen crash protection engaged in that
    case rather than force anything else on screen.
    """
    _mark_file_broken(ch_info, failed_file)

    show_name, eps = _get_show_episode_context(ch_info, current_block, failed_file)
    playback_log = ch_info.setdefault("playback_log", {})
    cached = playback_log.get("sched_cached_tracks", [])

    if show_name is None or not eps:
        return None

    broken = set(ch_info.get("broken_files", []))
    playable_eps = [e for e in eps if e["path"] not in broken]
    if not playable_eps:
        # Every episode of this show has now been confirmed unplayable.
        return None

    show_paths = [e["path"] for e in eps]
    episode_order_mode = ch_info.get("episode_order_mode", "sequential")

    if episode_order_mode == "random":
        already_in_block = {p for p in cached if p in show_paths}
        candidates = [e for e in playable_eps if e["path"] not in already_in_block]
        if not candidates:
            # Whole playable rotation for this show already aired this
            # block -- clear its footprint from the cache and start the
            # rotation over from the top so it plays through again.
            cached = [p for p in cached if p not in show_paths]
            candidates = list(playable_eps)
        substitute = random.choice(candidates)
        cached = [substitute["path"] if p == failed_file else p for p in cached]
        playback_log["sched_cached_tracks"] = cached
        _idx = next((i for i, e in enumerate(eps) if e["path"] == substitute["path"]), 0)
        note_random_episode_played(playback_log.setdefault("show_pos", {}), show_name, len(eps), _idx)
    else:
        n = len(eps)
        try:
            failed_idx = next(i for i, e in enumerate(eps) if e["path"] == failed_file)
        except StopIteration:
            failed_idx = 0

        step = 1
        substitute = None
        while step <= n:
            cand = eps[(failed_idx + step) % n]
            if cand["path"] not in broken:
                substitute = cand
                break
            step += 1
        if substitute is None:
            return None

        def _shift_forward(path, steps):
            if path not in show_paths:
                return path
            idx = show_paths.index(path)
            moved = 0
            tries = 0
            while moved < steps and tries < n * 2:
                idx = (idx + 1) % n
                tries += 1
                if eps[idx]["path"] not in broken:
                    moved += 1
            return show_paths[idx]

        new_cached = []
        past_failed = False
        for p in cached:
            if p == failed_file and not past_failed:
                new_cached.append(substitute["path"])
                past_failed = True
            elif past_failed and p in show_paths:
                new_cached.append(_shift_forward(p, step))
            else:
                new_cached.append(p)
        playback_log["sched_cached_tracks"] = new_cached
        _cur_pos = playback_log.setdefault("show_pos", {}).get(show_name, 0)
        playback_log["show_pos"][show_name] = (_cur_pos + step) % n

    try:
        if db is not None:
            db.save_settings()
    except Exception as e:
        log.warning("_resolve_broken_episode: failed to persist rescheduled cache for show '%s': %s", show_name, e)

    return {"file": substitute["path"], "seek": 0, "duration": substitute.get("duration", 1800)}


# --- THREADED BACKGROUND QUEUE PROCESSOR REGISTER ---
explorer_queue = queue.Queue()

# ==============================================================================
# PART 5 OF 28: GENERATIVE HARDWARE AUDIO & STATIC SOUND MIXERS
# ==============================================================================

def _perceptual_volume_pct(slider_pct, min_db=-40.0):
    """Map a raw 0-100 volume-SLIDER position onto a 0-100 actual playback
    level using a roughly equal-loudness-per-tick curve, instead of treating
    the slider position as a linear amplitude percentage.

    The ear perceives loudness on a log (dB) scale, not a linear amplitude
    scale. Feeding the raw slider value straight into VLC's audio_set_volume
    (or a straight vol/100.0 fraction into pygame) means the top half of the
    slider (100 -> 50) barely changes perceived loudness, while the bottom
    third (50 -> 15) drops off a cliff -- exactly the "no change until 50,
    then it dives" behavior. This fixes that by treating the slider as a
    position along a fixed dB range and converting back to a linear
    amplitude multiplier, so every tick is a roughly equal perceived step:

        slider=100 -> 0dB      (unity gain, unchanged from before)
        slider=0   -> min_db   (near-silent, floor of the range)

    min_db defaults to -40, chosen for channels whose top-of-slider level
    IS the channel's true unity/reference volume (TV/video via VLC, static).
    Pass a shallower (less negative) min_db -- see STACKED_GAIN_MIN_DB below
    -- for channels that ALSO multiply in a separate fixed attenuation
    constant on top of this curve's result (e.g. LOUDNESS_GAIN, MUSIC_GAIN).
    Those constants already cut the top-of-slider level down from true
    unity to compensate for hotter-than-TV raw source material; stacking
    the full -40dB curve on top of that additionally compounds into
    inaudibility far too early (reported: the game channel was already
    silent by 60%, well before the slider reached bottom).
    """
    pct = max(0, min(100, slider_pct))
    if pct <= 0:
        return 0.0
    frac = pct / 100.0
    db_level = min_db * (1.0 - frac)      # frac=1 -> 0dB, frac=0 -> min_db
    amplitude_frac = 10 ** (db_level / 20.0)
    return amplitude_frac * 100.0


# Shallower curve floor for any channel that ALSO applies a fixed
# hotness-compensation multiplier on top of _perceptual_volume_pct's result
# (LOUDNESS_GAIN for the game channel, MUSIC_GAIN for music/visualizer/ch03
# menu music). Those multipliers already push the top-of-slider level well
# below true unity; stacking the standard -40dB curve on top of that was
# compounding into silence far too early in the slider's range.
#
# -24dB (the first attempt) wasn't shallow enough: two rounds of real-world
# testing on the game channel (LOUDNESS_GAIN=0.45) both went inaudible at
# the SAME absolute output level (~0.07-0.075) regardless of curve shape --
# at -40dB that level was reached by 60% slider, at -24dB by 35% slider.
# That's a fixed perceptual/hardware floor for this content, not something
# curve-shape alone fixes -- the only way to keep it audible further down
# the slider is to reach that floor closer to 0%, which needs a shallower
# range still. -18dB solves for that floor landing around 10-15% slider,
# matching how the un-stacked channels (TV, static) fade out.
STACKED_GAIN_MIN_DB = -18.0


# Shallower floor for the plain TV/VLC channel itself (previously hardcoded
# to the default -40dB in VLCEngineWrapper.set_volume). Diagnostic logging
# (added to chase a "TV audio goes silent below ~60-65% slider" report)
# confirmed the value VLC actually receives at that point is genuinely
# non-zero and mathematically exactly what -40dB predicts (e.g. slider=65%
# -> boosted=19 out of VLC's 0-200 scale, ~-14dB below reference) -- so this
# isn't a truncation/gain bug, `file_gain` was a clean 1.0 the whole time.
#
# First attempt at -24dB just moved the same problem instead of fixing it:
# re-tested silent below 35% instead of 65%, at boosted=16 -- i.e. the SAME
# absolute output level (boosted ~16-19 / 200, roughly -14 to -16dB below
# reference) regardless of curve shape, just crossed at a different slider
# position. That's the identical real hardware/perceptual floor already
# documented above for STACKED_GAIN_MIN_DB, on a totally different audio
# pipeline (this is libVLC's own audio_set_volume, not a WASAPI/pycaw
# session volume) -- strong evidence it's a genuine floor of this specific
# hardware/speaker setup, not a per-pipeline quirk. Going straight to the
# already-validated -18dB from STACKED_GAIN_MIN_DB rather than guessing a
# third value -- the math puts that floor crossing around 10-15% slider,
# matching what -18dB was proven to do for the other channels.
VLC_GAIN_MIN_DB = -18.0


def _effective_audio_gain(min_db=-40.0):
    """Effective 0.0-1.0 playback gain for pygame Sound objects and
    pygame.mixer.music, with MUTE honored.

    Every spot that (re)starts or reloads static/music audio MUST compute its
    volume through this instead of reading global_volume directly. Otherwise a
    track that reloads on a channel change, TV-guide preview, or menu open
    comes back at full volume even while is_muted is still set — that was the
    'mute keeps turning itself back off' bug. Returns 0.0 when muted so a
    reloaded track stays silent until the user explicitly unmutes.

    The raw slider value is passed through _perceptual_volume_pct() so this
    stays perceptually consistent with the VLC path (see
    VLCEngineWrapper.set_volume), which applies the same curve. Callers that
    multiply their own fixed hotness-compensation constant on top of this
    (i.e. every pygame.mixer.music.set_volume(...) * MUSIC_GAIN call site)
    MUST pass min_db=STACKED_GAIN_MIN_DB -- see that constant's docstring."""
    if db is not None and db.config.get("is_muted", False):
        return 0.0
    vol = db.config.get("global_volume", 70) if db is not None else 70
    return _perceptual_volume_pct(vol, min_db=min_db) / 100.0


def load_custom_static_sound():
    # FIXED (exe-only "harsh repeating tone over static" distortion): this
    # used to resolve static.wav via a naive os.path.dirname(os.path.abspath
    # (__file__)) join, instead of resource_path() -- the sys._MEIPASS-aware
    # helper every other bundled asset in this file goes through (see
    # resource_path() above, and the _bootstrap_libvlc base_dir fix for the
    # exact same class of bug with the VLC DLLs). In the packaged exe that
    # naive path doesn't reliably land on the bundled main/audio/static.wav,
    # so the file "doesn't exist" from the app's point of view even though
    # it's sitting right there in the build -- silently falling into the
    # broken noise fallback below on every boot, exe-only.
    w_path = resource_path(os.path.join("main", "audio", "static.wav"))

    if os.path.exists(w_path):
        try: 
            s = pygame.mixer.Sound(w_path)
            s.set_volume(_effective_audio_gain())
            return s
        except Exception as e:
            log.warning("load_custom_static_sound: failed to load %s, falling back to generated noise: %s", w_path, e)

    # Fallback: generate simple white noise.
    #
    # FIXED: this used to build a bytearray of RANDOM BYTES and hand it
    # straight to pygame.mixer.Sound(buffer=...), which interprets the
    # buffer as raw audio in whatever format the mixer was actually
    # init'd with (16-bit signed stereo by default here). Random bytes
    # read as 16-bit stereo samples is full-scale harsh digital noise, in
    # a buffer barely 0.1s long -- and this Sound gets looped forever via
    # play(-1) -- so instead of soft static you got a short ugly tone
    # slamming into full volume and repeating rapidly. That's the exact
    # "harsh tone repeated quickly over the static" distortion, and since
    # the naive path above meant this fallback fired routinely in the exe
    # build, it fired there routinely too.
    #
    # Fix: build real 16-bit PCM white noise at low amplitude, matching
    # the mixer's actual init'd format/frequency/channel count, so a
    # missing static.wav degrades to genuinely soft static instead of a
    # malformed screech.
    try:
        _mixer_freq, _mixer_fmt, _mixer_ch = pygame.mixer.get_init()
    except Exception:
        _mixer_freq, _mixer_fmt, _mixer_ch = 44100, -16, 2
    _mixer_ch = _mixer_ch if _mixer_ch in (1, 2) else 2
    _duration_s = 1.0
    _n_frames = int(_mixer_freq * _duration_s)
    _amplitude = 3000  # low amplitude out of a possible +/-32767, so this reads as soft hiss, not a blast
    _samples = array.array('h')
    for _ in range(_n_frames):
        _val = random.randint(-_amplitude, _amplitude)
        _samples.append(_val)
        if _mixer_ch == 2:
            _samples.append(_val)
    s = pygame.mixer.Sound(buffer=_samples.tobytes())
    s.set_volume(_effective_audio_gain())
    return s

# This must be defined early
static_audio = load_custom_static_sound()
print("[TELEMETRY - PART 4] Static audio noise generator loaded.")


def _render_transition_static(screen, rect):
    """SINGLE SOURCE OF TRUTH for "TV static" — same look (animated
    per-pixel noise, redrawn fresh every frame, pillarboxed into `rect`)
    and same sound (looped static_audio, via play(-1)) as the real
    "no media assigned yet" Static channel mode. Every place in the app
    that needs a static effect — the real Static playback mode, the
    channel-switch transition, and the TV guide's inline preview shield —
    calls this exact function instead of keeping its own copy, so a
    "static" transition can never again look/sound different from genuine
    no-signal static (previously the transition had its own duplicated,
    non-looping version, which both undersold the effect and could bleed
    into whatever channel came next since nothing was tracking it).

    Caller is responsible for calling _stop_transition_static() the moment
    its own transition window ends — this function only ever STARTS the
    loop (idempotently; a already-looping Sound is left alone), it never
    stops it, since it doesn't know when the caller's window is over.
    """
    # NOTE: this used to guard with `not pygame.mixer.get_busy()`, which checks
    # ALL active mixer channels globally, not just static_audio's own. Any other
    # sound playing anywhere (a boot sound, ch03 menu music, an SFX on another
    # channel) made get_busy() return True and silently blocked static_audio
    # from ever starting -- the visuals still rendered fine since they don't
    # go through this check, which is exactly why static could be seen but not
    # heard on first boot. get_num_channels() is specific to this one Sound, so
    # it only ever reports whether static_audio ITSELF is already looping.
    if 'static_audio' in globals() and static_audio is not None and static_audio.get_num_channels() == 0:
        static_audio.play(-1)
        static_audio.set_volume(_effective_audio_gain())

    sw, sh = rect.width // 4, rect.height // 4
    if sw > 0 and sh > 0:
        surf = pygame.Surface((sw, sh))
        for y in range(sh):
            for x in range(sw):
                grey = random.randint(20, 85)
                surf.set_at((x, y), (grey, grey, grey))
        screen.blit(pygame.transform.scale(surf, (rect.width, rect.height)), (rect.x, rect.y))
    else:
        screen.fill((1, 1, 1), rect)


def _stop_transition_static():
    """Companion to _render_transition_static() — every caller of that
    function must call this the instant its transition window closes (or
    it lands on a channel that should never have static audio at all, e.g.
    Channel 03) so the loop can never keep playing unsupervised into
    whatever comes next."""
    if 'static_audio' in globals() and static_audio is not None:
        static_audio.stop()


def _run_pending_channel_activation():
    """SINGLE ACTIVATION CHECKPOINT — replaces the old per-subsystem
    mute-then-restore flags (_ch03_audio_unmuted_after_transition,
    _chaudio_audio_unmuted_after_transition/_chaudio_pending_kind/
    _chaudio_pending_unmute_state).

    change_channel() no longer starts any destination-channel audio or
    video itself. Instead it packages up exactly one callable describing
    "what the new channel needs to do to actually start" and stores it in
    app_state["_pending_channel_activation"]. This function is the ONLY
    place that callable is ever invoked, and it is only ever called from
    the render loop's shield-just-expired checkpoints (the "elif ch=='03'"
    and "elif ch != '04'" branches below) — i.e. strictly after the
    static/black transition has already finished covering the screen.

    Because nothing starts until then, everything can be started at its
    real, final volume the first time. There's no muted state to remember
    to undo later, so there's nothing left to race.
    """
    _fn = app_state.pop("_pending_channel_activation", None)
    if _fn is not None:
        try:
            _fn()
        except Exception as e:
            log.warning("[ACTIVATION] pending channel activation failed: %s", e)


# ==============================================================================
# PART 6 OF 28: CORE PLAYBACK CONTROLLERS & CHANNEL SURFING LOGIC NODES (DEBUG POOL)
# ==============================================================================

def change_channel(new_ch, is_surfing=False, force_reload=False, internal_advance=False, is_boot=False):
    """internal_advance=True marks a call that ISN'T a real tune -- specifically
    the end-of-show watchdog re-invoking this same function on the SAME channel
    just to load the next scheduled item once the previous one finishes. It
    reuses this function because loading the next item still needs everything
    else here (mixer/VLC bookkeeping, channel-03 special-casing, etc.), but a
    show ending on its own is not the user picking up the remote -- so it
    suppresses the user-facing "you just changed the channel" effects (the
    "CH XX" OSD banner and the black/static transition flash) that a real tune
    should get and an automatic same-channel advance should not.

    is_boot=True marks the one-time initial tune the boot sequence itself
    performs to land on the last-watched channel. The black/static transition
    is meant to sell the FEEL of changing channels -- there's no "previous
    channel" to be leaving on boot, so it's suppressed the same way
    internal_advance suppresses it. Unlike internal_advance, this leaves the
    "CH XX" OSD banner alone: seeing which channel you booted into is still
    useful the same way a real TV briefly shows it after power-on, it just
    shouldn't be preceded by a burst of static that was never leaving anything."""
    if (app_state.get("current_channel") == new_ch and app_state.get("is_playing_video", False)
            and not force_reload):
        return

    # HARD DISPLAY BOUNDS GUARD
    if not pygame.display.get_surface():
        print("[HARDWARE SHIELD] Refusing channel switch pass: Surface layout is uninitialized.")
        return

    # UNIFIED AUDIO PURGE LAYER: Forcefully kill static noises during channel transitions
    if 'static_audio' in globals() and static_audio is not None:
        static_audio.stop()
    
    old_ch = app_state.get("current_channel", "05")
    print(f"\n[DEBUG TUNER] --- START CHANNEL SWAP LOG ---")
    print(f"[DEBUG TUNER] Swapping from Channel {old_ch} -> Channel {new_ch}")
    
    # --- CHANNEL 03 SPECIAL HANDLING ---
    if old_ch == "03" and new_ch != "03":
        # Stop menu music whenever we leave ch03
        _stop_ch03_menu_music()
        if game_deck is not None and game_deck.is_process_running():
            _lr = getattr(game_deck, "_libretro_core", None)
            if _lr is not None and _lr.is_loaded:
                # Embedded libretro core -- freeze_libretro() mutes
                # synchronously right here (LibretroAudio.set_muted() stops
                # its pygame Channel immediately), so there's no gap for
                # this case to leak through the transition shield.
                game_deck.freeze_libretro()
            else:
                # External/standalone emulator process (no libretro core
                # loaded) -- its audio lives in a separate OS process, only
                # mutable via a Windows per-process session-volume call
                # (pycaw/WASAPI). That call is inherently async (COM session
                # enumeration), so it's kicked off in a thread here, as early
                # as possible -- before the shield timer below is even set,
                # so it gets the transition's full ~1s window to land. A
                # slow first COM enumeration can still occasionally lose
                # that race; there's no way to make the call itself
                # synchronous here without risking a UI freeze on the main
                # render thread.
                import threading as _t
                _t.Thread(
                    target=lambda: (
                        game_deck.set_viewport_state(visible=False),
                        game_deck.set_audio_state(muted=True)
                    ),
                    daemon=True
                ).start()
        # DVD BACKGROUND-KEEP-ALIVE. The DVD player runs on its OWN dedicated
        # engine (dvd_engine), which no other channel ever touches. So leaving
        # channel 03 does NOT stop or reopen anything -- it simply MUTES that
        # engine and leaves the disc running exactly as-is. Whatever was on
        # screen keeps going untouched in the background: the root menu / a
        # submenu stays on the very same highlighted option, and a playing
        # movie keeps advancing in real time. Channel-return is then just an
        # unmute (see the ch03 resume block below), so nothing about the
        # on-screen position is ever lost and there is no dvdnav reopen to
        # land the user back on the root menu. This replaces the entire old
        # stop + position/menu-snapshot + dvdnav-reopen machinery, which only
        # existed because the DVD used to share the single main vlc_engine and
        # could be reclaimed by another video channel.
        if (game_deck is not None and game_deck.dvd_playback_active
                and dvd_engine is not None):
            try:
                dvd_engine.player.audio_set_mute(True)
                game_deck.dvd_iso_backgrounded = True
                print("[DVD] Backgrounded on channel leave -- dvd_engine muted, "
                      "left running as-is (unmute-in-place on return, no reopen).")
            except Exception as _me:
                print(f"[DVD] Failed to mute backgrounded DVD on channel leave: {_me}")
    
    # Commit the channel switch NOW, before the ch03-entry block below. This
    # used to happen much later (after the ch03 block), which meant the very
    # first tune-in to channel 03 from anywhere else always found
    # app_state["current_channel"] still holding the OLD channel at the exact
    # moment _start_ch03_menu_music() ran — its own guard
    # (`if app_state.get("current_channel") != "03": return`) then bailed out
    # immediately every single time, so menu music silently never started on
    # first entry. Manually toggling the music setting off/on "fixed" it only
    # because that path calls _start_ch03_menu_music() later, by which point
    # current_channel had already (eventually) been set to "03" elsewhere.
    app_state["current_channel"] = new_ch

    # PERSIST LAST CHANNEL CONTINUOUSLY (not just on a graceful quit): before
    # this fix, "last_channel" was only ever written to disk from the
    # pygame.QUIT handler and the Exit Program menu action. A force-killed /
    # frozen / crashed process never reaches either of those, so the next
    # boot always fell back to the default channel even though the user had
    # genuinely tuned somewhere else. Saved here via the existing
    # throttled/async writer (save_settings_async), so this never blocks the
    # channel switch itself. Lightly debounced so holding channel-up/down to
    # surf doesn't spawn a save thread on every single tick.
    if db is not None:
        db.config["last_channel"] = new_ch
        _now_ms = pygame.time.get_ticks()
        if _now_ms - app_state.get("_last_channel_save_ms", -10_000) >= 1000:
            app_state["_last_channel_save_ms"] = _now_ms
            try:
                db.save_settings_async()
            except Exception as _lc_err:
                print(f"[PERSIST] last_channel async save failed: {_lc_err}")

    _ch03_activate = None
    if new_ch == "03":
        if game_deck is not None:
            _muted = db.config.get("is_muted", False) if db else False
            # Re-sync active profile settings when returning to the games channel
            _active_con = getattr(game_deck, "active_console", None) or getattr(game_deck, "_libretro_console", None)
            if _active_con and db and hasattr(game_deck, "sync_profile_from_db"):
                game_deck.sync_profile_from_db(_active_con, db.config)
            _wants_menu_music = (
                getattr(game_deck, "mode", "BROWSE") in ("BROWSE", "ROMLIST")
                and not getattr(game_deck, "dvd_playback_active", False)
            )
            _lr = getattr(game_deck, "_libretro_core", None)
            if _lr is not None and _lr.is_loaded:
                # NEW TRANSITION ARCHITECTURE: an already-loaded core has live
                # audio to leak, so its unfreeze/resume (and menu music) is
                # deferred to _run_pending_channel_activation(), fired only
                # once the shield has actually finished covering the screen.
                # Nothing plays muted-then-gets-unmuted here anymore -- it
                # simply doesn't start until it's safe to be heard.
                def _ch03_activate(_muted=_muted, _wants_menu_music=_wants_menu_music):
                    game_deck.unfreeze_libretro(muted=_muted)
                    game_deck.set_libretro_volume(db.config.get("global_volume", 70) if db else 70, _muted)
                    if _wants_menu_music:
                        _start_ch03_menu_music()
            elif game_deck.is_process_running():
                def _ch03_activate(_muted=_muted, _wants_menu_music=_wants_menu_music):
                    import threading as _t
                    _t.Thread(
                        target=lambda: (
                            game_deck.set_viewport_state(visible=True),
                            game_deck.set_audio_state(muted=_muted)
                        ),
                        daemon=True
                    ).start()
                    if _wants_menu_music:
                        _start_ch03_menu_music()
            else:
                # Nothing loaded yet -- no live audio can leak, so booting the
                # background deck is safe to kick off immediately rather than
                # waiting on the shield (it takes real time itself).
                game_deck.pre_boot_background_deck(parent_hwnd=pygame_hwnd)
                if _wants_menu_music:
                    def _ch03_activate(_wants_menu_music=_wants_menu_music):
                        _start_ch03_menu_music()
        
    # Capture our current playing track BEFORE running any calculation updates
    current_playing_track = app_state.get("active_music_track_file", "")
    print(f"[DEBUG TUNER] Track string cached in memory BEFORE timeline calculation: '{current_playing_track}'")

    # --- LEAVE ANCHOR: snap the old video channel's playback_anchor to the live VLC
    # position right now, so returning to it picks up exactly where we left off rather
    # than re-running the wall-clock scheduler (which can restart the show if the stored
    # duration is shorter than the real video or if the user watched past the estimate).
    _old_snap_key = str(old_ch).zfill(2)
    if (old_ch not in ("03", "04") and vlc_engine is not None
            and app_state.get("is_playing_video", False) and db is not None):
        try:
            _pos_ms  = vlc_engine.player.get_time()
            _dur_ms  = vlc_engine.player.get_length()
            _old_ch_entry = db.channels_db.setdefault(_old_snap_key, {})
            _old_anc = _old_ch_entry.get("playback_anchor") or {}
            # BUG FIX (episode restarts on channel return): this used to
            # require _old_anc to already have a "file" set before writing
            # anything, i.e. it could only ever UPDATE an existing anchor.
            # playback_anchor is routinely {} at this point -- the
            # end-of-show watchdog clears it the moment an episode finishes
            # (see the "playback_anchor = {}" resets elsewhere) -- so any
            # channel switch that happened to land right after an episode
            # boundary, or the very first switch since boot, silently wrote
            # NOTHING here. Returning to that channel then found no anchor
            # file at all, fell straight through to the wall-clock
            # scheduler, and recomputed a fresh slot -- which read exactly
            # like the episode restarting from the beginning even though
            # VLC had a perfectly good live position a moment ago.
            #
            # Fix: build a fresh anchor dict from the live VLC file/position
            # when one doesn't already exist, instead of only patching one
            # that does. We need the actual playing file's path for this
            # (the old file-less code path never needed it since it only
            # ever touched an anchor that already carried "file").
            if _pos_ms is not None and _pos_ms >= 0:
                _live_file = None
                try:
                    _media = vlc_engine.player.get_media()
                    if _media:
                        from urllib.parse import unquote as _unquote2
                        _mrl = _media.get_mrl()
                        if _mrl:
                            _live_file = os.path.normpath(_unquote2(_mrl.replace("file:///", "")))
                except Exception as _mrl_err:
                    log.warning("change_channel: could not read live MRL for leave-anchor on ch%s: %s", _old_snap_key, _mrl_err)

                if not _old_anc.get("file") and _live_file:
                    _old_anc = {
                        "file": _live_file,
                        "mode": "Video",
                        "block": _old_anc.get("block", "Morning"),
                        "duration": max(1800, int(_dur_ms // 1000)) if _dur_ms and _dur_ms > 0 else 1800,
                        "slot_start_seek": 0,
                    }

                if _old_anc.get("file"):
                    _now_l = datetime.datetime.now()
                    _old_anc["wall_start"]  = _now_l.hour * 3600 + _now_l.minute * 60 + _now_l.second
                    _old_anc["seek_offset"] = int(_pos_ms // 1000)
                    if _dur_ms and _dur_ms > 0:
                        _old_anc["duration"] = max(_old_anc.get("duration", 1800), int(_dur_ms // 1000))
                    _old_ch_entry["playback_anchor"] = _old_anc
                    print(f"[ANCHOR] Snapped ch{_old_snap_key} leave-position to {_old_anc['seek_offset']}s.")
        except Exception as e:
            log.warning("change_channel: failed to snap leave-position anchor for ch%s: %s", _old_snap_key, e)

    # IMMEDIATE CUTOFF: stop whatever the OLD channel was playing right now,
    # synchronously, instead of leaving it running underneath the transition
    # shield. Video/visualizer playback for the NEW channel is intentionally
    # deferred until the shield expires (see _content_activate below), but
    # that deferral was letting the OUTGOING channel's audio keep playing
    # the entire time the shield is up -- clearly audible for the full 1s
    # "static" burst, since nothing ever told VLC/the mixer to stop until
    # the new channel's activation closure got around to it. The leave-anchor
    # snapshot above already captured the exact resume position, so it's
    # safe to cut audio here.
    #
    # This ALWAYS cuts the main engine -- the DVD player runs on its own
    # separate dvd_engine, which this never touches, so there's no longer any
    # ch03 exception to make (the old shared-engine "_ch03_dvd_staying" guard
    # was removed with the two-engine rebuild). Muting/unmuting the DVD on
    # leave/return is handled entirely on dvd_engine by the ch03 blocks.
    if vlc_engine is not None:
        try:
            # Unconditional stop() rather than gating on is_playing() --
            # VLC can be sitting in a transitional state (Opening/
            # Buffering) where is_playing() reads False but the player
            # still has queued audio. stop() is safe/idempotent even if
            # nothing is actually playing.
            vlc_engine.stop()
        except Exception as e:
            print(f"[CHANNEL] immediate outgoing vlc stop failed: {e}")
    if pygame.mixer.music.get_busy():
        pygame.mixer.music.stop()

    t_state = calculate_slotted_playback_state(new_ch)
    
    # FIXED: Only protect Channel 04 (TV Guide Menu) from being hijacked by video files
    if new_ch == "04":
        t_state["mode"] = "Static"
        t_state["file"] = ""
        t_state["seek"] = 0
        
    next_track_path = t_state.get("file", "")
    print(f"[DEBUG TUNER] Playlist lookup mode returned: '{t_state.get('mode')}' | Target File: '{next_track_path}'")
        
    try:
        old_ch_info = db.channels_db.get(str(old_ch).zfill(2), {}) if db is not None else {}
        if (old_ch_info.get("is_visualizer", False) and new_ch != "04") or (new_ch == "04" and old_ch != "04" and not old_ch_info.get("is_visualizer", False)):
            if next_track_path != current_playing_track or not pygame.mixer.music.get_busy():
                pygame.mixer.Channel(1).stop()
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
    except Exception as e: print(f"[CHANNEL] visualizer music stop failed: {e}")

    ch_info = db.channels_db.get(str(new_ch).zfill(2), {}) if db is not None else {}
    if not ch_info.get("is_visualizer", False) and new_ch != "04":
        app_state["persistent_track_pool_cache"] = ""
        
    app_state["digit_buffer"] = ""
    app_state["digit_timer"] = 0
    
    # Drop the cached bridge frame so the black window isn't followed by the
    # OLD channel's last decoded frame papering over the new channel's VLC
    # startup buffering (~1-1.5s). Without this, the 150-200ms black flash
    # below ends, get_display_frame() keeps returning None while the new
    # channel buffers, and _blit_last_video_frame() bridges with the stale
    # frame that's still cached from BEFORE the switch — which reads as the
    # picture freezing instead of staying black. Now it stays black for the
    # whole wait, however long it actually takes.
    _clear_last_video_frame()

    _transition_style_now = db.config.get("transition_type", "black") if db is not None else "black"
    if is_boot:
        # BOOT: clean tune-in, no static. The boot sequence's initial tune is
        # NOT a channel change, so it must never show the channel-change static
        # burst -- static is only for user channel changes. We don't set the
        # shield here, BUT the first-frame buffering bridge in the render loop
        # (case A) would still draw static while VLC decodes its first frame if
        # transition_type == "static". This flag tells that bridge to use the
        # plain gray fill instead, so boot comes up clean and goes straight to
        # video. It's cleared the instant the first real frame renders, and by
        # any subsequent real (non-boot) channel change below.
        app_state["_boot_suppress_static"] = True
    elif internal_advance:
        # Automatic same-channel advance (previous show just ended on its
        # own) -- not a real channel change, so no black/static transition
        # flash. channel_switch_black_until is deliberately left untouched here
        # rather than reset to 0, in case a genuine tune's shield window somehow
        # still had time left on it (shouldn't happen in practice, but this is
        # strictly safer than cutting a real transition short).
        pass
    elif _transition_style_now == "static":
        # Real user channel change -- clear any lingering boot-suppression flag
        # so static works normally from here on (boot is a one-time event).
        app_state["_boot_suppress_static"] = False
        # Static needs real screen time to read as animated noise instead of
        # a single frozen-looking frame, AND enough time for the looped
        # static_audio to actually be audible -- the black flash's 150-200ms
        # window is too short for either. A full second matches an old TV's
        # channel-change static burst.
        app_state["channel_switch_black_until"] = pygame.time.get_ticks() + 1000
    elif is_surfing:
        app_state["_boot_suppress_static"] = False
        app_state["channel_switch_black_until"] = pygame.time.get_ticks() + 200
    else:
        # Direct switch (guide/number) — short blackout clears the old VLC
        # frame so it never flashes even one rendered frame before the new one.
        app_state["_boot_suppress_static"] = False
        app_state["channel_switch_black_until"] = pygame.time.get_ticks() + 150

    # NOTE: the "static" transition_type used to fire a single non-looping
    # audio pop, re-armed here every switch via _transition_static_fired.
    # It now loops for the whole shield window via _render_transition_static
    # and is explicitly stopped by _stop_transition_static() the moment that
    # window ends (see the transition shield gate + Channel 03 branch), so
    # there's nothing left to re-arm here.

    if internal_advance:
        # Same channel, next scheduled show -- nothing "changed" from the
        # viewer's perspective, so no "CH XX" banner and no visualizer-OSD
        # re-arm. Leave osd_channel_pending/osd_channel_timer exactly as
        # they were (almost certainly already expired from whatever the
        # last REAL tune was).
        pass
    elif t_state["mode"] in ("Video", "Commercial"):
        # Video channels take ~1-1.5s for VLC to actually start delivering
        # decoded frames after play_file_segmented() — if the OSD timer starts
        # counting down right now (channel-change press time), most of its
        # window burns silently while the screen is still black, and the "CH
        # XX" banner looks like it's already fading out the moment the picture
        # appears. Instead, arm it for real once the first real frame renders
        # (see the Video/Commercial render branch), so the full 2.5s is spent
        # actually visible on screen, matching a real cable box.
        app_state["osd_channel_pending"] = True
    else:
        # Static/visualizer/guide channels render instantly — no meaningful
        # buffering gap, so there's nothing to wait for.
        app_state["osd_channel_pending"] = False
        app_state["osd_channel_timer"] = pygame.time.get_ticks() + 2500
    if not internal_advance:
        app_state["visualizer_osd_timer"] = pygame.time.get_ticks() + 10000
    
    if new_ch == "04":
        app_state["selected_guide_time_idx"] = 0
        app_state["guide_col_pos"] = 0
        app_state["guide_inactivity_timer"] = pygame.time.get_ticks() + 60000

        # Emulate the Anti-Burn-In shield cleanup to stop manual swap freezes
        app_state["loaded_preview_path_cache"] = ""
        # Fresh guide session -- don't let a stale per-channel preview frame
        # from an earlier visit (possibly minutes/hours old) stand in for
        # real content during this session's VLC startup gap. See
        # _clear_guide_channel_frame_cache's docstring. Only on genuine
        # re-entry (old_ch != "04") so mid-session re-triggers never wipe
        # the smooth-scroll cache this same session already built up.
        if old_ch != "04":
            _clear_guide_channel_frame_cache()

        # Always restore the guide cursor to wherever the user last left it,
        # regardless of which channel they were just watching.
        #
        # Previously, coming back from a guide-row channel (>= 05) re-seeded the
        # cursor onto THAT channel, overriding the user's last position — e.g.
        # returning from ch05 forced the preview back to ch05, while returning
        # from ch03 (not a guide row) correctly kept the last position. Now both
        # paths behave the same: the cursor stays where it was left.
        #
        # GUARD: ch03 (game) and any channel < 05 are NOT browseable guide rows
        # (allowed_guide is built from range(5, 45)). If the saved highlight is
        # one of those (or unset), fall back to '05'. On first boot the default
        # is already "05".
        if old_ch and old_ch != "04":
            _existing = str(app_state.get("highlighted_guide_channel", "05")).zfill(2)
            try:
                if int(_existing) < 5:
                    _existing = "05"
            except (ValueError, TypeError):
                _existing = "05"
            app_state["highlighted_guide_channel"] = _existing
            app_state["selected_guide_channel"]    = int(_existing)
            # Recompute row offset so the restored highlight is guaranteed to
            # be visible in whichever row window is actually on screen (8
            # rows on 16:9, 5 rows on 4:3 -- see get_guide_visible_rows).
            #
            # BUG FIX: this used to only reset guide_row_offset when
            # _existing fell back to "05". But leaving the guide (see the
            # "else" branch below) unconditionally zeroes guide_row_offset
            # while leaving highlighted_guide_channel untouched. So coming
            # back to a remembered row other than 05 paired a stale
            # offset (0) with a highlight that could be scrolled off-screen
            # -- invisible on 16:9's 8-row window until you'd scrolled deep,
            # but breaking almost immediately on 4:3's narrower 5-row
            # window. Recalculating here for every restored channel (not
            # just ch05) keeps the highlight in view regardless of aspect
            # ratio.
            _allowed_guide_restore = [
                str(i).zfill(2) for i in range(5, 45)
                if db.channels_db.get(str(i).zfill(2), {}).get("active", True)
            ] if db is not None else []
            try:
                _restore_pos = _allowed_guide_restore.index(_existing)
            except ValueError:
                _restore_pos = 0
            _guide_rows_restore = get_guide_visible_rows(db)
            _cur_offset = app_state.get("guide_row_offset", 0)
            if _restore_pos < _cur_offset or _restore_pos >= _cur_offset + _guide_rows_restore:
                app_state["guide_row_offset"] = max(0, _restore_pos - (_guide_rows_restore - 1))
        
        guide_target = calculate_slotted_playback_state(str(app_state.get("highlighted_guide_channel", 5)).zfill(2))
        if pygame.mixer.music.get_busy() and guide_target.get("file") != current_playing_track:
            pygame.mixer.music.stop()
        app_state["guide_preview_music_track"] = ""
    else:
        app_state["guide_col_pos"] = 0
        app_state["guide_row_offset"] = 0
        
    # === VLC HANDLING WITH SAFE GUARDS ===
    # NEW TRANSITION ARCHITECTURE: this whole block used to call play()
    # immediately (muted, with the real volume restored once the shield
    # expired). Now it doesn't call play() at all -- it packages up a
    # closure that does, and that closure only ever runs from
    # _run_pending_channel_activation() once the shield is already gone,
    # so it can just play at the real volume directly. See
    # _run_pending_channel_activation()'s docstring for the full picture.
    _content_activate = None
    if t_state["mode"] in ("Video", "Commercial"):
        initialize_vlc_on_demand()

        if vlc_engine is None:
            print(f"[VLC WAIT] Engine still initializing for channel {new_ch}...")
            app_state["is_playing_video"] = False
        else:
            # DOUBLE-FIRE FIX: a Video/Commercial switch doesn't actually start
            # playing right here -- it queues _content_activate below, which only
            # runs later (from _run_pending_channel_activation, once the transition
            # shield has cleared). Until that closure runs, is_playing_video stays
            # False. The "not yet started" branch of the end-of-show watchdog
            # (main loop) treats is_playing_video==False as "this channel needs a
            # nudge" and re-fires change_channel(internal_advance=True) once its
            # own cooldown (last_vlc_retry_at) has elapsed -- but that cooldown
            # defaults to 0 when unset, so on the very first frame after ANY
            # channel switch (boot included) it reads as already-expired and the
            # watchdog immediately re-fires a second change_channel() call, often
            # within 20-30ms, before the first call's queued activation ever got a
            # chance to run. Both calls happen to suppress the visible transition
            # flash (is_boot/internal_advance both do), so this was previously
            # invisible as "static" -- but it's still two back-to-back VLC stop()/
            # play() cycles for nothing, which can read as its own stutter/flicker.
            # Stamping last_vlc_retry_at HERE means the watchdog's cooldown now
            # actually starts from the moment this switch queued its play, giving
            # _content_activate a real chance to run first.
            app_state["last_vlc_retry_at"] = pygame.time.get_ticks()

            def _content_activate(new_ch=new_ch, t_state=dict(t_state), force_reload=force_reload):
                try:
                    import vlc
                    current_media = vlc_engine.player.get_media()
                    current_path = ""
                    if current_media:
                        from urllib.parse import unquote
                        current_path = os.path.normpath(unquote(current_media.get_mrl().replace("file:///", ""))).lower()
                    target_path = os.path.normpath(t_state["file"]).lower()

                    # force_reload (used by _apply_scheduling_mode_change's boundary
                    # retune) means the caller has already decided this is a genuine
                    # re-tune and needs the exact new seek position applied — even if
                    # the target file HAPPENS to be the same path already loaded (e.g.
                    # the newly-picked episode coincides with what was already airing).
                    # Without this, the path-match branch below would silently no-op
                    # (VLC keeps playing from its old, un-seeked position while the
                    # anchor we write claims the new position), which looks exactly
                    # like a frozen/stuck picture that needs a manual channel refresh.
                    _should_play = force_reload or current_path != target_path or (
                        vlc_engine.player.get_state() not in [vlc.State.Playing, vlc.State.Opening])

                    _played_ok = True
                    if _should_play:
                        if vlc_engine.player.is_playing():
                            vlc_engine.stop()
                        vlc_engine.set_volume(db.config["global_volume"], db.config.get("is_muted", False))
                        # AUDIO-BLEED FIX: VLC begins emitting AUDIO within a few
                        # ms of play_file_segmented(), but its first VIDEO frame
                        # takes ~1-1.5s to decode -- and that gap is covered by
                        # the buffering static (the "static"-style bridge in the
                        # Video render branch below). Starting at full volume here
                        # is exactly what let the new channel's audio be heard
                        # UNDER the static before the picture arrived (the 05<->06
                        # bleed). An earlier mute-before-play guard was removed
                        # because it un-muted on the FIXED shield timer, which
                        # expires before the frame actually lands -- so it didn't
                        # cover the buffering window. Instead, hard-mute the player
                        # now and defer the un-mute to the exact moment the first
                        # real frame arrives (see _video_pending_unmute in the
                        # Video render branch), so audio and picture come in
                        # together and only the static's own noise plays while
                        # waiting.
                        _mute_now = db.config.get("is_muted", False)
                        print(f"[MUTE TRACE] pre-play hard-mute for ch{new_ch}: is_muted={_mute_now}, "
                              f"was_playing_before={vlc_engine.player.is_playing() if hasattr(vlc_engine.player,'is_playing') else '?'}")
                        try: vlc_engine.player.audio_set_mute(True)
                        except Exception: pass
                        app_state["_video_pending_unmute"] = True
                        _played_ok = vlc_engine.play_file_segmented(t_state["file"], t_state["seek"], gain=_lookup_file_gain(new_ch, t_state["file"]))
                        app_state["last_video_play_started_at"] = pygame.time.get_ticks()

                        # RACE GUARD: play_file_segmented() just called stop() +
                        # set_media() + play() on this player. If the previous
                        # media had no audio track at all (e.g. this channel was
                        # showing Static/no-schedule a moment ago, so this
                        # vlc_engine never had a live audio session), starting
                        # a brand-new track can spin up a fresh WASAPI/audio
                        # session that does NOT inherit the mute flag set on
                        # the player a few lines above -- the mute call landed
                        # on the OLD (trackless) session, not the one that just
                        # got created for the new file. Re-asserting mute here,
                        # immediately after play() has committed the new media,
                        # closes that gap so the buffering window can't bleed
                        # audio even on a fresh/first-ever audio session for
                        # this engine. Safe/idempotent if the flag already held.
                        try:
                            vlc_engine.player.audio_set_mute(True)
                            _post_mute = vlc_engine.player.audio_get_mute()
                        except Exception as _pm_err:
                            _post_mute = f"error: {_pm_err}"
                        print(f"[MUTE TRACE] post-play re-assert hard-mute for ch{new_ch}: "
                              f"played_ok={_played_ok}, audio_get_mute()={_post_mute}")

                        # BLACK-SCREEN FIX: the scheduled file existed at block-build
                        # time but refused to actually load now (moved/corrupted/
                        # network hiccup). Instead of silently marking
                        # is_playing_video True over a dead player (the old bug) or
                        # just retrying the SAME broken file in a loop, immediately
                        # substitute the next episode of the same show (honoring
                        # sequential/random episode_order_mode) and keep trying
                        # every remaining episode of that show until one actually
                        # plays. Each failure is permanently removed from future
                        # scheduling so it can't cause this again later.
                        _ch_key_for_retry = str(new_ch).zfill(2)
                        _retry_guard = 0
                        while not _played_ok and db is not None and _retry_guard < 200:
                            _retry_guard += 1
                            _ch_info_live = db.channels_db.get(_ch_key_for_retry, {})
                            _sub = _resolve_broken_episode(_ch_info_live, t_state["file"], t_state.get("block", "Morning"))
                            if _sub is None:
                                log.warning(
                                    "change_channel: no playable substitute left for channel %s "
                                    "(file %s) -- leaving black-screen protection engaged.",
                                    new_ch, t_state["file"])
                                break
                            print(f"[BLACK SCREEN FIX] '{t_state['file']}' failed to play -- "
                                  f"substituting '{_sub['file']}' and rescheduling future slots.")
                            t_state["file"] = _sub["file"]
                            t_state["seek"] = _sub["seek"]
                            t_state["duration"] = _sub["duration"]
                            _played_ok = vlc_engine.play_file_segmented(t_state["file"], t_state["seek"], gain=_lookup_file_gain(new_ch, t_state["file"]))
                            app_state["last_video_play_started_at"] = pygame.time.get_ticks()

                    if _played_ok:
                        app_state["is_playing_video"] = True
                        print(f"[CHANNEL] Switched to video channel {new_ch}")

                        # --- PLAYBACK ANCHOR: record exact file + wall-clock start so that
                        #     calculate_slotted_playback_state() returns this same state until
                        #     the file naturally ends, even if new media is added mid-show.
                        if db is not None and t_state.get("file"):
                            _now_a = datetime.datetime.now()
                            _wall_a = _now_a.hour * 3600 + _now_a.minute * 60 + _now_a.second
                            db.channels_db[str(new_ch).zfill(2)]["playback_anchor"] = {
                                "file":           t_state["file"],
                                "wall_start":     _wall_a,
                                "seek_offset":    t_state.get("seek", 0),
                                # slot_start_seek: the file position at the START of
                                # this timeline slot (0 for whole shows/first segments,
                                # mid_point for split-show second halves, etc.).
                                # Used by the anchor remaining formula to correctly
                                # compute how much of the slot is left regardless of
                                # where within the slot the viewer tuned in.
                                "slot_start_seek": t_state.get("seek_slot_offset", 0),
                                "duration":        t_state.get("duration", 1800),
                                "mode":            t_state.get("mode", "Video"),
                                "block":           t_state.get("block", "Morning"),
                            }
                            try:
                                # ASYNC WRITE: was db.save_settings() -- a synchronous
                                # full JSON serialize + write of the ENTIRE config
                                # (every channel's schedules/playback logs/probe
                                # cache, ~5MB+ on a well-populated library) on the
                                # MAIN thread, on every single channel change. That
                                # blocked pygame's render/input loop for the full
                                # write, and could additionally block behind
                                # _probe_upcoming_media's own save_settings() call
                                # holding the same _save_lock on its background
                                # thread -- together this is what made channel
                                # buttons/typed channel numbers intermittently stop
                                # responding (worse the longer the app ran and the
                                # bigger media_probe_cache grew). save_settings_async()
                                # snapshots the data here (cheap) and does the actual
                                # write on its own daemon thread instead.
                                db.save_settings_async()
                            except Exception as e:
                                log.warning("change_channel: failed to persist new playback_anchor for ch%s: %s", new_ch, e)
                    else:
                        # Nothing playable was found anywhere in this show's rotation
                        # -- keep is_playing_video False so the existing black-screen
                        # crash protection stays on screen (as designed) instead of
                        # falsely reporting a live picture.
                        app_state["is_playing_video"] = False
                except Exception as e:
                    print(f"[VLC ERROR] Failed to play video on channel {new_ch}: {e}")
                    log.warning("change_channel: exception while starting video on ch%s: %s", new_ch, e)
                    app_state["is_playing_video"] = False
    else:
        if t_state["mode"] == "Visualizer":
            track_file_path = t_state.get("file", "")
            track_seek_start = t_state.get("seek", 0)
            track_total_len = t_state.get("duration", 210)
            
            print(f"[DEBUG TUNER] Entering Visualizer mode setup matrix blocks...")
            if track_file_path:
                app_state["active_music_track_file"] = track_file_path
                print(f"[DEBUG TUNER SUCCESS] Assigned text string to cache register: '{app_state['active_music_track_file']}'")

                # --- PLAYBACK ANCHOR (Visualizer/music) ---
                # BUG FIX: this used to only be written for Video/Commercial
                # channels, so calculate_slotted_playback_state() always
                # recomputed music-channel position fresh via current_seconds
                # % total_playlist_seconds -- using whatever durations are in
                # the DB AT THAT INSTANT. Since the background prober keeps
                # correcting unprobed (-1/default-210s) durations to their
                # real values throughout the session, total_playlist_seconds
                # kept shifting under a channel that hadn't even been
                # switched away from, so the "currently playing" track/seek
                # could jump unpredictably -- reported as songs skipping and,
                # since each jump looked like a new scheduled track, a flood
                # of new random visualizer styles. Anchoring here pins the
                # exact file+wall-clock start, same as video, so it plays out
                # to its real end regardless of duration corrections that
                # land on OTHER tracks in the playlist while this one plays.
                if db is not None:
                    _now_va = datetime.datetime.now()
                    _wall_va = _now_va.hour * 3600 + _now_va.minute * 60 + _now_va.second
                    db.channels_db[str(new_ch).zfill(2)]["playback_anchor"] = {
                        "file":        track_file_path,
                        "wall_start":  _wall_va,
                        "seek_offset": track_seek_start,
                        "duration":    track_total_len,
                        "mode":        "Visualizer",
                        "block":       t_state.get("block", "Morning"),
                    }
                    try:
                        # ASYNC WRITE: see matching note on the video-channel
                        # branch above -- same synchronous full-config write on
                        # the main thread, same fix.
                        db.save_settings_async()
                    except Exception as e:
                        log.warning("change_channel: failed to persist new visualizer playback_anchor for ch%s: %s", new_ch, e)
            else:
                print(f"[DEBUG TUNER WARNING] Target track file path variable arrived blank or empty!")
            
            if track_file_path and (track_file_path != current_playing_track or not pygame.mixer.music.get_busy()):
                print(f"[DEBUG TUNER] Audio track change required. Initializing fresh music stream playhead load sequence.")
                if pygame.mixer.music.get_busy(): pygame.mixer.music.stop()
                _load_ok = False
                try:
                    pygame.mixer.music.load(track_file_path)
                    _load_ok = True
                except Exception as load_err:
                    print(f"[VISUALIZER CHANNEL] Music load failed for {track_file_path}: {load_err}")
                if _load_ok:
                    # LOUDNESS NORMALIZATION: same lookup used everywhere else a music
                    # track loads, keyed to the channel being tuned into.
                    app_state["active_music_gain"] = _lookup_file_gain(new_ch, track_file_path)
                    safe_seek_pos = max(0.0, min(float(track_seek_start), float(track_total_len - 10)))
                    _real_music_vol = _effective_audio_gain(min_db=STACKED_GAIN_MIN_DB) * MUSIC_GAIN * app_state["active_music_gain"]

                    # NEW TRANSITION ARCHITECTURE: load() above just decodes
                    # the header, it doesn't produce sound -- so it's safe to
                    # run immediately. The actual play() call is what starts
                    # audio, so it's deferred to activation, at the real
                    # volume, same reasoning as the VLC path above.
                    def _content_activate(safe_seek_pos=safe_seek_pos, _real_music_vol=_real_music_vol):
                        pygame.mixer.music.set_volume(_real_music_vol)
                        try:
                            pygame.mixer.music.play(start=safe_seek_pos)
                        except Exception as play_err:
                            print(f"[VISUALIZER CHANNEL] play(start={safe_seek_pos}) failed: {play_err}. Retrying at 0.0s.")
                            try:
                                pygame.mixer.music.play(start=0.0)
                            except Exception as retry_err:
                                print(f"[VISUALIZER CHANNEL] Fallback play also failed: {retry_err}")

                # Protect text string from being blanked out by the audio mixer load wipe
                app_state["active_music_track_file"] = track_file_path
                app_state["vis_card_timer"] = pygame.time.get_ticks() + 15000
            else:
                print(f"[DEBUG TUNER GATE] Audio track matches active playhead state. Maintaining continuous stream.")
                app_state["vis_card_timer"] = pygame.time.get_ticks() + 15000
        else:
            # Tuning into a non-video, non-Visualizer ("Static") channel like
            # the TV Guide: stop the main engine so the previous channel's
            # audio/video actually stops. The DVD is NOT affected here -- it
            # lives on its own dvd_engine, which this never touches, so no
            # DVD-preservation guard is needed anymore (the old shared-engine
            # ch03 guards were removed with the two-engine rebuild). This is a
            # SEPARATE stop() call from the "IMMEDIATE CUTOFF" one earlier in
            # this function.
            if vlc_engine is not None:
                try:
                    if vlc_engine.player.is_playing():
                        vlc_engine.stop()
                        pygame.time.wait(100)
                except Exception as e: print(f"[CHANNEL] vlc stop on switch failed: {e}")
        app_state["is_playing_video"] = False
        print(f"[CHANNEL] Switched to non-video channel {new_ch}")
        
    # === CHANNEL 03 DVD RESUME ===
    # If returning to ch03 with a movie in progress, restart VLC at the saved position.
    # IMPORTANT: Do NOT call vlc_engine.player.pause() here immediately after
    # play_file_segmented(). VLC is asynchronous — calling pause() before the
    # decoder has started just leaves it in Stopped/Opening state, which means
    # get_display_frame() returns None every frame and the screen goes black even
    # though dvd_paused_frame has the correct cached image.
    # Instead, set _dvd_pending_pause=True and let _render_dvd_player defer the
    # actual pause() call to the first frame where VLC is in Playing state.
    #
    # NEW TRANSITION ARCHITECTURE: same as everywhere else, this only queues
    # a closure now -- it doesn't call play() itself. It used to bypass the
    # game_deck mute handling entirely (a real, separate bug fixed alongside
    # this rebuild), routing through the exact same activation checkpoint as
    # ch03's own unfreeze/menu-music above.
    _dvd_resume_activate = None
    if new_ch == "03" and game_deck is not None and getattr(game_deck, 'dvd_playback_active', False) and getattr(game_deck, 'dvd_file_path', ''):
        initialize_vlc_on_demand()

        # DVD BACKGROUND-KEEP-ALIVE RESUME. The DVD lives on its own dedicated
        # dvd_engine, which nothing else in the app ever touches -- so the disc
        # we muted on leave is guaranteed to still be running exactly where the
        # user left it: the root menu, the same highlighted submenu option, or
        # the movie itself (which kept advancing in real time the whole time we
        # were away). Resuming is therefore nothing more than an unmute. There
        # is no stop, no dvdnav reopen, no menu-nav replay, no title re-entry
        # and no position reconstruction -- all of which only ever existed
        # because the DVD used to share the single main vlc_engine and could be
        # reclaimed by another video channel.
        if dvd_engine is not None:
            def _dvd_resume_activate():
                try:
                    dvd_engine.player.audio_set_mute(False)
                    dvd_engine.set_volume(db.config["global_volume"] if db else 70,
                                          db.config.get("is_muted", False) if db else False)
                    game_deck.dvd_iso_backgrounded = False
                    game_deck.dvd_exit_confirm = False
                    game_deck.dvd_awaiting_resume = False
                    game_deck.dvd_resume_pending = False
                    print("[DVD] Background resume: dvd_engine unmuted in place, no reopen needed.")
                except Exception as _ue:
                    print(f"[DVD] Background resume unmute failed: {_ue}")

    # === SINGLE PENDING ACTIVATION ===
    # Bundle whatever this switch needs to start (ch03 unfreeze/menu-music,
    # video/visualizer playback, DVD resume -- any/none may apply) into one
    # callable. _run_pending_channel_activation() fires it exactly once,
    # only once the shield has actually finished covering the screen.
    def _activate_pending():
        if _ch03_activate is not None:
            _ch03_activate()
        if _content_activate is not None:
            _content_activate()
        if _dvd_resume_activate is not None:
            _dvd_resume_activate()
    app_state["_pending_channel_activation"] = _activate_pending

    print(f"[DEBUG TUNER] Final app_state['active_music_track_file'] text payload leaving function: '{app_state.get('active_music_track_file')}'")
    print(f"[DEBUG TUNER] --- END CHANNEL SWAP LOG ---\n")

    pygame.event.pump()

# ==============================================================================
# PART 7 OF 28: RUNTIME LOOP INTERFACE PIPELINE & EVENT ROUTERS (DEBUG MODE)
# ==============================================================================

# ==============================================================================
# HELPER: Count active content channels (05-44) to enforce minimum-one rule
# ==============================================================================

def _count_active_content_channels(db):
    """Return how many channels in 05-44 range are currently active."""
    if db is None: return 1
    count = 0
    for n in range(5, 45):
        ch_str = str(n).zfill(2)
        ch = db.channels_db.get(ch_str, {})
        if ch.get('active', ch_str == '05'):
            count += 1
    return count

# ==============================================================================
# HELPER: Count active game consoles to enforce minimum-one rule (mirrors
# _count_active_content_channels above). DVD is the console-neutral, non-BIOS
# default — same role channel 05 plays for the Channels tab — so it defaults
# to active=True when no saved value exists; every other console defaults
# to False.
# ==============================================================================

def _count_active_consoles(db):
    """Return how many consoles in CONSOLE_ORDER are currently enabled."""
    if db is None: return 1
    from game_deck import CONSOLE_ORDER
    enabled_map = db.config.get("consoles_enabled", {})
    count = 0
    for c in CONSOLE_ORDER:
        default_on = (c == "DVD")
        if enabled_map.get(c, default_on):
            count += 1
    return count

# ==============================================================================
# HELPER: When a channel is turned OFF while you're watching it, surf UP to the
# next active channel (increasing direction, wrapping around). Applies to any
# channel: content channels 05-44, plus the Game (03) and TV Guide (04) channels.
# ==============================================================================

def _surf_up_from_disabled(off_ch):
    """If `off_ch` is the channel currently being watched, hop to the next active
    channel in the upward direction. Call this AFTER the channel's enabled flag
    has already been flipped off so it is naturally excluded from the pool."""
    off_str = str(off_ch).zfill(2)
    if str(app_state.get("current_channel", "05")).zfill(2) != off_str:
        return  # Not watching the channel that was turned off — nothing to do.

    # Build the live ordered pool of active channels (03, 04, then 05-44 ascending)
    allowed = []
    if db is not None and db.config.get("game_channel_enabled", False):
        allowed.append("03")
    if db is not None and db.config.get("tv_guide_enabled", True):
        allowed.append("04")
    if db is not None:
        for i in range(5, 45):
            ch_key = str(i).zfill(2)
            if db.channels_db.get(ch_key, {}).get("active", False):
                allowed.append(ch_key)

    if not allowed:
        return  # No active channels at all — leave current channel as-is.

    # First active channel numerically above the one just turned off; else wrap
    # around to the lowest active channel.
    target = next((ch for ch in allowed if int(ch) > int(off_str)), allowed[0])
    print(f"[CHANNEL] {off_str} turned off while watching — surfing up to {target}.")
    change_channel(target, is_surfing=True)

# ==============================================================================
# HELPER: TV GUIDE ONE-KEYPRESS SHOW-TO-SHOW NAVIGATION (D/A)
# ==============================================================================
# Previously D/A moved the highlighted cell exactly one half-hour column at a
# time, so a 2-hour show (4 columns wide) took 4 presses to clear instead of
# one. This walks straight to the next/previous SHOW'S start column by
# consulting guide_current_row_blocks -- the actual show-start columns for
# the highlighted row, computed fresh every time the guide's own (cached)
# renderer runs. That renderer is the guide's single source of truth for
# what's airing (same build_block_timeline/build_show_rotation data the
# playback engine uses), so reusing it here means this navigation can never
# drift from what the guide is actually displaying.
def _guide_jump_col(direction):
    """direction: +1 for D (forward/right), -1 for A (back/left).

    Navigation model (edge-scroll / pinned-cursor):
      1. If there is a show boundary inside the current visible window in
         the requested direction, jump the cursor straight to that column.
      2. If the cursor is not yet at the edge of the visible window, move
         it to that edge (right edge for D, col 0 for A) — one press brings
         you to the boundary of the guide's visible area.
      3. If the cursor is already pinned at the edge, scroll the timeline
         one half-hour slot in that direction so new content slides in,
         while the cursor stays pinned at the edge.  Each additional press
         reveals one more half-hour column — smooth and predictable, never
         snapping back to the opposite end of the window.
    """
    curr_col = app_state.get("guide_col_pos", 0)
    _guide_last_col = get_guide_num_cols(db) - 1
    blocks = sorted(set(app_state.get("guide_current_row_blocks", []) or [0]))

    if direction > 0:
        # --- Step 1: jump to the nearest visible show boundary to the right ---
        target = next((c for c in blocks if c > curr_col), None)
        if target is not None:
            app_state["guide_col_pos"] = min(target, _guide_last_col)
            return
        # --- Step 2: no boundary ahead — move cursor to the right edge ---
        if curr_col < _guide_last_col:
            app_state["guide_col_pos"] = _guide_last_col
            return
        # --- Step 3: already pinned at right edge — scroll timeline forward ---
        app_state["selected_guide_time_idx"] = (
            app_state.get("selected_guide_time_idx", 0) + 1) % 48
        render_tv_guide(screen, app_state, db, active_theme)
        app_state["guide_col_pos"] = _guide_last_col

    else:  # direction < 0
        # --- Step 1: jump to the nearest visible show boundary to the left ---
        target = next((c for c in reversed(blocks) if c < curr_col), None)
        if target is not None:
            app_state["guide_col_pos"] = max(target, 0)
            return
        # --- Step 2: no boundary behind — move cursor to the left edge ---
        if curr_col > 0:
            app_state["guide_col_pos"] = 0
            return
        # --- Step 3: already pinned at left edge — scroll timeline backward ---
        app_state["selected_guide_time_idx"] = (
            app_state.get("selected_guide_time_idx", 0) - 1) % 48
        render_tv_guide(screen, app_state, db, active_theme)
        app_state["guide_col_pos"] = 0

# ==============================================================================
# CRT POWER ANIMATION HELPERS
# ==============================================================================

def _standby_channel_with_rules():
    """Return the channel to restore on wake. Boot rules: CH03 (games) and
    CH04 (guide) default back to CH05 — they need deliberate navigation,
    not an accidental resume on a game mid-frame or the guide mid-scroll."""
    ch = app_state.get("current_channel", "05")
    if ch in ("03", "04") or not ch:
        return "05"
    return ch


def _trigger_crt_off(action):
    """Queue a CRT power-off animation for a real OS shutdown via the
    in-menu 'Turn Off Computer' button. The actual OS call happens only
    after the animation finishes."""
    app_state["pre_standby_channel"] = _standby_channel_with_rules()
    app_state["crt_anim"] = {
        "phase":    "off",
        "start_ms": pygame.time.get_ticks(),
        "action":   action,
        "snapshot": None,    # captured on the first render frame
        "total_ms": 900,
    }
    app_state["show_menu"] = False   # hide menu so animation plays over the channel


while True:
    if 'db' in globals() and db is not None:
        # FIXED: this used to be a raw db.themes[db.config["current_theme"]]
        # lookup. db.config starts life as an EMPTY dict (see Database.__init__
        # in media.py) and is only filled in by load_settings_async, which runs
        # on its own background thread -- there's no guarantee that thread has
        # finished before this loop's very first iteration. Confirmed via a
        # real crash log: on a heavier boot (large channel just had 500+ files
        # added, background duration/loudness probes still catching up from a
        # previous session), the main loop got there first and hit
        # KeyError: 'current_theme' before the config had a chance to load,
        # which crashed the whole app on startup. Falling back to the same
        # "DefaultBlueGreen" theme the not-ready branch below already uses
        # means a slow config load now just shows the default theme for a
        # frame or two instead of crashing -- it self-corrects the moment
        # db.config actually has the key.
        _theme_key = db.config.get("current_theme", "DefaultBlueGreen")
        active_theme = db.themes.get(_theme_key) or db.themes.get("DefaultBlueGreen") \
            or {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)}
    else:
        active_theme = {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)}
        
    events = pygame.event.get()
    now_ticks = pygame.time.get_ticks()

    # --- NUMERIC TIMEOUT FALLBACK TUNER ENGINE ---
    if app_state.get("digit_buffer", "") and now_ticks >= app_state.get("digit_timer", 0):
        final_ch = app_state["digit_buffer"].zfill(2)
        app_state["digit_buffer"] = ""
        app_state["digit_timer"] = 0
        
        valid_set = []
        if db is not None:
            if db.config.get("game_channel_enabled", False): valid_set.append("03")
            if db.config.get("tv_guide_enabled", True): valid_set.append("04")
            for i in range(5, 45):
                ch_key_str = str(i).zfill(2)
                if db.channels_db.get(ch_key_str, {}).get("active", False):
                    valid_set.append(ch_key_str)
        else:
            valid_set = ["04", "05", "06", "07"]
            
        if final_ch in valid_set: 
            print(f"[TUNER] Timeout reached. Tuning single-digit fallback: {final_ch}")
            change_channel(final_ch, is_surfing=True)

    if app_state["current_channel"] == "04":
        if (now_ticks >= app_state.get("guide_inactivity_timer", 0)
                and not app_state.get("file_explorer_active", False)
                and not app_state.get("show_menu", False)):
            # Use highlighted_guide_channel — that's what's showing in the preview window.
            # selected_guide_channel is an integer that defaults to 5 and can lag behind,
            # which is why the timeout was always jumping to channel 05.
            # Guards: skip if the file explorer is open (user is actively browsing a
            # sub-menu) or if the main menu is open (its own burn-in handles that path).
            target_burn_in_safe_ch = str(app_state.get("highlighted_guide_channel", "05")).zfill(2)
            print(f"[ANTI-BURN-IN SHIELD] Inactivity limit reached. Forcing fullscreen re-entry to Channel: {target_burn_in_safe_ch}")
            app_state["loaded_preview_path_cache"] = ""
            change_channel(target_burn_in_safe_ch)
            continue

    # --- ASYNCHRONOUS DEFERRED METADATA LOADER THREAD LAYER ---
    try:
        while not explorer_queue.empty():
            files_list = explorer_queue.get_nowait()
            if files_list and db is not None:
                target_ch = str(app_state["selected_channel_row"]).zfill(2)
                ch_info_cache = db.channels_db.get(target_ch, {})
                
                if "schedules" not in db.channels_db[target_ch]:
                    db.channels_db[target_ch]["schedules"] = {}
                
                if ch_info_cache.get("is_visualizer", False):
                    active_b = "Music Tracks"
                else:
                    sub_r_cached = app_state.get("sub_menu_row_index", 1)
                    sub_c_cached = app_state.get("sub_menu_col_index", 0)
                    if ch_info_cache.get("scheduling_mode", "random_slots") == "marathon":
                        # Marathon uses one dedicated 24h folder.
                        active_b = "Marathon"
                    elif (ch_info_cache.get("scheduling_mode", "random_slots") != "marathon"
                          and ch_info_cache.get("block_mode", "full_day") == "full_day"):
                        # BLOCK LENGTH set to 24-HOUR (FULL DAY) also collapses the
                        # 3 slots into one dedicated 24h folder, distinct from Marathon's.
                        active_b = "Full Day"
                    else:
                        # Folder slots share ONE row across columns. That row is 4
                        # for marathon channels but 6 for the block modes (random/
                        # one_slot/two_slots), since those have the extra Episode
                        # Order + Block Length rows above the folder slots — see
                        # _folder_row in ui.py's render_settings_overlay_menu for
                        # the source of truth. Hardcoding row 4 here caused every
                        # add to silently fall through to the "Morning" default
                        # in block modes.
                        _folder_row_cached = 6
                        folder_map = {
                            (_folder_row_cached, 0): "Morning",
                            (_folder_row_cached, 1): "Evening",
                            (_folder_row_cached, 2): "Night",
                        }
                        active_b = folder_map.get((sub_r_cached, sub_c_cached), "Morning")
                
                # APPEND to existing schedule list (do not wipe on second-wave additions)
                existing_entries = db.channels_db[target_ch]["schedules"].get(active_b, [])
                deduped_files = _dedupe_incoming_files(existing_entries, files_list)
                _dupe_count = len(files_list) - len(deduped_files)
                print(f"[FILE ADD] Appending {len(deduped_files)} file(s) to {active_b} (already had {len(existing_entries)})"
                      + (f" -- skipped {_dupe_count} exact-name duplicate(s)" if _dupe_count else ""))
                for file_path in deduped_files:
                    # -1 = "not yet probed". load_settings_async re-queues these on boot
                    # so closing mid-import doesn't strand files at the fallback forever.
                    #
                    # FIXED: this used to special-case visualizer/music entries to
                    # 210 instead of -1. That's a real duration value as far as the
                    # -1-sentinel check is concerned, so if the app was closed (or
                    # the background probe thread just hadn't gotten to a file yet)
                    # before _deferred_metadata_probing_worker patched in the real
                    # length, that track was PERMANENTLY stuck reporting 210s to
                    # calculate_slotted_playback_state's wall-clock scheduler,
                    # regardless of its actual length — the boot-time resume-probe
                    # only re-queues entries still sitting at -1, so a 210 never got
                    # a second chance. That's what causes songs to get cut off /
                    # skip early (real song longer than the assumed 210s) or restart
                    # from 0 (real song shorter than 210s): the schedule and the
                    # actual audio playback drift out of sync. Always start at -1
                    # here; _safe_dur() already supplies 210 as the read-time
                    # placeholder for visualizer entries until they're genuinely
                    # probed, so nothing regresses for the still-unprobed case.
                    fallback_dur = -1
                    new_entry = {"path": os.path.normpath(file_path), "duration": fallback_dur}
                    existing_entries.append(new_entry)
                db.channels_db[target_ch]["schedules"][active_b] = existing_entries

                # ASYNC WRITE: was db.save_settings() -- a synchronous full
                # config+every-channel JSON dump running right here in the
                # main loop, on the same tick that pops the explorer queue
                # and sends the UI back to the sub-menu. For a big batch
                # (hundreds/thousands of files freshly added elsewhere in
                # this same save, or just a large existing schedules dict)
                # that write itself was long enough to read as a freeze
                # right at the DONE -> sub-menu transition. The snapshot is
                # still taken synchronously (cheap), only the disk write
                # moves off-thread, so this can't race the metadata-probe
                # thread's own saves (both go through the same _save_lock).
                db.save_settings_async()
                print(f"[PERSISTENCE INITIALIZING] Saved immediate fast file pathways for Channel {target_ch} folder: {active_b}")

                # INSTANT PLAY: if this is the channel currently tuned in, refresh
                # playback right now, using the placeholder (-1 -> _safe_dur fallback)
                # duration just written above -- don't wait on the background
                # duration probe below to finish first. That probe can take a
                # couple seconds (real ffmpeg/VLC work) and previously was the
                # ONLY thing that triggered this refresh (see the old "if i == 0"
                # check inside _deferred_metadata_probing_worker), which is why
                # newly-added videos felt slow to appear or needed the menu
                # closed to force a refresh some other way. The probe still runs
                # exactly as before and patches in the real duration once known.
                if str(app_state.get("current_channel", "")).zfill(2) == str(target_ch).zfill(2):
                    print(f"[MUTE TRACE] instant-play reload triggered for ch{target_ch} by newly-added media: "
                          f"is_muted={db.config.get('is_muted', False)}, show_menu={app_state.get('show_menu', False)}")
                    app_state["is_playing_video"] = False
                    change_channel(target_ch, is_surfing=False)

                def _deferred_metadata_probing_worker(ch_key, block_key, items_pool):
                    import time as _time
                    initialize_vlc_on_demand()
                    pygame.time.wait(300)
                    is_vis = db.channels_db.get(ch_key, {}).get("is_visualizer", False)
                    save_counter = 0
                    pool_size = len(items_pool)
                    for i, item_path in enumerate(items_pool):
                        norm_p = os.path.normpath(item_path)
                        # CROSS-CHANNEL REUSE: check the global path-keyed cache
                        # first -- if this exact physical file was already probed
                        # via a DIFFERENT channel (e.g. the same song already sitting
                        # on channel 11 and just now added to channel 14 too), reuse
                        # that known-good value instead of launching VLC/ffmpeg all
                        # over again for a file we already fully know.
                        _cached = _get_cached_media_probe(norm_p)
                        probed = _cached.get("duration")
                        probed_gain = _cached.get("gain")
                        # FIXED: music/visualizer tracks were never actually probed —
                        # every song got hardcoded to 210s regardless of real length,
                        # which desyncs calculate_slotted_playback_state's wall-clock
                        # playlist math from what pygame.mixer.music actually plays:
                        # a real song longer than 210s gets cut off early (the virtual
                        # slot ends before playback does), and a real song shorter
                        # than 210s finishes and restarts from 0 (playback ends but
                        # the virtual slot hasn't). Probing real duration for both
                        # branches keeps the schedule in sync with actual playback.
                        if probed is None:
                            try:
                                probed = get_media_duration(norm_p)
                            except Exception as e:
                                probed = None
                                print(f"[DURATION PROBE] {norm_p}: {e}")
                        # FIXED: this used to ALWAYS assign a duration --
                        # int(probed) on success, else a guessed 210/1800
                        # fallback on failure -- and wrote that guess straight
                        # into entry["duration"], permanently overwriting the
                        # -1 "unprobed" sentinel with a fake real-looking
                        # value. That's what caused movies to freeze at a flat
                        # 1800s and get misclassified as short-form series
                        # content by is_movie_track's duration threshold
                        # (a real movie whose probe failed once would silently
                        # drop out of the movie pool for good), and it's why
                        # the boot-time/precache retry passes -- which only
                        # re-queue entries still sitting at -1 -- never got a
                        # second chance to fix it. Only ever write a REAL
                        # probed duration; on failure, leave the entry at -1
                        # so the next pass (precache tiers / boot resume-probe)
                        # retries it. _safe_dur already supplies the runtime
                        # placeholder (210 for visualizer, 1800 otherwise) for
                        # still-unprobed entries without corrupting the stored
                        # value, so nothing regresses for playback in the
                        # meantime.
                        got_real_duration = bool(probed and probed > 0)
                        # LOUDNESS NORMALIZATION: measure this file the same way as
                        # duration, right in the same background pass, so a newly
                        # added movie/song is volume-equalized from the moment it's
                        # first probed instead of needing a separate scan.
                        if probed_gain is None:
                            try:
                                probed_gain = get_media_loudness_gain(norm_p)
                            except Exception as e:
                                probed_gain = None
                                print(f"[LOUDNESS PROBE] {norm_p}: {e}")
                        if got_real_duration or probed_gain:
                            _store_cached_media_probe(
                                norm_p,
                                duration=(int(probed) if got_real_duration else None),
                                gain=probed_gain,
                            )
                        sched = db.channels_db.get(ch_key, {}).get("schedules", {}).get(block_key, [])
                        for entry in sched:
                            if entry.get("path") == norm_p:
                                if got_real_duration:
                                    entry["duration"] = int(probed)
                                if probed_gain:
                                    entry["gain"] = probed_gain
                                break
                        save_counter += 1
                        batch_size = 5 if pool_size < 50 else 25
                        if i == 0 or save_counter >= batch_size:
                            # ASYNC WRITE: was db.save_settings() -- same
                            # synchronous full config+every-channel JSON
                            # dump this file already made async at every
                            # other call site (see the "ASYNC WRITE"
                            # comments elsewhere). This is the one place
                            # that pattern got missed: called from INSIDE
                            # the probe loop, every 5-25 files, for the
                            # whole duration of an add-media scan. Each
                            # sync dump is long enough on slower hardware
                            # to steal the GIL from pygame's render/input
                            # thread -- that's the felt "video settings
                            # lag while media is loading."
                            db.save_settings_async()
                            save_counter = 0
                            # NOTE: playback refresh for the active channel now
                            # happens immediately after the schedule is saved,
                            # right where this thread is started (see "INSTANT
                            # PLAY" above) -- not here. Triggering it again once
                            # probing catches up to file 0 would restart playback
                            # a second time from 0, which is exactly the "plays,
                            # then restarts" symptom this was meant to avoid.
                            # The real (non-placeholder) duration for file 0 is
                            # simply patched into the schedule in place, below,
                            # same as every other file in the pool.
                        # Throttle: priority slots fast, then back off lightly for large pools.
                        # Previously 0.8s/file past index 15 for pools >100 -- for a
                        # freshly-added library of several hundred files that meant the
                        # TV guide and scheduler kept using the unprobed-duration
                        # fallback (1800s) for 10+ minutes, showing a flat half-hour
                        # grid and making shows look like they "started from the
                        # beginning" (the wall-clock math lines up near a 1800s
                        # boundary far more often when every placeholder is exactly
                        # 1800s). Real per-file durations should land within seconds,
                        # not minutes, so cut the steady-state throttle way down --
                        # still enough backoff to avoid hammering disk/ffmpeg at once.
                        if i < 2:
                            _time.sleep(0.05)
                        elif i < 15:
                            _time.sleep(0.1)
                        else:
                            _time.sleep(0.15 if pool_size > 100 else 0.1)
                    if save_counter > 0:
                        # Forced (still off-thread-safe) so the last batch
                        # is guaranteed to land before this daemon thread
                        # exits, same reasoning as _probe_and_patch_durations.
                        db.save_settings_async(force=True)
                        # Invalidate the guide timeline cache so the newly-probed
                        # durations (and corrected movie/episode classification)
                        # appear immediately the next time the guide renders,
                        # rather than waiting for a viewport-position change to
                        # bust the inner _guide_timeline_cache.
                        if 'app_state' in globals():
                            app_state["guide_refresh_token"] = app_state.get("guide_refresh_token", 0) + 1
                    print(f"[PERSISTENCE SUCCESS] Throttled background prober done for {len(items_pool)} file(s) in Channel {ch_key} / {block_key}.")

                import threading
                threading.Thread(target=_deferred_metadata_probing_worker, args=(target_ch, active_b, list(deduped_files)), daemon=True).start()
    except Exception as e: 
        print(f"[EXPLORER QUEUE EXCEPTION] {e}")

    for event in events:
        # PYGAME FILE EXPLORER: intercept ALL events while the overlay is open
        if app_state.get("file_explorer_active", False):
            _fe_handle_event(event)
            continue

        if event.type == pygame.QUIT:
            if vlc_engine is not None: 
                try: vlc_engine.stop()
                except Exception as e: print(f"[QUIT] vlc stop failed: {e}")
            if dvd_engine is not None:
                try: dvd_engine.stop()
                except Exception as e: print(f"[QUIT] dvd engine stop failed: {e}")
            if db is not None:
                db.config["last_channel"] = app_state.get("current_channel", "05")
                try: db.save_settings()
                except Exception as e: print(f"[QUIT] save_settings failed: {e}")
            if db is not None and db.config.get("start_on_boot", False):
                _launch_explorer_fallback()
            pygame.quit()
            sys.exit()

        # --- AUTO-INIT NEWLY CONNECTED JOYSTICKS ---
        if event.type == pygame.JOYDEVICEADDED:
            try:
                _j = pygame.joystick.Joystick(event.device_index)
                _j.init()
                print(f"[CTRL] Joystick connected: {_j.get_name()} (id={event.device_index})")
            except Exception as _je:
                print(f"[CTRL] Joystick connect error: {_je}")

        # --- CHANNEL 03 CONTROLLER INPUT ---
        # Navigation (D-pad/stick/A/B) is handled by the XInput nav thread in
        # game_deck.py which posts synthetic KEYDOWN events — no SDL joystick
        # subsystem needed. This block only handles screensaver dismiss from
        # any residual JOYBUTTONDOWN events (e.g. if SDL joystick is ever init'd
        # elsewhere), and can otherwise be left as a no-op for joy events.
        if (app_state.get("current_channel") == "03"
                and game_deck is not None
                and not app_state.get("show_menu", False)
                and not app_state.get("show_splash", False)
                and not app_state.get("show_quit_confirm", False)
                and not app_state.get("is_loading", False)):
            if event.type in (pygame.JOYBUTTONDOWN, pygame.JOYHATMOTION,
                               pygame.JOYAXISMOTION):
                app_state["ch03_screensaver_last_input"] = pygame.time.get_ticks()
                if app_state.get("ch03_screensaver_active", False):
                    app_state["ch03_screensaver_active"] = False
                    if app_state.get("ch03_ss_was_game_running", False) and hasattr(game_deck, "unfreeze_libretro"):
                        print("[SCREENSAVER] Unfreezing emulator after controller dismiss.")
                        game_deck.unfreeze_libretro(muted=False)
                    app_state["ch03_ss_was_game_running"] = False
                continue  # consumed — nav is handled by XInput thread via KEYDOWN

        if event.type == pygame.VIDEORESIZE:
            # Ignore resize events that are just side effects of our OWN
            # minimize/restore set_mode() calls (see window_event_guard_until
            # set in the fullscreen toggle). Without this, the stray VIDEORESIZE
            # Windows posts when leaving exclusive fullscreen gets processed a
            # frame later and clobbers WINDOW_WIDTH/HEIGHT + is_fullscreen,
            # which is what made the first minimize press "reset to fullscreen"
            # and required a second press to actually minimize.
            if pygame.time.get_ticks() < app_state.get("window_event_guard_until", 0):
                continue
            # Don't let a plain resize race the native title-bar Maximize
            # button. Clicking it fires BOTH VIDEORESIZE and WINDOWMAXIMIZED
            # for the same click, and VIDEORESIZE isn't guaranteed to be
            # handled after WINDOWMAXIMIZED. Treating it as an ordinary
            # resize here first calls set_mode(RESIZABLE) below, which
            # un-maximizes the real OS window as a side effect -- so the
            # button's icon flips back to "restore" without ever reaching
            # fullscreen, and only a SECOND click (now starting from a
            # genuinely non-maximized window) makes it through to the
            # WINDOWMAXIMIZED branch and does the real fullscreen swap.
            # Checking IsZoomed lets WINDOWMAXIMIZED own maximize clicks
            # exclusively, so the first click reaches fullscreen like before.
            _vr_is_zoomed = False
            if sys.platform == "win32":
                try:
                    _vr_hwnd = pygame.display.get_wm_info().get("window")
                    if _vr_hwnd:
                        _vr_is_zoomed = bool(ctypes.windll.user32.IsZoomed(_vr_hwnd))
                except Exception:
                    _vr_is_zoomed = False
            if (not _vr_is_zoomed and not app_state["is_fullscreen"]
                    and (event.w, event.h) != (WINDOW_WIDTH, WINDOW_HEIGHT)):
                WINDOW_WIDTH, WINDOW_HEIGHT = event.w, event.h
                screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.RESIZABLE)
                
        elif event.type == pygame.WINDOWMAXIMIZED:
            # Same guard: suppress the maximize event that fires as a side
            # effect of our explicit toggle so it can't fight the toggle and
            # re-enter fullscreen on what was supposed to be a minimize.
            if pygame.time.get_ticks() < app_state.get("window_event_guard_until", 0):
                continue
            monitor_info = pygame.display.Info()
            WINDOW_WIDTH = monitor_info.current_w
            WINDOW_HEIGHT = monitor_info.current_h
            screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.FULLSCREEN | pygame.RESIZABLE)
            app_state["is_fullscreen"] = True

# ==============================================================================
# PART 8a OF 28: DYNAMIC MENU OVERLAY INTERCEPTS
# ==============================================================================

        elif event.type == _ESC_CAPTURE_RESOLVE_EVENT:
            # Fires _ESC_DOUBLE_TAP_MS after a single ESC keydown during
            # REMOTE_REMAP_CAPTURE/NUMBER_REMAP_CAPTURE with no second ESC
            # following it -- commit that single tap as a normal binding for
            # whichever action was being captured at the time. See the
            # _ESC_CAPTURE_RESOLVE_EVENT comment above for why this exists.
            pygame.time.set_timer(_ESC_CAPTURE_RESOLVE_EVENT, 0)  # one-shot; stop repeats
            _pending = app_state.get("_esc_capture_pending")
            app_state["_esc_capture_pending"] = None
            if _pending is not None and app_state.get("menu_layer") == _pending["capture_layer"]:
                _action_set = NUMBER_REMAP_ACTIONS if _pending["is_number_capture"] else REMOTE_REMAP_ACTIONS
                if app_state.get("remote_remap_index", -1) == _pending["remap_idx"]:
                    _esc_conflict = check_remap_conflict(db, app_state, _pending["action_id"], pygame.K_ESCAPE)
                    if _esc_conflict is not None:
                        app_state["_remap_reject_flash"] = pygame.time.get_ticks()
                        app_state["_remap_reject_key"]   = pygame.K_ESCAPE
                        print(f"[REMOTE REMAP] Rejected key {pygame.K_ESCAPE} -- "
                              f"already bound to: {_esc_conflict}", flush=True)
                    else:
                        set_remote_binding(db, _pending["action_id"], pygame.K_ESCAPE)
                        app_state.setdefault("_remap_used_keys", []).append(pygame.K_ESCAPE)
                        app_state["_remap_reject_flash"] = None
                        app_state["_remap_last_accept"] = {"ms": pygame.time.get_ticks(), "key": pygame.K_ESCAPE, "scancode": 41}
                        next_idx = _pending["remap_idx"] + 1
                        if next_idx < len(_action_set):
                            app_state["remote_remap_index"] = next_idx
                        else:
                            app_state["menu_layer"] = "REMOTE_REMAP_LIST"
                            app_state["menu_selection_index"] = _pending["landing_sel"]
                            app_state["remote_remap_index"] = 0
                            app_state.pop("_remap_used_keys", None)
                            app_state["_remap_reject_flash"] = None
                            print("[REMOTE REMAP] Sequence complete.")
                        print(f"[REMOTE REMAP] '{_pending['action_id']}' bound to ESC (single tap, no cancel follow-up).")

        elif event.type == pygame.KEYUP:
            # Mirror image of the KEYDOWN translation+tracking below: apply
            # the same remap translation so a remapped physical button's
            # release clears the SAME canonical key that its press set, then
            # drop it from the manual held-keys set. Without this, a key
            # that only ever reaches pygame via the OS keyboard hook (e.g. a
            # remote's volume button) would look "held forever" once
            # pressed, since get_pressed() never saw it as pressed in the
            # first place and this was the only place tracking it.
            if app_state.get("menu_layer") not in ("REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"):
                event.key = translate_remote_key(db, event.key)
            app_state["_held_keys"].discard(event.key)

        elif event.type == pygame.KEYDOWN:
            now_ticks = pygame.time.get_ticks()
            
            # ANTI-BURN-IN TIMER: Save the exact time a user actively presses any button
            app_state["last_input_time"] = now_ticks

            # --- GLOBAL KEY TRANSLATION LAYER (Remote / Number Remapping) ---
            # Rewrite event.key to its canonical value ONCE here, so every
            # literal pygame.K_w / K_RETURN / K_5 etc. check elsewhere in the
            # file keeps working unchanged no matter what physical key the
            # user (or a real USB remote) actually sent. Skipped while a
            # remap-capture screen is waiting for the next raw keypress.
            if app_state.get("menu_layer") not in ("REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"):
                event.key = translate_remote_key(db, event.key)

            # Track this key as currently held (post-translation, so a
            # remapped physical button is tracked under its canonical key).
            # See PART 27 below for why pygame.key.get_pressed() alone isn't
            # enough here -- this is the only reliable "is it still held"
            # signal for any key delivered through the OS keyboard hook
            # (volume, media, F-keys, browser keys, and anything remapped
            # onto them), since posted synthetic events never update SDL's
            # own keyboard-state array that get_pressed() reads from.
            app_state["_held_keys"].add(event.key)

            # CH03 SCREENSAVER: reset inactivity timer and dismiss on any key press
            if app_state.get("current_channel") == "03":
                app_state["ch03_screensaver_last_input"] = now_ticks
                if app_state.get("ch03_screensaver_active", False):
                    app_state["ch03_screensaver_active"] = False
                    globals()["_ch03_ss_surf_cache"] = None  # force re-scale on next show
                    # Unfreeze emulator if it was running when saver activated
                    if app_state.get("ch03_ss_was_game_running", False) and game_deck is not None and hasattr(game_deck, "unfreeze_libretro"):
                        print("[SCREENSAVER] Unfreezing emulator after keyboard dismiss.")
                        game_deck.unfreeze_libretro(muted=False)
                    app_state["ch03_ss_was_game_running"] = False
                    continue  # swallow this keypress — don't let it trigger menu actions

            # ── GAMES_CTRL_MAP: full W/S/A/D navigation + Enter to select ─────   ─
            # Three bottom options (index 0/1/2): Map Controller | L-Stick toggle | Close
            # W/S/Up/Down and A/D/Left/Right all move the cursor between options.
            # Enter activates. ESC is handled by the existing back-nav dict above.
            if (app_state.get("show_menu") and
                    app_state.get("menu_layer") == "GAMES_CTRL_MAP" and
                    game_deck is not None):

                # While sequential remap is running, only ESC matters
                if getattr(game_deck, "_seq_remap_active", False):
                    if event.key == pygame.K_ESCAPE:
                        game_deck.ctrl_mapping_active_btn = None
                        game_deck._seq_remap_active = False
                        # fall through so ESC back-nav dict steps the layer back
                    else:
                        continue   # swallow all other keys so TV controls don't fire

                _ci = app_state.get("ctrl_map_cursor", 0)   # 0=Map 1=Stick 2=Close

                if event.key in (pygame.K_w, pygame.K_UP, pygame.K_a, pygame.K_LEFT):
                    app_state["ctrl_map_cursor"] = (_ci - 1) % 3
                    continue
                if event.key in (pygame.K_s, pygame.K_DOWN, pygame.K_d, pygame.K_RIGHT):
                    app_state["ctrl_map_cursor"] = (_ci + 1) % 3
                    continue

                if event.key == pygame.K_RETURN:
                    _ci = app_state.get("ctrl_map_cursor", 0)
                    if _ci == 0:
                        game_deck.start_sequential_remap()
                    elif _ci == 1:
                        game_deck.ctrl_mapping["stick_as_dpad"] = not game_deck.ctrl_mapping.get("stick_as_dpad", False)
                        game_deck._save_controller_mapping()
                    elif _ci == 2:
                        game_deck._mapping_close_requested = True
                    continue

                if event.key == pygame.K_ESCAPE:
                    pass   # let ESC fall through to the back-nav dict below

                else:
                    continue   # swallow everything else so volume/channel keys don't fire

            # ── REMOTE_REMAP_CAPTURE / NUMBER_REMAP_CAPTURE: swallow EVERY ──────
            # key except ESC (unless ESC is itself the action being bound) and
            # use it as the new binding for whichever action is currently
            # being remapped, then auto-advance.
            _capture_layer = app_state.get("menu_layer")
            if app_state.get("show_menu") and _capture_layer in ("REMOTE_REMAP_CAPTURE", "NUMBER_REMAP_CAPTURE"):
                # DEBUG: log every raw event that arrives while capture is
                # armed, BEFORE any filtering — proves whether the button
                # press reached pygame's queue at all, and with what key/
                # scancode/synthetic values. Remove once mapping is confirmed
                # fixed on all hardware.
                print(f"[REMOTE REMAP DEBUG] capture armed, event arrived: "
                      f"key={event.key} scancode={getattr(event, 'scancode', '?')} "
                      f"synthetic={getattr(event, 'synthetic', False)}", flush=True)
                # Skip synthetic controller nav events — controller buttons must
                # not be stored as keyboard/remote bindings.
                if getattr(event, "synthetic", False):
                    continue

                # DUPLICATE-DELIVERY DE-DUP (see _REMAP_CAPTURE_DEDUP_MS above).
                # One physical button (the back-arrow originally; this
                # remote's Menu/Home button confirmed the same way since)
                # can reach us as TWO KEYDOWN events at once via two
                # different Windows input paths (WM_APPCOMMAND + WH_KEYBOARD_LL).
                # The first bound the intended row and the second auto-bound
                # the next row too. Once a binding has just been committed,
                # swallow any NON-ESC capture keydown that lands inside the
                # dedup window -- that's the duplicate from the other
                # delivery path, never a deliberate press for the next row
                # (those come ~1s later, per the prompt changing). ESC is
                # intentionally exempt so the double-ESC cancel gesture (two
                # ESC within _ESC_DOUBLE_TAP_MS) still works.
                #
                # FIXED: this used to only dedup when event.key MATCHED the
                # just-accepted key (treating it as "same key, maybe
                # different scancode"). That assumed both delivery paths of
                # a doubled button decode to the SAME final pygame key. Real
                # logs disprove that: the WM_APPCOMMAND subclass fabricates
                # its own scancode (500 + cmd_id) for any command ID that
                # isn't in _APPCOMMAND_TO_SDL_SC, while the WH_KEYBOARD_LL
                # hook reposts its own _VK_TO_SDL_SC scancode for the same
                # physical VK -- two DIFFERENT pygame keys for one press
                # (e.g. Back showed up as 501 AND 270; this remote's Menu/
                # Home button does the same thing once rearranged into a
                # spot where its second delivery lands fast enough to be
                # read as "the next row"). Requiring a key match meant that
                # second, differently-keyed delivery sailed straight through
                # and auto-bound whatever row came next -- reproducing the
                # exact "double-mapping" bug, just for a different button
                # depending on which row happens to sit right after it.
                # Now: ANY non-ESC capture keydown (regardless of what key
                # it decodes to) arriving within the hard window of the
                # last ACCEPTED binding is treated as that same physical
                # press's duplicate delivery and swallowed outright. A
                # human moving on to genuinely press the next row's button
                # never happens this fast (~1s+ cadence); only two delivery
                # paths for one press land this close together.
                if event.key != pygame.K_ESCAPE:
                    _last_accept = app_state.get("_remap_last_accept")
                    if _last_accept is not None:
                        _ra_dt      = pygame.time.get_ticks() - _last_accept["ms"]
                        _ra_same_key = (event.key == _last_accept["key"])
                        _ra_same_sc  = getattr(event, "scancode", 0) == _last_accept["scancode"]
                        _REMAP_HARD_DEDUP_MS = 250
                        if _ra_dt < _REMAP_HARD_DEDUP_MS:
                            # Within the hard window of the last accepted
                            # binding -- always a duplicate delivery of that
                            # SAME physical press, whether or not it decoded
                            # to the same pygame key. Swallow unconditionally.
                            print(f"[REMOTE REMAP] Ignored duplicate capture event "
                                  f"(key={event.key} scancode={getattr(event, 'scancode', '?')}, "
                                  f"last accepted key={_last_accept['key']}) within "
                                  f"{_REMAP_HARD_DEDUP_MS}ms -- second delivery path of same "
                                  f"physical press.", flush=True)
                            continue
                        if _ra_same_key and _ra_dt < _REMAP_CAPTURE_DEDUP_MS and not _ra_same_sc:
                            # Same key, different scancode, within the softer window --
                            # a slower-arriving duplicate on a second input path.
                            print(f"[REMOTE REMAP] Ignored duplicate capture event "
                                  f"(key={event.key} scancode={getattr(event, 'scancode', '?')}) "
                                  f"within {_REMAP_CAPTURE_DEDUP_MS}ms of the last binding -- "
                                  f"same physical button arriving on a second input path.", flush=True)
                            continue

                _is_number_capture = (_capture_layer == "NUMBER_REMAP_CAPTURE")
                _action_set = NUMBER_REMAP_ACTIONS if _is_number_capture else REMOTE_REMAP_ACTIONS
                _landing_sel = 1 if _is_number_capture else 0   # both capture modes return to the same combined list

                remap_idx = app_state.get("remote_remap_index", 0)
                action_id, action_label = _action_set[remap_idx]

                # Some remotes' OK/Back buttons emit a literal, real hardware
                # VK_ESCAPE (confirmed via logs: key=27 scancode=41
                # synthetic=False during capture) -- indistinguishable from an
                # actual keyboard Escape press. So a single ESC here can no
                # longer mean "cancel" outright; it might be the very button
                # being bound. Require a SECOND ESC within _ESC_DOUBLE_TAP_MS
                # to cancel; a single tap with no follow-up commits as a
                # normal binding once the window closes (see
                # _ESC_CAPTURE_RESOLVE_EVENT handler above).
                if event.key == pygame.K_ESCAPE:
                    _pending = app_state.get("_esc_capture_pending")
                    if (_pending is not None
                            and _pending["capture_layer"] == _capture_layer
                            and _pending["remap_idx"] == remap_idx):
                        # Second ESC arrived in time -- cancel the sequence.
                        pygame.time.set_timer(_ESC_CAPTURE_RESOLVE_EVENT, 0)
                        app_state["_esc_capture_pending"] = None
                        app_state["menu_layer"] = "REMOTE_REMAP_LIST"
                        app_state["menu_selection_index"] = _landing_sel
                        app_state["remote_remap_index"] = 0
                        app_state.pop("_remap_used_keys", None)
                        app_state["_remap_reject_flash"] = None
                        print("[REMOTE REMAP] Sequence cancelled via double ESC.")
                    else:
                        # First ESC -- arm the resolve window instead of
                        # binding/cancelling immediately.
                        app_state["_esc_capture_pending"] = {
                            "capture_layer": _capture_layer,
                            "is_number_capture": _is_number_capture,
                            "remap_idx": remap_idx,
                            "action_id": action_id,
                            "landing_sel": _landing_sel,
                        }
                        pygame.time.set_timer(_ESC_CAPTURE_RESOLVE_EVENT, _ESC_DOUBLE_TAP_MS, loops=1)
                        print("[REMOTE REMAP] ESC captured, waiting to see if a 2nd ESC cancels...")
                    continue

                # Any non-ESC key: if there's STILL a pending single-ESC
                # resolve window open for THIS SAME row, the row hasn't
                # actually advanced yet (that only happens when the
                # _ESC_CAPTURE_RESOLVE_EVENT timer fires, up to
                # _ESC_DOUBLE_TAP_MS later). The old code just cleared the
                # pending state here and rebound *this* row (e.g. "esc"/
                # Back) to whatever key arrived next -- so pressing the next
                # remote button quickly (e.g. Mute, right after Back) got
                # bound to Back instead, Back silently stole Mute's button,
                # and the capture sequence then asked for a binding for
                # Mute using whatever came after that -- shifting every
                # remaining action down by one. This only ever showed up
                # when remapping Back specifically, because it's the only
                # action with this deferred double-tap window.
                #
                # Fix: resolve the pending ESC binding right now (exactly as
                # the deferred timer would have) BEFORE using this new key,
                # then re-derive which row we're actually on so the new key
                # goes to the correct (now-current) action.
                _pending = app_state.get("_esc_capture_pending")
                if (_pending is not None
                        and _pending["capture_layer"] == _capture_layer
                        and _pending["remap_idx"] == remap_idx):
                    pygame.time.set_timer(_ESC_CAPTURE_RESOLVE_EVENT, 0)
                    app_state["_esc_capture_pending"] = None
                    _esc_conflict = check_remap_conflict(db, app_state, _pending["action_id"], pygame.K_ESCAPE)
                    if _esc_conflict is not None:
                        app_state["_remap_reject_flash"] = pygame.time.get_ticks()
                        app_state["_remap_reject_key"]   = pygame.K_ESCAPE
                        print(f"[REMOTE REMAP] Rejected key {pygame.K_ESCAPE} -- "
                              f"already bound to: {_esc_conflict}", flush=True)
                        continue
                    set_remote_binding(db, _pending["action_id"], pygame.K_ESCAPE)
                    app_state.setdefault("_remap_used_keys", []).append(pygame.K_ESCAPE)
                    print(f"[REMOTE REMAP] '{_pending['action_id']}' bound to ESC "
                          f"(resolved early -- next button arrived before the double-tap window closed).")
                    remap_idx += 1
                    if remap_idx >= len(_action_set):
                        app_state["menu_layer"] = "REMOTE_REMAP_LIST"
                        app_state["menu_selection_index"] = _landing_sel
                        app_state["remote_remap_index"] = 0
                        app_state.pop("_remap_used_keys", None)
                        app_state["_remap_reject_flash"] = None
                        print("[REMOTE REMAP] Sequence complete.")
                        continue
                    app_state["remote_remap_index"] = remap_idx
                    action_id, action_label = _action_set[remap_idx]
                else:
                    app_state["_esc_capture_pending"] = None

                # ── Duplicate-binding conflict check ─────────────────────────
                # Reject a key that's already assigned to a DIFFERENT action in
                # the combined Remote + Number namespace, or was already used for
                # an earlier action in THIS session.  This prevents lockouts from
                # accidental double-mapping (e.g. binding "Back" to both Nav-Up
                # and Back/Minimize) that left one action permanently unreachable.
                _remap_conflict_lbl = check_remap_conflict(db, app_state, action_id, event.key)
                if _remap_conflict_lbl is not None:
                    app_state["_remap_reject_flash"] = pygame.time.get_ticks()
                    app_state["_remap_reject_key"]   = event.key
                    print(f"[REMOTE REMAP] Rejected key {event.key} -- "
                          f"already bound to: {_remap_conflict_lbl}", flush=True)
                    continue

                set_remote_binding(db, action_id, event.key)
                # Track this key so later actions in the same session cannot
                # reuse it (the conflict check above tests _remap_used_keys).
                app_state.setdefault("_remap_used_keys", []).append(event.key)
                # Stamp the accept so the dedup guard above can swallow a
                # duplicate of THIS same physical press arriving on a second
                # input path within _REMAP_CAPTURE_DEDUP_MS (see that guard).
                # Storing key+scancode lets the guard distinguish a hardware
                # duplicate (same key, different scancode) from the user
                # intentionally pressing the same button for the next action
                # (same key, same scancode) -- only the former is swallowed.
                app_state["_remap_reject_flash"] = None  # clear any stale flash
                app_state["_remap_last_accept"] = {
                    "ms":       pygame.time.get_ticks(),
                    "key":      event.key,
                    "scancode": getattr(event, "scancode", 0),
                }

                next_idx = remap_idx + 1
                if next_idx < len(_action_set):
                    app_state["remote_remap_index"] = next_idx
                else:
                    # Done — land back on the button that started this sequence.
                    app_state["menu_layer"] = "REMOTE_REMAP_LIST"
                    app_state["menu_selection_index"] = _landing_sel
                    app_state["remote_remap_index"] = 0
                    app_state.pop("_remap_used_keys", None)
                    print("[REMOTE REMAP] Sequence complete.")
                continue

            # FIXED INTERCEPT MATRIX: Check for global menu keys BEFORE tunneling to game deck!
            # This allows M and C to open settings sheets instantly right over playing videos.
            # C is excluded entirely when Kiosk Mode is on -- see the C-key
            # handler below for why -- so it falls through to normal game
            # deck/TV handling like any other unbound key instead of being
            # treated as a menu shortcut.
            # (C was previously used as DVD fast-forward default; DVD transport
            # defaults are now media keys so C is free again for controls.)
            _kiosk_on_now = db.config.get("kiosk_mode_enabled", False) if db is not None else False
            is_global_menu_trigger = event.key == pygame.K_m or (event.key == pygame.K_c and not _kiosk_on_now)
            is_any_menu_showing = (
                app_state.get("show_menu", False) or 
                app_state.get("show_splash", False) or 
                app_state.get("show_quit_confirm", False)
            )

            # --- CHANNEL 03 DVD CONFIRM-POPUP GATE (highest priority) ---
            # While a DVD confirm popup is open -- quit-to-TV (opened by ESC)
            # or back-to-main-menu (opened by Backspace, ISO only) -- the
            # ONLY live keys are A / D (move the Yes/No highlight) and Enter
            # (commit the choice). Channel up/down (the UP/DOWN arrows) stay
            # live too, so the user can surf away and come back to find the
            # popup exactly where they left it. Everything else is dead until
            # a choice is made: volume (LEFT/RIGHT arrows), the B / M settings
            # triggers, DVD transport (z/x/c/v) and WASD menu-nav.
            #
            # This runs BEFORE the menu-trigger / tunneling valve below on
            # purpose: B and M would otherwise be caught by is_global_menu_
            # trigger and open a settings sheet over the popup, and LEFT/RIGHT
            # would otherwise reach the global volume handler. Catching them
            # here first is what enforces "no other button works until a
            # choice is made."
            if (app_state.get("current_channel") == "03"
                    and 'game_deck' in globals() and game_deck is not None
                    and getattr(game_deck, "mode", "BROWSE") == "DVD_PLAYER"
                    and (getattr(game_deck, "dvd_exit_confirm", False)
                         or getattr(game_deck, "dvd_return_menu_confirm", False))):
                if event.key in (pygame.K_a, pygame.K_d, pygame.K_RETURN):
                    print(f"[DVD GATE] Confirm popup open -> routing '{pygame.key.name(event.key)}' to DVD deck.")
                    game_deck.handle_event(event)
                    continue
                if event.key in (pygame.K_UP, pygame.K_DOWN):
                    # Fall through to the global channel-change handler. The
                    # tunneling valve's is_global_tv_key branch passes UP/DOWN
                    # straight through untouched, so channel surf still works
                    # with the popup intact.
                    print(f"[DVD GATE] Confirm popup open -> channel key '{pygame.key.name(event.key)}' stays live.")
                else:
                    print(f"[DVD GATE] Confirm popup open -> swallowing '{pygame.key.name(event.key)}' (inactive until a choice is made).")
                    continue

            # --- CHANNEL 03 EVENT TUNNELING VALVE ---
            if app_state.get("current_channel") == "03" and not is_any_menu_showing and not is_global_menu_trigger:
                if 'game_deck' in globals() and game_deck is not None:
                    deck_mode = getattr(game_deck, "mode", "BROWSE")
                    is_global_tv_key = event.key in [pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_n]
                    is_emulator_live  = (deck_mode == "GAME"
                                         and not getattr(game_deck, "emulator_loading", False))

                    if event.key == pygame.K_ESCAPE and deck_mode == "BROWSE":
                        pass  # Let ESC fall through to global handler on carousel
                    elif is_emulator_live:
                        pass  # Emulator running: ALL keys fall through to TV controls.
                              # mGBA input comes from XInput hardware poll, not pygame events.
                    elif deck_mode == "GAME" and getattr(game_deck, "emulator_loading", False):
                        # Game is in its launch/loading transition — swallow ALL game-input
                        # keys including ESC. The loading screen is a one-way door: once you
                        # commit to launching a game the only way out is channel-change, not
                        # ESC (which would incorrectly open the system minimize dialog here).
                        continue
                    elif deck_mode == "DVD_PLAYER" and is_global_tv_key:
                        # DVD mode: arrow/N keys stay global for volume/channel.
                        # (Confirm-popup gating is handled entirely by the
                        # CHANNEL 03 DVD CONFIRM-POPUP GATE above, before this
                        # valve -- so when a popup is open only UP/DOWN even
                        # reach here, and they correctly pass through to the
                        # global channel-change handler.)
                        pass
                    elif deck_mode == "DVD_PLAYER" and event.key in (
                            pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d,
                            pygame.K_ESCAPE, pygame.K_RETURN):
                        # DVD-scoped keyboard remap: WASD / ESC / Enter are
                        # always consumed by the DVD player — they must never
                        # bleed through to global handlers (quit dialog, channel
                        # surf, volume) regardless of handle_event's return value.
                        # Controller inputs arrive via identical synthetic KEYDOWN
                        # events posted by the XInput nav thread and follow this
                        # same path, keeping controller and keyboard behaviour
                        # identical while both remain invisible to global handlers.
                        game_deck.handle_event(event)
                        continue
                    else:
                        if game_deck.handle_event(event):
                            continue  # Event consumed by game deck — bypass global shortcuts

            if app_state.get("is_loading", False):
                continue

            # CRASH SHIELD GUARD GATE: If actively renaming a station, bypass global shortcuts
            if app_state.get("editing_channel_name", False):
                target_ch_str = str(app_state["selected_channel_row"]).zfill(2)
                ch_info = db.channels_db.get(target_ch_str, {})
                current_name = ch_info.get("name", f"STATION {target_ch_str}")
                all_selected = app_state.get("channel_name_all_selected", False)
                
                if event.key == pygame.K_RETURN:
                    app_state["editing_channel_name"] = False
                    app_state["channel_name_all_selected"] = False
                    db.save_settings()
                    print(f"[NAME ENGINE] Saved customized channel label: '{current_name}'")
                elif event.key == pygame.K_BACKSPACE:
                    # If all text is selected, backspace clears the whole name
                    ch_info["name"] = "" if all_selected else current_name[:-1]
                    app_state["channel_name_all_selected"] = False
                elif event.key == pygame.K_SPACE:
                    if all_selected:
                        ch_info["name"] = " "
                        app_state["channel_name_all_selected"] = False
                    elif len(current_name) < 18:
                        ch_info["name"] = current_name + " "
                else:
                    if event.unicode.isalnum():
                        if all_selected:
                            # Replace entire name with the typed character
                            ch_info["name"] = event.unicode
                            app_state["channel_name_all_selected"] = False
                        elif len(current_name) < 18:
                            ch_info["name"] = current_name + event.unicode
                continue 


# ==============================================================================
# PART 8b OF 28: DYNAMIC VIDEO HUD OVERLAYS
# ==============================================================================

            if app_state["current_channel"] == "04":
                app_state["guide_inactivity_timer"] = now_ticks + 60000

            # --- SPLASH SCREEN SYSTEM OVERLAY CONTROLLER ---
            if app_state.get("show_splash", False):
                page  = app_state.get("controls_splash_page",  0)
                focus = app_state.get("controls_splash_focus", 1)

                if event.key == pygame.K_ESCAPE or event.key == pygame.K_c:
                    app_state["show_splash"] = False
                    app_state["splash_shown_at"] = 0
                    app_state["controls_splash_page"]  = 0
                    app_state["controls_splash_focus"] = 1
                    print("[TELEMETRY] Control splash menu dismissed.")
                elif event.key == pygame.K_a or event.key == pygame.K_LEFT:
                    if page == 1:
                        app_state["controls_splash_focus"] = 0
                elif event.key == pygame.K_d or event.key == pygame.K_RIGHT:
                    if page == 1:
                        app_state["controls_splash_focus"] = 1
                elif event.key == pygame.K_RETURN:
                    if page == 0:
                        app_state["controls_splash_page"]  = 1
                        app_state["controls_splash_focus"] = 1
                    else:
                        if focus == 0:
                            app_state["controls_splash_page"]  = 0
                            app_state["controls_splash_focus"] = 1
                        else:
                            app_state["show_splash"] = False
                            app_state["splash_shown_at"] = 0
                            app_state["controls_splash_page"]  = 0
                            app_state["controls_splash_focus"] = 1
                            print("[TELEMETRY] Control splash menu dismissed.")
                continue
            
            # --- MINIMIZE WINDOWS RE-ENTRY CONFIRMATION PROMPTS ---
            if app_state.get("show_quit_confirm", False):
                app_state["quit_confirm_shown_at"] = now_ticks
                if event.key == pygame.K_RETURN:
                    if app_state.get("is_fullscreen", True):
                        # Use the SAME size/shape logic as the windowed boot path
                        # (_compute_boot_window_size, keyed off DETECTED_ASPECT_RATIO)
                        # instead of a hardcoded 1280x720. The hardcoded value was
                        # always 16:9-shaped, so on a 4:3 display the re-minimized
                        # window came out oversized/wrong-shaped compared to the
                        # windowed-boot size the user configured. Recomputing here
                        # keeps minimize-restore visually identical to boot-windowed.
                        min_width, min_height = _compute_boot_window_size(
                            NATIVE_WIDTH, NATIVE_HEIGHT, DETECTED_ASPECT_RATIO
                        )
                        WINDOW_WIDTH, WINDOW_HEIGHT = min_width, min_height
                        screen = pygame.display.set_mode((min_width, min_height), pygame.RESIZABLE)
                        app_state["is_fullscreen"] = False
                        # Center the windowed frame and settle it in one shot:
                        # sizes the OUTER window so the CLIENT is exactly
                        # min_width x min_height (no clipped edges) and presents a
                        # clean frame so there's no black flash. See helper docs.
                        _pos_x = max(0, (NATIVE_WIDTH - min_width) // 2)
                        _pos_y = max(0, (NATIVE_HEIGHT - min_height) // 2)
                        _finalize_window_mode(
                            screen, windowed=True,
                            client_w=min_width, client_h=min_height,
                            pos_x=_pos_x, pos_y=_pos_y,
                        )
                    else:
                        # Plain fullscreen<->windowed toggle, regardless of Kiosk
                        # Mode / Boot on Start -- pressing ENTER while windowed
                        # always restores fullscreen. (A previous revision tried
                        # to make this branch do an OS-level minimize-to-taskbar
                        # instead when Kiosk/Boot were both off, on the theory
                        # that the original "won't minimize" bug was this logic
                        # being backwards. It wasn't -- that bug was actually the
                        # missing Windows DPI-awareness call elsewhere, which is
                        # now fixed separately. This toggle's original behavior
                        # was correct and wanted, so it's restored here.)
                        #
                        # Restore using NATIVE_WIDTH/HEIGHT (not WINDOW_WIDTH/HEIGHT --
                        # those get overwritten to the small windowed size by the
                        # VIDEORESIZE handler below while minimized, see notes there)
                        # and the SAME pygame.NOFRAME flag the app boots with, instead
                        # of pygame.FULLSCREEN. FULLSCREEN triggers a real exclusive
                        # display-mode switch, which is what made every minimize after
                        # the first one laggy and prone to clipping the right edge of
                        # the window -- it was re-entering fullscreen at whatever
                        # (possibly stale/shrunk) size WINDOW_WIDTH/HEIGHT held instead
                        # of the monitor's native resolution, and doing so via a heavier
                        # video-mode change than the borderless-window trick used at boot.
                        # Query the LIVE monitor size instead of trusting the
                        # boot-time NATIVE_WIDTH/HEIGHT globals. Those are read from
                        # pygame.display.Info() BEFORE the first window exists, which
                        # on Windows (especially with display scaling) can report a
                        # smaller size than the real fullscreen surface. That stale
                        # value is why the NOFRAME restore came back SMALLER -- and
                        # looked "not 4:3", since the 4:3 letterbox/border was being
                        # computed against the shrunk surface -- while the buggy
                        # WINDOWMAXIMIZED path (which already re-queried a fresh size)
                        # rendered the correct, bigger 4:3 fullscreen. get_desktop_sizes()
                        # reports the true monitor resolution independent of the current
                        # (small, windowed) surface, so both fullscreen paths now land on
                        # identical dimensions. Keep NOFRAME (not FULLSCREEN) to avoid the
                        # exclusive-mode lag / right-edge clipping documented below.
                        _fs_w, _fs_h = NATIVE_WIDTH, NATIVE_HEIGHT
                        try:
                            _desktops = pygame.display.get_desktop_sizes()
                            if _desktops:
                                _fs_w, _fs_h = _desktops[0]
                        except Exception as _fs_err:
                            print(f"[WINDOW] get_desktop_sizes() failed, using boot native size: {_fs_err}")
                        WINDOW_WIDTH, WINDOW_HEIGHT = _fs_w, _fs_h
                        screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.NOFRAME)
                        app_state["is_fullscreen"] = True
                        # Snap the borderless window to (0,0) at native size and
                        # settle it cleanly. The minimize step above centered a
                        # small window, and SDL keeps that position sticky across
                        # set_mode(), so without an explicit (0,0)+size the restored
                        # screen looked shifted; passing the real native size (no
                        # SWP_NOSIZE) also guarantees it fills the monitor. Presents
                        # a clean frame to kill the black flash. See helper docs.
                        _finalize_window_mode(
                            screen, windowed=False,
                            client_w=WINDOW_WIDTH, client_h=WINDOW_HEIGHT,
                            pos_x=0, pos_y=0,
                        )
                    # Suppress the stray VIDEORESIZE/WINDOWMAXIMIZED events our
                    # own set_mode() call above just queued, so they don't get
                    # replayed next frame and undo this toggle. Clear any already
                    # queued now, and guard for a short window to also catch the
                    # ones Windows posts a frame or two later when leaving
                    # exclusive fullscreen. is_fullscreen is now authoritative.
                    pygame.event.clear(pygame.VIDEORESIZE)
                    pygame.event.clear(pygame.WINDOWMAXIMIZED)
                    app_state["window_event_guard_until"] = pygame.time.get_ticks() + 400
                    app_state["show_quit_confirm"] = False
                elif event.key == pygame.K_ESCAPE:
                    app_state["show_quit_confirm"] = False
                continue

            # Global shortcut triggers for menu openings
            # Kiosk Mode hides the Controls menu entirely -- it's one of the
            # things kids could otherwise pop open and poke around in, and
            # this is its only way in (no button for it lives in a menu).
            if event.key == pygame.K_c and not (db is not None and db.config.get("kiosk_mode_enabled", False)):
                if app_state.get("show_menu", False):
                    app_state["show_menu"] = False  # Close options menu if open
                app_state["show_splash"] = True
                app_state["splash_shown_at"] = now_ticks
                app_state["controls_splash_page"]  = 0
                app_state["controls_splash_focus"] = 1
                continue

            # ESC: close the menu when on the top-level tab bar OR any main menu layer.
            # Only true sub-menus (CHANNEL_SUB_MENU, HOLIDAY_SUB_MENU,
            # GAMES_SUB_MENU) step back one level.
            if event.key == pygame.K_ESCAPE and app_state.get("show_menu", False):
                _layer = app_state.get("menu_layer", "TAB_SELECTION")
                _sub_menu_parent = {
                    "CHANNEL_SUB_MENU":     "CHANNEL_LIST",
                    "HOLIDAY_SUB_MENU":     "CHANNEL_SUB_MENU",
                    "AUDIO_TRACK_PICKER":   "CHANNEL_SUB_MENU",
                    "GAMES_SUB_MENU":       "GAMES_LIST",
                    "GAMES_CTRL_MAP":       "GAMES_SUB_MENU",   # controller mapping panel → back to sub-menu
                    "GAMES_SAVE_PROFILE":   "GAMES_SUB_MENU",   # profile sub-menu → back to console sub-menu
                    "REMOTE_REMAP_LIST":    "SYSTEM_ROWS",      # remap sub-menu → back to System tab rows
                }.get(_layer)
                if _sub_menu_parent is not None:
                    # Inside a true sub-menu — step back one level
                    app_state["menu_layer"] = _sub_menu_parent
                    if _layer == "AUDIO_TRACK_PICKER":
                        # Land back on the "Change Current Playing Audio Track" row
                        # itself -- its position shifts by one in Marathon (see
                        # _audio_track_row in the main CHANNEL_SUB_MENU handler).
                        _atp_ch = db.channels_db.get(str(app_state.get("selected_channel_row", "")).zfill(2), {})
                        _atp_marathon = _atp_ch.get("scheduling_mode", "random_slots") == "marathon"
                        app_state["sub_menu_row_index"] = 7 if _atp_marathon else 9
                        app_state["sub_menu_col_index"] = 0
                    elif _layer == "REMOTE_REMAP_LIST":
                        # Land back on the Remote Remapping row itself rather
                        # than a hardcoded index -- its position in the row
                        # list can shift (e.g. Kiosk Mode being added above
                        # it), so look it up instead of assuming where it is.
                        _sys_rows_now = get_system_menu_rows(db, app_state)
                        app_state["menu_selection_index"] = (
                            _sys_rows_now.index("remote_remap") if "remote_remap" in _sys_rows_now else 0
                        )
                    else:
                        app_state["sub_menu_row_index"] = 0
                        app_state["sub_menu_col_index"] = 0
                    print(f"[SETTINGS ENGINE] ESC: stepped back from {_layer} to {_sub_menu_parent}.")
                else:
                    # TAB_SELECTION or any main menu layer — close the menu entirely.
                    # Do NOT reset loaded_preview_path_cache while on the TV guide
                    # (ch04): that cache is ONLY consumed by the ch04 guide preview
                    # (see Part 22), and clearing it forces the preview to stop()
                    # and re-buffer the exact same file on the next frame — the
                    # flicker/re-tune-looking blip when closing a menu over the
                    # guide. It's a harmless no-op on every other channel, so only
                    # skip it here on ch04.
                    if app_state.get("current_channel") != "04":
                        app_state["loaded_preview_path_cache"] = ""
                    app_state["show_menu"] = False
                    app_state["menu_shown_at"] = 0
                    print("[SETTINGS ENGINE] Overlay menu dismissed via ESCAPE.")
                continue

            # ESC global handler: swallow when a game is loading or running (real
            # exit is Start+Select held 0.6 s via the XInput watcher thread), or
            # let the quit-confirm dialog open on all other channels/modes.
            # NOTE: DVD_PLAYER + ESC is already consumed earlier by the ch03
            # tunneling valve's DVD-scoped keyboard remap clause — it never
            # reaches here.
            if event.key == pygame.K_ESCAPE and not app_state.get("show_menu", False):
                if app_state.get("current_channel") == "03" and 'game_deck' in globals() and game_deck is not None:
                    # Swallow ESC for the entire GAME lifecycle — both during the
                    # launch/loading transition AND during active gameplay.
                    # • Loading: emulator_loading=True — this is a one-way door;
                    #   the minimize dialog must not open mid-launch. Channel-change
                    #   is the user's escape hatch if they change their mind.
                    # • Running: real exit is Start+Select (XInput watcher thread).
                    # DVD_PLAYER ESC is consumed upstream by the tunneling valve and
                    # handled by _handle_dvd_input, so it never reaches this guard.
                    if getattr(game_deck, "mode", "") == "GAME":
                        continue

                # Otherwise, use the standard system minimize menu -- but not
                # when this session is actually running as the OS shell
                # (shell_takeover_active). There's no desktop to minimize to
                # in that case, so ESC on the TV stations/guide/game channel
                # is simply swallowed instead of showing the minimize screen.
                if not app_state.get("shell_takeover_active", False):
                    app_state["show_quit_confirm"] = True
                    app_state["quit_confirm_shown_at"] = now_ticks
                continue

            if event.key == pygame.K_m:
                if app_state.get("show_menu", False):
                    # See ESC-close note: don't clear the guide-preview cache on
                    # ch04, it just forces a needless preview re-buffer/flicker.
                    if app_state.get("current_channel") != "04":
                        app_state["loaded_preview_path_cache"] = ""
                    app_state["show_menu"] = False
                    app_state["menu_shown_at"] = 0
                else:
                    if app_state.get("show_splash", False):
                        app_state["show_splash"] = False  # Close controls menu if open
                    app_state["show_menu"] = True
                    app_state["menu_shown_at"] = now_ticks
                    app_state["active_menu_tab"] = "System"
                    app_state["menu_layer"] = "TAB_SELECTION"
                    # Preload the Games tab's console logos right now, while
                    # the user is still looking at the System tab, instead
                    # of on the first frame they actually tab over to Games
                    # -- that first-navigation decode-from-disk hitch was
                    # the "split sec lag" getting into the Games tab.
                    if db is not None and db.config.get("game_channel_enabled", False):
                        preload_games_tab_assets(app_state)
                    print("[SETTINGS ENGINE] Main overlay menu initialized.")
                continue

# ==============================================================================
# PART 9 OF 28: TV GUIDE PROPERTY KEY CAPTURES & VERTICAL ROW SELECTION
# ==============================================================================

            # --- INTERACTIVE TV GUIDE WORKSPACE CONTROLLER LAYER ---
            if app_state["current_channel"] == "04" and not app_state["show_menu"] and not app_state["show_quit_confirm"] and not app_state["show_splash"]:
                allowed_guide = []
                for i in range(5, 45):
                    if db.channels_db.get(str(i).zfill(2), {}).get("active", True):
                        allowed_guide.append(str(i).zfill(2))
                        
                curr_g_str = str(app_state["selected_guide_channel"]).zfill(2)
                try: g_pos = allowed_guide.index(curr_g_str)
                except ValueError: g_pos = 0
                
                row_offset = app_state.get("guide_row_offset", 0)
                now_ticks = pygame.time.get_ticks()
                
                if event.key in [pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d, pygame.K_RETURN, pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT]:
                    app_state["guide_inactivity_timer"] = now_ticks + 60000

                # RETURN/ENTER KEY DETECTOR: Tunes the emulator instantly to the active highlighted channel row!
                if event.key == pygame.K_RETURN:
                    target_tuning_channel = str(app_state["selected_guide_channel"]).zfill(2)
                    print(f"[EPG TUNER] Activation dispatch caught. Tuning to selected row channel: {target_tuning_channel}")
                    change_channel(target_tuning_channel, is_surfing=True)
                    continue

                # VERTICAL SCROLL: W KEY ONLY (wraps around: top row -> bottom row).
                # Arrow keys are intentionally NOT bound here so they keep their normal
                # channel-up / channel-down / volume function inside the guide.
                elif event.key == pygame.K_w:
                    if allowed_guide:
                        if g_pos > 0:
                            next_pos = g_pos - 1
                        else:
                            next_pos = len(allowed_guide) - 1  # CYCLE: past the top wraps to the bottom
                        app_state["selected_guide_channel"] = int(allowed_guide[next_pos])
                        app_state["highlighted_guide_channel"] = allowed_guide[next_pos]
                        _clear_last_video_frame()  # new highlight = new preview source; don't bridge with the old channel's frame
                        _arm_guide_preview_transition()  # static-mode: burst of static in the preview box; black-mode: no-op
                        # Keep the highlighted row inside the visible row window (8 rows on
                        # 16:9, 5 rows on 4:3 — see get_guide_visible_rows)
                        _guide_rows = get_guide_visible_rows(db)
                        if next_pos < row_offset:
                            app_state["guide_row_offset"] = next_pos
                        elif next_pos >= row_offset + _guide_rows:
                            app_state["guide_row_offset"] = max(0, next_pos - (_guide_rows - 1))
                    continue
                    
                # VERTICAL SCROLL: S KEY ONLY (wraps around: bottom row -> top row).
                # Arrow keys stay free for channel/volume control.
                elif event.key == pygame.K_s:
                    if allowed_guide:
                        if g_pos < len(allowed_guide) - 1:
                            next_pos = g_pos + 1
                        else:
                            next_pos = 0  # CYCLE: past the bottom wraps to the top
                        app_state["selected_guide_channel"] = int(allowed_guide[next_pos])
                        app_state["highlighted_guide_channel"] = allowed_guide[next_pos]
                        _clear_last_video_frame()  # new highlight = new preview source; don't bridge with the old channel's frame
                        _arm_guide_preview_transition()  # static-mode: burst of static in the preview box; black-mode: no-op
                        # Keep the highlighted row inside the visible row window (8 rows on
                        # 16:9, 5 rows on 4:3 — see get_guide_visible_rows)
                        _guide_rows = get_guide_visible_rows(db)
                        if next_pos >= row_offset + _guide_rows:
                            app_state["guide_row_offset"] = next_pos - (_guide_rows - 1)
                        elif next_pos < row_offset:
                            app_state["guide_row_offset"] = next_pos
                    continue

# ==============================================================================
# PART 10 OF 28: HORIZONTAL CONTROLLERS & STATIC EDGE BOUNDARY PINNING (LEFT)
# ==============================================================================

                # HORIZONTAL LOCK: A KEY ONLY (One-Click Jump to the previous SHOW).
                # Arrow keys stay free for channel/volume control.
                elif event.key == pygame.K_a:
                    _guide_jump_col(-1)
                    continue

# ==============================================================================
# PART 11 OF 28: HORIZONTAL CONTROLLERS & STATIC EDGE BOUNDARY PINNING (RIGHT)
# ==============================================================================

                # D KEY ONLY: One-Click Jump Forward to the next SHOW.
                # Arrow keys stay free for channel/volume control.
                elif event.key == pygame.K_d:
                    _guide_jump_col(1)
                    continue


# ==============================================================================
# PART 12 OF 28: HARDWARE VOLUME MIXER CONTROLS & SURFING STEP SHIFTERS
# ==============================================================================

            # --- HARD LOCK BYPASS GATE ---
            if app_state.get("show_menu", False):
                pass
            else:
                # Direct number dial buffer key intercepts.
                # Supports BOTH the top-row number keys and the numpad (K_KP0..K_KP9).
                numpad_digit_map = {
                    pygame.K_KP0: "0", pygame.K_KP1: "1", pygame.K_KP2: "2",
                    pygame.K_KP3: "3", pygame.K_KP4: "4", pygame.K_KP5: "5",
                    pygame.K_KP6: "6", pygame.K_KP7: "7", pygame.K_KP8: "8",
                    pygame.K_KP9: "9",
                }
                pressed_digit = None
                if event.key in [pygame.K_0, pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5, pygame.K_6, pygame.K_7, pygame.K_8, pygame.K_9]:
                    pressed_digit = chr(event.key)
                elif event.key in numpad_digit_map:
                    pressed_digit = numpad_digit_map[event.key]

                if pressed_digit is not None:
                    app_state["digit_buffer"] = app_state.get("digit_buffer", "") + pressed_digit
                    app_state["digit_timer"] = now_ticks + 2500  
                    app_state["osd_channel_timer"] = now_ticks + 2500
                    
                    if len(app_state["digit_buffer"]) == 2:
                        final_ch = app_state["digit_buffer"]
                        app_state["digit_buffer"] = ""
                        app_state["digit_timer"] = 0
                        
                        valid_set = []
                        if db is not None:
                            # FIXED: Look at live config to allow number dialing to Channel 03 instantly
                            if db.config.get("game_channel_enabled", False): valid_set.append("03")
                            if db.config.get("tv_guide_enabled", True): valid_set.append("04")
                            for i in range(5, 45):
                                ch_key_str = str(i).zfill(2)
                                if db.channels_db.get(ch_key_str, {}).get("active", False):
                                    valid_set.append(ch_key_str)
                        else:
                            valid_set = ["04", "05", "06", "07"]
                            
                        if final_ch in valid_set:
                            change_channel(final_ch)
                    continue

                if event.key == pygame.K_n:
                    db.config["is_muted"] = not db.config.get("is_muted", False)
                    app_state["osd_volume_timer"] = now_ticks + 2500
                    if game_deck is not None:
                        game_deck.dvd_osd_msg = ""; game_deck.dvd_osd_until = 0
                    
                    safe_mute_flag = db.config["is_muted"]
                    current_vol = 0 if safe_mute_flag else int(db.config.get("global_volume", 70))
                    target_gain = _perceptual_volume_pct(current_vol) / 100.0
                    # Music/visualizer channels stack MUSIC_GAIN on top of the curve
                    # (to compensate for hotter-than-TV raw source material), so they
                    # use the shallower STACKED_GAIN_MIN_DB curve -- see its docstring.
                    music_target_gain = _perceptual_volume_pct(current_vol, min_db=STACKED_GAIN_MIN_DB) / 100.0
                    
                    if vlc_engine is not None: 
                        vlc_engine.set_volume(current_vol, safe_mute_flag)
                    _set_dvd_volume(int(db.config.get("global_volume", 70)), safe_mute_flag)
                    if static_audio is not None: 
                        static_audio.set_volume(target_gain)
                    pygame.mixer.music.set_volume(music_target_gain * MUSIC_GAIN * app_state.get("active_music_gain", 1.0))
                    pygame.mixer.Channel(1).set_volume(music_target_gain * MUSIC_GAIN * app_state.get("active_music_gain", 1.0))
                    if game_deck is not None and hasattr(game_deck, "set_libretro_volume"):
                        game_deck.set_libretro_volume(
                            db.config.get("global_volume", 70), db.config.get("is_muted", False))
                    if game_deck is not None and hasattr(game_deck, "set_audio_volume"):
                        game_deck.set_audio_volume(
                            db.config.get("global_volume", 70), db.config.get("is_muted", False))
                    _set_ch03_menu_music_volume(db.config.get("global_volume", 70), safe_mute_flag)
                    
                    _mark_settings_dirty()
                    continue

                elif event.key in [pygame.K_RIGHT, pygame.K_LEFT]:
                    if event.key == pygame.K_RIGHT:
                        db.config["global_volume"] = min(100, db.config.get("global_volume", 70) + 5)
                    else:
                        db.config["global_volume"] = max(0, db.config.get("global_volume", 70) - 5)
                        
                    app_state["osd_volume_timer"] = now_ticks + 2500
                    if game_deck is not None:
                        game_deck.dvd_osd_msg = ""; game_deck.dvd_osd_until = 0
                    db.config["is_muted"] = False 
                    
                    target_gain = _perceptual_volume_pct(db.config["global_volume"]) / 100.0
                    music_target_gain = _perceptual_volume_pct(db.config["global_volume"], min_db=STACKED_GAIN_MIN_DB) / 100.0
                    
                    if vlc_engine is not None: 
                        vlc_engine.set_volume(int(db.config["global_volume"]), False)
                    _set_dvd_volume(int(db.config["global_volume"]), False)
                    if static_audio is not None: 
                        static_audio.set_volume(target_gain)
                    pygame.mixer.music.set_volume(music_target_gain * MUSIC_GAIN * app_state.get("active_music_gain", 1.0))
                    pygame.mixer.Channel(1).set_volume(music_target_gain * MUSIC_GAIN * app_state.get("active_music_gain", 1.0))
                    if game_deck is not None and hasattr(game_deck, "set_libretro_volume"):
                        game_deck.set_libretro_volume(
                            db.config.get("global_volume", 70), db.config.get("is_muted", False))
                    if game_deck is not None and hasattr(game_deck, "set_audio_volume"):
                        game_deck.set_audio_volume(
                            db.config.get("global_volume", 70), db.config.get("is_muted", False))
                    _set_ch03_menu_music_volume(db.config["global_volume"], db.config.get("is_muted", False))
                    
                    _mark_settings_dirty()
                    continue

                elif event.key == pygame.K_UP:
                    # Gather the dynamic active channel pool list
                    allowed_channels = []
                    # FIXED: Direct live setting check inserts Channel 03 into the surfing loop immediately when ON
                    if db is not None and db.config.get("game_channel_enabled", False):
                        allowed_channels.append("03")
                    if db is not None and db.config.get("tv_guide_enabled", True):
                        allowed_channels.append("04")
                    for i in range(5, 45):
                        ch_key = str(i).zfill(2)
                        if db.channels_db.get(ch_key, {}).get("active", False):
                            allowed_channels.append(ch_key)
                            
                    current_lookup = str(app_state["current_channel"]).zfill(2)
                    if current_lookup not in allowed_channels:
                        current_lookup = allowed_channels[0] if allowed_channels else "05"
                    
                    if allowed_channels:
                        curr_pos = allowed_channels.index(current_lookup)
                        target_ch = allowed_channels[(curr_pos + 1) % len(allowed_channels)]
                        
                        target_track_state = calculate_slotted_playback_state(target_ch)
                        if target_track_state.get("mode") == "Visualizer":
                            if pygame.mixer.music.get_busy():
                                pygame.mixer.music.stop()
                                pygame.mixer.music.unload()
                            app_state["active_music_track_file"] = target_track_state.get("file", "")
                            
                        change_channel(target_ch)
                    continue
                    
                elif event.key == pygame.K_DOWN:
                    # Gather the dynamic active channel pool list
                    allowed_channels = []
                    # FIXED: Direct live setting check inserts Channel 03 into the surfing loop immediately when ON
                    if db is not None and db.config.get("game_channel_enabled", False):
                        allowed_channels.append("03")
                    if db is not None and db.config.get("tv_guide_enabled", True):
                        allowed_channels.append("04")
                    for i in range(5, 45):
                        ch_key = str(i).zfill(2)
                        if db.channels_db.get(ch_key, {}).get("active", False):
                            allowed_channels.append(ch_key)
                            
                    current_lookup = str(app_state["current_channel"]).zfill(2)
                    if current_lookup not in allowed_channels:
                        current_lookup = allowed_channels[0] if allowed_channels else "05"
                    
                    if allowed_channels:
                        curr_pos = allowed_channels.index(current_lookup)
                        target_ch = allowed_channels[(curr_pos - 1) % len(allowed_channels)]
                        
                        target_track_state = calculate_slotted_playback_state(target_ch)
                        if target_track_state.get("mode") == "Visualizer":
                            if pygame.mixer.music.get_busy():
                                pygame.mixer.music.stop()
                                pygame.mixer.music.unload()
                            app_state["active_music_track_file"] = target_track_state.get("file", "")
                            
                        change_channel(target_ch)
                    continue

# ==============================================================================
# PART 13 OF 28: MENU NAVIGATION INDEX CONTROLLERS & TAB SWITCHERS
# ==============================================================================

            if app_state.get("show_menu", False):
                # LOCKED INDEXES: Clean 4-tab pool matching your visible screen headings
                game_ch_on = db.config.get("game_channel_enabled", False) if db is not None else False
                kiosk_on = db.config.get("kiosk_mode_enabled", False) if db is not None else False
                tabs_pool = ["System", "Channels", "Games", "Video", "Theme"] if game_ch_on else ["System", "Channels", "Video", "Theme"]
                if kiosk_on:
                    # Kiosk Mode locks kids out of re-scheduling/editing
                    # channels, so the Channels tab itself disappears from
                    # the bar entirely.
                    tabs_pool = [t for t in tabs_pool if t != "Channels"]
                if not game_ch_on and app_state.get("active_menu_tab") == "Games":
                    app_state["active_menu_tab"] = "System"
                    app_state["menu_layer"] = "TAB_SELECTION"
                if kiosk_on and app_state.get("active_menu_tab") == "Channels":
                    app_state["active_menu_tab"] = "System"
                    app_state["menu_layer"] = "TAB_SELECTION"

                # --- MENU LAYER 1: TOP TAB BAR FOCUS NAVIGATION ---
                if app_state["menu_layer"] == "TAB_SELECTION":
                    try:
                        current_tab_idx = tabs_pool.index(app_state.get("active_menu_tab", "System"))
                    except ValueError:
                        current_tab_idx = 0

                    if event.key == pygame.K_d:
                        next_idx = (current_tab_idx + 1) % len(tabs_pool)
                        app_state["active_menu_tab"] = tabs_pool[next_idx]
                        print(f"[MENU NAVIGATION] Swapped tab focus right to: {app_state['active_menu_tab']}")
                        continue
                    elif event.key == pygame.K_a:
                        next_idx = (current_tab_idx - 1) % len(tabs_pool)
                        app_state["active_menu_tab"] = tabs_pool[next_idx]
                        print(f"[MENU NAVIGATION] Swapped tab focus left to: {app_state['active_menu_tab']}")
                        continue
                    elif event.key == pygame.K_s:
                        # Drop down directly into the matching layout layers
                        tab_layer_mapping = {
                            "System": "SYSTEM_ROWS",
                            "Channels": "CHANNEL_LIST",
                            "Games": "GAMES_LIST",
                            "Video": "VIDEO_ROWS",
                            "Theme": "THEME_ROWS"
                        }
                        target_layer = tab_layer_mapping.get(app_state["active_menu_tab"], "SYSTEM_ROWS")
                        app_state["menu_layer"] = target_layer
                        app_state["menu_selection_index"] = 0
                        print(f"[MENU NAVIGATION] Dropped cursor down into sub-panel layer: {target_layer}")
                        continue

                # --- MENU LAYER 3: TAB 2 CHANNELS LIST GRID SELECTION (8 ROWS x 5 COLUMNS) ---
                elif app_state["menu_layer"] == "CHANNEL_LIST":
                    cur_sel = app_state.get("menu_selection_index", 0)
                    # --- 4:3 TEST MODE + BORDER footer row (bottom-left of the
                    # Channels tab) ---
                    # cur_sel 40 = "4:3 Test Mode" toggle, sitting directly below
                    # grid column 0. cur_sel 41 = "Border" toggle, only visible/
                    # reachable to its right while 4:3 Test Mode is on. These are
                    # single-column, Enter-only toggles (see the Enter-only
                    # standing rule elsewhere in this menu) -- A/D between them
                    # is the one exception, since Border genuinely sits to the
                    # right of 4:3 Test the same way paired toggle/name columns
                    # do elsewhere in this menu.
                    _43_test_on = db.config.get("fake_43_test_mode_enabled", False) if db else False
                    # The 4:3 Test Mode / Border footer row is a 16:9-only cheat:
                    # a display Windows already reports as 4:3 has no reason to
                    # fake 4:3 or bezel itself, so on a true 4:3 display the row
                    # isn't drawn (see ui.py) and must not be reachable either.
                    _true_43_display = (getattr(db, "_detected_aspect_ratio", None) == "4:3") if db else False

                    if event.key == pygame.K_w:
                        # Going up past the top row (05, 06, 07, 08, 09) takes you back to the tab bar
                        if cur_sel in (40, 41):
                            app_state["menu_selection_index"] = 35 if cur_sel == 40 else 36
                        elif cur_sel < 5:
                            app_state["menu_layer"] = "TAB_SELECTION"
                            app_state["menu_selection_index"] = 1  # Channels Tab Index
                        else:
                            app_state["menu_selection_index"] = cur_sel - 5
                        continue
                        
                    elif event.key == pygame.K_s:
                        # Go down once by jumping 5 slots. The bottom channel row
                        # (35-39) now drops into the 4:3 Test Mode footer row
                        # instead of stopping dead.
                        if cur_sel in (40, 41):
                            pass
                        elif 35 <= cur_sel < 40:
                            # Bottom channel row drops into the footer toggle row
                            # only on 16:9 displays; on a true 4:3 display the
                            # footer doesn't exist, so stay put.
                            if not _true_43_display:
                                app_state["menu_selection_index"] = 40
                        elif cur_sel + 5 < 40:
                            app_state["menu_selection_index"] = cur_sel + 5
                        continue
                        
                    elif event.key == pygame.K_a:
                        if cur_sel == 41:
                            app_state["menu_selection_index"] = 40
                        elif cur_sel == 40:
                            pass
                        elif cur_sel % 5 != 0:
                            app_state["menu_selection_index"] = cur_sel - 1
                        continue
                        
                    elif event.key == pygame.K_d:
                        if cur_sel == 40:
                            if _43_test_on:
                                app_state["menu_selection_index"] = 41
                        elif cur_sel == 41:
                            pass
                        elif cur_sel % 5 != 4:
                            app_state["menu_selection_index"] = cur_sel + 1
                        continue
                        
                    elif event.key == pygame.K_RETURN:
                        if cur_sel == 40:
                            # Toggle 4:3 Test Mode. This drives the SAME
                            # "aspect_ratio" config value every other part of the
                            # app already reads (VLC decode aspect, the
                            # letterbox black-bar render, console border
                            # availability) -- see get_effective note at
                            # _draw_full_tv_border_overlay -- so flipping it
                            # "tricks" the whole program the same way a real 4:3
                            # display would, without touching Windows itself.
                            # Turning it off restores the REAL boot-detected
                            # ratio (DETECTED_ASPECT_RATIO), not a hardcoded
                            # "16:9", so this behaves correctly even for someone
                            # whose display genuinely is 4:3.
                            _new_43 = not db.config.get("fake_43_test_mode_enabled", False)
                            db.config["fake_43_test_mode_enabled"] = _new_43
                            db.config["aspect_ratio"] = "4:3" if _new_43 else DETECTED_ASPECT_RATIO
                            db.save_settings()
                            print(f"[SETTINGS] 4:3 Test Mode = {_new_43} (aspect_ratio -> {db.config['aspect_ratio']})")
                        elif cur_sel == 41:
                            if db.config.get("fake_43_test_mode_enabled", False):
                                db.config["fake_43_border_enabled"] = not db.config.get("fake_43_border_enabled", True)
                                db.save_settings()
                                print(f"[SETTINGS] 4:3 Border = {db.config['fake_43_border_enabled']}")
                        else:
                            # Translate zero-based grid slot safely into channel database row values (05 to 44)
                            selected_ch_num = cur_sel + 5
                            app_state["selected_channel_row"] = selected_ch_num
                            app_state["sub_menu_row_index"] = 0

                            # If this is the last active channel its Status toggle is locked —
                            # land on the Vis toggle (col 1) so the cursor isn't stuck on a greyed item.
                            _ch_str_entry = str(selected_ch_num).zfill(2)
                            _ch_active_entry = db.channels_db.get(_ch_str_entry, {}).get("active", True)
                            _active_count_entry = sum(
                                1 for _n in range(5, 45)
                                if db.channels_db.get(str(_n).zfill(2), {}).get("active", str(_n).zfill(2) == "05")
                            )
                            _is_last = _ch_active_entry and (_active_count_entry <= 1)
                            app_state["sub_menu_col_index"] = 1 if _is_last else 0

                            app_state["menu_layer"] = "CHANNEL_SUB_MENU"
                            print(f"[MENU NAVIGATION] Selected Channel {str(selected_ch_num).zfill(2)}. Opening sub-menu details window.")
                        continue


# ==============================================================================
# PART 14 OF 28: OVERLAY MENU KEYBOARD INTERCEPT CONTROLLERS
# ==============================================================================

                # --- MENU LAYER 2: TAB 1 SYSTEM OPTION SETTINGS ---
                elif app_state["menu_layer"] == "SYSTEM_ROWS":
                    cur_row = app_state.get("menu_selection_index", 0)
                    # sys_rows is the single source of truth (see
                    # get_system_menu_rows in ui.py) for which rows exist
                    # right now and in what order -- Kiosk Mode (and, within
                    # Kiosk Mode, whether Boot on Start is also on) collapses
                    # this down to just 2-3 rows instead of the full list.
                    # "close" is always the row right after the last entry
                    # in sys_rows, since it's pinned to the bottom-right
                    # corner rather than flowing with the other rows.
                    sys_rows = get_system_menu_rows(db, app_state)
                    sys_rows_close_idx = len(sys_rows)
                    row_id = sys_rows[cur_row] if cur_row < len(sys_rows) else "close"

                    if event.key == pygame.K_w:
                        if cur_row == 0:
                            app_state["menu_layer"] = "TAB_SELECTION"
                        else:
                            app_state["menu_selection_index"] = cur_row - 1
                        continue
                    elif event.key == pygame.K_s:
                        if cur_row < sys_rows_close_idx:
                            app_state["menu_selection_index"] = cur_row + 1
                        continue
                    elif event.key == pygame.K_RETURN:
                        # NOTE: A/D used to also flip these toggles, but that
                        # collided with A/D being needed for pure left/right
                        # navigation elsewhere — toggles are Enter-only now.
                        is_enter = True

                        if row_id == "game_ch":  # Channel 03 (Games) toggle
                            db.config["game_channel_enabled"] = not db.config.get("game_channel_enabled", False)
                            db.save_settings()
                            print(f"[SETTINGS] Channel 03 (Games) flipped to: {db.config['game_channel_enabled']}")
                            # Turned OFF while watching Channel 03 — surf up to next active channel
                            if not db.config.get("game_channel_enabled", False):
                                _surf_up_from_disabled("03")
                        elif row_id == "guide":  # Channel 04 (TV Guide) toggle, default ON
                            db.config["tv_guide_enabled"] = not db.config.get("tv_guide_enabled", True)
                            db.save_settings()
                            print(f"[SETTINGS] Channel 04 (TV Guide) flipped to: {db.config['tv_guide_enabled']}")
                            # Turned OFF while watching Channel 04 — surf up to next active channel
                            if not db.config.get("tv_guide_enabled", True):
                                _surf_up_from_disabled("04")
                        elif row_id == "controls_on_start":  # Controls Menu on Start toggle
                            db.config["show_controls_on_launch"] = not db.config.get("show_controls_on_launch", True)
                            db.save_settings()
                            print(f"[SETTINGS] Controls Menu on Start = {db.config['show_controls_on_launch']}")
                        elif row_id == "boot_on_start":  # Boot on Start toggle, default OFF
                            _new_boot_val = not db.config.get("start_on_boot", False)
                            if _set_start_on_boot(_new_boot_val):
                                db.config["start_on_boot"] = _new_boot_val
                                db.save_settings()
                                print(f"[SETTINGS] Boot on Start = {_new_boot_val}")
                        elif row_id == "kiosk_mode":  # Kiosk Mode toggle, default OFF
                            db.config["kiosk_mode_enabled"] = not db.config.get("kiosk_mode_enabled", False)
                            db.save_settings()
                            print(f"[SETTINGS] Kiosk Mode = {db.config['kiosk_mode_enabled']}")
                            # Turning Kiosk Mode ON should take effect immediately, not
                            # just on next boot -- if the app is currently windowed,
                            # force it into fullscreen right now. Reuses the exact same
                            # NOFRAME-restore sequence used by the minimize-restore path
                            # above (native size/pos, not the heavier pygame.FULLSCREEN
                            # display-mode switch) so this doesn't reintroduce the
                            # lag/clipping that approach was replaced to fix.
                            if db.config["kiosk_mode_enabled"] and not app_state.get("is_fullscreen", False):
                                WINDOW_WIDTH, WINDOW_HEIGHT = NATIVE_WIDTH, NATIVE_HEIGHT
                                screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.NOFRAME)
                                app_state["is_fullscreen"] = True
                                # Same clean borderless-fullscreen settle as the
                                # minimize-restore path (shares the identical frame
                                # flip RESIZABLE -> NOFRAME), so enabling Kiosk while
                                # windowed doesn't flash black or clip edges either.
                                _finalize_window_mode(
                                    screen, windowed=False,
                                    client_w=WINDOW_WIDTH, client_h=WINDOW_HEIGHT,
                                    pos_x=0, pos_y=0,
                                )
                            # Flipping this reshapes sys_rows entirely (down to
                            # 2-3 rows, or back up to the full list) — re-sync
                            # the cursor onto Kiosk Mode's new position rather
                            # than leaving it pointed at a numeric index that
                            # may now be a completely different row, or past
                            # the end of a much shorter list.
                            _new_sys_rows = get_system_menu_rows(db, app_state)
                            app_state["menu_selection_index"] = _new_sys_rows.index("kiosk_mode")
                        elif is_enter and row_id == "remote_remap":  # opens the combined remap sub-menu
                            app_state["menu_layer"] = "REMOTE_REMAP_LIST"
                            app_state["menu_selection_index"] = 0
                            app_state["remote_remap_index"] = 0
                        elif is_enter and row_id == "reset_settings":
                            db.factory_reset()
                            # Persist the reset immediately — previous code never saved,
                            # so a crash right after the reset would silently undo it.
                            db.save_settings()
                            # Reset also switches start_on_boot (and Kiosk
                            # Mode) back to their off defaults -- see
                            # RetroDatabase.factory_reset, neither is in the
                            # preserved-flags list -- so make the actual
                            # Windows registry entry match rather than leaving
                            # a stale startup entry behind after a reset.
                            _set_start_on_boot(False)

                            # ── HARD MEDIA TEARDOWN ──────────────────────────────────
                            # Kill every audio/video layer unconditionally.
                            # The old code gated vlc_engine.stop() on is_playing(),
                            # which misses Paused / Buffering states — the most common
                            # sources of "I can still hear the show after clearing."
                            try:
                                if vlc_engine is not None:
                                    vlc_engine.stop()
                            except Exception as e:
                                log.warning("Reset Settings: vlc_engine.stop() failed: %s", e)
                            if 'static_audio' in globals() and static_audio is not None:
                                try: static_audio.stop()
                                except Exception as e: log.warning("Reset Settings: static_audio.stop() failed: %s", e)
                            try:
                                pygame.mixer.Channel(1).stop()
                            except Exception as e:
                                log.warning("Reset Settings: mixer Channel(1).stop() failed: %s", e)
                            try:
                                if pygame.mixer.music.get_busy():
                                    pygame.mixer.music.stop()
                                pygame.mixer.music.unload()
                            except Exception as e:
                                log.warning("Reset Settings: pygame.mixer.music stop/unload failed: %s", e)

                            # Drop the last decoded frame so the old video can't
                            # paper over the static on the next render frame.
                            _clear_last_video_frame()

                            # Clear all playback / schedule state that survives
                            # across channel switches — stale caches here caused
                            # calculate_slotted_playback_state() to serve old file
                            # paths even after the schedules were wiped.
                            app_state["is_playing_video"]            = False
                            app_state["active_music_track_file"]     = ""
                            app_state["last_scheduled_vis_track"]    = {}
                            app_state["persistent_track_pool_cache"] = ""
                            app_state["loaded_preview_path_cache"]   = ""
                            app_state["last_vlc_retry_at"]           = 0
                            app_state["last_video_play_started_at"]  = 0
                            # Per-channel schedule caches live inside channels_db
                            # entries — flush them so calculate_slotted_playback_state
                            # re-derives from the now-empty schedule lists on first call.
                            if db is not None:
                                for _rc in list(db.channels_db.keys()):
                                    _rc_info = db.channels_db[_rc]
                                    _rc_info.pop("sched_cached_tracks",    None)
                                    _rc_info.pop("marathon_anchor_epoch",  None)
                                    _rc_info.pop("playback_log",           None)

                            # Re-apply volume from reset defaults
                            try:
                                if vlc_engine is not None:
                                    vlc_engine.set_volume(db.config.get("global_volume", 70), db.config.get("is_muted", False))
                            except Exception as vol_err:
                                print(f"[SETTINGS] Could not reapply volume after reset: {vol_err}")

                            # Return to first-launch state: controls splash on
                            # channel 04 (TV guide), same as a cold boot with an
                            # empty config.  Previous code went to channel 05 with
                            # no splash — this now matches what the user actually
                            # sees when they first open the app with no media loaded.
                            app_state["show_menu"]            = False
                            app_state["menu_shown_at"]        = 0
                            app_state["active_menu_tab"]      = "System"
                            app_state["menu_layer"]           = "TAB_SELECTION"
                            app_state["menu_selection_index"] = 0
                            app_state["show_splash"]          = db.config.get("show_controls_on_launch", True)
                            change_channel("04")
                            print("[SETTINGS] Full media clear complete — returned to first-launch state.")
                        elif is_enter and row_id == "exit_program":
                            if vlc_engine is not None:
                                try: vlc_engine.stop()
                                except Exception as e: print(f"[EXIT] vlc stop failed: {e}")
                            if dvd_engine is not None:
                                try: dvd_engine.stop()
                                except Exception as e: print(f"[EXIT] dvd engine stop failed: {e}")
                            if game_deck is not None and hasattr(game_deck, "shutdown_deck"):
                                game_deck.shutdown_deck()
                            if db is not None:
                                db.config["last_channel"] = app_state.get("current_channel", "05")
                                try: db.save_settings()
                                except Exception as e: print(f"[EXIT] save_settings failed: {e}")
                            if db is not None and db.config.get("start_on_boot", False):
                                _launch_explorer_fallback()
                            pygame.quit()
                            sys.exit()
                        elif is_enter and row_id == "poweroff":
                            # Turn Off Computer — always a real shutdown. The
                            # CRT-off animation plays first, hiding the
                            # shutdown process. (The remappable sleep hotkey
                            # that used to live on this row has been removed.)
                            _trigger_crt_off("shutdown")
                        elif is_enter and cur_row == sys_rows_close_idx:  # Close button — close the menu
                            # See ESC-close note: don't clear the guide-preview
                            # cache on ch04 (forces a needless preview re-buffer).
                            if app_state.get("current_channel") != "04":
                                app_state["loaded_preview_path_cache"] = ""
                            app_state["show_menu"] = False
                            app_state["menu_shown_at"] = 0
                        pygame.event.clear(pygame.KEYDOWN)
                        continue


                # --- MENU LAYER: REMOTE + NUMBER REMAPPING COMBINED SUB-MENU ---
                # Both control lists are shown side by side at once. Three
                # focusable items on the bottom row: Remap Remote (0),
                # Remap Numbers (1), Close (2). The lists above are
                # informational only (they're not navigable rows).
                elif app_state["menu_layer"] == "REMOTE_REMAP_LIST":
                    rr_sel = app_state.get("menu_selection_index", 0)

                    if event.key in (pygame.K_a, pygame.K_w):
                        app_state["menu_selection_index"] = (rr_sel - 1) % 3
                        continue
                    elif event.key in (pygame.K_d, pygame.K_s):
                        app_state["menu_selection_index"] = (rr_sel + 1) % 3
                        continue
                    elif event.key == pygame.K_RETURN:
                        if rr_sel == 0:  # Remap Remote — start the sequential capture flow
                            app_state["menu_layer"] = "REMOTE_REMAP_CAPTURE"
                            app_state["remote_remap_index"] = 0
                            app_state["_remap_used_keys"]   = []
                            app_state["_remap_reject_flash"] = None
                            app_state["_remap_last_accept"]  = None
                            print("[REMOTE REMAP] Sequence started.")
                        elif rr_sel == 1:  # Remap Numbers — start the sequential capture flow
                            app_state["menu_layer"] = "NUMBER_REMAP_CAPTURE"
                            app_state["remote_remap_index"] = 0
                            app_state["_remap_used_keys"]   = []
                            app_state["_remap_reject_flash"] = None
                            app_state["_remap_last_accept"]  = None
                            print("[NUMBER REMAP] Sequence started.")
                        else:  # Close — back to the System tab row list
                            app_state["menu_layer"] = "SYSTEM_ROWS"
                            _sys_rows_now = get_system_menu_rows(db, app_state)
                            app_state["menu_selection_index"] = (
                                _sys_rows_now.index("remote_remap") if "remote_remap" in _sys_rows_now else 0
                            )
                        pygame.event.clear(pygame.KEYDOWN)
                        continue



                # --- MENU LAYER 4: TAB 2 CHANNELS DATA SCHEDULE CONTENT EDITOR MATRIX ---
                elif app_state["menu_layer"] == "CHANNEL_SUB_MENU":
                    sub_row = app_state.get("sub_menu_row_index", 0) 
                    sub_col = app_state.get("sub_menu_col_index", 0) 
                    target_ch_str = str(app_state["selected_channel_row"]).zfill(2)
                    ch_info = db.channels_db.get(target_ch_str, {"active": True, "is_visualizer": False, "name": f"STATION {target_ch_str}"})
                    
                    is_music_mode = ch_info.get("is_visualizer", False)
                    _sched_is_marathon = (not is_music_mode) and (ch_info.get("scheduling_mode", "random_slots") == "marathon")
                    _is_full_day = (not _sched_is_marathon) and (ch_info.get("block_mode", "full_day") == "full_day")
                    if not ch_info.get("active", True):
                        max_close_row = 1          # inactive: only row 0 + Close button (row 1)
                    elif is_music_mode:
                        max_close_row = 4
                    else:
                        # Rows: 0 header, 1 sched, 2 pair, 3 commercials,
                        # [4 episode order, 5 block length — block modes only,
                        # hidden in marathon], folder slots, holiday, clear,
                        # audio track, close.
                        # Marathon has no per-episode "next up" pick (it just plays
                        # the whole folder straight through) and is already
                        # inherently 24-hour, so neither extra row exists there —
                        # every row after Commercials stays at its original number,
                        # and only the three block modes gain the two extra rows.
                        max_close_row = 8 if _sched_is_marathon else 10
                    # Row numbers for the rows below Commercials, dynamic on the same basis.
                    _order_row     = None if _sched_is_marathon else 4
                    _block_len_row = None if _sched_is_marathon else 5
                    _folder_row    = 4 if _sched_is_marathon else 6
                    _holiday_row   = 5 if _sched_is_marathon else 7
                    # "Change Current Playing Audio Track" — lets the viewer fix a
                    # show that's defaulting to the wrong embedded audio track
                    # (e.g. a foreign dub) without waiting for it to cycle around
                    # on its own. Now sits directly under Holiday (ABOVE Clear All),
                    # so Clear takes the next row and Close stays last. Only
                    # meaningful for real video playback (VLC), so it's None
                    # (absent) for inactive and music/visualizer channels -- those
                    # play plain audio files through pygame's mixer, which has no
                    # concept of multiple audio tracks. Keep the ordering in sync
                    # with the render block in ui.py.
                    _audio_track_row = None if (not ch_info.get("active", True) or is_music_mode) else (_holiday_row + 1)
                    _clear_row     = 7 if _sched_is_marathon else 9

                    if event.key == pygame.K_w:
                        if sub_row == 0:
                            pass  # Clamped — use ESC or Close to exit sub-menu
                        elif sub_row == max_close_row:
                            app_state["sub_menu_row_index"] = max_close_row - 1 
                            app_state["sub_menu_col_index"] = 0 
                        else: 
                            app_state["sub_menu_row_index"] = sub_row - 1
                            # Col only has meaning on row 0 (3 cols) and row 2 (commercials, 2 cols).
                            # Reset to 0 whenever moving between other rows so the cursor
                            # never lands on an invisible phantom column position.
                            app_state["sub_menu_col_index"] = 0
                        continue
                        
                    elif event.key == pygame.K_s:
                        if sub_row == max_close_row - 1: 
                            app_state["sub_menu_row_index"] = max_close_row
                            app_state["sub_menu_col_index"] = 0 
                        elif sub_row == max_close_row:
                            pass 
                        else:
                            new_sub_row = sub_row + 1
                            app_state["sub_menu_row_index"] = new_sub_row
                            # Col only has meaning on row 0 (3 cols) and row 2 (commercials, 2 cols).
                            # Reset to 0 whenever moving between other rows so the cursor
                            # never lands on an invisible phantom column position.
                            app_state["sub_menu_col_index"] = 0
                        continue
                        
                    elif event.key == pygame.K_d:
                        if sub_row == max_close_row: 
                            pass 
                        elif sub_row == 0:
                            # Only allow col navigation when channel is active (vis toggle + name hidden when off)
                            if ch_info.get("active", True):
                                app_state["sub_menu_col_index"] = min(2, sub_col + 1)
                        elif not is_music_mode and sub_row == 3:
                            # Only navigate to the Commercials file button (col 1) and
                            # the placement-mode toggle (col 2) when commercials are ON.
                            _com_vis = ch_info.get("commercials_enabled", False)
                            if _com_vis:
                                app_state["sub_menu_col_index"] = min(2, sub_col + 1)
                        elif not is_music_mode and sub_row == _folder_row:
                            # Folder slots row: 3 side-by-side cells (Morning/Evening/Night).
                            # Collapses to a single 24h button (no column move) in
                            # Marathon mode, or when this channel's BLOCK LENGTH is
                            # set to 24-HOUR (FULL DAY).
                            if not _sched_is_marathon and not _is_full_day:
                                app_state["sub_menu_col_index"] = min(2, sub_col + 1)
                        else: 
                            # Scheduling Mode, Pair Threshold, and Visualizer Style
                            # (rows 1/2, and row 1 in music mode) used to cycle here
                            # on A/D — they're Enter-only cycles now, same as every
                            # other multi-choice row. See the K_RETURN branch below.
                            pass  # rows 1-2 (non-nav) and 4–9 (folder/holiday/clear/close) have only col 0
                        continue
                        
                    elif event.key == pygame.K_a:
                        if sub_row == max_close_row:
                            pass
                        elif sub_row == 0:
                            # Row 0: LEFT navigates vis toggle → status toggle → name.
                            # If last active channel, status toggle (col 0) is locked — clamp min to 1.
                            if ch_info.get("active", True):
                                _active_count_r0 = sum(
                                    1 for _n in range(5, 45)
                                    if db.channels_db.get(str(_n).zfill(2), {}).get("active", str(_n).zfill(2) == "05")
                                )
                                _is_last_r0 = ch_info.get("active", True) and (_active_count_r0 <= 1)
                                _min_col_r0 = 1 if _is_last_r0 else 0
                                app_state["sub_menu_col_index"] = max(_min_col_r0, sub_col - 1)
                        elif not is_music_mode and sub_row == 3:
                            # Navigate left from Commercials button back to the toggle (col 0).
                            _new_col = max(0, sub_col - 1)
                            app_state["sub_menu_col_index"] = _new_col
                        else:
                            # Scheduling Mode, Pair Threshold, and Visualizer Style
                            # are Enter-only cycles now — see the K_RETURN branch below.
                            # Only allow col navigation when channel is active (col 1/2 hidden when off)
                            if ch_info.get("active", True):
                                app_state["sub_menu_col_index"] = max(0, sub_col - 1)
                        continue
# ==============================================================================
# PART 16 OF 28: SUB-MENU EXPLORER WINDOW POPUP CONTROLLERS
# ==============================================================================

                    elif event.key == pygame.K_RETURN:
                        if sub_row == max_close_row:
                            print("[SUB-MENU CONTROLLER] Close button triggered. Returning to main Channels tab layout.")
                            app_state["menu_layer"] = "CHANNEL_LIST"
                            app_state["menu_selection_index"] = max(0, min(39, app_state.get("selected_channel_row", 5) - 5))
                        elif sub_row == 0:
                            if sub_col == 0:
                                currently_active = ch_info.get("active", True)
                                if currently_active:
                                    # Only allow turning OFF if another channel (05-44) is still on
                                    if _count_active_content_channels(db) > 1:
                                        ch_info["active"] = False
                                        print(f"[CHANNEL] {target_ch_str} turned OFF.")
                                        # Invalidate guide timeline cache so the OFF state
                                        # appears immediately rather than waiting for a
                                        # viewport-position change to bust the inner cache.
                                        app_state["guide_refresh_token"] = app_state.get("guide_refresh_token", 0) + 1
                                        # If we're watching this channel, hop up to the next active one
                                        _surf_up_from_disabled(target_ch_str)
                                    else:
                                        print(f"[CHANNEL] {target_ch_str} is the last active channel - blocked.")
                                else:
                                    ch_info["active"] = True
                                    print(f"[CHANNEL] {target_ch_str} turned ON.")
                                    # Invalidate guide timeline cache so the newly-enabled channel's
                                    # schedule appears in the guide immediately.
                                    app_state["guide_refresh_token"] = app_state.get("guide_refresh_token", 0) + 1
                            elif sub_col == 1:
                                ch_info["is_visualizer"] = not ch_info.get("is_visualizer", False)
                                if ch_info["is_visualizer"] and app_state["sub_menu_row_index"] > 4:
                                    app_state["sub_menu_row_index"] = 4
                                print(f"[MODE ENGINE] Toggled channel mode. is_visualizer={ch_info['is_visualizer']}")
                            elif sub_col == 2: 
                                app_state["editing_channel_name"] = True
                                app_state["channel_name_all_selected"] = True  # highlight on entry
                                print(f"[NAME ENGINE] Editing channel {target_ch_str} text identification label string.")
                        else:
                            active_block = "Morning"
                            is_clear_operation = False
                            
                            if is_music_mode:
                                if sub_row == 1:
                                    # Visualizer style: ENTER cycles forward (used to be
                                    # A/D-only — now consistent with every other
                                    # multi-choice row in this menu).
                                    styles = ["Random", "PrismShards", "LiquidChrome", "PulseRings", "WarpTunnel", "GlowBraid", "EqualizerGrid", "ParticleFlow", "NeonStarfield"]
                                    try: curr_idx = styles.index(ch_info.get("visualizer_style", "Random"))
                                    except ValueError: curr_idx = 0
                                    ch_info["visualizer_style"] = styles[(curr_idx + 1) % len(styles)]
                                    print(f"[VISUALIZER STYLE] Cycled forward to style: {ch_info['visualizer_style']}")
                                    db.save_settings()
                                    continue
                                elif sub_row == 2:
                                    active_block = "Music Tracks"
                                elif sub_row == 3:
                                    is_clear_operation = True
                            else:
                                # Folder slots now share ONE row across columns (row number
                                # shifts by one in block modes vs marathon — see _folder_row).
                                folder_map = {
                                    (_folder_row, 0): "Morning",
                                    (_folder_row, 1): "Evening",
                                    (_folder_row, 2): "Night",
                                }
                                if sub_row == 1:
                                    # Sched mode: ENTER cycles forward
                                    _SM = ["random_slots", "one_slot", "two_slots", "marathon"]
                                    _sc = ch_info.get("scheduling_mode", "random_slots")
                                    try: _si = _SM.index(_sc)
                                    except ValueError: _si = 0
                                    _apply_scheduling_mode_change(ch_info, _SM[(_si + 1) % len(_SM)], target_ch_str)
                                    continue
                                elif sub_row == 2:
                                    # Pair threshold: ENTER cycles forward (same as D key)
                                    _pt = ch_info.get("pair_threshold_minutes", 15)
                                    ch_info["pair_threshold_minutes"] = _pt + 1 if _pt < 30 else 0
                                    db.save_settings()
                                    continue
                                elif sub_row == 3:
                                    if sub_col == 0:
                                        _new_val = not ch_info.get("commercials_enabled", False)
                                        ch_info["commercials_enabled"] = _new_val
                                        # Toggling commercials reshapes this channel's
                                        # timeline from here forward (shows round UP to
                                        # the half-hour with commercial breaks, or
                                        # collapse back to back-to-back). But exactly
                                        # like switching between the block scheduling
                                        # modes (see _apply_scheduling_mode_change),
                                        # that only changes what airs NEXT — the show
                                        # already live keeps airing uninterrupted.
                                        # playback_anchor pins the currently-airing
                                        # file/seek and is deliberately left alone here;
                                        # clearing it (as this used to do) force-reset
                                        # whatever was already playing the instant you
                                        # flipped the toggle, even before adding a
                                        # single commercial.
                                        #
                                        # Invalidate the per-block rotation cache so the
                                        # guide and the next-episode pick reflect the
                                        # commercial-adjusted timing once the current
                                        # show ends, without touching what's live now.
                                        _plog = ch_info.setdefault("playback_log", {})
                                        _plog["sched_block_key"] = ""
                                        _plog["sched_cached_tracks"] = []
                                        _plog["sched_cached_durations"] = {}
                                        db.save_settings()
                                    elif sub_col == 1:
                                        # Commercials file button: open the file
                                        # explorer directly to add commercial videos.
                                        # This used to open the COMMERCIALS_SUB_MENU
                                        # (which held Commercials/Intros/Outros pickers
                                        # + placement radios); that sub-menu has been
                                        # removed, so we go straight to the picker,
                                        # mirroring the schedule file-picker path below.
                                        _com_on = db.channels_db.get(target_ch_str, {}).get("commercials_enabled", False)
                                        if _com_on:
                                            if static_audio is not None:
                                                static_audio.stop()
                                            pygame.mixer.Channel(1).stop()
                                            _fe_open(
                                                "Add Commercials",
                                                {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv"},
                                                {"mode": "commercial", "ch": target_ch_str, "block": "Commercials"}
                                            )
                                    elif sub_col == 2:
                                        # Placement-mode toggle button: ENTER cycles
                                        # Interrupt show <-> End of show only, the same
                                        # per-channel commercial_placement value the
                                        # removed sub-menu's radios used to set. Mirrors
                                        # the Theme tab's CHANNEL TRANSITION button.
                                        _com_on = db.channels_db.get(target_ch_str, {}).get("commercials_enabled", False)
                                        if _com_on:
                                            _cur_place = ch_info.get("commercial_placement", "interrupt_half_hour")
                                            ch_info["commercial_placement"] = (
                                                "end_of_show" if _cur_place == "interrupt_half_hour"
                                                else "interrupt_half_hour")
                                            print(f"[COMMERCIALS] Placement -> {ch_info['commercial_placement']} for ch {target_ch_str}")
                                            db.save_settings()
                                    continue
                                elif _order_row is not None and sub_row == _order_row:
                                    # Episode order: ENTER toggles Sequential <-> Random.
                                    # Only reachable in block modes — marathon has no
                                    # per-episode "next up" pick, so _order_row is None
                                    # there and this branch can never be hit.
                                    _cur_order = ch_info.get("episode_order_mode", "sequential")
                                    ch_info["episode_order_mode"] = "random" if _cur_order == "sequential" else "sequential"
                                    print(f"[EPISODE ORDER] ch{target_ch_str} -> {ch_info['episode_order_mode']}")
                                    db.save_settings()
                                    continue
                                elif _block_len_row is not None and sub_row == _block_len_row:
                                    # Block length: ENTER toggles 8-HOUR BLOCKS <->
                                    # 24-HOUR (FULL DAY). Only reachable in block modes —
                                    # marathon is already inherently 24-hour, so
                                    # _block_len_row is None there and this branch can
                                    # never be hit.
                                    _cur_bl = ch_info.get("block_mode", "full_day")
                                    ch_info["block_mode"] = "full_day" if _cur_bl == "segmented" else "segmented"
                                    # Same pattern as the Commercials toggle: this only
                                    # reshapes what airs NEXT — the show already live
                                    # keeps playing uninterrupted via playback_anchor.
                                    # Just invalidate the per-block rotation cache so the
                                    # guide/next-pick reflect the new block layout once
                                    # the current show ends.
                                    _plog = ch_info.setdefault("playback_log", {})
                                    _plog["sched_block_key"] = ""
                                    _plog["sched_cached_tracks"] = []
                                    _plog["sched_cached_durations"] = {}
                                    print(f"[BLOCK LENGTH] ch{target_ch_str} -> {ch_info['block_mode']}")
                                    db.save_settings()
                                    continue
                                elif sub_row == _holiday_row:
                                    app_state["menu_layer"] = "HOLIDAY_SUB_MENU"
                                    app_state["sub_menu_row_index"] = 0
                                    app_state["sub_menu_col_index"] = 0
                                    continue
                                elif sub_row == _clear_row:
                                    is_clear_operation = True
                                elif _audio_track_row is not None and sub_row == _audio_track_row:
                                    _open_audio_track_picker(target_ch_str)
                                    continue
                                elif _sched_is_marathon and sub_row == _folder_row:
                                    # Marathon collapses the 3 slots into one 24h folder.
                                    active_block = "Marathon"
                                elif _is_full_day and sub_row == _folder_row:
                                    # BLOCK LENGTH set to 24-HOUR (FULL DAY) collapses the
                                    # 3 slots into one 24h folder too — but this channel
                                    # keeps using its own scheduling_mode to pick from it,
                                    # unlike Marathon's fixed full-catalog playlist.
                                    active_block = "Full Day"
                                else:
                                    active_block = folder_map.get((sub_row, sub_col), "Morning")
                            
                            if is_clear_operation:
                                if ch_info.get("is_visualizer", False):
                                    ch_info["schedules"]["Music Tracks"] = []
                                else:
                                    for key in ["Morning", "Commercials", "Evening", "Intros", "Night", "Outros", "Marathon", "Full Day"]:
                                        ch_info["schedules"][key] = []

                                # Flush per-channel schedule caches so
                                # calculate_slotted_playback_state() re-derives
                                # from the now-empty lists on the very next frame
                                # instead of serving the last cached file path.
                                ch_info.pop("sched_cached_tracks",   None)
                                ch_info.pop("marathon_anchor_epoch", None)
                                ch_info.pop("playback_log",          None)

                                print(f"[CLEARED PROFILE] Storage wiped for Channel {target_ch_str}.")

                                # ── ACTIVE-CHANNEL MEDIA TEARDOWN ────────────────────
                                # If the user is currently watching this channel, stop
                                # every audio/video layer immediately.  Without this,
                                # VLC keeps playing the old file — is_playing_video
                                # stays True, the render loop keeps calling
                                # get_display_frame(), and the video + audio keep
                                # running even though the schedule is now empty.
                                if app_state.get("current_channel", "") == target_ch_str:
                                    try:
                                        if vlc_engine is not None:
                                            vlc_engine.stop()
                                    except Exception as _ce:
                                        log.warning("[CLEAR CHANNEL] vlc_engine.stop() failed: %s", _ce)
                                    if 'static_audio' in globals() and static_audio is not None:
                                        try: static_audio.stop()
                                        except Exception: pass
                                    try:
                                        pygame.mixer.Channel(1).stop()
                                    except Exception: pass
                                    try:
                                        if pygame.mixer.music.get_busy():
                                            pygame.mixer.music.stop()
                                        pygame.mixer.music.unload()
                                    except Exception: pass
                                    # Drop the cached frame bridge so the last decoded
                                    # video image doesn't linger over the static.
                                    _clear_last_video_frame()
                                    app_state["is_playing_video"]            = False
                                    app_state["active_music_track_file"]     = ""
                                    app_state["last_scheduled_vis_track"]    = {}
                                    app_state["persistent_track_pool_cache"] = ""
                                    app_state["last_vlc_retry_at"]           = 0
                                    app_state["last_video_play_started_at"]  = 0
                            else:
                                if static_audio is not None:
                                    static_audio.stop()
                                pygame.mixer.Channel(1).stop()
                                
                                app_state["sub_menu_row_index"] = sub_row
                                is_vis_channel = ch_info.get("is_visualizer", False)
                                
                                if is_vis_channel:
                                    _pick_title = f"Add Audio — {active_block}"
                                    _pick_exts  = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma"}
                                else:
                                    _pick_title = f"Add Video — {active_block} Schedule"
                                    _pick_exts  = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv"}
                                _fe_open(_pick_title, _pick_exts, {
                                    "mode": "schedule", "ch": target_ch_str, "block": active_block
                                })
                            
                        db.save_settings()
                        continue

# ==============================================================================
                # ==============================================================================
                # PART 15b2 OF 28: CHANGE CURRENT PLAYING AUDIO TRACK POPUP
                # ==============================================================================

                # --- MENU LAYER: AUDIO TRACK PICKER (small popup, W/S to move,
                #     ENTER picks a track and closes the window) ---
                elif app_state["menu_layer"] == "AUDIO_TRACK_PICKER":
                    sub_row  = app_state.get("sub_menu_row_index", 0)
                    tracks   = app_state.get("audio_track_list", [])
                    close_row = len(tracks)   # one extra row past the tracks, for Close
                    max_row   = close_row

                    if event.key == pygame.K_w:
                        app_state["sub_menu_row_index"] = max(0, sub_row - 1)
                        continue
                    elif event.key == pygame.K_s:
                        app_state["sub_menu_row_index"] = min(max_row, sub_row + 1)
                        continue
                    elif event.key == pygame.K_RETURN:
                        if sub_row == close_row:
                            pass  # Close: fall through to the shared close-out below unchanged
                        elif 0 <= sub_row < len(tracks) and vlc_engine is not None:
                            track_id, track_name = tracks[sub_row]
                            vlc_engine.set_audio_track(track_id)
                            _src_ch = app_state.get("audio_track_source_channel")
                            _cur_file = getattr(vlc_engine, "current_filepath", None)
                            if _cur_file:
                                _save_audio_track_pref(_cur_file, track_id, track_name)
                            app_state["audio_track_active_id"] = track_id
                            print(f"[AUDIO TRACK] Channel {_src_ch}: switched to '{track_name}' and remembered for this file.")
                        # Selecting a track (or Close) closes the window, same as ESC.
                        _atp_ch = db.channels_db.get(str(app_state.get("selected_channel_row", "")).zfill(2), {})
                        _atp_marathon = _atp_ch.get("scheduling_mode", "random_slots") == "marathon"
                        app_state["menu_layer"] = "CHANNEL_SUB_MENU"
                        app_state["sub_menu_row_index"] = 7 if _atp_marathon else 9
                        app_state["sub_menu_col_index"] = 0
                        continue

# ==============================================================================
# PART 15b OF 28: HOLIDAY SCHEDULE OVERRIDES SUB-MENU HANDLER
# ==============================================================================

                # --- MENU LAYER: HOLIDAY SCHEDULE OVERRIDES ---
                elif app_state["menu_layer"] == "HOLIDAY_SUB_MENU":
                    sub_row       = app_state.get("sub_menu_row_index", 0)
                    target_ch_str = str(app_state["selected_channel_row"]).zfill(2)
                    ch_info       = db.channels_db.get(target_ch_str, {})
                    HKEYS = ["halloween", "christmas", "valentine", "thanksgiving", "new_year"]
                    MAX_H = 5   # rows 0-4 = holidays, row 5 = Back

                    if event.key == pygame.K_w:
                        if sub_row == 0:
                            pass  # Clamped — use ESC or Back to exit sub-menu
                        else:
                            app_state["sub_menu_row_index"] = sub_row - 1
                        continue
                    elif event.key == pygame.K_s:
                        if sub_row < MAX_H:
                            app_state["sub_menu_row_index"] = sub_row + 1
                        continue
                    elif event.key == pygame.K_RETURN:
                        if sub_row == MAX_H:
                            app_state["menu_layer"] = "CHANNEL_SUB_MENU"
                            # Land back on whichever row is "Holiday overrides" for this
                            # channel's current scheduling mode — that row shifts by one
                            # in block modes vs marathon (see _holiday_row in the main
                            # CHANNEL_SUB_MENU handler), so it can't be a fixed constant.
                            _is_marathon = ch_info.get("scheduling_mode", "random_slots") == "marathon"
                            app_state["sub_menu_row_index"] = 5 if _is_marathon else 6
                            app_state["sub_menu_col_index"] = 0
                        elif 0 <= sub_row < len(HKEYS):
                            h_key = HKEYS[sub_row]
                            if static_audio is not None:
                                static_audio.stop()
                            pygame.mixer.Channel(1).stop()
                            _fe_open(
                                f"Holiday Override — {h_key.replace('_', ' ').title()} Videos",
                                {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv"},
                                {"mode": "holiday", "ch": target_ch_str, "hkey": h_key}
                            )
                        pygame.event.clear(pygame.KEYDOWN)
                        continue

# ==============================================================================
# PART 16b OF 28: GAMES TAB GRID & CONSOLE SUB-MENU INPUT HANDLERS
# ==============================================================================

                # --- GAMES TAB: CONSOLE GRID ---
                elif app_state["menu_layer"] == "GAMES_LIST":
                    from game_deck import CONSOLE_ORDER
                    console_list = [c for c in CONSOLE_ORDER]
                    cols = 4
                    total = len(console_list)
                    cur_sel = app_state.get("menu_selection_index", 0)

                    _mm_on = db.config.get("ch03_menu_music_enabled", True) if db else True
                    _ss_on = db.config.get("ch03_screensaver_enabled", True) if db else True
                    # Set Music (96) opens the same free-roam file explorer as
                    # core_select does in the per-console sub-menu, so it's
                    # skipped over the same way under Kiosk Mode -- see the
                    # matching note in ui.py's Games-tab render block.
                    _kiosk_on_gl = db.config.get("kiosk_mode_enabled", False) if db else False
                    _set_music_visible = _mm_on and not _kiosk_on_gl
                    if event.key == pygame.K_w:
                        if cur_sel < cols:
                            app_state["menu_layer"] = "TAB_SELECTION"
                        elif cur_sel == 99:
                            # Close → row 2, rightmost visible item
                            app_state["menu_selection_index"] = 96 if _set_music_visible else 97
                        elif cur_sel == 96:
                            # Col 3 row2 → row1 (Menu Music)
                            app_state["menu_selection_index"] = 97
                        elif cur_sel == 101:
                            # Col 1 row2 → row1 (All Consoles)
                            app_state["menu_selection_index"] = 95
                        elif cur_sel == 100:
                            # Col 2 row2 → row1 (Screen Saver)
                            app_state["menu_selection_index"] = 98
                        elif cur_sel in (95, 98, 97):
                            # Control row 1 → arrow button
                            app_state["menu_selection_index"] = 102
                        elif cur_sel == 102:
                            # Arrow button → last console in grid
                            app_state["menu_selection_index"] = total - 1
                        else:
                            app_state["menu_selection_index"] = cur_sel - cols
                        continue
                    elif event.key == pygame.K_s:
                        if cur_sel == 99:
                            pass
                        elif cur_sel in (96, 101, 100):
                            # Row 2 items → Close
                            app_state["menu_selection_index"] = 99
                        elif cur_sel == 97:
                            # Col 3 row1 → Set Music (if visible) else Close
                            app_state["menu_selection_index"] = 96 if _set_music_visible else 99
                        elif cur_sel == 95:
                            # Col 1 row1 → All Profiles (row 2)
                            app_state["menu_selection_index"] = 101
                        elif cur_sel == 98:
                            # Col 2 row1 → Timer (if ss_on) else Close
                            app_state["menu_selection_index"] = 100 if _ss_on else 99
                        elif cur_sel == 102:
                            # Arrow button → first control row (All Consoles)
                            app_state["menu_selection_index"] = 95
                        elif cur_sel + cols < total:
                            app_state["menu_selection_index"] = cur_sel + cols
                        else:
                            # Last grid row → arrow button
                            app_state["menu_selection_index"] = 102
                        continue
                    elif event.key == pygame.K_a:
                        if cur_sel == 100:
                            # Timer row — A now navigates (to All Profiles), it no
                            # longer adjusts the value. Only Enter changes the value.
                            app_state["menu_selection_index"] = 101
                        elif cur_sel == 98:
                            # Col 2 row1 → col1 row1 (All Consoles)
                            app_state["menu_selection_index"] = 95
                        elif cur_sel == 97:
                            # Col 3 row1 → col2 row1 (Screen Saver)
                            app_state["menu_selection_index"] = 98
                        elif cur_sel == 96:
                            # Col 3 row2 → col2 row2 (Timer if ss_on) else col1 row2
                            app_state["menu_selection_index"] = 100 if _ss_on else 101
                        elif cur_sel in (95, 101, 99, 102):
                            pass  # Already leftmost / special
                        elif cur_sel % cols != 0:
                            app_state["menu_selection_index"] = cur_sel - 1
                        continue
                    elif event.key == pygame.K_d:
                        if cur_sel == 100:
                            # Timer row — D now navigates (to Set Music if visible),
                            # it no longer adjusts the value. Only Enter changes it.
                            if _set_music_visible:
                                app_state["menu_selection_index"] = 96
                        elif cur_sel == 95:
                            # Col 1 row1 → col2 row1 (Screen Saver)
                            app_state["menu_selection_index"] = 98
                        elif cur_sel == 98:
                            # Col 2 row1 → col3 row1 (Menu Music)
                            app_state["menu_selection_index"] = 97
                        elif cur_sel == 101:
                            # Col 1 row2 → col2 row2 (Timer if ss_on) else col3 row2
                            app_state["menu_selection_index"] = 100 if _ss_on else 96
                        elif cur_sel in (97, 96, 99, 102):
                            pass  # Already rightmost / special
                        elif cur_sel % cols != cols - 1 and cur_sel + 1 < total:
                            app_state["menu_selection_index"] = cur_sel + 1
                        continue
                    elif event.key == pygame.K_RETURN:
                        if cur_sel == 99:
                            app_state["show_menu"] = False
                            app_state["menu_shown_at"] = 0
                        elif cur_sel == 95:
                            _toggle_all_consoles()
                        elif cur_sel == 101:
                            _toggle_all_profiles()
                        elif cur_sel == 102:
                            # Arrow button — flip between page 0 (consoles) and page 1 (empty)
                            app_state["games_tab_page"] = 1 - app_state.get("games_tab_page", 0)
                        elif cur_sel == 100:
                            # Enter on Timer row also advances it by 5 min, wrapping 30 -> 5
                            _adjust_ss_timer(5, wrap=True)
                        elif cur_sel == 98:
                            _toggle_ss()
                        elif cur_sel == 97:
                            # Enter on toggle row flips it, same as A/D
                            _toggle_menu_music()
                        elif cur_sel == 96 and not _kiosk_on_gl:
                            # Enter on Set Music button — open file picker.
                            # The Kiosk Mode guard here is belt-and-suspenders:
                            # normal navigation already can't land the cursor on
                            # row 96 while Kiosk Mode is on (see the W/S skip
                            # logic above), but this stops it cold even if the
                            # cursor got here some other way.
                            if static_audio is not None: static_audio.stop()
                            pygame.mixer.Channel(1).stop()
                            _fe_open(
                                "Menu Music ��� Set Tracks",
                                {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma"},
                                {"mode": "ch03_menu_music"}
                            )
                        elif 0 <= cur_sel < total:
                            app_state["games_selected_console"] = console_list[cur_sel]

                            # If this is the last active console its Status toggle is
                            # locked — land one row down so the cursor isn't stuck on
                            # a greyed item (mirrors the Channels sub-menu entry logic).
                            _con_key_entry = console_list[cur_sel]
                            _con_enabled_map_entry = db.config.get("consoles_enabled", {})
                            _con_default_on_entry = (_con_key_entry == "DVD")
                            _con_is_on_entry = _con_enabled_map_entry.get(_con_key_entry, _con_default_on_entry)
                            _con_is_last_entry = _con_is_on_entry and (_count_active_consoles(db) <= 1)
                            app_state["games_sub_row"] = 1 if _con_is_last_entry else 0

                            app_state["menu_layer"] = "GAMES_SUB_MENU"
                            print(f"[GAMES TAB] Opening sub-menu for: {console_list[cur_sel]}")
                        pygame.event.clear(pygame.KEYDOWN)
                        continue

                # --- GAMES TAB: CONSOLE SUB-MENU ---
                elif app_state["menu_layer"] == "GAMES_SUB_MENU":
                    console_key = app_state.get("games_selected_console", "DVD")
                    sub_row = app_state.get("games_sub_row", 0)
                    _g_enabled_map = db.config.get("consoles_enabled", {})
                    _g_default_on  = (console_key == "DVD")
                    _g_is_on       = _g_enabled_map.get(console_key, _g_default_on)

                    # games_rows is the single source of truth (see
                    # get_games_sub_menu_rows in ui.py) for which rows exist
                    # below the Status toggle right now -- Kiosk Mode hides
                    # "core_select" specifically, since that's the one row
                    # that lets a kid browse the filesystem and swap in a
                    # different core .dll. "status" is always row 0 and
                    # "close" is always the last row.
                    games_rows = ["status"] + get_games_sub_menu_rows(db, app_state, console_key, _g_is_on) + ["close"]
                    GAMES_CLOSE_ROW = len(games_rows) - 1
                    MAX_GAMES_SUB_ROW = GAMES_CLOSE_ROW
                    row_id = games_rows[sub_row] if sub_row < len(games_rows) else "close"
                    _has_disc_row = "disc" in games_rows

                    # Status toggle (row 0) is locked when this is the last active
                    # console — mirrors the Channels sub-menu "last active channel"
                    # lock (05-44). DVD is the console-neutral, non-BIOS default so
                    # it plays the same role channel 05 plays there.
                    _g_is_last_console = _g_is_on and (_count_active_consoles(db) <= 1)

                    def _cycle_psx_disc(step):
                        if game_deck is None or not hasattr(game_deck, "get_psx_disc_info"):
                            return
                        discs, cur_idx = game_deck.get_psx_disc_info()
                        if not discs:
                            return
                        new_idx = 0 if cur_idx < 0 else (cur_idx + step) % len(discs)
                        game_deck.change_psx_disc(new_idx)

                    if event.key == pygame.K_w:
                        if sub_row == 0:
                            pass  # Clamped — use ESC or Close to exit sub-menu
                        elif sub_row == 1 and _g_is_last_console:
                            pass  # Status toggle above is locked — can't land on it
                        else:
                            app_state["games_sub_row"] = sub_row - 1
                        continue
                    elif event.key == pygame.K_s:
                        if sub_row < MAX_GAMES_SUB_ROW:
                            app_state["games_sub_row"] = sub_row + 1
                        continue
                    elif event.key in (pygame.K_a, pygame.K_d):
                        if row_id == "disc":
                            # A stepper (cycle to next/prev disc), not a toggle — stays on A/D.
                            _cycle_psx_disc(1 if event.key == pygame.K_d else -1)
                        # NOTE: row_id == "status" (console enable/disable) used to
                        # also flip here on A/D — now Enter-only, see below.
                        #
                        # FLAGGED FOR REVIEW: this used to ALSO flip the global
                        # screensaver on A/D when sitting on the "close" row for a
                        # non-DVD console with no disc row — with no Enter
                        # equivalent anywhere. That behavior had no visible label
                        # tying it to "toggle screensaver," so it's removed here
                        # rather than carried over as an Enter-triggered toggle.
                        # Let me know if that was actually load-bearing and I'll
                        # bring it back as a proper, visible Enter toggle instead.
                        continue
                    elif event.key == pygame.K_RETURN:
                        if row_id == "status":
                            if _g_is_last_console:
                                print(f"[GAMES] {console_key} is the last active console - blocked.")
                            else:
                                enabled_map = db.config.setdefault("consoles_enabled", {})
                                default_on = (console_key == "DVD")
                                enabled_map[console_key] = not enabled_map.get(console_key, default_on)
                                db.save_settings()
                                if game_deck is not None: game_deck.refresh_from_config(db.config)
                                # If just turned OFF, snap cursor back to row 0 so it's not stranded
                                if not enabled_map[console_key]:
                                    app_state["games_sub_row"] = 0
                                print(f"[GAMES] {console_key} enabled = {enabled_map[console_key]}")
                        elif row_id == "core_select":
                            import os as _os
                            emu_folder = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "main", "cores", console_key)
                            _os.makedirs(emu_folder, exist_ok=True)
                            if static_audio is not None:
                                static_audio.stop()
                            pygame.mixer.Channel(1).stop()
                            _fe_open(
                                f"Select Libretro Core (.dll) — {console_key}",
                                {".dll"},
                                {"mode": "libretro_core", "console_key": console_key, "emu_folder": emu_folder}
                            )
                            # Drop the explorer straight into this console's emulator folder
                            _fe_navigate(emu_folder)
                        elif row_id == "controller_map":
                            # Open the controller mapping panel
                            app_state["menu_layer"]  = "GAMES_CTRL_MAP"
                            app_state["games_ctrl_console"] = console_key
                            app_state["ctrl_map_cursor"] = 0
                            if game_deck is not None:
                                game_deck.ctrl_mapping_active_btn = None
                                game_deck._seq_remap_active = False
                                game_deck._ctrl_map_cursor  = 0
                            print(f"[GAMES] Opening controller mapping UI for {console_key}")
                        elif row_id in ("profile1", "profile2", "profile3"):
                            # Open profile sub-menu for this profile
                            profile_num = int(row_id[-1])
                            app_state["games_profile_num"]  = profile_num
                            app_state["games_profile_row"]  = 0
                            app_state["menu_layer"] = "GAMES_SAVE_PROFILE"
                            print(f"[GAMES] Opening profile {profile_num} for {console_key}")
                        elif row_id == "disc":
                            # Enter also advances to the next disc — same as pressing D
                            _cycle_psx_disc(1)
                        elif row_id == "dvd_button_mapping":
                            # Open the DVD transport-key remap popup.
                            app_state["menu_layer"] = "DVD_BTN_MAP"
                            app_state["dvd_map_cursor"] = 0
                            app_state["dvd_map_capturing"] = False
                            app_state["dvd_map_capture_idx"] = 0
                            print("[DVD REMAP] Opening DVD button mapping popup")
                        elif row_id == "close":
                            app_state["menu_layer"] = "GAMES_LIST"
                            app_state["menu_selection_index"] = 0
                        pygame.event.clear(pygame.KEYDOWN)
                        continue


                # --- GAMES TAB: DVD BUTTON MAPPING POPUP ---
                elif app_state["menu_layer"] == "DVD_BTN_MAP":
                    # Skip synthetic controller-nav events — only real key /
                    # remote presses are valid DVD transport bindings.
                    if getattr(event, "synthetic", False):
                        continue

                    _capturing = app_state.get("dvd_map_capturing", False)

                    def _close_dvd_map():
                        # Return to the DVD sub-menu, cursor back on the
                        # "Button Mapping" row (row index 1) that opened this.
                        app_state["menu_layer"] = "GAMES_SUB_MENU"
                        app_state["games_sub_row"] = 1
                        app_state["dvd_map_capturing"] = False
                        pygame.event.clear(pygame.KEYDOWN)

                    if _capturing:
                        # --- Capture state: waiting for a key for the current action ---
                        if event.key == pygame.K_ESCAPE:
                            # Cancel the remaining capture sequence, keep what's set.
                            app_state["dvd_map_capturing"] = False
                            print("[DVD REMAP] Capture cancelled.")
                            pygame.event.clear(pygame.KEYDOWN)
                            continue

                        # Duplicate-delivery de-dup: the back-arrow (and a few
                        # other buttons) can arrive as TWO KEYDOWNs at once via
                        # two Windows input paths, which would bind the NEXT
                        # action too. We used to only swallow when the same key
                        # code arrived with a DIFFERENT scancode, on the theory
                        # that intentional re-use of the same button for the
                        # next action always keeps the same scancode. That's
                        # true for most buttons, but the back-arrow's two
                        # delivery paths land with the SAME key AND the SAME
                        # scancode -- so the old check let the duplicate straight
                        # through and it bound both the current slot and the
                        # next one. Fix: also swallow same-key/same-scancode
                        # repeats when they arrive within a much tighter window
                        # -- true hardware duplicates land within the same
                        # frame or two, while a deliberate second press for the
                        # next slot takes at least the time to notice the
                        # prompt advance and press again (always well over this
                        # window in practice).
                        _DVD_HARD_DEDUP_MS = 120
                        _dvd_last = app_state.get("_dvd_map_last_accept")
                        if _dvd_last is not None and event.key == _dvd_last["key"]:
                            _dvd_dt = pygame.time.get_ticks() - _dvd_last["ms"]
                            _dvd_same_scancode = getattr(event, "scancode", 0) == _dvd_last["scancode"]
                            if _dvd_dt < _DVD_HARD_DEDUP_MS:
                                # Same key, arrived almost instantly -- always a
                                # duplicate delivery of the same physical press,
                                # regardless of whether the scancode matches.
                                print("[DVD REMAP] Ignored duplicate capture event "
                                      "(same button, same instant -- second delivery path).", flush=True)
                                continue
                            if _dvd_dt < _REMAP_CAPTURE_DEDUP_MS and not _dvd_same_scancode:
                                print("[DVD REMAP] Ignored duplicate capture event "
                                      "(same button on a second input path).", flush=True)
                                continue

                        # Conflict check: reject keys already bound in the remote
                        # remap, reserved as system shortcuts (controls menu,
                        # menu), or already assigned to an earlier action in
                        # THIS mapping pass. That last part was missing before --
                        # the check only looked at remote_bindings and the two
                        # hardcoded system keys, so nothing stopped picking the
                        # same key (e.g. "left") for every single DVD action.
                        _remote_binds = db.config.get("remote_bindings", {}) if db is not None else {}
                        _reserved = set(v for v in _remote_binds.values() if v is not None)
                        _reserved.add(pygame.K_c)   # controls menu hotkey
                        _reserved.add(pygame.K_m)   # system menu hotkey
                        _reserved.update(app_state.get("_dvd_map_used_keys", []))
                        if event.key in _reserved:
                            app_state["_dvd_map_reject_flash"] = pygame.time.get_ticks()
                            app_state["_dvd_map_reject_key"]   = event.key
                            print(f"[DVD REMAP] Rejected key {event.key} -- already used by remote remap, system, or an earlier action in this mapping pass.", flush=True)
                            continue

                        _idx = app_state.get("dvd_map_capture_idx", 0)
                        if _idx < len(DVD_MAP_ACTIONS):
                            _action_id = DVD_MAP_ACTIONS[_idx][0]
                            set_dvd_binding(db, _action_id, event.key)
                            app_state["_dvd_map_last_accept"] = {
                                "ms":       pygame.time.get_ticks(),
                                "key":      event.key,
                                "scancode": getattr(event, "scancode", 0),
                            }
                            app_state.setdefault("_dvd_map_used_keys", []).append(event.key)
                        _idx += 1
                        if _idx >= len(DVD_MAP_ACTIONS):
                            app_state["dvd_map_capturing"] = False
                            print("[DVD REMAP] Mapping sequence complete.")
                        else:
                            app_state["dvd_map_capture_idx"] = _idx
                        pygame.event.clear(pygame.KEYDOWN)
                        continue

                    # --- Browse state: move between Map Buttons (0) / Close (1) ---
                    _cursor = app_state.get("dvd_map_cursor", 0)
                    if event.key in (pygame.K_a, pygame.K_LEFT, pygame.K_w, pygame.K_UP):
                        app_state["dvd_map_cursor"] = 0
                    elif event.key in (pygame.K_d, pygame.K_RIGHT, pygame.K_s, pygame.K_DOWN):
                        app_state["dvd_map_cursor"] = 1
                    elif event.key == pygame.K_ESCAPE:
                        _close_dvd_map()
                    elif event.key == pygame.K_RETURN:
                        if _cursor == 0:
                            # Start the capture sequence at the first action.
                            app_state["dvd_map_capturing"] = True
                            app_state["dvd_map_capture_idx"] = 0
                            app_state["_dvd_map_last_accept"] = None
                            app_state["_dvd_map_reject_flash"] = None
                            app_state["_dvd_map_used_keys"] = []
                            print("[DVD REMAP] Starting capture sequence.")
                        else:
                            _close_dvd_map()
                    continue


                # --- GAMES TAB: SAVE/LOAD PROFILE SUB-MENU ---
                elif app_state["menu_layer"] == "GAMES_SAVE_PROFILE":
                    from game_deck import HANDHELD_CONSOLES, BORDER_CONSOLES, border_available_for_console, SCREEN_SIZE_OPTIONS
                    console_key  = app_state.get("games_selected_console", "DVD")
                    profile_num  = app_state.get("games_profile_num", 1)
                    profile_row  = app_state.get("games_profile_row", 0)
                    # Rows: 0=Set Active Profile, 1=Save Now, 2=Load Now,
                    #       3=Auto-Save toggle, 4=Auto-Load toggle,
                    #       5=Screen Size (see HANDHELD_CONSOLES — now every
                    #         emulated console except DVD),
                    #       6=TV Border (see BORDER_CONSOLES — same set, so
                    #         this always lands right after Screen Size), then Back.
                    #         Hidden entirely (not just greyed) for the
                    #         CRT-bezel home/arcade group on a detected 4:3
                    #         display — see border_available_for_console().
                    is_handheld       = console_key in HANDHELD_CONSOLES
                    aspect_ratio_now  = db.config.get("aspect_ratio", "16:9") if db else "16:9"
                    is_border_console = border_available_for_console(console_key, aspect_ratio_now)
                    SCREEN_SIZE_ROW   = 5 if is_handheld else None
                    BORDER_ROW        = 6 if is_border_console else None
                    BACK_ROW          = 5 + (1 if is_handheld else 0) + (1 if is_border_console else 0)
                    MAX_PROFILE_ROW   = BACK_ROW

                    # Helper: read/write a profile setting in db.config
                    def _get_prof_cfg(con, pnum):
                        profiles = (db.config
                                    .setdefault("console_profiles", {})
                                    .setdefault(con, {"active": 1, "profiles": {}})
                                    .setdefault("profiles", {}))
                        # pnum is always an int here, but JSON object keys can only
                        # be strings — so every time db.config gets saved and the
                        # app is later restarted, this "profiles" dict comes back
                        # with STRING keys ("1"/"2"/"3") instead of the int keys it
                        # had in memory. Without this migration, the setdefault
                        # below would silently create a brand-new, empty int-keyed
                        # entry the first time you touch this profile's settings —
                        # orphaning the real saved border/screen-size/auto-save
                        # values that are still sitting under the string key,
                        # and making the menu (and anything you edit) diverge
                        # from what was actually loaded and applied at launch.
                        if pnum not in profiles and str(pnum) in profiles:
                            profiles[pnum] = profiles.pop(str(pnum))
                        return profiles.setdefault(pnum, {"auto_save": False, "auto_load": False})

                    def _active_prof(con):
                        return int(db.config
                                   .setdefault("console_profiles", {})
                                   .setdefault(con, {"active": 1, "profiles": {}})
                                   .get("active", 1))

                    def _set_active_prof(con, pnum):
                        db.config.setdefault("console_profiles", {}) \
                                 .setdefault(con, {"active": 1, "profiles": {}}) \
                                 ["active"] = pnum
                        db.save_settings()
                        if game_deck is not None and hasattr(game_deck, "sync_profile_from_db"):
                            game_deck.sync_profile_from_db(con, db.config)

                    def _is_live_target(con, pnum):
                        """True only when *con* is the console actually loaded/
                        rendering right now (not just some console whose profile
                        menu happens to be open) AND pnum is that console's
                        active profile. Without the console check, editing ANY
                        console's active-profile screen-size/border — even while
                        browsing its menu with a totally different game running
                        underneath, or with nothing loaded at all — would stomp
                        game_deck's single live-render attributes and appear to
                        "bleed" the change onto whatever else was on screen next.
                        Those attributes only ever represent the ONE console
                        currently loaded (see game_deck._libretro_console), so a
                        live push is only safe when con matches it."""
                        return (game_deck is not None
                                and getattr(game_deck, "_libretro_console", None) == con
                                and _active_prof(con) == pnum)

                    def _cycle_screen_size(con, pnum, step):
                        cfg  = _get_prof_cfg(con, pnum)
                        keys = [k for k, _label, _f in SCREEN_SIZE_OPTIONS]
                        cur  = cfg.get("screen_size", "full")
                        idx  = keys.index(cur) if cur in keys else 0
                        idx  = (idx + step) % len(keys)
                        cfg["screen_size"] = keys[idx]
                        db.save_settings()
                        if _is_live_target(con, pnum):
                            game_deck._libretro_screen_size = keys[idx]
                        return keys[idx]

                    def _toggle_border(con, pnum):
                        cfg = _get_prof_cfg(con, pnum)
                        # "gb_border" is the old GB-only config key; read as a
                        # fallback so an existing GB setting isn't lost, but
                        # always write the new shared key going forward.
                        # Default is ON for a profile that's never set this
                        # explicitly; an explicit False someone already saved
                        # is still respected.
                        cur = cfg.get("border_enabled", cfg.get("gb_border", True))
                        cfg["border_enabled"] = not cur
                        # All Profiles Sync: mirror this value onto Profile
                        # `pnum` on every OTHER console (not other profile
                        # numbers on this same console). Consoles that don't
                        # support borders just carry the flag unused.
                        _sync_profile_setting_across_consoles(
                            con, pnum, "border_enabled", cfg["border_enabled"])
                        db.save_settings()
                        if _is_live_target(con, pnum):
                            # set_border_live() both stores the flag for the
                            # next launch AND, since this console is confirmed
                            # to be the one actually running right now, pushes
                            # the change live so it applies immediately without
                            # needing to relaunch.
                            if hasattr(game_deck, "set_border_live"):
                                game_deck.set_border_live(cfg["border_enabled"])
                            else:
                                game_deck._libretro_border_enabled = cfg["border_enabled"]
                        return cfg["border_enabled"]

                    if event.key == pygame.K_w:
                        if profile_row > 0:
                            app_state["games_profile_row"] = profile_row - 1
                        continue
                    elif event.key == pygame.K_s:
                        if profile_row < MAX_PROFILE_ROW:
                            app_state["games_profile_row"] = profile_row + 1
                        continue
                    elif event.key in (pygame.K_a, pygame.K_d):
                        # Screen Size used to cycle here on A/D — it's an
                        # Enter-only cycle now too (see K_RETURN below),
                        # consistent with every other multi-choice row.
                        continue
                    elif event.key == pygame.K_RETURN:
                        is_active = (_active_prof(console_key) == profile_num)
                        if profile_row == 0:
                            # Set as active profile
                            _set_active_prof(console_key, profile_num)
                            print(f"[GAMES] Profile {profile_num} now active for {console_key}")
                        elif profile_row == 1:
                            # Save Now
                            if game_deck is not None and hasattr(game_deck, "libretro_save_to_profile"):
                                ok = game_deck.libretro_save_to_profile(console_key, profile_num)
                                msg = f"SAVED TO PROFILE {profile_num}" if ok else "SAVE FAILED"
                                if hasattr(game_deck, "_libretro_osd_msg"):
                                    game_deck._libretro_osd_msg   = msg
                                    game_deck._libretro_osd_until = pygame.time.get_ticks() + 2000
                        elif profile_row == 2:
                            # Load Now
                            if game_deck is not None and hasattr(game_deck, "libretro_load_from_profile"):
                                ok = game_deck.libretro_load_from_profile(console_key, profile_num)
                                msg = f"LOADED PROFILE {profile_num}" if ok else "NO SAVE / FAILED"
                                if hasattr(game_deck, "_libretro_osd_msg"):
                                    game_deck._libretro_osd_msg   = msg
                                    game_deck._libretro_osd_until = pygame.time.get_ticks() + 2000
                        elif profile_row == 3 and is_active:
                            cfg = _get_prof_cfg(console_key, profile_num)
                            cfg["auto_save"] = not cfg.get("auto_save", False)
                            # All Profiles Sync: mirror this value onto Profile
                            # `profile_num` on every OTHER console (not other
                            # profile numbers on this same console).
                            _sync_profile_setting_across_consoles(
                                console_key, profile_num, "auto_save", cfg["auto_save"])
                            db.save_settings()
                            if _is_live_target(console_key, profile_num):
                                game_deck._libretro_auto_save = cfg["auto_save"]
                        elif profile_row == 4 and is_active:
                            cfg = _get_prof_cfg(console_key, profile_num)
                            cfg["auto_load"] = not cfg.get("auto_load", False)
                            # All Profiles Sync: mirror this value onto Profile
                            # `profile_num` on every OTHER console (not other
                            # profile numbers on this same console).
                            _sync_profile_setting_across_consoles(
                                console_key, profile_num, "auto_load", cfg["auto_load"])
                            db.save_settings()
                            if _is_live_target(console_key, profile_num):
                                game_deck._libretro_auto_load = cfg["auto_load"]
                        elif profile_row == SCREEN_SIZE_ROW and is_active:
                            _cycle_screen_size(console_key, profile_num, 1)
                        elif profile_row == BORDER_ROW and is_active:
                            _toggle_border(console_key, profile_num)
                        elif profile_row == BACK_ROW:
                            app_state["menu_layer"] = "GAMES_SUB_MENU"
                        pygame.event.clear(pygame.KEYDOWN)
                        continue

# PART 17 OF 28: VIDEO OPTION SETTINGS INPUT ROUTERS
# ==============================================================================

                # --- MENU LAYER 5: TAB 3 VIDEO ADJUSTMENTS AND SLIDERS ---
                elif app_state["menu_layer"] == "VIDEO_ROWS":
                    cur_row = app_state.get("menu_selection_index", 0)
                    # RE-ALIGNED TO 5 SLIDERS ONLY: 0=Brightness, 1=Contrast, 2=Color, 3=Sharpness, 4=Tint, 5=Close Button
                    
                    if event.key == pygame.K_w:
                        if cur_row == 0:
                            app_state["menu_layer"] = "TAB_SELECTION"
                        else:
                            app_state["menu_selection_index"] = cur_row - 1
                        continue
                    elif event.key == pygame.K_s:
                        if cur_row < 5:
                            app_state["menu_selection_index"] = cur_row + 1
                        continue
                    elif event.key in (pygame.K_d, pygame.K_a):
                        if cur_row >= 0 and cur_row <= 4:
                            video_keys_dict = {
                                0: "brightness", 
                                1: "contrast", 
                                2: "color", 
                                3: "sharpness", 
                                4: "tint"
                            }
                            cfg_key = video_keys_dict[cur_row]
                            
                            current_val = db.config.get(cfg_key, 50)
                            if event.key == pygame.K_d:
                                next_val = current_val + 5
                            else:
                                next_val = current_val - 5
                                
                            db.config[cfg_key] = max(0, min(100, (next_val // 5) * 5))
                            # Was db.save_settings() -- a synchronous full
                            # config+channels JSON write on every single
                            # keypress, which is exactly the felt video
                            # hiccup on slower disks. The held-key repeat
                            # path just below (PART 28-ish, keys held >200ms)
                            # already uses this same debounced call; this
                            # single-tap path had just been missed.
                            _mark_settings_dirty()
                        continue
                    elif event.key == pygame.K_RETURN:
                        if cur_row == 5:  # Close Button
                            app_state["show_menu"] = False
                            app_state["menu_shown_at"] = 0
                            pygame.event.clear(pygame.KEYDOWN)
                            continue

                # --- MENU LAYER 6: TAB 4 THEME PALETTE ADJUSTMENT ROUTERS ---
                elif app_state["menu_layer"] == "THEME_ROWS":
                    cur_row = app_state.get("menu_selection_index", 0)
                    # SWAPPED INDEX LOGIC TO MATCH VISUALS: 0=Menu Transparency, 1=Background Hue, 2=Border Hue, 3=Channel Transition Type, 4=Close Button
                    
                    if event.key == pygame.K_w:
                        if cur_row == 0:
                            app_state["menu_layer"] = "TAB_SELECTION"
                        else:
                            app_state["menu_selection_index"] = cur_row - 1
                        continue
                    elif event.key == pygame.K_s:
                        if cur_row < 4:
                            app_state["menu_selection_index"] = cur_row + 1
                        continue
                    elif event.key in (pygame.K_d, pygame.K_a):
                        if cur_row >= 0 and cur_row <= 2:
                            if cur_row == 0:
                                # ROW 0 IS NOW TRANSPARENCY: Scales up/down by 5 points between 0 and 100
                                current_val = db.config.get("menu_opacity", 50)
                                modifier = 5 if event.key == pygame.K_d else -5
                                next_val = current_val + modifier
                                db.config["menu_opacity"] = max(0, min(100, (next_val // 5) * 5))
                            else:
                                # ROW 1 & 2 ARE HUES: Scale between 0 and 360 degrees
                                cfg_key = "theme_bg_hue" if cur_row == 1 else "theme_ui_hue"
                                current_val = db.config.get(cfg_key, 220)
                                modifier = 5 if event.key == pygame.K_d else -5
                                next_val = current_val + modifier
                                db.config[cfg_key] = max(0, min(360, (next_val // 5) * 5))
                                
                            # Was db.save_settings() -- same synchronous
                            # full-database write as the VIDEO_ROWS case
                            # above; the held-key repeat path for these same
                            # theme sliders already uses the debounced call.
                            _mark_settings_dirty()
                        # ROW 3 (Channel Transition Type) is a BUTTON now, not a
                        # left/right slider -- it no longer reacts to A/D at all,
                        # same as every other button row in this app (Remote
                        # Remapping, Close, etc). Only Enter changes it -- see
                        # the K_RETURN branch just below.
                        continue
                    elif event.key == pygame.K_RETURN:
                        if cur_row == 3:  # Channel Transition Type button
                            current_tt = db.config.get("transition_type", "black")
                            db.config["transition_type"] = "static" if current_tt == "black" else "black"
                            _mark_settings_dirty()
                            continue
                        if cur_row == 4:  # Theme Tab Close Button
                            app_state["show_menu"] = False
                            app_state["menu_shown_at"] = 0
                            pygame.event.clear(pygame.KEYDOWN)
                            continue

# ==============================================================================
# PART 18 OF 28: THEME ADJUSTMENTS AND GRADIENT HUE CONTROLLERS
# ==============================================================================

                # --- MENU LAYER 6: TAB 5 THEME COLOR SLIDER CONTROLLERS ---
                elif app_state["menu_layer"] == "THEME_ROWS":
                    cur_row = app_state.get("menu_selection_index", 0)  # 0=BG Slider, 1=UI Slider, 2=Close Button
                    
                    # Duplicate Part 18 THEME block removed; handled by Part 17 WASD above

        # ── GAMES_CTRL_MAP: route mouse clicks to the mapping UI ─────────────
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if (app_state.get("show_menu") and
                    app_state.get("menu_layer") == "GAMES_CTRL_MAP" and
                    game_deck is not None):
                game_deck.handle_mapping_click(event.pos)

# ==============================================================================
# PART 19a OF 28: TV SCREEN ALIGNED LOADING BAR
# ==============================================================================

    if app_state["is_loading"]:
        pygame.event.pump()
        
        if "load_start_time" not in app_state:
            app_state["load_start_time"] = pygame.time.get_ticks()
        # NOTE: Joystick init has been moved to _background_subsystem_worker()
        # so it never blocks the main thread. _joystick_init_done is set there.
        
        # FIXED BOOT LAG: require DB, visualizer, AND the boot-channel path
        # prewarm to be ready - VLC loads lazily on first video channel, so
        # it isn't one of the gates. The prewarm gate is what actually fixes
        # the black screen: without it, the bar could hit 100% and dismiss
        # while _get_channel_ordered_content() still had thousands of cold
        # os.path.exists() calls left to do on the main thread.
        NUM_BOOT_SUBSYSTEMS = 3
        subsystems_ready = 0
        if db is not None and getattr(db, 'ready_flag', False):
            subsystems_ready += 1
        if visualizer_deck is not None:
            subsystems_ready += 1
        if app_state.get("_boot_prewarm_done", False):
            subsystems_ready += 1
        
        # Calculate smooth timeline progression over minimum 2 seconds (reduced from 5)
        elapsed = pygame.time.get_ticks() - app_state["load_start_time"]
        smooth_pct = min(100, int((elapsed / 2000.0) * 100))
        
        real_subsystems_loaded = (subsystems_ready == NUM_BOOT_SUBSYSTEMS)
        if real_subsystems_loaded:
            app_state["load_progress"] = smooth_pct
        else:
            # Still-loading subsystems (most commonly the path prewarm, on a
            # channel with a lot of newly-added media) hold the bar below
            # 100% instead of letting the animation finish and dismiss on
            # its own. This is what keeps the bar visibly moving/waiting for
            # as long as it actually takes, rather than either sitting at a
            # frozen-looking 100% or disappearing into a black screen before
            # the real work is done.
            app_state["load_progress"] = min(95, smooth_pct)
        
        # Build active theme from hue slider values
        if db is not None and getattr(db, 'ready_flag', False):
            import colorsys
            bg_hue = db.config.get("theme_bg_hue", 220) / 360.0
            ui_hue = db.config.get("theme_ui_hue", 140) / 360.0
            r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
            ui_color = (int(r * 255), int(g * 255), int(b * 255))
            r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
            bg_color = (int(r * 255), int(g * 255), int(b * 255))
            active_theme = {"ui": ui_color, "bg": bg_color, "text": (255, 255, 255)}
        else:
            active_theme = {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)}
        
        # The loading bar and percentage ALWAYS stay green, regardless of the
        # selected UI theme. Only the background tint follows the theme.
        LOADING_GREEN = (0, 255, 128)
        if loading_bg_surface:
            w_w, w_h = screen.get_size()
            _lbg_w, _lbg_h = loading_bg_surface.get_size()
            dest_w, dest_h, dest_x, dest_y = _letterbox_fit(_lbg_w, _lbg_h, w_w, w_h)
            screen.fill((1, 1, 1))  # bar color — not pure black, which is reserved as a transparency key elsewhere
            scaled = pygame.transform.scale(loading_bg_surface, (dest_w, dest_h))
            screen.blit(scaled, (dest_x, dest_y))

            # SCALE-AWARE DIMENSIONS: every fixed pixel value below (bar size,
            # offsets, font size) was tuned against the artwork at its native
            # pixel size. `art_scale` is how much smaller/larger the artwork
            # is actually drawn right now (1.0 at native size, <1.0 in a
            # small windowed boot), so multiplying every constant by it keeps
            # the bar and "##%" label locked in the same relative spot on the
            # TV art at any window size instead of just at one size.
            art_scale = dest_w / _lbg_w if _lbg_w else 1.0

            stretched_w = max(60, int(160 * art_scale))
            bar_h = max(3, int(10 * art_scale))
            offset_left = int(14 * art_scale)
            offset_up = int(6 * art_scale)

            # FIXED POSITION ANCHORS: Shifted left and pulled up slightly to trace your green line!
            # Anchored to the letterboxed picture area (dest_*) so the line stays aligned with the
            # artwork itself even when it's pillarboxed on a non-16:9 screen, instead of the raw window.
            bar_x = (dest_x + (dest_w - stretched_w) // 2) - offset_left
            bar_y = dest_y + int(dest_h * 0.72) - offset_up
            
            # Draw the outer background container rail tracker slot
            pygame.draw.rect(screen, (20, 30, 20), (bar_x, bar_y, stretched_w, bar_h), border_radius=3)
            
            # Draw the inner active loading filled progress meter bar
            if app_state["load_progress"] > 0:
                fill_w = int(stretched_w * (app_state["load_progress"] / 100.0))
                pygame.draw.rect(screen, LOADING_GREEN, (bar_x, bar_y, fill_w, bar_h), border_radius=3)

# ==============================================================================
# PART 19b OF 28: SNUG % TEXT COUPLINGS
# ==============================================================================

            # FIXED TEXT CONFIG: Render clear bold progress text labels, font
            # size scaled the same way as the bar above so it shrinks/grows
            # together with the artwork instead of staying a fixed 18pt.
            # 4:3 mode skips this label entirely -- the boot screen artwork's
            # letterboxing leaves no room for it there anymore, and the bar
            # alone is enough to show progress. 16:9 is untouched.
            _loading_aspect = (db.config.get("aspect_ratio", DETECTED_ASPECT_RATIO)
                                if (db is not None and getattr(db, "ready_flag", False))
                                else DETECTED_ASPECT_RATIO)
            if _loading_aspect != "4:3":
                font_size = max(9, int(18 * art_scale))
                text_gap = max(2, int(4 * art_scale))
                f_load = _cached_sys_font("Courier New", font_size, bold=True)
                txt_str = f"{app_state['load_progress']}%"
                lbl_txt = f_load.render(txt_str, True, LOADING_GREEN)

                # FIXED POSITION OFFSET: Tightened padding right above the line!
                txt_x = (dest_x + (dest_w - lbl_txt.get_width()) // 2) - offset_left
                txt_y = bar_y - lbl_txt.get_height() - text_gap
                screen.blit(lbl_txt, (txt_x, txt_y))
        else:
            green_loading_theme = {"ui": LOADING_GREEN, "bg": active_theme["bg"], "text": (255, 255, 255)}
            render_loading_screen(screen, app_state["load_progress"], green_loading_theme)

        # FIXED TIMING GATE: minimum 2s animation, plus db + visualizer + boot-channel
        # prewarm all ready (VLC is intentionally excluded - it loads lazily on first
        # video channel and isn't needed for change_channel() below to run fast)
        if elapsed >= 2000 and subsystems_ready == NUM_BOOT_SUBSYSTEMS:
            print("[TELEMETRY - PART 14] Preloader finished. Database records safely buffered from disk.")
            app_state["is_loading"] = False
            app_state["show_splash"] = db.config.get("show_controls_on_launch", True)
            
            # FORCE CAROUSEL SYNCHRONIZER: Sync visibility mappings live from disk configuration properties
            if 'game_deck' in globals() and game_deck is not None:
                try:
                    game_deck.refresh_from_config(db.config)
                    print("[PERSISTENCE HOOK] Synchronized game_deck configurations with disk records on launch.")
                except Exception as deck_sync_err:
                    print(f"[PERSISTENCE HOOK ERROR] Could not sync game_deck options on boot: {deck_sync_err}")

            # Safe boot trigger sequence — restore last channel if it was a real
            # TV station (ch06+). Ch03 (games) and ch04 (guide) are skipped on
            # boot; first-time launch (no last_channel saved) defaults to ch05.
            _last_ch = str(db.config.get("last_channel", "05")).zfill(2) if db else "05"
            if _last_ch in ("03", "04") or not _last_ch:
                _last_ch = "05"
            app_state["current_channel"] = _last_ch
            change_channel(_last_ch, is_boot=True)
            
        pygame.display.flip()
        clock.tick(60)
        continue

# ==============================================================================
# PART 20 OF 28: MAIN VIEWPORT SCREEN DIMENSIONS TRACKING & BLIT ROUTERS
# ==============================================================================

    w_w, w_h = screen.get_size()
    ch = app_state["current_channel"]
    
    # Calculate 4:3 letterbox dimensions if enabled.
    # content_rect comes from _compute_content_rect (shared with the DVD
    # leave-frame capture): in 4:3 Test Mode it's the border's screen cutout
    # (border on) or the centered 4:3 letterbox region (border off); otherwise
    # the whole window. All content (video, static, guide, games, DVD,
    # visualizers) is drawn into it so it pillarboxes/fits the bezel uniformly.
    aspect_ratio = db.config.get("aspect_ratio", "16:9") if db is not None else "16:9"
    if aspect_ratio == "4:3":
        # Black bars fill the 16:9 -> 4:3 letterbox area.
        screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
    content_rect = _compute_content_rect(w_w, w_h)
    
    if db is not None and getattr(db, 'ready_flag', False):
        # Same defensive fallback as the main loop's theme lookup above --
        # ready_flag being True means load_settings_async has finished, so
        # this shouldn't normally miss, but there's no reason to leave a
        # raw KeyError-able lookup sitting here too.
        _theme_key = db.config.get("current_theme", "DefaultBlueGreen")
        active_theme = dict(db.themes.get(_theme_key) or db.themes.get("DefaultBlueGreen")
                             or {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)})
        try:
            import colorsys as _cs
            _ui_hue = db.config.get("theme_ui_hue", 140) / 360.0
            _bg_hue = db.config.get("theme_bg_hue", 220) / 360.0
            _r, _g, _b = _cs.hsv_to_rgb(_ui_hue, 0.9, 1.0)
            active_theme["ui"] = (int(_r * 255), int(_g * 255), int(_b * 255))
            _r, _g, _b = _cs.hsv_to_rgb(_bg_hue, 0.6, 0.15)
            active_theme["bg"] = (int(_r * 255), int(_g * 255), int(_b * 255))
        except Exception as e:
            _log_warn_throttled("theme_hue_recompute", "Live theme hue recompute failed, using unmodified theme colors this frame: %s", e)
    else:
        active_theme = {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)}

    # NOTE: File-explorer overlay is drawn at the END of the frame (see Part 28)
    # so the normal video/static pipeline always runs — static keeps animating
    # and audio keeps playing even while the explorer is open.

    current_playback_state = calculate_slotted_playback_state(ch)
    
    # --- VLC READINESS & END-OF-SHOW WATCHDOG ---
    is_video_finished = False
    if vlc_engine is not None and app_state.get("is_playing_video", False):
        try:
            vlc_state = vlc_engine.player.get_state()
            if vlc_state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                # Any of Ended/Stopped/Error can show up for a brief moment right
                # after a channel switch, because change_channel()/play_file_segmented()
                # call stop() then play() back-to-back and libVLC's state update is
                # asynchronous. This isn't limited to Stopped/Error: if the shared VLC
                # engine was left sitting on a genuinely-finished clip from somewhere
                # else (e.g. a DVD movie that played all the way through while idling
                # on channel 03), get_state() can keep reporting that stale Ended
                # value for a frame or two after we've already called play() on this
                # channel's brand-new video. Without a grace window, the watchdog
                # catches that transient/stale reading and mistakes "we just switched
                # channels" for "the show ended" — which clears the saved playback
                # position and jumps the schedule forward, looking exactly like the
                # show restarting/skipping (or flashing then going black) right after
                # you tune back in.
                last_play_at = app_state.get("last_video_play_started_at", 0)
                if pygame.time.get_ticks() - last_play_at > 1500:
                    is_video_finished = True
            elif vlc_state in (vlc.State.Opening, vlc.State.Buffering):
                # STALL WATCHDOG: a genuinely corrupt/unreadable file can sit in
                # Opening/Buffering forever and NEVER transition to Playing or to
                # Ended/Stopped/Error, so the check above never fires. Nothing
                # else in this function times that state out, which is why a
                # bad file on a given channel just shows a black screen
                # indefinitely instead of eventually being skipped -- the same
                # failure mode _ch03_music_is_stalled() was written to catch for
                # the channel-03 menu music player, just never applied here for
                # regular TV video playback.
                last_play_at = app_state.get("last_video_play_started_at", 0)
                if last_play_at > 0 and pygame.time.get_ticks() - last_play_at > 8000:
                    print(f"[WATCHDOG] Video stalled in Opening/Buffering on ch{ch} for 8s+, treating as failed.")
                    is_video_finished = True
                    _stalled_ch_info = db.channels_db.get(str(ch).zfill(2), {}) if db is not None else {}
                    _stalled_file = current_playback_state.get("file", "")
                    if _stalled_file:
                        # A file that never even reaches Playing is almost certainly
                        # unplayable (missing codec, truncated/corrupt download,
                        # etc.) rather than just short -- unlike a clip that ends
                        # early, retrying it later won't help, so exclude it from
                        # future scheduling instead of just skipping past it once.
                        _mark_file_broken(_stalled_ch_info, _stalled_file)
        except Exception as e: print(f"[PLAYBACK] video-finished check failed: {e}")

    if (ch not in ("03", "04")
            and current_playback_state["mode"] in ("Video", "Commercial")
            and (not app_state.get("is_playing_video", False) or is_video_finished)
            and vlc_engine is not None
            and getattr(vlc_engine, "ready_flag", False)
            and not app_state.get("show_menu", False)):
        now_watchdog = pygame.time.get_ticks()
        # React in 300 ms when VLC has definitively ended; use a longer 2500 ms
        # cooldown for the "not yet started" case to avoid hammering VLC at startup.
        _watchdog_ms = 300 if is_video_finished else 2500
        if now_watchdog - app_state.get("last_vlc_retry_at", 0) > _watchdog_ms:
            app_state["last_vlc_retry_at"] = now_watchdog
            if is_video_finished:
                app_state["is_playing_video"] = False
                if db is not None:
                    _ch_entry = db.channels_db.get(str(ch).zfill(2), {})
                    _anchor   = _ch_entry.get("playback_anchor", {})

                    # --- CORRECT-THEN-RECOMPUTE (replaces the old grid-based skip) ---
                    # VLC just told us, definitively, that this file is done. That
                    # makes "how long did it actually just play" ground truth --
                    # seek_offset (where it started) + elapsed wall-clock time since
                    # wall_start (when it started) IS the file's real duration, full
                    # stop, no probe or guess required. We write that back as the
                    # authoritative duration for this exact slot using the same
                    # propagation a real ffmpeg probe uses (_patch_probed_gain) --
                    # the live schedule entry, this block's frozen
                    # sched_cached_durations, AND the global cross-channel
                    # media_probe_cache all get corrected in one shot.
                    #
                    # Once that's done there is nothing left to "skip past": the
                    # cumulative timeline this exact same function builds from
                    # sched_cached_tracks/sched_cached_durations now has the RIGHT
                    # length for the slot that just ended, so simply clearing the
                    # anchor and letting the next calculate_slotted_playback_state
                    # call run fresh lands precisely on the next show at (near)
                    # seek 0 -- no assumed-duration grid math, no half-hour jumps,
                    # no dead air. This replaces the old block-position "skip_to"
                    # calculation (and Marathon's separate additive-nudge variant)
                    # with one unified path that works identically for every block
                    # type, including Marathon.
                    if _anchor.get("wall_start") is not None and _anchor.get("file"):
                        _now_w = datetime.datetime.now()
                        _wall_now = _now_w.hour * 3600 + _now_w.minute * 60 + _now_w.second
                        _elapsed = _wall_now - _anchor["wall_start"]
                        if _elapsed < 0:
                            _elapsed += 86400  # midnight rollover while this file was playing
                        _real_dur = int(_anchor.get("seek_offset", 0) + _elapsed)
                        _ab = _anchor.get("block", "Morning")
                        if _real_dur > 0:
                            try:
                                _patch_probed_gain(
                                    "channel", str(ch).zfill(2), _ab,
                                    os.path.normpath(str(_anchor["file"])),
                                    gain=None, duration=_real_dur,
                                )
                                print(f"[WATCHDOG] {os.path.basename(str(_anchor['file']))} on ch{ch} "
                                      f"actually ran {_real_dur}s -- corrected and advancing to next show.")
                            except Exception as e:
                                log.warning("WATCHDOG: failed to correct real duration for ch%s: %s", ch, e)

                    # Clear the stale anchor so the scheduler doesn't re-serve the just-ended file.
                    _ch_entry["playback_anchor"] = {}

            change_channel(ch, is_surfing=False, internal_advance=True)
            current_playback_state = calculate_slotted_playback_state(ch)

    # --- MID-STREAM SCHEDULE CHANGE DETECTION (interruption breaks) ---
    # play_file_segmented_core() (media.py) only ever sets a VLC "start-time"
    # option -- it never sets a stop time -- so once a clip starts playing it
    # runs to its own real end no matter how long the scheduler allotted it
    # in the timeline. That meant a mid-episode commercial break (the first
    # half of a show, deliberately cut short at its half-way point in
    # calculate_slotted_playback_state's timeline) was computed correctly by
    # the scheduler but never actually happened on screen: VLC just kept
    # streaming the same episode file straight through the break, because
    # the ONLY thing that forced a new play() call mid-viewing was the
    # watchdog above noticing VLC had reported the file was truly Ended --
    # which only happens once the full underlying video file finishes, not
    # at the scheduler's intended cut point. This check compares what VLC
    # currently has loaded against what the schedule says should be playing
    # right now, and resyncs playback the instant they diverge (a file
    # change always means either an interruption break started, or a break
    # just ended and the show resumed). It intentionally skips the OSD/
    # black-flash/digit-buffer side effects that a real channel_change()
    # triggers, since this isn't a channel change -- it's the same channel
    # cutting to a scheduled commercial and back, exactly like real TV.
    if (ch not in ("03", "04")
            and current_playback_state["mode"] in ("Video", "Commercial")
            and app_state.get("is_playing_video", False)
            and vlc_engine is not None
            and getattr(vlc_engine, "ready_flag", False)
            and not app_state.get("show_menu", False)
            and current_playback_state.get("file")
            # Grace period: VLC is async -- get_media() still returns the OLD
            # file for ~300–500 ms after play_file_segmented() is called.  If we
            # compare immediately, the old path != new path → spurious resync →
            # play_file_segmented called a SECOND time → VLC flashes through
            # Stopped briefly → watchdog fires "ended early" → giant forward skip.
            # Two seconds is generous but harmless: legitimate mid-episode commercial
            # breaks always last longer than 2 s, so we never miss a real break.
            and pygame.time.get_ticks() - app_state.get("last_video_play_started_at", 0) > 2000):
        try:
            current_media = vlc_engine.player.get_media()
            loaded_path = ""
            if current_media:
                from urllib.parse import unquote
                loaded_path = os.path.normpath(unquote(current_media.get_mrl().replace("file:///", ""))).lower()
            sched_path = os.path.normpath(current_playback_state["file"]).lower()
            if loaded_path and loaded_path != sched_path:
                print(f"[SCHEDULE] Mid-stream content change on ch{ch}: "
                      f"{os.path.basename(loaded_path)} -> {os.path.basename(sched_path)}")
                vlc_engine.play_file_segmented(current_playback_state["file"], current_playback_state.get("seek", 0), gain=_lookup_file_gain(ch, current_playback_state["file"]))
                vlc_engine.set_volume(db.config["global_volume"], db.config.get("is_muted", False))
                app_state["last_video_play_started_at"] = pygame.time.get_ticks()
                if db is not None:
                    _now_a = datetime.datetime.now()
                    _wall_a = _now_a.hour * 3600 + _now_a.minute * 60 + _now_a.second
                    db.channels_db[str(ch).zfill(2)]["playback_anchor"] = {
                        "file":            current_playback_state["file"],
                        "wall_start":      _wall_a,
                        "seek_offset":     current_playback_state.get("seek", 0),
                        # slot_start_seek: matches the new slot's seek_offset from the
                        # timeline entry (e.g. mid_point for a split-show second half).
                        # The anchor remaining formula uses this to cancel out the slot's
                        # own file offset so remaining = slot_duration - elapsed (not
                        # slot_duration - slot_file_offset - elapsed which was always 0
                        # the moment a second-half segment's anchor was stamped).
                        "slot_start_seek": current_playback_state.get("seek_slot_offset", 0),
                        "duration":        current_playback_state.get("duration", 1800),
                        "mode":            current_playback_state.get("mode", "Video"),
                        "block":           current_playback_state.get("block", "Morning"),
                    }
                    try:
                        db.save_settings()
                    except Exception as e:
                        log.warning("Mid-stream resync: failed to persist playback_anchor for ch%s: %s", ch, e)
        except Exception as e:
            print(f"[SCHEDULE] Mid-stream resync check failed: {e}")
    
    if w_w < 100 or w_h < 100:
        screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
        w_w, w_h = 100, 100

    if ch != "04":
        if ch == "03":
            # On ch03 the main engine should never be playing -- the DVD runs
            # on its own dvd_engine, so unconditionally stop the main engine
            # here (idempotent). The old "not dvd_active" guard only existed
            # because the DVD used to share this engine.
            if vlc_engine is not None:
                try:
                    if vlc_engine.player.is_playing():
                        vlc_engine.stop()
                except Exception as e: print(f"[PLAYBACK] vlc stop failed: {e}")
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
            
            screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
            
            if game_deck is not None:
                # Our custom frontend draws the console carousel / rom list itself.
                # When an emulator is embedded (GAME mode) it draws nothing and the
                # child window shows through.
                game_deck.set_theme(active_theme.get("ui", (0, 255, 128)))
                crt_cfg = {}
                if db is not None:
                    crt_cfg = {
                        "brightness": db.config.get("brightness", 50),
                        "contrast":   db.config.get("contrast",   50),
                        "saturation": db.config.get("color",      50),
                        "sharpness":  db.config.get("sharpness",  50),
                        "hue_shift":  db.config.get("tint",       50),
                    }
                if game_deck.emulator_loading:
                    game_deck._libretro_tick_loading()
                    game_deck.render(screen, content_rect)
                elif (game_deck.mode == "GAME"
                      and getattr(game_deck, "_libretro_core", None) is not None):
                    _menu_overlay_open = (
                        app_state.get("show_menu", False)
                        or app_state.get("show_splash", False)
                        or app_state.get("show_quit_confirm", False)
                        or app_state.get("show_restart_confirm", False)
                    )
                    game_deck.render_libretro(screen, content_rect, crt_cfg=crt_cfg,
                                               overlay_open=_menu_overlay_open)
                else:
                    game_deck.render(screen, content_rect)
            else:
                font_loading = _cached_sys_font("Courier New", 24, bold=True)
                theme_color = active_theme.get("ui", (0, 255, 128))
                lbl = font_loading.render("Game frontend unavailable", True, theme_color)
                screen.blit(lbl, (content_rect.centerx - lbl.get_width() // 2, content_rect.centery - 12))
        elif current_playback_state["mode"] == "Video":
            if vlc_engine is not None:
                try:
                    vlc_bytes = vlc_engine.get_display_frame()
                    if vlc_bytes is not None and len(vlc_bytes) >= vlc_engine.buf_size:
                        video_surf = pygame.image.frombuffer(vlc_bytes, (vlc_engine.width, vlc_engine.height), "BGRA")
                        if video_surf.get_width() > 0 and video_surf.get_height() > 0:
                            
                            brightness, contrast, color_sat, sharpness, tint, fx_active = _get_video_effects(db)

                            # Correct pipeline: apply effects on the raw VLC-sized surface
                            # (small, e.g. 854x480), THEN smoothscale once to display size.
                            # Old pipeline did smoothscale-up THEN effects THEN scale-down THEN
                            # scale-up again — that was two extra scales causing blur and lag.
                            if fx_active:
                                effected_surf = apply_video_effects(video_surf, brightness, contrast, color_sat, sharpness, tint)
                                final_video_surf = pygame.transform.smoothscale(effected_surf, (content_rect.width, content_rect.height))
                            else:
                                effected_surf = video_surf
                                final_video_surf = pygame.transform.smoothscale(video_surf, (content_rect.width, content_rect.height))

                            screen.blit(final_video_surf, (content_rect.x, content_rect.y))
                            # Cache the post-effects surface, not the raw decode — otherwise
                            # the bridge frame used during buffering/seek gaps briefly shows
                            # unprocessed color before the next real frame snaps effects back on.
                            _store_last_video_frame(effected_surf)
                        else:
                            if not _blit_last_video_frame(screen, content_rect.x, content_rect.y, content_rect.width, content_rect.height):
                                screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
                    else:
                        if not _blit_last_video_frame(screen, content_rect.x, content_rect.y, content_rect.width, content_rect.height):
                            screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
                except Exception:
                    # Per-frame render fallback — runs in the hot draw loop, so stay
                    # silent instead of spamming the console every frame on failure.
                    if not _blit_last_video_frame(screen, content_rect.x, content_rect.y, content_rect.width, content_rect.height):
                        screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
            else:
                screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
                
        elif current_playback_state["mode"] == "Static":
            # SINGLE SOURCE OF TRUTH: _render_transition_static() is the
            # exact same noise+audio-loop implementation the channel-switch
            # and TV-guide transitions now borrow (see that function's
            # docstring). Kept as a plain-rect fallback fill only if the
            # content area is degenerate (zero-sized).
            _render_transition_static(screen, content_rect)

# ==============================================================================
# PART 21 OF 28: TV VISUALIZER MATRIX ENGINE AND OVERLAY TEXT DRAW FLUIDS
# ==============================================================================

        elif current_playback_state["mode"] == "Visualizer" and visualizer_deck is not None:
            target_file = current_playback_state.get("file", "")
            target_seek = current_playback_state.get("seek", 0)
            track_duration = current_playback_state.get("duration", 210)
            ch_conf = db.channels_db.get(str(ch).zfill(2), {}) if db else {}
            
            active_track = app_state.get("active_music_track_file", "")
            last_scheduled_vis_track = app_state.get("last_scheduled_vis_track", {}).get(str(ch).zfill(2), "")
            is_new_scheduled_track = last_scheduled_vis_track != target_file
            
            # --- FIXED DROPOUT PASS: DYNAMIC TRACK NAME TARGETING FALLBACKS ---
            display_track_path = active_track if active_track else target_file
            
            # TRACK DATABASE FALLBACK: If both are blank, read directly from the database profile schedules
            if not display_track_path and db and "Music Tracks" in ch_conf.get("schedules", {}):
                tracks_list = ch_conf["schedules"]["Music Tracks"]
                if tracks_list:
                    # Grab the very first loaded track item from your files profile array list
                    first_track = tracks_list[0]
                    display_track_path = first_track.get("path", "") if isinstance(first_track, dict) else str(first_track)
            
            if display_track_path:
                clean_track_name = os.path.basename(display_track_path)
                clean_track_name = os.path.splitext(clean_track_name)[0]
                clean_track_name = clean_track_name.replace("_", " ").upper()
            else:
                clean_track_name = "LIVE MUSIC FEED"

            # --- FIXED PERFORMANCE CRASH LINE MATCH ENGINE: render_active_engine ---
            theme_ui_color = active_theme.get("ui", (0, 255, 128))
            
            # Extract the focused viewport content surface map securely
            try:
                vis_surface = screen.subsurface(content_rect)
                # Call your true drawing engine function name to prevent AttributeError crashes
                visualizer_deck.render_active_engine(vis_surface, theme_ui_color)
            except (ValueError, pygame.error) as vis_sub_err:
                print(f"[VIS PART21] subsurface render skipped: {vis_sub_err}")
                vis_surface = None
            
            # Draw overlay textual tags at the upper left window boundary coordinates
            font_vis = _cached_sys_font("Courier New", 17, bold=True)
            text_surf_ch = font_vis.render(f"CH {str(ch).zfill(2)} {ch_conf.get('name', 'STATION').upper()}", True, theme_ui_color)
            text_surf_track = font_vis.render(f"NOW PLAYING: {clean_track_name}", True, (255, 255, 255))
            
            screen.blit(text_surf_ch, (content_rect.x + 20, content_rect.y + 20))
            screen.blit(text_surf_track, (content_rect.x + 20, content_rect.y + 45))
            
            # NOTE: This block used to write app_state["last_scheduled_vis_track"]
            # here whenever it saw a new track. That's the SAME shared flag that
            # PART 25 (further down, in the second if/elif chain) reads to decide
            # whether to reroll the "Random" visualizer style. Because this block
            # runs earlier in the frame, it was consuming/clearing the "new track"
            # signal before PART 25 ever got to see it, so PART 25's
            # `if is_new_scheduled_track: visualizer_deck.current_running_style =
            # visualizer_deck.get_next_random_style()` almost never fired — every
            # "Random" channel just sat on whatever current_running_style
            # (default "PrismShards") happened to already be set. This block only
            # draws into content_rect, which PART 25 immediately paints over with
            # a full-screen render anyway, so it has no reason to touch that
            # shared flag at all. Bookkeeping for last_scheduled_vis_track (and
            # the resulting style reroll) is now owned solely by PART 25.

# ==============================================================================
# PART 22 OF 28: TV GUIDE PREVIEW VIDEO CHANNEL HANDLERS
# ==============================================================================

    elif ch == "04":
        # Pass content_rect so the guide fills the 4:3 pillarboxed region (and
        # sits inside the TV border) in 4:3 Test Mode instead of stretching
        # across the full 16:9 window. In 16:9 / true-4:3 modes content_rect IS
        # the whole window, so this is unchanged there. The live preview below
        # is blitted at guide_preview_bounds, which render_tv_guide has already
        # offset into content_rect, so it follows the guide automatically.
        render_tv_guide(screen, app_state, db, active_theme, target_rect=content_rect)
        
        px, py, pw, ph = app_state.get("guide_preview_bounds", (w_w // 2 - 400, 15, 800, 450))
        hl_ch = str(app_state.get("highlighted_guide_channel", "05")).zfill(2)
        hl_state = calculate_slotted_playback_state(hl_ch)

        # Draw clean base canvas container block with no outline framing
        pygame.draw.rect(screen, (0, 0, 0), (px, py, pw, ph))

        # AUDIO TIMING FIX: while the black/static transition shield is still
        # covering the screen (see the shield gate further down, which is what
        # actually renders/loops the static), this preview block used to stop
        # static_audio and kick off the preview's own VLC/music audio on every
        # single frame regardless of the shield -- so the guide's real audio
        # started under the static instead of after it, and you'd hear it (or
        # a mix of both) well before the static's second was up. Gate the
        # audio-STARTING parts below on the shield having actually expired;
        # the visuals are unaffected since the shield already paints over them.
        _ch04_shield_active = (
            pygame.time.get_ticks() < app_state.get("channel_switch_black_until", 0)
            or pygame.time.get_ticks() < app_state.get("guide_preview_static_until", 0)
        )

        if hl_state["mode"] in ("Video", "Commercial") and vlc_engine is not None and not _ch04_shield_active:
            try:
                if 'static_audio' in globals() and static_audio is not None: static_audio.stop()
                if pygame.mixer.music.get_busy(): pygame.mixer.music.stop()
                
                if app_state.get("loaded_preview_path_cache", "") != hl_state["file"]:
                    vlc_engine.stop()
                    # calculate_slotted_playback_state already advances seek by wall-clock
                    # elapsed since the anchor was snapped, so the seek value is current.
                    # However VLC takes ~1-1.5s to buffer before the first frame/audio
                    # arrives, which makes the preview sound like a repeat of the last
                    # thing heard on the channel. Add a small forward nudge to land the
                    # preview roughly in sync with where the live channel would be by the
                    # time VLC actually starts playing.
                    _VLC_STARTUP_LATENCY_S = 1.5
                    vlc_engine.play_file_segmented(hl_state["file"], hl_state["seek"] + _VLC_STARTUP_LATENCY_S, gain=_lookup_file_gain(hl_ch, hl_state["file"]))
                    vlc_engine.set_volume(db.config.get("global_volume", 70), db.config.get("is_muted", False))
                    app_state["loaded_preview_path_cache"] = hl_state["file"]

                vlc_bytes = vlc_engine.get_display_frame()
                if vlc_bytes:
                    video_surf = pygame.image.frombuffer(vlc_bytes, (vlc_engine.width, vlc_engine.height), "BGRA")
                    brightness, contrast, color_sat, sharpness, tint, fx_active = _get_video_effects(db)
                    
                    if fx_active:
                        processed_surf = apply_video_effects(video_surf, brightness, contrast, color_sat, sharpness, tint)
                        screen.blit(pygame.transform.smoothscale(processed_surf, (pw, ph)), (px, py))
                    else:
                        processed_surf = video_surf
                        screen.blit(pygame.transform.smoothscale(video_surf, (pw, ph)), (px, py))
                    # Cache the post-effects surface, not the raw decode — otherwise the
                    # bridge frame used while the preview buffers on channel-switch briefly
                    # flashes unprocessed color before effects snap back on next real frame.
                    _store_last_video_frame(processed_surf)
                    # Also remember this specific CHANNEL's frame (see
                    # _store_guide_channel_frame) so scrolling back to it later in
                    # the session bridges instantly instead of going black again.
                    _store_guide_channel_frame(hl_ch, processed_surf)
                else:
                    # Prefer THIS channel's own last-known frame over the generic
                    # single-slot bridge -- a channel already seen this session
                    # shows its real last frame instead of whatever channel was
                    # previewed immediately before it.
                    if not _blit_guide_channel_frame(hl_ch, screen, px, py, pw, ph) \
                            and not _blit_last_video_frame(screen, px, py, pw, ph):
                        # BUG FIX: this used to ALWAYS grey-fill the preview box while
                        # VLC buffered its first frame, regardless of transition style.
                        # With the "static" transition on, the guide-scroll static
                        # burst (guide_preview_static_until) would end while VLC was
                        # still buffering (~1-1.5s > the burst window), so the box then
                        # flipped from static to a grey fill -- that's the "both static
                        # AND grey" the user saw. Mirror the live-channel buffering
                        # bridge (see PART 24, _transition_style_wait == "static"):
                        # keep drawing static during the buffering gap when static is
                        # selected, and only grey-fill when the transition is "black".
                        if db is not None and db.config.get("transition_type", "black") == "static":
                            _render_transition_static(screen, pygame.Rect(px, py, pw, ph))
                        else:
                            screen.fill((15, 15, 15), (px, py, pw, ph))
            except Exception:
                # Per-frame render path — don't log (would spam every frame); just
                # draw the fallback fill and keep going.
                if not _blit_guide_channel_frame(hl_ch, screen, px, py, pw, ph) \
                        and not _blit_last_video_frame(screen, px, py, pw, ph):
                    screen.fill((40, 40, 40), (px, py, pw, ph))

# ==============================================================================
# PART 23 OF 28: TV GUIDE PREVIEW VISUALIZER & DIRECT STATION SYNCHRONIZER
# ==============================================================================

        elif hl_state["mode"] == "Visualizer" and visualizer_deck is not None and not _ch04_shield_active:
            try:
                if 'static_audio' in globals() and static_audio is not None: static_audio.stop()
                if app_state.get("loaded_preview_path_cache", "") != "":
                    if vlc_engine is not None: vlc_engine.stop()
                    app_state["loaded_preview_path_cache"] = ""

                # Forces the TV Guide preview to look at what the live channel is playing right now
                live_channel_state = calculate_slotted_playback_state(app_state.get("current_channel", "07"))
                
                if live_channel_state.get("mode") == "Visualizer" and pygame.mixer.music.get_busy():
                    # Pull the exact same file path and timeline position from the running station
                    target_file = live_channel_state.get("file", "")
                    target_seek_seconds = live_channel_state.get("seek", 0)
                    track_duration_max = live_channel_state.get("duration", 210)
                    _gain_ch_key = app_state.get("current_channel", "07")
                else:
                    # Fallback to standard timeline targets if browsing a channel you aren't actively watching
                    target_file = hl_state.get("file", "")
                    target_seek_seconds = hl_state.get("seek", 0)
                    track_duration_max = hl_state.get("duration", 210)
                    _gain_ch_key = hl_ch
                
                guide_preview_track = app_state.get("guide_preview_music_track", "")
                
                if target_file and (guide_preview_track != target_file or not pygame.mixer.music.get_busy()):
                    pygame.mixer.Channel(1).stop()
                    if pygame.mixer.music.get_busy():
                        pygame.mixer.music.stop()
                    
                    try:
                        pygame.mixer.music.fadeout(50)
                        pygame.time.wait(60)
                        pygame.mixer.music.unload()
                    except Exception as e: print(f"[GUIDE PREVIEW] music fadeout/unload failed: {e}")
                    
                    pygame.mixer.music.load(target_file)
                    # LOUDNESS NORMALIZATION: apply this track's stored gain (from the
                    # ffmpeg loudness probe) on top of the normal volume/mute chain, so
                    # a quiet song previewed in the guide sounds the same loudness as
                    # everything else instead of needing a manual volume bump.
                    app_state["active_music_gain"] = _lookup_file_gain(_gain_ch_key, target_file)
                    pygame.mixer.music.set_volume(_effective_audio_gain(min_db=STACKED_GAIN_MIN_DB) * MUSIC_GAIN * app_state["active_music_gain"])
                    
                    safe_seek = max(0.0, min(float(target_seek_seconds), float(track_duration_max - 10)))
                    try:
                        pygame.mixer.music.play(start=safe_seek)
                    except Exception as e:
                        print(f"[GUIDE PREVIEW] seek to {safe_seek}s failed, restarting from 0: {e}")
                        pygame.mixer.music.play(start=0.0)
                        
                    app_state["guide_preview_music_track"] = target_file
                    # BUG FIX: this used to also write app_state["active_music_track_file"],
                    # which is the SAME cache key PART 25 (the live channel) reads to
                    # decide "has this track already been loaded, and therefore should I
                    # skip re-rolling the visualizer style". Since the guide preview runs
                    # every time you merely highlight a channel (before you ever tune to
                    # it live), it was priming that key with the previewed channel's file
                    # ahead of time -- so the moment you actually tuned there, PART 25 saw
                    # active_cached_track == target_file and skipped its whole style-setup
                    # block. Removed; the guide has its own "guide_preview_music_track" key
                    # for its own gating and doesn't need to touch the live channel's cache.
                    app_state["vis_card_timer"] = pygame.time.get_ticks() + 15000

                mini = pygame.Surface((pw, ph))
                mini.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
                
                h_ch_info = db.channels_db.get(hl_ch, {})
                preview_style = h_ch_info.get("visualizer_style", "Random")
                
                # BUG FIX: this used to fall back to mirroring
                # visualizer_deck.current_running_style (the LIVE tuned
                # channel's style) any time the previewed channel was set to
                # "Random". That's exactly why hovering a second Random
                # visualizer channel in the guide always looked identical to
                # whatever was already playing on the main screen -- there
                # was no independent style for the previewed channel. Now
                # each channel keeps its own entry in style_by_channel,
                # rolled fresh only when THAT channel's own scheduled track
                # changes (same per-channel bookkeeping PART 25 uses for the
                # live channel), so two different Random channels can show
                # two different styles at once.
                if preview_style != "Random":
                    preview_render_style = preview_style
                else:
                    _last_sched_by_ch_prev = app_state.setdefault("last_scheduled_vis_track", {})
                    _prev_track = hl_state.get("file", "")
                    if _last_sched_by_ch_prev.get(hl_ch, "") != _prev_track or hl_ch not in visualizer_deck.style_by_channel:
                        _last_sched_by_ch_prev[hl_ch] = _prev_track
                        visualizer_deck.style_by_channel[hl_ch] = visualizer_deck.get_next_random_style()
                    preview_render_style = visualizer_deck.style_by_channel[hl_ch]
                
                if pygame.mixer.music.get_busy():
                    # FIXED: Cranked up the multiplier timing so shrunken waves bounce with rapid life
                    p_amp = 45 + int(math.sin(pygame.time.get_ticks() * 0.045) * 45) + random.randint(0, 5)
                else:
                    p_amp = 25 + int(math.sin(pygame.time.get_ticks() * 0.008) * 20)
                
                # Render the identical background wave patterns (as a one-off
                # override, so this never touches the live channel's state)
                visualizer_deck.render_active_engine(mini, active_theme, mock_amplitude=p_amp, style_override=preview_render_style)
                
                # Force high-intensity background gradient flashes completely dark on all styles inside preview box
                if hasattr(visualizer_deck, "flash_intensity"): visualizer_deck.flash_intensity = 0.0
                if hasattr(visualizer_deck, "bg_flash"): visualizer_deck.bg_flash = 0
                if hasattr(visualizer_deck, "galaxy_glow"): visualizer_deck.galaxy_glow = 0
                
                brightness, contrast, color_sat, sharpness, tint, fx_active = _get_video_effects(db)
                
                if fx_active:
                    processed_mini = apply_video_effects(mini, brightness, contrast, color_sat, sharpness, tint)
                    screen.blit(processed_mini, (px, py))
                else:
                    screen.blit(mini, (px, py))
                
                f_card = _cached_sys_font("Courier New", 14, bold=True)
                raw_filename = os.path.basename(target_file).upper()
                base_filename_str = os.path.splitext(raw_filename)[0]
                
                import re
                song_name_cleaned = re.sub(r'^[0-9\s\.\-_]+', '', base_filename_str)
                style_name_cleaned = preview_render_style.upper()
                
                # Dynamic theme text color mapping
                import colorsys as _preview_colorsys
                _p_ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140 / 360.0
                _pr, _pg, _pb = _preview_colorsys.hsv_to_rgb(_p_ui_hue, 0.9, 1.0)
                theme_ui_text_color = (int(_pr * 255), int(_pg * 255), int(_pb * 255))
                
                lbl_s1 = f_card.render(song_name_cleaned, True, theme_ui_text_color)
                lbl_s2 = f_card.render(f"VISUALIZER: {style_name_cleaned}", True, theme_ui_text_color)
                
                screen.blit(lbl_s1, (px + 20, py + 20))
                screen.blit(lbl_s2, (px + 20, py + 40))
            except Exception as preview_err:
                print(f"[SYNC ENGINE ERROR] {preview_err}")
                screen.fill((40, 40, 40), (px, py, pw, ph))
                
        elif hl_state["mode"] == "Visualizer_Empty" and not _ch04_shield_active:
            if vlc_engine is not None: vlc_engine.stop()
            if 'static_audio' in globals() and static_audio is not None: static_audio.stop()
            if pygame.mixer.music.get_busy(): pygame.mixer.music.stop()
            app_state["loaded_preview_path_cache"] = ""
            pygame.draw.rect(screen, (0, 0, 0), (px, py, pw, ph))
        else:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
            app_state["guide_preview_music_track"] = ""
            if app_state.get("loaded_preview_path_cache", "") != "":
                if vlc_engine is not None: vlc_engine.stop()
                app_state["loaded_preview_path_cache"] = ""
            _render_transition_static(screen, pygame.Rect(px, py, pw, ph))

# ==============================================================================
# PART 24 OF 28: CHANNEL RENDERING (STATIC / VIDEO / VISUALIZER)
# ==============================================================================

    # --- RETRO UNIFIED BLACK/STATIC TRANSITION SHIELD GATE ---
    if pygame.time.get_ticks() < app_state.get("channel_switch_black_until", 0):
        _transition_style = db.config.get("transition_type", "black") if db is not None else "black"
        if _transition_style == "static":
            # STATIC TRANSITION: same look AND sound as genuine no-signal
            # static -- see _render_transition_static(), the single source
            # of truth for TV static, also used by the real Static playback
            # mode and the TV guide's inline preview shield below. Looped
            # for as long as this shield window stays active; whichever
            # branch runs the instant it ends (Channel 03's pass-through
            # included, just below) is responsible for calling
            # _stop_transition_static() so the loop can never bleed into
            # whatever comes next.
            _render_transition_static(screen, content_rect)
        else:
            _stop_transition_static()
            # Fill only content_rect so the between-channel grey flash is 4:3 in 4:3
            # Test Mode (the black bars were already painted earlier this frame) and
            # fits the TV border, instead of covering the whole 16:9 window. In
            # 16:9 / true-4:3 modes content_rect IS the whole window, so unchanged.
            screen.fill((15, 15, 15), content_rect)  # nostalgic gray transition fill, matches guide preview buffering fill

    # <<< FIXED FOR CHANNEL 03 >>>
    elif ch == "03":
        # Guaranteed stop: the instant the transition shield above expires
        # and we land here, any looped static from a "static"-style
        # transition MUST be cut off. Channel 03 (games/menu) never enters
        # real Static mode and never starts video playback, so nothing else
        # downstream would ever stop it otherwise -- this is exactly what
        # used to let static just keep playing indefinitely after switching
        # to Channel 03.
        _stop_transition_static()
        # SINGLE ACTIVATION CHECKPOINT: the instant the shield actually
        # expires and we land here is exactly when it's safe to start
        # anything this switch queued (game/emulator unfreeze+real volume,
        # menu music, DVD resume) -- see _run_pending_channel_activation()
        # and change_channel(). Replaces the old separate
        # _ch03_audio_unmuted_after_transition / _chaudio_audio_unmuted_after_transition
        # checks that used to live here.
        _run_pending_channel_activation()
        # Game Deck (carousel / rom list / embedded emulator) was already drawn
        # in the PART 18A block. We skip everything else here so it doesn't get painted over.
        pass

    elif ch != "04":
        # SINGLE ACTIVATION CHECKPOINT: this whole "elif" is only reached
        # once the shield-active branch above no longer matches, so it's
        # exactly the right moment to start whatever this switch queued
        # (VLC video/DVD or visualizer music) -- at its real volume,
        # directly, since nothing has played a frame of audio before this.
        # Replaces the old _chaudio_audio_unmuted_after_transition /
        # _chaudio_pending_kind / _chaudio_pending_unmute_state dance.
        _run_pending_channel_activation()

        current_state = calculate_slotted_playback_state(ch)
        
        if current_state["mode"] in ["Video", "Visualizer", "Visualizer_Empty"]:
            if vlc_engine is not None and current_state["mode"] != "Video":
                try:
                    if vlc_engine.player.is_playing(): vlc_engine.stop()
                except Exception as e: print(f"[PREVIEW] vlc stop failed: {e}")
            
        if current_state["mode"] == "Static":
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
                
            # FIXED: Forced clean black slate bypass for Channel 03
            if ch == "03":
                _stop_transition_static()
                screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
            else:
                _render_transition_static(screen, content_rect)

        # CONFIRMED CHANGE: Catch both "Video" and "Commercial" states to preventstartup freezes
        # BUG FIX: this was checking current_playback_state (computed earlier in the
        # frame, at the top of the draw pass) instead of current_state (computed just
        # above for this exact render). The two usually agree, but calculate_slotted_playback_state()
        # is wall-clock driven, so right at a schedule-slot boundary the two calls (made
        # milliseconds apart) can disagree on mode -- which would silently divert a
        # Visualizer/Static frame into this Video/Commercial branch instead of its own.
        elif current_state["mode"] in ("Video", "Commercial"):
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()

            _transition_style_wait = db.config.get("transition_type", "black") if db is not None else "black"

            if vlc_engine is not None:
                try:
                    vlc_bytes = vlc_engine.get_display_frame()
                    if vlc_bytes:
                        # First real frame since the switch. Whatever was covering the
                        # buffering gap (static or the gray fallback) ends right here --
                        # for the "static" style this is the ONLY place that stops the
                        # loop this branch started below, since it's the one moment we
                        # know for certain the wait is over.
                        _stop_transition_static()

                        # AUDIO-BLEED FIX (see _content_activate): the picture has
                        # now arrived, so it's finally safe to let the channel's
                        # audio through. Up to this frame the player was hard-muted
                        # so nothing bled under the buffering static. This is the
                        # authoritative "wait is over" moment, matching the static
                        # stop above, so the un-mute happens in lockstep with the
                        # first visible frame.
                        #
                        # FIXED: this used to call audio_set_mute(False)
                        # unconditionally, regardless of the app's own global
                        # mute setting. That's what caused "mute, then add
                        # media" to be briefly audible: adding a file to the
                        # live channel re-triggers change_channel's instant-play
                        # path, which hard-mutes the player for the buffering
                        # gap exactly like any other channel switch -- and this
                        # was the moment that hard-mute got lifted, even though
                        # the user was still muted. It then went quiet again
                        # the next time something (e.g. closing the menu)
                        # called vlc_engine.set_volume(..., is_muted), which
                        # correctly re-applies the real mute state. Now this
                        # only lifts the BUFFERING mute -- if the user is
                        # actually muted, the player stays muted straight
                        # through, no gap.
                        if app_state.get("_video_pending_unmute", False):
                            app_state["_video_pending_unmute"] = False
                            _still_muted = db.config.get("is_muted", False) if db is not None else False
                            try:
                                _pre_lift_mute = vlc_engine.player.audio_get_mute()
                            except Exception:
                                _pre_lift_mute = "?"
                            print(f"[MUTE TRACE] first-frame unmute checkpoint for ch{ch}: "
                                  f"is_muted={_still_muted}, audio_get_mute() before lift={_pre_lift_mute}")
                            if not _still_muted:
                                try: vlc_engine.player.audio_set_mute(False)
                                except Exception: pass
                            else:
                                # Belt-and-suspenders: explicitly keep it muted here too,
                                # in case the play()-time race (see post-play re-assert
                                # in change_channel) somehow still let it slip.
                                try: vlc_engine.player.audio_set_mute(True)
                                except Exception: pass

                        # Boot tune-in is finished the instant its first real frame
                        # lands -- clear the one-time boot-suppression flag so any
                        # later user channel change shows static normally.
                        app_state["_boot_suppress_static"] = False

                        # First real frame since the switch — start the OSD's
                        # 2.5s countdown NOW, so it's visible for its whole
                        # window instead of having burned down while the
                        # screen was still black/buffering.
                        if app_state.get("osd_channel_pending", False):
                            app_state["osd_channel_pending"] = False
                            app_state["osd_channel_timer"] = pygame.time.get_ticks() + 2500

                        video_surf = pygame.image.frombuffer(vlc_bytes, (vlc_engine.width, vlc_engine.height), "BGRA")
                        
                        brightness, contrast, color_sat, sharpness, tint, fx_active = _get_video_effects(db)
                        
                        if fx_active:
                            processed_surf = apply_video_effects(video_surf, brightness, contrast, color_sat, sharpness, tint)
                            screen.blit(pygame.transform.smoothscale(processed_surf, (content_rect.width, content_rect.height)), (content_rect.x, content_rect.y))
                        else:
                            processed_surf = video_surf
                            screen.blit(pygame.transform.smoothscale(video_surf, (content_rect.width, content_rect.height)), (content_rect.x, content_rect.y))
                        # Cache the post-effects surface, not the raw decode — otherwise the
                        # bridge frame used during buffering gaps briefly flashes unprocessed
                        # color before effects snap back on the next real frame.
                        _store_last_video_frame(processed_surf)
                    else:
                        # get_display_frame() returned None -- but that means two
                        # VERY different things, and telling them apart is what
                        # fixes the on-a-playing-channel static flicker:
                        #
                        #  A) FIRST-FRAME BUFFERING. _last_video_frame is None
                        #     (it was cleared by change_channel -> _clear_last_video_frame()
                        #     on the switch and nothing has decoded since). We are
                        #     genuinely still waiting for VLC's first frame (~1-1.5s),
                        #     which is longer than the fixed shield above, so a
                        #     static/gray bridge legitimately belongs here.
                        #
                        #  B) STEADY-STATE REPEAT POLL. _last_video_frame is set,
                        #     so a real frame has already been shown. The 60Hz game
                        #     loop simply polled faster than the 24-30fps video
                        #     produced a NEW frame, so there's just no new frame
                        #     THIS tick -- which happens on ~half of all frames, by
                        #     design (see get_display_frame). Here we must re-show
                        #     the last good frame and NEVER draw static. Drawing
                        #     static on these normal empty polls is exactly what made
                        #     a perfectly healthy "static"-transition channel flicker
                        #     video/static/video ~30x a second the whole time it played.
                        #
                        # So: only loop the static bridge while we truly have no
                        # decoded frame yet (case A). Once one exists, always bridge
                        # with the cached frame regardless of transition style (case B).
                        if _last_video_frame is not None:
                            if 'static_audio' in globals() and static_audio is not None:
                                static_audio.stop()
                            _blit_last_video_frame(screen, content_rect.x, content_rect.y, content_rect.width, content_rect.height)
                        elif _transition_style_wait == "static" and not app_state.get("_boot_suppress_static", False):
                            # Case A first-frame buffering with a "static" transition --
                            # loop static UNLESS this is the boot tune-in. Boot must come
                            # up clean (no channel-change static), so while _boot_suppress_static
                            # is set we fall through to the plain gray fill below instead.
                            _render_transition_static(screen, content_rect)
                        else:
                            if 'static_audio' in globals() and static_audio is not None:
                                static_audio.stop()
                            screen.fill((15, 15, 15), content_rect)  # nostalgic gray transition fill, matches guide preview buffering fill
                except Exception:
                    # Per-frame render path — don't log (would spam every frame).
                    # Match the main path: never draw the channel-change static during
                    # the boot tune-in (keep the clean gray/last-frame bridge instead).
                    if _transition_style_wait == "static" and not app_state.get("_boot_suppress_static", False):
                        _render_transition_static(screen, content_rect)
                    elif not _blit_last_video_frame(screen, content_rect.x, content_rect.y, content_rect.width, content_rect.height):
                        screen.fill((15, 15, 15), content_rect)  # nostalgic gray transition fill, matches guide preview buffering fill

# ==============================================================================
# PART 25 OF 28: CHANNEL RENDERING CONTINUED - VISUALIZER PLAYHEAD SETUP
# ==============================================================================

        elif current_state["mode"] == "Visualizer":
            try:
                if 'static_audio' in globals() and static_audio is not None: 
                    static_audio.stop()
                    
                target_file = current_state.get("file", "")
                target_seek_seconds = current_state.get("seek", 0)
                track_duration_max = current_state.get("duration", 210)
                ch_conf = db.channels_db.get(str(ch).zfill(2), {})
                
                active_cached_track = app_state.get("active_music_track_file", "")
                
                _ch_key_vis = str(ch).zfill(2)

                # --- WATCHDOG: pygame.mixer.music.get_busy() is the actual source
                # of truth for whether a song has finished -- NOT the wall-clock
                # modulo math above, which is only as accurate as each track's
                # stored (often still-unprobed/placeholder) duration. Without this,
                # an unprobed/underestimated duration made the schedule jump to a
                # "next" file the instant its assumed slot ran out, cutting the
                # real, still-playing audio off mid-song; an overestimated one left
                # the schedule expecting the same file long after it had actually
                # ended, restarting it from 0 instead of moving on.
                _vis_mixer_busy = pygame.mixer.music.get_busy()
                # Grace-period check: pygame reports get_busy()=False for 1-2
                # frames right after play() is called while the audio buffer
                # fills. Skip the "song ended" watchdog branch during this
                # window so a freshly-started song is never immediately cut.
                _vis_grace_ms = 600
                _vis_in_grace  = (pygame.time.get_ticks() -
                                  app_state.get("vis_song_started_at", 0)) < _vis_grace_ms
                if active_cached_track and _vis_mixer_busy and target_file != active_cached_track:
                    # Assumed slot ended but the real audio hasn't -- hold the
                    # current song, ignore the schedule's early pick this frame.
                    # It'll pick up the next real track on its own once playback
                    # genuinely ends (branch below), or naturally agree again once
                    # the wall clock catches up to where actual playback is.
                    target_file = active_cached_track
                elif active_cached_track and not _vis_mixer_busy and not _vis_in_grace:
                    # The song genuinely ended (whether early or on time) -- move
                    # to the real NEXT track in this channel's own shuffled
                    # rotation (the same ordering the TV guide/pre-cache peek use)
                    # rather than trusting the schedule's current pick, which may
                    # still show the just-finished file (assumed duration was too
                    # generous -- would otherwise restart it from 0) or may have
                    # already drifted several tracks ahead (assumed duration was
                    # too short -- would otherwise skip tracks that never played).
                    try:
                        _vis_ordered, _vis_block_key = _get_channel_ordered_content(_ch_key_vis, ch_conf)
                    except Exception:
                        _vis_ordered, _vis_block_key = [], None
                    _forced_next = None
                    if _vis_ordered:
                        _vis_upcoming = _next_n_from_ordered(_vis_ordered, active_cached_track, n=2)
                        if len(_vis_upcoming) >= 2 and active_cached_track in _paths_from_entry(_vis_upcoming[0]):
                            _forced_next = _vis_upcoming[1]
                        elif _vis_upcoming:
                            _forced_next = _vis_upcoming[0]
                    if _forced_next is not None:
                        _forced_path = _forced_next.get("path", _forced_next.get("file", "")) if isinstance(_forced_next, dict) else str(_forced_next)
                        if _forced_path and os.path.exists(_forced_path):
                            target_file = _forced_path
                            target_seek_seconds = 0
                            track_duration_max = _safe_dur(_forced_next, default=210) if isinstance(_forced_next, dict) else 210
                            # Sync the wall-clock schedule to agree with this pick going
                            # forward -- record how far into the shuffled playlist the
                            # chosen track actually starts, so the NEXT call to
                            # calculate_slotted_playback_state (which recomputes purely
                            # from wall-clock + assumed durations) lands here too instead
                            # of drifting back to a stale position.
                            try:
                                _vis_cum = 0
                                for _vt in _vis_ordered:
                                    _vp = _vt.get("path", _vt.get("file", "")) if isinstance(_vt, dict) else str(_vt)
                                    if os.path.normpath(_vp) == os.path.normpath(_forced_path):
                                        break
                                    _vis_cum += _safe_dur(_vt, default=210) if isinstance(_vt, dict) else 210
                                app_state["visualizer_skip_ch"]  = _ch_key_vis
                                app_state["visualizer_skip_pos"] = _vis_cum
                            except Exception as _vis_skip_err:
                                log.warning("Visualizer watchdog: failed to register skip position for ch%s: %s", _ch_key_vis, _vis_skip_err)
                # BUG FIX: this used to be a single flat app_state string shared
                # by every visualizer channel, so tuning A -> B -> A registered
                # as "new track" every single time (different channel = almost
                # always a different file), forcing a style reroll on every
                # channel flip rather than only when a channel's OWN track
                # actually changes. Keyed per-channel, revisiting a channel
                # whose track hasn't changed no longer looks "new".
                _last_sched_by_ch = app_state.setdefault("last_scheduled_vis_track", {})
                last_scheduled_vis_track = _last_sched_by_ch.get(_ch_key_vis, "")
                is_new_scheduled_track = last_scheduled_vis_track != target_file
                
                if active_cached_track != target_file:
                    if target_file and os.path.exists(target_file):
                        print(f"[DEBUG MASTER ENGINE] Initializing Track: {os.path.basename(target_file)} | Seek Targeted: {target_seek_seconds}s / Max: {track_duration_max}s")
                        pygame.mixer.Channel(1).stop()
                        
                        if pygame.mixer.music.get_busy():
                            pygame.mixer.music.stop()
                        
                        try: 
                            pygame.mixer.music.fadeout(50)
                            pygame.time.wait(60)
                            pygame.mixer.music.unload()
                        except Exception as unload_err:
                            log.warning("Track transition: pygame.mixer.music fadeout/unload failed for %s: %s", target_file, unload_err)
                        
                        pygame.mixer.music.load(target_file)
                        # LOUDNESS NORMALIZATION: same gain lookup as the guide preview,
                        # keyed to this live channel + file, so a live music channel and
                        # a live TV-show channel land at matched perceived loudness.
                        app_state["active_music_gain"] = _lookup_file_gain(_ch_key_vis, target_file)
                        pygame.mixer.music.set_volume(_effective_audio_gain(min_db=STACKED_GAIN_MIN_DB) * MUSIC_GAIN * app_state["active_music_gain"])

                        safe_seek = max(0.0, min(float(target_seek_seconds), float(track_duration_max - 10)))
                        if safe_seek >= (track_duration_max - 10):
                            safe_seek = 0.0

                        try:
                            pygame.mixer.music.play(start=safe_seek)
                        except Exception as play_err:
                            print(f"[TELEMETRY CRITICAL EXCEPTION] play(start={safe_seek}) rejected: {play_err}")
                            try:
                                pygame.mixer.music.play(start=0.0)
                            except Exception as retry_err:
                                print(f"[VISUALIZER PART25] Fallback play(start=0.0) also failed: {retry_err}")
                            
                        app_state["active_music_track_file"] = target_file
                        app_state["vis_card_timer"] = pygame.time.get_ticks() + 15000
                        # Grace-period stamp: pygame.mixer.music.get_busy() returns
                        # False for 1-2 frames after play() is called while the audio
                        # buffer fills. The watchdog below must not treat that window
                        # as a genuine song-end or it will immediately advance to the
                        # next track, cutting the song before it even starts. Stamp
                        # the start time here; the watchdog skips the "ended" branch
                        # for 600 ms after each new song begins.
                        app_state["vis_song_started_at"] = pygame.time.get_ticks()
                
                # BUG FIX: this used to live INSIDE the `if active_cached_track !=
                # target_file:` block above, so it only ran when the shared
                # "active_music_track_file" cache also happened to be stale. But
                # the TV Guide preview (PART 23) writes that exact same app_state
                # key while you're just browsing/highlighting a channel, so by the
                # time you actually tune to that channel live, active_cached_track
                # already equals target_file (the guide primed it) and this whole
                # block got skipped -- meaning current_running_style never got set
                # for the live channel and every visualizer channel kept whatever
                # style happened to be set last (usually the deck's initial
                # default), which is exactly the "stuck on the same visualizer on
                # every channel" symptom. Style selection is about whether THIS
                # channel's own scheduled track changed, not about whether the
                # audio buffer needs a reload, so it now runs unconditionally.
                if is_new_scheduled_track:
                    _last_sched_by_ch[_ch_key_vis] = target_file
                    if ch_conf.get("visualizer_style", "Random") == "Random":
                        _new_style = visualizer_deck.get_next_random_style()
                        visualizer_deck.current_running_style = _new_style
                        visualizer_deck.style_by_channel[_ch_key_vis] = _new_style
                elif (ch_conf.get("visualizer_style", "Random") == "Random"
                      and _ch_key_vis in visualizer_deck.style_by_channel):
                    # Track hasn't changed, but this channel may already have
                    # its own rolled style on record (e.g. from being previewed
                    # in the guide before you tuned to it) -- use that instead
                    # of whatever current_running_style was left over from
                    # whatever channel was live/previewed last.
                    visualizer_deck.current_running_style = visualizer_deck.style_by_channel[_ch_key_vis]
                
                if pygame.mixer.music.get_busy():
                    calc_amp = 35 + int(math.sin(pygame.time.get_ticks() * 0.12) * 30) + random.randint(0, 10)
                else:
                    if active_cached_track == target_file:
                        print(f"[TELEMETRY EXCEPTION INTERCEPT] Core rejected seek position: {target_seek_seconds}s. Safe fallback to 0.0s.")
                        pygame.mixer.music.play(start=0.0)
                    calc_amp = 0
                
                vis_style_setting = ch_conf.get("visualizer_style", "Random")
                if vis_style_setting != "Random":
                    visualizer_deck.current_running_style = vis_style_setting

# ==============================================================================
# PART 26 OF 28: CHANNEL RENDERING CONTINUED - HARDWARE OVERLAYS & HUD BLITS
# ==============================================================================

                import colorsys as _hud_colorsys
                _hr, _hg, _hb = _hud_colorsys.hsv_to_rgb(db.config.get("theme_ui_hue", 140) / 360.0, 0.9, 1.0)
                theme_green_color = (int(_hr * 255), int(_hg * 255), int(_hb * 255))
                
                # Render visualizer at optimized 960x540 resolution canvas to keep filters lag-free
                vis_w, vis_h = 960, 540
                vis_surf = pygame.Surface((vis_w, vis_h))
                visualizer_deck.render_active_engine(vis_surf, active_theme, mock_amplitude=calc_amp)
                
                # FIXED: Restored all 5 original filter properties for the visualizer channel
                brightness, contrast, color_sat, sharpness, tint, fx_active = _get_video_effects(db)
                
                # FIXED: Re-linked your original apply_video_effects function for the visualizer screen
                if fx_active:
                    processed_surf = apply_video_effects(vis_surf, brightness, contrast, color_sat, sharpness, tint)
                    screen.blit(pygame.transform.scale(processed_surf, (content_rect.width, content_rect.height)), (content_rect.x, content_rect.y))
                else:
                    screen.blit(pygame.transform.scale(vis_surf, (content_rect.width, content_rect.height)), (content_rect.x, content_rect.y))
                
                # --- PURE GREEN HUD DISPLAY CARD ---
                now_f = pygame.time.get_ticks()
                if now_f < app_state.get("vis_card_timer", 0):
                    f_full_card = _cached_sys_font("Courier New", 17, bold=True)
                    
                    display_path_string = active_cached_track if active_cached_track else target_file
                    raw_filename = os.path.basename(display_path_string).upper()
                    
                    if "." in raw_filename:
                        base_filename_str = raw_filename[:raw_filename.rfind(".")]
                    else:
                        base_filename_str = raw_filename
                        
                    import re
                    song_name_cleaned = re.sub(r'^[0-9\s\.\-_]+', '', base_filename_str)
                    style_name_cleaned = visualizer_deck.current_running_style.upper()
                    
                    screen.blit(f_full_card.render(song_name_cleaned, True, theme_green_color), (content_rect.x + 20, content_rect.y + 20))
                    screen.blit(f_full_card.render(f"VISUALIZER: {style_name_cleaned}", True, theme_green_color), (content_rect.x + 20, content_rect.y + 45))
            except Exception as e:
                print(f"[DEBUG MASTER RUNTIME ERROR] HUD drawing failed: {e}")
                screen.fill((5, 5, 10))
                
        elif current_state["mode"] == "Visualizer_Empty":
            if 'static_audio' in globals() and static_audio is not None: static_audio.stop()
            if pygame.mixer.music.get_busy(): pygame.mixer.music.stop()
            pygame.mixer.Channel(1).stop()
            app_state["active_music_track_file"] = ""
            screen.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key

    # --- INLINE LIVE TV GUIDE INTERRUPT PREVIEW DRIVER CHECK ---
    else:
        if pygame.time.get_ticks() < app_state.get("channel_switch_black_until", 0):
            # Same transition shield as a real channel switch (see the
            # "RETRO UNIFIED BLACK/STATIC TRANSITION SHIELD GATE" above) --
            # previously this branch only silenced audio and left whatever
            # was already on screen showing through, with no flash at all
            # while browsing the TV guide. Now the guide gets the exact
            # same black/static transition a real channel switch does.
            _transition_style = db.config.get("transition_type", "black") if db is not None else "black"
            if _transition_style == "static":
                _render_transition_static(screen, content_rect)
            else:
                _stop_transition_static()
                screen.fill((15, 15, 15), content_rect)
        elif (db is not None and db.config.get("transition_type", "black") == "static"
              and pygame.time.get_ticks() < app_state.get("guide_preview_static_until", 0)):
            # GUIDE PREVIEW SCROLL STATIC BURST (the "no audio on the preview
            # static" bug): this driver runs a SECOND time each frame, after the
            # Part 22/23 preview handler above has already drawn the small
            # preview-box static AND started its looped static audio via
            # _render_transition_static(). The plain `else` below unconditionally
            # calls _stop_transition_static() -- so on every frame of a guide
            # SCROLL transition it was silencing that audio the instant the
            # preview handler started it, leaving visible static with no sound.
            # This branch: while the DEDICATED guide-preview static timer is
            # active (armed by _arm_guide_preview_transition() on each highlight
            # move -- separate from channel_switch_black_until, which only covers
            # the full-screen guide-OPEN flash handled just above), do NOT stop
            # the static and do NOT start the previewed channel's music yet.
            # Both must wait for the static burst to finish, exactly like a real
            # channel change. The preview handler owns the visual+audio; here we
            # simply leave its loop running.
            pass
        else:
            preview_ch_num = str(app_state.get("highlighted_guide_channel", "05")).zfill(2)
            preview_ch_info = db.channels_db.get(preview_ch_num, {}) if db is not None else {}
            p_state = calculate_slotted_playback_state(preview_ch_num)

            if p_state.get("mode") == "Static":
                # GUIDE STATIC AUDIO (unscheduled / no-signal channel): the
                # Part 22/23 preview handler above intentionally loops static
                # audio for this channel via _render_transition_static(). This
                # driver runs again later in the same frame, and the old
                # unconditional _stop_transition_static() here silenced that
                # loop every single frame -- so the preview box showed visible
                # static with no sound. The scroll-burst branch above already
                # protects the SCROLL window, but a channel simply sitting on
                # static (or any run with the "black" transition style, where
                # that branch never matches) still landed here and got muted.
                # When the highlighted channel is genuinely static, leave the
                # preview handler's loop running so it stays audible, matching
                # the real Static channel.
                pass
            elif preview_ch_info.get("is_visualizer", False):
                _stop_transition_static()
                if p_state["mode"] == "Visualizer" and p_state.get("file"):
                    guide_cached_track = app_state.get("active_music_track_file", "")
                    if guide_cached_track != p_state["file"]:
                        if os.path.exists(p_state["file"]):
                            pygame.mixer.Channel(1).stop()
                            pygame.mixer.music.stop()
                            try:
                                pygame.mixer.music.load(p_state["file"])
                                app_state["active_music_gain"] = _lookup_file_gain(preview_ch_num, p_state["file"])
                                pygame.mixer.music.set_volume(_effective_audio_gain(min_db=STACKED_GAIN_MIN_DB) * MUSIC_GAIN * app_state["active_music_gain"])
                                pygame.mixer.music.play(start=float(p_state.get("seek", 0)))
                                app_state["active_music_track_file"] = p_state["file"]
                            except Exception as e: print(f"[MUSIC] load/play failed for {p_state.get('file')}: {e}")
            else:
                # Highlighted channel is a real Video/Commercial channel (not
                # static, not a visualizer) -- its own preview handler owns the
                # audio, so any leftover static loop must be cut here as before.
                _stop_transition_static()

# ==============================================================================
# PART 27 OF 28: CONTROL HOOK DYNAMIC REPEAT SWITCHERS
# ==============================================================================

    # --- RESTORED DYNAMIC VOLUME & SLIDER REPEAT ENGINE ---
    pressed_keys_matrix = pygame.key.get_pressed()

    def _key_is_held(_k):
        """True if `_k` is currently held down.

        pygame.key.get_pressed() only reflects keys SDL itself saw come in
        through the real hardware input path. Any key delivered via the
        WH_KEYBOARD_LL hook near the top of this file (volume, mute, media
        transport, browser keys, F-keys, and anything the user has remapped
        onto one of those physical buttons) arrives as a pygame event the
        hook posted directly -- pygame.event.post() puts it on the event
        queue but never touches SDL's internal keyboard-state array, so
        get_pressed() reports those keys as "not pressed" even while
        they're being held down. app_state["_held_keys"] is updated from
        the actual KEYDOWN/KEYUP event stream (post remap-translation)
        instead, so it catches those. Checking both here means ordinary
        keys keep working exactly as before and hook-delivered / remapped
        keys now do too.
        """
        return bool(pressed_keys_matrix[_k]) or (_k in app_state["_held_keys"])

    now_ticks_ms = pygame.time.get_ticks()
    
    # --------------------------------------------------------------------------
    # ⏱️ ANTI-BURN-IN PROTECTION MONITOR TIMERS
    # --------------------------------------------------------------------------
    last_input = app_state.get("last_input_time", now_ticks_ms)
    elapsed_ms = now_ticks_ms - last_input

    if app_state.get("show_splash", False) and elapsed_ms >= 60000:
        app_state["show_splash"] = False
        app_state["splash_shown_at"] = 0

    if app_state.get("show_quit_confirm", False) and elapsed_ms >= 20000:
        app_state["show_quit_confirm"] = False
        app_state["quit_confirm_shown_at"] = 0

    if app_state.get("show_menu", False) and elapsed_ms >= 60000:
        # Do not auto-close if the file explorer is open — it came FROM the menu
        # and browsing it is an active sub-menu interaction.
        if not app_state.get("file_explorer_active", False):
            app_state["loaded_preview_path_cache"] = ""
            app_state["show_menu"] = False
            app_state["menu_shown_at"] = 0
        else:
            # File explorer is open — treat this as continuous user activity.
            # Reset last_input_time (the field the burn-in check actually reads)
            # so elapsed_ms resets to 0 and the condition won't re-fire next frame.
            app_state["last_input_time"] = now_ticks_ms

    if app_state["current_channel"] == "04" and elapsed_ms >= 60000:
        # File explorer open = user is actively browsing (a sub-menu of the
        # main menu which itself is a sub-menu of the guide). Don't jump away.
        if (not app_state.get("show_menu", False)
                and not app_state.get("show_splash", False)
                and not app_state.get("file_explorer_active", False)):
            _burn_target = str(app_state.get("highlighted_guide_channel", "05")).zfill(2)
            print(f"[ANTI-BURN-IN SHIELD 2] Inactivity elapsed. Switching to previewed channel: {_burn_target}")
            app_state["loaded_preview_path_cache"] = ""
            change_channel(_burn_target)

    # --------------------------------------------------------------------------
    # CORE REPEAT DYNAMICS CONTROLLER ENGINE
    # --------------------------------------------------------------------------
    if 'visualizer_deck' in globals() and visualizer_deck is not None:
        if hasattr(visualizer_deck, "flash_intensity"): visualizer_deck.flash_intensity = 0.0
        if hasattr(visualizer_deck, "bg_flash"): visualizer_deck.bg_flash = 0

    # ── TV GUIDE: WASD hold-to-repeat ────��────────────────────────────────────
    # After a 300 ms initial hold, fire the navigation action every 100 ms so
    # the user can scroll through rows/columns by just holding the key down.
    if (app_state.get("current_channel") == "04"
            and not app_state.get("show_menu", False)
            and not app_state.get("show_quit_confirm", False)
            and not app_state.get("show_splash", False)
            and not app_state.get("file_explorer_active", False)):
        _gh_any = False
        for _gh_key in (pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d):
            if _key_is_held(_gh_key):
                _gh_any = True
                if app_state.get("_gh_held_key") != _gh_key:
                    # New key — record hold start; first fire already happened via KEYDOWN
                    app_state["_gh_held_key"]    = _gh_key
                    app_state["_gh_hold_start"]  = now_ticks_ms
                    app_state["_gh_last_repeat"]  = now_ticks_ms
                else:
                    _gh_held_ms = now_ticks_ms - app_state.get("_gh_hold_start",  now_ticks_ms)
                    _gh_since   = now_ticks_ms - app_state.get("_gh_last_repeat", now_ticks_ms)
                    if _gh_held_ms > 300 and _gh_since >= 100:
                        app_state["_gh_last_repeat"] = now_ticks_ms
                        app_state["guide_inactivity_timer"] = now_ticks_ms + 60000
                        # Rebuild allowed channel list (same logic as KEYDOWN handler)
                        _gh_allowed = []
                        if db:
                            for _gi in range(5, 45):
                                if db.channels_db.get(str(_gi).zfill(2), {}).get("active", True):
                                    _gh_allowed.append(str(_gi).zfill(2))
                        _gh_str = str(app_state.get("selected_guide_channel", 5)).zfill(2)
                        try:    _gh_pos = _gh_allowed.index(_gh_str)
                        except ValueError: _gh_pos = 0
                        _gh_roff = app_state.get("guide_row_offset", 0)
                        # 8 rows on 16:9, 5 rows on 4:3 — see get_guide_visible_rows
                        _gh_rows = get_guide_visible_rows(db)
                        if _gh_key == pygame.K_w and _gh_allowed:
                            _npos = (_gh_pos - 1) % len(_gh_allowed)
                            app_state["selected_guide_channel"]    = int(_gh_allowed[_npos])
                            app_state["highlighted_guide_channel"] = _gh_allowed[_npos]
                            _clear_last_video_frame()  # new highlight = new preview source
                            _arm_guide_preview_transition()  # static-mode: burst of static in the preview box; black-mode: no-op
                            if _npos < _gh_roff:
                                app_state["guide_row_offset"] = _npos
                            elif _npos >= _gh_roff + _gh_rows:
                                app_state["guide_row_offset"] = max(0, _npos - (_gh_rows - 1))
                        elif _gh_key == pygame.K_s and _gh_allowed:
                            _npos = (_gh_pos + 1) % len(_gh_allowed)
                            app_state["selected_guide_channel"]    = int(_gh_allowed[_npos])
                            app_state["highlighted_guide_channel"] = _gh_allowed[_npos]
                            _clear_last_video_frame()  # new highlight = new preview source
                            _arm_guide_preview_transition()  # static-mode: burst of static in the preview box; black-mode: no-op
                            if _npos >= _gh_roff + _gh_rows:
                                app_state["guide_row_offset"] = _npos - (_gh_rows - 1)
                            elif _npos < _gh_roff:
                                app_state["guide_row_offset"] = _npos
                        elif _gh_key == pygame.K_a:
                            # Held-repeat A: same show-to-show jump as the
                            # KEYDOWN handler (see _guide_jump_col) instead of
                            # stepping one half-hour column at a time.
                            _guide_jump_col(-1)
                        elif _gh_key == pygame.K_d:
                            # Held-repeat D: same show-to-show jump as the
                            # KEYDOWN handler (see _guide_jump_col) instead of
                            # stepping one half-hour column at a time.
                            _guide_jump_col(1)
                break  # only process the first held key
        if not _gh_any:
            app_state["_gh_held_key"] = None   # all guide keys released — reset tracker

    if app_state.get("show_menu", False):
        menu_tab_layer = app_state.get("menu_layer", "")
        focused_selection = app_state.get("menu_selection_index", 0)
        active_hold_direction = 0
        
        if _key_is_held(pygame.K_d):     active_hold_direction = 1
        elif _key_is_held(pygame.K_a):    active_hold_direction = -1
            
        if active_hold_direction != 0:
            if app_state.get("last_hold_direction", 0) != active_hold_direction:
                app_state["hold_start_timestamp"] = now_ticks_ms
                app_state["last_hold_direction"] = active_hold_direction
                
            time_held_down = now_ticks_ms - app_state.get("hold_start_timestamp", now_ticks_ms)
            
            if time_held_down > 200 and now_ticks_ms >= app_state.get("slider_scroll_cooldown", 0):
                valid_slider_row = False
                
                if menu_tab_layer == "VIDEO_ROWS" and (focused_selection >= 0 and focused_selection <= 4):
                    video_keys_dictionary = {0: "brightness", 1: "contrast", 2: "color", 3: "sharpness", 4: "tint"}
                    target_config_key = video_keys_dictionary[focused_selection]
                    current_val = db.config.get(target_config_key, 50)
                    db.config[target_config_key] = max(0, min(100, ((current_val + (active_hold_direction * 5)) // 5) * 5))
                    valid_slider_row = True
                    app_state["slider_scroll_cooldown"] = now_ticks_ms + 180
                    
                elif menu_tab_layer == "THEME_ROWS" and (focused_selection >= 0 and focused_selection <= 2):
                    if focused_selection == 0:
                        current_val = db.config.get("menu_opacity", 50)
                        db.config["menu_opacity"] = max(0, min(100, ((current_val + (active_hold_direction * 5)) // 5) * 5))
                    else:
                        target_config_key = "theme_bg_hue" if focused_selection == 1 else "theme_ui_hue"
                        current_val = db.config.get(target_config_key, 220)
                        db.config[target_config_key] = max(0, min(360, ((current_val + (active_hold_direction * 5)) // 5) * 5))
                    valid_slider_row = True
                    app_state["slider_scroll_cooldown"] = now_ticks_ms + 180
                    
                if valid_slider_row:
                    _mark_settings_dirty()
        else:
            app_state["last_hold_direction"] = 0
            app_state["hold_start_timestamp"] = 0
            
    elif not app_state.get("show_menu", False) and not app_state.get("show_splash", False) and not app_state.get("show_quit_confirm", False):
        # DVD hold-skip is handled entirely inside game_deck._poll_dvd_hold() (called each render frame)
        pass

        # Suppress the held-LEFT/RIGHT volume ramp while a DVD confirm popup
        # is open. The KEYDOWN gate above already swallows the initial
        # LEFT/RIGHT press, but this hold-repeat path runs every frame off
        # _key_is_held() and would otherwise keep ramping volume for as long
        # as the key stays down -- so it needs the same guard to honor "no
        # other button works until a choice is made."
        _dvd_popup_open = (
            app_state.get("current_channel") == "03"
            and 'game_deck' in globals() and game_deck is not None
            and getattr(game_deck, "mode", "BROWSE") == "DVD_PLAYER"
            and (getattr(game_deck, "dvd_exit_confirm", False)
                 or getattr(game_deck, "dvd_return_menu_confirm", False))
        )

        active_vol_direction = 0
        if not _dvd_popup_open:
            if _key_is_held(pygame.K_RIGHT):    active_vol_direction = 1
            elif _key_is_held(pygame.K_LEFT):   active_vol_direction = -1
            
        if active_vol_direction != 0:
            if app_state.get("last_vol_hold_direction", 0) != active_vol_direction:
                app_state["vol_hold_start_timestamp"] = now_ticks_ms
                app_state["last_vol_hold_direction"] = active_vol_direction
                
            vol_time_held = now_ticks_ms - app_state.get("vol_hold_start_timestamp", now_ticks_ms)
            
            if vol_time_held > 200 and now_ticks_ms >= app_state.get("slider_scroll_cooldown", 0):
                db.config["global_volume"] = max(0, min(100, db.config.get("global_volume", 70) + (active_vol_direction * 5)))
                app_state["osd_volume_timer"] = now_ticks_ms + 2500
                if game_deck is not None:
                    game_deck.dvd_osd_msg = ""; game_deck.dvd_osd_until = 0
                db.config["is_muted"] = False
                
                target_gain_factor = _perceptual_volume_pct(db.config["global_volume"]) / 100.0
                music_gain_factor = _perceptual_volume_pct(db.config["global_volume"], min_db=STACKED_GAIN_MIN_DB) / 100.0
                if vlc_engine is not None: vlc_engine.set_volume(int(db.config["global_volume"]), False)
                _set_dvd_volume(int(db.config["global_volume"]), False)
                if static_audio is not None: static_audio.set_volume(target_gain_factor)
                pygame.mixer.music.set_volume(music_gain_factor * MUSIC_GAIN * app_state.get("active_music_gain", 1.0))
                pygame.mixer.Channel(1).set_volume(music_gain_factor * MUSIC_GAIN * app_state.get("active_music_gain", 1.0))
                if game_deck is not None and hasattr(game_deck, "set_libretro_volume"):
                    game_deck.set_libretro_volume(
                        db.config.get("global_volume", 70), db.config.get("is_muted", False))
                if game_deck is not None and hasattr(game_deck, "set_audio_volume"):
                    game_deck.set_audio_volume(
                        db.config.get("global_volume", 70), db.config.get("is_muted", False))
                _set_ch03_menu_music_volume(db.config["global_volume"], db.config.get("is_muted", False))
                
                _mark_settings_dirty()
                app_state["slider_scroll_cooldown"] = now_ticks_ms + 180
        else:
            app_state["last_vol_hold_direction"] = 0
            app_state["vol_hold_start_timestamp"] = 0

    # Write out any settings change that's been sitting debounced (see
    # _mark_settings_dirty) once the user pauses, or after a hard cap if
    # they're still actively holding a slider. One cheap dict lookup per
    # frame when nothing is pending.
    _flush_settings_if_dirty()

    if app_state.get("show_menu", False):
        # Render the menu into the 4:3 content region in 4:3 Test Mode (see
        # _overlay_target) so it fits the 4:3 area / TV border instead of
        # spilling past it; in 16:9 / true-4:3 this IS the full screen.
        _menu_surf = _overlay_target(screen)
        render_settings_overlay_menu(_menu_surf, active_theme, app_state, db)

        #   ─ GAMES_CTRL_MAP: GBA controller mapping wireframe UI ───────────────
        if app_state.get("menu_layer") == "GAMES_CTRL_MAP" and game_deck is not None:
            # Sync cursor from app_state into game_deck so renderer reads it
            game_deck._ctrl_map_cursor = app_state.get("ctrl_map_cursor", 0)
            import colorsys as _cmcs
            _ch = db.config.get("theme_ui_hue", 140) / 360.0 if db else 0.39
            _cr, _cg, _cb = _cmcs.hsv_to_rgb(_ch, 0.88, 1.0)
            _ui_col = (int(_cr * 255), int(_cg * 255), int(_cb * 255))
            # Compute the same 1036×680 centered rect that render_settings_overlay_menu uses
            # -- against the SAME surface the menu drew into so the sub-panel
            # stays aligned with the menu inside the 4:3 region.
            _real_w, _real_h = _menu_surf.get_size()
            _mw, _mh = 1036, 680
            if _real_w >= _mw and _real_h >= _mh:
                _mx = (_real_w - _mw) // 2
                _my = (_real_h - _mh) // 2
            else:
                _shrink_w = min(_mw, _real_w - 20)
                _shrink_h = int(_shrink_w * (680.0 / 1036.0))
                if _shrink_h > _real_h - 20:
                    _shrink_h = _real_h - 20
                    _shrink_w = int(_shrink_h * (1036.0 / 680.0))
                _mx = (_real_w - _shrink_w) // 2
                _my = (_real_h - _shrink_h) // 2
                _mw, _mh = _shrink_w, _shrink_h
            # Confine the panel to the same content area (below the tab bar)
            # that every other sub-menu draws into — matches the cx/content_y/
            # content_w/content_h geometry in render_settings_overlay_menu.
            # Using the full 1036x680 rect here made this panel paint a second,
            # separately-blended layer directly over the already-opaque MENU
            # title and tab bar, which only partially covered them and let
            # them show through underneath. Scaling those content-area offsets
            # by the same factor used for _mw/_mh keeps it aligned when the
            # window is small enough to trigger the shrink path.
            _content_scale = _mw / 1036.0
            _ctrl_x = _mx + int(20 * _content_scale)
            _ctrl_y = _my + int(138 * _content_scale)
            _ctrl_w = int(996 * _content_scale)
            _ctrl_h = int(525 * _content_scale)
            game_deck.render_controller_mapping_ui(_menu_surf, (_ctrl_x, _ctrl_y, _ctrl_w, _ctrl_h), _ui_col, db, console=app_state.get("games_ctrl_console", "GBA"))
            # Per-frame XInput poll (captures controller button for remapping)
            game_deck.poll_xinput_for_remap()
            # Close button clicked → step back to GAMES_SUB_MENU
            if getattr(game_deck, "_mapping_close_requested", False):
                game_deck._mapping_close_requested = False
                app_state["menu_layer"] = "GAMES_SUB_MENU"
                app_state["sub_menu_row_index"] = 0

        #   ─ DVD_BTN_MAP: DVD transport-key remap popup ────��───────────────────
        elif app_state.get("menu_layer") == "DVD_BTN_MAP":
            render_dvd_button_mapping_popup(_menu_surf, app_state, db)
    elif app_state.get("show_splash", False):
        render_controls_splash(_overlay_target(screen), active_theme, db, app_state=app_state)

# ==============================================================================
# PART 28 OF 28: OSD TEXT DISPLAY WRAPPERS & FIXED POPUP ENGINE
# ==============================================================================

    elif app_state.get("show_quit_confirm", False):
        render_quit_prompt(_overlay_target(screen), active_theme, db)
        
    elif app_state.get("show_restart_confirm", False):
        # ⏱️ 20-Second Burn-in Protection Timer Check
        elapsed_popup_ms = now_ticks_ms - app_state.get("restart_confirm_shown_at", now_ticks_ms)
        if elapsed_popup_ms >= 20000:
            print("[ANTI-BURN-IN SHIELD] Restart popup timed out. Reverting changes.")
            app_state["show_restart_confirm"] = False
        else:
            # Draw the restart popup into the 4:3 content region in 4:3 Test
            # Mode (see _overlay_target) so it stays inside the 4:3 area / TV
            # border; in 16:9 / true-4:3 rs_surf IS the full screen.
            rs_surf = _overlay_target(screen)
            w_w, w_h = rs_surf.get_size()
            
            # NATIVE UNIFORM CALIBRATION: Sizing scales remain fixed to their native layouts
            native_box_w = 650
            native_box_h = 240
            
            box_w = min(native_box_w, w_w - 20)
            box_h = min(native_box_h, w_h - 20)
            box_x = (w_w - box_w) // 2
            box_y = (w_h - box_h) // 2
            
            scale_factor = 1.0 if w_w >= native_box_w else (w_w / 1920.0)
            
            # --- DYNAMIC THEME COLOR MAPPING ---
            import colorsys
            bg_hue = db.config.get("theme_bg_hue", 220) / 360.0 if db is not None else 220/360.0
            ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140/360.0
            r, g, b = colorsys.hsv_to_rgb(ui_hue, 0.9, 1.0)
            NEON_GREEN = (int(r * 255), int(g * 255), int(b * 255))
            r, g, b = colorsys.hsv_to_rgb(bg_hue, 0.6, 0.15)
            MIDNIGHT_NAVY = (int(r * 255), int(g * 255), int(b * 255))
            TEXT_WHITE  = (255, 255, 255)
            ARCADE_GOLD = (255, 220, 0)
            ACTION_RED  = (255, 60, 60)
            
            # FIXED OPACITY MAPPING: Transparent popup background using matching Alpha slider channel
            slider_val = db.config.get("menu_opacity", 50) if db is not None else 50
            menu_opacity = int((slider_val / 100.0) * 255)
            popup_surface = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            popup_surface.fill(MIDNIGHT_NAVY + (menu_opacity,))
            rs_surf.blit(popup_surface, (box_x, box_y))
            
            border_thickness = 2 if w_w >= native_box_w else max(1, int(2 * scale_factor))
            pygame.draw.rect(rs_surf, NEON_GREEN, (box_x, box_y, box_w, box_h), border_thickness)
            
            size_lg = 18 if w_w >= native_box_w else max(10, int(18 * scale_factor))
            size_sm = 15 if w_w >= native_box_w else max(6, int(15 * scale_factor))
            size_info = 12 if w_w >= native_box_w else max(6, int(12 * scale_factor))
            
            font_lg = _cached_sys_font("Courier New", size_lg, bold=True)
            font_sm = _cached_sys_font("Courier New", size_sm, bold=True)
            font_info = _cached_sys_font("Courier New", size_info, bold=True)
            
            lbl_w1 = font_lg.render("RESTART IS REQUIRED TO", True, ARCADE_GOLD)
            lbl_w2 = font_lg.render("MAKE THIS CHANGE", True, ARCADE_GOLD)
            lbl_setup_warning = font_info.render("Make sure ES-DE.exe is already setup before turning on channel 03", True, ACTION_RED)
            
            padding_y1 = 20 if w_w >= native_box_w else max(5, int(20 * scale_factor))
            padding_y2 = 45 if w_w >= native_box_w else max(10, int(45 * scale_factor))
            padding_y3 = 75 if w_w >= native_box_w else max(15, int(75 * scale_factor))
            
            rs_surf.blit(lbl_w1, (box_x + (box_w // 2) - (lbl_w1.get_width() // 2), box_y + padding_y1))
            rs_surf.blit(lbl_w2, (box_x + (box_w // 2) - (lbl_w2.get_width() // 2), box_y + padding_y2))
            rs_surf.blit(lbl_setup_warning, (box_x + (box_w // 2) - (lbl_setup_warning.get_width() // 2), box_y + padding_y3))
            
            current_choice = app_state.get("restart_confirm_selection", "No")
            yes_color = NEON_GREEN if current_choice == "Yes" else TEXT_WHITE
            no_color = NEON_GREEN if current_choice == "No" else TEXT_WHITE
            
            lbl_yes = font_sm.render("[ YES ]", True, yes_color)
            lbl_no  = font_sm.render("[ NO ]", True, no_color)
            
            button_y = box_y + (135 if w_w >= native_box_w else max(25, int(135 * scale_factor)))
            yes_x = box_x + int(box_w * 0.25) - (lbl_yes.get_width() // 2)
            no_x  = box_x + int(box_w * 0.75) - (lbl_no.get_width() // 2)
            
            rs_surf.blit(lbl_yes, (yes_x, button_y))
            rs_surf.blit(lbl_no, (no_x, button_y))
            
            time_left_sec = max(0, 20 - int(elapsed_popup_ms / 1000.0))
            lbl_foot = font_sm.render(f"AUTO-CLOSE IN {time_left_sec}S TO REVERT", True, ACTION_RED)
            footer_y = box_y + box_h - (25 if w_w >= native_box_w else max(5, int(25 * scale_factor)))
            rs_surf.blit(lbl_foot, (box_x + (box_w // 2) - (lbl_foot.get_width() // 2), footer_y))
            
    else:
        now = pygame.time.get_ticks()
        w_w, w_h = screen.get_size()
        
        # --- DYNAMIC OSD THEME COLOR ---
        import colorsys as _osd_colorsys
        _osd_ui_hue = db.config.get("theme_ui_hue", 140) / 360.0 if db is not None else 140 / 360.0
        _r, _g, _b = _osd_colorsys.hsv_to_rgb(_osd_ui_hue, 0.9, 1.0)
        OSD_COLOR = (int(_r * 255), int(_g * 255), int(_b * 255))
        
        if 'visualizer_deck' in globals() and visualizer_deck is not None:
            if hasattr(visualizer_deck, "flash_intensity"): visualizer_deck.flash_intensity = 0.0
            if hasattr(visualizer_deck, "bg_flash"): visualizer_deck.bg_flash = 0
            if hasattr(visualizer_deck, "galaxy_glow"): visualizer_deck.galaxy_glow = 0
            
        if now < app_state.get("osd_channel_timer", 0):
            f_osd = _cached_sys_font("Courier New", 54, bold=True)
            display_txt = app_state.get("digit_buffer") or app_state["current_channel"]
            screen.blit(f_osd.render(display_txt, True, OSD_COLOR),
                        (content_rect.right - 120, content_rect.y + 50))
            
        # If a DVD transport OSD (pause/FF/rewind/captions) just fired, dismiss
        # the volume OSD so the two never stack on top of each other.
        if (game_deck is not None
                and getattr(game_deck, "dvd_osd_msg", "") != ""
                and now < getattr(game_deck, "dvd_osd_until", 0)):
            app_state["osd_volume_timer"] = 0

        if now < app_state.get("osd_volume_timer", 0):
            _vol_cx  = content_rect.centerx
            _vol_bot = content_rect.bottom - 120   # same baseline as DVD OSD (y + h - 120)
            f_vol_text = _cached_sys_font("Courier New", 22, bold=True)
            f_vol_bar  = _cached_sys_font("Courier New", 44, bold=True)
            if db.config.get("is_muted", False):
                center_x = _vol_cx
                base_y = _vol_bot
                pygame.draw.rect(screen, OSD_COLOR, (center_x - 22, base_y + 12, 14, 14))
                pygame.draw.polygon(screen, OSD_COLOR, [
                    (center_x - 10, base_y + 12), (center_x + 4, base_y + 2),
                    (center_x + 4, base_y + 36), (center_x - 10, base_y + 26)
                ])
                pygame.draw.line(screen, OSD_COLOR, (center_x + 14, base_y + 10), (center_x + 28, base_y + 24), 3)
                pygame.draw.line(screen, OSD_COLOR, (center_x + 28, base_y + 10), (center_x + 14, base_y + 24), 3)
                f_mute = _cached_sys_font("Courier New", 14, bold=True)
                lbl_m = f_mute.render("MUTED", True, OSD_COLOR)
                screen.blit(lbl_m, (center_x - (lbl_m.get_width() // 2), base_y + 44))
            else:
                _vol_pct = db.config.get("global_volume", 70)
                lbl_v = f_vol_text.render(f"VOLUME: {_vol_pct}%", True, OSD_COLOR)
                screen.blit(lbl_v, (_vol_cx - lbl_v.get_width() // 2, _vol_bot))
                # Progress bar — sized to 44pt equivalent width, below the text
                _bar_ref = f_vol_bar.size("VOLUME: 100%")[0]
                _bar_w = _bar_ref
                _bar_h = 10
                _bar_x = _vol_cx - _bar_w // 2
                _bar_y = _vol_bot + lbl_v.get_height() + 6
                _fill_w = int(_bar_w * _vol_pct / 100)
                pygame.draw.rect(screen, (80, 80, 80),   (_bar_x, _bar_y, _bar_w, _bar_h), border_radius=5)
                pygame.draw.rect(screen, OSD_COLOR,      (_bar_x, _bar_y, _fill_w, _bar_h), border_radius=5)
                pygame.draw.rect(screen, OSD_COLOR,      (_bar_x, _bar_y, _bar_w, _bar_h), 1, border_radius=5)

    # PYGAME FILE EXPLORER: render overlay on top of everything
    # NOTE: Do NOT call mouse.set_pos() here — it generates a MOUSEMOTION event
    # every frame which causes the cursor to jitter and never settle.
    # Hit-testing in _fe_handle_event already ignores clicks outside the explorer.
    if app_state.get("file_explorer_active", False):
        _fe_sw, _fe_sh = screen.get_size()
        _fe_w = 1036
        _fe_h = 680
        if _fe_sw < _fe_w or _fe_sh < _fe_h:
            _fe_w = min(_fe_w, _fe_sw - 20)
            _fe_h = min(_fe_h, _fe_sh - 20)
        _fe_x = (_fe_sw - _fe_w) // 2
        _fe_y = (_fe_sh - _fe_h) // 2
        _fe_render(screen, active_theme, (_fe_x, _fe_y, _fe_w, _fe_h))

    # ── CHANNEL 03 GAME-WINDOW LAYERING ───────────────────────────────────────
    # mGBA mounts as a native WS_CHILD window; pygame physically cannot paint over
    # it. So the TV-shell UI (settings menu, splash, quit/restart prompts, file
    # explorer, channel/volume/mute OSD, and the channel-switch black shield) is
    # made visible by HIDING the game window whenever any of that UI is on screen,
    # and showing it — fitted to the TV screen rect (content_rect) — only during
    # clean gameplay. The game keeps running/audible while hidden; actually leaving
    # channel 03 still suspends it via set_viewport_state().
    # Libretro games render into pygame directly — skip child-window sync for them.
    _lr_active = (
        game_deck is not None
        and getattr(game_deck, "_libretro_core", None) is not None
        and game_deck._libretro_core.is_loaded
    )

    # ── Libretro soft-freeze on menu/overlay open/close ───────────────────────
    # Menu, controls splash, quit confirm → pause core (no save).
    # Edge-detected via _prev_lr_overlay so freeze/unfreeze fires once per transition.
    #
    # NOTE: emulator_loading / _libretro_loading are intentionally EXCLUDED here.
    # The load screen waits for the core to produce a real, non-black frame
    # (see LibretroCore.has_visible_frame) before it clears — which requires
    # retro_run() to keep firing the whole time, e.g. through a slow PS1 BIOS
    # boot. Freezing the core because the load screen itself is showing was a
    # deadlock: paused core -> no new frames -> load screen can never detect
    # completion -> stuck until the safety-valve timeout forces it through.
    # The game window stays hidden during loading regardless (see
    # _lr_active handling above), so there's nothing visually wrong with
    # leaving the core running while the splash covers it.
    if _lr_active and ch == "03":
        _lr_overlay = (
            app_state.get("show_menu", False)
            or app_state.get("show_splash", False)
            or app_state.get("show_quit_confirm", False)
            or app_state.get("show_restart_confirm", False)
        )
        _prev_lr_overlay = app_state.get("_prev_lr_overlay", False)
        if _lr_overlay and not _prev_lr_overlay:
            if hasattr(game_deck, "freeze_libretro_soft"):
                game_deck.freeze_libretro_soft()
        elif not _lr_overlay and _prev_lr_overlay:
            if hasattr(game_deck, "unfreeze_libretro_soft"):
                game_deck.unfreeze_libretro_soft()
        app_state["_prev_lr_overlay"] = _lr_overlay

    if game_deck is not None and getattr(game_deck, "is_embedded", False) and not _lr_active:
        _now_layer = pygame.time.get_ticks()
        _overlay_active = (
            app_state.get("show_menu", False)
            or app_state.get("show_splash", False)
            or app_state.get("show_quit_confirm", False)
            or app_state.get("show_restart_confirm", False)
            or app_state.get("file_explorer_active", False)
            or _now_layer < app_state.get("osd_volume_timer", 0)
            or _now_layer < app_state.get("osd_channel_timer", 0)
            or _now_layer < app_state.get("channel_switch_black_until", 0)
        )
        _game_live = (
            ch == "03"
            and not getattr(game_deck, "emulator_loading", False)
            and not getattr(game_deck, "is_frozen", False)
            and game_deck.is_process_running()
        )
        game_deck.sync_child_window(_game_live, content_rect)
        if _overlay_active and _game_live:
            game_deck.freeze_inputs()

    # ==========================================================================
    # CH03 MENU MUSIC — stop when a game/DVD launches, start when returning to browse
    # ==========================================================================
    if app_state.get("current_channel") == "03" and not app_state.get("is_loading", False) and game_deck is not None:
        _mm_mode = getattr(game_deck, "mode", "BROWSE")
        _mm_dvd  = getattr(game_deck, "dvd_playback_active", False)
        _mm_prev = app_state.get("_ch03_mm_last_mode", "BROWSE")
        # BROWSE and ROMLIST are both menu-navigation states — music plays through both
        _mm_browsing      = _mm_mode in ("BROWSE", "ROMLIST")
        _mm_prev_browsing = _mm_prev in ("BROWSE", "ROMLIST")
        # STATIC/BLACK TRANSITION GUARD: while the channel-change shield is still
        # covering the screen (static burst on a ch03 tune), this watchdog must NOT
        # START any menu music -- otherwise the logo/rom-area game music is audible
        # UNDER the static instead of waiting for it to finish. Starting music is
        # already deferred to _run_pending_channel_activation() (fired the instant
        # the shield expires); this watchdog was the one path bypassing that timing,
        # because during the shield the deferred music hasn't begun yet, so
        # _ch03_music_is_playing() is False and the "resume"/"track finished"
        # branches below would fire early. Stopping music (game/DVD launch) is still
        # allowed through the shield -- only the start/resume/advance/restart
        # branches are held until the shield is gone.
        _mm_shield_active = pygame.time.get_ticks() < app_state.get("channel_switch_black_until", 0)
        # Transition into game or DVD — stop music
        if (_mm_mode in ("GAME", "DVD_PLAYER") or _mm_dvd) and _mm_prev_browsing:
            _stop_ch03_menu_music()
        # Transition back to browse/romlist — resume music
        elif _mm_browsing and not _mm_dvd and not _mm_prev_browsing and not _mm_shield_active:
            _start_ch03_menu_music()
        # Track finished naturally — advance to next
        elif (_mm_browsing and not _mm_dvd and not _mm_shield_active
              and not _ch03_music_is_playing()
              and db is not None and db.config.get("ch03_menu_music")):
            _playlist = db.config["ch03_menu_music"]
            _cur_idx  = app_state.get("ch03_menu_music_idx", 0)
            app_state["ch03_menu_music_idx"] = (_cur_idx + 1) % len(_playlist)
            _start_ch03_menu_music()
        # A play() attempt started but never actually got past Opening/
        # Buffering (see _ch03_music_is_stalled docstring) — force a hard
        # stop+restart of the SAME track rather than waiting forever on a
        # silent channel. Same track (not the next one) since nothing was
        # actually heard, so there's nothing to "advance" past.
        elif (_mm_browsing and not _mm_dvd and not _mm_shield_active
              and db is not None and db.config.get("ch03_menu_music_enabled", True)
              and _ch03_music_is_stalled()):
            print("[CH03 MUSIC] Playback stalled in Opening/Buffering — forcing restart.")
            _stop_ch03_menu_music()
            _start_ch03_menu_music()
        app_state["_ch03_mm_last_mode"] = _mm_mode if not _mm_dvd else "DVD_PLAYER"

    # ==========================================================================
    # ==========================================================================
    # CH03 SCREEN SAVER — bouncing logo after inactivity
    # Active on ch03 whenever a DVD is NOT playing.
    # Timeout: user-configurable in the menu (Games tab, below the Screen Saver
    # on/off toggle), 5-30 min in 5-min steps. Same timeout applies whether or
    # not a game is actively running.
    # Freezes the emulator on activation; unfreezes on dismiss.
    # ==========================================================================
    if app_state.get("current_channel") == "03" and not app_state.get("is_loading", False):
        _dvd_playing = (
            game_deck is not None
            and getattr(game_deck, "dvd_playback_active", False)
            and not getattr(game_deck, "dvd_paused", True)
        )
        _ss_enabled = db.config.get("ch03_screensaver_enabled", True) if db is not None else True
        if not _dvd_playing and _ss_enabled:
            _ss_now = pygame.time.get_ticks()
            # Initialise last_input timestamp on first visit to ch03
            if app_state.get("ch03_screensaver_last_input", 0) == 0:
                app_state["ch03_screensaver_last_input"] = _ss_now

            # Is a game actively running right now?
            _game_is_running = (
                game_deck is not None
                and getattr(game_deck, "mode", "BROWSE") == "GAME"
                and not getattr(game_deck, "emulator_loading", False)
                and not getattr(game_deck, "is_frozen", False)
                and game_deck.is_process_running()
            )

            # Real gameplay controller input never reaches the pygame event
            # queue -- it's read directly via XInput in _poll_libretro_input,
            # not posted as KEYDOWN/JOY* events -- so it can't reset
            # ch03_screensaver_last_input the normal way. Pull the timestamp
            # game_deck stamps on every real button/stick press instead, and
            # adopt it if it's newer than what we already have.
            if _game_is_running:
                _gp_last_input = getattr(game_deck, "last_gameplay_input_ticks", 0)
                if _gp_last_input > app_state["ch03_screensaver_last_input"]:
                    app_state["ch03_screensaver_last_input"] = _gp_last_input

            _ss_idle_ms = _ss_now - app_state["ch03_screensaver_last_input"]
            # User-configurable idle timeout, set in the Games tab menu under the
            # Screen Saver on/off toggle. Adjustable in 5-minute steps, 5-30 min.
            # Replaces the old fixed 120s(gameplay)/60s(idle) dual timeout.
            _ss_timeout_min = db.config.get("ch03_screensaver_timeout_min", 5) if db is not None else 5
            _SS_TIMEOUT_MS = _ss_timeout_min * 60_000

            # Lazy-load screensaver image — once per session into a module-level cache
            if "_ch03_ss_surf_cache" not in globals():
                globals()["_ch03_ss_surf_cache"] = None
            if globals()["_ch03_ss_surf_cache"] is None:
                _ss_img_path = os.path.join(base_dir, "main", "images", "screensaver", "ScreenSaver.png")
                try:
                    _raw = pygame.image.load(_ss_img_path).convert_alpha()
                    # Scale to 40% of the content region width, preserving aspect
                    # ratio. Uses content_rect (the 4:3 area in 4:3 Mode, full
                    # window otherwise) so the logo bounces inside the 4:3 screen /
                    # TV border rather than the whole 16:9 window.
                    _target_w = max(80, int(content_rect.width * 0.40))
                    _aspect   = _raw.get_height() / max(1, _raw.get_width())
                    _target_h = max(1, int(_target_w * _aspect))
                    globals()["_ch03_ss_surf_cache"] = pygame.transform.smoothscale(_raw, (_target_w, _target_h))
                except Exception as _ss_load_err:
                    print(f"[SCREENSAVER] Could not load ScreenSaver.png: {_ss_load_err}")
                    _fb = pygame.Surface((120, 60), pygame.SRCALPHA)
                    _fb.fill((255, 255, 255, 200))
                    globals()["_ch03_ss_surf_cache"] = _fb

            if _ss_idle_ms >= _SS_TIMEOUT_MS:
                _ss_surf = globals()["_ch03_ss_surf_cache"]
                _ss_w    = _ss_surf.get_width()
                _ss_h    = _ss_surf.get_height()
                # Bounce space = the content region (4:3 area in 4:3 Mode, full
                # window otherwise). ss_x/ss_y are kept relative to this box (0..
                # _scr_w) and offset by content_rect.x/y only at blit time, so the
                # logo stays inside the 4:3 screen / TV border.
                _scr_w, _scr_h = content_rect.width, content_rect.height

                # ── Initialise on first activation ────────────────────────────
                if not app_state.get("ch03_screensaver_active", False) or app_state.get("ch03_ss_x") is None:
                    import random as _ssrand
                    import math   as _ssmath
                    # Spawn in the centre quarter so first bounce isn't instant
                    _spawn_x_lo = _scr_w // 4
                    _spawn_x_hi = max(_spawn_x_lo, _scr_w - _ss_w - _scr_w // 4)
                    _spawn_y_lo = _scr_h // 4
                    _spawn_y_hi = max(_spawn_y_lo, _scr_h - _ss_h - _scr_h // 4)
                    app_state["ch03_ss_x"] = float(_ssrand.randint(_spawn_x_lo, _spawn_x_hi))
                    app_state["ch03_ss_y"] = float(_ssrand.randint(_spawn_y_lo, _spawn_y_hi))
                    # Angle between 28-62 deg avoids near-horizontal/vertical glides
                    _ss_angle = _ssrand.uniform(28, 62)
                    _ss_speed = 2.2
                    app_state["ch03_ss_vx"] = _ss_speed * _ssmath.cos(_ssmath.radians(_ss_angle)) * _ssrand.choice([-1, 1])
                    app_state["ch03_ss_vy"] = _ss_speed * _ssmath.sin(_ssmath.radians(_ss_angle)) * _ssrand.choice([-1, 1])
                    app_state["ch03_screensaver_active"]    = True
                    app_state["ch03_ss_was_game_running"]   = _game_is_running
                    # Freeze emulator so it doesn't run unseen behind the black screen
                    if _game_is_running and game_deck is not None and hasattr(game_deck, "freeze_libretro"):
                        print("[SCREENSAVER] Freezing emulator.")
                        game_deck.freeze_libretro()

                # ── Move then bounce (clamp prevents overshoot accumulation) ──
                _new_x = app_state["ch03_ss_x"] + app_state["ch03_ss_vx"]
                _new_y = app_state["ch03_ss_y"] + app_state["ch03_ss_vy"]

                # Left / right edges — logo must reach x=0 and x=scr_w-logo_w
                if _new_x <= 0:
                    _new_x = 0.0
                    app_state["ch03_ss_vx"] = abs(app_state["ch03_ss_vx"])
                elif _new_x + _ss_w > _scr_w:
                    _new_x = float(_scr_w - _ss_w)
                    app_state["ch03_ss_vx"] = -abs(app_state["ch03_ss_vx"])

                # Top / bottom edges — logo must reach y=0 and y=scr_h-logo_h
                if _new_y <= 0:
                    _new_y = 0.0
                    app_state["ch03_ss_vy"] = abs(app_state["ch03_ss_vy"])
                elif _new_y + _ss_h > _scr_h:
                    _new_y = float(_scr_h - _ss_h)
                    app_state["ch03_ss_vy"] = -abs(app_state["ch03_ss_vy"])

                app_state["ch03_ss_x"] = _new_x
                app_state["ch03_ss_y"] = _new_y

                # ── Render ──────────────────────��─────────────────────────────
                # Fill only the content region black and blit the logo offset
                # into it; the 4:3 black bars / TV border remain untouched.
                screen.fill((0, 0, 0), content_rect)
                screen.blit(_ss_surf, (content_rect.x + int(_new_x), content_rect.y + int(_new_y)))

        else:
            # DVD actively playing OR screensaver disabled — keep timer fresh so saver never fires
            app_state["ch03_screensaver_last_input"] = pygame.time.get_ticks()
            if app_state.get("ch03_screensaver_active", False):
                app_state["ch03_screensaver_active"] = False
                if app_state.get("ch03_ss_was_game_running", False) and game_deck is not None and hasattr(game_deck, "unfreeze_libretro"):
                    game_deck.unfreeze_libretro(muted=False)
                app_state["ch03_ss_was_game_running"] = False

    elif app_state.get("current_channel") != "03":
        # Left ch03 — reset everything so timer starts fresh on return
        if app_state.get("ch03_screensaver_active", False):
            app_state["ch03_screensaver_active"] = False
        app_state["ch03_screensaver_last_input"] = 0
        app_state["ch03_ss_x"] = None
        app_state["ch03_ss_y"] = None
        globals()["_ch03_ss_surf_cache"] = None  # force re-scale on next visit

    # Update the XInput nav thread's view of ch03/game state each frame.
    # This is the only cross-thread write — both fields are plain booleans so
    # no lock is needed (Python GIL makes bool assignment atomic).
    try:
        from game_deck import _xi_nav_state as _xns
        _xns["ch03_active"]  = (app_state.get("current_channel") == "03"
                                 and not app_state.get("is_loading", False)
                                 and not app_state.get("show_splash", False)
                                 and not app_state.get("show_menu", False)
                                 and not app_state.get("show_quit_confirm", False)
                                 # The game's own load screen (BIOS boot, ROM read, etc.)
                                 # is neither "browsing ch03" nor "a live running game" —
                                 # it's a third state the nav thread must sit out entirely.
                                 # Without this, ch03_active stayed True and game_running
                                 # was False (emulator_loading forces that) during loading,
                                 # so the nav thread kept treating it as the ROM browse
                                 # list and posted B → K_ESCAPE, popping the quit/minimize
                                 # confirm dialog before the game had even finished loading.
                                 and not getattr(game_deck, "emulator_loading", False)
                                 and not getattr(game_deck, "_libretro_loading", False))
        _xns["game_running"] = (game_deck is not None
                                 and getattr(game_deck, "mode", "BROWSE") == "GAME"
                                 and not getattr(game_deck, "emulator_loading", False)
                                 and not getattr(game_deck, "is_frozen", False))
        # browse_mode = on the top-level console logo page; B/Back does nothing here
        _xns["browse_mode"]  = (game_deck is not None
                                 and getattr(game_deck, "mode", "BROWSE") == "BROWSE")
        # dvd_active = DVD player is mounted — switches the nav thread to
        # single-press semantics (no held-repeat) so captions/pause/skip
        # controls behave like one button press, not a held menu-scroll.
        _xns["dvd_active"]   = (game_deck is not None
                                 and (getattr(game_deck, "mode", "") == "DVD_PLAYER"
                                      or getattr(game_deck, "dvd_playback_active", False)))
        # dvd_in_menu = an on-disc dvdnav menu is currently on screen. While
        # True the D-pad should keep posting ordinary W/A/S/D for
        # _handle_dvd_menu_nav, same as before; only once a title is
        # actually playing does the nav thread switch the D-pad over to
        # DVD_ACTION_CANONICAL's Z/X/F/V transport keys (see _DVD_NAV_MAP).
        _xns["dvd_in_menu"]  = bool(game_deck is not None
                                     and getattr(game_deck, "dvd_in_menu", False))
    except Exception as e:
        _log_warn_throttled("xi_nav_state_sync", "Per-frame XInput nav-state sync failed: %s", e)

    # 4:3 TEST MODE BORDER OVERLAY -- last thing drawn each frame so it sits
    # on top of whatever channel/menu/screensaver just rendered (see
    # _draw_full_tv_border_overlay's docstring). No-ops instantly unless
    # both the 4:3 Test Mode and Border toggles are on.
    # ── CRT POWER ANIMATION OVERLAY ──────────────────────────────────────────
    # Drawn last so it sits on top of every channel, menu, or screensaver.
    # Phase "off": picture squishes to a bright line then to a dot, fades out.
    # The post-animation shutdown call fires at the very end so it's hidden
    # completely behind the black frame.
    _crt = app_state.get("crt_anim")
    if _crt is not None:
        _ct = pygame.time.get_ticks() - _crt["start_ms"]
        _ctot = _crt.get("total_ms", 900)
        _csw, _csh = screen.get_size()

        if _crt["phase"] == "off":
            # Capture a snapshot of the current frame on the very first pass
            if _crt.get("snapshot") is None:
                _crt["snapshot"] = screen.copy()
            _snap = _crt["snapshot"]

            if _ct < 450:                         # Phase 1: vertical squish
                _p = _ct / 450.0
                _bh = max(3, int(_csh * (1.0 - _p)))
                _by = (_csh - _bh) // 2
                _sc = pygame.transform.smoothscale(_snap, (_csw, _bh))
                screen.fill((0, 0, 0))
                screen.blit(_sc, (0, _by))
                # Phosphor bloom brightens as the picture compresses
                _ga = min(210, int(_p * 230))
                _gs = pygame.Surface((_csw, _bh), pygame.SRCALPHA)
                _gs.fill((255, 255, 255, _ga))
                screen.blit(_gs, (0, _by))

            elif _ct < 700:                       # Phase 2: horizontal squish
                _p = (_ct - 450) / 250.0
                _lw = max(2, int(_csw * (1.0 - _p)))
                _lx = (_csw - _lw) // 2
                _ly = _csh // 2 - 2
                _br = max(80, int(255 * (1.0 - _p * 0.55)))
                screen.fill((0, 0, 0))
                pygame.draw.rect(screen, (_br, _br, _br), (_lx, _ly, _lw, 4))
                # Soft bloom bands above/below the line
                for _gi in range(1, 6):
                    _ga = max(0, 170 - _gi * 35)
                    _gs2 = pygame.Surface((_lw, 4 + _gi * 3), pygame.SRCALPHA)
                    _gs2.fill((255, 255, 255, _ga))
                    screen.blit(_gs2, (_lx, _ly - (_gi * 3 // 2)))

            else:                                 # Phase 3: dot fades to black
                _p = (_ct - 700) / 200.0
                _da = max(0, int(255 * (1.0 - _p)))
                _dw = max(2, int(14 * (1.0 - _p)))
                _dh = max(1, int(4 * (1.0 - _p)))
                screen.fill((0, 0, 0))
                pygame.draw.ellipse(screen, (_da, _da, _da),
                                    (_csw // 2 - _dw, _csh // 2 - _dh, _dw * 2, _dh * 2))

        # --- Post-animation actions (fire once, when timer expires) ---
        if _ct >= _ctot:
            _caction = _crt.get("action", "")
            app_state["crt_anim"] = None
            screen.fill((0, 0, 0))

            if _caction == "shutdown":
                pygame.display.flip()
                if vlc_engine is not None:
                    try: vlc_engine.stop()
                    except Exception: pass
                if dvd_engine is not None:
                    try: dvd_engine.stop()
                    except Exception: pass
                if game_deck is not None and hasattr(game_deck, "shutdown_deck"):
                    try: game_deck.shutdown_deck()
                    except Exception: pass
                if db is not None:
                    db.config["last_channel"] = app_state.get("pre_standby_channel", "05")
                    try: db.save_settings()
                    except Exception: pass
                if os.name == "nt":
                    try: subprocess.Popen(["shutdown", "/s", "/t", "0"])
                    except Exception as _ce: print(f"[SHUTDOWN] shutdown call failed: {_ce}")
                else:
                    print("[SHUTDOWN] Not Windows �� power-off skipped.")
                pygame.quit()
                sys.exit()

    _draw_full_tv_border_overlay(screen)

    pygame.display.flip()
    # Pace the loop to the running core's native refresh rate (e.g. 59.73 for
    # GBA, 60.10 for NES) instead of a flat 60. Matching the core's real fps
    # keeps audio production and consumption in step so the dynamic rate control
    # in LibretroAudio barely has to correct, eliminating periodic A/V drift.
    # Falls back to 60 for menus/non-game channels or if fps is unavailable.
    _tick_fps = 60.0
    _gd = globals().get("game_deck", None)
    if (_gd is not None
            and getattr(_gd, "mode", None) == "GAME"
            and getattr(_gd, "_libretro_core", None) is not None
            and not getattr(_gd, "emulator_loading", False)):
        try:
            _core_fps = float(_gd._libretro_core.fps)
            if 40.0 <= _core_fps <= 120.0:   # sane guard against bad av_info
                _tick_fps = _core_fps
        except Exception as e:
            _log_warn_throttled("core_fps_tick_pacing", "Per-frame core.fps lookup for tick pacing failed, using 60.0: %s", e)
    # Whole-loop-iteration wall time, recorded only while a game channel is
    # actually running a core. Compared against run_frame()'s own
    # frame_total in the [PERF] log: if loop_total is much bigger than
    # frame_total, the stutter is coming from something OUTSIDE the
    # libretro pipeline (TV-border overlay draw, event polling, other
    # channel bookkeeping still running each pass) rather than from the
    # core/video/audio/blit stages themselves.
    _now_loop_ts = time.monotonic()
    if (_last_loop_ts is not None and _gd is not None
            and getattr(_gd, "mode", None) == "GAME"
            and getattr(_gd, "_libretro_core", None) is not None):
        try:
            _gd.FRAME_PERF.record("loop_total", (_now_loop_ts - _last_loop_ts) * 1000.0)
        except Exception:
            pass
    _last_loop_ts = _now_loop_ts

    # Use the sleeping tick() (NOT tick_busy_loop). tick_busy_loop spin-waits at
    # 100% CPU until the next frame, which starves the audio thread and the OS
    # event loop during heavy work (e.g. background ROM probing) and makes
    # Windows flag the window as "Not Responding". tick() yields the CPU; the
    # dynamic rate control in LibretroAudio absorbs the slightly looser timing.
    clock.tick(_tick_fps)
