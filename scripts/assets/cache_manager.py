"""Cache manager: one place that keeps every cache, memo, log and temp file in check.

Owns: bounded in-memory memos (an entry limit, and an age limit for the ones that only matter
while a show is near), the folders on disk that only hold rebuildable copies (a size and an age
cap), the program's left-behind temp files, the size caps of the growing log files, the release
of a show's cache once the show has gone off the air, and one log line for what each sweep freed.
Does not own: saved data (main/data, retro_config.json), which is never deleted here, or what a
cache holds (each owner fills its own; this only limits and frees).

WARNING: owner 2026-11-26: "make sure we are not going to build up and crash over time from maxing
out the memory. this is meant to run for as long as you want ... just make a cache manager.
everything that gets cached gets checked and managed and clears old unused cache files and temp
files." Every memo that grows with the library or with time is made here, so none can grow without
a limit, and the sweep is the one place old cache and temp files are removed.
"""

from __future__ import annotations

import collections
import logging
import os
import shutil
import tempfile
import threading
import time

log = logging.getLogger("retro_tv.cache_manager")

try:
    import performance_governor as _governor
except Exception as _governor_import_error:
    _governor = None
    log.warning("[CACHE] could not import performance_governor, using the fallback values written in cache_manager.py: %s", _governor_import_error)

try:
    from performance_governor import log_swallowed as _log_swallowed
except Exception:
    def _log_swallowed(where, error):
        """Fallback used only when performance_governor cannot be imported: the failure line goes to this module's log."""
        log.warning("[CACHE] %s: %s", where, error)


# ============================================================================
# SECTION: Tunables
# ============================================================================

# WARNING: the fallback numbers below must match the CACHE_ values of the same names in performance_governor.py; they are used only when that module fails to import.
SWEEP_SECONDS = _governor.CACHE_SWEEP_SECONDS if _governor is not None else 600.0
PATH_MEMO_MAX = _governor.CACHE_PATH_MEMO_MAX if _governor is not None else 50000
SHOW_MEMO_MAX = _governor.CACHE_SHOW_MEMO_MAX if _governor is not None else 2000
SMALL_MEMO_MAX = _governor.CACHE_SMALL_MEMO_MAX if _governor is not None else 256
SHOW_MEMO_MAX_AGE_SECONDS = _governor.CACHE_SHOW_MEMO_MAX_AGE_SECONDS if _governor is not None else 21600.0
TEMP_STRAY_MAX_AGE_SECONDS = _governor.CACHE_TEMP_STRAY_MAX_AGE_SECONDS if _governor is not None else 21600.0
LOG_FILE_MAX_BYTES = _governor.CACHE_LOG_FILE_MAX_BYTES if _governor is not None else 20971520
EXTRACTED_ROMS_MAX_BYTES = _governor.CACHE_EXTRACTED_ROMS_MAX_BYTES if _governor is not None else 4294967296
REPORT_SECONDS = _governor.CACHE_REPORT_SECONDS if _governor is not None else 3600.0

# VALUES CHANGE HERE: the name starts of the temp files and folders the program makes in the system temp folder; one older than TEMP_STRAY_MAX_AGE_SECONDS was left by a job that ended badly (a crash, a power cut) and is removed.
PROGRAM_TEMP_PREFIXES = ("asr_speed_", "asr_", "live_asr_", "caption_scan_", "retrotv_tray_logs_", "retrotv_logs_",
                         "retro-tv-rollback-", "retro-tv-shortcuts-", "retro-tv-update-helper-", "retro_tv_tests_")


# ============================================================================
# SECTION: Bounded memos
# ============================================================================

_registry_lock = threading.Lock()
_memos = []


class BoundedDict(collections.OrderedDict):
    """A dict that keeps at most `limit` entries, dropping the one set longest ago, and (with max_age_seconds) drops on each sweep the entries not set again for that long. Reads and writes from any thread."""

    def __init__(self, name, limit, max_age_seconds=None):
        super().__init__()
        self.name = name
        self.limit = max(1, int(limit))
        self.max_age_seconds = max_age_seconds
        self.evicted = 0
        self._stamps = {}
        self._lock = threading.RLock()

    def __setitem__(self, key, value):
        with self._lock:
            super().__setitem__(key, value)
            self.move_to_end(key)
            if self.max_age_seconds is not None:
                self._stamps[key] = time.monotonic()
            while super().__len__() > self.limit:
                old_key, _old = super().popitem(last=False)
                self._stamps.pop(old_key, None)
                self.evicted += 1

    def __delitem__(self, key):
        with self._lock:
            super().__delitem__(key)
            self._stamps.pop(key, None)

    def setdefault(self, key, default=None):
        with self._lock:
            if key in self:
                return self[key]
            self[key] = default
            return default

    def update(self, *args, **kwargs):
        with self._lock:
            for key, value in dict(*args, **kwargs).items():
                self[key] = value

    def pop(self, key, *default):
        with self._lock:
            self._stamps.pop(key, None)
            return super().pop(key, *default)

    def popitem(self, last=True):
        with self._lock:
            key, value = super().popitem(last=last)
            self._stamps.pop(key, None)
            return key, value

    def clear(self):
        with self._lock:
            super().clear()
            self._stamps.clear()

    def __reduce__(self):
        return (dict, (dict(self),))

    def drop_old(self, now=None):
        """Drops the entries not set for max_age_seconds; returns how many went."""
        if self.max_age_seconds is None:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            old = [key for key, at in self._stamps.items() if now - at > self.max_age_seconds]
            for key in old:
                super().pop(key, None)
                self._stamps.pop(key, None)
            return len(old)


class BoundedSet:
    """A set that keeps at most `limit` members, dropping the one added longest ago, and (with max_age_seconds) drops on each sweep the members not added again for that long."""

    def __init__(self, name, limit, max_age_seconds=None):
        self._items = BoundedDict(name, limit, max_age_seconds)
        self.name = name

    limit = property(lambda self: self._items.limit)
    max_age_seconds = property(lambda self: self._items.max_age_seconds)
    evicted = property(lambda self: self._items.evicted)

    def add(self, item):
        self._items[item] = True

    def update(self, items):
        for item in items:
            self._items[item] = True

    def discard(self, item):
        self._items.pop(item, None)

    def remove(self, item):
        if item not in self._items:
            raise KeyError(item)
        self._items.pop(item, None)

    def pop(self):
        return self._items.popitem(last=False)[0]

    def clear(self):
        self._items.clear()

    def drop_old(self, now=None):
        return self._items.drop_old(now)

    def __contains__(self, item):
        return item in self._items

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(list(self._items.keys()))

    def __bool__(self):
        return len(self._items) > 0

    def __repr__(self):
        return "BoundedSet(%r, %d of %d)" % (self.name, len(self), self.limit)


def memo(name, limit=None, max_age_seconds=None):
    """A registered BoundedDict: limit defaults to PATH_MEMO_MAX (a memo with one entry per file)."""
    made = BoundedDict(name, PATH_MEMO_MAX if limit is None else limit, max_age_seconds)
    with _registry_lock:
        _memos.append(made)
    return made


def flags(name, limit=None, max_age_seconds=None):
    """A registered BoundedSet: limit defaults to PATH_MEMO_MAX (one member per file)."""
    made = BoundedSet(name, PATH_MEMO_MAX if limit is None else limit, max_age_seconds)
    with _registry_lock:
        _memos.append(made)
    return made


def show_memo(name):
    """A memo for something only needed while a show is near (what was primed for it, its picture size): SHOW_MEMO_MAX entries, each dropped SHOW_MEMO_MAX_AGE_SECONDS after it was last set."""
    return memo(name, SHOW_MEMO_MAX, SHOW_MEMO_MAX_AGE_SECONDS)


def small_memo(name, limit=None):
    """A memo for a small lookup (fonts by size, scaled pictures by size): SMALL_MEMO_MAX entries."""
    return memo(name, SMALL_MEMO_MAX if limit is None else limit)


def memo_sizes():
    """[(name, entries, limit, dropped so far)] of every registered memo, biggest first."""
    with _registry_lock:
        rows = [(m.name, len(m), m.limit, m.evicted) for m in _memos]
    return sorted(rows, key=lambda row: -row[1])


# ============================================================================
# SECTION: Folders, log files and temp files on disk
# ============================================================================

_folders = []
_log_files = []
_temp_rules = []


def manage_folder(name, path_fn, max_bytes=None, max_age_seconds=None, in_use_fn=None, depth=1):
    """Registers a folder (path_fn returns one folder or a list of them, counted together) that only holds copies the program can make again: on each sweep its oldest files go first until it is under max_bytes, and files older than max_age_seconds go; in_use_fn(path) True keeps a file (the ROM being played). depth 2 manages each subfolder's files (one folder per console)."""
    with _registry_lock:
        _folders.append({"name": name, "path_fn": path_fn, "max_bytes": max_bytes, "max_age": max_age_seconds,
                         "in_use": in_use_fn, "depth": depth})


def manage_log_file(name, path_fn, max_bytes=None):
    """Registers a log file the program appends to: past max_bytes (default LOG_FILE_MAX_BYTES) it is moved to '<name>_previous' (replacing the older one) and a new file starts, so one log never fills the drive."""
    with _registry_lock:
        _log_files.append({"name": name, "path_fn": path_fn, "max_bytes": max_bytes or LOG_FILE_MAX_BYTES})


def manage_temp_files(name, dir_fn, prefixes, max_age_seconds=None):
    """Registers a folder where the program makes temp files and folders starting with one of the prefixes; on each sweep one older than max_age_seconds (default TEMP_STRAY_MAX_AGE_SECONDS) is removed."""
    with _registry_lock:
        _temp_rules.append({"name": name, "dir_fn": dir_fn, "prefixes": tuple(prefixes), "max_age": max_age_seconds or TEMP_STRAY_MAX_AGE_SECONDS})


def previous_log_path(path):
    """'app_log.txt' -> 'app_log_previous.txt': where a full log is moved."""
    root, ext = os.path.splitext(path)
    return root + "_previous" + ext


def cap_log_file(path, max_bytes):
    """Moves the log at path to its _previous name when it is past max_bytes; True when it was moved. Never raises."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return False
        os.replace(path, previous_log_path(path))
        return True
    except FileNotFoundError:
        # WARNING: the normal end: the log has not been written yet.
        return False
    except OSError as cap_error:
        _log_swallowed("cache manager: capping the log %s" % path, cap_error)
        return False


def _files_of(folder, depth):
    found = []
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if os.path.isdir(full):
            if depth > 1:
                found += _files_of(full, depth - 1)
            continue
        try:
            st = os.stat(full)
        except OSError as stat_error:
            _log_swallowed("cache manager: reading %s" % full, stat_error)
            continue
        found.append((max(st.st_mtime, st.st_atime), st.st_size, full))
    return found


def _trim_folder(rule, now):
    """(files removed, bytes freed) for one managed folder."""
    removed, freed = 0, 0
    try:
        found = rule["path_fn"]()
    except Exception as path_error:
        _log_swallowed("cache manager: finding the folder of %s" % rule["name"], path_error)
        return removed, freed
    folders = [f for f in ([found] if isinstance(found, str) else list(found or ())) if f and os.path.isdir(f)]
    files = []
    for folder in folders:
        try:
            files += _files_of(folder, rule["depth"])
        except OSError as list_error:
            _log_swallowed("cache manager: listing %s" % folder, list_error)
    files.sort()
    total = sum(size for _at, size, _path in files)
    for at, size, path in files:
        too_old = rule["max_age"] is not None and now - at > rule["max_age"]
        too_big = rule["max_bytes"] is not None and total > rule["max_bytes"]
        if not (too_old or too_big):
            continue
        if rule["in_use"] is not None:
            try:
                if rule["in_use"](path):
                    continue
            except Exception as use_error:
                _log_swallowed("cache manager: asking whether %s is in use" % path, use_error)
                continue
        try:
            os.remove(path)
        except OSError as remove_error:
            _log_swallowed("cache manager: removing %s" % path, remove_error)
            continue
        removed += 1
        freed += size
        total -= size
    return removed, freed


def _sweep_temp(rule, now):
    """(entries removed, bytes freed) for one temp-file rule."""
    removed, freed = 0, 0
    try:
        folder = rule["dir_fn"]()
    except Exception as path_error:
        _log_swallowed("cache manager: finding the temp folder of %s" % rule["name"], path_error)
        return removed, freed
    if not folder or not os.path.isdir(folder):
        return removed, freed
    for name in os.listdir(folder):
        if not name.startswith(rule["prefixes"]):
            continue
        full = os.path.join(folder, name)
        try:
            st = os.stat(full)
            if now - st.st_mtime <= rule["max_age"]:
                continue
            if os.path.isdir(full):
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(full) for f in fs)
                shutil.rmtree(full)
            else:
                size = st.st_size
                os.remove(full)
        except OSError as remove_error:
            _log_swallowed("cache manager: removing the left-behind temp %s" % full, remove_error)
            continue
        removed += 1
        freed += size
    return removed, freed


# ============================================================================
# SECTION: A show going off the air
# ============================================================================

_show_hooks = []


def on_show_change(name, fn):
    """Registers fn(previous_path, current_path), called when the picture on screen moves to a new file, to free what was cached only for the file that just left the air."""
    with _registry_lock:
        _show_hooks.append((name, fn))


def show_changed(previous_path, current_path):
    """Called by the program when a new file's picture starts: each registered hook frees the cache of the file that went off the air. Never raises."""
    if not previous_path or previous_path == current_path:
        return
    with _registry_lock:
        hooks = list(_show_hooks)
    for name, fn in hooks:
        try:
            fn(previous_path, current_path)
        except Exception as hook_error:
            _log_swallowed("cache manager: freeing the %s of %s" % (name, os.path.basename(str(previous_path))), hook_error)


# ============================================================================
# SECTION: The sweep
# ============================================================================

_last_report = [0.0]
_sweeper = {"thread": None, "stop": threading.Event()}


def sweep(now=None):
    """One pass over everything registered: old memo entries, managed folders, log sizes and left-behind temp files. Returns a dict of what was freed and writes one [CACHE] line when anything was."""
    wall = time.time() if now is None else now
    mono = time.monotonic()
    freed = {"memo entries": 0, "files": 0, "bytes": 0, "logs moved": 0, "temp": 0}
    with _registry_lock:
        memos, folders, log_files, temp_rules = list(_memos), list(_folders), list(_log_files), list(_temp_rules)
    for made in memos:
        try:
            freed["memo entries"] += made.drop_old(mono)
        except Exception as memo_error:
            _log_swallowed("cache manager: dropping old entries of %s" % made.name, memo_error)
    for rule in folders:
        files, size = _trim_folder(rule, wall)
        freed["files"] += files
        freed["bytes"] += size
    for rule in log_files:
        try:
            path = rule["path_fn"]()
        except Exception as path_error:
            _log_swallowed("cache manager: finding the log %s" % rule["name"], path_error)
            continue
        if path and cap_log_file(path, rule["max_bytes"]):
            freed["logs moved"] += 1
    for rule in temp_rules:
        files, size = _sweep_temp(rule, wall)
        freed["temp"] += files
        freed["bytes"] += size
    if freed["memo entries"] or freed["files"] or freed["logs moved"] or freed["temp"]:
        log.info("[CACHE] freed %d old memo entries, %d cache files, %d left-behind temp files and folders (%.1f MB), moved %d full log(s) aside",
                 freed["memo entries"], freed["files"], freed["temp"], freed["bytes"] / 1048576.0, freed["logs moved"])
    if mono - _last_report[0] >= REPORT_SECONDS:
        _last_report[0] = mono
        biggest = ", ".join("%s %d/%d" % (name, size, limit) for name, size, limit, _dropped in memo_sizes()[:8])
        log.info("[CACHE] memo sizes (entries/limit): %s", biggest or "none yet")
    return freed


def _sweep_loop(stop):
    """The sweeper thread: a sweep right away (the temp files left by the last run), then every SWEEP_SECONDS."""
    while not stop.is_set():
        try:
            sweep()
        except Exception as sweep_error:
            _log_swallowed("cache manager: a sweep", sweep_error)
        stop.wait(SWEEP_SECONDS)


def start():
    """Starts the sweeper thread once; later calls do nothing."""
    with _registry_lock:
        if _sweeper["thread"] is not None:
            return
        thread = threading.Thread(target=_sweep_loop, args=(_sweeper["stop"],), name="cache-manager", daemon=True)
        _sweeper["thread"] = thread
    thread.start()


def stop():
    """Stops the sweeper thread (at shutdown)."""
    _sweeper["stop"].set()


def system_temp_dir():
    return tempfile.gettempdir()


# WARNING: every copy of the program (the app, the server service) removes its own left-behind temp files from the system temp folder.
manage_temp_files("program temp files", system_temp_dir, PROGRAM_TEMP_PREFIXES)


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = ["BoundedDict", "BoundedSet", "memo", "flags", "show_memo", "small_memo", "memo_sizes", "manage_folder",
           "manage_log_file", "manage_temp_files", "cap_log_file", "previous_log_path", "on_show_change", "show_changed",
           "sweep", "start", "stop"]
