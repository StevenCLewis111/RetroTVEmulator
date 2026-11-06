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
# SECTION: Swallowed-failure lines (shared by every script)
# ============================================================================
_swallowed_failure_counts = {}
_swallowed_failure_lock = threading.Lock()


def log_swallowed(where, error):
    """Writes one [FAILURE] line naming where a caught failure happened, its error text and its source line, and again at the 10th, 100th, 1000th ... repeat."""
    # WARNING: this is the one shared copy; every script imports it from here, so the repeat counts are shared and a new script must import it, not copy it.
    # WARNING: this runs inside handlers on render, VLC callback and worker threads; it must never raise and must never wait on anything but its own short lock.
    try:
        key = (where, type(error).__name__)
        with _swallowed_failure_lock:
            count = _swallowed_failure_counts.get(key, 0) + 1
            _swallowed_failure_counts[key] = count
        if count != 10 ** (len(str(count)) - 1):
            return
        trace = error.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        source = "" if trace is None else " [%s:%d]" % (os.path.basename(trace.tb_frame.f_code.co_filename), trace.tb_lineno)
        log.warning("[FAILURE] %s: %s: %s%s%s", where, type(error).__name__, error, source,
                    "" if count == 1 else " (seen %d times)" % count)
    except Exception:
        pass


_log_swallowed = log_swallowed


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

# VALUES CHANGE HERE (duration probe threads: the pool is the probe lane's slots; a video playing divides it)
DURATION_WORKERS_WHILE_PLAYING_DIVISOR = 3

# VALUES CHANGE HERE (loudness scan dispatch: threads per round normally, and when nothing is playing or waiting; both capped by the loudness lane)
LOUDNESS_DISPATCH_WORKERS_BASE = 1
LOUDNESS_DISPATCH_WORKERS_SPARE = 2

# VALUES CHANGE HERE (VLC video decoder threads: the PC's effective physical cores less the reserved cores (left for the screen), never below the minimum or above the maximum; applied once, when the VLC engine is created; they do work only when the graphics card cannot decode a file)
VLC_AVCODEC_THREADS_RESERVED_CORES = 1
VLC_AVCODEC_THREADS_MIN = 1
VLC_AVCODEC_THREADS_MAX = 4

# VALUES CHANGE HERE (VLC hardware video decoding: when on, every video file the main engine opens asks VLC to decode on the graphics card, on Windows, when the boot scan found a real graphics card whose rating has a video decoder (or a card it cannot rate); a file the card cannot decode is decoded by the CPU, file by file)
VLC_HW_DECODE_ENABLED = True

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
# VALUES CHANGE HERE (weak PCs: the app is the one thing such a PC is used for, so it keeps back only what Windows itself needs: this many threads of headroom in every mode instead of the mode's own, and the same number of cores out of the core budget; standard PCs keep their headroom and Performance unlocks theirs)
WEAK_HEADROOM_THREADS = 1

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

# VALUES CHANGE HERE (Visualizer flying styles, Neon Starfield and Warp Tunnel: seconds the flying speed takes to ease toward the speed the music asks for, the slowest steady speed it may ever have so it never stops, the fastest speed (the starfield in depth per second, the tunnel in rings per second), longest frame time one step may use in seconds)
FLIGHT_SPEED_EASE_SECONDS = 3.0
FLIGHT_STARFIELD_MIN_SPEED = 0.35
FLIGHT_STARFIELD_MAX_SPEED = 1.4
FLIGHT_TUNNEL_MIN_SPEED = 0.40
FLIGHT_TUNNEL_MAX_SPEED = 1.10
FLIGHT_MAX_STEP_SECONDS = 0.1

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
# VALUES CHANGE HERE (learning the wait before the level comes down, from how this PC behaves: when the level climbs again within RELAX_RELAPSE_SECONDS of a step down, the calm wait before the next step down doubles, up to RELAX_COOLDOWN_MAX_SECONDS; each step down that holds that long halves it back toward RELAX_COOLDOWN_SECONDS)
RELAX_RELAPSE_SECONDS = 30.0
RELAX_COOLDOWN_MAX_SECONDS = 120.0
# VALUES CHANGE HERE (after a tuned channel's show starts, background work stays held until the picture has run TUNE_STEADY_SECONDS with no frames over their time and no lost pictures, and at most TUNE_STEADY_MAX_SECONDS; a tuned channel's first loudness scan starts only once the channel has stayed on screen TUNE_LOUDNESS_SETTLE_SECONDS, so flipping through channels does not start a scan on each one passed)
TUNE_STEADY_SECONDS = 3.0
TUNE_STEADY_MAX_SECONDS = 15.0
TUNE_LOUDNESS_SETTLE_SECONDS = 1.5

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
# VALUES CHANGE HERE (pressure test: each frame's busy time is judged against the frame time of the rate the app really ran that frame at (Lite menus run at the menu cap, games at the core's rate); the window's busy share is this percentile of those frame shares, so a few slow frames do not read as overload)
BUSY_RATIO_PERCENTILE = 0.75
# VALUES CHANGE HERE (the screen's own load, learned per screen (video, menu, game, other): learned while no background work runs, once the load level has held at the top for the settle seconds (all non-priority work is paused then), and when the last narrowing step has held for the settle seconds without lowering the busy share by the minimum gain; a reading below it lowers it at once; background work is blamed only for the share above it, and only when that share is at least the minimum excess; learn rate = how far one reading moves it; a move of at least the log step is logged, at most once per log interval per screen)
OWN_LOAD_LEARN_RATE = 0.2
OWN_LOAD_SETTLE_SECONDS = 6.0
OWN_LOAD_MIN_GAIN = 0.05
OWN_LOAD_MIN_EXCESS = 0.10
# VALUES CHANGE HERE (the minimum excess also grows with the screen's own load: at least this share of the own load, so the frame-to-frame swings of a screen already over its time are not read as background pressure, N11)
OWN_LOAD_MIN_EXCESS_SHARE = 0.15
OWN_LOAD_LOG_STEP = 0.10
OWN_LOAD_LOG_INTERVAL_SECONDS = 60.0
STALL_LOG_MS = 100.0
MIN_BUSY_SAMPLES = 5
FEED_INTERVAL_SECONDS = 1.0
LOSS_WINDOW_SECONDS = 5.0
MEDIA_START_GRACE_SECONDS = 3.0

# VALUES CHANGE HERE (burn-in idle close: seconds with no input before the controls splash, the settings menu with every sub-menu in it, the file explorer and the TV guide close (the guide goes to the channel it was previewing); seconds before every popup closes by itself as if No or Cancel was picked: Quit, the shared Yes/No popup (clear media, reset, burn-in warnings, update), Restart, DVD exit, DVD back to menu, game exit, the controller Pick Layout popup and the wait for a controller button)
MENU_IDLE_CLOSE_SECONDS = 60.0
CONFIRM_POPUP_IDLE_CLOSE_SECONDS = 30.0

# VALUES CHANGE HERE (held W/A/S/D key: milliseconds before a hold in a menu or the guide starts repeating)
MENU_NAV_REPEAT_DELAY_MS = 400

# VALUES CHANGE HERE (channel Up/Down: milliseconds you must stay on a channel number before that channel is tuned; presses faster than this only change the number shown on the grey screen)
CHANNEL_SURF_HESITATE_MS = 400

# VALUES CHANGE HERE (channel menu Schedule row: milliseconds a picked scheduling mode must be left alone, with no other press, before the channel really switches to it and the next block is rebuilt; this is the one 5-second wait, the screen shows the pick at once)
SCHEDULE_MODE_COMMIT_HOLD_MS = 5000

# VALUES CHANGE HERE (channel menu Schedule row: how many stopping points are kept per channel; one is written each time a picked scheduling mode is committed and holds the playing file, the clock moment and the show positions at that moment, and only the newest ones are kept)
SCHEDULE_MODE_STOP_POINTS_KEPT = 3

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
# VALUES CHANGE HERE (playback stall watchdog: a main-loop pass longer than this many ms is a blocked loop, and the blocked time is never counted as a playback stall)
LOOP_BLOCKED_GAP_MS = 1000
# VALUES CHANGE HERE (milliseconds between retries when a wedged playback engine is replaced)
WEDGED_ENGINE_REPLACE_RETRY_MS = 5000
# VALUES CHANGE HERE (tester report: bytes read from the end of app_log.txt and of each crash file; the most warning, error and failure lines of app_log.txt kept; the most health report lines kept; the most lines kept from each crash file)
TESTER_REPORT_LOG_TAIL_BYTES = 524288
TESTER_REPORT_MAX_LOG_LINES = 300
TESTER_REPORT_MAX_HEALTH_LINES = 80
TESTER_REPORT_MAX_CRASH_LINES = 200
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
# VALUES CHANGE HERE (boot drive wake-up: the most seconds boot waits for a sleeping media hard drive to spin up before it carries on without it, 0 = do not wake drives at boot)
BOOT_DRIVE_WAKE_SECONDS = 15.0
# VALUES CHANGE HERE (keep-awake: seconds between the reads that keep every media hard drive awake while the app is up, owner 2026-11-06, 0 = off; the most seconds one drive may take to answer before the next round)
DRIVE_KEEP_AWAKE_INTERVAL_SECONDS = 300.0
DRIVE_KEEP_AWAKE_READ_TIMEOUT_SECONDS = 10.0

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

# VALUES CHANGE HERE (Step 8 gates: how often a job waiting at a gate looks again)
GATE_POLL_SECONDS = 0.25

# VALUES CHANGE HERE (tuning to a channel: background work that has not started yet waits up to this many seconds, or until the channel's first show is playing, so the tune gets the PC to itself)
TUNE_BACKGROUND_HOLD_SECONDS = 3.0
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

# VALUES CHANGE HERE (boot GPU rating: each graphics chip name is matched against these patterns top to bottom, first match wins: (pattern, vendor, family, about the year it came out, the video formats its decoder plays); an empty format list = no video decoder; a chip that matches nothing is rated "unknown" and is still allowed to try hardware decoding)
BOOT_SCAN_GPU_TABLE = (
    (r"basic render|basic display", "microsoft", "software", 0, ()),
    (r"rtx\s*50\d\d", "nvidia", "rtx 50", 2025, ("h264", "hevc", "vp9", "av1")),
    (r"rtx\s*40\d\d", "nvidia", "rtx 40", 2022, ("h264", "hevc", "vp9", "av1")),
    (r"rtx\s*30\d\d|rtx\s*a\d{3,4}", "nvidia", "rtx 30", 2020, ("h264", "hevc", "vp9", "av1")),
    (r"rtx\s*20\d\d|gtx\s*16\d\d|quadro rtx", "nvidia", "rtx 20 / gtx 16", 2018, ("h264", "hevc", "vp9")),
    (r"gtx?\s*10[3-8]0|titan x|quadro p\d", "nvidia", "gtx 10 / gt 1030", 2016, ("h264", "hevc", "vp9")),
    (r"gtx\s*9[5-9]0|gtx\s*750|gtx\s*9\d0m|quadro m\d", "nvidia", "gtx 900 / 750", 2014, ("h264",)),
    (r"g[tx]{1,2}x?\s*[67]\d0|quadro k\d", "nvidia", "gt/gtx 600-700", 2012, ("h264",)),
    (r"g[tx]{1,2}x?\s*[45]\d0|quadro [0-9]{3,4}\b", "nvidia", "gt/gtx 400-500", 2010, ("h264",)),
    (r"geforce|quadro|nvs", "nvidia", "older geforce", 2008, ("h264",)),
    (r"\barc\b", "intel", "arc", 2022, ("h264", "hevc", "vp9", "av1")),
    (r"iris\(r\)?\s*xe|uhd graphics 7\d\d|uhd graphics$|intel\(r\) graphics$", "intel", "xe", 2020, ("h264", "hevc", "vp9", "av1")),
    (r"uhd graphics 6\d\d\b|hd graphics 6\d\d\b|iris\(r\)?\s*plus graphics 6\d\d\b", "intel", "uhd/hd 600", 2017, ("h264", "hevc", "vp9")),
    (r"hd graphics 5\d\d\b|iris\(r\)?\s*(pro )?graphics 5\d\d\b|iris\(r\)?\s*plus graphics 6[45]0", "intel", "hd 500", 2015, ("h264", "hevc")),
    (r"hd graphics [56]\d{3}|iris\(r\)?\s*(pro )?graphics 6\d{3}", "intel", "hd 5000-6000", 2014, ("h264",)),
    (r"hd graphics 4\d{3}|iris\(r\)?\s*(pro )?graphics 5\d{3}", "intel", "hd 4000-5000 (haswell)", 2013, ("h264",)),
    (r"hd graphics (2500|4000)", "intel", "hd 2500/4000", 2012, ("h264",)),
    (r"hd graphics", "intel", "hd graphics (older)", 2011, ("h264",)),
    (r"\bgma\b|media accelerator", "intel", "gma", 2008, ()),
    (r"rx\s*9\d{3}", "amd", "rx 9000", 2025, ("h264", "hevc", "vp9", "av1")),
    (r"rx\s*7\d{3}|radeon 7[0-9]0m", "amd", "rx 7000", 2022, ("h264", "hevc", "vp9", "av1")),
    (r"rx\s*6\d{3}|radeon 6[0-9]0m", "amd", "rx 6000", 2020, ("h264", "hevc", "vp9", "av1")),
    (r"rx\s*5\d{3}\b", "amd", "rx 5000", 2019, ("h264", "hevc", "vp9")),
    (r"vega|radeon\(tm\)? graphics$|radeon graphics$|radeon vii", "amd", "vega / apu", 2017, ("h264", "hevc", "vp9")),
    (r"rx\s*[45][0-9]0\b|rx\s*5[0-9]0x?\b", "amd", "rx 400-500", 2016, ("h264", "hevc")),
    (r"r[579]\s*[23m]\d\d|r9 fury|r9 nano", "amd", "r5/r7/r9", 2013, ("h264",)),
    (r"hd\s*[78]\d{3}", "amd", "hd 7000-8000", 2012, ("h264",)),
    (r"hd\s*[56]\d{3}", "amd", "hd 5000-6000", 2010, ("h264",)),
    (r"radeon|firepro|ati ", "amd", "older radeon", 2008, ("h264",)),
)
# VALUES CHANGE HERE (GPU age class from its year: up to the first year = A (old), up to the second = B, newer = C)
BOOT_SCAN_GPU_YEAR_CLASS = ((2013, "A"), (2018, "B"), (9999, "C"))


# VALUES CHANGE HERE (GPU frame path: adapter choice "monitor" or "fastest"; start-up self-check picture size, per-channel tolerance out of 255 and the share of values allowed past it; failures in a row before the GPU path is switched off for the run; device losses allowed per run; seconds to wait before reopening after a failure; frames of one path collected before a [GPU FRAME] log line is written)
GPU_ADAPTER_POLICY = "monitor"
GPU_SELF_CHECK_SIZE = (640, 360)
GPU_SELF_CHECK_TOLERANCE = 2
GPU_SELF_CHECK_MAX_OVER_FRACTION = 0.001
GPU_FRAME_FAILURE_LIMIT = 3
GPU_DEVICE_LOST_LIMIT = 3
GPU_RETRY_COOLDOWN_SECONDS = 30.0
GPU_FRAME_LOG_INTERVAL_FRAMES = 600
# VALUES CHANGE HERE (N7 owner 2026-11-03: the back-buffer size the GPU self-check is drawn at, the same for every window size, so switching between full screen and a window reopens the GPU path at once)
GPU_SELF_CHECK_TARGET_SIZE = (960, 540)
# VALUES CHANGE HERE (GPU frame path, the Video tab's Tint: colour shift per step away from 50, the same as the CPU picture pass)
GPU_PICTURE_TINT_PER_STEP = 1.5

# VALUES CHANGE HERE (menu key trace: key events landing in one frame that write a [MENU NAV BURST] warning; loop profile: frames collected before a [LOOP PROFILE] line is written, 0 = off)
MENU_NAV_BURST_WARN_EVENTS = 3
LOOP_PROFILE_LOG_INTERVAL_FRAMES = 600

# VALUES CHANGE HERE (stall stacks: when one main-loop frame has run longer than the first ms, a [STALL STACK] line in app_log.txt names where the screen thread is, again every repeat ms while that frame goes on, at most the max lines per frame, each with the innermost depth calls)
STALL_STACK_FIRST_MS = 1000
STALL_STACK_REPEAT_MS = 2000
STALL_STACK_MAX_PER_FRAME = 6
STALL_STACK_DEPTH = 14

# VALUES CHANGE HERE (guide history rebuild: most recent missed block occurrences recorded per channel at a block change)
GUIDE_REPLAY_MAX_BLOCKS = 60

# VALUES CHANGE HERE (schedule hub saved estimates: scanned episodes of one series needed before its unscanned episodes get the average as their saved estimate; length in seconds saved as the estimate of a movie that is not scanned yet)
HUB_SERIES_ESTIMATE_MIN_SCANNED = 2
HUB_MOVIE_ESTIMATE_SECONDS = 5400

# VALUES CHANGE HERE (season guesses for loudness, picture box and length: scanned episodes of one season needed before its unscanned episodes borrow their values; the scanned episodes must agree within these limits (length in seconds, loudness in dB, picture rectangle and picture shape as shares of the frame) or nothing is guessed; seconds a worked-out guess is kept before it is worked out again)
SEASON_GUESS_MIN_SCANNED = 2
SEASON_GUESS_DURATION_SPREAD_SECONDS = 90.0
SEASON_GUESS_GAIN_SPREAD_DB = 2.0
SEASON_GUESS_PICTURE_RECT_TOLERANCE = 0.02
SEASON_GUESS_PICTURE_ASPECT_TOLERANCE = 0.02
SEASON_GUESS_CACHE_SECONDS = 30.0

# VALUES CHANGE HERE (schedule hub: seconds before the hub asks the library again about a file that has no measured length yet)
HUB_UNMEASURED_RECHECK_SECONDS = 30.0

# VALUES CHANGE HERE (milliseconds a channel must stay on Static for the file-scanning reason before the "SCANNING FILES" / "SCANNING MUSIC" banner shows; the banner is for a scan that is taking too long, not for every normal tune-in)
FIRST_FILE_SCANNING_BANNER_DELAY_MS = 8000

# VALUES CHANGE HERE (milliseconds a channel stays on the static snow while it cannot play yet before the screen turns to the grey background; the grey then holds, so a channel that needs a little longer never flashes snow again)
STATIC_TO_GREY_MS = 2000

# VALUES CHANGE HERE (schedule hub loudness: seconds before the hub asks the library again about a file that has no scanned gain on record)
HUB_GAIN_MISS_RECHECK_SECONDS = 900.0

# VALUES CHANGE HERE (schedule hub clock check: seconds either side of a slot edge in which the hub's answer for "what is on air now" may differ from the scheduler's without being reported)
HUB_CLOCK_COMPARE_GRACE_SECONDS = 1.0

# VALUES CHANGE HERE (schedule hub airing lookup: seconds the scheduler waits before asking the hub again for the airing entry after the hub had no matching row; the scheduler's own pick airs in the meantime)
HUB_AIRING_LOOKUP_RETRY_SECONDS = 1.0

# VALUES CHANGE HERE (schedule hub rows: a rebuilt block whose rows at already-aired positions start this many seconds away from the rows on record, or hold other files, gets its own set of rows and the old rows stay as history)
HUB_ROW_GENERATION_START_TOLERANCE_SECONDS = 60.0

# VALUES CHANGE HERE (schedule hub future rows: how many hours ahead of the clock the background step works out what each channel will air and saves it in the hub for the guide to read; 192 is the guide's seven day span plus one extra day)
HUB_FUTURE_HOURS_AHEAD = 192.0

# VALUES CHANGE HERE (guide future: the first this many hours ahead are redrawn whenever a file length comes in; past them, each further day of this length is redrawn only once every file in it has a measured length, so the far guide does not keep shifting)
GUIDE_LIVE_HOURS = 24.0

# VALUES CHANGE HERE (update install: milliseconds the full bar stays up before the app hands over to the updater and restarts)
UPDATE_INSTALL_HOLD_AT_100_MS = 700

# VALUES CHANGE HERE (update install: milliseconds an update error stays up before the popup closes)
UPDATE_INSTALL_ERROR_SHOW_MS = 4000

# VALUES CHANGE HERE (update install: the download bar reads at most this fraction until the file is checked and unpacked)
UPDATE_INSTALL_DOWNLOAD_CAP = 0.97

# VALUES CHANGE HERE (schedule hub future rows: seconds the background step waits after working out one channel, before the next channel, so playback is never crowded)
HUB_FUTURE_BETWEEN_CHANNELS_SECONDS = 4.0

# VALUES CHANGE HERE (schedule hub future rows: seconds after boot before the background step starts, so the future is never worked out during boot)
HUB_FUTURE_START_DELAY_SECONDS = 45.0

# VALUES CHANGE HERE (schedule hub future rows: a channel whose inputs have not changed is still worked out again after this many seconds, so a newly found file length reaches rows that are still ahead)
HUB_FUTURE_REFRESH_SECONDS = 900.0

# VALUES CHANGE HERE (schedule hub future rows: seconds the background step sleeps when every channel is up to date)
HUB_FUTURE_IDLE_SECONDS = 30.0

# VALUES CHANGE HERE (hub writer tick: seconds between the scheduler's bookkeeping passes for the channel on screen)
HUB_WRITER_WATCHED_SECONDS = 0.5

# VALUES CHANGE HERE (hub writer tick: seconds the writer leaves a station alone after a pass that left it with no kept record, such as a Static station)
HUB_WRITER_STATIC_RECHECK_SECONDS = 60.0

# VALUES CHANGE HERE (hub writer tick: seconds between the scheduler's bookkeeping passes for each other channel, one channel per pass)
HUB_WRITER_OTHER_CHANNEL_SECONDS = 20.0

# VALUES CHANGE HERE (schedule hub slot-mode switch: seconds a channel's slot mode must stay unchanged before the hub puts its current set of rows aside and opens a new one, so cycling through modes quickly changes nothing in the hub)
HUB_MODE_SWITCH_SETTLE_SECONDS = 0.5

# VALUES CHANGE HERE (fall back to Static: milliseconds a playing channel may go without a new picture before the screen shows Static; it must stay below the stall recovery time in retro_tv_emulator.py, PLAYBACK_STALL_TIMEOUT_MS)
STALL_FORCES_STATIC_MS = 7000

# VALUES CHANGE HERE (fall back to Static on a channel's first load: milliseconds the channel you just tuned to may go without its first picture before the screen shows Static and the red banner; longer than STALL_FORCES_STATIC_MS because the first show is still being measured and opened (owner log 2026-11-06 08:31: ch05 first picture took about 8 s on a cold start); it must stay below PLAYBACK_INITIAL_BUFFER_TIMEOUT_MS)
STALL_FORCES_STATIC_FIRST_LOAD_MS = 10000

# VALUES CHANGE HERE (loudness scan sampling: each sample window is this many seconds of audio (how many windows a file gets is LOUDNESS_SCAN_WINDOW_COUNTS below); a file up to the whole-file length is scanned whole)
LOUDNESS_SCAN_WINDOW_SECONDS = 60.0
LOUDNESS_SCAN_WHOLE_FILE_MAX_SECONDS = 180.0

# VALUES CHANGE HERE (loudness scan window count by length: each pair is (longest file in seconds, windows), the last pair (None, windows) is for anything longer; windows are evenly spaced; a background first scan stops early once this many windows agree within the LU given, read from the middle, the start and the end of the file first)
LOUDNESS_SCAN_WINDOW_COUNTS = ((600.0, 3), (1500.0, 4), (3600.0, 5), (5400.0, 6), (9000.0, 7), (None, 9))
LOUDNESS_SCAN_EARLY_STOP_WINDOWS = 3
LOUDNESS_SCAN_EARLY_STOP_LU = 1.5

# VALUES CHANGE HERE (one loudness goal for every file: TV dialogue, commercials and music all aim at the target LUFS (ATSC A/85, -24); the station level in dB is then added to every file alike; False for the whole-file peak check skips an extra whole-file read of the true peak when a boosted peak lands near the ceiling)
LOUDNESS_TARGET_LUFS = -24.0
LOUDNESS_STATION_LEVEL_DB = 7.0
LOUDNESS_WHOLE_FILE_PEAK_CHECK = False

# VALUES CHANGE HERE (gain double-check, right away: a scan whose measured loudness lands more than this many dB from the target (the station level not counted) scans this many more batches of windows, each batch the size given below; the gain is then worked out from every window together)
GAIN_CHECK_DRASTIC_DB = 4.0
GAIN_CHECK_DRASTIC_EXTRA_BATCHES = 2

# VALUES CHANGE HERE (gain double-check, after the fact: a file whose gain is this many dB or more from the middle gain of its season folder is scanned again once with this many more batches of windows, and its gain is worked out from every window together; the season folder needs at least the second number of scanned files, the file itself included, before any file in it is compared)
GAIN_CHECK_NEIGHBOR_DB = 3.0
GAIN_CHECK_BUNDLE_MIN_SCANNED = 4
GAIN_CHECK_RECHECK_EXTRA_BATCHES = 2

# VALUES CHANGE HERE (gain double-check, where the new windows go: windows per extra batch; the share of one window a new window may overlap an earlier window, 0 meaning never)
GAIN_CHECK_EXTRA_BATCH_WINDOWS = 3
GAIN_CHECK_MAX_OVERLAP = 0.5

# VALUES CHANGE HERE (scheduler catch-up: most recent missed blocks replayed per channel at a block change; most cycles replayed inside one missed block)
CATCH_UP_MAX_BLOCKS = 3000
CATCH_UP_MAX_CYCLES_PER_BLOCK = 1000


# ============================================================================
# SECTION: Main script values (read by retro_tv_emulator.py)
# ============================================================================

# VALUES CHANGE HERE
BETWEEN_LOUDNESS_PROBES_SECONDS = 0.5

# VALUES CHANGE HERE
PROBE_IDLE_SLEEP_SECONDS = 20.0

# VALUES CHANGE HERE (pipeline pacing)
SCAN_PIPELINE_PREEMPT_CHECK_SECONDS = 5.0
SCAN_GATHER_LIBRARY_LOOKUPS = 400

# VALUES CHANGE HERE
PICTURE_BOX_PAUSE_RECHECK_SECONDS = 5.0

# VALUES CHANGE HERE
DURATION_PRECACHE_PAUSE_RECHECK_SECONDS = 5.0
DURATION_PRECACHE_IDLE_SLEEP_SECONDS = 20.0

# VALUES CHANGE HERE (used only when a bezel image has no transparent screen hole to measure, and before the boot preloader has measured it)
TV_BORDER_KNOWN_CUTOUTS = {
    "bezel":  (0.2943, 0.2434, 0.3523, 0.4591),
    "bezel1": (0.2053, 0.1344, 0.5060, 0.6595),
    "bezel2": (0.0774, 0.1402, 0.5646, 0.7181),
}

# VALUES CHANGE HERE
TV_BORDER_OSD_OFFSET = (48, 64)

# VALUES CHANGE HERE
PIXEL_ORBITER_MARGIN_PX = 3
PIXEL_ORBITER_INITIAL_DELAY_MS = 2 * 60 * 1000

# VALUES CHANGE HERE
PIXEL_ORBITER_PERIOD_MS = 60 * 1000

# VALUES CHANGE HERE
PICTURE_BOX_FIT_RETRY_SECONDS = 2.0

# VALUES CHANGE HERE
PAN_SCAN_ANALYZE_EVERY_N = 2
PAN_SCAN_SMOOTHING = 0.08
PAN_SCAN_DEAD_ZONE = 0.04

# VALUES CHANGE HERE
PAN_SCAN_SHOT_SIG_LEN = 10
PAN_SCAN_SHOT_MEMORY_MAX = 40
PAN_SCAN_REMEMBER_GRACE_MS = 400
PAN_SCAN_SHOT_MATCH_THRESHOLD = 0.025

# VALUES CHANGE HERE
CRT_SHADER_CURVATURE_DEFAULT = 0.20

# VALUES CHANGE HERE
CRT_SHADER_CURVATURE = {
    "bezel":  0.08,
    "bezel1": 0.08,
    "bezel2": 0.08,
}

# VALUES CHANGE HERE
CRT_SHADER_VIGNETTE = 0.35

# VALUES CHANGE HERE
CRT_SCANLINE_MIN_LINES = 180

# VALUES CHANGE HERE
CRT_SCANLINE_MAX_LINES = 320
CRT_SCANLINE_DARKEN_FLOOR = 0.70
CRT_SCANLINE_BLOOM_GAMMA = 1.6
CRT_SCANLINE_BLOOM_STRENGTH = 0.95

# VALUES CHANGE HERE
MENU_NAV_TRACE = True

# VALUES CHANGE HERE
PERF_LOG_INTERVAL_S = 5.0
PERF_LOG_MAX_SAMPLES = 900

# VALUES CHANGE HERE
STATIC_NOISE_REFRESH_MS = 110

# VALUES CHANGE HERE (Static Noise: how often the snow changes to another picture, in ms; smaller is faster, 33 is 30 changes a second)
STATIC_NOISE_AMBIENT_REFRESH_MS = 33
# VALUES CHANGE HERE (Static Noise: how many noise pictures are made once per screen size and shown in random order, never the same one twice running, so the snow shows no repeating pattern; how many screen sizes keep their pictures)
STATIC_NOISE_POOL_FRAMES = 12
STATIC_NOISE_POOL_SIZES = 3
# VALUES CHANGE HERE (Static Noise grain: the size in screen pixels of one speck of snow, 1 is the finest and costs the most to make, 3 is the coarse snow of V347; at most one new picture is made every STATIC_NOISE_BUILD_GAP_MS while the set fills, so the screen thread is never given a burst of them)
STATIC_NOISE_GRAIN_PX = 3
STATIC_NOISE_BUILD_GAP_MS = 80
# VALUES CHANGE HERE (Static Noise: the snow's strength is baked into each picture's own see-through values in this many steps, because a picture with both its own see-through values and an overall strength drew about 8 times slower; how many baked pictures are kept, all sizes and strengths together)
STATIC_NOISE_STRENGTH_STEPS = 16
STATIC_NOISE_BAKED_MAX = 24

# VALUES CHANGE HERE
STATIC_WEAK = 0.16
STATIC_MEDIUM = 0.30
STATIC_HEAVY = 0.46

# VALUES CHANGE HERE
STATIC_WEAK_HOLD_MS = 10000
STATIC_MEDIUM_HOLD_MS = 6500
STATIC_HEAVY_HOLD_MS = 7000
STATIC_RAMP_MS = 1200
STATIC_SUDDEN_RAMP_MS = 220

# VALUES CHANGE HERE
WATERMARK_JUMP_TOLERANCE_PX = 2
WATERMARK_REPOSITION_HIDE_MS = 450
WATERMARK_ALPHA = 150
WATERMARK_WIDTH_FRAC = 0.091
WATERMARK_MARGIN_FRAC = 0.12

# VALUES CHANGE HERE
GAME_CORE_FPS_FALLBACK = 60.0
GAME_CORE_FPS_GUARD = (40.0, 120.0)
UI_COMPOSITOR_FPS = 60.0

# VALUES CHANGE HERE
STATUS_BANNER_DURATION_MS = 30000
CAPTION_MEMORY_BANNER_LABEL = "LIVE CAPTIONS FAILED DUE TO LACK OF MEMORY"

# VALUES CHANGE HERE
DRIVE_BANNER_REARM_MS = 5 * 60 * 1000

# VALUES CHANGE HERE
MISSING_MEDIA_BANNER_REARM_MS = 5 * 60 * 1000

# VALUES CHANGE HERE
CAPTION_MIN_FIRST_SHOW_MS = 600

# VALUES CHANGE HERE
NATIVE_SUB_WATCH_MS = 20000
NATIVE_SUB_POLL_MS = 400

# VALUES CHANGE HERE
WATCHDOG_SPURIOUS_RESUME_BAIL_AFTER = 3
WATCHDOG_SPURIOUS_RESUME_WINDOW_MS = 20000

# VALUES CHANGE HERE
START_TIME_CONFIRM_MS = 800

# VALUES CHANGE HERE
START_TIME_POLL_MS = 150

# VALUES CHANGE HERE
LOAD_BAR_TEXT_GAP = 3

# VALUES CHANGE HERE
FE_FOOTER_H = 108
FE_LEFT_W = 215
FE_LEFT_ROW_H = 40
FE_LEFT_REMOVE_W = 24
FE_COL_H = 34
FE_ROW_H = 34
FE_BTN_H = 42

# VALUES CHANGE HERE
FE_BTN_W = 210
FE_BTN_W_SM = 85
FE_ADDLOC_HDR_H = 40
FE_ADDLOC_W = 150

# VALUES CHANGE HERE
FE_ICON_W = 14
FE_ICON_H = 11
FE_ICON_GAP = 6

# VALUES CHANGE HERE
CHANGED_FILE_SWEEP_INTERVAL_S = 1800

# VALUES CHANGE HERE (series loudness learning: how many of the owner's most recent small changes in one series must agree before the rest of the series is offset by their middle; how many dB apart agreeing changes may be (all up or all down); a change bigger than this many dB on an episode that scans like its season sets the series at once; the most dB an offset may be; seconds after the owner's last press before the change is learned from)
LOUDNESS_LEARN_MIN_FILES = 3
LOUDNESS_LEARN_AGREE_DB = 2.0
LOUDNESS_LEARN_SINGLE_CHANGE_DB = 3.0
LOUDNESS_LEARN_MAX_OFFSET_DB = 12
LOUDNESS_LEARN_SETTLE_SECONDS = 5.0

# VALUES CHANGE HERE (picture drawing: seconds a video file's size and date are remembered before the disk is asked again, so the disk is not asked for every picture drawn)
PAN_SCAN_KEY_CACHE_SECONDS = 2.0

# VALUES CHANGE HERE
GAIN_CHECK_FOLDER_REVISIT_S = 600

# VALUES CHANGE HERE
SCHED_MEMBERSHIP_CACHE_SECONDS = 5.0

# VALUES CHANGE HERE
ORDERED_CONTENT_CACHE_PRUNE_EVERY_MS = 60000
ORDERED_CONTENT_CACHE_LAST_PRUNE_MS = 0.0

# VALUES CHANGE HERE
MIXER_SEEK_TEST_ENCODERS = {
    ".mp3": "libmp3lame", ".ogg": "libvorbis", ".opus": "libopus", ".flac": "flac",
    ".wav": "pcm_s16le", ".aif": "pcm_s16be", ".aiff": "pcm_s16be",
    ".m4a": "aac", ".aac": "aac", ".wma": "wmav2",
}
MIXER_SEEK_TEST_SECONDS = 4
MIXER_SEEK_TEST_POSITION = 1.0

# VALUES CHANGE HERE
PRIME_DURATIONS_NEAR_TERM_N = 8

# VALUES CHANGE HERE
SCAN_SNAPSHOT_INTERVAL_S = 5.0
SCAN_SNAPSHOT_STALE_S = 15.0
# VALUES CHANGE HERE (the scan snapshot thread: after each rebuild it rests this many times as long as the rebuild took, so it never uses more than about 1 part in this number plus 1 of a core, however many files are being added; the longest that rest may be in seconds, which must stay below SCAN_SNAPSHOT_STALE_S)
SCAN_SNAPSHOT_DUTY_FACTOR = 8.0
SCAN_SNAPSHOT_MAX_REST_S = 10.0
SCAN_PICTURE_BOX_LOOKUPS_PER_BUILD = 200

# VALUES CHANGE HERE
MUSIC_PRECACHE_IDLE_SLEEP_SECONDS = 20.0
MUSIC_PRECACHE_BETWEEN_TRACKS_SECONDS = 0.5
MUSIC_PRECACHE_SAVE_THROTTLE_SECONDS = 10.0

# VALUES CHANGE HERE
GPU_DEDICATED_VRAM_FLOOR_BYTES = 512 * 1024 * 1024

# VALUES CHANGE HERE
SCAN_RETRY_LIMIT = 3
SCAN_DEAD_BURST_LIMIT = 5
SCAN_DEAD_BURST_WINDOW_S = 300.0

# VALUES CHANGE HERE
MAX_COMFORTABLE_VOLUME = 135

# VALUES CHANGE HERE
SS_SPEED_FRAC_PER_S = 0.07
SS_VISIBLE_MIN_ALPHA = 24
SS_MAX_DT_S = 0.1


# VALUES CHANGE HERE (main script, values used inside its functions: bezel screen hole grow per bezel image, width and height multipliers, and the multiplier for a bezel not listed)
TV_BORDER_SCREEN_EXPAND = {
    "bezel":  (1.00, 1.00),
    "bezel1": (1.00, 1.00),
    "bezel2": (1.00, 1.00),
}
TV_BORDER_SCREEN_EXPAND_DEFAULT = (1.00, 1.00)

# VALUES CHANGE HERE (reception line noise band: pixel block size, 2 is as fine as the Static Noise snow and 3 is the coarse look of V349; share of blocks drawn; share drawn bright; alpha range of bright and of dark blocks)
RECEPTION_NOISE_BLOCK_PX = 2
RECEPTION_NOISE_VISIBLE_SHARE = 0.55
RECEPTION_NOISE_BRIGHT_SHARE = 0.30
RECEPTION_NOISE_BRIGHT_ALPHA = (180, 255)
RECEPTION_NOISE_DARK_ALPHA = (110, 210)
# VALUES CHANGE HERE (reception lines: how often the noise inside a rolling line changes, in ms; smaller is faster, 33 is 30 changes a second like the Static Noise snow)
RECEPTION_NOISE_REFRESH_MS = 33

# VALUES CHANGE HERE (reception lines: height of a big and of a small rolling line as a share of the screen height, and the roll speed in pixels per millisecond)
RECEPTION_LINE_BIG_FRAC_RANGE = (0.035, 0.09)
RECEPTION_LINE_SMALL_FRAC_RANGE = (0.012, 0.03)
RECEPTION_LINE_SPEED_PX_MS = 0.85

# VALUES CHANGE HERE (file explorer: milliseconds a folder walk may go without reaching a new folder or file before it is given up as a drive that stopped answering)
FILE_EXPLORER_EXPAND_TIMEOUT_MS = 6000

# VALUES CHANGE HERE (playback seek: milliseconds between retries while a seek is checked to have landed)
SEEK_RETRY_INTERVAL_MS = 1500

# VALUES CHANGE HERE (frame copy timing: frames counted before one [FRAME COPY] log line)
FRAME_COPY_LOG_EVERY_FRAMES = 600

# VALUES CHANGE HERE (guide preload: seconds to wait for the window size before the preload worker gives up for that pass)
GUIDE_PRELOAD_WINDOW_SIZE_WAIT_SECONDS = 30.0

# VALUES CHANGE HERE (caption pre-cache: seconds between the cheap checks of the caption worker)
CAPTION_PRECACHE_BETWEEN_CHEAP_CHECKS_SECONDS = 0.3

# VALUES CHANGE HERE (scheduler repeat guard: seconds after a show ends in which the same show may not start again in the next slot, and seconds the just-ended record is kept)
SCHEDULER_REPEAT_GUARD_SECONDS = 60
SCHEDULER_REPEAT_GUARD_WINDOW_SECONDS = 900

# VALUES CHANGE HERE (block rotation caches: most blocks kept in the scheduler's rotation cache and in the saved rotation record)
BLOCK_ROTATION_CACHE_MAX_BLOCKS = 12


# ============================================================================
# SECTION: Media script values (read by media.py)
# ============================================================================

# VALUES CHANGE HERE (media.py, path checks: how often the path-exists cache is swept and its most entries; how long a not-found, found and unknown answer is kept in milliseconds; how long one exists check may take)
PATH_EXISTS_CACHE_SWEEP_INTERVAL_MS = 60000
PATH_EXISTS_CACHE_MAX_ENTRIES = 20000
PATH_NOT_FOUND_TTL_MS = 5000
PATH_FOUND_TTL_MS = 30000
PATH_UNKNOWN_TTL_MS = 1500
# VALUES CHANGE HERE (each channel ready ahead, owner 2026-11-02 N1: the screen thread uses a path status it holds for up to this long while its re-check runs on the channel-ready thread; path classes kept; track builds kept)
PATH_STALE_KEEP_MS = 600000
GUESS_CLASS_MEMO_MAX = 60000
TRACK_BUILD_MEMO_MAX = 400
# VALUES CHANGE HERE (media.py, show grouping: most files whose show, season and episode read from the path are kept, so a lineup rebuild does not read every file name again)
SHOW_NAME_MEMO_MAX = 60000
# VALUES CHANGE HERE (channel-ready thread: seconds between passes, pause between channels, re-checks and first checks per pass, seconds between its log lines)
CHANNEL_READY_INTERVAL_SECONDS = 2.0
CHANNEL_READY_CHANNEL_PAUSE_SECONDS = 0.05
CHANNEL_READY_REFRESH_BATCH = 400
CHANNEL_READY_WARM_BATCH = 400
CHANNEL_READY_LOG_SECONDS = 60.0
# VALUES CHANGE HERE (one [VIDEO DRAW] line with the part times of the TV picture every this many drawn pictures)
VIDEO_DRAW_LOG_FRAMES = 600
# VALUES CHANGE HERE (seconds between [MUSIC SCAN] lines)
MUSIC_SCAN_LOG_SECONDS = 60.0
PATH_EXISTS_PROBE_TIMEOUT_MS = 2500

# VALUES CHANGE HERE (media.py, disk and scan slots: fast-disk reads at once, loudness scans at once, loudness sample windows read side by side, music loudness scans at once; seconds background work waits for the playing file's reads before going on)
FAST_DISK_IO_MAX_CONCURRENT = 3
LOUDNESS_PROBE_MAX_CONCURRENT = 2
LOUDNESS_PARALLEL_MAX_CONCURRENT = 8
MUSIC_LOUDNESS_PROBE_MAX_CONCURRENT = 1
LIVE_PLAYBACK_PRIORITY_MAX_WAIT_S = 2.5

# VALUES CHANGE HERE (media.py, drive health: how long one drive check may take, how often a drive is checked again, and how soon an unresponsive drive is checked again, in milliseconds)
DRIVE_CHECK_TIMEOUT_MS = 2500
DRIVE_RECHECK_INTERVAL_MS = 4000
DRIVE_UNRESPONSIVE_RECHECK_MS = 3000

# VALUES CHANGE HERE (media.py, drive read cost: reads kept per drive, the fixed start cost of one ffmpeg read in milliseconds; background loudness work backs off only when sustained ffmpeg read cost exceeds the milliseconds per second of audio, one slow read is not enough; the fewest reads and the oldest read in milliseconds that count)
DRIVE_LATENCY_SAMPLE_WINDOW = 6
DRIVE_LATENCY_FIXED_OVERHEAD_MS = 2000
DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC = 150.0
DRIVE_LATENCY_MIN_SAMPLES = 3
DRIVE_LATENCY_SAMPLE_MAX_AGE_MS = 120_000

# VALUES CHANGE HERE (media.py, file and disc checks: how often a file is test-read again, how long the test read may take in milliseconds and how many bytes it reads; how often disc information is read again in milliseconds)
FILE_PROBE_RECHECK_MS = 5000
FILE_PROBE_TIMEOUT_MS = 3000
FILE_PROBE_READ_BYTES = 65536
DISC_INFO_RECHECK_MS = 4000

# VALUES CHANGE HERE (media.py, scheduling: seconds from which a file counts as a movie; the movie-heavy ratio and movies per block; recent picks held back from random play; block lengths in seconds by block name (the six-hour blocks are added from their hours); the default commercial interrupt floor in minutes)
MOVIE_LENGTH_THRESHOLD_SECONDS = 4800
MOVIE_HEAVY_RATIO = 2
MOVIES_PER_BLOCK = 2
RANDOM_REPEAT_HOLD_BACK = 10
BLOCK_LENGTH_SECONDS = {"Marathon": 86400, "Full Day": 86400, "Morning": 8 * 3600, "Evening": 8 * 3600, "Night": 8 * 3600}
COMMERCIAL_INTERRUPT_FLOOR_DEFAULT_MINUTES = 15

# VALUES CHANGE HERE (media.py, commercial breaks in a block's timeline: most seconds of deferred fill one break may catch up, fewest seconds between two breaks, shortest break in seconds)
BREAK_MAX_CATCHUP_SECONDS = 180
BREAK_MIN_GAP_SECONDS = 300
BREAK_MIN_SECONDS = 120

# VALUES CHANGE HERE (media.py, file facts: the highest bit rate in Mbps a believable length may give; seconds a bezel style folder listing is reused; seconds and most entries a shared header read is kept; the selection border width in pixels)
DURATION_SANITY_MAX_PLAUSIBLE_MBPS = 60
BEZEL_STYLE_DISCOVERY_TTL_SEC = 2.0
HEADER_PROBE_SHARE_SECONDS = 120.0
HEADER_PROBE_SHARE_MAX_ENTRIES = 64
SELECTION_BORDER_WIDTH = 3

# VALUES CHANGE HERE (media.py, generated captions, speech finding and cues: beam size, speech-finder settings, threshold and shortest speech in ms; join gap and longest clip in seconds; live audio filter; longest cue in seconds and characters, gap that breaks a cue, shortest time a cue shows, hold after the last word)
ASR_BEAM_SIZE = 5
ASR_VAD_PARAMETERS = {"min_silence_duration_ms": 500}
ASR_VAD_THRESHOLD = 0.25
ASR_VAD_MIN_SPEECH_MS = 150
ASR_MERGE_GAP_SEC = 1.5
ASR_LIVE_AUDIO_FILTER = "dynaudnorm=f=150:g=15:p=0.9:m=20"
ASR_MAX_CLIP_SEC = 25.0
ASR_CUE_MAX_SEC = 5.0
ASR_CUE_MAX_CHARS = 84
ASR_CUE_GAP_BREAK_SEC = 0.8
ASR_CUE_MIN_SHOW_SEC = 1.2
ASR_CUE_TRAIL_HOLD_SEC = 0.3

# VALUES CHANGE HERE (media.py, generated captions, guards: hallucination guard (highest no-speech chance, lowest average log chance); prompt-echo guard (word chance, repeats); gap recovery (on or off, gap, padding, windows, join, window length, beam, most seconds, share of a chunk, lead kept, word chance, log chance, no-speech chance, words per second, overlap share); runaway character and word runs; repetition-loop guard (fewest characters, highest compression))
ASR_NO_SPEECH_PROB_MAX = 0.75
ASR_AVG_LOGPROB_MIN = -1.5
ASR_ECHO_MAX_WORD_PROB = 0.5
ASR_ECHO_REPEAT_MIN = 3
ASR_ECHO_REPEAT_MAX_WORD_PROB = 0.75
ASR_RECOVER_ENABLED = True
ASR_RECOVER_MIN_GAP_SEC = 0.9
ASR_RECOVER_PAD_SEC = 0.3
ASR_RECOVER_MAX_WINDOWS = 2
ASR_RECOVER_JOIN_SEC = 3.0
ASR_RECOVER_MAX_WINDOW_SEC = 14.0
ASR_RECOVER_BEAM = 3
ASR_RECOVER_MAX_SEC = 6.0
ASR_RECOVER_MAX_FRAC_OF_CHUNK = 0.5
ASR_RECOVER_KEEP_LEAD_SEC = 8.0
ASR_RECOVER_MIN_WORD_PROB = 0.55
ASR_RECOVER_MIN_AVG_LOGPROB = -0.95
ASR_RECOVER_MAX_NO_SPEECH = 0.5
ASR_RECOVER_MAX_WORDS_PER_SEC = 5.0
ASR_RECOVER_MAX_OVERLAP_FRAC = 0.3
ASR_RUNAWAY_CHAR_RUN = 12
ASR_RUNAWAY_WORD_RUN = 6
ASR_LOOP_MIN_CHARS = 24
ASR_LOOP_COMPRESSION_MAX = 2.4

# VALUES CHANGE HERE (media.py, generated captions, music, names and chunks: music marking (shortest music, bridge, piece length, quietest level in dB, peak persistence, envelope steadiness, share replaced, word chance); name hint (word chance, fewest cues, shortest name, most names learned, likeness); chunk-boundary carry-over (share covered, nearness); first chunk, chunk and overlap in seconds; shortest tail; duplicate text likeness, short text length and its overlap; accurate seek margin)
ASR_MUSIC_MIN_SEC = 2.0
ASR_MUSIC_BRIDGE_SEC = 1.0
ASR_MUSIC_PIECE_SEC = 6.0
ASR_MUSIC_MIN_RMS_DB = -38.0
ASR_MUSIC_PEAK_PERSIST_MIN = 0.60
ASR_MUSIC_ENVELOPE_CV_MAX = 0.35
ASR_MUSIC_REPLACE_FRAC = 0.7
ASR_MUSIC_REPLACE_MAX_WORD_PROB = 0.5
ASR_NAME_MIN_WORD_PROB = 0.85
ASR_NAME_MIN_CUES = 2
ASR_NAME_MIN_LEN = 3
ASR_NAME_MAX_LEARNED = 6
ASR_NAME_SIMILAR_MIN = 0.75
ASR_CARRY_COVER_FRAC = 0.4
ASR_CARRY_NEAR_SEC = 1.5
ASR_FIRST_CHUNK_SEC = 60.0
ASR_CHUNK_SEC = 300.0
ASR_CHUNK_OVERLAP_SEC = 8.0
ASR_MIN_TAIL_SEC = 15.0
ASR_DUP_TEXT_SIMILARITY_MIN = 0.6
ASR_SHORT_TEXT_CHARS = 6
ASR_SHORT_TEXT_OVERLAP_MIN = 0.85
ASR_ACCURATE_SEEK_MARGIN_SEC = 15.0

# VALUES CHANGE HERE (media.py, live captions: lead, ready deadline and chunk in seconds; startup overlap and first-chunk head start; next-file prefetch (fewest seconds of lead, chunk, longest wait, cap); big-chunk safety margin and default estimate; lead cap; jump-back detection (lead, pace poll, skip behind, resume ahead); cues kept (seconds of past, most cues); most failures in a row; live beam size and accurate seek margin; clock check (shortest step, tolerance))
LIVE_CAPTION_LEAD_SEC = 5.0
LIVE_CAPTION_READY_DEADLINE_SEC = 10.0
LIVE_CAPTION_CHUNK_SEC = 30.0
LIVE_CAPTION_STARTUP_CHUNK_OVERLAP_SEC = 5.0
LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC = 3.0
LIVE_CAPTION_PREFETCH_MIN_LEAD_SEC = 15.0
LIVE_CAPTION_PREFETCH_CHUNK_SEC = 10.0
LIVE_CAPTION_PREFETCH_MAX_WAIT_SEC = 120.0
LIVE_CAPTION_PREFETCH_CAP_SEC = 60.0
LIVE_CAPTION_BIG_CHUNK_SAFETY_MARGIN_SEC = 3.0
LIVE_CAPTION_BIG_CHUNK_DEFAULT_ESTIMATE_SEC = 15.0
LIVE_CAPTION_MAX_LEAD_SEC = 60.0
LIVE_CAPTION_JUMP_BACK_LEAD_SEC = 120.0
LIVE_CAPTION_PACE_POLL_SEC = 1.0
LIVE_CAPTION_SKIP_BEHIND_SEC = 15.0
LIVE_CAPTION_RESUME_AHEAD_SEC = 2.0
LIVE_CAPTION_KEEP_PAST_SEC = 20.0
LIVE_CAPTION_MAX_CUES = 500
LIVE_CAPTION_MAX_FAILURES = 3
ASR_LIVE_BEAM_SIZE = 3
ASR_LIVE_ACCURATE_SEEK_MARGIN_SEC = 4.0
LIVE_CAPTION_CLOCK_MIN_DT = 0.75
LIVE_CAPTION_CLOCK_TOLERANCE = 0.4

# VALUES CHANGE HERE (media.py, loudness limits: highest and lowest gain multiplier, lowest gain for the standard floor; true-peak ceiling in dBTP; a file scanned by sample windows gets one whole-file true-peak pass when its gained peak would land within the margin in dB of the ceiling (only while LOUDNESS_WHOLE_FILE_PEAK_CHECK is True), and that pass's time limit in seconds)
LOUDNESS_GAIN_CEILING = 10.0
LOUDNESS_GAIN_FLOOR = 0.1
LOUDNESS_GAIN_FLOOR_STANDARD = 0.5
TRUE_PEAK_CEILING_DBTP = -2.0
TRUE_PEAK_CONFIRM_MARGIN_DB = 3.0
TRUE_PEAK_CONFIRM_TIMEOUT_SECONDS = 600.0

# VALUES CHANGE HERE (media.py, loudness compressor and windows: compressor attack and release in ms, threshold in dB, ratio, knee and makeup in dB; sample windows that disagree by more than this many LU use the quieter reading; a window is logged when it took at least this many seconds, or when it belongs to the playing file's first scan; loudness range in LU that flags a wide file)
COMPRESSOR_ATTACK_MS = 25.0
COMPRESSOR_RELEASE_MS = 800.0
COMPRESSOR_THRESHOLD_DB = -18.0
COMPRESSOR_RATIO = 2.0
COMPRESSOR_KNEE_DB = 6.0
COMPRESSOR_MAKEUP_DB = 4.0
LOUDNESS_WINDOW_DISAGREE_LU = 3.0
LOUDNESS_WINDOW_LOG_MIN_SECONDS = 5.0
LOUDNESS_WIDE_LRA_FLAG_LU = 15.0

# VALUES CHANGE HERE (media.py, dialogue loudness: a sample window needs at least this many seconds of subtitled speech, and a file at least this many such windows, before its loudness is measured on dialogue; subtitle lines closer together than the gap in seconds are joined; time limits for the header read and the subtitle extract in seconds; largest subtitle text read in bytes; seconds and most entries the dialogue choice is kept)
DIALOGUE_MIN_WINDOW_SECONDS = 8.0
DIALOGUE_MIN_WINDOWS = 2
DIALOGUE_MERGE_GAP_SECONDS = 0.5
DIALOGUE_HEADER_TIMEOUT_SECONDS = 30.0
DIALOGUE_EXTRACT_TIMEOUT_SECONDS = 180.0
DIALOGUE_SUBTITLE_MAX_BYTES = 8 * 1024 * 1024
DIALOGUE_CACHE_SECONDS = 600.0
DIALOGUE_CACHE_MAX_ENTRIES = 64

# VALUES CHANGE HERE (media.py, schedule: seconds within which the same catch-up walk asked again is answered from the last walk, the longest a file may be and still count as a commercial in seconds, and the most commercial breaks in one half hour)
CATCH_UP_REUSE_SECONDS = 2.0
COMMERCIAL_MAX_SECONDS = 5 * 60
BREAK_MAX_PER_HALF_HOUR = 2

# VALUES CHANGE HERE (media.py, settings save: a change to retro_config.json is written by the settings writer thread at most this many seconds after it was asked for, and every change asked for in that time goes into the same write (owner 2026-11-02: 2 seconds); one [SETTINGS SAVE] summary line is written at most once per report seconds)
SETTINGS_SAVE_DELAY_SECONDS = 2.0
SETTINGS_SAVE_REPORT_SECONDS = 60.0
# VALUES CHANGE HERE (media.py, N13 owner 2026-11-03: the settings text is made in pieces of at most this many schedule entries, and the screen gets a turn between pieces, so a big library never holds the picture for the whole save)
SETTINGS_SAVE_PIECE_ENTRIES = 500

# ============================================================================
# SECTION: Menu and guide drawing values (read by ui.py)
# ============================================================================

# VALUES CHANGE HERE (ui.py, channel scan bar: height and moving head in pixels, halo mix)
CHANNEL_SCAN_BAR_HEIGHT_PX = 3
CHANNEL_SCAN_BAR_HEAD_PX = 6
CHANNEL_SCAN_BAR_HALO_MIX = 0.35
# VALUES CHANGE HERE (Channels-tab scan bar: narrowest the bar is drawn while its channel still has files to scan, in pixels; a music station lists hundreds of songs and few are scanned, so its true share is under one pixel (owner log 2026-11-06))
CHANNEL_SCAN_BAR_MIN_PX = 12

# VALUES CHANGE HERE (ui.py, menus: shrink of every menu while bezel style 1 or 2 is on; the menu canvas width and height in pixels)
MENU_FIT_SHRINK_FACTOR = 0.94
MENU_CANVAS_W = 1036
MENU_CANVAS_H = 680

# VALUES CHANGE HERE (ui.py, TV guide: half hours the guide spans (336 = 7 days) and half hours before now it may scroll back; side padding of a channel name cell; seconds a drawn channel row stays valid and most rows kept)
GUIDE_TOTAL_HALF_HOURS = 336
GUIDE_PAST_HALF_HOURS = 48
NAME_CELL_SIDE_PAD = 10
GUIDE_ROW_CACHE_TTL_S = 20.0
GUIDE_ROW_CACHE_MAX = 24

# VALUES CHANGE HERE (ui.py, Server Mode toggle: False greys it out in Advanced Settings (the row can still be reached with the keys and controller but cannot be turned on or off); True lets it be switched again)
SERVER_TOGGLE_ENABLED = False
# VALUES CHANGE HERE (Server mode: True drops every scheduled entry a server sends that points at a network share (\\host\share) or a web address, so a server on the network can never make this PC reach out to another machine; False accepts them)
REMOTE_SCHEDULE_BLOCKS_NETWORK_PATHS = True

# VALUES CHANGE HERE (ui.py, controls splash: gap between a row's keys, between key columns and between columns, and the extra height of a section title, in pixels)
CONTROLS_ROW_KEY_GAP_PX = 48
CONTROLS_COL_KEY_GAP_PX = 40
CONTROLS_COL_GAP_PX = 90
CONTROLS_SECTION_EXTRA_PX = 22

# VALUES CHANGE HERE (ui.py, controls splash key pictures: height of every key in pixels (a long key such as ENTER is as tall and wider); the space between keys shown together; the space between rows; the height of the small keys in the footer; how solid a pixel must be (0 to 255) to count as part of a key when its picture is trimmed to the key's own outline; a picture this many times wider than tall is a long key, anything narrower is drawn exactly square)
CONTROLS_KEY_H_PX = 54
CONTROLS_KEY_GAP_PX = 10
CONTROLS_KEY_ROW_GAP_PX = 14
CONTROLS_FOOTER_KEY_H_PX = 30
CONTROLS_KEY_ALPHA_CUTOFF = 200
CONTROLS_LONG_KEY_MIN_RATIO = 1.25

# VALUES CHANGE HERE (ui.py and game_deck.py, hint lines with key pictures (popups, menu rows, the game menu legend): a key is this many times as tall as the hint's text; the space between a key and its words; the space between one key-and-words pair and the next, in pixels)
CONTROLS_HINT_KEY_SCALE = 1.45
CONTROLS_HINT_KEY_WORD_GAP_PX = 8
CONTROLS_HINT_GROUP_GAP_PX = 26

# VALUES CHANGE HERE (ui.py, controller pictures: how tall the long controller buttons (SELECT, START) are drawn, as a share of a key's height, so they sit beside the round buttons at a matching size)
CONTROLS_PAD_LONG_BUTTON_SCALE = 0.62

# VALUES CHANGE HERE (ui.py, TV guide and controls splash: channel rows shown in 16:9 and in 4:3, time columns in 16:9 and in 4:3, the guide header, clock and smallest font sizes, the controls splash body font size, width of an icon key, and the gaps under the title, between a section title and its body and between sub rows, in pixels)
GUIDE_VISIBLE_ROWS_16_9 = 8
GUIDE_VISIBLE_ROWS_4_3 = 5
GUIDE_NUM_COLS_16_9 = 5
GUIDE_NUM_COLS_4_3 = 4
GUIDE_HDR_FONT_PX = 17
GUIDE_CLOCK_FONT_PX = 17
GUIDE_MIN_FONT_PX = 17
CONTROLS_BODY_FONT_PX = 22
CONTROLS_ICON_KEY_W_PX = 64
CONTROLS_TITLE_SECTION_GAP_PX = 18
CONTROLS_SECTION_BODY_GAP_PX = 24
CONTROLS_SUB_BODY_GAP_PX = 20

# VALUES CHANGE HERE (ui.py, settings menu: how far the left and right toggle columns of the options page sit from their column start, the server picker's visible rows, and how far the channel grid moves left and its left column moves back right, in pixels)
OPTIONS_LEFT_TOGGLE_OFFSET_PX = 370
OPTIONS_RIGHT_TOGGLE_OFFSET_PX = 260
SERVER_PICKER_VISIBLE_ROWS = 6
CHANNEL_GRID_GROUP_SHIFT_LEFT_PX = 40
CHANNEL_GRID_LEFT_COL_SHIFT_PX = 50

# VALUES CHANGE HERE (ui.py, TV guide in 4:3 (used by the guide drawing and by the channel name length limit, which must agree): clock cell padding, header cell padding, gap between the two clocks in pixels, and the header-to-clock font size ratio)
GUIDE_43_CLOCK_CELL_PAD_PX = 10
GUIDE_43_HDR_CELL_PAD_PX = 12
GUIDE_43_CLOCK_PAIR_GAP_PX = 8
GUIDE_43_HDR_TO_CLOCK_RATIO = 22.0 / 17.0

# VALUES CHANGE HERE (ui.py, settings menu layout in pixels: options toggle box width; remote remap label column, key box width and height, gap between the two columns; channel settings header gap, bottom space, smallest top and title top; Holiday rows spread evenly between the title and the Close button (top and bottom space, smallest and largest row height, row padding, toggle width, gap); Video tab slider row space, largest and smallest slider spacing; Theme tab last block space, largest and smallest block spacing, smallest first gap and its trim)
OPTIONS_TOGGLE_BOX_W_PX = 18 + 24
REMOTE_REMAP_LABEL_COL_W_PX = 195
REMOTE_REMAP_KEY_BOX_W_PX = 110
REMOTE_REMAP_KEY_BOX_H_PX = 30
REMOTE_REMAP_COL_GAP_PX = 30
CHANNEL_SETTINGS_HEADER_GAP_PX = 40
CHANNEL_SETTINGS_BOTTOM_RESERVED_PX = 70
CHANNEL_SETTINGS_MIN_TOP_PX = 20
CHANNEL_SETTINGS_TITLE_TOP_PX = 20
HOLIDAY_ROWS_TOP_PX = 60
HOLIDAY_ROWS_BOTTOM_PX = 62
HOLIDAY_ROW_MIN_H_PX = 34
HOLIDAY_ROW_MAX_H_PX = 56
HOLIDAY_ROW_SLOT_PAD_PX = 8
HOLIDAY_TOGGLE_W_PX = 190
HOLIDAY_TOGGLE_GAP_PX = 12
VIDEO_SLIDER_ROW_RESERVE_PX = 40
VIDEO_SLIDER_SPACING_MAX_PX = 70
VIDEO_SLIDER_SPACING_MIN_PX = 40
THEME_SLIDER_LAST_BLOCK_RESERVE_PX = 50
THEME_SLIDER_SPACING_MAX_PX = 88
THEME_SLIDER_SPACING_MIN_PX = 50
THEME_SLIDER_FIRST_GAP_MIN_PX = 38
THEME_SLIDER_FIRST_GAP_TRIM_PX = 18

# ============================================================================
# SECTION: Menu navigation values (read by menu_nav.py)
# ============================================================================

# VALUES CHANGE HERE (menu_nav.py, double presses in milliseconds: a second press of one key with no release between counts as the same press inside the first window; the same press arriving by a different key code counts as one inside the second)
NAV_DUP_HELD_MS = 250
NAV_DUP_PATH_MS = 50

# VALUES CHANGE HERE (menu_nav.py, highlight speed limit: the fewest milliseconds between two W/A/S/D highlight moves made by separate presses in the menu, the TV guide and the file explorer; a press that comes sooner, or a second press in the same frame, is thrown away rather than saved; a held key keeps its own repeat pace from HOLD_RULES)
NAV_MOVE_MIN_MS = 150

# VALUES CHANGE HERE (menu_nav.py, sliders: one left or right press moves SLIDER_STEP; each slider is (config key, value when unset, lowest, highest); the Reactive Strength slider on a music channel's editor the same way; the fastest a value control (slider, volume) may step in milliseconds, however fast the key is pressed or held, kept below the held-key repeat intervals in HOLD_RULES so a hold is never dropped)
SLIDER_STEP = 5
SLIDER_SPECS = {
    "brightness": ("brightness", 50, 0, 100), "contrast": ("contrast", 50, 0, 100), "color": ("color", 50, 0, 100),
    "sharpness": ("sharpness", 50, 0, 100), "tint": ("tint", 50, 0, 100),
    "opacity": ("menu_opacity", 50, 0, 100), "bg_hue": ("theme_bg_hue", 220, 0, 360), "ui_hue": ("theme_ui_hue", 140, 0, 360),
}
REACTIVE_STRENGTH_SPEC = ("reactive_visualizer_strength", 50, 0, 100)
VALUE_STEP_MIN_MS = 100

# VALUES CHANGE HERE (menu_nav.py, held keys: each rule is (milliseconds before the first repeat, None for the menu's own delay; milliseconds between repeats; True when a hold of exactly that delay already counts); milliseconds after which a hold with no key-up is treated as stuck; percent one volume press or one repeat moves)
HOLD_RULES = {
    "vertical": (None, 100),
    "horizontal": (None, 110),
    "rom_list": (380, 70, True),
    "gamepad": (400, 180, True),
    "volume": (100, 160),
}
HOLD_STUCK_MS = {"menu": 1500, "guide": 3000, "rom_list": 1500, "volume": 1500}
VOLUME_STEP = 5

# VALUES CHANGE HERE (menu_nav.py, file explorer mouse: rows one wheel step scrolls, double-click time in ms, pixels before a drag starts, edge zone in pixels where a drag scrolls, slow and fast drag scroll steps in ms, smallest scrollbar thumb, scrollbar width and padding in pixels)
EXPLORER_WHEEL_ROWS = 3
EXPLORER_DOUBLE_CLICK_MS = 500
EXPLORER_DRAG_START_PX = 4
EXPLORER_DRAG_EDGE_ZONE_PX = 50
EXPLORER_DRAG_SCROLL_SLOW_MS = 120
EXPLORER_DRAG_SCROLL_FAST_MS = 60
EXPLORER_THUMB_MIN_H = 20
EXPLORER_SB_WIDTH = 8
EXPLORER_SB_PAD = 4

# VALUES CHANGE HERE (menu_nav.py, controller in menus: the stick axis that moves up and down and the one that moves left and right; the buttons that accept and the buttons that go back)
JOY_VERTICAL_AXIS = 1
JOY_HORIZONTAL_AXIS = 0
JOY_ACCEPT_BUTTONS = (0, 7)
JOY_BACK_BUTTONS = (1, 6)

# VALUES CHANGE HERE (menu_nav.py, channel number entry: milliseconds a single typed digit waits for a second one)
DIGIT_ENTRY_SINGLE_MS = 2500

# VALUES CHANGE HERE (menu_nav.py, name entry: most characters of a channel name when the guide gives no limit, and of a save slot name)
NAME_ENTRY_CHANNEL_MAX_DEFAULT = 18
NAME_ENTRY_SLOT_MAX = 24

# VALUES CHANGE HERE (menu_nav.py, DVD skip in milliseconds: one tap's jump, each step while held, how close to the real end a skip may land, the pause between skips, and how long a press must be held before it repeats)
DVD_SKIP_TAP_MS = 30000
DVD_SKIP_HOLD_MS = 5000
DVD_SKIP_END_GUARD_MS = 10000
DVD_SKIP_COOLDOWN_MS = 400
DVD_SKIP_HOLD_START_MS = 500

# ============================================================================
# SECTION: Game screen values (read by game_deck.py)
# ============================================================================

# VALUES CHANGE HERE (game_deck.py, controller mapping screen: where the left button column ends and the right one starts, as a share of the panel width; gap between the options, space above them under the wireframe, the NES lift as a share of the height, and the drop for every console but N64, in pixels)
GAME_MAP_LEFT_COLUMN_EDGE_FRAC = 0.22
GAME_MAP_RIGHT_COLUMN_EDGE_FRAC = 0.78
GAME_MAP_OPTION_GAP_PX = 40
GAME_MAP_OPTIONS_TOP_GAP_PX = 14
GAME_MAP_NES_OPTIONS_LIFT_FRAC = 0.04
GAME_MAP_OPTIONS_DROP_PX = 20

# VALUES CHANGE HERE (game_deck.py, key legend at the bottom of the game screens: a text row height that nothing reads (left for the owner's yes, R5), bottom padding in pixels, how much darker the key names are than the titles (0 to 255), the height of its key pictures and the space between its rows in pixels)
GAME_LEGEND_ROW_H_PX = 20
GAME_LEGEND_BOTTOM_PAD_PX = 8
GAME_LEGEND_KEY_DARKEN = 60
GAME_LEGEND_KEY_H_PX = 22
GAME_LEGEND_ROW_GAP_PX = 3
# VALUES CHANGE HERE (game_deck.py, key legend: pixels between one action's section and the next)
GAME_LEGEND_SECTION_GAP_PX = 36

# VALUES CHANGE HERE (game_deck.py, console picker and ROM list in pixels: size of the console switch arrows, their space beside the logo, how far past the Back column the ROM list arrows sit, the space under the ROM list and the gap of its arrows from the middle; the ROM list logo box width and height (on the menu canvas, scaled with it), how much taller it is, and its top space)
GAME_NAV_ARROW_SIZE_PX = 15
GAME_CONSOLE_ARROW_PAD_PX = 46
GAME_ROMLIST_ARROW_PAST_BACK_PX = 140
GAME_ROMLIST_BOTTOM_SPACE_PX = 90
GAME_ROMLIST_ARROW_GAP_PX = 46
GAME_ROMLIST_LOGO_BOX_W_PX = 226
GAME_ROMLIST_LOGO_BOX_H_PX = 72
GAME_ROMLIST_LOGO_HEADER_TALLER = 1.35
GAME_ROMLIST_LOGO_TOP_PX = 20

# VALUES CHANGE HERE (game_deck.py, ROM archives: most entries read from one archive, largest single file taken out of it in bytes, highest compression ratio allowed (safety caps against broken or hostile archives))
GAME_ARCHIVE_MAX_ENTRIES = 20000
GAME_ARCHIVE_MAX_ENTRY_BYTES = 8 * 1024 ** 3
GAME_ARCHIVE_MAX_COMPRESSION_RATIO = 300

# VALUES CHANGE HERE (game_deck.py, a game core that finishes loading after its loading screen gave up: seconds it is waited for so it can be unloaded, and seconds between checks)
GAME_LATE_CORE_LOAD_WAIT_SECONDS = 600
GAME_LATE_CORE_LOAD_POLL_SECONDS = 1.0

# VALUES CHANGE HERE (game_deck.py, game loading screen: how fast the bar fills toward 100 % in ms (bigger is slower) for a listed console and for every other one, ms it holds at 100 % before the game shows, and ms a core gets to draw its first picture before the load is given up, for a listed console and for every other one)
GAME_LOAD_BAR_TAU_MS = {"PSX": 9000}
GAME_LOAD_BAR_TAU_MS_DEFAULT = 1200
GAME_LOAD_HOLD_AT_100_MS = 350
GAME_FIRST_FRAME_GIVEUP_MS = {"PSX": 75000}
GAME_FIRST_FRAME_GIVEUP_MS_DEFAULT = 20000

# VALUES CHANGE HERE (game_deck.py, game picture and timing: share of the bezel screen a handheld picture fills, and most game frames run in one pass to catch up after a slow frame)
GAME_HANDHELD_BEZEL_FIT_MARGIN = 0.90
GAME_MAX_CATCHUP_FRAMES = 4

# VALUES CHANGE HERE (game_deck.py, game menus: most archives whose contents are remembered, ms between live rescans of the ROM folders, ms a console switch arrow flashes, seconds after one remap capture before the next is taken, and ms a message takes to fade out)
GAME_ARCHIVE_CACHE_MAX_ENTRIES = 5000
GAME_LIVE_RESCAN_INTERVAL_MS = 1500
GAME_CONSOLE_ARROW_FLASH_MS = 220
GAME_REMAP_CAPTURE_COOLDOWN_SECONDS = 0.30
GAME_MESSAGE_FADE_MS = 500

# VALUES CHANGE HERE (game_deck.py, controller mapping screen value boxes in pixels: width, extra height over the text, and gap from the button name)
GAME_MAP_VALUE_BOX_W_PX = 56
GAME_MAP_VALUE_BOX_PAD_PX = 6
GAME_MAP_VALUE_BOX_GAP_PX = 6

# VALUES CHANGE HERE (game_deck.py, DVD player in ms: how long the loading screen shows, how long a custom splash picture shows, longest a splash video plays, how long a disc image gets to open, the wait after a failed open before it is called failed, the wait before a menu change is believed, and how long an end state lasts before the player goes back to the list)
DVD_LOAD_SCREEN_MS = 1600
DVD_CUSTOM_SPLASH_MS = 3000
DVD_SPLASH_VIDEO_MAX_MS = 60000
DVD_ISO_OPEN_TIMEOUT_MS = 9000
DVD_ISO_OPEN_FAIL_GRACE_MS = 700
DVD_MENU_FLIP_DEBOUNCE_MS = 400
DVD_END_STATE_GRACE_MS = 2500

# ============================================================================
# SECTION: Values read by the smaller scripts (visualizers.py, libretro_core.py, media_library.py, audio_analysis.py)
# ============================================================================

# VALUES CHANGE HERE (visualizers.py, real-music visualizers: seconds a level takes to rise toward a louder reading, and to fall toward a quieter one; beat glow fade seconds)
VIS_REACT_ATTACK_SECONDS = 0.035
VIS_REACT_RELEASE_SECONDS = 0.16
VIS_REACT_BEAT_FADE_SECONDS = 0.18

# VALUES CHANGE HERE (visualizers.py, the space left above the Equalizer Grid's tallest bar, in pixels)
VIS_EQ_TOP_MARGIN_PX = 10

# VALUES CHANGE HERE (visualizers.py, every style: how far the animation clock moves each frame, how fast the Lite (no real music) level follows the music, and how many particles (Particle Flow) and stars (Neon Starfield) there are)
VIS_FRAME_TIME_STEP = 0.04
VIS_LITE_LEVEL_SMOOTH = 0.15
VIS_PARTICLE_COUNT = 200
VIS_STAR_COUNT = 200

# VALUES CHANGE HERE (visualizers.py, Prism Shards: mirror slices, shards and turning speed; Liquid Chrome: balls, grid columns across the screen (more is sharper and slower to draw) and the balls' fastest drift per frame)
VIS_PRISM_SLICES = 8
VIS_PRISM_SHARDS = 22
VIS_PRISM_TURN_SPEED = 0.15
VIS_CHROME_BALLS = 7
VIS_CHROME_GRID_COLUMNS = 108
VIS_CHROME_BALL_SPEED = 1.6

# VALUES CHANGE HERE (visualizers.py, Pulse Rings: the most frames between rings in Lite, frames before a ring is sent anyway when real music has no beat, how fast a ring grows per frame and its widest line; Warp Tunnel: sides of each ring and rings in the tunnel)
VIS_PULSE_LITE_SLOWEST_FRAMES = 16
VIS_PULSE_REACTIVE_IDLE_FRAMES = 90
VIS_PULSE_RING_SPEED = 4.0
VIS_PULSE_RING_WIDTH = 10
VIS_TUNNEL_SIDES = 6
VIS_TUNNEL_RINGS = 16

# VALUES CHANGE HERE (visualizers.py, Glow Braid: points along each strand, strands in Lite and Standard and with the full spectrum in Performance, glow and bright line widths; Equalizer Grid: bars and the gap between dots in pixels; Lightning: how much a bolt and the sky flash fade per frame)
VIS_BRAID_POINTS = 220
VIS_BRAID_STRANDS = 3
VIS_BRAID_STRANDS_FULL = 6
VIS_BRAID_GLOW_WIDTH = 5
VIS_BRAID_LINE_WIDTH = 2
VIS_EQ_BARS = 24
VIS_EQ_DOT_GAP_PX = 3
VIS_LIGHTNING_FADE_PER_FRAME = 0.06
VIS_LIGHTNING_FLASH_FADE_PER_FRAME = 0.08

# VALUES CHANGE HERE (libretro_core.py, game audio in sample frames: frames sent to the mixer at a time, the queue it aims for, the most it may hold, the frames gathered before playback starts; the largest speed nudge that keeps the queue level (0.004 = 0.4 %); the game volume multiplier)
LIBRETRO_AUDIO_OUT_CHUNK = 1024
LIBRETRO_AUDIO_TARGET_OUT = 4096
LIBRETRO_AUDIO_MAX_OUT = 12288
LIBRETRO_AUDIO_PREBUFFER = 3072
LIBRETRO_AUDIO_DRC_RANGE = 0.004
LIBRETRO_AUDIO_LOUDNESS_GAIN = 0.45

# VALUES CHANGE HERE (libretro_core.py, game core: seconds between repeats of one throttled warning; the most audio frames one core call may hand over before it is refused as broken; the widest and tallest picture a core may send, in pixels; the default enlargement of the pixel-art scaler)
LIBRETRO_WARN_THROTTLE_SECONDS = 5.0
LIBRETRO_AUDIO_MAX_BATCH_FRAMES = 500_000
LIBRETRO_MAX_FRAME_DIM = 4096
LIBRETRO_PIXEL_ART_SCALE_FACTOR = 2

# VALUES CHANGE HERE (media_library.py, burned-in subtitle check: frame size read; where in the file the frames are taken (shares of the length) and the seconds used when the length is unknown; time limit per frame; the lower share of the picture searched; brightest and darkest levels that count as text and outline, outline reach in pixels; a text row's least text share and colour changes; a text band's least and most height, least width, centre range, least letter clusters, most fill and contrast; how far bands may move between frames; least frames with text, most bands per frame, frames that end the check early, and the share of frames that must agree)
BURNED_IN_FRAME_W = 480
BURNED_IN_FRAME_H = 270
BURNED_IN_SAMPLE_FRACTIONS = (0.06, 0.14, 0.22, 0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78, 0.86, 0.94)
BURNED_IN_FALLBACK_OFFSETS_S = (20.0, 60.0, 120.0, 180.0, 240.0, 300.0)
BURNED_IN_FRAME_TIMEOUT_S = 20.0
BURNED_IN_REGION_TOP_FRAC = 0.55
BURNED_IN_BRIGHT_MIN = 205
BURNED_IN_DARK_MAX = 75
BURNED_IN_OUTLINE_REACH_PX = 2
BURNED_IN_ROW_MIN_TEXT_FRAC = 0.025
BURNED_IN_ROW_MIN_TRANSITIONS = 10
BURNED_IN_BAND_MIN_H_FRAC = 0.018
BURNED_IN_BAND_MAX_H_FRAC = 0.10
BURNED_IN_BAND_MIN_WIDTH_FRAC = 0.12
BURNED_IN_BAND_CENTER_RANGE = (0.25, 0.75)
BURNED_IN_BAND_MIN_GLYPH_CLUSTERS = 6
BURNED_IN_BAND_MAX_FILL = 0.50
BURNED_IN_BAND_CONTRAST_RATIO = 4.0
BURNED_IN_POSITION_TOLERANCE_FRAC = 0.05
BURNED_IN_MIN_POSITIVE_FRAMES = 3
BURNED_IN_MAX_BANDS_PER_FRAME = 3
BURNED_IN_EARLY_STOP_FRAMES = 4
BURNED_IN_MIN_AGREEING_SHARE = 0.6

# VALUES CHANGE HERE (audio_analysis.py, music analysis (raise ANALYSIS_VERSION in audio_analysis.py after changing any number here so cached tracks are rebuilt): sample rate, frames per second, FFT size, spectrum bands and their lowest and highest Hz, top of the low and mid bands in Hz, most seconds analysed, frames per chunk, beat flux bands, frames between beats, beat peak radius in frames)
AUDIO_ANALYSIS_SAMPLE_RATE = 22050
AUDIO_ANALYSIS_FPS = 25
AUDIO_ANALYSIS_FFT_SIZE = 2048
AUDIO_ANALYSIS_SPECTRUM_BINS = 24
AUDIO_ANALYSIS_SPECTRUM_LOW_HZ = 40.0
AUDIO_ANALYSIS_SPECTRUM_HIGH_HZ = 10000.0
AUDIO_ANALYSIS_BAND_LOW_TOP_HZ = 250.0
AUDIO_ANALYSIS_BAND_MID_TOP_HZ = 2500.0
AUDIO_ANALYSIS_MAX_ANALYZED_SECONDS = 1500
AUDIO_ANALYSIS_CHUNK_FRAMES = 1000
AUDIO_ANALYSIS_BEAT_FLUX_BANDS = 16
AUDIO_ANALYSIS_BEAT_REFRACTORY_FRAMES = 5
AUDIO_ANALYSIS_BEAT_PEAK_RADIUS_FRAMES = 2

# VALUES CHANGE HERE (audio_analysis.py, how the music numbers are scaled and beats are picked (raise ANALYSIS_VERSION in audio_analysis.py after changing any number here so cached tracks are rebuilt): the quiet and loud percentiles each band is stretched between and the smallest span in dB; the curve on the spectrum and the 3 bands and on the overall level; a beat must rise this many spreads plus this floor above the local average, its strength is scaled by this share of the given percentile of all rises, and a beat is never weaker than the minimum strength)
AUDIO_ANALYSIS_NORMALISE_FLOOR_PERCENTILE = 5
AUDIO_ANALYSIS_NORMALISE_CEILING_PERCENTILE = 98
AUDIO_ANALYSIS_NORMALISE_MIN_SPAN_DB = 6.0
AUDIO_ANALYSIS_SPECTRUM_GAMMA = 1.3
AUDIO_ANALYSIS_LEVEL_GAMMA = 1.2
AUDIO_ANALYSIS_BEAT_THRESHOLD_SPREAD = 1.0
AUDIO_ANALYSIS_BEAT_THRESHOLD_FLOOR = 0.004
AUDIO_ANALYSIS_BEAT_SCALE_PERCENTILE = 95
AUDIO_ANALYSIS_BEAT_SCALE_SHARE = 0.5
AUDIO_ANALYSIS_BEAT_MIN_STRENGTH = 0.3

# VALUES CHANGE HERE (audio_analysis.py, analysis housekeeping: decode time limit and seconds before a failed track is tried again; tracks kept in memory, tracks waiting at most, most cache files, seconds an idle analysis thread waits for work before it stops)
AUDIO_ANALYSIS_DECODE_TIMEOUT_SECONDS = 240
AUDIO_ANALYSIS_FAILED_RETRY_SECONDS = 600
AUDIO_ANALYSIS_MEMORY_TRACKS = 4
AUDIO_ANALYSIS_MAX_QUEUED_TRACKS = 3
AUDIO_ANALYSIS_MAX_CACHE_FILES = 400
AUDIO_ANALYSIS_WORKER_IDLE_SECONDS = 30.0

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
        except Exception as _swallowed_error:
            _log_swallowed("_read_ram_gb: class _MemStatus(ctypes.Structure): _fields_ = [('dwLength',", _swallowed_error)
    else:
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            return (os.sysconf("SC_PHYS_PAGES") * page / 1024 ** 3,
                    os.sysconf("SC_AVPHYS_PAGES") * page / 1024 ** 3)
        except Exception as _swallowed_error:
            _log_swallowed("_read_ram_gb: page = os.sysconf('SC_PAGE_SIZE')", _swallowed_error)
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
    elif profile.tier == "WEAK":
        reserve = WEAK_HEADROOM_THREADS
    else:
        reserve = max(HEADROOM_MIN_THREADS[mode], int(-(-logical * HEADROOM_FRACTION[mode] // 1)))
    return max(AFFORDABLE_THREADS_FLOOR, logical - reserve)


def core_budget(profile, mode=None):
    mode = _limit_mode("scan", _resolve_mode(mode))
    if profile.tier == "WEAK":
        return max(CORE_BUDGET_MIN, float(profile.effective_physical - WEAK_HEADROOM_THREADS))
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
    except Exception as _swallowed_error:
        _log_swallowed("apply_cv2_threads: import cv2", _swallowed_error)
        return None


def vlc_decoder_threads(profile=None):
    """Video decoder thread count for the VLC engine, chosen from the PC's effective physical cores."""
    profile = profile or get_profile()
    value = int(profile.effective_physical) - VLC_AVCODEC_THREADS_RESERVED_CORES
    value = max(VLC_AVCODEC_THREADS_MIN, min(VLC_AVCODEC_THREADS_MAX, value))
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


def quiet_lane_bonus(lane, level=0):
    """Extra jobs a lane may run while the PC is quiet at load level 0, else 0."""
    # WARNING: the bonus is read when a job is admitted, so a job already running is never stopped; the quiet test (SCAN_QUIET_MAX_SYSTEM_CPU_PCT for SCAN_QUIET_AFTER_SECONDS) is what keeps the extra job from adding to a busy PC.
    bonus = QUIET_LANE_BONUS.get(lane, 0)
    if bonus <= 0 or int(level) != 0:
        return 0
    return bonus if pc_is_quiet() else 0


def lane_slots(lane, level, profile, mode=None):
    factor = effective_factor(level, profile, mode)
    if factor <= 0.0:
        return 0
    return max(1, int(lane_ceiling(lane, mode) * factor)) + quiet_lane_bonus(lane, level)


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
    bonus = quiet_lane_bonus("loudness_and_bake", current_level() if enforcing() else 0)
    value = min(value + bonus, lane_ceiling("loudness_and_bake", mode) + bonus)
    if cap is not None:
        value = min(value, int(cap))
    return max(1, value)


def quiet_seconds(now=None):
    """Seconds the PC has been quiet (load level 0, low CPU use, light frames), 0 when it is busy or the governor is not enforcing."""
    throttle = _gate_state["throttle"]
    if throttle is None or not enforcing():
        return 0.0
    return throttle.quiet_seconds(now)


def pc_is_quiet(now=None):
    """True when the PC has been quiet for SCAN_QUIET_AFTER_SECONDS, so the scans may speed up."""
    return quiet_seconds(now) >= SCAN_QUIET_AFTER_SECONDS


_scan_turns = {}
_scan_turns_lock = threading.Lock()


def scan_report_head(kind, rank, now=None):
    """Records how urgent a scan's next file is, as (scan class, tier), or None when the scan has nothing waiting."""
    now = time.monotonic() if now is None else now
    with _scan_turns_lock:
        entry = _scan_turns.setdefault(kind, {"rank": None, "reported": now, "progress": now})
        if rank is not None and entry["rank"] is None:
            entry["progress"] = now
        entry["rank"] = None if rank is None else tuple(rank)
        entry["reported"] = now


def scan_note_progress(kind, now=None):
    """Records that a scan finished a file."""
    now = time.monotonic() if now is None else now
    with _scan_turns_lock:
        _scan_turns.setdefault(kind, {"rank": None, "reported": now, "progress": now})["progress"] = now


def _scan_more_urgent(kind, rank, now):
    """The other scans whose next file is in a more urgent scan class than rank and that are still reporting and finishing files."""
    if rank is None:
        return []
    out = []
    with _scan_turns_lock:
        for other, entry in _scan_turns.items():
            if other == kind or entry["rank"] is None:
                continue
            if now - entry["reported"] > SCAN_TURN_STALE_SECONDS or now - entry["progress"] > SCAN_TURN_STUCK_SECONDS:
                continue
            if entry["rank"][0] < rank[0]:
                out.append(other)
    return out


def scan_should_yield(kind, rank, now=None):
    """True when another scan has a more urgent file waiting, so this scan leaves the shared scan threads to it for now."""
    return bool(_scan_more_urgent(kind, rank, time.monotonic() if now is None else now))


def scan_is_most_urgent(kind, now=None):
    """True when this scan's next file is at least as urgent as every other scan's, so it may use the scan threads the others are leaving."""
    now = time.monotonic() if now is None else now
    with _scan_turns_lock:
        mine = _scan_turns.get(kind, {}).get("rank")
    return mine is not None and not _scan_more_urgent(kind, mine, now) and not any(
        e["rank"] is not None and e["rank"][0] == mine[0] and other != kind and now - e["reported"] <= SCAN_TURN_STALE_SECONDS
        for other, e in list(_scan_turns.items()))


_game_state = {"active": False, "ticks": collections.deque(), "behind_since": None, "steady_since": None}
_game_lock = threading.Lock()


def set_game_active(active):
    """Tells the governor whether a game core is running (loaded and not paused)."""
    with _game_lock:
        if bool(active) != _game_state["active"]:
            _game_state["active"] = bool(active)
            _game_state["ticks"].clear()
            _game_state["behind_since"] = None
            _game_state["steady_since"] = None


def game_active():
    return _game_state["active"]


def note_game_tick(caught_up, now=None):
    """Records one drawn game frame and whether the core had to run extra frames to catch up."""
    now = time.monotonic() if now is None else now
    with _game_lock:
        ticks = _game_state["ticks"]
        ticks.append((now, bool(caught_up)))
        while ticks and now - ticks[0][0] > GAME_BEHIND_WINDOW_SECONDS:
            ticks.popleft()
        behind = len(ticks) >= 10 and sum(1 for _t, c in ticks if c) > GAME_BEHIND_CATCHUP_SHARE * len(ticks)
        if behind:
            if _game_state["behind_since"] is None:
                _game_state["behind_since"] = now
                log.info("[GOVERNOR] the game is falling behind: background scans wait until it is steady for %.0fs", GAME_STEADY_SECONDS)
            _game_state["steady_since"] = None
        elif _game_state["behind_since"] is not None:
            if _game_state["steady_since"] is None:
                _game_state["steady_since"] = now
            elif now - _game_state["steady_since"] >= GAME_STEADY_SECONDS:
                _game_state["behind_since"] = None
                _game_state["steady_since"] = None
                log.info("[GOVERNOR] the game is steady again: background scans continue at the game pace")


def game_needs_all(now=None):
    """True while a game is running and falling behind (or has not yet been steady for GAME_STEADY_SECONDS since)."""
    with _game_lock:
        return _game_state["active"] and _game_state["behind_since"] is not None


def ffmpeg_scans_running():
    """How many ffmpeg scans are running right now, priority work included."""
    with _activity._lock:
        return sum(_activity._inflight.get(lane, 0) for lane in FFMPEG_SCAN_LANES)


def scan_threads(profile=None, mode=None):
    """How many of the scans (duration, picture box, loudness, visualizer music) may work on a not-airing-now file at the same time on this PC, in this mode, at this load level; 1 means they share one thread."""
    profile = profile or get_profile()
    count = int(core_budget(profile, mode) // max(0.1, SCAN_THREADS_CORES_PER_THREAD))
    count = min(count, affordable_threads(profile, mode), SCAN_THREADS_MAX)
    level = current_level() if enforcing() else 0
    if profile.tier == "WEAK":
        # WARNING: a weak PC uses the cores the boot scan found free (owner 2026-11-04: "if those cores are sitting unused, use them"); only the load level narrows them, not the weak and Lite caps, so it runs on every free core while the picture keeps up and drops to one as soon as it does not.
        count = int(count * LEVEL_FACTOR[max(0, min(MAX_LEVEL, int(level)))])
    else:
        count = int(count * effective_factor(level, profile, mode))
    if game_active():
        count = min(count, GAME_SCAN_THREADS)
    return max(1, count)


# VALUES CHANGE HERE (scan throttle: the scans that decode video or audio and so share the PC's scan capacity, scan_threads(); every other scan only counts; the longest pause after a file in seconds; the pause after a file at each load level 0 to 4, for the files behind the next 3 shows; the share of that pause the next 3 shows get)
SCAN_HEAVY_KINDS = ("picture_box", "loudness", "music")
SCAN_GAP_MAX_SECONDS = 1.0
SCAN_GAP_BY_LEVEL = (0.0, 0.25, 0.5, 1.0, 1.0)
SCAN_GAP_NEXT_SHARE = 0.5

_slot_cv = threading.Condition()
_slot_active = [0]
_slot_waiting = []
_slot_seq = [0]
_slot_tls = threading.local()


@contextlib.contextmanager
def scan_slot_idle():
    """For a scan that holds one of the PC's scan places and has to wait on another thread (a lock, a semaphore, the gate): gives the place to the next file while it waits and takes it back afterwards without waiting for it; does nothing when the thread holds no place."""
    # WARNING: a scan that sits blocked behind another thread is not using the PC, so its place must not stay taken; without this a waiting scan kept a place and the next file could not start (owner 2026-11-05: "how do they jump to that free thread"). The place is taken back at once on return, so the PC may carry one scan more than its places for a moment, never waiting.
    if not getattr(_slot_tls, "held", False):
        yield
        return
    with _slot_cv:
        _slot_active[0] -= 1
        _slot_tls.held = False
        _slot_cv.notify_all()
    try:
        yield
    finally:
        with _slot_cv:
            _slot_active[0] += 1
            _slot_tls.held = True


_scan_throttle = {"mode": "free", "slow_since": 0.0, "stop_started": None, "stop_until": 0.0, "catchup": None}
_scan_throttle_lock = threading.Lock()


def scan_throttle_mode(now=None):
    """\"free\" while no game is falling behind, \"slow\" while one is and the scans creep, \"stopped\" when creeping was not enough; a stop lasts as long as the game took to catch up the last time."""
    # WARNING: owner order 2026-11-05: throttle before stopping, and a stop never lasts a fixed time (not 60 s): its length is how long the game took to catch up after the previous stop (at least GAME_THROTTLE_STOP_MIN_SECONDS, at most GAME_THROTTLE_STOP_MAX_SECONDS), measured here.
    now = time.monotonic() if now is None else now
    behind = bool(game_needs_all(now))
    with _scan_throttle_lock:
        state = _scan_throttle
        if not behind:
            if state["stop_started"] is not None:
                took = now - state["stop_started"]
                state["catchup"] = max(GAME_THROTTLE_STOP_MIN_SECONDS, min(GAME_THROTTLE_STOP_MAX_SECONDS, took))
            state["mode"] = "free"
            state["stop_started"] = None
        elif state["mode"] == "free":
            state["mode"] = "slow"
            state["slow_since"] = now
        elif state["mode"] == "slow" and now - state["slow_since"] >= GAME_THROTTLE_STOP_AFTER_SECONDS:
            length = state["catchup"] if state["catchup"] is not None else GAME_THROTTLE_STOP_MIN_SECONDS
            state["mode"] = "stopped"
            if state["stop_started"] is None:
                state["stop_started"] = now
            state["stop_until"] = now + length
        elif state["mode"] == "stopped" and now >= state["stop_until"]:
            state["mode"] = "slow"
            state["slow_since"] = now
        return state["mode"]


def scan_gap_seconds(scan_class, mode=None):
    """Seconds a scan rests after one file: 0 for a file airing now and on a quiet PC, otherwise the pause for the current load level (0 at level 0, up to SCAN_GAP_MAX_SECONDS), half of it for the next 3 shows."""
    # WARNING: the load level is how strained the screen thread is (frame time, busy share, lost pictures), so this pause follows the strain on this PC; it is never longer than SCAN_GAP_MAX_SECONDS, because a long pause only makes the scans fall behind the schedule.
    if scan_class == "now":
        return 0.0
    level = current_level() if enforcing() else 0
    gap = float(SCAN_GAP_BY_LEVEL[max(0, min(len(SCAN_GAP_BY_LEVEL) - 1, int(level)))])
    if scan_class != "deep":
        gap *= SCAN_GAP_NEXT_SHARE
    if gap > 0 and pc_is_quiet():
        return 0.0
    gap = min(float(SCAN_GAP_MAX_SECONDS), gap)
    if scan_throttle_mode() == "slow":
        gap = max(gap, float(GAME_THROTTLE_SLOW_GAP_SECONDS))
    return gap


@contextlib.contextmanager
def scan_thread_slot(kind, priority=False, stop=None, rank=None):
    """Holds one place of the PC's scan capacity (scan_threads(): the cores, the RAM and the load level) around one file of a scan that decodes video or audio; the most urgent waiting file takes each place as it frees; yields False only when stop is set while it waits."""
    # WARNING: there is no time limit and no yielding to another kind: a file waits only while every place is taken, and the most urgent file (the lowest rank, then the one that has waited longest) gets the next free place whatever its kind; a file airing now and a priority scope never wait, and the scans that only read a length or a header just count.
    if priority or _is_priority_context():
        yield True
        return
    if kind not in SCAN_HEAVY_KINDS:
        if stop is not None and stop.is_set():
            yield False
            return
        _activity.begin(SCAN_THREADS_LANE)
        try:
            yield True
        finally:
            _activity.end(SCAN_THREADS_LANE)
            scan_note_progress(kind)
        return
    with _slot_cv:
        _slot_seq[0] += 1
        ticket = (tuple(rank) if rank is not None else (99, 0), _slot_seq[0])
        _slot_waiting.append(ticket)
        admitted = False
        while True:
            if stop is not None and stop.is_set():
                break
            if _slot_active[0] < scan_threads() and ticket == min(_slot_waiting):
                admitted = True
                break
            _slot_cv.wait(GATE_POLL_SECONDS)
        _slot_waiting.remove(ticket)
        if admitted:
            _slot_active[0] += 1
        _slot_cv.notify_all()
    if not admitted:
        yield False
        return
    _slot_tls.held = True
    _activity.begin(SCAN_THREADS_LANE)
    try:
        yield True
    finally:
        _activity.end(SCAN_THREADS_LANE)
        with _slot_cv:
            if getattr(_slot_tls, "held", False):
                _slot_active[0] -= 1
            _slot_tls.held = False
            _slot_cv.notify_all()
        scan_note_progress(kind)


def scan_gpu_decode_allowed(profile=None):
    """True when the picture-box scan may decode on the GPU: the switch is on, this is Windows and the PC has a real GPU of a listed tier."""
    if not SCAN_GPU_DECODE_ENABLED or os.name != "nt":
        return False
    profile = profile or get_profile()
    return str(getattr(profile, "gpu_tier", "none")) in SCAN_GPU_DECODE_TIERS


def music_scan_class(distance, falling_behind):
    """The scan class of a music song distance songs ahead of the one playing (0 = playing): "now" for the quick songs, "next" for the rest of the first wave and the wave after it, "deep" for every song beyond them, whether or not the scan is falling behind."""
    # WARNING: owner order 2026-11-06: a music station's first wave (10 songs) and the 10 after it come with the live shows; the rest of the list scans alongside at a slower rate and never ahead of a TV show that is still unscanned. Songs beyond the two waves stay "deep" even when the scan is falling behind, so 560 songs are never level with the TV shows.
    if distance < MUSIC_SCAN_QUICK_SONGS:
        return "now"
    if distance < MUSIC_SCAN_FIRST_WAVE_SONGS + MUSIC_SCAN_NEXT_WAVE_SONGS:
        return "next"
    return "deep"


def music_analysis_class(distance, falling_behind):
    """The pace class of a song's visualizer analysis: "now" for the quick songs, "next" while inside the fast window or while the analysis is falling behind, "deep" after that."""
    if distance < MUSIC_SCAN_QUICK_SONGS:
        return "now"
    if falling_behind or distance < MUSIC_SCAN_FAST_AHEAD_SONGS:
        return "next"
    return "deep"


def music_scan_falling_behind(previous, scanned_ahead):
    """True while the visualizer music scan must go fast: it starts when fewer than MUSIC_SCAN_FALL_BEHIND_SONGS of the next songs are scanned and ends once MUSIC_SCAN_FAST_AHEAD_SONGS are."""
    if scanned_ahead < MUSIC_SCAN_FALL_BEHIND_SONGS:
        return True
    if scanned_ahead >= MUSIC_SCAN_FAST_AHEAD_SONGS:
        return False
    return bool(previous)


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
        "target_width": int(GPU_SELF_CHECK_TARGET_SIZE[0]),
        "target_height": int(GPU_SELF_CHECK_TARGET_SIZE[1]),
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
# SECTION: Failed-drive values
# ============================================================================
# VALUES CHANGE HERE (milliseconds a drive must stay unresponsive, counted from when it was first marked unresponsive, before the list screen and the failed-drive banner treat it as failed)
DRIVE_FAILED_AFTER_MS = 7000


# ============================================================================
# SECTION: Probe marker rename retry values
# ============================================================================
# VALUES CHANGE HERE (how many times the probe-in-progress marker rename is tried before it gives up)
PROBE_MARKER_RENAME_ATTEMPTS = 6
# VALUES CHANGE HERE (seconds added to the wait after each refused rename: the wait after try n is this times n)
PROBE_MARKER_RENAME_STEP_SECONDS = 0.02


# ============================================================================
# SECTION: Bezel screen hole values
# ============================================================================
# VALUES CHANGE HERE (a bezel pixel counts as part of the transparent screen hole when its alpha is at or below this, 0 to 255)
BEZEL_HOLE_ALPHA_MAX = 23
# VALUES CHANGE HERE (the smallest screen hole accepted, as a share of the whole bezel image; anything smaller is treated as no hole)
BEZEL_HOLE_MIN_AREA_FRAC = 0.005


# ============================================================================
# SECTION: Bezel channel number window values
# ============================================================================
# VALUES CHANGE HERE (a bezel pixel counts as part of the black number window when red, green and blue are each at or below this, 0 to 255)
BEZEL_LED_WINDOW_DARK_MAX = 32
# VALUES CHANGE HERE (a bezel pixel must have at least this alpha to count as part of the black number window, 0 to 255)
BEZEL_LED_WINDOW_ALPHA_MIN = 200
# VALUES CHANGE HERE (the smallest black number window accepted, as a share of the whole bezel image)
BEZEL_LED_WINDOW_MIN_AREA_FRAC = 0.0005
# VALUES CHANGE HERE (how much of the window's box the black pixels must fill, 0 to 1; keeps out dark shadows and thin strips)
BEZEL_LED_WINDOW_MIN_FILL = 0.6
# VALUES CHANGE HERE (the narrowest and widest the window may be, as width divided by height)
BEZEL_LED_WINDOW_MIN_ASPECT = 1.2
BEZEL_LED_WINDOW_MAX_ASPECT = 6.0
# VALUES CHANGE HERE (the most the black number window may be tilted in the art, in degrees; a larger measured tilt is treated as level)
BEZEL_LED_WINDOW_MAX_TILT_DEG = 15.0
# VALUES CHANGE HERE (a measured tilt smaller than this, in degrees, is treated as level)
BEZEL_LED_WINDOW_MIN_TILT_DEG = 0.3


# ============================================================================
# SECTION: Holiday window values
# ============================================================================
# VALUES CHANGE HERE (how many calendar days a holiday lasts, counted back from midnight at the end of the holiday itself; 3 days is 72 hours)
HOLIDAY_WINDOW_DAYS = 3
# VALUES CHANGE HERE (a holiday whose window opens within this many days has its unscanned files scanned ahead of the deep backlog; a later holiday waits behind it)
HOLIDAY_SCAN_LEAD_DAYS = 7


# ============================================================================
# SECTION: Folder listing cache values (ROM/DVD lists and the file explorer)
# ============================================================================

# VALUES CHANGE HERE (most folder listings kept in memory; the oldest is dropped first)
LISTING_CACHE_MAX_FOLDERS = 400
# VALUES CHANGE HERE (seconds a cached listing is shown without a fresh read being forced; after this it is still shown, and re-read in the background)
LISTING_CACHE_FRESH_SECONDS = 30.0
# VALUES CHANGE HERE (how many sub-folders of a just-listed folder are read ahead)
LISTING_READ_AHEAD_FOLDERS = 12
# VALUES CHANGE HERE (most console ROM folders read ahead while the Channel 03 console list shows)
LISTING_CONSOLE_READ_AHEAD_MAX = 24
# VALUES CHANGE HERE (seconds between console ROM folder read-aheads while the console list shows)
LISTING_CONSOLE_READ_AHEAD_SECONDS = 30.0
# VALUES CHANGE HERE (seconds a "this folder has something in it" answer is remembered for the DVD list)
LISTING_DVD_HAS_CONTENT_SECONDS = 30.0
# VALUES CHANGE HERE (seconds a list can stay on the rocking wait bar before you are pushed out of the drive)
LISTING_DRIVE_PUSH_OUT_SECONDS = 6.0
# VALUES CHANGE HERE (seconds between retests of a drive that pushed you out; it is shown again once it answers)
LISTING_DRIVE_RETEST_SECONDS = 5.0
# VALUES CHANGE HERE (seconds the "why you were pushed out" banner stays up)
LISTING_PUSH_OUT_BANNER_SECONDS = 4.0


# ============================================================================
# SECTION: Guide hub fill values
# ============================================================================
# VALUES CHANGE HERE (seconds the guide keeps the hub rows it asked for, so a row drawn every frame asks the hub once)
GUIDE_HUB_FILL_CACHE_SECONDS = 2.0


# ============================================================================
# SECTION: Guide preview picture box hold values
# ============================================================================
# VALUES CHANGE HERE (milliseconds the guide preview stays black while the highlighted file's picture box has not been read yet, before it shows the full frame anyway)
GUIDE_PREVIEW_BOX_HOLD_MS = 1500
# VALUES CHANGE HERE (seconds between looks for the picture box while the guide preview is held black)
GUIDE_PREVIEW_BOX_RECHECK_SECONDS = 0.2


# ============================================================================
# SECTION: Commercial bundle values
# ============================================================================
# VALUES CHANGE HERE (seconds between commercial length bundles: 15 gives bundles of 15, 30, 45 and so on up to the 5 minute commercial limit, twenty bundles)
COMMERCIAL_BUNDLE_STEP_SECONDS = 15


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
# SECTION: Main script timers, limits and drawing numbers (read by retro_tv_emulator.py)
# ============================================================================

# VALUES CHANGE HERE (main script, logs and watchdogs: seconds between repeats of one throttled warning; seconds between re-installs of the Windows key hook; errors in a row before the playback watchdog gives up on a file; ms the main loop may go without a heartbeat before the watchdog logs it; ms VLC's position may stand still before the log says so; ms a file may be missing before the VLC log watchdog warns; seconds the guide preload may stand still before it is reported, and seconds before it may be reported again; app relaunches allowed inside the relaunch window, and that window in seconds)
THROTTLED_LOG_INTERVAL_S = 30.0
KEY_HOOK_REINSTALL_SECONDS = 45
WATCHDOG_ERROR_BAIL_AFTER = 2
HEARTBEAT_WATCHDOG_TIMEOUT_MS = 45000
VLC_LOG_STALL_WARN_MS = 30000
VLC_LOG_MISSING_FILE_GRACE_MS = 15000
GUIDE_PRELOAD_STALL_SECONDS = 2.0
GUIDE_PRELOAD_REPORT_COOLDOWN_SECONDS = 10.0
RELAUNCH_MAX_ATTEMPTS = 4
RELAUNCH_WINDOW_SECONDS = 600

# VALUES CHANGE HERE (main script, start-up: seconds a new start waits for the app already running to close before it gives up as a second copy; seconds it waits instead when Reset All Settings restarted the app, so the old copy can finish closing before its saved data is wiped)
SINGLE_INSTANCE_WAIT_SECONDS = 3.0
FRESH_START_WAIT_FOR_OLD_APP_SECONDS = 30.0

# VALUES CHANGE HERE (main script, the Reset All Settings button: seconds between its looks at the disk for game saves and the app's own work folders, so the System tab never reads the disk every frame)
RESET_DISK_CHECK_SECONDS = 2.0

# VALUES CHANGE HERE (main script, the background loudness pass over every channel's airing show: the most seconds it waits for the length and picture-box scans of the airing shows before it scans loudness anyway, and seconds between its looks)
LOUDNESS_SWEEP_WAIT_FOR_INFO_SCANS_MAX_SECONDS = 120.0
LOUDNESS_SWEEP_WAIT_RECHECK_SECONDS = 2.0

# VALUES CHANGE HERE (main script, background scan pacing: how many times slower scans go while a file plays; seconds the caption scan sleeps when it has nothing to do; seconds between the picture-box scans of two files, and its sleep when it has nothing to do; seconds before a skipped sweep is tried again; seconds a now-playing probe answer is trusted; seconds before one file's on-demand length probe may run again; how many times slower scans go while the guide is open; seconds before the upcoming-media sweep may run again; seconds between saves of its progress; seconds the guide preload waits between checks; ms a channel's ordered file list is kept; seconds a file whose loudness scan timed out waits before its next try, for the 1st, 2nd, 3rd and later timeouts)
SCAN_BACKOFF_WHILE_PLAYING_MULTIPLIER = 6.0
CAPTION_IDLE_SLEEP_SECONDS = 20.0
PICTURE_BOX_BETWEEN_FILES_SECONDS = 1.0
PICTURE_BOX_IDLE_SLEEP_SECONDS = 20.0
SWEEP_SKIP_BACKOFF_SECONDS = 1.0
PROBE_NOW_STATE_TTL_SECONDS = 3.0
ONDEMAND_DURATION_PROBE_COOLDOWN_SECONDS = 30.0
GUIDE_BACKOFF_MULTIPLIER = 2.0
UPCOMING_PROBE_RESWEEP_COOLDOWN_SECONDS = 30.0
UPCOMING_PROBE_SAVE_THROTTLE_SECONDS = 10.0
GUIDE_PRELOAD_POLL_SECONDS = 0.5

# VALUES CHANGE HERE (schedule timelines: how many built block timelines are kept so the same lineup asked for again (the screen, the guide, the scheduler's background threads) is handed back at once instead of being built again; each holds one block's entries)
TIMELINE_MEMO_SIZE = 24

# VALUES CHANGE HERE (guide while closed: the background copy of the guide is rebuilt at most once every this many seconds while the guide is not on screen, unless something asks for it (a channel edit, opening the guide); while the guide is open it is rebuilt as soon as anything changes)
GUIDE_CLOSED_REBUILD_SECONDS = 15.0

# VALUES CHANGE HERE (Python turn-taking: seconds a Python thread may run before it must give another thread a turn; the screen thread waits at most about this long for its turn behind background Python work; Python's own default is 0.005)
PYTHON_SWITCH_INTERVAL_SECONDS = 0.001
ORDERED_CONTENT_CACHE_TTL_MS = 8000
LOUDNESS_TIMEOUT_BACKOFF_SECONDS = (600, 1800, 7200, 21600)

# VALUES CHANGE HERE (main script, channel loads: seconds one load may run before it counts as stuck, is set aside so the next load can run, and the player is replaced; keep it above PICTURE_BOX_PRE_SHOW_BUDGET_S plus WEDGED_STOP_TIMEOUT_SECONDS plus a few seconds, the longest a healthy load takes; seconds between checks for a stuck load)
ACTIVATION_STUCK_SECONDS = 20.0
ACTIVATION_WATCH_CHECK_SECONDS = 0.5

# VALUES CHANGE HERE (main script, locked picture finder: how many times a frame may step around a locked picture before the error goes through; seconds the first search for what holds the lock may take on the screen thread)
SURFACE_LOCK_SWAPS_MAX = 5
SURFACE_LOCK_FIND_SECONDS = 4.0
# VALUES CHANGE HERE (surface lock recovery: seconds without a locked-screen error after which the next one counts as a new incident with its own SURFACE_LOCK_SWAPS_MAX repairs)
SURFACE_LOCK_INCIDENT_GAP_SECONDS = 10.0

# VALUES CHANGE HERE (health_monitor.py, the app's health report: on or off; ms between looks at what every thread is doing; seconds between report blocks in app_log.txt; seconds of looks kept to explain a stall; a screen-thread gap this long or longer is explained; how many gaps are explained per minute at most)
HEALTH_MONITOR_ENABLED = True
HEALTH_SAMPLE_MS = 50
HEALTH_REPORT_SECONDS = 60.0
HEALTH_RING_SECONDS = 15.0
HEALTH_EXPLAIN_GAP_MS = 100
HEALTH_EXPLAIN_MAX_PER_MINUTE = 20

# VALUES CHANGE HERE (health_monitor.py, stuck threads: seconds a thread may sit on one line of code before it is reported stuck; the shorter limit for the screen thread; seconds a short job thread may wait before it is reported; seconds between full dumps of every thread; ms a watched lock may be held before it is reported; how many places each busy thread lists)
HEALTH_STUCK_SECONDS = 10.0
HEALTH_SCREEN_STUCK_SECONDS = 3.0
HEALTH_JOB_WAIT_WARN_SECONDS = 120.0
HEALTH_DUMP_MIN_GAP_SECONDS = 60.0
HEALTH_LOCK_HOLD_WARN_MS = 500
HEALTH_PROFILE_TOP = 5

# VALUES CHANGE HERE (health_monitor.py: threads whose long waits are normal and are never reported stuck; name starts of short job threads, which are reported when they wait too long)
HEALTH_NEVER_STUCK_THREADS = ("OsKeyHookPump", "OsKeyHookWatchdog", "retro-tv-listener", "_readerthread", "_monitor", "VlcLogWriter", "SettingsWriter", "HealthMonitor", "HealthReport")
HEALTH_JOB_THREAD_STARTS = ("content-activate", "vlc-open", "vlc-stop", "pre-show-picbox", "first-scan-picbox", "prime-", "NextItemPrep", "HubRowSync", "NewChannelFirstShow", "_probe_and_patch_durations", "compressor-bake")

# VALUES CHANGE HERE (main script, playback: ms of Static between channels; ms of black while a guide preview settles; ms a guide preview may take to load; ms a playing file may stall before it is called stalled, and the longer limit for its first buffering; ms the sound is held after a seek; ms from the end that counts as ending; seconds a stuck stop may take; seconds of the short wait for a quick stop; seconds the picture-size probe may hold up an open; ms of grace for the first frame after a seek; seconds from the end a seek is kept away from; seconds an open is checked for an error, the longer window when it may be retried, seconds Stopped must last to count as a failed open, and the seconds before each retry; failed plays on one drive before that drive is held; seconds the sound is muted while stereo or mono is switched; failures before a file is set aside as unplayable; ms a channel's failure message stays; ms between sweeps for dead drives; seconds a DVD's audio track count is kept)
STATIC_TRANSITION_MS = 1500
GUIDE_PREVIEW_SETTLE_MS = 500
GUIDE_PREVIEW_LOAD_TIMEOUT_MS = 2500
PLAYBACK_STALL_TIMEOUT_MS = 10000
PLAYBACK_INITIAL_BUFFER_TIMEOUT_MS = 20000
# VALUES CHANGE HERE (extra milliseconds the last picture is held and the sound stays muted after a CHANNEL CHANGE; it stays at 500 so the audio of the old channel, channel 03's second VLC player above all, never follows the viewer to the new one)
AUDIO_SYNC_HOLD_MS = 500
# VALUES CHANGE HERE (extra milliseconds the last picture is held and the sound stays muted when a show, a break clip or the return to the show changes media on the SAME channel; 0 = none, the new media goes in at once and the mute lifts at its first picture frame; owner 2026-11-06, 14.3)
SAME_CHANNEL_AUDIO_HOLD_MS = 0
END_PROXIMITY_MS = 2000
WEDGED_STOP_TIMEOUT_SECONDS = 5
PLAY_SEGMENTED_STOP_FAST_WAIT_SECONDS = 0.05
PRE_OPEN_DIMS_PROBE_BOUND_SECONDS = 0.35
SEEK_FRAME_READY_GRACE_MS = 600
SEEK_GUARD_TAIL_MARGIN_SECONDS = 5.0
PLAY_OPEN_ERROR_CHECK_SECONDS = 0.25
PLAY_OPEN_CONFIRM_WINDOW_SECONDS = 1.5
PLAY_OPEN_STOPPED_DEBOUNCE_SECONDS = 0.15
PLAY_OPEN_RETRY_DELAYS_SECONDS = (0.5, 1.0)
PLAY_RETRY_DRIVE_BAIL_AFTER = 2
STEREO_TOGGLE_MUTE_SECONDS = 0.2
UNPLAYABLE_STRIKE_THRESHOLD = 3
CHANNEL_FAILURE_MESSAGE_MS = 30000
DEAD_DRIVE_SWEEP_INTERVAL_MS = 2000
DVD_AUDIO_TRACK_COUNT_TTL_SECONDS = 1.0

# VALUES CHANGE HERE (main script, sound levels: the music volume multiplier; the quietest dB the stacked volume curve reaches, and the VLC volume curve; the Static sound level; dB a smoothed gain may sit above the show's floor)
MUSIC_GAIN = 1.0
STACKED_GAIN_MIN_DB = -48.0
VLC_GAIN_MIN_DB = -18.0
STATIC_AUDIO_GAIN_PRECACHED = 0.25
SHOW_SMOOTH_TOLERANCE_DB = 0.3

# VALUES CHANGE HERE (main script, captions: seconds before a TV caption lookup is tried again; ms one caption may stay on screen)
TV_CAPTION_RETRY_SECONDS = 8.0
CAPTION_MAX_HOLD_MS = 7000

# VALUES CHANGE HERE (main script, picture and bezel drawing: seconds between checks of a bezel file for changes; pixels the channel number is nudged at the top; the menu's shrink inside each bezel and by default, and its extra shrink when warp is off; each bezel's channel LED box, gap, upward nudge, tilt, cover-up stretches and trims, cover-up nudge, text nudges, number scale, text scale and text gap, each with its default for a bezel not listed; the watermark's extra offset inside each bezel; how much of the frame an overlay border may fill and how much of the bezel's screen the picture may fill; CRT power-on: ms the picture grows, ms it settles, and its peak brightness; scan lines drawn on the screen)
TV_BORDER_MTIME_RECHECK_TTL_SECONDS = 2.0
CH_NUM_TOP_ALIGN_NUDGE_PX = -20
MENU_BEZEL_SHRINK = {
    "bezel":  (0.95, 0.95),
    "bezel1": (0.95, 0.95),
    "bezel2": (0.95, 0.95),
}
MENU_BEZEL_SHRINK_DEFAULT = (1.0, 1.0)
MENU_WARP_OFF_EXTRA_SHRINK = 0.97
BEZEL_CHANNEL_LED_FRAC = {
    "bezel":  (0.7010, 0.2402, 0.0676, 0.0776),
    "bezel1": (0.7801, 0.1191, 0.1263, 0.1282),
    "bezel2": (0.6954, 0.1303, 0.0231, 0.0354),
}
# VALUES CHANGE HERE (bezel number windows that are not measured from the picture because their glass is grey, not near-black; their listed numbers above are used)
BEZEL_LED_LISTED_ONLY = ("bezel2",)
BEZEL_CHANNEL_LED_GAP_FRAC = {
    "bezel":  0.28,
    "bezel1": 0.28,
}
BEZEL_CHANNEL_LED_GAP_FRAC_DEFAULT = 0.28
BEZEL_CHANNEL_LED_Y_NUDGE_FRAC = {
    "bezel":  0.0,
    "bezel1": 0.10,
}
BEZEL_CHANNEL_LED_Y_NUDGE_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_TILT_DEG = {
    "bezel":  -2.0,
    "bezel1": -2.0,
}
BEZEL_CHANNEL_LED_TILT_DEG_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_RIGHT_EXTEND_FRAC = {
    "bezel":  0.65,
    "bezel1": 0.0,
}
BEZEL_CHANNEL_LED_HIDE_RIGHT_EXTEND_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_LEFT_EXTEND_FRAC = {
    "bezel":  0.22,
    "bezel1": 0.0,
}
BEZEL_CHANNEL_LED_HIDE_LEFT_EXTEND_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_DOWN_EXTEND_FRAC = {
    "bezel":  0.65,
    "bezel1": 0.0,
}
BEZEL_CHANNEL_LED_HIDE_DOWN_EXTEND_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_BOTTOM_TRIM_FRAC = {
    "bezel":  0.061,
}
BEZEL_CHANNEL_LED_HIDE_BOTTOM_TRIM_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_RIGHT_TRIM_FRAC = {
    "bezel":  0.061,
}
BEZEL_CHANNEL_LED_HIDE_RIGHT_TRIM_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_LEFT_EXTRA_FRAC = {
    "bezel":  0.061,
    "bezel1": 0.241,
}
BEZEL_CHANNEL_LED_HIDE_LEFT_EXTRA_FRAC_DEFAULT = 0.0
BEZEL_CHANNEL_LED_HIDE_XY_NUDGE_PX = {
    "bezel1": (1, 1),
}
BEZEL_CHANNEL_LED_HIDE_XY_NUDGE_PX_DEFAULT = (0, 0)
BEZEL_CHANNEL_LED_TEXT_Y_NUDGE_PX = {
    "bezel":  2,
    "bezel1": -6,
}
BEZEL_CHANNEL_LED_TEXT_Y_NUDGE_PX_DEFAULT = 0
BEZEL_CHANNEL_LED_TEXT_X_NUDGE_PX = {
    "bezel":  0,
    "bezel1": -3,
}
BEZEL_CHANNEL_LED_TEXT_X_NUDGE_PX_DEFAULT = 0
BEZEL_SONG_CARD_LED_Y_NUDGE_PX = {
    "bezel":  4,
    "bezel1": 4,
}
BEZEL_SONG_CARD_LED_Y_NUDGE_PX_DEFAULT = 0
BEZEL_CHANNEL_LED_NUM_SCALE_FRAC = {
    "bezel":  1.0,
    "bezel1": 0.78,
}
BEZEL_CHANNEL_LED_NUM_SCALE_FRAC_DEFAULT = 1.0
BEZEL_CHANNEL_LED_TEXT_DISPLAY_SCALE_FRAC = {
    "bezel1": 0.9,
}
BEZEL_CHANNEL_LED_TEXT_DISPLAY_SCALE_FRAC_DEFAULT = 1.0
BEZEL_CHANNEL_LED_TEXT_GAP_ADJUST_PX = {
    "bezel1": -2,
}
BEZEL_CHANNEL_LED_TEXT_GAP_ADJUST_PX_DEFAULT = 0
WATERMARK_BEZEL_EXTRA_OFFSET = {
    "bezel":  (10, 10),
    "bezel1": (10, 10),
    "bezel2": (10, 10),
}
OVERLAY_BORDER_FIT_MARGIN = 0.85
BEZEL_PICTURE_FIT_MARGIN = 1.00
CRT_POWERON_EXPAND_MS = 680
CRT_POWERON_SETTLE_MS = 1000
CRT_POWERON_PEAK_ALPHA = 235
CRT_SCANLINE_TARGET_LINES = 240

# VALUES CHANGE HERE (main script, pan and scan: the motion grid's width and height; the least motion that counts; the frame difference that counts as a scene cut; ms between saves of a file's pan-and-scan memory to the library)
PAN_SCAN_GRID_W = 32
PAN_SCAN_GRID_H = 18
PAN_SCAN_MOTION_FLOOR = 0.006
PAN_SCAN_CUT_THRESHOLD = 0.09
PAN_SCAN_LIBRARY_SAVE_INTERVAL_MS = 8000

# VALUES CHANGE HERE (main script, start-up, file explorer, guide and key capture: the loading bar's centre, width and height on the loading art, and its font size; the boot progress weight of the steps that are not files; seconds the boot screen holds at 100 %; seconds between subsystem starts at boot; boot prewarm workers and seconds it waits for the settings; seconds probes get to finish at shutdown; the file explorer's title and toolbar heights; ms its left list is kept; half-hour steps the guide searches for the next show start; ms within which a second delivery of the same key press is ignored while a key is captured, and ms within which the same key from a second input path is ignored)
LOAD_BAR_CENTER_X = 1196
LOAD_BAR_CENTER_Y = 988
LOAD_BAR_WIDTH = 260
LOAD_BAR_HEIGHT = 12
LOAD_BAR_FONT_SIZE = 34
BOOT_READY_UNIT_WEIGHT = 15
BOOT_HOLD_AT_100_SECONDS = 0.5
BOOT_SUBSYSTEM_YIELD_SECONDS = 0.05
BOOT_PREWARM_WORKERS = 8
BOOT_PREWARM_SETTINGS_WAIT_SECONDS = 15.0
SHUTDOWN_PROBE_GRACE_SECONDS = 0.4
FILE_EXPLORER_TITLE_H = 44
FILE_EXPLORER_TOOLBAR_H = 40
FILE_EXPLORER_LEFT_ITEMS_TTL_MS = 2000
# VALUES CHANGE HERE (file explorer sizes: the UP button's width, height and letter size inside the toolbar bar (the bar is 40 high, so the button stays under that), the folder and file name letter size in the list, and the folder name letter size in the left panels; the rows keep their heights)
FILE_EXPLORER_UP_W = 84
FILE_EXPLORER_UP_H = 36
FILE_EXPLORER_UP_FONT = 20
FILE_EXPLORER_ITEM_FONT = 25
FILE_EXPLORER_LEFT_ITEM_FONT = 21
GUIDE_TIME_STEP_MAX_SHIFTS = 8
# VALUES CHANGE HERE (guide highlight: how many seconds apart two drawings of the same show's start may be and still count as the same stop, so the highlight does not jump back when a start time drifts a fraction of a second)
GUIDE_STOP_MATCH_SECONDS = 2.0
# VALUES CHANGE HERE (guide highlight: the seconds the drawn highlight time is nudged forward so a show start that drifted just past it still holds the highlight)
GUIDE_HIGHLIGHT_NUDGE_SECONDS = 0.75
KEY_CAPTURE_SAME_PRESS_MS = 250
KEY_CAPTURE_SECOND_PATH_MS = 600


# ============================================================================
# SECTION: Colours (read by retro_tv_emulator.py; ui.py and game_deck.py share the names that mean the same colour)
# ============================================================================

# VALUES CHANGE HERE (screen fills: the near-black every picture area is filled with (never pure black, which is the game overlay's see-through colour), the grey shown while a channel changes or buffers, and the guide preview's fill when it cannot draw)
COLOR_SCREEN_BLACK = (1, 1, 1)
COLOR_TRANSITION_GREY = (15, 15, 15)
COLOR_GUIDE_PREVIEW_ERROR_FILL = (40, 40, 40)

# VALUES CHANGE HERE (loading screen: the fill when there is no loading art, the loading bar's track and its green bar and percentage)
COLOR_LOADING_EMPTY_BG = (5, 5, 5)
COLOR_LOADING_BAR_TRACK = (20, 30, 20)
COLOR_LOADING_GREEN = (0, 255, 128)

# VALUES CHANGE HERE (text shared by the menus and popups: white, the gold of a warning headline and the red of a warning line)
COLOR_TEXT_WHITE = (255, 255, 255)
COLOR_ARCADE_GOLD = (255, 220, 0)
COLOR_ACTION_RED = (255, 60, 60)

# VALUES CHANGE HERE (banners and overlays: the text, fill and edge of the red status banner, the caption colour when a caption names none, the volume bar's track, and the screensaver's stand-in picture)
COLOR_BANNER_TEXT = (255, 255, 255)
COLOR_BANNER_FILL = (120, 20, 20, 210)
COLOR_BANNER_EDGE = (255, 90, 90, 255)
COLOR_CAPTION_DEFAULT = (255, 255, 255)
COLOR_VOLUME_OSD_TRACK = (80, 80, 80)
COLOR_SCREENSAVER_FALLBACK = (255, 255, 255, 200)

# VALUES CHANGE HERE (file explorer: grey and locked-file text, the toolbar and footer, the Up button, the path box and its edge, dividers and lines, section headers and their text, item text, the scroll track, the tint on every other row, selected-row text, the Add buttons and their hover, the remove-location sign, its hover and hover tint, the Cancel, Remove, Done buttons and hovers, the warning banner and the busy dim)
COLOR_FE_TEXT_GREY = (150, 150, 150)
COLOR_FE_LOCKED = (255, 170, 60)
COLOR_FE_BAR_BG = (10, 10, 10)
COLOR_FE_UP_BUTTON = (40, 40, 40)
COLOR_FE_PATH_BG = (18, 18, 18)
COLOR_FE_PATH_EDGE = (60, 60, 60)
COLOR_FE_DIVIDER = (50, 50, 50)
COLOR_FE_COLUMN_HEADER_BG = (15, 15, 15)
COLOR_FE_LINE = (45, 45, 45)
COLOR_FE_SECTION_HEADER_BG = (8, 8, 8)
COLOR_FE_SECTION_HEADER_TEXT = (100, 100, 100)
COLOR_FE_ITEM_TEXT = (200, 200, 200)
COLOR_FE_SCROLL_TRACK = (28, 28, 28)
COLOR_FE_ALT_ROW_TINT = (255, 255, 255, 8)
COLOR_FE_SELECTED_TEXT = (200, 225, 255)
COLOR_FE_ADD_BUTTON_HOVER = (40, 80, 150)
COLOR_FE_ADD_BUTTON = (25, 50, 95)
COLOR_FE_REMOVE_LOCATION_HOVER = (255, 120, 120)
COLOR_FE_REMOVE_LOCATION = (130, 70, 70)
COLOR_FE_REMOVE_LOCATION_HOVER_TINT = (180, 40, 40, 90)
COLOR_FE_CANCEL_HOVER = (210, 50, 50)
COLOR_FE_CANCEL = (80, 20, 20)
COLOR_FE_REMOVE_BUTTON_HOVER = (200, 100, 30)
COLOR_FE_REMOVE_BUTTON = (100, 50, 10)
COLOR_FE_DONE = (20, 70, 20)
COLOR_FE_BANNER_BG = (150, 30, 30)
COLOR_FE_BUSY_DIM = (0, 0, 0, 90)

# VALUES CHANGE HERE (game_deck.py, the colours a list or caption text can be set to in the menu, by name)
GAME_LIST_TEXT_COLORS = {
    "white":  (255, 255, 255),
    "black":  (0, 0, 0),
    "red":    (220, 60, 60),
    "orange": (230, 140, 40),
    "yellow": (230, 210, 60),
    "green":  (80, 220, 120),
    "blue":   (80, 140, 230),
    "purple": (170, 90, 220),
}

# VALUES CHANGE HERE (game_deck.py, stick deadzone screen: the stick ring's fill and edge, the deadzone ring, the threshold lines, the raw stick dot and text, and the grey of a label that is not highlighted)
COLOR_GAME_STICK_RING_FILL = (25, 38, 62)
COLOR_GAME_STICK_RING_EDGE = (90, 90, 110)
COLOR_GAME_STICK_DEADZONE_RING = (200, 160, 40)
COLOR_GAME_STICK_THRESHOLD_LINE = (70, 110, 160)
COLOR_GAME_STICK_RAW = (150, 150, 165)
COLOR_GAME_LABEL_GREY = (180, 180, 180)

# VALUES CHANGE HERE (game_deck.py, switches, sliders and buttons on the game screens: a switch or slider that is on and off, the help line, the off red, the plain button blue and the confirm blue)
COLOR_GAME_SWITCH_ON = (40, 167, 69)
COLOR_GAME_SWITCH_OFF = (45, 65, 100)
COLOR_GAME_HINT_TEXT = (120, 120, 140)
COLOR_GAME_OFF_RED = (220, 53, 69)

# VALUES CHANGE HERE (game_deck.py, player number boxes: a connected and an empty player, one waiting for a new controller, the number on a lit box, on a connected one and on an empty one)
COLOR_GAME_PLAYER_BOX_CONNECTED = (70, 70, 95)
COLOR_GAME_PLAYER_BOX_EMPTY = (35, 35, 45)
COLOR_GAME_PLAYER_BOX_ARMED = (200, 160, 30)
COLOR_GAME_PLAYER_TEXT_LIT = (20, 20, 20)
COLOR_GAME_PLAYER_TEXT_CONNECTED = (200, 200, 210)
COLOR_GAME_PLAYER_TEXT_OFF = (90, 90, 95)

# VALUES CHANGE HERE (game_deck.py, controller mapping screen: the missing-wireframe box fill, edge, headline and hint, a value box while it waits for a press and normally, a value box edge, the line and dot to the button on the picture, the controller pick button, and the remap help line)
COLOR_GAME_WIREFRAME_MISSING_FILL = (40, 50, 40)
COLOR_GAME_WIREFRAME_MISSING_EDGE = (60, 80, 60)
COLOR_GAME_WIREFRAME_MISSING_TEXT = (100, 120, 100)
COLOR_GAME_WIREFRAME_MISSING_HINT = (60, 80, 60)
COLOR_GAME_VALUE_BOX_ACTIVE = (90, 30, 30)
COLOR_GAME_VALUE_BOX = (60, 60, 80)
COLOR_GAME_VALUE_BOX_EDGE = (120, 120, 140)
COLOR_GAME_HOTSPOT_LINE = (60, 200, 90)
COLOR_GAME_HOTSPOT_DOT = (90, 255, 130)
COLOR_GAME_PICK_BUTTON = (45, 45, 55)
COLOR_GAME_REMAP_HINT = (120, 120, 60)

# VALUES CHANGE HERE (game_deck.py, ROM list and loading screens: warning text, the loading message, bar track and percentage, a console arrow that cannot switch, the no-files line, a missing ROM selected and not selected, the busy dim, the popup help line, the in-game message text and the controller-disconnected banner fill)
COLOR_GAME_WARNING_TEXT = (255, 90, 90)
# VALUES CHANGE HERE (Games: a console that can use a BIOS (PSX) launched with no BIOS set up shows a red banner; False still starts the game with the core's built-in BIOS, True shows the banner and does not start it)
GAME_NO_BIOS_BLOCKS_LAUNCH = False
COLOR_GAME_LOADING_TEXT = (150, 150, 150)
COLOR_GAME_LOADING_BAR_TRACK = (22, 22, 22)
COLOR_GAME_LOADING_PERCENT = (80, 80, 80)
COLOR_GAME_ARROW_DISABLED = (90, 90, 90)
COLOR_GAME_NO_FILES_TEXT = (200, 80, 80)
COLOR_GAME_MISSING_ROM_SELECTED = (255, 150, 90)
COLOR_GAME_MISSING_ROM = (200, 100, 60)
COLOR_GAME_BUSY_DIM = (0, 0, 0, 90)
COLOR_GAME_POPUP_HINT = (160, 160, 160)
COLOR_GAME_OSD_TEXT = (255, 255, 180)
COLOR_GAME_DISCONNECT_FILL = (120, 20, 20, 225)

# VALUES CHANGE HERE (ui.py, menu buttons and rows: a plain button, a greyed one and its text, a blue button and its dimmed form while another capture runs, the button blue that is on and its edge, a button that is off, the off red and on green of a switch, a switch that is off and greyed, the switch track when off, and a row that is off)
COLOR_MENU_BUTTON = (40, 78, 145)
COLOR_MENU_BUTTON_GREYED = (32, 32, 40)
COLOR_MENU_TEXT_GREYED = (105, 105, 115)
COLOR_MENU_BUTTON_BLUE = (40, 78, 145)
COLOR_MENU_BUTTON_BLUE_DIM = (32, 58, 105)
COLOR_MENU_ON_BLUE = (40, 100, 180)
COLOR_MENU_ON_BLUE_EDGE = (60, 140, 220)
COLOR_MENU_OFF_BUTTON = (35, 45, 60)
COLOR_MENU_OFF_RED = (220, 53, 69)
COLOR_MENU_ON_GREEN = (40, 167, 69)
# VALUES CHANGE HERE (ui.py, the commercials and holiday sub-menus, owner 2026-11-03: the rows are this share of the menu width, centred side to side, with this gap in pixels between rows, and the block of rows sits centred between the title and the bottom buttons)
SUB_MENU_ROW_WIDTH_SHARE = 0.6
SUB_MENU_ROW_GAP_PX = 40

# VALUES CHANGE HERE (one look for the same kind of button everywhere, owner 2026-11-03: Close and No the same red; Clear Media a darker red; Yes green; every add-media button a darker green than Yes)
COLOR_BUTTON_CLOSE = (220, 53, 69)
COLOR_BUTTON_NO = (220, 53, 69)
COLOR_BUTTON_YES = (40, 167, 69)
COLOR_BUTTON_CLEAR = (130, 28, 38)
COLOR_BUTTON_ADD = (22, 100, 42)
# VALUES CHANGE HERE (the small Add box beside a toggle: watermark Add and the game music Add, owner 2026-11-03: black)
COLOR_BUTTON_TOGGLE_ADD = (0, 0, 0)
# VALUES CHANGE HERE (the audio track button of every channel, owner 2026-11-03: yellow and the dark text on it; while it cannot be used it takes the same greyed look as every other button)
COLOR_BUTTON_AUDIO_TRACK = (230, 190, 40)
COLOR_BUTTON_AUDIO_TRACK_TEXT = (20, 20, 20)
# VALUES CHANGE HERE (where the shared buttons sit in every settings menu and popup: Close at the bottom right, Clear Media at the bottom left, as (inset from the panel side, up from the panel bottom, width, height); Yes left and No right of the popup's centre by the offset, both the given size)
MENU_CLOSE_BUTTON_PLACE = (25, 52, 110, 40)
MENU_CLEAR_BUTTON_PLACE = (25, 52, 240, 40)
POPUP_YES_NO_OFFSET = 100
POPUP_YES_NO_SIZE = (110, 36)
COLOR_MENU_SWITCH_OFF = (45, 65, 100)
COLOR_MENU_SWITCH_GREYED = (35, 35, 45)
COLOR_MENU_TRACK_OFF = (55, 55, 60)

# VALUES CHANGE HERE (ui.py, menu text: a value in mint, grey for a disabled label, a lighter disabled grey, the off-label grey, dim text, help lines, the warning red, the flash message red, the last-item grey, text on a lit box, the section label, and greyed text and its focused form)
COLOR_MENU_VALUE_MINT = (200, 255, 200)
COLOR_MENU_DISABLED_GREY = (100, 100, 100)
COLOR_MENU_TEXT_DISABLED_LIGHT = (140, 140, 140)
COLOR_MENU_OFF_LABEL_GREY = (110, 110, 110)
COLOR_MENU_DIM_TEXT = (150, 160, 180)
COLOR_MENU_HINT_TEXT = (120, 120, 140)
COLOR_MENU_WARNING_RED = (255, 80, 80)
COLOR_MENU_FLASH_TEXT = (255, 90, 90)
COLOR_MENU_LAST_ITEM_TEXT = (90, 90, 90)
COLOR_MENU_TEXT_ON_LIT = (20, 20, 20)
COLOR_MENU_SECTION_LABEL = (120, 120, 120)
COLOR_MENU_TEXT_GREYED_LILAC = (130, 130, 150)
COLOR_MENU_TEXT_GREYED_FOCUS = (160, 160, 175)

# VALUES CHANGE HERE (ui.py, menu boxes and edges: the value box, a swatch edge, a greyed edge, the dim grey box, the padlock, a locked switch's on colour and knob, a greyed knob, a greyed toggle and its edge, and the holiday row)
COLOR_MENU_VALUE_BOX = (10, 10, 10)
COLOR_MENU_SWATCH_EDGE = (90, 90, 100)
COLOR_MENU_EDGE_GREYED = (45, 45, 55)
COLOR_MENU_DIM_GREY = (60, 60, 80)
COLOR_MENU_PADLOCK = (80, 80, 80)
COLOR_MENU_LOCKED_SWITCH_ON = (55, 90, 55)
COLOR_MENU_LOCKED_SWITCH_KNOB = (130, 130, 130)
COLOR_MENU_KNOB_GREYED = (80, 80, 90)
COLOR_MENU_TOGGLE_GREYED = (22, 22, 30)
COLOR_MENU_TOGGLE_GREYED_EDGE = (150, 150, 170)

# VALUES CHANGE HERE (ui.py, clear buttons and key capture: the clear button, its text and dimmed form, a key box that clashes with another key and its text, and a key box waiting for a press)
COLOR_MENU_KEY_CONFLICT_FILL = (120, 20, 20)
COLOR_MENU_KEY_CONFLICT_TEXT = (255, 100, 100)
COLOR_MENU_KEY_CAPTURE_FILL = (90, 30, 30)

# VALUES CHANGE HERE (ui.py, cards, radio buttons and the server picker: a card, its edge, a greyed card and edge, card text and greyed card text, an unnamed slot, a pending delete, the radio fill, edge and selected edge, the server-not-found line, a locked cell, its text and edge, the SERVER label, the lock dim and lock text)
COLOR_MENU_CARD = (35, 45, 65)
COLOR_MENU_CARD_EDGE = (90, 90, 110)
COLOR_MENU_CARD_GREYED = (24, 27, 34)
COLOR_MENU_CARD_EDGE_GREYED = (60, 62, 70)
COLOR_MENU_CARD_TEXT = (210, 210, 220)
COLOR_MENU_CARD_TEXT_GREYED = (95, 97, 105)
COLOR_MENU_UNNAMED_TEXT = (110, 112, 120)
COLOR_MENU_DELETE_ARMED = (60, 25, 25)
COLOR_MENU_RADIO_FILL = (15, 15, 20)
COLOR_MENU_RADIO_EDGE = (140, 140, 160)
COLOR_MENU_RADIO_SELECTED_EDGE = (90, 160, 230)
COLOR_MENU_NO_SERVER_TEXT = (200, 160, 60)
COLOR_MENU_LOCKED_CELL = (48, 48, 55)
COLOR_MENU_LOCKED_TEXT = (150, 150, 160)
COLOR_MENU_EDGE_LOCKED = (145, 145, 155)
COLOR_MENU_SERVER_LABEL = (175, 175, 185)
COLOR_MENU_LOCK_DIM = (0, 0, 0, 145)
COLOR_MENU_LOCK_TEXT = (190, 190, 200)

# VALUES CHANGE HERE (ui.py, game, DVD and save profile rows: the player square, the player row, the disc edge and greyed disc, disc text that is off, the profile box focused and not and its edge, a step focused and its edge, the splash file name when splash is off, the dot ring and edge)
COLOR_MENU_PLAYER_SQUARE = (80, 220, 120)
COLOR_MENU_PLAYER_ROW = (25, 35, 55)
COLOR_MENU_DISC_EDGE = (40, 60, 90)
COLOR_MENU_DISC_GREYED = (20, 20, 28)
COLOR_MENU_DISC_TEXT_OFF = (120, 120, 130)
COLOR_MENU_PROFILE_FOCUS = (30, 50, 30)
COLOR_MENU_PROFILE = (20, 30, 50)
COLOR_MENU_PROFILE_EDGE = (50, 70, 100)
COLOR_MENU_STEP_FOCUS = (34, 34, 46)
COLOR_MENU_STEP_FOCUS_EDGE = (110, 110, 125)
COLOR_MENU_SPLASH_FILE_OFF = (75, 75, 82)
COLOR_MENU_DOT_RING = (170, 255, 195)
COLOR_MENU_DOT_EDGE = (90, 150, 220)

# VALUES CHANGE HERE (ui.py, popups and the DVD button-mapping popup: the hint line, the sub line and its dimmer form, the check box, and the mapping popup's off red, panel and key box)
COLOR_MENU_POPUP_HINT = (160, 160, 160)
COLOR_MENU_POPUP_SUBTEXT = (200, 200, 210)
COLOR_MENU_POPUP_SUBTEXT_DIM = (140, 140, 150)
COLOR_MENU_CHECKBOX = (200, 200, 200)
COLOR_MENU_DVD_MAP_OFF_RED = (150, 45, 45)
COLOR_MENU_DVD_MAP_PANEL = (16, 22, 40)
COLOR_MENU_DVD_MAP_KEY = (12, 16, 30)

# VALUES CHANGE HERE (visualizers.py: the Equalizer Grid's bottom, middle and top colours and its falling peak dot, and how much of the Glow Braid trail fades each frame (the last number, 0 to 255; bigger fades faster))
COLOR_VIS_EQ_BOTTOM = (255, 60, 170)
COLOR_VIS_EQ_MID = (0, 210, 220)
COLOR_VIS_EQ_TOP = (210, 230, 60)
COLOR_VIS_EQ_PEAK = (255, 240, 80)
COLOR_VIS_GLOW_BRAID_FADE = (0, 0, 0, 16)

# VALUES CHANGE HERE (the theme used when the saved theme cannot be read: menu colour, background and text)
DEFAULT_THEME = {'ui': (0, 255, 128), 'bg': (10, 15, 30), 'text': (255, 255, 255)}

# VALUES CHANGE HERE (media.py, the menu themes by name: menu colour, background and text; DefaultBlueGreen is the theme above)
COLOR_THEMES = {
    "DefaultBlueGreen": dict(DEFAULT_THEME),
    "Classic Amber": {"ui": (255, 170, 0), "bg": (15, 10, 0), "text": (240, 220, 180)},
}

# ============================================================================
# SECTION: Picture shapes
# ============================================================================
# VALUES CHANGE HERE (the picture shapes Normal (Fit to Screen) lays a picture out as, widest first: 2.39 scope film, 2:1, 16:9, 3:2, 4:3, 1:1 square, 4:5 portrait, 9:16 phone; a picture takes the nearest one, so the most any common picture is stretched or squeezed is about 9 %)
# WARNING: a shape wider than halfway between 4:3 and 16:9 is drawn on the whole Bezel screen with Bezel on, a narrower one in the true 4:3 box (_bezel_video_wants_tube_fit); a new shape goes with the group of the one it sits nearest.
PICTURE_STANDARD_ASPECTS = (2.39, 2.0, 16.0 / 9.0, 3.0 / 2.0, 4.0 / 3.0, 1.0, 4.0 / 5.0, 9.0 / 16.0)


# ============================================================================
# SECTION: Game picture shapes
# ============================================================================
# VALUES CHANGE HERE (the shape a console's picture is laid out as in Normal, width divided by height; a console not listed keeps the shape of its own frame)
GAME_DISPLAY_ASPECTS = {"NES": 4.0 / 3.0, "SNES": 4.0 / 3.0}


# ============================================================================
# SECTION: Picture box before the show
# ============================================================================
# VALUES CHANGE HERE (media.py, the Video tab's picture adjustments done inside VLC, owner 2026-11-03 option 1: degrees of hue turned for each Tint step away from 50; VLC sharpen strength for each Sharpness step above 50; VLC blur strength (sigma) for each Sharpness step below 50)
VLC_TINT_HUE_DEGREES_PER_STEP = 0.6
VLC_SHARPEN_SIGMA_PER_STEP = 0.02
VLC_BLUR_SIGMA_PER_STEP = 0.02

# VALUES CHANGE HERE (retro_tv_emulator.py, N16 owner 2026-11-03: a station file whose open waited at least this many seconds after its start position was worked out (picture box, size probe, stop) opens that much further in, so it joins where the clock is)
OPEN_WAIT_JOIN_CLOCK_MIN_SECONDS = 0.5

# VALUES CHANGE HERE
PICTURE_BOX_PRE_SHOW_BUDGET_S = 8.0
PICTURE_BOX_PRE_SHOW_PARALLEL = 6

# VALUES CHANGE HERE (next-item prep: when a file starts on the channel on air, the picture boxes of the next this-many items of its block are scanned ahead, nearest first, as priority work, so each one opens without a picture-box wait; on a PC whose plan runs first-start scans in sequence it first waits up to the wait seconds for the playing file's own first-start scans)
NEXT_ITEM_PREP_ITEMS = 3
NEXT_ITEM_PREP_FIRST_START_WAIT_SECONDS = 60.0
# VALUES CHANGE HERE (N16 owner 2026-11-03: when the next items are a commercial break, the look-ahead keeps going through the whole break and the item after it, up to this many items in all; while a break is on or next it does not wait for the playing file's first-start scans, since its items are seconds apart)
NEXT_ITEM_PREP_BREAK_MAX_ITEMS = 12


# ============================================================================
# SECTION: Picture box, flat-bar detection
# ============================================================================
# VALUES CHANGE HERE (a bar is an edge strip of one flat colour in every sample; the frame is read at this size, flat means the pixel levels in the strip differ by no more than the tolerance, a sample whose picture is thinner than the fraction of the frame height is not used)
PICTURE_BOX_FLAT_WIDTH = 640
PICTURE_BOX_FLAT_HEIGHT = 480
PICTURE_BOX_FLAT_TOLERANCE = 12
PICTURE_BOX_FLAT_FRAMES = 2
PICTURE_BOX_FLAT_MIN_PICTURE_FRACTION = 0.125

# VALUES CHANGE HERE (media_library.py, picture-box scan: where in the file the samples are taken as shares of its length, the seconds into the file used when its length is unknown, seconds decoded per sample and the most seconds one sample may take, the fewest good samples for an answer, ffmpeg cropdetect's black level, and the smallest bar as a share of the frame and in pixels)
# WARNING: raise PICTURE_BOX_DETECTOR_VERSION in media_library.py when one of these changes, so stored picture boxes are scanned again under the new numbers.
PICTURE_BOX_SAMPLE_FRACTIONS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)
PICTURE_BOX_FALLBACK_OFFSETS_S = (30.0, 120.0, 300.0)
PICTURE_BOX_SAMPLE_SECONDS = 2.0
PICTURE_BOX_SAMPLE_TIMEOUT_S = 25.0
PICTURE_BOX_MIN_GOOD_SAMPLES = 3
PICTURE_BOX_CROPDETECT_LIMIT = 32
PICTURE_BOX_MIN_BAR_FRACTION = 0.015
PICTURE_BOX_MIN_BAR_PX = 4

# VALUES CHANGE HERE (media_library.py, seconds between the two size reads that tell whether a file is still being copied in)
LIBRARY_GROWING_FILE_CHECK_DELAY_S = 0.05


# ============================================================================
# SECTION: First-start probe of pending scheduled files
# ============================================================================
# VALUES CHANGE HERE (pending scheduled files are probed in the background, this many at a time; a file whose probe found nothing is tried again after the retry seconds; a channel's request is dropped when its schedule has not asked again for the stale seconds)
# VALUES CHANGE HERE (tune-in loudness scans run at priority; this many may run at once, the channel on screen plus one it just left)
LOUDNESS_PRIME_AT_ONCE = 2
PENDING_SCHEDULE_PROBE_PARALLEL = 2
# VALUES CHANGE HERE (pending-file probe: a file whose length was just stored is not probed and stored again for this many seconds)
PENDING_PROBE_RESTORE_HOLD_SECONDS = 60.0
PENDING_SCHEDULE_PROBE_RETRY_SECONDS = 5.0
PENDING_SCHEDULE_PROBE_REQUEST_STALE_SECONDS = 30.0

# VALUES CHANGE HERE (pending-file probe priority: the first this-many files of a channel's request are needed soon and probe at priority, every channel's next file before any channel's second; every file after them is backlog, probed whenever background scans are not paused and no live open is in flight)
PENDING_PROBE_URGENT_COUNT = 4
# VALUES CHANGE HERE (pending-file probe: seconds between stores of one block's backlog lengths; each store makes that block's lineup be built again, so they are stored together; a length needed soon is stored at once)
PENDING_PROBE_STORE_BATCH_SECONDS = 15.0

# VALUES CHANGE HERE (quiet PC: when the load level is 0, the whole PC's CPU use is at or below the percent and the app's frame time is at or below the share of its frame budget for the seconds given, the scans speed back up; their pause is multiplied by the quiet multiplier until the PC is busy again)
SCAN_QUIET_MAX_SYSTEM_CPU_PCT = 40.0
SCAN_QUIET_MAX_BUSY_RATIO = 0.6
SCAN_QUIET_AFTER_SECONDS = 20.0
SCAN_QUIET_PACE_MULTIPLIER = 0.5
# VALUES CHANGE HERE (quiet PC: extra jobs a lane may run at the same time while the PC is quiet and the load level is 0; the extra job is not started when the PC is busy, and the lane falls back to its normal count as soon as the quiet ends)
QUIET_LANE_BONUS = {"loudness_and_bake": 1}

# VALUES CHANGE HERE (scan threads: the duration, picture-box, loudness and visualizer music scans each have their own thread, but only this many of them may work on a not-airing-now file at the same time: the scan cores of this PC and mode divided by the cores per thread, never above the PC's affordable threads or the maximum, cut by the load-level factor, never below 1; 1 means the scans take turns on one shared thread)
SCAN_THREADS_CORES_PER_THREAD = 1.0
SCAN_THREADS_MAX = 4
SCAN_THREADS_LANE = "scan_threads"

# VALUES CHANGE HERE (scan turns: each scan (duration, picture box, loudness, music) reports how urgent its next file is (its scan class: airing now, upcoming commercials, the next shows, the deep backlog); a scan whose next file is less urgent than another scan's waits for its turn instead of taking a shared scan thread; a report older than the stale seconds, or a scan that has finished no file for the stuck seconds, does not make others wait)
SCAN_TURN_STALE_SECONDS = 10.0
SCAN_TURN_STUCK_SECONDS = 90.0

# VALUES CHANGE HERE (ffmpeg scan lanes that count toward one overall cap: background scan work may not start while this many scans are already running as the threads the app may use in this mode; priority work is never held but is counted)
FFMPEG_SCAN_LANES = ("probe_and_duration", "loudness_and_bake", "cropdetect_threads", "music_loudness")

# VALUES CHANGE HERE (games: while a game core runs, the scans keep going on this many shared scan threads in total; the game counts as falling behind when more than the share of its frames in the last window seconds needed catching up, and then background scans wait until it has been steady for the steady seconds)
GAME_SCAN_THREADS = 1
GAME_BEHIND_WINDOW_SECONDS = 5.0
GAME_BEHIND_CATCHUP_SHARE = 0.10
GAME_STEADY_SECONDS = 10.0
# VALUES CHANGE HERE (scan throttle while a game falls behind: the pause after each scanned file while the scans creep, in seconds; how long the scans creep before they stop; the shortest and the longest a stop may last, a stop lasts as long as the game took to catch up the last time)
GAME_THROTTLE_SLOW_GAP_SECONDS = 2.0
GAME_THROTTLE_STOP_AFTER_SECONDS = 10.0
GAME_THROTTLE_STOP_MIN_SECONDS = 3.0
GAME_THROTTLE_STOP_MAX_SECONDS = 30.0

# VALUES CHANGE HERE (GPU help for scans: when the PC has a real GPU of a listed tier, the picture-box scan decodes its video on the GPU with this ffmpeg hwaccel, which takes the decode work off the CPU; seconds allowed for the one-time check that this ffmpeg has the hwaccel; GPU decodes that may fail in a run before the scan goes back to CPU decoding for the rest of the run; the loudness, duration and music scans read audio or file headers only and have nothing for the GPU to do)
SCAN_GPU_DECODE_ENABLED = True
SCAN_GPU_DECODE_TIERS = ("dedicated", "onboard")
SCAN_GPU_DECODE_HWACCEL = "d3d11va"
SCAN_GPU_DECODE_CHECK_TIMEOUT_SECONDS = 5.0
SCAN_GPU_DECODE_FAILURE_LIMIT = 3

# VALUES CHANGE HERE (visualizer music scans: the first this-many songs ahead are scanned with no pause because songs are short; scans stay fast until this many songs ahead are scanned, then slow to the deep pace; when fewer than the fall-behind count of the next songs are scanned the scan is falling behind and goes fast again; the visualizer analysis keeps the fast count of songs ahead analyzed and refills when fewer than the fall-behind count are left)
MUSIC_SCAN_QUICK_SONGS = 3
MUSIC_SCAN_FAST_AHEAD_SONGS = 10
MUSIC_SCAN_FALL_BEHIND_SONGS = 5
# VALUES CHANGE HERE (music scan classes: a music station's first wave of songs, then the wave after it, are scanned as "next" with the TV shows coming up; every song beyond them is "deep" and scans alongside at the slower deep pace)
MUSIC_SCAN_FIRST_WAVE_SONGS = 10
MUSIC_SCAN_NEXT_WAVE_SONGS = 10

# VALUES CHANGE HERE (audio tracks: languages picked first, in order, when a file has no saved track choice (matched against the track's language name or code); milliseconds the track list must stay unchanged after a file starts before the pick is made, and the most milliseconds to wait for that; seconds the Audio Track menu keeps re-reading the track list while it is open, and how often)
AUDIO_TRACK_PREFERRED_LANGUAGES = ("english", "eng", "en")
AUDIO_TRACK_SETTLE_MS = 600
AUDIO_TRACK_SETTLE_MAX_MS = 2500
AUDIO_TRACK_PICKER_REFRESH_SECONDS = 15.0
AUDIO_TRACK_PICKER_REFRESH_INTERVAL_SECONDS = 0.5

# VALUES CHANGE HERE (the first-start scans of the playing file, picture box and loudness, start together only when the PC can spare this many scan cores and threads and its load level is no higher than the limit; otherwise the picture box runs first and loudness follows; the priority disk slots are what those together-scans may hold at once)
FIRST_START_CONCURRENT_MIN_CORES = 3.0
FIRST_START_CONCURRENT_MIN_THREADS = 4
FIRST_START_CONCURRENT_MAX_LEVEL = 2
FIRST_START_PRIORITY_DISK_SLOTS = 3


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
        self._unattributed_target = 0
        self._unattributed_reason = ""
        self._last_unattributed_log = 0.0
        self._quiet_since = None
        self._quiet_announced = False
        self._own_load = {}
        self._own_load_logged = {}
        self._top_since = None
        self._step_check = None
        self._last_ratio = None
        self._last_own_note = 0.0
        self._own_note = ""
        self._relax_cooldown = RELAX_COOLDOWN_SECONDS
        self._last_step_down = None
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

    def _busy_ratios(self, signals):
        """(busy share, worst share) of the frame time from the signals; the shares come from the feed, or from the averages and this throttle's frame budget."""
        ratio = signals.get("busy_ratio")
        worst = signals.get("busy_worst_ratio")
        budget = self.frame_budget_ms
        if ratio is None and signals.get("busy_avg_ms") is not None and budget > 0:
            ratio = signals["busy_avg_ms"] / budget
        if worst is None and signals.get("busy_worst_ms") is not None and budget > 0:
            worst = signals["busy_worst_ms"] / budget
        return ratio, worst

    def own_load(self, kind):
        """The learned share of the frame time this screen kind uses by itself, or None before it is learned."""
        return self._own_load.get(str(kind or "screen"))

    def _learn_own_load(self, signals, now):
        """Learns the screen's own load from a calm reading (no background work, or the top level held long enough), and lowers it at once on any lighter reading."""
        ratio, _worst = self._busy_ratios(signals)
        if ratio is None:
            return
        kind = str(signals.get("busy_kind") or "screen")
        own = self._own_load.get(kind)
        top_settled = (self.level >= MAX_LEVEL and self._top_since is not None
                       and now - self._top_since >= OWN_LOAD_SETTLE_SECONDS)
        step = self._step_check
        no_gain = (step is not None and now - step[0] >= OWN_LOAD_SETTLE_SECONDS
                   and ratio > step[1] - OWN_LOAD_MIN_GAIN)
        if own is not None and ratio < own:
            new = ratio
            how = "lighter reading"
        elif signals.get("background_active") is False or top_settled or no_gain:
            new = ratio if own is None else own + OWN_LOAD_LEARN_RATE * (ratio - own)
            if signals.get("background_active") is False:
                how = "no background work"
            elif top_settled:
                how = "all background work held"
            else:
                how = "narrowing to level %d did not lower it (%d%% before)" % (self.level, round(step[1] * 100))
        else:
            return
        self._own_load[kind] = new
        logged = self._own_load_logged.get(kind)
        if logged is None or (abs(new - logged[0]) >= OWN_LOAD_LOG_STEP
                              and now - logged[1] >= OWN_LOAD_LOG_INTERVAL_SECONDS):
            self._own_load_logged[kind] = (new, now)
            log.info("[GOVERNOR] screen's own load %s=%d%% of its frame time (%s)", kind, round(new * 100), how)

    def _target(self, signals):
        target = 0
        reasons = []
        immediate = False
        self._own_note = ""
        ratio, worst = self._busy_ratios(signals)
        if ratio is not None:
            kind = str(signals.get("busy_kind") or "screen")
            own = self._own_load.get(kind)
            pressure = ratio
            if own is not None:
                excess = ratio - own
                min_excess = max(OWN_LOAD_MIN_EXCESS, own * OWN_LOAD_MIN_EXCESS_SHARE)
                pressure = min(ratio, 1.0 + excess) if excess >= min_excess else 0.0
                if pressure == 0.0 and ratio >= BUSY_RATIO_LEVELS[0][0]:
                    self._own_note = "%s busy=%d%% own=%d%%" % (kind, round(ratio * 100), round(own * 100))
            lvl = 0
            for threshold, level in BUSY_RATIO_LEVELS:
                if pressure >= threshold:
                    lvl = level
            if lvl and worst is not None and worst - (own or 0.0) >= WORST_BUMP_RATIO:
                lvl = min(MAX_LEVEL, lvl + 1)
            if lvl:
                reasons.append("busy=%d%%%s" % (round(ratio * 100),
                                                (" own=%d%%" % round(own * 100)) if own is not None else ""))
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
            if now >= self._event_until:
                self._learn_own_load(signals, now)
            self._last_ratio = self._busy_ratios(signals)[0]
            target, reason, immediate = self._target(signals)
            note_tune_steady(now >= self._event_until and self._last_ratio is not None
                             and self._last_ratio < BUSY_RATIO_LEVELS[0][0] and not signals.get("lost_pictures"), now)
            if (self._last_step_down is not None and now - self._last_step_down >= RELAX_RELAPSE_SECONDS):
                self._last_step_down = None
                if self._relax_cooldown > RELAX_COOLDOWN_SECONDS:
                    self._relax_cooldown = max(RELAX_COOLDOWN_SECONDS, self._relax_cooldown / 2.0)
                    log.info("[GOVERNOR] the last step down held %.0fs; calm wait before a step down now %.0fs",
                             RELAX_RELAPSE_SECONDS, self._relax_cooldown)
            self._note_quiet(signals, target, now)
            if (self._own_note and signals.get("background_active")
                    and now - self._last_own_note >= ATTRIBUTION_LOG_INTERVAL_SECONDS):
                self._last_own_note = now
                log.info("[GOVERNOR] frames over their time from the screen's own work, background work not narrowed: %s",
                         self._own_note)
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
                if immediate:
                    self._set_level(target, "tighten: " + reason, now)
                    self._over_since = None
                elif now - self._over_since >= SUSTAIN_SECONDS:
                    # WARNING: one level per SUSTAIN_SECONDS of pressure, never a jump straight to the target, so the scans are narrowed only as far as it takes (audit stage 5, 2026-11-02: it went 0->4->0 in 35 s cycles); lost audio still jumps at once.
                    self._set_level(self.level + 1, "tighten: " + reason, now)
                    self._over_since = now
            else:
                self._over_since = None
                if (target < self.level and now - self._last_hold >= self._relax_cooldown
                        and now - self._last_change >= RELAX_STEP_SECONDS):
                    self._set_level(self.level - 1, "relax: calm for %.0fs" % (now - self._last_hold), now)
            return self.level

    def _note_quiet(self, signals, target, now):
        """Starts or ends the quiet-PC timer from this tick's signals and logs when the scans speed up or slow back down."""
        cpu = signals.get("system_cpu_pct")
        ratio, _worst = self._busy_ratios(signals)
        light = ratio is None or ratio <= SCAN_QUIET_MAX_BUSY_RATIO
        quiet = (target == 0 and self.level == 0 and cpu is not None
                 and cpu <= SCAN_QUIET_MAX_SYSTEM_CPU_PCT and light)
        if quiet:
            if self._quiet_since is None:
                self._quiet_since = now
            if not self._quiet_announced and now - self._quiet_since >= SCAN_QUIET_AFTER_SECONDS:
                self._quiet_announced = True
                log.info("[GOVERNOR] PC quiet for %.0fs (system_cpu=%.0f%%): scans speed up x%.2f",
                         now - self._quiet_since, cpu, 1.0 / max(0.01, SCAN_QUIET_PACE_MULTIPLIER))
        else:
            if self._quiet_announced:
                log.info("[GOVERNOR] PC busy again (system_cpu=%s, level=%d): scans back to their normal pace",
                         "%.0f%%" % cpu if cpu is not None else "unknown", self.level)
            self._quiet_since = None
            self._quiet_announced = False

    def quiet_seconds(self, now=None):
        """Seconds this throttle has seen the PC quiet, 0 when it is not."""
        now = time.monotonic() if now is None else now
        since = self._quiet_since
        return 0.0 if since is None else max(0.0, now - since)

    def _set_level(self, new_level, reason, now):
        old = self.level
        self.level = max(0, min(MAX_LEVEL, new_level))
        self._last_change = now
        if self.level >= MAX_LEVEL:
            if old < MAX_LEVEL:
                self._top_since = now
        else:
            self._top_since = None
        if self.level > old and self._last_ratio is not None:
            self._step_check = (now, self._last_ratio)
        elif self.level < old:
            self._step_check = None
        if self.level > old:
            self._last_hold = now
            if self._last_step_down is not None and now - self._last_step_down <= RELAX_RELAPSE_SECONDS:
                self._relax_cooldown = min(RELAX_COOLDOWN_MAX_SECONDS, self._relax_cooldown * 2.0)
                log.info("[GOVERNOR] climbed again %.0fs after a step down; calm wait before a step down now %.0fs",
                         now - self._last_step_down, self._relax_cooldown)
            self._last_step_down = None
        elif self.level < old:
            self._last_step_down = now
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

    def add(self, busy_ms, now, budget_ms=None, kind=None):
        self._samples.append((now, float(busy_ms), float(budget_ms) if budget_ms else None, kind))
        self._trim(now)

    def _trim(self, now):
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def stats(self, now):
        self._trim(now)
        if not self._samples:
            return None, None, 0
        values = [v for _, v, _b, _k in self._samples]
        return sum(values) / len(values), max(values), len(values)

    def ratio_stats(self, now, kind=None, default_budget_ms=None):
        """(busy share at BUSY_RATIO_PERCENTILE, worst share, count) of the frames of `kind` (all frames when None), each judged against its own frame time."""
        self._trim(now)
        ratios = []
        for _t, busy, budget, sample_kind in self._samples:
            if kind is not None and sample_kind != kind:
                continue
            budget = budget or default_budget_ms
            if budget and budget > 0:
                ratios.append(busy / budget)
        if not ratios:
            return None, None, 0
        ratios.sort()
        index = min(len(ratios) - 1, max(0, int(round(BUSY_RATIO_PERCENTILE * (len(ratios) - 1)))))
        return ratios[index], ratios[-1], len(ratios)


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
        except Exception as _swallowed_error:
            _log_swallowed("percent: times = self._reader()", _swallowed_error)
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

    busy_ratio, _worst_ratio = throttle._busy_ratios(signals)
    ratio = ("%d%%" % round(busy_ratio * 100.0)) if busy_ratio is not None else "n/a"
    kind = signals.get("busy_kind") or "screen"
    own = throttle.own_load(kind)
    return ("[GOVERNOR] status ch=%s level=%d factor=%.2f screen=%s frame_time=%s busy=%s own=%s busy_avg=%s "
            "busy_worst=%s flip_avg=%s cpu=%s "
            "lost_pic=%s lost_aud=%s drive_degraded=%s event=%s bg=%s mode=%s%s" % (
                signals.get("channel", "n/a"), throttle.level, throttle.factor(profile, mode),
                kind, _fmt("frame_time_ms", "%.1fms"), ratio,
                ("%d%%" % round(own * 100.0)) if own is not None else "learning",
                _fmt("busy_avg_ms", "%.1fms"), _fmt("busy_worst_ms", "%.0fms"),
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
_gate_state = {"throttle": None, "enabled": True, "waits": 0, "last_wait_log": 0.0, "tune_until": 0.0}


@contextlib.contextmanager
def background_activity(lane):
    """Marks Python-heavy background work (guide builds, future guide rows, hub saves, the scan snapshot) as running, so the governor counts it as its own background work instead of unexplained pressure."""
    _activity.begin(lane)
    try:
        yield
    finally:
        _activity.end(lane)


def hold_background_for_tune():
    """Holds every background gate that has not admitted its work yet while a channel is being tuned, for at most TUNE_BACKGROUND_HOLD_SECONDS."""
    _gate_state["tune_until"] = time.monotonic() + TUNE_BACKGROUND_HOLD_SECONDS
    _gate_state["tune_settling_since"] = None


def release_tune_hold(now=None):
    """The new channel's show is playing: background work stays held until the picture has been steady TUNE_STEADY_SECONDS (as the throttle measures it on this PC), or TUNE_STEADY_MAX_SECONDS at most."""
    now = time.monotonic() if now is None else now
    _gate_state["tune_until"] = now + TUNE_STEADY_MAX_SECONDS
    _gate_state["tune_settling_since"] = now
    _gate_state["tune_steady_since"] = None


def note_tune_steady(steady, now=None):
    """Called by the throttle on each update after a tune: steady is true when this update saw no frames over their time and no lost pictures; TUNE_STEADY_SECONDS of steady updates end the hold."""
    started = _gate_state.get("tune_settling_since")
    if started is None:
        return
    now = time.monotonic() if now is None else now
    if now >= _gate_state["tune_until"]:
        _gate_state["tune_until"] = 0.0
        _gate_state["tune_settling_since"] = None
        log.info("[GOVERNOR] tune hold ended after %.0fs without %.0fs of steady picture; background work goes on", now - started, TUNE_STEADY_SECONDS)
        return
    if not steady:
        _gate_state["tune_steady_since"] = None
        return
    if _gate_state.get("tune_steady_since") is None:
        _gate_state["tune_steady_since"] = now
    elif now - _gate_state["tune_steady_since"] >= TUNE_STEADY_SECONDS:
        _gate_state["tune_until"] = 0.0
        _gate_state["tune_settling_since"] = None
        log.info("[GOVERNOR] picture steady for %.0fs after the tune (%.1fs after the show started); background work goes on", TUNE_STEADY_SECONDS, now - started)


def ffmpeg_scans_at_once(level=None):
    """How many ffmpeg scans the background gates let run at once: this PC's peak (the threads its mode affords) at load level 0, narrowed with the level by LEVEL_FACTOR, never below 1."""
    level = current_level() if level is None else level
    level = max(0, min(MAX_LEVEL, int(level)))
    return max(1, int(affordable_threads() * LEVEL_FACTOR[level]))


def tune_loudness_settle_seconds():
    """Seconds a tuned channel must stay on screen before its first loudness scan starts."""
    return TUNE_LOUDNESS_SETTLE_SECONDS


def loudness_parallel_windows(windows):
    """How many of one loudness scan's sample windows are measured at once: never more than the threads this PC's mode affords."""
    return max(1, min(max(1, int(windows)), affordable_threads()))


def tune_in_progress():
    """True while a channel tune holds background work."""
    return time.monotonic() < _gate_state["tune_until"]


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


def health_snapshot():
    """Short text of the governor's state for the health report: the load level, the work running in each lane, the gate waits so far and the scans with work waiting for their turn."""
    with _activity._lock:
        lanes = {lane: n for lane, n in _activity._inflight.items() if n}
    with _scan_turns_lock:
        waiting = sorted(kind for kind, entry in _scan_turns.items() if entry.get("rank") is not None)
    return "level %d, running %s, gate waits %d, scans with work waiting %s" % (
        current_level(), ", ".join("%s %d" % kv for kv in sorted(lanes.items())) or "nothing",
        _gate_state["waits"], ", ".join(waiting) or "none")


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


def first_start_scan_plan(profile=None, mode=None):
    """(True, reason) when the playing file's first-start scans may run at the same time, (False, reason) when they must run in sequence on this PC right now."""
    profile = profile or get_profile()
    mode = _limit_mode("scan", _resolve_mode(mode))
    if profile.tier == "WEAK":
        return False, "weak PC"
    if mode == "lite":
        return False, "lite mode"
    cores = core_budget(profile, mode)
    if cores < FIRST_START_CONCURRENT_MIN_CORES:
        return False, "scan cores %.1f below %.1f" % (cores, FIRST_START_CONCURRENT_MIN_CORES)
    threads = affordable_threads(profile, mode)
    if threads < FIRST_START_CONCURRENT_MIN_THREADS:
        return False, "threads %d below %d" % (threads, FIRST_START_CONCURRENT_MIN_THREADS)
    level = current_level() if enforcing() else 0
    if level > FIRST_START_CONCURRENT_MAX_LEVEL:
        return False, "load level %d above %d" % (level, FIRST_START_CONCURRENT_MAX_LEVEL)
    return True, "scan cores %.1f, threads %d, load level %d" % (cores, threads, level)


def first_start_scans_concurrent(profile=None, mode=None):
    """True when the playing file's first-start scans may run at the same time on this PC right now."""
    return first_start_scan_plan(profile, mode)[0]


def picture_box_pre_show_parallel(profile=None, mode=None):
    """(sample points decoded at once, reason) for the picture-box scan made before a file's first frame: PICTURE_BOX_PRE_SHOW_PARALLEL where first-start scans may run together, 1 where they must run in sequence."""
    together, why = first_start_scan_plan(profile, mode)
    return (max(1, int(PICTURE_BOX_PRE_SHOW_PARALLEL)) if together else 1), why


def in_priority_scope():
    """True when the calling thread is inside priority_scope (first-scan work of a file about to play)."""
    return getattr(_gate_tls, "depth", 0) > 0


def lane_held(lane):
    """True when the calling thread is inside an admitted gate of this lane."""
    return lane in (getattr(_gate_tls, "held", None) or ())


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
    """Context manager around one unit of background work; yields True when admitted, False when the caller's own timeout expired."""
    # WARNING: owner order 2026-11-05: no time cap and no load-level refusal. A job waits only while the PC's capacity is full (the lane's limit, the ffmpeg scans allowed at once for this PC and load level, never fewer than 1), during the short tune hold, or while the scan throttle has stopped the scans because a game could not catch up; the load level narrows the capacity, it never stops the work. While it waits it hands back the scan place it holds.
    held = getattr(_gate_tls, "held", None)
    if held is None:
        held = _gate_tls.held = set()
    # WARNING: a thread that already holds one ffmpeg scan lane is one scan; a step inside it (the audio-track listing of a loudness window, a file key) takes no second place in another ffmpeg lane. Owner log 2026-11-06: loudness windows admitted as priority waited for the length-probe lane, were counted as running scans, and filled the cap that lane waits on; 28 of them stood stuck and no background scan, picture boxes of airing shows included, could start.
    if lane in held or (lane in FFMPEG_SCAN_LANES and not held.isdisjoint(FFMPEG_SCAN_LANES)):
        yield True
        return
    priority = bool(priority) or _is_priority_context()
    admitted = False
    if priority or not enforcing():
        _activity.begin(lane)
        admitted = True
    else:
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        waited = False

        def _may_start():
            level = current_level()
            return (not tune_in_progress() and scan_throttle_mode() != "stopped"
                    and (lane not in FFMPEG_SCAN_LANES or ffmpeg_scans_running() < ffmpeg_scans_at_once(level))
                    and _activity.try_begin(lane, _lane_limit(lane, level)))
        admitted = _may_start()
        if not admitted:
            waited = True
            with scan_slot_idle():
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        _gate_state["waits"] += 1
                        break
                    time.sleep(GATE_POLL_SECONDS)
                    if _may_start():
                        admitted = True
                        break
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
        except Exception as _swallowed_error:
            _log_swallowed("_read_cpu_name: import winreg", _swallowed_error)
    else:
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return " ".join(line.split(":", 1)[1].split())
        except Exception as _swallowed_error:
            _log_swallowed("_read_cpu_name: with open('/proc/cpuinfo', 'r', encoding='utf-8', errors='re", _swallowed_error)
    return " ".join(str(platform.processor() or "").split())


def _read_avx2():
    if os.name == "nt":
        try:
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        except Exception as _swallowed_error:
            _log_swallowed("_read_avx2: return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent", _swallowed_error)
            return None
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("flags"):
                    return " avx2" in (" " + line.lower())
    except Exception as _swallowed_error:
        _log_swallowed("_read_avx2: with open('/proc/cpuinfo', 'r', encoding='utf-8', errors='re", _swallowed_error)
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


_GPU_VENDOR_PATTERNS = (("nvidia", r"nvidia|geforce|quadro|titan"), ("intel", r"intel"),
                        ("amd", r"\bamd\b|radeon|\bati\b|firepro"), ("microsoft", r"microsoft"))


def rate_gpu(gpu_name):
    """Rates one graphics chip from its name: vendor, family, about the year it came out, its age class and the video formats its decoder plays (None = not known)."""
    low = " ".join(str(gpu_name or "").lower().split())
    vendor = "unknown"
    for label, pattern in _GPU_VENDOR_PATTERNS:
        if re.search(pattern, low):
            vendor = label
            break
    for pattern, row_vendor, family, year, decoders in BOOT_SCAN_GPU_TABLE:
        if row_vendor == vendor and re.search(pattern, low):
            age_class = _class_from_threshold(year, BOOT_SCAN_GPU_YEAR_CLASS) if year else None
            return {"name": str(gpu_name), "vendor": vendor, "family": family, "year": year or None,
                    "class": age_class, "decoders": tuple(decoders), "software": family == "software"}
    return {"name": str(gpu_name), "vendor": vendor, "family": "unknown", "year": None,
            "class": None, "decoders": None, "software": False}


def rate_gpus(gpu_names):
    """Rates every real graphics chip; the best one (newest with a decoder, then newest) comes first."""
    rated = [r for r in (rate_gpu(n) for n in gpu_names or ()) if not r["software"]]

    def _rank(r):
        decodes = 1 if (r["decoders"] is None or r["decoders"]) else 0
        return (decodes, r["year"] or 0)
    return sorted(rated, key=_rank, reverse=True)


def gpu_rating_text(rating):
    """One short line for a rated chip, for the log."""
    if rating["decoders"] is None:
        decode = "decoder unknown"
    elif rating["decoders"]:
        decode = "decodes " + "/".join(rating["decoders"])
    else:
        decode = "no video decoder"
    return "%s (%s %s, ~%s, class %s, %s)" % (rating["name"], rating["vendor"], rating["family"],
                                             rating["year"] or "year unknown", rating["class"] or "?", decode)


def vlc_hw_decode_allowed(profile=None):
    """True when the main video engine should ask VLC to decode on the graphics card: the switch is on, this is Windows and a real graphics card was found whose rating has a video decoder (or could not be rated)."""
    if not VLC_HW_DECODE_ENABLED or os.name != "nt":
        return False
    profile = profile or get_profile()
    if str(getattr(profile, "gpu_tier", "none")) not in ("dedicated", "onboard"):
        return False
    return any(r["decoders"] is None or r["decoders"] for r in rate_gpus(profile.gpu_names))


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
    gpus = rate_gpus(profile.gpu_names)
    return {
        "cpu_name": cpu_name, "vendor": rating["vendor"], "generation": rating["generation"],
        "base_class": rating["base_class"], "class": label, "class_source": rating["class_source"],
        "note": rating["note"], "reasons": reasons, "avx2": avx2, "borderline": rating["borderline"],
        "tier": tier, "start_mode": start_mode, "caps": caps,
        "hardware_fingerprint": make_hardware_fingerprint(profile, cpu_name),
        "gpus": gpus, "gpu_class": gpus[0]["class"] if gpus else None,
        "video_hw_decode": vlc_hw_decode_allowed(profile),
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
    log.info("[GOVERNOR BOOT SCAN] gpu rating: %s; video decoding on the graphics card: %s",
             "; ".join(gpu_rating_text(r) for r in result.get("gpus") or ()) or "no real graphics card",
             "yes" if result.get("video_hw_decode") else "no")
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
    log_mode_ceilings()
    return before != (mode, source, auto, fingerprint)


def log_mode_ceilings():
    """Writes the [GOVERNOR] ceilings line for the mode now in effect: the lane ceilings, thread counts and core budget it gives."""
    profile = get_profile()
    mode_now = _state["mode"]
    log.info(
        "[GOVERNOR] ceilings mode=%s budget_cores=%.1f probe+duration=%s loudness+bake=%s music=%s whisper_slots=%s "
        "whisper_threads=%d cropdetect_threads=%s vlc_avcodec_threads=%d affordable_threads=%d caption_cadence=%s "
        "scan_threads=%d scan_gpu_decode=%s",
        mode_now, core_budget(profile, mode_now), lane_ceiling("probe_and_duration"), lane_ceiling("loudness_and_bake"),
        lane_ceiling("music_loudness"), lane_ceiling("whisper_slots"),
        whisper_threads(profile), lane_ceiling("cropdetect_threads"), vlc_decoder_threads(profile),
        affordable_threads(profile), caption_cadence(), scan_threads(profile),
        SCAN_GPU_DECODE_HWACCEL if scan_gpu_decode_allowed(profile) else "off")


def choose_mode(config, mode):
    """Records a mode the user picked in the menu and applies it live; returns the mode now in effect."""
    mode = mode if mode in allowed_modes() else ACTIVE_MODE_DEFAULT
    config[MODE_CONFIG_KEY] = mode
    config[MODE_SOURCE_CONFIG_KEY] = "user"
    set_mode(mode)
    log.info("[GOVERNOR] mode=%s source=user (menu)", mode)
    log_mode_ceilings()
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
        # WARNING: the start mode (lite, standard or performance) is picked a moment later by apply_boot_mode; nothing that depends on the mode is logged here, or the boot log names the wrong mode (L16).
        apply_cv2_threads(_state["mode"])
        log.info(
            "[GOVERNOR] profile cores=%d/%d (efficiency=%d) ram=%.1fGB (avail %.1fGB) gpu=%s [%s] "
            "tier=%s shell=%s shadow=%s frame_budget=%.2fms cpu_source=%s "
            "pygame=%s sdl=%s fingerprint=%s",
            profile.physical_cores, profile.logical_cores, profile.efficiency_cores,
            profile.ram_total_gb, profile.ram_avail_gb, profile.gpu_tier,
            "; ".join(profile.gpu_names) or "none", profile.tier,
            _shell["active"], SHADOW_MODE, profile.frame_budget_ms, profile.cpu_source,
            pygame_version or "unknown", sdl_version or "unknown", _state["fingerprint"])
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
    "ffmpeg_scans_at_once", "tune_loudness_settle_seconds", "loudness_parallel_windows", "note_tune_steady",
    "make_fingerprint",
    "build_profile", "classify_tier", "BusyWindow", "LossTracker", "SystemCpuSampler", "format_status",
    "gate", "activity", "priority_scope", "in_priority_scope", "lane_held", "first_start_scan_plan", "first_start_scans_concurrent", "background_active", "enforcing", "set_enabled",
    "register_throttle", "current_level",
    "run_boot_scan", "get_boot_scan", "parse_cpu_class", "rate_gpu", "rate_gpus", "gpu_rating_text", "vlc_hw_decode_allowed", "make_hardware_fingerprint",
    "vlc_decoder_threads", "affordable_threads", "caption_cadence", "cv2_threads", "apply_cv2_threads", "rate_cpu",
    "set_shell_mode", "is_shell_mode", "is_weak", "allowed_modes", "next_mode", "apply_boot_mode", "choose_mode", "log_mode_ceilings",
    "quiet_seconds", "pc_is_quiet", "quiet_lane_bonus", "scan_threads", "scan_thread_slot", "scan_gpu_decode_allowed",
    "music_scan_class", "music_analysis_class", "music_scan_falling_behind",
]
