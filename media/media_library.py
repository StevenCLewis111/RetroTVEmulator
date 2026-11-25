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
import math
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

try:
    import performance_governor as _governor
except Exception as _governor_import_error:
    _governor = None
    logging.getLogger(__name__).warning("[MEDIA LIBRARY] could not import performance_governor, using the fallback values written in media_library.py: %s", _governor_import_error)


def _time_critical():
    """True on the screen thread and the playback thread, which never wait for the database."""
    _check = getattr(_governor, "is_time_critical_thread", None)
    if _check is not None:
        return _check()
    return threading.current_thread() is threading.main_thread()

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
    loudness_off_gain_db REAL,      -- the owner's own dB for this file while Loudness Scan is off; no scanned gain is mixed in; None means none set
    gain_checked        INTEGER,    -- 1 once the scanned gain was double-checked with extra scans; a new scan result sets it back to 0
    gain_windows        TEXT,       -- JSON [[place in the file as a fraction, measured LUFS], ...]: every sample window behind the saved scanned gain
    audio_track_id      INTEGER,    -- the owner's chosen audio track: VLC's track id
    audio_track_name    TEXT,       -- the owner's chosen audio track: VLC's label
    audio_track_index   INTEGER,    -- the owner's chosen audio track: its place among the file's audio tracks, 0 = first
    learn_series_key    TEXT,       -- loudness learning: the series this file belongs to
    learn_change_db     REAL,       -- loudness learning: how many dB the owner moved this file from its own scan
    learn_change_at     REAL,       -- loudness learning: when that change was made (seconds since 1970)
    learn_baseline_db   REAL,       -- loudness learning: the file's own scanned dB when the owner first changed it
    learn_applied_db    REAL,       -- loudness learning: the series offset added to the file's saved scanned gain
    learn_slider_db     REAL,       -- loudness learning: the offset in dB the volume kept while watching this episode asked for
    learn_slider_at     REAL,       -- loudness learning: when that episode was watched (seconds since 1970)
    learn_watch_volume  REAL,       -- loudness learning: the volume slider setting kept for most of the last watch of this file
    learn_watch_at      REAL,       -- loudness learning: when that watch ended (seconds since 1970)
    track_gains         TEXT,       -- JSON {"primary": audio track the saved gain was measured on, "version": scan version, "gains": {"track number": gain}}: the scanned gain of every audio track

    captions_blob       BLOB,       -- zlib-compressed cue text
    captions_source     TEXT,       -- "embedded" | "sidecar" | "asr"
    captions_reason     TEXT,       -- e.g. "no_speech" | "asr_unavailable"

    pan_scan_memory     TEXT,       -- JSON, optional

    caption_kind        TEXT,       -- where the overlay captions come from: "embedded" | "sidecar"
    caption_srt_path    TEXT,       -- the .srt file those captions are read from
    saved_captions_file TEXT,       -- the file in main/saved captions holding this file's saved captions (saved_captions.py)
    saved_captions_covered REAL,    -- seconds of the file those saved captions cover
    saved_captions_complete INTEGER, -- 1 once they cover the whole file
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

CREATE TABLE IF NOT EXISTS hub_plan_state (
    channel        TEXT NOT NULL,
    name           TEXT NOT NULL,      -- what the planner keeps: a Visualizer's lineup of the day, a Marathon's clock anchor
    value          TEXT NOT NULL,      -- JSON
    updated_at     REAL NOT NULL,
    PRIMARY KEY (channel, name)
);

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

CREATE TABLE IF NOT EXISTS context_gain (
    content_key    TEXT NOT NULL,
    context        TEXT NOT NULL,      -- regular is never stored here; profile:<slot> for a server profile viewed on this computer, user:<host>:<slot> for a server streamed to this computer
    total_db       REAL NOT NULL,      -- the owner's chosen total dB for this file in this context only
    series_key     TEXT,               -- the show the file belongs to, for the context's own loudness learning
    baseline_db    REAL,               -- the file's own scanned dB (no learned offset) when the owner first changed it here
    change_db      REAL,               -- how far the owner moved it from that scan; None when the change is not counted
    change_at      REAL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (content_key, context)
);

CREATE TABLE IF NOT EXISTS context_off_gain (
    content_key    TEXT NOT NULL,
    context        TEXT NOT NULL,      -- regular, profile:<slot> or user:<host>:<slot>
    off_db         REAL NOT NULL,      -- the owner's own dB for this file in this context while Loudness Scan is off
    updated_at     REAL NOT NULL,
    PRIMARY KEY (content_key, context)
);

CREATE INDEX IF NOT EXISTS idx_context_gain_series ON context_gain(context, series_key);

CREATE TABLE IF NOT EXISTS hub_part_group_gain (
    group_key      TEXT PRIMARY KEY,   -- folder and title of a multi-part movie, lower case
    anchor_path    TEXT NOT NULL,      -- the part that was scanned; the other parts reuse its gain
    gain           REAL NOT NULL,
    target_lufs    REAL,
    scan_version   INTEGER,
    convergence_db REAL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library_once (
    name           TEXT PRIMARY KEY,   -- a one-time change to the stored rows that has been made
    done_at        REAL NOT NULL
);
"""


_LIBRARY_ADDED_COLUMNS = (
    ("estimated_duration", "REAL"),
    ("estimated_at", "REAL"),
    ("estimate_basis", "TEXT"),
    ("gain_convergence_db", "REAL"),
    ("manual_gain_db", "REAL"),
    ("loudness_off_gain_db", "REAL"),
    ("gain_checked", "INTEGER"),
    ("gain_windows", "TEXT"),
    ("track_gains", "TEXT"),
    ("audio_track_id", "INTEGER"),
    ("audio_track_name", "TEXT"),
    ("audio_track_index", "INTEGER"),
    ("learn_series_key", "TEXT"),
    ("learn_change_db", "REAL"),
    ("learn_change_at", "REAL"),
    ("learn_baseline_db", "REAL"),
    ("learn_applied_db", "REAL"),
    ("learn_slider_db", "REAL"),
    ("learn_slider_at", "REAL"),
    ("learn_watch_volume", "REAL"),
    ("learn_watch_at", "REAL"),
    ("caption_kind", "TEXT"),
    ("caption_srt_path", "TEXT"),
    ("saved_captions_file", "TEXT"),
    ("saved_captions_covered", "REAL"),
    ("saved_captions_complete", "INTEGER"),
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


LIBRARY_BUSY = object()


class MediaLibrary:
    """Thin synchronous wrapper around the sqlite3 connection."""

    def __init__(self, db_path: str):
        if sqlite3 is None:
            raise ImportError("No module named 'sqlite3' (the persistent media library needs it; add sqlite3 to the build)")
        self.db_path = db_path
        self._lock = threading.RLock()
        self._screen_memo = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA)
        self._ensure_library_columns()
        self._ensure_schedule_row_columns()
        self._conn.commit()
        self._clear_peak_held_content_gains()

    def _clear_peak_held_content_gains(self):
        """Once per library (owner 2026-11-24: "if the dialogue is the same volume as the tv series, the explosions will be as loud as they are meant to be"): shows and movies are no longer held under the true-peak ceiling, so every stored show or movie gain the ceiling held back more than 1 dB is cleared and measured again by the new rule; the file plays with its season's guess or unity until then. Gains it did not hold back, commercials, music and the owner's own gains are kept."""
        # WARNING: version 10 is the scan version these held-back gains were written under; the row's sample windows say what the station level wanted, as the GAIN APPLIED line reports it. A failure leaves every gain as it was and tries again on the next start.
        name = "peak_held_content_gains_cleared"
        try:
            if self._conn.execute("SELECT 1 FROM library_once WHERE name=?", (name,)).fetchone():
                return
            try:
                import performance_governor as _gov
                level = float(_gov.LOUDNESS_TARGET_LUFS) + float(_gov.LOUDNESS_STATION_LEVEL_DB)
            except Exception:
                level = -24.0 + 7.0
            cleared = []
            for key, gain, windows, learned in self._conn.execute(
                    "SELECT content_key, gain, gain_windows, learn_applied_db FROM library "
                    "WHERE gain > 0 AND gain_scan_version = 10 AND gain_windows IS NOT NULL").fetchall():
                values = [float(w[1]) for w in json.loads(windows) if isinstance(w, (list, tuple)) and len(w) >= 2]
                if len(values) < 2:
                    continue
                power = sum(10 ** (v / 10.0) for v in values) / len(values)
                if power <= 0:
                    continue
                wanted_db = level - 10 * math.log10(power)
                stored_db = 20 * math.log10(float(gain)) - float(learned or 0.0)
                if wanted_db - stored_db > 1.0:
                    cleared.append(key)
            for key in cleared:
                self._conn.execute("UPDATE library SET gain=NULL, gain_checked=0 WHERE content_key=?", (key,))
            groups = self._conn.execute("DELETE FROM hub_part_group_gain WHERE scan_version = 10").rowcount
            self._conn.execute("INSERT INTO library_once(name, done_at) VALUES (?,?)", (name, time.time()))
            self._conn.commit()
            log.info("[GAIN HUB] shows and movies are no longer held under the true-peak ceiling: %d held-back gain(s) and %d multi-part title gain(s) cleared to be measured again", len(cleared), groups)
        except Exception as _swallowed_error:
            _log_swallowed("_clear_peak_held_content_gains", _swallowed_error)
            try:
                self._conn.rollback()
            except Exception as _rollback_error:
                _log_swallowed("_clear_peak_held_content_gains: rollback", _rollback_error)

    def _ensure_library_columns(self):
        """Adds any library column an older database file lacks, so an existing library keeps all its rows."""
        # WARNING: _LIBRARY_ADDED_COLUMNS must list every column added after the first release, or an old library file fails on its first write.
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(library)").fetchall()}
        for name, sql_type in _LIBRARY_ADDED_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE library ADD COLUMN {name} {sql_type}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_library_learn_series ON library(learn_series_key)")

    def set_context_gain(self, content_key: str, context: str, total_db: float, series_key=None, baseline_db=None, change_db=None):
        """Saves the owner's total dB for one file in one context; nothing outside that context's row changes."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO context_gain(content_key, context, total_db, series_key, baseline_db, change_db, change_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(content_key, context) DO UPDATE SET total_db=excluded.total_db, "
                "series_key=excluded.series_key, baseline_db=COALESCE(context_gain.baseline_db, excluded.baseline_db), "
                "change_db=excluded.change_db, change_at=excluded.change_at, updated_at=excluded.updated_at",
                (content_key, context, float(total_db), series_key, baseline_db, change_db,
                 now if change_db is not None else None, now))
            self._conn.commit()

    def get_context_gain(self, content_key: str, context: str):
        """{'total_db', 'baseline_db'} for one file in one context, or None when the owner set none there."""
        with self._lock:
            row = self._conn.execute("SELECT total_db, baseline_db FROM context_gain WHERE content_key=? AND context=?",
                                     (content_key, context)).fetchone()
        return {"total_db": float(row[0]), "baseline_db": None if row[1] is None else float(row[1])} if row else None

    def set_context_off_gain(self, content_key: str, context: str, off_db: float):
        """Saves the owner's dB for one file in one context for use while Loudness Scan is off."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO context_off_gain(content_key, context, off_db, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(content_key, context) DO UPDATE SET off_db=excluded.off_db, updated_at=excluded.updated_at",
                (content_key, context, float(off_db), time.time()))
            self._conn.commit()

    def get_context_off_gain(self, content_key: str, context: str):
        """The saved Loudness-Scan-off dB for one file in one context, or None; on the screen thread while the database is busy, the answer last seen (the playback thread gets it only when it was prepared ahead, and otherwise reads)."""
        found = self._read_or_last_seen(("context_off_gain", content_key, context), None, lambda: self._conn.execute(
            "SELECT off_db FROM context_off_gain WHERE content_key=? AND context=?", (content_key, context)).fetchone())
        return None if found is None else float(found[0])

    def context_series_rows(self, context: str, series_key: str):
        """(change dB, change time) of every counted owner change in one series inside one context, oldest first; on the screen thread while the database is busy, the answer last seen (the playback thread gets it only when it was prepared ahead, and otherwise reads)."""
        rows = self._read_or_last_seen(("context_series", context, series_key), [], lambda: self._conn.execute(
            "SELECT change_db, change_at FROM context_gain WHERE context=? AND series_key=? AND change_db IS NOT NULL "
            "ORDER BY change_at", (context, series_key)).fetchall())
        return [(float(r[0]), float(r[1] or 0.0)) for r in rows]

    def series_learning_rows(self, series_key: str):
        """(change dB, change time) of every file of one series that holds an owner loudness change, oldest first; on the screen thread while the database is busy, the answer last seen (the playback thread gets it only when it was prepared ahead, and otherwise reads)."""
        rows = self._read_or_last_seen(("series_learning", series_key), [], lambda: self._conn.execute(
            "SELECT learn_change_db, learn_change_at FROM library WHERE learn_series_key=? AND learn_change_db IS NOT NULL "
            "ORDER BY learn_change_at", (series_key,)).fetchall())
        return [(float(r[0]), float(r[1] or 0.0)) for r in rows]

    def _read_or_last_seen(self, memo_key, never_seen, read):
        """Runs one read with the database taken (see _take_for_reading) and keeps its answer; when the database is busy on the screen thread, the answer last seen, or never_seen when there is none (see _take_for_reading for the playback thread)."""
        if not self._take_for_reading(memo_key):
            return self._screen_memo.get(memo_key, never_seen)
        try:
            found = read()
        finally:
            self._lock.release()
        self._remember_for_screen(memo_key, found)
        return found

    def _ensure_schedule_row_columns(self):
        """Adds the bundle, row_files and guide_entry columns to a hub_schedule_row table an older database file lacks, keeping every row."""
        # WARNING: the table must keep every existing row (R26); old rows read as having no bundle and no guide entry.
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(hub_schedule_row)").fetchall()}
        for name in ("bundle", "row_files", "guide_entry"):
            if name not in have:
                self._conn.execute(f"ALTER TABLE hub_schedule_row ADD COLUMN {name} TEXT")

    def _take_for_reading(self, memo_key=None):
        """Takes the database for a read: any background thread waits its turn; the screen thread only takes it when nobody else holds it and gets False otherwise; the playback thread gets False while the database is busy only when the answer for memo_key was prepared ahead, and otherwise waits its turn and reads it."""
        # WARNING: owner log 2026-11-18: the screen thread waited up to 3.7 s here behind the guide's background writes; it never waits, and reads the answer it last saw instead.
        # WARNING: owner 2026-11-21: "it precaches information so it never needs to look last min"; the playback thread uses what was prepared ahead, and a fact nobody prepared is read for real, never guessed, because a wrong fact takes the channel off schedule.
        if not _time_critical():
            self._lock.acquire()
            return True
        if self._lock.acquire(blocking=False):
            return True
        if threading.current_thread() is threading.main_thread() or (memo_key is not None and memo_key in self._screen_memo):
            return False
        self._lock.acquire()
        return True

    def _remember_for_screen(self, key, value):
        """Keeps the last answer of a read, for the screen thread to use while the database is busy."""
        if len(self._screen_memo) >= _SCREEN_MEMO_MAX:
            self._screen_memo.clear()
        self._screen_memo[key] = value

    def path_duration_row(self, norm_path: str):
        """The measured length and saved estimate for one keyed path, as (duration, estimated_duration), or None when the path has no library row; on the screen thread while the database is busy, the answer last seen, or LIBRARY_BUSY when none was."""
        if not self._take_for_reading(("duration", norm_path)):
            return self._screen_memo.get(("duration", norm_path), LIBRARY_BUSY)
        try:
            row = self._conn.execute(
                "SELECT l.duration, l.estimated_duration "
                "FROM path_keys p JOIN library l ON l.content_key = p.content_key WHERE p.norm_path = ?",
                (norm_path,),
            ).fetchone()
        finally:
            self._lock.release()
        self._remember_for_screen(("duration", norm_path), row)
        return row

    def _read_one_row(self, memo_name, norm_path, sql):
        """One row for one keyed path; on the screen thread while the database is busy, the row last seen, or LIBRARY_BUSY when none was."""
        # WARNING: owner log 2026-11-20: the screen thread waited 16 s for the database inside a channel change (file facts); like the length reads, it never waits and reads the answer it last saw instead.
        if not self._take_for_reading((memo_name, norm_path)):
            return self._screen_memo.get((memo_name, norm_path), LIBRARY_BUSY)
        try:
            row = self._conn.execute(sql, (norm_path,)).fetchone()
        finally:
            self._lock.release()
        self._remember_for_screen((memo_name, norm_path), row)
        return row

    def path_rows_many(self, norm_paths, columns_sql):
        """{norm_path: row} for every keyed path of the list, reading ROWS_PER_READ paths per turn of the database; paths with no row are left out. For background threads."""
        found = {}
        paths = [str(p) for p in norm_paths if p]
        for start in range(0, len(paths), _ROWS_PER_READ):
            chunk = paths[start:start + _ROWS_PER_READ]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT p.norm_path, {columns_sql} FROM path_keys p JOIN library l ON l.content_key = p.content_key "
                    f"WHERE p.norm_path IN ({marks})", chunk).fetchall()
            for row in rows:
                found[row[0]] = tuple(row[1:])
        return found

    def path_gain_row(self, norm_path: str):
        """The stored gain and picture-size fields for one keyed path as (gain, target_lufs, scan_version, convergence_db, manual_gain_db, width, height), or None when the path has no library row; LIBRARY_BUSY on a busy screen thread with nothing seen before."""
        return self._read_one_row("gain", norm_path,
                                  "SELECT l.gain, l.gain_target_lufs, l.gain_scan_version, l.gain_convergence_db, l.manual_gain_db, l.width, l.height "
                                  "FROM path_keys p JOIN library l ON l.content_key = p.content_key WHERE p.norm_path = ?")

    def path_facts_row(self, norm_path: str):
        """The stored caption-source, playability and loudness-retry fields for one keyed path, in FILE_FACT_COLUMNS order, or None when the path was never keyed; nothing is read from the file itself; LIBRARY_BUSY on a busy screen thread with nothing seen before."""
        columns = ", ".join("l." + name for name in FILE_FACT_COLUMNS)
        return self._read_one_row("facts", norm_path,
                                  f"SELECT {columns} FROM path_keys p JOIN library l ON l.content_key = p.content_key WHERE p.norm_path = ?")

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
        """The library row of one content key as a dictionary, or None; on the screen thread while the database is busy, the row last seen (None when none was)."""
        if not self._take_for_reading(("entry", content_key)):
            held = self._screen_memo.get(("entry", content_key))
            return dict(held) if held is not None else None
        try:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT * FROM library WHERE content_key=?", (content_key,)
            ).fetchone()
            self._conn.row_factory = None
        finally:
            self._lock.release()
        if row is None:
            self._remember_for_screen(("entry", content_key), None)
            return None
        d = dict(row)
        for json_field in ("audio_streams", "subtitle_streams", "picture_box", "pan_scan_memory"):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (TypeError, ValueError) as _swallowed_error:
                    _log_swallowed("MediaLibrary.get_entry: d[json_field] = json.loads(d[json_field])", _swallowed_error)
                    d[json_field] = None
        self._remember_for_screen(("entry", content_key), dict(d))
        return d

    def upsert_fields(self, content_key: str, size: Optional[int] = None, **fields):
        """Merges `fields` (column_name -> value) into the row for content_key, creating it if missing."""
        if not fields and size is None:
            return
        self._screen_memo.pop(("entry", content_key), None)
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
# WARNING: the fallback number below must match LIBRARY_GROWING_FILE_CHECK_DELAY_S in performance_governor.py; it is used only when that module fails to import.
_GROWING_FILE_CHECK_DELAY_S = _governor.LIBRARY_GROWING_FILE_CHECK_DELAY_S if _governor is not None else 0.05


# WARNING: the fallback number below must match SCREEN_LIBRARY_MEMO_MAX in performance_governor.py; it is used only when that module fails to import.
_SCREEN_MEMO_MAX = _governor.SCREEN_LIBRARY_MEMO_MAX if _governor is not None else 20000
# WARNING: the fallback number below must match HUB_FUTURE_ROWS_PER_TURN in performance_governor.py; it is used only when that module fails to import.
_FUTURE_ROWS_PER_TURN = _governor.HUB_FUTURE_ROWS_PER_TURN if _governor is not None else 200
# WARNING: the fallback number below must match PLAYBACK_STEP_ASIDE_MAX_S in performance_governor.py; it is used only when that module fails to import.
_PLAYBACK_STEP_ASIDE_MAX_S = _governor.PLAYBACK_STEP_ASIDE_MAX_S if _governor is not None else 5.0


def _step_aside_for_playback():
    """On a background thread, waits (at most PLAYBACK_STEP_ASIDE_MAX_S) while the playback thread is working on a step, so its next database turn comes first."""
    if _governor is None or _time_critical():
        return
    _until = time.monotonic() + _PLAYBACK_STEP_ASIDE_MAX_S
    while _governor.playback_needs_the_pc() and time.monotonic() < _until:
        time.sleep(0.05)

# WARNING: the fallback numbers below must match SCREEN_THREAD_SHARE_CHECK_WAIT_S and SCREEN_THREAD_SHARE_DOWN_FOR_S in performance_governor.py; they are used only when that module fails to import.
_SHARE_CHECK_WAIT_S = _governor.SCREEN_THREAD_SHARE_CHECK_WAIT_S if _governor is not None else 0.4
_SHARE_DOWN_FOR_S = _governor.SCREEN_THREAD_SHARE_DOWN_FOR_S if _governor is not None else 4.0
_share_down_until = {}
_share_down_lock = threading.Lock()


def _share_root_of(path):
    """The \\\\host\\share part of a network path, or None for a path on a local drive."""
    text = str(path)
    if not text.startswith("\\\\") or text.startswith("\\\\?\\"):
        return None
    parts = text.split("\\")
    return "\\".join(parts[:4]).lower() if len(parts) >= 4 else text.lower()


# WARNING: owner 2026-11-14 (CRT log, stalls of 1.1 to 1.7 s on the screen thread in _file_identity_key and get_cached > resolve > stat_for_screen_thread): one picture asks the share for the same file's size and time several times, and each cold ask waited up to _SHARE_CHECK_WAIT_S. A share file's answer is kept for _STAT_CACHE_S so the screen thread asks the share once per file per minute at most (and the warm threads fill it before the viewer arrives).
# WARNING: the fallback numbers below must match SCREEN_THREAD_STAT_CACHE_S and SCREEN_THREAD_STAT_CACHE_MAX in performance_governor.py; they are used only when that module fails to import.
_STAT_CACHE_S = _governor.SCREEN_THREAD_STAT_CACHE_S if _governor is not None else 60.0
_STAT_CACHE_MAX = _governor.SCREEN_THREAD_STAT_CACHE_MAX if _governor is not None else 2048
_stat_cache = {}
_stat_cache_lock = threading.Lock()


def _stat_cache_get(path):
    path = os.path.normpath(str(path)).lower()
    with _stat_cache_lock:
        hit = _stat_cache.get(path)
    if hit is not None and time.monotonic() - hit[0] < _STAT_CACHE_S:
        return hit[1]
    return None


def _stat_cache_put(path, st):
    path = os.path.normpath(str(path)).lower()
    with _stat_cache_lock:
        _stat_cache[path] = (time.monotonic(), st)
        if len(_stat_cache) > _STAT_CACHE_MAX:
            for old in sorted(_stat_cache, key=lambda k: _stat_cache[k][0])[:len(_stat_cache) - _STAT_CACHE_MAX]:
                _stat_cache.pop(old, None)


def _is_screen_critical_thread():
    """True on the screen thread, the playback thread and the thread that opens a channel (content-activate): none may sit inside a share call, because the first picture and the Static wait on them."""
    # WARNING: owner 2026-11-16 (CRT log, a channel change stuck 5 s in os.stat under _decide_picture_box_before_show > get_cached > resolve on content-activate): the bounded share check only covered the screen thread, so a tune on a share that had stopped answering waited out the whole network timeout and went to Static with the banner.
    if _UI_THREAD_IDENT is not None and threading.get_ident() == _UI_THREAD_IDENT:
        return True
    if _governor is not None and _governor.is_marked_time_critical_thread():
        return True
    return threading.current_thread().name == "content-activate"


def stat_for_screen_thread(path):
    """os.stat, except that a network path asked from the screen thread waits at most _SHARE_CHECK_WAIT_S and a share that did not answer reads as down for _SHARE_DOWN_FOR_S without being asked again; raises OSError for a share that is down. Any other thread, and any local path, is a plain os.stat."""
    # WARNING: owner 2026-11-13: the screen thread sat inside os.stat on a share that could not be reached (CRT log, _stored > get_cached > resolve), freezing Static, channel changes and the guide.
    root = _share_root_of(path) if _is_screen_critical_thread() else None
    _on_share = str(path or "").startswith("\\\\")
    if root is None:
        st = os.stat(path)
        if _on_share:
            _stat_cache_put(path, st)
        return st
    _cached_st = _stat_cache_get(path)
    if _cached_st is not None:
        return _cached_st
    now = time.monotonic()
    with _share_down_lock:
        if _share_down_until.get(root, 0.0) > now:
            raise OSError("share %s is not answering" % root)
    box = {}

    def _ask():
        try:
            box["st"] = os.stat(path)
            _stat_cache_put(path, box["st"])
            with _share_down_lock:
                _share_down_until.pop(root, None)
        except OSError as exc:
            box["err"] = exc
    worker = threading.Thread(target=_ask, daemon=True, name="ShareStat")
    worker.start()
    worker.join(_SHARE_CHECK_WAIT_S)
    if worker.is_alive():
        with _share_down_lock:
            _share_down_until[root] = time.monotonic() + _SHARE_DOWN_FOR_S
        raise OSError("share %s did not answer in %.1f s" % (root, _SHARE_CHECK_WAIT_S))
    if "err" in box:
        raise box["err"]
    return box["st"]


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
    except OSError as _swallowed_error:
        _log_swallowed("compute_identity_key: size_before = os.path.getsize(file_path)", _swallowed_error)
        return None
    if growing_check_delay > 0:
        time.sleep(growing_check_delay)
        try:
            size_after = os.path.getsize(file_path)
        except OSError as _swallowed_error:
            _log_swallowed("compute_identity_key: size_after = os.path.getsize(file_path)", _swallowed_error)
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
    except OSError as _swallowed_error:
        _log_swallowed("compute_identity_key: import media as _media", _swallowed_error)
        return None
    return f"{size:x}-{h.hexdigest()}"


# WARNING: the app registers its main (UI) thread here at startup. That thread must never open a file to hash it: a path with no saved key is queued for the background keyer and reads as "not keyed yet" until the key lands. Tests and tools never register one, so they hash inline as before.
_UI_THREAD_IDENT = None


def set_ui_thread(ident):
    """Registers the thread id that must never hash a file; None switches the guard off."""
    global _UI_THREAD_IDENT
    _UI_THREAD_IDENT = ident


class PathKeyResolver:
    """Return a stable content_key for a path, re-hashing only when its size or mtime changed."""

    def __init__(self, library: MediaLibrary):
        self.library = library
        self._key_lock = threading.Lock()
        self._key_queue = {}
        self._key_wake = threading.Event()
        self._key_thread = None

    def resolve(self, file_path: str) -> Optional[str]:
        norm_path = os.path.normpath(os.path.abspath(file_path)).lower()
        try:
            st = stat_for_screen_thread(file_path)
        except OSError as _swallowed_error:
            _log_swallowed("PathKeyResolver.resolve: st = os.stat(file_path)", _swallowed_error)
            return None
        cached = self.library.lookup_path_key(norm_path, st.st_size, st.st_mtime)
        if cached is not None:
            return cached
        if _is_screen_critical_thread():
            self._queue_background_key(norm_path, file_path)
            return None
        content_key = compute_identity_key(file_path)
        if content_key is None:
            return None
        try:
            st = os.stat(file_path)
        except OSError as _swallowed_error:
            _log_swallowed("PathKeyResolver.resolve: st = os.stat(file_path)", _swallowed_error)
            return None
        self.library.store_path_key(norm_path, st.st_size, st.st_mtime, content_key)
        return content_key

    def peek(self, file_path: str) -> Optional[str]:
        """The saved content_key for a path whose size and mtime still match, or None; never hashes a file and never queues one, so it is safe on any thread."""
        norm_path = os.path.normpath(os.path.abspath(file_path)).lower()
        try:
            st = stat_for_screen_thread(file_path)
        except OSError as _swallowed_error:
            _log_swallowed("PathKeyResolver.peek: st = os.stat(file_path)", _swallowed_error)
            return None
        return self.library.lookup_path_key(norm_path, st.st_size, st.st_mtime)

    def _queue_background_key(self, norm_path, file_path):
        """Hands a path to the one background keyer thread; never blocks."""
        with self._key_lock:
            self._key_queue.setdefault(norm_path, file_path)
            if self._key_thread is None or not self._key_thread.is_alive():
                self._key_thread = threading.Thread(target=self._key_worker, name="PathKeyer", daemon=True)
                self._key_thread.start()
        self._key_wake.set()

    def _key_worker(self):
        while True:
            with self._key_lock:
                item = None
                if self._key_queue:
                    norm_path = next(iter(self._key_queue))
                    item = (norm_path, self._key_queue.pop(norm_path))
            if item is None:
                self._key_wake.clear()
                if not self._key_wake.wait(timeout=30.0):
                    with self._key_lock:
                        if not self._key_queue:
                            self._key_thread = None
                            return
                continue
            try:
                self.resolve(item[1])
            except Exception as _swallowed_error:
                _log_swallowed("PathKeyResolver._key_worker: keying a queued file", _swallowed_error)
            time.sleep(0.01)


# ============================================================================
# SECTION: Combined header probe
# ============================================================================

_DURATION_RE = _re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_DIMS_RE = _re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_DAR_RE = _re.compile(r"DAR\s+(\d+):(\d+)")
_SUBTITLE_STREAM_RE = _re.compile(r"Stream #\d+:(\d+)(?:\((\w+)\))?[^\n]*:\s*Subtitle:\s*(\w+)")
_AUDIO_STREAM_RE = _re.compile(r"Stream #\d+:(\d+)(?:\((\w+)\))?[^\n]*:\s*Audio:\s*(\w+)")
_TEXT_SUBTITLE_CODECS = ("subrip", "ass", "ssa", "webvtt", "mov_text", "text")
_STREAM_START_RE = _re.compile(r"(?m)^\s*Stream #\d+:\d+")
_STREAM_DURATION_RE = _re.compile(r"(?m)^\s*DURATION\s*:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STILL_PICTURE_CODECS = ("mjpeg", "png", "bmp", "gif", "webp", "tiff")
# WARNING: the fallback number below must match PICTURE_ENDS_EARLY_MIN_SECONDS in performance_governor.py; it is used only when that module fails to import.
_PICTURE_ENDS_EARLY_MIN_S = _governor.PICTURE_ENDS_EARLY_MIN_SECONDS if _governor is not None else 30


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
    picture_seconds: Optional[float] = None
    picture_ends_early: bool = False
    error: Optional[str] = None
    stderr: Optional[str] = None


def _resolve_ffmpeg(ffmpeg_path: Optional[str]) -> str:
    if ffmpeg_path:
        return ffmpeg_path
    import media as _media
    return _media._resolve_ffmpeg_path()


def picture_length_from_header(err: str) -> Optional[float]:
    """The length in seconds that the header gives for the first real video stream's own DURATION tag; None when the header carries no such tag. A cover-art picture is not a video stream."""
    starts = [m.start() for m in _STREAM_START_RE.finditer(err or "")]
    for i, begin in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(err)
        block = err[begin:end]
        first_line = block.split("\n", 1)[0]
        if "Video:" not in first_line or "attached pic" in block:
            continue
        codec = first_line.split("Video:", 1)[1].strip().split(" ", 1)[0].strip(",").lower()
        if codec in _STILL_PICTURE_CODECS:
            continue
        m = _STREAM_DURATION_RE.search(block)
        if m:
            total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            return total if total > 0 else None
        return None
    return None


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

    out.picture_seconds = picture_length_from_header(err)
    if out.duration and out.picture_seconds and out.duration - out.picture_seconds >= _PICTURE_ENDS_EARLY_MIN_S:
        # WARNING: a file whose picture stops before its sound gets the length of its picture; a channel put in the part after it shows nothing while the sound plays (owner log 2026-11-06 16:40, Gone In Sixty Seconds.mkv: picture 6713 s, sound 7077 s).
        out.picture_ends_early = True
        out.duration = out.picture_seconds

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
PICTURE_BOX_DETECTOR_VERSION = 4

_CROPDETECT_RE = _re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

# WARNING: the fallback numbers below must match PICTURE_BOX_SAMPLE_FRACTIONS, PICTURE_BOX_FALLBACK_OFFSETS_S, PICTURE_BOX_SAMPLE_SECONDS, PICTURE_BOX_SAMPLE_TIMEOUT_S, PICTURE_BOX_MIN_GOOD_SAMPLES, PICTURE_BOX_CROPDETECT_LIMIT, PICTURE_BOX_MIN_BAR_FRACTION, PICTURE_BOX_MIN_BAR_PX and PICTURE_BOX_FLAT_* in performance_governor.py; they are used only when that module fails to import.
_PB_SAMPLE_FRACTIONS = _governor.PICTURE_BOX_SAMPLE_FRACTIONS if _governor is not None else (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)
_PB_FALLBACK_OFFSETS_S = _governor.PICTURE_BOX_FALLBACK_OFFSETS_S if _governor is not None else (30.0, 120.0, 300.0)
_PB_SAMPLE_SECONDS = _governor.PICTURE_BOX_SAMPLE_SECONDS if _governor is not None else 2.0
_PB_SAMPLE_TIMEOUT_S = _governor.PICTURE_BOX_SAMPLE_TIMEOUT_S if _governor is not None else 25.0
_PB_MIN_GOOD_SAMPLES = _governor.PICTURE_BOX_MIN_GOOD_SAMPLES if _governor is not None else 3
_PB_LIMIT = _governor.PICTURE_BOX_CROPDETECT_LIMIT if _governor is not None else 32
_PB_MIN_BAR_FRACTION = _governor.PICTURE_BOX_MIN_BAR_FRACTION if _governor is not None else 0.004
_PB_MIN_BAR_PX = _governor.PICTURE_BOX_MIN_BAR_PX if _governor is not None else 2
_PB_FLAT_W = _governor.PICTURE_BOX_FLAT_WIDTH if _governor is not None else 640
_PB_FLAT_H = _governor.PICTURE_BOX_FLAT_HEIGHT if _governor is not None else 480
_PB_FLAT_TOLERANCE = _governor.PICTURE_BOX_FLAT_TOLERANCE if _governor is not None else 12
_PB_FLAT_FRAMES = _governor.PICTURE_BOX_FLAT_FRAMES if _governor is not None else 2
_PB_FLAT_MIN_PICTURE_FRACTION = _governor.PICTURE_BOX_FLAT_MIN_PICTURE_FRACTION if _governor is not None else 0.125


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


_pb_gpu_state = {"checked_for": None, "available": False, "failures": 0, "off_logged": False, "on_logged": False}
_pb_gpu_lock = threading.Lock()


def _pb_gpu_decode_args(ffmpeg: str) -> list:
    """The ffmpeg arguments that move the picture-box decode onto the GPU, or [] when this PC, this ffmpeg or this run may not use it; the ffmpeg check runs once per ffmpeg path."""
    try:
        import performance_governor as _pg
        if not _pg.scan_gpu_decode_allowed():
            return []
        hwaccel = str(_pg.SCAN_GPU_DECODE_HWACCEL)
        check_timeout = float(_pg.SCAN_GPU_DECODE_CHECK_TIMEOUT_SECONDS)
        failure_limit = int(_pg.SCAN_GPU_DECODE_FAILURE_LIMIT)
    except Exception as _swallowed_error:
        _log_swallowed("_pb_gpu_decode_args: reading the governor's GPU scan values", _swallowed_error)
        return []
    with _pb_gpu_lock:
        if _pb_gpu_state["failures"] >= failure_limit:
            return []
        if _pb_gpu_state["checked_for"] != ffmpeg:
            _pb_gpu_state["checked_for"] = ffmpeg
            _pb_gpu_state["available"] = False
            try:
                popen_kwargs = {}
                if os.name == "nt":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                listed = subprocess.run([ffmpeg, "-hide_banner", "-hwaccels"], capture_output=True,
                                        timeout=check_timeout, stdin=subprocess.DEVNULL, **popen_kwargs)
                out = listed.stdout or b""
                if isinstance(out, bytes):
                    out = out.decode("utf-8", errors="replace")
                _pb_gpu_state["available"] = hwaccel in [w.strip() for w in out.splitlines()]
            except Exception as e:
                log.warning("[PICTURE BOX] GPU decode check failed at 'ffmpeg -hwaccels' (scans decode on the CPU): %s", e)
            log.info("[PICTURE BOX] GPU decode %s: hwaccel %s %s in this ffmpeg", "ON" if _pb_gpu_state["available"] else "off",
                     hwaccel, "found" if _pb_gpu_state["available"] else "not found")
        if not _pb_gpu_state["available"]:
            return []
    return ["-hwaccel", hwaccel]


def _pb_gpu_decode_failed(file_path: str, returncode) -> None:
    """Counts one failed GPU decode; once the governor's limit is reached the picture-box scan decodes on the CPU for the rest of the run."""
    try:
        import performance_governor as _pg
        failure_limit = int(_pg.SCAN_GPU_DECODE_FAILURE_LIMIT)
    except Exception as _swallowed_error:
        _log_swallowed("_pb_gpu_decode_failed: reading the governor's GPU failure limit", _swallowed_error)
        failure_limit = 1
    with _pb_gpu_lock:
        _pb_gpu_state["failures"] += 1
        count = _pb_gpu_state["failures"]
    log.warning("[PICTURE BOX] GPU decode of %s failed (exit %s), sample redone on the CPU (%d of %d)",
                os.path.basename(file_path), returncode, count, failure_limit)
    if count >= failure_limit:
        log.warning("[PICTURE BOX] GPU decode switched off for this run after %d failures; scans decode on the CPU", count)


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
    gpu_args = _pb_gpu_decode_args(ffmpeg)

    def _command(hw):
        return [
            ffmpeg, "-nostdin", "-hide_banner", "-threads", str(_pb_cropdetect_threads()),
        ] + list(hw) + [
            "-ss", f"{t:.3f}", "-i", file_path, "-t", f"{_PB_SAMPLE_SECONDS:g}",
            "-map", "0:v:0", "-vf", f"cropdetect=limit={_PB_LIMIT}:round=2:reset=0",
            "-an", "-sn", "-dn", "-f", "null", "-",
            "-map", "0:v:0", "-vf", f"fps=1,scale={_PB_FLAT_W}:{_PB_FLAT_H}:flags=area,format=gray",
            "-frames:v", str(_PB_FLAT_FRAMES), "-an", "-sn", "-dn", "-f", "rawvideo", "pipe:1",
        ]

    try:
        import media as _media
        with _media._fast_disk_io_slot(f"picture_box({os.path.basename(file_path)})"):
            try:
                result = subprocess.run(
                    _command(gpu_args), capture_output=True,
                    timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs,
                )
            except subprocess.TimeoutExpired:
                if not gpu_args:
                    raise
                result = None
            # WARNING: a GPU decode that fails must be redone on the CPU in the same slot, or a file the GPU cannot decode would be reported as unreadable and could be removed as broken.
            if gpu_args and (result is None or getattr(result, "returncode", 0) != 0 or not result.stdout):
                _pb_gpu_decode_failed(file_path, "timeout" if result is None else getattr(result, "returncode", None))
                result = subprocess.run(
                    _command([]), capture_output=True,
                    timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs,
                )
    except subprocess.TimeoutExpired as _swallowed_error:
        _log_swallowed("_pb_run_sample: import media as _media", _swallowed_error)
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
BURNED_IN_DETECTOR_VERSION = 5
_BI_SOURCE_BAKED = "baked"
_BI_SOURCE_CLEAN = "clean"

# WARNING: the fallback numbers below must match BURNED_IN_FRAME_W, BURNED_IN_FRAME_H, BURNED_IN_SAMPLE_FRACTIONS, BURNED_IN_FALLBACK_OFFSETS_S, BURNED_IN_FRAME_TIMEOUT_S, BURNED_IN_REGION_TOP_FRAC, BURNED_IN_REGION_LEFT_FRAC, BURNED_IN_REGION_RIGHT_FRAC, BURNED_IN_BRIGHT_MIN, BURNED_IN_DARK_MAX, BURNED_IN_OUTLINE_REACH_PX, BURNED_IN_ROW_MIN_TEXT_FRAC, BURNED_IN_ROW_MIN_TRANSITIONS, BURNED_IN_BAND_MIN_H_FRAC, BURNED_IN_BAND_MAX_H_FRAC, BURNED_IN_BAND_MIN_WIDTH_FRAC, BURNED_IN_BAND_CENTER_RANGE, BURNED_IN_BAND_MIN_GLYPH_CLUSTERS, BURNED_IN_BAND_MAX_FILL, BURNED_IN_BAND_CONTRAST_RATIO, BURNED_IN_POSITION_TOLERANCE_FRAC, BURNED_IN_MIN_POSITIVE_FRAMES, BURNED_IN_MAX_BANDS_PER_FRAME, BURNED_IN_EARLY_STOP_FRAMES, BURNED_IN_MIN_AGREEING_SHARE, BURNED_IN_MIN_POSITIVE_SHARE, BURNED_IN_STATIC_BAND_TOLERANCE_FRAC, BURNED_IN_STATIC_BAND_MAX_SHARE, BURNED_IN_REFERENCE_FRAME_H, BURNED_IN_BAND_GAP_ROWS, BURNED_IN_BAND_MAX_GLYPH_RUN_FRAC, BURNED_IN_EDGE_MARGIN_FRAC, BURNED_IN_SYMBOL_MAX_WIDTH_FRAC, BURNED_IN_SYMBOL_ASPECT, BURNED_IN_SYMBOL_FILL, BURNED_IN_SYMBOL_MIN_AREA_FRAC, BURNED_IN_SYMBOL_CENTRE_TOLERANCE, BURNED_IN_SYMBOL_JOIN_FRAC, BURNED_IN_SYMBOL_OTHERS_RATIO, BURNED_IN_BAND_MIN_BOTTOM_FRAC, BURNED_IN_BAR_ROW_MAX, BURNED_IN_BAR_MIN_FRAC, BURNED_IN_BAR_MAX_TOTAL_FRAC, BURNED_IN_BLACK_CARD_MEDIAN, BURNED_IN_BLACK_CARD_DARK_FRAC, BURNED_IN_LONG_FILE_S, BURNED_IN_LONG_START_FRAC, BURNED_IN_LONG_END_FRAC in performance_governor.py; they are used only when that module fails to import.
_BI_FRAME_W = _governor.BURNED_IN_FRAME_W if _governor is not None else 960
_BI_FRAME_H = _governor.BURNED_IN_FRAME_H if _governor is not None else 540
_BI_LONG_FILE_S = _governor.BURNED_IN_LONG_FILE_S if _governor is not None else 600.0
_BI_LONG_START_FRAC = _governor.BURNED_IN_LONG_START_FRAC if _governor is not None else 0.12
_BI_LONG_END_FRAC = _governor.BURNED_IN_LONG_END_FRAC if _governor is not None else 0.86
_BI_SAMPLE_FRACTIONS = _governor.BURNED_IN_SAMPLE_FRACTIONS if _governor is not None else (0.06, 0.14, 0.22, 0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78, 0.86, 0.94)
_BI_FALLBACK_OFFSETS_S = _governor.BURNED_IN_FALLBACK_OFFSETS_S if _governor is not None else (20.0, 60.0, 120.0, 180.0, 240.0, 300.0)
_BI_FRAME_TIMEOUT_S = _governor.BURNED_IN_FRAME_TIMEOUT_S if _governor is not None else 20.0
_BI_REGION_TOP_FRAC = _governor.BURNED_IN_REGION_TOP_FRAC if _governor is not None else 0.55
_BI_REGION_LEFT_FRAC = _governor.BURNED_IN_REGION_LEFT_FRAC if _governor is not None else 0.125
_BI_REGION_RIGHT_FRAC = _governor.BURNED_IN_REGION_RIGHT_FRAC if _governor is not None else 0.875
_BI_BRIGHT_MIN = _governor.BURNED_IN_BRIGHT_MIN if _governor is not None else 205
_BI_DARK_MAX = _governor.BURNED_IN_DARK_MAX if _governor is not None else 75
_BI_OUTLINE_REACH_PX = _governor.BURNED_IN_OUTLINE_REACH_PX if _governor is not None else 2
_BI_ROW_MIN_TEXT_FRAC = _governor.BURNED_IN_ROW_MIN_TEXT_FRAC if _governor is not None else 0.025
_BI_ROW_MIN_TRANSITIONS = _governor.BURNED_IN_ROW_MIN_TRANSITIONS if _governor is not None else 10
_BI_BAND_MIN_H_FRAC = _governor.BURNED_IN_BAND_MIN_H_FRAC if _governor is not None else 0.018
_BI_BAND_MAX_H_FRAC = _governor.BURNED_IN_BAND_MAX_H_FRAC if _governor is not None else 0.06
_BI_BAND_MIN_WIDTH_FRAC = _governor.BURNED_IN_BAND_MIN_WIDTH_FRAC if _governor is not None else 0.12
_BI_BAND_CENTER_RANGE = _governor.BURNED_IN_BAND_CENTER_RANGE if _governor is not None else (0.35, 0.65)
_BI_BAND_MIN_GLYPH_CLUSTERS = _governor.BURNED_IN_BAND_MIN_GLYPH_CLUSTERS if _governor is not None else 6
_BI_BAND_MAX_FILL = _governor.BURNED_IN_BAND_MAX_FILL if _governor is not None else 0.50
_BI_BAND_CONTRAST_RATIO = _governor.BURNED_IN_BAND_CONTRAST_RATIO if _governor is not None else 4.0
_BI_POSITION_TOLERANCE_FRAC = _governor.BURNED_IN_POSITION_TOLERANCE_FRAC if _governor is not None else 0.05
_BI_MIN_POSITIVE_FRAMES = _governor.BURNED_IN_MIN_POSITIVE_FRAMES if _governor is not None else 3
_BI_MAX_BANDS_PER_FRAME = _governor.BURNED_IN_MAX_BANDS_PER_FRAME if _governor is not None else 3
_BI_EARLY_STOP_FRAMES = _governor.BURNED_IN_EARLY_STOP_FRAMES if _governor is not None else 4
_BI_MIN_AGREEING_SHARE = _governor.BURNED_IN_MIN_AGREEING_SHARE if _governor is not None else 0.6
_BI_MIN_POSITIVE_SHARE = _governor.BURNED_IN_MIN_POSITIVE_SHARE if _governor is not None else 0.25
_BI_STATIC_BAND_TOLERANCE_FRAC = _governor.BURNED_IN_STATIC_BAND_TOLERANCE_FRAC if _governor is not None else 0.02
_BI_STATIC_BAND_MAX_SHARE = _governor.BURNED_IN_STATIC_BAND_MAX_SHARE if _governor is not None else 0.7
_BI_REFERENCE_FRAME_H = _governor.BURNED_IN_REFERENCE_FRAME_H if _governor is not None else 270
_BI_BAND_GAP_ROWS = _governor.BURNED_IN_BAND_GAP_ROWS if _governor is not None else 2
_BI_BAND_MAX_GLYPH_RUN_FRAC = _governor.BURNED_IN_BAND_MAX_GLYPH_RUN_FRAC if _governor is not None else 0.33
_BI_EDGE_MARGIN_FRAC = _governor.BURNED_IN_EDGE_MARGIN_FRAC if _governor is not None else 0.02
_BI_SYMBOL_MAX_WIDTH_FRAC = _governor.BURNED_IN_SYMBOL_MAX_WIDTH_FRAC if _governor is not None else 0.10
_BI_SYMBOL_ASPECT = _governor.BURNED_IN_SYMBOL_ASPECT if _governor is not None else (0.3, 1.0)
_BI_SYMBOL_FILL = _governor.BURNED_IN_SYMBOL_FILL if _governor is not None else (0.1, 0.65)
_BI_SYMBOL_MIN_AREA_FRAC = _governor.BURNED_IN_SYMBOL_MIN_AREA_FRAC if _governor is not None else 0.0002
_BI_SYMBOL_CENTRE_TOLERANCE = _governor.BURNED_IN_SYMBOL_CENTRE_TOLERANCE if _governor is not None else 0.08
_BI_SYMBOL_JOIN_FRAC = _governor.BURNED_IN_SYMBOL_JOIN_FRAC if _governor is not None else 0.02
_BI_SYMBOL_OTHERS_RATIO = _governor.BURNED_IN_SYMBOL_OTHERS_RATIO if _governor is not None else 2.0
_BI_BAND_MIN_BOTTOM_FRAC = _governor.BURNED_IN_BAND_MIN_BOTTOM_FRAC if _governor is not None else 0.76
_BI_BAR_ROW_MAX = _governor.BURNED_IN_BAR_ROW_MAX if _governor is not None else 28
_BI_BAR_MIN_FRAC = _governor.BURNED_IN_BAR_MIN_FRAC if _governor is not None else 0.03
_BI_BAR_MAX_TOTAL_FRAC = _governor.BURNED_IN_BAR_MAX_TOTAL_FRAC if _governor is not None else 0.45
_BI_BLACK_CARD_MEDIAN = _governor.BURNED_IN_BLACK_CARD_MEDIAN if _governor is not None else 3
_BI_BLACK_CARD_DARK_FRAC = _governor.BURNED_IN_BLACK_CARD_DARK_FRAC if _governor is not None else 0.8


def _bi_dilate(mask, reach):
    """Boolean dilation by a square of +-reach pixels (numpy only)."""
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


def _bi_picture_rows(gray):
    """(first row, one past the last row) of the real picture inside the frame: the black bars above and below a widescreen picture are left out, and a subtitle written into the bottom bar still counts as inside."""
    import numpy as np
    frame_h = gray.shape[0]
    lit = np.flatnonzero(gray.max(axis=1) > _BI_BAR_ROW_MAX)
    if lit.size == 0:
        return 0, frame_h
    min_bar = _BI_BAR_MIN_FRAC * frame_h
    top_bar = int(lit[0])
    bottom_bar = frame_h - 1 - int(lit[-1])
    # WARNING: a letterbox has equal bars, but a subtitle written into the bottom bar makes that bar look short, so a real top bar sets both.
    bars = max(top_bar, bottom_bar) if top_bar >= min_bar else (bottom_bar if bottom_bar >= min_bar else 0)
    if bars <= 0 or 2 * bars > _BI_BAR_MAX_TOTAL_FRAC * frame_h * 2:
        return 0, frame_h
    first = top_bar if top_bar >= min_bar else 0
    return first, frame_h - bars


def _bi_picture_columns(gray, top, bottom):
    """(first column, one past the last column) of the real picture: the black bars at the sides of a 4:3 picture in a wide frame are left out, and the same width is taken from both sides, so a logo sitting in one bar does not shift the picture."""
    import numpy as np
    frame_w = gray.shape[1]
    lit = np.flatnonzero(gray[top:bottom].max(axis=0) > _BI_BAR_ROW_MAX)
    if lit.size == 0:
        return 0, frame_w
    left_bar = int(lit[0])
    right_bar = frame_w - 1 - int(lit[-1])
    bars = max(left_bar, right_bar)
    if bars < _BI_BAR_MIN_FRAC * frame_w or 2 * bars > _BI_BAR_MAX_TOTAL_FRAC * frame_w * 2:
        return 0, frame_w
    return bars, frame_w - bars


def _bi_symbol_band(text, y0, x0, pic_top, pic_h, pic_left, pic_w, frame_h, frame_w):
    """[(bottom, left, right)] when the search area holds one small bright shape, centred, of a caption symbol's size and not drowned in other bright specks (a music note on its own line), else []."""
    import numpy as np
    occupied = np.flatnonzero(text.any(axis=0))
    if occupied.size == 0:
        return []
    join = max(2, int(round(_BI_SYMBOL_JOIN_FRAC * pic_w)))
    groups = []
    start = prev = int(occupied[0])
    for c in occupied[1:]:
        c = int(c)
        if c - prev > join:
            groups.append((start, prev))
            start = c
        prev = c
    groups.append((start, prev))
    total = int(text.sum())
    min_h = max(3, int(round(pic_h * _BI_BAND_MIN_H_FRAC)))
    max_h = int(round(pic_h * _BI_BAND_MAX_H_FRAC)) * 2
    best = None
    for c0, c1 in groups:
        part = text[:, c0:c1 + 1]
        rows = np.flatnonzero(part.any(axis=1))
        top, bottom = int(rows[0]), int(rows[-1])
        width, height = c1 - c0 + 1, bottom - top + 1
        pixels = int(part.sum())
        left, right = c0 + x0, c1 + x0
        if width > _BI_SYMBOL_MAX_WIDTH_FRAC * pic_w or not (min_h <= height <= max_h):
            continue
        if not (_BI_SYMBOL_ASPECT[0] <= width / float(height) <= _BI_SYMBOL_ASPECT[1]):
            continue
        if abs(((left + right) / 2.0 - pic_left) / pic_w - 0.5) > _BI_SYMBOL_CENTRE_TOLERANCE:
            continue
        if (y0 + bottom + 1 - pic_top) / float(pic_h) < _BI_BAND_MIN_BOTTOM_FRAC:
            continue
        if not (_BI_SYMBOL_FILL[0] <= float(part[top:bottom + 1].mean()) <= _BI_SYMBOL_FILL[1]):
            continue
        if pixels < _BI_SYMBOL_MIN_AREA_FRAC * pic_w * pic_h or total - pixels > _BI_SYMBOL_OTHERS_RATIO * pixels:
            continue
        if best is None or pixels > best[0]:
            best = (pixels, y0 + bottom, left, right)
    if best is None:
        return []
    return [((best[1] + 1) / float(frame_h), best[2] / float(frame_w), (best[3] + 1) / float(frame_w))]


def _bi_frame_text_bands(gray):
    """Returns (bottom, left, right), as fractions of the frame, of every band in one 8-bit gray frame that looks like a subtitle line."""
    import numpy as np
    frame_h, frame_w = gray.shape
    pic_top, pic_bottom = _bi_picture_rows(gray)
    pic_h = pic_bottom - pic_top
    # WARNING: owner 2026-11-17: a picture that is nearly all black is a title card or logo page, so no band in it is a subtitle.
    picture = gray[pic_top:pic_bottom]
    if float(np.median(picture)) <= _BI_BLACK_CARD_MEDIAN and float((picture <= _BI_DARK_MAX).mean()) >= _BI_BLACK_CARD_DARK_FRAC:
        return []
    pic_left, pic_right = _bi_picture_columns(gray, pic_top, pic_bottom)
    pic_w = pic_right - pic_left
    y0 = pic_top + int(pic_h * _BI_REGION_TOP_FRAC)
    x0 = pic_left + int(pic_w * _BI_REGION_LEFT_FRAC)
    x1 = pic_left + int(pic_w * _BI_REGION_RIGHT_FRAC)
    crop = gray[y0:, x0:x1]
    bright = crop >= _BI_BRIGHT_MIN
    dark = crop <= _BI_DARK_MAX
    # WARNING: the pixel limits (outline reach, the gap that splits two bands) are for a frame _BI_REFERENCE_FRAME_H high and grow with the frame, or a taller frame cuts one subtitle line into pieces and misses it.
    pixel_scale = frame_h / float(_BI_REFERENCE_FRAME_H)
    text = bright & _bi_dilate(dark, max(1, int(round(_BI_OUTLINE_REACH_PX * pixel_scale))))

    row_count = text.sum(axis=1)
    row_trans = (bright[:, 1:] != bright[:, :-1]).sum(axis=1)
    cand = (row_count >= _BI_ROW_MIN_TEXT_FRAC * pic_w) & (row_trans >= _BI_ROW_MIN_TRANSITIONS)
    # WARNING: owner 2026-11-17: a caption symbol on its own (a music note) is centred caption-sized text too, so one small bright shape alone in the middle counts; it has too few letter edges for the line test above.
    symbol = lambda: _bi_symbol_band(text, y0, x0, pic_top, pic_h, pic_left, pic_w, frame_h, frame_w)
    if not cand.any():
        return symbol()

    bands = []
    rows = np.flatnonzero(cand)
    start = prev = int(rows[0])
    for r in rows[1:]:
        r = int(r)
        if r - prev > max(2, int(round(_BI_BAND_GAP_ROWS * pixel_scale))):
            bands.append((start, prev))
            start = r
        prev = r
    bands.append((start, prev))

    min_h = max(3, int(round(pic_h * _BI_BAND_MIN_H_FRAC)))
    max_h = int(round(pic_h * _BI_BAND_MAX_H_FRAC))
    in_band = np.zeros(len(row_count), dtype=bool)
    for a, b in bands:
        in_band[a:b + 1] = True
    outside = row_count[~in_band]
    background = float(np.median(outside)) if outside.size else 0.0

    found = []
    for a, b in bands:
        height = b - a + 1
        if height < min_h or height > max_h:
            continue
        # WARNING: owner 2026-11-17: a subtitle sits low in the picture; a band that ends higher is not one.
        if (y0 + b + 1 - pic_top) / float(pic_h) < _BI_BAND_MIN_BOTTOM_FRAC:
            continue
        cols = text[a:b + 1].any(axis=0)
        hit = np.flatnonzero(cols)
        if hit.size == 0:
            continue
        left, right = int(hit[0]) + x0, int(hit[-1]) + x0
        extent = right - left + 1
        if extent < _BI_BAND_MIN_WIDTH_FRAC * pic_w:
            continue
        centre = ((left + right) / 2.0 - pic_left) / pic_w
        if not (_BI_BAND_CENTER_RANGE[0] <= centre <= _BI_BAND_CENTER_RANGE[1]):
            continue
        # WARNING: a band that runs into only one edge of the search area carries on past it (a big logo or a banner), so it is not a centred line.
        edge_margin = max(2, int(round(_BI_EDGE_MARGIN_FRAC * (x1 - x0))))
        if (left <= x0 + edge_margin - 1) != (right >= x1 - edge_margin):
            continue
        clusters = int(((cols[1:].astype(np.int8) - cols[:-1].astype(np.int8)) == 1).sum()) + int(bool(cols[0]))
        if clusters < _BI_BAND_MIN_GLYPH_CLUSTERS:
            continue
        # WARNING: owner 2026-11-17: letters as big as a third of the search area are a logo or a title card; a subtitle's joined letters (a word) stay far narrower.
        edges = np.diff(np.concatenate(([0], cols.astype(np.int8), [0])))
        longest_run = int((np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)).max())
        if longest_run > _BI_BAND_MAX_GLYPH_RUN_FRAC * (x1 - x0):
            continue
        box = bright[a:b + 1, left - x0:right - x0 + 1]
        if box.mean() > _BI_BAND_MAX_FILL:
            continue
        band_mean = float(row_count[a:b + 1].mean())
        if band_mean < _BI_BAND_CONTRAST_RATIO * max(background, 1.0):
            continue
        found.append(((y0 + b + 1) / float(frame_h), left / float(frame_w), (right + 1) / float(frame_w)))
    if len(found) > _BI_MAX_BANDS_PER_FRAME:
        return []
    return found


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
    except subprocess.TimeoutExpired as _swallowed_error:
        _log_swallowed("_bi_grab_frame: import media as _media", _swallowed_error)
        return "timeout"
    except Exception as _swallowed_error:
        _log_swallowed("_bi_grab_frame: import media as _media", _swallowed_error)
        return None
    data = result.stdout or b""
    if len(data) != _BI_FRAME_W * _BI_FRAME_H:
        return None
    return np.frombuffer(data, dtype=np.uint8).reshape(_BI_FRAME_H, _BI_FRAME_W)


def _bi_positions_agree(positive_frames, decoded, early=False):
    """True when the frames with text read as subtitles: enough of them put their lowest band at the same height, they are a fair share of all frames read, and the line is not the same fixed graphic (a logo, a banner or a one-off gag) every time."""
    lows = [max(bands) for bands in positive_frames]
    group = []
    for ref in lows:
        near = [v for v in lows if abs(v[0] - ref[0]) <= _BI_POSITION_TOLERANCE_FRAC]
        if len(near) > len(group):
            group = near
    need = _BI_EARLY_STOP_FRAMES if early else _BI_MIN_POSITIVE_FRAMES
    if len(group) < need or len(group) < _BI_MIN_AGREEING_SHARE * len(lows):
        return False
    if len(group) < _BI_MIN_POSITIVE_SHARE * decoded:
        return False
    tol = _BI_STATIC_BAND_TOLERANCE_FRAC
    same = max(sum(1 for w in group if abs(w[1] - v[1]) <= tol and abs(w[2] - v[2]) <= tol) for v in group)
    return same < _BI_STATIC_BAND_MAX_SHARE * len(group)


def _bi_sample_times(duration, extra=False):
    if duration and duration > 0:
        latest = max(0.0, duration - 1.0)
        fractions = _BI_SAMPLE_FRACTIONS
        if extra:
            fractions = tuple(f + 0.04 for f in _BI_SAMPLE_FRACTIONS)
        # WARNING: owner 2026-11-17: a long file is not read near its start or its end, where studio logo pages, web addresses and credits sit; a text band there is not a subtitle.
        if duration >= _BI_LONG_FILE_S:
            span = _BI_LONG_END_FRAC - _BI_LONG_START_FRAC
            fractions = tuple(_BI_LONG_START_FRAC + min(f, 1.0) * span for f in fractions)
        return [round(min(duration * f, latest), 3) for f in fractions]
    base = _BI_FALLBACK_OFFSETS_S
    return [t + 10.0 for t in base] if extra else list(base)


def detect_burned_in_captions(file_path, duration=None, ffmpeg_path=None,
                              frame_timeout=_BI_FRAME_TIMEOUT_S, diag=None):
    """Returns True when subtitle text is part of the picture, False when it is not, None when it could not tell."""
    try:
        import numpy
    except ImportError as _swallowed_error:
        _log_swallowed("detect_burned_in_captions: import numpy", _swallowed_error)
        if diag is not None:
            diag["reason"] = "numpy_unavailable"
        return None
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)

    positives = []
    positive_times = []
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
            bands = _bi_frame_text_bands_safe(frame)
            if bands:
                positives.append(bands)
                positive_times.append(t)
                if _bi_positions_agree(positives, decoded, early=True):
                    if diag is not None:
                        diag.update(positive_frames=len(positives), decoded=decoded, reason="text_band_found",
                                    seconds=list(positive_times), heights=[round(max(b)[0], 2) for b in positives])
                    return True
        if _bi_positions_agree(positives, decoded):
            if diag is not None:
                diag.update(positive_frames=len(positives), decoded=decoded, reason="text_band_found",
                            seconds=list(positive_times), heights=[round(max(b)[0], 2) for b in positives])
            return True
    if diag is not None:
        diag.update(positive_frames=len(positives), decoded=decoded, timeouts=timeouts)
        if positives:
            diag["reason"] = "text_seen_not_subtitles"
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
        return _bi_frame_text_bands(frame)
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

    def get_cached_known(self, file_path: str) -> Optional[dict]:
        """Like get_cached, but a file whose key is not saved yet reads as None instead of being hashed, so it never waits for a disk slot."""
        content_key = self.keys.peek(file_path)
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
        except OSError as _swallowed_error:
            _log_swallowed("ProbeLibrary._probe_header_into: size = os.path.getsize(file_path)", _swallowed_error)
            size = None
        if self.use_vlc_fallback:
            duration, source = probe_duration_with_fallback(file_path, result)
        else:
            duration, source = result.duration, "ffmpeg"
        if result.picture_ends_early and source == "ffmpeg":
            source = "ffmpeg_picture"
            log.warning("[PICTURE ENDS EARLY] %s: the picture is %.0fs long and the sound runs on, so %.0fs is the length the schedule uses",
                        os.path.basename(file_path), result.picture_seconds or 0.0, duration or 0.0)
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
        # WARNING: a file is scanned once and decided from the saved hub data afterwards (owner 2026-11-05, 14.4); a scan of a file that already has a saved picture box is written down with its reason so a second scan can never go unnoticed.
        if entry and entry.get("picture_box"):
            log.warning("[SCAN AGAIN] picture box of %s is scanned a second time: the saved result is from detector version %s and the current one is %s",
                        os.path.basename(str(file_path)), entry.get("picture_box_detector_version") or 0, PICTURE_BOX_DETECTOR_VERSION)
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

    def store_file_fields(self, file_path: str, **fields):
        """Writes per-file fields (audio track choice, loudness learning) into the file's row; a field given as None is cleared. False when the file cannot be keyed yet."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, **fields)
        return True

    def file_fields(self, file_path: str, names):
        """{name: value} of the named per-file fields, None for a field with nothing on record; None when the file cannot be keyed yet."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        held = self.library.get_entry(content_key) or {}
        return {name: held.get(name) for name in names}

    def store_track_gains(self, file_path: str, primary: int, gains: dict, scan_version: Optional[int] = None):
        """Stores the scanned gain of every audio track of a file ({track number: gain}, 0 = first track) and which track the file's saved gain was measured on; False when the file cannot be keyed."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        payload = {"primary": int(primary), "version": scan_version,
                   "gains": {str(int(k)): float(v) for k, v in gains.items() if v and float(v) > 0}}
        self.library.upsert_fields(content_key, track_gains=json.dumps(payload))
        return True

    def track_gains(self, file_path: str):
        """{"primary", "version", "gains"} as stored by store_track_gains, or None when nothing is on record."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        held = self.library.get_entry(content_key)
        raw = held.get("track_gains") if held else None
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            return {"primary": int(data.get("primary", 0)), "version": data.get("version"),
                    "gains": {int(k): float(v) for k, v in (data.get("gains") or {}).items()}}
        except (ValueError, TypeError, AttributeError) as _swallowed_error:
            _log_swallowed("ProbeLibrary.track_gains: data = json.loads(raw) if isinstance(raw, str) else raw", _swallowed_error)
            return None

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
        except (ValueError, TypeError) as _swallowed_error:
            _log_swallowed("ProbeLibrary.gain_windows: data = json.loads(raw) if isinstance(raw, str) else raw", _swallowed_error)
            return []

    def gain_is_checked(self, file_path: str) -> bool:
        """True when the file's saved gain was double-checked with extra scans."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        held = self.library.get_entry(content_key)
        return bool(held and held.get("gain_checked"))

    def store_owner_gain(self, file_path: str, total_db: float):
        """Replaces the file's saved gain with the owner's chosen total dB; the scanned number is gone, the scan version is set to the owner mark so the gain always counts as current and no scan replaces it, and the manual offset is 0."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, gain=10 ** (float(total_db) / 20.0), gain_target_lufs=None,
                                   gain_scan_version=_OWNER_GAIN_SCAN_VERSION, gain_convergence_db=None, manual_gain_db=0.0)
        return True

    def store_context_gain(self, file_path: str, context: str, total_db: float, series_key=None, baseline_db=None, change_db=None):
        """Saves the owner's total dB for the file inside one context only; the scanned gain and every other context stay as they are."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.set_context_gain(content_key, context, total_db, series_key, baseline_db, change_db)
        return True

    def context_gain(self, file_path: str, context: str):
        """The owner's saved total dB record for the file in one context, or None."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        return self.library.get_context_gain(content_key, context)

    def store_context_off_gain(self, file_path: str, context: str, off_db: float):
        """Saves the owner's Loudness-Scan-off dB for the file in one context only."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.set_context_off_gain(content_key, context, off_db)
        return True

    def context_off_gain(self, file_path: str, context: str):
        """The owner's saved Loudness-Scan-off dB for the file in one context, or None."""
        content_key = self.keys.resolve(file_path)
        return None if content_key is None else self.library.get_context_off_gain(content_key, context)

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
        except OSError as _swallowed_error:
            _log_swallowed("ProbeLibrary.file_changed_on_disk: st = os.stat(file_path)", _swallowed_error)
            return False
        stamp = self.library.path_key_stamp(os.path.normpath(os.path.abspath(file_path)).lower())
        return stamp is not None and (stamp[0] != st.st_size or stamp[1] != st.st_mtime)


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
        import media as _media
        if os.path.splitext(str(file_path))[1].lower() in _media.AUDIO_DISPLAY_EXTS:
            # WARNING: owner 2026-11-10: a song with cover art reports a picture size, so it was scanned for burned-in captions on every pass, found no frames and was never stored, which kept the scan decoding the same songs for hours; a song has no picture to read captions from, so it is stored as clean at once.
            self.library.upsert_fields(
                content_key,
                captions_source=_BI_SOURCE_CLEAN,
                captions_reason=f"burned_in_v{BURNED_IN_DETECTOR_VERSION}",
            )
            return False
        if diag is None:
            diag = {}
        entry = self.library.get_entry(content_key)
        if entry is None or not entry.get("header_probed_at"):
            entry = self.get_or_probe_sync(file_path)
        if entry is not None and entry.get("width") is None and entry.get("header_probed_at"):
            verdict = False
        else:
            verdict = detect_burned_in_captions(
                file_path, duration=(entry or {}).get("duration"),
                ffmpeg_path=self.ffmpeg_path, diag=diag)
        log.info("[BURNED-IN SCAN] %s -> %s (%s)", os.path.basename(file_path),
                 {True: "captions are part of the picture", False: "no captions in the picture", None: "could not tell, will retry"}[verdict],
                 ", ".join(f"{key}={value}" for key, value in diag.items()) or "no details")
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


_hub_key_cache = {}


def _hub_key(path) -> str:
    """The hub's key for a path: its full, normalised, lower-case form, worked out once per path text and remembered."""
    try:
        key = _hub_key_cache.get(path)
    except TypeError:
        return os.path.normpath(os.path.abspath(str(path))).lower()
    if key is None:
        key = os.path.normpath(os.path.abspath(str(path))).lower()
        if len(_hub_key_cache) > 200000:
            _hub_key_cache.clear()
        _hub_key_cache[path] = key
    return key


# WARNING: the fallback must match HUB_UNMEASURED_RECHECK_SECONDS in performance_governor.py; it is used only when that module fails to import.
_UNMEASURED_RECHECK_SECONDS = _governor.HUB_UNMEASURED_RECHECK_SECONDS if _governor is not None else 30.0
# WARNING: the fallback must match LIBRARY_ROWS_PER_READ in performance_governor.py; it is used only when that module fails to import.
_ROWS_PER_READ = _governor.LIBRARY_ROWS_PER_READ if _governor is not None else 400


class _PathMemoHub:
    """What every per-file hub shares: the library it reads, its lock, what it remembers per path, the paths it was asked about, and the paths that must be keyed again from disk."""

    def __init__(self, probe_library: "ProbeLibrary"):
        self.probe_library = probe_library
        self._lock = threading.RLock()
        self._memo = {}
        self._paths = {}
        self._must_resolve = set()
        self._generation = 0
        self._recheck_queue = {}
        self._peek_keys = {}
        self._context_memo = {}

    def generation(self):
        """A number that changes whenever any answer this hub holds changes."""
        return self._generation

    def peek(self, path):
        """The (measured, estimate) this hub holds for a path, or None when it holds nothing; never asks the library."""
        key = self._peek_keys.get(path)
        if key is None:
            key = _hub_key(path)
            if len(self._peek_keys) > 200000:
                self._peek_keys.clear()
            self._peek_keys[path] = key
        held = self._memo.get(key)
        return None if held is None else (held[0], held[1])

    def _set_memo(self, key, value):
        """Stores one remembered answer and moves the generation on when the answer differs from the one held; call with the lock held."""
        held = self._memo.get(key)
        if held is None or held[0] != value[0] or held[1] != value[1]:
            self._generation += 1
        self._memo[key] = value

    def forget(self, path):
        """Drops what the hub remembers about a path because the file changed on disk; the next look-up re-keys the file from disk and finds that file's own record. The library rows stay."""
        key = _hub_key(path)
        with self._lock:
            if self._memo.pop(key, None) is not None:
                self._generation += 1
            self._must_resolve.add(key)

    def rechecks_waiting(self):
        """How many answers the screen thread used while they were due to be asked again."""
        with self._lock:
            return len(self._recheck_queue)

    def run_queued_rechecks(self, limit=None):
        """Asks the library again for the answers the screen thread used while they were due (oldest first, at most `limit`); returns how many were asked. For a background thread only."""
        with self._lock:
            items = list(self._recheck_queue.items())[:limit] if limit else list(self._recheck_queue.items())
            for key, _path in items:
                self._recheck_queue.pop(key, None)
        for key, path in items:
            self._lookup(key, path, background=True)
        return len(items)

    def known_paths(self):
        """The paths this hub has been asked about, as the caller gave them."""
        with self._lock:
            return list(self._paths.values())


# WARNING: owner log 2026-11-20: the guide, the guide writer, the channel-ready and first-show threads each asked the library about 1,873 new files one at a time; every ask waited its turn behind the others and the new channel's first show took 67 s. Before a thread goes through a list of files it reads every answer it does not hold yet in turns of ROWS_PER_READ files.
def _keys_to_read(hub, paths, due):
    """{hub key: path} of the paths whose answer the hub does not hold or holds past its recheck time (due(held, now)); paths that must be keyed again from disk are left to the one-file look-up."""
    now = time.monotonic()
    wanted = {}
    for path in paths:
        if not path:
            continue
        key = _hub_key(path)
        if key in hub._must_resolve or key in wanted:
            continue
        held = hub._memo.get(key)
        if held is None or due(held, now):
            wanted[key] = str(path)
    return wanted


# WARNING: owner 2026-11-24: callables told whenever a length is queued to be asked again, so the channel-ready thread is woken for it instead of looking on a timer; each must return at once.
RECHECK_LISTENERS = []


def _tell_recheck_listeners():
    for _listener in list(RECHECK_LISTENERS):
        try:
            _listener()
        except Exception as _swallowed_error:
            _log_swallowed("_tell_recheck_listeners: a listener", _swallowed_error)


class DurationHub(_PathMemoHub):
    """Answers one file at a time: looks the file up in the library when it is asked about and remembers the answer; nothing is read in advance. The one place the scheduler, guide and catch-up read a file's length from."""

    def __init__(self, probe_library: "ProbeLibrary"):
        super().__init__(probe_library)
        self._pending_estimates = {}
        self._length_changed_at = {}
        self._estimate_wake = threading.Event()
        self._estimate_idle = threading.Event()
        self._estimate_idle.set()
        self._estimate_thread = None

    def _read_row(self, key, path, force):
        """The file's (measured, estimate) from the library; a forgotten file is re-keyed from disk first."""
        if force:
            content_key = self.probe_library.keys.resolve(path)
            entry = self.probe_library.library.get_entry(content_key) if content_key is not None else None
            row = None if not entry else (entry.get("duration"), entry.get("estimated_duration"))
        else:
            row = self.probe_library.library.path_duration_row(key)
        if row is LIBRARY_BUSY:
            return LIBRARY_BUSY, None
        if not row:
            return None, None
        return (float(row[0]) if row[0] and row[0] > 0 else None,
                float(row[1]) if row[1] and row[1] > 0 else None)

    def _lookup(self, key, path, background=False):
        """Returns [measured, estimate, looked-up-at] for a path, asking the library once and remembering the answer; a path with no measured length is asked again after the recheck time (on the screen thread the answer held is used and the ask is queued for a background thread), and a path the library has no length for gets its old settings values carried in first."""
        # WARNING: an answer already held is read without the hub's lock (one dict read and one set check, each whole under Python's own lock); the screen thread took this lock thousands of times a second and waited for it behind the scans on a 4-core PC (owner log 2026-11-04). Writes still take the lock.
        held = self._memo.get(key)
        force = key in self._must_resolve
        now = time.monotonic()
        if held is not None and not force and (held[0] or now - held[2] < _UNMEASURED_RECHECK_SECONDS):
            return held
        if (held is not None and not force and not background
                and _time_critical()):
            # WARNING: the screen thread never waits on the library for an answer it already holds (owner 2026-11-02, N1); the due recheck runs on the channel-ready thread.
            with self._lock:
                self._recheck_queue[key] = str(path)
            _tell_recheck_listeners()
            return held
        measured = estimate = None
        try:
            measured, estimate = self._read_row(key, path, force)
        except Exception as _swallowed_error:
            _log_swallowed("DurationHub._lookup: reading the file's row from the library", _swallowed_error)
        if measured is LIBRARY_BUSY:
            # WARNING: owner log 2026-11-18: the screen thread never waits for a busy library; it goes on without a length this frame, nothing is remembered, and the channel-ready thread asks again.
            with self._lock:
                self._recheck_queue[key] = str(path)
            _tell_recheck_listeners()
            return held if held is not None else [None, None, now]
        with self._lock:
            self._must_resolve.discard(key)
            self._paths[key] = str(path)
            held = self._memo.get(key)
            if held is not None and held[0] and not force:
                return held
            held = [measured, estimate or (held[1] if held and not force else None), now]
            self._set_memo(key, held)
        return held

    def read_ahead(self, paths):
        """Reads from the library, in a few turns, the length of every path whose answer is not held or is due to be asked again, and remembers each answer as a one-file look-up would. For background threads; the screen thread does nothing here."""
        if _time_critical():
            return
        wanted = _keys_to_read(self, paths, lambda held, now: not held[0] and now - held[2] >= _UNMEASURED_RECHECK_SECONDS)
        if not wanted:
            return
        rows = self.probe_library.library.path_rows_many(list(wanted), "l.duration, l.estimated_duration")
        now = time.monotonic()
        with self._lock:
            for key, path in wanted.items():
                if key in self._must_resolve:
                    continue
                row = rows.get(key)
                measured = float(row[0]) if row and row[0] and row[0] > 0 else None
                estimate = float(row[1]) if row and row[1] and row[1] > 0 else None
                held = self._memo.get(key)
                if held is not None and held[0]:
                    continue
                self._paths[key] = path
                self._set_memo(key, [measured, estimate or (held[1] if held else None), now])

    def _note_length_change(self, key):
        """Remembers when a file got a new length (measured or estimated), for the guide to work its channel out again; call with the lock held."""
        if len(self._length_changed_at) >= _SCREEN_MEMO_MAX:
            self._length_changed_at.clear()
        self._length_changed_at[key] = time.monotonic()

    def lengths_changed_since(self, since):
        """{path key: monotonic time} of every file that got a new length after `since` (a time.monotonic() value)."""
        with self._lock:
            return {k: t for k, t in self._length_changed_at.items() if t > since}

    def read(self, path):
        """Returns (whole seconds, kind) for a file: its measured length, else its saved estimate, else (None, None)."""
        measured, estimate, _at = self._lookup(_hub_key(path), path)
        if measured and measured > 0:
            # WARNING: rounded up, never down: a slot shorter than its file makes the file run past it and the next one start late, opened part way in (owner log 2026-11-03: every 30.1 to 30.6 s commercial had a 30 s slot).
            return int(math.ceil(float(measured) - 1e-6)), DURATION_KIND_MEASURED
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
            if not held or held[0] != recorded:
                self._note_length_change(key)
            self._set_memo(key, [recorded, held[1] if held else None, time.monotonic()])
        return saved

    def correct_measured(self, path, seconds, source="picture_end"):
        """Replaces a file's saved measured length with a shorter one that was proven (its picture ends early); the one case where a length on record changes. Returns False when the library cannot key the file or the new length is not shorter."""
        if not seconds or seconds <= 0:
            return False
        try:
            content_key = self.probe_library.keys.resolve(path)
            if content_key is None:
                return False
            entry = self.probe_library.library.get_entry(content_key)
            held = float((entry or {}).get("duration") or 0.0)
            if held and float(seconds) >= held:
                return False
            self.probe_library.library.upsert_fields(content_key, duration=float(seconds), duration_source=source)
        except Exception as _swallowed_error:
            _log_swallowed("DurationHub.correct_measured: saving the shorter length to the library", _swallowed_error)
            return False
        key = _hub_key(path)
        with self._lock:
            held_memo = self._memo.get(key)
            self._note_length_change(key)
            self._set_memo(key, [float(seconds), held_memo[1] if held_memo else None, time.monotonic()])
        return True

    def store_estimate(self, path, seconds, basis, replace=False):
        """Saves an estimate for a file with no measured length; a file that already has an estimate keeps it. Returns the estimate on record, or None for a measured file or a length that is not positive. The answer is held in memory at once and the library row is written by one background thread, so nothing on the asking thread ever opens a file."""
        # WARNING: this is called while the schedule is built, on the main thread too; keying a new file here (a disk read behind the governor gate) froze the app for a minute when hundreds of files were added.
        if not seconds or seconds <= 0:
            return None
        key = _hub_key(path)
        measured, existing, _at = self._lookup(key, path)
        if measured:
            return None
        # WARNING: replace=True is only for a series-average estimate that a later scan of the same season has moved (owner 2026-11-06, 14.5: "updated as they get scanned later"); every other estimate is kept as saved.
        if existing and not (replace and int(existing) != int(round(seconds))):
            return int(existing)
        saved = float(int(round(seconds)))
        with self._lock:
            held = self._memo.get(key)
            self._note_length_change(key)
            self._set_memo(key, [held[0] if held else None, saved, time.monotonic()])
            self._pending_estimates[key] = (str(path), saved, basis, bool(replace))
            self._estimate_idle.clear()
            if self._estimate_thread is None or not self._estimate_thread.is_alive():
                self._estimate_thread = threading.Thread(target=self._estimate_writer, name="EstimateWriter", daemon=True)
                self._estimate_thread.start()
        self._estimate_wake.set()
        return int(saved)

    def flush_estimates(self, timeout=10.0):
        """Waits until every queued estimate is written to the library; True when the queue is empty. For restarts and tests, never the main loop."""
        return self._estimate_idle.wait(timeout)

    def _estimate_writer(self):
        while True:
            with self._lock:
                item = None
                if self._pending_estimates:
                    key = next(iter(self._pending_estimates))
                    item = (key,) + self._pending_estimates.pop(key)
            if item is None:
                with self._lock:
                    if not self._pending_estimates:
                        self._estimate_idle.set()
                self._estimate_wake.clear()
                if not self._estimate_wake.wait(timeout=30.0):
                    with self._lock:
                        if not self._pending_estimates:
                            self._estimate_thread = None
                            return
                continue
            try:
                self._write_estimate(*item)
            except Exception as _swallowed_error:
                _log_swallowed("DurationHub._estimate_writer: saving a queued estimate", _swallowed_error)

    def _write_estimate(self, key, path, saved, basis, replace=False):
        """Background thread: keys the file and saves its estimate in the library, unless the library already holds a length or an older estimate for that content."""
        content_key = self.probe_library.keys.resolve(path)
        if content_key is None:
            return
        entry = self.probe_library.library.get_entry(content_key) or {}
        if entry.get("duration") and entry["duration"] > 0:
            with self._lock:
                self._set_memo(key, [float(entry["duration"]), entry.get("estimated_duration"), time.monotonic()])
            return
        if (entry.get("estimated_duration") and entry["estimated_duration"] > 0
                and not (replace and entry.get("estimate_basis") == ESTIMATE_BASIS_SERIES_AVERAGE)):
            with self._lock:
                held = self._memo.get(key)
                self._set_memo(key, [held[0] if held else None, float(entry["estimated_duration"]), time.monotonic()])
            return
        self.probe_library.library.upsert_fields(
            content_key, estimated_duration=saved, estimated_at=time.time(), estimate_basis=basis)


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
_GAIN_MISS_RECHECK_SECONDS = _governor.HUB_GAIN_MISS_RECHECK_SECONDS if _governor is not None else 900.0


class GainHub(_PathMemoHub):
    """Answers one file at a time: looks the file's scanned gain, the settings it was scanned under and the owner's manual dB up in the library when asked, and remembers the answer; nothing is read in advance."""

    def _lookup(self, key, path):
        """Returns the remembered record for a path, asking the library once; a path with no scanned gain on record is asked again after the recheck time, and a path the library has no gain or picture size for gets its old settings values carried in first."""
        # WARNING: an answer already held is read without the hub's lock (one dict read and one set check, each whole under Python's own lock); the screen thread took this lock thousands of times a second and waited for it behind the scans on a 4-core PC (owner log 2026-11-04). Writes still take the lock.
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
            if row is LIBRARY_BUSY:
                return held if held is not None else record
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

    def note_context_gain(self, path, context, total_db, series_key=None, baseline_db=None, change_db=None):
        """Saves the owner's total dB for a file inside one context (a server profile viewed here, or a streamed server) without touching the scanned gain or any other context; False when the library cannot key the file."""
        try:
            if not self.probe_library.store_context_gain(path, context, total_db, series_key, baseline_db, change_db):
                return False
        except Exception as exc:
            _log_swallowed("GainHub.note_context_gain: saving the owner's gain for one context", exc)
            return False
        with self._lock:
            self._context_memo.pop((context, _hub_key(path)), None)
        return True

    def read_context_gain(self, path, context):
        """The owner's saved total dB for a file in one context as a dict (total_db, baseline_db), or None; held in memory like the other hub reads."""
        key = (context, _hub_key(path))
        with self._lock:
            if key in self._context_memo:
                return self._context_memo[key]
        try:
            held = self.probe_library.context_gain(path, context)
        except Exception as exc:
            _log_swallowed("GainHub.read_context_gain: reading the owner's gain for one context", exc)
            return None
        with self._lock:
            self._context_memo[key] = held
        return held

    def note_context_off_gain(self, path, context, off_db):
        """Saves the owner's dB for a file while Loudness Scan is off inside one context; False when the library cannot key the file."""
        try:
            return bool(self.probe_library.store_context_off_gain(path, context, off_db))
        except Exception as exc:
            _log_swallowed("GainHub.note_context_off_gain: saving the Loudness-Scan-off dB for one context", exc)
            return False

    def read_context_off_gain(self, path, context):
        """The owner's Loudness-Scan-off dB for a file in one context, or None when none is set there."""
        try:
            return self.probe_library.context_off_gain(path, context)
        except Exception as exc:
            _log_swallowed("GainHub.read_context_off_gain: reading the Loudness-Scan-off dB for one context", exc)
            return None

    def read_context_series_changes(self, context, series_key):
        """(change dB, change time) of the owner's counted changes in one series inside one context, oldest first; [] when none."""
        try:
            return self.probe_library.library.context_series_rows(context, series_key)
        except Exception as exc:
            _log_swallowed("GainHub.read_context_series_changes: reading one context's loudness changes", exc)
            return []

    def note_learned_gain(self, path, gain, applied_db):
        """Replaces a scanned file's saved gain with its own scan plus the series' learned offset, keeping every other scan fact; False when the gain is the owner's or the library cannot hold it."""
        if not gain or gain <= 0 or self.read(path)["scan_version"] == _OWNER_GAIN_SCAN_VERSION:
            return False
        try:
            if not self.probe_library.store_file_fields(path, gain=float(gain), learn_applied_db=float(applied_db) if applied_db else None):
                return False
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_learned_gain: saving the offset gain to the library", _swallowed_error)
            return False
        key = _hub_key(path)
        with self._lock:
            held = self._memo.get(key)
            if held:
                self._memo[key] = dict(held, gain=float(gain))
        return True

    def note_file_fields(self, path, **fields):
        """Saves per-file fields (audio track choice, loudness learning) in the file's library row; False when the library cannot hold them yet."""
        try:
            return bool(self.probe_library.store_file_fields(path, **fields))
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_file_fields: saving per-file fields to the library", _swallowed_error)
            return False

    def read_file_fields(self, path, names):
        """The named per-file fields of a file, or None when the library cannot read them yet."""
        try:
            return self.probe_library.file_fields(path, names)
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.read_file_fields: reading per-file fields from the library", _swallowed_error)
            return None

    def read_series_changes(self, series_key):
        """(change dB, change time) of every owner loudness change saved in one series, oldest first; [] when none or unreadable."""
        try:
            return self.probe_library.library.series_learning_rows(series_key)
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.read_series_changes: reading the series' loudness changes from the library", _swallowed_error)
            return []

    def note_track_gains(self, path, primary, gains, scan_version=None):
        """Saves the scanned gain of every audio track of a file; False when the library cannot hold it."""
        try:
            return bool(self.probe_library.store_track_gains(path, primary, gains, scan_version))
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.note_track_gains: saving the per-track gains to the library", _swallowed_error)
            return False

    def read_track_gains(self, path):
        """The per-track gains of a file as stored by note_track_gains, or None."""
        try:
            return self.probe_library.track_gains(path)
        except Exception as _swallowed_error:
            _log_swallowed("GainHub.read_track_gains: reading the per-track gains from the library", _swallowed_error)
            return None

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


class FileFactsHub(_PathMemoHub):
    """Answers one file at a time: looks the file's caption source, playability verdicts and loudness retry timers up in the library when asked, and remembers the answer; nothing is read in advance."""

    @staticmethod
    def _blank():
        record = {name: None for name in FILE_FACT_COLUMNS}
        record["at"] = time.monotonic()
        return record

    def _lookup(self, key, path):
        """Returns the remembered record for a path, asking the library once; a path with nothing on record is asked again after the recheck time. Only a file the hub was told changed (forget) is re-keyed from disk; every other read goes by the saved path row and never opens the file."""
        # WARNING: this is called for every pending file by the scheduler, the guide and the preloader, on the main thread too; hashing a file here made 500 new files stall the whole app for a minute. A path nobody has keyed yet has no facts on record, so it reads as blank.
        # WARNING: an answer already held is read without the hub's lock (one dict read and one set check, each whole under Python's own lock); the screen thread took this lock thousands of times a second and waited for it behind the scans on a 4-core PC (owner log 2026-11-04). Writes still take the lock.
        held = self._memo.get(key)
        force = key in self._must_resolve
        now = time.monotonic()
        if held is not None and not force:
            if any(held[name] is not None for name in FILE_FACT_COLUMNS) or now - held["at"] < _GAIN_MISS_RECHECK_SECONDS:
                return held
        record = self._blank()
        try:
            if force:
                content_key = self.probe_library.keys.resolve(path)
                entry = self.probe_library.library.get_entry(content_key) if content_key is not None else None
                row = None if not entry else tuple(entry.get(name) for name in FILE_FACT_COLUMNS)
            else:
                row = self.probe_library.library.path_facts_row(key)
            if row is LIBRARY_BUSY:
                return held if held is not None else record
            if row:
                for name, value in zip(FILE_FACT_COLUMNS, row):
                    record[name] = value
        except Exception as _swallowed_error:
            _log_swallowed("FileFactsHub._lookup: reading the file's facts row from the library", _swallowed_error)
        with self._lock:
            self._must_resolve.discard(key)
            self._paths[key] = str(path)
            self._memo[key] = record
        return record

    def read_ahead(self, paths):
        """Reads from the library, in a few turns, the facts of every path whose answer is not held or is due to be asked again, and remembers each answer as a one-file look-up would. For background threads; the screen thread does nothing here."""
        if _time_critical():
            return
        wanted = _keys_to_read(self, paths, lambda held, now: not any(held[name] is not None for name in FILE_FACT_COLUMNS) and now - held["at"] >= _GAIN_MISS_RECHECK_SECONDS)
        if not wanted:
            return
        rows = self.probe_library.library.path_rows_many(list(wanted), ", ".join("l." + name for name in FILE_FACT_COLUMNS))
        with self._lock:
            for key, path in wanted.items():
                if key in self._must_resolve:
                    continue
                record = self._blank()
                row = rows.get(key)
                if row:
                    for name, value in zip(FILE_FACT_COLUMNS, row):
                        record[name] = value
                self._paths[key] = path
                self._memo[key] = record

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
_ROW_GENERATION_START_TOLERANCE_SECONDS = _governor.HUB_ROW_GENERATION_START_TOLERANCE_SECONDS if _governor is not None else 60.0

_GENERATION_MARK = "#g"

SCHEDULE_ROW_STATES = ("planned", "airing", "aired", "static")
_SCHEDULE_STATE_RANK = {"planned": 0, "airing": 1, "aired": 2, "static": 2}
_SCHEDULE_ROW_FIELDS = ("channel", "slot_key", "position", "path", "kind", "pass_no", "planned_start", "used_length", "length_source", "state")


def _forgets_show_facts(method):
    """Makes a hub write that changes what is scheduled throw away the station's remembered playback facts, so the scheduler is asked again."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            channel = args[0] if args else kwargs.get("channel")
            if channel is None:
                self._show_facts.clear()
            else:
                self._show_facts.pop(channel, None)
    return wrapper


class ScheduleHub:
    """Keeps each channel's ordered rows in the library: what is meant to play, when, for how long, and what has played."""
    # WARNING: a row that is airing, aired or static is history; no call here may change or remove it except mark() moving it forward, or aired rows and pass records are lost (R26) and a past slot moves (R18).

    def __init__(self, library: "MediaLibrary"):
        self._show_facts = {}
        self._upcoming = {}
        self.library = library

    @staticmethod
    def _as_dict(row):
        found = dict(zip(_SCHEDULE_ROW_FIELDS, row))
        found["channel"] = str(found["channel"]).rsplit(":", 1)[-1]
        return found

    @_forgets_show_facts
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

    @_forgets_show_facts
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

    @_forgets_show_facts
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

    def planned_rows_between(self, channel, start_epoch, end_epoch):
        """The channel's planned rows (episodes, movies and commercials) that overlap a time span, from sets that were not put aside, in start order; each carries guide_entry like rows_between with full."""
        sql = ("SELECT channel, slot_key, position, path, kind, pass_no, planned_start, used_length, length_source, state, guide_entry "
               "FROM hub_schedule_row WHERE channel = ? AND kind IN ('episode', 'movie', 'commercial') AND state = 'planned' "
               "AND planned_start IS NOT NULL AND planned_start < ? AND planned_start + COALESCE(used_length, 0) > ? "
               "AND NOT EXISTS (SELECT 1 FROM hub_schedule_set s WHERE s.channel = hub_schedule_row.channel "
               "AND s.slot_key = hub_schedule_row.slot_key AND s.state = 'set_aside') "
               "ORDER BY planned_start, slot_key, position")
        with self.library._lock:
            found = self.library._conn.execute(sql, (channel, float(end_epoch), float(start_epoch))).fetchall()
        rows = []
        for record in found:
            row = self._as_dict(record[:10])
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

    @_forgets_show_facts
    def clear_all_schedule_data(self):
        """Empties every channel's schedule: the planned, airing and aired rows, the future rows, the slot records and the play orders; returns {table: rows removed}."""
        # WARNING: only for a fresh start (no retro_config.json at boot, owner order 2026-11-02); everywhere else aired rows are history and stay (R26).
        removed = {}
        with self.library._lock:
            with self.library._conn as conn:
                for table in ("hub_schedule_row", "hub_future_row", "hub_schedule_set", "hub_plan_state", "hub_play_order", "hub_play_state"):
                    removed[table] = conn.execute("DELETE FROM %s" % table).rowcount
        return removed

    def plan_state(self, channel, name):
        """A value the server's planner keeps for a channel (decoded from JSON), or None when none is kept."""
        with self.library._lock:
            found = self.library._conn.execute("SELECT value FROM hub_plan_state WHERE channel = ? AND name = ?", (channel, str(name))).fetchone()
        if not found:
            return None
        try:
            return json.loads(found[0])
        except ValueError as _swallowed_error:
            _log_swallowed("ScheduleHub.plan_state: the kept value is unreadable and is worked out again", _swallowed_error)
            return None

    def set_plan_state(self, channel, name, value):
        """Keeps a value of the server's planner for a channel (any JSON value)."""
        with self.library._lock:
            with self.library._conn as conn:
                conn.execute("INSERT OR REPLACE INTO hub_plan_state (channel, name, value, updated_at) VALUES (?, ?, ?, ?)",
                             (channel, str(name), json.dumps(value), time.time()))

    def clear_channel(self, channel):
        """Removes every schedule row of one channel (planned, airing, aired and Static rows, its future rows and its set labels) and what is remembered of its show on air; returns {table: rows removed}."""
        # WARNING: owner 2026-11-24: "one time removal of the old style of user schedule". Only a clear of the whole app or of one channel may delete rows (R26); this one is called once per channel when a User moves over to the server's rows.
        removed = {}
        with self.library._lock:
            with self.library._conn as conn:
                for table in ("hub_schedule_row", "hub_future_row", "hub_schedule_set", "hub_plan_state"):
                    removed[table] = conn.execute("DELETE FROM %s WHERE channel = ?" % table, (channel,)).rowcount
        self._show_facts.pop(channel, None)
        self._upcoming.pop(channel, None)
        return removed

    def replace_future_rows(self, channel, rows):
        """Saves the channel's projected future rows, replacing the ones saved before; only rows that differ are written, a few at a time with the database let go between turns; each row is a dictionary with slot_key, position, path, kind, planned_start, used_length and guide_entry. Returns how many rows were written."""
        # WARNING: these rows are a plan worked out from the prebuilt order, never history: they live in their own table, so no airing, aired, static or planned row of the schedule is read, changed or removed here (R26), and nothing that asks "what is on air" can pick one up.
        # WARNING: owner log 2026-11-18: one change that deleted and wrote thousands of rows held the database up to 8 s while 15 jobs and the screen waited; rows that did not change are left alone, and the rest go in turns of HUB_FUTURE_ROWS_PER_TURN, each turn after the playback thread's step.
        stamp = time.time()
        wanted = {}
        for row in rows:
            if not row.get("path") or row.get("planned_start") is None or float(row.get("used_length") or 0) <= 0:
                continue
            entry = json.dumps(row["guide_entry"]) if row.get("guide_entry") else None
            wanted[(str(row["slot_key"]), int(row["position"]))] = (row["path"], row.get("kind", "episode"), float(row["planned_start"]),
                                                                   float(row["used_length"]), entry)
        with self.library._lock:
            held = {(r[0], int(r[1])): (r[2], r[3], float(r[4]), float(r[5]), r[6]) for r in self.library._conn.execute(
                "SELECT slot_key, position, path, kind, planned_start, used_length, guide_entry FROM hub_future_row WHERE channel = ?",
                (channel,)).fetchall()}
        gone = [k for k in held if k not in wanted]
        changed = [(k, v) for k, v in wanted.items() if held.get(k) != v]
        for start in range(0, max(len(gone), 1), _FUTURE_ROWS_PER_TURN):
            part = gone[start:start + _FUTURE_ROWS_PER_TURN]
            if not part:
                break
            _step_aside_for_playback()
            with self.library._lock:
                with self.library._conn as conn:
                    conn.executemany("DELETE FROM hub_future_row WHERE channel = ? AND slot_key = ? AND position = ?",
                                     [(channel, k[0], k[1]) for k in part])
        for start in range(0, len(changed), _FUTURE_ROWS_PER_TURN):
            part = changed[start:start + _FUTURE_ROWS_PER_TURN]
            _step_aside_for_playback()
            with self.library._lock:
                with self.library._conn as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO hub_future_row (channel, slot_key, position, path, kind, planned_start, used_length, guide_entry, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(channel, k[0], k[1], v[0], v[1], v[2], v[3], v[4], stamp) for k, v in part])
        return len(changed)

    def future_rows_between(self, channel, start_epoch, end_epoch):
        """The channel's projected future rows that overlap a time span, in start order, each with the facts the guide draws its cell from (a dictionary, or None when unreadable); on the screen thread while the database is busy, the answer last seen for that span, or LIBRARY_BUSY when none was."""
        sql = ("SELECT channel, slot_key, position, path, kind, planned_start, used_length, guide_entry FROM hub_future_row "
               "WHERE channel = ? AND planned_start < ? AND planned_start + used_length > ? ORDER BY planned_start, slot_key, position")
        memo_key = ("future", channel, float(start_epoch), float(end_epoch))
        if not self.library._take_for_reading(memo_key):
            # WARNING: owner log 2026-11-23: an empty answer while the database was busy was taken for "no future" and the guide kept OFF AIR for that window; LIBRARY_BUSY tells the caller the rows were not read.
            found = self.library._screen_memo.get(memo_key)
            if found is None:
                return LIBRARY_BUSY
        else:
            try:
                found = self.library._conn.execute(sql, (channel, float(end_epoch), float(start_epoch))).fetchall()
            finally:
                self.library._lock.release()
            if _time_critical():
                self.library._remember_for_screen(memo_key, found)
        return [{"channel": r[0], "slot_key": r[1], "position": r[2], "path": r[3], "kind": r[4], "planned_start": r[5],
                 "used_length": r[6], "guide_entry": self._decode_guide_entry(r[7])} for r in found]

    def future_signature(self, channel):
        """A small value that changes whenever the channel's projected future rows are replaced, so the guide can tell its saved future is out of date."""
        if not self.library._take_for_reading(("future_signature", channel)):
            return self.library._screen_memo.get(("future_signature", channel), (0, 0.0))
        try:
            count, newest = self.library._conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM hub_future_row WHERE channel = ?", (channel,)).fetchone()
        finally:
            self.library._lock.release()
        answer = (int(count or 0), float(newest or 0.0))
        self.library._remember_for_screen(("future_signature", channel), answer)
        return answer

    def past_signature(self, channel):
        """A small value that changes whenever a row of the channel that is not planned is added or moves forward, so the guide can tell its saved past is out of date."""
        with self.library._lock:
            count, newest = self.library._conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM hub_schedule_row WHERE channel = ? AND state != 'planned'", (channel,)).fetchone()
        return (int(count or 0), float(newest or 0.0))

    def remember_show_facts(self, channel, facts, epoch, guard=None, upcoming=None):
        """Records the playback facts the scheduler worked out for the show on air (file, length, block, seek at that moment); guard is a small value that must still match for the facts to be trusted. upcoming, when given, replaces the station's prepared items: (clock start, playback facts at that start) of the item on air and the items after it, in start order."""
        # WARNING: only the scheduler's door writes these; readers use show_facts().
        self._show_facts[channel] = {"facts": dict(facts), "epoch": float(epoch), "guard": guard}
        if upcoming is not None:
            self._upcoming[channel] = {"records": [(float(start), dict(item)) for start, item in upcoming], "guard": guard}

    def forget_show_facts(self, channel=None):
        """Drops the remembered playback facts and the prepared items of one station, or of all stations."""
        if channel is None:
            self._show_facts.clear()
            self._upcoming.clear()
        else:
            self._show_facts.pop(channel, None)
            self._upcoming.pop(channel, None)

    def show_facts(self, channel, epoch, guard=None):
        """The remembered playback facts for the show on air with the seek moved forward by the clock; once that show has run out, the prepared item whose clock slot holds epoch (which then becomes the show on air); None when nothing is remembered or prepared for epoch or the guard changed, and the caller then asks the scheduler through the hub's miss path."""
        kept = self._show_facts.get(channel)
        if kept is not None and kept["guard"] == guard and not self._newer_item_began(channel, kept["epoch"], float(epoch), guard):
            elapsed = float(epoch) - kept["epoch"]
            if elapsed >= 0:
                facts = dict(kept["facts"])
                seek = float(facts.get("seek", 0) or 0) + elapsed
                duration = float(facts.get("duration", 0) or 0)
                # WARNING: for a show cut out of the inside of a longer file the seek is a place in the whole file and the duration is only the slot, so the slot's own start is taken off before asking whether the show has run out (owner log 2026-11-06: ch08, ch18 and ch19 always missed the hub).
                slot_start = float(facts.get("seek_slot_offset", 0) or 0) if facts.get("interior_cut") else 0.0
                if duration > 0 and seek - slot_start < duration - 1.0:
                    facts["seek"] = seek
                    return facts
        return self._prepared_item(channel, float(epoch), guard)

    def _prepared_item(self, channel, epoch, guard):
        """The prepared item whose clock slot holds epoch, with its seek moved forward by the time since its start, kept as the show on air; None when none is prepared for epoch or the guard changed."""
        # WARNING: owner 2026-11-21: "STAY ON SCHEDULE AT ALL TIMES"; the next item, a commercial break and a channel change are answered from the items the scheduler prepared ahead with their clock starts, never looked up at the last minute.
        ahead = self._upcoming.get(channel)
        if ahead is None or ahead["guard"] != guard:
            return None
        # WARNING: owner test 2026-11-23: the item on air is the one that began last; an earlier item whose length runs past the next one's start must never take that one's place.
        began = None
        for start, item in ahead["records"]:
            if start > epoch:
                break
            began = (start, item)
        if began is None:
            return None
        start, item = began
        elapsed = epoch - start
        # WARNING: owner log 2026-11-23 02:41: no margin at the end; the next item begins the moment this one ends, and a gap there sent the screen to an answer 16 minutes old, which reopened a file that had aired long before for a moment in every break.
        if elapsed >= float(item.get("duration", 0) or 0):
            return None
        self._show_facts[channel] = {"facts": dict(item), "epoch": start, "guard": guard}
        facts = dict(item)
        facts["seek"] = float(item.get("seek", 0) or 0) + elapsed
        return facts

    def _newer_item_began(self, channel, since_epoch, epoch, guard):
        """True when a prepared item of the station began after since_epoch and by epoch, so the facts kept from before it are out of date."""
        ahead = self._upcoming.get(channel)
        if ahead is None or ahead["guard"] != guard:
            return False
        return any(since_epoch < start <= epoch for start, _item in ahead["records"])

    def prepared_items(self, channel, guard=None):
        """The station's prepared items as (clock start, playback facts at that start), in start order; empty when none are prepared or the guard changed."""
        ahead = self._upcoming.get(channel)
        if ahead is None or ahead["guard"] != guard:
            return []
        return [(start, dict(item)) for start, item in ahead["records"]]

    def upcoming(self, channel, after_epoch, count=2, kinds=("episode", "movie", "music")):
        """The next count shows of a channel that start at or after after_epoch, in start order; rows that have aired or went to Static and rows of a set put aside are left out. On the screen thread while the database is busy, the rows last seen that still start after after_epoch (the playback thread gets them only when they were prepared ahead, and otherwise reads)."""
        marks = ",".join("?" for _ in kinds)
        sql = ("SELECT channel, slot_key, position, path, kind, pass_no, planned_start, used_length, length_source, state "
               "FROM hub_schedule_row WHERE channel = ? AND planned_start IS NOT NULL AND planned_start >= ? "
               "AND state IN ('planned', 'airing') AND kind IN (" + marks + ") "
               "AND NOT EXISTS (SELECT 1 FROM hub_schedule_set s WHERE s.channel = hub_schedule_row.channel "
               "AND s.slot_key = hub_schedule_row.slot_key AND s.state = 'set_aside') "
               "ORDER BY planned_start, slot_key, position LIMIT ?")
        memo_key = ("upcoming", channel, int(count), tuple(kinds))
        if not self.library._take_for_reading(memo_key):
            found = [row for row in self.library._screen_memo.get(memo_key, []) if float(row[6]) >= float(after_epoch) - 0.5]
        else:
            try:
                found = self.library._conn.execute(sql, (channel, float(after_epoch) - 0.5) + tuple(kinds) + (int(count),)).fetchall()
            finally:
                self.library._lock.release()
            if _time_critical():
                self.library._remember_for_screen(memo_key, found)
        return [self._as_dict(row) for row in found]

    def at(self, channel, epoch, busy=None):
        """The channel's row that covers one clock moment (planned start up to planned start plus length), or None; a row that has aired, is airing or went to Static is preferred over a planned one, then the latest start. On the screen thread while the database is busy, busy is returned instead; the playback thread reads."""
        sql = ("SELECT channel, slot_key, position, path, kind, pass_no, planned_start, used_length, length_source, state "
               "FROM hub_schedule_row WHERE channel = ? AND planned_start IS NOT NULL AND planned_start <= ? "
               "AND planned_start + COALESCE(used_length, 0) > ? "
               "AND NOT (state = 'planned' AND EXISTS (SELECT 1 FROM hub_schedule_set s WHERE s.channel = hub_schedule_row.channel "
               "AND s.slot_key = hub_schedule_row.slot_key AND s.state = 'set_aside')) "
               "ORDER BY (state = 'planned'), planned_start DESC, updated_at DESC LIMIT 1")
        if not self.library._take_for_reading():
            return busy
        try:
            found = self.library._conn.execute(sql, (channel, float(epoch), float(epoch))).fetchone()
        finally:
            self.library._lock.release()
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

    def restore_static_to_airing(self, channel, path):
        """Puts the channel's newest Static row of this file back to airing, for a show that was marked Static and then played after all. Returns how many rows changed."""
        key = _hub_key(path)
        with self.library._lock:
            with self.library._conn:
                cur = self.library._conn.execute(
                    "UPDATE hub_schedule_row SET state = 'airing', updated_at = ? WHERE channel = ? AND path_key = ? AND state = 'static' "
                    "AND rowid = (SELECT rowid FROM hub_schedule_row WHERE channel = ? AND path_key = ? AND state = 'static' "
                    "ORDER BY updated_at DESC LIMIT 1)",
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


_schedule_scope_state = threading.local()
_schedule_scope_fn = [None]


def set_schedule_scope(fn):
    """Sets the function that names the setup in use ("" for Regular, otherwise a prefix like "profile:2:"), so a channel's rows are filed per setup and never mix."""
    _schedule_scope_fn[0] = fn


def schedule_scope_of_slot(slot_id):
    """The prefix a server profile's rows are filed under."""
    return "profile:%s:" % slot_id


class schedule_scope_for:
    """Reads and writes the hub inside the with-block for the named prefix instead of the setup in use (the server reading one profile's rows)."""

    def __init__(self, prefix):
        self.prefix = str(prefix)

    def __enter__(self):
        self._before = getattr(_schedule_scope_state, "override", None)
        _schedule_scope_state.override = self.prefix
        return self

    def __exit__(self, *exc_info):
        _schedule_scope_state.override = self._before
        return False


def _schedule_scope():
    """The prefix for the setup in use right now; empty for Regular or when no function is set."""
    forced = getattr(_schedule_scope_state, "override", None)
    if forced is not None:
        return forced
    fn = _schedule_scope_fn[0]
    if fn is None:
        return ""
    try:
        return str(fn() or "")
    except Exception as error:
        log.warning("[HUB SCHEDULE] could not read which setup is on, rows stay under Regular: %s", error)
        return ""


def _scoped_to_setup(method):
    """Files the channel argument of a hub call under the setup in use; calls inside a call keep the key they were given."""
    import functools

    @functools.wraps(method)
    def wrapper(self, channel=None, *args, **kwargs):
        if channel is None or getattr(_schedule_scope_state, "inside", False):
            return method(self, channel, *args, **kwargs)
        _schedule_scope_state.inside = True
        try:
            return method(self, _schedule_scope() + str(channel), *args, **kwargs)
        finally:
            _schedule_scope_state.inside = False
    return wrapper


# WARNING: owner 2026-11-23: the guide's table is built again only when the hub says a channel's schedule changed; every hub call that writes rows notes its channel and what kind of rows it wrote, and the guide's table worker takes the notes.
_schedule_changes = {}
_schedule_changes_lock = threading.Lock()
schedule_changed = threading.Event()
SCHEDULE_CHANGE_ALL = "*"
_SCHEDULE_CHANGE_KINDS = {"put_rows": "plan", "place_rows": "plan", "put_aired_rows": "history", "replace_future_rows": "future",
                          "mark": "progress", "mark_airing_static": "progress", "restore_static_to_airing": "progress",
                          "close_passed_rows": "progress", "clear_all_schedule_data": "all", "clear_channel": "history"}


def note_schedule_change(channel, kind):
    """Records that a channel's rows of this kind ("plan", "history", "future", "progress" or "all") were written; channel SCHEDULE_CHANGE_ALL for every channel."""
    key = SCHEDULE_CHANGE_ALL if channel is None or channel == SCHEDULE_CHANGE_ALL else str(channel).rsplit(":", 1)[-1].zfill(2)
    with _schedule_changes_lock:
        _schedule_changes.setdefault(key, set()).add(kind)
    schedule_changed.set()


def take_schedule_changes():
    """The schedule changes noted since the last call, as {channel: set of kinds}, and clears them."""
    with _schedule_changes_lock:
        taken = dict(_schedule_changes)
        _schedule_changes.clear()
        schedule_changed.clear()
    return taken


def _notes_schedule_change(method, kind):
    """Makes a hub call that writes rows note the change for the guide's table, whether it wrote one row or many."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            note_schedule_change(SCHEDULE_CHANGE_ALL if kind == "all" else (args[0] if args else kwargs.get("channel")), kind)
    return wrapper


for _noted_name, _noted_kind in _SCHEDULE_CHANGE_KINDS.items():
    setattr(ScheduleHub, _noted_name, _notes_schedule_change(getattr(ScheduleHub, _noted_name), _noted_kind))


for _scoped_name in ("put_rows", "set_info", "sets", "place_rows", "put_aired_rows", "rows", "rows_between", "planned_rows_between", "replace_future_rows", "future_rows_between",
                     "future_signature", "past_signature", "remember_show_facts", "forget_show_facts", "show_facts", "upcoming", "at", "mark",
                     "mark_airing_static", "restore_static_to_airing", "close_passed_rows", "counts", "clear_channel", "plan_state", "set_plan_state"):
    setattr(ScheduleHub, _scoped_name, _scoped_to_setup(getattr(ScheduleHub, _scoped_name)))


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
    "GainHub", "shared_gain_hub", "FileFactsHub", "shared_file_facts_hub", "FILE_FACT_COLUMNS", "PlayOrderHub", "shared_play_order_hub", "ScheduleHub", "shared_schedule_hub", "note_schedule_change", "take_schedule_changes", "schedule_changed", "set_schedule_scope", "schedule_scope_for", "schedule_scope_of_slot", "SCHEDULE_ROW_STATES", "DurationHub", "shared_duration_hub", "DURATION_KIND_MEASURED", "DURATION_KIND_ESTIMATED",
    "ESTIMATE_BASIS_SERIES_AVERAGE", "ESTIMATE_BASIS_MOVIE_DEFAULT",
]
