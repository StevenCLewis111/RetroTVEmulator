"""ctypes host for libretro emulator cores (.dll) used by the Game Deck.

Owns: the libretro constants and C structures, per-port input state, the audio
queue, the CRT and pixel-art filters, LibretroCore (load, run, save/state,
disc change) and build_core_for_console().
Does not own: the game UI, button settings, or which core a console uses
(game_deck.py).

WARNING: anything a core holds a raw pointer to must stay referenced for as
long as the core can use it: the ctypes callback objects, the ROM and disc
buffers (_rom_data_ref, _disc_data_ref) and the playing/queued Sound objects.
If Python garbage-collects them the core reads freed memory.
"""
import os
import sys
import math
import ctypes
import ctypes.wintypes
import threading
import collections
import time
import logging

import pygame
import numpy as np

log = logging.getLogger(__name__)


# ============================================================================
# SECTION: Swallowed-failure lines
# ============================================================================
try:
    from performance_governor import log_swallowed as _log_swallowed
except Exception:
    def _log_swallowed(where, error):
        """Fallback used only when performance_governor cannot be imported: the failure line is lost, nothing else changes."""
        return None

try:
    import performance_governor as _timing_governor
except Exception:
    _timing_governor = None


# ============================================================================
# SECTION: Game frame timing (log-only)
# ============================================================================


class GameFrameTimer:
    """Collects per-stage milliseconds of the libretro game frame and logs one summary line per window; log-only, never changes drawing."""

    _TOP_LEVEL = ("input", "core_run", "audio_tick", "color", "pixel", "blit", "osd")

    def __init__(self):
        self._scratch = {}
        self._scratch_counts = {}
        self._t0 = None
        self._console = ""
        self._session_t0 = time.monotonic()
        self._phase = None
        self._prev_t0 = None
        self._prev_phase = None
        self._pad_src = None
        self._pad_base = None
        self._reset_window()

    def set_pad_stats_source(self, fn):
        """Registers the callable that returns the background controller reader's running totals."""
        self._pad_src = fn

    def _read_pad_src(self):
        try:
            return self._pad_src() if self._pad_src is not None else None
        except Exception as _swallowed_error:
            _log_swallowed("_read_pad_src: return self._pad_src() if self._pad_src is not None else Non", _swallowed_error)
            return None

    def _reset_window(self):
        self._win = {"ticks": 0, "total": 0.0, "worst": 0.0, "runs": 0, "max_runs": 0, "catchup": 0,
                     "cap": 0, "interval": 0.0, "interval_n": 0, "sum": {}, "count": {}}
        self._win_start = time.monotonic()
        self._pad_base = self._read_pad_src()

    def new_session(self):
        """Starts a fresh timing session; called when a ROM load begins."""
        try:
            self._flush()
        except Exception as _swallowed_error:
            _log_swallowed("new_session: self._flush()", _swallowed_error)
        self._session_t0 = time.monotonic()
        self._phase = None
        self._prev_t0 = None
        self._prev_phase = None
        self._reset_window()

    def add(self, stage, ms):
        self._scratch[stage] = self._scratch.get(stage, 0.0) + ms

    def count(self, stage):
        self._scratch_counts[stage] = self._scratch_counts.get(stage, 0) + 1

    def tick_start(self):
        self._t0 = time.perf_counter()
        self._scratch.clear()
        self._scratch_counts.clear()

    def tick_end(self, phase, console, runs, cap):
        """Closes one drawn frame: merges its stage times into the window and logs the window when it is due."""
        if self._t0 is None:
            return
        try:
            end = time.perf_counter()
            total = (end - self._t0) * 1000.0
            self._console = console or self._console
            if phase != self._phase:
                self._flush()
                self._phase = phase
                self._reset_window()
            w = self._win
            w["ticks"] += 1
            w["total"] += total
            w["worst"] = max(w["worst"], total)
            w["runs"] += runs
            w["max_runs"] = max(w["max_runs"], runs)
            w["cap"] = cap
            if runs > 1:
                w["catchup"] += 1
            if self._prev_t0 is not None and self._prev_phase == phase:
                w["interval"] += (self._t0 - self._prev_t0) * 1000.0
                w["interval_n"] += 1
            self._prev_t0 = self._t0
            self._prev_phase = phase
            for k, v in self._scratch.items():
                w["sum"][k] = w["sum"].get(k, 0.0) + v
            for k, v in self._scratch_counts.items():
                w["count"][k] = w["count"].get(k, 0) + v
            self._scratch.clear()
            self._scratch_counts.clear()
            self._t0 = None
            if _timing_governor is None:
                return
            now = time.monotonic()
            early = now - self._session_t0 < _timing_governor.GAME_FRAME_TIMING_EARLY_SECONDS
            period = (_timing_governor.GAME_FRAME_TIMING_EARLY_LOG_SECONDS if early
                      else _timing_governor.GAME_FRAME_TIMING_LOG_SECONDS)
            if now - self._win_start >= period:
                self._flush()
                self._reset_window()
        except Exception:
            self._t0 = None

    def _flush(self):
        w = self._win
        if w["ticks"] <= 0 or self._phase == "idle" or _timing_governor is None:
            return
        n = float(w["ticks"])
        s = w["sum"]
        c = w["count"]

        def mean(key):
            return s.get(key, 0.0) / n

        total = w["total"] / n
        interval = (w["interval"] / w["interval_n"]) if w["interval_n"] else 0.0
        top = sum(mean(k) for k in self._TOP_LEVEL)
        blit_parts = mean("cache_rebuild") + mean("scale") + mean("shader") + mean("border")
        log.info(
            "[GAME FRAME TIMING] %s phase=%s t=+%.1fs ticks=%d interval=%.1fms (outside the game call %.1fms) "
            "core_runs=%d (max %d per tick, cap %d, %d tick(s) caught up) | mean per tick ms: total=%.1f "
            "input=%.1f core_run=%.1f [video_cb=%.1f audio_cb=%.1f] audio_tick=%.1f color=%.1f pixel=%.1f "
            "blit=%.1f [cache_rebuild=%.1f x%d scale=%.1f shader=%.1f border_art_led=%.1f other=%.1f] "
            "osd=%.1f unaccounted=%.1f | worst tick=%.1fms",
            self._console, self._phase, time.monotonic() - self._session_t0, w["ticks"],
            interval, max(0.0, interval - total),
            w["runs"], w["max_runs"], w["cap"], w["catchup"], total,
            mean("input"), mean("core_run"), mean("video_cb"), mean("audio_cb"), mean("audio_tick"),
            mean("color"), mean("pixel"),
            mean("blit"), mean("cache_rebuild"), c.get("cache_rebuild", 0), mean("scale"), mean("shader"),
            mean("border"), max(0.0, mean("blit") - blit_parts),
            mean("osd"), max(0.0, total - top), w["worst"])
        pad_now = self._read_pad_src()
        pad_base = self._pad_base
        pinned = c.get("pad_pinned", 0)
        bg = ""
        if pad_now is not None and pad_base is not None and pad_now["scans"] >= pad_base["scans"]:
            scans = pad_now["scans"] - pad_base["scans"]
            per = max(1, scans)
            bg = (" | background reader per window: scans=%d mean=%.2fms worst=%.2fms [xinput=%.2f joystick=%.2f] "
                  "hid_scans=%d (%.1fms) joystick_reads=%d" % (
                      scans, (pad_now["ms"] - pad_base["ms"]) / per, pad_now["worst"],
                      (pad_now["xinput"] - pad_base["xinput"]) / per, (pad_now["joy"] - pad_base["joy"]) / per,
                      pad_now["hid_scans"] - pad_base["hid_scans"], pad_now["hid_ms"] - pad_base["hid_ms"],
                      pad_now["joy_reads"] - pad_base["joy_reads"]))
        log.info(
            "[GAME PAD TIMING] %s phase=%s | game thread per tick: own controller read=%.2fms (x%d, newest background read "
            "was stale x%d) snapshot age=%.1fms calls served=%.1f%s",
            self._console, self._phase, mean("pad_scan"), c.get("pad_scan", 0), c.get("pad_stale", 0),
            (s.get("pad_age", 0.0) / pinned) if pinned else 0.0, c.get("pad_served", 0) / n, bg)


GAME_FRAME_TIMER = GameFrameTimer()

# ============================================================================
# SECTION: Throttled logging & DLL handle
# ============================================================================
_WARN_THROTTLE_INTERVAL_S = 5.0
_warn_throttle_last = {}


def _log_warn_throttled(_key, msg, *args, **kwargs):
    """Log a warning at most once per _WARN_THROTTLE_INTERVAL_S per _key."""
    now = time.monotonic()
    last = _warn_throttle_last.get(_key, float('-inf'))
    if now - last < _WARN_THROTTLE_INTERVAL_S:
        return
    _warn_throttle_last[_key] = now
    log.warning(msg, *args)


def _free_dll_handle(dll):
    """Release a loaded core .dll's OS module handle (FreeLibrary)."""
    if sys.platform != "win32" or dll is None:
        return
    try:
        handle = getattr(dll, "_handle", None)
        if handle:
            ctypes.windll.kernel32.FreeLibrary.argtypes = [ctypes.wintypes.HMODULE]
            ctypes.windll.kernel32.FreeLibrary(handle)
    except Exception as e:
        log.warning(f"[LIBRETRO] FreeLibrary failed (core .dll may stay locked): {e}")


# ============================================================================
# SECTION: Libretro API constants
# ============================================================================


RETRO_API_VERSION = 1

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

RETRO_MEMORY_SAVE_RAM = 0
RETRO_ENVIRONMENT_GET_LANGUAGE              = 39
RETRO_ENVIRONMENT_GET_AUDIO_VIDEO_ENABLE    = 47
RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION  = 52
RETRO_ENVIRONMENT_SET_CORE_OPTIONS          = 53
RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2       = 67

RETRO_PIXEL_FORMAT_0RGB1555 = 0
RETRO_PIXEL_FORMAT_XRGB8888 = 1
RETRO_PIXEL_FORMAT_RGB565   = 2

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

RETRO_DEVICE_TYPE_SHIFT = 8
RETRO_DEVICE_MASK       = (1 << RETRO_DEVICE_TYPE_SHIFT) - 1

RETRO_DEVICE_INDEX_ANALOG_LEFT  = 0

RETRO_DEVICE_ID_ANALOG_X = 0
RETRO_DEVICE_ID_ANALOG_Y = 1

RETRO_LANGUAGE_ENGLISH = 0


# ============================================================================
# SECTION: Console button mapping
# ============================================================================


CONSOLE_BTN_TO_LIBRETRO = {
    "Up":     RETRO_DEVICE_ID_JOYPAD_UP,
    "Down":   RETRO_DEVICE_ID_JOYPAD_DOWN,
    "Left":   RETRO_DEVICE_ID_JOYPAD_LEFT,
    "Right":  RETRO_DEVICE_ID_JOYPAD_RIGHT,
    "Start":  RETRO_DEVICE_ID_JOYPAD_START,
    "Select": RETRO_DEVICE_ID_JOYPAD_SELECT,
    "Option": RETRO_DEVICE_ID_JOYPAD_SELECT,
    "A":      RETRO_DEVICE_ID_JOYPAD_A,
    "B":      RETRO_DEVICE_ID_JOYPAD_B,
    "X":      RETRO_DEVICE_ID_JOYPAD_X,
    "Y":      RETRO_DEVICE_ID_JOYPAD_Y,
    "L":      RETRO_DEVICE_ID_JOYPAD_L,
    "R":      RETRO_DEVICE_ID_JOYPAD_R,
    "L2":     RETRO_DEVICE_ID_JOYPAD_L2,
    "R2":     RETRO_DEVICE_ID_JOYPAD_R2,
    "L3":     RETRO_DEVICE_ID_JOYPAD_L3,
    "R3":     RETRO_DEVICE_ID_JOYPAD_R3,
    "1":      RETRO_DEVICE_ID_JOYPAD_B,
    "2":      RETRO_DEVICE_ID_JOYPAD_A,
    "C":      RETRO_DEVICE_ID_JOYPAD_R,
    "Z":      RETRO_DEVICE_ID_JOYPAD_L,
    "Mode":   RETRO_DEVICE_ID_JOYPAD_SELECT,
    "Cross":    RETRO_DEVICE_ID_JOYPAD_B,
    "Circle":   RETRO_DEVICE_ID_JOYPAD_A,
    "Square":   RETRO_DEVICE_ID_JOYPAD_Y,
    "Triangle": RETRO_DEVICE_ID_JOYPAD_X,
    "L1":       RETRO_DEVICE_ID_JOYPAD_L,
    "R1":       RETRO_DEVICE_ID_JOYPAD_R,
    "CUp":    RETRO_DEVICE_ID_JOYPAD_Y,
    "CDown":  RETRO_DEVICE_ID_JOYPAD_SELECT,
    "CLeft":  RETRO_DEVICE_ID_JOYPAD_L2,
    "CRight": RETRO_DEVICE_ID_JOYPAD_R2,
}

CONSOLE_BTN_TO_LIBRETRO_OVERRIDES = {
    ("N64", "Z"): RETRO_DEVICE_ID_JOYPAD_X,
}

def lr_id_for(console, btn_name):
    override = CONSOLE_BTN_TO_LIBRETRO_OVERRIDES.get((console, btn_name))
    if override is not None:
        return override
    return CONSOLE_BTN_TO_LIBRETRO.get(btn_name)


# ============================================================================
# SECTION: C structures & callback types
# ============================================================================


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


RETRO_NUM_CORE_OPTION_VALUES_MAX = 128


class retro_core_option_value(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.c_char_p),
        ("label", ctypes.c_char_p),
    ]


class retro_core_option_definition(ctypes.Structure):
    _fields_ = [
        ("key",           ctypes.c_char_p),
        ("desc",          ctypes.c_char_p),
        ("info",          ctypes.c_char_p),
        ("values",        retro_core_option_value * RETRO_NUM_CORE_OPTION_VALUES_MAX),
        ("default_value", ctypes.c_char_p),
    ]


class retro_core_option_v2_category(ctypes.Structure):
    _fields_ = [
        ("key",  ctypes.c_char_p),
        ("desc", ctypes.c_char_p),
        ("info", ctypes.c_char_p),
    ]


class retro_core_option_v2_definition(ctypes.Structure):
    _fields_ = [
        ("key",              ctypes.c_char_p),
        ("desc",             ctypes.c_char_p),
        ("desc_categorized", ctypes.c_char_p),
        ("info",             ctypes.c_char_p),
        ("info_categorized", ctypes.c_char_p),
        ("category_key",     ctypes.c_char_p),
        ("values",           retro_core_option_value * RETRO_NUM_CORE_OPTION_VALUES_MAX),
        ("default_value",    ctypes.c_char_p),
    ]


class retro_core_options_v2(ctypes.Structure):
    _fields_ = [
        ("categories",  ctypes.POINTER(retro_core_option_v2_category)),
        ("definitions", ctypes.POINTER(retro_core_option_v2_definition)),
    ]


class retro_controller_description(ctypes.Structure):
    _fields_ = [
        ("desc", ctypes.c_char_p),
        ("id",   ctypes.c_uint),
    ]


class retro_controller_info(ctypes.Structure):
    _fields_ = [
        ("types",     ctypes.POINTER(retro_controller_description)),
        ("num_types", ctypes.c_uint),
    ]


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


RETRO_ENVIRONMENT_CB        = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)
RETRO_VIDEO_REFRESH_CB      = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t)
RETRO_AUDIO_SAMPLE_CB       = ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
RETRO_AUDIO_SAMPLE_BATCH_CB = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t)
RETRO_INPUT_POLL_CB         = ctypes.CFUNCTYPE(None)
RETRO_INPUT_STATE_CB        = ctypes.CFUNCTYPE(ctypes.c_int16, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)


# ============================================================================
# SECTION: Input state
# ============================================================================


class LibretroInputState:

    def __init__(self):
        self._buttons = {}
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


class LibretroInputManager:

    def __init__(self):
        self._ports = {p: LibretroInputState() for p in range(4)}

    def port(self, port_num):
        st = self._ports.get(port_num)
        if st is None:
            st = LibretroInputState()
            self._ports[port_num] = st
        return st

    def get_button(self, port_num, btn_id):
        return self.port(port_num).get_button(btn_id)

    def get_analog(self, port_num, index, axis_id):
        return self.port(port_num).get_analog(index, axis_id)

    def set_buttons(self, port_num, state_dict):
        self.port(port_num).set_buttons(state_dict)

    def set_analog(self, port_num, index, axis_id, value):
        self.port(port_num).set_analog(index, axis_id, value)

    def both_pressed(self, port_num, a, b):
        return self.port(port_num).both_pressed(a, b)

    def clear(self, port_num=None):
        if port_num is None:
            for st in self._ports.values():
                st.clear()
        else:
            self.port(port_num).clear()


# ============================================================================
# SECTION: Audio output
# ============================================================================


class LibretroAudio:

    # VALUES CHANGE HERE
    CHANNEL_ID  = 6
    OUT_CHUNK   = 1024
    TARGET_OUT  = 4096
    MAX_OUT     = 12288
    PREBUFFER   = 3072
    DRC_RANGE   = 0.004

    LOUDNESS_GAIN = 0.45

    def __init__(self, sample_rate=32768):
        self._sample_rate = int(sample_rate) if sample_rate else 32768

        self._in_blocks = collections.deque()
        self._in_count  = 0
        self._buf_lock  = threading.Lock()

        self._src = np.empty((0, 2), dtype=np.float32)

        self._out = np.empty((0, 2), dtype=np.int16)

        self._pos = 0.0

        self._channel     = None
        # WARNING: keep hard refs to the playing and queued Sound objects so GC never frees one the channel still needs.
        self._snd_playing = None
        self._snd_queued  = None

        self._volume = 1.0
        self._muted  = False
        self._ready  = False

        try:
            self._mixer_freq, _, self._mixer_channels = pygame.mixer.get_init()
        except Exception:
            self._mixer_freq     = 44100
            self._mixer_channels = 2

        self._base_ratio = (self._mixer_freq / self._sample_rate
                             if self._sample_rate > 0 else 1.0)

    MAX_BATCH_FRAMES = 500_000

    def push_samples(self, ptr, frames):
        """Zero-copy ingest of int16 stereo pairs from a ctypes pointer."""
        try:
            frames = int(frames)
        except (TypeError, ValueError):
            log.error("[LIBRETRO AUDIO] non-integer frame count from core")
            return 0
        if frames <= 0:
            return frames
        if frames > self.MAX_BATCH_FRAMES:
            log.error("[LIBRETRO AUDIO] frame count %d exceeds the %d-frame safety cap", frames, self.MAX_BATCH_FRAMES)
            return 0
        try:
            raw = np.frombuffer(
                (ctypes.c_int16 * (frames * 2)).from_address(
                    ctypes.cast(ptr, ctypes.c_void_p).value),
                dtype=np.int16
            ).reshape(frames, 2).copy()

            with self._buf_lock:
                self._in_blocks.append(raw)
                self._in_count += frames
                while self._in_count > self.MAX_OUT * 3 and len(self._in_blocks) > 1:
                    dropped = self._in_blocks.popleft()
                    self._in_count -= len(dropped)
        except Exception as exc:
            log.error("[LIBRETRO AUDIO] push_samples error: %s", exc)
        return frames


    def _resample_into_out(self, ratio):
        src = self._src
        n_src = len(src)
        if n_src < 2 or ratio <= 0:
            return

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

        new_pos  = self._pos + n_out / ratio
        consumed = int(np.floor(new_pos))
        if consumed > n_src - 1:
            consumed = n_src - 1
        self._pos = new_pos - consumed

        self._src = src[consumed:]

        if len(self._out):
            self._out = np.concatenate([self._out, out], axis=0)
        else:
            self._out = out
        if len(self._out) > self.MAX_OUT:
            self._out = self._out[-self.MAX_OUT:]


    def tick(self):
        if self._muted:
            with self._buf_lock:
                self._in_blocks.clear()
                self._in_count = 0
            return

        if self._channel is None:
            try:
                self._channel = pygame.mixer.Channel(self.CHANNEL_ID)
                self._channel.set_volume(self._volume)
            except Exception as _swallowed_error:
                _log_swallowed("tick: self._channel = pygame.mixer.Channel(self.CHANNEL_ID)", _swallowed_error)
                return

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

        buffered = len(self._out)
        error    = (buffered - self.TARGET_OUT) / float(self.TARGET_OUT)
        error    = max(-1.0, min(1.0, error))
        ratio    = self._base_ratio * (1.0 - self.DRC_RANGE * error)

        self._resample_into_out(ratio)

        if not self._ready:
            if len(self._out) < self.PREBUFFER:
                return
            self._ready = True

        try:
            busy = self._channel.get_busy()
            if not busy:
                self._feed_one_chunk(start=True)
                self._feed_one_chunk(start=False)
            elif self._channel.get_queue() is None:
                self._feed_one_chunk(start=False)
        except Exception as exc:
            log.error("[LIBRETRO AUDIO] tick feed error: %s", exc)

    def _feed_one_chunk(self, start):
        """Pop one OUT_CHUNK of output and play or queue it on the channel."""
        if len(self._out) < self.OUT_CHUNK:
            return
        chunk     = np.ascontiguousarray(self._out[:self.OUT_CHUNK])
        self._out = self._out[self.OUT_CHUNK:]
        if self._mixer_channels == 1 and chunk.ndim == 2 and chunk.shape[1] == 2:
            chunk = ((chunk[:, 0].astype(np.int32) + chunk[:, 1].astype(np.int32)) // 2).astype(np.int16)
            chunk = np.ascontiguousarray(chunk)
        try:
            snd = pygame.sndarray.make_sound(chunk)
        except Exception as exc:
            log.warning(f"[LIBRETRO AUDIO] make_sound error: {exc}")
            return
        self._snd_playing = self._snd_queued
        self._snd_queued  = snd
        if start:
            self._channel.play(snd)
        else:
            self._channel.queue(snd)


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
        log.info("[LIBRETRO AUDIO] Source sample rate updated to %.1fHz", sample_rate)


    def set_volume(self, volume_pct):
        pct = max(0.0, min(100.0, float(volume_pct)))
        if pct <= 0:
            curved_frac = 0.0
        else:
            frac = pct / 100.0
            db_level = -18.0 * (1.0 - frac)
            curved_frac = 10 ** (db_level / 20.0)
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
        self._ready       = False


# ============================================================================
# SECTION: Video filters
# ============================================================================


class CRTFilter:

    def apply(self, surface, brightness=50, contrast=50, saturation=50,
              sharpness=50, hue_shift=50):
        if (brightness == 50 and contrast == 50 and saturation == 50
                and sharpness == 50 and hue_shift == 50):
            return surface

        try:
            arr = pygame.surfarray.array3d(surface).astype(np.float32)

            if brightness != 50:
                arr = arr * (brightness / 50.0)

            if contrast != 50:
                arr = (arr - 127.5) * (contrast / 50.0) + 127.5

            arr = np.clip(arr, 0, 255)

            if saturation != 50:
                grey = arr.mean(axis=2, keepdims=True)
                arr  = grey + (arr - grey) * (saturation / 50.0)
                arr  = np.clip(arr, 0, 255)

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
            _log_warn_throttled("crt_filter", f"[CRT FILTER] {e}")
            return surface


class PixelArtScaler:
    """Edge-preserving 2x upscale (Scale2x/AdvMAME2x/EPX) of the core's native-resolution frame, applied before the final stretch."""

    def __init__(self):
        self._cache_key = None
        self._cache_surf = None

    @staticmethod
    def _scale2x_pass(arr):
        """One 2x pass. arr: (H, W, 3) uint8 -> (2H, 2W, 3) uint8."""
        h, w = arr.shape[0], arr.shape[1]
        packed = (arr[:, :, 0].astype(np.uint32) << 16
                  | arr[:, :, 1].astype(np.uint32) << 8
                  | arr[:, :, 2].astype(np.uint32))
        padded = np.pad(packed, ((1, 1), (1, 1)), mode="edge")
        C = padded[1:-1, 1:-1]
        B = padded[0:-2, 1:-1]
        H = padded[2:, 1:-1]
        D = padded[1:-1, 0:-2]
        F = padded[1:-1, 2:]

        gate = (B != H) & (D != F)
        out_packed = np.empty((h * 2, w * 2), dtype=np.uint32)
        out_packed[0::2, 0::2] = np.where(gate & (D == B), D, C)
        out_packed[0::2, 1::2] = np.where(gate & (B == F), B, C)
        out_packed[1::2, 0::2] = np.where(gate & (D == H), D, C)
        out_packed[1::2, 1::2] = np.where(gate & (F == H), F, C)

        out = np.empty((h * 2, w * 2, 3), dtype=np.uint8)
        out[:, :, 0] = (out_packed >> 16) & 0xFF
        out[:, :, 1] = (out_packed >> 8) & 0xFF
        out[:, :, 2] = out_packed & 0xFF
        return out

    def apply(self, surface, factor=2):
        """Return `surface` scaled by `factor` (2 or 4, other values fall back to 2) using Scale2x, cached by source-surface identity and factor."""
        if factor not in (2, 4):
            factor = 2
        cache_key = (id(surface), factor)
        if self._cache_key == cache_key and self._cache_surf is not None:
            return self._cache_surf
        try:
            arr = pygame.surfarray.array3d(surface)
            arr = arr.transpose(1, 0, 2)
            out = self._scale2x_pass(arr)
            if factor == 4:
                out = self._scale2x_pass(out)
            out_surf = pygame.surfarray.make_surface(out.transpose(1, 0, 2))
            self._cache_key = cache_key
            self._cache_surf = out_surf
            return out_surf
        except Exception as e:
            _log_warn_throttled("pixel_art_scaler", f"[PIXEL SCALER] {e}")
            return surface


PIXEL_ART_SCALER_DEFAULT_FACTOR = 2


# ============================================================================
# SECTION: Core wrapper
# ============================================================================


class LibretroCore:

    # ========================================================================
    # SECTION: Setup & core loading
    # ========================================================================

    def __init__(self, dll_path, system_dir, save_dir, create_system_dir=True):
        self.dll_path      = dll_path
        self.system_dir    = system_dir
        self.save_dir      = save_dir
        self._create_system_dir = create_system_dir
        self.active_profile = 1
        self._profile_save_dir = os.path.join(self.save_dir, "profile1")

        self._dll       = None
        self._rom_path  = ""
        self._loaded    = False
        self._frozen    = False
        self._pixel_fmt = RETRO_PIXEL_FORMAT_XRGB8888

        self._vid_surface     = None
        self._raw_vid_surface = None

        self._fps         = 60.0
        self._sample_rate = 32768.0
        self._base_w      = 240
        self._base_h      = 160
        self._need_fullpath = False

        self._audio  = LibretroAudio()
        self._input  = LibretroInputManager()
        self._crt    = CRTFilter()
        self._pixel_scaler = PixelArtScaler()

        self._volume = 100
        self._muted  = False

        self._sys_dir_b  = b""
        self._save_dir_b = b""

        self._disk_control  = None
        # WARNING: _disc_data_ref and _rom_data_ref keep the core's raw buffers alive; dropping them frees memory the core still reads.
        self._disc_data_ref = None
        self._rom_data_ref  = None

        # WARNING: the ctypes callback refs below must stay referenced or the core calls freed callbacks.
        self._cb_env   = None
        self._cb_vid   = None
        self._cb_aud   = None
        self._cb_audb  = None
        self._cb_inp   = None
        self._cb_inps  = None

        self._core_variables = {}
        self._var_dirty      = False

        self._controller_info = None
        self._multitap_hub_port = None

        self._rom_load_ts        = None
        self._first_frame_ts     = None
        self._first_nonblack_ts  = None


    def load_dll(self):
        """Load and initialise the core DLL. Returns True on success."""
        if not os.path.exists(self.dll_path):
            log.warning(f"[LIBRETRO] DLL not found: {self.dll_path}")
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
            log.warning(f"[LIBRETRO] DLL load failed: {e}")
            return False

        self._setup_prototypes()
        self._register_callbacks()

        try:
            self._dll.retro_init()
        except Exception as e:
            log.warning(f"[LIBRETRO] retro_init() failed: {e}")
            return False

        self._need_fullpath = False
        _core_name = "unknown"
        _core_version = "unknown"
        try:
            info = retro_system_info()
            self._dll.retro_get_system_info(ctypes.byref(info))
            self._need_fullpath = bool(info.need_fullpath)
            if info.library_name:
                _core_name = info.library_name.decode("utf-8", "replace")
            if info.library_version:
                _core_version = info.library_version.decode("utf-8", "replace")
        except Exception as e:
            log.warning("retro_get_system_info failed (assuming need_fullpath=False): %s", e)
        log.info("[LIBRETRO] Core loaded: %s v%s (%s) -- %sx%s @ %.1ffps, "
                 "audio %sHz, need_fullpath=%s",
                 _core_name, _core_version, os.path.basename(self.dll_path),
                 self._base_w, self._base_h, self._fps,
                 int(self._sample_rate), self._need_fullpath)
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


    def _parse_controller_info(self, data):
        infos = []
        arr = ctypes.cast(data, ctypes.POINTER(retro_controller_info))
        for port in range(64):
            info = arr[port]
            if not info.types:
                break
            types = []
            for i in range(info.num_types):
                desc = info.types[i]
                name = desc.desc.decode("utf-8", "ignore") if desc.desc else f"type_{desc.id}"
                types.append((name, desc.id))
            infos.append(types)
        return infos

    # ========================================================================
    # SECTION: Core callbacks
    # ========================================================================

    def _cb_environment(self, cmd, data):
        try:
            if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
                fmt = ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value
                self._pixel_fmt = fmt
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
                if self._create_system_dir:
                    os.makedirs(self.system_dir, exist_ok=True)
                self._sys_dir_b = self.system_dir.encode("utf-8")
                ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = self._sys_dir_b
                return True
            elif cmd == RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY:
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
                ctypes.cast(data, ctypes.POINTER(ctypes.c_uint))[0] = 0
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_DISK_CONTROL_INTERFACE:
                try:
                    dc_struct = ctypes.cast(
                        data, ctypes.POINTER(retro_disk_control_callback)).contents
                    self._disk_control = dc_struct
                    print("[LIBRETRO] Disk control interface registered �� live disc swap available")
                except Exception as e:
                    log.warning(f"[LIBRETRO] Failed to read disk control interface: {e}")
                    self._disk_control = None
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO:
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
                    log.warning(f"[LIBRETRO ENV] SET_SYSTEM_AV_INFO read failed: {e}")
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_CONTROLLER_INFO:
                try:
                    self._controller_info = self._parse_controller_info(data)
                    for _port, _types in enumerate(self._controller_info):
                        _names = ", ".join(f"{n!r}(id={i})" for n, i in _types)
                        print(f"[LIBRETRO] Controller info port {_port}: {_names}")
                except Exception as e:
                    log.warning(f"[LIBRETRO ENV] SET_CONTROLLER_INFO read failed "
                          f"(falling back to forcing RETRO_DEVICE_JOYPAD on every "
                          f"port at load time): {e}")
                    self._controller_info = None
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_VARIABLES:
                try:
                    arr = ctypes.cast(data, ctypes.POINTER(retro_variable))
                    i = 0
                    while arr[i].key:
                        key = arr[i].key.decode("utf-8", "ignore")
                        raw = arr[i].value.decode("utf-8", "ignore") if arr[i].value else ""
                        default = raw.split(";", 1)[-1].strip().split("|", 1)[0].strip()
                        if key and key not in self._core_variables and default:
                            self._core_variables[key] = default.encode("utf-8")
                        i += 1
                except Exception as e:
                    log.warning(f"[LIBRETRO ENV] SET_VARIABLES parse failed: {e}")
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_CORE_OPTIONS:
                try:
                    arr = ctypes.cast(data, ctypes.POINTER(retro_core_option_definition))
                    i = 0
                    while arr[i].key:
                        key = arr[i].key.decode("utf-8", "ignore")
                        default_b = arr[i].default_value
                        if default_b:
                            default = default_b.decode("utf-8", "ignore")
                        elif arr[i].values[0].value:
                            default = arr[i].values[0].value.decode("utf-8", "ignore")
                        else:
                            default = ""
                        if key and key not in self._core_variables and default:
                            self._core_variables[key] = default.encode("utf-8")
                        i += 1
                except Exception as e:
                    log.warning(f"[LIBRETRO ENV] SET_CORE_OPTIONS parse failed: {e}")
                return True
            elif cmd == RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2:
                try:
                    opts = ctypes.cast(data, ctypes.POINTER(retro_core_options_v2)).contents
                    defs = opts.definitions
                    i = 0
                    while defs[i].key:
                        key = defs[i].key.decode("utf-8", "ignore")
                        default_b = defs[i].default_value
                        if default_b:
                            default = default_b.decode("utf-8", "ignore")
                        elif defs[i].values[0].value:
                            default = defs[i].values[0].value.decode("utf-8", "ignore")
                        else:
                            default = ""
                        if key and key not in self._core_variables and default:
                            self._core_variables[key] = default.encode("utf-8")
                        i += 1
                except Exception as e:
                    log.warning(f"[LIBRETRO ENV] SET_CORE_OPTIONS_V2 parse failed: {e}")
                return True
            elif cmd in (RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME,
                         RETRO_ENVIRONMENT_SET_GEOMETRY, RETRO_ENVIRONMENT_SET_PERFORMANCE_LEVEL):
                return True
        except Exception as e:
            _log_warn_throttled("env_cb_cmd_failed", f"[LIBRETRO ENV] cmd={cmd}: {e}")
        return False

    _MAX_FRAME_DIM   = 4096
    _MAX_FRAME_PITCH = 4096 * 4
    _MAX_FRAME_BYTES = 64 * 1024 * 1024

    def _cb_video_refresh(self, data, width, height, pitch):
        _t0 = time.perf_counter()
        try:
            self._cb_video_refresh_impl(data, width, height, pitch)
        finally:
            GAME_FRAME_TIMER.add("video_cb", (time.perf_counter() - _t0) * 1000.0)

    def _cb_video_refresh_impl(self, data, width, height, pitch):
        if data is None:
            return
        try:
            width, height, pitch = int(width), int(height), int(pitch)
        except (TypeError, ValueError):
            _log_warn_throttled("video_cb_bad_geometry", "[LIBRETRO VIDEO] non-integer frame geometry from core")
            return
        if width <= 0 or height <= 0 or pitch <= 0:
            _log_warn_throttled("video_cb_bad_geometry", f"[LIBRETRO VIDEO] non-positive frame geometry w={width} h={height} pitch={pitch}")
            return
        if (width > self._MAX_FRAME_DIM or height > self._MAX_FRAME_DIM
                or pitch > self._MAX_FRAME_PITCH or pitch < width):
            _log_warn_throttled("video_cb_bad_geometry", f"[LIBRETRO VIDEO] frame geometry out of bounds w={width} h={height} pitch={pitch}")
            return
        if height * pitch > self._MAX_FRAME_BYTES:
            _log_warn_throttled("video_cb_bad_geometry", f"[LIBRETRO VIDEO] frame size exceeds cap w={width} h={height} pitch={pitch}")
            return
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
                    if int(rgb.max()) > 8:
                        self._first_nonblack_ts = time.time()
                        ms = int((self._first_nonblack_ts - self._rom_load_ts) * 1000)
                        print(f"[LIBRETRO BOOT] First NON-BLACK frame {ms}ms after ROM load "
                              f"— this is the real black-screen duration the player experiences")

            self._base_w = width
            self._base_h = height
            self._raw_vid_surface = pygame.surfarray.make_surface(
                np.ascontiguousarray(rgb.transpose(1, 0, 2)))
            self._vid_surface = self._raw_vid_surface
    
        except Exception as e:
            _log_warn_throttled("video_cb", f"[LIBRETRO VIDEO] {e}")

    def _cb_audio_sample(self, left, right):
        pair = np.array([[left, right]], dtype=np.int16)
        with self._audio._buf_lock:
            self._audio._in_blocks.append(pair)
            self._audio._in_count += 1
            while self._audio._in_count > self._audio.MAX_OUT * 3 and len(self._audio._in_blocks) > 1:
                dropped = self._audio._in_blocks.popleft()
                self._audio._in_count -= len(dropped)

    def _cb_audio_batch(self, data, frames):
        _t0 = time.perf_counter()
        try:
            return self._audio.push_samples(data, frames)
        finally:
            GAME_FRAME_TIMER.add("audio_cb", (time.perf_counter() - _t0) * 1000.0)

    def _cb_input_poll(self):
        """No-op: input state is filled in by game_deck._poll_libretro_input()."""

    def _cb_input_state(self, port, device, index, btn_id):
        if device == RETRO_DEVICE_JOYPAD:
            return self._input.get_button(port, btn_id)
        if device == RETRO_DEVICE_ANALOG:
            return self._input.get_analog(port, index, btn_id)
        return 0

    # ========================================================================
    # SECTION: ROM loading & controller ports
    # ========================================================================

    def load_rom(self, rom_path):
        """Load a ROM file. Returns True on success."""
        GAME_FRAME_TIMER.new_session()
        self._rom_load_ts       = time.time()
        self._first_frame_ts    = None
        self._first_nonblack_ts = None
        if not os.path.exists(rom_path):
            log.warning(f"[LIBRETRO] ROM not found: {rom_path}")
            return False
        if self._dll is None:
            log.warning("[LIBRETRO] Call load_dll() first")
            return False

        try:
            _disk_size = os.path.getsize(rom_path)
        except OSError:
            _disk_size = -1

        if self._need_fullpath:
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
                log.warning(f"[LIBRETRO] Cannot read ROM: {e}")
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
            log.warning(f"[LIBRETRO] retro_load_game() failed: {e}")
            log.warning(f"[LIBRETRO] Crash context — path={rom_path!r}  "
                  f"need_fullpath={self._need_fullpath}  disk_size={_disk_size:,} B")
            return False

        if not ok:
            log.warning("[LIBRETRO] Core rejected the ROM")
            log.warning(f"[LIBRETRO] retro_load_game() returned False — the core refused the "
                  f"file. Common causes: wrong ROM region/format, missing BIOS in "
                  f"system_dir='{self.system_dir}', or the file is still an archive.")
            return False

        self._rom_path    = rom_path
        # WARNING: keep this reference alive so the core's pointer doesn't dangle.
        self._rom_data_ref = c_data

        self._read_av_info()
        self._audio = LibretroAudio(int(self._sample_rate))
        self._audio.set_volume(self._volume)
        self._audio.set_muted(self._muted)

        self._multitap_hub_port = None
        self._negotiate_controller_ports()

        os.makedirs(self.save_dir, exist_ok=True)
        self._loaded = True
        self._frozen = False

        self._migrate_stray_save_file(rom_path)
        self.load_sram()

        print(f"[LIBRETRO] ROM loaded: {os.path.basename(rom_path)}  "
              f"{self._base_w}×{self._base_h} @ {self._fps:.1f}fps")
        return True

    def _pick_plain_joypad_id(self, types):
        for _, tid in types:
            if tid == RETRO_DEVICE_JOYPAD:
                return tid
        for _, tid in types:
            if (tid & RETRO_DEVICE_MASK) == RETRO_DEVICE_JOYPAD:
                return tid
        for name, tid in types:
            lname = name.lower()
            if any(k in lname for k in ("retropad", "gamepad", "joypad", "standard", "pad")):
                return tid
        return types[0][1] if types else RETRO_DEVICE_JOYPAD

    def _negotiate_controller_ports(self):
        self._multitap_hub_candidates = {}
        info = self._controller_info
        for _port in range(4):
            types = info[_port] if info and _port < len(info) else None
            if types:
                for name, tid in types:
                    lname = name.lower()
                    if ("multitap" in lname or "multi-tap" in lname or "multi tap" in lname
                            or ("tap" in lname and "player" in lname)):
                        self._multitap_hub_candidates[_port] = (name, tid)
                dev_id = self._pick_plain_joypad_id(types)
            else:
                dev_id = RETRO_DEVICE_JOYPAD
            try:
                self._dll.retro_set_controller_port_device(_port, dev_id)
            except Exception as e:
                log.warning("retro_set_controller_port_device(%d, %d) failed "
                             "(core may not recognize input on this port for "
                             "this session): %s", _port, dev_id, e)

    def multitap_available(self):
        return bool(getattr(self, "_multitap_hub_candidates", None))

    def multitap_active(self):
        return getattr(self, "_multitap_hub_port", None) is not None

    def enable_multitap(self, hub_port=None):
        candidates = getattr(self, "_multitap_hub_candidates", None)
        if not candidates:
            return False
        if hub_port is None:
            hub_port = next(iter(candidates))
        if hub_port not in candidates:
            return False
        if self._multitap_hub_port == hub_port:
            return True
        name, dev_id = candidates[hub_port]
        try:
            self._dll.retro_set_controller_port_device(hub_port, dev_id)
        except Exception as e:
            log.warning("enable_multitap: retro_set_controller_port_device(%d, %d) failed: %s",
                        hub_port, dev_id, e)
            return False
        self._multitap_hub_port = hub_port
        print(f"[LIBRETRO] Multitap enabled on port {hub_port} ({name!r}, id={dev_id})")
        return True

    def disable_multitap(self):
        if self._multitap_hub_port is None:
            return
        port = self._multitap_hub_port
        try:
            self._dll.retro_set_controller_port_device(port, RETRO_DEVICE_JOYPAD)
            print(f"[LIBRETRO] Multitap disabled on port {port} -- reverted to plain joypad")
        except Exception as e:
            log.warning("disable_multitap: retro_set_controller_port_device(%d) failed: %s", port, e)
        self._multitap_hub_port = None

    # ========================================================================
    # SECTION: Disc change & unload
    # ========================================================================

    def change_disc(self, new_disc_path):
        if self._disk_control is None:
            log.warning("[LIBRETRO] Core has no disk control interface — cannot swap discs live")
            return False
        if not os.path.exists(new_disc_path):
            log.warning(f"[LIBRETRO] Disc image not found: {new_disc_path}")
            return False

        try:
            with open(new_disc_path, "rb") as f:
                disc_data = f.read()
        except Exception as e:
            log.warning(f"[LIBRETRO] Cannot read disc image: {e}")
            return False

        c_data = (ctypes.c_uint8 * len(disc_data)).from_buffer_copy(disc_data)
        gi     = retro_game_info()
        gi.path = new_disc_path.encode()
        gi.data = ctypes.cast(c_data, ctypes.c_void_p)
        gi.size = len(disc_data)
        gi.meta = None
        self._disc_data_ref = c_data

        dc = self._disk_control
        try:
            dc.set_eject_state(True)
            ok = bool(dc.replace_image_index(0, ctypes.byref(gi)))
            dc.set_eject_state(False)
        except Exception as e:
            log.warning(f"[LIBRETRO] Disc swap failed: {e}")
            return False

        if ok:
            self._rom_path = new_disc_path
            print(f"[LIBRETRO] Disc changed live: {os.path.basename(new_disc_path)}")
        else:
            log.warning("[LIBRETRO] Core rejected the replacement disc image")
        return ok

    def unload(self):
        if not self._loaded:
            return
        # WARNING: save SRAM before the core and its memory buffer are released.
        self.save_sram()
        try:
            self._dll.retro_unload_game()
            self._dll.retro_deinit()
        except Exception as e:
            log.warning(f"[LIBRETRO] Unload error: {e}")
        self._audio.stop()
        self._loaded = False
        _free_dll_handle(self._dll)
        self._dll    = None
        print("[LIBRETRO] Core unloaded.")

    def unload_game(self):
        if not self._loaded or self._dll is None:
            return
        # WARNING: save SRAM before the game and its memory buffer are released.
        self.save_sram()
        try:
            self._dll.retro_unload_game()
        except Exception as e:
            log.warning(f"[LIBRETRO] unload_game error: {e}")
        self._audio.stop()
        self._loaded = False
        self._frozen = False
        self._disk_control = None
        self._rom_data_ref = None
        self._disc_data_ref = None
        print("[LIBRETRO] Game unloaded (core still initialized).")

    # ========================================================================
    # SECTION: Frame loop & playback state
    # ========================================================================

    def run_frame(self, crt_cfg=None, pixel_cfg=None):
        if not self._loaded or self._frozen or self._dll is None:
            return self._vid_surface

        _t0 = time.perf_counter()
        try:
            self._dll.retro_run()

        except Exception as e:
            _log_warn_throttled("retro_run", f"[LIBRETRO] retro_run(): {e}")
            return self._vid_surface
        finally:
            GAME_FRAME_TIMER.add("core_run", (time.perf_counter() - _t0) * 1000.0)

        _t0 = time.perf_counter()
        self._audio.tick()
        GAME_FRAME_TIMER.add("audio_tick", (time.perf_counter() - _t0) * 1000.0)

        if self._raw_vid_surface and crt_cfg:
            if any(v != 50 for v in crt_cfg.values()):
                _t0 = time.perf_counter()
                self._vid_surface = self._crt.apply(
                    self._raw_vid_surface,
                    brightness=crt_cfg.get("brightness", 50),
                    contrast=crt_cfg.get("contrast",   50),
                    saturation=crt_cfg.get("saturation", 50),
                    sharpness=crt_cfg.get("sharpness",  50),
                    hue_shift=crt_cfg.get("hue_shift",  50),
                )
                GAME_FRAME_TIMER.add("color", (time.perf_counter() - _t0) * 1000.0)
            else:
                self._vid_surface = self._raw_vid_surface

        if pixel_cfg and pixel_cfg.get("enabled") and self._vid_surface is not None:
            _t0 = time.perf_counter()
            self._vid_surface = self._pixel_scaler.apply(
                self._vid_surface, factor=pixel_cfg.get("factor", PIXEL_ART_SCALER_DEFAULT_FACTOR))
            GAME_FRAME_TIMER.add("pixel", (time.perf_counter() - _t0) * 1000.0)

        return self._vid_surface

    def reprocess_current_frame(self, crt_cfg=None, pixel_cfg=None):
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
        if pixel_cfg and pixel_cfg.get("enabled") and self._vid_surface is not None:
            self._vid_surface = self._pixel_scaler.apply(
                self._vid_surface, factor=pixel_cfg.get("factor", PIXEL_ART_SCALER_DEFAULT_FACTOR))
        return self._vid_surface

    def get_surface(self):
        return self._vid_surface


    def freeze(self):
        if self._loaded and not self._frozen:
            self._frozen = True
            self._input.clear()
            self._audio.set_muted(True)
            print("[LIBRETRO] Frozen.")

    def unfreeze(self, muted=False):
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
        return self._first_nonblack_ts is not None
    @property
    def fps(self):         return self._fps

    # ========================================================================
    # SECTION: Save profiles, SRAM & save states
    # ========================================================================

    def set_active_profile(self, profile_num):
        self.active_profile = int(profile_num)
        self._profile_save_dir = os.path.join(self.save_dir, f"profile{self.active_profile}")
        os.makedirs(self._profile_save_dir, exist_ok=True)
        print(f"[LIBRETRO] Active save profile set to {self.active_profile}")

    def _state_path(self, slot):
        name = os.path.splitext(os.path.basename(self._rom_path))[0]
        os.makedirs(self._profile_save_dir, exist_ok=True)
        return os.path.join(self._profile_save_dir, f"{name}_slot{slot}.state")

    def _sram_path(self):
        name = os.path.splitext(os.path.basename(self._rom_path))[0]
        os.makedirs(self._profile_save_dir, exist_ok=True)
        return os.path.join(self._profile_save_dir, f"{name}.srm")

    def save_sram(self):
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
            # WARNING: copy the data out before the pointer can move or be invalidated.
            data = bytes(buf)
            path = self._sram_path()
            with open(path, "wb") as f:
                f.write(data)
            print(f"[LIBRETRO] SRAM saved → {os.path.basename(path)} ({size} bytes)")
            return True
        except Exception as e:
            log.warning(f"[LIBRETRO] save_sram: {e}")
            return False

    def load_sram(self):
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
            log.warning(f"[LIBRETRO] load_sram: {e}")
            return False

    def _migrate_stray_save_file(self, rom_path):
        try:
            rom_dir  = os.path.dirname(rom_path)
            rom_name = os.path.splitext(os.path.basename(rom_path))[0]
            dest = self._sram_path()
            if os.path.exists(dest):
                # WARNING: never overwrite a profile's existing save.
                return
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
            log.warning(f"[LIBRETRO] save_state: {e}")
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
            log.warning(f"[LIBRETRO] load_state: {e}")
            return False

    def has_state(self, slot=0):
        return self._loaded and os.path.exists(self._state_path(slot))

    # ========================================================================
    # SECTION: Audio pass-through & core variables
    # ========================================================================

    def set_volume(self, volume_pct):
        self._volume = volume_pct
        self._audio.set_volume(volume_pct)

    def set_muted(self, muted):
        self._muted = bool(muted)
        if not self._frozen:
            self._audio.set_muted(self._muted)


    def set_variable(self, key, value):
        self._core_variables[key] = str(value).encode("utf-8")


# ============================================================================
# SECTION: Core factory
# ============================================================================


BIOS_REQUIRED_CONSOLES = {"PSX"}


def build_core_for_console(console, base_dir, preferred_name=None, preferred_bios_name=None):
    emu_dir    = os.path.join(base_dir, "main", "cores", console)
    save_dir   = os.path.join(base_dir, "main", "roms", console, "saves")
    needs_bios = console in BIOS_REQUIRED_CONSOLES

    system_dir = os.path.join(emu_dir, "system")

    if not os.path.isdir(emu_dir):
        log.warning(f"[LIBRETRO]   core folder does NOT exist: {emu_dir}")

    dll_path = None
    if os.path.isdir(emu_dir):
        _dll_files = [f for f in os.listdir(emu_dir) if f.lower().endswith(".dll")]
        if preferred_name:
            for fname in _dll_files:
                if fname.lower() == preferred_name.lower():
                    dll_path = os.path.join(emu_dir, fname)
                    break
            if dll_path is None:
                log.warning(f"[LIBRETRO]   saved active core {preferred_name!r} not found "
                      f"in {emu_dir}, auto-detecting instead.")
        if dll_path is None:
            _preferred_fname = None
            try:
                from game_deck import resolve_preferred_core_filename
                _preferred_fname = resolve_preferred_core_filename(console, _dll_files)
            except Exception as _pref_err:
                log.warning(f"[LIBRETRO]   resolve_preferred_core_filename failed, "
                      f"falling back to directory order: {_pref_err}")
            if _preferred_fname:
                dll_path = os.path.join(emu_dir, _preferred_fname)
            else:
                for fname in _dll_files:
                    if fname.lower().endswith("_libretro.dll") or fname.lower().endswith("libretro.dll"):
                        dll_path = os.path.join(emu_dir, fname)
                        break
                if dll_path is None and _dll_files:
                    dll_path = os.path.join(emu_dir, _dll_files[0])

    if dll_path is None:
        log.warning(f"[LIBRETRO] No core .dll found in {emu_dir}")
        return None

    if needs_bios:
        if os.path.isdir(system_dir):
            _bios_files = os.listdir(system_dir)
            if not any(
                os.path.isfile(os.path.join(system_dir, name))
                for name in _bios_files
            ):
                log.warning(f"[LIBRETRO]   *** BIOS folder exists but is EMPTY: {system_dir} ***")
                log.warning("[LIBRETRO]   *** Place the required BIOS file(s) there and retry. ***")
        else:
            log.warning(f"[LIBRETRO]   *** BIOS folder does NOT exist yet: {system_dir} ***")
            log.warning("[LIBRETRO]   *** It will be created on first launch, but it needs "
                  "BIOS files placed inside it before the core can boot. ***")

    core = LibretroCore(dll_path, system_dir, save_dir, create_system_dir=needs_bios)

    if console == "PSX":
        _bios_choice = "HLE"
        if preferred_bios_name and os.path.isdir(system_dir):
            for _bf in os.listdir(system_dir):
                if _bf.lower() == preferred_bios_name.lower():
                    _bios_choice = _bf
                    break
            else:
                log.warning(f"[LIBRETRO]   active BIOS {preferred_bios_name!r} not found "
                      f"in {system_dir}, falling back to HLE.")
        print(f"[LIBRETRO] pcsx_rearmed_bios = {_bios_choice}")
        core.set_variable("pcsx_rearmed_bios", _bios_choice)

    if not core.load_dll():
        return None

    return core


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "LibretroCore", "LibretroAudio", "LibretroInputManager", "LibretroInputState",
    "CRTFilter", "PixelArtScaler", "build_core_for_console", "lr_id_for",
    "BIOS_REQUIRED_CONSOLES",
    "RETRO_DEVICE_ID_JOYPAD_UP", "RETRO_DEVICE_ID_JOYPAD_DOWN",
    "RETRO_DEVICE_ID_JOYPAD_LEFT", "RETRO_DEVICE_ID_JOYPAD_RIGHT",
    "RETRO_DEVICE_INDEX_ANALOG_LEFT", "RETRO_DEVICE_ID_ANALOG_X", "RETRO_DEVICE_ID_ANALOG_Y",
]
