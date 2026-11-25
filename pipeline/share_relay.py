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

# VALUES CHANGE HERE (the formats whose index the player reads before it can seek)
RELAY_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".flv")
# VALUES CHANGE HERE (bytes kept on this PC's disk from the start and from the end of each file: the container header and index)
HEAD_CACHE_BYTES = 3 * 1024 * 1024
TAIL_CACHE_BYTES = 6 * 1024 * 1024
# VALUES CHANGE HERE (the most disk the kept index copies may use; the least recently used are removed first)
CACHE_CAP_BYTES = 1200 * 1024 * 1024
# VALUES CHANGE HERE (the share is read in blocks of this size, and at most this many blocks are read ahead of the player per open request)
BLOCK_BYTES = 1024 * 1024
READ_AHEAD_BLOCKS = 24
# VALUES CHANGE HERE (a request starts by reading this many blocks of the share ahead and widens to READ_AHEAD_BLOCKS only while the player keeps reading, so a request the player drops after a seek does not leave 24 MB of wasted reads competing with the one that matters)
RAMP_START_BLOCKS = 2
# VALUES CHANGE HERE (the longest the first request of a file waits to copy its index before it is served without the copy)
INDEX_FILL_MAX_SECONDS = 8.0
# VALUES CHANGE HERE (seconds a file that failed through the relay is opened directly instead; seconds an idle share handle stays open)
FAIL_HOLD_SECONDS = 120.0
IDLE_HANDLE_SECONDS = 60.0
IDLE_REAP_INTERVAL_SECONDS = 15.0
# VALUES CHANGE HERE (seconds a file named by hold() keeps its idle handles after it was last named: the files airing now or next on a streamed channel, so the sweeper never closes the handle a show change or a channel change is about to use)
HOLD_SECONDS = 150.0
MAX_IDLE_HANDLES_PER_FILE = 2
MAX_REGISTERED_FILES = 256
# VALUES CHANGE HERE (seconds the share may give no data to a request before it is dropped and the file opens directly next time)
STALL_SECONDS = 30.0

_MAGIC = b"RIX1"
_META = struct.Struct("<4sqqqq")  # magic, size, mtime_ns, head_len, tail_len

_CONTENT_TYPES = {
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".flv": "video/x-flv",
}

_index_warned = {}
_slow_warned = {}
# VALUES CHANGE HERE (an answer to the player that took this long is written to the log once)
SLOW_ANSWER_SECONDS = 1.0
_lock = threading.RLock()
_enabled = True
_cache_dir = None
_server = None
_server_thread = None
_tokens = collections.OrderedDict()   # token -> path
_token_of = {}                        # path key -> token
_failed = {}                          # path key -> monotonic time of the failure
_sources = {}                         # path key -> _Source
_held = {}                            # path key -> monotonic time the hold ends
_stats = {"requests": 0, "cache_bytes": 0, "share_bytes": 0, "snapshot_bytes": 0, "fills": 0, "failures": 0}


def _key(path):
    return os.path.normpath(str(path or "")).lower()


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
        _stats["failures"] += 1
    log.warning("[SHARE RELAY] %s failed through the relay (%s); it opens directly for the next %.0f s",
                os.path.basename(str(path)), why, FAIL_HOLD_SECONDS)


def url_for(path):
    """The loopback address that serves this share file, or None when it should be opened directly. Never touches the disk or the network, so the screen thread can call it."""
    try:
        if not wants(path):
            return None
        key = _key(path)
        with _lock:
            if _recently_failed(key):
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
    except Exception:
        return None


def stats():
    with _lock:
        return dict(_stats)


# ---------------------------------------------------------------------------
# share file handles
# ---------------------------------------------------------------------------

class _Source:
    """One share file: its size and time, and a few idle handles kept open so each request does not pay the server's open again."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.idle = []   # (handle, idle_since)
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
                except OSError:
                    pass
        handle = open(self.path, "rb", buffering=0)
        st = os.fstat(handle.fileno())
        self.size, self.mtime_ns = int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        return handle

    def release(self, handle, ok=True):
        if not ok:
            try:
                handle.close()
            except OSError:
                pass
            return
        with self.lock:
            if len(self.idle) < MAX_IDLE_HANDLES_PER_FILE:
                self.idle.append((handle, time.monotonic()))
                return
        try:
            handle.close()
        except OSError:
            pass

    def close_all(self):
        with self.lock:
            handles, self.idle = self.idle, []
            self.size = None
            self.mtime_ns = None
        for handle, _since in handles:
            try:
                handle.close()
            except OSError:
                pass


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
        if len(_held) > 4 * MAX_REGISTERED_FILES:
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
                    except OSError:
                        pass
                else:
                    keep.append((handle, since))
            src.idle = keep


# ---------------------------------------------------------------------------
# the kept index copy
# ---------------------------------------------------------------------------

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
    except OSError:
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


_index_reach_checked = {}   # path key -> (size, mtime_ns) of a kept copy already measured for a cut-off MP4 index


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
                if len(_index_reach_checked) > 2000:
                    _index_reach_checked.clear()
                if reach is None or (reach[0] <= meta[2] and reach[1] <= meta[3]):
                    try:
                        os.utime(cache_path, None)
                    except OSError:
                        pass
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
                _stats["fills"] += 1
                # WARNING: a table made from the old kept copy (None when its index was cut off) is dropped, or the new copy would never be read for it.
                _keyframe_tables.pop(_key(path), None)
            _trim_cache()
            ok = True
            return (size, mtime_ns, head_len, tail_len)
        except OSError as exc:
            # WARNING: owner 2026-11-15: one missing file asked again every few seconds wrote this line 192 times; the same file is written once every 10 minutes.
            _now = time.monotonic()
            _name = os.path.basename(str(path))
            if _now - _index_warned.get(_name, -1e9) >= 600.0:
                if len(_index_warned) > 500:
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
            except OSError:
                continue
            if ".tmp" in name:
                if time.time() - st.st_mtime > 3600:
                    try:
                        os.remove(full)
                    except OSError:
                        pass
                continue
            entries.append((st.st_mtime, st.st_size, full))
            total += st.st_size
        entries.sort()
        while total > CACHE_CAP_BYTES and entries:
            _mtime, size, full = entries.pop(0)
            try:
                os.remove(full)
                total -= size
            except OSError:
                pass
    except OSError as exc:
        log.warning("[SHARE RELAY] trimming the index folder failed: %s", exc)


# ---------------------------------------------------------------------------
# the byte stream of one request
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# where a time is in the file (from the kept head and tail, no share read) and the snapshots made from it
# ---------------------------------------------------------------------------

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
    top = view.get(0, min(len(view.head), 1 << 20))
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
    # the Cues element: its header is read from the kept copy, then its payload
    head_bytes = view.get(seg + cues_at, 12)
    if not head_bytes:
        return None
    ident = _ebml_vint(head_bytes, 0, True)
    size = _ebml_vint(head_bytes, ident[1], False) if ident else None
    if not ident or ident[0] != 0x1C53BB6B or not size or size[0] < 0 or size[0] > (4 << 20):
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


# VALUES CHANGE HERE (the biggest MP4 index (moov box) kept whole on this PC; a bigger one is not read, as _mp4_keyframes does not read it)
MP4_INDEX_MAX_BYTES = 24 << 20


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
            if size > (24 << 20):
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
    # decode time of each sync sample, walking the time-to-sample runs once
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


_keyframe_tables = collections.OrderedDict()   # path key -> (size, mtime_ns, [(seconds, offset)] or None)
KEYFRAME_TABLES_KEPT = 64


def _avi_keyframes(view):
    """[(seconds, byte offset)] of the video key frames of an AVI, from the stream header in the kept head and the idx1 index at the end of the file (the part of it in the kept tail is enough: frames are counted back from the stream's length), or None."""
    head = view.head
    if len(head) < 64 or head[:4] != b"RIFF" or head[8:12] != b"AVI ":
        return None
    movi = head.find(b"movi")
    if movi < 12 or head[movi - 8:movi - 4] != b"LIST":
        return None
    # the first video stream's number, its frame time and its length in frames
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
    # the idx1 chunk ends the file: its entries (16 bytes each) are read from the kept tail; when the chunk's header is before the tail, the entries are cut from the end of the file
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
    # offsets are counted from the 'movi' word (nearly always) or from the start of the file: the first base whose chunk headers read back as the entries say wins
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
    except OSError:
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


# VALUES CHANGE HERE (main script, channel snapshots: the most memory all the kept mid-file snapshots may use together)
SNAPSHOT_TOTAL_BYTES = 256 * 1024 * 1024
# VALUES CHANGE HERE (main script, channel snapshots: the most one file's snapshot may hold)
SNAPSHOT_FILE_BYTES = 12 * 1024 * 1024
SNAPSHOT_MAX_AGE_SECONDS = 180.0
# VALUES CHANGE HERE (main script, channel snapshots: a snapshot starts this many seconds before the time asked, so a player that reads a little before its keyframe (audio interleaved ahead of it) is still inside it)
SNAPSHOT_BACK_SECONDS = 2.0

_snapshot_notes = {}   # path key -> how this file's last snapshot read went, for the log


def snapshot_note(path):
    """One line on how the last snapshot read of this file went, or ''."""
    return _snapshot_notes.get(_key(path), "")


_snapshots = collections.OrderedDict()   # path key -> {"start", "data", "size", "mtime_ns", "t0", "t1", "at"}


def _snapshot_bytes_total():
    return sum(len(s["data"]) for s in _snapshots.values())


def snapshot(path, from_seconds, to_seconds, max_seconds=4.0):
    """Keeps in memory the bytes of the file from the keyframe at from_seconds to the keyframe after to_seconds (at most SNAPSHOT_FILE_BYTES), so a tune that opens inside that time is answered from memory. An earlier snapshot of the same file that still overlaps is extended, not read again. Returns the bytes read from the share (0 when nothing was needed or possible). Background thread only."""
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
    with _lock:
        _stats["share_bytes"] += len(fresh)
        _snapshots[key] = {"start": start, "data": prefix + fresh, "size": size, "mtime_ns": mtime_ns,
                           "t0": from_seconds, "t1": t1, "at": time.monotonic()}
        _snapshots.move_to_end(key)
        while len(_snapshots) > 1 and _snapshot_bytes_total() > SNAPSHOT_TOTAL_BYTES:
            _snapshots.popitem(last=False)
    return len(fresh)


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
    return None


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
    """One line (at most one per file each 60 s) when the share took SLOW_ANSWER_SECONDS or more to give the first bytes of a stretch the player asked for, saying whether a snapshot held the start of it (owner 2026-11-16: a tune 30 minutes after the start waited and the log did not say on what)."""
    # WARNING: log only the slow case; a normal read writes nothing.
    if seconds < SLOW_ANSWER_SECONDS:
        return
    _name = ntpath.basename(path)
    if time.monotonic() - _slow_warned.get(_name + "#read", -1e9) < 60.0:
        return
    if len(_slow_warned) > 200:
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
                        with _lock:
                            _stats["snapshot_bytes"] += len(chunk)
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
                    chunk = handle.read(min(BLOCK_BYTES, length - done))
                    if not chunk:
                        raise OSError("the share ended early at byte %d of %d" % (offset + done, size))
                    if done == 0:
                        _note_slow_share_read(src.path, offset, time.monotonic() - _t_first, snap is not None)
                    done += len(chunk)
                    with _lock:
                        _stats["share_bytes"] += len(chunk)
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
                        with _lock:
                            _stats["cache_bytes"] += len(chunk)
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
        if permits.acquire(timeout=0.5):
            return True
    return False


def _put(out_queue, item, cancel):
    while not cancel.is_set():
        try:
            out_queue.put(item, timeout=0.5)
            return
        except queue.Full:
            continue


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
    conn.settimeout(10.0)
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        raw += chunk
        if len(raw) > 16384:
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
    except (OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _serve(conn, head_only, target, request_headers):
    match = re.match(r"^/f/([0-9a-f]{16})(?:/.*)?$", target.split("?", 1)[0])
    with _lock:
        path = _tokens.get(match.group(1)) if match else None
    if not path:
        _send_head(conn, 404, [("Content-Length", "0")])
        return
    with _lock:
        _stats["requests"] += 1
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
        # WARNING: owner 2026-11-16: one line per slow answer (at most one per file each 60 s) so a slow tune names where it waited.
        _name = ntpath.basename(path)
        if time.monotonic() - _slow_warned.get(_name, -1e9) >= 60.0:
            if len(_slow_warned) > 200:
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
            if from_share:
                permits.release()
                if widened[0] < READ_AHEAD_BLOCKS - RAMP_START_BLOCKS:
                    widened[0] += 1
                    permits.release()
    except OSError:
        pass
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
        self.sock.listen(64)
        self.server_address = self.sock.getsockname()
        self._stop = threading.Event()

    def serve_forever(self):
        self.sock.settimeout(0.5)
        last_reap = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_reap >= IDLE_REAP_INTERVAL_SECONDS:
                last_reap = time.monotonic()
                _reap_idle_handles()
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_handle_connection, args=(conn,), daemon=True, name="ShareRelayConn").start()

    def shutdown(self):
        self._stop.set()

    def server_close(self):
        try:
            self.sock.close()
        except OSError:
            pass


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
    # the registered addresses name the old port after a restart, so they are dropped
    _tokens.clear()
    _token_of.clear()
    log.info("[SHARE RELAY] serving share files to the player at 127.0.0.1:%d", _server.server_address[1])
    return True


def shutdown():
    """Stops the relay and closes every kept share handle."""
    global _server, _server_thread
    with _lock:
        server, _server = _server, None
        _server_thread = None
        sources = list(_sources.values())
        _sources.clear()
        _held.clear()
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except OSError:
            pass
    for src in sources:
        src.close_all()
