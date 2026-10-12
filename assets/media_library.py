"""Persistent Probe Library: a SQLite-backed cache of what is known about each media file.

Owns: the library database and its storage location, content-identity keys,
the combined ffmpeg header probe, picture-box (baked-in bars) detection and
storage, and migration from the legacy config-dict caches.
Does not own: playback, scheduling, or applying a stored picture box to the
video (the app reads the stored result).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re as _re
try:
    import sqlite3
except ImportError:
    sqlite3 = None
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ============================================================================
# SECTION: Storage location & database
# ============================================================================

_library_dir_ready = False


def resolve_media_library_dir(main_dir_resolver: Optional[Callable[[], str]] = None) -> str:
    """Return the program's own folder for the persistent probe library."""
    global _library_dir_ready
    if main_dir_resolver is None:
        import media as _media
        main_dir_resolver = _media.resolve_main_dir
    path = os.path.join(main_dir_resolver(), "media_library")
    if not _library_dir_ready:
        os.makedirs(path, exist_ok=True)
        _library_dir_ready = True
    return path


def default_db_path(main_dir_resolver: Optional[Callable[[], str]] = None) -> str:
    return os.path.join(resolve_media_library_dir(main_dir_resolver), "library.sqlite3")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS path_keys (
    norm_path   TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    content_key TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library (
    content_key        TEXT PRIMARY KEY,
    size                INTEGER,

    duration            REAL,
    duration_source     TEXT,       -- "ffmpeg" | "vlc" | "legacy_migration"

    width               INTEGER,
    height              INTEGER,
    dar_num             INTEGER,
    dar_den             INTEGER,
    audio_streams       TEXT,       -- JSON list
    subtitle_streams    TEXT,       -- JSON list
    header_probed_at    REAL,

    picture_box         TEXT,       -- JSON {"rect": [...], ...}; filled by detect_picture_box()
    picture_box_detector_version INTEGER,

    gain                REAL,
    gain_target_lufs    REAL,
    gain_scan_version   INTEGER,

    captions_blob       BLOB,       -- zlib-compressed cue text
    captions_source     TEXT,       -- "embedded" | "sidecar" | "asr"
    captions_reason     TEXT,       -- e.g. "no_speech" | "asr_unavailable"

    pan_scan_memory     TEXT,       -- JSON, optional

    created_at          REAL,
    updated_at          REAL
);

CREATE INDEX IF NOT EXISTS idx_library_updated ON library(updated_at);
"""


class MediaLibrary:
    """Thin synchronous wrapper around the sqlite3 connection."""

    def __init__(self, db_path: str):
        if sqlite3 is None:
            raise ImportError("No module named 'sqlite3' (the persistent media library needs it; add sqlite3 to the build)")
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ========================================================================
    # SECTION: Path -> content key
    # ========================================================================

    def lookup_path_key(self, norm_path: str, size: int, mtime: float) -> Optional[str]:
        """Return the cached content_key for norm_path if its recorded size/mtime still match the file on disk."""
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime, content_key FROM path_keys WHERE norm_path=?",
                (norm_path,),
            ).fetchone()
        if row is None:
            return None
        cached_size, cached_mtime, content_key = row
        if cached_size == size and cached_mtime == mtime:
            return content_key
        return None

    def store_path_key(self, norm_path: str, size: int, mtime: float, content_key: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO path_keys (norm_path, size, mtime, content_key, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(norm_path) DO UPDATE SET "
                "size=excluded.size, mtime=excluded.mtime, "
                "content_key=excluded.content_key, updated_at=excluded.updated_at",
                (norm_path, size, mtime, content_key, time.time()),
            )
            self._conn.commit()

    # ========================================================================
    # SECTION: Library rows
    # ========================================================================

    def get_entry(self, content_key: str) -> Optional[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT * FROM library WHERE content_key=?", (content_key,)
            ).fetchone()
            self._conn.row_factory = None
        if row is None:
            return None
        d = dict(row)
        for json_field in ("audio_streams", "subtitle_streams", "picture_box", "pan_scan_memory"):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (TypeError, ValueError):
                    d[json_field] = None
        return d

    def upsert_fields(self, content_key: str, size: Optional[int] = None, **fields):
        """Merges `fields` (column_name -> value) into the row for content_key, creating it if missing."""
        if not fields and size is None:
            return
        now = time.time()
        json_cols = {"audio_streams", "subtitle_streams", "picture_box", "pan_scan_memory"}
        cols = dict(fields)
        for k in json_cols & cols.keys():
            if cols[k] is not None and not isinstance(cols[k], str):
                cols[k] = json.dumps(cols[k])
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM library WHERE content_key=?", (content_key,)
            ).fetchone()
            if existing is None:
                cols["content_key"] = content_key
                cols["size"] = size
                cols["created_at"] = now
                cols["updated_at"] = now
                placeholders = ", ".join("?" for _ in cols)
                col_names = ", ".join(cols.keys())
                self._conn.execute(
                    f"INSERT INTO library ({col_names}) VALUES ({placeholders})",
                    tuple(cols.values()),
                )
            else:
                cols["updated_at"] = now
                if size is not None:
                    cols["size"] = size
                set_clause = ", ".join(f"{k}=?" for k in cols)
                self._conn.execute(
                    f"UPDATE library SET {set_clause} WHERE content_key=?",
                    tuple(cols.values()) + (content_key,),
                )
            self._conn.commit()


# ============================================================================
# SECTION: Identity keys
# ============================================================================

_SAMPLE_SIZE = 64 * 1024
_GROWING_FILE_CHECK_DELAY_S = 0.05


def _sample_offsets(size: int):
    """Return start/middle/end 64 KB sample windows, clamped and de-duplicated for small files."""
    if size <= 0:
        return []
    if size <= _SAMPLE_SIZE:
        return [0]
    offsets = {0, max(0, size - _SAMPLE_SIZE), max(0, (size - _SAMPLE_SIZE) // 2)}
    return sorted(offsets)


def compute_identity_key(file_path: str, growing_check_delay: float = _GROWING_FILE_CHECK_DELAY_S) -> Optional[str]:
    """File size plus a SHA-1 of up to three 64 KB samples (start, middle, end)."""
    try:
        size_before = os.path.getsize(file_path)
    except OSError:
        return None
    if growing_check_delay > 0:
        time.sleep(growing_check_delay)
        try:
            size_after = os.path.getsize(file_path)
        except OSError:
            return None
        if size_after != size_before:
            return None
        size = size_after
    else:
        size = size_before

    h = hashlib.sha1()
    h.update(str(size).encode("ascii"))
    try:
        # WARNING: the disk read must stay inside _fast_disk_io_slot so it yields to live playback.
        import media as _media
        with _media._fast_disk_io_slot(f"identity_key({os.path.basename(file_path)})"):
            with open(file_path, "rb") as f:
                for offset in _sample_offsets(size):
                    f.seek(offset)
                    h.update(f.read(_SAMPLE_SIZE))
    except OSError:
        return None
    return f"{size:x}-{h.hexdigest()}"


class PathKeyResolver:
    """Return a stable content_key for a path, re-hashing only when its size or mtime changed."""

    def __init__(self, library: MediaLibrary):
        self.library = library

    def resolve(self, file_path: str) -> Optional[str]:
        norm_path = os.path.normpath(os.path.abspath(file_path)).lower()
        try:
            st = os.stat(file_path)
        except OSError:
            return None
        cached = self.library.lookup_path_key(norm_path, st.st_size, st.st_mtime)
        if cached is not None:
            return cached
        content_key = compute_identity_key(file_path)
        if content_key is None:
            return None
        try:
            st = os.stat(file_path)
        except OSError:
            return None
        self.library.store_path_key(norm_path, st.st_size, st.st_mtime, content_key)
        return content_key


# ============================================================================
# SECTION: Combined header probe
# ============================================================================

_DURATION_RE = _re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_DIMS_RE = _re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_DAR_RE = _re.compile(r"DAR\s+(\d+):(\d+)")
_SUBTITLE_STREAM_RE = _re.compile(r"Stream #\d+:(\d+)(?:\((\w+)\))?[^\n]*:\s*Subtitle:\s*(\w+)")
_AUDIO_STREAM_RE = _re.compile(r"Stream #\d+:(\d+)(?:\((\w+)\))?[^\n]*:\s*Audio:\s*(\w+)")
_TEXT_SUBTITLE_CODECS = ("subrip", "ass", "ssa", "webvtt", "mov_text", "text")


@dataclass
class HeaderProbeResult:
    ok: bool
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    dar: Optional[tuple] = None
    audio_streams: list = field(default_factory=list)
    subtitle_streams: list = field(default_factory=list)
    has_audio: bool = False
    error: Optional[str] = None
    stderr: Optional[str] = None


def _resolve_ffmpeg(ffmpeg_path: Optional[str]) -> str:
    if ffmpeg_path:
        return ffmpeg_path
    import media as _media
    return _media._resolve_ffmpeg_path()


def combined_header_probe(file_path: str, ffmpeg_path: Optional[str] = None,
                           timeout: float = 15.0) -> HeaderProbeResult:
    """One `ffmpeg -i` header read, parsed into duration, video dimensions + DAR, subtitle streams and the audio-stream list."""
    import media as _media
    if not file_path or not _media._cached_path_exists(file_path):
        return HeaderProbeResult(ok=False, error="path_missing")
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    try:
        with _media._fast_disk_io_slot(f"header_probe({os.path.basename(file_path)})"):
            result = subprocess.run(
                [_resolve_ffmpeg(ffmpeg_path), "-nostdin", "-hide_banner", "-i", file_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs,
            )
    except subprocess.TimeoutExpired:
        return HeaderProbeResult(ok=False, error="timeout")
    except Exception as e:
        return HeaderProbeResult(ok=False, error=str(e))

    err = result.stderr or ""
    if "Input #0" not in err:
        return HeaderProbeResult(ok=False, error="no_input_stream", stderr=err[-2000:])

    out = HeaderProbeResult(ok=True)

    m = _DURATION_RE.search(err)
    if m:
        hh, mm, ss = m.groups()
        total = int(hh) * 3600 + int(mm) * 60 + float(ss)
        out.duration = total if total > 0 else None

    vm = _VIDEO_DIMS_RE.search(err)
    if vm:
        w, h = int(vm.group(1)), int(vm.group(2))
        if w > 0 and h > 0:
            out.width, out.height = w, h
            tail = err[vm.end():vm.end() + 200]
            dm = _DAR_RE.search(tail)
            if dm:
                dn, dd = int(dm.group(1)), int(dm.group(2))
                if dn > 0 and dd > 0:
                    out.dar = (dn, dd)
                    raw_ratio = w / float(h)
                    dar_ratio = dn / float(dd)
                    if abs(raw_ratio - dar_ratio) > 0.02:
                        corrected_w = int(round(h * dar_ratio))
                        corrected_w = max(2, corrected_w - (corrected_w % 2))
                        out.width = corrected_w

    out.subtitle_streams = [
        {"index": int(idx), "lang": lang, "codec": codec,
         "is_text": codec in _TEXT_SUBTITLE_CODECS}
        for idx, lang, codec in _SUBTITLE_STREAM_RE.findall(err)
    ]
    out.audio_streams = [
        {"index": int(idx), "lang": lang, "codec": codec}
        for idx, lang, codec in _AUDIO_STREAM_RE.findall(err)
    ]
    out.has_audio = bool(out.audio_streams) or bool(_re.search(r"Stream #\d+:\d+[^\n]*:\s*Audio:", err))
    if out.has_audio and not out.audio_streams:
        # WARNING: the stored audio_streams list is what says "this file has audio"; keep it non-empty whenever the header shows an audio stream, even one the strict pattern missed.
        out.audio_streams = [{"index": -1, "lang": None, "codec": "unknown"}]
    return out


def probe_duration_with_fallback(file_path: str, header_result: HeaderProbeResult) -> tuple:
    """Returns (duration_seconds_or_None, source_str)."""
    import media as _media
    if header_result.duration and not _media._duration_looks_implausible(file_path, header_result.duration):
        return header_result.duration, "ffmpeg"
    vlc_duration = _media.get_media_duration(file_path)
    if vlc_duration:
        return vlc_duration, "vlc"
    return None, None


# ============================================================================
# SECTION: Picture-box detection
# ============================================================================
PICTURE_BOX_DETECTOR_VERSION = 1

_CROPDETECT_RE = _re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

_PB_SAMPLE_FRACTIONS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)
_PB_FALLBACK_OFFSETS_S = (30.0, 120.0, 300.0)
_PB_SAMPLE_SECONDS = 2.0
_PB_SAMPLE_TIMEOUT_S = 25.0
_PB_MIN_GOOD_SAMPLES = 3
_PB_LIMIT = 32
_PB_MIN_BAR_FRACTION = 0.015
_PB_MIN_BAR_PX = 4
_PB_SYMMETRY_TOLERANCE = 0.25


def _pb_axis(start: int, length: int, frame: int):
    """Decide one axis (vertical or horizontal) from a detected span."""
    lead = max(0, start)
    trail = max(0, frame - (start + length))
    min_bar = max(_PB_MIN_BAR_PX, int(round(frame * _PB_MIN_BAR_FRACTION)))
    if lead < min_bar and trail < min_bar:
        return 0, frame, "none"
    if lead < min_bar or trail < min_bar:
        return 0, frame, "asymmetric"
    if abs(lead - trail) > max(_PB_MIN_BAR_PX, _PB_SYMMETRY_TOLERANCE * max(lead, trail)):
        return 0, frame, "asymmetric"
    return start, length, "bars"


def _pb_sample_times(duration: Optional[float]) -> list:
    if duration and duration > 0:
        latest = max(0.0, duration - _PB_SAMPLE_SECONDS - 0.5)
        return [round(max(0.0, min(duration * f, latest)), 3) for f in _PB_SAMPLE_FRACTIONS]
    return list(_PB_FALLBACK_OFFSETS_S)


def _pb_cropdetect_threads() -> int:
    """ffmpeg thread count for the picture-box scan, from the current performance mode (2 when the governor is unavailable)."""
    try:
        import performance_governor as _pg
        return max(1, int(_pg.lane_ceiling("cropdetect_threads")))
    except Exception:
        return 2


def _pb_run_sample(file_path: str, ffmpeg: str, t: float, timeout: float):
    """One short cropdetect decode."""
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-threads", str(_pb_cropdetect_threads()),
        "-ss", f"{t:.3f}", "-i", file_path, "-t", f"{_PB_SAMPLE_SECONDS:g}",
        "-map", "0:v:0", "-vf", f"cropdetect=limit={_PB_LIMIT}:round=2:reset=0",
        "-an", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        import media as _media
        with _media._fast_disk_io_slot(f"picture_box({os.path.basename(file_path)})"):
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs,
            )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    return result.stderr or ""


def detect_picture_box(file_path: str, duration: Optional[float] = None,
                        ffmpeg_path: Optional[str] = None,
                        sample_timeout: float = _PB_SAMPLE_TIMEOUT_S,
                        diag: Optional[dict] = None, parallel: int = 1) -> Optional[dict]:
    """Find the real picture inside the frame: ffmpeg cropdetect at several points, widest box wins; parallel above 1 decodes the sample points at the same time; diag, if given, receives the timeout count and the stderr of a file ffmpeg would not open."""
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    frame_w = frame_h = None
    pixel_aspect = 1.0
    boxes = []
    timeouts = 0

    _times = _pb_sample_times(duration)
    if parallel > 1 and len(_times) > 1:
        from concurrent.futures import ThreadPoolExecutor as _Pool
        _texts = _Pool(max_workers=min(int(parallel), len(_times))).map(
            lambda _t: _pb_run_sample(file_path, ffmpeg, _t, sample_timeout), _times)
    else:
        _texts = (_pb_run_sample(file_path, ffmpeg, _t, sample_timeout) for _t in _times)
    for text in _texts:
        if text is None:
            timeouts += 1
            continue
        if frame_w is None:
            if "Input #0" not in text:
                if diag is not None:
                    diag["stderr"] = text[-2000:]
                return None
            vm = _VIDEO_DIMS_RE.search(text)
            if not vm:
                return _pb_no_video()
            line_end = text.find("\n", vm.end())
            video_line = text[vm.start():line_end if line_end != -1 else len(text)]
            if "attached pic" in video_line:
                return _pb_no_video()
            frame_w, frame_h = int(vm.group(1)), int(vm.group(2))
            dm = _DAR_RE.search(text[vm.end():vm.end() + 200])
            if dm and int(dm.group(1)) > 0 and int(dm.group(2)) > 0:
                dar = int(dm.group(1)) / float(dm.group(2))
                stored = frame_w / float(frame_h)
                if abs(dar - stored) > 0.02:
                    pixel_aspect = dar / stored
        found = _CROPDETECT_RE.findall(text)
        if not found:
            continue
        w, h, x, y = (int(v) for v in found[-1])
        if w > 0 and h > 0:
            boxes.append((x, y, w, h))

    if diag is not None:
        diag["timeouts"] = timeouts
    if frame_w is None:
        return None
    if len(boxes) < _PB_MIN_GOOD_SAMPLES:
        # WARNING: thin samples with no timeout mean the file opened fine (very short or mostly black); it still gets a label. Samples lost to timeouts mean a struggling drive, which must not be stored as "no bars".
        if timeouts:
            return None
        x0, y0, pw, ph = 0, 0, frame_w, frame_h
        reason = "thin_samples_full_frame"
    else:
        left = min(b[0] for b in boxes)
        top = min(b[1] for b in boxes)
        right = max(b[0] + b[2] for b in boxes)
        bottom = max(b[1] + b[3] for b in boxes)
        right, bottom = min(right, frame_w), min(bottom, frame_h)

        y0, ph, vkind = _pb_axis(top, bottom - top, frame_h)
        x0, pw, hkind = _pb_axis(left, right - left, frame_w)

        if vkind == "bars" and hkind == "bars":
            reason = "windowbox"
        elif vkind == "bars":
            reason = "letterbox"
        elif hkind == "bars":
            reason = "pillarbox"
        else:
            bad_v, bad_h = vkind == "asymmetric", hkind == "asymmetric"
            reason = ("asymmetric_both" if bad_v and bad_h else
                      "asymmetric_vertical" if bad_v else
                      "asymmetric_horizontal" if bad_h else "no_bars")

    return {
        "rect": [x0, y0, pw, ph],
        "frame": [frame_w, frame_h],
        "norm": [round(x0 / frame_w, 5), round(y0 / frame_h, 5),
                 round(pw / frame_w, 5), round(ph / frame_h, 5)],
        "has_bars": (pw, ph) != (frame_w, frame_h),
        "reason": reason,
        "pixel_aspect": round(pixel_aspect, 5),
        "picture_aspect": round((pw / float(ph)) * pixel_aspect, 4),
        "samples": len(boxes),
        "sample_boxes": [list(b) for b in boxes],
        "detector_version": PICTURE_BOX_DETECTOR_VERSION,
    }


def _pb_no_video() -> dict:
    """Returns the definitive answer for audio-only and cover-art-only files."""
    return {"rect": None, "frame": None, "norm": None, "has_bars": False,
            "reason": "no_video", "detector_version": PICTURE_BOX_DETECTOR_VERSION}


# ============================================================================
# SECTION: Burned-in (hard-coded) caption detection
# ============================================================================
BURNED_IN_DETECTOR_VERSION = 1
_BI_SOURCE_BAKED = "baked"
_BI_SOURCE_CLEAN = "clean"

# VALUES CHANGE HERE
_BI_FRAME_W = 480
_BI_FRAME_H = 270
_BI_SAMPLE_FRACTIONS = (0.06, 0.14, 0.22, 0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78, 0.86, 0.94)
_BI_FALLBACK_OFFSETS_S = (20.0, 60.0, 120.0, 180.0, 240.0, 300.0)
_BI_FRAME_TIMEOUT_S = 20.0
_BI_REGION_TOP_FRAC = 0.55
_BI_BRIGHT_MIN = 205
_BI_DARK_MAX = 75
_BI_OUTLINE_REACH_PX = 2
_BI_ROW_MIN_TEXT_FRAC = 0.025
_BI_ROW_MIN_TRANSITIONS = 10
_BI_BAND_MIN_H_FRAC = 0.018
_BI_BAND_MAX_H_FRAC = 0.10
_BI_BAND_MIN_WIDTH_FRAC = 0.12
_BI_BAND_CENTER_RANGE = (0.25, 0.75)
_BI_BAND_MIN_GLYPH_CLUSTERS = 6
_BI_BAND_MAX_FILL = 0.50
_BI_BAND_CONTRAST_RATIO = 4.0
_BI_POSITION_TOLERANCE_FRAC = 0.05
_BI_MIN_POSITIVE_FRAMES = 3
_BI_MAX_BANDS_PER_FRAME = 3
_BI_EARLY_STOP_FRAMES = 4
_BI_MIN_AGREEING_SHARE = 0.6


def _bi_dilate(mask, reach):
    """Boolean dilation by a square of +-reach pixels (numpy only)."""
    import numpy as np
    out = mask.copy()
    h, w = mask.shape
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            if dx == 0 and dy == 0:
                continue
            ys0, ys1 = max(0, dy), h + min(0, dy)
            xs0, xs1 = max(0, dx), w + min(0, dx)
            out[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx] |= mask[ys0:ys1, xs0:xs1]
    return out


def _bi_frame_text_bottoms(gray):
    """Returns the bottom edge, as a fraction of frame height, of every band in one 8-bit gray frame that looks like a subtitle line."""
    import numpy as np
    frame_h, frame_w = gray.shape
    y0 = int(frame_h * _BI_REGION_TOP_FRAC)
    crop = gray[y0:, :]
    bright = crop >= _BI_BRIGHT_MIN
    dark = crop <= _BI_DARK_MAX
    text = bright & _bi_dilate(dark, _BI_OUTLINE_REACH_PX)

    row_count = text.sum(axis=1)
    row_trans = (bright[:, 1:] != bright[:, :-1]).sum(axis=1)
    cand = (row_count >= _BI_ROW_MIN_TEXT_FRAC * frame_w) & (row_trans >= _BI_ROW_MIN_TRANSITIONS)
    if not cand.any():
        return []

    bands = []
    rows = np.flatnonzero(cand)
    start = prev = int(rows[0])
    for r in rows[1:]:
        r = int(r)
        if r - prev > 2:
            bands.append((start, prev))
            start = r
        prev = r
    bands.append((start, prev))

    min_h = max(3, int(round(frame_h * _BI_BAND_MIN_H_FRAC)))
    max_h = int(round(frame_h * _BI_BAND_MAX_H_FRAC))
    in_band = np.zeros(len(row_count), dtype=bool)
    for a, b in bands:
        in_band[a:b + 1] = True
    outside = row_count[~in_band]
    background = float(np.median(outside)) if outside.size else 0.0

    bottoms = []
    for a, b in bands:
        height = b - a + 1
        if height < min_h or height > max_h:
            continue
        cols = text[a:b + 1].any(axis=0)
        hit = np.flatnonzero(cols)
        if hit.size == 0:
            continue
        left, right = int(hit[0]), int(hit[-1])
        extent = right - left + 1
        if extent < _BI_BAND_MIN_WIDTH_FRAC * frame_w:
            continue
        centre = (left + right) / 2.0 / frame_w
        if not (_BI_BAND_CENTER_RANGE[0] <= centre <= _BI_BAND_CENTER_RANGE[1]):
            continue
        clusters = int(((cols[1:].astype(np.int8) - cols[:-1].astype(np.int8)) == 1).sum()) + int(bool(cols[0]))
        if clusters < _BI_BAND_MIN_GLYPH_CLUSTERS:
            continue
        box = bright[a:b + 1, left:right + 1]
        if box.mean() > _BI_BAND_MAX_FILL:
            continue
        band_mean = float(row_count[a:b + 1].mean())
        if band_mean < _BI_BAND_CONTRAST_RATIO * max(background, 1.0):
            continue
        bottoms.append((y0 + b + 1) / float(frame_h))
    if len(bottoms) > _BI_MAX_BANDS_PER_FRAME:
        return []
    return bottoms


def _bi_grab_frame(file_path, ffmpeg, t, timeout):
    """One scaled gray frame as a numpy array, or None when ffmpeg timed out or produced nothing."""
    import numpy as np
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{t:.3f}", "-i", file_path, "-map", "0:v:0", "-frames:v", "1",
        "-an", "-sn", "-dn",
        "-vf", f"scale={_BI_FRAME_W}:{_BI_FRAME_H}:flags=area,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        import media as _media
        with _media._fast_disk_io_slot(f"burned_in({os.path.basename(file_path)})"):
            result = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                    stdin=subprocess.DEVNULL, **popen_kwargs)
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception:
        return None
    data = result.stdout or b""
    if len(data) != _BI_FRAME_W * _BI_FRAME_H:
        return None
    return np.frombuffer(data, dtype=np.uint8).reshape(_BI_FRAME_H, _BI_FRAME_W)


def _bi_positions_agree(bottoms_per_positive_frame, early=False):
    """True when enough positive frames put their lowest text band at the same height (the same spot, frame after frame, is what separates subtitles from scrolling credits or a one-off graphic)."""
    lows = [max(b) for b in bottoms_per_positive_frame]
    best = 0
    for ref in lows:
        near = sum(1 for v in lows if abs(v - ref) <= _BI_POSITION_TOLERANCE_FRAC)
        best = max(best, near)
    need = _BI_EARLY_STOP_FRAMES if early else _BI_MIN_POSITIVE_FRAMES
    return best >= need and best >= _BI_MIN_AGREEING_SHARE * len(lows)


def _bi_sample_times(duration, extra=False):
    if duration and duration > 0:
        latest = max(0.0, duration - 1.0)
        fractions = _BI_SAMPLE_FRACTIONS
        if extra:
            fractions = tuple(f + 0.04 for f in _BI_SAMPLE_FRACTIONS)
        return [round(min(duration * f, latest), 3) for f in fractions]
    base = _BI_FALLBACK_OFFSETS_S
    return [t + 10.0 for t in base] if extra else list(base)


def detect_burned_in_captions(file_path, duration=None, ffmpeg_path=None,
                              frame_timeout=_BI_FRAME_TIMEOUT_S, diag=None):
    """Returns True when subtitle text is part of the picture, False when it is not, None when it could not tell."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        if diag is not None:
            diag["reason"] = "numpy_unavailable"
        return None
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)

    positives = []
    decoded = 0
    timeouts = 0
    planned = 0
    for extra in (False, True):
        if extra and not positives:
            break
        times = _bi_sample_times(duration, extra=extra)
        planned += len(times)
        for t in times:
            frame = _bi_grab_frame(file_path, ffmpeg, t, frame_timeout)
            if isinstance(frame, str):
                timeouts += 1
                continue
            if frame is None:
                continue
            decoded += 1
            bottoms = _bi_frame_text_bands_safe(frame)
            if bottoms:
                positives.append(bottoms)
                if _bi_positions_agree(positives, early=True):
                    if diag is not None:
                        diag.update(positive_frames=len(positives), decoded=decoded, reason="text_band_found")
                    return True
        if _bi_positions_agree(positives):
            if diag is not None:
                diag.update(positive_frames=len(positives), decoded=decoded, reason="text_band_found")
            return True
    if diag is not None:
        diag.update(positive_frames=len(positives), decoded=decoded, timeouts=timeouts)
    if decoded == 0:
        if diag is not None:
            diag["reason"] = "no_frames"
        return None
    # WARNING: samples lost to timeouts mean a struggling drive; that must not be stored as "clean".
    if timeouts and decoded < planned // 2:
        return None
    return False


def _bi_frame_text_bands_safe(frame):
    try:
        return _bi_frame_text_bottoms(frame)
    except Exception as e:
        log.warning("[MEDIA LIBRARY] burned-in frame analysis failed: %s", e)
        return []


# ============================================================================
# SECTION: Probing into the library
# ============================================================================

class ProbeLibrary:
    """Entry point: ties PathKeyResolver, MediaLibrary and combined_header_probe together (get_or_probe_sync / get_cached) and hosts the migration helpers."""

    def __init__(self, library: MediaLibrary, ffmpeg_path: Optional[str] = None,
                 use_vlc_fallback: bool = True):
        self.library = library
        self.keys = PathKeyResolver(library)
        self.ffmpeg_path = ffmpeg_path
        self.use_vlc_fallback = use_vlc_fallback
        self._header_locks = {}
        self._header_locks_guard = threading.Lock()

    def ensure_header(self, file_path: str) -> Optional[dict]:
        """Returns the library entry with its header facts stored, reading the header once if it has never been read. Concurrent callers for one file wait on a per-file lock and reuse the stored result, so a file's header is opened once however many lanes ask."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        entry = self.library.get_entry(content_key)
        if entry is not None and entry.get("header_probed_at"):
            return entry
        with self._header_locks_guard:
            slot = self._header_locks.get(content_key)
            if slot is None:
                slot = [threading.Lock(), 0]
                self._header_locks[content_key] = slot
            slot[1] += 1
        try:
            with slot[0]:
                entry = self.library.get_entry(content_key)
                if entry is None or not entry.get("header_probed_at"):
                    self._probe_header_into(file_path, content_key)
                    entry = self.library.get_entry(content_key)
        finally:
            with self._header_locks_guard:
                slot[1] -= 1
                if slot[1] == 0:
                    self._header_locks.pop(content_key, None)
        return entry

    def header_facts(self, file_path: str) -> Optional[dict]:
        """The shared header read as {ok, duration, width, height, has_audio}. None when the library cannot key the file; ok False when the header read failed."""
        entry = self.ensure_header(file_path)
        if entry is None:
            return None
        if not entry.get("header_probed_at"):
            return {"ok": False}
        return {
            "ok": True,
            "duration": entry.get("duration") or None,
            "width": entry.get("width") or None,
            "height": entry.get("height") or None,
            "has_audio": bool(entry.get("audio_streams")),
        }

    def get_cached(self, file_path: str) -> Optional[dict]:
        """Read-only: returns the library entry if one already exists for this path's current content, else None."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        return self.library.get_entry(content_key)

    def get_or_probe_sync(self, file_path: str, max_age_s: Optional[float] = None) -> Optional[dict]:
        """Return a library entry no older than max_age_s, probing synchronously if there isn't one."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        entry = self.library.get_entry(content_key)
        if entry is not None:
            if max_age_s is None:
                return entry
            if (time.time() - (entry.get("header_probed_at") or 0)) <= max_age_s:
                return entry
        self._probe_header_into(file_path, content_key)
        return self.library.get_entry(content_key)

    def _probe_header_into(self, file_path: str, content_key: str):
        result = combined_header_probe(file_path, ffmpeg_path=self.ffmpeg_path)
        if not result.ok:
            log.warning("[MEDIA LIBRARY] header probe failed for %s: %s",
                        os.path.basename(file_path), result.error)
            return
        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = None
        if self.use_vlc_fallback:
            duration, source = probe_duration_with_fallback(file_path, result)
        else:
            duration, source = result.duration, "ffmpeg"
        self.library.upsert_fields(
            content_key,
            size=size,
            duration=duration,
            duration_source=source,
            width=result.width,
            height=result.height,
            dar_num=(result.dar[0] if result.dar else None),
            dar_den=(result.dar[1] if result.dar else None),
            audio_streams=result.audio_streams,
            subtitle_streams=result.subtitle_streams,
            header_probed_at=time.time(),
        )

    def detect_picture_box_if_needed(self, file_path: str, diag: Optional[dict] = None, parallel: int = 1) -> Optional[dict]:
        """The picture-box hook for any caller that wants the answer now; diag, if given, receives why there was no answer."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            if diag is not None:
                diag["unkeyable"] = True
            return None
        entry = self.library.get_entry(content_key)
        if (entry and entry.get("picture_box")
                and (entry.get("picture_box_detector_version") or 0) >= PICTURE_BOX_DETECTOR_VERSION):
            return entry["picture_box"]
        if entry is None or not entry.get("header_probed_at"):
            entry = self.get_or_probe_sync(file_path)
        duration = (entry or {}).get("duration")
        box = detect_picture_box(file_path, duration=duration, ffmpeg_path=self.ffmpeg_path, diag=diag, parallel=parallel)
        if box is None:
            return None
        self.library.upsert_fields(
            content_key, picture_box=box,
            picture_box_detector_version=PICTURE_BOX_DETECTOR_VERSION,
        )
        return box

    def store_full_frame_picture_box(self, file_path: str) -> Optional[dict]:
        """Labels a file whose cropdetect keeps timing out from its stored frame size, with no bars; None when the frame size is unknown."""
        content_key = self.keys.resolve(file_path)
        entry = self.library.get_entry(content_key) if content_key is not None else None
        w, h = (entry or {}).get("width"), (entry or {}).get("height")
        if not w or not h:
            return None
        box = {
            "rect": [0, 0, int(w), int(h)], "frame": [int(w), int(h)], "norm": [0.0, 0.0, 1.0, 1.0],
            "has_bars": False, "reason": "timeout_full_frame", "pixel_aspect": 1.0,
            "picture_aspect": round(int(w) / float(int(h)), 4), "samples": 0, "sample_boxes": [],
            "detector_version": PICTURE_BOX_DETECTOR_VERSION,
        }
        self.library.upsert_fields(
            content_key, picture_box=box,
            picture_box_detector_version=PICTURE_BOX_DETECTOR_VERSION,
        )
        return box


    def store_picture_box(self, file_path: str, rect, detector_version: int):
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(
            content_key, picture_box={"rect": list(rect)},
            picture_box_detector_version=detector_version,
        )
        return True

    def store_gain(self, file_path: str, gain: float, target_lufs: float, scan_version: int):
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(
            content_key, gain=gain, gain_target_lufs=target_lufs,
            gain_scan_version=scan_version,
        )
        return True

    def store_captions(self, file_path: str, text: str, source: str, reason: Optional[str] = None):
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        blob = zlib.compress(text.encode("utf-8")) if text else None
        self.library.upsert_fields(
            content_key, captions_blob=blob, captions_source=source,
            captions_reason=reason,
        )
        return True

    def get_captions_text(self, file_path: str) -> Optional[str]:
        entry = self.get_cached(file_path)
        if not entry or not entry.get("captions_blob"):
            return None
        try:
            return zlib.decompress(entry["captions_blob"]).decode("utf-8")
        except (zlib.error, UnicodeDecodeError) as e:
            log.warning("[MEDIA LIBRARY] stored captions for %s failed to decompress "
                        "(%s) -- treating as no captions on file", os.path.basename(file_path), e)
            return None

    def get_burned_in_verdict(self, file_path: str) -> Optional[bool]:
        """Read-only: the stored burned-in verdict for this file's current content (True / False), or None when it has never been scanned by the current detector."""
        entry = self.get_cached(file_path)
        if not entry:
            return None
        if entry.get("captions_reason") != f"burned_in_v{BURNED_IN_DETECTOR_VERSION}":
            return None
        source = entry.get("captions_source")
        if source == _BI_SOURCE_BAKED:
            return True
        if source == _BI_SOURCE_CLEAN:
            return False
        return None

    def detect_burned_in_if_needed(self, file_path: str, diag: Optional[dict] = None) -> Optional[bool]:
        """Stored burned-in verdict if there is one, otherwise scan the file once and store the answer; None when the scan could not decide (nothing is stored then, so a later pass retries)."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        stored = self.get_burned_in_verdict(file_path)
        if stored is not None:
            return stored
        entry = self.library.get_entry(content_key)
        if entry is None or not entry.get("header_probed_at"):
            entry = self.get_or_probe_sync(file_path)
        if entry is not None and entry.get("width") is None and entry.get("header_probed_at"):
            verdict = False
        else:
            verdict = detect_burned_in_captions(
                file_path, duration=(entry or {}).get("duration"),
                ffmpeg_path=self.ffmpeg_path, diag=diag)
        if verdict is None:
            return None
        self.library.upsert_fields(
            content_key,
            captions_source=_BI_SOURCE_BAKED if verdict else _BI_SOURCE_CLEAN,
            captions_reason=f"burned_in_v{BURNED_IN_DETECTOR_VERSION}",
        )
        return verdict

    def store_pan_scan_memory(self, file_path: str, memory: dict):
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, pan_scan_memory=memory)
        return True


_shared_probe_library = None
_shared_probe_library_lock = threading.Lock()


def shared_probe_library() -> "ProbeLibrary":
    """The one process-wide ProbeLibrary on the default database, built on first use."""
    global _shared_probe_library
    if _shared_probe_library is None:
        with _shared_probe_library_lock:
            if _shared_probe_library is None:
                _shared_probe_library = ProbeLibrary(MediaLibrary(default_db_path()))
    return _shared_probe_library


# ============================================================================
# SECTION: Legacy cache migration
# ============================================================================

@dataclass
class MigrationProgress:
    scanned: int = 0
    migrated: int = 0
    skipped_missing: int = 0
    skipped_growing: int = 0
    done: bool = False
    skipped_stale: int = 0
    newly_written: int = 0


class LegacyCacheMigrator:
    """Throttled, resumable import of db.config["media_probe_cache"] and db.config["dvd_gain_cache"] into the library."""

    def __init__(self, probe_library: ProbeLibrary, batch_size: int = 25):
        self.probe_library = probe_library
        self.batch_size = batch_size
        self.progress = MigrationProgress()
        self._media_probe_cache_iter = None
        self._dvd_gain_cache_iter = None
        self._media_probe_cache_exhausted = True
        self._dvd_gain_cache_exhausted = True
        self.gain_target_lufs: Optional[float] = None

    def start(self, media_probe_cache: dict, dvd_gain_cache: dict,
              gain_target_lufs: Optional[float] = None):
        self.gain_target_lufs = gain_target_lufs
        self._media_probe_cache_iter = iter(list(media_probe_cache.items()))
        self._dvd_gain_cache_iter = iter(list((dvd_gain_cache or {}).items()))
        self._media_probe_cache_exhausted = False
        self._dvd_gain_cache_exhausted = False
        self.progress = MigrationProgress()

    def step(self) -> MigrationProgress:
        if self._media_probe_cache_iter is None:
            return self.progress
        budget = self.batch_size
        # WARNING: dvd_gain_cache must drain BEFORE media_probe_cache; fields are never overwritten once written, so the order decides which gain a file keeps.
        budget = self._drain(self._dvd_gain_cache_iter, self._migrate_gain_entry, budget, "dvd_gain_cache")
        if budget > 0:
            budget = self._drain(self._media_probe_cache_iter, self._migrate_probe_entry, budget, "media_probe_cache")
        if self._media_probe_cache_exhausted and self._dvd_gain_cache_exhausted:
            self.progress.done = True
        return self.progress

    def _drain(self, it, fn, budget, which):
        while budget > 0:
            try:
                path, value = next(it)
            except StopIteration:
                if which == "media_probe_cache":
                    self._media_probe_cache_exhausted = True
                else:
                    self._dvd_gain_cache_exhausted = True
                break
            self.progress.scanned += 1
            fn(path, value)
            budget -= 1
        return budget

    @staticmethod
    def _legacy_entry_is_stale(norm_path: str, entry: dict) -> bool:
        """True if the legacy entry's recorded size/mtime no longer match the file on disk."""
        mtime = entry.get("measured_mtime")
        size = entry.get("measured_size")
        if mtime is None or size is None:
            return False
        try:
            st = os.stat(norm_path)
        except OSError:
            return False
        return st.st_mtime != mtime or st.st_size != size

    def _write_missing_fields(self, content_key: str, fields: dict) -> bool:
        """Add only the fields the library row doesn't already have."""
        existing = self.probe_library.library.get_entry(content_key) or {}
        to_write = {k: v for k, v in fields.items() if existing.get(k) is None}
        if "duration" in fields and existing.get("duration") is not None:
            to_write.pop("duration_source", None)
        if "gain" not in to_write:
            to_write.pop("gain_target_lufs", None)
        if not to_write:
            return False
        self.probe_library.library.upsert_fields(content_key, **to_write)
        return True

    def _migrate_probe_entry(self, norm_path: str, entry: dict):
        if not os.path.exists(norm_path):
            self.progress.skipped_missing += 1
            return
        if self._legacy_entry_is_stale(norm_path, entry):
            self.progress.skipped_stale += 1
            return
        content_key = self.probe_library.keys.resolve(norm_path)
        if content_key is None:
            self.progress.skipped_growing += 1
            return
        fields = {}
        duration = entry.get("duration")
        if duration:
            fields["duration"] = duration
            fields["duration_source"] = "legacy_migration"
        dims = entry.get("dims")
        if dims and len(dims) == 2:
            fields["width"], fields["height"] = int(dims[0]), int(dims[1])
        gain = entry.get("gain")
        if gain:
            fields["gain"] = gain
            if self.gain_target_lufs is not None:
                fields["gain_target_lufs"] = self.gain_target_lufs
        if fields:
            if self._write_missing_fields(content_key, fields):
                self.progress.newly_written += 1
            self.progress.migrated += 1

    def _migrate_gain_entry(self, norm_path: str, gain):
        if not os.path.exists(norm_path):
            self.progress.skipped_missing += 1
            return
        content_key = self.probe_library.keys.resolve(norm_path)
        if content_key is None:
            self.progress.skipped_growing += 1
            return
        if gain:
            fields = {"gain": gain}
            if self.gain_target_lufs is not None:
                fields["gain_target_lufs"] = self.gain_target_lufs
            if self._write_missing_fields(content_key, fields):
                self.progress.newly_written += 1
            self.progress.migrated += 1


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "resolve_media_library_dir", "default_db_path", "MediaLibrary", "PathKeyResolver",
    "compute_identity_key", "HeaderProbeResult", "combined_header_probe",
    "probe_duration_with_fallback", "PICTURE_BOX_DETECTOR_VERSION", "detect_picture_box",
    "ProbeLibrary", "MigrationProgress", "LegacyCacheMigrator",
    "BURNED_IN_DETECTOR_VERSION", "detect_burned_in_captions", "shared_probe_library",
]
