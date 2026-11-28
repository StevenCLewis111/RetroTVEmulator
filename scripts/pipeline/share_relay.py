"""Share relay: serves a file on a server's share to the player through a loopback address, keeping each file's header and index on this PC's disk.

Why (owner 2026-11-14, CRT logs): opening an mkv or mp4 on the share means reading its index (the end of an mkv, the moov box of an mp4) in many small reads, and every read over the network waits on the server. The same file opened in 0.4 s right after a visit and in 10 s a minute later. This relay answers those reads from a local copy of the file's first and last megabytes and reads everything else from the share in large blocks, a good way ahead of the player, so the player never makes a small network read.

Only files whose index the player reads first (mkv, mp4, m4v, mov, avi, webm, flv) on a share path (two backslashes) go through here; everything else is opened directly as before. Any failure marks the file for a while and the next open of it goes straight to the share.
"""
import collections
import hashlib
import logging
import ntpath
import os
import queue
import re
import socket
import struct
import threading
import time
import urllib.parse

log = logging.getLogger(__name__)

try:
    import performance_governor as _governor
except Exception as _governor_import_error:
    _governor = None
    log.warning("[SHARE RELAY] could not import performance_governor, using the fallback values written in share_relay.py: %s", _governor_import_error)

# WARNING: owner 2026-11-26: every memo that grows with the library or with time is made by the cache manager, so none can grow without a limit.
import cache_manager

try:
    from performance_governor import log_swallowed as _log_swallowed
except Exception:
    def _log_swallowed(where, error):
        """Fallback used only when performance_governor cannot be imported: the failure line is lost, nothing else changes."""
        return None


# ============================================================================
# SECTION: Tunables
# ============================================================================

# WARNING: only files whose index the player reads before it can seek go through the relay; every other file opens straight from the share.
RELAY_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".flv")
# WARNING: the fallback numbers below must match the SHARE_RELAY_ values of the same names in performance_governor.py; they are used only when that module fails to import.
HEAD_CACHE_BYTES = _governor.SHARE_RELAY_HEAD_CACHE_BYTES if _governor is not None else 3145728
TAIL_CACHE_BYTES = _governor.SHARE_RELAY_TAIL_CACHE_BYTES if _governor is not None else 6291456
CACHE_CAP_BYTES = _governor.SHARE_RELAY_CACHE_CAP_BYTES if _governor is not None else 1258291200
BLOCK_BYTES = _governor.SHARE_RELAY_BLOCK_BYTES if _governor is not None else 1048576
READ_AHEAD_BLOCKS = _governor.SHARE_RELAY_READ_AHEAD_BLOCKS if _governor is not None else 24
RAMP_START_BLOCKS = _governor.SHARE_RELAY_RAMP_START_BLOCKS if _governor is not None else 2
INDEX_FILL_MAX_SECONDS = _governor.SHARE_RELAY_INDEX_FILL_MAX_SECONDS if _governor is not None else 8.0
FAIL_HOLD_SECONDS = _governor.SHARE_RELAY_FAIL_HOLD_SECONDS if _governor is not None else 120.0
IDLE_HANDLE_SECONDS = _governor.SHARE_RELAY_IDLE_HANDLE_SECONDS if _governor is not None else 60.0
IDLE_REAP_INTERVAL_SECONDS = _governor.SHARE_RELAY_IDLE_REAP_INTERVAL_SECONDS if _governor is not None else 15.0
HOLD_SECONDS = _governor.SHARE_RELAY_HOLD_SECONDS if _governor is not None else 150.0
MAX_IDLE_HANDLES_PER_FILE = _governor.SHARE_RELAY_MAX_IDLE_HANDLES_PER_FILE if _governor is not None else 2
MAX_REGISTERED_FILES = _governor.SHARE_RELAY_MAX_REGISTERED_FILES if _governor is not None else 256
HELD_FILES_MAX = _governor.SHARE_RELAY_HELD_FILES_MAX if _governor is not None else 1024
STALL_SECONDS = _governor.SHARE_RELAY_STALL_SECONDS if _governor is not None else 30.0
SLOW_ANSWER_SECONDS = _governor.SHARE_RELAY_SLOW_ANSWER_SECONDS if _governor is not None else 1.0
SLOW_WARN_REPEAT_SECONDS = _governor.SHARE_RELAY_SLOW_WARN_REPEAT_SECONDS if _governor is not None else 60.0
INDEX_WARN_REPEAT_SECONDS = _governor.SHARE_RELAY_INDEX_WARN_REPEAT_SECONDS if _governor is not None else 600.0
SLOW_WARN_FILES = _governor.SHARE_RELAY_SLOW_WARN_FILES if _governor is not None else 200
INDEX_WARN_FILES = _governor.SHARE_RELAY_INDEX_WARN_FILES if _governor is not None else 500
MP4_INDEX_MAX_BYTES = _governor.SHARE_RELAY_MP4_INDEX_MAX_BYTES if _governor is not None else 25165824
MKV_CUES_MAX_BYTES = _governor.SHARE_RELAY_MKV_CUES_MAX_BYTES if _governor is not None else 4194304
MKV_HEAD_SCAN_BYTES = _governor.SHARE_RELAY_MKV_HEAD_SCAN_BYTES if _governor is not None else 1048576
INDEX_REACH_FILES = _governor.SHARE_RELAY_INDEX_REACH_FILES if _governor is not None else 2000
KEYFRAME_TABLES_KEPT = _governor.SHARE_RELAY_KEYFRAME_TABLES_KEPT if _governor is not None else 64
STALE_TEMP_SECONDS = _governor.SHARE_RELAY_STALE_TEMP_SECONDS if _governor is not None else 3600.0
SNAPSHOT_TOTAL_BYTES = _governor.SHARE_RELAY_SNAPSHOT_TOTAL_BYTES if _governor is not None else 268435456
SNAPSHOT_FILE_BYTES = _governor.SHARE_RELAY_SNAPSHOT_FILE_BYTES if _governor is not None else 25165824
LONG_PATH_CHARS = _governor.LONG_PATH_CHARS if _governor is not None else 260
SERVED_AT_KEPT_FILES = _governor.SHARE_RELAY_SERVED_AT_KEPT_FILES if _governor is not None else 1024
# WARNING: the fallback below must match SHARE_RELAY_STREAM_MIN_BYTES in performance_governor.py; it is used only when that module fails to import.
STREAM_MIN_BYTES = getattr(_governor, "SHARE_RELAY_STREAM_MIN_BYTES", 2097152) if _governor is not None else 2097152
OTHER_TRACK_NEAR_BYTES = _governor.SHARE_RELAY_OTHER_TRACK_NEAR_BYTES if _governor is not None else 8388608
SNAPSHOT_MAX_AGE_SECONDS = _governor.SHARE_RELAY_SNAPSHOT_MAX_AGE_SECONDS if _governor is not None else 600.0
SNAPSHOT_BACK_SECONDS = _governor.SHARE_RELAY_SNAPSHOT_BACK_SECONDS if _governor is not None else 2.0
SNAPSHOT_READ_MAX_SECONDS = _governor.SHARE_RELAY_SNAPSHOT_READ_MAX_SECONDS if _governor is not None else 4.0
REQUEST_READ_TIMEOUT_SECONDS = _governor.SHARE_RELAY_REQUEST_READ_TIMEOUT_SECONDS if _governor is not None else 10.0
REQUEST_HEADER_MAX_BYTES = _governor.SHARE_RELAY_REQUEST_HEADER_MAX_BYTES if _governor is not None else 16384
WAIT_STEP_SECONDS = _governor.SHARE_RELAY_WAIT_STEP_SECONDS if _governor is not None else 0.5
LISTEN_BACKLOG = _governor.SHARE_RELAY_LISTEN_BACKLOG if _governor is not None else 64

# WARNING: the kept-copy file layout is a header (magic, file size, file time, head length, tail length) then the head and the tail; a new layout needs a new magic, or old copies are read wrong.
_MAGIC = b"RIX1"
_META = struct.Struct("<4sqqqq")

_CONTENT_TYPES = {
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".flv": "video/x-flv",
}


# ============================================================================
# SECTION: State
# ============================================================================

_index_warned = {}
_slow_warned = {}
_lock = threading.RLock()
_enabled = True
_cache_dir = None
_server = None
_server_thread = None
_tokens = collections.OrderedDict()
_token_of = {}
_failed = {}
_sources = {}
_held = {}
_served_at = {}
_sent_upto = {}


# ============================================================================
# SECTION: Switches and addresses
# ============================================================================


def _key(path):
    return os.path.normpath(str(path or "")).lower()


def _openable(path):
    """The path in the form Windows opens past 260 characters (a \\\\?\\ or \\\\?\\UNC\\ path); shorter paths and other systems get it unchanged."""
    # WARNING: owner logs 2026-11-25 (send_logs60): The Walking Dead World Beyond's files on the server's share are 262 characters long, so every open failed with "No such file" and ch09 stayed on Static on the streaming computer.
    path = str(path or "")
    if os.name != "nt" or len(path) < LONG_PATH_CHARS or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + os.path.abspath(path)


def set_enabled(value):
    """Turns the relay on or off (the owner's switch); off means every file is opened directly."""
    global _enabled
    _enabled = bool(value)


def set_cache_dir(path):
    """Where the kept index copies live (made when missing)."""
    global _cache_dir
    with _lock:
        _cache_dir = str(path) if path else None
    if _cache_dir:
        try:
            os.makedirs(_cache_dir, exist_ok=True)
        except OSError as exc:
            log.warning("[SHARE RELAY] could not make the index folder %s: %s", _cache_dir, exc)
            _cache_dir = None


def too_long(path):
    """True for a video path of 260 characters or more: Windows and the player cannot open it as it is, so it always plays through the relay, which opens it in the long form, whether it is on a share or on this PC and whether or not the relay is switched on."""
    # WARNING: owner 2026-11-25: "make it to where long names dont matter or slow down channel loading"; such a file never falls back to opening directly, which can only fail.
    text = str(path or "")
    return len(text) >= LONG_PATH_CHARS and not text.startswith("\\\\?\\") and ntpath.splitext(text)[1].lower() in RELAY_EXTENSIONS


def served_within(path, seconds):
    """True when the relay handed the player bytes of this file in the last `seconds` seconds: an open that is slow but still receiving."""
    at = _served_at.get(_key(path))
    return at is not None and time.monotonic() - at <= float(seconds)


def sent_upto(path):
    """The byte offset up to which the relay has handed the player this file in its longest-running request (the show playing on; an index or end read does not count), or None."""
    held = _sent_upto.get(_key(path))
    return None if held is None else held[0]


def seconds_at(path, offset):
    """The play time of a byte offset, from the file's keyframe table already in memory (the keyframe at or before the offset), or None when no table is held; never reads the disk, so the screen thread may call it."""
    with _lock:
        held = _keyframe_tables.get(_key(path))
    table = held[2] if held is not None else None
    if not table:
        return None
    lo, hi = 0, len(table)
    while lo < hi:
        mid = (lo + hi) // 2
        if table[mid][1] <= offset:
            lo = mid + 1
        else:
            hi = mid
    return float(table[max(0, lo - 1)][0])


def wants(path):
    """True when this file would be served through the relay (no disk or network access)."""
    text = str(path or "")
    return bool(_enabled and text.startswith("\\\\") and ntpath.splitext(text)[1].lower() in RELAY_EXTENSIONS)


def _recently_failed(key):
    at = _failed.get(key)
    if at is None:
        return False
    if time.monotonic() - at > FAIL_HOLD_SECONDS:
        _failed.pop(key, None)
        return False
    return True


def _mark_failed(path, why):
    with _lock:
        _failed[_key(path)] = time.monotonic()
    log.warning("[SHARE RELAY] %s failed through the relay (%s); it opens directly for the next %.0f s",
                os.path.basename(str(path)), why, FAIL_HOLD_SECONDS)


def url_for(path):
    """The loopback address that serves this share file, or None when it should be opened directly. Never touches the disk or the network, so the screen thread can call it."""
    try:
        long_form = too_long(path)
        if not (wants(path) or long_form):
            return None
        key = _key(path)
        with _lock:
            if _recently_failed(key) and not long_form:
                return None
            if not _ensure_server_locked():
                return None
            token = _token_of.get(key)
            if token is None:
                token = os.urandom(8).hex()
                _token_of[key] = token
                _tokens[token] = str(path)
                while len(_tokens) > MAX_REGISTERED_FILES:
                    _old_token, _old_path = _tokens.popitem(last=False)
                    _token_of.pop(_key(_old_path), None)
            else:
                _tokens.move_to_end(token)
            port = _server.server_address[1]
        name = urllib.parse.quote(ntpath.basename(str(path)))
        return "http://127.0.0.1:%d/f/%s/%s" % (port, token, name)
    except Exception as exc:
        log.warning("[SHARE RELAY] no address for %s: %s", os.path.basename(str(path)), exc)
        return None


def path_for_url(url):
    """The share path behind a relay address, or None for any other address."""
    try:
        text = str(url or "")
        match = re.match(r"^http://127\.0\.0\.1:(\d+)/f/([0-9a-f]{16})/", text)
        if not match:
            return None
        with _lock:
            if _server is None or _server.server_address[1] != int(match.group(1)):
                return None
            return _tokens.get(match.group(2))
    except Exception as _swallowed_error:
        _log_swallowed("path_for_url: reading a relay address", _swallowed_error)
        return None


# ============================================================================
# SECTION: Share file handles
# ============================================================================

class _Source:
    """One share file: its size and time, and a few idle handles kept open so each request does not pay the server's open again."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.idle = []
        self.size = None
        self.mtime_ns = None
        self.fill_lock = threading.Lock()

    def acquire(self):
        now = time.monotonic()
        with self.lock:
            while self.idle:
                handle, since = self.idle.pop()
                if now - since <= IDLE_HANDLE_SECONDS:
                    return handle
                try:
                    handle.close()
                except OSError as _swallowed_error:
                    _log_swallowed("_Source.acquire: closing an old share handle", _swallowed_error)
        handle = open(_openable(self.path), "rb", buffering=0)
        st = os.fstat(handle.fileno())
        self.size, self.mtime_ns = int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        return handle

    def release(self, handle, ok=True):
        if not ok:
            try:
                handle.close()
            except OSError as _swallowed_error:
                _log_swallowed("_Source.release: closing a failed share handle", _swallowed_error)
            return
        with self.lock:
            if len(self.idle) < MAX_IDLE_HANDLES_PER_FILE:
                self.idle.append((handle, time.monotonic()))
                return
        try:
            handle.close()
        except OSError as _swallowed_error:
            _log_swallowed("_Source.release: closing a spare share handle", _swallowed_error)

    def close_all(self):
        with self.lock:
            handles, self.idle = self.idle, []
            self.size = None
            self.mtime_ns = None
        for handle, _since in handles:
            try:
                handle.close()
            except OSError as _swallowed_error:
                _log_swallowed("_Source.close_all: closing a share handle", _swallowed_error)


def _source_for(path):
    key = _key(path)
    with _lock:
        src = _sources.get(key)
        if src is None:
            src = _Source(path)
            _sources[key] = src
            if len(_sources) > MAX_REGISTERED_FILES:
                for old_key in [k for k in _sources if k != key][:len(_sources) - MAX_REGISTERED_FILES]:
                    _sources.pop(old_key).close_all()
        return src


def hold(paths, seconds=None):
    """Keeps the idle handles of these files from being closed by the sweeper for this many seconds from now (each call renews them); the player's caller names the files airing now or next on every streamed channel."""
    until = time.monotonic() + (HOLD_SECONDS if seconds is None else float(seconds))
    with _lock:
        for path in paths:
            _held[_key(path)] = until
        if len(_held) > HELD_FILES_MAX:
            now = time.monotonic()
            for key in [k for k, t in _held.items() if t < now]:
                _held.pop(key, None)


def _is_held(key, now):
    with _lock:
        until = _held.get(key)
        if until is None:
            return False
        if until < now:
            _held.pop(key, None)
            return False
        return True


def _reap_idle_handles():
    """Closes idle handles older than IDLE_HANDLE_SECONDS, except those of held files."""
    now = time.monotonic()
    with _lock:
        sources = list(_sources.items())
    for key, src in sources:
        if _is_held(key, now):
            continue
        with src.lock:
            keep = []
            for handle, since in src.idle:
                if now - since > IDLE_HANDLE_SECONDS:
                    try:
                        handle.close()
                    except OSError as _swallowed_error:
                        _log_swallowed("_reap_idle_handles: closing an idle share handle", _swallowed_error)
                else:
                    keep.append((handle, since))
            src.idle = keep


# ============================================================================
# SECTION: The kept index copy
# ============================================================================

def _cache_file(path):
    if not _cache_dir:
        return None
    return os.path.join(_cache_dir, hashlib.sha1(_key(path).encode("utf-8", "replace")).hexdigest() + ".rix")


def _read_exact(handle, offset, length, deadline=None):
    handle.seek(offset)
    parts = []
    got = 0
    while got < length:
        if deadline is not None and time.monotonic() > deadline:
            return None
        chunk = handle.read(min(BLOCK_BYTES, length - got))
        if not chunk:
            return None
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _read_partial(handle, offset, length, deadline=None):
    """The bytes read from offset up to length or the deadline, whichever comes first; what was read is kept, never thrown away."""
    handle.seek(offset)
    parts = []
    got = 0
    while got < length:
        if deadline is not None and time.monotonic() > deadline:
            break
        chunk = handle.read(min(BLOCK_BYTES, length - got))
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _load_meta(cache_path):
    """(size, mtime_ns, head_len, tail_len) of a kept copy, or None."""
    try:
        with open(cache_path, "rb") as fh:
            raw = fh.read(_META.size)
        if len(raw) != _META.size:
            return None
        magic, size, mtime_ns, head_len, tail_len = _META.unpack(raw)
        if magic != _MAGIC:
            return None
        return size, mtime_ns, head_len, tail_len
    except FileNotFoundError:
        return None
    except OSError as _swallowed_error:
        _log_swallowed("_load_meta: reading a kept index copy", _swallowed_error)
        return None


def has_index(path, size=None, mtime_ns=None):
    """True when a current kept copy of this file's header and index exists on this PC."""
    cache_path = _cache_file(path)
    meta = _load_meta(cache_path) if cache_path else None
    if meta is None:
        return False
    if size is not None and (meta[0] != size or (mtime_ns is not None and meta[1] != mtime_ns)):
        return False
    return True


_index_reach_checked = {}


def ensure_index(path, source=None, max_seconds=INDEX_FILL_MAX_SECONDS):
    """Keeps a copy of the file's first and last megabytes on this PC's disk when none is current; returns the meta (size, mtime_ns, head_len, tail_len) or None. Runs off the screen thread: the channel warm calls it, and a file's first request calls it when the warm has not."""
    cache_path = _cache_file(path)
    if not cache_path:
        return None
    src = source or _source_for(path)
    with src.fill_lock:
        handle = None
        ok = False
        try:
            handle = src.acquire()
            size, mtime_ns = src.size, src.mtime_ns
            meta = _load_meta(cache_path)
            head_len = min(HEAD_CACHE_BYTES, size)
            tail_len = min(TAIL_CACHE_BYTES, max(0, size - head_len))
            if meta is not None and meta[0] == size and meta[1] == mtime_ns:
                # WARNING: the kept copy is measured only when its MP4 index could not be read from it; a file whose table is held costs nothing here, as before.
                _measured = _index_reach_checked.get(_key(path)) == (size, mtime_ns)
                kept = (_read_kept(path) if not _measured and ntpath.splitext(str(path))[1].lower() in (".mp4", ".m4v", ".mov")
                        and keyframe_table(path) is None else None)
                reach = _mp4_index_reach(kept[0], size, path, lambda o, n: _read_exact(handle, o, n, time.monotonic() + max_seconds)) if kept is not None else None
                _index_reach_checked[_key(path)] = (size, mtime_ns)
                if len(_index_reach_checked) > INDEX_REACH_FILES:
                    _index_reach_checked.clear()
                if reach is None or (reach[0] <= meta[2] and reach[1] <= meta[3]):
                    try:
                        os.utime(cache_path, None)
                    except OSError as _swallowed_error:
                        _log_swallowed("ensure_index: marking a kept index copy as used", _swallowed_error)
                    ok = True
                    return meta
                # WARNING: owner log 2026-11-24: a long movie's MP4 index was bigger than the kept start, so no time could be found in it and its channel never had its airing part read ahead; the kept copy is made again with the whole index.
                head_len, tail_len = max(head_len, reach[0]), max(tail_len, reach[1])
                log.info("[SHARE RELAY] the index of %s is bigger than the part kept on this PC; keeping %d KB from the start and %d KB from the end",
                         os.path.basename(str(path)), head_len // 1024, tail_len // 1024)
            deadline = time.monotonic() + max_seconds
            head = _read_exact(handle, 0, head_len, deadline) if head_len else b""
            reach = None
            if head is not None and meta is None:
                reach = _mp4_index_reach(_View(head, b"", size), size, path, lambda o, n: _read_exact(handle, o, n, deadline))
                if reach is not None and reach[0] > head_len:
                    more = _read_exact(handle, head_len, reach[0] - head_len, deadline)
                    head = None if more is None else head + more
                    head_len = reach[0]
            tail_len = min(tail_len, max(0, size - head_len))
            tail = _read_exact(handle, size - tail_len, tail_len, deadline) if tail_len else b""
            if head is not None and tail is not None and reach is not None and tail_len:
                if reach[1] > tail_len:
                    extra = min(reach[1], size - head_len) - tail_len
                    more = _read_exact(handle, size - tail_len - extra, extra, deadline) if extra > 0 else b""
                    tail = None if more is None else more + tail
                    tail_len += max(0, extra)
            if head is None or tail is None:
                ok = True
                return None
            tmp = cache_path + ".tmp%d" % threading.get_ident()
            with open(tmp, "wb") as fh:
                fh.write(_META.pack(_MAGIC, size, mtime_ns, head_len, tail_len))
                fh.write(head)
                fh.write(tail)
            os.replace(tmp, cache_path)
            with _lock:
                # WARNING: a table made from the old kept copy (None when its index was cut off) is dropped, or the new copy would never be read for it.
                _keyframe_tables.pop(_key(path), None)
            _trim_cache()
            ok = True
            return (size, mtime_ns, head_len, tail_len)
        except OSError as exc:
            # WARNING: owner 2026-11-15: one missing file asked again every few seconds wrote this line 192 times; the same file is written once every 10 minutes.
            _now = time.monotonic()
            _name = os.path.basename(str(path))
            if _now - _index_warned.get(_name, -1e9) >= INDEX_WARN_REPEAT_SECONDS:
                if len(_index_warned) > INDEX_WARN_FILES:
                    _index_warned.clear()
                _index_warned[_name] = _now
                log.warning("[SHARE RELAY] could not keep the index of %s: %s", _name, exc)
            return None
        finally:
            if handle is not None:
                src.release(handle, ok=ok)


def _trim_cache():
    """Removes the least recently used kept copies until the folder is under the cap."""
    try:
        if not _cache_dir:
            return
        entries = []
        total = 0
        for name in os.listdir(_cache_dir):
            full = os.path.join(_cache_dir, name)
            try:
                st = os.stat(full)
            except OSError as _swallowed_error:
                _log_swallowed("_trim_cache: reading a kept copy's size", _swallowed_error)
                continue
            if ".tmp" in name:
                if time.time() - st.st_mtime > STALE_TEMP_SECONDS:
                    try:
                        os.remove(full)
                    except OSError as _swallowed_error:
                        _log_swallowed("_trim_cache: removing an unfinished copy", _swallowed_error)
                continue
            entries.append((st.st_mtime, st.st_size, full))
            total += st.st_size
        entries.sort()
        while total > CACHE_CAP_BYTES and entries:
            _mtime, size, full = entries.pop(0)
            try:
                os.remove(full)
                total -= size
            except OSError as _swallowed_error:
                _log_swallowed("_trim_cache: removing an old kept copy", _swallowed_error)
    except OSError as exc:
        log.warning("[SHARE RELAY] trimming the index folder failed: %s", exc)


# ============================================================================
# SECTION: Where a time is in a file (key frames from the kept head and tail)
# ============================================================================

class _View:
    """The kept first and last megabytes of a file as one addressable thing: get(offset, length) answers only when the whole range is in one of them."""

    def __init__(self, head, tail, size):
        self.head, self.tail, self.size = head, tail, size
        self.tail_start = size - len(tail)

    def get(self, offset, length):
        if length < 0 or offset < 0:
            return None
        if offset + length <= len(self.head):
            return self.head[offset:offset + length]
        if self.tail and offset >= self.tail_start and offset + length <= self.size:
            return self.tail[offset - self.tail_start:offset - self.tail_start + length]
        return None


def _ebml_vint(buf, pos, keep_marker):
    """(value, length) of the EBML number at pos, or None; the all-ones size ('unknown') comes back as -1."""
    if pos >= len(buf):
        return None
    first = buf[pos]
    length, mask = 1, 0x80
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(buf):
        return None
    value = first if keep_marker else first & (mask - 1)
    all_ones = (first & (mask - 1)) == (mask - 1)
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
        all_ones = all_ones and buf[pos + i] == 0xFF
    if not keep_marker and all_ones:
        return -1, length
    return value, length


def _ebml_children(buf):
    """(id, payload_start, payload_end) of each element in buf, stopping at the first one that does not fit."""
    pos = 0
    while pos < len(buf):
        ident = _ebml_vint(buf, pos, True)
        if ident is None:
            return
        size = _ebml_vint(buf, pos + ident[1], False)
        if size is None:
            return
        start = pos + ident[1] + size[1]
        end = len(buf) if size[0] < 0 else start + size[0]
        yield ident[0], start, min(end, len(buf)), end > len(buf)
        pos = end


def _ebml_uint(buf, start, end):
    return int.from_bytes(bytes(buf[start:end]), "big") if end > start else 0


def _mkv_keyframes(view):
    """[(seconds, byte offset of the cluster)] from the Cues of a Matroska or WebM file, or None."""
    top = view.get(0, min(len(view.head), MKV_HEAD_SCAN_BYTES))
    if not top:
        return None
    seg = None
    for ident, start, end, _cut in _ebml_children(top):
        if ident == 0x18538067:
            seg = start
            break
    if seg is None:
        return None
    scale = 1000000
    cues_at = None
    body = top[seg:]
    for ident, start, end, _cut in _ebml_children(body):
        if ident == 0x1F43B675:
            break
        if ident == 0x1549A966:
            for cid, cs, ce, _c in _ebml_children(body[start:end]):
                if cid == 0x2AD7B1:
                    scale = _ebml_uint(body[start:end], cs, ce) or scale
        elif ident == 0x114D9B74:
            for sid, ss, se, _c in _ebml_children(body[start:end]):
                if sid == 0x4DBB:
                    entry = body[start:end][ss:se]
                    sid_bytes, sid_pos = None, None
                    for eid, es, ee, _c2 in _ebml_children(entry):
                        if eid == 0x53AB:
                            sid_bytes = bytes(entry[es:ee])
                        elif eid == 0x53AC:
                            sid_pos = _ebml_uint(entry, es, ee)
                    if sid_bytes == b"\x1c\x53\xbb\x6b" and sid_pos is not None:
                        cues_at = sid_pos
        elif ident == 0x1C53BB6B:
            return _mkv_cue_points(body[start:end], seg, scale)
    if cues_at is None:
        return None
    head_bytes = view.get(seg + cues_at, 12)
    if not head_bytes:
        return None
    ident = _ebml_vint(head_bytes, 0, True)
    size = _ebml_vint(head_bytes, ident[1], False) if ident else None
    if not ident or ident[0] != 0x1C53BB6B or not size or size[0] < 0 or size[0] > MKV_CUES_MAX_BYTES:
        return None
    payload = view.get(seg + cues_at + ident[1] + size[1], size[0])
    return _mkv_cue_points(payload, seg, scale) if payload else None


def _mkv_cue_points(buf, seg_start, scale):
    out = []
    for ident, start, end, _cut in _ebml_children(buf):
        if ident != 0xBB:
            continue
        point = buf[start:end]
        t = None
        pos = None
        for cid, cs, ce, _c in _ebml_children(point):
            if cid == 0xB3:
                t = _ebml_uint(point, cs, ce)
            elif cid == 0xB7 and pos is None:
                for tid, ts, te, _c2 in _ebml_children(point[cs:ce]):
                    if tid == 0xF1:
                        pos = _ebml_uint(point[cs:ce], ts, te)
        if t is not None and pos is not None:
            out.append((t * scale / 1e9, seg_start + pos))
    out.sort()
    return out or None


def _mp4_boxes(buf):
    """(type, payload_start, payload_end) of each box in buf."""
    pos = 0
    while pos + 8 <= len(buf):
        size, kind = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1 and pos + 16 <= len(buf):
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = len(buf) - pos
        if size < header:
            return
        yield kind, pos + header, min(pos + size, len(buf))
        pos += size


def _mp4_index_reach(view, size, path, read_at=None):
    """(bytes needed from the start, bytes needed from the end) to hold an MP4's whole index (moov box) on this PC, worked out from the top-level box headers: from the view where it holds them, else read_at(offset, length) reads the 16 header bytes from the file. None for a file that is not MP4 or MOV, an index bigger than MP4_INDEX_MAX_BYTES, or one whose place cannot be told."""
    if ntpath.splitext(str(path))[1].lower() not in (".mp4", ".m4v", ".mov"):
        return None
    off = 0
    while off < size:
        want = 16 if off + 16 <= size else 8
        raw = view.get(off, want)
        if raw is None and read_at is not None:
            raw = read_at(off, want)
        if raw is None or len(raw) < 8:
            return None
        box, kind = struct.unpack_from(">I4s", raw, 0)
        if box == 1 and len(raw) >= 16:
            box = struct.unpack_from(">Q", raw, 8)[0]
        elif box == 0:
            box = size - off
        if box < 8:
            return None
        if kind == b"moov":
            if box > MP4_INDEX_MAX_BYTES:
                return None
            if off < len(view.head):
                return (off + box, 0)
            return (0, size - off)
        off += box
    return None


def _mp4_keyframes(view):
    """[(seconds, byte offset)] of the video sync samples from the moov box of an MP4 or MOV, or None."""
    off = 0
    moov = None
    while off < view.size and moov is None:
        raw = view.get(off, 16)
        if raw is None or len(raw) < 8:
            break
        size, kind = struct.unpack_from(">I4s", raw, 0)
        header = 8
        if size == 1 and len(raw) >= 16:
            size = struct.unpack_from(">Q", raw, 8)[0]
            header = 16
        elif size == 0:
            size = view.size - off
        if size < header:
            return None
        if kind == b"moov":
            if size > MP4_INDEX_MAX_BYTES:
                return None
            moov = view.get(off + header, size - header)
            break
        off += size
    if not moov:
        return None
    for kind, start, end in _mp4_boxes(moov):
        if kind != b"trak":
            continue
        table = _mp4_track_keyframes(moov[start:end])
        if table:
            return table
    return None


def _mp4_track_keyframes(trak):
    mdia = next((trak[s:e] for k, s, e in _mp4_boxes(trak) if k == b"mdia"), None)
    if mdia is None:
        return None
    timescale, handler, stbl = 0, b"", None
    for kind, s, e in _mp4_boxes(mdia):
        if kind == b"hdlr":
            handler = bytes(mdia[s + 8:s + 12])
        elif kind == b"mdhd":
            version = mdia[s]
            timescale = struct.unpack_from(">I", mdia, s + (20 if version == 1 else 12))[0]
        elif kind == b"minf":
            minf = mdia[s:e]
            stbl = next((minf[a:b] for k, a, b in _mp4_boxes(minf) if k == b"stbl"), None)
    if handler != b"vide" or not timescale or stbl is None:
        return None
    boxes = {k: stbl[s:e] for k, s, e in _mp4_boxes(stbl)}
    try:
        stts, stsc, stsz = boxes[b"stts"], boxes[b"stsc"], boxes[b"stsz"]
        chunk_box = boxes.get(b"stco")
        wide = False
        if chunk_box is None:
            chunk_box, wide = boxes[b"co64"], True
        n_stts = struct.unpack_from(">I", stts, 4)[0]
        runs = [struct.unpack_from(">II", stts, 8 + 8 * i) for i in range(n_stts)]
        n_stsc = struct.unpack_from(">I", stsc, 4)[0]
        firsts = [struct.unpack_from(">III", stsc, 8 + 12 * i) for i in range(n_stsc)]
        fixed, count = struct.unpack_from(">II", stsz, 4)
        sizes = [fixed] * count if fixed else list(struct.unpack_from(">%dI" % count, stsz, 12))
        n_chunks = struct.unpack_from(">I", chunk_box, 4)[0]
        offsets = list(struct.unpack_from(">%d%s" % (n_chunks, "Q" if wide else "I"), chunk_box, 8))
        if b"stss" in boxes:
            n_sync = struct.unpack_from(">I", boxes[b"stss"], 4)[0]
            sync = [v - 1 for v in struct.unpack_from(">%dI" % n_sync, boxes[b"stss"], 8)]
        else:
            sync = list(range(count))
    except (KeyError, struct.error):
        return None
    want = set(sync)
    sample_offset = {}
    sample = 0
    for ci in range(n_chunks):
        per = 0
        for first, spc, _desc in firsts:
            if first - 1 <= ci:
                per = spc
            else:
                break
        pos = offsets[ci]
        for _ in range(per):
            if sample >= count:
                break
            if sample in want:
                sample_offset[sample] = pos
            pos += sizes[sample]
            sample += 1
    out = []
    sync_sorted = sorted(sync)
    si = 0
    t_units, base = 0, 0
    for run_count, delta in runs:
        while si < len(sync_sorted) and sync_sorted[si] < base + run_count:
            idx = sync_sorted[si]
            if idx in sample_offset:
                out.append(((t_units + (idx - base) * delta) / timescale, sample_offset[idx]))
            si += 1
        t_units += run_count * delta
        base += run_count
    return out or None


def _mp4_track_chunk_table(trak):
    """[(seconds, byte offset)] of each chunk of a sound or subtitle track of an MP4 or MOV (every chunk is a place the player reads from), or None for a video track or one that cannot be read."""
    mdia = next((trak[s:e] for k, s, e in _mp4_boxes(trak) if k == b"mdia"), None)
    if mdia is None:
        return None
    timescale, handler, stbl = 0, b"", None
    for kind, s, e in _mp4_boxes(mdia):
        if kind == b"hdlr":
            handler = bytes(mdia[s + 8:s + 12])
        elif kind == b"mdhd":
            version = mdia[s]
            timescale = struct.unpack_from(">I", mdia, s + (20 if version == 1 else 12))[0]
        elif kind == b"minf":
            minf = mdia[s:e]
            stbl = next((minf[a:b] for k, a, b in _mp4_boxes(minf) if k == b"stbl"), None)
    if handler == b"vide" or not timescale or stbl is None:
        return None
    boxes = {k: stbl[s:e] for k, s, e in _mp4_boxes(stbl)}
    try:
        stts, stsc = boxes[b"stts"], boxes[b"stsc"]
        chunk_box = boxes.get(b"stco")
        wide = False
        if chunk_box is None:
            chunk_box, wide = boxes[b"co64"], True
        n_stts = struct.unpack_from(">I", stts, 4)[0]
        runs = [struct.unpack_from(">II", stts, 8 + 8 * i) for i in range(n_stts)]
        n_stsc = struct.unpack_from(">I", stsc, 4)[0]
        firsts = [struct.unpack_from(">III", stsc, 8 + 12 * i) for i in range(n_stsc)]
        n_chunks = struct.unpack_from(">I", chunk_box, 4)[0]
        offsets = struct.unpack_from(">%d%s" % (n_chunks, "Q" if wide else "I"), chunk_box, 8)
    except (KeyError, struct.error):
        return None
    out = []
    run_i, run_left, t_units = 0, (runs[0][0] if runs else 0), 0
    fi = 0
    for ci in range(n_chunks):
        while fi + 1 < len(firsts) and firsts[fi + 1][0] - 1 <= ci:
            fi += 1
        per = firsts[fi][1] if firsts else 0
        out.append((t_units / float(timescale), offsets[ci]))
        left = per
        while left > 0 and run_i < len(runs):
            step = min(left, run_left)
            t_units += step * runs[run_i][1]
            left -= step
            run_left -= step
            if run_left <= 0:
                run_i += 1
                run_left = runs[run_i][0] if run_i < len(runs) else 0
    return out or None


def _mp4_other_tables(view):
    """The chunk tables of the sound and subtitle tracks of an MP4 or MOV, from its moov box; [] when there are none or the box is not in the kept copy."""
    off = 0
    while off < view.size:
        raw = view.get(off, 16)
        if raw is None or len(raw) < 8:
            return []
        size, kind = struct.unpack_from(">I4s", raw, 0)
        header = 8
        if size == 1 and len(raw) >= 16:
            size = struct.unpack_from(">Q", raw, 8)[0]
            header = 16
        elif size == 0:
            size = view.size - off
        if size < header:
            return []
        if kind == b"moov":
            if size > MP4_INDEX_MAX_BYTES:
                return []
            moov = view.get(off + header, size - header)
            if not moov:
                return []
            return [t for t in (_mp4_track_chunk_table(moov[s:e]) for k, s, e in _mp4_boxes(moov) if k == b"trak") if t]
        off += size
    return []


_other_tables = collections.OrderedDict()


def other_track_tables(path):
    """The chunk tables of a file's other tracks (MP4 and MOV only), made once per file from the kept copy and kept; [] for any other kind."""
    if ntpath.splitext(str(path))[1].lower() not in (".mp4", ".m4v", ".mov"):
        return []
    key = _key(path)
    kept = _read_kept(path)
    if kept is None:
        return []
    view, size, mtime_ns = kept
    with _lock:
        held = _other_tables.get(key)
        if held is not None and held[0] == size and held[1] == mtime_ns:
            return held[2]
    try:
        tables = _mp4_other_tables(view)
    except (struct.error, IndexError, ValueError):
        tables = []
    with _lock:
        _other_tables[key] = (size, mtime_ns, tables)
        while len(_other_tables) > KEYFRAME_TABLES_KEPT:
            _other_tables.popitem(last=False)
    return tables


_keyframe_tables = collections.OrderedDict()


def _avi_keyframes(view):
    """[(seconds, byte offset)] of the video key frames of an AVI, from the stream header in the kept head and the idx1 index at the end of the file (the part of it in the kept tail is enough: frames are counted back from the stream's length), or None."""
    head = view.head
    if len(head) < 64 or head[:4] != b"RIFF" or head[8:12] != b"AVI ":
        return None
    movi = head.find(b"movi")
    if movi < 12 or head[movi - 8:movi - 4] != b"LIST":
        return None
    scale = rate = length = None
    stream = -1
    pos = 12
    hdrl_end = min(len(head), movi - 8)
    pos = head.find(b"strl")
    while pos != -1 and pos < hdrl_end and scale is None:
        stream += 1
        strh = head.find(b"strh", pos, hdrl_end)
        if strh == -1:
            break
        body = head[strh + 8:strh + 8 + 56]
        if len(body) >= 36 and body[:4] == b"vids":
            _scale, _rate, _start, _length = struct.unpack_from("<4I", body, 20)
            if _scale and _rate:
                scale, rate, length = float(_scale), float(_rate), _length
            break
        pos = head.find(b"strl", pos + 4)
    if scale is None or not length:
        return None
    wanted = (b"%02ddc" % stream, b"%02ddb" % stream)
    tail = view.tail
    if not tail or len(tail) < 64:
        return None
    pos = tail.rfind(b"idx1")
    raw = None
    if pos != -1 and pos + 8 <= len(tail):
        idx_size = struct.unpack_from("<I", tail, pos + 4)[0]
        if idx_size and idx_size % 16 == 0 and view.tail_start + pos + 8 + idx_size <= view.size:
            raw = tail[pos + 8:pos + 8 + idx_size]
    if raw is None:
        raw = tail[len(tail) % 16:]
    count = len(raw) // 16
    if count < 2:
        return None
    entries = struct.unpack("<" + "4sIII" * count, raw[:count * 16])
    ids, flags, offs, sizes = entries[0::4], entries[1::4], entries[2::4], entries[3::4]
    video = [j for j, i in enumerate(ids) if i in wanted]
    if len(video) < 2:
        return None
    # WARNING: idx1 offsets are counted from the 'movi' word (nearly always) or from the start of the file; the first base whose chunk headers read back as the entries say is used, a wrong base points every key frame at the wrong bytes.
    base = movi
    for candidate in (movi, 0):
        seen = [view.get(candidate + offs[j], 4) for j in video[:6] + video[-6:]]
        seen = [(raw4, ids[j]) for raw4, j in zip(seen, video[:6] + video[-6:]) if raw4 is not None]
        if seen and all(a == b for a, b in seen):
            base = candidate
            break
    table = []
    total = int(length)
    for rank, j in enumerate(reversed(video)):
        frame = total - 1 - rank
        if frame < 0:
            break
        if flags[j] & 0x10:
            table.append((frame * scale / rate, base + offs[j]))
    table.reverse()
    return table or None


def _read_kept(path):
    """(view, size, mtime_ns) from the kept copy on this PC's disk, or None; never touches the share."""
    cache_path = _cache_file(path)
    meta = _load_meta(cache_path) if cache_path else None
    if meta is None:
        return None
    size, mtime_ns, head_len, tail_len = meta
    try:
        with open(cache_path, "rb") as fh:
            fh.seek(_META.size)
            head = fh.read(head_len)
            tail = fh.read(tail_len)
    except OSError as _swallowed_error:
        _log_swallowed("_read_kept: reading a kept index copy", _swallowed_error)
        return None
    return _View(head, tail, size), size, mtime_ns


def keyframe_table(path):
    """[(seconds, byte offset)] of a file's keyframes (cluster starts for Matroska), read from the kept head and tail without touching the share; None when the file's index is not in them or its kind is not understood. Made once per file and kept."""
    key = _key(path)
    meta = _load_meta(_cache_file(path) or "") if _cache_file(path) else None
    with _lock:
        held = _keyframe_tables.get(key)
        if held is not None and meta is not None and held[0] == meta[0] and held[1] == meta[1]:
            _keyframe_tables.move_to_end(key)
            return held[2]
    kept = _read_kept(path)
    if kept is None:
        return None
    view, size, mtime_ns = kept
    ext = ntpath.splitext(str(path))[1].lower()
    try:
        if ext in (".mkv", ".webm"):
            table = _mkv_keyframes(view)
        elif ext in (".mp4", ".m4v", ".mov"):
            table = _mp4_keyframes(view)
        elif ext == ".avi":
            table = _avi_keyframes(view)
        else:
            table = None
    except (struct.error, IndexError, ValueError):
        table = None
    with _lock:
        _keyframe_tables[key] = (size, mtime_ns, table)
        while len(_keyframe_tables) > KEYFRAME_TABLES_KEPT:
            _keyframe_tables.popitem(last=False)
    return table


def locate(path, seconds):
    """The byte offset of the keyframe at or just before this time, or None."""
    table = keyframe_table(path)
    if not table:
        return None
    lo, hi = 0, len(table)
    while lo < hi:
        mid = (lo + hi) // 2
        if table[mid][0] <= seconds:
            lo = mid + 1
        else:
            hi = mid
    return table[max(0, lo - 1)][1]


# ============================================================================
# SECTION: Snapshots of a channel's airing position
# ============================================================================

_snapshot_notes = cache_manager.memo("snapshot notes")
def snapshot_note(path):
    """One line on how the last snapshot read of this file went, or ''."""
    return _snapshot_notes.get(_key(path), "")


_snapshots = collections.OrderedDict()


def _snapshot_bytes_total():
    return sum(len(s["data"]) + sum(len(d) for _o, d in s.get("more") or []) for s in _snapshots.values())


def snapshot(path, from_seconds, to_seconds, max_seconds=None):
    """Keeps in memory the bytes of the file from the keyframe at from_seconds to the keyframe after to_seconds (at most SNAPSHOT_FILE_BYTES), so a tune that opens inside that time is answered from memory. An earlier snapshot of the same file that still overlaps is extended, not read again. Returns the bytes read from the share (0 when nothing was needed or possible). Background thread only."""
    max_seconds = SNAPSHOT_READ_MAX_SECONDS if max_seconds is None else max_seconds
    table = keyframe_table(path)
    if not table or to_seconds <= from_seconds:
        return 0
    start = locate(path, max(0.0, from_seconds - SNAPSHOT_BACK_SECONDS))
    after = next((off for t, off in table if t > to_seconds), None)
    kept = _read_kept(path)
    if start is None or kept is None:
        return 0
    size, mtime_ns = kept[1], kept[2]
    end = after if after is not None else size
    end = min(end, start + SNAPSHOT_FILE_BYTES, size)
    if end <= start:
        return 0
    key = _key(path)
    with _lock:
        held = _snapshots.get(key)
        if held is not None and (held["size"] != size or held["mtime_ns"] != mtime_ns):
            _snapshots.pop(key, None)
            held = None
        prefix = b""
        read_from = start
        if held is not None and held["start"] <= start < held["start"] + len(held["data"]):
            keep_from = start - held["start"]
            prefix = held["data"][keep_from:]
            read_from = start + len(prefix)
    if read_from >= end:
        with _lock:
            if held is not None:
                held["at"] = time.monotonic()
                held["t0"], held["t1"] = from_seconds, max(held["t1"], to_seconds)
        return 0
    src = _source_for(path)
    handle = None
    ok = False
    began = time.monotonic()
    failure = ""
    try:
        handle = src.acquire()
        # WARNING: a read that runs out of time keeps what it got (the next pass carries on from there), so a slow share or a big file still ends up held after a few passes instead of failing the same way every time.
        fresh = _read_partial(handle, read_from, end - read_from, began + max_seconds)
        ok = True
    except OSError as exc:
        fresh = b""
        failure = "read error: %s" % exc
    finally:
        if handle is not None:
            src.release(handle, ok=ok)
    complete = read_from + len(fresh) >= end
    covered_end = start + len(prefix) + len(fresh)
    if not (prefix or fresh):
        _snapshot_notes[key] = "nothing came back in %.1fs%s" % (time.monotonic() - began, (" (" + failure + ")") if failure else "")
        return 0
    t1 = to_seconds if complete else max((t for t, off in table if off <= covered_end), default=from_seconds)
    _snapshot_notes[key] = "%s: %.1f MB in %.1fs, covers %.0fs to %.0fs of the %.0fs asked" % (
        "complete" if complete else "PARTIAL, carries on next pass", len(fresh) / 1048576.0, time.monotonic() - began, from_seconds, t1, to_seconds)
    more = _snapshot_other_tracks(path, src, start, covered_end, from_seconds, t1, size, began + max_seconds, held)
    with _lock:
        _snapshots[key] = {"start": start, "data": prefix + fresh, "size": size, "mtime_ns": mtime_ns,
                           "t0": from_seconds, "t1": t1, "at": time.monotonic(), "more": more}
        _snapshots.move_to_end(key)
        while len(_snapshots) > 1 and _snapshot_bytes_total() > SNAPSHOT_TOTAL_BYTES:
            _snapshots.popitem(last=False)
    return len(fresh)


def _snapshot_other_tracks(path, src, start, end, from_seconds, to_seconds, size, deadline, held):
    """[(start, data)] of the sound tracks' bytes for the same time when they lie away from the picture's bytes (an MP4 whose sound is stored far from its picture): without them the player reads two places of the share at every step and a start takes many seconds."""
    # WARNING: owner logs 2026-11-25 (send_logs64): Die Hard 5's sound sits hundreds of MB from its picture (the player read at byte 262,511,181 at 6.5 s and 1,402,030,314 at 60 s); 15 opens and 98 s went by before its picture showed.
    out = []
    for table in other_track_tables(path):
        lo = None
        for t, off in table:
            if t <= max(0.0, from_seconds - SNAPSHOT_BACK_SECONDS):
                lo = off
            else:
                break
        if lo is None:
            lo = table[0][1]
        hi = next((off for t, off in table if t > to_seconds), size)
        hi = min(hi, lo + SNAPSHOT_FILE_BYTES, size)
        if hi <= lo or (start - OTHER_TRACK_NEAR_BYTES <= lo <= end + OTHER_TRACK_NEAR_BYTES):
            continue
        kept_part = next(((s0, d) for s0, d in ((held or {}).get("more") or []) if s0 <= lo and s0 + len(d) >= hi), None)
        if kept_part is not None:
            out.append(kept_part)
            continue
        if time.monotonic() >= deadline:
            break
        handle = None
        ok = False
        try:
            handle = src.acquire()
            data = _read_partial(handle, lo, hi - lo, deadline)
            ok = True
        except OSError as exc:
            data = b""
            log.warning("[SHARE RELAY] could not keep the sound of %s for its snapshot: %s", os.path.basename(str(path)), exc)
        finally:
            if handle is not None:
                src.release(handle, ok=ok)
        if data:
            out.append((lo, data))
    return out


def known_size(path):
    """The size of the file as the kept copy on this PC recorded it, or None; never touches the share."""
    meta = _load_meta(_cache_file(path)) if _cache_file(path) else None
    return meta[0] if meta else None


def snapshot_span(path):
    """(from_seconds, to_seconds) a file's snapshot covers, or None."""
    with _lock:
        held = _snapshots.get(_key(path))
        if held is None or time.monotonic() - held["at"] > SNAPSHOT_MAX_AGE_SECONDS:
            return None
        return held["t0"], held["t1"]


def drop_snapshot(path):
    """Frees one file's snapshot (the file just went off the air, owner 2026-11-26); True when one was held."""
    with _lock:
        return _snapshots.pop(_key(path), None) is not None


def drop_aged_snapshots():
    """Frees every snapshot older than SNAPSHOT_MAX_AGE_SECONDS (no longer used for playing); returns how many went."""
    now = time.monotonic()
    with _lock:
        old = [k for k, held in _snapshots.items() if now - held["at"] > SNAPSHOT_MAX_AGE_SECONDS]
        for key in old:
            _snapshots.pop(key, None)
    return len(old)


def drop_snapshots(keep_paths=()):
    """Frees the snapshots of every file not in keep_paths."""
    keep = {_key(p) for p in keep_paths}
    with _lock:
        for key in [k for k in _snapshots if k not in keep]:
            _snapshots.pop(key, None)


def _snapshot_covering(path, offset, size, mtime_ns):
    """(start, data) of this file's snapshot when it holds the byte at offset, else None."""
    with _lock:
        held = _snapshots.get(_key(path))
        if held is None or held["size"] != size or (mtime_ns and held["mtime_ns"] != mtime_ns):
            return None
        if time.monotonic() - held["at"] > SNAPSHOT_MAX_AGE_SECONDS:
            _snapshots.pop(_key(path), None)
            return None
        if held["start"] <= offset < held["start"] + len(held["data"]):
            return held["start"], held["data"]
        for part_start, part_data in held.get("more") or []:
            if part_start <= offset < part_start + len(part_data):
                return part_start, part_data
    return None


# ============================================================================
# SECTION: The byte stream of one request
# ============================================================================


def _segments(start, end, size, head_len, tail_len):
    """Splits the inclusive range into (kind, offset, length): 'head' and 'tail' come from the kept copy, 'share' from the share."""
    tail_start = size - tail_len
    pos = start
    while pos <= end:
        if pos < head_len:
            stop = min(end, head_len - 1)
            yield ("head", pos, stop - pos + 1)
        elif tail_len and pos >= tail_start:
            stop = end
            yield ("tail", pos, stop - pos + 1)
        else:
            stop = min(end, (tail_start - 1) if tail_len else end)
            if stop < pos:
                stop = end
            yield ("share", pos, stop - pos + 1)
        pos = stop + 1


def _note_slow_share_read(path, offset, seconds, had_snapshot):
    """One line (at most one per file each SLOW_WARN_REPEAT_SECONDS) when the share took SLOW_ANSWER_SECONDS or more to give the first bytes of a stretch the player asked for, saying whether a snapshot held the start of it (owner 2026-11-16: a tune 30 minutes after the start waited and the log did not say on what)."""
    # WARNING: log only the slow case; a normal read writes nothing.
    if seconds < SLOW_ANSWER_SECONDS:
        return
    _name = ntpath.basename(path)
    if time.monotonic() - _slow_warned.get(_name + "#read", -1e9) < SLOW_WARN_REPEAT_SECONDS:
        return
    if len(_slow_warned) > SLOW_WARN_FILES:
        _slow_warned.clear()
    _slow_warned[_name + "#read"] = time.monotonic()
    log.warning("[SHARE RELAY] slow read: %s the share took %.1fs to give the first bytes at byte %d (the start of this stretch %s in the in-memory snapshot)",
                _name, seconds, offset, "was" if had_snapshot else "was not")


def _produce(src, cache_path, meta, start, end, out_queue, cancel, permits):
    """Reads the range (kept copy for the ends, the share for the rest, a long way ahead of the consumer) into the queue; ends with None or an exception."""
    handle = None
    handle_ok = False
    try:
        size, _mtime, head_len, tail_len = meta if meta else (src.size, 0, 0, 0)
        for kind, offset, length in _segments(start, end, size, head_len, tail_len):
            done = 0
            if kind == "share":
                # WARNING: owner 2026-11-16: the bytes of a channel's airing position, kept ahead of the viewer, are answered from memory so a tune does not wait on the share for them.
                snap = _snapshot_covering(src.path, offset, size, meta[1] if meta else 0)
                if snap is not None:
                    snap_start, snap_data = snap
                    take = min(length, snap_start + len(snap_data) - offset)
                    sent = 0
                    while sent < take and not cancel.is_set():
                        chunk = snap_data[offset - snap_start + sent:offset - snap_start + sent + min(BLOCK_BYTES, take - sent)]
                        sent += len(chunk)
                        _put(out_queue, (True, chunk), cancel)
                    offset += take
                    length -= take
                    if length <= 0 or cancel.is_set():
                        continue
                if handle is None:
                    handle = src.acquire()
                handle.seek(offset)
                _t_first = time.monotonic()
                while done < length and not cancel.is_set():
                    if not _take_permit(permits, cancel):
                        break
                    # WARNING: owner 2026-11-25: "slow down video buffering, pick up speed on captions buffering, and back and forth"; a picture already well ahead reads at a pace while the captions or caching have their turn (performance_governor.video_pace_seconds).
                    _pace = _governor.video_pace_seconds(src.path) if _governor is not None else 0.0
                    if _pace > 0 and cancel.wait(_pace):
                        break
                    chunk = handle.read(min(BLOCK_BYTES, length - done))
                    if not chunk:
                        raise OSError("the share ended early at byte %d of %d" % (offset + done, size))
                    if done == 0:
                        _note_slow_share_read(src.path, offset, time.monotonic() - _t_first, snap is not None)
                    done += len(chunk)
                    _put(out_queue, (True, chunk), cancel)
            else:
                with open(cache_path, "rb") as fh:
                    base = _META.size + (0 if kind == "head" else head_len)
                    inner = offset if kind == "head" else offset - (size - tail_len)
                    fh.seek(base + inner)
                    while done < length and not cancel.is_set():
                        chunk = fh.read(min(BLOCK_BYTES, length - done))
                        if not chunk:
                            raise OSError("the kept index copy ended early")
                        done += len(chunk)
                        _put(out_queue, (False, chunk), cancel)
            if cancel.is_set():
                break
        handle_ok = True
        _put(out_queue, None, cancel)
    except BaseException as exc:
        _put(out_queue, exc, cancel)
    finally:
        if handle is not None:
            src.release(handle, ok=handle_ok)


def _take_permit(permits, cancel):
    while not cancel.is_set():
        if permits.acquire(timeout=WAIT_STEP_SECONDS):
            return True
    return False


def _put(out_queue, item, cancel):
    while not cancel.is_set():
        try:
            out_queue.put(item, timeout=WAIT_STEP_SECONDS)
            return
        except queue.Full:
            continue


# ============================================================================
# SECTION: The loopback server
# ============================================================================


def _parse_range(header, size):
    """(start, end) inclusive for a Range header, None for no header, or False for an unsatisfiable one."""
    if not header:
        return None
    match = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?$", header)
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        count = int(last)
        if count <= 0:
            return False
        return (max(0, size - count), size - 1)
    start = int(first)
    end = int(last) if last != "" else size - 1
    if start >= size or start > end:
        return False
    return (start, min(end, size - 1))


_REASONS = {200: "OK", 206: "Partial Content", 404: "Not Found", 416: "Range Not Satisfiable", 400: "Bad Request", 500: "Internal Server Error"}


def _send_head(conn, status, headers):
    lines = ["HTTP/1.0 %d %s" % (status, _REASONS.get(status, "Status"))]
    lines.extend("%s: %s" % (k, v) for k, v in headers)
    lines.append("Connection: close")
    conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def _read_request(conn):
    """(method, target, headers) of one request, or None."""
    conn.settimeout(REQUEST_READ_TIMEOUT_SECONDS)
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        raw += chunk
        if len(raw) > REQUEST_HEADER_MAX_BYTES:
            return None
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    parts = head[0].split(" ")
    if len(parts) < 2:
        return None
    headers = {}
    for line in head[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return parts[0].upper(), parts[1], headers


def _handle_connection(conn):
    try:
        request = _read_request(conn)
        if request is None:
            _send_head(conn, 400, [("Content-Length", "0")])
            return
        method, target, headers = request
        if method not in ("GET", "HEAD"):
            _send_head(conn, 400, [("Content-Length", "0")])
            return
        _serve(conn, method == "HEAD", target, headers)
    except (ConnectionError, socket.timeout):
        # WARNING: the player closes its connection whenever it seeks or changes file, and a player that sends nothing times out; both are the normal end of a request and write nothing.
        pass
    except (OSError, ValueError) as _swallowed_error:
        _log_swallowed("_handle_connection: answering the player", _swallowed_error)
    finally:
        try:
            conn.close()
        except OSError as _swallowed_error:
            _log_swallowed("_handle_connection: closing the player's connection", _swallowed_error)


def _serve(conn, head_only, target, request_headers):
    match = re.match(r"^/f/([0-9a-f]{16})(?:/.*)?$", target.split("?", 1)[0])
    with _lock:
        path = _tokens.get(match.group(1)) if match else None
    if not path:
        _send_head(conn, 404, [("Content-Length", "0")])
        return
    _t_in = time.monotonic()
    src = _source_for(path)
    cache_path = _cache_file(path)
    meta = _load_meta(cache_path) if cache_path else None
    # WARNING: owner 2026-11-16: a request whose size and time are already known and match the kept copy is answered at once; opening the share first (a second or more on a busy share) made every channel change wait before the player heard anything.
    if not (meta is not None and src.size is not None and meta[0] == src.size and meta[1] == src.mtime_ns):
        try:
            handle = src.acquire()
        except OSError as exc:
            _mark_failed(path, "open: %s" % exc)
            _send_head(conn, 404, [("Content-Length", "0")])
            return
        src.release(handle, ok=True)
        if src.size is None or src.size <= 0:
            _mark_failed(path, "empty file")
            _send_head(conn, 404, [("Content-Length", "0")])
            return
        if cache_path and (meta is None or meta[0] != src.size or meta[1] != src.mtime_ns):
            meta = ensure_index(path, source=src)
    size = src.size
    if size is None or size <= 0:
        _mark_failed(path, "empty file")
        _send_head(conn, 404, [("Content-Length", "0")])
        return
    rng = _parse_range(request_headers.get("range"), size)
    if rng is False:
        _send_head(conn, 416, [("Content-Range", "bytes */%d" % size), ("Content-Length", "0")])
        return
    if rng is None:
        start, end, status = 0, size - 1, 200
    else:
        (start, end), status = rng, 206
    head = [("Content-Type", _CONTENT_TYPES.get(ntpath.splitext(path)[1].lower(), "application/octet-stream")),
            ("Accept-Ranges", "bytes"), ("Content-Length", str(end - start + 1))]
    if status == 206:
        head.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
    _waited = time.monotonic() - _t_in
    if _waited >= SLOW_ANSWER_SECONDS:
        # WARNING: owner 2026-11-16: one line per slow answer (at most one per file each SLOW_WARN_REPEAT_SECONDS) so a slow tune names where it waited.
        _name = ntpath.basename(path)
        if time.monotonic() - _slow_warned.get(_name, -1e9) >= SLOW_WARN_REPEAT_SECONDS:
            if len(_slow_warned) > SLOW_WARN_FILES:
                _slow_warned.clear()
            _slow_warned[_name] = time.monotonic()
            log.warning("[SHARE RELAY] slow answer: %s waited %.1fs before the player heard anything (range %s)", _name, _waited, request_headers.get("range", "none"))
    _send_head(conn, status, head)
    if head_only:
        return
    cancel = threading.Event()
    out_queue = queue.Queue(maxsize=READ_AHEAD_BLOCKS + 2)
    permits = threading.Semaphore(RAMP_START_BLOCKS)
    widened = [0]
    producer = threading.Thread(target=_produce, args=(src, cache_path, meta, start, end, out_queue, cancel, permits),
                                daemon=True, name="ShareRelayRead")
    producer.start()
    conn.settimeout(STALL_SECONDS)
    _upto = start
    try:
        while True:
            try:
                item = out_queue.get(timeout=STALL_SECONDS)
            except queue.Empty:
                _mark_failed(path, "the share gave no data for %.0f s" % STALL_SECONDS)
                break
            if item is None:
                break
            if isinstance(item, BaseException):
                _mark_failed(path, "read: %s" % item)
                break
            from_share, chunk = item
            conn.sendall(chunk)
            if len(_served_at) > SERVED_AT_KEPT_FILES:
                _served_at.clear()
                _sent_upto.clear()
            _served_at[_key(path)] = time.monotonic()
            _upto += len(chunk)
            # WARNING: owner logs 2026-11-26 (send_logs70): the program counted only what the player read after its first picture, so the read-ahead it filled before it was lost and a streamed show read 2 s ahead for 16 minutes; the relay's own count of what it handed over is the show's real place.
            if _upto - start >= STREAM_MIN_BYTES:
                _sent_upto[_key(path)] = (_upto, time.monotonic())
            if from_share:
                permits.release()
                if widened[0] < READ_AHEAD_BLOCKS - RAMP_START_BLOCKS:
                    widened[0] += 1
                    permits.release()
    except ConnectionError:
        # WARNING: the player closing its connection part way through (a seek, a channel change) is the normal end of a request and writes nothing.
        pass
    except OSError as _swallowed_error:
        _log_swallowed("_serve: sending to the player", _swallowed_error)
    finally:
        cancel.set()
        try:
            while True:
                out_queue.get_nowait()
        except queue.Empty:
            pass


class _Server:
    """A loopback listener: one thread accepts, one thread per connection serves."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(LISTEN_BACKLOG)
        self.server_address = self.sock.getsockname()

    def serve_forever(self):
        self.sock.settimeout(WAIT_STEP_SECONDS)
        last_reap = time.monotonic()
        while True:
            if time.monotonic() - last_reap >= IDLE_REAP_INTERVAL_SECONDS:
                last_reap = time.monotonic()
                _reap_idle_handles()
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                log.warning("[SHARE RELAY] the loopback address stopped taking connections; the next file opened starts it again: %s", exc)
                break
            threading.Thread(target=_handle_connection, args=(conn,), daemon=True, name="ShareRelayConn").start()


def _ensure_server_locked():
    global _server, _server_thread
    if _server is not None and _server_thread is not None and _server_thread.is_alive():
        return True
    try:
        _server = _Server()
    except OSError as exc:
        log.warning("[SHARE RELAY] could not open the loopback address: %s", exc)
        _server = None
        return False
    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True, name="ShareRelay")
    _server_thread.start()
    # WARNING: the registered addresses name the old port after a restart, so they are dropped; one kept would point the player at a closed port.
    _tokens.clear()
    _token_of.clear()
    log.info("[SHARE RELAY] serving share files to the player at 127.0.0.1:%d", _server.server_address[1])
    return True
