"""Persistent Probe Library: a SQLite-backed cache of what is known about each media file.

Owns: the library database and its storage location, content-identity keys,
the combined ffmpeg header probe, picture-box (baked-in bars) detection and
storage.
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
import random
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

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

# ============================================================================
# SECTION: Storage location & database
# ============================================================================

_library_dirs_ready = set()


def resolve_media_library_dir(main_dir_resolver: Optional[Callable[[], str]] = None) -> str:
    """Return the program's own folder for the persistent probe library."""
    if main_dir_resolver is None:
        import media as _media
        main_dir_resolver = _media.resolve_main_dir
    path = os.path.join(main_dir_resolver(), "media_library")
    if path not in _library_dirs_ready:
        os.makedirs(path, exist_ok=True)
        _library_dirs_ready.add(path)
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

    estimated_duration  REAL,       -- saved estimate while the real length is unscanned; kept after the real length arrives
    estimated_at        REAL,       -- when the estimate was saved
    estimate_basis      TEXT,       -- "series_average" | "movie_default"

    gain                REAL,       -- measured loudness gain only; never holds the owner's manual correction
    gain_target_lufs    REAL,
    gain_scan_version   INTEGER,
    gain_convergence_db REAL,
    manual_gain_db      REAL,       -- the owner's own dB correction for this file; 0 means the owner set it back to zero
    gain_checked        INTEGER,    -- 1 once the scanned gain was double-checked with extra scans; a new scan result sets it back to 0
    gain_windows        TEXT,       -- JSON [[place in the file as a fraction, measured LUFS], ...]: every sample window behind the saved scanned gain

    captions_blob       BLOB,       -- zlib-compressed cue text
    captions_source     TEXT,       -- "embedded" | "sidecar" | "asr"
    captions_reason     TEXT,       -- e.g. "no_speech" | "asr_unavailable"

    pan_scan_memory     TEXT,       -- JSON, optional

    caption_kind        TEXT,       -- where the overlay captions come from: "embedded" | "sidecar"
    caption_srt_path    TEXT,       -- the .srt file those captions are read from
    unplayable_strikes  INTEGER,    -- playability probe failures so far
    unplayable_reason   TEXT,
    confirmed_unplayable INTEGER,   -- 1 once the file is confirmed corrupt or broken
    confirmed_no_audio  INTEGER,    -- 1 once the file is confirmed to have nothing audible
    no_audio_reason     TEXT,
    loudness_timeout_streak INTEGER,
    loudness_next_retry_ts  REAL,

    created_at          REAL,
    updated_at          REAL
);

CREATE INDEX IF NOT EXISTS idx_library_updated ON library(updated_at);

CREATE TABLE IF NOT EXISTS hub_play_order (
    list_id   TEXT NOT NULL,
    path_key  TEXT NOT NULL,
    path      TEXT NOT NULL,
    position  INTEGER NOT NULL,
    aired     INTEGER NOT NULL DEFAULT 0,
    active    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (list_id, path_key)
);

CREATE TABLE IF NOT EXISTS hub_play_state (
    list_id   TEXT PRIMARY KEY,
    pass_no   INTEGER NOT NULL DEFAULT 1,
    last_key  TEXT
);

CREATE TABLE IF NOT EXISTS hub_schedule_row (
    channel        TEXT NOT NULL,
    slot_key       TEXT NOT NULL,      -- the block name or date this row belongs to
    position       INTEGER NOT NULL,
    path_key       TEXT NOT NULL,
    path           TEXT NOT NULL,
    kind           TEXT NOT NULL,      -- episode, movie, commercial or music
    pass_no        INTEGER NOT NULL DEFAULT 1,
    planned_start  REAL,               -- wall-clock seconds since the epoch
    used_length    REAL,
    length_source  TEXT,               -- measured, estimate or schedule
    state          TEXT NOT NULL DEFAULT 'planned',   -- planned, airing, aired or static
    updated_at     REAL NOT NULL,
    bundle         TEXT,               -- the series or movie bundle the row belongs to
    row_files      TEXT,               -- JSON list of every file the row airs when it airs more than one
    guide_entry    TEXT,               -- JSON of the facts the guide draws the row's cell from (seek offset, pair parts, leftovers files)
    PRIMARY KEY (channel, slot_key, position)
);

CREATE INDEX IF NOT EXISTS idx_hub_schedule_start ON hub_schedule_row(channel, planned_start);

CREATE TABLE IF NOT EXISTS hub_future_row (
    channel        TEXT NOT NULL,
    slot_key       TEXT NOT NULL,      -- the block cycle this projected row belongs to
    position       INTEGER NOT NULL,
    path           TEXT NOT NULL,
    kind           TEXT NOT NULL,      -- episode, movie or commercial
    planned_start  REAL NOT NULL,      -- wall-clock seconds since the epoch
    used_length    REAL NOT NULL,
    guide_entry    TEXT,               -- JSON of the facts the guide draws the row's cell from
    updated_at     REAL NOT NULL,
    PRIMARY KEY (channel, slot_key, position)
);

CREATE INDEX IF NOT EXISTS idx_hub_future_start ON hub_future_row(channel, planned_start);

CREATE TABLE IF NOT EXISTS hub_schedule_set (
    channel        TEXT NOT NULL,
    slot_key       TEXT NOT NULL,      -- the set of rows this label belongs to
    mode           TEXT NOT NULL,      -- the slot mode that made the set
    state          TEXT NOT NULL DEFAULT 'active',   -- active or set_aside
    stop_position  INTEGER,            -- the entry that was airing when the set was put aside
    stop_epoch     REAL,               -- the clock moment it was put aside
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (channel, slot_key)
);

CREATE TABLE IF NOT EXISTS hub_part_group_gain (
    group_key      TEXT PRIMARY KEY,   -- folder and title of a multi-part movie, lower case
    anchor_path    TEXT NOT NULL,      -- the part that was scanned; the other parts reuse its gain
    gain           REAL NOT NULL,
    target_lufs    REAL,
    scan_version   INTEGER,
    convergence_db REAL,
    updated_at     REAL NOT NULL
);
"""


_LIBRARY_ADDED_COLUMNS = (
    ("estimated_duration", "REAL"),
    ("estimated_at", "REAL"),
    ("estimate_basis", "TEXT"),
    ("gain_convergence_db", "REAL"),
    ("manual_gain_db", "REAL"),
    ("gain_checked", "INTEGER"),
    ("gain_windows", "TEXT"),
    ("caption_kind", "TEXT"),
    ("caption_srt_path", "TEXT"),
    ("unplayable_strikes", "INTEGER"),
    ("unplayable_reason", "TEXT"),
    ("confirmed_unplayable", "INTEGER"),
    ("confirmed_no_audio", "INTEGER"),
    ("no_audio_reason", "TEXT"),
    ("loudness_timeout_streak", "INTEGER"),
    ("loudness_next_retry_ts", "REAL"),
)

# WARNING: FileFactsHub reads and names these fields in this order; change the library schema, _LIBRARY_ADDED_COLUMNS and this tuple together.
FILE_FACT_COLUMNS = (
    "caption_kind", "caption_srt_path", "unplayable_strikes", "unplayable_reason",
    "confirmed_unplayable", "confirmed_no_audio", "no_audio_reason",
    "loudness_timeout_streak", "loudness_next_retry_ts",
)


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
        self._ensure_library_columns()
        self._ensure_schedule_row_columns()
        self._conn.commit()

    def _ensure_library_columns(self):
        """Adds any library column an older database file lacks, so an existing library keeps all its rows."""
        # WARNING: _LIBRARY_ADDED_COLUMNS must list every column added after the first release, or an old library file fails on its first write.
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(library)").fetchall()}
        for name, sql_type in _LIBRARY_ADDED_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE library ADD COLUMN {name} {sql_type}")

    def _ensure_schedule_row_columns(self):
        """Adds the bundle, row_files and guide_entry columns to a hub_schedule_row table an older database file lacks, keeping every row."""
        # WARNING: the table must keep every existing row (R26); old rows read as having no bundle and no guide entry.
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(hub_schedule_row)").fetchall()}
        for name in ("bundle", "row_files", "guide_entry"):
            if name not in have:
                self._conn.execute(f"ALTER TABLE hub_schedule_row ADD COLUMN {name} TEXT")

    def path_duration_row(self, norm_path: str):
        """The measured length and saved estimate for one keyed path, as (duration, estimated_duration), or None when the path has no library row."""
        with self._lock:
            return self._conn.execute(
                "SELECT l.duration, l.estimated_duration "
                "FROM path_keys p JOIN library l ON l.content_key = p.content_key WHERE p.norm_path = ?",
                (norm_path,),
            ).fetchone()

    def path_gain_row(self, norm_path: str):
        """The stored gain and picture-size fields for one keyed path as (gain, target_lufs, scan_version, convergence_db, manual_gain_db, width, height), or None when the path has no library row."""
        with self._lock:
            return self._conn.execute(
                "SELECT l.gain, l.gain_target_lufs, l.gain_scan_version, l.gain_convergence_db, l.manual_gain_db, l.width, l.height "
                "FROM path_keys p JOIN library l ON l.content_key = p.content_key WHERE p.norm_path = ?",
                (norm_path,),
            ).fetchone()

    def path_key_stamp(self, norm_path: str):
        """The size and modified time recorded when a path was last keyed, as (size, mtime), or None when the path was never keyed."""
        with self._lock:
            return self._conn.execute(
                "SELECT size, mtime FROM path_keys WHERE norm_path = ?", (norm_path,),
            ).fetchone()

    def part_group_gain_row(self, group_key: str):
        """The saved gain of one multi-part title as (anchor_path, gain, target_lufs, scan_version, convergence_db), or None when the title has none."""
        with self._lock:
            return self._conn.execute(
                "SELECT anchor_path, gain, target_lufs, scan_version, convergence_db FROM hub_part_group_gain WHERE group_key = ?",
                (group_key,),
            ).fetchone()

    def store_part_group_gain(self, group_key: str, anchor_path: str, gain: float, target_lufs: Optional[float] = None,
                              scan_version: Optional[int] = None, convergence_db: Optional[float] = None,
                              keep_existing: bool = False) -> bool:
        """Saves the gain of a multi-part title with the settings it was measured under; a saved gain is replaced unless keep_existing is set. Returns True when a row was written."""
        verb = "INSERT OR IGNORE" if keep_existing else "INSERT OR REPLACE"
        with self._lock:
            cursor = self._conn.execute(
                f"{verb} INTO hub_part_group_gain (group_key, anchor_path, gain, target_lufs, scan_version, convergence_db, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (group_key, anchor_path, float(gain), target_lufs, scan_version, convergence_db, time.time()),
            )
            self._conn.commit()
            return cursor.rowcount > 0

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
PICTURE_BOX_DETECTOR_VERSION = 3

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

# WARNING: the fallback numbers must match PICTURE_BOX_FLAT_* in performance_governor.py; they are used only when that module fails to import.
try:
    import performance_governor as _pb_governor

    _PB_FLAT_W = int(_pb_governor.PICTURE_BOX_FLAT_WIDTH)
    _PB_FLAT_H = int(_pb_governor.PICTURE_BOX_FLAT_HEIGHT)
    _PB_FLAT_TOLERANCE = int(_pb_governor.PICTURE_BOX_FLAT_TOLERANCE)
    _PB_FLAT_FRAMES = int(_pb_governor.PICTURE_BOX_FLAT_FRAMES)
    _PB_FLAT_MIN_PICTURE_FRACTION = float(_pb_governor.PICTURE_BOX_FLAT_MIN_PICTURE_FRACTION)
except Exception as _swallowed_error:
    _log_swallowed("media_library picture box flat tunables: import performance_governor as _pb_governor", _swallowed_error)
    _PB_FLAT_W = 640
    _PB_FLAT_H = 480
    _PB_FLAT_TOLERANCE = 12
    _PB_FLAT_FRAMES = 2
    _PB_FLAT_MIN_PICTURE_FRACTION = 0.125


def _pb_axis(start: int, length: int, frame: int):
    """Decide one axis (vertical or horizontal) from a detected span: a bar of at least the minimum size on either side is cropped to the span found, even when one-sided or uneven."""
    lead = max(0, start)
    trail = max(0, frame - (start + length))
    min_bar = max(_PB_MIN_BAR_PX, int(round(frame * _PB_MIN_BAR_FRACTION)))
    if lead < min_bar and trail < min_bar:
        return 0, frame, "none"
    return start, length, "bars"


def _pb_flat_strip(count: int, stat, from_end: bool):
    """Counts how many lines from one edge are flat and share the edge line's colour; returns (count, edge colour)."""
    count_flat = 0
    edge = None
    order = range(count - 1, -1, -1) if from_end else range(count)
    for index in order:
        lo, hi, mean = stat(index)
        if hi - lo > _PB_FLAT_TOLERANCE:
            break
        if edge is None:
            edge = mean
        elif abs(mean - edge) > _PB_FLAT_TOLERANCE:
            break
        count_flat += 1
    return count_flat, edge


def _pb_flat_sample(frames) -> Optional[dict]:
    """Measures the flat-colour strips on the four edges of one sample from its gray frames; None when the sample cannot say (no frames, a flat or too thin picture)."""
    width, height = _PB_FLAT_W, _PB_FLAT_H
    frame_size = width * height
    frames = [f for f in (frames or []) if len(f) == frame_size]
    if not frames:
        return None
    mid_a, mid_b = width // 4, width - width // 4

    def row_stat(r):
        parts = [f[r * width + mid_a:r * width + mid_b] for f in frames]
        return (min(min(v) for v in parts), max(max(v) for v in parts),
                sum(sum(v) for v in parts) / float(len(parts) * (mid_b - mid_a)))

    top, top_colour = _pb_flat_strip(height, row_stat, False)
    if top >= height:
        return None
    bottom, bottom_colour = _pb_flat_strip(height, row_stat, True)
    first_row, last_row = top, height - bottom
    if (last_row - first_row) < height * _PB_FLAT_MIN_PICTURE_FRACTION:
        return None

    def col_stat(c):
        parts = [f[first_row * width + c:last_row * width:width] for f in frames]
        return (min(min(v) for v in parts), max(max(v) for v in parts),
                sum(sum(v) for v in parts) / float(len(parts) * (last_row - first_row)))

    left, left_colour = _pb_flat_strip(width, col_stat, False)
    right, right_colour = _pb_flat_strip(width, col_stat, True)
    if left + right >= width:
        return None
    return {"top": (top, top_colour), "bottom": (bottom, bottom_colour),
            "left": (left, left_colour), "right": (right, right_colour)}


def _pb_flat_boxes(flat_samples: list, frame_w: int, frame_h: int) -> list:
    """Turns each sample's flat strips into a box in frame pixels (None for a sample that could not say); a side whose strip colour differs between samples is not a bar and is dropped from every sample."""
    sides = ("top", "bottom", "left", "right")
    keep = {}
    for side in sides:
        colours = [f[side][1] for f in flat_samples if f is not None and f[side][0] > 0]
        keep[side] = not colours or (max(colours) - min(colours)) <= _PB_FLAT_TOLERANCE
    boxes = []
    for flat in flat_samples:
        if flat is None:
            boxes.append(None)
            continue
        px = {}
        for side in sides:
            scale = (frame_h if side in ("top", "bottom") else frame_w) / float(_PB_FLAT_H if side in ("top", "bottom") else _PB_FLAT_W)
            px[side] = (int(flat[side][0] * scale) // 2) * 2 if keep[side] else 0
        boxes.append((px["left"], px["top"], frame_w - px["left"] - px["right"], frame_h - px["top"] - px["bottom"]))
    return boxes


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
    except Exception as _swallowed_error:
        _log_swallowed("_pb_cropdetect_threads: import performance_governor as _pg", _swallowed_error)
        return 2


class _PbSampleText(str):
    """The ffmpeg stderr of one sample; its frames attribute holds the gray frames read in the same decode."""
    frames: list = []


def _pb_run_sample(file_path: str, ffmpeg: str, t: float, timeout: float):
    """One short decode: cropdetect text plus a few small gray frames for the flat-bar check."""
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
        "-map", "0:v:0", "-vf", f"fps=1,scale={_PB_FLAT_W}:{_PB_FLAT_H}:flags=area,format=gray",
        "-frames:v", str(_PB_FLAT_FRAMES), "-an", "-sn", "-dn", "-f", "rawvideo", "pipe:1",
    ]
    try:
        import media as _media
        with _media._fast_disk_io_slot(f"picture_box({os.path.basename(file_path)})"):
            result = subprocess.run(
                cmd, capture_output=True,
                timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs,
            )
    except subprocess.TimeoutExpired:
        return None
    except Exception as _swallowed_error:
        _log_swallowed("_pb_run_sample: import media as _media", _swallowed_error)
        return None
    stderr = result.stderr or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    sample = _PbSampleText(stderr)
    raw = result.stdout if isinstance(result.stdout, (bytes, bytearray)) else b""
    frame_size = _PB_FLAT_W * _PB_FLAT_H
    sample.frames = [bytes(raw[k:k + frame_size]) for k in range(0, len(raw) - frame_size + 1, frame_size)]
    return sample


def detect_picture_box(file_path: str, duration: Optional[float] = None,
                        ffmpeg_path: Optional[str] = None,
                        sample_timeout: float = _PB_SAMPLE_TIMEOUT_S,
                        diag: Optional[dict] = None, parallel: int = 1) -> Optional[dict]:
    """Find the real picture inside the frame: at several points ffmpeg cropdetect finds dark bars and the flat-strip check finds bars of any one colour, each sample keeps what both agree is picture, the widest box wins; parallel above 1 decodes the sample points at the same time; diag, if given, receives the timeout count and the stderr of a file ffmpeg would not open."""
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    frame_w = frame_h = None
    pixel_aspect = 1.0
    crop_samples = []
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
            try:
                flat = _pb_flat_sample(getattr(text, "frames", None))
            except Exception as _swallowed_error:
                _log_swallowed("detect_picture_box: _pb_flat_sample", _swallowed_error)
                flat = None
            crop_samples.append(((x, y, w, h), flat))

    if diag is not None:
        diag["timeouts"] = timeouts
    if frame_w is None:
        return None

    flat_boxes = _pb_flat_boxes([c[1] for c in crop_samples], frame_w, frame_h)
    boxes = []
    flat_used = 0
    for (cx, cy, cw, ch), fb in zip((c[0] for c in crop_samples), flat_boxes):
        if fb is not None:
            nx, ny = max(cx, fb[0]), max(cy, fb[1])
            nr, nb = min(cx + cw, fb[0] + fb[2]), min(cy + ch, fb[1] + fb[3])
            if nr > nx and nb > ny:
                cx, cy, cw, ch = nx, ny, nr - nx, nb - ny
                flat_used += 1
        boxes.append((cx, cy, cw, ch))
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
        "flat_samples": flat_used,
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
    except Exception as _swallowed_error:
        _log_swallowed("_bi_grab_frame: import media as _media", _swallowed_error)
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
        import numpy
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
    """Entry point: ties PathKeyResolver, MediaLibrary and combined_header_probe together (get_or_probe_sync / get_cached)."""

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
        # WARNING: a row holding only a saved estimate or a gain has no probe result; returning it would stop the file from ever being measured.
        if entry is not None and (entry.get("header_probed_at") or entry.get("duration")):
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

    def store_gain(self, file_path: str, gain: float, target_lufs: Optional[float] = None,
                   scan_version: Optional[int] = None, convergence_db: Optional[float] = None,
                   checked: bool = False, windows=None):
        """Stores a measured gain with the settings it was measured under; checked marks a gain that was double-checked with extra scans, and a gain stored without it is unchecked; windows is the list of (place in the file as a fraction, LUFS) pairs behind the gain, kept so a later check can compare every window, and a gain stored without them has none on record. A file whose gain the owner has set keeps the owner's gain and the scan result is not written."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        held = self.library.get_entry(content_key)
        if held and held.get("gain_scan_version") == _OWNER_GAIN_SCAN_VERSION:
            return True
        fields = {"gain": gain, "gain_target_lufs": target_lufs,
                  "gain_scan_version": scan_version, "gain_convergence_db": convergence_db,
                  "gain_checked": 1 if checked else 0,
                  "gain_windows": json.dumps([[float(c), float(v)] for c, v in windows]) if windows else None}
        self.library.upsert_fields(content_key, **fields)
        return True

    def gain_windows(self, file_path: str):
        """The sample windows behind the file's saved gain as (place in the file as a fraction, LUFS) pairs; empty when none are on record."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return []
        held = self.library.get_entry(content_key)
        raw = held.get("gain_windows") if held else None
        if not raw:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            return [(float(c), float(v)) for c, v in data]
        except (ValueError, TypeError):
            return []

    def gain_is_checked(self, file_path: str) -> bool:
        """True when the file's saved gain was double-checked with extra scans."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        held = self.library.get_entry(content_key)
        return bool(held and held.get("gain_checked"))

    def mark_gain_checked(self, file_path: str) -> bool:
        """Marks the file's saved gain as double-checked; returns False when the library cannot key the file."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, gain_checked=1)
        return True

    def store_owner_gain(self, file_path: str, total_db: float):
        """Replaces the file's saved gain with the owner's chosen total dB; the scanned number is gone, the scan version is set to the owner mark so the gain always counts as current and no scan replaces it, and the manual offset is 0."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, gain=10 ** (float(total_db) / 20.0), gain_target_lufs=None,
                                   gain_scan_version=_OWNER_GAIN_SCAN_VERSION, gain_convergence_db=None, manual_gain_db=0.0)
        return True

    def clear_scan_values(self, file_path: str, *columns: str) -> bool:
        """Sets the named scan values (duration, gain, width, height) of one file back to nothing so a fresh scan replaces them; returns False when the library cannot key the file or a column is not one of those."""
        if not columns or any(name not in ("duration", "gain", "width", "height") for name in columns):
            return False
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, **{name: None for name in columns})
        return True

    def file_changed_on_disk(self, file_path: str) -> bool:
        """True when the library keyed this path before and the file's size or modified time no longer match what was recorded."""
        try:
            st = os.stat(file_path)
        except OSError:
            return False
        stamp = self.library.path_key_stamp(os.path.normpath(os.path.abspath(file_path)).lower())
        return stamp is not None and (stamp[0] != st.st_size or stamp[1] != st.st_mtime)

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

    def store_dims(self, file_path: str, width: int, height: int):
        """Stores the picture size read from a file; the header-probe stamp is left alone so the full header read still runs once."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, width=int(width), height=int(height))
        return True

    def get_pan_scan_axis(self, file_path: str, axis: str):
        """The saved Pan & Scan shot list for one axis of a file as a list of {"sig", "offset"} dicts, or [] when none is saved."""
        entry = self.get_cached(file_path)
        memory = entry.get("pan_scan_memory") if entry else None
        if not isinstance(memory, dict):
            return []
        shots = (memory.get("axes") or {}).get(axis)
        return list(shots) if isinstance(shots, list) else []

    def store_pan_scan_axis(self, file_path: str, axis: str, shots: list):
        """Saves one axis's Pan & Scan shot list for a file, keeping the other axis; nothing is trimmed."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        with self.library._lock:
            entry = self.library.get_entry(content_key)
            memory = entry.get("pan_scan_memory") if entry else None
            if not isinstance(memory, dict):
                memory = {}
            axes = memory.get("axes") if isinstance(memory.get("axes"), dict) else {}
            axes[axis] = shots
            memory = {"_v": 1, "_saved_at": time.time(), "axes": axes}
            self.library.upsert_fields(content_key, pan_scan_memory=memory)
        return True

    def store_pan_scan_memory(self, file_path: str, memory: dict):
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, pan_scan_memory=memory)
        return True

    def store_file_facts(self, file_path: str, **facts):
        """Saves caption-source, playability and loudness-retry fields for a file; False when the library cannot key the file or a field is not one of FILE_FACT_COLUMNS."""
        if not facts or any(name not in FILE_FACT_COLUMNS for name in facts):
            return False
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, **facts)
        return True

    def add_unplayable_strike(self, file_path: str, reason: str, threshold: int):
        """Adds one playability strike to a file and confirms it unplayable at the threshold; returns the new strike count, or None when the library cannot key the file."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        with self.library._lock:
            entry = self.library.get_entry(content_key) or {}
            count = int(entry.get("unplayable_strikes") or 0) + 1
            fields = {"unplayable_strikes": count, "unplayable_reason": reason}
            if count >= threshold:
                fields["confirmed_unplayable"] = 1
            self.library.upsert_fields(content_key, **fields)
        return count


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
# SECTION: Duration hub (one reader for measured lengths and saved estimates)
# ============================================================================
DURATION_KIND_MEASURED = "measured"
DURATION_KIND_ESTIMATED = "estimated"
ESTIMATE_BASIS_SERIES_AVERAGE = "series_average"
ESTIMATE_BASIS_MOVIE_DEFAULT = "movie_default"


def _hub_key(path) -> str:
    return os.path.normpath(os.path.abspath(str(path))).lower()


# WARNING: the fallback must match HUB_UNMEASURED_RECHECK_SECONDS in performance_governor.py; it is used only when that module fails to import.
try:
    import performance_governor as _hub_governor
    _UNMEASURED_RECHECK_SECONDS = float(_hub_governor.HUB_UNMEASURED_RECHECK_SECONDS)
except Exception:
    _UNMEASURED_RECHECK_SECONDS = 30.0


class DurationHub:
    """Answers one file at a time: looks the file up in the library when it is asked about and remembers the answer; nothing is read in advance. The one place the scheduler, guide and catch-up read a file's length from."""

    def __init__(self, probe_library: "ProbeLibrary"):
        self.probe_library = probe_library
        self._lock = threading.RLock()
        self._memo = {}
        self._paths = {}
        self._must_resolve = set()

    def _read_row(self, key, path, force):
        """The file's (measured, estimate) from the library; a forgotten file is re-keyed from disk first."""
        if force:
            content_key = self.probe_library.keys.resolve(path)
            entry = self.probe_library.library.get_entry(content_key) if content_key is not None else None
            row = None if not entry else (entry.get("duration"), entry.get("estimated_duration"))
        else:
            row = self.probe_library.library.path_duration_row(key)
        if not row:
            return None, None
        return (float(row[0]) if row[0] and row[0] > 0 else None,
                float(row[1]) if row[1] and row[1] > 0 else None)

    def _lookup(self, key, path):
        """Returns [measured, estimate, looked-up-at] for a path, asking the library once and remembering the answer; a path with no measured length is asked again after the recheck time, and a path the library has no length for gets its old settings values carried in first."""
        with self._lock:
            held = self._memo.get(key)
            force = key in self._must_resolve
        now = time.monotonic()
        if held is not None and not force and (held[0] or now - held[2] < _UNMEASURED_RECHECK_SECONDS):
            return held
        measured = estimate = None
        try:
            measured, estimate = self._read_row(key, path, force)
        except Exception as _swallowed_error:
            _log_swallowed("DurationHub._lookup: reading the file's row from the library", _swallowed_error)
        with self._lock:
            self._must_resolve.discard(key)
            self._paths[key] = str(path)
            held = self._memo.get(key)
            if held is not None and held[0] and not force:
                return held
            held = [measured, estimate or (held[1] if held and not force else None), now]
            self._memo[key] = held
        return held

    def read(self, path):
        """Returns (whole seconds, kind) for a file: its measured length, else its saved estimate, else (None, None)."""
        measured, estimate, _at = self._lookup(_hub_key(path), path)
        if measured and measured > 0:
            return int(measured), DURATION_KIND_MEASURED
        if estimate and estimate > 0:
            return int(estimate), DURATION_KIND_ESTIMATED
        return None, None

    def note_measured(self, path, seconds, source="probed"):
        """Records a file's measured length; the first length the library holds for that content stays the one on record. Returns False when the library cannot key the file, so the length is remembered for this run only."""
        if not seconds or seconds <= 0:
            return False
        recorded = float(seconds)
        saved = False
        try:
            content_key = self.probe_library.keys.resolve(path)
            if content_key is not None:
                entry = self.probe_library.library.get_entry(content_key)
                if entry and entry.get("duration") and entry["duration"] > 0:
                    recorded = float(entry["duration"])
                else:
                    self.probe_library.library.upsert_fields(content_key, duration=recorded, duration_source=source)
                saved = True
        except Exception as _swallowed_error:
            _log_swallowed("DurationHub.note_measured: saving the measured length to the library", _swallowed_error)
        key = _hub_key(path)
        with self._lock:
            held = self._memo.get(key)
            self._memo[key] = [recorded, held[1] if held else None, time.monotonic()]
        return saved

    def store_estimate(self, path, seconds, basis):
        """Saves an estimate for a file with no measured length; a file that already has an estimate keeps it. Returns the estimate on record, or None for a measured file or a length that is not positive."""
        if not seconds or seconds <= 0:
            return None
        key = _hub_key(path)
        measured, existing, _at = self._lookup(key, path)
        if measured:
            return None
        if existing:
            return int(existing)
        saved = float(int(round(seconds)))
        try:
            content_key = self.probe_library.keys.resolve(path)
            if content_key is not None:
                entry = self.probe_library.library.get_entry(content_key) or {}
                if entry.get("duration") and entry["duration"] > 0:
                    with self._lock:
                        self._memo[key] = [float(entry["duration"]), entry.get("estimated_duration"), time.monotonic()]
                    return None
                if entry.get("estimated_duration") and entry["estimated_duration"] > 0:
                    saved = float(entry["estimated_duration"])
                else:
                    self.probe_library.library.upsert_fields(
                        content_key, estimated_duration=saved, estimated_at=time.time(), estimate_basis=basis)
        except Exception as _swallowed_error:
            _log_swallowed("DurationHub.store_estimate: saving the estimate to the library", _swallowed_error)
        with self._lock:
            held = self._memo.get(key)
            self._memo[key] = [held[0] if held else None, saved, time.monotonic()]
        return int(saved)

    def forget(self, path):
        """Drops what the hub remembers about a path because the file changed on disk; the next look-up re-keys the file from disk. The library rows stay."""
        key = _hub_key(path)
        with self._lock:
            self._memo.pop(key, None)
            self._must_resolve.add(key)

    def known_paths(self):
        """The paths this hub has been asked about, as the caller gave them."""
        with self._lock:
            return list(self._paths.values())


_shared_duration_hub = None
_shared_duration_hub_lock = threading.Lock()


def shared_duration_hub() -> "DurationHub":
    """The one process-wide DurationHub on the shared probe library."""
    global _shared_duration_hub
    if _shared_duration_hub is None:
        with _shared_duration_hub_lock:
            if _shared_duration_hub is None:
                _shared_duration_hub = DurationHub(shared_probe_library())
    return _shared_duration_hub


# ============================================================================
# SECTION: Gain hub (one reader for scanned gain, its settings and the owner's manual dB)
# ============================================================================


# WARNING: must match OWNER_GAIN_SCAN_VERSION in media.py; a gain row with this scan version is the owner's own gain and no scan replaces it.
_OWNER_GAIN_SCAN_VERSION = 0

# WARNING: the fallback must match HUB_GAIN_MISS_RECHECK_SECONDS in performance_governor.py; it is used only when that module fails to import.
try:
    _GAIN_MISS_RECHECK_SECONDS = float(_hub_governor.HUB_GAIN_MISS_RECHECK_SECONDS)
except Exception:
    _GAIN_MISS_RECHECK_SECONDS = 900.0


class GainHub:
    """Answers one file at a time: looks the file's scanned gain, the settings it was scanned under and the owner's manual dB up in the library when asked, and remembers the answer; nothing is read in advance."""

    def __init__(self, probe_library: "ProbeLibrary"):
        self.probe_library = probe_library
        self._lock = threading.RLock()
        self._memo = {}
        self._paths = {}
        self._must_resolve = set()

    def _lookup(self, key, path):
        """Returns the remembered record for a path, asking the library once; a path with no scanned gain on record is asked again after the recheck time, and a path the library has no gain or picture size for gets its old settings values carried in first."""
        with self._lock:
            held = self._memo.get(key)
            force = key in self._must_resolve
        now = time.monotonic()
        if held is not None and not force and ((held["gain"] is not None and held["width"] is not None) or now - held["at"] < _GAIN_MISS_RECHECK_SECONDS):
            return held
        record = {"gain": None, "target_lufs": None, "scan_version": None,
                  "convergence_db": None, "manual_gain_db": None, "width": None, "height": None, "at": now}
        try:
            if force:
                content_key = self.probe_library.keys.resolve(path)
                entry = self.probe_library.library.get_entry(content_key) if content_key is not None else None
                row = None if not entry else (entry.get("gain"), entry.get("gain_target_lufs"),
                                              entry.get("gain_scan_version"), entry.get("gain_convergence_db"),
                                              entry.get("manual_gain_db"), entry.get("width"), entry.get("height"))
            else:
                row = self.probe_library.library.path_gain_row(key)
            if row:
                record["gain"] = float(row[0]) if row[0] and row[0] > 0 else None
                record["target_lufs"] = row[1]
                record["scan_version"] = row[2]
                record["convergence_db"] = row[3]
                record["manual_gain_db"] = row[4]
                record["width"] = row[5]
                record["height"] = row[6]
        except Exception as _swallowed_error:
            _log_swallowed("GainHub._lookup: reading the file's gain row from the library", _swallowed_error)
        with self._lock:
            self._must_resolve.discard(key)
            self._paths[key] = str(path)
            self._memo[key] = record
        return record

    def read(self, path):
        """Returns the file's stored gain fields as a dict: gain, target_lufs, scan_version, convergence_db and manual_gain_db; a field with nothing on record is None."""
        record = self._lookup(_hub_key(path), path)
        return {name: record[name] for name in ("gain", "target_lufs", "scan_version", "convergence_db", "manual_gain_db")}

    def read_dims(self, path):
        """Returns the file's stored picture size as (width, height), or None when the library has none on record."""
        record = self._lookup(_hub_key(path), path)
        if record["width"] and record["height"]:
            return int(record["width"]), int(record["height"])
        return None

    def note_dims(self, path, width, height):
        """Records a measured picture size. Returns False when the library cannot key the file, so the caller can keep the value elsewhere."""
        try:
            if not self.probe_library.store_dims(path, int(width), int(height)):
                return False
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_dims: saving the picture size to the library", _swallowed_error)
            return False
        key = _hub_key(path)
        with self._lock:
            held = self._memo.get(key)
            if held is None:
                held = {"gain": None, "target_lufs": None, "scan_version": None, "convergence_db": None,
                        "manual_gain_db": None, "width": None, "height": None, "at": time.monotonic()}
                self._memo[key] = held
            held["width"], held["height"] = int(width), int(height)
        return True

    def note_gain(self, path, gain, target_lufs=None, scan_version=None, convergence_db=None, checked=False, windows=None):
        """Records a measured gain with the settings it was measured under; checked marks a gain that was double-checked with extra scans; windows are the (place, LUFS) pairs behind it. Returns False when the library cannot key the file, so the caller can keep the value elsewhere."""
        if not gain or gain <= 0:
            return False
        if self.read(path)["scan_version"] == _OWNER_GAIN_SCAN_VERSION:
            return True
        try:
            if not self.probe_library.store_gain(path, float(gain), target_lufs, scan_version, convergence_db, checked=checked, windows=windows):
                return False
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_gain: saving the measured gain to the library", _swallowed_error)
            return False
        key = _hub_key(path)
        with self._lock:
            held = self._memo.get(key)
            self._memo[key] = {"gain": float(gain), "target_lufs": target_lufs, "scan_version": scan_version,
                               "convergence_db": convergence_db,
                               "manual_gain_db": held["manual_gain_db"] if held else None,
                               "width": held["width"] if held else None, "height": held["height"] if held else None, "at": time.monotonic()}
        return True

    def note_owner_gain(self, path, total_db):
        """Replaces the file's saved gain with the owner's chosen total dB and marks it as the owner's. Returns False when the library cannot key the file, so the caller can keep the value elsewhere."""
        try:
            if not self.probe_library.store_owner_gain(path, total_db):
                return False
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_owner_gain: saving the owner's gain to the library", _swallowed_error)
            return False
        with self._lock:
            held = self._memo.get(_hub_key(path)) or {}
            self._memo[_hub_key(path)] = {"gain": 10 ** (float(total_db) / 20.0), "target_lufs": None,
                                          "scan_version": _OWNER_GAIN_SCAN_VERSION, "convergence_db": None, "manual_gain_db": 0.0,
                                          "width": held.get("width"), "height": held.get("height"), "at": time.monotonic()}
        return True

    def read_windows(self, path):
        """The sample windows behind the file's scanned gain as (place in the file as a fraction, LUFS) pairs; empty when none are on record, as for a gain scanned before windows were kept."""
        try:
            return self.probe_library.gain_windows(path)
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.read_windows: reading the gain's sample windows from the library", _swallowed_error)
            return []

    def is_checked(self, path):
        """True when the file's scanned gain was double-checked with extra scans; the owner's own gain counts as checked."""
        try:
            if self.read(path)["scan_version"] == _OWNER_GAIN_SCAN_VERSION:
                return True
            return bool(self.probe_library.gain_is_checked(path))
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.is_checked: reading the gain check mark from the library", _swallowed_error)
            return False

    def mark_checked(self, path):
        """Marks the file's scanned gain as double-checked. Returns False when the library cannot key the file."""
        try:
            return bool(self.probe_library.mark_gain_checked(path))
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.mark_checked: saving the gain check mark to the library", _swallowed_error)
            return False

    def read_group(self, group_key):
        """Returns the saved gain of a multi-part title as a dict: anchor_path, gain, target_lufs, scan_version and convergence_db; None when the title has none on record."""
        try:
            row = self.probe_library.library.part_group_gain_row(group_key)
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.read_group: reading a multi-part title's gain from the library", _swallowed_error)
            return None
        if not row or row[1] is None or row[1] <= 0:
            return None
        return {"anchor_path": row[0], "gain": float(row[1]), "target_lufs": row[2], "scan_version": row[3], "convergence_db": row[4]}

    def note_group(self, group_key, anchor_path, gain, target_lufs=None, scan_version=None, convergence_db=None):
        """Records the gain of a multi-part title with the settings it was measured under, replacing the one on record. Returns False when the gain cannot be saved, so the caller can say so."""
        if not group_key or not anchor_path or not gain or gain <= 0:
            return False
        try:
            return bool(self.probe_library.library.store_part_group_gain(group_key, str(anchor_path), float(gain), target_lufs, scan_version, convergence_db))
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_group: saving a multi-part title's gain to the library", _swallowed_error)
            return False

    def forget(self, path):
        """Drops what the hub remembers about a path because the file changed on disk; the next look-up re-keys the file from disk. The library rows stay."""
        key = _hub_key(path)
        with self._lock:
            self._memo.pop(key, None)
            self._must_resolve.add(key)

    def known_paths(self):
        """The paths this hub has been asked about, as the caller gave them."""
        with self._lock:
            return list(self._paths.values())


_shared_gain_hub = None
_shared_gain_hub_lock = threading.Lock()


def shared_gain_hub() -> "GainHub":
    """The one process-wide GainHub on the shared probe library."""
    global _shared_gain_hub
    if _shared_gain_hub is None:
        with _shared_gain_hub_lock:
            if _shared_gain_hub is None:
                _shared_gain_hub = GainHub(shared_probe_library())
    return _shared_gain_hub


# ============================================================================
# SECTION: File facts hub (caption source, playability verdicts and loudness retry timers)
# ============================================================================


class FileFactsHub:
    """Answers one file at a time: looks the file's caption source, playability verdicts and loudness retry timers up in the library when asked, and remembers the answer; nothing is read in advance."""

    def __init__(self, probe_library: "ProbeLibrary"):
        self.probe_library = probe_library
        self._lock = threading.RLock()
        self._memo = {}
        self._paths = {}

    @staticmethod
    def _blank():
        record = {name: None for name in FILE_FACT_COLUMNS}
        record["at"] = time.monotonic()
        return record

    def _lookup(self, key, path):
        """Returns the remembered record for a path, asking the library once; the file is re-keyed from disk first, so a file that changed since it was last seen has no record; a path with nothing on record is asked again after the recheck time."""
        with self._lock:
            held = self._memo.get(key)
        now = time.monotonic()
        if held is not None:
            if any(held[name] is not None for name in FILE_FACT_COLUMNS) or now - held["at"] < _GAIN_MISS_RECHECK_SECONDS:
                return held
        record = self._blank()
        try:
            content_key = self.probe_library.keys.resolve(path)
            entry = self.probe_library.library.get_entry(content_key) if content_key is not None else None
            if entry:
                for name in FILE_FACT_COLUMNS:
                    record[name] = entry.get(name)
        except Exception as _swallowed_error:
            _log_swallowed("FileFactsHub._lookup: reading the file's facts row from the library", _swallowed_error)
        with self._lock:
            self._paths[key] = str(path)
            self._memo[key] = record
        return record

    def read(self, path):
        """Returns the file's stored facts as a dict keyed by FILE_FACT_COLUMNS; a field with nothing on record is None, and the two confirmed_* fields read as True or False."""
        record = self._lookup(_hub_key(path), path)
        facts = {name: record[name] for name in FILE_FACT_COLUMNS}
        facts["confirmed_unplayable"] = bool(facts["confirmed_unplayable"])
        facts["confirmed_no_audio"] = bool(facts["confirmed_no_audio"])
        return facts

    def note(self, path, **facts):
        """Records facts for a file. Returns False when the library cannot key the file, so the caller can keep the value elsewhere."""
        stored = {name: (int(value) if isinstance(value, bool) else value) for name, value in facts.items()}
        try:
            if not self.probe_library.store_file_facts(path, **stored):
                return False
        except Exception as _swallowed_error:
            _log_swallowed("FileFactsHub.note: saving the file's facts to the library", _swallowed_error)
            return False
        self._remember(path, stored)
        return True

    def clear(self, path, *names):
        """Sets the named facts back to nothing on record. Returns False when the library cannot key the file."""
        return self.note(path, **{name: None for name in names})

    def add_unplayable_strike(self, path, reason, threshold):
        """Adds one playability strike; returns the new strike count, or None when the library cannot key the file so the caller can keep the count elsewhere."""
        try:
            count = self.probe_library.add_unplayable_strike(path, reason, threshold)
        except Exception as _swallowed_error:
            _log_swallowed("FileFactsHub.add_unplayable_strike: saving the strike to the library", _swallowed_error)
            return None
        if count is None:
            return None
        update = {"unplayable_strikes": count, "unplayable_reason": reason}
        if count >= threshold:
            update["confirmed_unplayable"] = 1
        self._remember(path, update)
        return count

    def pan_scan_shots(self, path, axis):
        """The saved Pan & Scan shot list for one axis of a file, read from the same library row as the file's other facts, as a list of {"sig", "offset"} dicts; [] when none is saved or the library cannot be read."""
        # WARNING: the shot lists are long and the player keeps its own working copy, so they are never held in this hub's memo.
        try:
            return self.probe_library.get_pan_scan_axis(path, axis)
        except Exception as _swallowed_error:
            _log_swallowed("FileFactsHub.pan_scan_shots: reading the file's saved shots from the library", _swallowed_error)
            return []

    def note_pan_scan_shots(self, path, axis, shots):
        """Saves one axis's Pan & Scan shot list for a file in the same library row as its other facts, keeping the other axis; nothing is trimmed. Returns False when the library cannot key the file or the save fails, so the caller can say so."""
        try:
            return bool(self.probe_library.store_pan_scan_axis(path, axis, shots))
        except Exception as _swallowed_error:
            _log_swallowed("FileFactsHub.note_pan_scan_shots: saving the file's shots to the library", _swallowed_error)
            return False

    def _remember(self, path, update):
        key = _hub_key(path)
        with self._lock:
            held = self._memo.get(key)
            if held is None:
                held = self._blank()
                self._memo[key] = held
            held.update(update)
            held["at"] = time.monotonic()

    def forget(self, path):
        """Drops what the hub remembers about a path because the file changed on disk; the next look-up re-keys the file from disk and finds that file's own record. The library rows stay."""
        key = _hub_key(path)
        with self._lock:
            self._memo.pop(key, None)

    def known_paths(self):
        """The paths this hub has been asked about, as the caller gave them."""
        with self._lock:
            return list(self._paths.values())


_shared_file_facts_hub = None
_shared_file_facts_hub_lock = threading.Lock()


def shared_file_facts_hub() -> "FileFactsHub":
    """The one process-wide FileFactsHub on the shared probe library."""
    global _shared_file_facts_hub
    if _shared_file_facts_hub is None:
        with _shared_file_facts_hub_lock:
            if _shared_file_facts_hub is None:
                _shared_file_facts_hub = FileFactsHub(shared_probe_library())
    return _shared_file_facts_hub


# ============================================================================
# SECTION: Play-order hub (a random order dealt once, with no repeats until every track has played)
# ============================================================================


class PlayOrderHub:
    """Keeps one named list's play order in the library: dealt at random once, kept until the list changes, and a track airs again only after every active track in the list has aired."""

    def __init__(self, library: "MediaLibrary", rng=None):
        self.library = library
        self._rng = rng or random.Random()

    def _rows(self, list_id):
        return self.library._conn.execute(
            "SELECT path_key, path, position, aired, active FROM hub_play_order WHERE list_id = ? ORDER BY position, path_key",
            (list_id,)).fetchall()

    def _write_order(self, list_id, ordered_rows):
        """Rewrites positions so the given rows (path_key, path, aired) are numbered in order; rows already stored and not given keep their place at the end."""
        conn = self.library._conn
        for position, (key, path, aired) in enumerate(ordered_rows):
            conn.execute(
                "INSERT INTO hub_play_order (list_id, path_key, path, position, aired, active) VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(list_id, path_key) DO UPDATE SET path = excluded.path, position = excluded.position, "
                "aired = excluded.aired, active = 1", (list_id, key, path, position, aired))

    def sync(self, list_id, paths):
        """Makes the stored order match the list's current tracks: a track that stays keeps its place and its aired mark, a new track is dealt into the part of the order that has not aired yet, and a removed track is set aside with its history kept."""
        wanted = {}
        for path in paths:
            wanted.setdefault(_hub_key(path), path)
        with self.library._lock:
            rows = self._rows(list_id)
            known = {row[0]: row for row in rows}
            active_keys = {row[0] for row in rows if row[4]}
            if set(wanted) == active_keys and all(row[1] == wanted[row[0]] for row in rows if row[4]):
                return
            kept = [row for row in rows if row[4] and row[0] in wanted]
            aired = [(row[0], wanted[row[0]], 1) for row in kept if row[3]]
            unaired = [(row[0], wanted[row[0]], 0) for row in kept if not row[3]]
            returning = [(key, wanted[key], known[key][3]) for key in wanted if key in known and not known[key][4]]
            fresh = [(key, wanted[key], 0) for key in wanted if key not in known]
            for item in returning + fresh:
                slot = self._rng.randint(0, len(unaired)) if not item[2] else None
                if slot is None:
                    aired.append(item)
                else:
                    unaired.insert(slot, item)
            with self.library._conn:
                for row in rows:
                    if row[4] and row[0] not in wanted:
                        self.library._conn.execute(
                            "UPDATE hub_play_order SET active = 0 WHERE list_id = ? AND path_key = ?", (list_id, row[0]))
                self._write_order(list_id, aired + unaired)

    def next_track(self, list_id, usable=None):
        """Returns the next track path in the dealt order and marks it aired; when every active track has aired a new pass is dealt first. Returns None when no unaired track is usable."""
        with self.library._lock:
            rows = [row for row in self._rows(list_id) if row[4]]
            if not rows:
                return None
            conn = self.library._conn
            state = conn.execute("SELECT pass_no, last_key FROM hub_play_state WHERE list_id = ?", (list_id,)).fetchone()
            pass_no, last_key = (state[0], state[1]) if state else (1, None)
            if all(row[3] for row in rows):
                shuffled = [(row[0], row[1], 0) for row in rows]
                self._rng.shuffle(shuffled)
                if len(shuffled) > 1 and shuffled[0][0] == last_key:
                    swap = self._rng.randint(1, len(shuffled) - 1)
                    shuffled[0], shuffled[swap] = shuffled[swap], shuffled[0]
                pass_no += 1
                with conn:
                    self._write_order(list_id, shuffled)
                rows = [row for row in self._rows(list_id) if row[4]]
            for key, path, _position, aired, _active in rows:
                if aired:
                    continue
                if usable is not None and not usable(path):
                    continue
                with conn:
                    conn.execute("UPDATE hub_play_order SET aired = 1 WHERE list_id = ? AND path_key = ?", (list_id, key))
                    conn.execute(
                        "INSERT INTO hub_play_state (list_id, pass_no, last_key) VALUES (?, ?, ?) "
                        "ON CONFLICT(list_id) DO UPDATE SET pass_no = excluded.pass_no, last_key = excluded.last_key",
                        (list_id, pass_no, key))
                return path
            return None

    def state(self, list_id):
        """The list's pass number and how many of its active tracks have aired this pass, for logs."""
        with self.library._lock:
            rows = [row for row in self._rows(list_id) if row[4]]
            row = self.library._conn.execute("SELECT pass_no FROM hub_play_state WHERE list_id = ?", (list_id,)).fetchone()
            return {"pass_no": row[0] if row else 1, "aired": sum(1 for r in rows if r[3]), "total": len(rows)}

    def order(self, list_id):
        """The active tracks in their dealt order as (path, aired)."""
        with self.library._lock:
            return [(row[1], bool(row[3])) for row in self._rows(list_id) if row[4]]


_shared_play_order_hub = None
_shared_play_order_hub_lock = threading.Lock()


def shared_play_order_hub() -> "PlayOrderHub":
    """The one process-wide PlayOrderHub on the shared library."""
    global _shared_play_order_hub
    if _shared_play_order_hub is None:
        with _shared_play_order_hub_lock:
            if _shared_play_order_hub is None:
                _shared_play_order_hub = PlayOrderHub(shared_probe_library().library)
    return _shared_play_order_hub


# ============================================================================
# SECTION: Schedule rows (the hub's ordered list per channel)
# ============================================================================

# WARNING: the fallback must match HUB_ROW_GENERATION_START_TOLERANCE_SECONDS in performance_governor.py; it is used only when that module fails to import.
try:
    _ROW_GENERATION_START_TOLERANCE_SECONDS = float(_hub_governor.HUB_ROW_GENERATION_START_TOLERANCE_SECONDS)
except Exception:
    _ROW_GENERATION_START_TOLERANCE_SECONDS = 60.0

_GENERATION_MARK = "#g"

SCHEDULE_ROW_STATES = ("planned", "airing", "aired", "static")
_SCHEDULE_STATE_RANK = {"planned": 0, "airing": 1, "aired": 2, "static": 2}
_SCHEDULE_ROW_FIELDS = ("channel", "slot_key", "position", "path", "kind", "pass_no", "planned_start", "used_length", "length_source", "state")


class ScheduleHub:
    """Keeps each channel's ordered rows in the library: what is meant to play, when, for how long, and what has played."""
    # WARNING: a row that is airing, aired or static is history; no call here may change or remove it except mark() moving it forward, or aired rows and pass records are lost (R26) and a past slot moves (R18).

    def __init__(self, library: "MediaLibrary"):
        self.library = library

    @staticmethod
    def _as_dict(row):
        return dict(zip(_SCHEDULE_ROW_FIELDS, row))

    def put_rows(self, channel, slot_key, rows):
        """Replaces the slot's planned rows with the given ones; rows already airing, aired or static stay as they are and a new row for their position is skipped. A row without a pass number gets one more than the highest pass of that file already on air in this channel, and a continuation of a split show keeps its first part's pass. Returns (written, skipped)."""
        with self.library._lock:
            with self.library._conn as conn:
                return self._put_rows_in(conn, channel, slot_key, rows)

    @staticmethod
    def _bundle_history(conn, channel, bundle):
        """The round a bundle is in and the files already aired in it, read from the bundle's saved rows that are not planned."""
        round_no, aired = 1, set()
        for pass_no, path_key, row_files in conn.execute(
                "SELECT pass_no, path_key, row_files FROM hub_schedule_row WHERE channel = ? AND bundle = ? AND state != 'planned' "
                "ORDER BY (planned_start IS NULL), planned_start, slot_key, position", (channel, bundle)).fetchall():
            if int(pass_no) > round_no:
                round_no, aired = int(pass_no), set()
            aired.add(path_key)
            if row_files:
                try:
                    aired.update(_hub_key(p) for p in json.loads(row_files))
                except (ValueError, TypeError) as error:
                    log.warning("[HUB SCHEDULE] _bundle_history: could not read the files of a saved row of bundle %s on channel %s: %s", bundle, channel, error)
        return round_no, aired

    def _round_for_row(self, conn, channel, row, rounds):
        """The round number of one row: the bundle's current round, or the next one when every active file of the bundle has already aired in it."""
        # WARNING: a file added mid-round is active but not yet aired, so the round cannot end before it airs; the round comes only from saved rows, never from per-file counts.
        bundle = str(row["bundle"])
        if bundle not in rounds:
            rounds[bundle] = list(self._bundle_history(conn, channel, bundle))
        state = rounds[bundle]
        active = {_hub_key(p) for p in (row.get("bundle_files") or ())}
        if active and active <= state[1]:
            state[0] += 1
            state[1] = set()
        state[1].add(_hub_key(row["path"]))
        state[1].update(_hub_key(p) for p in (row.get("row_files") or ()))
        return state[0]

    def _put_rows_in(self, conn, channel, slot_key, rows):
        """put_rows inside a transaction the caller already holds, so a set switch and its rows are saved together or not at all."""
        now = time.time()
        written = skipped = 0
        conn.execute("DELETE FROM hub_schedule_row WHERE channel = ? AND slot_key = ? AND state = 'planned'", (channel, slot_key))
        held = dict(conn.execute(
            "SELECT position, pass_no FROM hub_schedule_row WHERE channel = ? AND slot_key = ?", (channel, slot_key)).fetchall())
        last_pass = dict(conn.execute(
            "SELECT path_key, MAX(pass_no) FROM hub_schedule_row WHERE channel = ? AND state != 'planned' GROUP BY path_key", (channel,)).fetchall())
        batch_pass = {}
        rounds = {}
        for row in rows:
            position = int(row["position"])
            key = _hub_key(row["path"])
            if position in held:
                batch_pass[key] = held[position]
                skipped += 1
                continue
            if row.get("pass_no") is not None:
                pass_no = int(row["pass_no"])
            elif row.get("continuation"):
                pass_no = batch_pass.get(key, last_pass.get(key, 1))
            elif row.get("bundle"):
                pass_no = self._round_for_row(conn, channel, row, rounds)
            else:
                pass_no = batch_pass.get(key, last_pass.get(key, 0)) + 1
            batch_pass[key] = pass_no
            files = row.get("row_files") or []
            conn.execute(
                "INSERT INTO hub_schedule_row (channel, slot_key, position, path_key, path, kind, pass_no, planned_start, "
                "used_length, length_source, state, updated_at, bundle, row_files, guide_entry) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?)",
                (channel, slot_key, position, key, row["path"], row.get("kind", "episode"),
                 pass_no, row.get("planned_start"), row.get("used_length"), row.get("length_source"), now,
                 row.get("bundle"), json.dumps(list(files)) if len(files) > 1 else None,
                 json.dumps(row["guide_entry"]) if row.get("guide_entry") else None))
            held[position] = pass_no
            written += 1
        return written, skipped

    @staticmethod
    def generation_key(block_key, generation):
        """The slot key of one generation of a block: the first generation is the block key itself, later ones add a suffix."""
        block_key = str(block_key)
        return block_key if int(generation) <= 1 else "%s%s%d" % (block_key, _GENERATION_MARK, int(generation))

    def _generations(self, conn, channel, block_key):
        block_key = str(block_key)
        prefix = block_key + _GENERATION_MARK
        found = {}
        for (slot,) in conn.execute("SELECT DISTINCT slot_key FROM hub_schedule_row WHERE channel = ?", (channel,)).fetchall():
            if slot == block_key:
                found[1] = slot
            elif slot.startswith(prefix) and slot[len(prefix):].isdigit():
                found[int(slot[len(prefix):])] = slot
        return found

    def current_slot_key(self, channel, block_key):
        """The slot key of the newest generation of the block that has rows, or the block key itself when it has none."""
        with self.library._lock:
            found = self._generations(self.library._conn, channel, block_key)
        return found[max(found)] if found else str(block_key)

    def set_info(self, channel, slot_key):
        """The label of one set of rows (mode, active or set aside, and where it was put aside), or None when it has no label."""
        with self.library._lock:
            found = self.library._conn.execute(
                "SELECT mode, state, stop_position, stop_epoch FROM hub_schedule_set WHERE channel = ? AND slot_key = ?", (channel, slot_key)).fetchone()
        return dict(zip(("mode", "state", "stop_position", "stop_epoch"), found)) if found else None

    def sets(self, channel, block_key):
        """Every labelled set of one block, oldest first, each with its slot key, mode, state and stopping point."""
        with self.library._lock:
            conn = self.library._conn
            found = self._generations(conn, channel, block_key)
            result = []
            for generation in sorted(found):
                info = self.set_info(channel, found[generation]) or {"mode": None, "state": None, "stop_position": None, "stop_epoch": None}
                result.append(dict(info, slot_key=found[generation], generation=generation))
        return result

    def place_rows(self, channel, block_key, rows, airing_position=None, now_epoch=None, start_tolerance=None, mode=None):
        """Writes a block's planned rows into its newest set. When the plan holds other files, or other start times, at positions that already airing, aired or static rows hold, or when the slot mode changed, it opens a new set: the old set is put aside with its unaired rows kept, its airing row is closed at the clock moment and the stopping point is recorded, and the new set gets the rows from the airing position on, the first starting at the clock moment, all in one saved change. Returns (slot_key, written, skipped, new_generation)."""
        # WARNING: rows already airing, aired or static are history (R26) and the old set's unaired rows are kept, never deleted (owner answer to Q1); only a clear of the whole app or one channel may delete. The close, the labels and the new rows are one transaction, so a failure leaves the old set exactly as it was.
        tolerance = _ROW_GENERATION_START_TOLERANCE_SECONDS if start_tolerance is None else float(start_tolerance)
        moment = time.time() if now_epoch is None else float(now_epoch)
        new_generation = False
        stamp = time.time()
        with self.library._lock:
            conn = self.library._conn
            found = self._generations(conn, channel, block_key)
            with conn:
                if not found:
                    slot_key = str(block_key)
                    if mode is not None:
                        conn.execute("INSERT OR REPLACE INTO hub_schedule_set (channel, slot_key, mode, state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                                     (channel, slot_key, str(mode), stamp, stamp))
                else:
                    newest = max(found)
                    slot_key = found[newest]
                    label = conn.execute("SELECT mode FROM hub_schedule_set WHERE channel = ? AND slot_key = ?", (channel, slot_key)).fetchone()
                    if label is None and mode is not None:
                        conn.execute("INSERT INTO hub_schedule_set (channel, slot_key, mode, state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                                     (channel, slot_key, str(mode), stamp, stamp))
                    mode_changed = label is not None and mode is not None and label[0] != str(mode)
                    held = {}
                    for position, path_key, start, used in conn.execute(
                            "SELECT position, path_key, planned_start, used_length FROM hub_schedule_row WHERE channel = ? AND slot_key = ? AND state != 'planned'",
                            (channel, slot_key)).fetchall():
                        held[int(position)] = (path_key, start, used)
                    conflict = False
                    for row in rows:
                        old = held.get(int(row["position"]))
                        if old is None:
                            continue
                        if old[0] != _hub_key(row["path"]):
                            conflict = True
                            break
                        if old[1] is not None and row.get("planned_start") is not None:
                            same_start = abs(float(old[1]) - float(row["planned_start"])) <= tolerance
                            same_end = (old[2] is not None and row.get("used_length") is not None
                                        and abs(float(old[1]) + float(old[2]) - float(row["planned_start"]) - float(row["used_length"])) <= tolerance)
                            if not (same_start or same_end):
                                conflict = True
                                break
                    if conflict or mode_changed:
                        new_generation = True
                        stopped = conn.execute("SELECT MAX(position) FROM hub_schedule_row WHERE channel = ? AND slot_key = ? AND state = 'airing'",
                                               (channel, slot_key)).fetchone()[0]
                        if stopped is None:
                            stopped = airing_position
                        conn.execute(
                            "UPDATE hub_schedule_row SET used_length = MAX(0.0, MIN(COALESCE(used_length, ? - planned_start), ? - planned_start)), "
                            "state = 'aired', updated_at = ? WHERE channel = ? AND slot_key = ? AND state = 'airing' AND planned_start IS NOT NULL",
                            (moment, moment, stamp, channel, slot_key))
                        conn.execute("UPDATE hub_schedule_row SET state = 'aired', updated_at = ? WHERE channel = ? AND slot_key = ? AND state = 'airing'",
                                     (stamp, channel, slot_key))
                        conn.execute("INSERT OR IGNORE INTO hub_schedule_set (channel, slot_key, mode, state, created_at, updated_at) VALUES (?, ?, 'unknown', 'active', ?, ?)",
                                     (channel, slot_key, stamp, stamp))
                        conn.execute("UPDATE hub_schedule_set SET state = 'set_aside', stop_position = ?, stop_epoch = ?, updated_at = ? WHERE channel = ? AND slot_key = ?",
                                     (stopped, moment, stamp, channel, slot_key))
                        slot_key = self.generation_key(block_key, newest + 1)
                        conn.execute("INSERT OR REPLACE INTO hub_schedule_set (channel, slot_key, mode, state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                                     (channel, slot_key, str(mode) if mode is not None else "unknown", stamp, stamp))
                        if airing_position is not None:
                            rows = [dict(r) for r in rows if int(r["position"]) >= int(airing_position)]
                            for row in rows:
                                if int(row["position"]) == int(airing_position) and row.get("planned_start") is not None and float(row["planned_start"]) < moment:
                                    if row.get("used_length") is not None:
                                        row["used_length"] = max(0.0, float(row["planned_start"]) + float(row["used_length"]) - moment)
                                    row["planned_start"] = moment
                written, skipped = self._put_rows_in(conn, channel, slot_key, rows)
        return slot_key, written, skipped, new_generation

    def put_aired_rows(self, channel, slot_key, rows, mode=None):
        """Saves rows of a stretch that has already passed, such as a block the station missed while it was off, as aired rows in a set of their own, in one saved change. Returns how many rows were written."""
        # WARNING: the rows go through the same round numbering as planned rows and are marked aired in the same transaction; a slot that already holds rows keeps them (R26).
        stamp = time.time()
        with self.library._lock:
            conn = self.library._conn
            with conn:
                if conn.execute("SELECT 1 FROM hub_schedule_row WHERE channel = ? AND slot_key = ? LIMIT 1", (channel, slot_key)).fetchone():
                    return 0
                conn.execute("INSERT OR IGNORE INTO hub_schedule_set (channel, slot_key, mode, state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                             (channel, slot_key, str(mode) if mode is not None else "unknown", stamp, stamp))
                written, _skipped = self._put_rows_in(conn, channel, slot_key, rows)
                conn.execute("UPDATE hub_schedule_row SET state = 'aired', updated_at = ? WHERE channel = ? AND slot_key = ? AND state = 'planned'",
                             (stamp, channel, slot_key))
        return written

    def rows(self, channel, slot_key=None):
        """The channel's rows in order, for one slot or for every slot."""
        sql = ("SELECT channel, slot_key, position, path, kind, pass_no, planned_start, used_length, length_source, state "
               "FROM hub_schedule_row WHERE channel = ?")
        args = [channel]
        if slot_key is not None:
            sql += " AND slot_key = ?"
            args.append(slot_key)
        sql += " ORDER BY planned_start IS NULL, planned_start, slot_key, position"
        with self.library._lock:
            return [self._as_dict(r) for r in self.library._conn.execute(sql, args).fetchall()]

    def rows_between(self, channel, start_epoch, end_epoch, now_epoch, full=False):
        """The channel's episode and movie rows that overlap a time span and are airing, aired or Static, plus the planned row that covers now_epoch; rows of a set that was put aside are left out. A Static row is the show the clock scheduled, so the guide draws it as that show. With full, commercial rows are returned too, only rows that are airing, aired or Static are returned, and every row carries guide_entry: the facts the guide draws its cell from (a dictionary, or None for a row saved before the hub kept them)."""
        # WARNING: with full, a planned row is never returned; the guide's future is not read from the hub yet.
        if full:
            kinds = "('episode', 'movie', 'commercial')"
            state_sql = "state IN ('airing', 'aired', 'static')"
            args = (channel, float(end_epoch), float(start_epoch))
        else:
            kinds = "('episode', 'movie')"
            state_sql = ("(state IN ('airing', 'aired', 'static') OR (state = 'planned' AND planned_start <= ? AND planned_start + COALESCE(used_length, 0) > ? "
                         "AND NOT EXISTS (SELECT 1 FROM hub_schedule_set s WHERE s.channel = hub_schedule_row.channel "
                         "AND s.slot_key = hub_schedule_row.slot_key AND s.state = 'set_aside')))")
            args = (channel, float(end_epoch), float(start_epoch), float(now_epoch), float(now_epoch))
        sql = ("SELECT channel, slot_key, position, path, kind, pass_no, planned_start, used_length, length_source, state, guide_entry "
               "FROM hub_schedule_row WHERE channel = ? AND kind IN " + kinds + " AND planned_start IS NOT NULL "
               "AND planned_start < ? AND planned_start + COALESCE(used_length, 0) > ? AND " + state_sql + " "
               "ORDER BY planned_start, slot_key, position")
        with self.library._lock:
            found = self.library._conn.execute(sql, args).fetchall()
        rows = []
        for record in found:
            row = self._as_dict(record[:10])
            if full:
                row["guide_entry"] = self._decode_guide_entry(record[10])
            rows.append(row)
        return rows

    @staticmethod
    def _decode_guide_entry(raw):
        """The dictionary saved in a row's guide_entry column, or None when the column is empty or cannot be read."""
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except Exception as _swallowed_error:
            _log_swallowed("ScheduleHub._decode_guide_entry: the row's guide entry is unreadable, the cell is drawn from the file alone", _swallowed_error)
            return None
        return value if isinstance(value, dict) else None

    def replace_future_rows(self, channel, rows):
        """Saves the channel's projected future rows, replacing the ones saved before, in one saved change; each row is a dictionary with slot_key, position, path, kind, planned_start, used_length and guide_entry. Returns how many rows were written."""
        # WARNING: these rows are a plan worked out from the prebuilt order, never history: they live in their own table, so no airing, aired, static or planned row of the schedule is read, changed or removed here (R26), and nothing that asks "what is on air" can pick one up.
        stamp = time.time()
        with self.library._lock:
            with self.library._conn as conn:
                conn.execute("DELETE FROM hub_future_row WHERE channel = ?", (channel,))
                written = 0
                for row in rows:
                    if not row.get("path") or row.get("planned_start") is None or float(row.get("used_length") or 0) <= 0:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO hub_future_row (channel, slot_key, position, path, kind, planned_start, used_length, guide_entry, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (channel, str(row["slot_key"]), int(row["position"]), row["path"], row.get("kind", "episode"),
                         float(row["planned_start"]), float(row["used_length"]),
                         json.dumps(row["guide_entry"]) if row.get("guide_entry") else None, stamp))
                    written += 1
        return written

    def future_rows_between(self, channel, start_epoch, end_epoch):
        """The channel's projected future rows that overlap a time span, in start order, each with the facts the guide draws its cell from (a dictionary, or None when unreadable)."""
        sql = ("SELECT channel, slot_key, position, path, kind, planned_start, used_length, guide_entry FROM hub_future_row "
               "WHERE channel = ? AND planned_start < ? AND planned_start + used_length > ? ORDER BY planned_start, slot_key, position")
        with self.library._lock:
            found = self.library._conn.execute(sql, (channel, float(end_epoch), float(start_epoch))).fetchall()
        return [{"channel": r[0], "slot_key": r[1], "position": r[2], "path": r[3], "kind": r[4], "planned_start": r[5],
                 "used_length": r[6], "guide_entry": self._decode_guide_entry(r[7])} for r in found]

    def future_signature(self, channel):
        """A small value that changes whenever the channel's projected future rows are replaced, so the guide can tell its saved future is out of date."""
        with self.library._lock:
            count, newest = self.library._conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM hub_future_row WHERE channel = ?", (channel,)).fetchone()
        return (int(count or 0), float(newest or 0.0))

    def past_signature(self, channel):
        """A small value that changes whenever a row of the channel that is not planned is added or moves forward, so the guide can tell its saved past is out of date."""
        with self.library._lock:
            count, newest = self.library._conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM hub_schedule_row WHERE channel = ? AND state != 'planned'", (channel,)).fetchone()
        return (int(count or 0), float(newest or 0.0))

    def at(self, channel, epoch):
        """The channel's row that covers one clock moment (planned start up to planned start plus length), or None; a row that has aired, is airing or went to Static is preferred over a planned one, then the latest start."""
        sql = ("SELECT channel, slot_key, position, path, kind, pass_no, planned_start, used_length, length_source, state "
               "FROM hub_schedule_row WHERE channel = ? AND planned_start IS NOT NULL AND planned_start <= ? "
               "AND planned_start + COALESCE(used_length, 0) > ? "
               "AND NOT (state = 'planned' AND EXISTS (SELECT 1 FROM hub_schedule_set s WHERE s.channel = hub_schedule_row.channel "
               "AND s.slot_key = hub_schedule_row.slot_key AND s.state = 'set_aside')) "
               "ORDER BY (state = 'planned'), planned_start DESC, updated_at DESC LIMIT 1")
        with self.library._lock:
            found = self.library._conn.execute(sql, (channel, float(epoch), float(epoch))).fetchone()
        return self._as_dict(found) if found else None

    def mark(self, channel, slot_key, position, state, used_length=None, length_source=None):
        """Moves a row forward (planned, airing, aired or static); a row never moves back. Returns whether it changed."""
        if state not in _SCHEDULE_STATE_RANK:
            return False
        with self.library._lock:
            conn = self.library._conn
            row = conn.execute("SELECT state FROM hub_schedule_row WHERE channel = ? AND slot_key = ? AND position = ?",
                               (channel, slot_key, position)).fetchone()
            if row is None or _SCHEDULE_STATE_RANK[state] <= _SCHEDULE_STATE_RANK[row[0]]:
                return False
            with conn:
                conn.execute(
                    "UPDATE hub_schedule_row SET state = ?, used_length = COALESCE(?, used_length), "
                    "length_source = COALESCE(?, length_source), updated_at = ? WHERE channel = ? AND slot_key = ? AND position = ?",
                    (state, used_length, length_source, time.time(), channel, slot_key, position))
            return True

    def set_length(self, channel, slot_key, position, used_length, length_source):
        """Changes the length of a row that is still ahead; an airing, aired or static row never changes. Returns whether it changed."""
        with self.library._lock:
            conn = self.library._conn
            with conn:
                cur = conn.execute(
                    "UPDATE hub_schedule_row SET used_length = ?, length_source = ?, updated_at = ? "
                    "WHERE channel = ? AND slot_key = ? AND position = ? AND state = 'planned'",
                    (used_length, length_source, time.time(), channel, slot_key, position))
            return cur.rowcount > 0

    def mark_airing_static(self, channel, path):
        """Marks the channel's newest airing row for this file as static, the slot that went to Static because the file would not play. Returns how many rows changed."""
        # WARNING: only the newest airing row of the file changes; an older row of the same file that was left airing by an earlier block is history and must not become Static.
        key = _hub_key(path)
        with self.library._lock:
            with self.library._conn:
                cur = self.library._conn.execute(
                    "UPDATE hub_schedule_row SET state = 'static', updated_at = ? WHERE channel = ? AND path_key = ? AND state = 'airing' "
                    "AND rowid = (SELECT rowid FROM hub_schedule_row WHERE channel = ? AND path_key = ? AND state = 'airing' "
                    "ORDER BY (planned_start IS NULL), planned_start DESC, updated_at DESC LIMIT 1)",
                    (time.time(), channel, key, channel, key))
            return cur.rowcount

    def close_passed_rows(self, channel, now_epoch, current_slot, current_position):
        """Marks as aired every row of the channel that the clock has left behind in another block: a row still airing that is not the one airing now, and a planned row of an active set whose start and length ended before now_epoch. Rows of a set that was put aside and rows without a start time stay as they are. Returns (rows closed that were airing, rows closed that were planned)."""
        # WARNING: nothing is deleted and no row moves back (R26); a row of the block on air is left to the caller's own marking, and a set that was put aside keeps its unaired rows (owner answer to Q1).
        moment = float(now_epoch)
        stamp = time.time()
        with self.library._lock:
            conn = self.library._conn
            with conn:
                airing = conn.execute(
                    "UPDATE hub_schedule_row SET used_length = CASE WHEN planned_start IS NULL THEN used_length "
                    "ELSE MAX(0.0, MIN(COALESCE(used_length, ? - planned_start), ? - planned_start)) END, state = 'aired', updated_at = ? "
                    "WHERE channel = ? AND state = 'airing' AND NOT (slot_key = ? AND position = ?)",
                    (moment, moment, stamp, channel, str(current_slot), int(current_position))).rowcount
                planned = conn.execute(
                    "UPDATE hub_schedule_row SET state = 'aired', updated_at = ? "
                    "WHERE channel = ? AND state = 'planned' AND slot_key != ? AND planned_start IS NOT NULL "
                    "AND planned_start + COALESCE(used_length, 0) <= ? "
                    "AND NOT EXISTS (SELECT 1 FROM hub_schedule_set s WHERE s.channel = hub_schedule_row.channel "
                    "AND s.slot_key = hub_schedule_row.slot_key AND s.state = 'set_aside')",
                    (stamp, channel, str(current_slot), moment)).rowcount
        return airing, planned

    def counts(self, channel):
        """How many rows the channel has in each state, for logs."""
        with self.library._lock:
            found = dict(self.library._conn.execute(
                "SELECT state, COUNT(*) FROM hub_schedule_row WHERE channel = ? GROUP BY state", (channel,)).fetchall())
        return {state: found.get(state, 0) for state in SCHEDULE_ROW_STATES}


_shared_schedule_hub = None
_shared_schedule_hub_lock = threading.Lock()


def shared_schedule_hub() -> "ScheduleHub":
    """The one process-wide ScheduleHub on the shared library."""
    global _shared_schedule_hub
    if _shared_schedule_hub is None:
        with _shared_schedule_hub_lock:
            if _shared_schedule_hub is None:
                _shared_schedule_hub = ScheduleHub(shared_probe_library().library)
    return _shared_schedule_hub


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = [
    "resolve_media_library_dir", "default_db_path", "MediaLibrary", "PathKeyResolver",
    "compute_identity_key", "HeaderProbeResult", "combined_header_probe",
    "probe_duration_with_fallback", "PICTURE_BOX_DETECTOR_VERSION", "detect_picture_box",
    "ProbeLibrary",
    "BURNED_IN_DETECTOR_VERSION", "detect_burned_in_captions", "shared_probe_library",
    "GainHub", "shared_gain_hub", "FileFactsHub", "shared_file_facts_hub", "FILE_FACT_COLUMNS", "PlayOrderHub", "shared_play_order_hub", "ScheduleHub", "shared_schedule_hub", "SCHEDULE_ROW_STATES", "DurationHub", "shared_duration_hub", "DURATION_KIND_MEASURED", "DURATION_KIND_ESTIMATED",
    "ESTIMATE_BASIS_SERIES_AVERAGE", "ESTIMATE_BASIS_MOVIE_DEFAULT",
]
