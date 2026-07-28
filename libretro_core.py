# ==============================================================================
# libretro_core.py  ─  Built-in libretro frontend for Retro TV Emulator
# ==============================================================================
#
# Loads a libretro .dll core (e.g. mgba_libretro.dll) inside the same process
# as pygame. No child windows, no .exe, no suspension hacks.
#
# INPUT NOTE:
#   This module does NOT poll joystick or XInput itself. Button states are fed
#   externally each frame by game_deck._poll_libretro_input(), which reuses the
#   exact same XInput code and ctrl_mapping already used by the existing
#   controller wireframe remapping UI. No new input UI is needed.
#
# Architecture:
#   · LibretroCore        — wraps one loaded core DLL via ctypes
#   · LibretroInputState  — thin dict holder; game_deck fills it each frame
#   · LibretroAudio       — collects samples from core callback → pygame.mixer
#   · CRTFilter           — numpy brightness/contrast/saturation/sharpness/hue
#   · build_core_for_console() — convenience factory used by game_deck
#
# Save states: <roms_dir>/<CONSOLE>/saves/<rom_name>_slot<N>.state
# ==============================================================================

import os
import sys
import math
import ctypes
import threading
import collections
import time
import logging

import pygame
import numpy as np

log = logging.getLogger(__name__)

# DEBUG SWITCH: when True, every retro_environment callback (cmd, arriving
# from inside retro_run on every core frame -- often dozens of times a
# second per frame while a game is running) is printed to app_log.txt with
# an immediate flush. This was left permanently on ("TEMP DIAGNOSTIC") and
# is the single biggest source of log spam in the app: it alone accounted
# for the large majority of a typical session's log volume, rotating the
# log file every few minutes during gameplay and adding a synchronous disk
# write on the hot path of every emulator frame.
# MUST stay False in normal operation. Only flip on briefly on a dev
# machine while diagnosing a specific core/env-cmd issue, then turn it back
# off -- see the identical warning on _HOOK_DEBUG in retro_tv_emulator.py
# for why leaving a per-event flush=True print on is dangerous long-term.
_ENV_CB_DEBUG = False


# ------------------------------------------------------------------------------
# Frame-stage profiler
# ------------------------------------------------------------------------------
# Every console channel (GB/GBC/GBA/NES/SNES/Genesis/N64/PSX/MAME/GG/NGP) runs
# through this SAME LibretroCore/LibretroAudio/render_libretro pipeline -- there
# is no per-console code path. So instead of adding one-off debug prints for
# whichever console is currently misbehaving, this collects per-stage timings
# generically, for whatever core happens to be loaded, so a single test pass
# across every console channel catches stage-level regressions on all of them
# at once instead of needing a separate debugging pass per console later.
#
# Cost is kept off the hot path: two perf_counter() calls per timed stage per
# frame, with all aggregation/printing done at most once every REPORT_INTERVAL
# seconds (not every frame). PERF_DEBUG defaults on since this is meant to run
# during a normal test pass; flip off for a quiet log during normal play.
PERF_DEBUG = True

# Stages reported in this fixed order when present, so successive summaries
# line up in the log instead of reshuffling column order every print.
_PERF_STAGE_ORDER = (
    "loop_total", "frame_total", "retro_run", "video_cb", "audio_tick",
    "crt_filter", "blit_scale", "border_blit",
)


class _FramePerf:
    """Low-overhead per-stage frame timing, aggregated and reported on a
    throttled interval. One shared instance (FRAME_PERF, below) is used by
    every console's core, so switching channels just relabels the same
    counters instead of needing a new profiler wired up per console."""

    REPORT_INTERVAL = 3.0  # seconds between summaries

    def __init__(self):
        self._sums          = {}
        self._counts        = {}
        self._maxes         = {}
        self._window_start  = time.monotonic()
        self._console_label = "?"

    def set_console(self, label):
        """Called whenever a console channel loads a core, so summaries are
        tagged with whichever console is actually running right now."""
        if label and label != self._console_label:
            # Reset the window on a console switch so one console's numbers
            # never bleed into another's summary line.
            self._console_label = label
            self._sums.clear(); self._counts.clear(); self._maxes.clear()
            self._window_start = time.monotonic()

    def record(self, stage, ms):
        self._sums[stage]   = self._sums.get(stage, 0.0) + ms
        self._counts[stage] = self._counts.get(stage, 0) + 1
        if ms > self._maxes.get(stage, 0.0):
            self._maxes[stage] = ms

    def maybe_report(self):
        if not PERF_DEBUG:
            return
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed < self.REPORT_INTERVAL or not self._counts:
            return
        frames = self._counts.get("frame_total", 0)
        fps = (frames / elapsed) if elapsed > 0 else 0.0
        cols = []
        for stage in _PERF_STAGE_ORDER:
            if stage in self._counts:
                n   = self._counts[stage]
                avg = self._sums[stage] / n
                mx  = self._maxes[stage]
                cols.append(f"{stage}={avg:5.2f}ms(max {mx:5.2f})")
        print(f"[PERF][{self._console_label}] {fps:5.1f}fps/{elapsed:.1f}s  " + "  ".join(cols))
        self._sums.clear(); self._counts.clear(); self._maxes.clear()
        self._window_start = now


FRAME_PERF = _FramePerf()


class _perf_timer:
    """Context manager: times its block and records it against FRAME_PERF
    under `stage`. No-op cost when PERF_DEBUG is False (still takes two
    perf_counter() calls, but skips the dict work in record() via the
    early-out in maybe_report(); record() itself is cheap enough to always
    run so PERF_DEBUG can be flipped on/off without restarting mid-session).
    Usage: `with _perf_timer("retro_run"): self._dll.retro_run()`"""
    __slots__ = ("stage", "_t0")

    def __init__(self, stage):
        self.stage = stage

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc_info):
        FRAME_PERF.record(self.stage, (time.perf_counter() - self._t0) * 1000.0)
        return False


# ------------------------------------------------------------------------------
# libretro constants
# ------------------------------------------------------------------------------

RETRO_API_VERSION = 1

# Environment commands
# NOTE: GET_CAN_DUPE was previously (incorrectly) defined as 8 here. Per the
# real libretro.h it is 3 -- 8 is actually SET_PERFORMANCE_LEVEL, an
# informational SET call where `data` points to the CORE's own unsigned
# value for us to read, not something we should write through. The old wrong
# mapping meant every SET_PERFORMANCE_LEVEL call (which MAME2003-Plus makes
# during retro_init()) was mishandled as if it were GET_CAN_DUPE, writing a
# stray bool byte into memory the core owns and never asked us to touch.
RETRO_ENVIRONMENT_GET_CAN_DUPE              = 3
RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL     = 8
RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY      = 9
RETRO_ENVIRONMENT_SET_PIXEL_FORMAT          = 10
RETRO_ENVIRONMENT_SET_DISK_CONTROL_INTERFACE = 13
RETRO_ENVIRONMENT_GET_VARIABLE              = 15
RETRO_ENVIRONMENT_SET_VARIABLES             = 16
RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE       = 17
RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME       = 18
RETRO_ENVIRONMENT_GET_LIBRETRO_PATH         = 19
RETRO_ENVIRONMENT_GET_RUMBLE_INTERFACE      = 23
RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY        = 31
RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO        = 32
RETRO_ENVIRONMENT_SET_CONTROLLER_INFO       = 35
RETRO_ENVIRONMENT_SET_GEOMETRY              = 37
RETRO_ENVIRONMENT_GET_USERNAME              = 38

# retro_get_memory() memory IDs. SAVE_RAM is the cartridge battery/SRAM save
# data (what shows up on disk as a ".sav"/".srm" file) -- this is the
# frontend-managed path EVERY compliant libretro core supports, independent of
# whether the core ALSO tries to write its own save file to disk using
# RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY (some do, some don't, some get it
# wrong and fall back to writing next to the ROM). Persisting SAVE_RAM
# ourselves means the save always lands exactly where we put it -- the
# active profile's folder -- regardless of any individual core's own
# directory-handling quirks.
RETRO_MEMORY_SAVE_RAM = 0
RETRO_ENVIRONMENT_GET_LANGUAGE              = 39
RETRO_ENVIRONMENT_GET_AUDIO_VIDEO_ENABLE    = 47
RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION  = 52
RETRO_ENVIRONMENT_SET_CORE_OPTIONS          = 53
RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2       = 67

# Pixel formats
RETRO_PIXEL_FORMAT_0RGB1555 = 0
RETRO_PIXEL_FORMAT_XRGB8888 = 1
RETRO_PIXEL_FORMAT_RGB565   = 2

# Joypad button IDs
RETRO_DEVICE_ID_JOYPAD_B      = 0
RETRO_DEVICE_ID_JOYPAD_Y      = 1
RETRO_DEVICE_ID_JOYPAD_SELECT = 2
RETRO_DEVICE_ID_JOYPAD_START  = 3
RETRO_DEVICE_ID_JOYPAD_UP     = 4
RETRO_DEVICE_ID_JOYPAD_DOWN   = 5
RETRO_DEVICE_ID_JOYPAD_LEFT   = 6
RETRO_DEVICE_ID_JOYPAD_RIGHT  = 7
RETRO_DEVICE_ID_JOYPAD_A      = 8
RETRO_DEVICE_ID_JOYPAD_X      = 9
RETRO_DEVICE_ID_JOYPAD_L      = 10
RETRO_DEVICE_ID_JOYPAD_R      = 11
RETRO_DEVICE_ID_JOYPAD_L2     = 12
RETRO_DEVICE_ID_JOYPAD_R2     = 13
RETRO_DEVICE_ID_JOYPAD_L3     = 14
RETRO_DEVICE_ID_JOYPAD_R3     = 15

RETRO_DEVICE_JOYPAD = 1
RETRO_DEVICE_ANALOG = 5

# RETRO_DEVICE_INDEX_ANALOG_* — which physical stick
RETRO_DEVICE_INDEX_ANALOG_LEFT  = 0
RETRO_DEVICE_INDEX_ANALOG_RIGHT = 1

# RETRO_DEVICE_ID_ANALOG_* — which axis on that stick
RETRO_DEVICE_ID_ANALOG_X = 0
RETRO_DEVICE_ID_ANALOG_Y = 1

# Language
RETRO_LANGUAGE_ENGLISH = 0

# ------------------------------------------------------------------------------
# Console button name → libretro joypad button ID
# Covers every console in game_deck.CONSOLE_BUTTON_DEFS: GB, GBC, GBA, NES,
# SNES, Genesis, N64, PSX, MAME, GG, NGP.
# ------------------------------------------------------------------------------
CONSOLE_BTN_TO_LIBRETRO = {
    # Directions (universal)
    "Up":     RETRO_DEVICE_ID_JOYPAD_UP,
    "Down":   RETRO_DEVICE_ID_JOYPAD_DOWN,
    "Left":   RETRO_DEVICE_ID_JOYPAD_LEFT,
    "Right":  RETRO_DEVICE_ID_JOYPAD_RIGHT,
    # System buttons
    "Start":  RETRO_DEVICE_ID_JOYPAD_START,
    "Select": RETRO_DEVICE_ID_JOYPAD_SELECT,
    "Option": RETRO_DEVICE_ID_JOYPAD_SELECT,   # NGP "Option" = Select
    "Coin":   RETRO_DEVICE_ID_JOYPAD_SELECT,   # MAME "Coin" = Select (insert coin)
    # Face buttons
    "A":      RETRO_DEVICE_ID_JOYPAD_A,
    "B":      RETRO_DEVICE_ID_JOYPAD_B,
    "X":      RETRO_DEVICE_ID_JOYPAD_X,
    "Y":      RETRO_DEVICE_ID_JOYPAD_Y,
    # Shoulder
    "L":      RETRO_DEVICE_ID_JOYPAD_L,
    "R":      RETRO_DEVICE_ID_JOYPAD_R,
    "L2":     RETRO_DEVICE_ID_JOYPAD_L2,
    "R2":     RETRO_DEVICE_ID_JOYPAD_R2,
    "L3":     RETRO_DEVICE_ID_JOYPAD_L3,
    "R3":     RETRO_DEVICE_ID_JOYPAD_R3,
    # SNES
    "1":      RETRO_DEVICE_ID_JOYPAD_B,        # NES 1 = B
    "2":      RETRO_DEVICE_ID_JOYPAD_A,        # NES 2 = A
    # Genesis / Mega Drive — A/B/X/Y map directly onto their same-named
    # joypad IDs (this frontend has its own wireframe/remap UI, so the A/B/
    # X/Y half doesn't need to match RetroArch's own swapped retropad
    # convention). C and Z DO follow Genesis Plus GX's real convention
    # (C=R1, Z=L1 in libretro terms — JOYPAD_R/JOYPAD_L are the spec's L1/R1
    # IDs, there's no separate "L1" id).
    "C":      RETRO_DEVICE_ID_JOYPAD_R,
    "Z":      RETRO_DEVICE_ID_JOYPAD_L,
    "Mode":   RETRO_DEVICE_ID_JOYPAD_SELECT,
    # PlayStation — standard libretro convention: Cross/Circle/Square/Triangle
    # map onto the same joypad IDs as A/B/X/Y, L1/R1 onto L/R.
    "Cross":    RETRO_DEVICE_ID_JOYPAD_A,
    "Circle":   RETRO_DEVICE_ID_JOYPAD_B,
    "Square":   RETRO_DEVICE_ID_JOYPAD_X,
    "Triangle": RETRO_DEVICE_ID_JOYPAD_Y,
    "L1":       RETRO_DEVICE_ID_JOYPAD_L,
    "R1":       RETRO_DEVICE_ID_JOYPAD_R,
    # Nintendo 64 — C-buttons are read by the core primarily via the right
    # analog stick (see _poll_libretro_input's N64 analog block + the
    # RETRO_DEVICE_ANALOG handling in _cb_input_state — that's the path real
    # N64 cores use by default). These joypad-ID entries are a secondary,
    # rarely-hit fallback: only reachable if a button is explicitly remapped
    # to "CUp"/etc. in the controller-mapping UI, or for cores running in
    # "Independent C-button Controls" mode. Chosen to avoid colliding with
    # N64's own A, B, Z→X, L, R (all already taken).
    "CUp":    RETRO_DEVICE_ID_JOYPAD_Y,
    "CDown":  RETRO_DEVICE_ID_JOYPAD_SELECT,
    "CLeft":  RETRO_DEVICE_ID_JOYPAD_L2,
    "CRight": RETRO_DEVICE_ID_JOYPAD_R2,
    # MAME — numbered action buttons, mapped per researched arcade convention:
    # Button1=A, Button2=B, Button3=R1, Button4=R2, Button5=X, Button6=Y,
    # Button7=L1, Button8=L2 (physical controller roles, not RetroArch's
    # older 6-button default order).
    "Button1": RETRO_DEVICE_ID_JOYPAD_A,
    "Button2": RETRO_DEVICE_ID_JOYPAD_B,
    "Button3": RETRO_DEVICE_ID_JOYPAD_R,    # R1
    "Button4": RETRO_DEVICE_ID_JOYPAD_R2,   # R2
    "Button5": RETRO_DEVICE_ID_JOYPAD_X,
    "Button6": RETRO_DEVICE_ID_JOYPAD_Y,
    "Button7": RETRO_DEVICE_ID_JOYPAD_L,    # L1
    "Button8": RETRO_DEVICE_ID_JOYPAD_L2,   # L2
}

# ------------------------------------------------------------------------------
# Per-console overrides for CONSOLE_BTN_TO_LIBRETRO.
# The table above is a flat name→id lookup, which works because most shared
# names ("A", "B", "Up"...) mean the same role on every console that uses
# them. "Z" is the one exception: Genesis's Z is a 6th face button (mapped to
# match Genesis Plus GX's real retropad convention, C=R1/Z=L1) while N64's Z
# is a dedicated shoulder trigger with no relation to Genesis's layout — they
# can't share one global id. Look up here FIRST; fall back to the flat table
# for every console/button combo that isn't listed.
# ------------------------------------------------------------------------------
CONSOLE_BTN_TO_LIBRETRO_OVERRIDES = {
    ("N64", "Z"): RETRO_DEVICE_ID_JOYPAD_X,
}

def lr_id_for(console, btn_name):
    """Resolve a console's button name to a libretro joypad ID, checking the
    per-console override table before falling back to the shared one."""
    override = CONSOLE_BTN_TO_LIBRETRO_OVERRIDES.get((console, btn_name))
    if override is not None:
        return override
    return CONSOLE_BTN_TO_LIBRETRO.get(btn_name)


# ------------------------------------------------------------------------------
# ctypes structures
# ------------------------------------------------------------------------------

class retro_system_info(ctypes.Structure):
    _fields_ = [
        ("library_name",     ctypes.c_char_p),
        ("library_version",  ctypes.c_char_p),
        ("valid_extensions", ctypes.c_char_p),
        ("need_fullpath",    ctypes.c_bool),
        ("block_extract",    ctypes.c_bool),
    ]


class retro_game_geometry(ctypes.Structure):
    _fields_ = [
        ("base_width",   ctypes.c_uint),
        ("base_height",  ctypes.c_uint),
        ("max_width",    ctypes.c_uint),
        ("max_height",   ctypes.c_uint),
        ("aspect_ratio", ctypes.c_float),
    ]


class retro_system_timing(ctypes.Structure):
    _fields_ = [
        ("fps",         ctypes.c_double),
        ("sample_rate", ctypes.c_double),
    ]


class retro_system_av_info(ctypes.Structure):
    _fields_ = [
        ("geometry", retro_game_geometry),
        ("timing",   retro_system_timing),
    ]


class retro_game_info(ctypes.Structure):
    _fields_ = [
        ("path",  ctypes.c_char_p),
        ("data",  ctypes.c_void_p),
        ("size",  ctypes.c_size_t),
        ("meta",  ctypes.c_char_p),
    ]


class retro_variable(ctypes.Structure):
    _fields_ = [
        ("key",   ctypes.c_char_p),
        ("value", ctypes.c_char_p),
    ]


# retro_disk_control_callback — handed to us BY THE CORE (not filled by us) via
# RETRO_ENVIRONMENT_SET_DISK_CONTROL_INTERFACE. Used for live multi-disc swaps
# (e.g. PSX games with a Disc 1 / Disc 2) without unloading/reloading the core,
# so save-RAM and in-progress state survive the swap.
RETRO_SET_EJECT_STATE_T     = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_bool)
RETRO_GET_EJECT_STATE_T     = ctypes.CFUNCTYPE(ctypes.c_bool)
RETRO_GET_IMAGE_INDEX_T     = ctypes.CFUNCTYPE(ctypes.c_uint)
RETRO_SET_IMAGE_INDEX_T     = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
RETRO_GET_NUM_IMAGES_T      = ctypes.CFUNCTYPE(ctypes.c_uint)
RETRO_ADD_IMAGE_INDEX_T     = ctypes.CFUNCTYPE(ctypes.c_bool)
RETRO_REPLACE_IMAGE_INDEX_T = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.POINTER(retro_game_info))


class retro_disk_control_callback(ctypes.Structure):
    _fields_ = [
        ("set_eject_state",     RETRO_SET_EJECT_STATE_T),
        ("get_eject_state",     RETRO_GET_EJECT_STATE_T),
        ("get_image_index",     RETRO_GET_IMAGE_INDEX_T),
        ("set_image_index",     RETRO_SET_IMAGE_INDEX_T),
        ("get_num_images",      RETRO_GET_NUM_IMAGES_T),
        ("add_image_index",     RETRO_ADD_IMAGE_INDEX_T),
        ("replace_image_index", RETRO_REPLACE_IMAGE_INDEX_T),
    ]


# ------------------------------------------------------------------------------
# Callback types
# ------------------------------------------------------------------------------

RETRO_ENVIRONMENT_CB        = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)
RETRO_VIDEO_REFRESH_CB      = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t)
RETRO_AUDIO_SAMPLE_CB       = ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
RETRO_AUDIO_SAMPLE_BATCH_CB = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t)
RETRO_INPUT_POLL_CB         = ctypes.CFUNCTYPE(None)
RETRO_INPUT_STATE_CB        = ctypes.CFUNCTYPE(ctypes.c_int16, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)


# ==============================================================================
# Input state — thin holder; game_deck fills it from XInput each frame
# ==============================================================================

class LibretroInputState:
    """
    Holds the current joypad button state for one port (player 1).
    game_deck._poll_libretro_input() populates this each frame by reading
    XInput through the same API as the existing ctrl_mapping / remap UI.
    The core calls get_button() inside its retro_run() via the input_state callback.
    """

    def __init__(self):
        self._buttons = {}   # RETRO_DEVICE_ID_JOYPAD_* → bool
        # Analog stick axes, range -32768..32767 (matches libretro/XInput convention).
        # (index, id) -> value, e.g. (RETRO_DEVICE_INDEX_ANALOG_RIGHT, RETRO_DEVICE_ID_ANALOG_X)
        self._analog = {}

    def set_buttons(self, state_dict):
        """Replace the full button state.  state_dict: {libretro_id: bool}"""
        self._buttons = dict(state_dict)

    def set_analog(self, index, axis_id, value):
        """Set one analog axis. value range: -32768..32767."""
        self._analog[(index, axis_id)] = int(max(-32768, min(32767, value)))

    def get_button(self, btn_id):
        return 1 if self._buttons.get(btn_id, False) else 0

    def get_analog(self, index, axis_id):
        return self._analog.get((index, axis_id), 0)

    def both_pressed(self, a, b):
        return bool(self._buttons.get(a, False) and self._buttons.get(b, False))

    def clear(self):
        self._buttons = {}
        self._analog  = {}


# ==============================================================================
# Audio stream player
# ==============================================================================

class LibretroAudio:
    """
    Gapless, GC-safe, drift-corrected audio pipeline for libretro cores.

    Why the old version sounded "gross / laggy / stuttering"
    ────────────────────────────────────────────────────────
      1. PER-CHUNK RESAMPLE CLICKS.  Every 2048-sample chunk was resampled in
         isolation with np.linspace(0, 1, len). The interpolation grid reset to
         0 at the start of every chunk, so the sample *phase* did not carry
         across chunk boundaries → a tiny discontinuity (click/buzz) ≈ every
         46 ms. That is the constant "gross" texture, independent of framerate.
      2. UNDERRUNS.  pygame's Channel.queue() holds only ONE pending Sound, so
         the whole reservoir was ~2 chunks (~90 ms). Any slow frame (video
         decode, CRT filter, TV-guide work all share this one thread) drained
         it → silence gap → audible stutter. Nothing refilled it proactively.
      3. RATE DRIFT.  source rate (e.g. 32768/32040 Hz) vs the 44100 mixer was
         handled with a fixed ratio and no feedback, so the buffer slowly
         over/under-filled → periodic drops even at a steady framerate.

    How this version fixes it
    ──────────────────────────
      • CONTINUOUS-PHASE RESAMPLER.  A single fractional read position
        (self._pos) is carried across calls, and one overlap sample is kept
        between calls, so the resampled stream is phase-continuous → no clicks.
      • REICH RESERVOIR + DYNAMIC RATE CONTROL.  We hold an adjustable
        reservoir of already-resampled output (self._out) and nudge the
        resample ratio ±~0.4 % to keep that reservoir near a target depth.
        This absorbs frame-time jitter AND cancels long-term rate drift with no
        audible pitch change (this is exactly how RetroArch's DRC works).
      • DEQUE INGEST.  push_samples() appends to a deque (O(1)) instead of
        np.concatenate-ing the whole buffer every push, removing CPU churn on
        the render thread.
      • Same GC-safe Channel.play()/queue() anchoring as before so set_volume()
        can never free a live Sound.
    """

    CHANNEL_ID  = 6          # pygame.mixer.Channel reserved for game audio
    OUT_CHUNK   = 1024       # output (mixer-rate) pairs handed to the mixer at a time
    # Target reservoir of already-resampled output pairs kept in self._out.
    # ~4 mixer chunks ≈ 4096 pairs ≈ 93 ms at 44100 — deep enough to ride out
    # several slow frames, shallow enough to keep latency low.
    TARGET_OUT  = 4096
    MAX_OUT     = 12288      # hard cap on the reservoir to bound latency
    PREBUFFER   = 3072       # fill this many output pairs before first playback
    DRC_RANGE   = 0.004      # max ±fraction the ratio is nudged for drift control

    # Libretro cores output near full-scale int16 audio, which is far hotter
    # than the VLC / pygame.mixer playback used on the other channels. Without
    # attenuation the game channel is ~14x louder, so the only usable setting is
    # one volume step above silence. This factor scales the game channel down so
    # that a given global_volume % sounds the same on the game channel as on
    # every other channel. (5% usable ÷ 70% default ≈ 0.07.) Bumped up from
    # 0.10 → 0.35 so game audio is meaningfully audible at typical volume
    # settings and reaches a comfortable maximum at 100%.
    LOUDNESS_GAIN = 0.45

    def __init__(self, sample_rate=32768):
        self._sample_rate = int(sample_rate) if sample_rate else 32768

        # Ingest: deque of (frames, 2) int16 blocks straight from the core.
        self._in_blocks = collections.deque()
        self._in_count  = 0
        self._buf_lock  = threading.Lock()

        # Unconsumed source samples carried between ticks (float32 for clean
        # interpolation + 1 overlap sample so phase is continuous).
        self._src = np.empty((0, 2), dtype=np.float32)

        # Already-resampled output reservoir (mixer-rate int16 pairs).
        self._out = np.empty((0, 2), dtype=np.int16)

        # Fractional read position within self._src, carried across ticks.
        self._pos = 0.0

        self._channel     = None
        self._snd_playing = None    # hard refs so GC can't free a live Sound
        self._snd_queued  = None

        self._volume = 1.0
        self._muted  = False
        self._ready  = False        # prebuffer gate

        try:
            self._mixer_freq, _, self._mixer_channels = pygame.mixer.get_init()
        except Exception:
            self._mixer_freq     = 44100
            self._mixer_channels = 2

        # Base conversion ratio (mixer samples produced per source sample).
        self._base_ratio = (self._mixer_freq / self._sample_rate
                             if self._sample_rate > 0 else 1.0)

        # Throttle for hot-path error logs (push_samples/tick run per audio
        # callback / per frame; an unthrottled print would flood the console).
        self._last_log_ts = {}
        self._LOG_INTERVAL = 2.0  # seconds between repeats of the same message key

    # ------------------------------------------------------------------
    # Called from the core's audio batch callback (same thread as retro_run)
    # ------------------------------------------------------------------

    def _log_throttled(self, key, msg):
        """Print `msg` at most once per self._LOG_INTERVAL seconds per `key`.
        Used on the audio hot paths so a persistent fault reports itself without
        flooding the console every callback/frame."""
        now = time.monotonic()
        last = self._last_log_ts.get(key, 0.0)
        if now - last >= self._LOG_INTERVAL:
            self._last_log_ts[key] = now
            print(msg)

    def push_samples(self, ptr, frames):
        """Zero-copy ingest of int16 stereo pairs from a ctypes pointer."""
        if frames <= 0:
            return frames
        try:
            # numpy view of the core's memory (L R L R …) → (frames, 2), copied
            # so it detaches from the core's buffer before we hand control back.
            raw = np.frombuffer(
                (ctypes.c_int16 * (frames * 2)).from_address(
                    ctypes.cast(ptr, ctypes.c_void_p).value),
                dtype=np.int16
            ).reshape(frames, 2).copy()

            with self._buf_lock:
                self._in_blocks.append(raw)
                self._in_count += frames
                # Bound ingest backlog (e.g. if ticks stall) to avoid runaway latency.
                while self._in_count > self.MAX_OUT * 3 and len(self._in_blocks) > 1:
                    dropped = self._in_blocks.popleft()
                    self._in_count -= len(dropped)
        except Exception as exc:
            self._log_throttled("push_samples", f"[LIBRETRO AUDIO] push_samples error: {exc}")
        return frames

    # ------------------------------------------------------------------
    # Continuous-phase linear resampler
    # ------------------------------------------------------------------

    def _resample_into_out(self, ratio):
        """Resample everything currently in self._src into self._out using a
        carried fractional read position, then trim consumed source (keeping a
        1-sample overlap so the next call interpolates seamlessly)."""
        src = self._src
        n_src = len(src)
        if n_src < 2 or ratio <= 0:
            return

        # Output samples j map to source position pos = self._pos + j / ratio.
        # We can produce while pos <= n_src - 1 (need src[idx] and src[idx+1]).
        max_span = (n_src - 1) - self._pos
        if max_span <= 0:
            return
        n_out = int(np.floor(max_span * ratio)) + 1
        if n_out <= 0:
            return

        j   = np.arange(n_out, dtype=np.float64)
        pos = self._pos + j / ratio
        idx = np.floor(pos).astype(np.int64)
        np.clip(idx, 0, n_src - 2, out=idx)
        frac = (pos - idx).astype(np.float32)[:, None]

        out = src[idx] * (1.0 - frac) + src[idx + 1] * frac
        out = np.clip(out, -32768, 32767).astype(np.int16)

        # Advance the read head and carry the fractional remainder.
        new_pos  = self._pos + n_out / ratio
        consumed = int(np.floor(new_pos))
        if consumed > n_src - 1:
            consumed = n_src - 1
        self._pos = new_pos - consumed

        # Keep the unconsumed tail (plus the overlap sample at `consumed`).
        self._src = src[consumed:]

        # Append to the output reservoir; cap it to bound latency.
        if len(self._out):
            self._out = np.concatenate([self._out, out], axis=0)
        else:
            self._out = out
        if len(self._out) > self.MAX_OUT:
            self._out = self._out[-self.MAX_OUT:]

    # ------------------------------------------------------------------
    # Called once per main-loop frame (same thread as pygame rendering)
    # ------------------------------------------------------------------

    def tick(self):
        """Drain the core's samples, resample with drift control, and keep the
        mixer channel continuously fed."""
        if self._muted:
            with self._buf_lock:
                self._in_blocks.clear()
                self._in_count = 0
            return

        if self._channel is None:
            try:
                self._channel = pygame.mixer.Channel(self.CHANNEL_ID)
                self._channel.set_volume(self._volume)
            except Exception:
                return

        # 1) Pull newly-ingested source into the contiguous working buffer.
        with self._buf_lock:
            if self._in_blocks:
                blocks = list(self._in_blocks)
                self._in_blocks.clear()
                self._in_count = 0
            else:
                blocks = None
        if blocks:
            new_src = np.concatenate(blocks, axis=0).astype(np.float32)
            if len(self._src):
                self._src = np.concatenate([self._src, new_src], axis=0)
            else:
                self._src = new_src

        # 2) Dynamic rate control: nudge the ratio based on reservoir fill so the
        #    output buffer hovers around TARGET_OUT (cancels drift + jitter).
        buffered = len(self._out)
        error    = (buffered - self.TARGET_OUT) / float(self.TARGET_OUT)
        error    = max(-1.0, min(1.0, error))
        # Too full → produce slightly fewer output samples (ratio↓); too empty → ratio↑.
        ratio    = self._base_ratio * (1.0 - self.DRC_RANGE * error)

        # 3) Resample available source into the reservoir.
        self._resample_into_out(ratio)

        # 4) Prebuffer gate so the first frames after load don't underrun.
        if not self._ready:
            if len(self._out) < self.PREBUFFER:
                return
            self._ready = True

        # 5) Feed the mixer. The channel plays one chunk while exactly one more
        #    is queued; we top the queue slot back up whenever it frees.
        try:
            busy = self._channel.get_busy()
            if not busy:
                self._feed_one_chunk(start=True)        # (re)start after idle/underrun
                self._feed_one_chunk(start=False)       # immediately queue the next
            elif self._channel.get_queue() is None:
                self._feed_one_chunk(start=False)       # refill the freed queue slot
        except Exception as exc:
            self._log_throttled("tick_feed", f"[LIBRETRO AUDIO] tick feed error: {exc}")

    def _feed_one_chunk(self, start):
        """Pop one OUT_CHUNK of output and play or queue it on the channel."""
        if len(self._out) < self.OUT_CHUNK:
            return
        chunk     = np.ascontiguousarray(self._out[:self.OUT_CHUNK])
        self._out = self._out[self.OUT_CHUNK:]
        try:
            snd = pygame.sndarray.make_sound(chunk)
        except Exception as exc:
            print(f"[LIBRETRO AUDIO] make_sound error: {exc}")
            return
        # Promote refs so GC never frees a Sound the channel still needs.
        self._snd_playing = self._snd_queued
        self._snd_queued  = snd
        if start:
            self._channel.play(snd)
        else:
            self._channel.queue(snd)

    # ------------------------------------------------------------------
    # Live sample-rate update — some cores (notably Mupen64Plus-Next on
    # N64) call RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO mid-game to retune
    # their reported audio rate once they've measured real VI timing.
    # tick() recomputes `ratio` from self._base_ratio every call, so
    # updating it here is all that's needed — no reservoir reset, no
    # audible glitch, and it stops the resampler running against a stale
    # ratio (which is what produces a constant crackle/buzz).
    # ------------------------------------------------------------------

    def set_sample_rate(self, sample_rate):
        try:
            sample_rate = float(sample_rate)
        except (TypeError, ValueError):
            return
        if sample_rate <= 0 or sample_rate == self._sample_rate:
            return
        self._sample_rate = sample_rate
        self._base_ratio = (self._mixer_freq / self._sample_rate
                             if self._sample_rate > 0 else 1.0)
        self._log_throttled(
            "rate_change",
            f"[LIBRETRO AUDIO] Source sample rate updated to {sample_rate:.1f}Hz "
            f"(core renegotiated timing; e.g. N64 VI-rate detection).")

    # ------------------------------------------------------------------
    # Volume / mute — safe to call at any time, no array manipulation
    # ------------------------------------------------------------------

    def set_volume(self, volume_pct):
        # Curve the raw 0-100 slider position the same way the main VLC/
        # pygame volume paths do (see _perceptual_volume_pct in
        # retro_tv_emulator.py) -- otherwise the game channel would go back
        # to feeling "dead" from 100->50 and then diving from 50->15 while
        # every other channel now changes smoothly. Duplicated locally
        # (rather than imported) to avoid a cross-module import here.
        #
        # Uses a shallower -18dB range (STACKED_GAIN_MIN_DB in
        # retro_tv_emulator.py) rather than the standard -40dB, because
        # LOUDNESS_GAIN below is ALSO a fixed attenuation stacked on top of
        # this curve, to compensate for libretro's raw audio being much
        # hotter than the other channels' source material. Stacking the
        # full -40dB curve on top of that fixed cut compounded into
        # inaudibility far too early. -24dB (a first attempt) still wasn't
        # shallow enough -- real testing showed it goes inaudible at the
        # same absolute output level regardless of curve shape, just at a
        # different slider position, so -18dB was solved to push that
        # point down to roughly 10-15% slider, matching how the channels
        # without a stacked multiplier (TV, static) fade out.
        pct = max(0.0, min(100.0, float(volume_pct)))
        if pct <= 0:
            curved_frac = 0.0
        else:
            frac = pct / 100.0
            db_level = -18.0 * (1.0 - frac)
            curved_frac = 10 ** (db_level / 20.0)
        # Apply LOUDNESS_GAIN so the game channel matches the perceived volume of
        # the other channels at the same global_volume setting.
        self._volume = max(0.0, min(1.0, curved_frac * self.LOUDNESS_GAIN))
        if self._channel:
            try:
                self._channel.set_volume(self._volume)
            except Exception as e:
                log.warning("LibretroAudio.set_volume: pygame Channel.set_volume failed: %s", e)

    def set_muted(self, muted):
        self._muted = bool(muted)
        if muted:
            with self._buf_lock:
                self._in_blocks.clear()
                self._in_count = 0
            if self._channel:
                try:
                    self._channel.stop()
                except Exception as e:
                    log.warning("LibretroAudio.set_muted: pygame Channel.stop() failed while muting: %s", e)
            # Reset the resampler/reservoir so we re-prebuffer cleanly on unmute.
            self._src   = np.empty((0, 2), dtype=np.float32)
            self._out   = np.empty((0, 2), dtype=np.int16)
            self._pos   = 0.0
            self._ready = False
        else:
            if self._channel:
                try:
                    self._channel.set_volume(self._volume)
                except Exception as e:
                    log.warning("LibretroAudio.set_muted: pygame Channel.set_volume failed while unmuting: %s", e)

    def stop(self):
        if self._channel:
            try:
                self._channel.stop()
            except Exception as e:
                log.warning("LibretroAudio.stop: pygame Channel.stop() failed: %s", e)
        with self._buf_lock:
            self._in_blocks.clear()
            self._in_count = 0
        self._src         = np.empty((0, 2), dtype=np.float32)
        self._out         = np.empty((0, 2), dtype=np.int16)
        self._pos         = 0.0
        self._snd_playing = None
        self._snd_queued  = None
        self._ready       = False   # reset prebuffer for next ROM load


# ==============================================================================
# CRT Video Post-Processor
# ==============================================================================

class CRTFilter:
    """
    numpy-based CRT-style video adjustments applied at native resolution.
    All sliders are 0-100 integers; 50 = neutral for all.
    """

    def apply(self, surface, brightness=50, contrast=50, saturation=50,
              sharpness=50, hue_shift=50):
        if (brightness == 50 and contrast == 50 and saturation == 50
                and sharpness == 50 and hue_shift == 50):
            return surface

        try:
            arr = pygame.surfarray.array3d(surface).astype(np.float32)   # (w, h, 3)

            # brightness
            if brightness != 50:
                arr = arr * (brightness / 50.0)

            # contrast
            if contrast != 50:
                arr = (arr - 127.5) * (contrast / 50.0) + 127.5

            arr = np.clip(arr, 0, 255)

            # saturation
            if saturation != 50:
                grey = arr.mean(axis=2, keepdims=True)
                arr  = grey + (arr - grey) * (saturation / 50.0)
                arr  = np.clip(arr, 0, 255)

            # hue shift (±180° range mapped to 0-100 slider, 50=0°)
            if hue_shift != 50:
                angle = math.radians((hue_shift - 50) / 50.0 * 180.0)
                c, s  = math.cos(angle), math.sin(angle)
                k     = 1.0 / 3.0
                sq    = math.sqrt(k)
                r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]
                arr[:,:,0] = np.clip(r*(c+k*(1-c))+g*(k*(1-c)-sq*s)+b*(k*(1-c)+sq*s), 0, 255)
                arr[:,:,1] = np.clip(r*(k*(1-c)+sq*s)+g*(c+k*(1-c))+b*(k*(1-c)-sq*s), 0, 255)
                arr[:,:,2] = np.clip(r*(k*(1-c)-sq*s)+g*(k*(1-c)+sq*s)+b*(c+k*(1-c)), 0, 255)

            arr = arr.astype(np.uint8)

            # sharpness (unsharp mask)
            if sharpness > 50:
                strength = (sharpness - 50) / 50.0
                blurred  = arr.astype(np.float32)
                blurred[1:-1, 1:-1] = (
                    arr[:-2,:-2]+arr[1:-1,:-2]+arr[2:,:-2]+
                    arr[:-2,1:-1]+arr[1:-1,1:-1]+arr[2:,1:-1]+
                    arr[:-2,2:]+arr[1:-1,2:]+arr[2:,2:]
                ) / 9.0
                arr = np.clip(arr.astype(np.float32) +
                              strength * (arr.astype(np.float32) - blurred),
                              0, 255).astype(np.uint8)

            return pygame.surfarray.make_surface(arr)

        except Exception as e:
            print(f"[CRT FILTER] {e}")
            return surface


# ==============================================================================
# LibretroCore
# ==============================================================================

class LibretroCore:
    """
    Thin Python/ctypes wrapper around a libretro core .dll.
    All public methods must be called from the pygame main thread.
    """

    def __init__(self, dll_path, system_dir, save_dir):
        self.dll_path      = dll_path
        self.system_dir    = system_dir
        self.save_dir      = save_dir
        self.active_profile = 1   # 1, 2, or 3 — controls which profile subfolder saves land in
        # The directory actually handed to the core (via GET_SAVE_DIRECTORY)
        # and used for our own frontend-managed SRAM file -- always
        # save_dir/profile{N}, kept in sync by set_active_profile(). Starts
        # pointed at profile 1 so it's never left pointing at the flat,
        # non-profile-specific save_dir root even before the first explicit
        # set_active_profile() call.
        self._profile_save_dir = os.path.join(self.save_dir, "profile1")

        self._dll       = None
        self._rom_path  = ""
        self._loaded    = False
        self._frozen    = False
        self._pixel_fmt = RETRO_PIXEL_FORMAT_XRGB8888

        # Video
        self._vid_surface     = None
        self._raw_vid_surface = None  # unfiltered native frame, see _cb_video_refresh

        # AV
        self._fps         = 60.0
        self._sample_rate = 32768.0
        self._base_w      = 240
        self._base_h      = 160
        self._need_fullpath = False   # updated from retro_get_system_info() in load_dll()

        # Subsystems
        self._audio  = LibretroAudio()
        self._input  = LibretroInputState()
        self._crt    = CRTFilter()

        # Volume / mute
        self._volume = 100
        self._muted  = False

        # Encoded dir strings handed to the core — kept alive so the pointers
        # the core stores don't dangle after the environment callback returns.
        self._sys_dir_b  = b""
        self._save_dir_b = b""

        # Disk control interface — populated (if the core supports it) via
        # RETRO_ENVIRONMENT_SET_DISK_CONTROL_INTERFACE. None means the core has
        # no multi-disc / disc-swap support (most non-optical-media cores).
        self._disk_control  = None
        self._disc_data_ref = None   # keeps the swapped-in disc's bytes alive

        # Ctypes callback refs (must be kept alive — GC will kill them otherwise)
        self._cb_env   = None
        self._cb_vid   = None
        self._cb_aud   = None
        self._cb_audb  = None
        self._cb_inp   = None
        self._cb_inps  = None

        # Core variable overrides (libretro "core options"), e.g. mGBA's
        # "mgba_sgb_borders". Set via set_variable() before load_rom() so the
        # core's GET_VARIABLE environment calls return our value instead of
        # its built-in default.
        self._core_variables = {}   # str key -> bytes value
        self._var_dirty      = False

        # Boot-timing debug instrumentation. "First frame" and "first
        # non-black frame" are NOT the same thing — a booting PS1 core (or
        # any core) calls the video_refresh callback with genuine black
        # frames for as long as its boot sequence takes. The frontend's
        # splash overlay hides as soon as ANY frame exists, so a slow/black
        # boot shows up to the player as a plain black screen after the
        # splash disappears. These timestamps make that gap measurable.
        self._rom_load_ts        = None
        self._first_frame_ts     = None
        self._first_nonblack_ts  = None

    # ------------------------------------------------------------------
    # DLL loading & setup
    # ------------------------------------------------------------------

    def load_dll(self):
        """Load and initialise the core DLL. Returns True on success."""
        if not os.path.exists(self.dll_path):
            print(f"[LIBRETRO] DLL not found: {self.dll_path}")
            return False

        try:
            dll_dir = os.path.dirname(self.dll_path)
            if sys.platform == "win32" and dll_dir and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(dll_dir)
                except Exception as e:
                    log.warning("add_dll_directory(%s) failed (core DLL load may fail next if it needs bundled dependencies): %s", dll_dir, e)
            self._dll = ctypes.CDLL(self.dll_path)
            print(f"[LIBRETRO] Loaded: {os.path.basename(self.dll_path)}")
        except Exception as e:
            print(f"[LIBRETRO] DLL load failed: {e}")
            return False

        self._setup_prototypes()
        self._register_callbacks()

        try:
            self._dll.retro_init()
        except Exception as e:
            print(f"[LIBRETRO] retro_init() failed: {e}")
            return False

        # NOTE: do NOT call _read_av_info() here. It queries
        # retro_get_system_av_info(), which for driver-based cores (MAME
        # family) is only valid AFTER a ROM/driver is loaded -- calling it
        # this early reads through a not-yet-initialized internal pointer
        # (confirmed: access violation reading 0x300 on mame2003_plus).
        # We already have sane defaults (_fps/_base_w/_base_h set in
        # __init__) for this pre-load "Ready" print, and load_rom() already
        # calls _read_av_info() again for real once the ROM is actually in.
        self._need_fullpath = False
        try:
            info = retro_system_info()
            self._dll.retro_get_system_info(ctypes.byref(info))
            self._need_fullpath = bool(info.need_fullpath)
        except Exception as e:
            log.warning("retro_get_system_info failed (assuming need_fullpath=False): %s", e)
        print(f"[LIBRETRO] Ready  {self._base_w}×{self._base_h} @ {self._fps:.1f}fps  "
              f"audio {int(self._sample_rate)}Hz  need_fullpath={self._need_fullpath}")
        return True

    def _setup_prototypes(self):
        dll = self._dll
        dll.retro_set_environment.argtypes        = [RETRO_ENVIRONMENT_CB];     dll.retro_set_environment.restype        = None
        dll.retro_set_video_refresh.argtypes      = [RETRO_VIDEO_REFRESH_CB];   dll.retro_set_video_refresh.restype      = None
        dll.retro_set_audio_sample.argtypes       = [RETRO_AUDIO_SAMPLE_CB];    dll.retro_set_audio_sample.restype       = None
        dll.retro_set_audio_sample_batch.argtypes = [RETRO_AUDIO_SAMPLE_BATCH_CB]; dll.retro_set_audio_sample_batch.restype = None
        dll.retro_set_input_poll.argtypes         = [RETRO_INPUT_POLL_CB];      dll.retro_set_input_poll.restype         = None
        dll.retro_set_input_state.argtypes        = [RETRO_INPUT_STATE_CB];     dll.retro_set_input_state.restype        = None
        dll.retro_init.argtypes                   = [];    dll.retro_init.restype  = None
        dll.retro_deinit.argtypes                 = [];    dll.retro_deinit.restype = None
        dll.retro_get_system_av_info.argtypes     = [ctypes.POINTER(retro_system_av_info)]; dll.retro_get_system_av_info.restype = None
        dll.retro_get_system_info.argtypes        = [ctypes.POINTER(retro_system_info)]; dll.retro_get_system_info.restype = None
        dll.retro_run.argtypes                    = [];    dll.retro_run.restype   = None
        dll.retro_reset.argtypes                  = [];    dll.retro_reset.restype = None
        dll.retro_serialize_size.argtypes         = [];    dll.retro_serialize_size.restype = ctypes.c_size_t
        dll.retro_serialize.argtypes              = [ctypes.c_void_p, ctypes.c_size_t]; dll.retro_serialize.restype = ctypes.c_bool
        dll.retro_unserialize.argtypes            = [ctypes.c_void_p, ctypes.c_size_t]; dll.retro_unserialize.restype = ctypes.c_bool
        dll.retro_load_game.argtypes              = [ctypes.POINTER(retro_game_info)]; dll.retro_load_game.restype = ctypes.c_bool
        dll.retro_unload_game.argtypes            = [];    dll.retro_unload_game.restype = None
        dll.retro_set_controller_port_device.argtypes = [ctypes.c_uint, ctypes.c_uint]; dll.retro_set_controller_port_device.restype = None
        # Frontend-managed SRAM save persistence (see RETRO_MEMORY_SAVE_RAM
        # above) -- retro_get_memory_data returns a raw pointer directly into
        # the core's own live SRAM buffer (NOT a copy), valid only while the
        # core stays loaded, so it must be read/written promptly and never
        # cached across calls.
        dll.retro_get_memory_data.argtypes        = [ctypes.c_uint]; dll.retro_get_memory_data.restype = ctypes.c_void_p
        dll.retro_get_memory_size.argtypes        = [ctypes.c_uint]; dll.retro_get_memory_size.restype = ctypes.c_size_t

    def _register_callbacks(self):
        self._cb_env  = RETRO_ENVIRONMENT_CB(self._cb_environment)
        self._cb_vid  = RETRO_VIDEO_REFRESH_CB(self._cb_video_refresh)
        self._cb_aud  = RETRO_AUDIO_SAMPLE_CB(self._cb_audio_sample)
        self._cb_audb = RETRO_AUDIO_SAMPLE_BATCH_CB(self._cb_audio_batch)
        self._cb_inp  = RETRO_INPUT_POLL_CB(self._cb_input_poll)
        self._cb_inps = RETRO_INPUT_STATE_CB(self._cb_input_state)
        self._dll.retro_set_environment(self._cb_env)
        self._dll.retro_set_video_refresh(self._cb_vid)
        self._dll.retro_set_audio_sample(self._cb_aud)
        self._dll.retro_set_audio_sample_batch(self._cb_audb)
        self._dll.retro_set_input_poll(self._cb_inp)
        self._dll.retro_set_input_state(self._cb_inps)

    def _read_av_info(self):
        try:
            av = retro_system_av_info()
            self._dll.retro_get_system_av_info(ctypes.byref(av))
            if av.timing.fps > 0:          self._fps         = av.timing.fps
            if av.timing.sample_rate > 0:  self._sample_rate = av.timing.sample_rate
            if av.geometry.base_width:     self._base_w      = av.geometry.base_width
            if av.geometry.base_height:    self._base_h      = av.geometry.base_height
        except Exception as e:
            log.warning("_read_av_info: retro_get_system_av_info failed, keeping previous fps/sample_rate/geometry values: %s", e)

    # ------------------------------------------------------------------
    # Callbacks (called synchronously from inside retro_run)
    # ------------------------------------------------------------------

    def _cb_environment(self, cmd, data):
        # TEMP DIAGNOSTIC: log every environment cmd as it arrives, and flush
        # immediately. If the native call segfaults partway through, this is
        # the only way to see which cmd was in flight when it happened --
        # the exception message alone doesn't tell us that.
        # Gated behind _ENV_CB_DEBUG (see module-level comment above) -- this
        # fires on every core frame and was previously always-on, which is
        # what filled app_log.txt so fast during any gameplay session.
        if _ENV_CB_DEBUG:
            try:
                print(f"[LIBRETRO ENV] >> cmd={cmd}", flush=True)
            except Exception:
                pass
        try:
            if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
                fmt = ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value
                self._pixel_fmt = fmt
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
                os.makedirs(self.system_dir, exist_ok=True)
                # Keep the encoded bytes alive — the core stores this pointer and
                # may read it long after this callback returns. A temporary
                # .encode() would be freed immediately, leaving a dangling pointer.
                self._sys_dir_b = self.system_dir.encode("utf-8")
                ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = self._sys_dir_b
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY:
                # Profile-specific (see _profile_save_dir / set_active_profile) --
                # NOT the flat save_dir root -- so a core that manages its own
                # save file using this hint writes it into the active profile's
                # folder instead of every profile's save colliding into one
                # shared file.
                os.makedirs(self._profile_save_dir, exist_ok=True)
                self._save_dir_b = self._profile_save_dir.encode("utf-8")
                ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = self._save_dir_b
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_CAN_DUPE:
                ctypes.cast(data, ctypes.POINTER(ctypes.c_bool))[0] = True
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_VARIABLE:
                var_ptr = ctypes.cast(data, ctypes.POINTER(retro_variable))
                key_b = var_ptr[0].key
                key   = key_b.decode("utf-8", "ignore") if key_b else None
                if not getattr(self, "_logged_var_reads", None):
                    self._logged_var_reads = set()
                if key is not None and key not in self._logged_var_reads:
                    self._logged_var_reads.add(key)
                    have = key in self._core_variables
                    val  = self._core_variables[key].decode("utf-8", "ignore") if have else "(no override — core uses its own default)"
                    print(f"[LIBRETRO VAR] Core requested '{key}' -> {val}")
                if key is not None and key in self._core_variables:
                    # Value bytes live in self._core_variables, so the pointer
                    # stays valid after this callback returns.
                    var_ptr[0].value = self._core_variables[key]
                    return True
                var_ptr[0].value = None
                return False
            elif cmd == RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE:
                ctypes.cast(data, ctypes.POINTER(ctypes.c_bool))[0] = self._var_dirty
                self._var_dirty = False
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_LANGUAGE:
                ctypes.cast(data, ctypes.POINTER(ctypes.c_uint))[0] = RETRO_LANGUAGE_ENGLISH
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION:
                # Must write the version when returning True. Advertise legacy (0)
                # so the core falls back to SET_VARIABLES / GET_VARIABLE defaults.
                ctypes.cast(data, ctypes.POINTER(ctypes.c_uint))[0] = 0
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_DISK_CONTROL_INTERFACE:
                # Core is handing US a struct of ITS OWN function pointers (not
                # asking us to fill one in) — read it and keep it alive so we
                # can call set_eject_state/replace_image_index/etc. later when
                # the player picks "Change Disc" from the PSX menu.
                try:
                    dc_struct = ctypes.cast(
                        data, ctypes.POINTER(retro_disk_control_callback)).contents
                    self._disk_control = dc_struct
                    print("[LIBRETRO] Disk control interface registered — live disc swap available")
                except Exception as e:
                    print(f"[LIBRETRO] Failed to read disk control interface: {e}")
                    self._disk_control = None
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO:
                # Unlike the SET_* commands below, this one hands us a real
                # retro_system_av_info struct with a live timing/geometry
                # update — must actually read it, not just acknowledge.
                try:
                    av = ctypes.cast(data, ctypes.POINTER(retro_system_av_info)).contents
                    if av.timing.fps > 0:
                        self._fps = av.timing.fps
                    if av.timing.sample_rate > 0:
                        self._sample_rate = av.timing.sample_rate
                        self._audio.set_sample_rate(av.timing.sample_rate)
                    if av.geometry.base_width:
                        self._base_w = av.geometry.base_width
                    if av.geometry.base_height:
                        self._base_h = av.geometry.base_height
                except Exception as e:
                    print(f"[LIBRETRO ENV] SET_SYSTEM_AV_INFO read failed: {e}")
                return True
            elif cmd in (RETRO_ENVIRONMENT_SET_VARIABLES, RETRO_ENVIRONMENT_SET_CORE_OPTIONS,
                         RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2,
                         RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME, RETRO_ENVIRONMENT_SET_CONTROLLER_INFO,
                         RETRO_ENVIRONMENT_SET_GEOMETRY, RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL):
                # SET_* informational commands: the core hands us data, we just
                # acknowledge. These never require us to write back through `data`.
                return True
            # NOTE: every other command (including GET_RUMBLE_INTERFACE,
            # GET_USERNAME, GET_AUDIO_VIDEO_ENABLE, GET_LIBRETRO_PATH and any
            # interface query) MUST return False. Returning True without actually
            # filling the struct makes the core believe it received valid function
            # pointers / strings and then dereference garbage inside retro_run()
            # — the cause of the per-frame "access violation writing" crash.
        except Exception as e:
            print(f"[LIBRETRO ENV] cmd={cmd}: {e}")
        return False

    def _cb_video_refresh(self, data, width, height, pitch):
        if data is None:
            return   # duplicate frame — keep last surface
        _perf_t0 = time.perf_counter()
        try:
            if self._pixel_fmt == RETRO_PIXEL_FORMAT_XRGB8888:
                total = height * pitch
                raw   = (ctypes.c_uint8 * total).from_address(
                    ctypes.cast(data, ctypes.c_void_p).value)
                arr   = np.frombuffer(raw, dtype=np.uint8).reshape((height, pitch))
                r = arr[:height, 2:width*4:4].copy()
                g = arr[:height, 1:width*4:4].copy()
                b = arr[:height, 0:width*4:4].copy()
                rgb = np.stack([r, g, b], axis=2)

            elif self._pixel_fmt == RETRO_PIXEL_FORMAT_RGB565:
                total  = height * pitch
                raw    = (ctypes.c_uint8 * total).from_address(
                    ctypes.cast(data, ctypes.c_void_p).value)
                shorts = np.frombuffer(raw, dtype=np.uint16).reshape((height, pitch//2))[:height,:width]
                r = (((shorts >> 11) & 0x1F) * 255 // 31).astype(np.uint8)
                g = (((shorts >> 5)  & 0x3F) * 255 // 63).astype(np.uint8)
                b = ((shorts         & 0x1F) * 255 // 31).astype(np.uint8)
                rgb = np.stack([r, g, b], axis=2)

            elif self._pixel_fmt == RETRO_PIXEL_FORMAT_0RGB1555:
                total  = height * pitch
                raw    = (ctypes.c_uint8 * total).from_address(
                    ctypes.cast(data, ctypes.c_void_p).value)
                shorts = np.frombuffer(raw, dtype=np.uint16).reshape((height, pitch//2))[:height,:width]
                r = (((shorts >> 10) & 0x1F) * 255 // 31).astype(np.uint8)
                g = (((shorts >> 5)  & 0x1F) * 255 // 31).astype(np.uint8)
                b = ((shorts         & 0x1F) * 255 // 31).astype(np.uint8)
                rgb = np.stack([r, g, b], axis=2)
            else:
                return

            if self._rom_load_ts is not None:
                if self._first_frame_ts is None:
                    self._first_frame_ts = time.time()
                    ms = int((self._first_frame_ts - self._rom_load_ts) * 1000)
                    print(f"[LIBRETRO BOOT] First frame from core {ms}ms after ROM load "
                          f"(still may be black — this is just 'core produced pixels')")
                if self._first_nonblack_ts is None:
                    # Cheap heuristic: real content will have SOME pixel above
                    # near-zero brightness. Boot-black frames are exactly what
                    # they sound like — every channel at or near 0.
                    if int(rgb.max()) > 8:
                        self._first_nonblack_ts = time.time()
                        ms = int((self._first_nonblack_ts - self._rom_load_ts) * 1000)
                        print(f"[LIBRETRO BOOT] First NON-BLACK frame {ms}ms after ROM load "
                              f"— this is the real black-screen duration the player experiences")

            self._base_w = width
            self._base_h = height
            # pygame surfarray wants (width, height, 3) — transpose rows↔cols
            # _raw_vid_surface holds the unfiltered native frame straight from the
            # core. CRT effects are applied to a COPY of this (see run_frame /
            # reprocess_current_frame), never to this surface in place — otherwise
            # repeatedly re-applying the filter (e.g. while adjusting sliders) would
            # compound on top of an already-filtered image instead of starting clean
            # from the source each time.
            self._raw_vid_surface = pygame.surfarray.make_surface(
                np.ascontiguousarray(rgb.transpose(1, 0, 2)))
            self._vid_surface = self._raw_vid_surface
            FRAME_PERF.record("video_cb", (time.perf_counter() - _perf_t0) * 1000.0)

        except Exception as e:
            print(f"[LIBRETRO VIDEO] {e}")

    def _cb_audio_sample(self, left, right):
        # Single stereo pair from the core (some cores, e.g. Beetle PSX /
        # PCSX ReARMed, fall back to this path for certain audio such as
        # CD-DA/redbook playback). Must feed the SAME deque-based pipeline
        # the batch path (_cb_audio_batch/push_samples) uses -- LibretroAudio
        # has no "_buf" attribute (that was the bug: every call here raised
        # AttributeError inside the ctypes callback trampoline, silently
        # dropping this audio path entirely, which is why PSX -- one of the
        # few consoles whose cores actually use this callback -- sounded
        # choppy while batch-only consoles like SNES/Genesis didn't).
        pair = np.array([[left, right]], dtype=np.int16)
        with self._audio._buf_lock:
            self._audio._in_blocks.append(pair)
            self._audio._in_count += 1
            while self._audio._in_count > self._audio.MAX_OUT * 3 and len(self._audio._in_blocks) > 1:
                dropped = self._audio._in_blocks.popleft()
                self._audio._in_count -= len(dropped)

    def _cb_audio_batch(self, data, frames):
        return self._audio.push_samples(data, frames)

    def _cb_input_poll(self):
        pass   # state is pre-populated by game_deck._poll_libretro_input()

    def _cb_input_state(self, port, device, index, btn_id):
        if port != 0:
            return 0
        if device == RETRO_DEVICE_JOYPAD:
            return self._input.get_button(btn_id)
        if device == RETRO_DEVICE_ANALOG:
            return self._input.get_analog(index, btn_id)
        return 0

    # ------------------------------------------------------------------
    # ROM management
    # ------------------------------------------------------------------

    def load_rom(self, rom_path):
        """Load a ROM file. Returns True on success."""
        self._rom_load_ts       = time.time()
        self._first_frame_ts    = None
        self._first_nonblack_ts = None
        if not os.path.exists(rom_path):
            print(f"[LIBRETRO] ROM not found: {rom_path}")
            return False
        if self._dll is None:
            print("[LIBRETRO] Call load_dll() first")
            return False

        if self._need_fullpath:
            # need_fullpath cores (MAME2003-Plus, MAME2016, FBNeo, etc.) open
            # the file themselves — often specifically the zip's own internal
            # CRC/driver-matching logic for arcade sets — and reject in-memory
            # data outright. Must hand back data=NULL/size=0 with just the path.
            #
            # Unlike every other core type here, MAME-family cores parse the
            # driver/game name directly out of this path string themselves
            # (old xmame-derived code) rather than just fopen()-ing it. The
            # libretro convention is that content paths use '/' regardless of
            # platform; Windows' own CRT accepts '/' fine, so normalizing
            # here is safe for every core and specifically avoids a
            # backslash tripping up MAME's own path parsing.
            rom_path_for_core = rom_path.replace("\\", "/")
            print(f"[LIBRETRO] need_fullpath path being sent to core: {rom_path_for_core!r}")
            rom_data = None
            c_data = None
            gi = retro_game_info()
            gi.path = rom_path_for_core.encode()
            gi.data = None
            gi.size = 0
            gi.meta = None
        else:
            try:
                with open(rom_path, "rb") as f:
                    rom_data = f.read()
            except Exception as e:
                print(f"[LIBRETRO] Cannot read ROM: {e}")
                return False

            c_data = (ctypes.c_uint8 * len(rom_data)).from_buffer_copy(rom_data)
            gi     = retro_game_info()
            gi.path = rom_path.encode()
            gi.data = ctypes.cast(c_data, ctypes.c_void_p)
            gi.size = len(rom_data)
            gi.meta = None

        try:
            ok = self._dll.retro_load_game(ctypes.byref(gi))
        except Exception as e:
            print(f"[LIBRETRO] retro_load_game() failed: {e}")
            return False

        if not ok:
            print("[LIBRETRO] Core rejected the ROM")
            return False

        self._rom_path    = rom_path
        self._rom_data_ref = c_data   # keep alive so the pointer doesn't dangle (None for need_fullpath cores)

        # Re-read AV info now that ROM is loaded (more accurate)
        self._read_av_info()
        self._audio = LibretroAudio(int(self._sample_rate))
        self._audio.set_volume(self._volume)
        self._audio.set_muted(self._muted)

        try:
            self._dll.retro_set_controller_port_device(0, RETRO_DEVICE_JOYPAD)
        except Exception as e:
            log.warning("retro_set_controller_port_device failed (core may not recognize joypad input for this session): %s", e)

        os.makedirs(self.save_dir, exist_ok=True)
        self._loaded = True
        self._frozen = False

        # SRAM (see save_sram/load_sram/_migrate_stray_save_file): pull in an
        # old stray save left next to the ROM (if this profile has no save of
        # its own yet), then load whatever this profile's own save file has,
        # into the core's freshly-allocated SRAM buffer. Must happen AFTER
        # _loaded is set (both methods check it) and after the core has had
        # a chance to allocate its memory buffers via retro_load_game above.
        self._migrate_stray_save_file(rom_path)
        self.load_sram()

        print(f"[LIBRETRO] ROM loaded: {os.path.basename(rom_path)}  "
              f"{self._base_w}×{self._base_h} @ {self._fps:.1f}fps")
        return True

    def has_disk_control(self):
        """True if the loaded core exposes a disk control interface, i.e. it
        supports swapping discs live (PSX cores with multi-disc games)."""
        return self._disk_control is not None

    def change_disc(self, new_disc_path):
        """
        Swap the inserted disc image while the core keeps running — no
        unload/reload, so the game's RAM state and save data are untouched.
        Requires the core to have registered a disk control interface (most
        PSX cores do). Returns True on success.
        """
        if self._disk_control is None:
            print("[LIBRETRO] Core has no disk control interface — cannot swap discs live")
            return False
        if not os.path.exists(new_disc_path):
            print(f"[LIBRETRO] Disc image not found: {new_disc_path}")
            return False

        try:
            with open(new_disc_path, "rb") as f:
                disc_data = f.read()
        except Exception as e:
            print(f"[LIBRETRO] Cannot read disc image: {e}")
            return False

        c_data = (ctypes.c_uint8 * len(disc_data)).from_buffer_copy(disc_data)
        gi     = retro_game_info()
        gi.path = new_disc_path.encode()
        gi.data = ctypes.cast(c_data, ctypes.c_void_p)
        gi.size = len(disc_data)
        gi.meta = None
        # Keep the buffer alive — the core may read from this pointer any
        # time after replace_image_index() returns, same reasoning as
        # self._rom_data_ref for the initial load.
        self._disc_data_ref = c_data

        dc = self._disk_control
        try:
            dc.set_eject_state(True)
            # The initial disc loaded via retro_load_game() is always image
            # index 0 in single-disc-at-a-time frontends like this one (no
            # M3U playlist), so we always replace that same slot.
            ok = bool(dc.replace_image_index(0, ctypes.byref(gi)))
            dc.set_eject_state(False)
        except Exception as e:
            print(f"[LIBRETRO] Disc swap failed: {e}")
            return False

        if ok:
            self._rom_path = new_disc_path
            print(f"[LIBRETRO] Disc changed live: {os.path.basename(new_disc_path)}")
        else:
            print("[LIBRETRO] Core rejected the replacement disc image")
        return ok

    def unload(self):
        if not self._loaded:
            return
        self.save_sram()  # persist battery/SRAM save data before the core (and its memory buffer) goes away
        try:
            self._dll.retro_unload_game()
            self._dll.retro_deinit()
        except Exception as e:
            print(f"[LIBRETRO] Unload error: {e}")
        self._audio.stop()
        self._loaded = False
        self._dll    = None
        print("[LIBRETRO] Core unloaded.")

    def unload_game(self):
        """Unload the current game but keep the DLL/core initialized (i.e.
        skip retro_deinit()) so a new ROM can be loaded right after — use
        this when relaunching on the SAME console (restarting a game,
        picking a different ROM). retro_load_game() must never be called a
        second time on top of an already-loaded game without unloading
        first; most cores (mGBA included) don't support that and crash
        hard at the native level with no Python traceback. Use the heavier
        unload() instead when tearing the whole core down for a console
        switch."""
        if not self._loaded or self._dll is None:
            return
        self.save_sram()  # persist battery/SRAM save data before the game (and its memory buffer) goes away
        try:
            self._dll.retro_unload_game()
        except Exception as e:
            print(f"[LIBRETRO] unload_game error: {e}")
        self._audio.stop()
        self._loaded = False
        self._frozen = False
        # A new ROM about to be loaded may or may not re-register a disk
        # control interface — don't let a stale one from the previous game
        # make change_disc() look available when it no longer is.
        self._disk_control = None
        print("[LIBRETRO] Game unloaded (core still initialized).")

    # ------------------------------------------------------------------
    # Per-frame execution
    # ------------------------------------------------------------------

    def run_frame(self, crt_cfg=None):
        """
        Advance emulation by one frame.  game_deck must have already called
        _poll_libretro_input() → core._input.set_buttons() before this.
        Returns a pygame.Surface (the rendered frame), or None.
        """
        if not self._loaded or self._frozen or self._dll is None:
            return self._vid_surface

        _frame_t0 = time.perf_counter()
        try:
            with _perf_timer("retro_run"):
                self._dll.retro_run()
        except Exception as e:
            print(f"[LIBRETRO] retro_run(): {e}")
            return self._vid_surface

        with _perf_timer("audio_tick"):
            self._audio.tick()

        # Apply CRT filter at native resolution (cheap — GBA is 240×160).
        # Filter is applied to a fresh copy of the unfiltered _raw_vid_surface each
        # frame, never in place — see reprocess_current_frame() for why.
        if self._raw_vid_surface and crt_cfg:
            if any(v != 50 for v in crt_cfg.values()):
                with _perf_timer("crt_filter"):
                    self._vid_surface = self._crt.apply(
                        self._raw_vid_surface,
                        brightness=crt_cfg.get("brightness", 50),
                        contrast=crt_cfg.get("contrast",   50),
                        saturation=crt_cfg.get("saturation", 50),
                        sharpness=crt_cfg.get("sharpness",  50),
                        hue_shift=crt_cfg.get("hue_shift",  50),
                    )
            else:
                self._vid_surface = self._raw_vid_surface

        FRAME_PERF.record("frame_total", (time.perf_counter() - _frame_t0) * 1000.0)
        FRAME_PERF.maybe_report()
        return self._vid_surface

    def reprocess_current_frame(self, crt_cfg=None):
        """Re-apply CRT effects to the CURRENT (already-rendered) frame without
        advancing emulation. Used while frozen (e.g. menu open) so brightness/
        contrast/hue/color/sharpness sliders update the on-screen frame in real
        time, instead of needing to unfreeze to see the change.
        Always starts from _raw_vid_surface so repeated calls (one per slider
        tick) don't compound the filter on top of itself.
        """
        if self._raw_vid_surface is None:
            return self._vid_surface
        if crt_cfg and any(v != 50 for v in crt_cfg.values()):
            self._vid_surface = self._crt.apply(
                self._raw_vid_surface,
                brightness=crt_cfg.get("brightness", 50),
                contrast=crt_cfg.get("contrast",   50),
                saturation=crt_cfg.get("saturation", 50),
                sharpness=crt_cfg.get("sharpness",  50),
                hue_shift=crt_cfg.get("hue_shift",  50),
            )
        else:
            self._vid_surface = self._raw_vid_surface
        return self._vid_surface

    def get_surface(self):
        return self._vid_surface

    # ------------------------------------------------------------------
    # Freeze / resume
    # ------------------------------------------------------------------

    def freeze(self):
        if self._loaded and not self._frozen:
            self._frozen = True
            self._input.clear()
            self._audio.set_muted(True)
            print("[LIBRETRO] Frozen.")

    def unfreeze(self, muted=False):
        # NOTE: the mute application used to be gated behind `self._frozen`,
        # so a caller asking to force-mute on resume (change_channel()'s ch03
        # entry, to keep audio silent under the transition shield) was
        # silently ignored whenever the core hadn't actually been frozen this
        # session (e.g. the very first entry into ch03 after a game just
        # loaded). That let real, unmuted audio through immediately instead
        # of staying silent until the shield expired. Mute is applied
        # unconditionally now; only the frozen/input-clear bookkeeping stays
        # gated on actually having been frozen.
        if self._loaded and self._frozen:
            self._frozen = False
            print("[LIBRETRO] Unfrozen.")
        if self._loaded:
            self._audio.set_muted(muted or self._muted)

    @property
    def is_frozen(self):  return self._frozen
    @property
    def is_loaded(self):  return self._loaded
    @property
    def has_visible_frame(self):
        """True once the core has produced a real (non-black) frame — as
        opposed to get_surface() being non-None, which is also true for
        boot-black frames and was previously mistaken for "ready"."""
        return self._first_nonblack_ts is not None
    @property
    def fps(self):         return self._fps

    # ------------------------------------------------------------------
    # Save / load states
    # ------------------------------------------------------------------

    def set_active_profile(self, profile_num):
        """Switch the active save profile (1, 2, or 3). Saves and loads will use
        saves/profile{N}/ so each profile keeps its own independent save files."""
        self.active_profile = int(profile_num)
        self._profile_save_dir = os.path.join(self.save_dir, f"profile{self.active_profile}")
        os.makedirs(self._profile_save_dir, exist_ok=True)
        print(f"[LIBRETRO] Active save profile set to {self.active_profile}")

    def _state_path(self, slot):
        name = os.path.splitext(os.path.basename(self._rom_path))[0]
        os.makedirs(self._profile_save_dir, exist_ok=True)
        return os.path.join(self._profile_save_dir, f"{name}_slot{slot}.state")

    def _sram_path(self):
        """Path for OUR OWN frontend-managed SRAM/battery-save file (see
        save_sram/load_sram) -- always the active profile's folder. Uses a
        .srm extension (RetroArch's own convention for a frontend-persisted
        save) rather than .sav, specifically so it can never collide with a
        core that ALSO writes its own .sav directly into this same folder
        via GET_SAVE_DIRECTORY -- both can coexist safely."""
        name = os.path.splitext(os.path.basename(self._rom_path))[0]
        os.makedirs(self._profile_save_dir, exist_ok=True)
        return os.path.join(self._profile_save_dir, f"{name}.srm")

    def save_sram(self):
        """Persist the core's cartridge battery/SRAM save data ourselves,
        via retro_get_memory_data/size (RETRO_MEMORY_SAVE_RAM) -- the
        standard frontend-managed path every compliant libretro core
        supports, independent of whether the core's own GET_SAVE_DIRECTORY-
        based self-save (if it even does one) behaves correctly. This is
        what actually guarantees a save never ends up sitting next to the
        ROM file instead of in the active profile's folder: we write it
        ourselves, to a path WE control, rather than trusting each
        individual core's own directory handling.

        Returns True if there was save data and it was written successfully
        (False is also the normal, harmless result for games with no
        battery-backed save memory at all -- e.g. NES titles that don't use
        SRAM -- so a False return here is not necessarily an error).
        """
        if not self._loaded:
            return False
        try:
            size = self._dll.retro_get_memory_size(RETRO_MEMORY_SAVE_RAM)
            if not size:
                return False
            ptr = self._dll.retro_get_memory_data(RETRO_MEMORY_SAVE_RAM)
            if not ptr:
                return False
            buf = (ctypes.c_uint8 * size).from_address(ptr)
            data = bytes(buf)  # copy out before the pointer can move/invalidate
            path = self._sram_path()
            with open(path, "wb") as f:
                f.write(data)
            print(f"[LIBRETRO] SRAM saved → {os.path.basename(path)} ({size} bytes)")
            return True
        except Exception as e:
            print(f"[LIBRETRO] save_sram: {e}")
            return False

    def load_sram(self):
        """Counterpart to save_sram() -- copies a previously-saved SRAM file
        back into the core's live memory. Must be called AFTER load_rom()
        succeeds (the core has to have allocated its SRAM buffer first).
        Missing/undersized files are handled safely: nothing is written if
        there's no save yet, and the copy is clamped to whichever of
        (file size, core's reported buffer size) is smaller so a save from a
        different-region ROM version can never overrun the core's buffer.
        """
        if not self._loaded:
            return False
        path = self._sram_path()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                return False
            size = self._dll.retro_get_memory_size(RETRO_MEMORY_SAVE_RAM)
            if not size:
                return False
            ptr = self._dll.retro_get_memory_data(RETRO_MEMORY_SAVE_RAM)
            if not ptr:
                return False
            n = min(len(data), size)
            ctypes.memmove(ptr, data, n)
            print(f"[LIBRETRO] SRAM loaded ← {os.path.basename(path)} ({n} bytes)")
            return True
        except Exception as e:
            print(f"[LIBRETRO] load_sram: {e}")
            return False

    def _migrate_stray_save_file(self, rom_path):
        """One-time cleanup: if an OLDER session left a .sav/.srm sitting
        next to the ROM itself (a core that ignored/mishandled
        GET_SAVE_DIRECTORY, or a file from before this profile-folder system
        existed), pull it into the active profile's folder as that profile's
        starting save instead of leaving it stranded in the ROM folder
        forever. Only runs when this profile doesn't already have its own
        save on file, so it can never clobber real progress already made
        under the new system. The stray original is renamed (not deleted)
        with a ".migrated" suffix so it's obviously inert but still there if
        something ever needs to be double-checked by hand.
        """
        try:
            rom_dir  = os.path.dirname(rom_path)
            rom_name = os.path.splitext(os.path.basename(rom_path))[0]
            dest = self._sram_path()
            if os.path.exists(dest):
                return  # this profile already has its own save -- never overwrite it
            for ext in (".sav", ".srm"):
                stray = os.path.join(rom_dir, rom_name + ext)
                if os.path.exists(stray):
                    import shutil
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copyfile(stray, dest)
                    try:
                        os.rename(stray, stray + ".migrated")
                    except Exception as rename_err:
                        log.warning("Stray save migrated but couldn't rename original %s: %s", stray, rename_err)
                    print(f"[LIBRETRO] Migrated stray save {os.path.basename(stray)} "
                          f"→ profile{self.active_profile}/{os.path.basename(dest)}")
                    return
        except Exception as e:
            log.warning("_migrate_stray_save_file failed for %s: %s", rom_path, e)

    def save_state(self, slot=0):
        if not self._loaded: return False
        try:
            sz = self._dll.retro_serialize_size()
            if sz == 0: return False
            buf = (ctypes.c_uint8 * sz)()
            ok  = self._dll.retro_serialize(ctypes.cast(buf, ctypes.c_void_p), sz)
            if not ok: return False
            path = self._state_path(slot)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f: f.write(bytes(buf))
            print(f"[LIBRETRO] State saved → {os.path.basename(path)}")
            return True
        except Exception as e:
            print(f"[LIBRETRO] save_state: {e}")
            return False

    def load_state(self, slot=0):
        if not self._loaded: return False
        path = self._state_path(slot)
        if not os.path.exists(path): return False
        try:
            with open(path, "rb") as f: data = f.read()
            buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            ok  = self._dll.retro_unserialize(ctypes.cast(buf, ctypes.c_void_p), len(data))
            print(f"[LIBRETRO] State {'loaded' if ok else 'FAILED'} ← {os.path.basename(path)}")
            return ok
        except Exception as e:
            print(f"[LIBRETRO] load_state: {e}")
            return False

    def has_state(self, slot=0):
        return self._loaded and os.path.exists(self._state_path(slot))

    def list_states(self, max_slots=5):
        return [(s, self._state_path(s), self.has_state(s)) for s in range(max_slots)]

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    def set_volume(self, volume_pct):
        self._volume = volume_pct
        self._audio.set_volume(volume_pct)

    def set_muted(self, muted):
        self._muted = bool(muted)
        if not self._frozen:
            self._audio.set_muted(self._muted)

    # ------------------------------------------------------------------
    # Core variable overrides
    # ------------------------------------------------------------------

    def set_variable(self, key, value):
        """Set a libretro core variable BEFORE load_rom()/retro_load_game()
        so the core picks it up on its own initial GET_VARIABLE read while
        loading. Does NOT flag GET_VARIABLE_UPDATE — the value is brand new
        to the core (nothing has actually "changed" from its point of view
        yet), so there's nothing to notify it about. Flagging dirty here
        used to make some cores (mGBA included) try to live-reconfigure
        their video buffer for a geometry-affecting option like the SGB
        border on literally the very first frame, before they'd even
        finished their normal load-time setup for it — which crashed the
        core at the native level. Use update_variable_live() instead for
        genuine runtime toggles on an already-running game."""
        self._core_variables[key] = str(value).encode("utf-8")

    def update_variable_live(self, key, value):
        """Push a live change to a core variable WHILE the game is already
        running (e.g. toggling the GB SGB border mid-session from the
        profile menu). Flags GET_VARIABLE_UPDATE so the core re-reads the
        variable on its next retro_run() and reconfigures itself — for
        geometry-affecting options this includes calling back through
        RETRO_ENVIRONMENT_SET_GEOMETRY and then delivering differently
        sized frames to _cb_video_refresh, both of which are already
        handled generically (frame size is read fresh from the
        video-refresh callback every frame, so any new resolution is
        picked up and scaled to fit automatically)."""
        self._core_variables[key] = str(value).encode("utf-8")
        self._var_dirty = True


# ==============================================================================
# Factory helper
# ==============================================================================

def build_core_for_console(console, base_dir):
    """
    Find the libretro .dll for *console* under base_dir/main/cores/<CONSOLE>/
    and return an initialised (but ROM-not-yet-loaded) LibretroCore, or None.
    """
    emu_dir    = os.path.join(base_dir, "main", "cores", console)
    save_dir   = os.path.join(base_dir, "main", "roms", console, "saves")
    if console == "PSX":
        # PSX cores (Beetle PSX, PCSX ReARMed, etc.) need real console BIOS
        # dumps (scph5500.bin / scph5501.bin / scph5502.bin, etc.) rather than
        # a per-core "system" scratch folder. Point RETRO_ENVIRONMENT_GET_
        # SYSTEM_DIRECTORY at a shared main/bios folder so the user only has
        # to drop BIOS files in one well-known place.
        system_dir = os.path.join(base_dir, "main", "bios")
    else:
        system_dir = os.path.join(emu_dir, "system")

    dll_path = None
    if os.path.isdir(emu_dir):
        for fname in os.listdir(emu_dir):
            if fname.lower().endswith("_libretro.dll") or fname.lower().endswith("libretro.dll"):
                dll_path = os.path.join(emu_dir, fname)
                break
        if dll_path is None:
            for fname in os.listdir(emu_dir):
                if fname.lower().endswith(".dll"):
                    dll_path = os.path.join(emu_dir, fname)
                    break

    if dll_path is None:
        print(f"[LIBRETRO] No core .dll found in {emu_dir}")
        return None

    core = LibretroCore(dll_path, system_dir, save_dir)

    if console == "PSX":
        # Real-BIOS boot on pcsx_rearmed runs the actual PS1 startup ROM,
        # including per-disc anti-piracy/protection sector checks — slow at
        # the best of times, and brutal when combined with a degraded/non-JIT
        # memory map (see "Memory map is sub-par" in the log), stretching
        # what should be a couple seconds into a long black screen before the
        # game appears. HLE (high-level emulation) BIOS skips the real boot
        # ROM entirely and simulates just enough of it to jump straight to
        # the game, which is dramatically faster to reach gameplay.
        #
        # Set BEFORE load_dll() (not just before load_rom()) — cores commonly
        # read their core-option variables during retro_init(), not only
        # while a ROM is loading, and a variable set after init has already
        # queried it arrives too late to matter. set_variable() only touches
        # a plain Python dict, so it's safe to call before the DLL/core is
        # even initialized.
        core.set_variable("pcsx_rearmed_bios", "HLE")

    if not core.load_dll():
        return None

    return core