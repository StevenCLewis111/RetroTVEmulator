"""Persistent Probe Library -- item B of the 2026-09-19 handoff.

A SQLite-backed, content-addressed library that replaces the role of
db.config["media_probe_cache"] / ["dvd_gain_cache"] / ["caption_cache"] /
["pan_scan_library"] going forward, without deleting or depending on any of
them: this module reads none of the app's live state and writes nothing
into retro_config.json. It is a new, separate store the rest of the app can
be wired up to in a later, separately-reviewed pass -- see the module
docstring at the bottom ("INTEGRATION STATUS") for exactly what is and
isn't connected yet.

Design points implemented here, numbered to match the handoff doc's
"Agreed design" list under item B:

  1. Storage       -- MediaLibrary, a SQLite file in its own project
                       folder (resolve_media_library_dir(), same pattern as
                       media.resolve_loudness_dir()), WAL mode, one commit
                       per finished file.
  2. Identity key   -- compute_identity_key() / PathKeyCache: file size
                       plus a SHA-1 of three 64 KB samples (start, middle,
                       end). A path/size/mtime -> key table avoids
                       re-hashing an unchanged file. Growing files
                       (partial downloads) are skipped.
  3. Per-file fields -- the `library` table: duration, dimensions, DAR,
                       audio/subtitle streams, a picture-box (item D --
                       detect_picture_box() fills it), a loudness-gain slot
                       (tagged with target LUFS + scan version), a
                       captions slot (compressed cue text + source/
                       reason), and an optional pan-scan shot-memory slot.
  4. Combined probe -- combined_header_probe(): ONE `ffmpeg -i` parse ->
                       duration, dimensions, DAR, audio streams, subtitle
                       streams, replacing four separate subprocess calls
                       (media.get_media_video_dimensions,
                       media._probe_embedded_subtitle_streams,
                       media._asr_probe_media's duration read, and
                       media._probe_duration_seconds_fast) with one. VLC
                       duration (media.get_media_duration) stays available
                       as an explicit fallback via
                       probe_duration_with_fallback(), gated by the
                       existing media._duration_looks_implausible() check.
  5. Pre-caching    -- PrecachePool: a 2-thread low-priority pool with a
                       5-tier queue (now-playing+2, guide-highlighted,
                       scheduled-by-air-time, commercial pool, everything
                       else), then picture-box, then loudness at the
                       lowest priority sharing the same 2-thread cap.
  6. On-demand fallback -- get_or_probe_sync(): probes synchronously
                       (milliseconds, not the 1800 s placeholder) when a
                       needed file has no library entry yet.
  7. Migration      -- migrate_legacy_cache(): throttled, incremental
                       import of the existing media_probe_cache and
                       dvd_gain_cache dicts. Never deletes from the
                       source dicts -- they remain a valid read fallback
                       for any entry this module hasn't (yet) migrated,
                       e.g. because the file was missing at migration
                       time.
  8. Re-key on re-encode -- rekey_after_reencode(): call this when the
                       renormalization sweep finishes rewriting a file in
                       place. Content changed, so the identity key
                       changes; this carries the existing library row
                       (now-correct gain, in particular) forward onto the
                       new key instead of losing it.

None of this module's own probing logic re-implements measurement work
that already exists and is correct elsewhere in the app (VLC duration
parsing, LUFS loudness scanning, ASR transcription): those stay exactly
where they are in media.py / retro_tv_emulator.py. Picture-box detection
had no existing implementation anywhere (the handoff confirms there is no
crop-detection code), so detect_picture_box() lives here, next to the
header probe it shares a library row and a worker pass with. This module is
the storage + combined-header-probe + picture-box + scheduling layer around
the rest.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# 1. Storage location
# ---------------------------------------------------------------------------

_library_dir_ready = False


def resolve_media_library_dir(main_dir_resolver: Optional[Callable[[], str]] = None) -> str:
    """Return the program's own folder for the persistent probe library.

    Same reasoning/pattern as media.resolve_loudness_dir() and
    media.resolve_captions_dir(): this is an app-owned working file, not
    programming, and must not live inside a user's media folders (the
    folder scanner would pick it up) or be touched by "Reset All
    Settings" (which only resets db.config / retro_config.json -- this
    file is outside that entirely, by construction, matching the doc's
    "Never auto-pruned and never touched by Reset").

    main_dir_resolver defaults to media.resolve_main_dir so this follows
    the packaged .exe the same way the loudness/captions folders do;
    callers (or tests) may pass their own to avoid importing the full
    media module.
    """
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
    """Thin synchronous wrapper around the sqlite3 connection.

    One MediaLibrary per db file is expected to be shared across threads;
    sqlite3 connections aren't thread-safe by default, so this class
    takes its own lock around every statement rather than trusting
    callers to serialize access themselves (the pre-cache pool below
    runs multiple worker threads against the same MediaLibrary).
    """

    def __init__(self, db_path: str):
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

    # -- path -> content key -------------------------------------------------

    def lookup_path_key(self, norm_path: str, size: int, mtime: float) -> Optional[str]:
        """Return the cached content_key for norm_path IF its recorded
        size/mtime still match what's on disk right now -- i.e. we can
        skip re-hashing. None if there's no entry or it's stale."""
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

    def drop_path_key(self, norm_path: str):
        with self._lock:
            self._conn.execute("DELETE FROM path_keys WHERE norm_path=?", (norm_path,))
            self._conn.commit()

    # -- library rows ---------------------------------------------------------

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
        """Merge `fields` (column_name -> value) into the row for
        content_key, creating it if it doesn't exist yet. JSON-encodes
        list/dict values for the JSON-typed columns automatically."""
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

    def copy_entry(self, from_key: str, to_key: str, overwrite_fields: Optional[dict] = None):
        """Used by rekey_after_reencode(): duplicate from_key's row onto
        to_key (which may not exist yet), applying overwrite_fields on
        top, then leaves from_key's row alone (caller decides whether to
        delete it)."""
        entry = self.get_entry(from_key)
        if entry is None:
            entry = {}
        entry.pop("content_key", None)
        entry.pop("created_at", None)
        entry.pop("updated_at", None)
        if overwrite_fields:
            entry.update(overwrite_fields)
        entry = {k: v for k, v in entry.items() if v is not None}
        self.upsert_fields(to_key, **entry)

    def delete_entry(self, content_key: str):
        with self._lock:
            self._conn.execute("DELETE FROM library WHERE content_key=?", (content_key,))
            self._conn.commit()


# ---------------------------------------------------------------------------
# 2. Identity key
# ---------------------------------------------------------------------------

_SAMPLE_SIZE = 64 * 1024
_GROWING_FILE_CHECK_DELAY_S = 0.05


def _sample_offsets(size: int):
    """Start/middle/end 64 KB windows, clamped and de-duplicated for
    files smaller than 3x the sample size so nothing reads past EOF or
    hashes the same bytes twice."""
    if size <= 0:
        return []
    if size <= _SAMPLE_SIZE:
        return [0]
    offsets = {0, max(0, size - _SAMPLE_SIZE), max(0, (size - _SAMPLE_SIZE) // 2)}
    return sorted(offsets)


def compute_identity_key(file_path: str, growing_check_delay: float = _GROWING_FILE_CHECK_DELAY_S) -> Optional[str]:
    """File size plus a SHA-1 of up to three 64 KB samples (start, middle,
    end). Returns None (skip) for a file that's still growing -- caught
    by comparing size across a short delay, since a partial download's
    size is the one thing guaranteed to keep changing while active
    reads to it are actively landing."""
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
        with open(file_path, "rb") as f:
            for offset in _sample_offsets(size):
                f.seek(offset)
                h.update(f.read(_SAMPLE_SIZE))
    except OSError:
        return None
    return f"{size:x}-{h.hexdigest()}"


class PathKeyResolver:
    """Wraps MediaLibrary.lookup_path_key/store_path_key so callers get a
    stable content_key for a path without re-hashing on every call --
    only when the path's (size, mtime) has actually changed since the
    last time this resolver saw it."""

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
            return None  # growing file, or became unreadable mid-check
        # Re-stat: a growing-file check sleeps briefly, so use the freshest
        # size/mtime available for the path_keys row rather than the one
        # taken before compute_identity_key's own settle check.
        try:
            st = os.stat(file_path)
        except OSError:
            return None
        self.library.store_path_key(norm_path, st.st_size, st.st_mtime, content_key)
        return content_key


# ---------------------------------------------------------------------------
# 4. Combined header probe
# ---------------------------------------------------------------------------

import re as _re

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


def _resolve_ffmpeg(ffmpeg_path: Optional[str]) -> str:
    if ffmpeg_path:
        return ffmpeg_path
    import media as _media
    return _media._resolve_ffmpeg_path()


def combined_header_probe(file_path: str, ffmpeg_path: Optional[str] = None,
                           timeout: float = 15.0) -> HeaderProbeResult:
    """One `ffmpeg -i` header read, parsed for everything the four
    separate probes it replaces each pulled out of the identical stderr
    text: duration (was _probe_duration_seconds_fast /
    _asr_probe_media's own duration read), video dimensions + DAR (was
    get_media_video_dimensions), and subtitle streams (was
    _probe_embedded_subtitle_streams) -- plus an audio-stream list,
    which none of the four individually returned as structured data.

    No decode happens here (container/header metadata only), so this is
    the same, cheap cost tier the four originals were, just paid once
    instead of up to four times per file.
    """
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    try:
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
        return HeaderProbeResult(ok=False, error="no_input_stream")

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
    return out


def probe_duration_with_fallback(file_path: str, header_result: HeaderProbeResult) -> tuple:
    """Returns (duration_seconds_or_None, source_str). Uses the ffmpeg
    header duration when it's present and plausible; falls back to a
    full VLC parse (media.get_media_duration -- slower, but authoritative)
    only when ffmpeg's number looks implausible for the file's size, per
    the existing media._duration_looks_implausible() sanity check, or
    when ffmpeg found no duration at all."""
    import media as _media
    if header_result.duration and not _media._duration_looks_implausible(file_path, header_result.duration):
        return header_result.duration, "ffmpeg"
    vlc_duration = _media.get_media_duration(file_path)
    if vlc_duration:
        return vlc_duration, "vlc"
    return None, None


# ---------------------------------------------------------------------------
# 4b. Picture-box detection (item D -- detection and storage ONLY)
# ---------------------------------------------------------------------------
#
# Finds the real picture inside the frame, ignoring the pure-black bars a
# file carries baked in. Nothing here decides how the picture is FITTED to
# the screen (the 16:9 rule, Screen Mode, bezel on/off) -- that is the rule
# table item D still needs approved. This only measures and records.

PICTURE_BOX_DETECTOR_VERSION = 1

_CROPDETECT_RE = _re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

# Sample points as fractions of the runtime. 10%..85% skips studio logos and
# title cards at the start and the credits at the end, which are the two
# places a full-frame graphic is most likely to widen the box wrongly.
_PB_SAMPLE_FRACTIONS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)
# Used only when the runtime is unknown (seconds from the start).
_PB_FALLBACK_OFFSETS_S = (30.0, 120.0, 300.0)
_PB_SAMPLE_SECONDS = 2.0
_PB_SAMPLE_TIMEOUT_S = 25.0
_PB_MIN_GOOD_SAMPLES = 3
# cropdetect luma threshold. 16 is video black; 32 leaves headroom for the
# noisy bars analogue-capture rips have. Dark scenes only make a sample's box
# too SMALL, and taking the widest box across samples absorbs that.
_PB_LIMIT = 32
# A bar counts only if it is at least this fraction of its axis (and >= 4 px),
# and the two sides of an axis must match within this fraction of the larger.
_PB_MIN_BAR_FRACTION = 0.015
_PB_MIN_BAR_PX = 4
_PB_SYMMETRY_TOLERANCE = 0.25


def _pb_axis(start: int, length: int, frame: int):
    """Decide one axis (vertical or horizontal) from a detected span.

    Returns (new_start, new_length, kind) where kind is "bars", "none" or
    "asymmetric". Asymmetric or insubstantial bars leave the axis at full
    frame: cropping one side only would shift the picture, and a burnt-in
    subtitle sitting in one bar looks exactly like that.
    """
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


def _pb_run_sample(file_path: str, ffmpeg: str, t: float, timeout: float):
    """One short decode with cropdetect. Returns ffmpeg's stderr text, or
    None on timeout/launch failure. -threads 2 keeps it inside the same
    two-thread background cap the other ffmpeg scans use."""
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-threads", "2",
        "-ss", f"{t:.3f}", "-i", file_path, "-t", f"{_PB_SAMPLE_SECONDS:g}",
        "-map", "0:v:0", "-vf", f"cropdetect=limit={_PB_LIMIT}:round=2:reset=0",
        "-an", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
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
                        sample_timeout: float = _PB_SAMPLE_TIMEOUT_S) -> Optional[dict]:
    """Find the real picture inside the frame.

    Runs ffmpeg cropdetect at several points through the file and takes the
    WIDEST box across them (a dark scene can only make one sample's box too
    small, never too big). Each axis is then accepted as "bars" only if the
    bars are substantial and symmetric; otherwise that axis stays full
    frame.

    Returns a dict, or None when no answer could be reached (timeouts, too
    few readable samples) -- None is never stored, so it is retried later
    rather than recorded as "no bars".

      rect            [x, y, w, h] of the picture in the frame's own pixels
      frame           [w, h] of the frame those pixels belong to
      norm            rect as fractions of the frame, so it survives any
                      decode-time rescale
      has_bars        True if the rect is smaller than the frame
      reason          "letterbox" | "pillarbox" | "windowbox" | "no_bars" |
                      "asymmetric_vertical" | "asymmetric_horizontal" |
                      "asymmetric_both" | "no_video"
      pixel_aspect    display aspect / stored aspect from the file's DAR
                      (1.0 when the file declares none)
      picture_aspect  rect w/h times pixel_aspect -- the picture's shape as
                      it should be displayed. Measured, not classified: what
                      counts as "16:9" is the owner's rule-table decision.
      samples         number of samples that produced a box
      sample_boxes    each sample's own box, for diagnosing a bad result
      detector_version
    """
    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    frame_w = frame_h = None
    pixel_aspect = 1.0
    boxes = []

    for t in _pb_sample_times(duration):
        text = _pb_run_sample(file_path, ffmpeg, t, sample_timeout)
        if text is None:
            continue
        if frame_w is None:
            if "Input #0" not in text:
                return None
            vm = _VIDEO_DIMS_RE.search(text)
            if not vm:
                return _pb_no_video()
            # A cover-art picture inside an audio file is not video.
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
        w, h, x, y = (int(v) for v in found[-1])  # last line = running widest box
        if w > 0 and h > 0:
            boxes.append((x, y, w, h))

    if frame_w is None or len(boxes) < _PB_MIN_GOOD_SAMPLES:
        return None

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
    """Definitive answer for audio-only files and cover-art-only files, so
    they are recorded once instead of being re-scanned every pass."""
    return {"rect": None, "frame": None, "norm": None, "has_bars": False,
            "reason": "no_video", "detector_version": PICTURE_BOX_DETECTOR_VERSION}


# ---------------------------------------------------------------------------
# 3 + 4 combined: probing into the library
# ---------------------------------------------------------------------------

class ProbeLibrary:
    """The main entry point: ties PathKeyResolver + MediaLibrary +
    combined_header_probe together into get_or_probe_sync() /
    get_cached(), and hosts the pre-cache pool and migration helpers."""

    def __init__(self, library: MediaLibrary, ffmpeg_path: Optional[str] = None,
                 use_vlc_fallback: bool = True):
        self.library = library
        self.keys = PathKeyResolver(library)
        self.ffmpeg_path = ffmpeg_path
        self.use_vlc_fallback = use_vlc_fallback

    def get_cached(self, file_path: str) -> Optional[dict]:
        """Read-only: returns the library entry if one already exists for
        this path's current content, else None. Never probes."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        return self.library.get_entry(content_key)

    def get_or_probe_sync(self, file_path: str, max_age_s: Optional[float] = None) -> Optional[dict]:
        """Item 6, on-demand fallback: return a fresh-enough library
        entry, probing synchronously right now if there isn't one (or
        it's older than max_age_s). This is meant to replace a 1800 s
        placeholder-duration default with a real, few-hundred-ms probe
        the caller can afford to wait on -- see item C's "closes a hole
        behind item C" in the handoff doc."""
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

    def detect_picture_box_if_needed(self, file_path: str) -> Optional[dict]:
        """The picture-box hook for PrecachePool (and any caller that wants
        the answer now). Reads the library first; scans only when the file
        has no picture box yet or the stored one came from an older
        detector version, then stores the result on the same library row
        the header probe filled in. Returns the stored picture box, or None
        if the scan couldn't reach an answer (nothing is stored then, so a
        later pass tries again)."""
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return None
        entry = self.library.get_entry(content_key)
        if (entry and entry.get("picture_box")
                and (entry.get("picture_box_detector_version") or 0) >= PICTURE_BOX_DETECTOR_VERSION):
            return entry["picture_box"]
        if entry is None or not entry.get("header_probed_at"):
            entry = self.get_or_probe_sync(file_path)
        duration = (entry or {}).get("duration")
        box = detect_picture_box(file_path, duration=duration, ffmpeg_path=self.ffmpeg_path)
        if box is None:
            return None
        self.library.upsert_fields(
            content_key, picture_box=box,
            picture_box_detector_version=PICTURE_BOX_DETECTOR_VERSION,
        )
        return box

    # -- storage APIs for the other data types item 3 calls for --------------
    # These are plain storage; the actual measurement logic (picture-box
    # detection for item D, LUFS scanning, ASR transcription) lives where
    # it already does in media.py / retro_tv_emulator.py and is expected
    # to call these once it has a result, not be reimplemented here.

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
        except (zlib.error, UnicodeDecodeError):
            return None

    def store_pan_scan_memory(self, file_path: str, memory: dict):
        content_key = self.keys.resolve(file_path)
        if content_key is None:
            return False
        self.library.upsert_fields(content_key, pan_scan_memory=memory)
        return True

    # -- 8. re-key on re-encode ----------------------------------------------

    def rekey_after_reencode(self, file_path: str, preserve_fields: Optional[dict] = None):
        """Call once the renormalization sweep finishes rewriting
        `file_path` in place. The file's bytes changed, so its identity
        key changes -- this looks up whatever key it had BEFORE the
        rewrite (from path_keys, which still has the stale size/mtime
        pointing at the old content), copies that row's data onto the
        new, freshly-hashed key (so, e.g., the OLD gain measurement --
        now wrong for the renormalized audio -- doesn't silently keep
        being served under the new content), and drops the stale
        path_keys row so the next resolve() re-hashes for real.

        preserve_fields lets the caller pass the renorm sweep's own
        freshly-known-correct fields (typically the new gain/duration it
        just computed) to be written onto the new key directly, rather
        than copied stale from the old one.
        """
        norm_path = os.path.normpath(os.path.abspath(file_path)).lower()
        with self.library._lock:
            row = self.library._conn.execute(
                "SELECT content_key FROM path_keys WHERE norm_path=?", (norm_path,)
            ).fetchone()
        old_key = row[0] if row else None

        self.library.drop_path_key(norm_path)
        new_key = self.keys.resolve(file_path)
        if new_key is None:
            return None  # file vanished or is mid-write; nothing to carry forward yet

        if old_key and old_key != new_key:
            self.library.copy_entry(old_key, new_key, overwrite_fields=preserve_fields)
            self.library.delete_entry(old_key)
        elif preserve_fields:
            self.library.upsert_fields(new_key, **preserve_fields)
        return new_key


# ---------------------------------------------------------------------------
# 5. Pre-caching pool
# ---------------------------------------------------------------------------

# Lower number = higher priority, matching the doc's ordered list exactly.
TIER_NOW_PLAYING = 1        # now playing plus the next 2 items
TIER_GUIDE_HIGHLIGHT = 2    # the guide's highlighted channels
TIER_SCHEDULED = 3          # all scheduled tracks, by how soon they air
TIER_COMMERCIAL_POOL = 4    # the commercial pool
TIER_EVERYTHING_ELSE = 5    # everything else
TIER_LOUDNESS = 6           # lowest priority, shares the same 2-thread cap


@dataclass(order=True)
class _QueueItem:
    tier: int
    seq: int
    path: str = field(compare=False)
    picture_box_fn: Optional[Callable] = field(compare=False, default=None)


class PrecachePool:
    """2 low-priority worker threads draining a 5(+1)-tier priority
    queue. Item 5 in the doc: header probe first (this module), then
    picture-box detection (item D hook, no-op unless a detector is
    injected), then loudness at the lowest tier, sharing the same
    2-thread cap rather than getting its own pool."""

    def __init__(self, probe_library: ProbeLibrary, thread_count: int = 2,
                 loudness_fn: Optional[Callable[[str], None]] = None,
                 picture_box_fn: Optional[Callable[[str], None]] = None,
                 picture_box_current_version: Optional[int] = None):
        self.probe_library = probe_library
        self.thread_count = thread_count
        self.loudness_fn = loudness_fn
        self.picture_box_fn = picture_box_fn
        # When set, an entry whose stored detector version is older is
        # re-scanned (only that field), not just entries with no box yet.
        self.picture_box_current_version = picture_box_current_version
        self._q: "queue.PriorityQueue[_QueueItem]" = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._enqueued = set()  # (tier, path) currently in-flight/queued, avoids dup work
        self._enqueued_lock = threading.Lock()

    def enqueue(self, path: str, tier: int = TIER_EVERYTHING_ELSE):
        key = (tier, path)
        with self._enqueued_lock:
            if key in self._enqueued:
                return
            self._enqueued.add(key)
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        self._q.put(_QueueItem(tier=tier, seq=seq, path=path))

    def enqueue_many(self, paths, tier: int = TIER_EVERYTHING_ELSE):
        for p in paths:
            self.enqueue(p, tier)

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for i in range(self.thread_count):
            t = threading.Thread(target=self._worker_loop, name=f"precache-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, join_timeout: Optional[float] = 2.0):
        self._stop.set()
        for _ in self._threads:
            self._q.put(None)  # wake blocked workers
        for t in self._threads:
            t.join(timeout=join_timeout)
        self._threads = []

    def qsize(self) -> int:
        return self._q.qsize()

    def _worker_loop(self):
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                continue
            try:
                self._process(item)
            except Exception:
                pass
            finally:
                with self._enqueued_lock:
                    self._enqueued.discard((item.tier, item.path))

    def _picture_box_needed(self, entry: dict) -> bool:
        if not entry.get("picture_box"):
            return True
        current = self.picture_box_current_version
        return current is not None and (entry.get("picture_box_detector_version") or 0) < current

    def _process(self, item: _QueueItem):
        entry = self.probe_library.get_cached(item.path)
        if entry is None or not entry.get("header_probed_at"):
            self.probe_library.get_or_probe_sync(item.path)
            entry = self.probe_library.get_cached(item.path)

        picture_box_fn = item.picture_box_fn or self.picture_box_fn
        if picture_box_fn is not None and entry is not None and self._picture_box_needed(entry):
            try:
                picture_box_fn(item.path)
            except Exception:
                pass

        if item.tier == TIER_LOUDNESS and self.loudness_fn is not None:
            try:
                self.loudness_fn(item.path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 7. Migration from the legacy config-dict caches
# ---------------------------------------------------------------------------

@dataclass
class MigrationProgress:
    scanned: int = 0
    migrated: int = 0
    skipped_missing: int = 0
    skipped_growing: int = 0
    done: bool = False
    skipped_stale: int = 0      # legacy entry measured a different version of the file
    newly_written: int = 0      # entries that actually added something to the library


class LegacyCacheMigrator:
    """Throttled, resumable import of db.config["media_probe_cache"] and
    db.config["dvd_gain_cache"] into the new library. Call step()
    periodically (e.g. from the same kind of timer the app already uses
    for _orphan_cache_prune_loop) rather than all at once -- each call
    processes at most `batch_size` entries and returns.

    Never mutates or deletes from the source dicts: they keep serving as
    a valid read fallback (per the doc: "The old cache stays as a read
    fallback") for anything not yet migrated, most commonly because the
    file was missing on disk at migration time -- per the doc,
    "Entries for currently missing files are kept" in the legacy cache
    rather than migrated, since there's nothing on disk to hash into a
    content key yet. A later step() call will pick them up once/if the
    file reappears (e.g. a removable drive gets reconnected).
    """

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
        # The legacy cache clears every gain whenever the loudness target
        # changes, so whatever gains it still holds were measured against
        # the current target -- pass it here to tag migrated gains with it.
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
        budget = self._drain(self._media_probe_cache_iter, self._migrate_probe_entry, budget, "media_probe_cache")
        if budget > 0:
            budget = self._drain(self._dvd_gain_cache_iter, self._migrate_gain_entry, budget, "dvd_gain_cache")
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
        """True if the legacy entry carries the size/mtime it was measured
        at and the file on disk no longer matches -- the path now holds a
        different file, and its old numbers must not be filed under the
        new content's key. Same rule as the app's
        _media_probe_fingerprint_stale. An entry with no fingerprint is
        trusted, as everywhere else in the app."""
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
        """Add only what the library row doesn't already have. The library
        row can be newer than the legacy entry (a fresh probe, a later
        scan), so a re-run of the migration must never overwrite it.
        Returns True if anything was written."""
        existing = self.probe_library.library.get_entry(content_key) or {}
        to_write = {k: v for k, v in fields.items() if existing.get(k) is None}
        if "duration" in fields and existing.get("duration") is not None:
            to_write.pop("duration_source", None)   # source belongs with its duration
        if "gain" not in to_write:
            to_write.pop("gain_target_lufs", None)  # target belongs with its gain
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


# ---------------------------------------------------------------------------
# INTEGRATION STATUS (read this before wiring anything else to this module)
# ---------------------------------------------------------------------------
#
# Built and covered by tests (test_media_library.py):
#   - MediaLibrary schema/storage, WAL mode, upsert/copy/delete semantics.
#   - compute_identity_key(): stable across rename/move, distinguishes
#     changed content, skips a still-growing file.
#   - PathKeyResolver: re-hashes only when (size, mtime) changed.
#   - combined_header_probe(): parses duration/dims/DAR/audio/subtitle
#     streams from synthetic ffmpeg stderr text matching each of the four
#     original functions' own test fixtures.
#   - PrecachePool: tier ordering, de-duplication of in-flight items.
#   - LegacyCacheMigrator: batch/throttle behavior, missing-file skip,
#     growing-file skip, idempotent re-run.
#   - rekey_after_reencode(): old-key data lands on the new key, old key
#     is removed, preserve_fields overrides stale copied data.
#
# Deliberately NOT done in this pass, and not silently assumed:
#   - No call site in retro_tv_emulator.py or media.py has been changed.
#     The five duplicate header-probe functions the doc lists (item B,
#     "Five separate header probes") are all still in place and still
#     being called exactly as before. This module does not run yet
#     during normal app operation.
#   - Picture-box DETECTION exists (detect_picture_box(),
#     ProbeLibrary.detect_picture_box_if_needed()) and is stored, but
#     nothing in the app reads it. How a picture is FITTED to the screen
#     (the 16:9 rule, Screen Mode, bezel on/off, the +/-10% band) is the
#     item D rule table, still unapproved and not built. The watermark
#     needs no scan of its own: it follows the rectangle the video is
#     actually drawn into.
#   - No LUFS/ASR measurement logic was duplicated; store_gain() /
#     store_captions() are storage-only, meant to be called BY the
#     existing measurement code once it has a result, not to replace it.
#   - The pre-cache pool's tier-2/3/4 sources (guide highlighted
#     channels, the schedule, the commercial pool) aren't wired to real
#     app state yet -- PrecachePool.enqueue()/enqueue_many() are ready to
#     be called from wherever that state already lives in
#     retro_tv_emulator.py, but that call hasn't been added.
#
# Recommended next step, once this foundation itself is signed off: wire
# ONE read path at a time (duration is the natural first target, since
# item C's fix depends on get_or_probe_sync() replacing the 1800 s
# default) rather than swapping all ~40 call sites in one pass.
