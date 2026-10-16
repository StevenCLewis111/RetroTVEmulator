"""Performance governor for the Retro TV emulator: machine profile, throttle level and background-work gates.

The gates (media.py) ask this module before starting background work, and the throttle only tightens
when background work was actually running.

Owns: the per-boot machine profile (cores, RAM, GPU, hardware tier), the core budget and unit
costs, the Lite / Standard / Performance mode table, the hard ceilings table, the smart throttle
level (0-4) with event-vs-sustained classification.
Does not own: any worker pool, semaphore, probe or scan (media.py, retro_tv_emulator.py), saving
the config to disk (media.py RetroDatabase), or reading VLC / loop timing (callers pass signals in).

WARNING: this module imports NO other project module. Callers pass everything in, so an import
cycle between media.py, the main script and this file is impossible. Keep it that way.
WARNING: SHADOW_MODE = False means gates obey the level. Set it True (or config key
"perf_governor_enabled" = False) to return to log-only; both are instant, no other code change.
"""
from __future__ import annotations

import collections
import contextlib
import ctypes
import hashlib
import logging
import os
import platform
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ============================================================================
# SECTION: TUNABLES - every number the governor uses lives in this section
# ============================================================================

# VALUES CHANGE HERE
SHADOW_MODE = False
ACTIVE_MODE_DEFAULT = "standard"
TARGET_FPS = 60.0

# VALUES CHANGE HERE (hardware tiers: the boot scan rates the CPU model first; physical = effective physical cores, see weights below)
TIER_WEAK_MAX_PHYSICAL = 2
TIER_WEAK_MAX_LOGICAL = 4
TIER_WEAK_MIN_RAM_GB = 8.0
# VALUES CHANGE HERE (minimum effective physical cores per CPU class for LOW / REFERENCE / STRONG; None = that class never reaches the tier; below LOW = WEAK)
TIER_MIN_EFFECTIVE_BY_CLASS = {"A": (6, None, None), "B": (4, 8, 10), "C": (3, 5, 8)}
EFFICIENCY_CORE_WEIGHT = 0.5

# VALUES CHANGE HERE (core budget by tier, Standard mode; never above physical - 2 except WEAK)
CORE_BUDGET_BY_TIER = {"WEAK": 1.0, "LOW": 2.0, "REFERENCE": 3.0, "STRONG": 4.0}
STRONG_BUDGET_HIGH = 5.0
STRONG_HIGH_AT_PHYSICAL = 10
CORE_BUDGET_RESERVED_CORES = 2
CORE_BUDGET_MIN = 1.0

# VALUES CHANGE HERE (mode multipliers and caps; the smart throttle runs in every mode)
MODE_NAMES = ("lite", "standard", "performance")
MODE_BUDGET_MULTIPLIER = {"lite": 0.5, "standard": 1.0, "performance": 1.5}
PERFORMANCE_RESERVE_FRACTION = 0.33
PERFORMANCE_RESERVE_MIN_CORES = 2
MODE_FACTOR_CAP = {"lite": 0.5, "standard": 1.0, "performance": 1.0}
TIER_FACTOR_CAP = {"WEAK": 0.5, "LOW": 1.0, "REFERENCE": 1.0, "STRONG": 1.0}

# VALUES CHANGE HERE (hard ceilings per lane and mode; None = shares the loudness lane)
CEILINGS = {
    "probe_and_duration": {"lite": 1, "standard": 3, "performance": 4},
    "loudness_and_bake": {"lite": 1, "standard": 2, "performance": 3},
    "music_loudness": {"lite": None, "standard": 1, "performance": 1},
    "whisper_slots": {"lite": 1, "standard": 1, "performance": 1},
    "cropdetect_threads": {"lite": 1, "standard": 2, "performance": 3},
}

# VALUES CHANGE HERE (extra lane slots added in Performance on a STRONG PC, on top of the CEILINGS row)
PERFORMANCE_STRONG_LANE_BONUS = {"probe_and_duration": 2, "loudness_and_bake": 1, "cropdetect_threads": 1}

# VALUES CHANGE HERE (duration probe batches: the pool is the probe lane's slots; a video playing divides it; chunk = pool size x factor)
DURATION_WORKERS_WHILE_PLAYING_DIVISOR = 3
DURATION_CHUNK_FACTOR_FIRST_PASS = 4
DURATION_CHUNK_FACTOR_BACKLOG = 2

# VALUES CHANGE HERE (loudness scan dispatch: threads per round normally, and when nothing is playing or waiting; both capped by the loudness lane)
LOUDNESS_DISPATCH_WORKERS_BASE = 1
LOUDNESS_DISPATCH_WORKERS_SPARE = 2

# VALUES CHANGE HERE (VLC video decoder threads per hardware tier; applied once, when the VLC engine is created)
VLC_AVCODEC_THREADS_BY_TIER = {"WEAK": 1, "LOW": 2, "REFERENCE": 2, "STRONG": 3}
VLC_AVCODEC_THREADS_DEFAULT = 2

# VALUES CHANGE HERE (Whisper thread formulas; P = effective physical cores)
WHISPER_THREADS_LITE = 2
WHISPER_THREADS_STANDARD_MAX = 4
WHISPER_THREADS_STANDARD_MIN = 2
WHISPER_THREADS_STANDARD_DIVISOR = 2
WHISPER_THREADS_PERFORMANCE_MAX = 8
WHISPER_THREADS_PERFORMANCE_MIN = 2

# VALUES CHANGE HERE (shell mode headroom)
SHELL_HEADROOM_MIN_THREADS = {"standard": 1, "performance": 1}
SHELL_HEADROOM_FRACTION = {"standard": 0.10, "performance": 0.10}
SHELL_CORE_BUDGET_BONUS = 1.0
SHELL_CORE_BUDGET_RESERVED_CORES = 1
SHELL_PERFORMANCE_RESERVE_FRACTION = 0.15
SHELL_PERFORMANCE_RESERVE_MIN_CORES = 1
SHELL_LANE_BONUS = {"probe_and_duration": 1, "loudness_and_bake": 1, "cropdetect_threads": 1}
SHELL_VLC_AVCODEC_THREAD_BONUS = 1
SHELL_WHISPER_THREADS_STANDARD_MAX = 6
SHELL_WHISPER_THREADS_STANDARD_DIVISOR = 1

# VALUES CHANGE HERE (headroom left for the user's other programs: threads the app may use = logical threads - max(minimum, logical x fraction), never below the floor)
HEADROOM_MIN_THREADS = {"lite": 2, "standard": 3, "performance": 3}
HEADROOM_FRACTION = {"lite": 0.50, "standard": 0.25, "performance": 0.25}
AFFORDABLE_THREADS_FLOOR = 2

# VALUES CHANGE HERE (live caption cadence per mode; Standard = the values the captions were tuned on, Lite shares them because Lite has no captions)
CAPTION_CADENCE = {
    "lite": {"first_chunk_sec": 5.0, "first_overlap_sec": 3.0, "startup_chunk_sec": 10.0},
    "standard": {"first_chunk_sec": 5.0, "first_overlap_sec": 3.0, "startup_chunk_sec": 10.0},
    "performance": {"first_chunk_sec": 4.0, "first_overlap_sec": 2.5, "startup_chunk_sec": 8.0},
}

# VALUES CHANGE HERE (DVD player captions for regular video files, in milliseconds: wait for the caption-source lookup, wait for VLC to list a caption track, check interval, keep VLC's own subtitle track off after a burned-in file starts, check interval for that)
DVD_CAPTIONS_LOOKUP_TIMEOUT_MS = 20000
DVD_CAPTIONS_TRACK_TIMEOUT_MS = 8000
DVD_CAPTIONS_POLL_MS = 500
DVD_CAPTIONS_SUPPRESS_WATCH_MS = 20000
DVD_CAPTIONS_SUPPRESS_POLL_MS = 400

# VALUES CHANGE HERE (Loudness Scan toggle, in milliseconds: how long the last picture stays on screen while the rebuilt VLC engine reopens the show)
LOUDNESS_HOLD_LAST_FRAME_MS = 3000

# VALUES CHANGE HERE (Visualizer Equalizer Grid: seconds the column heights take to follow the music level, the top dot fades in and out between whole dots, how fast the peak marker falls per second, longest frame time one step may use in seconds)
EQ_GRID_SMOOTH_SECONDS = 0.12
EQ_GRID_PEAK_FALL_PER_SECOND = 0.42
EQ_GRID_MAX_STEP_SECONDS = 0.1

# VALUES CHANGE HERE (throttle level -> allowance factor; level 4 pauses everything non-priority)
LEVEL_FACTOR = (1.0, 0.75, 0.5, 0.25, 0.0)
MAX_LEVEL = 4

# VALUES CHANGE HERE (scan pace between units of work = base pace / max(floor, LEVEL_FACTOR[level]); 0.25 = at most 4x slower)
PACE_FACTOR_FLOOR = 0.25

# VALUES CHANGE HERE (pressure thresholds, all relative to the frame budget)
BUSY_RATIO_LEVELS = ((1.00, 1), (1.20, 2), (1.50, 3), (2.00, 4))
WORST_BUMP_RATIO = 3.0
SYSTEM_CPU_LEVELS = ((90.0, 1), (96.0, 2))
LOST_PICTURE_LEVELS = ((3, 1), (10, 2))
LOST_AUDIO_IMMEDIATE_LEVEL = 3
DRIVE_DEGRADED_LEVEL = 1

# VALUES CHANGE HERE (timing; tighten fast, relax slowly)
SUSTAIN_SECONDS = 2.0
RELAX_COOLDOWN_SECONDS = 15.0
RELAX_STEP_SECONDS = 5.0
EVENT_WINDOW_SECONDS = 1.5
# VALUES CHANGE HERE (event kinds that do NOT restart the calm timer; the frames inside them are already not counted)
RELAX_NEUTRAL_EVENT_KINDS = ("channel_change", "guide_row_switch", "play_file")

# VALUES CHANGE HERE (saved mode keys and fingerprint bucket)
MODE_CONFIG_KEY = "performance_mode"
MODE_SOURCE_CONFIG_KEY = "performance_mode_source"
MODE_AUTO_CONFIG_KEY = "performance_mode_auto"
MODE_FINGERPRINT_CONFIG_KEY = "performance_hw_fingerprint"
RAM_FINGERPRINT_BUCKET_GB = 4.0

# VALUES CHANGE HERE (signal feed)
BUSY_WINDOW_SECONDS = 2.0
STALL_LOG_MS = 100.0
MIN_BUSY_SAMPLES = 5
FEED_INTERVAL_SECONDS = 1.0
LOSS_WINDOW_SECONDS = 5.0
MEDIA_START_GRACE_SECONDS = 3.0

# VALUES CHANGE HERE (held W/A/S/D key: milliseconds before a hold in a menu or the guide starts repeating)
MENU_NAV_REPEAT_DELAY_MS = 400

# VALUES CHANGE HERE (channel Up/Down: milliseconds you must stay on a channel number before that channel is tuned; presses faster than this only change the number shown on the grey screen)
CHANNEL_SURF_HESITATE_MS = 400

# VALUES CHANGE HERE (typed channel number: milliseconds between the second digit and the tune; the show on air is hidden, muted and shut down the moment the second digit is typed)
CHANNEL_DIGIT_TUNE_DELAY_MS = 350

# VALUES CHANGE HERE (logging)
STATUS_LOG_INTERVAL_SECONDS = 5.0

# VALUES CHANGE HERE (boot load bar: first-boot guesses, replaced by measured times after one boot)
BOOT_BAR_DEFAULT_QUICK_SECONDS = 3.0
BOOT_BAR_DEFAULT_PATHS_SECONDS = 0.5
BOOT_BAR_DEFAULT_READY_SECONDS = 6.0
BOOT_BAR_DEFAULT_PER_TARGET_SECONDS = 0.6
BOOT_BAR_DEFAULT_OTHERS_SECONDS = 6.0
BOOT_BAR_LEARN_BLEND = 0.5
BOOT_BAR_MIN_PHASE_SECONDS = 0.05
BOOT_BAR_MAX_PHASE_SECONDS = 120.0
BOOT_BAR_MAX_BEFORE_DONE = 99.0
BOOT_BAR_CATCHUP_RATE_PER_SECOND = 12.0
BOOT_BAR_FINISH_RATE_PER_SECOND = 60.0
BOOT_BAR_CONFIG_KEY = "_boot_bar_costs"
# VALUES CHANGE HERE (boot stall watchdog: seconds into boot before one [BOOT STALL] report names what is unfinished, 0 = off; seconds before the optional boot warm-up stops holding the loading screen, 0 = never)
BOOT_STALL_REPORT_SECONDS = 8.0
BOOT_PREWARM_MAX_SECONDS = 20.0
# VALUES CHANGE HERE (boot bar weighting and file holds: how much of each file's slice of the bar follows its size, 0 = every file the same slice, 1 = slice follows size only; the most of its slice an unfinished file may fill; the fewest seconds one file may hold the bar before it is skipped (the real limit is the time the whole bar is expected to take); files probed at once; threads reading file sizes; seconds allowed to read one file's size)
BOOT_BAR_FILE_SIZE_WEIGHT = 0.5
BOOT_BAR_INFLIGHT_CAP = 0.9
BOOT_UNIT_HOLD_MIN_SECONDS = 5.0
BOOT_PREWARM_OTHER_WORKERS = 4
BOOT_FILE_SIZE_WORKERS = 8
BOOT_FILE_SIZE_TIMEOUT_SECONDS = 1.5

# VALUES CHANGE HERE (Advanced Settings menu: width of every bar in pixels; text padding inside a bar; gap between a bar and its side pill or button; preferred gap between rows, the smallest gap rows may shrink to when the list is tall, gap under the title, space kept free above the Close button; how fast a too-long label scrolls in pixels per second, the pause in seconds at each end of the scroll, and how many times per second the menu is redrawn while one scrolls)
ADVANCED_MENU_BAR_WIDTH = 360
ADVANCED_MENU_TEXT_PAD = 18
ADVANCED_MENU_SIDE_GAP = 20
ADVANCED_MENU_ROW_GAP = 20
ADVANCED_MENU_ROW_GAP_MIN = 8
ADVANCED_MENU_HEADER_GAP = 40
ADVANCED_MENU_BOTTOM_RESERVED = 70
ADVANCED_MENU_SCROLL_SPEED_PX = 55
ADVANCED_MENU_SCROLL_PAUSE_SECONDS = 1.0
ADVANCED_MENU_SCROLL_STEPS_PER_SECOND = 20

# VALUES CHANGE HERE (Step 8 gates and attribution)
GATE_POLL_SECONDS = 0.25
GATE_MAX_WAIT_SECONDS = 60.0
BOOT_PREWARM_GATE_WAIT_SECONDS = 5.0
WHISPER_GATE_KEEP_LEAD_SECONDS = 10.0
WHISPER_GATE_MAX_HOLD_SECONDS = 30.0
WHISPER_PREFETCH_GATE_WAIT_SECONDS = 30.0
WHISPER_UNLOAD_WAIT_SECONDS = 20.0
GAME_FRAME_TIMING_LOG_SECONDS = 5.0
GAME_FRAME_TIMING_EARLY_SECONDS = 20.0
GAME_FRAME_TIMING_EARLY_LOG_SECONDS = 1.0
GAME_PAD_POLL_HZ = 120.0
GAME_PAD_SNAPSHOT_MAX_AGE_SECONDS = 0.25
GAME_PAD_WARMUP_MAX_WAIT_SECONDS = 3.0
GAME_PAD_POLL_IDLE_STOP_SECONDS = 2.0
ACTIVITY_LOOKBACK_SECONDS = 3.0
ATTRIBUTION_LOG_INTERVAL_SECONDS = 30.0
GATE_LANE_UNIT = {
    "probe_and_duration": "header_probe",
    "loudness_and_bake": "loudness",
    "music_loudness": "loudness",
    "cropdetect_threads": "cropdetect",
}


# VALUES CHANGE HERE (boot hardware scan)
BOOT_SCAN_INTEL_GEN_CLASS = ((7, "A"), (11, "B"), (99, "C"))
BOOT_SCAN_AMD_SERIES_CLASS = ((2, "A"), (6, "B"), (99, "C"))
BOOT_SCAN_BORDERLINE = (("intel", 11), ("amd", 5))
BOOT_SCAN_CLASS_START_MODE = {"A": "lite", "B": "standard", "C": "standard"}
BOOT_SCAN_TOP_MIN_PHYSICAL = 8
BOOT_SCAN_BORDERLINE_PROMOTE_MIN_PHYSICAL = 6
BOOT_SCAN_PERFORMANCE_SCAN_TIERS = ("REFERENCE", "STRONG")
BOOT_SCAN_PERFORMANCE_SCAN_MIN_EFFECTIVE = 6
BOOT_SCAN_LOW_CORES_BELOW = 4
BOOT_SCAN_LOW_RAM_GB = 8.0
BOOT_SCAN_FALLBACK_CLASS = "C"
BOOT_SCAN_EFFECTS_MAX_MODE = "standard"
BOOT_SCAN_FINGERPRINT_BUCKET_GB = 4.0


# VALUES CHANGE HERE (GPU frame path: adapter choice "monitor" or "fastest"; start-up self-check picture size, per-channel tolerance out of 255 and the share of values allowed past it; failures in a row before the GPU path is switched off for the run; device losses allowed per run; seconds to wait before reopening after a failure; frames of one path collected before a [GPU FRAME] log line is written)
GPU_ADAPTER_POLICY = "monitor"
GPU_SELF_CHECK_SIZE = (640, 360)
GPU_SELF_CHECK_TOLERANCE = 2
GPU_SELF_CHECK_MAX_OVER_FRACTION = 0.001
GPU_FRAME_FAILURE_LIMIT = 3
GPU_DEVICE_LOST_LIMIT = 3
GPU_RETRY_COOLDOWN_SECONDS = 30.0
GPU_FRAME_LOG_INTERVAL_FRAMES = 600

# VALUES CHANGE HERE (menu key trace: key events landing in one frame that write a [MENU NAV BURST] warning; loop profile: frames collected before a [LOOP PROFILE] line is written, 0 = off)
MENU_NAV_BURST_WARN_EVENTS = 3
LOOP_PROFILE_LOG_INTERVAL_FRAMES = 600

# VALUES CHANGE HERE (guide past rebuild: most recent missed block occurrences replayed per channel at a block change; length in seconds guessed for an unscanned episode with no scanned episode of its series or channel to average)
GUIDE_REPLAY_MAX_BLOCKS = 60
GUIDE_REPLAY_EPISODE_GUESS_SECONDS = 1800

# VALUES CHANGE HERE (scheduler catch-up: most recent missed blocks replayed per channel at a block change; most cycles replayed inside one missed block)
CATCH_UP_MAX_BLOCKS = 3000
CATCH_UP_MAX_CYCLES_PER_BLOCK = 1000


# ============================================================================
# SECTION: Mode table
# ============================================================================

# VALUES CHANGE HERE (locked_config_keys = settings greyed out and switched off in that mode; menu_fps = menu/list frame-rate cap, 0 = none)
MODE_TABLE = {
    "lite": {
        "label": "Lite",
        "active": True,
        "locked_config_keys": ("warp_screen_enabled", "fake_43_scanlines_enabled",
                               "tv_captions_enabled", "dvd_captions_enabled"),
        "menu_fps": 30.0,
        "extra_rules": None,
    },
    "standard": {
        "label": "Standard",
        "active": True,
        "locked_config_keys": (),
        "menu_fps": 0.0,
        "extra_rules": None,
    },
    "performance": {
        "label": "Performance",
        "active": True,
        "locked_config_keys": (),
        "menu_fps": 0.0,
        # COMING SOON - Performance rules go here (what to boost)
        "extra_rules": None,
    },
}


def normalize_mode(value):
    text = str(value or "").strip().lower()
    if text in MODE_TABLE and MODE_TABLE[text]["active"]:
        return text
    return ACTIVE_MODE_DEFAULT


def feature_locked(config_key, mode=None):
    """True when `config_key` is greyed out and switched off in the given (default: current) mode."""
    return config_key in MODE_TABLE[_resolve_mode(mode)].get("locked_config_keys", ())


def menu_fps(mode=None):
    """Frame-rate cap for menus and list screens in the given (default: current) mode; 0 means no cap."""
    return float(MODE_TABLE[_resolve_mode(mode)].get("menu_fps", 0.0) or 0.0)


def _resolve_mode(mode):
    text = str(mode or "").strip().lower()
    if text in MODE_TABLE:
        return text
    return _state["mode"]


# ============================================================================
# SECTION: Machine profile
# ============================================================================


@dataclass
class MachineProfile:
    physical_cores: int = 1
    efficiency_cores: int = 0
    logical_cores: int = 1
    effective_physical: int = 1
    ram_total_gb: float = 0.0
    ram_avail_gb: float = 0.0
    cpu_source: str = "estimate"
    hybrid: bool = False
    gpu_tier: str = "pending"
    gpu_names: tuple = field(default_factory=tuple)
    tier: str = "LOW"
    cpu_name: str = ""
    cpu_class: str = ""
    frame_budget_ms: float = 1000.0 / TARGET_FPS


class _GroupAffinity(ctypes.Structure):
    _fields_ = [("Mask", ctypes.c_size_t), ("Group", ctypes.c_ushort), ("Reserved", ctypes.c_ushort * 3)]


class _ProcessorRelationship(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ubyte), ("EfficiencyClass", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte * 20), ("GroupCount", ctypes.c_ushort),
                ("GroupMask", _GroupAffinity * 1)]


class _LogicalProcessorInfoEx(ctypes.Structure):
    _fields_ = [("Relationship", ctypes.c_ulong), ("Size", ctypes.c_ulong),
                ("Processor", _ProcessorRelationship)]


_RELATION_PROCESSOR_CORE = 0
_ERROR_INSUFFICIENT_BUFFER = 122


def _parse_core_records(buf, length):
    """Return a list of (efficiency_class, logical_processors) for each physical core in the buffer."""
    cores = []
    offset = 0
    mask_off = _LogicalProcessorInfoEx.Processor.offset + _ProcessorRelationship.GroupMask.offset
    mask_size = ctypes.sizeof(_GroupAffinity)
    header = _LogicalProcessorInfoEx.Processor.offset
    while offset + header <= length:
        rec = _LogicalProcessorInfoEx.from_buffer(buf, offset) if offset + ctypes.sizeof(_LogicalProcessorInfoEx) <= len(buf) else None
        if rec is None:
            break
        size = int(rec.Size)
        if size <= 0 or offset + size > length:
            break
        if rec.Relationship == _RELATION_PROCESSOR_CORE:
            logical = 0
            for i in range(int(rec.Processor.GroupCount)):
                at = offset + mask_off + i * mask_size
                if at + mask_size > offset + size:
                    break
                logical += bin(_GroupAffinity.from_buffer(buf, at).Mask).count("1")
            cores.append((int(rec.Processor.EfficiencyClass), logical))
        offset += size
    return cores


def _read_cpu_winapi():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel32.GetLogicalProcessorInformationEx
    fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    fn.restype = ctypes.c_int
    length = ctypes.c_ulong(0)
    fn(_RELATION_PROCESSOR_CORE, None, ctypes.byref(length))
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or length.value == 0:
        return None
    buf = ctypes.create_string_buffer(length.value + ctypes.sizeof(_LogicalProcessorInfoEx))
    if not fn(_RELATION_PROCESSOR_CORE, buf, ctypes.byref(length)):
        return None
    return _parse_core_records(buf, length.value)


def _summarize_cores(cores):
    if not cores:
        return None
    top = max(cls for cls, _ in cores)
    efficiency = sum(1 for cls, _ in cores if cls < top)
    logical = sum(n for _, n in cores)
    if logical <= 0 or len(cores) > logical:
        return None
    return len(cores), efficiency, logical


def _read_cpu_counts():
    logical_os = os.cpu_count() or 1
    if os.name == "nt":
        try:
            summary = _summarize_cores(_read_cpu_winapi())
            if summary is not None:
                physical, efficiency, logical = summary
                return physical, efficiency, max(logical, 1), "winapi"
        except Exception as err:
            log.warning("[GOVERNOR] core count API failed, estimating: %s", err)
    return max(1, logical_os // 2), 0, logical_os, "estimate"


def _read_ram_gb():
    if os.name == "nt":
        try:
            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / 1024 ** 3, status.ullAvailPhys / 1024 ** 3
        except Exception:
            pass
    else:
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            return (os.sysconf("SC_PHYS_PAGES") * page / 1024 ** 3,
                    os.sysconf("SC_AVPHYS_PAGES") * page / 1024 ** 3)
        except Exception:
            pass
    return 0.0, 0.0


def classify_tier(effective_physical, logical, ram_total_gb, cpu_class=None):
    """One tier label (WEAK / LOW / REFERENCE / STRONG) from the boot scan's CPU class plus cores, threads and RAM."""
    ram_known = ram_total_gb > 0.0
    if (effective_physical <= TIER_WEAK_MAX_PHYSICAL or logical <= TIER_WEAK_MAX_LOGICAL
            or (ram_known and ram_total_gb < TIER_WEAK_MIN_RAM_GB)):
        return "WEAK"
    low_min, reference_min, strong_min = TIER_MIN_EFFECTIVE_BY_CLASS.get(
        cpu_class or BOOT_SCAN_FALLBACK_CLASS, TIER_MIN_EFFECTIVE_BY_CLASS[BOOT_SCAN_FALLBACK_CLASS])
    if strong_min is not None and effective_physical >= strong_min:
        return "STRONG"
    if reference_min is not None and effective_physical >= reference_min:
        return "REFERENCE"
    if low_min is not None and effective_physical >= low_min:
        return "LOW"
    return "WEAK"


def build_profile(physical, efficiency, logical, ram_total_gb, ram_avail_gb, cpu_source="winapi", cpu_name=None):
    performance = max(physical - efficiency, 0)
    effective = max(1, int(performance + efficiency * EFFICIENCY_CORE_WEIGHT))
    cpu_name = _read_cpu_name() if cpu_name is None else cpu_name
    rating = rate_cpu(cpu_name, physical, ram_total_gb)
    return MachineProfile(
        physical_cores=physical, efficiency_cores=efficiency, logical_cores=logical,
        effective_physical=effective, ram_total_gb=ram_total_gb, ram_avail_gb=ram_avail_gb,
        cpu_source=cpu_source, hybrid=efficiency > 0, cpu_name=cpu_name, cpu_class=rating["class"],
        tier=classify_tier(effective, logical, ram_total_gb, rating["class"]))


# ============================================================================
# SECTION: Core budget, ceilings and admission
# ============================================================================


_shell = {"active": False}


def set_shell_mode(active):
    """Tells the governor whether this program is running as the Windows shell (--as-shell). Call it once at startup, before start()."""
    _shell["active"] = bool(active)
    log.info("[GOVERNOR] shell mode %s", "ON: Standard and Performance may use more cores and threads" if _shell["active"] else "off")
    return _shell["active"]


def is_shell_mode():
    return _shell["active"]


def _shell_boost(mode, profile=None):
    """True when shell mode's extra headroom applies: shell mode is on, the mode is Standard or Performance, and the PC is not WEAK."""
    if not _shell["active"] or mode not in SHELL_HEADROOM_MIN_THREADS:
        return False
    profile = profile or get_profile()
    return profile.tier != "WEAK"


def _performance_reserve_cores(effective_physical, shell=False):
    if shell:
        return max(SHELL_PERFORMANCE_RESERVE_MIN_CORES, int(-(-effective_physical * SHELL_PERFORMANCE_RESERVE_FRACTION // 1)))
    return max(PERFORMANCE_RESERVE_MIN_CORES, int(-(-effective_physical * PERFORMANCE_RESERVE_FRACTION // 1)))


def affordable_threads(profile=None, mode=None):
    """Threads the app may use in the given (default: current) mode, leaving headroom for the user's other programs."""
    profile = profile or get_profile()
    mode = _resolve_mode(mode)
    logical = max(1, int(profile.logical_cores))
    if _shell_boost(mode, profile):
        reserve = max(SHELL_HEADROOM_MIN_THREADS[mode], int(-(-logical * SHELL_HEADROOM_FRACTION[mode] // 1)))
    else:
        reserve = max(HEADROOM_MIN_THREADS[mode], int(-(-logical * HEADROOM_FRACTION[mode] // 1)))
    return max(AFFORDABLE_THREADS_FLOOR, logical - reserve)


def core_budget(profile, mode=None):
    mode = _limit_mode("scan", _resolve_mode(mode))
    if profile.tier == "WEAK":
        return CORE_BUDGET_MIN
    if profile.tier == "STRONG":
        base = STRONG_BUDGET_HIGH if profile.effective_physical >= STRONG_HIGH_AT_PHYSICAL else CORE_BUDGET_BY_TIER["STRONG"]
    else:
        base = CORE_BUDGET_BY_TIER[profile.tier]
    shell = _shell_boost(mode, profile)
    reserved = SHELL_CORE_BUDGET_RESERVED_CORES if shell else CORE_BUDGET_RESERVED_CORES
    if shell:
        base += SHELL_CORE_BUDGET_BONUS
    budget = min(base, max(CORE_BUDGET_MIN, float(profile.effective_physical - reserved)))
    budget *= MODE_BUDGET_MULTIPLIER[mode]
    if mode == "performance":
        budget = min(budget, float(profile.effective_physical - _performance_reserve_cores(profile.effective_physical, shell)))
    return max(CORE_BUDGET_MIN, budget)


def whisper_threads(profile, mode=None):
    mode = _limit_mode("caption", _resolve_mode(mode))
    p = profile.effective_physical
    shell = _shell_boost(mode, profile)
    if mode == "lite":
        return WHISPER_THREADS_LITE
    if shell:
        shell_standard = min(SHELL_WHISPER_THREADS_STANDARD_MAX, affordable_threads(profile, mode),
                             max(WHISPER_THREADS_STANDARD_MIN, p // SHELL_WHISPER_THREADS_STANDARD_DIVISOR))
    if mode == "performance":
        value = min(WHISPER_THREADS_PERFORMANCE_MAX, affordable_threads(profile, mode),
                    max(WHISPER_THREADS_PERFORMANCE_MIN, p - _performance_reserve_cores(p, shell)))
        # WARNING: in shell mode Performance must never end up below Standard.
        return max(value, shell_standard) if shell else value
    if shell:
        return shell_standard
    return min(WHISPER_THREADS_STANDARD_MAX,
               max(WHISPER_THREADS_STANDARD_MIN, p // WHISPER_THREADS_STANDARD_DIVISOR))


def caption_cadence(mode=None):
    """First-scan chunk sizes for live captions in the given (default: current) mode, limited by what this PC can do."""
    return dict(CAPTION_CADENCE[_limit_mode("caption", _resolve_mode(mode))])


def cv2_threads(profile=None, mode=None):
    """OpenCV thread count for the given (default: current) mode."""
    return affordable_threads(profile, mode)


def apply_cv2_threads(mode=None):
    """Sets OpenCV's process-wide thread count; returns the count applied, or None when OpenCV is not available."""
    try:
        import cv2
        count = int(cv2_threads(None, mode))
        cv2.setNumThreads(count)
        return count
    except Exception:
        return None


def vlc_decoder_threads(profile=None):
    """Video decoder thread count for the VLC engine, chosen from the hardware tier."""
    profile = profile or get_profile()
    value = int(VLC_AVCODEC_THREADS_BY_TIER.get(profile.tier, VLC_AVCODEC_THREADS_DEFAULT))
    if _shell_boost(_state["mode"], profile):
        value += SHELL_VLC_AVCODEC_THREAD_BONUS
    return value


def lane_ceiling(lane, mode=None):
    mode = _limit_mode("caption" if lane == "whisper_slots" else "scan", _resolve_mode(mode))
    value = CEILINGS[lane][mode]
    if value is None:
        value = CEILINGS["loudness_and_bake"][mode]
        lane = "loudness_and_bake"
    profile = get_profile()
    shell = _shell_boost(mode, profile)
    if mode == "performance":
        if profile.tier == "STRONG":
            value += PERFORMANCE_STRONG_LANE_BONUS.get(lane, 0)
    if shell:
        value += SHELL_LANE_BONUS.get(lane, 0)
    if mode == "performance" or shell:
        value = min(value, affordable_threads(profile, mode))
    return max(1, value)


def effective_factor(level, profile, mode=None):
    mode = _resolve_mode(mode)
    level = max(0, min(MAX_LEVEL, int(level)))
    return min(LEVEL_FACTOR[level], MODE_FACTOR_CAP[mode], TIER_FACTOR_CAP[profile.tier])


def lane_slots(lane, level, profile, mode=None):
    factor = effective_factor(level, profile, mode)
    if factor <= 0.0:
        return 0
    return max(1, int(lane_ceiling(lane, mode) * factor))


def duration_workers(playing=False, mode=None):
    """Returns how many duration probe threads one batch may run: the probe lane's slots at the current load level, divided while a video plays, never below 1."""
    level = current_level() if enforcing() else 0
    slots = lane_slots("probe_and_duration", level, get_profile(), mode)
    if playing:
        slots //= DURATION_WORKERS_WHILE_PLAYING_DIVISOR
    return max(1, slots)


def loudness_dispatch_workers(spare=False, cap=None, mode=None):
    """Returns how many loudness probes one round may run: the base or spare count, held to the loudness lane's ceiling and to cap when given, never below 1."""
    value = LOUDNESS_DISPATCH_WORKERS_SPARE if spare else LOUDNESS_DISPATCH_WORKERS_BASE
    value = min(value, lane_ceiling("loudness_and_bake", mode))
    if cap is not None:
        value = min(value, int(cap))
    return max(1, value)


def level_allows(level, kind):
    if level <= 2:
        return True
    if level == 3:
        return kind in ("priority", "wave1")
    return kind == "priority"


# ============================================================================
# SECTION: GPU gate values
# ============================================================================


def gpu_gate_values():
    """Returns the GPU gate tunables as keyword arguments for GateSettings in d3d11_gpu_gate.py."""
    return {
        "adapter_policy": str(GPU_ADAPTER_POLICY),
        "check_width": int(GPU_SELF_CHECK_SIZE[0]),
        "check_height": int(GPU_SELF_CHECK_SIZE[1]),
        "tolerance": int(GPU_SELF_CHECK_TOLERANCE),
        "max_over_fraction": float(GPU_SELF_CHECK_MAX_OVER_FRACTION),
        "failure_limit": int(GPU_FRAME_FAILURE_LIMIT),
        "lost_limit": int(GPU_DEVICE_LOST_LIMIT),
        "retry_cooldown_s": float(GPU_RETRY_COOLDOWN_SECONDS),
    }


# ============================================================================
# SECTION: Stick deadzone values
# ============================================================================
# VALUES CHANGE HERE
STICK_DZ_ENABLED_KEY = "stick_deadzone_enabled"
STICK_DZ_ENABLED_DEFAULT = True
STICK_DZ_SPECS = {
    "deadzone": ("stick_deadzone_percent", 0, 0, 95, 1),
    "sensitivity": ("stick_sensitivity_percent", 100, 10, 200, 5),
    "axis_threshold": ("stick_axis_threshold_percent", 50, 5, 95, 1),
}
STICK_DZ_DPAD_RELEASE_FRACTION = 0.75
STICK_DZ_OFF_DPAD_THRESHOLD = 0
STICK_DZ_OFF_DINPUT_THRESHOLD = 0
STICK_DZ_TEST_HOLD_SECONDS = 0.25
STICK_DZ_TEST_RADIUS = 50

# VALUES CHANGE HERE (stick activity numbers: how far a stick must move, out of 32767, to wake the screensaver while a game plays; the same for the multitap controller-activity check; how far a joystick axis must move, 0 to 1, to wake the Channel 03 screensaver; how far a joystick axis must move, 0 to 1, to press a menu direction)
STICK_SS_ACTIVITY_DEADZONE = 6000
STICK_MULTITAP_ACTIVITY_DEADZONE = 6000
STICK_CH03_AXIS_ACTIVITY_DEADZONE = 8000 / 32767.0
STICK_MENU_AXIS_THRESHOLD = 0.6


# ============================================================================
# SECTION: Controller read values
# ============================================================================
# VALUES CHANGE HERE (trigger travel, 0 to 255, that counts as a button press in the Controller Map and the DVD button map; trigger travel for the in-game hotkey; milliseconds before that hotkey can fire again; how far, in raw DirectInput units, an axis must move to show a pad is alive)
TRIGGER_PRESS_THRESHOLD = 30
GAME_HOTKEY_TRIGGER_THRESHOLD = 128
GAME_HOTKEY_COOLDOWN_MS = 2000
DINPUT_AXIS_LIVENESS_THRESHOLD = 1500

# VALUES CHANGE HERE (seconds a pad identity scan is reused; seconds a DirectInput read is reused within one frame; seconds a Linux joystick may stay silent before it is dropped; menu pad polls per second; seconds between polls with no pad; polls with no pad before the slow rate)
HID_IDENTITY_CACHE_TTL_SECONDS = 2.0
DINPUT_FRAME_CACHE_WINDOW_SECONDS = 0.008
JS_DEAD_TIMEOUT_SECONDS = 5.0
XI_NAV_POLL_HZ = 30
XI_NAV_NO_PAD_INTERVAL_SECONDS = 0.25
XI_NAV_MISSES_BEFORE_SLOW = 30


# ============================================================================
# SECTION: Broadcast clock values
# ============================================================================
# VALUES CHANGE HERE (seconds between clock resyncs; size of a clock step that forces a resync; size of a backward jitter the clock absorbs)
BROADCAST_CLOCK_RESYNC_SECONDS = 60.0
BROADCAST_CLOCK_STEP_TOLERANCE_SECONDS = 2.0
BROADCAST_CLOCK_JITTER_TOLERANCE_SECONDS = 0.25


# ============================================================================
# SECTION: Video decode canvas
# ============================================================================
# VALUES CHANGE HERE
VIDEO_DECODE_CANVAS_SD = (854, 480)
VIDEO_DECODE_CANVAS_HD = (1280, 720)


# ============================================================================
# SECTION: Picture shapes
# ============================================================================
# VALUES CHANGE HERE
PICTURE_STANDARD_ASPECTS = (16.0 / 9.0, 4.0 / 3.0, 9.0 / 16.0)


# ============================================================================
# SECTION: Game picture shapes
# ============================================================================
# VALUES CHANGE HERE (the shape a console's picture is laid out as in Normal, width divided by height; a console not listed keeps the shape of its own frame)
GAME_DISPLAY_ASPECTS = {"NES": 4.0 / 3.0, "SNES": 4.0 / 3.0}


# ============================================================================
# SECTION: Picture box before the show
# ============================================================================
# VALUES CHANGE HERE
PICTURE_BOX_PRE_SHOW_BUDGET_S = 8.0
PICTURE_BOX_PRE_SHOW_PARALLEL = 6


# ============================================================================
# SECTION: Caption memory check
# ============================================================================
# VALUES CHANGE HERE (free RAM in GB the caption model needs before it may load; seconds the red "no memory" banner stays up)
CAPTION_MODEL_MIN_AVAILABLE_RAM_GB = 1.5
CAPTION_MEMORY_BANNER_SECONDS = 5.0


def caption_memory_check():
    """Returns (ok, total_gb, available_gb, needed_gb) for loading the caption model now; ok is True when memory cannot be read, so an unknown machine is never blocked."""
    total_gb, avail_gb = _read_ram_gb()
    needed_gb = float(CAPTION_MODEL_MIN_AVAILABLE_RAM_GB)
    if total_gb <= 0.0 or avail_gb <= 0.0:
        return True, total_gb, avail_gb, needed_gb
    return avail_gb >= needed_gb, total_gb, avail_gb, needed_gb


# ============================================================================
# SECTION: Smart throttle
# ============================================================================


class Throttle:
    def __init__(self, frame_budget_ms, name="main"):
        self.name = name
        self.frame_budget_ms = float(frame_budget_ms)
        self.level = 0
        self._over_since = None
        self._last_hold = 0.0
        self._last_change = 0.0
        self._event_until = 0.0
        self._event_kind = ""
        self._event_logged = True
        self._last_status_log = 0.0
        self.last_reason = "start"
        self._unattributed_target = 0
        self._unattributed_reason = ""
        self._last_unattributed_log = 0.0
        self._lock = threading.Lock()

    def note_event(self, kind, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._event_until = now + EVENT_WINDOW_SECONDS
            self._event_kind = str(kind)
            self._event_logged = False
            self._over_since = None

    def in_event(self, now=None):
        now = time.monotonic() if now is None else now
        return now < self._event_until

    def event_kind(self):
        return self._event_kind

    def _target(self, signals):
        budget = self.frame_budget_ms
        target = 0
        reasons = []
        immediate = False
        avg = signals.get("busy_avg_ms")
        worst = signals.get("busy_worst_ms")
        if avg is not None and budget > 0:
            ratio = avg / budget
            lvl = 0
            for threshold, level in BUSY_RATIO_LEVELS:
                if ratio >= threshold:
                    lvl = level
            if lvl and worst is not None and worst / budget >= WORST_BUMP_RATIO:
                lvl = min(MAX_LEVEL, lvl + 1)
            if lvl:
                reasons.append("busy_avg=%.1fms(%d%%)" % (avg, round(ratio * 100)))
            target = max(target, lvl)
        cpu = signals.get("system_cpu_pct")
        if cpu is not None:
            lvl = 0
            for threshold, level in SYSTEM_CPU_LEVELS:
                if cpu >= threshold:
                    lvl = level
            if lvl:
                reasons.append("system_cpu=%.0f%%" % cpu)
            target = max(target, lvl)
        pictures = signals.get("lost_pictures")
        if pictures is not None:
            lvl = 0
            for threshold, level in LOST_PICTURE_LEVELS:
                if pictures >= threshold:
                    lvl = level
            if lvl:
                reasons.append("lost_pictures=%d" % pictures)
            target = max(target, lvl)
        audio = signals.get("lost_audio")
        if audio:
            reasons.append("lost_audio=%d" % audio)
            target = max(target, LOST_AUDIO_IMMEDIATE_LEVEL)
            immediate = True
        if signals.get("drive_degraded"):
            reasons.append("drive_degraded")
            target = max(target, DRIVE_DEGRADED_LEVEL)
        if target > 0 and signals.get("background_active") is False:
            self._unattributed_target = target
            self._unattributed_reason = ", ".join(reasons)
            return 0, "not attributable (no background work running): " + ", ".join(reasons), False
        self._unattributed_target = 0
        lanes = signals.get("background_lanes")
        if target > 0 and lanes:
            reasons.append("bg=" + lanes)
        return target, ", ".join(reasons) or "calm", immediate

    def update(self, signals, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            target, reason, immediate = self._target(signals)
            if self._unattributed_target > self.level and now - self._last_unattributed_log >= ATTRIBUTION_LOG_INTERVAL_SECONDS:
                self._last_unattributed_log = now
                log.info("[GOVERNOR] pressure NOT attributable to background work (none running): would_level=%d (%s)",
                         self._unattributed_target, self._unattributed_reason)
            if now < self._event_until:
                if target > self.level and not self._event_logged:
                    self._event_logged = True
                    log.info("[GOVERNOR] pressure at event start NOT counted kind=%s would_level=%d (signals from before the event: %s)%s",
                             self._event_kind, target, reason, " (shadow)" if SHADOW_MODE else "")
                self._over_since = None
                if self._event_kind not in RELAX_NEUTRAL_EVENT_KINDS:
                    self._last_hold = now
                return self.level
            if target >= self.level:
                self._last_hold = now
            if target > self.level:
                if self._over_since is None:
                    self._over_since = now
                if immediate or now - self._over_since >= SUSTAIN_SECONDS:
                    self._set_level(target, "tighten: " + reason, now)
                    self._over_since = None
            else:
                self._over_since = None
                if (target < self.level and now - self._last_hold >= RELAX_COOLDOWN_SECONDS
                        and now - self._last_change >= RELAX_STEP_SECONDS):
                    self._set_level(self.level - 1, "relax: calm for %.0fs" % (now - self._last_hold), now)
            return self.level

    def _set_level(self, new_level, reason, now):
        old = self.level
        self.level = max(0, min(MAX_LEVEL, new_level))
        self._last_change = now
        if self.level > old:
            self._last_hold = now
        self.last_reason = reason
        if self.level != old:
            log.info("[GOVERNOR] level %d->%d reason=%s%s", old, self.level, reason,
                     " (shadow)" if SHADOW_MODE else "")

    def factor(self, profile, mode=None):
        return effective_factor(self.level, profile, mode)

    def status_due(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self._last_status_log >= STATUS_LOG_INTERVAL_SECONDS:
            self._last_status_log = now
            return True
        return False


# ============================================================================
# SECTION: Signal helpers (pure; the caller reads the real sources and passes values in)
# ============================================================================


class BusyWindow:
    def __init__(self, window_seconds=BUSY_WINDOW_SECONDS):
        self.window_seconds = float(window_seconds)
        self._samples = collections.deque()

    def add(self, busy_ms, now):
        self._samples.append((now, float(busy_ms)))
        self._trim(now)

    def _trim(self, now):
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def stats(self, now):
        self._trim(now)
        if not self._samples:
            return None, None, 0
        values = [v for _, v in self._samples]
        return sum(values) / len(values), max(values), len(values)


class LossTracker:
    def __init__(self, window_seconds=LOSS_WINDOW_SECONDS, grace_seconds=MEDIA_START_GRACE_SECONDS):
        self.window_seconds = float(window_seconds)
        self.grace_seconds = float(grace_seconds)
        self._key = None
        self._last_pictures = 0
        self._last_audio = 0
        self._grace_until = 0.0
        self._events = collections.deque()

    def update(self, media_key, pictures, audio, now, in_event=False):
        if media_key != self._key:
            self._key = media_key
            self._last_pictures = int(pictures)
            self._last_audio = int(audio)
            self._grace_until = now + self.grace_seconds
            self._events.clear()
            return 0, 0
        d_pictures = int(pictures) - self._last_pictures
        d_audio = int(audio) - self._last_audio
        self._last_pictures = int(pictures)
        self._last_audio = int(audio)
        if d_pictures < 0 or d_audio < 0:
            self._events.clear()
            return 0, 0
        if not in_event and now >= self._grace_until and (d_pictures or d_audio):
            self._events.append((now, d_pictures, d_audio))
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        return sum(e[1] for e in self._events), sum(e[2] for e in self._events)

    def reset(self):
        self._key = None
        self._events.clear()


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def _read_system_times_winapi():
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None

    def _value(ft):
        return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

    return _value(idle), _value(kernel), _value(user)


def _read_system_times_proc():
    with open("/proc/stat", "r", encoding="ascii") as handle:
        fields = handle.readline().split()[1:]
    values = [int(x) for x in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values[:8])
    return idle, total, 0


class SystemCpuSampler:
    def __init__(self, reader=None):
        if reader is None:
            reader = _read_system_times_winapi if os.name == "nt" else _read_system_times_proc
        self._reader = reader
        self._last = None

    def percent(self):
        try:
            times = self._reader()
        except Exception:
            return None
        if times is None:
            return None
        previous, self._last = self._last, times
        if previous is None:
            return None
        d_idle = times[0] - previous[0]
        d_total = (times[1] - previous[1]) + (times[2] - previous[2])
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, (d_total - d_idle) * 100.0 / d_total))


def format_status(throttle, profile, signals, mode=None):
    def _fmt(key, template):
        value = signals.get(key)
        return template % value if value is not None else "n/a"

    avg = signals.get("busy_avg_ms")
    ratio = ("%d%%" % round(avg * 100.0 / profile.frame_budget_ms)) if avg is not None else "n/a"
    return ("[GOVERNOR] status ch=%s level=%d factor=%.2f busy_avg=%s(%s) busy_worst=%s flip_avg=%s cpu=%s "
            "lost_pic=%s lost_aud=%s drive_degraded=%s event=%s bg=%s mode=%s%s" % (
                signals.get("channel", "n/a"), throttle.level, throttle.factor(profile, mode),
                _fmt("busy_avg_ms", "%.1fms"), ratio, _fmt("busy_worst_ms", "%.0fms"),
                _fmt("flip_avg_ms", "%.1fms"), _fmt("system_cpu_pct", "%.0f%%"),
                _fmt("lost_pictures", "%d"), _fmt("lost_audio", "%d"),
                "yes" if signals.get("drive_degraded") else "no",
                "yes" if throttle.in_event() else "no", signals.get("background_lanes") or "none",
                mode or get_mode(), " (shadow)" if SHADOW_MODE else ""))


# ============================================================================
# SECTION: Activity tracking and gates (Step 8)
# ============================================================================


class ActivityTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = collections.defaultdict(int)
        self._last_end = {}

    def begin(self, lane):
        with self._lock:
            self._inflight[lane] += 1

    def end(self, lane, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._inflight[lane] = max(0, self._inflight[lane] - 1)
            self._last_end[lane] = now

    def inflight(self, lane):
        with self._lock:
            return self._inflight[lane]

    def try_begin(self, lane, limit):
        """Atomic check-and-start so two jobs cannot both take the last slot."""
        with self._lock:
            if self._inflight[lane] >= limit:
                return False
            self._inflight[lane] += 1
            return True

    def active_lanes(self, now=None, lookback=None):
        now = time.monotonic() if now is None else now
        lookback = ACTIVITY_LOOKBACK_SECONDS if lookback is None else lookback
        with self._lock:
            lanes = [l for l, n in self._inflight.items() if n > 0]
            lanes += [l for l, t in self._last_end.items()
                      if now - t <= lookback and l not in lanes]
        return sorted(lanes)


_activity = ActivityTracker()
_gate_tls = threading.local()
_gate_state = {"throttle": None, "enabled": True, "waits": 0, "starved": 0, "last_wait_log": 0.0}


def register_throttle(throttle):
    _gate_state["throttle"] = throttle


def set_enabled(value):
    _gate_state["enabled"] = bool(value)
    log.info("[GOVERNOR] gates %s (config perf_governor_enabled)", "ENABLED" if _gate_state["enabled"] else "DISABLED")


def enforcing():
    return (not SHADOW_MODE) and _gate_state["enabled"] and _gate_state["throttle"] is not None


def current_level():
    t = _gate_state["throttle"]
    return t.level if t is not None else 0


def background_active(now=None):
    """(active, 'lane,lane') for the throttle's attribution check."""
    lanes = _activity.active_lanes(now)
    return bool(lanes), ",".join(lanes)


@contextlib.contextmanager
def priority_scope():
    """Work started inside this scope is first-scan / playing-file work and is never held back."""
    _gate_tls.depth = getattr(_gate_tls, "depth", 0) + 1
    try:
        yield
    finally:
        _gate_tls.depth -= 1


def _is_priority_context():
    # WARNING: the UI thread must never wait on a gate; it must count as priority.
    return getattr(_gate_tls, "depth", 0) > 0 or threading.current_thread() is threading.main_thread()


@contextlib.contextmanager
def activity(lane):
    """Report work that is not gated (Whisper) so attribution can see it."""
    _activity.begin(lane)
    try:
        yield
    finally:
        _activity.end(lane)


def _lane_limit(lane, level):
    if lane not in CEILINGS:
        return 1 << 30
    return lane_slots(lane, level, get_profile())


@contextlib.contextmanager
def gate(lane, priority=False, timeout=None, label=""):
    """Context manager around one unit of background work; yields True when admitted, False when the caller's timeout expired."""
    held = getattr(_gate_tls, "held", None)
    if held is None:
        held = _gate_tls.held = set()
    if lane in held:
        yield True
        return
    priority = bool(priority) or _is_priority_context()
    admitted = False
    if priority or not enforcing():
        _activity.begin(lane)
        admitted = True
    else:
        start = time.monotonic()
        wait_cap = GATE_MAX_WAIT_SECONDS if timeout is None else timeout
        deadline = start + wait_cap
        waited = False
        while True:
            level = current_level()
            if level_allows(level, "background") and _activity.try_begin(lane, _lane_limit(lane, level)):
                admitted = True
                break
            waited = True
            if time.monotonic() >= deadline:
                if timeout is not None:
                    _gate_state["waits"] += 1
                    break
                _gate_state["starved"] += 1
                _activity.begin(lane)
                admitted = True
                log.info("[GOVERNOR] gate wait cap %.0fs reached, running anyway: lane=%s %s level=%d",
                         GATE_MAX_WAIT_SECONDS, lane, label, level)
                break
            time.sleep(GATE_POLL_SECONDS)
        if waited and admitted:
            now = time.monotonic()
            if now - _gate_state["last_wait_log"] >= 5.0:
                _gate_state["last_wait_log"] = now
                log.info("[GOVERNOR] gate held back lane=%s %s for %.1fs (level=%d)", lane, label,
                         now - start, current_level())
    if admitted:
        held.add(lane)
    try:
        yield admitted
    finally:
        if admitted:
            held.discard(lane)
            _activity.end(lane)


# ============================================================================
# SECTION: Boot hardware scan (rates the CPU; the result sets the tier, the start mode and the Performance limits)
# ============================================================================

_CLASS_ORDER = ("A", "B", "C")
_MODE_ORDER = ("lite", "standard", "performance")


def _read_cpu_name():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                value = winreg.QueryValueEx(key, "ProcessorNameString")[0]
                if str(value).strip():
                    return " ".join(str(value).split())
        except Exception:
            pass
    else:
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return " ".join(line.split(":", 1)[1].split())
        except Exception:
            pass
    return " ".join(str(platform.processor() or "").split())


def _read_avx2():
    if os.name == "nt":
        try:
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        except Exception:
            return None
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("flags"):
                    return " avx2" in (" " + line.lower())
    except Exception:
        pass
    return None


def _class_from_threshold(value, table):
    for limit, label in table:
        if value <= limit:
            return label
    return table[-1][1]


def parse_cpu_class(cpu_name):
    """Returns (vendor, generation, class, note) from a CPU model string; class is None when the family is not recognised."""
    name = str(cpu_name or "")
    low = name.lower()
    if "amd" in low or "ryzen" in low or re.search(r"\bfx-|athlon|phenom", low):
        if re.search(r"\bfx-|athlon|phenom|\ba[0-9]+-", low):
            return "amd", 0, "A", "fx/athlon/phenom/a-series"
        if re.search(r"ryzen\s+ai", low):
            return "amd", 9, "C", "ryzen ai"
        found = re.search(r"ryzen\s+[3579]\s+(?:pro\s+)?(\d)(\d{3})", low)
        if found:
            series = int(found.group(1))
            return "amd", series, _class_from_threshold(series, BOOT_SCAN_AMD_SERIES_CLASS), ""
        return "amd", 0, None, "amd family not recognised"
    if "intel" in low or re.search(r"\bcore\b|celeron|pentium|atom", low):
        if re.search(r"celeron|pentium|\batom\b", low):
            return "intel", 0, "A", "celeron/pentium/atom"
        if re.search(r"core\(tm\)\s*ultra|core\s+ultra", low):
            return "intel", 15, "C", "core ultra"
        gen = None
        found = re.search(r"(\d{1,2})(?:st|nd|rd|th)\s+gen", low)
        if found:
            gen = int(found.group(1))
        else:
            found = re.search(r"\bi[3579]-(\d{3,5})", low)
            if found:
                digits = found.group(1)
                if len(digits) == 3:
                    gen = 1
                elif len(digits) == 4:
                    gen = int(digits[:2]) if digits[0] == "1" else int(digits[0])
                else:
                    gen = int(digits[:2])
        if gen is not None:
            return "intel", gen, _class_from_threshold(gen, BOOT_SCAN_INTEL_GEN_CLASS), ""
        return "intel", 0, None, "intel family not recognised"
    return "unknown", 0, None, "vendor not recognised"


def _demote(label, steps=1):
    index = max(_CLASS_ORDER.index(label) - steps, 0)
    return _CLASS_ORDER[index]


def _min_mode(first, second):
    return _MODE_ORDER[min(_MODE_ORDER.index(first), _MODE_ORDER.index(second))]


def make_hardware_fingerprint(profile, cpu_name=""):
    """Hardware-only fingerprint (CPU model, cores, RAM bucket, GPU names); software versions are not part of it."""
    bucket = BOOT_SCAN_FINGERPRINT_BUCKET_GB
    ram_bucket = int(round(profile.ram_total_gb / bucket) * bucket) if profile.ram_total_gb > 0 else 0
    parts = [
        "c" + " ".join(str(cpu_name or "unknown").lower().split()),
        "p%d" % profile.physical_cores, "e%d" % profile.efficiency_cores,
        "l%d" % profile.logical_cores, "r%d" % ram_bucket,
        "g" + ";".join(sorted(str(n).lower() for n in profile.gpu_names)),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def rate_cpu(cpu_name, physical_cores, ram_total_gb):
    """Rates the CPU model into class A / B / C, then adjusts for cores and RAM; unknown chips use the core-count class."""
    vendor, generation, label, note = parse_cpu_class(cpu_name)
    source = "model"
    if label is None:
        source = "core-count fallback"
        label = BOOT_SCAN_FALLBACK_CLASS
    base_label = label
    reasons = []
    borderline = (vendor, generation) in BOOT_SCAN_BORDERLINE
    if borderline and label == "B" and physical_cores >= BOOT_SCAN_BORDERLINE_PROMOTE_MIN_PHYSICAL:
        label = "C"
        reasons.append("borderline chip with %d+ physical cores counted as class C" % BOOT_SCAN_BORDERLINE_PROMOTE_MIN_PHYSICAL)
    if physical_cores < BOOT_SCAN_LOW_CORES_BELOW:
        label = _demote(label)
        reasons.append("under %d physical cores" % BOOT_SCAN_LOW_CORES_BELOW)
    if ram_total_gb > 0.0 and ram_total_gb < BOOT_SCAN_LOW_RAM_GB:
        label = _demote(label)
        reasons.append("under %.0f GB RAM" % BOOT_SCAN_LOW_RAM_GB)
    return {"vendor": vendor, "generation": generation, "base_class": base_label, "class": label,
            "class_source": source, "note": note, "reasons": reasons, "borderline": borderline}


def run_boot_scan(profile, cpu_name=None, avx2=None):
    """Scores the machine from CPU model, cores, RAM and GPU; returns the per-capability caps (scan and caption caps are applied by _limit_mode)."""
    if cpu_name is None:
        cpu_name = profile.cpu_name or _read_cpu_name()
    avx2 = _read_avx2() if avx2 is None else avx2
    rating = rate_cpu(cpu_name, profile.physical_cores, profile.ram_total_gb)
    label = rating["class"]
    reasons = list(rating["reasons"])
    tier = classify_tier(profile.effective_physical, profile.logical_cores, profile.ram_total_gb, label)
    start_mode = BOOT_SCAN_CLASS_START_MODE[label]
    top = tier == "STRONG" or (label == "C" and (profile.physical_cores >= BOOT_SCAN_TOP_MIN_PHYSICAL or profile.hybrid))
    if tier == "WEAK":
        start_mode = "lite"
    elif top:
        start_mode = "performance"
    performance_ok = (tier in BOOT_SCAN_PERFORMANCE_SCAN_TIERS
                      and profile.effective_physical >= BOOT_SCAN_PERFORMANCE_SCAN_MIN_EFFECTIVE)
    scan_cap = "performance" if performance_ok else "standard"
    caption_cap = scan_cap
    if avx2 is False:
        caption_cap = "off"
        reasons.append("no AVX2: captions off")
    caps = {
        "scan": scan_cap,
        "caption": caption_cap,
        "effects": _min_mode(start_mode, BOOT_SCAN_EFFECTS_MAX_MODE),
        "video": start_mode,
        "gpu": "%s" % profile.gpu_tier,
    }
    return {
        "cpu_name": cpu_name, "vendor": rating["vendor"], "generation": rating["generation"],
        "base_class": rating["base_class"], "class": label, "class_source": rating["class_source"],
        "note": rating["note"], "reasons": reasons, "avx2": avx2, "borderline": rating["borderline"],
        "tier": tier, "start_mode": start_mode, "caps": caps,
        "hardware_fingerprint": make_hardware_fingerprint(profile, cpu_name),
    }


def _ensure_boot_scan():
    """The boot scan result, computed on first use; None when the scan failed."""
    with _state_lock:
        scan = _state.get("boot_scan")
        failed = _state.get("boot_scan_failed")
    if scan is not None or failed:
        return scan
    profile = get_profile()
    try:
        result = run_boot_scan(profile)
    except Exception as err:
        log.warning("[GOVERNOR BOOT SCAN] failed: %s", err)
        with _state_lock:
            _state["boot_scan_failed"] = True
        return None
    with _state_lock:
        if _state.get("boot_scan") is None:
            _state["boot_scan"] = result
        return _state["boot_scan"]


def get_boot_scan():
    return _ensure_boot_scan()


_limit_logged = set()


def _limit_mode(capability, mode):
    """The mode this PC can actually run `capability` at: Performance drops to Standard where the boot scan says the PC cannot take it; Lite and Standard are never changed."""
    if mode != "performance":
        return mode
    scan = _ensure_boot_scan()
    cap = ((scan or {}).get("caps") or {}).get(capability)
    if cap is None or cap == "performance":
        return mode
    if capability not in _limit_logged:
        _limit_logged.add(capability)
        log.info("[GOVERNOR] Performance limited to Standard for %s on this PC (cpu class %s, tier %s, scan cap %s)",
                 capability, (scan or {}).get("class"), (scan or {}).get("tier"), cap)
    return "standard"


def _log_boot_scan(profile):
    result = _ensure_boot_scan()
    if result is None:
        return None
    caps = result["caps"]
    log.info(
        "[GOVERNOR BOOT SCAN] cpu=%r vendor=%s generation=%s class=%s (base %s, %s) borderline=%s avx2=%s "
        "tier=%s would_start_mode=%s caps: scan=%s caption=%s effects=%s video=%s gpu=%s hw_fingerprint=%s "
        "adjustments=%s%s",
        result["cpu_name"], result["vendor"], result["generation"], result["class"], result["base_class"],
        result["class_source"], result["borderline"], result["avx2"], profile.tier, result["start_mode"],
        caps["scan"], caps["caption"], caps["effects"], caps["video"], caps["gpu"],
        result["hardware_fingerprint"], "; ".join(result["reasons"]) or "none",
        (" note=" + result["note"]) if result["note"] else "")
    return result


# ============================================================================
# SECTION: Software fingerprint (logged at boot)
# ============================================================================


def make_fingerprint(profile, vlc_version="", app_version="", pygame_version=""):
    bucket = RAM_FINGERPRINT_BUCKET_GB
    ram_bucket = int(round(profile.ram_total_gb / bucket) * bucket) if profile.ram_total_gb > 0 else 0
    parts = [
        "p%d" % profile.physical_cores, "e%d" % profile.efficiency_cores,
        "l%d" % profile.logical_cores, "r%d" % ram_bucket,
        "g" + ";".join(sorted(str(n) for n in profile.gpu_names)),
        "v" + str(vlc_version or "unknown"),
        "a" + str(app_version or "unknown"),
        "y" + str(pygame_version or "unknown"),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


# ============================================================================
# SECTION: Module state and public entry points
# ============================================================================

_state = {
    "profile": None,
    "mode": ACTIVE_MODE_DEFAULT,
    "fingerprint": "",
    "started": False,
    "boot_scan": None,
    "boot_scan_failed": False,
}
_state_lock = threading.Lock()


def get_profile():
    with _state_lock:
        if _state["profile"] is None:
            physical, efficiency, logical, source = _read_cpu_counts()
            ram_total, ram_avail = _read_ram_gb()
            _state["profile"] = build_profile(physical, efficiency, logical, ram_total, ram_avail, source)
        return _state["profile"]


def set_mode(value):
    _state["mode"] = normalize_mode(value)
    _apply_cv2_threads_logged()
    return _state["mode"]


_cv2_threads_applied = {"count": None}


def _apply_cv2_threads_logged():
    count = apply_cv2_threads(_state["mode"])
    if count is not None and count != _cv2_threads_applied["count"]:
        _cv2_threads_applied["count"] = count
        log.info("[GOVERNOR] OpenCV threads=%d (mode=%s, logical threads=%d)", count, _state["mode"],
                 get_profile().logical_cores)


def is_weak(profile=None):
    """True when the machine is labelled WEAK; Performance mode is not selectable then."""
    profile = profile or get_profile()
    return profile.tier == "WEAK"


def allowed_modes(profile=None):
    """Modes the user may select on this machine, in menu order."""
    weak = is_weak(profile)
    return tuple(name for name in MODE_NAMES if not (weak and name == "performance"))


def next_mode(current=None, profile=None):
    """The mode after `current` in the menu cycle (Lite, Standard, Performance), skipping modes not allowed here."""
    allowed = allowed_modes(profile)
    current = _resolve_mode(current)
    if current not in allowed:
        return ACTIVE_MODE_DEFAULT
    return allowed[(allowed.index(current) + 1) % len(allowed)]


def _auto_mode(profile):
    scan = _state.get("boot_scan")
    pick = scan["start_mode"] if scan else ACTIVE_MODE_DEFAULT
    return pick if pick in allowed_modes(profile) else ACTIVE_MODE_DEFAULT


def apply_boot_mode(config, force_auto=False):
    """Picks the starting mode into `config` and applies it live; returns True when the config changed and needs saving."""
    profile = get_profile()
    scan = _state.get("boot_scan")
    fingerprint = scan["hardware_fingerprint"] if scan else make_hardware_fingerprint(profile, "")
    auto = _auto_mode(profile)
    saved = str(config.get(MODE_CONFIG_KEY, "") or "").strip().lower()
    saved_source = config.get(MODE_SOURCE_CONFIG_KEY)
    same_hardware = config.get(MODE_FINGERPRINT_CONFIG_KEY) == fingerprint
    if force_auto:
        mode, source, why = auto, "auto", "reset"
    elif same_hardware and saved in allowed_modes(profile):
        mode = saved
        source = saved_source if saved_source in ("auto", "user") else "auto"
        why = "saved mode, hardware unchanged"
    else:
        mode, source = auto, "auto"
        why = "no saved mode" if not saved else ("hardware changed" if not same_hardware else "saved mode not allowed here")
    before = (config.get(MODE_CONFIG_KEY), config.get(MODE_SOURCE_CONFIG_KEY),
              config.get(MODE_AUTO_CONFIG_KEY), config.get(MODE_FINGERPRINT_CONFIG_KEY))
    config[MODE_CONFIG_KEY] = mode
    config[MODE_SOURCE_CONFIG_KEY] = source
    config[MODE_AUTO_CONFIG_KEY] = auto
    config[MODE_FINGERPRINT_CONFIG_KEY] = fingerprint
    set_mode(mode)
    log.info("[GOVERNOR] mode=%s source=%s auto_pick=%s (%s) fingerprint=%s", mode, source, auto, why, fingerprint)
    return before != (mode, source, auto, fingerprint)


def choose_mode(config, mode):
    """Records a mode the user picked in the menu and applies it live; returns the mode now in effect."""
    mode = mode if mode in allowed_modes() else ACTIVE_MODE_DEFAULT
    config[MODE_CONFIG_KEY] = mode
    config[MODE_SOURCE_CONFIG_KEY] = "user"
    set_mode(mode)
    log.info("[GOVERNOR] mode=%s source=user (menu)", mode)
    return mode


def get_mode():
    return _state["mode"]


def start(gpu_tier="none", gpu_names=(), vlc_version="", app_version="", pygame_version="",
          sdl_version="", mode=None):
    profile = get_profile()
    with _state_lock:
        profile.gpu_tier = str(gpu_tier)
        profile.gpu_names = tuple(str(n) for n in gpu_names)
        _state["fingerprint"] = make_fingerprint(profile, vlc_version, app_version, pygame_version)
        if mode is not None:
            _state["mode"] = normalize_mode(mode)
        first = not _state["started"]
        _state["started"] = True
        if first:
            _state["boot_scan"] = None
            _state["boot_scan_failed"] = False
    if first:
        _log_boot_scan(profile)
        mode_now = _state["mode"]
        _apply_cv2_threads_logged()
        log.info(
            "[GOVERNOR] profile cores=%d/%d (efficiency=%d) ram=%.1fGB (avail %.1fGB) gpu=%s [%s] "
            "tier=%s budget_cores=%.1f mode=%s shell=%s shadow=%s frame_budget=%.2fms cpu_source=%s "
            "pygame=%s sdl=%s fingerprint=%s",
            profile.physical_cores, profile.logical_cores, profile.efficiency_cores,
            profile.ram_total_gb, profile.ram_avail_gb, profile.gpu_tier,
            "; ".join(profile.gpu_names) or "none", profile.tier, core_budget(profile, mode_now),
            mode_now, _shell["active"], SHADOW_MODE, profile.frame_budget_ms, profile.cpu_source,
            pygame_version or "unknown", sdl_version or "unknown", _state["fingerprint"])
        log.info(
            "[GOVERNOR] ceilings mode=%s probe+duration=%s loudness+bake=%s music=%s whisper_slots=%s "
            "whisper_threads=%d cropdetect_threads=%s vlc_avcodec_threads=%d affordable_threads=%d caption_cadence=%s",
            mode_now, lane_ceiling("probe_and_duration"), lane_ceiling("loudness_and_bake"),
            lane_ceiling("music_loudness"), lane_ceiling("whisper_slots"),
            whisper_threads(profile), lane_ceiling("cropdetect_threads"), vlc_decoder_threads(profile),
            affordable_threads(profile), caption_cadence())
    return profile


def new_throttle(name="main"):
    t = Throttle(get_profile().frame_budget_ms, name)
    if name == "main":
        register_throttle(t)
    return t


__all__ = [
    "MachineProfile", "Throttle", "MODE_TABLE", "MODE_NAMES", "SHADOW_MODE",
    "get_profile", "start", "set_mode", "get_mode", "normalize_mode", "core_budget", "whisper_threads",
    "feature_locked", "menu_fps", "lane_ceiling", "lane_slots", "effective_factor", "level_allows", "new_throttle",
    "make_fingerprint",
    "build_profile", "classify_tier", "BusyWindow", "LossTracker", "SystemCpuSampler", "format_status",
    "gate", "activity", "priority_scope", "background_active", "enforcing", "set_enabled",
    "register_throttle", "current_level",
    "run_boot_scan", "get_boot_scan", "parse_cpu_class", "make_hardware_fingerprint",
    "vlc_decoder_threads", "affordable_threads", "caption_cadence", "cv2_threads", "apply_cv2_threads", "rate_cpu",
    "set_shell_mode", "is_shell_mode", "is_weak", "allowed_modes", "next_mode", "apply_boot_mode", "choose_mode",
]
