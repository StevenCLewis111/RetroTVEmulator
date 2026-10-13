"""Media helpers and the RetroDatabase for the Retro TV Emulator.

WARNING: this module must import cleanly with dummy SDL drivers; the tests rely on it.
"""
import os
import re
import sys
import json
import contextlib
import copy
import ctypes
import difflib
import threading
import shutil
import concurrent.futures
import time
import types
import zlib
import logging
import math
import subprocess
import numpy as np
import pygame
try:
    import cv2 as _cv2_fx
except Exception:
    _cv2_fx = None


# ============================================================================
# SECTION: Logging & shared helpers
# ============================================================================


log = logging.getLogger(__name__)

SELECTION_BORDER_WIDTH = 3

def _media_warning(_key, message, *args):
    """Write one inexpensive line for a recoverable media failure."""
    log.warning(message, *args)


# ============================================================================
# SECTION: Shortcut resolution
# ============================================================================

SHORTCUT_EXTENSIONS = {".lnk", ".url"}


def is_shortcut_path(path):
    """True if `path` is a Windows shortcut (.lnk/.url) or a symlink."""
    try:
        if os.path.islink(path):
            return True
    except OSError:
        pass
    return os.path.splitext(str(path))[1].lower() in SHORTCUT_EXTENSIONS


def _read_lnk_target(path):
    """Pull the target path out of a Windows .lnk (Shell Link) file with no external dependency."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    if len(data) < 0x4C or data[:4] != b"\x4C\x00\x00\x00":
        return None

    flags = int.from_bytes(data[0x14:0x18], "little")
    HAS_LINK_TARGET_ID_LIST = 0x01
    HAS_LINK_INFO           = 0x02
    offset = 0x4C

    if flags & HAS_LINK_TARGET_ID_LIST:
        if offset + 2 > len(data):
            return None
        idlist_size = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2 + idlist_size

    if not (flags & HAS_LINK_INFO) or offset + 4 > len(data):
        return None

    li_size = int.from_bytes(data[offset:offset + 4], "little")
    li = data[offset:offset + li_size]
    if len(li) < 16:
        return None

    li_flags = int.from_bytes(li[8:12], "little")
    VOLUME_ID_AND_LOCAL_BASE_PATH = 0x01
    if not (li_flags & VOLUME_ID_AND_LOCAL_BASE_PATH):
        return None

    local_base_off = int.from_bytes(li[16:20], "little")
    if not local_base_off or local_base_off >= len(li):
        return None

    end = li.find(b"\x00", local_base_off)
    if end == -1:
        end = len(li)
    raw = li[local_base_off:end]
    try:
        return raw.decode("mbcs" if os.name == "nt" else "cp1252", errors="replace") or None
    except LookupError:
        return raw.decode("utf-8", errors="replace") or None


def resolve_shortcut_target(path):
    """Return the local path a shortcut points at, or None if it can't be resolved."""
    try:
        if os.path.islink(path):
            target = os.readlink(path)
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(path), target)
            return os.path.normpath(target)
    except OSError:
        pass

    ext = os.path.splitext(str(path))[1].lower()
    if ext == ".lnk":
        target = _read_lnk_target(path)
        return os.path.normpath(target) if target else None

    if ext == ".url":
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line[:4].lower() == "url=":
                        raw = line[4:].strip()
                        if raw.lower().startswith("file:///"):
                            from urllib.parse import unquote
                            local = unquote(raw[8:]).replace("/", os.sep)
                            return os.path.normpath(local)
                        return None
        except OSError:
            return None

    return None


def resolve_shortcut_chain(path, max_hops=8):
    """Follow a shortcut (or a shortcut pointing at another shortcut) down to a real file/folder path."""
    current = path
    seen = set()
    for _ in range(max_hops):
        if not is_shortcut_path(current):
            return current
        if current in seen:
            return current
        seen.add(current)
        target = resolve_shortcut_target(current)
        if not target:
            return current
        current = target
    return current


def strip_shortcut_display_suffix(name):
    """Drop the .lnk/.url extension and Windows' ' - Shortcut' suffix from a shortcut's filename for display."""
    base, ext = os.path.splitext(name)
    if ext.lower() in SHORTCUT_EXTENSIONS:
        name = base
    name = re.sub(r'(?i)\s*-\s*shortcut(?:\s*\(\d+\))?\s*$', '', name).strip()
    return name or base


def resolve_dir_entry(entry_path, entry_name):
    """Resolve one os.scandir() entry, following a shortcut or symlink to its real target."""
    target = entry_path
    if is_shortcut_path(entry_path):
        resolved = resolve_shortcut_chain(entry_path)
        if resolved == entry_path or not os.path.exists(resolved):
            return None, None, None
        target = resolved

    display_name = strip_shortcut_display_suffix(entry_name)
    try:
        if os.path.isdir(target):
            return "dir", os.path.normpath(target), display_name
        if os.path.isfile(target):
            return "file", os.path.normpath(target), display_name
    except OSError:
        pass
    return None, None, None


def iter_files_resolving_shortcuts(root, ignored_dir_names=None, valid_exts=None,
                                    is_drive_responsive=None):
    """Recursively yield real file paths under `root`, following shortcut and symlink folders into their targets."""
    ignored_dir_names = ignored_dir_names or set()
    seen_dirs = set()

    def _walk(dir_path):
        if is_drive_responsive is not None and not is_drive_responsive(dir_path):
            return
        try:
            real = os.path.realpath(dir_path)
        except OSError:
            real = dir_path
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        try:
            entries = list(os.scandir(dir_path))
        except (PermissionError, OSError):
            return

        subdirs = []
        files = []
        for entry in entries:
            try:
                kind, real_path, _disp = resolve_dir_entry(entry.path, entry.name)
            except OSError:
                continue
            if kind == "dir":
                if entry.name.lower() not in ignored_dir_names:
                    subdirs.append(real_path)
            elif kind == "file":
                files.append((entry.name, real_path))

        subdirs.sort()
        files.sort(key=lambda x: x[0].lower())
        for fname, fpath in files:
            if valid_exts is None or os.path.splitext(fpath.lower())[1] in valid_exts:
                yield fpath
        for sd in subdirs:
            yield from _walk(sd)

    yield from _walk(root)


# ============================================================================
# SECTION: Display-title cleanup
# ============================================================================

VIDEO_DISPLAY_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts",
}
AUDIO_DISPLAY_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma"}


def clean_video_display_title(raw_title):
    """Clean a video's display name: strips season/episode markers, resolution, codec and rip tags, folder path and extension."""

    def _strip_markers(text):
        t = text.replace("_", " ")
        t = re.sub(r'[\[\(][^\]\)]*[\]\)]', ' ', t)

        _CUT_PATTERNS = [
            r's\d{1,2}[\.\s_-]*[ex]\d{1,3}\b',
            r'\bseason[\.\s_-]*\d+\b',
            r'\b\d{1,2}x\d{2,3}\b',
            r'\bepisode[\.\s_-]*\d+\b',
            r'\bep[\.\s_-]*\d+\b',
            r'\bpart[\.\s_-]*\d+\b',
            r'\bpt\.?[\.\s_-]*\d+\b',
            r'\b(?:19|20)\d{2}\b',
            r'\b(?:1080p|720p|480p|2160p|4k)\b',
            r'\b(?:dvdrip|webrip|web-?dl|blu-?ray|hdtv|xvid|divx|x264|x265|h26[45]|hevc|aac|ac3|dts|remux|repack|proper|extended|unrated)\b',
        ]
        # WARNING: a bare "E15" is stripped only when it leads the name; stripping it mid-string eats real titles.
        _LEADING_EP_PATTERNS = _CUT_PATTERNS[:5] + [r'e\d{1,3}\b']
        for _ in range(4):
            _lead = None
            for pat in _LEADING_EP_PATTERNS:
                _lead = re.match(r'^[\s\.\-\u2013\u2014:]*(?:' + pat + r')', t, re.IGNORECASE)
                if _lead:
                    break
            if not _lead:
                break
            t = t[_lead.end():]
            t = re.sub(r'^[\s\.\-\u2013\u2014:]+', '', t)

        cut_at = len(t)
        for pat in _CUT_PATTERNS:
            m = re.search(pat, t, re.IGNORECASE)
            if m and m.start() < cut_at:
                cut_at = m.start()

        t = t[:cut_at]
        t = t.replace(".", " ").replace("-", " ")
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    s = raw_title if isinstance(raw_title, str) else str(raw_title)
    _original_raw = s
    if "/" in s or "\\" in s:
        s = os.path.basename(s)
    if "." in s:
        s = os.path.splitext(s)[0]

    cleaned = _strip_markers(s)
    if cleaned:
        return cleaned

    if "/" in _original_raw or "\\" in _original_raw:
        _folder = os.path.dirname(_original_raw)
        for _ in range(3):
            if not _folder:
                break
            cleaned_folder = _strip_markers(os.path.basename(_folder))
            if cleaned_folder:
                return cleaned_folder
            _next = os.path.dirname(_folder)
            if _next == _folder:
                break
            _folder = _next

    log.error("Unable to derive a video title from %r", _original_raw)
    return cleaned


_MUSIC_TITLE_JUNK_PATTERNS = [
    r'\bthe\s+complete\s+greatest\s+hits\b',
    r'\bcomplete\s+greatest\s+hits\b',
    r'\bgreatest\s+hits\b',
    r'\bthe\s+very\s+best\s+of\b',
    r'\bvery\s+best\s+of\b',
    r'\bthe\s+best\s+of\b',
    r'\bthe\s+complete\s+collection\b',
    r'\bcomplete\s+collection\b',
    r'\bdefinitive\s+collection\b',
    r'\bultimate\s+collection\b',
    r'\bessential\s+collection\b',
    r'\banthology\b',
    r'\bbox\s*set\b',
    r'\bd\d{1,2}t\d{1,3}\b',
    r'\bdisc\s*\d+\b',
    r'\bcd\s*\d+\b',
    r'[\[\(]\s*cd\s*[\]\)]',
    r'_?www\.[a-z0-9\-]+\.[a-z]{2,}',
    r'\b[a-z0-9\-]+\.(?:com|net|org|info)\b',
    r'\b(?:19|20)\d{2}\s+digitally\s+remastered\b',
    r'\b(?:19|20)\d{2}\s+remaster(?:ed)?\b',
    r'\bdigitally\s+remastered\b',
    r'\bremaster(?:ed)?\b',
    r'\bbonus\s+track\b',
    r'\bappended\s+track\b',
    r'\bdeluxe\s+edition\b',
    r'\bexpanded\s+edition\b',
    r'\bspecial\s+edition\b',
    r'[\[\(]\s*live\s+acoustic\s*[\]\)]',
    r'[\[\(]\s*live\s*[\]\)]',
    r'\btrack\s*\d+\b',
    r'\btk\s*\d+\b',
    r'\bside\s*[ab]\b',
    r'\bvinyl\s+rip\b',
    r'\bcd\s+rip\b',
    r'[\[\(]\s*rip\s*[\]\)]',
    r'\bhigh\s+quality\b',
    r'[\[\(]\s*hq\s*[\]\)]',
    r'[\[\(]\s*hd\s*[\]\)]',
    r'[\[\(]\s*free\s+download\s*[\]\)]',
    r'\buploaded\s+by\s+\S+',
    r'[\[\(]\s*exclusive\s*[\]\)]',
    r'\bmp3\b',
    r'\bwav\b',
    r'\bflac\b',
    r'\baac\b',
    r'\b\d{2,3}\s*kbps\b',
    r'\bvbr\b',
    r'\blossless\b',
]


def clean_music_display_title(raw_title):
    """Clean a music track's display name: strips album branding, disc-track codes and site watermarks."""
    t = raw_title if isinstance(raw_title, str) else str(raw_title)
    if "/" in t or "\\" in t:
        t = os.path.basename(t)
    if "." in t:
        t = os.path.splitext(t)[0]
    for pat in _MUSIC_TITLE_JUNK_PATTERNS:
        t = re.sub(pat, ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'[\(\[]\s*-?\s*[\)\]]', ' ', t)
    t = re.sub(r'\s*-\s*(?=(?:-\s*)*(?:$|-))', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    t = t.strip(' -:.,')
    return t


def clean_display_title_for_ext(name, ext):
    """Route display-name cleanup by extension: video cleaner, music cleaner, or plain shortcut/extension stripping for anything else."""
    ext = (ext or "").lower()
    if ext in VIDEO_DISPLAY_EXTS:
        return clean_video_display_title(name)
    if ext in AUDIO_DISPLAY_EXTS:
        return clean_music_display_title(name)
    base = os.path.splitext(name)[0] if ext else name
    return base


# ============================================================================
# SECTION: Shared video effects
# ============================================================================

_video_effects_cache = {
    "params":      None,
    "channel_lut": None,
    "sat_factor":  1.0,
    "cv2_key":     None,
    "cv2_lut":     None,
    "cv2_mat":     None,
}
_video_effects_cv2_state = {"disabled": False}
_effect_scratch = {
    "surf":      None,
    "float_buf": None,
}


def _get_video_effects(db):
    brightness = db.config.get("brightness", 50) if db else 50
    contrast   = db.config.get("contrast",   50) if db else 50
    color_sat  = db.config.get("color",      50) if db else 50
    sharpness  = db.config.get("sharpness",  50) if db else 50
    tint       = db.config.get("tint",       50) if db else 50
    fx_active  = (brightness != 50 or contrast != 50 or color_sat != 50
                  or sharpness != 50 or tint != 50)
    return brightness, contrast, color_sat, sharpness, tint, fx_active


def _fx_channel_offsets(surf):
    """Return (r_off, g_off, b_off), the channel byte offsets in a 24-bit pygame surface, or None for layouts the cv2 path can't handle."""
    try:
        if sys.byteorder != "little" or surf.get_bytesize() != 3:
            return None
        shifts = surf.get_shifts()[:3]
        if any(sh % 8 for sh in shifts):
            return None
        offs = tuple(sh // 8 for sh in shifts)
        return offs if sorted(offs) == [0, 1, 2] else None
    except Exception:
        return None


def _apply_video_effects_cv2(cached_surf, offs, current_params, channel_lut, color):
    """In-place Brightness/Contrast/Tint LUT + Color blend + Sharpness on the 24-bit scratch surface using cv2."""
    brightness, contrast, color_p, tint, sharpness = current_params
    key = (current_params, offs)
    cache = _video_effects_cache
    if cache["cv2_key"] != key:
        lut3 = np.empty((256, 1, 3), dtype=np.uint8)
        for ch in range(3):
            lut3[:, 0, offs[ch]] = channel_lut[ch]
        sat = color_p / 50.0
        luma = (0.299, 0.587, 0.114)
        luma_by_byte = [0.0, 0.0, 0.0]
        for ch in range(3):
            luma_by_byte[offs[ch]] = luma[ch]
        mat = np.zeros((3, 4), dtype=np.float32)
        for i in range(3):
            for j in range(3):
                mat[i, j] = (sat if i == j else 0.0) + (1.0 - sat) * luma_by_byte[j]
            mat[i, 3] = -0.5
        cache["cv2_lut"], cache["cv2_mat"], cache["cv2_key"] = lut3, mat, key

    w, h, pitch = cached_surf.get_width(), cached_surf.get_height(), cached_surf.get_pitch()
    buf = cached_surf.get_buffer()
    view = None
    try:
        view = np.frombuffer(buf, dtype=np.uint8, count=pitch * h).reshape(h, pitch)[:, :w * 3].reshape(h, w, 3)
        if not (brightness == 50 and contrast == 50 and tint == 50):
            _cv2_fx.LUT(view, cache["cv2_lut"], dst=view)
        if color_p != 50:
            _cv2_fx.transform(view, cache["cv2_mat"], dst=view)
        if sharpness != 50:
            _sharp_strength = (sharpness - 50) / 50.0
            _blurred = _cv2_fx.GaussianBlur(view, (0, 0), 1.0)
            _cv2_fx.addWeighted(view, 1.0 + _sharp_strength, _blurred, -_sharp_strength, 0, dst=view)
    finally:
        view = None
        del buf


def apply_video_effects(surface, brightness=50, contrast=50, color=50, sharpness=50, tint=50):

    if brightness == 50 and contrast == 50 and color == 50 and tint == 50 and sharpness == 50:
        return surface

    try:
        current_params = (brightness, contrast, color, tint, sharpness)

        if _video_effects_cache["params"] != current_params:
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

        cached_surf = _effect_scratch["surf"]
        if cached_surf is None or cached_surf.get_width() != w or cached_surf.get_height() != h:
            _effect_scratch["surf"]      = pygame.Surface((w, h), 0, 24)
            _effect_scratch["float_buf"] = None
            cached_surf = _effect_scratch["surf"]

        cached_surf.blit(surface, (0, 0))

        _cv2_fx_failed = False
        if _cv2_fx is not None and not _video_effects_cv2_state["disabled"]:
            _offs = _fx_channel_offsets(cached_surf)
            if _offs is not None:
                try:
                    _apply_video_effects_cv2(cached_surf, _offs, current_params, channel_lut, color)
                    return cached_surf
                except Exception as _cv2_fx_error:
                    _video_effects_cv2_state["disabled"] = True
                    _cv2_fx_failed = True
                    _media_warning(
                        "video_effects_cv2_error",
                        "[VIDEO EFFECTS] cv2 fast path failed; using the NumPy path from now on: %s",
                        # WARNING: log str(e), not the exception; a buffering log handler would keep the traceback and the locked pixel view alive.
                        str(_cv2_fx_error),
                    )
        if _cv2_fx_failed:
            import gc
            gc.collect()
            cached_surf.blit(surface, (0, 0))

        arr = pygame.surfarray.pixels3d(cached_surf)

        arr[:, :, 0] = channel_lut[0][arr[:, :, 0]]
        arr[:, :, 1] = channel_lut[1][arr[:, :, 1]]
        arr[:, :, 2] = channel_lut[2][arr[:, :, 2]]

        if color != 50:
            shape = arr.shape

            fb = _effect_scratch["float_buf"]
            if fb is None or fb.shape != shape:
                fb = np.empty(shape, dtype=np.float32)
                _effect_scratch["float_buf"] = fb

            np.copyto(fb, arr, casting="unsafe")

            gray = (0.299 * fb[:, :, 0] +
                    0.587 * fb[:, :, 1] +
                    0.114 * fb[:, :, 2])[:, :, None]
            fb -= gray
            fb *= sat_factor
            fb += gray
            np.clip(fb, 0, 255, out=fb)

            np.copyto(arr, fb, casting="unsafe")

        if sharpness != 50:
            strength = (sharpness - 50) / 50.0

            fb = _effect_scratch["float_buf"]
            if fb is None or fb.shape != arr.shape:
                fb = np.empty(arr.shape, dtype=np.float32)
                _effect_scratch["float_buf"] = fb
            np.copyto(fb, arr, casting="unsafe")

            padded = np.pad(fb, ((1, 1), (1, 1), (0, 0)), mode="edge")
            blurred = (
                padded[0:-2, 0:-2] + padded[0:-2, 1:-1] + padded[0:-2, 2:] +
                padded[1:-1, 0:-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:] +
                padded[2:,   0:-2] + padded[2:,   1:-1] + padded[2:,   2:]
            ) * (1.0 / 9.0)

            fb += strength * (fb - blurred)
            np.clip(fb, 0, 255, out=fb)
            np.copyto(arr, fb, casting="unsafe")

        del arr

        return cached_surf

    except Exception as effect_error:
        _media_warning(
            "video_effects_error",
            "[VIDEO EFFECTS] Processing failed; using the unmodified surface: %s",
            effect_error,
        )
        return surface


# ============================================================================
# SECTION: Path-exists cache & disk I/O throttling
# ============================================================================


def _now_ms():
    return time.time() * 1000.0


_path_exists_cache = {}
_path_exists_cache_lock = threading.Lock()

_PATH_EXISTS_CACHE_SWEEP_INTERVAL_MS = 60000
_PATH_EXISTS_CACHE_MAX_ENTRIES = 20000
_last_path_exists_cache_sweep_ms = 0.0

_PATH_NOT_FOUND_TTL_MS = 5000
_PATH_FOUND_TTL_MS = 30000
_PATH_UNKNOWN_TTL_MS = 1500

# WARNING: treat PATH_UNKNOWN like PRESENT for eligibility; never drop a file because its drive had a rough moment.
PATH_PRESENT = "present"
PATH_MISSING = "missing"
PATH_UNKNOWN = "unknown"

FAST_DISK_IO_MAX_CONCURRENT = 3
_FAST_DISK_IO_SEMAPHORE = threading.BoundedSemaphore(FAST_DISK_IO_MAX_CONCURRENT)

LOUDNESS_PROBE_MAX_CONCURRENT = 2
_LOUDNESS_PROBE_SEMAPHORE = threading.BoundedSemaphore(LOUDNESS_PROBE_MAX_CONCURRENT)

_LOUDNESS_PRIORITY_SEMAPHORE = threading.BoundedSemaphore(1)
_LOUDNESS_PARALLEL_SEMAPHORE = threading.BoundedSemaphore(8)

# VALUES CHANGE HERE
MUSIC_LOUDNESS_PROBE_MAX_CONCURRENT = 1
_MUSIC_LOUDNESS_PROBE_SEMAPHORE = threading.BoundedSemaphore(MUSIC_LOUDNESS_PROBE_MAX_CONCURRENT)

_LIVE_PLAYBACK_PRIORITY = threading.Event()
_LIVE_PLAYBACK_PRIORITY_MAX_WAIT_S = 2.5


def set_live_playback_priority(active):
    """Mark live playback as opening (True) or finished opening (False) so background work yields."""
    # WARNING: pair every True with a False in a finally.
    if active:
        _LIVE_PLAYBACK_PRIORITY.set()
    else:
        _LIVE_PLAYBACK_PRIORITY.clear()


def _yield_to_live_playback(label=""):
    """Wait, bounded, for a live channel change to finish opening its file before a background disk read starts."""
    if not _LIVE_PLAYBACK_PRIORITY.is_set():
        return
    _wait_start = _now_ms()
    _deadline_ms = _wait_start + (_LIVE_PLAYBACK_PRIORITY_MAX_WAIT_S * 1000)
    while _LIVE_PLAYBACK_PRIORITY.is_set():
        if _now_ms() >= _deadline_ms:
            break
        time.sleep(0.02)

@contextlib.contextmanager
def _gov_gate(lane, priority=False, timeout=None, label=""):
    """Asks the performance governor to admit one unit of background work; True when admitted, False if the caller's timeout expired; admits everything if the governor is missing or errors."""
    gate = None
    if _perf_governor is not None:
        try:
            gate = _perf_governor.gate(lane, priority=priority, timeout=timeout, label=label)
        except Exception as _gate_err:
            log.warning("[GOVERNOR] gate unavailable, admitting: %s", _gate_err)
    if gate is None:
        yield True
        return
    with gate as _admitted:
        yield _admitted


def _fast_disk_lane(label):
    """Map a job kind to its governor lane: picture_box to cropdetect, asr_extract to none, everything else to probe_and_duration."""
    if label.startswith("asr_extract"):
        return None
    if label.startswith("picture_box"):
        return "cropdetect_threads"
    return "probe_and_duration"


@contextlib.contextmanager
def _fast_disk_io_slot(label=""):
    """Like `with _FAST_DISK_IO_SEMAPHORE:` but first yields to live playback, then asks the governor to admit the job."""
    _yield_to_live_playback(label)
    _lane = _fast_disk_lane(label)
    if _lane is None:
        with _FAST_DISK_IO_SEMAPHORE:
            yield
        return
    with _gov_gate(_lane, label=label):
        with _FAST_DISK_IO_SEMAPHORE:
            yield


def _sweep_path_exists_cache(now_ms):
    """Drop entries whose TTL has expired."""
    # WARNING: call with _path_exists_cache_lock held.
    expired = []
    for path, (status, cached_at) in _path_exists_cache.items():
        if status == PATH_MISSING:
            ttl = _PATH_NOT_FOUND_TTL_MS
        elif status == PATH_PRESENT:
            ttl = _PATH_FOUND_TTL_MS
        else:
            ttl = _PATH_UNKNOWN_TTL_MS
        if now_ms - cached_at >= ttl:
            expired.append(path)
    for path in expired:
        _path_exists_cache.pop(path, None)


def _maybe_sweep_path_exists_cache(now_ms):
    """Best-effort periodic + size-triggered sweep."""
    global _last_path_exists_cache_sweep_ms
    due_by_time = (now_ms - _last_path_exists_cache_sweep_ms) >= _PATH_EXISTS_CACHE_SWEEP_INTERVAL_MS
    due_by_size = len(_path_exists_cache) > _PATH_EXISTS_CACHE_MAX_ENTRIES
    if not (due_by_time or due_by_size):
        return
    _sweep_path_exists_cache(now_ms)
    _last_path_exists_cache_sweep_ms = now_ms


# WARNING: must match _DRIVE_CHECK_TIMEOUT_MS below.
_PATH_EXISTS_PROBE_TIMEOUT_MS = 2500


def _bounded_os_path_exists(p, timeout_ms=_PATH_EXISTS_PROBE_TIMEOUT_MS):
    """os.path.exists(p) run on a background thread and bounded to timeout_ms, so a hung drive can't block the caller."""
    _box = []
    _done = threading.Event()

    def _worker():
        try:
            _box.append(os.path.exists(p))
        except Exception:
            _box.append(False)
        finally:
            _done.set()

    threading.Thread(target=_worker, daemon=True, name="PathExistsProbe").start()
    if _done.wait(timeout_ms / 1000.0) and _box:
        return PATH_PRESENT if _box[0] else PATH_MISSING

    root = _drive_root_of(p)
    if root is not None:
        with _drive_health_lock:
            _watched_drive_roots.add(root)
            _drive_health[root] = {"responsive": False, "checked_at": _now_ms(), "checking_since": None}
        log.warning(
            "[DRIVE HEALTH] %s marked unresponsive -- a direct os.path.exists() "
            "probe on %s took over %dms and never returned. Path-existence "
            "checks on this drive will fail OPEN (assume present) until it "
            "recovers; genuinely missing files are still caught by the "
            "normal playback-failure path.",
            root, p, timeout_ms,
        )
    return PATH_UNKNOWN


def _path_status(p):
    """Tri-state existence check: PATH_PRESENT / PATH_MISSING / PATH_UNKNOWN."""
    if not p:
        return PATH_MISSING
    now_ms = _now_ms()
    with _path_exists_cache_lock:
        _maybe_sweep_path_exists_cache(now_ms)
        cached = _path_exists_cache.get(p)
    if cached is not None:
        _cached_status, _cached_at = cached
        if _cached_status == PATH_MISSING:
            _ttl = _PATH_NOT_FOUND_TTL_MS
        elif _cached_status == PATH_PRESENT:
            _ttl = _PATH_FOUND_TTL_MS
        else:
            _ttl = _PATH_UNKNOWN_TTL_MS
        if now_ms - _cached_at < _ttl:
            return _cached_status

    if not _is_drive_responsive(p):
        status = PATH_UNKNOWN
    else:
        status = _bounded_os_path_exists(p)

    with _path_exists_cache_lock:
        _path_exists_cache[p] = (status, now_ms)
    return status


def _cached_path_exists(p):
    """Boolean view of _path_status(): PRESENT and UNKNOWN read True, only a confirmed MISSING reads False."""
    return _path_status(p) != PATH_MISSING


# ============================================================================
# SECTION: Drive health monitoring
# ============================================================================


_drive_health = {}
_drive_health_lock = threading.Lock()
_watched_drive_roots = set()

_media_shutting_down = threading.Event()


def request_media_shutdown():
    """Stop media.py's background loops (the drive health monitor); call once from the quit handler."""
    _media_shutting_down.set()


_DRIVE_CHECK_TIMEOUT_MS         = 2500
# VALUES CHANGE HERE
_DRIVE_RECHECK_INTERVAL_MS      = 4000
_DRIVE_UNRESPONSIVE_RECHECK_MS  = 3000


def _drive_root_of(path):
    if not path:
        return None
    if sys.platform == "win32":
        drive, _ = os.path.splitdrive(str(path))
        return (drive + "\\") if drive else None
    return "/"


def _purge_stale_negative_cache_for_root(root):
    """Drop cached "not found" verdicts for every path on `root`."""
    with _path_exists_cache_lock:
        stale = [p for p, v in _path_exists_cache.items()
                 if isinstance(v, tuple) and v[0] in (PATH_MISSING, PATH_UNKNOWN)
                 and _drive_root_of(p) == root]
        for p in stale:
            _path_exists_cache.pop(p, None)
    if stale:
        log.info(
            "[DRIVE HEALTH] %s is responsive again -- cleared %d stale "
            "'not found'/'unknown' cache entries so playback re-verifies them "
            "immediately instead of waiting out their TTL", root, len(stale),
        )


def _drive_probe_worker(root):
    try:
        ok = os.path.exists(root)
    except Exception:
        ok = False
    now_ms = _now_ms()
    with _drive_health_lock:
        _was_responsive = _drive_health.get(root, {}).get("responsive", True)
        _drive_health[root] = {"responsive": ok, "checked_at": now_ms, "checking_since": None}
    if ok and not _was_responsive:
        log.info("[DRIVE HEALTH] %s recovered", root)
        _purge_stale_negative_cache_for_root(root)


def _drive_health_monitor_loop():
    while True:
        if _media_shutting_down.is_set():
            return
        try:
            now_ms = _now_ms()
            with _drive_health_lock:
                roots = list(_watched_drive_roots)
            for root in roots:
                with _drive_health_lock:
                    entry = _drive_health.get(root)
                if entry is None:
                    with _drive_health_lock:
                        _drive_health[root] = {"responsive": True, "checked_at": now_ms, "checking_since": now_ms}
                    threading.Thread(target=_drive_probe_worker, args=(root,), daemon=True).start()
                    continue
                checking_since = entry.get("checking_since")
                if checking_since is not None:
                    if now_ms - checking_since > _DRIVE_CHECK_TIMEOUT_MS and entry.get("responsive", True):
                        with _drive_health_lock:
                            if _drive_health.get(root, {}).get("checking_since") == checking_since:
                                _drive_health[root]["responsive"] = False
                                log.warning(
                                    "[DRIVE HEALTH] %s marked unresponsive -- "
                                    "its own os.path.exists() probe took over "
                                    "%dms. Path-existence checks on this drive "
                                    "will fail OPEN (assume present) until it "
                                    "recovers; genuinely missing files are still "
                                    "caught by the normal playback-failure path.",
                                    root, _DRIVE_CHECK_TIMEOUT_MS,
                                )
                    continue
                interval = _DRIVE_UNRESPONSIVE_RECHECK_MS if not entry.get("responsive", True) else _DRIVE_RECHECK_INTERVAL_MS
                if now_ms - entry.get("checked_at", 0) >= interval:
                    with _drive_health_lock:
                        _drive_health[root]["checking_since"] = now_ms
                    threading.Thread(target=_drive_probe_worker, args=(root,), daemon=True).start()
        except Exception as e:
            _media_warning(
                "drive_health_monitor",
                "_drive_health_monitor_loop: iteration failed: %s",
                e,
            )
        if _media_shutting_down.wait(0.5):
            return


def _is_drive_responsive(path):
    root = _drive_root_of(path)
    if root is None:
        return True
    with _drive_health_lock:
        _watched_drive_roots.add(root)
        entry = _drive_health.get(root)
    return True if entry is None else entry.get("responsive", True)


def _unresponsive_drive_roots():
    with _drive_health_lock:
        return sorted(r for r, e in _drive_health.items() if not e.get("responsive", True))


import collections


# ============================================================================
# SECTION: Drive latency tracking
# ============================================================================


_drive_op_latency = {}
_drive_op_latency_lock = threading.Lock()
# VALUES CHANGE HERE
_DRIVE_LATENCY_SAMPLE_WINDOW = 6
_DRIVE_LATENCY_FIXED_OVERHEAD_MS = 2000
# VALUES CHANGE HERE
_DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC = 100.0
_DRIVE_LATENCY_MIN_SAMPLES = 3
_DRIVE_LATENCY_SAMPLE_MAX_AGE_MS = 120_000


def _record_drive_op_latency(path, elapsed_ms, audio_sec=None):
    """Record a real disk operation's wall time in the per-drive rolling latency tracker."""
    root = _drive_root_of(path)
    if root is None:
        return
    try:
        audio_sec = float(audio_sec) if audio_sec is not None else 0.0
    except (TypeError, ValueError):
        audio_sec = 0.0
    if audio_sec <= 0.0:
        return
    per_audio_sec_ms = max(0.0, elapsed_ms - _DRIVE_LATENCY_FIXED_OVERHEAD_MS) / audio_sec
    now_ms = _now_ms()
    with _drive_op_latency_lock:
        dq = _drive_op_latency.setdefault(root, collections.deque(maxlen=_DRIVE_LATENCY_SAMPLE_WINDOW))
        dq.append((now_ms, per_audio_sec_ms))


def _is_drive_degraded(path):
    """True if this drive is not responding, or its recent real reads are slow enough that background work should back off."""
    root = _drive_root_of(path)
    if root is None:
        return False
    with _drive_health_lock:
        _health = _drive_health.get(root)
    if _health is not None and not _health.get("responsive", True):
        return True
    with _drive_op_latency_lock:
        dq = _drive_op_latency.get(root)
        if not dq:
            return False
        # WARNING: samples need an age limit; without one a degraded flag locks itself on because skipped drives add no new samples.
        _cutoff = _now_ms() - _DRIVE_LATENCY_SAMPLE_MAX_AGE_MS
        samples = [ms for t, ms in dq if t >= _cutoff]
        if len(samples) < _DRIVE_LATENCY_MIN_SAMPLES:
            return False
    return (sum(samples) / len(samples)) >= _DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC


def _drive_latency_summary(path):
    """Human-readable snippet for a log line, or None if there's not enough data yet to say anything."""
    root = _drive_root_of(path)
    if root is None:
        return None
    with _drive_health_lock:
        _health = _drive_health.get(root)
    if _health is not None and not _health.get("responsive", True):
        return f"{root} is not responding"
    with _drive_op_latency_lock:
        dq = _drive_op_latency.get(root)
        if not dq:
            return None
        _cutoff = _now_ms() - _DRIVE_LATENCY_SAMPLE_MAX_AGE_MS
        samples = [ms for t, ms in dq if t >= _cutoff]
        if not samples:
            return f"{root} no recent reads"
    return (f"{root} avg {sum(samples) / len(samples):.1f}ms of ffmpeg time per audio second "
            f"over last {len(samples)} recent reads (degraded at >= "
            f"{_DRIVE_LATENCY_DEGRADED_MS_PER_AUDIO_SEC:.0f})")


# ============================================================================
# SECTION: File-level recovery probe
# ============================================================================


_file_probe_state = {}
_file_probe_lock = threading.Lock()
# VALUES CHANGE HERE
_FILE_PROBE_RECHECK_MS   = 5000
_FILE_PROBE_TIMEOUT_MS   = 3000
_FILE_PROBE_READ_BYTES   = 65536


def _file_probe_worker(root, path):
    ok = False
    try:
        with open(path, "rb") as f:
            f.read(_FILE_PROBE_READ_BYTES)
        ok = True
    except Exception:
        ok = False
    now_ms = _now_ms()
    with _file_probe_lock:
        entry = _file_probe_state.get(root)
        if entry is not None and entry.get("path") == path and entry.get("checking_since") is not None:
            _was_ok = entry.get("last_result")
            _file_probe_state[root] = {
                "last_result": ok, "checked_at": now_ms, "checking_since": None, "path": path,
            }
            if ok and _was_ok is not True:
                log.info(
                    "[DRIVE HEALTH] file-level probe confirmed %s is actually "
                    "readable again -- real read of %s succeeded", root, path,
                )
            elif not ok:
                log.warning(
                    "[DRIVE HEALTH] file-level probe: real read of %s still "
                    "fails -- %s not confirmed recovered yet", path, root,
                )


def probe_file_level_recovery(root, candidate_path):
    """Return the latest result of a real open+read of `candidate_path`: True (succeeded recently), False (failed or timed out), or None (no recent result)."""
    if root is None or not candidate_path:
        return None
    now_ms = _now_ms()
    with _file_probe_lock:
        entry = _file_probe_state.get(root)
        if entry is not None and entry.get("path") != candidate_path:
            entry = None
        checking_since = entry.get("checking_since") if entry else None
        if checking_since is not None:
            if now_ms - checking_since > _FILE_PROBE_TIMEOUT_MS:
                _file_probe_state[root] = {
                    "last_result": False, "checked_at": now_ms,
                    "checking_since": None, "path": candidate_path,
                }
                return False
            return entry.get("last_result")
        due = entry is None or (now_ms - entry.get("checked_at", 0) >= _FILE_PROBE_RECHECK_MS)
        if due:
            _prior_result = entry.get("last_result") if entry else None
            _file_probe_state[root] = {
                "last_result": _prior_result,
                "checked_at": entry.get("checked_at", 0) if entry else 0,
                "checking_since": now_ms,
                "path": candidate_path,
            }
            threading.Thread(target=_file_probe_worker, args=(root, candidate_path), daemon=True).start()
            return _prior_result
        return entry.get("last_result")


# ============================================================================
# SECTION: Optical disc sources
# ============================================================================


DRIVE_CDROM = 5

_disc_info_cache = {}
_disc_info_lock  = threading.Lock()
_DISC_INFO_RECHECK_MS = 4000


def _win_drive_type_raw(drive_root):
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive_root))
    except Exception:
        return None


def is_optical_drive_root(path):
    if sys.platform != "win32" or not path:
        return False
    p = str(path)
    if not (len(p) == 3 and p[1] == ":" and p[2] in ("\\", "/")):
        return False
    return _win_drive_type_raw(p[0] + ":\\") == DRIVE_CDROM


def _get_volume_label_raw(drive_root):
    if sys.platform != "win32":
        return None
    try:
        vol_buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_root), vol_buf, ctypes.sizeof(vol_buf),
            None, None, None, None, 0,
        )
        if not ok:
            return None
        label = vol_buf.value.strip()
        return label or None
    except Exception:
        return None


def _has_video_ts_raw(drive_root):
    try:
        return os.path.isdir(os.path.join(drive_root, "VIDEO_TS"))
    except Exception:
        return False


def _disc_info_probe_worker(drive_root):
    label        = _get_volume_label_raw(drive_root)
    has_video_ts = _has_video_ts_raw(drive_root)
    with _disc_info_lock:
        _disc_info_cache[drive_root] = {
            "label": label, "has_video_ts": has_video_ts,
            "checked_at": _now_ms(), "checking": False,
        }


def get_disc_info(drive_root):
    now = _now_ms()
    need_probe = False
    with _disc_info_lock:
        entry = _disc_info_cache.get(drive_root)
        if entry is None:
            entry = {"label": None, "has_video_ts": False, "checked_at": 0, "checking": True}
            _disc_info_cache[drive_root] = entry
            need_probe = True
        elif not entry.get("checking") and now - entry.get("checked_at", 0) >= _DISC_INFO_RECHECK_MS:
            entry["checking"] = True
            need_probe = True
        result = dict(entry)
    if need_probe:
        threading.Thread(target=_disc_info_probe_worker, args=(drive_root,), daemon=True).start()
    return result


def is_dvd_disc_source(path):
    if not path:
        return False
    if os.path.splitext(str(path))[1].lower() == ".iso":
        return True
    if is_optical_drive_root(path):
        drive_root = str(path)[0] + ":\\"
        return bool(get_disc_info(drive_root).get("has_video_ts"))
    parent_name = os.path.basename(os.path.dirname(str(path).rstrip("\\/"))).upper()
    if parent_name == "VIDEO_TS":
        return True
    return False


def dvd_disc_mrl(path):
    if os.path.splitext(str(path))[1].lower() == ".iso":
        return "dvdnav:///" + os.path.abspath(path).replace("\\", "/")
    parent_dir = os.path.dirname(str(path).rstrip("\\/"))
    if os.path.basename(parent_dir).upper() == "VIDEO_TS":
        disc_root = os.path.dirname(parent_dir) or parent_dir
        drive_letter = disc_root.rstrip("\\/")
        if len(drive_letter) == 2 and drive_letter[1] == ":":
            return "dvd:///" + drive_letter + "\\"
        return "dvdnav:///" + os.path.abspath(disc_root).replace("\\", "/")
    drive_letter = str(path).rstrip("\\/")
    if len(drive_letter) == 2 and drive_letter[1] == ":":
        return "dvd:///" + drive_letter + "\\"
    return "dvdnav://" + drive_letter

# ============================================================================
# SECTION: In-flight guard & probe slots
# ============================================================================
class _InFlightGuard:
    """Prevents duplicate concurrent work on the same key(s). try_claim() is all-or-nothing."""
    def __init__(self):
        self._keys = set()
        self._lock = threading.Lock()

    def try_claim(self, *keys):
        with self._lock:
            if any(k in self._keys for k in keys):
                return False
            self._keys.update(keys)
            return True

    def release(self, *keys):
        with self._lock:
            self._keys.difference_update(keys)


_loudness_inflight = _InFlightGuard()
_duration_inflight = _InFlightGuard()
_dims_inflight = _InFlightGuard()

try:
    _cpu_n = os.cpu_count() or 4
except Exception:
    _cpu_n = 4
_DURATION_POOL_SIZE = max(2, min(6, _cpu_n - 1))
_duration_pool = []
_duration_pool_lock = threading.Lock()
_duration_pool_rr = 0

def _get_duration_probe_slot():
    global _duration_pool, _duration_pool_rr
    if not _duration_pool:
        with _duration_pool_lock:
            if not _duration_pool:
                import vlc
                _duration_pool = [[vlc.Instance("--no-xlib", "--quiet"),
                                   threading.Lock()]
                                  for _ in range(_DURATION_POOL_SIZE)]
    with _duration_pool_lock:
        slot = _duration_pool[_duration_pool_rr % len(_duration_pool)]
        _duration_pool_rr += 1
    return slot


# ============================================================================
# SECTION: Track ordering & show grouping
# ============================================================================


def _natural_ep_key(item):
    p = item.get("path", item.get("file", "")) if isinstance(item, dict) else str(item)
    name = os.path.splitext(os.path.basename(p))[0].lower()
    m = re.search(r's(\d+)[\s._-]*[ex][\s._-]*(\d+)', name)
    if m: return (0, int(m.group(1)), int(m.group(2)), name)
    m = re.search(r'season[\s._-]*(\d+).*?episode[\s._-]*(\d+)', name)
    if m: return (0, int(m.group(1)), int(m.group(2)), name)
    m = re.search(r'ep(?:isode)?\W*(\d+)', name)
    if m: return (0, 0, int(m.group(1)), name)
    parts = re.split(r'(\d+)', name)
    norm = tuple((0, int(x)) if x.isdigit() else (1, x) for x in parts)
    return (1, norm)


def build_ordered_tracks(items, sched_mode="natural", seed=0):
    tracks = []
    for a in items:
        p = a.get("path", a.get("file", "")) if isinstance(a, dict) else str(a)
        if p and _cached_path_exists(p):
            d = a.get("duration", 1800) if isinstance(a, dict) else 1800
            d = d if d and d > 0 else 1800
            tracks.append({"path": p, "duration": d})
    if not tracks:
        return []

    if sched_mode == "marathon":
        show_names, show_episodes = group_shows_flexible(tracks)
        import random as _random
        rng = _random.Random(seed)
        rng.shuffle(show_names)
        return [ep for sn in show_names for ep in show_episodes[sn]]

    import collections as _col
    dm = _col.defaultdict(list)
    for t in tracks:
        dm[os.path.dirname(t["path"])].append(t)
    for k in dm:
        dm[k].sort(key=_natural_ep_key)

    groups = [dm[k] for k in sorted(dm.keys())]
    if sched_mode == "random_slots":
        import random as _random
        rng = _random.Random(seed)
        rng.shuffle(groups)
    return [t for g in groups for t in g]


def _flex_path_parts(path):
    parts = [p for p in re.split(r"[\\/]+", str(path)) if p and p not in (".", "..")]
    # WARNING: strip the drive letter here or a file under a generic wrapper folder gets titled "C:" and the filename fallback never runs.
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    return parts


def _flex_episode_marker(text):
    low = str(text).lower()
    m = re.search(r"\bs(\d+)[\s._-]*[ex][\s._-]*(\d+)\b", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})[\s._-]*x[\s._-]*(\d{1,3})\b", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\bseason[\s._-]*(\d+).*?\bepisode[\s._-]*(\d+)\b", low)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\bep(?:isode)?[\s._-]*(\d+)\b", low)
    if m:
        return 0, int(m.group(1))
    return None


def _flex_clean_series_title(name):
    s = _strip_leading_index(str(name)).strip()
    s = re.sub(r"(?i)[\s._\-(]+seasons?[\s._-]*\d+(?:[\s._-]*to[\s._-]*\d+)?\b.*$", "", s)
    s = re.sub(r"(?i)[\s._\-(]+s\d{1,2}(?:[\s._-]*-[\s._-]*s?\d{1,2})?\b.*$", "", s)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.()")
    return s


def _flex_is_generic_folder(name):
    return _normalize_show_key(name) in {
        "tv", "television", "shows", "series", "media", "videos", "video",
        "collections", "collection", "new folder", "complete series",
        "complete tv series", "adventure", "comedy", "drama", "cartoons",
        "cartoon", "anime", "documentaries", "documentary",
    }


def _flex_is_non_episode_track(track):
    """Exclude obvious extras while preserving normal and aired pilots."""
    path = track.get("path", track.get("file", "")) if isinstance(track, dict) else str(track)
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    return bool(re.search(
        r"(?i)(?:^|[ ._()\[\]-])(?:unaired[ ._-]*pilot|unaired|bonus|extras?|featurette|trailer|sample)(?:$|[ ._()\[\]-])",
        stem,
    ))


def _flex_series_and_season(track):
    path = track.get("path", track.get("file", "")) if isinstance(track, dict) else str(track)
    parts = _flex_path_parts(path)
    if not parts:
        return "", None, None
    filename = parts[-1]
    folders = parts[:-1]
    filename_stem = filename.rsplit(".", 1)[0]
    marker = _flex_episode_marker(filename_stem)
    season_number = marker[0] if marker else None

    title = None
    for idx in range(len(folders) - 1, -1, -1):
        folder = folders[idx]
        bare = _bare_season_number(folder)
        suffix = _season_folder_base(folder)
        if suffix:
            title = suffix[0]
            season_number = season_number if season_number is not None else suffix[2]
            break
        if bare is not None:
            season_number = season_number if season_number is not None else bare
            if idx > 0:
                title = folders[idx - 1]
            break

    if not title:
        for folder in reversed(folders):
            cleaned = _flex_clean_series_title(folder)
            if cleaned and not _flex_is_generic_folder(cleaned):
                title = cleaned
                break

    if (not title or _flex_is_generic_folder(title)) and marker:
        before = re.split(r"(?i)\bs\d+[\s._-]*e\d+\b|\b\d{1,2}[\s._-]*x[\s._-]*\d+\b",
                           filename_stem, maxsplit=1)[0]
        cleaned = _flex_clean_series_title(before)
        if not cleaned and re.search(r"[A-Za-z]", re.sub(
                r"(?i)\bs\d+[\s._-]*e\d+\b|\b\d{1,2}[\s._-]*x[\s._-]*\d+\b", " ", filename_stem)):
            cleaned = clean_video_display_title(filename)
        if cleaned:
            title = cleaned

    title = _flex_clean_series_title(title or os.path.splitext(filename)[0])
    return title, season_number, marker[1] if marker else None


def group_shows_flexible(raw_tracks):
    """Resolve arbitrary layouts into stable series/season/episode groups."""
    import collections as _col
    groups = _col.defaultdict(list)
    display_names = {}
    for track in raw_tracks:
        if not isinstance(track, dict):
            track = {"path": str(track)}
        if _flex_is_non_episode_track(track):
            continue
        title, season, episode = _flex_series_and_season(track)
        if not title:
            continue
        key = _normalize_show_key(title)
        if not key:
            continue
        item = dict(track)
        item["_series_key"] = key
        item["_series_title"] = title
        # WARNING: the guide must not reconstruct a show name from an episode filename.
        item["title"] = title
        item["_season_number"] = season if season is not None else 0
        item["_episode_number"] = episode if episode is not None else 0
        display_names.setdefault(key, title)
        groups[key].append(item)

    result = {}
    for key, items in groups.items():
        best = {}
        order = []
        for item in items:
            path = item.get("path", item.get("file", ""))
            stem = os.path.splitext(os.path.basename(str(path)))[0]
            stem = re.sub(r"(?i)(?:\.ia|[\s._-]+copy(?:[\s._-]+\d+)?|\s*\(\d+\))+$", "", stem)
            identity = (key, item["_season_number"], item["_episode_number"],
                        _normalize_show_key(stem) if not item["_episode_number"] else "")
            if identity not in best:
                best[identity] = item
                order.append(identity)
            else:
                old = best[identity]
                old_path = str(old.get("path", ""))
                old_marked = bool(re.search(r"(?i)(\.ia\.|- copy|\(\d+\))", old_path))
                new_marked = bool(re.search(r"(?i)(\.ia\.|- copy|\(\d+\))", str(path)))
                if old_marked and not new_marked:
                    best[identity] = item
        clean = [best[k] for k in order]
        clean.sort(key=lambda x: (
            x["_season_number"], x["_episode_number"],
            _natural_ep_key(x),
        ))
        result[display_names[key]] = clean

    show_names = sorted(result)
    return show_names, {sn: result[sn] for sn in show_names}


def _strip_leading_index(name):
    return re.sub(r'^\s*\d+\s*[.)\-_]\s+', '', name).strip()


def _normalize_show_key(s):
    s = re.sub(r'[._\-]+', ' ', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip().lower()


def _bare_season_number(dirname):
    name = dirname.strip()
    m = re.match(r'^seasons?[\s._-]*(\d+)(?!\s*-\s*\d)\b', name, re.IGNORECASE)
    if not m:
        m = re.match(r'^s(\d{1,2})(?:[\s._-]|$)', name, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def _season_folder_base(show_name):
    m = re.match(r'^(.*?)[\s._\-(]+seasons?[\s._-]*(\d+)(?!\s*-\s*\d)\b', show_name, re.IGNORECASE)
    if not m:
        m = re.match(r'^(.*?)[\s._\-(]+s(\d{2})(?:[\s._-]|$)', show_name, re.IGNORECASE)
    if not m:
        return None
    base_raw = m.group(1).strip(' ._-(')
    if not base_raw:
        return None
    return base_raw, _normalize_show_key(base_raw), int(m.group(2))


# ============================================================================
# SECTION: Track durations, movie detection & de-duplication
# ============================================================================


MOVIE_LENGTH_THRESHOLD_SECONDS = 4800


SIX_HOUR_BLOCKS = [
    ("12-4am", 0, 4),
    ("4-8am", 4, 8),
    ("8-12pm", 8, 12),
    ("12-4pm", 12, 16),
    ("4-8pm", 16, 20),
    ("8-12am", 20, 24),
]
SIX_HOUR_BLOCK_NAMES = [b[0] for b in SIX_HOUR_BLOCKS]


def resolve_track_duration(media_probe_cache, path, stored_duration, default=1800):
    if path:
        try:
            norm = os.path.normpath(str(path))
            cached = (media_probe_cache or {}).get(norm)
            cached_dur = cached.get("duration") if isinstance(cached, dict) else None
            if cached_dur and cached_dur > 0:
                return int(cached_dur)
        except Exception:
            pass
    d = stored_duration
    return d if d and d > 0 else default


def track_duration_is_estimated(media_probe_cache, path, stored_duration):
    """Report whether a resolved duration still came from the fallback."""
    try:
        if path:
            cached = (media_probe_cache or {}).get(os.path.normpath(str(path)))
            cached_dur = cached.get("duration") if isinstance(cached, dict) else None
            if cached_dur and cached_dur > 0:
                return False
    except Exception:
        pass
    return not (stored_duration and stored_duration > 0)


def _cached_lineup_durations_stale(cached_durations, media_probe_cache,
                                    movie_threshold_seconds=None,
                                    material_diff_seconds=60):
    """True if a frozen {path: duration} schedule snapshot has drifted from the live probe cache."""
    # WARNING: mirrors the same-named helper in retro_tv_emulator.py; keep both in step.
    if not cached_durations or not isinstance(media_probe_cache, dict):
        return False
    if movie_threshold_seconds is None:
        movie_threshold_seconds = MOVIE_LENGTH_THRESHOLD_SECONDS
    for path, frozen in cached_durations.items():
        if not (frozen and frozen > 0):
            continue
        probed = media_probe_cache.get(path)
        real = probed.get("duration") if isinstance(probed, dict) else probed
        if not (real and real > 0):
            continue
        crosses_movie_line = (
            (frozen >= movie_threshold_seconds) != (real >= movie_threshold_seconds))
        if crosses_movie_line or abs(float(real) - float(frozen)) > material_diff_seconds:
            return True
    return False


def is_movie_track(track, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS):
    if not isinstance(track, dict):
        return False
    path = track.get("path") or track.get("file") or ""
    if path:
        try:
            p = str(path).lower().replace("\\", "/")
            if "/movies/" in p:
                return True
            if any(marker in p for marker in ("/tv/", "/series/", "/shows/")):
                return False
        except Exception:
            pass
    dur = track.get("duration", 0)
    return bool(dur and dur >= threshold_seconds)


def stable_series_id(series_title, channel_id=None):
    """Return a filesystem-independent identity for playback state."""
    value = _normalize_show_key(series_title)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = value or "unknown-series"
    if channel_id is not None and str(channel_id).strip():
        channel = re.sub(r"[^a-z0-9]+", "-", _normalize_show_key(channel_id)).strip("-")
        if channel:
            return f"{channel}:{value}"
    return value


def migrate_show_positions(show_pos, show_names, channel_id=None):
    """Copy legacy title-keyed positions to channel-scoped keys."""
    migrated = dict(show_pos or {})
    report = {"migrated": [], "already_scoped": [], "unmatched": []}
    for title in show_names or []:
        old_key = str(title)
        new_key = stable_series_id(title, channel_id)
        if new_key == old_key:
            report["already_scoped"].append(old_key)
            continue
        if new_key in migrated:
            report["already_scoped"].append(new_key)
            continue
        if old_key in migrated:
            migrated[new_key] = copy.deepcopy(migrated[old_key])
            report["migrated"].append({"from": old_key, "to": new_key})
        else:
            report["unmatched"].append(old_key)
    return migrated, report


def _looks_like_episode_filename(stem):
    import re
    low = stem.lower()
    if re.search(r's\d+[ex]\d+', low):
        return True
    if re.search(r'season\s*\d+.*?episode\s*\d+', low):
        return True
    if re.search(r'\bep(?:isode)?\W*\d+', low):
        return True
    return False


def _movie_part_info(track):
    import re
    if not isinstance(track, dict):
        return None
    p = track.get("path", track.get("file", ""))
    stem = os.path.splitext(os.path.basename(p))[0]
    if _looks_like_episode_filename(stem):
        return None
    m = re.search(r'(?i)^(.*?)[\s._\-:]*\b(?:part|pt)\.?\s*(\d+)\b(?:\s*of\s*\d+)?(.*)$', stem)
    if not m:
        return None
    base = m.group(1) + " " + m.group(3)
    base = re.sub(r'[\s._\-:]+', ' ', base).strip().lower()
    if not base:
        return None
    return base, int(m.group(2))


def pair_movie_parts(raw_tracks, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS):
    import collections as _col
    by_base = _col.defaultdict(dict)
    untouched = []
    for t in raw_tracks:
        info = _movie_part_info(t)
        if info is None:
            untouched.append(t)
            continue
        base, num = info
        if num in by_base[base]:
            by_base[base]["_ambiguous"] = True
        by_base[base][num] = t

    result = list(untouched)
    for base, parts in by_base.items():
        ambiguous = parts.pop("_ambiguous", False)
        if not ambiguous and set(parts.keys()) == {1, 2}:
            p1, p2 = parts[1], parts[2]
            p1_path = p1.get("path", p1.get("file", ""))
            p2_path = p2.get("path", p2.get("file", ""))
            p1_dur  = p1.get("duration", 0) or 0
            p2_dur  = p2.get("duration", 0) or 0
            combined_dur = p1_dur + p2_dur
            if combined_dur >= threshold_seconds:
                result.append({
                    "path":              p1_path,
                    "duration":          combined_dur,
                    "is_pair":           True,
                    "pair_ep1":          p1,
                    "pair_ep2":          p2,
                    "pair_ep1_path":     p1_path,
                    "pair_ep1_duration": p1_dur,
                    "pair_ep2_path":     p2_path,
                    "pair_ep2_duration": p2_dur,
                })
                continue
        result.extend(parts.values())
    return result


_DUPLICATE_FILE_MARKER_RE = re.compile(r'\.(?:ia)(\.[^.\\/]+)$', re.IGNORECASE)


def _dedupe_key_for_path(path):
    return _DUPLICATE_FILE_MARKER_RE.sub(r'\1', str(path))


def dedupe_duplicate_episode_files(episodes):
    best_by_key = {}
    order = []
    for ep in episodes:
        path = ep.get("path", ep.get("file", "")) if isinstance(ep, dict) else str(ep)
        key = _dedupe_key_for_path(path)
        if key not in best_by_key:
            best_by_key[key] = ep
            order.append(key)
        else:
            existing_path = best_by_key[key].get("path", best_by_key[key].get("file", "")) \
                if isinstance(best_by_key[key], dict) else str(best_by_key[key])
            existing_is_marked = existing_path != key
            current_is_marked = path != key
            if existing_is_marked and not current_is_marked:
                best_by_key[key] = ep
    return [best_by_key[k] for k in order]


def _dedupe_key_ignore_extension(path):
    d = os.path.dirname(str(path))
    base = os.path.splitext(os.path.basename(str(path)))[0]
    return os.path.join(d, base).lower()


_EXTENSION_STABILITY_PRIORITY = [
    ".mkv",
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".avi",
    ".wmv",
    ".flv",
]
_EXTENSION_STABILITY_RANK = {ext: i for i, ext in enumerate(_EXTENSION_STABILITY_PRIORITY)}


def _extension_stability_rank(path):
    ext = os.path.splitext(str(path))[1].lower()
    return _EXTENSION_STABILITY_RANK.get(ext, len(_EXTENSION_STABILITY_PRIORITY))


def dedupe_same_name_different_extension(episodes):
    """Same folder and filename but a different container."""
    best_by_key = {}
    order = []
    for ep in episodes:
        path = ep.get("path", ep.get("file", "")) if isinstance(ep, dict) else str(ep)
        key = _dedupe_key_ignore_extension(path)
        if key not in best_by_key:
            best_by_key[key] = ep
            order.append(key)
        else:
            existing = best_by_key[key]
            existing_path = existing.get("path", existing.get("file", "")) \
                if isinstance(existing, dict) else str(existing)
            if _extension_stability_rank(path) < _extension_stability_rank(existing_path):
                best_by_key[key] = ep
    return [best_by_key[k] for k in order]


_split_movies_cache_lock = threading.Lock()
_split_movies_cache = {}


def _split_movies_fingerprint(raw_tracks, threshold_seconds):
    try:
        return (threshold_seconds, tuple(
            (t.get("path", t.get("file", "")) if isinstance(t, dict) else str(t),
             t.get("duration", 0) if isinstance(t, dict) else 0)
            for t in raw_tracks
        ))
    except Exception:
        return None


def split_movies_and_series(raw_tracks, threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS, channel_id=None):
    _fp = _split_movies_fingerprint(raw_tracks, threshold_seconds) if channel_id is not None else None
    if _fp is not None:
        with _split_movies_cache_lock:
            _cached = _split_movies_cache.get(channel_id)
        if _cached is not None and _cached[0] == _fp:
            _c_movie_pool, _c_show_names, _c_show_episodes = _cached[1]
            return (list(_c_movie_pool), list(_c_show_names),
                    {sn: list(eps) for sn, eps in _c_show_episodes.items()})

    raw_tracks = dedupe_duplicate_episode_files(raw_tracks)
    raw_tracks = dedupe_same_name_different_extension(raw_tracks)
    raw_tracks = pair_movie_parts(raw_tracks, threshold_seconds)
    movie_pool = []
    series_tracks = []
    for t in raw_tracks:
        if is_movie_track(t, threshold_seconds):
            movie_pool.append(t)
        else:
            series_tracks.append(t)
    show_names, show_episodes = group_shows_flexible(series_tracks)
    show_episodes = {
        sn: dedupe_same_name_different_extension(dedupe_duplicate_episode_files(eps))
        for sn, eps in show_episodes.items()
    }

    if _fp is not None:
        with _split_movies_cache_lock:
            _split_movies_cache[channel_id] = (
                _fp,
                (list(movie_pool), list(show_names),
                 {sn: list(eps) for sn, eps in show_episodes.items()}),
            )

    return movie_pool, show_names, show_episodes


# ============================================================================
# SECTION: Short-episode pairing & movie placement
# ============================================================================


SHORT_EPISODE_PAIRING_VERSION = 3
LEFTOVERS_TITLE = "The Leftovers"


def _episode_series_key(episode):
    """Return the series identity used by short-episode pairing."""
    # WARNING: two unrelated short files must not pair just because they are adjacent in the flattened list.
    if isinstance(episode, dict):
        explicit = episode.get("_series_key")
        if explicit:
            return str(explicit)
        path = episode.get("path", episode.get("file", ""))
    else:
        path = str(episode)
    try:
        title, _season, _episode = _flex_series_and_season(
            {"path": path}
        )
        return _normalize_show_key(title) if title else ""
    except Exception:
        return ""


def pair_short_episodes(episodes, threshold_seconds=900):
    if threshold_seconds <= 0:
        return list(episodes)
    result = []
    leftovers = []
    i = 0
    while i < len(episodes):
        ep = episodes[i]
        dur = ep.get("duration", 1800) if isinstance(ep, dict) else 1800
        if dur < threshold_seconds and i + 1 < len(episodes):
            nxt = episodes[i + 1]
            nxt_dur = nxt.get("duration", 1800) if isinstance(nxt, dict) else 1800
            same_series = (_episode_series_key(ep) and
                           _episode_series_key(ep) == _episode_series_key(nxt))
            if nxt_dur < threshold_seconds and same_series:
                ep_path = ep.get("path", ep.get("file", "")) if isinstance(ep, dict) else str(ep)
                nxt_path = nxt.get("path", nxt.get("file", "")) if isinstance(nxt, dict) else str(nxt)
                result.append({"path": ep_path, "duration": dur + nxt_dur,
                               "is_pair": True, "pair_ep1": ep, "pair_ep2": nxt,
                               "pair_ep1_path": ep_path, "pair_ep1_duration": dur,
                               "pair_ep2_path": nxt_path, "pair_ep2_duration": nxt_dur})
                i += 2
                continue
        if dur < threshold_seconds:
            leftovers.append(ep)
        else:
            result.append(ep)
        i += 1
    if leftovers:
        result.append({"path": leftovers[0].get("path", ""),
                       "duration": sum(e.get("duration", 0) for e in leftovers),
                       "title": LEFTOVERS_TITLE, "_series_title": LEFTOVERS_TITLE,
                       "is_bundle": True, "bundle_items": leftovers})
    return result


MOVIE_HEAVY_RATIO = 2

MOVIES_PER_BLOCK = 2

MOVIE_ROTATION_KEY = "__MOVIES__"

_MOVIE_DURATION_FALLBACK_SECONDS = 6000


def _movie_runtime_budget(movie_pool, block_duration_seconds,
                           series_total_episodes, series_total_runtime_seconds=None):
    durations = [
        t.get("duration", 0) for t in movie_pool
        if isinstance(t, dict) and t.get("duration", 0) > 0
    ]
    movie_total_runtime = sum(durations)
    if series_total_runtime_seconds is not None and series_total_runtime_seconds > 0:
        series_runtime = float(series_total_runtime_seconds)
    else:
        series_runtime = float(max(series_total_episodes, 0) * _MOVIE_DURATION_FALLBACK_SECONDS)
    if series_runtime <= 0:
        return float(block_duration_seconds)
    return float(block_duration_seconds) * (
        movie_total_runtime / float(movie_total_runtime + series_runtime)
    )


def _fit_movies_to_runtime(candidates, target_seconds, max_movies):
    """Play whatever the rotation drew -- don't drop candidates for a tighter runtime fit."""
    if not candidates or max_movies <= 0:
        return []
    return list(candidates[:max_movies])


# VALUES CHANGE HERE
RANDOM_REPEAT_HOLD_BACK = 10


def _draw_from_shuffle_bag(state, n, count, seed_prefix, hold_back=RANDOM_REPEAT_HOLD_BACK):
    """Draw `count` indices from a persistent per-cycle shuffle bag with a don't-repeat window."""
    import random as _random

    if isinstance(state, dict) and "bag" in state:
        bag = [i for i in state.get("bag", []) if 0 <= i < n]
        cycle = int(state.get("cycle", 0) or 0)
        last = state.get("last")
        recent = [i for i in state.get("recent", []) if 0 <= i < n]
    else:
        bag = []
        cycle = 0
        if isinstance(state, dict):
            last = state.get("last")
        else:
            last = state if isinstance(state, int) else None
        recent = [last] if isinstance(last, int) else []

    eff_hold_back = min(hold_back, max(0, (n - 1) // 2)) if n > 1 else 0

    picked = []
    for _ in range(count):
        if n == 1:
            idx = 0
        else:
            if not bag:
                cycle += 1
                bag = list(range(n))
                bag_rng = _random.Random(f"{seed_prefix}:{cycle}")
                bag_rng.shuffle(bag)
                if eff_hold_back > 0:
                    blocked = set(recent[-eff_hold_back:])
                    if blocked and len(blocked) < len(bag):
                        allowed = [i for i in bag if i not in blocked]
                        held = [i for i in bag if i in blocked]
                        bag = allowed + held
                elif len(bag) > 1 and bag[0] == last:
                    j = bag_rng.randrange(1, len(bag))
                    bag[0], bag[j] = bag[j], bag[0]
            idx = bag.pop(0)
        picked.append(idx)
        last = idx
        recent.append(idx)
        if hold_back > 0:
            recent = recent[-hold_back:]

    return picked, {"bag": bag, "cycle": cycle, "last": last, "recent": recent}


def _rotation_take_random(show_pos, name, count, eps, day_seed):
    if not eps:
        return []
    n = len(eps)
    state = show_pos.get(name)
    idxs, new_state = _draw_from_shuffle_bag(state, n, count, f"{day_seed}:{name}")
    show_pos[name] = new_state
    return [eps[i] for i in idxs]


def _movie_slot_count(movie_pool, block_duration_seconds, series_total_episodes,
                      scheduling_mode, series_total_runtime_seconds=None):
    if not movie_pool or block_duration_seconds is None or block_duration_seconds <= 0:
        return 0

    durations = [t.get("duration", 0) for t in movie_pool if isinstance(t, dict) and t.get("duration", 0) > 0]
    avg_dur = (sum(durations) / len(durations)) if durations else _MOVIE_DURATION_FALLBACK_SECONDS
    avg_dur = max(avg_dur, 60)

    budget = _movie_runtime_budget(
        movie_pool, block_duration_seconds, series_total_episodes,
        series_total_runtime_seconds,
    )

    n_pick = max(1, round(budget / avg_dur))
    n_pick = min(n_pick, len(movie_pool))

    if scheduling_mode == "two_slots" and 1 < n_pick < len(movie_pool) and n_pick % 2:
        n_pick += 1
    elif scheduling_mode == "two_slots" and n_pick % 2 and n_pick == len(movie_pool) and n_pick > 1:
        n_pick -= 1

    return n_pick


def _place_movies_in_block(video_tracks, picked_movies, day_seed, movie_heavy,
                            scheduling_mode="random_slots"):
    if not picked_movies:
        return video_tracks

    if scheduling_mode == "two_slots" and len(picked_movies) > 1:
        units = [picked_movies[i:i + 2] for i in range(0, len(picked_movies), 2)]
    else:
        units = [[m] for m in picked_movies]

    result = list(video_tracks)

    if not result:
        return [item for unit in units for item in unit]

    series_runtime = sum(
        float(item.get("duration", 0) or 0)
        for item in result if isinstance(item, dict)
    )
    if series_runtime <= 0:
        series_runtime = float(len(result))
    used_idxs = []
    for i, u in enumerate(units):
        target = series_runtime * (i + 1) / float(len(units) + 1)
        elapsed = 0.0
        idx = len(result)
        for candidate, item in enumerate(result):
            elapsed += float(item.get("duration", 0) or 0) \
                if isinstance(item, dict) else 1.0
            if elapsed >= target:
                idx = candidate + 1
                break
        guard = 0
        while (idx in used_idxs or (idx - 1) in used_idxs) and guard < len(result) + 2:
            idx += 1
            guard += 1
        idx = min(idx, len(result))
        used_idxs.append(idx)
        result[idx:idx] = u

    return result


# ============================================================================
# SECTION: Show rotation
# ============================================================================


def build_show_rotation(raw_tracks, scheduling_mode, show_pos, day_seed, pair_threshold_seconds=900,
                         movie_threshold_seconds=MOVIE_LENGTH_THRESHOLD_SECONDS,
                         movies_per_block=MOVIES_PER_BLOCK,
                         episode_order_mode="sequential",
                         fixed_show_order=None,
                         block_duration_seconds=None,
                         channel_id=None,
                         order_seed=None):
    import random as _random

    show_pos = dict(show_pos or {})
    movie_pool, show_names, show_episodes = split_movies_and_series(raw_tracks, movie_threshold_seconds, channel_id=channel_id)
    show_pos = migrate_show_positions(show_pos, show_names, channel_id)[0]

    if pair_threshold_seconds > 0:
        show_episodes = {
            sn: pair_short_episodes(eps, threshold_seconds=pair_threshold_seconds)
            for sn, eps in show_episodes.items()
        }

    def _order_mode_for(show_name):
        if isinstance(episode_order_mode, dict):
            return episode_order_mode.get(show_name, episode_order_mode.get("__default__", "sequential"))
        return episode_order_mode or "sequential"

    def _take_sequential(show_name, count, eps):
        state_key = stable_series_id(show_name, channel_id)
        stored = show_pos.get(state_key, show_pos.get(show_name, 0))
        pos = stored % len(eps) if isinstance(stored, int) else 0
        out = [eps[(pos + n) % len(eps)] for n in range(count)]
        show_pos[state_key] = (pos + count) % len(eps)
        return out

    def _take_random(show_name, count, eps):
        n = len(eps)
        state_key = stable_series_id(show_name, channel_id)
        state = show_pos.get(state_key, show_pos.get(show_name))
        idxs, new_state = _draw_from_shuffle_bag(state, n, count, f"{day_seed}:{show_name}")
        show_pos[state_key] = new_state
        return [eps[i] for i in idxs]

    def _take(show_name, count):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return []
        if _order_mode_for(show_name) == "random":
            return _take_random(show_name, count, eps)
        return _take_sequential(show_name, count, eps)

    def _fair_show_order(order, requested_counts):
        """Skip over-visited small shows without creating empty schedules."""
        fairness = show_pos.get("__FAIRNESS__")
        if not isinstance(fairness, dict):
            fairness = {}
        usage = fairness.get("used", {})
        if not isinstance(usage, dict):
            usage = {}
        run = int(fairness.get("run", 0) or 0) + 1
        fairness["run"] = run

        usable = [sn for sn in order if show_episodes.get(sn)]
        if len(usable) <= 1:
            show_pos["__FAIRNESS__"] = {"run": run, "used": usage}
            return usable

        ratios = []
        for sn in usable:
            n = max(1, len(show_episodes.get(sn) or []))
            ratios.append(float(usage.get(sn, 0) or 0) / n)
        ratios.sort()
        baseline = ratios[len(ratios) // 2] if ratios else 0.0
        pressure_limit = max(0.25, baseline * 1.5)

        eligible = []
        for sn in usable:
            n = max(1, len(show_episodes.get(sn) or []))
            projected = (float(usage.get(sn, 0) or 0) +
                         requested_counts.get(sn, 1)) / n
            if projected <= pressure_limit:
                eligible.append(sn)

        if not eligible:
            eligible = usable

        for sn in eligible:
            usage[sn] = int(usage.get(sn, 0) or 0) + requested_counts.get(sn, 1)
        fairness["used"] = usage
        show_pos["__FAIRNESS__"] = fairness
        return [sn for sn in order if sn in set(eligible)]

    video_tracks = []

    if fixed_show_order:
        _known = set(show_names)
        order = [sn for sn in fixed_show_order if sn in _known]
        _placed = set(order)
        order.extend(sorted(sn for sn in show_names if sn not in _placed))
    else:
        rng = _random.Random(order_seed if order_seed is not None else day_seed)
        order = list(show_names)
        rng.shuffle(order)
    show_order_used = order

    if scheduling_mode == "one_slot":
        requested_counts = {sn: 1 for sn in order}
        for sn in _fair_show_order(order, requested_counts):
            video_tracks.extend(_take(sn, requested_counts[sn]))

    elif scheduling_mode == "two_slots":
        requested_counts = {sn: 2 for sn in order}
        for sn in _fair_show_order(order, requested_counts):
            video_tracks.extend(_take(sn, requested_counts[sn]))

    else:
        _slot_counts = {sn: len(eps_list)
                        for sn, eps_list in show_episodes.items()}
        max_slots = max(_slot_counts.values(), default=1)
        requested_counts = {}
        for sn in order:
            slot_len = _slot_counts.get(sn, 0)
            requested_counts[sn] = max(1, min(slot_len, round((slot_len / max_slots) * 3))) \
                if slot_len else 0
        for sn in _fair_show_order(order, requested_counts):
            eps = show_episodes.get(sn) or []
            if not eps:
                continue
            run_slots = requested_counts[sn]
            video_tracks.extend(_take(sn, run_slots))


    if movie_pool:
        series_total = sum(len(v) for v in show_episodes.values())
        series_runtime = sum(
            (ep.get("duration", 0) if isinstance(ep, dict) else 0)
            for eps in show_episodes.values()
            for ep in eps
        )
        _bds = block_duration_seconds if block_duration_seconds else max(movies_per_block, 1) * 4 * 3600
        n_movie_pick = _movie_slot_count(
            movie_pool, _bds, series_total, scheduling_mode,
            series_total_runtime_seconds=series_runtime,
        )
        _ordered_pool = sorted(movie_pool, key=lambda t: t.get("path", ""))
        _movie_seed = order_seed if (order_seed is not None and show_pos.get(MOVIE_ROTATION_KEY) is None) else day_seed
        picked_movies = _rotation_take_random(show_pos, MOVIE_ROTATION_KEY, n_movie_pick, _ordered_pool, _movie_seed)
        movie_budget = _movie_runtime_budget(
            movie_pool, _bds, series_total, series_runtime
        )
        picked_movies = _fit_movies_to_runtime(
            picked_movies, movie_budget, n_movie_pick
        )
        movie_heavy = (series_total == 0) or (len(movie_pool) > MOVIE_HEAVY_RATIO * series_total)
        video_tracks = _place_movies_in_block(video_tracks, picked_movies, day_seed, movie_heavy,
                                               scheduling_mode=scheduling_mode)

    return video_tracks, show_pos, show_order_used


def _episode_label(path):
    name = os.path.splitext(os.path.basename(str(path)))[0]
    m = re.search(r's(\d+)[\s._-]*[ex][\s._-]*(\d+)', name, re.IGNORECASE)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = re.search(r'season[\s._-]*(\d+).*?episode[\s._-]*(\d+)', name, re.IGNORECASE)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = re.search(r'ep(?:isode)?\W*(\d+)', name, re.IGNORECASE)
    if m:
        return f"Episode {int(m.group(1))}"
    return None


def _slots_per_block_for_show(show_name, show_episodes, scheduling_mode):
    eps = show_episodes.get(show_name) or []
    if not eps:
        return 0
    if scheduling_mode == "one_slot":
        return 1
    if scheduling_mode == "two_slots":
        return 2
    slot_counts = {sn: len(v) for sn, v in show_episodes.items()}
    max_slots = max(slot_counts.values(), default=1)
    slot_len = slot_counts.get(show_name, len(eps))
    return max(1, min(slot_len, round((slot_len / max_slots) * 3)))


def fast_forward_show_pos(raw_tracks, scheduling_mode, show_pos, day_seeds,
                           episode_order_mode="sequential", pair_threshold_seconds=900,
                           fixed_show_order=None, block_duration_seconds=None,
                           movies_per_block=MOVIES_PER_BLOCK, max_simulated_blocks=21900,
                           channel_id=None, order_seed=None):
    show_pos = dict(show_pos or {})
    if not day_seeds:
        return show_pos, fixed_show_order, {}

    seeds = list(day_seeds)[-max_simulated_blocks:]
    n_blocks = len(seeds)

    movie_pool, show_names, show_episodes = split_movies_and_series(
        raw_tracks, channel_id=channel_id
    )
    if pair_threshold_seconds > 0:
        # WARNING: count paired short episodes the same way normal scheduling does or a block catch-up shifts the saved position.
        show_episodes = {
            sn: pair_short_episodes(eps, threshold_seconds=pair_threshold_seconds)
            for sn, eps in show_episodes.items()
        }

    def _order_mode_for(show_name):
        if isinstance(episode_order_mode, dict):
            return episode_order_mode.get(show_name, episode_order_mode.get("__default__", "sequential"))
        return episode_order_mode

    def _label_before(show_name):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return None
        state = show_pos.get(stable_series_id(show_name, channel_id),
                             show_pos.get(show_name))
        if _order_mode_for(show_name) == "random":
            idx = state.get("last") if isinstance(state, dict) else (state if isinstance(state, int) else None)
        else:
            nxt = state if isinstance(state, int) else 0
            idx = (nxt - 1) % len(eps)
        if idx is None or not (0 <= idx < len(eps)):
            return None
        return _episode_label(eps[idx].get("path", ""))

    def _label_after(show_name):
        eps = show_episodes.get(show_name) or []
        if not eps:
            return None
        state = show_pos.get(stable_series_id(show_name, channel_id),
                             show_pos.get(show_name))
        if _order_mode_for(show_name) == "random":
            idx = state.get("last") if isinstance(state, dict) else (state if isinstance(state, int) else None)
        else:
            idx = state if isinstance(state, int) else 0
        if idx is None or not (0 <= idx < len(eps)):
            return None
        return _episode_label(eps[idx % len(eps)].get("path", ""))

    before_labels = {sn: _label_before(sn) for sn in show_names}

    order = fixed_show_order
    for seed in seeds:
        _tracks, show_pos, used_order = build_show_rotation(
            raw_tracks, scheduling_mode, show_pos, seed,
            pair_threshold_seconds=pair_threshold_seconds,
            episode_order_mode=episode_order_mode,
            fixed_show_order=order,
            block_duration_seconds=block_duration_seconds,
            movies_per_block=movies_per_block,
            channel_id=channel_id,
            order_seed=order_seed,
        )
        order = order or used_order

    report = {}
    for sn in show_names:
        per_block = _slots_per_block_for_show(sn, show_episodes, scheduling_mode)
        report[sn] = {
            "slots_skipped": per_block * n_blocks,
            "label_before": before_labels.get(sn),
            "label_after": _label_after(sn),
        }
    return show_pos, order, report


def note_random_episode_played(show_pos, show_name, episode_count, played_index,
                               channel_id=None):
    state_key = stable_series_id(show_name, channel_id)
    state = show_pos.get(state_key, show_pos.get(show_name))
    if isinstance(state, dict) and "bag" in state:
        bag = [i for i in state.get("bag", []) if 0 <= i < episode_count and i != played_index]
        cycle = int(state.get("cycle", 0) or 0)
        recent = [i for i in state.get("recent", []) if 0 <= i < episode_count]
    else:
        bag = []
        cycle = 0
        recent = []
    recent = (recent + [played_index])[-RANDOM_REPEAT_HOLD_BACK:]
    show_pos[state_key] = {"bag": bag, "cycle": cycle, "last": played_index, "recent": recent}


# ============================================================================
# SECTION: Commercial bundles & block timeline
# ============================================================================
COMMERCIAL_MAX_SECONDS = 5 * 60
COMMERCIAL_BUNDLE_CEILINGS = (15, 30, 45, 60, 90, 120, 180, COMMERCIAL_MAX_SECONDS)


def commercial_bundle_ceiling(duration):
    """Round a commercial's duration up to its bundle's ceiling."""
    # WARNING: round up, never down, so every ad fits any break budget its bundle fills.
    for ceiling in COMMERCIAL_BUNDLE_CEILINGS:
        if duration <= ceiling:
            return ceiling
    return COMMERCIAL_BUNDLE_CEILINGS[-1]


def build_block_timeline(video_tracks, commercial_tracks, commercial_placement, day_seed,
                          interrupt_floor_seconds=900,
                          max_interrupt_break_seconds=360,
                          max_end_break_seconds=600,
                          media_probe_cache=None,
                          commercial_state=None):
    import random as _random

    HALF_HOUR         = 1800
    # VALUES CHANGE HERE
    MAX_CATCHUP       = 180
    MIN_BREAK_GAP     = 300
    MIN_BREAK_SECONDS = 120

    def _dur_is_estimated(show):
        try:
            if isinstance(show, dict) and show.get("_duration_estimated"):
                return True
            if media_probe_cache is None:
                return False
            p = show.get("path") if isinstance(show, dict) else None
            if not p:
                return False
            cached = media_probe_cache.get(os.path.normpath(str(p)))
            cdur = cached.get("duration") if isinstance(cached, dict) else None
            if cdur and cdur > 0:
                return False
            stored = show.get("duration") if isinstance(show, dict) else None
            return not (stored and stored > 0 and stored != HALF_HOUR)
        except Exception:
            return False

    _bundles = {}
    for _com in (commercial_tracks or []):
        _cdur = _com.get("duration", 0) if isinstance(_com, dict) else 0
        if not _cdur or _cdur <= 0:
            continue
        _bundles.setdefault(commercial_bundle_ceiling(_cdur), []).append(_com)

    _saved_bundles = (commercial_state or {}).get("bundles")
    if not isinstance(_saved_bundles, dict):
        _saved_bundles = {}
    _bundle_cycle  = {}
    _bundle_recent = {}
    for _ceiling in _bundles:
        _saved = _saved_bundles.get(str(_ceiling))
        if not isinstance(_saved, dict):
            _saved = {}
        _bundle_cycle[_ceiling]  = int(_saved.get("cycle", 0) or 0)
        _bundle_recent[_ceiling] = [p for p in _saved.get("recent", []) if p]

    def _shuffled_ad_lap(pool_items, ceiling):
        _bundle_cycle[ceiling] = _bundle_cycle.get(ceiling, 0) + 1
        n = len(pool_items)
        order = list(range(n))
        lap_rng = _random.Random(f"{day_seed}:__commercials__:{ceiling}:{_bundle_cycle[ceiling]}")
        lap_rng.shuffle(order)
        recent = _bundle_recent.get(ceiling, [])
        hold_back = min(RANDOM_REPEAT_HOLD_BACK, n - 1) if n > 1 else 0
        if hold_back > 0:
            blocked_paths = set(recent[-hold_back:])
            blocked = {i for i in order if pool_items[i]["path"] in blocked_paths}
            if blocked and len(blocked) < len(order):
                allowed = [i for i in order if i not in blocked]
                held = [i for i in order if i in blocked]
                order = allowed + held
        return [pool_items[i] for i in order]

    _bundle_pools = {}
    _bundle_idx   = {}
    for _ceiling, _items in _bundles.items():
        _bundle_pools[_ceiling] = _shuffled_ad_lap(_items, _ceiling)
        _bundle_idx[_ceiling]   = 0

    _bundle_order = sorted(_bundle_pools.keys())
    _saved_turn_ceiling = (commercial_state or {}).get("turn_ceiling")
    _bundle_turn = _bundle_order.index(_saved_turn_ceiling) if _saved_turn_ceiling in _bundle_order else 0

    commercial_pool = list(commercial_tracks or [])

    timeline = []
    total_block_content = 0

    # WARNING: modified by the closures below via nonlocal.
    _deferred_fill  = 0
    _last_break_end = -MIN_BREAK_GAP
    _last_played_path = (commercial_state or {}).get("last")

    MAX_BREAKS_PER_HALF_HOUR = 2
    _half_hour_bucket        = 0
    _breaks_this_half_hour   = 0

    def _register_half_hour(t):
        nonlocal _half_hour_bucket, _breaks_this_half_hour
        bucket = int(t) // HALF_HOUR
        if bucket != _half_hour_bucket:
            _half_hour_bucket      = bucket
            _breaks_this_half_hour = 0

    def _half_hour_capped(t):
        """True once MAX_BREAKS_PER_HALF_HOUR breaks have aired in t's half-hour window."""
        # WARNING: every break-triggering path must check this so none adds a 3rd break.
        _register_half_hour(t)
        return _breaks_this_half_hour >= MAX_BREAKS_PER_HALF_HOUR

    def _note_ad_played(ceiling, path):
        nonlocal _last_played_path
        _last_played_path = path
        recent = _bundle_recent.setdefault(ceiling, [])
        recent.append(path)
        if len(recent) > RANDOM_REPEAT_HOLD_BACK:
            _bundle_recent[ceiling] = recent[-RANDOM_REPEAT_HOLD_BACK:]

    def _next_from_bundle(ceiling, exclude_paths):
        """Pull the next unused-in-this-break item from `ceiling`'s rotation, reshuffling when a lap ends."""
        pool = _bundle_pools.get(ceiling)
        if not pool:
            return None
        n = len(pool)
        seen = 0
        while seen < n:
            idx = _bundle_idx[ceiling]
            if idx > 0 and idx % n == 0:
                pool = _bundle_pools[ceiling] = _shuffled_ad_lap(pool, ceiling)
            com = pool[idx % n]
            _bundle_idx[ceiling] = idx + 1
            seen += 1
            if com["duration"] <= 0 or com["path"] in exclude_paths:
                continue
            return com
        return None

    def _pick_one(budget_seconds, exclude_paths):
        """Pick one commercial that fits `budget_seconds`, trying bundles in round-robin order."""
        nonlocal _bundle_turn
        n = len(_bundle_order)
        for offset in range(n):
            idx = (_bundle_turn + offset) % n
            ceiling = _bundle_order[idx]
            if ceiling > budget_seconds:
                continue
            com = _next_from_bundle(ceiling, exclude_paths)
            if com is None:
                continue
            _bundle_turn = (idx + 1) % n
            return com, ceiling
        return None, None

    def _show_entry(show, duration, start_time, seek_offset=0, interior_cut=False):
        e = {"path": show["path"], "duration": duration,
             "start_time": start_time, "is_commercial": False,
             "seek_offset": seek_offset}
        if _dur_is_estimated(show):
            e["duration_estimated"] = True
        if interior_cut:
            e["interior_cut"] = True
        if show.get("is_pair"):
            e["is_pair"]           = True
            e["pair_ep1_path"]     = show.get("pair_ep1_path", show["path"])
            e["pair_ep1_duration"] = show.get("pair_ep1_duration", 0)
            e["pair_ep2_path"]     = show.get("pair_ep2_path", "")
            e["pair_ep2_duration"] = show.get("pair_ep2_duration", 0)
        return e

    def _pack_break(budget_seconds):
        picked = []
        used = 0
        if budget_seconds <= 0 or not _bundle_pools:
            return picked, 0
        break_paths = set()
        while True:
            remaining = budget_seconds - used
            if remaining <= 0:
                break
            com, ceiling = _pick_one(remaining, break_paths)
            if com is None:
                break
            picked.append(com)
            used += com["duration"]
            break_paths.add(com["path"])
            _note_ad_played(ceiling, com["path"])
        return picked, used

    def _append_break(picked, t_block_content):
        """Append picked commercial entries to timeline and update _last_break_end."""
        nonlocal _last_break_end, _breaks_this_half_hour
        t = t_block_content
        for com in picked:
            timeline.append({"path": com["path"], "duration": com["duration"],
                             "start_time": t, "is_commercial": True,
                             "seek_offset": 0})
            t += com["duration"]
        if picked:
            _last_break_end = t
            _register_half_hour(t_block_content)
            _breaks_this_half_hour += 1
        return t

    def _pack_and_round(budget_seconds, t_block_content, max_seconds=None):
        nonlocal _deferred_fill
        if budget_seconds <= 0 or not _bundle_pools:
            return t_block_content
        capped = min(budget_seconds, max_seconds) if max_seconds is not None else budget_seconds
        if capped < budget_seconds:
            _deferred_fill += budget_seconds - capped
        picked, used = _pack_break(capped)
        t = _append_break(picked, t_block_content)
        leftover = int(capped) - used
        if leftover > 0:
            _deferred_fill += leftover
        return t

    def _pack_even_interior_breaks(show, show_dur, gap, num_breaks, t_block_content, next_mark):
        nonlocal _deferred_fill
        per_break  = gap / float(num_breaks)
        capped_per = min(per_break, max_interrupt_break_seconds) if max_interrupt_break_seconds else per_break
        if per_break > capped_per:
            _deferred_fill += (per_break - capped_per) * (num_breaks - 1)
        capped_per = max(capped_per, MIN_BREAK_SECONDS)

        base_seg    = int(show_dur) // num_breaks
        seek_offset = 0
        for i in range(num_breaks):
            is_last = (i == num_breaks - 1)
            seg = (show_dur - seek_offset) if is_last else base_seg
            timeline.append(_show_entry(show, seg, t_block_content, seek_offset,
                                        interior_cut=not is_last))
            t_block_content += seg
            seek_offset     += seg
            if is_last:
                end_budget = next_mark - t_block_content
                if 0 < end_budget < MIN_BREAK_SECONDS:
                    _deferred_fill += end_budget
                else:
                    t_block_content = _pack_and_round(
                        end_budget, t_block_content,
                        max_seconds=max_interrupt_break_seconds)
            else:
                picked, used = _pack_break(capped_per)
                t_block_content = _append_break(picked, t_block_content)
        return t_block_content

    def _ceil_div(a, b):
        """Integer ceiling division, safe on a non-positive divisor."""
        if not b or b <= 0:
            return 1
        return int(-(-a // b))

    def _maybe_flush_deferred_fill(t):
        nonlocal _deferred_fill
        if _deferred_fill <= 0 or not commercial_pool:
            return t
        if t - _last_break_end < MIN_BREAK_GAP:
            return t
        if _half_hour_capped(t):
            return t
        insert_amt = min(MAX_CATCHUP, _deferred_fill)
        if insert_amt < MIN_BREAK_SECONDS:
            return t
        picked, used = _pack_break(insert_amt)
        if picked:
            t = _append_break(picked, t)
            _deferred_fill -= used
        return t

    def _snap_to_mark(t, cap_seconds):
        nonlocal _deferred_fill
        if t % HALF_HOUR == 0:
            return t
        snap_budget = HALF_HOUR - (t % HALF_HOUR)
        if snap_budget < MIN_BREAK_SECONDS:
            snap_budget += HALF_HOUR
        if t - _last_break_end < MIN_BREAK_GAP:
            _deferred_fill += snap_budget
            return t
        if _half_hour_capped(t):
            _deferred_fill += snap_budget
            return t
        return _pack_and_round(snap_budget, t,
                               max_seconds=cap_seconds)

    def _resolved_show_duration(show):
        # WARNING: resolve probe results before using stored durations so sentinel values never enter schedule math.
        if isinstance(show, dict) and show.get("is_pair"):
            p1 = show.get("pair_ep1_path", show.get("path", ""))
            p2 = show.get("pair_ep2_path", "")
            d1 = resolve_track_duration(
                media_probe_cache, p1, show.get("pair_ep1_duration", 0), default=0
            )
            d2 = resolve_track_duration(
                media_probe_cache, p2, show.get("pair_ep2_duration", 0), default=0
            )
            if d1 > 0 and d2 > 0:
                return int(d1 + d2)
        p = show.get("path") if isinstance(show, dict) else None
        stored = show.get("duration") if isinstance(show, dict) else None
        return resolve_track_duration(media_probe_cache, p, stored, default=HALF_HOUR)

    def _mid_split_point(show, show_dur):
        if isinstance(show, dict) and show.get("is_pair"):
            p1 = show.get("pair_ep1_path", show.get("path", ""))
            d1 = resolve_track_duration(
                media_probe_cache, p1, show.get("pair_ep1_duration", 0), default=0
            )
            if 0 < d1 < show_dur:
                raw_mid = show_dur // 2
                if abs(int(d1) - raw_mid) >= 1:
                    log.info(
                        "[BLOCK TIMELINE] paired-show mid-break snapped to ep1 seam: "
                        "%s -- seam=%ds vs raw_midpoint=%ds (combined=%ds)",
                        os.path.basename(p1) if p1 else "?", int(d1), raw_mid, show_dur)
                return int(d1)
        return show_dur // 2

    if commercial_tracks and commercial_placement == "interrupt_half_hour":
        for show in video_tracks:
            show_dur = _resolved_show_duration(show)
            _est = _dur_is_estimated(show)

            total_block_content = _snap_to_mark(total_block_content, max_interrupt_break_seconds)
            total_block_content = _maybe_flush_deferred_fill(total_block_content)
            _register_half_hour(total_block_content)

            natural_end = total_block_content + show_dur
            next_mark = ((natural_end // HALF_HOUR) + (1 if natural_end % HALF_HOUR != 0 else 0)) * HALF_HOUR
            gap = next_mark - natural_end
            if _est and gap <= 0:
                # WARNING: do not turn an estimated placeholder into a long-form program with a full fallback break; the next probe rebuilds it.
                if show_dur == HALF_HOUR:
                    timeline.append(_show_entry(show, show_dur,
                                                 total_block_content, 0))
                    total_block_content += show_dur
                    continue
                next_mark += HALF_HOUR
                gap = next_mark - natural_end

            if 0 < gap < MIN_BREAK_SECONDS:
                next_mark += HALF_HOUR
                gap = next_mark - natural_end

            if gap <= 0:
                _fallback_break_len = max_interrupt_break_seconds if max_interrupt_break_seconds else MIN_BREAK_SECONDS

                if show_dur < interrupt_floor_seconds:
                    timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                    total_block_content += show_dur
                    if _half_hour_capped(total_block_content):
                        _deferred_fill += _fallback_break_len
                    else:
                        total_block_content = _pack_and_round(
                            _fallback_break_len, total_block_content,
                            max_seconds=max_interrupt_break_seconds)

                elif show_dur < HALF_HOUR:
                    mid_point = _mid_split_point(show, show_dur)
                    timeline.append(_show_entry(show, mid_point, total_block_content, 0,
                                                interior_cut=True))
                    total_block_content += mid_point
                    picked, used = _pack_break(_fallback_break_len)
                    total_block_content = _append_break(picked, total_block_content)
                    remaining_half = show_dur - mid_point
                    timeline.append(_show_entry(show, remaining_half, total_block_content, mid_point))
                    total_block_content += remaining_half
                    total_block_content = _pack_and_round(
                        _fallback_break_len, total_block_content,
                        max_seconds=max_interrupt_break_seconds)

                else:
                    num_interior_breaks = max(1, int(show_dur // HALF_HOUR))
                    seek_offset    = 0
                    remaining_show = show_dur
                    for _i in range(num_interior_breaks):
                        seg = HALF_HOUR
                        _is_true_tail = (seek_offset + seg) >= show_dur
                        timeline.append(_show_entry(show, seg, total_block_content, seek_offset,
                                                    interior_cut=not _is_true_tail))
                        total_block_content += seg
                        seek_offset         += seg
                        remaining_show      -= seg
                        picked, used = _pack_break(_fallback_break_len)
                        total_block_content   = _append_break(picked, total_block_content)

                    if remaining_show > 0:
                        timeline.append(_show_entry(show, remaining_show, total_block_content, seek_offset))
                        total_block_content += remaining_show
                        total_block_content = _pack_and_round(
                            _fallback_break_len, total_block_content,
                            max_seconds=max_interrupt_break_seconds)
                continue

            if show_dur < interrupt_floor_seconds:
                timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                total_block_content += show_dur
                end_budget = next_mark - total_block_content
                if _deferred_fill > 0:
                    end_budget += _deferred_fill
                    _deferred_fill = 0
                if end_budget > 0 and _half_hour_capped(total_block_content):
                    _deferred_fill += end_budget
                elif end_budget > 0:
                    total_block_content = _pack_and_round(
                        end_budget, total_block_content,
                        max_seconds=max_interrupt_break_seconds)

            elif show_dur < HALF_HOUR:
                if gap < 2 * MIN_BREAK_SECONDS:
                    timeline.append(_show_entry(show, show_dur, total_block_content, 0))
                    total_block_content += show_dur
                    end_budget = next_mark - total_block_content
                    total_block_content = _pack_and_round(
                        end_budget, total_block_content,
                        max_seconds=max_interrupt_break_seconds)
                else:
                    # WARNING: at most 2 breaks per half-hour slot, each capped at max_interrupt_break_seconds.
                    mid_budget = gap / 2.0
                    if max_interrupt_break_seconds:
                        mid_budget = min(mid_budget, max_interrupt_break_seconds)
                    mid_point  = _mid_split_point(show, show_dur)

                    timeline.append(_show_entry(show, mid_point, total_block_content, 0,
                                                interior_cut=True))
                    total_block_content += mid_point

                    picked, used = _pack_break(mid_budget)
                    total_block_content = _append_break(picked, total_block_content)

                    remaining_half = show_dur - mid_point
                    timeline.append(_show_entry(show, remaining_half, total_block_content, mid_point))
                    total_block_content += remaining_half

                    end_budget = next_mark - total_block_content
                    total_block_content = _pack_and_round(
                        end_budget, total_block_content,
                        max_seconds=max_interrupt_break_seconds)

            else:
                num_interior_breaks = int(show_dur // HALF_HOUR)
                baseline_breaks      = num_interior_breaks + 1
                baseline_per_break   = gap / float(baseline_breaks)

                if max_interrupt_break_seconds and baseline_per_break > max_interrupt_break_seconds:
                    num_breaks = max(baseline_breaks, _ceil_div(gap, max_interrupt_break_seconds))
                    total_block_content = _pack_even_interior_breaks(
                        show, show_dur, gap, num_breaks, total_block_content, next_mark)
                else:
                    capped_per = min(baseline_per_break, max_interrupt_break_seconds) if max_interrupt_break_seconds else baseline_per_break
                    capped_per = max(capped_per, MIN_BREAK_SECONDS)

                    seek_offset    = 0
                    remaining_show = show_dur
                    for i in range(num_interior_breaks):
                        seg = HALF_HOUR
                        _is_true_tail = (seek_offset + seg) >= show_dur
                        timeline.append(_show_entry(show, seg, total_block_content, seek_offset,
                                                    interior_cut=not _is_true_tail))
                        total_block_content += seg
                        seek_offset         += seg
                        remaining_show      -= seg

                        picked, used = _pack_break(capped_per)
                        total_block_content   = _append_break(picked, total_block_content)

                    if remaining_show > 0:
                        timeline.append(_show_entry(show, remaining_show, total_block_content, seek_offset))
                        total_block_content += remaining_show

                    end_budget = next_mark - total_block_content
                    if 0 < end_budget < MIN_BREAK_SECONDS:
                        _deferred_fill += end_budget
                    else:
                        total_block_content = _pack_and_round(
                            end_budget, total_block_content,
                            max_seconds=max_interrupt_break_seconds)

    elif commercial_tracks and commercial_placement == "end_of_show":
        for show in video_tracks:
            total_block_content = _snap_to_mark(total_block_content, max_end_break_seconds)
            total_block_content = _maybe_flush_deferred_fill(total_block_content)

            _dur = _resolved_show_duration(show)
            if _dur_is_estimated(show) and _dur == HALF_HOUR:
                timeline.append(_show_entry(show, _dur, total_block_content, 0))
                total_block_content += _dur
                continue
            timeline.append(_show_entry(show, _dur, total_block_content, 0))
            total_block_content += _dur

            next_mark  = (((total_block_content // HALF_HOUR)
                           + (1 if total_block_content % HALF_HOUR != 0 else 0)) * HALF_HOUR)
            end_budget = next_mark - total_block_content
            if 0 < end_budget < MIN_BREAK_SECONDS:
                next_mark += HALF_HOUR
                end_budget = next_mark - total_block_content
            total_block_content = _pack_and_round(
                end_budget, total_block_content,
                max_seconds=max_end_break_seconds)

    else:
        for show in video_tracks:
            _dur = _resolved_show_duration(show)
            timeline.append(_show_entry(show, _dur, total_block_content, 0))
            total_block_content += _dur

    new_commercial_state = {
        "bundles": {
            str(_ceiling): {"cycle": _bundle_cycle.get(_ceiling, 0),
                             "recent": list(_bundle_recent.get(_ceiling, []))}
            for _ceiling in _bundles
        },
        "turn_ceiling": _bundle_order[_bundle_turn] if _bundle_order else None,
        "last": _last_played_path,
    }
    return timeline, total_block_content, new_commercial_state


# ============================================================================
# SECTION: Duration & dimension probing
# ============================================================================


# VALUES CHANGE HERE
_DURATION_SANITY_MAX_PLAUSIBLE_MBPS = 60


def _duration_looks_implausible(file_path, duration_sec):
    """True if file_size / duration implies an impossible bitrate, so the probed duration is probably truncated or bogus."""
    try:
        if not duration_sec or duration_sec <= 0:
            return False
        size_bytes = os.path.getsize(file_path)
        implied_mbps = (size_bytes * 8 / 1_000_000) / duration_sec
        return implied_mbps > _DURATION_SANITY_MAX_PLAUSIBLE_MBPS
    except Exception:
        return False


def get_media_duration(file_path):
    if not file_path or not _cached_path_exists(file_path):
        return None

    norm_p = os.path.normpath(os.path.abspath(file_path))
    if not _duration_inflight.try_claim(norm_p):
        return None

    try:
        import vlc
        _slot = _get_duration_probe_slot()
        instance, _slot_lock = _slot[0], _slot[1]
        _yield_to_live_playback(f"media_duration_probe({os.path.basename(file_path)})")
        with _slot_lock:
            media = instance.media_new(file_path)
            media.parse_with_options(vlc.MediaParseFlag.local, 5000)
            
            import time
            start_time = time.time()
            
            pending_status = None
            for attr in ['Pending', 'pending']:
                if hasattr(vlc.MediaParsedStatus, attr):
                    pending_status = getattr(vlc.MediaParsedStatus, attr)
                    break
                    
            if pending_status is None:
                complete_flags = []
                for attr in ['Done', 'done', 'Failed', 'failed', 'Timeout', 'timeout', 'Skipped', 'skipped']:
                    if hasattr(vlc.MediaParsedStatus, attr):
                        complete_flags.append(getattr(vlc.MediaParsedStatus, attr))
                
                while media.get_parsed_status() not in complete_flags:
                    if time.time() - start_time > 5:
                        break
                    time.sleep(0.10)
            else:
                while media.get_parsed_status() == pending_status:
                    if time.time() - start_time > 5:
                        break
                    time.sleep(0.10)
            
            duration_ms = media.get_duration()
            _parse_ms = (time.time() - start_time) * 1000.0
            if duration_ms > 0:
                duration_sec = duration_ms / 1000.0
                if _duration_looks_implausible(file_path, duration_sec):
                    _media_warning(
                        "duration_implausible",
                        "[MEDIA DURATION] %s parsed as %.0fs, but that "
                        "implies an unrealistic bitrate for the file's "
                        "size -- treating this as a bad probe rather than "
                        "trusting/caching it, leaving unprobed for retry.",
                        os.path.basename(file_path), duration_sec,
                    )
                    return None
                return duration_sec
            else:
                try:
                    _final_status = media.get_parsed_status()
                    _status_name = next(
                        (n for n in dir(vlc.MediaParsedStatus)
                         if not n.startswith("_") and getattr(vlc.MediaParsedStatus, n) == _final_status),
                        str(_final_status))
                except Exception:
                    _status_name = "unknown"
                _media_warning(
                    "duration_parse",
                    "[MEDIA DURATION] Could not parse duration for %s "
                    "(parsed_status=%s, waited=%.0fms) -- leaving unprobed for retry",
                    os.path.basename(file_path), _status_name, _parse_ms,
                )
                return None
    except Exception as e:
        _media_warning(
            "duration_error",
            "[MEDIA DURATION ERROR] %s -- leaving unprobed for retry",
            e,
        )
        return None
    finally:
        _duration_inflight.release(norm_p)


_header_facts_provider = None


def set_header_facts_provider(fn):
    """Registers the shared header read (one ffmpeg -i per file, stored in the media library) that duration, size and audio checks draw from."""
    global _header_facts_provider
    _header_facts_provider = fn


def _get_header_facts(file_path):
    """{ok, duration, width, height, has_audio} from the shared header read; {"ok": False} when that read failed; None when no shared read is available."""
    fn = _header_facts_provider
    if fn is None:
        return None
    try:
        return fn(file_path)
    except Exception:
        return None


def get_media_video_dimensions(file_path):
    # WARNING: the fallback ffmpeg call below must stay separate from the picture-box scan and the loudness decodes; do not fold them together.
    if not file_path or not _cached_path_exists(file_path):
        return None
    _facts = _get_header_facts(file_path)
    if _facts is not None:
        if _facts.get("ok") and _facts.get("width") and _facts.get("height"):
            return int(_facts["width"]), int(_facts["height"])
        return None

    norm_p = os.path.normpath(os.path.abspath(file_path))
    if not _dims_inflight.try_claim(norm_p):
        return None

    try:
        import subprocess, re

        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )

        with _fast_disk_io_slot(f"ffprobe_duration({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", result.stderr)
        if not match:
            audio_stream = re.search(
                r"(?im)^\s*Stream #\d+:\d+.*\bAudio:",
                result.stderr,
            )
            if audio_stream:
                return None

            _media_warning(
                "dimensions_no_video",
                "[MEDIA DIMENSIONS] No video stream found for %s -- leaving unprobed for retry",
                os.path.basename(file_path),
            )
            return None

        w, h = int(match.group(1)), int(match.group(2))
        if w <= 0 or h <= 0:
            _media_warning(
                "dimensions_invalid",
                "[MEDIA DIMENSIONS] Invalid dimensions parsed for %s -- leaving unprobed for retry",
                os.path.basename(file_path),
            )
            return None

        _tail = result.stderr[match.end():match.end() + 200]
        dar_match = re.search(r"DAR\s+(\d+):(\d+)", _tail)
        if dar_match:
            dar_num, dar_den = int(dar_match.group(1)), int(dar_match.group(2))
            if dar_num > 0 and dar_den > 0:
                raw_ratio = w / float(h)
                dar_ratio = dar_num / float(dar_den)
                if abs(raw_ratio - dar_ratio) > 0.02:
                    corrected_w = int(round(h * dar_ratio))
                    corrected_w = max(2, corrected_w - (corrected_w % 2))
                    return (corrected_w, h)

        return (w, h)
    except subprocess.TimeoutExpired:
        _media_warning(
            "dimensions_timeout",
            "[MEDIA DIMENSIONS] Probe timed out for %s -- leaving unprobed for retry",
            os.path.basename(file_path),
        )
        return None
    except Exception as e:
        _media_warning(
            "dimensions_error",
            "[MEDIA DIMENSIONS ERROR] %s -- leaving unprobed for retry",
            e,
        )
        return None
    finally:
        _dims_inflight.release(norm_p)


# ============================================================================
# SECTION: FFmpeg & main-directory resolution
# ============================================================================


_ffmpeg_exe_path = None
_ffmpeg_resolved = False


def _resolve_ffmpeg_path():
    global _ffmpeg_exe_path, _ffmpeg_resolved
    if _ffmpeg_resolved:
        return _ffmpeg_exe_path
    _ffmpeg_resolved = True

    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for base_dir in (meipass, exe_dir, src_dir):
        if not base_dir:
            continue
        candidate = os.path.join(base_dir, exe_name)
        if os.path.exists(candidate):
            _ffmpeg_exe_path = candidate
            log.info("[FFMPEG] Using bundled binary: %s", candidate)
            return _ffmpeg_exe_path
        candidate = os.path.join(base_dir, "ffmpeg", exe_name)
        if os.path.exists(candidate):
            _ffmpeg_exe_path = candidate
            log.info("[FFMPEG] Using bundled binary: %s", candidate)
            return _ffmpeg_exe_path

    found_on_path = shutil.which(exe_name)
    if found_on_path:
        log.info("[FFMPEG] No bundled binary found; using PATH copy: %s", found_on_path)
        _ffmpeg_exe_path = found_on_path
        return _ffmpeg_exe_path

    log.warning("[FFMPEG] No bundled binary and none found on PATH -- "
                "loudness normalization will stay disabled (gain=1.0) until "
                "one is available.")
    _ffmpeg_exe_path = exe_name
    return _ffmpeg_exe_path


_resolved_main_dir = None


def resolve_main_dir():
    global _resolved_main_dir
    if _resolved_main_dir is not None:
        return _resolved_main_dir

    if getattr(sys, "frozen", False):
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "main"))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "main"))
        for candidate in candidates:
            if os.path.isdir(candidate):
                _resolved_main_dir = candidate
                log.info("[MAIN DIR] Found existing 'main' folder at: %s", candidate)
                return _resolved_main_dir
        _resolved_main_dir = candidates[0]
        log.warning("[MAIN DIR] No existing 'main' folder found in any candidate "
                    "location; defaulting to %s", _resolved_main_dir)
        return _resolved_main_dir

    _resolved_main_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main")
    return _resolved_main_dir


# ============================================================================
# SECTION: Bezel discovery
# ============================================================================


# WARNING: read by ui.py's _aa_rounded_rect.
CRT_WARP_ACTIVE = False

_bezel_style_files_cache = {"key": None, "files": [], "checked_at": 0.0}
_BEZEL_STYLE_DISCOVERY_TTL_SEC = 2.0

_bezel_dir_cache = {"dir": None, "checked_at": 0.0}
_BEZEL_DIR_RECHECK_TTL_SEC = _BEZEL_STYLE_DISCOVERY_TTL_SEC


def resolve_bezel_dir():
    now = time.monotonic()
    cached = _bezel_dir_cache
    if cached["dir"] is not None and (now - cached["checked_at"]) < _BEZEL_DIR_RECHECK_TTL_SEC:
        return cached["dir"]
    d = os.path.join(resolve_main_dir(), "images", "bezel")
    if not os.path.isdir(d):
        d = os.path.join(resolve_main_dir(), "images", "border")
    cached["dir"] = d
    cached["checked_at"] = now
    return d


def discover_bezel_style_files():
    """Bezel images available for the Channels-menu Bezel style selector, in style order."""
    import time as _time_bezel
    b_dir = resolve_bezel_dir()
    cached = _bezel_style_files_cache
    now = _time_bezel.monotonic()
    if (cached["key"] is not None
            and cached["key"][0] == b_dir
            and (now - cached["checked_at"]) < _BEZEL_STYLE_DISCOVERY_TTL_SEC):
        return cached["files"]

    try:
        _dir_fingerprint = (os.path.getmtime(b_dir), len(os.listdir(b_dir))) if os.path.isdir(b_dir) else None
    except Exception:
        _dir_fingerprint = None
    _cache_key = (b_dir, _dir_fingerprint)
    cached["checked_at"] = now
    if cached["key"] == _cache_key:
        return cached["files"]

    files = []
    if os.path.isdir(b_dir):
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
        unnumbered_path = None
        numbered = {}
        try:
            for f in os.listdir(b_dir):
                stem, ext = os.path.splitext(f)
                if ext.lower() not in exts:
                    continue
                if stem.lower() in ("bezel", "border") and unnumbered_path is None:
                    unnumbered_path = os.path.join(b_dir, f)
                    continue
                m = re.match(r"^(?:bezel|border)(\d+)$", stem, re.IGNORECASE)
                if m:
                    numbered[int(m.group(1))] = os.path.join(b_dir, f)
        except Exception as e:
            log.warning("discover_bezel_style_files: could not list %s: %s", b_dir, e)

        ordered_numbered = [numbered[n] for n in sorted(numbered.keys())]
        if unnumbered_path is not None:
            files = [unnumbered_path] + ordered_numbered
        else:
            files = ordered_numbered

    cached["key"] = _cache_key
    cached["files"] = files
    return files


def bezel_style_count():
    """How many selectable bezel styles are available."""
    return max(1, len(discover_bezel_style_files()))


# ============================================================================
# SECTION: Data-folder resolution
# ============================================================================


_loudness_dir_ready = False


def resolve_loudness_dir():
    """Return the program's own folder for loudness work files."""
    global _loudness_dir_ready
    path = os.path.join(resolve_main_dir(), "loudness")
    if not _loudness_dir_ready:
        try:
            os.makedirs(path, exist_ok=True)
            _loudness_dir_ready = True
        except OSError as e:
            log.warning("[LOUDNESS DIR] could not create %s: %s", path, e)
    return path


_captions_dir_ready = False


def resolve_captions_dir():
    """Return the program's own folder for generated/extracted caption files (.srt)."""
    global _captions_dir_ready
    path = os.path.join(resolve_main_dir(), "captions")
    if not _captions_dir_ready:
        try:
            os.makedirs(path, exist_ok=True)
            _captions_dir_ready = True
        except OSError as e:
            log.warning("[CAPTIONS DIR] could not create %s: %s", path, e)
    return path


# ============================================================================
# SECTION: Caption sources & cue parsing
# ============================================================================


CAPTION_SIDECAR_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub", ".smi")

_TEXT_SUBTITLE_CODECS = ("subrip", "ass", "ssa", "webvtt", "mov_text", "text")
_BITMAP_SUBTITLE_CODECS = ("dvd_subtitle", "hdmv_pgs_subtitle", "xsub", "dvb_subtitle")


# ============================================================================
# SECTION: Caption start-up helpers
# ============================================================================
# VALUES CHANGE HERE
_HEADER_PROBE_SHARE_SECONDS = 120.0
_HEADER_PROBE_SHARE_MAX_ENTRIES = 64
_header_probe_share = {}
_header_probe_share_lock = threading.Lock()


def _playing_file_scope(active):
    """Priority scope for the playing file's caption start-up; a no-op for background callers."""
    if active and _perf_governor is not None:
        return _perf_governor.priority_scope()
    return contextlib.nullcontext()


def _shared_header_probe(file_path, timeout):
    """combined_header_probe(), reusing a successful result for the same unchanged file for a short time."""
    import media_library as _ml
    try:
        st = os.stat(file_path)
        key = (os.path.normpath(file_path).lower(), st.st_mtime, st.st_size)
    except OSError:
        return _ml.combined_header_probe(file_path, timeout=timeout)
    now = time.monotonic()
    with _header_probe_share_lock:
        hit = _header_probe_share.get(key)
        if hit is not None and now - hit[0] <= _HEADER_PROBE_SHARE_SECONDS:
            return hit[1]
    result = _ml.combined_header_probe(file_path, timeout=timeout)
    if result.ok:
        with _header_probe_share_lock:
            _header_probe_share[key] = (time.monotonic(), result)
            if len(_header_probe_share) > _HEADER_PROBE_SHARE_MAX_ENTRIES:
                for stale in sorted(_header_probe_share, key=lambda k: _header_probe_share[k][0])[
                        :len(_header_probe_share) - _HEADER_PROBE_SHARE_MAX_ENTRIES]:
                    _header_probe_share.pop(stale, None)
    return result


_subtitle_probe_import_warned = False


def _probe_embedded_subtitle_streams(file_path):
    """List the subtitle streams in a video file's container (header read only)."""
    if not file_path or not _cached_path_exists(file_path):
        return []
    try:
        import media_library as _ml
    except Exception as e:
        global _subtitle_probe_import_warned
        if not _subtitle_probe_import_warned:
            _subtitle_probe_import_warned = True
            log.warning("_probe_embedded_subtitle_streams: media_library import failed (%s) -- "
                        "embedded subtitle detection is unavailable (logged once)", e)
        return []
    result = _shared_header_probe(file_path, 15)
    if not result.ok:
        if result.error not in ("path_missing",):
            log.warning("_probe_embedded_subtitle_streams: failed for %s: %s",
                        file_path, result.error)
        return []
    return result.subtitle_streams


_ENGLISH_LABELS = {"en", "eng", "english", "en-us", "en-gb", "en_us", "en_gb"}
_IGNORED_SIDECAR_LABELS = {"forced", "commentary", "signs", "songs"}
_SIDECAR_LABEL_NOISE = {"sdh", "cc", "hi", "default", "captions", "caption", "subs", "sub"}
_BAKED_NAME_RE = re.compile(
    r"(?<![a-z0-9])(hard[\W_]*sub(?:s|bed)?|hard[\W_]*coded|burn(?:ed|t)?[\W_]*in|open[\W_]*captions?)(?![a-z0-9])",
    re.I)


def _sidecar_rank(stem_lower, candidate_name_lower):
    """Ranks a directory entry as a caption sidecar of the video named stem_lower; None when it is not one, else a sortable tuple where lower is better."""
    root, ext = os.path.splitext(candidate_name_lower)
    if ext not in CAPTION_SIDECAR_EXTS:
        return None
    if root == stem_lower:
        return (0, CAPTION_SIDECAR_EXTS.index(ext), "")
    if not root.startswith(stem_lower + "."):
        return None
    labels = [t for t in root[len(stem_lower) + 1:].split(".") if t]
    if not labels or any(t in _IGNORED_SIDECAR_LABELS for t in labels):
        return None
    language = [t for t in labels if t not in _SIDECAR_LABEL_NOISE]
    if len(language) > 1:
        return None
    if not language or language[0] in _ENGLISH_LABELS:
        return (1, CAPTION_SIDECAR_EXTS.index(ext), "")
    if re.fullmatch(r"[a-z]{2,3}(?:[-_][a-z0-9]{2,4})?", language[0]):
        return (2, CAPTION_SIDECAR_EXTS.index(ext), language[0])
    return None


def _find_sidecar_caption_file(file_path):
    """Look next to `file_path` for its caption file: same name plus .srt/.vtt/etc., or with a language label (movie.en.srt)."""
    if not file_path:
        return None
    folder, name = os.path.split(file_path)
    stem_lower = os.path.splitext(name)[0].lower()
    base, _ = os.path.splitext(file_path)
    for ext in CAPTION_SIDECAR_EXTS:
        candidate = base + ext
        if _cached_path_exists(candidate):
            return candidate
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return None
    best = None
    for entry in names:
        rank = _sidecar_rank(stem_lower, entry.lower())
        if rank is None:
            continue
        if best is None or rank < best[0]:
            best = (rank, entry)
    if best is None:
        return None
    return os.path.join(folder, best[1])


def filename_says_captions_baked_in(file_path):
    """True when the file's own name says the subtitles are part of the picture (hardsub, hardcoded, burned-in, open captions)."""
    return bool(file_path) and bool(_BAKED_NAME_RE.search(os.path.basename(file_path)))


def _extract_embedded_subtitle_to_file(file_path, stream_index, out_path, timeout=30):
    """Remux one text-based embedded subtitle stream to a standalone .srt with no re-encode."""
    try:
        import subprocess
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"extract_subs({os.path.basename(file_path)})"):
            subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-y", "-i", file_path,
                 "-map", f"0:{stream_index}", "-c:s", "srt", out_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        log.warning("_extract_embedded_subtitle_to_file: failed for %s: %s", file_path, e)
        return False


def _burned_in_verdict(file_path, allow_scan=True):
    """Stored burned-in verdict (True/False) for the file, scanning it once if allowed; None when unknown."""
    try:
        import media_library as _ml
        lib = _ml.shared_probe_library()
        verdict = lib.get_burned_in_verdict(file_path)
        if verdict is None and allow_scan:
            verdict = lib.detect_burned_in_if_needed(file_path)
        return verdict
    except Exception as e:
        log.warning("_burned_in_verdict: failed for %s: %s", file_path, e)
        return None


def resolve_caption_source(file_path, cache_entry=None):
    """Looks up the caption source of a file ahead of ASR and returns a dict whose "kind" says where the captions come from, or None when live transcription may take over."""
    result = {"kind": None, "srt_path": None, "stream_index": None,
              "mtime": None, "size": None}
    if not file_path or not _cached_path_exists(file_path):
        return result
    try:
        st = os.stat(file_path)
        result["mtime"], result["size"] = st.st_mtime, st.st_size
    except OSError:
        return result

    if filename_says_captions_baked_in(file_path):
        result["kind"] = "baked"
        return result

    if (cache_entry and cache_entry.get("mtime") == result["mtime"]
            and cache_entry.get("size") == result["size"]):
        if cache_entry.get("source") == "baked":
            result["kind"] = "baked"
            return result
        if cache_entry.get("srt_path") and _cached_path_exists(cache_entry["srt_path"]):
            cached_is_sidecar = cache_entry.get("source") == "sidecar"
            newer_sidecar = None if cached_is_sidecar else _find_sidecar_caption_file(file_path)
            if not newer_sidecar:
                result["kind"] = "cache"
                result["srt_path"] = cache_entry["srt_path"]
                return result

    sidecar = _find_sidecar_caption_file(file_path)
    if sidecar:
        result["kind"] = "sidecar"
        result["srt_path"] = sidecar
        return result

    for stream in _probe_embedded_subtitle_streams(file_path):
        if stream["is_text"]:
            result["kind"] = "embedded"
            result["stream_index"] = stream["index"]
            return result

    if _burned_in_verdict(file_path):
        result["kind"] = "baked"
        return result

    return result


def _convert_sidecar_to_srt(sidecar_path, timeout=30):
    """Convert an .ass/.ssa/.smi/.sub text sidecar to a cached .srt with ffmpeg; None when it cannot be converted (for example a bitmap VobSub .sub)."""
    try:
        import subprocess, hashlib
        st = os.stat(sidecar_path)
        digest = hashlib.sha1(f"{os.path.normpath(sidecar_path).lower()}|{st.st_size}|{st.st_mtime}".encode("utf-8", "replace")).hexdigest()[:16]
        out_path = os.path.join(resolve_captions_dir(), f"sidecar_{digest}.srt")
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        subprocess.run(
            [_resolve_ffmpeg_path(), "-nostdin", "-y", "-i", sidecar_path, "-c:s", "srt", out_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        log.warning("_convert_sidecar_to_srt: failed for %s: %s", sidecar_path, e)
    return None


_CUE_TAG_RE = None


def parse_caption_cues(srt_or_vtt_path):
    """Parse a .srt or .vtt file into a flat, time-sorted cue list: [(start_ms, end_ms, text), ...]."""
    import re
    global _CUE_TAG_RE
    if _CUE_TAG_RE is None:
        _CUE_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")

    if not srt_or_vtt_path or not _cached_path_exists(srt_or_vtt_path):
        return []
    if os.path.splitext(srt_or_vtt_path)[1].lower() not in (".srt", ".vtt"):
        srt_or_vtt_path = _convert_sidecar_to_srt(srt_or_vtt_path)
        if not srt_or_vtt_path:
            return []
    try:
        with open(srt_or_vtt_path, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        log.warning("parse_caption_cues: could not read %s: %s", srt_or_vtt_path, e)
        return []

    time_re = re.compile(
        r"(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})"
    )

    def _to_ms(h, m, s, ms):
        h = int(h.rstrip(":")) if h else 0
        return ((h * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)

    cues = []
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        time_line_idx = None
        for i, ln in enumerate(lines):
            if time_re.search(ln):
                time_line_idx = i
                break
        if time_line_idx is None:
            continue
        m = time_re.search(lines[time_line_idx])
        start_ms = _to_ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end_ms   = _to_ms(m.group(5), m.group(6), m.group(7), m.group(8))
        text_lines = lines[time_line_idx + 1:]
        text = " ".join(_CUE_TAG_RE.sub("", ln).strip() for ln in text_lines).strip()
        text = (text.replace("&amp;", "&").replace("&lt;", "<")
                    .replace("&gt;", ">").replace("&nbsp;", " "))
        if text and end_ms > start_ms:
            cues.append((start_ms, end_ms, text))

    cues.sort(key=lambda c: c[0])
    return cues


# ============================================================================
# SECTION: ASR model & settings
# ============================================================================


_asr_model_dir_ready = False


def _whisper_dir_has_model(path):
    """True when `path` holds a faster-whisper model (a Hugging Face cache folder or a plain model.bin)."""
    try:
        if not os.path.isdir(path):
            return False
        if os.path.isfile(os.path.join(path, "model.bin")):
            return True
        for entry in os.listdir(path):
            if entry.startswith("models--") and os.path.isdir(os.path.join(path, entry)):
                return True
            if os.path.isfile(os.path.join(path, entry, "model.bin")):
                return True
    except OSError:
        pass
    return False


def resolve_whisper_model_dir():
    """Return the folder holding the faster-whisper model weights; in a packaged build it looks beside the .exe first, because the bundle's temp folder is thrown away on exit."""
    global _asr_model_dir_ready, _asr_model_dir_choice
    if _asr_model_dir_choice is not None:
        return _asr_model_dir_choice
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        meipass = getattr(sys, "_MEIPASS", None)
        candidates.append(os.path.join(exe_dir, "models", "whisper"))
        candidates.append(os.path.join(exe_dir, "main", "models", "whisper"))
        candidates.append(os.path.join(exe_dir, "_internal", "models", "whisper"))
        if meipass:
            candidates.append(os.path.join(meipass, "models", "whisper"))
            candidates.append(os.path.join(meipass, "main", "models", "whisper"))
    candidates.append(os.path.join(resolve_main_dir(), "models", "whisper"))
    chosen = next((c for c in candidates if _whisper_dir_has_model(c)), None)
    if chosen is None:
        chosen = candidates[0]
        log.warning("[ASR MODEL DIR] no Whisper model found in any of: %s -- captions need the '%s' model in one of these folders (or a one-time download into %s)",
                    "; ".join(candidates), _ASR_MODEL_SIZE, chosen)
    else:
        log.info("[ASR MODEL DIR] using Whisper model folder %s", chosen)
    try:
        os.makedirs(chosen, exist_ok=True)
        _asr_model_dir_ready = True
    except OSError as e:
        log.warning("[ASR MODEL DIR] could not create %s: %s", chosen, e)
    _asr_model_dir_choice = chosen
    return chosen


_asr_model_dir_choice = None


_ASR_SEMAPHORE = threading.BoundedSemaphore(1)

_ASR_MODEL_SIZE = "small.en"
_ASR_LANGUAGE = "en"
_ASR_BEAM_SIZE = 5
_ASR_VAD_PARAMETERS = {"min_silence_duration_ms": 500}
# VALUES CHANGE HERE (fallback only; used when performance_governor is missing or fails)
_ASR_CPU_THREADS = max(4, min(8, (os.cpu_count() or 8) // 2))
try:
    import performance_governor as _perf_governor
except Exception:
    _perf_governor = None
_whisper_model = None
_whisper_model_load_failed = False
_whisper_model_lock = threading.Lock()


# WARNING: the thread count is fixed when the model loads; a performance-mode change applies on the next launch.
def _asr_thread_count():
    if _perf_governor is not None:
        try:
            return max(1, int(_perf_governor.whisper_threads(_perf_governor.get_profile())))
        except Exception:
            pass
    return _ASR_CPU_THREADS


@contextlib.contextmanager
def _asr_slot(label="", hold_sec=0.0, skip_if_held=False):
    """Context manager like `with _ASR_SEMAPHORE:` that yields to live playback, then asks the governor's whisper lane to admit the job; yields True when the job may run, False when skip_if_held is set and the hold expired."""
    _yield_to_live_playback(label)
    _hold = max(0.0, float(hold_sec or 0.0))
    with _gov_gate("whisper_slots", priority=(_hold <= 0.0), timeout=(_hold if _hold > 0.0 else None),
                   label=label) as _admitted:
        if _admitted:
            with _ASR_SEMAPHORE:
                yield True
            return
        if skip_if_held:
            yield False
            return
        with _gov_gate("whisper_slots", priority=True, label=label):
            with _ASR_SEMAPHORE:
                yield True


def _whisper_gate_hold_seconds(lead, expected_chunk_sec):
    """How long the governor may hold the next live chunk back while the caption lead stays safe; 0 when the lead is too small to wait."""
    if _perf_governor is None:
        return 0.0
    try:
        spare = lead - expected_chunk_sec - _perf_governor.WHISPER_GATE_KEEP_LEAD_SECONDS
        return max(0.0, min(spare, _perf_governor.WHISPER_GATE_MAX_HOLD_SECONDS))
    except Exception:
        return 0.0


_caption_memory_refusal_handler = None


def set_caption_memory_refusal_handler(handler):
    """Registers handler(total_gb, available_gb, needed_gb), called when the caption model is refused for lack of free memory."""
    global _caption_memory_refusal_handler
    _caption_memory_refusal_handler = handler


def _caption_memory_allows_load():
    """True when there is enough free memory to load the caption model; otherwise logs it, tells the registered handler and returns False."""
    # WARNING: loading the model with too little free memory kills the whole program with no crash log; this check must stay in front of every load.
    if _perf_governor is None:
        return True
    try:
        ok, total_gb, avail_gb, needed_gb = _perf_governor.caption_memory_check()
    except Exception as e:
        log.warning("[ASR MEMORY] memory check failed, allowing the load: %s", e)
        return True
    log.info("[ASR MEMORY] total=%.1fGB available=%.1fGB needed=%.1fGB -> %s",
             total_gb, avail_gb, needed_gb, "ok" if ok else "REFUSED")
    if ok:
        return True
    handler = _caption_memory_refusal_handler
    if handler is not None:
        try:
            handler(total_gb, avail_gb, needed_gb)
        except Exception as e:
            log.warning("[ASR MEMORY] refusal handler failed: %s", e)
    return False


def _get_whisper_model():
    """Lazily load the faster-whisper model once and reuse it; returns None (one warning logged) when it isn't installed."""
    global _whisper_model, _whisper_model_load_failed
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_model_load_failed:
        return None
    with _whisper_model_lock:
        if _whisper_model is not None or _whisper_model_load_failed:
            return _whisper_model
        if not _caption_memory_allows_load():
            return None
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            _whisper_model_load_failed = True
            log.warning(
                "[ASR] faster-whisper is not installed -- auto-captioning is "
                "disabled (embedded/sidecar captions still work normally). "
                "Install with: pip install faster-whisper"
            )
            return None
        _threads = _asr_thread_count()
        try:
            try:
                # WARNING: keep offline mode on or faster-whisper contacts huggingface.co on every launch and delays the first tune-in.
                _whisper_model = WhisperModel(
                    _ASR_MODEL_SIZE, device="cpu", compute_type="int8",
                    cpu_threads=_threads,
                    download_root=resolve_whisper_model_dir(),
                    local_files_only=True,
                )
            except Exception:
                _whisper_model = WhisperModel(
                    _ASR_MODEL_SIZE, device="cpu", compute_type="int8",
                    cpu_threads=_threads,
                    download_root=resolve_whisper_model_dir(),
                )
            log.info("[ASR] faster-whisper '%s' model ready (CPU, int8, %d threads).",
                     _ASR_MODEL_SIZE, _threads)
        except Exception as e:
            _whisper_model_load_failed = True
            log.warning("[ASR] failed to load whisper model: %s", e)
            return None
        try:
            import numpy as _np
            _silence = _np.zeros(16000, dtype="float32")
            _segs, _ = _whisper_model.transcribe(
                _silence, language=_ASR_LANGUAGE, beam_size=1)
            list(_segs)
        except Exception as e:
            log.warning("[ASR] model warm-up skipped: %s", e)
    return _whisper_model


def _live_asr_running():
    """True while any live caption session has been started and not stopped."""
    for sess in list(_LIVE_RUNNING_SESSIONS):
        if not sess._stopped():
            return True
    return False


_whisper_unload_thread = None
_whisper_unload_lock = threading.Lock()


def release_whisper_model(reason):
    """Frees the Whisper model once no live caption session is running and no transcription holds the slot; returns True when an unload was scheduled."""
    global _whisper_unload_thread
    if _whisper_model is None:
        return False
    with _whisper_unload_lock:
        if _whisper_unload_thread is not None and _whisper_unload_thread.is_alive():
            return True
        wait_s = 20.0
        if _perf_governor is not None:
            wait_s = float(getattr(_perf_governor, "WHISPER_UNLOAD_WAIT_SECONDS", wait_s))

        def _job():
            global _whisper_model
            if not _ASR_SEMAPHORE.acquire(timeout=wait_s):
                log.info("[ASR] model unload skipped (%s): a transcription still holds the slot", reason)
                return
            try:
                if _live_asr_running():
                    return
                with _whisper_model_lock:
                    if _whisper_model is None:
                        return
                    _whisper_model = None
                import gc as _gc
                _gc.collect()
                log.info("[ASR] whisper model unloaded (%s)", reason)
            finally:
                _ASR_SEMAPHORE.release()

        _whisper_unload_thread = threading.Thread(target=_job, daemon=True, name="whisper-unload")
        _whisper_unload_thread.start()
    return True


# ============================================================================
# SECTION: Speech-to-text core
# ============================================================================
# WARNING: shared by the DVD player's disk-cached captions and the TV overlay's live captions; a change here affects both.
_asr_vad_utils_load_failed = False

_ASR_VAD_THRESHOLD = 0.25
# VALUES CHANGE HERE
_ASR_VAD_MIN_SPEECH_MS = 150
# VALUES CHANGE HERE
_ASR_MERGE_GAP_SEC = 1.5
_ASR_LIVE_AUDIO_FILTER = "dynaudnorm=f=150:g=15:p=0.9:m=20"
_ASR_MAX_CLIP_SEC = 25.0

_ASR_CUE_MAX_SEC = 5.0
# VALUES CHANGE HERE
_ASR_CUE_MAX_CHARS = 84
_ASR_CUE_GAP_BREAK_SEC = 0.8
_ASR_CUE_MIN_SHOW_SEC = 1.2
_ASR_CUE_TRAIL_HOLD_SEC = 0.3
_ASR_SENTENCE_END = (".", "?", "!", "\u2026")

# VALUES CHANGE HERE (hallucination guard)
_ASR_NO_SPEECH_PROB_MAX = 0.75
_ASR_AVG_LOGPROB_MIN = -1.5

# VALUES CHANGE HERE (prompt-echo guard)
_ASR_ECHO_MAX_WORD_PROB = 0.5
_ASR_ECHO_REPEAT_MIN = 3
_ASR_ECHO_REPEAT_MAX_WORD_PROB = 0.75
_ASR_STOCK_PHRASE_RE = re.compile(
    r"^(thanks for watching|thank you for watching|please subscribe( to (my|the) channel)?|"
    r"subtitles by .*|captions by .*|amara\.org.*|www\..*)[.! ]*$", re.I)

# VALUES CHANGE HERE (gap recovery)
_ASR_RECOVER_ENABLED = True
_ASR_RECOVER_MIN_GAP_SEC = 0.9
_ASR_RECOVER_PAD_SEC = 0.3
_ASR_RECOVER_MAX_WINDOWS = 2
_ASR_RECOVER_JOIN_SEC = 3.0
_ASR_RECOVER_MAX_WINDOW_SEC = 14.0
_ASR_RECOVER_BEAM = 3
_ASR_RECOVER_MAX_SEC = 6.0
_ASR_RECOVER_MAX_FRAC_OF_CHUNK = 0.5
_ASR_RECOVER_KEEP_LEAD_SEC = 8.0
_ASR_RECOVER_MIN_WORD_PROB = 0.55
_ASR_RECOVER_MIN_AVG_LOGPROB = -0.95
_ASR_RECOVER_MAX_NO_SPEECH = 0.5
_ASR_RECOVER_MAX_WORDS_PER_SEC = 5.0
_ASR_RECOVER_MAX_OVERLAP_FRAC = 0.3
_ASR_RUNAWAY_CHAR_RUN = 12
_ASR_RUNAWAY_WORD_RUN = 6

# VALUES CHANGE HERE (repetition-loop guard)
_ASR_LOOP_MIN_CHARS = 24
_ASR_LOOP_COMPRESSION_MAX = 2.4

# VALUES CHANGE HERE (music marking)
_ASR_MUSIC_CAPTION_TEXT = "\u266a"
_ASR_MUSIC_MIN_SEC = 2.0
_ASR_MUSIC_BRIDGE_SEC = 1.0
_ASR_MUSIC_PIECE_SEC = 6.0
_ASR_MUSIC_MIN_RMS_DB = -38.0
_ASR_MUSIC_PEAK_PERSIST_MIN = 0.60
_ASR_MUSIC_ENVELOPE_CV_MAX = 0.35
_ASR_MUSIC_REPLACE_FRAC = 0.7
_ASR_MUSIC_REPLACE_MAX_WORD_PROB = 0.5

# VALUES CHANGE HERE (name hint)
_ASR_NAME_MIN_WORD_PROB = 0.85
_ASR_NAME_MIN_CUES = 2
_ASR_NAME_MIN_LEN = 3
_ASR_NAME_MAX_LEARNED = 6
_ASR_NAME_SIMILAR_MIN = 0.75

# VALUES CHANGE HERE (chunk-boundary carry-over)
_ASR_CARRY_COVER_FRAC = 0.4
_ASR_CARRY_NEAR_SEC = 1.5


def _asr_words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _asr_prompt_vocab(prompt):
    return frozenset(_asr_words(prompt))


def _asr_max_sentence_repeat(text):
    """Longest run of identical consecutive sentences ("Prisoners of Love. Prisoners of Love. ...")."""
    sents = [" ".join(_asr_words(x)) for x in re.split(r"(?<=[.!?])\s+", (text or "").strip())]
    sents = [x for x in sents if x]
    best = run = 0
    prev = None
    for x in sents:
        run = run + 1 if x == prev else 1
        prev = x
        best = max(best, run)
    return best


def _asr_longest_word_run(words):
    best = run = 0
    prev = None
    for w in words:
        run = run + 1 if w == prev else 1
        prev = w
        best = max(best, run)
    return best


def _asr_loop_reason(text):
    """Why a segment's own text is a decoder loop (a few characters repeated over and over), or None."""
    t = (text or "").strip()
    if len(t) < _ASR_LOOP_MIN_CHARS:
        return None
    raw = t.encode("utf-8", "ignore")
    ratio = len(raw) / float(max(1, len(zlib.compress(raw))))
    if ratio >= _ASR_LOOP_COMPRESSION_MAX:
        return "repetition loop: the segment's own text compresses %.1fx (limit %.1fx)" % (
            ratio, _ASR_LOOP_COMPRESSION_MAX)
    return None


def _asr_cue_reject_reason(text, word_prob, vocab):
    """Why a cue is a hallucination (stock phrase or an echo of the title hint), or None to keep it."""
    words = _asr_words(text)
    if not words:
        return None
    if _ASR_STOCK_PHRASE_RE.match((text or "").strip()):
        return "stock phrase Whisper invents over silence/noise"
    if vocab and word_prob is not None and all(w in vocab for w in words):
        if word_prob < _ASR_ECHO_MAX_WORD_PROB:
            return "title echo: only title words, word confidence %.2f < %.2f" % (
                word_prob, _ASR_ECHO_MAX_WORD_PROB)
        if (_asr_max_sentence_repeat(text) >= _ASR_ECHO_REPEAT_MIN
                and word_prob < _ASR_ECHO_REPEAT_MAX_WORD_PROB):
            return "repeated title echo: only title words said %d+ times, word confidence %.2f < %.2f" % (
                _ASR_ECHO_REPEAT_MIN, word_prob, _ASR_ECHO_REPEAT_MAX_WORD_PROB)
    return None


def _asr_recovery_reject_reason(text, word_prob, avg_logprob, no_speech_prob, gap_sec, vocab):
    """The strict gate for text found by gap recovery; None means accept."""
    base = _asr_cue_reject_reason(text, word_prob, vocab)
    if base:
        return base
    words = _asr_words(text)
    if not words:
        return "no words"
    if word_prob is None or word_prob < _ASR_RECOVER_MIN_WORD_PROB:
        return "word confidence %s < %.2f" % (
            "n/a" if word_prob is None else "%.2f" % word_prob, _ASR_RECOVER_MIN_WORD_PROB)
    if avg_logprob is not None and avg_logprob < _ASR_RECOVER_MIN_AVG_LOGPROB:
        return "avg_logprob %.2f < %.2f" % (avg_logprob, _ASR_RECOVER_MIN_AVG_LOGPROB)
    if no_speech_prob is not None and no_speech_prob > _ASR_RECOVER_MAX_NO_SPEECH:
        return "no_speech %.2f > %.2f" % (no_speech_prob, _ASR_RECOVER_MAX_NO_SPEECH)
    if (re.search(r"(.)\1{%d,}" % (_ASR_RUNAWAY_CHAR_RUN - 1), text or "")
            or _asr_longest_word_run(words) >= _ASR_RUNAWAY_WORD_RUN):
        return "runaway repetition"
    if len(words) > gap_sec * _ASR_RECOVER_MAX_WORDS_PER_SEC + 1:
        return "%d words cannot fit in %.1fs" % (len(words), gap_sec)
    return None


_ASR_PROMPT_TAG_RE = re.compile(
    r"\bs\d{1,2}\s*e\d{1,3}(?:\s*[-\u2013]?\s*e?\d{1,3})*\b"
    r"|\b\d{1,2}x\d{1,3}(?:\s*[-\u2013]\s*\d{1,3})*\b"
    r"|\bseason\s*\d+\b|\bepisode\s*\d+\b|\bepisodes?\s*\d+\b",
    re.I)
_ASR_PROMPT_JUNK_RE = re.compile(
    r"^(s\d{1,2}\s*e\d{1,3}|e\d{1,3}|\d{1,4}|\d{1,2}x\d{1,3}|season\s*\d+|episode\s*\d+)$",
    re.I)


def _asr_prompt_for(file_path):
    """Short vocabulary hint for Whisper built from the file's own names."""
    try:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        stem = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stem)
        stem = re.sub(r"[._]+", " ", stem)
        stem = _ASR_PROMPT_TAG_RE.sub(" ", stem)
        parts = [p.strip() for p in stem.split(" - ")]
        parts = [p for p in parts if p and not _ASR_PROMPT_JUNK_RE.match(p)]
        if len(parts) == 1:
            parts = [re.split(r"\b(19|20)\d{2}\b|\b(480|720|1080|2160)p\b", parts[0])[0].strip()]
        parts = [p for p in parts if p]
        if not parts:
            return None
        return (". ".join(parts) + ".")[:120]
    except Exception:
        return None


def _asr_merge_spans(spans, sampling_rate):
    """Merge VAD spans whose gap is under _ASR_MERGE_GAP_SEC without letting a clip exceed _ASR_MAX_CLIP_SEC."""
    merged = []
    max_gap = int(_ASR_MERGE_GAP_SEC * sampling_rate)
    max_len = int(_ASR_MAX_CLIP_SEC * sampling_rate)
    for sp in spans:
        if merged:
            last = merged[-1]
            if sp["start"] - last["end"] <= max_gap and sp["end"] - last["start"] <= max_len:
                last["end"] = sp["end"]
                continue
        merged.append({"start": sp["start"], "end": sp["end"]})
    return merged


def _asr_split_segment(seg, offset=0.0):
    """Split one Whisper segment into short caption cues, with times shifted by `offset`."""
    nsp = getattr(seg, "no_speech_prob", None)
    alp = getattr(seg, "avg_logprob", None)
    loop = _asr_loop_reason(getattr(seg, "text", None))

    def _mean_prob(ws):
        ps = [w.probability for w in ws if getattr(w, "probability", None) is not None]
        return (sum(ps) / len(ps)) if ps else None

    def mk(a, b, text, ws=()):
        return types.SimpleNamespace(start=a, end=b, text=text,
                                     no_speech_prob=nsp, avg_logprob=alp,
                                     word_prob=_mean_prob(ws), recovered=False, runaway=loop,
                                     word_list=[(w.word, getattr(w, "probability", None)) for w in ws])

    words = getattr(seg, "words", None) or []
    whole = [mk(offset + float(seg.start), offset + float(seg.end), (seg.text or "").strip(), words)]
    if not words or any(getattr(w, "start", None) is None or getattr(w, "end", None) is None
                        for w in words):
        return whole
    cues, cur = [], []

    def flush():
        if cur:
            text = "".join((w.word or "") for w in cur).strip()
            if text:
                cues.append(mk(offset + float(cur[0].start), offset + float(cur[-1].end), text, list(cur)))
            del cur[:]

    for w in words:
        wtext = w.word or ""
        if cur:
            gap = w.start - cur[-1].end
            dur = w.end - cur[0].start
            chars = len("".join((x.word or "") for x in cur)) + len(wtext)
            if gap >= _ASR_CUE_GAP_BREAK_SEC or dur > _ASR_CUE_MAX_SEC or chars > _ASR_CUE_MAX_CHARS:
                flush()
        cur.append(w)
        tail = wtext.rstrip().rstrip("\"'\u201d\u2019)")
        chars_now = len("".join((x.word or "") for x in cur).strip())
        if tail.endswith(_ASR_SENTENCE_END) and chars_now >= 12:
            flush()
        elif tail.endswith((",", ";", ":")) and chars_now > 55:
            flush()
    flush()
    return cues or whole


def _asr_finalize_cue_timing(cues):
    """Give each cue a readable minimum on-screen time and a short trailing hold without running into the next cue."""
    cues.sort(key=lambda c: c.start)
    for i, c in enumerate(cues):
        end = max(c.end + _ASR_CUE_TRAIL_HOLD_SEC, c.start + _ASR_CUE_MIN_SHOW_SEC)
        if i + 1 < len(cues):
            end = min(end, max(cues[i + 1].start, c.end))
        c.end = max(end, c.end)


class _SegList(list):
    """list of cue-like objects plus VAD stats for the per-chunk log line."""
    vad_islands = 0
    vad_speech_sec = 0.0
    recover_tried = 0
    recover_ok = 0
    recover_sec = 0.0
    music_secs = ()
    audio_secs = 0.0


def _asr_uncovered_ranges(a, b, intervals):
    """Parts of [a, b] not covered by any (start, end) in `intervals`."""
    out, cur = [], a
    for s, e in sorted(intervals):
        if e <= cur:
            continue
        if s >= b:
            break
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < b:
        out.append((cur, b))
    return out


def _asr_recover_gaps(model, audio, sampling_rate, raw_spans, out, own_len, language, prompt,
                      budget_sec, should_stop):
    """Re-read VAD-detected speech the first pass left uncaptioned and append accepted cues to `out`. Never raises."""
    t_start = time.monotonic()
    deadline = t_start + budget_sec
    vocab = _asr_prompt_vocab(prompt)
    secs = len(audio) / float(sampling_rate)
    try:
        cue_iv = [(float(c.start), float(c.end)) for c in out]
        gaps = []
        for sp in raw_spans:
            a = sp["start"] / float(sampling_rate)
            b = min(sp["end"] / float(sampling_rate), own_len)
            if a >= own_len:
                continue
            for ga, gb in _asr_uncovered_ranges(a, b, cue_iv):
                if (gb - ga >= _ASR_RECOVER_MIN_GAP_SEC
                        and _asr_music_fraction(getattr(out, "music_secs", ()), ga, gb) < _ASR_MUSIC_REPLACE_FRAC):
                    gaps.append((ga, gb))
        gaps = sorted(gaps)
    except Exception as e:
        log.warning("[ASR] gap recovery could not analyse the chunk: %s", e)
        return
    windows = []
    for ga, gb in sorted(gaps):
        if (windows and ga - windows[-1]["gaps"][-1][1] <= _ASR_RECOVER_JOIN_SEC
                and gb - windows[-1]["gaps"][0][0] <= _ASR_RECOVER_MAX_WINDOW_SEC):
            windows[-1]["gaps"].append((ga, gb))
        else:
            windows.append({"gaps": [(ga, gb)]})
    for w in windows:
        w["lo"], w["hi"] = w["gaps"][0][0], w["gaps"][-1][1]
        w["sec"] = sum(b - a for a, b in w["gaps"])
    windows = sorted(sorted(windows, key=lambda w: -w["sec"])[:_ASR_RECOVER_MAX_WINDOWS], key=lambda w: w["lo"])
    for w in windows:
        ga, gb = w["lo"], w["hi"]
        try:
            if (should_stop and should_stop()) or time.monotonic() >= deadline:
                break
            s0 = max(0.0, ga - _ASR_RECOVER_PAD_SEC)
            s1 = min(secs, gb + _ASR_RECOVER_PAD_SEC)
            clip = audio[int(s0 * sampling_rate):int(s1 * sampling_rate)]
            if clip.size == 0:
                continue
            out.recover_tried += 1
            segs, _info = model.transcribe(
                clip, language=language, beam_size=_ASR_RECOVER_BEAM, vad_filter=False,
                condition_on_previous_text=False, initial_prompt=None, word_timestamps=True,
                temperature=0.0, no_speech_threshold=None, log_prob_threshold=None,
                compression_ratio_threshold=None)
            for seg in segs:
                for c in _asr_split_segment(seg, s0):
                    text = (c.text or "").strip()
                    mid = (float(c.start) + float(c.end)) / 2.0
                    home = next(((a, b) for a, b in w["gaps"] if a - 0.15 <= mid <= b + 0.15), None)
                    reason = getattr(c, "runaway", None) or _asr_recovery_reject_reason(
                        text, c.word_prob, c.avg_logprob, c.no_speech_prob,
                        ((home[1] - home[0]) if home else (gb - ga)) + 2 * _ASR_RECOVER_PAD_SEC, vocab)
                    if not reason and home is None:
                        reason = "lies outside the gaps (%.2f-%.2f)" % (ga, gb)
                    if not reason:
                        dur = max(0.001, float(c.end) - float(c.start))
                        ov = sum(max(0.0, min(float(c.end), e) - max(float(c.start), s)) for s, e in cue_iv)
                        if ov > _ASR_RECOVER_MAX_OVERLAP_FRAC * dur:
                            reason = "overlaps cues that already exist"
                    if not reason:
                        _w = set(_asr_words(text))
                        if len(_asr_words(text)) >= 3:
                            for nc in out:
                                if nc is c or max(0.0, float(c.start) - float(nc.end),
                                                  float(nc.start) - float(c.end)) > 1.5:
                                    continue
                                if _w and _w <= set(_asr_words(nc.text)):
                                    reason = "repeats the words of a neighbouring cue"
                                    break
                    if reason:
                        continue
                    c.recovered = True
                    out.append(c)
                    cue_iv.append((float(c.start), float(c.end)))
                    out.recover_ok += 1
        except Exception as e:
            log.warning("[ASR] gap recovery window %.2f-%.2f failed: %s", ga, gb, e)
    out.recover_sec = time.monotonic() - t_start


def _asr_transcribe_chunk(model, wav_path, language, beam_size, initial_prompt=None,
                          temperature=None, should_stop=None, own_len=None, recover_budget_sec=0.0):
    """Transcribe one extracted chunk WAV; when `own_len` and a positive `recover_budget_sec` are given, re-read missed speech afterwards."""
    global _asr_vad_utils_load_failed

    def _stopped():
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    temp_kw = {} if temperature is None else {"temperature": temperature}
    if not _asr_vad_utils_load_failed:
        try:
            from faster_whisper.audio import decode_audio
            from faster_whisper.vad import get_speech_timestamps, VadOptions
        except ImportError:
            _asr_vad_utils_load_failed = True
            log.warning("[ASR] faster-whisper VAD utilities unavailable -- falling back to "
                        "the built-in vad_filter (caption sync may drift after long silences).")

    if _asr_vad_utils_load_failed:
        segments_iter, _info = model.transcribe(
            wav_path, language=language, beam_size=beam_size, vad_filter=True,
            vad_parameters=_ASR_VAD_PARAMETERS, condition_on_previous_text=False,
            initial_prompt=initial_prompt, word_timestamps=True, **temp_kw,
        )
        out = _SegList()
        for seg in segments_iter:
            if _stopped():
                break
            out.extend(_asr_split_segment(seg, 0.0))
        _asr_finalize_cue_timing(out)
        return out

    audio = decode_audio(wav_path)
    sampling_rate = 16000
    min_silence = _ASR_VAD_PARAMETERS["min_silence_duration_ms"]
    try:
        vad_opts = VadOptions(threshold=_ASR_VAD_THRESHOLD, min_silence_duration_ms=min_silence,
                              min_speech_duration_ms=_ASR_VAD_MIN_SPEECH_MS)
    except TypeError:
        vad_opts = VadOptions(min_silence_duration_ms=min_silence)
    raw_spans = get_speech_timestamps(audio, vad_opts)
    out = _SegList()
    out.vad_islands = len(raw_spans)
    out.vad_speech_sec = sum((sp["end"] - sp["start"]) for sp in raw_spans) / sampling_rate
    _merged_spans = _asr_merge_spans(raw_spans, sampling_rate)
    out.audio_secs = len(audio) / float(sampling_rate)
    out.music_secs = _asr_music_seconds(audio, sampling_rate)
    for span in _merged_spans:
        if _stopped():
            break
        clip = audio[span["start"]:span["end"]]
        if clip.size == 0:
            continue
        span_start_sec = span["start"] / sampling_rate
        t_clip = time.monotonic()
        segments_iter, _info = model.transcribe(
            clip, language=language, beam_size=beam_size, vad_filter=False,
            condition_on_previous_text=False, initial_prompt=initial_prompt,
            word_timestamps=True, **temp_kw,
        )
        _n_raw = 0
        for seg in segments_iter:
            if _stopped():
                break
            _n_raw += 1
            out.extend(_asr_split_segment(seg, span_start_sec))
        t_clip = time.monotonic() - t_clip
        if _n_raw == 0 and not _stopped():
            log.warning("[ASR] speech was detected in %.1fs of audio starting %.1fs into the chunk, "
                        "but Whisper returned nothing for it", clip.size / float(sampling_rate), span_start_sec)
        if t_clip > 8.0:
            log.warning("[ASR SLOW] %.1fs of speech took %.1fs to transcribe (beam=%s)",
                        clip.size / float(sampling_rate), t_clip, beam_size)
    if (_ASR_RECOVER_ENABLED and own_len is not None and recover_budget_sec > 0.0 and not _stopped()):
        _asr_recover_gaps(model, audio, sampling_rate, raw_spans, out, float(own_len), language,
                          initial_prompt, float(recover_budget_sec), should_stop)
    _asr_finalize_cue_timing(out)
    return out


_ASR_NAME_TOKEN_RE = re.compile(r"^[^A-Za-z]*([A-Za-z][A-Za-z\-]*)(?:['\u2019]s)?[^A-Za-z]*$")


def _asr_name_hits(word_list):
    """Capitalised words in one cue heard at high confidence: {word: True if heard in the middle of a sentence}."""
    hits = {}
    at_start = True
    for raw, prob in word_list:
        tok = (raw or "").strip()
        starts_sentence = at_start
        at_start = tok.rstrip("\"'\u201d\u2019)").endswith(_ASR_SENTENCE_END)
        m = _ASR_NAME_TOKEN_RE.match(tok)
        if not m or prob is None or prob < _ASR_NAME_MIN_WORD_PROB:
            continue
        name = m.group(1)
        if len(name) < _ASR_NAME_MIN_LEN or not name[0].isupper() or not any(ch.islower() for ch in name[1:]):
            continue
        hits[name] = hits.get(name, False) or not starts_sentence
    return hits


def _asr_learn_names(stats, word_lists):
    """Counts, per capitalised word, the cues it was heard clearly in and how many had it mid-sentence."""
    for wl in word_lists:
        if not wl:
            continue
        for name, mid in _asr_name_hits(wl).items():
            e = stats.setdefault(name, {"cues": 0, "mid": 0, "order": len(stats)})
            e["cues"] += 1
            if mid:
                e["mid"] += 1


def _asr_learned_names(stats):
    """Names worth hinting: heard clearly in several cues and capitalised mid-sentence at least once, most-heard first."""
    ok = [(n, e) for n, e in stats.items() if e["cues"] >= _ASR_NAME_MIN_CUES and e["mid"] >= 1]
    ok.sort(key=lambda ne: (-ne[1]["cues"], ne[1]["order"]))
    names = []
    for n, _ in ok:
        # WARNING: a near-identical spelling of a name already chosen (Bongo next to Pongo) is the same misheard word, not a second name.
        if any(difflib.SequenceMatcher(None, n.lower(), k.lower()).ratio() >= _ASR_NAME_SIMILAR_MIN for k in names):
            continue
        names.append(n)
        if len(names) >= _ASR_NAME_MAX_LEARNED:
            break
    return names


def _asr_prompt_with_names(base, names):
    if not names:
        return base
    return ((base + " ") if base else "") + ", ".join(names) + "."


def _asr_music_seconds(audio, sampling_rate=16000):
    """Per-second music flags for decoded audio: loud, with spectral peaks that hold their place and a steady loudness."""
    n_fft, hop, per_peak = 512, 256, 6
    flags = []
    try:
        win = np.hanning(n_fft).astype(np.float32)
        lag = 4
        for sec in range(int(len(audio) // sampling_rate)):
            seg = np.asarray(audio[sec * sampling_rate:(sec + 1) * sampling_rate], dtype=np.float32)
            rms = float(np.sqrt(np.mean(seg * seg)) + 1e-9)
            db = 20.0 * math.log10(rms)
            n_frames = (len(seg) - n_fft) // hop + 1
            if db < _ASR_MUSIC_MIN_RMS_DB or n_frames <= lag + 1:
                flags.append({"db": db, "persist": 0.0, "cv": 0.0, "music": False})
                continue
            idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
            frames = seg[idx] * win
            mag = np.abs(np.fft.rfft(frames, axis=1))[:, 6:129]
            energy = np.sqrt(np.mean(frames * frames, axis=1))
            cv = float(np.std(energy) / (np.mean(energy) + 1e-9))
            peaks = np.zeros(mag.shape, dtype=bool)
            inner = (mag[:, 1:-1] > mag[:, :-2]) & (mag[:, 1:-1] >= mag[:, 2:])
            order = np.argsort(-np.where(inner, mag[:, 1:-1], 0.0), axis=1)[:, :per_peak]
            rows = np.arange(n_frames)[:, None]
            keep = inner[rows, order] & (mag[:, 1:-1][rows, order] > 0.1 * mag.max(axis=1, keepdims=True))
            peaks[:, 1:-1][rows, order] = keep
            wide = peaks.copy()
            wide[:, 1:] |= peaks[:, :-1]
            wide[:, :-1] |= peaks[:, 1:]
            held = (peaks[:-lag] & wide[lag:]).sum()
            total = peaks[:-lag].sum()
            persist = float(held) / float(total) if total else 0.0
            flags.append({"db": db, "persist": persist, "cv": cv,
                          "music": persist >= _ASR_MUSIC_PEAK_PERSIST_MIN and cv <= _ASR_MUSIC_ENVELOPE_CV_MAX})
    except Exception as e:
        log.warning("[ASR] music analysis failed: %s", e)
        return []
    return flags


def _asr_music_runs(flags, own_len):
    """(start, end) second ranges inside own_len where music-flagged seconds run for _ASR_MUSIC_MIN_SEC or more, bridging short gaps."""
    runs, start, last = [], None, None
    limit = int(own_len)
    for i, f in enumerate(flags[:limit]):
        if f["music"]:
            if start is None:
                start = i
            elif i - last - 1 > _ASR_MUSIC_BRIDGE_SEC:
                runs.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        runs.append((start, last + 1))
    return [(a, b) for a, b in runs if b - a >= _ASR_MUSIC_MIN_SEC]


def _asr_music_fraction(flags, a, b):
    """Share of the seconds between a and b (chunk-relative) that are music-flagged."""
    lo, hi = int(max(0.0, a)), int(math.ceil(max(a, b)))
    window = flags[lo:hi]
    if not window:
        return 0.0
    return sum(1 for f in window if f["music"]) / float(len(window))


def _asr_music_cues(flags, chunk_start, own_len, existing):
    """[start, end, text] music-sign cues for music stretches no existing cue covers, split into short pieces."""
    cues = []
    intervals = [(float(c[0]), float(c[1])) for c in existing]
    for a, b in _asr_music_runs(flags, own_len):
        for ua, ub in _asr_uncovered_ranges(chunk_start + a, chunk_start + b, intervals):
            if ub - ua < _ASR_MUSIC_MIN_SEC:
                continue
            t = ua
            while t < ub - 1e-6:
                piece_end = min(ub, t + _ASR_MUSIC_PIECE_SEC)
                if ub - piece_end <= 1.0:
                    piece_end = ub
                cues.append([t, piece_end, _ASR_MUSIC_CAPTION_TEXT])
                t = piece_end
    return cues


class _CueList(list):
    """Kept [start, end, text] cues plus `carry`: valid cues that start inside the next chunk's range."""

    def __init__(self, *args):
        super().__init__(*args)
        self.carry = []
        self.words = []


def _asr_carry_is_covered(cue, new_cues):
    """True when the chunk after a boundary already produced this cue (same time, or the same words just beside it)."""
    cs, ce, ct = cue
    span = max(0.001, ce - cs)
    a = (ct or "").strip().lower()
    for ns, ne, nt in new_cues:
        if min(ce, ne) - max(cs, ns) >= _ASR_CARRY_COVER_FRAC * span:
            return True
        b = (nt or "").strip().lower()
        if (a and b and ns <= ce + _ASR_CARRY_NEAR_SEC and ne >= cs - _ASR_CARRY_NEAR_SEC
                and (a in b or b in a
                     or difflib.SequenceMatcher(None, a, b).ratio() >= _ASR_DUP_TEXT_SIMILARITY_MIN)):
            return True
    return False


def _asr_filter_cues(cues, chunk_start, chunk_end, at_file_end, prompt=None, music_secs=None):
    """Turn a chunk's cue-like objects into absolute [start, end, text] cues, dropping out-of-chunk, non-speech and hallucinated ones."""
    kept = _CueList()
    drops = {"next_chunk": 0, "empty": 0, "not_speech": 0, "hallucination": 0}
    vocab = _asr_prompt_vocab(prompt)
    for c in cues:
        s_abs = chunk_start + float(c.start)
        e_abs = chunk_start + float(c.end)
        if not at_file_end and s_abs >= chunk_end:
            drops["next_chunk"] += 1
            _ctext = (c.text or "").strip()
            _cnsp = getattr(c, "no_speech_prob", None)
            _calp = getattr(c, "avg_logprob", None)
            if (_ctext and e_abs > s_abs and not getattr(c, "runaway", None)
                    and not (_cnsp is not None and _calp is not None
                             and _cnsp > _ASR_NO_SPEECH_PROB_MAX and _calp < _ASR_AVG_LOGPROB_MIN)
                    and not _asr_cue_reject_reason(_ctext, getattr(c, "word_prob", None), vocab)):
                kept.carry.append([s_abs, e_abs, _ctext])
            continue
        text = (c.text or "").strip()
        if not text or e_abs <= s_abs:
            drops["empty"] += 1
            continue
        nsp = getattr(c, "no_speech_prob", None)
        alp = getattr(c, "avg_logprob", None)
        if (nsp is not None and alp is not None
                and nsp > _ASR_NO_SPEECH_PROB_MAX and alp < _ASR_AVG_LOGPROB_MIN):
            drops["not_speech"] += 1
            continue
        _why = getattr(c, "runaway", None) or _asr_cue_reject_reason(text, getattr(c, "word_prob", None), vocab)
        if _why:
            drops["hallucination"] += 1
            continue
        _wp = getattr(c, "word_prob", None)
        if (music_secs and _wp is not None and _wp < _ASR_MUSIC_REPLACE_MAX_WORD_PROB
                and _asr_music_fraction(music_secs, s_abs - chunk_start, e_abs - chunk_start) >= _ASR_MUSIC_REPLACE_FRAC):
            text = _ASR_MUSIC_CAPTION_TEXT
            kept.words.append(None)
        else:
            kept.words.append(getattr(c, "word_list", None))
        kept.append([s_abs, e_abs, text])
    return kept, drops


def asr_known_unavailable():
    """True once this run has found faster-whisper missing or its model unloadable."""
    return bool(_whisper_model_load_failed)


def _srt_timestamp(seconds):
    ms_total = max(0, round(seconds * 1000))
    h, rem = divmod(ms_total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================================
# SECTION: Chunked ASR captions (disk-cached)
# ============================================================================
ASR_REASON_SUCCESS = "ok"
ASR_REASON_UNAVAILABLE = "unavailable"
ASR_REASON_NO_AUDIO = "no_audio"
ASR_REASON_NO_SPEECH = "no_speech"
ASR_REASON_EXTRACT_FAILED = "extract_failed"
ASR_REASON_TRANSCRIBE_FAILED = "transcribe_failed"
ASR_REASON_INTERRUPTED = "interrupted"

_ASR_FIRST_CHUNK_SEC = 60.0
_ASR_CHUNK_SEC = 300.0
_ASR_CHUNK_OVERLAP_SEC = 8.0
# VALUES CHANGE HERE
_ASR_MIN_TAIL_SEC = 15.0
_ASR_META_VERSION = 1


def _asr_paths(file_path):
    """(srt_path, meta_path) for a plain media file."""
    import hashlib
    norm_path = os.path.normpath(os.path.abspath(file_path))
    digest = hashlib.sha1(norm_path.encode("utf-8", "replace")).hexdigest()[:16]
    out_dir = resolve_captions_dir()
    return (os.path.join(out_dir, f"{digest}.srt"),
            os.path.join(out_dir, f"{digest}.asr.json"))


def _atomic_write_text(path, text):
    """Write `text` to `path` so a reader sees either the old or the new complete file, never a partial one."""
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as e:
        log.warning("[ASR] could not write %s: %s", tmp_path, e)
        return False
    last_err = None
    for _attempt in range(20):
        try:
            os.replace(tmp_path, path)
            return True
        except OSError as e:
            last_err = e
            time.sleep(0.05)
    log.warning("[ASR] could not replace %s: %s", path, last_err)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return False


def _asr_serialize_srt(cues):
    """cues: list of [start_sec, end_sec, text] -> SRT text."""
    parts = []
    for i, (start, end, text) in enumerate(cues, 1):
        parts.append(f"{i}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n\n")
    return "".join(parts)


def _asr_read_valid(file_path, mtime, size):
    """Return {"srt_path", "meta", "srt_exists"} for a valid ASR resume record matching this exact file, else None."""
    try:
        srt_path, meta_path = _asr_paths(file_path)
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict) or meta.get("version") != _ASR_META_VERSION:
            return None
        if meta.get("mtime") != mtime or meta.get("size") != size:
            return None
        srt_exists = os.path.isfile(srt_path) and os.path.getsize(srt_path) > 0
        if int(meta.get("cue_count", 0) or 0) > 0 and not srt_exists:
            return None
        return {"srt_path": srt_path, "meta": meta, "srt_exists": srt_exists}
    except Exception:
        return None


def _asr_discard_state(file_path):
    """Delete a stale/invalid ASR resume record and its partial transcript."""
    try:
        srt_path, meta_path = _asr_paths(file_path)
    except Exception:
        return
    for p in (srt_path, meta_path, srt_path + ".tmp", meta_path + ".tmp"):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def _asr_merge_ranges(ranges):
    """Merge [start, end] pairs into a sorted list of disjoint [start, end]."""
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges if float(b) > float(a)):
        if out and a <= out[-1][1] + 0.01:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _asr_subtract_ranges(lo, hi, done):
    """The pieces of [lo, hi) not covered by `done` (merged + sorted), in ascending order."""
    out = []
    pos = lo
    for a, b in done:
        if b <= pos:
            continue
        if a >= hi:
            break
        if a > pos:
            out.append((pos, min(a, hi)))
        pos = max(pos, b)
        if pos >= hi:
            break
    if pos < hi:
        out.append((pos, hi))
    return out


def _asr_plan_chunks(duration, start_sec, done):
    """Return the ordered (start, end) chunks still to transcribe: start_sec to end of file first, then 0 to start_sec."""
    start = min(max(0.0, float(start_sec or 0.0)), float(duration))
    out = []
    first = True
    for lo, hi in ((start, float(duration)), (0.0, start)):
        for a, b in _asr_subtract_ranges(lo, hi, done):
            pos = a
            while b - pos > 0.5:
                size = _ASR_FIRST_CHUNK_SEC if first else _ASR_CHUNK_SEC
                end = min(b, pos + size)
                if b - end < _ASR_MIN_TAIL_SEC:
                    end = b
                out.append((pos, end))
                pos = end
                first = False
    if not out and not done and duration > 0.1:
        # WARNING: never plan nothing for a file that has had nothing done yet.
        out.append((0.0, float(duration)))
    return out


_ASR_DUP_TEXT_SIMILARITY_MIN = 0.6

_ASR_SHORT_TEXT_CHARS = 6
_ASR_SHORT_TEXT_OVERLAP_MIN = 0.85


def _asr_cues_are_same_speech(new_text, old_text, overlap_frac=None):
    """True only if two time-overlapping cues are the same utterance re-transcribed, not two different sentences."""
    a = (new_text or "").strip().lower()
    b = (old_text or "").strip().lower()
    if not a or not b:
        return False
    text_matches = False
    if a in b or b in a:
        text_matches = True
    elif difflib.SequenceMatcher(None, a, b).ratio() >= _ASR_DUP_TEXT_SIMILARITY_MIN:
        text_matches = True
    if not text_matches:
        return False
    if len(a) <= _ASR_SHORT_TEXT_CHARS or len(b) <= _ASR_SHORT_TEXT_CHARS:
        is_dup = overlap_frac is not None and overlap_frac >= _ASR_SHORT_TEXT_OVERLAP_MIN
        log.info(
            "[LIVE ASR SHORT-TEXT] %r vs %r: overlap=%s min=%.2f -> %s",
            new_text, old_text,
            ("n/a" if overlap_frac is None else f"{overlap_frac:.2f}"),
            _ASR_SHORT_TEXT_OVERLAP_MIN,
            "treated as duplicate (dropped)" if is_dup else "kept as distinct cue")
        return is_dup
    return True


def _asr_merge_cues(cues, new_cues):
    """Add new_cues ([start, end, text] lists) into `cues` in place."""
    added = 0
    for s, e, t in new_cues:
        span = max(0.001, e - s)
        dup_idx = None
        for i, (cs, ce, ct) in enumerate(cues):
            overlap = min(e, ce) - max(s, cs)
            shorter_span = min(span, max(0.001, ce - cs))
            if overlap > 0.5 * shorter_span:
                overlap_frac = overlap / shorter_span
                if _asr_cues_are_same_speech(t, ct, overlap_frac=overlap_frac):
                    dup_idx = i
                    break
                log.info(
                    "[LIVE ASR MERGE] overlapping cues kept as distinct speech: "
                    "existing=%.2f-%.2fs %r new=%.2f-%.2fs %r",
                    cs, ce, ct, s, e, t)
        if dup_idx is None:
            cues.append([s, e, t])
            added += 1
        else:
            cs, ce, _ct = cues[dup_idx]
            if span > (ce - cs) + 0.25:
                cues[dup_idx] = [s, e, t]
    cues.sort(key=lambda c: c[0])
    return added


def _asr_probe_media(file_path, timeout=30):
    """One ffmpeg header read returning {"duration": float|None, "has_audio": bool}, or None if unreadable."""
    try:
        import media_library as _ml
    except Exception as e:
        log.warning("[ASR] probe failed for %s: %s (media_library import)",
                    os.path.basename(file_path), e)
        return None
    result = _shared_header_probe(file_path, timeout)
    if not result.ok:
        if result.error != "path_missing":
            log.warning("[ASR] probe failed for %s: %s", os.path.basename(file_path), result.error)
        return None
    return {"duration": result.duration, "has_audio": result.has_audio}


_ASR_ACCURATE_SEEK_MARGIN_SEC = 15.0


def _asr_extract_chunk(file_path, start_sec, length_sec, out_wav_path, timeout=120,
                        accurate_seek_margin_sec=None, audio_filter=None):
    """Extract mono 16 kHz PCM for just [start, start+length] of the file."""
    if accurate_seek_margin_sec is None:
        accurate_seek_margin_sec = _ASR_ACCURATE_SEEK_MARGIN_SEC
    start_sec = max(0.0, start_sec)
    coarse_sec = max(0.0, start_sec - accurate_seek_margin_sec)
    fine_sec = start_sec - coarse_sec
    try:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"asr_extract({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{coarse_sec:.3f}", "-i", file_path,
                 "-ss", f"{fine_sec:.3f}",
                 "-t", f"{max(0.1, length_sec):.3f}",
                 "-vn", "-ac", "1", "-ar", "16000",
                 *(["-af", audio_filter] if audio_filter else []),
                 "-f", "wav", out_wav_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs
            )
    except subprocess.TimeoutExpired:
        return "failed"
    except Exception as e:
        log.warning("[ASR] chunk extract error for %s: %s", os.path.basename(file_path), e)
        return "failed"
    try:
        size = os.path.getsize(out_wav_path) if os.path.isfile(out_wav_path) else 0
    except OSError:
        size = 0
    if size > 44:
        return "ok"
    return "empty" if result.returncode == 0 else "failed"


def _asr_cleanup_orphan_wavs(out_dir, max_age_sec=3600):
    """Remove temp wavs a hard kill left behind (only our own asr_*.wav)."""
    try:
        now = time.time()
        for name in os.listdir(out_dir):
            if name.startswith("asr_") and name.endswith(".wav"):
                p = os.path.join(out_dir, name)
                try:
                    if now - os.path.getmtime(p) > max_age_sec:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def generate_captions_via_asr_chunked(file_path, start_sec=0.0, language=None,
                                      should_stop=None, timeout_extract=120):
    """Transcribes a video or audio file in chunks with local faster-whisper and returns (srt_path, reason); an unchanged file resumes where it stopped."""
    def _stopped():
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    if not file_path or not _cached_path_exists(file_path):
        return None, ASR_REASON_EXTRACT_FAILED
    try:
        st = os.stat(file_path)
    except OSError:
        return None, ASR_REASON_EXTRACT_FAILED
    srt_path, meta_path = _asr_paths(file_path)
    base_name = os.path.basename(file_path)

    state = _asr_read_valid(file_path, st.st_mtime, st.st_size)
    if state and state["meta"].get("complete"):
        if state["srt_exists"]:
            return srt_path, ASR_REASON_SUCCESS
        return None, ASR_REASON_NO_SPEECH

    model = _get_whisper_model()
    if model is None:
        return None, ASR_REASON_UNAVAILABLE

    probe = _asr_probe_media(file_path)
    if probe is None or not probe["duration"]:
        return None, ASR_REASON_EXTRACT_FAILED
    if not probe["has_audio"]:
        return None, ASR_REASON_NO_AUDIO
    duration = probe["duration"]

    if state is None:
        _asr_discard_state(file_path)
        done, cues, audio_chunks = [], [], 0
    else:
        meta = state["meta"]
        done = _asr_merge_ranges(meta.get("done") or [])
        audio_chunks = int(meta.get("audio_chunks", 0) or 0)
        cues = []
        if state["srt_exists"]:
            cues = [[s / 1000.0, e / 1000.0, t] for s, e, t in parse_caption_cues(srt_path)]
        log.info("[ASR] resuming %s: %d range(s) already done, %d cues",
                 base_name, len(done), len(cues))

    lang = language if language is not None else _ASR_LANGUAGE
    out_dir = resolve_captions_dir()
    _asr_cleanup_orphan_wavs(out_dir)

    def _save(complete):
        ok = True
        if cues:
            ok = _atomic_write_text(srt_path, _asr_serialize_srt(cues))
        meta_out = {
            "version": _ASR_META_VERSION, "mtime": st.st_mtime, "size": st.st_size,
            "duration": duration, "model": _ASR_MODEL_SIZE, "done": done,
            "cue_count": len(cues), "audio_chunks": audio_chunks, "complete": bool(complete),
        }
        return ok and _atomic_write_text(meta_path, json.dumps(meta_out))

    plan = _asr_plan_chunks(duration, start_sec, done)
    total = len(plan)
    import tempfile
    _prev_chunk_end = None
    for idx, (a, b) in enumerate(plan, 1):
        if _stopped():
            return None, ASR_REASON_INTERRUPTED
        _yield_to_live_playback(f"asr_chunk({base_name})")

        if _prev_chunk_end is not None and abs(a - _prev_chunk_end) > 0.05:
            _is_wrap = a < _prev_chunk_end - 0.05
            _is_resume_skip = any(d_a - 0.05 <= _prev_chunk_end and a <= d_b + 0.05 for d_a, d_b in done)
            if not _is_wrap and not _is_resume_skip:
                log.warning(
                    "[CAPTION SAMPLE GAP] %s %.2fs of show audio between %.2fs and %.2fs "
                    "was never sampled by any chunk -- any speech in that stretch cannot "
                    "appear in captions.", base_name, a - _prev_chunk_end, _prev_chunk_end, a,
                )
        _prev_chunk_end = b

        at_file_end = b >= duration - 0.5
        audio_end = duration if at_file_end else min(duration, b + _ASR_CHUNK_OVERLAP_SEC)
        wav_fd, wav_path = tempfile.mkstemp(prefix="asr_", suffix=".wav", dir=out_dir)
        os.close(wav_fd)
        t0 = time.monotonic()
        try:
            status = _asr_extract_chunk(file_path, a, audio_end - a, wav_path, timeout=timeout_extract)
            new_cues = []
            if status == "failed":
                log.info("[ASR] extract failed for %s at %.0fs", base_name, a)
                return None, ASR_REASON_EXTRACT_FAILED
            if status == "ok":
                audio_chunks += 1
                try:
                    _disk_prompt = _asr_prompt_for(file_path)
                    segments = _asr_transcribe_chunk(model, wav_path, lang, _ASR_BEAM_SIZE,
                                                     initial_prompt=_disk_prompt,
                                                     should_stop=_stopped)
                    if _stopped():
                        return None, ASR_REASON_INTERRUPTED
                    new_cues, _drops = _asr_filter_cues(segments, a, b, at_file_end, prompt=_disk_prompt)
                except Exception as e:
                    log.warning("[ASR] transcription failed for %s: %s", base_name, e)
                    return None, ASR_REASON_TRANSCRIBE_FAILED
        finally:
            try:
                if os.path.isfile(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass

        _asr_merge_cues(cues, new_cues)
        done = _asr_merge_ranges(done + [[a, b]])
        if not _save(complete=False):
            return None, ASR_REASON_TRANSCRIBE_FAILED
        log.info(
            "[ASR] %s chunk %d/%d (%.0f-%.0fs): +%d cues (%d total) in %.1fs",
            base_name, idx, total, a, b, len(new_cues), len(cues), time.monotonic() - t0)

    if audio_chunks == 0:
        return None, ASR_REASON_EXTRACT_FAILED
    if not _save(complete=True):
        return None, ASR_REASON_TRANSCRIBE_FAILED
    if not cues:
        log.info("[ASR] no speech detected in %s", base_name)
        return None, ASR_REASON_NO_SPEECH
    log.info("[ASR] captions generated for %s (%d cues)", base_name, len(cues))
    return srt_path, ASR_REASON_SUCCESS


# ============================================================================
# SECTION: Live captions
# ============================================================================

# VALUES CHANGE HERE
LIVE_CAPTION_LEAD_SEC = 5.0
LIVE_CAPTION_READY_DEADLINE_SEC = 10.0
LIVE_CAPTION_CHUNK_SEC = 30.0
# VALUES CHANGE HERE (fallback only; used when performance_governor is missing or fails; the live values are CAPTION_CADENCE in performance_governor.py)
_LIVE_CAPTION_STARTUP_CHUNK_SEC = 10.0
_LIVE_CAPTION_FIRST_CHUNK_SEC = 5.0
_LIVE_CAPTION_FIRST_CHUNK_OVERLAP_SEC = 3.0
_LIVE_CAPTION_STARTUP_CHUNK_OVERLAP_SEC = 5.0
_LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC = 3.0
_LIVE_CAPTION_PREFETCH_MIN_LEAD_SEC = 15.0
_LIVE_CAPTION_PREFETCH_CHUNK_SEC = 10.0
_LIVE_CAPTION_PREFETCH_MAX_WAIT_SEC = 120.0
# VALUES CHANGE HERE (next-file prefetch cap)
_LIVE_CAPTION_PREFETCH_CAP_SEC = 60.0
_LIVE_ATTACHED_SESSIONS = set()
_LIVE_RUNNING_SESSIONS = set()


def _live_caption_cadence():
    """First-scan chunk sizes (first chunk, its overlap, startup chunk) for the current performance mode."""
    if _perf_governor is not None:
        try:
            cadence = _perf_governor.caption_cadence()
            return (float(cadence["first_chunk_sec"]), float(cadence["first_overlap_sec"]),
                    float(cadence["startup_chunk_sec"]))
        except Exception:
            pass
    return (_LIVE_CAPTION_FIRST_CHUNK_SEC, _LIVE_CAPTION_FIRST_CHUNK_OVERLAP_SEC, _LIVE_CAPTION_STARTUP_CHUNK_SEC)


def _prefetch_may_run():
    """True when no attached live session needs the transcription slot."""
    for sess in list(_LIVE_ATTACHED_SESSIONS):
        if sess._stopped():
            _LIVE_ATTACHED_SESSIONS.discard(sess)
            continue
        if sess._busy or sess._last_lead < _LIVE_CAPTION_PREFETCH_MIN_LEAD_SEC:
            return False
    return True
_LIVE_CAPTION_BIG_CHUNK_SAFETY_MARGIN_SEC = 3.0
_LIVE_CAPTION_BIG_CHUNK_DEFAULT_ESTIMATE_SEC = 15.0
# VALUES CHANGE HERE (lead cap)
LIVE_CAPTION_MAX_LEAD_SEC = 60.0
# VALUES CHANGE HERE (jump-back detection)
_LIVE_CAPTION_JUMP_BACK_LEAD_SEC = 120.0
_LIVE_CAPTION_PACE_POLL_SEC = 1.0
_LIVE_CAPTION_SKIP_BEHIND_SEC = 15.0
_LIVE_CAPTION_RESUME_AHEAD_SEC = 2.0
# VALUES CHANGE HERE
_LIVE_CAPTION_KEEP_PAST_SEC = 20.0
_LIVE_CAPTION_MAX_CUES = 500
# VALUES CHANGE HERE
_LIVE_CAPTION_MAX_FAILURES = 3
_ASR_LIVE_BEAM_SIZE = 3
_ASR_LIVE_ACCURATE_SEEK_MARGIN_SEC = 4.0
_LIVE_CAPTION_CLOCK_MIN_DT = 0.75
_LIVE_CAPTION_CLOCK_TOLERANCE = 0.4


class LiveCaptionSession:
    """Transcribes `file_path` forward from `start_sec` on a background thread."""

    def __init__(self, file_path, start_sec=0.0, language=None, position_fn=None):
        self.file_path = file_path
        self.start_sec = max(0.0, float(start_sec or 0.0))
        self.language = language if language is not None else _ASR_LANGUAGE
        self.position_fn = position_fn
        self.cues = []
        self.lock = threading.Lock()
        self.ready = False
        self.status = None
        self._stop = threading.Event()
        self._thread = None
        self._created = time.monotonic()
        self._anchor_mono = self._created
        self._anchor_pos = self.start_sec
        self._clock_probe = None
        self._clock_trusted = False
        self._attached = position_fn is not None
        self._just_attached = False
        self._chunk_rtf_ema = None
        # WARNING: read by _prefetch_may_run().
        self._busy = False
        self._last_lead = 0.0
        self._last_sample_end = None
        self._prefetch_capped = False
        self._chunks_done = 0
        self._late_cues = 0
        self._partial_late = 0

    def start(self):
        _LIVE_RUNNING_SESSIONS.add(self)
        if self._attached:
            _LIVE_ATTACHED_SESSIONS.add(self)
        self._thread = threading.Thread(target=self._run, daemon=True, name="LiveCaptionASR")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self.lock:
            self.cues = []

    def snapshot(self):
        """Return cues in milliseconds as [start_ms, end_ms, text]."""
        with self.lock:
            return [[s * 1000.0, e * 1000.0, t] for s, e, t in self.cues]

    def attach_playback(self, position_fn, file_pos=0.0):
        """Give a prefetched session the player clock when its file goes live, re-anchoring the wall-clock estimate to `file_pos`."""
        self.position_fn = position_fn
        self._anchor_mono = time.monotonic()
        self._anchor_pos = max(0.0, float(file_pos or 0.0))
        self._clock_probe = None
        self._clock_trusted = False
        self._attached = True
        self._just_attached = True
        _LIVE_ATTACHED_SESSIONS.add(self)

    def _stopped(self):
        return self._stop.is_set()

    def _playback_sec(self):
        """Current playback position in file seconds, from position_fn when it advances in step with real time, else the wall-clock estimate."""
        mono = time.monotonic()
        est = self._anchor_pos + (mono - self._anchor_mono)
        fn = self.position_fn
        if fn is None:
            return est
        try:
            p = fn()
        except Exception:
            p = None
        if p is None or p < 0:
            return est
        if self._clock_probe is None:
            self._clock_probe = (mono, p)
        else:
            probe_mono, probe_p = self._clock_probe
            dt = mono - probe_mono
            if dt >= _LIVE_CAPTION_CLOCK_MIN_DT:
                self._clock_trusted = abs((p - probe_p) - dt) <= _LIVE_CAPTION_CLOCK_TOLERANCE
                self._clock_probe = (mono, p)
        if self._clock_trusted:
            self._anchor_mono, self._anchor_pos = mono, p
            return p
        return est

    def _trim_cues(self):
        if not self._attached:
            with self.lock:
                if len(self.cues) > _LIVE_CAPTION_MAX_CUES:
                    del self.cues[:len(self.cues) - _LIVE_CAPTION_MAX_CUES]
            return
        cutoff = self._playback_sec() - _LIVE_CAPTION_KEEP_PAST_SEC
        with self.lock:
            self.cues[:] = [c for c in self.cues if c[1] >= cutoff]
            if len(self.cues) > _LIVE_CAPTION_MAX_CUES:
                del self.cues[:len(self.cues) - _LIVE_CAPTION_MAX_CUES]

    def _run(self):
        try:
            self._run_locked()
        except Exception as e:
            log.warning("[LIVE ASR] session failed for %s: %s",
                        os.path.basename(self.file_path), e)
            self.status = ASR_REASON_TRANSCRIBE_FAILED
        finally:
            _LIVE_ATTACHED_SESSIONS.discard(self)
            _LIVE_RUNNING_SESSIONS.discard(self)
            log.info("[LIVE ASR] %s session ended: status=%s chunks=%d late_cues=%d partial_late_cues=%d",
                     os.path.basename(self.file_path), self.status, self._chunks_done, self._late_cues,
                     self._partial_late)

    def _run_locked(self):
        import tempfile

        if self._stopped():
            return
        model = _get_whisper_model()
        if model is None:
            self.status = ASR_REASON_UNAVAILABLE
            return
        with _playing_file_scope(self._attached):
            probe = _asr_probe_media(self.file_path)
        if probe is None or not probe["duration"]:
            self.status = ASR_REASON_EXTRACT_FAILED
            return
        if not probe["has_audio"]:
            self.status = ASR_REASON_NO_AUDIO
            return
        duration = probe["duration"]
        base_name = os.path.basename(self.file_path)
        base_prompt = _asr_prompt_for(self.file_path)
        prompt = base_prompt
        name_stats = {}
        learned_names = []

        pos = min(self.start_sec, duration)
        chunks_done = 0
        failures = 0
        carry_prev = []
        carry_end = None
        while not self._stopped() and pos < duration:
            _yield_to_live_playback(f"live_caption({base_name})")

            if not self._attached and pos - self.start_sec >= _LIVE_CAPTION_PREFETCH_CAP_SEC:
                if not self._prefetch_capped:
                    self._prefetch_capped = True
                    log.info("[LIVE ASR] %s prefetch reached its %.0fs cap (transcribed up to %.1fs) -- "
                             "idle until this file goes live", base_name, _LIVE_CAPTION_PREFETCH_CAP_SEC, pos)
                self._stop.wait(_LIVE_CAPTION_PACE_POLL_SEC)
                continue

            if not self._attached:
                now, lead = None, LIVE_CAPTION_MAX_LEAD_SEC
            else:
                now = self._playback_sec()
                lead = pos - now
                just_attached, self._just_attached = self._just_attached, False
                if chunks_done > 0 and lead < -_LIVE_CAPTION_SKIP_BEHIND_SEC:
                    new_pos = min(duration, now + _LIVE_CAPTION_RESUME_AHEAD_SEC)
                    log.warning("[LIVE ASR] %s behind playback by %.1fs -- skipping ahead to %.1fs; "
                                "the audio from %.1fs to %.1fs gets no captions",
                                base_name, -lead, new_pos, pos, new_pos)
                    pos = new_pos
                    if pos >= duration:
                        break
                    lead = pos - now
                elif chunks_done > 0 and not just_attached and lead > _LIVE_CAPTION_JUMP_BACK_LEAD_SEC:
                    log.warning("[LIVE ASR] %s playback is %.1fs behind the transcription position -- "
                                "restarting from %.1fs", base_name, lead, now)
                    pos, lead = now, 0.0

            if chunks_done == 0 and self._attached and now is not None:
                _first_pos = min(duration, now + _LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC)
                if pos < _first_pos:
                    log.info("[LIVE ASR] %s first chunk was set to start %.1fs %s playback -- "
                             "starting at %.1fs (playback %.1fs + %.1fs ahead) so its cues land in time",
                             base_name, abs(lead), "behind" if lead < 0 else "ahead of",
                             _first_pos, now, _LIVE_CAPTION_FIRST_CHUNK_AHEAD_SEC)
                    pos = _first_pos
                    lead = pos - now
                    if pos >= duration:
                        break

            self._last_lead = lead if self._attached else LIVE_CAPTION_MAX_LEAD_SEC

            if not self.ready and (
                    chunks_done > 0
                    or lead >= LIVE_CAPTION_LEAD_SEC
                    or (chunks_done > 0
                        and time.monotonic() - self._created >= LIVE_CAPTION_READY_DEADLINE_SEC)):
                self.ready = True
                log.info("[LIVE ASR READY] %s captions live %.1fs after session start "
                         "(chunks_done=%d lead=%s)", base_name,
                         time.monotonic() - self._created, chunks_done,
                         ("n/a" if now is None else f"{lead:.1f}s"))
            if self.ready and self._attached:
                while not self._stopped() and lead > LIVE_CAPTION_MAX_LEAD_SEC:
                    self._stop.wait(_LIVE_CAPTION_PACE_POLL_SEC)
                    lead = pos - self._playback_sec()
                if self._stopped():
                    break

            small = (not self.ready) or lead < LIVE_CAPTION_LEAD_SEC
            est_big_chunk_sec = (self._chunk_rtf_ema * LIVE_CAPTION_CHUNK_SEC
                                  if self._chunk_rtf_ema is not None
                                  else _LIVE_CAPTION_BIG_CHUNK_DEFAULT_ESTIMATE_SEC)
            use_big_chunk = (not small) and lead >= (
                est_big_chunk_sec + _LIVE_CAPTION_BIG_CHUNK_SAFETY_MARGIN_SEC)
            _first_len, _first_overlap, _startup_len = _live_caption_cadence()
            _target_len = LIVE_CAPTION_CHUNK_SEC if use_big_chunk else _startup_len
            if chunks_done == 0 and self._attached:
                _target_len = _first_len
            elif not self._attached:
                _target_len = _LIVE_CAPTION_PREFETCH_CHUNK_SEC
            length = min(_target_len, duration - pos)
            at_file_end = (pos + length) >= duration - 0.5
            if chunks_done == 0 and self._attached:
                _overlap = _first_overlap
            elif not self._attached or not use_big_chunk:
                _overlap = _LIVE_CAPTION_STARTUP_CHUNK_OVERLAP_SEC
            else:
                _overlap = _ASR_CHUNK_OVERLAP_SEC
            audio_end = duration if at_file_end else min(duration, pos + length + _overlap)

            if not self._attached:
                _pf_deadline = time.monotonic() + _LIVE_CAPTION_PREFETCH_MAX_WAIT_SEC
                while (not self._stopped() and not _prefetch_may_run()
                       and time.monotonic() < _pf_deadline):
                    self._stop.wait(0.25)
                if self._stopped():
                    break
            # WARNING: recovery must never leave less than _ASR_RECOVER_KEEP_LEAD_SEC of lead over playback.
            if self._attached:
                # WARNING: no recovery until this session has measured its own chunk speed; tune-in chunks must stay short and fast.
                if self._chunk_rtf_ema is None:
                    _slack = 0.0
                else:
                    _slack = lead + length - self._chunk_rtf_ema * length - _ASR_RECOVER_KEEP_LEAD_SEC
            else:
                _slack = _ASR_RECOVER_MAX_SEC
            _recover_budget = max(0.0, min(_ASR_RECOVER_MAX_SEC,
                                           _ASR_RECOVER_MAX_FRAC_OF_CHUNK * length, _slack))
            self._busy = True
            t0 = time.monotonic()
            _slot_t0 = t0
            took = None
            wav_fd, wav_path = tempfile.mkstemp(prefix="live_asr_", suffix=".wav")
            os.close(wav_fd)
            if self._attached and self.ready and chunks_done > 0:
                _gate_hold = _whisper_gate_hold_seconds(
                    lead, (self._chunk_rtf_ema * length) if self._chunk_rtf_ema is not None
                    else _LIVE_CAPTION_BIG_CHUNK_DEFAULT_ESTIMATE_SEC)
                _gate_skip = False
            elif not self._attached and _perf_governor is not None:
                _gate_hold = _perf_governor.WHISPER_PREFETCH_GATE_WAIT_SECONDS
                _gate_skip = True
            else:
                _gate_hold, _gate_skip = 0.0, False
            try:
                with _asr_slot(f"live_caption({base_name})", hold_sec=_gate_hold, skip_if_held=_gate_skip) as _may_run:
                    if not _may_run:
                        continue
                    if self._stopped():
                        return
                    _slot_t0 = time.monotonic()
                    status = _asr_extract_chunk(self.file_path, pos, audio_end - pos, wav_path,
                                                accurate_seek_margin_sec=_ASR_LIVE_ACCURATE_SEEK_MARGIN_SEC,
                                                audio_filter=_ASR_LIVE_AUDIO_FILTER)
                    if status != "ok":
                        log.warning("[LIVE ASR] %s ffmpeg returned %r for the audio at %.1f-%.1fs; "
                                    "that window was not captioned", base_name, status, pos, audio_end)
                    if status == "failed":
                        failures += 1
                        if failures >= _LIVE_CAPTION_MAX_FAILURES:
                            self.status = ASR_REASON_EXTRACT_FAILED
                            return
                        pos += length
                        continue
                    if status == "ok" and not self._stopped():
                        try:
                            segments = _asr_transcribe_chunk(
                                model, wav_path, self.language, _ASR_LIVE_BEAM_SIZE,
                                initial_prompt=prompt, temperature=0.0, should_stop=self._stopped,
                                own_len=length, recover_budget_sec=_recover_budget)
                        except Exception as e:
                            log.warning("[LIVE ASR] transcription failed for %s: %s", base_name, e)
                            failures += 1
                            if failures >= _LIVE_CAPTION_MAX_FAILURES:
                                self.status = ASR_REASON_TRANSCRIBE_FAILED
                                return
                            pos += length
                            continue
                        if self._stopped():
                            return
                        failures = 0
                        new_cues, drops = _asr_filter_cues(segments, pos, pos + length, at_file_end, prompt=prompt,
                                                           music_secs=getattr(segments, "music_secs", None))
                        _asr_learn_names(name_stats, new_cues.words)
                        _names_now = _asr_learned_names(name_stats)
                        if _names_now != learned_names:
                            learned_names = _names_now
                            prompt = _asr_prompt_with_names(base_prompt, learned_names)
                            log.info("[LIVE ASR] %s name hint for the next chunks: %s", base_name, prompt)
                        _next_carry = list(new_cues.carry)
                        if carry_prev and carry_end is not None and abs(pos - carry_end) < 0.05:
                            _rescued = [c for c in carry_prev if not _asr_carry_is_covered(c, new_cues)]
                            if _rescued:
                                log.info("[LIVE ASR] %s carried %d cue(s) over the chunk boundary at %.1fs that the "
                                         "next chunk did not hear: %s", base_name, len(_rescued), pos,
                                         " | ".join("%.2f-%.2f %r" % (s, e, t) for s, e, t in _rescued))
                                new_cues = list(new_cues) + _rescued
                        carry_prev, carry_end = _next_carry, pos + length
                        if self._last_sample_end is not None:
                            _gap_amt = pos - self._last_sample_end
                            if abs(_gap_amt) > 0.05 and _gap_amt > 0:
                                log.warning(
                                    "[CAPTION SAMPLE GAP] %s %.2fs of show audio between %.2fs and %.2fs "
                                    "was never sampled by either chunk -- any speech in that stretch "
                                    "cannot appear in captions.",
                                    base_name, _gap_amt, self._last_sample_end, pos,
                                )
                        self._last_sample_end = pos + length
                        if new_cues:
                            with self.lock:
                                _asr_merge_cues(self.cues, new_cues)
                        _music_flags = getattr(segments, "music_secs", None)
                        if _music_flags:
                            with self.lock:
                                _music_new = _asr_music_cues(_music_flags, pos, length, self.cues)
                                if _music_new:
                                    _asr_merge_cues(self.cues, _music_new)
                            if _music_new:
                                log.info("[LIVE ASR] %s music sign added for %.1fs of music-like audio with no speech "
                                         "(%d cue(s)) at %.1f-%.1fs", base_name,
                                         sum(c[1] - c[0] for c in _music_new), len(_music_new),
                                         _music_new[0][0], _music_new[-1][1])
                        took = time.monotonic() - _slot_t0
                        _slot_wait = _slot_t0 - t0
                        log.info(
                            "[LIVE ASR] %s pos=%.1fs len=%.1fs audio=%.1fs took=%.1fs wait=%.1fs lead=%s ready=%s "
                            "chunk=%s(est_30s=%.1fs) vad=%s islands/%.1fs speech cues=+%d "
                            "dropped(next=%d empty=%d not_speech=%d hallucination=%d) "
                            "recovered=%d/%d(%.1fs)",
                            base_name, pos, length, (audio_end - pos), took, _slot_wait,
                            ("n/a" if now is None else f"{lead:.1f}s"), self.ready,
                            ("BIG" if use_big_chunk else ("first" if (chunks_done == 0 and self._attached) else "startup")), est_big_chunk_sec,
                            getattr(segments, "vad_islands", "?"), getattr(segments, "vad_speech_sec", 0.0),
                            len(new_cues), drops["next_chunk"], drops["empty"], drops["not_speech"],
                            drops["hallucination"], getattr(segments, "recover_ok", 0),
                            getattr(segments, "recover_tried", 0), getattr(segments, "recover_sec", 0.0))
                        if self._attached:
                            _pb = self._playback_sec()
                            for _s, _e, _t in new_cues:
                                if _e < _pb:
                                    self._late_cues += 1
                                elif _s < _pb:
                                    self._partial_late += 1
                        _short = (audio_end - pos) - (getattr(segments, "audio_secs", 0.0) or (audio_end - pos))
                        if _short > 0.5:
                            log.warning("[LIVE ASR] %s the audio for %.1f-%.1fs came back %.1fs short; "
                                        "the end of that window cannot be captioned",
                                        base_name, pos, audio_end, _short)
            finally:
                self._busy = False
                try:
                    if os.path.isfile(wav_path):
                        os.remove(wav_path)
                except OSError:
                    pass

            if took is not None and chunks_done > 0 and length > 0:
                _measured_ratio = took / length
                self._chunk_rtf_ema = (
                    _measured_ratio if self._chunk_rtf_ema is None
                    else 0.4 * _measured_ratio + 0.6 * self._chunk_rtf_ema)

            chunks_done += 1
            self._chunks_done = chunks_done
            pos += length
            self._trim_cues()

        if not self._stopped():
            self.status = ASR_REASON_SUCCESS


# ============================================================================
# SECTION: Media errors
# ============================================================================


class NoAudioStreamError(Exception):
    def __init__(self, file_path, reason="no audio track"):
        super().__init__(file_path)
        self.file_path = file_path
        self.reason = reason


class UnplayableMediaError(Exception):
    def __init__(self, file_path, reason="confirmed decode failure"):
        super().__init__(file_path)
        self.file_path = file_path
        self.reason = reason


class LoudnessProbeTimeoutError(Exception):
    def __init__(self, file_path):
        super().__init__(file_path)
        self.file_path = file_path


# ============================================================================
# SECTION: End-of-media detection
# ============================================================================
def check_media_truly_ended(
    state,
    pos_ms,
    length_ms,
    now_ms,
    last_play_started_at_ms,
    tracking,
    min_play_time_ms=1500,
    debounce_ms=200,
    proximity_ms=2000,
):
    """Decide whether a terminal engine read really means the file finished rather than a spurious state glitch."""
    # WARNING: unknown length or position counts as NOT near the end, never as about to finish.
    tracking["_media_end_debounce_time"] = debounce_ms

    if state == "error":
        tracking["_media_end_check_start_time"] = 0
        if now_ms - last_play_started_at_ms > min_play_time_ms:
            tracking["_media_end_confirmed"] = True
            return True, "error"
        tracking["_media_end_confirmed"] = False
        return False, None

    if state in ("ended", "stopped"):
        _start = tracking.get("_media_end_check_start_time", 0)
        if not _start:
            tracking["_media_end_check_start_time"] = now_ms
            tracking.setdefault("_media_end_glitch_since_ms", now_ms)
            tracking["_media_end_confirmed"] = False
            return False, None
        if now_ms - _start >= debounce_ms:
            if now_ms - last_play_started_at_ms > min_play_time_ms:
                # WARNING: a confirmed finish needs real position/length data or a sustained multi-second stuck streak.
                _near_end = False
                if length_ms and length_ms > 0 and pos_ms is not None and pos_ms >= 0:
                    _near_end = pos_ms >= (length_ms - proximity_ms)
                _glitch_since = tracking.get("_media_end_glitch_since_ms") or now_ms
                _stuck_ms = now_ms - _glitch_since
                if _near_end or _stuck_ms >= max(1500, debounce_ms + proximity_ms):
                    tracking["_media_end_confirmed"] = True
                    tracking["_media_end_glitch_since_ms"] = 0
                    return True, "ended"
                tracking["_media_end_check_start_time"] = 0
                tracking["_media_end_confirmed"] = False
                return False, None
        tracking["_media_end_confirmed"] = False
        return False, None

    tracking["_media_end_check_start_time"] = 0
    tracking["_media_end_glitch_since_ms"] = 0
    tracking["_media_end_confirmed"] = False
    return False, None


# ============================================================================
# SECTION: Loudness constants & ffmpeg helpers
# ============================================================================


LOUDNESS_GAIN_CEILING = 10.0
# WARNING: this is the loudness safety ceiling; LOUDNESS_GAIN_FLOOR_STANDARD never loosens it.
LOUDNESS_GAIN_FLOOR = 0.1
# WARNING: true-peak safety is a separate clamp; this floor never lets a file exceed TRUE_PEAK_CEILING_DBTP.
LOUDNESS_GAIN_FLOOR_STANDARD = 0.5

MUSIC_GAIN_FLOOR = LOUDNESS_GAIN_FLOOR

LOUDNESS_CONVERGENCE_DB = 1.5


def _converge_clamped_gain(gain, clamped_gain, peak_capped, ceiling,
                           attenuation_floor=None):
    """Nudge an already target/peak-clamped gain by up to LOUDNESS_CONVERGENCE_DB of extra convergence."""
    _factor = 10 ** (LOUDNESS_CONVERGENCE_DB / 20.0)
    if gain > 1.0 and peak_capped:
        return min(clamped_gain * _factor, ceiling)
    if gain < 1.0:
        _floor = LOUDNESS_GAIN_FLOOR_STANDARD if attenuation_floor is None else attenuation_floor
        return max(_floor, clamped_gain / _factor)
    return clamped_gain

TRUE_PEAK_CEILING_DBTP = -2.0

TARGET_LUFS = -20.5

MUSIC_TARGET_LUFS = -18.5

COMPRESSOR_ATTACK_MS = 25.0
COMPRESSOR_RELEASE_MS = 800.0
# VALUES CHANGE HERE
COMPRESSOR_THRESHOLD_DB = -18.0
COMPRESSOR_RATIO = 2.0
COMPRESSOR_KNEE_DB = 6.0
COMPRESSOR_MAKEUP_DB = 4.0

COMPRESSOR_BAKE_VERSION = 1


_CORRUPT_MEDIA_SIGNATURES = (
    "invalid data found when processing input",
    "moov atom not found",
    "error while decoding stream",
    "could not find codec parameters",
    "invalid nal unit size",
    "header missing",
    "truncated file",
    "corrupt input packet",
    "eof while searching for stream header",
)


def _stderr_indicates_corrupt(stderr_text):
    if not stderr_text:
        return False
    _lower = stderr_text.lower()
    return any(sig in _lower for sig in _CORRUPT_MEDIA_SIGNATURES)


def _stderr_has_audio_stream(stderr_text):
    if not stderr_text:
        return False
    return bool(re.search(r"Stream #\d+:\d+[^\n]*:\s*Audio:", stderr_text))


def _probe_duration_seconds_fast(file_path):
    # WARNING: the fallback ffmpeg call below must stay separate from the picture-box scan and the loudness decodes; do not fold them together.
    if not file_path or not _cached_path_exists(file_path):
        return None
    _facts = _get_header_facts(file_path)
    if _facts is not None:
        return _facts.get("duration") if _facts.get("ok") else None
    try:
        import subprocess, re
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            )
        with _fast_disk_io_slot(f"ffprobe_duration({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if not m:
            return None
        h, mi, s = m.groups()
        total = int(h) * 3600 + int(mi) * 60 + float(s)
        return total if total > 0 else None
    except Exception:
        return None


def _lufs_window_ffmpeg(file_path, target_lufs, start_sec=None, length_sec=None, timeout=90,
                         semaphore=None, semaphore_timeout=None):
    """Run one loudness window after the governor admits it; the work is in _lufs_window_ffmpeg_inner."""
    _lane = "music_loudness" if semaphore is _MUSIC_LOUDNESS_PROBE_SEMAPHORE else "loudness_and_bake"
    _prio = semaphore is _LOUDNESS_PRIORITY_SEMAPHORE or semaphore is _LOUDNESS_PARALLEL_SEMAPHORE
    _win_t0 = time.time()
    with _gov_gate(_lane, priority=_prio, timeout=semaphore_timeout,
                   label=f"loudness({os.path.basename(file_path)})") as _admitted:
        if not _admitted:
            return None, None, None, "", True
        _gate_wait = time.time() - _win_t0
        _result = _lufs_window_ffmpeg_inner(file_path, target_lufs, start_sec=start_sec, length_sec=length_sec,
                                             timeout=timeout, semaphore=semaphore,
                                             semaphore_timeout=semaphore_timeout)
        _total = time.time() - _win_t0
        if _prio or _total >= LOUDNESS_WINDOW_LOG_MIN_SECONDS:
            log.info("[LOUDNESS WINDOW TIMING] %s start=%s len=%s gate_wait=%.1fs total=%.1fs reading=%s%s",
                     os.path.basename(file_path),
                     "whole" if start_sec is None else f"{start_sec:.0f}s",
                     "whole" if length_sec is None else f"{length_sec:.0f}s",
                     _gate_wait, _total,
                     "none" if _result[0] is None else f"{_result[0]:.1f}LUFS",
                     " TIMEOUT" if _result[4] else "")
        return _result


def _lufs_window_ffmpeg_inner(file_path, target_lufs, start_sec=None, length_sec=None, timeout=90,
                               semaphore=None, semaphore_timeout=None):
    import subprocess, re, json
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    cmd = [_resolve_ffmpeg_path(), "-nostdin"]
    if start_sec is not None:
        cmd += ["-ss", f"{max(0.0, start_sec):.2f}"]
    cmd += ["-i", file_path, "-vn"]
    if length_sec is not None:
        cmd += ["-t", f"{length_sec:.2f}"]
    cmd += ["-af", f"loudnorm=I={target_lufs}:TP={TRUE_PEAK_CEILING_DBTP}:LRA=11:print_format=json",
            "-f", "null", "-"]
    _probe_t0 = time.time()
    _sem = semaphore or _LOUDNESS_PROBE_SEMAPHORE
    if semaphore_timeout is not None:
        if not _sem.acquire(timeout=semaphore_timeout):
            _elapsed = time.time() - _probe_t0
            _media_warning(
                "loudness_semaphore_timeout",
                "[LOUDNESS PROBE WINDOW] Gave up after %.1fs waiting for a free "
                "probe slot (limit %ss) at start_sec=%s: %s",
                _elapsed, semaphore_timeout, start_sec, os.path.basename(file_path),
            )
            return None, None, None, "", True
    else:
        _sem.acquire()
    try:
        _yield_to_live_playback("loudness probe")
        _probe_t0 = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL,
                **popen_kwargs
            )
        finally:
            _sem.release()
    except subprocess.TimeoutExpired:
        _elapsed = time.time() - _probe_t0
        _media_warning(
            "loudness_window_timeout",
            "[LOUDNESS PROBE WINDOW] Timeout after %.1fs (limit %ss) "
            "at start_sec=%s: %s",
            _elapsed, timeout, start_sec, os.path.basename(file_path),
        )
        _record_drive_op_latency(file_path, _elapsed * 1000.0, audio_sec=length_sec)
        return None, None, None, "", True
    _record_drive_op_latency(file_path, (time.time() - _probe_t0) * 1000.0, audio_sec=length_sec)
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr)
    if not match:
        return None, None, None, result.stderr, False
    try:
        stats = json.loads(match.group(0))
        _measured_i = float(stats.get("input_i", 0))
        try:
            _measured_tp = float(stats.get("input_tp"))
        except (TypeError, ValueError):
            _measured_tp = None
        try:
            _measured_lra = float(stats.get("input_lra"))
        except (TypeError, ValueError):
            _measured_lra = None
        return _measured_i, _measured_tp, _measured_lra, result.stderr, False
    except Exception:
        return None, None, None, result.stderr, False


def _combine_lufs_readings(readings):
    import math
    if not readings:
        return None
    powers = [10 ** (r / 10.0) for r in readings]
    avg_power = sum(powers) / len(powers)
    return 10 * math.log10(avg_power) if avg_power > 0 else -70.0


def _combine_true_peak_readings(tp_readings):
    if not tp_readings:
        return None
    return max(tp_readings)


# ============================================================================
# SECTION: Loudness scanning & gain
# ============================================================================


# VALUES CHANGE HERE (sample windows that disagree by more than this many LU use the quieter reading)
LOUDNESS_WINDOW_DISAGREE_LU = 3.0

# VALUES CHANGE HERE (gain scan sampling: files up to the whole-file limit are scanned whole; longer files get
LOUDNESS_WHOLE_FILE_MAX_SECONDS = 120.0
LOUDNESS_SAMPLE_WINDOW_SECONDS = 30.0
LOUDNESS_SAMPLE_CENTERS = (0.25, 0.5, 0.75)

# VALUES CHANGE HERE (a loudness window is logged when it took at least this long, or when it belongs to the playing file's first scan)
LOUDNESS_WINDOW_LOG_MIN_SECONDS = 5.0

LOUDNESS_WIDE_LRA_FLAG_LU = 15.0


def _lufs_scan_window(duration_sec):
    """Return (start_sec, length_sec) covering the whole file."""
    return 0.0, duration_sec


def _reconcile_lufs_readings(pass_readings):
    """Reconcile 2 or 3 window readings into one file-level LUFS basis."""
    if len(pass_readings) >= 3:
        r1, r2, r3 = pass_readings[0], pass_readings[1], pass_readings[2]
        pairs = [(r1, r2), (r1, r3), (r2, r3)]
        for a, b in pairs:
            if abs(a - b) <= LOUDNESS_WINDOW_DISAGREE_LU:
                return _combine_lufs_readings([a, b])
        return min(r1, r2, r3)
    if len(pass_readings) == 2:
        r1, r2 = pass_readings
        if abs(r1 - r2) > LOUDNESS_WINDOW_DISAGREE_LU:
            return min(r1, r2)
        return _combine_lufs_readings(pass_readings)
    if len(pass_readings) == 1:
        return pass_readings[0]
    return None


def _lufs_sample_windows(duration_sec):
    """Return the (start_sec, length_sec) sample windows for a file, or None when the whole file is scanned."""
    if not duration_sec or duration_sec <= LOUDNESS_WHOLE_FILE_MAX_SECONDS:
        return None
    length = min(LOUDNESS_SAMPLE_WINDOW_SECONDS, duration_sec)
    max_start = max(0.0, duration_sec - length)
    return [(max(0.0, min(duration_sec * frac - length / 2.0, max_start)), length)
            for frac in LOUDNESS_SAMPLE_CENTERS]


def _sample_lufs_readings(file_path, target_lufs, semaphore=None, chunk_timeout=180, semaphore_timeout=None,
                          parallel_windows=False, duration_out=None):
    """Measure a file and return one file-level loudness basis; parallel_windows runs all sample windows at once, and duration_out receives the length measured for the scan as "duration_sec"."""
    duration_sec = _probe_duration_seconds_fast(file_path)
    if (duration_out is not None and duration_sec and duration_sec > 0
            and not _duration_looks_implausible(file_path, duration_sec)):
        duration_out["duration_sec"] = float(duration_sec)
    readings = []
    tp_readings = []
    lra_readings = []
    stderr_for_stream_checks = ""
    any_timeout = False

    windows = _lufs_sample_windows(duration_sec)
    if windows is None:
        if duration_sec:
            start, length = _lufs_scan_window(duration_sec)
        else:
            start, length = None, None
        m_i, m_tp, m_lra, stderr_for_stream_checks, timed_out = _lufs_window_ffmpeg(
            file_path, target_lufs, start_sec=start,
            length_sec=(None if length is None else max(1.0, length)), timeout=chunk_timeout,
            semaphore=semaphore, semaphore_timeout=semaphore_timeout)
        any_timeout = timed_out
        if m_i is not None:
            readings.append(m_i)
        if m_tp is not None:
            tp_readings.append(m_tp)
        if m_lra is not None:
            lra_readings.append(m_lra)
        return readings, tp_readings, lra_readings, stderr_for_stream_checks, any_timeout

    def _one(window):
        return _lufs_window_ffmpeg(
            file_path, target_lufs, start_sec=float(window[0]), length_sec=max(1.0, window[1]),
            timeout=chunk_timeout,
            semaphore=(_LOUDNESS_PARALLEL_SEMAPHORE if parallel_windows else semaphore),
            semaphore_timeout=semaphore_timeout)

    if parallel_windows:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(windows), thread_name_prefix="loud-win") as pool:
            results = list(pool.map(_one, windows))
    else:
        results = [_one(w) for w in windows]

    pass_readings = []
    timeouts = []
    for m_i, m_tp, m_lra, chunk_stderr, timed_out in results:
        if not stderr_for_stream_checks and chunk_stderr:
            stderr_for_stream_checks = chunk_stderr
        timeouts.append(timed_out)
        if m_i is not None:
            pass_readings.append(m_i)
        if m_tp is not None:
            tp_readings.append(m_tp)
        if m_lra is not None:
            lra_readings.append(m_lra)
    any_timeout = all(timeouts) if timeouts else False
    _basis = _reconcile_lufs_readings(pass_readings)
    if _basis is not None:
        readings.append(_basis)
    return readings, tp_readings, lra_readings, stderr_for_stream_checks, any_timeout


def get_media_loudness_gain(file_path, target_lufs=TARGET_LUFS, *args, **kwargs):
    """Wrapper for the loudness scan: a scan on the priority semaphore is the playing file's first scan and runs in a priority scope, never held back by the throttle."""
    if kwargs.get("semaphore") is _LOUDNESS_PRIORITY_SEMAPHORE and _perf_governor is not None:
        with _perf_governor.priority_scope():
            return _get_media_loudness_gain_impl(file_path, target_lufs, *args, **kwargs)
    return _get_media_loudness_gain_impl(file_path, target_lufs, *args, **kwargs)


def _get_media_loudness_gain_impl(file_path, target_lufs=TARGET_LUFS,
                             raise_on_no_audio=False,
                             treat_fallback_as_unknown=False,
                             raise_on_unplayable=False,
                             raise_on_timeout=False,
                             shortfall_out=None,
                             db=None,
                             semaphore=None,
                             chunk_timeout=180,
                             semaphore_timeout=None,
                             attenuation_floor=None,
                             parallel_windows=False,
                             duration_out=None):
    _fallback = None if treat_fallback_as_unknown else 1.0
    _ceiling = LOUDNESS_GAIN_CEILING
    _atten_floor = LOUDNESS_GAIN_FLOOR_STANDARD if attenuation_floor is None else attenuation_floor

    if not file_path or not _cached_path_exists(file_path):
        return _fallback

    norm_p = os.path.normpath(os.path.abspath(file_path))
    if not _loudness_inflight.try_claim(norm_p):
        return _fallback

    try:
        import subprocess
        _facts = _get_header_facts(file_path)
        if (_facts and _facts.get("ok") and _facts.get("width")
                and (_facts.get("duration") or 0) > 0 and not _facts.get("has_audio")):
            _media_warning(
                "loudness_no_audio",
                "[LOUDNESS PROBE] No audio stream in %s (header shows none; no scan run)",
                os.path.basename(file_path),
            )
            if raise_on_no_audio:
                raise NoAudioStreamError(file_path)
            return _fallback
        readings, tp_readings, lra_readings, stderr_for_stream_checks, any_timeout = _sample_lufs_readings(
            file_path, target_lufs, semaphore=semaphore, chunk_timeout=chunk_timeout,
            semaphore_timeout=semaphore_timeout, parallel_windows=parallel_windows,
            duration_out=duration_out)

        if not readings:
            if any_timeout:
                _media_warning(
                    "loudness_timeout",
                    "[LOUDNESS PROBE] Timed out measuring %s",
                    os.path.basename(file_path),
                )
                if raise_on_timeout:
                    raise LoudnessProbeTimeoutError(file_path)
                return _fallback
            if raise_on_unplayable and _stderr_indicates_corrupt(stderr_for_stream_checks):
                _corrupt_reason = "confirmed decode failure"
                for _sig in _CORRUPT_MEDIA_SIGNATURES:
                    if _sig in stderr_for_stream_checks.lower():
                        _corrupt_reason = _sig
                        break
                _media_warning(
                    "playability_unplayable",
                    "[PLAYABILITY PROBE] Confirmed unplayable (%s) in %s",
                    _corrupt_reason, os.path.basename(file_path),
                )
                raise UnplayableMediaError(file_path, reason=_corrupt_reason)
            if not _stderr_has_audio_stream(stderr_for_stream_checks):
                _media_warning(
                    "loudness_no_audio",
                    "[LOUDNESS PROBE] No audio stream in %s",
                    os.path.basename(file_path),
                )
                if raise_on_no_audio:
                    raise NoAudioStreamError(file_path)
                return _fallback
            _media_warning(
                "loudness_no_stats",
                "[LOUDNESS PROBE] No loudnorm stats returned for %s",
                os.path.basename(file_path),
            )
            return _fallback

        measured_i = _combine_lufs_readings(readings)
        if measured_i <= -70.0:
            _media_warning(
                "loudness_silent",
                "[LOUDNESS PROBE] Effectively silent (%.1f LUFS) in %s",
                measured_i, os.path.basename(file_path),
            )
            if raise_on_no_audio:
                raise NoAudioStreamError(file_path, reason=f"measured as silent ({measured_i:.1f} LUFS)")
            return _fallback
        if measured_i > 0.0:
            _media_warning(
                "loudness_unreliable",
                "[LOUDNESS PROBE] Unreliable reading (%.1f LUFS) for %s",
                measured_i, os.path.basename(file_path),
            )
            return _fallback
        gain_db = target_lufs - measured_i
        gain = 10 ** (gain_db / 20.0)
        _clamped_gain = max(_atten_floor, min(gain, _ceiling))
        combined_tp = _combine_true_peak_readings(tp_readings)
        _peak_capped = False
        if combined_tp is not None:
            _peak_headroom_db = TRUE_PEAK_CEILING_DBTP - combined_tp
            _peak_gain_cap = 10 ** (_peak_headroom_db / 20.0)
            if _peak_gain_cap < _clamped_gain:
                _peak_floor = 1.0 if gain > 1.0 else LOUDNESS_GAIN_FLOOR
                _clamped_gain = max(_peak_floor, _peak_gain_cap)
                _peak_capped = True
        _clamped_gain = _converge_clamped_gain(
            gain, _clamped_gain, _peak_capped, _ceiling, attenuation_floor=_atten_floor)
        _max_lra = max(lra_readings) if lra_readings else None
        _wide_lra_flag = _max_lra is not None and _max_lra >= LOUDNESS_WIDE_LRA_FLAG_LU
        if _wide_lra_flag:
            _media_warning(
                "loudness_wide_lra",
                "[LOUDNESS PROBE] %s: wide loudness range (%.1f LU) on the "
                "scanned window(s) -- integrated/true-peak targets are met, "
                "but this is a signal (not a measurement) that a short-term "
                "moment could still land well above -20 LUFS/3s",
                os.path.basename(file_path), _max_lra,
            )
        if shortfall_out is not None:
            shortfall_out.update({
                "measured_lufs": measured_i,
                "achieved_lufs": measured_i + (20 * math.log10(_clamped_gain)),
                "peak_capped": _peak_capped,
                "clamped_gain": _clamped_gain,
                "measured_lra": _max_lra,
                "wide_lra_flag": _wide_lra_flag,
            })
        if gain != _clamped_gain:
            _achieved_lufs = measured_i + (20 * math.log10(_clamped_gain))
            _shortfall_db = target_lufs - _achieved_lufs
            _side = "BOOST" if gain > 1.0 else "ATTENUATION"
            _reason = "TRUE PEAK" if _peak_capped else "LUFS"
            if shortfall_out is not None and (
                _side == "BOOST" or (_side == "ATTENUATION" and _reason == "TRUE PEAK")
            ):
                shortfall_out.update({
                    "reason": _reason,
                    "side": _side,
                    "shortfall_db": _shortfall_db,
                    "needed_gain": gain,
                    "clamped_gain": _clamped_gain,
                })
        return _clamped_gain
    except (NoAudioStreamError, UnplayableMediaError, LoudnessProbeTimeoutError):
        raise
    except FileNotFoundError:
        _media_warning(
            "ffmpeg_missing_loudness",
            "[LOUDNESS PROBE] ffmpeg not found (checked bundled locations and PATH) -- "
            "loudness normalization is disabled until it is available.",
        )
        return _fallback
    except subprocess.TimeoutExpired:
        _media_warning(
            "loudness_timeout",
            "[LOUDNESS PROBE] Timed out measuring %s",
            os.path.basename(file_path),
        )
        return _fallback
    except Exception as e:
        _media_warning(
            "loudness_error",
            "[LOUDNESS PROBE ERROR] %s: %s",
            os.path.basename(file_path), e,
        )
        return _fallback
    finally:
        _loudness_inflight.release(norm_p)


def get_music_loudness_gain(file_path, raise_on_no_audio=False,
                             treat_fallback_as_unknown=False,
                             raise_on_unplayable=False,
                             raise_on_timeout=False,
                             shortfall_out=None,
                             db=None,
                             duration_out=None):
    """Music counterpart of get_media_loudness_gain: targets MUSIC_TARGET_LUFS and uses the dedicated music probe semaphore; duration_out receives the file's length from the same scan."""
    return get_media_loudness_gain(
        file_path, target_lufs=MUSIC_TARGET_LUFS,
        duration_out=duration_out,
        raise_on_no_audio=raise_on_no_audio,
        treat_fallback_as_unknown=treat_fallback_as_unknown,
        raise_on_unplayable=raise_on_unplayable,
        raise_on_timeout=raise_on_timeout,
        shortfall_out=shortfall_out,
        db=db,
        semaphore=_MUSIC_LOUDNESS_PROBE_SEMAPHORE,
        attenuation_floor=MUSIC_GAIN_FLOOR,
    )


def get_pair_loudness_gain(path1, path2, target_lufs=TARGET_LUFS,
                            raise_on_no_audio=False,
                            treat_fallback_as_unknown=False,
                            raise_on_unplayable=False,
                            raise_on_timeout=False,
                            shortfall_out=None,
                            db=None):
    _fallback = None if treat_fallback_as_unknown else 1.0
    _ceiling = LOUDNESS_GAIN_CEILING

    if not path1 or not path2 or not _cached_path_exists(path1) or not _cached_path_exists(path2):
        return _fallback

    norm_p1 = os.path.normpath(os.path.abspath(path1))
    norm_p2 = os.path.normpath(os.path.abspath(path2))
    if not _loudness_inflight.try_claim(norm_p1, norm_p2):
        return _fallback

    try:
        import subprocess
        readings = []
        tp_readings = []
        lra_readings = []
        stderr_for_stream_checks = ""
        any_timeout = False
        for _fp in (path1, path2):
            _r, _tp_r, _lra_r, _stderr, _timed_out = _sample_lufs_readings(_fp, target_lufs)
            readings.extend(_r)
            tp_readings.extend(_tp_r)
            lra_readings.extend(_lra_r)
            if not stderr_for_stream_checks and _stderr:
                stderr_for_stream_checks = _stderr
            any_timeout = any_timeout or _timed_out
            if not _r and _stderr and _stderr_indicates_corrupt(_stderr) and raise_on_unplayable:
                _media_warning(
                    "pair_unplayable",
                    "[PLAYABILITY PROBE] Confirmed unplayable in pair half: %s",
                    os.path.basename(_fp),
                )
                raise UnplayableMediaError(_fp, reason="confirmed decode failure")

        if not readings:
            if any_timeout:
                _media_warning(
                    "pair_loudness_timeout",
                    "[LOUDNESS PROBE] Timed out measuring pair: %s + %s",
                    os.path.basename(path1), os.path.basename(path2),
                )
                if raise_on_timeout:
                    raise LoudnessProbeTimeoutError(path1)
                return _fallback
            if raise_on_unplayable and _stderr_indicates_corrupt(stderr_for_stream_checks):
                raise UnplayableMediaError(path1, reason="confirmed decode failure")
            if not _stderr_has_audio_stream(stderr_for_stream_checks):
                _media_warning(
                    "pair_no_audio",
                    "[LOUDNESS PROBE] No audio stream in pair: %s + %s",
                    os.path.basename(path1), os.path.basename(path2),
                )
                if raise_on_no_audio:
                    raise NoAudioStreamError(path1)
                return _fallback
            _media_warning(
                "pair_no_stats",
                "[LOUDNESS PROBE] No loudnorm stats returned for pair: %s + %s",
                os.path.basename(path1), os.path.basename(path2),
            )
            return _fallback

        measured_i = _combine_lufs_readings(readings)
        if measured_i <= -70.0:
            _media_warning(
                "pair_silent",
                "[LOUDNESS PROBE] Effectively silent (%.1f LUFS) in pair: %s + %s",
                measured_i, os.path.basename(path1), os.path.basename(path2),
            )
            if raise_on_no_audio:
                raise NoAudioStreamError(path1, reason=f"measured as silent ({measured_i:.1f} LUFS)")
            return _fallback
        if measured_i > 0.0:
            _media_warning(
                "pair_unreliable",
                "[LOUDNESS PROBE] Unreliable reading (%.1f LUFS) for pair: %s + %s",
                measured_i, os.path.basename(path1), os.path.basename(path2),
            )
            return _fallback
        gain_db = target_lufs - measured_i
        gain = 10 ** (gain_db / 20.0)
        _clamped_gain = max(LOUDNESS_GAIN_FLOOR_STANDARD, min(gain, _ceiling))
        combined_tp = _combine_true_peak_readings(tp_readings)
        _peak_capped = False
        if combined_tp is not None:
            _peak_headroom_db = TRUE_PEAK_CEILING_DBTP - combined_tp
            _peak_gain_cap = 10 ** (_peak_headroom_db / 20.0)
            if _peak_gain_cap < _clamped_gain:
                _peak_floor = 1.0 if gain > 1.0 else LOUDNESS_GAIN_FLOOR
                _clamped_gain = max(_peak_floor, _peak_gain_cap)
                _peak_capped = True
        _clamped_gain = _converge_clamped_gain(gain, _clamped_gain, _peak_capped, _ceiling)
        _max_lra = max(lra_readings) if lra_readings else None
        if _max_lra is not None and _max_lra >= LOUDNESS_WIDE_LRA_FLAG_LU:
            _media_warning(
                "pair_wide_lra",
                "[LOUDNESS PROBE] pair %s + %s: wide loudness range (%.1f LU) "
                "on the scanned window(s) -- signal only, not a short-term measurement",
                os.path.basename(path1), os.path.basename(path2), _max_lra,
            )
        if gain != _clamped_gain:
            _achieved_lufs = measured_i + (20 * math.log10(_clamped_gain))
            _shortfall_db = target_lufs - _achieved_lufs
            _side = "BOOST" if gain > 1.0 else "ATTENUATION"
            _reason = "TRUE PEAK" if _peak_capped else "LUFS"
            if shortfall_out is not None and (
                _side == "BOOST" or (_side == "ATTENUATION" and _reason == "TRUE PEAK")
            ):
                shortfall_out.update({
                    "reason": _reason,
                    "side": _side,
                    "shortfall_db": _shortfall_db,
                    "needed_gain": gain,
                    "clamped_gain": _clamped_gain,
                })
        return _clamped_gain
    except (NoAudioStreamError, UnplayableMediaError, LoudnessProbeTimeoutError):
        raise
    except FileNotFoundError:
        _media_warning(
            "ffmpeg_missing_loudness",
            "[LOUDNESS PROBE] ffmpeg not found (checked bundled locations and PATH) -- "
            "loudness normalization is disabled until it is available.",
        )
        return _fallback
    except subprocess.TimeoutExpired:
        _media_warning(
            "pair_loudness_timeout",
            "[LOUDNESS PROBE] Timed out measuring pair: %s + %s",
            os.path.basename(path1), os.path.basename(path2),
        )
        return _fallback
    except Exception as e:
        _media_warning(
            "pair_loudness_error",
            "[LOUDNESS PROBE ERROR] pair %s + %s: %s",
            os.path.basename(path1), os.path.basename(path2), e,
        )
        return _fallback
    finally:
        _loudness_inflight.release(norm_p1, norm_p2)


# ============================================================================
# SECTION: Compressor bake
# ============================================================================


def _run_ffmpeg_interruptible(cmd, timeout, should_abort=None, poll_interval=0.4):
    """Like subprocess.run() but polls should_abort() every poll_interval seconds and terminates ffmpeg early when it returns True."""
    import subprocess, collections
    _FFResult = collections.namedtuple("_FFResult", ["returncode", "stdout", "stderr"])
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, **popen_kwargs
    )
    _start = time.monotonic()
    while True:
        try:
            out, err = proc.communicate(timeout=poll_interval)
            return _FFResult(proc.returncode, out, err), False
        except subprocess.TimeoutExpired:
            if should_abort is not None and should_abort():
                proc.terminate()
                try:
                    proc.communicate(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                return None, True
            if (time.monotonic() - _start) >= timeout:
                proc.kill()
                try:
                    proc.communicate(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout)


def _compressor_bake_cache_path(file_path):
    """Return where the baked (pre-compressed) copy of file_path lives, whether or not it exists yet."""
    import hashlib as _hashlib
    file_path = os.path.abspath(file_path)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    _path_key = _hashlib.sha1(file_path.encode("utf-8", "ignore")).hexdigest()[:16]
    _cache_dir = os.path.join(resolve_loudness_dir(), "compressor_cache")
    try:
        os.makedirs(_cache_dir, exist_ok=True)
    except OSError as e:
        log.warning("[COMPRESSOR BAKE] could not create cache dir %s: %s", _cache_dir, e)
    return os.path.join(
        _cache_dir, f"{_path_key}__{stem}.v{COMPRESSOR_BAKE_VERSION}.compressed.wav",
    )


def get_cached_compressor_bake(file_path):
    """Return the baked copy of file_path for the current COMPRESSOR_BAKE_VERSION if it exists, else None."""
    if not file_path or not _cached_path_exists(file_path):
        return None
    _out_path = _compressor_bake_cache_path(file_path)
    if _cached_path_exists(_out_path):
        return _out_path
    return None


_compressor_bake_inflight = _InFlightGuard()


def bake_compressor_pass(file_path, timeout=180, should_abort=None):
    """Render a copy of file_path through the same compressor curve VLC applies live."""
    # WARNING: COMPRESSOR_* must stay in lockstep with retro_tv_emulator.py's _compressor_filter_args().
    if not file_path or not _cached_path_exists(file_path):
        return None
    norm_p = os.path.normpath(os.path.abspath(file_path))
    if not _compressor_bake_inflight.try_claim(norm_p):
        return None
    try:
        out_path = _compressor_bake_cache_path(norm_p)
        if _cached_path_exists(out_path):
            return out_path
        tmp_path = out_path + ".tmp"
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        _threshold_lin = 10 ** (COMPRESSOR_THRESHOLD_DB / 20.0)
        _makeup_lin = 10 ** (COMPRESSOR_MAKEUP_DB / 20.0)
        af = (
            f"acompressor=threshold={_threshold_lin:.6f}:"
            f"ratio={COMPRESSOR_RATIO}:"
            f"attack={COMPRESSOR_ATTACK_MS}:"
            f"release={COMPRESSOR_RELEASE_MS}:"
            f"knee={COMPRESSOR_KNEE_DB}:"
            f"makeup={_makeup_lin:.6f}"
        )
        with _gov_gate("loudness_and_bake", label=f"compressor_bake({os.path.basename(norm_p)})"), _LOUDNESS_PROBE_SEMAPHORE:
            result, aborted = _run_ffmpeg_interruptible(
                [_resolve_ffmpeg_path(), "-nostdin", "-hide_banner", "-y",
                 "-i", norm_p, "-vn", "-af", af, "-f", "wav", tmp_path],
                timeout, should_abort=should_abort,
            )
        if aborted:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None
        if (result.returncode != 0 or not os.path.exists(tmp_path)
                or os.path.getsize(tmp_path) == 0):
            _stderr_tail = (getattr(result, "stderr", "") or "").strip().splitlines()[-6:]
            _media_warning(
                "compressor_bake_failed",
                "[COMPRESSOR BAKE] failed for %s (ffmpeg exit %s): %s",
                os.path.basename(norm_p), getattr(result, "returncode", "?"),
                " | ".join(_stderr_tail) if _stderr_tail else "(no stderr captured)",
            )
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None
        os.replace(tmp_path, out_path)
        log.info("[COMPRESSOR BAKE] %s -> %s", os.path.basename(norm_p), os.path.basename(out_path))
        return out_path
    except Exception as e:
        _media_warning(
            "compressor_bake_error",
            "[COMPRESSOR BAKE ERROR] %s: %s", os.path.basename(file_path), e,
        )
        return None
    finally:
        _compressor_bake_inflight.release(norm_p)


# ============================================================================
# SECTION: Database
# ============================================================================


class RetroDatabase:

    # ========================================================================
    # SECTION: Setup
    # ========================================================================

    def __init__(self, detected_aspect_ratio=None):
        self.base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        self.config_path = os.path.join(self.base_dir, "retro_config.json")
        self._detected_aspect_ratio = detected_aspect_ratio

        self.playback_active_check = None
        
        self.themes = {
            "DefaultBlueGreen": {"ui": (0, 255, 128), "bg": (10, 15, 30), "text": (255, 255, 255)},
            "Classic Amber": {"ui": (255, 170, 0), "bg": (15, 10, 0), "text": (240, 220, 180)}
        }
        
        self.config = {}
        self.channels_db = {}
        self.audio_files = [] 
        self.ready_flag = False
        self._save_lock = threading.Lock()
        self._save_request_lock = threading.Lock()
        self._save_generation = 0

        self.channels_path = self.config_path
        self._main_channels_cache = {}
        self._channels_lock = threading.Lock()
        self._channels_save_generation = 0

        # WARNING: the config and channels writers must never target the same physical file.
        self.runtime_path = os.path.join(self.base_dir, "playback_runtime.json")
        self._runtime_lock = threading.Lock()
        self._runtime_save_generation = 0


        # WARNING: keep the remote overlay separate from normal save/load so a remote snapshot is never persisted over the user's channels.
        self._remote_schedule_lock = threading.RLock()
        self._remote_schedule_active = False
        self._remote_local_channels_snapshot = None
        self._remote_locked_channels = set()
        self.remote_playback = {}

        threading.Thread(target=self.load_settings_async, daemon=True).start()

    # ========================================================================
    # SECTION: Config & channels loading
    # ========================================================================

    def _apply_config_defaults(self, raw_config):
        """Fill in defaults/migrations on top of a raw 'config' dict loaded from disk."""
        config = dict(raw_config)
        config["is_muted"] = False
        if "screen_mode" not in config:
            config["screen_mode"] = "normal"
        if "dvd_screen_mode" not in config:
            config["dvd_screen_mode"] = "normal"
        if "game_screen_mode" not in config:
            config["game_screen_mode"] = "normal"
        if "pixel_orbiter_enabled" not in config:
            config["pixel_orbiter_enabled"] = True
        if not config.get("_static_color_migrated"):
            if config.get("static_noise_color") == "white":
                config["static_noise_color"] = "grey"
            config["_static_color_migrated"] = True
        # WARNING: the Aspect Ratio settings have no controls. The picture box follows each video's own shape, a screen that is not 16:9 gets the 16:9 picture boxed inside it, and Bezel mode sets 4:3 itself at boot, so the saved value is forced back to 16:9 here (this also clears a 4:3 the old setting saved), and the old DVD and Games Aspect Ratio keys are dropped from the config.
        config["aspect_ratio"] = "16:9"
        for _gone_key in ("dvd_aspect_ratio", "game_aspect_ratio", "_aspect_ratio_before_bezel", "_dvd_aspect_ratio_before_bezel", "_game_aspect_ratio_before_bezel"):
            config.pop(_gone_key, None)
        config["_aspect_screen_forcing_migrated"] = True
        return config

    def _build_channels_db(self, raw_channels):
        """Build a full channels_db dict (all 40 channel slots, every field defaulted) from a raw 'channels' dict."""
        channels_db = {}
        _VIS_STYLE_MIGRATIONS = {
            "KaleidoscopeEdge": "PrismShards",
            "KaleidoscopeBloom": "LiquidChrome",
            "StainedGlass": "LiquidChrome",
            "RibbonWave": "PulseRings",
            "ChromaWave": "WarpTunnel",
            "AuroraPeaks": "GlowBraid",
        }
        _SCHED_MODE_MIGRATIONS = {
            "random":       "random_slots",
            "round_robin":  "one_slot",
            "two_episodes": "two_slots",
        }
        for i in range(5, 45):
            ch_str = str(i).zfill(2)
            incoming_profile = raw_channels.get(ch_str, {})

            saved_schedules = incoming_profile.get("schedules", {})

            _raw_vis_style = incoming_profile.get("visualizer_style", "Random")
            _vis_style = _VIS_STYLE_MIGRATIONS.get(_raw_vis_style, _raw_vis_style)

            _explicit_block_mode = incoming_profile.get("block_mode")
            if _explicit_block_mode in ("full_day", "segmented", "six_block"):
                _block_mode = _explicit_block_mode
            else:
                _six_keys = ("12-4am", "4-8am", "8-12pm", "12-4pm", "4-8pm", "8-12am")
                _seg_keys = ("Morning", "Evening", "Night")
                _has_six = any(saved_schedules.get(k) for k in _six_keys)
                _has_seg = any(saved_schedules.get(k) for k in _seg_keys)
                _has_fullday = bool(saved_schedules.get("Full Day"))
                if _has_six and not _has_fullday:
                    _block_mode = "six_block"
                elif _has_seg and not _has_fullday:
                    _block_mode = "segmented"
                else:
                    _block_mode = "full_day"

            channels_db[ch_str] = {
                "active":            incoming_profile.get("active", True if ch_str == "05" else False),
                "is_visualizer":     incoming_profile.get("is_visualizer", False),
                "visualizer_style":  _vis_style,
                "name":              incoming_profile.get("name", f"STATION {ch_str}"),

                "scheduling_mode": _SCHED_MODE_MIGRATIONS.get(
                    incoming_profile.get("scheduling_mode", "random_slots"),
                    incoming_profile.get("scheduling_mode", "random_slots"),
                ),

                "episode_order_mode": incoming_profile.get("episode_order_mode", "sequential"),

                "block_mode": _block_mode,

                "pair_threshold_minutes": incoming_profile.get("pair_threshold_minutes", 15),

                "commercials_enabled":   incoming_profile.get("commercials_enabled", False),
                "commercial_placement":  incoming_profile.get("commercial_placement", "interrupt_half_hour"),
                "intros_enabled":        incoming_profile.get("intros_enabled", False),
                "outros_enabled":        incoming_profile.get("outros_enabled", False),

                "schedules": {
                    "Morning":      saved_schedules.get("Morning",      []),
                    "Evening":      saved_schedules.get("Evening",      []),
                    "Night":        saved_schedules.get("Night",        []),
                    "Full Day":     saved_schedules.get("Full Day",     []),
                    "12-4am":       saved_schedules.get("12-4am",       []),
                    "4-8am":        saved_schedules.get("4-8am",        []),
                    "8-12pm":       saved_schedules.get("8-12pm",       []),
                    "12-4pm":       saved_schedules.get("12-4pm",       []),
                    "4-8pm":        saved_schedules.get("4-8pm",        []),
                    "8-12am":       saved_schedules.get("8-12am",       []),
                    "Commercials":  saved_schedules.get("Commercials",  []),
                    "Intros":       saved_schedules.get("Intros",       []),
                    "Outros":       saved_schedules.get("Outros",       []),
                    "Music Tracks": saved_schedules.get("Music Tracks", []),
                    "Marathon":     saved_schedules.get("Marathon",     []),
                },

                "holiday_schedules": {
                    "halloween":    incoming_profile.get("holiday_schedules", {}).get("halloween",    []),
                    "christmas":    incoming_profile.get("holiday_schedules", {}).get("christmas",    []),
                    "valentine":    incoming_profile.get("holiday_schedules", {}).get("valentine",    []),
                    "thanksgiving": incoming_profile.get("holiday_schedules", {}).get("thanksgiving", []),
                    "new_year":     incoming_profile.get("holiday_schedules", {}).get("new_year",     []),
                },

                "playback_log": incoming_profile.get("playback_log", {}),

                "playback_anchor": incoming_profile.get("playback_anchor", {}),

                "marathon_anchor_epoch": incoming_profile.get("marathon_anchor_epoch"),
            }
        return channels_db

    def _start_resume_probe(self, channels_db):
        """No-op kept so existing call sites stay valid."""
        return

    def _load_channels_from_path(self, path):
        """Load channels_db from `path`, independent of whatever config_path is doing."""
        raw_channels = {}
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                raw_channels = data.get("channels", {})
            except Exception as load_err:
                _media_warning(
                    "channels_load_error",
                    "Channel source read failed path=%s: %s",
                    path, load_err,
                )
                raw_channels = {}
        elif path != self.config_path:
            log.error("Active channel source is missing; using defaults: %s", path)
        self.channels_db = self._build_channels_db(raw_channels)
        self._start_resume_probe(self.channels_db)

    def reload_channels_from(self, path):
        """Flushes channels_db to the current channels_path, repoints channels_path at `path` and reloads channels_db from it."""
        try:
            target_path = os.fspath(path)
        except (TypeError, ValueError):
            log.error("Channel redirect rejected invalid path: %r", path)
            return False

        old_path = self.channels_path
        self.save_channels(force=True)
        self.channels_path = target_path
        self._load_channels_from_path(target_path)
        log.info(
            "Channel source redirected old=%s new=%s exists=%s",
            old_path, target_path, os.path.exists(target_path),
        )
        return True

    @staticmethod
    def _remote_channel_has_content(channel):
        """Return whether a remote channel actually owns scheduled content."""
        if not isinstance(channel, dict):
            return False
        schedules = channel.get("schedules", {})
        if isinstance(schedules, dict) and any(
                isinstance(items, (list, tuple)) and len(items) > 0
                for items in schedules.values()):
            return True
        holidays = channel.get("holiday_schedules", {})
        if isinstance(holidays, dict) and any(
                isinstance(items, (list, tuple)) and len(items) > 0
                for items in holidays.values()):
            return True
        return False

    def _fold_live_unlocked_into_snapshot_locked(self):
        """Fold live unlocked channel state into the snapshot."""
        # WARNING: caller must hold _remote_schedule_lock.
        snap = self._remote_local_channels_snapshot
        if not self._remote_schedule_active or snap is None:
            return
        for key, rec in list(self.channels_db.items()):
            if key in self._remote_locked_channels or not isinstance(rec, dict):
                continue
            clean = copy.deepcopy(rec)
            clean.pop("server_locked", None)
            snap[key] = clean

    def apply_remote_schedule(self, channels, playback=None):
        """Apply a live Server schedule without destroying local ownership."""
        if not isinstance(channels, dict):
            return False

        normalized = {}
        for key, value in channels.items():
            try:
                channel_num = int(str(key))
            except (TypeError, ValueError):
                continue
            if 5 <= channel_num <= 44 and isinstance(value, dict):
                normalized[str(channel_num).zfill(2)] = value

        with self._remote_schedule_lock:
            if not self._remote_schedule_active:
                self._remote_local_channels_snapshot = copy.deepcopy(self.channels_db)
                self._remote_schedule_active = True

            self._fold_live_unlocked_into_snapshot_locked()
            local_channels = self._remote_local_channels_snapshot or {}
            locked = {
                channel_key for channel_key, channel in normalized.items()
                if self._remote_channel_has_content(channel)
            }
            remote_channels = self._build_channels_db(normalized)
            merged = {}

            for channel_num in range(5, 45):
                channel_key = str(channel_num).zfill(2)
                if channel_key in locked:
                    merged[channel_key] = copy.deepcopy(remote_channels[channel_key])
                    merged[channel_key]["server_locked"] = True
                else:
                    local_record = local_channels.get(channel_key)
                    if local_record is None:
                        local_record = self._build_channels_db({}).get(channel_key, {})
                    merged[channel_key] = copy.deepcopy(local_record)
                    merged[channel_key].pop("server_locked", None)

            self.channels_db = merged
            self._remote_locked_channels = locked
            _mode = str((playback or {}).get("screen_mode", "")).strip().lower() if isinstance(playback, dict) else ""
            self.remote_playback = ({"screen_mode": _mode}
                                    if _mode in ("normal", "stretch", "crop", "panscan") else {})
        return True

    def remote_screen_mode_for(self, ch_key):
        """Return the Server's screen mode if ch_key is a Server-streamed channel, else None."""
        try:
            mode = self.remote_playback.get("screen_mode") if self._remote_schedule_active else None
            if mode and str(ch_key).zfill(2) in self._remote_locked_channels:
                return mode
        except Exception:
            pass
        return None

    def disconnect_remote_schedule(self):
        """Restore the exact local channel state captured at connect time."""
        with self._remote_schedule_lock:
            if not self._remote_schedule_active:
                return False
            self._fold_live_unlocked_into_snapshot_locked()
            restored = copy.deepcopy(self._remote_local_channels_snapshot or {})
            self.channels_db = restored
            self._remote_schedule_active = False
            self._remote_local_channels_snapshot = None
            self._remote_locked_channels = set()
            self.remote_playback = {}
        self._start_resume_probe(self.channels_db)
        return True

    # ========================================================================
    # SECTION: Server slot management
    # ========================================================================

    SERVER_SLOT_IDS = tuple(f"server{i}" for i in range(1, 7))
    SERVER_SLOT_SCHEMA_VERSION = 1

    def get_server_slot_path(self, slot_id):
        """Resolve a slot_id ('server1'..'server6') to <base_dir>/server/.json/serverN/retro.config.json."""
        return os.path.join(self.base_dir, "server", ".json", slot_id, "retro.config.json")

    def list_server_slots(self):
        """Return [(slot_id, name), ...] for every named slot in insertion order."""
        server_slots = self.config.get("server_slots", {})
        return [(slot_id, name) for slot_id, name in server_slots.items()]

    def next_free_server_slot(self):
        """First unused slot id (1-6), or None if all 6 are already taken."""
        used = set(self.config.get("server_slots", {}).keys())
        for slot_id in self.SERVER_SLOT_IDS:
            if slot_id not in used:
                return slot_id
        return None

    def create_server_slot(self, name):
        """Create a new named server slot with an empty channels file and save the name-to-slot mapping in config."""
        slot_id = self.next_free_server_slot()
        if slot_id is None:
            return None
        path = self.get_server_slot_path(slot_id)
        try:
            dir_name = os.path.dirname(path)
            os.makedirs(dir_name, exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(
                    {"schema_version": self.SERVER_SLOT_SCHEMA_VERSION, "channels": {}},
                    f, indent=4,
                )
            os.replace(tmp_path, path)
        except Exception as create_err:
            log.error("Slot file create failed path=%s: %s", path, create_err)
            return None
        server_slots = dict(self.config.get("server_slots", {}))
        server_slots[slot_id] = name
        self.config["server_slots"] = server_slots
        self.save_config()
        return slot_id

    def rename_server_slot(self, slot_id, new_name):
        """Edit a slot's display label only -- never touches its folder or file."""
        server_slots = dict(self.config.get("server_slots", {}))
        if slot_id not in server_slots:
            return False
        server_slots[slot_id] = new_name
        self.config["server_slots"] = server_slots
        self.save_config()
        return True

    def delete_server_slot(self, slot_id):
        """Remove a named slot."""
        server_slots = dict(self.config.get("server_slots", {}))
        if slot_id not in server_slots:
            return False

        slot_path = self.get_server_slot_path(slot_id)
        if self.channels_path == slot_path:
            self.reload_channels_from(self.config_path)
            self.config["active_server_slot"] = None

        del server_slots[slot_id]
        self.config["server_slots"] = server_slots
        self.save_config()

        slot_dir = os.path.dirname(slot_path)
        server_root = os.path.join(self.base_dir, "server", ".json")
        try:
            if os.path.isdir(slot_dir) and os.path.commonpath(
                [os.path.abspath(slot_dir), os.path.abspath(server_root)]
            ) == os.path.abspath(server_root):
                shutil.rmtree(slot_dir)
        except Exception as delete_err:
            _media_warning(
                "server_slot_delete_error",
                "[DB ERR] Could not delete server slot folder %s: %s",
                slot_dir, delete_err,
            )
        return True

    # ========================================================================
    # SECTION: Make active / leave server mode
    # ========================================================================

    def make_server_slot_active(self, slot_id):
        """Flush channels_db to the current channels_path, then redirect channels_db to slot_id's own file."""
        server_slots = self.config.get("server_slots", {})
        if slot_id not in server_slots:
            return False
        self.reload_channels_from(self.get_server_slot_path(slot_id))
        self.config["active_server_slot"] = slot_id
        self.save_config()
        return True

    def leave_server_slot(self):
        """Redirect channels_db back to the main project's channel data."""
        if self.config.get("active_server_slot") is None and self.channels_path == self.config_path:
            return
        self.reload_channels_from(self.config_path)
        self.config["active_server_slot"] = None
        self.save_config()

    # ========================================================================
    # SECTION: Settings load & save
    # ========================================================================

    def load_settings_async(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                self.config = self._apply_config_defaults(data.get("config", {}))
                raw_channels = data.get("channels", {})

                self._main_channels_cache = raw_channels

                if self.channels_path == self.config_path:
                    self.channels_db = self._build_channels_db(raw_channels)
                    self._load_playback_anchors()
                    self.ready_flag = True
                    self._start_resume_probe(self.channels_db)
                else:
                    self.ready_flag = True
                    self._load_channels_from_path(self.channels_path)

                return
            except Exception as load_err:
                _media_warning(
                    "settings_load_error",
                    "[DB ERROR] Settings reload recovery pass triggered: %s",
                    load_err,
                )
                self._recovered_from_load_error = True
                try:
                    if os.path.exists(self.config_path):
                        backup_path = self.config_path + f".corrupt-{int(time.time())}.bak"
                        shutil.copy2(self.config_path, backup_path)
                except Exception as backup_err:
                    _media_warning(
                        "settings_backup_error",
                        "[DB ERROR] Could not back up unparsed settings file: %s",
                        backup_err,
                    )

        self.config = {
            "global_volume": 70,
            "current_theme": "DefaultBlueGreen",
            "show_controls_on_launch": True,
            "tv_guide_enabled": True,
            "start_on_boot": False,
            "kiosk_mode_enabled": False,
            "server_mode_enabled": False,
            "is_muted": False,
            "audio_stereo": True,
            "brightness": 50,
            "contrast": 50,
            "color": 50,
            "sharpness": 50,
            "tint": 50,
            "vlc_decode_res": "sd",
            "theme_ui_color": "Green",
            "theme_bg_hue": 220,
            "theme_ui_hue": 140,
            "transition_type": "black",
            "letterbox_color": "black",
            "static_noise_color": "white",
            "_static_color_migrated": True,
            "volume_bar_style": "1",
            "aspect_ratio": "16:9",
            "_aspect_screen_forcing_migrated": True,
            "fake_43_border_enabled": False,
            "bezel_style": 1,
            "fake_43_scanlines_enabled": False,
            "fake_static_noise_enabled": False,
            "loudness_scan_enabled": True,
            "screen_mode": "normal",
            "dvd_screen_mode": "normal",
            "pixel_orbiter_enabled": True,
            "burn_in_protection_skip_confirm": False,
            "console_bezel_skip_confirm": False,
            "program_bezel_skip_confirm": False,
            "pixel_orbiter_skip_confirm": False,
            "ch03_screensaver_skip_confirm": False,
            "screen_offset_x": 0,
            "screen_offset_y": 0,
            "screen_edge_left": 0,
            "screen_edge_right": 0,
            "screen_edge_top": 0,
            "screen_edge_bottom": 0,
            "remote_bindings": {},
        }

        self._main_channels_cache = {}
        self.channels_db = self._build_channels_db({})
        if not getattr(self, "_recovered_from_load_error", False):
            self.save_settings()
        self.ready_flag = True
        self._start_resume_probe(self.channels_db)

    def _next_save_generation(self):
        with self._save_request_lock:
            self._save_generation += 1
            return self._save_generation

    def _next_channels_save_generation(self):
        with self._save_request_lock:
            self._channels_save_generation += 1
            return self._channels_save_generation

    def _snapshot_config(self):
        """Return a stable copy of just the config tree."""
        for attempt in range(8):
            try:
                return copy.deepcopy(self.config)
            except RuntimeError as snapshot_err:
                if attempt == 7:
                    _media_warning(
                        "config_snapshot_retries_exhausted",
                        "[DB ERR] Config snapshot retries exhausted (%s); "
                        "skipping this write.",
                        snapshot_err,
                    )
                    return None
                time.sleep(0.01 * (attempt + 1))
            except Exception as snapshot_err:
                _media_warning(
                    "config_snapshot_error",
                    "[DB ERR] Config snapshot failed: %s; skipping this write.",
                    snapshot_err,
                )
                return None

    def _snapshot_channels(self):
        """Return a stable copy of just the channels tree."""
        for attempt in range(8):
            try:
                # WARNING: save_settings() must keep saving the user's local records, never streamed records or transient server_locked flags.
                with self._remote_schedule_lock:
                    self._fold_live_unlocked_into_snapshot_locked()
                    source = (
                        self._remote_local_channels_snapshot
                        if self._remote_schedule_active
                        and self._remote_local_channels_snapshot is not None
                        else self.channels_db
                    )
                    return copy.deepcopy(source)
            except RuntimeError as snapshot_err:
                if attempt == 7:
                    _media_warning(
                        "channels_snapshot_retries_exhausted",
                        "[DB ERR] Channels snapshot retries exhausted (%s); "
                        "skipping this write.",
                        snapshot_err,
                    )
                    return None
                time.sleep(0.01 * (attempt + 1))
            except Exception as snapshot_err:
                _media_warning(
                    "channels_snapshot_error",
                    "[DB ERR] Channels snapshot failed: %s; skipping this write.",
                    snapshot_err,
                )
                return None

    def _snapshot_settings(self):
        """Return a stable copy of both mutable settings trees."""
        config_snapshot = self._snapshot_config()
        if config_snapshot is None:
            return None
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot is None:
            return None
        return {"config": config_snapshot, "channels": channels_snapshot}

    def _snapshot_playback_anchors(self):
        """Return a stable copy of just the per-channel playback_anchor data."""
        for attempt in range(8):
            try:
                with self._channels_lock:
                    return {
                        ch_key: copy.deepcopy(ch.get("playback_anchor"))
                        for ch_key, ch in self.channels_db.items()
                        if ch.get("playback_anchor")
                    }
            except RuntimeError as snapshot_err:
                if attempt == 7:
                    _media_warning(
                        "runtime_snapshot_retries_exhausted",
                        "[DB ERR] Playback-anchor snapshot retries exhausted (%s); "
                        "skipping this write.",
                        snapshot_err,
                    )
                    return None
                time.sleep(0.01 * (attempt + 1))
            except Exception as snapshot_err:
                _media_warning(
                    "runtime_snapshot_error",
                    "[DB ERR] Playback-anchor snapshot failed: %s; skipping this write.",
                    snapshot_err,
                )
                return None

    def _next_runtime_save_generation(self):
        with self._save_request_lock:
            self._runtime_save_generation += 1
            return self._runtime_save_generation

    def _write_runtime_snapshot(self, anchors_snapshot, generation):
        """Atomically write runtime_path unless this write is stale."""
        try:
            with self._runtime_lock:
                with self._save_request_lock:
                    if generation != self._runtime_save_generation:
                        return
                tmp_path = self.runtime_path + ".tmp"
                self._atomic_write_json(tmp_path, self.runtime_path, {"anchors": anchors_snapshot})
        except Exception as save_err:
            log.error("Runtime (playback_anchor) write failed path=%s: %s", self.runtime_path, save_err)

    def save_playback_anchor(self, force=False):
        """Persist just the runtime playback_anchor data."""
        if not getattr(self, "ready_flag", False):
            return
        anchors_snapshot = self._snapshot_playback_anchors()
        if anchors_snapshot is None:
            return
        generation = self._next_runtime_save_generation()
        if force:
            self._write_runtime_snapshot(anchors_snapshot, generation)
        else:
            threading.Thread(
                target=self._write_runtime_snapshot,
                args=(anchors_snapshot, generation),
                daemon=True,
            ).start()

    def _load_playback_anchors(self):
        """Merge runtime_path's anchors back into an already-built channels_db."""
        if not os.path.exists(self.runtime_path):
            return
        try:
            with open(self.runtime_path, "r") as f:
                data = json.load(f)
            anchors = data.get("anchors", {})
            for ch_key, anchor in anchors.items():
                if ch_key in self.channels_db and isinstance(anchor, dict):
                    self.channels_db[ch_key]["playback_anchor"] = anchor
        except Exception as load_err:
            _media_warning(
                "runtime_load_error",
                "[DB ERR] Playback-anchor runtime load failed path=%s: %s (continuing "
                "without prior anchors -- never fatal, since anchors are never trusted "
                "to resume playback across a restart anyway)",
                self.runtime_path, load_err,
            )

    @staticmethod
    def _atomic_write_json(tmp_path, final_path, data):
        """Write `data` to `tmp_path` as JSON, fsync, then atomically rename onto `final_path`."""
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)

    def _write_config_snapshot(self, config_snapshot, generation):
        """Atomically write config_path unless this write is stale."""
        try:
            with self._save_lock:
                with self._save_request_lock:
                    if generation != self._save_generation:
                        return
                tmp_path = self.config_path + ".tmp"
                self._atomic_write_json(
                    tmp_path, self.config_path,
                    {"config": config_snapshot, "channels": self._main_channels_cache},
                )
        except Exception as save_err:
            log.error("Main config write failed path=%s: %s", self.config_path, save_err)

    def _write_channels_snapshot(self, channels_snapshot, path, generation):
        """Atomically write `path` (whichever file channels_path currently points at) unless this write is stale."""
        try:
            with self._channels_lock:
                with self._save_request_lock:
                    if generation != self._channels_save_generation:
                        return
                if path == self.config_path:
                    self._main_channels_cache = channels_snapshot
                    with self._save_lock:
                        tmp_path = self.config_path + ".tmp"
                        self._atomic_write_json(
                            tmp_path, self.config_path,
                            {"config": copy.deepcopy(self.config), "channels": channels_snapshot},
                        )
                else:
                    dir_name = os.path.dirname(path)
                    if dir_name:
                        os.makedirs(dir_name, exist_ok=True)
                    tmp_path = path + ".tmp"
                    self._atomic_write_json(tmp_path, path, {"channels": channels_snapshot})
        except Exception as save_err:
            log.error("Channel source write failed path=%s: %s", path, save_err)

    def save_config(self):
        """Save just db.config to config_path."""
        if not getattr(self, "ready_flag", False):
            return
        config_snapshot = self._snapshot_config()
        if config_snapshot is None:
            return
        self._write_config_snapshot(config_snapshot, self._next_save_generation())

    def save_channels(self, force=False):
        """Save db.channels_db to wherever channels_path currently points."""
        if not getattr(self, "ready_flag", False):
            return
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot is None:
            return
        generation = self._next_channels_save_generation()
        path = self.channels_path
        if force:
            self._write_channels_snapshot(channels_snapshot, path, generation)
        else:
            threading.Thread(
                target=self._write_channels_snapshot,
                args=(channels_snapshot, path, generation),
                daemon=True,
            ).start()

    def save_settings(self):
        """Save everything -- config AND channels."""
        if not getattr(self, "ready_flag", False):
            return
        if self.channels_path == self.config_path:
            snapshot = self._snapshot_settings()
            if snapshot is None:
                return
            self._main_channels_cache = snapshot["channels"]
            self._write_config_snapshot(snapshot["config"], self._next_save_generation())
        else:
            self.save_config()
            self.save_channels(force=True)

    def save_settings_async(self, force=False):
        """Save config and channels, async by default."""
        if not getattr(self, "ready_flag", False):
            return
        if self.channels_path == self.config_path:
            snapshot = self._snapshot_settings()
            if snapshot is None:
                return
            self._main_channels_cache = snapshot["channels"]
            generation = self._next_save_generation()
            if force:
                self._write_config_snapshot(snapshot["config"], generation)
            else:
                threading.Thread(
                    target=self._write_config_snapshot,
                    args=(snapshot["config"], generation),
                    daemon=True,
                ).start()
        else:
            self.save_config()
            self.save_channels(force=force)

    def factory_reset(self):

        game_ch_flag   = self.config.get("game_channel_enabled", True)
        guide_ch_flag  = self.config.get("tv_guide_enabled", True)
        theme_name     = self.config.get("current_theme", "DefaultBlueGreen")
        theme_bg_color = self.config.get("theme_bg_color", "Blue")
        theme_ui_color = self.config.get("theme_ui_color", "Green")
        theme_bg_hue   = self.config.get("theme_bg_hue", 220)
        theme_ui_hue   = self.config.get("theme_ui_hue", 140)
        menu_opacity   = self.config.get("menu_opacity", 50)
        transition_type = self.config.get("transition_type", "black")
        remote_bindings = self.config.get("remote_bindings", {})

        consoles_enabled      = self.config.get("consoles_enabled", {})
        console_rom_dir       = self.config.get("console_rom_dir", {})
        active_bios           = self.config.get("active_bios", {})
        console_profiles      = self.config.get("console_profiles", {})
        all_profiles_sync     = self.config.get("all_profiles_sync", False)
        all_profiles_sync_tgt = self.config.get("all_profiles_sync_target", 1)
        console_scroll_style  = self.config.get("console_scroll_style", "carousel")
        console_splash_enabled = self.config.get("console_splash_enabled", {})
        console_splash_path   = self.config.get("console_splash_path", {})
        dvd_splash_enabled    = self.config.get("dvd_splash_enabled", False)
        dvd_splash_path       = self.config.get("dvd_splash_path", "")

        self.config = {
            "global_volume": 70,
            "current_theme": theme_name,
            "show_controls_on_launch": True,
            "tv_guide_enabled": guide_ch_flag,
            "game_channel_enabled": game_ch_flag,
            "start_on_boot": False,
            "kiosk_mode_enabled": False,
            "server_mode_enabled": False,
            "is_muted": False,
            "audio_stereo": True,
            "brightness": 50,
            "contrast": 50,
            "color": 50,
            "sharpness": 50,
            "tint": 50,
            "vlc_decode_res": "sd",
            "theme_bg_color": theme_bg_color,
            "theme_ui_color": theme_ui_color,
            "theme_bg_hue": theme_bg_hue,
            "theme_ui_hue": theme_ui_hue,
            "menu_opacity": menu_opacity,
            "transition_type": transition_type,
            "aspect_ratio": "16:9",
            "_aspect_screen_forcing_migrated": True,
            "fake_43_border_enabled": False,
            "bezel_style": 1,
            "fake_43_scanlines_enabled": False,
            "fake_static_noise_enabled": False,
            "loudness_scan_enabled": True,
            "screen_mode": "normal",
            "dvd_screen_mode": "normal",
            "pixel_orbiter_enabled": True,
            "burn_in_protection_skip_confirm": False,
            "console_bezel_skip_confirm": False,
            "program_bezel_skip_confirm": False,
            "pixel_orbiter_skip_confirm": False,
            "ch03_screensaver_skip_confirm": False,
            "screen_offset_x": 0,
            "screen_offset_y": 0,
            "screen_edge_left": 0,
            "screen_edge_right": 0,
            "screen_edge_top": 0,
            "screen_edge_bottom": 0,
            "remote_bindings": remote_bindings,
            "consoles_enabled": consoles_enabled,
            "console_rom_dir": console_rom_dir,
            "active_bios": active_bios,
            "console_profiles": console_profiles,
            "all_profiles_sync": all_profiles_sync,
            "all_profiles_sync_target": all_profiles_sync_tgt,
            "console_scroll_style": console_scroll_style,
            "console_splash_enabled": console_splash_enabled,
            "console_splash_path": console_splash_path,
            "dvd_splash_enabled": dvd_splash_enabled,
            "dvd_splash_path": dvd_splash_path,
        }

        self.channels_db = self._build_channels_db({})

        self.save_settings()


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "ASR_REASON_EXTRACT_FAILED", "ASR_REASON_INTERRUPTED", "ASR_REASON_NO_AUDIO",
    "ASR_REASON_NO_SPEECH", "ASR_REASON_SUCCESS", "ASR_REASON_TRANSCRIBE_FAILED",
    "ASR_REASON_UNAVAILABLE", "AUDIO_DISPLAY_EXTS", "CAPTION_SIDECAR_EXTS",
    "COMMERCIAL_BUNDLE_CEILINGS", "COMMERCIAL_MAX_SECONDS", "COMPRESSOR_ATTACK_MS",
    "COMPRESSOR_BAKE_VERSION", "COMPRESSOR_KNEE_DB", "COMPRESSOR_MAKEUP_DB", "COMPRESSOR_RATIO",
    "COMPRESSOR_RELEASE_MS", "COMPRESSOR_THRESHOLD_DB", "CRT_WARP_ACTIVE", "DRIVE_CDROM",
    "FAST_DISK_IO_MAX_CONCURRENT", "LEFTOVERS_TITLE", "LIVE_CAPTION_CHUNK_SEC",
    "LIVE_CAPTION_LEAD_SEC", "LIVE_CAPTION_MAX_LEAD_SEC", "LIVE_CAPTION_READY_DEADLINE_SEC",
    "LOUDNESS_CONVERGENCE_DB", "LOUDNESS_WINDOW_DISAGREE_LU",
    "LOUDNESS_GAIN_CEILING", "LOUDNESS_GAIN_FLOOR", "LOUDNESS_GAIN_FLOOR_STANDARD",
    "LOUDNESS_PROBE_MAX_CONCURRENT", "LOUDNESS_WIDE_LRA_FLAG_LU",
    "LiveCaptionSession",
    "LoudnessProbeTimeoutError", "MOVIES_PER_BLOCK", "MOVIE_HEAVY_RATIO",
    "MOVIE_LENGTH_THRESHOLD_SECONDS", "MOVIE_ROTATION_KEY", "MUSIC_GAIN_FLOOR",
    "MUSIC_LOUDNESS_PROBE_MAX_CONCURRENT", "MUSIC_TARGET_LUFS", "NoAudioStreamError",
    "PATH_MISSING", "PATH_PRESENT", "PATH_UNKNOWN", "RANDOM_REPEAT_HOLD_BACK",
    "RetroDatabase",
    "SELECTION_BORDER_WIDTH", "SHORTCUT_EXTENSIONS", "SHORT_EPISODE_PAIRING_VERSION",
    "SIX_HOUR_BLOCKS", "SIX_HOUR_BLOCK_NAMES", "TARGET_LUFS", "TRUE_PEAK_CEILING_DBTP",
    "UnplayableMediaError", "VIDEO_DISPLAY_EXTS", "apply_video_effects",
    "asr_known_unavailable", "bake_compressor_pass", "bezel_style_count",
    "build_block_timeline", "build_ordered_tracks", "build_show_rotation",
    "check_media_truly_ended", "clean_display_title_for_ext", "clean_music_display_title",
    "clean_video_display_title", "commercial_bundle_ceiling", "dedupe_duplicate_episode_files",
    "dedupe_same_name_different_extension", "discover_bezel_style_files", "dvd_disc_mrl",
    "fast_forward_show_pos", "generate_captions_via_asr_chunked", "get_cached_compressor_bake",
    "get_disc_info", "get_media_duration", "get_media_loudness_gain",
    "get_media_video_dimensions", "get_music_loudness_gain", "get_pair_loudness_gain",
    "group_shows_flexible", "is_dvd_disc_source", "is_movie_track", "is_optical_drive_root",
    "is_shortcut_path", "iter_files_resolving_shortcuts", "migrate_show_positions",
    "note_random_episode_played", "pair_movie_parts", "pair_short_episodes",
    "parse_caption_cues", "filename_says_captions_baked_in", "probe_file_level_recovery", "request_media_shutdown", "resolve_bezel_dir", "resolve_caption_source",
    "resolve_captions_dir", "resolve_dir_entry", "resolve_loudness_dir", "resolve_main_dir",
    "resolve_shortcut_chain", "resolve_shortcut_target", "resolve_track_duration",
    "resolve_whisper_model_dir", "set_caption_memory_refusal_handler", "set_live_playback_priority", "split_movies_and_series",
    "stable_series_id", "strip_shortcut_display_suffix", "track_duration_is_estimated",
]
